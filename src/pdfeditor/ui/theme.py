"""Light/dark application theme built from the design tokens in ``ui/style``.

Fusion draws the widgets; ``build_palette`` and ``stylesheet`` give them the app's colors,
spacing and corner radii. The accent follows the Windows accent unless the user picks one.
Qt's color scheme is still set so native parts (title bar, file dialogs) match.

With Windows High Contrast on, the tokens step aside: no style sheet, and the system palette,
so the user's contrast colors win. Widgets that paint themselves read ``current_colors()``,
which then maps every token to a role of that palette.
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from pdfeditor.ui.style.palette import build_palette, colors_from_palette
from pdfeditor.ui.style.qss import DEFAULT_FONT_PT, stylesheet
from pdfeditor.ui.style.tokens import Colors, Scheme, build_colors

# Windows 11's UI font, tuned for text sizes; older systems keep Qt's default (Segoe UI).
PREFERRED_FONT = "Segoe UI Variable Text"


class Theme(Enum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


class ThemeManager(QObject):
    """Applies the theme and keeps it applied when the system scheme or contrast changes."""

    changed = Signal()

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self._app = app
        self.theme = Theme.SYSTEM
        self.accent = ""  # "" = follow the system accent
        # Read before we install our own palette: this is the platform's (Windows accent).
        self.system_accent = app.palette().color(QPalette.ColorRole.Accent).name()
        self.colors: Colors = build_colors("light", self.system_accent)
        self._applying = False
        self._applied: tuple[Colors, bool] | None = None
        # Keep the wrappers: PySide can invalidate a temporary QAccessibilityHints wrapper
        # once its QStyleHints wrapper is collected.
        self._hints = QGuiApplication.styleHints()
        self._accessibility_hints = self._hints.accessibility()
        self._hints.colorSchemeChanged.connect(self._reapply)
        self._accessibility_hints.contrastPreferenceChanged.connect(self._reapply)
        app.setStyle("Fusion")
        _use_preferred_font(app)

    @property
    def scheme(self) -> Scheme:
        return "dark" if self.colors.scheme == "dark" else "light"

    def high_contrast(self) -> bool:
        """Is the system asking for high contrast (Windows High Contrast)?"""
        preference = self._accessibility_hints.contrastPreference()
        return preference == Qt.ContrastPreference.HighContrast

    @property
    def high_contrast_applied(self) -> bool:
        """The applied theme is the High Contrast one (no style sheet, system palette)."""
        return self._applied[1] if self._applied is not None else self.high_contrast()

    def apply(self, theme: Theme, accent: str = "") -> None:
        self.theme, self.accent = theme, accent
        self._reapply()

    def _reapply(self) -> None:
        if self._applying:  # setColorScheme below re-emits colorSchemeChanged
            return
        self._applying = True
        try:
            hints = self._hints
            scheme: Scheme
            if self.theme is Theme.SYSTEM:
                hints.unsetColorScheme()
                scheme = "dark" if hints.colorScheme() == Qt.ColorScheme.Dark else "light"
            else:
                # The user's choice wins even where the platform ignores the request (no
                # platform theme, offscreen), which only affects the native frame then.
                scheme = "dark" if self.theme is Theme.DARK else "light"
                hints.setColorScheme(
                    Qt.ColorScheme.Dark if scheme == "dark" else Qt.ColorScheme.Light
                )
            self.colors = build_colors(scheme, self.accent or self.system_accent)
            # Setting the app style sheet repolishes every widget in the process, so skip it
            # when nothing changed (each new main window applies the saved theme again).
            high_contrast = self.high_contrast()
            sheet = "" if high_contrast else stylesheet(self.colors, base_pt=self._font_pt())
            if high_contrast:
                # Qt skips a palette equal to the current one, so this is cheap when repeated.
                self._app.setPalette(system_palette())
                self.colors = colors_from_palette(self._app.palette())
            applied = (self.colors, high_contrast)
            if applied == self._applied and self._app.styleSheet() == sheet:
                return
            self._applied = applied
            if not high_contrast:
                self._app.setPalette(build_palette(self.colors))
            self._app.setStyleSheet(sheet)
        finally:
            self._applying = False
        self.changed.emit()

    def _font_pt(self) -> float:
        size = self._app.font().pointSizeF()
        return size if size > 0 else DEFAULT_FONT_PT


def system_palette() -> QPalette:
    """The palette to use with High Contrast: an empty one, which resolves to the system's
    (tests substitute a contrast palette here)."""
    return QPalette()


def _use_preferred_font(app: QApplication) -> None:
    if PREFERRED_FONT in QFontDatabase.families():
        font = QFont(app.font())
        font.setFamily(PREFERRED_FONT)
        app.setFont(font)


def theme_manager(app: QApplication | None = None) -> ThemeManager:
    """The application's theme manager, created on first use."""
    app = app or QApplication.instance()  # type: ignore[assignment]
    assert isinstance(app, QApplication)
    manager = app.findChild(ThemeManager)
    return manager if isinstance(manager, ThemeManager) else ThemeManager(app)


def apply_theme(app: QApplication, theme: Theme, accent: str = "") -> None:
    theme_manager(app).apply(theme, accent)


def current_colors() -> Colors:
    """Tokens of the active theme, for widgets that paint their own colors."""
    return theme_manager().colors
