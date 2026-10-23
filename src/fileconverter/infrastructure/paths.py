"""Per-user directories for settings and logs, following each OS's conventions.

None of these depend on the current working directory.
"""

import os
import sys
from collections.abc import Mapping
from pathlib import Path

APP_NAME = "FileConverter"


def settings_dir(platform: str = sys.platform, environ: Mapping[str, str] = os.environ) -> Path:
    """Windows: %APPDATA%\\FileConverter (roams with the user's profile)."""
    home = Path.home()
    if platform.startswith("win"):
        return _env_path(environ, "APPDATA", home / "AppData" / "Roaming") / APP_NAME
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    return _env_path(environ, "XDG_CONFIG_HOME", home / ".config") / APP_NAME


def log_dir(platform: str = sys.platform, environ: Mapping[str, str] = os.environ) -> Path:
    """Windows: %LOCALAPPDATA%\\FileConverter\\logs (machine-local; logs should not roam)."""
    home = Path.home()
    if platform.startswith("win"):
        return _env_path(environ, "LOCALAPPDATA", home / "AppData" / "Local") / APP_NAME / "logs"
    if platform == "darwin":
        return home / "Library" / "Logs" / APP_NAME
    return _env_path(environ, "XDG_STATE_HOME", home / ".local" / "state") / APP_NAME / "logs"


def _env_path(environ: Mapping[str, str], name: str, fallback: Path) -> Path:
    # A relative value would depend on the working directory, so it is ignored.
    value = environ.get(name, "")
    return Path(value) if value and Path(value).is_absolute() else fallback
