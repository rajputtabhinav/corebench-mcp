"""Tests for the MCP tool surface + safety model (engine logic, no live hardware)."""

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mcp_server as M  # noqa: E402


# --------------------------------------------------------------------------- #
#  §14c -- read-only tools return valid JSON; server starts over stdio        #
# --------------------------------------------------------------------------- #
def test_readonly_tools_return_valid_json():
    for fn in (M.list_block_devices, M.telemetry_snapshot, M.list_runs):
        d = json.loads(fn())
        assert d["ok"] is True
    # hardware summary shells out to the collector; still valid JSON
    d = json.loads(M.server_hardware_summary())
    assert "ok" in d


def test_server_starts_over_stdio():
    proc = subprocess.Popen([sys.executable, M.__file__],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        time.sleep(2.0)
        assert proc.poll() is None, "server exited immediately (startup error)"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# --------------------------------------------------------------------------- #
#  §14d -- start_validation / drive-allow-list refusals                       #
# --------------------------------------------------------------------------- #
DEVS = [
    {"path": "/dev/nvme9n1", "mounted": False, "holds_root": False, "mountpoint": None, "safe_to_test": True},
    {"path": "/dev/sda", "mounted": True, "holds_root": False, "mountpoint": "/data"},
    {"path": "/dev/sdb", "mounted": False, "holds_root": True, "mountpoint": "/"},
]


def test_refuse_empty():
    r = M.validate_drives([], True, DEVS)
    assert r["ok"] is False and "empty" in r["error"].lower()


def test_refuse_all():
    r = M.validate_drives(["all"], True, DEVS)
    assert r["ok"] is False and "all" in r["error"].lower()


def test_refuse_without_confirm():
    r = M.validate_drives(["/dev/nvme9n1"], False, DEVS)
    assert r["ok"] is False and "confirm" in r["error"].lower()


def test_refuse_mounted():
    r = M.validate_drives(["/dev/sda"], True, DEVS)
    assert r["ok"] is False and "mounted" in r["error"].lower()


def test_refuse_root():
    r = M.validate_drives(["/dev/sdb"], True, DEVS)
    assert r["ok"] is False and "root" in r["error"].lower()


def test_refuse_nonexistent():
    r = M.validate_drives(["/dev/nvmeX9"], True, DEVS)
    assert r["ok"] is False and "not found" in r["error"].lower()


def test_accept_valid_drive():
    r = M.validate_drives(["/dev/nvme9n1"], True, DEVS)
    assert r["ok"] is True and r["drives"] == ["/dev/nvme9n1"]


def test_start_validation_unknown_tier():
    r = M.start_validation_impl(["/dev/nvme9n1"], "bogus", True)
    assert r["ok"] is False and "tier" in r["error"].lower()


def test_start_validation_refuses_bad_drives_without_spawning(monkeypatch):
    spawned = []
    monkeypatch.setattr(M, "launch_run", lambda *a, **k: spawned.append(a) or "x")
    r = M.start_validation_impl([], "acceptance", True)
    assert r["ok"] is False
    assert spawned == [], "must not launch a run when validation fails"


# --------------------------------------------------------------------------- #
#  §14e -- platform settings capture/apply/restore round-trip                 #
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(M, "SNAP_DIR", str(tmp_path / "snaps"))
    return tmp_path


def test_apply_requires_confirm(isolated_state):
    r = M.apply_platform_settings_impl({"cpu_governor": "performance"}, confirm=False)
    assert r["ok"] is False and "confirm" in r["error"].lower()


def test_platform_roundtrip(isolated_state):
    cap0 = M.capture_platform_settings_impl()
    assert cap0["ok"] and cap0["snapshot_id"]
    snap0 = cap0["snapshot_id"]
    gov0 = cap0["settings"]["cpu_governor"]

    ap = M.apply_platform_settings_impl(
        {"cpu_governor": "performance", "c_states": "disabled"}, confirm=True)
    assert ap["ok"] is True
    assert ap["rollback_snapshot_id"]                       # auto-snapshot taken
    assert "c_states" in ap["reboot_required"]              # BIOS key -> reboot
    assert "cpu_governor" in ap["applied"]
    assert any(e["key"] == "cpu_governor" and "from" in e and "to" in e for e in ap["log"])

    cap1 = M.capture_platform_settings_impl()               # re-read reflects changes
    assert cap1["settings"]["cpu_governor"] == "performance"
    assert cap1["settings"]["c_states"] == "disabled"

    rb = M.restore_platform_settings_impl(snap0, confirm=True)
    assert rb["ok"] is True
    cap2 = M.capture_platform_settings_impl()               # back to original
    assert cap2["settings"]["cpu_governor"] == gov0
    assert cap2["settings"]["c_states"] == cap0["settings"]["c_states"]


def test_restore_requires_confirm(isolated_state):
    cap = M.capture_platform_settings_impl()
    r = M.restore_platform_settings_impl(cap["snapshot_id"], confirm=False)
    assert r["ok"] is False and "confirm" in r["error"].lower()


# --------------------------------------------------------------------------- #
#  §14f -- firmware gates (dummy image; never flashes anything)               #
# --------------------------------------------------------------------------- #
def test_firmware_disabled_by_default(tmp_path):
    img = tmp_path / "fw.bin"
    img.write_bytes(b"dummy")
    sha = M._sha256_file(str(img))
    r = M.flash_firmware_impl("bmc", str(img), sha, True, True, config={})
    assert r["ok"] is False and "disabl" in r["error"].lower()


def test_firmware_checksum_mismatch(tmp_path):
    img = tmp_path / "fw.bin"
    img.write_bytes(b"dummy")
    r = M.flash_firmware_impl("bmc", str(img), "deadbeef" * 8, True, True,
                              config={"enable_firmware_tools": True})
    assert r["ok"] is False and "mismatch" in r["error"].lower()


def test_firmware_requires_both_flags(tmp_path):
    img = tmp_path / "fw.bin"
    img.write_bytes(b"dummy")
    sha = M._sha256_file(str(img))
    cfg = {"enable_firmware_tools": True}
    assert M.flash_firmware_impl("bmc", str(img), sha, True, False, config=cfg)["ok"] is False
    assert M.flash_firmware_impl("bmc", str(img), sha, False, True, config=cfg)["ok"] is False
    ok = M.flash_firmware_impl("bmc", str(img), sha, True, True, config=cfg)
    assert ok["ok"] is True and ok["sha256"] == sha


def test_firmware_missing_image(tmp_path):
    r = M.flash_firmware_impl("bmc", str(tmp_path / "nope.bin"), "x", True, True,
                              config={"enable_firmware_tools": True})
    assert r["ok"] is False and "not found" in r["error"].lower()


# --------------------------------------------------------------------------- #
#  §14g -- network validation gating                                          #
# --------------------------------------------------------------------------- #
def test_network_refuses_unregistered_peer(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "PEERS_FILE", str(tmp_path / "peers.json"))
    r = M.start_network_validation_impl("eth0", "10.0.0.250", ["tcp"], confirm_destructive=True)
    assert r["ok"] is False and "register" in r["error"].lower()


def test_network_requires_confirm(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "PEERS_FILE", str(tmp_path / "peers.json"))
    M._save_json(M.PEERS_FILE, [{"host": "10.0.0.38", "ssh_user": "lab", "role": "responder"}])
    r = M.start_network_validation_impl("eth0", "10.0.0.38", ["tcp"], confirm_destructive=False)
    assert r["ok"] is False and "confirm" in r["error"].lower()


# --------------------------------------------------------------------------- #
#  §14h -- async run lifecycle (demo): run_id immediately, running -> done     #
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required to launch run.sh")
def test_async_run_lifecycle_demo():
    res = M.start_validation_impl([], "acceptance", demo=True)
    assert res["ok"] is True and res["run_id"]
    rid = res["run_id"]
    rd = os.path.join(M.RUNS_DIR, rid)
    try:
        status = "starting"
        for _ in range(120):                # up to ~60s
            st = M.get_run_status_impl(rid)
            assert st["ok"] is True
            status = st["status"]
            if status in ("done", "failed"):
                break
            time.sleep(0.5)
        assert status == "done", f"run ended as {status}"
        assert os.path.isfile(os.path.join(rd, "results.json"))
        assert os.path.isfile(os.path.join(rd, "report.pdf"))
        assert M.get_run_status_impl(rid)["has_report"] is True
        assert M.get_results_impl(rid)["ok"] is True
    finally:
        shutil.rmtree(rd, ignore_errors=True)
