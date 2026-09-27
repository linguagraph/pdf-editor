"""Export dialogs: format + options + pages + target, and the Excel table picker."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.model.text import TableData
from pdfeditor.services.export import TextFormat
from pdfeditor.services.export.images import ImageFormat, PageImageOptions
from pdfeditor.services.export.structure import StructureOptions
from pdfeditor.ui.dialogs.pages import PageRangeBox


class ExportFormat(Enum):
    WORD = ("Word Document", "docx")
    EXCEL = ("Excel Workbook (tables)", "xlsx")
    HTML = ("HTML Web Page", "html")
    MARKDOWN = ("Markdown", "md")
    TEXT = ("Plain Text", "txt")
    PNG = ("PNG Images", "png")
    JPEG = ("JPEG Images", "jpg")
    TIFF = ("TIFF Image", "tif")

    @property
    def label(self) -> str:
        return self.value[0]

    @property
    def suffix(self) -> str:
        return "." + self.value[1]

    @property
    def is_image(self) -> bool:
        return self in (ExportFormat.PNG, ExportFormat.JPEG, ExportFormat.TIFF)

    @property
    def text_format(self) -> TextFormat | None:
        return {
            ExportFormat.WORD: TextFormat.WORD,
            ExportFormat.HTML: TextFormat.HTML,
            ExportFormat.MARKDOWN: TextFormat.MARKDOWN,
            ExportFormat.TEXT: TextFormat.TEXT,
        }.get(self)

    @property
    def image_format(self) -> ImageFormat | None:
        return {
            ExportFormat.PNG: ImageFormat.PNG,
            ExportFormat.JPEG: ImageFormat.JPEG,
            ExportFormat.TIFF: ImageFormat.TIFF,
        }.get(self)


class ExportDialog(QDialog):
    def __init__(
        self,
        page_count: int,
        current: int,
        selected: list[int],
        source: Path | None,
        fmt: ExportFormat = ExportFormat.WORD,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export PDF")
        self.source = source
        self.format_combo = QComboBox(self)
        for f in ExportFormat:
            self.format_combo.addItem(f.label, f)
        self.target = QLineEdit(self)
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._browse)
        # text formats
        self.pictures = QCheckBox("Include pictures", self)
        self.pictures.setChecked(True)
        self.tables = QCheckBox("Detect tables", self)
        self.tables.setChecked(True)
        self.page_breaks = QCheckBox("Keep page breaks", self)
        self.page_breaks.setChecked(True)
        # image formats
        self.dpi = QSpinBox(self)
        self.dpi.setRange(36, 1200)
        self.dpi.setValue(150)
        self.dpi.setSuffix(" dpi")
        self.grayscale = QCheckBox("Grayscale", self)
        self.quality = QSpinBox(self)
        self.quality.setRange(10, 100)
        self.quality.setValue(90)
        self.multipage = QCheckBox("All pages in one TIFF file", self)
        self.multipage.setChecked(True)
        self.annotations = QCheckBox("Include comments", self)
        self.annotations.setChecked(True)
        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        self.range = PageRangeBox(page_count, current, selected, self)

        target_row = QHBoxLayout()
        target_row.addWidget(self.target, 1)
        target_row.addWidget(browse)
        self.form = QFormLayout()
        self.form.addRow("Format:", self.format_combo)
        self.form.addRow("Save as:", target_row)
        self.form.addRow("", self.pictures)
        self.form.addRow("", self.tables)
        self.form.addRow("", self.page_breaks)
        self.form.addRow("Resolution:", self.dpi)
        self.form.addRow("", self.grayscale)
        self.form.addRow("JPEG quality:", self.quality)
        self.form.addRow("", self.multipage)
        self.form.addRow("", self.annotations)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(self.form)
        layout.addWidget(self.hint)
        layout.addWidget(self.range)
        layout.addWidget(buttons)
        self.format_combo.currentIndexChanged.connect(lambda _i: self._format_changed())
        self.set_format(fmt)

    def format(self) -> ExportFormat:
        value = self.format_combo.currentData()
        assert isinstance(value, ExportFormat)
        return value

    def set_format(self, fmt: ExportFormat) -> None:
        self.format_combo.setCurrentIndex(list(ExportFormat).index(fmt))
        self._format_changed()

    def _format_changed(self) -> None:
        fmt = self.format()
        reflow = fmt.text_format not in (None, TextFormat.TEXT)
        for widget in (self.pictures, self.tables, self.page_breaks):
            self.form.setRowVisible(widget, reflow)
        image_rows: tuple[QWidget, ...] = (self.dpi, self.grayscale, self.annotations)
        for row in image_rows:
            self.form.setRowVisible(row, fmt.is_image)
        self.form.setRowVisible(self.quality, fmt is ExportFormat.JPEG)
        self.form.setRowVisible(self.multipage, fmt is ExportFormat.TIFF)
        self.hint.setText(
            {
                ExportFormat.WORD: "Text is reflowed into editable paragraphs; headings, tables "
                "and pictures are kept. Complex layouts may need touching up.",
                ExportFormat.EXCEL: "Tables are detected on the chosen pages; you pick which "
                "ones to export, one sheet each.",
                ExportFormat.MARKDOWN: "Pictures are saved in a folder next to the file.",
            }.get(
                fmt,
                "Several pages are written as numbered files."
                if fmt in (ExportFormat.PNG, ExportFormat.JPEG)
                else "",
            )
        )
        current = self.target.text()
        base = Path(current) if current else self._default_target()
        self.target.setText(str(base.with_suffix(fmt.suffix)))
        self.adjustSize()

    def _default_target(self) -> Path:
        if self.source is not None:
            return self.source.with_suffix("")
        return Path.home() / "export"

    def _browse(self) -> None:
        fmt = self.format()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export As", self.target.text(), f"{fmt.label} (*{fmt.suffix})"
        )
        if path:
            self.target.setText(path)

    def target_path(self) -> Path:
        return Path(self.target.text()).with_suffix(self.format().suffix)

    def structure_options(self) -> StructureOptions:
        return StructureOptions(
            tables=self.tables.isChecked(),
            pictures=self.pictures.isChecked(),
            page_breaks=self.page_breaks.isChecked(),
        )

    def image_options(self) -> PageImageOptions:
        fmt = self.format().image_format
        assert fmt is not None
        return PageImageOptions(
            format=fmt,
            dpi=self.dpi.value(),
            grayscale=self.grayscale.isChecked(),
            jpeg_quality=self.quality.value(),
            annotations=self.annotations.isChecked(),
            multipage_tiff=self.multipage.isChecked(),
        )


class TablePickerDialog(QDialog):
    """Choose which detected tables go to the workbook (all checked by default)."""

    def __init__(self, tables: list[TableData], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Tables to Excel")
        self.resize(560, 380)
        self.tables = tables
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Table", "Size", "First row"])
        for i, t in enumerate(tables):
            rows, cols = t.size
            first = " | ".join((c or "") for c in t.rows[0]) if t.rows else ""
            item = QTreeWidgetItem(
                [f"Page {t.page_index + 1}", f"{rows} rows, {cols} columns", first]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)
            self.tree.addTopLevelItem(item)
        self.tree.resizeColumnToContents(0)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{len(tables)} table(s) found. Choose the ones to export:"))
        layout.addWidget(self.tree)
        layout.addWidget(buttons)

    def chosen(self) -> list[TableData]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item is not None and item.checkState(0) == Qt.CheckState.Checked:
                out.append(self.tables[int(item.data(0, Qt.ItemDataRole.UserRole))])
        return out
