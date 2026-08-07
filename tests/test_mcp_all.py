"""Verify all 9 MCP tools round-trip over stdio, including the new ones."""
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

        r = await client.call_tool("list_languages", {})
        data = r.data if hasattr(r, "data") else r
        print("list_languages: entries =", len(data), "| c available =",
              [l for l in data if l["name"] == "c"][0]["available"])

        r = await client.call_tool("execute_code", {"language": "python3", "code": "print('mcp ok', 6*7)"})
        print("execute_code:", r.data["stdout"].strip())

        r = await client.call_tool("evaluate_expression", {"expression": "integrate(x**2, x)"})
        print("evaluate_expression:", str(r.data)[:90])

        r = await client.call_tool("truth_table", {"expression": "a and b or not c"})
        print("truth_table: rows =", r.data["row_count"], "| satisfiable =", r.data["satisfiable"])

        r = await client.call_tool("z3_check", {"smt2": "(declare-const x Int)(assert (> x 5))(assert (< x 10))(check-sat)"})
        print("z3_check:", r.data["result"], r.data["model"])

        r = await client.call_tool("solve_linear", {"system": "x + y = 10; x - y = 2", "variables": "x, y"})
        print("solve_linear:", r.data["solutions"])

        r = await client.call_tool("analyze_complexity", {"code": "for i in range(n):\n    for j in range(n):\n        pass"})
        print("analyze_complexity:", r.data["estimate"])

        r = await client.call_tool(
            "benchmark",
            {"code": "import sys\nn=int(sys.stdin.readline())\ns=0\nfor i in range(n): s+=i\nprint(s)",
             "sizes": "5000,10000,20000,40000", "timeout": 15},
        )
        print("benchmark:", r.data["estimate"], "| ratios:", r.data["doubling_ratios"])

        r = await client.call_tool(
            "compare_execution",
            {"snippets": {"python3": "print(6*7)", "node": "console.log(6*7)", "ruby": 'puts 6*7'}},
        )
        print("compare_execution:", r.data["count"], "langs | fastest:", r.data["fastest"])


import asyncio

asyncio.run(main())
