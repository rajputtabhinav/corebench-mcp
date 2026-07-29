#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bench_memory.sh <run_dir> <tier>  ->  bench_memory.json
# STREAM (bandwidth) and Intel MLC (loaded latency + peak BW) where available.
# Non-destructive. Degrades to null/"n/a". Treats output as data only.
#
# STREAM binary: $CB_STREAM (default: looks for 'stream' on PATH).
# MLC binary:    $CB_MLC    (default: looks for 'mlc' on PATH).
# ---------------------------------------------------------------------------
set -uo pipefail
RUN_DIR="${1:-}"; TIER="${2:-acceptance}"
[ -n "$RUN_DIR" ] || { echo "usage: bench_memory.sh <run_dir> <tier>" >&2; exit 2; }
mkdir -p "$RUN_DIR"; OUT="$RUN_DIR/bench_memory.json"

have() { command -v "$1" >/dev/null 2>&1; }
log()  { printf '%s\n' "$*" >&2; }
Jn()   { case "${1:-}" in ''|n/a) printf 'null';; *) printf '%s' "$1" | grep -qE '^-?[0-9]+(\.[0-9]+)?$' && printf '%s' "$1" || printf 'null';; esac; }
Js()   { printf '"%s"' "${1:-n/a}"; }

triad="n/a"; copy="n/a"; mlc_lat="n/a"; mlc_bw="n/a"; per_socket="n/a"
dimm_mts="n/a"; rated_mts="n/a"

# STREAM (MB/s in its output -> GB/s)
STREAM_BIN="${CB_STREAM:-$(command -v stream 2>/dev/null)}"
if [ -n "$STREAM_BIN" ] && [ -x "$STREAM_BIN" ]; then
  log "  STREAM: $STREAM_BIN"
  so="$("$STREAM_BIN" 2>/dev/null)" || true
  copy="$(printf '%s' "$so" | awk '/^Copy:/{printf "%.0f", $2/1000}')"
  triad="$(printf '%s' "$so" | awk '/^Triad:/{printf "%.0f", $2/1000}')"
fi

# Intel MLC
MLC_BIN="${CB_MLC:-$(command -v mlc 2>/dev/null)}"
if [ -n "$MLC_BIN" ] && [ -x "$MLC_BIN" ]; then
  log "  MLC: $MLC_BIN --loaded_latency"
  ml="$("$MLC_BIN" --loaded_latency 2>/dev/null)" || true
  # last data row: "<inject> <latency-ns> <bandwidth-MB/s>"
  read -r mlc_lat mlc_bw < <(printf '%s' "$ml" | awk '/^[ \t]*[0-9]+[ \t]+[0-9.]+[ \t]+[0-9.]+/{lat=$2; bw=$3} END{if(bw!="")printf "%.0f %.0f", lat, bw/1000}')
  [ -n "${mlc_lat:-}" ] || mlc_lat="n/a"; [ -n "${mlc_bw:-}" ] || mlc_bw="n/a"
fi

# configured vs rated DIMM speed (dmidecode, needs root)
if have dmidecode && [ "$(id -u 2>/dev/null || echo 1000)" = "0" ]; then
  dm="$(dmidecode -t memory 2>/dev/null)"
  dimm_mts="$(printf '%s' "$dm" | awk -F: '/Configured Memory Speed:/{gsub(/[^0-9]/,"",$2); if($2!=""){print $2; exit}}')"
  rated_mts="$(printf '%s' "$dm" | awk -F: '/^\tSpeed:/{gsub(/[^0-9]/,"",$2); if($2!=""){print $2; exit}}')"
  [ -n "$dimm_mts" ] || dimm_mts="n/a"; [ -n "$rated_mts" ] || rated_mts="n/a"
fi

{
printf '{'
printf '"stream_triad_gbs":%s,'      "$(Jn "$triad")"
printf '"stream_copy_gbs":%s,'       "$(Jn "$copy")"
printf '"mlc_loaded_latency_ns":%s,' "$(Jn "$mlc_lat")"
printf '"mlc_peak_bw_gbs":%s,'       "$(Jn "$mlc_bw")"
printf '"per_socket_gbs":%s,'        "$(Jn "$per_socket")"
printf '"dimm_speed_mts":%s,'        "$(Jn "$dimm_mts")"
printf '"rated_mts":%s'              "$(Jn "$rated_mts")"
printf '}\n'
} > "$OUT"
log "bench_memory: wrote $OUT"
