"""Light/dark application theme built from the design tokens in ``ui/style``.

Fusion draws the widgets; ``build_palette`` and ``stylesheet`` give them the app's colors,
spacing and corner radii. The accent follows the Windows accent unless the user picks one.
Qt's color scheme is still set so native parts (title bar, file dialogs) match.

With Windows High Contrast on, the tokens step aside: no style sheet, and the system palette,
so the user's contrast colors win.
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from pdfeditor.ui.style.palette import build_palette
from pdfeditor.ui.style.qss import stylesheet
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
        hints = QGuiApplication.styleHints()
        hints.colorSchemeChanged.connect(self._reapply)
        hints.accessibility().contrastPreferenceChanged.connect(self._reapply)
        app.setStyle("Fusion")
        _use_preferred_font(app)

    @property
    def scheme(self) -> Scheme:
        return "dark" if self.colors.scheme == "dark" else "light"

    def high_contrast(self) -> bool:
        hints = QGuiApplication.styleHints().accessibility()
        return hints.contrastPreference() == Qt.ContrastPreference.HighContrast

    def apply(self, theme: Theme, accent: str = "") -> None:
        self.theme, self.accent = theme, accent
        self._reapply()

    def _reapply(self) -> None:
        if self._applying:  # setColorScheme below re-emits colorSchemeChanged
            return
        self._applying = True
        try:
            hints = QGuiApplication.styleHints()
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
            sheet = "" if high_contrast else stylesheet(self.colors)
            applied = (self.colors, high_contrast)
            if applied == self._applied and self._app.styleSheet() == sheet:
                return
            self._applied = applied
            if high_contrast:
                self._app.setPalette(QPalette())  # empty resolve mask: the system palette
            else:
                self._app.setPalette(build_palette(self.colors))
            self._app.setStyleSheet(sheet)
        finally:
            self._applying = False
        self.changed.emit()


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
