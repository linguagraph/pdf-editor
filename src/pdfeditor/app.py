"""Application bootstrap: logging, crash handling, QApplication startup."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import traceback
from datetime import datetime
from pathlib import Path
from types import TracebackType

from pdfeditor import __version__
from pdfeditor.core.paths import APP_NAME, data_dir

log = logging.getLogger(APP_NAME)


def setup_logging(level: int = logging.INFO, log_dir: Path | None = None) -> Path:
    """Log to stderr and a rotating file; returns the log file path."""
    log_dir = log_dir or data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{APP_NAME}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    return log_file


def write_crash_report(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> Path:
    crash_dir = data_dir() / "crashes"
    crash_dir.mkdir(parents=True, exist_ok=True)
    path = crash_dir / f"crash-{datetime.now():%Y%m%d-%H%M%S}.txt"
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    path.write_text(f"{APP_NAME} {__version__}\nPython {sys.version}\n\n{text}", encoding="utf-8")
    return path


def _excepthook(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
    report = write_crash_report(exc_type, exc, tb)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is not None:
            QMessageBox.critical(
                None,
                "Unexpected error",
                f"{exc_type.__name__}: {exc}\n\nA crash report was saved to:\n{report}",
            )
    except Exception:  # never let the hook itself raise
        log.exception("Failed to show crash dialog")


def install_excepthook() -> None:
    sys.excepthook = _excepthook


def _option_value(argv: list[str], name: str) -> str | None:
    """Value of ``--name=value`` or ``--name value``."""
    for i, arg in enumerate(argv):
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
    return None


def run(argv: list[str]) -> int:
    if "--self-test" in argv:
        from pdfeditor.selftest import run_self_test

        setup_logging(logging.WARNING)
        report = _option_value(argv, "--self-test-report")
        return run_self_test(Path(report) if report else None)

    setup_logging()
    install_excepthook()
    from pdfeditor.core.engine_lock import install_manual_gc

    install_manual_gc()
    log.info("Starting %s %s", APP_NAME, __version__)

    from PySide6.QtWidgets import QApplication

    from pdfeditor.ui.main_window import MainWindow
    from pdfeditor.ui.single_instance import InstanceServer, send_to_running_instance

    new_instance = "--new-instance" in argv
    files = [str(Path(a).resolve()) for a in argv[1:] if not a.startswith("--")]

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setApplicationVersion(__version__)
    from PySide6.QtGui import QIcon

    from pdfeditor.bundle import data_path

    app.setWindowIcon(QIcon(str(data_path("icon.png"))))

    if not new_instance and send_to_running_instance(files):
        log.info("handed %d file(s) to the running instance", len(files))
        return 0

    window = MainWindow()
    server = InstanceServer(parent=window)
    if not new_instance:
        server.listen()
    server.files_received.connect(window.open_forwarded)
    window.show()
    window.offer_recovery()
    for path in files:
        window.open_path(Path(path))
    return app.exec()
