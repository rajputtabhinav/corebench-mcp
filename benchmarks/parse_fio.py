#!/usr/bin/env python3
"""
Parse fio --output-format=json results into CoreBench drive metric objects.

Kept separate from the Bash runner so the (error-prone) JSON math is pure and
unit-testable without fio or hardware. ``bench_storage.sh`` runs fio per
workload, saving ``fio.<label>.<workload>.json`` next to a ``storage_meta.json``
inventory; this merges them into ``bench_storage.json`` (the report's contract).

Workload files consumed (each optional -> field simply absent):
  seqread seqwrite randread randwrite qd1read qd1write steady

CLI:  parse_fio.py <run_dir> [-o bench_storage.json]
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional


def load(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def first_job(doc: Dict[str, Any]) -> Dict[str, Any]:
    jobs = doc.get("jobs") or []
    return jobs[0] if jobs else {}


def _lat_block(op: Dict[str, Any]) -> Dict[str, Any]:
    # fio reports completion latency in clat_ns; fall back to total lat_ns.
    return op.get("clat_ns") or op.get("lat_ns") or {}


def bw_gbs(op: Dict[str, Any]) -> Optional[float]:
    """Bandwidth in GB/s (decimal). Prefer bw_bytes; else bw is KiB/s."""
    b = op.get("bw_bytes")
    if b is None:
        kib = op.get("bw")
        if kib is None:
            return None
        b = float(kib) * 1024.0
    return round(float(b) / 1e9, 3)


def iops(op: Dict[str, Any]) -> Optional[float]:
    v = op.get("iops")
    return round(float(v)) if v is not None else None


def _us(ns: Optional[float]) -> Optional[float]:
    return None if ns is None else round(float(ns) / 1000.0, 1)


def percentiles_us(op: Dict[str, Any]) -> Dict[str, Optional[float]]:
    lat = _lat_block(op)
    p = lat.get("percentile") or {}

    def gk(*names: str) -> Optional[float]:
        for n in names:
            if n in p:
                return p[n]
        return None

    return {
        "p99": _us(gk("99.000000", "99.0")),
        "p99_9": _us(gk("99.900000", "99.9")),
        "p99_99": _us(gk("99.990000", "99.99")),
        "p99_999": _us(gk("99.999000", "99.999")),
        "max": _us(lat.get("max")),
    }


def mean_lat_us(op: Dict[str, Any]) -> Optional[float]:
    return _us(_lat_block(op).get("mean"))


def build_drive(meta: Dict[str, Any], run_dir: str) -> Dict[str, Any]:
    """Merge a drive's inventory meta with its per-workload fio results."""
    label = meta.get("label") or meta.get("dev") or "drive"
    d: Dict[str, Any] = dict(meta)

    def wl(name: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(run_dir, f"fio.{label}.{name}.json")
        return load(path) if os.path.isfile(path) else None

    sr, sw = wl("seqread"), wl("seqwrite")
    rr, rw = wl("randread"), wl("randwrite")
    q1r, q1w = wl("qd1read"), wl("qd1write")
    steady = wl("steady")

    tail: Dict[str, Any] = {}
    if sr is not None:
        d["seq_read_gbs"] = bw_gbs(first_job(sr).get("read", {}))
    if sw is not None:
        d["seq_write_gbs"] = bw_gbs(first_job(sw).get("write", {}))
    if rr is not None:
        rd = first_job(rr).get("read", {})
        d["rand_read_iops"] = iops(rd)
        tail["read"] = percentiles_us(rd)
        d["p99_9_read_us"] = tail["read"]["p99_9"]
    if rw is not None:
        wd = first_job(rw).get("write", {})
        fob = iops(wd)
        d["fob_rand_write_iops"] = fob          # fresh-out-of-box
        d["rand_write_iops"] = fob              # may be overridden by steady below
        tail["write"] = percentiles_us(wd)
    if steady is not None:
        sd = first_job(steady).get("write", {})
        sv = iops(sd)
        d["steady_rand_write_iops"] = sv
        d["rand_write_iops"] = sv               # steady-state is the headline number
    if q1r is not None:
        d["qd1_read_us"] = mean_lat_us(first_job(q1r).get("read", {}))
    if q1w is not None:
        d["qd1_write_us"] = mean_lat_us(first_job(q1w).get("write", {}))
    if tail:
        d["tail"] = tail
    return d


def build_all(run_dir: str) -> List[Dict[str, Any]]:
    meta_path = os.path.join(run_dir, "storage_meta.json")
    meta = load(meta_path) if os.path.isfile(meta_path) else []
    if isinstance(meta, dict):
        meta = meta.get("drives", [])
    return [build_drive(m, run_dir) for m in meta]


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print("usage: parse_fio.py <run_dir> [-o out.json]", file=sys.stderr)
        return 2
    run_dir = argv[0]
    out = os.path.join(run_dir, "bench_storage.json")
    if "-o" in argv:
        out = argv[argv.index("-o") + 1]
    drives = build_all(run_dir)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(drives, fh, indent=2)
    print(f"parse_fio: wrote {out} ({len(drives)} drive(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
