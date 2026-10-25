"""AVIF, ICO and animated images (GIF, WebP, APNG, AVIF) through the real registry."""

import threading

import pytest
from PIL import Image

from fileconverter.app import build_registry
from fileconverter.cli import main
from fileconverter.conversion import runner
from fileconverter.conversion.backends import images
from fileconverter.conversion.backends.images import AVIF_QUALITY, avif_support
from fileconverter.core.errors import (
    InvalidInputError,
    InvalidOptionError,
    MissingDependencyError,
)
from fileconverter.core.models import ConversionRequest, JobStatus
from fileconverter.gui.main_window import MainWindow
from fileconverter.services.jobs import JobState
from helpers import make_image, temp_files
from test_metadata import MAKE, ORIENTATION, make_tagged

AVIF_OK = avif_support() == (True, True)
needs_avif = pytest.mark.skipif(not AVIF_OK, reason="this Pillow build cannot read and write AVIF")

COLORS = [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)]
DURATIONS = [100, 200, 300]


def convert(registry, source, target, cancel=None, on_progress=None, **options):
    request = ConversionRequest(source, target, source.parent, options)
    return runner.run_job(request, registry, cancel, on_progress)


def animation_frames(size=(20, 10)):
    """Three distinct frames, each with a transparent right-hand edge."""
    frames = []
    for index, color in enumerate(COLORS):
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        frame.paste(color, (index * 4, 0, index * 4 + 8, size[1]))
        frames.append(frame)
    return frames


def make_animation(path, pillow_format, loop=0, **extra):
    frames = animation_frames()
    args = {"save_all": True, "append_images": frames[1:], "duration": DURATIONS, **extra}
    if loop is not None:
        args["loop"] = loop
    if pillow_format == "GIF":
        args["disposal"] = 2
    if pillow_format == "WEBP":
        args.update(kmin=0, kmax=1)  # otherwise libwebp drops the alpha channel (see images.py)
    frames[0].save(path, pillow_format, **args)
    return path


def read_frames(path):
    """(format, durations, loop, alpha at the transparent edge per frame)."""
    with Image.open(path) as image:
        durations, edge_alpha = [], []
        for index in range(getattr(image, "n_frames", 1)):
            image.seek(index)
            image.load()  # durations are only known after loading each frame
            durations.append(image.info.get("duration"))
            edge_alpha.append(image.convert("RGBA").getpixel((19, 5))[3])
        return image.format, durations, image.info.get("loop"), edge_alpha


# --- AVIF -----------------------------------------------------------------------


def test_avif_is_detected_by_its_brand(registry, tmp_path):
    source = make_image(tmp_path / "a.png")
    if AVIF_OK:
        Image.open(source).save(tmp_path / "a.avif")
        assert registry.detect_format(tmp_path / "a.avif").id == "avif"
    fake_mif1 = tmp_path / "x.avif"  # generic HEIF brand that lists AVIF as compatible
    fake_mif1.write_bytes(b"\x00\x00\x00\x1cftypmif1\x00\x00\x00\x00mif1avifmiaf" + b"\x00" * 40)
    assert registry.detect_format(fake_mif1).id == "avif"


@needs_avif
def test_avif_round_trip_keeps_size_and_transparency(registry, tmp_path):
    image = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    image.paste((255, 0, 0, 255), (0, 0, 20, 20))
    source = tmp_path / "logo.png"
    image.save(source)

    to_avif = convert(registry, source, "avif")
    assert to_avif.status is JobStatus.SUCCEEDED, to_avif.error
    back = convert(registry, to_avif.output_path, "png")

    with Image.open(back.output_path) as result:
        assert result.size == (40, 20) and result.mode == "RGBA"
        assert result.getpixel((35, 10))[3] == 0  # transparent half stays transparent
        red, green, blue, alpha = result.getpixel((5, 10))
        assert alpha == 255 and red > 200 and green < 60 and blue < 60


@needs_avif
def test_avif_quality_option(registry, tmp_path):
    noisy = tmp_path / "noise.png"
    Image.effect_noise((64, 64), 80).convert("RGB").save(noisy)

    low = convert(registry, noisy, "avif", quality=10)
    high = convert(registry, noisy, "avif", quality=95)

    assert low.output_path.stat().st_size < high.output_path.stat().st_size
    assert AVIF_QUALITY.default == 75


@pytest.mark.parametrize("bad", [0, 101, "80"])
def test_invalid_avif_quality_is_rejected(registry, tmp_path, bad):
    if not AVIF_OK:
        pytest.skip("this Pillow build cannot read and write AVIF")
    source = make_image(tmp_path / "a.png")
    result = convert(registry, source, "avif", quality=bad)
    assert isinstance(result.error, InvalidOptionError)
    assert temp_files(tmp_path) == [] and not list(tmp_path.glob("*.avif"))


@needs_avif
@pytest.mark.parametrize("preserve", [False, True])
def test_avif_respects_privacy_setting_and_orientation(registry, tmp_path, preserve):
    source = make_tagged(tmp_path / "phone.jpg", "JPEG", size=(40, 20), orientation=6)
    request = ConversionRequest(source, "avif", tmp_path, preserve_metadata=preserve)

    result = runner.run_job(request, registry)

    data = result.output_path.read_bytes()
    with Image.open(result.output_path) as output:
        assert output.size == (20, 40)  # upright pixels...
        exif = output.getexif()
        assert exif.get(ORIENTATION) is None  # ...and no tag that would rotate them again
        assert (exif.get(MAKE) == "SecretCam") is preserve
    assert (b"SecretAuthor" in data) is preserve  # XMP


@needs_avif
@pytest.mark.parametrize("preserve", [False, True])
def test_avif_icc_profile_follows_privacy_setting(registry, tmp_path, preserve):
    source = make_tagged(tmp_path / "photo.png", "PNG")  # carries an sRGB ICC profile
    request = ConversionRequest(source, "avif", tmp_path, preserve_metadata=preserve)

    result = runner.run_job(request, registry)

    with Image.open(result.output_path) as output:
        assert bool(output.info.get("icc_profile")) is preserve


@pytest.mark.parametrize(
    ("support", "readable", "writable", "message"),
    [
        ((False, False), False, False, "no working AVIF support"),
        ((True, False), True, False, "cannot write AVIF"),
        ((False, True), False, True, "cannot read AVIF"),
    ],
)
def test_missing_avif_codecs_are_explained(
    monkeypatch, tmp_path, support, readable, writable, message
):  # noqa: E501
    monkeypatch.setattr(images, "avif_support", lambda: support)
    registry = build_registry(None)

    assert bool(registry.targets_for("avif")) is readable
    assert ("avif" in {f.id for f in registry.targets_for("png")}) is writable
    assert message in registry.unavailable_reason("avif")
    if not writable:
        with pytest.raises(MissingDependencyError, match=message):
            registry.find_route("png", "avif")
    if not readable:
        assert "pdf" not in {f.id for f in registry.targets_for("avif")}


def test_gui_explains_unreadable_avif(monkeypatch, qtbot, tmp_path):
    monkeypatch.setattr(images, "avif_support", lambda: (False, False))
    window = MainWindow(build_registry(None))
    qtbot.addWidget(window)
    fake = tmp_path / "photo.avif"
    fake.write_bytes(b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + b"\x00" * 40)

    window.add_files([fake])

    job = window.model.job_at(0)
    assert job.state is JobState.UNSUPPORTED
    assert "AVIF" in job.problem and "Pillow" in job.problem


# --- ICO ------------------------------------------------------------------------


def ico_sizes(path):
    with Image.open(path) as icon:
        return sorted(icon.ico.sizes()), icon.size, icon.mode


def test_wide_image_becomes_square_icon_without_distortion(registry, tmp_path):
    image = Image.new("RGBA", (600, 300), (0, 0, 0, 0))
    image.paste((255, 0, 0, 255), (0, 0, 600, 300))  # fully red, no transparency
    source = tmp_path / "banner.png"
    image.save(source)

    result = convert(registry, source, "ico")

    sizes, largest, mode = ico_sizes(result.output_path)
    assert sizes == [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    assert largest == (256, 256) and mode == "RGBA"
    with Image.open(result.output_path) as icon:
        assert icon.getpixel((128, 10))[3] == 0  # padding above the picture is transparent
        assert icon.getpixel((128, 128))[:3] == (255, 0, 0)  # the picture in the middle
    text = " ".join(result.warnings)
    assert "centred on a transparent 600 x 600 square" in text and "not stretched" in text
    assert "reduced from 600 x 600 to 256 x 256" in text


def test_small_square_icon_keeps_its_own_size(registry, tmp_path):
    source = make_image(tmp_path / "dot.png", mode="RGBA", size=(20, 20), color=(0, 0, 255, 128))

    result = convert(registry, source, "ico")

    sizes, largest, _ = ico_sizes(result.output_path)
    assert sizes == [(16, 16), (20, 20)] and largest == (20, 20)
    assert result.warnings == ()
    with Image.open(result.output_path) as icon:
        assert icon.getpixel((10, 10)) == (0, 0, 255, 128)  # semi-transparency kept


def test_icon_to_png_uses_the_largest_size(registry, tmp_path):
    source = tmp_path / "app.ico"
    Image.new("RGBA", (64, 64), (0, 200, 0, 100)).save(source, sizes=[(16, 16), (64, 64)])

    result = convert(registry, source, "png")

    with Image.open(result.output_path) as png:
        assert png.size == (64, 64) and png.mode == "RGBA"
        assert png.getpixel((5, 5)) == (0, 200, 0, 100)


def test_jpeg_to_icon(registry, tmp_path):
    source = make_image(tmp_path / "photo.jpg", "JPEG", size=(48, 48))
    result = convert(registry, source, "ico")
    assert ico_sizes(result.output_path)[0] == [(16, 16), (24, 24), (32, 32), (48, 48)]


def test_damaged_icon(registry, tmp_path):
    broken = tmp_path / "broken.ico"
    broken.write_bytes(b"\x00\x00\x01\x00\x01\x00" + b"\xff" * 30)
    result = convert(registry, broken, "png")
    assert isinstance(result.error, InvalidInputError)
    assert temp_files(tmp_path) == []


# --- animation --------------------------------------------------------------------

ANIMATED_TARGETS = ["webp", "png"] + (["avif"] if AVIF_OK else [])


@pytest.mark.parametrize("target", ANIMATED_TARGETS)
def test_animated_gif_keeps_frames_timing_and_transparency(registry, tmp_path, target):
    source = make_animation(tmp_path / "spin.gif", "GIF", loop=0)

    result = convert(registry, source, target)

    assert result.status is JobStatus.SUCCEEDED, result.error
    fmt, durations, loop, edge_alpha = read_frames(result.output_path)
    assert durations == DURATIONS
    assert edge_alpha == [0, 0, 0]
    if target != "avif":  # Pillow cannot read the AVIF loop count back
        assert loop == 0
    assert not any("animation was not kept" in w for w in result.warnings)


@pytest.mark.parametrize(
    ("gif_loop", "expected_plays"),
    [(0, 0), (2, 3), (None, 1)],  # GIF counts repeats; "no loop" means play once
)
def test_gif_loop_count_becomes_total_plays(registry, tmp_path, gif_loop, expected_plays):
    source = make_animation(tmp_path / "spin.gif", "GIF", loop=gif_loop)

    for target in ("webp", "png"):
        result = convert(registry, source, target)
        assert read_frames(result.output_path)[2] == expected_plays, target


@pytest.mark.parametrize(("plays", "gif_loop"), [(0, 0), (3, 2), (1, None)])
def test_webp_animation_to_gif_converts_the_loop_count_back(registry, tmp_path, plays, gif_loop):
    source = make_animation(tmp_path / "spin.webp", "WEBP", loop=plays, lossless=True)

    result = convert(registry, source, "gif")

    fmt, durations, loop, edge_alpha = read_frames(result.output_path)
    assert (fmt, durations, loop, edge_alpha) == ("GIF", DURATIONS, gif_loop, [0, 0, 0])


def test_apng_to_gif(registry, tmp_path):
    source = make_animation(tmp_path / "spin.png", "PNG", loop=0)
    result = convert(registry, source, "gif")
    assert read_frames(result.output_path)[:2] == ("GIF", DURATIONS)


@needs_avif
def test_avif_animation_source_and_target_warnings(registry, tmp_path):
    source = make_animation(tmp_path / "spin.gif", "GIF", loop=2)

    to_avif = convert(registry, source, "avif")
    back = convert(registry, to_avif.output_path, "gif")

    assert any("cannot be verified" in w for w in to_avif.warnings)  # finite loop in AVIF
    assert any("loops forever" in w for w in back.warnings)  # AVIF loop count unknown
    assert read_frames(back.output_path)[:2] == ("GIF", DURATIONS)


@pytest.mark.parametrize("target", ["jpeg", "bmp", "tiff", "ico"])
def test_static_targets_get_first_frame_and_a_clear_warning(registry, tmp_path, target):
    source = make_animation(tmp_path / "spin.gif", "GIF")

    result = convert(registry, source, target)

    assert result.status is JobStatus.SUCCEEDED, result.error
    with Image.open(result.output_path) as output:
        assert getattr(output, "n_frames", 1) == 1
    assert any("animation was not kept" in w and "first of 3 frames" in w for w in result.warnings)


def test_gif_reports_lost_semi_transparency(registry, tmp_path):
    frames = [Image.new("RGBA", (8, 8), (255, 0, 0, alpha)) for alpha in (128, 255)]
    source = tmp_path / "soft.png"
    frames[0].save(source, "PNG", save_all=True, append_images=frames[1:], duration=[50, 50])

    result = convert(registry, source, "gif")

    assert any("Semi-transparent pixels" in w for w in result.warnings)


def test_static_transparency_survives_gif(registry, tmp_path):
    image = Image.new("RGBA", (20, 10), (0, 0, 0, 0))
    image.paste((0, 128, 0, 255), (0, 0, 10, 10))
    source = tmp_path / "logo.png"
    image.save(source)

    to_gif = convert(registry, source, "gif")
    back = convert(registry, to_gif.output_path, "png")

    with Image.open(back.output_path) as result:
        rgba = result.convert("RGBA")
        assert rgba.getpixel((15, 5))[3] == 0 and rgba.getpixel((5, 5))[3] == 255


def test_too_large_animation_is_refused(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(images, "MAX_ANIMATION_PIXELS", 500)  # 3 frames of 20 x 10 = 600
    source = make_animation(tmp_path / "spin.gif", "GIF")

    result = convert(registry, source, "webp")

    assert isinstance(result.error, InvalidInputError)
    assert "too large" in str(result.error)
    assert not list(tmp_path.glob("*.webp")) and temp_files(tmp_path) == []


def test_cancel_while_reading_frames(registry, tmp_path):
    source = make_animation(tmp_path / "spin.gif", "GIF")
    cancel = threading.Event()

    # Progress None is reported just before the converter starts.
    result = convert(registry, source, "webp", cancel, lambda fraction: cancel.set())

    assert result.status is JobStatus.CANCELLED
    assert not list(tmp_path.glob("*.webp")) and temp_files(tmp_path) == []


def test_truncated_gif(registry, tmp_path):
    source = make_animation(tmp_path / "spin.gif", "GIF")
    source.write_bytes(source.read_bytes()[:60])

    result = convert(registry, source, "webp")

    assert result.status is JobStatus.FAILED
    assert temp_files(tmp_path) == [] and not list(tmp_path.glob("*.webp"))


@pytest.mark.parametrize("preserve", [False, True])
def test_animation_never_carries_source_metadata(registry, tmp_path, preserve):
    exif = Image.Exif()
    exif[MAKE] = "SecretCam"
    source = make_animation(tmp_path / "spin.webp", "WEBP", exif=exif.tobytes())
    request = ConversionRequest(source, "png", tmp_path, preserve_metadata=preserve)

    result = runner.run_job(request, registry)

    assert b"SecretCam" not in result.output_path.read_bytes()
    assert any("not kept when converting animations" in w for w in result.warnings) is preserve


def test_route_notes_describe_the_animation_policy(registry):
    kept = registry.find_route("gif", "webp")[0].notes
    lost = registry.find_route("gif", "jpeg")[0].notes
    assert any("keep every frame" in note for note in kept)
    assert any("animation is not kept" in note for note in lost)


# --- GUI and CLI ----------------------------------------------------------------------


def test_gui_offers_new_formats_and_avif_quality(registry, qtbot, tmp_path):
    window = MainWindow(registry)
    qtbot.addWidget(window)
    window.add_files([make_animation(tmp_path / "spin.gif", "GIF")])
    window.table.selectRow(0)
    combo = window.target_combo
    targets = {combo.itemData(i) for i in range(combo.count())}

    assert {"png", "webp", "ico", "jpeg"} <= targets and "gif" not in targets
    assert ("avif" in targets) is AVIF_OK
    if AVIF_OK:
        combo.activated.emit(combo.findData("avif"))
        assert window.option_widgets["quality"].value() == AVIF_QUALITY.default
    combo.activated.emit(combo.findData("png"))
    assert window.option_widgets == {}  # PNG has no quality setting
    combo.activated.emit(combo.findData("webp"))
    assert window.option_widgets["quality"].value() == 90


def test_cli_quality_help_matches_the_shared_options(capsys):
    with pytest.raises(SystemExit):
        main(["convert", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "JPEG 1-95 (default 90)" in help_text
    assert "WEBP 1-100 (default 90)" in help_text
    assert "AVIF 1-100 (default 75)" in help_text


def test_cli_lists_and_converts_new_formats(tmp_path, capsys):
    assert main(["formats"]) == 0
    out = capsys.readouterr().out
    assert "gif    (.gif) ->" in out and "ico    (.ico) ->" in out
    source = make_animation(tmp_path / "spin.gif", "GIF")

    assert main(["convert", str(source), "--to", "webp", "--quality", "80"]) == 0
    assert read_frames(tmp_path / "spin.webp")[1] == DURATIONS
    assert main(["convert", str(source), "--to", "png", "--quality", "80"]) == 1  # no such option
