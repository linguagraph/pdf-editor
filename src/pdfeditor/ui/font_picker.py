"""A searchable font picker: document fonts, recents, the base-14 standards, and fonts
installed on this machine, grouped under non-selectable header rows.

Qt-only; talks to :mod:`pdfeditor.services.fonts` for the installed-font catalog."""

from __future__ import annotations

import weakref
from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QComboBox, QCompleter, QWidget

from pdfeditor.model.fonts import FontRef, FontRefKind
from pdfeditor.services.fonts import FontCatalog, cached_catalog, text_scripts
from pdfeditor.ui.jobs import Job
from pdfeditor.ui.settings import AppSettings

STANDARD_FONTS = (
    ("Sans (Helvetica)", "Helvetica"),
    ("Serif (Times)", "Times-Roman"),
    ("Mono (Courier)", "Courier"),
)

_LOADING_TEXT = "Loading installed fonts…"

# module-wide: only one scan runs no matter how many pickers are open, started lazily by the
# first one created (never at app start).
_scan_started = False
_scan_job: Job | None = None
_open_pickers: list[weakref.ref[FontPicker]] = []


def _ensure_scan_started(catalog: FontCatalog) -> None:
    global _scan_started, _scan_job
    if _scan_started:
        return
    _scan_started = True

    def work(job: Job) -> None:
        catalog.scan(token=job.token)

    job = Job(work, label="Scanning installed fonts")
    job.finished.connect(lambda _result: _notify_pickers())
    _scan_job = job
    job.start()


def remember_font(ref: FontRef) -> None:
    """Add ``ref`` to the Recent section (call from a commit path, not merely on browsing)."""
    if ref.kind is FontRefKind.DOCUMENT:
        return  # not a font choice worth remembering across documents
    AppSettings().add_recent_font(ref)
    _notify_pickers()


def _notify_pickers() -> None:
    for ref in list(_open_pickers):
        picker = ref()
        if picker is None:
            continue
        try:
            picker._reload_installed()
        except RuntimeError:
            # the Python wrapper outlived the Qt widget (deleteLater already ran); drop it.
            if ref in _open_pickers:
                _open_pickers.remove(ref)


class FontPicker(QComboBox):
    """Pick a :class:`FontRef`. Editable with a filtering completer; keyboard navigation
    skips the section header rows."""

    font_chosen = Signal(object, str)  # FontRef, display name

    def __init__(
        self,
        parent: QWidget | None = None,
        document_fonts: Sequence[str] = (),
        sample_text: str = "",
    ) -> None:
        super().__init__(parent)
        self.setAccessibleName("Font")
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        line_edit = self.lineEdit()
        if line_edit is not None:
            line_edit.setAccessibleName("Font")
        self._document_fonts = list(document_fonts)
        self._sample_text = sample_text
        self._current: FontRef = FontRef.standard("Helvetica")
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        completer = self.completer()
        if completer is not None:
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._rebuild()
        self.activated.connect(self._on_activated)

        def _forget(dead: weakref.ref[FontPicker]) -> None:
            if dead in _open_pickers:
                _open_pickers.remove(dead)

        _open_pickers.append(weakref.ref(self, _forget))

    # -- building the list -------------------------------------------------------------------
    def _add_header(self, text: str) -> None:
        item = QStandardItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        self._model.appendRow(item)

    def _add_ref(
        self, display: str, ref: FontRef, *, family_font: str | None = None, tooltip: str = ""
    ) -> QStandardItem:
        item = QStandardItem(display)
        item.setData(ref, Qt.ItemDataRole.UserRole)
        if family_font:
            item.setFont(QFont(family_font))
        if tooltip:
            item.setToolTip(tooltip)
        self._model.appendRow(item)
        return item

    def _rebuild(self) -> None:
        current = self._current
        current_text = self.currentText() if self.count() else ""
        self._model.clear()

        if self._document_fonts:
            self._add_header("In this document")
            for name in self._document_fonts:
                self._add_ref(name, FontRef.document(name))

        recents = AppSettings().recent_fonts
        if recents:
            self._add_header("Recent")
            for ref in recents:
                self._add_ref(ref.name or ref.path or ref.kind.value, ref)

        self._add_header("Standard")
        for label, name in STANDARD_FONTS:
            self._add_ref(label, FontRef.standard(name))

        self._add_header("Installed")
        catalog = cached_catalog()
        families = catalog.families()
        if not families:
            _ensure_scan_started(catalog)
            loading = QStandardItem(_LOADING_TEXT)
            loading.setFlags(
                loading.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled
            )
            self._model.appendRow(loading)
        else:
            wanted = text_scripts(self._sample_text) if self._sample_text else frozenset()
            wanted -= {"latin"}  # nearly every font covers latin; only flag the unusual ones

            def sort_key(family: str) -> tuple[int, str]:
                face = catalog.find(family)
                covers = not (face and wanted - face.scripts)
                return (0 if covers else 1, family.casefold())

            for family in sorted(families, key=sort_key):
                face = catalog.find(family)
                if face is None:
                    continue
                ref = FontRef.file(face.path, face.index, family)
                display = family
                missing = wanted - face.scripts if wanted else frozenset()
                if missing:
                    display = f"{family} (no {', '.join(sorted(missing))})"
                tooltip = face.reason if not face.embeddable else ""
                item = self._add_ref(display, ref, family_font=family, tooltip=tooltip)
                if not face.embeddable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)

        self._select_ref(current, current_text)

    def _reload_installed(self) -> None:
        self._rebuild()

    # -- selection -----------------------------------------------------------------------------
    def _find_row(self, ref: FontRef) -> int:
        for row in range(self._model.rowCount()):
            item = self._model.item(row)
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, FontRef) and data == ref:
                return row
        return -1

    def _select_ref(self, ref: FontRef, fallback_text: str) -> None:
        row = self._find_row(ref)
        if row >= 0:
            self.setCurrentIndex(row)
            self._current = ref
            return
        # not in any section (e.g. a document font not in this block's list): show it anyway.
        self.setCurrentIndex(-1)
        self.setEditText(fallback_text or ref.name)
        self._current = ref

    def _on_activated(self, row: int) -> None:
        item = self._model.item(row)
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, FontRef):
            return  # a header row; shouldn't be reachable, but never select it
        self._current = data
        self.font_chosen.emit(data, item.text())

    def current_ref(self) -> FontRef:
        return self._current

    def set_variant(self, ref: FontRef) -> None:
        """Swap in a bold/italic face of the same family without changing what's displayed."""
        self._current = ref

    def current_display(self) -> str:
        return self.currentText()

    def set_selection(self, ref: FontRef, display: str = "") -> None:
        """Preselect ``ref`` (editing an existing block/mark, or restoring the last style)."""
        self._select_ref(ref, display)

    def set_sample_text(self, text: str) -> None:
        """Re-sort/flag the Installed section for the script(s) actually being typed."""
        if text == self._sample_text:
            return
        self._sample_text = text
        self._rebuild()
