"""Floating mini toolbars next to what's selected (Phase U7).

* Text selected with the Select tool: Copy, Highlight, Underline, Strikeout, Add note, Redact
  (and Edit text where the engine can edit content).
* A comment or shape selected: color, opacity, Reply, Delete (locked ones: Reply and info).
* Pages selected in the organizer: Rotate left/right, Delete, Extract.

They are shortcuts: every button runs an existing action or command (so it is undoable and
also reachable from the ribbon, the menus and the keyboard). Each bar sits above what's
selected (below when there's no room), never over it, inside the page area. Like the pill it is
a child of the viewport, and it follows the view through signals, never event filters. It
hides on scroll, zoom, Esc or when the selection goes, and doesn't take the keyboard from the
page: Tab from the page walks into it, Esc goes back.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from itertools import pairwise
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QAbstractAnimation,
    QCoreApplication,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QMenu,
    QToolButton,
    QWidget,
)

from pdfeditor.core.commands import Command, MacroCommand, UpdateAnnotationCommand
from pdfeditor.model.annotations import AnnotationModel, AnnotationType
from pdfeditor.model.color import Color
from pdfeditor.ui.color_picker import pick_color, swatch_icon
from pdfeditor.ui.i18n import QT_TRANSLATE_NOOP
from pdfeditor.ui.icons import icon
from pdfeditor.ui.panels.inspector import restyled, shown_color
from pdfeditor.ui.style.tokens import METRICS
from pdfeditor.ui.theme import current_colors, theme_manager
from pdfeditor.ui.tools import annotate
from pdfeditor.ui.view import motion
from pdfeditor.ui.view.document_view import DocumentView, annotation_bounds

if TYPE_CHECKING:
    from pdfeditor.ui.main_window import MainWindow
    from pdfeditor.ui.organizer.organizer import OrganizerWidget

SHADOW = 4  # px around the bar reserved for its drop shadow
GAP = 6  # px between the bar and what it's about
MARGIN = 4  # px kept free between the bar and the edges of the page area

# Comment colors offered as swatches (document colors, not UI colors).
SWATCHES: tuple[tuple[str, Color], ...] = (
    (QT_TRANSLATE_NOOP("ContextualToolbars", "Yellow"), Color(1, 0.92, 0)),
    (QT_TRANSLATE_NOOP("ContextualToolbars", "Green"), Color(0, 0.6, 0.2)),
    (QT_TRANSLATE_NOOP("ContextualToolbars", "Blue"), Color(0.1, 0.3, 0.9)),
    (QT_TRANSLATE_NOOP("ContextualToolbars", "Red"), Color(0.85, 0.1, 0.1)),
    (QT_TRANSLATE_NOOP("ContextualToolbars", "Orange"), Color(1, 0.45, 0)),
    (QT_TRANSLATE_NOOP("ContextualToolbars", "Purple"), Color(0.55, 0.2, 0.75)),
    (QT_TRANSLATE_NOOP("ContextualToolbars", "Black"), Color(0, 0, 0)),
)
OPACITIES = (100, 75, 50, 25)  # percent


def _tr(text: str) -> str:
    """Strings of the bars, context "ContextualToolbars" (QT_TRANSLATE_NOOP marks the ones
    in tables)."""
    return QCoreApplication.translate("ContextualToolbars", text)


def place(size: QSize, target: QRect, area: QRect) -> QPoint:
    """Top-left for a bar of ``size`` about ``target`` inside ``area``: centred above it,
    else below it, never over it when either side has room; always inside ``area``."""
    w, h = size.width(), size.height()
    left, right = area.left() + MARGIN, area.right() + 1 - MARGIN - w
    x = target.center().x() - w // 2
    x = max(left, min(x, right)) if right >= left else area.left()
    above = target.top() - GAP - h + SHADOW
    below = target.bottom() + 1 + GAP - SHADOW
    top, bottom = area.top() + MARGIN, area.bottom() + 1 - MARGIN - h
    if above >= top:
        y = above
    elif below <= bottom:
        y = below
    else:  # the selection fills the page area: overlap on the side with more room
        room_above = target.top() - area.top()
        room_below = area.bottom() - target.bottom()
        y = top if room_above >= room_below else bottom
    return QPoint(x, max(area.top(), min(y, max(area.top(), bottom))))


class MiniToolbar(QFrame):
    """A small floating row of icon buttons with a rounded, shadowed background."""

    def __init__(self, name: str, home: QWidget) -> None:
        super().__init__(home)
        self.setObjectName("MiniToolbar")
        self.setAccessibleName(name)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.home = home  # parent while there's nothing to show on
        self.return_focus: QWidget | None = None  # where Esc sends the keyboard
        self.buttons: list[QToolButton] = []
        self._separators: list[QWidget] = []
        self._layout = QHBoxLayout(self)
        pad = METRICS.space(1)
        self._layout.setContentsMargins(SHADOW + pad, SHADOW + pad, SHADOW + pad, SHADOW + pad)
        self._layout.setSpacing(2)
        # As with the pill, the opacity effect is on only while fading in.
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setEnabled(False)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(motion.FADE_MS)
        self._fade.finished.connect(lambda: self._effect.setEnabled(False))
        theme_manager().changed.connect(self.update)
        self.hide()

    # -- building -------------------------------------------------------------------------
    def add_button(
        self, icon_name: str, name: str, slot: Callable[[], object], tip: str = ""
    ) -> QToolButton:
        button = QToolButton(self)
        button.setIcon(icon(icon_name))
        button.setIconSize(QSize(18, 18))
        button.setAutoRaise(True)
        button.setAccessibleName(name)
        button.setToolTip(tip or name)
        # Clicking doesn't take the keyboard from the page; Tab still reaches the button.
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.setCursor(Qt.CursorShape.ArrowCursor)
        button.clicked.connect(lambda _=False: slot())
        self._layout.addWidget(button)
        self.buttons.append(button)
        return button

    def add_menu_button(self, icon_name: str, name: str, menu: QMenu) -> QToolButton:
        button = self.add_button(icon_name, name, lambda: None)
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        return button

    def add_separator(self) -> QWidget:
        line = QWidget(self)  # painted by paintEvent, in the border color
        line.setFixedWidth(1)
        self._layout.addWidget(line)
        self._separators.append(line)
        return line

    def visible_buttons(self) -> list[QToolButton]:
        return [b for b in self.buttons if not b.isHidden()]

    # -- showing --------------------------------------------------------------------------
    def show_for(self, host: QWidget, target: QRect) -> None:
        """Show inside ``host`` (a viewport) next to ``target`` (host coordinates)."""
        if self.parentWidget() is not host:
            self.setParent(host)
        self.adjustSize()
        size = self.sizeHint()
        top_left = place(size, target, host.rect())
        geometry = QRect(top_left, size)
        if self.geometry() != geometry:
            self.setGeometry(geometry)
        if self.isHidden():
            self.raise_()
            self.show()
            self._fade_in()
        self.raise_()

    def _fade_in(self) -> None:
        self._fade.stop()
        if not motion.animations_enabled():
            self._effect.setEnabled(False)
            return
        self._effect.setOpacity(0.0)
        self._effect.setEnabled(True)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    @property
    def fading(self) -> bool:
        return self._fade.state() is QAbstractAnimation.State.Running

    def has_focus(self) -> bool:
        focus = QApplication.focusWidget()
        return focus is not None and self.isAncestorOf(focus)

    def dismiss(self) -> None:
        """Hide; if the keyboard was in the bar, it goes back to the page."""
        if self.isHidden():
            return
        had_focus = self.has_focus()
        self.hide()
        if had_focus and self.return_focus is not None:
            self.return_focus.setFocus(Qt.FocusReason.OtherFocusReason)

    def park(self) -> None:
        """Back to the window (the host is going away)."""
        self.dismiss()
        if self.parentWidget() is not self.home:
            self.setParent(self.home)
            self.hide()

    def chain_after(self, first: QWidget) -> None:
        """Tab from ``first`` (the page) walks into the bar, left to right."""
        chain = [first, *self.visible_buttons()]
        for a, b in pairwise(chain):
            QWidget.setTabOrder(a, b)

    # -- keyboard -------------------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.dismiss()
            if self.return_focus is not None:
                self.return_focus.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            buttons = self.visible_buttons()
            focus = QApplication.focusWidget()
            if focus in buttons:
                step = 1 if key == Qt.Key.Key_Right else -1
                index = (buttons.index(focus) + step) % len(buttons)
                buttons[index].setFocus(Qt.FocusReason.TabFocusReason)
                return
        super().keyPressEvent(event)

    # Clicks on the bar's own background stay here instead of reaching the page.
    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def paintEvent(self, event: QPaintEvent) -> None:
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        body = QRectF(self.rect()).adjusted(SHADOW, SHADOW - 1, -SHADOW, -SHADOW - 1)
        radius = float(METRICS.radius_large)
        shadow = QColor(colors.shadow)
        painter.setPen(Qt.PenStyle.NoPen)
        for spread, alpha in ((1, 34), (2, 20), (3, 10), (4, 5)):
            shadow.setAlpha(alpha)
            painter.setBrush(shadow)
            r = body.adjusted(-spread, -spread + 1, spread, spread + 1)
            painter.drawRoundedRect(r, radius + spread, radius + spread)
        painter.setBrush(QColor(colors.surface))
        pen = QPen(QColor(colors.border_strong))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        for line in self._separators:
            if not line.isHidden():
                painter.fillRect(line.geometry().adjusted(0, 3, 0, -3), QColor(colors.border))
        painter.end()


class ContextualToolbars:
    """The window's three mini toolbars; they move into the current document."""

    def __init__(self, window: MainWindow) -> None:
        self.w = window
        self.view: DocumentView | None = None
        self.organizer: OrganizerWidget | None = None
        self._pressed = False  # a mouse button is down on the pages (selecting, dragging)
        self._dismissed = False  # scrolled, zoomed or Esc: stay hidden until the next selection
        self.text_bar = self._text_bar()
        self.annotation_bar = self._annotation_bar()
        self.page_bar = self._page_bar()

    @property
    def enabled(self) -> bool:
        return self.w.prefs.show_mini_toolbars

    # -- the bars -------------------------------------------------------------------------
    def _text_bar(self) -> MiniToolbar:
        bar = MiniToolbar(_tr("Text actions"), self.w)
        copy_keys = self.w.act_copy.shortcut().toString()
        self.copy_button = bar.add_button(
            "copy",
            _tr("Copy"),
            self.w.act_copy.trigger,
            _tr("Copy ({keys})").format(keys=copy_keys) if copy_keys else "",
        )
        bar.add_separator()
        self.highlight_button = bar.add_button(
            "highlighter", _tr("Highlight"), lambda: self._markup("highlight")
        )
        self.underline_button = bar.add_button(
            "underline", _tr("Underline"), lambda: self._markup("underline")
        )
        self.strikeout_button = bar.add_button(
            "strikethrough", _tr("Strikethrough"), lambda: self._markup("strikeout")
        )
        self.note_button = bar.add_button(
            "sticky-note", _tr("Add note"), self.add_note, _tr("Add a note to the selected text")
        )
        bar.add_separator()
        self.redact_button = bar.add_button(
            "eraser", _tr("Mark for redaction"), self.w.protect.start_redact_tool
        )
        self.edit_text_button = bar.add_button(
            "file-pen-line", _tr("Edit text"), self.edit_text, _tr("Edit text && images")
        )
        return bar

    def _annotation_bar(self) -> MiniToolbar:
        bar = MiniToolbar(_tr("Comment actions"), self.w)
        self.color_menu = QMenu(bar)
        for name, color in SWATCHES:
            action = self.color_menu.addAction(swatch_icon(color), _tr(name))
            action.triggered.connect(lambda _=False, c=color: self.set_annotation_color(c))
        self.color_menu.addSeparator()
        self.color_menu.addAction(_tr("More Colors…"), self.pick_annotation_color)
        self.color_button = bar.add_menu_button("paint-bucket", _tr("Color"), self.color_menu)
        self.opacity_menu = QMenu(bar)
        group = QActionGroup(self.opacity_menu)
        self.opacity_actions: dict[int, QAction] = {}
        for percent in OPACITIES:
            action = self.opacity_menu.addAction(f"{percent}%")
            action.setCheckable(True)
            group.addAction(action)
            action.triggered.connect(
                lambda _=False, p=percent: self.set_annotation_opacity(p / 100)
            )
            self.opacity_actions[percent] = action
        self.opacity_menu.aboutToShow.connect(self._check_opacity)
        self.opacity_button = bar.add_menu_button("droplet", _tr("Opacity"), self.opacity_menu)
        self.annotation_separator = bar.add_separator()
        self.reply_button = bar.add_button("message-square-text", _tr("Reply"), self.reply)
        self.info_button = bar.add_button(
            "lock",
            _tr("Locked: show properties"),
            self.w.show_inspector,
            _tr("Locked · Properties"),
        )
        self.delete_button = bar.add_button("trash-2", _tr("Delete"), self.delete_annotations)
        return bar

    def _page_bar(self) -> MiniToolbar:
        bar = MiniToolbar(_tr("Page actions"), self.w)
        organize = self.w.organize
        for icon_name, name, action in (
            ("rotate-ccw", _tr("Rotate left"), organize.act_rotate_left),
            ("rotate-cw", _tr("Rotate right"), organize.act_rotate_right),
            ("file-x", _tr("Delete pages"), organize.act_delete),
            ("file-output", _tr("Extract pages"), organize.act_extract),
        ):
            keys = action.shortcut().toString()
            tip = _tr("{name} ({keys})").format(name=name, keys=keys) if keys else name
            bar.add_button(icon_name, name, action.trigger, tip)
        return bar

    # -- binding --------------------------------------------------------------------------
    def attach(self, view: DocumentView | None) -> None:
        """Follow ``view`` (the current document) and its organizer."""
        if view is not self.view:
            self._bind(view)
        tab = self.w.current_tab()
        organizer = tab.organizer if tab is not None else None
        if organizer is not self.organizer:
            self._bind_organizer(organizer)
        self.update_pages()

    def _view_signals(self, view: DocumentView) -> list[tuple[object, Callable[..., None]]]:
        return [
            (view.selection_changed, self._on_selection_changed),
            (view.annotation_selection_changed, self._on_selection_changed),
            (view.pointer_pressed, self._on_pressed),
            (view.pointer_released, self._on_released),
            (view.zoom_changed, self._on_moved),
            (view.geometry_changed, self.refresh),
            (view.document_changed, self.refresh),
            (view.verticalScrollBar().valueChanged, self._on_moved),
            (view.horizontalScrollBar().valueChanged, self._on_moved),
        ]

    def _bind(self, view: DocumentView | None) -> None:
        old = self.view
        if old is not None:
            for signal, slot in self._view_signals(old):
                with contextlib.suppress(RuntimeError, TypeError):  # being destroyed
                    signal.disconnect(slot)  # type: ignore[attr-defined]
        self.view = view
        self._pressed = False
        self._dismissed = False
        for bar in (self.text_bar, self.annotation_bar):
            bar.park()
            bar.return_focus = view
        if view is None:
            return
        for signal, slot in self._view_signals(view):
            signal.connect(slot)  # type: ignore[attr-defined]
        self.refresh()

    def _bind_organizer(self, organizer: OrganizerWidget | None) -> None:
        old = self.organizer
        if old is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                old.selection_changed.disconnect(self._on_pages_selected)
                old.grid.verticalScrollBar().valueChanged.disconnect(self._on_grid_scrolled)
        self.page_bar.park()
        self.organizer = organizer
        if organizer is not None:
            self.page_bar.return_focus = organizer.grid
            organizer.selection_changed.connect(self._on_pages_selected)
            organizer.grid.verticalScrollBar().valueChanged.connect(self._on_grid_scrolled)

    # -- events ---------------------------------------------------------------------------
    def _on_selection_changed(self) -> None:
        self._dismissed = False
        self.refresh()

    def _on_pressed(self) -> None:
        self._pressed = True
        self.text_bar.dismiss()
        self.annotation_bar.dismiss()

    def _on_released(self) -> None:
        self._pressed = False
        self._dismissed = False
        self.refresh()

    def _on_moved(self, *_args: object) -> None:
        """Scrolled or zoomed: the bars would float away from the selection."""
        if self.text_bar.isVisible() or self.annotation_bar.isVisible():
            self._dismissed = True
        self.text_bar.dismiss()
        self.annotation_bar.dismiss()

    def _on_pages_selected(self) -> None:
        self.update_pages()

    def _on_grid_scrolled(self, _value: int) -> None:
        self.page_bar.dismiss()

    # -- showing --------------------------------------------------------------------------
    def refresh(self, *_args: object) -> None:
        """Show, move or hide the page view's bars to match the selection."""
        view = self.view
        ready = (
            view is not None
            and self.enabled
            and not self._pressed
            and not self._dismissed
            and view.isVisible()
        )
        self._update_text_bar(view if ready else None)
        self._update_annotation_bar(view if ready else None)
        self.update_pages()

    def _update_text_bar(self, view: DocumentView | None) -> None:
        rect = None
        if view is not None and view.tool.name == "select" and not view.selected_annotations:
            rect = view.selection_viewport_rect()
        if view is None or rect is None:
            self.text_bar.dismiss()
            return
        self.edit_text_button.setVisible(view.session.engine.capabilities.content_edit)
        self.text_bar.show_for(view.viewport(), rect)
        self.text_bar.chain_after(view)

    def _update_annotation_bar(self, view: DocumentView | None) -> None:
        models = view.selected_models() if view is not None and not view.tool.busy else []
        rect = None
        if view is not None and models:
            rect = view.viewport_rect([(m.page_index, annotation_bounds(m)) for m in models])
        if view is None or rect is None:
            self.annotation_bar.dismiss()
            return
        editable = any(not m.locked for m in models)
        for widget in (
            self.color_button,
            self.opacity_button,
            self.annotation_separator,
            self.delete_button,
        ):
            widget.setVisible(editable)
        self.info_button.setVisible(not editable)
        self.reply_button.setVisible(len(models) == 1)
        self.annotation_separator.setVisible(editable and len(models) == 1)
        color = next((shown_color(m) for m in models if not m.locked), None)
        self.color_button.setIcon(
            swatch_icon(color, QSize(16, 16)) if color is not None else icon("paint-bucket")
        )
        self.annotation_bar.show_for(view.viewport(), rect)
        self.annotation_bar.chain_after(view)

    def update_pages(self) -> None:
        organizer = self.organizer
        tab = self.w.current_tab()
        showing = (
            organizer is not None
            and tab is not None
            and tab.organizer is organizer
            and tab.organizing
            and self.enabled
            and organizer.isVisible()
        )
        pages = organizer.selected_pages() if showing and organizer is not None else []
        if organizer is None or not pages:
            self.page_bar.dismiss()
            return
        grid = organizer.grid
        port = grid.viewport().rect()
        rect = QRect()
        for page in pages:
            rect = rect.united(grid.visualRect(organizer.model.index(page)) & port)
        if rect.isEmpty():
            self.page_bar.dismiss()
            return
        self.page_bar.show_for(grid.viewport(), rect)
        self.page_bar.chain_after(grid)

    # -- text actions ---------------------------------------------------------------------
    def _markup(self, tool: str) -> None:
        """Highlight / underline / strike out the selection (the tool applies it at once)."""
        if self.view is not None and self.view.has_selection():
            self.w.set_tool(tool, self.view)

    def add_note(self) -> bool:
        """Highlight the selection with a note on it (one undo step)."""
        view = self.view
        if view is None or not view.has_selection():
            return False
        text = annotate.ask_text(view, _tr("Add Note"))
        if text is None:
            return False
        return annotate.apply_markup(view, AnnotationType.HIGHLIGHT, "Add Note to Text", text)

    def edit_text(self) -> None:
        if self.view is not None:
            self.view.clear_selection()
            self.w.set_tool("edit", self.view)

    # -- comment actions ------------------------------------------------------------------
    def _editable(self) -> list[AnnotationModel]:
        return [m for m in self.view.selected_models() if not m.locked] if self.view else []

    def _restyle(self, field: str, value: object, label: str) -> bool:
        view = self.view
        if view is None:
            return False
        commands: list[Command] = []
        for model in self._editable():
            after = restyled(model, field, value)
            if after is not None:
                commands.append(UpdateAnnotationCommand(model, after, label))
        if not commands:
            return False
        view.session.execute(commands[0] if len(commands) == 1 else MacroCommand(label, commands))
        return True

    def set_annotation_color(self, color: Color) -> bool:
        return self._restyle("color", color, "Change Color")

    def pick_annotation_color(self) -> None:
        models = self._editable()
        if not models:
            return
        chosen = pick_color(shown_color(models[0]) or Color(1, 0, 0), self.annotation_bar)
        if chosen is not None:
            self.set_annotation_color(chosen)

    def set_annotation_opacity(self, opacity: float) -> bool:
        return self._restyle("opacity", opacity, "Change Opacity")

    def _check_opacity(self) -> None:
        models = self._editable()
        current = round(models[0].opacity * 100) if models else None
        for percent, action in self.opacity_actions.items():
            action.setChecked(percent == current)

    def reply(self) -> None:
        view = self.view
        models = view.selected_models() if view is not None else []
        if view is not None and len(models) == 1:
            self.w.reply_to(view, models[0])

    def delete_annotations(self) -> None:
        if self.view is not None:
            self.view.delete_selected_annotations()
