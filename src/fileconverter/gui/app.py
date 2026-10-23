"""GUI entry point: `python -m fileconverter.gui` or the `fileconverter-gui` command."""

import logging
import os
import platform
import sys

from PySide6 import __version__ as pyside_version
from PySide6.QtWidgets import QApplication

from fileconverter import __version__
from fileconverter.app import build_registry
from fileconverter.gui.main_window import MainWindow
from fileconverter.infrastructure import paths
from fileconverter.infrastructure.logging_setup import install_excepthook, setup_file_logging
from fileconverter.infrastructure.settings import SETTINGS_FILE_NAME, SettingsStore

logger = logging.getLogger(__name__)


def main() -> int:
    # Logging first, so that problems during startup are recorded too.
    log_file = setup_file_logging(paths.log_dir())
    install_excepthook()
    logger.info(
        "Starting FileConverter %s (Python %s, PySide6 %s, %s); log file: %s",
        __version__,
        platform.python_version(),
        pyside_version,
        platform.platform(),
        log_file,
    )

    app = QApplication(sys.argv)
    app.setApplicationName("FileConverter")
    settings_store = SettingsStore(paths.settings_dir() / SETTINGS_FILE_NAME)
    window = MainWindow(build_registry(), settings_store)
    window.show()
    exit_code = app.exec()

    if window.has_running_jobs():
        # A conversion ignored cancellation for longer than the shutdown timeout
        # (e.g. stuck inside one long library call). Python threads cannot be
        # killed, and waiting would leave an invisible process running, so end the
        # process now. That job's temporary file may remain in its output folder.
        logger.warning("Exiting while a conversion is still running; forcing process exit")
        logging.shutdown()
        os._exit(exit_code)

    logger.info("FileConverter exited normally")
    return exit_code
