"""First-page previews of files that aren't open (start screen, recent files).

Like other services this leaves locking to the caller: hold the engine lock around
:func:`first_page_thumbnail`. The document is opened and closed inside the call, so no engine
object outlives it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pdfeditor.engine.base import ColorMode, Engine, EngineError, RenderRequest, RenderResult
from pdfeditor.model.geometry import Matrix

log = logging.getLogger(__name__)


def first_page_thumbnail(
    engine: Engine, path: Path, max_width: int, max_height: int
) -> RenderResult | None:
    """Page 1 of ``path`` scaled to fit ``max_width`` x ``max_height`` pixels.

    Returns None for files that can't be previewed without asking the user anything: missing,
    damaged beyond opening, password protected or empty.
    """
    try:
        doc = engine.open(path)
    except (EngineError, OSError) as exc:  # PasswordRequired is an OpenError
        log.debug("no thumbnail for %s: %s", path, exc)
        return None
    try:
        if doc.page_count == 0:
            return None
        page = doc.page(0)
        rect = page.rect
        if rect.width <= 0 or rect.height <= 0:
            return None
        scale = min(max_width / rect.width, max_height / rect.height)
        return page.render(RenderRequest(matrix=Matrix.scale(scale), color=ColorMode.RGB))
    except EngineError as exc:
        log.debug("thumbnail of %s failed: %s", path, exc)
        return None
    finally:
        doc.close()
