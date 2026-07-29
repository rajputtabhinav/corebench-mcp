#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# collect_telemetry.sh -- 1 Hz power/thermal/clock timeline on a shared clock.
#
#   collect_telemetry.sh start <run_dir>
#   collect_telemetry.sh phase <run_dir> "<label>"     # write a phase marker
#   collect_telemetry.sh stop  <run_dir> [out.json]    # stop + convert to JSON
#
# Each sample row: t(s), wall_w, pkg_w, inlet_c, outlet_c, fan_rpm, core_mhz.
# Sources (each degrades to empty -> JSON null):
#   wall_w   : ipmitool dcmi power reading  (polled OVER LAN if CB_IPMI_HOST set,
#              so heavy CPU load can't starve the collector; else in-band)
#   pkg_w    : RAPL energy_uj delta (intel-rapl / amd_energy)
#   inlet/outlet/fan : ipmitool sdr (temperature/fan)
#   core_mhz : mean of scaling_cur_freq across CPUs
#
# BMC SDR is sometimes cached 5-10 s; prefer Redfish where available and treat
# the achieved cadence as best-effort (the "sample" flag reflects whether power
# samples were actually obtained). Never executes collected output.
# ---------------------------------------------------------------------------
set -uo pipefail
shopt -s nullglob

CMD="${1:-}"
RUN_DIR="${2:-}"

have() { command -v "$1" >/dev/null 2>&1; }

# IPMI transport: poll the BMC over LAN when creds are provided (-E reads the
# password from $IPMI_PASSWORD so it never appears in the process list).
IPMI_ARGS=""
if [ -n "${CB_IPMI_HOST:-}" ]; then
  export IPMI_PASSWORD="${CB_IPMI_PASS:-}"
  IPMI_ARGS="-I lanplus -H ${CB_IPMI_HOST} -U ${CB_IPMI_USER:-ADMIN} -E"
fi

jesc() { local s; s="$(cat)"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//$'\t'/\\t}"; s="${s//$'\r'/}"; s="${s//$'\n'/\\n}"; printf '%s' "$s"; }
J() { printf '"%s"' "$(printf '%s' "${1:-}" | jesc)"; }

# ----- per-sample probes ---------------------------------------------------
sample_wall() {
  have ipmitool || { printf ''; return; }
  ipmitool $IPMI_ARGS dcmi power reading 2>/dev/null \
    | awk '/Instantaneous/{print $(NF-1); exit}'
}

PREV_E=""; PREV_T=""
sample_pkg() {
  local e=0 found=0 f
  for f in /sys/class/powercap/intel-rapl:*/energy_uj /sys/class/powercap/amd_energy*/energy_uj; do
    [ -r "$f" ] || continue; found=1
    e=$(( e + $(cat "$f" 2>/dev/null || echo 0) ))
  done
  [ "$found" = 1 ] || { printf ''; return; }
  local now; now="$(date +%s.%N)"
  if [ -n "$PREV_E" ] && [ -n "$PREV_T" ]; then
    awk -v de="$((e - PREV_E))" -v a="$now" -v b="$PREV_T" \
      'BEGIN{dt=a-b; if(dt>0) printf "%.1f", de/1e6/dt}'
  fi
  PREV_E="$e"; PREV_T="$now"
}

sample_thermal() {   # -> "inlet outlet fan"
  local inlet="" outlet="" fan="" sdr=""
  if have ipmitool; then sdr="$(ipmitool $IPMI_ARGS sdr 2>/dev/null)"; fi
  if [ -n "$sdr" ]; then
    inlet="$(printf '%s'  "$sdr" | awk -F'|' 'tolower($1)~/inlet|ambient/{gsub(/[^0-9.]/,"",$2); if($2!=""){print $2; exit}}')"
    outlet="$(printf '%s' "$sdr" | awk -F'|' 'tolower($1)~/outlet|exhaust/{gsub(/[^0-9.]/,"",$2); if($2!=""){print $2; exit}}')"
    fan="$(printf '%s'    "$sdr" | awk -F'|' 'tolower($1)~/fan/{gsub(/[^0-9.]/,"",$2); if($2!=""){print $2; exit}}')"
  fi
  printf '%s %s %s' "$inlet" "$outlet" "$fan"
}

sample_mhz() {
  local sum=0 c=0 f
  for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq; do
    [ -r "$f" ] || continue
    sum=$(( sum + $(cat "$f" 2>/dev/null || echo 0) )); c=$(( c + 1 ))
  done
  [ "$c" -gt 0 ] && awk -v s="$sum" -v c="$c" 'BEGIN{printf "%.0f", s/c/1000}' || printf ''
}

# ----- internal sampler loop (1 Hz) ---------------------------------------
do_sample_loop() {
  local start csv
  start="$(cat "$RUN_DIR/.telemetry.start" 2>/dev/null || date +%s)"
  csv="$RUN_DIR/telemetry.csv"
  while :; do
    local now t wall pkg th inlet outlet fan mhz
    now="$(date +%s)"; t=$(( now - start ))
    wall="$(sample_wall)"; pkg="$(sample_pkg)"
    th="$(sample_thermal)"; inlet="${th%% *}"; rest="${th#* }"; outlet="${rest%% *}"; fan="${rest##* }"
    mhz="$(sample_mhz)"
    printf '%s,%s,%s,%s,%s,%s,%s\n' "$t" "$wall" "$pkg" "$inlet" "$outlet" "$fan" "$mhz" >> "$csv"
    sleep 1
  done
}

# ----- commands ------------------------------------------------------------
case "$CMD" in
  start)
    [ -n "$RUN_DIR" ] || { echo "usage: $0 start <run_dir>" >&2; exit 2; }
    mkdir -p "$RUN_DIR"
    date +%s > "$RUN_DIR/.telemetry.start"
    printf 't,wall_w,pkg_w,inlet_c,outlet_c,fan_rpm,core_mhz\n' > "$RUN_DIR/telemetry.csv"
    : > "$RUN_DIR/telemetry_phases.csv"
    # spawn the sampler detached; record its PID for stop
    nohup bash "$0" __sample "$RUN_DIR" >/dev/null 2>&1 &
    echo $! > "$RUN_DIR/.telemetry.pid"
    echo "telemetry: started (pid $(cat "$RUN_DIR/.telemetry.pid"))" >&2
    ;;

  phase)
    [ -n "$RUN_DIR" ] || { echo "usage: $0 phase <run_dir> <label>" >&2; exit 2; }
    label="${3:-phase}"
    start="$(cat "$RUN_DIR/.telemetry.start" 2>/dev/null || date +%s)"
    t=$(( $(date +%s) - start ))
    printf '%s,%s\n' "$t" "$label" >> "$RUN_DIR/telemetry_phases.csv"
    echo "telemetry: phase '$label' @ ${t}s" >&2
    ;;

  __sample)
    do_sample_loop
    ;;

  stop)
    [ -n "$RUN_DIR" ] || { echo "usage: $0 stop <run_dir> [out.json]" >&2; exit 2; }
    OUT="${3:-$RUN_DIR/telemetry.json}"
    if [ -r "$RUN_DIR/.telemetry.pid" ]; then
      kill "$(cat "$RUN_DIR/.telemetry.pid")" 2>/dev/null || true
      rm -f "$RUN_DIR/.telemetry.pid"
    fi
    # phases JSON
    phase_items=()
    if [ -r "$RUN_DIR/telemetry_phases.csv" ]; then
      while IFS=, read -r pt plabel; do
        [ -n "$pt" ] || continue
        phase_items+=("$(printf '{"t":%s,"label":%s}' "$pt" "$(J "$plabel")")")
      done < "$RUN_DIR/telemetry_phases.csv"
    fi
    phases_json="$(IFS=,; printf '%s' "${phase_items[*]}")"
    # columns -> JSON via awk (numeric or null), and derive the "sample" flag
    awk -F, -v phases="[$phases_json]" '
      function emit(A,   s,i,v){ s="["; for(i=1;i<=n;i++){ if(i>1)s=s","; v=A[i];
        if(v ~ /^-?[0-9]+(\.[0-9]+)?$/) s=s v; else s=s "null" } return s "]" }
      NR==1{next}
      { n++; T[n]=$1; W[n]=$2; P[n]=$3; I[n]=$4; O[n]=$5; F[n]=$6; M[n]=$7;
        if($2 ~ /^-?[0-9.]+$/) wnum++ }
      END{
        printf "{\n"
        printf "  \"t\": %s,\n",        emit(T)
        printf "  \"wall_w\": %s,\n",   emit(W)
        printf "  \"pkg_w\": %s,\n",    emit(P)
        printf "  \"inlet_c\": %s,\n",  emit(I)
        printf "  \"outlet_c\": %s,\n", emit(O)
        printf "  \"fan_rpm\": %s,\n",  emit(F)
        printf "  \"core_mhz\": %s,\n", emit(M)
        printf "  \"phases\": %s,\n",   phases
        printf "  \"sample\": %s\n",    (wnum>0 ? "true" : "false")
        printf "}\n"
      }' "$RUN_DIR/telemetry.csv" > "$OUT"
    echo "telemetry: wrote $OUT" >&2
    ;;

  *)
    echo "usage: $0 {start|phase|stop} <run_dir> [...]" >&2
    exit 2
    ;;
esac
