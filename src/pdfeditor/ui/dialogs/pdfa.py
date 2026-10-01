"""PDF/A preflight report and conversion result."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtWidgets import (
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from pdfeditor.services.pdfa import Issue
from pdfeditor.ui.dialogs.base import FormDialog


class PdfaReportDialog(FormDialog):
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
        super().__init__(title, parent=parent, primary=None, cancel="Close")
        self.resize(620, 460)
        self.headline = QLabel(headline, self)
        self.headline.setWordWrap(True)
        layout = self.content
        layout.addWidget(self.headline)
        self.fixed = QListWidget(self)
        self.fixed.setAccessibleName("Fixed")
        if fixed:
            layout.addWidget(QLabel("Fixed:", self))
            for f in fixed:
                self.fixed.addItem(f"✓ {f}")
            layout.addWidget(self.fixed)
        else:
            self.fixed.hide()  # not in the layout: keep it from floating over the header
        self.issues = QListWidget(self)
        self.issues.setAccessibleName("Issues")
        for issue in issues:
            mark = "fixable" if issue.fixable else "needs attention"
            self.issues.addItem(f"{issue} — {mark}")
        if issues:
            layout.addWidget(QLabel("Issues:" if not fixed else "Still blocking PDF/A:", self))
            layout.addWidget(self.issues, 1)
        else:
            self.issues.hide()
        self.verapdf_report = QPlainTextEdit(self)
        self.verapdf_report.setReadOnly(True)
        self.verapdf_report.setAccessibleName("veraPDF report")
        self.verapdf_report.hide()
        layout.addWidget(self.verapdf_report, 1)
        if not issues:
            layout.addStretch(1)  # keep the headline at the top
        buttons = self.button_box
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
