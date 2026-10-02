"""Entry script for the frozen executable (PyInstaller needs a file, not ``-m``)."""

import multiprocessing
import sys

from pdfeditor.__main__ import main

if __name__ == "__main__":
    # OCR runs Tesseract in a helper process (engine/mupdf/ocr_worker.py): when the frozen exe
    # is started as that process, this runs the worker instead of the app.
    multiprocessing.freeze_support()
    sys.exit(main())
