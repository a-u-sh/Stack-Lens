"""Regression tests for ProfilerWindow's startup ordering.

Guards against the bug where the window was built once with hardcoded
defaults (Dark theme, Default palette) and then, if the user's saved
settings differed, immediately rebuilt (stylesheet re-applied, call
graph rebuilt, flame chart repopulated) to reflect the real settings.
Theme/palette should now be read and applied *before* the first build,
so there is exactly one build, already showing the right thing.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from gui.main_window import ProfilerWindow
from gui.theme import THEME, THEMES, apply_theme
from gui.constants import PALETTES

SPANS = [
    {"name": "main", "addr": 1, "start_us": 0, "end_us": 10, "duration_us": 10, "depth": 0, "ipsr": 0},
    {"name": "foo", "addr": 2, "start_us": 1, "end_us": 5, "duration_us": 4, "depth": 1, "ipsr": 0},
    {"name": "bar", "addr": 3, "start_us": 6, "end_us": 9, "duration_us": 3, "depth": 1, "ipsr": 0},
]


@pytest.fixture(autouse=True)
def _restore_theme():
    yield
    apply_theme("Dark")


@pytest.fixture
def saved_settings(tmp_path, monkeypatch):
    """Point ProfilerWindow at a scratch settings file with a non-default
    theme + palette, instead of the real ~/.cortexm0_profiler.json."""
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"theme": "Light", "palette": "Colorblind-safe"}))
    monkeypatch.setattr(ProfilerWindow, "_SETTINGS_FILE", settings_file)
    return settings_file


def test_non_default_theme_and_palette_applied_on_first_build(qapp, saved_settings):
    win = ProfilerWindow(list(SPANS))
    try:
        assert THEME == THEMES["Light"]
        assert win._palette_name == "Colorblind-safe"
        assert win.color_map == win._compute_palette_color_map("Colorblind-safe")
    finally:
        win.close()


def test_populate_plot_called_exactly_once_on_startup(qapp, saved_settings, monkeypatch):
    calls = []
    original = ProfilerWindow._populate_plot

    def counting_populate_plot(self, *a, **kw):
        calls.append(1)
        return original(self, *a, **kw)

    monkeypatch.setattr(ProfilerWindow, "_populate_plot", counting_populate_plot)

    win = ProfilerWindow(list(SPANS))
    try:
        assert len(calls) == 1
    finally:
        win.close()


def test_on_theme_changed_not_invoked_during_startup(qapp, saved_settings, monkeypatch):
    calls = []
    original = ProfilerWindow._on_theme_changed

    def counting_on_theme_changed(self, *a, **kw):
        calls.append(1)
        return original(self, *a, **kw)

    monkeypatch.setattr(ProfilerWindow, "_on_theme_changed", counting_on_theme_changed)

    win = ProfilerWindow(list(SPANS))
    try:
        assert calls == []
    finally:
        win.close()


def test_default_settings_still_use_default_palette(qapp, tmp_path, monkeypatch):
    """No saved settings at all -> Dark theme, Default palette, same as before."""
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(ProfilerWindow, "_SETTINGS_FILE", settings_file)

    win = ProfilerWindow(list(SPANS))
    try:
        assert THEME == THEMES["Dark"]
        assert win._palette_name == "Default"
    finally:
        win.close()
