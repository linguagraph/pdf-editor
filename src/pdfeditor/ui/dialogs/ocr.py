"""OCR dialogs: recognize text in this document, batch OCR, language downloads."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QHideEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.model.scan import ScanCleanup
from pdfeditor.services.ocr import (
    COMMON_LANGUAGES,
    OcrOptions,
    download_language,
    installed_languages,
    language_name,
    pick_languages,
)
from pdfeditor.ui.dialogs.base import FormDialog, Section, add_row, caption, form_layout, set_role
from pdfeditor.ui.dialogs.pages import PageRangeBox
from pdfeditor.ui.jobs import Job
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.style.tokens import METRICS

OUTPUTS = (
    (ScanCleanup.KEEP, "Searchable: keep the scan, add invisible text"),
    (ScanCleanup.ERASE, "Editable: replace the scanned text with real text"),
    (ScanCleanup.REMOVE, "Text only: real text, remove the scanned image"),
)


def format_bytes(n: int) -> str:
    if n < 1024 * 1024:
        return f"{round(n / 1024)} KB"
    return f"{n / (1024 * 1024):.1f} MB"


class LanguageList(QListWidget):
    """Installed languages with checkboxes; the ones used last time are checked (English if
    none of them is installed any more)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMaximumHeight(140)
        self.setAccessibleName("OCR languages")
        self.reload()

    def reload(self, checked: set[str] | None = None) -> None:
        installed = sorted(installed_languages())
        if checked is None:
            checked = set(pick_languages(AppSettings().ocr_languages, installed))
        self.clear()
        for code in installed:
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
    """OCR options, with language downloads shown inline (progress, cancel, errors)."""

    language_installed = Signal(str)  # language code

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.languages = LanguageList(self)
        self.download_combo = QComboBox(self)
        for code in COMMON_LANGUAGES:
            self.download_combo.addItem(language_name(code), code)
        self.download_button = QPushButton("Download", self)
        self.download_button.clicked.connect(self.start_download)
        self.download_job: Job | None = None
        self._downloading = ""
        # shown under "Get more" once a download starts: what, how far, and a way to stop it
        self.download_panel = QWidget(self)
        self.download_status = caption("", self.download_panel)
        self.download_bar = QProgressBar(self.download_panel)
        self.download_bar.setAccessibleName("Download progress")
        self.download_cancel = QPushButton("Cancel", self.download_panel)
        self.download_cancel.setAccessibleName("Cancel download")
        self.download_cancel.clicked.connect(self.cancel_download)
        bar_row = QHBoxLayout()
        bar_row.setSpacing(METRICS.space(2))
        bar_row.addWidget(self.download_bar, 1)
        bar_row.addWidget(self.download_cancel)
        panel = QVBoxLayout(self.download_panel)
        panel.setContentsMargins(0, 0, 0, 0)
        panel.setSpacing(METRICS.space(1))
        panel.addWidget(self.download_status)
        panel.addLayout(bar_row)
        self.download_panel.setVisible(False)
        self.dpi = QSpinBox(self)
        self.dpi.setRange(100, 600)
        self.dpi.setSingleStep(50)
        remembered = AppSettings()
        self.dpi.setValue(remembered.ocr_dpi)
        self.dpi.setSuffix(" dpi")
        self.skip_text = QCheckBox("Skip pages that already have text", self)
        self.skip_text.setChecked(remembered.ocr_skip_pages_with_text)
        self.output = QComboBox(self)
        self.output.setAccessibleName("OCR output")
        for cleanup, text in OUTPUTS:
            self.output.addItem(text, cleanup)
        self.output.setCurrentIndex(max(0, self.output.findData(remembered.ocr_cleanup)))
        self.preprocess = QCheckBox("Clean up the image first (grayscale, denoise, binarize)", self)
        self.preprocess.setChecked(remembered.ocr_preprocess)
        self.deskew = QCheckBox("Straighten skewed scans (deskew)", self)
        self.deskew.setChecked(remembered.ocr_deskew)
        download = QHBoxLayout()
        download.addWidget(self.download_combo, 1)
        download.addWidget(self.download_button)
        self.download_combo.setAccessibleName("Language to download")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        form = form_layout()
        layout.addLayout(form)
        add_row(form, "Languages:", self.languages)
        get_more = QVBoxLayout()
        get_more.setSpacing(METRICS.space(2))
        get_more.addLayout(download)
        get_more.addWidget(self.download_panel)
        add_row(form, "Get more:", get_more, "Downloaded languages are kept for next time.")
        add_row(
            form,
            "Output:",
            self.output,
            "Editable output erases the recognized text from the scan, so edits look clean; "
            "pictures and figures stay.",
        )
        form.addRow("", self.skip_text)
        # the defaults suit most scans; these are for poor ones
        self.advanced = Section("Scan quality", self, expanded=False)
        layout.addWidget(self.advanced)
        advanced = self.advanced.form()
        add_row(advanced, "Resolution:", self.dpi, "Higher is slower but reads small print better.")
        advanced.addRow("", self.preprocess)
        advanced.addRow("", self.deskew)

    # -- language download (a background job; the dialog stays usable) ------------------
    def is_downloading(self) -> bool:
        return self.download_job is not None

    def start_download(self) -> None:
        if self.download_job is not None:
            return
        code = str(self.download_combo.currentData())
        job = Job(lambda job: download_language(code, job.token, job.progress))
        job.progress_changed.connect(self._on_progress)
        job.finished.connect(self._on_finished)
        job.failed.connect(self._on_failed)
        job.cancelled.connect(self._on_cancelled)
        self.download_job = job
        self._download_started(code)
        job.start()

    def cancel_download(self) -> None:
        if self.download_job is None:
            return
        self.download_job.cancel()
        self.download_cancel.setEnabled(False)
        self._set_status(f"Cancelling the download of {language_name(self._downloading)}…")

    def _download_started(self, code: str) -> None:
        self._downloading = code
        self.download_combo.setEnabled(False)
        self.download_button.setEnabled(False)
        self._set_status(f"Connecting to download {language_name(code)}…")
        self.download_bar.setRange(0, 0)  # busy until the size is known
        self.download_bar.setVisible(True)
        self.download_cancel.setVisible(True)
        self.download_cancel.setEnabled(True)
        self.download_panel.setVisible(True)

    def _download_progress(self, received: int, total: int) -> None:
        name = language_name(self._downloading)
        if total > 0:
            self.download_bar.setRange(0, total)
            self.download_bar.setValue(min(received, total))
            text = f"Downloading {name}: {format_bytes(received)} of {format_bytes(total)}"
        else:
            self.download_bar.setRange(0, 0)  # the server didn't say how big it is
            text = f"Downloading {name}: {format_bytes(received)} received"
        if self.download_cancel.isEnabled():  # keep "Cancelling…" once asked
            self._set_status(text)

    def _download_ended(self, message: str, role: str = "caption") -> None:
        self.download_job = None
        self.download_combo.setEnabled(True)
        self.download_button.setEnabled(True)
        self.download_bar.setVisible(False)
        self.download_cancel.setVisible(False)
        self._set_status(message, role)

    def _download_finished(self) -> None:
        code = self._downloading
        self.languages.reload(set(self.languages.languages()) | {code})
        self._download_ended(f"{language_name(code)} is installed and selected.")
        self.language_installed.emit(code)

    def _download_failed(self, message: str) -> None:
        self._download_ended(
            f"Couldn't download {language_name(self._downloading)}: {message}. "
            "Check the internet connection and try again.",
            "error",
        )

    def _set_status(self, text: str, role: str = "caption") -> None:
        if self.download_status.property("role") != role:
            set_role(self.download_status, role)
        self.download_status.setText(text)

    # job signals; a job that was cancelled when the dialog closed no longer counts
    def _on_progress(self, received: int, total: int) -> None:
        if self.sender() is self.download_job:
            self._download_progress(received, total)

    def _on_finished(self, _path: object) -> None:
        if self.sender() is self.download_job:
            self._download_finished()

    def _on_failed(self, message: str) -> None:
        if self.sender() is self.download_job:
            self._download_failed(message)

    def _on_cancelled(self) -> None:
        if self.sender() is self.download_job:
            self._download_ended("Download cancelled.")

    def hideEvent(self, event: QHideEvent) -> None:
        # the dialog closed (not just minimized): stop downloading
        if self.download_job is not None and not event.spontaneous():
            self.download_job.cancel()
            self._download_ended("Download cancelled.")
        super().hideEvent(event)

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
            cleanup=self.output.currentData(),
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
