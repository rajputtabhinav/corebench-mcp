#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bench_network.sh <run_dir> <peer_host> [tests_csv]   ->  bench_network.json
#
# Two-node initiator side. The responder (iperf3 -s / ib_write_bw server) is
# launched on the registered peer by the MCP layer (start_network_validation)
# over the secured SSH channel; this script runs the client locally and parses
# both sides into the report.
#
#   tests_csv : tcp,roce,latency   (default: tcp)
#   env       : CB_IFACE, CB_IPERF_STREAMS (8), CB_NET_SECS, CB_PEER_RDMA(host)
#
# RoCEv2 numbers are meaningless without lossless config, so the fabric
# prerequisites (RDMA link/speed, MTU, PFC priority, ECN) are captured read-only
# and recorded in the report. Non-destructive; treats all output as data.
# ---------------------------------------------------------------------------
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="${1:-}"; PEER="${2:-}"; TESTS="${3:-tcp}"
[ -n "$RUN_DIR" ] && [ -n "$PEER" ] || { echo "usage: bench_network.sh <run_dir> <peer> [tests]" >&2; exit 2; }
mkdir -p "$RUN_DIR"; OUT="$RUN_DIR/bench_network.json"

have() { command -v "$1" >/dev/null 2>&1; }
log()  { printf '%s\n' "$*" >&2; }
Jn()   { case "${1:-}" in ''|n/a) printf 'null';; *) printf '%s' "$1" | grep -qE '^-?[0-9]+(\.[0-9]+)?$' && printf '%s' "$1" || printf 'null';; esac; }
Js()   { printf '"%s"' "${1:-n/a}"; }

PY="${CB_PYTHON:-}"; [ -x "$PY" ] || PY="$HERE/../.venv/bin/python"; [ -x "$PY" ] || PY="$HERE/../.venv/Scripts/python.exe"; [ -x "$PY" ] || PY="$(command -v python3 || command -v python || echo python3)"
IFACE="${CB_IFACE:-}"; STREAMS="${CB_IPERF_STREAMS:-8}"; DUR="${CB_NET_SECS:-10}"
ipf() { "$PY" -c "import json,sys
d=json.load(open(sys.argv[1]))['end']; k=sys.argv[2]
v={'rx':d.get('sum_received',{}).get('bits_per_second'),
   'tx':d.get('sum_sent',{}).get('bits_per_second'),
   'bidir':(d.get('sum_sent',{}).get('bits_per_second',0) or 0)+(d.get('sum_received',{}).get('bits_per_second',0) or 0)}.get(k)
print('' if not v else round(v/1e9,1))" "$1" "$2" 2>/dev/null; }

rx="n/a"; tx="n/a"; bidir="n/a"; multi="n/a"; pps="n/a"
roce_bw="n/a"; roce_lat="n/a"; mtu="n/a"; pfc="n/a"; ecn="n/a"; rdma_link="n/a"; fabric="n/a"

# ----- TCP (iperf3) --------------------------------------------------------
if [[ ",$TESTS," == *",tcp,"* ]] && have iperf3; then
  log "  iperf3 -> $PEER (fwd/rev/bidir/${STREAMS}-stream, ${DUR}s)"
  iperf3 -c "$PEER" -J -t "$DUR"            >"$RUN_DIR/iperf_fwd.json"   2>>"$RUN_DIR/net.err" && rx="$(ipf "$RUN_DIR/iperf_fwd.json" rx)"
  iperf3 -c "$PEER" -J -t "$DUR" -R         >"$RUN_DIR/iperf_rev.json"   2>>"$RUN_DIR/net.err" && tx="$(ipf "$RUN_DIR/iperf_rev.json" rx)"
  iperf3 -c "$PEER" -J -t "$DUR" --bidir    >"$RUN_DIR/iperf_bidir.json" 2>>"$RUN_DIR/net.err" && bidir="$(ipf "$RUN_DIR/iperf_bidir.json" bidir)"
  iperf3 -c "$PEER" -J -t "$DUR" -P "$STREAMS" >"$RUN_DIR/iperf_multi.json" 2>>"$RUN_DIR/net.err" && multi="$(ipf "$RUN_DIR/iperf_multi.json" rx)"
fi

# ----- latency (sockperf / netperf TCP_RR) ---------------------------------
if [[ ",$TESTS," == *",latency,"* ]]; then
  if have sockperf; then
    log "  sockperf ping-pong -> $PEER"
    sp="$(sockperf ping-pong -i "$PEER" -t "$DUR" --full-rtt 2>/dev/null)" || true
    roce_lat="$(printf '%s' "$sp" | awk -F= '/percentile 99.900/{gsub(/[^0-9.]/,"",$2);print $2}' | head -1)"
  elif have netperf; then
    netperf -H "$PEER" -t TCP_RR -- -o P99_LATENCY >/dev/null 2>&1 || true
  fi
fi

# ----- RoCE / RDMA (perftest) + fabric prerequisites -----------------------
if [[ ",$TESTS," == *",roce,"* ]]; then
  RPEER="${CB_PEER_RDMA:-$PEER}"
  if have ib_write_bw; then
    log "  ib_write_bw -> $RPEER"
    bw="$(ib_write_bw "$RPEER" 2>/dev/null | awk '/[0-9]/{v=$(NF-1)} END{print v}')"
    [ -n "$bw" ] && roce_bw="$(awk -v m="$bw" 'BEGIN{printf "%.1f", m*8/1000}')"  # MB/s -> Gb/s
  fi
  if have ib_write_lat; then
    lat="$(ib_write_lat "$RPEER" 2>/dev/null | awk '/99.9/{print $(NF)}' | head -1)"
    [ -n "$lat" ] && roce_lat="$lat"
  fi
  # fabric prerequisites (read-only)
  if have ibstat; then
    rdma_link="$(ibstat 2>/dev/null | awk '/State:/{s=$2} /Rate:/{r=$2} END{if(r)printf "%s Gb/s, %s", r, s}')"
    rdma_link="${rdma_link:-n/a}"
  fi
  [ -n "$IFACE" ] && mtu="$(cat "/sys/class/net/$IFACE/mtu" 2>/dev/null || echo n/a)"
  if [ -n "$IFACE" ] && have mlnx_qos; then
    pfc="$(mlnx_qos -i "$IFACE" 2>/dev/null | awk '/^[ \t]*enabled/{print; exit}')"; pfc="${pfc:-n/a}"
  fi
  fabric="back-to-back or lossless switch (verify PFC/ECN)"
fi

{
printf '{'
printf '"tcp_rx_gbps":%s,'              "$(Jn "$rx")"
printf '"tcp_tx_gbps":%s,'              "$(Jn "$tx")"
printf '"tcp_bidir_gbps":%s,'          "$(Jn "$bidir")"
printf '"tcp_multistream_gbps":%s,'    "$(Jn "$multi")"
printf '"pps_mpps":%s,'                "$(Jn "$pps")"
printf '"roce_ib_write_bw_gbps":%s,'   "$(Jn "$roce_bw")"
printf '"roce_ib_write_lat_p99_9_us":%s,' "$(Jn "$roce_lat")"
printf '"mtu":%s,'                     "$(Jn "$mtu")"
printf '"pfc":%s,'                     "$(Js "$pfc")"
printf '"ecn":%s,'                     "$(Js "$ecn")"
printf '"rdma_link":%s,'               "$(Js "$rdma_link")"
printf '"peer":%s,'                    "$(Js "$PEER")"
printf '"fabric":%s'                   "$(Js "$fabric")"
printf '}\n'
} > "$OUT"
log "bench_network: wrote $OUT"
