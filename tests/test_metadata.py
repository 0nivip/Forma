"""The 'Preserve metadata' setting across image, images-to-PDF and FFmpeg backends.

Fixtures carry known markers: an EXIF camera maker and GPS block, an XMP creator, an
ICC profile and a JPEG comment. Each test checks exactly which of them survive.
"""

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageCms, ImageOps

from fileconverter.conversion import runner
from fileconverter.conversion.backends.media import STRIP_METADATA, MediaConverter
from fileconverter.core.models import ConversionRequest, JobStatus
from fileconverter.infrastructure.ffmpeg import MediaInfo
from test_media import fake_tools

MAKE, GPS_IFD, ORIENTATION = 0x010F, 0x8825, 0x0112
XMP = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-'
    b'syntax-ns#"><rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" '
    b'dc:creator="SecretAuthor"/></rdf:RDF></x:xmpmeta>'
)
ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def tagged_exif(orientation=None):
    exif = Image.Exif()
    exif[MAKE] = "SecretCam"
    exif.get_ifd(GPS_IFD)[2] = (48.0, 8.0, 0.0)  # GPSLatitude
    if orientation:
        exif[ORIENTATION] = orientation
    return exif


def make_tagged(path, pillow_format, size=(40, 20), orientation=None):
    image = Image.new("RGB", size, (10, 120, 200))
    image.paste((250, 10, 10), (0, 0, size[0] // 2, size[1] // 2))  # marks the top-left corner
    extra = {"comment": b"SecretComment", "quality": 95} if pillow_format == "JPEG" else {}
    if pillow_format in ("JPEG", "WEBP"):
        extra["xmp"] = XMP
    image.save(path, pillow_format, exif=tagged_exif(orientation), icc_profile=ICC, **extra)
    return path


def convert(registry, source, target, preserve, extra=()):
    request = ConversionRequest(
        source, target, source.parent, extra_sources=tuple(extra), preserve_metadata=preserve
    )
    result = runner.run_job(request, registry)
    assert result.status is JobStatus.SUCCEEDED, result.error
    return result


def markers(path):
    data = path.read_bytes()
    with Image.open(path) as image:
        exif = image.getexif()
        return {
            "make": exif.get(MAKE) == "SecretCam",
            "gps": bool(exif.get_ifd(GPS_IFD)),
            "orientation": exif.get(ORIENTATION),
            "icc": bool(image.info.get("icc_profile")),
            "xmp": b"SecretAuthor" in data,
            "comment": b"SecretComment" in data,
        }


SOURCES = {
    "JPEG": ("photo.jpg", "jpeg"),
    "PNG": ("photo.png", "png"),
    "WEBP": ("photo.webp", "webp"),
}
# Every (source kind, target) pair except same-format conversions, which are not offered.
ALL_PAIRS = [
    (kind, target)
    for kind, (_, own_id) in SOURCES.items()
    for target in ("jpeg", "png", "webp", "tiff", "bmp")
    if target != own_id
]
METADATA_PAIRS = [(kind, target) for kind, target in ALL_PAIRS if target != "bmp"]


@pytest.mark.parametrize(("kind", "target"), ALL_PAIRS)
def test_images_lose_all_markers_by_default(registry, tmp_path, kind, target):
    source = make_tagged(tmp_path / SOURCES[kind][0], kind)

    found = markers(convert(registry, source, target, preserve=False).output_path)

    assert found == {
        "make": False,
        "gps": False,
        "orientation": None,
        "icc": False,
        "xmp": False,
        "comment": False,
    }


@pytest.mark.parametrize(("kind", "target"), METADATA_PAIRS)
def test_images_keep_exif_gps_and_icc_when_preserving(registry, tmp_path, kind, target):
    source = make_tagged(tmp_path / SOURCES[kind][0], kind)

    result = convert(registry, source, target, preserve=True)

    found = markers(result.output_path)
    assert found["make"] and found["gps"] and found["icc"]
    # XMP is only written by Pillow's JPEG and WebP encoders (documented limitation).
    assert found["xmp"] == (kind in ("JPEG", "WEBP") and target in ("jpeg", "webp"))
    assert found["comment"] is False  # JPEG comments are not carried over (documented)


def test_tiff_with_gps_is_written_uncompressed_with_a_warning(registry, tmp_path):
    source = make_tagged(tmp_path / "photo.jpg", "JPEG")

    kept = convert(registry, source, "tiff", preserve=True)
    stripped = convert(registry, source, "tiff", preserve=False)

    with Image.open(kept.output_path) as image:
        assert image.info.get("compression") == "raw"
    with Image.open(stripped.output_path) as image:
        assert image.info.get("compression") == "tiff_lzw"
    assert any("uncompressed" in w for w in kept.warnings)
    assert not any("uncompressed" in w for w in stripped.warnings)


def test_bmp_cannot_hold_metadata_even_when_preserving(registry, tmp_path):
    source = make_tagged(tmp_path / "photo.jpg", "JPEG")
    found = markers(convert(registry, source, "bmp", preserve=True).output_path)
    assert not any(found[key] for key in ("make", "gps", "icc", "xmp"))


@pytest.mark.parametrize("preserve", [False, True])
def test_orientation_is_applied_exactly_once(registry, tmp_path, preserve):
    source = make_tagged(tmp_path / "phone.jpg", "JPEG", size=(40, 20), orientation=6)
    with Image.open(source) as original:
        upright = ImageOps.exif_transpose(original).convert("RGB")

    result = convert(registry, source, "png", preserve)

    with Image.open(result.output_path) as output:
        assert output.size == upright.size == (20, 40)  # rotated pixels...
        assert output.getexif().get(ORIENTATION) is None  # ...and no tag to rotate again
        top_left = output.convert("RGB").getpixel((2, 2))
        assert all(abs(a - b) < 40 for a, b in zip(top_left, upright.getpixel((2, 2)), strict=True))
    assert markers(result.output_path)["make"] is preserve


# --- images -> PDF --------------------------------------------------------------


def pdf_image_streams(path):
    pdf = pdfium.PdfDocument(str(path))
    try:
        result = []
        for page in pdf:
            image = next(page.get_objects(filter=(pdfium.raw.FPDF_PAGEOBJ_IMAGE,)))
            result.append(bytes(image.get_data()))
        return result
    finally:
        pdf.close()


def test_pdf_from_jpeg_has_no_metadata_by_default(registry, tmp_path):
    source = make_tagged(tmp_path / "photo.jpg", "JPEG")

    result = convert(registry, source, "pdf", preserve=False)

    data = result.output_path.read_bytes()
    markers_found = [
        marker
        for marker in (
            b"SecretCam",
            b"SecretAuthor",
            b"SecretComment",
            b"Exif\x00\x00",
            b"ICC_PROFILE",
        )  # noqa: E501
        if marker in data
    ]
    assert markers_found == []


def test_pdf_from_jpeg_embeds_original_bytes_when_preserving(registry, tmp_path):
    source = make_tagged(tmp_path / "photo.jpg", "JPEG", orientation=6)

    result = convert(registry, source, "pdf", preserve=True)

    assert pdf_image_streams(result.output_path) == [source.read_bytes()]
    pdf = pdfium.PdfDocument(str(result.output_path))
    try:
        width, height = pdf[0].get_size()
        rendered = pdf[0].render(scale=1).to_pil().convert("RGB")
    finally:
        pdf.close()
    assert height > width  # still upright: the embedded EXIF orientation is not re-applied
    assert rendered.getpixel((rendered.width - 3, 3))[0] > 200  # red corner after rotating


def test_pdf_from_png_warns_that_metadata_cannot_be_kept(registry, tmp_path):
    source = make_tagged(tmp_path / "photo.png", "PNG")

    preserved = convert(registry, source, "pdf", preserve=True)
    stripped = convert(registry, source, "pdf", preserve=False)

    assert any("cannot be stored in the PDF" in w for w in preserved.warnings)
    assert not any("cannot be stored" in w for w in stripped.warnings)
    assert b"SecretCam" not in stripped.output_path.read_bytes()


# --- FFmpeg arguments (the real effect is checked in test_media_integration) ------


@pytest.mark.parametrize("target", ["mp4", "mkv", "webm", "mp3", "m4a", "flac"])
def test_ffmpeg_strips_tags_and_chapters_by_default(tmp_path, target):
    converter = MediaConverter(fake_tools())
    info = MediaInfo(1.0, 1, 1, 0)

    stripped = converter.build_args(tmp_path / "a.mkv", tmp_path / "o.part", target, info)
    kept = converter.build_args(tmp_path / "a.mkv", tmp_path / "o.part", target, info, True)

    joined = " ".join(stripped)
    assert " ".join(STRIP_METADATA) in joined
    assert "-map_chapters -1" in joined
    assert "-map_metadata" not in kept and "-map_chapters" not in kept
    assert stripped[-3:-1] == kept[-3:-1]  # same container either way
