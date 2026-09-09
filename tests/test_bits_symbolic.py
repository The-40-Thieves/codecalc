"""The 2026-09-08 merge: `bits`/`symbolic` replace four tools each.

docs/design/2026-08-10-tool-facade.md's "Scope amendment on tool count"
authorised a same-SIGNATURE-union merge of `bit_analysis`/`bitop`/
`int_widths`/`base_repr` into `bits(mode=...)` and `solve_expression`/
`solve_linear`/`simplify_expression`/`limit_expression` into
`symbolic(op=...)` — NOT the generic `call_capability(name, args)` facade
that same document's §2 rejected. The eight old names stayed registered as
thin, deprecated aliases for one minor release (0.11.0) and were retired in
0.12.0 (CHANGELOG.md): the ORIGINAL functions (`codecalc/exact.py`'s
`bit_analysis`/`bitop`/`int_widths`/`base_repr`/`solve_expression`/
`limit_expression`/`simplify_expression`, `codecalc/logic.py`'s
`solve_linear`) are unchanged and still what `bits`/`symbolic` call — only
the eight MCP-level wrapper tools bearing their old names are gone.

This file asserts the retirement is honest, both ways:

  * every mode/op still produces exactly its pre-merge function's own
    result, plus one additive selector key (`mode` on `bits`, `op` on
    `symbolic`) — checked against the underlying `exact`/`logic` functions
    directly, not the deleted wrapper tools;
  * a mode/parameter mismatch is a closed-enum `validation` error naming the
    missing or extra field, not a Python TypeError with no code;
  * none of the eight old names is a `server` module attribute, a
    `TOOL_GROUPS` member, or a name `tools/list` serves any more — calling
    one over the wire returns the SDK's own unknown-tool result, never a
    result shaped like the tool used to return;
  * the tool count matches `_helpers.expected_tool_count()` (49 — the eight
    retired names no longer count) and `bits`/`symbolic` keep the
    calculator group's read-only annotation.

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention
(see CONTRIBUTING.md and tests/conftest.py for why).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import expected_tool_count
from _mcp_client import data, in_process, over_stdio

from codecalc import errors, exact, logic, server

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def _without(d: dict, *keys: str) -> dict:
    return {k: v for k, v in d.items() if k not in keys}


#: The eight retired MCP tool names -> their replacement call, for the
#: "gone everywhere" checks below. The underlying `exact`/`logic` FUNCTIONS
#: of the same names still exist and are exercised directly in BITS_CASES/
#: SYMBOLIC_CASES — only the `@mcp.tool()` wrappers in server.py are gone.
_BITS_ALIASES = {
    "bit_analysis": "bits(mode=\"analysis\")",
    "bitop": "bits(mode=\"op\")",
    "int_widths": "bits(mode=\"widths\")",
    "base_repr": "bits(mode=\"repr\")",
}
_SYMBOLIC_ALIASES = {
    "solve_expression": "symbolic(op=\"solve\")",
    "solve_linear": "symbolic(op=\"solve_linear\")",
    "simplify_expression": "symbolic(op=\"simplify\")",
    "limit_expression": "symbolic(op=\"limit\")",
}
ALL_ALIASES = {**_BITS_ALIASES, **_SYMBOLIC_ALIASES}


# ── direct-call matrix: bits(mode=...) vs the ORIGINAL exact.py function ───
# `mode` is the one additive key `bits` adds — stripped before comparing so
# the rest of the dict is asserted byte-identical to what the retired tool
# used to return for that mode.
BITS_CASES = [
    ("analysis", {"n": 202}, lambda: exact.bit_analysis(202)),
    ("analysis", {"n": 256, "align": 16}, lambda: exact.bit_analysis(256, 16)),
    ("analysis", {"n": 0}, lambda: exact.bit_analysis(0)),
    ("op", {"a": 12, "op": "xor", "b": 10, "width": 8}, lambda: exact.bitop(12, "xor", 10, 8)),
    ("op", {"a": 1, "op": "not"}, lambda: exact.bitop(1, "not", None, 64)),
    ("op", {"a": 1, "op": "shl", "b": 256, "width": 8},
     lambda: exact.bitop(1, "shl", 256, 8)),
    ("op", {"a": 0x80, "op": "sar", "b": 1, "width": 8},
     lambda: exact.bitop(0x80, "sar", 1, 8)),
    ("widths", {"n": 300}, lambda: exact.int_widths(300)),
    ("widths", {"n": -200}, lambda: exact.int_widths(-200)),
    ("widths", {"n": 2 ** 60}, lambda: exact.int_widths(2 ** 60)),
    ("repr", {"n": 3000000000, "width": 32}, lambda: exact.base_repr(3000000000, 32)),
    ("repr", {"n": 255}, lambda: exact.base_repr(255)),
    ("repr", {"n": -5, "width": 8}, lambda: exact.base_repr(-5, 8)),
]

for mode, kwargs, original_call in BITS_CASES:
    merged = server.bits(mode=mode, **kwargs)
    original = original_call()
    check(f"bits(mode={mode!r}, {kwargs}) carries mode={mode!r}",
          merged.get("mode") == mode, f"-> {merged.get('mode')!r}")
    check(f"bits(mode={mode!r}, {kwargs}) == the original exact.py function's "
          f"result, plus mode",
          _without(merged, "mode", "contract_version") == original,
          f"-> merged={merged}\n     original={original}")

SYMBOLIC_CASES = [
    ("solve", {"expr": "x**2 - 4 = 0"}, lambda: exact.solve_expression("x**2 - 4 = 0", "x")),
    ("solve", {"expr": "2*x + 1 = 7", "var": "x"},
     lambda: exact.solve_expression("2*x + 1 = 7", "x")),
    ("solve_linear", {"system": "x + y = 10; x - y = 2", "variables": "x, y"},
     lambda: logic.solve_linear("x + y = 10; x - y = 2", ["x", "y"])),
    ("simplify", {"expr": "x**2 + 2*x + 1"}, lambda: exact.simplify_expression("x**2 + 2*x + 1")),
    ("limit", {"expr": "n*log(n)/n**2", "var": "n"},
     lambda: exact.limit_expression("n*log(n)/n**2", "n", "oo")),
    ("limit", {"expr": "1/x"}, lambda: exact.limit_expression("1/x", "x", "oo")),
]

for op, kwargs, original_call in SYMBOLIC_CASES:
    merged = server.symbolic(op=op, **kwargs)
    original = original_call()
    check(f"symbolic(op={op!r}, {kwargs}) carries op={op!r}",
          merged.get("op") == op, f"-> {merged.get('op')!r}")
    check(f"symbolic(op={op!r}, {kwargs}) == the original function's result, plus op",
          _without(merged, "op", "contract_version") == original,
          f"-> merged={merged}\n     original={original}")


# ── mode/parameter validation: closed-enum, names the field ────────────────
BITS_VALIDATION_CASES = [
    ("unknown mode", {"mode": "bogus", "n": 1}, "unknown mode"),
    ("analysis missing n", {"mode": "analysis"}, "requires n"),
    ("analysis extra a", {"mode": "analysis", "n": 5, "a": 1}, "does not accept a"),
    ("op missing a/op", {"mode": "op"}, "requires a, op"),
    ("op extra n", {"mode": "op", "a": 1, "op": "not", "n": 5}, "does not accept n"),
    ("widths missing n", {"mode": "widths"}, "requires n"),
    ("widths extra align", {"mode": "widths", "n": 1, "align": 4}, "does not accept align"),
    ("repr missing n", {"mode": "repr"}, "requires n"),
    ("repr extra a", {"mode": "repr", "n": 1, "a": 2}, "does not accept a"),
]
for label, kwargs, fragment in BITS_VALIDATION_CASES:
    result = server.bits(**kwargs)
    check(f"bits validation [{label}]: ok is False", result.get("ok") is False, f"-> {result}")
    check(f"bits validation [{label}]: code is validation",
          result.get("code") == errors.VALIDATION, f"-> {result.get('code')}")
    check(f"bits validation [{label}]: message names the field ({fragment!r})",
          fragment in result.get("error", ""), f"-> {result.get('error')!r}")

SYMBOLIC_VALIDATION_CASES = [
    ("unknown op", {"op": "bogus"}, "unknown op"),
    ("solve missing expr", {"op": "solve"}, "requires expr"),
    ("solve_linear missing variables", {"op": "solve_linear", "system": "x=1"},
     "requires variables"),
    ("solve_linear missing both", {"op": "solve_linear"}, "requires system, variables"),
    ("simplify extra var", {"op": "simplify", "expr": "x", "var": "x"}, "does not accept var"),
    ("limit missing expr", {"op": "limit"}, "requires expr"),
    ("solve extra system", {"op": "solve", "expr": "x=1", "system": "x=1"},
     "does not accept system"),
]
for label, kwargs, fragment in SYMBOLIC_VALIDATION_CASES:
    result = server.symbolic(**kwargs)
    check(f"symbolic validation [{label}]: ok is False", result.get("ok") is False, f"-> {result}")
    check(f"symbolic validation [{label}]: code is validation",
          result.get("code") == errors.VALIDATION, f"-> {result.get('code')}")
    check(f"symbolic validation [{label}]: message names the field ({fragment!r})",
          fragment in result.get("error", ""), f"-> {result.get('error')!r}")


# ── the eight retired aliases: gone from every surface, not just one ───────
for name in ALL_ALIASES:
    check(f"{name} is no longer a `server` module attribute",
          not hasattr(server, name))
    check(f"{name} is no longer in TOOL_GROUPS",
          name not in server.TOOL_GROUPS)
    check(f"{name} is no longer registered on the default MCP server",
          name not in server.mcp._tool_manager._tools)

check("bits/symbolic keep the calculator group",
      all(server.TOOL_GROUPS.get(n) == "calculator" for n in ("bits", "symbolic")),
      f"-> {[n for n in ('bits', 'symbolic') if server.TOOL_GROUPS.get(n) != 'calculator']}")

_CALC_ANNOTATIONS = server.GROUP_ANNOTATIONS["calculator"]
for name in ("bits", "symbolic"):
    ann = server.mcp._tool_manager._tools[name].annotations
    check(f"{name} keeps the calculator group's read-only annotation "
          "(no per-tool override needed for a pure calculator merge)",
          ann == _CALC_ANNOTATIONS, f"-> {ann}")
    check(f"{name} has no bespoke TOOL_ANNOTATION_OVERRIDES entry",
          name not in server.TOOL_ANNOTATION_OVERRIDES)

# ── tool count: README's gated total (the 8 retired names no longer count) ─
check(f"{expected_tool_count()} tools declared in TOOL_GROUPS",
      len(server.TOOL_GROUPS) == expected_tool_count(), f"-> {len(server.TOOL_GROUPS)}")
check(f"{expected_tool_count()} tools actually registered (default CODECALC_TOOLS)",
      len(server.mcp._tool_manager._tools) == expected_tool_count(),
      f"-> {len(server.mcp._tool_manager._tools)}")


# ── protocol-layer round trip: in-process AND real stdio ───────────────────
async def _protocol_checks() -> None:
    for client_name, client_cm in (("in-process", in_process), ("stdio", over_stdio)):
        async with client_cm() as client:
            listed = {t.name for t in (await client.list_tools()).tools}
            check(f"[{client_name}] tools/list serves {expected_tool_count()} tools",
                  len(listed) == expected_tool_count(), f"-> {len(listed)}")
            missing_new = {"bits", "symbolic"} - listed
            check(f"[{client_name}] tools/list serves bits and symbolic",
                  not missing_new, f"-> missing {missing_new}")
            check(f"[{client_name}] tools/list serves none of the 8 retired aliases",
                  not (set(ALL_ALIASES) & listed),
                  f"-> still listed {set(ALL_ALIASES) & listed}")

            # A representative case per mode/op, over the wire, matched against
            # the SAME direct-call result computed above — the protocol layer
            # (JSON-RPC framing for stdio, the SDK's own dispatcher either way)
            # must not add, drop or reshape a single field.
            wire = data(await client.call_tool("bits", {"mode": "op", "a": 12, "op": "xor",
                                                        "b": 10, "width": 8}))
            direct = server.bits(mode="op", a=12, op="xor", b=10, width=8)
            check(f"[{client_name}] bits(mode='op') over the wire == direct call",
                  wire == direct, f"-> wire={wire}\n     direct={direct}")

            wire = data(await client.call_tool("symbolic", {"op": "solve", "expr": "x**2-4=0"}))
            direct = server.symbolic(op="solve", expr="x**2-4=0")
            check(f"[{client_name}] symbolic(op='solve') over the wire == direct call",
                  wire == direct, f"-> wire={wire}\n     direct={direct}")

            # a validation error over the wire still carries `code`/`error`
            bad = data(await client.call_tool("bits", {"mode": "bogus"}))
            check(f"[{client_name}] a mode-validation error survives the wire",
                  isinstance(bad, dict) and bad.get("ok") is False
                  and bad.get("code") == errors.VALIDATION,
                  f"-> {bad}")

            # every retired alias: the SDK's own unknown-tool result, not a
            # tool-shaped {"ok": ...} dict — proves the name is gone from the
            # wire protocol, not merely unlisted while still callable.
            for name in ALL_ALIASES:
                result = await client.call_tool(name, {})
                text = " ".join(
                    getattr(block, "text", "") for block in (result.content or []))
                check(f"[{client_name}] calling retired alias {name!r} is an error",
                      result.is_error is True, f"-> is_error={result.is_error}")
                check(f"[{client_name}] {name!r}'s unknown-tool error names the tool",
                      name in text, f"-> {text!r}")


asyncio.run(_protocol_checks())

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL BITS/SYMBOLIC TESTS PASS ===")
sys.exit(1 if FAILS else 0)
