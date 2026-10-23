"""The contract every conversion backend implements."""

import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol

from fileconverter.core.errors import ConversionCancelled
from fileconverter.core.models import Route

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
    ) -> None:
        self._cancel_event = cancel_event or threading.Event()
        self._on_progress = on_progress
        self.warnings: list[str] = []

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


class Converter(Protocol):
    """A conversion backend (Pillow, FFmpeg, LibreOffice, ...).

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
        options: Mapping[str, int],
        ctx: JobContext,
    ) -> None:
        """Write the converted file to `destination`.

        Must raise a ConversionError subclass for expected failures and
        ConversionCancelled when ctx.raise_if_cancelled() fires.
        """
        ...
