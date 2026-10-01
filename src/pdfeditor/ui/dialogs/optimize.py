"""Reduce File Size and Space Audit dialogs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from pdfeditor.engine.base import OptimizeOptions
from pdfeditor.model.metadata import SpaceUsage
from pdfeditor.services.optimize import PRESETS, ReduceOptions, ReduceResult
from pdfeditor.ui.dialogs.base import FormDialog, add_row, caption


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} bytes"


class ReduceSizeDialog(FormDialog):
    """Pick a preset or custom settings, estimate, then save the smaller copy."""

    def __init__(
        self, source: Path | None, current_size: int | None, parent: QWidget | None = None
    ):
        super().__init__(
            "Reduce File Size",
            "Saves a smaller copy; the open document is left unchanged.",
            parent,
            primary="Save",
        )
        self.current_size = current_size
        self.estimate: ReduceResult | None = None
        self.estimate_for: ReduceOptions | None = None
        self.preset = QComboBox(self)
        for p in PRESETS:
            self.preset.addItem(p.label, p.key)
        self.preset.addItem("Custom", "custom")
        self.description = caption("", self)
        self.keep_images = QCheckBox("Keep image resolution", self)
        self.dpi = QSpinBox(self)
        self.dpi.setRange(36, 1200)
        self.dpi.setSuffix(" dpi")
        self.quality = QSpinBox(self)
        self.quality.setRange(10, 100)
        self.grayscale = QCheckBox("Convert images to grayscale", self)
        self.subset = QCheckBox("Subset embedded fonts", self)
        self.thumbnails = QCheckBox("Remove page thumbnails", self)
        self.metadata = QCheckBox("Remove document information and XMP metadata", self)
        self.object_streams = QCheckBox("Compress document structure (object streams)", self)
        self.linearize = QCheckBox("Optimize for fast web view", self)
        self.estimate_label = QLabel(self)
        self.estimate_label.setProperty("role", "muted")
        self.estimate_label.setWordWrap(True)
        self.estimate_button = QPushButton("Estimate Size", self)
        self.target = QLineEdit(self)
        base = (
            source.with_name(source.stem + "-reduced.pdf")
            if source
            else Path.home() / "reduced.pdf"
        )
        self.target.setText(str(base))
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._browse)

        form = self.add_form()
        add_row(form, "Preset:", self.preset)
        form.addRow("", self.description)
        row = QHBoxLayout()
        row.addWidget(self.target, 1)
        row.addWidget(browse)
        add_row(form, "Save as:", row)

        self.images_section = self.add_section("Images")
        images = self.images_section.form()
        images.addRow("", self.keep_images)
        add_row(images, "Image resolution:", self.dpi)
        add_row(images, "JPEG quality:", self.quality)
        images.addRow("", self.grayscale)
        # rarely changed by hand: the presets set them
        self.advanced_section = self.add_section("Fonts and structure", expanded=False)
        advanced = self.advanced_section.form()
        for box in (self.subset, self.thumbnails, self.metadata, self.object_streams,
                    self.linearize):  # fmt: skip
            advanced.addRow("", box)

        est = QHBoxLayout()
        est.addWidget(self.estimate_button)
        est.addWidget(self.estimate_label, 1)
        self.content.addLayout(est)

        self.preset.currentIndexChanged.connect(lambda _i: self._load_preset())
        self.keep_images.toggled.connect(lambda on: self.dpi.setEnabled(not on))
        for w in (self.dpi, self.quality):
            w.valueChanged.connect(lambda _v: self._edited())
        for b in (self.keep_images, self.grayscale, self.subset, self.thumbnails, self.metadata,
                  self.object_streams, self.linearize):  # fmt: skip
            b.toggled.connect(lambda _on: self._edited())
        self._loading = False
        self.preset.setCurrentIndex(1)  # balanced
        self._load_preset()

    def _load_preset(self) -> None:
        key = self.preset.currentData()
        match = next((p for p in PRESETS if p.key == key), None)
        if match is None:
            self.description.setText("Your own settings.")
            return
        self._loading = True
        o = match.options.optimize
        self.keep_images.setChecked(o.image_dpi is None)
        self.dpi.setValue(o.image_dpi or 300)
        self.dpi.setEnabled(o.image_dpi is not None)
        self.quality.setValue(o.jpeg_quality)
        self.grayscale.setChecked(o.grayscale)
        self.subset.setChecked(o.subset_fonts)
        self.thumbnails.setChecked(o.remove_thumbnails)
        self.metadata.setChecked(o.remove_metadata)
        self.object_streams.setChecked(match.options.object_streams)
        self.linearize.setChecked(match.options.linearize)
        self._loading = False
        self.description.setText(match.description)
        self._edited()

    def _edited(self) -> None:
        if self._loading:
            return
        key = self.preset.currentData()
        match = next((p for p in PRESETS if p.key == key), None)
        if match is not None and match.options != self.options():
            self._loading = True
            self.preset.setCurrentIndex(self.preset.count() - 1)  # custom
            self.description.setText("Your own settings.")
            self._loading = False
        self.show_estimate(None)

    def options(self) -> ReduceOptions:
        return ReduceOptions(
            optimize=replace(
                OptimizeOptions(),
                image_dpi=None if self.keep_images.isChecked() else self.dpi.value(),
                jpeg_quality=self.quality.value(),
                grayscale=self.grayscale.isChecked(),
                subset_fonts=self.subset.isChecked(),
                remove_thumbnails=self.thumbnails.isChecked(),
                remove_metadata=self.metadata.isChecked(),
            ),
            object_streams=self.object_streams.isChecked(),
            linearize=self.linearize.isChecked(),
        )

    def show_estimate(self, result: ReduceResult | None) -> None:
        self.estimate = result
        self.estimate_for = self.options() if result is not None else None
        if result is None:
            now = f"Now: {human_size(self.current_size)}" if self.current_size else ""
            self.estimate_label.setText(now)
            return
        pct = (1 - result.ratio) * 100
        text = f"{human_size(result.before)} → {human_size(result.after)}"
        text += f" ({pct:.0f}% smaller)" if pct >= 0.5 else " (no reduction)"
        if result.notes:
            text += "\n" + " ".join(result.notes)
        self.estimate_label.setText(text)

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Reduced Copy", self.target.text(), "PDF (*.pdf)"
        )
        if path:
            self.target.setText(path)

    def target_path(self) -> Path:
        return Path(self.target.text()).with_suffix(".pdf")


class SpaceAuditDialog(FormDialog):
    def __init__(self, usage: SpaceUsage, parent: QWidget | None = None) -> None:
        super().__init__(
            "Space Usage",
            f"Total: {human_size(usage.total)}. What takes up room in the file.",
            parent,
            primary=None,
            cancel="Close",
        )
        self.resize(560, 440)
        rows = sorted(
            ((k, v) for k, v in usage.categories.items() if v), key=lambda kv: kv[1], reverse=True
        )
        self.table = QTableWidget(len(rows), 3, self)
        self.table.setHorizontalHeaderLabels(["Category", "Size", "Share"])
        self.table.setAccessibleName("Space by category")
        self.table.verticalHeader().setVisible(False)
        for r, (name, size) in enumerate(rows):
            self.table.setItem(r, 0, QTableWidgetItem(name))
            self.table.setItem(r, 1, QTableWidgetItem(human_size(size)))
            bar = QProgressBar(self.table)
            bar.setRange(0, 1000)
            bar.setValue(round(usage.share(name) * 1000))
            bar.setFormat(f"{usage.share(name) * 100:.1f}%")
            self.table.setCellWidget(r, 2, bar)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.add_widget(self.table, 1)
