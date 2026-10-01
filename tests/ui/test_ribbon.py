"""Window frame and ribbon (Phase U2): full-width ribbon, captions, overflow, compact and
collapsed modes, the hidden menu bar with Alt access, and tooltips."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QToolButton

from pdfeditor.ui.action_help import HELP
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.shortcuts import action_id

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot) -> Iterator[MainWindow]:
    w = MainWindow()
    qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    qtbot.waitExposed(w)
    yield w
    w.close()


def settle(qtbot) -> None:
    for _ in range(5):
        QApplication.processEvents()
    qtbot.wait(10)


def test_ribbon_spans_the_window_above_the_docks(window: MainWindow) -> None:
    assert window.nav_dock.isVisible()
    assert window.menuWidget().isAncestorOf(window.ribbon)
    assert window.ribbon.width() >= window.width() - 2
    dock_top = window.nav_dock.mapTo(window, QPoint(0, 0)).y()
    ribbon_bottom = window.ribbon.mapTo(window, QPoint(0, window.ribbon.height())).y()
    assert dock_top >= ribbon_bottom


def test_groups_have_captions_and_short_labels(window: MainWindow) -> None:
    for name in window.ribbon.tab_names():
        tab = window.ribbon.tab(name)
        assert tab.groups, name
        assert all(g.label.text() for g in tab.groups), name
    long = [a.iconText() for a in window.ribbon.button_actions() if len(a.iconText()) > 16]
    assert long == []
    assert window.act_properties.iconText() == "Properties"
    # menus keep the full name
    assert "Document Properties" in window.act_properties.text().replace("&", "")


def test_tooltips_show_name_shortcut_and_help(window: MainWindow) -> None:
    tip = window.act_save.toolTip()
    assert "<b>Save</b>" in tip and "Ctrl+S" in tip and HELP["save"] in tip
    window.shortcuts.set("save", ("Ctrl+Alt+S",))
    assert "Ctrl+Alt+S" in window.act_save.toolTip()
    window.shortcuts.reset("save")
    assert "Ctrl+S" in window.act_save.toolTip()
    missing = [action_id(a) for a in window.ribbon.button_actions() if action_id(a) not in HELP]
    assert missing == []


def test_narrow_window_moves_whole_groups_to_overflow(qtbot, window: MainWindow) -> None:
    window.ribbon.bar.setCurrentIndex(window.ribbon.tab_names().index("Organize"))
    tab = window.ribbon.tab("Organize")
    window.resize(2400, 900)  # wide enough for every group, whatever the platform's fonts
    settle(qtbot)
    assert all(g.isVisible() for g in tab.groups)
    window.resize(760, 700)
    settle(qtbot)
    hidden = [g for g in tab.groups if not g.isVisible()]
    assert hidden
    for g in tab.groups:  # groups are never squeezed: each shows all of its buttons
        assert g.width() >= g.sizeHint().width() or not g.isVisible()
    more = tab.findChild(QToolButton, "qt_toolbar_ext_button")
    assert more is not None and more.isVisible() and not more.icon().isNull()


def test_compact_and_collapsed_ribbon(qtbot, window: MainWindow) -> None:
    settle(qtbot)
    full = window.ribbon.height()
    window.act_compact_ribbon.trigger()
    settle(qtbot)
    compact = window.ribbon.height()
    assert compact < full - 20
    group = window.ribbon.tab("Home").groups[0]
    assert not group.label.isVisible()
    assert AppSettings().ribbon_compact
    window.act_compact_ribbon.trigger()

    window.act_collapse_ribbon.trigger()  # Ctrl+F1
    settle(qtbot)
    assert not window.ribbon.stack.isVisible() and window.ribbon.height() < compact
    assert AppSettings().ribbon_collapsed
    # clicking a tab brings the buttons back
    bar = window.ribbon.bar
    qtbot.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(2).center())
    assert window.ribbon.stack.isVisible() and not window.act_collapse_ribbon.isChecked()


def test_double_click_tab_toggles_collapse(qtbot, window: MainWindow) -> None:
    bar = window.ribbon.bar
    pos = bar.tabRect(1).center()

    def double_click() -> None:  # what a real mouse sends: a click, then the double-click
        qtbot.mouseClick(bar, Qt.MouseButton.LeftButton, pos=pos)
        bar.tabBarDoubleClicked.emit(1)

    double_click()
    assert window.ribbon.collapsed
    # on a collapsed ribbon the first click already expands; the double-click keeps it open
    double_click()
    assert not window.ribbon.collapsed
    double_click()
    assert window.ribbon.collapsed


def test_menu_bar_hidden_by_default_behind_menu_button(window: MainWindow) -> None:
    assert not window.menu_bar.isVisible()
    menu = window.ribbon.menu_button.menu()
    assert isinstance(menu, QMenu)
    assert [a.text() for a in menu.actions()] == [a.text() for a in window.menu_bar.actions()]
    window.act_menu_bar.trigger()
    assert window.menu_bar.isVisible() and AppSettings().show_menu_bar


def _top_menu(window: MainWindow, title: str) -> tuple[QAction, QMenu]:
    action = next(a for a in window.menu_bar.actions() if a.text().replace("&", "") == title)
    menu = action.menu()
    assert isinstance(menu, QMenu)
    return action, menu


def test_alt_letter_opens_menus_while_hidden(qtbot, window: MainWindow) -> None:
    _, view_menu = _top_menu(window, "View")
    window.activateWindow()
    qtbot.keyClick(window, Qt.Key.Key_V, Qt.KeyboardModifier.AltModifier)
    qtbot.waitUntil(view_menu.isVisible)
    assert window.menu_bar.isVisible()
    view_menu.hide()
    qtbot.waitUntil(lambda: not window.menu_bar.isVisible())


def test_alt_tap_opens_first_menu_but_alt_shortcuts_do_not(qtbot, window: MainWindow) -> None:
    _, file_menu = _top_menu(window, "File")
    qtbot.keyPress(window, Qt.Key.Key_Alt)
    qtbot.keyClick(window, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)  # Back
    qtbot.keyRelease(window, Qt.Key.Key_Alt)
    assert not file_menu.isVisible()
    qtbot.keyPress(window, Qt.Key.Key_Alt)
    qtbot.keyRelease(window, Qt.Key.Key_Alt)
    qtbot.waitUntil(file_menu.isVisible)
    file_menu.hide()
    qtbot.waitUntil(lambda: not window.menu_bar.isVisible())


def test_alt_watch_ends_when_the_app_loses_focus(qtbot, window: MainWindow) -> None:
    from PySide6.QtCore import QEvent

    _, file_menu = _top_menu(window, "File")
    qtbot.keyPress(window, Qt.Key.Key_Alt)
    app = QApplication.instance()
    assert app is not None
    app.sendEvent(app, QEvent(QEvent.Type.ApplicationDeactivate))  # Alt+Tab away
    assert not window._alt_tap
    qtbot.keyRelease(window, Qt.Key.Key_Alt)
    assert not file_menu.isVisible()


def test_shown_menu_bar_disables_extra_alt_shortcuts(qtbot) -> None:
    AppSettings().show_menu_bar = True
    w = MainWindow()
    qtbot.addWidget(w)
    w.show()
    assert w.menu_bar.isVisible()
    assert w._menu_shortcuts and not any(s.isEnabled() for s in w._menu_shortcuts)
    w.close()


def test_old_window_layout_is_not_restored(qtbot) -> None:
    from PySide6.QtWidgets import QMainWindow

    old = QMainWindow()  # a layout saved by an earlier version (no state version)
    qtbot.addWidget(old)
    QSettings().setValue("window/state", old.saveState())
    w = MainWindow()
    qtbot.addWidget(w)
    assert not w._state_restored
    w.close()
