"""OCR: language data management and document/batch recognition (headless)."""

from __future__ import annotations

import shutil
import tempfile
import urllib.request
from collections.abc import Callable, Collection, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path

from pdfeditor.bundle import data_path
from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.core.paths import data_dir
from pdfeditor.engine.base import Document, Engine, SaveOptions
from pdfeditor.model.scan import ScanCleanup, ScanTextPlan

DOWNLOAD_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"
# Common languages offered for download (Tesseract codes).
COMMON_LANGUAGES = {
    "eng": "English",
    "deu": "German",
    "fra": "French",
    "spa": "Spanish",
    "ita": "Italian",
    "por": "Portuguese",
    "nld": "Dutch",
    "pol": "Polish",
    "bul": "Bulgarian",
    "rus": "Russian",
    "ukr": "Ukrainian",
    "ces": "Czech",
    "ell": "Greek",
    "tur": "Turkish",
    "chi_sim": "Chinese (Simplified)",
    "jpn": "Japanese",
    "kor": "Korean",
    "ara": "Arabic",
}
DOWNLOAD_CHUNK = 64 * 1024
MIN_TEXT_CHARS = 20  # pages with at least this much extractable text are skipped
DEFAULT_LANGUAGE = "eng"

StatusFn = Callable[[str], None]  # what a step is doing, e.g. "page 2 of 5"


def user_tessdata() -> Path:
    path = data_dir() / "tessdata"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bundled_tessdata() -> Path | None:
    try:
        return data_path("tessdata")
    except FileNotFoundError:
        return None


def installed_languages() -> dict[str, Path]:
    """Language code -> folder holding its .traineddata (user downloads override the bundle)."""
    out: dict[str, Path] = {}
    for folder in (bundled_tessdata(), user_tessdata()):
        if folder is None:
            continue
        for f in folder.glob("*.traineddata"):
            out[f.stem] = folder
    return out


def tessdata_for(languages: Sequence[str]) -> Path:
    """A folder containing every requested language (MuPDF takes a single tessdata path)."""
    installed = installed_languages()
    missing = [lang for lang in languages if lang not in installed]
    if missing:
        raise ValueError(f"OCR language data not installed: {', '.join(missing)}")
    folders = {installed[lang] for lang in languages}
    if len(folders) == 1:
        return folders.pop()
    merged = Path(tempfile.mkdtemp(prefix="pdfeditor-tessdata-"))
    for lang in languages:
        shutil.copy2(installed[lang] / f"{lang}.traineddata", merged / f"{lang}.traineddata")
    return merged


def pick_languages(saved: Sequence[str], installed: Collection[str]) -> tuple[str, ...]:
    """The remembered OCR languages that are still installed, in their saved order.

    A language can go away (its downloaded data deleted), so when none of the saved ones is
    left this falls back to English, else to the first installed language. Empty only when
    no language is installed at all.
    """
    picked = tuple(dict.fromkeys(lang for lang in saved if lang in installed))
    if picked:
        return picked
    if DEFAULT_LANGUAGE in installed:
        return (DEFAULT_LANGUAGE,)
    return (min(installed),) if installed else ()


def language_name(code: str) -> str:
    """``Bulgarian (bul)``, or just the code for a language without a known name."""
    name = COMMON_LANGUAGES.get(code)
    return f"{name} ({code})" if name else code


def download_language(
    lang: str, token: CancelToken | None = None, progress: ProgressFn = no_progress
) -> Path:
    """Fetch a language into the user folder (the only feature that uses the network).

    ``progress(received, total)`` reports bytes; ``total`` is 0 when the server doesn't say
    how big the file is. A cancelled or failed download leaves nothing behind.
    """
    target = user_tessdata() / f"{lang}.traineddata"
    tmp = target.with_suffix(".part")
    url = DOWNLOAD_URL.format(lang=lang)
    try:
        with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as out:
            try:
                total = max(0, int(response.headers.get("Content-Length") or 0))
            except ValueError:
                total = 0
            received = 0
            progress(0, total)
            while chunk := response.read(DOWNLOAD_CHUNK):
                if token is not None:
                    token.check()
                out.write(chunk)
                received += len(chunk)
                progress(received, max(total, received) if total else 0)
        if token is not None:
            token.check()
        if received == 0:
            raise OSError(f"The download of {language_name(lang)} was empty.")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def page_has_text(doc: Document, index: int, min_chars: int = MIN_TEXT_CHARS) -> bool:
    text = doc.page(index).text_page(with_chars=False).text
    return len("".join(text.split())) >= min_chars


@dataclass(frozen=True)
class OcrOptions:
    languages: tuple[str, ...] = ("eng",)
    dpi: int = 300
    skip_pages_with_text: bool = True
    preprocess: bool = False
    deskew: bool = False
    # KEEP: searchable (invisible text over the scan); ERASE/REMOVE: editable, visible text
    cleanup: ScanCleanup = ScanCleanup.KEEP


@dataclass
class OcrResult:
    layers: dict[int, bytes] = field(default_factory=dict)  # page -> invisible text layer
    skipped: list[int] = field(default_factory=list)
    # page -> how to make its recognized text editable (editable output only)
    plans: dict[int, ScanTextPlan] = field(default_factory=dict)

    @property
    def changed_pages(self) -> list[int]:
        return sorted(set(self.layers) | set(self.plans))


def recognize(
    doc: Document,
    pages: Sequence[int],
    options: OcrOptions,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
    lock: AbstractContextManager[object] | None = None,
    status: StatusFn | None = None,
) -> OcrResult:
    """Recognize ``pages`` (read-only). ``lock``, if given, is held per page so other work
    (rendering) can interleave with a long OCR run.

    ``status`` hears which page is being read *before* the work on it starts: one page can
    take Tesseract several seconds, and ``progress`` only moves once it is done.

    With editable output, a skipped page whose only text is an earlier, invisible OCR layer
    gets a plan too, so it becomes editable without being recognized again."""
    tessdata = tessdata_for(options.languages)
    language = "+".join(options.languages)
    result = OcrResult()
    for n, index in enumerate(pages):
        if token is not None:
            token.check()
        if status is not None:
            status(f"page {n + 1} of {len(pages)}")
        with lock if lock is not None else nullcontext():
            page = doc.page(index)
            layer: bytes | None = None
            if options.skip_pages_with_text and page_has_text(doc, index):
                result.skipped.append(index)
                editable = options.cleanup is not ScanCleanup.KEEP
                if not (editable and page.scan_info().has_hidden_ocr):
                    progress(n + 1, len(pages))
                    continue
            else:
                layer = page.ocr_text_layer(
                    language, options.dpi, tessdata, options.preprocess, options.deskew
                )
                result.layers[index] = layer
            if options.cleanup is not ScanCleanup.KEEP:
                plan = page.scan_text_plan(layer, options.cleanup)
                if plan is not None:
                    result.plans[index] = plan
        progress(n + 1, len(pages))
    return result


def apply(doc: Document, result: OcrResult) -> None:
    for index in result.changed_pages:
        page = doc.page(index)
        layer = result.layers.get(index)
        if layer is not None:
            page.add_text_layer(layer)
        plan = result.plans.get(index)
        if plan is not None:
            page.apply_scan_text_plan(plan)


def scan_pages_to_edit(
    doc: Document,
    token: CancelToken | None = None,
    lock: AbstractContextManager[object] | None = None,
) -> list[int]:
    """Scanned pages whose text can't be edited yet: never recognized, or recognized only
    as an invisible (searchable) layer."""
    out = []
    for index in range(doc.page_count):
        if token is not None:
            token.check()
        with lock if lock is not None else nullcontext():
            info = doc.page(index).scan_info()
        if info.needs_ocr or info.has_hidden_ocr:
            out.append(index)
    return out


def make_scans_editable(
    doc: Document,
    options: OcrOptions,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
    lock: AbstractContextManager[object] | None = None,
    status: StatusFn | None = None,
) -> OcrResult:
    """Recognize every scanned page that isn't editable yet and plan visible text for it
    (read-only; :func:`apply` makes the change). Used when the Edit tool opens a scan."""
    pages = scan_pages_to_edit(doc, token, lock)
    if not pages:
        return OcrResult()
    cleanup = ScanCleanup.ERASE if options.cleanup is ScanCleanup.KEEP else options.cleanup
    editable = replace(options, skip_pages_with_text=True, cleanup=cleanup)
    return recognize(doc, pages, editable, token, progress, lock, status)


def _prefixed(status: StatusFn, prefix: str) -> StatusFn:
    return lambda text: status(prefix + text)


def ocr_files(
    engine: Engine,
    paths: Sequence[Path],
    out_dir: Path,
    options: OcrOptions,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
    status: StatusFn | None = None,
) -> list[Path]:
    """Batch OCR: write a searchable copy of each file to ``out_dir`` (same file name).

    ``status`` hears the file being worked on ("file 2 of 3: name.pdf") and, while it is
    recognized, its page ("file 2 of 3: name.pdf, page 4 of 9")."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for n, path in enumerate(paths):
        if token is not None:
            token.check()
        current = f"file {n + 1} of {len(paths)}: {path.name}"
        if status is not None:
            status(current)
        page_status = None if status is None else _prefixed(status, f"{current}, ")
        doc = engine.open(path)
        try:
            result = recognize(doc, list(range(doc.page_count)), options, token, status=page_status)
            apply(doc, result)
            written.append(doc.save(out_dir / path.name, SaveOptions(garbage=3)))
        finally:
            doc.close()
        progress(n + 1, len(paths))
    return written
