"""Phase U10: keyboard, screen readers, translations, scaling and High Contrast for the new UI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QPoint, QRect, Qt, QTranslator
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QLabel,
    QToolButton,
    QWidget,
)

from pdfeditor.core.commands import AddAnnotationCommand
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.geometry import Rect
from pdfeditor.services.text import TextPos, TextSelection
from pdfeditor.ui import theme as theme_module
from pdfeditor.ui.contextual import MiniToolbar
from pdfeditor.ui.dialogs.base import FormDialog
from pdfeditor.ui.dialogs.confirm import ConfirmDialog
from pdfeditor.ui.jobs import Job
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.panels.base import EmptyState
from pdfeditor.ui.side_panels import RailButton
from pdfeditor.ui.style.qss import font_pt, stylesheet
from pdfeditor.ui.style.tokens import (
    AA_NON_TEXT,
    NON_TEXT_PAIRS,
    build_colors,
    contrast,
)
from pdfeditor.ui.theme import Theme, ThemeManager, apply_theme, current_colors, theme_manager
from pdfeditor.ui.toasts import Toast
from pdfeditor.ui.view.mode_banner import ModeBanner
from pdfeditor.ui.view.pill import CanvasPill
from tests.ui.test_dialog_base import migrated

pytestmark = pytest.mark.gui

ACCENTS = [None, "#00b25a", "#ffd700", "#00ffff", "#0000ff", "#ffffff", "#000000", "#c42b1c"]


def add_comment(view) -> None:
    note = AnnotationModel(AnnotationType.TEXT, 0, Rect(100, 100, 120, 120), contents="Hi")
    view.session.execute(AddAnnotationCommand(note))


def make_window(qtbot, width: int = 1280, height: int = 720) -> MainWindow:
    w = MainWindow()
    w.resize(width, height)
    w.show()
    qtbot.waitExposed(w)
    w.activateWindow()
    return w


def close_window(w: MainWindow) -> None:
    w.jobs.cancel_all()
    w.toasts.clear()
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()


@pytest.fixture
def window(qtbot) -> Iterator[MainWindow]:
    w = make_window(qtbot)
    yield w
    close_window(w)


def settle(qtbot, n: int = 5) -> None:
    for _ in range(n):
        QApplication.processEvents()
    qtbot.wait(10)


def focus() -> QWidget | None:
    return QApplication.focusWidget()


# -- keyboard: F6 regions -----------------------------------------------------------------
def test_f6_cycles_through_the_regions_and_back(qtbot, window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("report"))
    assert view is not None
    window.show_panel(window.comments_panel)
    window.inspector_panels.open(window.inspector)
    window.notify("Saved", "success")
    settle(qtbot)
    view.setFocus()
    assert window.focus_regions.current().name == "page"

    visited = [window.cycle_focus(1) for _ in range(5)]
    # the status bar has nothing to focus without a job or a tool: skipped
    assert visited == ["right panels", "notifications", "ribbon", "left panels", "page"]
    assert focus() is view
    assert window.cycle_focus(-1) == "left panels"
    assert window.nav_dock.isAncestorOf(focus())
    assert window.cycle_focus(-1) == "ribbon"
    assert focus() is window.ribbon.bar
    assert window.cycle_focus(-1) == "notifications"
    toast = window.toasts.toasts()[-1]
    assert focus() is toast.close_button and toast.paused  # focus holds the toast
    # coming back to a region restores where the keyboard was
    window.cycle_focus(1)  # ribbon
    window.cycle_focus(1)  # left panels
    target = window.comments_panel.import_button
    target.setFocus()
    window.cycle_focus(1)  # page
    assert window.cycle_focus(-1) == "left panels" and focus() is target


def test_f6_key_and_menu_entries(qtbot, window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("report"))
    assert view is not None
    assert window.act_next_pane.shortcut().toString() == "F6"
    assert window.act_prev_pane.shortcut().toString() == "Shift+F6"
    assert window.shortcuts.current("next-pane") == ("F6",)
    view.setFocus()
    window.act_next_pane.trigger()
    assert window.focus_regions.current().name != "page"
    window.act_prev_pane.trigger()
    assert focus() is view


def test_f6_reaches_the_job_chip_and_start_page(qtbot, window: MainWindow) -> None:
    gate = threading.Event()

    def work(job: Job) -> None:
        job.progress(0, 2)
        gate.wait(10)

    window.focus_page()
    assert focus() is window.start_page.open_button  # no document: the start page
    job = window.jobs.start("Exporting…", work, total=2)
    qtbot.waitUntil(window.progress_chip.isVisible, timeout=3000)
    names = []
    for _ in range(4):
        names.append(window.cycle_focus(1))
        if names[-1] == "status bar":
            break
    assert names[-1] == "status bar" and focus() is window.progress_chip.button
    gate.set()
    qtbot.waitUntil(lambda: job.is_settled, timeout=5000)


def test_escape_in_a_side_panel_returns_to_the_page(qtbot, window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("report"))
    assert view is not None
    window.show_panel(window.comments_panel)
    settle(qtbot)
    window.comments_panel.import_button.setFocus()
    qtbot.keyClick(window.comments_panel.import_button, Qt.Key.Key_Escape)
    assert focus() is view
    rail = window.nav_panels.button(window.comments_panel)
    rail.setFocus()
    qtbot.keyClick(rail, Qt.Key.Key_Down)
    assert focus() is window.nav_panels.button(window.protect.panel)  # arrows on the rail
    qtbot.keyClick(focus(), Qt.Key.Key_Escape)
    assert focus() is view


def test_ribbon_buttons_are_tab_stops_in_order(qtbot, window: MainWindow) -> None:
    ribbon = window.ribbon
    home = ribbon.tab("Home")
    buttons = [home.groups[0].bar.widgetForAction(a) for a in home.groups[0].bar.actions()]
    assert all(b.focusPolicy() == Qt.FocusPolicy.TabFocus for b in buttons)
    ribbon.bar.setFocus()
    qtbot.keyClick(ribbon.bar, Qt.Key.Key_Tab)
    assert focus() is buttons[0]  # straight from the tab row into the current tab


# -- focus ring -----------------------------------------------------------------------------
def test_focus_rules_are_two_pixel_accent_rings() -> None:
    for scheme in ("light", "dark"):
        c = build_colors(scheme)
        qss = stylesheet(c)
        for selector in ("QToolButton:focus", "QPushButton:focus", "QLineEdit:focus"):
            block = qss.split(selector, 1)[1].split("}", 1)[0]
            assert f"2px solid {c.accent_text}" in block, selector
        # rules that remove Qt's focus outline have a replacement (or the widget never
        # takes focus, see CommandSearchPopup)
        assert 'QListWidget[role="sidebar"]:focus' in qss


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("accent", ACCENTS)
def test_rings_reach_three_to_one(scheme, accent) -> None:
    c = build_colors(scheme, accent)
    for fg, bg in NON_TEXT_PAIRS:
        assert contrast(getattr(c, fg), getattr(c, bg)) >= AA_NON_TEXT, (fg, bg)
    # "current" and "selected" page rings are told apart by more than color, but the
    # colors differ too
    assert c.ring_selected != c.accent_text


def _ring_pixels(widget: QWidget, color: str) -> int:
    image = widget.grab().toImage()
    target = QColor(color)
    count = 0
    for x in range(image.width()):
        for y in (0, 1, image.height() - 1, image.height() - 2):
            p = image.pixelColor(x, y)
            if max(abs(p.red() - target.red()), abs(p.green() - target.green())) <= 24 and (
                abs(p.blue() - target.blue()) <= 24
            ):
                count += 1
    return count


@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_focused_widget_shows_the_ring(qtbot, window: MainWindow, theme: Theme) -> None:
    window.set_theme(theme)
    try:
        button = window.nav_panels.button(window.comments_panel)
        other = window.nav_panels.button(window.search_panel)
        button.setFocus()
        settle(qtbot)
        assert button.hasFocus()
        ring = current_colors().accent_text
        assert _ring_pixels(button, ring) > 20
        assert _ring_pixels(other, ring) == 0
    finally:
        window.set_theme(Theme.SYSTEM)


def test_caption_size_follows_the_app_font() -> None:
    assert font_pt(9.0, 0.89) == "8pt"
    assert font_pt(12.0, 0.89) == "10.5pt"  # a larger system font: larger captions
    assert font_pt(6.0, 0.89) == "8pt"  # never below 8 pt
    qss = stylesheet(build_colors("light"), base_pt=12.0)
    assert 'QLabel[role="caption"] { color: #5c5c5c; font-size: 10.5pt' in qss


# -- screen readers -------------------------------------------------------------------------
CUSTOM = (Toast, CanvasPill, ModeBanner, MiniToolbar, RailButton, EmptyState)


def unnamed(root: QWidget) -> list[str]:
    """Visible widgets a screen reader can't name: focusable ones without a name, text or
    tooltip, and the new custom widgets without an accessible name."""
    out = []
    for w in [root, *root.findChildren(QWidget)]:
        if not w.isVisible():
            continue
        if isinstance(w, CUSTOM) and type(w) is not EmptyState and not w.accessibleName():
            out.append(f"{type(w).__name__} (custom)")
            continue
        if w.focusPolicy() == Qt.FocusPolicy.NoFocus or w.objectName().startswith("qt_"):
            continue  # (qt_*: the line edit inside a spin or combo box, named by its owner)
        text = w.text() if isinstance(w, QAbstractButton | QLabel) else ""
        if not (w.accessibleName() or text or w.toolTip()):
            out.append(f"{type(w).__name__} {w.objectName()}")
    return out


def all_windows_unnamed(window: MainWindow) -> list[str]:
    found = unnamed(window)
    for top in QApplication.topLevelWidgets():
        if top is not window and top.isVisible():
            found += unnamed(top)
    return found


def test_widgets_are_named_in_every_state(qtbot, window: MainWindow, fixture_pdf) -> None:
    assert all_windows_unnamed(window) == []  # empty start page
    view = window.open_path(fixture_pdf("report"))
    assert view is not None
    add_comment(view)  # so the comments panel shows its list, filters and buttons
    for panel in window.panels:
        window.show_panel(panel)
        settle(qtbot, 2)
        assert all_windows_unnamed(window) == [], type(panel).__name__
    window.inspector_panels.open(window.inspector)
    window.set_tool("edit")  # mode banner
    settle(qtbot)
    assert window.mode_banner.isVisible()
    assert window.mode_banner.accessibleName() == "Mode: Editing text & images"
    assert all_windows_unnamed(window) == []
    window.set_tool("select")
    view.set_selection(TextSelection(TextPos(0, 0), TextPos(0, 20)))  # mini toolbar
    settle(qtbot)
    assert window.contextual.text_bar.isVisible()
    assert all_windows_unnamed(window) == []
    window.notify("3 pages deleted", "success")  # toasts
    window.notify("Couldn't save", "error")
    settle(qtbot)
    assert all_windows_unnamed(window) == []
    toast = window.toasts.toasts()[-1]
    assert toast.accessibleName() == "Error: Couldn't save"

    gate = threading.Event()  # the job chip and its details
    job = window.jobs.start("Recognizing text…", lambda j: gate.wait(10), total=0)
    qtbot.waitUntil(window.progress_chip.isVisible, timeout=3000)
    details = window.progress_chip.show_details()
    assert details is not None
    settle(qtbot)
    assert all_windows_unnamed(window) == []
    details.close()
    gate.set()
    qtbot.waitUntil(lambda: job.is_settled, timeout=5000)


def test_every_form_dialog_is_named(qtbot, window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("report"))
    assert view is not None
    factories: dict[str, Callable[[], FormDialog]] = dict(migrated(window, view))
    factories["confirm"] = lambda: ConfirmDialog("Delete?", "Gone for good.", "Delete", window)
    for name, make in factories.items():
        dialog = make()
        dialog.show()
        settle(qtbot, 2)
        assert unnamed(dialog) == [], name
        dialog.close()
        dialog.deleteLater()


def test_rail_badges_are_spoken(qtbot, window: MainWindow, fixture_pdf) -> None:
    window.open_path(fixture_pdf("report"))
    button = window.nav_panels.button(window.comments_panel)
    button.set_count(6)
    assert button.accessibleName() == "Comments, 6 items"
    button.set_count(1)
    assert button.accessibleName() == "Comments, 1 item"
    button.set_count(0)
    assert button.accessibleName() == "Comments"


# -- translations ---------------------------------------------------------------------------
class FakeTranslator(QTranslator):
    """Translates every string with ``fn`` (prefix "XX", or a longer pseudo-locale)."""

    def __init__(self, fn: Callable[[str], str]) -> None:
        super().__init__()
        self.fn = fn

    def translate(  # type: ignore[override]
        self, context: str, source: str, disambiguation: str | None = None, n: int = -1
    ) -> str:
        return self.fn(source) if source else source

    def isEmpty(self) -> bool:
        return False


@pytest.fixture
def translated(qtbot) -> Iterator[Callable[[Callable[[str], str]], None]]:
    installed: list[QTranslator] = []

    def install(fn: Callable[[str], str]) -> None:
        t = FakeTranslator(fn)
        QCoreApplication.installTranslator(t)
        installed.append(t)

    yield install
    for t in installed:
        QCoreApplication.removeTranslator(t)


def test_new_widgets_go_through_tr(qtbot, translated, fixture_pdf) -> None:
    translated(lambda s: "XX" + s)
    w = make_window(qtbot)
    try:
        start = w.start_page
        assert start.accessibleName().startswith("XX")
        assert start.open_button.text().strip().startswith("XX")
        assert start.quick_buttons["Combine files"].text().startswith("XX")
        assert start.recent_view.accessibleName().startswith("XX")
        assert w.nav_panels.no_document.title.text().startswith("XX")
        assert w.nav_panels.close_button.toolTip().startswith("XX")
        assert w.ribbon.bar.tabText(0).startswith("XX")
        assert w.ribbon.tab("Home").groups[0].label.text().startswith("XX")
        assert w.ribbon.menu_button.accessibleName().startswith("XX")
        assert w.command_search.edit.placeholderText().startswith("XX")
        assert w.tabs.plus_button.toolTip().startswith("XX")
        assert w.pill.navigator.edit.accessibleName().startswith("XX")
        assert w.pill.zoom_box.itemText(0).startswith("XX")
        assert w.contextual.copy_button.accessibleName().startswith("XX")
        assert w.mode_banner.done_button.text().startswith("XX")
        view = w.open_path(fixture_pdf("report"))
        assert view is not None
        rail = w.nav_panels.button(w.comments_panel)
        assert rail.accessibleName().startswith("XX")
        w.set_tool("edit")
        assert w.mode_banner.text_label.text().startswith("XX")
        w.set_tool("select")
        w.pill.zoom_box._apply(w.pill.zoom_box.itemText(0))  # "Fit Width", translated
        toast = w.notify("hello", "info")
        assert toast.close_button.accessibleName().startswith("XX")
        assert w.comments_panel.import_button.accessibleName().startswith("XX")
        d = ConfirmDialog("t", "x", "Delete", w)
        section = d.add_section("Advanced")
        assert section.header.toolTip().startswith("XX")
        d.deleteLater()
    finally:
        close_window(w)


def _pseudo(s: str) -> str:
    """~35 % longer, with the original text intact (placeholders keep working)."""
    return "[" + s + "~" * max(2, len(s) * 35 // 100) + "]"


def _clipped(label: QLabel) -> bool:
    if not label.isVisible() or not label.text():
        return False
    if label.wordWrap():
        return label.height() < label.heightForWidth(label.width())
    return label.width() < label.sizeHint().width() - 1


def _overlaps(widgets: list[QWidget]) -> list[tuple[str, str]]:
    shown = [w for w in widgets if w.isVisible()]
    rects = [(w, QRect(w.mapTo(w.window(), QPoint(0, 0)), w.size())) for w in shown]
    return [
        (a.objectName() or type(a).__name__, b.objectName() or type(b).__name__)
        for i, (a, ra) in enumerate(rects)
        for b, rb in rects[i + 1 :]
        if ra.intersects(rb)
    ]


def test_layouts_survive_longer_strings(qtbot, translated) -> None:
    translated(_pseudo)
    w = make_window(qtbot)
    try:
        settle(qtbot)
        # ribbon: every tab's groups show their whole caption and don't overlap
        for index, name in enumerate(w.ribbon.tab_names()):
            w.ribbon.bar.setCurrentIndex(index)
            settle(qtbot, 2)
            tab = w.ribbon.tab(name)
            groups = [g for g in tab.groups if g.isVisible()]
            assert groups, name
            assert [g.label.text() for g in groups if _clipped(g.label)] == [], name
            assert _overlaps(list(groups)) == [], name
        # start page: texts wrap instead of being cut, quick actions don't overlap
        start = w.start_page
        labels = start.findChildren(QLabel)
        assert [lb.text() for lb in labels if _clipped(lb)] == []
        assert _overlaps(list(start.quick_buttons.values())) == []
        for button in start.quick_buttons.values():
            assert button.width() >= button.sizeHint().width()
        # side panel empty state (no document) wraps too
        empty = w.nav_panels.no_document
        assert [lb.text() for lb in (empty.title, empty.text) if _clipped(lb)] == []
        assert (
            empty.button.width() >= empty.button.sizeHint().width() or not empty.button.isVisible()
        )
    finally:
        close_window(w)


# -- 1280 x 720 -------------------------------------------------------------------------------
def test_small_window_layout(qtbot, window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("text_multipage"))
    assert view is not None
    window.show_panel(window.comments_panel)
    window.inspector_panels.open(window.inspector)
    window.set_tool("edit")
    window.notify("One", "info")
    window.notify("Two with a longer text that wraps onto a second line", "success")
    settle(qtbot, 10)
    assert window.width() == 1280 and window.height() == 720
    port = view.viewport()
    pill = QRect(window.pill.mapTo(window, QPoint(0, 0)), window.pill.size())
    banner = window.mode_banner
    assert banner.isVisible() and window.pill.isVisible()
    chip = QRect(banner.chip.mapTo(window, QPoint(0, 0)), banner.chip.size())
    assert not chip.intersects(pill)
    for toast in window.toasts.toasts():
        frame = QRect(toast.frame.mapTo(window, QPoint(0, 0)), toast.frame.size())
        assert not frame.intersects(pill) and not frame.intersects(chip)
    # the page area keeps a usable size, the side panels fit
    assert port.width() >= 400 and port.height() >= 300
    for dock in (window.nav_dock, window.inspector_dock):
        assert dock.geometry().right() <= window.width()
        assert dock.panels.content.width() >= 160
    # the comments panel's buttons fit its default width side by side
    panel = window.comments_panel
    add_comment(view)
    settle(qtbot, 5)
    row = [panel.reply_button, panel.import_button, panel.export_button]
    assert all(b.isVisible() for b in row)
    assert _overlaps(row) == []
    assert all(b.width() >= b.minimumSizeHint().width() for b in row)
    right = max(b.geometry().right() for b in row)
    assert right <= panel.width()


def test_narrow_ribbon_overflows_whole_groups(qtbot, window: MainWindow) -> None:
    window.resize(900, 720)
    window.ribbon.bar.setCurrentIndex(window.ribbon.tab_names().index("Organize"))
    settle(qtbot, 5)
    tab = window.ribbon.tab("Organize")
    more = tab.findChild(QToolButton, "qt_toolbar_ext_button")
    assert more is not None and more.isVisible()
    shown = [g for g in tab.groups if g.isVisible()]
    assert 0 < len(shown) < len(tab.groups)
    assert _overlaps(shown) == []
    assert all(g.geometry().right() <= more.geometry().left() for g in shown)


def test_start_page_scrolls_in_a_small_window(qtbot, window: MainWindow, fixture_pdf) -> None:
    for name in ("report", "text_multipage", "outline", "large_1000"):
        window.recent.add(str(fixture_pdf(name).resolve()))
    window.resize(1000, 500)
    settle(qtbot, 5)
    scroll = window.start_page.findChild(QWidget, "StartScroll")
    assert scroll is not None
    assert scroll.verticalScrollBar().maximum() > 0  # type: ignore[attr-defined]
    assert window.start_page.width() <= window.width()


# -- scale factors (subprocess: QT_SCALE_FACTOR applies at startup) ----------------------------
SCALE_SCRIPT = textwrap.dedent(
    """
    import json, sys, tempfile
    from PySide6.QtCore import QCoreApplication, QPoint, QRect, QSettings, QSize
    from PySide6.QtWidgets import QApplication
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, tempfile.mkdtemp())
    QCoreApplication.setOrganizationName("pdfeditor-tests")
    QCoreApplication.setApplicationName("pdfeditor-tests")
    app = QApplication([])
    from pdfeditor.ui.icons import icon
    from pdfeditor.ui.main_window import MainWindow
    from pdfeditor.ui.view import motion
    motion.set_animations_enabled(False)
    w = MainWindow()
    w.resize(1280, 720)
    w.show()
    for _ in range(20):
        app.processEvents()
    view = w.open_path(__import__("pathlib").Path(sys.argv[1]))
    w.show_panel(w.comments_panel)
    w.notify("Saved", "success")
    for _ in range(40):
        app.processEvents()
    def rect(widget):
        return QRect(widget.mapTo(w, QPoint(0, 0)), widget.size())
    pill, toast = rect(w.pill), rect(w.toasts.toasts()[-1].frame)
    groups = [g for g in w.ribbon.tab("Home").groups if g.isVisible()]
    out = {
        "dpr": w.devicePixelRatioF(),
        "size": [w.width(), w.height()],
        "icon": icon("x").pixmap(QSize(16, 16), w.devicePixelRatioF()).width(),
        "grab": w.grab().width(),
        "pill_in_view": view.viewport().rect().contains(
            QRect(w.pill.pos(), w.pill.size())
        ),
        "toast_pill_overlap": toast.intersects(pill),
        "ribbon_height": w.ribbon.height(),
        "group_overlap": any(
            rect(a).intersects(rect(b)) for i, a in enumerate(groups) for b in groups[i + 1:]
        ),
        "caption_px": groups[0].label.fontMetrics().height(),
        "panel_width": w.nav_panels.content.width(),
    }
    for v in w.views():
        v.session.undo_stack.set_clean()
    w.close()
    print("RESULT " + json.dumps(out))
    """
)


@pytest.mark.slow
@pytest.mark.parametrize("scale", ["1", "1.5", "2"])
def test_layout_at_scale_factors(fixture_pdf, tmp_path: Path, scale: str) -> None:
    script = tmp_path / "scale.py"
    script.write_text(SCALE_SCRIPT)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=scale)
    env.pop("QT_SCREEN_SCALE_FACTORS", None)
    proc = subprocess.run(
        [sys.executable, str(script), str(fixture_pdf("report"))],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        cwd=Path(__file__).resolve().parents[2],
    )
    line = next((x for x in proc.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line is not None, proc.stdout + proc.stderr
    r = json.loads(line.removeprefix("RESULT "))
    factor = float(scale)
    assert r["dpr"] == pytest.approx(factor)
    assert r["size"] == [1280, 720]  # logical pixels
    assert r["icon"] == round(16 * factor)  # icons are rendered for the pixel ratio: sharp
    assert r["grab"] == round(1280 * factor)
    assert r["pill_in_view"] and not r["toast_pill_overlap"]
    assert not r["group_overlap"]
    assert 60 <= r["ribbon_height"] <= 160
    assert r["caption_px"] >= 10  # captions stay legible
    assert r["panel_width"] >= 160


# -- High Contrast --------------------------------------------------------------------------
HC = {
    QPalette.ColorRole.Window: "#000000",
    QPalette.ColorRole.WindowText: "#ffff00",
    QPalette.ColorRole.Base: "#000080",
    QPalette.ColorRole.AlternateBase: "#000060",
    QPalette.ColorRole.Text: "#ffffff",
    QPalette.ColorRole.Button: "#000000",
    QPalette.ColorRole.ButtonText: "#ffff00",
    QPalette.ColorRole.Highlight: "#00ffff",
    QPalette.ColorRole.HighlightedText: "#000000",
    QPalette.ColorRole.ToolTipBase: "#000000",
    QPalette.ColorRole.ToolTipText: "#ffffff",
}


@pytest.fixture
def high_contrast(qtbot, monkeypatch) -> Iterator[None]:
    app = QApplication.instance()
    assert isinstance(app, QApplication)

    def palette() -> QPalette:
        p = QPalette()
        for role, color in HC.items():
            p.setColor(role, QColor(color))
        return p

    monkeypatch.setattr(ThemeManager, "high_contrast", lambda self: True)
    monkeypatch.setattr(theme_module, "system_palette", palette)
    apply_theme(app, Theme.DARK)
    yield
    monkeypatch.undo()
    apply_theme(app, Theme.SYSTEM)


def _has(widget: QWidget, color: str, tolerance: int = 8) -> bool:
    image = widget.grab().toImage()
    target = QColor(color)
    step = max(1, min(image.width(), image.height()) // 40)
    for x in range(0, image.width(), step):
        for y in range(0, image.height(), step):
            p = image.pixelColor(x, y)
            if (
                abs(p.red() - target.red()) <= tolerance
                and abs(p.green() - target.green()) <= tolerance
                and abs(p.blue() - target.blue()) <= tolerance
            ):
                return True
    return False


def test_high_contrast_widgets_paint_with_the_palette(qtbot, high_contrast, fixture_pdf) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication) and app.styleSheet() == ""
    c = current_colors()
    assert c.surface == HC[QPalette.ColorRole.Base] and c.text == HC[QPalette.ColorRole.WindowText]
    assert c.accent == HC[QPalette.ColorRole.Highlight] and theme_manager().high_contrast_applied
    w = make_window(qtbot)
    try:
        view = w.open_path(fixture_pdf("report"))
        assert view is not None
        w.show_panel(w.comments_panel)
        w.set_tool("edit")
        settle(qtbot, 10)
        # the pill and the mini toolbar: palette Base body, WindowText outline
        assert _has(w.pill, HC[QPalette.ColorRole.Base])
        assert _has(w.pill, HC[QPalette.ColorRole.WindowText])
        # the banner chip: Highlight fill; its text uses HighlightedText
        assert _has(w.mode_banner.chip, HC[QPalette.ColorRole.Highlight])
        label_color = w.mode_banner.text_label.palette().color(QPalette.ColorRole.WindowText)
        assert label_color.name() == HC[QPalette.ColorRole.HighlightedText]
        w.set_tool("select")
        view.set_selection(TextSelection(TextPos(0, 0), TextPos(0, 20)))
        settle(qtbot)
        assert _has(w.contextual.text_bar, HC[QPalette.ColorRole.Base])
        # a checked rail button's bar and badge: Highlight
        rail = w.nav_panels.button(w.comments_panel)
        rail.set_count(3)
        assert _has(rail, HC[QPalette.ColorRole.Highlight])
        # toasts: an opaque Base box (no style sheet would leave them see-through)
        toast = w.notify("Saved", "success")
        settle(qtbot)
        assert toast.frame.autoFillBackground()
        assert _has(toast.frame, HC[QPalette.ColorRole.Base])
        # page cards: the current page's ring in Highlight
        w.show_panel(w.panels[0])  # pages
        settle(qtbot, 10)
        thumbs = w.panels[0]
        assert _has(thumbs.list.viewport(), HC[QPalette.ColorRole.Highlight])  # type: ignore[attr-defined]
    finally:
        close_window(w)


def test_job_details_popup_has_no_square_corners(qtbot, window: MainWindow) -> None:
    gate = threading.Event()
    job = window.jobs.start("Exporting…", lambda j: gate.wait(10))
    qtbot.waitUntil(window.progress_chip.isVisible, timeout=3000)
    details = window.progress_chip.show_details()
    assert details is not None
    assert details.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    image = details.grab().toImage()
    assert image.pixelColor(0, 0).alpha() == 0  # outside the rounded corner
    centre = image.pixelColor(image.width() // 2, image.height() - 4)
    assert centre.alpha() == 255
    details.close()
    gate.set()
    qtbot.waitUntil(lambda: job.is_settled, timeout=5000)
