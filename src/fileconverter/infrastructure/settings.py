"""User preferences stored as a small JSON file.

Loading and saving never raise: a missing, damaged or unwritable file only means
the defaults are used (or the preferences are not remembered), which is logged.
"""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

logger = logging.getLogger(__name__)

SETTINGS_FILE_NAME = "settings.json"
SETTINGS_VERSION = 1
MAX_FILE_BYTES = 64 * 1024  # a real settings file is a few hundred bytes
MAX_VALUE_LENGTH = 4096


@dataclass
class Settings:
    output_dir: str = ""  # "" means: next to each source file
    preferred_target: str = ""  # format id, e.g. "jpeg"; "" means no preference
    window_geometry: str = ""  # opaque base64 string produced by the GUI toolkit

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        """Keep valid known fields; ignore unknown keys and wrongly typed values."""
        values = {}
        for spec in fields(cls):
            value = data.get(spec.name)
            if isinstance(value, str) and len(value) <= MAX_VALUE_LENGTH:
                values[spec.name] = value
            elif value is not None:
                logger.warning("Ignoring invalid setting %r", spec.name)
        return cls(**values)


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Settings:
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                logger.warning("Settings file %s is too large; using defaults", self.path)
                return Settings()
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Settings()
        except (OSError, ValueError):  # unreadable, not UTF-8, or not valid JSON
            logger.warning("Cannot read settings from %s; using defaults", self.path, exc_info=True)
            return Settings()
        if not isinstance(data, dict):
            logger.warning(
                "Settings file %s has an unexpected structure; using defaults", self.path
            )
            return Settings()
        return Settings.from_dict(data)

    def save(self, settings: Settings) -> bool:
        """Write atomically: a crash mid-write leaves the previous file intact."""
        text = json.dumps({"version": SETTINGS_VERSION, **asdict(settings)}, indent=2)
        temp_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-", suffix=".tmp")
            temp_path = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            # Replacing is intended here: this is our own file, not user data.
            os.replace(temp_path, self.path)
            return True
        except OSError:
            logger.warning("Cannot save settings to %s", self.path, exc_info=True)
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            return False
