"""Sanitizing leak checks through MuPDF itself (backend test: uses PyMuPDF directly).

MuPDF's extraction hides OFF layers and clips to the page, so the checks switch every layer
on and read the raw text trace, which reports every glyph wherever it is drawn.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pymupdf

from pdfeditor.engine.base import SaveOptions
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.redaction import SanitizeOptions


def _all_text(path: Path) -> str:
    doc = pymupdf.open(path)
    try:
        for layer in doc.layer_ui_configs():
            doc.set_layer_ui_config(layer["number"], 0)  # switch on
        traced = "|".join(
            "".join(chr(c[0]) for c in span["chars"])
            for page in doc
            for span in page.get_texttrace()
        )
        return traced + "|" + "|".join(page.get_text() for page in doc)
    finally:
        doc.close()


def _sanitized(fixture_pdf, tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.pdf"
    shutil.copy2(fixture_pdf(name), path)
    doc = get_engine("mupdf").open(path)
    doc.sanitize(SanitizeOptions())
    doc.save(options=SaveOptions(garbage=4))
    doc.close()
    return path


def test_hidden_layer_text_gone_even_with_all_layers_on(fixture_pdf, tmp_path: Path) -> None:
    before = _all_text(fixture_pdf("hidden_layers"))
    for secret in (
        "Hidden layer secret",
        "Membership secret",
        "Hidden form secret",
        "Nested form secret",
        "Hidden note secret",
    ):
        assert secret in before  # the check can see hidden-layer text
    after = _all_text(_sanitized(fixture_pdf, tmp_path, "hidden_layers"))
    assert "secret" not in after.lower()
    assert "Always visible" in after and "Visible form text" in after


def test_off_page_text_gone_from_text_trace(fixture_pdf, tmp_path: Path) -> None:
    before = _all_text(fixture_pdf("off_page_text"))
    assert "Cropped secret" in before and "Below media secret" in before
    after = _all_text(_sanitized(fixture_pdf, tmp_path, "off_page_text"))
    assert "secret" not in after.lower()
    assert after.count("Visible text stays") == 4  # trace + extraction, two pages
    assert after.count("Stradd") == 4


def test_shared_resources_and_ocmd_policies(tmp_path: Path) -> None:
    """Two pages sharing one resource dictionary, an OCMD kept visible by a shown group and a
    group that starts OFF through /BaseState."""
    doc = pymupdf.open()
    shown = doc.add_ocg("Shown", on=True)
    off = doc.add_ocg("Off", on=False)
    ocmd_visible = doc.set_ocmd(ocgs=[shown, off], policy="AnyOn")
    for i in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), f"Visible {i}", fontsize=12)
        page.insert_text((72, 100), f"Off secret {i}", fontsize=12, oc=off)
        page.insert_text((72, 130), f"Either layer {i}", fontsize=12, oc=ocmd_visible)
    shared = doc.xref_get_key(doc[0].xref, "Resources")[1]
    doc.xref_set_key(doc[1].xref, "Resources", shared)
    path = tmp_path / "shared.pdf"
    doc.save(path)
    doc.close()
    sanitized = get_engine("mupdf").open(path)
    report = sanitized.sanitize(SanitizeOptions())
    sanitized.save(options=SaveOptions(garbage=4))
    sanitized.close()
    assert any("hidden layer" in r for r in report)
    text = _all_text(path)
    assert "secret" not in text.lower()
    assert "Either layer 0" in text and "Either layer 1" in text
    check = pymupdf.open(path)
    # the OFF group is still named by the visible OCMD, so it must stay defined
    assert sorted(v["name"] for v in check.get_ocgs().values()) == ["Off", "Shown"]
    check.close()
