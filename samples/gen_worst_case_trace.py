"""Reusable worst-case synthetic trace generator, for benchmarking.

Produces a raw trace event stream the size of a *full* ring buffer
(TRACE_BUFFER_SIZE events — the hard cap enforced by trace_buf in
cpp/ambiq/trace_common/trace.cpp) with realistic nested enter/exit call
patterns, ISR preemption, and mark events. Deterministic (fixed seed) so
repeated runs are comparable across optimization attempts.

Usage:
    from samples.gen_worst_case_trace import gen_raw_buffer, ADDR_TO_NAME

    raw_buf, trace_idx = gen_raw_buffer(TRACE_BUFFER_SIZE)  # or any smaller N
    events = parse_events(raw_buf, trace_idx)
    resolve_names(events, ADDR_TO_NAME, elf_path=None)
    spans, marks, pause_regions = build_spans(events, cpu_mhz=96.0)

Run directly to (re)generate samples/worst_case_131072.sltrace:
    python samples/gen_worst_case_trace.py
"""
import os
import random
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trace_reader import TRACE_BUFFER_SIZE, TRACE_EVENT_SIZE, EVENT_FMT

SEED = 0
N_FUNCS = 200
FUNC_NAMES = [f"func_{i}" for i in range(N_FUNCS)]
FUNC_ADDRS = {name: 0x08000000 + i * 4 for i, name in enumerate(FUNC_NAMES)}
ADDR_TO_NAME = {addr: name for name, addr in FUNC_ADDRS.items()}
for _addr, _name in list(ADDR_TO_NAME.items()):
    ADDR_TO_NAME[_addr & ~1] = _name


def gen_raw_buffer(n_events, seed=SEED):
    """Generate a packed raw trace buffer (worst case up to TRACE_BUFFER_SIZE
    events): nested thread calls (up to depth 8) with occasional marks,
    plus ISR preemption (~3% chance per thread-mode event, nested up to
    depth 4). Returns (raw_buf_bytes, trace_idx) matching what
    trace_reader.read_trace() would return from a live J-Link session.
    """
    rng = random.Random(seed)
    events = []  # (etype, ipsr, cyccnt, context)
    cyccnt = 0
    thread_stack = []
    isr_stack = []
    mark_addr = 0x08010000

    while len(events) < n_events:
        in_isr = isr_stack or (rng.random() < 0.03 and len(events) < n_events - 20)
        if in_isr:
            if not isr_stack or (rng.random() < 0.6 and len(isr_stack) < 4):
                addr = FUNC_ADDRS[rng.choice(FUNC_NAMES)]
                cyccnt += rng.randint(5, 50)
                events.append((0, 14, cyccnt, addr))
                isr_stack.append(addr)
            else:
                addr = isr_stack.pop()
                cyccnt += rng.randint(5, 50)
                events.append((1, 14, cyccnt, addr))
        else:
            r = rng.random()
            if r < 0.02:
                cyccnt += rng.randint(1, 10)
                events.append((2, 0, cyccnt, mark_addr))
            elif not thread_stack or (r < 0.55 and len(thread_stack) < 8):
                addr = FUNC_ADDRS[rng.choice(FUNC_NAMES)]
                cyccnt += rng.randint(5, 100)
                events.append((0, 0, cyccnt, addr))
                thread_stack.append(addr)
            else:
                addr = thread_stack.pop()
                cyccnt += rng.randint(5, 100)
                events.append((1, 0, cyccnt, addr))

    while thread_stack:
        cyccnt += 10
        events.append((1, 0, cyccnt, thread_stack.pop()))
    while isr_stack:
        cyccnt += 10
        events.append((1, 14, cyccnt, isr_stack.pop()))

    events = events[:n_events]

    raw = bytearray(TRACE_BUFFER_SIZE * TRACE_EVENT_SIZE)
    for i, (etype, ipsr, cyc, ctx) in enumerate(events):
        struct.pack_into(EVENT_FMT, raw, i * TRACE_EVENT_SIZE, etype, ipsr, cyc, ctx)
    return bytes(raw), len(events)


if __name__ == "__main__":
    from span_builder import parse_events, resolve_names, build_spans
    from trace_io import export_sltrace

    n_events = TRACE_BUFFER_SIZE
    raw_buf, trace_idx = gen_raw_buffer(n_events)
    events = parse_events(raw_buf, trace_idx)
    resolve_names(events, ADDR_TO_NAME, elf_path=None)
    spans, marks, pause_regions = build_spans(events, cpu_mhz=96.0)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worst_case_131072.sltrace")
    export_sltrace(
        out_path,
        spans,
        meta={
            "cpu_mhz": 96.0,
            "source": "Deterministic worst-case synthetic trace (samples/gen_worst_case_trace.py, seed=0) "
                      "at the full 131072-event ring-buffer cap, for benchmarking Stack-Lens itself.",
            "synthetic_trace_events": n_events,
        },
        marks=marks,
        pause_regions=pause_regions,
    )
    print(f"spans={len(spans)} marks={len(marks)} pause_regions={len(pause_regions)}")
    print(f"Wrote {out_path}")
