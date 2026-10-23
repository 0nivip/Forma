"""Running external programs safely.

* Arguments are always passed as a list; no shell is involved, so file names can
  never be interpreted as commands.
* stdout and stderr are drained by background threads, so a chatty process can
  never block on a full pipe; only the last lines of stderr are kept in memory.
* Stopping is cooperative first (e.g. sending "q" to FFmpeg), then forced after a
  grace period, and the process is always waited for, so none is left running.
"""

import contextlib
import logging
import os
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Keeps console programs from flashing a window when started from the GUI on Windows.
_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
DEFAULT_GRACE_PERIOD_S = 5.0
MAX_STDERR_LINES = 200
POLL_INTERVAL_S = 0.1


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None  # None if the process was stopped by us
    stderr_tail: str  # the last lines of stderr, for error messages and the log
    cancelled: bool = False
    timed_out: bool = False


def run_process(
    args: Sequence[str],
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_stdout_line: Callable[[str], None] | None = None,
    on_poll: Callable[[], None] | None = None,
    stop_input: bytes | None = None,
    grace_period: float = DEFAULT_GRACE_PERIOD_S,
    timeout: float | None = None,
) -> ProcessResult:
    """Run a program to completion, cancellation, or timeout.

    `should_stop` and `on_poll` are called from the calling thread about every
    POLL_INTERVAL_S. `on_stdout_line` is called from a reader thread and must only
    do thread-safe work (e.g. store a value).

    Raises OSError (e.g. FileNotFoundError) if the program cannot be started.
    """
    process = subprocess.Popen(
        list(args),
        stdin=subprocess.PIPE if stop_input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATION_FLAGS,
    )
    stderr_lines: deque[str] = deque(maxlen=MAX_STDERR_LINES)
    readers = [
        _start_reader(process.stderr, stderr_lines.append),
        _start_reader(process.stdout, on_stdout_line or (lambda line: None)),
    ]
    cancelled = timed_out = False
    started = time.monotonic()
    try:
        while True:
            try:
                process.wait(timeout=POLL_INTERVAL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if on_poll is not None:
                on_poll()
            if should_stop():
                cancelled = True
            elif timeout is not None and time.monotonic() - started > timeout:
                timed_out = True
            if cancelled or timed_out:
                _stop(process, stop_input, grace_period)
                break
    finally:
        if process.poll() is None:  # an exception escaped the loop: never leak the process
            process.kill()
            process.wait()
        for reader in readers:
            reader.join(timeout=5)
        if process.stdin:
            _close_quietly(process.stdin)

    return ProcessResult(
        returncode=None if (cancelled or timed_out) else process.returncode,
        stderr_tail="\n".join(stderr_lines),
        cancelled=cancelled,
        timed_out=timed_out,
    )


def run_quick(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a short query command (e.g. `ffmpeg -version`) and capture its text output.

    Raises OSError if it cannot start and subprocess.TimeoutExpired on timeout.
    """
    return subprocess.run(
        list(args),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_CREATION_FLAGS,
        check=False,
    )


def _start_reader(stream, handle_line: Callable[[str], None]) -> threading.Thread:
    def read() -> None:
        try:
            for raw in iter(stream.readline, b""):
                handle_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        except (OSError, ValueError):
            pass  # the pipe was closed while the process was being stopped
        finally:
            _close_quietly(stream)

    thread = threading.Thread(target=read, daemon=True, name="process-reader")
    thread.start()
    return thread


def _stop(process: subprocess.Popen, stop_input: bytes | None, grace_period: float) -> None:
    """Ask the process to stop, and force it after `grace_period` seconds."""
    if stop_input is not None and process.stdin is not None:
        try:
            process.stdin.write(stop_input)
            process.stdin.flush()
        except OSError:
            pass  # it may already be exiting
    else:
        process.terminate()  # SIGTERM on POSIX; on Windows this already ends it
    try:
        process.wait(timeout=grace_period)
    except subprocess.TimeoutExpired:
        logger.warning("Process %s did not stop within %ss; killing it", process.pid, grace_period)
        process.kill()
        process.wait()


def _close_quietly(stream) -> None:
    with contextlib.suppress(OSError):
        stream.close()
