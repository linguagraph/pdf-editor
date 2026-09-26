# pdfeditor

An Acrobat-style desktop PDF editor written in Python (PySide6 UI, PyMuPDF engine behind an
abstract engine layer). See [PLAN.md](PLAN.md) for the roadmap and progress, and
[AGENTS.md](AGENTS.md) for contributor/agent conventions.

## Setup

```sh
uv venv --python 3.11 .venv
uv pip install -e ".[dev]"
uv run python scripts/make_fixtures.py   # generate test PDFs
uv run pytest
uv run python -m pdfeditor [file.pdf]
```

## License

AGPL-3.0-or-later, because the default engine backend (PyMuPDF) is AGPL.
