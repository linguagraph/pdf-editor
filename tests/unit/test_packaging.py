"""Packaging tooling: build script helpers, license notices, the About dialog."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from pdfeditor import SOURCE_URL, __version__
from pdfeditor.licenses import about_text, build_notices, license_text

ROOT = Path(__file__).resolve().parents[2]


def build_script():
    spec = importlib.util.spec_from_file_location("build_exe", ROOT / "scripts" / "build_exe.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_exe"] = module
    spec.loader.exec_module(module)
    return module


def test_notices_and_agpl() -> None:
    notices = build_notices()
    for name, license_ in (("pymupdf", "AFFERO"), ("pikepdf", "MPL-2.0"), ("PySide6", "LGPL")):
        line = next(ln for ln in notices.splitlines() if ln.lower().startswith(name.lower()))
        assert license_.lower() in line.lower(), line
    assert "numpy" not in notices.split("License texts")[0].lower()  # not bundled
    assert "Tesseract" in notices and "MuPDF" in notices
    assert "Lucide icons" in notices and "Copyright (c) 2013-present Cole Bemis" in notices
    about = about_text()
    assert SOURCE_URL in about and "Affero" in about and __version__ in about
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in license_text()


def test_sign_command() -> None:
    b = build_script()
    tool, target = Path("signtool.exe"), Path("dist/pdfeditor.exe")
    cmd = b.sign_command(
        tool, target, {"PDFEDITOR_SIGN_PFX": "c.pfx", "PDFEDITOR_SIGN_PASSWORD": "pw"}
    )
    assert cmd[:2] == [str(tool), "sign"] and cmd[cmd.index("/f") : cmd.index("/f") + 2] == [
        "/f",
        "c.pfx",
    ]
    assert "/p" in cmd and cmd[-1] == str(target) and "/tr" in cmd and "/fd" in cmd
    cmd = b.sign_command(tool, target, {"PDFEDITOR_SIGN_THUMBPRINT": "ABC"})
    assert cmd[cmd.index("/sha1") : cmd.index("/sha1") + 2] == ["/sha1", "ABC"]
    with pytest.raises(SystemExit, match="PDFEDITOR_SIGN"):
        b.sign_command(tool, target, {})


def test_installer_script(tmp_path: Path, monkeypatch) -> None:
    b = build_script()
    monkeypatch.setattr(b, "GENERATED", tmp_path)
    script = b.installer_script(Path("C:/out/pdfeditor.exe"), Path("C:/out")).read_text("utf-8")
    assert not re.search(r"@[A-Z]+@", script)  # every placeholder filled
    assert f'#define AppVersion "{__version__}"' in script
    assert r'Source: "C:\out\pdfeditor.exe"' in script or 'Source: "C:/out/pdfeditor.exe"' in script
    for section in ("[Setup]", "[Files]", "[Icons]", "[Registry]", "[Tasks]"):
        assert section in script
    assert "pdfassoc" in script and "uninsdeletekey" in script


def test_clean_env(tmp_path: Path) -> None:
    env = build_script().clean_env(tmp_path)
    assert not any(k.upper().startswith("PYTHON") for k in env)
    assert all("python" not in p.lower() for p in env["PATH"].split(";"))
    for key in ("APPDATA", "LOCALAPPDATA", "TEMP", "USERPROFILE"):
        assert Path(env[key]).is_dir() and str(tmp_path) in env[key]


@pytest.mark.gui
def test_about_dialog(qtbot) -> None:
    from pdfeditor.ui.dialogs.about import AboutDialog

    d = AboutDialog("PyMuPDF test")
    qtbot.addWidget(d)
    assert d.tabs.count() == 3
    texts = [d.tabs.widget(i).toPlainText() for i in range(3)]
    assert SOURCE_URL in texts[0] and "pymupdf" in texts[1].lower() and "AFFERO" in texts[2]
