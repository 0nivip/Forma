"""Finding FFmpeg/ffprobe, asking what the installed build can do, and probing media.

FFmpeg is used as an external program, never linked into this application.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from fileconverter.core.errors import ConversionCancelled, InvalidInputError, MissingDependencyError
from fileconverter.infrastructure.process import run_process, run_quick

logger = logging.getLogger(__name__)

ENV_DIR = "FORMA_FFMPEG_DIR"  # optional override: a folder containing ffmpeg and ffprobe
QUERY_TIMEOUT_S = 30
PROBE_TIMEOUT_S = 60
_EXE = ".exe" if os.name == "nt" else ""

MISSING_MESSAGE = (
    "Audio and video conversion needs FFmpeg and ffprobe, which were not found. "
    "Install FFmpeg (see https://ffmpeg.org/download.html) so that both programs are "
    "on PATH, then restart the application."
)


@dataclass(frozen=True)
class FFmpegTools:
    ffmpeg: Path
    ffprobe: Path
    version: str  # first line of `ffmpeg -version`
    encoders: frozenset[str]
    muxers: frozenset[str]


@dataclass(frozen=True)
class MediaInfo:
    duration: float | None  # seconds; None when the container does not say
    video_streams: int  # real video, not cover art
    audio_streams: int
    subtitle_streams: int


# --- discovery ----------------------------------------------------------------


def find_ffmpeg(
    environ: Mapping[str, str] = os.environ, app_dir: Path | None = None
) -> FFmpegTools | None:
    """Locate a working ffmpeg + ffprobe pair, or return None.

    Search order: the FORMA_FFMPEG_DIR folder, an "ffmpeg" folder next to a packaged
    application (reserved for a future bundled build), then PATH.
    """
    if app_dir is None and getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).parent

    candidates: list[tuple[Path | None, Path | None]] = []
    if environ.get(ENV_DIR):
        candidates.append(_pair_in(Path(environ[ENV_DIR])))
    if app_dir is not None:
        candidates += [_pair_in(app_dir / "ffmpeg"), _pair_in(app_dir / "ffmpeg" / "bin")]
    candidates.append(_pair_on_path())

    for ffmpeg, ffprobe in candidates:
        if ffmpeg is not None and ffprobe is not None:
            tools = inspect_tools(ffmpeg, ffprobe)
            if tools is not None:
                logger.info("Using %s (%s, %s)", tools.version, tools.ffmpeg, tools.ffprobe)
                return tools
    logger.warning("FFmpeg/ffprobe not found; audio and video conversion is disabled")
    return None


def inspect_tools(ffmpeg: Path, ffprobe: Path) -> FFmpegTools | None:
    """Check that both programs run, and read which encoders and muxers exist."""
    try:
        version = _query(ffmpeg, "-version").splitlines()[0].strip()
        _query(ffprobe, "-version")
        encoders = parse_name_list(_query(ffmpeg, "-encoders"))
        muxers = parse_name_list(_query(ffmpeg, "-muxers"))
    except (OSError, subprocess.SubprocessError, IndexError, RuntimeError) as exc:
        logger.warning("Ignoring unusable FFmpeg at %s: %s", ffmpeg, exc)
        return None
    return FFmpegTools(ffmpeg, ffprobe, version, encoders, muxers)


def parse_name_list(text: str) -> frozenset[str]:
    """Names from `ffmpeg -encoders` / `-muxers`: the 2nd column after the '--' line."""
    names = set()
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            in_table = True
            continue
        parts = stripped.split()
        if in_table and len(parts) >= 2:
            names.add(parts[1])
    return frozenset(names)


def _pair_in(directory: Path) -> tuple[Path | None, Path | None]:
    ffmpeg, ffprobe = directory / f"ffmpeg{_EXE}", directory / f"ffprobe{_EXE}"
    return (ffmpeg if ffmpeg.is_file() else None, ffprobe if ffprobe.is_file() else None)


def _pair_on_path() -> tuple[Path | None, Path | None]:
    ffmpeg = _which("ffmpeg")
    if ffmpeg is None:
        return None, None
    sibling = ffmpeg.with_name(f"ffprobe{_EXE}")  # prefer the matching ffprobe
    return ffmpeg, sibling if sibling.is_file() else _which("ffprobe")


def _which(name: str) -> Path | None:
    found = shutil.which(name)
    if found is None:
        return None
    path = Path(found)
    # On Windows PATH lookup may also find .bat/.cmd wrappers. Those run through
    # cmd.exe, which re-parses arguments, so only real executables are accepted.
    if _EXE and path.suffix.lower() != _EXE:
        logger.warning("Ignoring %s: not an .exe file", path)
        return None
    return path


def _query(program: Path, flag: str) -> str:
    completed = run_quick([str(program), "-hide_banner", flag], timeout=QUERY_TIMEOUT_S)
    if completed.returncode != 0:
        raise RuntimeError(f"'{program.name} {flag}' exited with {completed.returncode}")
    return completed.stdout


# --- probing ------------------------------------------------------------------


def file_arg(path: Path) -> str:
    """Path as an FFmpeg input/output argument.

    The absolute path keeps names starting with '-' from looking like options, and
    the "file:" prefix keeps names like "http:x" or "concat:a|b" from being treated
    as network or special protocols.
    """
    return "file:" + str(path.absolute())


def probe_media(
    tools: FFmpegTools, path: Path, should_stop: Callable[[], bool] = lambda: False
) -> MediaInfo:
    """Read stream and duration information with ffprobe.

    Raises InvalidInputError if the file is not readable media, ConversionCancelled
    if `should_stop` became true, MissingDependencyError if ffprobe cannot start.
    """
    args = [
        str(tools.ffprobe),
        "-hide_banner",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        file_arg(path),
    ]  # fmt: skip
    output: list[str] = []  # filled by the reader thread; complete once run_process returns
    try:
        result = run_process(
            args, should_stop=should_stop, on_stdout_line=output.append, timeout=PROBE_TIMEOUT_S
        )
    except OSError as exc:
        raise MissingDependencyError(f"Cannot run ffprobe: {exc}") from exc
    if result.cancelled:
        raise ConversionCancelled()
    if result.timed_out:
        raise InvalidInputError(f"Inspecting '{path.name}' took too long; the file may be damaged.")
    if result.returncode != 0:
        logger.info("ffprobe failed for '%s': %s", path, result.stderr_tail)
        raise InvalidInputError(f"'{path.name}' is damaged or not a supported media file.")
    return parse_probe_json("\n".join(output), path.name)


def parse_probe_json(text: str, name: str) -> MediaInfo:
    try:
        data = json.loads(text)
        streams = data.get("streams", [])
        container = data.get("format", {})
    except (ValueError, AttributeError) as exc:
        raise InvalidInputError(f"'{name}' could not be inspected.") from exc

    video = audio = subtitles = 0
    durations = []
    for stream in streams:
        kind = stream.get("codec_type")
        is_cover_art = stream.get("disposition", {}).get("attached_pic") == 1
        if kind == "video" and not is_cover_art:
            video += 1
        elif kind == "audio":
            audio += 1
        elif kind == "subtitle":
            subtitles += 1
        durations.append(_seconds(stream.get("duration")))

    duration = _seconds(container.get("duration")) or max(filter(None, durations), default=None)
    if video == 0 and audio == 0:
        raise InvalidInputError(f"'{name}' contains no audio or video.")
    return MediaInfo(duration, video, audio, subtitles)


def _seconds(value: object) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None  # missing or "N/A"
    return seconds if seconds > 0 else None
