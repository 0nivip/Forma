"""Plain data objects passed between the application layers."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from fileconverter.core.errors import ConversionError, InvalidOptionError


@dataclass(frozen=True)
class IntOption:
    """An integer conversion option, e.g. JPEG quality."""

    name: str
    label: str
    default: int
    minimum: int
    maximum: int

    def validate(self, value: object) -> int:
        # bool is a subclass of int in Python, so reject it explicitly.
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidOptionError(f"Option '{self.name}' must be an integer, got {value!r}.")
        if not self.minimum <= value <= self.maximum:
            raise InvalidOptionError(
                f"Option '{self.name}' must be between {self.minimum} and {self.maximum}, "
                f"got {value}."
            )
        return value


@dataclass(frozen=True)
class Route:
    """One supported conversion: source format -> target format."""

    source: str  # format id
    target: str  # format id
    options: tuple[IntOption, ...] = ()
    # Known, predictable limitations shown to the user BEFORE converting.
    notes: tuple[str, ...] = ()

    def resolve_options(self, given: Mapping[str, object]) -> dict[str, int]:
        """Validate user-supplied options and fill in defaults."""
        known = {option.name: option for option in self.options}
        unknown = set(given) - set(known)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise InvalidOptionError(
                f"Unsupported option(s) for {self.source} -> {self.target}: {names}."
            )
        return {
            name: option.validate(given[name]) if name in given else option.default
            for name, option in known.items()
        }


@dataclass(frozen=True)
class ConversionRequest:
    source: Path
    target_format: str  # format id
    output_dir: Path
    options: Mapping[str, object] = field(default_factory=dict)


class JobStatus(Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ConversionResult:
    request: ConversionRequest
    status: JobStatus
    output_path: Path | None = None  # set only when status is SUCCEEDED
    error: ConversionError | None = None  # set only when status is FAILED
    warnings: tuple[str, ...] = ()  # things the user should know even on success
