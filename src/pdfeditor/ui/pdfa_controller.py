"""Tools ▸ PDF/A and accessibility: preflight the open document, save a PDF/A-2b copy, run the
accessibility check and (experimental) auto-tag an untagged document."""

from __future__ import annotations

from collections.abc import Callable
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
from pdfeditor.ui.jobs import Job
from pdfeditor.ui.toasts import folder_action, undo_action

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

    def _verapdf(self, path: Path, deliver: Callable[[tuple[bool, str]], object]) -> Job:
        """Validate a saved file with veraPDF in the background, then ``deliver`` its verdict
        (to the report dialog that asked)."""

        def done(outcome: object) -> object:
            if isinstance(outcome, tuple):
                deliver(outcome)
            return outcome

        return self.w.jobs.start(
            "Validating with veraPDF…", lambda _j: validate_with_verapdf(path), on_done=done
        )

    def preflight(self, show: bool = True) -> Job | None:
        """Check the document against PDF/A-2b; the job's outcome is the list of issues."""
        session = self._session()
        if session is None:
            return None
        fonts = self.w.engine().standard_font_program

        def work(_job: Job) -> list[Issue]:
            data, encrypted = self._plain_bytes(session)
            return preflight(data, fonts, encrypted)

        return self.w.jobs.start(
            "Checking PDF/A-2b…",
            work,
            session=session,
            on_done=lambda issues: self._show_preflight(session, issues, show),
        )

    def _show_preflight(
        self, session: DocumentSession, issues: object, show: bool
    ) -> list[Issue] | None:
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
                on_verapdf=(
                    (lambda deliver: self._verapdf(saved, deliver))
                    if saved and find_verapdf()
                    else None
                ),
                parent=self.w,
            )
            self.dialog.show()
        return issues

    def save_as_pdfa(self, target: Path | None = None, show: bool = True) -> Job | None:
        """Save a PDF/A-2b copy; the job's outcome is the :class:`ConversionResult`."""
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

        saved_as = target
        return self.w.jobs.start(
            "Converting to PDF/A-2b…",
            work,
            session=session,
            on_done=lambda result: self._write_pdfa(result, saved_as, show),
        )

    def _write_pdfa(self, result: object, target: Path, show: bool) -> ConversionResult | None:
        if not isinstance(result, ConversionResult):
            return None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(result.data)
            tmp.replace(target)
        except OSError as exc:
            self.w.notify(f"Couldn't save {target.name}: {exc}", "error")
            return None
        self.last_message = (
            f"Saved PDF/A-2b copy: {target.name}"
            if result.conforming
            else f"Saved {target.name}, but it isn't PDF/A yet: {len(result.remaining)} problem(s)"
        )
        self.w.notify(
            self.last_message, "success" if result.conforming else "info", folder_action(target)
        )
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
                on_verapdf=(
                    (lambda deliver: self._verapdf(target, deliver)) if find_verapdf() else None
                ),
                parent=self.w,
            )
            self.dialog.show()
        return result

    def auto_tag(self) -> Job | None:
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

        def work(job: Job) -> tuple[bytes, dict[str, int]]:
            with job.hold(lock):
                copy = doc.copy()
                try:
                    counts = copy.auto_tag()
                    return copy.to_bytes(SNAPSHOT_OPTIONS), counts
                finally:
                    copy.close()

        def done(outcome: object) -> dict[str, int] | None:
            if not isinstance(outcome, tuple):
                return None
            data, counts = outcome
            session.execute(SnapshotCommand(title, lambda d: d.load_state(data), session.snapshots))
            summary = ", ".join(f"{n} {tag}" for tag, n in sorted(counts.items()))
            self.last_message = (
                f"Tagged the document: {summary}. Review the tags and give figures alternate text."
            )
            self.w.notify(self.last_message, "success", undo_action(session))
            return dict(counts)

        def failed(message: str) -> None:
            self.last_message = f"Auto-tagging failed: {message}"
            self.w.notify(self.last_message, "error")

        return self.w.jobs.start(
            "Tagging the document…", work, session=session, on_done=done, on_failed=failed
        )
