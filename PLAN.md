# PDF Editor in Python — Implementation Plan

## Context

The goal is an Acrobat-style desktop PDF editor written in Python, built from scratch in the empty `C:\LinguaGraph_Work\Dev\pdf-editor` (Python 3.11 is installed).

**Can it be done?** Yes, with honest limits. A capable editor covering viewing, annotation, page organization, in-place content editing, redaction, OCR, compare, optimize, conversion, security, PDF/A and accessibility checks can be built in Python on top of mature C engines. "Full Acrobat parity" is not a realistic target: Adobe has spent decades on edge cases such as reflowing edited text in subset fonts, the full PDF 2.0 spec and auto-tagging. The plan aims for parity on the common workflows and documents where it falls short.

**Decisions made with you:**
- **Engine:** an abstract engine layer, implemented first with PyMuPDF (AGPL). No `pymupdf` import is allowed outside the backend package, so a permissive backend (pypdfium2 + pikepdf) can be added later.
- **App type:** a desktop app with PySide6 (Qt 6, LGPL), packaged for Windows.
- **In scope:** core view/annotate/pages, content editing, and the advanced/pro features.
- **Distribution: a self-contained executable** (decided after Phase 3). Users run one `pdfeditor.exe` with no Python, no installer and no separately installed tools. Everything every in-scope feature needs is bundled: Qt, MuPDF, qpdf/pikepdf, fonts, ICC profiles and OCR language data. External programs (LibreOffice, Ghostscript, veraPDF) may only add optional extras, never a core feature. The frozen build is built and smoke-tested in CI from Phase P on, so packaging problems show up early rather than at release.
- **Out of scope for now:** creating form fields and digital signatures. Existing forms and signatures must still render and be kept intact on save (no flattening, and incremental save where signatures exist).

---

## Architecture

```
pdf-editor/
  pyproject.toml            # hatchling, deps, ruff/mypy/pytest/import-linter config
  src/pdfeditor/
    __main__.py             # `python -m pdfeditor [file.pdf]`
    app.py                  # QApplication bootstrap, settings, logging, crash handler
    model/                  # ENGINE-NEUTRAL data types (pure Python, no Qt, no fitz)
      geometry.py           # Point, Rect, Quad, Matrix
      color.py, fonts.py    # Color, FontInfo, FontMatch
      annotations.py        # AnnotationModel (+ subtypes), Reply, Status
      text.py               # Char, Span, Line, Block, TextPage, SearchHit
      objects.py            # PageObject: TextObj, ImageObj, PathObj (for content editing)
      outline.py, metadata.py, page_ops.py
    engine/
      base.py               # Protocols: Engine, Document, Page, Renderer, TextService,
                            #   AnnotationService, ContentEditor, Redactor, Optimizer,
                            #   SecurityService, Capabilities (feature flags)
      registry.py           # get_engine("mupdf"); capability queries
      mupdf/                # ONLY place allowed to import pymupdf
        document.py, page.py, render.py, text.py, annots.py,
        content.py, redact.py, optimize.py, security.py, convert.py
      contentstream/        # own engine-neutral PDF content-stream lexer/parser/serializer
        lexer.py, parser.py, ops.py, writer.py, graphics_state.py
    core/
      session.py            # DocumentSession: engine doc + undo stack + dirty + file path
      commands/             # Command pattern: base.py, annotation_cmds.py, page_cmds.py,
                            #   content_cmds.py, snapshot.py (disk-backed snapshots)
      jobs.py               # Background Job API (progress, cancel) on QThreadPool
      autosave.py           # periodic recovery files + restore on startup
      render_cache.py       # LRU tile cache keyed (doc_id, page, zoom, tile, revision)
    services/               # feature logic built on the engine interfaces
      search.py, pages.py, bookmarks.py, stamping.py (header/footer/Bates/watermark),
      ocr.py, redaction.py, sanitize.py, compare.py, optimize.py, export/,
      pdfa.py, accessibility.py, security.py, fonts.py (matching/fallback via fontTools)
    ui/
      main_window.py        # menus, ribbon-style toolbars, tabbed documents, docks
      view/                 # DocumentView (QGraphicsView), PageItem, tiling, overlays
      tools/                # Tool state machine: Hand, SelectText, SelectObject,
                            #   Highlight, Note, FreeText, Ink, Shape, EditText,
                            #   AddImage, Redact, Crop, Measure(later)
      panels/               # Thumbnails, Bookmarks, Comments, Search, Layers,
                            #   Attachments, Properties inspector, Tags (a11y)
      organizer/            # page grid view (drag-and-drop reorder, insert, delete)
      dialogs/              # Open/Save, Password, Print, Split, Merge, Optimize, OCR,
                            #   Export, Compare, Security, Preferences, Header/Footer…
      resources/            # icons (SVG), qss themes (light/dark), translations
  tests/
    fixtures/ (generated + curated corpus), unit/, engine/, services/, ui/, golden/
  scripts/make_fixtures.py, scripts/build_installer.ps1
```

**Key design rules**
1. **Engine isolation.** `ui` → `services`/`core` → `engine.base` protocols, and the rule is enforced by an **import-linter** contract in CI. Only `engine/mupdf` imports `pymupdf`. Libraries that depend on PyMuPDF (`pdf2docx`, `pymupdf4llm`) sit behind the mupdf backend as optional exporters.
2. **Capabilities.** Each engine publishes `Capabilities` (e.g. `edit_text`, `redact`, `ocr`), and the UI turns actions on or off from these flags. This keeps a future permissive backend workable.
3. **Undo/redo is a hybrid.** Operations that can be reversed exactly (annotations, page ops, bookmarks, metadata) get their own inverse commands. Destructive operations (content edits, redaction, OCR, optimize) use `SnapshotCommand`, which serializes the document to a temp file (`tobytes(garbage=0)`) before applying the change. Undo depth and disk usage are configurable.
4. **Threading.** A MuPDF `Document` is not thread-safe. Each session has one lock, and rendering runs on worker threads with cached per-page display lists. Every mutation bumps `page.revision`, which invalidates cached tiles. Long jobs (OCR, compare, export) run as `Job`s that report progress and can be cancelled.
5. **Saving.** The editor uses incremental save when the file is signed or when the user chooses it, and a full rewrite (garbage collection and deflate) otherwise. It never overwrites the source file until the temp file has been written and verified, then swaps them atomically.

**Core dependencies:** `pymupdf`, `PySide6`, `pikepdf` (linearize, low-level repair, struct tree), `fontTools` (glyph coverage, subsetting), `Pillow`, `numpy`. Bundled in the executable as well: `pdf2docx`, `openpyxl`, `opencv-python-headless` (deskew), Tesseract language data (`tessdata`; MuPDF has the Tesseract engine built in, so no Tesseract install is needed), and an sRGB ICC profile. Optional external tools that are used only if found and never required: LibreOffice (Office import), Ghostscript and veraPDF (extra PDF/A validation). **Build:** PyInstaller one-file (evaluate Nuitka for startup time).
**Dev dependencies:** `pytest`, `pytest-qt`, `pytest-benchmark`, `hypothesis`, `ruff`, `mypy`, `import-linter`, `pre-commit`, `pyinstaller`.

---

## Implementation phases (todo list)

Progress is tracked here: see AGENTS.md for the rules. **Current phase: P (Self-contained executable), then 4.**

Sizes: S ≈ days, M ≈ 1–2 weeks, L ≈ 3–4 weeks, XL ≈ 5+ weeks, for one developer.

### Phase 0: Foundations (S)
- [x] `git init`, `.gitignore`, `pyproject.toml` (src layout, Python ≥3.11), `uv`/venv setup doc in README
- [x] Configure ruff, mypy (strict for `model/`, `engine/base.py`, `core/`), pytest, pytest-qt
- [x] import-linter contracts (engine isolation, layers `ui > services > core > engine > model`)
- [x] pre-commit hooks, plus a GitHub Actions CI matrix (windows-latest; ubuntu for headless tests with `QT_QPA_PLATFORM=offscreen`)
- [x] `scripts/make_fixtures.py`: generate test PDFs (multi-page text, images, vector art, annotations, outlines, encrypted, rotated pages, CJK text, subset fonts, scanned, broken xref, large 1000-page doc). RTL fixture deferred until a bundled Arabic/Hebrew font is chosen
- [ ] Curate a small corpus of real-world PDFs (scanned, broken xref, forms, signed) in `tests/fixtures/real/` with sources noted _(partial: synthetic `scanned` and `broken_xref` fixtures generated, and `tests/fixtures/real/README.md` holds the sourcing table; real forms/signed samples still need to be sourced)_
- [x] Logging setup, plus a global exception hook that shows a dialog and writes a crash log

### Phase 1: Engine abstraction and PyMuPDF backend (M)
- [x] `model/` dataclasses: geometry, color, text hierarchy, annotation models, outline, metadata
- [x] `engine/base.py` Protocols and `Capabilities`; `engine/registry.py`
- [x] mupdf backend: open (path/bytes), password prompt callback, repair on open, page count and sizes, rotation, boxes (Media/Crop/Trim/Bleed)
- [x] Rendering: `render(page, matrix, clip, alpha, annots=True)` → RGBA bytes, plus display-list caching
- [x] Text: `TextPage` extraction (chars/spans/lines/blocks with bboxes, font, size, color), search with quads (case-sensitive engine search covers single-line hits; regex/multi-line search belongs to the Phase 3 search service)
- [x] Metadata and XMP read/write; outline read/write; links read; embedded files list
- [x] Save: full, incremental, save-as, with atomic temp-then-swap and validation reopen
- [x] Engine contract tests (`tests/engine/`) written against the Protocol, so a future backend reuses them

### Phase 2: Viewer (L)
- [x] `MainWindow`: menu bar, ribbon-style tool tabs (Home / Comment / Edit / Organize / Protect / Tools; the ribbon framework ships with Home and View, and each later phase adds its own tab), status bar, tabbed multi-document UI, recent files, drag-and-drop open, single-instance file opening
- [x] `DocumentView` (QGraphicsView): continuous or single-page layout, one- or two-page spreads, cover mode, smooth scroll, zoom (Ctrl+wheel, presets, fit width/page/actual), rotate view
- [x] Tiled asynchronous rendering (256–512 px tiles) with low-res placeholders, a DPI-aware device pixel ratio, and an LRU cache with a memory cap
- [x] Thumbnails panel (lazy, virtualized), Bookmarks panel (tree, click-to-navigate), Attachments panel, Layers (OCG) panel with visibility toggles (engine gained `layers()`/`set_layer_visible()` and attachment extraction)
- [x] Navigation: page number box, first/prev/next/last, history back/forward, go-to-page, page labels
- [x] Clickable links (internal destinations, URIs with a confirmation prompt)
- [x] Light/dark theme, plus an optional "night reading" inverted render
- [x] Document properties dialog (metadata, fonts list, PDF version, security summary)

### Phase 3: Text selection, search, print (M)
- [x] SelectText tool: glyph-accurate selection across lines/columns, double-click word, triple-click line, copy as plain text
- [x] Search panel: whole doc, case/whole-word/regex, result list with context, highlight overlays, F3 next/prev, runs incrementally as a background job
- [x] Print: QPrinter with page range, fit/actual size, render at printer DPI, annotations toggle, print preview (pages print as 300 dpi images; vector printing is a Phase 16 follow-up)
- [x] Rendering golden tests (perceptual tolerance), plus a benchmark for first paint of page 1 and scrolling a 1000-page doc (`tests/benchmarks`; open 0.5 ms, first paint ~120 ms, cold full-text search ~1 s)

### Phase P: Self-contained executable, early (S–M), done next, before Phase 4
- [ ] `resources.py`: one helper to find bundled data (`importlib.resources` / `sys._MEIPASS`); no feature reads files relative to the source tree or relies on PATH tools
- [ ] `--self-test` CLI mode: open a bundled sample PDF, render a page, extract text, save a copy to a temp dir, exit 0/1. It's used by CI and by users reporting problems
- [ ] PyInstaller spec (`packaging/pdfeditor.spec`): one-file `pdfeditor.exe` (windowed), app icon, version info, trimmed Qt (only needed modules and plugins, no QtWebEngine/Qt3D/QML), excluding test and dev packages
- [ ] Build script `scripts/build_exe.py` (clean build, reports size) and a size budget (target < 150 MB for the one-file exe)
- [ ] CI job on windows-latest: build the exe, run `pdfeditor.exe --self-test`, upload it as a workflow artifact
- [ ] Startup check of the one-file build (unpack + first window); if it's over ~3 s, evaluate Nuitka or a one-folder portable zip as an alternative deliverable
- [ ] AGENTS.md rule: each new dependency or data file must work in the frozen build (added to the spec and covered by `--self-test` where practical)

### Phase 4: Session, undo/redo, persistence (M)
- [ ] `DocumentSession`: dirty tracking, title asterisk, save prompts on close
- [ ] Command framework: `Command.do/undo/merge_with`, `UndoStack` wrapping `QUndoStack`, disk-backed `SnapshotCommand`
- [ ] Autosave recovery files every N minutes, and a restore dialog on startup after a crash
- [ ] Settings via `QSettings`: default zoom, tool prefs, author name for annotations, cache sizes

### Phase 5: Annotations and comments (L)
- [ ] Text markup: highlight, underline, strikeout, squiggly (built from text-selection quads)
- [ ] Sticky note, FreeText (typewriter and callout), Ink (pressure-agnostic smoothing), Line/Arrow, Rectangle, Ellipse, Polygon/Polyline, Stamp (standard plus custom image stamps), File attachment
- [ ] SelectObject tool: select, move, resize handles, multi-select, delete, z-order, copy/paste between pages and docs
- [ ] Properties inspector: color, fill, opacity, border width/style, font for FreeText, author, subject, lock
- [ ] Comments panel: list and filter by type/author/page, replies (IRT), review status, jump-to, summary export (PDF/CSV)
- [ ] Appearance streams regenerated on every change, so other viewers show edits correctly
- [ ] Flatten annotations (selected or all); import/export XFDF (own writer/reader in `services/`)
- [ ] Round-trip tests: create → save → reopen (and check it opens in pdfium via `pypdfium2` in tests) → compare models

### Phase 6: Page organization and document assembly (L)
- [ ] Organizer view: thumbnail grid, multi-select, drag-and-drop reorder, drop other PDFs or images in to insert
- [ ] Insert blank / from file / from clipboard image; delete; duplicate; rotate; replace pages; extract to new file
- [ ] Split by page count, page ranges, bookmarks (top level), or file size; merge multiple files (dialog with order and ranges, bookmarks merged)
- [ ] Crop tool (CropBox) with apply-to-range, and remove white margins automatically
- [ ] Page labels editor; bookmarks editing (add from current view or selection, rename, nest by drag, delete, set destination)
- [ ] Header/footer, Bates numbering, text/image watermark, and background (tokens: page, total, date, file name; position, font, opacity, page ranges)
- [ ] Create a PDF from images or from multiple files

### Phase 7: Content editing (XL, the hardest part)
- [ ] `engine/contentstream`: lexer and parser for content streams (operators, operands, inline images), tracking graphics and text state, and a serializer. Hypothesis round-trip tests (parse → write → parse is identical)
- [ ] Object model: enumerate `TextObj` (from rawdict spans, grouped into editable paragraphs), `ImageObj` (xref, placement matrix), `PathObj` (from drawings) per page, and link each to its content-stream operator ranges
- [ ] **Add content:** text box (rich: font, size, color, alignment, line spacing), images (PNG/JPEG, keep aspect), shapes/lines as real page content (not annotations)
- [ ] **Edit existing images:** move/resize/rotate (rewrite the `cm` matrix before `Do`), replace (swap XObject stream), delete (remove operator), extract/save
- [ ] **Edit existing text** (paragraph-level, the way Acrobat does it):
  - [ ] Detect paragraph blocks and show an inline `QGraphicsTextItem` overlay with the original font, size and color
  - [ ] Font strategy in `services/fonts.py`: extract the embedded font, check glyph coverage with fontTools, reuse it if it covers all glyphs, otherwise fall back to the closest system font (family/weight/italic/metrics match) and tell the user it was substituted
  - [ ] Apply the edit by removing the original glyphs precisely (a redaction limited to text, which keeps images and vectors), then reinserting the text with `insert_htmlbox`/`TextWriter` reflowed inside the original block width, and embed a font subset
  - [ ] Handle rotated text, character spacing and word spacing; decline CJK vertical and Type3 fonts for now, with a clear message
- [ ] Delete/move whole objects (text blocks, images, paths); marquee selection
- [ ] Snapshot-based undo for every content command; golden tests before and after editing

### Phase 8: Redaction and sanitization (M)
- [ ] Redact tool: mark area / text selection / whole page; search-and-redact with presets (email, phone, IBAN, credit card, national ID patterns, custom regex)
- [ ] Review mode: list of marks, per-mark accept/reject, overlay text and fill color
- [ ] Apply: true removal of text, image pixels and vector graphics under each mark (configurable), then a **verification pass** that re-extracts text and checks image pixels in every redacted area and reports any leak
- [ ] Sanitize document: metadata/XMP, JavaScript, embedded files, hidden layers, hidden or off-page text, comments, form data, links, thumbnails, and orphaned objects (full rewrite with garbage collection)

### Phase 9: OCR (M)
- [ ] OCR through MuPDF's built-in Tesseract engine with bundled `tessdata` (English plus a few common languages in the exe). More languages can be downloaded into the user data folder from the OCR dialog; no Tesseract install is needed
- [ ] Preprocessing (optional OpenCV): deskew, denoise, and binarize for OCR only (the visible image stays unchanged)
- [ ] Make scanned pages searchable by adding an invisible text layer (MuPDF OCR page output overlaid with `show_pdf_page`), with page ranges, skipping pages that already have text, and a `Job` with progress and cancel
- [ ] Batch OCR of many files through the same in-process engine (no `ocrmypdf` dependency)
- [ ] Accuracy check against fixture scans (text similarity threshold)

### Phase 10: Export and conversion (M)
- [ ] To images: PNG/JPEG/TIFF (multi-page), with DPI, color space and page range
- [ ] To text / HTML / Markdown (reading order via blocks, or `pymupdf4llm` when available)
- [ ] To Word via `pdf2docx`; to Excel via table detection (`page.find_tables`) → `openpyxl`, with a table-picking UI
- [ ] Extract all images and fonts
- [ ] From Office files: optional extra, used only when LibreOffice is installed (`soffice --convert-to pdf`); the menu item explains the requirement otherwise. Core conversions never depend on it

### Phase 11: Optimize and compress (M)
- [ ] Space-usage audit report (images / fonts / content / other, by bytes)
- [ ] Downsample and recompress images (target DPI, JPEG quality, grayscale option, via Pillow), subset fonts, drop unused objects and duplicates, object streams, deflate everything
- [ ] Linearize for fast web view (a pikepdf/qpdf post-process)
- [ ] Presets (screen / ebook / print / custom) with a before/after size preview

### Phase 12: Compare documents (M)
- [ ] Page alignment (text-similarity matching, to handle inserted and deleted pages)
- [ ] Word-level text diff (difflib) with insert/delete/change highlights
- [ ] Visual diff: render both pages, compute a numpy pixel difference, and group differing pixels into region boxes
- [ ] Side-by-side synced view with a change list; export a comparison report PDF

### Phase 13: Security (S)
- [ ] Password encryption (AES-256, AES-128), owner and user passwords, permissions (print/copy/modify/annotate)
- [ ] Remove security (requires the owner password); security summary in Properties
- [ ] Metadata editor (Info dictionary plus XMP sync)

### Phase 14: Standards (PDF/A) (M)
- [ ] Preflight-lite checks: fonts embedded, no encryption, no JS, color spaces with OutputIntent, transparency, XMP present
- [ ] Convert to PDF/A-2b: embed missing fonts, add sRGB OutputIntent ICC, write the XMP PDF/A identification, remove disallowed features. Done fully in-process (MuPDF + pikepdf + bundled sRGB ICC); no Ghostscript
- [ ] Built-in PDF/A checks cover the common rules; optional veraPDF integration (only if installed) for authoritative reports

### Phase 15: Accessibility (M)
- [ ] Checker: tagged or not, document language, title shown in the window, image alt text, headings structure, reading-order sanity, and contrast of annotations/added text. Results panel with jump-to
- [ ] Fix-ups: set language/title/DisplayDocTitle; edit alt text in the struct tree (pikepdf); a Tags tree panel (read, rename, reorder)
- [ ] Stretch: basic auto-tagging (paragraphs/headings/figures from block analysis). Marked experimental

### Phase 16: Polish, robustness, performance (M)
- [ ] Keyboard shortcuts that match Acrobat conventions, customizable; a command palette (Ctrl+Shift+P)
- [ ] Localization scaffolding (Qt translations), high-DPI and multi-monitor checks, app accessibility (focus order, screen-reader names)
- [ ] Hard cases: corrupt or broken files (repair), huge files (streaming thumbnails, memory caps), encrypted files with unknown handlers
- [ ] Profiling pass: open-to-first-paint under 300 ms for typical docs; scrolling at 60 fps on 1000 pages

- [ ] Vector printing (native print path instead of 300 dpi raster), and running `select_all` off the GUI thread for very large documents (follow-ups from the Phase 3 review)

### Phase 17: Packaging and distribution (S–M)
- [ ] Release build of the self-contained one-file `pdfeditor.exe` (from Phase P), with all optional-feature data bundled (tessdata, ICC, fonts) and final size/startup tuning
- [ ] Optional thin installer around the same exe (Inno Setup: Start menu, optional `.pdf` association, uninstall). The portable exe stays the primary deliverable
- [ ] Code signing, version stamping, an "About" dialog with third-party licenses (AGPL notice and source offer)
- [ ] Smoke test in a clean Windows VM with no Python or other tools installed (every feature, including OCR and PDF/A, works offline from the single exe)

### Later / not in current scope
- AcroForm creation and editing, digital signatures (PAdES) and certificate validation. Rendering and keeping existing forms and signatures intact is covered above.
- A permissive engine backend (pypdfium2 + pikepdf) implementing `engine/base.py`. The contract tests from Phase 1 are its acceptance suite.
- Measurement tools, 3D/multimedia, batch "Action Wizard", cloud sync.

**Suggested milestones:** M1 = Phases 0–3 (a usable viewer) + Phase P (it ships as one exe). M2 = Phases 4–6 (annotate and organize, the first useful release). M3 = Phase 7 (content editing). M4 = Phases 8–13. M5 = Phases 14–17 (release).

---

## Verification

- **Automated (every phase):** `pytest` for unit, engine-contract, service and UI (pytest-qt, offscreen) tests; `lint-imports`; `ruff check`; `mypy`.
- **Round-trip rule:** every editing feature has a test that edits, saves, reopens, and asserts on the result. Every saved file is also opened with `pikepdf` (a qpdf structural check) and rendered with `pypdfium2`, so the output is checked by independent PDF implementations and not only by MuPDF.
- **Golden rendering tests:** reference PNGs per fixture, compared with a perceptual tolerance.
- **Redaction:** automated leak checks (text extraction and pixel inspection in redacted areas).
- **Frozen build:** from Phase P on, CI builds `pdfeditor.exe` and runs `--self-test` on every PR, so a feature that works from source but breaks when frozen fails the build.
- **Performance:** `pytest-benchmark` on the 1000-page fixture (open, first paint, search, save).
- **Manual end-to-end per milestone:** `python -m pdfeditor tests/fixtures/real/<file>.pdf`, then run the milestone checklist (open, navigate, annotate, organize, edit text, redact, OCR, save), then reopen the result in Adobe Reader and a browser viewer to confirm it's compatible.
