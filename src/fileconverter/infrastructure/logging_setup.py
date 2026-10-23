"""Diagnostic logging to a rotating file in the user's application data folder."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

LOG_FILE_NAME = "fileconverter.log"
MAX_LOG_BYTES = 1_000_000
BACKUP_COUNT = 3  # fileconverter.log.1 .. .3
FILE_HANDLER_NAME = "fileconverter-file"
LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s"

logger = logging.getLogger(__name__)


def setup_file_logging(directory: Path, level: int = logging.INFO) -> Path | None:
    """Attach the rotating log file to the root logger. Safe to call more than once.

    Returns the log file path, or None if the file could not be opened; the
    application keeps working without a log file in that case.
    """
    root = logging.getLogger()
    _remove_file_handler(root)  # avoid duplicate handlers on repeated setup

    path = directory / LOG_FILE_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=MAX_LOG_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        # Logging is a diagnostic aid; never let it stop the application.
        if sys.stderr is not None:  # None when running without a console (pythonw)
            print(f"FileConverter: file logging disabled: {exc}", file=sys.stderr)
        return None

    handler.set_name(FILE_HANDLER_NAME)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return path


def install_excepthook() -> None:
    """Log otherwise unhandled exceptions (including ones raised in Qt slots)."""
    sys.excepthook = log_unhandled_exception


def log_unhandled_exception(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    if not issubclass(exc_type, KeyboardInterrupt):
        logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
    if sys.stderr is not None:  # also show it when started from a console
        sys.__excepthook__(exc_type, exc, tb)


def _remove_file_handler(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if handler.get_name() == FILE_HANDLER_NAME:
            root.removeHandler(handler)
            handler.close()
