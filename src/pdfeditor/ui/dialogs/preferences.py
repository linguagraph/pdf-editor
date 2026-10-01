"""Preferences dialog."""

from __future__ import annotations

from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
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

from pdfeditor.ui.i18n import available_languages
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.style.tokens import ACCENT_PRESETS
from pdfeditor.ui.theme import theme_manager

CUSTOM = "custom"


def _swatch(color: str) -> QIcon:
    pix = QPixmap(14, 14)
    pix.fill(QColor(color))
    return QIcon(pix)


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
        self.keep_tools = QCheckBox("Keep tools selected after use")
        self.keep_tools.setToolTip("Off: comment and drawing tools go back to Select after one use")
        self.keep_tools.setChecked(settings.keep_tools)
        form.addRow("", self.keep_tools)
        self.language = QComboBox()
        self.language.addItem("System default", "")
        for code, name in available_languages().items():
            self.language.addItem(name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(settings.language)))
        form.addRow("Language (after restart):", self.language)

        appearance = QGroupBox("Appearance", self)
        form = QFormLayout(appearance)
        self.theme = QComboBox()
        for key, text in (("system", "Same as Windows"), ("light", "Light"), ("dark", "Dark")):
            self.theme.addItem(text, key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(settings.theme)))
        form.addRow("Theme:", self.theme)
        self.accent = QComboBox()
        self.accent.addItem(_swatch(theme_manager().system_accent), "Windows accent color", "")
        for name, color in ACCENT_PRESETS:
            self.accent.addItem(_swatch(color), name, color)
        self.accent.addItem("Custom…", CUSTOM)
        self._select_accent(settings.accent)
        self._previous_accent = self.accent.currentIndex()
        self.accent.activated.connect(self._accent_activated)
        form.addRow("Accent color:", self.accent)
        self.menu_bar = QCheckBox("Always show the menu bar")
        self.menu_bar.setToolTip("Off: the menus are behind the ☰ button; tap Alt to open them")
        self.menu_bar.setChecked(settings.show_menu_bar)
        form.addRow("", self.menu_bar)

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
        for w in (general, appearance, documents, performance, buttons):
            layout.addWidget(w)

    def _select_accent(self, color: str) -> None:
        index = self.accent.findData(color)
        if index < 0:  # a custom color: show it just above "Custom…"
            index = self.accent.count() - 1
            self.accent.insertItem(index, _swatch(color), color, color)
        self.accent.setCurrentIndex(index)

    def _accent_activated(self, index: int) -> None:
        if self.accent.itemData(index) == CUSTOM:
            start = QColor(theme_manager().colors.accent)
            color = QColorDialog.getColor(start, self, "Accent Color")
            if not color.isValid():
                self.accent.setCurrentIndex(self._previous_accent)
                return
            self._select_accent(color.name())
        self._previous_accent = self.accent.currentIndex()

    def accept(self) -> None:
        s = self.settings
        s.author = self.author.text().strip()
        s.default_zoom = str(self.zoom.currentData())
        s.default_tool = str(self.tool.currentData())
        s.keep_tools = self.keep_tools.isChecked()
        s.language = str(self.language.currentData())
        s.theme = str(self.theme.currentData())
        s.show_menu_bar = self.menu_bar.isChecked()
        accent = self.accent.currentData()
        s.accent = accent if isinstance(accent, str) and accent != CUSTOM else ""
        s.autosave_minutes = self.autosave.value()
        s.undo_disk_mb = self.undo_disk.value()
        s.cache_mb = self.cache.value()
        super().accept()
