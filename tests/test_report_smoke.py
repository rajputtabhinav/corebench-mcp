"""Fixtures-based PDF smoke test: render the sample results and assert a valid,
multi-page PDF -- no hardware, CI-friendly."""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

import generate_report  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_results.json")


def _page_count(pdf_bytes: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf_bytes))


def test_sample_fixture_exists():
    assert os.path.isfile(FIXTURE), "run: python tests/fixtures/make_sample.py"


def test_render_sample_pdf(tmp_path):
    out = str(tmp_path / "report.pdf")
    generate_report.render(FIXTURE, out)
    assert os.path.isfile(out)
    data = open(out, "rb").read()
    assert data[:5] == b"%PDF-", "output is not a PDF"
    assert len(data) > 20000, "PDF suspiciously small -- charts likely missing"
    assert _page_count(data) >= 5, "expected a multi-page report"


def test_chart_builders_are_pure():
    """Chart builders must work on plain data without a results file or hardware."""
    import json
    with open(FIXTURE, encoding="utf-8") as fh:
        r = json.load(fh)
    # each returns a matplotlib Figure with at least one axes
    fig = generate_report.build_spec_compliance_chart(
        [{"metric": "x", "pct": 88.0}, {"metric": "y", "pct": 50.0}])
    assert fig.axes
    fig2 = generate_report.build_telemetry_chart(r["telemetry"])
    assert len(fig2.axes) == 4
    # new visualizations
    assert generate_report.build_health_radar(r["scorecard"]).axes
    gpu = r["benchmarks"]["gpu"]
    assert generate_report.build_gpu_compute_chart(gpu).axes
    assert generate_report.build_gpu_health_chart(gpu).axes
    assert generate_report.build_nvlink_heatmap(gpu["nvlink_matrix"]).axes
    assert generate_report.build_gpu_timeline_chart(gpu["timeline"]).axes
    assert generate_report.build_compute_memory_chart(
        r["benchmarks"]["cpu"], r["benchmarks"]["memory"]).axes
    assert generate_report.build_network_chart(r["benchmarks"]["network"]).axes
    sweeps = next(d for d in r["benchmarks"]["storage"] if d.get("sweeps"))["sweeps"]
    assert generate_report.build_sweep_chart(sweeps).axes
    import matplotlib.pyplot as plt
    plt.close("all")


def test_missing_sections_skip_gracefully(tmp_path):
    """A minimal results dict (meta only) still renders a valid PDF."""
    out = str(tmp_path / "min.pdf")
    generate_report.build_report({"meta": {"report_title": "Tiny", "status": "PASS"}}, out)
    data = open(out, "rb").read()
    assert data[:5] == b"%PDF-"
