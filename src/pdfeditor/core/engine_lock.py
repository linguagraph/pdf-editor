"""Process-wide serialization of PDF-engine access, and garbage collection that respects it.

MuPDF's global context is not thread-safe, even across different documents, and PyMuPDF frees
native objects from ``__del__``. Python's cyclic garbage collector can run in *any* thread at
*any* allocation, so an automatic collection on the GUI thread could free MuPDF objects while a
render worker is inside MuPDF and corrupt the heap. Therefore:

* every engine call holds :data:`ENGINE_LOCK` (sessions share it as ``session.lock``);
* automatic cyclic GC is disabled while the app runs and :func:`collect` runs it periodically
  while holding the lock.
"""

from __future__ import annotations

import gc
import threading

ENGINE_LOCK = threading.RLock()


def install_manual_gc() -> None:
    """Turn off automatic cyclic collection (reference counting still frees most objects)."""
    gc.disable()


def collect() -> int:
    """Run a cyclic collection while no engine call can be in progress."""
    with ENGINE_LOCK:
        return gc.collect()
