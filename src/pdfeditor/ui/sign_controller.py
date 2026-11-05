"""Protect ribbon ▸ Sign: digital signatures with Windows-store, PKCS#11 and PFX keys.

Signing doesn't touch the open document: it writes a new file from the saved one, so a signature
always covers exactly what's on disk. It runs as a job because a token can take a long time, and
Windows shows its PIN prompt from the job's thread while the app stays responsive.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QMouseEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsView,
    QInputDialog,
    QLineEdit,
    QMessageBox,
)

from pdfeditor.model.geometry import Point, Rect
from pdfeditor.model.signing import (
    KeyRef,
    Pkcs11Key,
    SignatureOptions,
    SignaturePlacement,
    SignatureRequest,
    SignResult,
)
from pdfeditor.services.signing import (
    SigningError,
    WrongPassword,
    WrongPin,
    mailto_url,
    preflight,
    prepare_for_signing,
    sign_file,
)
from pdfeditor.ui.dialogs.request_signature import RequestSignerDialog
from pdfeditor.ui.dialogs.sign import SignDialog
from pdfeditor.ui.icons import icon
from pdfeditor.ui.jobs import Job, run_modal
from pdfeditor.ui.reveal import show_in_folder
from pdfeditor.ui.toasts import folder_action
from pdfeditor.ui.tools.base import Tool

if TYPE_CHECKING:
    from pdfeditor.core.session import DocumentSession
    from pdfeditor.services.signing._pdf import Preflight
    from pdfeditor.ui.main_window import MainWindow
    from pdfeditor.ui.view.document_view import DocumentView

TITLE = "Sign Document"
REQUEST_TITLE = "Request Signatures"
REQUEST_SUFFIX = "-for-signature.pdf"
REQUEST_TOOL = "request_signatures"
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
        self._placed(page, rect)
        return True

    def _placed(self, page: int, rect: Rect) -> None:
        self.controller.placed(page, rect)


class RequestSignaturesTool(PlaceSignatureTool):
    """Drag a box for each signer. Leaving the tool (Done, Esc, another tool) ends the request."""

    name = REQUEST_TOOL

    def _placed(self, page: int, rect: Rect) -> None:
        self.controller.request_placed(page, rect)

    def deactivate(self, view: DocumentView) -> None:
        self.controller.request_tool_left(view)


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
        # Request Signatures: the same drag tool, but each box names a signer instead of signing.
        request_tool = QAction(icon("file-pen-line"), "Request Signatures", window, checkable=True)
        request_tool.setProperty("needs_doc", True)
        request_tool.triggered.connect(lambda _=False: window.tool_clicked(REQUEST_TOOL))
        tool_group.addAction(request_tool)
        tool_actions[REQUEST_TOOL] = request_tool
        self.act_request = QAction(icon("file-pen-line"), "&Request Signatures…", window)
        self.act_request.setProperty("needs_doc", True)
        self.act_request.triggered.connect(lambda _=False: self.request())
        window.addAction(self.act_request)
        self.last_message = ""
        # (serial, slot) of tokens that got a wrong PIN: warn before the next attempt.
        self.rejected_tokens: set[tuple[str, int]] = set()
        self._flow_running = False
        self.dialog: SignDialog | None = None
        # A request in progress: the signers so far (None when no request is running), and the
        # saved document and view it was started from.
        self._requests: list[SignatureRequest] | None = None
        self._request_view: DocumentView | None = None
        self._request_password: str | None = None

    def ribbon(self) -> None:
        self.w.ribbon.tab("Protect").add_group(
            self.act_place, self.act_sign, self.act_request, title="Sign"
        )

    # -- placing --------------------------------------------------------------------------
    def placed(self, page: int, rect: Rect) -> None:
        if rect.width < MIN_WIDTH or rect.height < MIN_HEIGHT:
            self.w.notify("Drag a larger box: a signature needs room for its text.", "info")
            return
        # Leave the mouse event handler before opening a modal dialog.
        QTimer.singleShot(0, lambda: self.sign(SignaturePlacement(page, rect)))

    # -- requesting signatures -------------------------------------------------------------
    # The file is prepared with one empty field per signer; nothing is sent. Each recipient
    # signs their field later, in Elanero or Acrobat.
    def request(self) -> None:
        """Start a request: check the saved file, then let the sender drag a box per signer."""
        if self._requests is not None:
            self.w.notify("Finish the request you started first: press Done on its banner.", "info")
            return
        view = self.w.current_view()
        if view is None:
            return
        session = view.session
        if session.busy:
            self.w.notify(f"Wait until “{session.busy}” has finished, then request.", "info")
            return
        if not self._saved_first(view):
            return
        source = session.path
        assert source is not None
        ok, password, found = self._check_document(source, visible=False)
        if not ok:
            return
        if found is not None and found.certified:
            from pdfeditor.services.signing.request import CERTIFIED

            self._warn(CERTIFIED)
            return
        self._requests = []
        self._request_view = view
        self._request_password = password
        self.w.set_tool(REQUEST_TOOL, view)
        self.w.notify("Drag a box for each signer, then press Done.", "info")

    def request_placed(self, page: int, rect: Rect) -> None:
        if rect.width < MIN_WIDTH or rect.height < MIN_HEIGHT:
            self.w.notify("Drag a larger box: a signature needs room for its name.", "info")
            return
        QTimer.singleShot(0, lambda: self._ask_signer(page, rect))

    def _ask_signer(self, page: int, rect: Rect) -> None:
        """One box, one signer. Finish makes the file; Add Another Signer goes back to dragging;
        Cancel drops only this box, so the signers placed so far aren't lost."""
        if self._requests is None:
            return
        dialog = RequestSignerDialog(len(self._requests) + 1, page, self.w)
        if not dialog.exec():
            self.w.notify("That box was dropped. Drag it again, or press Done to finish.", "info")
            return
        self._requests.append(
            SignatureRequest(dialog.signer_name(), dialog.signer_email(), page, rect)
        )
        if dialog.add_another:
            count = len(self._requests)
            self.w.notify(
                f"{count} signer{'s' if count != 1 else ''} so far. Drag the next box, or press "
                "Done to finish.",
                "info",
            )
            return
        self.w.set_tool("select", self._request_view)  # which ends the request: see below

    def request_tool_left(self, view: DocumentView) -> None:
        """Every way out of the tool ends the request: Done, Esc, another tool, closing the tab.
        With signers placed the file is made next, and its save dialog can still cancel."""
        if self._requests is None or view is not self._request_view:
            return
        requests, password = self._requests, self._request_password
        self._requests = None
        self._request_view = None
        self._request_password = None
        if not requests:
            self.last_message = "Requesting signatures was cancelled. No file was written."
            return
        # Not from inside the tool switch: a modal dialog comes next.
        QTimer.singleShot(0, self.w, lambda: self._finish_requests(view, requests, password))

    def _finish_requests(
        self, view: DocumentView, requests: list[SignatureRequest], password: str | None
    ) -> None:
        if view not in self.w.views() or view.session.closed:  # left by closing the document
            self.last_message = "Requesting signatures was cancelled. No file was written."
            return
        source = view.session.path
        assert source is not None
        target = self.choose_target(source, suffix=REQUEST_SUFFIX, title="Save Copy for Signing")
        if target is None:
            self.last_message = "Requesting signatures was cancelled. No file was written."
            self.w.notify(self.last_message, "info")
            return
        problem = self._target_problem(source, target)
        if problem:
            self._warn(problem)
            return
        self._start_prepare(view.session, source, target, requests, password)

    def _start_prepare(
        self,
        session: DocumentSession,
        source: Path,
        target: Path,
        requests: list[SignatureRequest],
        password: str | None,
    ) -> Job:
        engine = self.w.engine()

        def work(job: Job) -> Path | SigningError:
            try:
                path = prepare_for_signing(
                    source, target, requests, engine, password, token=job.token
                )
            except SigningError as exc:  # shown with its own wording, once
                return exc
            job.commit()  # the file is there: a cancel from here on can't take that back
            return path

        return self.w.jobs.start(
            "Preparing the file for signing…",
            work,
            session=session,
            on_done=lambda outcome: self._prepared(outcome, target, requests),
            on_cancelled=self._cancelled,
            detached=True,  # reads the file, not the session
        )

    def _prepared(self, outcome: object, target: Path, requests: list[SignatureRequest]) -> object:
        if isinstance(outcome, SigningError):
            self._warn(str(outcome))
            return outcome
        if not isinstance(outcome, Path):
            return outcome
        self.w.open_path(outcome)
        count = len(requests)
        self.last_message = f"{target.name} is ready for {count} signer{'s' if count != 1 else ''}."
        self.w.notify(self.last_message, "success", folder_action(outcome))
        box = QMessageBox(self.w)
        box.setWindowTitle(REQUEST_TITLE)
        box.setText(self.last_message)
        box.setInformativeText(
            "Your email app can't attach the file automatically, so attach "
            f"{target.name} to the message."
        )
        email = box.addButton("Email Recipients", QMessageBox.ButtonRole.AcceptRole)
        reveal = box.addButton("Show in Folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.setDefaultButton(email)
        box.exec()
        clicked = box.clickedButton()
        if clicked is email:
            QDesktopServices.openUrl(QUrl(mailto_url(requests, target.name)))
        elif clicked is reveal:
            show_in_folder(outcome)
        return outcome

    # -- signing --------------------------------------------------------------------------
    def _warn(self, message: str) -> None:
        self.last_message = message
        QMessageBox.warning(self.w, TITLE, message)

    def _saved_first(self, view: DocumentView) -> bool:
        """A signature covers the bytes on disk, so what's on screen must be saved first."""
        session = view.session
        if session.file_changed_on_disk():
            self._warn(
                f"“{session.display_name}” was changed outside Elanero since it was opened here, "
                "so the file on disk isn't what you see. Close it and open it again, then sign."
            )
            return False
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

    def _check_document(
        self, source: Path, visible: bool
    ) -> tuple[bool, str | None, Preflight | None]:
        """Refuse early what can't be signed, before anyone looks for their token. Returns
        (go on?, the document's password if it needs one, what the check found)."""
        password: str | None = None
        while True:
            pw = password

            def work(_job: Job, pw: str | None = pw) -> object:
                try:
                    return preflight(source, pw)
                except SigningError as exc:
                    return exc

            try:
                outcome = run_modal(self.w, "Checking the document…", work)
            except RuntimeError as exc:  # a file the checks themselves trip over
                self._warn(f"This document can't be signed: {exc}")
                return False, None, None
            if outcome is None:
                return False, None, None
            if isinstance(outcome, WrongPassword) or getattr(outcome, "needs_password", False):
                prompt = (
                    "Enter this document's password to sign it. The signed copy stays encrypted."
                )
                if isinstance(outcome, WrongPassword):
                    prompt = f"{outcome}\n\n{prompt}"
                password = self.ask_password(prompt)
                if password is None:
                    return False, None, None
                continue
            if isinstance(outcome, SigningError):
                self._warn(str(outcome))
                return False, None, None
            if visible and getattr(outcome, "certified", False):
                from pdfeditor.services.signing._pdf import VISIBLE_AFTER_CERTIFICATION

                self._warn(VISIBLE_AFTER_CERTIFICATION)
                return False, None, None
            return True, password, cast("Preflight", outcome)

    def ask_password(self, prompt: str) -> str | None:
        text, ok = QInputDialog.getText(
            self.w, "Password Required", prompt, QLineEdit.EchoMode.Password
        )
        return text if ok else None

    def choose_target(
        self, source: Path, suffix: str = "-signed.pdf", title: str = "Save Signed Copy"
    ) -> Path | None:
        chosen, _ = QFileDialog.getSaveFileName(
            self.w,
            title,
            str(source.with_name(source.stem + suffix)),
            "PDF documents (*.pdf)",
        )
        if not chosen:
            return None
        path = Path(chosen)
        if path.suffix.lower() == ".pdf":
            return path  # the file dialog has asked about overwriting this name
        # Append, don't replace: "Contract 1.5" must not become "Contract 1.pdf". The file dialog
        # only asked about the name typed, so ask about the name that's really used.
        path = path.with_name(path.name + ".pdf")
        if path.exists() and not self.confirm_overwrite(path):
            return None
        return path

    def confirm_overwrite(self, path: Path) -> bool:
        answer = QMessageBox.question(
            self.w,
            "Save Signed Copy",
            f"“{path.name}” already exists. Replace it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

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
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=target.parent):
                pass
        except OSError as exc:
            return f"The folder “{target.parent}” can't be written to: {exc}"
        return ""

    def sign(
        self,
        placement: SignaturePlacement | None = None,
        dialog: SignDialog | None = None,
        target: Path | None = None,
    ) -> Job | None:
        """The whole flow; the job's outcome is a :class:`SignResult` or the error it met."""
        if self._flow_running:  # its first check runs a nested event loop: a second click
            return None
        self._flow_running = True
        try:
            return self._sign(placement, dialog, target)
        finally:
            self._flow_running = False

    def _sign(
        self,
        placement: SignaturePlacement | None,
        dialog: SignDialog | None,
        target: Path | None,
    ) -> Job | None:
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
        ok, password, found = self._check_document(source, placement is not None)
        if not ok:
            return None
        if dialog is None:
            empty = found.empty_fields if found is not None else ()
            dialog = SignDialog(
                placement, self.w, rejected_tokens=self.rejected_tokens, empty_fields=empty
            )
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
                result = sign_file(
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
            job.commit()  # the file is there: a cancel from here on can't take that back
            return result

        return self.w.jobs.start(
            "Signing the document…",
            work,
            session=session,
            on_done=lambda outcome: self._finished(outcome, key),
            on_cancelled=self._cancelled,
            detached=True,  # reads the file, not the session; a PIN prompt can wait for minutes
        )

    def _cancelled(self) -> None:
        self.last_message = "Signing was cancelled. No file was written."
        self.w.notify(self.last_message, "info")

    def _finished(self, outcome: object, key: KeyRef) -> object:
        if isinstance(outcome, WrongPin) and isinstance(key, Pkcs11Key):
            self.rejected_tokens.add((key.token_serial, key.slot_id))
        if isinstance(outcome, SigningError):
            self._warn(str(outcome))
            return outcome
        if isinstance(key, Pkcs11Key):
            self.rejected_tokens.discard((key.token_serial, key.slot_id))
        if isinstance(outcome, SignResult):
            self.w.open_path(outcome.path)
            stamp = " with a timestamp" if outcome.timestamped else ""
            self.last_message = (
                f"Signed by {outcome.signer}{stamp} and saved as {outcome.path.name}. Editing the "
                "content of this copy would invalidate the signature."
            )
            self.w.notify(self.last_message, "success", folder_action(outcome.path))
        return outcome
