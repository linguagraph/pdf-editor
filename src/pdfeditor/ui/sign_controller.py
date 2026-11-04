"""Protect ribbon ▸ Sign: digital signatures with Windows-store, PKCS#11 and PFX keys.

Signing doesn't touch the open document: it writes a new file from the saved one, so a signature
always covers exactly what's on disk. It runs as a job because a token can take a long time, and
Windows shows its PIN prompt from the job's thread while the app stays responsive.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QMouseEvent
from PySide6.QtWidgets import QFileDialog, QGraphicsView, QInputDialog, QLineEdit, QMessageBox

from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.signing import (
    KeyRef,
    Pkcs11Key,
    SignatureOptions,
    SignaturePlacement,
    SignResult,
)
from pdfeditor.services.signing import SigningError, WrongPassword, WrongPin, preflight, sign_file
from pdfeditor.ui.dialogs.sign import SignDialog
from pdfeditor.ui.icons import icon
from pdfeditor.ui.jobs import Job, run_modal
from pdfeditor.ui.toasts import folder_action
from pdfeditor.ui.tools.base import Tool

if TYPE_CHECKING:
    from pdfeditor.core.session import DocumentSession
    from pdfeditor.ui.main_window import MainWindow
    from pdfeditor.ui.view.document_view import DocumentView

TITLE = "Sign Document"
MIN_WIDTH, MIN_HEIGHT = 72.0, 24.0  # points: room for two lines of small text


class PlaceSignatureTool(Tool):
    """Drag a box where the visible signature should go; the Sign dialog follows."""

    name = "place_signature"

    def __init__(self, controller: SignController) -> None:
        self.controller = controller
        self._start: tuple[int, Point] | None = None

    def activate(self, view: DocumentView) -> None:
        view.setDragMode(QGraphicsView.DragMode.NoDrag)

    def hover(self, view: DocumentView, event: QMouseEvent) -> None:
        view.viewport().setCursor(Qt.CursorShape.CrossCursor)

    def press(self, view: DocumentView, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        self._start = view.page_point_at(view.mapToScene(event.position().toPoint()))
        return self._start is not None

    def move(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._start is None:
            return False
        page, a = self._start
        b = view.scene_to_page(page, view.mapToScene(event.position().toPoint()))
        view.set_annotation_preview({page: [Rect.from_points([a, b])]})
        return True

    def release(self, view: DocumentView, event: QMouseEvent) -> bool:
        if self._start is None:
            return False
        page, a = self._start
        self._start = None
        view.set_annotation_preview({})
        b = view.scene_to_page(page, view.mapToScene(event.position().toPoint()))
        rect = Rect.from_points([a, b]).intersection(view.page_rect(page))
        view.tool_used.emit()
        self.controller.placed(page, rect)
        return True


class SignController:
    def __init__(
        self, window: MainWindow, tool_group: QActionGroup, tool_actions: dict[str, QAction]
    ) -> None:
        self.w = window
        tool = QAction(icon("pen-tool"), "&Place Signature", window, checkable=True)
        tool.setProperty("needs_doc", True)
        tool.triggered.connect(lambda _=False: window.tool_clicked("place_signature"))
        tool_group.addAction(tool)
        tool_actions["place_signature"] = tool
        self.act_place = tool
        self.act_sign = QAction(icon("signature"), "&Sign Document…", window)
        self.act_sign.setProperty("needs_doc", True)
        self.act_sign.triggered.connect(lambda _=False: self.sign())
        window.addAction(self.act_sign)
        self.last_message = ""
        self.rejected_tokens: set[str] = set()  # a wrong PIN: warn before the next attempt
        self.dialog: SignDialog | None = None

    def ribbon(self) -> None:
        self.w.ribbon.tab("Protect").add_group(self.act_place, self.act_sign, title="Sign")

    # -- placing --------------------------------------------------------------------------
    def placed(self, page: int, rect: Rect) -> None:
        if rect.width < MIN_WIDTH or rect.height < MIN_HEIGHT:
            self.w.notify("Drag a larger box: a signature needs room for its text.", "info")
            return
        # Leave the mouse event handler before opening a modal dialog.
        QTimer.singleShot(0, lambda: self.sign(SignaturePlacement(page, rect)))

    # -- signing --------------------------------------------------------------------------
    def _warn(self, message: str) -> None:
        self.last_message = message
        QMessageBox.warning(self.w, TITLE, message)

    def _saved_first(self, view: DocumentView) -> bool:
        """A signature covers the bytes on disk, so what's on screen must be saved first."""
        session = view.session
        with session.lock:
            repaired = session.document.info().is_repaired
        if session.path is not None and not session.is_dirty and not repaired:
            return True
        reason = (
            "The file on disk is damaged and was repaired when opening it."
            if repaired and not session.is_dirty
            else "This document has changes that aren't in a saved file."
        )
        answer = QMessageBox.question(
            self.w,
            TITLE,
            f"{reason} A signature covers the saved file, so save it first to sign what you "
            "see on screen.",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer != QMessageBox.StandardButton.Save or not self.w.save(view):
            return False
        with session.lock:
            repaired = session.document.info().is_repaired
        if repaired:  # saved over itself; the tab still reports the state it opened with
            self._warn("Close and reopen the repaired file, then sign it.")
            return False
        return session.path is not None and not session.is_dirty

    def _check_document(self, source: Path, visible: bool) -> tuple[bool, str | None]:
        """Refuse early what can't be signed, before anyone looks for their token. Returns
        (go on?, the document's password, if it needs one)."""
        password: str | None = None
        while True:
            pw = password

            def work(_job: Job, pw: str | None = pw) -> object:
                try:
                    return preflight(source, pw)
                except SigningError as exc:
                    return exc

            outcome = run_modal(self.w, "Checking the document…", work)
            if outcome is None:
                return False, None
            if isinstance(outcome, WrongPassword) or getattr(outcome, "needs_password", False):
                prompt = (
                    "Enter this document's password to sign it. The signed copy stays encrypted."
                )
                if isinstance(outcome, WrongPassword):
                    prompt = f"{outcome}\n\n{prompt}"
                password = self.ask_password(prompt)
                if password is None:
                    return False, None
                continue
            if isinstance(outcome, SigningError):
                self._warn(str(outcome))
                return False, None
            if visible and getattr(outcome, "certified", False):
                from pdfeditor.services.signing._pdf import VISIBLE_AFTER_CERTIFICATION

                self._warn(VISIBLE_AFTER_CERTIFICATION)
                return False, None
            return True, password

    def ask_password(self, prompt: str) -> str | None:
        text, ok = QInputDialog.getText(
            self.w, "Password Required", prompt, QLineEdit.EchoMode.Password
        )
        return text if ok else None

    def choose_target(self, source: Path) -> Path | None:
        chosen, _ = QFileDialog.getSaveFileName(
            self.w,
            "Save Signed Copy",
            str(source.with_name(source.stem + "-signed.pdf")),
            "PDF documents (*.pdf)",
        )
        return Path(chosen).with_suffix(".pdf") if chosen else None

    def _target_problem(self, source: Path, target: Path) -> str:
        """Find out before asking the token for a signature, not after."""
        target = target.resolve()
        if target == source.resolve():
            return "Choose a different file name: the original is kept as it is."
        if any(v.session.path == target for v in self.w.views()):
            return f"“{target.name}” is open in a tab. Close it, or choose a different file name."
        if target.exists():
            try:
                with target.open("ab"):
                    pass
            except OSError as exc:
                return f"“{target.name}” can't be overwritten: {exc}"
        return ""

    def sign(
        self,
        placement: SignaturePlacement | None = None,
        dialog: SignDialog | None = None,
        target: Path | None = None,
    ) -> Job | None:
        """The whole flow; the job's outcome is a :class:`SignResult` or the error it met."""
        view = self.w.current_view()
        if view is None:
            return None
        session = view.session
        if session.busy:
            self.w.notify(f"Wait until “{session.busy}” has finished, then sign.", "info")
            return None
        if not self._saved_first(view):
            return None
        source = session.path
        assert source is not None
        ok, password = self._check_document(source, placement is not None)
        if not ok:
            return None
        if dialog is None:
            dialog = SignDialog(placement, self.w, rejected_tokens=self.rejected_tokens)
            self.dialog = dialog
        if not dialog.result() and not dialog.exec():
            dialog.clear_secrets()
            return None
        key = dialog.key(int(self.w.winId()))
        options = dialog.options()
        dialog.clear_secrets()
        target = target or self.choose_target(source)
        if target is None:
            return None
        problem = self._target_problem(source, target)
        if problem:
            self._warn(problem)
            return None
        return self._start_job(session, source, target, key, options, password)

    def _start_job(
        self,
        session: DocumentSession,
        source: Path,
        target: Path,
        key: KeyRef,
        options: SignatureOptions,
        password: str | None,
    ) -> Job:
        engine = self.w.engine()

        def work(job: Job) -> SignResult | SigningError:
            try:
                return sign_file(
                    source,
                    target,
                    key,
                    options,
                    engine,
                    document_password=password,
                    token=job.token,
                    status=job.status,
                )
            except SigningError as exc:  # shown with its own wording, once
                return exc

        return self.w.jobs.start(
            "Signing the document…",
            work,
            session=session,
            on_done=lambda outcome: self._finished(outcome, key),
            on_cancelled=self._cancelled,
        )

    def _cancelled(self) -> None:
        self.last_message = "Signing was cancelled. No file was written."
        self.w.notify(self.last_message, "info")

    def _finished(self, outcome: object, key: KeyRef) -> object:
        if isinstance(outcome, WrongPin) and isinstance(key, Pkcs11Key):
            self.rejected_tokens.add(key.token_serial)
        if isinstance(outcome, SigningError):
            self._warn(str(outcome))
            return outcome
        if isinstance(key, Pkcs11Key):
            self.rejected_tokens.discard(key.token_serial)
        if isinstance(outcome, SignResult):
            self.w.open_path(outcome.path)
            stamp = " with a timestamp" if outcome.timestamped else ""
            self.last_message = (
                f"Signed by {outcome.signer}{stamp} and saved as {outcome.path.name}. Editing the "
                "content of this copy would invalidate the signature."
            )
            self.w.notify(self.last_message, "success", folder_action(outcome.path))
        return outcome
