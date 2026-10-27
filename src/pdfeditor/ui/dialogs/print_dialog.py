"""Print dialog: settings on the left, a live preview of the sheets on the right."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QPageLayout
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.jobs import CancelToken
from pdfeditor.services.pages import parse_page_ranges
from pdfeditor.ui import printing
from pdfeditor.ui.dialogs.base import FormDialog, Section, add_row, caption, form_layout, set_role
from pdfeditor.ui.dialogs.print_preview import PrintPreviewPane
from pdfeditor.ui.printing import PrintOptions, Scaling, print_pages
from pdfeditor.ui.style.tokens import METRICS
from pdfeditor.ui.view.document_view import DocumentView

RANGE_DEBOUNCE_MS = 200
AUTO, PORTRAIT, LANDSCAPE = "auto", "portrait", "landscape"


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
        self.setMinimumSize(METRICS.space(180), METRICS.space(130))
        self.resize(METRICS.space(225), METRICS.space(160))
        self.printer: QPrinter = printing.printer_for(None)

        settings = QWidget()
        column = QVBoxLayout(settings)
        column.setContentsMargins(0, 0, METRICS.space(2), 0)
        column.setSpacing(METRICS.space(3))
        form = form_layout()
        column.addLayout(form)

        self.printer_combo = QComboBox(settings)
        self.printer_combo.setToolTip("The printer to print on, or save a PDF file instead")
        self.properties_button = QPushButton("Properties…", settings)
        self.properties_button.setToolTip("Printer properties: paper size, quality, trays")
        self.properties_button.clicked.connect(self.choose_printer)
        printer_row = QHBoxLayout()
        printer_row.setSpacing(METRICS.space(2))
        printer_row.addWidget(self.printer_combo, 1)
        printer_row.addWidget(self.properties_button)
        add_row(form, "Printer:", printer_row)
        self._fill_printers()

        self.copies = QSpinBox(settings)
        self.copies.setRange(1, 999)
        self.copies.setToolTip("Number of copies")
        self.collate = QCheckBox("Collate", settings)
        self.collate.setChecked(True)
        self.collate.setEnabled(False)
        self.collate.setToolTip("Print complete copies one after another (1, 2, 1, 2)")
        copies_row = QHBoxLayout()
        copies_row.setSpacing(METRICS.space(3))
        copies_row.addWidget(self.copies)
        copies_row.addWidget(self.collate)
        copies_row.addStretch(1)
        add_row(form, "Copies:", copies_row)

        self.all_pages = QRadioButton(f"All ({view.page_count} pages)", settings)
        self.current_page = QRadioButton(
            f"Current page ({view.page_label(view.current_page)})", settings
        )
        self.range_pages = QRadioButton("Pages:", settings)
        self.range_edit = QLineEdit(settings)
        self.range_edit.setPlaceholderText("e.g. 1-3, 5, 8-")
        self.range_edit.setAccessibleName("Page range")
        self.all_pages.setChecked(True)
        self.range_error = caption("", settings)
        set_role(self.range_error, "error")
        self.range_error.hide()
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(METRICS.space(1))
        grid.addWidget(self.all_pages, 0, 0, 1, 2)
        grid.addWidget(self.current_page, 1, 0, 1, 2)
        grid.addWidget(self.range_pages, 2, 0)
        grid.addWidget(self.range_edit, 2, 1)
        grid.addWidget(self.range_error, 3, 0, 1, 2)
        add_row(form, "Pages:", grid)

        self.scale_group = QButtonGroup(self)
        scale_layout = QVBoxLayout()
        scale_layout.setSpacing(METRICS.space(1))
        self.scale_buttons: dict[Scaling, QRadioButton] = {}
        for scaling, text in (
            (Scaling.FIT, "Fit to printable area"),
            (Scaling.SHRINK, "Shrink oversized pages"),
            (Scaling.ACTUAL, "Actual size"),
        ):
            button = QRadioButton(text, settings)
            self.scale_group.addButton(button)
            self.scale_buttons[scaling] = button
            scale_layout.addWidget(button)
        self.scale_buttons[Scaling.SHRINK].setChecked(True)
        add_row(form, "Page sizing:", scale_layout)

        self.orientation = QComboBox(settings)
        for text, data in (
            ("Auto (portrait/landscape)", AUTO),
            ("Portrait", PORTRAIT),
            ("Landscape", LANDSCAPE),
        ):
            self.orientation.addItem(text, data)
        self.orientation.setToolTip("Auto turns each page to match the paper")
        add_row(form, "Orientation:", self.orientation)

        self.annotations = QCheckBox("Print comments and markups", settings)
        self.annotations.setChecked(True)
        self.grayscale = QCheckBox("Print in grayscale", settings)
        self.as_image = QCheckBox("Print as image", settings)

        self.options_section = Section("Options", settings)
        column.addWidget(self.options_section)
        options = self.options_section.form()
        for box in (self.annotations, self.grayscale):
            options.addRow("", box)
        self.advanced_section = Section("Advanced", settings, expanded=False)
        column.addWidget(self.advanced_section)
        add_row(
            self.advanced_section.form(),
            "",
            self.as_image,
            "Slower, but helps with pages that print incorrectly.",
        )
        column.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(settings)
        scroll.setFixedWidth(METRICS.space(104))
        scroll.setAccessibleName("Print settings")
        self.settings_area = scroll

        self.preview = PrintPreviewPane(view, self)
        body = QHBoxLayout()
        body.setSpacing(METRICS.space(5))
        body.addWidget(scroll)
        body.addWidget(self.preview, 1)
        self.content.addLayout(body, 1)

        assert self.primary_button is not None
        self.print_button = self.primary_button

        self._range_timer = QTimer(self)
        self._range_timer.setSingleShot(True)
        self._range_timer.setInterval(RANGE_DEBOUNCE_MS)
        self._range_timer.timeout.connect(self._refresh_preview)
        self.range_edit.textEdited.connect(self._range_typed)
        self.range_edit.textChanged.connect(self._range_changed)
        for radio in (
            self.all_pages,
            self.current_page,
            self.range_pages,
            *self.scale_buttons.values(),
        ):
            radio.toggled.connect(self._radio_toggled)
        for box in (self.annotations, self.grayscale, self.as_image):
            box.toggled.connect(self._refresh_preview)
        self.collate.toggled.connect(self._refresh_preview)
        self.copies.valueChanged.connect(self._copies_changed)
        self.orientation.currentIndexChanged.connect(self._refresh_preview)
        self.printer_combo.currentIndexChanged.connect(self._printer_changed)
        self._printer_changed()

    # -- printers ---------------------------------------------------------------------------
    def _fill_printers(self) -> None:
        names, default = printing.available_printers()
        with QSignalBlocker(self.printer_combo):
            for name in names:
                self.printer_combo.addItem(name, name)
            if names:
                self.printer_combo.insertSeparator(self.printer_combo.count())
            self.printer_combo.addItem("Save as PDF", None)
            if names:
                self.printer_combo.setCurrentIndex(self.printer_combo.findData(default or names[0]))

    def _printer_changed(self, _index: int = 0) -> None:
        name = self.printer_combo.currentData()
        self.printer = printing.printer_for(name)
        self.properties_button.setEnabled(name is not None)
        self._refresh_preview()

    def choose_printer(self) -> None:
        """The system's printer properties (paper, quality, trays, copies)."""
        self._configure(self.printer)
        dialog = QPrintDialog(self.printer, self)
        dialog.setOption(QPrintDialog.PrintDialogOption.PrintPageRange, False)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        with QSignalBlocker(self.copies), QSignalBlocker(self.collate):
            self.copies.setValue(max(1, self.printer.copyCount()))
            self.collate.setChecked(self.printer.collateCopies())
        self.collate.setEnabled(self.copies.value() > 1)
        landscape = self.printer.pageLayout().orientation() == QPageLayout.Orientation.Landscape
        wanted = LANDSCAPE if landscape else None
        if wanted is None and self.orientation.currentData() == LANDSCAPE:
            wanted = PORTRAIT
        if wanted is not None:
            with QSignalBlocker(self.orientation):
                self.orientation.setCurrentIndex(self.orientation.findData(wanted))
        self._refresh_preview()

    # -- settings ---------------------------------------------------------------------------
    @property
    def auto_rotate(self) -> bool:
        return self.orientation.currentData() == AUTO

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
            auto_rotate=self.auto_rotate,
            annotations=self.annotations.isChecked(),
            grayscale=self.grayscale.isChecked(),
            as_image=self.as_image.isChecked(),
            copies=self.copies.value(),
            collate=self.collate.isChecked(),
        )

    def page_layout(self) -> QPageLayout:
        """The printer's page setup, turned to the chosen orientation (Auto keeps the paper's)."""
        layout = QPageLayout(self.printer.pageLayout())
        explicit = {
            PORTRAIT: QPageLayout.Orientation.Portrait,
            LANDSCAPE: QPageLayout.Orientation.Landscape,
        }.get(self.orientation.currentData())
        if explicit is not None:
            layout.setOrientation(explicit)
        return layout

    def _configure(self, printer: QPrinter) -> None:
        explicit = {
            PORTRAIT: QPageLayout.Orientation.Portrait,
            LANDSCAPE: QPageLayout.Orientation.Landscape,
        }.get(self.orientation.currentData())
        if explicit is not None:
            printer.setPageOrientation(explicit)

    # -- preview ----------------------------------------------------------------------------
    def _range_typed(self, _text: str) -> None:
        if not self.range_pages.isChecked():
            with QSignalBlocker(self.range_pages):  # the text change refreshes, debounced
                self.range_pages.setChecked(True)

    def _range_changed(self, _text: str) -> None:
        self._range_timer.start()

    def _radio_toggled(self, checked: bool) -> None:
        if checked:
            self._refresh_preview()

    def _copies_changed(self, value: int) -> None:
        self.collate.setEnabled(value > 1)
        self._refresh_preview()

    def _refresh_preview(self, *_args: object) -> None:
        self._range_timer.stop()
        try:
            options = self.options()
        except ValueError:
            self.range_error.setText("Enter a valid page range, for example 1-3, 5")
            self.range_error.show()
            self.preview.show_message("Enter a valid page range", error=True)
            return
        self.range_error.hide()
        self.preview.set_job(self.view.session, options.pages, options, self.page_layout())

    def preview_settled(self) -> bool:
        """True when no refresh is waiting on the range debounce or the renderer."""
        return not self._range_timer.isActive() and not self.preview.is_pending()

    # -- printing ---------------------------------------------------------------------------
    def primary_clicked(self) -> None:
        self._print()

    def _checked_options(self) -> PrintOptions | None:
        try:
            return self.options()
        except ValueError as exc:
            QMessageBox.warning(self, "Print", str(exc))
            self.range_edit.setFocus()
            return None

    def run(self, printer: QPrinter) -> int:
        """Print with a cancellable progress dialog; returns sheets printed."""
        options = self._checked_options()
        if options is None:
            return 0
        self._configure(printer)
        token = CancelToken()
        progress = QProgressDialog("Printing…", "Cancel", 0, len(options.pages), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)
        progress.canceled.connect(token.cancel)

        def step(done: int, total: int) -> None:
            progress.setMaximum(total)
            progress.setValue(done)

        try:
            return print_pages(self.view.session, printer, options, token, step)
        finally:
            progress.close()

    def _print(self) -> None:
        if self._checked_options() is None:
            return
        printer = self.printer
        if self.printer_combo.currentData() is None:
            stem = Path(self.view.session.display_name).stem or "document"
            path, _filter = QFileDialog.getSaveFileName(
                self, "Save as PDF", f"{stem}.pdf", "PDF files (*.pdf)"
            )
            if not path:
                return
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            printer.setOutputFileName(path)
        self.preview.renderer.shutdown()  # printing owns the engine from here
        try:
            self.run(printer)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Print", f"Printing failed: {exc}")
            self.preview.renderer.restart()
            self._refresh_preview()
            return
        self.accept()

    def done(self, result: int) -> None:
        self._range_timer.stop()
        self.preview.renderer.shutdown()
        super().done(result)
