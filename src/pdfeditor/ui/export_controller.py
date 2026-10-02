"""Export and conversion commands: export to Word/Excel/HTML/Markdown/text/images, extract
images and fonts, and create a PDF from an Office file (with LibreOffice)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFileDialog, QMessageBox

from pdfeditor.services.export import export_document, find_tables, tables_to_xlsx
from pdfeditor.services.export.images import export_pages, extract_fonts, extract_images
from pdfeditor.services.export.office import (
    INSTALL_HINT,
    OFFICE_SUFFIXES,
    convert_to_pdf,
    find_soffice,
)
from pdfeditor.ui.dialogs.export import ExportDialog, ExportFormat, TablePickerDialog
from pdfeditor.ui.dialogs.pages import checked_pages
from pdfeditor.ui.icons import icon
from pdfeditor.ui.jobs import Job
from pdfeditor.ui.toasts import folder_action

if TYPE_CHECKING:
    from pdfeditor.core.session import DocumentSession
    from pdfeditor.ui.document_tab import DocumentTab
    from pdfeditor.ui.main_window import MainWindow
    from pdfeditor.ui.view.document_view import DocumentView


class ExportController:
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

        self.act_export = act("&Export PDF…", "file-output", self.export)
        self.format_actions: dict[ExportFormat, QAction] = {}
        for fmt in ExportFormat:

            def export_as(f: ExportFormat = fmt) -> Job | None:
                return self.export(fmt=f)

            self.format_actions[fmt] = act(f"{fmt.label}…", "file-output", export_as)
        self.act_extract_images = act("Extract &Images…", "images", self.extract_images)
        self.act_extract_fonts = act("Extract &Fonts…", "a-large-small", self.extract_fonts)
        self.act_from_office = act(
            "Create PDF from &Office File…", "file-input", self.from_office, False
        )
        self.last_message = ""

    def ribbon(self) -> None:
        r = self.w.ribbon.tab("Tools")
        r.add_group(
            self.act_export, self.act_extract_images, self.act_extract_fonts, title="Export"
        )
        r.add_group(self.act_from_office, title="Create")

    def fill_menu(self, menu: object) -> None:
        from PySide6.QtWidgets import QMenu

        assert isinstance(menu, QMenu)
        sub = menu.addMenu("Export &To")
        for fmt, action in self.format_actions.items():
            sub.addAction(action)
            if fmt in (ExportFormat.TEXT,):
                sub.addSeparator()
        menu.addAction(self.act_extract_images)
        menu.addAction(self.act_extract_fonts)

    def _report(self, message: str, written: Path | None = None) -> None:
        self.last_message = message
        action = folder_action(written) if written is not None else None
        self.w.notify(message, "success" if written is not None else "info", action)

    def _run(
        self,
        label: str,
        work: Callable[[Job], object],
        total: int = 0,
        session: DocumentSession | None = None,
        on_done: Callable[[object], object] | None = None,
        failure: str = "",
    ) -> Job:
        """Start ``work`` as a background job (a failure shows ``failure``: the message)."""
        title = failure or label.rstrip("…")

        def failed(message: str) -> None:
            self.w.notify(f"{title} failed: {message}", "error")

        return self.w.jobs.start(
            label, work, total=total, session=session, on_done=on_done, on_failed=failed
        )

    def _current(self) -> tuple[DocumentTab, DocumentView, DocumentSession] | None:
        tab = self.w.current_tab()
        if tab is None:
            return None
        return tab, tab.view, tab.view.session

    # -- export -----------------------------------------------------------------------------
    def export(
        self,
        dialog: ExportDialog | None = None,
        fmt: ExportFormat = ExportFormat.WORD,
        picker: TablePickerDialog | None = None,
    ) -> Job | None:
        """Export in the background; the job's outcome is the list of files written."""
        current = self._current()
        if current is None:
            return None
        tab, view, session = current
        if dialog is None:
            dialog = ExportDialog(
                view.page_count, view.current_page, tab.target_pages(), session.path, fmt, self.w
            )
        if not dialog.result() and not dialog.exec():
            return None
        pages = checked_pages(self.w, dialog.range)
        if not pages:
            return None
        chosen, target = dialog.format(), dialog.target_path()
        doc, lock = session.document, session.lock
        if chosen is ExportFormat.EXCEL:
            return self._export_tables(session, pages, target, picker)

        def done(result: object) -> list[Path]:
            if isinstance(result, list):
                written = result
            else:
                written = [result] if isinstance(result, Path) else []
            if written:
                where = written[0] if len(written) == 1 else written[0].parent
                self._report(f"Exported {len(pages)} page(s) to {where}", written[0])
            return written

        if chosen.is_image:
            options = dialog.image_options()

            def pictures(job: Job) -> list[Path]:
                with job.hold(lock):
                    return export_pages(doc, pages, target, options, job.token, job.progress)

            return self._run(
                "Exporting pages…", pictures, len(pages), session, done, failure="Export"
            )
        text_format = chosen.text_format
        assert text_format is not None
        # the vector-art background comes from a copy with the text deleted
        structure = replace(
            dialog.structure_options(),
            vector_background=session.engine.capabilities.content_edit,
        )

        def document(job: Job) -> Path:
            with job.hold(lock):
                return export_document(
                    doc,
                    pages,
                    target,
                    text_format,
                    structure,
                    job.token,
                    job.progress,
                    session.engine.text_width,
                )

        return self._run(
            f"Exporting to {chosen.label}…", document, len(pages), session, done, failure="Export"
        )

    def _export_tables(
        self,
        session: DocumentSession,
        pages: list[int],
        target: Path,
        picker: TablePickerDialog | None,
    ) -> Job:
        doc, lock = session.document, session.lock

        def work(job: Job) -> list[object]:
            with job.hold(lock):
                return list(find_tables(doc, pages, job.token, job.progress))

        def done(found: object) -> list[Path]:
            tables = found if isinstance(found, list) else []
            if not tables:
                QMessageBox.information(
                    self.w, "Export Tables", "No tables were found on the chosen pages."
                )
                self._report("No tables found")
                return []
            dialog = picker or TablePickerDialog(tables, self.w)
            if not dialog.result() and not dialog.exec():
                return []
            chosen = dialog.chosen()
            if not chosen:
                return []
            try:
                path = tables_to_xlsx(chosen, target)
            except OSError as exc:
                self.w.notify(f"Export failed: {exc}", "error")
                return []
            self._report(f"Exported {len(chosen)} table(s) to {path}", path)
            return [path]

        return self._run("Finding tables…", work, len(pages), session, done, failure="Export")

    # -- extraction -------------------------------------------------------------------------
    def _folder(self, title: str, folder: Path | None) -> Path | None:
        if folder is not None:
            return folder
        current = self._current()
        start = str(current[2].path.parent) if current and current[2].path else ""
        chosen = QFileDialog.getExistingDirectory(self.w, title, start)
        return Path(chosen) if chosen else None

    def extract_images(self, folder: Path | None = None) -> Job | None:
        """Save the document's images; the job's outcome is the list of files written."""
        current = self._current()
        if current is None:
            return None
        session = current[2]
        target = self._folder("Extract Images To", folder)
        if target is None:
            return None
        doc, lock = session.document, session.lock

        def work(job: Job) -> list[Path]:
            with job.hold(lock):
                return extract_images(doc, target, token=job.token, progress=job.progress)

        def done(result: object) -> list[Path]:
            written = result if isinstance(result, list) else []
            self._report(
                f"Extracted {len(written)} image(s) to {target}", target if written else None
            )
            return written

        return self._run("Extracting images…", work, 0, session, done)

    def extract_fonts(self, folder: Path | None = None) -> Job | None:
        """Save the embedded fonts; the job's outcome is the list of files written."""
        current = self._current()
        if current is None:
            return None
        session = current[2]
        target = self._folder("Extract Fonts To", folder)
        if target is None:
            return None
        doc, lock = session.document, session.lock

        def work(job: Job) -> tuple[list[Path], list[str]]:
            with job.hold(lock):
                return extract_fonts(doc, target, job.token, job.progress)

        def done(result: object) -> list[Path]:
            written, skipped = result if isinstance(result, tuple) else ([], [])
            message = f"Extracted {len(written)} font(s) to {target}"
            if skipped:
                message += f"; {len(skipped)} not embedded ({', '.join(sorted(set(skipped))[:5])})"
            self._report(message, target if written else None)
            return list(written)

        return self._run("Extracting fonts…", work, 0, session, done)

    # -- Office import ----------------------------------------------------------------------
    def from_office(self, path: Path | None = None) -> Job | None:
        """Convert with LibreOffice in the background; the outcome is the new document's view."""
        if find_soffice() is None:
            QMessageBox.information(self.w, "Create PDF from Office File", INSTALL_HINT)
            return None
        if path is None:
            patterns = " ".join(f"*{s}" for s in OFFICE_SUFFIXES)
            chosen, _ = QFileDialog.getOpenFileName(
                self.w, "Create PDF from Office File", "", f"Office documents ({patterns})"
            )
            if not chosen:
                return None
            path = Path(chosen)
        source = path

        def done(data: object) -> DocumentView | None:
            if not isinstance(data, bytes):
                return None
            view = self.w.add_document(self.w.engine().open(data), source.stem + ".pdf")
            self._report(f"Created a PDF from {source.name}")
            return view

        return self._run(
            f"Converting {source.name}…",
            lambda _job: convert_to_pdf(source),
            on_done=done,
            failure="Creating the PDF",
        )
