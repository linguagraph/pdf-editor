"""Temp-then-swap for files produced outside the engine (the engine's own save has its own)."""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def replaced_atomically(target: Path) -> Iterator[Path]:
    """Yield a temp path next to ``target``. If the block finishes, write and verify it there:
    the temp file then replaces ``target`` in one step. If the block raises, ``target`` is
    untouched and the temp file is gone."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        yield tmp
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
