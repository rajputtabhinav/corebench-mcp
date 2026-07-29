# CoreBench MCP — setup

How to install dependencies, wire the server into an MCP client (Claude Desktop),
register peers for two-node tests, and (optionally) enable firmware tooling.

---

## 1. Install

```bash
cd "CoreBench MCP"
python3 -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Smoke-test the server starts over stdio (Ctrl-C to exit):

```bash
python mcp_server.py
```

Or explore it with the MCP Inspector:

```bash
mcp dev mcp_server.py
```

---

## 2. Wire into Claude Desktop

Edit the Claude Desktop config file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

```jsonc
{
  "mcpServers": {
    "corebench": {
      "command": "/abs/path/to/CoreBench MCP/.venv/bin/python",
      "args": ["/abs/path/to/CoreBench MCP/mcp_server.py"],
      "env": {
        "CB_IPMI_HOST": "10.0.0.217",   // BMC IP — telemetry polled OVER LAN
        "CB_IPMI_USER": "ADMIN",
        "CB_IPMI_PASS": "******"        // passed to ipmitool via $IPMI_PASSWORD (not argv)
      }
    }
  }
}
```

On **Windows**, use the venv interpreter and forward-slash or escaped paths:

```jsonc
{
  "mcpServers": {
    "corebench": {
      "command": "C:/Users/you/Desktop/CoreBench MCP/.venv/Scripts/python.exe",
      "args": ["C:/Users/you/Desktop/CoreBench MCP/mcp_server.py"]
    }
  }
}
```

Restart Claude Desktop. You should see the CoreBench tools available. Try:

> *"List the block devices, then run an acceptance validation on the unmounted
> NVMe test drive and give me the PDF."*

The agent will call `list_block_devices`, ask you to confirm the target, call
`start_validation(..., confirm_destructive=True)`, poll `get_run_status`, then
`generate_report`.

---

## 3. Environment variables

All optional; sensible defaults apply.

| Var | Purpose | Default |
|---|---|---|
| `CB_IPMI_HOST` / `CB_IPMI_USER` / `CB_IPMI_PASS` | Poll the BMC for power/thermal **over LAN** (so heavy CPU load can't starve telemetry) | in-band |
| `CB_IFACE` | Local NIC for network tests | — |
| `CB_PEER_RDMA` | Peer RDMA host for `ib_write_bw` (if different from peer) | peer |
| `CB_IPERF_STREAMS` | iperf3 parallel streams | 8 |
| `CB_RUNTIME` | per-fio-workload seconds | 30/60/120 by tier |
| `CB_SOAK_SECS` | deep-dive soak duration | 86400 |
| `CB_IDLE_SECS` / `CB_COOL_SECS` | idle-baseline / cooldown window | 3 |
| `CB_COMBINED_SECS` | combined max-load window | 20 |
| `CB_HPL_DIR` / `CB_HPL_PEAK_TFLOPS` | HPL working dir / theoretical peak for efficiency % | run dir |
| `CB_STREAM` / `CB_MLC` | paths to STREAM / Intel MLC binaries | `$PATH` |
| `CONFIRM_DESTRUCTIVE` | set to `1` only via the gated tool path | unset |

---

## 4. Register a peer for two-node network tests

Network validation needs a second machine on a trusted management/test network,
reachable by **SSH key** (no passwords, no unauthenticated listeners).

```bash
# from the system under test:
ssh-keygen -t ed25519              # if you don't have a key
ssh-copy-id lab@10.0.0.38          # install your key on the peer
ssh lab@10.0.0.38 true            # must succeed non-interactively (BatchMode)

# the peer needs the responder tools:
ssh lab@10.0.0.38 'sudo apt install -y iperf3 perftest'
```

Then, via the agent / MCP:

```
register_peer(host="10.0.0.38", ssh_user="lab", role="responder")
start_network_validation(local_iface="enp1s0f0", peer="10.0.0.38",
                         tests=["tcp", "roce"], confirm_destructive=True)
```

CoreBench starts the responder on the peer over SSH, runs the initiator locally,
and records the RoCE fabric prerequisites (link/MTU/PFC/ECN). The peer is a
responder only — it is never wiped or reconfigured.

---

## 5. (Optional) enable firmware tooling — Tier D, irreversible

Off by default. To allow `flash_firmware`, set in `config.json`:

```json
{ "enable_firmware_tools": true }
```

Even then, every flash requires the image's **SHA-256** (verified against the
file) **and** both `confirm_destructive=True` and `i_understand_brick_risk=True`.
A bad flash can brick the component — ensure stable power. There is no assumed
rollback.

---

## 6. Per-campaign config

Edit `config.json` for branding and the datasheet `spec_targets` that drive the
auto spec-compliance scorecard. Leave `executive_summary` / `findings` /
`recommendations` empty to let the agent draft them from `results.json` via
`generate_report`.
