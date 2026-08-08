"""The MCP server over stdio: connect, list tools, and check what they answer.

This file used to print tool output and exit 0 unconditionally. It had no
assertions and no conditionals, so it could only fail if a call raised —
measured: replacing `runtimes_status`'s total with -999 left the equivalent
script exiting 0 and printing `total = -999` happily. A crash was detected; a
wrong answer was not.

Every check here therefore pins a value that is knowably correct
(sum(range(101)) is 5050, `a xor b` has exactly 4 rows) rather than merely
non-empty. Timing-dependent results are asserted structurally, because a
benchmark that asserts a duration is a test that fails on a busy machine.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mcp_client import data, over_stdio

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import registry

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


async def main():
    async with over_stdio() as client:
        tools = (await client.list_tools()).tools
        names = {t.name for t in tools}
        # A floor, not a snapshot: adding a tool should not fail this, losing
        # one should. scripts/check_claims.py pins the exact count against the
        # README; this asserts the server really serves them over the wire.
        check("stdio server lists its tools", len(names) >= 48, f"-> {len(names)}")
        for required in ("execute_code", "truth_table", "z3_check", "analyze_complexity",
                         "list_languages"):
            check(f"tool {required!r} is served", required in names)

        langs = data(await client.call_tool("list_languages", {}))
        check("list_languages returns a list", isinstance(langs, list), f"-> {type(langs).__name__}")
        check("list_languages covers the whole registry",
              len(langs) == len(registry.LANGUAGES),
              f"-> {len(langs)} over the wire, {len(registry.LANGUAGES)} in the registry")

        r = data(await client.call_tool(
            "execute_code",
            {"language": "python3", "code": "print('mcp hello', sum(range(101)))"}))
        check("execute_code succeeds", r.get("ok") is True, f"-> {str(r)[:80]}")
        # 5050, not "some output": the arithmetic is the point.
        check("execute_code returns the RIGHT answer",
              (r.get("stdout") or "").strip() == "mcp hello 5050",
              f"-> {(r.get('stdout') or '').strip()!r}")
        check("execute_code reports a verdict", r.get("verdict") == "OK", f"-> {r.get('verdict')}")

        r = data(await client.call_tool("truth_table", {"expression": "a xor b"}))
        check("truth_table: two variables give 4 rows", r.get("row_count") == 4,
              f"-> {r.get('row_count')}")
        check("truth_table: a xor b is satisfiable", r.get("satisfiable") is True)
        rows = r.get("rows") or []
        true_rows = [row for row in rows if row.get("result")]
        check("truth_table: xor is true in exactly 2 of 4 rows",
              len(true_rows) == 2, f"-> {len(true_rows)}")

        r = data(await client.call_tool(
            "z3_check",
            {"smt2": "(declare-const x Int)(assert (> x 5))(assert (< x 10))(check-sat)"}))
        check("z3_check: 5 < x < 10 is sat", r.get("result") == "sat", f"-> {r.get('result')}")
        model = r.get("model") or {}
        check("z3_check: the model satisfies the constraints",
              "x" in model and 5 < int(model["x"]) < 10, f"-> {model}")

        r = data(await client.call_tool(
            "analyze_complexity",
            {"code": "for i in range(n):\n    for j in range(n):\n        pass",
             "language": "python3"}))
        check("analyze_complexity: two nested loops are O(n^2)",
              r.get("estimate") == "O(n^2)", f"-> {r.get('estimate')}")
        check("  ...and it came from the parser, not the regex heuristic",
              r.get("analysis") == "tree-sitter", f"-> {r.get('analysis')}")


asyncio.run(main())
print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== MCP STDIO SURFACE OK ===")
sys.exit(1 if FAILS else 0)
