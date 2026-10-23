"""End-to-end image conversions through the real registry, runner and Pillow backend."""

import itertools

import pytest
from PIL import Image

from fileconverter.conversion.runner import run_job
from fileconverter.core.errors import InvalidInputError, InvalidOptionError
from fileconverter.core.models import ConversionRequest, JobStatus
from helpers import make_image, temp_files, write_png16, write_tiff16

PILLOW_NAMES = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP", "bmp": "BMP", "tiff": "TIFF"}
EXTENSIONS = {"png": ".png", "jpeg": ".jpg", "webp": ".webp", "bmp": ".bmp", "tiff": ".tif"}
TRANSPARENT = (0, 0, 0, 0)


def convert(registry, source, target, out_dir=None, **options):
    request = ConversionRequest(source, target, out_dir or source.parent, options)
    return run_job(request, registry)


def half_transparent_image(path, pillow_format="PNG"):
    """Left half opaque red, right half fully transparent."""
    image = Image.new("RGBA", (16, 8), TRANSPARENT)
    image.paste((255, 0, 0, 255), (0, 0, 8, 8))
    image.save(path, format=pillow_format, lossless=True)
    return path


@pytest.mark.parametrize(("source_id", "target_id"), list(itertools.permutations(PILLOW_NAMES, 2)))
def test_converts_every_supported_pair(registry, tmp_path, source_id, target_id):
    source = make_image(tmp_path / f"sample{EXTENSIONS[source_id]}", PILLOW_NAMES[source_id])

    result = convert(registry, source, target_id)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert result.output_path.suffix == EXTENSIONS[target_id]
    with Image.open(result.output_path) as output:
        assert output.format == PILLOW_NAMES[target_id]
        assert output.size == (16, 8)
    assert temp_files(tmp_path) == []


@pytest.mark.parametrize("target", ["jpeg", "bmp"])
def test_transparency_is_flattened_to_white_with_warning(registry, tmp_path, target):
    source = half_transparent_image(tmp_path / "logo.png")

    result = convert(registry, source, target)

    assert result.status is JobStatus.SUCCEEDED
    assert any("transparen" in w.lower() for w in result.warnings)
    with Image.open(result.output_path) as output:
        rgb = output.convert("RGB")
        assert all(channel > 240 for channel in rgb.getpixel((12, 4)))  # was transparent
        red, green, _ = rgb.getpixel((2, 4))  # was opaque red
        assert red > 200 and green < 50


def test_palette_transparency_is_also_flattened(registry, tmp_path):
    source = make_image(tmp_path / "icon.png", "PNG", mode="P", color=0, transparency=0)

    result = convert(registry, source, "jpeg")

    assert any("transparen" in w.lower() for w in result.warnings)
    with Image.open(result.output_path) as output:
        assert all(channel > 240 for channel in output.convert("RGB").getpixel((0, 0)))


def test_fully_opaque_alpha_gives_no_transparency_warning(registry, tmp_path):
    source = make_image(tmp_path / "opaque.png", "PNG", mode="RGBA", color=(10, 20, 30, 255))
    result = convert(registry, source, "jpeg")
    assert result.status is JobStatus.SUCCEEDED
    assert not any("transparen" in w.lower() for w in result.warnings)


@pytest.mark.parametrize(
    ("source_name", "pillow_format", "target"),
    [("logo.png", "PNG", "webp"), ("logo.png", "PNG", "tiff"), ("logo.webp", "WEBP", "png")],
)
def test_transparency_is_kept_where_supported(
    registry, tmp_path, source_name, pillow_format, target
):
    source = half_transparent_image(tmp_path / source_name, pillow_format)

    result = convert(registry, source, target)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert not any("transparen" in w.lower() for w in result.warnings)
    with Image.open(result.output_path) as output:
        assert output.convert("RGBA").getpixel((12, 4))[3] == 0


def test_quality_option_changes_jpeg_size(registry, tmp_path):
    noisy = tmp_path / "noise.png"
    Image.effect_noise((64, 64), 80).convert("RGB").save(noisy)

    low = convert(registry, noisy, "jpeg", quality=10)
    high = convert(registry, noisy, "jpeg", quality=95)

    assert low.output_path.stat().st_size < high.output_path.stat().st_size


def test_out_of_range_quality_is_rejected(registry, tmp_path):
    source = make_image(tmp_path / "a.png")
    result = convert(registry, source, "jpeg", quality=500)
    assert isinstance(result.error, InvalidOptionError)
    assert list(tmp_path.iterdir()) == [source]


def test_option_not_supported_by_target_is_rejected(registry, tmp_path):
    source = make_image(tmp_path / "a.jpg", "JPEG")
    result = convert(registry, source, "png", quality=80)
    assert isinstance(result.error, InvalidOptionError)


def test_corrupted_image_fails_cleanly(registry, tmp_path):
    good = make_image(tmp_path / "good.png", size=(200, 200))
    broken = tmp_path / "broken.png"
    data = good.read_bytes()
    broken.write_bytes(data[: len(data) // 2])  # valid header, truncated body

    result = convert(registry, broken, "jpeg")

    assert result.status is JobStatus.FAILED
    assert isinstance(result.error, InvalidInputError)
    assert temp_files(tmp_path) == []


def test_png_signature_with_garbage_fails_cleanly(registry, tmp_path):
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 20)

    result = convert(registry, fake, "jpeg")

    assert isinstance(result.error, InvalidInputError)
    assert temp_files(tmp_path) == []


def test_source_with_target_extension_is_not_overwritten(registry, tmp_path):
    # A PNG mis-named "photo.jpg", converted to JPEG in its own folder: the natural
    # output name is the source file itself.
    source = make_image(tmp_path / "photo.jpg", "PNG")
    original_bytes = source.read_bytes()

    result = convert(registry, source, "jpeg")

    assert result.status is JobStatus.SUCCEEDED
    assert result.output_path == tmp_path / "photo (1).jpg"
    assert source.read_bytes() == original_bytes
    assert any("extension" in w for w in result.warnings)


def test_existing_output_is_kept_and_new_name_used(registry, tmp_path):
    source = make_image(tmp_path / "photo.png")
    existing = tmp_path / "photo.webp"
    existing.write_bytes(b"do not touch")

    result = convert(registry, source, "webp")

    assert result.output_path == tmp_path / "photo (1).webp"
    assert existing.read_bytes() == b"do not touch"


def test_exif_orientation_is_applied(registry, tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90 degrees clockwise when displaying
    source = make_image(tmp_path / "phone.jpg", "JPEG", size=(40, 20), exif=exif)

    result = convert(registry, source, "png")

    with Image.open(result.output_path) as output:
        assert output.size == (20, 40)


def test_multipage_tiff_converts_first_page_with_warning(registry, tmp_path):
    source = tmp_path / "scan.tif"
    first = Image.new("RGB", (16, 8), (255, 0, 0))
    second = Image.new("RGB", (16, 8), (0, 0, 255))
    first.save(source, format="TIFF", save_all=True, append_images=[second])

    result = convert(registry, source, "png")

    assert any("2 frames" in w for w in result.warnings)
    with Image.open(result.output_path) as output:
        assert output.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.parametrize(
    ("source_format", "target"),
    [("JPEG", "png"), ("JPEG", "webp"), ("JPEG", "tiff"), ("PNG", "jpeg")],
)
def test_exif_metadata_is_not_copied(registry, tmp_path, source_format, target):
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # camera maker; stands in for GPS and other private data
    source = make_image(tmp_path / "photo.bin", source_format, exif=exif)

    result = convert(registry, source, target)

    with Image.open(result.output_path) as output:
        assert 0x010F not in output.getexif()


CYAN_CMYK = (255, 0, 0, 0)
FAKE_CMYK_PROFILE = b"not-a-real-icc-profile-but-stored-verbatim"


@pytest.mark.parametrize("target", ["png", "webp", "bmp"])
def test_cmyk_to_rgb_only_target_converts_with_warning(registry, tmp_path, target):
    source = make_image(tmp_path / "print.tif", "TIFF", mode="CMYK", color=CYAN_CMYK)

    result = convert(registry, source, target)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert any("CMYK" in w for w in result.warnings)
    with Image.open(result.output_path) as output:
        assert output.mode == "RGB"
        red, green, blue = output.getpixel((0, 0))
        assert red < 10 and green > 245 and blue > 245  # cyan


@pytest.mark.parametrize(("source_format", "target"), [("TIFF", "jpeg"), ("JPEG", "tiff")])
def test_cmyk_is_kept_where_supported(registry, tmp_path, source_format, target):
    source = make_image(tmp_path / "print.bin", source_format, mode="CMYK", color=CYAN_CMYK)

    result = convert(registry, source, target)

    assert not any("CMYK" in w for w in result.warnings)
    with Image.open(result.output_path) as output:
        assert output.mode == "CMYK"


def test_cmyk_profile_is_dropped_when_converting_to_rgb(registry, tmp_path):
    source = make_image(
        tmp_path / "print.jpg", "JPEG", mode="CMYK", color=CYAN_CMYK, icc_profile=FAKE_CMYK_PROFILE
    )

    to_png = convert(registry, source, "png")
    to_tiff = convert(registry, source, "tiff")

    with Image.open(to_png.output_path) as output:
        assert "icc_profile" not in output.info  # a CMYK profile would be wrong for RGB
    with Image.open(to_tiff.output_path) as output:
        assert output.info.get("icc_profile") == FAKE_CMYK_PROFILE


MID = 32768  # middle of the 16-bit range; must become ~128, never 255 (clipping)


@pytest.mark.parametrize(
    ("writer", "name", "pixel", "target"),
    [
        (write_png16, "deep.png", (MID, 65535, 0), "jpeg"),
        (write_png16, "deep.png", (MID, 65535, 0), "tiff"),
        (write_png16, "deep.png", (MID, 65535, 0, MID), "webp"),
        (write_tiff16, "deep.tif", (MID, 65535, 0), "png"),
        (write_tiff16, "deep.tif", (MID, 65535, 0, MID), "png"),
    ],
)
def test_16bit_color_is_scaled_to_8bit_with_warning(
    registry, tmp_path, writer, name, pixel, target
):
    source = writer(tmp_path / name, pixel)

    result = convert(registry, source, target)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert any("16-bit color" in w for w in result.warnings)
    with Image.open(result.output_path) as output:
        values = output.getpixel((0, 0))
        assert abs(values[0] - 128) <= 3 and values[1] >= 250 and values[2] <= 5
        if len(pixel) == 4:
            assert abs(values[3] - 128) <= 3


def test_16bit_rgba_to_jpeg_warns_about_depth_and_transparency(registry, tmp_path):
    source = write_png16(tmp_path / "deep.png", (MID, 65535, 0, MID))

    result = convert(registry, source, "jpeg")

    assert any("16-bit color" in w for w in result.warnings)
    assert any("transparen" in w.lower() for w in result.warnings)


def test_8bit_image_gives_no_bit_depth_warning(registry, tmp_path):
    result = convert(registry, make_image(tmp_path / "a.png"), "tiff")
    assert not any("16-bit" in w for w in result.warnings)


@pytest.mark.parametrize(("extension", "target"), [(".tif", "png"), (".png", "tiff")])
def test_16bit_grayscale_is_preserved_where_supported(registry, tmp_path, extension, target):
    source = tmp_path / f"depth{extension}"
    Image.new("I;16", (8, 8), MID).save(source)

    result = convert(registry, source, target)

    assert not any("16-bit" in w for w in result.warnings)
    with Image.open(result.output_path) as output:
        assert output.mode == "I;16"
        assert output.getpixel((0, 0)) == MID


def test_16bit_grayscale_is_scaled_not_clipped(registry, tmp_path):
    source = tmp_path / "depth.png"
    Image.new("I;16", (8, 8), 32768).save(source)  # mid-gray in 16-bit

    result = convert(registry, source, "jpeg")

    assert any("16-bit" in w for w in result.warnings)
    with Image.open(result.output_path) as output:
        assert 120 <= output.convert("L").getpixel((0, 0)) <= 136  # mid-gray, not white
