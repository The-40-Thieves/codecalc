"""CODECALC_TOOLS: registering a SLICE of the 52-tool surface.

The tool-definition token cost (README: "Tool-definition token cost") is real,
and the fix that stayed unbuilt on purpose is a facade
(docs/design/2026-08-10-tool-facade.md) — collapsing 52 typed tools behind one
dispatcher erases per-tool schemas and per-tool approval boundaries. What this
adds instead is server-side REGISTRATION filtering: a tool outside the active
group set is never handed to `_mcp_tool(...)` at all, so it is absent from
`tools/list` AND rejected by `tools/call` — the same "does not exist here"
that an operator gets from disabling a plugin, not a hidden extra hop.

Each case below needs a FRESH process: `codecalc.server` decides its
registered surface once, at import, from `CODECALC_TOOLS` — re-importing an
already-imported module in this process would just return the cached module
object with whatever the FIRST import decided. So every case spawns
`python -c` with a controlled environment and reads back what that process's
server actually registered, the same pattern tests/test_audit_log.py uses for
"importing codecalc.server must be free of side effects".

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── the declared side, derived independently of server.py's own bookkeeping ──
# Same AST walk scripts/check_tool_groups.py and scripts/check_tool_names.py
# use: reading TOOL_GROUPS back out of an imported module would just be the
# code checking itself. This is a SEPARATE derivation, from source text.

def _declared_tool_groups() -> dict[str, str]:
    src = (REPO_ROOT / "codecalc" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "tool"):
                continue
            name = node.name
            group = None
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name = str(kw.value.value)
                if kw.arg == "group" and isinstance(kw.value, ast.Constant):
                    group = kw.value.value
            out[name] = group
    return out


DECLARED = _declared_tool_groups()


# ── run a fresh import of codecalc.server under a controlled CODECALC_TOOLS ──

_PROBE = (
    "import json\n"
    "from codecalc import server\n"
    "tools = server.mcp._tool_manager._tools\n"
    "print(json.dumps({\n"
    "    'registered': sorted(tools),\n"
    "    'active_groups': sorted(server._ACTIVE_GROUPS),\n"
    "    'declared_groups': server.TOOL_GROUPS,\n"
    "    'schema_of_one': (\n"
    "        tools[sorted(tools)[0]].parameters if tools else None\n"
    "    ),\n"
    "}))\n"
)


def _probe(codecalc_tools: str | None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if codecalc_tools is None:
        env.pop("CODECALC_TOOLS", None)
    else:
        env["CODECALC_TOOLS"] = codecalc_tools
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )


# ── 1. no env set: every declared tool registers ─────────────────────────────
default = _probe(None)
check("unset CODECALC_TOOLS: the probe subprocess succeeds",
      default.returncode == 0, f"-> rc={default.returncode} stderr={default.stderr[:400]!r}")
default_data = json.loads(default.stdout) if default.returncode == 0 else {}
check("unset CODECALC_TOOLS: exactly the declared tools register (none missing, none extra)",
      default.returncode == 0 and set(default_data.get("registered", [])) == set(DECLARED),
      f"-> {len(default_data.get('registered', []))} registered vs {len(DECLARED)} declared; "
      f"missing={sorted(set(DECLARED) - set(default_data.get('registered', [])))}, "
      f"extra={sorted(set(default_data.get('registered', [])) - set(DECLARED))}")
check("unset CODECALC_TOOLS: registers all 52",
      default.returncode == 0 and len(default_data.get("registered", [])) == 52,
      f"-> {len(default_data.get('registered', []))}")
check("unset CODECALC_TOOLS: every known group is active",
      default.returncode == 0
      and set(default_data.get("active_groups", []))
      == {"calculator", "verification", "execution", "sessions", "analysis", "admin"},
      f"-> {default_data.get('active_groups')}")


# ── 2. group mapping is complete: every declared tool has a valid group ──────
KNOWN_GROUPS = {"calculator", "verification", "execution", "sessions", "analysis", "admin"}
check("every declared tool names a group in the AST", None not in DECLARED.values(),
      f"-> ungrouped: {sorted(n for n, g in DECLARED.items() if g is None)}")
check("every declared group is a KNOWN group", all(g in KNOWN_GROUPS for g in DECLARED.values()),
      f"-> unknown: {sorted(g for g in DECLARED.values() if g not in KNOWN_GROUPS)}")
declared_groups_live = default_data.get("declared_groups", {})
check("server.TOOL_GROUPS (live) agrees with the AST derivation",
      default.returncode == 0 and declared_groups_live == DECLARED,
      f"-> live={declared_groups_live}\n    ast ={DECLARED}")


# ── 3. CODECALC_TOOLS=calculator: only that group registers, the rest absent ─
calc_expected = {n for n, g in DECLARED.items() if g == "calculator"}
calc = _probe("calculator")
check("CODECALC_TOOLS=calculator: the probe subprocess succeeds",
      calc.returncode == 0, f"-> rc={calc.returncode} stderr={calc.stderr[:400]!r}")
calc_data = json.loads(calc.stdout) if calc.returncode == 0 else {}
check("CODECALC_TOOLS=calculator: registers exactly the calculator-group tools",
      calc.returncode == 0 and set(calc_data.get("registered", [])) == calc_expected,
      f"-> {sorted(calc_data.get('registered', []))} vs expected {sorted(calc_expected)}")
check("CODECALC_TOOLS=calculator: a non-calculator tool (execute_code) is ABSENT, not erroring",
      calc.returncode == 0 and "execute_code" not in calc_data.get("registered", []))
check("CODECALC_TOOLS=calculator: only 'calculator' is active",
      calc.returncode == 0 and calc_data.get("active_groups") == ["calculator"],
      f"-> {calc_data.get('active_groups')}")
# Schema intact for what DOES register: a facade would erase this by dispatching through one
# `call_capability(name, args)`, so it is worth pinning that a present tool still has real
# properties in its parameters schema, not an untyped passthrough object.
schema = calc_data.get("schema_of_one")
check("CODECALC_TOOLS=calculator: a registered tool keeps its own typed parameters schema",
      calc.returncode == 0 and isinstance(schema, dict) and "properties" in schema,
      f"-> {schema}")


# ── 4. preset name resolves to its groups: CODECALC_TOOLS=core == calculator ─
core = _probe("core")
check("CODECALC_TOOLS=core: the probe subprocess succeeds",
      core.returncode == 0, f"-> rc={core.returncode} stderr={core.stderr[:400]!r}")
core_data = json.loads(core.stdout) if core.returncode == 0 else {}
check("preset 'core' registers the same set as the literal group 'calculator'",
      core.returncode == 0 and set(core_data.get("registered", [])) == calc_expected,
      f"-> {sorted(core_data.get('registered', []))} vs {sorted(calc_expected)}")


# ── 5. two groups, comma-separated, union correctly ───────────────────────────
two = _probe("calculator,admin")
two_expected = {n for n, g in DECLARED.items() if g in ("calculator", "admin")}
check("CODECALC_TOOLS=calculator,admin: the probe subprocess succeeds",
      two.returncode == 0, f"-> rc={two.returncode} stderr={two.stderr[:400]!r}")
two_data = json.loads(two.stdout) if two.returncode == 0 else {}
check("CODECALC_TOOLS=calculator,admin: registers the union of both groups",
      two.returncode == 0 and set(two_data.get("registered", [])) == two_expected,
      f"-> {sorted(two_data.get('registered', []))} vs expected {sorted(two_expected)}")


# ── 6. an unknown group/preset name is a LOUD startup error, not a silent 0 or all ──
bogus = _probe("not_a_real_group")
check("CODECALC_TOOLS=<unknown>: the import fails rather than silently registering something",
      bogus.returncode != 0, f"-> rc={bogus.returncode}")
check("CODECALC_TOOLS=<unknown>: the error names the bad value",
      "not_a_real_group" in bogus.stderr, f"-> stderr={bogus.stderr[-500:]!r}")
check("CODECALC_TOOLS=<unknown>: the error names the known groups/presets (actionable, not opaque)",
      "calculator" in bogus.stderr and "core" in bogus.stderr,
      f"-> stderr={bogus.stderr[-500:]!r}")

# A typo that happens to collide with nothing must not silently fall back to
# "everything" OR "nothing" — both are covered above by the same assertion
# shape, since `_probe` never installed a fallback branch for either case.
blank_entry = _probe("calculator,,admin")  # a stray empty entry from a trailing comma
check("a stray empty CODECALC_TOOLS entry (trailing comma) is ignored, not treated as unknown",
      blank_entry.returncode == 0, f"-> rc={blank_entry.returncode} stderr={blank_entry.stderr[:400]!r}")


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL TOOL-GROUP TESTS PASS ===")
sys.exit(1 if FAILS else 0)
