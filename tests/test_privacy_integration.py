"""'Preserve metadata' and CSV options through the GUI, the CLI and a real FFmpeg."""

import json
import subprocess

import pytest
from PIL import Image

from fileconverter.cli import main
from fileconverter.conversion import runner
from fileconverter.conversion.backends.spreadsheets import (
    DECIMAL_OPTION,
    DELIMITER_OPTION,
    FORMULAS_OPTION,
)
from fileconverter.core.models import ConversionRequest, JobStatus
from fileconverter.gui.main_window import MainWindow
from fileconverter.infrastructure.settings import Settings, SettingsStore
from fileconverter.services.jobs import JobState
from test_metadata import MAKE, make_tagged
from test_spreadsheets import make_workbook

TIMEOUT_MS = 60_000


def wait_done(qtbot, window):
    qtbot.waitUntil(
        lambda: all(job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)
                    for job in window.model.jobs()),
        timeout=TIMEOUT_MS,
    )  # fmt: skip


def has_make(path):
    with Image.open(path) as image:
        return image.getexif().get(MAKE) == "SecretCam"


# --- GUI ------------------------------------------------------------------------


def test_checkbox_is_off_by_default_and_saved(registry, qtbot, tmp_path):
    store = SettingsStore(tmp_path / "cfg" / "settings.json")
    window = MainWindow(registry, store)
    qtbot.addWidget(window)

    assert window.preserve_metadata_box.isChecked() is False
    assert "personal information" in window.preserve_metadata_box.text()
    assert "Some format-specific information may remain" in window.metadata_help.text()

    window.preserve_metadata_box.setChecked(True)  # saved right away, not only on close
    assert store.load().preserve_metadata is True

    reopened = MainWindow(registry, store)
    qtbot.addWidget(reopened)
    assert reopened.preserve_metadata_box.isChecked() is True


def test_restoring_settings_does_not_overwrite_them(registry, qtbot, tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(output_dir=str(tmp_path), preserve_metadata=True, window_geometry="x"))

    window = MainWindow(registry, store)
    qtbot.addWidget(window)

    assert store.load().window_geometry == "x"  # restore did not trigger a save


def test_setting_applies_to_next_conversion_without_restart(registry, qtbot, tmp_path):
    first = make_tagged(tmp_path / "a.jpg", "JPEG")
    second = make_tagged(tmp_path / "b.jpg", "JPEG")
    window = MainWindow(registry)
    qtbot.addWidget(window)

    window.add_files([first])
    window.start_conversion()
    wait_done(qtbot, window)
    window.preserve_metadata_box.setChecked(True)
    window.add_files([second])
    window.start_conversion()
    wait_done(qtbot, window)

    stripped, kept = (job.result for job in window.model.jobs())
    assert stripped.request.preserve_metadata is False and kept.request.preserve_metadata
    assert not has_make(stripped.output_path)
    assert has_make(kept.output_path)


def test_csv_options_in_gui_use_the_shared_defaults(registry, qtbot, tmp_path):
    window = MainWindow(registry)
    qtbot.addWidget(window)
    window.add_files([make_workbook(tmp_path / "book.xlsx", {"S": [["=1+1", 2]]})])
    window.table.selectRow(0)

    delimiter = window.option_widgets["delimiter"]
    decimal = window.option_widgets["decimal"]
    formulas = window.option_widgets["formulas"]
    assert [decimal.itemText(i) for i in range(decimal.count())] == ["Comma (,)", "Dot (.)"]
    assert decimal.currentData() == DECIMAL_OPTION.default == "comma"
    assert "Text cells, dates and formulas" in decimal.toolTip()
    assert [delimiter.itemText(i) for i in range(delimiter.count())] == [
        "Semicolon (;)",
        "Comma (,)",
        "Tab",
    ]
    assert delimiter.currentData() == DELIMITER_OPTION.default == "semicolon"
    assert formulas.currentData() == FORMULAS_OPTION.default == "protect"
    assert "'" in formulas.toolTip()

    delimiter.setCurrentIndex(delimiter.findData("tab"))
    window.start_conversion()
    wait_done(qtbot, window)

    output = window.model.job_at(0).result.output_path
    assert output.read_bytes().decode("utf-8") == "﻿'=1+1\t2\r\n"


# --- CLI ------------------------------------------------------------------------


def test_cli_csv_defaults_match_gui_defaults(tmp_path):
    source = make_workbook(tmp_path / "book.xlsx", {"S": [["=1+1", "a;b"]]})

    assert main(["convert", str(source), "--to", "csv"]) == 0

    assert (tmp_path / "book.csv").read_bytes().decode("utf-8") == '﻿\'=1+1;"a;b"\r\n'


def test_cli_csv_options(tmp_path):
    source = make_workbook(tmp_path / "book.xlsx", {"S": [["=1+1", "a;b"]]})
    args = ["convert", str(source), "--to", "csv", "--delimiter", "comma", "--formulas", "raw"]

    assert main(args) == 0

    assert (tmp_path / "book.csv").read_bytes().decode("utf-8") == "﻿=1+1,a;b\r\n"


def test_gui_decimal_choice_is_used(registry, qtbot, tmp_path):
    window = MainWindow(registry)
    qtbot.addWidget(window)
    window.add_files([make_workbook(tmp_path / "book.xlsx", {"S": [[1.5, "1.5"]]})])
    window.table.selectRow(0)
    decimal = window.option_widgets["decimal"]

    decimal.setCurrentIndex(decimal.findData("dot"))
    window.start_conversion()
    wait_done(qtbot, window)

    output = window.model.job_at(0).result.output_path
    assert output.read_bytes().decode("utf-8") == "﻿1.5;1.5\r\n"


def test_cli_and_gui_offer_the_same_decimal_choices(capsys):
    with pytest.raises(SystemExit):
        main(["convert", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())  # undo argparse line wrapping
    choices = ",".join(value for value, _ in DECIMAL_OPTION.choices)
    assert f"--decimal {{{choices}}}" in help_text  # same values as the GUI combo box
    assert f"(default: {DECIMAL_OPTION.default})" in help_text


def test_cli_decimal_default_and_option(tmp_path):
    source = make_workbook(tmp_path / "book.xlsx", {"S": [[1.5, -2.25, 3, "1.5"]]})

    assert main(["convert", str(source), "--to", "csv"]) == 0
    assert main(["convert", str(source), "--to", "csv", "--decimal", "dot"]) == 0

    assert (tmp_path / "book.csv").read_bytes().decode("utf-8") == "﻿1,5;-2,25;3;1.5\r\n"
    assert (tmp_path / "book (1).csv").read_bytes().decode("utf-8") == ("﻿1.5;-2.25;3;1.5\r\n")


def test_cli_rejects_unknown_decimal(tmp_path, capsys):
    source = make_workbook(tmp_path / "book.xlsx", {"S": [[1.5]]})
    with pytest.raises(SystemExit):
        main(["convert", str(source), "--to", "csv", "--decimal", "space"])
    assert "invalid choice" in capsys.readouterr().err


def test_cli_rejects_unknown_delimiter(tmp_path, capsys):
    source = make_workbook(tmp_path / "book.xlsx", {"S": [[1]]})
    with pytest.raises(SystemExit):
        main(["convert", str(source), "--to", "csv", "--delimiter", "pipe"])
    assert "invalid choice" in capsys.readouterr().err


def test_cli_preserve_metadata_flag(tmp_path):
    source = make_tagged(tmp_path / "a.jpg", "JPEG")

    assert main(["convert", str(source), "--to", "png"]) == 0
    assert main(["convert", str(source), "--to", "png", "--preserve-metadata"]) == 0

    assert not has_make(tmp_path / "a.png")
    assert has_make(tmp_path / "a (1).png")


# --- real FFmpeg: global tags, stream tags and chapters ----------------------------


@pytest.fixture
def tagged_media(ffmpeg_tools, tmp_path):
    meta = tmp_path / "meta.txt"
    meta.write_text(
        ";FFMETADATA1\ntitle=SecretTitle\nartist=SecretArtist\nlocation=+48.1+011.5/\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\ntitle=SecretChapter\n",
        encoding="utf-8",
    )
    source = tmp_path / "clip.mkv"
    subprocess.run(
        [str(ffmpeg_tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=64x48:duration=1",
         "-f", "lavfi", "-i", "sine=duration=1", "-i", str(meta),
         "-map", "0", "-map", "1", "-map_metadata", "2", "-map_chapters", "2",
         "-metadata:s:a:0", "title=SecretStreamTitle", "-c:v", "mpeg4", "-c:a", "aac",
         "-shortest", str(source)],
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    return source


def media_markers(tools, path):
    out = subprocess.run(
        [str(tools.ffprobe), "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", "-show_chapters", str(path)],
        check=True, capture_output=True, timeout=60,
    ).stdout  # fmt: skip
    text = out.decode("utf-8")
    data = json.loads(text)
    return {
        "title": "SecretTitle" in text,
        "artist": "SecretArtist" in text,
        "location": "+48.1" in text,
        "stream_title": "SecretStreamTitle" in text,
        "chapters": len(data.get("chapters", [])),
    }


@pytest.mark.parametrize("target", ["mp4", "webm", "mp3", "m4a"])  # source is .mkv
def test_ffmpeg_removes_tags_and_chapters_by_default(
    media_registry, ffmpeg_tools, tagged_media, target
):
    request = ConversionRequest(tagged_media, target, tagged_media.parent)

    result = runner.run_job(request, media_registry)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert media_markers(ffmpeg_tools, result.output_path) == {
        "title": False,
        "artist": False,
        "location": False,
        "stream_title": False,
        "chapters": 0,
    }


@pytest.mark.parametrize("target", ["mp4", "webm", "mp3", "m4a"])  # source is .mkv
def test_ffmpeg_keeps_tags_and_chapters_when_preserving(
    media_registry, ffmpeg_tools, tagged_media, target
):
    request = ConversionRequest(tagged_media, target, tagged_media.parent, preserve_metadata=True)

    result = runner.run_job(request, media_registry)

    assert result.status is JobStatus.SUCCEEDED, result.error
    found = media_markers(ffmpeg_tools, result.output_path)
    assert found["title"] and found["artist"] and found["chapters"] == 1
