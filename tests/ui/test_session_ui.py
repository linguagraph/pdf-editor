from __future__ import annotations

import json
import shutil
from pathlib import Path

import pikepdf
import pytest
from PySide6.QtWidgets import QMessageBox

from pdfeditor.core.commands import SetOutlineCommand, SnapshotCommand
from pdfeditor.core.paths import recovery_dir
from pdfeditor.engine.base import Document
from pdfeditor.model.outline import Destination, OutlineItem
from pdfeditor.ui.dialogs.preferences import PreferencesDialog
from pdfeditor.ui.dialogs.properties import PropertiesDialog
from pdfeditor.ui.dialogs.recovery import RecoveryDialog
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.panels.bookmarks import BookmarksPanel
from pdfeditor.ui.panels.thumbnails import ThumbnailsPanel

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    # Not registered with qtbot: pytest-qt would close it before this fixture could mark the
    # sessions clean, and a "Save changes?" box during teardown crashes the offscreen platform.
    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    qtbot.waitExposed(w)
    yield w
    for view in w.views():
        view.session.undo_stack.set_clean()  # don't prompt during teardown
    w.close()
    w.deleteLater()


@pytest.fixture
def pdf_copy(fixture_pdf, tmp_path: Path):
    def make(name: str) -> Path:
        dst = tmp_path / f"{name}.pdf"
        shutil.copy2(fixture_pdf(name), dst)
        return dst

    return make


def _edit_title(window: MainWindow, title: str) -> None:
    view = window.current_view()
    dialog = PropertiesDialog(view.session, window)
    dialog.fields["title"].setText(title)
    dialog.accept()


def test_edit_properties_marks_dirty_and_undoes(window: MainWindow, pdf_copy) -> None:
    view = window.open_path(pdf_copy("images"))
    assert not window.act_undo.isEnabled()
    _edit_title(window, "New Title")
    window._update_ui()
    assert view.session.is_dirty
    assert window.tabs.tabText(0) == "images.pdf" and window.tabs.is_dirty(0)
    assert window.windowTitle().startswith("images.pdf*")
    assert window.act_undo.isEnabled()
    assert window.act_undo.text() == "&Undo Change Document Properties"
    window.act_undo.trigger()
    assert view.session.document.metadata().title == "images"
    assert not view.session.is_dirty and not window.tabs.is_dirty(0)
    window.act_redo.trigger()
    assert view.session.document.metadata().title == "New Title"
    # an unchanged dialog doesn't add an undo step
    depth = len(view.session.undo_stack)
    PropertiesDialog(view.session, window).accept()
    assert len(view.session.undo_stack) == depth


def test_save_and_save_as(window: MainWindow, pdf_copy, tmp_path: Path, monkeypatch) -> None:
    path = pdf_copy("images")
    view = window.open_path(path)
    _edit_title(window, "Saved Title")
    window.act_save.trigger()
    assert not view.session.is_dirty and not window.tabs.is_dirty(0)
    with pikepdf.open(path) as pdf:
        assert str(pdf.docinfo["/Title"]) == "Saved Title"
    other = tmp_path / "renamed"
    monkeypatch.setattr(window, "choose_save_path", lambda start: other.with_suffix(".pdf"))
    _edit_title(window, "Second")
    assert window.save_as()
    assert view.session.path == other.with_suffix(".pdf").resolve()
    assert window.tabs.tabText(0) == "renamed.pdf"
    monkeypatch.setattr(window, "choose_save_path", lambda start: None)
    assert not window.save_as()  # cancelled


def test_save_failure_is_reported(window: MainWindow, pdf_copy, monkeypatch) -> None:
    from pdfeditor.engine.base import SaveError

    view = window.open_path(pdf_copy("images"))
    _edit_title(window, "x")
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))

    def fail(path=None):
        raise SaveError("disk full")

    monkeypatch.setattr(view.session, "save", fail)
    assert not window.save()
    assert "disk full" in warnings[0] and view.session.is_dirty


def test_close_prompts(window: MainWindow, pdf_copy, monkeypatch) -> None:
    path = pdf_copy("images")
    window.open_path(path)
    _edit_title(window, "Changed")
    answers = [QMessageBox.StandardButton.Cancel]
    monkeypatch.setattr(window, "ask_save_changes", lambda s: answers.pop(0))
    assert not window.close_tab(0)
    assert window.tabs.count() == 1
    answers.append(QMessageBox.StandardButton.Save)
    assert window.close_tab(0)
    with pikepdf.open(path) as pdf:
        assert str(pdf.docinfo["/Title"]) == "Changed"
    window.open_path(path)
    _edit_title(window, "Thrown away")
    answers.append(QMessageBox.StandardButton.Discard)
    assert window.close_tab(0)
    with pikepdf.open(path) as pdf:
        assert str(pdf.docinfo["/Title"]) == "Changed"


def test_window_close_can_be_cancelled(window: MainWindow, pdf_copy, monkeypatch) -> None:
    window.open_path(pdf_copy("images"))
    _edit_title(window, "Changed")
    monkeypatch.setattr(window, "ask_save_changes", lambda s: QMessageBox.StandardButton.Cancel)
    assert not window.close()
    assert window.tabs.count() == 1


def test_structure_change_reloads_view_and_panels(window: MainWindow, pdf_copy) -> None:
    view = window.open_path(pdf_copy("outline"))
    thumbs = next(p for p in window.panels if isinstance(p, ThumbnailsPanel))
    bookmarks = next(p for p in window.panels if isinstance(p, BookmarksPanel))
    assert thumbs.model.rowCount() == 6

    def delete_last(doc: Document) -> None:
        doc.fz.delete_page(doc.page_count - 1)  # test-only: no page-ops API until Phase 6
        doc.structure_changed()

    session = view.session
    session.execute(SnapshotCommand("Delete Page", delete_last, session.snapshots))
    assert view.page_count == 5 and thumbs.model.rowCount() == 5
    session.undo()
    assert view.page_count == 6 and thumbs.model.rowCount() == 6

    session.execute(SetOutlineCommand([OutlineItem("Solo", dest=Destination(0))]))
    assert bookmarks.tree.topLevelItemCount() == 1
    session.undo()
    assert bookmarks.tree.topLevelItemCount() == 3


def test_autosave_and_recovery(qtbot, window: MainWindow, pdf_copy) -> None:
    path = pdf_copy("images")
    window.open_path(path)
    _edit_title(window, "Recover me")
    assert window.autosave_now() == 1
    (meta_file,) = recovery_dir().glob("*.json")
    # Simulate a crash: the owning process is gone, the window never saved.
    info = json.loads(meta_file.read_text())
    info["pid"] = 999999999
    meta_file.write_text(json.dumps(info))
    uid = meta_file.stem
    # A crashed process no longer holds the file open (on Windows an open file can't be
    # replaced), so release the document without the normal close path deleting the copy.
    window.current_view().session.close()

    fresh = MainWindow()
    dialog = RecoveryDialog(fresh.recovery.entries(), fresh)
    dialog._recover()
    assert fresh.offer_recovery(dialog) == 1
    view = fresh.current_view()
    assert view.session.is_dirty and view.session.path is None
    assert view.session.document.metadata().title == "Recover me"
    assert view.session.save_target() == path.resolve()
    assert fresh.tabs.tabText(0) == "images.pdf" and fresh.tabs.is_dirty(0)
    assert not (recovery_dir() / f"{uid}.pdf").exists()
    assert fresh.save()
    with pikepdf.open(path) as pdf:
        assert str(pdf.docinfo["/Title"]) == "Recover me"
    fresh.close()
    fresh.deleteLater()


def test_saving_removes_recovery_copy(window: MainWindow, pdf_copy) -> None:
    view = window.open_path(pdf_copy("images"))
    _edit_title(window, "x")
    window.autosave_now()
    pdf = recovery_dir() / f"{view.session.uid}.pdf"
    assert pdf.exists()
    window.save()
    assert not pdf.exists()


def test_preferences_apply(qtbot, window: MainWindow, pdf_copy) -> None:
    dialog = PreferencesDialog(window.prefs, window)
    dialog.author.setText("Reviewer")
    dialog.zoom.setCurrentIndex(dialog.zoom.findData("100"))
    dialog.autosave.setValue(0)
    dialog.accept()
    window._apply_prefs()
    assert window.prefs.author == "Reviewer" and window.prefs.default_zoom == "100"
    assert not window.autosave_timer.isActive()
    view = window.open_path(pdf_copy("images"))
    assert view.zoom == pytest.approx(1.0)
