"""Tests for CallTreeDock's lazy child population.

A node's children are only materialized into real QTreeWidgetItems the
first time it's expanded, instead of building the entire tree upfront.
These tests guard the placeholder/materialize mechanics and make sure
set_unit still reaches everything that's actually visible.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from gui.call_tree_dock import CallTreeDock, _ROLE_IS_PLACEHOLDER, _ROLE_INCLUSIVE_US

# main -> foo -> baz (three levels deep, single chain)
SPANS = [
    {"name": "main", "addr": 1, "start_us": 0, "end_us": 10, "duration_us": 10, "depth": 0, "ipsr": 0},
    {"name": "foo", "addr": 2, "start_us": 1, "end_us": 8, "duration_us": 7, "depth": 1, "ipsr": 0},
    {"name": "baz", "addr": 3, "start_us": 2, "end_us": 5, "duration_us": 3, "depth": 2, "ipsr": 0},
]
COLOR_MAP = {"main": "#ff0000", "foo": "#00ff00", "baz": "#0000ff"}
TOTAL_US = 10.0


def _make_dock(qapp):
    return CallTreeDock(list(SPANS), dict(COLOR_MAP), TOTAL_US)


def _is_placeholder(item):
    return bool(item.data(0, _ROLE_IS_PLACEHOLDER))


def test_top_level_and_first_level_are_populated_by_default(qapp):
    """set_spans auto-expands top-level items, which lazily materializes
    their immediate (level-1) children — matching the old eager-build UX
    for the default view. Level-2 (foo's children) stays lazy."""
    dock = _make_dock(qapp)
    assert dock._tree.topLevelItemCount() == 1

    main_item = dock._tree.topLevelItem(0)
    assert main_item.text(0) == "main"
    assert main_item.isExpanded()

    assert main_item.childCount() == 1
    foo_item = main_item.child(0)
    assert not _is_placeholder(foo_item)
    assert foo_item.text(0) == "foo"

    # foo has a child (baz) but hasn't been expanded, so it's still lazy.
    assert foo_item.childCount() == 1
    assert _is_placeholder(foo_item.child(0))


def test_expanding_lazy_node_materializes_real_children(qapp):
    dock = _make_dock(qapp)
    main_item = dock._tree.topLevelItem(0)
    foo_item = main_item.child(0)

    foo_item.setExpanded(True)  # fires itemExpanded -> _on_item_expanded

    assert foo_item.childCount() == 1
    baz_item = foo_item.child(0)
    assert not _is_placeholder(baz_item)
    assert baz_item.text(0) == "baz"


def test_expanding_a_leaf_is_a_no_op(qapp):
    dock = _make_dock(qapp)
    main_item = dock._tree.topLevelItem(0)
    foo_item = main_item.child(0)
    foo_item.setExpanded(True)
    baz_item = foo_item.child(0)

    # baz has no children at all — no placeholder should ever be added.
    assert baz_item.childCount() == 0
    baz_item.setExpanded(True)  # should not raise / should stay a no-op
    assert baz_item.childCount() == 0


def test_set_unit_updates_materialized_items_only(qapp):
    dock = _make_dock(qapp)
    main_item = dock._tree.topLevelItem(0)
    foo_item = main_item.child(0)

    dock.set_unit("ms", 0.001)

    # main/foo are already real items — their text must reflect the new unit.
    assert main_item.text(2) == f"{10.0 * 0.001:.3f}"
    assert foo_item.text(2) == f"{7.0 * 0.001:.3f}"

    # baz is still lazy; expanding it now must build it with the NEW unit
    # (not the stale "us" one it would have had if built eagerly earlier).
    foo_item.setExpanded(True)
    baz_item = foo_item.child(0)
    assert baz_item.data(2, _ROLE_INCLUSIVE_US) == 3.0
    assert baz_item.text(2) == f"{3.0 * 0.001:.3f}"


def test_set_spans_rebuild_resets_laziness(qapp):
    dock = _make_dock(qapp)
    main_item = dock._tree.topLevelItem(0)
    main_item.child(0).setExpanded(True)  # fully expand everything once

    dock.set_spans(list(SPANS), TOTAL_US, dict(COLOR_MAP))

    # Fresh tree: same default-expanded state as a brand-new dock.
    new_main = dock._tree.topLevelItem(0)
    new_foo = new_main.child(0)
    assert not _is_placeholder(new_foo)
    assert _is_placeholder(new_foo.child(0))
