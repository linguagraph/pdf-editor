"""Signatures already in the document, checked offline, in the style of Acrobat's panel.

The check reads the file on disk, never the open session, so it runs in the background without
locking the document. It happens only while the panel is open, and again when the file changes.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdfeditor.core.commands import ChangeKind
from pdfeditor.model.signing import SignatureReport, SignatureVerdict
from pdfeditor.services.signing import SigningError, extract_revision, inspect_signatures
from pdfeditor.ui.icons import icon
from pdfeditor.ui.job_center import JobCenter
from pdfeditor.ui.jobs import Job
from pdfeditor.ui.panels.base import EmptyState, ViewPanel
from pdfeditor.ui.theme import current_colors

log = logging.getLogger(__name__)

REPORT_ROLE = Qt.ItemDataRole.UserRole
ACTION_ROLE = Qt.ItemDataRole.UserRole + 1
VIEW_VERSION = "view-version"

ICONS = {
    SignatureVerdict.VALID: "circle-check",
    SignatureVerdict.UNTRUSTED: "circle-alert",
    SignatureVerdict.INVALID: "x",
}
# In the row itself: a collapsed entry must say its verdict in words, not only by its icon.
VERDICT_LABELS = {
    SignatureVerdict.VALID: "valid",
    SignatureVerdict.UNTRUSTED: "needs checking",
    SignatureVerdict.INVALID: "invalid",
}
NOT_CHECKED_HERE = "Unsaved changes aren't part of these results."
REVOCATION = "Revocation wasn't checked: Elanero doesn't go online to validate."


class SignaturesPanel(ViewPanel):
    title = "Signatures"
    # Any change can add or remove the unsaved-changes note; the check itself reruns only when
    # the file on disk or the view changes (see ``_refresh``).
    rebuild_on = frozenset(ChangeKind)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._jobs: JobCenter | None = None  # the window's job center, once attached
        self._open_version: Callable[[Path], object] | None = None
        self._generation = 0  # results from an older generation are ignored
        self._checked_key: tuple[object, ...] | None = None
        self._checked_path: Path | None = None
        self._reports: list[SignatureReport] | None = None
        # Kept until the app exits: a tab may have the earlier version open.
        self._version_folders: list[tempfile.TemporaryDirectory[str]] = []

        self.note = QLabel(NOT_CHECKED_HERE, self)
        self.note.setWordWrap(True)
        self.note.setVisible(False)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        # The column is as wide as its longest row and the panel scrolls sideways: these sentences
        # and certificate names mustn't be cut off with "...".
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setHorizontalScrollMode(QTreeWidget.ScrollMode.ScrollPerPixel)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.tree.setVisible(False)
        self.tree.itemClicked.connect(self._clicked)
        self.empty = EmptyState(
            "signature",
            "No signatures",
            "This document hasn't been digitally signed.",
            self,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.note)
        layout.addWidget(self.status)
        layout.addWidget(self.tree)
        layout.addWidget(self.empty)

    def attach(self, jobs: JobCenter, open_version: Callable[[Path], object]) -> None:
        """Wire the window's job center and its "open this file in a tab" action."""
        self._jobs = jobs
        self._open_version = open_version

    # -- state -------------------------------------------------------------------------------
    def rebuild(self) -> None:
        self._refresh()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._refresh()

    def badge_count(self) -> int:
        return len(self._reports) if self._reports else 0

    def _refresh(self) -> None:
        view = self.view
        if view is None:
            self._forget()
            self._show_message("")
            self.empty.setVisible(False)
            return
        session = view.session
        with session.lock:
            has_signatures = bool(session.document.info().has_signatures)
        self.note.setVisible(has_signatures and session.is_dirty)
        if not has_signatures:
            self._forget()
            self._show_empty()
            return
        path = session.path
        if path is None:
            self._forget()
            self._show_message("Save this document to check its signatures.")
            return
        key = (id(view), path, _stamp(path))
        if key == self._checked_key:
            return  # the reports (or the check in flight) are for this file already
        self._forget()
        if not self.isVisible():
            self._show_message("Signatures are checked when this panel is open.")
            return
        self._start_check(path, key)

    def _forget(self) -> None:
        """Drop results and any check in flight: they belong to a file or view that's gone."""
        self._generation += 1
        self._checked_key = None
        self._checked_path = None
        self._reports = None
        self.tree.clear()
        self.tree.setVisible(False)

    def _start_check(self, path: Path, key: tuple[object, ...]) -> None:
        if self._jobs is None:
            return
        self._checked_key = key
        self._checked_path = path
        generation = self._generation
        self._show_message("Checking signatures…")

        def work(_job: Job) -> object:
            try:
                return inspect_signatures(path, None)  # no password: the panel never asks
            except SigningError as exc:
                return exc

        def done(outcome: object) -> None:
            if generation != self._generation:
                return  # the view or the file changed while this ran
            if isinstance(outcome, SigningError):
                self._checked_key = None  # a later refresh may try again
                self._show_message(str(outcome))
                return
            if isinstance(outcome, list):
                self._show_reports(outcome)

        def failed(message: str) -> None:
            if generation != self._generation:
                return
            log.warning("checking signatures failed: %s", message)
            self._checked_key = None
            self._show_message(f"Couldn't check the signatures: {message}")

        self._jobs.start(
            "Checking signatures…",
            work,
            on_done=done,
            on_failed=failed,
            detached=True,  # reads the file, not the session
        )

    # -- display -----------------------------------------------------------------------------
    def _show_empty(self) -> None:
        self.status.setVisible(False)
        self.tree.setVisible(False)
        self.empty.setVisible(True)

    def _show_message(self, text: str) -> None:
        self.empty.setVisible(False)
        self.tree.setVisible(False)
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _show_reports(self, reports: list[SignatureReport]) -> None:
        self._reports = reports
        self.tree.clear()
        self.status.setVisible(False)
        self.empty.setVisible(not reports)
        for index, report in enumerate(reports):
            self.tree.addTopLevelItem(self._entry(report, expanded=index == 0))
        self.tree.setVisible(bool(reports))
        self.badge_changed.emit()

    def _entry(self, report: SignatureReport, expanded: bool) -> QTreeWidgetItem:
        signer = report.signer_name or "an unknown signer"
        if report.signer_email:
            signer = f"{signer} <{report.signer_email}>"
        label = VERDICT_LABELS[report.verdict]
        top = QTreeWidgetItem([f"Rev. {report.revision}: Signed by {signer} ({label})"])
        top.setIcon(0, icon(ICONS[report.verdict], _verdict_color(report.verdict)))
        top.setData(0, REPORT_ROLE, report)
        if report.page is not None:
            top.setToolTip(0, f"Go to page {report.page + 1}")
        top.addChild(_text(report.summary))
        if report.reason and report.reason != report.summary:
            top.addChild(_text(report.reason))
        top.addChild(_text(_modified_line(report)))
        top.addChild(_text(_time_line(report)))
        top.addChild(_text(_ltv_line(report)))
        top.addChild(self._details(report))
        top.addChild(_text(f"Last Checked: {report.checked_at.astimezone():%Y.%m.%d %H:%M:%S %z}"))
        top.addChild(_text(REVOCATION))
        if report.invisible:
            top.addChild(_text(f"Field: {report.field_name} (invisible signature)"))
        else:
            top.addChild(_text(f"Field: {report.field_name}"))
        if report.revision_end > 0:  # a damaged byte range has no earlier version to show
            link = _text("Click to view this version")
            link.setData(0, ACTION_ROLE, VIEW_VERSION)
            font = link.font(0)
            font.setUnderline(True)
            link.setFont(0, font)
            top.addChild(link)
        top.setExpanded(expanded)
        return top

    def _details(self, report: SignatureReport) -> QTreeWidgetItem:
        node = QTreeWidgetItem(["Signature Details"])
        rows = [
            f"Digest algorithm: {report.digest_algorithm or 'unknown'}",
            f"Signature type: {report.subfilter or 'unknown'}",
        ]
        if report.location:
            rows.append(f"Location: {report.location}")
        cert = report.certificate
        if cert is not None:
            rows += [
                f"Subject: {cert.subject}",
                f"Issuer: {cert.issuer}",
                f"Serial number: {cert.serial}",
                f"Valid from {cert.not_before:%Y-%m-%d} to {cert.not_after:%Y-%m-%d}",
            ]
        for row in rows:
            node.addChild(_text(row))
        return node

    # -- actions -----------------------------------------------------------------------------
    def _clicked(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        if item.data(0, ACTION_ROLE) == VIEW_VERSION:
            parent = item.parent()
            report = parent.data(0, REPORT_ROLE) if parent is not None else None
            if isinstance(report, SignatureReport):
                self._view_version(report)
            return
        report = item.data(0, REPORT_ROLE)
        if isinstance(report, SignatureReport) and report.page is not None and self.view:
            self.view.go_to_page(report.page, animated=True)

    def _view_version(self, report: SignatureReport) -> None:
        """Write the file as it stood when this signature was applied, then open it."""
        source = self._checked_path
        if source is None or self._jobs is None or self._open_version is None:
            return
        folder = tempfile.TemporaryDirectory(
            prefix="elanero-signatures-", ignore_cleanup_errors=True
        )
        self._version_folders.append(folder)
        target = Path(folder.name) / f"{source.stem} (revision {report.revision}).pdf"

        def work(_job: Job) -> object:
            extract_revision(source, report.revision_end, target)
            return target

        def opened(result: object) -> None:
            if self._open_version is not None and isinstance(result, Path):
                self._open_version(result)

        self._jobs.start("Preparing the earlier version…", work, on_done=opened, detached=True)


def _verdict_color(verdict: SignatureVerdict) -> str:
    colors = current_colors()
    return {
        SignatureVerdict.VALID: colors.success,
        SignatureVerdict.UNTRUSTED: colors.warning,
        SignatureVerdict.INVALID: colors.danger,
    }[verdict]


def _text(value: str) -> QTreeWidgetItem:
    item = QTreeWidgetItem([value])
    item.setTextAlignment(0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    return item


def _modified_line(report: SignatureReport) -> str:
    if report.modified_after:
        return report.coverage
    return "Document has not been modified since this signature was applied."


def _time_line(report: SignatureReport) -> str:
    when = f"Signed {report.signing_time:%Y.%m.%d %H:%M:%S %z}. " if report.signing_time else ""
    if report.time_source == "trusted timestamp":
        return f"{when}Signing time is from a trusted timestamp."
    if report.time_source == "unconfirmed timestamp":
        return f"{when}Signing time is from a timestamp server whose identity wasn't confirmed."
    return f"{when}Signing time is from the clock on the signer's computer."


def _ltv_line(report: SignatureReport) -> str:
    return "Long-term validation data: " + ("present" if report.has_ltv_data else "absent")


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size
