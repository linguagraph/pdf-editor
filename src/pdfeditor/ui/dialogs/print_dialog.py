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
    QGroupBox,
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
from pdfeditor.ui.printing import PrintOptions, Scaling, print_pages
from pdfeditor.ui.view.document_view import DocumentView


class PrintDialog(QDialog):
    def __init__(self, view: DocumentView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view = view
        self.setWindowTitle(f"Print — {view.session.display_name}")
        self.printer = QPrinter(QPrinter.PrinterMode.HighResolution)

        self.printer_label = QLabel(self)
        setup = QPushButton("Printer…", self)
        setup.clicked.connect(self.choose_printer)
        printer_row = QHBoxLayout()
        printer_row.addWidget(QLabel("Printer:"))
        printer_row.addWidget(self.printer_label, 1)
        printer_row.addWidget(setup)

        pages_box = QGroupBox("Pages to print", self)
        self.all_pages = QRadioButton(f"All ({view.page_count} pages)")
        self.current_page = QRadioButton(f"Current page ({view.page_label(view.current_page)})")
        self.range_pages = QRadioButton("Pages:")
        self.range_edit = QLineEdit(self)
        self.range_edit.setPlaceholderText("e.g. 1-3, 5, 8-")
        self.range_edit.textEdited.connect(lambda _t: self.range_pages.setChecked(True))
        self.all_pages.setChecked(True)
        grid = QGridLayout(pages_box)
        grid.addWidget(self.all_pages, 0, 0, 1, 2)
        grid.addWidget(self.current_page, 1, 0, 1, 2)
        grid.addWidget(self.range_pages, 2, 0)
        grid.addWidget(self.range_edit, 2, 1)

        scale_box = QGroupBox("Page sizing", self)
        self.scale_group = QButtonGroup(self)
        scale_layout = QVBoxLayout(scale_box)
        self.scale_buttons: dict[Scaling, QRadioButton] = {}
        for scaling, text in (
            (Scaling.FIT, "Fit to printable area"),
            (Scaling.SHRINK, "Shrink oversized pages"),
            (Scaling.ACTUAL, "Actual size"),
        ):
            button = QRadioButton(text, scale_box)
            self.scale_group.addButton(button)
            self.scale_buttons[scaling] = button
            scale_layout.addWidget(button)
        self.scale_buttons[Scaling.SHRINK].setChecked(True)

        self.auto_rotate = QCheckBox("Auto-rotate and center pages", self)
        self.auto_rotate.setChecked(True)
        self.annotations = QCheckBox("Print comments and markups", self)
        self.annotations.setChecked(True)
        self.grayscale = QCheckBox("Print in grayscale", self)

        buttons = QDialogButtonBox(self)
        self.print_button = buttons.addButton("Print", QDialogButtonBox.ButtonRole.AcceptRole)
        preview = buttons.addButton("Preview…", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._print)
        buttons.rejected.connect(self.reject)
        preview.clicked.connect(self.preview)

        layout = QVBoxLayout(self)
        layout.addLayout(printer_row)
        layout.addWidget(pages_box)
        layout.addWidget(scale_box)
        layout.addWidget(self.auto_rotate)
        layout.addWidget(self.annotations)
        layout.addWidget(self.grayscale)
        self.as_image = QCheckBox("Print as image (for pages that print incorrectly)", self)
        layout.addWidget(self.as_image)
        layout.addWidget(buttons)
        self._update_printer_label()

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
