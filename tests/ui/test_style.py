"""Design tokens and the theme built from them (Phase U1)."""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QColorDialog, QLabel

from pdfeditor.ui.dialogs.preferences import PreferencesDialog
from pdfeditor.ui.icons import page_icon
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.style.qss import stylesheet
from pdfeditor.ui.style.tokens import (
    AA_NON_TEXT,
    AA_TEXT,
    DEFAULT_ACCENT,
    TEXT_PAIRS,
    build_colors,
    contrast,
    ensure_contrast,
    mix,
)
from pdfeditor.ui.theme import Theme, ThemeManager, apply_theme, theme_manager

# Default, the user's Windows green, and accents that are hard to read on one scheme or both.
ACCENTS = [None, "#00b25a", "#ffd700", "#00ffff", "#0000ff", "#ffffff", "#000000", "#808080"]


# -- tokens (no Qt) ----------------------------------------------------------------------------
def test_contrast_math() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777777", "#777777") == pytest.approx(1.0)
    assert mix("#000000", "#ffffff", 0.5) == "#808080"
    assert contrast(ensure_contrast("#ffd700", ("#ffffff",), AA_TEXT, "#000000"), "#ffffff") >= 4.5


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("accent", ACCENTS)
def test_every_text_color_meets_wcag_aa(scheme, accent) -> None:
    c = build_colors(scheme, accent)
    failures = [
        (fg, bg, round(contrast(getattr(c, fg), getattr(c, bg)), 2))
        for fg, bg in TEXT_PAIRS
        if contrast(getattr(c, fg), getattr(c, bg)) < AA_TEXT
    ]
    assert failures == []
    # focus rings and the selected-tab underline must stand out from the chrome
    assert contrast(c.accent_text, c.window) >= AA_NON_TEXT
    assert contrast(c.accent, c.window) >= AA_NON_TEXT


def test_accent_keeps_its_hue_when_it_already_passes() -> None:
    assert build_colors("light", "#0067c0").accent == "#0067c0"
    green = build_colors("light", "#00b25a").accent
    assert green != "#00b25a" and green.startswith("#00")  # darkened, still green
    assert build_colors("dark", "#00b25a").accent == "#00b25a"  # black text already reads


def test_invalid_accent_falls_back_to_default() -> None:
    default = build_colors("light", DEFAULT_ACCENT)
    assert build_colors("light", "not a color").accent == default.accent


def test_stylesheet_uses_the_tokens() -> None:
    c = build_colors("dark", "#8764b8")
    qss = stylesheet(c)
    assert c.accent in qss and c.tooltip_bg in qss and c.note_bg in qss
    assert "{c." not in qss


# -- applied theme (Qt) ------------------------------------------------------------------------
@pytest.fixture
def app(qtbot) -> Iterator[QApplication]:
    instance = QApplication.instance()
    assert isinstance(instance, QApplication)
    yield instance
    apply_theme(instance, Theme.SYSTEM)


def _color(role: QPalette.ColorRole) -> str:
    return QGuiApplication.palette().color(role).name()


@pytest.mark.gui
@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_theme_sets_palette_and_style_sheet(app: QApplication, theme: Theme) -> None:
    apply_theme(app, theme, "#00b25a")
    c = theme_manager().colors
    assert c.scheme == theme.value
    assert _color(QPalette.ColorRole.Window) == c.window
    assert _color(QPalette.ColorRole.Text) == c.text
    assert _color(QPalette.ColorRole.Highlight) == c.accent
    assert contrast(c.accent, _color(QPalette.ColorRole.HighlightedText)) >= AA_TEXT
    assert app.styleSheet() == stylesheet(c)


@pytest.mark.gui
def test_icons_follow_theme_switch(app: QApplication) -> None:
    def ink(ic: QIcon) -> set[str]:
        image = ic.pixmap(QSize(24, 24)).toImage()
        return {
            image.pixelColor(x, y).name()
            for x in range(24)
            for y in range(24)
            if image.pixelColor(x, y).alpha() == 255
        }

    from pdfeditor.ui.icons import icon

    ic = icon("save")
    apply_theme(app, Theme.LIGHT)
    assert ink(ic) == {build_colors("light").text}
    apply_theme(app, Theme.DARK)
    assert ink(ic) == {build_colors("dark").text}


@pytest.mark.gui
def test_system_scheme_change_reapplies(app: QApplication) -> None:
    apply_theme(app, Theme.SYSTEM)
    app.setStyleSheet("")
    QGuiApplication.styleHints().colorSchemeChanged.emit(QGuiApplication.styleHints().colorScheme())
    assert app.styleSheet() == stylesheet(theme_manager().colors)


@pytest.mark.gui
def test_high_contrast_uses_system_colors(app: QApplication, monkeypatch) -> None:
    monkeypatch.setattr(ThemeManager, "high_contrast", lambda self: True)
    apply_theme(app, Theme.DARK)
    assert app.styleSheet() == ""
    assert _color(QPalette.ColorRole.Window) != build_colors("dark").window


@pytest.mark.gui
@pytest.mark.skipif(sys.platform != "win32", reason="Windows title bar")
def test_dark_theme_darkens_title_bar(app: QApplication, qtbot) -> None:
    if QGuiApplication.platformName() != "windows":
        pytest.skip("needs real windows (not offscreen)")
    import ctypes

    def dark(w: QLabel) -> bool:
        value = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(  # type: ignore[attr-defined]
            ctypes.c_void_p(int(w.winId())), 20, ctypes.byref(value), ctypes.sizeof(value)
        )
        return bool(value.value)

    window = QLabel("x")
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    apply_theme(app, Theme.DARK)
    qtbot.waitUntil(lambda: dark(window))
    apply_theme(app, Theme.LIGHT)
    qtbot.waitUntil(lambda: not dark(window))


@pytest.mark.gui
def test_semantic_label_roles(app: QApplication, qtbot) -> None:
    apply_theme(app, Theme.DARK)
    label = QLabel("Passwords don't match")
    label.setProperty("role", "error")
    qtbot.addWidget(label)
    label.ensurePolished()
    assert (
        label.palette().color(QPalette.ColorRole.WindowText).name()
        == build_colors("dark", theme_manager().accent or theme_manager().system_accent).danger
    )


@pytest.mark.gui
def test_selected_page_thumbnails_are_not_tinted(app: QApplication) -> None:
    apply_theme(app, Theme.LIGHT, "#00b25a")
    pix = QPixmap(40, 50)
    pix.fill(QColor("#ffffff"))
    ic = page_icon(pix)
    selected = ic.pixmap(QSize(40, 50), QIcon.Mode.Selected).toImage()
    assert selected.pixelColor(20, 25).name() == "#ffffff"


# -- preferences -------------------------------------------------------------------------------
@pytest.mark.gui
def test_preferences_save_theme_and_accent(app: QApplication, qtbot) -> None:
    settings = AppSettings()
    dialog = PreferencesDialog(settings)
    qtbot.addWidget(dialog)
    assert dialog.accent.currentData() == ""  # follows Windows by default
    dialog.theme.setCurrentIndex(dialog.theme.findData("dark"))
    dialog.accent.setCurrentIndex(dialog.accent.findData("#8764b8"))
    dialog.accept()
    assert (settings.theme, settings.accent) == ("dark", "#8764b8")
    reopened = PreferencesDialog(settings)
    qtbot.addWidget(reopened)
    assert reopened.theme.currentData() == "dark"
    assert reopened.accent.currentData() == "#8764b8"


@pytest.mark.gui
def test_preferences_custom_accent(app: QApplication, qtbot, monkeypatch) -> None:
    settings = AppSettings()
    dialog = PreferencesDialog(settings)
    qtbot.addWidget(dialog)
    custom = dialog.accent.findData("custom")
    monkeypatch.setattr(QColorDialog, "getColor", lambda *a, **k: QColor("#123456"))
    dialog.accent.setCurrentIndex(custom)
    dialog._accent_activated(custom)
    assert dialog.accent.currentData() == "#123456"
    monkeypatch.setattr(QColorDialog, "getColor", lambda *a, **k: QColor())  # cancelled
    custom = dialog.accent.findData("custom")
    dialog.accent.setCurrentIndex(custom)
    dialog._accent_activated(custom)
    assert dialog.accent.currentData() == "#123456"  # back to the previous choice
    dialog.accept()
    assert settings.accent == "#123456"
    reopened = PreferencesDialog(settings)
    qtbot.addWidget(reopened)
    assert reopened.accent.currentData() == "#123456"


@pytest.mark.gui
def test_main_window_applies_saved_theme(app: QApplication, qtbot) -> None:
    from pdfeditor.ui.main_window import MainWindow

    settings = AppSettings()
    settings.theme, settings.accent = "dark", "#c42b1c"
    window = MainWindow()
    qtbot.addWidget(window)
    assert theme_manager().colors == build_colors("dark", "#c42b1c")
    assert window.theme_actions[Theme.DARK].isChecked()
    window.theme_actions[Theme.LIGHT].trigger()
    assert settings.theme == "light"
    assert theme_manager().colors == build_colors("light", "#c42b1c")
    window.close()
