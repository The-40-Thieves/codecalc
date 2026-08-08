"""Verify the MCP server over stdio: connect, list tools, call several."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mcp_client import data, over_stdio


async def main():
    async with over_stdio() as client:
        tools = (await client.list_tools()).tools
        print(f"tools ({len(tools)}): {[t.name for t in tools]}")

        r = await client.call_tool("list_languages", {})
        langs = data(r)
        print("languages sample:", langs[:4] if isinstance(langs, list) else langs)

        r = await client.call_tool(
            "execute_code", {"language": "python3", "code": "print('mcp hello', sum(range(101)))"}
        )
        print("execute_code:", str(data(r))[:110])

        r = await client.call_tool(
            "truth_table", {"expression": "a xor b"}
        )
        print("truth_table:", str(data(r))[:110])

        r = await client.call_tool(
            "z3_check", {"smt2": "(declare-const x Int)(assert (> x 5))(assert (< x 10))(check-sat)"}
        )
        print("z3_check:", str(data(r))[:110])

        r = await client.call_tool(
            "analyze_complexity", {"code": "for i in range(n):\n    for j in range(n):\n        pass", "language": "python3"}
        )
        print("complexity:", str(data(r))[:110])


asyncio.run(main())
