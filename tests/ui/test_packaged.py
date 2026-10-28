"""Running from the Microsoft Store (MSIX) package, faked: paths, About, Preferences, recovery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pdfeditor import bundle
from pdfeditor.core import paths

FAMILY = "SGNSolutions.Elanero-PDFEditor_abc123"


@pytest.fixture
def packaged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Pretend to run from the package, with ``tmp_path`` as %LOCALAPPDATA%."""
    names = {"GetCurrentPackageFullName": FAMILY + "_x64", "GetCurrentPackageFamilyName": FAMILY}
    monkeypatch.setattr(bundle, "_package_name", names.get)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


def test_not_packaged_from_source() -> None:
    assert not bundle.is_packaged() and bundle.package_family_name() is None
    assert bundle.channel() == "Source"


def test_channels(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, packaged: Path) -> None:
    assert bundle.is_packaged() and bundle.channel() == "Microsoft Store"
    monkeypatch.setattr(bundle, "_package_name", lambda function: None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Elanero.exe"))
    assert bundle.channel() == "Portable"
    (tmp_path / "unins000.exe").write_bytes(b"")
    assert bundle.channel() == "Installer"


def test_visible_path_points_into_the_package(packaged: Path) -> None:
    report = packaged / "pdfeditor" / "crashes" / "crash.txt"  # what the app thinks it wrote
    real = packaged / "Packages" / FAMILY / "LocalCache" / "Local" / "pdfeditor" / "crashes"
    real.mkdir(parents=True)
    (real / "crash.txt").write_text("boom", encoding="utf-8")
    assert paths.visible_path(report) == real / "crash.txt"
    elsewhere = Path("D:/Documents/file.pdf")
    assert paths.visible_path(elsewhere) == elsewhere  # outside %LOCALAPPDATA%: untouched


def test_visible_path_unchanged_outside_the_package(tmp_path: Path) -> None:
    path = tmp_path / "pdfeditor" / "logs"
    assert paths.visible_path(path) == path


@pytest.mark.gui
def test_preferences_leaves_registration_to_the_package(qtbot, packaged: Path, monkeypatch):
    from pdfeditor.services import file_association as fa
    from pdfeditor.ui.dialogs.preferences import PreferencesDialog
    from pdfeditor.ui.settings import AppSettings

    monkeypatch.setattr(fa, "is_supported", lambda: True)
    monkeypatch.setattr(fa, "register", lambda: pytest.fail("must not register from a package"))
    d = PreferencesDialog(AppSettings())
    qtbot.addWidget(d)
    d.show()
    assert d.register_button.isHidden()
    assert d.default_button.isEnabled() and "Open with" in d.pdf_status.text()


@pytest.mark.gui
def test_about_names_the_channel(qtbot, packaged: Path) -> None:
    from pdfeditor.ui.dialogs.about import AboutDialog

    d = AboutDialog()
    qtbot.addWidget(d)
    assert "(Microsoft Store)" in d.header_subtitle.text()


@pytest.mark.gui
def test_recovery_warns_that_uninstalling_deletes_copies(qtbot, packaged: Path) -> None:
    from pdfeditor.core.autosave import RecoveryEntry
    from pdfeditor.ui.dialogs.recovery import RecoveryDialog

    entry = RecoveryEntry("u", packaged / "u.pdf", packaged / "u.json", None, "Report", 0.0, 1)
    d = RecoveryDialog([entry])
    qtbot.addWidget(d)
    assert "uninstalling" in d.uninstall_note.text()


@pytest.mark.gui
def test_about_links_the_privacy_policy(qtbot) -> None:
    from PySide6.QtWidgets import QLabel

    from pdfeditor import PRIVACY_URL
    from pdfeditor.ui.dialogs.about import AboutDialog

    d = AboutDialog()
    qtbot.addWidget(d)
    assert any(PRIVACY_URL in label.text() for label in d.findChildren(QLabel))
    assert (Path(__file__).resolve().parents[2] / "PRIVACY.md").exists()
