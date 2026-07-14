"""Call-Graph dock — interactive node-link diagram of the aggregated call tree.

Shows which function calls which, with boxes sized/coloured by function colour
and labelled with call count + inclusive time. Bezier edges with arrowheads
connect callers to callees.

Interaction:
- Click a node  → emits ``function_clicked(name)`` → main window jumps to it.
- Ctrl+scroll   → zoom anchored to mouse cursor.
- "Fit" button  → reset zoom to show all nodes.

Rendering: every node box and every edge is drawn in a single paint() call on
one batched _CallGraphLayer item (mirroring flame_item.py's approach), instead
of one QGraphicsObject per node plus two QGraphicsItems per edge. Click/hover
hit-testing is done manually (row/x-range lookup) since there are no
per-node QGraphicsItems left for Qt to dispatch events to.
"""

import math
from bisect import bisect_right

from PySide6 import QtCore, QtGui, QtWidgets

from span_builder import build_call_tree

from .dock_base import DockBase
from .theme import THEME

# ── Layout constants ──────────────────────────────────────────────────
NODE_W    = 160     # node box width  (px)
NODE_H    = 52      # node box height (px)
STRIDE_X  = 196     # horizontal distance between node centres
STRIDE_Y  = 110     # vertical   distance between node centres (row spacing)

# ── Visual constants ──────────────────────────────────────────────────
NODE_ALPHA  = 160   # background fill alpha (0-255)
EDGE_WIDTH  = 1.5
ARROW_SIZE  = 8     # filled arrowhead half-width in px
# All colors are read from THEME at paint/build time so theme switches take effect.


# ═══════════════════════════════════════════════════════════════════════
# Layout helpers
# ═══════════════════════════════════════════════════════════════════════

def _compute_width(node: dict, widths: dict) -> float:
    """Post-order: return the column-unit width of this subtree.

    Each node occupies at least 1 unit; an internal node occupies the sum
    of its children's widths (with 0.4-unit gaps between siblings).

    ``widths`` (keyed by ``id(node)``) holds the result instead of writing
    it onto ``node`` itself, so the aggregated call tree (which may be
    shared with other docks, e.g. CallTreeDock, computed once from the
    same spans) is never mutated by layout.
    """
    children = list(node["children"].values())
    if not children:
        widths[id(node)] = 1.0
        return 1.0
    total = sum(_compute_width(c, widths) for c in children)
    # gaps between N children: (N-1) * 0.4
    total += (len(children) - 1) * 0.4
    widths[id(node)] = total
    return total


def _assign_positions(node: dict, cx: float, row: int, positions: dict, path: str, widths: dict) -> None:
    """Pre-order: assign pixel (x, y) to every node.

    ``positions`` is filled with ``path → (px_x, px_y)`` where the coordinates
    are the *top-left* corner of the node box.  ``path`` is the parent-chain
    joined by '/' so identical function names at different call sites map to
    separate entries. ``widths`` is the id(node)->width map _compute_width()
    built for this same tree.
    """
    px_x = cx * STRIDE_X - NODE_W / 2
    px_y = row * STRIDE_Y
    positions[path] = QtCore.QPointF(px_x, px_y)

    children = list(node["children"].values())
    if not children:
        return

    # Centre children under this node
    total_child_width = sum(widths.get(id(c), 1.0) for c in children)
    total_child_width += (len(children) - 1) * 0.4

    child_cx = cx - total_child_width / 2
    for child in children:
        w = widths.get(id(child), 1.0)
        child_cx += w / 2
        child_path = path + "/" + child["name"]
        _assign_positions(child, child_cx, row + 1, positions, child_path, widths)
        child_cx += w / 2 + 0.4


def _edge_geometry(p1: QtCore.QPointF, p2: QtCore.QPointF) -> tuple:
    """Return (QPainterPath, QPolygonF) for a cubic Bezier edge from p1
    (bottom-centre of the parent box) to p2 (top-centre of the child box),
    with a filled arrowhead triangle at p2."""
    dx = 0.0
    dy = abs(p2.y() - p1.y()) * 0.5

    path = QtGui.QPainterPath(p1)
    path.cubicTo(
        QtCore.QPointF(p1.x() + dx, p1.y() + dy),
        QtCore.QPointF(p2.x() - dx, p2.y() - dy),
        p2,
    )

    tx = p2.x() - (p2.x() - dx)
    ty = p2.y() - (p2.y() - dy)
    tang = math.atan2(ty, tx) if (tx != 0 or ty != 0) else math.pi / 2

    hs = ARROW_SIZE
    left  = QtCore.QPointF(
        p2.x() + hs * math.cos(tang + math.pi * 0.85),
        p2.y() + hs * math.sin(tang + math.pi * 0.85),
    )
    right = QtCore.QPointF(
        p2.x() + hs * math.cos(tang - math.pi * 0.85),
        p2.y() + hs * math.sin(tang - math.pi * 0.85),
    )
    arrow = QtGui.QPolygonF([p2, left, right])
    return path, arrow


# ═══════════════════════════════════════════════════════════════════════
# Batched graphics layer — one item draws every node + edge
# ═══════════════════════════════════════════════════════════════════════

class _CallGraphLayer(QtWidgets.QGraphicsObject):
    """Draws every node box and every edge in a single paint() call.

    There are no per-node QGraphicsItems, so click/hover hit-testing is done
    manually: nodes are bucketed by row (fixed STRIDE_Y spacing) and sorted
    by x within each row, so a click/hover position can be resolved via a
    row lookup + bisect, mirroring flame_item.py's bisect-based culling.
    """

    clicked = QtCore.Signal(str)   # emits function name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptHoverEvents(True)
        self._nodes: list = []       # dicts: path, name, rect, color, count, inclusive_us
        self._edges: list = []       # list of (p1, p2) QPointF pairs
        self._row_index: dict = {}   # row -> (sorted x_lefts, node indices)
        self._bounds = QtCore.QRectF()
        self._unit_label = "us"
        self._unit_scale = 1.0
        self._edge_color = QtGui.QColor(THEME["accent_primary"])
        self._hover_path = None

    # ── Data ──────────────────────────────────────────────────────────

    def set_data(self, nodes: list, edges: list, bounds: QtCore.QRectF,
                 unit_label: str, unit_scale: float) -> None:
        self.prepareGeometryChange()
        self._nodes = nodes
        self._edges = edges
        self._bounds = bounds
        self._unit_label = unit_label
        self._unit_scale = unit_scale
        self._hover_path = None
        self._rebuild_row_index()
        self.update()

    def set_unit(self, unit_label: str, unit_scale: float) -> None:
        self._unit_label = unit_label
        self._unit_scale = unit_scale
        self.update()

    def set_edge_color(self, color: QtGui.QColor) -> None:
        self._edge_color = color
        self.update()

    def _rebuild_row_index(self) -> None:
        rows: dict = {}
        for i, n in enumerate(self._nodes):
            row = round(n["rect"].y() / STRIDE_Y)
            rows.setdefault(row, []).append((n["rect"].x(), i))
        index = {}
        for row, entries in rows.items():
            entries.sort(key=lambda e: e[0])
            index[row] = ([e[0] for e in entries], [e[1] for e in entries])
        self._row_index = index

    def _hit_test(self, pos: QtCore.QPointF):
        """Return the node dict under ``pos`` (item-local coords), or None."""
        row = round(pos.y() / STRIDE_Y)
        entry = self._row_index.get(row)
        if not entry:
            return None
        xs, idxs = entry
        i = bisect_right(xs, pos.x()) - 1
        if i < 0:
            return None
        node = self._nodes[idxs[i]]
        if node["rect"].contains(pos):
            return node
        return None

    # ── QGraphicsItem ───────────────────────────────────────────────

    def boundingRect(self) -> QtCore.QRectF:
        return self._bounds

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        exposed = option.exposedRect

        # Edges first (drawn behind nodes), culled to the exposed rect.
        # Path/arrow/bbox are precomputed once in _build_scene() (they only
        # depend on fixed layout positions), so paint() just draws them.
        pen = QtGui.QPen(self._edge_color, EDGE_WIDTH)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setBrush(self._edge_color)
        for _p1, _p2, path, arrow, bbox in self._edges:
            if not bbox.intersects(exposed):
                continue
            painter.setPen(pen)
            painter.drawPath(path)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.drawPolygon(arrow)

        # Nodes. Elided labels and node colors are precomputed once in
        # _build_scene() too — only the hover state and unit-scaled time
        # label are inherently dynamic and computed here.
        f1 = QtGui.QFont(painter.font())
        f1.setPointSize(9)
        f1.setBold(True)
        f2 = QtGui.QFont(f1)
        f2.setBold(False)
        f2.setPointSize(8)
        text_white = QtGui.QColor(THEME["text_white"])
        text_secondary = QtGui.QColor(THEME["text_secondary"])
        hover_path = self._hover_path

        for n in self._nodes:
            rect = n["rect"]
            if not rect.intersects(exposed):
                continue
            hovered = n["path"] == hover_path

            if hovered:
                fill = QtGui.QColor(n["color"])
                fill.setAlpha(NODE_ALPHA + 30)
            else:
                fill = n["fill_normal"]
            painter.fillRect(rect, fill)

            painter.setPen(QtGui.QPen(n["border_color"], 1.5 if not hovered else 2.0))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))

            painter.setPen(text_white)
            painter.setFont(f1)
            painter.drawText(int(rect.x()) + 5, int(rect.y() + NODE_H * 0.44), n["elided_name"])

            painter.setPen(text_secondary)
            painter.setFont(f2)
            time_val = n["inclusive_us"] * self._unit_scale
            label2 = f"{n['count']}× · {time_val:.3f} {self._unit_label}"
            painter.drawText(int(rect.x()) + 5, int(rect.y() + NODE_H * 0.78), label2)

    # ── Hover / click ────────────────────────────────────────────────

    def hoverMoveEvent(self, event):
        node = self._hit_test(event.pos())
        new_hover = node["path"] if node is not None else None
        if new_hover != self._hover_path:
            self._hover_path = new_hover
            self.update()
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor) if node is not None else self.unsetCursor()

    def hoverLeaveEvent(self, event):
        if self._hover_path is not None:
            self._hover_path = None
            self.update()
        self.unsetCursor()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            node = self._hit_test(event.pos())
            if node is not None:
                self.clicked.emit(node["name"])
                event.accept()
                return
        # Not on a node (background/edge) — don't consume the click, so the
        # view's ScrollHandDrag can still start a pan from here.
        event.ignore()


# ═══════════════════════════════════════════════════════════════════════
# Scene builder
# ═══════════════════════════════════════════════════════════════════════

def _build_scene(tree: dict, color_map: dict,
                 unit_label: str, unit_scale: float) -> tuple:
    """Build a QGraphicsScene containing a single _CallGraphLayer covering
    the aggregated call tree.

    Returns ``(scene, layer)``.
    """
    scene = QtWidgets.QGraphicsScene()
    scene.setBackgroundBrush(QtGui.QColor(THEME["bg_base"]))
    layer = _CallGraphLayer()
    scene.addItem(layer)

    # Skip the synthetic <root> node — iterate its children as top-level roots
    roots = list(tree["children"].values())
    if not roots:
        layer.set_data([], [], QtCore.QRectF(), unit_label, unit_scale)
        return scene, layer

    # Compute layout widths (kept in a side dict, not written onto the tree
    # itself -- see _compute_width()'s docstring).
    widths: dict = {}
    total_width = sum(_compute_width(r, widths) for r in roots) + (len(roots) - 1) * 0.4

    # Assign positions — treat all top-level children as siblings under a
    # virtual root centred at 0.
    positions: dict[str, QtCore.QPointF] = {}
    cx = -total_width / 2
    for r in roots:
        w = widths.get(id(r), 1.0)
        cx += w / 2
        _assign_positions(r, cx, 0, positions, r["name"], widths)
        cx += w / 2 + 0.4

    nodes = []
    edges = []

    # Precompute once (not per paint() call, not even per node): the label
    # font/metrics used for eliding node names don't depend on the node, so
    # building them here and reusing across all nodes avoids the per-paint
    # elidedText() cost that used to dominate _CallGraphLayer.paint().
    label_font = QtGui.QFont()
    label_font.setPointSize(9)
    label_font.setBold(True)
    label_fm = QtGui.QFontMetrics(label_font)
    max_label_w = int(NODE_W - 10)

    def _collect(node: dict, path: str, parent_path: str | None):
        pos = positions.get(path)
        if pos is None:
            return

        name = node["name"]
        color_hex = color_map.get(name, THEME["canvas_fallback"])
        color = QtGui.QColor(color_hex)
        border_color = QtGui.QColor(color)
        border_color.setAlpha(255)
        fill_normal = QtGui.QColor(color)
        fill_normal.setAlpha(NODE_ALPHA)
        nodes.append({
            "path": path,
            "name": name,
            "rect": QtCore.QRectF(pos.x(), pos.y(), NODE_W, NODE_H),
            "color": color,
            "border_color": border_color,
            "fill_normal": fill_normal,
            "elided_name": label_fm.elidedText(name, QtCore.Qt.TextElideMode.ElideRight, max_label_w),
            "count": node["count"],
            "inclusive_us": node["inclusive_us"],
        })

        if parent_path is not None:
            parent_pos = positions.get(parent_path)
            if parent_pos is not None:
                p1 = QtCore.QPointF(parent_pos.x() + NODE_W / 2, parent_pos.y() + NODE_H)
                p2 = QtCore.QPointF(pos.x() + NODE_W / 2, pos.y())
                path_geom, arrow_geom = _edge_geometry(p1, p2)
                bbox = QtCore.QRectF(p1, p2).normalized()
                edges.append((p1, p2, path_geom, arrow_geom, bbox))

        for child in node["children"].values():
            child_path = path + "/" + child["name"]
            _collect(child, child_path, path)

    for r in roots:
        _collect(r, r["name"], None)

    # Expand the scrollable scene rect so there's always breathing room at
    # every edge when the user zooms in and pans to the boundary.
    _PAD = 90
    xs_lo = [n["rect"].left() for n in nodes]
    ys_lo = [n["rect"].top() for n in nodes]
    xs_hi = [n["rect"].right() for n in nodes]
    ys_hi = [n["rect"].bottom() for n in nodes]
    items_rect = QtCore.QRectF(
        min(xs_lo), min(ys_lo),
        max(xs_hi) - min(xs_lo), max(ys_hi) - min(ys_lo),
    )
    bounds = items_rect.adjusted(-_PAD, -_PAD, _PAD, _PAD)

    layer.set_data(nodes, edges, bounds, unit_label, unit_scale)
    scene.setSceneRect(bounds)

    return scene, layer


# ═══════════════════════════════════════════════════════════════════════
# Graph view (handles Ctrl+scroll zoom)
# ═══════════════════════════════════════════════════════════════════════

class _GraphView(QtWidgets.QGraphicsView):
    """QGraphicsView with Ctrl+scroll zoom anchored to the cursor."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTransformationAnchor(
            QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(
            QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter
        )
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        bg = QtGui.QColor(THEME["bg_base"])
        self.setBackgroundBrush(bg)

    def refresh_theme(self):
        """Update the view background brush to the current theme color."""
        self.setBackgroundBrush(QtGui.QColor(THEME["bg_base"]))
        scene = self.scene()
        if scene is not None:
            scene.setBackgroundBrush(QtGui.QColor(THEME["bg_base"]))
        self.viewport().update()

    def wheelEvent(self, event: QtGui.QWheelEvent):  # noqa: N802
        modifiers = event.modifiers()
        if modifiers & QtCore.Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            factor = 1.15 if delta > 0 else 1.0 / 1.15
            self.scale(factor, factor)
            event.accept()
        else:
            super().wheelEvent(event)


# ═══════════════════════════════════════════════════════════════════════
# Dock widget
# ═══════════════════════════════════════════════════════════════════════

class CallGraphDock(DockBase):
    """Dock that renders the aggregated call tree as an interactive node-link graph.

    Signals:
        function_clicked(name) — user clicked a node; payload is the function
                                  name so the main window can jump to it.
    """

    function_clicked = QtCore.Signal(str)

    def __init__(self, spans, color_map, parent=None, tree=None, lazy=False):
        """``lazy=True`` skips building the graph now (spans/tree are just
        stashed) -- call ensure_built() to build it on first actual use.
        This dock is tabbed with CallTreeDock (only one tab is visible at
        a time), so building ~58k nodes/edges eagerly is wasted work for
        any session that never opens this tab.
        """
        super().__init__("Call Graph", parent)
        self.setAllowedAreas(QtCore.Qt.DockWidgetArea.AllDockWidgetAreas)

        self._color_map = color_map
        self._spans = spans
        self._layer: _CallGraphLayer | None = None
        self._built = False
        self._pending_tree = tree

        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Control panel
        ctrl = QtWidgets.QWidget()
        ctrl.setObjectName("DockCtrlPanel")
        ctrl_row = QtWidgets.QHBoxLayout(ctrl)
        ctrl_row.setContentsMargins(6, 5, 6, 5)
        ctrl_row.setSpacing(6)
        fit_btn = QtWidgets.QPushButton("Fit")
        fit_btn.setFixedWidth(52)
        fit_btn.setToolTip("Reset zoom to show all nodes  (F)")
        fit_btn.clicked.connect(self._fit_view)
        ctrl_row.addWidget(fit_btn)
        ctrl_row.addWidget(QtWidgets.QLabel("Ctrl+scroll to zoom · drag to pan"))
        ctrl_row.addStretch(1)
        layout.addWidget(ctrl)

        # Graph view
        self._view = _GraphView()
        layout.addWidget(self._view, 1)

        self.setWidget(container)

        # Fit shortcut (F key, scoped to this dock)
        fit_sc = QtGui.QShortcut(QtGui.QKeySequence("F"), self)
        fit_sc.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        fit_sc.activated.connect(self._fit_view)

        if lazy:
            self._pending_tree = tree
        else:
            self.set_spans(spans, color_map, tree=tree)

    # ── Public API ──────────────────────────────────────────────────

    def ensure_built(self) -> None:
        """Build the graph now if it hasn't been already.

        Safe to call unconditionally (e.g. from a visibilityChanged
        handler every time the tab is shown) -- a no-op once built.
        """
        if self._built:
            return
        self.set_spans(self._spans, self._color_map, tree=self._pending_tree)

    def refresh_or_defer(self, spans, color_map=None, tree=None) -> None:
        """Like set_spans(), but if this dock hasn't been built yet (its
        tab has never been shown), just stash the new data instead of
        forcing a build. Meant for callers like a live-refresh/reload path
        that shouldn't undo the laziness by rebuilding a tab nobody has
        looked at yet on every refresh.
        """
        if self._built:
            self.set_spans(spans, color_map, tree=tree)
        else:
            self._spans = spans
            if color_map is not None:
                self._color_map = color_map
            self._pending_tree = tree

    def set_spans(self, spans, color_map=None, _fit=True, tree=None):
        """Rebuild the graph from a new span list.

        ``tree`` lets a caller that already computed build_call_tree(spans)
        (e.g. ProfilerWindow, sharing it with CallTreeDock) pass it in
        directly instead of this dock recomputing it. The tree is only
        ever read here -- _build_scene()'s layout pass keeps its own
        per-node width data in a side dict rather than writing onto the
        tree, so it's safe to share the same tree object with other docks.

        Calling this (from anywhere) always builds immediately, even if
        the dock was constructed with lazy=True and never actually shown
        -- an explicit rebuild request (e.g. a new trace loaded) should
        never be silently skipped.
        """
        self._built = True
        self._spans = spans
        if color_map is not None:
            self._color_map = color_map
        if tree is None:
            tree = build_call_tree(spans)
        scene, layer = _build_scene(
            tree, self._color_map, self._unit_label, self._unit_scale
        )
        layer.clicked.connect(self.function_clicked)
        self._layer = layer
        self._view.setScene(scene)
        if _fit:
            # Defer fit so the view has been laid out
            QtCore.QTimer.singleShot(0, self._fit_view)

    def refresh_theme(self):
        """Recolor edges/background in place — no scene rebuild.

        Node fill/border colors come from ``_color_map`` (per-function,
        theme-independent) so they don't need to change. Node text colors
        are read live from THEME at paint time, so a repaint is enough.
        Only the edge color and background are theme-dependent.
        """
        super().refresh_theme()
        self._view.refresh_theme()
        if self._layer is not None:
            self._layer.set_edge_color(QtGui.QColor(THEME["accent_primary"]))

    def set_unit(self, unit_label: str, unit_scale: float):
        """Refresh node labels for a new display unit (us / ms)."""
        super().set_unit(unit_label, unit_scale)
        if self._layer is not None:
            self._layer.set_unit(unit_label, unit_scale)

    # ── Internal ────────────────────────────────────────────────────

    def _fit_view(self):
        scene = self._view.scene()
        if scene is None:
            return
        bounds = scene.itemsBoundingRect()
        if bounds.isNull():
            return
        self._view.resetTransform()
        self._view.fitInView(
            bounds.adjusted(-40, -40, 40, 40),
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        )
