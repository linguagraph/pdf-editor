"""Preferences dialog."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.ui.settings import AppSettings


class PreferencesDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.settings = settings

        general = QGroupBox("General", self)
        form = QFormLayout(general)
        self.author = QLineEdit(settings.author)
        form.addRow("Author name (for comments):", self.author)
        self.zoom = QComboBox()
        for key, text in (("fit_width", "Fit Width"), ("fit_page", "Fit Page"), ("100", "100%")):
            self.zoom.addItem(text, key)
        self.zoom.setCurrentIndex(max(0, self.zoom.findData(settings.default_zoom)))
        form.addRow("Default zoom:", self.zoom)
        self.tool = QComboBox()
        self.tool.addItem("Select", "select")
        self.tool.addItem("Hand", "hand")
        self.tool.setCurrentIndex(max(0, self.tool.findData(settings.default_tool)))
        form.addRow("Default tool:", self.tool)

        documents = QGroupBox("Documents", self)
        form = QFormLayout(documents)
        self.autosave = QSpinBox()
        self.autosave.setRange(0, 60)
        self.autosave.setSuffix(" min")
        self.autosave.setSpecialValueText("Off")
        self.autosave.setValue(settings.autosave_minutes)
        form.addRow("Save recovery copies every:", self.autosave)
        self.undo_disk = QSpinBox()
        self.undo_disk.setRange(64, 65536)
        self.undo_disk.setSingleStep(256)
        self.undo_disk.setSuffix(" MB")
        self.undo_disk.setValue(settings.undo_disk_mb)
        form.addRow("Undo history disk space:", self.undo_disk)

        performance = QGroupBox("Performance", self)
        form = QFormLayout(performance)
        self.cache = QSpinBox()
        self.cache.setRange(64, 4096)
        self.cache.setSingleStep(64)
        self.cache.setSuffix(" MB")
        self.cache.setValue(settings.cache_mb)
        form.addRow("Page image cache (after restart):", self.cache)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        for w in (general, documents, performance, buttons):
            layout.addWidget(w)

    def accept(self) -> None:
        s = self.settings
        s.author = self.author.text().strip()
        s.default_zoom = str(self.zoom.currentData())
        s.default_tool = str(self.tool.currentData())
        s.autosave_minutes = self.autosave.value()
        s.undo_disk_mb = self.undo_disk.value()
        s.cache_mb = self.cache.value()
        super().accept()
