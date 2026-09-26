"""Run a service function on a worker thread with Qt signals for progress and results."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThreadPool, Signal

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
