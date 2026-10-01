"""Optional-content layers with visibility checkboxes."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from pdfeditor.ui.panels.base import EmptyState, ViewPanel


class LayersPanel(ViewPanel):
    title = "Layers"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.list = QListWidget(self)
        self.list.itemChanged.connect(self._on_changed)
        self.empty = EmptyState(
            "layers",
            "No layers",
            "Some documents, such as maps and technical drawings, have layers. "
            "You can show or hide each one here.",
            self,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list)
        layout.addWidget(self.empty)
        self._updating = False

    def rebuild(self) -> None:
        self._updating = True
        try:
            self.list.clear()
            layers = []
            if self.view is not None and self.view.session.engine.capabilities.layers:
                with self.view.session.lock:
                    layers = self.view.session.document.layers()
            for layer in layers:
                item = QListWidgetItem("    " * layer.depth + layer.name)
                item.setData(Qt.ItemDataRole.UserRole, layer.id)
                flags = item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                if layer.locked:
                    flags &= ~Qt.ItemFlag.ItemIsEnabled
                item.setFlags(flags)
                item.setCheckState(
                    Qt.CheckState.Checked if layer.visible else Qt.CheckState.Unchecked
                )
                self.list.addItem(item)
            self.list.setVisible(bool(layers))
            self.empty.setVisible(not layers)
        finally:
            self._updating = False

    def _on_changed(self, item: QListWidgetItem) -> None:
        if self._updating or self.view is None:
            return
        visible = item.checkState() == Qt.CheckState.Checked
        with self.view.session.lock:
            self.view.session.document.set_layer_visible(
                item.data(Qt.ItemDataRole.UserRole), visible
            )
        self.view.refresh()
