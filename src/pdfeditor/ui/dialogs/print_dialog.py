"""Print dialog: page range, scaling and options, with printer setup and preview."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrintPreviewDialog
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.jobs import CancelToken
from pdfeditor.services.pages import parse_page_ranges
from pdfeditor.ui.dialogs.base import FormDialog, add_row
from pdfeditor.ui.printing import PrintOptions, Scaling, print_pages
from pdfeditor.ui.style.tokens import METRICS
from pdfeditor.ui.view.document_view import DocumentView


class PrintDialog(FormDialog):
    def __init__(self, view: DocumentView, parent: QWidget | None = None) -> None:
        super().__init__(
            "Print",
            view.session.display_name,
            parent,
            window_title=f"Print — {view.session.display_name}",
            primary="Print",
        )
        self.view = view
        self.printer = QPrinter(QPrinter.PrinterMode.HighResolution)

        self.printer_label = QLabel(self)
        setup = QPushButton("Printer…", self)
        setup.setToolTip("Choose the printer, paper and copies")
        setup.clicked.connect(self.choose_printer)
        printer_row = QHBoxLayout()
        printer_row.addWidget(self.printer_label, 1)
        printer_row.addWidget(setup)

        self.all_pages = QRadioButton(f"All ({view.page_count} pages)")
        self.current_page = QRadioButton(f"Current page ({view.page_label(view.current_page)})")
        self.range_pages = QRadioButton("Pages:")
        self.range_edit = QLineEdit(self)
        self.range_edit.setPlaceholderText("e.g. 1-3, 5, 8-")
        self.range_edit.textEdited.connect(lambda _t: self.range_pages.setChecked(True))
        self.range_edit.setAccessibleName("Page range")
        self.all_pages.setChecked(True)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(METRICS.space(1))
        grid.addWidget(self.all_pages, 0, 0, 1, 2)
        grid.addWidget(self.current_page, 1, 0, 1, 2)
        grid.addWidget(self.range_pages, 2, 0)
        grid.addWidget(self.range_edit, 2, 1)

        self.scale_group = QButtonGroup(self)
        scale_layout = QVBoxLayout()
        scale_layout.setSpacing(METRICS.space(1))
        self.scale_buttons: dict[Scaling, QRadioButton] = {}
        for scaling, text in (
            (Scaling.FIT, "Fit to printable area"),
            (Scaling.SHRINK, "Shrink oversized pages"),
            (Scaling.ACTUAL, "Actual size"),
        ):
            button = QRadioButton(text, self)
            self.scale_group.addButton(button)
            self.scale_buttons[scaling] = button
            scale_layout.addWidget(button)
        self.scale_buttons[Scaling.SHRINK].setChecked(True)

        self.auto_rotate = QCheckBox("Auto-rotate and center pages", self)
        self.auto_rotate.setChecked(True)
        self.annotations = QCheckBox("Print comments and markups", self)
        self.annotations.setChecked(True)
        self.grayscale = QCheckBox("Print in grayscale", self)

        self.as_image = QCheckBox("Print as image", self)

        assert self.primary_button is not None
        self.print_button = self.primary_button
        preview = self.button_box.addButton("Preview…", QDialogButtonBox.ButtonRole.ActionRole)
        preview.clicked.connect(self.preview)

        form = self.add_form()
        add_row(form, "Printer:", printer_row)
        add_row(form, "Pages:", grid)
        add_row(form, "Page sizing:", scale_layout)
        self.options_section = self.add_section("Options")
        options = self.options_section.form()
        for box in (self.auto_rotate, self.annotations, self.grayscale):
            options.addRow("", box)
        self.advanced_section = self.add_section("Advanced", expanded=False)
        add_row(
            self.advanced_section.form(),
            "",
            self.as_image,
            "Slower, but helps with pages that print incorrectly.",
        )
        self._update_printer_label()

    def primary_clicked(self) -> None:
        self._print()

    def _update_printer_label(self) -> None:
        name = self.printer.printerName() or "(default printer)"
        if self.printer.outputFormat() == QPrinter.OutputFormat.PdfFormat:
            name = f"PDF file: {self.printer.outputFileName()}"
        self.printer_label.setText(name)

    def choose_printer(self) -> None:
        dialog = QPrintDialog(self.printer, self)
        dialog.setOption(QPrintDialog.PrintDialogOption.PrintPageRange, False)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._update_printer_label()

    def options(self) -> PrintOptions:
        """Current settings; raises ValueError for an invalid page range."""
        view = self.view
        if self.current_page.isChecked():
            pages = [view.current_page]
        elif self.range_pages.isChecked():
            pages = parse_page_ranges(self.range_edit.text(), view.page_count)
        else:
            pages = list(range(view.page_count))
        scaling = next(s for s, b in self.scale_buttons.items() if b.isChecked())
        return PrintOptions(
            pages=tuple(pages),
            scaling=scaling,
            auto_rotate=self.auto_rotate.isChecked(),
            annotations=self.annotations.isChecked(),
            grayscale=self.grayscale.isChecked(),
            as_image=self.as_image.isChecked(),
        )

    def _checked_options(self) -> PrintOptions | None:
        try:
            return self.options()
        except ValueError as exc:
            QMessageBox.warning(self, "Print", str(exc))
            self.range_edit.setFocus()
            return None

    def run(self, printer: QPrinter) -> int:
        """Print with a cancellable progress dialog; returns pages printed."""
        options = self._checked_options()
        if options is None:
            return 0
        token = CancelToken()
        progress = QProgressDialog("Printing…", "Cancel", 0, len(options.pages), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)
        progress.canceled.connect(token.cancel)

        def step(done: int, _total: int) -> None:
            progress.setValue(done)

        try:
            return print_pages(self.view.session, printer, options, token, step)
        finally:
            progress.close()

    def _print(self) -> None:
        if self._checked_options() is None:
            return
        try:
            self.run(self.printer)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Print", f"Printing failed: {exc}")
            return
        self.accept()

    def preview(self) -> None:
        options = self._checked_options()
        if options is None:
            return
        dialog = QPrintPreviewDialog(self.printer, self)
        dialog.paintRequested.connect(
            lambda printer: print_pages(self.view.session, printer, options)
        )
        dialog.exec()
        self._update_printer_label()
