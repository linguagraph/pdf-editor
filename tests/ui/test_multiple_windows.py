"""Several copies of Elanero side by side: each launch its own window, offset from the others."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QSettings

from pdfeditor import bundle
from pdfeditor.ui import single_instance
from pdfeditor.ui.dialogs.preferences import PreferencesDialog
from pdfeditor.ui.main_window import CASCADE_STEP, SETTINGS_GEOMETRY, MainWindow
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.single_instance import (
    NEW_INSTANCE_FLAG,
    RESET_ENVIRONMENT,
    InstanceSlot,
    new_window_command,
)


def test_slots_number_the_running_copies(tmp_path: Path) -> None:
    first, second, third = InstanceSlot(tmp_path), InstanceSlot(tmp_path), InstanceSlot(tmp_path)
    assert (first.index, second.index, third.index) == (0, 1, 2)
    first.release()
    again = InstanceSlot(tmp_path)  # the lowest free slot is reused
    assert again.index == 0
    for slot in (second, third, again):
        slot.release()


def test_a_crashed_copy_frees_its_slot(tmp_path: Path) -> None:
    # Another process takes slot 0 and dies without releasing it (on Windows the lock file
    # goes with its process; elsewhere Qt finds it stale).
    code = (
        "import os, sys; from pathlib import Path; "
        "from pdfeditor.ui.single_instance import InstanceSlot; "
        "assert InstanceSlot(Path(sys.argv[1])).index == 0; os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True, timeout=60)
    slot = InstanceSlot(tmp_path)
    assert slot.index == 0
    slot.release()


def test_new_window_command_from_source() -> None:
    program, args, env = new_window_command()
    assert Path(program).parent == Path(sys.executable).parent
    assert args == ["-m", "pdfeditor", NEW_INSTANCE_FLAG] and env == {}


def test_new_window_command_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(single_instance, "is_frozen", lambda: True)
    monkeypatch.setattr(bundle, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\Elanero.exe")
    # The exe started by itself must unpack afresh, not share (and lose) the parent's folder.
    assert new_window_command() == (
        r"C:\Apps\Elanero.exe",
        [NEW_INSTANCE_FLAG],
        {RESET_ENVIRONMENT: "1"},
    )


def test_unwritable_data_folder_still_starts(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a folder", encoding="utf-8")
    slot = InstanceSlot(blocker / "instances")  # mkdir fails: a file is in the way
    assert slot.index == 0
    slot.release()


@pytest.mark.gui
def test_new_window_action(qtbot, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(
        "pdfeditor.ui.main_window.start_new_window", lambda: started.append(True) or True
    )
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.act_new_window.shortcut().toString() == "Ctrl+N"
    assert window.act_new_window.isEnabled()  # no document needed
    window.act_new_window.trigger()
    assert started == [True]


@pytest.mark.gui
def test_later_copies_open_offset_and_keep_the_saved_position(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(600, 400)
    window.move(50, 50)
    window._geometry_restored = True  # as if opened at a saved position
    saved = window.pos()
    window.cascade(0)
    assert window.pos() == saved
    window.cascade(2)
    assert window.pos() == saved + QPoint(2 * CASCADE_STEP, 2 * CASCADE_STEP)
    QSettings().remove(SETTINGS_GEOMETRY)
    window.close()
    # Left where the offset put it: not saved, so the offset doesn't stick.
    assert QSettings().value(SETTINGS_GEOMETRY) is None


@pytest.mark.gui
def test_a_later_copy_saves_a_position_the_user_chose(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(600, 400)
    window._geometry_restored = True
    window.cascade(1)
    window.resize(700, 450)  # the user resized it
    QSettings().remove(SETTINGS_GEOMETRY)
    window.close()
    assert QSettings().value(SETTINGS_GEOMETRY) is not None


@pytest.mark.gui
def test_a_later_copy_without_a_saved_position_starts_centered(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(600, 400)
    window.cascade(1)
    screen = window.screen().availableGeometry()
    center = window.frameGeometry().center() - QPoint(CASCADE_STEP, CASCADE_STEP)
    assert (center - screen.center()).manhattanLength() <= 4


@pytest.mark.gui
def test_cascade_wraps_before_leaving_the_screen(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    screen = window.screen().availableGeometry()
    window.resize(screen.width() // 2, screen.height() // 2)
    window.move(screen.topLeft())
    window.cascade(10_000)
    assert screen.contains(window.frameGeometry().bottomRight())


@pytest.mark.gui
def test_preference_sends_files_to_the_running_window(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    assert not AppSettings().single_window  # separate windows by default
    dialog = PreferencesDialog(window.prefs, window)
    assert not dialog.single_window.isChecked()
    dialog.single_window.setChecked(True)
    dialog.accept()
    assert AppSettings().single_window
