"""Batch workflow, retrying failed jobs, quality presets and deterministic output names."""

import threading

import pytest
from PIL import Image
from PySide6.QtWidgets import QMessageBox

from fileconverter.cli import main
from fileconverter.conversion.backends.images import PRESET_QUALITY
from fileconverter.core.errors import InvalidOptionError
from fileconverter.core.formats import BUILTIN_FORMATS
from fileconverter.core.models import Route
from fileconverter.core.output_paths import PlannedOutput, plan_output_stems
from fileconverter.core.registry import FormatRegistry
from fileconverter.gui.job_model import Column
from fileconverter.gui.main_window import MainWindow
from fileconverter.services.jobs import ACTIVE_STATES, JobState
from helpers import make_image, temp_files, write_pdf

TIMEOUT_MS = 30_000


def noisy_image(path, size=(96, 96)):
    Image.effect_noise(size, 80).convert("RGB").save(path)
    return path


def broken_png(path):
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"junk" * 30)
    return path


@pytest.fixture
def window(qtbot, registry):
    win = MainWindow(registry)
    qtbot.addWidget(win)
    return win


def wait_done(qtbot, window):
    qtbot.waitUntil(
        lambda: not any(job.state in ACTIVE_STATES for job in window.model.jobs()),
        timeout=TIMEOUT_MS,
    )


def choose_target(window, row, format_id):
    window.table.selectRow(row)
    combo = window.target_combo
    combo.activated.emit(combo.findData(format_id))


# --- presets in the shared option definitions --------------------------------------


@pytest.mark.parametrize("target", ["jpeg", "webp", "avif"])
@pytest.mark.parametrize("preset", ["high", "balanced", "small"])
def test_preset_sets_the_documented_quality(registry, target, preset):
    route = registry.find_route("png", target)[0]
    assert route.resolve_options({"preset": preset})["quality"] == PRESET_QUALITY[target][preset]


def test_preset_values_are_codec_specific():
    assert PRESET_QUALITY == {
        "jpeg": {"high": 92, "balanced": 82, "small": 65},
        "webp": {"high": 90, "balanced": 80, "small": 60},
        "avif": {"high": 85, "balanced": 65, "small": 45},
    }


def test_custom_preset_keeps_quality_and_defaults_are_unchanged(registry):
    route = registry.find_route("png", "jpeg")[0]
    assert route.resolve_options({}) == {"preset": "custom", "quality": 90}  # as before
    assert route.resolve_options({"preset": "custom", "quality": 70})["quality"] == 70


def test_preset_and_contradicting_quality_is_rejected(registry):
    route = registry.find_route("png", "jpeg")[0]
    with pytest.raises(InvalidOptionError, match="contradicts preset 'small'"):
        route.resolve_options({"preset": "small", "quality": 50})
    # The same value is not a contradiction (this is what the GUI sends).
    assert route.resolve_options({"preset": "small", "quality": 65})["quality"] == 65


@pytest.mark.parametrize("target", ["png", "gif", "bmp", "tiff", "ico"])
def test_lossless_or_palette_targets_have_no_preset(registry, target):
    route = registry.find_route("jpeg", target)[0]
    assert {option.name for option in route.options} == set()
    with pytest.raises(InvalidOptionError, match="Unsupported option"):
        route.resolve_options({"preset": "small"})


def test_pdf_to_jpeg_has_the_jpeg_presets(registry):
    route = registry.find_route("pdf", "jpeg")[0]
    assert route.resolve_options({"preset": "small"})["quality"] == 65


def test_smaller_file_preset_really_makes_smaller_files(tmp_path):
    source = noisy_image(tmp_path / "noise.png")
    assert main(["convert", str(source), "--to", "jpeg", "--preset", "high"]) == 0
    assert main(["convert", str(source), "--to", "jpeg", "--preset", "small"]) == 0
    assert (tmp_path / "noise (1).jpg").stat().st_size < (tmp_path / "noise.jpg").stat().st_size


def test_cli_preset_errors(tmp_path, capsys):
    source = make_image(tmp_path / "a.png")
    photo = make_image(tmp_path / "b.jpg", "JPEG")
    assert (
        main(["convert", str(source), "--to", "jpeg", "--preset", "small", "--quality", "50"]) == 1
    )
    assert main(["convert", str(photo), "--to", "png", "--preset", "small"]) == 1
    err = capsys.readouterr().err
    assert "contradicts preset" in err and "Unsupported option" in err
    assert not list(tmp_path.glob("a*.jpg")) and not list(tmp_path.glob("b*.png"))
    assert temp_files(tmp_path) == []


def test_cli_preset_help_lists_the_values(capsys):
    with pytest.raises(SystemExit):
        main(["convert", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--preset {custom,high,balanced,small}" in text
    assert "JPEG high 92, balanced 82, small 65" in text


# --- presets in the GUI ----------------------------------------------------------------


def test_gui_preset_fills_quality_and_manual_quality_means_custom(window, tmp_path):
    window.add_files([make_image(tmp_path / "a.png")])
    choose_target(window, 0, "jpeg")
    preset, quality = window.option_widgets["preset"], window.option_widgets["quality"]
    assert (preset.currentData(), quality.value()) == ("custom", 90)

    preset.setCurrentIndex(preset.findData("small"))
    assert quality.value() == 65
    assert window.model.job_at(0).options == {"preset": "small", "quality": 65}

    quality.setValue(72)  # the user's own value overrides the preset
    assert preset.currentData() == "custom"
    assert window.model.job_at(0).options == {"preset": "custom", "quality": 72}


def test_gui_preset_choice_is_used_for_the_conversion(window, tmp_path, qtbot):
    window.add_files([noisy_image(tmp_path / "n.png")])
    choose_target(window, 0, "webp")
    preset = window.option_widgets["preset"]
    preset.setCurrentIndex(preset.findData("balanced"))

    window.start_conversion()
    wait_done(qtbot, window)

    job = window.model.job_at(0)
    assert job.state is JobState.SUCCEEDED
    assert job.request.options == {"preset": "balanced", "quality": 80}


def test_gui_shows_no_preset_for_png(window, tmp_path):
    window.add_files([make_image(tmp_path / "a.jpg", "JPEG")])
    choose_target(window, 0, "png")
    assert window.option_widgets == {}


# --- deterministic output names --------------------------------------------------------


def test_plan_output_stems(tmp_path):
    (tmp_path / "photo.jpg").write_bytes(b"existing")
    other = tmp_path / "other"
    planned = [
        PlannedOutput(tmp_path, "photo", ".jpg"),
        PlannedOutput(tmp_path, "Photo", ".jpg"),  # same name for Windows
        PlannedOutput(other, "photo", ".jpg"),  # different folder: no conflict
        PlannedOutput(tmp_path, "photo", ".png"),  # different extension: no conflict
        PlannedOutput(tmp_path, "doc", ".png", check_disk=False),
        PlannedOutput(tmp_path, "doc", ".png", check_disk=False),
    ]
    reserved = [PlannedOutput(tmp_path, "photo (1)", ".jpg")]  # an already queued job

    stems = plan_output_stems(planned, reserved)

    assert stems == ["photo (2)", "Photo (3)", "photo", "photo", "doc", "doc (1)"]


class GatedConverter:
    """png -> jpeg; each source waits for its own event, so the test decides the order."""

    def __init__(self):
        self.gates: dict[str, threading.Event] = {}

    def routes(self):
        return [Route("png", "jpeg")]

    def convert(self, source, destination, route, options, ctx):
        gate = self.gates.setdefault(source.parent.name, threading.Event())
        if not gate.wait(10):
            raise RuntimeError("never released")
        destination.write_bytes(source.parent.name.encode())


def test_same_output_name_is_decided_by_list_order_not_finish_order(qtbot, tmp_path):
    converter = GatedConverter()
    registry = FormatRegistry()
    registry.register_formats(BUILTIN_FORMATS)
    registry.register_converter(converter)
    window = MainWindow(registry, max_parallel_jobs=2)
    qtbot.addWidget(window)
    out = tmp_path / "out"
    out.mkdir()
    first = make_image(tmp_path / "first" / "photo.png")
    second = make_image(tmp_path / "second" / "photo.png")
    window.add_files([first, second])
    window.output_edit.setText(str(out))

    window.start_conversion()
    converter.gates.setdefault("second", threading.Event()).set()  # second finishes first
    qtbot.waitUntil(lambda: window.model.job_at(1).state is JobState.SUCCEEDED, timeout=TIMEOUT_MS)
    converter.gates.setdefault("first", threading.Event()).set()
    wait_done(qtbot, window)

    assert (out / "photo.jpg").read_bytes() == b"first"
    assert (out / "photo (1).jpg").read_bytes() == b"second"


# --- batch processing ------------------------------------------------------------------


def test_batch_with_one_failure_finishes_the_others(window, tmp_path, qtbot):
    window.add_files(
        [
            make_image(tmp_path / "a.png"),
            broken_png(tmp_path / "b.png"),
            make_image(tmp_path / "c.png"),
        ]
    )
    window.start_conversion()
    wait_done(qtbot, window)

    states = [job.state for job in window.model.jobs()]
    assert states == [JobState.SUCCEEDED, JobState.FAILED, JobState.SUCCEEDED]
    details = [window.model.index(row, Column.DETAILS).data() for row in range(3)]
    assert details[0].startswith("Saved as a.") and details[2].startswith("Saved as c.")
    assert "corrupted or unreadable" in details[1]


def test_files_waiting_or_converting_are_not_added_twice(window, tmp_path, qtbot):
    a = make_image(tmp_path / "a.png")
    window.add_files([a, a])
    assert window.model.rowCount() == 1
    assert "1 file(s) already in the list" in window.statusBar().currentMessage()

    window.start_conversion()
    window.add_files([a])  # queued or running: still not added
    assert window.model.rowCount() == 1

    wait_done(qtbot, window)
    window.add_files([a])  # finished: may be converted again
    assert window.model.rowCount() == 2


def test_queued_job_can_be_removed_and_never_runs(qtbot, tmp_path):
    converter = GatedConverter()
    registry = FormatRegistry()
    registry.register_formats(BUILTIN_FORMATS)
    registry.register_converter(converter)
    window = MainWindow(registry, max_parallel_jobs=1)
    qtbot.addWidget(window)
    window.add_files([make_image(tmp_path / "x" / "a.png"), make_image(tmp_path / "y" / "b.png")])
    window.start_conversion()
    assert [job.state for job in window.model.jobs()] == [JobState.RUNNING, JobState.QUEUED]

    window.table.selectRow(1)
    assert window.remove_button.isEnabled()
    window._remove_selected()
    window.table.selectRow(0)
    window._remove_selected()  # running: not removed

    assert [job.source.name for job in window.model.jobs()] == ["a.png"]
    converter.gates.setdefault("x", threading.Event()).set()
    wait_done(qtbot, window)
    assert window._queue.is_idle()
    assert "y" not in converter.gates  # convert() was never called for the removed job
    assert not list((tmp_path / "y").glob("*.jpg"))


# --- retry ------------------------------------------------------------------------------


def run_with_one_failure(window, tmp_path, qtbot, out=None):
    good = make_image(tmp_path / "good.png")
    bad = broken_png(tmp_path / "bad.png")
    window.add_files([good, bad])
    if out is not None:
        window.output_edit.setText(str(out))
    window.start_conversion()
    wait_done(qtbot, window)
    return good, bad


def test_retry_reruns_only_the_failed_job_with_its_original_settings(window, tmp_path, qtbot):
    out = tmp_path / "out"
    out.mkdir()
    good, bad = run_with_one_failure(window, tmp_path, qtbot, out)
    failed = window.model.job_at(1)
    original_request = failed.request
    make_image(bad)  # the user fixes the file
    window.output_edit.setText("")  # a later UI change must not affect the retry
    window.preserve_metadata_box.setChecked(True)

    window.table.clearSelection()
    assert window.retry_button.isEnabled()
    window.retry_failed()
    wait_done(qtbot, window)

    jobs = window.model.jobs()
    assert [job.attempt for job in jobs] == [1, 1, 2]
    assert [job.state for job in jobs] == [JobState.SUCCEEDED, JobState.FAILED, JobState.SUCCEEDED]
    retry = jobs[2]
    assert retry.request.output_dir == out == original_request.output_dir
    assert retry.request.options == original_request.options
    assert retry.request.preserve_metadata is original_request.preserve_metadata is False
    assert window.model.index(1, Column.STATUS).data() == "Failed (retried)"
    assert window.model.index(2, Column.FILE).data() == "bad.png (attempt 2)"
    assert sorted(p.name for p in out.iterdir()) == ["bad.jpg", "good.jpg"]  # good not rerun
    assert not window.retry_button.isEnabled()  # nothing left to retry
    # The retried attempt is history, not an open failure.
    assert "2 converted, 0 failed" in window.statusBar().currentMessage()


def test_target_list_signal_without_a_choice_is_ignored(window, tmp_path):
    window.add_files([make_image(tmp_path / "a.png")])
    window.target_combo.activated.emit(-1)  # e.g. an empty list
    assert window.model.job_at(0).target.id == "jpeg"


def test_retrying_twice_does_not_start_two_attempts(window, tmp_path, qtbot):
    _, bad = run_with_one_failure(window, tmp_path, qtbot)
    make_image(bad)

    window.retry_failed()
    window.retry_failed()  # the failed job is already marked as retried
    wait_done(qtbot, window)

    assert [job.attempt for job in window.model.jobs()] == [1, 1, 2]


def test_retry_after_the_source_disappeared(window, tmp_path, qtbot, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    _, bad = run_with_one_failure(window, tmp_path, qtbot)
    bad.unlink()

    window.retry_failed()

    assert window.model.rowCount() == 2  # no new attempt
    assert "no longer exists" in warnings[0]
    assert window.model.job_at(1).can_retry  # can be retried once the file is back


def test_retry_when_the_route_no_longer_applies(window, tmp_path, qtbot, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    _, bad = run_with_one_failure(window, tmp_path, qtbot)
    bad.write_text("now a text file")

    window.retry_failed()

    assert window.model.rowCount() == 2
    assert "not in a supported format" in warnings[0]


def test_retry_never_overwrites_an_earlier_result(window, tmp_path, qtbot):
    _, bad = run_with_one_failure(window, tmp_path, qtbot)
    target_ext = window.model.job_at(1).target.preferred_extension
    earlier = tmp_path / f"bad{target_ext}"
    earlier.write_bytes(b"an earlier result")
    make_image(bad)

    window.retry_failed()
    wait_done(qtbot, window)

    assert earlier.read_bytes() == b"an earlier result"
    assert window.model.job_at(2).result.output_path.name == f"bad (1){target_ext}"
    assert temp_files(tmp_path) == []


def test_successful_and_cancelled_jobs_cannot_be_retried(window, tmp_path, qtbot):
    window.add_files([make_image(tmp_path / "a.png")])
    window.start_conversion()
    wait_done(qtbot, window)
    window.table.selectRow(0)
    assert not window.retry_button.isEnabled()
    window.retry_failed()
    assert window.model.rowCount() == 1


def test_retry_of_multi_output_job_keeps_the_full_set_rules(window, tmp_path, qtbot):
    source = write_pdf(tmp_path / "doc.pdf", [(72, 72, (0, 0, 0))] * 2)
    original = source.read_bytes()
    source.write_bytes(b"%PDF-1.7\n" + b"\x00broken" * 20)
    window.add_files([source])
    window.start_conversion()
    wait_done(qtbot, window)
    assert window.model.job_at(0).state is JobState.FAILED
    assert not list(tmp_path.glob("*.png"))  # no partial set

    source.write_bytes(original)
    window.retry_failed()
    wait_done(qtbot, window)

    assert window.model.job_at(1).state is JobState.SUCCEEDED
    assert sorted(p.name for p in tmp_path.glob("*.png")) == [
        "doc_page_001.png",
        "doc_page_002.png",
    ]
