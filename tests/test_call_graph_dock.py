"""Tests for CallGraphDock's batched-rendering _CallGraphLayer.

Covers both regressions this dock has had:
  1. refresh_theme() must recolor in place, not rebuild the scene.
  2. Since rendering batches every node/edge into one paint() call (instead
     of one QGraphicsObject per node), click/hover hit-testing is now
     manual (row/x-range lookup) and must be verified directly: clicking a
     node emits function_clicked with the right name, clicking empty space
     does not (and lets the event through for drag-to-pan), and hover
     tracks the correct node.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtGui

from gui.call_graph_dock import CallGraphDock, STRIDE_Y, NODE_H
from gui.theme import THEME, apply_theme

SPANS = [
    {"name": "main", "addr": 1, "start_us": 0, "end_us": 10, "duration_us": 10, "depth": 0, "ipsr": 0},
    {"name": "foo", "addr": 2, "start_us": 1, "end_us": 5, "duration_us": 4, "depth": 1, "ipsr": 0},
    {"name": "bar", "addr": 3, "start_us": 6, "end_us": 9, "duration_us": 3, "depth": 1, "ipsr": 0},
]
COLOR_MAP = {"main": "#ff0000", "foo": "#00ff00", "bar": "#0000ff"}


class _FakeMouseEvent:
    """Minimal stand-in for QGraphicsSceneMouseEvent — mousePressEvent only
    calls .pos(), .button(), .accept(), .ignore()."""

    def __init__(self, pos, button=QtCore.Qt.MouseButton.LeftButton):
        self._pos = pos
        self._button = button
        self.accepted = None

    def pos(self):
        return self._pos

    def button(self):
        return self._button

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


class _FakeHoverEvent:
    def __init__(self, pos):
        self._pos = pos

    def pos(self):
        return self._pos


@pytest.fixture(autouse=True)
def _restore_theme():
    yield
    apply_theme("Dark")


def _make_dock(qapp):
    return CallGraphDock(list(SPANS), dict(COLOR_MAP))


def _node_by_name(dock, name):
    for n in dock._layer._nodes:
        if n["name"] == name:
            return n
    raise AssertionError(f"no node named {name!r}")


def test_set_spans_builds_expected_node_and_edge_counts(qapp):
    dock = _make_dock(qapp)
    # main is the sole root; foo and bar are its children.
    assert len(dock._layer._nodes) == 3
    assert len(dock._layer._edges) == 2  # main->foo, main->bar


def test_node_paint_data_is_precomputed_not_recomputed_per_paint(qapp):
    """Elided labels and node colors are computed once in _build_scene(),
    not on every paint() call — this is the whole point of the fix."""
    dock = _make_dock(qapp)
    for n in dock._layer._nodes:
        # Short test names fit well within the node width, so elision
        # should be a no-op -- this also catches an elision computed
        # against the wrong font/width silently mangling short names.
        assert n["elided_name"] == n["name"]
        assert isinstance(n["border_color"], QtGui.QColor)
        assert isinstance(n["fill_normal"], QtGui.QColor)
        assert n["border_color"].alpha() == 255
        assert n["fill_normal"].alpha() == 160  # NODE_ALPHA


def test_edge_geometry_is_precomputed(qapp):
    dock = _make_dock(qapp)
    for p1, p2, path, arrow, bbox in dock._layer._edges:
        assert isinstance(path, QtGui.QPainterPath)
        assert isinstance(arrow, QtGui.QPolygonF)
        assert bbox.contains(p1)
        assert bbox.contains(p2)


def test_refresh_theme_does_not_rebuild_scene(qapp):
    dock = _make_dock(qapp)
    scene_before = dock._view.scene()
    layer_before = dock._layer
    nodes_before = dock._layer._nodes
    edges_before = dock._layer._edges

    apply_theme("Light")
    dock.refresh_theme()

    assert dock._view.scene() is scene_before
    assert dock._layer is layer_before
    assert dock._layer._nodes is nodes_before
    assert dock._layer._edges is edges_before


def test_refresh_theme_does_not_call_set_spans(qapp, monkeypatch):
    dock = _make_dock(qapp)
    calls = []
    monkeypatch.setattr(dock, "set_spans", lambda *a, **kw: calls.append((a, kw)))

    apply_theme("Light")
    dock.refresh_theme()

    assert calls == []


def test_refresh_theme_recolors_edges_to_new_theme(qapp):
    dock = _make_dock(qapp)

    apply_theme("Light")
    dock.refresh_theme()

    expected = QtGui.QColor(THEME["accent_primary"])
    assert dock._layer._edge_color == expected


def test_refresh_theme_preserves_node_colors(qapp):
    """Node fill/border colors come from color_map (per-function), which is
    theme-independent — they must not change on a theme switch."""
    dock = _make_dock(qapp)
    colors_before = {n["path"]: n["color"].name() for n in dock._layer._nodes}

    apply_theme("Light")
    dock.refresh_theme()

    colors_after = {n["path"]: n["color"].name() for n in dock._layer._nodes}
    assert colors_after == colors_before


def test_set_unit_updates_layer_without_rebuild(qapp, monkeypatch):
    dock = _make_dock(qapp)
    nodes_before = dock._layer._nodes
    calls = []
    monkeypatch.setattr(dock, "set_spans", lambda *a, **kw: calls.append((a, kw)))

    dock.set_unit("ms", 0.001)

    assert calls == []
    assert dock._layer._nodes is nodes_before  # no rebuild
    assert dock._layer._unit_label == "ms"
    assert dock._layer._unit_scale == 0.001


def test_hit_test_finds_each_node_at_its_center(qapp):
    dock = _make_dock(qapp)
    for n in dock._layer._nodes:
        hit = dock._layer._hit_test(n["rect"].center())
        assert hit is not None
        assert hit["path"] == n["path"]


def test_hit_test_returns_none_in_the_gap_between_rows(qapp):
    dock = _make_dock(qapp)
    # Strictly between row 0 (y in [0, NODE_H]) and row 1 (y starts at STRIDE_Y);
    # NODE_H < STRIDE_Y so this y is outside every node's rect regardless of x.
    gap_y = NODE_H + (STRIDE_Y - NODE_H) / 2
    assert dock._layer._hit_test(QtCore.QPointF(0, gap_y)) is None


def test_click_on_node_emits_function_clicked(qapp):
    dock = _make_dock(qapp)
    clicks = []
    dock.function_clicked.connect(clicks.append)

    node = _node_by_name(dock, "foo")
    ev = _FakeMouseEvent(node["rect"].center())
    dock._layer.mousePressEvent(ev)

    assert clicks == ["foo"]
    assert ev.accepted is True


def test_click_on_background_does_not_emit_and_ignores_event(qapp):
    """Clicking empty space must not fire function_clicked, and must leave
    the event un-accepted so the view's drag-to-pan can still handle it."""
    dock = _make_dock(qapp)
    clicks = []
    dock.function_clicked.connect(clicks.append)

    gap_y = NODE_H + (STRIDE_Y - NODE_H) / 2
    ev = _FakeMouseEvent(QtCore.QPointF(0, gap_y))
    dock._layer.mousePressEvent(ev)

    assert clicks == []
    assert ev.accepted is False


def test_hover_tracks_node_under_cursor(qapp):
    dock = _make_dock(qapp)
    node = _node_by_name(dock, "bar")

    dock._layer.hoverMoveEvent(_FakeHoverEvent(node["rect"].center()))
    assert dock._layer._hover_path == node["path"]

    dock._layer.hoverLeaveEvent(_FakeHoverEvent(node["rect"].center()))
    assert dock._layer._hover_path is None


def test_lazy_construction_does_not_build_until_ensure_built(qapp):
    dock = CallGraphDock(list(SPANS), dict(COLOR_MAP), lazy=True)
    assert dock._layer is None
    assert dock._view.scene() is None

    dock.ensure_built()

    assert dock._layer is not None
    assert len(dock._layer._nodes) == 3


def test_ensure_built_is_idempotent(qapp, monkeypatch):
    dock = CallGraphDock(list(SPANS), dict(COLOR_MAP), lazy=True)
    calls = []
    original = CallGraphDock.set_spans

    def counting_set_spans(self, *a, **kw):
        calls.append(1)
        return original(self, *a, **kw)

    monkeypatch.setattr(CallGraphDock, "set_spans", counting_set_spans)

    dock.ensure_built()
    dock.ensure_built()
    dock.ensure_built()

    assert len(calls) == 1


def test_lazy_dock_set_unit_and_refresh_theme_are_safe_before_build(qapp):
    """set_unit/refresh_theme must not crash when called before the tab
    has ever been shown (self._layer is still None)."""
    dock = CallGraphDock(list(SPANS), dict(COLOR_MAP), lazy=True)

    dock.set_unit("ms", 0.001)  # must not raise
    apply_theme("Light")
    dock.refresh_theme()  # must not raise
    apply_theme("Dark")

    # Once actually built, the unit set earlier while lazy must still apply.
    dock.ensure_built()
    assert dock._layer._unit_label == "ms"
    assert dock._layer._unit_scale == 0.001


def test_refresh_or_defer_does_not_force_build_when_not_yet_shown(qapp):
    dock = CallGraphDock(list(SPANS), dict(COLOR_MAP), lazy=True)

    dock.refresh_or_defer(list(SPANS), dict(COLOR_MAP))

    assert dock._layer is None  # still not built

    dock.ensure_built()
    assert dock._layer is not None


def test_refresh_or_defer_rebuilds_immediately_once_already_built(qapp, monkeypatch):
    dock = CallGraphDock(list(SPANS), dict(COLOR_MAP), lazy=True)
    dock.ensure_built()

    calls = []
    monkeypatch.setattr(dock, "set_spans", lambda *a, **kw: calls.append((a, kw)))

    dock.refresh_or_defer(list(SPANS), dict(COLOR_MAP))

    assert len(calls) == 1
