"""Run a service function on a worker thread with Qt signals for progress and results."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QEventLoop, QObject, Qt, QThreadPool, Signal
from PySide6.QtWidgets import QProgressDialog, QWidget

from pdfeditor.core.jobs import Cancelled, CancelToken

log = logging.getLogger(__name__)

JobFn = Callable[["Job"], Any]


class Job(QObject):
    """One background task. ``fn(job)`` runs off the GUI thread; signals arrive on the GUI thread.

    Inside ``fn``, call ``job.report(...)`` to stream partial results, pass ``job.progress`` as a
    progress callback, and ``job.token`` to services that support cancellation.
    """

    progress_changed = Signal(int, int)  # done, total
    partial = Signal(object)
    finished = Signal(object)  # return value
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, fn: JobFn, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._fn = fn
        self.token = CancelToken()
        self.done = False

    def progress(self, done: int, total: int) -> None:
        self.progress_changed.emit(done, total)

    def report(self, value: object) -> None:
        self.partial.emit(value)

    def cancel(self) -> None:
        self.token.cancel()

    def start(self) -> Job:
        """Queue the job. Connect to its signals *before* calling this, or a fast job can
        finish before anyone listens."""
        job_pool().start(self.run)
        return self

    def run(self) -> None:
        try:
            result = self._fn(self)
        except Cancelled:
            self.done = True
            self.cancelled.emit()
            return
        except Exception as exc:
            log.exception("background job failed")
            self.done = True
            self.failed.emit(str(exc))
            return
        self.done = True
        if self.token.cancelled:
            self.cancelled.emit()
        else:
            self.finished.emit(result)


_pool: QThreadPool | None = None


def job_pool() -> QThreadPool:
    """Pool for jobs, separate from the render pool so a long search can't starve rendering."""
    global _pool
    if _pool is None:
        _pool = QThreadPool()
        _pool.setMaxThreadCount(2)
    return _pool


def run_modal(parent: QWidget, label: str, work: JobFn, total: int = 0) -> object:
    """Run ``work`` on a worker thread behind a window-modal, cancellable progress dialog.

    Returns the result, or None if the user cancelled; raises RuntimeError if ``work`` failed.
    """
    progress = QProgressDialog(label, "Cancel", 0, total, parent)
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setMinimumDuration(300)
    job = Job(work)
    outcome: dict[str, object] = {}
    loop = QEventLoop()

    def on_progress(done: int, t: int) -> None:
        progress.setMaximum(t)
        progress.setValue(done)

    def finish(key: str, value: object) -> None:
        outcome[key] = value
        loop.quit()

    progress.canceled.connect(job.cancel)
    job.progress_changed.connect(on_progress)
    job.finished.connect(lambda result: finish("result", result))
    job.failed.connect(lambda msg: finish("error", msg))
    job.cancelled.connect(lambda: finish("cancelled", True))
    job.start()
    loop.exec()  # results arrive as queued signals, so none can be missed
    progress.close()
    progress.deleteLater()
    if "error" in outcome:
        raise RuntimeError(str(outcome["error"]))
    return None if outcome.get("cancelled") else outcome.get("result")
