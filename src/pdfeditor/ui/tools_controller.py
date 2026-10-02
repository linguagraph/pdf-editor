"""Tools ribbon: OCR (this document and batch), and OCR of scans opened in the Edit tool."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox

from pdfeditor.core.commands import SnapshotCommand
from pdfeditor.model.scan import ScanCleanup
from pdfeditor.services.ocr import (
    OcrOptions,
    OcrResult,
    apply,
    make_scans_editable,
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
    from pdfeditor.ui.view.document_view import DocumentView


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
        self._prepared: set[int] = set()  # sessions whose scans were offered for editing

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
            if result.changed_pages:
                session.execute(
                    SnapshotCommand(
                        "Recognize Text", lambda doc: apply(doc, result), session.snapshots
                    )
                )
            recognized = len(result.layers)
            skipped = len(set(result.skipped) - set(result.plans))
            self.last_message = f"Recognized text on {recognized} page(s)" + (
                f"; skipped {skipped} page(s) that already had text." if skipped else "."
            )
            converted = len(set(result.plans) - set(result.layers))
            if converted:
                self.last_message += f" Made the text on {converted} searchable page(s) editable."
            undo = undo_action(session) if result.changed_pages else None
            self.w.notify(self.last_message, "success", undo)
            return result

        return self.w.jobs.start(
            "Recognizing text…", work, total=len(pages), session=session, on_done=done
        )

    def remembered_options(self) -> OcrOptions | None:
        """The last OCR settings, for OCR without a dialog; output is always editable (None
        when no language is installed)."""
        options = self.w.prefs.ocr_options()
        if not options.languages:
            return None
        cleanup = ScanCleanup.ERASE if options.cleanup is ScanCleanup.KEEP else options.cleanup
        return replace(options, cleanup=cleanup)

    def prepare_scans_for_editing(self, view: DocumentView) -> Job | None:
        """The Edit tool opened: if the document is a scan, recognize its scanned pages and
        replace their scanned text with real text (one undo step), so a double-click edits a
        paragraph. Runs once per document; returns the job, if one started."""
        session = view.session
        if (
            not self.w.prefs.ocr_when_editing
            or not session.engine.capabilities.ocr
            or session.id in self._prepared
            or self.w.jobs.busy(session)
            or session.busy
        ):
            return None
        with session.lock:  # a quick look at the page in view decides whether to look further
            info = session.document.page(view.current_page).scan_info()
        if not (info.needs_ocr or info.has_hidden_ocr):
            return None
        self._prepared.add(session.id)
        options = self.remembered_options()
        if options is None:
            self.w.notify(
                "To edit the text of this scan, download an OCR language in "
                "Tools ▸ Recognize Text (OCR).",
                "info",
            )
            return None

        def work(job: Job) -> OcrResult:
            return make_scans_editable(
                session.document, options, job.token, job.progress, session.lock, job.status
            )

        def done(result: object) -> OcrResult | None:
            if not isinstance(result, OcrResult) or not result.changed_pages:
                return None
            session.execute(
                SnapshotCommand(
                    "Make Scanned Text Editable", lambda doc: apply(doc, result), session.snapshots
                )
            )
            if result.plans:
                self.last_message = (
                    f"Recognized text on {len(result.plans)} scanned page(s) so you can edit it."
                )
            else:  # e.g. masked scan images, which aren't erased: the text stays searchable
                self.last_message = (
                    f"Recognized text on {len(result.layers)} scanned page(s), but it can't be "
                    "edited in place; it is searchable."
                )
            self.w.notify(self.last_message, "success", undo_action(session))
            return result

        return self.w.jobs.start("Recognizing scanned text…", work, session=session, on_done=done)

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
