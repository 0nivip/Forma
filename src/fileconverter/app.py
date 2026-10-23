"""Composition root: the one place that wires formats and backends together."""

from fileconverter.conversion.backends.documents import DocumentConverter
from fileconverter.conversion.backends.images import ImageConverter
from fileconverter.conversion.backends.media import register_media
from fileconverter.conversion.backends.pdf import PdfConverter
from fileconverter.conversion.backends.spreadsheets import SpreadsheetConverter
from fileconverter.core.formats import BUILTIN_FORMATS
from fileconverter.core.registry import FormatRegistry
from fileconverter.infrastructure.ffmpeg import FFmpegTools


def build_registry(ffmpeg: FFmpegTools | None) -> FormatRegistry:
    """Pass the result of infrastructure.ffmpeg.find_ffmpeg(); None disables audio/video."""
    registry = FormatRegistry()
    registry.register_formats(BUILTIN_FORMATS)
    registry.register_converter(ImageConverter())
    registry.register_converter(DocumentConverter())
    registry.register_converter(SpreadsheetConverter())
    registry.register_converter(PdfConverter())
    register_media(registry, ffmpeg)
    return registry
