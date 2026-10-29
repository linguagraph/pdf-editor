"""Compile PyInstaller's Windows launcher from source and install it in this environment.

    uv run python scripts/build_bootloader.py           # build and install (skips if done)
    uv run python scripts/build_bootloader.py --check   # only report which launcher is in use

``Elanero.exe`` is PyInstaller's launcher with the app attached. The prebuilt launcher that
ships in PyInstaller's wheel is byte-for-byte the one malware packed with PyInstaller uses, so
cloud scanners such as the Microsoft Store's flag unsigned apps built with it (issue #90).
A launcher compiled here from the same source has its own bytes and doesn't match that
signature.

This downloads the PyInstaller source for the installed version from PyPI, checks its SHA-256,
builds the launcher with the local MSVC (Visual Studio Build Tools with the C++ workload) and
copies it over the prebuilt one. A marker file next to it records the hashes, so
``build_exe.py`` can tell a locally built launcher from the stock one. Reinstalling or
upgrading PyInstaller restores the stock launcher; run this again afterwards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from importlib.metadata import version as dist_version
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "build" / "bootloader"
ARCH_DIR = "Windows-64bit-intel"
LAUNCHERS = ("run.exe", "runw.exe", "run_d.exe", "runw_d.exe")
MARKER = "elanero-local-build.json"
PYPI_JSON = "https://pypi.org/pypi/pyinstaller/{version}/json"


def pyinstaller_version() -> str:
    return dist_version("pyinstaller")


def installed_dir() -> Path:
    """The folder this environment's PyInstaller takes its Windows launcher from."""
    import PyInstaller

    return Path(PyInstaller.__file__).parent / "bootloader" / ARCH_DIR


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_build_problem(folder: Path, version: str) -> str | None:
    """Why ``folder`` lacks the launcher this script built for ``version`` (None: it has it)."""
    marker = folder / MARKER
    if not marker.is_file():
        return "the prebuilt launcher from PyInstaller's wheel is in use"
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"{marker.name} can't be read"
    if record.get("pyinstaller") != version:
        return f"the local launcher was built for PyInstaller {record.get('pyinstaller')}"
    for name, digest in record.get("files", {}).items():
        path = folder / name
        if not path.is_file() or sha256(path) != digest:
            return f"{name} was replaced since the local build (PyInstaller reinstalled?)"
    if set(record.get("files", {})) != set(LAUNCHERS):
        return f"{marker.name} doesn't list every launcher"
    return None


def fetch_source(version: str, work: Path = WORK) -> Path:
    """PyPI's source archive for ``version``, checked and unpacked; returns its folder."""
    meta = json.load(urllib.request.urlopen(PYPI_JSON.format(version=version), timeout=60))
    sdist = next(u for u in meta["urls"] if u["packagetype"] == "sdist")
    archive = work / sdist["filename"]
    work.mkdir(parents=True, exist_ok=True)
    if not archive.is_file() or sha256(archive) != sdist["digests"]["sha256"]:
        data = urllib.request.urlopen(sdist["url"], timeout=300).read()
        if hashlib.sha256(data).hexdigest() != sdist["digests"]["sha256"]:
            raise SystemExit(f"{sdist['filename']}: SHA-256 doesn't match PyPI")
        archive.write_bytes(data)
    source = work / f"pyinstaller-{version}"
    shutil.rmtree(source, ignore_errors=True)
    with tarfile.open(archive) as tar:
        # Only plain files and folders inside the archive: the sdist has a few symlinks in its
        # docs and tests that aren't needed, and nothing may land outside ``work``.
        members = [m for m in tar.getmembers() if m.isfile() or m.isdir()]
        for member in members:
            parts = PurePosixPath(member.name).parts
            if member.name.startswith(("/", "\\")) or ".." in parts:
                raise SystemExit(f"unsafe path in {archive.name}: {member.name}")
        tar.extractall(work, members=members)
    return source


def compile_launcher(source: Path) -> Path:
    """Build the 64-bit launchers with waf; returns the folder they're installed into."""
    subprocess.run(
        [sys.executable, "./waf", "all", "--target-arch=64bit"],
        cwd=source / "bootloader",
        check=True,
    )
    out = source / "PyInstaller" / "bootloader" / ARCH_DIR
    missing = [n for n in LAUNCHERS if not (out / n).is_file()]
    if missing:
        raise SystemExit(f"waf didn't produce {', '.join(missing)}")
    return out


def install(built: Path, target: Path, version: str) -> None:
    for name in LAUNCHERS:
        shutil.copy2(built / name, target / name)
    record = {"pyinstaller": version, "files": {n: sha256(target / n) for n in LAUNCHERS}}
    (target / MARKER).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only report the launcher in use")
    parser.add_argument("--force", action="store_true", help="rebuild even if already built")
    args = parser.parse_args()
    if sys.platform != "win32":
        print("the local launcher build is only needed on Windows; nothing to do")
        return 0
    version, target = pyinstaller_version(), installed_dir()
    problem = local_build_problem(target, version)
    if args.check:
        print(problem or f"locally built launcher in use (PyInstaller {version})")
        return 1 if problem else 0
    if problem is None and not args.force:
        print(f"locally built launcher already installed (PyInstaller {version})")
        return 0
    built = compile_launcher(fetch_source(version))
    install(built, target, version)
    print(f"installed the locally built launcher into {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
