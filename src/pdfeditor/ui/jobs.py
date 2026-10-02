"""Run a service function on a worker thread with Qt signals for progress and results."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEventLoop, QObject, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QProgressDialog, QWidget

from pdfeditor.core.jobs import Cancelled, CancelToken

if TYPE_CHECKING:
    from pdfeditor.core.session import DocumentSession

log = logging.getLogger(__name__)

JobFn = Callable[["Job"], Any]

# Pause at each progress step while a job holds the engine lock (see Job.hold), long enough
# for a thread waiting on the lock (the GUI, a render worker) to get it first.
YIELD_SECONDS = 0.001


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
    settled = Signal()  # (JobCenter) the job ended and its result was handled

    def __init__(
        self,
        fn: JobFn,
        parent: QObject | None = None,
        *,
        label: str = "",
        total: int = 0,
        session: DocumentSession | None = None,
    ) -> None:
        super().__init__(parent)
        self._fn = fn
        self.token = CancelToken()
        self.done = False
        self.label = label
        self.session = session
        self.done_steps = 0
        self.total = total
        self.created = time.monotonic()
        self.started = False
        self.is_settled = False
        self.outcome: object = None  # what the JobCenter's result handler returned
        self._state = threading.Lock()  # orders "started" against cancel (see JobCenter)
        self._held: list[AbstractContextManager[object]] = []

    def progress(self, done: int, total: int) -> None:
        self.progress_changed.emit(done, total)
        if self._held:
            self._yield_locks()

    def report(self, value: object) -> None:
        self.partial.emit(value)

    def cancel(self) -> None:
        self.token.cancel()

    @contextmanager
    def hold(self, lock: AbstractContextManager[object]) -> Iterator[None]:
        """Hold ``lock`` (the engine lock) for a step of work that the service can't split,
        but let other threads in at each :meth:`progress` call.

        A service that reads a whole document page by page under the lock would otherwise
        freeze every GUI action that needs the engine (hovering a link, selecting text) for
        the whole run, now that jobs run behind a non-modal progress chip. Between steps the
        document is only read, never changed (the session is read-only while a job runs).
        """
        with lock:
            self._held.append(lock)
            try:
                yield
            finally:
                self._held.remove(lock)

    def _yield_locks(self) -> None:
        held = list(self._held)
        for lock in reversed(held):
            lock.__exit__(None, None, None)
        try:
            time.sleep(YIELD_SECONDS)
        finally:
            for lock in held:
                lock.__enter__()

    def start(self) -> Job:
        """Queue the job. Connect to its signals *before* calling this, or a fast job can
        finish before anyone listens."""
        job_pool().start(self.run)
        return self

    def run(self) -> None:
        with self._state:
            skip = self.token.cancelled  # cancelled while queued: never touch the document
            self.started = not skip
        if skip:
            self.done = True
            self.cancelled.emit()
            return
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


def wait_for(job: object, timeout_ms: int = 60_000) -> object:
    """Spin an event loop until a :class:`JobCenter` job has been settled; return its outcome.

    For tests and scripted runs (the app itself never waits: results arrive through the
    handlers given to :meth:`JobCenter.start`). Anything that isn't a job passes through, so
    a command that returned before starting one (None, a cached result) is waited on alike.
    """
    if not isinstance(job, Job):
        return job
    if not job.is_settled:
        loop = QEventLoop()
        job.settled.connect(loop.quit)
        QTimer.singleShot(timeout_ms, loop.quit)
        if not job.is_settled:
            loop.exec()
        if not job.is_settled:
            raise TimeoutError(f"job {job.label!r} did not finish in {timeout_ms} ms")
    return job.outcome


def run_modal(parent: QWidget, label: str, work: JobFn, total: int = 0) -> object:
    """Run ``work`` on a worker thread behind a window-modal, cancellable progress dialog.

    Only for work started from inside a modal dialog, which must wait for the result (the
    Reduce File Size estimate). Everything else runs through ``MainWindow.jobs`` and shows a
    progress chip instead. Returns the result, or None if the user cancelled; raises
    RuntimeError if ``work`` failed.
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
