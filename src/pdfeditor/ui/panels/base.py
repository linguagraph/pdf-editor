"""Common base for navigation-pane panels that follow the active document view."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence, QShowEvent
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from pdfeditor.core.commands import Change, ChangeKind
from pdfeditor.ui.icons import icon
from pdfeditor.ui.view.document_view import DocumentView


class ViewPanel(QWidget):
    title = "Panel"
    # Rebuild when the document changes in one of these ways (STRUCTURE = reloaded).
    rebuild_on: frozenset[ChangeKind] = frozenset({ChangeKind.STRUCTURE})
    # The number on the panel's rail icon changed (see ``badge_count``).
    badge_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view: DocumentView | None = None

    def set_view(self, view: DocumentView | None) -> None:
        """Bind to a view (or ``None`` when no document is open)."""
        if self.view is not None:
            self.view.document_changed.disconnect(self._on_document_changed)
            self.unbind(self.view)
        self.view = view
        if view is not None:
            view.document_changed.connect(self._on_document_changed)
            self.bind(view)
        self.rebuild()
        self.badge_changed.emit()

    def _on_document_changed(self, changes: tuple[Change, ...]) -> None:
        if any(c.kind in self.rebuild_on for c in changes):
            self.rebuild()
            self.badge_changed.emit()

    def bind(self, view: DocumentView) -> None:
        """Connect to the view's signals."""

    def unbind(self, view: DocumentView) -> None:
        """Disconnect from the view's signals."""

    def rebuild(self) -> None:
        """Reload content from ``self.view`` (which may be ``None``)."""

    def badge_count(self) -> int:
        """Count shown on the panel's rail icon, from data the panel already holds (0: none).

        Panels emit ``badge_changed`` when it may have changed; the base class does so after
        every rebuild."""
        return 0


class EmptyState(QWidget):
    """What an empty panel is for, and a button for its main action.

    The button can trigger a window action, whose current shortcut is shown in its label (so
    a shortcut the user changed shows up), or call a panel's own slot.
    """

    ICON_SIZE = 32

    def __init__(self, icon_name: str, title: str, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._icon_name = icon_name
        self.icon_label = QLabel(self)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = QLabel(title, self)
        self.title.setProperty("role", "empty-title")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        self.text = QLabel(text, self)
        self.text.setProperty("role", "muted")
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text.setWordWrap(True)
        self.button = QPushButton(self)
        self.button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.button.clicked.connect(self._clicked)
        self.button.hide()
        self._action: QAction | None = None
        self._slot: Callable[[], object] | None = None
        self._label = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(8)
        layout.addStretch(1)
        layout.addWidget(self.icon_label)
        layout.addWidget(self.title)
        layout.addWidget(self.text)
        layout.addSpacing(4)
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(2)
        self._update_icon()

    def set_text(self, title: str, text: str) -> None:
        self.title.setText(title)
        self.text.setText(text)

    def set_action(
        self,
        label: str,
        action: QAction | None = None,
        slot: Callable[[], object] | None = None,
    ) -> None:
        """Offer ``label`` as the main action: trigger ``action`` or call ``slot``."""
        if self._action is not None:
            self._action.changed.disconnect(self.refresh)
        self._label, self._action, self._slot = label, action, slot
        if action is not None:
            action.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        action = self._action
        if action is None and self._slot is None:
            self.button.hide()
            return
        label = self._label
        if action is not None:
            keys = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            if keys:
                label = f"{label} ({keys})"
            self.button.setEnabled(action.isEnabled())
            self.button.setVisible(action.isVisible())
        else:
            self.button.show()
        self.button.setText(label)

    def _clicked(self) -> None:
        if self._action is not None:
            self._action.trigger()
        elif self._slot is not None:
            self._slot()

    def _update_icon(self) -> None:
        size = QSize(self.ICON_SIZE, self.ICON_SIZE)
        # Disabled mode: the muted palette color, so the glyph stays in the background.
        pixmap = icon(self._icon_name).pixmap(size, self.devicePixelRatio(), QIcon.Mode.Disabled)
        self.icon_label.setPixmap(pixmap)

    def showEvent(self, event: QShowEvent) -> None:
        self.refresh()  # pick up shortcut changes made while hidden
        super().showEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self._update_icon()
        super().changeEvent(event)
