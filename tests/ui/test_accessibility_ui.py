from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pdfeditor.services.accessibility import Status
from pdfeditor.ui.main_window import MainWindow

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
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
    path = tmp_path / "tagged.pdf"
    shutil.copy2(fixture_pdf("tagged"), path)
    return window.open_path(path)


def select(panel, rule: str) -> None:
    for i in range(panel.tree.topLevelItemCount()):
        group = panel.tree.topLevelItem(i)
        for j in range(group.childCount()):
            item = group.child(j)
            idx = item.data(0, 256)
            if idx is not None and panel.findings[idx].rule == rule:
                panel.tree.setCurrentItem(item)
                return
    raise AssertionError(f"no finding {rule}")


def test_check_and_fix(window: MainWindow, view) -> None:
    window.accessibility_check()
    panel = window.accessibility_panel
    assert window.nav_panels.current() is panel
    failed = {f.rule for f in panel.findings if f.status is Status.FAILED}
    assert {"language", "display-title", "alt-text"} <= failed
    assert "failed" in panel.summary.text()
    select(panel, "language")
    assert panel.fix_button.isEnabled()
    assert panel.fix_selected("bg-BG")
    select(panel, "display-title")
    assert panel.fix_selected()
    select(panel, "alt-text")
    assert panel.fix_selected("A colour gradient")
    failed = {f.rule for f in panel.findings if f.status is Status.FAILED}
    assert not failed & {"language", "display-title", "alt-text"}
    with view.session.lock:
        settings = view.session.document.accessibility_settings()
    assert settings.language == "bg-BG" and settings.display_doc_title
    # every fix is one undo step
    window.act_undo.trigger()
    window.act_undo.trigger()
    window.act_undo.trigger()
    with view.session.lock:
        assert view.session.document.accessibility_settings().language == ""


def test_jump_to_tag(window: MainWindow, view) -> None:
    window.accessibility_check()
    panel = window.accessibility_panel
    select(panel, "reading-order")
    panel._jump(panel.tree.currentItem())
    tags = window.tags_panel
    assert window.nav_panels.current() is tags
    assert tags.nodes[tags.selected_ref()].type == "Figure"


def test_tags_panel_edits(window: MainWindow, view) -> None:
    tags = window.tags_panel
    window.show_panel(tags)
    assert not tags.empty.isVisible() and len(tags.nodes) == 8
    h3 = next(ref for ref, n in tags.nodes.items() if n.type == "H3")
    tags.show_tag(h3)
    assert tags.change_type("H2")
    assert tags.nodes[h3].type == "H2"  # rebuilt after the command
    tags.show_tag(h3)
    before = [c.ref for c in tags.nodes[tags.parents[h3]].children]
    assert tags.move_tag(-1)
    after = [c.ref for c in tags.nodes[tags.parents[h3]].children]
    i = before.index(h3)
    assert after[i - 1] == h3
    tags.show_tag(h3)
    assert tags.change_alt("Heading about levels")
    assert tags.nodes[h3].alt == "Heading about levels"
    window.act_undo.trigger()
    window.act_undo.trigger()
    window.act_undo.trigger()
    assert (
        tags.nodes[h3].type == "H3"
        and [c.ref for c in tags.nodes[tags.parents[h3]].children] == before
    )


def test_untagged_document(window: MainWindow, fixture_pdf) -> None:
    window.open_path(fixture_pdf("report"))
    assert window.tags_panel.empty.isVisibleTo(window.tags_panel)


def test_auto_tag(window: MainWindow, fixture_pdf, tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "report.pdf"
    shutil.copy2(fixture_pdf("report"), path)
    view = window.open_path(path)
    action = window.pdfa.act_auto_tag
    assert action.isVisible() and "experimental" in action.text()
    assert action in window.ribbon.tab("Tools").button_actions()
    tools_menu = next(a.menu() for a in window.menu_bar.actions() if "Tools" in a.text())
    assert action in tools_menu.actions()
    tags = window.tags_panel
    window.show_panel(tags)
    assert not tags.nodes
    action.trigger()
    assert "Tagged the document" in window.pdfa.last_message and view.session.is_dirty
    types = {n.type for n in tags.nodes.values()}
    assert {"Document", "H1", "H2", "P", "Figure"} <= types
    window.accessibility_check()
    tagged = next(f for f in window.accessibility_panel.findings if f.rule == "tagged")
    assert tagged.status is Status.PASSED
    shown: list[str] = []
    monkeypatch.setattr(
        "pdfeditor.ui.pdfa_controller.QMessageBox.information",
        lambda _p, _t, msg: shown.append(msg),
    )
    assert window.pdfa.auto_tag() is None  # refuses a tagged document
    assert shown and "already tagged" in shown[0]
    window.act_undo.trigger()
    assert not tags.nodes
    with view.session.lock:
        assert not view.session.document.info().is_tagged
