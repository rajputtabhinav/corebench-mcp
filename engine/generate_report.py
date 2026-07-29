#!/usr/bin/env python3
"""
CoreBench MCP -- Branded PDF report engine.

Consumes a ``results.json`` (see the schema in README.md / the project spec) and
renders a multi-page, branded A4 PDF. Every section is optional: missing
sections are skipped gracefully, so the same renderer serves an `acceptance`
run (hardware + storage only) and a full `deep-dive` campaign.

Design notes
------------
* Charts are built with matplotlib (Agg backend) and embedded as in-memory PNGs.
  No headless browser dependency.
* The ``build_*_chart`` functions are PURE: they take plain Python data and
  return a matplotlib ``Figure``. They can be unit-tested without hardware,
  reportlab, or a results file (spec section 12).
* Layout is reportlab Platypus. Header bar + footer are drawn on every page via
  per-template ``onPage`` callbacks.

Rebrand by editing the ``THEME`` dict near the top of this file.

CLI
---
    python generate_report.py <results.json> <out.pdf>
"""

from __future__ import annotations

import io
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")  # headless; must be set before pyplot import
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    ListFlowable,
    ListItem,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

try:  # vector technical diagrams (rack / rail-kit / motherboard) -- same package
    import diagrams as _diagrams
except Exception:  # pragma: no cover
    _diagrams = None

# --------------------------------------------------------------------------- #
#  THEME -- edit this block to rebrand the whole report.                       #
# --------------------------------------------------------------------------- #
THEME = {
    "primary": "#1F3863",   # Netweb navy (header/footer bars, table headers)
    "accent":  "#BF0303",   # Netweb red (accent stripe, subtitles, secondary series)
    "gold":    "#D4A53A",   # tagline gold
    "pass":    "#2E8B57",   # PASS verdict (green)
    "limited": "#D9930A",   # LIMITED verdict (amber)
    "action":  "#BF0303",   # ACTION verdict (red)
    "ink":     "#1F2933",   # body text
    "muted":   "#6B7280",   # secondary text
    "rule":    "#D0D5DD",   # hairlines
    "band":    "#EEF2F7",   # zebra row tint
    "tagline": "Empowering Compute, Network, Storage & AI",
    "company": "Netweb Technologies India Limited",
}

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN
TOP_BAR = 18 * mm
HERO_H = 62 * mm
FOOTER_H = 11 * mm

# Brand logos (white-on-transparent, for the navy bars). Drawn if present.
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")
LOGO_NETWEB = os.path.join(ASSETS_DIR, "netweb_white.png")
LOGO_TYRONE = os.path.join(ASSETS_DIR, "tyrone_white.png")


# --------------------------------------------------------------------------- #
#  Color + format helpers                                                      #
# --------------------------------------------------------------------------- #
def _c(hex_: str) -> colors.Color:
    return colors.HexColor(hex_)


PRIMARY = _c(THEME["primary"])
ACCENT = _c(THEME["accent"])
GOLD = _c(THEME["gold"])
PASSC = _c(THEME["pass"])
LIMITC = _c(THEME["limited"])
ACTIONC = _c(THEME["action"])
INK = _c(THEME["ink"])
MUTED = _c(THEME["muted"])
RULE = _c(THEME["rule"])
BAND = _c(THEME["band"])
WHITE = colors.white


def verdict_fill(text: Any) -> Optional[colors.Color]:
    """Map a verdict string to a fill color (or None if not a known verdict).

    Keyword-based so multi-word statuses resolve sensibly, e.g.
    "PASS WITH LIMITATIONS" -> amber (LIMIT wins over PASS), "ACTION REQUIRED" -> red.
    """
    t = str(text).strip().upper()
    if not t:
        return None
    if any(k in t for k in ("ACTION", "FAIL", "CRITICAL", "FAULT", "DAMAG")) or t in ("RED", "NO"):
        return ACTIONC
    if any(k in t for k in ("LIMIT", "WARN", "MARGINAL", "CONDITION", "REVIEW")) or t == "AMBER":
        return LIMITC
    if any(k in t for k in ("PASS", "HEALTH", "WORKING")) or t in ("OK", "GOOD", "GREEN", "YES"):
        return PASSC
    return None


def mpl_pct_color(pct: float) -> str:
    """Spec-compliance bar color: >=90 green, 75-90 amber, <75 red."""
    if pct >= 90:
        return THEME["pass"]
    if pct >= 75:
        return THEME["limited"]
    return THEME["action"]


def fmt_iops(x: Any) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x if x is not None else "n/a")
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}k"
    return f"{v:.0f}"


def fmt_num(x: Any, nd: int = 1) -> str:
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return str(x if x is not None else "n/a")


def fmt_g(x: Any) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x if x is not None else "n/a")
    if v >= 1000:
        return f"{v:,.0f}"
    if v == int(v):
        return f"{int(v)}"
    return f"{v:g}"


def g(d: Any, *keys: str, default: str = "n/a") -> Any:
    """Safe nested get; returns ``default`` for any missing/None hop."""
    cur = d
    for k in keys:
        if isinstance(cur, dict) and cur.get(k) is not None:
            cur = cur[k]
        else:
            return default
    return cur


# --------------------------------------------------------------------------- #
#  Chart builders -- PURE: data -> matplotlib Figure (unit-testable).          #
# --------------------------------------------------------------------------- #
def _iops_formatter() -> mticker.FuncFormatter:
    def _f(v, _pos):
        if v >= 1e6:
            return f"{v / 1e6:.1f}M"
        if v >= 1e3:
            return f"{v / 1e3:.0f}k"
        return f"{v:.0f}"

    return mticker.FuncFormatter(_f)


def build_storage_perf_chart(drives: Sequence[Dict[str, Any]]):
    labels = [d.get("label") or d.get("dev") or f"drv{i}" for i, d in enumerate(drives)]
    x = np.arange(len(drives))
    w = 0.38
    fig, axes = plt.subplots(1, 3, figsize=(9.2, 3.2))

    def _f(key):
        return [float(d.get(key) or 0) for d in drives]

    panels = [
        (axes[0], "Sequential BW (GB/s)", _f("seq_read_gbs"), _f("seq_write_gbs"), False, THEME["pass"]),
        (axes[1], "4K Random (IOPS)", _f("rand_read_iops"), _f("rand_write_iops"), True, THEME["limited"]),
        (axes[2], "QD1 latency (us)", _f("qd1_read_us"), _f("qd1_write_us"), False, THEME["action"]),
    ]
    for ax, title, rd, wr, is_iops, write_color in panels:
        ax.bar(x - w / 2, rd, w, label="Read", color=THEME["primary"])
        ax.bar(x + w / 2, wr, w, label="Write", color=write_color)
        ax.set_title(title, fontsize=9)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=7)
        ax.grid(axis="y", ls=":", alpha=0.4)
        ax.legend(fontsize=7)
        if is_iops:
            ax.yaxis.set_major_formatter(_iops_formatter())
        ax.tick_params(axis="y", labelsize=7)
    fig.tight_layout()
    return fig


def build_spec_compliance_chart(rows: Sequence[Dict[str, Any]]):
    names = [r.get("metric", "?") for r in rows]
    pcts = [float(r.get("pct") or 0) for r in rows]
    cols = [mpl_pct_color(p) for p in pcts]
    fig, ax = plt.subplots(figsize=(7.4, 0.7 + 0.5 * max(1, len(rows))))
    y = np.arange(len(rows))
    ax.barh(y, pcts, color=cols)
    ax.axvline(100, ls="--", color="#555", lw=1.0)
    ax.set_yticks(y)
    ax.set_yticklabels(names, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("% of datasheet rating", fontsize=8)
    ax.set_xlim(0, max(120, (max(pcts) + 12) if pcts else 120))
    for i, p in enumerate(pcts):
        ax.text(p + 1, i, f"{p:.0f}%", va="center", fontsize=8)
    ax.tick_params(axis="x", labelsize=7)
    fig.tight_layout()
    return fig


_TAIL_ORDER = [
    ("p99", "p99"),
    ("p99_9", "p99.9"),
    ("p99_99", "p99.99"),
    ("p99_999", "p99.999"),
    ("max", "max"),
]


def build_tail_latency_chart(tail: Dict[str, Any]):
    xs = np.arange(len(_TAIL_ORDER))
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    for series, color, lbl in (
        ("read", THEME["primary"], "Read"),
        ("write", THEME["accent"], "Write"),
    ):
        d = tail.get(series) or {}
        ys = [d.get(k) for k, _ in _TAIL_ORDER]
        if all(v is None for v in ys):
            continue
        ax.plot(xs, [float(v or 0) for v in ys], marker="o", color=color, label=lbl)
    ax.set_xticks(xs)
    ax.set_xticklabels([lab for _, lab in _TAIL_ORDER], fontsize=8)
    ax.set_ylabel("latency (us)", fontsize=8)
    ax.set_yscale("log")
    ax.set_title("Tail latency -- the nines", fontsize=9)
    ax.grid(True, which="both", ls=":", alpha=0.4)
    ax.legend(fontsize=8)
    fig.tight_layout()
    return fig


def build_pcie_chart(drives: Sequence[Dict[str, Any]]):
    labels = [d.get("label") or d.get("dev") or f"drv{i}" for i, d in enumerate(drives)]
    x = np.arange(len(drives))
    w = 0.26
    cap = [float(d.get("link_capable_gbs") or 0) for d in drives]
    neg = [float(d.get("link_negotiated_gbs") or 0) for d in drives]
    meas = [float(d.get("seq_read_gbs") or 0) for d in drives]
    fig, ax = plt.subplots(figsize=(7.4, 3.3))
    ax.bar(x - w, cap, w, label="Capable", color="#9AA7B0")
    ax.bar(x, neg, w, label="Negotiated", color=THEME["primary"])
    ax.bar(x + w, meas, w, label="Measured read", color=THEME["accent"])
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
    ax.set_ylabel("GB/s", fontsize=8)
    ax.set_title("PCIe link -- capable vs negotiated vs measured", fontsize=9)
    ax.grid(axis="y", ls=":", alpha=0.4)
    ax.legend(fontsize=7)
    for i, d in enumerate(drives):
        if d.get("link_width_warn"):
            top = max(cap[i], neg[i], meas[i])
            ax.text(x[i], top, "!", color=THEME["action"], ha="center", va="bottom",
                    fontsize=12, fontweight="bold")
    fig.tight_layout()
    return fig


def build_comparison_chart(comp: Dict[str, Any]):
    metrics = comp.get("metrics", [])
    names = [m.get("name", "?") for m in metrics]
    rel = []
    for m in metrics:
        try:
            a = float(m.get("a"))
            b = float(m.get("b"))
            rel.append(100.0 * b / a if a else 0.0)
        except (TypeError, ValueError):
            rel.append(0.0)
    fig, ax = plt.subplots(figsize=(7.2, 0.8 + 0.45 * max(1, len(metrics))))
    y = np.arange(len(metrics))
    ax.barh(y, rel, color=THEME["accent"])
    ax.axvline(100, ls="--", color="#555", lw=1.0)
    ax.set_yticks(y)
    ax.set_yticklabels(names, fontsize=8)
    ax.invert_yaxis()
    servers = comp.get("servers", ["A", "B"])
    base = servers[0] if servers else "A"
    other = servers[1] if len(servers) > 1 else "B"
    ax.set_xlabel(f"{other} as % of {base}", fontsize=8)
    for i, v in enumerate(rel):
        ax.text(v + 1, i, f"{v:.0f}%", va="center", fontsize=8)
    fig.tight_layout()
    return fig


def _farr(seq: Optional[Sequence[Any]]) -> np.ndarray:
    """Coerce a series to a float ndarray, mapping None/null/non-numeric to NaN
    (matplotlib draws gaps for NaN rather than crashing)."""
    out = []
    for v in seq or []:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(np.nan)
    return np.array(out, dtype=float)


def build_telemetry_chart(tel: Dict[str, Any]):
    t = _farr(tel.get("t"))
    fig, axes = plt.subplots(4, 1, sharex=True, figsize=(7.4, 8.6))

    axes[0].plot(t, _farr(tel.get("core_mhz")), color=THEME["primary"])
    axes[0].set_ylabel("Core MHz", fontsize=8)

    axes[1].plot(t, _farr(tel.get("wall_w")), color=THEME["action"], label="Wall (BMC)")
    if tel.get("pkg_w"):
        axes[1].plot(t, _farr(tel["pkg_w"]), color=THEME["limited"], label="CPU pkg (RAPL)")
    axes[1].set_ylabel("Watts", fontsize=8)
    axes[1].legend(fontsize=7, loc="upper left")

    axes[2].plot(t, _farr(tel.get("inlet_c")), color=THEME["accent"], label="Inlet")
    if tel.get("outlet_c"):
        axes[2].plot(t, _farr(tel["outlet_c"]), color=THEME["primary"], label="Outlet")
    axes[2].set_ylabel("deg C", fontsize=8)
    axes[2].legend(fontsize=7, loc="upper left")

    axes[3].plot(t, _farr(tel.get("fan_rpm")), color=THEME["muted"])
    axes[3].set_ylabel("Fan RPM", fontsize=8)
    axes[3].set_xlabel("time (s)", fontsize=8)

    for ph in tel.get("phases") or []:
        pt = ph.get("t")
        if pt is None:
            continue
        for ax in axes:
            ax.axvline(pt, color="#444", ls="--", lw=0.8, alpha=0.6)
        axes[0].text(pt, axes[0].get_ylim()[1], " " + str(ph.get("label", "")),
                     rotation=90, va="top", ha="left", fontsize=6.5, color="#444")
    for ax in axes:
        ax.grid(True, ls=":", alpha=0.35)
        ax.tick_params(labelsize=7)
    fig.tight_layout()
    return fig


def build_perf_per_watt_chart(items: Sequence[Dict[str, Any]]):
    names = [f"{i.get('name', '?')} ({i.get('unit', '')})" for i in items]
    vals = [float(i.get("value") or 0) for i in items]
    fig, ax = plt.subplots(figsize=(7.2, 0.7 + 0.5 * max(1, len(items))))
    y = np.arange(len(items))
    ax.barh(y, vals, color=THEME["accent"])
    ax.set_yticks(y)
    ax.set_yticklabels(names, fontsize=8)
    ax.invert_yaxis()
    ax.set_title("Performance per watt", fontsize=9)
    for i, v in enumerate(vals):
        ax.text(v, i, f" {v:g}", va="center", fontsize=8)
    fig.tight_layout()
    return fig


_VERDICT_SCORE = {"PASS": 100, "LIMITED": 70, "ACTION": 40, "N/A": 55}


def _verdict_key(text: Any) -> str:
    t = str(text).strip().upper()
    if "ACTION" in t or "FAIL" in t:
        return "ACTION"
    if "LIMIT" in t or "WARN" in t:
        return "LIMITED"
    if "PASS" in t or "OK" in t:
        return "PASS"
    return "N/A"


def build_health_radar(scorecard: Dict[str, Any]):
    """At-a-glance subsystem-health radar (PASS=100, LIMITED=70, ACTION=40)."""
    rows = scorecard.get("rows", [])
    labels = [str(r[0]).replace(" (", "\n(") for r in rows]
    verdicts = [_verdict_key(r[-1]) for r in rows]
    vals = [_VERDICT_SCORE[v] for v in verdicts]
    n = len(rows)
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
    vc = vals + vals[:1]
    ac = angles + angles[:1]
    fig = plt.figure(figsize=(5.4, 4.4))
    ax = fig.add_subplot(111, polar=True)
    ax.set_theta_offset(np.pi / 2)
    ax.set_theta_direction(-1)
    ax.plot(ac, vc, color=THEME["primary"], lw=2)
    ax.fill(ac, vc, color=THEME["primary"], alpha=0.12)
    for a, v, verd in zip(angles, vals, verdicts):
        ax.plot(a, v, "o", ms=8, color=mpl_pct_color(v if verd != "N/A" else 80))
    ax.set_xticks(angles)
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_yticks([40, 70, 100])
    ax.set_yticklabels(["action", "limited", "pass"], fontsize=6.5, color="#888")
    ax.set_ylim(0, 105)
    ax.set_title("Subsystem health at a glance", fontsize=10, pad=14)
    fig.tight_layout()
    return fig


def build_compute_memory_chart(cpu: Dict[str, Any], memory: Dict[str, Any]):
    """Compute (HPL) and/or memory-bandwidth bars. Panels adapt to available data
    (a memory-only report shows just the memory panel, not an empty HPL one)."""
    has_hpl = memory_pairs = None
    has_hpl = bool(cpu.get("hpl_tflops"))
    candidates = [("STREAM\nCopy", memory.get("stream_copy_gbs")),
                  ("STREAM\nTriad", memory.get("stream_triad_gbs")),
                  ("Per-socket", memory.get("per_socket_gbs")),
                  ("MLC\nPeak", memory.get("mlc_peak_bw_gbs"))]
    mem_pairs = [(lbl, float(v)) for lbl, v in candidates if v not in (None, "n/a")]
    npanels = (1 if has_hpl else 0) + (1 if mem_pairs else 0) or 1
    fig, axes = plt.subplots(1, npanels, figsize=(9.2 if npanels == 2 else 5.2, 3.0))
    if npanels == 1:
        axes = [axes]
    idx = 0
    if has_hpl:
        hpl = float(cpu.get("hpl_tflops") or 0)
        peak = cpu.get("hpl_peak_tflops")
        axes[idx].bar(["HPL Rmax"], [hpl], color=THEME["primary"], width=0.5)
        if peak:
            axes[idx].axhline(float(peak), ls="--", color=THEME["action"], lw=1, label="Rpeak")
            axes[idx].legend(fontsize=7)
        axes[idx].set_title("Compute — HPL (TFLOPS)", fontsize=9)
        axes[idx].grid(axis="y", ls=":", alpha=0.4)
        idx += 1
    if mem_pairs:
        palette = [THEME["primary"], THEME["pass"], THEME["limited"], THEME["accent"]]
        labels = [lbl for lbl, _ in mem_pairs]
        vals = [v for _, v in mem_pairs]
        axes[idx].bar(labels, vals, color=[palette[i % len(palette)] for i in range(len(vals))])
        axes[idx].set_title("Memory bandwidth (GB/s)", fontsize=9)
        axes[idx].grid(axis="y", ls=":", alpha=0.4)
        axes[idx].tick_params(labelsize=7)
        for i, v in enumerate(vals):
            axes[idx].text(i, v, f"{v:.0f}", ha="center", va="bottom", fontsize=7)
    fig.tight_layout()
    return fig


def build_network_chart(net: Dict[str, Any]):
    items = [("TCP fwd", net.get("tcp_rx_gbps"), THEME["primary"]),
             ("TCP rev", net.get("tcp_tx_gbps"), THEME["primary"]),
             ("TCP bidir", net.get("tcp_bidir_gbps"), THEME["primary"]),
             ("TCP multi", net.get("tcp_multistream_gbps"), THEME["primary"]),
             ("RoCE BW", net.get("roce_ib_write_bw_gbps"), THEME["accent"])]
    items = [(n, float(v), c) for n, v, c in items if v not in (None, "n/a")]
    fig, ax = plt.subplots(figsize=(8.6, 3.0))
    ax.bar([i[0] for i in items], [i[1] for i in items], color=[i[2] for i in items])
    ax.set_title("Network throughput (Gb/s)", fontsize=9)
    ax.set_ylabel("Gb/s", fontsize=8)
    ax.grid(axis="y", ls=":", alpha=0.4)
    for i, (_, v, _c) in enumerate(items):
        ax.text(i, v, f"{v:.0f}", ha="center", va="bottom", fontsize=7)
    fig.tight_layout()
    return fig


def build_gpu_compute_chart(gpu: Dict[str, Any]):
    per = gpu.get("per_gpu", [])
    ids = [f"G{g.get('id', i)}" for i, g in enumerate(per)]
    fp8 = [float(g.get("gemm_fp8_tflops") or 0) for g in per]
    hbm = [float(g.get("hbm_bw_gbs") or 0) / 1000.0 for g in per]
    fig, axes = plt.subplots(1, 2, figsize=(9.4, 3.0))
    axes[0].bar(ids, fp8, color=THEME["primary"])
    rated = gpu.get("rated_gemm_fp8_tflops")
    if rated:
        axes[0].axhline(float(rated), ls="--", color=THEME["action"], lw=1, label="datasheet")
        axes[0].legend(fontsize=7)
    axes[0].set_title("Per-GPU GEMM FP8 (TFLOPS)", fontsize=9)
    axes[0].tick_params(labelsize=7)
    axes[0].grid(axis="y", ls=":", alpha=0.4)
    axes[1].bar(ids, hbm, color=THEME["pass"])
    rated_hbm = gpu.get("rated_hbm_bw_gbs")
    if rated_hbm:
        axes[1].axhline(float(rated_hbm) / 1000.0, ls="--", color=THEME["action"], lw=1)
    axes[1].set_title("Per-GPU HBM bandwidth (TB/s)", fontsize=9)
    axes[1].tick_params(labelsize=7)
    axes[1].grid(axis="y", ls=":", alpha=0.4)
    fig.tight_layout()
    return fig


def build_gpu_health_chart(gpu: Dict[str, Any]):
    per = gpu.get("per_gpu", [])
    ids = [f"G{g.get('id', i)}" for i, g in enumerate(per)]
    power = [float(g.get("power_w") or 0) for g in per]
    temp = [float(g.get("temp_c") or 0) for g in per]
    fig, ax = plt.subplots(figsize=(9.0, 3.0))
    ax.bar(ids, power, color=THEME["primary"], label="Power (W)")
    tdp = gpu.get("tdp_w")
    if tdp:
        ax.axhline(float(tdp), ls="--", color=THEME["action"], lw=1, label=f"TDP {tdp} W")
    ax.set_ylabel("Power (W)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.grid(axis="y", ls=":", alpha=0.4)
    ax2 = ax.twinx()
    ax2.plot(ids, temp, color=THEME["accent"], marker="o", lw=1.5, label="Temp (deg C)")
    ax2.set_ylabel("Temp (deg C)", fontsize=8)
    ax2.tick_params(labelsize=7)
    lines = ax.get_legend_handles_labels()[0] + ax2.get_legend_handles_labels()[0]
    labels = ax.get_legend_handles_labels()[1] + ax2.get_legend_handles_labels()[1]
    ax.legend(lines, labels, fontsize=7, loc="lower right")
    ax.set_title("Per-GPU power & temperature under load", fontsize=9)
    fig.tight_layout()
    return fig


def build_sweep_chart(sweeps: Dict[str, Any]):
    qd = sweeps.get("qd", [])
    bs = sweeps.get("bs", [])
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.0))
    if qd:
        axes[0].plot([p.get("qd") for p in qd], [float(p.get("iops") or 0) / 1000 for p in qd],
                     marker="o", color=THEME["primary"])
        axes[0].set_xscale("log", base=2)
        axes[0].set_xlabel("queue depth", fontsize=8)
        axes[0].set_ylabel("kIOPS", fontsize=8)
    axes[0].set_title("4K random read — QD sweep", fontsize=9)
    axes[0].grid(True, ls=":", alpha=0.4)
    if bs:
        axes[1].plot([p.get("bs_kb") for p in bs], [float(p.get("bw_gbs") or 0) for p in bs],
                     marker="s", color=THEME["accent"])
        axes[1].set_xscale("log", base=2)
        axes[1].set_xlabel("block size (KiB)", fontsize=8)
        axes[1].set_ylabel("GB/s", fontsize=8)
    axes[1].set_title("Sequential read — block-size sweep", fontsize=9)
    axes[1].grid(True, ls=":", alpha=0.4)
    fig.tight_layout()
    return fig


def build_nvlink_heatmap(matrix: Sequence[Sequence[float]]):
    """Pairwise GPU NVLink bandwidth (GB/s) as an annotated heatmap."""
    arr = np.array(matrix, dtype=float)
    n = arr.shape[0]
    fig, ax = plt.subplots(figsize=(5.6, 4.8))
    vmax = float(np.nanmax(arr)) if arr.size else 1.0
    im = ax.imshow(arr, cmap="YlGnBu", vmin=0, vmax=vmax)
    ax.set_xticks(range(n))
    ax.set_yticks(range(n))
    ax.set_xticklabels([f"G{i}" for i in range(n)], fontsize=7)
    ax.set_yticklabels([f"G{i}" for i in range(n)], fontsize=7)
    for i in range(n):
        for j in range(n):
            v = arr[i, j]
            if i == j:
                ax.text(j, i, "--", ha="center", va="center", fontsize=6, color="#999")
            else:
                # YlGnBu: high values render dark -> white text; low values light -> dark text
                ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=5.5,
                        color="white" if v >= vmax * 0.5 else "#08306b")
    ax.set_title("NVLink pairwise bandwidth (GB/s)", fontsize=9)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    fig.tight_layout()
    return fig


def build_gpu_timeline_chart(timeline: Dict[str, Any]):
    """Per-GPU power and temperature over the run (one line per GPU)."""
    t = _farr(timeline.get("t"))
    gpus = timeline.get("gpus", [])
    n = len(gpus)
    fig, axes = plt.subplots(2, 1, sharex=True, figsize=(8.8, 5.2))
    cmap = plt.cm.viridis(np.linspace(0, 0.92, max(1, n)))
    for i, g in enumerate(gpus):
        axes[0].plot(t, _farr(g.get("power")), lw=0.9, color=cmap[i], label=f"G{i}")
        axes[1].plot(t, _farr(g.get("temp")), lw=0.9, color=cmap[i])
    axes[0].set_ylabel("Power (W)", fontsize=8)
    axes[0].set_title("Per-GPU power & temperature over the run", fontsize=9)
    axes[1].set_ylabel("Temp (deg C)", fontsize=8)
    axes[1].set_xlabel("time (s)", fontsize=8)
    for ax in axes:
        ax.grid(True, ls=":", alpha=0.35)
        ax.tick_params(labelsize=7)
    if n:
        axes[0].legend(fontsize=6, ncol=min(n, 8), loc="upper left")
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------- #
#  Flowable helpers                                                            #
# --------------------------------------------------------------------------- #
def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["BodyText"]
    return {
        "body": ParagraphStyle("cb_body", parent=base, fontName="Helvetica",
                               fontSize=9, leading=12.5, textColor=INK, spaceAfter=2),
        "small": ParagraphStyle("cb_small", fontName="Helvetica", fontSize=7.5,
                                leading=9.5, textColor=MUTED),
        "h1": ParagraphStyle("cb_h1", fontName="Helvetica-Bold", fontSize=18,
                             leading=22, textColor=PRIMARY, spaceAfter=4),
        "h2": ParagraphStyle("cb_h2", fontName="Helvetica-Bold", fontSize=13,
                             leading=16, textColor=PRIMARY, spaceBefore=8, spaceAfter=5),
        "h3": ParagraphStyle("cb_h3", fontName="Helvetica-Bold", fontSize=10,
                             leading=13, textColor=INK, spaceBefore=6, spaceAfter=3),
        "cover_title": ParagraphStyle("cb_ct", fontName="Helvetica-Bold",
                                      fontSize=25, leading=29, textColor=PRIMARY),
        "cover_sub": ParagraphStyle("cb_cs", fontName="Helvetica", fontSize=12,
                                    leading=16, textColor=MUTED),
        "cell": ParagraphStyle("cb_cell", fontName="Helvetica", fontSize=8,
                               leading=10, textColor=INK),
        "key": ParagraphStyle("cb_key", fontName="Helvetica-Bold", fontSize=8.5,
                              leading=11, textColor=MUTED),
        "val": ParagraphStyle("cb_val", fontName="Helvetica", fontSize=8.5,
                              leading=11, textColor=INK),
    }


S = _styles()


def fig_to_flowable(fig, width: float = CONTENT_W) -> Image:
    """Render a matplotlib Figure to an in-memory PNG and wrap as a reportlab Image."""
    w_in, h_in = fig.get_size_inches()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return Image(buf, width=width, height=width * (h_in / w_in))


def vwidths(n: int, total: float = CONTENT_W) -> List[float]:
    """Column widths for verdict tables: wide first col, fixed 20mm verdict col."""
    if n <= 1:
        return [total]
    last = 20 * mm
    if n == 2:
        return [total - last, last]
    first = 30 * mm
    mid = (total - last - first) / (n - 2)
    return [first] + [mid] * (n - 2) + [last]


def grid_table(header: Optional[Sequence[Any]], rows: Sequence[Sequence[Any]],
               widths: Optional[Sequence[float]] = None, verdict_last: bool = False,
               font: float = 8.0) -> Table:
    data: List[List[Any]] = []
    if header:
        data.append([str(h) for h in header])
    for r in rows:
        rr: List[Any] = []
        for ci, cell in enumerate(r):
            if verdict_last and ci == len(r) - 1:
                rr.append(str(cell))  # plain so TableStyle colors apply
            else:
                rr.append(Paragraph(str(cell), S["cell"]))
        data.append(rr)

    t = Table(data, colWidths=list(widths) if widths else None,
              repeatRows=1 if header else 0)
    st: List[Any] = [
        ("FONTSIZE", (0, 0), (-1, -1), font),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
    ]
    start = 0
    if header:
        st += [
            ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("TOPPADDING", (0, 0), (-1, 0), 5),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ]
        start = 1
    st.append(("ROWBACKGROUNDS", (0, start), (-1, -1), [WHITE, BAND]))
    if verdict_last:
        for ri in range(start, len(data)):
            fill = verdict_fill(data[ri][-1])
            if fill:
                st += [
                    ("BACKGROUND", (-1, ri), (-1, ri), fill),
                    ("TEXTCOLOR", (-1, ri), (-1, ri), WHITE),
                    ("FONTNAME", (-1, ri), (-1, ri), "Helvetica-Bold"),
                    ("ALIGN", (-1, ri), (-1, ri), "CENTER"),
                ]
    t.setStyle(TableStyle(st))
    return t


def kv_table(pairs: Sequence[Tuple[Any, Any]], key_w: float = 42 * mm,
             total_w: float = CONTENT_W) -> Table:
    data = [[Paragraph(str(k), S["key"]), Paragraph(str(v), S["val"])] for k, v in pairs]
    t = Table(data, colWidths=[key_w, total_w - key_w])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, BAND]),
    ]))
    return t


def _bullets(items: Sequence[Any]) -> ListFlowable:
    return ListFlowable(
        [ListItem(Paragraph(str(x), S["body"]), leftIndent=10) for x in items],
        bulletType="bullet", start="•", leftIndent=12,
    )


def _dict_pairs(d: Dict[str, Any]) -> List[Tuple[str, Any]]:
    out = []
    for k, v in d.items():
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        elif isinstance(v, dict):
            v = ", ".join(f"{kk}: {vv}" for kk, vv in v.items())
        out.append((str(k).replace("_", " ").title(), v))
    return out


# --------------------------------------------------------------------------- #
#  Section builders -- each returns a list of flowables (empty if no data).    #
# --------------------------------------------------------------------------- #
def cover_flowables(meta: Dict[str, Any]) -> List[Any]:
    out: List[Any] = [
        Spacer(1, 6 * mm),
        Paragraph(meta.get("report_title", "Server Hardware Validation Report"), S["cover_title"]),
    ]
    if meta.get("subtitle"):
        out.append(Paragraph(meta["subtitle"], S["cover_sub"]))
    out.append(Spacer(1, 9 * mm))
    pairs = [
        ("Server / Host", f"{meta.get('hostname', 'n/a')}  ({meta.get('server_ip', 'n/a')})"),
        ("Platform", meta.get("platform", "n/a")),
        ("Validation Tier", str(meta.get("tier", "n/a")).title()),
        ("Campaign Window", meta.get("window", "n/a")),
        ("Prepared For", meta.get("prepared_for", "n/a")),
        ("Date", meta.get("date", "n/a")),
    ]
    out.append(kv_table(pairs, key_w=46 * mm))
    out.append(Spacer(1, 10 * mm))
    status = str(meta.get("status", "")).strip()
    fill = verdict_fill(status) or ACCENT
    banner = Table(
        [[Paragraph(f"Overall Result:  {status or 'n/a'}",
                    ParagraphStyle("cb_banner", fontName="Helvetica-Bold", fontSize=13,
                                   textColor=WHITE, alignment=TA_CENTER))]],
        colWidths=[CONTENT_W],
    )
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), fill),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    out.append(banner)
    return out


def sec_exec_summary(r: Dict[str, Any]) -> List[Any]:
    es = r.get("executive_summary")
    sc = r.get("scorecard")
    if not es and not (sc and sc.get("rows")):
        return []
    out: List[Any] = [Paragraph("Executive Summary", S["h2"])]
    for p in es or []:
        out.append(Paragraph(p, S["body"]))
        out.append(Spacer(1, 3))
    if sc and sc.get("rows"):
        out.append(Spacer(1, 4))
        out.append(Paragraph("Validation Scorecard", S["h3"]))
        header = sc.get("header")
        out.append(grid_table(header, sc["rows"],
                              widths=vwidths(len(header)) if header else None,
                              verdict_last=True, font=8.5))
        if len(sc["rows"]) >= 3:
            radar = fig_to_flowable(build_health_radar(sc), width=112 * mm)
            radar.hAlign = "CENTER"
            out.append(Spacer(1, 4))
            out.append(radar)
    return out


def sec_hardware(r: Dict[str, Any]) -> List[Any]:
    hw = r.get("hardware")
    if not hw:
        return []
    out: List[Any] = [Paragraph("Hardware Inventory", S["h2"])]

    def block(title: str, key: str) -> None:
        d = hw.get(key)
        if not isinstance(d, dict) or not d:
            return
        out.append(Paragraph(title, S["h3"]))
        out.append(kv_table(_dict_pairs(d)))

    block("System", "system")
    block("CPU", "cpu")
    block("Memory", "memory")
    dimms = hw.get("dimms")
    if dimms:
        out.append(Paragraph("DIMM Population Map (per module)", S["h3"]))
        header = ["#", "Slot", "Size", "MT/s", "Vendor", "Part Number", "Serial Number", "Rank/Width"]
        rows = [[i + 1, g(d, "slot"), g(d, "size"), g(d, "speed"), g(d, "vendor"),
                 g(d, "part"), g(d, "serial"), g(d, "rank_width")]
                for i, d in enumerate(dimms)]
        out.append(grid_table(header, rows,
                              widths=[8 * mm, 24 * mm, 14 * mm, 14 * mm, 20 * mm,
                                      36 * mm, 40 * mm, 18 * mm],
                              font=7))
    block("BIOS / Platform", "bios")
    block("BMC / Power", "bmc")
    block("Kernel / OS", "kernel")

    st = hw.get("storage")
    if st:
        out.append(Paragraph("Storage Devices", S["h3"]))
        header = ["Dev", "Model", "Form", "Capacity", "Serial", "Link (neg)", "Link (cap)"]
        rows = [[g(d, "dev"), g(d, "model"), g(d, "form"), g(d, "capacity"),
                 g(d, "serial"), g(d, "link"), g(d, "link_capable")] for d in st]
        out.append(grid_table(header, rows,
                              widths=[15 * mm, 38 * mm, 15 * mm, 22 * mm, 31 * mm, 27 * mm, 26 * mm],
                              font=7.5))
    nw = hw.get("network")
    if nw:
        out.append(Paragraph("Network Interfaces", S["h3"]))
        header = ["Interface", "Model", "Speed", "Driver"]
        rows = [[g(d, "iface"), g(d, "model"), g(d, "speed"), g(d, "driver")] for d in nw]
        out.append(grid_table(header, rows, font=8))
    return out


def sec_storage(r: Dict[str, Any]) -> List[Any]:
    b = r.get("benchmarks") or {}
    drives = b.get("storage")
    if not drives:
        return []
    out: List[Any] = [Paragraph("Storage Performance", S["h2"])]
    out.append(fig_to_flowable(build_storage_perf_chart(drives)))
    out.append(Spacer(1, 6))
    header = ["Drive", "Link", "SeqR", "SeqW", "RandR", "RandW", "QD1R", "QD1W", "p99.9R"]
    rows = []
    for d in drives:
        rows.append([
            d.get("label") or d.get("dev") or "n/a",
            d.get("link", "n/a"),
            fmt_num(d.get("seq_read_gbs")),
            fmt_num(d.get("seq_write_gbs")),
            fmt_iops(d.get("rand_read_iops")),
            fmt_iops(d.get("rand_write_iops")),
            fmt_num(d.get("qd1_read_us"), 0),
            fmt_num(d.get("qd1_write_us"), 0),
            fmt_num(d.get("p99_9_read_us"), 0),
        ])
    out.append(grid_table(header, rows, font=7.5))
    out.append(Paragraph(
        "Units: SeqR/SeqW in GB/s; RandR/RandW in IOPS; QD1 &amp; p99.9 in microseconds.",
        S["small"]))
    tails = [d for d in drives if d.get("tail")]
    if tails:
        out.append(Spacer(1, 6))
        out.append(Paragraph("Tail Latency (representative drive)", S["h3"]))
        out.append(fig_to_flowable(build_tail_latency_chart(tails[0]["tail"]), width=125 * mm))
    swept = [d for d in drives if d.get("sweeps")]
    if swept:
        out.append(Spacer(1, 6))
        out.append(Paragraph("Queue-depth &amp; block-size sweeps", S["h3"]))
        out.append(fig_to_flowable(build_sweep_chart(swept[0]["sweeps"]), width=165 * mm))
    return out


def sec_spec(r: Dict[str, Any]) -> List[Any]:
    rows = r.get("spec_rows")
    if not rows:
        return []
    enr = []
    for x in rows:
        pct = x.get("pct")
        if pct is None:
            try:
                pct = 100.0 * float(x["measured"]) / float(x["rated"])
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pct = None
        enr.append({**x, "pct": pct})
    chartable = [e for e in enr if e["pct"] is not None]
    out: List[Any] = [
        Paragraph("Specification Compliance", S["h2"]),
        Paragraph(
            "Measured performance as a percentage of datasheet rating. Reference line "
            "at 100%. Bars auto-colored: &#8805;90% green, 75-90% amber, &lt;75% red.",
            S["body"]),
        Spacer(1, 4),
    ]
    if chartable:
        out.append(fig_to_flowable(build_spec_compliance_chart(chartable), width=155 * mm))
    header = ["Metric", "Measured", "Rated", "Unit", "% of rating"]
    trows = [[e.get("metric"), fmt_g(e.get("measured")), fmt_g(e.get("rated")),
              e.get("unit", ""), (f"{e['pct']:.0f}%" if e["pct"] is not None else "n/a")]
             for e in enr]
    out.append(Spacer(1, 6))
    out.append(grid_table(header, trows, font=8))
    return out


def sec_pcie(r: Dict[str, Any]) -> List[Any]:
    b = r.get("benchmarks") or {}
    drives = b.get("storage") or (r.get("hardware") or {}).get("storage") or []
    drives = [d for d in drives if d.get("link_capable_gbs") or d.get("link_negotiated_gbs")]
    if not drives:
        return []
    out: List[Any] = [
        Paragraph("PCIe Link Analysis", S["h2"]),
        Paragraph(
            "Capable vs negotiated link bandwidth vs measured sequential read. Drives "
            "negotiating below their capable width/speed are flagged.", S["body"]),
        fig_to_flowable(build_pcie_chart(drives), width=155 * mm),
    ]
    warns = [d for d in drives if d.get("link_width_warn")]
    if warns:
        msg = "; ".join(f"{d.get('label') or d.get('dev')}: {d.get('link_width_warn')}"
                        for d in warns)
        out.append(Paragraph("<b>Link warnings:</b> " + msg, S["small"]))
    return out


def sec_comparison(r: Dict[str, Any]) -> List[Any]:
    comp = r.get("comparison")
    if not comp or not comp.get("metrics"):
        return []
    servers = comp.get("servers", ["A", "B"])
    out: List[Any] = [Paragraph(comp.get("title", "Comparison"), S["h2"])]
    if comp.get("caption"):
        out.append(Paragraph(comp["caption"], S["body"]))
    out.append(fig_to_flowable(build_comparison_chart(comp), width=155 * mm))
    header = ["Metric", servers[0] if servers else "A",
              servers[1] if len(servers) > 1 else "B", "Delta"]
    rows = []
    for m in comp["metrics"]:
        fmt = m.get("fmt", "{:.1f}")
        try:
            a = float(m.get("a"))
            b = float(m.get("b"))
            av, bv = fmt.format(a), fmt.format(b)
            dv = f"{(b - a) / a * 100:+.0f}%" if a else "n/a"
        except (TypeError, ValueError):
            av, bv, dv = str(m.get("a")), str(m.get("b")), "n/a"
        rows.append([m.get("name"), av, bv, dv])
    out.append(Spacer(1, 6))
    out.append(grid_table(header, rows, font=8))
    if comp.get("notes"):
        out.append(Paragraph(comp["notes"], S["small"]))
    return out


def sec_telemetry(r: Dict[str, Any]) -> List[Any]:
    tel = r.get("telemetry")
    if not tel or not tel.get("t"):
        return []
    out: List[Any] = [
        Paragraph("Power &amp; Thermal Telemetry", S["h2"]),
        Paragraph(
            "Synchronized 1 Hz timeline across the run. Phase markers annotate idle "
            "baseline, load phases and cooldown. Peak wall power and inlet/outlet "
            "temperature occur during the combined-load phase, not in isolation.",
            S["body"]),
        fig_to_flowable(build_telemetry_chart(tel), width=150 * mm),
    ]
    if tel.get("sample") is not None:
        src = "1 Hz (Redfish)" if tel.get("sample") else "coarse (BMC SDR cache)"
        out.append(Paragraph(f"Sampling achieved: {src}.", S["small"]))
    ppw = r.get("perf_per_watt")
    if ppw:
        out.append(Spacer(1, 8))
        out.append(Paragraph("Efficiency -- Performance per Watt", S["h3"]))
        out.append(fig_to_flowable(build_perf_per_watt_chart(ppw), width=140 * mm))
    return out


def sec_health(r: Dict[str, Any]) -> List[Any]:
    out: List[Any] = []
    h = r.get("health")
    if h and h.get("rows"):
        out.append(Paragraph("Component Health", S["h2"]))
        header = h.get("header")
        out.append(grid_table(header, h["rows"],
                              widths=vwidths(len(header)) if header else None,
                              verdict_last=True, font=7.5))
    sel = r.get("sel_status")
    dm = r.get("dmesg_status")
    if sel or dm:
        if not out:
            out.append(Paragraph("Component Health", S["h2"]))
        out.append(Spacer(1, 4))
        if sel:
            out.append(Paragraph("<b>BMC SEL (before &#8594; after):</b> " + str(sel), S["body"]))
        if dm:
            out.append(Paragraph("<b>Kernel log (dmesg) diff:</b> " + str(dm), S["body"]))
    return out


def sec_findings(r: Dict[str, Any]) -> List[Any]:
    f = r.get("findings")
    rec = r.get("recommendations")
    conc = r.get("conclusion")
    if not (f or rec or (conc and conc.get("rows"))):
        return []
    out: List[Any] = [Paragraph("Findings &amp; Recommendations", S["h2"])]
    if f:
        out.append(Paragraph("Findings", S["h3"]))
        out.append(_bullets(f))
    if rec:
        out.append(Paragraph("Recommendations", S["h3"]))
        out.append(_bullets(rec))
    if conc and conc.get("rows"):
        out.append(Spacer(1, 6))
        out.append(Paragraph("Conclusion", S["h3"]))
        header = conc.get("header")
        out.append(grid_table(header, conc["rows"],
                              widths=vwidths(len(header)) if header else None,
                              verdict_last=True, font=8))
    return out


def sec_repro(r: Dict[str, Any]) -> List[Any]:
    rep = r.get("reproducibility")
    if not rep:
        return []
    out: List[Any] = [
        Paragraph("Reproducibility Appendix", S["h2"]),
        Paragraph(
            "Exact tooling, kernel command line, BIOS state and a SHA-256 of the raw "
            "data bundle, so this run can be audited and reproduced.", S["body"]),
        kv_table(_dict_pairs(rep), key_w=50 * mm),
    ]
    return out


def sec_compute_memory(r: Dict[str, Any]) -> List[Any]:
    b = r.get("benchmarks") or {}
    cpu = b.get("cpu") or {}
    mem = b.get("memory") or {}
    if not cpu and not mem:
        return []
    title = "Compute &amp; Memory" if cpu.get("hpl_tflops") else ("Memory" if mem else "Compute")
    chart_w = 165 * mm if cpu.get("hpl_tflops") else 120 * mm
    chart = fig_to_flowable(build_compute_memory_chart(cpu, mem), width=chart_w)
    chart.hAlign = "LEFT"
    out: List[Any] = [Paragraph(title, S["h2"]), chart]
    pairs = []
    for label, val in [
        ("HPL Rmax (TFLOPS)", cpu.get("hpl_tflops")),
        ("HPL efficiency (%)", cpu.get("hpl_efficiency_pct")),
        ("AVX-512 / AMX", f"{cpu.get('avx512', 'n/a')} / {cpu.get('amx', 'n/a')}"),
        ("Soak (h) / throttle events", f"{cpu.get('soak_hours', 'n/a')} / {cpu.get('throttle_events', 'n/a')}"),
        ("STREAM Triad (GB/s)", mem.get("stream_triad_gbs")),
        ("STREAM Copy (GB/s)", mem.get("stream_copy_gbs")),
        ("MLC peak BW (GB/s)", mem.get("mlc_peak_bw_gbs")),
        ("MLC loaded latency (ns)", mem.get("mlc_loaded_latency_ns")),
        ("DIMM speed (MT/s)", f"{mem.get('dimm_speed_mts', 'n/a')} (rated {mem.get('rated_mts', 'n/a')})"),
    ]:
        if val not in (None, "n/a", "n/a / n/a", "n/a (rated n/a)"):
            pairs.append((label, val))
    if pairs:
        out.append(Spacer(1, 6))
        out.append(kv_table(pairs, key_w=60 * mm))
    return out


def sec_gpu(r: Dict[str, Any]) -> List[Any]:
    b = r.get("benchmarks") or {}
    gpu = b.get("gpu")
    if not gpu or not gpu.get("per_gpu"):
        return []
    n = gpu.get("count") or len(gpu["per_gpu"])
    out: List[Any] = [
        Paragraph("GPU / AI Accelerators", S["h2"]),
        Paragraph(
            f"{n} &#215; {gpu.get('model', 'GPU')} -- per-GPU GEMM throughput, HBM bandwidth, "
            "NVLink fabric, and power/thermals under sustained AI load.", S["body"]),
        fig_to_flowable(build_gpu_compute_chart(gpu), width=170 * mm),
        Spacer(1, 6),
        fig_to_flowable(build_gpu_health_chart(gpu), width=165 * mm),
    ]
    if gpu.get("nvlink_matrix"):
        out.append(Spacer(1, 6))
        hm = fig_to_flowable(build_nvlink_heatmap(gpu["nvlink_matrix"]), width=118 * mm)
        hm.hAlign = "CENTER"
        out.append(hm)
    if gpu.get("timeline"):
        out.append(Spacer(1, 6))
        out.append(fig_to_flowable(build_gpu_timeline_chart(gpu["timeline"]), width=165 * mm))
    header = ["GPU", "GEMM FP8", "GEMM BF16", "HBM BW", "NVLink", "Power", "Temp", "ECC"]
    rows = []
    for i, g in enumerate(gpu["per_gpu"]):
        rows.append([
            f"G{g.get('id', i)}",
            fmt_num(g.get("gemm_fp8_tflops"), 0), fmt_num(g.get("gemm_bf16_tflops"), 0),
            f"{fmt_num((g.get('hbm_bw_gbs') or 0) / 1000, 1)} TB/s",
            f"{fmt_num((g.get('nvlink_bw_gbs') or 0) / 1000, 2)} TB/s",
            f"{fmt_num(g.get('power_w'), 0)} W", f"{fmt_num(g.get('temp_c'), 0)} C",
            str(g.get("ecc_errors", "0")),
        ])
    out.append(Spacer(1, 6))
    out.append(grid_table(header, rows, font=7.5))
    out.append(Paragraph("Units: GEMM in TFLOPS; HBM/NVLink bandwidth measured per GPU.", S["small"]))
    extras = []
    if gpu.get("nccl_allreduce_gbps"):
        extras.append(f"NCCL all-reduce ({n}-GPU): {gpu['nccl_allreduce_gbps']} GB/s bus bandwidth.")
    if gpu.get("hbm_total_tb"):
        extras.append(f"Aggregate HBM: {gpu['hbm_total_tb']} TB; driver {gpu.get('driver', 'n/a')}, CUDA {gpu.get('cuda', 'n/a')}.")
    if gpu.get("dcgm_diag"):
        extras.append(f"DCGM diagnostics: {gpu['dcgm_diag']}.")
    if extras:
        out.append(Spacer(1, 3))
        out.append(_bullets(extras))
    return out


def sec_network(r: Dict[str, Any]) -> List[Any]:
    b = r.get("benchmarks") or {}
    net = b.get("network")
    keys = ("tcp_rx_gbps", "tcp_tx_gbps", "tcp_bidir_gbps", "tcp_multistream_gbps", "roce_ib_write_bw_gbps")
    if not net or all(net.get(k) in (None, "n/a") for k in keys):
        return []
    out: List[Any] = [Paragraph("Network", S["h2"]),
                      fig_to_flowable(build_network_chart(net), width=160 * mm)]
    pairs = []
    for label, val in [
        ("TCP RX / TX (Gb/s)", f"{net.get('tcp_rx_gbps', 'n/a')} / {net.get('tcp_tx_gbps', 'n/a')}"),
        ("TCP bidirectional (Gb/s)", net.get("tcp_bidir_gbps")),
        ("RoCE ib_write_bw (Gb/s)", net.get("roce_ib_write_bw_gbps")),
        ("RoCE p99.9 latency (us)", net.get("roce_ib_write_lat_p99_9_us")),
        ("MTU / PFC / ECN", f"{net.get('mtu', 'n/a')} / {net.get('pfc', 'n/a')} / {net.get('ecn', 'n/a')}"),
        ("RDMA link", net.get("rdma_link")),
        ("Peer / fabric", f"{net.get('peer', 'n/a')} / {net.get('fabric', 'n/a')}"),
    ]:
        if val not in (None, "n/a", "n/a / n/a", "n/a / n/a / n/a"):
            pairs.append((label, val))
    if pairs:
        out.append(Spacer(1, 6))
        out.append(kv_table(pairs, key_w=60 * mm))
    return out


def sec_deployment(r: Dict[str, Any]) -> List[Any]:
    meta = r.get("meta") or {}
    rack = meta.get("rack")
    if not rack or _diagrams is None:
        return []
    occ = rack.get("occupants")
    if not occ and rack.get("position"):
        occ = [(rack["position"][0], rack["position"][1], meta.get("hostname", "server"))]
    try:
        draw = _diagrams.rack_elevation(total_u=rack.get("total_u", 42), occupants=occ or [],
                                        width=70 * mm, u_h=2.6 * mm,
                                        title=f"Rack {rack.get('name', '')}".strip())
    except Exception:
        return []
    draw.hAlign = "LEFT"
    return [
        Paragraph("Deployment &amp; Rack Position", S["h2"]),
        Paragraph("Physical placement of the system under test. Full rack-mount, rail-kit and "
                  "mainboard service procedures are produced as a separate branded install guide.",
                  S["body"]),
        Spacer(1, 4),
        draw,
    ]


# --------------------------------------------------------------------------- #
#  Page furniture                                                              #
# --------------------------------------------------------------------------- #
_LOGO_CACHE: Dict[str, Any] = {}


def _logo_reader(path: str):
    if path not in _LOGO_CACHE:
        _LOGO_CACHE[path] = ImageReader(path) if os.path.isfile(path) else None
    return _LOGO_CACHE[path]


def _draw_logo(canvas, path: str, x: float, y: float, h: float, align: str = "left") -> float:
    """Draw a logo at height ``h`` (aspect preserved). Returns drawn width (0 if missing)."""
    ir = _logo_reader(path)
    if ir is None:
        return 0.0
    iw, ih = ir.getSize()
    w = h * iw / ih
    if align == "right":
        x -= w
    canvas.drawImage(ir, x, y, width=w, height=h, mask="auto", preserveAspectRatio=True)
    return w


def _footer(canvas, doc, meta: Dict[str, Any]) -> None:
    # navy footer bar + red stripe (house style); no classification, no person name
    canvas.setFillColor(PRIMARY)
    canvas.rect(0, 0, PAGE_W, FOOTER_H, fill=1, stroke=0)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, FOOTER_H, PAGE_W, 1.2 * mm, fill=1, stroke=0)
    canvas.setFillColor(WHITE)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(MARGIN, 4 * mm, meta.get("company") or THEME["company"])
    canvas.drawCentredString(PAGE_W / 2, 4 * mm, f"Page {doc.page}")
    canvas.drawRightString(PAGE_W - MARGIN, 4 * mm, str(meta.get("date", "")))


def _draw_body(canvas, doc, meta: Dict[str, Any]) -> None:
    canvas.saveState()
    canvas.setFillColor(PRIMARY)
    canvas.rect(0, PAGE_H - TOP_BAR, PAGE_W, TOP_BAR, fill=1, stroke=0)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, PAGE_H - TOP_BAR - 1.4 * mm, PAGE_W, 1.4 * mm, fill=1, stroke=0)
    drew = _draw_logo(canvas, LOGO_NETWEB, MARGIN,
                      PAGE_H - TOP_BAR + (TOP_BAR - 8 * mm) / 2, 8 * mm, "left")
    if not drew:
        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", 12)
        canvas.drawString(MARGIN, PAGE_H - 12 * mm, meta.get("company", THEME["company"]))
    _footer(canvas, doc, meta)
    canvas.restoreState()


def _draw_cover(canvas, doc, meta: Dict[str, Any]) -> None:
    canvas.saveState()
    canvas.setFillColor(PRIMARY)
    canvas.rect(0, PAGE_H - HERO_H, PAGE_W, HERO_H, fill=1, stroke=0)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, PAGE_H - HERO_H, PAGE_W, 2.4 * mm, fill=1, stroke=0)
    # both white logos: Netweb (left) + Tyrone (right), vertically centred in the hero
    logo_h = 16 * mm
    cy = PAGE_H - HERO_H + (HERO_H - logo_h) / 2 + 4 * mm
    drew = _draw_logo(canvas, LOGO_NETWEB, MARGIN, cy, logo_h, "left")
    _draw_logo(canvas, LOGO_TYRONE, PAGE_W - MARGIN, cy + 1 * mm, logo_h * 0.82, "right")
    if not drew:
        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", 28)
        canvas.drawString(MARGIN, cy + 3 * mm, meta.get("company", THEME["company"]))
    canvas.setFillColor(GOLD)
    canvas.setFont("Helvetica-Oblique", 11)
    canvas.drawCentredString(PAGE_W / 2, PAGE_H - HERO_H + 7 * mm, THEME["tagline"])
    _footer(canvas, doc, meta)
    canvas.restoreState()


def sec_extra_tables(r: Dict[str, Any]) -> List[Any]:
    """Generic data-driven tables: [{title, caption?, header, rows, widths_mm?, verdict_last?, font?}]."""
    tables = r.get("extra_tables")
    if not tables:
        return []
    out: List[Any] = []
    for t in tables:
        if not t.get("rows"):
            continue
        out.append(Paragraph(str(t.get("title", "Table")), S["h2"]))
        if t.get("caption"):
            out.append(Paragraph(str(t["caption"]), S["body"]))
        widths = [float(w) * mm for w in t["widths_mm"]] if t.get("widths_mm") else None
        out.append(grid_table(t.get("header"), t["rows"], widths=widths,
                              verdict_last=bool(t.get("verdict_last")),
                              font=float(t.get("font", 7.5))))
        out.append(Spacer(1, 8))
    return out


SECTIONS = (
    sec_exec_summary,
    sec_hardware,
    sec_compute_memory,
    sec_gpu,
    sec_storage,
    sec_network,
    sec_spec,
    sec_pcie,
    sec_comparison,
    sec_extra_tables,
    sec_telemetry,
    sec_health,
    sec_findings,
    sec_deployment,
    sec_repro,
)


def build_report(results: Dict[str, Any], out_path: str) -> str:
    """Render ``results`` (a results.json dict) to a branded PDF at ``out_path``."""
    meta = results.get("meta") or {}
    cover_frame = Frame(MARGIN, 16 * mm, CONTENT_W, PAGE_H - HERO_H - 22 * mm, id="cover")
    body_frame = Frame(MARGIN, 16 * mm, CONTENT_W, PAGE_H - TOP_BAR - 22 * mm, id="body")
    doc = BaseDocTemplate(
        out_path, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=TOP_BAR + 6 * mm, bottomMargin=16 * mm,
        title=meta.get("report_title", "CoreBench Report"),
        author=meta.get("prepared_by", "CoreBench"),
    )
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[cover_frame],
                     onPage=lambda c, d: _draw_cover(c, d, meta)),
        PageTemplate(id="body", frames=[body_frame],
                     onPage=lambda c, d: _draw_body(c, d, meta)),
    ])
    story: List[Any] = list(cover_flowables(meta))
    story.append(NextPageTemplate("body"))
    story.append(PageBreak())
    for sec in SECTIONS:
        flow = sec(results)
        if flow:
            story += flow
            story.append(Spacer(1, 8))
    doc.build(story)
    return out_path


def render(results_path: str, out_path: str) -> str:
    with open(results_path, "r", encoding="utf-8") as fh:
        results = json.load(fh)
    return build_report(results, out_path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if len(argv) < 2:
        print("usage: generate_report.py <results.json> <out.pdf>", file=sys.stderr)
        return 2
    out = render(argv[0], argv[1])
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
