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
1. **Engine isolation.** `ui` → `services`/`core` → `engine.base` protocols, and the rule is enforced by an **import-linter** contract in CI. Only `engine/mupdf` imports `pymupdf`. Word/Excel/HTML/Markdown export uses our own writers on top of engine-neutral text, table and image data (no `pdf2docx`/`openpyxl`/OpenCV: they would double the executable).
2. **Capabilities.** Each engine publishes `Capabilities` (e.g. `edit_text`, `redact`, `ocr`), and the UI turns actions on or off from these flags. This keeps a future permissive backend workable.
3. **Undo/redo is a hybrid.** Operations that can be reversed exactly (annotations, page ops, bookmarks, metadata) get their own inverse commands. Destructive operations (content edits, redaction, OCR, optimize) use `SnapshotCommand`, which serializes the document to a temp file (`tobytes(garbage=0)`) before applying the change. Undo depth and disk usage are configurable.
4. **Threading.** A MuPDF `Document` is not thread-safe. Each session has one lock, and rendering runs on worker threads with cached per-page display lists. Every mutation bumps `page.revision`, which invalidates cached tiles. Long jobs (OCR, compare, export) run as `Job`s that report progress and can be cancelled.
5. **Saving.** The editor uses incremental save when the file is signed or when the user chooses it, and a full rewrite (garbage collection and deflate) otherwise. It never overwrites the source file until the temp file has been written and verified, then swaps them atomically.

**Core dependencies:** `pymupdf`, `PySide6`, `pikepdf` (linearize, low-level repair, struct tree), `fontTools` (glyph coverage, subsetting), `Pillow`, `numpy`. Bundled in the executable as well: Tesseract language data (`tessdata`; MuPDF has the Tesseract engine built in, so no Tesseract install is needed), and an sRGB ICC profile. Optional external tools that are used only if found and never required: LibreOffice (Office import), Ghostscript and veraPDF (extra PDF/A validation). **Build:** PyInstaller one-file (evaluate Nuitka for startup time).
**Dev dependencies:** `pytest`, `pytest-qt`, `pytest-benchmark`, `hypothesis`, `ruff`, `mypy`, `import-linter`, `pre-commit`, `pyinstaller`.

---

## Implementation phases (todo list)

Progress is tracked here: see AGENTS.md for the rules. **Current phase: 17 (Packaging), last items need a certificate, Inno Setup and a VM.**

Sizes: S ≈ days, M ≈ 1–2 weeks, L ≈ 3–4 weeks, XL ≈ 5+ weeks, for one developer.

### Phase 0: Foundations (S)
- [x] `git init`, `.gitignore`, `pyproject.toml` (src layout, Python ≥3.11), `uv`/venv setup doc in README
- [x] Configure ruff, mypy (strict for `model/`, `engine/base.py`, `core/`), pytest, pytest-qt
- [x] import-linter contracts (engine isolation, layers `ui > services > core > engine > model`)
- [x] pre-commit hooks, plus a GitHub Actions CI matrix (windows-latest; ubuntu for headless tests with `QT_QPA_PLATFORM=offscreen`)
- [x] `scripts/make_fixtures.py`: generate test PDFs (multi-page text, images, vector art, annotations, outlines, encrypted, rotated pages, CJK text, subset fonts, scanned, broken xref, large 1000-page doc). RTL fixture deferred until a bundled Arabic/Hebrew font is chosen
- [x] Curate a small corpus of real-world PDFs (scanned, broken xref, forms, signed) in `tests/fixtures/real/` with sources noted _(IRS W-9 (fillable form with XFA, tagged, usage-rights signature) and a 1953 US Navy letter scan, both public domain; a pyHanko-signed sample from `scripts/make_signed_sample.py` with a self-signed test certificate; broken xref stays synthetic. `tests/services/test_real_corpus.py` checks that signatures and form fields survive incremental saves; the real files exposed a bug in reading indirect /Lang, /DisplayDocTitle and /Alt, now fixed)_
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
- [x] `resources.py`: one helper to find bundled data (`importlib.resources` / `sys._MEIPASS`); no feature reads files relative to the source tree or relies on PATH tools
- [x] `--self-test` CLI mode: open a bundled sample PDF, render a page, extract text, save a copy to a temp dir, exit 0/1. It's used by CI and by users reporting problems
- [x] PyInstaller spec (`packaging/pdfeditor.spec`): one-file `pdfeditor.exe` (windowed), app icon, version info, trimmed Qt (only needed modules and plugins, no QtWebEngine/Qt3D/QML), excluding test and dev packages
- [x] Build script `scripts/build_exe.py` (clean build, reports size) and a size budget (target < 150 MB for the one-file exe)
- [x] CI job on windows-latest: build the exe, run `pdfeditor.exe --self-test`, upload it as a workflow artifact
- [x] Startup check of the one-file build (unpack + first window); if it's over ~3 s, evaluate Nuitka or a one-folder portable zip as an alternative deliverable _(measured: 80 MB exe, window shown ~1.1 s after launch, so no fallback needed)_
- [x] AGENTS.md rule: each new dependency or data file must work in the frozen build (added to the spec and covered by `--self-test` where practical)

### Phase 4: Session, undo/redo, persistence (M)
- [x] `DocumentSession`: dirty tracking, title asterisk, save prompts on close
- [x] Command framework: `Command.do/undo/merge_with`, `UndoStack` (pure Python, not `QUndoStack`, so `core/` stays Qt-free), disk-backed `SnapshotCommand`
- [x] Autosave recovery files every N minutes, and a restore dialog on startup after a crash
- [x] Settings via `QSettings`: default zoom, tool prefs, author name for annotations, cache sizes (Preferences dialog, Ctrl+K)

### Phase 5: Annotations and comments (L)
- [x] Text markup: highlight, underline, strikeout, squiggly (built from text-selection quads)
- [x] Sticky note, FreeText (typewriter and callout), Ink (pressure-agnostic smoothing), Line/Arrow, Rectangle, Ellipse, Polygon/Polyline, Stamp (standard plus custom image stamps), File attachment _(ink smoothed with RDP simplification. Callout tool: click the point, drag the box; resizing keeps the tip, but the tip can't be dragged on its own. Custom PNG/JPEG stamps are copied into the user data folder and listed in the Stamp menu)_
- [x] SelectObject tool: select, move, resize handles, multi-select, delete, z-order, copy/paste between pages and docs
- [x] Properties inspector: color, fill, opacity, border width/style, font for FreeText, author, subject, lock (dash style is kept but not editable yet)
- [x] Comments panel: list and filter by type/author/page, replies (IRT), review status, jump-to, summary export (PDF/CSV), XFDF import/export
- [x] Appearance streams regenerated on every change, so other viewers show edits correctly
- [x] Flatten annotations (selected or all); import/export XFDF (own writer/reader in `services/`)
- [x] Round-trip tests: create → save → reopen (and check it opens in pdfium via `pypdfium2` in tests) → compare models

### Phase 6: Page organization and document assembly (L)
- [x] Organizer view: thumbnail grid, multi-select, drag-and-drop reorder, drop other PDFs or images in to insert
- [x] Insert blank / from file / from clipboard image; delete; duplicate; rotate; replace pages; extract to new file
- [x] Split by page count, page ranges, bookmarks (top level), or file size; merge multiple files (dialog with order and ranges, bookmarks merged)
- [x] Crop (CropBox) with apply-to-range via the Crop dialog (margins), and remove white margins automatically (interactive drag-to-crop tool deferred to Phase 16)
- [x] Page labels editor; bookmarks editing (add from current view or selection, rename, nest by drag, delete, set destination)
- [x] Header/footer, Bates numbering, text/image watermark, and background (tokens: page, total, date, file name; position, font, opacity, page ranges) _(each is added as a marked `/Artifact` with its settings in the page's `/PieceInfo`, so Header & Footer, Bates, Watermark and Background each have Add / Update (pre-filled, replaces) / Remove; adding again asks whether to replace; Acrobat's own marks are recognised (#39, #40))_
- [x] Create a PDF from images or from multiple files

### Phase 7: Content editing (XL, the hardest part)
- [x] `engine/contentstream`: lexer and parser for content streams (operators, operands, inline images), tracking graphics and text state, and a serializer. Hypothesis round-trip tests (parse → write → parse is identical)
- [x] Object model: text blocks (MuPDF blocks in reading order, with the dominant style detected), images, form XObjects and paths (from the content-stream tracker, with operator ranges and page-space boxes)
- [x] **Add content:** text box (rich: font, size, color, alignment, line spacing), images (PNG/JPEG, keep aspect), shapes/lines as real page content (not annotations) _(the inline editor's style bar sets font, size, bold, italic, colour, alignment and line spacing; Add Text remembers the last style)_
- [x] **Edit existing images:** move/resize (the `cm` is rewritten around `Do`, computed from the CTM), replace, delete, extract/save _(rotation works through the API; there's no rotate handle in the UI yet)_
- [x] **Edit existing text** (paragraph-level, the way Acrobat does it):
  - [x] Detect paragraph blocks and show an inline editor over them with a matching screen font, size and color
  - [x] Font strategy: extract the embedded font, check glyph coverage with fontTools, reuse it if it covers all glyphs, otherwise fall back to the closest standard family (weight/italic kept) and tell the user it was substituted
  - [x] Apply the edit by removing the original glyphs precisely (a text-only redaction that keeps images, vector art and the user's pending redaction marks), then re-typesetting with `insert_htmlbox` inside the block width (the box grows downward instead of shrinking the font)
  - [x] Handle rotated text, character spacing and word spacing; decline CJK vertical and Type3 fonts for now, with a clear message _(rotated/vertical and Type3 text are declined with a reason. Character spacing (Tc) and word spacing (Tw) are detected per block and reproduced on edit, including alignment and justification, by rewriting the typeset operators, because MuPDF's HTML layout ignores CSS spacing. Horizontal scaling (Tz) isn't reproduced)_
- [x] Delete/move whole objects (text blocks, images, forms, paths); marquee selection; Delete key
- [x] Snapshot-based undo for every content command; render/pixel and text-extraction checks before and after editing (instead of golden images)

### Phase 8: Redaction and sanitization (M)
- [x] Redact tool: mark area / text selection / whole page; search-and-redact with presets (email, phone, IBAN, credit card (Luhn-checked), US SSN, dates, custom regex)
- [x] Review mode: Redactions panel listing marks, jump-to, apply selected / remove / apply all; overlay text and fill color (Redaction Properties, remembered)
- [x] Apply: true removal of text, image pixels (blank or remove images) and vector graphics (covered or touched) under each mark, then a **verification pass** that re-extracts text and inspects image pixels in every redacted area and reports any leak; the next save is forced to be a full, garbage-collected rewrite (an incremental save would keep the old content)
- [x] Sanitize document: metadata/XMP, JavaScript (incl. open actions and triggers), embedded files, hidden layers, hidden or off-page text, comments, form data, links, thumbnails, and orphaned objects (full rewrite with garbage collection) _(hidden-layer content (OFF in the default configuration, including membership dictionaries and hidden form XObjects) is stripped and those layers are dropped; glyphs entirely outside the CropBox are removed one by one, so partly visible words survive. Leak-tested with MuPDF (all layers on), pypdfium2 and pikepdf)_

### Phase T: Tool modes and discoverability (S–M), done next, before Phase 9
Problem (user feedback): once a tool such as Sticky Note is active there's no visible way to turn it off, and clicking an existing comment creates another one instead of showing it. Escape and the Select tool (V, Home tab) exist but aren't discoverable, and creation tools ignore what's already on the page.
- [x] Clicking the active tool's button again turns it off and returns to the Select tool (every tool: comment, edit, redact, hand)
- [x] Quick-access toolbar next to the ribbon tabs with Select, Hand, Undo, Redo and Save, so the Select tool is one click away on every tab
- [x] Status-bar indicator of the active tool with an "×" to exit it, and the hint "Esc: back to Select"
- [x] Creation tools respect existing content: clicking an existing comment with any comment tool selects it (double-click opens it) instead of creating a new one; the same for Edit tools over existing objects
- [x] Notes are readable without switching tools: hovering a sticky note (or any comment with text) shows a popup with its text and replies; clicking it in the Select tool opens it for reading and editing
- [x] Creation tools return to Select after one use by default; a Preferences option "Keep tools selected after use" restores the sticky behavior
- [x] Right-click in any creation tool offers "Back to Select"
- [x] GUI tests for each behavior (toggle-off, Esc, status-bar exit, click-existing-comment, hover popup, one-shot vs sticky preference)

### Phase 9: OCR (M), done
- [x] OCR through MuPDF's built-in Tesseract engine with bundled `tessdata` (English plus a few common languages in the exe). More languages can be downloaded into the user data folder from the OCR dialog; no Tesseract install is needed. *(The exe bundles English (`tessdata_fast`, fetched by `scripts/fetch_tessdata.py`); the other common languages are one click away in the dialog.)*
- [x] Preprocessing (optional OpenCV): deskew, denoise, and binarize for OCR only (the visible image stays unchanged). *(Pure Pillow, no OpenCV: grayscale, denoise, binarize, and deskew by projection profile (±10°). The recognized text layer is rotated back with a `cm` so it lines up with the skewed scan; OCR ▸ "Straighten skewed scans")*
- [x] Make scanned pages searchable by adding an invisible text layer (MuPDF OCR page output overlaid with `show_pdf_page`), with page ranges, skipping pages that already have text, and a `Job` with progress and cancel. Undo through one "Recognize Text" snapshot; rotated pages are handled
- [x] Batch OCR of many files through the same in-process engine (no `ocrmypdf` dependency)
- [x] Accuracy check against fixture scans (text similarity threshold), plus an OCR check in the exe `--self-test`

### Phase 10: Export and conversion (M), done
- [x] To images: PNG/JPEG/TIFF (multi-page), with DPI, color space and page range. *(RGB or grayscale; TIFF as one multi-page file or one file per page; comments optional)*
- [x] To text / HTML / Markdown (reading order via blocks, or `pymupdf4llm` when available). *(Our own converters on a neutral structure model: headings by font size, styled runs, tables, pictures; `pymupdf4llm` isn't used)*
- [x] To Word via `pdf2docx`; to Excel via table detection (`page.find_tables`) → `openpyxl`, with a table-picking UI. *(Own minimal OOXML writers instead of `pdf2docx`/`openpyxl`, which would pull in OpenCV/numpy: Word gets headings, styled paragraphs, tables, pictures and page breaks; Excel gets one sheet per chosen table with numbers as numbers. Word's default is **Keep page layout**: one section per page at the PDF page size, text in frames at its places (letter and word spacing reproduced from the fonts' metrics), detected tables as real Word tables floating at their places (grid, merged cells, rotated cell text, borders and shading measured from the drawing), vertical text in borderless cells, pictures and a transparent vector-art background anchored behind it; the PDF's TrueType fonts are embedded in the .docx (made loadable on Windows), and fonts that can't be are mapped to metric-compatible ones; **Flowing text** reflows. Both tag the text language (PDF /Lang or guessed from the script) and use embedded fonts' real names)*
- [x] Extract all images and fonts
- [x] From Office files: optional extra, used only when LibreOffice is installed (`soffice --convert-to pdf`); the menu item explains the requirement otherwise. Core conversions never depend on it

### Phase 11: Optimize and compress (M), done
- [x] Space-usage audit report (images / fonts / content / other, by bytes). *(Tools ▸ Space Usage: images, fonts, page content, comments/forms, tags, bookmarks/links, embedded files, metadata/thumbnails, other, file overhead)*
- [x] Downsample and recompress images (target DPI, JPEG quality, grayscale option, via Pillow), subset fonts, drop unused objects and duplicates, object streams, deflate everything. *(MuPDF's image rewriter instead of Pillow; it halves resolution in power-of-two steps, never going below the target)*
- [x] Linearize for fast web view (a pikepdf/qpdf post-process). *(Skipped with a note for encrypted files, since qpdf would drop the encryption)*
- [x] Presets (screen / ebook / print / custom) with a before/after size preview. *(Plus a lossless preset; the result is saved as a new file and the open document is left alone)*

### Phase E: Text editing fixes (S–M), from user testing, done
Reported on a two-column manual (icons plus labels in two columns):
- [x] Clicking a text line in a two-column layout opens an edit box that contains the text of both columns. Paragraph detection must split blocks at large horizontal gaps (column gutters) and never merge lines that don't overlap horizontally. *(`engine/textlayout.py`: lines are cut at gaps wider than 2.5 em (gutters stored as spaces included) and blank margins trimmed; blocks with pieces side by side become one paragraph per piece; running text is unchanged)*
- [x] Closing the edit box after editing existing text leaves the original place blank and creates a new text box elsewhere with the formatting lost. The edit must be re-typeset in the original block's position with its font, size and color, and closing without changes must leave the page untouched. *(Only the edited column is removed; whitespace is kept; the font is resolved before removal and matched by a loose name, so the document font survives repeated edits; one-line labels widen instead of wrapping; the first baseline is measured and kept, so edits don't drift; duplicate cmap entries are pruned so "(" doesn't copy as U+FD3E; an unchanged close is a no-op)*
- [x] No way to format edited text: add a style bar to the inline editor (font family, size, bold, italic, color, alignment) that applies to the whole block and is kept when saving the edit. *(The editor previews the embedded font; changing bold/italic of an embedded font falls back to a standard face and says so; Add Text uses the same bar and remembers the last style)*

### Phase 12: Compare documents (M), done
- [x] Page alignment (text-similarity matching, to handle inserted and deleted pages)
- [x] Word-level text diff (difflib) with insert/delete/change highlights. *(Words come in visual reading order, so an edited paragraph re-typeset at the end of the content stream still diffs as "changed")*
- [x] Visual diff: render both pages, compute a numpy pixel difference, and group differing pixels into region boxes. *(Pillow instead of numpy, which the exe excludes; runs on aligned pages whose text is unchanged, e.g. scans or drawings)*
- [x] Side-by-side synced view with a change list; export a comparison report PDF. *(Tools ▸ Compare Files: two views that follow each other, coloured change boxes, click a change to show it on both sides; the report has a summary page and each changed page of both versions with outlined changes)*

### Phase 13: Security (S), done
- [x] Password encryption (AES-256, AES-128), owner and user passwords, permissions (print/copy/modify/annotate). *(Protect ▸ Encrypt with Password; applied on the next full save, undoable until then; RC4 isn't offered for new files)*
- [x] Remove security (requires the owner password); security summary in Properties. *(The owner password is checked on a scratch copy, because a wrong password leaves an open MuPDF document unreadable; Properties also shows a security change that is pending until the next save)*
- [x] Metadata editor (Info dictionary plus XMP sync). *(The existing Properties editor now writes title/author/subject/keywords/creator/producer into XMP too, keeping other XMP content such as the PDF/A identification)*

### Phase 14: Standards (PDF/A) (M), done
- [x] Preflight-lite checks: fonts embedded, no encryption, no JS, color spaces with OutputIntent, transparency, XMP present. *(Tools ▸ PDF/A Preflight, via pikepdf: also embedded files, XFA, annotation types/flags/appearances, image interpolation/alternates, PostScript XObjects, transfer functions, CMYK images; each marked fixable or not. Transparency is allowed in PDF/A-2, so it isn't flagged)*
- [x] Convert to PDF/A-2b: embed missing fonts, add sRGB OutputIntent ICC, write the XMP PDF/A identification, remove disallowed features. Done fully in-process (MuPDF + pikepdf + bundled sRGB ICC); no Ghostscript. *(Tools ▸ Save as PDF/A writes a new file and leaves the open document alone. Standard fonts the file only names (and Arial/Times New Roman/Courier New) get MuPDF's metric-compatible Type1C programs with widths computed by fontTools; rendering is pixel-identical and text positions match in pdfium. Other non-embedded fonts are reported, not substituted)*
- [x] Built-in PDF/A checks cover the common rules; optional veraPDF integration (only if installed) for authoritative reports. *(The result is re-checked; "Validate with veraPDF" appears only when veraPDF is found)*

### Phase 15: Accessibility (M)
- [x] Checker: tagged or not, document language, title shown in the window, image alt text, headings structure, reading-order sanity, and contrast of annotations/added text. Results panel with jump-to. *(Tools ▸ Accessibility Check and an Accessibility panel: failed / needs review / passed, double-click goes to the page and the tag; also tab order for pages with annotations. Contrast follows WCAG 2 against an assumed white background, for all text and FreeText comments)*
- [x] Fix-ups: set language/title/DisplayDocTitle; edit alt text in the struct tree (pikepdf); a Tags tree panel (read, rename, reorder). *(Fix… in the Accessibility panel for language, title, title bar, tab order and alt text; a Tags panel to change tag types and alt text and move tags up/down. All undoable. Done through the engine (MuPDF objects plus our own PDF object parser), not pikepdf, so edits apply to the open document)*
- [x] Stretch: basic auto-tagging (paragraphs/headings/figures from block analysis). Marked experimental. *(Tools ▸ Auto-Tag Document (experimental): marks each page's content as /P, /H1–/H3 and /Figure (vector art as /Artifact), builds Document → elements in reading order with a ParentTree and /MarkInfo, and refuses documents that are already tagged. Undoable; rendering and text extraction are unchanged. No lists, tables, links or alt text yet)*

### Phase 16: Polish, robustness, performance (M), done
- [x] Keyboard shortcuts that match Acrobat conventions, customizable; a command palette (Ctrl+Shift+P). *(Edit ▸ Keyboard Shortcuts: filter, assign, remove, reset, with conflict detection; overrides persist. Acrobat keys added where Qt's defaults differ: Ctrl+W, Ctrl+Q, Ctrl+6. Edit ▸ Command Palette runs any enabled command by name)*
- [x] Localization scaffolding (Qt translations), high-DPI and multi-monitor checks, app accessibility (focus order, screen-reader names). *(Preferences ▸ Language loads Qt's own translations and app catalogues from `data/i18n`; UI strings still need to be moved to `tr()` and translated. Qt 6's pass-through DPI scaling is used and tiles render at each screen's device pixel ratio. Every focusable widget has a screen-reader name, checked by a test)*
- [x] Hard cases: corrupt or broken files (repair), huge files (streaming thumbnails, memory caps), encrypted files with unknown handlers. *(Damaged files are repaired and announced in the status bar; empty and non-PDF files get plain-language errors; certificate or rights-management security (e.g. Adobe.PubSec) is refused with an explanation. Thumbnails render on demand, the tile cache is capped (Preferences), and the text-index cache is now an LRU of 300 pages)*
- [x] Profiling pass: open-to-first-paint under 300 ms for typical docs; scrolling at 60 fps on 1000 pages. *(Measured: 1000-page file ~275 ms to first paint, small files ~20-35 ms; scrolling ~190 steps/s. Select-all on 1000 pages went from 913 ms to 10 ms, because highlight boxes are now computed per page when painted)*

- [x] Vector printing (native print path instead of 300 dpi raster), and running `select_all` off the GUI thread for very large documents (follow-ups from the Phase 3 review). *(Pages print as vectors through the engine's SVG and QtSvg, with a workaround for QtSvg's handling of MuPDF glyph outlines; output matches the screen within anti-aliasing. Grayscale, printing without comments, pages QtSvg can't draw (masks, blend modes, patterns), and "Print as image" use the 300 dpi raster path. select_all no longer needs a thread: it's now instant)*

### Phase 17: Packaging and distribution (S–M)
- [x] Release build of the self-contained one-file `pdfeditor.exe` (from Phase P), with all optional-feature data bundled (tessdata, ICC, fonts) and final size/startup tuning. *(66 MB, down from 82.6: Qt pieces nothing loads are left out (software OpenGL, virtual keyboard with QML/Quick, the PDF image plugin with Qt6Pdf, unused platform/TLS plugins, Qt Help translations). English tessdata, the sRGB profile, MuPDF's fonts, the AGPL text and third-party notices are bundled. About 1.5 s from launch to window; the self-test runs in about 2.4 s including unpacking)*
- [ ] Optional thin installer around the same exe (Inno Setup: Start menu, optional `.pdf` association, uninstall). The portable exe stays the primary deliverable _(partial: `packaging/installer.iss` (per-user install, Start menu, optional desktop icon, optional .pdf default, "Open with" entry, clean uninstall) and `build_exe.py --installer` are ready and unit-tested; not compiled yet because Inno Setup isn't installed on the build machine)_
- [ ] Code signing, version stamping, an "About" dialog with third-party licenses (AGPL notice and source offer) _(partial: version resource stamped in the exe; Help ▸ About shows the AGPL notice with a source offer (`SOURCE_URL`), the full license and generated third-party notices; `build_exe.py --sign` signs with signtool from a .pfx or a certificate thumbprint, but no code-signing certificate is available yet)_
- [ ] Smoke test in a clean Windows VM with no Python or other tools installed (every feature, including OCR and PDF/A, works offline from the single exe) _(partial: no VM or Windows Sandbox on this machine. `build_exe.py --clean-env` runs the exe copied to a temporary folder with no Python, a system-only PATH and an empty profile: all self-test checks (OCR, PDF/A, export, security, printing, ...) pass and the window opens a document in about 2 s. CI runs the clean-environment self-test on every PR)_

### Phase U: Modern, friendlier UI (L)
Problem (UI review of the current build): the app looks like stock Fusion. It has no accent color or visual hierarchy, and a few colors are hardcoded. The left dock runs the full window height, so the ribbon starts beside it, and the ribbon repeats most of the menu bar. Nine text tabs are squeezed into the navigation dock and get cut off. The selected thumbnail is filled with flat mint. The dark canvas clashes with the light theme. Page and zoom controls sit at opposite corners of the status bar. The empty window shows a single line of text, and all feedback goes to the status bar.

Approach: stay on QtWidgets with Fusion. Add a small token-based theme layer (a palette plus a QSS built from it) instead of a QML rewrite or a third-party theme package. QML/Quick are deliberately left out of the exe, and theme packages add size and maintenance risk. No new runtime dependencies. Use the system UI font (Segoe UI Variable on Windows) and the bundled Lucide icons. Each block below is one PR on `phase-U-modern-ui`, shippable on its own, in this order:

- [x] **U1 Design tokens and theme engine.** A `ui/style/` package holds tokens (spacing on a 4 px scale, corner radii, type scale, surface, border, text and accent colors, light and dark variants). From them it builds the `QPalette` and one QSS file covering buttons, tool buttons, tabs, inputs, scrollbars, menus, tooltips, docks and splitters. The accent color follows the Windows accent (Qt ≥6.6 `QPalette.Accent`), and Preferences can override it. Hardcoded colors in `dialogs/security.py`, `view/note_popup.py` and `view/text_editor.py` move to tokens. On Windows 11 the title bar follows the dark theme through `DwmSetWindowAttribute` (ctypes, skipped elsewhere). Windows High Contrast falls back to the system palette with no QSS. Tests: every text/background token pair meets WCAG AA (4.5:1). Switching theme live repaints icons and QSS. _(Qt 6.11 already switches the native title bar with the color scheme (verified through DWM in a test), so no ctypes call was needed. Fusion's tint of selected icons with the highlight color is also turned off for page thumbnails; that tint caused the mint fill)_
- [x] **U2 Window frame and ribbon.** The ribbon runs the full window width above both docks (use `setMenuWidget`). Bump the saved window-state version so old dock layouts don't restore into the new frame. Tabs get a flat style with an accent underline and a label under each button group. Labels get shorter ("Document Properties" becomes "Properties"). A "compact ribbon" toggle shows icons only, and double-clicking a tab collapses the ribbon. Groups that don't fit a narrow window go into a "»" overflow menu. The classic menu bar is hidden by default behind a File/☰ menu button, comes back while Alt is held, and has a Preferences option. Tooltips show the action name, shortcut and one line of help. _(Menus open with Alt+letter while the bar is hidden, and tapping Alt opens File. The ☰ button holds the same menus. View ▸ Compact Ribbon / Collapse Ribbon (Ctrl+F1) and the bar setting are remembered. Every ribbon command has a help line in `ui/action_help.py`, and its tooltip follows shortcut changes)_
- [x] **U3 Side panels: icon rail.** Replace the navigation tab strip with a vertical icon rail. Clicking an icon opens that panel and clicking it again collapses the panel. The panels are Pages, Bookmarks, Comments, Search, Redactions, Attachments, Layers, Accessibility and Tags. Badges show counts (comments, search hits, accessibility issues). A matching right-hand rail holds Properties and comment threads. Panel width and the open panel are remembered. Each empty panel says what it's for and offers its main action, for example "No bookmarks yet · Add bookmark (Ctrl+B)". _(Each side is a title-less dock: rail plus open panel; collapsing pins the dock to the rail width and reopening restores the remembered width (`AppSettings` `panels/*`). F4 still hides the whole left side. The right rail holds only Properties for now (`act_inspector`, Ctrl+E, toggles it); comment threads stay in the left Comments panel. Badges come from `ViewPanel.badge_count()` (comments, search hits, accessibility problems, and pending redaction marks). Empty states show the real shortcut of their action; bookmarks have no shortcut, so none is shown (no Ctrl+B invented). `nav_tabs` is gone: use `window.show_panel(panel)` and `window.nav_panels.current()`)_
- [x] **U4 Document canvas.** The canvas color comes from tokens (neutral grey in light theme, near-black in dark). Pages are centered, with even gaps and a soft drop shadow. Thumbnails become cards: page shadow, an accent outline for the current page, a lighter outline for selected pages, and the page number below (this replaces the mint fill). A floating pill at the bottom center of the view holds "‹ 3 / 120 ›", −, zoom %, + and fit width/page. It fades out after inactivity, and these controls leave the status bar. Ctrl+wheel and pinch zoom keep the point under the cursor fixed. Zoom and page-jump animations last about 150 ms, and none run when Windows "show animations" is off. _(Pill, navigator and zoom box live in `ui/view/pill.py`; `window.navigator`/`window.zoom_box` still point at them. The pill is a child of the viewport so Qt can still scroll the page area by blitting. QGraphicsView's own centring is off by half a scroll bar, so the scene rect is widened to centre pages. Animations follow SPI_GETCLIENTAREAANIMATION, user commands animate, programmatic calls don't, and tests turn them off. 1000-page doc, native Windows run: open-to-first-paint 59–61 ms (was 134–150, thumbnail sizes now come from a size role instead of a pixmap per page); full-viewport repaint while scrolling 2.5 ms (was 1.9–2.1); event-loop scroll frame 1.8–2.0 ms (was 0.9), mostly repainting the pill, still far inside 60 fps)_
- [x] **U5 Document tabs.** Tabs get rounded corners, and the close button appears only on hover or on the active tab. A dot marks unsaved changes instead of "*". The tooltip shows the full path, and a "+" button opens a file. When tabs overflow, a menu lists all open documents. Middle-click closes a tab. _(Our own close button in `ui/document_tabs.py`: on a dirty tab the dot sits where the × goes and turns into it on hover, so the tab text never changes width. Middle-click and the × go through `close_tab`, so unsaved changes still prompt. Screen readers hear "unsaved changes" in the tab name)_
- [x] **U6 Start screen.** With no document open, show a start page instead of the label. It has a large Open button, a drop zone, and a grid of recent files with first-page thumbnails (rendered in a background `Job`). Recent files can be pinned and removed, and missing ones appear dimmed. Quick actions: Combine files, Create from Office, Compare, OCR a scan. Everything works by keyboard. _(`ui/start_page.py`; previews come from `services/thumbnails.py` under the engine lock and are cached per path and modification time. Pins live in QSettings `recent_pinned` and don't count against the 10 recent files; Clear List keeps them. Cards open with a click or Enter, Delete removes, the menu key or Shift+F10 opens Pin/Remove/Show in Folder. "OCR a scan" asks for a file, opens it, then starts Recognize Text)_
- [x] **U7 Contextual actions.** Selecting text shows a floating mini toolbar: Copy, Highlight, Underline, Strikeout, Add note, Redact (and Edit text while editing). Selecting an annotation shows color, opacity, Reply and Delete, and selecting pages in the organizer shows Rotate, Delete and Extract. A "Search tools…" box in the ribbon row opens the command palette inline, with Ctrl+Shift+P as its shortcut (Ctrl+K stays Preferences, the Acrobat convention). Each action stays reachable from the ribbon, the menus and the keyboard, so the mini toolbars are shortcuts rather than the only way. _(`ui/contextual.py`: one bar of each kind per window, a child of the page view's (or organizer's) viewport like the pill. Buttons run the existing actions and Commands; Add note makes a highlight carrying the note, in one undo step. A bar sits above the selection, else below, inside the page area; it waits for the mouse button to come up, hides on scroll, zoom, Esc and when the selection goes, and is reached by Tab from the page (arrow keys move inside, Esc goes back). Multi-line text marked for redaction no longer fails in MuPDF (no cross-out on multi-quad marks). Search tools (`ui/command_search.py`) shares the palette's matching; below a 1000 px ribbon or in compact mode it is a search button. Preferences ▸ Tools ▸ Show mini toolbars)_
- [ ] **U8 Feedback.** Non-blocking toasts at the bottom right replace transient status-bar messages. They come in info, success and error styles, and can carry an action, for example "3 pages deleted · Undo" or "Saved · Show in folder". Running jobs (OCR, export, optimize, compare) appear as a progress chip with Cancel instead of a modal progress dialog. Clicking the chip opens job details. Destructive confirmations (redact, flatten, sanitize) use a red primary button and say exactly what will be lost.
- [x] **U9 Dialog consistency.** One dialog base class sets token margins, a title/subtitle header, `QFormLayout` with right-aligned labels, an accent primary button and help text under fields. The large dialogs (`pages`, `export`, `optimize`, `redaction`, `compare`, `ocr`, `print_dialog`) move onto it, with sections that can be collapsed instead of long forms. Preferences gets a sidebar of categories: General, Appearance, Tools, Performance, Shortcuts. _(`ui/dialogs/base.py`: `FormDialog` and a collapsible `Section`. Every dialog in `ui/dialogs/` is on it except the Compare window and the password prompt, which is a `QInputDialog`. Section forms line up with the main form. Apply Redactions and Sanitize use the red primary button. Preferences has a Shortcuts page that opens the shortcut editor)_
- [ ] **U10 Accessibility, i18n and DPI for the new UI.** Every new widget is reachable by keyboard: the rail with arrow keys, F6 to cycle between ribbon, panel, canvas and status bar, and a visible focus ring from tokens. Every new widget has a screen-reader name (extend the existing test). New strings go through `tr()`. Check layouts at 100/150/200 % scale and on a 1280×720 window.
- [ ] **U11 Verification.** Golden screenshots in `tests/golden/ui/` (light and dark, at 1× and 2×) of the start screen, a document with the rail open, the floating mini toolbar and a toast, compared with a tolerance. GUI tests for the rail toggle, ribbon collapse and overflow, start-screen recent files, toasts with Undo and the zoom anchor. Performance must not regress: window-to-first-paint stays within 50 ms of the current figure, scrolling 1000 pages stays at ≥60 fps, and the exe stays within its size budget. `--self-test` checks that the QSS and tokens load when frozen. A hands-on pass of every ribbon tab in both themes before ticking.

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
