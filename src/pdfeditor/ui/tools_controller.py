"""Tools ribbon: OCR (this document and batch)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox

from pdfeditor.core.commands import SnapshotCommand
from pdfeditor.services.ocr import (
    OcrOptions,
    OcrResult,
    apply,
    download_language,
    ocr_files,
    recognize,
)
from pdfeditor.ui.dialogs.ocr import BatchOcrDialog, OcrDialog, OcrOptionsBox
from pdfeditor.ui.dialogs.pages import checked_pages
from pdfeditor.ui.icons import icon
from pdfeditor.ui.jobs import Job, run_modal

if TYPE_CHECKING:
    from pdfeditor.ui.main_window import MainWindow


class ToolsController:
    def __init__(self, window: MainWindow) -> None:
        self.w = window

        def act(
            text: str, icon_name: str, slot: Callable[[], object], needs_doc: bool = True
        ) -> QAction:
            a = QAction(icon(icon_name), text, window)
            a.triggered.connect(lambda _=False: slot())
            a.setProperty("needs_doc", needs_doc)
            window.addAction(a)
            return a

        self.act_ocr = act("&Recognize Text (OCR)…", "scan-text", self.recognize)
        self.act_batch_ocr = act("&Batch OCR…", "scan-line", self.batch_ocr, needs_doc=False)
        self.last_message = ""

    def ribbon(self) -> None:
        r = self.w.ribbon.add_tab("Tools")
        r.add_group(self.act_ocr, self.act_batch_ocr)

    # -- helpers ----------------------------------------------------------------------------
    def _wire_download(self, box: OcrOptionsBox) -> None:
        def download() -> None:
            code = str(box.download_combo.currentData())
            checked = set(box.languages.languages()) | {code}
            try:
                self.run_job(f"Downloading {code}…", lambda job: download_language(code, job.token))
            except Exception as exc:  # network errors are reported, never fatal
                QMessageBox.warning(self.w, "OCR Languages", f"Couldn't download {code}:\n\n{exc}")
                return
            box.languages.reload(checked)

        box.download_button.clicked.connect(download)

    def run_job(self, label: str, work: Callable[[Job], object], total: int = 0) -> object:
        return run_modal(self.w, label, work, total)

    # -- commands ---------------------------------------------------------------------------
    def recognize(self, dialog: OcrDialog | None = None) -> OcrResult | None:
        tab = self.w.current_tab()
        if tab is None:
            return None
        view = tab.view
        if dialog is None:
            dialog = OcrDialog(view.page_count, view.current_page, tab.target_pages(), self.w)
            self._wire_download(dialog.box)
        if not dialog.result() and not dialog.exec():
            return None
        pages = checked_pages(self.w, dialog.range)
        if not pages:
            return None
        try:
            options = dialog.box.options()
        except ValueError as exc:
            QMessageBox.warning(self.w, "OCR", str(exc))
            return None
        session = view.session

        def work(job: Job) -> OcrResult:
            # engine access is serialized page by page, so the view keeps rendering meanwhile
            return recognize(
                session.document, pages, options, job.token, job.progress, session.lock
            )

        try:
            result = self.run_job("Recognizing text…", work, len(pages))
        except (RuntimeError, ValueError) as exc:
            QMessageBox.warning(self.w, "OCR", str(exc))
            return None
        if not isinstance(result, OcrResult):
            return None  # cancelled
        if result.layers:
            session.execute(
                SnapshotCommand("Recognize Text", lambda doc: apply(doc, result), session.snapshots)
            )
        done, skipped = len(result.layers), len(result.skipped)
        self.last_message = f"Recognized text on {done} page(s)" + (
            f"; skipped {skipped} page(s) that already had text." if skipped else "."
        )
        self.w.statusBar().showMessage(self.last_message, 8000)
        return result

    def batch_ocr(self, dialog: BatchOcrDialog | None = None) -> list[Path]:
        if dialog is None:
            dialog = BatchOcrDialog(self.w)
            self._wire_download(dialog.box)
        if not dialog.result() and not dialog.exec():
            return []
        paths, out_dir = dialog.paths(), Path(dialog.out_dir.text())
        if not paths or not dialog.out_dir.text():
            QMessageBox.warning(self.w, "Batch OCR", "Add files and choose an output folder.")
            return []
        try:
            options: OcrOptions = dialog.box.options()
            engine = self.w.engine()
            written = self.run_job(
                "Batch OCR…",
                lambda job: ocr_files(engine, paths, out_dir, options, job.token, job.progress),
                len(paths),
            )
        except (RuntimeError, ValueError) as exc:
            QMessageBox.warning(self.w, "Batch OCR", str(exc))
            return []
        result = written if isinstance(written, list) else []
        self.last_message = f"Wrote {len(result)} searchable file(s) to {out_dir}"
        self.w.statusBar().showMessage(self.last_message, 8000)
        return result
