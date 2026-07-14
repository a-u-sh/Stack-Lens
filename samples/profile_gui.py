"""cProfile ProfilerWindow construction against the worst-case synthetic
trace (full 131072-event ring buffer). Skips app.exec() (no interactive
event loop) but runs a few processEvents() passes so layout/paint
callbacks fire, then dumps a .pstats file for viewing with snakeviz.

    python samples/profile_gui.py
    snakeviz samples/profiles/gui_worst_case_construction.pstats
"""
import cProfile
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6 import QtWidgets

from trace_reader import TRACE_BUFFER_SIZE
from span_builder import parse_events, resolve_names, build_spans
from gen_worst_case_trace import gen_raw_buffer, ADDR_TO_NAME
from gui.main_window import ProfilerWindow


def main():
    n_events = TRACE_BUFFER_SIZE
    raw_buf, trace_idx = gen_raw_buffer(n_events)
    events = parse_events(raw_buf, trace_idx)
    resolve_names(events, ADDR_TO_NAME, elf_path=None)
    spans, marks, pause_regions = build_spans(events, cpu_mhz=96.0)
    print(f"{n_events} events -> spans={len(spans)} marks={len(marks)}")

    app = QtWidgets.QApplication(sys.argv)

    profiler = cProfile.Profile()
    t0 = time.perf_counter()
    profiler.enable()
    win = ProfilerWindow(spans, marks=marks, pause_regions=pause_regions, wrapped=False, cpu_mhz=96.0)
    win.show()
    for _ in range(10):
        app.processEvents()
    profiler.disable()
    wall = time.perf_counter() - t0
    print(f"wall time: {wall:.3f}s")

    win.close()
    app.quit()

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "gui_worst_case_construction.pstats")
    profiler.dump_stats(out_path)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
