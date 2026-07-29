#!/usr/bin/env python3
"""
Generate ``tests/fixtures/sample_results.json`` -- a complete, realistic
results.json that exercises every section of the report engine.

The showcase is a Tyrone Camarero AI node (8x NVIDIA B200, HGX) so the GPU/AI,
Compute & Memory, Network, telemetry, sweep and radar visuals all populate.
Sections are dynamic: a storage-only server simply omits the GPU section, etc.

Run:  python tests/fixtures/make_sample.py   (fixed RNG seed -> reproducible)
"""

import hashlib
import json
import os
import random

random.seed(7)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sample_results.json")


def _ramp(t, t0, t1, v0, v1):
    if t <= t0:
        return v0
    if t >= t1:
        return v1
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def build_telemetry():
    # AI server: idle -> storage -> GPU combined max-load (peak ~12.5 kW) -> cooldown
    phases = [
        {"t": 0, "label": "idle baseline"},
        {"t": 20, "label": "storage load"},
        {"t": 50, "label": "AI combined load"},
        {"t": 130, "label": "cooldown"},
    ]
    t = list(range(0, 161))
    wall, pkg, inlet, outlet, fan, mhz = [], [], [], [], [], []
    for s in t:
        if s < 20:
            w, p, ic, oc, f, m = 2500, 240, 23.0, 31.0, 7000, 2600
        elif s < 50:
            w, p, ic, oc, f, m = 4200, 360, 24.0, _ramp(s, 20, 50, 34, 40), \
                _ramp(s, 20, 50, 9000, 11000), 3000
        elif s < 130:                      # GPUs + CPU + storage together
            w = _ramp(s, 50, 130, 9500, 12600)
            p = _ramp(s, 50, 130, 520, 610)
            ic = _ramp(s, 50, 130, 24.0, 25.5)
            oc = _ramp(s, 50, 130, 42, 52)
            f = _ramp(s, 50, 130, 13000, 19500)
            m = 3500
        else:
            w = _ramp(s, 130, 160, 7000, 2800)
            p = _ramp(s, 130, 160, 360, 250)
            ic = _ramp(s, 130, 160, 25.0, 23.0)
            oc = _ramp(s, 130, 160, 48, 33)
            f = _ramp(s, 130, 160, 14000, 7500)
            m = _ramp(s, 130, 160, 3200, 2600)
        nz = lambda k: random.uniform(-k, k)  # noqa: E731
        wall.append(round(w + nz(80), 1))
        pkg.append(round(p + nz(8), 1))
        inlet.append(round(ic + nz(0.2), 1))
        outlet.append(round(oc + nz(0.5), 1))
        fan.append(int(f + nz(200)))
        mhz.append(int(m + nz(30)))
    return {"t": t, "wall_w": wall, "pkg_w": pkg, "inlet_c": inlet, "outlet_c": outlet,
            "fan_rpm": fan, "core_mhz": mhz, "phases": phases, "sample": True}


def drive(label, dev, serial, link, link_neg_gbs, seq_r, seq_w, rr, rw,
          qd1r, qd1w, p999r, warn=None, tail=None, sweeps=None):
    return {"dev": dev, "label": label, "model": "Samsung PM9D3a", "form": "E3.S",
            "serial": serial, "link": link, "seq_read_gbs": seq_r, "seq_write_gbs": seq_w,
            "rand_read_iops": rr, "rand_write_iops": rw, "qd1_read_us": qd1r,
            "qd1_write_us": qd1w, "p99_9_read_us": p999r, "link_capable_gbs": 15.75,
            "link_negotiated_gbs": link_neg_gbs, "link_width_warn": warn,
            "tail": tail, "sweeps": sweeps}


def build_gpus(n=8):
    per = []
    for i in range(n):
        hot = (i == 3)  # one GPU runs a touch hotter/lower -- still PASS, shows variance
        per.append({
            "id": i,
            "gemm_fp8_tflops": round(4410 + random.uniform(-35, 35) - (60 if hot else 0)),
            "gemm_bf16_tflops": round(2240 + random.uniform(-20, 20)),
            "hbm_bw_gbs": round(7620 + random.uniform(-70, 70)),
            "nvlink_bw_gbs": round(1762 + random.uniform(-18, 18)),
            "power_w": round(986 + random.uniform(-15, 12)),
            "temp_c": round((71 if hot else 67) + random.uniform(-2, 3)),
            "ecc_errors": 0,
            "util_pct": round(98 + random.uniform(-1, 1)),
        })
    avg_fp8 = round(sum(g["gemm_fp8_tflops"] for g in per) / n)
    avg_hbm = round(sum(g["hbm_bw_gbs"] for g in per) / n)
    min_nvl = min(g["nvlink_bw_gbs"] for g in per)
    # NVLink pairwise bandwidth matrix (GB/s); 0 on the diagonal
    nvmatrix = [[0 if i == j else round(1760 + random.uniform(-28, 22)) for j in range(n)]
                for i in range(n)]
    # per-GPU power/temperature timeline over the run (ramps during AI combined load)
    tt = list(range(0, 161, 2))
    tgpus = []
    for gi in range(n):
        pw, tp = [], []
        for s in tt:
            if s < 50:
                bp, bt = 180, 38
            elif s < 130:
                bp = _ramp(s, 50, 130, 700, per[gi]["power_w"])
                bt = _ramp(s, 50, 130, 45, per[gi]["temp_c"])
            else:
                bp = _ramp(s, 130, 160, 620, 190)
                bt = _ramp(s, 130, 160, per[gi]["temp_c"], 40)
            pw.append(round(bp + random.uniform(-12, 12)))
            tp.append(round(bt + random.uniform(-1.2, 1.2), 1))
        tgpus.append({"power": pw, "temp": tp})
    return {
        "model": "NVIDIA B200 (HGX)", "count": n, "driver": "560.35.03", "cuda": "12.6",
        "tdp_w": 1000, "hbm_total_tb": round(n * 192 / 1000, 1),
        "rated_gemm_fp8_tflops": 4500, "rated_hbm_bw_gbs": 8000, "rated_nvlink_bw_gbs": 1800,
        "avg_gemm_fp8_tflops": avg_fp8, "avg_hbm_bw_gbs": avg_hbm, "min_nvlink_bw_gbs": min_nvl,
        "nccl_allreduce_gbps": 478, "dcgm_diag": "passed (level 3, all subtests)",
        "per_gpu": per, "nvlink_matrix": nvmatrix, "timeline": {"t": tt, "gpus": tgpus},
    }


def build():
    bench_storage = [
        drive("nvme0", "/dev/nvme0n1", "S6X1A0", "Gen5 x4", 15.75, 6.7, 3.85,
              1270000, 300000, 79, 13, 142,
              tail={"read": {"p99": 98, "p99_9": 142, "p99_99": 210, "p99_999": 480, "max": 1100},
                    "write": {"p99": 22, "p99_9": 41, "p99_99": 120, "p99_999": 350, "max": 900}},
              sweeps={"qd": [{"qd": 1, "iops": 12700}, {"qd": 4, "iops": 49000},
                             {"qd": 16, "iops": 185000}, {"qd": 64, "iops": 690000},
                             {"qd": 256, "iops": 1270000}],
                      "bs": [{"bs_kb": 4, "bw_gbs": 5.2}, {"bs_kb": 16, "bw_gbs": 6.0},
                             {"bs_kb": 64, "bw_gbs": 6.5}, {"bs_kb": 256, "bw_gbs": 6.7},
                             {"bs_kb": 1024, "bw_gbs": 6.7}]}),
        drive("nvme1", "/dev/nvme1n1", "S6X1A1", "Gen5 x4", 15.75, 6.6, 3.82, 1262000, 298000, 80, 13, 148),
        drive("nvme2", "/dev/nvme2n1", "S6X1A2", "Gen5 x4", 15.75, 6.65, 3.80, 1255000, 295000, 81, 14, 151),
        drive("nvme3", "/dev/nvme3n1", "S6X1A3", "Gen5 x2", 7.88, 6.6, 2.10, 980000, 190000, 82, 18, 233,
              warn="negotiated PCIe x2, capable x4 -- random write 53% of rating"),
    ]
    hw_storage = [{"dev": d["dev"], "label": d["label"], "model": d["model"], "pn": "MZWLO3T8HCLS",
                   "form": d["form"], "capacity": "3.84 TB", "serial": d["serial"],
                   "link": d["link"], "link_capable": "Gen5 x4"} for d in bench_storage]
    gpu = build_gpus(8)

    results = {
        "meta": {
            "report_title": "AI Server Hardware Validation Report",
            "subtitle": "8x NVIDIA B200 - GPU, Compute, Memory, NVMe, Network, Power & Thermal",
            "company": "Netweb Technologies India Limited",
            "classification": "",
            "hostname": "ty-cam-38",
            "server_ip": "172.16.15.38",
            "platform": "Tyrone Camarero AI / 2x Intel Xeon 6788P + 8x NVIDIA B200 (HGX)",
            "tier": "qualification",
            "window": "2026-06-01  ->  2026-06-04",
            "prepared_for": "Netweb Technologies India Limited",
            "date": "2026-06-05",
            "status": "PASS WITH LIMITATIONS",
            "rack": {"total_u": 42, "name": "R12 (lab)", "position": [20, 8]},
        },
        "executive_summary": [
            "This report covers a tier-2 (qualification) validation of a Tyrone Camarero AI node: "
            "dual Intel Xeon 6788P, 2 TB DDR5, eight NVIDIA B200 (HGX) accelerators on an NVLink "
            "fabric, four Samsung PM9D3a NVMe drives, and 400 GbE/NDR networking. The GPU, compute, "
            "memory, network and power/thermal subsystems meet or exceed datasheet expectations "
            "under sustained combined load.",
            "All eight B200 GPUs sustained ~4.41 PFLOPS FP8 each (98% of datasheet) with full NVLink "
            "bandwidth, zero ECC errors, and DCGM level-3 diagnostics passing. Peak wall power reached "
            "12.6 kW at a 25 C inlet with no GPU thermal throttling.",
            "One storage finding requires action: drive nvme3 negotiated a PCIe Gen5 x2 link instead "
            "of the capable x4, dropping its 4K random write to 53% of rating. The other three drives "
            "are within spec; re-seat / bifurcation remediation and a re-test are recommended.",
        ],
        "scorecard": {
            "header": ["Subsystem", "Result", "Detail", "Verdict"],
            "rows": [
                ["GPU / AI (8x B200)", "GEMM FP8 ~4.41 PFLOPS/GPU",
                 "98% of datasheet; NVLink 1.76 TB/s; 0 ECC; DCGM L3 pass", "PASS"],
                ["Compute (CPU)", "HPL 6.10 TFLOPS",
                 "90% of Rpeak; stable across soak, 0 throttle events", "PASS"],
                ["Memory", "STREAM Triad 920 GB/s",
                 "DDR5-6400, all DIMMs at rated speed; loaded latency 118 ns", "PASS"],
                ["Storage (4x NVMe)", "3 of 4 drives at spec",
                 "nvme3 negotiated PCIe x2 -> random write 53% of rating", "ACTION"],
                ["Network (400 GbE)", "RoCE 392 Gb/s",
                 "Near line-rate RoCEv2; PFC + DCQCN verified lossless", "PASS"],
                ["Power & Thermal", "Peak 12.6 kW @ 25 C inlet",
                 "Outlet 52 C, fans 19.5k RPM at combined load; no throttle", "PASS"],
            ],
        },
        "hardware": {
            "system": {"vendor": "Tyrone (Netweb)", "model": "Camarero AI HGX-B200",
                       "board": "auto-detected at run time (dmidecode)",
                       "chassis": "8U HGX", "hostname": "ty-cam-38", "serial": "TY-CAM-380417"},
            "cpu": {"model": "2x Intel Xeon 6788P (Granite Rapids)", "sockets": 2,
                    "cores_per_socket": 86, "threads": 344, "base_ghz": 2.0, "boost_ghz": 3.8,
                    "numa_nodes": 2, "isa": "AVX-512, AMX, BF16",
                    "numa_device_map": "GPU0-3 -> node0; GPU4-7 -> node1; nvme0-3 -> node0"},
            "memory": {"total": "2 TB", "type": "DDR5 RDIMM",
                       "dimms_populated": "32 x 64 GB (16 per socket)",
                       "configured_speed": "6400 MT/s", "rated_speed": "6400 MT/s",
                       "speed_status": "all DIMMs at rated speed"},
            "bios": {"version": "2.4", "date": "2026-02-10", "power_profile": "Max Performance",
                     "c_states": "disabled", "numa_nps": "NPS2", "pcie_aspm": "disabled",
                     "m2_bifurcation": "x4x4x4x4 (slot 6 reports x2 -- see findings)"},
            "bmc": {"firmware": "BMC 2.06.0", "psu_model": "16 kW shelf (6x 3kW, 5+1)",
                    "psu_watts": "18000 W (5+1)", "redundancy": "N+1 redundant",
                    "telemetry_source": "Redfish (DCMI power fallback)"},
            "kernel": {"distro": "Ubuntu 24.04.1 LTS", "kernel": "6.8.0-45-generic",
                       "governor": "performance", "tuned_profile": "accelerator-performance",
                       "cmdline": "ro quiet pcie_aspm=off nvme_core.default_ps_max_latency_us=0 "
                                  "iommu=pt nvidia_drm.modeset=1"},
            "storage": hw_storage,
            "network": [
                {"iface": "ibp1s0", "model": "NVIDIA ConnectX-7 NDR (400G)",
                 "speed": "400 Gb/s", "driver": "mlx5_core 24.07"},
                {"iface": "eno1", "model": "Intel X710", "speed": "10 Gb/s", "driver": "i40e"},
            ],
        },
        "benchmarks": {
            "storage": bench_storage,
            "cpu": {"hpl_tflops": 6.10, "hpl_peak_tflops": 6.77, "hpl_efficiency_pct": 90,
                    "stress_ng_cpu_bogo_ops": 612000, "avx512": "yes", "amx": "yes",
                    "peak_pkg_w": 610, "soak_hours": 24, "throttle_events": 0},
            "memory": {"stream_triad_gbs": 920, "stream_copy_gbs": 905,
                       "mlc_loaded_latency_ns": 118, "mlc_peak_bw_gbs": 948,
                       "per_socket_gbs": 460, "dimm_speed_mts": 6400, "rated_mts": 6400},
            "network": {"tcp_rx_gbps": 372, "tcp_tx_gbps": 378, "tcp_bidir_gbps": 690,
                        "tcp_multistream_gbps": 388, "roce_ib_write_bw_gbps": 392,
                        "roce_ib_write_lat_p99_9_us": 3.4, "mtu": 9000, "pfc": "enabled (prio 3)",
                        "ecn": "DCQCN enabled", "rdma_link": "400 Gb/s, Active",
                        "peer": "ty-cam-37", "fabric": "NDR via lossless switch"},
            "gpu": gpu,
        },
        "spec_rows": [
            {"metric": "GEMM FP8 (per GPU)", "measured": gpu["avg_gemm_fp8_tflops"], "rated": 4500, "unit": "TFLOPS"},
            {"metric": "HBM bandwidth (per GPU)", "measured": gpu["avg_hbm_bw_gbs"], "rated": 8000, "unit": "GB/s"},
            {"metric": "NVLink bandwidth (per GPU)", "measured": gpu["min_nvlink_bw_gbs"], "rated": 1800, "unit": "GB/s"},
            {"metric": "HPL Rmax", "measured": 6.10, "rated": 6.77, "unit": "TFLOPS"},
            {"metric": "STREAM Triad", "measured": 920, "rated": 980, "unit": "GB/s"},
            {"metric": "RoCE ib_write_bw", "measured": 392, "rated": 400, "unit": "Gb/s"},
            {"metric": "Seq Read (nvme0)", "measured": 6.7, "rated": 6.8, "unit": "GB/s"},
            {"metric": "4K Rand Write (nvme0)", "measured": 300000, "rated": 360000, "unit": "IOPS"},
            {"metric": "4K Rand Write (nvme3, x2 link)", "measured": 190000, "rated": 360000, "unit": "IOPS"},
        ],
        "comparison": {
            "servers": ["node-217 (Gen4 slot)", "ty-cam-38 (Gen5 slot)"],
            "metrics": [
                {"name": "Seq Read GB/s", "a": 3.4, "b": 6.7, "fmt": "{:.1f}"},
                {"name": "4K Rand Read IOPS", "a": 820000, "b": 1270000, "fmt": "{:,.0f}"},
                {"name": "GEMM FP8 (PFLOPS, 8-GPU)", "a": 0, "b": 35.3, "fmt": "{:.1f}"},
            ],
            "caption": "Same four PM9D3a drives across slot generations; the AI node adds 8x B200.",
            "notes": "Sequential read nearly doubles on Gen5; the B200 GPU complex is new to this node.",
        },
        "telemetry": build_telemetry(),
        "perf_per_watt": [
            {"name": "GEMM FP8 per kW (8-GPU)", "value": 2820, "unit": "TFLOPS/kW"},
            {"name": "HBM bandwidth per kW", "value": 4.86, "unit": "TB/s per kW"},
            {"name": "Rand-read IOPS per W (NVMe)", "value": 1114, "unit": "IOPS/W"},
        ],
        "health": {
            "header": ["Component", "Link", "Temp pre->post", "Wear", "Media errors", "Verdict"],
            "rows": [
                ["nvme0 PM9D3a", "Gen5 x4", "41 -> 58 C", "0%", "0", "PASS"],
                ["nvme1 PM9D3a", "Gen5 x4", "42 -> 59 C", "0%", "0", "PASS"],
                ["nvme2 PM9D3a", "Gen5 x4", "40 -> 57 C", "1%", "0", "PASS"],
                ["nvme3 PM9D3a", "Gen5 x2 (!)", "43 -> 61 C", "0%", "0", "ACTION"],
                ["8x B200 (HGX)", "NVLink5", "37 -> 71 C", "n/a", "0 ECC", "PASS"],
            ],
        },
        "sel_status": "SEL cleared at run start; 0 new critical events after run "
                      "(1 informational PSU input-history entry).",
        "dmesg_status": "No new MCE, ECC, PCIe AER, NVMe reset or Xid GPU events. One pre-existing "
                        "informational APST quirk message (unchanged).",
        "findings": [
            "nvme3 negotiated a PCIe Gen5 x2 link (capable x4); 4K random write fell to 190k IOPS "
            "(53% of the 360k datasheet rating) and sequential write to 2.1 GB/s.",
            "All 8x B200 sustained ~4.41 PFLOPS FP8 (98% of datasheet) with NVLink at 1.76 TB/s and "
            "zero ECC errors; DCGM level-3 diagnostics passed on every GPU.",
            "Peak combined-load wall power was 12.6 kW at 25 C inlet / 52 C outlet with no GPU thermal "
            "throttling; the 5+1 PSU shelf retained redundancy throughout.",
        ],
        "recommendations": [
            "Re-seat nvme3 and confirm slot-6 bifurcation is x4x4x4x4 in BIOS; if the x2 link persists, "
            "swap the riser and re-run the storage tier.",
            "Retain the current performance profile (C-states off, NPS2, ASPM off) for production AI "
            "workloads; it is the configuration these GPU/memory numbers were measured under.",
            "Monitor GPU3 (ran ~4 C warmer); within spec but worth tracking across the soak fleet.",
        ],
        "conclusion": {
            "header": ["Area", "Outcome", "Verdict"],
            "rows": [
                ["AI / GPU complex", "8x B200 at 98% datasheet, full NVLink, 0 ECC, DCGM L3 pass", "PASS"],
                ["Acceptance criteria", "Compute / memory / network meet or exceed datasheet; thermals in envelope", "PASS"],
                ["Storage", "3 of 4 drives nominal; nvme3 link degraded -- remediate and re-test", "ACTION"],
                ["Release recommendation", "Approve for AI deployment after nvme3 PCIe remediation", "LIMITED"],
            ],
        },
    }

    repro = {
        "fio": "fio-3.36", "nvme_cli": "nvme version 2.8", "ipmitool": "1.8.19",
        "stress_ng": "0.17.06", "iperf3": "3.16", "dcgmi": "3.3.6", "cuda": "12.6",
        "kernel": "6.8.0-45-generic", "kernel_cmdline": results["hardware"]["kernel"]["cmdline"],
        "tuned_profile": "accelerator-performance",
        "bios_profile": "Max Performance (Determinism: Power)",
        "commands": "see runs/<run_id>/run.log for exact invocations",
    }
    digest = hashlib.sha256(
        json.dumps(results, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    repro["data_bundle_sha256"] = digest
    results["reproducibility"] = repro
    return results


def main():
    results = build()
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"wrote {OUT}  ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    main()
