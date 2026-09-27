"""Protect ribbon: redaction (mark, review, apply, verify) and sanitizing."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QActionGroup, QMouseEvent
from PySide6.QtWidgets import QGraphicsView, QInputDialog, QLineEdit, QMessageBox

from pdfeditor.core.commands import (
    AddAnnotationCommand,
    Command,
    DeleteAnnotationsCommand,
    MacroCommand,
    SetSecurityCommand,
    SnapshotCommand,
)
from pdfeditor.engine.base import Document
from pdfeditor.model.annotations import AnnotationModel
from pdfeditor.model.color import Color
from pdfeditor.model.geometry import Point, Quad, Rect
from pdfeditor.model.metadata import EncryptionMethod, SecuritySettings
from pdfeditor.services.redaction import (
    MarkStyle,
    VerificationReport,
    find_sensitive,
    mark_areas,
    mark_for_area,
    mark_for_hit,
    verify,
)
from pdfeditor.ui.dialogs.redaction import (
    ApplyRedactionsDialog,
    MarkTextDialog,
    RedactionPropertiesDialog,
    SanitizeDialog,
)
from pdfeditor.ui.dialogs.security import SecurityDialog
from pdfeditor.ui.panels.redactions import RedactionsPanel
from pdfeditor.ui.tools.base import Tool

if TYPE_CHECKING:
    from pdfeditor.core.session import DocumentSession
    from pdfeditor.ui.main_window import MainWindow
    from pdfeditor.ui.view.document_view import DocumentView


class RedactTool(Tool):
    """Drag a box over anything (text, images, graphics) to mark it for redaction."""

    name = "redact"
    respects_existing = True  # clicking an existing mark selects it

    def __init__(self, controller: ProtectController) -> None:
        self.controller = controller
        self._start: tuple[int, Point] | None = None

    def activate(self, view: DocumentView) -> None:
        view.setDragMode(QGraphicsView.DragMode.NoDrag)

    def hover(self, view: DocumentView, event: QMouseEvent) -> None:
        view.viewport().setCursor(Qt.CursorShape.CrossCursor)

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        self._start = view.page_point_at(view.mapToScene(event.position().toPoint()))
        return self._start is not None

    def move(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._start is None:
            return False
        page, a = self._start
        b = view.scene_to_page(page, view.mapToScene(event.position().toPoint()))
        view.set_annotation_preview({page: [Rect.from_points([a, b])]})
        return True

    def release(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._start is None:
            return False
        page, a = self._start
        self._start = None
        view.set_annotation_preview({})
        b = view.scene_to_page(page, view.mapToScene(event.position().toPoint()))
        if abs(b.x - a.x) < 3 or abs(b.y - a.y) < 3:
            return True
        rect = Rect.from_points([a, b]).intersection(view.page_rect(page))
        self.controller.add_marks(
            view,
            [mark_for_area(page, rect, self.controller.style, view.author)],
            "Mark for Redaction",
        )
        view.tool_used.emit()
        return True


class ProtectController:
    def __init__(
        self, window: MainWindow, tool_group: QActionGroup, tool_actions: dict[str, QAction]
    ) -> None:
        self.w = window
        self.style = self._load_style()
        tool = QAction("&Redact", window, checkable=True)
        tool.setProperty("needs_doc", True)
        tool.setToolTip("Drag over content to mark it for redaction (or mark the selected text)")
        tool.triggered.connect(lambda _=False: self.start_redact_tool())
        tool_group.addAction(tool)
        tool_actions["redact"] = tool
        self.act_redact = tool

        def act(text: str, slot: Callable[[], object], needs_doc: bool = True) -> QAction:
            a = QAction(text, window)
            a.triggered.connect(lambda _=False: slot())
            a.setProperty("needs_doc", needs_doc)
            window.addAction(a)
            return a

        self.act_mark_text = act("&Find Text to Redact…", self.find_and_mark)
        self.act_mark_pages = act("Mark Whole &Pages", self.mark_pages)
        self.act_properties = act("Redaction &Properties…", self.edit_properties, needs_doc=False)
        self.act_apply = act("&Apply Redactions…", lambda: self.apply(None))
        self.act_sanitize = act("&Sanitize Document…", self.sanitize)
        self.act_encrypt = act("&Encrypt with Password…", self.encrypt)
        self.act_remove_security = act("Remove &Security", self.remove_security)
        self.last_message = ""
        self.panel = RedactionsPanel()
        self.panel.on_apply = self.apply
        self.panel.on_remove = self.remove_marks

    # -- settings -------------------------------------------------------------------------
    @staticmethod
    def _load_style() -> MarkStyle:
        s = QSettings()
        try:
            fill = Color.from_hex(str(s.value("redaction/fill", "#000000")))
            text_color = Color.from_hex(str(s.value("redaction/text_color", "#ffffff")))
        except ValueError:
            return MarkStyle()
        return MarkStyle(fill, str(s.value("redaction/overlay", "")), text_color)

    def edit_properties(self, dialog: RedactionPropertiesDialog | None = None) -> None:
        dialog = dialog or RedactionPropertiesDialog(self.style, self.w)
        if not dialog.result() and not dialog.exec():
            return
        self.style = dialog.mark_style()
        s = QSettings()
        s.setValue("redaction/fill", self.style.fill.to_hex())
        s.setValue("redaction/overlay", self.style.overlay_text)
        s.setValue("redaction/text_color", self.style.text_color.to_hex())

    def ribbon(self) -> None:
        r = self.w.ribbon.add_tab("Protect")
        r.add_group(self.act_redact, self.act_mark_text, self.act_mark_pages, self.act_properties)
        r.add_group(self.act_apply)
        r.add_group(self.act_sanitize)
        if self.w.engine().capabilities.encrypt:
            r.add_group(self.act_encrypt, self.act_remove_security)

    # -- marking --------------------------------------------------------------------------
    def add_marks(self, view: DocumentView, marks: list[AnnotationModel], label: str) -> None:
        if not marks:
            return
        commands: list[Command] = [AddAnnotationCommand(m, label) for m in marks]
        view.session.execute(commands[0] if len(commands) == 1 else MacroCommand(label, commands))

    def start_redact_tool(self) -> None:
        """With text selected, mark the selection at once; otherwise switch to the box tool
        (or, if it's already active, back to Select)."""
        if self.w.current_tool == "redact":
            self.w.set_tool("select")
            return
        view = self.w.current_view()
        if view is not None and view.has_selection() and view.selection is not None:
            marks = []
            for page, rects in view.selection.rects(view.text_cache).items():
                if not rects:
                    continue
                box = rects[0]
                for r in rects[1:]:
                    box = box.union(r)
                mark = mark_for_area(page, box, self.style, view.author)
                mark.quads = tuple(Quad.from_rect(r) for r in rects)
                marks.append(mark)
            view.clear_selection()
            self.add_marks(view, marks, "Mark Selection for Redaction")
            self.w.tool_actions[self.w.current_tool].setChecked(True)
            return
        self.w.set_tool("redact")

    def mark_dialog(self, view: DocumentView) -> MarkTextDialog:
        """The Find Text to Redact dialog, with its Find button searching ``view``."""
        dialog = MarkTextDialog(self.w)

        def search() -> None:
            dialog.show_hits(find_sensitive(view.text_cache, dialog.patterns()), view.page_label)

        dialog.search_button.clicked.connect(search)
        return dialog

    def find_and_mark(self, dialog: MarkTextDialog | None = None) -> int:
        view = self.w.current_view()
        if view is None:
            return 0
        dialog = dialog or self.mark_dialog(view)
        if not dialog.result() and not dialog.exec():
            return 0
        hits = dialog.selected_hits()
        marks = [mark_for_hit(h, self.style, view.author) for h in hits]
        self.add_marks(view, marks, "Mark Text for Redaction")
        self.show_panel()
        return len(hits)

    def mark_pages(self) -> None:
        tab = self.w.current_tab()
        if tab is None:
            return
        view = tab.view
        marks = [
            mark_for_area(p, view.page_rect(p), self.style, view.author) for p in tab.target_pages()
        ]
        self.add_marks(view, marks, "Mark Pages for Redaction")

    def remove_marks(self, marks: list[AnnotationModel]) -> None:
        view = self.w.current_view()
        if view is None or not marks:
            return
        by_page: dict[int, list[str]] = {}
        for m in marks:
            by_page.setdefault(m.page_index, []).append(m.name)
        commands: list[Command] = [
            DeleteAnnotationsCommand(p, n, "Remove Redaction Mark") for p, n in by_page.items()
        ]
        view.session.execute(
            commands[0] if len(commands) == 1 else MacroCommand("Remove Redaction Marks", commands)
        )

    def show_panel(self) -> None:
        self.w.nav_dock.show()
        self.w.nav_tabs.setCurrentWidget(self.panel)

    # -- applying -------------------------------------------------------------------------
    def apply(
        self, marks: list[AnnotationModel] | None, dialog: ApplyRedactionsDialog | None = None
    ) -> VerificationReport | None:
        view = self.w.current_view()
        if view is None:
            return None
        all_marks = self.panel.marks() if self.panel.view is view else self._all_marks(view)
        if not all_marks:
            QMessageBox.information(
                self.w, "Apply Redactions", "There are no redaction marks to apply."
            )
            return None
        dialog = dialog or ApplyRedactionsDialog(len(all_marks), len(marks or []), self.w)
        if not dialog.result() and not dialog.exec():
            return None
        chosen = marks if (marks and dialog.selected.isChecked()) else all_marks
        options = dialog.options()
        areas: dict[int, list[Rect]] = {}
        by_page: dict[int, list[int]] = {}
        for m in chosen:
            areas.setdefault(m.page_index, []).extend(mark_areas(m))
            if m.id is not None:
                by_page.setdefault(m.page_index, []).append(m.id)
        selective = len(chosen) != len(all_marks)

        def operation(doc: Document) -> None:
            for page, ids in by_page.items():
                doc.page(page).apply_redactions(ids if selective else None, options)

        session = view.session
        label = "Apply Redaction" if len(chosen) == 1 else f"Apply {len(chosen)} Redactions"
        session.execute(SnapshotCommand(label, operation, session.snapshots))
        session.require_full_save = True
        with session.lock:
            report = verify(session.document, areas, [m.overlay_text for m in chosen])
        self._report(report)
        return report

    @staticmethod
    def _all_marks(view: DocumentView) -> list[AnnotationModel]:
        from pdfeditor.model.annotations import AnnotationType

        return [
            a
            for p in range(view.page_count)
            for a in view.page_annotations(p)
            if a.type is AnnotationType.REDACT
        ]

    def _report(self, report: VerificationReport) -> None:
        if report.ok:
            details = f"Verified {report.areas} area(s)"
            if report.images_checked:
                details += f" and {report.images_checked} image(s)"
            message = f"{details}: no text or image content is left under the redactions."
            if report.unverified:
                message += "\n\nNot verified:\n" + "\n".join(report.unverified)
            self.w.statusBar().showMessage(message.splitlines()[0], 8000)
            self.last_message = message
        else:
            self.last_message = "Some content may still be readable:\n\n" + "\n".join(report.leaks)
            QMessageBox.warning(self.w, "Redaction Check", self.last_message)

    def sanitize(self, dialog: SanitizeDialog | None = None) -> list[str]:
        view = self.w.current_view()
        if view is None:
            return []
        dialog = dialog or SanitizeDialog(self.w)
        if not dialog.result() and not dialog.exec():
            return []
        options = dialog.options()
        removed: list[str] = []

        def operation(doc: Document) -> None:
            removed.extend(doc.sanitize(options))

        session = view.session
        session.execute(SnapshotCommand("Sanitize Document", operation, session.snapshots))
        session.require_full_save = True
        summary = ("Removed: " + "; ".join(removed)) if removed else "Nothing needed removing."
        self.last_message = summary + "\n\nSave the document to write the cleaned file."
        self.w.statusBar().showMessage(summary, 8000)
        return removed

    last_message = ""

    # -- password security --------------------------------------------------------------------
    def _owner_access(self, session: DocumentSession, owner_password: str | None) -> bool:
        """Changing security needs the owner password when the document already has one."""
        with session.lock:
            if session.document.has_owner_access():
                return True
        if owner_password is None:
            owner_password, ok = QInputDialog.getText(
                self.w,
                "Permissions Password",
                "This document is protected. Enter its permissions (owner) password to change "
                "its security:",
                QLineEdit.EchoMode.Password,
            )
            if not ok:
                return False
        with session.lock:
            unlocked = session.document.unlock_owner(owner_password)
        if not unlocked:
            QMessageBox.warning(self.w, "Security", "That permissions password is not correct.")
        return unlocked

    def _notify(self, message: str) -> None:
        self.last_message = message
        self.w.statusBar().showMessage(message, 8000)

    def encrypt(
        self, dialog: SecurityDialog | None = None, owner_password: str | None = None
    ) -> bool:
        view = self.w.current_view()
        if view is None:
            return False
        session = view.session
        if not self._owner_access(session, owner_password):
            return False
        if dialog is None:
            with session.lock:
                current = session.document.info().permissions
            dialog = SecurityDialog(current, self.w)
        if not dialog.result() and not dialog.exec():
            return False
        session.execute(SetSecurityCommand(dialog.settings()))
        self._notify("Password security will be applied when you save the document.")
        return True

    def remove_security(self, owner_password: str | None = None) -> bool:
        view = self.w.current_view()
        if view is None:
            return False
        session = view.session
        with session.lock:
            doc = session.document
            encrypted = doc.info().encryption is not EncryptionMethod.NONE
            pending = doc.pending_security()
        if not encrypted and (pending is None or pending.method is EncryptionMethod.NONE):
            QMessageBox.information(self.w, "Remove Security", "This document has no security.")
            return False
        if encrypted and not self._owner_access(session, owner_password):
            return False
        if not encrypted:  # only a pending change: just drop it
            session.execute(SetSecurityCommand(SecuritySettings(EncryptionMethod.NONE)))
            self._notify("The pending password security was removed.")
            return True
        session.execute(SetSecurityCommand(SecuritySettings(EncryptionMethod.NONE)))
        self._notify("Security will be removed when you save the document.")
        return True
