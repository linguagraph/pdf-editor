from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "generated"


def pytest_sessionstart(session: pytest.Session) -> None:
    """Generate the fixture corpus once if it is missing."""
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
