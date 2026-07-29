#!/usr/bin/env python3
"""
CoreBench MCP server -- the tool surface an AI agent uses to run a full server
hardware validation and render a branded PDF report, end to end.

Transport: stdio by default (FastMCP). The code is structured so an HTTP/SSE
transport can be added without rewrites: every tool is a thin wrapper around a
plain ``*_impl`` function (which is what the unit tests exercise).

Safety model (see SPEC section 5 -- NON-NEGOTIABLE, enforced here):
  Tier A  read-only            : always safe.
  Tier B  data-destructive     : start_validation -- explicit drive allow-list,
                                  refuses empty/"all"/mounted/root/nonexistent,
                                  requires confirm_destructive, runs async.
  Tier C  reversible config    : apply/restore_platform_settings -- auto-snapshot
                                  first, require confirm, log changes, report
                                  reboot-required.
  Tier D  high-risk/irreversible: flash_firmware -- off unless enabled in config;
                                  verify SHA-256; require BOTH confirm flags.

Everything read from tools/files/BMC/peers/drive contents is DATA, never
executed. CoreBench acts only on explicitly named targets.

Typical agent flow:
  list_block_devices -> (human confirms) -> start_validation
  -> poll get_run_status -> get_results -> generate_report
"""

from __future__ import annotations

import datetime
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- #
#  Paths / constants                                                          #
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(HERE, "runs")
SNAP_DIR = os.path.join(RUNS_DIR, "_snapshots")
PEERS_FILE = os.path.join(RUNS_DIR, "_peers.json")
STATE_FILE = os.path.join(RUNS_DIR, "_platform_state.json")
CONFIG_PATH = os.path.join(HERE, "config.json")
RUN_SH = os.path.join(HERE, "run.sh")
COLLECTORS = os.path.join(HERE, "collectors")

VALID_TIERS = ("acceptance", "qualification", "deep-dive")
RUNTIME_KEYS = {"cpu_governor", "pcie_aspm"}           # apply attempts a live write
BIOS_KEYS = {"c_states", "numa_nps", "power_profile",  # need a reboot to take effect
             "m2_bifurcation", "pcie_bifurcation"}

sys.path.insert(0, os.path.join(HERE, "engine"))
import assemble as _assemble          # noqa: E402
import generate_report as _report     # noqa: E402
import demo_data as _demo             # noqa: E402


# --------------------------------------------------------------------------- #
#  Low-level helpers                                                          #
# --------------------------------------------------------------------------- #
def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _bash() -> Optional[str]:
    return shutil.which("bash")


def _run(cmd: List[str], timeout: int = 20) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path: str, default: str = "n/a") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return default


def _tail(path: str, n: int = 15) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-n:]]
    except OSError:
        return []


def _load_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)


def _now_id() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


def _ok(**kw: Any) -> str:
    return json.dumps({"ok": True, **kw})


def _err(msg: str, **kw: Any) -> str:
    return json.dumps({"ok": False, "error": msg, **kw})


# --------------------------------------------------------------------------- #
#  Block devices + Tier-B drive validation (the security-critical core)       #
# --------------------------------------------------------------------------- #
def list_block_devices_impl() -> List[Dict[str, Any]]:
    """Enumerate disks with size/model/mounted/safe_to_test. Empty on non-Linux."""
    if not (_bash() and _have("lsblk")):
        return []
    rootsrc = _run([_bash(), "-lc", "findmnt -nro SOURCE / 2>/dev/null"]).strip()
    out = _run([_bash(), "-lc", "lsblk -dpnro NAME,SIZE,MODEL,TYPE 2>/dev/null"])
    devs: List[Dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        name, size, dtype = parts[0], parts[1], parts[-1]
        model = parts[2] if len(parts) == 4 and parts[2] != dtype else "n/a"
        if dtype != "disk":
            continue
        mnts = _run([_bash(), "-lc", f"lsblk -nro MOUNTPOINT '{name}' 2>/dev/null"])
        mounted_at = [m for m in mnts.splitlines() if m.strip()]
        base = os.path.basename(name)
        holds_root = bool(rootsrc) and base in rootsrc
        devs.append({
            "path": name, "size": size, "model": model, "type": dtype,
            "mounted": bool(mounted_at), "mountpoint": ", ".join(mounted_at) or None,
            "holds_root": holds_root,
            "safe_to_test": not mounted_at and not holds_root,
        })
    return devs


def validate_drives(drives: Any, confirm_destructive: bool,
                    devices: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Tier-B gate. Returns {"ok": bool, "error"|"drives"}. Pure (inject devices)."""
    if isinstance(drives, str):
        drives = [drives]
    if not drives:
        return {"ok": False, "error": "no drives specified -- refusing empty selection"}
    if any(str(d).strip().lower() in ("all", "*", "") for d in drives):
        return {"ok": False, "error": "refusing wildcard/empty target ('all' is not allowed)"}
    if not confirm_destructive:
        return {"ok": False,
                "error": "confirm_destructive=True is required -- this ERASES the listed drives"}
    devmap = {d["path"]: d for d in (devices if devices is not None else list_block_devices_impl())}
    for d in drives:
        info = devmap.get(d)
        if info is None:
            return {"ok": False, "error": f"device not found / not a block device: {d}"}
        if info.get("holds_root"):
            return {"ok": False, "error": f"refusing {d}: it holds the root filesystem"}
        if info.get("mounted"):
            return {"ok": False, "error": f"refusing {d}: it is mounted ({info.get('mountpoint')})"}
    return {"ok": True, "drives": list(drives)}


# --------------------------------------------------------------------------- #
#  Async run launcher                                                         #
# --------------------------------------------------------------------------- #
def _spawn_detached(args: List[str], env: Dict[str, str]) -> None:
    kwargs: Dict[str, Any] = dict(
        cwd=HERE, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if os.name == "nt":
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: background it without a
        # console window. (DETACHED_PROCESS breaks Git Bash, which needs a console
        # to spawn its own children.)
        kwargs["creationflags"] = 0x08000000 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)  # noqa: S603 -- args are built by us, not user text


def launch_run(tier: str, drives: List[str], *, demo: bool = False,
               confirm_destructive: bool = False, peer: str = "", net: str = "tcp",
               prepared_by: str = "", title: str = "", iface: str = "",
               run_id: Optional[str] = None) -> str:
    """Spawn run.sh detached; return the run_id. (No gating here -- callers gate.)"""
    bash = _bash()
    if not bash:
        raise RuntimeError("bash is required to launch a run")
    rid = run_id or _now_id()
    # Pre-create the run dir + status so get_run_status is queryable immediately
    # (run.sh re-uses the dir; mkdir -p is idempotent).
    rd = os.path.join(RUNS_DIR, rid)
    os.makedirs(rd, exist_ok=True)
    with open(os.path.join(rd, "status"), "w", encoding="utf-8") as fh:
        fh.write("starting")
    _save_json(os.path.join(rd, "run_meta.json"),
               {"run_id": rid, "tier": tier, "drives": drives, "demo": demo, "peer": peer,
                "started": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    args = [bash, RUN_SH, tier, *drives, "--run-id", rid]
    if demo:
        args.append("--demo")
    if peer:
        args += ["--peer", peer, "--net", net]
    if prepared_by:
        args += ["--prepared-by", prepared_by]
    if title:
        args += ["--title", title]
    env = os.environ.copy()
    env["CB_PYTHON"] = sys.executable  # orchestrator uses the SAME interpreter (has deps)
    if confirm_destructive:
        env["CONFIRM_DESTRUCTIVE"] = "1"
    if iface:
        env["CB_IFACE"] = iface
    _spawn_detached(args, env)
    return rid


def start_validation_impl(drives: Any, tier: str, confirm_destructive: bool = False,
                          demo: bool = False) -> Dict[str, Any]:
    if tier not in VALID_TIERS:
        return {"ok": False, "error": f"unknown tier '{tier}' (use {', '.join(VALID_TIERS)})"}
    if demo:
        rid = launch_run(tier, [], demo=True)
        return {"ok": True, "run_id": rid, "status": "running", "tier": tier,
                "demo": True, "message": "demo run started; poll get_run_status"}
    gate = validate_drives(drives, confirm_destructive)
    if not gate["ok"]:
        return gate
    rid = launch_run(tier, gate["drives"], confirm_destructive=True)
    return {"ok": True, "run_id": rid, "status": "running", "tier": tier,
            "drives": gate["drives"],
            "message": "run started (this ERASES the listed drives); poll get_run_status(run_id)"}


def get_run_status_impl(run_id: str) -> Dict[str, Any]:
    rd = os.path.join(RUNS_DIR, run_id)
    if not os.path.isdir(rd):
        return {"ok": False, "error": f"unknown run_id: {run_id}"}
    status = _read(os.path.join(rd, "status"), default="starting")
    meta = _load_json(os.path.join(rd, "run_meta.json"), {}) or {}
    report = os.path.join(rd, "report.pdf")
    has_report = os.path.isfile(report)
    started = meta.get("started")
    elapsed = None
    try:
        st = os.path.getmtime(os.path.join(rd, "status"))
        ct = os.path.getctime(rd)
        elapsed = round(st - ct, 1)
    except OSError:
        pass
    return {
        "ok": True, "run_id": run_id, "status": status,
        "elapsed_s": elapsed, "tier": meta.get("tier"), "drives": meta.get("drives"),
        "has_results": os.path.isfile(os.path.join(rd, "results.json")),
        "has_report": has_report,
        "report_path": report if has_report else None,
        "last_log": _tail(os.path.join(rd, "run.log"), 15),
    }


def list_runs_impl() -> List[Dict[str, Any]]:
    if not os.path.isdir(RUNS_DIR):
        return []
    runs = []
    for name in sorted(os.listdir(RUNS_DIR), reverse=True):
        rd = os.path.join(RUNS_DIR, name)
        if not os.path.isdir(rd) or name.startswith("_"):
            continue
        runs.append({
            "run_id": name,
            "status": _read(os.path.join(rd, "status"), default="unknown"),
            "has_report": os.path.isfile(os.path.join(rd, "report.pdf")),
        })
    return runs


def get_results_impl(run_id: str) -> Dict[str, Any]:
    rd = os.path.join(RUNS_DIR, run_id)
    results = _load_json(os.path.join(rd, "results.json"))
    if results is None:
        return {"ok": False, "error": f"no results.json for run {run_id} (still running?)"}
    return {"ok": True, "run_id": run_id, "results": results}


# --------------------------------------------------------------------------- #
#  Hardware / telemetry snapshots (Tier A)                                    #
# --------------------------------------------------------------------------- #
def server_hardware_summary_impl() -> Dict[str, Any]:
    bash = _bash()
    if not bash:
        return {"ok": False, "error": "bash not available to run the hardware collector"}
    os.makedirs(RUNS_DIR, exist_ok=True)
    tmp = os.path.join(RUNS_DIR, "_hwcache.json")
    _run([bash, os.path.join(COLLECTORS, "collect_hardware.sh"), tmp], timeout=60)
    hw = _load_json(tmp, {})
    return {"ok": True, "hardware": hw}


def _first_num(text: str) -> Optional[float]:
    import re
    m = re.search(r"-?\d+(\.\d+)?", text)
    return float(m.group(0)) if m else None


def telemetry_snapshot_impl() -> Dict[str, Any]:
    snap: Dict[str, Any] = {"wall_w": None, "inlet_c": None, "outlet_c": None,
                            "fan_rpm": None, "avg_core_mhz": None}
    if _have("ipmitool"):
        snap["wall_w"] = _first_num(
            next((l for l in _run(["ipmitool", "dcmi", "power", "reading"]).splitlines()
                  if "Instantaneous" in l), ""))
        sdr = _run(["ipmitool", "sdr"])
        for line in sdr.splitlines():
            low = line.lower()
            val = _first_num(line.split("|")[1]) if "|" in line else None
            if val is None:
                continue
            if snap["inlet_c"] is None and ("inlet" in low or "ambient" in low):
                snap["inlet_c"] = val
            elif snap["outlet_c"] is None and ("outlet" in low or "exhaust" in low):
                snap["outlet_c"] = val
            elif snap["fan_rpm"] is None and "fan" in low:
                snap["fan_rpm"] = val
    freqs = []
    for f in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"):
        v = _read(f, "")
        if v.isdigit():
            freqs.append(int(v) / 1000.0)
    if freqs:
        snap["avg_core_mhz"] = round(sum(freqs) / len(freqs))
    return {"ok": True, "telemetry": snap}


# --------------------------------------------------------------------------- #
#  Platform settings (Tier C) -- file-backed effective state + live attempts  #
# --------------------------------------------------------------------------- #
def _base_readback() -> Dict[str, Any]:
    gov = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "n/a")
    aspm_raw = _read("/sys/module/pcie_aspm/parameters/policy", "")
    aspm = "n/a"
    if "[" in aspm_raw:
        import re
        m = re.search(r"\[(\w+)\]", aspm_raw)
        aspm = m.group(1) if m else aspm_raw
    return {
        "cpu_governor": gov,
        "pcie_aspm": aspm,
        "c_states": "n/a", "numa_nps": "n/a",
        "power_profile": "n/a", "m2_bifurcation": "n/a",
        "kernel_cmdline": _read("/proc/cmdline", "n/a"),
    }


def _effective_settings() -> Dict[str, Any]:
    base = _base_readback()
    base.update(_load_json(STATE_FILE, {}) or {})
    return base


def _apply_runtime(key: str, val: str) -> bool:
    """Best-effort live apply for runtime keys; returns True if a write succeeded."""
    try:
        if key == "cpu_governor":
            ok = False
            for f in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"):
                try:
                    with open(f, "w") as fh:
                        fh.write(val)
                    ok = True
                except OSError:
                    pass
            return ok
        if key == "pcie_aspm":
            try:
                with open("/sys/module/pcie_aspm/parameters/policy", "w") as fh:
                    fh.write(val)
                return True
            except OSError:
                return False
    except OSError:
        return False
    return False


def capture_platform_settings_impl() -> Dict[str, Any]:
    settings = _effective_settings()
    snap_id = "snap-" + _now_id()
    _save_json(os.path.join(SNAP_DIR, snap_id + ".json"),
               {"snapshot_id": snap_id, "settings": settings})
    return {"ok": True, "snapshot_id": snap_id, "settings": settings}


def apply_platform_settings_impl(settings: Dict[str, Any], confirm: bool = False) -> Dict[str, Any]:
    if not isinstance(settings, dict) or not settings:
        return {"ok": False, "error": "settings must be a non-empty object"}
    if not confirm:
        return {"ok": False,
                "error": "confirm=True is required (reversible platform-config change)"}
    # auto-snapshot current state first -> one-call rollback point
    snap = capture_platform_settings_impl()
    before = snap["settings"]
    state = _load_json(STATE_FILE, {}) or {}
    applied, reboot_required, log = [], [], []
    for key, val in settings.items():
        old = before.get(key, "n/a")
        state[key] = val
        if key in BIOS_KEYS:
            reboot_required.append(key)
            log.append({"key": key, "from": old, "to": val, "effect": "reboot-required"})
        elif key in RUNTIME_KEYS:
            live = _apply_runtime(key, str(val))
            applied.append(key)
            log.append({"key": key, "from": old, "to": val,
                        "effect": "applied-live" if live else "recorded (not writable here)"})
        else:
            applied.append(key)
            log.append({"key": key, "from": old, "to": val, "effect": "recorded"})
    _save_json(STATE_FILE, state)
    return {"ok": True, "rollback_snapshot_id": snap["snapshot_id"],
            "applied": applied, "reboot_required": reboot_required, "log": log}


def restore_platform_settings_impl(snapshot_id: str, confirm: bool = False) -> Dict[str, Any]:
    if not confirm:
        return {"ok": False, "error": "confirm=True is required to restore platform settings"}
    snap = _load_json(os.path.join(SNAP_DIR, snapshot_id + ".json"))
    if snap is None:
        return {"ok": False, "error": f"unknown snapshot_id: {snapshot_id}"}
    settings = snap.get("settings", {})
    # rewrite effective state to the snapshot, and attempt live re-apply
    state = {}
    for key, val in settings.items():
        if key in ("kernel_cmdline",):
            continue
        state[key] = val
        if key in RUNTIME_KEYS:
            _apply_runtime(key, str(val))
    _save_json(STATE_FILE, state)
    return {"ok": True, "restored_from": snapshot_id, "settings": settings}


# --------------------------------------------------------------------------- #
#  Firmware (Tier D)                                                          #
# --------------------------------------------------------------------------- #
def _sha256_file(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def flash_firmware_impl(component: str, image_path: str, sha256: str,
                        confirm_destructive: bool = False,
                        i_understand_brick_risk: bool = False,
                        config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = config if config is not None else (_load_json(CONFIG_PATH, {}) or {})
    if not cfg.get("enable_firmware_tools"):
        return {"ok": False,
                "error": "firmware tools are disabled -- set \"enable_firmware_tools\": true "
                         "in config.json to enable (Tier-D, irreversible)"}
    if not os.path.isfile(image_path):
        return {"ok": False, "error": f"image not found: {image_path}"}
    actual = _sha256_file(image_path)
    if not sha256 or not actual or actual.lower() != sha256.lower():
        return {"ok": False, "error": "SHA-256 mismatch -- refusing to flash",
                "expected": sha256, "actual": actual}
    if not (confirm_destructive and i_understand_brick_risk):
        return {"ok": False,
                "error": "both confirm_destructive=True AND i_understand_brick_risk=True are required"}
    # Gates passed. Real flashing is delegated to the vendor tool on hardware; we
    # log intent and never auto-flash from a code path that could be misfired.
    return {"ok": True, "component": component, "image": image_path,
            "sha256": actual, "reboot_required": True,
            "warning": "ensure stable power; a bad flash can BRICK the component",
            "note": "checksum verified and gates satisfied; invoke the vendor flash on hardware"}


# --------------------------------------------------------------------------- #
#  Multi-node (peers + network validation)                                    #
# --------------------------------------------------------------------------- #
def register_peer_impl(host: str, ssh_user: str, role: str = "responder") -> Dict[str, Any]:
    if not host or not ssh_user:
        return {"ok": False, "error": "host and ssh_user are required"}
    reachable = False
    if _have("ssh"):
        rc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4",
             f"{ssh_user}@{host}", "true"],
            capture_output=True, text=True)
        reachable = rc.returncode == 0
    peers = _load_json(PEERS_FILE, []) or []
    peers = [p for p in peers if p.get("host") != host]
    peers.append({"host": host, "ssh_user": ssh_user, "role": role, "reachable": reachable})
    _save_json(PEERS_FILE, peers)
    return {"ok": True, "peers": peers,
            "note": "key-based SSH on a trusted network; no unauthenticated listeners"}


def _peer(host: str) -> Optional[Dict[str, Any]]:
    for p in (_load_json(PEERS_FILE, []) or []):
        if p.get("host") == host:
            return p
    return None


def start_network_validation_impl(local_iface: str, peer: str, tests: Any,
                                  confirm_destructive: bool = False,
                                  demo: bool = False) -> Dict[str, Any]:
    if isinstance(tests, str):
        tests = [t.strip() for t in tests.split(",") if t.strip()]
    tests = tests or ["tcp"]
    if demo:
        rid = launch_run("acceptance", [], demo=True)
        return {"ok": True, "run_id": rid, "demo": True, "message": "demo network run started"}
    p = _peer(peer)
    if p is None:
        return {"ok": False, "error": f"peer '{peer}' is not registered -- call register_peer first"}
    if not confirm_destructive:
        return {"ok": False,
                "error": "confirm_destructive=True is required for a two-node network run"}
    # Start the responder on the peer over the secured SSH channel (best effort).
    responder_started = False
    if _have("ssh"):
        cmd = "nohup iperf3 -s -D >/dev/null 2>&1 || true"
        if "roce" in tests:
            cmd += "; nohup ib_write_bw >/dev/null 2>&1 &"
        rc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             f"{p['ssh_user']}@{peer}", cmd], capture_output=True, text=True)
        responder_started = rc.returncode == 0
    if not _bash():
        return {"ok": False, "error": "bash is required to launch a run"}
    rid = launch_run("acceptance", [], peer=peer, net=",".join(tests), iface=local_iface)
    return {"ok": True, "run_id": rid, "peer": peer, "tests": tests,
            "responder_started": responder_started,
            "message": "two-node run started; poll get_run_status(run_id)"}


# --------------------------------------------------------------------------- #
#  Report (merge agent narrative -> render)                                   #
# --------------------------------------------------------------------------- #
def generate_report_impl(run_id: str, report_title: str = "", prepared_by: str = "",
                         executive_summary: Optional[List[str]] = None,
                         findings: Optional[List[str]] = None,
                         recommendations: Optional[List[str]] = None) -> Dict[str, Any]:
    rd = os.path.join(RUNS_DIR, run_id)
    results = _load_json(os.path.join(rd, "results.json"))
    if results is None:
        return {"ok": False, "error": f"no results.json for run {run_id}"}
    meta = results.setdefault("meta", {})
    if report_title:
        meta["report_title"] = report_title
    if prepared_by:
        meta["prepared_by"] = prepared_by
    if executive_summary is not None:
        results["executive_summary"] = executive_summary
    if findings is not None:
        results["findings"] = findings
    if recommendations is not None:
        results["recommendations"] = recommendations
    _save_json(os.path.join(rd, "results.json"), results)
    out = os.path.join(rd, "report.pdf")
    _report.build_report(results, out)
    return {"ok": True, "run_id": run_id, "report_path": out}


# =========================================================================== #
#  MCP TOOL SURFACE -- thin wrappers with agent-facing docstrings             #
# =========================================================================== #
mcp = FastMCP("CoreBench")


@mcp.tool()
def list_block_devices() -> str:
    """List all block devices (NVMe/SATA) with size, model, mount state, and a
    `safe_to_test` flag. READ-ONLY. ALWAYS CALL THIS FIRST so you (and the human)
    can choose an explicit, unmounted, non-root drive allow-list before any
    destructive step. Never pass 'all'."""
    return _ok(devices=list_block_devices_impl())


@mcp.tool()
def server_hardware_summary() -> str:
    """Run the hardware collector and return the parsed inventory (CPU, memory,
    BIOS, BMC, kernel, storage, NIC). READ-ONLY. Missing tools degrade to 'n/a'."""
    return json.dumps(server_hardware_summary_impl())


@mcp.tool()
def telemetry_snapshot() -> str:
    """One-shot telemetry: wall watts (BMC), inlet/outlet temp, fan RPM, average
    core MHz. READ-ONLY. Fields are null where the BMC/sensors are unavailable."""
    return json.dumps(telemetry_snapshot_impl())


@mcp.tool()
def list_runs() -> str:
    """List recent validation runs with run_id, status, and whether a report
    exists. READ-ONLY."""
    return _ok(runs=list_runs_impl())


@mcp.tool()
def get_run_status(run_id: str) -> str:
    """Status of a run: running/done/failed, elapsed seconds, drives, tier, the
    last 15 log lines, and the report path if done. READ-ONLY. Poll this after
    start_validation until status is 'done'."""
    return json.dumps(get_run_status_impl(run_id))


@mcp.tool()
def get_results(run_id: str) -> str:
    """Return the raw results.json for a run so you can read the numbers and draft
    the report narrative. READ-ONLY."""
    return json.dumps(get_results_impl(run_id))


@mcp.tool()
def start_validation(drives: List[str], tier: str, confirm_destructive: bool = False) -> str:
    """Start a full validation run. DESTRUCTIVE + ASYNC: this ERASES the listed
    drives (raw fio writes / SNIA preconditioning). Returns a run_id immediately;
    poll get_run_status(run_id).

    Guardrails (refused): empty list or 'all'; confirm_destructive=False; a
    mounted device; the root device; a nonexistent device. You must pass an
    EXPLICIT allow-list the human approved. `tier` is acceptance | qualification
    | deep-dive."""
    return json.dumps(start_validation_impl(drives, tier, confirm_destructive))


@mcp.tool()
def capture_platform_settings() -> str:
    """Snapshot current BIOS/BMC/kernel performance settings and return a
    snapshot_id (a one-call rollback point via restore_platform_settings).
    READ-ONLY. Taken automatically before any change, and used for the
    BIOS-sensitivity report."""
    return json.dumps(capture_platform_settings_impl())


@mcp.tool()
def apply_platform_settings(settings: Dict[str, Any], confirm: bool = False) -> str:
    """Apply reversible performance settings (e.g. cpu_governor, pcie_aspm,
    c_states, numa_nps, power_profile, m2_bifurcation). Tier-C: auto-snapshots
    first (returns rollback_snapshot_id), logs every before/after change, and
    reports which settings need a reboot. Requires confirm=True."""
    return json.dumps(apply_platform_settings_impl(settings, confirm))


@mcp.tool()
def restore_platform_settings(snapshot_id: str, confirm: bool = False) -> str:
    """Roll the platform back to a captured snapshot. Tier-C. Requires confirm=True."""
    return json.dumps(restore_platform_settings_impl(snapshot_id, confirm))


@mcp.tool()
def flash_firmware(component: str, image_path: str, sha256: str,
                   confirm_destructive: bool = False,
                   i_understand_brick_risk: bool = False) -> str:
    """Flash BIOS/BMC/NIC/SSD firmware. TIER-D, HIGH-RISK / IRREVERSIBLE. Off
    unless enable_firmware_tools:true in config. Verifies the image SHA-256 and
    requires BOTH confirm_destructive=True AND i_understand_brick_risk=True. A bad
    flash can BRICK the component; ensure power is stable."""
    return json.dumps(flash_firmware_impl(component, image_path, sha256,
                                          confirm_destructive, i_understand_brick_risk))


@mcp.tool()
def register_peer(host: str, ssh_user: str, role: str = "responder") -> str:
    """Register a partner server (reachable by SSH key on a trusted network) as a
    load/responder node for two-node tests. Records reachability. The peer is a
    responder only -- never wiped/reconfigured without its own confirmation."""
    return json.dumps(register_peer_impl(host, ssh_user, role))


@mcp.tool()
def start_network_validation(local_iface: str, peer: str, tests: List[str],
                             confirm_destructive: bool = False) -> str:
    """Coordinate a two-node NIC test (tcp, roce, latency). DESTRUCTIVE + ASYNC.
    Pushes a responder to the registered peer over the secured SSH channel, runs
    the initiator locally, and collects both sides. Requires the peer be
    registered and confirm_destructive=True. Returns a run_id; poll
    get_run_status."""
    return json.dumps(start_network_validation_impl(local_iface, peer, tests, confirm_destructive))


@mcp.tool()
def generate_report(run_id: str, report_title: str = "", prepared_by: str = "",
                    executive_summary: Optional[List[str]] = None,
                    findings: Optional[List[str]] = None,
                    recommendations: Optional[List[str]] = None) -> str:
    """Merge your drafted narrative (title, prepared_by, executive_summary,
    findings, recommendations) into the run's results.json and render the branded
    PDF. Returns the report path. Leave narrative empty to keep what assemble
    already produced."""
    return json.dumps(generate_report_impl(run_id, report_title, prepared_by,
                                           executive_summary, findings, recommendations))


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
