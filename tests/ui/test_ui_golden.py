"""UI golden screenshots (Phase U11): the start page, a document with the Pages panel, the text
mini toolbar, a toast, the edit-mode banner and a FormDialog, in light and dark at 1x and 2x.

The references live in ``tests/golden/ui/<platform>/`` because font rasterization differs
between Windows and Linux. A platform without references skips with a reason. Regenerate on
the platform (headless, like CI) after an intended UI change with:

    QT_QPA_PLATFORM=offscreen uv run pytest tests/ui/test_ui_golden.py --update-goldens

Everything that could change between runs is pinned: the offscreen platform, the font, the
accent, the window size, animations off, no cursor blink, the mouse away from the window,
isolated settings, a fixed folder name for recent files and no timers that hide things.
2x shots are drawn with ``QWidget.render`` into an image with device pixel ratio 2 (the
process-wide scale factor can't change once the QApplication exists), so styles, text and
icons draw at 2x while page tiles, rendered for the 1x screen, are scaled up.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QRect, QSettings, QSize
from PySide6.QtGui import QColor, QCursor, QFont, QFontDatabase, QGuiApplication, QImage
from PySide6.QtWidgets import QApplication, QWidget

from pdfeditor.services.text import TextPos, TextSelection
from pdfeditor.ui.dialogs.pages import HeaderFooterDialog
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.theme import Theme, apply_theme
from pdfeditor.ui.toasts import ToastAction
from tests.golden.compare import Tolerance, compare, save_png

pytestmark = pytest.mark.gui

GOLDEN = Path(__file__).resolve().parent.parent / "golden" / "ui"
PLATFORM = {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
ACCENT = "#0067c0"
# Per platform: a UI font every machine of that OS has, at a fixed size. The offscreen
# platform on Windows sees no system fonts at all (text draws nothing), so the font files are
# registered as application fonts for these tests and removed again afterwards.
FONT = {"windows": ("Segoe UI", 9), "linux": ("DejaVu Sans", 9)}
FONT_FILES = {"windows": ("segoeui.ttf", "seguisb.ttf", "segoeuib.ttf")}
WINDOW = QSize(1100, 720)
THEMES = ("light", "dark")
RATIOS = (1, 2)
# Gray-scale text anti-aliasing and the font version can shift a few edge pixels between
# machines of the same OS; a moved, missing or recolored element is far above these.
TOLERANCE = Tolerance(max_mean=1.0, max_bad_fraction=0.003)
_failures_dir: Path | None = None


def failures_dir() -> Path:
    global _failures_dir
    if _failures_dir is None:
        _failures_dir = Path(tempfile.mkdtemp(prefix="pdfeditor-ui-golden-"))
    return _failures_dir


# -- determinism ------------------------------------------------------------------------------
@pytest.fixture
def pinned(qtbot, request) -> Iterator[None]:
    """Font, cursor and platform pinned for the test; restored afterwards."""
    if QGuiApplication.platformName() != "offscreen":
        pytest.skip("UI goldens are taken headless: set QT_QPA_PLATFORM=offscreen")
    if not (GOLDEN / PLATFORM).is_dir() and not request.config.getoption("--update-goldens"):
        pytest.skip(
            f"no UI goldens for {PLATFORM} yet: take them on {PLATFORM} with "
            "QT_QPA_PLATFORM=offscreen pytest tests/ui/test_ui_golden.py --update-goldens"
        )
    font_ids = [QFontDatabase.addApplicationFont(str(f)) for f in _font_files()]
    if PLATFORM not in FONT or FONT[PLATFORM][0] not in QFontDatabase.families():
        for font_id in font_ids:
            QFontDatabase.removeApplicationFont(font_id)
        pytest.skip(f"the pinned UI font for {PLATFORM} isn't installed")
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    font, flash = app.font(), QApplication.cursorFlashTime()
    family, size = FONT[PLATFORM]
    app.setFont(QFont(family, size))
    QApplication.setCursorFlashTime(0)  # no blinking caret in line edits
    QCursor.setPos(QPoint(-5000, -5000))  # no hover highlight anywhere
    yield
    app.setFont(font)
    QApplication.setCursorFlashTime(flash)
    apply_theme(app, Theme.SYSTEM)
    for font_id in font_ids:
        QFontDatabase.removeApplicationFont(font_id)


def _font_files() -> list[Path]:
    folder = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    return [folder / name for name in FONT_FILES.get(PLATFORM, ()) if (folder / name).is_file()]


def settle(qtbot, window: MainWindow | None = None, rounds: int = 3) -> None:
    """Until no page tile or preview is pending and the event queue stays quiet."""

    def idle() -> bool:
        busy = False
        if window is not None:
            window.renderer.wait_idle(200)
            busy = bool(window.renderer._pending) or window.start_page.thumbnails_pending()
        return not busy

    for _ in range(rounds):
        qtbot.waitUntil(idle, timeout=10000)
        for _ in range(5):
            QApplication.processEvents()
        qtbot.wait(30)


def snapshot(widget: QWidget, ratio: int, crop: QRect | None = None) -> np.ndarray:
    """``widget`` (or its ``crop`` rect, in its own coordinates) drawn at ``ratio``, as RGB."""
    area = crop if crop is not None else widget.rect()
    image = QImage(area.size() * ratio, QImage.Format.Format_RGB32)
    image.setDevicePixelRatio(ratio)
    image.fill(QColor("#ff00ff"))  # anything left unpainted shows up loudly
    widget.render(image, QPoint(0, 0), area)
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    ptr = image.constBits()
    rows = np.frombuffer(ptr, dtype=np.uint8, count=image.sizeInBytes())
    rows = rows.reshape(image.height(), image.bytesPerLine())
    return rows[:, : image.width() * 3].reshape(image.height(), image.width(), 3).copy()


def check(request, name: str, theme: str, widget: QWidget, crop: QRect | None = None) -> None:
    """Compare ``widget`` at 1x and 2x against ``<platform>/<name>-<theme>@<ratio>x.png``."""
    update = request.config.getoption("--update-goldens")
    problems: list[str] = []
    for ratio in RATIOS:
        actual = snapshot(widget, ratio, crop)
        ref = GOLDEN / PLATFORM / f"{name}-{theme}@{ratio}x.png"
        if update:
            save_png(actual, ref)
            continue
        if not ref.exists():
            pytest.skip(
                f"no UI golden {ref.relative_to(GOLDEN)} for {PLATFORM}: take them with "
                "--update-goldens on this platform"
            )
        problem = compare(actual, ref, TOLERANCE, failures_dir())
        if problem:
            problems.append(problem)
    if update:
        pytest.skip(f"wrote UI goldens for {name}-{theme}")
    assert not problems, "\n".join(problems)


def around(widget: QWidget, window: QWidget, margin: int = 24) -> QRect:
    """``widget``'s rectangle in ``window`` coordinates, grown by ``margin``, kept inside."""
    top_left = widget.mapTo(window, QPoint(0, 0))
    rect = QRect(top_left, widget.size()).adjusted(-margin, -margin, margin, margin)
    return rect.intersected(window.rect())


@pytest.fixture
def make_window(qtbot, pinned) -> Iterator[Callable[[str], MainWindow]]:
    windows: list[MainWindow] = []

    def make(theme: str) -> MainWindow:
        prefs = AppSettings()
        prefs.theme = theme
        prefs.accent = ACCENT
        w = MainWindow()  # applies the saved theme and accent
        windows.append(w)
        w.resize(WINDOW)
        w.show()
        qtbot.waitExposed(w)
        apply_theme(QApplication.instance(), Theme(theme), ACCENT)  # type: ignore[arg-type]
        w.pill.idle_ms = 24 * 3600 * 1000  # the pill fades after 3 s: keep it shown
        settle(qtbot, w)
        return w

    yield make
    for w in windows:
        for view in w.views():
            view.session.undo_stack.set_clean()
        w.close()
        w.deleteLater()


def open_document(qtbot, window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("text_multipage"))
    assert view is not None
    window.pill.poke()
    settle(qtbot, window)


# -- scenes -----------------------------------------------------------------------------------
@pytest.mark.parametrize("theme", THEMES)
def test_start_page(qtbot, request, make_window, fixture_pdf, tmp_path: Path, theme: str) -> None:
    # Cards show the folder's name only, so a fixed folder name keeps them identical.
    folder = tmp_path / "Documents"
    folder.mkdir()
    files = []
    for name in ("report", "images", "outline"):
        files.append(folder / f"{name}.pdf")
        shutil.copy2(fixture_pdf(name), files[-1])
    settings = QSettings()
    settings.setValue("recent_files", [str(p) for p in [*files, folder / "moved away.pdf"]])
    settings.setValue("recent_pinned", [str(files[0])])
    window = make_window(theme)
    start = window.start_page
    qtbot.waitUntil(lambda: not start.thumbnails_pending(), timeout=10000)
    settle(qtbot, window)
    assert start.isVisible() and start.recent_view.count() == 4
    assert all(start.has_thumbnail(str(p)) for p in files)
    check(request, "start-page", theme, window)


@pytest.mark.parametrize("theme", THEMES)
def test_document_with_pages_panel(qtbot, request, make_window, fixture_pdf, theme: str) -> None:
    window = make_window(theme)
    open_document(qtbot, window, fixture_pdf)
    side = window.nav_panels
    assert side.current() is side.panels()[0] and side.content.isVisible()  # Pages
    assert window.pill.isVisible() and not window.pill.faded
    check(request, "document-pages-panel", theme, window)


@pytest.mark.parametrize("theme", THEMES)
def test_text_mini_toolbar(qtbot, request, make_window, fixture_pdf, theme: str) -> None:
    window = make_window(theme)
    open_document(qtbot, window, fixture_pdf)
    view = window.current_view()
    assert view is not None
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window)
    view.setFocus()
    view.set_selection(TextSelection(TextPos(0, 0), TextPos(0, 20)))
    settle(qtbot, window)
    bar = window.contextual.text_bar
    assert bar.isVisible()
    check(request, "text-mini-toolbar", theme, window, around(bar, window, 48))


@pytest.mark.parametrize("theme", THEMES)
def test_toast(qtbot, request, make_window, fixture_pdf, theme: str) -> None:
    window = make_window(theme)
    open_document(qtbot, window, fixture_pdf)
    toast = window.notify(
        "3 pages deleted", "info", ToastAction("Undo", lambda: None), timeout_ms=24 * 3600 * 1000
    )
    settle(qtbot, window)
    assert toast.isVisible()
    check(request, "toast", theme, window, around(toast, window))


@pytest.mark.parametrize("theme", THEMES)
def test_edit_mode_banner(qtbot, request, make_window, fixture_pdf, theme: str) -> None:
    window = make_window(theme)
    open_document(qtbot, window, fixture_pdf)
    window.tool_actions["edit"].trigger()
    settle(qtbot, window)
    banner = window.mode_banner
    assert banner.isVisible()
    view = window.current_view()
    assert view is not None
    # the banner row and the top of the framed page area below it
    top = banner.mapTo(window, QPoint(0, 0))
    crop = QRect(view.mapTo(window, QPoint(0, 0)), QSize(view.width(), banner.height() + 120))
    assert crop.top() <= top.y()
    check(request, "edit-mode-banner", theme, window, crop)


@pytest.mark.parametrize("theme", THEMES)
def test_form_dialog(qtbot, request, make_window, theme: str) -> None:
    window = make_window(theme)
    dialog = HeaderFooterDialog(12, 0, [0], parent=window)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    settle(qtbot, window)
    check(request, "header-footer-dialog", theme, dialog)
    dialog.close()
