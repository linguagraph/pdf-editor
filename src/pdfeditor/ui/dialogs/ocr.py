"""OCR dialogs: recognize text in this document, batch OCR, language downloads."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.services.ocr import COMMON_LANGUAGES, OcrOptions, installed_languages
from pdfeditor.ui.dialogs.pages import PageRangeBox


class LanguageList(QListWidget):
    """Installed languages with checkboxes (English checked by default)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMaximumHeight(140)
        self.reload()

    def reload(self, checked: set[str] | None = None) -> None:
        checked = checked if checked is not None else {"eng"}
        self.clear()
        for code in sorted(installed_languages()):
            item = QListWidgetItem(f"{COMMON_LANGUAGES.get(code, code)} ({code})")
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if code in checked else Qt.CheckState.Unchecked
            )
            self.addItem(item)

    def languages(self) -> tuple[str, ...]:
        return tuple(
            self.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.count())
            if self.item(i).checkState() == Qt.CheckState.Checked
        )


class OcrOptionsBox(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.languages = LanguageList(self)
        self.download_combo = QComboBox(self)
        for code, name in COMMON_LANGUAGES.items():
            self.download_combo.addItem(f"{name} ({code})", code)
        self.download_button = QPushButton("Download", self)
        self.dpi = QSpinBox(self)
        self.dpi.setRange(100, 600)
        self.dpi.setSingleStep(50)
        self.dpi.setValue(300)
        self.dpi.setSuffix(" dpi")
        self.skip_text = QCheckBox("Skip pages that already have text", self)
        self.skip_text.setChecked(True)
        self.preprocess = QCheckBox("Clean up the image first (grayscale, denoise, binarize)", self)
        download = QHBoxLayout()
        download.addWidget(self.download_combo, 1)
        download.addWidget(self.download_button)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Languages:", self.languages)
        form.addRow("Get more:", download)
        form.addRow("Resolution:", self.dpi)
        form.addRow("", self.skip_text)
        form.addRow("", self.preprocess)

    def options(self) -> OcrOptions:
        """Raises ValueError when no language is chosen."""
        langs = self.languages.languages()
        if not langs:
            raise ValueError("Choose at least one OCR language.")
        return OcrOptions(
            langs, self.dpi.value(), self.skip_text.isChecked(), self.preprocess.isChecked()
        )


class OcrDialog(QDialog):
    def __init__(
        self, page_count: int, current: int, selected: list[int], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Recognize Text (OCR)")
        intro = QLabel(
            "Adds an invisible text layer so scanned pages can be searched, selected and copied."
        )
        intro.setWordWrap(True)
        self.box = OcrOptionsBox(self)
        self.range = PageRangeBox(page_count, current, selected, self)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Recognize")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.box)
        layout.addWidget(self.range)
        layout.addWidget(buttons)


class BatchOcrDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Batch OCR")
        self.files = QListWidget(self)
        add = QPushButton("Add Files…", self)
        add.clicked.connect(self._add)
        self.out_dir = QLineEdit(self)
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._browse)
        self.box = OcrOptionsBox(self)
        out = QHBoxLayout()
        out.addWidget(self.out_dir, 1)
        out.addWidget(browse)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Files:"))
        layout.addWidget(self.files)
        layout.addWidget(add)
        layout.addWidget(QLabel("Save searchable copies to:"))
        layout.addLayout(out)
        layout.addWidget(self.box)
        layout.addWidget(buttons)

    def add_path(self, path: Path) -> None:
        self.files.addItem(str(path))

    def paths(self) -> list[Path]:
        return [Path(self.files.item(i).text()) for i in range(self.files.count())]

    def _add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add PDFs", "", "PDF documents (*.pdf)")
        for p in paths:
            self.add_path(Path(p))

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Output Folder", self.out_dir.text())
        if chosen:
            self.out_dir.setText(chosen)
