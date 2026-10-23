"""Central catalogue of formats and of which converter handles each conversion."""

import zipfile
from collections.abc import Iterable
from pathlib import Path

from fileconverter.core.converter import Converter
from fileconverter.core.errors import (
    InvalidInputError,
    MissingDependencyError,
    UnsupportedConversionError,
    UnsupportedFormatError,
)
from fileconverter.core.formats import HEADER_SIZE, Format
from fileconverter.core.models import Route


class FormatRegistry:
    def __init__(self) -> None:
        self._formats: dict[str, Format] = {}
        self._routes: dict[tuple[str, str], tuple[Route, Converter]] = {}
        # format id -> why conversions to/from it are unavailable (e.g. FFmpeg missing)
        self._unavailable: dict[str, str] = {}

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

    def note_unavailable(self, format_id: str, reason: str) -> None:
        """Record an actionable explanation for a format that has no (or fewer) routes."""
        if format_id not in self._formats:
            raise ValueError(f"Unknown format '{format_id}'.")
        self._unavailable[format_id] = reason

    # --- queries ------------------------------------------------------------

    def unavailable_reason(self, format_id: str) -> str | None:
        return self._unavailable.get(format_id)

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
            pass
        reason = self._unavailable.get(target_id) or self._unavailable.get(source_id)
        if reason and source_id != target_id:
            raise MissingDependencyError(reason)
        raise UnsupportedConversionError(
            f"Conversion from {source_id} to {target_id} is not supported."
        )

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
        candidates = sorted(
            ((fmt.match_strength(header), fmt) for fmt in self._formats.values()),
            key=lambda pair: pair[0],
            reverse=True,  # stable: equally strong matches keep registration order
        )
        for strength, fmt in candidates:
            if strength == 0:
                break
            if fmt.zip_member is None or _zip_contains(path, fmt.zip_member):
                return fmt
        raise UnsupportedFormatError(f"'{path.name}' is not in a supported format.")


def _zip_contains(path: Path, member: str) -> bool:
    """Whether a ZIP archive lists `member`. Reads only the archive's directory."""
    try:
        with zipfile.ZipFile(path) as archive:
            return member in archive.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return False
