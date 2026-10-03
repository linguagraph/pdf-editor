from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtGui import QImage

from pdfeditor.core.render_cache import RenderCache
from pdfeditor.core.session import DocumentSession
from pdfeditor.services.fonts import FontCatalog
from pdfeditor.ui.view.document_view import DocumentView
from pdfeditor.ui.view.renderer import TileRenderer


@pytest.fixture(autouse=True)
def font_catalog(monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path) -> FontCatalog:
    """Point the font picker at the synthetic fonts (never scan real system fonts, and never
    start the lazy background scan, which would otherwise fire once per test session)."""
    catalog = FontCatalog()
    catalog.scan([fixtures_dir / "fonts"])
    monkeypatch.setattr("pdfeditor.ui.font_picker.cached_catalog", lambda: catalog)
    monkeypatch.setattr("pdfeditor.ui.view.text_editor.cached_catalog", lambda: catalog)
    monkeypatch.setattr("pdfeditor.ui.dialogs.properties.cached_catalog", lambda: catalog)
    return catalog


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path) -> Iterator[None]:
    """Keep QSettings (recent files, window state) out of the real user profile."""
    from PySide6.QtCore import QCoreApplication, QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    QCoreApplication.setOrganizationName("pdfeditor-tests")
    QCoreApplication.setApplicationName("pdfeditor-tests")
    yield


@pytest.fixture(autouse=True)
def no_animations() -> Iterator[None]:
    """Zoom and page jumps complete synchronously unless a test turns animations on."""
    from pdfeditor.ui.view import motion

    motion.set_animations_enabled(False)
    yield
    motion.set_animations_enabled(None)


@pytest.fixture
def renderer(qtbot) -> Iterator[TileRenderer]:
    r = TileRenderer(RenderCache[QImage](64 * 1024 * 1024))
    yield r
    r.wait_idle()


@pytest.fixture
def make_view(qtbot, renderer: TileRenderer, fixture_pdf) -> Iterator[Callable[..., DocumentView]]:
    sessions: list[DocumentSession] = []

    def make(name: str, width: int = 800, height: int = 600) -> DocumentView:
        session = DocumentSession.open(fixture_pdf(name))
        sessions.append(session)
        view = DocumentView(session, renderer)
        qtbot.addWidget(view)
        view.resize(width, height)
        view.show()
        qtbot.waitExposed(view)
        return view

    yield make
    renderer.wait_idle()
    for s in sessions:
        s.close()


def wait_rendered(qtbot, view: DocumentView, timeout: int = 5000) -> QImage:
    """Wait until the render queue is empty and return a grab of the viewport."""

    def done() -> None:
        view.renderer.wait_idle(100)
        qtbot.wait(20)
        assert not view.renderer._pending

    qtbot.waitUntil(done, timeout=timeout)
    qtbot.wait(50)
    return view.viewport().grab().toImage()


@pytest.fixture(autouse=True)
def _reset_font_picker_state() -> Iterator[None]:
    """The picker's cross-process "only scan once" bookkeeping must not leak between tests."""
    from pdfeditor.ui import font_picker

    font_picker._open_pickers.clear()
    font_picker._scan_started = False
    font_picker._scan_job = None
    yield
    font_picker._open_pickers.clear()


@pytest.fixture(autouse=True)
def no_blocking_message_boxes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record warnings/errors instead of opening them: offscreen, a modal message box (say,
    from an edit committed while a failed test tears down) would block the whole run. Tests
    that check a message stub these themselves, which overrides this."""
    from PySide6.QtWidgets import QMessageBox

    shown: list[str] = []

    def record(*args: object, **_kwargs: object) -> QMessageBox.StandardButton:
        shown.append(str(args[2]) if len(args) > 2 else "")
        return QMessageBox.StandardButton.Ok

    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(record))
    return shown
