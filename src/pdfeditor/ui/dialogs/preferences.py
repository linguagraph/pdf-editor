"""Preferences dialog: a sidebar of categories, each a form."""

from __future__ import annotations

from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.ui.dialogs.base import FormDialog, add_row, form_layout
from pdfeditor.ui.i18n import available_languages
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.style.tokens import ACCENT_PRESETS, METRICS
from pdfeditor.ui.theme import theme_manager

CUSTOM = "custom"
CATEGORIES = ("General", "Appearance", "Tools", "Performance", "Shortcuts")


def _swatch(color: str) -> QIcon:
    pix = QPixmap(14, 14)
    pix.fill(QColor(color))
    return QIcon(pix)


class PreferencesDialog(FormDialog):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__("Preferences", parent=parent)
        self.header_title.hide()  # the sidebar and page headings say where you are
        self.settings = settings
        self.pages = QStackedWidget(self)
        self.sidebar = QListWidget(self)
        self.sidebar.setProperty("role", "sidebar")
        self.sidebar.setAccessibleName("Preference categories")
        self.sidebar.setFixedWidth(150)
        self.page_forms: dict[str, QFormLayout] = {}
        for name in CATEGORIES:
            self.sidebar.addItem(name)
            self._add_page(name)

        # -- General
        form = self.page_forms["General"]
        self.author = QLineEdit(settings.author)
        add_row(form, "Author name:", self.author, "Shown on the comments you add.")
        self.language = QComboBox()
        self.language.addItem("System default", "")
        for code, name in available_languages().items():
            self.language.addItem(name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(settings.language)))
        add_row(form, "Language:", self.language, "Takes effect after a restart.")
        self.autosave = QSpinBox()
        self.autosave.setRange(0, 60)
        self.autosave.setSuffix(" min")
        self.autosave.setSpecialValueText("Off")
        self.autosave.setValue(settings.autosave_minutes)
        add_row(
            form,
            "Save recovery copies every:",
            self.autosave,
            "Unsaved changes can be recovered if the app closes unexpectedly.",
        )

        # -- Appearance
        form = self.page_forms["Appearance"]
        self.theme = QComboBox()
        for key, text in (("system", "Same as Windows"), ("light", "Light"), ("dark", "Dark")):
            self.theme.addItem(text, key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(settings.theme)))
        add_row(form, "Theme:", self.theme)
        self.accent = QComboBox()
        self.accent.addItem(_swatch(theme_manager().system_accent), "Windows accent color", "")
        for name, color in ACCENT_PRESETS:
            self.accent.addItem(_swatch(color), name, color)
        self.accent.addItem("Custom…", CUSTOM)
        self._select_accent(settings.accent)
        self._previous_accent = self.accent.currentIndex()
        self.accent.activated.connect(self._accent_activated)
        add_row(form, "Accent color:", self.accent)
        self.menu_bar = QCheckBox("Always show the menu bar")
        self.menu_bar.setToolTip("Off: the menus are behind the ☰ button; tap Alt to open them")
        self.menu_bar.setChecked(settings.show_menu_bar)
        add_row(form, "", self.menu_bar)
        self.zoom = QComboBox()
        for key, text in (("fit_width", "Fit Width"), ("fit_page", "Fit Page"), ("100", "100%")):
            self.zoom.addItem(text, key)
        self.zoom.setCurrentIndex(max(0, self.zoom.findData(settings.default_zoom)))
        add_row(form, "Default zoom:", self.zoom, "Used when a document is opened.")

        # -- Tools
        form = self.page_forms["Tools"]
        self.tool = QComboBox()
        self.tool.addItem("Select", "select")
        self.tool.addItem("Hand", "hand")
        self.tool.setCurrentIndex(max(0, self.tool.findData(settings.default_tool)))
        add_row(form, "Default tool:", self.tool)
        self.keep_tools = QCheckBox("Keep tools selected after use")
        self.keep_tools.setToolTip("Off: comment and drawing tools go back to Select after one use")
        self.keep_tools.setChecked(settings.keep_tools)
        add_row(
            form,
            "",
            self.keep_tools,
            "Off: comment and drawing tools go back to Select after one use.",
        )

        # -- Performance
        form = self.page_forms["Performance"]
        self.cache = QSpinBox()
        self.cache.setRange(64, 4096)
        self.cache.setSingleStep(64)
        self.cache.setSuffix(" MB")
        self.cache.setValue(settings.cache_mb)
        add_row(
            form,
            "Page image cache:",
            self.cache,
            "Memory for rendered pages. Takes effect after a restart.",
        )
        self.undo_disk = QSpinBox()
        self.undo_disk.setRange(64, 65536)
        self.undo_disk.setSingleStep(256)
        self.undo_disk.setSuffix(" MB")
        self.undo_disk.setValue(settings.undo_disk_mb)
        add_row(
            form,
            "Undo history disk space:",
            self.undo_disk,
            "Snapshots that let large edits (redaction, OCR, text editing) be undone.",
        )

        # -- Shortcuts: the full editor is its own dialog (it applies changes immediately)
        form = self.page_forms["Shortcuts"]
        self.shortcuts_button = QPushButton("Customize Keyboard Shortcuts…")
        self.shortcuts_button.clicked.connect(self.open_shortcuts)
        self.shortcuts_button.setEnabled(self._shortcut_manager() is not None)
        row = QHBoxLayout()
        row.addWidget(self.shortcuts_button)
        row.addStretch()
        add_row(
            form,
            "",
            row,
            "Assign, remove or reset the keys for any command. Changes apply at once.",
        )

        body = QHBoxLayout()
        body.setSpacing(METRICS.space(5))
        body.addWidget(self.sidebar)
        body.addWidget(self.pages, 1)
        self.content.addLayout(body)
        self.sidebar.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.sidebar.setCurrentRow(0)
        self.resize(640, 400)

    def _add_page(self, name: str) -> None:
        page = QWidget(self.pages)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(METRICS.space(3))
        heading = QLabel(name, page)
        font = heading.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        form = form_layout()
        layout.addLayout(form)
        layout.addStretch(1)
        self.pages.addWidget(page)
        self.page_forms[name] = form

    def show_category(self, name: str) -> None:
        self.sidebar.setCurrentRow(CATEGORIES.index(name))

    def current_category(self) -> str:
        return CATEGORIES[self.pages.currentIndex()]

    def _shortcut_manager(self) -> object | None:
        return getattr(self.parent(), "shortcuts", None)

    def open_shortcuts(self) -> None:
        from pdfeditor.ui.shortcuts import ShortcutManager, ShortcutsDialog

        manager = self._shortcut_manager()
        if isinstance(manager, ShortcutManager):
            ShortcutsDialog(manager, self).exec()

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
