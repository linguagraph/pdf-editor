"""Dialogs for Organize commands: page ranges, insert, extract, split, combine, crop, labels,
header/footer (+ Bates), watermark and background."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
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
from pdfeditor.model.fonts import FontRef, FontRefKind
from pdfeditor.model.objects import family_of
from pdfeditor.model.pages import LabelStyle, PageLabelRule
from pdfeditor.services.assembly import IMAGE_SUFFIXES, MergeSource, SplitMode
from pdfeditor.services.pages import format_page_ranges, parse_page_ranges
from pdfeditor.services.stamping import TOKENS_HELP, HeaderFooter, Slot, Watermark
from pdfeditor.ui.color_picker import ColorButton
from pdfeditor.ui.dialogs.base import FormDialog, add_row, caption
from pdfeditor.ui.font_picker import FontPicker, remember_font
from pdfeditor.ui.style.tokens import METRICS

# base-14 codes used by the engine (MuPDF's short names) <-> the font picker's standard refs
_BASE14_BY_STANDARD = {"Helvetica": "helv", "Times-Roman": "tiro", "Courier": "cour"}
_BASE14_BY_FAMILY = {"sans": "helv", "serif": "tiro", "mono": "cour"}
_STANDARD_BY_BASE14 = {
    "helv": "Helvetica",
    "hebo": "Helvetica",
    "tiro": "Times-Roman",
    "tibo": "Times-Roman",
    "cour": "Courier",
    "cobo": "Courier",
}


def _base14_for_ref(ref: FontRef) -> str:
    """The base-14 code (``spec.font``) that goes with a font picker choice."""
    if ref.kind is FontRefKind.STANDARD:
        return _BASE14_BY_STANDARD.get(ref.name, "helv")
    if ref.kind is FontRefKind.FILE:
        return _BASE14_BY_FAMILY[family_of(ref.name)]
    return "helv"


def _ref_for_base14(font_ref: FontRef | None, base14: str) -> tuple[FontRef, str]:
    """What the font picker should preselect for a saved ``HeaderFooter``/``Watermark``."""
    if font_ref is not None:
        return font_ref, font_ref.name
    name = _STANDARD_BY_BASE14.get(base14, "Helvetica")
    return FontRef.standard(name), name


PDF_OR_IMAGES = (
    "PDF and images (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp);;All files (*)"
)


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
        self.edit.setAccessibleName("Page range")
        self.edit.textEdited.connect(lambda _t: self.custom.setChecked(True))
        grid = QGridLayout(self)
        grid.setVerticalSpacing(METRICS.space(1))
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

    def select_pages(self, pages: list[int]) -> None:
        """Pre-select ``pages`` (0-based), e.g. the pages an existing mark is on."""
        chosen = sorted(set(pages))
        if chosen == list(range(self.page_count)):
            self.all.setChecked(True)
        elif chosen == [self.current_index]:
            self.current.setChecked(True)
        elif chosen:
            self.edit.setText(format_page_ranges(chosen))
            self.custom.setChecked(True)


def checked_pages(dialog: QWidget, box: PageRangeBox) -> list[int] | None:
    try:
        return box.pages()
    except ValueError as exc:
        QMessageBox.warning(dialog, dialog.windowTitle(), str(exc))
        return None


# -- insert -------------------------------------------------------------------------------------
class InsertPagesDialog(FormDialog):
    """Where to insert and what: a blank page, pages from a file, or the clipboard image."""

    def __init__(self, page_count: int, current: int, parent: QWidget | None = None) -> None:
        super().__init__("Insert Pages", parent=parent, primary="Insert")
        self.blank = QRadioButton("Blank page")
        self.file = QRadioButton("From file:")
        self.clipboard = QRadioButton("Image from clipboard")
        self.blank.setChecked(True)
        self.path_edit = QLineEdit()
        self.path_edit.setAccessibleName("File to insert from")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.path_edit.textEdited.connect(lambda _t: self.file.setChecked(True))
        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText("all pages")
        self.where = QComboBox()
        self.where.addItems(["After", "Before"])
        self.where.setAccessibleName("Insert after or before")
        self.page = QSpinBox()
        self.page.setAccessibleName("Page number")
        self.page.setRange(1, page_count)
        self.page.setValue(current + 1)
        file_row = QHBoxLayout()
        file_row.addWidget(self.path_edit, 1)
        file_row.addWidget(browse)
        form = self.add_form()
        form.addRow(self.blank)
        form.addRow(self.file, file_row)
        add_row(form, "Pages of that file:", self.range_edit, "Empty inserts every page.")
        form.addRow(self.clipboard)
        where = QHBoxLayout()
        where.setSpacing(METRICS.space(2))
        where.addWidget(self.where)
        where.addWidget(QLabel("page"))
        where.addWidget(self.page)
        where.addStretch()
        form.addRow("Insert:", where)

    def _browse(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Insert From File", "", PDF_OR_IMAGES)
        if chosen:
            self.path_edit.setText(chosen)
            self.file.setChecked(True)

    def position(self) -> int:
        """0-based index to insert before."""
        return self.page.value() if self.where.currentText() == "After" else self.page.value() - 1


# -- extract ------------------------------------------------------------------------------------
class ExtractDialog(FormDialog):
    def __init__(
        self, page_count: int, current: int, selected: list[int], parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "Extract Pages",
            "Copies the chosen pages into a new document.",
            parent,
            primary="Extract",
        )
        self.range = PageRangeBox(page_count, current, selected, self)
        if len(selected) <= 1:
            self.range.current.setChecked(True)
        self.delete_after = QCheckBox("Delete pages after extracting")
        self.separate = QCheckBox("Extract each page as a separate file")
        for w in (self.range, self.delete_after, self.separate):
            self.add_widget(w)


# -- split --------------------------------------------------------------------------------------
class SplitDialog(FormDialog):
    def __init__(self, stem: str, folder: Path, parent: QWidget | None = None) -> None:
        super().__init__(
            "Split Document",
            "Saves the parts as separate files; this document is left unchanged.",
            parent,
            primary="Split",
        )
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
        self.pages.setAccessibleName("Pages per file")
        self.size_mb = QDoubleSpinBox()
        self.size_mb.setAccessibleName("Maximum file size")
        self.size_mb.setRange(0.05, 10000)
        self.size_mb.setValue(10)
        self.size_mb.setSuffix(" MB")
        self.ranges = QLineEdit()
        self.ranges.setPlaceholderText("e.g. 1-3; 4-10; 11-")
        self.ranges.setAccessibleName("Page ranges")
        self.folder = QLineEdit(str(folder))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.stem = QLineEdit(stem)
        grid = QGridLayout()
        grid.setVerticalSpacing(METRICS.space(2))
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
        self.content.addLayout(grid)
        out = self.add_form()
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        add_row(out, "Output folder:", folder_row)
        add_row(out, "File name prefix:", self.stem, "Parts are numbered after the prefix.")

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
class CombineDialog(FormDialog):
    """Pick and order files (PDFs and images) to combine into a new document."""

    def __init__(self, initial: list[Path] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(
            "Combine Files",
            "Drag to reorder. Double-click a PDF to choose its pages.",
            parent,
            primary="Combine",
        )
        self.resize(600, 440)
        self.list = QListWidget(self)
        self.list.setAccessibleName("Files to combine")
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
        self.content.addLayout(top, 1)
        self.add_widget(self.bookmarks)
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
class CropDialog(FormDialog):
    def __init__(
        self,
        page_count: int,
        current: int,
        selected: list[int],
        margins: tuple[float, float, float, float] = (0, 0, 0, 0),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Crop Pages",
            "Hides the margins from view and print; the content stays in the file.",
            parent,
            primary="Crop",
        )
        self.spins = []
        form = self.add_form()
        for label, value in zip(("Left", "Top", "Right", "Bottom"), margins, strict=True):
            spin = QDoubleSpinBox()
            spin.setRange(0, 5000)
            spin.setSuffix(" pt")
            spin.setValue(value)
            self.spins.append(spin)
            add_row(form, f"{label}:", spin)
        self.trim = QCheckBox("Remove white margins automatically")
        add_row(form, "", self.trim, "Ignores the values above.")
        self.range = PageRangeBox(page_count, current, selected, self)
        if len(selected) <= 1 and any(margins):
            self.range.current.setChecked(True)
        self.add_widget(self.range)

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


class PageLabelsDialog(FormDialog):
    def __init__(
        self, rules: list[PageLabelRule], page_count: int, parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "Page Labels",
            "Labels replace page numbers in the navigator, thumbnails and printing dialogs.",
            parent,
        )
        self.page_count = page_count
        self.table = QTableWidget(0, 4, self)
        self.table.setAccessibleName("Label ranges")
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
        self.add_widget(self.table, 1)
        self.content.addLayout(row)
        for rule in rules:
            self.add_rule(rule)
        self.resize(560, 360)

    def add_rule(self, rule: PageLabelRule) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        start = QSpinBox()
        start.setAccessibleName("From page")
        start.setRange(1, self.page_count)
        start.setValue(rule.start + 1)
        style = QComboBox()
        style.setAccessibleName("Numbering style")
        for s, name in _STYLE_NAMES:
            style.addItem(name, s)
        style.setCurrentIndex(max(0, style.findData(rule.style)))
        first = QSpinBox()
        first.setAccessibleName("Start at")
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
class HeaderFooterDialog(FormDialog):
    def __init__(
        self,
        page_count: int,
        current: int,
        selected: list[int],
        bates: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Bates Numbering" if bates else "Header & Footer",
            "Text stamped at the top and bottom of each page.",
            parent,
            primary="Apply",
        )
        self.slots: dict[Slot, QLineEdit] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(METRICS.space(2))
        grid.addWidget(QLabel("Left"), 0, 1)
        grid.addWidget(QLabel("Center"), 0, 2)
        grid.addWidget(QLabel("Right"), 0, 3)
        for row, prefix in ((1, "header"), (2, "footer")):
            grid.addWidget(QLabel(prefix.capitalize() + ":"), row, 0)
            for col, side in ((1, "left"), (2, "center"), (3, "right")):
                edit = QLineEdit()
                edit.setAccessibleName(f"{prefix.capitalize()} {side}")
                slot = Slot(f"{prefix}_{side}")
                self.slots[slot] = edit
                grid.addWidget(edit, row, col)
        if bates:
            self.slots[Slot.FOOTER_RIGHT].setText("<<bates>>")
        help_label = caption(f"Fields: {TOKENS_HELP}")
        self.font_picker = FontPicker(self)
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
        self.content.addLayout(grid)
        self.add_widget(help_label)
        self.style_section = self.add_section("Text style", expanded=False)
        style = self.style_section.form()
        add_row(style, "Font:", self.font_picker)
        add_row(style, "Size:", self.size_box)
        add_row(style, "Color:", self.color_button)
        add_row(style, "Side margin:", self.margin_x)
        add_row(style, "Top/bottom margin:", self.margin_y)
        self.bates_section = self.add_section("Bates number (<<bates>>)", expanded=bates)
        bates_form = self.bates_section.form()
        add_row(bates_form, "Prefix:", self.bates_prefix)
        add_row(bates_form, "Start at:", self.bates_start)
        add_row(bates_form, "Digits:", self.bates_digits)
        add_row(bates_form, "Suffix:", self.bates_suffix)
        self.range = PageRangeBox(page_count, current, selected, self)
        self.add_widget(self.range)

    def spec(self) -> HeaderFooter:
        ref = self.font_picker.current_ref()
        font_ref = ref if ref.kind is FontRefKind.FILE else None
        if font_ref is not None:
            remember_font(font_ref)
        return HeaderFooter(
            texts={slot: edit.text() for slot, edit in self.slots.items() if edit.text()},
            font=_base14_for_ref(ref),
            font_ref=font_ref,
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

    def load(self, spec: HeaderFooter) -> None:
        """Fill the fields from existing settings (Update Header & Footer)."""
        for slot, edit in self.slots.items():
            edit.setText(spec.texts.get(slot, ""))
        self.font_picker.set_selection(*_ref_for_base14(spec.font_ref, spec.font))
        self.size_box.setValue(spec.font_size)
        self.color_button.set(spec.color)
        self.margin_x.setValue(spec.margin_x)
        self.margin_y.setValue(spec.margin_top)
        self.bates_prefix.setText(spec.bates_prefix)
        self.bates_start.setValue(spec.bates_start)
        self.bates_digits.setValue(spec.bates_digits)
        self.bates_suffix.setText(spec.bates_suffix)


# -- watermark / background ---------------------------------------------------------------------
class WatermarkDialog(FormDialog):
    def __init__(
        self, page_count: int, current: int, selected: list[int], parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "Watermark", "Text or an image stamped across the pages.", parent, primary="Apply"
        )
        self.use_text = QRadioButton("Text:")
        self.use_image = QRadioButton("Image:")
        self.use_text.setChecked(True)
        self.text = QLineEdit("CONFIDENTIAL")
        self.text.setAccessibleName("Watermark text")
        self.text.textEdited.connect(lambda _t: self.use_text.setChecked(True))
        self.image_path = QLineEdit()
        self.image_path.setAccessibleName("Watermark image file")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.font_picker = FontPicker(self)
        self.font_picker.set_selection(FontRef.standard("Helvetica"), "Helvetica")
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
        form = self.add_form()
        form.addRow(self.use_text, self.text)
        form.addRow(self.use_image, img_row)
        self.appearance_section = self.add_section("Appearance")
        look = self.appearance_section.form()
        add_row(look, "Font:", self.font_picker)
        add_row(look, "Size:", self.size_box)
        add_row(look, "Color:", self.color_button)
        add_row(look, "Opacity:", self.opacity)
        add_row(look, "Rotation:", self.angle)
        add_row(look, "Image size:", self.scale_box)
        look.addRow("", self.behind)
        self.add_widget(self.range)

    def _browse(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Watermark Image", "", PDF_OR_IMAGES)
        if chosen:
            self.image_path.setText(chosen)
            self.use_image.setChecked(True)

    def spec(self) -> Watermark:
        """Raises OSError if the image can't be read."""
        image = Path(self.image_path.text()).read_bytes() if self.use_image.isChecked() else None
        ref = self.font_picker.current_ref()
        font_ref = ref if ref.kind is FontRefKind.FILE else None
        if font_ref is not None:
            remember_font(font_ref)
        return Watermark(
            text=self.text.text(),
            image=image,
            image_path=self.image_path.text() if image is not None else "",
            font=_base14_for_ref(ref),
            font_ref=font_ref,
            font_size=self.size_box.value(),
            color=self.color_button.color,
            opacity=self.opacity.value() / 100,
            angle=self.angle.value(),
            scale=self.scale_box.value() / 100,
            on_top=not self.behind.isChecked(),
        )

    def load(self, spec: Watermark) -> None:
        """Fill the fields from existing settings (Update Watermark)."""
        self.text.setText(spec.text)
        self.image_path.setText(spec.image_path)
        (self.use_image if spec.image_path else self.use_text).setChecked(True)
        self.font_picker.set_selection(*_ref_for_base14(spec.font_ref, spec.font))
        self.size_box.setValue(spec.font_size)
        self.color_button.set(spec.color)
        self.opacity.setValue(round(spec.opacity * 100))
        self.angle.setValue(spec.angle)
        self.scale_box.setValue(round(spec.scale * 100))
        self.behind.setChecked(not spec.on_top)


class BackgroundDialog(FormDialog):
    def __init__(
        self, page_count: int, current: int, selected: list[int], parent: QWidget | None = None
    ) -> None:
        super().__init__("Background", "A color behind the page content.", parent, primary="Apply")
        self.color_button = ColorButton(Color(1, 1, 0.9))
        self.opacity = QSpinBox()
        self.opacity.setRange(5, 100)
        self.opacity.setValue(100)
        self.opacity.setSuffix(" %")
        self.range = PageRangeBox(page_count, current, selected, self)
        form = self.add_form()
        add_row(form, "Color:", self.color_button)
        add_row(form, "Opacity:", self.opacity)
        self.add_widget(self.range)

    def load(self, color: Color, opacity: float) -> None:
        """Fill the fields from existing settings (Update Background)."""
        self.color_button.set(color)
        self.opacity.setValue(round(opacity * 100))
