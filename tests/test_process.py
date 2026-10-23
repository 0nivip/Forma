"""run_process(), exercised with small Python child processes instead of FFmpeg."""

import json
import subprocess
import sys
import time

import pytest

from fileconverter.infrastructure import process
from fileconverter.infrastructure.process import MAX_STDERR_LINES, run_process


def child(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@pytest.fixture
def started(monkeypatch):
    """Records every Popen object, so tests can check that no process is left running."""
    popens = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        popen = real_popen(*args, **kwargs)
        popens.append(popen)
        return popen

    monkeypatch.setattr(process.subprocess, "Popen", recording_popen)
    return popens


def test_success_delivers_stdout_lines_and_exit_code():
    lines = []
    result = run_process(child("print('a=1'); print('b=2')"), on_stdout_line=lines.append)

    assert result.returncode == 0
    assert lines == ["a=1", "b=2"]
    assert not result.cancelled


def test_failure_keeps_stderr_tail():
    code = "import sys; print('first', file=sys.stderr); print('boom', file=sys.stderr); exit(3)"
    result = run_process(child(code))

    assert result.returncode == 3
    assert result.stderr_tail.splitlines()[-1] == "boom"


def test_huge_stderr_does_not_block_and_is_bounded():
    # 20 000 lines (~1 MB) would fill an unread pipe and hang the child.
    code = "import sys\nfor i in range(20000): print('x' * 50, i, file=sys.stderr)"
    result = run_process(child(code), timeout=60)

    assert result.returncode == 0
    lines = result.stderr_tail.splitlines()
    assert len(lines) == MAX_STDERR_LINES
    assert lines[-1].endswith(" 19999")


def test_cancel_with_stop_input_lets_process_exit_cleanly(started):
    # Like FFmpeg: reads "q" from stdin and exits by itself.
    code = "import sys; print('ready', flush=True); sys.stdin.readline(); sys.exit(0)"
    ready = []

    result = run_process(
        child(code),
        on_stdout_line=ready.append,
        should_stop=lambda: bool(ready),
        stop_input=b"q\n",
        grace_period=10,
    )

    assert result.cancelled and result.returncode is None
    assert started[0].poll() == 0  # exited on its own, not killed


def test_cancel_escalates_to_kill_after_grace_period(started):
    code = "import time; print('ready', flush=True); time.sleep(60)"  # ignores stdin
    ready = []
    begin = time.monotonic()

    result = run_process(
        child(code),
        on_stdout_line=ready.append,
        should_stop=lambda: bool(ready),
        stop_input=b"q\n",
        grace_period=0.5,
    )

    assert result.cancelled
    assert time.monotonic() - begin < 15
    assert started[0].poll() is not None  # the process is gone


def test_cancel_without_stop_input_terminates(started):
    ready = []
    result = run_process(
        child("import time; print('ready', flush=True); time.sleep(60)"),
        on_stdout_line=ready.append,
        should_stop=lambda: bool(ready),
        grace_period=5,
    )
    assert result.cancelled
    assert started[0].poll() is not None


def test_timeout_stops_process(started):
    result = run_process(child("import time; time.sleep(60)"), timeout=0.5, grace_period=0.5)
    assert result.timed_out
    assert started[0].poll() is not None


def test_error_in_callback_never_leaks_the_process(started):
    def explode():
        raise RuntimeError("bug in poll callback")

    with pytest.raises(RuntimeError):
        run_process(child("import time; time.sleep(60)"), on_poll=explode)
    assert started[0].poll() is not None


def test_missing_program_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        run_process([str(tmp_path / "no-such-program.exe")])


def test_arguments_are_passed_verbatim_without_a_shell():
    # Spaces, quotes, shell metacharacters and Unicode must arrive as one argument each.
    tricky = ["a b", 'quote"inside', "semi;colon & pipe | caret^", "юникод ñ"]
    lines = []
    run_process(
        child("import sys, json; print(json.dumps(sys.argv[1:]))") + tricky,
        on_stdout_line=lines.append,
    )
    assert json.loads(lines[0]) == tricky
