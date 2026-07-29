#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run.sh -- orchestrate one validation campaign end to end:
#   SEL/dmesg snapshot -> collect hardware -> telemetry(start) -> benchmarks
#   (with phase markers) -> telemetry(stop) -> SEL/dmesg diff -> assemble
#   -> render branded PDF.
#
#   run.sh <tier> [<dev> ...] [options]
#     tier            acceptance | qualification | deep-dive
#     <dev>           EXPLICIT block device(s) for the storage tier (never "all")
#   options:
#     --peer HOST     run two-node network tests against HOST
#     --net TESTS     network tests csv (tcp,roce,latency)   [tcp]
#     --config PATH   config.json                            [./config.json]
#     --prepared-by X / --title X / --window X / --server-ip X
#     --demo          synthesize fragments from the sample fixture (no hardware)
#
#   Writes everything to runs/<run_id>/ ; the run_id is printed on stdout.
#   Designed to be launched in the background by the MCP server and polled via
#   get_run_status (which tails runs/<run_id>/run.log and reads .../status).
#
# Destructive storage steps require CONFIRM_DESTRUCTIVE=1 (the MCP Tier-B gate
# sets this only after explicit human confirmation). Nothing read is executed.
# ---------------------------------------------------------------------------
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${CB_PYTHON:-}"; [ -x "$PY" ] || PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY="$HERE/.venv/Scripts/python.exe"; [ -x "$PY" ] || PY="$(command -v python3 || command -v python || echo python3)"

TIER="${1:-acceptance}"; shift || true
DRIVES=(); PEER=""; NET="tcp"; CONFIG="$HERE/config.json"
TITLE=""; PREP=""; WINDOW=""; SERVER_IP=""; DEMO=0; RUN_ID_ARG=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --peer) PEER="${2:-}"; shift 2;;
    --net) NET="${2:-tcp}"; shift 2;;
    --config) CONFIG="${2:-}"; shift 2;;
    --prepared-by) PREP="${2:-}"; shift 2;;
    --title) TITLE="${2:-}"; shift 2;;
    --window) WINDOW="${2:-}"; shift 2;;
    --server-ip) SERVER_IP="${2:-}"; shift 2;;
    --run-id) RUN_ID_ARG="${2:-}"; shift 2;;
    --demo) DEMO=1; shift;;
    --) shift; break;;
    -*) echo "unknown option $1" >&2; exit 2;;
    *) DRIVES+=("$1"); shift;;
  esac
done

RUN_ID="${RUN_ID_ARG:-$(date +%Y%m%d-%H%M%S)-$$}"
RUN_DIR="$HERE/runs/$RUN_ID"
mkdir -p "$RUN_DIR"
echo "$RUN_ID"                       # <- stdout: the run_id, for the caller
exec >>"$RUN_DIR/run.log" 2>&1       # everything else goes to the run log

echo "running" > "$RUN_DIR/status"
finish() { rc=$?; if [ "$rc" -eq 0 ]; then echo done; else echo failed; fi > "$RUN_DIR/status"; }
trap finish EXIT

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
COLL="$HERE/collectors"; BENCH="$HERE/benchmarks"; WINDOW_START="$(date +%Y-%m-%d\ %H:%M)"

# record run metadata
{ printf '{"run_id":"%s","tier":"%s","demo":%s,"drives":[' "$RUN_ID" "$TIER" "$DEMO"
  for i in "${!DRIVES[@]}"; do [ "$i" -gt 0 ] && printf ','; printf '"%s"' "${DRIVES[$i]}"; done
  printf '],"peer":"%s","started":"%s"}\n' "$PEER" "$WINDOW_START"; } > "$RUN_DIR/run_meta.json"

log "CoreBench run $RUN_ID  tier=$TIER demo=$DEMO drives=${DRIVES[*]:-none} peer=${PEER:-none}"

idle_secs="${CB_IDLE_SECS:-3}"; cool_secs="${CB_COOL_SECS:-3}"

if [ "$DEMO" = "1" ]; then
  # -------- demo: synthesize fragments, skip real collection/benchmarks ------
  log "DEMO mode: synthesizing fragments from sample fixture"
  "$PY" "$HERE/engine/demo_data.py" "$RUN_DIR" || { log "demo_data failed"; exit 5; }
else
  # -------- SEL + dmesg: snapshot, then clear SEL so the diff shows new --------
  if command -v ipmitool >/dev/null 2>&1; then
    log "snapshot BMC SEL (preserve, then clear)"
    ipmitool sel list  > "$RUN_DIR/sel_before.txt" 2>/dev/null || : > "$RUN_DIR/sel_before.txt"
    [ "${CB_SEL_CLEAR:-1}" = "1" ] && ipmitool sel clear >/dev/null 2>&1 || true
  else : > "$RUN_DIR/sel_before.txt"; fi
  { dmesg 2>/dev/null || journalctl -k --no-pager 2>/dev/null; } > "$RUN_DIR/dmesg_before.txt" || : > "$RUN_DIR/dmesg_before.txt"

  # -------- hardware inventory ----------------------------------------------
  log "collect hardware"
  bash "$COLL/collect_hardware.sh" "$RUN_DIR/hardware.json" || log "hardware collector returned non-zero"

  # -------- telemetry start + idle baseline ---------------------------------
  log "telemetry start"
  bash "$COLL/collect_telemetry.sh" start "$RUN_DIR"
  bash "$COLL/collect_telemetry.sh" phase "$RUN_DIR" "idle baseline"
  sleep "$idle_secs"

  # -------- storage ----------------------------------------------------------
  if [ "${#DRIVES[@]}" -gt 0 ]; then
    log "storage benchmarks (tier=$TIER)"
    bash "$COLL/collect_telemetry.sh" phase "$RUN_DIR" "storage load"
    CONFIRM_DESTRUCTIVE="${CONFIRM_DESTRUCTIVE:-0}" bash "$BENCH/bench_storage.sh" "$RUN_DIR" "$TIER" "${DRIVES[@]}" \
      || log "bench_storage returned non-zero"
  else
    log "no drives specified -- skipping storage tier"
  fi

  # -------- compute + memory -------------------------------------------------
  log "compute + memory + GPU benchmarks"
  bash "$COLL/collect_telemetry.sh" phase "$RUN_DIR" "compute load"
  bash "$BENCH/bench_cpu.sh" "$RUN_DIR" "$TIER"    || log "bench_cpu non-zero"
  bash "$BENCH/bench_memory.sh" "$RUN_DIR" "$TIER" || log "bench_memory non-zero"
  bash "$COLL/collect_telemetry.sh" phase "$RUN_DIR" "GPU load"
  bash "$BENCH/bench_gpu.sh" "$RUN_DIR" "$TIER"    || log "bench_gpu non-zero"

  # -------- combined max-load (peak power + inlet temp happen here) ----------
  if [ "$TIER" != "acceptance" ]; then
    log "combined max-load phase"
    bash "$COLL/collect_telemetry.sh" phase "$RUN_DIR" "combined load"
    combined="${CB_COMBINED_SECS:-20}"
    pids=()
    command -v stress-ng >/dev/null 2>&1 && { stress-ng --cpu 0 -t "${combined}s" >/dev/null 2>&1 & pids+=($!); }
    if command -v fio >/dev/null 2>&1 && [ "${#DRIVES[@]}" -gt 0 ]; then
      fio --name=combined --filename="${DRIVES[0]}" --ioengine=libaio --direct=1 --rw=randread \
        --bs=4k --iodepth=64 --numjobs=2 --time_based --runtime="$combined" >/dev/null 2>&1 & pids+=($!)
    fi
    for p in "${pids[@]:-}"; do [ -n "$p" ] && wait "$p" 2>/dev/null || true; done
  fi

  # -------- network (two-node) ----------------------------------------------
  if [ -n "$PEER" ]; then
    log "network benchmarks -> $PEER ($NET)"
    bash "$COLL/collect_telemetry.sh" phase "$RUN_DIR" "network load"
    bash "$BENCH/bench_network.sh" "$RUN_DIR" "$PEER" "$NET" || log "bench_network non-zero"
  fi

  # -------- cooldown + telemetry stop ---------------------------------------
  bash "$COLL/collect_telemetry.sh" phase "$RUN_DIR" "cooldown"
  sleep "$cool_secs"
  log "telemetry stop"
  bash "$COLL/collect_telemetry.sh" stop "$RUN_DIR"

  # -------- SEL + dmesg after ------------------------------------------------
  command -v ipmitool >/dev/null 2>&1 && ipmitool sel list > "$RUN_DIR/sel_after.txt" 2>/dev/null || : > "$RUN_DIR/sel_after.txt"
  { dmesg 2>/dev/null || journalctl -k --no-pager 2>/dev/null; } > "$RUN_DIR/dmesg_after.txt" || : > "$RUN_DIR/dmesg_after.txt"
fi

# -------- assemble results.json -------------------------------------------
WINDOW_END="$(date +%Y-%m-%d\ %H:%M)"
[ -n "$WINDOW" ] || WINDOW="$WINDOW_START -> $WINDOW_END"
log "assemble results.json"
ASM_ARGS=( "$RUN_DIR" --config "$CONFIG" --tier "$TIER" --window "$WINDOW" --date "$(date +%Y-%m-%d)" )
[ -n "$PREP" ] && ASM_ARGS+=( --prepared-by "$PREP" )
[ -n "$SERVER_IP" ] && ASM_ARGS+=( --server-ip "$SERVER_IP" )
"$PY" "$HERE/engine/assemble.py" "${ASM_ARGS[@]}" || { log "assemble failed"; exit 6; }

# apply optional title override
if [ -n "$TITLE" ]; then
  "$PY" - "$RUN_DIR/results.json" "$TITLE" <<'PYEOF' || true
import json,sys
p=sys.argv[1]; d=json.load(open(p)); d.setdefault("meta",{})["report_title"]=sys.argv[2]
json.dump(d,open(p,"w"),indent=2)
PYEOF
fi

# -------- render report.pdf -----------------------------------------------
log "render report.pdf"
"$PY" "$HERE/engine/generate_report.py" "$RUN_DIR/results.json" "$RUN_DIR/report.pdf" || { log "render failed"; exit 7; }

STATUS="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['meta'].get('status','n/a'))" "$RUN_DIR/results.json" 2>/dev/null || echo n/a)"
log "DONE  status=$STATUS  report=$RUN_DIR/report.pdf"
echo "$RUN_DIR/report.pdf" > "$RUN_DIR/report_path.txt"
