"""Undoable annotation edits.

Annotations are tracked by their /NM ``name``, which stays the same when an undo re-creates a
deleted annotation (the engine object id may change).
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Sequence

from pdfeditor.core.commands.base import Change, ChangeKind, Command
from pdfeditor.engine.base import Document
from pdfeditor.model.annotations import AnnotationModel


def _changes(pages: Sequence[int]) -> list[Change]:
    return [Change(ChangeKind.ANNOTATIONS, frozenset(pages))]


def _ensure_name(model: AnnotationModel) -> AnnotationModel:
    model = copy.deepcopy(model)
    if not model.name:
        model.name = f"pdfeditor-{uuid.uuid4().hex}"
    return model


def _by_name(doc: Document, page: int, name: str) -> AnnotationModel | None:
    return next((a for a in doc.page(page).annotations() if a.name == name), None)


class AddAnnotationCommand(Command):
    def __init__(self, model: AnnotationModel, label: str | None = None) -> None:
        self.model = _ensure_name(model)
        self.label = label or f"Add {model.type.value} Comment"
        self.stored: AnnotationModel | None = None

    def do(self, doc: Document) -> None:
        model = copy.deepcopy(self.model)
        if model.in_reply_to is None and self.model.extra.get("irt_name"):
            parent = _by_name(doc, model.page_index, self.model.extra["irt_name"])
            model.in_reply_to = parent.id if parent else None
        self.stored = doc.page(model.page_index).add_annotation(model)

    def undo(self, doc: Document) -> None:
        doc.page(self.model.page_index).delete_annotation(None, self.model.name)

    def changes(self) -> list[Change]:
        return _changes([self.model.page_index])


class UpdateAnnotationCommand(Command):
    """Change geometry/properties. Consecutive edits of the same annotation with the same
    ``merge_key`` (e.g. dragging a slider) collapse into one undo step."""

    def __init__(
        self,
        before: AnnotationModel,
        after: AnnotationModel,
        label: str = "Edit Comment",
        merge_key: str = "",
    ) -> None:
        self.before = copy.deepcopy(before)
        self.after = copy.deepcopy(after)
        self.label = label
        self.merge_key = merge_key

    def do(self, doc: Document) -> None:
        doc.page(self.after.page_index).update_annotation(self.after)

    def undo(self, doc: Document) -> None:
        doc.page(self.before.page_index).update_annotation(self.before)

    def merge_with(self, other: Command) -> bool:
        if (
            isinstance(other, UpdateAnnotationCommand)
            and self.merge_key
            and other.merge_key == self.merge_key
            and other.after.name == self.after.name
        ):
            self.after = other.after
            return True
        return False

    def changes(self) -> list[Change]:
        return _changes([self.before.page_index])


class DeleteAnnotationsCommand(Command):
    """Delete annotations (and their replies); undo re-creates them in the original z-order."""

    def __init__(
        self, page_index: int, names: Sequence[str], label: str = "Delete Comment"
    ) -> None:
        self.page_index = page_index
        self.names = list(names)
        self.label = label
        self._saved: list[AnnotationModel] = []  # parents before replies
        self._order: list[str] = []

    def do(self, doc: Document) -> None:
        page = doc.page(self.page_index)
        existing = page.annotations()
        by_id = {a.id: a for a in existing}
        chosen = {a.name for a in existing if a.name in self.names}
        # include replies, transitively
        changed = True
        while changed:
            changed = False
            for a in existing:
                parent = by_id.get(a.in_reply_to) if a.in_reply_to is not None else None
                if a.name not in chosen and parent is not None and parent.name in chosen:
                    chosen.add(a.name)
                    changed = True
        id_to_name = {a.id: a.name for a in existing}
        self._order = [id_to_name.get(i, "") for i in page.annotation_order()]
        saved = []
        for a in existing:
            if a.name in chosen:
                model = copy.deepcopy(a)
                if model.in_reply_to is not None:
                    model.extra = {**model.extra, "irt_name": id_to_name.get(model.in_reply_to, "")}
                saved.append(model)
        # parents first so replies can point at the re-created parent
        self._saved = sorted(saved, key=lambda m: m.in_reply_to is not None)
        for model in reversed(self._saved):
            page.delete_annotation(model.id, model.name)

    def undo(self, doc: Document) -> None:
        page = doc.page(self.page_index)
        for model in self._saved:
            model = copy.deepcopy(model)
            irt = model.extra.get("irt_name")
            if irt:
                parent = _by_name(doc, self.page_index, irt)
                model.in_reply_to = parent.id if parent else None
            page.add_annotation(model)
        _restore_order(doc, self.page_index, self._order)

    def changes(self) -> list[Change]:
        return _changes([self.page_index])


def _restore_order(doc: Document, page_index: int, names: list[str]) -> None:
    page = doc.page(page_index)
    current = page.annotations()
    name_to_id = {a.name: a.id for a in current if a.id is not None}
    ordered = [name_to_id[n] for n in names if n in name_to_id]
    rest = [i for i in page.annotation_order() if i not in ordered]
    target = ordered + rest
    if target != page.annotation_order():
        page.set_annotation_order(target)


class ReorderAnnotationCommand(Command):
    """Bring to front / send to back."""

    def __init__(self, page_index: int, name: str, to_front: bool) -> None:
        self.page_index = page_index
        self.name = name
        self.to_front = to_front
        self.label = "Bring to Front" if to_front else "Send to Back"
        self._before: list[str] = []

    def _names(self, doc: Document) -> list[str]:
        page = doc.page(self.page_index)
        id_to_name = {a.id: a.name for a in page.annotations()}
        return [id_to_name.get(i, "") for i in page.annotation_order()]

    def do(self, doc: Document) -> None:
        self._before = self._names(doc)
        order = [n for n in self._before if n != self.name]
        order = [*order, self.name] if self.to_front else [self.name, *order]
        _restore_order(doc, self.page_index, order)

    def undo(self, doc: Document) -> None:
        _restore_order(doc, self.page_index, self._before)

    def changes(self) -> list[Change]:
        return _changes([self.page_index])
