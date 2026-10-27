"""Combine Files: the panel in the main area, and the merge it starts as a background job."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QCoreApplication

from pdfeditor.core.engine_lock import ENGINE_LOCK
from pdfeditor.engine.base import Document
from pdfeditor.services.assembly import merge
from pdfeditor.ui.combine.panel import CombinePanel
from pdfeditor.ui.jobs import Job

if TYPE_CHECKING:
    from pdfeditor.ui.main_window import MainWindow


def _tr(text: str) -> str:
    return QCoreApplication.translate("CombineController", text)


class CombineController:
    """Owns the Combine panel. While it is open it replaces the page area (and the side
    panels, which belong to documents); the list of files survives hiding it."""

    def __init__(self, window: MainWindow) -> None:
        self.w = window
        self.panel = CombinePanel(window.engine(), window.notify, window)
        self.panel.hide()
        self.panel.combine_requested.connect(self.combine)
        self.panel.close_requested.connect(self.close_panel)
        self._active = False
        self._docks: tuple[bool, bool] = (False, False)
        self._job: Job | None = None

    @property
    def active(self) -> bool:
        return self._active

    def open_panel(self, paths: Iterable[Path] = ()) -> None:
        w = self.w
        if not self._active:
            w.set_tool("select")  # nothing may still draw on a page while the panel is up
            self._docks = (w.nav_dock.isVisible(), w.inspector_dock.isVisible())
            w.nav_dock.hide()
            w.inspector_dock.hide()
            self._active = True
            w._update_ui()
        self.panel.add_paths(paths)
        self.panel.focus_target().setFocus()

    def hide_panel(self) -> None:
        """Back to the documents (or the start page); the files stay in the list."""
        if not self._active:
            return
        self._active = False
        self._restore_docks()
        self.w._update_ui()
        self.w.focus_page()

    def close_panel(self) -> None:
        """Close: cancels a merge in progress and forgets the files."""
        job = self._job
        if job is not None:
            job.cancel()
        self.panel.clear()
        self.hide_panel()

    def _restore_docks(self) -> None:
        nav, inspector = self._docks
        self.w.nav_dock.setVisible(nav)
        self.w.inspector_dock.setVisible(inspector)

    def combine(self) -> Job | None:
        """Merge the listed files into a new unsaved document, in the background."""
        panel = self.panel
        if not panel.model.ready() or self._job is not None:
            return None
        sources = panel.model.sources()
        bookmarks = panel.bookmarks.isChecked()
        engine = self.w.engine()
        panel.set_busy(True)

        def work(job: Job) -> Document:
            with job.hold(ENGINE_LOCK):
                return merge(engine, sources, bookmarks, job.token, job.progress)

        def done(result: object) -> object:
            self._job = None
            panel.set_busy(False)
            doc = cast(Document, result)
            with ENGINE_LOCK:
                pages = doc.page_count
                if pages == 0:
                    doc.close()
            if pages == 0:
                self.w.notify(_tr("Nothing to combine."), "error")
                return None
            view = self.w.add_document(doc, "Combined.pdf")
            files = len(sources)
            panel.clear()
            self.hide_panel()
            self.w.notify(
                _tr("Combined {files} files into {pages} pages").format(files=files, pages=pages),
                "success",
            )
            return view.session

        def failed(message: str) -> None:
            self._job = None
            panel.set_busy(False)
            text = _tr("Couldn't combine the files: {message}").format(message=message)
            self.w.notify(text, "error")

        def cancelled() -> None:
            self._job = None
            panel.set_busy(False)
            self.w.notify(_tr("Combining was cancelled."))

        self._job = self.w.jobs.start(
            _tr("Combining files…"),
            work,
            total=len(sources),
            on_done=done,
            on_failed=failed,
            on_cancelled=cancelled,
        )
        return self._job

    def shutdown(self) -> None:
        """The window is closing: stop the panel's job and put the docks back, so the saved
        window state holds the user's layout."""
        self.panel.shutdown()
        if self._active:
            self._active = False
            self._restore_docks()
