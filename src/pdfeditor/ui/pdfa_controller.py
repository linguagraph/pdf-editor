"""Tools ▸ PDF/A and accessibility: preflight the open document, save a PDF/A-2b copy, run the
accessibility check and (experimental) auto-tag an untagged document."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox

from pdfeditor.core.commands import SnapshotCommand
from pdfeditor.core.commands.snapshot import SNAPSHOT_OPTIONS
from pdfeditor.engine.base import SaveOptions
from pdfeditor.model.metadata import EncryptionMethod
from pdfeditor.services.pdfa import (
    ConversionResult,
    Issue,
    convert_to_pdfa,
    find_verapdf,
    preflight,
    validate_with_verapdf,
)
from pdfeditor.ui.dialogs.pdfa import PdfaReportDialog
from pdfeditor.ui.icons import icon
from pdfeditor.ui.jobs import Job, run_modal

if TYPE_CHECKING:
    from pdfeditor.core.session import DocumentSession
    from pdfeditor.ui.main_window import MainWindow


class PdfaController:
    def __init__(self, window: MainWindow) -> None:
        self.w = window

        def act(text: str, icon_name: str, slot: object) -> QAction:
            a = QAction(icon(icon_name), text, window)
            a.triggered.connect(lambda _=False: slot())  # type: ignore[operator]
            a.setProperty("needs_doc", True)
            window.addAction(a)
            return a

        self.act_preflight = act("PDF/A &Preflight…", "list-checks", self.preflight)
        self.act_save_pdfa = act("Save as PDF/&A…", "archive", self.save_as_pdfa)
        self.act_accessibility = act(
            "Accessibility &Check", "accessibility", window.accessibility_check
        )
        self.act_auto_tag = act("Auto-&Tag Document (experimental)", "tags", self.auto_tag)
        self.act_auto_tag.setIconText("Auto-Tag")
        self.act_auto_tag.setVisible(window.engine().capabilities.auto_tag)
        self.last_message = ""
        self.dialog: PdfaReportDialog | None = None

    def ribbon(self) -> None:
        self.w.ribbon.tab("Tools").add_group(
            self.act_preflight,
            self.act_save_pdfa,
            self.act_accessibility,
            self.act_auto_tag,
            title="Standards",
        )
        # the Tools menu is built before the ribbon; list auto-tag next to the accessibility check
        for menu_action in self.w.menu_bar.actions():
            menu = menu_action.menu()
            if isinstance(menu, QMenu) and self.act_accessibility in menu.actions():
                menu.addAction(self.act_auto_tag)

    def _session(self) -> DocumentSession | None:
        view = self.w.current_view()
        return view.session if view is not None else None

    def _plain_bytes(self, session: DocumentSession) -> tuple[bytes, bool]:
        with session.lock:
            doc = session.document
            encrypted = doc.info().encryption is not EncryptionMethod.NONE
            return doc.to_bytes(SaveOptions(decrypt=True)), encrypted

    def _verapdf(self, path: Path) -> tuple[bool, str] | None:
        try:
            outcome = run_modal(
                self.w, "Validating with veraPDF…", lambda _j: validate_with_verapdf(path)
            )
        except RuntimeError as exc:
            QMessageBox.warning(self.w, "veraPDF", str(exc))
            return None
        return outcome if isinstance(outcome, tuple) else None

    def preflight(self, show: bool = True) -> list[Issue] | None:
        session = self._session()
        if session is None:
            return None
        fonts = self.w.engine().standard_font_program

        def work(_job: Job) -> list[Issue]:
            data, encrypted = self._plain_bytes(session)
            return preflight(data, fonts, encrypted)

        try:
            issues = run_modal(self.w, "Checking PDF/A-2b…", work)
        except RuntimeError as exc:
            QMessageBox.warning(self.w, "PDF/A Preflight", str(exc))
            return None
        if not isinstance(issues, list):
            return None
        if show:
            fixable = all(i.fixable for i in issues)
            headline = (
                "No PDF/A-2b problems found by the built-in checks."
                if not issues
                else f"{len(issues)} problem(s) found. "
                + (
                    "All can be fixed by saving as PDF/A."
                    if fixable
                    else "Some need attention before the file can be PDF/A."
                )
            )
            saved = session.path if session.path and not session.is_dirty else None
            self.dialog = PdfaReportDialog(
                "PDF/A Preflight",
                headline,
                issues,
                on_convert=self.save_as_pdfa if issues else None,
                on_verapdf=(lambda: self._verapdf(saved)) if saved and find_verapdf() else None,
                parent=self.w,
            )
            self.dialog.show()
        return issues

    def save_as_pdfa(
        self, target: Path | None = None, show: bool = True
    ) -> ConversionResult | None:
        session = self._session()
        if session is None:
            return None
        with session.lock:
            encrypted = session.document.info().encryption is not EncryptionMethod.NONE
        if encrypted and not self.w.protect.owner_access(session, None):
            return None  # PDF/A removes the encryption: that needs the owner password
        if target is None:
            base = session.path or Path.home() / session.display_name
            chosen, _ = QFileDialog.getSaveFileName(
                self.w,
                "Save as PDF/A",
                str(base.with_name(base.stem + "-PDFA.pdf")),
                "PDF documents (*.pdf)",
            )
            if not chosen:
                return None
            target = Path(chosen)
        target = target.with_suffix(".pdf")
        if session.path is not None and target.resolve() == session.path.resolve():
            QMessageBox.warning(
                self.w, "Save as PDF/A", "Choose a different file name: the open document is kept."
            )
            return None
        fonts = self.w.engine().standard_font_program

        def work(_job: Job) -> ConversionResult:
            data, _encrypted = self._plain_bytes(session)
            return convert_to_pdfa(data, fonts)

        try:
            result = run_modal(self.w, "Converting to PDF/A-2b…", work)
        except RuntimeError as exc:
            QMessageBox.warning(self.w, "Save as PDF/A", f"Conversion failed:\n\n{exc}")
            return None
        if not isinstance(result, ConversionResult):
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(result.data)
        tmp.replace(target)
        self.last_message = (
            f"Saved PDF/A-2b copy: {target.name}"
            if result.conforming
            else f"Saved {target.name}, but it isn't PDF/A yet: {len(result.remaining)} problem(s)"
        )
        self.w.statusBar().showMessage(self.last_message, 10000)
        if show:
            headline = (
                f"Saved {target.name} as PDF/A-2b."
                if result.conforming
                else f"Saved {target.name}, but these problems still block PDF/A-2b."
            )
            self.dialog = PdfaReportDialog(
                "Save as PDF/A",
                headline,
                result.remaining,
                result.fixed,
                on_verapdf=(lambda: self._verapdf(target)) if find_verapdf() else None,
                parent=self.w,
            )
            self.dialog.show()
        return result

    def auto_tag(self) -> dict[str, int] | None:
        """Tag an untagged document (undoable). The tagging runs on a copy in a background job;
        the result then replaces the document through a snapshot command."""
        session = self._session()
        if session is None:
            return None
        title = "Auto-Tag Document"
        with session.lock:
            tagged = session.document.info().is_tagged
        if tagged:
            self.last_message = (
                "The document is already tagged. Auto-tagging only works on untagged documents; "
                "edit the existing tags in the Tags panel instead."
            )
            QMessageBox.information(self.w, title, self.last_message)
            return None
        doc, lock = session.document, session.lock

        def work(_job: Job) -> tuple[bytes, dict[str, int]]:
            with lock:
                copy = doc.copy()
                try:
                    counts = copy.auto_tag()
                    return copy.to_bytes(SNAPSHOT_OPTIONS), counts
                finally:
                    copy.close()

        try:
            outcome = run_modal(self.w, "Tagging the document…", work)
        except RuntimeError as exc:
            self.last_message = f"Auto-tagging failed:\n\n{exc}"
            QMessageBox.warning(self.w, title, self.last_message)
            return None
        if not isinstance(outcome, tuple):
            return None  # cancelled
        data, counts = outcome
        session.execute(SnapshotCommand(title, lambda d: d.load_state(data), session.snapshots))
        summary = ", ".join(f"{n} {tag}" for tag, n in sorted(counts.items()))
        self.last_message = (
            f"Tagged the document: {summary}. Review the tags and give figures alternate text."
        )
        self.w.statusBar().showMessage(self.last_message, 10000)
        return counts
