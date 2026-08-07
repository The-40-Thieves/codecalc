"""Verify the FastMCP server over stdio: connect, list tools, call several."""
import asyncio
import sys

from fastmcp import Client


async def main():
    config = {
        "mcpServers": {
            "codecalc": {
                "command": sys.executable,
                "args": ["-m", "codecalc.server"],
                "env": {"PYTHONPATH": "."},
            }
        }
    }
    async with Client(config) as client:
        tools = await client.list_tools()
        print(f"tools ({len(tools)}): {[t.name for t in tools]}")

        r = await client.call_tool("list_languages", {})
        langs = r.data if hasattr(r, "data") else r
        print("languages sample:", langs[:4] if isinstance(langs, list) else langs)

        r = await client.call_tool(
            "execute_code", {"language": "python3", "code": "print('mcp hello', sum(range(101)))"}
        )
        print("execute_code:", r)

        r = await client.call_tool(
            "truth_table", {"expression": "a xor b"}
        )
        print("truth_table:", r)

        r = await client.call_tool(
            "z3_check", {"smt2": "(declare-const x Int)(assert (> x 5))(assert (< x 10))(check-sat)"}
        )
        print("z3_check:", r)

        r = await client.call_tool(
            "analyze_complexity", {"code": "for i in range(n):\n    for j in range(n):\n        pass", "language": "python3"}
        )
        print("complexity:", r)


asyncio.run(main())
