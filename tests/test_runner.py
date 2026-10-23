"""Runner behavior that is independent of any real backend, tested with a fake converter."""

import threading
from pathlib import Path

import pytest

from fileconverter.conversion import runner
from fileconverter.conversion.runner import run_job
from fileconverter.core.errors import (
    ConversionError,
    InvalidInputError,
    OutputError,
    UnsupportedConversionError,
)
from fileconverter.core.formats import BUILTIN_FORMATS
from fileconverter.core.models import ConversionRequest, JobStatus, Route
from fileconverter.core.registry import FormatRegistry
from helpers import make_image, temp_files


class FakeConverter:
    """Supports only png -> jpeg and does whatever `action` says."""

    def __init__(self, action):
        self.action = action
        self.calls = 0

    def routes(self):
        return [Route("png", "jpeg")]

    def convert(self, source, destination, route, options, ctx):
        self.calls += 1
        self.action(destination, ctx)


def write_bytes(destination, ctx):
    destination.write_bytes(b"converted")


def make_registry(action=write_bytes):
    converter = FakeConverter(action)
    registry = FormatRegistry()
    registry.register_formats(BUILTIN_FORMATS)
    registry.register_converter(converter)
    return registry, converter


@pytest.fixture
def source(tmp_path):
    return make_image(tmp_path / "in" / "photo.png")


@pytest.fixture
def out_dir(tmp_path):
    path = tmp_path / "out"
    path.mkdir()
    return path


def test_success_moves_output_into_place(source, out_dir):
    registry, _ = make_registry()
    result = run_job(ConversionRequest(source, "jpeg", out_dir), registry)

    assert result.status is JobStatus.SUCCEEDED
    assert result.output_path == out_dir / "photo.jpg"
    assert result.output_path.read_bytes() == b"converted"
    assert temp_files(out_dir) == []


def test_existing_output_file_is_never_overwritten(source, out_dir):
    existing = out_dir / "photo.jpg"
    existing.write_bytes(b"precious")
    registry, _ = make_registry()

    result = run_job(ConversionRequest(source, "jpeg", out_dir), registry)

    assert result.output_path == out_dir / "photo (1).jpg"
    assert existing.read_bytes() == b"precious"


def test_converter_error_fails_job_and_cleans_up(source, out_dir):
    def fail(destination, ctx):
        destination.write_bytes(b"half-written")
        raise OutputError("disk full")

    registry, _ = make_registry(fail)
    result = run_job(ConversionRequest(source, "jpeg", out_dir), registry)

    assert result.status is JobStatus.FAILED
    assert isinstance(result.error, OutputError)
    assert list(out_dir.iterdir()) == []


def test_unexpected_exception_becomes_failed_result(source, out_dir, caplog):
    def crash(destination, ctx):
        raise RuntimeError("bug in backend")

    registry, _ = make_registry(crash)
    result = run_job(ConversionRequest(source, "jpeg", out_dir), registry)

    assert result.status is JobStatus.FAILED
    assert isinstance(result.error, ConversionError)
    assert isinstance(result.error.__cause__, RuntimeError)
    assert "bug in backend" in caplog.text  # logged with traceback, not swallowed
    assert list(out_dir.iterdir()) == []


def test_empty_output_is_not_reported_as_success(source, out_dir):
    registry, _ = make_registry(lambda destination, ctx: None)
    result = run_job(ConversionRequest(source, "jpeg", out_dir), registry)

    assert result.status is JobStatus.FAILED
    assert isinstance(result.error, OutputError)
    assert list(out_dir.iterdir()) == []


def test_cancel_before_start_does_not_call_converter(source, out_dir):
    registry, converter = make_registry()
    cancel = threading.Event()
    cancel.set()

    result = run_job(ConversionRequest(source, "jpeg", out_dir), registry, cancel_event=cancel)

    assert result.status is JobStatus.CANCELLED
    assert converter.calls == 0
    assert list(out_dir.iterdir()) == []


def test_cancel_during_conversion_cleans_up(source, out_dir):
    cancel = threading.Event()

    def cancel_midway(destination, ctx):
        destination.write_bytes(b"partial")
        cancel.set()
        ctx.raise_if_cancelled()

    registry, _ = make_registry(cancel_midway)
    result = run_job(ConversionRequest(source, "jpeg", out_dir), registry, cancel_event=cancel)

    assert result.status is JobStatus.CANCELLED
    assert list(out_dir.iterdir()) == []


def test_missing_source_file(tmp_path, out_dir):
    registry, _ = make_registry()
    result = run_job(ConversionRequest(tmp_path / "nope.png", "jpeg", out_dir), registry)
    assert isinstance(result.error, InvalidInputError)


def test_directory_as_source(tmp_path, out_dir):
    registry, _ = make_registry()
    result = run_job(ConversionRequest(tmp_path, "jpeg", out_dir), registry)
    assert isinstance(result.error, InvalidInputError)


def test_missing_output_dir(source, tmp_path):
    registry, _ = make_registry()
    result = run_job(ConversionRequest(source, "jpeg", tmp_path / "missing"), registry)
    assert isinstance(result.error, OutputError)


def test_unsupported_pair(source, out_dir):
    registry, _ = make_registry()
    result = run_job(ConversionRequest(source, "webp", out_dir), registry)
    assert isinstance(result.error, UnsupportedConversionError)


def test_name_taken_between_check_and_commit_picks_next_name(source, out_dir, monkeypatch):
    # Simulate another program creating the chosen file right after we chose it.
    real_choose = runner.choose_output_path
    planted = []

    def choose_then_steal(*args):
        path = real_choose(*args)
        if not planted:
            path.write_bytes(b"other program")
            planted.append(path)
        return path

    monkeypatch.setattr(runner, "choose_output_path", choose_then_steal)
    registry, _ = make_registry()

    result = run_job(ConversionRequest(source, "jpeg", out_dir), registry)

    assert result.status is JobStatus.SUCCEEDED
    assert result.output_path == out_dir / "photo (1).jpg"
    assert planted[0].read_bytes() == b"other program"  # not overwritten by the rename
    assert temp_files(out_dir) == []


def test_name_always_stolen_fails_without_overwriting(source, out_dir, monkeypatch):
    real_choose = runner.choose_output_path

    def always_steal(*args):
        path = real_choose(*args)
        path.write_bytes(b"other program")
        return path

    monkeypatch.setattr(runner, "choose_output_path", always_steal)
    registry, _ = make_registry()

    result = run_job(ConversionRequest(source, "jpeg", out_dir), registry)

    assert isinstance(result.error, OutputError)
    assert temp_files(out_dir) == []
    assert all(path.read_bytes() == b"other program" for path in out_dir.iterdir())


def test_lookalike_user_file_is_not_deleted_on_failure(source, out_dir):
    # Cleanup removes only the exact temp path of this job, never files matching a pattern.
    lookalike = out_dir / ".fileconverter-mine.part"
    lookalike.write_bytes(b"user data")

    def fail(destination, ctx):
        raise OutputError("boom")

    registry, _ = make_registry(fail)
    run_job(ConversionRequest(source, "jpeg", out_dir), registry)

    assert list(out_dir.iterdir()) == [lookalike]


# The POSIX commit path (hard link + unlink) also works on NTFS, so it is tested here.
def test_link_commit_refuses_to_replace_existing_file(tmp_path):
    src, dst = tmp_path / "temp.part", tmp_path / "out.jpg"
    src.write_bytes(b"new")
    dst.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        runner._link_then_unlink(src, dst)
    assert dst.read_bytes() == b"existing"


def test_link_commit_succeeds_even_if_temp_name_cannot_be_removed(tmp_path, monkeypatch):
    src, dst = tmp_path / "temp.part", tmp_path / "out.jpg"
    src.write_bytes(b"new")

    def refuse(self, missing_ok=False):
        raise PermissionError("locked by antivirus")

    monkeypatch.setattr(Path, "unlink", refuse)
    runner._link_then_unlink(src, dst)  # must not raise: the output is already committed
    monkeypatch.undo()

    assert dst.read_bytes() == b"new"


def test_progress_reports_completion(source, out_dir):
    registry, _ = make_registry()
    reported = []
    run_job(ConversionRequest(source, "jpeg", out_dir), registry, on_progress=reported.append)
    assert reported[-1] == 1.0
