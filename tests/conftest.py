from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "generated"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-goldens",
        action="store_true",
        help="rewrite reference images (tests/golden/data, tests/golden/ui) instead of comparing",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Generate the fixture corpus once if it is missing; isolate the user data folder."""
    # Logs, crash reports and recovery copies must never touch the real profile.
    os.environ["PDFEDITOR_DATA_DIR"] = tempfile.mkdtemp(prefix="pdfeditor-test-data-")
    # Same GC policy as the app: never collect MuPDF objects outside the engine lock.
    from pdfeditor.core.engine_lock import install_manual_gc

    install_manual_gc()
    if not (FIXTURES / "text_multipage.pdf").exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_fixtures.py")], check=True)


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def fixture_pdf(fixtures_dir: Path):
    """``fixture_pdf("images")`` -> path of tests/fixtures/generated/images.pdf."""

    def get(name: str) -> Path:
        path = fixtures_dir / f"{name.removesuffix('.pdf')}.pdf"
        assert path.exists(), f"missing fixture {path}"
        return path

    return get


@pytest.fixture(autouse=True)
def _collect_garbage_under_engine_lock():
    yield
    from pdfeditor.core.engine_lock import collect

    collect()
