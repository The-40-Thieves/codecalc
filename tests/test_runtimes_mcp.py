"""Verify the runtime self-update tools over MCP (dry-run only — non-mutating)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mcp_client import data, over_stdio


async def main():
    async with over_stdio() as client:
        tools = (await client.list_tools()).tools
        print(f"tools ({len(tools)}): {[t.name for t in tools]}")

        r = await client.call_tool("runtimes_status", {})
        st = data(r)
        print("runtimes_status: total =", st["summary"]["total"],
              "| updatable =", st["summary"]["updatable"],
              "| dry_run =", st["dry_run"])
        upd = {k: v for k, v in st["languages"].items() if v.get("updatable")}
        print("updatable:", {k: f"{v['current']} -> {v['latest']}" for k, v in upd.items()})

        r = await client.call_tool("update_runtimes", {"languages": "gradle,swift"})
        up = data(r)
        print("update_runtimes (dry): dry_run =", up["dry_run"],
              "| msg =", up["message"][:50])
        g = up["languages"]["gradle"]
        print("gradle update_command:", g["update_command"])


asyncio.run(main())
