"""Run Tesseract (MuPDF's built-in OCR) in a helper process.

MuPDF's OCR holds Python's GIL for the whole recognition of a page, about a second at 300 dpi.
Run on a worker thread it would still stop every other Python thread for that long, the GUI
included: no repaint, no progress, no Cancel. A process of its own keeps the window live.

The worker gets the rendered page as raw samples and returns the searchable-PDF bytes; it
opens no document, so it shares no MuPDF state with the app. If a process can't be started
(or dies), recognition falls back to running in this process.
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import pymupdf

log = logging.getLogger(__name__)

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_disabled = False  # starting a helper process failed once: stay in process


def recognize_samples(
    samples: bytes, width: int, height: int, gray: bool, dpi: int, language: str, tessdata: str
) -> bytes:
    """A one-page searchable PDF (image plus invisible text) for the given pixels."""
    cs = pymupdf.csGRAY if gray else pymupdf.csRGB
    pix = pymupdf.Pixmap(cs, width, height, samples, False)
    pix.set_dpi(dpi, dpi)
    return bytes(pix.pdfocr_tobytes(language=language, tessdata=tessdata))


def _get_pool() -> ProcessPoolExecutor | None:
    global _pool
    with _pool_lock:
        if _pool is None and not _disabled:
            _pool = ProcessPoolExecutor(
                max_workers=1, mp_context=multiprocessing.get_context("spawn")
            )
        return _pool


def _drop_pool(disable: bool) -> None:
    global _pool, _disabled
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
        _disabled = _disabled or disable


def recognize(pix: pymupdf.Pixmap, dpi: int, language: str, tessdata: str) -> bytes:
    """Like ``pix.pdfocr_tobytes``, but in the helper process when possible."""
    if pix.alpha or pix.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    args = (bytes(pix.samples), pix.width, pix.height, pix.n == 1, dpi, language, tessdata)
    pool = _get_pool()
    if pool is not None:
        try:
            return pool.submit(recognize_samples, *args).result()
        except BrokenProcessPool:
            log.warning("the OCR helper process stopped; recognizing in the app instead")
            _drop_pool(disable=False)
        except (OSError, RuntimeError):
            log.warning("can't start an OCR helper process; recognizing in the app", exc_info=True)
            _drop_pool(disable=True)
    return recognize_samples(*args)


def shutdown() -> None:
    """Stop the helper process (tests, app exit)."""
    _drop_pool(disable=False)
