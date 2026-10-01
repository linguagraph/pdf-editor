"""Organize ribbon: page commands, document assembly and page stamping for the main window."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox, QToolButton

from pdfeditor.core.commands import SetPageLabelsCommand
from pdfeditor.core.jobs import Cancelled
from pdfeditor.core.session import DocumentSession
from pdfeditor.engine.base import Document, EngineError, OpenError, PasswordRequired
from pdfeditor.model.pages import MarkKind
from pdfeditor.services import stamping
from pdfeditor.services.assembly import (
    IMAGE_SUFFIXES,
    SplitMode,
    extract,
    merge,
    plan_split,
    subset_size,
    write_split,
)
from pdfeditor.services.pages import format_page_ranges, parse_page_ranges
from pdfeditor.services.stamping import (
    MARK_NAMES,
    MarkInfo,
    apply_background,
    apply_header_footer,
    apply_watermark,
    background_from_settings,
    header_footer_from_settings,
    watermark_from_settings,
)
from pdfeditor.ui import page_ops
from pdfeditor.ui.dialogs.pages import (
    BackgroundDialog,
    CombineDialog,
    CropDialog,
    ExtractDialog,
    HeaderFooterDialog,
    InsertPagesDialog,
    PageLabelsDialog,
    SplitDialog,
    WatermarkDialog,
    checked_pages,
)
from pdfeditor.ui.dialogs.password import password_prompt
from pdfeditor.ui.icons import icon

if TYPE_CHECKING:
    from pdfeditor.ui.document_tab import DocumentTab
    from pdfeditor.ui.main_window import MainWindow


class OrganizeController:
    def __init__(self, window: MainWindow) -> None:
        self.w = window

        def act(
            text: str,
            slot: Callable[[], object],
            icon_name: str,
            shortcut: str | None = None,
            needs_doc: bool = True,
        ) -> QAction:
            a = QAction(icon(icon_name), text, window)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(lambda _=False: self._guard(slot))
            a.setProperty("needs_doc", needs_doc)
            window.addAction(a)
            return a

        self.act_organize = act(
            "&Organize Pages", self.toggle_organizer, "layout-grid", "Ctrl+Shift+O"
        )
        self.act_organize.setCheckable(True)
        self.act_insert = act("&Insert Pages…", self.insert_pages, "file-plus", "Ctrl+Shift+I")
        self.act_delete = act("&Delete Pages", self.delete_pages, "file-x", "Ctrl+Shift+D")
        self.act_rotate_left = act(
            "Rotate Pages &Left", lambda: self.rotate(-90), "rotate-ccw", "Ctrl+Shift+Left"
        )
        self.act_rotate_right = act(
            "Rotate Pages &Right", lambda: self.rotate(90), "rotate-cw", "Ctrl+Shift+Right"
        )
        self.act_duplicate = act("D&uplicate Pages", self.duplicate, "copy")
        self.act_extract = act("E&xtract Pages…", self.extract_pages, "file-output")
        self.act_replace = act("Re&place Pages…", self.replace_pages, "replace")
        self.act_split = act("&Split Document…", self.split_document, "split")
        self.act_crop = act("&Crop Pages…", self.crop_pages, "crop", "Ctrl+Shift+T")
        self.act_labels = act("Page &Labels…", self.page_labels, "tag")
        self.act_header = act("&Header && Footer…", lambda: self.header_footer(False), "panel-top")
        self.act_bates = act("&Bates Numbering…", lambda: self.header_footer(True), "hash")
        self.act_watermark = act("&Watermark…", self.watermark, "droplet")
        self.act_background = act("Bac&kground…", self.background, "paint-bucket")
        # Each mark button opens its dialog (asking first whether to replace what's there); its
        # menu has Add / Update / Remove, also shown as a submenu of the Pages menu.
        marks: list[tuple[MarkKind, QAction, str, Callable[[bool], object]]] = [
            (
                MarkKind.HEADER_FOOTER,
                self.act_header,
                "panel-top",
                lambda update: self.header_footer(False, update=update),
            ),
            (
                MarkKind.BATES,
                self.act_bates,
                "hash",
                lambda update: self.header_footer(True, update=update),
            ),
            (
                MarkKind.WATERMARK,
                self.act_watermark,
                "droplet",
                lambda update: self.watermark(update=update),
            ),
            (
                MarkKind.BACKGROUND,
                self.act_background,
                "paint-bucket",
                lambda update: self.background(update=update),
            ),
        ]
        self.mark_actions: dict[MarkKind, tuple[QAction, QAction, QAction]] = {}
        for kind, main, icon_name, open_dialog in marks:
            label = MARK_NAMES[kind].replace("&", "&&")
            add = act(f"&Add {label}…", partial(open_dialog, False), icon_name)
            update = act(f"&Update {label}…", partial(open_dialog, True), "pencil")
            remove = act(f"&Remove {label}", partial(self.remove_marks, kind), "eraser")
            menu = QMenu(main.text(), window)
            menu.addActions([add, update, remove])
            main.setMenu(menu)
            self.mark_actions[kind] = (add, update, remove)
        self.act_combine = act(
            "&Combine Files into PDF…",
            self.combine,
            "combine",
            needs_doc=False,
        )

    # -- plumbing -------------------------------------------------------------------------
    def _guard(self, slot: Callable[[], object]) -> None:
        """Report refused operations instead of letting them escape as crashes."""
        try:
            slot()
        except (page_ops.PageOpError, EngineError, OSError, ValueError) as exc:
            QMessageBox.warning(self.w, "Organize Pages", str(exc))

    def tab(self) -> DocumentTab | None:
        return self.w.current_tab()

    def _pages(self) -> tuple[DocumentTab, list[int]] | None:
        tab = self.tab()
        return None if tab is None else (tab, tab.target_pages())

    def _after(self, tab: DocumentTab, select: list[int] | None = None) -> None:
        if tab.organizer is not None and select is not None:
            tab.organizer.select_pages([p for p in select if p < tab.view.page_count])
        self.w._update_ui()

    def ribbon(self) -> None:
        r = self.w.ribbon.add_tab("Organize")
        r.add_group(self.act_organize, title="Organize")
        r.add_group(
            self.act_insert,
            self.act_delete,
            self.act_duplicate,
            self.act_rotate_left,
            self.act_rotate_right,
            title="Pages",
        )
        r.add_group(
            self.act_extract, self.act_replace, self.act_split, self.act_combine, title="Assemble"
        )
        r.add_group(self.act_crop, self.act_labels, title="Page Setup")
        marks = r.add_group(
            self.act_header, self.act_bates, self.act_watermark, self.act_background, title="Marks"
        )
        for action in (self.act_header, self.act_bates, self.act_watermark, self.act_background):
            button = marks.bar.widgetForAction(action)
            if isinstance(button, QToolButton):  # click: the dialog; arrow: Add/Update/Remove
                button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)

    def menu_actions(self) -> list[QAction | None]:
        return [
            self.act_organize, None, self.act_insert, self.act_delete, self.act_duplicate,
            self.act_rotate_left, self.act_rotate_right, None, self.act_extract, self.act_replace,
            self.act_split, None, self.act_crop, self.act_labels, None, self.act_header,
            self.act_bates, self.act_watermark, self.act_background,
        ]  # fmt: skip

    def update_state(self) -> None:
        tab = self.tab()
        self.act_organize.setChecked(tab is not None and tab.organizing)

    # -- page commands --------------------------------------------------------------------
    def toggle_organizer(self) -> None:
        tab = self.tab()
        if tab is not None:
            tab.set_organizing(not tab.organizing)
            self.w._update_ui()

    def delete_pages(self) -> None:
        got = self._pages()
        if got is None:
            return
        tab, pages = got
        page_ops.delete_pages(tab.view.session, pages)
        self._after(tab, [min(pages)])

    def rotate(self, delta: int) -> None:
        got = self._pages()
        if got is not None:
            tab, pages = got
            page_ops.rotate_pages(tab.view.session, pages, delta)
            self._after(tab, pages)

    def duplicate(self) -> None:
        got = self._pages()
        if got is not None:
            tab, pages = got
            page_ops.duplicate_pages(tab.view.session, pages)
            start = max(pages) + 1
            self._after(tab, list(range(start, start + len(set(pages)))))

    def insert_pages(self, dialog: InsertPagesDialog | None = None) -> None:
        tab = self.tab()
        if tab is None:
            return
        view = tab.view
        dialog = dialog or InsertPagesDialog(view.page_count, tab.target_pages()[0], self.w)
        if not dialog.result() and not dialog.exec():
            return
        at = dialog.position()
        session = view.session
        if dialog.blank.isChecked():
            page_ops.insert_blank(session, at, like_page=min(at, view.page_count - 1))
            inserted = 1
        elif dialog.clipboard.isChecked():
            data = page_ops.clipboard_image_png()
            if data is None:
                raise page_ops.PageOpError("The clipboard doesn't contain an image.")
            page_ops.insert_image_bytes(session, at, data, "Insert Image from Clipboard")
            inserted = 1
        else:
            path = Path(dialog.path_edit.text())
            pages = None
            if dialog.range_edit.text().strip() and path.suffix.lower() not in IMAGE_SUFFIXES:
                with session.lock:
                    other = session.engine.open(path, password_prompt(self.w, path.name))
                    count = other.page_count
                    other.close()
                pages = parse_page_ranges(dialog.range_edit.text(), count)
            inserted = page_ops.insert_file(
                session, path, at, pages, password_prompt(self.w, path.name)
            )
        self._after(tab, list(range(at, at + inserted)))

    def extract_pages(
        self, dialog: ExtractDialog | None = None, folder: Path | None = None
    ) -> list[DocumentSession] | list[Path]:
        tab = self.tab()
        if tab is None:
            return []
        view = tab.view
        dialog = dialog or ExtractDialog(
            view.page_count, view.current_page, tab.target_pages(), self.w
        )
        if not dialog.result() and not dialog.exec():
            return []
        pages = checked_pages(self.w, dialog.range)
        if not pages:
            return []
        session = view.session
        results: list[DocumentSession] | list[Path]
        if dialog.separate.isChecked():
            folder = folder or self._choose_folder("Extract Pages To")
            if folder is None:
                return []
            written: list[Path] = []
            stem = Path(session.display_name).stem
            with session.lock:
                for n in pages:
                    part = extract(session.engine, session.document, [n])
                    try:
                        written.append(part.save(folder / f"{stem}_page{n + 1}.pdf"))
                    finally:
                        part.close()
            results = written
        else:
            with session.lock:
                part = extract(session.engine, session.document, pages)
            new = self.w.add_document(part, f"{Path(session.display_name).stem} (extract).pdf")
            results = [new.session]
        if dialog.delete_after.isChecked():
            page_ops.delete_pages(session, pages)
        return results

    def replace_pages(self, path: Path | None = None, source_range: str = "") -> None:
        got = self._pages()
        if got is None:
            return
        tab, pages = got
        if path is None:
            chosen, _ = QFileDialog.getOpenFileName(
                self.w, "Replace Pages With", "", "PDF documents (*.pdf)"
            )
            if not chosen:
                return
            path = Path(chosen)
        session = tab.view.session
        other = session.engine.open(path, password_prompt(self.w, path.name))
        count = other.page_count
        other.close()
        wanted = source_range or f"1-{min(count, len(pages))}"
        source_pages = parse_page_ranges(wanted, count)
        page_ops.replace_pages(
            session, pages, path, source_pages, password_prompt(self.w, path.name)
        )
        self._after(tab, list(range(min(pages), min(pages) + len(source_pages))))

    def _choose_folder(self, title: str) -> Path | None:
        chosen = QFileDialog.getExistingDirectory(self.w, title)
        return Path(chosen) if chosen else None

    def split_document(self, dialog: SplitDialog | None = None) -> list[Path]:
        tab = self.tab()
        if tab is None:
            return []
        session = tab.view.session
        target = session.save_target()
        folder = target.parent if target else Path.home()
        dialog = dialog or SplitDialog(Path(session.display_name).stem, folder, self.w)
        if not dialog.result() and not dialog.exec():
            return []
        mode = dialog.mode()
        engine = session.engine
        with session.lock:
            doc = session.document
            plan = plan_split(
                doc,
                mode,
                pages_per_file=dialog.pages.value(),
                ranges=dialog.range_groups(doc.page_count) if mode is SplitMode.RANGES else (),
                max_bytes=int(dialog.size_mb.value() * 1024 * 1024),
                size_of=lambda g: subset_size(engine, doc, g),
            )
            written = write_split(
                engine, doc, plan, Path(dialog.folder.text()), dialog.stem.text() or "part"
            )
        self.w.statusBar().showMessage(
            f"Split into {len(written)} files in {dialog.folder.text()}", 5000
        )
        return written

    def combine(self, dialog: CombineDialog | None = None) -> DocumentSession | None:
        dialog = dialog or CombineDialog(parent=self.w)
        if not dialog.result() and not dialog.exec():
            return None
        engine = self.w.engine()
        counts: dict[str, int] = {}
        passwords: dict[str, str | None] = {}
        try:
            for s in dialog.sources():
                if s.path.suffix.lower() in IMAGE_SUFFIXES:
                    continue
                used: list[str] = []
                prompt = password_prompt(self.w, s.path.name)

                def remember(
                    attempt: int,
                    prompt: Callable[[int], str | None] = prompt,
                    used: list[str] = used,
                ) -> str | None:
                    value = prompt(attempt)
                    if value is not None:
                        used.append(value)
                    return value

                probe = engine.open(s.path, remember)
                counts[str(s.path)] = probe.page_count
                probe.close()
                passwords[str(s.path)] = used[-1] if used else None
            sources = dialog.sources(counts)
            for s in sources:
                s.password = passwords.get(str(s.path))
            doc = merge(engine, sources, bookmarks=dialog.bookmarks.isChecked())
        except PasswordRequired:
            return None
        except (OpenError, Cancelled) as exc:
            raise page_ops.PageOpError(f"Couldn't combine the files: {exc}") from exc
        if doc.page_count == 0:
            doc.close()
            raise page_ops.PageOpError("Nothing to combine.")
        return self.w.add_document(doc, "Combined.pdf").session

    def crop_pages(self, dialog: CropDialog | None = None) -> None:
        tab = self.tab()
        if tab is None:
            return
        view = tab.view
        dialog = dialog or CropDialog(
            view.page_count, view.current_page, tab.target_pages(), parent=self.w
        )
        if not dialog.result() and not dialog.exec():
            return
        pages = checked_pages(self.w, dialog.range)
        if not pages:
            return
        if dialog.trim.isChecked():
            changed = page_ops.trim_white_margins(view.session, pages)
            self.w.statusBar().showMessage(f"Removed white margins on {changed} page(s).", 4000)
        else:
            page_ops.crop_pages(view.session, pages, dialog.margins())
        self._after(tab, pages)

    def page_labels(self, dialog: PageLabelsDialog | None = None) -> None:
        tab = self.tab()
        if tab is None:
            return
        session = tab.view.session
        with session.lock:
            rules = session.document.page_label_rules()
        dialog = dialog or PageLabelsDialog(rules, session.page_count, self.w)
        if not dialog.result() and not dialog.exec():
            return
        new = dialog.rules()
        if new != rules:
            session.execute(SetPageLabelsCommand(new))

    # -- marks: header & footer, Bates numbers, watermark, background ---------------------
    def find_marks(self, kind: MarkKind) -> MarkInfo | None:
        tab = self.tab()
        if tab is None:
            return None
        session = tab.view.session
        with session.lock:
            return stamping.find_marks(session.engine, session.document, [kind]).get(kind)

    def ask_replace(self, kind: MarkKind, info: MarkInfo) -> bool | None:
        """Ask what to do with the marks already there: True replaces them, False adds another
        set as well, None cancels."""
        box = QMessageBox(
            QMessageBox.Icon.Question,
            MARK_NAMES[kind],
            f"This document already has {_PHRASES[kind]} (pages "
            f"{format_page_ranges(info.pages)}).\n\n"
            "Replace it with the new one, or add the new one as well?",
            parent=self.w,
        )
        replace = box.addButton("&Replace Existing", QMessageBox.ButtonRole.AcceptRole)
        add = box.addButton("&Add New", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(replace)
        box.exec()
        clicked = box.clickedButton()
        return True if clicked is replace else False if clicked is add else None

    def _target(self, kind: MarkKind, update: bool) -> tuple[bool, MarkInfo | None]:
        """Whether to go on, and the existing marks to replace (None: add new ones)."""
        info = self.find_marks(kind)
        if info is None:
            if update:
                QMessageBox.information(
                    self.w,
                    f"Update {MARK_NAMES[kind]}",
                    f"This document has no {_bare(kind)} added by this app or Acrobat to update.",
                )
            return not update, None
        if update:
            return True, info
        choice = self.ask_replace(kind, info)
        return choice is not None, info if choice else None

    def _as_update(
        self,
        dialog: HeaderFooterDialog | WatermarkDialog | BackgroundDialog,
        kind: MarkKind,
        info: MarkInfo,
    ) -> None:
        title = f"Update {MARK_NAMES[kind]}"
        dialog.setWindowTitle(title)
        dialog.header_title.setText(title)
        if info.foreign:
            dialog.header_subtitle.setText(
                "Replaces the one another application added (its settings can't be read)."
            )
            dialog.header_subtitle.setVisible(True)
        dialog.range.select_pages(info.pages)

    def _run_marks(
        self, kind: MarkKind, existing: MarkInfo | None, draw: Callable[[Document], object]
    ) -> None:
        tab = self.tab()
        if tab is None:
            return
        session = tab.view.session
        engine = session.engine

        def op(doc: Document) -> None:
            if existing is not None:
                stamping.remove_marks(engine, doc, [kind])
            draw(doc)

        verb = "Update" if existing is not None else "Add"
        page_ops.run(session, f"{verb} {MARK_NAMES[kind]}", op)

    def header_footer_dialog(self, bates: bool, existing: MarkInfo | None) -> HeaderFooterDialog:
        tab = self.tab()
        assert tab is not None
        view = tab.view
        dialog = HeaderFooterDialog(
            view.page_count, view.current_page, tab.target_pages(), bates, self.w
        )
        if existing is not None:
            self._as_update(dialog, MarkKind.BATES if bates else MarkKind.HEADER_FOOTER, existing)
            spec = header_footer_from_settings(existing.settings)
            if spec is not None:
                dialog.load(spec)
        return dialog

    def header_footer(
        self, bates: bool, dialog: HeaderFooterDialog | None = None, update: bool = False
    ) -> None:
        tab = self.tab()
        if tab is None:
            return
        kind = MarkKind.BATES if bates else MarkKind.HEADER_FOOTER
        go, existing = self._target(kind, update)
        if not go:
            return
        dialog = dialog or self.header_footer_dialog(bates, existing)
        if not dialog.result() and not dialog.exec():
            return
        pages = checked_pages(self.w, dialog.range)
        spec = dialog.spec()
        if not pages or not spec.texts:
            return
        name = tab.view.session.display_name
        engine = tab.view.session.engine
        self._run_marks(
            kind, existing, lambda doc: apply_header_footer(engine, doc, spec, pages, name, kind)
        )

    def watermark_dialog(self, existing: MarkInfo | None) -> WatermarkDialog:
        tab = self.tab()
        assert tab is not None
        view = tab.view
        dialog = WatermarkDialog(view.page_count, view.current_page, tab.target_pages(), self.w)
        if existing is not None:
            self._as_update(dialog, MarkKind.WATERMARK, existing)
            spec = watermark_from_settings(existing.settings)
            if spec is not None:
                dialog.load(spec)
        return dialog

    def watermark(self, dialog: WatermarkDialog | None = None, update: bool = False) -> None:
        tab = self.tab()
        if tab is None:
            return
        go, existing = self._target(MarkKind.WATERMARK, update)
        if not go:
            return
        dialog = dialog or self.watermark_dialog(existing)
        if not dialog.result() and not dialog.exec():
            return
        pages = checked_pages(self.w, dialog.range)
        if not pages:
            return
        spec = dialog.spec()
        engine = tab.view.session.engine
        self._run_marks(
            MarkKind.WATERMARK, existing, lambda doc: apply_watermark(engine, doc, spec, pages)
        )

    def background_dialog(self, existing: MarkInfo | None) -> BackgroundDialog:
        tab = self.tab()
        assert tab is not None
        view = tab.view
        dialog = BackgroundDialog(view.page_count, view.current_page, tab.target_pages(), self.w)
        if existing is not None:
            self._as_update(dialog, MarkKind.BACKGROUND, existing)
            stored = background_from_settings(existing.settings)
            if stored is not None:
                dialog.load(*stored)
        return dialog

    def background(self, dialog: BackgroundDialog | None = None, update: bool = False) -> None:
        tab = self.tab()
        if tab is None:
            return
        go, existing = self._target(MarkKind.BACKGROUND, update)
        if not go:
            return
        dialog = dialog or self.background_dialog(existing)
        if not dialog.result() and not dialog.exec():
            return
        pages = checked_pages(self.w, dialog.range)
        if not pages:
            return
        color, opacity = dialog.color_button.color, dialog.opacity.value() / 100
        engine = tab.view.session.engine
        self._run_marks(
            MarkKind.BACKGROUND,
            existing,
            lambda doc: apply_background(doc, color, pages, opacity, engine),
        )

    def remove_marks(self, kind: MarkKind) -> int:
        """Remove every mark of ``kind`` (one undoable step); returns how many were removed."""
        tab = self.tab()
        if tab is None:
            return 0
        info = self.find_marks(kind)
        if info is None:
            QMessageBox.information(
                self.w,
                f"Remove {MARK_NAMES[kind]}",
                f"This document has no {_bare(kind)} added by this app or Acrobat to remove.",
            )
            return 0
        session = tab.view.session
        engine = session.engine
        removed: list[int] = []
        page_ops.run(
            session,
            f"Remove {MARK_NAMES[kind]}",
            lambda doc: removed.append(stamping.remove_marks(engine, doc, [kind])),
        )
        self.w.statusBar().showMessage(
            f"Removed {_bare(kind)} from {len(info.pages)} page(s).", 4000
        )
        return sum(removed)


_PHRASES = {
    MarkKind.HEADER_FOOTER: "a header or footer",
    MarkKind.BATES: "Bates numbers",
    MarkKind.WATERMARK: "a watermark",
    MarkKind.BACKGROUND: "a background",
}


def _bare(kind: MarkKind) -> str:
    """The phrase without its article: "watermark", "Bates numbers"."""
    return _PHRASES[kind].removeprefix("a ")
