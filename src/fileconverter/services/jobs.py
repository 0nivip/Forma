"""Conversion jobs: their states, allowed state changes, and a small scheduling queue.

Nothing here starts threads or touches Qt. The GUI owns the threads; this module
only records what is allowed and what should run next, which keeps it easy to test.
"""

import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

from fileconverter.core.formats import Format
from fileconverter.core.models import ConversionRequest, ConversionResult, OptionValue


class JobState(Enum):
    READY = auto()  # waiting for the user to press Convert
    QUEUED = auto()  # scheduled, waiting for a free worker
    RUNNING = auto()  # handed to a worker thread
    CANCELLING = auto()  # cancel requested; stops at the next safe checkpoint
    SUCCEEDED = auto()
    FAILED = auto()
    CANCELLED = auto()
    UNSUPPORTED = auto()  # the file cannot be converted at all; never queued


ACTIVE_STATES = {JobState.QUEUED, JobState.RUNNING, JobState.CANCELLING}

ALLOWED_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.READY: {JobState.QUEUED},
    JobState.QUEUED: {JobState.RUNNING, JobState.CANCELLED},
    JobState.RUNNING: {
        JobState.CANCELLING,
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
    },
    # A cancel request can lose the race against a job that is already finishing:
    # the job may still fail, or succeed if its output was committed first.
    JobState.CANCELLING: {JobState.CANCELLED, JobState.FAILED, JobState.SUCCEEDED},
    JobState.SUCCEEDED: set(),
    JobState.FAILED: set(),
    JobState.CANCELLED: set(),
    JobState.UNSUPPORTED: set(),
}


class InvalidTransitionError(RuntimeError):
    """A job was asked to move to a state that is not allowed from its current one."""


@dataclass
class Job:
    id: int
    source: Path
    targets: list[Format] = field(default_factory=list)  # valid targets, from the registry
    target: Format | None = None
    state: JobState = JobState.READY
    progress: float | None = None  # 0.0..1.0, or None when unknown
    request: ConversionRequest | None = None  # set when the job is queued
    result: ConversionResult | None = None
    cancel_requested: bool = False
    problem: str = ""  # why an UNSUPPORTED file cannot be converted
    source_format: Format | None = None
    options: dict[str, OptionValue] = field(default_factory=dict)  # only values the user set
    # Further files combined with `source` into one output (e.g. images into one PDF).
    extra_sources: tuple[Path, ...] = ()

    def transition_to(self, new_state: JobState) -> None:
        if new_state not in ALLOWED_TRANSITIONS[self.state]:
            raise InvalidTransitionError(
                f"Job {self.id}: {self.state.name} -> {new_state.name} is not allowed."
            )
        self.state = new_state


class CancelOutcome(Enum):
    REMOVED = auto()  # the job had not started; it will never run
    REQUESTED = auto()  # the job is running; its cancel event has been set
    NOT_ACTIVE = auto()  # the queue does not know this job (finished or never queued)


class JobQueue:
    """Decides which jobs may start, never running more than `max_running` at once.

    Bookkeeping only, and not thread-safe: call it from a single thread (the GUI thread).
    """

    def __init__(self, max_running: int) -> None:
        if max_running < 1:
            raise ValueError("max_running must be at least 1.")
        self.max_running = max_running
        self._pending: deque[int] = deque()
        self._running: dict[int, threading.Event] = {}  # job id -> its cancel event

    def submit(self, job_id: int) -> None:
        if job_id in self._pending or job_id in self._running:
            raise ValueError(f"Job {job_id} is already scheduled.")
        self._pending.append(job_id)

    def take_startable(self) -> list[tuple[int, threading.Event]]:
        """Move jobs from pending to running while free slots remain."""
        started = []
        while self._pending and len(self._running) < self.max_running:
            job_id = self._pending.popleft()
            cancel_event = threading.Event()
            self._running[job_id] = cancel_event
            started.append((job_id, cancel_event))
        return started

    def cancel(self, job_id: int) -> CancelOutcome:
        if job_id in self._pending:
            self._pending.remove(job_id)
            return CancelOutcome.REMOVED
        if job_id in self._running:
            self._running[job_id].set()
            return CancelOutcome.REQUESTED
        return CancelOutcome.NOT_ACTIVE

    def finished(self, job_id: int) -> None:
        """Free the slot of a job whose worker has reported its result."""
        self._running.pop(job_id, None)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def running_count(self) -> int:
        return len(self._running)

    def is_idle(self) -> bool:
        return not self._pending and not self._running
