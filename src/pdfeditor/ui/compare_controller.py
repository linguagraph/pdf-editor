"""Tools ▸ Compare Files: run the comparison as a job and show it side by side."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFileDialog, QMessageBox

from pdfeditor.core.session import DocumentSession
from pdfeditor.engine.base import OpenError, PasswordRequired
from pdfeditor.services.compare import CompareResult, compare, write_report
from pdfeditor.ui.dialogs.compare import CompareFilesDialog, CompareWindow
from pdfeditor.ui.dialogs.password import password_prompt
from pdfeditor.ui.icons import icon
from pdfeditor.ui.jobs import Job, run_modal
from pdfeditor.ui.view.document_view import DocumentView

if TYPE_CHECKING:
    from pdfeditor.ui.main_window import MainWindow


class CompareController:
    def __init__(self, window: MainWindow) -> None:
        self.w = window
        self.act_compare = QAction(icon("git-compare"), "&Compare Files…", window)
        self.act_compare.triggered.connect(lambda _=False: self.compare())
        self.act_compare.setProperty("needs_doc", False)
        window.addAction(self.act_compare)
        self.window: CompareWindow | None = None
        self.last_message = ""

    def ribbon(self) -> None:
        self.w.ribbon.tab("Tools").add_group(self.act_compare)

    def compare(self, dialog: CompareFilesDialog | None = None) -> CompareWindow | None:
        if dialog is None:
            view = self.w.current_view()
            dialog = CompareFilesDialog(view.session.path if view else None, self.w)
        if not dialog.result() and not dialog.exec():
            return None
        old_path, new_path = dialog.paths()
        if not old_path.is_file() or not new_path.is_file():
            QMessageBox.warning(self.w, "Compare Files", "Choose two existing PDF files.")
            return None
        sessions: list[DocumentSession] = []
        try:
            for path in (old_path, new_path):
                sessions.append(DocumentSession.open(path, password_prompt(self.w, path.name)))
        except (OpenError, PasswordRequired) as exc:
            for s in sessions:
                s.close()
            if not isinstance(exc, PasswordRequired):
                QMessageBox.warning(self.w, "Compare Files", str(exc))
            return None
        old, new = sessions
        options = dialog.options()

        def work(job: Job) -> CompareResult:
            with old.lock:
                return compare(old.document, new.document, options, job.token, job.progress)

        try:
            result = run_modal(self.w, "Comparing…", work, max(old.page_count, new.page_count))
        except RuntimeError as exc:
            QMessageBox.warning(self.w, "Compare Files", f"Comparing failed:\n\n{exc}")
            result = None
        if not isinstance(result, CompareResult):
            old.close()
            new.close()
            return None
        return self._show(old, new, result, (old_path.name, new_path.name))

    def _show(
        self,
        old: DocumentSession,
        new: DocumentSession,
        result: CompareResult,
        names: tuple[str, str],
    ) -> CompareWindow:
        views = [DocumentView(s, self.w.renderer) for s in (old, new)]
        for v in views:
            v.set_zoom(0.8)
        window = CompareWindow(
            views[0], views[1], result, names, lambda: self.export_report(window), self.w
        )

        def cleanup() -> None:
            for v in views:
                v.close_view()
                v.session.close()
            if self.window is window:
                self.window = None

        window.finished.connect(lambda _code: cleanup())
        self.window = window
        self.last_message = (
            "The documents are identical"
            if result.identical
            else f"{len(result.changes)} change(s) found"
        )
        self.w.statusBar().showMessage(self.last_message, 8000)
        window.show()
        return window

    def export_report(self, window: CompareWindow, target: Path | None = None) -> Path | None:
        if target is None:
            chosen, _ = QFileDialog.getSaveFileName(
                window, "Export Comparison Report", "comparison.pdf", "PDF documents (*.pdf)"
            )
            if not chosen:
                return None
            target = Path(chosen)
        old, new = window.old_view.session, window.new_view.session
        title = window.windowTitle().removeprefix("Compare: ")
        names = tuple(title.split(" ↔ ", 1)) if " ↔ " in title else ("Old", "New")
        try:
            with old.lock:
                path = write_report(
                    self.w.engine(),
                    old.document,
                    new.document,
                    window.compare_result,
                    target.with_suffix(".pdf"),
                    names[0],
                    names[-1],
                )
        except Exception as exc:  # report I/O or engine errors are shown, never fatal
            QMessageBox.warning(window, "Export Report", f"Couldn't write the report:\n\n{exc}")
            return None
        self.last_message = f"Report saved to {path}"
        self.w.statusBar().showMessage(self.last_message, 8000)
        return path
