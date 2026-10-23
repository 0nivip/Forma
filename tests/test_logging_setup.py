import logging
import sys
from logging.handlers import RotatingFileHandler

import pytest

from fileconverter.infrastructure.logging_setup import (
    BACKUP_COUNT,
    FILE_HANDLER_NAME,
    LOG_FILE_NAME,
    MAX_LOG_BYTES,
    log_unhandled_exception,
    setup_file_logging,
)


def file_handlers():
    return [h for h in logging.getLogger().handlers if h.get_name() == FILE_HANDLER_NAME]


@pytest.fixture(autouse=True)
def remove_file_handler():
    root = logging.getLogger()
    original_level = root.level
    yield
    for handler in file_handlers():
        logging.getLogger().removeHandler(handler)
        handler.close()
    root.setLevel(original_level)


def test_creates_directory_and_writes_formatted_records(tmp_path):
    log_dir = tmp_path / "logs"

    path = setup_file_logging(log_dir)
    logging.getLogger("fileconverter.test").warning("disk is %s", "full")
    file_handlers()[0].flush()

    assert path == log_dir / LOG_FILE_NAME
    line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert "WARNING" in line and "fileconverter.test" in line and "disk is full" in line
    assert line[:4].isdigit()  # starts with a timestamp (year)


def test_rotation_is_configured(tmp_path):
    setup_file_logging(tmp_path)
    handler = file_handlers()[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == MAX_LOG_BYTES
    assert handler.backupCount == BACKUP_COUNT


def test_repeated_setup_does_not_duplicate_handlers(tmp_path):
    setup_file_logging(tmp_path)
    setup_file_logging(tmp_path)
    setup_file_logging(tmp_path / "other")

    assert len(file_handlers()) == 1
    logging.getLogger("fileconverter.test").warning("once")
    file_handlers()[0].flush()
    assert (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8").count("once") == 0
    assert (tmp_path / "other" / LOG_FILE_NAME).read_text(encoding="utf-8").count("once") == 1


def test_unusable_directory_does_not_raise(tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is expected")

    assert setup_file_logging(blocker / "logs") is None
    assert file_handlers() == []
    assert "file logging disabled" in capsys.readouterr().err


def test_unhandled_exception_is_logged_with_traceback(tmp_path, capsys):
    path = setup_file_logging(tmp_path)
    try:
        raise ValueError("boom in a slot")
    except ValueError:
        log_unhandled_exception(*sys.exc_info())
    file_handlers()[0].flush()

    text = path.read_text(encoding="utf-8")
    assert "CRITICAL" in text and "Unhandled exception" in text
    assert "Traceback" in text and "ValueError: boom in a slot" in text


def test_expected_conversion_failure_is_logged_without_traceback(tmp_path, registry):
    from fileconverter.conversion.runner import run_job
    from fileconverter.core.models import ConversionRequest

    path = setup_file_logging(tmp_path / "logs")
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 20)

    run_job(ConversionRequest(broken, "jpeg", tmp_path), registry)
    file_handlers()[0].flush()

    text = path.read_text(encoding="utf-8")
    assert "broken.png" in text and "InvalidInputError" in text
    assert "Traceback" not in text
    assert "garbage" not in text  # file contents are never logged
