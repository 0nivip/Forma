"""PDF -> PNG/JPEG page images, and images -> PDF, with pypdfium2 (PDFium).

PDFium is not thread-safe, even across different documents, so every use of it goes
through one process-wide lock; concurrent PDF jobs therefore run one at a time.

Images -> PDF is lossless where possible:
  * JPEG files (RGB or grayscale) keep their compressed image data unchanged (only
    metadata segments are removed); their EXIF orientation is applied by the
    placement matrix, not by re-encoding the pixels.
  * Every other image is decoded and stored losslessly (Flate). Transparency is
    flattened onto white, as on a printed page; EXIF orientation is applied.
  * Each image gets its own page, exactly the size of the image at its stored
    resolution (96 DPI if it has none); pixels are never resampled.
  * Metadata: by default EXIF/GPS/XMP/ICC segments are removed from embedded JPEGs and
    decoded images carry none. With 'Preserve metadata' JPEGs are embedded byte for
    byte; other images' metadata cannot be stored (a warning says so). No document
    information (author, title) is added to the PDF either way.
"""

import contextlib
import io
import logging
import math
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError

from fileconverter.conversion.backends.images import (
    JPEG_QUALITY,
    load_first_frame,
    prepare_for_target,
)
from fileconverter.core.converter import JobContext, OutputPart
from fileconverter.core.errors import InvalidInputError, InvalidOptionError, OutputError
from fileconverter.core.formats import IMAGE_FORMATS
from fileconverter.core.models import IntOption, OptionValue, Route, TextOption

logger = logging.getLogger(__name__)

_PDFIUM_LOCK = threading.Lock()  # see module docstring
LOCK_POLL_S = 0.2

DPI_OPTION = IntOption("dpi", "Resolution (DPI)", default=150, minimum=36, maximum=600)
PAGES_OPTION = TextOption("pages", "Pages", placeholder="All pages (e.g. 1-3, 5)")
MAX_PAGES_PER_JOB = 1000
MAX_PAGE_PIXELS = 60_000_000  # ~240 MB as a 32-bit bitmap
DEFAULT_IMAGE_DPI = 96
MAX_PAGE_POINTS = 14_400  # 200 inches: the largest page many PDF viewers accept

NOTE_PAGES = "Creates one image file per page (rendered pixels, not editable text or vectors)."
NOTE_TO_PDF = (
    "One page per image at its original resolution; JPEGs are embedded unchanged, other "
    "images losslessly. Transparency becomes white; metadata is removed unless "
    "'Preserve metadata' is on."
)

_RANGE_PART = re.compile(r"^(\d+)?\s*(-)?\s*(\d+)?$")


class PdfConverter:
    def __init__(self, image_sources: Sequence[str] | None = None) -> None:
        # Image formats Pillow can actually read here (AVIF depends on the build).
        self._image_sources = (
            list(image_sources) if image_sources is not None else [f.id for f in IMAGE_FORMATS]
        )

    def routes(self) -> Iterator[Route]:
        yield Route("pdf", "png", (DPI_OPTION, PAGES_OPTION), (NOTE_PAGES,), multi_output=True)
        jpeg_options = (DPI_OPTION, PAGES_OPTION, JPEG_QUALITY)
        yield Route("pdf", "jpeg", jpeg_options, (NOTE_PAGES,), multi_output=True)
        for format_id in self._image_sources:
            yield Route(format_id, "pdf", notes=(NOTE_TO_PDF,), multi_input=True)

    def convert(
        self,
        source: Path,
        destination: Path,
        route: Route,
        options: Mapping[str, OptionValue],
        ctx: JobContext,
    ) -> list[OutputPart] | None:
        if route.source == "pdf":
            return self._render_pages(source, destination, route.target, options, ctx)
        self.convert_group([source], destination, route, options, ctx)
        return None

    # --- PDF -> images ----------------------------------------------------------

    def _render_pages(
        self, source: Path, folder: Path, target: str, options, ctx: JobContext
    ) -> list[OutputPart]:
        dpi = int(options["dpi"])
        with _pdfium(ctx):
            pdf = _open_pdf(source)
            try:
                count = len(pdf)
                if count == 0:
                    raise InvalidInputError(f"'{source.name}' has no pages.")
                pages = parse_page_range(str(options["pages"]), count)
                if len(pages) > MAX_PAGES_PER_JOB:
                    raise InvalidOptionError(
                        f"That is {len(pages)} pages; convert at most {MAX_PAGES_PER_JOB} "
                        "pages at a time (use the Pages option)."
                    )
                digits = max(3, len(str(count)))
                parts = []
                for done, index in enumerate(pages):
                    ctx.raise_if_cancelled()
                    path = folder / f"{done:05d}.{target}"
                    _render_page(pdf, index, dpi, path, target, options, ctx)
                    parts.append(OutputPart(path, f"page_{index + 1:0{digits}d}"))
                    ctx.report_progress((done + 1) / len(pages))
                return parts
            finally:
                pdf.close()

    # --- images -> PDF ------------------------------------------------------------

    def convert_group(
        self,
        sources: Sequence[Path],
        destination: Path,
        route: Route,
        options: Mapping[str, OptionValue],
        ctx: JobContext,
    ) -> None:
        with _pdfium(ctx):
            pdf = pdfium.PdfDocument.new()
            try:
                for done, source in enumerate(sources):
                    ctx.raise_if_cancelled()
                    _add_image_page(pdf, source, ctx)
                    ctx.report_progress((done + 1) / (len(sources) + 1))
                ctx.raise_if_cancelled()
                try:
                    pdf.save(str(destination))
                except (pdfium.PdfiumError, OSError) as exc:
                    raise OutputError(f"Could not write the PDF: {exc}") from exc
            finally:
                pdf.close()
            _check_pdf(destination, expected_pages=len(sources))


def parse_page_range(text: str, count: int) -> list[int]:
    """'' -> all pages; '1-3, 5, 8-' -> zero-based sorted page indexes."""
    if not text.strip():
        return list(range(count))
    selected: set[int] = set()
    for part in text.split(","):
        match = _RANGE_PART.match(part.strip())
        if not part.strip() or not match or not (match[1] or match[3]):
            raise _bad_range(text, count)
        start = int(match[1]) if match[1] else 1
        end = (int(match[3]) if match[3] else count) if match[2] else start
        if not 1 <= start <= end <= count:
            raise _bad_range(text, count)
        selected.update(range(start - 1, end))
    return sorted(selected)


def _bad_range(text: str, count: int) -> InvalidOptionError:
    return InvalidOptionError(
        f"Invalid page range '{text}'. Use e.g. 1-3, 5 (the document has {count} pages)."
    )


@contextlib.contextmanager
def _pdfium(ctx: JobContext):
    """Hold the PDFium lock, staying cancellable while waiting for it."""
    while not _PDFIUM_LOCK.acquire(timeout=LOCK_POLL_S):
        ctx.raise_if_cancelled()
    try:
        yield
    finally:
        _PDFIUM_LOCK.release()


def _open_pdf(source: Path) -> pdfium.PdfDocument:
    try:
        pdf = pdfium.PdfDocument(str(source))
    except pdfium.PdfiumError as exc:
        logger.info("PDFium cannot open '%s': %s", source, exc)
        if "password" in str(exc).lower():
            raise InvalidInputError(
                f"'{source.name}' is password-protected; open it in a PDF reader and save an "
                "unprotected copy first."
            ) from exc
        raise InvalidInputError(f"'{source.name}' is damaged or not a valid PDF.") from exc
    pdf.init_forms()  # so that filled-in form fields are rendered too
    return pdf


def _render_page(pdf, index: int, dpi: int, path: Path, target: str, options, ctx) -> None:
    page = pdf[index]
    try:
        width_pt, height_pt = page.get_size()
        if width_pt <= 0 or height_pt <= 0:
            raise InvalidInputError(f"Page {index + 1} has no size.")
        scale = dpi / 72
        pixels = width_pt * scale * height_pt * scale
        if pixels > MAX_PAGE_PIXELS:
            scale *= math.sqrt(MAX_PAGE_PIXELS / pixels)
            ctx.warn(
                f"Page {index + 1} is very large; it was rendered at "
                f"{round(scale * 72)} DPI instead of {dpi} DPI."
            )
        try:
            bitmap = page.render(scale=scale)
        except pdfium.PdfiumError as exc:
            raise InvalidInputError(f"Page {index + 1} could not be rendered.") from exc
        try:
            image = bitmap.to_pil()
        finally:
            bitmap.close()
    finally:
        page.close()

    effective_dpi = round(scale * 72)
    try:
        if target == "jpeg":
            image.convert("RGB").save(
                path, "JPEG", quality=int(options["quality"]), dpi=(effective_dpi,) * 2
            )
        else:
            image.save(path, "PNG", dpi=(effective_dpi,) * 2)
    except OSError as exc:
        raise OutputError(f"Could not write the image of page {index + 1}: {exc}") from exc


def _add_image_page(pdf, source: Path, ctx: JobContext) -> None:
    try:
        with Image.open(source) as probe:
            info = (probe.format, probe.mode, probe.size, probe.info.get("dpi"))
            orientation = probe.getexif().get(0x0112, 1)
            has_metadata = bool(probe.info.get("exif") or probe.info.get("xmp"))
            if probe.format == "JPEG" and probe.mode in ("RGB", "L"):
                probe.load()  # decode once to reject damaged data before embedding it
    except Image.DecompressionBombError as exc:
        raise InvalidInputError(f"'{source.name}' is too large to process safely.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise InvalidInputError(f"'{source.name}' is corrupted or unreadable: {exc}") from exc

    image_format, mode, (width_px, height_px), dpi = info
    image_object = pdfium.PdfImage.new(pdf)
    if image_format == "JPEG" and mode in ("RGB", "L"):
        # The original compressed image data, no re-encoding. With 'Preserve metadata'
        # the file is embedded byte for byte (its EXIF stays inside the image stream);
        # otherwise its metadata segments are removed first.
        data = source.read_bytes()
        if not ctx.preserve_metadata:
            data = strip_jpeg_metadata(data)
        image_object.load_jpeg(io.BytesIO(data), inline=True)
    else:
        if ctx.preserve_metadata and has_metadata:
            ctx.warn(
                f"The metadata of '{source.name}' cannot be stored in the PDF and was not kept."
            )
        image = load_first_frame(source, ctx)  # applies EXIF orientation to the pixels
        image = prepare_for_target(image, "jpeg", ctx)  # white background, 8-bit, no palette
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")  # CMYK; prepare_for_target already warned
        width_px, height_px = image.size
        orientation = 1
        bitmap = pdfium.PdfBitmap.from_pil(image)
        try:
            image_object.set_bitmap(bitmap)  # stored Flate-compressed (lossless)
        finally:
            bitmap.close()

    points_per_pixel = 72 / _usable_dpi(dpi)
    width, height = width_px * points_per_pixel, height_px * points_per_pixel
    rotated = orientation in (5, 6, 7, 8)
    page_width, page_height = (height, width) if rotated else (width, height)
    shrink = min(1.0, MAX_PAGE_POINTS / max(page_width, page_height))
    width, height = width * shrink, height * shrink
    page = pdf.new_page(page_width * shrink, page_height * shrink)
    try:
        image_object.set_matrix(pdfium.PdfMatrix(*_placement(orientation, width, height)))
        page.insert_obj(image_object)
        page.gen_content()
    finally:
        page.close()


def strip_jpeg_metadata(data: bytes) -> bytes:
    """Remove metadata segments from a JPEG without touching the compressed image.

    Kept: APP0 (JFIF), APP14 (Adobe colour transform) and every non-APP marker.
    Dropped: EXIF/XMP (APP1), ICC profiles and MPF (APP2), IPTC (APP13), other APPn,
    comments, and anything after the end-of-image marker (e.g. embedded preview images).
    PDF viewers ignore ICC profiles inside embedded JPEGs anyway.
    """
    if data[:2] != b"\xff\xd8":
        raise InvalidInputError("Not a JPEG file.")
    out = bytearray(b"\xff\xd8")
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise InvalidInputError("Damaged JPEG structure.")
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        segment = data[pos : pos + 2 + length]
        if len(segment) != 2 + length or length < 2:
            raise InvalidInputError("Damaged JPEG structure.")
        if marker == 0xDA:  # start of scan: copy the image data up to end-of-image
            end = data.find(b"\xff\xd9", pos + 2 + length)
            return bytes(out + data[pos : (end + 2 if end != -1 else len(data))])
        keep = not (0xE0 <= marker <= 0xEF or marker == 0xFE) or marker in (0xE0, 0xEE)
        if keep:
            out += segment
        pos += 2 + length
    raise InvalidInputError("Damaged JPEG structure.")


def _usable_dpi(dpi: object) -> float:
    try:
        value = float(dpi[0])  # type: ignore[index]
    except (TypeError, ValueError, IndexError):
        return DEFAULT_IMAGE_DPI
    return value if 36 <= value <= 2400 else DEFAULT_IMAGE_DPI  # ignore missing/bogus values


def _placement(orientation: int, w: float, h: float) -> tuple[float, ...]:
    """PDF image matrix that shows a w x h image upright for an EXIF orientation (1-8).

    PDF draws an image into the unit square; the matrix maps that square onto the page.
    """
    return {
        1: (w, 0, 0, h, 0, 0),
        2: (-w, 0, 0, h, w, 0),  # mirrored horizontally
        3: (-w, 0, 0, -h, w, h),  # rotated 180
        4: (w, 0, 0, -h, 0, h),  # mirrored vertically
        5: (0, -w, -h, 0, h, w),  # transposed
        6: (0, -w, h, 0, 0, w),  # rotated 90 clockwise
        7: (0, w, h, 0, 0, 0),  # transversed
        8: (0, w, -h, 0, h, 0),  # rotated 90 counter-clockwise
    }.get(orientation, (w, 0, 0, h, 0, 0))


def _check_pdf(path: Path, expected_pages: int) -> None:
    """Re-open the written PDF to make sure it is readable and complete."""
    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        raise OutputError("The written PDF cannot be opened.") from exc
    try:
        if len(pdf) != expected_pages:
            raise OutputError(f"The written PDF has {len(pdf)} pages instead of {expected_pages}.")
    finally:
        pdf.close()
