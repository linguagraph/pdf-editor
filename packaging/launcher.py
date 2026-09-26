"""Entry script for the frozen executable (PyInstaller needs a file, not ``-m``)."""

import sys

from pdfeditor.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
