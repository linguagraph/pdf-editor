"""Registering as a PDF editor with Windows, in a scratch registry key (never the real one)."""

from __future__ import annotations

import contextlib
import sys
import uuid
from collections.abc import Iterator

import pytest

from pdfeditor import APP_DISPLAY_NAME
from pdfeditor.services import file_association as fa

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")

LAUNCHER = fa.Launcher(r'"C:\Apps\Elanero.exe" "%1"', r"C:\Apps\Elanero.exe,0", "Elanero.exe")


@pytest.fixture
def base() -> Iterator[str]:
    """A throwaway HKCU subkey standing in for HKCU itself."""
    import winreg

    key = rf"Software\pdfeditor-tests\{uuid.uuid4().hex}"
    yield key

    def delete_tree(path: str) -> None:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    child = winreg.EnumKey(k, 0)
                except OSError:
                    break
                delete_tree(f"{path}\\{child}")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)

    with contextlib.suppress(FileNotFoundError):
        delete_tree(key)


def read(base: str, path: str, name: str = "") -> object:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\{path}") as key:
        return winreg.QueryValueEx(key, name)[0]


def write(base: str, path: str, name: str, value: str) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, f"{base}\\{path}") as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def test_register_round_trip(base: str) -> None:
    assert not fa.is_registered(LAUNCHER, base=base)
    fa.register(LAUNCHER, base=base, notify=False)
    assert fa.is_registered(LAUNCHER, base=base)
    classes = r"Software\Classes"
    assert read(base, rf"{classes}\pdfeditor.pdf\shell\open\command") == LAUNCHER.command
    # the file type's icon in Explorer is the app's icon
    assert read(base, rf"{classes}\pdfeditor.pdf\DefaultIcon") == LAUNCHER.icon
    assert read(base, rf"{classes}\pdfeditor.pdf\Application", "ApplicationName") == (
        APP_DISPLAY_NAME
    )
    assert read(base, rf"{classes}\.pdf\OpenWithProgids", "pdfeditor.pdf") == ""
    app = rf"{classes}\Applications\Elanero.exe"
    assert read(base, app, "FriendlyAppName") == APP_DISPLAY_NAME
    assert read(base, rf"{app}\SupportedTypes", ".pdf") == ""
    # listed in Settings > Default apps
    assert read(base, r"Software\RegisteredApplications", APP_DISPLAY_NAME) == fa.CAPABILITIES
    assert read(base, rf"{fa.CAPABILITIES}\FileAssociations", ".pdf") == "pdfeditor.pdf"
    assert read(base, fa.CAPABILITIES, "ApplicationIcon") == LAUNCHER.icon

    # another copy of the exe isn't "this copy"
    other = fa.Launcher(r'"D:\Other\Elanero.exe" "%1"', "x", "Elanero.exe")
    assert not fa.is_registered(other, base=base)

    fa.unregister(LAUNCHER, base=base, notify=False)
    assert not fa.is_registered(LAUNCHER, base=base) and fa.registered_command(base=base) is None
    for path in (rf"{classes}\pdfeditor.pdf", app, fa.CAPABILITIES):
        with pytest.raises(FileNotFoundError):
            read(base, path)
    with pytest.raises(FileNotFoundError):
        read(base, rf"{classes}\.pdf\OpenWithProgids", "pdfeditor.pdf")
    with pytest.raises(FileNotFoundError):
        read(base, r"Software\RegisteredApplications", APP_DISPLAY_NAME)


def test_unregister_keeps_other_apps_and_settings(base: str) -> None:
    write(base, r"Software\Classes\.pdf\OpenWithProgids", "Acme.Pdf", "")
    write(base, r"Software\Classes\.pdf", "", "Acme.Pdf")
    write(base, r"Software\pdfeditor\pdfeditor", "theme", "dark")  # Qt's QSettings
    fa.register(LAUNCHER, base=base, notify=False)
    fa.unregister(LAUNCHER, base=base, notify=False)
    assert read(base, r"Software\Classes\.pdf\OpenWithProgids", "Acme.Pdf") == ""
    assert read(base, r"Software\Classes\.pdf") == "Acme.Pdf"
    assert read(base, r"Software\pdfeditor\pdfeditor", "theme") == "dark"


def test_unregister_clears_the_installer_default(base: str) -> None:
    write(base, r"Software\Classes\.pdf", "", "pdfeditor.pdf")  # the installer's pdfassoc task
    fa.register(LAUNCHER, base=base, notify=False)
    fa.unregister(LAUNCHER, base=base, notify=False)
    with pytest.raises(FileNotFoundError):
        read(base, r"Software\Classes\.pdf")


def test_is_default_reads_the_user_choice(base: str) -> None:
    choice = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice"
    assert not fa.is_default(LAUNCHER, base=base)
    write(base, choice, "ProgId", "Acme.Pdf")
    assert not fa.is_default(LAUNCHER, base=base)
    write(base, choice, "ProgId", "pdfeditor.pdf")
    assert fa.is_default(LAUNCHER, base=base)
    write(base, choice, "ProgId", r"Applications\Elanero.exe")
    assert fa.is_default(LAUNCHER, base=base)


def test_source_checkout_launcher() -> None:
    launcher = fa.current_launcher()
    assert launcher.exe_name is None  # tests run from source, not the frozen exe
    assert launcher.command.endswith('-m pdfeditor "%1"')
    assert launcher.icon.endswith("elanero.ico")


def test_default_apps_uri() -> None:
    assert fa.default_apps_uri() == (
        "ms-settings:defaultapps?registeredAppUser=Elanero%20-%20PDF%20Editor"
    )
