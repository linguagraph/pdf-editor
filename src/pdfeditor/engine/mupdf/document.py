"""PyMuPDF implementation of :class:`pdfeditor.engine.base.Document`."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pymupdf

from pdfeditor.engine.base import (
    Document,
    EngineError,
    OpenError,
    PasswordCallback,
    PasswordRequired,
    SaveError,
    SaveOptions,
)
from pdfeditor.engine.mupdf import convert as cv
from pdfeditor.engine.mupdf import pages
from pdfeditor.engine.mupdf.page import MuPage
from pdfeditor.model.color import Color
from pdfeditor.model.metadata import (
    DocumentInfo,
    EmbeddedFile,
    EncryptionMethod,
    FontInfo,
    LayerInfo,
    Metadata,
    Permissions,
)
from pdfeditor.model.outline import Destination, OutlineItem, flatten
from pdfeditor.model.pages import PageLabelRule

log = logging.getLogger(__name__)

_META_KEYS = {
    "title": "title",
    "author": "author",
    "subject": "subject",
    "keywords": "keywords",
    "creator": "creator",
    "producer": "producer",
    "creation_date": "creationDate",
    "mod_date": "modDate",
}
MAX_PASSWORD_ATTEMPTS = 5


def _open_fz(source: Path | bytes) -> pymupdf.Document:
    try:
        if isinstance(source, bytes):
            fz = pymupdf.open(stream=source, filetype="pdf")
        else:
            if not source.exists():
                raise OpenError(f"file not found: {source}")
            fz = pymupdf.open(source, filetype="pdf")
    except OpenError:
        raise
    except Exception as exc:
        raise OpenError(f"cannot open PDF: {exc}") from exc
    if not fz.is_pdf:
        fz.close()
        raise OpenError("not a PDF document")
    return fz


def _authenticate(fz: pymupdf.Document, password: str | PasswordCallback | None) -> str | None:
    """Unlock ``fz`` if needed; returns the password that worked (None if not encrypted)."""
    if not fz.needs_pass:
        return None
    if isinstance(password, str):
        if fz.authenticate(password):
            return password
        raise PasswordRequired("wrong password")
    if password is None:
        raise PasswordRequired("document is password protected")
    for attempt in range(1, MAX_PASSWORD_ATTEMPTS + 1):
        candidate = password(attempt)
        if candidate is None:
            break
        if fz.authenticate(candidate):
            return candidate
    raise PasswordRequired("document is password protected")


def open_document(source: Path | bytes, password: str | PasswordCallback | None) -> MuDocument:
    fz = _open_fz(source)
    try:
        used = _authenticate(fz, password)
    except PasswordRequired:
        fz.close()
        raise
    path = source.resolve() if isinstance(source, Path) else None
    if fz.is_repaired:
        log.warning("document was repaired on open: %s", path or "<bytes>")
    return MuDocument(fz, path, used)


class MuDocument:
    def __init__(self, fz: pymupdf.Document, path: Path | None, password: str | None) -> None:
        self._fz = fz
        self._path = path
        self._password = password
        self._pages: dict[int, MuPage] = {}
        self._revisions: dict[int, int] = {}
        self._generation = 0  # bumped whenever the whole document is reloaded
        # Cached so page()/revision never call into MuPDF (they're used without the lock).
        self._page_count = int(fz.page_count)
        self._backing_tmp: Path | None = None  # set when a failed save left us on a temp file
        self._force_dirty = False

    # -- internal ---------------------------------------------------------------------------
    @property
    def fz(self) -> pymupdf.Document:
        return self._fz

    def page_revision(self, index: int) -> int:
        return self._generation * 1_000_000 + self._revisions.get(index, 0)

    def mark_page_changed(self, index: int) -> None:
        self._revisions[index] = self._revisions.get(index, 0) + 1
        page = self._pages.get(index)
        if page is not None:
            page.invalidate()

    def structure_changed(self) -> None:
        """Call after pages were inserted, deleted or moved: resync counts and drop caches."""
        self._reset_pages()

    def _reset_pages(self) -> None:
        self._generation += 1
        self._page_count = int(self._fz.page_count)
        for page in self._pages.values():
            page.invalidate()
        self._pages.clear()

    def _catalog_key(self, key: str) -> tuple[str, str]:
        result: tuple[str, str] = self._fz.xref_get_key(self._fz.pdf_catalog(), key)
        return result

    # -- Document protocol ------------------------------------------------------------------
    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def page_count(self) -> int:
        return self._page_count

    @property
    def is_dirty(self) -> bool:
        return bool(self._fz.is_dirty) or self._force_dirty

    def page(self, index: int) -> MuPage:
        if not 0 <= index < self.page_count:
            raise IndexError(f"page index {index} out of range 0..{self.page_count - 1}")
        page = self._pages.get(index)
        if page is None:
            page = self._pages[index] = MuPage(self, index)
        return page

    def info(self) -> DocumentInfo:
        fz = self._fz
        meta = fz.metadata or {}
        size = None
        if self._path is not None and self._path.exists():
            size = self._path.stat().st_size
        names_js = self._catalog_key("Names/JavaScript")[0] != "null"
        open_action = self._catalog_key("OpenAction")
        open_js = "/JS" in open_action[1] or "/JavaScript" in open_action[1]
        marked = self._catalog_key("MarkInfo/Marked")[1] == "true"
        struct_tree = self._catalog_key("StructTreeRoot")[0] != "null"
        sigflags = fz.get_sigflags()
        return DocumentInfo(
            page_count=self.page_count,
            pdf_version=str(meta.get("format", "")).removeprefix("PDF ").strip(),
            encryption=_encryption_method(meta.get("encryption")),
            permissions=_permissions(fz.permissions),
            is_repaired=bool(fz.is_repaired),
            has_forms=bool(fz.is_form_pdf),
            has_signatures=sigflags is not None and sigflags > 0,
            has_javascript=names_js or open_js,
            is_tagged=marked or struct_tree,
            has_xmp=self._catalog_key("Metadata")[0] == "xref",
            file_size=size,
        )

    def metadata(self) -> Metadata:
        meta = self._fz.metadata or {}
        return Metadata(**{ours: meta.get(theirs) or "" for ours, theirs in _META_KEYS.items()})

    def set_metadata(self, meta: Metadata) -> None:
        self._fz.set_metadata({theirs: getattr(meta, ours) for ours, theirs in _META_KEYS.items()})

    def xmp(self) -> str:
        return str(self._fz.get_xml_metadata())

    def set_xmp(self, xml: str) -> None:
        if xml:
            self._fz.set_xml_metadata(xml)
        else:
            self._fz.del_xml_metadata()

    def outline(self) -> list[OutlineItem]:
        roots: list[OutlineItem] = []
        stack: list[tuple[int, OutlineItem]] = []
        for entry in self._fz.get_toc(simple=False):
            level, title, page_no = int(entry[0]), str(entry[1]), int(entry[2])
            info: dict[str, Any] = entry[3] if len(entry) > 3 else {}
            item = OutlineItem(title=title)
            if info.get("kind") == pymupdf.LINK_URI:
                item.uri = info.get("uri")
            elif page_no >= 1:
                to = info.get("to")
                item.dest = Destination(
                    page_index=page_no - 1,
                    point=cv.point(to) if to is not None else None,
                    zoom=float(info["zoom"]) if info.get("zoom") else None,
                )
            item.is_open = not info.get("collapse", True)
            if info.get("color"):
                item.color = Color(*info["color"])
            item.bold = bool(info.get("bold"))
            item.italic = bool(info.get("italic"))
            while stack and stack[-1][0] >= level:
                stack.pop()
            (stack[-1][1].children if stack else roots).append(item)
            stack.append((level, item))
        return roots

    def set_outline(self, items: Sequence[OutlineItem]) -> None:
        toc: list[list[Any]] = []
        for level, item in flatten(list(items)):
            info: dict[str, Any] = {"collapse": not item.is_open}
            if item.color is not None:
                info["color"] = item.color.rgb()
            if item.bold:
                info["bold"] = True
            if item.italic:
                info["italic"] = True
            if item.uri:
                info.update(kind=pymupdf.LINK_URI, uri=item.uri)
                toc.append([level, item.title, -1, info])
            elif item.dest is not None:
                info["kind"] = pymupdf.LINK_GOTO
                if item.dest.point is not None:
                    info["to"] = cv.to_fz_point(item.dest.point)
                if item.dest.zoom:
                    info["zoom"] = item.dest.zoom
                toc.append([level, item.title, item.dest.page_index + 1, info])
            else:
                toc.append([level, item.title, -1, info])
        self._fz.set_toc(toc)

    def page_label(self, index: int) -> str:
        label = self.page(index).fz.get_label()
        return str(label) if label else str(index + 1)

    def embedded_files(self) -> list[EmbeddedFile]:
        out = []
        for name in self._fz.embfile_names():
            info = self._fz.embfile_info(name)
            out.append(
                EmbeddedFile(
                    name=name,
                    filename=info.get("ufilename") or info.get("filename") or name,
                    size=int(info.get("size") or info.get("length") or 0),
                    description=info.get("description") or "",
                )
            )
        return out

    def extract_embedded_file(self, name: str) -> bytes:
        return bytes(self._fz.embfile_get(name))

    def layers(self) -> list[LayerInfo]:
        return [
            LayerInfo(
                id=int(cfg["number"]),
                name=str(cfg["text"]),
                visible=bool(cfg["on"]),
                depth=int(cfg["depth"]),
                locked=bool(cfg["locked"]),
            )
            for cfg in self._fz.layer_ui_configs()
        ]

    def set_layer_visible(self, layer_id: int, visible: bool) -> None:
        self._fz.set_layer_ui_config(layer_id, 0 if visible else 2)
        # Cached display lists bake in layer visibility.
        for index in range(self.page_count):
            self.mark_page_changed(index)

    def fonts(self) -> list[FontInfo]:
        seen: dict[int, FontInfo] = {}
        for pno in range(self.page_count):
            for xref, ext, ftype, basefont, _name, encoding, *_ in self._fz.get_page_fonts(pno):
                if xref in seen:
                    continue
                subset = len(basefont) > 7 and basefont[6] == "+" and basefont[:6].isupper()
                seen[xref] = FontInfo(
                    name=basefont[7:] if subset else basefont,
                    type=ftype,
                    encoding=encoding,
                    embedded=ext not in ("n/a", ""),
                    subset=subset,
                    ref=xref,
                )
        return list(seen.values())

    # -- saving -----------------------------------------------------------------------------
    def _save_kwargs(self, options: SaveOptions) -> dict[str, Any]:
        return {
            "garbage": options.garbage,
            "deflate": options.deflate,
            "use_objstms": 1 if options.object_streams else 0,
            "clean": options.clean_content,
            "encryption": pymupdf.PDF_ENCRYPT_KEEP,
        }

    def _verify(self, path: Path, expected_pages: int) -> None:
        """Reopen a freshly written file and check it's sound before we trust it."""
        try:
            check = pymupdf.open(path, filetype="pdf")
        except Exception as exc:
            raise SaveError(f"saved file cannot be reopened: {exc}") from exc
        try:
            if check.needs_pass and self._password is not None:
                check.authenticate(self._password)
            if check.is_repaired:
                raise SaveError("saved file needed repair when reopened")
            if check.page_count != expected_pages:
                raise SaveError(
                    f"saved file has {check.page_count} pages, expected {expected_pages}"
                )
        finally:
            check.close()

    def _reopen(self, path: Path) -> None:
        self._fz = _open_fz(path)
        if self._fz.needs_pass:
            self._fz.authenticate(self._password or "")
        self._reset_pages()

    def save(self, path: Path | None = None, options: SaveOptions | None = None) -> Path:
        options = options or SaveOptions()
        base = path or self._path
        if base is None:
            raise SaveError("document has no file name; use save-as")
        target = base.resolve()
        if options.incremental:
            self._save_incremental(target)
        else:
            self._save_full(target, options)
        self._path = target
        self._force_dirty = False
        return target

    def _save_incremental(self, target: Path) -> None:
        if self._path is None or target != self._path or self._backing_tmp is not None:
            raise SaveError("incremental save is only possible to the file the document came from")
        if not self._fz.can_save_incrementally():
            raise SaveError("this document can't be saved incrementally; use a full save")
        backup = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.bak")
        shutil.copy2(target, backup)
        pages = self.page_count
        try:
            self._fz.save(target, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
            self._verify(target, pages)
        except Exception as exc:
            log.error("incremental save failed, restoring backup: %s", exc)
            self._fz.close()
            shutil.copy2(backup, target)
            self._reopen(target)
            if isinstance(exc, SaveError):
                raise
            raise SaveError(str(exc)) from exc
        finally:
            with contextlib.suppress(OSError):
                backup.unlink()

    def _save_full(self, target: Path, options: SaveOptions) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
        pages = self.page_count
        try:
            self._fz.save(tmp, **self._save_kwargs(options))
            self._verify(tmp, pages)
        except Exception as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            if isinstance(exc, SaveError):
                raise
            raise SaveError(str(exc)) from exc
        # MuPDF keeps its source file open (locked on Windows): release it before replacing.
        self._fz.close()
        old_backing = self._backing_tmp
        try:
            os.replace(tmp, target)
        except OSError as exc:
            # Keep all edits by continuing on the verified temp file; the user can retry.
            self._reopen(tmp)
            self._backing_tmp = tmp
            self._force_dirty = True
            raise SaveError(f"cannot replace {target}: {exc}") from exc
        self._reopen(target)
        self._backing_tmp = None
        if old_backing is not None:
            with contextlib.suppress(OSError):
                old_backing.unlink()

    def can_save_incrementally(self) -> bool:
        return (
            self._path is not None
            and self._backing_tmp is None
            and bool(self._fz.name)
            and bool(self._fz.can_save_incrementally())
        )

    def load_state(self, data: bytes) -> None:
        new = _open_fz(data)
        if new.needs_pass and not new.authenticate(self._password or ""):
            new.close()
            raise OpenError("snapshot can't be decrypted with the document's password")
        self._fz.close()
        self._fz = new
        self._reset_pages()

    def select_pages(self, order: Sequence[int]) -> None:
        pages.select(self, order)

    def insert_blank_page(self, at: int, width: float, height: float) -> None:
        pages.insert_blank(self, at, width, height)

    def insert_pages(self, source: Document, pages_: Sequence[int], at: int) -> None:
        if not isinstance(source, MuDocument):
            raise EngineError("can only copy pages between documents of the same engine")
        pages.insert_document(self, source, pages_, at)

    def insert_image_page(self, at: int, image: bytes) -> None:
        pages.insert_image_page(self, at, image)

    def page_label_rules(self) -> list[PageLabelRule]:
        return pages.label_rules(self)

    def set_page_label_rules(self, rules: Sequence[PageLabelRule]) -> None:
        pages.set_label_rules(self, rules)

    def to_bytes(self, options: SaveOptions | None = None) -> bytes:
        options = options or SaveOptions()
        return bytes(self._fz.tobytes(**self._save_kwargs(options)))

    def close(self) -> None:
        self._reset_pages()
        self._fz.close()
        if self._backing_tmp is not None:
            with contextlib.suppress(OSError):
                self._backing_tmp.unlink()
            self._backing_tmp = None


def _encryption_method(value: str | None) -> EncryptionMethod:
    if not value:
        return EncryptionMethod.NONE
    v = value.lower()
    if "aes" in v:
        return EncryptionMethod.AES_256 if "256" in v else EncryptionMethod.AES_128
    if "rc4" in v:
        return EncryptionMethod.RC4_40 if "40" in v else EncryptionMethod.RC4_128
    return EncryptionMethod.UNKNOWN


def _permissions(bits: int) -> Permissions:
    return Permissions(
        print=bool(bits & pymupdf.PDF_PERM_PRINT),
        modify=bool(bits & pymupdf.PDF_PERM_MODIFY),
        copy=bool(bits & pymupdf.PDF_PERM_COPY),
        annotate=bool(bits & pymupdf.PDF_PERM_ANNOTATE),
        fill_forms=bool(bits & pymupdf.PDF_PERM_FORM),
        accessibility=bool(bits & pymupdf.PDF_PERM_ACCESSIBILITY),
        assemble=bool(bits & pymupdf.PDF_PERM_ASSEMBLE),
        print_high_quality=bool(bits & pymupdf.PDF_PERM_PRINT_HQ),
    )
