"""Catalog of fonts installed on this machine: where to look, how to read a face, and a
cache so the app doesn't re-read every font file on every launch.

Headless (no Qt): the UI runs :meth:`FontCatalog.scan` as a background job.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
from collections.abc import Iterable
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.ttLib.ttCollection import TTCollection

from pdfeditor.core.jobs import CancelToken, ProgressFn, no_progress
from pdfeditor.core.paths import data_dir
from pdfeditor.model.fonts import BITMAP_ONLY, RESTRICTED, FontFace, FontRef, FontRefKind

logger = logging.getLogger(__name__)

# fontTools warns about some installed fonts' implausible 'created' timestamp; it's noise here.
logging.getLogger("fontTools.ttLib.tables._h_e_a_d").setLevel(logging.ERROR)

__all__ = [
    "BITMAP_ONLY",
    "RESTRICTED",
    "FontCatalog",
    "cached_catalog",
    "installed_ref_for_font_name",
    "normalize_font_name",
    "read_faces",
    "system_font_dirs",
    "text_scripts",
]

_SKIP_SUFFIXES = {".fon", ".fnt", ".pfb", ".pfm"}
_FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}
CACHE_VERSION = 1

# coarse cmap coverage: representative code point -> script label
_SCRIPT_PROBES: tuple[tuple[str, int], ...] = (
    ("latin", ord("A")),
    ("cyrillic", 0x0410),  # capital A
    ("greek", 0x0391),  # capital alpha
    ("cjk", 0x4E2D),  # "middle"
    ("arabic", 0x0627),  # alef
    ("hebrew", 0x05D0),  # alef
)

# same coarse buckets as _SCRIPT_PROBES, but as Unicode ranges, for classifying a piece of text
# rather than a font's cmap.
_SCRIPT_RANGES: tuple[tuple[str, int, int], ...] = (
    ("cyrillic", 0x0400, 0x04FF),
    ("greek", 0x0370, 0x03FF),
    ("cjk", 0x3040, 0x30FF),  # hiragana/katakana
    ("cjk", 0x3400, 0x9FFF),  # CJK unified ideographs (+ extension A)
    ("cjk", 0xAC00, 0xD7A3),  # hangul syllables
    ("arabic", 0x0600, 0x06FF),
    ("hebrew", 0x0590, 0x05FF),
)

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
_STYLE_SUFFIX = re.compile(r"[-, ]?(Bold\s*Italic|BoldItalic|Bold|Italic|Oblique)$", re.IGNORECASE)


def text_scripts(text: str) -> frozenset[str]:
    """Coarse scripts used in ``text`` (the same buckets as :data:`_SCRIPT_PROBES`).

    Used to flag an installed font that doesn't cover what's being typeset (e.g. no Cyrillic).
    """
    found: set[str] = set()
    for ch in text:
        cp = ord(ch)
        if ch.isalpha() and cp < 0x0250:
            found.add("latin")
            continue
        for label, lo, hi in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                found.add(label)
                break
    return frozenset(found)


def normalize_font_name(name: str) -> tuple[str, bool, bool]:
    """Split a PDF/engine font name into ``(family, bold, italic)``.

    Strips a subset prefix (``ABCDEF+Calibri``) and a style suffix (``-Bold``, ``,BoldItalic``,
    ``-Italic``, ``-Oblique``), so a name MuPDF wrote on save, like ``"ABCDEF+Calibri-Bold"``,
    matches the catalog family ``"Calibri"`` with ``bold=True``.
    """
    base = _SUBSET_PREFIX.sub("", name)
    bold = False
    italic = False
    while True:
        match = _STYLE_SUFFIX.search(base)
        if not match:
            break
        token = match.group(1).lower().replace(" ", "")
        if "bold" in token:
            bold = True
        if "italic" in token or "oblique" in token:
            italic = True
        base = base[: match.start()].rstrip("-, ")
    return base, bold, italic


def _family_key(name: str) -> str:
    key = "".join(c for c in name.lower() if c.isalnum())
    for suffix in ("psmt", "mt", "ps"):
        if key.endswith(suffix) and len(key) > len(suffix) + 2:
            return key[: -len(suffix)]
    return key


def same_font_family(a: str, b: str) -> bool:
    """Whether two font names (PDF BaseFont, extracted span font or family) are one family."""
    fa, fb = normalize_font_name(a)[0], normalize_font_name(b)[0]
    return bool(fa) and _family_key(fa) == _family_key(fb)


def installed_ref_for_font_name(
    catalog: FontCatalog, font_name: str, bold: bool = False, italic: bool = False
) -> FontRef | None:
    """The catalog's best face for ``font_name`` (after normalizing it), or ``None``.

    For reusing an installed font after reopening a document: the embedded font program MuPDF
    wrote loses its original name/cmap tables, so a later edit needs to recognize the family
    from the (possibly subsetted, styled) name alone.
    """
    family, name_bold, name_italic = normalize_font_name(font_name)
    if not family:
        return None
    face = catalog.find(family, bold or name_bold, italic or name_italic)
    if face is None:
        # PDF names drop the spaces ("TimesNewRomanPSMT", "SegoeUI"): compare loosely
        wanted = _family_key(family)
        match = next((f for f in catalog.families() if _family_key(f) == wanted), None)
        if match is not None:
            face = catalog.find(match, bold or name_bold, italic or name_italic)
    if face is None or not face.embeddable:
        return None
    return FontRef.file(face.path, face.index, face.family)


def system_font_dirs() -> list[Path]:
    """Existing, platform-appropriate directories to scan for installed fonts."""
    system = platform.system()
    candidates: list[Path] = []
    if system == "Windows":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        candidates.append(Path(windir) / "Fonts")
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Microsoft" / "Windows" / "Fonts")
    elif system == "Darwin":
        candidates += [
            Path("/Library/Fonts"),
            Path("/System/Library/Fonts"),
            Path.home() / "Library" / "Fonts",
        ]
    else:
        candidates += [
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".local" / "share" / "fonts",
            Path.home() / ".fonts",
        ]
    return [path for path in candidates if path.is_dir()]


def _iter_font_files(dirs: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for root in dirs:
        try:
            walker = root.rglob("*")
        except OSError:
            continue
        for path in walker:
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            suffix = path.suffix.lower()
            if suffix in _SKIP_SUFFIXES or suffix not in _FONT_SUFFIXES:
                continue
            files.append(path)
    return files


def _script_coverage(font: TTFont) -> frozenset[str]:
    try:
        cmap = font.getBestCmap() or {}
    except Exception:
        return frozenset()
    return frozenset(label for label, probe in _SCRIPT_PROBES if probe in cmap)


def _name(font: TTFont, preferred: int, fallback: int) -> str:
    names = font["name"]
    value = names.getDebugName(preferred) or names.getDebugName(fallback)
    return value or ""


def _embeddable(font: TTFont) -> tuple[bool, str]:
    os2 = font.get("OS/2")
    if os2 is None:
        return True, ""
    fs_type = int(getattr(os2, "fsType", 0))
    if fs_type & RESTRICTED:
        return False, "restricted by fsType"
    if fs_type & BITMAP_ONLY:
        return False, "bitmap-only by fsType"
    return True, ""


def _face_from_ttfont(font: TTFont, path: Path, index: int) -> FontFace | None:
    family = _name(font, 16, 1)
    if not family:
        return None
    style = _name(font, 17, 2) or "Regular"
    os2 = font.get("OS/2")
    head = font.get("head")
    weight = int(getattr(os2, "usWeightClass", 400)) if os2 is not None else 400
    fs_selection = int(getattr(os2, "fsSelection", 0)) if os2 is not None else 0
    mac_style = int(getattr(head, "macStyle", 0)) if head is not None else 0
    italic = bool(fs_selection & 0x01) or bool(mac_style & 0x02)
    embeddable, reason = _embeddable(font)
    return FontFace(
        family=family,
        style=style,
        weight=weight,
        italic=italic,
        path=str(path),
        index=index,
        embeddable=embeddable,
        reason=reason,
        scripts=_script_coverage(font),
        variable="fvar" in font,
    )


_TABLES = ("name", "OS/2", "head", "cmap", "fvar")


def read_faces(path: Path) -> list[FontFace]:
    """Every face in ``path``, skipping any that can't be read. Never raises."""
    suffix = path.suffix.lower()
    if suffix in _SKIP_SUFFIXES or suffix not in _FONT_SUFFIXES:
        return []
    try:
        if suffix in (".ttc", ".otc"):
            collection = TTCollection(str(path), lazy=True)
            try:
                faces = []
                for index, font in enumerate(collection.fonts):
                    face = _face_from_ttfont(font, path, index)
                    if face is not None:
                        faces.append(face)
                return faces
            finally:
                collection.close()
        font = TTFont(str(path), lazy=True, fontNumber=0)
        try:
            face = _face_from_ttfont(font, path, 0)
        finally:
            font.close()
        return [face] if face is not None else []
    except Exception:
        logger.debug("unreadable font file: %s", path, exc_info=True)
        return []


def _cache_path() -> Path:
    return data_dir() / "fonts.json"


def _file_key(path: Path) -> tuple[int, float] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime


def _face_to_json(face: FontFace) -> dict[str, object]:
    return {
        "family": face.family,
        "style": face.style,
        "weight": face.weight,
        "italic": face.italic,
        "index": face.index,
        "embeddable": face.embeddable,
        "reason": face.reason,
        "scripts": sorted(face.scripts),
        "variable": face.variable,
    }


def _face_from_json(path: str, data: dict[str, object]) -> FontFace:
    scripts = data["scripts"]
    if not isinstance(scripts, list):  # a damaged cache file: skip the entry, never crash
        raise ValueError("bad cache entry: scripts")
    return FontFace(
        family=str(data["family"]),
        style=str(data["style"]),
        weight=int(str(data["weight"])),
        italic=bool(data["italic"]),
        path=path,
        index=int(str(data["index"])),
        embeddable=bool(data["embeddable"]),
        reason=str(data["reason"]),
        scripts=frozenset(scripts),
        variable=bool(data["variable"]),
    )


class FontCatalog:
    """An indexed view of the font files on disk, cached in ``data_dir()/fonts.json``."""

    def __init__(self) -> None:
        self._faces: list[FontFace] = []
        self._by_path: dict[str, list[FontFace]] = {}

    def faces(self) -> list[FontFace]:
        return list(self._faces)

    def families(self) -> list[str]:
        names = {face.family for face in self._faces}
        return sorted(names, key=str.casefold)

    def faces_of(self, family: str) -> list[FontFace]:
        return [face for face in self._faces if face.family == family]

    def find(self, family: str, bold: bool = False, italic: bool = False) -> FontFace | None:
        """The face of ``family`` that best matches ``bold``/``italic``, or ``None``."""
        candidates = self.faces_of(family)
        if not candidates:
            return None

        def score(face: FontFace) -> tuple[int, int]:
            is_bold = face.weight >= 600
            return (int(is_bold != bold), int(face.italic != italic))

        return min(candidates, key=score)

    def face_for(self, ref: FontRef) -> FontFace | None:
        if ref.kind is not FontRefKind.FILE:
            return None
        for face in self._by_path.get(ref.path, []):
            if face.index == ref.index:
                return face
        return None

    def load_cache(self) -> None:
        """Load the on-disk cache without touching the filesystem for font files.

        Fast path for app startup: shows what was known as of the last scan.
        """
        path = _cache_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return
        entries = raw.get("files")
        if not isinstance(entries, dict):
            return
        faces: list[FontFace] = []
        by_path: dict[str, list[FontFace]] = {}
        for file_path, entry in entries.items():
            try:
                face_list = [_face_from_json(file_path, item) for item in entry["faces"]]
            except (KeyError, TypeError, ValueError):
                continue
            faces.extend(face_list)
            by_path[file_path] = face_list
        self._faces = faces
        self._by_path = by_path

    def scan(
        self,
        dirs: Iterable[Path] | None = None,
        token: CancelToken | None = None,
        progress: ProgressFn = no_progress,
    ) -> None:
        """Rebuild the catalog from ``dirs`` (default: :func:`system_font_dirs`).

        Reuses the cache for files whose size and mtime haven't changed; reads everything
        else. Cancellable: raises :class:`pdfeditor.core.jobs.Cancelled` via ``token``, and
        the cache on disk is left as it was before the scan.
        """
        search_dirs = list(dirs) if dirs is not None else system_font_dirs()
        token = token or CancelToken()

        path = _cache_path()
        cached_entries: dict[str, dict[str, object]] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("version") == CACHE_VERSION:
                entries = raw.get("files")
                if isinstance(entries, dict):
                    cached_entries = entries
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass

        files = _iter_font_files(search_dirs)
        total = len(files)
        new_entries: dict[str, dict[str, object]] = {}
        faces: list[FontFace] = []
        by_path: dict[str, list[FontFace]] = {}

        for done, file_path in enumerate(files, start=1):
            token.check()
            key = _file_key(file_path)
            if key is None:
                progress(done, total)
                continue
            size, mtime = key
            str_path = str(file_path)
            cached = cached_entries.get(str_path)
            if cached is not None and cached.get("size") == size and cached.get("mtime") == mtime:
                try:
                    cached_faces = cached["faces"]
                    assert isinstance(cached_faces, list)
                    face_list = [_face_from_json(str_path, item) for item in cached_faces]
                except (KeyError, TypeError, ValueError, AssertionError):
                    face_list = read_faces(file_path)
            else:
                face_list = read_faces(file_path)
            new_entries[str_path] = {
                "size": size,
                "mtime": mtime,
                "faces": [_face_to_json(face) for face in face_list],
            }
            faces.extend(face_list)
            by_path[str_path] = face_list
            progress(done, total)

        self._faces = faces
        self._by_path = by_path
        try:
            path.write_text(
                json.dumps({"version": CACHE_VERSION, "files": new_entries}), encoding="utf-8"
            )
        except OSError:
            logger.debug("couldn't write font cache: %s", path, exc_info=True)


_shared_catalog: FontCatalog | None = None


def cached_catalog() -> FontCatalog:
    """A process-wide :class:`FontCatalog` loaded from disk (no filesystem scan).

    Call :meth:`FontCatalog.scan` on it (normally as a background job) to refresh it.
    """
    global _shared_catalog
    if _shared_catalog is None:
        _shared_catalog = FontCatalog()
        _shared_catalog.load_cache()
    return _shared_catalog
