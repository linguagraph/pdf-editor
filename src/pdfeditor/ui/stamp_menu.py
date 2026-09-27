"""The Stamp tool's menu: standard stamps, the user's custom image stamps, and managing them."""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Callable
from pathlib import Path

from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox, QWidget

from pdfeditor.model.annotations import STANDARD_STAMPS
from pdfeditor.services.custom_stamps import StampImageError, StampLibrary
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.tools.annotate import IMAGE_PREFIX

log = logging.getLogger(__name__)


def ask_stamp_image(parent: QWidget) -> Path | None:
    """File prompt (module-level so tests can replace it)."""
    chosen, _ = QFileDialog.getOpenFileName(
        parent, "Add Custom Stamp", "", "Images (*.png *.jpg *.jpeg)"
    )
    return Path(chosen) if chosen else None


def stamp_label(name: str) -> str:
    """Menu text for a standard stamp name (``NotApproved`` -> ``Not Approved``)."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


class StampMenu(QMenu):
    """Rebuilt each time it opens, so stamps added or removed elsewhere show up."""

    def __init__(
        self,
        parent: QWidget,
        on_choose: Callable[[str], None],
        prefs: AppSettings,
        stamp_library: StampLibrary | None = None,
    ) -> None:
        super().__init__("Stamp", parent)
        self.on_choose = on_choose
        self.prefs = prefs
        self.stamp_library = stamp_library or StampLibrary()
        self.remove_menu = QMenu("Remove Custom Stamp", self)
        self.aboutToShow.connect(self.rebuild_items)
        self.rebuild_items()

    def rebuild_items(self) -> None:
        self.clear()
        for name in STANDARD_STAMPS:
            self.addAction(stamp_label(name), functools.partial(self.on_choose, name))
        self.addSeparator()
        custom = self.stamp_library.stamps(self.prefs.custom_stamps)
        for stamp in custom:
            act = self.addAction(
                stamp.label, functools.partial(self.on_choose, IMAGE_PREFIX + stamp.key)
            )
            pix = QPixmap(str(stamp.path))
            if not pix.isNull():
                act.setIcon(QIcon(pix))
        self.addAction("Add Custom Stamp…", self.add_custom)
        self.remove_menu.clear()
        self.remove_menu.setEnabled(bool(custom))
        self.addMenu(self.remove_menu)
        for stamp in custom:
            self.remove_menu.addAction(
                stamp.label, functools.partial(self.remove_custom, stamp.key)
            )

    def add_custom(self) -> str | None:
        """Copy an image into the library, remember it and start stamping with it."""
        path = ask_stamp_image(self.parentWidget() or self)
        if path is None:
            return None
        try:
            stamp = self.stamp_library.add(path)
        except (StampImageError, OSError) as exc:
            log.warning("can't add custom stamp %s: %s", path, exc)
            QMessageBox.warning(self.parentWidget() or self, "Add Custom Stamp", str(exc))
            return None
        # The menu is rebuilt when it next opens (not here: this runs from one of its actions).
        self.prefs.custom_stamps = [*self.prefs.custom_stamps, stamp.key]
        self.on_choose(IMAGE_PREFIX + stamp.key)
        return stamp.key

    def remove_custom(self, key: str) -> None:
        """Forget a custom stamp. Stamps already placed in documents are not affected."""
        self.stamp_library.remove(key)
        self.prefs.custom_stamps = [k for k in self.prefs.custom_stamps if k != key]
