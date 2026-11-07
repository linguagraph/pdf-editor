"""What a pull request's changes require from CI (issue #109).

Stdlib only: it runs before anything is installed. A path it doesn't recognize counts as code,
and so does an empty list, so a mistake here costs time, never a skipped test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable
from fnmatch import fnmatch

# Nothing in the app, its tests or its packages reads these.
DOCS = ("*.md", "docs/*", "LICENSE", ".gitignore", ".gitattributes", ".github/CODEOWNERS")
# These can break the installer or the MSIX package without any test noticing, so changing them
# also runs the install tests of both.
PACKAGING = (
    "packaging/*",
    "pyproject.toml",
    ".github/workflows/*",
    "scripts/build_bootloader.py",
    "scripts/build_exe.py",
    "scripts/msix_package.py",
    "src/pdfeditor/bundle.py",
    "src/pdfeditor/selftest.py",
    "src/pdfeditor/services/file_association.py",
)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch(path, pattern) for pattern in patterns)


def classify(paths: Iterable[str]) -> dict[str, bool]:
    """``app``: run the checks, tests and build. ``packaging``: also the install tests."""
    changed = [path.replace("\\", "/") for path in paths if path.strip()]
    if not changed:
        return {"app": True, "packaging": True}
    code = [path for path in changed if not _matches(path, DOCS)]
    return {"app": bool(code), "packaging": any(_matches(path, PACKAGING) for path in code)}


def changed_paths() -> list[str]:
    """What a pull request changes: its merge commit against the branch it merges into."""
    done = subprocess.run(
        ["git", "diff", "--name-only", "HEAD^1", "HEAD"], capture_output=True, text=True, check=True
    )
    return done.stdout.splitlines()


def main() -> int:
    # Anything but a pull request (the nightly run, a release) runs everything.
    pull_request = os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
    result = classify(changed_paths()) if pull_request else classify([])
    lines = [f"{name}={str(value).lower()}" for name, value in result.items()]
    print("\n".join(lines))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
