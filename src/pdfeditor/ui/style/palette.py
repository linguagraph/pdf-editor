"""QPalette from tokens, for everything the style sheet doesn't draw (Fusion, icons, views)."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

from pdfeditor.ui.style.tokens import Colors, Scheme, contrast, luminance, mix

Role = QPalette.ColorRole
Group = QPalette.ColorGroup


def build_palette(c: Colors) -> QPalette:
    p = QPalette()
    toward = "#000000" if c.scheme == "light" else "#ffffff"
    roles = {
        Role.Window: c.window,
        Role.WindowText: c.text,
        Role.Base: c.surface,
        Role.AlternateBase: c.surface_alt,
        Role.Text: c.text,
        Role.Button: c.surface,
        Role.ButtonText: c.text,
        Role.BrightText: c.danger,
        Role.Highlight: c.accent,
        Role.HighlightedText: c.on_accent,
        Role.ToolTipBase: c.tooltip_bg,
        Role.ToolTipText: c.tooltip_text,
        Role.PlaceholderText: c.text_muted,
        Role.Link: c.accent_text,
        Role.LinkVisited: c.accent_text,
        Role.Accent: c.accent,
        # Fusion's bevels and frames
        Role.Light: mix(c.surface, "#ffffff", 0.5),
        Role.Midlight: mix(c.window, toward, 0.03),
        Role.Mid: c.border_strong,
        Role.Dark: mix(c.border_strong, toward, 0.2),
        Role.Shadow: mix(c.border_strong, toward, 0.5),
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (Role.WindowText, Role.Text, Role.ButtonText, Role.PlaceholderText):
        p.setColor(Group.Disabled, role, QColor(c.text_disabled))
    p.setColor(Group.Disabled, Role.Highlight, QColor(c.hover))
    p.setColor(Group.Disabled, Role.HighlightedText, QColor(c.text_disabled))
    # Inactive windows keep their selection color (Fusion would grey it out).
    p.setColor(Group.Inactive, Role.Highlight, QColor(c.accent))
    p.setColor(Group.Inactive, Role.HighlightedText, QColor(c.on_accent))
    return p


def colors_from_palette(p: QPalette) -> Colors:
    """Tokens taken from a system palette, for Windows High Contrast.

    The style sheet is off then, but custom-painted widgets (page cards, the pill, toasts,
    rails...) still ask for tokens; these map every token to a palette role, so the user's
    contrast colors reach them too. Muted and subtle shades collapse to the full colors:
    contrast themes don't have them.
    """

    def role(r: QPalette.ColorRole, group: QPalette.ColorGroup = Group.Active) -> str:
        return p.color(group, r).name()

    window, text = role(Role.Window), role(Role.WindowText)
    surface = role(Role.Base)
    highlight, on_highlight = role(Role.Highlight), role(Role.HighlightedText)
    scheme: Scheme = "dark" if luminance(window) < 0.5 else "light"
    return Colors(
        scheme=scheme,
        window=window,
        surface=surface,
        surface_alt=role(Role.AlternateBase),
        border=text,
        border_strong=text,
        text=text,
        text_muted=text,
        text_disabled=role(Role.WindowText, Group.Disabled),
        hover=surface,
        pressed=surface,
        accent=highlight,
        on_accent=on_highlight,
        accent_text=highlight if contrast(highlight, window) >= 3.0 else text,
        accent_subtle=surface,
        danger=text,
        success=text,
        warning=text,
        note_bg=surface,
        note_text=role(Role.Text),
        tooltip_bg=role(Role.ToolTipBase),
        tooltip_text=role(Role.ToolTipText),
        scrollbar=text,
        scrollbar_hover=highlight,
        canvas=window,
        page_outline=text,
        shadow="#000000",
        ring_selected=text,
    )
