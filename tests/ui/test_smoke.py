from __future__ import annotations

import pytest

pytestmark = pytest.mark.gui


def test_main_window_shows(qtbot) -> None:
    from pdfeditor.ui.main_window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    assert window.isVisible()
    assert window.windowTitle() == "pdfeditor"
