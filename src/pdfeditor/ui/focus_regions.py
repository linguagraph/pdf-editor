"""F6 / Shift+F6: move the keyboard between the main window's regions.

The regions are, in order: the ribbon (with the Search tools box), the left side panels, the
page area, the right side panels, the status bar (the job chip) and the notifications, when
any are shown. Regions with nothing to focus (a hidden dock, an empty status bar) are skipped.
Coming back to a region puts the keyboard where it was when it left, if that widget is still
there; otherwise each region picks its natural first stop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget


@dataclass
class Region:
    """``containers`` are the widgets the region is made of (called each time, since toasts
    come and go); ``entry`` is where the keyboard lands on arrival (None: skip the region)."""

    name: str
    containers: Callable[[], list[QWidget]]
    entry: Callable[[], QWidget | None]
    last: QWidget | None = field(default=None, repr=False)

    def contains(self, widget: QWidget) -> bool:
        return any(c is widget or c.isAncestorOf(widget) for c in self.containers())


def focusable(widget: QWidget | None) -> bool:
    """Can take the keyboard right now: shown, enabled and accepting focus."""
    return (
        widget is not None
        and widget.isVisible()
        and widget.isEnabled()
        and widget.focusPolicy() != Qt.FocusPolicy.NoFocus
    )


def first_focusable(root: QWidget) -> QWidget | None:
    """The first widget inside ``root``, in Tab order, that can take the keyboard."""
    if focusable(root) and root.focusProxy() is None:
        return root
    widget = root.nextInFocusChain()
    seen: set[int] = set()
    while widget is not None and widget is not root and id(widget) not in seen:
        seen.add(id(widget))
        if root.isAncestorOf(widget) and focusable(widget) and widget.focusProxy() is None:
            return widget
        widget = widget.nextInFocusChain()
    return None


class FocusRegions:
    def __init__(self, regions: list[Region]) -> None:
        self.regions = regions

    def current(self, widget: QWidget | None = None) -> Region | None:
        """The region holding ``widget`` (default: the focus widget)."""
        widget = widget if widget is not None else QApplication.focusWidget()
        if widget is None:
            return None
        return next((r for r in self.regions if r.contains(widget)), None)

    def names(self) -> list[str]:
        """The regions F6 would visit now, in order (tests, screen-reader hints)."""
        return [r.name for r in self.regions if self._target(r) is not None]

    def cycle(self, step: int = 1) -> Region | None:
        """Focus the next (``step`` 1) or previous (-1) region that can take the keyboard."""
        focus = QApplication.focusWidget()
        here = self.current(focus)
        if here is not None and focus is not None:
            here.last = focus
        count = len(self.regions)
        start = self.regions.index(here) if here is not None else (-1 if step > 0 else count)
        for k in range(1, count + 1):
            region = self.regions[(start + k * step) % count]
            if region is here:
                continue
            target = self._target(region)
            if target is not None:
                target.setFocus(Qt.FocusReason.OtherFocusReason)
                return region
        return None

    def _target(self, region: Region) -> QWidget | None:
        last = region.last
        if last is not None:
            try:
                if focusable(last) and region.contains(last):
                    return last
            except RuntimeError:  # deleted with its toast or panel
                pass
            region.last = None
        target = region.entry()
        return target if focusable(target) else None
