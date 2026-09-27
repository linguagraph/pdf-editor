"""Custom image stamps: a user's library of PNG/JPEG images used as stamp appearances.

Images are copied into a folder under the per-user data directory, so a stamp keeps working
when the original file is moved or deleted. The library only manages files; the UI remembers
the order in its settings and passes it to :meth:`StampLibrary.stamps`.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pdfeditor.core.paths import data_dir
from pdfeditor.model.geometry import Point, Rect

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # a stamp is embedded in every document it's used in
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# JPEG start-of-frame markers (baseline, progressive, ...) that carry the image size.
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


class StampImageError(ValueError):
    """The file isn't a PNG or JPEG image this library can use."""


def image_size(data: bytes) -> tuple[int, int] | None:
    """Pixel size of PNG or JPEG ``data``, or ``None`` if it's neither (or damaged)."""
    if data.startswith(_PNG_SIGNATURE) and data[12:16] == b"IHDR" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return (w, h) if w and h else None
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # markers without a length
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker in _JPEG_SOF and i + 9 <= len(data):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return (w, h) if w and h else None
        i += 2 + length
    return None


def stamp_rect(center: Point, size: tuple[int, int], max_side: float = 200.0) -> Rect:
    """Where a click at ``center`` places an image of ``size`` pixels: its size at 96 DPI,
    shrunk to fit ``max_side`` points, centered on the click."""
    w, h = size[0] * 0.75, size[1] * 0.75
    scale = min(1.0, max_side / max(w, h, 1e-6))
    w, h = w * scale, h * scale
    return Rect(center.x - w / 2, center.y - h / 2, center.x + w / 2, center.y + h / 2)


@dataclass(frozen=True)
class CustomStamp:
    path: Path

    @property
    def key(self) -> str:
        """Stable identifier (the file name inside the library)."""
        return self.path.name

    @property
    def label(self) -> str:
        return self.path.stem.replace("_", " ")

    def read(self) -> bytes:
        return self.path.read_bytes()


class StampLibrary:
    def __init__(self, folder: Path | None = None) -> None:
        self.folder = folder if folder is not None else data_dir() / "stamps"

    def stamps(self, order: Sequence[str] = ()) -> list[CustomStamp]:
        """Stamps in ``order`` (keys), then any others in the folder by name."""
        if not self.folder.is_dir():
            return []
        found = {
            p.name: p
            for p in self.folder.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        }
        keys = [k for k in dict.fromkeys(order) if k in found]
        keys += sorted((k for k in found if k not in keys), key=str.lower)
        return [CustomStamp(found[k]) for k in keys]

    def get(self, key: str) -> CustomStamp | None:
        path = self.folder / Path(key).name
        if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
            return CustomStamp(path)
        return None

    def add(self, source: Path, label: str = "") -> CustomStamp:
        """Copy an image into the library; raises :class:`StampImageError` if unusable."""
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise StampImageError(f"can't read {source.name}: {exc.strerror}") from exc
        if size > MAX_IMAGE_BYTES:
            raise StampImageError(f"{source.name} is too large for a stamp")
        data = source.read_bytes()
        if image_size(data) is None:
            raise StampImageError(f"{source.name} is not a PNG or JPEG image")
        suffix = ".png" if data.startswith(_PNG_SIGNATURE) else ".jpg"
        stem = re.sub(r"[^\w\- ]+", "_", label or source.stem).strip(" ._") or "stamp"
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / f"{stem}{suffix}"
        n = 2
        while target.exists():
            target = self.folder / f"{stem} ({n}){suffix}"
            n += 1
        target.write_bytes(data)
        return CustomStamp(target)

    def remove(self, key: str) -> None:
        stamp = self.get(key)
        if stamp is not None:
            stamp.path.unlink(missing_ok=True)
