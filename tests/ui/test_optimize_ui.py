from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pdfeditor.ui.dialogs.optimize import ReduceSizeDialog, SpaceAuditDialog, human_size
from pdfeditor.ui.main_window import MainWindow

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    w.resize(1000, 800)
    w.show()
    qtbot.waitExposed(w)
    yield w
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()


@pytest.fixture
def view(window: MainWindow, fixture_pdf, tmp_path: Path):
    path = tmp_path / "heavy.pdf"
    shutil.copy2(fixture_pdf("heavy"), path)
    return window.open_path(path)


def test_human_size() -> None:
    assert human_size(512) == "512 bytes"
    assert human_size(1536) == "1.5 KB"
    assert human_size(7 * 1024 * 1024) == "7.0 MB"


def test_presets_and_custom(window: MainWindow, view) -> None:
    d = ReduceSizeDialog(view.session.path, 1000, window)
    assert d.preset.currentData() == "ebook" and d.dpi.value() == 150
    d.preset.setCurrentIndex(0)
    assert d.dpi.value() == 72 and d.quality.value() == 50
    d.quality.setValue(40)
    assert d.preset.currentData() == "custom"
    assert d.options().optimize.jpeg_quality == 40
    d.preset.setCurrentIndex(3)  # lossless
    assert d.keep_images.isChecked() and not d.dpi.isEnabled()
    assert d.options().optimize.image_dpi is None
    d.close()


def test_reduce_saves_a_copy(window: MainWindow, view, tmp_path: Path) -> None:
    original = view.session.path.stat().st_size
    d = ReduceSizeDialog(view.session.path, original, window)
    d.target.setText(str(tmp_path / "small.pdf"))
    d.accept()
    target = window.optimize.reduce(d)
    assert target == tmp_path / "small.pdf"
    assert target.stat().st_size < original / 20
    assert "→" in window.optimize.last_message
    assert view.session.path.stat().st_size == original  # the source is untouched
    assert not view.session.is_dirty
    reopened = window.open_path(target)
    assert reopened is not None and reopened.page_count == 1


def test_estimate_is_reused(window: MainWindow, view, tmp_path: Path, monkeypatch) -> None:
    d = ReduceSizeDialog(view.session.path, None, window)
    d.target.setText(str(tmp_path / "x.pdf"))
    result = window.optimize._reduce(view.session, d.options())
    d.show_estimate(result)
    assert "smaller" in d.estimate_label.text()
    calls: list[int] = []
    monkeypatch.setattr(window.optimize, "_reduce", lambda *a: calls.append(1))
    d.accept()
    assert window.optimize.reduce(d) == tmp_path / "x.pdf" and calls == []
    d.grayscale.setChecked(True)  # changing settings drops the estimate
    assert d.estimate is None


def test_refuses_to_overwrite_open_file(window: MainWindow, view, monkeypatch) -> None:
    warned: list[str] = []
    monkeypatch.setattr(
        "pdfeditor.ui.optimize_controller.QMessageBox.warning",
        lambda _p, _t, msg: warned.append(msg),
    )
    d = ReduceSizeDialog(view.session.path, None, window)
    d.target.setText(str(view.session.path))
    d.accept()
    assert window.optimize.reduce(d) is None and "different file" in warned[0]


def test_audit(window: MainWindow, view) -> None:
    usage = window.optimize.audit(show=False)
    assert usage is not None and usage.share("Images") > 0.5
    dialog = SpaceAuditDialog(usage, window)
    assert dialog.table.item(0, 0).text() == "Images"
    dialog.close()
