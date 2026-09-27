"""Typed access to user preferences stored in QSettings."""

from __future__ import annotations

import getpass

from PySide6.QtCore import QSettings

DEFAULT_ZOOMS = ("fit_width", "fit_page", "100")


def _default_author() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


class AppSettings:
    """Preferences with defaults. Reads go straight to QSettings, so changes apply at once."""

    def __init__(self, settings: QSettings | None = None) -> None:
        self.qs = settings or QSettings()

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.qs.value(key, default))  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return default

    def _str(self, key: str, default: str) -> str:
        value = self.qs.value(key, default)
        return value if isinstance(value, str) else default

    # comments & identity
    @property
    def author(self) -> str:
        return self._str("prefs/author", _default_author())

    @author.setter
    def author(self, value: str) -> None:
        self.qs.setValue("prefs/author", value)

    # documents
    @property
    def autosave_minutes(self) -> int:
        """0 disables crash-recovery copies."""
        return max(0, self._int("prefs/autosave_minutes", 2))

    @autosave_minutes.setter
    def autosave_minutes(self, value: int) -> None:
        self.qs.setValue("prefs/autosave_minutes", int(value))

    @property
    def undo_disk_mb(self) -> int:
        return max(64, self._int("prefs/undo_disk_mb", 2048))

    @undo_disk_mb.setter
    def undo_disk_mb(self, value: int) -> None:
        self.qs.setValue("prefs/undo_disk_mb", int(value))

    # viewing
    @property
    def default_zoom(self) -> str:
        value = self._str("prefs/default_zoom", "fit_width")
        return value if value in DEFAULT_ZOOMS else "fit_width"

    @default_zoom.setter
    def default_zoom(self, value: str) -> None:
        self.qs.setValue("prefs/default_zoom", value)

    @property
    def default_tool(self) -> str:
        value = self._str("prefs/default_tool", "select")
        return value if value in ("select", "hand") else "select"

    @default_tool.setter
    def default_tool(self, value: str) -> None:
        self.qs.setValue("prefs/default_tool", value)

    @property
    def keep_tools(self) -> bool:
        """Keep creation tools active after use (off: back to Select after one use)."""
        return self._str("prefs/keep_tools", "false") == "true"

    @keep_tools.setter
    def keep_tools(self, value: bool) -> None:
        self.qs.setValue("prefs/keep_tools", "true" if value else "false")

    @property
    def cache_mb(self) -> int:
        """Render cache budget; applies on next start."""
        return min(4096, max(64, self._int("prefs/cache_mb", 384)))

    @cache_mb.setter
    def cache_mb(self, value: int) -> None:
        self.qs.setValue("prefs/cache_mb", int(value))

    # comments
    @property
    def custom_stamps(self) -> list[str]:
        """Custom stamp keys (file names in the stamp library), in menu order."""
        value = self.qs.value("stamps/custom", [])
        if isinstance(value, str):  # QSettings returns a one-item list as a plain string
            return [value] if value else []
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    @custom_stamps.setter
    def custom_stamps(self, value: list[str]) -> None:
        self.qs.setValue("stamps/custom", list(value))
