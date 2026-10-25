"""Choosing output file names without ever overwriting an existing file."""

import os
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True)
class PlannedOutput:
    output_dir: Path
    stem: str  # wanted name without extension, usually the source file's stem
    extension: str  # e.g. ".jpg"
    # Single-output jobs: also skip names of files that already exist. Multi-output jobs
    # add suffixes per file ("_page_001"), so only names within the batch are compared.
    check_disk: bool = True


def plan_output_stems(
    planned: Sequence[PlannedOutput], reserved: Iterable[PlannedOutput] = ()
) -> list[str]:
    """Choose an output stem for each job in order: 'photo', 'photo (1)', ...

    Deterministic: when several jobs of a batch would produce the same file name, the
    earlier job in the list gets the plain name. Names in `reserved` (jobs already
    queued or running) and, for single-output jobs, existing files are skipped. The
    runner still refuses to overwrite anything when it finally saves the file.
    """
    taken = {_name_key(item.output_dir, item.stem, item.extension) for item in reserved}
    stems = []
    for item in planned:
        for name in candidate_names(item.stem, ""):
            key = _name_key(item.output_dir, name, item.extension)
            on_disk = item.check_disk and os.path.lexists(
                item.output_dir / f"{name}{item.extension}"
            )
            if key not in taken and not on_disk:
                taken.add(key)
                stems.append(name)
                break
        else:
            raise OutputError(f"Could not find a free file name for '{item.stem}'.")
    return stems


def _name_key(folder: Path, stem: str, extension: str) -> tuple[str, str]:
    # Windows file names ignore case; normcase also unifies slashes.
    return os.path.normcase(os.path.abspath(folder)), f"{stem}{extension}".casefold()


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
