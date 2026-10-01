"""U9: the shared dialog frame, collapsible sections, Preferences categories, migrated dialogs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from pdfeditor.model.geometry import Rect
from pdfeditor.model.metadata import SpaceUsage
from pdfeditor.model.text import TableData
from pdfeditor.services.redaction import MarkStyle
from pdfeditor.ui.dialogs import (
    about,
    compare,
    export,
    ocr,
    optimize,
    pages,
    pdfa,
    print_dialog,
    properties,
    recovery,
    redaction,
    security,
)
from pdfeditor.ui.dialogs.base import FormDialog, Section, add_row
from pdfeditor.ui.dialogs.preferences import CATEGORIES, PreferencesDialog
from pdfeditor.ui.main_window import MainWindow
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.shortcuts import ShortcutsDialog

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
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat))


def unnamed_widgets(dialog: QWidget) -> list[str]:
    out = []
    for w in dialog.findChildren(QWidget):
        if not w.isVisibleTo(dialog) or w.focusPolicy() == Qt.FocusPolicy.NoFocus:
            continue
        if isinstance(w.parentWidget(), QAbstractSpinBox | QComboBox):
            continue  # the editor inside a spin or combo box: the box itself is named
        text = w.text() if isinstance(w, QAbstractButton) else ""
        if not (w.accessibleName() or text or w.toolTip()):
            out.append(f"{type(w).__name__} {w.objectName()}")
    return out


# -- base class --------------------------------------------------------------------------------
def test_header_form_help_and_primary_button(qtbot) -> None:
    d = FormDialog("Do Things", "Explains what happens.", primary="Go")
    qtbot.addWidget(d)
    form = d.add_form()
    edit = QLineEdit()
    help_label = add_row(form, "Name:", edit, "Shown on everything.")
    d.show()
    assert d.windowTitle() == "Do Things"
    assert d.header_title.text() == "Do Things"
    assert d.header_subtitle.isVisible() and d.header_subtitle.text() == "Explains what happens."
    assert form.labelAlignment() & Qt.AlignmentFlag.AlignRight
    assert help_label is not None and help_label.property("role") == "caption"
    assert help_label.text() == "Shown on everything." and help_label.isVisible()
    assert edit.accessibleName() == "Name"  # from the form label
    assert d.primary_button is not None and d.primary_button.isDefault()
    assert d.primary_button.text() == "Go"
    assert d.cancel_button is not None and not d.cancel_button.isDefault()
    with qtbot.waitSignal(d.accepted):
        d.primary_button.click()


def test_no_subtitle_and_close_only(qtbot) -> None:
    d = FormDialog("Info", primary=None, cancel="Close")
    qtbot.addWidget(d)
    d.show()
    assert not d.header_subtitle.isVisible()
    assert d.primary_button is None
    assert d.cancel_button is not None and d.cancel_button.isDefault()
    with qtbot.waitSignal(d.rejected):
        d.cancel_button.click()


def test_danger_primary_button(qtbot) -> None:
    d = FormDialog("Remove", primary="Delete", danger=True)
    qtbot.addWidget(d)
    assert d.primary_button is not None and d.primary_button.property("role") == "danger"
    assert d.primary_button.isDefault()
    applied = redaction.ApplyRedactionsDialog(2, 0)
    qtbot.addWidget(applied)
    assert applied.primary_button is not None
    assert applied.primary_button.property("role") == "danger"


def test_section_toggles_and_is_named(qtbot) -> None:
    d = FormDialog("Sections")
    qtbot.addWidget(d)
    section = d.add_section("Advanced", expanded=False)
    box = QSpinBox()
    add_row(section.form(), "Value:", box)
    d.show()
    assert section.header.accessibleName() == "Advanced section"
    assert section.header.text() == "Advanced"
    assert not section.is_expanded() and not box.isVisible()
    assert section.header.arrowType() == Qt.ArrowType.RightArrow
    with qtbot.waitSignal(section.toggled) as blocker:
        qtbot.mouseClick(section.header, Qt.MouseButton.LeftButton)
    assert blocker.args == [True]
    assert section.is_expanded() and box.isVisible()
    assert section.header.arrowType() == Qt.ArrowType.DownArrow
    section.set_expanded(False)
    assert not box.isVisible() and not section.header.isChecked()
    box.setValue(7)  # hidden fields keep working
    assert box.value() == 7


def test_standalone_section(qtbot) -> None:
    s = Section("More")
    qtbot.addWidget(s)
    s.show()
    assert s.content.isVisible()
    s.header.click()
    assert not s.content.isVisible()


def test_section_forms_line_up_with_the_main_form(qtbot) -> None:
    d = FormDialog("Aligned")
    qtbot.addWidget(d)
    main = d.add_form()
    add_row(main, "A:", QLineEdit())
    section = d.add_section("Details")
    add_row(section.form(), "A much longer label:", QLineEdit())
    d.show()
    qtbot.waitExposed(d)

    def field_x(form: QFormLayout) -> int:
        item = form.itemAt(0, QFormLayout.ItemRole.FieldRole)
        assert item is not None and item.widget() is not None
        return item.widget().mapTo(d, item.widget().rect().topLeft()).x()

    assert field_x(main) == field_x(section.form())


# -- preferences ---------------------------------------------------------------------------------
def test_preferences_sidebar_switches_pages(qtbot, settings: AppSettings) -> None:
    d = PreferencesDialog(settings)
    qtbot.addWidget(d)
    d.show()
    assert [d.sidebar.item(i).text() for i in range(d.sidebar.count())] == list(CATEGORIES)
    assert d.current_category() == "General" and d.author.isVisible()
    d.sidebar.setCurrentRow(CATEGORIES.index("Appearance"))
    assert d.current_category() == "Appearance"
    assert d.theme.isVisible() and d.accent.isVisible() and not d.author.isVisible()
    d.show_category("Performance")
    assert d.cache.isVisible() and d.undo_disk.isVisible()
    d.show_category("Tools")
    assert d.tool.isVisible() and d.keep_tools.isVisible()
    for name in CATEGORIES:
        d.show_category(name)
        assert unnamed_widgets(d) == [], name


def test_preferences_saves_every_setting(qtbot, settings: AppSettings) -> None:
    d = PreferencesDialog(settings)
    qtbot.addWidget(d)
    d.author.setText(" Reviewer ")
    d.language.setCurrentIndex(d.language.count() - 1)
    language = d.language.currentData()
    d.autosave.setValue(5)
    d.theme.setCurrentIndex(d.theme.findData("dark"))
    d.accent.setCurrentIndex(d.accent.findData("#8764b8"))
    d.menu_bar.setChecked(not settings.show_menu_bar)
    menu_bar = d.menu_bar.isChecked()
    d.zoom.setCurrentIndex(d.zoom.findData("100"))
    d.tool.setCurrentIndex(d.tool.findData("hand"))
    d.keep_tools.setChecked(True)
    d.cache.setValue(512)
    d.undo_disk.setValue(1024)
    d.show_category("Shortcuts")  # the page shown doesn't matter
    assert d.primary_button is not None and d.primary_button.isDefault()
    d.primary_button.click()
    assert d.result() == QDialog.DialogCode.Accepted
    s = settings
    assert s.author == "Reviewer" and s.language == language and s.autosave_minutes == 5
    assert (s.theme, s.accent, s.show_menu_bar) == ("dark", "#8764b8", menu_bar)
    assert (s.default_zoom, s.default_tool, s.keep_tools) == ("100", "hand", True)
    assert (s.cache_mb, s.undo_disk_mb) == (512, 1024)
    again = PreferencesDialog(settings)
    qtbot.addWidget(again)
    assert again.author.text() == "Reviewer" and again.tool.currentData() == "hand"
    assert again.cache.value() == 512 and again.undo_disk.value() == 1024


def test_preferences_shortcuts_page(window: MainWindow, monkeypatch) -> None:
    opened: list[ShortcutsDialog] = []
    monkeypatch.setattr(ShortcutsDialog, "exec", lambda self: opened.append(self) or 0)
    d = PreferencesDialog(window.prefs, window)
    d.show_category("Shortcuts")
    assert d.shortcuts_button.isEnabled()
    d.shortcuts_button.click()
    assert len(opened) == 1 and opened[0].manager is window.shortcuts
    d.close()
    alone = PreferencesDialog(window.prefs)  # no main window: nothing to edit
    assert not alone.shortcuts_button.isEnabled()
    alone.close()


# -- migrated dialogs ------------------------------------------------------------------------------
def migrated(window: MainWindow, view) -> dict[str, Callable[[], FormDialog]]:
    n = view.page_count
    path = view.session.path
    usage = SpaceUsage(15, {"Images": 10, "Fonts": 5})
    table = TableData(0, Rect(0, 0, 10, 10), (("a", "b"),))
    return {
        "export": lambda: export.ExportDialog(n, 0, [], path, parent=window),
        "tables": lambda: export.TablePickerDialog([table], window),
        "reduce": lambda: optimize.ReduceSizeDialog(path, 1000, window),
        "audit": lambda: optimize.SpaceAuditDialog(usage, window),
        "redaction properties": lambda: redaction.RedactionPropertiesDialog(MarkStyle(), window),
        "mark text": lambda: redaction.MarkTextDialog(window),
        "apply redactions": lambda: redaction.ApplyRedactionsDialog(2, 1, window),
        "sanitize": lambda: redaction.SanitizeDialog(window),
        "compare": lambda: compare.CompareFilesDialog(path, window),
        "ocr": lambda: ocr.OcrDialog(n, 0, [], window),
        "batch ocr": lambda: ocr.BatchOcrDialog(window),
        "print": lambda: print_dialog.PrintDialog(view, window),
        "insert": lambda: pages.InsertPagesDialog(n, 0, window),
        "extract": lambda: pages.ExtractDialog(n, 0, [0], window),
        "split": lambda: pages.SplitDialog("x", Path.home(), window),
        "combine": lambda: pages.CombineDialog([path], window),
        "crop": lambda: pages.CropDialog(n, 0, [0], parent=window),
        "labels": lambda: pages.PageLabelsDialog([], n, window),
        "header": lambda: pages.HeaderFooterDialog(n, 0, [0], bates=True, parent=window),
        "watermark": lambda: pages.WatermarkDialog(n, 0, [0], window),
        "background": lambda: pages.BackgroundDialog(n, 0, [0], window),
        "security": lambda: security.SecurityDialog(parent=window),
        "properties": lambda: properties.PropertiesDialog(view.session, window),
        "pdfa": lambda: pdfa.PdfaReportDialog("PDF/A", "Fine.", [], parent=window),
        "recovery": lambda: recovery.RecoveryDialog([], window),
        "about": lambda: about.AboutDialog("engine", window),
    }


def test_migrated_dialogs_use_the_frame(window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("report"))
    for name, make in migrated(window, view).items():
        d = make()
        assert isinstance(d, FormDialog), name
        d.show()
        assert d.header_title.text(), name
        default = d.primary_button or d.cancel_button
        assert default is not None and default.isDefault(), name
        # expand every section so its fields are checked too
        for section in d.findChildren(Section):
            section.set_expanded(True)
        assert unnamed_widgets(d) == [], name
        for label in d.findChildren(QLabel):
            role = label.property("role")
            assert role in (None, "title", "muted", "caption", "error"), (name, role)
        d.close()


def test_collapsed_options_are_still_read(window: MainWindow, fixture_pdf) -> None:
    view = window.open_path(fixture_pdf("report"))
    reduce = optimize.ReduceSizeDialog(view.session.path, None, window)
    assert not reduce.advanced_section.is_expanded()
    reduce.linearize.setChecked(True)
    assert reduce.options().linearize and reduce.preset.currentData() == "custom"
    reduce.close()
    box = ocr.OcrDialog(1, 0, [], window)
    assert not box.box.advanced.is_expanded()
    box.box.deskew.setChecked(True)
    box.box.dpi.setValue(400)
    assert box.box.deskew.isChecked() and box.box.dpi.value() == 400
    box.close()
    printing = print_dialog.PrintDialog(view, window)
    assert not printing.advanced_section.is_expanded()
    printing.as_image.setChecked(True)
    assert printing.options().as_image
    printing.close()


def test_primary_button_validates_before_accepting(
    window: MainWindow, fixture_pdf, monkeypatch
) -> None:
    d = security.SecurityDialog(parent=window)
    d.show()
    assert d.primary_button is not None
    d.primary_button.click()  # nothing chosen yet
    assert d.result() != QDialog.DialogCode.Accepted and "open password" in d.error.text()
    d.require_open.setChecked(True)
    d.user_pw.setText("a")
    d.user_pw2.setText("a")
    d.primary_button.click()
    assert d.result() == QDialog.DialogCode.Accepted and d.error.text() == ""

    view = window.open_path(fixture_pdf("report"))
    printing = print_dialog.PrintDialog(view, window)
    printing.show()
    calls: list[str] = []
    monkeypatch.setattr(printing, "run", lambda printer: calls.append("run") or 0)
    monkeypatch.setattr(print_dialog.QMessageBox, "warning", lambda *a: calls.append("warned"))
    assert printing.primary_button is printing.print_button
    printing.range_pages.setChecked(True)
    printing.range_edit.setText("99")
    printing.print_button.click()  # invalid range: warns and stays open
    assert calls == ["warned"] and printing.isVisible()
    printing.range_edit.setText("1")
    printing.print_button.click()
    assert calls == ["warned", "run"] and printing.result() == QDialog.DialogCode.Accepted
