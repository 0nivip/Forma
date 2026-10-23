"""Composition root: the one place that wires formats and backends together."""

from fileconverter.conversion.backends.images import ImageConverter
from fileconverter.core.formats import BUILTIN_FORMATS
from fileconverter.core.registry import FormatRegistry


def build_registry() -> FormatRegistry:
    registry = FormatRegistry()
    registry.register_formats(BUILTIN_FORMATS)
    registry.register_converter(ImageConverter())
    return registry
