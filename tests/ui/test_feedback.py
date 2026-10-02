"""U8 feedback: toasts, the progress chip for background jobs, destructive confirmations."""

from __future__ import annotations

import shutil
import sys
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QEnterEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from pdfeditor.core.commands import SnapshotCommand
from pdfeditor.core.session import DocumentSession
from pdfeditor.ui import reveal, toasts
from pdfeditor.ui.dialogs import confirm
from pdfeditor.ui.dialogs.redaction import ApplyRedactionsDialog
from pdfeditor.ui.job_center import JobDetails
from pdfeditor.ui.jobs import Job, wait_for
from pdfeditor.ui.main_window import MainWindow

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    qtbot.waitExposed(w)
    yield w
    for view in w.views():
        view.session.undo_stack.set_clean()
    w.close()
    w.deleteLater()


@pytest.fixture
def view(window: MainWindow, fixture_pdf, tmp_path: Path):
    path = tmp_path / "doc.pdf"
    shutil.copy2(fixture_pdf("text_multipage"), path)
    return window.open_path(path)


def hover(widget, inside: bool) -> None:
    if inside:
        QApplication.sendEvent(widget, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
    else:
        QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))


# -- toasts ---------------------------------------------------------------------------------
def test_toast_shows_text_kind_and_accessible_name(qtbot, window: MainWindow) -> None:
    toast = window.notify("Exported 2 pages", "success")
    assert toast.isVisible() and toast.label.text() == "Exported 2 pages"
    assert toast.frame.property("kind") == "success"
    assert toast.accessibleName() == "Done: Exported 2 pages"
    assert "Success notification" in toast.accessibleDescription()
    assert toast.close_button.accessibleName() == "Dismiss notification"
    assert window.toasts.last_text() == "Exported 2 pages"
    assert window.toasts.last_kind() == "success"
    error = window.notify("Export failed: disk full", "error")
    assert error.frame.property("kind") == "error" and error.accessibleName().startswith("Error")
    assert error.timeout_ms > toast.timeout_ms  # errors stay longer


def test_toasts_sit_bottom_right_above_the_status_bar_and_clear_of_the_pill(
    qtbot, window: MainWindow, view
) -> None:
    def frame_rect() -> QRect:
        return toast.frame.geometry().translated(toast.pos())

    # a mid-size window: the toast column would cover the centred pill, so it sits above it
    pill = window.pill
    toast = window.notify("Hello")
    qtbot.waitUntil(lambda: toast.frame.isVisible())
    assert pill.isVisible()
    pill_rect = QRect(pill.mapTo(window, QPoint(0, 0)), pill.size())
    central = window.centralWidget().geometry()
    assert frame_rect().right() > pill_rect.left()  # the columns overlap...
    assert frame_rect().bottom() < pill_rect.top()  # ...so the toast is above the pill
    assert frame_rect().right() <= central.right() and frame_rect().right() > central.right() - 40
    # wide: clear of the pill, at the bottom right just above the status bar
    window.resize(1700, 900)
    qtbot.waitUntil(lambda: window.width() >= 1500)
    window.toasts.place()
    central = window.centralWidget().geometry()
    pill_rect = QRect(pill.mapTo(window, QPoint(0, 0)), pill.size())
    assert not frame_rect().intersects(pill_rect)
    assert frame_rect().right() <= central.right() and frame_rect().right() > central.right() - 40
    assert central.bottom() - 40 < frame_rect().bottom() <= central.bottom()
    assert window.statusBar().geometry().top() > frame_rect().bottom()


def test_toasts_stack_up_to_three(qtbot, window: MainWindow) -> None:
    shown = [window.notify(f"Message {i}") for i in range(4)]
    current = window.toasts.toasts()
    assert current == shown[1:]  # the oldest made room
    assert shown[0].closing
    tops = [t.y() for t in current]
    assert tops == sorted(tops)  # newest at the bottom


def test_toast_auto_dismisses_and_pauses_on_hover(qtbot, window: MainWindow) -> None:
    quick = window.notify("Quick", timeout_ms=100)
    qtbot.waitUntil(lambda: quick not in window.toasts.toasts(), timeout=2000)
    toast = window.notify("Stay while hovered", timeout_ms=200)
    hover(toast, True)
    assert toast.paused
    qtbot.wait(400)
    assert toast in window.toasts.toasts()  # still there
    hover(toast, False)
    assert not toast.paused
    qtbot.waitUntil(lambda: toast not in window.toasts.toasts(), timeout=4000)


def test_toast_pauses_while_focused_and_closes_by_keyboard(qtbot, window: MainWindow) -> None:
    toast = window.notify("Focus me", timeout_ms=200)
    toast.close_button.setFocus()
    qtbot.waitUntil(lambda: toast.paused)
    qtbot.wait(400)
    assert toast in window.toasts.toasts()
    qtbot.keyClick(toast.close_button, Qt.Key.Key_Escape)
    assert toast not in window.toasts.toasts()
    other = window.notify("Close button")
    other.close_button.click()
    assert other not in window.toasts.toasts()


def test_undo_action_after_deleting_pages(qtbot, window: MainWindow, view) -> None:
    before = view.page_count
    window.organize.delete_pages()
    toast = window.toasts.toasts()[-1]
    assert toast.text == "1 page deleted" and toast.action_button is not None
    assert toast.action_button.text() == "Undo" and toast.action_button.isEnabled()
    toast.action_button.click()
    assert view.page_count == before
    assert toast not in window.toasts.toasts()


def test_undo_action_is_disabled_when_not_latest(qtbot, window: MainWindow, view) -> None:
    window.organize.delete_pages()
    toast = window.toasts.toasts()[-1]
    count = view.page_count
    window.organize.rotate(90)  # another command on top
    assert not toast.action_button.isEnabled()
    toast._run_action()  # even if triggered anyway, it must not undo the rotation
    assert view.page_count == count
    assert window.act_undo.text().startswith("&Undo Rotate")


def test_saved_toast_shows_in_folder(qtbot, window: MainWindow, view, monkeypatch) -> None:
    shown: list[Path] = []
    monkeypatch.setattr(toasts, "show_in_folder", lambda path: shown.append(path))
    window.organize.rotate(90)
    assert window.save()
    toast = window.toasts.toasts()[-1]
    assert toast.text == "Saved doc.pdf" and toast.kind == "success"
    assert toast.action_button is not None and toast.action_button.text() == "Show in folder"
    toast.action_button.click()
    assert shown == [view.session.path]


def test_show_in_folder_selects_the_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "a b.pdf"
    target.write_bytes(b"%PDF")
    launched: list[Path] = []
    opened: list[str] = []
    monkeypatch.setattr(reveal, "_launch_explorer", lambda path: launched.append(path) or True)
    monkeypatch.setattr(
        reveal.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True
    )
    assert reveal.show_in_folder(target)
    if sys.platform == "win32":
        assert launched == [target.resolve()] and opened == []
    else:
        assert opened == [str(tmp_path).replace("\\", "/")]
    assert not reveal.show_in_folder(tmp_path / "gone" / "x.pdf")


# -- progress chip --------------------------------------------------------------------------
def slow_job(gate: threading.Event, steps: int = 4):
    def work(job: Job) -> str:
        for i in range(steps):
            job.token.check()
            job.progress(i, steps)
            gate.wait(0.05)
        job.token.check()
        return "finished"

    return work


def test_progress_chip_runs_a_job_without_a_modal_dialog(qtbot, window: MainWindow, view) -> None:
    gate = threading.Event()
    results: list[object] = []

    def work(job: Job) -> str:
        job.progress(1, 4)
        assert gate.wait(10)
        job.progress(3, 4)
        return "done"

    job = window.jobs.start(
        "Recognizing text…", work, total=4, session=view.session, on_done=results.append
    )
    chip = window.progress_chip
    qtbot.waitUntil(chip.isVisible, timeout=3000)
    qtbot.waitUntil(lambda: "1/4" in chip.button.text())
    assert chip.button.text().startswith("Recognizing text")
    assert chip.cancel_button.accessibleName() == "Cancel Recognizing text"
    assert chip.bar.maximum() == 4 and chip.bar.value() == 1
    assert window.statusBar().isAncestorOf(chip)
    assert not [w for w in QApplication.topLevelWidgets() if isinstance(w, QProgressDialog)]
    # the document is read-only meanwhile, the window isn't
    assert view.session.busy == "Recognizing text"
    assert not window.act_undo.isEnabled() and not window.tool_actions["highlight"].isEnabled()
    view.session.execute(SnapshotCommand("Edit", lambda doc: None, view.session.snapshots))
    assert window.toasts.last_kind() == "error" and "can't be changed" in window.toasts.last_text()
    assert not view.session.undo_stack.can_undo
    window.next_page()  # navigation still works
    gate.set()
    assert wait_for(job) is None  # on_done (list.append) returned None
    assert results == ["done"]
    qtbot.waitUntil(lambda: not chip.isVisible())
    assert not view.session.busy and window.tool_actions["highlight"].isEnabled()


def test_chip_details_and_cancel(qtbot, window: MainWindow, view) -> None:
    gate = threading.Event()
    job = window.jobs.start(
        "Exporting pages…", slow_job(gate, 400), total=400, session=view.session
    )
    other = window.jobs.start("Batch OCR…", slow_job(gate, 400), total=400)
    chip = window.progress_chip
    qtbot.waitUntil(chip.isVisible, timeout=3000)
    assert chip.button.text() == "2 tasks running"
    assert chip.cancel_button.accessibleName() == "Cancel all tasks"
    details = chip.show_details()
    assert isinstance(details, JobDetails) and details.isVisible()
    names = [row.name.text() for row in details.rows]
    assert names == ["Exporting pages", "Batch OCR"]
    first = details.rows[0]
    assert "doc.pdf" in first.detail.text() and "elapsed" in first.detail.text()
    assert first.cancel.accessibleName() == "Cancel Exporting pages"
    first.cancel.click()
    wait_for(job)
    assert window.toasts.last_text() == "Exporting pages was cancelled."
    assert not view.session.busy
    qtbot.waitUntil(lambda: chip.button.text().startswith("Batch OCR"))
    chip.cancel_button.click()
    wait_for(other)
    qtbot.waitUntil(lambda: not chip.isVisible())
    qtbot.waitUntil(lambda: window.progress_chip.details is None)


def test_failed_job_shows_an_error_toast(qtbot, window: MainWindow) -> None:
    def boom(_job: Job) -> None:
        raise RuntimeError("disk full")

    wait_for(window.jobs.start("Exporting to Word…", boom))
    assert window.toasts.last_kind() == "error"
    assert window.toasts.last_text() == "Exporting to Word failed: disk full"


def test_closing_a_document_cancels_its_job(qtbot, window: MainWindow, view) -> None:
    gate = threading.Event()
    job = window.jobs.start("Exporting pages…", slow_job(gate, 10_000), session=view.session)
    qtbot.waitUntil(lambda: job.started)
    window.close_tab(window.tabs.currentIndex(), ask=False)
    assert job.done and view.session.closed
    wait_for(job)
    assert window.jobs.running() == []


def test_session_refuses_saving_while_busy(fixture_pdf, tmp_path: Path) -> None:
    from pdfeditor.engine.base import SaveError

    session = DocumentSession.open(fixture_pdf("text_multipage"))
    try:
        session.begin_task("Optimizing")
        assert session.busy == "Optimizing" and not session.undo()
        with pytest.raises(SaveError, match="Optimizing"):
            session.save(tmp_path / "x.pdf")
        session.end_task("Optimizing")
        assert session.busy == ""
        session.save(tmp_path / "x.pdf")
    finally:
        session.close()


def test_held_lock_is_shared_between_steps(qtbot) -> None:
    """A job holding the engine lock lets other threads in at each progress step."""
    from pdfeditor.core.engine_lock import ENGINE_LOCK

    got_in: list[bool] = []
    at_step = threading.Event()
    proceed = threading.Event()

    def work(job: Job) -> None:
        with job.hold(ENGINE_LOCK):
            at_step.set()
            proceed.wait(5)
            job.progress(1, 2)  # the waiter gets the lock here
            job.progress(2, 2)

    job = Job(work)
    with qtbot.waitSignal(job.finished, timeout=5000):
        job.start()
        assert at_step.wait(5)

        def waiter() -> None:
            with ENGINE_LOCK:
                got_in.append(True)

        thread = threading.Thread(target=waiter)
        thread.start()
        proceed.set()
        thread.join(5)
    assert got_in == [True]


# -- destructive confirmations -------------------------------------------------------------
def test_confirm_dialog_is_red_with_cancel_as_default(qtbot) -> None:
    text = "Permanently remove 12 marked areas on 4 pages. This can't be undone after saving."
    dialog = confirm.ConfirmDialog("Apply Redactions", text, "Apply 12 Redactions")
    qtbot.addWidget(dialog)
    assert dialog.message.text() == text
    assert dialog.action_button.text() == "Apply 12 Redactions"
    assert dialog.action_button.property("role") == "danger"
    assert dialog.cancel_button.isDefault() and not dialog.action_button.isDefault()
    dialog.show()
    qtbot.waitExposed(dialog)
    assert dialog.focusWidget() is dialog.cancel_button
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert dialog.result() == confirm.ConfirmDialog.DialogCode.Rejected


def test_confirm_destructive_answers(qtbot) -> None:
    def press(label: str) -> None:
        def click() -> None:
            dialog = QApplication.activeModalWidget()
            assert isinstance(dialog, confirm.ConfirmDialog)
            button = dialog.action_button if label == "action" else dialog.cancel_button
            button.click()

        QTimer.singleShot(50, click)

    press("action")
    assert confirm.confirm_destructive(None, "Flatten Comments", "Burn 3 comments…", "Flatten")
    press("cancel")
    assert not confirm.confirm_destructive(None, "Flatten Comments", "Burn 3…", "Flatten")


def test_unsaved_changes_prompt(qtbot, window: MainWindow, view) -> None:
    window.organize.rotate(90)

    def answer(which: str) -> None:
        def click() -> None:
            dialog = QApplication.activeModalWidget()
            assert isinstance(dialog, confirm.ConfirmDialog)
            assert "doc.pdf" in dialog.header_title.text()
            assert dialog.message.text().startswith("The last 1 change you made will be lost")
            assert dialog.action_button.text() == "Don't Save"
            assert dialog.action_button.property("role") == "danger"
            {"save": dialog.extra_button, "discard": dialog.action_button}.get(
                which, dialog.cancel_button
            ).click()

        QTimer.singleShot(50, click)

    answer("save")
    assert window.ask_save_changes(view.session) == QMessageBox.StandardButton.Save
    answer("discard")
    assert window.ask_save_changes(view.session) == QMessageBox.StandardButton.Discard
    answer("cancel")
    assert window.ask_save_changes(view.session) == QMessageBox.StandardButton.Cancel


def test_apply_redactions_says_what_is_lost(qtbot) -> None:
    dialog = ApplyRedactionsDialog(12, 3, total_pages=4, selected_pages=1)
    qtbot.addWidget(dialog)
    assert dialog.primary_button.property("role") == "danger"
    assert dialog.header_subtitle.text() == (
        "Permanently remove the text, images and graphics under 3 marked areas on 1 page. "
        "You can undo it until you close the document, but this can't be undone after saving."
    )
    dialog.all.setChecked(True)
    assert "under 12 marked areas on 4 pages." in dialog.header_subtitle.text()


def test_flatten_all_goes_through_the_confirmation(
    qtbot, window: MainWindow, view, monkeypatch
) -> None:
    asked: list[tuple[str, str, str]] = []
    monkeypatch.setattr(confirm, "confirm_destructive", lambda _p, *a: asked.append(a) or False)
    assert window.flatten_all() == 0
    assert asked == []  # nothing to flatten: told so instead of asked
    assert window.toasts.last_text() == "There are no comments to flatten."
    window.protect.mark_pages()  # redaction marks are comments too
    assert window.flatten_all() == 0  # declined
    ((title, text, action),) = asked
    assert title == "Flatten Comments" and action == "Flatten 1 Comment"
    assert text.startswith("Burn 1 comment on 1 page into the page content.")


def test_toasts_are_announced_to_screen_readers(qtbot, window: MainWindow, monkeypatch) -> None:
    said: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        toasts, "announce", lambda _w, text, assertive=False: said.append((text, assertive))
    )
    window.notify("Saved a.pdf", "success")
    window.notify("Couldn't save", "error")
    assert said == [("Done: Saved a.pdf", False), ("Error: Couldn't save", True)]
    toasts.announce(window, "real call, without a screen reader attached")  # must not raise


def test_toasts_fade_only_with_animations(qtbot, window: MainWindow) -> None:
    from pdfeditor.ui.view import motion

    still = window.notify("No motion")
    assert still.graphicsEffect() is None
    still.dismiss()
    assert not still.isVisible()  # gone at once
    motion.set_animations_enabled(True)
    faded = window.notify("Fading in")
    assert faded.graphicsEffect() is not None  # opacity animation running
    qtbot.waitUntil(lambda: faded.graphicsEffect() is None, timeout=2000)
    faded.dismiss()
    assert faded.isVisible()  # fading out
    qtbot.waitUntil(lambda: faded not in window.findChildren(toasts.Toast), timeout=2000)
