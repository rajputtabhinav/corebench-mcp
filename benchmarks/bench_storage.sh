#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bench_storage.sh -- fio storage suite (peak + SNIA steady-state + sweeps).
#
#   bench_storage.sh <run_dir> <tier> <dev> [<dev> ...]
#       tier: acceptance | qualification | deep-dive
#       dev : EXPLICIT device(s), e.g. /dev/nvme0n1   (never "all")
#
# DESTRUCTIVE: writes raw to the listed devices (and PURGE/precondition for
# steady-state). Refuses to run unless CONFIRM_DESTRUCTIVE=1, and independently
# re-checks that no target is mounted or the root device (defense in depth -- the
# MCP Tier-B gate is the primary guard). Treats nothing it reads as a command.
#
# Per workload it writes fio.<label>.<workload>.json; then parse_fio.py merges
# those + storage_meta.json into bench_storage.json. Raw JSON is retained so the
# report's numbers can be audited against fio's own output.
# ---------------------------------------------------------------------------
set -uo pipefail
shopt -s nullglob
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RUN_DIR="${1:-}"; TIER="${2:-acceptance}"; shift 2 2>/dev/null || true
DEVICES=("$@")

[ -n "$RUN_DIR" ] && [ "${#DEVICES[@]}" -gt 0 ] || { echo "usage: bench_storage.sh <run_dir> <tier> <dev>..." >&2; exit 2; }
mkdir -p "$RUN_DIR"

have() { command -v "$1" >/dev/null 2>&1; }
log()  { printf '%s\n' "$*" >&2; }

# ----- safety (belt-and-suspenders; MCP gate is primary) -------------------
[ "${CONFIRM_DESTRUCTIVE:-0}" = "1" ] || { log "REFUSE: CONFIRM_DESTRUCTIVE != 1"; exit 3; }
for d in "${DEVICES[@]}"; do
  case "$d" in all|""|/|/dev) log "REFUSE: illegal target '$d'"; exit 3;; esac
  [ -b "$d" ] || { log "REFUSE: '$d' is not a block device"; exit 3; }
  base="$(basename "$d")"
  if have lsblk; then
    mnts="$(lsblk -nro MOUNTPOINT "$d" 2>/dev/null | grep -v '^$' || true)"
    [ -n "$mnts" ] && { log "REFUSE: '$d' is mounted ($mnts)"; exit 3; }
  fi
  if have findmnt; then
    rootsrc="$(findmnt -nro SOURCE / 2>/dev/null)"
    case "$rootsrc" in *"$base"*) log "REFUSE: '$d' holds the root filesystem"; exit 3;; esac
  fi
done

if ! have fio; then log "ERROR: fio not found"; exit 4; fi

# python used for JSON parsing (parser + steady-state extraction)
PY="${CB_PYTHON:-}"; [ -x "$PY" ] || PY="$HERE/../.venv/bin/python"; [ -x "$PY" ] || PY="$HERE/../.venv/Scripts/python.exe"; [ -x "$PY" ] || PY="$(command -v python3 || command -v python || echo python3)"
fio_iops() { "$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); print(d['jobs'][0][sys.argv[2]]['iops'])" "$1" "$2" 2>/dev/null; }

# ----- runtimes by tier ----------------------------------------------------
case "$TIER" in
  acceptance)    RT="${CB_RUNTIME:-30}";  DO_STEADY=0; DO_SWEEP=0; DO_SOAK=0 ;;
  qualification) RT="${CB_RUNTIME:-60}";  DO_STEADY=1; DO_SWEEP=1; DO_SOAK=0 ;;
  deep-dive)     RT="${CB_RUNTIME:-120}"; DO_STEADY=1; DO_SWEEP=1; DO_SOAK=1 ;;
  *) log "unknown tier '$TIER'"; exit 2 ;;
esac
SOAK_SECS="${CB_SOAK_SECS:-86400}"   # 24h default for deep-dive

gen_lane_gbs() { case "$1" in
  Gen1) echo 0.25;; Gen2) echo 0.5;; Gen3) echo 0.985;; Gen4) echo 1.969;;
  Gen5) echo 3.938;; Gen6) echo 7.563;; *) echo 0;; esac; }

# probe negotiated/capable link -> sets LINK / LINK_CAP / NEG_GBS / CAP_GBS / WARN
probe_link() {
  local dev="$1" base pci sta cap sgts swid cgts cwid
  base="$(basename "$dev")"; LINK="n/a"; LINK_CAP="n/a"; NEG_GBS=0; CAP_GBS=0; WARN=""
  pci="$(basename "$(readlink -f "/sys/block/$base/device/device" 2>/dev/null)" 2>/dev/null)"
  have lspci && [ -n "$pci" ] && [ "$pci" != "." ] || return 0
  local lnk; lnk="$(lspci -vvs "$pci" 2>/dev/null)"
  sta="$(printf '%s' "$lnk" | grep -m1 'LnkSta:')"; cap="$(printf '%s' "$lnk" | grep -m1 'LnkCap:')"
  sgts="$(printf '%s' "$sta" | grep -oE '[0-9.]+GT/s' | grep -oE '[0-9.]+')"
  swid="$(printf '%s' "$sta" | grep -oE 'Width x[0-9]+' | grep -oE '[0-9]+')"
  cgts="$(printf '%s' "$cap" | grep -oE '[0-9.]+GT/s' | grep -oE '[0-9.]+')"
  cwid="$(printf '%s' "$cap" | grep -oE 'Width x[0-9]+' | grep -oE '[0-9]+')"
  local sgen cgen
  case "$sgts" in 2.5*)sgen=Gen1;;5)sgen=Gen2;;8)sgen=Gen3;;16)sgen=Gen4;;32)sgen=Gen5;;64)sgen=Gen6;;*)sgen="";; esac
  case "$cgts" in 2.5*)cgen=Gen1;;5)cgen=Gen2;;8)cgen=Gen3;;16)cgen=Gen4;;32)cgen=Gen5;;64)cgen=Gen6;;*)cgen="";; esac
  [ -n "$sgen" ] && LINK="$sgen x${swid:-?}" && NEG_GBS="$(awk -v l="$(gen_lane_gbs "$sgen")" -v w="${swid:-0}" 'BEGIN{printf "%.2f", l*w}')"
  [ -n "$cgen" ] && LINK_CAP="$cgen x${cwid:-?}" && CAP_GBS="$(awk -v l="$(gen_lane_gbs "$cgen")" -v w="${cwid:-0}" 'BEGIN{printf "%.2f", l*w}')"
  if [ -n "$sgen" ] && [ -n "$cgen" ]; then
    if [ "${swid:-0}" -lt "${cwid:-0}" ] 2>/dev/null || [ "$sgen" != "$cgen" ]; then
      WARN="negotiated $LINK, capable $LINK_CAP"
    fi
  fi
}

# run one fio workload -> fio.<label>.<wl>.json
runfio() {
  local label="$1" wl="$2"; shift 2
  local out="$RUN_DIR/fio.$label.$wl.json"
  log "    fio: $label/$wl"
  $NUMACTL fio --name="$wl" --filename="$DEV" --ioengine=libaio --direct=1 \
    --output-format=json --time_based --runtime="$RT" --group_reporting \
    --clat_percentiles=1 --lat_percentiles=1 "$@" >"$out" 2>>"$RUN_DIR/fio.err" \
    || log "      (fio $wl returned non-zero -- see fio.err)"
}

meta_items=()
for DEV in "${DEVICES[@]}"; do
  base="$(basename "$DEV")"; label="${base%n[0-9]}"   # nvme0n1 -> nvme0 ; sda -> sda
  log "  drive: $DEV (label=$label, tier=$TIER)"

  # NUMA pin to the node the drive lives on
  NUMACTL=""
  node="$(cat "/sys/block/$base/device/numa_node" 2>/dev/null || echo -1)"
  if have numactl && [ "${node:--1}" -ge 0 ] 2>/dev/null; then
    NUMACTL="numactl --cpunodebind=$node --membind=$node"
    log "    NUMA-pinned to node $node"
  fi

  probe_link "$DEV"

  # --- peak (all tiers) ---
  runfio "$label" seqread   --rw=read      --bs=1M --iodepth=32  --numjobs=4
  runfio "$label" seqwrite  --rw=write     --bs=1M --iodepth=32  --numjobs=4
  runfio "$label" randread  --rw=randread  --bs=4k --iodepth=128 --numjobs=4
  runfio "$label" randwrite --rw=randwrite --bs=4k --iodepth=128 --numjobs=4
  runfio "$label" qd1read   --rw=randread  --bs=4k --iodepth=1   --numjobs=1
  runfio "$label" qd1write  --rw=randwrite --bs=4k --iodepth=1   --numjobs=1

  # --- SNIA steady-state (qualification/deep-dive) ---
  if [ "$DO_STEADY" = 1 ]; then
    log "    SNIA preconditioning $DEV (PURGE + 2x write)"
    if have nvme && [[ "$base" == nvme* ]]; then nvme format "$DEV" -s 1 >/dev/null 2>&1 || blkdiscard "$DEV" 2>/dev/null || true
    elif have blkdiscard; then blkdiscard "$DEV" 2>/dev/null || true; fi
    cap_bytes="$(blockdev --getsize64 "$DEV" 2>/dev/null || echo 0)"
    io2x=$(( cap_bytes * 2 ))
    [ "$io2x" -gt 0 ] && $NUMACTL fio --name=precondition --filename="$DEV" --ioengine=libaio \
      --direct=1 --rw=randwrite --bs=4k --iodepth=128 --numjobs=4 --io_size="$io2x" \
      --output-format=json >"$RUN_DIR/fio.$label.precondition.json" 2>>"$RUN_DIR/fio.err" || true
    # measure rounds; keep last 5; stop when 5-point range<=10% and slope<=5%
    iops5=(); round=0
    while [ "$round" -lt "${CB_STEADY_MAX_ROUNDS:-25}" ]; do
      round=$((round+1))
      $NUMACTL fio --name=steady --filename="$DEV" --ioengine=libaio --direct=1 \
        --rw=randwrite --bs=4k --iodepth=128 --numjobs=4 --time_based --runtime=60 \
        --output-format=json --clat_percentiles=1 >"$RUN_DIR/fio.$label.steady.json" 2>>"$RUN_DIR/fio.err" || break
      cur="$(fio_iops "$RUN_DIR/fio.$label.steady.json" write)"
      [ -n "$cur" ] || break
      iops5+=("$cur"); iops5=("${iops5[@]: -5}")
      if [ "${#iops5[@]}" -eq 5 ]; then
        stable="$(awk -v a="${iops5[0]}" -v b="${iops5[1]}" -v c="${iops5[2]}" -v d="${iops5[3]}" -v e="${iops5[4]}" '
          BEGIN{ n=5; s=a+b+c+d+e; avg=s/n; mx=a;mn=a;
            split(a" "b" "c" "d" "e,v," "); for(i=1;i<=n;i++){if(v[i]>mx)mx=v[i]; if(v[i]<mn)mn=v[i]}
            rng=(mx-mn)/avg*100;
            # slope via least squares over x=1..5
            sx=15; sxx=55; sy=s; sxy=1*a+2*b+3*c+4*d+5*e;
            slope=(n*sxy - sx*sy)/(n*sxx - sx*sx); slopep=slope/avg*100;
            print (rng<=10 && (slopep<5 && slopep>-5))?"1":"0" }')"
        log "      steady round $round: iops=$cur (window range/slope check=$stable)"
        [ "$stable" = "1" ] && { log "    steady-state reached at round $round"; break; }
      fi
    done
  fi

  # --- sweeps (qualification/deep-dive): QD, block size, rw-mix ---
  if [ "$DO_SWEEP" = 1 ]; then
    for qd in 1 4 16 64 256; do
      runfio "$label" "sweep_qd$qd" --rw=randread --bs=4k --iodepth="$qd" --numjobs=1
    done
    for bs in 4k 16k 64k 256k 1M; do
      runfio "$label" "sweep_bs$bs" --rw=read --bs="$bs" --iodepth=32 --numjobs=1
    done
    for mix in 100 70 50 30 0; do
      runfio "$label" "sweep_mix$mix" --rw=randrw --rwmixread="$mix" --bs=4k --iodepth=64 --numjobs=1
    done
  fi

  # --- soak (deep-dive) ---
  if [ "$DO_SOAK" = 1 ]; then
    log "    soak: ${SOAK_SECS}s mixed load"
    $NUMACTL fio --name=soak --filename="$DEV" --ioengine=libaio --direct=1 --rw=randrw \
      --rwmixread=70 --bs=4k --iodepth=64 --numjobs=4 --time_based --runtime="$SOAK_SECS" \
      --output-format=json --clat_percentiles=1 >"$RUN_DIR/fio.$label.soak.json" 2>>"$RUN_DIR/fio.err" || true
  fi

  meta_items+=("$(printf '{"dev":"%s","label":"%s","link":"%s","link_capable":"%s","link_negotiated_gbs":%s,"link_capable_gbs":%s,"link_width_warn":%s}' \
    "$DEV" "$label" "$LINK" "$LINK_CAP" "${NEG_GBS:-0}" "${CAP_GBS:-0}" \
    "$([ -n "$WARN" ] && printf '"%s"' "$WARN" || printf 'null')")")
done

# storage_meta.json (inventory) -> parse_fio merges with fio.*.json
printf '[%s]\n' "$(IFS=,; printf '%s' "${meta_items[*]}")" > "$RUN_DIR/storage_meta.json"

"$PY" "$HERE/parse_fio.py" "$RUN_DIR" || { log "parse_fio failed"; exit 5; }
log "bench_storage: done -> $RUN_DIR/bench_storage.json"
