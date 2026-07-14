"""Launch the real Stack-Lens GUI with a synthetic trace loaded, then
auto-quit after a fixed duration. Meant to be run under py-spy / viztracer
so it samples the actual running app (real Qt event loop, real
construction + idle time), not a synthetic shortcut:

    py-spy record -o samples/profiles/gui_worst_case_pyspy.svg -- \
        <python> samples/launch_gui_worst_case.py [seconds] [n_events]

n_events defaults to the full 131072-event worst case. Pass a smaller
value for tracing profilers (viztracer) whose per-call overhead makes
the full worst case impractically slow.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6 import QtCore, QtWidgets

from trace_reader import TRACE_BUFFER_SIZE
from span_builder import parse_events, resolve_names, build_spans
from gen_worst_case_trace import gen_raw_buffer, ADDR_TO_NAME
from gui import show_gui

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
n_events = int(sys.argv[2]) if len(sys.argv) > 2 else TRACE_BUFFER_SIZE

raw_buf, trace_idx = gen_raw_buffer(n_events)
events = parse_events(raw_buf, trace_idx)
resolve_names(events, ADDR_TO_NAME, elf_path=None)
spans, marks, pause_regions = build_spans(events, cpu_mhz=96.0)
print(f"{n_events} events -> {len(spans)} spans, {len(marks)} marks", flush=True)


def _quit():
    QtWidgets.QApplication.instance().quit()


QtCore.QTimer.singleShot(int(seconds * 1000), _quit)
show_gui(spans, marks=marks, pause_regions=pause_regions, cpu_mhz=96.0)
