"""The locally built PyInstaller launcher (issue #90): how builds tell it from the stock one."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bl = _load("build_bootloader")


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """A fake waf output folder with every launcher."""
    folder = tmp_path / "built"
    folder.mkdir()
    for name in bl.LAUNCHERS:
        (folder / name).write_bytes(b"local " + name.encode())
    return folder


@pytest.fixture
def target(tmp_path: Path) -> Path:
    """A fake installed PyInstaller launcher folder, holding the stock launchers."""
    folder = tmp_path / "installed"
    folder.mkdir()
    for name in bl.LAUNCHERS:
        (folder / name).write_bytes(b"stock " + name.encode())
    return folder


def test_stock_launcher_is_reported(target: Path) -> None:
    assert "prebuilt" in (bl.local_build_problem(target, "6.0") or "")


def test_installed_local_build_passes(built: Path, target: Path) -> None:
    bl.install(built, target, "6.0")
    assert bl.local_build_problem(target, "6.0") is None
    assert (target / "run.exe").read_bytes() == b"local run.exe"
    record = json.loads((target / bl.MARKER).read_text(encoding="utf-8"))
    assert set(record["files"]) == set(bl.LAUNCHERS)


def test_reinstalled_pyinstaller_is_caught(built: Path, target: Path) -> None:
    bl.install(built, target, "6.0")
    (target / "runw.exe").write_bytes(b"stock runw.exe")  # pip put the wheel's file back
    assert "runw.exe" in (bl.local_build_problem(target, "6.0") or "")


def test_other_pyinstaller_version_is_caught(built: Path, target: Path) -> None:
    bl.install(built, target, "6.0")
    assert "6.0" in (bl.local_build_problem(target, "6.1") or "")


def test_unreadable_marker_is_caught(target: Path) -> None:
    (target / bl.MARKER).write_text("{not json", encoding="utf-8")
    assert bl.local_build_problem(target, "6.0") is not None


def test_msix_build_requires_local_launcher(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    build_exe = _load("build_exe")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys.modules["build_bootloader"], "installed_dir", lambda: target)
    monkeypatch.setattr(sys.modules["build_bootloader"], "pyinstaller_version", lambda: "6.0")
    with pytest.raises(SystemExit, match=r"build_bootloader\.py"):
        build_exe.check_launcher(require=True)
    build_exe.check_launcher(require=False)  # only warns
