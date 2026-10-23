"""Real conversions with the FFmpeg installed on this machine (skipped if there is none).

All media fixtures are generated on the fly by FFmpeg's built-in test sources.
"""

import json
import subprocess
import threading
import time

import pytest
from PySide6.QtWidgets import QMessageBox

from fileconverter.conversion import runner
from fileconverter.core.errors import InvalidInputError, UnsupportedConversionError
from fileconverter.core.models import ConversionRequest, JobStatus
from fileconverter.gui.main_window import MainWindow
from fileconverter.infrastructure import process
from fileconverter.services.jobs import JobState
from helpers import temp_files

TIMEOUT_MS = 120_000


def ff(tools, *args):
    subprocess.run(
        [str(tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
        check=True,
        capture_output=True,
        timeout=120,
    )


def make_audio(tools, path, seconds=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    ff(tools, "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-ac", "2", path)
    return path


def make_video(tools, path, seconds=1, size="160x120", audio=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=15:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:a", "aac"]
    ff(tools, *args, "-c:v", "mpeg4", "-q:v", "5", "-shortest", path)
    return path


def streams(tools, path):
    out = subprocess.run(
        [str(tools.ffprobe), "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    return json.loads(out)["streams"]


def codecs(tools, path):
    return {s["codec_type"]: s["codec_name"] for s in streams(tools, path)}


def convert(registry, source, target, out_dir=None, **kwargs):
    request = ConversionRequest(source, target, out_dir or source.parent)
    return runner.run_job(request, registry, **kwargs)


@pytest.mark.parametrize(
    ("target", "codec"),
    [
        ("mp3", "mp3"),
        ("flac", "flac"),
        ("aac", "aac"),
        ("m4a", "aac"),  # M4A is the container, AAC the codec
        ("ogg", "vorbis"),
        ("opus", "opus"),
    ],
)
def test_audio_conversions(media_registry, ffmpeg_tools, tmp_path, target, codec):
    source = make_audio(ffmpeg_tools, tmp_path / "tone.wav")

    result = convert(media_registry, source, target)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert result.output_path.name == f"tone.{target}"
    assert codecs(ffmpeg_tools, result.output_path) == {"audio": codec}
    assert temp_files(tmp_path) == []


def test_lossy_to_wav(media_registry, ffmpeg_tools, tmp_path):
    source = make_audio(ffmpeg_tools, tmp_path / "tone.mp3")
    result = convert(media_registry, source, "wav")
    assert codecs(ffmpeg_tools, result.output_path) == {"audio": "pcm_s16le"}


@pytest.mark.parametrize(
    ("target", "video_codec", "audio_codec"),
    [
        ("mp4", "h264", "aac"),
        ("webm", "vp9", "opus"),
        ("mov", "h264", "aac"),
        ("avi", "mpeg4", "mp3"),
    ],
)
def test_video_conversions(
    media_registry, ffmpeg_tools, tmp_path, target, video_codec, audio_codec
):
    source = make_video(ffmpeg_tools, tmp_path / "clip.mkv")

    result = convert(media_registry, source, target)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert codecs(ffmpeg_tools, result.output_path) == {"video": video_codec, "audio": audio_codec}


def test_mp4_to_mkv(media_registry, ffmpeg_tools, tmp_path):
    source = make_video(ffmpeg_tools, tmp_path / "clip.mp4")
    result = convert(media_registry, source, "mkv")
    assert codecs(ffmpeg_tools, result.output_path) == {"video": "h264", "audio": "aac"}


def test_audio_is_extracted_from_video(media_registry, ffmpeg_tools, tmp_path):
    source = make_video(ffmpeg_tools, tmp_path / "clip.mp4")

    result = convert(media_registry, source, "mp3")

    assert codecs(ffmpeg_tools, result.output_path) == {"audio": "mp3"}
    assert any("video was dropped" in w for w in result.warnings)


def test_odd_frame_size_is_handled(media_registry, ffmpeg_tools, tmp_path):
    source = make_video(ffmpeg_tools, tmp_path / "odd.mkv", size="161x121")

    result = convert(media_registry, source, "mp4")

    video = next(s for s in streams(ffmpeg_tools, result.output_path) if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (160, 120)


def test_unicode_and_spaces_in_paths(media_registry, ffmpeg_tools, tmp_path):
    source = make_audio(ffmpeg_tools, tmp_path / "папка с пробелами" / "-тест файл ñ & co.wav")
    out_dir = tmp_path / "выход out"
    out_dir.mkdir()
    original = source.read_bytes()

    result = convert(media_registry, source, "mp3", out_dir)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert result.output_path == out_dir / "-тест файл ñ & co.mp3"
    assert source.read_bytes() == original


def test_audio_only_mp4_cannot_become_video(media_registry, ffmpeg_tools, tmp_path):
    source = tmp_path / "voice.mp4"
    ff(ffmpeg_tools, "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac", source)

    to_video = convert(media_registry, source, "mkv")
    to_audio = convert(media_registry, source, "mp3")

    assert isinstance(to_video.error, UnsupportedConversionError)
    assert "no video" in str(to_video.error)
    assert to_audio.status is JobStatus.SUCCEEDED
    assert temp_files(tmp_path) == []


def test_damaged_media_fails_cleanly(media_registry, tmp_path):
    fake = tmp_path / "broken.mp4"
    fake.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"garbage" * 200)

    result = convert(media_registry, fake, "mp3")

    assert isinstance(result.error, InvalidInputError)
    assert temp_files(tmp_path) == []
    assert [p.name for p in tmp_path.iterdir()] == ["broken.mp4"]


def test_cancelling_real_conversion_stops_ffmpeg(
    media_registry, ffmpeg_tools, tmp_path, monkeypatch
):
    source = make_video(ffmpeg_tools, tmp_path / "long.mkv", seconds=60, size="640x360")
    popens = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        popens.append(real_popen(*args, **kwargs))
        return popens[-1]

    monkeypatch.setattr(process.subprocess, "Popen", recording_popen)
    cancel = threading.Event()
    progress = []

    def on_progress(fraction):
        progress.append(fraction)
        if fraction is not None and 0 < fraction < 1:
            cancel.set()  # cancel as soon as encoding is visibly under way

    begin = time.monotonic()
    result = convert(media_registry, source, "webm", cancel_event=cancel, on_progress=on_progress)

    assert result.status is JobStatus.CANCELLED
    assert any(f is not None and 0 < f < 1 for f in progress)  # real percentage progress
    assert time.monotonic() - begin < 60
    assert all(p.poll() is not None for p in popens)  # no FFmpeg/ffprobe left running
    assert temp_files(tmp_path) == []
    assert [p.name for p in tmp_path.iterdir()] == ["long.mkv"]


def test_gui_offers_targets_by_media_kind(media_registry, ffmpeg_tools, tmp_path, qtbot):
    window = MainWindow(media_registry)
    qtbot.addWidget(window)
    window.add_files([make_audio(ffmpeg_tools, tmp_path / "a.wav")])
    window.table.selectRow(0)
    combo = window.target_combo
    audio_choices = {combo.itemData(i) for i in range(combo.count())}

    assert audio_choices == {"mp3", "flac", "aac", "m4a", "ogg", "opus"}

    window.add_files([make_video(ffmpeg_tools, tmp_path / "v.mkv")])
    window.table.selectRow(1)
    video_choices = {combo.itemData(i) for i in range(combo.count())}
    assert {"mp4", "webm", "mov", "avi", "mp3", "wav"} <= video_choices
    assert "mkv" not in video_choices and "png" not in video_choices


def test_gui_runs_several_media_jobs_without_clobbering(
    media_registry, ffmpeg_tools, tmp_path, qtbot, monkeypatch
):
    sources = [
        make_audio(ffmpeg_tools, tmp_path / "clip.wav"),
        make_audio(ffmpeg_tools, tmp_path / "clip.flac"),
        make_audio(ffmpeg_tools, tmp_path / "clip.ogg"),
    ]
    existing = tmp_path / "clip.mp3"
    existing.write_bytes(b"user's own file")
    window = MainWindow(media_registry, max_parallel_jobs=3)
    qtbot.addWidget(window)
    window.add_files(sources)
    window.target_combo.activated.emit(window.target_combo.findData("mp3"))

    window.start_conversion()
    qtbot.waitUntil(
        lambda: all(j.state is JobState.SUCCEEDED for j in window.model.jobs()), timeout=TIMEOUT_MS
    )

    outputs = sorted(j.result.output_path.name for j in window.model.jobs())
    assert outputs == ["clip (1).mp3", "clip (2).mp3", "clip (3).mp3"]
    assert existing.read_bytes() == b"user's own file"
    for job in window.model.jobs():
        assert codecs(ffmpeg_tools, job.result.output_path) == {"audio": "mp3"}
    assert temp_files(tmp_path) == []


def test_gui_explains_missing_ffmpeg(registry, ffmpeg_tools, tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: None)
    window = MainWindow(registry)  # registry built without FFmpeg
    qtbot.addWidget(window)

    window.add_files([make_audio(ffmpeg_tools, tmp_path / "a.wav")])

    job = window.model.job_at(0)
    assert job.state is JobState.UNSUPPORTED
    assert "FFmpeg" in job.problem and "restart" in job.problem
