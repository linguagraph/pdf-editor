# AGENTS.md

Guidance for AI coding agents and human contributors working in this repository.

## What this project is

An Acrobat-style desktop PDF editor in Python: PySide6 (Qt 6) UI on top of an **abstract PDF
engine layer**, implemented first with PyMuPDF. The roadmap and live progress are in
[PLAN.md](PLAN.md). Forms authoring and digital signatures are **out of scope** for now, but
existing forms and signatures must render and survive saving.

## Progress tracking (required)

- `PLAN.md` is the single source of truth for progress. When you finish a todo item, tick its
  checkbox (`- [x]`) in the same change. Only tick an item when its tests and checks pass.
- If an item is done only partially, leave it unticked and append a short note, e.g.
  `- [ ] … _(partial: X done; Y pending)_`.
- Work phases in order unless the plan says otherwise. Don't start UI for a feature before its
  engine/service layer has tests.

## Setup and commands

```sh
uv venv --python 3.11 .venv
uv pip install -e ".[dev]"            # add ",ocr,export" for optional features
uv run python scripts/make_fixtures.py  # regenerate tests/fixtures/generated (git-ignored)
uv run pytest                          # all tests; GUI tests need QT_QPA_PLATFORM=offscreen headless
uv run pytest -m "not slow"            # quick loop
uv run ruff check . && uv run ruff format .
uv run lint-imports                    # architecture contracts
uv run mypy
uv run python -m pdfeditor [file.pdf]  # launch the app
uv run python -m pdfeditor --self-test # end-to-end smoke test (no window)
uv pip install -e ".[build]" && uv run python scripts/build_exe.py --test  # dist/pdfeditor.exe
```

Before calling a task done, run: `ruff check`, `ruff format --check`, `lint-imports`, `mypy`,
and `pytest`. All must pass.

## Architecture rules (enforced)

Layers, top to bottom. A layer may import only from layers below it (import-linter contract
"Layered architecture"):

```
pdfeditor.ui        PySide6 widgets, tools, panels, dialogs
pdfeditor.services  feature logic (search, redaction, OCR, compare, export, ...)
pdfeditor.core      DocumentSession, undo/redo commands, jobs, render cache, autosave
pdfeditor.engine    base.py Protocols + backends (engine/mupdf) + contentstream parser
pdfeditor.model     pure-Python dataclasses: geometry, text, annotations, outline, ...
```

1. **Engine isolation.** `pymupdf`/`fitz` may be imported **only** inside
   `pdfeditor/engine/mupdf/`. Everything else talks to the `engine/base.py` Protocols and
   `model/` types. Libraries that themselves depend on PyMuPDF (`pdf2docx`, `pymupdf4llm`) are
   also backend-only. Dev scripts in `scripts/` and tests in `tests/engine/mupdf*` may use
   PyMuPDF directly.
2. **Capabilities, not isinstance.** Features check `engine.capabilities` flags; the UI hides or
   disables actions that the active engine can't do.
3. **`model/` stays pure.** No Qt, no engine imports, no I/O. Frozen dataclasses where practical.
4. **Qt stays in `ui/`**, with narrow exceptions in `core/` for `QThreadPool`/`QUndoStack`
   wrappers. Services must be usable headless (CLI/tests).
5. **Every mutation goes through a Command** (`core/commands/`) so it's undoable. Reversible
   ops implement a real inverse; destructive ops (content edit, redaction, OCR, optimize) use
   the disk-backed `SnapshotCommand`.
6. **Thread safety.** MuPDF isn't thread-safe, not even across documents. Every engine call
   holds `session.lock`, which is the process-wide `ENGINE_LOCK` (`core/engine_lock.py`).
   Automatic cyclic GC is disabled; `engine_lock.collect()` runs it under the lock (a window
   timer, and after each test). Never keep PyMuPDF objects outside the engine package, since
   freeing one outside the lock can corrupt MuPDF's heap. Mutations bump the page revision so
   cached tiles are invalidated.
7. **Safe saving.** Write to a temp file, verify by reopening, then atomically replace. Use
   incremental save when the document has signatures. Never silently flatten forms/signatures.

## Self-contained executable (required)

The product ships as one `pdfeditor.exe` (PyInstaller one-file, `packaging/pdfeditor.spec`).
No feature may need Python, an installer or an external tool at runtime.

- Bundled files go in `src/pdfeditor/data/` and are read only through
  `pdfeditor.bundle.data_path()`. Never use paths relative to the source tree or tools on PATH.
- A new runtime dependency must work frozen. Check that its imports are found (add
  `hiddenimports` for lazy or dynamic imports), that its data files are collected, and that
  nothing under `DEV_EXCLUDES` is needed. If you start using a package excluded there (such as
  numpy), remove it from the list.
- Extend `pdfeditor/selftest.py` when you add a subsystem that could break only when frozen
  (new native library, bundled data, plugin).
- `uv run python scripts/build_exe.py --test` builds the exe and runs `--self-test`. CI does the
  same on every PR (size budget: 150 MB).
- External programs (LibreOffice, Ghostscript, veraPDF) may only power optional extras, detected
  at runtime, with a clear message when they're missing.

## Coding conventions

- Python ≥3.11, `from __future__ import annotations`, full type hints. `mypy --strict` applies to
  `model/`, `engine/base.py` and `core/`.
- Ruff config in `pyproject.toml` (line length 100). Match surrounding code's naming and comment
  density; docstrings explain *why*, not *what*.
- Use `logging.getLogger(__name__)`; no `print` in the package.
- Page coordinates everywhere above the engine are in the page's **visible space**: points,
  origin top-left, with the page's /Rotate and CropBox applied (what `Page.rect` describes and
  `Page.render` draws). Backends convert their native coordinates (MuPDF reports text, links and
  annotations unrotated). View rotation and zoom are applied only in `ui/view/`.
- Long-running work (OCR, compare, export, optimize, big searches) runs as a cancellable `Job`
  with progress, never on the GUI thread.

## Testing conventions

- Fixtures: use the `fixture_pdf("name")` pytest fixture (see `tests/conftest.py`). Add new
  synthetic fixtures to `scripts/make_fixtures.py`; never commit generated PDFs. Real-world
  samples go in `tests/fixtures/real/` with their source and license noted in its README.
- **Round-trip rule:** every editing feature needs a test that edits → saves → reopens →
  asserts, and the saved file must open in **pikepdf** and render in **pypdfium2** (independent
  implementations), not just in MuPDF.
- Engine behavior is specified by contract tests in `tests/engine/` written against the
  Protocols, so a future permissive backend can reuse them.
- GUI tests use `pytest-qt` (`qtbot`) and the `gui` marker; mark slow tests `slow`.
- Rendering golden images live in `tests/golden/`; compare with a tolerance, not byte equality.
- Redaction features must include a leak test (re-extract text / inspect pixels under marks).

## Licensing

The default backend (PyMuPDF) is AGPL, so the project is AGPL-3.0-or-later. Don't add
dependencies with licenses that conflict with AGPL, and record every new runtime dependency in
`pyproject.toml` (optional features go in an extra, not in core `dependencies`).

## Git workflow

- One branch per plan phase (`phase-N-short-name`), branched from an up-to-date `main`.
- When the phase's checks pass: commit, push, open a PR against `main`, review it, then merge
  (squash) and delete the branch. Commit messages and PR bodies follow the repo's attribution
  settings.
- After each phase is merged, build the executable from the updated `main`:
  `uv run python scripts/build_exe.py --test` (writes `dist/pdfeditor.exe`, must end with
  `RESULT: OK`), and report the size and self-test result.
- If a pre-commit hook modifies files and aborts the commit, `git add` and run the same
  `git commit` again. Never use `--amend` for this: it amends the previous (possibly merged)
  commit. Never rewrite or force-push shared history; fix mistakes with new commits.
- GitHub doesn't let a PR's author approve it. When the reviewer is the same account, post the
  review with `gh pr review --comment` (verdict, findings, follow-ups), fix findings on the
  branch, wait for green CI, then merge.

## Things to avoid

- Don't import PyMuPDF outside `engine/mupdf/` (CI will fail).
- Don't mutate a document outside a Command, and don't block the GUI thread with engine work.
- Don't overwrite a user's file without the temp-then-swap save path.
- Don't tick PLAN.md items for untested or partial work.
