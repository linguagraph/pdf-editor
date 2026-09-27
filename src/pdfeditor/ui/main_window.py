"""Main application window: tabbed documents, ribbon, navigation pane, status bar."""

from __future__ import annotations

import copy
import functools
import logging
import re
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QPoint, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QKeySequence,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStyle,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor import __version__
from pdfeditor.core import engine_lock
from pdfeditor.core.autosave import Autosaver, RecoveryStore
from pdfeditor.core.commands import (
    AddAnnotationCommand,
    Command,
    MacroCommand,
    ReorderAnnotationCommand,
    SnapshotCommand,
    UpdateAnnotationCommand,
)
from pdfeditor.core.layout import LayoutMode
from pdfeditor.core.paths import recovery_dir
from pdfeditor.core.render_cache import RenderCache
from pdfeditor.core.session import DocumentSession, EventKind, SessionEvent
from pdfeditor.engine.base import Document, Engine, OpenError, PasswordRequired, SaveError
from pdfeditor.engine.registry import get_engine
from pdfeditor.model.annotations import (
    STANDARD_STAMPS,
    AnnotationModel,
    AnnotationType,
    ReviewState,
)
from pdfeditor.model.geometry import Matrix
from pdfeditor.model.outline import Link, LinkKind
from pdfeditor.ui.compare_controller import CompareController
from pdfeditor.ui.dialogs.password import password_prompt
from pdfeditor.ui.dialogs.preferences import PreferencesDialog
from pdfeditor.ui.dialogs.print_dialog import PrintDialog
from pdfeditor.ui.dialogs.properties import PropertiesDialog
from pdfeditor.ui.dialogs.recovery import RecoveryDialog
from pdfeditor.ui.document_tab import DocumentTab
from pdfeditor.ui.edit_controller import EDIT_TOOLS, EditController, make_edit_tool
from pdfeditor.ui.export_controller import ExportController
from pdfeditor.ui.optimize_controller import OptimizeController
from pdfeditor.ui.organize import OrganizeController
from pdfeditor.ui.panels.accessibility import AccessibilityPanel, TagsPanel
from pdfeditor.ui.panels.attachments import AttachmentsPanel
from pdfeditor.ui.panels.base import ViewPanel
from pdfeditor.ui.panels.bookmarks import BookmarksPanel
from pdfeditor.ui.panels.comments import CommentsPanel, add_reply
from pdfeditor.ui.panels.inspector import InspectorPanel
from pdfeditor.ui.panels.layers import LayersPanel
from pdfeditor.ui.panels.search import SearchPanel
from pdfeditor.ui.panels.thumbnails import ThumbnailsPanel
from pdfeditor.ui.pdfa_controller import PdfaController
from pdfeditor.ui.protect_controller import ProtectController, RedactTool
from pdfeditor.ui.ribbon import Ribbon
from pdfeditor.ui.settings import AppSettings
from pdfeditor.ui.theme import Theme, apply_theme
from pdfeditor.ui.tools import annotate
from pdfeditor.ui.tools.base import Tool
from pdfeditor.ui.tools.hand import HandTool
from pdfeditor.ui.tools.select import SelectTool
from pdfeditor.ui.tools_controller import ToolsController
from pdfeditor.ui.view.document_view import DocumentView
from pdfeditor.ui.view.note_popup import NotePopup
from pdfeditor.ui.view.renderer import TileRenderer

log = logging.getLogger(__name__)

# (action name, label, standard icon name or None)
COMMENT_TOOLS = (
    ("highlight", "&Highlight", None),
    ("underline", "&Underline", None),
    ("strikeout", "S&trikethrough", None),
    ("squiggly", "S&quiggly", None),
    ("note", "Sticky &Note", "SP_MessageBoxInformation"),
    ("textbox", "Te&xt Box", None),
    ("stamp", "Sta&mp", None),
    ("attach", "Attach &File", "SP_FileLinkIcon"),
    ("rectangle", "&Rectangle", None),
    ("oval", "&Oval", None),
    ("line", "&Line", None),
    ("arrow", "&Arrow", None),
    ("polygon", "Pol&ygon", None),
    ("polyline", "Poly&line", None),
    ("pen", "&Pen", None),
)
EDIT_TOOL_NAMES = {name for name, _t, _k in EDIT_TOOLS}
# Tools that aren't "make one thing" tools: they don't switch back after use.
STAY_ACTIVE = {"select", "hand", "edit"}
MARKUP_TOOLS = {
    "highlight": AnnotationType.HIGHLIGHT,
    "underline": AnnotationType.UNDERLINE,
    "strikeout": AnnotationType.STRIKEOUT,
    "squiggly": AnnotationType.SQUIGGLY,
}


def _split_camel(name: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def annotate_transform(model: AnnotationModel, m: Matrix) -> AnnotationModel:
    from pdfeditor.model.annotations import transformed

    return transformed(model, m)


MAX_RECENT = 10
SETTINGS_RECENT = "recent_files"
SETTINGS_THEME = "theme"
SETTINGS_GEOMETRY = "window/geometry"
SETTINGS_STATE = "window/state"
PDF_FILTER = "PDF documents (*.pdf);;All files (*)"


class PageNavigator(QWidget):
    """First/prev/[page]/of N/next/last controls; accepts page labels or numbers."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window
        style = self.style()

        def button(icon: QStyle.StandardPixmap, tip: str, slot: Callable[[], None]) -> QToolButton:
            b = QToolButton(self)
            b.setIcon(style.standardIcon(icon))
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(slot)
            return b

        self.first = button(
            QStyle.StandardPixmap.SP_MediaSkipBackward, "First page", window.first_page
        )
        self.prev = button(QStyle.StandardPixmap.SP_ArrowUp, "Previous page", window.previous_page)
        self.edit = QLineEdit(self)
        self.edit.setFixedWidth(56)
        self.edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.edit.returnPressed.connect(self._jump)
        self.total = QLabel(self)
        self.next = button(QStyle.StandardPixmap.SP_ArrowDown, "Next page", window.next_page)
        self.last = button(QStyle.StandardPixmap.SP_MediaSkipForward, "Last page", window.last_page)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for w in (self.first, self.prev, self.edit, self.total, self.next, self.last):
            layout.addWidget(w)

    def update_state(self, view: DocumentView | None) -> None:
        enabled = view is not None and view.page_count > 0
        for w in (self.first, self.prev, self.edit, self.next, self.last):
            w.setEnabled(enabled)
        if view is None or not enabled:
            self.edit.clear()
            self.total.clear()
            return
        page = view.current_page
        label = view.page_label(page)
        self.edit.setText(label)
        numeric = label == str(page + 1)
        self.total.setText(
            f"of {view.page_count}" if numeric else f"({page + 1} of {view.page_count})"
        )
        self.first.setEnabled(page > 0)
        self.prev.setEnabled(page > 0)
        self.next.setEnabled(page < view.page_count - 1)
        self.last.setEnabled(page < view.page_count - 1)

    def _jump(self) -> None:
        view = self._window.current_view()
        if view is None:
            return
        index = view.page_index_for_label(self.edit.text())
        if index is None:
            QApplication.beep()
            self.update_state(view)
        else:
            view.go_to_page(index)
            view.setFocus()


class ZoomBox(QComboBox):
    PRESETS = ("Fit Width", "Fit Page", "50%", "75%", "100%", "125%", "150%", "200%", "400%")

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItems(self.PRESETS)
        self.setMinimumContentsLength(8)
        self.textActivated.connect(self._apply)

    def show_zoom(self, zoom: float) -> None:
        self.setEditText(f"{zoom * 100:.0f}%")

    def _apply(self, text: str) -> None:
        view = self._window.current_view()
        if view is None:
            return
        if text == "Fit Width":
            view.fit_width()
        elif text == "Fit Page":
            view.fit_page()
        else:
            try:
                view.set_zoom(float(text.strip().rstrip("%")) / 100)
            except ValueError:
                self.show_zoom(view.zoom)
        view.setFocus()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("pdfeditor")
        self.resize(1280, 860)
        self.setAcceptDrops(True)
        self.settings = QSettings()
        self.prefs = AppSettings(self.settings)
        cache = RenderCache[QImage](self.prefs.cache_mb * 1024 * 1024)
        self.renderer = TileRenderer(cache, self)
        self.recovery = RecoveryStore(recovery_dir())
        self.autosaver = Autosaver(self.recovery)
        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self.autosave_now)
        # Cyclic GC runs here, under the engine lock (see core/engine_lock.py).
        self.gc_timer = QTimer(self)
        self.gc_timer.timeout.connect(engine_lock.collect)
        self.gc_timer.start(4000)

        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.welcome = QLabel(
            "Open a PDF with Ctrl+O, or drop files here.", alignment=Qt.AlignmentFlag.AlignCenter
        )

        self.ribbon = Ribbon(self)
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.ribbon)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.welcome, 1)
        self.setCentralWidget(central)

        self.search_panel = SearchPanel()
        self.comments_panel = CommentsPanel()
        self.inspector = InspectorPanel()
        self.panels: list[ViewPanel] = [
            ThumbnailsPanel(),
            BookmarksPanel(),
            self.comments_panel,
            self.search_panel,
            AttachmentsPanel(),
            LayersPanel(),
        ]
        self.accessibility_panel = AccessibilityPanel()
        self.tags_panel = TagsPanel()
        self.panels += [self.accessibility_panel, self.tags_panel]
        self.accessibility_panel.tag_requested.connect(self._show_tag)
        self.nav_tabs = QTabWidget()
        self.nav_tabs.setDocumentMode(True)
        for panel in self.panels:
            self.nav_tabs.addTab(panel, panel.title)
        self.nav_dock = QDockWidget("Navigation", self)
        self.nav_dock.setObjectName("navigation")
        self.nav_dock.setWidget(self.nav_tabs)
        self.nav_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.nav_dock)
        self.inspector_dock = QDockWidget("Properties", self)
        self.inspector_dock.setObjectName("inspector")
        self.inspector_dock.setWidget(self.inspector)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.inspector_dock)
        self.inspector_dock.hide()  # opened on demand: the page needs the width
        self.panels.append(self.inspector)
        self._annotation_clipboard: list[AnnotationModel] = []
        self._note_popup: NotePopup | None = None
        self._state_restored = False

        self.navigator = PageNavigator(self)
        self.zoom_box = ZoomBox(self)
        self.tool_label = QLabel(self)
        self.tool_exit = QToolButton(self)
        self.tool_exit.setText("✕")
        self.tool_exit.setToolTip("Stop using this tool (Esc)")
        self.tool_exit.setAutoRaise(True)
        self.tool_exit.clicked.connect(lambda: self.set_tool("select"))
        self.statusBar().addWidget(self.tool_label)
        self.statusBar().addWidget(self.tool_exit)
        self.tool_label.hide()
        self.tool_exit.hide()
        self.statusBar().addPermanentWidget(self.navigator)
        self.statusBar().addPermanentWidget(self.zoom_box)

        self._create_actions()
        self.organize = OrganizeController(self)
        self.edit = EditController(self, self.tool_group, self.tool_actions)
        self.protect = ProtectController(self, self.tool_group, self.tool_actions)
        self.tools = ToolsController(self)
        self.export = ExportController(self)
        self.optimize = OptimizeController(self)
        self.compare = CompareController(self)
        self.pdfa = PdfaController(self)
        self.panels.insert(3, self.protect.panel)
        self.nav_tabs.insertTab(3, self.protect.panel, self.protect.panel.title)
        self.search_panel.hits_changed.connect(self._update_ui)
        self._create_menus()
        self._create_ribbon()
        self._restore_settings()
        self._apply_prefs()
        self._update_ui()

    # -- actions --------------------------------------------------------------------------
    def _action(
        self,
        text: str,
        slot: Callable[..., object],
        shortcut: QKeySequence | QKeySequence.StandardKey | str | None = None,
        icon: QStyle.StandardPixmap | None = None,
        needs_doc: bool = True,
        checkable: bool = False,
        scope: QWidget | None = None,
    ) -> QAction:
        """Create an action. ``scope`` limits its shortcut to that widget and its children."""
        action = QAction(text, self)
        if icon is not None:
            action.setIcon(self.style().standardIcon(icon))
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        action.setCheckable(checkable)
        if checkable:
            action.toggled.connect(slot)
        else:
            action.triggered.connect(slot)
        action.setProperty("needs_doc", needs_doc)
        if scope is not None:
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            scope.addAction(action)
        else:
            self.addAction(action)
        return action

    def _create_actions(self) -> None:
        sp = QStyle.StandardPixmap
        a = self._action
        self.act_open = a(
            "&Open…", self.open_dialog, QKeySequence.StandardKey.Open, sp.SP_DialogOpenButton, False
        )
        self.act_close = a(
            "&Close", self.close_current, QKeySequence.StandardKey.Close, sp.SP_DialogCloseButton
        )
        self.act_properties = a(
            "Document &Properties…", self.show_properties, "Ctrl+D", sp.SP_FileDialogInfoView
        )
        self.act_save = a("&Save", self.save, QKeySequence.StandardKey.Save, sp.SP_DialogSaveButton)
        self.act_save_as = a("Save &As…", self.save_as, QKeySequence.StandardKey.SaveAs)
        self.act_undo = a("&Undo", self.undo, QKeySequence.StandardKey.Undo, sp.SP_ArrowBack)
        self.act_redo = a("&Redo", self.redo, QKeySequence.StandardKey.Redo, sp.SP_ArrowForward)
        self.act_redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.act_prefs = a("Pre&ferences…", self.show_preferences, "Ctrl+K", None, False)
        self.act_print = a(
            "&Print…", self.print_document, QKeySequence.StandardKey.Print, sp.SP_FileIcon
        )
        # Copy/Select All only apply while the page view has focus (line edits keep theirs).
        self.act_copy = a(
            "&Copy",
            self.copy_selection,
            QKeySequence.StandardKey.Copy,
            None,
            True,
            False,
            self.tabs,
        )
        self.act_select_all = a(
            "Select &All",
            lambda: self._with_view(DocumentView.select_all),
            QKeySequence.StandardKey.SelectAll,
            None,
            True,
            False,
            self.tabs,
        )
        self.act_find = a("&Find…", self.show_find, QKeySequence.StandardKey.Find)
        self.act_find_next = a(
            "Find &Next", self.search_panel.next_hit, QKeySequence.StandardKey.FindNext
        )
        self.act_find_prev = a(
            "Find Pre&vious", self.search_panel.previous_hit, QKeySequence.StandardKey.FindPrevious
        )
        self.tool_group = QActionGroup(self)
        self.tool_actions: dict[str, QAction] = {}
        for name, text, key in (("select", "&Select Tool", "V"), ("hand", "&Hand Tool", "H")):
            act = QAction(text, self, checkable=True)
            act.setShortcut(QKeySequence(key))
            act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            act.setProperty("needs_doc", True)
            act.triggered.connect(lambda _=False, n=name: self.tool_clicked(n))
            self.tabs.addAction(act)
            self.tool_group.addAction(act)
            self.tool_actions[name] = act
        sp_note = QStyle.StandardPixmap
        for name, text, icon in COMMENT_TOOLS:
            act = QAction(text, self, checkable=True)
            if icon is not None:
                act.setIcon(self.style().standardIcon(getattr(sp_note, icon)))
            act.setProperty("needs_doc", True)
            act.triggered.connect(lambda _=False, n=name: self.tool_clicked(n))
            self.tool_group.addAction(act)
            self.tool_actions[name] = act
        self.stamp_name = STANDARD_STAMPS[0]
        self.stamp_menu = QMenu("Stamp", self)
        for stamp in STANDARD_STAMPS:
            choose = functools.partial(self.choose_stamp, stamp)
            self.stamp_menu.addAction(_split_camel(stamp), choose)
        self.tool_actions["stamp"].setMenu(self.stamp_menu)
        self.current_tool = self.prefs.default_tool
        self.tool_actions[self.current_tool].setChecked(True)
        self.act_paste = a(
            "&Paste Comments",
            self.paste_annotations,
            QKeySequence.StandardKey.Paste,
            None,
            True,
            False,
            self.tabs,
        )
        self.act_delete_annots = a(
            "&Delete Comment", self.delete_annotations, None, sp_note.SP_TrashIcon
        )
        self.act_flatten = a("&Flatten All Comments…", self.flatten_all)
        self.act_show_comments = a("Comments &List", self.show_comments, "Ctrl+Shift+C")
        self.act_quit = a("E&xit", self.close, QKeySequence.StandardKey.Quit, None, False)
        self.act_zoom_in = a(
            "Zoom &In",
            lambda: self._with_view(DocumentView.zoom_in),
            QKeySequence.StandardKey.ZoomIn,
        )
        self.act_zoom_out = a(
            "Zoom &Out",
            lambda: self._with_view(DocumentView.zoom_out),
            QKeySequence.StandardKey.ZoomOut,
        )
        self.act_actual = a(
            "&Actual Size", lambda: self._with_view(lambda v: v.set_zoom(1.0)), "Ctrl+1"
        )
        self.act_fit_page = a("Fit &Page", lambda: self._with_view(DocumentView.fit_page), "Ctrl+0")
        self.act_fit_width = a(
            "Fit &Width", lambda: self._with_view(DocumentView.fit_width), "Ctrl+2"
        )
        self.act_rotate_cw = a(
            "Rotate View &Clockwise",
            lambda: self._with_view(lambda v: v.rotate_view(90)),
            "Ctrl+Shift+=",
            sp.SP_BrowserReload,
        )
        self.act_rotate_ccw = a(
            "Rotate View Counterc&lockwise",
            lambda: self._with_view(lambda v: v.rotate_view(-90)),
            "Ctrl+Shift+-",
        )
        self.act_night = a("&Night Reading", self._toggle_night, "Ctrl+Alt+N", None, True, True)
        self.act_nav_pane = self.nav_dock.toggleViewAction()
        self.act_nav_pane.setText("&Navigation Pane")
        self.act_nav_pane.setShortcut(QKeySequence("F4"))
        self.act_first = a("&First Page", self.first_page, "Home")
        self.act_prev = a("&Previous Page", self.previous_page, None, sp.SP_ArrowUp)
        self.act_next = a("&Next Page", self.next_page, None, sp.SP_ArrowDown)
        self.act_last = a("&Last Page", self.last_page, "End")
        self.act_goto = a("&Go to Page…", self.go_to_page_dialog, "Ctrl+Shift+N")
        self.act_back = a(
            "&Back", lambda: self._with_view(DocumentView.go_back), "Alt+Left", sp.SP_ArrowBack
        )
        self.act_forward = a(
            "F&orward",
            lambda: self._with_view(DocumentView.go_forward),
            "Alt+Right",
            sp.SP_ArrowForward,
        )
        self.act_next_tab = a("Next Document", lambda: self._cycle_tab(1), "Ctrl+Tab")
        self.act_prev_tab = a("Previous Document", lambda: self._cycle_tab(-1), "Ctrl+Shift+Tab")
        self.act_about = a("&About pdfeditor", self.show_about, None, None, False)
        # Home/End belong to the view (it handles them itself); keep them out of window shortcuts.
        self.act_first.setShortcut(QKeySequence())
        self.act_last.setShortcut(QKeySequence())

        self.layout_group = QActionGroup(self)
        self.layout_actions: dict[LayoutMode, QAction] = {}
        for mode, text in (
            (LayoutMode.SINGLE, "&Single Page"),
            (LayoutMode.CONTINUOUS, "Single Page &Continuous"),
            (LayoutMode.TWO_UP, "&Two-Up Continuous"),
            (LayoutMode.TWO_UP_COVER, "Two-Up with &Cover Page"),
        ):
            act = QAction(text, self, checkable=True)
            act.setData(mode)
            act.setProperty("needs_doc", True)
            act.triggered.connect(
                lambda _=False, m=mode: self._with_view(lambda v: v.set_layout_mode(m))
            )
            self.layout_group.addAction(act)
            self.layout_actions[mode] = act

        self.theme_group = QActionGroup(self)
        self.theme_actions: dict[Theme, QAction] = {}
        for theme, text in (
            (Theme.SYSTEM, "&System"),
            (Theme.LIGHT, "&Light"),
            (Theme.DARK, "&Dark"),
        ):
            act = QAction(text, self, checkable=True)
            act.triggered.connect(lambda _=False, t=theme: self.set_theme(t))
            self.theme_group.addAction(act)
            self.theme_actions[theme] = act

    def _create_menus(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")
        file_menu.addAction(self.act_open)
        file_menu.addAction(self.organize.act_combine)
        file_menu.addAction(self.export.act_from_office)
        self.recent_menu = file_menu.addMenu("Open &Recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        file_menu.addAction(self.act_close)
        file_menu.addAction(self.act_save)
        file_menu.addAction(self.act_save_as)
        self.export.fill_menu(file_menu)
        file_menu.addAction(self.optimize.act_reduce)
        file_menu.addSeparator()
        file_menu.addAction(self.act_properties)
        file_menu.addAction(self.act_print)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        edit_menu = mb.addMenu("&Edit")
        edit_menu.addAction(self.act_undo)
        edit_menu.addAction(self.act_redo)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_copy)
        edit_menu.addAction(self.act_select_all)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_find)
        edit_menu.addAction(self.act_find_next)
        edit_menu.addAction(self.act_find_prev)
        edit_menu.addSeparator()
        for act in self.tool_actions.values():
            edit_menu.addAction(act)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_prefs)

        view_menu = mb.addMenu("&View")
        for act in (
            self.act_zoom_in,
            self.act_zoom_out,
            self.act_actual,
            self.act_fit_page,
            self.act_fit_width,
        ):
            view_menu.addAction(act)
        view_menu.addSeparator()
        layout_menu = view_menu.addMenu("Page &Display")
        for act in self.layout_actions.values():
            layout_menu.addAction(act)
        view_menu.addAction(self.act_rotate_cw)
        view_menu.addAction(self.act_rotate_ccw)
        view_menu.addSeparator()
        view_menu.addAction(self.act_night)
        theme_menu = view_menu.addMenu("&Theme")
        for act in self.theme_actions.values():
            theme_menu.addAction(act)
        view_menu.addAction(self.act_nav_pane)
        self.act_inspector = self.inspector_dock.toggleViewAction()
        self.act_inspector.setText("&Properties Panel")
        self.act_inspector.setShortcut(QKeySequence("Ctrl+E"))
        view_menu.addAction(self.act_inspector)

        pages_menu = mb.addMenu("&Pages")
        for action in self.organize.menu_actions():
            if action is None:
                pages_menu.addSeparator()
            else:
                pages_menu.addAction(action)

        go_menu = mb.addMenu("&Go")
        for act in (self.act_first, self.act_prev, self.act_next, self.act_last, self.act_goto):
            go_menu.addAction(act)
        go_menu.addSeparator()
        go_menu.addAction(self.act_back)
        go_menu.addAction(self.act_forward)
        go_menu.addSeparator()
        go_menu.addAction(self.act_next_tab)
        go_menu.addAction(self.act_prev_tab)

        tools_menu = mb.addMenu("&Tools")
        tools_menu.addAction(self.tools.act_ocr)
        tools_menu.addAction(self.tools.act_batch_ocr)
        tools_menu.addSeparator()
        tools_menu.addAction(self.optimize.act_reduce)
        tools_menu.addAction(self.optimize.act_audit)
        tools_menu.addSeparator()
        tools_menu.addAction(self.compare.act_compare)
        tools_menu.addSeparator()
        tools_menu.addAction(self.pdfa.act_preflight)
        tools_menu.addAction(self.pdfa.act_save_pdfa)
        tools_menu.addAction(self.pdfa.act_accessibility)

        help_menu = mb.addMenu("&Help")
        help_menu.addAction(self.act_about)

    def _create_ribbon(self) -> None:
        home = self.ribbon.add_tab("Home")
        home.add_group(self.act_open, self.act_save, self.act_print, self.act_properties)
        home.add_group(self.act_undo, self.act_redo)
        home.add_group(self.tool_actions["select"], self.tool_actions["hand"])
        home.add_group(self.act_find)
        home.add_group(self.act_back, self.act_forward)
        home.add_group(self.act_prev, self.act_next)
        self.tool_actions["select"].setIconText("Select")
        self.tool_actions["hand"].setIconText("Hand")
        self.ribbon.set_quick_actions(
            [
                self.tool_actions["select"],
                self.tool_actions["hand"],
                self.act_undo,
                self.act_redo,
                self.act_save,
            ]
        )
        self.edit.ribbon()
        self.organize.ribbon()
        self.protect.ribbon()
        comment = self.ribbon.add_tab("Comment")
        comment.add_group(
            *(self.tool_actions[n] for n in ("highlight", "underline", "strikeout", "squiggly"))
        )
        comment.add_group(*(self.tool_actions[n] for n in ("note", "textbox", "stamp", "attach")))
        comment.add_group(
            *(
                self.tool_actions[n]
                for n in ("rectangle", "oval", "line", "arrow", "polygon", "polyline", "pen")
            )
        )
        comment.add_group(self.act_show_comments, self.act_delete_annots, self.act_flatten)
        view = self.ribbon.add_tab("View")
        view.add_group(
            self.act_zoom_out,
            self.act_zoom_in,
            self.act_fit_width,
            self.act_fit_page,
            self.act_actual,
        )
        view.add_group(*self.layout_actions.values())
        view.add_group(self.act_rotate_ccw, self.act_rotate_cw, self.act_night)
        self.tools.ribbon()
        self.export.ribbon()
        self.optimize.ribbon()
        self.compare.ribbon()
        self.pdfa.ribbon()

    # -- documents ------------------------------------------------------------------------
    def document_tabs(self) -> list[DocumentTab]:
        return [
            w for i in range(self.tabs.count()) if isinstance(w := self.tabs.widget(i), DocumentTab)
        ]

    def views(self) -> list[DocumentView]:
        return [t.view for t in self.document_tabs()]

    def current_tab(self) -> DocumentTab | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, DocumentTab) else None

    def _show_tag(self, ref: int) -> None:
        self.nav_dock.show()
        self.nav_tabs.setCurrentWidget(self.tags_panel)
        self.tags_panel.show_tag(ref)

    def accessibility_check(self) -> None:
        self.nav_dock.show()
        self.nav_tabs.setCurrentWidget(self.accessibility_panel)
        self.accessibility_panel.run()

    def current_view(self) -> DocumentView | None:
        tab = self.current_tab()
        return tab.view if tab is not None else None

    def _tab_index(self, view: DocumentView) -> int:
        return next((i for i, t in enumerate(self.document_tabs()) if t.view is view), -1)

    def engine(self) -> Engine:
        return get_engine()

    def add_document(self, doc: Document, name: str) -> DocumentView:
        """Open an in-memory document (combined, extracted...) as a new unsaved tab."""
        session = DocumentSession(doc, self.engine())
        session.name_hint = name
        session.undo_stack.mark_dirty()
        return self._add_session(session)

    def _with_view(self, fn: Callable[[DocumentView], object]) -> None:
        view = self.current_view()
        if view is not None:
            fn(view)

    def open_dialog(self) -> None:
        start = str(Path(self._recent_files()[0]).parent) if self._recent_files() else ""
        paths, _ = QFileDialog.getOpenFileNames(self, "Open PDF", start, PDF_FILTER)
        for p in paths:
            self.open_path(Path(p))

    def open_path(self, path: Path) -> DocumentView | None:
        path = path.resolve()
        for i, view in enumerate(self.views()):
            if view.session.path == path:
                self.tabs.setCurrentIndex(i)
                return view
        try:
            session = DocumentSession.open(path, password_prompt(self, path.name))
        except PasswordRequired:
            return None  # user cancelled the password prompt
        except OpenError as exc:
            QMessageBox.warning(self, "Open", f"Couldn't open “{path.name}”:\n\n{exc}")
            self._forget_recent(path)
            return None
        self._add_recent(path)
        return self._add_session(session)

    def _add_session(self, session: DocumentSession) -> DocumentView:
        session.undo_stack.max_disk_bytes = self.prefs.undo_disk_mb * 1024 * 1024
        view = DocumentView(session, self.renderer)
        view.current_page_changed.connect(lambda _p: self._update_ui())
        view.zoom_changed.connect(
            lambda z: self.zoom_box.show_zoom(z) if view is self.current_view() else None
        )
        view.history_changed.connect(self._update_ui)
        view.link_activated.connect(self.open_external_link)
        view.selection_changed.connect(self._update_ui)
        view.annotation_selection_changed.connect(self._update_ui)
        view.annotation_activated.connect(self.edit_annotation)
        view.annotation_context_menu.connect(self.annotation_menu)
        view.escape_pressed.connect(lambda: self.set_tool("select"))
        view.back_to_select.connect(lambda: self.set_tool("select"))
        view.tool_used.connect(self.tool_used)
        view.note_clicked.connect(self.open_note)
        view.object_selection_changed.connect(self._update_ui)
        view.author = self.prefs.author
        view.set_tool(self._make_tool(self.current_tool))
        session.subscribe(lambda event: self._on_session_event(view, event))
        index = self.tabs.addTab(DocumentTab(view), session.display_name)
        target = session.save_target()
        self.tabs.setTabToolTip(index, str(target) if target else session.display_name)
        self.tabs.setCurrentIndex(index)
        zoom = self.prefs.default_zoom
        if zoom == "fit_page":
            view.fit_page()
        elif zoom == "100":
            view.set_zoom(1.0)
        self._update_tab_title(view)
        view.setFocus()
        return view

    def _on_session_event(self, view: DocumentView, event: SessionEvent) -> None:
        if event.kind in (EventKind.DIRTY, EventKind.SAVED):
            self._update_tab_title(view)
            if view is self.current_view():
                self._update_ui()
        if event.kind is EventKind.SAVED:
            self.autosaver.forget(view.session)

    def _update_tab_title(self, view: DocumentView) -> None:
        index = self._tab_index(view)
        if index >= 0:
            mark = "*" if view.session.is_dirty else ""
            self.tabs.setTabText(index, view.session.display_name + mark)

    def close_tab(self, index: int, ask: bool = True) -> bool:
        """Close a tab; with unsaved changes, ask first. Returns False if the user cancelled."""
        tab = self.tabs.widget(index)
        if not isinstance(tab, DocumentTab):
            return True
        view = tab.view
        if ask and not self._confirm_discard(view):
            return False
        self.tabs.removeTab(index)
        tab.close_tab()
        self.autosaver.forget(view.session)  # saved or deliberately discarded
        view.close_view()
        view.session.close()
        tab.deleteLater()
        self._update_ui()
        return True

    def close_current(self) -> None:
        if self.tabs.currentIndex() >= 0:
            self.close_tab(self.tabs.currentIndex())

    def ask_save_changes(self, session: DocumentSession) -> QMessageBox.StandardButton:
        """Save / Discard / Cancel prompt (separate so tests can answer it)."""
        return QMessageBox.question(
            self,
            "Unsaved Changes",
            f"Save changes to “{session.display_name}” before closing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )

    def _confirm_discard(self, view: DocumentView) -> bool:
        if not view.session.is_dirty:
            return True
        self.tabs.setCurrentIndex(self._tab_index(view))
        answer = self.ask_save_changes(view.session)
        if answer == QMessageBox.StandardButton.Save:
            return self.save(view)
        return answer == QMessageBox.StandardButton.Discard

    # -- saving & undo --------------------------------------------------------------------
    def save(self, view: DocumentView | None = None) -> bool:
        view = view or self.current_view()
        if view is None:
            return False
        if view.session.save_target() is None:
            return self.save_as(view)
        return self._save_to(view, None)

    def save_as(self, view: DocumentView | None = None) -> bool:
        view = view or self.current_view()
        if view is None:
            return False
        target = view.session.save_target()
        start = str(target) if target else view.session.display_name + ".pdf"
        chosen = self.choose_save_path(start)
        if chosen is None:
            return False
        return self._save_to(view, chosen)

    def choose_save_path(self, start: str) -> Path | None:
        """Save-as file dialog (separate so tests can answer it)."""
        chosen, _ = QFileDialog.getSaveFileName(self, "Save As", start, PDF_FILTER)
        if not chosen:
            return None
        path = Path(chosen)
        return path if path.suffix.lower() == ".pdf" else path.with_suffix(".pdf")

    def _save_to(self, view: DocumentView, path: Path | None) -> bool:
        try:
            saved = view.session.save(path)
        except (SaveError, OSError) as exc:
            QMessageBox.warning(
                self, "Save", f"Couldn't save “{view.session.display_name}”:\n\n{exc}"
            )
            return False
        self._add_recent(saved)
        index = self._tab_index(view)
        self.tabs.setTabToolTip(index, str(saved))
        self._update_tab_title(view)
        self._update_ui()
        self.statusBar().showMessage(f"Saved {saved.name}", 3000)
        return True

    def undo(self) -> None:
        view = self.current_view()
        if view is not None:
            view.session.undo()
            self._update_ui()

    def redo(self) -> None:
        view = self.current_view()
        if view is not None:
            view.session.redo()
            self._update_ui()

    # -- autosave & recovery --------------------------------------------------------------
    def autosave_now(self) -> int:
        return self.autosaver.tick([v.session for v in self.views()])

    def _apply_prefs(self) -> None:
        minutes = self.prefs.autosave_minutes
        if minutes:
            self.autosave_timer.start(minutes * 60 * 1000)
        else:
            self.autosave_timer.stop()
        for view in self.views():
            view.session.undo_stack.max_disk_bytes = self.prefs.undo_disk_mb * 1024 * 1024
            view.author = self.prefs.author

    def show_preferences(self) -> None:
        if PreferencesDialog(self.prefs, self).exec():
            self._apply_prefs()

    def offer_recovery(self, dialog: RecoveryDialog | None = None) -> int:
        """After a crash, offer the recovery copies left behind. Returns documents reopened."""
        entries = self.recovery.entries()
        if not entries:
            return 0
        dialog = dialog or RecoveryDialog(entries, self)
        if dialog.result() == 0 and not dialog.isVisible():
            dialog.exec()
        reopened = 0
        for entry in dialog.selected:
            name = entry.display_name
            try:
                session = DocumentSession.open(entry.pdf.read_bytes(), password_prompt(self, name))
            except (OpenError, OSError) as exc:
                QMessageBox.warning(self, "Recover", f"Couldn't recover “{name}”:\n\n{exc}")
                continue
            session.save_path_hint = entry.original_path
            session.undo_stack.mark_dirty()
            self._add_session(session)
            self.recovery.discard(entry.uid)
            reopened += 1
        if dialog.discard_rest:
            for entry in entries:
                self.recovery.discard(entry.uid)
        return reopened

    def _cycle_tab(self, delta: int) -> None:
        if self.tabs.count():
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + delta) % self.tabs.count())

    def _on_tab_changed(self, _index: int) -> None:
        view = self.current_view()
        for panel in self.panels:
            panel.set_view(view)
        self._update_ui()

    def _update_ui(self) -> None:
        view = self.current_view()
        has_doc = view is not None
        self.tabs.setVisible(has_doc)
        self.welcome.setVisible(not has_doc)
        for action in self.actions() + self.layout_group.actions():
            if action.property("needs_doc"):
                action.setEnabled(has_doc)
        self.navigator.update_state(view)
        self.organize.update_state()
        self.edit.update_state(view)
        if view is not None:
            dirty = view.session.is_dirty
            self.setWindowTitle(f"{view.session.display_name}{'*' if dirty else ''} — pdfeditor")
            stack = view.session.undo_stack
            self.act_undo.setEnabled(stack.can_undo)
            self.act_undo.setText(f"&Undo {stack.undo_label}".rstrip())
            self.act_redo.setEnabled(stack.can_redo)
            self.act_redo.setText(f"&Redo {stack.redo_label}".rstrip())
            self.zoom_box.show_zoom(view.zoom)
            self.act_back.setEnabled(view.can_go_back)
            self.act_forward.setEnabled(view.can_go_forward)
            self.layout_actions[view.layout_mode].setChecked(True)
            self.act_night.blockSignals(True)
            self.act_night.setChecked(view.night_mode)
            self.act_night.blockSignals(False)
            self.act_copy.setEnabled(view.has_selection() or bool(view.selected_annotations))
            self.act_paste.setEnabled(bool(self._annotation_clipboard))
            self.act_delete_annots.setEnabled(bool(view.selected_annotations))
            self.act_find_next.setEnabled(bool(self.search_panel.hits))
            self.act_find_prev.setEnabled(bool(self.search_panel.hits))
            if view.tool.name in self.tool_actions:
                self.tool_actions[view.tool.name].setChecked(True)
            self.act_prev.setEnabled(view.current_page > 0)
            self.act_next.setEnabled(view.current_page < view.page_count - 1)
        else:
            self.setWindowTitle("pdfeditor")
            self.zoom_box.setEditText("")
        self.zoom_box.setEnabled(has_doc)

    # -- navigation slots -----------------------------------------------------------------
    def first_page(self) -> None:
        self._with_view(DocumentView.first_page)

    def last_page(self) -> None:
        self._with_view(DocumentView.last_page)

    def next_page(self) -> None:
        self._with_view(DocumentView.next_page)

    def previous_page(self) -> None:
        self._with_view(DocumentView.previous_page)

    def go_to_page_dialog(self) -> None:
        view = self.current_view()
        if view is None:
            return
        number, ok = QInputDialog.getInt(
            self,
            "Go to Page",
            f"Page number (1 to {view.page_count}):",
            view.current_page + 1,
            1,
            view.page_count,
        )
        if ok:
            view.go_to_page(number - 1)

    def open_external_link(self, link: Link) -> None:
        if link.kind is LinkKind.URI and link.uri:
            answer = QMessageBox.question(
                self, "Open Link", f"The document is trying to open:\n\n{link.uri}\n\nAllow it?"
            )
            if answer == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl(link.uri))
        elif link.kind is LinkKind.GOTO_REMOTE and link.file:
            view = self.current_view()
            base = view.session.path.parent if view and view.session.path else Path.cwd()
            target = (base / link.file).resolve()
            if target.suffix.lower() == ".pdf" and target.exists():
                new_view = self.open_path(target)
                if new_view is not None and link.dest is not None:
                    new_view.go_to_page(link.dest.page_index, link.dest.point)
        else:
            self.statusBar().showMessage("This kind of link isn't supported.", 4000)

    # -- text & tools ---------------------------------------------------------------------
    def copy_selection(self) -> None:
        view = self.current_view()
        if view is None:
            return
        if view.selected_annotations:
            self._annotation_clipboard = [copy.deepcopy(m) for m in view.selected_models()]
            self.statusBar().showMessage(
                f"Copied {len(self._annotation_clipboard)} comment(s).", 2000
            )
            self._update_ui()
        elif view.copy_selection():
            self.statusBar().showMessage("Copied to clipboard.", 2000)

    def paste_annotations(self) -> None:
        """Paste copied comments onto the current page, slightly offset."""
        view = self.current_view()
        if view is None or not self._annotation_clipboard:
            return
        page = view.current_page
        rect = view.page_rect(page)
        commands: list[Command] = []
        for original in self._annotation_clipboard:
            model = annotate_transform(original, Matrix.translate(12, 12))
            model.page_index = page
            model.name = ""
            model.id = None
            model.in_reply_to = None
            # keep pasted comments on the page even when it is smaller than the source
            b = model.rect
            if not b.intersects(rect):
                model = annotate_transform(
                    model, Matrix.translate(rect.x0 - b.x0 + 20, rect.y0 - b.y0 + 20)
                )
            commands.append(AddAnnotationCommand(model, "Paste Comment"))
        view.session.execute(
            commands[0] if len(commands) == 1 else MacroCommand("Paste Comments", commands)
        )
        added = [(page, c.model.name) for c in commands if isinstance(c, AddAnnotationCommand)]
        view.set_annotation_selection(added)

    def delete_annotations(self) -> None:
        view = self.current_view()
        if view is not None:
            view.delete_selected_annotations()

    def show_inspector(self) -> None:
        was_hidden = not self.inspector_dock.isVisible()
        self.inspector_dock.show()
        if was_hidden:
            self.resizeDocks([self.inspector_dock], [250], Qt.Orientation.Horizontal)

    def show_comments(self) -> None:
        self.nav_dock.show()
        self.nav_tabs.setCurrentWidget(self.comments_panel)

    def choose_stamp(self, stamp: str) -> None:
        self.stamp_name = stamp
        self.set_tool("stamp")  # a stamp picked from the menu always (re)starts the tool

    def edit_annotation(self, model: AnnotationModel) -> None:
        """Double-click: edit a comment's text (or save an attached file)."""
        view = self.current_view()
        if view is None:
            return
        if model.type is AnnotationType.FILE_ATTACHMENT and model.file_data is not None:
            chosen, _ = QFileDialog.getSaveFileName(self, "Save Attached File", model.file_name)
            if chosen:
                Path(chosen).write_bytes(model.file_data)
            return
        if model.locked:
            self.statusBar().showMessage("This comment is locked.", 3000)
            return
        text = annotate.ask_text(view, "Edit Comment", model.contents)
        if text is not None and text != model.contents:
            after = copy.deepcopy(model)
            after.contents = text
            view.session.execute(UpdateAnnotationCommand(model, after, "Edit Comment Text"))

    def annotation_menu(self, model: AnnotationModel, pos: QPoint) -> None:
        view = self.current_view()
        if view is None:
            return
        menu = QMenu(self)
        menu.addAction("Edit Text…", lambda: self.edit_annotation(model)).setEnabled(
            not model.locked
        )
        menu.addAction("Reply…", lambda: self._reply(view, model))
        status = menu.addMenu("Set Status")
        for state in ReviewState:
            if state is not ReviewState.NONE:
                status.addAction(state.value, functools.partial(add_reply, view, model, "", state))
        menu.addAction("Properties", self.show_inspector)
        menu.addSeparator()
        menu.addAction(
            "Bring to Front",
            lambda: view.session.execute(
                ReorderAnnotationCommand(model.page_index, model.name, True)
            ),
        )
        menu.addAction(
            "Send to Back",
            lambda: view.session.execute(
                ReorderAnnotationCommand(model.page_index, model.name, False)
            ),
        )
        menu.addSeparator()
        menu.addAction("Flatten", self.flatten_selected)
        menu.addAction("Delete", view.delete_selected_annotations).setEnabled(not model.locked)
        menu.exec(pos)

    def _reply(self, view: DocumentView, model: AnnotationModel) -> None:
        text = annotate.ask_text(view, "Reply")
        if text:
            add_reply(view, model, text)

    def flatten_selected(self) -> int:
        """Burn only the selected comments into their pages (one undo step)."""
        view = self.current_view()
        if view is None or not view.session.engine.capabilities.annotations_flatten:
            return 0
        by_page: dict[int, list[int]] = {}
        for model in view.selected_models():
            if model.id is not None:
                by_page.setdefault(model.page_index, []).append(model.id)
        if not by_page:
            return 0
        counted: list[int] = []

        def operation(doc: Document) -> None:
            counted.append(sum(doc.page(p).flatten_annotations(ids) for p, ids in by_page.items()))

        view.set_annotation_selection([])
        session = view.session
        session.execute(SnapshotCommand("Flatten Comment", operation, session.snapshots))
        return counted[0] if counted else 0

    def flatten_all(self, confirm: bool = True) -> int:
        """Burn every visible comment into the pages (one undo step)."""
        view = self.current_view()
        if view is None or not view.session.engine.capabilities.annotations_flatten:
            return 0
        if confirm:
            answer = QMessageBox.question(
                self,
                "Flatten Comments",
                "Make all comments part of the page content? They can't be edited as comments "
                "afterwards (you can still undo).",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return 0
        counted: list[int] = []

        def operation(doc: Document) -> None:
            counted.append(sum(doc.page(i).flatten_annotations() for i in range(doc.page_count)))

        session = view.session
        session.execute(SnapshotCommand("Flatten Comments", operation, session.snapshots))
        return counted[0] if counted else 0

    def show_find(self) -> None:
        view = self.current_view()
        if view is None:
            return
        self.nav_dock.show()
        self.nav_tabs.setCurrentWidget(self.search_panel)
        self.search_panel.focus_query(view.selected_text())

    def _make_tool(self, name: str) -> Tool:
        if name == "hand":
            tool: Tool = HandTool()
        elif name == "stamp":
            tool = annotate.StampTool(self.stamp_name)
        elif (edit_tool := make_edit_tool(name)) is not None:
            tool = edit_tool
        elif name == "redact":
            tool = RedactTool(self.protect)
        else:
            tool = annotate.tool_for(name) or SelectTool()
        tool.name = name
        return tool

    def tool_clicked(self, name: str) -> None:
        """A tool button was clicked: clicking the active tool again turns it off."""
        if name == self.current_tool and name != "select":
            self.set_tool("select")
        else:
            self.set_tool(name)

    def open_note(self, note: AnnotationModel, pos: QPoint) -> NotePopup | None:
        view = self.current_view()
        if view is None:
            return None
        if self._note_popup is not None:
            self._note_popup.close()
        self._note_popup = NotePopup(view, note, self)
        self._note_popup.show_at(pos + QPoint(12, 12))
        return self._note_popup

    def tool_used(self) -> None:
        """A creation tool finished its job: back to Select unless tools should stay."""
        if not self.prefs.keep_tools and self.current_tool not in STAY_ACTIVE:
            self.set_tool("select")

    def set_tool(self, name: str) -> None:
        view = self.current_view()
        markup = MARKUP_TOOLS.get(name)
        if markup is not None and view is not None and view.has_selection():
            # Acrobat-style: select text first, then pick Highlight, and it's applied at once.
            annotate.apply_markup(view, markup, self.tool_actions[name].text().replace("&", ""))
            self.tool_actions[self.current_tool].setChecked(True)
            return
        if name in EDIT_TOOL_NAMES and view is not None and not self.edit.confirm_signed(view):
            self.tool_actions[self.current_tool].setChecked(True)
            return
        self.current_tool = name
        for v in self.views():
            v.set_tool(self._make_tool(name))
        self.tool_actions[name].setChecked(True)
        self._update_tool_indicator()

    def _update_tool_indicator(self) -> None:
        name = self.current_tool
        active = name not in ("select", "hand")
        if active:
            text = (
                self.tool_actions[name]
                .text()
                .replace("&&", "\0")
                .replace("&", "")
                .replace("\0", "&")
            )
            self.tool_label.setText(f"Tool: {text}  (Esc: back to Select)")
        self.tool_label.setVisible(active)
        self.tool_exit.setVisible(active)

    def print_document(self) -> None:
        view = self.current_view()
        if view is not None:
            PrintDialog(view, self).exec()

    def _toggle_night(self, on: bool) -> None:
        self._with_view(lambda v: v.set_night_mode(on))

    # -- dialogs --------------------------------------------------------------------------
    def show_properties(self) -> None:
        view = self.current_view()
        if view is not None:
            PropertiesDialog(view.session, self).exec()
            self._update_ui()

    def show_about(self) -> None:
        engine_version = ""
        view = self.current_view()
        if view is not None:
            engine_version = getattr(view.session.engine, "version", "")
        QMessageBox.about(
            self,
            "About pdfeditor",
            f"<b>pdfeditor {__version__}</b><p>An open-source PDF editor.</p>"
            f"<p>{engine_version}</p><p>Licensed under the GNU AGPL v3 or later.</p>",
        )

    # -- theme ----------------------------------------------------------------------------
    def set_theme(self, theme: Theme) -> None:
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_theme(app, theme)
        self.theme_actions[theme].setChecked(True)
        self.settings.setValue(SETTINGS_THEME, theme.value)

    # -- recent files ---------------------------------------------------------------------
    def _recent_files(self) -> list[str]:
        value = self.settings.value(SETTINGS_RECENT, [])
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(v) for v in value]
        return []

    def _add_recent(self, path: Path) -> None:
        items = [p for p in self._recent_files() if Path(p) != path]
        items.insert(0, str(path))
        self.settings.setValue(SETTINGS_RECENT, items[:MAX_RECENT])

    def _forget_recent(self, path: Path) -> None:
        self.settings.setValue(
            SETTINGS_RECENT, [p for p in self._recent_files() if Path(p) != path]
        )

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        files = self._recent_files()
        for i, p in enumerate(files, 1):
            act = self.recent_menu.addAction(f"&{i % 10} {Path(p).name}")
            act.setToolTip(p)
            act.triggered.connect(lambda _=False, path=p: self.open_path(Path(path)))
        if not files:
            self.recent_menu.addAction("(empty)").setEnabled(False)
        else:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction(
                "Clear List", lambda: self.settings.setValue(SETTINGS_RECENT, [])
            )

    # -- settings & window events ---------------------------------------------------------
    def _restore_settings(self) -> None:
        geometry = self.settings.value(SETTINGS_GEOMETRY)
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = self.settings.value(SETTINGS_STATE)
        if state is not None:
            self._state_restored = bool(self.restoreState(state))
        try:
            theme = Theme(str(self.settings.value(SETTINGS_THEME, Theme.SYSTEM.value)))
        except ValueError:
            theme = Theme.SYSTEM
        self.set_theme(theme)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._state_restored:
            # Dock sizes only apply once the window has a size; do it on first show.
            self._state_restored = True
            self.resizeDocks([self.nav_dock], [210], Qt.Orientation.Horizontal)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if any(
            u.isLocalFile() and u.toLocalFile().lower().endswith(".pdf")
            for u in event.mimeData().urls()
        ):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                self.open_path(Path(url.toLocalFile()))
        event.acceptProposedAction()

    def open_forwarded(self, paths: list[str]) -> None:
        """Files handed over by a second launch (single-instance mode)."""
        for p in paths:
            self.open_path(Path(p))
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:
        for view in self.views():
            if not self._confirm_discard(view):
                event.ignore()
                return
        self.settings.setValue(SETTINGS_GEOMETRY, self.saveGeometry())
        self.settings.setValue(SETTINGS_STATE, self.saveState())
        self.autosave_timer.stop()
        while self.tabs.count():
            self.close_tab(0, ask=False)
        self.renderer.wait_idle(2000)
        super().closeEvent(event)
