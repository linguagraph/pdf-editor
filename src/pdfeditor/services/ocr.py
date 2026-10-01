"""OCR: language data management and document/batch recognition (headless)."""

from __future__ import annotations

import shutil
import tempfile
import urllib.request
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from pdfeditor.bundle import data_path
from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.core.paths import data_dir
from pdfeditor.engine.base import Document, Engine, SaveOptions

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


@dataclass
class OcrResult:
    layers: dict[int, bytes] = field(default_factory=dict)  # page -> invisible text layer
    skipped: list[int] = field(default_factory=list)


def recognize(
    doc: Document,
    pages: Sequence[int],
    options: OcrOptions,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
    lock: AbstractContextManager[object] | None = None,
) -> OcrResult:
    """Recognize ``pages`` (read-only). ``lock``, if given, is held per page so other work
    (rendering) can interleave with a long OCR run."""
    tessdata = tessdata_for(options.languages)
    language = "+".join(options.languages)
    result = OcrResult()
    for n, index in enumerate(pages):
        if token is not None:
            token.check()
        with lock if lock is not None else nullcontext():
            if options.skip_pages_with_text and page_has_text(doc, index):
                result.skipped.append(index)
            else:
                result.layers[index] = doc.page(index).ocr_text_layer(
                    language, options.dpi, tessdata, options.preprocess, options.deskew
                )
        progress(n + 1, len(pages))
    return result


def apply(doc: Document, result: OcrResult) -> None:
    for index, layer in result.layers.items():
        doc.page(index).add_text_layer(layer)


def ocr_files(
    engine: Engine,
    paths: Sequence[Path],
    out_dir: Path,
    options: OcrOptions,
    token: CancelToken | None = None,
    progress: ProgressFn = no_progress,
) -> list[Path]:
    """Batch OCR: write a searchable copy of each file to ``out_dir`` (same file name)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for n, path in enumerate(paths):
        if token is not None:
            token.check()
        doc = engine.open(path)
        try:
            result = recognize(doc, list(range(doc.page_count)), options, token)
            apply(doc, result)
            written.append(doc.save(out_dir / path.name, SaveOptions(garbage=3)))
        finally:
            doc.close()
        progress(n + 1, len(paths))
    return written
