"""Entry point: ``python -m pdfeditor [file.pdf ...]``."""

from __future__ import annotations

import sys


def main() -> int:
    from pdfeditor.app import run

    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
