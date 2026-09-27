"""Exactly reversible document-level commands."""

from __future__ import annotations

import copy
from collections.abc import Sequence

from pdfeditor.core.commands.base import Change, ChangeKind, Command
from pdfeditor.core.xmp import sync_xmp
from pdfeditor.engine.base import Document
from pdfeditor.model.metadata import Metadata, SecuritySettings
from pdfeditor.model.outline import OutlineItem
from pdfeditor.model.pages import PageLabelRule
from pdfeditor.model.structure import AccessibilitySettings


class SetMetadataCommand(Command):
    label = "Change Document Properties"

    def __init__(self, new: Metadata) -> None:
        self.new = copy.copy(new)
        self.old: Metadata | None = None
        self.old_xmp: str | None = None

    def do(self, doc: Document) -> None:
        if self.old is None:
            self.old = doc.metadata()
            self.old_xmp = doc.xmp()
        doc.set_metadata(self.new)
        # keep XMP in step with the Info dictionary (PDF 2.0 / PDF/A readers use XMP)
        synced = sync_xmp(self.old_xmp or "", self.new)
        if synced != (self.old_xmp or ""):
            doc.set_xmp(synced)

    def undo(self, doc: Document) -> None:
        assert self.old is not None
        doc.set_metadata(self.old)
        if self.old_xmp is not None and doc.xmp() != self.old_xmp:
            doc.set_xmp(self.old_xmp)

    def merge_with(self, other: Command) -> bool:
        if isinstance(other, SetMetadataCommand):
            self.new = other.new  # keep our original "old"
            return True
        return False

    def changes(self) -> list[Change]:
        return [Change(ChangeKind.METADATA)]


class SetOutlineCommand(Command):
    label = "Edit Bookmarks"

    def __init__(self, items: Sequence[OutlineItem], label: str | None = None) -> None:
        self.new = copy.deepcopy(list(items))
        self.old: list[OutlineItem] | None = None
        if label:
            self.label = label

    def do(self, doc: Document) -> None:
        if self.old is None:
            self.old = doc.outline()
        doc.set_outline(self.new)

    def undo(self, doc: Document) -> None:
        assert self.old is not None
        doc.set_outline(self.old)

    def changes(self) -> list[Change]:
        return [Change(ChangeKind.OUTLINE)]


class SetPageLabelsCommand(Command):
    label = "Edit Page Labels"

    def __init__(self, rules: Sequence[PageLabelRule]) -> None:
        self.new = list(rules)
        self.old: list[PageLabelRule] | None = None

    def do(self, doc: Document) -> None:
        if self.old is None:
            self.old = doc.page_label_rules()
        doc.set_page_label_rules(self.new)

    def undo(self, doc: Document) -> None:
        assert self.old is not None
        doc.set_page_label_rules(self.old)

    def changes(self) -> list[Change]:
        return [Change(ChangeKind.STRUCTURE)]  # labels show in thumbnails and the navigator


class SetSecurityCommand(Command):
    """Password protection (or its removal) to apply on the next save."""

    def __init__(self, settings: SecuritySettings) -> None:
        self.new = settings
        self.old: SecuritySettings | None = None
        self._did = False
        self.label = (
            "Remove Security" if settings.method.value == "none" else "Encrypt with Password"
        )

    def do(self, doc: Document) -> None:
        if not self._did:
            self.old = doc.pending_security()
            self._did = True
        doc.set_pending_security(self.new)

    def undo(self, doc: Document) -> None:
        doc.set_pending_security(self.old)

    def changes(self) -> list[Change]:
        return [Change(ChangeKind.METADATA)]


class SetStructElementCommand(Command):
    """Change a tag's type or alternate text."""

    def __init__(
        self, ref: int, old_type: str, old_alt: str, type: str | None, alt: str | None
    ) -> None:
        self.ref = ref
        self.old = (old_type, old_alt)
        self.new = (type, alt)
        self.label = "Change Alternate Text" if type is None else "Change Tag"

    def do(self, doc: Document) -> None:
        doc.set_struct_element(self.ref, *self.new)

    def undo(self, doc: Document) -> None:
        new_type, new_alt = self.new
        doc.set_struct_element(
            self.ref,
            self.old[0] if new_type is not None else None,
            self.old[1] if new_alt is not None else None,
        )

    def changes(self) -> list[Change]:
        return [Change(ChangeKind.METADATA)]


class ReorderStructCommand(Command):
    label = "Reorder Tags"

    def __init__(self, parent: int | None, old: Sequence[int], new: Sequence[int]) -> None:
        self.parent, self.old, self.new = parent, list(old), list(new)

    def do(self, doc: Document) -> None:
        doc.reorder_struct_children(self.parent, self.new)

    def undo(self, doc: Document) -> None:
        doc.reorder_struct_children(self.parent, self.old)

    def changes(self) -> list[Change]:
        return [Change(ChangeKind.METADATA)]


class SetAccessibilityCommand(Command):
    label = "Change Accessibility Settings"

    def __init__(self, new: AccessibilitySettings) -> None:
        self.new = new
        self.old: AccessibilitySettings | None = None

    def do(self, doc: Document) -> None:
        if self.old is None:
            self.old = doc.accessibility_settings()
        doc.set_accessibility_settings(self.new)

    def undo(self, doc: Document) -> None:
        assert self.old is not None
        doc.set_accessibility_settings(self.old)

    def changes(self) -> list[Change]:
        return [Change(ChangeKind.METADATA)]
