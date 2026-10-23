"""Raster image conversion with Pillow."""

import logging
from collections.abc import Iterator, Mapping
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from fileconverter.core.converter import JobContext
from fileconverter.core.errors import InvalidInputError, OutputError
from fileconverter.core.models import IntOption, Route

logger = logging.getLogger(__name__)

# Our format id -> Pillow's format name.
PILLOW_FORMATS = {
    "png": "PNG",
    "jpeg": "JPEG",
    "webp": "WEBP",
    "bmp": "BMP",
    "tiff": "TIFF",
}

# Pixel modes each target can store as-is. Anything else is converted first.
# See https://pillow.readthedocs.io/en/stable/handbook/concepts.html#modes
SUPPORTED_MODES = {
    "png": {"1", "L", "LA", "P", "RGB", "RGBA", "I;16"},
    "jpeg": {"L", "RGB", "CMYK"},
    "webp": {"RGB", "RGBA"},
    "bmp": {"1", "L", "P", "RGB"},
    "tiff": {"1", "L", "LA", "P", "RGB", "RGBA", "CMYK", "I;16"},
}
ALPHA_MODES = {"RGBA", "LA", "PA", "RGBa", "La"}
# 16-bit grayscale as Pillow opens it from PNG/TIFF.
HIGH_BIT_GRAY_MODES = {"I", "I;16", "I;16B", "I;16L"}
GRAYSCALE_MODES = {"1", "L", "LA", "La", "F"} | HIGH_BIT_GRAY_MODES
WHITE = (255, 255, 255)

TIFF_BITS_PER_SAMPLE = 258  # TIFF tag id
# PNG layout: 8-byte signature, then IHDR = length(4) type(4) width(4) height(4) bit depth(1).
PNG_BIT_DEPTH_OFFSET = 24

# Pillow advises against JPEG quality above 95: files grow, quality barely improves.
JPEG_QUALITY = IntOption("quality", "Quality", default=90, minimum=1, maximum=95)
WEBP_QUALITY = IntOption("quality", "Quality", default=90, minimum=1, maximum=100)

NOTE_NO_ALPHA = "Transparent areas will be filled with white."
NOTE_LOSSY = "Lossy format: some image detail is lost."
NOTE_FIRST_FRAME = "Only the first frame or page of an animated or multi-page image is converted."
NOTE_NO_METADATA = (
    "Metadata (EXIF, GPS location, camera info) is removed unless 'Preserve metadata' is on."
)
# Formats whose Pillow writer stores EXIF / XMP passed to save() (verified by tests).
EXIF_TARGETS = {"jpeg", "png", "webp", "tiff"}
XMP_TARGETS = {"jpeg", "webp"}  # Pillow's PNG and TIFF writers do not store XMP
EXIF_ORIENTATION = 0x0112
EXIF_SUB_IFDS = (0x8769, 0x8825, 0xA005)  # Exif, GPS and Interoperability blocks


class ImageConverter:
    def routes(self) -> Iterator[Route]:
        for source in PILLOW_FORMATS:
            for target in PILLOW_FORMATS:
                if source != target:
                    yield Route(source, target, _options_for(target), _notes_for(source, target))

    def convert(
        self,
        source: Path,
        destination: Path,
        route: Route,
        options: Mapping[str, int],
        ctx: JobContext,
    ) -> None:
        image = load_first_frame(source, ctx)
        ctx.raise_if_cancelled()

        original_mode = image.mode
        # Take metadata out of image.info and clear the rest: some Pillow writers embed
        # info values (ICC profile, comments, ...) automatically. Only what is passed to
        # save() explicitly below ends up in the output. EXIF and XMP here no longer
        # contain an orientation: load_first_frame() already rotated the pixels.
        icc_profile = image.info.pop("icc_profile", None)
        exif = _clean_exif(image.info.pop("exif", None))
        xmp = image.info.pop("xmp", None)
        image.info = {}
        image = prepare_for_target(image, route.target, ctx)
        if _color_family(image.mode) != _color_family(original_mode):
            icc_profile = None  # a profile for one color space is wrong for another
        if not ctx.preserve_metadata:
            icc_profile = exif = xmp = None
        ctx.raise_if_cancelled()

        pillow_format = PILLOW_FORMATS[route.target]
        save_args = _save_args(route.target, options, icc_profile, exif, xmp)
        if route.target == "tiff" and exif is not None and _has_sub_ifds(exif):
            # libtiff (used for LZW) cannot write EXIF sub-blocks such as GPS; Pillow's own
            # TIFF writer can, but only without compression.
            save_args["compression"] = None
            ctx.warn("The TIFF file was saved uncompressed so that its EXIF/GPS data is kept.")
        try:
            image.save(destination, format=pillow_format, **save_args)
        except OSError as exc:
            raise OutputError(f"Could not write {pillow_format} file: {exc}") from exc

        _verify_output(destination, pillow_format)


def _options_for(target: str) -> tuple[IntOption, ...]:
    if target == "jpeg":
        return (JPEG_QUALITY,)
    if target == "webp":
        return (WEBP_QUALITY,)
    return ()


def _notes_for(source: str, target: str) -> tuple[str, ...]:
    notes = []
    if not _supports_alpha(target):
        notes.append(NOTE_NO_ALPHA)
    if target in ("jpeg", "webp"):
        notes.append(NOTE_LOSSY)
    if source in ("webp", "tiff"):  # the source formats here that can hold several frames
        notes.append(NOTE_FIRST_FRAME)
    notes.append(NOTE_NO_METADATA)
    return tuple(notes)


def load_first_frame(source: Path, ctx: JobContext) -> Image.Image:
    try:
        with Image.open(source) as opened:
            frame_count = getattr(opened, "n_frames", 1)
            if frame_count > 1:
                ctx.warn(f"The image has {frame_count} frames/pages; only the first was converted.")
            opened.seek(0)
            bit_depth = _source_bit_depth(opened, source)
            opened.load()
            # Apply the EXIF "Orientation" tag to the pixels, so that a photo taken
            # in portrait mode still looks upright in formats that ignore EXIF.
            # Returns a new in-memory image, so it stays valid after the file is closed.
            image = ImageOps.exif_transpose(opened)
    except Image.DecompressionBombError as exc:
        # Pillow refuses images with absurd pixel counts that could exhaust memory.
        raise InvalidInputError(f"'{source.name}' is too large to process safely.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        # Pillow reports corrupted/truncated data through several exception types.
        raise InvalidInputError(f"'{source.name}' is corrupted or unreadable: {exc}") from exc

    # Pillow keeps 16-bit grayscale, but silently reads 16-bit color (RGB, RGBA, LA)
    # as 8 bits per channel. The scaling is correct; only the extra precision is lost.
    if bit_depth > 8 and image.mode not in HIGH_BIT_GRAY_MODES:
        ctx.warn("16-bit color was reduced to 8 bits per channel when reading the file.")

    # Palette/RGB images can mark one color as transparent via info["transparency"].
    # Turning that into a real alpha channel lets the rest of the code check one thing.
    if "transparency" in image.info:
        image = image.convert("RGBA")
    return image


def _source_bit_depth(opened: Image.Image, source: Path) -> int:
    """Bits per channel stored in the file; 8 when the format cannot say otherwise."""
    if opened.format == "TIFF":
        bits = opened.tag_v2.get(TIFF_BITS_PER_SAMPLE, 8)
        return max(bits) if isinstance(bits, tuple) else bits
    if opened.format == "PNG":
        # Read the IHDR field directly: Pillow does not expose the PNG bit depth.
        with source.open("rb") as file:
            header = file.read(PNG_BIT_DEPTH_OFFSET + 1)
        if len(header) > PNG_BIT_DEPTH_OFFSET:
            return header[PNG_BIT_DEPTH_OFFSET]
    return 8


def prepare_for_target(image: Image.Image, target: str, ctx: JobContext) -> Image.Image:
    """Convert the pixel mode into one the target format can store."""
    if image.mode in ALPHA_MODES and not _supports_alpha(target):
        image, had_transparency = _flatten_on_white(image)
        if had_transparency:
            ctx.warn(
                "Transparent areas were filled with white: the target format has no transparency."
            )

    if image.mode in HIGH_BIT_GRAY_MODES and image.mode not in SUPPORTED_MODES[target]:
        image = _reduce_to_8bit_gray(image)
        ctx.warn("16-bit grayscale was reduced to 8-bit: the target format cannot store more.")

    if image.mode == "CMYK" and "CMYK" not in SUPPORTED_MODES[target]:
        # Pillow's CMYK -> RGB is a simple formula, not an ICC-based conversion.
        ctx.warn(
            "CMYK colors were converted to RGB without color management; "
            "colors may look different from the original."
        )

    if image.mode not in SUPPORTED_MODES[target]:
        image = image.convert(_fallback_mode(image, target))
    return image


def _reduce_to_8bit_gray(image: Image.Image) -> Image.Image:
    # Pillow's convert("L") clips 16-bit values (anything above 255 becomes white),
    # so scale the 0..65535 range down to 0..255 first.
    return image.convert("I").point(lambda value: value * (1 / 256)).convert("L")


def _flatten_on_white(image: Image.Image) -> tuple[Image.Image, bool]:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    min_alpha, _ = alpha.getextrema()
    background = Image.new("RGB", rgba.size, WHITE)
    background.paste(rgba, mask=alpha)
    return background, min_alpha < 255


def _fallback_mode(image: Image.Image, target: str) -> str:
    supported = SUPPORTED_MODES[target]
    if image.mode in ALPHA_MODES and "RGBA" in supported:
        return "RGBA"
    if image.mode in GRAYSCALE_MODES and "L" in supported:
        return "L"
    return "RGB"  # every target supports RGB


def _supports_alpha(target: str) -> bool:
    return "RGBA" in SUPPORTED_MODES[target]


def _color_family(mode: str) -> str:
    if mode == "CMYK":
        return "cmyk"
    if mode in GRAYSCALE_MODES:
        return "gray"
    return "rgb"


def _clean_exif(raw: bytes | None) -> bytes | None:
    """Re-serialize EXIF in the standard form every writer expects, without orientation.

    WebP stores EXIF without the "Exif\\0\\0" header that JPEG needs; re-serializing
    fixes that. The orientation tag is dropped because the pixels are already upright.
    """
    if not raw:
        return None
    exif = Image.Exif()
    try:
        exif.load(raw)
    except Exception:  # damaged EXIF must not stop the conversion
        logger.info("Ignoring unreadable EXIF data", exc_info=True)
        return None
    exif.pop(EXIF_ORIENTATION, None)
    return exif.tobytes() if len(exif) else None


def _has_sub_ifds(exif_bytes: bytes) -> bool:
    exif = Image.Exif()
    exif.load(exif_bytes)
    return any(tag in exif for tag in EXIF_SUB_IFDS)


def _save_args(
    target: str,
    options: Mapping[str, int],
    icc_profile: bytes | None,
    exif: bytes | None,
    xmp: bytes | str | None,
) -> dict:
    args: dict = {}
    if target in ("jpeg", "webp"):
        args["quality"] = options["quality"]
    if target == "tiff":
        args["compression"] = "tiff_lzw"  # lossless; uncompressed TIFF is needlessly huge
    if icc_profile and target != "bmp":
        args["icc_profile"] = icc_profile
    if exif and target in EXIF_TARGETS:
        args["exif"] = exif
    if xmp and target in XMP_TARGETS:
        args["xmp"] = xmp.encode("utf-8") if isinstance(xmp, str) else xmp
    return args


def _verify_output(path: Path, expected_format: str) -> None:
    """Re-open the written file to make sure it is a valid image of the right type."""
    try:
        with Image.open(path) as written:
            if written.format != expected_format:
                raise OutputError(f"Output is {written.format}, expected {expected_format}.")
            written.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise OutputError(f"The written file is not a valid image: {exc}") from exc
