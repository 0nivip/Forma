import pytest

from fileconverter.core.errors import (
    InvalidInputError,
    InvalidOptionError,
    UnsupportedConversionError,
    UnsupportedFormatError,
)
from fileconverter.core.formats import BUILTIN_FORMATS
from fileconverter.core.models import IntOption, Route
from fileconverter.core.registry import FormatRegistry
from helpers import make_image

IMAGE_IDS = ["png", "jpeg", "webp", "bmp", "tiff"]


def test_every_image_format_converts_to_every_other(registry):
    for source in IMAGE_IDS:
        targets = {fmt.id for fmt in registry.targets_for(source)}
        assert targets == set(IMAGE_IDS) - {source}


def test_same_format_conversion_is_not_offered(registry):
    with pytest.raises(UnsupportedConversionError):
        registry.find_route("png", "png")


def test_unknown_target_format_is_rejected(registry):
    with pytest.raises(UnsupportedFormatError):
        registry.get_format("docx")


def test_detects_format_from_content_not_extension(registry, tmp_path):
    disguised = make_image(tmp_path / "actually_png.jpg", "PNG")
    assert registry.detect_format(disguised).id == "png"


@pytest.mark.parametrize(
    ("pillow_format", "expected"),
    [("PNG", "png"), ("JPEG", "jpeg"), ("WEBP", "webp"), ("BMP", "bmp"), ("TIFF", "tiff")],
)
def test_detects_each_supported_format(registry, tmp_path, pillow_format, expected):
    path = make_image(tmp_path / "image.bin", pillow_format)
    assert registry.detect_format(path).id == expected


def test_empty_file_is_invalid_input(registry, tmp_path):
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(InvalidInputError):
        registry.detect_format(empty)


def test_text_file_is_unsupported_format(registry, tmp_path):
    text = tmp_path / "notes.png"
    text.write_text("definitely not an image")
    with pytest.raises(UnsupportedFormatError):
        registry.detect_format(text)


class _Backend:
    def __init__(self, routes):
        self._routes = routes

    def routes(self):
        return self._routes

    def convert(self, *args):
        raise NotImplementedError


def test_registering_a_route_twice_fails():
    registry = FormatRegistry()
    registry.register_formats(BUILTIN_FORMATS)
    registry.register_converter(_Backend([Route("png", "jpeg")]))
    with pytest.raises(ValueError, match="already registered"):
        registry.register_converter(_Backend([Route("png", "jpeg")]))


def test_route_with_unknown_format_fails():
    registry = FormatRegistry()
    registry.register_formats(BUILTIN_FORMATS)
    with pytest.raises(ValueError, match="unknown format"):
        registry.register_converter(_Backend([Route("png", "mp3")]))


QUALITY = IntOption("quality", "Quality", default=90, minimum=1, maximum=95)
ROUTE = Route("png", "jpeg", options=(QUALITY,))


def test_options_default_is_used_when_not_given():
    assert ROUTE.resolve_options({}) == {"quality": 90}


def test_options_valid_value_is_kept():
    assert ROUTE.resolve_options({"quality": 50}) == {"quality": 50}


@pytest.mark.parametrize("bad", [0, 96, "80", True, 8.5])
def test_options_invalid_value_is_rejected(bad):
    with pytest.raises(InvalidOptionError):
        ROUTE.resolve_options({"quality": bad})


def test_options_unknown_name_is_rejected():
    with pytest.raises(InvalidOptionError, match="compression"):
        ROUTE.resolve_options({"compression": 5})
