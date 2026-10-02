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
    ocr_files,
    recognize,
)
from pdfeditor.ui.dialogs.ocr import BatchOcrDialog, OcrDialog
from pdfeditor.ui.dialogs.pages import checked_pages
from pdfeditor.ui.icons import icon
from pdfeditor.ui.jobs import Job
from pdfeditor.ui.toasts import folder_action, undo_action

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
        r.add_group(self.act_ocr, self.act_batch_ocr, title="Recognize Text")

    # -- commands ---------------------------------------------------------------------------
    def recognize(self, dialog: OcrDialog | None = None) -> Job | None:
        """Run OCR on the chosen pages in the background; the text layer is added (one undo
        step) when it's done. Returns the job; its outcome is the :class:`OcrResult`."""
        tab = self.w.current_tab()
        if tab is None:
            return None
        view = tab.view
        if dialog is None:
            dialog = OcrDialog(view.page_count, view.current_page, tab.target_pages(), self.w)
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
        self.w.prefs.remember_ocr_options(options)
        session = view.session

        def work(job: Job) -> OcrResult:
            # engine access is serialized page by page, so the view keeps rendering meanwhile
            return recognize(
                session.document,
                pages,
                options,
                job.token,
                job.progress,
                session.lock,
                status=job.status,
            )

        def done(result: object) -> OcrResult | None:
            if not isinstance(result, OcrResult):
                return None
            if result.layers:
                session.execute(
                    SnapshotCommand(
                        "Recognize Text", lambda doc: apply(doc, result), session.snapshots
                    )
                )
            recognized, skipped = len(result.layers), len(result.skipped)
            self.last_message = f"Recognized text on {recognized} page(s)" + (
                f"; skipped {skipped} page(s) that already had text." if skipped else "."
            )
            undo = undo_action(session) if result.layers else None
            self.w.notify(self.last_message, "success", undo)
            return result

        return self.w.jobs.start(
            "Recognizing text…", work, total=len(pages), session=session, on_done=done
        )

    def batch_ocr(self, dialog: BatchOcrDialog | None = None) -> Job | None:
        """OCR files into searchable copies; the job's outcome is the list of files written."""
        if dialog is None:
            dialog = BatchOcrDialog(self.w)
        if not dialog.result() and not dialog.exec():
            return None
        paths, out_dir = dialog.paths(), Path(dialog.out_dir.text())
        if not paths or not dialog.out_dir.text():
            QMessageBox.warning(self.w, "Batch OCR", "Add files and choose an output folder.")
            return None
        try:
            options: OcrOptions = dialog.box.options()
        except ValueError as exc:
            QMessageBox.warning(self.w, "Batch OCR", str(exc))
            return None
        self.w.prefs.remember_ocr_options(options)
        engine = self.w.engine()

        def done(written: object) -> list[Path]:
            result = written if isinstance(written, list) else []
            self.last_message = f"Wrote {len(result)} searchable file(s) to {out_dir}"
            target = result[0] if result else out_dir
            self.w.notify(self.last_message, "success", folder_action(target))
            return result

        return self.w.jobs.start(
            "Batch OCR…",
            lambda job: ocr_files(
                engine, paths, out_dir, options, job.token, job.progress, job.status
            ),
            total=len(paths),
            on_done=done,
        )
