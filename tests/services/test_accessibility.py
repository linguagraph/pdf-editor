from __future__ import annotations

import pytest

from pdfeditor.engine.registry import get_engine
from pdfeditor.model.color import Color
from pdfeditor.model.metadata import Metadata
from pdfeditor.model.structure import AccessibilitySettings
from pdfeditor.services.accessibility import Status, check, contrast_ratio

ENGINE = get_engine()


def by_rule(findings) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in findings:
        out.setdefault(f.rule, []).append(f)
    return out


def test_contrast_ratio() -> None:
    assert contrast_ratio(Color(0, 0, 0), Color(1, 1, 1)) == pytest.approx(21, abs=0.01)
    grey = 0x76 / 255  # #767676: the classic just-passing grey on white
    assert contrast_ratio(Color(grey, grey, grey), Color(1, 1, 1)) == pytest.approx(4.54, abs=0.01)
    assert contrast_ratio(Color(1, 1, 1), Color(1, 1, 1)) == pytest.approx(1)


def test_tagged_document_findings(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("tagged"))
    rules = by_rule(check(doc))
    assert rules["tagged"][0].status is Status.PASSED
    assert rules["language"][0].status is Status.FAILED and rules["language"][0].fixable
    assert rules["display-title"][0].status is Status.FAILED
    (alt,) = rules["alt-text"]
    assert alt.status is Status.FAILED and alt.page_index == 0 and alt.ref is not None
    (heading,) = rules["headings"]
    assert heading.status is Status.WARNING and "H3 after H1" in heading.title
    (order,) = rules["reading-order"]
    assert order.status is Status.WARNING and order.page_index == 0
    (contrast,) = rules["contrast"]
    assert contrast.status is Status.WARNING and contrast.page_index == 0
    doc.close()


def test_findings_clear_after_fixes(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("tagged"))
    (root,) = doc.structure_tree()
    doc.set_accessibility_settings(AccessibilitySettings("en-US", True, True))
    doc.set_struct_element(root.children[3].ref, alt="A gradient")
    doc.set_struct_element(root.children[4].ref, type="H2")
    rules = by_rule(check(doc))
    for rule in ("language", "display-title", "alt-text", "headings"):
        assert all(f.status is Status.PASSED for f in rules[rule]), rule
    doc.close()


def test_untagged_document(fixture_pdf) -> None:
    doc = ENGINE.open(fixture_pdf("annotations"))
    rules = by_rule(check(doc))
    assert rules["tagged"][0].status is Status.FAILED and not rules["tagged"][0].fixable
    assert rules["tab-order"][0].status is Status.FAILED
    assert "alt-text" not in rules  # nothing tagged, nothing to report per tag
    doc.set_metadata(Metadata(title=""))
    assert by_rule(check(doc))["title"][0].status is Status.FAILED
    doc.close()
