"""Plain data objects passed between the application layers."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from fileconverter.core.errors import ConversionError, InvalidOptionError

type OptionValue = int | str


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
class TextOption:
    """A short free-text option, e.g. a page range. Its meaning is checked by the backend."""

    name: str
    label: str
    default: str = ""
    placeholder: str = ""  # shown when empty, e.g. "All pages"
    max_length: int = 200

    def validate(self, value: object) -> str:
        if not isinstance(value, str):
            raise InvalidOptionError(f"Option '{self.name}' must be text, got {value!r}.")
        if len(value) > self.max_length:
            raise InvalidOptionError(f"Option '{self.name}' is too long.")
        return value.strip()


@dataclass(frozen=True)
class ChoiceOption:
    """One value out of a fixed list, e.g. the CSV delimiter."""

    name: str
    label: str
    choices: tuple[tuple[str, str], ...]  # (value, human-readable label)
    default: str
    help: str = ""

    def validate(self, value: object) -> str:
        values = [choice for choice, _ in self.choices]
        if value not in values:
            raise InvalidOptionError(
                f"Option '{self.name}' must be one of {', '.join(values)}, got {value!r}."
            )
        return value


type Option = IntOption | TextOption | ChoiceOption


@dataclass(frozen=True)
class Route:
    """One supported conversion: source format -> target format."""

    source: str  # format id
    target: str  # format id
    options: tuple[Option, ...] = ()
    # Known, predictable limitations shown to the user BEFORE converting.
    notes: tuple[str, ...] = ()
    # The converter writes several files (e.g. one image per PDF page) into a folder.
    multi_output: bool = False
    # Several source files may be combined into one output (e.g. images into one PDF).
    multi_input: bool = False

    def resolve_options(self, given: Mapping[str, object]) -> dict[str, OptionValue]:
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
    # Further sources combined with `source` into one output (multi_input routes only).
    extra_sources: tuple[Path, ...] = ()
    # False: backends try to strip transferable metadata (EXIF, GPS, tags, chapters, ...).
    preserve_metadata: bool = False

    @property
    def sources(self) -> tuple[Path, ...]:
        return (self.source, *self.extra_sources)


class JobStatus(Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ConversionResult:
    request: ConversionRequest
    status: JobStatus
    # Every file the job created, in order; set only when status is SUCCEEDED.
    output_paths: tuple[Path, ...] = ()
    error: ConversionError | None = None  # set only when status is FAILED
    warnings: tuple[str, ...] = ()  # things the user should know even on success

    @property
    def output_path(self) -> Path | None:
        """The (first) output file; most conversions create exactly one."""
        return self.output_paths[0] if self.output_paths else None
