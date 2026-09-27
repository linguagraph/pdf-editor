"""Dialogs for Organize commands: page ranges, insert, extract, split, combine, crop, labels,
header/footer (+ Bates), watermark and background."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.model.color import Color
from pdfeditor.model.pages import FONTS, LabelStyle, PageLabelRule
from pdfeditor.services.assembly import IMAGE_SUFFIXES, MergeSource, SplitMode
from pdfeditor.services.pages import format_page_ranges, parse_page_ranges
from pdfeditor.services.stamping import TOKENS_HELP, HeaderFooter, Slot, Watermark

PDF_OR_IMAGES = (
    "PDF and images (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp);;All files (*)"
)


def _buttons(dialog: QDialog, ok_text: str = "OK") -> QDialogButtonBox:
    box = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dialog
    )
    box.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    return box


class PageRangeBox(QGroupBox):
    """All / current / selected / custom range, resolving to 0-based page indices."""

    def __init__(
        self, page_count: int, current: int, selected: list[int], parent: QWidget | None = None
    ) -> None:
        super().__init__("Pages", parent)
        self.page_count = page_count
        self.current_index = current
        self.selected = selected
        self.all = QRadioButton(f"All pages ({page_count})")
        self.current = QRadioButton(f"Current page ({current + 1})")
        self.sel = QRadioButton(f"Selected pages ({format_page_ranges(selected)})")
        self.custom = QRadioButton("Pages:")
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("e.g. 1-3, 5, 8-")
        self.edit.textEdited.connect(lambda _t: self.custom.setChecked(True))
        grid = QGridLayout(self)
        grid.addWidget(self.all, 0, 0, 1, 2)
        grid.addWidget(self.current, 1, 0, 1, 2)
        grid.addWidget(self.sel, 2, 0, 1, 2)
        grid.addWidget(self.custom, 3, 0)
        grid.addWidget(self.edit, 3, 1)
        self.sel.setVisible(len(selected) > 1)
        (self.sel if len(selected) > 1 else self.all).setChecked(True)

    def pages(self) -> list[int]:
        """Raises ValueError for an invalid custom range."""
        if self.current.isChecked():
            return [self.current_index]
        if self.sel.isChecked():
            return list(self.selected)
        if self.custom.isChecked():
            return parse_page_ranges(self.edit.text(), self.page_count)
        return list(range(self.page_count))


def checked_pages(dialog: QWidget, box: PageRangeBox) -> list[int] | None:
    try:
        return box.pages()
    except ValueError as exc:
        QMessageBox.warning(dialog, dialog.windowTitle(), str(exc))
        return None


class ColorButton(QPushButton):
    def __init__(self, color: Color, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.color = color
        self.setFixedWidth(60)
        self.clicked.connect(self._pick)
        self._paint()

    def _paint(self) -> None:
        self.setStyleSheet(f"background-color: {self.color.to_hex()}")

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(QColor.fromRgbF(*self.color.rgb()), self)
        if chosen.isValid():
            self.set(Color(chosen.redF(), chosen.greenF(), chosen.blueF()))

    def set(self, color: Color) -> None:
        self.color = color
        self._paint()


# -- insert -------------------------------------------------------------------------------------
class InsertPagesDialog(QDialog):
    """Where to insert and what: a blank page, pages from a file, or the clipboard image."""

    def __init__(self, page_count: int, current: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Insert Pages")
        self.blank = QRadioButton("Blank page")
        self.file = QRadioButton("From file:")
        self.clipboard = QRadioButton("Image from clipboard")
        self.blank.setChecked(True)
        self.path_edit = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.path_edit.textEdited.connect(lambda _t: self.file.setChecked(True))
        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText("all pages")
        self.where = QComboBox()
        self.where.addItems(["After", "Before"])
        self.page = QSpinBox()
        self.page.setRange(1, page_count)
        self.page.setValue(current + 1)
        file_row = QHBoxLayout()
        file_row.addWidget(self.path_edit, 1)
        file_row.addWidget(browse)
        form = QFormLayout()
        form.addRow(self.blank)
        form.addRow(self.file, file_row)
        form.addRow("Pages of that file:", self.range_edit)
        form.addRow(self.clipboard)
        where = QHBoxLayout()
        where.addWidget(self.where)
        where.addWidget(QLabel("page"))
        where.addWidget(self.page)
        where.addStretch()
        form.addRow("Insert:", where)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(_buttons(self, "Insert"))

    def _browse(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Insert From File", "", PDF_OR_IMAGES)
        if chosen:
            self.path_edit.setText(chosen)
            self.file.setChecked(True)

    def position(self) -> int:
        """0-based index to insert before."""
        return self.page.value() if self.where.currentText() == "After" else self.page.value() - 1


# -- extract ------------------------------------------------------------------------------------
class ExtractDialog(QDialog):
    def __init__(
        self, page_count: int, current: int, selected: list[int], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Extract Pages")
        self.range = PageRangeBox(page_count, current, selected, self)
        if len(selected) <= 1:
            self.range.current.setChecked(True)
        self.delete_after = QCheckBox("Delete pages after extracting")
        self.separate = QCheckBox("Extract each page as a separate file")
        layout = QVBoxLayout(self)
        for w in (self.range, self.delete_after, self.separate):
            layout.addWidget(w)
        layout.addWidget(_buttons(self, "Extract"))


# -- split --------------------------------------------------------------------------------------
class SplitDialog(QDialog):
    def __init__(self, stem: str, folder: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Split Document")
        self.group = QButtonGroup(self)
        self.by_pages = QRadioButton("Every")
        self.by_bookmarks = QRadioButton("At each top-level bookmark")
        self.by_size = QRadioButton("Maximum file size")
        self.by_ranges = QRadioButton("Page ranges (one file per range, separated by ;)")
        for b in (self.by_pages, self.by_bookmarks, self.by_size, self.by_ranges):
            self.group.addButton(b)
        self.by_pages.setChecked(True)
        self.pages = QSpinBox()
        self.pages.setRange(1, 100000)
        self.pages.setValue(1)
        self.size_mb = QDoubleSpinBox()
        self.size_mb.setRange(0.05, 10000)
        self.size_mb.setValue(10)
        self.size_mb.setSuffix(" MB")
        self.ranges = QLineEdit()
        self.ranges.setPlaceholderText("e.g. 1-3; 4-10; 11-")
        self.folder = QLineEdit(str(folder))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.stem = QLineEdit(stem)
        grid = QGridLayout()
        grid.addWidget(self.by_pages, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.pages)
        row.addWidget(QLabel("pages"))
        row.addStretch()
        grid.addLayout(row, 0, 1)
        grid.addWidget(self.by_bookmarks, 1, 0, 1, 2)
        grid.addWidget(self.by_size, 2, 0)
        grid.addWidget(self.size_mb, 2, 1)
        grid.addWidget(self.by_ranges, 3, 0, 1, 2)
        grid.addWidget(self.ranges, 4, 0, 1, 2)
        out = QFormLayout()
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        out.addRow("Output folder:", folder_row)
        out.addRow("File name prefix:", self.stem)
        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addLayout(out)
        layout.addWidget(_buttons(self, "Split"))

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Output Folder", self.folder.text())
        if chosen:
            self.folder.setText(chosen)

    def mode(self) -> SplitMode:
        if self.by_bookmarks.isChecked():
            return SplitMode.TOP_BOOKMARKS
        if self.by_size.isChecked():
            return SplitMode.MAX_SIZE
        if self.by_ranges.isChecked():
            return SplitMode.RANGES
        return SplitMode.EVERY_N_PAGES

    def range_groups(self, page_count: int) -> list[list[int]]:
        """Raises ValueError for a malformed range."""
        return [
            parse_page_ranges(part, page_count)
            for part in self.ranges.text().split(";")
            if part.strip()
        ]


# -- combine ------------------------------------------------------------------------------------
class CombineDialog(QDialog):
    """Pick and order files (PDFs and images) to combine into a new document."""

    def __init__(self, initial: list[Path] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Combine Files")
        self.resize(560, 380)
        self.list = QListWidget(self)
        self.list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list.setToolTip("Drag to reorder. Double-click a PDF to choose its pages.")
        self.list.itemDoubleClicked.connect(self._edit_range)
        add = QPushButton("Add Files…")
        add.clicked.connect(self._add)
        remove = QPushButton("Remove")
        remove.clicked.connect(
            lambda: [self.list.takeItem(self.list.row(i)) for i in self.list.selectedItems()]
        )
        up = QPushButton("Up")
        up.clicked.connect(lambda: self._move(-1))
        down = QPushButton("Down")
        down.clicked.connect(lambda: self._move(1))
        self.bookmarks = QCheckBox("Add a bookmark for each file")
        self.bookmarks.setChecked(True)
        side = QVBoxLayout()
        for b in (add, remove, up, down):
            side.addWidget(b)
        side.addStretch()
        top = QHBoxLayout()
        top.addWidget(self.list, 1)
        top.addLayout(side)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.bookmarks)
        layout.addWidget(_buttons(self, "Combine"))
        for path in initial or []:
            self.add_path(path)

    def add_path(self, path: Path, pages: str = "") -> None:
        item = QListWidgetItem(self._label(path, pages))
        item.setData(Qt.ItemDataRole.UserRole, (str(path), pages))
        self.list.addItem(item)

    @staticmethod
    def _label(path: Path, pages: str) -> str:
        return f"{path.name}  —  {'pages ' + pages if pages else 'all pages'}"

    def _add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add Files", "", PDF_OR_IMAGES)
        for p in paths:
            self.add_path(Path(p))

    def _move(self, delta: int) -> None:
        row = self.list.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.list.count():
            return
        item = self.list.takeItem(row)
        self.list.insertItem(target, item)
        self.list.setCurrentRow(target)

    def _edit_range(self, item: QListWidgetItem) -> None:
        from PySide6.QtWidgets import QInputDialog

        path, pages = item.data(Qt.ItemDataRole.UserRole)
        if Path(path).suffix.lower() in IMAGE_SUFFIXES:
            return
        text, ok = QInputDialog.getText(
            self, "Pages", "Pages to include (empty = all):", text=pages
        )
        if ok:
            item.setData(Qt.ItemDataRole.UserRole, (path, text.strip()))
            item.setText(self._label(Path(path), text.strip()))

    def sources(self, page_counts: dict[str, int] | None = None) -> list[MergeSource]:
        """Raises ValueError for a malformed page range (needs each PDF's page count)."""
        out = []
        for i in range(self.list.count()):
            path, pages = self.list.item(i).data(Qt.ItemDataRole.UserRole)
            chosen = None
            if pages and page_counts is not None and path in page_counts:
                chosen = parse_page_ranges(pages, page_counts[path])
            out.append(MergeSource(Path(path), chosen))
        return out


# -- crop ---------------------------------------------------------------------------------------
class CropDialog(QDialog):
    def __init__(
        self,
        page_count: int,
        current: int,
        selected: list[int],
        margins: tuple[float, float, float, float] = (0, 0, 0, 0),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Crop Pages")
        self.spins = []
        form = QFormLayout()
        for label, value in zip(("Left", "Top", "Right", "Bottom"), margins, strict=True):
            spin = QDoubleSpinBox()
            spin.setRange(0, 5000)
            spin.setSuffix(" pt")
            spin.setValue(value)
            self.spins.append(spin)
            form.addRow(f"{label}:", spin)
        self.trim = QCheckBox("Remove white margins automatically (ignores the values above)")
        self.range = PageRangeBox(page_count, current, selected, self)
        if len(selected) <= 1 and any(margins):
            self.range.current.setChecked(True)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.trim)
        layout.addWidget(self.range)
        layout.addWidget(_buttons(self, "Crop"))

    def margins(self) -> tuple[float, float, float, float]:
        left, top, right, bottom = (s.value() for s in self.spins)
        return left, top, right, bottom


# -- page labels --------------------------------------------------------------------------------
_STYLE_NAMES = [
    (LabelStyle.DECIMAL, "1, 2, 3"),
    (LabelStyle.ROMAN_LOWER, "i, ii, iii"),
    (LabelStyle.ROMAN_UPPER, "I, II, III"),
    (LabelStyle.ALPHA_LOWER, "a, b, c"),
    (LabelStyle.ALPHA_UPPER, "A, B, C"),
    (LabelStyle.NONE, "none (prefix only)"),
]


class PageLabelsDialog(QDialog):
    def __init__(
        self, rules: list[PageLabelRule], page_count: int, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Page Labels")
        self.page_count = page_count
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["From page", "Style", "Prefix", "Start at"])
        self.table.horizontalHeader().setStretchLastSection(True)
        add = QPushButton("Add Range")
        add.clicked.connect(lambda: self.add_rule(PageLabelRule(0)))
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: self.table.removeRow(self.table.currentRow()))
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("Labels replace page numbers in the navigator, thumbnails and printing dialogs.")
        )
        layout.addWidget(self.table)
        layout.addLayout(row)
        layout.addWidget(_buttons(self))
        for rule in rules:
            self.add_rule(rule)
        self.resize(520, 300)

    def add_rule(self, rule: PageLabelRule) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        start = QSpinBox()
        start.setRange(1, self.page_count)
        start.setValue(rule.start + 1)
        style = QComboBox()
        for s, name in _STYLE_NAMES:
            style.addItem(name, s)
        style.setCurrentIndex(max(0, style.findData(rule.style)))
        first = QSpinBox()
        first.setRange(1, 100000)
        first.setValue(rule.first)
        self.table.setCellWidget(r, 0, start)
        self.table.setCellWidget(r, 1, style)
        self.table.setItem(r, 2, QTableWidgetItem(rule.prefix))
        self.table.setCellWidget(r, 3, first)

    def rules(self) -> list[PageLabelRule]:
        out: dict[int, PageLabelRule] = {}
        for r in range(self.table.rowCount()):
            start = cast(QSpinBox, self.table.cellWidget(r, 0)).value() - 1
            style = cast(QComboBox, self.table.cellWidget(r, 1)).currentData()
            prefix_item = self.table.item(r, 2)
            prefix = prefix_item.text() if prefix_item is not None else ""
            first = cast(QSpinBox, self.table.cellWidget(r, 3)).value()
            out[start] = PageLabelRule(start, style, prefix, first)
        return [out[k] for k in sorted(out)]


# -- header / footer / Bates --------------------------------------------------------------------
class HeaderFooterDialog(QDialog):
    def __init__(
        self,
        page_count: int,
        current: int,
        selected: list[int],
        bates: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bates Numbering" if bates else "Header & Footer")
        self.slots: dict[Slot, QLineEdit] = {}
        grid = QGridLayout()
        grid.addWidget(QLabel("Left"), 0, 1)
        grid.addWidget(QLabel("Center"), 0, 2)
        grid.addWidget(QLabel("Right"), 0, 3)
        for row, prefix in ((1, "header"), (2, "footer")):
            grid.addWidget(QLabel(prefix.capitalize() + ":"), row, 0)
            for col, side in ((1, "left"), (2, "center"), (3, "right")):
                edit = QLineEdit()
                slot = Slot(f"{prefix}_{side}")
                self.slots[slot] = edit
                grid.addWidget(edit, row, col)
        if bates:
            self.slots[Slot.FOOTER_RIGHT].setText("<<bates>>")
        help_label = QLabel(f"Fields: {TOKENS_HELP}")
        help_label.setWordWrap(True)
        self.font_box = QComboBox()
        self.font_box.addItems(FONTS)
        self.size_box = QDoubleSpinBox()
        self.size_box.setRange(4, 72)
        self.size_box.setValue(9)
        self.color_button = ColorButton(Color(0, 0, 0))
        self.margin_x = QDoubleSpinBox()
        self.margin_x.setRange(0, 500)
        self.margin_x.setValue(36)
        self.margin_y = QDoubleSpinBox()
        self.margin_y.setRange(0, 500)
        self.margin_y.setValue(24)
        self.bates_prefix = QLineEdit()
        self.bates_start = QSpinBox()
        self.bates_start.setRange(0, 10**9)
        self.bates_start.setValue(1)
        self.bates_digits = QSpinBox()
        self.bates_digits.setRange(1, 15)
        self.bates_digits.setValue(6)
        self.bates_suffix = QLineEdit()
        style = QFormLayout()
        style.addRow("Font:", self.font_box)
        style.addRow("Size:", self.size_box)
        style.addRow("Color:", self.color_button)
        style.addRow("Side margin:", self.margin_x)
        style.addRow("Top/bottom margin:", self.margin_y)
        bates_box = QGroupBox("Bates number (<<bates>>)")
        bates_form = QFormLayout(bates_box)
        bates_form.addRow("Prefix:", self.bates_prefix)
        bates_form.addRow("Start at:", self.bates_start)
        bates_form.addRow("Digits:", self.bates_digits)
        bates_form.addRow("Suffix:", self.bates_suffix)
        self.range = PageRangeBox(page_count, current, selected, self)
        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addWidget(help_label)
        layout.addLayout(style)
        layout.addWidget(bates_box)
        layout.addWidget(self.range)
        layout.addWidget(_buttons(self, "Apply"))

    def spec(self) -> HeaderFooter:
        return HeaderFooter(
            texts={slot: edit.text() for slot, edit in self.slots.items() if edit.text()},
            font=self.font_box.currentText(),
            font_size=self.size_box.value(),
            color=self.color_button.color,
            margin_x=self.margin_x.value(),
            margin_top=self.margin_y.value(),
            margin_bottom=self.margin_y.value(),
            bates_prefix=self.bates_prefix.text(),
            bates_start=self.bates_start.value(),
            bates_digits=self.bates_digits.value(),
            bates_suffix=self.bates_suffix.text(),
        )


# -- watermark / background ---------------------------------------------------------------------
class WatermarkDialog(QDialog):
    def __init__(
        self, page_count: int, current: int, selected: list[int], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Watermark")
        self.use_text = QRadioButton("Text:")
        self.use_image = QRadioButton("Image:")
        self.use_text.setChecked(True)
        self.text = QLineEdit("CONFIDENTIAL")
        self.text.textEdited.connect(lambda _t: self.use_text.setChecked(True))
        self.image_path = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.font_box = QComboBox()
        self.font_box.addItems(FONTS)
        self.font_box.setCurrentText("hebo")
        self.size_box = QDoubleSpinBox()
        self.size_box.setRange(6, 400)
        self.size_box.setValue(60)
        self.color_button = ColorButton(Color(0.75, 0.1, 0.1))
        self.opacity = QSpinBox()
        self.opacity.setRange(5, 100)
        self.opacity.setValue(30)
        self.opacity.setSuffix(" %")
        self.angle = QDoubleSpinBox()
        self.angle.setRange(-180, 180)
        self.angle.setValue(45)
        self.angle.setSuffix("°")
        self.scale_box = QSpinBox()
        self.scale_box.setRange(5, 100)
        self.scale_box.setValue(50)
        self.scale_box.setSuffix(" % of page width")
        self.behind = QCheckBox("Behind page content")
        self.range = PageRangeBox(page_count, current, selected, self)
        img_row = QHBoxLayout()
        img_row.addWidget(self.image_path, 1)
        img_row.addWidget(browse)
        form = QFormLayout()
        form.addRow(self.use_text, self.text)
        form.addRow(self.use_image, img_row)
        form.addRow("Font:", self.font_box)
        form.addRow("Size:", self.size_box)
        form.addRow("Color:", self.color_button)
        form.addRow("Opacity:", self.opacity)
        form.addRow("Rotation:", self.angle)
        form.addRow("Image size:", self.scale_box)
        form.addRow("", self.behind)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.range)
        layout.addWidget(_buttons(self, "Apply"))

    def _browse(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Watermark Image", "", PDF_OR_IMAGES)
        if chosen:
            self.image_path.setText(chosen)
            self.use_image.setChecked(True)

    def spec(self) -> Watermark:
        """Raises OSError if the image can't be read."""
        image = Path(self.image_path.text()).read_bytes() if self.use_image.isChecked() else None
        return Watermark(
            text=self.text.text(),
            image=image,
            font=self.font_box.currentText(),
            font_size=self.size_box.value(),
            color=self.color_button.color,
            opacity=self.opacity.value() / 100,
            angle=self.angle.value(),
            scale=self.scale_box.value() / 100,
            on_top=not self.behind.isChecked(),
        )


class BackgroundDialog(QDialog):
    def __init__(
        self, page_count: int, current: int, selected: list[int], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Background")
        self.color_button = ColorButton(Color(1, 1, 0.9))
        self.opacity = QSpinBox()
        self.opacity.setRange(5, 100)
        self.opacity.setValue(100)
        self.opacity.setSuffix(" %")
        self.range = PageRangeBox(page_count, current, selected, self)
        form = QFormLayout()
        form.addRow("Color:", self.color_button)
        form.addRow("Opacity:", self.opacity)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.range)
        layout.addWidget(_buttons(self, "Apply"))
