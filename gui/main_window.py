"""Main profiler window — flame chart, scrolling, menus, all features."""

import json
import os
import pathlib
import shutil
import sys

_RECENT_MAX = 8

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from trace_io import export_csv, export_json, export_sltrace

from .axes import DepthAxis, UnitAxis
from .bookmark_dock import BookmarkDock
from .call_graph_dock import CallGraphDock
from .call_tree_dock import CallTreeDock
from .color_bar import ColorBarWidget
from .constants import COLORS, DEFAULT_WINDOW_US, PALETTES, ROW_HEIGHT, build_dark_stylesheet, build_stylesheet
from .cursor_handle import CursorHandle
from .dock_title_bar import DockTitleBar
from .event_filters import _QuestionKeyFilter, _ZoomKeyFilter
from .flame_item import FlameItem
from .jitter_dialog import JitterDialog
from .marker_dock import MarkerDock
from .minimap_widget import MinimapWidget
from .ribbon_dock import RibbonDock
from .settings_dialog import SettingsDialog
from .shortcut_overlay import ShortcutOverlay
from .summary_dock import SummaryDock
from .theme import (
    THEME, THEMES, CANVAS_DIM_RGBA, Z_PICK, Z_SELECTION, Z_HIGHLIGHT,
    SELECTION_FILL_ALPHA, configure_pyqtgraph, spinbox_qss, _ICONS,
    apply_theme,
)
from .toast_widget import ToastWidget
from .top_n_dock import TopNSlowestDock


# ── Main window ──────────────────────────────────────────────────────

class ProfilerWindow(QtWidgets.QMainWindow):
    """Flame-chart window with full navigation + filter + export + refresh."""

    def __init__(
        self,
        spans,
        marks=None,
        pause_regions=None,
        wrapped=False,
        refresh_fn=None,
        elf_path=None,
        cpu_mhz=96.0,
        parent=None,
    ):
        super().__init__(parent)
        self.spans = spans
        self.marks = list(marks or [])
        self.pause_regions = list(pause_regions or [])
        self.wrapped = bool(wrapped)
        self.refresh_fn = refresh_fn
        self.elf_path = elf_path
        self.cpu_mhz = cpu_mhz
        self._jlink = None  # live J-Link handle; closed on reconnect or window close

        self.setWindowTitle("Stack Lens")
        self.resize(1500, 900)

        # Data extents (always stored internally in microseconds)
        self._compute_extents()

        # Color map per function name — preserved across refreshes so colors stay stable
        self.color_map = {}

        # Display unit: "us" → 1.0, "ms" → 0.001. Default to ms.
        self.unit_label = "ms"
        self.unit_scale = 0.001

        # Current visible window
        self.window_us = min(DEFAULT_WINDOW_US, self.total_us) if self.total_us > 0 else 1.0
        self.view_start = 0.0

        # Span-picker state
        self._pick_mode = False
        self._picked_a = None
        self._picked_b = None
        self._pick_overlay_a = None
        self._pick_overlay_b = None
        self._pick_label_a = None
        self._pick_label_b = None

        # Zoom-to-selection state
        self._select_mode = False
        self._shift_drag_active = False  # Shift+drag mode-less region zoom
        self._select_start_x = None
        self._select_overlay = None

        # Bookmark pick mode
        self._bookmark_pick_mode = False

        # Iterator cursor for "jump to next occurrence" — tracks the last
        # function/marker the user clicked and the current index into its
        # occurrence list. Shared by summary dock, Find combobox, and marker
        # dock so any of them can advance the same cursor.
        self._iter_cursor = {"kind": None, "name": None, "index": 0}
        self._iter_highlight_item = None       # QGraphicsRectItem currently glowing
        self._iter_highlight_timer = None      # QTimer that removes it

        # Highlight-on-hover state (off by default)
        self.highlight_hover_enabled = False

        # Color mode
        self._color_mode = "function"

        # Chart display settings (defaults; overridden below / by _restore_session)
        self._row_height_setting: float = 0.85
        self._font_size_setting:  int   = 8
        self._palette_name:       str   = "Default"
        self._ts_decimals:        int   = 3
        self._bookmark_snap:      bool  = False
        self._exclude_isr_time:   bool  = False

        # Apply persisted theme/palette *before* building anything, so the
        # UI is constructed once with the user's actual settings instead of
        # being built with hardcoded defaults and then immediately rebuilt.
        # (Widget-dependent restores — dock geometry, view toggles, etc. —
        # still happen post-build in _restore_session().)
        s = self._load_settings()
        saved_theme = s.get("theme", "Dark")
        if saved_theme in THEMES:
            apply_theme(saved_theme)
        self._palette_name = s.get("palette", "Default")
        if self._palette_name != "Default":
            self.color_map = self._compute_palette_color_map(self._palette_name)
        else:
            self._update_color_map()

        self._build_ui()
        self._toast = ToastWidget(self)
        self._populate_plot()
        self._update_view_range()
        self._update_overflow_banner()

        # Make sure keyboard focus lands on the plot (not the toolbar's
        # Window spinbox) when the window first appears, so Home/End/F3 etc.
        # work without clicking on the chart first.
        self.plot_widget.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.plot_widget.setFocus()
        self._initial_focus_done = False

        # Keyboard pan/zoom shortcuts. Use QShortcut with WindowShortcut context
        # so they fire regardless of which child widget currently has focus
        # (the plot widget's QGraphicsView would otherwise swallow arrow keys,
        # and some child widgets consume +/- too).
        self._install_pan_zoom_shortcuts()

        # Restore remaining session state (unit/docks/visibility). Theme and
        # palette were already applied above, before the UI was built.
        self._restore_session(s)

        from .file_association import register_sltrace_association as _reg
        _reg(os.path.join(os.path.dirname(__file__), "icons", "icon.ico"))

    def closeEvent(self, event):
        self._cleanup_temp_elf()
        self._save_session()
        if self._jlink is not None:
            try:
                self._jlink.close()
            except Exception:
                pass
            self._jlink = None
        super().closeEvent(event)

    # ── Internal setup helpers ───────────────────────────────────────

    def _compute_extents(self):
        mins = []
        maxs = []
        if self.spans:
            mins.append(min(sp["start_us"] for sp in self.spans))
            maxs.append(max(sp["end_us"] for sp in self.spans))
        if self.marks:
            mins.append(min(m["t_us"] for m in self.marks))
            maxs.append(max(m["t_us"] for m in self.marks))

        if mins:
            self.t_min = min(mins)
            self.t_max = max(maxs)
        else:
            self.t_min, self.t_max = 0.0, 1.0

        if self.spans:
            self.max_depth = max(sp["depth"] for sp in self.spans)
        else:
            self.max_depth = 0

        # Display-y bounds: thread spans occupy 0..max_depth,
        # ISR spans occupy -1..-(isr_max_depth+1)
        isr_max = 0
        has_isr = False
        for sp in self.spans:
            if sp.get("ipsr", 0) != 0:
                has_isr = True
                if sp["depth"] > isr_max:
                    isr_max = sp["depth"]
        self.has_isr = has_isr
        self.isr_max_depth = isr_max
        # min_display_y: most-negative (top of chart under invertY)
        self.min_display_y = -(isr_max + 1) if has_isr else 0
        self.max_display_y = self.max_depth

        self.total_us = self.t_max - self.t_min
        self.func_names = sorted(set(sp["name"] for sp in self.spans))

    def _update_color_map(self):
        """Add stable colors for any new function names (keep existing)."""
        used = set(self.color_map.values())
        for i, name in enumerate(self.func_names):
            if name not in self.color_map:
                # Pick the first palette slot not already used
                for j in range(len(COLORS)):
                    candidate = COLORS[(i + j) % len(COLORS)]
                    if candidate not in used:
                        self.color_map[name] = candidate
                        used.add(candidate)
                        break
                else:
                    self.color_map[name] = COLORS[i % len(COLORS)]

    def _compute_palette_color_map(self, palette_name: str) -> dict:
        """Assign every known function a stable color from the named palette."""
        palette_colors = PALETTES.get(palette_name, COLORS)
        color_map: dict = {}
        used: set[str] = set()
        for i, name in enumerate(self.func_names):
            for j in range(len(palette_colors)):
                candidate = palette_colors[(i + j) % len(palette_colors)]
                if candidate not in used:
                    color_map[name] = candidate
                    used.add(candidate)
                    break
            else:
                color_map[name] = palette_colors[i % len(palette_colors)]
        return color_map

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self):
        QtWidgets.QApplication.instance().setStyleSheet(build_dark_stylesheet())

        # Create the shortcut cheat-sheet overlay + its trigger action
        # BEFORE _build_menu so the Help menu entry can reference it.
        #
        # Important: use a SINGLE QKeySequence for "?" — Qt canonicalises
        # both "?" and "Shift+/" to the same internal key, so passing
        # both to setShortcuts registers the same binding twice and Qt
        # then reports every press as an ambiguous overload and fires
        # nothing. One entry is enough to catch the key on every layout.
        self._shortcut_overlay = ShortcutOverlay(self)
        self._act_show_shortcuts = QtGui.QAction("Keyboard Shortcuts…", self)
        # No QAction shortcut — QTableWidget/QTreeWidget with focus intercept
        # key events before ApplicationShortcut fires. The app-level event
        # filter below is the reliable path. We keep the action for the Help
        # menu entry only.
        self._act_show_shortcuts.triggered.connect(self._shortcut_overlay.show_centered)
        self.addAction(self._act_show_shortcuts)

        # App-level event filter for '?' — fires before any widget's own
        # keyPressEvent, so it works regardless of focus owner.
        self._question_filter = _QuestionKeyFilter(
            self._shortcut_overlay.show_centered, self
        )
        QtWidgets.QApplication.instance().installEventFilter(self._question_filter)

        self._build_menu()
        self._build_toolbar()

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(2)  # tight so minimap/plot are separated only by the 1px border

        # Overflow banner (hidden unless the ring buffer wrapped)
        self.overflow_banner = QtWidgets.QFrame()
        self.overflow_banner.setStyleSheet(
            f"QFrame{{background:{THEME['status_error_bg']}; border-radius:4px;}}"
        )
        _ovf_row = QtWidgets.QHBoxLayout(self.overflow_banner)
        _ovf_row.setContentsMargins(8, 8, 6, 8)
        _ovf_row.setSpacing(6)

        self.overflow_banner_label = QtWidgets.QLabel()
        self.overflow_banner_label.setStyleSheet(
            "background:transparent; color:#ffeaea; font-weight:bold;"
        )
        self.overflow_banner_label.setWordWrap(True)
        _ovf_row.addWidget(self.overflow_banner_label, 1)

        _ovf_close = QtWidgets.QToolButton()
        _ovf_close.setText("\u00d7")
        _ovf_close.setFixedSize(20, 20)
        _ovf_close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        _ovf_close.setToolTip("Dismiss")
        _ovf_close.setStyleSheet(
            "QToolButton{border:none;color:#ffeaea;font-size:14px;"
            "background:transparent;}"
            "QToolButton:hover{color:#ffffff;}"
        )
        _ovf_close.clicked.connect(self._dismiss_overflow_banner)
        _ovf_row.addWidget(_ovf_close, 0, QtCore.Qt.AlignmentFlag.AlignTop)

        self.overflow_banner.setVisible(False)
        self._overflow_dismissed = False
        layout.addWidget(self.overflow_banner)

        # Minimap strip — visible by default (toggle via View → Show Minimap)
        self.minimap = MinimapWidget(
            self.spans, self.marks, self.color_map, self.t_min, self.total_us,
            pause_regions=self.pause_regions,
        )
        self.minimap.view_start_changed.connect(self._on_minimap_jump)
        self.minimap.setVisible(False)
        layout.addWidget(self.minimap)

        configure_pyqtgraph()
        pg.setConfigOptions(antialias=False, useOpenGL=False)

        self.x_axis = UnitAxis(orientation="bottom")
        self.x_axis.enableAutoSIPrefix(False)
        self.x_axis.unit_scale = self.unit_scale
        self.y_axis = DepthAxis(orientation="left")
        self.plot_widget = pg.PlotWidget(axisItems={"bottom": self.x_axis, "left": self.y_axis})
        self.plot = self.plot_widget.getPlotItem()
        self.plot.setLabel("bottom", f"Time ({self.unit_label})")
        self.plot.setLabel("left", "Call Depth")
        self.plot.showGrid(x=True, y=False, alpha=0.25)

        vb = self.plot.getViewBox()
        vb.invertY(True)
        vb.setMouseEnabled(x=False, y=False)
        vb.setMenuEnabled(False)
        vb.setDefaultPadding(0)

        # Override wheel event for horizontal scroll
        self.plot_widget.wheelEvent = self._plot_wheel_event

        # Save original mouse handlers so non-select-mode behavior falls through unchanged
        self._orig_mouse_press = self.plot_widget.mousePressEvent
        self._orig_mouse_move = self.plot_widget.mouseMoveEvent
        self._orig_mouse_release = self.plot_widget.mouseReleaseEvent
        self.plot_widget.mousePressEvent = self._plot_mouse_press
        self.plot_widget.mouseMoveEvent = self._plot_mouse_move
        self.plot_widget.mouseReleaseEvent = self._plot_mouse_release

        layout.addWidget(self.plot_widget, 1)

        # Measurement cursors (two draggable vertical lines)
        pen_a = pg.mkPen(THEME["pick_a"], width=2, style=QtCore.Qt.PenStyle.DashLine)
        pen_b = pg.mkPen(THEME["pick_b"], width=2, style=QtCore.Qt.PenStyle.DashLine)
        self.cursor_a = pg.InfiniteLine(
            pos=0, angle=90, movable=True, pen=pen_a,
            label="A", labelOpts={"position": 0.05, "color": THEME["pick_a"]},
        )
        self.cursor_b = pg.InfiniteLine(
            pos=0, angle=90, movable=True, pen=pen_b,
            label="B", labelOpts={"position": 0.05, "color": THEME["pick_b"]},
        )
        self.cursor_a.setHoverPen(pg.mkPen(THEME["pick_a"], width=4))
        self.cursor_b.setHoverPen(pg.mkPen(THEME["pick_b"], width=4))
        self.cursor_a.sigPositionChanged.connect(self._on_cursors_moved)
        self.cursor_b.sigPositionChanged.connect(self._on_cursors_moved)
        self.cursor_a.setVisible(False)
        self.cursor_b.setVisible(False)
        self.plot.addItem(self.cursor_a)
        self.plot.addItem(self.cursor_b)

        # Grab-tab handles — small coloured rectangles at the top of each cursor
        self.cursor_handle_a = CursorHandle(self.cursor_a, THEME["pick_a"])
        self.cursor_handle_b = CursorHandle(self.cursor_b, THEME["pick_b"])
        self.cursor_handle_a.setVisible(False)
        self.cursor_handle_b.setVisible(False)
        self.plot.addItem(self.cursor_handle_a)
        self.plot.addItem(self.cursor_handle_b)

        # Keep handles pinned to viewport top when cursor or Y range changes
        self.cursor_a.sigPositionChanged.connect(self._update_cursor_handle_pos)
        self.cursor_b.sigPositionChanged.connect(self._update_cursor_handle_pos)
        self.plot.getViewBox().sigYRangeChanged.connect(self._update_cursor_handle_pos)

        self.cursors_enabled = False

        # Scrollbar
        self.scrollbar = QtWidgets.QScrollBar(QtCore.Qt.Orientation.Horizontal)
        self.scrollbar.setSingleStep(1)
        self.scrollbar.valueChanged.connect(self._on_scroll)
        layout.addWidget(self.scrollbar)

        # Hover details bar (with bookmark toggle pinned to the right)
        self.hover_label = QtWidgets.QLabel("Hover over a bar to see details")
        self.hover_label.setStyleSheet(
            f"color:{THEME['text_primary']}; padding:6px;"
        )
        self._bm_status_btn = QtWidgets.QToolButton()
        self._bm_status_btn.setText("\U0001f516")
        self._bm_status_btn.setToolTip("Bookmarks  (toggle dock)")
        self._bm_status_btn.setObjectName("HoverBarBtn")
        self._bm_status_btn.setCheckable(True)
        self._bm_status_btn.setAutoRaise(False)
        self._bm_status_btn.clicked.connect(self._on_bm_btn_clicked)
        hover_row = QtWidgets.QWidget()
        hover_row.setObjectName("HoverRow")
        hover_row.setStyleSheet(
            f"QWidget#HoverRow {{ background:{THEME['bg_raised']}; border-radius:4px; }}"
        )
        hover_row_layout = QtWidgets.QHBoxLayout(hover_row)
        hover_row_layout.setContentsMargins(0, 0, 0, 0)
        hover_row_layout.setSpacing(0)
        hover_row_layout.addWidget(self.hover_label, 1)
        hover_row_layout.addWidget(self._bm_status_btn)
        layout.addWidget(hover_row)

        # Cursor measurement bar
        self.measure_label = QtWidgets.QLabel()
        self.measure_label.setStyleSheet(
            f"background:{THEME['bg_elevated']}; color:{THEME['text_white']};"
            "padding:6px; border-radius:4px; font-weight:bold;"
        )
        self.measure_label.setVisible(False)
        layout.addWidget(self.measure_label)

        # Pick-spans measurement bar (shows A→B delta when user picks two spans)
        self.pick_label = QtWidgets.QLabel()
        self.pick_label.setStyleSheet(
            f"background:{THEME['bg_elevated']}; color:{THEME['text_white']};"
            "padding:6px; border-radius:4px;"
        )
        self.pick_label.setVisible(False)
        layout.addWidget(self.pick_label)

        # Color-by-duration legend (hidden unless in duration mode)
        self.color_bar = ColorBarWidget()
        self.color_bar.setVisible(False)
        layout.addWidget(self.color_bar)

        # Status bar — persistent mode badge on the right side
        self._mode_label = QtWidgets.QLabel("")
        self._mode_label.setObjectName("ModeBadge")
        self._mode_label.setVisible(False)
        self.statusBar().addPermanentWidget(self._mode_label)

        self.statusBar().setSizeGripEnabled(False)
        self._update_status_bar()

        # Summary dock (bottom)
        self.summary_dock = SummaryDock(self.spans, self.color_map, self)
        self.summary_dock.setObjectName("dock_summary")
        self.summary_dock.setTitleBarWidget(DockTitleBar(self.summary_dock))
        self.summary_dock.function_clicked.connect(self._jump_to_function_name)
        self.summary_dock.visibility_changed.connect(self._on_visibility_changed)
        self.summary_dock.analyze_jitter_requested.connect(self._show_jitter_for_function)
        self.summary_dock.ribbon_requested.connect(self._on_ribbon_requested)
        self.summary_dock.cursor_snap_requested.connect(self._snap_cursor_to_function)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.summary_dock)

        # Call-tree dock (bottom, tabbed with summary)
        self.call_tree_dock = CallTreeDock(self.spans, self.color_map, self.total_us, self)
        self.call_tree_dock.setObjectName("dock_call_tree")
        self.call_tree_dock.setTitleBarWidget(DockTitleBar(self.call_tree_dock))
        self.call_tree_dock.function_clicked.connect(self._jump_to_function_name)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.call_tree_dock)
        self.tabifyDockWidget(self.summary_dock, self.call_tree_dock)

        # Call-graph dock (bottom, tabbed with call tree)
        self.call_graph_dock = CallGraphDock(self.spans, self.color_map, self)
        self.call_graph_dock.setObjectName("dock_call_graph")
        self.call_graph_dock.setTitleBarWidget(DockTitleBar(self.call_graph_dock))
        self.call_graph_dock.function_clicked.connect(self._jump_to_function_name)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.call_graph_dock)
        self.tabifyDockWidget(self.call_tree_dock, self.call_graph_dock)

        # Markers dock (bottom, tabbed with summary + call tree)
        self.marker_dock = MarkerDock(self.marks, self)
        self.marker_dock.setObjectName("dock_markers")
        self.marker_dock.setTitleBarWidget(DockTitleBar(self.marker_dock))
        self.marker_dock.mark_clicked.connect(self._jump_to_mark_name)
        self.marker_dock.visibility_changed.connect(self._on_mark_visibility_changed)
        self.marker_dock.analyze_jitter_requested.connect(self._show_jitter_for_marker)
        self.marker_dock.cursor_snap_requested.connect(self._snap_cursor_to_time)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.marker_dock)
        self.tabifyDockWidget(self.summary_dock, self.marker_dock)

        # Top-N slowest calls dock (bottom, tabbed)
        self.top_n_dock = TopNSlowestDock(self.spans, self.color_map, self)
        self.top_n_dock.setObjectName("dock_top_n")
        self.top_n_dock.setTitleBarWidget(DockTitleBar(self.top_n_dock))
        self.top_n_dock.span_clicked.connect(self._jump_to_span_instance)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.top_n_dock)
        self.tabifyDockWidget(self.summary_dock, self.top_n_dock)

        # Function Ribbon dock — starts empty; populated via Summary
        # context menu → "Show in Ribbon View"
        self.ribbon_dock = RibbonDock(
            self.spans, self.color_map, self.t_min, self.total_us, self,
        )
        self.ribbon_dock.setObjectName("dock_ribbon")
        self.ribbon_dock.setTitleBarWidget(DockTitleBar(self.ribbon_dock))
        self.ribbon_dock.tick_clicked.connect(self._on_ribbon_tick_clicked)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.ribbon_dock)
        self.tabifyDockWidget(self.summary_dock, self.ribbon_dock)

        # Bookmark dock — starts hidden; shown via Ctrl+Shift+B or Panels menu
        self.bookmark_dock = BookmarkDock(parent=self)
        self.bookmark_dock.setObjectName("dock_bookmarks")
        self.bookmark_dock.setTitleBarWidget(DockTitleBar(self.bookmark_dock))
        self.bookmark_dock.activated.connect(self._on_bookmark_activated)
        self.bookmark_dock.save_bookmark_requested.connect(self._save_current_bookmark)
        self.bookmark_dock.bookmarks_changed.connect(
            lambda bms: self._save_settings({"bookmarks": bms})
        )
        self.bookmark_dock.bookmarks_changed.connect(self._sync_bookmark_lines)
        self.bookmark_dock.pick_position_requested.connect(self._toggle_bookmark_pick)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.bookmark_dock)
        self.bookmark_dock.hide()
        self.bookmark_dock.visibilityChanged.connect(self._on_bookmark_visibility_changed)

        self.summary_dock.raise_()

        self.resizeDocks([self.summary_dock], [260], QtCore.Qt.Orientation.Vertical)

        # Sync docks to the current display unit (default is ms)
        self.summary_dock.set_unit(self.unit_label, self.unit_scale)
        self.call_tree_dock.set_unit(self.unit_label, self.unit_scale)
        self.marker_dock.set_unit(self.unit_label, self.unit_scale)
        self.top_n_dock.set_unit(self.unit_label, self.unit_scale)
        self.call_graph_dock.set_unit(self.unit_label, self.unit_scale)

        # Now that the docks exist, populate the Panels submenu with their
        # toggle actions (they stay in sync with the dock's visibility state).
        self._populate_panels_menu()

        # Throttled hover
        self._hover_timer = QtCore.QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(20)
        self._hover_timer.timeout.connect(self._do_hover_update)
        self._last_mouse_pos = None
        self.plot_widget.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.plot_widget.scene().sigMouseClicked.connect(self._on_plot_clicked)

    # ── Settings ─────────────────────────────────────────────────────

    _SETTINGS_FILE = pathlib.Path.home() / ".cortexm0_profiler.json"

    def _load_settings(self) -> dict:
        try:
            return json.loads(self._SETTINGS_FILE.read_text())
        except Exception:
            return {}

    def _save_settings(self, updates: dict) -> None:
        data = self._load_settings()
        data.update(updates)
        try:
            self._SETTINGS_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _restore_session(self, s: dict | None = None) -> None:
        """Apply persisted session state that needs the UI already built
        (dock geometry, view toggles, etc).

        Theme and palette are handled earlier, in __init__, before the UI
        is constructed — see _compute_palette_color_map() — so they are
        not touched here.
        """
        import base64
        if s is None:
            s = self._load_settings()

        # Display unit
        saved_unit = s.get("unit", "ms")
        if saved_unit != self.unit_label:
            self._set_unit(saved_unit)

        # Color mode
        saved_mode = s.get("color_mode", "function")
        if saved_mode != self._color_mode:
            self._set_color_mode(saved_mode)

        # Chart display settings
        self._row_height_setting = float(s.get("row_height",    0.85))
        self._font_size_setting  = int(s.get("font_size",     8))
        self._ts_decimals        = int(s.get("ts_decimals",   3))
        self._bookmark_snap      = bool(s.get("bookmark_snap", False))
        # Apply timestamp decimals to toolbar spinboxes immediately
        self.window_spin.setDecimals(self._ts_decimals)
        self.jump_spin.setDecimals(self._ts_decimals)

        # Dock geometry — restoreState needs all docks already added to the window
        dock_state_b64 = s.get("dock_state")
        if dock_state_b64:
            try:
                raw_bytes = base64.b64decode(dock_state_b64.encode())
                self.restoreState(QtCore.QByteArray(raw_bytes))
            except Exception:
                pass  # stale/corrupt state — silently ignore

        # View toggles — (settings_key, action, handler, default)
        for _key, _act, _fn, _default in [
            ("show_minimap",    self.act_minimap,     self._toggle_minimap,    False),
            ("highlight_hover", self.act_highlight,   self._toggle_highlight,  False),
            ("show_bar_labels", self.act_bar_labels,  self._toggle_bar_labels, False),
            ("show_mark_labels",self.act_mark_labels, self._toggle_mark_labels,False),
            ("sticky_hover",    self.act_sticky_hover,self._toggle_sticky_hover,True),
            ("show_grid",       self.act_grid,        self._toggle_grid,       True),
            ("cursors_enabled", self.act_cursors,           self._toggle_cursors,      False),
            ("pick_spans_mode", self.act_pick_spans,         self._toggle_pick_mode,    False),
            ("select_zoom",     self.act_select_zoom,        self._toggle_select_zoom,  False),
            ("exclude_isr_time",self._act_exclude_isr,       self._toggle_exclude_isr,  False),
        ]:
            val = bool(s.get(_key, _default))
            if val != _act.isChecked():
                _act.setChecked(val)
                _fn(val)

        # Hidden functions / markers (applied after spans are in the docks)
        hidden_fns = s.get("hidden_functions", [])
        if hidden_fns:
            self.summary_dock.restore_hidden(hidden_fns)

        hidden_marks = s.get("hidden_markers", [])
        if hidden_marks:
            self.marker_dock.restore_hidden(hidden_marks)

        saved_bms = s.get("bookmarks", [])
        if saved_bms:
            self.bookmark_dock.load_bookmarks(saved_bms)
            self._sync_bookmark_lines(self.bookmark_dock.get_bookmarks())

    def _save_session(self) -> None:
        """Persist all session state to the settings file."""
        import base64
        dock_b64 = base64.b64encode(bytes(self.saveState())).decode()
        self._save_settings({
            "unit":             self.unit_label,
            "color_mode":       self._color_mode,
            "dock_state":       dock_b64,
            "hidden_functions": sorted(self.summary_dock.hidden_names()),
            "hidden_markers":   sorted(self.marker_dock.hidden_names()),
            "bookmarks":        self.bookmark_dock.get_bookmarks(),
            "palette":          self._palette_name,
            "row_height":       self._row_height_setting,
            "font_size":        self._font_size_setting,
            "ts_decimals":      self._ts_decimals,
            "bookmark_snap":    self._bookmark_snap,
            # View toggles
            "show_minimap":     self.act_minimap.isChecked(),
            "highlight_hover":  self.act_highlight.isChecked(),
            "show_bar_labels":  self.act_bar_labels.isChecked(),
            "show_mark_labels": self.act_mark_labels.isChecked(),
            "sticky_hover":     self.act_sticky_hover.isChecked(),
            "show_grid":        self.act_grid.isChecked(),
            "cursors_enabled":  self.act_cursors.isChecked(),
            "pick_spans_mode":  self.act_pick_spans.isChecked(),
            "select_zoom":      self.act_select_zoom.isChecked(),
            "exclude_isr_time": self._exclude_isr_time,
        })

    def _save_current_bookmark(self):
        """Pin the centre of the current view as a named bookmark (Ctrl+B)."""
        t_us = self.view_start + self.window_us / 2
        self.bookmark_dock.save_bookmark(t_us, depth=0)

    def _on_bookmark_activated(self, t_us: float):
        """Centre the view on *t_us*, keeping the current zoom level."""
        self.view_start = t_us - self.window_us / 2
        self._update_view_range()
        self.window_spin.blockSignals(True)
        self.window_spin.setValue(self.window_us * self.unit_scale)
        self.window_spin.blockSignals(False)

    def _on_bm_btn_clicked(self, checked: bool) -> None:
        """Button click: show or hide the bookmark dock.

        setVisible(True) on a tabified QDockWidget emits visibilityChanged(False)
        first (tab is inserted but is not yet the active tab), then
        visibilityChanged(True) once the tab is raised.  If we let those signals
        reach _on_bookmark_visibility_changed while the click is still being
        processed, the False signal unchecks the button before the True signal
        corrects it — leaving state that causes a second click to be needed.

        Blocking the dock's signals during the setVisible+raise_() call prevents
        any intermediate visibilityChanged from interfering.  We then set the
        button state directly and trigger the relayout when needed.
        """
        blocker = QtCore.QSignalBlocker(self.bookmark_dock)
        self.bookmark_dock.setVisible(checked)
        if checked:
            self.bookmark_dock.raise_()
        del blocker          # unblock — future tab-switching signals work normally
        self._bm_status_btn.setChecked(checked)
        if checked:
            QtCore.QTimer.singleShot(0, self._relayout_dock_tabs)

    def _on_bookmark_visibility_changed(self, visible: bool) -> None:
        """Sync button when the user switches tabs by clicking the tab bar directly."""
        self._bm_status_btn.setChecked(visible)

    def _relayout_dock_tabs(self) -> None:
        """Re-elide tab text on the bottom dock tab bar after a dock is shown.

        When a tabified QDockWidget becomes visible Qt re-inserts its tab and
        calls QTabBarPrivate::layoutTabs() immediately — before the tab bar has
        received its final geometry from the main-window layout pass.  The text
        is elided with a stale width and stays wrong because the tab bar's size
        does not change afterwards (it already spanned the full width), so no
        resizeEvent arrives to correct it.

        QTabBar::setTabText() calls QTabBarPrivate::refresh() → layoutTabs()
        unconditionally regardless of whether the text changed.  We call it on
        the first tab we find that belongs to our bottom dock group, which
        forces a correct second layout pass with the settled geometry.

        We match by known window titles so we never touch PyQtGraph's internal
        tab bars, which have uninitialised font sizes and would produce
        "QFont::setPointSize: Point size <= 0" warnings if disturbed.
        """
        known = {
            self.summary_dock.windowTitle(),
            self.top_n_dock.windowTitle(),
            self.ribbon_dock.windowTitle(),
            self.call_tree_dock.windowTitle(),
            self.call_graph_dock.windowTitle(),
            self.bookmark_dock.windowTitle(),
        }
        for tab_bar in self.findChildren(QtWidgets.QTabBar):
            for i in range(tab_bar.count()):
                if tab_bar.tabText(i) in known:
                    tab_bar.setTabText(i, tab_bar.tabText(i))
                    return

    def _sync_bookmark_lines(self, bookmarks: list) -> None:
        """Push bookmark data to the flame chart for rendering."""
        if hasattr(self, "_flame_item") and self._flame_item is not None:
            self._flame_item.set_bookmarks(bookmarks)

    def _depth_from_event(self, event) -> int:
        """Map a QMouseEvent position to a depth (Y-axis row index)."""
        vb = self.plot.getViewBox()
        try:
            scene_pt = self.plot_widget.mapToScene(event.position().toPoint())
        except AttributeError:
            scene_pt = self.plot_widget.mapToScene(event.pos())
        y = vb.mapSceneToView(scene_pt).y()
        return max(0, int(y))

    def _toggle_bookmark_pick(self, on=None) -> None:
        if on is None:
            on = not self._bookmark_pick_mode
        self._bookmark_pick_mode = on
        if on:
            self.plot_widget.setCursor(QtCore.Qt.CursorShape.CrossCursor)
            self.statusBar().showMessage(
                "Click on the chart to place a bookmark.  Esc to cancel."
            )
            self._mode_label.setText("BOOKMARK PICK")
            self._mode_label.setVisible(True)
        else:
            self._bookmark_pick_mode = False
            self.plot_widget.unsetCursor()
            self._update_status_bar()
            self._mode_label.setText("")
            self._mode_label.setVisible(False)

    def _open_settings(self):
        import gui.theme as _theme_mod
        current = {
            "palette":       self._palette_name,
            "row_height":    self._row_height_setting,
            "font_size":     self._font_size_setting,
            "ts_decimals":      self._ts_decimals,
            "bookmark_snap":    self._bookmark_snap,
        }
        dlg = SettingsDialog(
            current_theme=_theme_mod.CURRENT_THEME_NAME,
            current_settings=current,
            parent=self,
        )
        dlg.theme_changed.connect(self._on_theme_changed)
        dlg.settings_applied.connect(self._on_settings_applied)
        dlg.exec()

    def _on_theme_changed(self, name: str) -> None:
        """Switch to the named theme and reapply all styled widgets."""
        apply_theme(name)

        # 1. Global QSS — set on QApplication so it reaches QDockWidgetGroupWindow
        # (tabified docks) which can be a separate top-level window not parented
        # under QMainWindow, and would miss a self.setStyleSheet() cascade.
        QtWidgets.QApplication.instance().setStyleSheet(build_stylesheet())

        # 2. Per-widget stylesheets that were set individually at build time
        self.window_spin.setStyleSheet(spinbox_qss("QDoubleSpinBox"))
        self.jump_spin.setStyleSheet(spinbox_qss("QDoubleSpinBox"))

        _cd = (_ICONS / "chevron_dn.svg").as_posix()
        self.search_combo.setStyleSheet(
            f"QComboBox::drop-down {{"
            f"  subcontrol-origin: border; subcontrol-position: right center;"
            f"  width: 18px; background: {THEME['bg_raised']};"
            f"  border-left: 1px solid {THEME['border_normal']}; border-radius: 0px 3px 3px 0px; }}"
            f"QComboBox::drop-down:hover {{ background: {THEME['interactive_hover_btn']}; }}"
            f"QComboBox::down-arrow {{ image: url({_cd}); width: 7px; height: 5px; }}"
        )

        # Inline-styled labels
        self.hover_label.setStyleSheet(
            f"background:{THEME['bg_raised']}; color:{THEME['text_primary']};"
            "padding:6px; border-radius:4px;"
        )
        self.measure_label.setStyleSheet(
            f"background:{THEME['bg_elevated']}; color:{THEME['text_white']};"
            "padding:6px; border-radius:4px; font-weight:bold;"
        )
        self.pick_label.setStyleSheet(
            f"background:{THEME['bg_elevated']}; color:{THEME['text_white']};"
            "padding:6px; border-radius:4px;"
        )

        # 3. pyqtgraph main plot — background + axis colors
        configure_pyqtgraph()
        if hasattr(self, "plot_widget"):
            self.plot_widget.setBackground(THEME["bg_base"])
        if hasattr(self, "plot"):
            axis_pen = pg.mkPen(THEME["text_primary"])
            for axis_name in ("bottom", "left", "right", "top"):
                ax = self.plot.getAxis(axis_name)
                if ax is not None:
                    ax.setPen(axis_pen)
                    ax.setTextPen(axis_pen)

        # 4. Repaint custom-painted widgets
        if hasattr(self, "minimap"):
            self.minimap._invalidate_cache()
            self.minimap.update()
        if hasattr(self, "color_bar"):
            self.color_bar.update()
        if hasattr(self, "_flame_item") and self._flame_item is not None:
            self._flame_item.update()

        # 5. Docks with individually-styled widgets
        if hasattr(self, "top_n_dock"):
            self.top_n_dock.refresh_theme()
        if hasattr(self, "call_graph_dock"):
            self.call_graph_dock.refresh_theme()

        # 6. Persist
        self._save_settings({"theme": name})

    def _on_settings_applied(self, s: dict) -> None:
        """Handle settings_applied signal from SettingsDialog."""
        changed_palette   = s.get("palette", "Default")        != self._palette_name
        changed_row_h     = s.get("row_height", 0.85)          != self._row_height_setting
        changed_font      = s.get("font_size", 8)              != self._font_size_setting
        changed_decimals  = s.get("ts_decimals", 3)            != self._ts_decimals

        self._palette_name       = s.get("palette",         "Default")
        self._row_height_setting = s.get("row_height",      0.85)
        self._font_size_setting  = s.get("font_size",       8)
        self._ts_decimals        = s.get("ts_decimals",     3)
        self._bookmark_snap      = s.get("bookmark_snap",   False)

        # Apply row height / font size live to the existing flame item
        if hasattr(self, "_flame_item") and self._flame_item is not None:
            if changed_row_h:
                self._flame_item.set_row_height(self._row_height_setting)
            if changed_font:
                self._flame_item.set_chart_font_size(self._font_size_setting)

        # Color palette change: reset color map and rebuild flame item
        if changed_palette:
            palette_colors = PALETTES.get(self._palette_name, COLORS)
            self.color_map.clear()
            # Rebuild color assignments with the new palette
            used: set[str] = set()
            for i, name in enumerate(self.func_names):
                for j in range(len(palette_colors)):
                    candidate = palette_colors[(i + j) % len(palette_colors)]
                    if candidate not in used:
                        self.color_map[name] = candidate
                        used.add(candidate)
                        break
                else:
                    self.color_map[name] = palette_colors[i % len(palette_colors)]
            self._populate_plot()

        # Timestamp decimals: update toolbar spinboxes
        if changed_decimals:
            dec = self._ts_decimals
            self.window_spin.setDecimals(dec)
            self.jump_spin.setDecimals(dec)

        self._save_settings({
            "palette":          self._palette_name,
            "row_height":       self._row_height_setting,
            "font_size":        self._font_size_setting,
            "ts_decimals":      self._ts_decimals,
            "bookmark_snap":    self._bookmark_snap,
        })

    def _snap_to_span_edge(self, t_us: float, depth: int) -> float:
        """Return t_us snapped to the nearest span start/end at *depth*."""
        if not self._bookmark_snap:
            return t_us
        best_t = t_us
        best_d = float("inf")
        for sp in self.spans:
            if sp.get("depth", 0) != depth:
                continue
            for edge in (sp["start_us"], sp["end_us"]):
                d = abs(edge - (t_us + self.t_min))
                if d < best_d:
                    best_d = d
                    best_t = edge - self.t_min
        return best_t

    def _build_menu(self):
        menubar = self.menuBar()

        # ── File menu ──
        file_menu = menubar.addMenu("&File")

        act_open_trace = QtGui.QAction("Open Trace...", self)
        act_open_trace.setShortcut("Ctrl+O")
        act_open_trace.triggered.connect(self._open_trace_dialog)
        file_menu.addAction(act_open_trace)

        act_connect_jlink = QtGui.QAction("Connect to J-Link\u2026", self)
        act_connect_jlink.setShortcut("Ctrl+J")
        act_connect_jlink.triggered.connect(self._connect_jlink_dialog)
        file_menu.addAction(act_connect_jlink)

        # Recent traces submenu (populated from QSettings)
        self._recent_menu = file_menu.addMenu("Recent Traces")
        self._populate_recent_menu()

        file_menu.addSeparator()

        export_menu = file_menu.addMenu("Export")
        act_export_json = QtGui.QAction("Export as JSON...", self)
        act_export_json.triggered.connect(self._export_json)
        export_menu.addAction(act_export_json)

        act_export_sltrace = QtGui.QAction("Export as .sltrace…", self)
        act_export_sltrace.triggered.connect(self._export_sltrace)
        export_menu.addAction(act_export_sltrace)

        act_export_csv = QtGui.QAction("Export as CSV...", self)
        act_export_csv.triggered.connect(self._export_csv)
        export_menu.addAction(act_export_csv)

        act_export_image = QtGui.QAction("Export Image…", self)
        act_export_image.setShortcut("Ctrl+Shift+E")
        act_export_image.triggered.connect(self._export_image_dialog)
        export_menu.addAction(act_export_image)

        file_menu.addSeparator()

        self.act_refresh = QtGui.QAction("Refresh Trace", self)
        self.act_refresh.setShortcut("F5")
        self.act_refresh.triggered.connect(self._refresh)
        self.act_refresh.setEnabled(self.refresh_fn is not None)
        file_menu.addAction(self.act_refresh)

        file_menu.addSeparator()

        act_quit = QtGui.QAction("Quit", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        # ── View menu ──
        view_menu = menubar.addMenu("&View")

        # Units submenu
        unit_menu = view_menu.addMenu("Units")
        self.unit_group = QtGui.QActionGroup(self)
        self.unit_group.setExclusive(True)
        act_us = QtGui.QAction("Microseconds (us)", self, checkable=True)
        act_us.setChecked(self.unit_label == "us")
        act_us.triggered.connect(lambda: self._set_unit("us"))
        self.unit_group.addAction(act_us)
        unit_menu.addAction(act_us)

        act_ms = QtGui.QAction("Milliseconds (ms)", self, checkable=True)
        act_ms.setChecked(self.unit_label == "ms")
        act_ms.triggered.connect(lambda: self._set_unit("ms"))
        self.unit_group.addAction(act_ms)
        unit_menu.addAction(act_ms)

        # Color-mode submenu
        color_menu = view_menu.addMenu("Color Mode")
        self.color_group = QtGui.QActionGroup(self)
        self.color_group.setExclusive(True)

        act_color_fn = QtGui.QAction("By Function", self, checkable=True)
        act_color_fn.setChecked(True)
        act_color_fn.triggered.connect(lambda: self._set_color_mode("function"))
        self.color_group.addAction(act_color_fn)
        color_menu.addAction(act_color_fn)

        act_color_dur = QtGui.QAction("By Duration (heat)", self, checkable=True)
        act_color_dur.triggered.connect(lambda: self._set_color_mode("duration"))
        self.color_group.addAction(act_color_dur)
        color_menu.addAction(act_color_dur)

        view_menu.addSeparator()

        # Minimap toggle (on by default)
        self.act_minimap = QtGui.QAction("Show Minimap", self, checkable=True)
        self.act_minimap.setChecked(False)
        self.act_minimap.setShortcut("Ctrl+Shift+M")
        self.act_minimap.triggered.connect(self._toggle_minimap)
        view_menu.addAction(self.act_minimap)

        # Dock panel toggles (Summary / Call Tree / Markers) — actions are
        # added in _populate_panels_menu() after the docks exist.
        self._panels_menu = view_menu.addMenu("Panels")

        view_menu.addSeparator()

        self.act_highlight = QtGui.QAction("Highlight Hovered Function", self, checkable=True)
        self.act_highlight.setChecked(False)
        self.act_highlight.setShortcut("Ctrl+H")
        self.act_highlight.triggered.connect(self._toggle_highlight)
        view_menu.addAction(self.act_highlight)

        self.act_bar_labels = QtGui.QAction("Show Function Names on Bars", self, checkable=True)
        self.act_bar_labels.setChecked(False)  # OFF by default
        self.act_bar_labels.setShortcut("Ctrl+L")
        self.act_bar_labels.triggered.connect(self._toggle_bar_labels)
        view_menu.addAction(self.act_bar_labels)

        self.act_mark_labels = QtGui.QAction("Show Marker Names on Chart", self, checkable=True)
        self.act_mark_labels.setChecked(False)  # OFF by default
        self.act_mark_labels.triggered.connect(self._toggle_mark_labels)
        view_menu.addAction(self.act_mark_labels)

        self.act_sticky_hover = QtGui.QAction("Sticky Hover Label", self, checkable=True)
        self.act_sticky_hover.setChecked(True)   # ON by default
        self.act_sticky_hover.triggered.connect(self._toggle_sticky_hover)
        view_menu.addAction(self.act_sticky_hover)

        self.act_grid = QtGui.QAction("Show Grid", self, checkable=True)
        self.act_grid.setChecked(True)   # x-grid on by default (matches plot init)
        self.act_grid.triggered.connect(self._toggle_grid)
        view_menu.addAction(self.act_grid)

        self.act_cursors = QtGui.QAction("Measurement Cursors", self, checkable=True)
        self.act_cursors.setShortcut("Ctrl+M")
        self.act_cursors.triggered.connect(self._toggle_cursors)
        view_menu.addAction(self.act_cursors)

        self.act_pick_spans = QtGui.QAction("Pick Spans Mode", self, checkable=True)
        self.act_pick_spans.setShortcut("Ctrl+P")
        self.act_pick_spans.triggered.connect(self._toggle_pick_mode)
        view_menu.addAction(self.act_pick_spans)

        self.act_select_zoom = QtGui.QAction("Zoom to Selection", self, checkable=True)
        self.act_select_zoom.setShortcut("Ctrl+R")
        self.act_select_zoom.triggered.connect(self._toggle_select_zoom)
        view_menu.addAction(self.act_select_zoom)

        self.act_fit_all = QtGui.QAction("Fit to Waveform", self)
        self.act_fit_all.setShortcut("Ctrl+Shift+F")
        self.act_fit_all.setToolTip("Zoom out to show the entire trace")
        self.act_fit_all.triggered.connect(self._fit_all)
        view_menu.addAction(self.act_fit_all)

        view_menu.addSeparator()

        act_reset = QtGui.QAction("Reset View", self)
        act_reset.setShortcut("Ctrl+0")
        act_reset.triggered.connect(self._reset_view)
        view_menu.addAction(act_reset)

        # ── Navigate menu ──
        nav_menu = menubar.addMenu("&Navigate")
        act_home = QtGui.QAction("Go to Start", self)
        act_home.setShortcut("Home")
        act_home.triggered.connect(lambda: self._set_view_start(0.0))
        nav_menu.addAction(act_home)

        act_end = QtGui.QAction("Go to End", self)
        act_end.setShortcut("End")
        act_end.triggered.connect(
            lambda: self._set_view_start(max(0.0, self.total_us - self.window_us))
        )
        nav_menu.addAction(act_end)

        nav_menu.addSeparator()

        # Iterate through occurrences of the most recently selected
        # function/marker (set via Find combobox, summary dock, or marker dock)
        act_next_occ = QtGui.QAction("Next Occurrence", self)
        act_next_occ.setShortcut("F3")
        act_next_occ.triggered.connect(self._iter_next)
        nav_menu.addAction(act_next_occ)

        act_prev_occ = QtGui.QAction("Previous Occurrence", self)
        act_prev_occ.setShortcut("Shift+F3")
        act_prev_occ.triggered.connect(self._iter_prev)
        nav_menu.addAction(act_prev_occ)

        # ── Settings menu ──
        settings_menu = menubar.addMenu("&Settings")
        act_settings = QtGui.QAction("Preferences\u2026", self)
        act_settings.setShortcut("Ctrl+,")
        act_settings.triggered.connect(self._open_settings)
        settings_menu.addAction(act_settings)

        # ── Help menu ──
        help_menu = menubar.addMenu("&Help")
        # Reuse the shortcut action created in __init__ (lives on self.*)
        # so the QAction's keyboard shortcut and menu entry are the same
        # instance, keeping the accelerator display consistent.
        help_menu.addAction(self._act_show_shortcuts)

    def _populate_panels_menu(self):
        """Add toggle actions for the three bottom docks.

        Uses QDockWidget.toggleViewAction() which is automatically wired to
        the dock's visibility state — clicking it hides/shows the dock and
        the checkmark stays in sync, even if the user closes the dock via
        its own title-bar X button.
        """
        summary_action = self.summary_dock.toggleViewAction()
        summary_action.setText("Function Summary")
        summary_action.setShortcut("Ctrl+Shift+F")
        self._panels_menu.addAction(summary_action)

        tree_action = self.call_tree_dock.toggleViewAction()
        tree_action.setText("Call Tree")
        tree_action.setShortcut("Ctrl+Shift+T")
        self._panels_menu.addAction(tree_action)

        marker_action = self.marker_dock.toggleViewAction()
        marker_action.setText("Markers")
        marker_action.setShortcut("Ctrl+Shift+K")
        self._panels_menu.addAction(marker_action)

        top_n_action = self.top_n_dock.toggleViewAction()
        top_n_action.setText("Top Slowest Calls")
        top_n_action.setShortcut("Ctrl+Shift+O")
        self._panels_menu.addAction(top_n_action)

        graph_action = self.call_graph_dock.toggleViewAction()
        graph_action.setText("Call Graph")
        graph_action.setShortcut("Ctrl+Shift+G")
        self._panels_menu.addAction(graph_action)

        bm_action = self.bookmark_dock.toggleViewAction()
        bm_action.setText("Bookmarks")
        bm_action.setShortcut("Ctrl+Shift+B")
        self._panels_menu.addAction(bm_action)

        act_pin_bm = QtGui.QAction("Pin Position", self)
        act_pin_bm.setShortcut("Ctrl+B")
        act_pin_bm.triggered.connect(self._save_current_bookmark)
        self.addAction(act_pin_bm)

    def _build_toolbar(self):
        tb = QtWidgets.QToolBar("Main")
        tb.setObjectName("toolbar_main")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.window_label = QtWidgets.QLabel(f" Window ({self.unit_label}): ")
        tb.addWidget(self.window_label)
        self.window_spin = QtWidgets.QDoubleSpinBox()
        self.window_spin.setRange(0.1, 1e12)
        self.window_spin.setDecimals(2 if self.unit_label == "us" else 4)
        self.window_spin.setValue(self.window_us * self.unit_scale)
        self.window_spin.setSingleStep(10.0 if self.unit_label == "us" else 0.01)
        self.window_spin.setMinimumWidth(140)
        self.window_spin.setKeyboardTracking(False)
        self.window_spin.valueChanged.connect(self._on_window_changed)
        tb.addWidget(self.window_spin)

        tb.addSeparator()

        self.jump_label = QtWidgets.QLabel(f" Jump to ({self.unit_label}): ")
        tb.addWidget(self.jump_label)
        self.jump_spin = QtWidgets.QDoubleSpinBox()
        self.jump_spin.setRange(0, 1e12)
        self.jump_spin.setDecimals(2 if self.unit_label == "us" else 4)
        self.jump_spin.setMinimumWidth(140)
        self.jump_spin.setKeyboardTracking(False)
        tb.addWidget(self.jump_spin)

        self.window_spin.setStyleSheet(spinbox_qss("QDoubleSpinBox"))
        self.jump_spin.setStyleSheet(spinbox_qss("QDoubleSpinBox"))

        jump_btn = QtGui.QAction("Go", self)
        jump_btn.triggered.connect(self._jump_to_time)
        tb.addAction(jump_btn)

        tb.addSeparator()

        tb.addWidget(QtWidgets.QLabel(" Find: "))
        self.search_combo = QtWidgets.QComboBox()
        self.search_combo.setEditable(True)
        self.search_combo.setMinimumWidth(260)
        self.search_combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        for name in self.func_names:
            self.search_combo.addItem(name)
        # Start with no selection — line edit shows the placeholder text
        self.search_combo.setCurrentIndex(-1)
        line_edit = self.search_combo.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText("Find function...")
            line_edit.returnPressed.connect(self._on_search_return_pressed)
        self.search_combo.activated.connect(self._on_search_activated)
        tb.addWidget(self.search_combo)
        _cd = (_ICONS / "chevron_dn.svg").as_posix()
        self.search_combo.setStyleSheet(
            # subcontrol-origin: border positions the drop-down at the widget's
            # right edge; Qt automatically constrains the inner QLineEdit to the
            # space left of the button — no extra padding-right needed.
            f"QComboBox::drop-down {{"
            f"  subcontrol-origin: border; subcontrol-position: right center;"
            f"  width: 18px; background: {THEME['bg_raised']};"
            f"  border-left: 1px solid {THEME['border_normal']}; border-radius: 0px 3px 3px 0px; }}"
            f"QComboBox::drop-down:hover {{ background: {THEME['interactive_hover_btn']}; }}"
            f"QComboBox::down-arrow {{ image: url({_cd}); width: 7px; height: 5px; }}"
        )

        # Search-as-you-type: QCompleter with substring match (not just prefix)
        self._search_completer = QtWidgets.QCompleter(self.func_names, self.search_combo)
        self._search_completer.setCaseSensitivity(QtCore.Qt.CaseSensitivity.CaseInsensitive)
        self._search_completer.setFilterMode(QtCore.Qt.MatchFlag.MatchContains)
        self._search_completer.setCompletionMode(
            QtWidgets.QCompleter.CompletionMode.PopupCompletion
        )
        self._search_completer.setMaxVisibleItems(12)
        self.search_combo.setCompleter(self._search_completer)

        # Completer popups are top-level windows; global QSS doesn't always
        # reach them. Style the popup directly so it matches the dark theme.
        _popup = self._search_completer.popup()
        if _popup is not None:
            _popup.setStyleSheet(
                f"QListView {{"
                f"  background: {THEME['bg_raised']};"
                f"  color: {THEME['text_primary']};"
                f"  border: 1px solid {THEME['border_normal']};"
                f"  outline: 0;"
                f"  padding: 2px;"
                f"}}"
                f"QListView::item {{"
                f"  padding: 5px 10px;"
                f"  min-height: 20px;"
                f"}}"
                f"QListView::item:selected {{"
                f"  background: {THEME['selection_bg']};"
                f"  color: {THEME['text_white']};"
                f"}}"
                f"QListView::item:hover {{"
                f"  background: {THEME['bg_elevated']};"
                f"}}"
            )

        # "Next" button — advances to the next occurrence of the currently
        # selected function (or the most recently jumped-to one).
        next_action = QtGui.QAction("Next", self)
        next_action.setToolTip("Jump to the next occurrence of the selected function (F3)")
        next_action.triggered.connect(self._iter_next)
        tb.addAction(next_action)

        tb.addSeparator()

        # Zoom to Selection (shares QAction with View menu)
        tb.addAction(self.act_select_zoom)
        tb.addAction(self.act_fit_all)

        tb.addSeparator()

        self._act_exclude_isr = QtGui.QAction("Exc. ISR", self)
        self._act_exclude_isr.setCheckable(True)
        self._act_exclude_isr.setChecked(self._exclude_isr_time)
        self._act_exclude_isr.setToolTip(
            "Exclude ISR preemption time from function timing stats\n"
            "When enabled, time spent in interrupts is subtracted from the\n"
            "thread function durations in the Summary and Top Slowest tables."
        )
        self._act_exclude_isr.toggled.connect(self._toggle_exclude_isr)
        tb.addAction(self._act_exclude_isr)

        if self.refresh_fn is not None:
            # Share the same QAction as the File menu entry (avoids duplicate F5 shortcut)
            tb.addAction(self.act_refresh)

    def _update_status_bar(self):
        self.statusBar().showMessage(
            f"{len(self.spans)} spans  |  "
            f"{len(self.func_names)} unique functions  |  "
            f"Total: {self.total_us * self.unit_scale:.2f} {self.unit_label}"
        )

    # ── Plot population ──────────────────────────────────────────────

    def _populate_plot(self):
        # Remove any previous flame item
        if hasattr(self, "_flame_item") and self._flame_item is not None:
            try:
                self.plot.removeItem(self._flame_item)
            except Exception:
                pass
        self._clear_picks()

        if not self.spans and not self.marks:
            return

        current_mode = "function"
        for act in self.color_group.actions() if hasattr(self, "color_group") else []:
            if act.isChecked():
                current_mode = "duration" if "Duration" in act.text() else "function"
                break

        self._flame_item = FlameItem(
            self.spans,
            self.color_map,
            self.t_min,
            color_mode=current_mode,
            marks=self.marks,
            pause_regions=self.pause_regions,
            row_height=self._row_height_setting,
            font_size=self._font_size_setting,
        )
        self._flame_item.set_hidden(
            self.summary_dock.hidden_names() if hasattr(self, "summary_dock") else set()
        )
        self._flame_item.set_hidden_marks(
            self.marker_dock.hidden_names() if hasattr(self, "marker_dock") else set()
        )
        # Preserve toggle states across refresh/load
        if hasattr(self, "act_bar_labels") and self.act_bar_labels.isChecked():
            self._flame_item.set_show_bar_labels(True)
        if hasattr(self, "act_mark_labels") and self.act_mark_labels.isChecked():
            self._flame_item.set_show_mark_labels(True)
        if hasattr(self, "act_sticky_hover") and self.act_sticky_hover.isChecked():
            self._flame_item.set_show_sticky_hover(True)
        self.plot.addItem(self._flame_item)

        # Restore bookmarks on the new flame item
        if hasattr(self, "bookmark_dock"):
            self._sync_bookmark_lines(self.bookmark_dock.get_bookmarks())

        y_top = self.min_display_y - 0.2
        y_bot = self.max_display_y + 1.0
        self.plot.setYRange(y_top, y_bot, padding=0)

        vb = self.plot.getViewBox()
        vb.setLimits(
            xMin=0,
            xMax=max(self.total_us, 1.0),
            yMin=y_top - 0.3,
            yMax=y_bot + 0.5,
        )

    # ── Live refresh ─────────────────────────────────────────────────

    def _refresh(self):
        if self.refresh_fn is None:
            return
        try:
            result = self.refresh_fn()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Refresh failed", str(e))
            return

        # refresh_fn returns (spans, marks, pause_regions, wrapped)
        if isinstance(result, tuple) and len(result) == 4:
            new_spans, new_marks, new_pauses, new_wrapped = result
        elif isinstance(result, tuple) and len(result) == 3:
            new_spans, new_marks, new_wrapped = result
            new_pauses = []
        else:
            new_spans, new_marks, new_pauses, new_wrapped = result, [], [], False

        if not new_spans and not new_marks:
            self.statusBar().showMessage("Refresh: no trace events yet", 3000)
            return

        self._apply_new_data(new_spans, new_marks, new_pauses, new_wrapped, preserve_view=True)

    # ── Export ───────────────────────────────────────────────────────

    def _export_json(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export to JSON", "", "JSON Files (*.json)"
        )
        if not path:
            return

        elf_meta = self.elf_path or ""
        elf_copied = False

        if self.elf_path and os.path.isfile(self.elf_path):
            reply = QtWidgets.QMessageBox.question(
                self,
                "Include ELF file?",
                "Also copy the ELF file alongside this export?\n"
                "This makes the export self-contained if the ELF changes later.",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes,
            )
            if reply == QtWidgets.QMessageBox.Yes:
                elf_dest = os.path.splitext(path)[0] + ".elf"
                try:
                    shutil.copy2(self.elf_path, elf_dest)
                    elf_meta = os.path.basename(elf_dest)
                    elf_copied = True
                except Exception as e:
                    QtWidgets.QMessageBox.warning(self, "ELF copy failed", str(e))

        try:
            export_json(
                path,
                self.spans,
                meta={
                    "cpu_mhz": self.cpu_mhz,
                    "elf_path": elf_meta,
                    "total_us": self.total_us,
                },
                marks=self.marks,
                pause_regions=self.pause_regions,
                wrapped=self.wrapped,
            )
            msg = (
                f"Exported {len(self.spans)} spans, {len(self.marks)} marks, "
                f"{len(self.pause_regions)} pause regions to {path}"
            )
            if elf_copied:
                msg += f" (+ {os.path.basename(elf_dest)})"
            self.statusBar().showMessage(msg, 4000)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export failed", str(e))

    def _export_sltrace(self):
        if not self.elf_path or not os.path.isfile(self.elf_path):
            QtWidgets.QMessageBox.warning(
                self,
                "No ELF loaded",
                "A loaded ELF file is required to export a .sltrace bundle.\n"
                "Connect to J-Link first, or use Export as JSON instead.",
            )
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export as .sltrace bundle", "", "Stack-Lens Trace (*.sltrace)"
        )
        if not path:
            return
        try:
            export_sltrace(
                path,
                self.spans,
                meta={"cpu_mhz": self.cpu_mhz, "total_us": self.total_us},
                marks=self.marks,
                pause_regions=self.pause_regions,
                wrapped=self.wrapped,
                elf_path=self.elf_path,
            )
            self.statusBar().showMessage(
                f"Exported {len(self.spans)} spans, {len(self.marks)} marks "
                f"+ ELF to {os.path.basename(path)}",
                4000,
            )
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export failed", str(e))

    def _export_csv(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export to CSV", "", "CSV Files (*.csv)"
        )
        if not path:
            return
        try:
            export_csv(path, self.spans)
            self.statusBar().showMessage(f"Exported {len(self.spans)} spans to {path}", 4000)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export failed", str(e))

    def _export_image_dialog(self):
        """File → Export → Export Image... — save the current chart view as PNG or SVG."""
        path, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export Chart Image",
            "profiler_view",
            "PNG Image (*.png);;SVG Vector (*.svg)",
        )
        if not path:
            return

        is_svg = selected_filter.startswith("SVG") or path.lower().endswith(".svg")
        if not path.lower().endswith((".png", ".svg")):
            path += ".svg" if is_svg else ".png"

        try:
            self._export_image(path, is_svg)
            self.statusBar().showMessage(
                f"Image exported to {os.path.basename(path)}", 4000
            )
        except Exception as e:
            self._toast.show_message("Export failed", str(e), level="error")

    def _export_image(self, path: str, as_svg: bool) -> None:
        """Render and save the chart widget to *path* as PNG or SVG."""
        from PySide6 import QtSvg

        widget = self.plot_widget

        if as_svg:
            gen = QtSvg.QSvgGenerator()
            gen.setFileName(path)
            gen.setSize(widget.size())
            gen.setViewBox(widget.rect())
            gen.setTitle("CortexM0 Profiler — chart export")
            painter = QtGui.QPainter(gen)
            widget.render(painter)
            painter.end()
        else:
            pixmap = widget.grab()
            if not pixmap.save(path, "PNG"):
                raise OSError(f"Could not write PNG to {path}")

    # ── Open Trace / Recent ─────────────────────────────────────────

    def _connect_jlink_dialog(self):
        """File → Connect to J-Link... — establish a live J-Link session from the GUI."""
        from trace_reader import TRACE_BUFFER_SIZE, connect_jlink, load_elf_symbols, read_trace
        from span_builder import build_spans, parse_events, resolve_names
        from .jlink_connect_dialog import ConnectJLinkDialog

        dlg = ConnectJLinkDialog(
            elf_path=self.elf_path or "",
            cpu_mhz=self.cpu_mhz,
            parent=self,
        )
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        elf_path, device, cpu_mhz = dlg.get_params()
        if not elf_path:
            QtWidgets.QMessageBox.warning(self, "Connect failed", "Please select an ELF file.")
            return

        # Close any previous J-Link connection
        if self._jlink is not None:
            try:
                self._jlink.close()
            except Exception:
                pass
            self._jlink = None

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            name_to_addr, addr_to_name = load_elf_symbols(elf_path)
            jlink = connect_jlink(device)

            def read_and_parse():
                raw_buf, trace_idx = read_trace(jlink, name_to_addr)
                n_events = min(trace_idx, TRACE_BUFFER_SIZE)
                wrapped = trace_idx > TRACE_BUFFER_SIZE
                if n_events == 0:
                    return [], [], [], wrapped
                events = parse_events(raw_buf, trace_idx)
                resolve_names(events, addr_to_name, elf_path=elf_path)
                spans, marks, pause_regions = build_spans(events, cpu_mhz)
                return spans, marks, pause_regions, wrapped

            spans, marks, pause_regions, wrapped = read_and_parse()
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.warning(self, "Connect failed", str(exc))
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        self._jlink = jlink
        self.refresh_fn = read_and_parse
        self.elf_path = elf_path
        self.cpu_mhz = cpu_mhz
        self.act_refresh.setEnabled(True)

        if not spans and not marks:
            self.statusBar().showMessage(
                "Connected \u2014 no trace events yet. Press F5 to refresh.", 5000
            )
            return

        self._apply_new_data(spans, marks, pause_regions, wrapped, preserve_view=False)
        self.statusBar().showMessage(
            f"Connected to {device}: {len(spans)} spans, {len(marks)} marks", 5000
        )

    def _open_trace_dialog(self):
        """File → Open Trace... — load a JSON or .sltrace previously exported by this tool."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Open Trace",
            "",
            "Trace Files (*.json *.sltrace);;JSON Files (*.json);;Stack-Lens Bundle (*.sltrace);;All Files (*)",
        )
        if not path:
            return
        self._open_trace(path)

    def _open_trace(self, path):
        """Load a JSON trace file and install it as the current data."""
        if path.lower().endswith(".sltrace"):
            self._open_sltrace(path)
            return

        from trace_io import validate_trace

        # Parse JSON
        try:
            with open(path, "r") as f:
                raw = json.load(f)
        except json.JSONDecodeError as e:
            self._toast.show_message(
                "Invalid JSON",
                f"{os.path.basename(path)}: {e}",
                level="error",
            )
            return
        except OSError as e:
            self._toast.show_message("Cannot open file", str(e), level="error")
            return

        # Validate structure
        issues  = validate_trace(raw)
        fatal   = [s for s in issues if not s.startswith("Warning:")]
        warnings = [s for s in issues if s.startswith("Warning:")]

        if fatal:
            self._toast.show_message(
                "Trace validation failed",
                "\n".join(fatal[:3]),
                level="error",
            )
            return

        if warnings:
            self._toast.show_message(
                "Trace loaded with warnings",
                "\n".join(w.removeprefix("Warning: ") for w in warnings[:2]),
                level="warning",
            )

        spans         = raw.get("spans", [])
        marks         = raw.get("marks", [])
        pause_regions = raw.get("pause_regions", [])
        meta          = raw.get("metadata", {})

        meta_elf = meta.get("elf_path", "")
        if meta_elf:
            if not os.path.isabs(meta_elf):
                meta_elf = os.path.join(os.path.dirname(path), meta_elf)
            if os.path.isfile(meta_elf):
                self.elf_path = meta_elf

        if not spans and not marks:
            self._toast.show_message(
                "Empty trace",
                f"{os.path.basename(path)} contains no spans or marks.",
                level="info",
            )
            return

        self._apply_new_data(
            spans,
            list(marks),
            list(pause_regions),
            bool(meta.get("wrapped", False)),
            preserve_view=False,
        )

        # Persist + refresh the Recent menu
        self._add_recent_trace(path)

        self.statusBar().showMessage(
            f"Loaded {len(spans)} spans, {len(marks)} marks, "
            f"{len(pause_regions)} pause regions from {os.path.basename(path)}",
            5000,
        )
        QtCore.QTimer.singleShot(5100, self._update_status_bar)

    def _cleanup_temp_elf(self) -> None:
        """Delete self.elf_path if it is a temp file extracted from a .sltrace bundle."""
        import tempfile
        if (
            self.elf_path
            and os.path.isfile(self.elf_path)
            and self.elf_path.startswith(tempfile.gettempdir())
        ):
            try:
                os.unlink(self.elf_path)
            except OSError:
                pass

    def _open_sltrace(self, path):
        """Load a .sltrace bundle (ZIP containing trace.json + ELF)."""
        import tempfile
        from trace_io import import_sltrace, validate_trace

        try:
            spans, marks, pause_regions, meta, elf_bytes, elf_name = import_sltrace(path)
        except Exception as e:
            self._toast.show_message("Cannot open bundle", str(e), level="error")
            return

        raw = {
            "spans": spans,
            "marks": marks,
            "pause_regions": pause_regions,
            "metadata": meta,
        }
        issues = validate_trace(raw)
        fatal    = [s for s in issues if not s.startswith("Warning:")]
        warnings = [s for s in issues if s.startswith("Warning:")]

        if fatal:
            self._toast.show_message(
                "Trace validation failed", "\n".join(fatal[:3]), level="error"
            )
            return
        if warnings:
            self._toast.show_message(
                "Trace loaded with warnings",
                "\n".join(w.removeprefix("Warning: ") for w in warnings[:2]),
                level="warning",
            )

        if not spans and not marks:
            self._toast.show_message(
                "Empty trace",
                f"{os.path.basename(path)} contains no spans or marks.",
                level="info",
            )
            return

        if elf_bytes and elf_name:
            self._cleanup_temp_elf()
            tmp = tempfile.NamedTemporaryFile(
                suffix=os.path.splitext(elf_name)[1] or ".elf", delete=False
            )
            tmp.write(elf_bytes)
            tmp.close()
            self.elf_path = tmp.name

        self._apply_new_data(
            spans, list(marks), list(pause_regions),
            bool(meta.get("wrapped", False)), preserve_view=False,
        )
        self._add_recent_trace(path)
        self.statusBar().showMessage(
            f"Loaded {len(spans)} spans, {len(marks)} marks "
            f"from {os.path.basename(path)}",
            5000,
        )
        QtCore.QTimer.singleShot(5100, self._update_status_bar)

    def _populate_recent_menu(self):
        """Rebuild the Recent Traces submenu from QSettings."""
        self._recent_menu.clear()
        settings = QtCore.QSettings("IxanaProfiler", "Profiler")
        recent = settings.value("recent_traces", []) or []
        # QSettings on some platforms stores a single string instead of a list
        if isinstance(recent, str):
            recent = [recent]

        if not recent:
            empty = QtGui.QAction("(empty)", self)
            empty.setEnabled(False)
            self._recent_menu.addAction(empty)
            return

        for path in recent:
            name = os.path.basename(path)
            act = QtGui.QAction(name, self)
            act.setToolTip(path)
            # capture p in the default arg so the lambda doesn't close over the loop var
            act.triggered.connect(lambda checked=False, p=path: self._open_trace(p))
            self._recent_menu.addAction(act)

        self._recent_menu.addSeparator()
        clear = QtGui.QAction("Clear", self)
        clear.triggered.connect(self._clear_recent_traces)
        self._recent_menu.addAction(clear)

    def _add_recent_trace(self, path):
        settings = QtCore.QSettings("IxanaProfiler", "Profiler")
        recent = settings.value("recent_traces", []) or []
        if isinstance(recent, str):
            recent = [recent]
        # Move to front, dedupe, cap
        recent = [p for p in recent if p != path]
        recent.insert(0, path)
        recent = recent[:_RECENT_MAX]
        settings.setValue("recent_traces", recent)
        self._populate_recent_menu()

    def _clear_recent_traces(self):
        settings = QtCore.QSettings("IxanaProfiler", "Profiler")
        settings.setValue("recent_traces", [])
        self._populate_recent_menu()

    def _apply_new_data(self, spans, marks, pause_regions, wrapped, *, preserve_view=True):
        """Swap in a fresh (spans, marks, pauses) tuple.

        Shared by `_refresh()` (live J-Link reads) and `_open_trace()` (JSON
        load). When `preserve_view=True` the current scroll position and
        window size are retained if they still fit the new data; otherwise
        the view is reset to the default 1 ms window at t=0.
        """
        if preserve_view:
            preserved_start = self.view_start
            preserved_window = self.window_us

        self.spans = spans
        self.marks = list(marks or [])
        self.pause_regions = list(pause_regions or [])
        self.wrapped = bool(wrapped)
        self._overflow_dismissed = False
        self._compute_extents()
        self._update_color_map()

        # Reset iterator cursor and any leftover highlight
        self._iter_cursor = {"kind": None, "name": None, "index": 0}
        self._clear_iter_highlight()

        # Rebuild Find combobox + completer
        self.search_combo.blockSignals(True)
        self.search_combo.clear()
        for name in self.func_names:
            self.search_combo.addItem(name)
        self.search_combo.setCurrentIndex(-1)
        self.search_combo.blockSignals(False)
        self._update_search_completer()

        # Rebuild docks
        self.summary_dock.set_spans(self.spans, self.color_map)
        self.call_tree_dock.set_spans(self.spans, self.total_us, self.color_map)
        self.call_graph_dock.set_spans(self.spans, self.color_map)
        self.marker_dock.set_marks(self.marks)
        self.marker_dock.set_unit(self.unit_label, self.unit_scale)
        self.top_n_dock.set_spans(self.spans, self.color_map)
        self.top_n_dock.set_unit(self.unit_label, self.unit_scale)
        if hasattr(self, "ribbon_dock") and self.ribbon_dock is not None:
            self.ribbon_dock.set_data(
                self.spans, self.color_map, self.t_min, self.total_us,
            )

        # Rebuild plot + minimap
        self._populate_plot()
        self.minimap.set_data(
            self.spans, self.marks, self.color_map, self.t_min, self.total_us,
            pause_regions=self.pause_regions,
        )
        self._update_overflow_banner()

        # Restore or reset view
        if preserve_view:
            self.view_start = min(
                preserved_start, max(0.0, self.total_us - preserved_window)
            )
            self.window_us = min(preserved_window, max(0.1, self.total_us))
        else:
            self.view_start = 0.0
            self.window_us = (
                min(DEFAULT_WINDOW_US, self.total_us) if self.total_us > 0 else 1.0
            )

        self.window_spin.blockSignals(True)
        self.window_spin.setValue(self.window_us * self.unit_scale)
        self.window_spin.blockSignals(False)

        self._update_view_range()
        self._update_status_bar()

    # ── Unit switching ───────────────────────────────────────────────

    def _set_unit(self, unit):
        self.unit_label = unit
        self.unit_scale = 1.0 if unit == "us" else 0.001

        self.x_axis.unit_scale = self.unit_scale
        self.plot.setLabel("bottom", f"Time ({unit})")
        self.x_axis.update()

        self.window_label.setText(f" Window ({unit}): ")
        self.jump_label.setText(f" Jump to ({unit}): ")

        self.window_spin.blockSignals(True)
        self.window_spin.setValue(self.window_us * self.unit_scale)
        self.window_spin.setSingleStep(10.0 if unit == "us" else 0.01)
        self.window_spin.blockSignals(False)

        self.jump_spin.blockSignals(True)
        self.jump_spin.setValue(self.view_start * self.unit_scale)
        self.jump_spin.blockSignals(False)

        # Update docks so their headers + numeric columns reflect the new unit
        if hasattr(self, "summary_dock") and self.summary_dock is not None:
            self.summary_dock.set_unit(unit, self.unit_scale)
        if hasattr(self, "call_tree_dock") and self.call_tree_dock is not None:
            self.call_tree_dock.set_unit(unit, self.unit_scale)
        if hasattr(self, "marker_dock") and self.marker_dock is not None:
            self.marker_dock.set_unit(unit, self.unit_scale)
        if hasattr(self, "top_n_dock") and self.top_n_dock is not None:
            self.top_n_dock.set_unit(unit, self.unit_scale)
        if hasattr(self, "call_graph_dock") and self.call_graph_dock is not None:
            self.call_graph_dock.set_unit(unit, self.unit_scale)

        self._update_status_bar()
        self._update_measure_label()
        self._update_pick_label()
        self._refresh_hover_label()
        self._update_color_bar()

    # ── Color mode ───────────────────────────────────────────────────

    def _set_color_mode(self, mode):
        self._color_mode = mode
        if hasattr(self, "_flame_item") and self._flame_item is not None:
            self._flame_item.set_color_mode(mode)
            self._update_color_bar()
        self._update_status_bar()

    def _update_color_bar(self):
        """Show/hide the viridis legend based on color mode."""
        if self._color_mode == "duration" and hasattr(self, "_flame_item"):
            self.color_bar.set_range(
                self._flame_item.duration_min_us,
                self._flame_item.duration_max_us,
                self.unit_label,
                self.unit_scale,
            )
            self.color_bar.setVisible(True)
        else:
            self.color_bar.setVisible(False)

    # ── Highlight-on-hover toggle ────────────────────────────────────

    def _toggle_highlight(self, checked):
        self.highlight_hover_enabled = checked
        if not checked and hasattr(self, "_flame_item") and self._flame_item is not None:
            self._flame_item.set_highlighted_name(None)

    def _toggle_bar_labels(self, checked):
        """View > Show Function Names on Bars."""
        if hasattr(self, "_flame_item") and self._flame_item is not None:
            self._flame_item.set_show_bar_labels(checked)

    def _toggle_mark_labels(self, checked):
        """View > Show Marker Names on Chart."""
        if hasattr(self, "_flame_item") and self._flame_item is not None:
            self._flame_item.set_show_mark_labels(checked)

    def _toggle_sticky_hover(self, checked):
        """View > Sticky Hover Label."""
        if hasattr(self, "_flame_item") and self._flame_item is not None:
            self._flame_item.set_show_sticky_hover(checked)
            if checked:
                # Seed with whatever the hover is currently pointing at
                self._refresh_sticky_hover_text()
            else:
                self._flame_item.set_sticky_text("")

    # ── Visibility filter ────────────────────────────────────────────

    def _on_visibility_changed(self, hidden_set):
        if hasattr(self, "_flame_item") and self._flame_item is not None:
            self._flame_item.set_hidden(hidden_set)

    # ── Toolbar handlers ─────────────────────────────────────────────

    def _toggle_exclude_isr(self, checked: bool) -> None:
        """Toggle ISR preemption time exclusion from function timing stats."""
        self._exclude_isr_time = checked
        self.summary_dock.set_exclude_isr(checked)
        self.top_n_dock.set_exclude_isr(checked)

    def _on_window_changed(self, value):
        self.window_us = value / self.unit_scale
        self._update_view_range()

    def _jump_to_time(self):
        self.view_start = max(0, self.jump_spin.value() / self.unit_scale)
        self._update_view_range()

    # ── View range / scrollbar sync ─────────────────────────────────

    def _update_view_range(self):
        if self.window_us >= self.total_us:
            self.view_start = 0.0
            end = self.window_us
        else:
            end = self.view_start + self.window_us
            if end > self.total_us:
                end = self.total_us
                self.view_start = max(0.0, end - self.window_us)

        self.plot.setXRange(self.view_start, end, padding=0)

        scrollable = max(0.0, self.total_us - self.window_us)
        max_scroll = int(scrollable * 10)
        self.scrollbar.blockSignals(True)
        self.scrollbar.setRange(0, max_scroll)
        self.scrollbar.setPageStep(max(1, int(self.window_us * 10)))
        self.scrollbar.setValue(int(self.view_start * 10))
        self.scrollbar.setEnabled(max_scroll > 0)
        self.scrollbar.blockSignals(False)

        # Sync minimap viewport overlay
        if hasattr(self, "minimap"):
            self.minimap.set_viewport(
                self.t_min + self.view_start,
                self.t_min + end,
            )
        if hasattr(self, "ribbon_dock") and self.ribbon_dock is not None:
            self.ribbon_dock.set_viewport(
                self.t_min + self.view_start,
                self.t_min + end,
            )

    def _on_scroll(self, value):
        self.view_start = value / 10.0
        self.plot.setXRange(self.view_start, self.view_start + self.window_us, padding=0)
        if hasattr(self, "minimap"):
            self.minimap.set_viewport(
                self.t_min + self.view_start,
                self.t_min + self.view_start + self.window_us,
            )
        if hasattr(self, "ribbon_dock") and self.ribbon_dock is not None:
            self.ribbon_dock.set_viewport(
                self.t_min + self.view_start,
                self.t_min + self.view_start + self.window_us,
            )

    def _on_minimap_jump(self, start_us_abs):
        """Minimap widget asked us to jump — start_us_abs is absolute us."""
        self.view_start = max(0.0, start_us_abs - self.t_min)
        self._update_view_range()

    def _toggle_minimap(self, checked):
        self.minimap.setVisible(checked)

    def _toggle_grid(self, checked):
        """View > Show Grid — toggle the x-axis grid on the flame chart."""
        self.plot.showGrid(x=checked, y=False, alpha=0.25 if checked else 0)

    def _update_overflow_banner(self):
        if self.wrapped and not self._overflow_dismissed:
            shown = len(self.spans)
            tail = f" and {len(self.marks)} marks." if self.marks else "."
            self.overflow_banner_label.setText(
                f"WARNING: Trace buffer wrapped - oldest events lost. "
                f"Showing the most recent {shown} reconstructed spans" + tail
            )
            self.overflow_banner.setVisible(True)
        else:
            self.overflow_banner.setVisible(False)

    def _dismiss_overflow_banner(self):
        self._overflow_dismissed = True
        self.overflow_banner.setVisible(False)

    def _set_view_start(self, start_us):
        self.view_start = max(0, min(start_us, max(0, self.total_us - self.window_us)))
        self._update_view_range()

    # ── Wheel → horizontal pan ───────────────────────────────────────

    def _plot_wheel_event(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta == 0:
            return
        mods = event.modifiers()
        if mods & QtCore.Qt.KeyboardModifier.ControlModifier:
            # Ctrl+scroll → zoom anchored to the mouse cursor position
            pos = event.position()
            scene_pos = self.plot_widget.mapToScene(
                QtCore.QPoint(int(pos.x()), int(pos.y()))
            )
            anchor_us = self.plot.vb.mapSceneToView(scene_pos).x()
            anchor_us = max(self.view_start,
                            min(anchor_us, self.view_start + self.window_us))
            anchor_frac = (
                (anchor_us - self.view_start) / self.window_us
                if self.window_us > 0 else 0.5
            )
            factor = 0.8 if delta > 0 else 1.25
            new_window = max(0.1, min(self.window_us * factor, self.total_us))
            new_start = anchor_us - anchor_frac * new_window
            self.view_start = max(0.0, min(new_start,
                                           max(0.0, self.total_us - new_window)))
            self.window_us = new_window
            self.window_spin.blockSignals(True)
            self.window_spin.setValue(self.window_us * self.unit_scale)
            self.window_spin.blockSignals(False)
            self._update_view_range()
        else:
            fraction = 0.5 if (mods & QtCore.Qt.KeyboardModifier.ShiftModifier) else 0.1
            pan_us = self.window_us * fraction * (-1 if delta > 0 else 1)
            self.view_start = max(
                0.0,
                min(self.view_start + pan_us, max(0.0, self.total_us - self.window_us)),
            )
            self._update_view_range()
        event.accept()

    # ── Measurement cursors ─────────────────────────────────────────

    def _toggle_cursors(self, checked):
        self.cursors_enabled = checked
        if checked:
            a = self.view_start + self.window_us * 0.25
            b = self.view_start + self.window_us * 0.75
            self.cursor_a.setPos(a)
            self.cursor_b.setPos(b)
        self.cursor_a.setVisible(checked)
        self.cursor_b.setVisible(checked)
        self.cursor_handle_a.setVisible(checked)
        self.cursor_handle_b.setVisible(checked)
        self.measure_label.setVisible(checked)
        self.marker_dock.set_cursors_enabled(checked)
        self.summary_dock.set_cursors_enabled(checked)
        if checked:
            self._update_cursor_handle_pos()
            self._update_measure_label()

    def _on_cursors_moved(self):
        if self.cursors_enabled:
            self._update_measure_label()

    def _update_cursor_handle_pos(self):
        """Pin cursor handles to the top of the visible viewport."""
        if not hasattr(self, "cursor_handle_a"):
            return
        vr = self.plot.getViewBox().viewRect()
        y = vr.top()   # min data Y = visual top with invertY(True)
        self.cursor_handle_a.setPos(self.cursor_a.value(), y)
        self.cursor_handle_b.setPos(self.cursor_b.value(), y)

    def _update_measure_label(self):
        if not self.cursors_enabled:
            return
        a = self.cursor_a.value()
        b = self.cursor_b.value()
        delta = abs(b - a)
        u = self.unit_label
        s = self.unit_scale
        if delta > 0:
            self.measure_label.setText(
                f"A: {a * s:.3f} {u}    "
                f"B: {b * s:.3f} {u}    "
                f"Delta: {delta * s:.3f} {u}    "
                f"({1_000_000 / delta:.1f} Hz)"
            )
        else:
            self.measure_label.setText(
                f"A: {a * s:.3f} {u}    B: {b * s:.3f} {u}    Delta: 0"
            )

    def _snap_cursor_to_time(self, which: str, t_us: float) -> None:
        """Snap cursor A or B to an absolute time, enabling cursors if needed."""
        rel = t_us - self.t_min
        if which == "A":
            self.cursor_a.setValue(rel)
        else:
            self.cursor_b.setValue(rel)
        if not self.cursors_enabled:
            self.act_cursors.setChecked(True)
            self._toggle_cursors(True)
        self._update_measure_label()

    def _snap_cursor_to_function(self, which: str, name: str) -> None:
        """Snap cursor A or B to the first occurrence of a function's start."""
        matches = [sp for sp in self.spans if sp["name"] == name]
        if not matches:
            return
        first = min(matches, key=lambda s: s["start_us"])
        self._snap_cursor_to_time(which, first["start_us"])

    # ── Pick-spans: click two bars, measure between them ───────────

    def _toggle_pick_mode(self, checked):
        self._pick_mode = checked
        self._clear_picks()
        self.pick_label.setVisible(checked)
        if checked:
            self.pick_label.setText("Pick span A (click a bar)")
            self._mode_label.setText("PICK SPANS")
            self._mode_label.setVisible(True)
        else:
            self._mode_label.setText("")
            self._mode_label.setVisible(False)

    # ── Zoom to Selection (drag-to-zoom) ─────────────────────────────

    def _toggle_select_zoom(self, checked):
        self._select_mode = checked
        if checked:
            self.plot_widget.setCursor(QtCore.Qt.CursorShape.CrossCursor)
            self.statusBar().showMessage(
                "Drag horizontally on the chart, release to zoom. Esc to cancel."
            )
            self._mode_label.setText("SELECT-ZOOM")
            self._mode_label.setVisible(True)
        else:
            self.plot_widget.unsetCursor()
            self._cancel_select_overlay()
            self._update_status_bar()
            self._mode_label.setText("")
            self._mode_label.setVisible(False)

    def _plot_mouse_press(self, event):
        # Bookmark pick mode — one-shot left-click to place a bookmark
        if self._bookmark_pick_mode and event.button() == QtCore.Qt.MouseButton.LeftButton:
            t_us  = max(0.0, self._view_x_from_event(event))
            depth = self._depth_from_event(event)
            t_us  = self._snap_to_span_edge(t_us, depth)
            self._toggle_bookmark_pick(False)
            self.bookmark_dock.save_bookmark(t_us, depth)
            event.accept()
            return

        # Right-click: function actions + bookmark
        if event.button() == QtCore.Qt.MouseButton.RightButton:
            t_us  = max(0.0, self._view_x_from_event(event))
            depth = self._depth_from_event(event)
            menu  = QtWidgets.QMenu(self.plot_widget)

            # Hit-test: find the span under the cursor
            vb = self.plot.getViewBox()
            try:
                scene_pt = self.plot_widget.mapToScene(event.position().toPoint())
            except AttributeError:
                scene_pt = self.plot_widget.mapToScene(event.pos())
            pt = vb.mapSceneToView(scene_pt)
            hit = self._find_span_at(depth, pt.x(), pt.y()) if hasattr(self, "_find_span_at") else None

            # Hit-test markers — ±8 px of the vertical line (same approach as bookmarks).
            mark_hit = None
            if hasattr(self, "marks"):
                best_dx = 9
                for _mk in self.marks:
                    _mk_sx = vb.mapViewToScene(QtCore.QPointF(_mk["t_us"] - self.t_min, 0)).x()
                    _dx = abs(scene_pt.x() - _mk_sx)
                    if _dx < best_dx:
                        best_dx = _dx
                        mark_hit = _mk

            # Hit-test bookmarks — work entirely in scene (pixel) space so the
            # label area (which is drawn in screen coords, extending rightward
            # from the line) is also detectable.
            _vb_rect  = vb.sceneBoundingRect()
            # "Label zone": top ~22 px of the viewbox where the 🔖 text sits
            _in_label_zone = (scene_pt.y() - _vb_rect.top()) <= 22
            bm_hit_idx = -1
            for _i, _bm in enumerate(self.bookmark_dock.get_bookmarks()):
                if not _bm.get("visible", True):
                    continue
                # Map bookmark x to scene x
                _bm_sx = vb.mapViewToScene(QtCore.QPointF(_bm["t_us"], 0)).x()
                _dx    = scene_pt.x() - _bm_sx
                # Line hit: ±8 px anywhere on the vertical dashed line
                if abs(_dx) <= 8:
                    bm_hit_idx = _i
                    break
                # Label hit: click is 0–180 px to the right of the line AND
                # within the top label zone
                if _in_label_zone and 0 <= _dx <= 180:
                    bm_hit_idx = _i
                    break

            if bm_hit_idx >= 0:
                _bm_name = self.bookmark_dock.get_bookmarks()[bm_hit_idx]["name"]
                menu.addSection(f"\U0001f516 {_bm_name}")
                act_remove_bm = QtGui.QAction("Remove bookmark", menu)
                act_remove_bm.triggered.connect(
                    lambda *_, _idx=bm_hit_idx: self.bookmark_dock._remove_bookmark(_idx)
                )
                menu.addAction(act_remove_bm)
                menu.addSeparator()

            if mark_hit is not None:
                menu.addSection(f"\U0001f6a9 {mark_hit['name']}")
                for which in ("A", "B"):
                    act = QtGui.QAction(f"Set as Cursor {which}", menu)
                    act.triggered.connect(
                        lambda _=False, w=which, t=mark_hit["t_us"]: self._snap_cursor_to_time(w, t)
                    )
                    menu.addAction(act)
                menu.addSeparator()

            if hit is not None:
                fn_name = hit["name"]
                menu.addSection(fn_name)

                act_summary = QtGui.QAction("Show in Function Summary", menu)
                act_summary.triggered.connect(
                    lambda checked=False, n=fn_name: self._show_function_in_summary(n)
                )
                menu.addAction(act_summary)

                act_ribbon = QtGui.QAction("Add to Function Ribbon", menu)
                act_ribbon.triggered.connect(
                    lambda checked=False, n=fn_name: self._on_ribbon_requested(n)
                )
                menu.addAction(act_ribbon)

                act_jitter = QtGui.QAction("Jitter Analysis…", menu)
                act_jitter.triggered.connect(
                    lambda checked=False, n=fn_name: self._show_jitter_for_function(n)
                )
                menu.addAction(act_jitter)

                if self.cursors_enabled:
                    mid = (hit["start_us"] + hit["end_us"]) / 2
                    snap_t = hit["start_us"] if (pt.x() + self.t_min) <= mid else hit["end_us"]
                    menu.addSeparator()
                    for which in ("A", "B"):
                        act = QtGui.QAction(f"Set as Cursor {which}", menu)
                        act.triggered.connect(
                            lambda _=False, w=which, t=snap_t: self._snap_cursor_to_time(w, t)
                        )
                        menu.addAction(act)

                menu.addSeparator()

            add_bm = QtGui.QAction("\U0001f516  Add bookmark here", menu)
            snapped = (mark_hit["t_us"] - self.t_min) if mark_hit is not None else self._snap_to_span_edge(t_us, depth)
            add_bm.triggered.connect(
                lambda *_, _t=snapped, _d=depth: self.bookmark_dock.save_bookmark(_t, _d)
            )
            menu.addAction(add_bm)

            try:
                menu.exec(event.globalPosition().toPoint())
            except AttributeError:
                menu.exec(event.globalPos())
            event.accept()
            return

        # Shift + Left-drag: mode-less region zoom (alternative to the
        # toolbar Select Zoom action). Uses the same overlay + release
        # machinery but doesn't require entering a mode.
        is_shift_drag = (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)
            and not self._select_mode
        )
        if self._select_mode and event.button() == QtCore.Qt.MouseButton.LeftButton:
            vp = self._view_x_from_event(event)
            self._select_start_x = vp
            self._select_overlay = pg.LinearRegionItem(
                values=[vp, vp],
                movable=False,
                brush=(255, 200, 0, SELECTION_FILL_ALPHA),
                pen=pg.mkPen(THEME["selection"], width=1),
            )
            self._select_overlay.setZValue(Z_SELECTION)
            self.plot.addItem(self._select_overlay)
            event.accept()
            return
        if is_shift_drag:
            vp = self._view_x_from_event(event)
            self._select_start_x = vp
            self._shift_drag_active = True
            self._select_overlay = pg.LinearRegionItem(
                values=[vp, vp],
                movable=False,
                brush=(255, 200, 0, SELECTION_FILL_ALPHA),
                pen=pg.mkPen(THEME["selection"], width=1),
            )
            self._select_overlay.setZValue(Z_SELECTION)
            self.plot.addItem(self._select_overlay)
            self.plot_widget.setCursor(QtCore.Qt.CursorShape.CrossCursor)
            event.accept()
            return
        self._orig_mouse_press(event)

    def _plot_mouse_move(self, event):
        if (self._select_mode or getattr(self, "_shift_drag_active", False)) and self._select_start_x is not None:
            vp = self._view_x_from_event(event)
            self._select_overlay.setRegion([self._select_start_x, vp])
            event.accept()
            return
        self._orig_mouse_move(event)

    def _plot_mouse_release(self, event):
        if (
            (self._select_mode or getattr(self, "_shift_drag_active", False))
            and self._select_start_x is not None
            and event.button() == QtCore.Qt.MouseButton.LeftButton
        ):
            vp = self._view_x_from_event(event)
            x1 = max(0.0, min(self._select_start_x, vp))
            x2 = min(self.total_us, max(self._select_start_x, vp))
            width = x2 - x1
            was_shift_drag = getattr(self, "_shift_drag_active", False)
            self._cancel_select_overlay()

            # Only apply if the selection is meaningful (avoid accidental zero-width clicks)
            min_width = max(1e-3, 1e-6 * self.total_us)
            if width >= min_width:
                self.view_start = x1
                self.window_us = width
                self.window_spin.blockSignals(True)
                self.window_spin.setValue(width * self.unit_scale)
                self.window_spin.blockSignals(False)
                self._update_view_range()

            if was_shift_drag:
                self._shift_drag_active = False
                self.plot_widget.unsetCursor()
            else:
                # Auto-exit toolbar select mode (one-shot)
                self.act_select_zoom.setChecked(False)
                self._toggle_select_zoom(False)
            event.accept()
            return
        self._orig_mouse_release(event)

    def _view_x_from_event(self, event):
        """Map a QMouseEvent position to data-coordinate X (absolute microseconds)."""
        vb = self.plot.getViewBox()
        # event.pos() is in widget coords; map to scene then view
        try:
            scene_pt = self.plot_widget.mapToScene(event.position().toPoint())
        except AttributeError:
            # Older PySide6 compat
            scene_pt = self.plot_widget.mapToScene(event.pos())
        return vb.mapSceneToView(scene_pt).x()

    def _cancel_select_overlay(self):
        if self._select_overlay is not None:
            try:
                self.plot.removeItem(self._select_overlay)
            except Exception:
                pass
            self._select_overlay = None
        self._select_start_x = None

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Escape:
            if getattr(self, "_shortcut_overlay", None) is not None and self._shortcut_overlay.isVisible():
                self._shortcut_overlay.hide()
                event.accept()
                return
            if self._bookmark_pick_mode:
                self._toggle_bookmark_pick(False)
                event.accept()
                return
            if self._select_mode:
                self.act_select_zoom.setChecked(False)
                self._toggle_select_zoom(False)
                event.accept()
                return
            # Esc also clears any persistent flame-chart highlight (e.g.
            # set by the hover-highlight toggle).
            if hasattr(self, "_flame_item") and self._flame_item is not None:
                self._flame_item.set_highlighted_name(None)
        super().keyPressEvent(event)

    def _install_pan_zoom_shortcuts(self):
        """Register keyboard pan/zoom as QShortcuts so they fire from anywhere
        in the window (not blocked by the plot widget's QGraphicsView)."""
        ctx = QtCore.Qt.ShortcutContext.WindowShortcut

        def sc(keys, handler):
            """Register one shortcut (or alternate set of keys) to `handler`."""
            for key in keys if isinstance(keys, (list, tuple)) else [keys]:
                s = QtGui.QShortcut(QtGui.QKeySequence(key), self)
                s.setContext(ctx)
                s.activated.connect(handler)

        # Pan left / right — 10%
        sc("Left",        lambda: self._pan_by_fraction(-0.1))
        sc("Right",       lambda: self._pan_by_fraction(0.1))
        # Fast pan — 50%
        sc("Shift+Left",  lambda: self._pan_by_fraction(-0.5))
        sc("Shift+Right", lambda: self._pan_by_fraction(0.5))
        # Fine pan — 1%
        sc("[",           lambda: self._pan_by_fraction(-0.01))
        sc("]",           lambda: self._pan_by_fraction(0.01))

        # Zoom in/out via '+' and '-': handled by an app-level text filter so
        # they work regardless of focus and keyboard layout (QShortcut is
        # unreliable for shifted keys like '+').
        self._zoom_filter = _ZoomKeyFilter(
            zoom_in=lambda: self._scale_window(0.5),
            zoom_out=lambda: self._scale_window(2.0),
            parent=self,
        )
        QtWidgets.QApplication.instance().installEventFilter(self._zoom_filter)

    def _pan_by_fraction(self, frac):
        """Shift view_start by `frac * window_us`. Negative = left, positive = right."""
        if self.total_us <= 0:
            return
        new_start = self.view_start + self.window_us * frac
        max_start = max(0.0, self.total_us - self.window_us)
        self.view_start = max(0.0, min(new_start, max_start))
        self._update_view_range()

    def _scale_window(self, factor):
        """Multiply window_us by `factor` (0.5 = zoom in, 2.0 = zoom out),
        keeping the current view center fixed."""
        if self.total_us <= 0:
            return
        center = self.view_start + self.window_us / 2
        new_window = max(0.1, min(self.window_us * factor, self.total_us))
        self.window_us = new_window
        self.view_start = max(0.0, min(center - new_window / 2,
                                       max(0.0, self.total_us - new_window)))
        self.window_spin.blockSignals(True)
        self.window_spin.setValue(self.window_us * self.unit_scale)
        self.window_spin.blockSignals(False)
        self._update_view_range()

    def showEvent(self, event):
        """Override to re-grab keyboard focus on the very first show.

        Some desktops reset focus during Qt's first showEvent pass; deferring
        the setFocus call to the next event-loop iteration via a 0-ms timer
        is the standard idiom for "override Qt's initial focus".
        """
        super().showEvent(event)
        if not getattr(self, "_initial_focus_done", False):
            self._initial_focus_done = True
            QtCore.QTimer.singleShot(0, self.plot_widget.setFocus)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep the shortcut overlay matched to the full window area so it
        # can re-centre its panel on the new geometry.
        if getattr(self, "_shortcut_overlay", None) is not None:
            self._shortcut_overlay.setGeometry(self.rect())
        # Keep the toast pinned to the bottom-right corner.
        if getattr(self, "_toast", None) is not None and self._toast.isVisible():
            self._toast.reposition()

    def _on_plot_clicked(self, ev):
        if not self._pick_mode:
            return
        if ev.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        vb = self.plot.getViewBox()
        pt = vb.mapSceneToView(ev.scenePos())
        x_rel = pt.x()
        y = pt.y()
        depth = int(y)
        hit = self._find_span_at(depth, x_rel, y)
        if not hit:
            return

        if self._picked_a is None or self._picked_b is not None:
            # Start a fresh pair
            self._clear_picks()
            self._picked_a = hit
            self._pick_overlay_a = self._draw_pick_overlay(hit, "a")
        else:
            self._picked_b = hit
            self._pick_overlay_b = self._draw_pick_overlay(hit, "b")

        self._update_pick_label()

    def _draw_pick_overlay(self, sp, which):
        """Add a translucent highlight rectangle + A/B badge over the picked span."""
        border = QtGui.QColor(THEME["pick_a"] if which == "a" else THEME["pick_b"])
        fill = QtGui.QColor(border)
        fill.setAlpha(110)

        x0 = sp["start_us"] - self.t_min
        w = max(sp["duration_us"], 0.1)

        rect = QtWidgets.QGraphicsRectItem(x0, sp["depth"], w, ROW_HEIGHT)
        rect.setBrush(QtGui.QBrush(fill))
        pen = QtGui.QPen(border)
        pen.setWidthF(0)
        rect.setPen(pen)
        rect.setZValue(Z_PICK)
        self.plot.addItem(rect)

        # Bold "A" / "B" badge centred on the bar so the user always knows
        # which pick is which directly on the flame chart.
        badge_font = QtGui.QFont()
        badge_font.setBold(True)
        badge_font.setPointSize(8)
        pick_color = THEME["pick_a"] if which == "a" else THEME["pick_b"]
        badge = pg.TextItem(
            text="A" if which == "a" else "B",
            color=pick_color,
            anchor=(0.5, 0.5),
            fill=pg.mkBrush(0, 0, 0, 210),              # dark background
            border=pg.mkPen(pick_color, width=1),        # coloured outline
        )
        badge.setFont(badge_font)
        badge.setPos(x0 + w * 0.5, sp["depth"] + ROW_HEIGHT * 0.5)
        badge.setZValue(Z_PICK + 1)
        self.plot.addItem(badge)
        setattr(self, f"_pick_label_{which}", badge)

        return rect

    def _clear_picks(self):
        for attr in ("_pick_overlay_a", "_pick_overlay_b",
                     "_pick_label_a", "_pick_label_b"):
            o = getattr(self, attr, None)
            if o is not None:
                try:
                    self.plot.removeItem(o)
                except Exception:
                    pass
            setattr(self, attr, None)
        self._picked_a = None
        self._picked_b = None

    def _update_pick_label(self):
        u = self.unit_label
        s = self.unit_scale
        if self._picked_a is None:
            self.pick_label.setText("Pick span A (click a bar)")
            return
        a = self._picked_a
        if self._picked_b is None:
            self.pick_label.setText(
                f"<b>A:</b> {a['name']} "
                f"[{(a['start_us']-self.t_min)*s:.3f} to {(a['end_us']-self.t_min)*s:.3f} {u}]"
                f" &nbsp;&nbsp; - &nbsp;&nbsp; now pick span B"
            )
            return
        b = self._picked_b
        a_start = (a["start_us"] - self.t_min) * s
        a_end = (a["end_us"] - self.t_min) * s
        b_start = (b["start_us"] - self.t_min) * s
        b_end = (b["end_us"] - self.t_min) * s

        gap = (b["start_us"] - a["end_us"]) * s
        start_to_start = (b["start_us"] - a["start_us"]) * s
        full = (b["end_us"] - a["start_us"]) * s

        self.pick_label.setText(
            f"<b>A:</b> {a['name']} [{a_start:.3f} - {a_end:.3f} {u}]   "
            f"<b>B:</b> {b['name']} [{b_start:.3f} - {b_end:.3f} {u}]<br>"
            f"<b>A -&gt; B gap:</b> {gap:.3f} {u}   "
            f"<b>start -&gt; start:</b> {start_to_start:.3f} {u}   "
            f"<b>A.start -&gt; B.end:</b> {full:.3f} {u}"
        )

    # ── Navigation actions ───────────────────────────────────────────

    def _on_search_activated(self, idx):
        if idx < 0:
            return
        name = self.search_combo.itemText(idx)
        if name:
            self._jump_to_function_name(name)

    def _on_search_return_pressed(self):
        """Enter key in the Find combobox — jump to (or advance through)
        whatever the user typed."""
        text = self.search_combo.currentText().strip()
        if not text:
            return
        if text in self.func_names:
            self._jump_to_function_name(text)
        else:
            self.statusBar().showMessage(f"No function named '{text}'", 3000)

    def _update_search_completer(self):
        """Refresh the Find combo's completer with the current func_names.

        Called whenever spans change (refresh or Open Trace) so the
        autocomplete list stays in sync.
        """
        if hasattr(self, "_search_completer") and self._search_completer is not None:
            model = QtCore.QStringListModel(list(self.func_names), self._search_completer)
            self._search_completer.setModel(model)

    def _jump_to_function_name(self, name):
        """Center on the next occurrence of ``name``, cycling through matches."""
        matches = sorted(
            [sp for sp in self.spans if sp["name"] == name],
            key=lambda s: s["start_us"],
        )
        if not matches:
            return

        c = self._iter_cursor
        if c["kind"] == "function" and c["name"] == name:
            c["index"] = (c["index"] + 1) % len(matches)
        else:
            c["kind"] = "function"
            c["name"] = name
            c["index"] = 0

        sp = matches[c["index"]]
        center = (sp["start_us"] + sp["end_us"]) / 2 - self.t_min
        self.view_start = max(0, center - self.window_us / 2)
        self._update_view_range()
        self._flash_span_highlight(sp)
        self._show_iter_status(name, c["index"] + 1, len(matches), sp["start_us"] - self.t_min)

    def _show_function_in_summary(self, name: str) -> None:
        """Raise the Function Summary dock and scroll to *name*'s row."""
        self.summary_dock.show()
        self.summary_dock.raise_()
        self.summary_dock.scroll_to_function(name)

    def _on_ribbon_requested(self, name):
        """Context menu: Show in Ribbon View → pin a ribbon for ``name``."""
        if hasattr(self, "ribbon_dock") and self.ribbon_dock is not None:
            self.ribbon_dock.add_function(name)
            self.ribbon_dock.show()
            self.ribbon_dock.raise_()

    def _on_ribbon_tick_clicked(self, name, start_us):
        """A tick inside a ribbon row was clicked — centre the main chart
        on that exact span occurrence, keeping current zoom width, then
        flash the span so the user sees where it is.

        Uses a one-shot flash (like Top-N's click path) rather than
        ``set_highlighted_name`` — the latter is persistent and dims every
        other bar on the chart, which has no obvious way to exit.
        """
        rel_start = start_us - self.t_min
        new_start = max(0.0, rel_start - self.window_us / 2)
        max_start = max(0.0, self.total_us - self.window_us)
        self.view_start = min(new_start, max_start)
        self._update_view_range()
        # Find the actual span dict for a proper flash rectangle
        for sp in self.spans:
            if sp["name"] == name and sp["start_us"] == start_us:
                self._flash_span_highlight(sp)
                break

    def _jump_to_span_instance(self, sp):
        """Center on a specific span instance (used by the Top-N dock)."""
        if not sp:
            return
        center = (sp["start_us"] + sp["end_us"]) / 2 - self.t_min
        self.view_start = max(0, center - self.window_us / 2)
        self._update_view_range()
        self._flash_span_highlight(sp)
        u = self.unit_label
        s = self.unit_scale
        self.statusBar().showMessage(
            f"{sp['name']}  duration={sp['duration_us'] * s:.3f} {u}  "
            f"@ {(sp['start_us'] - self.t_min) * s:.3f} {u}",
            4000,
        )
        QtCore.QTimer.singleShot(4100, self._update_status_bar)

    def _jump_to_mark_name(self, name):
        """Center on the next occurrence of a marker, cycling through matches."""
        matches = sorted(
            [m for m in self.marks if m["name"] == name],
            key=lambda m: m["t_us"],
        )
        if not matches:
            return

        c = self._iter_cursor
        if c["kind"] == "mark" and c["name"] == name:
            c["index"] = (c["index"] + 1) % len(matches)
        else:
            c["kind"] = "mark"
            c["name"] = name
            c["index"] = 0

        mk = matches[c["index"]]
        center = mk["t_us"] - self.t_min
        self.view_start = max(0, center - self.window_us / 2)
        self._update_view_range()
        self._flash_mark_highlight(mk)
        self._show_iter_status(name, c["index"] + 1, len(matches), mk["t_us"] - self.t_min)

    def _show_iter_status(self, name, index, total, t_rel_us):
        """Show `foo (3/10) @ 1.234 ms` in the status bar for 4 seconds."""
        u = self.unit_label
        s = self.unit_scale
        msg = f"{name}  ({index}/{total})    @ {t_rel_us * s:.3f} {u}"
        self.statusBar().showMessage(msg, 4000)
        # Restore the default status line 100 ms after the timed message expires
        QtCore.QTimer.singleShot(4100, self._update_status_bar)

    # ── Iteration highlight overlay ─────────────────────────────────

    def _flash_highlight_rect(self, x_rel, y, width, height):
        """Draw a high-contrast outlined rectangle that pulses then fades.

        Uses a thick white outline so it stays visible regardless of the
        underlying bar color (the palette includes yellow shades that would
        hide a yellow highlight). A short alternating pulse (white <-> red)
        catches the eye, then the rectangle settles into a steady white
        outline for a moment before being removed.

        Removes any previous highlight first so successive iterations don't
        accumulate overlays.
        """
        self._clear_iter_highlight()

        rect = QtWidgets.QGraphicsRectItem(x_rel, y, max(width, 1e-6), height)
        # Slight dark dim over the bar so the outline pops
        fill = QtGui.QColor(*CANVAS_DIM_RGBA)
        rect.setBrush(QtGui.QBrush(fill))

        pen = QtGui.QPen(QtGui.QColor(THEME["text_white"]))
        pen.setWidth(3)
        pen.setCosmetic(True)  # 3 screen pixels regardless of zoom
        rect.setPen(pen)
        rect.setZValue(Z_HIGHLIGHT)
        self.plot.addItem(rect)
        self._iter_highlight_item = rect

        # Pulse animation: alternate outline color white <-> red 6 times
        # (≈ 720 ms of attention-grabbing flashing), then steady white for
        # another 1.2 s, then remove. Total visible time ≈ 1.9 s.
        self._iter_highlight_step = 0
        timer = QtCore.QTimer(self)
        timer.timeout.connect(self._iter_highlight_tick)
        timer.start(120)
        self._iter_highlight_timer = timer

    def _iter_highlight_tick(self):
        """Pulse the highlight outline color, then settle, then remove."""
        if self._iter_highlight_item is None:
            return
        self._iter_highlight_step += 1
        step = self._iter_highlight_step

        if step <= 6:
            # Alternate red <-> white outline
            color = "#ff3030" if (step % 2 == 1) else "#ffffff"
            pen = self._iter_highlight_item.pen()
            pen.setColor(QtGui.QColor(color))
            pen.setWidth(3)
            pen.setCosmetic(True)
            self._iter_highlight_item.setPen(pen)
        elif step >= 16:
            # Done — clean up (≈ 6×120 ms pulse + 10×120 ms steady = 1.92 s)
            self._clear_iter_highlight()

    def _flash_span_highlight(self, sp):
        """Highlight an instrumented span (handles ISR display lane)."""
        x_rel = sp["start_us"] - self.t_min
        if sp.get("ipsr", 0) == 0:
            y = sp["depth"]
        else:
            y = -(sp["depth"] + 1)
        self._flash_highlight_rect(x_rel, y, max(sp["duration_us"], 0.1), ROW_HEIGHT)

    def _flash_mark_highlight(self, mk):
        """Highlight a TRACE_MARK by lighting up its line, not a vertical band."""
        x_rel = mk["t_us"] - self.t_min
        y_top = self.min_display_y - 0.2
        y_bot = self.max_display_y + 1.0
        self._flash_highlight_line(x_rel, y_top, y_bot)

    def _flash_highlight_line(self, x_rel, y_top, y_bot):
        """Draw a thick cosmetic vertical line that pulses then fades.

        Used for marker iteration (no vertical box). Reuses the existing
        pulse animation by storing a QGraphicsLineItem in _iter_highlight_item.
        """
        self._clear_iter_highlight()

        line = QtWidgets.QGraphicsLineItem(x_rel, y_top, x_rel, y_bot)
        pen = QtGui.QPen(QtGui.QColor(THEME["text_white"]))
        pen.setWidth(4)
        pen.setCosmetic(True)
        line.setPen(pen)
        line.setZValue(Z_HIGHLIGHT)
        self.plot.addItem(line)
        self._iter_highlight_item = line

        # Reuse the same pulse loop the rectangle uses
        self._iter_highlight_step = 0
        timer = QtCore.QTimer(self)
        timer.timeout.connect(self._iter_highlight_tick)
        timer.start(120)
        self._iter_highlight_timer = timer

    def _clear_iter_highlight(self):
        if self._iter_highlight_item is not None:
            try:
                self.plot.removeItem(self._iter_highlight_item)
            except Exception:
                pass
            self._iter_highlight_item = None
        if self._iter_highlight_timer is not None:
            self._iter_highlight_timer.stop()
            self._iter_highlight_timer = None
        self._iter_highlight_step = 0

    def _iter_next(self):
        """F3 — advance the current iterator cursor (same function/mark)."""
        c = self._iter_cursor
        if c["kind"] is None or c["name"] is None:
            return
        # Re-call the same jump method, which auto-advances since the
        # kind+name match.
        if c["kind"] == "function":
            self._jump_to_function_name(c["name"])
        else:
            self._jump_to_mark_name(c["name"])

    def _iter_prev(self):
        """Shift+F3 — step the cursor backwards with wrap-around."""
        c = self._iter_cursor
        if c["kind"] is None or c["name"] is None:
            return
        if c["kind"] == "function":
            matches = sorted(
                [sp for sp in self.spans if sp["name"] == c["name"]],
                key=lambda s: s["start_us"],
            )
            if not matches:
                return
            c["index"] = (c["index"] - 2) % len(matches)  # -2 because _jump adds +1
            self._jump_to_function_name(c["name"])
        else:
            matches = sorted(
                [m for m in self.marks if m["name"] == c["name"]],
                key=lambda m: m["t_us"],
            )
            if not matches:
                return
            c["index"] = (c["index"] - 2) % len(matches)
            self._jump_to_mark_name(c["name"])

    def _on_mark_visibility_changed(self, hidden_set):
        """Propagate hidden marker names to the flame chart and minimap."""
        if hasattr(self, "_flame_item") and self._flame_item is not None:
            self._flame_item.set_hidden_marks(hidden_set)
        if hasattr(self, "minimap"):
            self.minimap.set_hidden_marks(hidden_set)

    def _show_jitter_for_function(self, name):
        """Open the JitterDialog for a function. Uses span start_us as event times."""
        times = sorted(sp["start_us"] for sp in self.spans if sp["name"] == name)
        if len(times) < 2:
            QtWidgets.QMessageBox.information(
                self,
                "Period / Jitter",
                f"'{name}' has only {len(times)} occurrence(s). "
                "Need at least 2 to compute periods.",
            )
            return
        dlg = JitterDialog(name, times, self.unit_label, self.unit_scale, parent=self)
        dlg.exec()

    def _show_jitter_for_marker(self, name):
        """Open the JitterDialog for a marker. Uses mark t_us as event times."""
        times = sorted(m["t_us"] for m in self.marks if m["name"] == name)
        if len(times) < 2:
            QtWidgets.QMessageBox.information(
                self,
                "Period / Jitter",
                f"Marker '{name}' has only {len(times)} occurrence(s). "
                "Need at least 2 to compute periods.",
            )
            return
        dlg = JitterDialog(name, times, self.unit_label, self.unit_scale, parent=self)
        dlg.exec()

    def _reset_view(self):
        self.window_us = min(DEFAULT_WINDOW_US, self.total_us) if self.total_us > 0 else 1.0
        self.window_spin.blockSignals(True)
        self.window_spin.setValue(self.window_us * self.unit_scale)
        self.window_spin.blockSignals(False)
        self.view_start = 0.0
        self._update_view_range()

    def _fit_all(self):
        if self.total_us <= 0:
            return
        self.window_us = self.total_us
        self.window_spin.blockSignals(True)
        self.window_spin.setValue(self.window_us * self.unit_scale)
        self.window_spin.blockSignals(False)
        self.view_start = 0.0
        self._update_view_range()

    # ── Hover handling ───────────────────────────────────────────────

    def _on_mouse_moved(self, pos):
        self._last_mouse_pos = pos
        if not self._hover_timer.isActive():
            self._hover_timer.start()

    def _do_hover_update(self):
        pos = self._last_mouse_pos
        if pos is None:
            return
        vb = self.plot.getViewBox()
        if not vb.sceneBoundingRect().contains(pos):
            return

        mouse_pt = vb.mapSceneToView(pos)
        x_rel = mouse_pt.x()
        y = mouse_pt.y()
        depth = int(y)

        hit = self._find_span_at(depth, x_rel, y)
        self._last_hit = hit

        # Mark hover detection — tolerance is 5 px in current X transform
        self._last_mark = None
        if hasattr(self, "_flame_item") and self._flame_item is not None:
            # Convert 5 pixels → data units using the view box mapping
            vb_rect = vb.viewRect()
            px_per_unit = vb.width() / max(vb_rect.width(), 1e-9)
            if px_per_unit > 0:
                tolerance = 5.0 / px_per_unit
                self._last_mark = self._flame_item.nearest_mark(x_rel, tolerance)

        # Pause region hover detection — checks whether the cursor is inside
        # a paused interval
        self._last_pause = self._find_pause_at(x_rel)

        self._refresh_hover_label()
        self._refresh_sticky_hover_text()

        # Highlight all instances of the hovered function
        if self.highlight_hover_enabled and hasattr(self, "_flame_item") and self._flame_item is not None:
            self._flame_item.set_highlighted_name(hit["name"] if hit else None)

    def _refresh_sticky_hover_text(self):
        """Push a short ``name  duration`` string into the flame item for
        the sticky in-chart hover pill. No-op if the toggle is off."""
        if not hasattr(self, "_flame_item") or self._flame_item is None:
            return
        if not getattr(self, "act_sticky_hover", None) or not self.act_sticky_hover.isChecked():
            return
        u = self.unit_label
        s = self.unit_scale
        hit = getattr(self, "_last_hit", None)
        if hit:
            txt = f"{hit['name']}   {hit['duration_us'] * s:.3f} {u}"
        else:
            txt = ""
        self._flame_item.set_sticky_text(txt)

    def _find_pause_at(self, x_rel):
        """Return the pause region that contains the cursor's X, or None."""
        x_abs = x_rel + self.t_min
        for r in self.pause_regions:
            if r["start_us"] <= x_abs <= r["end_us"]:
                return r
        return None

    def _refresh_hover_label(self):
        u = self.unit_label
        s = self.unit_scale

        # Pause region takes priority — show "PAUSED" details when over a band
        pause = getattr(self, "_last_pause", None)
        if pause is not None:
            start_rel = (pause["start_us"] - self.t_min) * s
            end_rel = (pause["end_us"] - self.t_min) * s
            dur = (pause["end_us"] - pause["start_us"]) * s
            self.hover_label.setText(
                f"<b>PAUSED</b>  |  "
                f"Start: {start_rel:.3f} {u}  |  "
                f"End: {end_rel:.3f} {u}  |  "
                f"Duration: {dur:.3f} {u}"
            )
            return

        # Marks take precedence in the hover label — they're the rarer event
        mark = getattr(self, "_last_mark", None)
        if mark is not None:
            # nearest_mark returns flame-internal form ({"x","name","ipsr"});
            # raw marks use ("t_us","name","ipsr"). Handle both.
            if "x" in mark:
                t_rel = mark["x"]
            else:
                t_rel = mark["t_us"] - self.t_min
            t = t_rel * s
            ctx = f" (ISR {mark['ipsr']})" if mark.get("ipsr", 0) else ""
            mark_color = THEME["selection"]
            self.hover_label.setText(
                f"<b>MARK:</b> <span style='color:{mark_color}'>{mark['name']}</span>"
                f"  @ {t:.3f} {u}{ctx}"
            )
            return

        hit = getattr(self, "_last_hit", None)
        if not hit:
            self.hover_label.setText("Hover over a bar to see details")
            return
        ipsr = hit.get("ipsr", 0)
        ctx = f"  |  ISR {ipsr}" if ipsr else ""
        self.hover_label.setText(
            f"<b>{hit['name']}</b>  |  "
            f"Duration: {hit['duration_us'] * s:.3f} {u}  |  "
            f"Start: {(hit['start_us'] - self.t_min) * s:.3f} {u}  |  "
            f"End: {(hit['end_us'] - self.t_min) * s:.3f} {u}  |  "
            f"Depth: {hit['depth']}  |  "
            f"Addr: 0x{hit['addr']:08X}"
            + ctx
        )

    def _find_span_at(self, depth, x_rel, y):
        """Locate a span under the cursor, accounting for thread + ISR lanes."""
        # Hidden spans are not drawn, so they must not be hittable either.
        hidden = (
            self._flame_item._hidden
            if hasattr(self, "_flame_item") and self._flame_item
            else set()
        )
        x_abs = x_rel + self.t_min
        # Thread lane hit: depth >= 0 and within ROW_HEIGHT of a depth row
        if depth >= 0 and (depth <= y <= depth + ROW_HEIGHT):
            for sp in self.spans:
                if sp.get("ipsr", 0) != 0:
                    continue
                if sp["depth"] != depth:
                    continue
                if sp["name"] in hidden:
                    continue
                if sp["start_us"] <= x_abs <= sp["end_us"]:
                    return sp
            return None

        # ISR lane hit: y negative. display_y = -(depth + 1) → depth_isr = -int(y)-1
        if y < 0:
            for sp in self.spans:
                if sp.get("ipsr", 0) == 0:
                    continue
                disp_y = -(sp["depth"] + 1)
                if disp_y <= y <= disp_y + ROW_HEIGHT:
                    if sp["name"] in hidden:
                        continue
                    if sp["start_us"] <= x_abs <= sp["end_us"]:
                        return sp
        return None


_ICON_PATH = os.path.join(os.path.dirname(__file__), "icons", "icon.svg")


def _apply_dark_title_bar(widget):
    """Force the Windows native title bar into dark mode so it matches
    the rest of the UI. No-op on non-Windows platforms or older Windows
    versions that don't support the DWM attribute."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20          # Windows 10 20H1+
        DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19      # older builds
        value = ctypes.c_int(1)
        dwmapi = ctypes.windll.dwmapi
        # Try the current attribute first; fall back to the legacy one
        # on older Windows 10 builds.
        res = dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        if res != 0:
            dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_USE_IMMERSIVE_DARK_MODE_OLD,
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
    except Exception:
        # Dark title bar is cosmetic; never let it block startup.
        pass


def show_gui(
    spans,
    marks=None,
    pause_regions=None,
    wrapped=False,
    refresh_fn=None,
    elf_path=None,
    cpu_mhz=96.0,
):
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv)
    # Application icon — shown in the taskbar, window title bar, and
    # Alt+Tab switcher.  Pre-render the SVG at several sizes so Windows
    # always gets a crisp bitmap rather than scaling a single raster.
    if os.path.exists(_ICON_PATH):
        from PySide6.QtSvg import QSvgRenderer
        renderer = QSvgRenderer(_ICON_PATH)
        icon = QtGui.QIcon()
        for sz in (16, 24, 32, 48, 64, 128, 256):
            pm = QtGui.QPixmap(sz, sz)
            pm.fill(QtCore.Qt.GlobalColor.transparent)
            painter = QtGui.QPainter(pm)
            renderer.render(painter)
            painter.end()
            icon.addPixmap(pm)
        app.setWindowIcon(icon)
    else:
        icon = None
    win = ProfilerWindow(
        spans,
        marks=marks,
        pause_regions=pause_regions,
        wrapped=wrapped,
        refresh_fn=refresh_fn,
        elf_path=elf_path,
        cpu_mhz=cpu_mhz,
    )
    if icon is not None:
        win.setWindowIcon(icon)
    win.show()
    # Apply dark title bar AFTER show() — DWM needs a valid HWND.
    _apply_dark_title_bar(win)
    app.exec()
