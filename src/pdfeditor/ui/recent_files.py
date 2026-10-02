"""The recent-files list (with pins), shared by File ▸ Open Recent and the start screen."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal

MAX_RECENT = 10
SETTINGS_RECENT = "recent_files"
SETTINGS_PINNED = "recent_pinned"


def _paths(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


class RecentFiles(QObject):
    """Most recent first, in QSettings. Pinned files stay listed (first) whatever the age.

    ``recent_files`` keeps its old format (a plain list of paths) so older settings still load;
    pins are a separate list of paths that are also in ``recent_files``.
    """

    changed = Signal()

    def __init__(self, settings: QSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings

    def files(self) -> list[str]:
        """All entries: pinned first (in pin order), then the rest, newest first."""
        recent = _paths(self.settings.value(SETTINGS_RECENT, []))
        pinned = [p for p in self.pinned() if p in recent]
        return pinned + [p for p in recent if p not in pinned]

    def pinned(self) -> list[str]:
        return _paths(self.settings.value(SETTINGS_PINNED, []))

    def is_pinned(self, path: str | Path) -> bool:
        return any(Path(p) == Path(path) for p in self.pinned())

    def add(self, path: Path) -> None:
        recent = [p for p in _paths(self.settings.value(SETTINGS_RECENT, [])) if Path(p) != path]
        recent.insert(0, str(path))
        # MAX_RECENT counts unpinned files only: pinning must not push anything out.
        pinned = self.pinned()
        kept, unpinned = [], 0
        for p in recent:
            if p in pinned:
                kept.append(p)
            elif unpinned < MAX_RECENT:
                kept.append(p)
                unpinned += 1
        self.settings.setValue(SETTINGS_RECENT, kept)
        self.changed.emit()

    def forget(self, path: str | Path) -> None:
        recent = _paths(self.settings.value(SETTINGS_RECENT, []))
        self.settings.setValue(SETTINGS_RECENT, [p for p in recent if Path(p) != Path(path)])
        self.settings.setValue(SETTINGS_PINNED, [p for p in self.pinned() if Path(p) != Path(path)])
        self.changed.emit()

    def set_pinned(self, path: str | Path, pinned: bool) -> None:
        pins = [p for p in self.pinned() if Path(p) != Path(path)]
        if pinned:
            match = next((p for p in self.files() if Path(p) == Path(path)), str(path))
            pins.append(match)
        self.settings.setValue(SETTINGS_PINNED, pins)
        self.changed.emit()

    def clear(self) -> None:
        """Forget every file that isn't pinned."""
        pins = self.pinned()
        recent = _paths(self.settings.value(SETTINGS_RECENT, []))
        self.settings.setValue(SETTINGS_RECENT, [p for p in recent if p in pins])
        self.changed.emit()
