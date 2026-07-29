#!/usr/bin/env python3
"""
Write demo benchmark/hardware/telemetry fragments into a run dir from the bundled
sample fixture. Lets the full pipeline (assemble -> report) and the MCP async run
lifecycle be exercised on a machine with no server hardware (CB_DEMO=1).

This is for try-it/CI/demo only -- real runs use the collectors + benchmarks.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FIXTURE = os.path.join(HERE, "..", "tests", "fixtures", "sample_results.json")


def write_demo_fragments(run_dir: str, fixture: str = DEFAULT_FIXTURE) -> str:
    os.makedirs(run_dir, exist_ok=True)
    with open(fixture, "r", encoding="utf-8") as fh:
        s = json.load(fh)

    def w_json(name, obj):
        with open(os.path.join(run_dir, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)

    def w_text(name, text):
        with open(os.path.join(run_dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    w_json("hardware.json", s.get("hardware", {}))
    w_json("telemetry.json", s.get("telemetry", {}))
    b = s.get("benchmarks", {})
    w_json("bench_storage.json", b.get("storage", []))
    w_json("bench_cpu.json", b.get("cpu", {}))
    w_json("bench_memory.json", b.get("memory", {}))
    w_json("bench_network.json", b.get("network", {}))
    # SEL cleared at start; one informational event after (no critical) for realism
    w_text("sel_before.txt", "PSU1 Status | Presence detected\n")
    w_text("sel_after.txt", "PSU1 Status | Presence detected\nPSU1 Input history logged (informational)\n")
    w_text("dmesg_before.txt", "kernel: Linux version 6.8.0\n")
    w_text("dmesg_after.txt", "kernel: Linux version 6.8.0\n")
    return run_dir


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print("usage: demo_data.py <run_dir> [fixture.json]", file=sys.stderr)
        return 2
    fixture = argv[1] if len(argv) > 1 else DEFAULT_FIXTURE
    rd = write_demo_fragments(argv[0], fixture)
    print(f"demo_data: wrote fragments to {rd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
