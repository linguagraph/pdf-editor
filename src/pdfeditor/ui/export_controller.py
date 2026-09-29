"""Export and conversion commands: export to Word/Excel/HTML/Markdown/text/images, extract
images and fonts, and create a PDF from an Office file (with LibreOffice)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

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
from pdfeditor.ui.jobs import Job, run_modal

if TYPE_CHECKING:
    from pdfeditor.core.session import DocumentSession
    from pdfeditor.ui.document_tab import DocumentTab
    from pdfeditor.ui.main_window import MainWindow
    from pdfeditor.ui.view.document_view import DocumentView

T = TypeVar("T")


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

            def export_as(f: ExportFormat = fmt) -> list[Path]:
                return self.export(fmt=f)

            self.format_actions[fmt] = act(f"{fmt.label}…", "file-output", export_as)
        self.act_extract_images = act("Extract &Images…", "images", self.extract_images)
        self.act_extract_fonts = act("Extract &Fonts…", "a-large-small", self.extract_fonts)
        self.act_from_office = act(
            "Create PDF from &Office File…", "file-input", self.from_office, False
        )
        self.act_from_office.setToolTip(
            "Word, Excel, PowerPoint and OpenDocument files; needs LibreOffice installed"
        )
        self.last_message = ""

    def ribbon(self) -> None:
        r = self.w.ribbon.tab("Tools")
        r.add_group(self.act_export, self.act_extract_images, self.act_extract_fonts)
        r.add_group(self.act_from_office)

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

    def _report(self, message: str) -> None:
        self.last_message = message
        self.w.statusBar().showMessage(message, 8000)

    def _run(self, label: str, work: Callable[[Job], object], total: int = 0) -> object:
        return run_modal(self.w, label, work, total)

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
    ) -> list[Path]:
        current = self._current()
        if current is None:
            return []
        tab, view, session = current
        if dialog is None:
            dialog = ExportDialog(
                view.page_count, view.current_page, tab.target_pages(), session.path, fmt, self.w
            )
        if not dialog.result() and not dialog.exec():
            return []
        pages = checked_pages(self.w, dialog.range)
        if not pages:
            return []
        chosen, target = dialog.format(), dialog.target_path()
        doc, lock = session.document, session.lock
        try:
            if chosen is ExportFormat.EXCEL:
                return self._export_tables(session, pages, target, picker)
            if chosen.is_image:
                options = dialog.image_options()
                result = self._run(
                    "Exporting pages…",
                    lambda job: _locked(
                        lock,
                        lambda: export_pages(doc, pages, target, options, job.token, job.progress),
                    ),
                    len(pages),
                )
                written = result if isinstance(result, list) else []
            else:
                text_format = chosen.text_format
                assert text_format is not None
                # the vector-art background comes from a copy with the text deleted
                structure = replace(
                    dialog.structure_options(),
                    vector_background=session.engine.capabilities.content_edit,
                )
                result = self._run(
                    f"Exporting to {chosen.label}…",
                    lambda job: _locked(
                        lock,
                        lambda: export_document(
                            doc,
                            pages,
                            target,
                            text_format,
                            structure,
                            job.token,
                            job.progress,
                            session.engine.text_width,
                        ),
                    ),
                    len(pages),
                )
                written = [result] if isinstance(result, Path) else []
        except (RuntimeError, OSError) as exc:
            QMessageBox.warning(self.w, "Export", f"Export failed:\n\n{exc}")
            return []
        if written:
            where = written[0] if len(written) == 1 else written[0].parent
            self._report(f"Exported {len(pages)} page(s) to {where}")
        return written

    def _export_tables(
        self,
        session: DocumentSession,
        pages: list[int],
        target: Path,
        picker: TablePickerDialog | None,
    ) -> list[Path]:
        doc, lock = session.document, session.lock
        found = self._run(
            "Finding tables…",
            lambda job: _locked(lock, lambda: find_tables(doc, pages, job.token, job.progress)),
            len(pages),
        )
        tables = found if isinstance(found, list) else []
        if found is None:
            return []  # cancelled
        if not tables:
            QMessageBox.information(
                self.w, "Export Tables", "No tables were found on the chosen pages."
            )
            self._report("No tables found")
            return []
        picker = picker or TablePickerDialog(tables, self.w)
        if not picker.result() and not picker.exec():
            return []
        chosen = picker.chosen()
        if not chosen:
            return []
        path = tables_to_xlsx(chosen, target)
        self._report(f"Exported {len(chosen)} table(s) to {path}")
        return [path]

    # -- extraction -------------------------------------------------------------------------
    def _folder(self, title: str, folder: Path | None) -> Path | None:
        if folder is not None:
            return folder
        current = self._current()
        start = str(current[2].path.parent) if current and current[2].path else ""
        chosen = QFileDialog.getExistingDirectory(self.w, title, start)
        return Path(chosen) if chosen else None

    def extract_images(self, folder: Path | None = None) -> list[Path]:
        current = self._current()
        if current is None:
            return []
        session = current[2]
        target = self._folder("Extract Images To", folder)
        if target is None:
            return []
        doc, lock = session.document, session.lock
        try:
            result = self._run(
                "Extracting images…",
                lambda job: _locked(
                    lock,
                    lambda: extract_images(doc, target, token=job.token, progress=job.progress),
                ),
            )
        except (RuntimeError, OSError) as exc:
            QMessageBox.warning(self.w, "Extract Images", str(exc))
            return []
        written = result if isinstance(result, list) else []
        self._report(f"Extracted {len(written)} image(s) to {target}")
        return written

    def extract_fonts(self, folder: Path | None = None) -> list[Path]:
        current = self._current()
        if current is None:
            return []
        session = current[2]
        target = self._folder("Extract Fonts To", folder)
        if target is None:
            return []
        doc, lock = session.document, session.lock
        try:
            result = self._run(
                "Extracting fonts…",
                lambda job: _locked(
                    lock, lambda: extract_fonts(doc, target, job.token, job.progress)
                ),
            )
        except (RuntimeError, OSError) as exc:
            QMessageBox.warning(self.w, "Extract Fonts", str(exc))
            return []
        written, skipped = result if isinstance(result, tuple) else ([], [])
        message = f"Extracted {len(written)} font(s) to {target}"
        if skipped:
            message += f"; {len(skipped)} not embedded ({', '.join(sorted(set(skipped))[:5])})"
        self._report(message)
        return list(written)

    # -- Office import ----------------------------------------------------------------------
    def from_office(self, path: Path | None = None) -> DocumentView | None:
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
        try:
            data = self._run(f"Converting {source.name}…", lambda _job: convert_to_pdf(source))
        except RuntimeError as exc:
            QMessageBox.warning(self.w, "Create PDF from Office File", str(exc))
            return None
        if not isinstance(data, bytes):
            return None
        view = self.w.add_document(self.w.engine().open(data), source.stem + ".pdf")
        self._report(f"Created a PDF from {source.name}")
        return view


def _locked(lock: AbstractContextManager[object], fn: Callable[[], T]) -> T:
    """Hold the engine lock for the whole job (the progress dialog keeps the UI modal)."""
    with lock:
        return fn()
