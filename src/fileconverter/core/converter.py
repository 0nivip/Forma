"""The contract every conversion backend implements."""

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fileconverter.core.errors import ConversionCancelled
from fileconverter.core.models import OptionValue, Route

# Receives a fraction in [0.0, 1.0], or None when progress is unknown.
type ProgressCallback = Callable[[float | None], None]


class JobContext:
    """Lets a running converter report progress/warnings and check for cancellation.

    It contains no Qt code: a GUI can pass a callback that emits a Qt signal,
    while tests and the CLI can pass a plain function (or nothing).
    """

    def __init__(
        self,
        cancel_event: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
        preserve_metadata: bool = False,
    ) -> None:
        self._cancel_event = cancel_event or threading.Event()
        self._on_progress = on_progress
        self.warnings: list[str] = []
        # The user's privacy choice for this job; see ConversionRequest.preserve_metadata.
        self.preserve_metadata = preserve_metadata

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise ConversionCancelled()

    def report_progress(self, fraction: float | None) -> None:
        if self._on_progress is not None:
            self._on_progress(fraction)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


@dataclass(frozen=True)
class OutputPart:
    """One file of a multi-output conversion, written inside the job's temp folder."""

    path: Path
    # Appended to the source name: "report" + "page_001" -> "report_page_001.png".
    # Empty means the plain source name ("report.csv").
    name_suffix: str


class Converter(Protocol):
    """A conversion backend (Pillow, FFmpeg, mammoth, ...).

    A converter only transforms `source` into `destination`. Everything around that
    (validation, output naming, temp files, overwrite protection) is the runner's job.
    """

    def routes(self) -> Iterable[Route]:
        """All source -> target conversions this backend supports."""
        ...

    def convert(
        self,
        source: Path,
        destination: Path,
        route: Route,
        options: Mapping[str, OptionValue],
        ctx: JobContext,
    ) -> list[OutputPart] | None:
        """Write the converted result.

        Single-output routes: write the file `destination` and return None.
        Multi-output routes (route.multi_output): `destination` is an empty temp folder;
        write the files into it and return them in order.

        Must raise a ConversionError subclass for expected failures and
        ConversionCancelled when ctx.raise_if_cancelled() fires.
        """
        ...


class MultiInputConverter(Converter, Protocol):
    """A converter whose routes may combine several sources (route.multi_input)."""

    def convert_group(
        self,
        sources: Sequence[Path],
        destination: Path,
        route: Route,
        options: Mapping[str, OptionValue],
        ctx: JobContext,
    ) -> None:
        """Combine `sources`, in order, into the single file `destination`."""
        ...
