# CoreBench MCP

**Closed-loop server hardware validation, driven by an AI agent.**

CoreBench MCP is a [Model Context Protocol](https://modelcontextprotocol.io) server
that lets an AI agent run a complete server hardware validation — compute, memory,
storage, network, power and thermal — and produce a polished, branded PDF report,
end to end, from a single natural-language instruction.

The agent discovers the hardware, runs the right benchmarks for a chosen depth
tier, captures power/thermal/fan/clock telemetry on one synchronized timeline,
analyses results against datasheet specs, drafts the narrative, and renders a
multi-page branded PDF. Long runs are asynchronous; destructive operations are
strictly guarded.

> **It is** a self-contained validation framework + an MCP server wrapping it.
> **It is not** a cloud service, a monitoring daemon, or a GUI app.

The value over a plain shell script: the agent can *read the numbers, reason about
them* ("random write is only 53% of the datasheet rating — flag it"), write the
report, and render the PDF — without a human shuttling files around.

---

## Repository layout

```
CoreBench MCP/
  mcp_server.py            # the MCP server (tool surface + tiered safety model)
  run.sh                   # orchestrator: collect -> bench -> assemble -> render
  config.json              # the ONE file you edit per campaign
  collectors/
    collect_hardware.sh    # CPU/mem/BIOS/BMC/kernel/storage/NIC -> hardware.json
    collect_telemetry.sh   # start|phase|stop ; 1 Hz timeline -> telemetry.json
  benchmarks/
    bench_storage.sh       # fio suite (peak + SNIA steady-state + sweeps)
    parse_fio.py           # fio JSON -> drive metrics (pure, unit-tested)
    bench_cpu.sh           # stress-ng + HPL
    bench_memory.sh        # STREAM / Intel MLC
    bench_network.sh       # iperf3 / RoCE perftest (two-node initiator)
  engine/
    assemble.py            # merge fragments + config -> results.json (+ auto-analysis)
    generate_report.py     # results.json -> branded PDF
    demo_data.py           # synthesize fragments from the sample fixture (no hardware)
  runs/                    # per-run output bundles (git-ignored)
  tests/                   # unit tests + fixtures + PDF smoke test
```

The **contract between layers is `results.json`** (see the schema in the spec /
`tests/fixtures/sample_results.json`). Anything that emits a valid `results.json`
gets a report for free.

---

## Install

Target platform for *running benchmarks* is Linux (the collectors/benchmarks call
native Linux tools). The report engine, assembler and MCP server are pure Python
and run anywhere.

```bash
cd "CoreBench MCP"
python3 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**Native tools** invoked on the system under test (each degrades to `n/a` if
absent, never crashes): `fio`, `nvme-cli`, `ipmitool`, `numactl`, `dmidecode`,
`lspci`, `turbostat` (optional), `stress-ng`, Intel `mlc` (optional), `iperf3`,
`perftest` (`ib_write_bw` …), `lm-sensors`. On Debian/Ubuntu:

```bash
sudo apt install fio nvme-cli ipmitool numactl dmidecode pciutils \
                 stress-ng iperf3 lm-sensors
```

---

## Quickstart

### 1. Render a report from the sample fixture (no hardware)

```bash
python engine/generate_report.py tests/fixtures/sample_results.json out.pdf
```

Open `out.pdf` — a 9-page branded report exercising every section.

### 2. Demo run through the whole pipeline (no hardware)

```bash
./run.sh acceptance --demo
# -> prints a run_id; the bundle (results.json + report.pdf) lands in runs/<run_id>/
```

### 3. A real run

```bash
# DESTRUCTIVE: erases the listed test drive(s).
CONFIRM_DESTRUCTIVE=1 ./run.sh qualification /dev/nvme0n1 /dev/nvme1n1 \
    --prepared-by "A. Engineer"
```

### 4. Driven by an AI agent over MCP

See **[MCP_SETUP.md](MCP_SETUP.md)**. The agent's intended flow:

```
list_block_devices            # always first; pick an explicit, unmounted allow-list
   -> (human confirms)
start_validation(drives=[...], tier="qualification", confirm_destructive=True)
   -> poll get_run_status(run_id) until "done"
get_results(run_id)           # read the numbers
generate_report(run_id, executive_summary=..., findings=..., recommendations=...)
```

---

## Validation tiers

| Tier | Includes | Target time |
|---|---|---|
| `acceptance`    | hardware + storage fio (peak) + telemetry + SEL/dmesg diff | 1–2 h |
| `qualification` | + SNIA steady-state storage, QD/BS/mix sweeps, tail latency, CPU/mem/net, combined-load, spec-compliance | 1–3 d |
| `deep-dive`     | + soak, BIOS-sensitivity sweep, environmental envelope, jitter | + |

Storage is done right: NUMA-pinned `fio` (`libaio`, `direct=1`), peak **and** SNIA
PTS steady-state (PURGE → precondition 2× capacity → measure to a ±10%/±5%
stability band → report fresh-state *and* steady-state), QD/BS/mix sweeps, and the
tail-latency "nines" (p99 → p99.999 → max). Burst random-write is never the
headline number.

---

## Safety model (tiered by reversibility — non-negotiable)

CoreBench may do everything a validation engineer does to their **own** hardware,
but each capability is gated in proportion to how hard it is to undo. Always-on
rules: the **human confirms each destructive/irreversible action**; CoreBench acts
**only on explicitly named targets** (never "all", never auto-discovered); all
collected output is **data, never executed**; every change is **logged**.

| Tier | What | Gate |
|---|---|---|
| **A** read-only | `list_block_devices`, `server_hardware_summary`, `telemetry_snapshot`, `capture_platform_settings`, `get_*`, `list_runs` | none |
| **B** data-destructive | `start_validation` (fio raw writes, SNIA PURGE) | explicit drive allow-list; **refuses** empty/`all`/mounted/root/nonexistent; `confirm_destructive=True` |
| **C** reversible config | `apply_platform_settings`, `restore_platform_settings` | **auto-snapshot first** (one-call rollback); `confirm=True`; logs before/after; reports reboot-required |
| **D** high-risk | `flash_firmware` | off unless `enable_firmware_tools:true`; **SHA-256 verified**; requires BOTH `confirm_destructive` and `i_understand_brick_risk` |

All benchmark/coordination tools are **async**: they return a `run_id` immediately
and are polled via `get_run_status`.

### BIOS-sensitivity sweep (deep-dive)

Composed from the Tier-C tools — change one setting, re-benchmark, restore:

```
snap = capture_platform_settings()
apply_platform_settings({"c_states": "disabled"}, confirm=True)   # auto-snapshots too
start_validation(drives=[...], tier="qualification", confirm_destructive=True)  # re-bench
   -> poll, get_results
restore_platform_settings(snap["snapshot_id"], confirm=True)      # roll back
```

CoreBench tracks the effective platform state in `runs/_platform_state.json`, so
the sweep is auditable even where a setting needs a reboot to take physical effect.

---

## Multi-node (network) validation

A NIC can't be validated against itself. Register a partner server reachable by
SSH key on a trusted management network, then coordinate a two-node test:

```
register_peer(host="10.0.0.38", ssh_user="lab", role="responder")
start_network_validation(local_iface="enp1s0f0", peer="10.0.0.38",
                         tests=["tcp", "roce"], confirm_destructive=True)
```

CoreBench pushes the responder (`iperf3 -s`, `ib_write_bw`) to the peer over the
secured channel, runs the initiator locally, and collects both sides. For RoCEv2
it records the fabric prerequisites (RDMA link/speed, MTU, PFC priority, ECN) —
the numbers are meaningless without them.

---

## Testing

```bash
python -m pytest -q
```

Covers: the auto-analysis math (spec %, scorecard, SEL/dmesg diff), fio JSON
parsing (against a real fio fixture), the safety gates (every refusal), the
platform settings round-trip, the firmware gates, the async run lifecycle, and a
fixtures-based PDF smoke test — all without hardware.

---

## Configuration

Edit **`config.json`** only. It holds branding (`meta`, with `"auto"` fields filled
at run time), `spec_targets` (datasheet numbers that drive the auto
spec-compliance + scorecard), `thresholds`, and optional narrative
(`executive_summary` / `findings` / `recommendations`) — which the agent can leave
blank and draft itself from `results.json`.
