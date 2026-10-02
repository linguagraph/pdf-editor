"""Edit ribbon: content-editing tools and object commands for the main window."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import QFileDialog, QMessageBox

from pdfeditor.model.objects import ObjectType, ShapeKind
from pdfeditor.ui.icons import icon
from pdfeditor.ui.tools import edit
from pdfeditor.ui.tools.base import Tool

if TYPE_CHECKING:
    from pdfeditor.ui.main_window import MainWindow
    from pdfeditor.ui.view.document_view import DocumentView

EDIT_TOOLS = (
    ("edit", "&Edit Text && Images", "Ctrl+Shift+E"),
    ("add_text", "Add &Text", None),
    ("add_image", "Add &Image", None),
    ("add_rectangle", "Add &Rectangle", None),
    ("add_ellipse", "Add &Ellipse", None),
    ("add_line", "Add &Line", None),
)
EDIT_TOOL_ICONS = {
    "edit": "file-pen-line",
    "add_text": "type",
    "add_image": "image-plus",
    "add_rectangle": "square",
    "add_ellipse": "circle",
    "add_line": "slash",
}


def make_edit_tool(name: str) -> Tool | None:
    if name == "edit":
        return edit.EditObjectsTool()
    if name == "add_text":
        return edit.AddTextTool()
    if name == "add_image":
        return edit.AddImageTool()
    kinds = {
        "add_rectangle": ShapeKind.RECTANGLE,
        "add_ellipse": ShapeKind.ELLIPSE,
        "add_line": ShapeKind.LINE,
    }
    if name in kinds:
        return edit.AddShapeTool(kinds[name])
    return None


class EditController:
    def __init__(
        self, window: MainWindow, tool_group: QActionGroup, tool_actions: dict[str, QAction]
    ) -> None:
        self.w = window
        self._warned_signed: set[int] = set()
        for name, text, key in EDIT_TOOLS:
            tool_action = QAction(icon(EDIT_TOOL_ICONS[name]), text, window, checkable=True)
            if key:
                tool_action.setShortcut(QKeySequence(key))
            tool_action.setProperty("needs_doc", True)
            tool_action.triggered.connect(lambda _=False, n=name: window.tool_clicked(n))
            window.addAction(tool_action)
            tool_group.addAction(tool_action)
            tool_actions[name] = tool_action
        self.tool_actions = tool_actions

        def act(
            text: str, icon_name: str, slot: Callable[[], object], needs_doc: bool = True
        ) -> QAction:
            a = QAction(icon(icon_name), text, window)
            a.triggered.connect(lambda _=False: slot())
            a.setProperty("needs_doc", needs_doc)
            window.addAction(a)
            return a

        self.act_replace_image = act("Re&place Image…", "image-up", self.replace_image)
        self.act_extract_image = act("E&xport Image…", "image-down", self.export_image)
        self.act_delete_objects = act("&Delete Selected Objects", "trash-2", self.delete_objects)

        def notify(message: str) -> None:
            window.notify(message)

        edit.notify = notify

        def show_error(message: str) -> None:
            QMessageBox.warning(window, "Edit", message)

        edit.error = show_error

    def ribbon(self) -> None:
        r = self.w.ribbon.add_tab("Edit")
        r.add_group(self.tool_actions["edit"], title="Edit")
        r.add_group(self.tool_actions["add_text"], self.tool_actions["add_image"], title="Add")
        r.add_group(
            self.tool_actions["add_rectangle"],
            self.tool_actions["add_ellipse"],
            self.tool_actions["add_line"],
            title="Shapes",
        )
        r.add_group(
            self.act_replace_image, self.act_extract_image, self.act_delete_objects, title="Objects"
        )

    def update_state(self, view: DocumentView | None) -> None:
        images = [
            o for o in (view.selected_page_objects() if view else []) if o.type is ObjectType.IMAGE
        ]
        single_image = len(images) == 1 and view is not None and len(view.selected_objects) == 1
        self.act_replace_image.setEnabled(single_image)
        self.act_extract_image.setEnabled(single_image)
        self.act_delete_objects.setEnabled(bool(view and view.selected_objects))
        capable = view is not None and view.session.engine.capabilities.content_edit
        for name, _text, _key in EDIT_TOOLS:
            self.tool_actions[name].setEnabled(capable)

    def confirm_signed(self, view: DocumentView) -> bool:
        """Editing content invalidates digital signatures: warn once per document."""
        session = view.session
        if session.id in self._warned_signed:
            return True
        with session.lock:
            signed = session.document.info().has_signatures
        if signed and not self.w.confirm(
            "Edit a Signed Document?",
            f"“{session.display_name}” is digitally signed. Editing its content invalidates "
            "every signature in it: readers will be told the document changed after signing.",
            "Edit Anyway",
        ):
            return False
        self._warned_signed.add(session.id)
        return True

    def _selected_image(self) -> tuple[DocumentView, int, str] | None:
        view = self.w.current_view()
        if view is None or len(view.selected_objects) != 1:
            return None
        page, key = view.selected_objects[0]
        return view, page, key

    def replace_image(self, path: Path | None = None) -> bool:
        target = self._selected_image()
        if target is None:
            return False
        view, page, key = target
        if path is None:
            chosen, _ = QFileDialog.getOpenFileName(
                self.w,
                "Replace Image",
                "",
                "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp)",
            )
            if not chosen:
                return False
            path = Path(chosen)
        data = path.read_bytes()
        return edit.run_edit(
            view, "Replace Image", lambda doc: doc.page(page).replace_image(key, data)
        )

    def export_image(self, path: Path | None = None) -> Path | None:
        target = self._selected_image()
        if target is None:
            return None
        view, page, key = target
        with view.session.lock:
            data, ext = view.session.document.page(page).image_data(key)
        if path is None:
            chosen, _ = QFileDialog.getSaveFileName(self.w, "Export Image", f"image.{ext}")
            if not chosen:
                return None
            path = Path(chosen)
        path.write_bytes(data)
        return path

    def delete_objects(self) -> bool:
        view = self.w.current_view()
        return view is not None and edit.delete_selected(view)
