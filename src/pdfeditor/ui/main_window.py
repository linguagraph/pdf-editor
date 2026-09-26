"""Main application window. Placeholder until Phase 2 (Viewer)."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import QLabel, QMainWindow

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("pdfeditor")
        self.resize(1200, 800)
        self.setCentralWidget(QLabel("pdfeditor: viewer arrives in Phase 2"))

    def open_path(self, path: Path) -> None:
        log.info("open requested: %s (viewer not implemented yet)", path)
