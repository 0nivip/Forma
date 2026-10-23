"""Audio and video conversion with an external FFmpeg executable.

Containers and codecs are different things: M4A and MP4 are containers, AAC and
H.264 are codecs. Each target format below names its container (FFmpeg "muxer")
and a preference-ordered list of codecs; the first codec that the installed FFmpeg
build provides is used. A target whose container or codecs are missing is not
offered at all.
"""

import contextlib
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from fileconverter.core.converter import JobContext
from fileconverter.core.errors import (
    ConversionCancelled,
    ConversionError,
    InvalidInputError,
    MissingDependencyError,
    OutputError,
    UnsupportedConversionError,
)
from fileconverter.core.formats import AUDIO_FORMATS, VIDEO_FORMATS, FormatFamily
from fileconverter.core.models import Route
from fileconverter.core.registry import FormatRegistry
from fileconverter.infrastructure import ffmpeg
from fileconverter.infrastructure.ffmpeg import FFmpegTools, MediaInfo
from fileconverter.infrastructure.process import ProcessResult, run_process

logger = logging.getLogger(__name__)

# Time FFmpeg gets to stop after "q" before it is killed.
STOP_GRACE_PERIOD_S = 3.0
MAX_DETAIL_LENGTH = 160


@dataclass(frozen=True)
class Encoder:
    name: str  # FFmpeg encoder name, as listed by `ffmpeg -encoders`
    label: str  # human-readable codec name
    args: tuple[str, ...]


@dataclass(frozen=True)
class TargetProfile:
    muxer: str  # FFmpeg output format; given explicitly because temp files end in ".part"
    video: tuple[Encoder, ...]  # preference order; empty for audio-only targets
    audio: tuple[Encoder, ...]  # preference order
    extra_args: tuple[str, ...] = ()


# yuv420p is the pixel format that virtually every player can decode.
H264 = Encoder(
    "libx264",
    "H.264",
    ("-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p"),
)
MPEG4 = Encoder("mpeg4", "MPEG-4 Part 2", ("-c:v", "mpeg4", "-q:v", "3", "-pix_fmt", "yuv420p"))
VP9 = Encoder(
    "libvpx-vp9",
    "VP9",
    ("-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-pix_fmt", "yuv420p"),
)
VP8 = Encoder(
    "libvpx", "VP8", ("-c:v", "libvpx", "-crf", "10", "-b:v", "2M", "-pix_fmt", "yuv420p")
)
AAC = Encoder("aac", "AAC", ("-c:a", "aac", "-b:a", "192k"))
MP3 = Encoder("libmp3lame", "MP3", ("-c:a", "libmp3lame", "-q:a", "2"))
OPUS = Encoder("libopus", "Opus", ("-c:a", "libopus", "-b:a", "128k"))
VORBIS = Encoder("libvorbis", "Vorbis", ("-c:a", "libvorbis", "-q:a", "5"))
FLAC = Encoder("flac", "FLAC", ("-c:a", "flac"))
PCM = Encoder("pcm_s16le", "PCM 16-bit", ("-c:a", "pcm_s16le"))
AC3 = Encoder("ac3", "AC-3", ("-c:a", "ac3", "-b:a", "192k"))

FASTSTART = ("-movflags", "+faststart")  # index at the start: playback before full download

PROFILES: dict[str, TargetProfile] = {
    # video containers
    "mp4": TargetProfile("mp4", (H264, MPEG4), (AAC,), FASTSTART),
    "mkv": TargetProfile("matroska", (H264, VP9, MPEG4), (AAC, OPUS, VORBIS, FLAC)),
    "webm": TargetProfile("webm", (VP9, VP8), (OPUS, VORBIS)),  # WebM allows only these
    "mov": TargetProfile("mov", (H264, MPEG4), (AAC,), FASTSTART),
    "avi": TargetProfile("avi", (MPEG4,), (MP3, AC3)),
    # audio
    "mp3": TargetProfile("mp3", (), (MP3,)),
    "wav": TargetProfile("wav", (), (PCM,)),
    "flac": TargetProfile("flac", (), (FLAC,)),
    "aac": TargetProfile("adts", (), (AAC,)),  # raw AAC stream in ADTS framing
    "m4a": TargetProfile("ipod", (), (AAC,), FASTSTART),  # "ipod" = FFmpeg's M4A muxer
    "ogg": TargetProfile("ogg", (), (VORBIS, OPUS)),
    "opus": TargetProfile("opus", (), (OPUS,)),
}

# Video must have even width and height for yuv420p; round odd sizes down by one pixel.
EVEN_SIZE_FILTER = ("-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2")

LOSSY_TARGETS = {"mp3", "aac", "m4a", "ogg", "opus", "mp4", "mkv", "webm", "mov", "avi"}
NOTE_FIRST_TRACKS = "Only the first video and audio track are kept; subtitles are dropped."
NOTE_LOSSY = "Lossy format: the media is re-encoded and some quality is lost."

# Known FFmpeg error texts -> (error class, message for the user).
FAILURE_PATTERNS: tuple[tuple[str, type[ConversionError], str], ...] = (
    ("no space left on device", OutputError, "There is not enough free disk space."),
    ("permission denied", OutputError, "Permission was denied while reading or writing a file."),
    (
        "invalid data found when processing input",
        InvalidInputError,
        "The file is damaged or not a supported media file.",
    ),
    ("moov atom not found", InvalidInputError, "The file is incomplete or damaged."),
    ("unknown encoder", MissingDependencyError, "This FFmpeg build lacks a required encoder."),
    ("encoder not found", MissingDependencyError, "This FFmpeg build lacks a required encoder."),
    (
        "could not find tag for codec",
        UnsupportedConversionError,
        "The target format cannot store this kind of media.",
    ),
    (
        "not currently supported in container",
        UnsupportedConversionError,
        "The target format cannot store this kind of media.",
    ),
)


class MediaConverter:
    def __init__(self, tools: FFmpegTools) -> None:
        self._tools = tools
        # target id -> (video encoder or None, audio encoder) usable with this build
        self._encoders: dict[str, tuple[Encoder | None, Encoder]] = {}
        # target id -> why it is unavailable with this build
        self.unavailable: dict[str, str] = {}
        for target, profile in PROFILES.items():
            self._choose_encoders(target, profile)

    def _choose_encoders(self, target: str, profile: TargetProfile) -> None:
        def first_available(encoders: tuple[Encoder, ...]) -> Encoder | None:
            return next((e for e in encoders if e.name in self._tools.encoders), None)

        video = first_available(profile.video)
        audio = first_available(profile.audio)
        missing = []
        if profile.muxer not in self._tools.muxers:
            missing.append(f"the '{profile.muxer}' muxer")
        if profile.video and video is None:
            missing.append("a video encoder (" + " or ".join(e.name for e in profile.video) + ")")
        if audio is None:
            missing.append("an audio encoder (" + " or ".join(e.name for e in profile.audio) + ")")
        if missing:
            self.unavailable[target] = (
                f"The installed FFmpeg cannot create {target.upper()} files: it lacks "
                + " and ".join(missing)
                + "."
            )
        else:
            self._encoders[target] = (video, audio)

    def routes(self) -> Iterator[Route]:
        video_targets = [f for f in VIDEO_FORMATS if f.id in self._encoders]
        audio_targets = [f for f in AUDIO_FORMATS if f.id in self._encoders]
        for source in VIDEO_FORMATS + AUDIO_FORMATS:
            # Video sources: video containers first, then audio extraction.
            # Audio sources: audio targets only (there is no picture to put in a video).
            if source.family is FormatFamily.VIDEO:
                targets = video_targets + audio_targets
            else:
                targets = audio_targets
            for target in targets:
                if target.id != source.id:
                    yield Route(source.id, target.id, notes=self._notes(target.id))

    def _notes(self, target: str) -> tuple[str, ...]:
        notes = [NOTE_FIRST_TRACKS]
        if target in LOSSY_TARGETS:
            notes.append(NOTE_LOSSY)
        return tuple(notes)

    def convert(
        self,
        source: Path,
        destination: Path,
        route: Route,
        options: Mapping[str, int],
        ctx: JobContext,
    ) -> None:
        info = ffmpeg.probe_media(self._tools, source, ctx.is_cancelled)
        ctx.raise_if_cancelled()
        wants_video = bool(PROFILES[route.target].video)
        if wants_video and info.video_streams == 0:
            raise UnsupportedConversionError(
                f"'{source.name}' contains no video; choose an audio format instead."
            )
        if not wants_video and info.audio_streams == 0:
            raise UnsupportedConversionError(f"'{source.name}' contains no audio track.")
        self._warn_about_dropped_streams(info, wants_video, route.target, ctx)

        args = self.build_args(source, destination, route.target, info)
        progress = _ProgressTracker(info.duration)
        logger.debug("Running FFmpeg: %s", args)
        try:
            result = run_process(
                args,
                should_stop=ctx.is_cancelled,
                on_stdout_line=progress.feed,
                on_poll=lambda: ctx.report_progress(progress.fraction()),
                stop_input=b"q",
                grace_period=STOP_GRACE_PERIOD_S,
            )
        except OSError as exc:
            raise MissingDependencyError(f"Cannot run FFmpeg: {exc}") from exc
        if result.cancelled:
            raise ConversionCancelled()
        if result.returncode != 0:
            raise classify_failure(result, source)

        self._verify_output(destination, wants_video, ctx)

    def build_args(
        self, source: Path, destination: Path, target: str, info: MediaInfo
    ) -> list[str]:
        profile = PROFILES[target]
        video, audio = self._encoders[target]
        args = [
            str(self._tools.ffmpeg),
            "-hide_banner",
            "-nostats",
            "-loglevel", "error",
            "-y",  # the output is our own, already created temp file
            "-i", ffmpeg.file_arg(source),
        ]  # fmt: skip
        if video is not None:
            # "V" (capital) selects real video only, never cover art pictures.
            args += ["-map", "0:V:0", *video.args, *EVEN_SIZE_FILTER]
        if info.audio_streams:
            args += ["-map", "0:a:0", *audio.args]
        args += [*profile.extra_args, "-progress", "pipe:1"]
        args += ["-f", profile.muxer, ffmpeg.file_arg(destination)]
        return args

    def _warn_about_dropped_streams(
        self, info: MediaInfo, wants_video: bool, target: str, ctx: JobContext
    ) -> None:
        video, _ = self._encoders[target]
        if wants_video and video is not None and video is not PROFILES[target].video[0]:
            ctx.warn(
                f"{PROFILES[target].video[0].label} encoding is not available in this FFmpeg "
                f"build; {video.label} was used instead."
            )
        if wants_video and info.video_streams > 1:
            ctx.warn("Only the first of several video tracks was kept.")
        if info.audio_streams > 1:
            ctx.warn("Only the first of several audio tracks was kept.")
        if info.subtitle_streams:
            ctx.warn("Subtitles were not copied.")
        if not wants_video and info.video_streams:
            ctx.warn("The video was dropped; only the audio was converted.")

    def _verify_output(self, path: Path, wants_video: bool, ctx: JobContext) -> None:
        try:
            info = ffmpeg.probe_media(self._tools, path, ctx.is_cancelled)
        except InvalidInputError as exc:
            raise OutputError("FFmpeg did not produce a valid file.") from exc
        if wants_video and info.video_streams == 0:
            raise OutputError("The converted file contains no video.")
        if not wants_video and info.audio_streams == 0:
            raise OutputError("The converted file contains no audio.")


def register_media(registry: FormatRegistry, tools: FFmpegTools | None) -> None:
    """Add audio/video routes for the given FFmpeg build, or explain why there are none."""
    if tools is None:
        for fmt in VIDEO_FORMATS + AUDIO_FORMATS:
            registry.note_unavailable(fmt.id, ffmpeg.MISSING_MESSAGE)
        return
    converter = MediaConverter(tools)
    registry.register_converter(converter)
    for target, reason in converter.unavailable.items():
        registry.note_unavailable(target, reason)


def classify_failure(result: ProcessResult, source: Path) -> ConversionError:
    """Turn FFmpeg's exit status and stderr into a short, specific error."""
    logger.info(
        "FFmpeg failed for '%s' (exit code %s):\n%s", source, result.returncode, result.stderr_tail
    )
    lowered = result.stderr_tail.lower()
    for pattern, error_class, message in FAILURE_PATTERNS:
        if pattern in lowered:
            return error_class(message)
    last_line = next(
        (line.strip() for line in reversed(result.stderr_tail.splitlines()) if line.strip()), ""
    )
    detail = last_line[:MAX_DETAIL_LENGTH] or "no error message"
    return ConversionError(f"FFmpeg failed (exit code {result.returncode}): {detail}")


class _ProgressTracker:
    """Turns FFmpeg's `-progress` key=value lines into a 0..1 fraction."""

    def __init__(self, duration: float | None) -> None:
        self._duration_us = duration * 1_000_000 if duration else None
        self._out_time_us: int | None = None  # written by the reader thread only

    def feed(self, line: str) -> None:
        key, _, value = line.partition("=")
        # "out_time_ms" is, despite its name, also in microseconds.
        if key in ("out_time_us", "out_time_ms"):
            with contextlib.suppress(ValueError):  # "N/A" before the first frame
                self._out_time_us = int(value)

    def fraction(self) -> float | None:
        if self._duration_us is None or self._out_time_us is None:
            return None  # unknown duration: report indeterminate progress
        return min(max(self._out_time_us / self._duration_us, 0.0), 1.0)
