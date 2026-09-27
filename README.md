# pdfeditor

An Acrobat-style desktop PDF editor written in Python (PySide6 UI, PyMuPDF engine behind an
abstract engine layer). See [PLAN.md](PLAN.md) for the roadmap and progress, and
[AGENTS.md](AGENTS.md) for contributor/agent conventions.

## Features

View, search and print (vector printing) · comments and markup (notes, text boxes, callouts,
shapes, ink, standard and custom image stamps) · page organizing, split, merge, headers and
footers, Bates numbering, watermarks · editing text and images in place · redaction with leak
verification and document sanitizing · OCR (built-in Tesseract, deskew) · export to Word,
Excel, HTML, Markdown, text and images · reduce file size · compare documents · password
security · PDF/A-2b conversion · accessibility checker, tag editor and experimental
auto-tagging · keyboard shortcuts and a command palette (Ctrl+Shift+P).

## Download

pdfeditor is a single self-contained `pdfeditor.exe`: no Python or other installation needed.
Every CI build of a pull request or of `main` uploads it as the `pdfeditor-exe-<commit>`
workflow artifact. Run `pdfeditor.exe --self-test --self-test-report report.txt` to check an
installation.

## Setup (development)

```sh
uv venv --python 3.11 .venv
uv pip install -e ".[dev]"
uv run python scripts/make_fixtures.py   # generate test PDFs
uv run pytest
uv run python -m pdfeditor [file.pdf]
```

## Build the executable

```sh
uv pip install -e ".[build]"
uv run python scripts/build_exe.py --test   # -> dist/pdfeditor.exe, then runs its self-test
uv run python scripts/build_exe.py --skip-build --clean-env  # self-test + launch with no Python
uv run python scripts/build_exe.py --installer              # + setup.exe (needs Inno Setup 6)
uv run python scripts/build_exe.py --sign                   # Authenticode (see the script)
```

## License

AGPL-3.0-or-later (see [LICENSE](LICENSE)), because the default engine backend (PyMuPDF) is
AGPL. Help ▸ About lists the bundled third-party components and their licenses.
