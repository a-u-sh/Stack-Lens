"""Call-Graph dock — interactive node-link diagram of the aggregated call tree.

Shows which function calls which, with boxes sized/coloured by function colour
and labelled with call count + inclusive time. Bezier edges with arrowheads
connect callers to callees.

Interaction:
- Click a node  → emits ``function_clicked(name)`` → main window jumps to it.
- Ctrl+scroll   → zoom anchored to mouse cursor.
- "Fit" button  → reset zoom to show all nodes.
"""

import math

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

def _compute_width(node: dict) -> float:
    """Post-order: return the column-unit width of this subtree.

    Each node occupies at least 1 unit; an internal node occupies the sum
    of its children's widths (with 0.4-unit gaps between siblings).
    """
    children = list(node["children"].values())
    if not children:
        node["_width"] = 1.0
        return 1.0
    total = sum(_compute_width(c) for c in children)
    # gaps between N children: (N-1) * 0.4
    total += (len(children) - 1) * 0.4
    node["_width"] = total
    return total


def _assign_positions(node: dict, cx: float, row: int, positions: dict, path: str) -> None:
    """Pre-order: assign pixel (x, y) to every node.

    ``positions`` is filled with ``path → (px_x, px_y)`` where the coordinates
    are the *top-left* corner of the node box.  ``path`` is the parent-chain
    joined by '/' so identical function names at different call sites map to
    separate entries.
    """
    px_x = cx * STRIDE_X - NODE_W / 2
    px_y = row * STRIDE_Y
    positions[path] = QtCore.QPointF(px_x, px_y)

    children = list(node["children"].values())
    if not children:
        return

    # Centre children under this node
    total_child_width = sum(c.get("_width", 1.0) for c in children)
    total_child_width += (len(children) - 1) * 0.4

    child_cx = cx - total_child_width / 2
    for child in children:
        w = child.get("_width", 1.0)
        child_cx += w / 2
        child_path = path + "/" + child["name"]
        _assign_positions(child, child_cx, row + 1, positions, child_path)
        child_cx += w / 2 + 0.4


# ═══════════════════════════════════════════════════════════════════════
# Graphics items
# ═══════════════════════════════════════════════════════════════════════

class _NodeItem(QtWidgets.QGraphicsObject):
    """Clickable function box with coloured background and two label lines."""

    clicked = QtCore.Signal(str)   # emits function name

    def __init__(self, name: str, display_name: str, count: int,
                 time_val: float, unit: str, color: QtGui.QColor, parent=None):
        super().__init__(parent)
        self._name = name
        self._display_name = display_name
        self._count = count
        self._time_val = time_val
        self._unit = unit
        self._color = color
        self._hovered = False

        self.setAcceptHoverEvents(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    # Geometry
    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(0, 0, NODE_W, NODE_H)

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        r = QtCore.QRectF(0, 0, NODE_W, NODE_H)

        # Background fill
        fill = QtGui.QColor(self._color)
        fill.setAlpha(NODE_ALPHA + (30 if self._hovered else 0))
        painter.fillRect(r, fill)

        # Border
        border_color = QtGui.QColor(self._color)
        border_color.setAlpha(255)
        pen = QtGui.QPen(border_color, 1.5 if not self._hovered else 2.0)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))

        # Line 1 — function name (elided)
        painter.setPen(QtGui.QColor(THEME["text_white"]))
        f1 = painter.font()
        f1.setPointSize(9)
        f1.setBold(True)
        painter.setFont(f1)
        fm1 = QtGui.QFontMetrics(f1)
        max_w = int(NODE_W - 10)
        elided = fm1.elidedText(self._display_name, QtCore.Qt.TextElideMode.ElideRight, max_w)
        painter.drawText(5, int(NODE_H * 0.44), elided)

        # Line 2 — count × time
        painter.setPen(QtGui.QColor(THEME["text_secondary"]))
        f2 = QtGui.QFont(f1)
        f2.setBold(False)
        f2.setPointSize(8)
        painter.setFont(f2)
        label2 = f"{self._count}\u00d7 \u00b7 {self._time_val:.3f} {self._unit}"
        painter.drawText(5, int(NODE_H * 0.78), label2)

    # Hover
    def hoverEnterEvent(self, event):
        self._hovered = True
        self.update()

    def hoverLeaveEvent(self, event):
        self._hovered = False
        self.update()

    # Click
    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.clicked.emit(self._name)
        super().mousePressEvent(event)

    # Public update
    def set_label(self, time_val: float, unit: str):
        self._time_val = time_val
        self._unit = unit
        self.update()


def _make_edge(scene: QtWidgets.QGraphicsScene,
               p1: QtCore.QPointF, p2: QtCore.QPointF) -> tuple:
    """Draw a cubic Bezier edge from p1 (bottom-centre) to p2 (top-centre)
    with a filled arrowhead at p2. Returns (edge_item, arrow_item) so the
    caller can recolor them later without rebuilding the scene."""
    dx = 0.0
    dy = abs(p2.y() - p1.y()) * 0.5

    path = QtGui.QPainterPath(p1)
    path.cubicTo(
        QtCore.QPointF(p1.x() + dx, p1.y() + dy),
        QtCore.QPointF(p2.x() - dx, p2.y() - dy),
        p2,
    )

    edge_color = QtGui.QColor(THEME["accent_primary"])
    pen = QtGui.QPen(edge_color, EDGE_WIDTH)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    edge_item = QtWidgets.QGraphicsPathItem(path)
    edge_item.setPen(pen)
    edge_item.setZValue(-1)
    scene.addItem(edge_item)

    # Arrowhead (filled triangle)
    angle = math.atan2(p2.y() - (p2.y() - dy * 0.3), p2.x() - (p2.x()))
    # Direction vector of the last segment (Bezier end tangent)
    # tangent at t=1: 3*(p3 - p2) in cubic terms; here roughly downward
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
    arrow_item = QtWidgets.QGraphicsPolygonItem(arrow)
    arrow_item.setPen(QtGui.QPen(edge_color, 1.0))
    arrow_item.setBrush(edge_color)
    arrow_item.setZValue(-1)
    scene.addItem(arrow_item)

    return edge_item, arrow_item


# ═══════════════════════════════════════════════════════════════════════
# Scene builder
# ═══════════════════════════════════════════════════════════════════════

def _build_scene(tree: dict, color_map: dict,
                 unit_label: str, unit_scale: float) -> tuple:
    """Build a QGraphicsScene from the aggregated call tree.

    Returns ``(scene, node_items, edge_items)`` where ``node_items`` is a
    list of ``(path, _NodeItem, node)`` and ``edge_items`` is a list of
    ``(edge_path_item, arrow_item)`` so the caller can update labels /
    recolor on unit or theme change without rebuilding the scene.
    """
    scene = QtWidgets.QGraphicsScene()
    scene.setBackgroundBrush(QtGui.QColor(THEME["bg_base"]))
    node_items = []
    edge_items = []

    # Skip the synthetic <root> node — iterate its children as top-level roots
    roots = list(tree["children"].values())
    if not roots:
        return scene, node_items, edge_items

    # Compute layout widths
    total_width = sum(_compute_width(r) for r in roots) + (len(roots) - 1) * 0.4

    # Assign positions — treat all top-level children as siblings under a
    # virtual root centred at 0.
    positions: dict[str, QtCore.QPointF] = {}
    cx = -total_width / 2
    for r in roots:
        w = r.get("_width", 1.0)
        cx += w / 2
        _assign_positions(r, cx, 0, positions, r["name"])
        cx += w / 2 + 0.4

    # ── Recursive item builder ──────────────────────────────────────
    def _add_items(node: dict, path: str, parent_path: str | None):
        pos = positions.get(path)
        if pos is None:
            return

        name = node["name"]
        color_hex = color_map.get(name, THEME["canvas_fallback"])
        color = QtGui.QColor(color_hex)
        time_val = node["inclusive_us"] * unit_scale
        item = _NodeItem(
            name=name,
            display_name=name,
            count=node["count"],
            time_val=time_val,
            unit=unit_label,
            color=color,
        )
        item.setPos(pos)
        scene.addItem(item)
        node_items.append((path, item, node))

        # Edge from parent
        if parent_path is not None:
            parent_pos = positions.get(parent_path)
            if parent_pos is not None:
                p1 = QtCore.QPointF(
                    parent_pos.x() + NODE_W / 2,
                    parent_pos.y() + NODE_H,
                )
                p2 = QtCore.QPointF(
                    pos.x() + NODE_W / 2,
                    pos.y(),
                )
                edge_items.append(_make_edge(scene, p1, p2))

        for child in node["children"].values():
            child_path = path + "/" + child["name"]
            _add_items(child, child_path, path)

    for r in roots:
        _add_items(r, r["name"], None)

    # Expand the scrollable scene rect so there's always breathing room at
    # every edge when the user zooms in and pans to the boundary.
    _PAD = 90
    items_rect = scene.itemsBoundingRect()
    scene.setSceneRect(items_rect.adjusted(-_PAD, -_PAD, _PAD, _PAD))

    return scene, node_items, edge_items


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

    def __init__(self, spans, color_map, parent=None):
        super().__init__("Call Graph", parent)
        self.setAllowedAreas(QtCore.Qt.DockWidgetArea.AllDockWidgetAreas)

        self._color_map = color_map
        self._spans = spans
        self._node_items: list = []   # list of (path, _NodeItem, raw_node)
        self._edge_items: list = []   # list of (edge_path_item, arrow_item)

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

        self.set_spans(spans, color_map)

    # ── Public API ──────────────────────────────────────────────────

    def set_spans(self, spans, color_map=None, _fit=True):
        """Rebuild the graph from a new span list."""
        self._spans = spans
        if color_map is not None:
            self._color_map = color_map
        tree = build_call_tree(spans)
        scene, node_items, edge_items = _build_scene(
            tree, self._color_map, self._unit_label, self._unit_scale
        )
        for _path, item, _node in node_items:
            item.clicked.connect(self.function_clicked)
        self._node_items = node_items
        self._edge_items = edge_items
        self._view.setScene(scene)
        if _fit:
            # Defer fit so the view has been laid out
            QtCore.QTimer.singleShot(0, self._fit_view)

    def refresh_theme(self):
        """Recolor edges/background/text in place — no scene rebuild.

        Node fill/border colors come from ``_color_map`` (per-function,
        theme-independent) so they don't need to change. Node text colors
        are read live from THEME at paint time, so a repaint is enough.
        Only the edge color and background are theme-dependent and stored
        on the items themselves, so those need an explicit update.
        """
        super().refresh_theme()
        self._view.refresh_theme()

        edge_color = QtGui.QColor(THEME["accent_primary"])
        for edge_item, arrow_item in self._edge_items:
            pen = edge_item.pen()
            pen.setColor(edge_color)
            edge_item.setPen(pen)
            arrow_pen = arrow_item.pen()
            arrow_pen.setColor(edge_color)
            arrow_item.setPen(arrow_pen)
            arrow_item.setBrush(edge_color)

        for _path, item, _node in self._node_items:
            item.update()

    def set_unit(self, unit_label: str, unit_scale: float):
        """Refresh node labels for a new display unit (us / ms)."""
        super().set_unit(unit_label, unit_scale)
        for _path, item, node in self._node_items:
            item.set_label(node["inclusive_us"] * unit_scale, unit_label)

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
