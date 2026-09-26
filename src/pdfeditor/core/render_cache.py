"""Memory-bounded LRU cache for rendered tiles and thumbnails (thread-safe)."""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar

V = TypeVar("V")

THUMBNAIL_TILE = -1  # tile_x value used for whole-page low-resolution images


@dataclass(frozen=True, slots=True)
class TileKey:
    """Identifies one rendered tile.

    ``scale_milli`` is the quantized device scale (pixels per point * 1000), so zoom levels that
    differ by rounding noise share cache entries. ``revision`` changes whenever the page changes,
    which makes stale tiles unreachable (they age out of the LRU).
    """

    doc_id: int
    page_index: int
    revision: int
    scale_milli: int
    rotation: int
    tile_x: int
    tile_y: int
    variant: str = ""  # e.g. "night" for inverted rendering

    @property
    def scale(self) -> float:
        return self.scale_milli / 1000


def quantize_scale(scale: float) -> int:
    """Snap a device scale to ~2% steps so continuous zooming reuses tiles."""
    if scale <= 0:
        raise ValueError("scale must be positive")
    step = 1.02
    n = round(math.log(scale) / math.log(step))
    return max(1, round(1000 * step**n))


class RenderCache(Generic[V]):
    def __init__(self, max_bytes: int = 256 * 1024 * 1024) -> None:
        self.max_bytes = max_bytes
        self._items: OrderedDict[TileKey, tuple[V, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def size_bytes(self) -> int:
        return self._bytes

    def get(self, key: TileKey) -> V | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            self._items.move_to_end(key)
            return entry[0]

    def __contains__(self, key: TileKey) -> bool:
        with self._lock:
            return key in self._items

    def put(self, key: TileKey, value: V, cost: int) -> None:
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            self._items[key] = (value, cost)
            self._bytes += cost
            while self._bytes > self.max_bytes and len(self._items) > 1:
                _, (_, evicted_cost) = self._items.popitem(last=False)
                self._bytes -= evicted_cost

    def drop_document(self, doc_id: int) -> None:
        with self._lock:
            for key in [k for k in self._items if k.doc_id == doc_id]:
                self._bytes -= self._items.pop(key)[1]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0
