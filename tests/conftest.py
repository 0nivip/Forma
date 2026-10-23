import os

import pytest

from fileconverter.app import build_registry
from fileconverter.core.registry import FormatRegistry

# Run Qt without showing windows on screen; must be set before QApplication exists.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_user_dirs(tmp_path_factory, monkeypatch):
    """Point every per-user directory at a temp folder: tests never touch the real profile."""
    base = tmp_path_factory.mktemp("user")
    for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, str(base / name))
    return base


@pytest.fixture(autouse=True)
def no_blocking_dialogs(monkeypatch):
    """A modal dialog would block a test forever; answer 'No' unless a test overrides it."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: QMessageBox.StandardButton.Ok)


@pytest.fixture
def registry() -> FormatRegistry:
    """Image-only registry: independent of whether FFmpeg is installed on this machine."""
    return build_registry(None)


@pytest.fixture(scope="session")
def ffmpeg_tools():
    """The real FFmpeg on this machine, or skip the test if there is none."""
    from fileconverter.infrastructure.ffmpeg import find_ffmpeg

    tools = find_ffmpeg()
    if tools is None:
        pytest.skip("FFmpeg/ffprobe not installed")
    return tools


@pytest.fixture
def media_registry(ffmpeg_tools) -> FormatRegistry:
    return build_registry(ffmpeg_tools)
