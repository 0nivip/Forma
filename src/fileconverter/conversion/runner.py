"""Runs a single conversion job safely, whatever backend performs it.

Steps:
  1. Validate the source file(s) and output directory.
  2. Detect the real source format and find a converter for the requested target.
  3. Let the converter write into a temporary file (or, for routes producing several
     files, a temporary folder) inside the output directory.
  4. Verify the result, then rename it to a free name (never overwriting).
  5. On any failure or cancellation, delete the temporary file or folder.

Success is reported only after step 4 has completed for every output file. If
committing a multi-file result fails half-way, the files already committed by this
job are removed again, so an incomplete set is never presented as a success.

Temporary files: the runner deletes only the exact paths that mkstemp()/mkdtemp()
created for the current job. It never deletes files by name pattern, so a user's
file that merely looks like ours (".fileconverter-*.part") is never touched.
"""

import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

from fileconverter.core.converter import Converter, JobContext, OutputPart, ProgressCallback
from fileconverter.core.errors import (
    ConversionCancelled,
    ConversionError,
    InvalidInputError,
    OutputError,
    UnsupportedConversionError,
)
from fileconverter.core.formats import Format
from fileconverter.core.models import ConversionRequest, ConversionResult, JobStatus, Route
from fileconverter.core.output_paths import choose_output_path
from fileconverter.core.registry import FormatRegistry

logger = logging.getLogger(__name__)

TEMP_PREFIX = ".fileconverter-"
TEMP_SUFFIX = ".part"
TEMP_DIR_SUFFIX = ".partdir"
# How often to pick a new name if someone grabs ours right before the rename. Parallel
# jobs writing files with the same name into one folder can each cause one retry.
MAX_MOVE_ATTEMPTS = 100


def run_job(
    request: ConversionRequest,
    registry: FormatRegistry,
    cancel_event: threading.Event | None = None,
    on_progress: ProgressCallback | None = None,
) -> ConversionResult:
    """Execute one conversion. Never raises for conversion problems; returns a result instead."""
    ctx = JobContext(cancel_event, on_progress, request.preserve_metadata)
    temp_path: Path | None = None
    try:
        for source in request.sources:
            _check_source(source)
        _check_output_dir(request.output_dir)

        source_format = registry.detect_format(request.source)
        _warn_on_extension_mismatch(request.source, source_format, ctx)
        target_format = registry.get_format(request.target_format)
        route, converter = registry.find_route(source_format.id, target_format.id)
        options = route.resolve_options(request.options)
        if request.extra_sources:
            _check_extra_sources(request, route, converter, registry, ctx)

        ctx.raise_if_cancelled()
        ctx.report_progress(None)
        extension = target_format.preferred_extension
        if route.multi_output:
            temp_path = _create_temp_dir(request.output_dir)
            parts = converter.convert(request.source, temp_path, route, options, ctx)
            ctx.raise_if_cancelled()
            _check_parts_written(parts, temp_path)
            output_paths = _commit_parts(parts, request, extension)
        else:
            temp_path = _create_temp_file(request.output_dir)
            if request.extra_sources:
                converter.convert_group(request.sources, temp_path, route, options, ctx)
            else:
                converter.convert(request.source, temp_path, route, options, ctx)
            ctx.raise_if_cancelled()
            _check_output_written(temp_path)
            output_paths = [
                _move_into_place(temp_path, request.source.stem, request.output_dir, extension)
            ]
            temp_path = None  # now owned by the user; must not be deleted
        ctx.report_progress(1.0)
        logger.info("Converted '%s' -> %s", request.source, [str(p) for p in output_paths])
        return ConversionResult(
            request,
            JobStatus.SUCCEEDED,
            output_paths=tuple(output_paths),
            warnings=tuple(ctx.warnings),
        )

    except ConversionCancelled:
        logger.info("Cancelled conversion of '%s'", request.source)
        return ConversionResult(request, JobStatus.CANCELLED, warnings=tuple(ctx.warnings))
    except ConversionError as exc:
        # An expected failure (bad input, full disk, ...): no traceback, but the
        # underlying library error is kept for diagnosis.
        logger.info(
            "Conversion of '%s' to %s failed: %s: %s (cause: %r)",
            request.source,
            request.target_format,
            type(exc).__name__,
            exc,
            exc.__cause__,
        )
        return ConversionResult(request, JobStatus.FAILED, error=exc, warnings=tuple(ctx.warnings))
    except Exception as exc:
        # A bug or an unforeseen library error. Log the full traceback so it can be
        # diagnosed, but report it as a failed job instead of crashing the whole queue.
        logger.exception("Unexpected error while converting '%s'", request.source)
        error = ConversionError(f"Unexpected error: {exc}")
        error.__cause__ = exc
        return ConversionResult(
            request, JobStatus.FAILED, error=error, warnings=tuple(ctx.warnings)
        )
    finally:
        if temp_path is not None:
            _remove_temp_path(temp_path)


def _check_source(source: Path) -> None:
    if not source.exists():
        raise InvalidInputError(f"'{source}' does not exist.")
    if not source.is_file():
        raise InvalidInputError(f"'{source}' is not a file.")


def _check_output_dir(output_dir: Path) -> None:
    if not output_dir.is_dir():
        raise OutputError(f"Output folder '{output_dir}' does not exist.")


def _check_extra_sources(
    request: ConversionRequest,
    route: Route,
    converter: Converter,
    registry: FormatRegistry,
    ctx: JobContext,
) -> None:
    """Every combined source must be convertible by the same multi-input converter."""
    if not route.multi_input:
        raise UnsupportedConversionError(
            f"Several files cannot be combined into one {request.target_format.upper()} file."
        )
    for extra in request.extra_sources:
        detected = registry.detect_format(extra)
        _warn_on_extension_mismatch(extra, detected, ctx)
        _, extra_converter = registry.find_route(detected.id, request.target_format)
        if extra_converter is not converter:
            raise UnsupportedConversionError(
                f"'{extra.name}' cannot be combined with '{request.source.name}'."
            )


def _warn_on_extension_mismatch(source: Path, detected: Format, ctx: JobContext) -> None:
    if source.suffix.lower() not in detected.extensions:
        ctx.warn(
            f"'{source.name}' has the extension '{source.suffix}' "
            f"but actually contains {detected.display_name} data."
        )


def _create_temp_file(output_dir: Path) -> Path:
    # The temp file lives in the output folder so the final rename stays on the same
    # drive, which makes it a fast, atomic metadata operation instead of a copy.
    try:
        fd, name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX, dir=output_dir)
    except OSError as exc:
        raise OutputError(f"Cannot write to '{output_dir}': {exc.strerror or exc}") from exc
    os.close(fd)
    return Path(name)


def _create_temp_dir(output_dir: Path) -> Path:
    try:
        name = tempfile.mkdtemp(prefix=TEMP_PREFIX, suffix=TEMP_DIR_SUFFIX, dir=output_dir)
    except OSError as exc:
        raise OutputError(f"Cannot write to '{output_dir}': {exc.strerror or exc}") from exc
    return Path(name)


def _check_output_written(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise OutputError("The converter did not produce any output.")


def _check_parts_written(parts: list[OutputPart] | None, temp_dir: Path) -> None:
    if not parts:
        raise OutputError("The converter did not produce any output.")
    for part in parts:
        # Parts may legitimately be empty (e.g. the CSV of an empty worksheet).
        if part.path.parent != temp_dir or not part.path.is_file():
            raise OutputError(f"Missing output file '{part.path.name}'.")


def _commit_parts(
    parts: list[OutputPart], request: ConversionRequest, extension: str
) -> list[Path]:
    """Move every part to a free final name; on failure, undo the parts already moved."""
    committed: list[Path] = []
    try:
        for part in parts:
            stem = request.source.stem
            if part.name_suffix:
                stem = f"{stem}_{part.name_suffix}"
            committed.append(_move_into_place(part.path, stem, request.output_dir, extension))
    except BaseException:
        for path in committed:  # created by this job moments ago, never a user's file
            _remove_temp_path(path)
        raise
    return committed


def _move_into_place(temp_path: Path, stem: str, output_dir: Path, extension: str) -> Path:
    for _ in range(MAX_MOVE_ATTEMPTS):
        destination = choose_output_path(stem, output_dir, extension)
        try:
            _rename_no_replace(temp_path, destination)
        except FileExistsError:
            continue  # someone created this name in the meantime; pick the next free one
        except OSError as exc:
            raise OutputError(f"Cannot save '{destination.name}': {exc.strerror or exc}") from exc
        return destination
    raise OutputError(f"Could not find a free file name in '{output_dir}'.")


def _rename_no_replace(src: Path, dst: Path) -> None:
    """Rename that raises FileExistsError instead of replacing an existing file."""
    if os.name == "nt":
        # On Windows os.rename() already refuses to replace an existing file.
        os.rename(src, dst)
    else:
        _link_then_unlink(src, dst)


def _link_then_unlink(src: Path, dst: Path) -> None:
    # On POSIX os.rename() silently replaces; a hard link fails if dst exists.
    os.link(src, dst)
    # The output is committed at this point. If removing the temp name fails, a
    # leftover temp file is logged, but the conversion itself still succeeded.
    _remove_temp_path(src)


def _remove_temp_path(path: Path) -> None:
    """Delete a temp file, or a temp folder created by this job with its contents."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        # Not fatal for the job result, but never silent.
        logger.warning("Could not delete temporary path '%s'", path, exc_info=True)
