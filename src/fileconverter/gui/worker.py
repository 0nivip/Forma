"""Runs conversion jobs on QThreadPool threads, away from the GUI thread."""

import logging
import threading

from PySide6.QtCore import QObject, QRunnable, Signal

from fileconverter.conversion.runner import run_job
from fileconverter.core.errors import ConversionError
from fileconverter.core.models import ConversionRequest, ConversionResult, JobStatus
from fileconverter.core.registry import FormatRegistry

logger = logging.getLogger(__name__)

_NOTHING_REPORTED = object()


class WorkerSignals(QObject):
    # QRunnable is not a QObject and cannot define signals itself, hence this helper.
    progress = Signal(int, object)  # job id, fraction (float) or None
    finished = Signal(int, object)  # job id, ConversionResult


class ConversionWorker(QRunnable):
    """Executes one job with the Qt-free runner and reports back through signals.

    The signals are emitted from a pool thread. Because the receiving slots belong
    to objects living in the GUI thread, Qt delivers them as queued events, so the
    slots run in the GUI thread and may safely update widgets.

    Cancellation is cooperative: setting `cancel_event` (from any thread) makes the
    runner stop at its next checkpoint. The thread itself is never killed.
    """

    def __init__(
        self,
        job_id: int,
        request: ConversionRequest,
        registry: FormatRegistry,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self.job_id = job_id
        self.request = request
        self.registry = registry
        self.cancel_event = cancel_event
        self.signals = WorkerSignals()
        self._last_percent: object = _NOTHING_REPORTED  # only touched by the worker thread

    def run(self) -> None:
        try:
            result = run_job(
                self.request,
                self.registry,
                cancel_event=self.cancel_event,
                on_progress=self._report_progress,
            )
        except Exception as exc:
            # run_job is designed not to raise; if it ever does, still finish the job
            # so its row does not stay "Converting" and the queue keeps going.
            logger.exception("Worker failed for '%s'", self.request.source)
            error = ConversionError(f"Unexpected error: {exc}")
            error.__cause__ = exc
            result = ConversionResult(self.request, JobStatus.FAILED, error=error)
        self.signals.finished.emit(self.job_id, result)

    def _report_progress(self, fraction: float | None) -> None:
        # Forward only visible changes (whole percents), so a backend reporting
        # thousands of tiny steps cannot flood the GUI thread with events.
        percent = None if fraction is None else round(fraction * 100)
        if percent != self._last_percent:
            self._last_percent = percent
            self.signals.progress.emit(self.job_id, fraction)
