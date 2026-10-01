"""The application style sheet, generated from tokens.

Backgrounds of text inputs are left to the palette (``Base``) on purpose: a style-sheet
background would override per-widget palettes, such as the inline text editor that sits on the
white page. Combo and spin boxes keep Fusion's own drawing for the same reason (styling their
frame forces every sub-control to be restyled).
"""

from __future__ import annotations

from pdfeditor.ui.style.tokens import METRICS, Colors, Metrics


def stylesheet(c: Colors, m: Metrics = METRICS) -> str:
    return f"""
QToolTip {{
    background: {c.tooltip_bg}; color: {c.tooltip_text};
    border: 1px solid {c.border_strong}; border-radius: {m.radius_small}px;
    padding: {m.space(1)}px {m.space(2)}px;
}}

QMenuBar {{ background: {c.window}; }}
QMenuBar::item {{
    background: transparent; padding: {m.space(1)}px {m.space(2) + 2}px;
    border-radius: {m.radius_small}px;
}}
QMenuBar::item:selected {{ background: {c.hover}; color: {c.text}; }}
QMenuBar::item:pressed {{ background: {c.pressed}; color: {c.text}; }}

QMenu {{
    background: {c.surface}; color: {c.text};
    border: 1px solid {c.border_strong}; padding: {m.space(1)}px;
}}
QMenu::item {{
    padding: {m.space(1) + 2}px {m.space(6)}px {m.space(1) + 2}px {m.space(2)}px;
    border-radius: {m.radius_small}px; background: transparent;
}}
QMenu::item:selected {{ background: {c.hover}; color: {c.text}; }}
QMenu::item:disabled {{ color: {c.text_disabled}; }}
QMenu::icon {{ padding-left: {m.space(2)}px; }}
QMenu::separator {{ height: 1px; background: {c.border}; margin: {m.space(1)}px {m.space(2)}px; }}

QToolBar {{ background: transparent; border: none; spacing: 2px; }}
QToolBar::separator {{
    background: {c.border}; width: 1px; height: 1px; margin: {m.space(2)}px {m.space(1)}px;
}}
QToolButton {{
    background: transparent; color: {c.text};
    border: 1px solid transparent; border-radius: {m.radius}px; padding: 3px;
}}
QToolButton:hover {{ background: {c.hover}; }}
QToolButton:pressed {{ background: {c.pressed}; }}
QToolButton:checked {{ background: {c.accent_subtle}; border-color: {c.accent_subtle}; }}
QToolButton:checked:hover {{ border-color: {c.accent_text}; }}
QToolButton:disabled {{ color: {c.text_disabled}; }}
QToolButton:focus {{ border-color: {c.accent_text}; }}
QToolButton[popupMode="1"] {{ padding-right: 14px; }}
QToolButton::menu-button {{
    border: none; border-left: 1px solid {c.border}; width: 12px;
    border-top-right-radius: {m.radius}px; border-bottom-right-radius: {m.radius}px;
}}

QPushButton {{
    background: {c.surface}; color: {c.text};
    border: 1px solid {c.border_strong}; border-radius: {m.radius}px;
    padding: {m.space(1) + 1}px {m.space(4)}px; min-width: 56px;
}}
QPushButton:hover {{ background: {c.surface_alt}; border-color: {c.text_muted}; }}
QPushButton:pressed {{ background: {c.hover}; }}
QPushButton:focus {{ border-color: {c.accent_text}; }}
QPushButton:disabled {{ color: {c.text_disabled}; border-color: {c.border}; }}
QPushButton:default {{
    background: {c.accent}; color: {c.on_accent}; border-color: {c.accent};
}}
QPushButton:default:hover {{ border-color: {c.accent_text}; }}
QPushButton:default:focus {{ border: 2px solid {c.accent_text}; }}
QPushButton:default:disabled {{
    background: {c.hover}; color: {c.text_disabled}; border-color: {c.border};
}}
QPushButton[role="danger"] {{
    background: {c.danger if c.scheme == "light" else c.surface};
    color: {"#ffffff" if c.scheme == "light" else c.danger}; border-color: {c.danger};
}}

QLineEdit, QPlainTextEdit, QTextEdit {{
    border: 1px solid {c.border_strong}; border-radius: {m.radius_small}px;
    padding: 2px {m.space(1)}px;
    selection-background-color: {c.accent}; selection-color: {c.on_accent};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {c.accent_text}; }}
QLineEdit:disabled {{ color: {c.text_disabled}; border-color: {c.border}; }}
InlineTextEditor {{ border: 1px dashed {c.accent_text}; border-radius: 0; padding: 0; }}

QGroupBox {{
    border: 1px solid {c.border}; border-radius: {m.radius_large}px;
    margin-top: 1.4em; padding: {m.space(2)}px {m.space(2)}px {m.space(1)}px {m.space(2)}px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left;
    left: {m.space(2)}px; padding: 0 {m.space(1)}px; font-weight: 600;
}}

QTabBar::tab {{
    background: transparent; color: {c.text_muted};
    border: none; border-bottom: 2px solid transparent;
    padding: {m.space(1) + 2}px {m.space(3)}px; margin-right: 2px;
}}
QTabBar::tab:hover {{ color: {c.text}; background: {c.hover};
    border-top-left-radius: {m.radius_small}px; border-top-right-radius: {m.radius_small}px; }}
QTabBar::tab:selected {{ color: {c.text}; border-bottom-color: {c.accent_text}; }}
QTabBar::tab:disabled {{ color: {c.text_disabled}; }}
QTabWidget::pane {{ border: 1px solid {c.border}; border-radius: {m.radius_small}px; }}
QTabWidget[documentMode="true"]::pane {{ border: none; }}

QDockWidget {{ color: {c.text}; }}
QDockWidget::title {{
    background: {c.window}; padding: {m.space(1) + 2}px {m.space(2)}px; text-align: left;
}}
/* 4 px so the dock edge is easy to grab; a 1 px border line drawn by a gradient */
QMainWindow::separator {{ width: 4px; height: 4px; background: {c.window}; }}
QMainWindow::separator:vertical {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
    stop:0 {c.window}, stop:0.49 {c.window}, stop:0.5 {c.border}, stop:0.75 {c.border},
    stop:0.76 {c.window}, stop:1 {c.window}); }}
QMainWindow::separator:horizontal {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
    stop:0 {c.window}, stop:0.49 {c.window}, stop:0.5 {c.border}, stop:0.75 {c.border},
    stop:0.76 {c.window}, stop:1 {c.window}); }}
QMainWindow::separator:hover {{ background: {c.accent_text}; }}
QSplitter::handle {{ background: {c.border}; }}
QSplitter::handle:hover {{ background: {c.accent_text}; }}
QStatusBar {{ background: {c.window}; border-top: 1px solid {c.border}; }}
QStatusBar::item {{ border: none; }}

QAbstractItemView {{
    selection-background-color: {c.accent_subtle}; selection-color: {c.text};
    alternate-background-color: {c.surface_alt};
}}
QAbstractItemView::item:hover {{ background: {c.hover}; }}
QAbstractItemView::item:selected {{ background: {c.accent_subtle}; color: {c.text}; }}
QHeaderView::section {{
    background: {c.window}; color: {c.text_muted}; border: none;
    border-bottom: 1px solid {c.border}; border-right: 1px solid {c.border};
    padding: {m.space(1)}px {m.space(2)}px;
}}

QScrollBar:vertical {{ background: transparent; width: {m.scrollbar}px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: {m.scrollbar}px; margin: 0; }}
QScrollBar::handle {{ background: {c.scrollbar}; border-radius: 3px; }}
QScrollBar::handle:vertical {{ min-height: 32px; margin: 2px 3px; }}
QScrollBar::handle:horizontal {{ min-width: 32px; margin: 3px 2px; }}
QScrollBar::handle:hover, QScrollBar::handle:pressed {{ background: {c.scrollbar_hover}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QLabel[role="error"] {{ color: {c.danger}; }}
QLabel[role="success"] {{ color: {c.success}; }}
QLabel[role="warning"] {{ color: {c.warning}; }}
QLabel[role="muted"] {{ color: {c.text_muted}; }}

NotePopup {{ background: {c.note_bg}; border: 1px solid {c.border_strong}; }}
NotePopup > QLabel {{ color: {c.note_text}; }}
"""
