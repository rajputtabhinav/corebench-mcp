#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bench_cpu.sh <run_dir> <tier>  ->  bench_cpu.json
# stress-ng (bogo ops + heat) and HPL (peak FLOPS) where available. Non-
# destructive. Every tool degrades to null/"n/a". Treats output as data only.
#
# HPL: runs ./xhpl if both xhpl and an HPL.dat are present in $CB_HPL_DIR
# (default: run_dir). Set CB_HPL_PEAK_TFLOPS for an efficiency %.
# ---------------------------------------------------------------------------
set -uo pipefail
RUN_DIR="${1:-}"; TIER="${2:-acceptance}"
[ -n "$RUN_DIR" ] || { echo "usage: bench_cpu.sh <run_dir> <tier>" >&2; exit 2; }
mkdir -p "$RUN_DIR"; OUT="$RUN_DIR/bench_cpu.json"

have() { command -v "$1" >/dev/null 2>&1; }
log()  { printf '%s\n' "$*" >&2; }
Jn()   { case "${1:-}" in ''|n/a) printf 'null';; *) printf '%s' "$1" | grep -qE '^-?[0-9]+(\.[0-9]+)?$' && printf '%s' "$1" || printf 'null';; esac; }
Js()   { printf '"%s"' "${1:-n/a}"; }

case "$TIER" in deep-dive) DUR="${CB_CPU_SECS:-120}"; SOAK_H="${CB_SOAK_HOURS:-24}";; qualification) DUR="${CB_CPU_SECS:-60}"; SOAK_H=0;; *) DUR="${CB_CPU_SECS:-30}"; SOAK_H=0;; esac

# ISA flags
avx512="n/a"; amx="n/a"
if [ -r /proc/cpuinfo ]; then
  flags="$(awk -F: '/^flags|^Features/{print $2; exit}' /proc/cpuinfo)"
  case " $flags " in *" avx512f "*) avx512="yes";; *) avx512="no";; esac
  case " $flags " in *" amx_tile "*) amx="yes";; *) amx="no";; esac
fi

# stress-ng bogo ops (also produces the heat for the telemetry overlay)
bogo="n/a"
if have stress-ng; then
  log "  stress-ng --cpu 0 for ${DUR}s"
  so="$(stress-ng --cpu 0 --cpu-method all --metrics-brief -t "${DUR}s" 2>&1)" || true
  bogo="$(printf '%s' "$so" | awk '/cpu/ && /bogo ops/ {next} /^stress-ng: metrc.*cpu/ {print $(NF-4)}' | tail -1)"
  [ -n "$bogo" ] || bogo="$(printf '%s' "$so" | grep -iE 'cpu .*[0-9]+' | awk '{print $5}' | tail -1)"
  [ -n "$bogo" ] || bogo="n/a"
fi

# HPL (optional; needs xhpl + HPL.dat)
hpl_tflops="n/a"; hpl_eff="n/a"
HPL_DIR="${CB_HPL_DIR:-$RUN_DIR}"
if have "$HPL_DIR/xhpl" 2>/dev/null || { [ -x "$HPL_DIR/xhpl" ] && [ -r "$HPL_DIR/HPL.dat" ]; }; then
  log "  HPL: running xhpl"
  ( cd "$HPL_DIR" && ./xhpl > "$RUN_DIR/hpl.out" 2>&1 ) || true
  gf="$(awk '/WR/{v=$(NF)} END{print v}' "$RUN_DIR/hpl.out" 2>/dev/null)"
  [ -n "$gf" ] && hpl_tflops="$(awk -v g="$gf" 'BEGIN{printf "%.3f", g/1000}')"
  if [ "$hpl_tflops" != "n/a" ] && [ -n "${CB_HPL_PEAK_TFLOPS:-}" ]; then
    hpl_eff="$(awk -v a="$hpl_tflops" -v p="$CB_HPL_PEAK_TFLOPS" 'BEGIN{if(p>0)printf "%.0f", a/p*100}')"
  fi
fi

# throttle events from thermal_throttle counters (best effort)
throttle="n/a"
if ls /sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count >/dev/null 2>&1; then
  throttle="$(awk '{s+=$1} END{print s+0}' /sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count 2>/dev/null)"
fi

{
printf '{'
printf '"hpl_tflops":%s,'             "$(Jn "$hpl_tflops")"
printf '"hpl_efficiency_pct":%s,'     "$(Jn "$hpl_eff")"
printf '"stress_ng_cpu_bogo_ops":%s,' "$(Jn "$bogo")"
printf '"avx512":%s,'                 "$(Js "$avx512")"
printf '"amx":%s,'                    "$(Js "$amx")"
printf '"throttle_events":%s,'        "$(Jn "$throttle")"
printf '"soak_hours":%s'              "$(Jn "$SOAK_H")"
printf '}\n'
} > "$OUT"
log "bench_cpu: wrote $OUT"
