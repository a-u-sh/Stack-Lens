"""cProfile the trace-processing pipeline against the worst-case synthetic
trace (full 131072-event ring buffer) and save a .pstats file for viewing
with snakeviz / gprof2dot / any pstats-compatible tool.

    python samples/profile_pipeline.py
    snakeviz samples/profiles/pipeline_worst_case.pstats
"""
import cProfile
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trace_reader import TRACE_BUFFER_SIZE
from span_builder import parse_events, resolve_names, build_spans, build_call_tree, compute_stats, compute_stats_no_isr
from gen_worst_case_trace import gen_raw_buffer, ADDR_TO_NAME


def run_pipeline(raw_buf, trace_idx):
    events = parse_events(raw_buf, trace_idx)
    resolve_names(events, ADDR_TO_NAME, elf_path=None)
    spans, marks, pause_regions = build_spans(events, cpu_mhz=96.0)
    tree = build_call_tree(spans)
    stats = compute_stats(spans)
    stats_no_isr = compute_stats_no_isr(spans)
    return spans, marks, pause_regions, tree, stats, stats_no_isr


def main():
    n_events = TRACE_BUFFER_SIZE
    raw_buf, trace_idx = gen_raw_buffer(n_events)

    profiler = cProfile.Profile()
    profiler.enable()
    spans, *_ = run_pipeline(raw_buf, trace_idx)
    profiler.disable()

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "pipeline_worst_case.pstats")
    profiler.dump_stats(out_path)
    print(f"{n_events} events -> {len(spans)} spans")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
