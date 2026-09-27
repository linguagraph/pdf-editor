"""Build the self-contained pdfeditor executable and optionally smoke-test it.

    uv run python scripts/build_exe.py            # build dist/pdfeditor.exe
    uv run python scripts/build_exe.py --test     # build, then run --self-test on the result

Fails if the executable exceeds the size budget.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "pdfeditor.spec"
DIST = ROOT / "dist"
SIZE_BUDGET_MB = 150


def exe_path() -> Path:
    return DIST / ("pdfeditor.exe" if sys.platform == "win32" else "pdfeditor")


def build() -> Path:
    subprocess.run(  # bundle English OCR data (not committed)
        [sys.executable, str(ROOT / "scripts" / "fetch_tessdata.py"), "eng"], cwd=ROOT, check=True
    )
    shutil.rmtree(ROOT / "build" / "pdfeditor", ignore_errors=True)
    start = time.perf_counter()
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)],
        cwd=ROOT,
        check=True,
    )
    exe = exe_path()
    size_mb = exe.stat().st_size / 1024 / 1024
    print(f"built {exe} ({size_mb:.1f} MB) in {time.perf_counter() - start:.0f}s")
    if size_mb > SIZE_BUDGET_MB:
        raise SystemExit(f"executable is {size_mb:.1f} MB, over the {SIZE_BUDGET_MB} MB budget")
    return exe


def self_test(exe: Path) -> int:
    """Run the frozen self-test; windowed builds have no console, so read the report file."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.txt"
        start = time.perf_counter()
        proc = subprocess.run(
            [str(exe), "--self-test", "--self-test-report", str(report)], timeout=300
        )
        elapsed = time.perf_counter() - start
        print(report.read_text(encoding="utf-8") if report.exists() else "(no report written)")
        print(f"self-test exit code {proc.returncode} in {elapsed:.1f}s (includes unpacking)")
        return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="run --self-test on the build")
    parser.add_argument("--skip-build", action="store_true", help="only test the existing exe")
    args = parser.parse_args()
    exe = exe_path() if args.skip_build else build()
    return self_test(exe) if args.test or args.skip_build else 0


if __name__ == "__main__":
    sys.exit(main())
