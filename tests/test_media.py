"""Audio/video support without a real FFmpeg: detection, discovery, routes, arguments,
error mapping, progress and cancellation. FFmpeg is replaced by mocks."""

import threading
from pathlib import Path

import pytest

from fileconverter.app import build_registry
from fileconverter.conversion import runner
from fileconverter.conversion.backends import media
from fileconverter.conversion.backends.media import MediaConverter, classify_failure
from fileconverter.core.converter import JobContext
from fileconverter.core.errors import (
    ConversionError,
    InvalidInputError,
    MissingDependencyError,
    OutputError,
    UnsupportedConversionError,
)
from fileconverter.core.models import ConversionRequest, JobStatus
from fileconverter.infrastructure import ffmpeg
from fileconverter.infrastructure.ffmpeg import FFmpegTools, MediaInfo, parse_probe_json
from fileconverter.infrastructure.process import ProcessResult
from helpers import temp_files

ALL_ENCODERS = frozenset(
    {"libx264", "mpeg4", "libvpx-vp9", "libvpx", "aac", "libmp3lame", "libopus", "libvorbis"}
    | {"flac", "pcm_s16le", "ac3"}
)
ALL_MUXERS = frozenset(
    {"mp4", "matroska", "webm", "mov", "avi", "mp3", "wav", "flac", "adts", "ipod", "ogg", "opus"}
)
# Roughly what an LGPL build offers: no x264, no LAME.
LGPL_ENCODERS = ALL_ENCODERS - {"libx264", "libmp3lame"}

VIDEO_IDS = ["mp4", "mkv", "webm", "mov", "avi"]
AUDIO_IDS = ["mp3", "wav", "flac", "aac", "m4a", "ogg", "opus"]

HEADERS = {
    "mp4": b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41",
    "mov": b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  ",
    "m4a": b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00M4A mp42isom",
    "mkv": b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\x82\x88matroska",
    "webm": b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\x82\x84webm",
    "avi": b"RIFF\x00\x10\x00\x00AVI LIST",
    "wav": b"RIFF\x24\x00\x00\x00WAVEfmt ",
    "mp3": b"ID3\x04\x00\x00\x00\x00\x00\x00",
    "flac": b"fLaC\x00\x00\x00\x22",
    "aac": b"\xff\xf1\x50\x80\x02\x1f\xfc",
    "ogg": b"OggS\x00\x02" + b"\x00" * 22 + b"\x01vorbis",
    "opus": b"OggS\x00\x02" + b"\x00" * 22 + b"OpusHead\x01\x02",
}
EXTENSIONS = {"mkv": ".mkv", "webm": ".webm", "mov": ".mov", "avi": ".avi", "mp4": ".mp4"}


def fake_tools(encoders=ALL_ENCODERS, muxers=ALL_MUXERS) -> FFmpegTools:
    return FFmpegTools(
        Path("C:/ff/ffmpeg.exe"), Path("C:/ff/ffprobe.exe"), "ffmpeg test", encoders, muxers
    )


def media_file(folder: Path, name: str, kind: str) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(HEADERS[kind] + b"\x00" * 64)
    return path


# --- detection ----------------------------------------------------------------


@pytest.mark.parametrize("kind", list(HEADERS))
def test_media_containers_are_detected_by_content(registry, tmp_path, kind):
    path = media_file(tmp_path, "sample.bin", kind)
    assert registry.detect_format(path).id == kind


def test_media_extension_alone_is_not_trusted(registry, tmp_path):
    fake = tmp_path / "movie.mp4"
    fake.write_text("this is a text file pretending to be a video")
    with pytest.raises(ConversionError):
        registry.detect_format(fake)


# --- routes and capabilities --------------------------------------------------


def test_video_sources_offer_video_and_audio_targets():
    registry = build_registry(fake_tools())
    targets = [fmt.id for fmt in registry.targets_for("mp4")]
    assert targets == ["mkv", "webm", "mov", "avi", *AUDIO_IDS]  # video first, no MP4 itself


def test_audio_sources_offer_audio_targets_only():
    registry = build_registry(fake_tools())
    assert [fmt.id for fmt in registry.targets_for("flac")] == [
        "mp3", "wav", "aac", "m4a", "ogg", "opus"
    ]  # fmt: skip


def test_image_routes_are_unchanged_by_media_support():
    registry = build_registry(fake_tools())
    assert {fmt.id for fmt in registry.targets_for("png")} == {
        "jpeg",
        "webp",
        "avif",
        "gif",
        "bmp",
        "tiff",
        "ico",
        "pdf",
    }


def test_lgpl_like_build_uses_fallbacks_and_hides_impossible_targets():
    registry = build_registry(fake_tools(encoders=LGPL_ENCODERS))
    targets = {fmt.id for fmt in registry.targets_for("mkv")}

    assert "mp3" not in targets  # no MP3 encoder at all
    assert {"mp4", "avi", "webm"} <= targets  # MPEG-4 Part 2 / AC-3 / VP9 fallbacks
    assert "libmp3lame" in registry.unavailable_reason("mp3")
    with pytest.raises(MissingDependencyError, match="libmp3lame"):
        registry.find_route("wav", "mp3")


def test_missing_muxer_hides_target():
    registry = build_registry(fake_tools(muxers=ALL_MUXERS - {"webm"}))
    assert "webm" not in {fmt.id for fmt in registry.targets_for("mp4")}
    assert "muxer" in registry.unavailable_reason("webm")


def test_without_ffmpeg_media_files_explain_what_is_missing(tmp_path):
    registry = build_registry(None)
    source = media_file(tmp_path, "clip.mp4", "mp4")

    assert registry.targets_for("mp4") == []
    assert "FFmpeg" in registry.unavailable_reason("mp4")
    result = runner.run_job(ConversionRequest(source, "mp3", tmp_path), registry)
    assert isinstance(result.error, MissingDependencyError)
    assert "FFmpeg" in str(result.error)


# --- discovery ----------------------------------------------------------------


def make_exes(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (folder / name).write_bytes(b"")
    return folder


@pytest.fixture
def accept_any_tools(monkeypatch):
    """Skip running the (fake) executables: treat every found pair as working."""

    def inspect(ffmpeg_path, ffprobe_path):
        return FFmpegTools(ffmpeg_path, ffprobe_path, "ffmpeg test", ALL_ENCODERS, ALL_MUXERS)

    monkeypatch.setattr(ffmpeg, "inspect_tools", inspect)
    monkeypatch.setattr(ffmpeg, "_EXE", ".exe")


def test_discovery_prefers_env_folder(tmp_path, monkeypatch, accept_any_tools):
    folder = make_exes(tmp_path / "custom ffmpeg")
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)

    tools = ffmpeg.find_ffmpeg({"FORMA_FFMPEG_DIR": str(folder)})

    assert tools.ffmpeg == folder / "ffmpeg.exe"
    assert tools.ffprobe == folder / "ffprobe.exe"


def test_discovery_checks_folder_next_to_packaged_app(tmp_path, monkeypatch, accept_any_tools):
    make_exes(tmp_path / "app" / "ffmpeg" / "bin")
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)

    tools = ffmpeg.find_ffmpeg({}, app_dir=tmp_path / "app")

    assert tools.ffmpeg == tmp_path / "app" / "ffmpeg" / "bin" / "ffmpeg.exe"


def test_discovery_falls_back_to_path(tmp_path, monkeypatch, accept_any_tools):
    folder = make_exes(tmp_path / "on path")
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: str(folder / f"{name}.exe"))

    tools = ffmpeg.find_ffmpeg({})

    assert tools.ffmpeg == folder / "ffmpeg.exe"


def test_discovery_rejects_batch_file_wrappers(tmp_path, monkeypatch, accept_any_tools):
    # .bat/.cmd run through cmd.exe, which would re-parse file names as commands.
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: str(tmp_path / f"{name}.bat"))
    assert ffmpeg.find_ffmpeg({}) is None


def test_discovery_requires_ffprobe_too(tmp_path, monkeypatch, accept_any_tools):
    folder = tmp_path / "half"
    folder.mkdir()
    (folder / "ffmpeg.exe").write_bytes(b"")
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)

    assert ffmpeg.find_ffmpeg({"FORMA_FFMPEG_DIR": str(folder)}) is None


def test_unusable_executable_is_ignored(tmp_path):
    folder = tmp_path / "broken"
    folder.mkdir()
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (folder / name).write_text("not a program")

    assert ffmpeg.inspect_tools(folder / "ffmpeg.exe", folder / "ffprobe.exe") is None


def test_encoder_and_muxer_lists_are_parsed():
    text = """Encoders:
 V..... = Video
 ------
 V....D libx264              libx264 H.264 / AVC
 A....D aac                  AAC (Advanced Audio Coding)
 A..X.D opus                 Opus (experimental)
"""
    assert ffmpeg.parse_name_list(text) == {"libx264", "aac", "opus"}


# --- ffprobe output -----------------------------------------------------------


def test_probe_counts_streams_and_ignores_cover_art():
    info = parse_probe_json(
        """{"streams": [
            {"codec_type": "audio", "duration": "3.5"},
            {"codec_type": "video", "disposition": {"attached_pic": 1}},
            {"codec_type": "subtitle"}],
           "format": {"duration": "3.500000"}}""",
        "song.mp3",
    )
    assert info == MediaInfo(duration=3.5, video_streams=0, audio_streams=1, subtitle_streams=1)


def test_probe_unknown_duration():
    info = parse_probe_json(
        '{"streams": [{"codec_type": "audio", "duration": "N/A"}], "format": {}}', "x"
    )
    assert info.duration is None


@pytest.mark.parametrize(
    "text", ["not json", '{"streams": [], "format": {}}', '{"streams": [{"codec_type": "data"}]}']
)
def test_probe_rejects_non_media(text):
    with pytest.raises(InvalidInputError):
        parse_probe_json(text, "x")


# --- FFmpeg arguments ---------------------------------------------------------


def test_arguments_keep_unusual_paths_intact(tmp_path):
    converter = MediaConverter(fake_tools())
    source = tmp_path / "папка с пробелами" / "-клип ñ & co.mkv"
    dest = tmp_path / "out dir" / ".fileconverter-abc.part"

    args = converter.build_args(source, dest, "mp4", MediaInfo(1.0, 1, 1, 0))

    assert f"file:{source.absolute()}" in args  # one argument, protected from option parsing
    assert args[-1] == f"file:{dest.absolute()}"
    assert args[-3:-1] == ["-f", "mp4"]  # container set explicitly: ".part" says nothing
    assert "libx264" in args and "aac" in args
    assert "0:V:0" in args  # never maps cover art as video


def test_audio_target_maps_audio_only(tmp_path):
    converter = MediaConverter(fake_tools())
    args = converter.build_args(
        tmp_path / "a.mp4", tmp_path / "o.part", "mp3", MediaInfo(1, 1, 1, 0)
    )
    assert "0:a:0" in args and "0:V:0" not in args
    assert args[-3:-1] == ["-f", "mp3"]


def test_m4a_uses_aac_codec_in_m4a_container(tmp_path):
    converter = MediaConverter(fake_tools())
    args = converter.build_args(
        tmp_path / "a.wav", tmp_path / "o.part", "m4a", MediaInfo(1, 0, 1, 0)
    )
    assert args[args.index("-c:a") + 1] == "aac"
    assert args[-3:-1] == ["-f", "ipod"]  # FFmpeg's name for the M4A muxer


def test_silent_video_maps_no_audio(tmp_path):
    converter = MediaConverter(fake_tools())
    args = converter.build_args(
        tmp_path / "a.mp4", tmp_path / "o.part", "webm", MediaInfo(1, 1, 0, 0)
    )
    assert "0:a:0" not in args and "libvpx-vp9" in args


# --- error mapping ------------------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "error_class"),
    [
        ("av_interleaved_write_frame(): No space left on device", OutputError),
        ("file:C:\\out\\x.part: Permission denied", OutputError),
        ("file:C:\\in.mp4: Invalid data found when processing input", InvalidInputError),
        ("[mov,mp4] moov atom not found", InvalidInputError),
        ("Unknown encoder 'libx264'", MissingDependencyError),
        ("Could not find tag for codec vp9 in stream #0", UnsupportedConversionError),
    ],
)
def test_ffmpeg_failures_are_classified(stderr, error_class):
    error = classify_failure(ProcessResult(1, stderr), Path("in.mp4"))
    assert type(error) is error_class


def test_unknown_failure_keeps_a_short_detail():
    error = classify_failure(ProcessResult(1, "noise\n" + "X" * 1000), Path("in.mp4"))
    assert type(error) is ConversionError
    assert "exit code 1" in str(error)
    assert len(str(error)) < 250  # never a whole FFmpeg log in the GUI


# --- conversion flow with mocked FFmpeg ----------------------------------------


class FakeFFmpeg:
    """Replaces probe_media and run_process inside the media backend."""

    def __init__(self, monkeypatch, info=None, result=None, output=b"media"):
        self.info = info or MediaInfo(10.0, 1, 1, 0)
        self.result = result or ProcessResult(0, "")
        self.output = output
        self.progress_lines = ["out_time_us=2500000", "out_time_us=N/A", "out_time_us=5000000"]
        self.calls = []
        monkeypatch.setattr(media.ffmpeg, "probe_media", self.probe)
        monkeypatch.setattr(media, "run_process", self.run)

    def probe(self, tools, path, should_stop=lambda: False):
        if isinstance(self.info, Exception):
            raise self.info
        return self.info

    def run(self, args, *, should_stop, on_stdout_line, on_poll, stop_input, grace_period):
        self.calls.append(args)
        if isinstance(self.result, Exception):
            raise self.result
        destination = Path(args[-1].removeprefix("file:"))
        destination.write_bytes(self.output)  # FFmpeg writes into the temp file
        for line in self.progress_lines:
            on_stdout_line(line)
            on_poll()
        return self.result


@pytest.fixture
def mocked_registry():
    return build_registry(fake_tools())


def run(registry, source, target, **kwargs):
    return runner.run_job(ConversionRequest(source, target, source.parent), registry, **kwargs)


def test_successful_conversion_reports_progress(mocked_registry, tmp_path, monkeypatch):
    FakeFFmpeg(monkeypatch)
    source = media_file(tmp_path, "clip.mkv", "mkv")
    progress = []

    result = run(mocked_registry, source, "mp4", on_progress=progress.append)

    assert result.status is JobStatus.SUCCEEDED
    assert result.output_path == tmp_path / "clip.mp4"
    fractions = [p for p in progress if p not in (None, 1.0)]
    assert sorted(set(fractions)) == [0.25, 0.5]  # "N/A" just repeats the last known value
    assert fractions == sorted(fractions)  # never goes backwards
    assert progress[-1] == 1.0


def test_unknown_duration_reports_indeterminate_progress(mocked_registry, tmp_path, monkeypatch):
    FakeFFmpeg(monkeypatch, info=MediaInfo(None, 0, 1, 0))
    progress = []

    run(mocked_registry, media_file(tmp_path, "a.wav", "wav"), "mp3", on_progress=progress.append)

    assert set(progress[:-1]) == {None}


def test_ffmpeg_failure_cleans_up_and_reports(mocked_registry, tmp_path, monkeypatch):
    FakeFFmpeg(monkeypatch, result=ProcessResult(1, "Error: No space left on device"))
    source = media_file(tmp_path, "a.wav", "wav")

    result = run(mocked_registry, source, "flac")

    assert isinstance(result.error, OutputError)
    assert "disk space" in str(result.error)
    assert temp_files(tmp_path) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.wav"]


def test_ffmpeg_that_cannot_start_is_a_missing_dependency(mocked_registry, tmp_path, monkeypatch):
    FakeFFmpeg(monkeypatch, result=FileNotFoundError("ffmpeg.exe"))
    result = run(mocked_registry, media_file(tmp_path, "a.wav", "wav"), "mp3")
    assert isinstance(result.error, MissingDependencyError)
    assert temp_files(tmp_path) == []


def test_cancelled_ffmpeg_is_reported_as_cancelled(mocked_registry, tmp_path, monkeypatch):
    FakeFFmpeg(monkeypatch, result=ProcessResult(None, "", cancelled=True))
    source = media_file(tmp_path, "a.mkv", "mkv")

    result = run(mocked_registry, source, "webm")

    assert result.status is JobStatus.CANCELLED
    assert temp_files(tmp_path) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.mkv"]


def test_cancel_before_ffmpeg_starts_never_runs_it(mocked_registry, tmp_path, monkeypatch):
    fake = FakeFFmpeg(monkeypatch)
    cancel = threading.Event()
    cancel.set()

    result = run(mocked_registry, media_file(tmp_path, "a.wav", "wav"), "mp3", cancel_event=cancel)

    assert result.status is JobStatus.CANCELLED
    assert fake.calls == []


def test_video_target_for_audio_only_file_is_refused(mocked_registry, tmp_path, monkeypatch):
    # An MP4 that holds only audio: MP4 -> MKV is offered, but there is no picture.
    fake = FakeFFmpeg(monkeypatch, info=MediaInfo(5.0, 0, 1, 0))

    result = run(mocked_registry, media_file(tmp_path, "voice.mp4", "mp4"), "mkv")

    assert isinstance(result.error, UnsupportedConversionError)
    assert "no video" in str(result.error)
    assert fake.calls == []


def test_damaged_media_is_reported(mocked_registry, tmp_path, monkeypatch):
    FakeFFmpeg(
        monkeypatch, info=InvalidInputError("'a.mp4' is damaged or not a supported media file.")
    )
    result = run(mocked_registry, media_file(tmp_path, "a.mp4", "mp4"), "mp3")
    assert isinstance(result.error, InvalidInputError)


def test_output_without_expected_stream_is_not_a_success(mocked_registry, tmp_path, monkeypatch):
    fake = FakeFFmpeg(monkeypatch)
    source = media_file(tmp_path, "a.mkv", "mkv")
    probes = iter([MediaInfo(10.0, 1, 1, 0), MediaInfo(10.0, 0, 1, 0)])  # output lost video
    fake.probe = lambda *args, **kwargs: next(probes)
    monkeypatch.setattr(media.ffmpeg, "probe_media", fake.probe)

    result = run(mocked_registry, source, "mp4")

    assert isinstance(result.error, OutputError)
    assert temp_files(tmp_path) == []


def test_existing_output_and_source_are_preserved(mocked_registry, tmp_path, monkeypatch):
    FakeFFmpeg(monkeypatch)
    source = media_file(tmp_path, "song.wav", "wav")
    original = source.read_bytes()
    existing = tmp_path / "song.mp3"
    existing.write_bytes(b"keep me")

    result = run(mocked_registry, source, "mp3")

    assert result.output_path == tmp_path / "song (1).mp3"
    assert existing.read_bytes() == b"keep me"
    assert source.read_bytes() == original


def test_warnings_explain_dropped_streams(mocked_registry, tmp_path, monkeypatch):
    FakeFFmpeg(monkeypatch, info=MediaInfo(10.0, 1, 3, 2))
    result = run(mocked_registry, media_file(tmp_path, "film.mkv", "mkv"), "mp3")
    text = " ".join(result.warnings)
    assert "audio tracks" in text and "Subtitles" in text and "video was dropped" in text


def test_fallback_encoder_is_announced(tmp_path, monkeypatch):
    FakeFFmpeg(monkeypatch)
    registry = build_registry(fake_tools(encoders=LGPL_ENCODERS))

    result = run(registry, media_file(tmp_path, "a.mkv", "mkv"), "mp4")

    assert result.status is JobStatus.SUCCEEDED
    assert any("MPEG-4 Part 2 was used" in w for w in result.warnings)


def test_progress_is_reported_from_the_calling_thread(tmp_path, monkeypatch):
    # on_poll runs in the job's own thread, so progress never comes from a reader thread.
    FakeFFmpeg(monkeypatch)
    threads = set()
    ctx = JobContext(on_progress=lambda f: threads.add(threading.get_ident()))
    converter = MediaConverter(fake_tools())
    source = media_file(tmp_path, "a.wav", "wav")
    route = next(r for r in converter.routes() if (r.source, r.target) == ("wav", "mp3"))

    converter.convert(source, tmp_path / "out.part", route, {}, ctx)

    assert threads == {threading.get_ident()}
