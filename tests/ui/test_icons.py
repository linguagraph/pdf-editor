"""Toolbar icons: every button has one, and they follow the palette (light/dark, disabled)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QToolButton

from pdfeditor.bundle import data_path
from pdfeditor.ui.icons import icon
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.panels.bookmarks import BookmarksPanel

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    yield w
    w.close()


def _ink(ic: QIcon, mode: QIcon.Mode = QIcon.Mode.Normal) -> set[str]:
    """Colours of the icon's fully opaque pixels."""
    image = ic.pixmap(QSize(24, 24), mode).toImage()
    return {
        image.pixelColor(x, y).name()
        for x in range(24)
        for y in range(24)
        if image.pixelColor(x, y).alpha() == 255
    }


def test_every_toolbar_button_has_an_icon(window: MainWindow) -> None:
    actions = window.ribbon.button_actions()
    assert len(actions) > 80  # all ribbon tabs plus the quick bar
    assert [a.text() for a in actions if a.icon().isNull()] == []
    assert all(_ink(a.icon()) for a in actions)


def test_other_tool_buttons_have_icons(window: MainWindow, qtbot) -> None:
    nav = window.navigator
    buttons: list[QToolButton] = [nav.first, nav.prev, nav.next, nav.last, window.tool_exit]
    panel = BookmarksPanel()
    qtbot.addWidget(panel)
    buttons += [panel.add_button, panel.delete_button, panel.dest_button]
    assert [b.toolTip() for b in buttons if b.icon().isNull()] == []


def test_icons_are_tinted_with_the_palette(qtbot) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    original = QGuiApplication.palette()
    try:
        for text in ("#101010", "#f0f0f0"):  # light theme, then dark theme
            palette = QPalette(original)
            palette.setColor(QPalette.ColorRole.WindowText, QColor(text))
            palette.setColor(
                QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor("#808080")
            )
            app.setPalette(palette)
            ic = icon("save")
            assert _ink(ic) == {text}
            assert _ink(ic, QIcon.Mode.Disabled) == {"#808080"}
    finally:
        app.setPalette(original)


def test_hidpi_pixmap_is_sharp() -> None:
    pix = icon("hand").pixmap(QSize(24, 24), 2.0)
    assert pix.devicePixelRatio() == 2.0
    assert pix.width() == 48


def test_bundled_icons_render_and_are_licensed() -> None:
    folder = data_path("icons")
    assert (folder / "LICENSE").read_text(encoding="utf-8").startswith("ISC License")
    svgs = sorted(folder.glob("*.svg"))
    assert svgs
    for svg in svgs:
        assert b"currentColor" in svg.read_bytes(), svg.name
        assert _ink(icon(svg.stem)), svg.name


def test_missing_icon_fails_early() -> None:
    with pytest.raises(FileNotFoundError):
        icon("no-such-icon")
