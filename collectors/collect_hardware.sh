#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# collect_hardware.sh -- emit hardware.json (CPU/mem/BIOS/BMC/kernel/storage/NIC).
#
# Every external tool and every /sys|/proc read degrades to "n/a" if absent, so
# this runs (and emits structurally-valid JSON) on a bare VM -- or even on a
# non-Linux box for shape testing. It NEVER executes anything found in collected
# output; all collected text is treated as data.
#
# Usage:  collect_hardware.sh [output.json]   (default: ./hardware.json)
# ---------------------------------------------------------------------------
set -uo pipefail          # NOT -e: a failing probe must not abort the run
shopt -s nullglob         # unmatched globs expand to nothing, not literal text

OUT="${1:-hardware.json}"

# ----- primitives ----------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }
is_root() { [ "${EUID:-$(id -u 2>/dev/null || echo 1000)}" -eq 0 ] 2>/dev/null; }
trim() { sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }
nz()  { local v="${1:-}"; if [ -n "$v" ]; then printf '%s' "$v"; else printf 'n/a'; fi; }

# read a file (capped) or "n/a"
rd() { if [ -r "$1" ]; then tr -d '\000' < "$1" 2>/dev/null | head -c 4096; else printf 'n/a'; fi; }

# read KEY=value from a shell-style file WITHOUT sourcing it (data, not code)
rd_kv() {
  [ -r "$1" ] || { printf 'n/a'; return; }
  local v; v="$(awk -F= -v k="$2" '$1==k{gsub(/"/,"",$2); print $2; exit}' "$1" 2>/dev/null | trim)"
  nz "$v"
}

# pull a "Key: value" field out of lscpu
lscpu_val() {
  have lscpu || { printf 'n/a'; return; }
  local v; v="$(lscpu 2>/dev/null | awk -F: -v k="$1" '$1==k{sub(/^[ \t]+/,"",$2); print $2; exit}' | trim)"
  nz "$v"
}

# JSON string escaper: stdin -> escaped body (no surrounding quotes)
jesc() {
  local s; s="$(cat)"
  s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
  s="${s//$'\t'/\\t}"; s="${s//$'\r'/}"; s="${s//$'\n'/\\n}"
  printf '%s' "$s"
}
# J <value> -> a quoted, escaped JSON string
J() { printf '"%s"' "$(printf '%s' "${1:-n/a}" | jesc)"; }

# GT/s -> PCIe generation label
gts_to_gen() {
  case "$1" in
    2.5*) echo "Gen1" ;; 5*) echo "Gen2" ;; 8*) echo "Gen3" ;;
    16*)  echo "Gen4" ;; 32*) echo "Gen5" ;; 64*) echo "Gen6" ;; *) echo "" ;;
  esac
}

# chassis type code -> label (subset of SMBIOS table)
chassis_label() {
  case "$1" in
    17|23) echo "Rack Mount" ;; 22) echo "2U" ;; 3) echo "Desktop" ;;
    9|10) echo "Laptop" ;; 1) echo "Other" ;; *) echo "${1:-n/a}" ;;
  esac
}

# ----- system --------------------------------------------------------------
sys_vendor="$(nz "$(rd /sys/class/dmi/id/sys_vendor | trim)")"
sys_model="$(nz "$(rd /sys/class/dmi/id/product_name | trim)")"
chassis="$(chassis_label "$(rd /sys/class/dmi/id/chassis_type | trim)")"
host="$(hostname 2>/dev/null | trim)"; [ -n "$host" ] || host="$(rd /proc/sys/kernel/hostname | trim)"; host="$(nz "$host")"
serial="n/a"
if have dmidecode && is_root; then serial="$(dmidecode -s system-serial-number 2>/dev/null | trim)"; fi
[ "$serial" = "n/a" ] || [ -z "$serial" ] && serial="$(rd /sys/class/dmi/id/product_serial | trim)"
serial="$(nz "$serial")"
# baseboard / motherboard (readable without root via sysfs)
board_vendor="$(rd /sys/class/dmi/id/board_vendor | trim)"
board_name="$(rd /sys/class/dmi/id/board_name | trim)"
board="$(printf '%s %s' "$board_vendor" "$board_name" | trim)"
[ "$board" = "n/a n/a" ] && board="n/a"
board="$(nz "$board")"

# ----- cpu -----------------------------------------------------------------
cpu_model="$(lscpu_val 'Model name')"
[ "$cpu_model" = "n/a" ] && cpu_model="$(nz "$(awk -F: '/model name/{sub(/^[ \t]+/,"",$2);print $2;exit}' /proc/cpuinfo 2>/dev/null | trim)")"
sockets="$(lscpu_val 'Socket(s)')"
cores_ps="$(lscpu_val 'Core(s) per socket')"
threads="$(lscpu_val 'CPU(s)')"
numa_nodes="$(lscpu_val 'NUMA node(s)')"
cpu_max="$(lscpu_val 'CPU max MHz')"
cpu_min="$(lscpu_val 'CPU min MHz')"
isa="n/a"
if [ -r /proc/cpuinfo ]; then
  flags="$(awk -F: '/^flags|^Features/{print $2; exit}' /proc/cpuinfo 2>/dev/null)"
  set -- ; acc=""
  for f in avx512f avx512_bf16 amx_tile vnni avx2 sve; do
    case " $flags " in *" $f "*) acc="$acc${acc:+, }$f" ;; esac
  done
  [ -n "$acc" ] && isa="$acc"
fi
# NUMA node of each storage/NIC device (critical for pinning)
numa_map=""
for d in /sys/class/nvme/nvme* /sys/class/net/*; do
  [ -e "$d" ] || continue
  base="$(basename "$d")"; [ "$base" = "lo" ] && continue
  node="$(rd "$d/device/numa_node" 2>/dev/null | trim)"
  [ -z "$node" ] || [ "$node" = "n/a" ] && node="$(rd "$d/device/device/numa_node" 2>/dev/null | trim)"
  [ -n "$node" ] && [ "$node" != "n/a" ] && numa_map="$numa_map${numa_map:+; }$base -> node$node"
done
numa_map="$(nz "$numa_map")"

# ----- memory --------------------------------------------------------------
mem_total="n/a"
if [ -r /proc/meminfo ]; then
  kb="$(awk '/^MemTotal/{print $2; exit}' /proc/meminfo 2>/dev/null)"
  [ -n "$kb" ] && mem_total="$(awk -v k="$kb" 'BEGIN{printf "%.0f GB", k/1024/1024}')"
fi
mem_type="n/a"; mem_cfg_speed="n/a"; mem_rated="n/a"; dimms_pop="n/a"; speed_status="n/a"
if have dmidecode && is_root; then
  dmi_mem="$(dmidecode -t memory 2>/dev/null)"
  mem_type="$(nz "$(printf '%s' "$dmi_mem" | awk -F: '/^\tType:/{gsub(/^[ \t]+/,"",$2); if($2!="Unknown"){print $2; exit}}' | trim)")"
  mem_cfg_speed="$(nz "$(printf '%s' "$dmi_mem" | awk -F: '/Configured Memory Speed:/{gsub(/^[ \t]+/,"",$2); if($2!="Unknown"){print $2; exit}}' | trim)")"
  mem_rated="$(nz "$(printf '%s' "$dmi_mem" | awk -F: '/^\tSpeed:/{gsub(/^[ \t]+/,"",$2); if($2!="Unknown"){print $2; exit}}' | trim)")"
  cnt="$(printf '%s' "$dmi_mem" | grep -c 'Size:.*[0-9].*\(MB\|GB\)' 2>/dev/null)"
  [ -n "$cnt" ] && [ "$cnt" -gt 0 ] && dimms_pop="$cnt populated"
  if [ "$mem_cfg_speed" != "n/a" ] && [ "$mem_rated" != "n/a" ]; then
    [ "$mem_cfg_speed" = "$mem_rated" ] && speed_status="at rated speed" || speed_status="BELOW rated ($mem_cfg_speed vs $mem_rated)"
  fi
fi

# ----- bios ----------------------------------------------------------------
bios_ver="$(nz "$(rd /sys/class/dmi/id/bios_version | trim)")"
bios_date="$(nz "$(rd /sys/class/dmi/id/bios_date | trim)")"
pcie_aspm="$(nz "$(rd /sys/module/pcie_aspm/parameters/policy | trim)")"
# These are vendor/Redfish-specific; capture_platform_settings (MCP) fills them.
power_profile="n/a"; c_states="n/a"; numa_nps="n/a"; m2_bif="n/a"

# ----- bmc -----------------------------------------------------------------
bmc_fw="n/a"; psu_model="n/a"; psu_watts="n/a"; redundancy="n/a"; tele_src="n/a"
if have ipmitool; then
  bmc_fw="$(nz "$(ipmitool mc info 2>/dev/null | awk -F: '/Firmware Revision/{gsub(/^[ \t]+/,"",$2);print $2;exit}' | trim)")"
  psu_model="$(nz "$(ipmitool fru 2>/dev/null | awk -F: '/Power Supply|PSU/{getline; if($0 ~ /Product Name/){split($0,a,":"); print a[2]; exit}}' | trim)")"
  [ "$bmc_fw" != "n/a" ] && tele_src="IPMI"
fi
# Prefer Redfish if the local BMC answers (kept read-only; no creds embedded)
if have curl && curl -ks --max-time 2 https://127.0.0.1/redfish/v1/ >/dev/null 2>&1; then
  tele_src="Redfish"
fi

# ----- kernel / os ---------------------------------------------------------
distro="$(rd_kv /etc/os-release PRETTY_NAME)"
kver="$(nz "$(uname -r 2>/dev/null | trim)")"
governor="$(nz "$(rd /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor | trim)")"
tuned="n/a"; have tuned-adm && tuned="$(nz "$(tuned-adm active 2>/dev/null | sed 's/.*: //' | trim)")"
cmdline="$(nz "$(rd /proc/cmdline | trim)")"

# ----- storage devices -----------------------------------------------------
storage_items=()
if have lsblk; then
  while IFS= read -r dev; do
    [ -n "$dev" ] || continue
    path="/dev/$dev"
    model="$(nz "$(lsblk -dn -o MODEL "$path" 2>/dev/null | trim)")"
    size="$(nz "$(lsblk -dn -o SIZE  "$path" 2>/dev/null | trim)")"
    sern="$(nz "$(lsblk -dn -o SERIAL "$path" 2>/dev/null | trim)")"
    link_neg="n/a"; link_cap="n/a"
    pci="$(basename "$(readlink -f "/sys/block/$dev/device/device" 2>/dev/null)" 2>/dev/null)"
    if have lspci && [ -n "$pci" ] && [ "$pci" != "." ]; then
      lnk="$(lspci -vvs "$pci" 2>/dev/null)"
      sta="$(printf '%s' "$lnk" | grep -m1 'LnkSta:')"
      cap="$(printf '%s' "$lnk" | grep -m1 'LnkCap:')"
      sgts="$(printf '%s' "$sta" | grep -oE 'Speed [0-9.]+GT/s' | grep -oE '[0-9.]+')"
      swid="$(printf '%s' "$sta" | grep -oE 'Width x[0-9]+' | grep -oE 'x[0-9]+')"
      cgts="$(printf '%s' "$cap" | grep -oE 'Speed [0-9.]+GT/s' | grep -oE '[0-9.]+')"
      cwid="$(printf '%s' "$cap" | grep -oE 'Width x[0-9]+' | grep -oE 'x[0-9]+')"
      [ -n "$sgts" ] && link_neg="$(gts_to_gen "$sgts") ${swid:-}"
      [ -n "$cgts" ] && link_cap="$(gts_to_gen "$cgts") ${cwid:-}"
      link_neg="$(echo "$link_neg" | trim)"; link_cap="$(echo "$link_cap" | trim)"
    fi
    storage_items+=("$(printf '{"dev":%s,"label":%s,"model":%s,"pn":%s,"form":%s,"capacity":%s,"serial":%s,"link":%s,"link_capable":%s}' \
      "$(J "$path")" "$(J "$dev")" "$(J "$model")" "$(J "n/a")" "$(J "n/a")" "$(J "$size")" "$(J "$sern")" "$(J "$link_neg")" "$(J "$link_cap")")")
  done < <(lsblk -dn -o NAME,TYPE 2>/dev/null | awk '$2=="disk"{print $1}' | grep -E '^(nvme|sd)')
fi
storage_json="$(IFS=,; printf '%s' "${storage_items[*]}")"

# ----- network interfaces --------------------------------------------------
net_items=()
for ifp in /sys/class/net/*; do
  iface="$(basename "$ifp")"
  [ "$iface" = "lo" ] && continue
  drv="$(basename "$(readlink -f "$ifp/device/driver" 2>/dev/null)" 2>/dev/null)"
  [ -z "$drv" ] || [ "$drv" = "." ] && drv="n/a"
  spd_raw="$(rd "$ifp/speed" 2>/dev/null | trim)"
  if [ -n "$spd_raw" ] && [ "$spd_raw" != "n/a" ] && [ "$spd_raw" -gt 0 ] 2>/dev/null; then
    spd="$(awk -v m="$spd_raw" 'BEGIN{printf (m>=1000)?"%g Gb/s":"%g Mb/s", (m>=1000)?m/1000:m}')"
  else spd="n/a"; fi
  model="n/a"
  if have ethtool; then
    bus="$(ethtool -i "$iface" 2>/dev/null | awk -F: '/bus-info/{gsub(/^[ \t]+/,"",$2);print $2;exit}' | trim)"
    [ -n "$bus" ] && have lspci && model="$(nz "$(lspci -s "$bus" 2>/dev/null | sed 's/^[^ ]* //' | trim)")"
  fi
  net_items+=("$(printf '{"iface":%s,"model":%s,"speed":%s,"driver":%s}' \
    "$(J "$iface")" "$(J "$model")" "$(J "$spd")" "$(J "$drv")")")
done
network_json="$(IFS=,; printf '%s' "${net_items[*]}")"

# ----- emit ----------------------------------------------------------------
{
printf '{\n'
printf '  "system": {"vendor":%s,"model":%s,"board":%s,"chassis":%s,"hostname":%s,"serial":%s},\n' \
  "$(J "$sys_vendor")" "$(J "$sys_model")" "$(J "$board")" "$(J "$chassis")" "$(J "$host")" "$(J "$serial")"
printf '  "cpu": {"model":%s,"sockets":%s,"cores_per_socket":%s,"threads":%s,"numa_nodes":%s,"base_mhz":%s,"max_mhz":%s,"isa":%s,"numa_device_map":%s},\n' \
  "$(J "$cpu_model")" "$(J "$sockets")" "$(J "$cores_ps")" "$(J "$threads")" "$(J "$numa_nodes")" "$(J "$cpu_min")" "$(J "$cpu_max")" "$(J "$isa")" "$(J "$numa_map")"
printf '  "memory": {"total":%s,"type":%s,"dimms_populated":%s,"configured_speed":%s,"rated_speed":%s,"speed_status":%s},\n' \
  "$(J "$mem_total")" "$(J "$mem_type")" "$(J "$dimms_pop")" "$(J "$mem_cfg_speed")" "$(J "$mem_rated")" "$(J "$speed_status")"
printf '  "bios": {"version":%s,"date":%s,"power_profile":%s,"c_states":%s,"numa_nps":%s,"pcie_aspm":%s,"m2_bifurcation":%s},\n' \
  "$(J "$bios_ver")" "$(J "$bios_date")" "$(J "$power_profile")" "$(J "$c_states")" "$(J "$numa_nps")" "$(J "$pcie_aspm")" "$(J "$m2_bif")"
printf '  "bmc": {"firmware":%s,"psu_model":%s,"psu_watts":%s,"redundancy":%s,"telemetry_source":%s},\n' \
  "$(J "$bmc_fw")" "$(J "$psu_model")" "$(J "$psu_watts")" "$(J "$redundancy")" "$(J "$tele_src")"
printf '  "kernel": {"distro":%s,"kernel":%s,"governor":%s,"tuned_profile":%s,"cmdline":%s},\n' \
  "$(J "$distro")" "$(J "$kver")" "$(J "$governor")" "$(J "$tuned")" "$(J "$cmdline")"
printf '  "storage": [%s],\n' "$storage_json"
printf '  "network": [%s]\n' "$network_json"
printf '}\n'
} > "$OUT"

printf 'collect_hardware: wrote %s\n' "$OUT" >&2
