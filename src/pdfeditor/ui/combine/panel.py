"""The Combine Files panel: toolbar, a grid of file cards and an empty state to drop files on.

Files are described (page count, first-page preview) by a background :class:`Job` that holds
the engine lock for one file at a time. The panel never merges anything itself: it reports
``combine_requested`` and the controller runs the merge as a job.
"""

from __future__ import annotations

import contextlib
import dataclasses
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSize, Qt, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QImage,
    QKeyEvent,
    QPixmap,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.engine_lock import ENGINE_LOCK
from pdfeditor.engine.base import Engine
from pdfeditor.services.assembly import SUPPORTED_SUFFIXES, SourceInfo, describe_source
from pdfeditor.ui.combine.grid import THUMB, CombineGrid
from pdfeditor.ui.combine.model import CombineModel, EntryState
from pdfeditor.ui.dialogs.pages import PDF_OR_IMAGES
from pdfeditor.ui.icons import icon
from pdfeditor.ui.jobs import Job
from pdfeditor.ui.panels.base import EmptyState
from pdfeditor.ui.style.tokens import METRICS
from pdfeditor.ui.theme import theme_manager
from pdfeditor.ui.view.renderer import to_qimage

Notify = Callable[..., object]


def _tr(text: str) -> str:
    return QCoreApplication.translate("CombinePanel", text)


class CombineDropZone(QFrame):
    """What an empty panel shows: a dashed target for files dragged in from the file manager."""

    files_dropped = Signal(list)  # list[Path]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CombineDropZone")
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)  # the style sheet's dashed border wins
        self.setProperty("dragging", False)
        self.empty = EmptyState(
            "upload",
            self.tr("Drag and drop files here"),
            self.tr("PDFs and images (PNG, JPEG, TIFF…) are combined in the order shown."),
            self,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.empty)

    def _set_dragging(self, on: bool) -> None:
        self.setProperty("dragging", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if CombineModel.local_files(event.mimeData()):
            event.acceptProposedAction()
            self._set_dragging(True)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_dragging(False)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_dragging(False)
        paths = CombineModel.local_files(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)


class CombinePanel(QWidget):
    combine_requested = Signal()
    close_requested = Signal()

    def __init__(self, engine: Engine, notify: Notify, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CombinePanel")
        self.setAccessibleName(self.tr("Combine Files"))
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setAcceptDrops(True)
        self._engine = engine
        self._notify = notify
        self._job: Job | None = None
        self._again = False  # files were added while a job ran: describe them after it
        self._closed = False
        self._busy = False
        self._unlocking: set[int] = set()  # uids the user just gave a password for
        self.model = CombineModel(self)
        m = METRICS

        # toolbar
        self.toolbar = QFrame(self)
        self.toolbar.setObjectName("CombineToolbar")
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(m.space(4), m.space(2), m.space(4), m.space(2))
        bar.setSpacing(m.space(3))
        title = QLabel(self.tr("Combine Files"), self.toolbar)
        title.setObjectName("CombineTitle")
        self.summary = QLabel("", self.toolbar)
        self.summary.setProperty("role", "muted")
        self.add_button = QPushButton(self.tr("Add Files…"), self.toolbar)
        self.add_button.setToolTip(self.tr("Add PDFs and images to combine"))
        self.add_button.setIconSize(QSize(16, 16))
        self.add_button.clicked.connect(self.add_files_dialog)
        self.bookmarks = QCheckBox(self.tr("Add a bookmark for each file"), self.toolbar)
        self.bookmarks.setChecked(True)
        self.combine_button = QPushButton(self.tr("Combine"), self.toolbar)
        self.combine_button.setObjectName("CombineButton")
        self.combine_button.setDefault(True)
        self.combine_button.clicked.connect(self.combine_requested)
        self.close_button = QToolButton(self.toolbar)
        self.close_button.setAccessibleName(self.tr("Close Combine Files"))
        self.close_button.setToolTip(self.tr("Close Combine Files (Esc)"))
        self.close_button.setIconSize(QSize(16, 16))
        self.close_button.clicked.connect(self.close_requested)
        bar.addWidget(title)
        bar.addWidget(self.summary)
        bar.addStretch(1)
        bar.addWidget(self.add_button)
        bar.addWidget(self.bookmarks)
        bar.addWidget(self.combine_button)
        bar.addWidget(self.close_button)

        self.status = QLabel("", self)
        self.status.setProperty("role", "muted")
        self.status.setWordWrap(True)
        self.status.setContentsMargins(m.space(4), m.space(2), m.space(4), 0)

        # content: the empty state, or the cards
        self.grid = CombineGrid(self.model, self)
        self.grid.activated_row.connect(self.activate)
        self.grid.pages_requested.connect(self.choose_pages)
        self.grid.unlock_requested.connect(self.unlock)
        self.grid.files_dropped.connect(lambda paths, at: self.add_paths(paths, at))
        self.drop_zone = CombineDropZone(self)
        self.drop_zone.files_dropped.connect(lambda paths: self.add_paths(paths))
        self.drop_zone.empty.set_action(self.tr("Add Files…"), slot=self.add_files_dialog)
        content = QWidget(self)
        self._stack = QStackedLayout(content)
        self._stack.addWidget(self.drop_zone)
        self._stack.addWidget(self.grid)
        content_frame = QVBoxLayout()
        content_frame.setContentsMargins(m.space(4), m.space(2), m.space(4), m.space(4))
        content_frame.addWidget(content)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.status)
        layout.addLayout(content_frame, 1)

        for first, second in (
            (self.add_button, self.bookmarks),
            (self.bookmarks, self.combine_button),
            (self.combine_button, self.close_button),
            (self.close_button, self.grid),
        ):
            QWidget.setTabOrder(first, second)

        self.model.rowsInserted.connect(self.refresh_state)
        self.model.rowsRemoved.connect(self.refresh_state)
        self.model.modelReset.connect(self.refresh_state)
        self.model.dataChanged.connect(self.refresh_state)
        theme_manager().changed.connect(self._theme_changed)
        self._theme_changed()
        self.refresh_state()

    def _theme_changed(self) -> None:
        self.add_button.setIcon(icon("plus"))
        self.close_button.setIcon(icon("x"))
        self.grid.viewport().update()

    # -- state ----------------------------------------------------------------------------
    def describing(self) -> bool:
        return self._job is not None

    def focus_target(self) -> QWidget:
        return self.grid if self.model.rowCount() else self.drop_zone.empty.button

    def set_busy(self, busy: bool) -> None:
        """While the merge runs the list can't change; only Close (cancel) stays enabled."""
        self._busy = busy
        self.refresh_state()

    def refresh_state(self, *_args: object) -> None:
        model = self.model
        count = model.rowCount()
        busy = self._busy
        self._stack.setCurrentWidget(self.grid if count else self.drop_zone)
        self.grid.setEnabled(not busy)
        self.add_button.setEnabled(not busy)
        self.bookmarks.setEnabled(not busy)
        self.combine_button.setEnabled(model.ready() and not busy)
        self.combine_button.setText(self.tr("Combining…") if busy else self.tr("Combine"))
        if count:
            pages = model.total_pages()
            files = self.tr("1 file") if count == 1 else self.tr("{n} files").format(n=count)
            noun = self.tr("1 page") if pages == 1 else self.tr("{n} pages").format(n=pages)
            self.summary.setText(f"{files} · {noun}")
        else:
            self.summary.setText("")
        locked, error = model.blocking()
        if not count:
            text = ""
        elif locked:
            text = (
                self.tr("Unlock 1 password-protected file, or remove it, to combine.")
                if locked == 1
                else self.tr(
                    "Unlock {n} password-protected files, or remove them, to combine."
                ).format(n=locked)
            )
        elif error:
            text = (
                self.tr("Remove 1 file that can't be opened.")
                if error == 1
                else self.tr("Remove {n} files that can't be opened.").format(n=error)
            )
        elif count < 2:
            text = self.tr("Add at least two files.")
        else:
            text = self.tr(
                "Drag files to change their order. Double-click a PDF to choose its pages."
            )
        self.status.setText(text)
        self.status.setVisible(bool(text))

    # -- adding files ---------------------------------------------------------------------
    def add_paths(self, paths: Iterable[Path], at: int | None = None) -> int:
        """Queue PDFs and images (before row ``at``, default the end); says how many other
        files were skipped. Returns how many were added."""
        chosen = [Path(p) for p in paths]
        usable = [p for p in chosen if p.suffix.lower() in SUPPORTED_SUFFIXES]
        skipped = len(chosen) - len(usable)
        if skipped:
            self._notify(
                self.tr("1 file skipped: only PDFs and images can be combined.")
                if skipped == 1
                else self.tr("{n} files skipped: only PDFs and images can be combined.").format(
                    n=skipped
                )
            )
        added = self.model.add_paths(usable, at)
        if added:
            if not self.grid.currentIndex().isValid():
                first = self.model.index(self.model.rowCount() - added if at is None else at)
                self.grid.selectionModel().setCurrentIndex(
                    first, self.grid.selectionModel().SelectionFlag.NoUpdate
                )
            self._describe()
        return added

    def add_files_dialog(self) -> None:
        entries = self.model.entries
        start = str(entries[-1].path.parent) if entries else ""
        chosen, _ = QFileDialog.getOpenFileNames(self, self.tr("Add Files"), start, PDF_OR_IMAGES)
        self.add_paths(Path(p) for p in chosen)

    # -- per-file actions -----------------------------------------------------------------
    def activate(self, row: int) -> None:
        if not 0 <= row < self.model.rowCount():
            return
        entry = self.model.entry(row)
        if entry.state is EntryState.LOCKED:
            self.unlock(row)
        elif entry.state is EntryState.READY and not entry.image:
            self.choose_pages(row)

    def unlock(self, row: int) -> None:
        entry = self.model.entry(row)
        password, ok = QInputDialog.getText(
            self,
            self.tr("Unlock"),
            self.tr("Password for “{name}”:").format(name=entry.path.name),
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return
        self.model.set_password(row, password)
        self._unlocking.add(entry.uid)
        self._describe()

    def choose_pages(self, row: int) -> None:
        entry = self.model.entry(row)
        if entry.image or entry.state is not EntryState.READY:
            return
        text = entry.pages
        while True:
            text, ok = QInputDialog.getText(
                self,
                self.tr("Choose Pages"),
                self.tr("Pages to include from “{name}” (1-{n}; empty = all):").format(
                    name=entry.path.name, n=entry.page_count
                ),
                QLineEdit.EchoMode.Normal,
                text,
            )
            if not ok:
                return
            try:
                self.model.set_pages(row, text)
            except ValueError as exc:
                QMessageBox.warning(self, self.tr("Choose Pages"), str(exc))
                continue
            return

    # -- describing files (background) ----------------------------------------------------
    def _describe(self) -> None:
        if self._closed:
            return
        if self._job is not None:
            self._again = True
            return
        pending = [(e.uid, e.path, e.password) for e in self.model.pending()]
        if not pending:
            return
        engine = self._engine
        ratio = self.devicePixelRatioF()
        width, height = round(THUMB.width() * ratio), round(THUMB.height() * ratio)

        def work(job: Job) -> None:
            for uid, path, password in pending:
                job.token.check()
                with ENGINE_LOCK:  # one file at a time, so the GUI is never kept waiting
                    info = describe_source(engine, path, width, height, password)
                image = to_qimage(info.preview) if info.preview is not None else None
                job.report((uid, password, dataclasses.replace(info, preview=None), image))

        job = Job(work)
        job.partial.connect(self._described)
        job.finished.connect(self._job_done)
        job.failed.connect(self._job_done)
        job.cancelled.connect(self._job_done)
        self._job = job
        job.start()

    def _described(self, value: tuple[int, str | None, SourceInfo, QImage | None]) -> None:
        uid, password, info, image = value
        pixmap: QPixmap | None = None
        if image is not None:
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(self.devicePixelRatioF())
        applied = self.model.update(uid, info, pixmap, password)
        if applied and uid in self._unlocking:
            self._unlocking.discard(uid)
            if info.locked:
                self._notify(self.tr("Incorrect password."), "error")
        self.refresh_state()

    def _job_done(self, *_args: object) -> None:
        self._job = None
        if self._again and not self._closed:
            self._again = False
            self._describe()
        self.refresh_state()

    # -- lifecycle ------------------------------------------------------------------------
    def clear(self) -> None:
        self._unlocking.clear()
        self.model.clear()
        self.bookmarks.setChecked(True)

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Stop the describing job and wait for it: its signals must not outlive the panel."""
        self._closed = True
        job = self._job
        if job is None:
            return
        job.cancel()
        deadline = time.monotonic() + timeout_ms / 1000
        while not job.done and time.monotonic() < deadline:
            time.sleep(0.005)
        for signal in (job.partial, job.finished, job.failed, job.cancelled):
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect()
        self._job = None

    # -- events ---------------------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if CombineModel.local_files(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = CombineModel.local_files(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.add_paths(paths)
