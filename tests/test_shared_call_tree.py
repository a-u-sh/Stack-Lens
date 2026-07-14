"""Tests for sharing one build_call_tree(spans) result between
CallTreeDock and CallGraphDock instead of each dock recomputing it.

The key guarantee under test: CallGraphDock's layout pass must never
write onto the shared tree (it keeps per-node width data in a side dict
instead), since a mutation there would leak into whatever else holds a
reference to the same tree object.
"""
import copy
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from span_builder import build_call_tree
from gui.call_graph_dock import CallGraphDock
from gui.call_tree_dock import CallTreeDock

SPANS = [
    {"name": "main", "addr": 1, "start_us": 0, "end_us": 10, "duration_us": 10, "depth": 0, "ipsr": 0},
    {"name": "foo", "addr": 2, "start_us": 1, "end_us": 5, "duration_us": 4, "depth": 1, "ipsr": 0},
    {"name": "bar", "addr": 3, "start_us": 6, "end_us": 9, "duration_us": 3, "depth": 1, "ipsr": 0},
]
COLOR_MAP = {"main": "#ff0000", "foo": "#00ff00", "bar": "#0000ff"}
TOTAL_US = 10.0


def test_call_graph_dock_does_not_mutate_shared_tree(qapp):
    tree = build_call_tree(list(SPANS))
    tree_before = copy.deepcopy(tree)

    CallGraphDock(list(SPANS), dict(COLOR_MAP), tree=tree)

    assert tree == tree_before


def test_call_graph_dock_uses_precomputed_tree_instead_of_recomputing(qapp, monkeypatch):
    import gui.call_graph_dock as cgd

    calls = []
    monkeypatch.setattr(cgd, "build_call_tree", lambda spans: calls.append(spans) or {"children": {}})

    tree = build_call_tree(list(SPANS))
    CallGraphDock(list(SPANS), dict(COLOR_MAP), tree=tree)

    assert calls == []


def test_call_tree_dock_uses_precomputed_tree_instead_of_recomputing(qapp, monkeypatch):
    import gui.call_tree_dock as ctd

    calls = []
    monkeypatch.setattr(ctd, "build_call_tree", lambda spans: calls.append(spans) or {"children": {}})

    tree = build_call_tree(list(SPANS))
    CallTreeDock(list(SPANS), dict(COLOR_MAP), TOTAL_US, tree=tree)

    assert calls == []


def test_both_docks_share_one_tree_object_and_render_correctly(qapp):
    """Integration check: one tree, fed to both docks, produces correct
    (and mutually unaffected) output in each."""
    tree = build_call_tree(list(SPANS))

    graph_dock = CallGraphDock(list(SPANS), dict(COLOR_MAP), tree=tree)
    tree_dock = CallTreeDock(list(SPANS), dict(COLOR_MAP), TOTAL_US, tree=tree)

    assert len(graph_dock._layer._nodes) == 3
    assert len(graph_dock._layer._edges) == 2

    assert tree_dock._tree.topLevelItemCount() == 1
    main_item = tree_dock._tree.topLevelItem(0)
    assert main_item.text(0) == "main"
    # foo and bar are both main's direct children (siblings); both get
    # materialized as real items via the default first-level auto-expand.
    assert main_item.childCount() == 2
    child_names = {main_item.child(i).text(0) for i in range(2)}
    assert child_names == {"foo", "bar"}
