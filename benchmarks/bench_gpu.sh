#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bench_gpu.sh <run_dir> <tier>  ->  bench_gpu.json   (NVIDIA GPUs / AI nodes)
#
# nvidia-smi inventory + per-GPU power/temp/util/ECC, driver/CUDA versions, and
# DCGM diagnostics. GEMM/HBM/NVLink/NCCL throughput come from optional tools and
# are filled when their result files are provided (env below); otherwise null.
# Non-destructive. Degrades to {"count":0,"per_gpu":[]} with no GPUs present.
#
#   env: CB_DCGM_LEVEL (1..4, default 1), CB_NCCL_ALLREDUCE_GBPS (from nccl-tests)
# ---------------------------------------------------------------------------
set -uo pipefail
RUN_DIR="${1:-}"; TIER="${2:-acceptance}"
[ -n "$RUN_DIR" ] || { echo "usage: bench_gpu.sh <run_dir> <tier>" >&2; exit 2; }
mkdir -p "$RUN_DIR"; OUT="$RUN_DIR/bench_gpu.json"

have() { command -v "$1" >/dev/null 2>&1; }
log()  { printf '%s\n' "$*" >&2; }
Jn()   { case "${1:-}" in ''|n/a|'[N/A]'|'[Not Supported]') printf 'null';; *) printf '%s' "$1" | grep -qE '^-?[0-9]+(\.[0-9]+)?$' && printf '%s' "$1" || printf 'null';; esac; }
Js()   { printf '"%s"' "${1:-n/a}"; }

if ! have nvidia-smi; then
  printf '{"model":"n/a","count":0,"per_gpu":[]}\n' > "$OUT"
  log "bench_gpu: no nvidia-smi -> no GPUs"
  exit 0
fi

driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')"
cuda="$(nvidia-smi -q 2>/dev/null | awk -F: '/CUDA Version/{gsub(/ /,"",$2);print $2;exit}')"
model="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | sed 's/^ *//;s/ *$//')"
tdp="$(nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"

# optional throughput result files (one value per GPU per line), produced by the
# operator's GEMM/HBM/NVLink benchmarks; absent -> null
read_line() { sed -n "$2p" "$1" 2>/dev/null | tr -dc '0-9.'; }
GEMM_F="${CB_GPU_GEMM_FP8_FILE:-}"; BF16_F="${CB_GPU_GEMM_BF16_FILE:-}"
HBM_F="${CB_GPU_HBM_FILE:-}"; NVL_F="${CB_GPU_NVLINK_FILE:-}"

items=(); i=0
while IFS=, read -r idx pwr temp util ecc; do
  idx="$(printf '%s' "$idx" | tr -dc '0-9')"; [ -n "$idx" ] || continue
  ln=$((i + 1))
  g_fp8="$([ -n "$GEMM_F" ] && read_line "$GEMM_F" "$ln" || echo '')"
  g_bf16="$([ -n "$BF16_F" ] && read_line "$BF16_F" "$ln" || echo '')"
  g_hbm="$([ -n "$HBM_F" ] && read_line "$HBM_F" "$ln" || echo '')"
  g_nvl="$([ -n "$NVL_F" ] && read_line "$NVL_F" "$ln" || echo '')"
  items+=("$(printf '{"id":%s,"gemm_fp8_tflops":%s,"gemm_bf16_tflops":%s,"hbm_bw_gbs":%s,"nvlink_bw_gbs":%s,"power_w":%s,"temp_c":%s,"util_pct":%s,"ecc_errors":%s}' \
    "$(Jn "$idx")" "$(Jn "$g_fp8")" "$(Jn "$g_bf16")" "$(Jn "$g_hbm")" "$(Jn "$g_nvl")" \
    "$(Jn "$(printf '%s' "$pwr" | tr -dc '0-9.')")" "$(Jn "$(printf '%s' "$temp" | tr -dc '0-9')")" \
    "$(Jn "$(printf '%s' "$util" | tr -dc '0-9')")" "$(Jn "$(printf '%s' "$ecc" | tr -dc '0-9')")")")
  i=$((i + 1))
done < <(nvidia-smi --query-gpu=index,power.draw,temperature.gpu,utilization.gpu,ecc.errors.uncorrected.aggregate.total --format=csv,noheader,nounits 2>/dev/null)

count=${#items[@]}

# DCGM diagnostics (level scales with tier by default)
dcgm="n/a"
if have dcgmi; then
  lvl="${CB_DCGM_LEVEL:-$([ "$TIER" = acceptance ] && echo 1 || echo 3)}"
  log "  dcgmi diag -r $lvl"
  if dcgmi diag -r "$lvl" >"$RUN_DIR/dcgm.out" 2>&1; then dcgm="passed (level $lvl)"; else dcgm="FAILED -- see dcgm.out"; fi
fi

per_json="$(IFS=,; printf '%s' "${items[*]}")"
{
printf '{'
printf '"model":%s,"count":%s,"driver":%s,"cuda":%s,"tdp_w":%s,' \
  "$(Js "$model")" "$count" "$(Js "$driver")" "$(Js "$cuda")" "$(Jn "$tdp")"
printf '"nccl_allreduce_gbps":%s,"dcgm_diag":%s,' "$(Jn "${CB_NCCL_ALLREDUCE_GBPS:-}")" "$(Js "$dcgm")"
printf '"per_gpu":[%s]}' "$per_json"
printf '\n'
} > "$OUT"
log "bench_gpu: wrote $OUT ($count GPU(s), dcgm=$dcgm)"
