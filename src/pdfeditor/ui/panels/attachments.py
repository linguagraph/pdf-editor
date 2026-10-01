"""Embedded files list with save-to-disk."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.ui.panels.base import EmptyState, ViewPanel


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


class AttachmentsPanel(ViewPanel):
    title = "Attachments"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Name", "Size", "Description"])
        self.tree.setRootIsDecorated(False)
        self.tree.itemDoubleClicked.connect(lambda *_: self.save_selected())
        self.save_button = QPushButton("Save…", self)
        self.save_button.clicked.connect(self.save_selected)
        self.empty = EmptyState(
            "paperclip",
            "No attachments",
            "Files embedded in this PDF, such as spreadsheets or source documents, are listed "
            "here so you can save them.",
            self,
        )
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree)
        layout.addWidget(self.empty)
        layout.addLayout(buttons)

    def rebuild(self) -> None:
        self.tree.clear()
        if self.view is not None:
            with self.view.session.lock:
                files = self.view.session.document.embedded_files()
            for f in files:
                node = QTreeWidgetItem([f.filename, human_size(f.size), f.description])
                node.setData(0, Qt.ItemDataRole.UserRole, f.name)
                self.tree.addTopLevelItem(node)
        has_files = self.tree.topLevelItemCount() > 0
        self.save_button.setEnabled(has_files)
        self.save_button.setVisible(has_files)
        self.tree.setVisible(has_files)
        self.empty.setVisible(not has_files)

    def save_selected(self, target: Path | None = None) -> Path | None:
        node = self.tree.currentItem() or self.tree.topLevelItem(0)
        if node is None or self.view is None:
            return None
        name = node.data(0, Qt.ItemDataRole.UserRole)
        if target is None:
            chosen, _ = QFileDialog.getSaveFileName(self, "Save attachment", node.text(0))
            if not chosen:
                return None
            target = Path(chosen)
        try:
            with self.view.session.lock:
                data = self.view.session.document.extract_embedded_file(name)
            target.write_bytes(data)
        except OSError as exc:
            QMessageBox.warning(self, "Save attachment", f"Couldn't save the attachment:\n{exc}")
            return None
        return target
