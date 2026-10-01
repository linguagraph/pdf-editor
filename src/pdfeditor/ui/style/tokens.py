"""Design tokens: the colors and metrics every styled widget is built from.

Pure Python (no Qt), so the contrast rules can be unit-tested directly. The accent color comes
from the user (Windows accent or a preference) and can be anything, so the shades used for
fills and for text are *derived* from it until they meet WCAG AA against the surfaces they sit
on, rather than used as given.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Scheme = Literal["light", "dark"]

DEFAULT_ACCENT = "#0067c0"  # Windows 11 default blue
AA_TEXT = 4.5  # WCAG 2.x minimum for body text
AA_NON_TEXT = 3.0  # WCAG 2.x minimum for focus indicators and UI component boundaries

# Accent presets offered in Preferences (label, hex); "" means "follow Windows".
ACCENT_PRESETS: tuple[tuple[str, str], ...] = (
    ("Blue", "#0067c0"),
    ("Teal", "#038387"),
    ("Green", "#107c10"),
    ("Purple", "#8764b8"),
    ("Pink", "#c30052"),
    ("Red", "#c42b1c"),
    ("Orange", "#ca5010"),
    ("Graphite", "#5d6b7a"),
)


# -- color math ----------------------------------------------------------------------------
def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"not a #rrggbb color: {hex_color!r}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def is_color(value: str) -> bool:
    try:
        _rgb(value)
    except ValueError:
        return False
    return True


def mix(a: str, b: str, t: float) -> str:
    """``a`` moved a fraction ``t`` (0..1) of the way towards ``b``."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(ra, rb, strict=True)))  # type: ignore[arg-type]


def luminance(color: str) -> float:
    """WCAG relative luminance."""

    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def ensure_contrast(color: str, backgrounds: tuple[str, ...], ratio: float, toward: str) -> str:
    """``color`` moved towards ``toward`` (black or white) just far enough to reach ``ratio``
    against every background, keeping as much of its hue as possible."""
    if all(contrast(color, bg) >= ratio for bg in backgrounds):
        return color
    for step in range(1, 101):
        candidate = mix(color, toward, step / 100)
        if all(contrast(candidate, bg) >= ratio for bg in backgrounds):
            return candidate
    return toward


# -- tokens --------------------------------------------------------------------------------
@dataclass(frozen=True)
class Colors:
    scheme: Scheme
    window: str  # chrome: ribbon, panels, status bar
    surface: str  # inputs, lists, menus, panel content
    surface_alt: str  # alternate rows, subtle fills
    border: str  # dividers and card outlines
    border_strong: str  # input and button outlines
    text: str
    text_muted: str  # secondary text: hints, captions
    text_disabled: str  # WCAG exempts disabled controls from contrast rules
    hover: str  # hovered buttons, menu items, list rows
    pressed: str
    accent: str  # solid fills: default button, text selection
    on_accent: str  # text and icons on ``accent``
    accent_text: str  # accent used as text or a focus ring on window/surface
    accent_subtle: str  # checked tool buttons, selected list rows (with ``text``)
    danger: str  # error text, destructive actions
    success: str
    warning: str
    note_bg: str  # sticky-note popups
    note_text: str
    tooltip_bg: str
    tooltip_text: str
    scrollbar: str
    scrollbar_hover: str
    canvas: str  # behind the pages in the document view
    page_outline: str  # hairline around each page, so pages read even where shadows don't
    shadow: str  # page and card drop shadows (painted translucent)


@dataclass(frozen=True)
class Metrics:
    unit: int = 4  # spacing scale: multiples of 4 px
    radius_small: int = 4  # inputs, menu items
    radius: int = 6  # buttons
    radius_large: int = 8  # menus, cards, group boxes
    scrollbar: int = 12

    def space(self, n: int) -> int:
        return self.unit * n


METRICS = Metrics()

_BASE: dict[Scheme, dict[str, str]] = {
    "light": {
        "window": "#f3f3f3",
        "surface": "#ffffff",
        "surface_alt": "#f9f9f9",
        "border": "#e0e0e0",
        "border_strong": "#c2c2c2",
        "text": "#1b1b1b",
        "text_muted": "#5c5c5c",
        "text_disabled": "#a0a0a0",
        "danger": "#c42b1c",
        "success": "#0f7b0f",
        "warning": "#8a5100",
        "note_bg": "#fff4b3",
        "note_text": "#1b1b1b",
        "tooltip_bg": "#ffffff",
        "tooltip_text": "#1b1b1b",
        "canvas": "#d4d5d7",
        "page_outline": "#b4b4b4",
        "shadow": "#000000",
    },
    "dark": {
        "window": "#202020",
        "surface": "#2b2b2b",
        "surface_alt": "#323232",
        "border": "#3b3b3b",
        "border_strong": "#5a5a5a",
        "text": "#f3f3f3",
        "text_muted": "#b4b4b4",
        "text_disabled": "#6e6e6e",
        "danger": "#ff99a4",
        "success": "#6ccb5f",
        "warning": "#fce100",
        "note_bg": "#4a4220",
        "note_text": "#f3f3f3",
        "tooltip_bg": "#2c2c2c",
        "tooltip_text": "#f3f3f3",
        "canvas": "#141414",
        "page_outline": "#4d4d4d",
        "shadow": "#000000",
    },
}


def build_colors(scheme: Scheme, accent: str | None = None) -> Colors:
    """The full token set for ``scheme`` with ``accent`` (any color; None = default blue)."""
    base = _BASE[scheme]
    seed = accent if accent and is_color(accent) else DEFAULT_ACCENT
    seed = _hex(_rgb(seed))  # normalize #abc / upper case
    window, surface, text = base["window"], base["surface"], base["text"]
    surfaces = (window, surface, base["surface_alt"])
    if scheme == "light":
        # Windows 11 light: dark accent shade with white text on it.
        on_accent = "#ffffff"
        fill = ensure_contrast(seed, (on_accent,), AA_TEXT, "#000000")
        accent_text = ensure_contrast(seed, surfaces, AA_TEXT, "#000000")
        hover, pressed = mix(window, "#000000", 0.06), mix(window, "#000000", 0.10)
        subtle = mix(surface, fill, 0.16)
        scrollbar, scrollbar_hover = "#c2c2c2", "#8a8a8a"
    else:
        # Windows 11 dark: light accent shade with black text on it.
        on_accent = "#000000"
        fill = ensure_contrast(seed, (on_accent,), AA_TEXT, "#ffffff")
        accent_text = ensure_contrast(seed, surfaces, AA_TEXT, "#ffffff")
        hover, pressed = mix(window, "#ffffff", 0.08), mix(window, "#ffffff", 0.12)
        subtle = mix(surface, fill, 0.28)
        scrollbar, scrollbar_hover = "#5a5a5a", "#8a8a8a"
    # A very light/dark accent can leave the subtle tint too close to ``text``; back it off.
    subtle = ensure_contrast(subtle, (text,), AA_TEXT, surface)
    return Colors(
        scheme=scheme,
        window=window,
        surface=surface,
        surface_alt=base["surface_alt"],
        border=base["border"],
        border_strong=base["border_strong"],
        text=text,
        text_muted=base["text_muted"],
        text_disabled=base["text_disabled"],
        hover=hover,
        pressed=pressed,
        accent=fill,
        on_accent=on_accent,
        accent_text=accent_text,
        accent_subtle=subtle,
        danger=base["danger"],
        success=base["success"],
        warning=base["warning"],
        note_bg=base["note_bg"],
        note_text=base["note_text"],
        tooltip_bg=base["tooltip_bg"],
        tooltip_text=base["tooltip_text"],
        scrollbar=scrollbar,
        scrollbar_hover=scrollbar_hover,
        canvas=base["canvas"],
        page_outline=base["page_outline"],
        shadow=base["shadow"],
    )


# Page edges against the canvas (non-text contrast): a white page, and the black page of night
# mode, must stand out from the canvas through the page itself or its outline.
PAGE_COLORS: tuple[str, ...] = ("#ffffff", "#000000")
PAGE_EDGE_MIN = 1.3

# Text/background pairs that must meet AA, checked by tests for every scheme and accent.
TEXT_PAIRS: tuple[tuple[str, str], ...] = (
    ("text", "window"),
    ("text", "surface"),
    ("text", "surface_alt"),
    ("text", "hover"),
    ("text", "pressed"),
    ("text", "accent_subtle"),
    ("text_muted", "window"),
    ("text_muted", "surface"),
    ("on_accent", "accent"),
    ("accent_text", "window"),
    ("accent_text", "surface"),
    ("danger", "window"),
    ("danger", "surface"),
    ("success", "window"),
    ("success", "surface"),
    ("warning", "window"),
    ("warning", "surface"),
    ("note_text", "note_bg"),
    ("tooltip_text", "tooltip_bg"),
)
