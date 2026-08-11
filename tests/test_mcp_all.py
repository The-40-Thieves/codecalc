"""Every major MCP tool round-trips over stdio, and returns the RIGHT answer.

This file used to print each tool's output and exit 0 unconditionally — no
assertions, no conditionals. It caught a tool that crashed or changed shape and
nothing else: a `solve_linear` returning the wrong roots, or a `z3_check`
answering unsat for a satisfiable system, passed silently.

Each answer below is pinned to a value that is checkable by hand. Two are not,
and are treated differently rather than pinned to whatever this machine happened
to produce:

    benchmark            measures TIME, so its estimate moves under load
    compare_execution    picks a winner by TIME, so the winner moves under load

For those, the structure is asserted (every size measured, sizes doubling, a
winner drawn from the languages actually run, every run succeeding) and the
timing is not. A test
that pins a duration is a test that fails on a busy machine for a reason that
is not a defect — and one that pins nothing is what this file used to be.
"""

import ast
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mcp_client import data, over_stdio

REPO_ROOT = Path(__file__).resolve().parents[1]

FAILS: list[str] = []

#: What the structural analyser is allowed to conclude. Membership is asserted
#: rather than a specific value, for the timing-derived estimates only.
COMPLEXITY_ESTIMATES = {"O(1)", "O(log n)", "O(n)", "O(n log n)", "O(n^2)", "O(n^3)",
                        "O(2^n)", "O(n!)", "unknown"}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def declared_tool_names() -> list[str]:
    """Tool NAMES declared in server.py, the same way scripts/check_tool_names.py reads them.

    Derived rather than hardcoded so this tracks the gate instead of becoming a
    third number to keep in sync.

    Names, not a count. This used to return `sum(1 for line in src ... )` and the
    assertion below compared it to `len(names)`, which proves the same NUMBER of
    tools arrived and never that they are the same tools — it passes when one is
    lost and another gained. THE-811.

    A list rather than a set, because two decorators declaring the same name is
    the one silent registration path and a set would hide it.
    """
    src = (REPO_ROOT / "codecalc" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                    and dec.func.attr == "tool":
                name = node.name
                for kw in dec.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        name = str(kw.value.value)
                found.append(name)
    return found


async def main():
    async with over_stdio() as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
        # This exact line is parsed by ci-python.yml's round-trip step. Removing
        # it did not make that gate fail open — it reported that it had nothing
        # to assert on and failed the build, which is the behaviour it was
        # written for. Kept, and the assertion it protects is now made here too.
        print(f"tools ({len(names)}): {names}")
        # Sets, not counts, and the reason named correctly. The old detail said a
        # tool the SDK cannot schematise "is dropped at registration, silently".
        # It is not: Tool.from_function RAISES, so such a tool fails loudly at
        # import. The silent path is a DUPLICATE NAME, which add_tool discards
        # after logging a warning nothing reads. THE-810.
        declared = declared_tool_names()
        dupes = sorted({n for n in declared if declared.count(n) > 1})
        check("no tool name is declared twice in server.py", not dupes,
              f"-> {dupes}; add_tool keeps the first registration and discards the rest, "
              f"so the served surface loses a tool while the decorator count does not")
        # Unique declared names, deliberately: a duplicate collapses in a set, so
        # this comparison cannot see one and must not appear to. Printing the raw
        # 48-vs-47 here made a PASSING line read as a contradiction. Duplicates
        # are the assertion above; this one owns identity.
        missing, extra = sorted(set(declared) - set(names)), sorted(set(names) - set(declared))
        check("every tool declared in server.py reaches the client",
              not missing and not extra,
              f"-> {len(set(declared))} unique declared, {len(names)} served; "
              f"declared but never served: {missing or 'none'}; "
              f"served but not declared: {extra or 'none'}")

        # ── list_languages ────────────────────────────────────────────────
        langs = data(await client.call_tool("list_languages", {}))
        check("list_languages returns entries", isinstance(langs, list) and langs,
              f"-> {type(langs).__name__}")
        by_name = {entry["name"]: entry for entry in langs}
        check("c is present and available", by_name.get("c", {}).get("available") is True,
              f"-> {by_name.get('c')}")
        check("every entry says whether it is available",
              all(isinstance(e.get("available"), bool) for e in langs))

        # ── execute_code ──────────────────────────────────────────────────
        r = data(await client.call_tool(
            "execute_code", {"language": "python3", "code": "print('mcp ok', 6*7)"}))
        check("execute_code returns 6*7 = 42", (r.get("stdout") or "").strip() == "mcp ok 42",
              f"-> {(r.get('stdout') or '').strip()!r}")

        # ── evaluate_expression ───────────────────────────────────────────
        r = data(await client.call_tool("evaluate_expression",
                                        {"expression": "integrate(x**2, x)"}))
        # sympy writes it as x**3/3; accept either spelling of the same integral.
        result = str(r.get("result") or r.get("value") or r)
        check("evaluate_expression integrates x^2 to x^3/3",
              "x**3/3" in result.replace(" ", "") or "x^3/3" in result.replace(" ", ""),
              f"-> {result[:60]}")

        # ── truth_table ───────────────────────────────────────────────────
        r = data(await client.call_tool("truth_table", {"expression": "a and b or not c"}))
        check("truth_table: three variables give 8 rows", r.get("row_count") == 8,
              f"-> {r.get('row_count')}")
        check("truth_table: the expression is satisfiable", r.get("satisfiable") is True)
        # a·b + c̄ is true in 5 of 8 assignments. Worked by hand: all four rows
        # with c false, plus (a,b,c) = (T,T,T).
        rows = r.get("rows") or []
        check("truth_table: true in exactly 5 of 8 rows",
              sum(1 for row in rows if row.get("result")) == 5,
              f"-> {sum(1 for row in rows if row.get('result'))}")

        # ── z3_check ──────────────────────────────────────────────────────
        r = data(await client.call_tool(
            "z3_check",
            {"smt2": "(declare-const x Int)(assert (> x 5))(assert (< x 10))(check-sat)"}))
        check("z3_check: sat", r.get("result") == "sat", f"-> {r.get('result')}")
        model = r.get("model") or {}
        check("z3_check: the model actually satisfies 5 < x < 10",
              "x" in model and 5 < int(model["x"]) < 10, f"-> {model}")

        # ── solve_linear ──────────────────────────────────────────────────
        r = data(await client.call_tool(
            "solve_linear", {"system": "x + y = 10; x - y = 2", "variables": "x, y"}))
        solutions = str(r.get("solutions"))
        # x + y = 10, x - y = 2  ->  x = 6, y = 4. One answer, and it is checkable.
        check("solve_linear: x = 6", "x: 6" in solutions, f"-> {solutions[:60]}")
        check("solve_linear: y = 4", "y: 4" in solutions, f"-> {solutions[:60]}")
        check("solve_linear: exactly one solution", r.get("count") == 1, f"-> {r.get('count')}")

        # ── analyze_complexity ────────────────────────────────────────────
        r = data(await client.call_tool(
            "analyze_complexity",
            {"code": "for i in range(n):\n    for j in range(n):\n        pass"}))
        check("analyze_complexity: nested loops are O(n^2)", r.get("estimate") == "O(n^2)",
              f"-> {r.get('estimate')}")

        # ── benchmark — TIMING, so structure only ─────────────────────────
        sizes = [5000, 10000, 20000, 40000]
        r = data(await client.call_tool(
            "benchmark",
            {"code": "import sys\nn=int(sys.stdin.readline())\ns=0\nfor i in range(n): s+=i\nprint(s)",
             "sizes": ",".join(str(s) for s in sizes), "timeout": 15}))
        check("benchmark returns a recognised estimate",
              r.get("estimate") in COMPLEXITY_ESTIMATES, f"-> {r.get('estimate')!r}")
        # Coverage is asserted on `runs`, not on the ratio count. A first draft
        # demanded len(sizes) - 1 ratios and failed: the classifier subtracts the
        # smallest measurement as a baseline, so the smallest size's corrected
        # time is exactly 0 and its gap is deliberately skipped as sub-noise.
        # At most len(sizes) - 2 ratios can ever exist. The code was right and
        # the assertion was wrong; demanding the extra ratio would have meant
        # "fixing" a deliberate noise correction.
        runs = r.get("runs") or []
        check("benchmark measured every size", len(runs) == len(sizes),
              f"-> {len(runs)} runs for {len(sizes)} sizes")
        check("every benchmark run succeeded", all(run.get("ok") for run in runs),
              f"-> {[(run.get('n'), run.get('ok')) for run in runs]}")
        check("every benchmark run has a duration",
              all(isinstance(run.get("duration_ms"), int) for run in runs),
              f"-> {[run.get('duration_ms') for run in runs]}")
        # Sizes double, whether or not auto-scaling raised them first.
        ns = [run.get("n") for run in runs]
        check("benchmark sizes double across runs",
              all(ns[i + 1] == 2 * ns[i] for i in range(len(ns) - 1)), f"-> {ns}")

        ratios = r.get("doubling_ratios") or []
        check("benchmark reports no more ratios than gaps exist",
              len(ratios) <= len(sizes) - 1, f"-> {len(ratios)} ratios for {len(sizes)} sizes")
        # Finite and non-negative, NOT strictly positive. These are measured
        # durations with a noise baseline subtracted, so a fast runner can
        # legitimately produce 0.0 — observed on CI as [0.12, 0.0]. Requiring
        # >0 pinned a timing value, which this file's own header says not to do.
        check("benchmark ratios are finite and non-negative",
              ratios and all(isinstance(x, (int, float)) and x >= 0
                             and x == x and x != float("inf") for x in ratios),
              f"-> {ratios}")

        # ── compare_execution — TIMING, so structure only ─────────────────
        # Only languages this machine actually HAS. A runner without ruby or
        # node is an environment fact, not a defect, and asserting "every
        # language produced 42" against a missing runtime tests the runner
        # rather than the tool.
        candidates = {"python3": "print(6*7)", "node": "console.log(6*7)", "ruby": "puts 6*7"}
        snippets = {k: v for k, v in candidates.items()
                    if by_name.get(k, {}).get("available")}
        check("at least two languages are available to compare",
              len(snippets) >= 2, f"-> available: {sorted(snippets)}")
        r = data(await client.call_tool("compare_execution", {"snippets": snippets}))
        check("compare_execution ran every snippet", r.get("count") == len(snippets),
              f"-> {r.get('count')}")
        # The winner varies with load; that it is one of the languages actually
        # run does not. A `fastest` naming something absent — or a crashed run —
        # is the defect this can catch.
        check("compare_execution's winner is one of the languages run",
              r.get("fastest") in snippets, f"-> {r.get('fastest')!r}")
        results = r.get("results") or []
        # THE-802: `node` intermittently comes back with empty stdout and
        # ok=false on windows-latest. Three occurrences went by reporting only
        # (language, stdout) and (language, ok), which cannot separate "node
        # never started" from "node started and printed nothing" from "node was
        # killed by the deadline". Those are different bugs with different
        # fixes, and the executor ALREADY distinguishes them — `spawn failed:
        # {e}`, `cannot create I/O files in {dir}`, and a synthesised message
        # when a timeout leaves stderr empty, plus exit_code and timed_out on
        # every row. compare_execution returns all of it. The detail string
        # threw it away, so each occurrence cost a rerun and told us nothing.
        #
        # The PASS detail is deliberately left exactly as it was: this file's
        # stdout is parsed by ci-python.yml's round-trip step, and a green run
        # should not change shape. Only a FAILING run pays for the detail.
        def _diag(rows: list) -> list:
            return [{"language": x.get("language"),
                     "ok": x.get("ok"),
                     "stdout": (x.get("stdout") or "").strip()[:80],
                     "stderr": (x.get("stderr") or "").strip()[:300],
                     "exit_code": x.get("exit_code"),
                     "timed_out": x.get("timed_out"),
                     "duration_ms": x.get("duration_ms")} for x in rows]

        produced_42 = all((res.get("stdout") or "").strip() == "42" for res in results)
        check("every language produced 42", produced_42,
              f"-> {[(res.get('language'), (res.get('stdout') or '').strip()) for res in results]}"
              if produced_42 else f"-> {_diag(results)}")
        all_ok = all(res.get("ok") for res in results)
        check("every run succeeded", all_ok,
              f"-> {[(res.get('language'), res.get('ok')) for res in results]}"
              if all_ok else f"-> {_diag(results)}")


asyncio.run(main())
print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== EVERY TOOL RETURNED THE RIGHT ANSWER ===")
sys.exit(1 if FAILS else 0)
