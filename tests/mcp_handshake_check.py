"""Deploy-readiness probe: connect to the CoreBench MCP server over stdio (the
real transport), initialize, list tools, call read-only tools, and confirm a
destructive tool refuses. Run with the venv python."""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_server.py")
EXPECTED = {
    "list_block_devices", "server_hardware_summary", "telemetry_snapshot", "list_runs",
    "get_run_status", "get_results", "start_validation", "capture_platform_settings",
    "apply_platform_settings", "restore_platform_settings", "flash_firmware",
    "register_peer", "start_network_validation", "generate_report",
}


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=[SERVER])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("initialize OK ->", init.serverInfo.name)
            tools = {t.name for t in (await session.list_tools()).tools}
            print(f"tools advertised: {len(tools)}")
            missing = EXPECTED - tools
            extra = tools - EXPECTED
            print("  missing:", sorted(missing) or "none")
            print("  unexpected:", sorted(extra) or "none")
            # every tool must have a non-empty description (agent-facing)
            descs = {t.name: (t.description or "") for t in (await session.list_tools()).tools}
            no_desc = [n for n, d in descs.items() if len(d) < 20]
            print("  tools w/ weak description:", no_desc or "none")

            async def call(name, args):
                r = await session.call_tool(name, args)
                txt = r.content[0].text if r.content else "{}"
                return json.loads(txt)

            ok = True
            for tn in ("list_block_devices", "list_runs", "telemetry_snapshot"):
                d = await call(tn, {})
                print(f"call {tn}: ok={d.get('ok')}")
                ok = ok and d.get("ok") is True
            # destructive gate must refuse
            g = await call("start_validation", {"drives": [], "tier": "acceptance", "confirm_destructive": False})
            print("start_validation(empty,no-confirm): refused =", g.get("ok") is False, "|", g.get("error", "")[:70])
            ok = ok and g.get("ok") is False
            # firmware gate must be disabled by default
            f = await call("flash_firmware", {"component": "bmc", "image_path": "x", "sha256": "y"})
            print("flash_firmware(default): refused =", f.get("ok") is False, "|", f.get("error", "")[:60])
            ok = ok and f.get("ok") is False

            verdict = ok and not missing and not no_desc
            print("\nHANDSHAKE RESULT:", "PASS" if verdict else "FAIL")
            return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
