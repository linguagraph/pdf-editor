"""Typed access to user preferences stored in QSettings."""

from __future__ import annotations

import getpass

from PySide6.QtCore import QSettings

from pdfeditor.ui.style.tokens import is_color

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
    def language(self) -> str:
        """UI language code ("" = follow the system)."""
        return self._str("language", "")

    @language.setter
    def language(self, value: str) -> None:
        self.qs.setValue("language", value)

    # appearance
    @property
    def theme(self) -> str:
        """One of system, light or dark (``Theme`` values)."""
        value = self._str("theme", "system")
        return value if value in ("system", "light", "dark") else "system"

    @theme.setter
    def theme(self, value: str) -> None:
        self.qs.setValue("theme", value)

    @property
    def accent(self) -> str:
        """Accent color as #rrggbb ("" = follow the Windows accent)."""
        value = self._str("prefs/accent", "")
        return value if is_color(value) else ""

    @accent.setter
    def accent(self, value: str) -> None:
        self.qs.setValue("prefs/accent", value)

    def _bool(self, key: str, default: bool) -> bool:
        value = self.qs.value(key, default)
        return value if isinstance(value, bool) else str(value).lower() == "true"

    @property
    def show_menu_bar(self) -> bool:
        """Classic menu bar always visible (off: behind the ☰ button and the Alt key)."""
        return self._bool("prefs/show_menu_bar", False)

    @show_menu_bar.setter
    def show_menu_bar(self, value: bool) -> None:
        self.qs.setValue("prefs/show_menu_bar", "true" if value else "false")

    @property
    def ribbon_compact(self) -> bool:
        return self._bool("ribbon/compact", False)

    @ribbon_compact.setter
    def ribbon_compact(self, value: bool) -> None:
        self.qs.setValue("ribbon/compact", "true" if value else "false")

    @property
    def ribbon_collapsed(self) -> bool:
        return self._bool("ribbon/collapsed", False)

    @ribbon_collapsed.setter
    def ribbon_collapsed(self, value: bool) -> None:
        self.qs.setValue("ribbon/collapsed", "true" if value else "false")

    # side panels: which panel each rail has open ("" = collapsed) and its width
    def side_panel(self, side: str, default: str) -> str:
        return self._str(f"panels/{side}_open", default)

    def set_side_panel(self, side: str, key: str) -> None:
        self.qs.setValue(f"panels/{side}_open", key)

    def side_panel_width(self, side: str, default: int) -> int:
        return min(1200, max(120, self._int(f"panels/{side}_width", default)))

    def set_side_panel_width(self, side: str, width: int) -> None:
        self.qs.setValue(f"panels/{side}_width", int(width))

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
