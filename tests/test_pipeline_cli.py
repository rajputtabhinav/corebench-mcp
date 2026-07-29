"""Integration test: split the sample into on-disk fragments, then exercise
load_fragments -> assemble -> render, mirroring what run.sh does on a real box."""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

import assemble  # noqa: E402
import generate_report  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_results.json")


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(obj, str):
            fh.write(obj)
        else:
            json.dump(obj, fh)


def test_fragments_to_report(tmp_path):
    with open(FIXTURE, encoding="utf-8") as fh:
        sample = json.load(fh)
    rd = tmp_path / "run"
    rd.mkdir()

    _write(str(rd / "hardware.json"), sample["hardware"])
    _write(str(rd / "telemetry.json"), sample["telemetry"])
    _write(str(rd / "bench_storage.json"), sample["benchmarks"]["storage"])
    _write(str(rd / "bench_cpu.json"), sample["benchmarks"]["cpu"])
    _write(str(rd / "bench_memory.json"), sample["benchmarks"]["memory"])
    _write(str(rd / "bench_network.json"), sample["benchmarks"]["network"])
    _write(str(rd / "bench_gpu.json"), sample["benchmarks"]["gpu"])
    _write(str(rd / "sel_before.txt"), "PSU1 Presence detected\nFan1 OK\n")
    _write(str(rd / "sel_after.txt"), "PSU1 Presence detected\nFan1 OK\nCPU1 Thermal Trip - Assertion\n")
    _write(str(rd / "dmesg_before.txt"), "boot ok\n")
    _write(str(rd / "dmesg_after.txt"), "boot ok\nnvme nvme3: I/O timeout, reset controller\n")

    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        config = json.load(fh)

    frags = assemble.load_fragments(str(rd))
    assert "hardware" in frags and "storage" in frags and "telemetry" in frags

    results = assemble.assemble(frags, config,
                               run_meta={"tier": "qualification", "date": "2026-06-05",
                                         "window": "2026-06-01 -> 2026-06-04"})
    # nvme3 drags storage to ACTION -> overall ACTION REQUIRED
    assert results["meta"]["status"] == "ACTION REQUIRED"
    assert results["meta"]["hostname"] == "ty-cam-38"
    # spec compliance computed for the representative drive + GPU
    pcts = {r["metric"]: r["pct"] for r in results["spec_rows"]}
    assert pcts["Seq Read (nvme0)"] == sys_approx(98.5)
    assert pcts["4K Rand Write (nvme3)"] < 75
    assert pcts["GEMM FP8 (per GPU)"] > 95           # 8x B200 at ~98% of datasheet
    # GPU subsystem present in the auto scorecard
    assert any("GPU" in row[0] for row in results["scorecard"]["rows"])

    out_json = rd / "results.json"
    _write(str(out_json), results)
    pdf = str(rd / "report.pdf")
    generate_report.build_report(results, pdf)
    data = open(pdf, "rb").read()
    assert data[:5] == b"%PDF-"
    assert len(re.findall(rb"/Type\s*/Page[^s]", data)) >= 5


def sys_approx(v, tol=0.3):
    class _A:
        def __eq__(self, other):
            return abs(other - v) <= tol
    return _A()
