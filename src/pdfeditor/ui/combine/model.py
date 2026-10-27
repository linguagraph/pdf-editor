"""Files queued for Combine: one entry per file, in the order they will be merged.

The model owns the order, so drag-and-drop, the keyboard and the context menu all go through
it. Moves relayout (never reset) the model, which keeps the view's selection and scroll
position. Added files start PENDING; a background job fills in page counts and previews.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QCoreApplication,
    QMimeData,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)
from PySide6.QtGui import QPixmap

from pdfeditor.services.assembly import SUPPORTED_SUFFIXES, MergeSource, SourceInfo, is_image
from pdfeditor.services.pages import format_page_ranges, move_order, parse_page_ranges

MIME_COMBINE = "application/x-pdfeditor-combine"
ENTRY_ROLE = Qt.ItemDataRole.UserRole
DETAIL_ROLE = Qt.ItemDataRole.UserRole + 1


class EntryState(Enum):
    PENDING = "pending"  # not looked at yet
    READY = "ready"
    LOCKED = "locked"  # needs a password
    ERROR = "error"  # can't be opened


@dataclass
class CombineEntry:
    uid: int
    path: Path
    image: bool
    state: EntryState = EntryState.PENDING
    page_count: int = 0
    pages: str = ""  # chosen page ranges as typed; "" = all
    password: str | None = None  # kept in memory only, to open the file again
    error: str = ""
    preview: QPixmap | None = None

    def chosen(self) -> list[int] | None:
        """The chosen 0-based pages, or None for all of them. Raises ValueError if the range
        doesn't fit the file."""
        if self.image or not self.pages.strip() or not self.page_count:
            return None
        return parse_page_ranges(self.pages, self.page_count)

    def source(self) -> MergeSource:
        return MergeSource(self.path, self.chosen(), self.password)


def _tr(text: str) -> str:
    return QCoreApplication.translate("CombineModel", text)


_ROOT = QModelIndex()


class CombineModel(QAbstractListModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._entries: list[CombineEntry] = []
        self._uids = itertools.count(1)

    # -- reading --------------------------------------------------------------------------
    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT) -> int:
        return 0 if parent.isValid() else len(self._entries)

    @property
    def entries(self) -> list[CombineEntry]:
        return list(self._entries)

    def entry(self, row: int) -> CombineEntry:
        return self._entries[row]

    def row_of(self, uid: int) -> int:
        return next((i for i, e in enumerate(self._entries) if e.uid == uid), -1)

    def detail(self, entry: CombineEntry) -> str:
        if entry.state is EntryState.PENDING:
            return _tr("Reading…")
        if entry.state is EntryState.LOCKED:
            return _tr("Password protected")
        if entry.state is EntryState.ERROR:
            return _tr("Can't open")
        if entry.image:
            return _tr("Image")
        if entry.pages.strip():
            try:
                shown = format_page_ranges(entry.chosen() or [])
            except ValueError:
                shown = entry.pages.strip()
            return _tr("Pages {pages} of {count}").format(pages=shown, count=entry.page_count)
        if entry.page_count == 1:
            return _tr("1 page")
        return _tr("{n} pages").format(n=entry.page_count)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        entry = self._entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return entry.path.name
        if role == Qt.ItemDataRole.DecorationRole:
            return entry.preview
        if role == Qt.ItemDataRole.ToolTipRole:
            extra = f": {entry.error}" if entry.error else ""
            return f"{entry.path}\n{self.detail(entry)}{extra}"
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return _tr("{name}, {detail}, file {n} of {total}").format(
                name=entry.path.name,
                detail=self.detail(entry),
                n=index.row() + 1,
                total=len(self._entries),
            )
        if role == ENTRY_ROLE:
            return entry
        if role == DETAIL_ROLE:
            return self.detail(entry)
        return None

    # -- editing --------------------------------------------------------------------------
    def add_paths(self, paths: Iterable[Path], at: int | None = None) -> int:
        """Queue ``paths`` (before row ``at``, default the end); returns how many."""
        new = [CombineEntry(next(self._uids), Path(p), is_image(Path(p))) for p in paths]
        if not new:
            return 0
        row = len(self._entries) if at is None else max(0, min(at, len(self._entries)))
        self.beginInsertRows(QModelIndex(), row, row + len(new) - 1)
        self._entries[row:row] = new
        self.endInsertRows()
        self._renumbered()
        return len(new)

    def remove_rows(self, rows: Iterable[int]) -> None:
        for row in sorted({r for r in rows if 0 <= r < len(self._entries)}, reverse=True):
            self.beginRemoveRows(QModelIndex(), row, row)
            del self._entries[row]
            self.endRemoveRows()
        self._renumbered()

    def clear(self) -> None:
        self.beginResetModel()
        self._entries.clear()
        self.endResetModel()

    def move_rows(self, rows: Sequence[int], before: int) -> list[int]:
        """Move ``rows`` so they sit before row ``before`` (original numbering). Returns their
        new rows."""
        valid = [r for r in rows if 0 <= r < len(self._entries)]
        order, new_rows = move_order(len(self._entries), valid, before)
        if order == list(range(len(self._entries))):
            return new_rows
        self.layoutAboutToBeChanged.emit()
        old_indexes = self.persistentIndexList()
        new_of = {old: new for new, old in enumerate(order)}
        self._entries = [self._entries[i] for i in order]
        self.changePersistentIndexList(
            old_indexes, [self.index(new_of[i.row()]) for i in old_indexes]
        )
        self.layoutChanged.emit()
        self._renumbered()
        return new_rows

    def update(
        self, uid: int, info: SourceInfo, preview: QPixmap | None, password: str | None
    ) -> bool:
        """Apply a described file. Ignored if the entry was removed, or got another password
        meanwhile. Returns whether it was applied."""
        row = self.row_of(uid)
        if row < 0 or self._entries[row].password != password:
            return False
        entry = self._entries[row]
        entry.page_count = info.page_count
        entry.error = info.error
        entry.preview = preview
        if info.locked:
            entry.state = EntryState.LOCKED
            entry.password = None  # it didn't fit
        elif info.error:
            entry.state = EntryState.ERROR
        else:
            entry.state = EntryState.READY
        if entry.pages:  # a range typed for an earlier reading may not fit now
            try:
                entry.chosen()
            except ValueError:
                entry.pages = ""
        self._changed(row)
        return True

    def set_password(self, row: int, password: str) -> None:
        """Try ``password`` on the file: it is read again."""
        entry = self._entries[row]
        entry.password = password
        entry.state = EntryState.PENDING
        self._changed(row)

    def set_pages(self, row: int, text: str) -> None:
        """Choose the pages of a PDF ("" = all). Raises ValueError if they don't fit."""
        entry = self._entries[row]
        text = text.strip()
        if text and entry.page_count:
            parse_page_ranges(text, entry.page_count)
        entry.pages = text
        self._changed(row)

    def _changed(self, row: int) -> None:
        index = self.index(row)
        self.dataChanged.emit(index, index)

    def _renumbered(self) -> None:
        """The accessible text ("file 2 of 5") depends on position and total."""
        if self._entries:
            self.dataChanged.emit(self.index(0), self.index(len(self._entries) - 1))

    # -- state ----------------------------------------------------------------------------
    def pending(self) -> list[CombineEntry]:
        return [e for e in self._entries if e.state is EntryState.PENDING]

    def total_pages(self) -> int:
        total = 0
        for e in self._entries:
            if e.state is not EntryState.READY:
                continue
            try:
                chosen = e.chosen()
            except ValueError:
                chosen = None
            total += e.page_count if chosen is None else len(chosen)
        return total

    def ready(self) -> bool:
        """Two or more files, all readable."""
        return len(self._entries) >= 2 and all(e.state is EntryState.READY for e in self._entries)

    def blocking(self) -> tuple[int, int]:
        """How many files are locked, and how many can't be opened."""
        locked = sum(1 for e in self._entries if e.state is EntryState.LOCKED)
        error = sum(1 for e in self._entries if e.state is EntryState.ERROR)
        return locked, error

    def sources(self) -> list[MergeSource]:
        return [e.source() for e in self._entries]

    # -- drag and drop --------------------------------------------------------------------
    def mimeTypes(self) -> list[str]:
        return [MIME_COMBINE, "text/uri-list"]

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if index.isValid():
            return (
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
        return Qt.ItemFlag.ItemIsDropEnabled  # the root: between and after the cards

    def payload(self, rows: Sequence[int]) -> str:
        """The drag's text: which model it comes from (an internal drag is told apart by this,
        not by ``event.source()``) and the rows."""
        return f"{id(self)}:{','.join(str(r) for r in rows)}"

    def mimeData(self, indexes: Sequence[QModelIndex]) -> QMimeData:
        rows = sorted({i.row() for i in indexes if i.isValid()})
        mime = QMimeData()
        mime.setData(MIME_COMBINE, QByteArray(self.payload(rows).encode()))
        return mime

    def internal_rows(self, mime: QMimeData) -> list[int] | None:
        """Rows of an internal drag of this model; None for anything else."""
        if not mime.hasFormat(MIME_COMBINE):
            return None
        text = bytes(mime.data(MIME_COMBINE).data()).decode(errors="replace")
        owner, _, rows = text.partition(":")
        if owner != str(id(self)):
            return None
        try:
            return [int(r) for r in rows.split(",") if r]
        except ValueError:
            return None

    @staticmethod
    def local_files(mime: QMimeData) -> list[Path]:
        """Every local file of a drop (the panel says which ones it skipped)."""
        return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]

    @classmethod
    def dropped_paths(cls, mime: QMimeData) -> list[Path]:
        """Local files of a drop that Combine can use."""
        return [p for p in cls.local_files(mime) if p.suffix.lower() in SUPPORTED_SUFFIXES]

    def dropMimeData(
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        at = len(self._entries) if row < 0 else row
        rows = self.internal_rows(data)
        if rows is not None:
            self.move_rows(rows, at)
            return True
        return self.add_paths(self.dropped_paths(data), at) > 0
