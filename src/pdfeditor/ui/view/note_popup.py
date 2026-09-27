"""A sticky-note popup: read a note, edit its text, see and add replies."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.commands import UpdateAnnotationCommand
from pdfeditor.model.annotations import AnnotationModel

if TYPE_CHECKING:
    from pdfeditor.ui.view.document_view import DocumentView


class NotePopup(QFrame):
    def __init__(
        self, view: DocumentView, note: AnnotationModel, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent or view, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.view = view
        self.note = note
        self.setFrameShape(QFrame.Shape.Box)
        self.setStyleSheet("NotePopup { background: #fff9c4; }")
        when = note.modified or note.created
        header = QLabel(
            f"<b>{note.author or 'Unknown'}</b>" + (f"  {when:%Y-%m-%d %H:%M}" if when else "")
        )
        self.text = QPlainTextEdit(note.contents)
        self.text.setReadOnly(note.locked)
        self.replies = QLabel()
        self.replies.setWordWrap(True)
        self.reply_edit = QLineEdit()
        self.reply_edit.setPlaceholderText("Reply…")
        self.reply_edit.returnPressed.connect(self.add_reply)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self.text)
        layout.addWidget(self.replies)
        layout.addWidget(self.reply_edit)
        layout.addLayout(buttons)
        self.resize(280, 220)
        self._show_replies()

    def _show_replies(self) -> None:
        lines = [
            f"↳ <b>{r.author or 'Reply'}</b>: {r.contents}"
            for r in self.view.page_annotations(self.note.page_index)
            if r.in_reply_to == self.note.id and r.contents
        ]
        self.replies.setText("<br>".join(lines))
        self.replies.setVisible(bool(lines))

    def add_reply(self) -> None:
        from pdfeditor.ui.panels.comments import add_reply

        text = self.reply_edit.text().strip()
        if text:
            add_reply(self.view, self.note, text)
            self.reply_edit.clear()
            self._show_replies()

    def save_text(self) -> None:
        text = self.text.toPlainText()
        current = self.view.annotation_by_name(self.note.page_index, self.note.name)
        if current is None or text == current.contents or current.locked:
            return
        after = copy.deepcopy(current)
        after.contents = text
        self.view.session.execute(UpdateAnnotationCommand(current, after, "Edit Note"))

    def show_at(self, pos: QPoint) -> None:
        self.move(pos)
        self.show()
        self.text.setFocus()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.save_text()
        super().closeEvent(event)
