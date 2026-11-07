"""What CI runs for a pull request: skipping is safe only for what can't affect the app."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import ci_changes


@pytest.mark.parametrize(
    "paths",
    [
        ["PLAN.md"],
        ["README.md", "docs/microsoft-store-submission.md"],
        ["LICENSE"],
        [".github/CODEOWNERS"],
    ],
)
def test_docs_alone_skip_everything(paths: list[str]) -> None:
    assert ci_changes.classify(paths) == {"app": False, "packaging": False}


@pytest.mark.parametrize(
    "paths",
    [
        ["src/pdfeditor/ui/main_window.py"],
        ["tests/unit/test_msix.py"],
        ["scripts/make_fixtures.py"],
        ["PLAN.md", "src/pdfeditor/app.py"],
        ["something/new.txt"],
    ],
)
def test_code_runs_the_tests_but_not_the_install_tests(paths: list[str]) -> None:
    assert ci_changes.classify(paths) == {"app": True, "packaging": False}


@pytest.mark.parametrize(
    "path",
    [
        "packaging/pdfeditor.spec",
        "packaging/installer.iss",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        "scripts/build_exe.py",
        "scripts/msix_package.py",
        "src/pdfeditor/selftest.py",
    ],
)
def test_packaging_changes_run_the_install_tests(path: str) -> None:
    assert ci_changes.classify([path]) == {"app": True, "packaging": True}


def test_no_changes_found_runs_everything() -> None:
    """If the diff came back empty, something's wrong: never take that as nothing to test."""
    assert ci_changes.classify([]) == {"app": True, "packaging": True}
    assert ci_changes.classify(["", "  "]) == {"app": True, "packaging": True}


def test_a_nightly_or_release_run_tests_everything(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_call")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(ci_changes, "changed_paths", lambda: ["PLAN.md"])
    assert ci_changes.main() == 0
    assert output.read_text("utf-8").split() == ["app=true", "packaging=true"]
