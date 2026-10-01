"""Regressions for dialog issues: color picker look (#37), collapsible sections that moved the
dialog around (#38) and the OCR language download progress (#41)."""

from __future__ import annotations

import threading
import urllib.error
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QRect, QTimer
from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtWidgets import QApplication, QColorDialog, QPushButton, QWidget

from pdfeditor.model.color import Color
from pdfeditor.services import ocr as ocr_service
from pdfeditor.ui import color_picker
from pdfeditor.ui.color_picker import ColorButton, color_dialog, pick_color
from pdfeditor.ui.dialogs import compare, ocr, optimize, pages, redaction
from pdfeditor.ui.dialogs.base import FormDialog, Section
from pdfeditor.ui.theme import Theme, apply_theme, current_colors

pytestmark = pytest.mark.gui

SRC = Path(__file__).resolve().parents[2] / "src" / "pdfeditor"


@pytest.fixture(params=[Theme.LIGHT, Theme.DARK], ids=["light", "dark"])
def theme(request, qapp: QApplication) -> Iterator[Theme]:
    apply_theme(qapp, request.param)
    yield request.param
    apply_theme(qapp, Theme.LIGHT)


def y_of(widget: QWidget, top: QWidget) -> int:
    return widget.mapTo(top, QPoint(0, 0)).y()


def x_of(widget: QWidget, top: QWidget) -> int:
    return widget.mapTo(top, QPoint(0, 0)).x()


def grab_in(top: QWidget, widget: QWidget) -> QImage:
    """What ``widget`` looks like on screen, background included."""
    return top.grab(QRect(widget.mapTo(top, QPoint(0, 0)), widget.size())).toImage()


def close(a: QColor, b: QColor, tolerance: int = 24) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a.getRgb()[:3], b.getRgb()[:3], strict=True))


# -- #37 color picker ---------------------------------------------------------------------------
def test_every_color_dialog_goes_through_the_helper() -> None:
    users = [
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if "QColorDialog" in p.read_text(encoding="utf-8")
    ]
    assert users == ["ui/color_picker.py"]


def test_color_dialog_takes_the_app_theme_not_the_opener(qtbot, theme: Theme) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    background = pages.BackgroundDialog(3, 0, [0])
    qtbot.addWidget(background)
    background.show()
    # the opener used to paint itself with a style sheet the dialog inherited (#37)
    assert background.color_button.styleSheet() == ""
    # a widget with its own palette, like the inline text editor's pale page color
    pale = QWidget(background)
    palette = pale.palette()
    palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 240))
    palette.setColor(QPalette.ColorRole.Window, QColor(255, 255, 240))
    pale.setPalette(palette)
    dialog = color_dialog(Color(1, 1, 0.9), pale, "Background Color")
    qtbot.addWidget(dialog)
    assert dialog.parentWidget() is background  # the window, not the widget that asked
    assert dialog.windowTitle() == "Background Color"
    assert dialog.testOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
        assert dialog.palette().color(role) == app.palette().color(role), role
    assert dialog.styleSheet() == ""
    dialog.show()
    qtbot.waitExposed(dialog)
    ok = [b for b in dialog.findChildren(QPushButton) if b.isDefault()]
    assert len(ok) == 1
    image = ok[0].grab().toImage()
    fill = image.pixelColor(6, image.height() // 2)
    assert close(fill, QColor(current_colors().accent)), fill.name()  # accent, not pale yellow
    window = dialog.grab().toImage().pixelColor(2, 2)
    assert close(window, QColor(current_colors().window)), window.name()
    dialog.reject()


def test_pick_color_returns_the_choice_or_none(qtbot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()

    def answer(accept: bool) -> None:
        modal = QApplication.activeModalWidget()
        assert isinstance(modal, QColorDialog)
        modal.setCurrentColor(QColor("#336699"))
        modal.accept() if accept else modal.reject()

    QTimer.singleShot(0, lambda: answer(True))
    chosen = pick_color("#ff0000", parent, "Text Color")
    assert chosen is not None and chosen.to_hex() == "#336699"
    QTimer.singleShot(0, lambda: answer(False))
    assert pick_color(Color(1, 0, 0), parent) is None


def test_color_buttons_show_a_swatch_and_use_the_helper(qtbot, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_pick(initial: Color, parent: QWidget, title: str) -> Color:
        calls.append((initial.to_hex(), title))
        return Color(0, 0.5, 0)

    monkeypatch.setattr(color_picker, "pick_color", fake_pick)
    dialog = redaction.RedactionPropertiesDialog(redaction.MarkStyle())
    qtbot.addWidget(dialog)
    button = dialog.fill
    assert isinstance(button, ColorButton) and button.styleSheet() == ""
    assert not button.icon().isNull() and button.toolTip() == button.color.to_hex()
    before = button.color.to_hex()
    button.click()
    assert calls == [(before, color_picker.DEFAULT_TITLE)]
    assert button.color == Color(0, 0.5, 0) and button.toolTip() == "#008000"


# -- #38 sections -------------------------------------------------------------------------------
def above_and_below(dialog: FormDialog, section: Section) -> tuple[list[QWidget], list[QWidget]]:
    """Visible content widgets above and below ``section``."""
    top = y_of(section, dialog)
    widgets = [
        w
        for w in dialog.findChildren(QWidget)
        if w.isVisible()
        and w.window() is dialog
        and not section.isAncestorOf(w)
        and w is not section
    ]
    above = [w for w in widgets if y_of(w, dialog) + w.height() <= top]
    below = [w for w in widgets if y_of(w, dialog) > top]
    return above, below


def test_toggling_a_section_moves_only_what_is_below_it(qtbot, theme: Theme) -> None:
    d = pages.HeaderFooterDialog(16, 0, [0], bates=False)
    qtbot.addWidget(d)
    d.show()
    qtbot.waitExposed(d)
    section = d.style_section
    assert not section.is_expanded()
    above, below = above_and_below(d, section)
    assert d.header_title in above and d.range in below
    tops = {w: y_of(w, d) for w in above}
    range_y, header_y, height = y_of(d.range, d), y_of(section.header, d), d.height()
    header_look = grab_in(d, section.header)

    section.set_expanded(True)
    qtbot.wait(10)
    grown = section.content.height()
    assert grown > 0
    assert {w: y_of(w, d) for w in above} == tops  # nothing above moved
    assert y_of(section.header, d) == header_y
    assert y_of(d.range, d) - range_y == d.height() - height > 0  # grew by the content
    # the header looks the same open or closed (only the chevron turns): same background
    open_look = grab_in(d, section.header)
    w = header_look.width()
    for x in (w - 3, w // 2, w - 30):
        assert open_look.pixelColor(x, 3) == header_look.pixelColor(x, 3)

    section.set_expanded(False)
    qtbot.wait(10)
    assert {w: y_of(w, d) for w in above} == tops
    assert y_of(d.range, d) == range_y and d.height() == height


def test_spare_height_goes_below_the_content(qtbot) -> None:
    d = pages.HeaderFooterDialog(16, 0, [0], bates=False)
    qtbot.addWidget(d)
    d.show()
    qtbot.waitExposed(d)
    positions = [y_of(w, d) for w in (d.style_section, d.bates_section, d.range)]
    d.resize(d.width(), d.height() + 200)  # the user makes the dialog taller
    qtbot.wait(10)
    assert [y_of(w, d) for w in (d.style_section, d.bates_section, d.range)] == positions
    assert d.button_box.geometry().bottom() > d.range.geometry().bottom() + 200
    height, range_y = d.height(), y_of(d.range, d)
    d.style_section.set_expanded(True)  # opening keeps the user's extra room
    qtbot.wait(10)
    assert d.height() - height == y_of(d.range, d) - range_y > 0
    d.style_section.set_expanded(False)
    qtbot.wait(10)
    assert d.height() == height


def test_headers_group_boxes_and_labels_share_a_left_edge(qtbot) -> None:
    d = pages.HeaderFooterDialog(16, 0, [0], bates=True)
    qtbot.addWidget(d)
    d.show()
    qtbot.waitExposed(d)
    edge = x_of(d.header_title, d)
    assert x_of(d.style_section.header, d) == edge
    assert x_of(d.bates_section.header, d) == edge
    assert x_of(d.range, d) == edge
    # the section's chevron sits at the edge too (not padded in by the icon box)
    image = grab_in(d, d.style_section.header)
    background = image.pixelColor(image.width() - 2, image.height() // 2)
    first_ink = next(
        x
        for x in range(image.width())
        if any(not close(image.pixelColor(x, y), background, 40) for y in range(image.height()))
    )
    assert first_ink <= 5  # was 12 (#38)


@pytest.mark.parametrize(
    "make",
    [
        lambda: pages.HeaderFooterDialog(4, 0, [0], bates=True),
        lambda: pages.WatermarkDialog(4, 0, [0]),
        lambda: optimize.ReduceSizeDialog(Path("x.pdf"), 1000),
        lambda: ocr.OcrDialog(4, 0, []),
        lambda: compare.CompareFilesDialog(Path("x.pdf")),
        lambda: redaction.MarkTextDialog(),
        lambda: redaction.ApplyRedactionsDialog(2, 1),
    ],
    ids=["header", "watermark", "reduce", "ocr", "compare", "mark text", "apply redactions"],
)
def test_every_section_keeps_what_is_above_it(qtbot, make) -> None:
    d = make()
    qtbot.addWidget(d)
    check_sections(qtbot, d)


def test_print_dialog_sections_keep_what_is_above_them(qtbot, fixture_pdf) -> None:
    from pdfeditor.ui.dialogs.print_dialog import PrintDialog
    from pdfeditor.ui.main_window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    view = window.open_path(fixture_pdf("report"))
    d = PrintDialog(view, window)
    check_sections(qtbot, d)
    d.close()
    window.close()


def check_sections(qtbot, d: FormDialog) -> None:
    d.show()
    qtbot.waitExposed(d)
    sections = d.findChildren(Section)
    assert sections
    for section in sections:
        for _ in range(2):
            above, _below = above_and_below(d, section)
            tops = {w: y_of(w, d) for w in above}
            height = d.height()
            section.set_expanded(not section.is_expanded())
            qtbot.wait(5)
            assert {w: y_of(w, d) for w in above} == tops, section.header.text()
            assert (d.height() > height) == section.is_expanded(), section.header.text()


# -- #41 OCR language download -----------------------------------------------------------------
class FakeResponse:
    """A urlopen() result whose chunks are released one by one by the test."""

    def __init__(self, chunks: list[bytes], total: int | None, gate: threading.Semaphore) -> None:
        self.chunks = list(chunks)
        self.headers = {"Content-Length": str(total)} if total is not None else {}
        self.gate = gate

    def read(self, _size: int) -> bytes:
        assert self.gate.acquire(timeout=10)
        return self.chunks.pop(0) if self.chunks else b""

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        pass


@pytest.fixture
def tessdata(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("PDFEDITOR_DATA_DIR", str(tmp_path))
    return tmp_path / "tessdata"


def fake_network(monkeypatch, total: int | None, chunks: int = 4) -> threading.Semaphore:
    gate = threading.Semaphore(0)
    body = [bytes([i]) * 1000 for i in range(chunks)]
    monkeypatch.setattr(
        ocr_service.urllib.request,
        "urlopen",
        lambda url, timeout=0: FakeResponse(body, total, gate),
    )
    return gate


def start(qtbot, code: str = "bul") -> ocr.OcrDialog:
    d = ocr.OcrDialog(4, 0, [])
    qtbot.addWidget(d)
    d.show()
    box = d.box
    box.download_combo.setCurrentIndex(box.download_combo.findData(code))
    assert not box.download_panel.isVisible()
    box.download_button.click()
    return d


def test_download_shows_bytes_percent_and_the_language(qtbot, monkeypatch, tessdata) -> None:
    gate = fake_network(monkeypatch, total=4000)
    d = start(qtbot)
    box = d.box
    assert box.is_downloading() and box.download_panel.isVisible()
    assert not box.download_button.isEnabled() and box.download_cancel.isVisible()
    gate.release(2)
    qtbot.waitUntil(lambda: box.download_bar.value() == 2000)
    assert box.download_bar.maximum() == 4000 and box.download_bar.text() == "50%"
    assert box.download_status.text() == "Downloading Bulgarian (bul): 2 KB of 4 KB"
    gate.release(3)  # the rest and the end of the stream
    qtbot.waitUntil(lambda: not box.is_downloading())
    assert (tessdata / "bul.traineddata").stat().st_size == 4000
    assert not list(tessdata.glob("*.part"))
    assert "bul" in box.languages.languages()  # installed and checked
    assert box.download_status.text() == "Bulgarian (bul) is installed and selected."
    assert not box.download_bar.isVisible() and box.download_button.isEnabled()


def test_download_without_size_counts_bytes(qtbot, monkeypatch, tessdata) -> None:
    gate = fake_network(monkeypatch, total=None)
    d = start(qtbot)
    box = d.box
    gate.release(1)
    qtbot.waitUntil(lambda: "received" in box.download_status.text())
    assert box.download_bar.maximum() == 0  # busy bar: no total to show
    assert box.download_status.text() == "Downloading Bulgarian (bul): 1 KB received"
    gate.release(10)
    qtbot.waitUntil(lambda: not box.is_downloading())
    assert (tessdata / "bul.traineddata").exists()


def test_download_can_be_cancelled(qtbot, monkeypatch, tessdata) -> None:
    gate = fake_network(monkeypatch, total=4000)
    d = start(qtbot)
    box = d.box
    gate.release(1)
    qtbot.waitUntil(lambda: box.download_bar.value() == 1000)
    box.download_cancel.click()
    assert box.download_status.text().startswith("Cancelling")
    gate.release(10)
    qtbot.waitUntil(lambda: not box.is_downloading())
    assert box.download_status.text() == "Download cancelled."
    assert not list(tessdata.glob("bul.*"))  # no partial file left behind
    assert box.download_button.isEnabled()


def test_closing_the_dialog_stops_the_download(qtbot, monkeypatch, tessdata) -> None:
    gate = fake_network(monkeypatch, total=4000)
    d = start(qtbot)
    job = d.box.download_job
    assert job is not None
    d.reject()
    assert job.token.cancelled and not d.box.is_downloading()
    gate.release(10)
    qtbot.waitUntil(lambda: job.done)
    assert not list(tessdata.glob("bul.*"))


def test_download_errors_are_shown_inline(qtbot, monkeypatch, tessdata) -> None:
    def offline(url: str, timeout: float = 0) -> FakeResponse:
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(ocr_service.urllib.request, "urlopen", offline)
    d = start(qtbot)
    box = d.box
    qtbot.waitUntil(lambda: not box.is_downloading())
    assert box.download_status.property("role") == "error"
    assert box.download_status.text().startswith("Couldn't download Bulgarian (bul):")
    assert "no route to host" in box.download_status.text()
    assert box.download_button.isEnabled() and not box.download_bar.isVisible()
    assert d.isVisible()  # no separate message box; the dialog stays as it was


def test_download_service_reports_progress(monkeypatch, tessdata) -> None:
    gate = fake_network(monkeypatch, total=3000, chunks=3)
    gate.release(4)
    seen: list[tuple[int, int]] = []
    path = ocr_service.download_language("xyz", progress=lambda d, t: seen.append((d, t)))
    assert seen == [(0, 3000), (1000, 3000), (2000, 3000), (3000, 3000)]
    assert path == tessdata / "xyz.traineddata" and path.stat().st_size == 3000
    assert ocr_service.language_name("bul") == "Bulgarian (bul)"
    assert ocr_service.language_name("xyz") == "xyz"
