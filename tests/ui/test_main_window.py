from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QInputDialog, QMessageBox

from pdfeditor.core.layout import LayoutMode
from pdfeditor.ui.dialogs.properties import PropertiesDialog
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.panels.attachments import AttachmentsPanel, human_size
from pdfeditor.ui.panels.bookmarks import BookmarksPanel
from pdfeditor.ui.panels.layers import LayersPanel
from pdfeditor.ui.panels.thumbnails import ThumbnailsPanel
from pdfeditor.ui.single_instance import InstanceServer, send_to_running_instance
from tests.ui.conftest import wait_rendered

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    w.resize(1100, 800)
    w.show()
    qtbot.waitExposed(w)
    yield w
    w.close()


def panel(window: MainWindow, cls):
    return next(p for p in window.panels if isinstance(p, cls))


def test_starts_empty(window: MainWindow) -> None:
    assert window.current_view() is None
    assert window.welcome.isVisible()
    assert not window.act_zoom_in.isEnabled()
    assert window.act_open.isEnabled()
    assert window.ribbon.tab_names() == [
        "Home",
        "Edit",
        "Organize",
        "Protect",
        "Comment",
        "View",
        "Tools",
    ]


def test_open_and_close_tabs(window: MainWindow, fixture_pdf) -> None:
    v1 = window.open_path(fixture_pdf("text_multipage"))
    v2 = window.open_path(fixture_pdf("outline"))
    assert v1 is not None and v2 is not None
    assert window.tabs.count() == 2 and window.current_view() is v2
    assert window.windowTitle().startswith("outline.pdf")
    # re-opening activates the existing tab
    assert window.open_path(fixture_pdf("text_multipage")) is v1
    assert window.tabs.count() == 2 and window.current_view() is v1
    assert window.act_zoom_in.isEnabled()
    window.close_current()
    window.close_current()
    assert window.tabs.count() == 0 and window.welcome.isVisible()
    assert v1.session.closed and v2.session.closed


def test_recent_files(window: MainWindow, fixture_pdf, tmp_path: Path, monkeypatch) -> None:
    window.open_path(fixture_pdf("images"))
    window.open_path(fixture_pdf("outline"))
    recent = window._recent_files()
    assert Path(recent[0]).name == "outline.pdf" and Path(recent[1]).name == "images.pdf"
    window._fill_recent_menu()
    labels = [a.text() for a in window.recent_menu.actions()]
    assert any("outline.pdf" in t for t in labels)
    # a file that disappeared is dropped from the list when opening fails
    gone = tmp_path / "gone.pdf"
    shutil.copy(fixture_pdf("images"), gone)
    window.open_path(gone)
    window.close_current()
    gone.unlink()
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    )
    assert window.open_path(gone) is None
    assert str(gone.resolve()) not in window._recent_files()


def test_password_prompt_flow(window: MainWindow, fixture_pdf, monkeypatch) -> None:
    answers = iter([("wrong", True), ("user", True)])
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: next(answers)))
    view = window.open_path(fixture_pdf("encrypted"))
    assert view is not None and view.page_count == 1
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))
    window.close_current()
    assert window.open_path(fixture_pdf("encrypted")) is None  # cancelled: no error dialog


def test_navigator_and_zoom_box(qtbot, window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("outline"))
    assert view is not None
    nav = window.navigator
    assert nav.edit.text() == "i" and nav.total.text() == "(1 of 6)"
    nav.edit.setText("iii")
    nav.edit.returnPressed.emit()
    assert view.current_page == 2
    assert nav.edit.text() == "iii"
    window.zoom_box._apply("200%")
    assert view.zoom == pytest.approx(2.0)
    assert window.zoom_box.currentText() == "200%"
    window.zoom_box._apply("Fit Page")
    assert view.fit_mode.value == "page"


def test_layout_and_night_actions(window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("text_multipage"))
    assert view is not None
    window.layout_actions[LayoutMode.TWO_UP].trigger()
    assert view.layout_mode is LayoutMode.TWO_UP
    window.act_night.trigger()
    assert view.night_mode
    window.act_rotate_cw.trigger()
    assert view.rotation == 90


def test_panels_follow_current_tab(qtbot, window: MainWindow, fixture_pdf) -> None:
    window.open_path(fixture_pdf("outline"))
    bookmarks = panel(window, BookmarksPanel)
    assert bookmarks.tree.topLevelItemCount() == 3
    item = bookmarks.tree.topLevelItem(2)
    bookmarks._activate(item)
    assert window.current_view().current_page == 4

    window.open_path(fixture_pdf("images"))
    assert bookmarks.tree.topLevelItemCount() == 0 and bookmarks.empty.isVisibleTo(bookmarks)
    attachments = panel(window, AttachmentsPanel)
    assert attachments.tree.topLevelItemCount() == 1
    assert attachments.tree.topLevelItem(0).text(0) == "notes.txt"


def test_attachment_save(window: MainWindow, fixture_pdf, tmp_path: Path) -> None:
    window.open_path(fixture_pdf("images"))
    attachments = panel(window, AttachmentsPanel)
    out = attachments.save_selected(tmp_path / "saved.txt")
    assert out is not None and out.read_bytes() == b"attached notes\n"
    assert human_size(1536) == "1.5 KB" and human_size(10) == "10 B"


def test_layers_panel_toggles(qtbot, window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("layers"))
    layers = panel(window, LayersPanel)
    assert layers.list.count() == 2
    hidden = layers.list.item(1)
    assert hidden.checkState() == Qt.CheckState.Unchecked
    hidden.setCheckState(Qt.CheckState.Checked)
    with view.session.lock:
        assert all(layer.visible for layer in view.session.document.layers())


def test_thumbnails_render_and_navigate(qtbot, window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("text_multipage"))
    thumbs = panel(window, ThumbnailsPanel)
    assert thumbs.model is not None and thumbs.model.rowCount() == 5
    thumbs.model.data(thumbs.model.index(1), Qt.ItemDataRole.DecorationRole)  # triggers render
    wait_rendered(qtbot, view)
    page = thumbs.model.data(thumbs.model.index(1), Qt.ItemDataRole.DecorationRole)
    assert page.availableSizes()[0].width() >= 150
    thumbs._on_clicked(thumbs.model.index(3))
    assert view.current_page == 3
    assert thumbs.list.currentIndex().row() == 3


def test_properties_dialog(window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("subset_fonts"))
    dialog = PropertiesDialog(view.session, window)
    assert dialog.tabs.count() == 3
    assert dialog.fonts_table.rowCount() >= 1
    assert "Embedded subset" in {
        dialog.fonts_table.item(r, 3).text() for r in range(dialog.fonts_table.rowCount())
    }
    dialog.close()


def test_drop_opens_pdf(window: MainWindow, fixture_pdf) -> None:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(fixture_pdf("images")))])
    event = QDropEvent(
        QPoint(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(event)
    assert window.tabs.count() == 1


def test_uri_link_asks_before_opening(window: MainWindow, fixture_pdf, monkeypatch) -> None:
    from pdfeditor.model.geometry import Rect
    from pdfeditor.model.outline import Link, LinkKind

    opened: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    )
    monkeypatch.setattr(
        "pdfeditor.ui.main_window.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()),
    )
    link = Link(Rect(0, 0, 1, 1), LinkKind.URI, uri="https://example.org/")
    window.open_external_link(link)
    assert opened == []
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    window.open_external_link(link)
    assert opened == ["https://example.org/"]


def test_single_instance_handoff(qtbot) -> None:
    """A second process forwards its files to the running instance."""
    name = f"pdfeditor-test-{os.getpid()}"
    server = InstanceServer(name)
    assert server.listen()
    code = (
        "import sys; from PySide6.QtCore import QCoreApplication; "
        "from pdfeditor.ui.single_instance import send_to_running_instance as send; "
        "app = QCoreApplication([]); "
        f"sys.exit(0 if send(['C:/a.pdf', 'C:/b c.pdf'], {name!r}, 3000) else 3)"
    )
    with qtbot.waitSignal(server.files_received, timeout=15000) as blocker:
        proc = subprocess.Popen([sys.executable, "-c", code])
    assert blocker.args[0] == ["C:/a.pdf", "C:/b c.pdf"]
    qtbot.waitUntil(lambda: proc.poll() is not None, timeout=15000)
    assert proc.returncode == 0
    server.close()
    assert not send_to_running_instance(["x"], name, timeout_ms=100)
