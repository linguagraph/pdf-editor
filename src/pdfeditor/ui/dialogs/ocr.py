"""OCR dialogs: recognize text in this document, batch OCR, language downloads."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.services.ocr import COMMON_LANGUAGES, OcrOptions, installed_languages
from pdfeditor.ui.dialogs.base import FormDialog, Section, add_row, form_layout
from pdfeditor.ui.dialogs.pages import PageRangeBox


class LanguageList(QListWidget):
    """Installed languages with checkboxes (English checked by default)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMaximumHeight(140)
        self.setAccessibleName("OCR languages")
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
        self.deskew = QCheckBox("Straighten skewed scans (deskew)", self)
        download = QHBoxLayout()
        download.addWidget(self.download_combo, 1)
        download.addWidget(self.download_button)
        self.download_combo.setAccessibleName("Language to download")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        form = form_layout()
        layout.addLayout(form)
        add_row(form, "Languages:", self.languages)
        add_row(form, "Get more:", download, "Downloaded languages are kept for next time.")
        form.addRow("", self.skip_text)
        # the defaults suit most scans; these are for poor ones
        self.advanced = Section("Scan quality", self, expanded=False)
        layout.addWidget(self.advanced)
        advanced = self.advanced.form()
        add_row(advanced, "Resolution:", self.dpi, "Higher is slower but reads small print better.")
        advanced.addRow("", self.preprocess)
        advanced.addRow("", self.deskew)

    def options(self) -> OcrOptions:
        """Raises ValueError when no language is chosen."""
        langs = self.languages.languages()
        if not langs:
            raise ValueError("Choose at least one OCR language.")
        return OcrOptions(
            langs,
            self.dpi.value(),
            self.skip_text.isChecked(),
            self.preprocess.isChecked(),
            self.deskew.isChecked(),
        )


class OcrDialog(FormDialog):
    def __init__(
        self, page_count: int, current: int, selected: list[int], parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "Recognize Text (OCR)",
            "Adds an invisible text layer so scanned pages can be searched, selected and copied.",
            parent,
            primary="Recognize",
        )
        self.box = OcrOptionsBox(self)
        self.range = PageRangeBox(page_count, current, selected, self)
        self.add_widget(self.box)
        self.add_widget(self.range)


class BatchOcrDialog(FormDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Batch OCR",
            "Makes searchable copies of several scanned PDFs; the originals stay unchanged.",
            parent,
            primary="Start",
        )
        self.files = QListWidget(self)
        self.files.setAccessibleName("Files")
        add = QPushButton("Add Files…", self)
        add.clicked.connect(self._add)
        self.out_dir = QLineEdit(self)
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._browse)
        self.box = OcrOptionsBox(self)
        out = QHBoxLayout()
        out.addWidget(self.out_dir, 1)
        out.addWidget(browse)
        files = QVBoxLayout()
        files.addWidget(self.files)
        add_row_layout = QHBoxLayout()
        add_row_layout.addWidget(add)
        add_row_layout.addStretch()
        files.addLayout(add_row_layout)
        form = self.add_form()
        add_row(form, "Files:", files)
        add_row(form, "Save copies to:", out)
        self.add_widget(self.box)

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
