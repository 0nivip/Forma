"""Central catalogue of formats and of which converter handles each conversion."""

from collections.abc import Iterable
from pathlib import Path

from fileconverter.core.converter import Converter
from fileconverter.core.errors import (
    InvalidInputError,
    UnsupportedConversionError,
    UnsupportedFormatError,
)
from fileconverter.core.formats import HEADER_SIZE, Format
from fileconverter.core.models import Route


class FormatRegistry:
    def __init__(self) -> None:
        self._formats: dict[str, Format] = {}
        self._routes: dict[tuple[str, str], tuple[Route, Converter]] = {}

    # --- registration -------------------------------------------------------

    def register_formats(self, formats: Iterable[Format]) -> None:
        for fmt in formats:
            if fmt.id in self._formats:
                raise ValueError(f"Format '{fmt.id}' is already registered.")
            self._formats[fmt.id] = fmt

    def register_converter(self, converter: Converter) -> None:
        for route in converter.routes():
            for format_id in (route.source, route.target):
                if format_id not in self._formats:
                    raise ValueError(f"Route uses unknown format '{format_id}'.")
            key = (route.source, route.target)
            if key in self._routes:
                raise ValueError(f"Route {route.source} -> {route.target} is already registered.")
            self._routes[key] = (route, converter)

    # --- queries ------------------------------------------------------------

    def get_format(self, format_id: str) -> Format:
        try:
            return self._formats[format_id]
        except KeyError:
            raise UnsupportedFormatError(f"Unknown format '{format_id}'.") from None

    def formats(self) -> list[Format]:
        return list(self._formats.values())

    def targets_for(self, source_id: str) -> list[Format]:
        """Formats that a file of `source_id` can be converted into."""
        return [self._formats[target] for (source, target) in self._routes if source == source_id]

    def find_route(self, source_id: str, target_id: str) -> tuple[Route, Converter]:
        try:
            return self._routes[(source_id, target_id)]
        except KeyError:
            raise UnsupportedConversionError(
                f"Conversion from {source_id} to {target_id} is not supported."
            ) from None

    def detect_format(self, path: Path) -> Format:
        """Identify a file's format from its content (magic bytes), not its extension.

        The extension can lie (e.g. a PNG saved as .jpg); the header usually does not.
        """
        try:
            with path.open("rb") as file:
                header = file.read(HEADER_SIZE)
        except OSError as exc:
            raise InvalidInputError(f"Cannot read '{path}': {exc.strerror or exc}") from exc

        if not header:
            raise InvalidInputError(f"'{path.name}' is empty.")
        for fmt in self._formats.values():
            if fmt.matches_header(header):
                return fmt
        raise UnsupportedFormatError(f"'{path.name}' is not in a supported format.")
