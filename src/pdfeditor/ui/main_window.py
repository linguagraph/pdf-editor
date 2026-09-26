"""Main application window: tabbed documents, ribbon, navigation pane, status bar."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl
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
    QMessageBox,
    QStyle,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pdfeditor import __version__
from pdfeditor.core.layout import LayoutMode
from pdfeditor.core.render_cache import RenderCache
from pdfeditor.core.session import DocumentSession
from pdfeditor.engine.base import OpenError, PasswordRequired
from pdfeditor.model.outline import Link, LinkKind
from pdfeditor.ui.dialogs.password import password_prompt
from pdfeditor.ui.dialogs.print_dialog import PrintDialog
from pdfeditor.ui.dialogs.properties import PropertiesDialog
from pdfeditor.ui.panels.attachments import AttachmentsPanel
from pdfeditor.ui.panels.base import ViewPanel
from pdfeditor.ui.panels.bookmarks import BookmarksPanel
from pdfeditor.ui.panels.layers import LayersPanel
from pdfeditor.ui.panels.search import SearchPanel
from pdfeditor.ui.panels.thumbnails import ThumbnailsPanel
from pdfeditor.ui.ribbon import Ribbon
from pdfeditor.ui.theme import Theme, apply_theme
from pdfeditor.ui.tools.base import Tool
from pdfeditor.ui.tools.hand import HandTool
from pdfeditor.ui.tools.select import SelectTool
from pdfeditor.ui.view.document_view import DocumentView
from pdfeditor.ui.view.renderer import TileRenderer

log = logging.getLogger(__name__)

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
        self.renderer = TileRenderer(RenderCache[QImage](384 * 1024 * 1024), self)

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
        self.panels: list[ViewPanel] = [
            ThumbnailsPanel(),
            BookmarksPanel(),
            self.search_panel,
            AttachmentsPanel(),
            LayersPanel(),
        ]
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
        self._state_restored = False

        self.navigator = PageNavigator(self)
        self.zoom_box = ZoomBox(self)
        self.statusBar().addPermanentWidget(self.navigator)
        self.statusBar().addPermanentWidget(self.zoom_box)

        self._create_actions()
        self.search_panel.hits_changed.connect(self._update_ui)
        self._create_menus()
        self._create_ribbon()
        self._restore_settings()
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
            act.triggered.connect(lambda _=False, n=name: self.set_tool(n))
            self.tabs.addAction(act)
            self.tool_group.addAction(act)
            self.tool_actions[name] = act
        self.tool_actions["select"].setChecked(True)
        self.current_tool = "select"
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
        self.recent_menu = file_menu.addMenu("Open &Recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        file_menu.addAction(self.act_close)
        file_menu.addSeparator()
        file_menu.addAction(self.act_properties)
        file_menu.addAction(self.act_print)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        edit_menu = mb.addMenu("&Edit")
        edit_menu.addAction(self.act_copy)
        edit_menu.addAction(self.act_select_all)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_find)
        edit_menu.addAction(self.act_find_next)
        edit_menu.addAction(self.act_find_prev)
        edit_menu.addSeparator()
        for act in self.tool_actions.values():
            edit_menu.addAction(act)

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

        go_menu = mb.addMenu("&Go")
        for act in (self.act_first, self.act_prev, self.act_next, self.act_last, self.act_goto):
            go_menu.addAction(act)
        go_menu.addSeparator()
        go_menu.addAction(self.act_back)
        go_menu.addAction(self.act_forward)
        go_menu.addSeparator()
        go_menu.addAction(self.act_next_tab)
        go_menu.addAction(self.act_prev_tab)

        help_menu = mb.addMenu("&Help")
        help_menu.addAction(self.act_about)

    def _create_ribbon(self) -> None:
        home = self.ribbon.add_tab("Home")
        home.add_group(self.act_open, self.act_print, self.act_properties)
        home.add_group(*self.tool_actions.values())
        home.add_group(self.act_find)
        home.add_group(self.act_back, self.act_forward)
        home.add_group(self.act_prev, self.act_next)
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

    # -- documents ------------------------------------------------------------------------
    def views(self) -> list[DocumentView]:
        return [
            w
            for i in range(self.tabs.count())
            if isinstance(w := self.tabs.widget(i), DocumentView)
        ]

    def current_view(self) -> DocumentView | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, DocumentView) else None

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
        view = DocumentView(session, self.renderer)
        view.current_page_changed.connect(lambda _p: self._update_ui())
        view.zoom_changed.connect(
            lambda z: self.zoom_box.show_zoom(z) if view is self.current_view() else None
        )
        view.history_changed.connect(self._update_ui)
        view.link_activated.connect(self.open_external_link)
        view.selection_changed.connect(self._update_ui)
        view.set_tool(self._make_tool(self.current_tool))
        index = self.tabs.addTab(view, session.display_name)
        self.tabs.setTabToolTip(index, str(path))
        self.tabs.setCurrentIndex(index)
        view.setFocus()
        return view

    def close_tab(self, index: int) -> None:
        view = self.tabs.widget(index)
        if not isinstance(view, DocumentView):
            return
        self.tabs.removeTab(index)
        view.close_view()
        view.session.close()
        view.deleteLater()
        self._update_ui()

    def close_current(self) -> None:
        if self.tabs.currentIndex() >= 0:
            self.close_tab(self.tabs.currentIndex())

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
        if view is not None:
            self.setWindowTitle(f"{view.session.display_name} — pdfeditor")
            self.zoom_box.show_zoom(view.zoom)
            self.act_back.setEnabled(view.can_go_back)
            self.act_forward.setEnabled(view.can_go_forward)
            self.layout_actions[view.layout_mode].setChecked(True)
            self.act_night.blockSignals(True)
            self.act_night.setChecked(view.night_mode)
            self.act_night.blockSignals(False)
            self.act_copy.setEnabled(view.has_selection())
            self.act_find_next.setEnabled(bool(self.search_panel.hits))
            self.act_find_prev.setEnabled(bool(self.search_panel.hits))
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
        if view is not None and view.copy_selection():
            self.statusBar().showMessage("Copied to clipboard.", 2000)

    def show_find(self) -> None:
        view = self.current_view()
        if view is None:
            return
        self.nav_dock.show()
        self.nav_tabs.setCurrentWidget(self.search_panel)
        self.search_panel.focus_query(view.selected_text())

    @staticmethod
    def _make_tool(name: str) -> Tool:
        return HandTool() if name == "hand" else SelectTool()

    def set_tool(self, name: str) -> None:
        self.current_tool = name
        for view in self.views():
            view.set_tool(self._make_tool(name))
        self.tool_actions[name].setChecked(True)

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
        self.settings.setValue(SETTINGS_GEOMETRY, self.saveGeometry())
        self.settings.setValue(SETTINGS_STATE, self.saveState())
        while self.tabs.count():
            self.close_tab(0)
        self.renderer.wait_idle(2000)
        super().closeEvent(event)
