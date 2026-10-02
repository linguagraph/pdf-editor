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
from pdfeditor.ui.toasts import folder_action

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
        self.w.ribbon.tab("Tools").add_group(self.act_reduce, self.act_audit, title="Optimize")

    def _session(self) -> DocumentSession | None:
        view = self.w.current_view()
        return view.session if view is not None else None

    def _on_disk_size(self, session: DocumentSession) -> int | None:
        if session.path is not None and session.path.exists() and not session.is_dirty:
            return session.path.stat().st_size
        return None

    @staticmethod
    def _reduce_work(
        session: DocumentSession, options: ReduceOptions, before: int | None
    ) -> Callable[[Job], ReduceResult]:
        doc, lock = session.document, session.lock

        def work(job: Job) -> ReduceResult:
            with job.hold(lock):
                return reduce_size(doc, options, before, job.token, job.progress)

        return work

    def _reduce(self, session: DocumentSession, options: ReduceOptions) -> ReduceResult | None:
        """The estimate, run from inside the (modal) Reduce File Size dialog: it waits."""
        work = self._reduce_work(session, options, self._on_disk_size(session))
        result = run_modal(self.w, "Optimizing…", work, 3)
        return result if isinstance(result, ReduceResult) else None

    def reduce(self, dialog: ReduceSizeDialog | None = None) -> Job | Path | None:
        """Save a smaller copy. Returns the job writing it (its outcome is the path), or the
        path at once when the dialog's estimate already holds the result."""
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
        target = dialog.target_path()
        if session.path is not None and target.resolve() == session.path.resolve():
            QMessageBox.warning(
                self.w,
                "Reduce File Size",
                "Choose a different file name: the open document is not replaced.",
            )
            return None
        estimate_result = dialog.estimate if dialog.estimate_for == options else None
        if estimate_result is not None:
            return self._write(estimate_result, target)
        return self.w.jobs.start(
            "Reducing file size…",
            self._reduce_work(session, options, self._on_disk_size(session)),
            total=3,
            session=session,
            on_done=lambda result: (
                self._write(result, target) if isinstance(result, ReduceResult) else None
            ),
        )

    def _write(self, result: ReduceResult, target: Path) -> Path | None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(result.data)
            tmp.replace(target)
        except OSError as exc:
            self.w.notify(f"Couldn't save {target.name}: {exc}", "error")
            return None
        self.last_message = (
            f"Saved {target.name}: {human_size(result.before)} → {human_size(result.after)}"
        )
        self.w.notify(self.last_message, "success", folder_action(target))
        return target

    def audit(self, show: bool = True) -> Job | None:
        """Measure what takes space; the job's outcome is the :class:`SpaceUsage`."""
        session = self._session()
        if session is None:
            return None
        doc, lock = session.document, session.lock

        def work(job: Job) -> SpaceUsage:
            with job.hold(lock):
                return audit(doc)

        def done(usage: object) -> SpaceUsage | None:
            if not isinstance(usage, SpaceUsage):
                return None
            if show:
                SpaceAuditDialog(usage, self.w).exec()
            return usage

        return self.w.jobs.start("Measuring space usage…", work, session=session, on_done=done)
