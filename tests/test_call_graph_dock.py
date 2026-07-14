"""Regression tests for CallGraphDock.refresh_theme().

Guards against the bug where a theme change triggered a full scene
rebuild (rebuilding the call tree, every node item, and every edge from
scratch) instead of recoloring the existing items in place.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtGui

from gui.call_graph_dock import CallGraphDock
from gui.theme import THEME, apply_theme

SPANS = [
    {"name": "main", "addr": 1, "start_us": 0, "end_us": 10, "duration_us": 10, "depth": 0, "ipsr": 0},
    {"name": "foo", "addr": 2, "start_us": 1, "end_us": 5, "duration_us": 4, "depth": 1, "ipsr": 0},
    {"name": "bar", "addr": 3, "start_us": 6, "end_us": 9, "duration_us": 3, "depth": 1, "ipsr": 0},
]
COLOR_MAP = {"main": "#ff0000", "foo": "#00ff00", "bar": "#0000ff"}


@pytest.fixture(autouse=True)
def _restore_theme():
    """Every test that switches theme must leave THEME as it found it."""
    yield
    apply_theme("Dark")


def _make_dock(qapp):
    return CallGraphDock(list(SPANS), dict(COLOR_MAP))


def test_set_spans_builds_expected_node_and_edge_counts(qapp):
    dock = _make_dock(qapp)
    # main is the sole root; foo and bar are its children.
    assert len(dock._node_items) == 3
    assert len(dock._edge_items) == 2  # main->foo, main->bar


def test_refresh_theme_does_not_rebuild_scene(qapp):
    dock = _make_dock(qapp)
    scene_before = dock._view.scene()
    node_items_before = list(dock._node_items)
    edge_items_before = list(dock._edge_items)

    apply_theme("Light")
    dock.refresh_theme()

    assert dock._view.scene() is scene_before
    assert dock._node_items == node_items_before
    assert dock._edge_items == edge_items_before


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
    assert dock._edge_items, "expected at least one edge in the test call graph"
    for edge_item, arrow_item in dock._edge_items:
        assert edge_item.pen().color() == expected
        assert arrow_item.pen().color() == expected
        assert arrow_item.brush().color() == expected


def test_refresh_theme_preserves_node_colors(qapp):
    """Node fill/border colors come from color_map (per-function), which is
    theme-independent — they must not change on a theme switch."""
    dock = _make_dock(qapp)
    colors_before = {path: item._color.name() for path, item, _node in dock._node_items}

    apply_theme("Light")
    dock.refresh_theme()

    colors_after = {path: item._color.name() for path, item, _node in dock._node_items}
    assert colors_after == colors_before
