"""PDF/A preflight report and conversion result."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.services.pdfa import Issue


class PdfaReportDialog(QDialog):
    """Issues found (and, after a conversion, what was fixed); optional veraPDF button."""

    def __init__(
        self,
        title: str,
        headline: str,
        issues: Sequence[Issue],
        fixed: Sequence[str] = (),
        on_convert: Callable[[], object] | None = None,
        on_verapdf: Callable[[], tuple[bool, str] | None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(620, 460)
        self.headline = QLabel(headline, self)
        self.headline.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.headline)
        self.fixed = QListWidget(self)
        if fixed:
            layout.addWidget(QLabel("Fixed:", self))
            for f in fixed:
                self.fixed.addItem(f"✓ {f}")
            layout.addWidget(self.fixed)
        self.issues = QListWidget(self)
        for issue in issues:
            mark = "fixable" if issue.fixable else "needs attention"
            self.issues.addItem(f"{issue} — {mark}")
        if issues:
            layout.addWidget(QLabel("Issues:" if not fixed else "Still blocking PDF/A:", self))
            layout.addWidget(self.issues, 1)
        self.verapdf_report = QPlainTextEdit(self)
        self.verapdf_report.setReadOnly(True)
        self.verapdf_report.hide()
        layout.addWidget(self.verapdf_report, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        self.convert_button: QPushButton | None = None
        if on_convert is not None:
            self.convert_button = buttons.addButton(
                "Save as PDF/A…", QDialogButtonBox.ButtonRole.ActionRole
            )

            def convert() -> None:
                self.accept()
                on_convert()

            self.convert_button.clicked.connect(convert)
        self.verapdf_button: QPushButton | None = None
        if on_verapdf is not None:
            self.verapdf_button = buttons.addButton(
                "Validate with veraPDF", QDialogButtonBox.ButtonRole.ActionRole
            )

            def run() -> None:
                outcome = on_verapdf()
                if outcome is None:
                    return
                ok, text = outcome
                self.verapdf_report.setPlainText(text)
                self.verapdf_report.show()
                self.headline.setText(
                    self.headline.text()
                    + ("\nveraPDF: compliant." if ok else "\nveraPDF: not compliant (see report).")
                )

            self.verapdf_button.clicked.connect(run)
        layout.addWidget(buttons)
