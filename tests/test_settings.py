import json

import pytest

from fileconverter.infrastructure import paths
from fileconverter.infrastructure.settings import MAX_FILE_BYTES, Settings, SettingsStore


@pytest.fixture
def store(tmp_path):
    return SettingsStore(tmp_path / "FileConverter" / "settings.json")


def test_missing_file_gives_defaults(store):
    assert store.load() == Settings()


def test_save_creates_directory_and_round_trips(store):
    saved = Settings(output_dir="C:\\Out", preferred_target="webp", window_geometry="AAAA")

    assert store.save(saved) is True
    assert store.load() == saved
    assert json.loads(store.path.read_text(encoding="utf-8"))["version"] == 1


def test_save_leaves_no_temp_files(store):
    store.save(Settings(output_dir="x"))
    store.save(Settings(output_dir="y"))
    assert [p.name for p in store.path.parent.iterdir()] == ["settings.json"]


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"", b"\xff\xfe\x00garbage", b"[1, 2, 3]", b'"just a string"'],
)
def test_malformed_file_gives_defaults(store, content):
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(content)
    assert store.load() == Settings()


def test_invalid_values_are_replaced_by_defaults(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"output_dir": 42, "preferred_target": "jpeg", "window_geometry": ["x"]}),
        encoding="utf-8",
    )
    assert store.load() == Settings(preferred_target="jpeg")


def test_outdated_file_keeps_known_fields(store):
    # Older/newer versions may lack fields or contain ones we do not know.
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"version": 0, "output_dir": "D:\\Old", "removed_option": True}),
        encoding="utf-8",
    )
    assert store.load() == Settings(output_dir="D:\\Old")


def test_oversized_file_is_ignored(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(" " * (MAX_FILE_BYTES + 1), encoding="utf-8")
    assert store.load() == Settings()


def test_unreadable_location_does_not_raise(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is expected")
    store = SettingsStore(blocker / "settings.json")

    assert store.load() == Settings()
    assert store.save(Settings(output_dir="x")) is False


def test_failed_save_keeps_previous_file(store, monkeypatch):
    store.save(Settings(output_dir="previous"))

    def broken_replace(*args):
        raise PermissionError("locked")

    monkeypatch.setattr("fileconverter.infrastructure.settings.os.replace", broken_replace)
    assert store.save(Settings(output_dir="new")) is False
    monkeypatch.undo()

    assert store.load().output_dir == "previous"
    assert [p.name for p in store.path.parent.iterdir()] == ["settings.json"]


def test_windows_paths_use_appdata_and_localappdata(tmp_path):
    env = {"APPDATA": str(tmp_path / "Roaming"), "LOCALAPPDATA": str(tmp_path / "Local")}
    assert paths.settings_dir("win32", env) == tmp_path / "Roaming" / "FileConverter"
    assert paths.log_dir("win32", env) == tmp_path / "Local" / "FileConverter" / "logs"


def test_relative_or_missing_env_values_fall_back_to_home():
    # A relative path would depend on the working directory, so it is not trusted.
    settings = paths.settings_dir("win32", {"APPDATA": "relative\\dir"})
    assert settings.is_absolute()
    assert settings.parts[-3:] == ("AppData", "Roaming", "FileConverter")


def test_linux_paths_follow_xdg(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "state")}
    assert paths.settings_dir("linux", env) == tmp_path / "cfg" / "FileConverter"
    assert paths.log_dir("linux", env) == tmp_path / "state" / "FileConverter" / "logs"
