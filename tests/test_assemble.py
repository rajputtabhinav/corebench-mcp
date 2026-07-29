"""Unit tests for the auto-analysis core in engine/assemble.py (no hardware needed)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

import assemble  # noqa: E402


# --------------------------------------------------------------------------- #
#  Fixtures: inline fragments + config (mirrors config.json spec_targets).     #
# --------------------------------------------------------------------------- #
FRAGMENTS = {
    "hardware": {
        "system": {"vendor": "Supermicro", "model": "AS-2125HS-TNR", "hostname": "node-test"},
        "cpu": {"model": "2x EPYC 9554"},
        "kernel": {"cmdline": "ro quiet pcie_aspm=off"},
    },
    "storage": [
        {"label": "nvme0", "seq_read_gbs": 6.7, "seq_write_gbs": 3.85,
         "rand_read_iops": 1270000, "rand_write_iops": 300000, "link": "Gen5 x4"},
        {"label": "nvme3", "rand_write_iops": 190000, "link": "Gen5 x2",
         "link_width_warn": "negotiated x2, capable x4"},
    ],
    "cpu": {"hpl_tflops": 3.42, "throttle_events": 0},
    "memory": {"stream_triad_gbs": 712},
    "network": {"tcp_rx_gbps": 188},
    "telemetry": {"wall_w": [400, 1140, 500], "inlet_c": [22, 24, 23], "outlet_c": [30, 47, 33]},
    "sel_before": ["PSU1 Presence detected", "Fan1 OK"],
    "sel_after": ["PSU1 Presence detected", "Fan1 OK", "CPU1 Thermal Trip - Assertion"],
    "dmesg_before": ["systemd[1]: Started."],
    "dmesg_after": ["systemd[1]: Started.",
                    "nvme nvme3: I/O 12 QID 4 timeout, reset controller"],
}

CONFIG = {
    "meta": {"company": "CoreBench", "hostname": "auto", "platform": "auto", "status": "auto"},
    "auto_scorecard": True,
    "thresholds": {"limited": 90, "action": 75},
    "spec_targets": [
        {"metric": "Seq Read (nvme0)", "rated": 6.8, "unit": "GB/s", "subsystem": "Storage",
         "from": {"fragment": "storage", "match": {"label": "nvme0"}, "field": "seq_read_gbs"}},
        {"metric": "Seq Write (nvme0)", "rated": 4.0, "unit": "GB/s", "subsystem": "Storage",
         "from": {"fragment": "storage", "match": {"label": "nvme0"}, "field": "seq_write_gbs"}},
        {"metric": "4K Rand Read (nvme0)", "rated": 1400000, "unit": "IOPS", "subsystem": "Storage",
         "from": {"fragment": "storage", "match": {"label": "nvme0"}, "field": "rand_read_iops"}},
        {"metric": "4K Rand Write (nvme0)", "rated": 360000, "unit": "IOPS", "subsystem": "Storage",
         "from": {"fragment": "storage", "match": {"label": "nvme0"}, "field": "rand_write_iops"}},
        {"metric": "4K Rand Write (nvme3)", "rated": 360000, "unit": "IOPS", "subsystem": "Storage",
         "from": {"fragment": "storage", "match": {"label": "nvme3"}, "field": "rand_write_iops"}},
        {"metric": "HPL throughput", "rated": 3.7, "unit": "TFLOPS", "subsystem": "Compute",
         "from": {"fragment": "cpu", "field": "hpl_tflops"}},
        {"metric": "STREAM Triad", "rated": 760, "unit": "GB/s", "subsystem": "Memory",
         "from": {"fragment": "memory", "field": "stream_triad_gbs"}},
        {"metric": "TCP RX throughput", "rated": 200, "unit": "Gb/s", "subsystem": "Network",
         "from": {"fragment": "network", "field": "tcp_rx_gbps"}},
    ],
}


def _row(rows, metric):
    return next(r for r in rows if r["metric"] == metric)


# --------------------------------------------------------------------------- #
#  verdict_for_pct / worst_verdict                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pct,expected", [
    (130, "PASS"), (100, "PASS"), (90.0, "PASS"),
    (89.9, "LIMITED"), (83, "LIMITED"), (75.0, "LIMITED"),
    (74.9, "ACTION"), (53, "ACTION"), (0, "ACTION"),
    (None, "N/A"),
])
def test_verdict_for_pct(pct, expected):
    assert assemble.verdict_for_pct(pct) == expected


def test_verdict_custom_thresholds():
    assert assemble.verdict_for_pct(85, limited=80, action=70) == "PASS"
    assert assemble.verdict_for_pct(72, limited=80, action=70) == "LIMITED"


def test_worst_verdict():
    assert assemble.worst_verdict(["PASS", "LIMITED", "PASS"]) == "LIMITED"
    assert assemble.worst_verdict(["PASS", "ACTION", "LIMITED"]) == "ACTION"
    assert assemble.worst_verdict(["PASS", "PASS"]) == "PASS"
    assert assemble.worst_verdict(["N/A"]) == "N/A"
    assert assemble.worst_verdict([]) == "N/A"


# --------------------------------------------------------------------------- #
#  resolve_measured / compute_spec_rows                                        #
# --------------------------------------------------------------------------- #
def test_resolve_measured_list_match():
    val = assemble.resolve_measured(
        FRAGMENTS, {"fragment": "storage", "match": {"label": "nvme0"}, "field": "seq_read_gbs"})
    assert val == 6.7


def test_resolve_measured_dict():
    val = assemble.resolve_measured(FRAGMENTS, {"fragment": "memory", "field": "stream_triad_gbs"})
    assert val == 712


def test_resolve_measured_missing():
    assert assemble.resolve_measured(FRAGMENTS, {"fragment": "nope", "field": "x"}) is None
    assert assemble.resolve_measured(
        FRAGMENTS, {"fragment": "storage", "match": {"label": "ghost"}, "field": "x"}) is None


def test_compute_spec_rows_pct_and_verdict():
    rows = assemble.compute_spec_rows(FRAGMENTS, CONFIG["spec_targets"])
    assert _row(rows, "Seq Read (nvme0)")["verdict"] == "PASS"
    assert _row(rows, "Seq Read (nvme0)")["pct"] == pytest.approx(98.5, abs=0.2)
    # 300000 / 360000 = 83.3% -> LIMITED (<90)
    assert _row(rows, "4K Rand Write (nvme0)")["verdict"] == "LIMITED"
    assert 82 < _row(rows, "4K Rand Write (nvme0)")["pct"] < 84
    # 190000 / 360000 = 52.8% -> ACTION (<75)
    assert _row(rows, "4K Rand Write (nvme3)")["verdict"] == "ACTION"
    assert _row(rows, "4K Rand Write (nvme3)")["pct"] < 75


# --------------------------------------------------------------------------- #
#  scorecard / status                                                          #
# --------------------------------------------------------------------------- #
def test_build_scorecard_worst_per_subsystem():
    rows = assemble.compute_spec_rows(FRAGMENTS, CONFIG["spec_targets"])
    sc = assemble.build_scorecard(rows)
    by_sub = {r[0]: r[-1] for r in sc["rows"]}
    assert by_sub["Storage"] == "ACTION"   # dragged down by nvme3
    assert by_sub["Compute"] == "PASS"
    assert by_sub["Memory"] == "PASS"
    assert by_sub["Network"] == "PASS"


def test_status_from_scorecard():
    sc_pass = {"rows": [["A", "", "", "PASS"], ["B", "", "", "PASS"]]}
    sc_lim = {"rows": [["A", "", "", "PASS"], ["B", "", "", "LIMITED"]]}
    sc_act = {"rows": [["A", "", "", "LIMITED"], ["B", "", "", "ACTION"]]}
    assert assemble.status_from_scorecard(sc_pass) == "PASS"
    assert assemble.status_from_scorecard(sc_lim) == "PASS WITH LIMITATIONS"
    assert assemble.status_from_scorecard(sc_act) == "ACTION REQUIRED"
    assert assemble.status_from_scorecard(None) == "N/A"


# --------------------------------------------------------------------------- #
#  findings: sub-threshold + SEL/dmesg diffs                                   #
# --------------------------------------------------------------------------- #
def test_candidate_findings_flags_subthreshold():
    rows = assemble.compute_spec_rows(FRAGMENTS, CONFIG["spec_targets"])
    finds = assemble.candidate_findings(rows)
    # nvme0 rand write (LIMITED) + nvme3 rand write (ACTION) = 2; PASS metrics excluded
    assert len(finds) == 2
    assert any("ACTION" in f and "nvme3" in f for f in finds)


def test_diff_sel_flags_thermal():
    status, finds = assemble.diff_sel(FRAGMENTS["sel_before"], FRAGMENTS["sel_after"])
    assert len(finds) == 1
    assert "Thermal Trip" in finds[0]
    assert "1 flagged" in status


def test_diff_dmesg_flags_nvme_reset():
    status, finds = assemble.diff_dmesg(FRAGMENTS["dmesg_before"], FRAGMENTS["dmesg_after"])
    assert len(finds) == 1
    assert "nvme3" in finds[0]


def test_diff_clean_when_no_new_events():
    status, finds = assemble.diff_dmesg(["a", "b"], ["a", "b"])
    assert finds == []
    assert "No new" in status


# --------------------------------------------------------------------------- #
#  assemble() end-to-end                                                       #
# --------------------------------------------------------------------------- #
def test_assemble_end_to_end():
    res = assemble.assemble(FRAGMENTS, CONFIG,
                            run_meta={"tier": "qualification", "date": "2026-06-05"})
    # spec rows present and trimmed to contract keys
    assert {r["metric"] for r in res["spec_rows"]} >= {"Seq Read (nvme0)", "STREAM Triad"}
    assert set(res["spec_rows"][0].keys()) == {"metric", "measured", "rated", "unit", "pct"}
    # scorecard includes auto Power & Thermal row, overall ACTION REQUIRED
    subs = {r[0] for r in res["scorecard"]["rows"]}
    assert "Power & Thermal" in subs
    assert res["meta"]["status"] == "ACTION REQUIRED"
    # meta auto-fill from hardware
    assert res["meta"]["hostname"] == "node-test"
    assert "Supermicro" in res["meta"]["platform"]
    assert res["meta"]["tier"] == "qualification"
    # auto health table from storage drives; nvme3 flagged ACTION
    health_verdicts = [r[-1] for r in res["health"]["rows"]]
    assert "ACTION" in health_verdicts
    # findings include sub-threshold + thermal SEL + nvme reset
    joined = " ".join(res["findings"])
    assert "nvme3" in joined and "Thermal Trip" in joined


def test_assemble_respects_explicit_narrative():
    cfg = dict(CONFIG, executive_summary=["Custom summary."],
               recommendations=["Do the thing."])
    res = assemble.assemble(FRAGMENTS, cfg)
    assert res["executive_summary"] == ["Custom summary."]
    assert res["recommendations"] == ["Do the thing."]
