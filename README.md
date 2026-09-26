# pdfeditor

An Acrobat-style desktop PDF editor written in Python (PySide6 UI, PyMuPDF engine behind an
abstract engine layer). See [PLAN.md](PLAN.md) for the roadmap and progress, and
[AGENTS.md](AGENTS.md) for contributor/agent conventions.

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
```

## License

AGPL-3.0-or-later, because the default engine backend (PyMuPDF) is AGPL.
