#!/usr/bin/env python3
"""
CoreBench MCP -- results assembler + auto-analysis.

Merges the per-run data fragments (hardware.json, telemetry.json, the benchmark
fragments, and the SEL/dmesg before/after captures) with ``config.json`` into a
single ``results.json`` -- the contract consumed by ``generate_report.py``.

Auto-analysis (no human arithmetic required), driven by ``spec_targets`` in the
config:
  * compute each metric's measured value as a % of its datasheet rating,
  * derive a PASS / LIMITED / ACTION verdict per metric and per subsystem
    (flag <90% as LIMITED, <75% as ACTION),
  * build the color-coded scorecard and an overall status,
  * surface every sub-threshold metric as a candidate finding,
  * diff the BMC SEL (before clear vs after run) and the dmesg level filter; any
    new ECC / thermal / PSU / reset / MCE / AER event becomes a flagged finding.

The functions in the "pure core" section take plain Python data and return plain
Python data -- no file IO, no hardware, no clock -- so they are unit-testable
(spec section 12).

CLI:
    python assemble.py <run_dir> [--config config.json] [--out results.json]
                       [--tier T] [--window W] [--date D] [--hostname H]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
#  Pure core -- unit-testable without hardware / files / clock.               #
# --------------------------------------------------------------------------- #
DEFAULT_THRESHOLDS = {"limited": 90.0, "action": 75.0}
_VERDICT_RANK = {"ACTION": 3, "LIMITED": 2, "PASS": 1, "N/A": 0}

# Keywords that promote a *new* SEL / dmesg line to a flagged finding.
_SEL_KEYWORDS = ("ecc", "thermal", "over-temp", "overtemp", "psu", "power supply",
                 "fault", "critical", "fail", "correctable", "uncorrectable",
                 "voltage", "fan")
_DMESG_KEYWORDS = ("mce", "machine check", "hardware error", "pcie bus error",
                   "aer", "nvme", "reset", "ecc", "thermal", "throttl",
                   "link is down", "link down", "corrected error", "i/o error",
                   "controller is down")


def verdict_for_pct(pct: Optional[float], limited: float = 90.0,
                    action: float = 75.0) -> str:
    """PASS if >= limited%, LIMITED if >= action%, else ACTION. N/A if unknown."""
    if pct is None:
        return "N/A"
    if pct < action:
        return "ACTION"
    if pct < limited:
        return "LIMITED"
    return "PASS"


def worst_verdict(verdicts: Sequence[str]) -> str:
    """Return the most severe verdict (ACTION > LIMITED > PASS > N/A)."""
    real = [v for v in verdicts if v in _VERDICT_RANK]
    if not real:
        return "N/A"
    return max(real, key=lambda v: _VERDICT_RANK[v])


def resolve_measured(benchmarks: Dict[str, Any], frm: Dict[str, Any]) -> Any:
    """Pull a measured value out of the benchmark fragments per a ``from`` spec.

    ``from`` = {"fragment": "storage", "match": {"label": "nvme0"}, "field": "seq_read_gbs"}
    or, for dict fragments, {"fragment": "memory", "field": "stream_triad_gbs"}.
    """
    if not frm:
        return None
    frag = benchmarks.get(frm.get("fragment"))
    field = frm.get("field")
    if frag is None or field is None:
        return None
    match = frm.get("match")
    if isinstance(frag, list):
        for item in frag:
            if isinstance(item, dict) and all(item.get(k) == v for k, v in (match or {}).items()):
                return item.get(field)
        return None
    if isinstance(frag, dict):
        return frag.get(field)
    return None


def compute_spec_rows(benchmarks: Dict[str, Any], spec_targets: Sequence[Dict[str, Any]],
                      thresholds: Optional[Dict[str, float]] = None) -> List[Dict[str, Any]]:
    """For each datasheet target, resolve the measured value and compute % of rating."""
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    rows: List[Dict[str, Any]] = []
    for sp in spec_targets:
        measured = resolve_measured(benchmarks, sp.get("from", {}))
        rated = sp.get("rated")
        pct: Optional[float] = None
        if measured is not None and rated:
            try:
                pct = 100.0 * float(measured) / float(rated)
            except (TypeError, ValueError, ZeroDivisionError):
                pct = None
        rows.append({
            "metric": sp.get("metric"),
            "measured": measured,
            "rated": rated,
            "unit": sp.get("unit", ""),
            "subsystem": sp.get("subsystem", "Other"),
            "pct": round(pct, 1) if pct is not None else None,
            "verdict": verdict_for_pct(pct, thr["limited"], thr["action"]),
        })
    return rows


def build_scorecard(spec_rows: Sequence[Dict[str, Any]],
                    thresholds: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Aggregate spec rows into a per-subsystem scorecard; verdict = worst metric."""
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    by_sub: "Dict[str, List[Dict[str, Any]]]" = {}
    order: List[str] = []
    for r in spec_rows:
        sub = r.get("subsystem", "Other")
        if sub not in by_sub:
            by_sub[sub] = []
            order.append(sub)
        by_sub[sub].append(r)

    rows: List[List[Any]] = []
    for sub in order:
        items = by_sub[sub]
        verdicts = [verdict_for_pct(i.get("pct"), thr["limited"], thr["action"]) for i in items]
        v = worst_verdict(verdicts)
        pcts = [i["pct"] for i in items if i.get("pct") is not None]
        if pcts:
            worst_item = min(items, key=lambda i: i["pct"] if i.get("pct") is not None else 1e9)
            result = f"min {min(pcts):.0f}% of rating across {len(items)} metric(s)"
            detail = f"lowest: {worst_item['metric']} at {worst_item['pct']:.0f}%"
        else:
            result, detail = "n/a", "no measured values"
        rows.append([sub, result, detail, v])
    return {"header": ["Subsystem", "Result", "Detail", "Verdict"], "rows": rows}


def thermal_scorecard_row(telemetry: Dict[str, Any],
                          benchmarks: Dict[str, Any]) -> Optional[List[Any]]:
    """Build a Power & Thermal scorecard row from the telemetry timeline."""
    if not telemetry:
        return None
    wall = telemetry.get("wall_w") or []
    inlet = telemetry.get("inlet_c") or []
    outlet = telemetry.get("outlet_c") or []
    peak_w = max(wall) if wall else None
    peak_in = max(inlet) if inlet else None
    peak_out = max(outlet) if outlet else None
    throttle = (benchmarks.get("cpu") or {}).get("throttle_events")
    verdict = "ACTION" if (throttle or 0) else ("PASS" if peak_w is not None else "N/A")
    result = f"Peak {peak_w / 1000:.2f} kW" if peak_w is not None else "n/a"
    bits = []
    if peak_in is not None:
        bits.append(f"inlet {peak_in:.0f}C")
    if peak_out is not None:
        bits.append(f"outlet {peak_out:.0f}C")
    bits.append(f"throttle events: {throttle if throttle is not None else 'n/a'}")
    return ["Power & Thermal", result, "; ".join(bits), verdict]


def candidate_findings(spec_rows: Sequence[Dict[str, Any]],
                       thresholds: Optional[Dict[str, float]] = None) -> List[str]:
    """Every sub-threshold metric becomes a candidate finding line."""
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    out: List[str] = []
    for r in spec_rows:
        pct = r.get("pct")
        if pct is None or pct >= thr["limited"]:
            continue
        v = verdict_for_pct(pct, thr["limited"], thr["action"])
        out.append(
            f"[{v}] {r.get('metric')}: measured {r.get('measured')} {r.get('unit')} "
            f"= {pct:.0f}% of the {r.get('rated')} {r.get('unit')} datasheet rating."
        )
    return out


def _new_lines(before: Sequence[str], after: Sequence[str]) -> List[str]:
    seen = {ln.strip() for ln in before}
    return [ln.strip() for ln in after if ln.strip() and ln.strip() not in seen]


def diff_sel(before: Sequence[str], after: Sequence[str]) -> Tuple[str, List[str]]:
    """Diff BMC SEL before-clear vs after-run; flag new critical-ish events."""
    new = _new_lines(before, after)
    crit = [ln for ln in new if any(k in ln.lower() for k in _SEL_KEYWORDS)]
    if not before and not after:
        return "SEL not captured (n/a).", []
    status = (f"SEL cleared at start; {len(new)} new entr"
              f"{'y' if len(new) == 1 else 'ies'} after run "
              f"({len(crit)} flagged).")
    findings = [f"New BMC SEL event: {ln}" for ln in crit]
    return status, findings


def diff_dmesg(before: Sequence[str], after: Sequence[str]) -> Tuple[str, List[str]]:
    """Diff dmesg before vs after; flag new MCE / ECC / AER / reset / thermal lines."""
    new = _new_lines(before, after)
    flagged = [ln for ln in new if any(k in ln.lower() for k in _DMESG_KEYWORDS)]
    if not before and not after:
        return "dmesg not captured (n/a).", []
    if not flagged:
        status = f"No new MCE/ECC/PCIe-AER/NVMe-reset/thermal events ({len(new)} new lines, none flagged)."
    else:
        status = f"{len(flagged)} new kernel event(s) flagged out of {len(new)} new lines."
    findings = [f"New kernel-log event: {ln}" for ln in flagged]
    return status, findings


def status_from_scorecard(scorecard: Optional[Dict[str, Any]]) -> str:
    """Overall status string from the worst scorecard verdict."""
    if not scorecard or not scorecard.get("rows"):
        return "N/A"
    w = worst_verdict([str(row[-1]) for row in scorecard["rows"]])
    return {
        "PASS": "PASS",
        "LIMITED": "PASS WITH LIMITATIONS",
        "ACTION": "ACTION REQUIRED",
        "N/A": "N/A",
    }[w]


def auto_health(benchmarks: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a component-health table from storage benchmark drives."""
    drives = benchmarks.get("storage")
    if not drives:
        return None
    rows = []
    for d in drives:
        warn = d.get("link_width_warn")
        rows.append([
            f"{d.get('label') or d.get('dev')} {d.get('model', '')}".strip(),
            d.get("link", "n/a"),
            d.get("temp_pre_post", "n/a"),
            d.get("wear", "n/a"),
            d.get("media_errors", "n/a"),
            "ACTION" if warn else "PASS",
        ])
    return {"header": ["Component", "Link", "Temp pre->post", "Wear", "Media errors", "Verdict"],
            "rows": rows}


def _is_auto(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "auto"))


def assemble(fragments: Dict[str, Any], config: Dict[str, Any],
             run_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge fragments + config into a complete results dict (pure: no file IO)."""
    run_meta = run_meta or {}
    thresholds = {**DEFAULT_THRESHOLDS, **(config.get("thresholds") or {})}

    hardware = fragments.get("hardware") or {}
    benchmarks: Dict[str, Any] = {}
    # benchmark fragments may be provided at top level or nested under "benchmarks"
    nested = fragments.get("benchmarks") or {}
    for key in ("storage", "cpu", "memory", "network", "gpu"):
        if key in fragments and fragments[key] is not None:
            benchmarks[key] = fragments[key]
        elif key in nested and nested[key] is not None:
            benchmarks[key] = nested[key]
    telemetry = fragments.get("telemetry")

    spec_rows = compute_spec_rows(benchmarks, config.get("spec_targets") or [], thresholds)

    if config.get("auto_scorecard", True):
        scorecard = build_scorecard(spec_rows, thresholds)
        trow = thermal_scorecard_row(telemetry, benchmarks)
        if trow:
            scorecard["rows"].append(trow)
    else:
        scorecard = config.get("scorecard")

    # findings: config narrative first, then auto candidates, then log diffs
    findings: List[str] = list(config.get("findings") or [])
    findings += candidate_findings(spec_rows, thresholds)
    sel_status, sel_find = diff_sel(fragments.get("sel_before") or [],
                                    fragments.get("sel_after") or [])
    dmesg_status, dmesg_find = diff_dmesg(fragments.get("dmesg_before") or [],
                                          fragments.get("dmesg_after") or [])
    findings += sel_find + dmesg_find

    status = status_from_scorecard(scorecard)

    # meta: config branding, with "auto" fields filled from data / run_meta
    meta: Dict[str, Any] = dict(config.get("meta") or {})
    sysd = hardware.get("system") or {}
    cpud = hardware.get("cpu") or {}
    auto_platform = " / ".join(
        x for x in [f"{sysd.get('vendor', '')} {sysd.get('model', '')}".strip(),
                    str(cpud.get("model", "")).strip()] if x
    ) or "n/a"
    auto_fill = {
        "hostname": sysd.get("hostname") or run_meta.get("hostname"),
        "server_ip": run_meta.get("server_ip"),
        "platform": auto_platform,
        "tier": run_meta.get("tier"),
        "window": run_meta.get("window"),
        "date": run_meta.get("date"),
        "prepared_by": run_meta.get("prepared_by"),
        "status": status,
    }
    for k, v in auto_fill.items():
        if _is_auto(meta.get(k)) and v is not None:
            meta[k] = v
    meta.setdefault("company", "CoreBench")
    meta.setdefault("classification", "INTERNAL")

    results: Dict[str, Any] = {
        "meta": meta,
        "hardware": hardware,
        "benchmarks": benchmarks,
        "spec_rows": [{k: r[k] for k in ("metric", "measured", "rated", "unit", "pct")}
                      for r in spec_rows],
    }
    if scorecard:
        results["scorecard"] = scorecard
    if config.get("executive_summary"):
        results["executive_summary"] = config["executive_summary"]
    if telemetry:
        results["telemetry"] = telemetry
    if config.get("perf_per_watt"):
        results["perf_per_watt"] = config["perf_per_watt"]
    if config.get("comparison"):
        results["comparison"] = config["comparison"]
    health = config.get("health") or auto_health(benchmarks)
    if health:
        results["health"] = health
    results["sel_status"] = sel_status
    results["dmesg_status"] = dmesg_status
    if findings:
        results["findings"] = findings
    if config.get("recommendations"):
        results["recommendations"] = config["recommendations"]
    if config.get("conclusion"):
        results["conclusion"] = config["conclusion"]
    return results


# --------------------------------------------------------------------------- #
#  File IO layer                                                               #
# --------------------------------------------------------------------------- #
# fragment filename -> (results-key, kind)   kind: json | lines
FRAGMENT_FILES = {
    "hardware.json": ("hardware", "json"),
    "telemetry.json": ("telemetry", "json"),
    "storage.json": ("storage", "json"),
    "bench_storage.json": ("storage", "json"),
    "cpu.json": ("cpu", "json"),
    "bench_cpu.json": ("cpu", "json"),
    "memory.json": ("memory", "json"),
    "bench_memory.json": ("memory", "json"),
    "network.json": ("network", "json"),
    "bench_network.json": ("network", "json"),
    "gpu.json": ("gpu", "json"),
    "bench_gpu.json": ("gpu", "json"),
    "sel_before.txt": ("sel_before", "lines"),
    "sel_after.txt": ("sel_after", "lines"),
    "dmesg_before.txt": ("dmesg_before", "lines"),
    "dmesg_after.txt": ("dmesg_after", "lines"),
}


def load_fragments(run_dir: str) -> Dict[str, Any]:
    """Load all recognised fragment files from a run directory (each optional)."""
    frags: Dict[str, Any] = {}
    for fname, (key, kind) in FRAGMENT_FILES.items():
        path = os.path.join(run_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                if kind == "json":
                    frags[key] = json.load(fh)
                else:
                    frags[key] = fh.read().splitlines()
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: could not read {fname}: {exc}", file=sys.stderr)
    return frags


def bundle_sha256(run_dir: str) -> str:
    """SHA-256 over the raw fragment files (sorted), for the reproducibility appendix."""
    h = hashlib.sha256()
    for fname in sorted(FRAGMENT_FILES):
        path = os.path.join(run_dir, fname)
        if os.path.isfile(path):
            h.update(fname.encode("utf-8"))
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
    return h.hexdigest()


def _load_config(path: str) -> Dict[str, Any]:
    if not path or not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Assemble results.json from a run dir.")
    ap.add_argument("run_dir")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None, help="default: <run_dir>/results.json")
    ap.add_argument("--tier", default=None)
    ap.add_argument("--window", default=None)
    ap.add_argument("--date", default=None)
    ap.add_argument("--hostname", default=None)
    ap.add_argument("--server-ip", default=None)
    ap.add_argument("--prepared-by", default=None)
    args = ap.parse_args(list(argv) if argv is not None else None)

    here = os.path.dirname(os.path.abspath(__file__))
    cfg_path = args.config or os.path.join(here, "..", "config.json")
    config = _load_config(cfg_path)
    fragments = load_fragments(args.run_dir)

    run_meta = {k: v for k, v in {
        "tier": args.tier, "window": args.window, "date": args.date,
        "hostname": args.hostname, "server_ip": args.server_ip,
        "prepared_by": args.prepared_by,
    }.items() if v is not None}

    results = assemble(fragments, config, run_meta)

    repro: Dict[str, Any] = dict(config.get("reproducibility") or {})
    repro.setdefault("kernel_cmdline", (results.get("hardware", {}).get("kernel", {}) or {}).get("cmdline", "n/a"))
    repro["data_bundle_sha256"] = bundle_sha256(args.run_dir)
    repro.setdefault("run_dir", os.path.basename(os.path.abspath(args.run_dir)))
    repro.setdefault("commands", "see run.log for exact invocations")
    results["reproducibility"] = repro

    out = args.out or os.path.join(args.run_dir, "results.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"wrote {out}  (status: {results['meta'].get('status')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
