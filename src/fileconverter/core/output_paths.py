"""Choosing output file names without ever overwriting an existing file."""

import os
import re
from collections.abc import Iterator
from pathlib import Path

from fileconverter.core.errors import OutputError

MAX_NAME_ATTEMPTS = 10_000
MAX_PART_LENGTH = 80

# Characters Windows does not allow in file names, plus ASCII control characters.
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}  # fmt: skip


def candidate_names(stem: str, extension: str) -> Iterator[str]:
    """Yield 'name.ext', 'name (1).ext', 'name (2).ext', ..."""
    yield f"{stem}{extension}"
    for number in range(1, MAX_NAME_ATTEMPTS):
        yield f"{stem} ({number}){extension}"


def choose_output_path(stem: str, output_dir: Path, extension: str) -> Path:
    """Return a path in `output_dir` that does not exist yet.

    Because the result never points to an existing file, it can never be the
    source file either. The runner still refuses to replace an existing file
    when moving the result into place, since another program may create a
    file with the same name in the meantime.
    """
    for name in candidate_names(stem, extension):
        candidate = output_dir / name
        # lexists() also treats a broken symlink as "taken".
        if not os.path.lexists(candidate):
            return candidate
    raise OutputError(f"Could not find a free file name for '{stem}' in '{output_dir}'.")


def safe_name_part(text: str, fallback: str) -> str:
    """Turn arbitrary text (e.g. a worksheet name) into a valid Windows file name part."""
    cleaned = _INVALID_CHARS.sub("_", text).strip().rstrip(". ")[:MAX_PART_LENGTH].rstrip(". ")
    if not cleaned:
        return fallback
    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned += "_"
    return cleaned


def make_unique(names: list[str]) -> list[str]:
    """Make names unique, ignoring case as Windows does: 'a', 'A' -> 'a', 'A_2'."""
    seen: set[str] = set()
    unique = []
    for name in names:
        candidate, number = name, 1
        while candidate.casefold() in seen:
            number += 1
            candidate = f"{name}_{number}"
        seen.add(candidate.casefold())
        unique.append(candidate)
    return unique
