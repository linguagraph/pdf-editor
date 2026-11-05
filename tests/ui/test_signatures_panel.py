"""The Signatures panel: what it says about each signature, and when it checks the file."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

from pdfeditor.engine.registry import get_engine
from pdfeditor.model.signing import PfxKey, SignatureOptions, SignatureVerdict
from pdfeditor.services.signing import inspect_signatures, sign_file
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.panels.signatures import VERDICT_LABELS, SignaturesPanel

pytestmark = pytest.mark.gui

ENGINE = get_engine()


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


def _signed_copy(fixture_pdf: Callable[[str], Path], pfx: Path, target: Path) -> Path:
    sign_file(fixture_pdf("images"), target, PfxKey(pfx, "test"), SignatureOptions(), ENGINE)
    return target


def _der(pfx: Path) -> bytes:
    _key, cert, _others = pkcs12.load_key_and_certificates(pfx.read_bytes(), b"test")
    assert cert is not None
    return cert.public_bytes(serialization.Encoding.DER)


def _rows(top: Any) -> list[str]:
    return [top.child(i).text(0) for i in range(top.childCount())]


class FakeJobs:
    """Records what the panel asks the job center to run, so tests can finish it by hand."""

    def __init__(self) -> None:
        self.work: Callable[[Any], Any] | None = None
        self.kwargs: dict[str, Any] = {}

    def start(self, _label: str, work: Callable[[Any], Any], **kwargs: Any) -> None:
        self.work = work
        self.kwargs = kwargs


def test_unsigned_document_shows_the_empty_state(window, fixture_pdf) -> None:
    window.open_path(fixture_pdf("images"))
    signatures = panel(window, SignaturesPanel)
    signatures._refresh()
    assert not signatures.empty.isHidden()
    assert signatures.tree.isHidden()
    assert signatures.badge_count() == 0


def test_signed_document_is_checked_in_the_background(
    qtbot, window, fixture_pdf, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    signed = _signed_copy(fixture_pdf, throwaway_pfx(), tmp_path / "signed.pdf")
    window.open_path(signed)
    signatures = panel(window, SignaturesPanel)
    monkeypatch.setattr(signatures, "isVisible", lambda: True)
    signatures._refresh()
    qtbot.waitUntil(lambda: signatures._reports is not None, timeout=20000)
    assert signatures.badge_count() == 1
    top = signatures.tree.topLevelItem(0)
    assert top.text(0) == "Rev. 1: Signed by Elanero Test Signer (needs checking)"
    # Windows doesn't trust the throwaway certificate, so it must not show as valid.
    assert signatures._reports[0].verdict is not SignatureVerdict.VALID
    assert any("hasn't changed" in row for row in _rows(top))


def test_each_verdict_has_its_own_validity_sentence(
    window, fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    pfx = throwaway_pfx()
    signed = _signed_copy(fixture_pdf, pfx, tmp_path / "signed.pdf")
    tampered = tmp_path / "tampered.pdf"
    data = bytearray(signed.read_bytes())
    data[20] ^= 0x01
    tampered.write_bytes(bytes(data))
    cases = {
        SignatureVerdict.VALID: inspect_signatures(signed, None, [_der(pfx)]),
        SignatureVerdict.UNTRUSTED: inspect_signatures(signed, None, []),
        SignatureVerdict.INVALID: inspect_signatures(tampered, None, [_der(pfx)]),
    }
    signatures = panel(window, SignaturesPanel)
    sentences = {
        SignatureVerdict.VALID: "The signature is valid",
        SignatureVerdict.UNTRUSTED: "signer's identity couldn't be confirmed",
        SignatureVerdict.INVALID: "doesn't match the document",
    }
    for verdict, reports in cases.items():
        signatures._show_reports(reports)
        rows = _rows(signatures.tree.topLevelItem(0))
        assert any(sentences[verdict] in row for row in rows), (verdict, rows)
        assert any("Revocation wasn't checked" in row for row in rows)
        assert any(row.startswith("Last Checked:") for row in rows)
        assert "Click to view this version" in rows
        top = signatures.tree.topLevelItem(0).text(0)
        assert top.endswith(f"({VERDICT_LABELS[verdict]})"), "collapsed rows must say it in words"


def test_unsaved_changes_are_called_out(
    window, fixture_pdf, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    signed = _signed_copy(fixture_pdf, throwaway_pfx(), tmp_path / "signed.pdf")
    view = window.open_path(signed)
    signatures = panel(window, SignaturesPanel)
    monkeypatch.setattr(type(view.session), "is_dirty", property(lambda _s: True))
    signatures._refresh()
    assert not signatures.note.isHidden()
    assert signatures.note.text() == "Unsaved changes aren't part of these results."
    # Undo the patch before the window closes: a dirty document would prompt to save and block.
    monkeypatch.undo()


def test_checks_wait_until_the_panel_is_open(
    window, fixture_pdf, throwaway_pfx, tmp_path: Path
) -> None:
    signed = _signed_copy(fixture_pdf, throwaway_pfx(), tmp_path / "signed.pdf")
    window.open_path(signed)
    signatures = panel(window, SignaturesPanel)
    fake = FakeJobs()
    signatures.attach(fake, lambda _path: None)  # type: ignore[arg-type]
    signatures.hide()
    signatures._refresh()
    assert fake.work is None  # nothing was started
    assert signatures._reports is None
    assert not signatures.status.isHidden()


def test_a_result_for_an_old_view_is_ignored(
    window, fixture_pdf, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    signed = _signed_copy(fixture_pdf, throwaway_pfx(), tmp_path / "signed.pdf")
    window.open_path(signed)
    signatures = panel(window, SignaturesPanel)
    fake = FakeJobs()
    signatures.attach(fake, lambda _path: None)  # type: ignore[arg-type]
    monkeypatch.setattr(signatures, "isVisible", lambda: True)
    signatures._refresh()
    assert fake.work is not None
    finish = fake.kwargs["on_done"]
    signatures._forget()  # the document is closed or changed while the check runs
    finish(inspect_signatures(signed, None, []))
    assert signatures._reports is None
    assert signatures.tree.isHidden()


def test_clicking_an_entry_goes_to_its_page(
    window, fixture_pdf, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    pfx = throwaway_pfx()
    visible = tmp_path / "visible.pdf"
    from pdfeditor.model.geometry import Rect
    from pdfeditor.model.signing import SignaturePlacement

    sign_file(
        fixture_pdf("images"),
        visible,
        PfxKey(pfx, "test"),
        SignatureOptions(placement=SignaturePlacement(0, Rect(50, 50, 250, 110))),
        ENGINE,
    )
    view = window.open_path(visible)
    signatures = panel(window, SignaturesPanel)
    signatures._show_reports(inspect_signatures(visible, None, [_der(pfx)]))
    visits: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        view,
        "go_to_page",
        lambda index, *_a, animated=False, **_k: visits.append((index, animated)),
    )
    signatures._clicked(signatures.tree.topLevelItem(0))
    assert visits == [(0, True)]


def test_view_this_version_opens_the_earlier_revision(
    window, fixture_pdf, throwaway_pfx, tmp_path: Path, monkeypatch
) -> None:
    pfx = throwaway_pfx()
    first = _signed_copy(fixture_pdf, pfx, tmp_path / "one.pdf")
    second = tmp_path / "two.pdf"
    sign_file(first, second, PfxKey(pfx, "test"), SignatureOptions(), ENGINE)
    window.open_path(second)
    signatures = panel(window, SignaturesPanel)
    reports = inspect_signatures(second, None, [_der(pfx)])
    signatures._checked_path = second
    signatures._show_reports(reports)
    opened: list[Path] = []
    fake = FakeJobs()
    signatures.attach(fake, opened.append)  # type: ignore[arg-type]
    link = signatures.tree.topLevelItem(0).child(signatures.tree.topLevelItem(0).childCount() - 1)
    signatures._clicked(link)  # the first revision's "view this version" link
    assert fake.work is not None
    target = fake.work(None)
    fake.kwargs["on_done"](target)
    assert opened == [target]
    (earlier,) = inspect_signatures(target, None, [_der(pfx)])
    assert earlier.verdict is SignatureVerdict.VALID
    assert earlier.revision == 1
    assert "revision 1" in target.name


def test_long_rows_scroll_sideways_instead_of_being_cut_off(
    fixture_pdf, throwaway_pfx, tmp_path: Path, qtbot
) -> None:
    signed = _signed_copy(fixture_pdf, throwaway_pfx(), tmp_path / "signed.pdf")
    signatures = SignaturesPanel()
    qtbot.addWidget(signatures)
    signatures.resize(200, 400)
    signatures.show()
    qtbot.waitExposed(signatures)
    signatures._show_reports(inspect_signatures(signed, None, []))
    tree = signatures.tree
    top = tree.topLevelItem(0)

    def needed(parent: Any) -> int:
        metrics = tree.fontMetrics()
        return max(metrics.horizontalAdvance(_) for _ in _rows(parent))

    qtbot.waitUntil(lambda: tree.columnWidth(0) >= needed(top))
    assert tree.horizontalScrollBar().maximum() > 0
    # Expanding the details widens the column to fit them too.
    details = next(
        top.child(i) for i in range(top.childCount()) if top.child(i).text(0) == "Signature Details"
    )
    details.setExpanded(True)
    qtbot.waitUntil(lambda: tree.columnWidth(0) >= needed(details))
