"""Build the self-contained Elanero executable, and optionally test, sign and package it.

    uv run python scripts/build_exe.py                 # build dist/Elanero.exe
    uv run python scripts/build_exe.py --test          # + run --self-test on the result
    uv run python scripts/build_exe.py --clean-env     # + self-test and a windowed launch with
                                                       #   no Python/PATH and an empty profile
    uv run python scripts/build_exe.py --sign          # + Authenticode signature (see below)
    uv run python scripts/build_exe.py --installer     # + dist/pdfeditor-<ver>-setup.exe (Inno)
    uv run python scripts/build_exe.py --onedir        # the app as a folder: dist/Elanero/
    uv run python scripts/build_exe.py --msix          # + dist/Elanero-<ver>-x64.msix (Store)
    uv run python scripts/build_exe.py --msix --sign-test  # ... signed for a test machine

Signing uses signtool from the Windows SDK and either
  PDFEDITOR_SIGN_PFX (+ PDFEDITOR_SIGN_PASSWORD)  a .pfx file, or
  PDFEDITOR_SIGN_THUMBPRINT                        a certificate in the user's store;
PDFEDITOR_SIGN_TIMESTAMP overrides the RFC 3161 timestamp server.

Fails if the executable (or the folder) exceeds the size budget.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "pdfeditor.spec"
ISS = ROOT / "packaging" / "installer.iss"
BRAND = ROOT / "src" / "pdfeditor" / "data" / "branding" / "elanero"
ICON = BRAND / "icons" / "platform" / "elanero.ico"  # exe and installer icon
DIST = ROOT / "dist"
GENERATED = ROOT / "build" / "generated"
SIZE_BUDGET_MB = 150  # the one-file exe and the MSIX (both compressed)
ONEDIR_BUDGET_MB = 300  # the unpacked folder build
DEFAULT_TIMESTAMP = "http://timestamp.digicert.com"
ISCC_GLOB = "Program Files (x86)/Inno Setup 6/ISCC.exe"


def version() -> str:
    text = (ROOT / "src" / "pdfeditor" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', text)
    assert match is not None
    return match.group(1)


def exe_path(onedir: bool = False) -> Path:
    name = "Elanero.exe" if sys.platform == "win32" else "Elanero"
    return DIST / "Elanero" / name if onedir else DIST / name


def msix_path() -> Path:
    return DIST / f"Elanero-{version()}-x64.msix"


def build_size_mb(exe: Path, onedir: bool) -> float:
    files = (f for f in exe.parent.rglob("*") if f.is_file()) if onedir else iter([exe])
    return sum(f.stat().st_size for f in files) / 1024 / 1024


def write_notices() -> Path:
    """Third-party notices bundled into the exe (shown in Help ▸ About)."""
    sys.path.insert(0, str(ROOT / "src"))
    from pdfeditor.licenses import NOTICES_FILE, build_notices

    GENERATED.mkdir(parents=True, exist_ok=True)
    out = GENERATED / NOTICES_FILE
    out.write_text(build_notices(), encoding="utf-8")
    return out


def build(onedir: bool = False) -> Path:
    subprocess.run(  # bundle English OCR data (not committed)
        [sys.executable, str(ROOT / "scripts" / "fetch_tessdata.py"), "eng"], cwd=ROOT, check=True
    )
    write_notices()
    shutil.rmtree(ROOT / "build" / "pdfeditor", ignore_errors=True)
    start = time.perf_counter()
    env = dict(os.environ, PDFEDITOR_ONEDIR="1" if onedir else "0")  # read by the spec
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)],
        cwd=ROOT,
        check=True,
        env=env,
    )
    exe = exe_path(onedir)
    size_mb = build_size_mb(exe, onedir)
    print(f"built {exe} ({size_mb:.1f} MB) in {time.perf_counter() - start:.0f}s")
    budget = ONEDIR_BUDGET_MB if onedir else SIZE_BUDGET_MB
    if size_mb > budget:
        raise SystemExit(f"the build is {size_mb:.1f} MB, over the {budget} MB budget")
    return exe


def self_test(exe: Path, env: dict[str, str] | None = None) -> int:
    """Run the frozen self-test; windowed builds have no console, so read the report file."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.txt"
        start = time.perf_counter()
        proc = subprocess.run(
            [str(exe), "--self-test", "--self-test-report", str(report)], timeout=300, env=env
        )
        elapsed = time.perf_counter() - start
        print(report.read_text(encoding="utf-8") if report.exists() else "(no report written)")
        print(f"self-test exit code {proc.returncode} in {elapsed:.1f}s (includes unpacking)")
        return proc.returncode


# -- clean environment ---------------------------------------------------------------------------
def clean_env(home: Path) -> dict[str, str]:
    """An environment like a fresh Windows account: no Python, no dev tools on PATH, empty
    profile folders (so no settings, recovery files or OCR downloads from this machine)."""
    windir = os.environ.get("SYSTEMROOT", r"C:\Windows")
    env = {
        "SystemRoot": windir,
        "windir": windir,
        "SystemDrive": os.environ.get("SYSTEMDRIVE", "C:"),
        "PATH": os.pathsep.join([rf"{windir}\System32", windir, rf"{windir}\System32\Wbem"]),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "COMSPEC": rf"{windir}\System32\cmd.exe",
        "NUMBER_OF_PROCESSORS": os.environ.get("NUMBER_OF_PROCESSORS", "4"),
        "PROCESSOR_ARCHITECTURE": os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64"),
        "USERNAME": "cleanuser",
    }
    for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "HOME"):
        folder = home / key.lower()
        folder.mkdir(parents=True, exist_ok=True)
        env[key] = str(folder)
    return env


def launch_check(exe: Path, env: dict[str, str], sample: Path, timeout: float = 30) -> float:
    """Start the app windowed with a document and wait for its window (title shows the file
    name); returns seconds. Windows only. The app is closed afterwards."""
    start = time.perf_counter()
    proc = subprocess.Popen([str(exe), "--new-instance", str(sample)], env=env)
    try:
        while time.perf_counter() - start < timeout:
            out = subprocess.run(
                ["tasklist", "/v", "/fo", "csv", "/fi", f"imagename eq {exe.name}"],
                capture_output=True,
                text=True,
                errors="replace",
            ).stdout
            if sample.name in out:
                return time.perf_counter() - start
            if proc.poll() is not None and exe.name not in out:
                raise SystemExit(f"the app exited ({proc.returncode}) before showing a window")
            time.sleep(0.2)
        raise SystemExit(f"no window showing {sample.name} within {timeout:.0f}s")
    finally:
        subprocess.run(["taskkill", "/f", "/t", "/pid", str(proc.pid)], capture_output=True)
        proc.wait(timeout=30)


def _remove(folder: Path) -> None:
    """Delete a test folder, waiting briefly for the exe (and its unpacker) to let go."""
    for _ in range(50):
        try:
            shutil.rmtree(folder)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.2)
    shutil.rmtree(folder, ignore_errors=True)


def clean_env_smoke(exe: Path, window_check: bool = True, onedir: bool = False) -> int:
    base = Path(tempfile.mkdtemp(prefix="pdfeditor-clean-"))
    try:
        copy = base / "app" / exe.name  # run from an unrelated folder, like a download
        if onedir:
            shutil.copytree(exe.parent, copy.parent)
        else:
            copy.parent.mkdir()
            shutil.copy2(exe, copy)
        env = clean_env(base / "profile")
        code = self_test(copy, env)
        if code != 0 or sys.platform != "win32" or not window_check:
            return code
        sample = base / "sample.pdf"
        shutil.copy2(ROOT / "src" / "pdfeditor" / "data" / "selftest.pdf", sample)
        seconds = launch_check(copy, env, sample)
        print(f"clean environment: window with {sample.name} after {seconds:.1f}s")
        return 0
    finally:
        _remove(base)


# -- signing and installer -------------------------------------------------------------------------
def find_tool(name: str, pattern: str) -> Path | None:
    found = shutil.which(name)
    if found:
        return Path(found)
    drive = Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\")
    matches = sorted(drive.glob(pattern))
    return matches[-1] if matches else None


def sign_command(signtool: Path, target: Path, env: dict[str, str]) -> list[str]:
    cmd = [str(signtool), "sign", "/fd", "SHA256", "/td", "SHA256"]
    cmd += ["/tr", env.get("PDFEDITOR_SIGN_TIMESTAMP", DEFAULT_TIMESTAMP)]
    if env.get("PDFEDITOR_SIGN_PFX"):
        cmd += ["/f", env["PDFEDITOR_SIGN_PFX"]]
        if env.get("PDFEDITOR_SIGN_PASSWORD"):
            cmd += ["/p", env["PDFEDITOR_SIGN_PASSWORD"]]
    elif env.get("PDFEDITOR_SIGN_THUMBPRINT"):
        cmd += ["/sha1", env["PDFEDITOR_SIGN_THUMBPRINT"]]
    else:
        raise SystemExit("set PDFEDITOR_SIGN_PFX or PDFEDITOR_SIGN_THUMBPRINT to sign")
    cmd += ["/d", "pdfeditor", str(target)]
    return cmd


def sign(target: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from msix_package import find_sdk_tool

    signtool = find_sdk_tool("signtool")
    subprocess.run(sign_command(signtool, target, dict(os.environ)), check=True)
    print(f"signed {target}")


def installer_script(exe: Path, out_dir: Path) -> Path:
    """The Inno Setup script with this build's version and paths filled in."""
    text = ISS.read_text(encoding="utf-8")
    text = text.replace("@VERSION@", version()).replace("@EXE@", str(exe))
    text = text.replace("@OUTDIR@", str(out_dir)).replace("@LICENSE@", str(ROOT / "LICENSE"))
    text = text.replace("@ICON@", str(ICON))
    GENERATED.mkdir(parents=True, exist_ok=True)
    script = GENERATED / "installer.iss"
    script.write_text(text, encoding="utf-8")
    return script


def build_installer(exe: Path) -> Path:
    iscc = find_tool("iscc", ISCC_GLOB)
    if iscc is None:
        raise SystemExit("Inno Setup 6 (ISCC.exe) not found: https://jrsoftware.org/isinfo.php")
    subprocess.run([str(iscc), "/Q", str(installer_script(exe, DIST))], check=True)
    setup = DIST / f"pdfeditor-{version()}-setup.exe"
    print(f"built {setup}")
    return setup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="run --self-test on the build")
    parser.add_argument("--skip-build", action="store_true", help="only test the existing exe")
    parser.add_argument("--clean-env", action="store_true", help="test in a clean environment")
    parser.add_argument(
        "--no-window-check",
        action="store_true",
        help="with --clean-env: skip the windowed launch (CI desktops can't show one reliably)",
    )
    parser.add_argument("--sign", action="store_true", help="Authenticode-sign the outputs")
    parser.add_argument("--installer", action="store_true", help="also build the installer")
    parser.add_argument("--onedir", action="store_true", help="build the app as a folder")
    parser.add_argument(
        "--msix", action="store_true", help="also build the Store MSIX package (implies --onedir)"
    )
    parser.add_argument(
        "--sign-test",
        action="store_true",
        help="with --msix: sign the package with a self-signed test certificate",
    )
    args = parser.parse_args()
    onedir = args.onedir or args.msix
    if args.installer and onedir:
        parser.error("the installer wraps the one-file exe: build it without --onedir/--msix")
    exe = exe_path(onedir) if args.skip_build else build(onedir)
    if args.sign:
        sign(exe)
    code = 0
    if args.test or (args.skip_build and not args.clean_env):
        code = self_test(exe)
    if code == 0 and args.clean_env:
        code = clean_env_smoke(exe, window_check=not args.no_window_check, onedir=onedir)
    if code == 0 and args.msix:
        sys.path.insert(0, str(ROOT / "scripts"))
        from msix_package import build_msix

        package = build_msix(exe.parent, msix_path(), version(), sign=args.sign_test)
        size_mb = package.stat().st_size / 1024 / 1024
        if size_mb > SIZE_BUDGET_MB:
            raise SystemExit(f"the MSIX is {size_mb:.1f} MB, over the {SIZE_BUDGET_MB} MB budget")
    if code == 0 and args.installer:
        setup = build_installer(exe)
        if args.sign:
            sign(setup)
    return code


if __name__ == "__main__":
    sys.exit(main())
