"""Reduce File Size (saves an optimized copy) and the space-usage audit."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox

from pdfeditor.model.metadata import SpaceUsage
from pdfeditor.services.optimize import ReduceOptions, ReduceResult, audit, reduce_size
from pdfeditor.ui.dialogs.optimize import ReduceSizeDialog, SpaceAuditDialog, human_size
from pdfeditor.ui.icons import icon
from pdfeditor.ui.jobs import Job, run_modal

if TYPE_CHECKING:
    from pdfeditor.core.session import DocumentSession
    from pdfeditor.ui.main_window import MainWindow


class OptimizeController:
    def __init__(self, window: MainWindow) -> None:
        self.w = window

        def act(text: str, icon_name: str, slot: Callable[[], object]) -> QAction:
            a = QAction(icon(icon_name), text, window)
            a.triggered.connect(lambda _=False: slot())
            a.setProperty("needs_doc", True)
            window.addAction(a)
            return a

        self.act_reduce = act("Reduce File &Size…", "shrink", self.reduce)
        self.act_audit = act("Space &Usage…", "chart-pie", self.audit)
        self.last_message = ""
        enabled = window.engine().capabilities.optimize
        for a in (self.act_reduce, self.act_audit):
            a.setVisible(enabled)

    def ribbon(self) -> None:
        self.w.ribbon.tab("Tools").add_group(self.act_reduce, self.act_audit)

    def _session(self) -> DocumentSession | None:
        view = self.w.current_view()
        return view.session if view is not None else None

    def _on_disk_size(self, session: DocumentSession) -> int | None:
        if session.path is not None and session.path.exists() and not session.is_dirty:
            return session.path.stat().st_size
        return None

    def _reduce(self, session: DocumentSession, options: ReduceOptions) -> ReduceResult | None:
        doc, lock, before = session.document, session.lock, self._on_disk_size(session)

        def work(job: Job) -> ReduceResult:
            with lock:
                return reduce_size(doc, options, before, job.token, job.progress)

        result = run_modal(self.w, "Optimizing…", work, 3)
        return result if isinstance(result, ReduceResult) else None

    def reduce(self, dialog: ReduceSizeDialog | None = None) -> Path | None:
        session = self._session()
        if session is None:
            return None
        if dialog is None:
            dialog = ReduceSizeDialog(session.path, self._on_disk_size(session), self.w)

            def estimate() -> None:
                try:
                    dialog.show_estimate(self._reduce(session, dialog.options()))
                except RuntimeError as exc:
                    QMessageBox.warning(self.w, "Reduce File Size", str(exc))

            dialog.estimate_button.clicked.connect(estimate)
        if not dialog.result() and not dialog.exec():
            return None
        options = dialog.options()
        result = dialog.estimate if dialog.estimate_for == options else None
        try:
            result = result or self._reduce(session, options)
        except RuntimeError as exc:
            QMessageBox.warning(self.w, "Reduce File Size", f"Optimizing failed:\n\n{exc}")
            return None
        if result is None:
            return None  # cancelled
        target = dialog.target_path()
        if session.path is not None and target.resolve() == session.path.resolve():
            QMessageBox.warning(
                self.w,
                "Reduce File Size",
                "Choose a different file name: the open document is not replaced.",
            )
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(result.data)
        tmp.replace(target)
        self.last_message = (
            f"Saved {target.name}: {human_size(result.before)} → {human_size(result.after)}"
        )
        self.w.statusBar().showMessage(self.last_message, 10000)
        return target

    def audit(self, show: bool = True) -> SpaceUsage | None:
        session = self._session()
        if session is None:
            return None
        doc, lock = session.document, session.lock

        def work(_job: Job) -> SpaceUsage:
            with lock:
                return audit(doc)

        try:
            usage = run_modal(self.w, "Measuring space usage…", work)
        except RuntimeError as exc:
            QMessageBox.warning(self.w, "Space Usage", str(exc))
            return None
        if not isinstance(usage, SpaceUsage):
            return None
        if show:
            SpaceAuditDialog(usage, self.w).exec()
        return usage
