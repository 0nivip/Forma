from fileconverter.cli import main
from helpers import make_image


def test_convert_command_creates_file(tmp_path, capsys):
    source = make_image(tmp_path / "a.png")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    exit_code = main(["convert", str(source), "--to", "webp", "--out-dir", str(out_dir)])

    assert exit_code == 0
    assert (out_dir / "a.webp").is_file()
    assert "OK" in capsys.readouterr().out


def test_convert_command_reports_failure(tmp_path, capsys):
    text = tmp_path / "notes.txt"
    text.write_text("not an image")

    exit_code = main(["convert", str(text), "--to", "png"])

    assert exit_code == 1
    assert "FAILED" in capsys.readouterr().err


def test_formats_command_lists_targets(capsys):
    assert main(["formats"]) == 0
    assert "png" in capsys.readouterr().out
