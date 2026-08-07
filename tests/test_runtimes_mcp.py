"""Verify the runtime self-update tools over MCP (dry-run only — non-mutating)."""
import asyncio
import sys

from fastmcp import Client

CONFIG = {
    "mcpServers": {
        "codecalc": {
            "command": sys.executable,
            "args": ["-m", "codecalc.server"],
            "env": {"PYTHONPATH": "."},
        }
    }
}


async def main():
    async with Client(CONFIG) as client:
        tools = await client.list_tools()
        print(f"tools ({len(tools)}): {[t.name for t in tools]}")

        r = await client.call_tool("runtimes_status", {})
        data = r.data
        print("runtimes_status: total =", data["summary"]["total"],
              "| updatable =", data["summary"]["updatable"],
              "| dry_run =", data["dry_run"])
        upd = {k: v for k, v in data["languages"].items() if v.get("updatable")}
        print("updatable:", {k: f"{v['current']} -> {v['latest']}" for k, v in upd.items()})

        r = await client.call_tool("update_runtimes", {"languages": "gradle,swift"})
        data = r.data
        print("update_runtimes (dry): dry_run =", data["dry_run"],
              "| msg =", data["message"][:50])
        g = data["languages"]["gradle"]
        print("gradle update_command:", g["update_command"])


asyncio.run(main())
