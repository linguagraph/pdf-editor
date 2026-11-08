# Elanero - PDF Editor

Elanero (internal name `pdfeditor`, executable `Elanero.exe`) is an Acrobat-style desktop PDF editor written in Python (PySide6 UI, PyMuPDF engine behind an
abstract engine layer). See [PLAN.md](PLAN.md) for the roadmap and progress, and
[AGENTS.md](AGENTS.md) for contributor/agent conventions.

## Features

View, search and print (vector printing) · comments and markup (notes, text boxes, callouts,
shapes, ink, standard and custom image stamps) · page organizing, split, merge, headers and
footers, Bates numbering, watermarks · editing text and images in place · redaction with leak
verification and document sanitizing · OCR (built-in Tesseract, deskew) · export to Word,
Excel, HTML, Markdown, text and images · reduce file size · compare documents · password
security · digital signatures (PAdES, with USB tokens and smart cards through Windows or
PKCS#11, or a PFX file) · request signatures from several recipients (the app sends nothing; you
email the file) · PDF/A-2b conversion · accessibility checker, tag editor and experimental
auto-tagging · keyboard shortcuts and a command palette (Ctrl+Shift+P).

## Screenshots

Elanero running on Windows with a PDF open. The Protect ribbon includes document signing controls; the screenshot does not show a signed document.

![Elanero displaying a PDF with page thumbnails and document signing controls](docs/images/elanero-opened-pdf-signing.png)

### Comments and markup

The Comment ribbon provides text markup, notes, callouts, stamps and drawing tools. Document-dependent controls are disabled on the welcome screen shown here.

![Elanero Comment ribbon](docs/images/elanero-03-comments.png)

### Page organization

The Organize ribbon exposes page operations and document assembly tools.

![Elanero Organize ribbon](docs/images/elanero-02-organize.png)

### OCR and other tools

The Tools ribbon exposes OCR, export, Office-to-PDF creation, comparison and document checking tools.

![Elanero Tools ribbon](docs/images/elanero-04-tools.png)

## Download

Elanero is a single self-contained `Elanero.exe`: no Python or other installation needed.
Every CI build of a pull request or of `main` uploads it as the `pdfeditor-exe-<commit>`
workflow artifact. Run `Elanero.exe --self-test --self-test-report report.txt` to check an
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
uv run python scripts/build_exe.py --test   # -> dist/Elanero.exe, then runs its self-test
uv run python scripts/build_exe.py --skip-build --clean-env  # self-test + launch with no Python
uv run python scripts/build_exe.py --installer              # + setup.exe (needs Inno Setup 6)
uv run python scripts/build_exe.py --sign                   # Authenticode (see the script)
```

## License

AGPL-3.0-or-later (see [LICENSE](LICENSE)), because the default engine backend (PyMuPDF) is
AGPL. Help ▸ About lists the bundled third-party components and their licenses.
