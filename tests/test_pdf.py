"""PDF -> page images and images -> PDF through the real registry and runner."""

import threading

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageOps

from fileconverter.conversion import runner
from fileconverter.conversion.backends import pdf as pdf_backend
from fileconverter.conversion.backends.pdf import parse_page_range, strip_jpeg_metadata
from fileconverter.core.errors import (
    InvalidInputError,
    InvalidOptionError,
    UnsupportedConversionError,
)
from fileconverter.core.models import ConversionRequest, JobStatus
from helpers import make_docx, make_image, temp_files, write_pdf

RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)


def run(registry, source, target, extra=(), cancel=None, on_progress=None, **options):
    request = ConversionRequest(source, target, source.parent, options, tuple(extra))
    return runner.run_job(request, registry, cancel, on_progress)


def outputs(folder, suffix):
    return sorted(p.name for p in folder.iterdir() if p.suffix == suffix)


def color_at(path, x=0.5, y=0.5):
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        return rgb.getpixel((int(rgb.width * x), int(rgb.height * y)))


def close_to(actual, expected, tolerance=40):
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected, strict=True))


# --- PDF -> images ------------------------------------------------------------


def test_pdf_is_detected(registry, tmp_path):
    source = write_pdf(tmp_path / "doc.pdf", [(100, 100, RED)])
    assert registry.detect_format(source).id == "pdf"
    assert {f.id for f in registry.targets_for("pdf")} == {"png", "jpeg"}


def test_multi_page_pdf_gives_one_png_per_page_in_order(registry, tmp_path):
    source = write_pdf(tmp_path / "doc.pdf", [(144, 72, RED), (144, 72, GREEN), (144, 72, BLUE)])
    original = source.read_bytes()

    result = run(registry, source, "png")

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert [p.name for p in result.output_paths] == [
        "doc_page_001.png",
        "doc_page_002.png",
        "doc_page_003.png",
    ]
    assert [color_at(p) for p in result.output_paths] == [RED, GREEN, BLUE]
    with Image.open(result.output_paths[0]) as page:
        assert page.format == "PNG"
        assert page.size == (300, 150)  # 2 x 1 inch at the default 150 DPI
    assert source.read_bytes() == original
    assert temp_files(tmp_path) == []


def test_jpeg_output_and_resolution_option(registry, tmp_path):
    source = write_pdf(tmp_path / "doc.pdf", [(72, 72, BLUE)])

    result = run(registry, source, "jpeg", dpi=300, quality=80)

    with Image.open(result.output_path) as page:
        assert page.format == "JPEG"
        assert page.size == (300, 300)
    assert result.output_path.name == "doc_page_001.jpg"
    assert close_to(color_at(result.output_path), BLUE)


def test_page_range_selection(registry, tmp_path):
    source = write_pdf(tmp_path / "doc.pdf", [(72, 72, c) for c in (RED, GREEN, BLUE, RED)])

    result = run(registry, source, "png", pages="4, 2-3")

    assert [p.name for p in result.output_paths] == [
        "doc_page_002.png",
        "doc_page_003.png",
        "doc_page_004.png",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", [0, 1, 2, 3, 4]), ("2", [1]), ("1-2, 5", [0, 1, 4]), ("4-", [3, 4]), ("-2", [0, 1]),
     ("3,1,3", [0, 2])],
)  # fmt: skip
def test_parse_page_range(text, expected):
    assert parse_page_range(text, 5) == expected


@pytest.mark.parametrize("text", ["0", "6", "3-2", "a", "1,,2", "1-2-3", "-"])
def test_parse_page_range_rejects(text):
    with pytest.raises(InvalidOptionError, match="has 5 pages"):
        parse_page_range(text, 5)


def test_invalid_range_produces_nothing(registry, tmp_path):
    source = write_pdf(tmp_path / "doc.pdf", [(72, 72, RED)])
    result = run(registry, source, "png", pages="2")
    assert isinstance(result.error, InvalidOptionError)
    assert outputs(tmp_path, ".png") == [] and temp_files(tmp_path) == []


def test_corrupt_pdf(registry, tmp_path):
    source = tmp_path / "bad.pdf"
    source.write_bytes(b"%PDF-1.7\n" + b"\x00garbage" * 50)

    result = run(registry, source, "png")

    assert isinstance(result.error, InvalidInputError)
    assert "damaged" in str(result.error)


def test_password_protected_pdf(registry, tmp_path):
    source = write_pdf(tmp_path / "secret.pdf", [(72, 72, RED)], encrypted=True)

    result = run(registry, source, "png")

    assert isinstance(result.error, InvalidInputError)
    assert "password-protected" in str(result.error)


def test_page_limit_is_checked_before_rendering(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_backend, "MAX_PAGES_PER_JOB", 2)
    source = write_pdf(tmp_path / "doc.pdf", [(72, 72, RED)] * 3)

    result = run(registry, source, "png")

    assert isinstance(result.error, InvalidOptionError)
    assert outputs(tmp_path, ".png") == []
    assert run(registry, source, "png", pages="1-2").status is JobStatus.SUCCEEDED


def test_oversized_page_is_rendered_smaller_with_warning(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_backend, "MAX_PAGE_PIXELS", 10_000)
    source = write_pdf(tmp_path / "poster.pdf", [(720, 720, GREEN)])

    result = run(registry, source, "png")

    with Image.open(result.output_path) as page:
        assert page.width * page.height <= 10_000
    assert any("very large" in w for w in result.warnings)


def test_cancel_between_pages_leaves_no_partial_set(registry, tmp_path):
    source = write_pdf(tmp_path / "doc.pdf", [(72, 72, RED)] * 5)
    cancel = threading.Event()

    def on_progress(fraction):
        if fraction and fraction < 1:
            cancel.set()  # after the first page was rendered

    result = run(registry, source, "png", cancel=cancel, on_progress=on_progress)

    assert result.status is JobStatus.CANCELLED
    assert outputs(tmp_path, ".png") == []
    assert temp_files(tmp_path) == []


def test_existing_page_images_are_not_overwritten(registry, tmp_path):
    source = write_pdf(tmp_path / "doc.pdf", [(72, 72, RED), (72, 72, GREEN)])
    taken = tmp_path / "doc_page_001.png"
    taken.write_bytes(b"user file")

    result = run(registry, source, "png")

    assert [p.name for p in result.output_paths] == ["doc_page_001 (1).png", "doc_page_002.png"]
    assert taken.read_bytes() == b"user file"


def test_failed_commit_rolls_back_the_whole_set(registry, tmp_path, monkeypatch):
    source = write_pdf(tmp_path / "doc.pdf", [(72, 72, RED)] * 3)
    real_move = runner._move_into_place
    calls = []

    def fail_on_third(*args):
        calls.append(1)
        if len(calls) == 3:
            raise runner.OutputError("disk full")
        return real_move(*args)

    monkeypatch.setattr(runner, "_move_into_place", fail_on_third)

    result = run(registry, source, "png")

    assert result.status is JobStatus.FAILED
    assert outputs(tmp_path, ".png") == []  # the two committed pages were removed again
    assert temp_files(tmp_path) == []


# --- images -> PDF ------------------------------------------------------------


def page_sizes(path):
    pdf = pdfium.PdfDocument(str(path))
    try:
        return [tuple(round(v, 2) for v in pdf[i].get_size()) for i in range(len(pdf))]
    finally:
        pdf.close()


def embedded_images(path):
    """(filters, raw bytes, PIL image) of the single image on each page."""
    pdf = pdfium.PdfDocument(str(path))
    try:
        result = []
        for page in pdf:
            image = next(page.get_objects(filter=(pdfium.raw.FPDF_PAGEOBJ_IMAGE,)))
            bitmap = image.get_bitmap(render=False)
            result.append((image.get_filters(), bytes(image.get_data()), bitmap.to_pil().copy()))
        return result
    finally:
        pdf.close()


def render_page(path, index=0):
    pdf = pdfium.PdfDocument(str(path))
    try:
        return pdf[index].render(scale=1).to_pil().convert("RGB").copy()
    finally:
        pdf.close()


def test_png_becomes_lossless_pdf_page_at_96_dpi(registry, tmp_path):
    pixels = Image.effect_noise((40, 30), 90).convert("RGB")  # any recompression would show
    source = tmp_path / "noise.png"
    pixels.save(source)

    result = run(registry, source, "pdf")

    assert result.output_path.name == "noise.pdf"
    assert page_sizes(result.output_path) == [(30.0, 22.5)]  # 40x30 px at 96 DPI
    filters, _, embedded = embedded_images(result.output_path)[0]
    assert filters == ["FlateDecode"]
    assert embedded.convert("RGB").tobytes() == pixels.tobytes()  # bit-exact pixels


def test_image_resolution_sets_page_size(registry, tmp_path):
    source = make_image(tmp_path / "scan.png", size=(300, 600), dpi=(300, 300))
    result = run(registry, source, "pdf")
    assert page_sizes(result.output_path) == [(72.0, 144.0)]


def test_jpeg_data_is_embedded_unchanged_without_metadata(registry, tmp_path):
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"
    source = make_image(tmp_path / "photo.jpg", "JPEG", size=(64, 48), exif=exif, quality=70)
    original = source.read_bytes()

    result = run(registry, source, "pdf")

    filters, data, _ = embedded_images(result.output_path)[0]
    assert filters == ["DCTDecode"]  # still JPEG: not decoded and recompressed
    assert data == strip_jpeg_metadata(original)
    assert b"SecretCam" not in result.output_path.read_bytes()
    assert source.read_bytes() == original


def test_several_images_combine_into_one_pdf_in_order(registry, tmp_path):
    first = make_image(tmp_path / "1.png", size=(96, 96), color=RED)
    second = make_image(tmp_path / "2.jpg", "JPEG", size=(192, 96), color=GREEN)
    third = make_image(tmp_path / "3.webp", "WEBP", size=(96, 192), color=BLUE, lossless=True)

    result = run(registry, first, "pdf", extra=[second, third])

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert [p.name for p in result.output_paths] == ["1.pdf"]
    assert page_sizes(result.output_path) == [(72.0, 72.0), (144.0, 72.0), (72.0, 144.0)]
    colors = [render_page(result.output_path, i).getpixel((10, 10)) for i in range(3)]
    assert [close_to(c, e) for c, e in zip(colors, (RED, GREEN, BLUE), strict=True)] == [True] * 3


QUADRANTS = [RED, GREEN, BLUE, (255, 255, 255)]  # top-left, top-right, bottom-left, bottom-right


def quadrant_image(width=64, height=32):
    image = Image.new("RGB", (width, height))
    for index, color in enumerate(QUADRANTS):
        x, y = (index % 2) * width // 2, (index // 2) * height // 2
        image.paste(color, (x, y, x + width // 2, y + height // 2))
    return image


def quadrant_colors(image):
    w, h = image.size
    return [
        image.getpixel((int(w * fx), int(h * fy))) for fy in (0.25, 0.75) for fx in (0.25, 0.75)
    ]  # noqa: E501


@pytest.mark.parametrize("orientation", range(1, 9))
@pytest.mark.parametrize("kind", ["JPEG", "PNG"])
def test_exif_orientation_is_applied(registry, tmp_path, orientation, kind):
    exif = Image.Exif()
    exif[0x0112] = orientation
    suffix = ".jpg" if kind == "JPEG" else ".png"
    source = tmp_path / f"o{orientation}{suffix}"
    quadrant_image().save(source, kind, exif=exif, **({"quality": 95} if kind == "JPEG" else {}))
    with Image.open(source) as original:
        expected = ImageOps.exif_transpose(original).convert("RGB")

    result = run(registry, source, "pdf")

    rendered = render_page(result.output_path)
    assert (rendered.width > rendered.height) == (expected.width > expected.height)
    for got, want in zip(quadrant_colors(rendered), quadrant_colors(expected), strict=True):
        assert close_to(got, want), (orientation, kind, quadrant_colors(rendered))


def test_transparency_becomes_white_with_warning(registry, tmp_path):
    image = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    image.paste((255, 0, 0, 255), (0, 0, 20, 20))
    source = tmp_path / "logo.png"
    image.save(source)

    result = run(registry, source, "pdf")

    rendered = render_page(result.output_path)
    assert close_to(rendered.getpixel((rendered.width - 2, 5)), (255, 255, 255), 5)
    assert close_to(rendered.getpixel((2, 5)), RED, 5)
    assert any("Transparent" in w for w in result.warnings)


def test_one_bad_image_fails_the_whole_pdf(registry, tmp_path):
    good = make_image(tmp_path / "good.png")
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"junk" * 30)

    result = run(registry, good, "pdf", extra=[bad])

    assert isinstance(result.error, InvalidInputError)
    assert outputs(tmp_path, ".pdf") == [] and temp_files(tmp_path) == []


def test_non_images_cannot_be_combined(registry, tmp_path):
    image = make_image(tmp_path / "a.png")
    document = make_docx(tmp_path / "b.docx", "<w:p><w:r><w:t>x</w:t></w:r></w:p>")

    result = run(registry, image, "pdf", extra=[document])

    assert isinstance(result.error, UnsupportedConversionError)


def test_existing_pdf_is_not_overwritten(registry, tmp_path):
    source = make_image(tmp_path / "a.png")
    taken = tmp_path / "a.pdf"
    taken.write_bytes(b"user pdf")

    result = run(registry, source, "pdf")

    assert result.output_path.name == "a (1).pdf"
    assert taken.read_bytes() == b"user pdf"


def test_cancel_while_combining_cleans_up(registry, tmp_path):
    images = [make_image(tmp_path / f"{i}.png") for i in range(4)]
    cancel = threading.Event()

    def on_progress(fraction):
        if fraction:
            cancel.set()

    result = run(
        registry, images[0], "pdf", extra=images[1:], cancel=cancel, on_progress=on_progress
    )

    assert result.status is JobStatus.CANCELLED
    assert outputs(tmp_path, ".pdf") == [] and temp_files(tmp_path) == []


def test_strip_jpeg_metadata_rejects_non_jpeg():
    with pytest.raises(InvalidInputError):
        strip_jpeg_metadata(b"\x89PNG....")
