"""Queue, cancellation, settings and shutdown behavior of the main window.

Synchronization uses threading.Event and qtbot.waitUntil on observable state,
never fixed sleeps.
"""

import threading
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from fileconverter.conversion import runner
from fileconverter.core.errors import OutputError
from fileconverter.core.formats import BUILTIN_FORMATS
from fileconverter.core.models import Route
from fileconverter.core.registry import FormatRegistry
from fileconverter.gui.job_model import Column
from fileconverter.gui.main_window import MainWindow
from fileconverter.infrastructure.settings import Settings, SettingsStore
from fileconverter.services.jobs import JobState
from helpers import make_image, temp_files

TIMEOUT_S = 10
TIMEOUT_MS = TIMEOUT_S * 1000


class ControlledConverter:
    """png -> jpeg converter whose calls block until the test releases them.

    cooperative=True: checks for cancellation while blocked (like a backend with
    checkpoints). cooperative=False: ignores it until released (like one long,
    uninterruptible library call).
    """

    def __init__(self, cooperative=True, fail_for=(), progress_steps=0):
        self.cooperative = cooperative
        self.fail_for = set(fail_for)
        self.progress_steps = progress_steps
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    def routes(self):
        return [Route("png", "jpeg")]

    def convert(self, source, destination, route, options, ctx):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if source.name in self.fail_for:
                raise RuntimeError(f"simulated crash for {source.name}")
            for step in range(self.progress_steps):
                ctx.report_progress(step / self.progress_steps)
            destination.write_bytes(b"partial")
            deadline = time.monotonic() + TIMEOUT_S
            while not self.release.wait(0.01):
                if self.cooperative:
                    ctx.raise_if_cancelled()
                if time.monotonic() > deadline:
                    raise OutputError("test never released the converter")
            destination.write_bytes(b"converted")
        finally:
            with self._lock:
                self.active -= 1


@pytest.fixture
def make_window(qtbot):
    converters = []

    def factory(converter, settings_store=None, max_parallel_jobs=4, shutdown_timeout_ms=10_000):
        converters.append(converter)
        registry = FormatRegistry()
        registry.register_formats(BUILTIN_FORMATS)
        registry.register_converter(converter)
        window = MainWindow(registry, settings_store, max_parallel_jobs, shutdown_timeout_ms)
        qtbot.addWidget(window)
        window.show()
        return window

    yield factory
    for converter in converters:  # never leave worker threads blocked after a test
        converter.release.set()


@pytest.fixture
def sources(tmp_path):
    folder = tmp_path / "src"
    return [make_image(folder / f"{name}.png") for name in ("a", "b", "c")]


def states(window):
    return [job.state for job in window.model.jobs()]


def wait_for(qtbot, condition):
    qtbot.waitUntil(condition, timeout=TIMEOUT_MS)


def select(window, row):
    window.table.selectRow(row)


def outputs(folder):
    return sorted(p.name for p in folder.iterdir() if p.suffix != ".png")


def answer_close_dialog(monkeypatch, answer):
    monkeypatch.setattr(QMessageBox, "question", lambda *args: answer)


# --- queue ------------------------------------------------------------------


def test_concurrency_limit_is_respected(make_window, qtbot, sources):
    converter = ControlledConverter()
    window = make_window(converter, max_parallel_jobs=2)
    window.add_files(sources)

    window.start_conversion()

    assert states(window) == [JobState.RUNNING, JobState.RUNNING, JobState.QUEUED]
    wait_for(qtbot, lambda: converter.active == 2)
    converter.release.set()
    wait_for(qtbot, lambda: states(window) == [JobState.SUCCEEDED] * 3)
    assert converter.max_active == 2


def test_starting_twice_does_not_schedule_jobs_twice(make_window, qtbot, sources):
    converter = ControlledConverter()
    window = make_window(converter, max_parallel_jobs=1)
    window.add_files(sources[:2])

    window.start_conversion()
    window.start_conversion()
    converter.release.set()

    wait_for(qtbot, lambda: states(window) == [JobState.SUCCEEDED] * 2)
    assert converter.calls == 2


def test_crash_in_one_job_does_not_stop_the_queue(make_window, qtbot, sources):
    converter = ControlledConverter(fail_for={"a.png"})
    converter.release.set()
    window = make_window(converter, max_parallel_jobs=1)
    window.add_files(sources[:2])

    window.start_conversion()

    wait_for(qtbot, lambda: window.model.job_at(1).state is JobState.SUCCEEDED)
    assert window.model.job_at(0).state is JobState.FAILED
    assert "probably a bug" in window.model.index(0, Column.DETAILS).data()
    assert not window.has_running_jobs()
    assert window._workers == {}  # finished workers are released


def test_crash_inside_worker_does_not_stop_the_queue(make_window, qtbot, sources, monkeypatch):
    real_run_job = runner.run_job
    calls = []

    def crash_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("runner bug")
        return real_run_job(*args, **kwargs)

    monkeypatch.setattr("fileconverter.gui.worker.run_job", crash_once)
    converter = ControlledConverter()
    converter.release.set()
    window = make_window(converter, max_parallel_jobs=1)
    window.add_files(sources[:2])

    window.start_conversion()

    wait_for(qtbot, lambda: states(window) == [JobState.FAILED, JobState.SUCCEEDED])


def test_progress_updates_are_throttled(make_window, qtbot, sources):
    converter = ControlledConverter(progress_steps=5000)
    converter.release.set()
    window = make_window(converter)
    updates = []
    window.model.dataChanged.connect(lambda *args: updates.append(1))
    window.add_files(sources[:1])

    window.start_conversion()

    wait_for(qtbot, lambda: states(window) == [JobState.SUCCEEDED])
    # 5000 reports collapse to at most one per visible percent (+ state changes).
    assert len(updates) <= 110


# --- cancellation -----------------------------------------------------------


def test_cancel_button_follows_selected_job_state(make_window, qtbot, sources):
    converter = ControlledConverter()
    window = make_window(converter)
    window.add_files(sources[:1])
    select(window, 0)
    assert not window.cancel_button.isEnabled()  # READY: nothing to cancel

    window.start_conversion()
    assert window.cancel_button.isEnabled()

    converter.release.set()
    wait_for(qtbot, lambda: states(window) == [JobState.SUCCEEDED])
    assert not window.cancel_button.isEnabled()


def test_cancel_queued_job_never_starts_it(make_window, qtbot, sources):
    converter = ControlledConverter()
    window = make_window(converter, max_parallel_jobs=1)
    window.add_files(sources[:2])
    window.start_conversion()
    wait_for(qtbot, lambda: converter.active == 1)

    select(window, 1)
    qtbot.mouseClick(window.cancel_button, Qt.MouseButton.LeftButton)

    assert window.model.job_at(1).state is JobState.CANCELLED  # immediately
    assert window.model.index(1, Column.DETAILS).data() == "Cancelled before it started."
    converter.release.set()
    wait_for(qtbot, lambda: window.model.job_at(0).state is JobState.SUCCEEDED)
    assert converter.calls == 1
    assert outputs(sources[0].parent) == ["a.jpg"]


def test_cancel_running_job_stops_and_cleans_up(make_window, qtbot, sources):
    converter = ControlledConverter(cooperative=True)
    window = make_window(converter)
    gui_thread = threading.get_ident()
    update_threads = set()
    window.model.dataChanged.connect(lambda *a: update_threads.add(threading.get_ident()))
    window.add_files(sources[:1])
    original = sources[0].read_bytes()
    window.start_conversion()
    wait_for(qtbot, lambda: converter.active == 1)

    select(window, 0)
    qtbot.mouseClick(window.cancel_button, Qt.MouseButton.LeftButton)

    assert window.model.index(0, Column.STATUS).data() == "Cancelling…"
    wait_for(qtbot, lambda: states(window) == [JobState.CANCELLED])
    details = window.model.index(0, Column.DETAILS).data()
    assert details == "Stopped before finishing; no file was saved."
    assert outputs(sources[0].parent) == []
    assert temp_files(sources[0].parent) == []
    assert sources[0].read_bytes() == original
    assert update_threads == {gui_thread}


def test_cancel_waits_for_safe_checkpoint_without_freezing(make_window, qtbot, sources):
    converter = ControlledConverter(cooperative=False)  # one long uninterruptible step
    window = make_window(converter)
    window.add_files(sources[:1])
    window.start_conversion()
    wait_for(qtbot, lambda: converter.active == 1)

    select(window, 0)
    window.cancel_selected()

    # Still blocked inside the converter: the request is pending, the GUI responsive.
    assert states(window) == [JobState.CANCELLING]
    window.add_files(sources[1:2])
    assert window.model.rowCount() == 2

    converter.release.set()  # the step ends; the runner's next checkpoint cancels
    wait_for(qtbot, lambda: window.model.job_at(0).state is JobState.CANCELLED)
    assert outputs(sources[0].parent) == []
    assert temp_files(sources[0].parent) == []


def test_cancel_after_output_was_committed_reports_success_honestly(
    make_window, qtbot, sources, monkeypatch
):
    committed = threading.Event()
    proceed = threading.Event()
    real_move = runner._move_into_place

    def move_then_pause(*args):
        path = real_move(*args)
        committed.set()
        proceed.wait(TIMEOUT_S)
        return path

    monkeypatch.setattr(runner, "_move_into_place", move_then_pause)
    converter = ControlledConverter()
    converter.release.set()
    window = make_window(converter)
    window.add_files(sources[:1])
    window.start_conversion()
    wait_for(qtbot, committed.is_set)

    select(window, 0)
    window.cancel_selected()  # too late: the file is already saved
    assert states(window) == [JobState.CANCELLING]
    proceed.set()

    wait_for(qtbot, lambda: states(window) == [JobState.SUCCEEDED])
    details = window.model.index(0, Column.DETAILS).data()
    assert "already saved when cancellation was requested" in details
    assert outputs(sources[0].parent) == ["a.jpg"]


def test_cancelled_jobs_never_leave_output(make_window, qtbot, sources):
    # Cancel every job at once (one running, two queued): no output may appear.
    converter = ControlledConverter()
    window = make_window(converter, max_parallel_jobs=1)
    window.add_files(sources)
    window.start_conversion()
    wait_for(qtbot, lambda: converter.active == 1)

    window.table.selectAll()
    window.cancel_selected()

    wait_for(qtbot, lambda: states(window) == [JobState.CANCELLED] * 3)
    assert outputs(sources[0].parent) == []
    assert temp_files(sources[0].parent) == []


# --- settings ---------------------------------------------------------------


def test_settings_are_saved_on_close_and_restored(make_window, qtbot, tmp_path, sources):
    store = SettingsStore(tmp_path / "cfg" / "settings.json")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    window = make_window(ControlledConverter(), store)
    window.output_edit.setText(str(out_dir))
    window.add_files(sources[:1])
    combo = window.target_combo
    combo.activated.emit(combo.findData("jpeg"))

    assert window.close()

    saved = store.load()
    assert saved.output_dir == str(out_dir)
    assert saved.preferred_target == "jpeg"
    assert saved.window_geometry

    reopened = make_window(ControlledConverter(), store)
    assert reopened.output_edit.text() == str(out_dir)


def test_restored_preferences_are_validated(registry, qtbot, tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        Settings(
            output_dir=str(tmp_path / "deleted-folder"),
            preferred_target="webp",
            window_geometry="not-valid-geometry",
        )
    )
    window = MainWindow(registry, store)
    qtbot.addWidget(window)

    assert window.output_edit.text() == ""  # folder no longer exists
    window.add_files([make_image(tmp_path / "a.png")])
    assert window.model.job_at(0).target.id == "webp"  # still valid for PNG

    store.save(Settings(preferred_target="no-such-format"))
    window2 = MainWindow(registry, store)
    qtbot.addWidget(window2)
    window2.add_files([make_image(tmp_path / "b.png")])
    assert window2.model.job_at(0).target is not None  # falls back to a valid target


def test_conversion_still_works_when_settings_cannot_be_saved(registry, qtbot, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the settings folder should be")
    window = MainWindow(registry, SettingsStore(blocker / "settings.json"))
    qtbot.addWidget(window)
    window.add_files([make_image(tmp_path / "a.png")])

    window.start_conversion()

    wait_for(qtbot, lambda: states(window) == [JobState.SUCCEEDED])
    assert window.close()


# --- shutdown ---------------------------------------------------------------


def test_close_without_jobs_closes_immediately(make_window):
    window = make_window(ControlledConverter())
    assert window.close()
    assert not window.isVisible()


def test_declining_close_keeps_jobs_running(make_window, qtbot, sources, monkeypatch):
    answer_close_dialog(monkeypatch, QMessageBox.StandardButton.No)
    converter = ControlledConverter()
    window = make_window(converter)
    window.add_files(sources[:1])
    window.start_conversion()

    assert not window.close()

    assert window.isVisible()
    assert states(window) == [JobState.RUNNING]
    converter.release.set()
    wait_for(qtbot, lambda: states(window) == [JobState.SUCCEEDED])


def test_close_with_queued_and_running_jobs(make_window, qtbot, sources, monkeypatch, tmp_path):
    answer_close_dialog(monkeypatch, QMessageBox.StandardButton.Yes)
    store = SettingsStore(tmp_path / "cfg" / "settings.json")
    converter = ControlledConverter(cooperative=True)
    window = make_window(converter, store, max_parallel_jobs=1)
    window.add_files(sources[:2])
    window.start_conversion()
    wait_for(qtbot, lambda: converter.active == 1)

    closed_now = window.close()

    # close() returned at once: the event loop was not blocked waiting for workers.
    assert not closed_now
    assert window.isVisible()
    assert states(window) == [JobState.CANCELLING, JobState.CANCELLED]
    assert not window.add_button.isEnabled()
    assert not window.convert_button.isEnabled()

    wait_for(qtbot, lambda: not window.isVisible())
    assert states(window) == [JobState.CANCELLED, JobState.CANCELLED]
    assert not window.has_running_jobs()
    assert outputs(sources[0].parent) == []
    assert temp_files(sources[0].parent) == []
    assert store.path.is_file()  # settings saved on the way out


def test_close_gives_up_waiting_after_timeout(make_window, qtbot, sources, monkeypatch):
    answer_close_dialog(monkeypatch, QMessageBox.StandardButton.Yes)
    converter = ControlledConverter(cooperative=False)
    window = make_window(converter, shutdown_timeout_ms=200)
    window.add_files(sources[:1])
    window.start_conversion()
    wait_for(qtbot, lambda: converter.active == 1)

    window.close()

    wait_for(qtbot, lambda: not window.isVisible())
    assert window.has_running_jobs()  # app.main uses this to end the process

    # Once the stuck step ends, the job still reaches its checkpoint and cleans up.
    converter.release.set()
    wait_for(qtbot, lambda: not window.has_running_jobs())
    assert states(window) == [JobState.CANCELLED]
    assert outputs(sources[0].parent) == []
    assert temp_files(sources[0].parent) == []
