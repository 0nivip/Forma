"""Main window behavior, driven through its widgets with pytest-qt."""

import threading

import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QFileDialog, QMessageBox

from fileconverter.core.errors import OutputError
from fileconverter.core.formats import BUILTIN_FORMATS
from fileconverter.core.models import Route
from fileconverter.core.registry import FormatRegistry
from fileconverter.gui.job_model import Column
from fileconverter.gui.main_window import MainWindow
from fileconverter.services.jobs import ACTIVE_STATES, JobState
from helpers import make_image

TIMEOUT_MS = 10_000


@pytest.fixture
def window(qtbot, registry):
    win = MainWindow(registry)
    qtbot.addWidget(win)  # closed (and its jobs waited for) after the test
    win.show()
    return win


def states(window):
    return [job.state for job in window.model.jobs()]


def cell(window, row, column):
    return window.model.index(row, column).data()


def combo_ids(window):
    combo = window.target_combo
    return [combo.itemData(i) for i in range(combo.count())]


def choose_target(window, format_id):
    combo = window.target_combo
    index = combo.findData(format_id)
    combo.setCurrentIndex(index)
    combo.activated.emit(index)  # what a real user selection emits


def convert_and_wait(qtbot, window):
    qtbot.mouseClick(window.convert_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(
        lambda: not any(state in ACTIVE_STATES for state in states(window)), timeout=TIMEOUT_MS
    )


def test_window_starts_empty(window):
    assert window.windowTitle() == "FileConverter"
    assert window.model.rowCount() == 0
    assert not window.convert_button.isEnabled()
    assert not window.target_combo.isEnabled()


def test_added_file_offers_only_supported_targets(window, tmp_path):
    window.add_files([make_image(tmp_path / "a.png")])

    assert states(window) == [JobState.READY]
    assert cell(window, 0, Column.STATUS) == "Ready"
    assert set(combo_ids(window)) == {"jpeg", "webp", "bmp", "tiff", "pdf"}
    assert window.convert_button.isEnabled()


def test_target_choices_follow_selection(window, tmp_path):
    window.add_files([make_image(tmp_path / "a.png"), make_image(tmp_path / "b.jpg", "JPEG")])

    # Nothing selected: only targets valid for every file.
    assert set(combo_ids(window)) == {"webp", "bmp", "tiff", "pdf"}

    window.table.selectRow(0)  # the PNG
    assert set(combo_ids(window)) == {"jpeg", "webp", "bmp", "tiff", "pdf"}
    window.table.selectRow(1)  # the JPEG
    assert set(combo_ids(window)) == {"png", "webp", "bmp", "tiff", "pdf"}


def test_choosing_target_updates_selected_row_only(window, tmp_path):
    window.add_files([make_image(tmp_path / "a.png"), make_image(tmp_path / "b.png")])
    window.table.selectRow(1)

    choose_target(window, "bmp")

    assert window.model.job_at(1).target.id == "bmp"
    assert window.model.job_at(0).target.id != "bmp"


def test_unsupported_file_is_marked_and_not_convertible(window, tmp_path):
    text = tmp_path / "notes.txt"
    text.write_text("not an image")

    window.add_files([text])

    assert states(window) == [JobState.UNSUPPORTED]
    assert cell(window, 0, Column.STATUS) == "Not supported"
    assert "not in a supported format" in cell(window, 0, Column.DETAILS)
    assert not window.convert_button.isEnabled()


def test_same_file_is_not_added_twice(window, tmp_path):
    path = make_image(tmp_path / "a.png")
    window.add_files([path])
    window.add_files([path])
    assert window.model.rowCount() == 1


def test_add_files_button_uses_file_picker(window, tmp_path, qtbot, monkeypatch):
    path = make_image(tmp_path / "a.png")
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *args: ([str(path)], ""))

    qtbot.mouseClick(window.add_button, Qt.MouseButton.LeftButton)

    assert window.model.job_at(0).source == path


def test_dropping_files_adds_them(window, tmp_path):
    path = make_image(tmp_path / "a.png")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )

    window.dropEvent(event)

    assert window.model.job_at(0).source == path


def test_remove_selected(window, tmp_path, qtbot):
    window.add_files([make_image(tmp_path / "a.png"), make_image(tmp_path / "b.png")])
    window.table.selectRow(0)

    qtbot.mouseClick(window.remove_button, Qt.MouseButton.LeftButton)

    assert [job.source.name for job in window.model.jobs()] == ["b.png"]


def test_successful_conversion(window, tmp_path, qtbot):
    window.add_files([make_image(tmp_path / "a.png")])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    choose_target(window, "webp")
    window.output_edit.setText(str(out_dir))

    convert_and_wait(qtbot, window)

    assert states(window) == [JobState.SUCCEEDED]
    assert cell(window, 0, Column.STATUS) == "Done"
    assert cell(window, 0, Column.DETAILS) == "Saved as a.webp"
    with Image.open(out_dir / "a.webp") as output:
        assert output.format == "WEBP"
    assert "1 converted, 0 failed" in window.statusBar().currentMessage()
    assert not window.convert_button.isEnabled()  # nothing left to convert


def test_default_output_is_next_to_source_and_never_overwrites(window, tmp_path, qtbot):
    source = make_image(tmp_path / "a.png")
    existing = tmp_path / "a.jpg"
    existing.write_bytes(b"keep me")
    window.add_files([source])
    choose_target(window, "jpeg")

    convert_and_wait(qtbot, window)

    assert cell(window, 0, Column.DETAILS) == "Saved as a (1).jpg"
    assert existing.read_bytes() == b"keep me"


def test_failed_conversion_reports_error_and_window_keeps_working(window, tmp_path, qtbot):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 20)  # PNG header, corrupt body
    window.add_files([broken])

    convert_and_wait(qtbot, window)

    assert states(window) == [JobState.FAILED]
    assert cell(window, 0, Column.STATUS) == "Failed"
    details = cell(window, 0, Column.DETAILS)
    assert "corrupted or unreadable" in details
    assert details.endswith(". Check that the file exists and is not damaged.")  # the hint
    assert list(tmp_path.glob(".fileconverter-*")) == []

    # The window is still fully usable afterwards.
    window.add_files([make_image(tmp_path / "good.png")])
    convert_and_wait(qtbot, window)
    assert states(window) == [JobState.FAILED, JobState.SUCCEEDED]


def test_missing_output_folder_is_reported_before_starting(window, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    window.add_files([make_image(tmp_path / "a.png")])
    window.output_edit.setText(str(tmp_path / "does-not-exist"))

    window.start_conversion()

    assert len(warnings) == 1
    assert states(window) == [JobState.READY]


class BlockingConverter:
    """png -> jpeg converter that blocks until the test releases it."""

    def __init__(self):
        self.release = threading.Event()
        self.thread_ids = []

    def routes(self):
        return [Route("png", "jpeg")]

    def convert(self, source, destination, route, options, ctx):
        self.thread_ids.append(threading.get_ident())
        ctx.report_progress(0.5)
        if not self.release.wait(TIMEOUT_MS / 1000):
            raise OutputError("test was never released")
        destination.write_bytes(b"converted")


def test_conversion_runs_off_gui_thread_with_status_updates(qtbot, tmp_path):
    converter = BlockingConverter()
    registry = FormatRegistry()
    registry.register_formats(BUILTIN_FORMATS)
    registry.register_converter(converter)
    window = MainWindow(registry)
    qtbot.addWidget(window)

    gui_thread = threading.get_ident()
    seen_states = []
    update_threads = set()

    def record_update(*args):
        update_threads.add(threading.get_ident())
        state = window.model.job_at(0).state
        if not seen_states or seen_states[-1] is not state:
            seen_states.append(state)

    window.model.dataChanged.connect(record_update)
    window.add_files([make_image(tmp_path / "a.png")])
    qtbot.mouseClick(window.convert_button, Qt.MouseButton.LeftButton)

    # The converter is now blocked inside convert(). The GUI can only show its progress
    # if the GUI thread is free to process events, i.e. the work runs elsewhere.
    qtbot.waitUntil(lambda: cell(window, 0, Column.STATUS) == "Converting 50%", timeout=TIMEOUT_MS)
    assert converter.thread_ids and converter.thread_ids[0] != gui_thread
    assert not window.convert_button.isEnabled()

    converter.release.set()
    qtbot.waitUntil(lambda: states(window) == [JobState.SUCCEEDED], timeout=TIMEOUT_MS)

    assert seen_states == [JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED]
    assert update_threads == {gui_thread}  # the model is only ever touched by the GUI thread
