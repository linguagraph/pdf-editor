"""Background jobs of the main window, and the progress chip that shows them.

Long work (OCR, export, optimize, compare, PDF/A...) runs as a :class:`~pdfeditor.ui.jobs.Job`
without blocking the window: a compact chip at the right of the status bar shows its progress
with a Cancel button, and clicking it opens the details (name, document, progress, elapsed
time, Cancel for each job). Several jobs stack into one chip ("2 tasks running").

A job that works on an open document makes that document read-only until it ends
(``DocumentSession.begin_task``): the job reads it between steps, and OCR and auto-tagging
apply their result to it at the end, so edits in between would conflict. The rest of the
window, and other documents, stay fully usable.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QObject, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.ui.icons import icon
from pdfeditor.ui.jobs import Job, JobFn
from pdfeditor.ui.style.tokens import METRICS
from pdfeditor.ui.theme import current_colors
from pdfeditor.ui.toasts import Kind, announce

if TYPE_CHECKING:
    from pdfeditor.core.session import DocumentSession

log = logging.getLogger(__name__)

SHOW_DELAY_MS = 300  # jobs quicker than this never flash the chip
WAIT_SECONDS = 30.0  # longest a closing document waits for its cancelled job to stop

Notify = Callable[[str, Kind], object]


def _title(label: str) -> str:
    return label.rstrip(".…").strip()


def job_heading(job: Job) -> str:
    """The job's name, with what it's doing now when it said so ("Recognizing text · page 1
    of 3")."""
    title = _title(job.label)
    if not job.status_text:
        return title
    text = QCoreApplication.translate("JobCenter", "{task} · {status}")
    return text.format(task=title, status=job.status_text)


def is_indeterminate(job: Job) -> bool:
    """Show a busy bar: the job has no steps to count, or none has finished yet (a bar
    sitting at 0 of 1 for the whole of a one-page OCR looks stuck)."""
    return job.total <= 0 or job.done_steps <= 0


def elapsed_text(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def progress_text(job: Job) -> str:
    if job.total > 0:
        return QCoreApplication.translate("JobCenter", "{done} of {total}").format(
            done=min(job.done_steps, job.total), total=job.total
        )
    return QCoreApplication.translate("JobCenter", "Working…")


class JobCenter(QObject):
    """Starts jobs, tracks the running ones and hands their results to the caller."""

    changed = Signal()  # a job started, progressed or ended
    running_changed = Signal()  # a job started or ended (not on every progress step)

    def __init__(self, notify: Notify, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._notify = notify
        self._jobs: list[Job] = []

    def start(
        self,
        label: str,
        work: JobFn,
        *,
        total: int = 0,
        session: DocumentSession | None = None,
        on_done: Callable[[object], object] | None = None,
        on_failed: Callable[[str], object] | None = None,
        on_cancelled: Callable[[], object] | None = None,
    ) -> Job:
        """Run ``work`` in the background.

        ``on_done(result)`` runs on the GUI thread when it succeeds; what it returns becomes
        ``job.outcome`` (see :func:`pdfeditor.ui.jobs.wait_for`). Failures and cancellation
        show a toast unless handlers are given. With ``session`` the document is read-only
        while the job runs, and nothing is delivered if it was closed meanwhile.
        """
        # No Qt parent: the list below keeps it alive while it runs, callers may keep it after
        job = Job(work, label=label, total=total, session=session)
        title = _title(label)

        def failed(message: str) -> object:
            if on_failed is not None:
                return on_failed(message)
            text = QCoreApplication.translate("JobCenter", "{task} failed: {message}")
            return self._notify(text.format(task=title, message=message), "error")

        def cancelled() -> object:
            if on_cancelled is not None:
                return on_cancelled()
            text = QCoreApplication.translate("JobCenter", "{task} was cancelled.")
            return self._notify(text.format(task=title), "info")

        job.progress_changed.connect(lambda done, t: self._on_progress(job, done, t))
        job.status_changed.connect(lambda text: self._on_status(job, text))
        job.finished.connect(lambda result: self._settle(job, on_done, result))
        job.failed.connect(lambda message: self._settle(job, lambda m: failed(str(m)), message))
        job.cancelled.connect(lambda: self._settle(job, lambda _v: cancelled(), None))
        if session is not None:
            session.begin_task(title)
        self._jobs.append(job)
        log.info("job started: %s", label)
        self.changed.emit()
        self.running_changed.emit()
        job.start()
        return job

    def running(self) -> list[Job]:
        return list(self._jobs)

    def busy(self, session: DocumentSession) -> bool:
        return any(job.session is session for job in self._jobs)

    def cancel(self, job: Job) -> None:
        log.info("job cancelled by the user: %s", job.label)
        job.cancel()

    def cancel_for(self, session: DocumentSession | None, wait: bool = True) -> None:
        """Cancel the jobs on ``session`` (all jobs for None) and, with ``wait``, block until
        none of them touches it any more (before the document is closed)."""
        jobs = [j for j in self._jobs if session is None or j.session is session]
        started: list[Job] = []
        for job in jobs:
            with job._state:  # a job not started yet now never will touch the document
                job.cancel()
                if job.started:
                    started.append(job)
        if not wait:
            return
        deadline = time.monotonic() + WAIT_SECONDS
        for job in started:
            while not job.done and time.monotonic() < deadline:
                time.sleep(0.01)
            if not job.done:
                log.warning("job %r didn't stop in time", job.label)

    def cancel_all(self, wait: bool = True) -> None:
        self.cancel_for(None, wait)

    # -- plumbing ---------------------------------------------------------------------------
    def _on_progress(self, job: Job, done: int, total: int) -> None:
        job.done_steps, job.total = done, total
        self.changed.emit()

    def _on_status(self, job: Job, text: str) -> None:
        job.status_text = text
        self.changed.emit()

    def _settle(self, job: Job, handler: Callable[[object], object] | None, value: object) -> None:
        if job not in self._jobs:
            return
        self._jobs.remove(job)
        session = job.session
        if session is not None:
            session.end_task(_title(job.label))
        log.info("job ended: %s", job.label)
        try:
            if session is not None and session.closed:
                return  # the document went away; its job's result has nowhere to go
            if handler is not None:
                job.outcome = handler(value)
            else:
                job.outcome = value
        finally:
            job.is_settled = True
            self.changed.emit()
            self.running_changed.emit()
            job.settled.emit()


class _JobRow(QWidget):
    """One job in the details popup."""

    def __init__(self, job: Job, center: JobCenter, parent: QWidget) -> None:
        super().__init__(parent)
        self.job = job
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(METRICS.space(2))
        grid.setVerticalSpacing(METRICS.space(1))
        title = _title(job.label)
        self.name = QLabel(job_heading(job), self)
        self.name.setObjectName("JobName")
        grid.addWidget(self.name, 0, 0)
        self.cancel = QToolButton(self)
        self.cancel.setText(self.tr("Cancel"))
        self.cancel.setObjectName("JobCancel")
        self.cancel.setAccessibleName(self.tr("Cancel {task}").format(task=title))
        self.cancel.clicked.connect(lambda: self._cancel(center))
        grid.addWidget(self.cancel, 0, 1, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        self.bar = QProgressBar(self)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        grid.addWidget(self.bar, 1, 0)
        self.detail = QLabel(self)
        self.detail.setProperty("role", "muted")
        grid.addWidget(self.detail, 2, 0, 1, 2)
        self.update_row()

    def _cancel(self, center: JobCenter) -> None:
        center.cancel(self.job)
        self.cancel.setEnabled(False)
        self.cancel.setText(self.tr("Cancelling…"))

    def update_row(self) -> None:
        job = self.job
        if is_indeterminate(job):
            self.bar.setRange(0, 0)  # busy indicator
        else:
            self.bar.setRange(0, job.total)
            self.bar.setValue(min(job.done_steps, job.total))
        heading = job_heading(job)
        if self.name.text() != heading:
            self.name.setText(heading)
        self.bar.setAccessibleName(self.tr("{task} progress").format(task=heading))
        elapsed = elapsed_text(time.monotonic() - job.created)
        parts = [progress_text(job), self.tr("{time} elapsed").format(time=elapsed)]
        if job.session is not None:
            parts.insert(0, job.session.display_name)
        self.detail.setText(" · ".join(parts))


class JobDetails(QFrame):
    """Popup listing the running jobs; opens from the progress chip.

    Its window is translucent and it paints its own rounded body (from the tokens, which map
    to the system palette under High Contrast), so no square window corners show behind it.
    """

    def __init__(self, center: JobCenter, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName("JobDetails")
        self.setAccessibleName(self.tr("Running tasks"))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.center = center
        self.rows: list[_JobRow] = []
        m = METRICS
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(m.space(3), m.space(3), m.space(3), m.space(3))
        self._layout.setSpacing(m.space(3))
        heading = QLabel(self.tr("Running tasks"), self)
        heading.setObjectName("JobDetailsTitle")
        self._layout.addWidget(heading)
        self.setMinimumWidth(m.space(80))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)  # elapsed time
        center.changed.connect(self.refresh)
        self.refresh()

    def paintEvent(self, event: QPaintEvent) -> None:
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(colors.border_strong))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(QColor(colors.surface))
        radius = float(METRICS.radius_large)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.end()

    def refresh(self) -> None:
        jobs = self.center.running()
        if not jobs:
            self.close()
            return
        current = [row.job for row in self.rows]
        if current != jobs:
            for row in self.rows:
                self._layout.removeWidget(row)
                row.deleteLater()
            self.rows = [_JobRow(job, self.center, self) for job in jobs]
            for row in self.rows:
                self._layout.addWidget(row)
            self.adjustSize()
        for row in self.rows:
            row.update_row()
        if any(job.status_text for job in jobs):
            self.repaint()  # before the announced step can block the GUI thread (see the chip)


class ProgressChip(QFrame):
    """Status-bar chip: what's running, a progress bar and Cancel. Click for details."""

    def __init__(self, center: JobCenter, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ProgressChip")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.center = center
        self.details: JobDetails | None = None
        m = METRICS
        row = QHBoxLayout(self)
        row.setContentsMargins(m.space(1), 0, 0, 0)
        row.setSpacing(m.space(1))
        self.button = QToolButton(self)
        self.button.setObjectName("ProgressChipButton")
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.button.clicked.connect(self.show_details)
        row.addWidget(self.button)
        self.bar = QProgressBar(self)
        self.bar.setTextVisible(False)
        self.bar.setFixedSize(m.space(18), 6)
        self.bar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row.addWidget(self.bar)
        self.cancel_button = QToolButton(self)
        self.cancel_button.setObjectName("ProgressChipCancel")
        self.cancel_button.setIcon(icon("x"))
        self.cancel_button.setAutoRaise(True)
        self.cancel_button.clicked.connect(self.cancel)
        row.addWidget(self.cancel_button)
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self._reveal)
        self._announced: set[int] = set()
        center.changed.connect(self.update_chip)
        self.hide()
        self.update_chip()

    def update_chip(self) -> None:
        jobs = self.center.running()
        if not jobs:
            self._show_timer.stop()
            self.hide()
            return
        if len(jobs) == 1:
            job = jobs[0]
            text = job_heading(job)
            if job.total > 0 and not job.status_text:
                text += f"  {min(job.done_steps, job.total)}/{job.total}"
            cancel_name = self.tr("Cancel {task}").format(task=_title(job.label))
        else:
            text = self.tr("{count} tasks running").format(count=len(jobs))
            cancel_name = self.tr("Cancel all tasks")
        done = sum(min(j.done_steps, j.total) for j in jobs)
        if any(j.total <= 0 for j in jobs) or done <= 0:
            self.bar.setRange(0, 0)  # busy until there are finished steps to count
        else:
            self.bar.setRange(0, sum(j.total for j in jobs))
            self.bar.setValue(done)
        self.button.setText(text)
        self.button.setToolTip(self.tr("Show running tasks"))
        self.button.setAccessibleName(self.tr("{status}. Show details").format(status=text))
        self.bar.setAccessibleName(self.tr("Progress of {status}").format(status=text))
        self.cancel_button.setToolTip(cancel_name)
        self.cancel_button.setAccessibleName(cancel_name)
        if any(j.status_text for j in jobs):
            # A job that names its step is starting a slow one (an OCR page), which can keep
            # the GUI thread from running anything until it ends (Tesseract holds the GIL).
            # Show the chip and paint it now, not on a timer or a later paint event.
            if not self.isVisible():
                self._show_timer.stop()
                self._reveal()
            self._paint_now()
        elif not self.isVisible() and not self._show_timer.isActive():
            self._show_timer.start(SHOW_DELAY_MS)

    def _paint_now(self) -> None:
        if not self.isVisible():
            return
        parent = self.parentWidget()
        layout = parent.layout() if parent is not None else None
        if layout is not None:
            layout.activate()  # place a chip that was just shown before painting it
        self.repaint()

    def _reveal(self) -> None:
        jobs = self.center.running()
        if not jobs:
            return
        self.show()
        new = [j for j in jobs if id(j) not in self._announced]
        if new:
            self._announced.update(id(j) for j in new)
            text = self.tr("{task} started. Running in the background.")
            announce(self, text.format(task=_title(new[-1].label)))

    def cancel(self) -> None:
        for job in self.center.running():
            self.center.cancel(job)

    def show_details(self) -> JobDetails | None:
        if not self.center.running():
            return None
        if self.details is not None:
            self.details.close()
        details = JobDetails(self.center, self)
        details.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        details.adjustSize()
        anchor = self.mapToGlobal(QPoint(self.width(), 0))
        details.move(anchor.x() - details.width(), anchor.y() - details.height() - 4)
        details.show()
        if details.rows:
            details.rows[0].cancel.setFocus()
        self.details = details
        details.destroyed.connect(lambda _=None: setattr(self, "details", None))
        return details
