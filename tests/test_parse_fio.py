"""Unit tests for benchmarks/parse_fio.py against a real fio JSON fixture."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "benchmarks"))

import parse_fio  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "fio_randread.json")


def test_bw_iops_from_fixture():
    doc = parse_fio.load(FIX)
    rd = parse_fio.first_job(doc)["read"]
    assert parse_fio.bw_gbs(rd) == 6.7          # bw_bytes 6.7e9 -> 6.7 GB/s
    assert parse_fio.iops(rd) == 1270000


def test_bw_falls_back_to_kib():
    # no bw_bytes -> bw is KiB/s; 1,000,000 KiB/s * 1024 / 1e9 = 1.024 GB/s
    assert parse_fio.bw_gbs({"bw": 1000000}) == 1.024
    assert parse_fio.bw_gbs({}) is None


def test_percentiles_ns_to_us():
    doc = parse_fio.load(FIX)
    rd = parse_fio.first_job(doc)["read"]
    p = parse_fio.percentiles_us(rd)
    assert p["p99"] == 98.0
    assert p["p99_9"] == 142.0
    assert p["p99_99"] == 210.0
    assert p["p99_999"] == 480.0
    assert p["max"] == 1100.0


def test_build_drive_merges_workloads(tmp_path):
    rd = str(tmp_path)
    doc = json.load(open(FIX, encoding="utf-8"))
    # seqread: reuse fixture's read block as a "seq" job (bw 6.7 GB/s)
    json.dump(doc, open(os.path.join(rd, "fio.nvme0.seqread.json"), "w"))
    json.dump(doc, open(os.path.join(rd, "fio.nvme0.randread.json"), "w"))
    # a write workload (FOB) then a steady-state write that must override headline
    fob = {"jobs": [{"write": {"bw_bytes": 0, "iops": 300000.0,
                               "clat_ns": {"max": 900000, "mean": 13000,
                                           "percentile": {"99.900000": 41000}}}}]}
    steady = {"jobs": [{"write": {"iops": 190000.0,
                                  "clat_ns": {"mean": 18000, "percentile": {}}}}]}
    json.dump(fob, open(os.path.join(rd, "fio.nvme0.randwrite.json"), "w"))
    json.dump(steady, open(os.path.join(rd, "fio.nvme0.steady.json"), "w"))

    meta = {"label": "nvme0", "dev": "/dev/nvme0n1", "model": "PM9D3a", "link": "Gen5 x4"}
    d = parse_fio.build_drive(meta, rd)

    assert d["model"] == "PM9D3a"               # inventory preserved
    assert d["seq_read_gbs"] == 6.7
    assert d["rand_read_iops"] == 1270000
    assert d["p99_9_read_us"] == 142.0
    assert d["fob_rand_write_iops"] == 300000
    assert d["steady_rand_write_iops"] == 190000
    assert d["rand_write_iops"] == 190000       # steady is the headline, not FOB
    assert d["tail"]["read"]["max"] == 1100.0
    assert d["tail"]["write"]["p99_9"] == 41.0


def test_build_all_reads_meta(tmp_path):
    rd = str(tmp_path)
    json.dump([{"label": "nvme0", "dev": "/dev/nvme0n1"}],
              open(os.path.join(rd, "storage_meta.json"), "w"))
    json.dump(json.load(open(FIX, encoding="utf-8")),
              open(os.path.join(rd, "fio.nvme0.randread.json"), "w"))
    drives = parse_fio.build_all(rd)
    assert len(drives) == 1
    assert drives[0]["rand_read_iops"] == 1270000
