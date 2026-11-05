"""Request Signatures in the window: drag a box per signer, then Finish writes the file and offers
an email draft. Nothing is sent, and the email app is never started here."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.signing import EmptySignatureField, SignatureOptions, SignaturePlacement
from pdfeditor.services.signing import preflight
from pdfeditor.ui import sign_controller
from pdfeditor.ui.dialogs.request_signature import RequestSignerDialog, valid_email
from pdfeditor.ui.dialogs.sign import SignDialog
from pdfeditor.ui.main_window import MainWindow

pytestmark = pytest.mark.gui


class StubDialog:
    """Stands in for the signer dialog: answers come from the test's script."""

    # (result, add another, name, email), one entry per signer dialog shown
    script: ClassVar[list[tuple[int, bool, str, str]]] = []

    def __init__(self, number: int, page: int, parent=None) -> None:
        self.result, self.add_another, self._name, self._email = self.script.pop(0)

    def exec(self) -> int:
        return self.result

    def signer_name(self) -> str:
        return self._name

    def signer_email(self) -> str:
        return self._email


@pytest.fixture
def window(qtbot, monkeypatch):
    from pdfeditor.services.signing import windows_store

    monkeypatch.setattr(windows_store, "list_certificates", list)
    w = MainWindow()
    w.resize(1200, 900)
    w.show()
    qtbot.waitExposed(w)
    yield w
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()


@pytest.fixture
def view(window: MainWindow, fixture_pdf, tmp_path: Path):
    path = tmp_path / "contract.pdf"
    shutil.copy2(fixture_pdf("text_multipage"), path)
    v = window.open_path(path)
    v.set_zoom(1.0)
    v.go_to_page(0, record=False)
    return v


@pytest.fixture
def opened_urls(monkeypatch) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr(
        sign_controller.QDesktopServices, "openUrl", lambda url: urls.append(url.toString())
    )
    return urls


@pytest.fixture
def stub_dialog(monkeypatch) -> type[StubDialog]:
    StubDialog.script = []
    monkeypatch.setattr(sign_controller, "RequestSignerDialog", StubDialog)
    return StubDialog


@pytest.fixture
def email_button(monkeypatch) -> None:
    """The result box opens; the test presses Email Recipients without a person at the screen."""

    def press_email(self: QMessageBox) -> int:
        next(b for b in self.buttons() if b.text() == "Email Recipients").click()
        return 0

    monkeypatch.setattr(sign_controller.QMessageBox, "exec", press_email)


def drag(window: MainWindow, qtbot, view, a: Point, b: Point) -> None:
    """A real drag on the page, which the request tool turns into a signer's box."""
    start = view.mapFromScene(view.page_point_to_scene(0, a))
    end = view.mapFromScene(view.page_point_to_scene(0, b))
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(view.viewport(), pos=end)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)


def test_request_action_is_in_the_ribbon_and_help(window: MainWindow) -> None:
    from pdfeditor.ui.action_help import HELP
    from pdfeditor.ui.shortcuts import action_id

    assert window.signing.act_request in window.ribbon.button_actions()
    assert not window.signing.act_request.icon().isNull()
    assert action_id(window.signing.act_request) in HELP


def test_finish_writes_the_file_and_offers_the_email_draft(
    window: MainWindow,
    view,
    qtbot,
    stub_dialog,
    opened_urls,
    email_button,
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "contract-for-signature.pdf"
    monkeypatch.setattr(window.signing, "choose_target", lambda *a, **k: target)
    stub_dialog.script = [(1, False, "Ana Alvarez", "ana@example.com")]
    window.signing.request()
    assert window.current_tool == "request_signatures"
    drag(window, qtbot, view, Point(70, 250), Point(300, 320))
    qtbot.waitUntil(lambda: bool(opened_urls))
    assert window.current_tool == "select"
    assert target.exists()
    assert window.current_view().session.path == target.resolve()
    (field,) = preflight(target).empty_fields
    assert field.name == "SignatureRequest1"
    assert field.label == "Signature of Ana Alvarez"
    assert field.page == 0
    url = opened_urls[0]
    assert url.startswith("mailto:ana@example.com?subject=")
    assert unquote(url).find("Please sign: contract-for-signature.pdf") >= 0
    assert "Your email app" not in url  # the message text is for the dialog, not the address


def test_add_another_keeps_the_tool_and_both_signers_get_fields(
    window: MainWindow,
    view,
    qtbot,
    stub_dialog,
    opened_urls,
    email_button,
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "two-for-signature.pdf"
    monkeypatch.setattr(window.signing, "choose_target", lambda *a, **k: target)
    stub_dialog.script = [
        (1, True, "Ana", "ana@example.com"),
        (1, False, "Ben", ""),
    ]
    window.signing.request()
    window.signing.request_placed(0, Rect(50, 50, 300, 110))
    qtbot.waitUntil(
        lambda: window.signing._requests is not None and len(window.signing._requests) == 1
    )
    assert window.current_tool == "request_signatures"  # back to drawing for the next signer
    window.signing.request_placed(0, Rect(50, 130, 300, 190))
    qtbot.waitUntil(lambda: bool(opened_urls))
    fields = preflight(target).empty_fields
    assert [f.label for f in fields] == ["Signature of Ana", "Signature of Ben"]
    assert opened_urls[0].startswith("mailto:ana@example.com?subject=")


def test_cancelling_a_signer_drops_only_that_box(
    window: MainWindow, view, qtbot, stub_dialog, opened_urls, email_button, tmp_path, monkeypatch
) -> None:
    target = tmp_path / "one.pdf"
    monkeypatch.setattr(window.signing, "choose_target", lambda *a, **k: target)
    stub_dialog.script = [(1, True, "Ana", ""), (0, False, "Ben", ""), (1, False, "Cy", "")]
    window.signing.request()
    for top in (50, 130, 210):
        window.signing.request_placed(0, Rect(50, top, 300, top + 60))
        qtbot.wait(20)
    qtbot.waitUntil(lambda: bool(opened_urls))
    labels = [f.label for f in preflight(target).empty_fields]
    assert labels == ["Signature of Ana", "Signature of Cy"]


def test_done_finishes_without_drawing_another_box(
    window: MainWindow, view, qtbot, stub_dialog, opened_urls, email_button, tmp_path, monkeypatch
) -> None:
    target = tmp_path / "done.pdf"
    monkeypatch.setattr(window.signing, "choose_target", lambda *a, **k: target)
    stub_dialog.script = [(1, True, "Ana", "")]
    window.signing.request()
    window.signing.request_placed(0, Rect(50, 50, 300, 110))
    qtbot.waitUntil(lambda: bool(window.signing._requests))
    assert window.current_tool == "request_signatures"
    window.mode_banner.done.emit()
    qtbot.waitUntil(lambda: bool(opened_urls))
    assert window.current_tool == "select"
    assert [f.label for f in preflight(target).empty_fields] == ["Signature of Ana"]


def test_the_tool_stays_active_after_a_box_whatever_the_preference(
    window: MainWindow, view, qtbot, stub_dialog
) -> None:
    window.prefs.keep_tools = False
    stub_dialog.script = [(1, True, "Ana", "")]
    window.signing.request()
    view.tool_used.emit()
    assert window.current_tool == "request_signatures"
    window.set_tool("select")


def test_leaving_the_tool_with_no_signers_ends_the_request(
    window: MainWindow, view, qtbot, stub_dialog, monkeypatch
) -> None:
    """It used to stay running: Request Signatures then did nothing, and Place Signature's next
    box was taken for a signer's."""
    window.signing.request()
    window.set_tool("select")
    assert window.signing._requests is None
    assert "No file was written" in window.signing.last_message
    signed: list[object] = []
    monkeypatch.setattr(window.signing, "sign", signed.append)
    window.signing.placed(0, Rect(50, 50, 300, 110))
    qtbot.waitUntil(lambda: bool(signed))
    assert not stub_dialog.script
    window.signing.request()
    assert window.current_tool == "request_signatures"
    window.set_tool("select")


def test_closing_the_document_discards_the_request(
    window: MainWindow, view, qtbot, stub_dialog, tmp_path, monkeypatch
) -> None:
    chosen: list[object] = []
    monkeypatch.setattr(window.signing, "choose_target", lambda *a, **k: chosen.append(a))
    stub_dialog.script = [(1, True, "Ana", "")]
    window.signing.request()
    window.signing.request_placed(0, Rect(50, 50, 300, 110))
    qtbot.waitUntil(lambda: bool(window.signing._requests))
    window.close_tab(window.tabs.currentIndex(), ask=False)
    qtbot.wait(30)
    assert window.signing._requests is None and window.signing._request_password is None
    assert not chosen, "no save dialog for a document that's gone"


def test_a_second_request_says_why_it_cant_start(window: MainWindow, view, monkeypatch) -> None:
    said: list[str] = []
    window.signing.request()
    monkeypatch.setattr(window, "notify", lambda text, *_a, **_k: said.append(text))
    window.signing.request()
    assert said and "Done" in said[0]
    monkeypatch.undo()
    window.set_tool("select")


def test_too_small_a_box_only_says_so(window: MainWindow, view, qtbot, stub_dialog) -> None:
    window.signing.request()
    stub_dialog.script = []
    window.signing.request_placed(0, Rect(10, 10, 20, 20))
    qtbot.wait(5)
    assert window.signing._requests == []  # nothing was asked for


def test_a_blank_name_is_refused_in_the_dialog(qtbot) -> None:
    dialog = RequestSignerDialog(1, 0)
    qtbot.addWidget(dialog)
    dialog.name.setText("   ")
    dialog.primary_clicked()
    assert dialog.result() == 0
    assert not dialog.error.isHidden()
    dialog.name.setText("Ana")
    dialog.email.setText("not-an-email")
    dialog.primary_clicked()
    assert dialog.result() == 0
    dialog.email.setText("ana@example.com")
    dialog.primary_clicked()
    assert dialog.result() == 1
    assert dialog.add_another is False


def test_add_another_needs_a_valid_name_too(qtbot) -> None:
    dialog = RequestSignerDialog(1, 0)
    qtbot.addWidget(dialog)
    dialog._add_another()
    assert dialog.result() == 0
    assert dialog.add_another is False


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("", True),
        ("ana@example.com", True),
        ("  ana@example.com  ", True),
        ("ana.example.com", False),
        ("a@@b.c", False),
        ("@example.com", False),
        ("ana@", False),
        ("an a@example.com", False),
    ],
)
def test_email_check_is_simple_and_optional(text: str, ok: bool) -> None:
    assert valid_email(text) is ok


def test_sign_dialog_offers_empty_fields_only_for_invisible_signatures(
    window: MainWindow, qtbot
) -> None:
    fields = (
        EmptySignatureField("SignatureRequest1", "Signature of Ana", 0, Rect(50, 50, 300, 110)),
        EmptySignatureField("SignatureRequest2", "Signature of Ben", 1, None),
    )
    dialog = SignDialog(None, window, empty_fields=fields)
    qtbot.addWidget(dialog)
    combo = dialog.field_choice
    assert combo is not None
    assert combo.count() == 3  # invisible, then the two fields
    assert combo.currentIndex() == 1  # the first empty field is the default
    assert combo.itemText(2) == "Signature of Ben (page 2)"
    assert dialog.options().field_name == "SignatureRequest1"
    combo.setCurrentIndex(0)
    assert dialog.options().field_name is None
    combo.setCurrentIndex(2)
    assert dialog.options().field_name == "SignatureRequest2"
    assert dialog.options().placement is None

    drawn = SignDialog(SignaturePlacement(0, Rect(1, 1, 90, 40)), window, empty_fields=fields)
    qtbot.addWidget(drawn)
    assert drawn.field_choice is None
    assert drawn.options().field_name is None

    plain = SignDialog(None, window)
    qtbot.addWidget(plain)
    assert plain.field_choice is None
    assert plain.options().field_name is None
    assert SignatureOptions().field_name is None
