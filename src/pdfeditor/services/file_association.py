"""Register Elanero as an editor for PDF files with Windows (per user, no admin rights).

Registration puts Elanero in Explorer's "Open with" list and in Settings ▸ Default apps, and
gives the PDF file type Elanero's icon once Elanero is the chosen default. Since Windows 8 an
app can't make itself the default handler (the user's choice is hash-protected), so the last
step is the user's, in Windows Settings: :func:`default_apps_uri` opens the right page.

The keys match ``packaging/installer.iss`` (same ``pdfeditor.pdf`` ProgID), so the in-app
option and the installer's "Open PDF files" task describe the same registration. Everything
lives under ``HKEY_CURRENT_USER``; ``base`` puts it below a scratch key instead (tests).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from pdfeditor import APP_DISPLAY_NAME
from pdfeditor.bundle import app_command, data_path, is_frozen

log = logging.getLogger(__name__)

PROG_ID = "pdfeditor.pdf"
EXTENSION = ".pdf"
TYPE_NAME = "PDF document"
DESCRIPTION = "View, edit, comment on, organize and convert PDF documents."
CAPABILITIES = r"Software\pdfeditor\Capabilities"
_CLASSES = r"Software\Classes"
_REGISTERED_APPS = r"Software\RegisteredApplications"
_USER_CHOICE = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice"
_SHCNE_ASSOCCHANGED = 0x08000000
_SHCNF_FLUSH = 0x1000


@dataclass(frozen=True)
class Launcher:
    """How Windows starts this copy of the app for a file."""

    command: str  # shell\open\command, with "%1" for the file
    icon: str  # DefaultIcon: "path,index" or an .ico path
    exe_name: str | None  # for Classes\Applications\<exe>; None from a source checkout


def current_launcher() -> Launcher:
    """This copy: the frozen exe, or ``pythonw -m pdfeditor`` from a source checkout."""
    program, *args = app_command()
    command = " ".join([f'"{program}"', *args, '"%1"'])
    if is_frozen():
        exe = Path(program)
        return Launcher(command, f"{exe},0", exe.name)
    icon = data_path("branding", "elanero", "icons", "platform", "elanero.ico")
    return Launcher(command, str(icon), None)


def is_supported() -> bool:
    return sys.platform == "win32"


def _key(base: str, path: str) -> str:
    return f"{base}\\{path}" if base else path


def register(launcher: Launcher | None = None, *, base: str = "", notify: bool = True) -> None:
    """Offer this copy for PDF files: "Open with", Default apps, the file type's icon."""
    if sys.platform != "win32":
        raise OSError("file associations are only supported on Windows")
    import winreg

    launcher = launcher or current_launcher()
    classes = _key(base, _CLASSES)

    def put(path: str, values: dict[str, str]) -> None:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE) as key:
            for name, value in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    prog = f"{classes}\\{PROG_ID}"
    put(prog, {"": TYPE_NAME, "FriendlyTypeName": TYPE_NAME})
    put(f"{prog}\\DefaultIcon", {"": launcher.icon})
    put(f"{prog}\\shell\\open\\command", {"": launcher.command})
    put(
        f"{prog}\\Application",
        {"ApplicationName": APP_DISPLAY_NAME, "ApplicationIcon": launcher.icon},
    )
    put(f"{classes}\\{EXTENSION}\\OpenWithProgids", {PROG_ID: ""})
    if launcher.exe_name:
        app = f"{classes}\\Applications\\{launcher.exe_name}"
        put(app, {"FriendlyAppName": APP_DISPLAY_NAME})
        put(f"{app}\\DefaultIcon", {"": launcher.icon})
        put(f"{app}\\shell\\open\\command", {"": launcher.command})
        put(f"{app}\\SupportedTypes", {EXTENSION: ""})
    capabilities = _key(base, CAPABILITIES)
    put(
        capabilities,
        {
            "ApplicationName": APP_DISPLAY_NAME,
            "ApplicationDescription": DESCRIPTION,
            "ApplicationIcon": launcher.icon,
        },
    )
    put(f"{capabilities}\\FileAssociations", {EXTENSION: PROG_ID})
    put(_key(base, _REGISTERED_APPS), {APP_DISPLAY_NAME: CAPABILITIES})
    log.info("registered for %s files: %s", EXTENSION, launcher.command)
    if notify:
        _notify_shell()


def unregister(launcher: Launcher | None = None, *, base: str = "", notify: bool = True) -> None:
    """Undo :func:`register`. Values other apps own (the .pdf key itself) are left alone."""
    if sys.platform != "win32":
        raise OSError("file associations are only supported on Windows")
    import winreg

    launcher = launcher or current_launcher()
    hkcu = winreg.HKEY_CURRENT_USER
    classes = _key(base, _CLASSES)

    def delete_tree(path: str) -> None:
        try:
            with winreg.OpenKey(hkcu, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
                while True:
                    try:
                        child = winreg.EnumKey(key, 0)
                    except OSError:
                        break
                    delete_tree(f"{path}\\{child}")
            winreg.DeleteKey(hkcu, path)
        except FileNotFoundError:
            pass

    def delete_value(path: str, name: str, only_if: str | None = None) -> None:
        try:
            with winreg.OpenKey(hkcu, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
                if only_if is not None and winreg.QueryValueEx(key, name)[0] != only_if:
                    return
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass

    delete_tree(f"{classes}\\{PROG_ID}")
    delete_value(f"{classes}\\{EXTENSION}\\OpenWithProgids", PROG_ID)
    # The installer's "Open PDF files" task points .pdf straight at the ProgID; don't leave
    # it dangling.
    delete_value(f"{classes}\\{EXTENSION}", "", only_if=PROG_ID)
    if launcher.exe_name:
        delete_tree(f"{classes}\\Applications\\{launcher.exe_name}")
    delete_tree(_key(base, CAPABILITIES))  # not its parent: Qt keeps the settings there
    delete_value(_key(base, _REGISTERED_APPS), APP_DISPLAY_NAME)
    log.info("unregistered for %s files", EXTENSION)
    if notify:
        _notify_shell()


def registered_command(*, base: str = "") -> str | None:
    """The command Windows runs for the ProgID, or None when it isn't registered."""
    if sys.platform != "win32":
        return None
    import winreg

    path = _key(base, f"{_CLASSES}\\{PROG_ID}\\shell\\open\\command")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value = winreg.QueryValueEx(key, "")[0]
    except OSError:
        return None
    return value if isinstance(value, str) else None


def is_registered(launcher: Launcher | None = None, *, base: str = "") -> bool:
    """Is *this* copy registered (not another copy of the exe somewhere else)?"""
    command = registered_command(base=base)
    launcher = launcher or current_launcher()
    return command is not None and command.casefold() == launcher.command.casefold()


def is_default(launcher: Launcher | None = None, *, base: str = "") -> bool:
    """Has the user picked Elanero as the default app for PDF files?"""
    if sys.platform != "win32":
        return False
    import winreg

    launcher = launcher or current_launcher()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _key(base, _USER_CHOICE)) as key:
            prog_id = winreg.QueryValueEx(key, "ProgId")[0]
    except OSError:
        return False
    ours = {PROG_ID.casefold()}
    if launcher.exe_name:
        ours.add(f"applications\\{launcher.exe_name}".casefold())
    return isinstance(prog_id, str) and prog_id.casefold() in ours


def default_apps_uri() -> str:
    """Windows Settings ▸ Default apps, on Elanero's page where Windows supports it."""
    return "ms-settings:defaultapps?registeredAppUser=" + quote(APP_DISPLAY_NAME)


def _notify_shell() -> None:
    """Tell Explorer the associations changed, so icons and "Open with" refresh now."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SHChangeNotify(_SHCNE_ASSOCCHANGED, _SHCNF_FLUSH, None, None)
    except OSError:
        log.warning("couldn't notify the shell of the association change", exc_info=True)
