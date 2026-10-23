"""Choosing output file names without ever overwriting an existing file."""

import os
from collections.abc import Iterator
from pathlib import Path

from fileconverter.core.errors import OutputError

MAX_NAME_ATTEMPTS = 10_000


def candidate_names(stem: str, extension: str) -> Iterator[str]:
    """Yield 'name.ext', 'name (1).ext', 'name (2).ext', ..."""
    yield f"{stem}{extension}"
    for number in range(1, MAX_NAME_ATTEMPTS):
        yield f"{stem} ({number}){extension}"


def choose_output_path(source: Path, output_dir: Path, extension: str) -> Path:
    """Return a path in `output_dir` that does not exist yet.

    Because the result never points to an existing file, it can never be the
    source file either. The runner still refuses to replace an existing file
    when moving the result into place, since another program may create a
    file with the same name in the meantime.
    """
    for name in candidate_names(source.stem, extension):
        candidate = output_dir / name
        # lexists() also treats a broken symlink as "taken".
        if not os.path.lexists(candidate):
            return candidate
    raise OutputError(f"Could not find a free file name for '{source.stem}' in '{output_dir}'.")
