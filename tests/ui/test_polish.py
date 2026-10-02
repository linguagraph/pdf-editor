"""Phase 16: shortcuts, command palette, accessibility names, translations, hard cases."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QAbstractButton, QWidget

from pdfeditor.core.session import DocumentSession
from pdfeditor.services.text import TextIndexCache
from pdfeditor.ui.i18n import available_languages, install_translators
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.shortcuts import CommandPalette, ShortcutManager, ShortcutsDialog

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    w.resize(1200, 900)
    w.show()
    qtbot.waitExposed(w)
    yield w
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()


def test_acrobat_defaults_and_custom_shortcuts(window: MainWindow) -> None:
    m = window.shortcuts
    assert "Ctrl+W" in m.current("close") and "Ctrl+Q" in m.current("exit")
    assert m.current("command-palette") == ("Ctrl+Shift+P",)
    assert m.actions["sticky-note"].shortcut() == QKeySequence("Ctrl+6")
    assert m.conflicts("fit-width", "Ctrl+S") == ["save"]
    m.set("fit-width", ("Ctrl+Alt+W",))
    assert m.actions["fit-width"].shortcut() == QKeySequence("Ctrl+Alt+W")
    # a fresh manager (next start) applies the saved override
    again = ShortcutManager(window)
    assert again.current("fit-width") == ("Ctrl+Alt+W",)
    m.set("fit-width", ())
    assert m.actions["fit-width"].shortcut().isEmpty()
    m.reset()
    assert m.current("fit-width") == ("Ctrl+2",)


def test_shortcuts_dialog(window: MainWindow) -> None:
    d = ShortcutsDialog(window.shortcuts, window)
    d.filter.setText("fit width")
    assert d.table.rowCount() == 1
    d.select("fit-width")
    assert not d.apply_keys("Ctrl+S")  # taken by Save
    assert "Save" in d.message.text()
    assert d.apply_keys("Ctrl+Alt+F")
    assert window.shortcuts.current("fit-width") == ("Ctrl+Alt+F",)
    d._reset_all()
    assert window.shortcuts.current("fit-width") == ("Ctrl+2",)
    d.close()


def test_command_palette(window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("report"))
    palette = CommandPalette(window.shortcuts, window)
    palette.query.setText("rotate view clock")
    assert palette.list.count() >= 1
    assert palette.list.item(0).text().startswith("Rotate View Clockwise")
    before = view.rotation
    assert palette.run_current()
    assert view.rotation == (before + 90) % 360
    palette = CommandPalette(window.shortcuts, window)
    palette.query.setText("no such command at all")
    assert palette.list.count() == 0 and not palette.run_current()


def test_every_focusable_widget_is_named(window: MainWindow, fixture_pdf) -> None:
    window.open_path(fixture_pdf("report"))
    unnamed = []
    for w in window.findChildren(QWidget):
        if not w.isVisibleTo(window) or w.focusPolicy() == Qt.FocusPolicy.NoFocus:
            continue
        text = w.text() if isinstance(w, QAbstractButton) else ""
        if not (w.accessibleName() or text or w.toolTip()):
            unnamed.append(type(w).__name__)
    assert unnamed == []


def test_translations(qapp) -> None:
    assert "en" in available_languages()
    translators = install_translators(QCoreApplication.instance(), "de")
    try:
        assert translators  # Qt's own German strings
        assert QCoreApplication.translate("QPlatformTheme", "Cancel") == "Abbrechen"
    finally:
        for t in translators:
            QCoreApplication.removeTranslator(t)


def test_repaired_file_is_announced(window: MainWindow, fixture_pdf, tmp_path: Path) -> None:
    data = fixture_pdf("report").read_bytes()
    path = tmp_path / "damaged.pdf"
    path.write_bytes(data[: len(data) // 2])
    assert window.open_path(path) is not None
    assert "repaired" in window.toasts.last_text()
    assert window.toasts.toasts()[-1].text == window.toasts.last_text()


def test_select_all_is_instant_on_huge_documents(window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("large_1000"))
    start = time.perf_counter()
    view.select_all()
    assert time.perf_counter() - start < 0.2
    assert view.has_selection()
    assert view.overlays(0)  # highlight of the visible page is worked out when painted


def test_text_cache_is_bounded(fixture_pdf) -> None:
    session = DocumentSession.open(fixture_pdf("text_multipage"))
    cache = TextIndexCache(session, max_pages=2)
    for i in range(5):
        cache.get(i)
    assert len(cache) == 2
    assert cache.get(4).text  # still works after eviction
    session.close()
