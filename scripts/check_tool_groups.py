#!/usr/bin/env python3
"""Every `@mcp.tool()` declares a real group, and the default set registers all of them (THE-896).

`CODECALC_TOOLS` lets an operator register a SLICE of the 52-tool surface instead of paying the
full ~9.2k-token `tools/list` cost for every client. That only works if two things hold, neither of
which the Python type checker or a normal test run would catch on its own:

  1. every `@mcp.tool()` in server.py carries a `group=` naming one of `server.KNOWN_GROUPS` — a
     tool with no group, or a typo'd one, is unreachable by ANY value of CODECALC_TOOLS, which is a
     tool silently dropped out of every possible configuration including the one nobody set.
  2. the DEFAULT (CODECALC_TOOLS unset) still registers every declared tool. `server._tool` computes
     `_ACTIVE_GROUPS` once at import from `KNOWN_GROUPS` itself when the variable is unset, so this
     should be structurally guaranteed — but "should be" is exactly what the tool-count identity
     comment three lines above `_tool` already warns is load-bearing and has broken four suites
     before. This re-derives it from a live import rather than trusting the code that computes it,
     the same reasoning check_tool_names.py uses against the served/declared tool NAME sets.

Static analysis (1) runs on a bare checkout. The live check (2) needs `mcp` installed — spawned as a
subprocess importing `codecalc.server` fresh, with CODECALC_TOOLS explicitly unset, so it belongs
next to scripts/check_tool_names.py in the built round-trip job, not the stdlib-only ci-security jobs.

FLOOR: asserts a plausible number of decorated tools and known groups were parsed, and that the
group-taxonomy assignment this file depends on (`KNOWN_GROUPS = frozenset({...})`) still exists and
still parses as a set of string literals. A parse that finds nothing must not read as agreement.

Usage: check_tool_groups.py
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "codecalc" / "server.py"

#: Fewer decorated tools than this means the parse missed them, not that the surface shrank.
#: Mirrors scripts/check_tool_names.py's own floor.
MIN_TOOLS = 20

#: Fewer groups than this means KNOWN_GROUPS was not found/parsed, not that the taxonomy shrank.
MIN_GROUPS = 2


def fail(msg: str) -> None:
    print(f"::error::{msg}")
    sys.exit(1)


def _module_ast() -> ast.Module:
    return ast.parse(SERVER.read_text(encoding="utf-8"), filename=str(SERVER))


def known_groups(tree: ast.Module) -> set[str]:
    """The string literals inside `KNOWN_GROUPS = frozenset({...})`.

    Read from the source rather than hardcoded here, so a group renamed in server.py cannot drift
    out of sync with the set this gate checks against — the same reasoning check_tool_names.py uses
    for reading the tool name override instead of assuming every decorator is bare.
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == "KNOWN_GROUPS"):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "frozenset" and call.args):
            continue
        literal = call.args[0]
        if not isinstance(literal, (ast.Set, ast.List, ast.Tuple)):
            continue
        return {elt.value for elt in literal.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)}
    return set()


def declared_tool_groups(tree: ast.Module) -> list[tuple[str, int, str | None]]:
    """(tool name, line, group value or None) for every `@mcp.tool()`-decorated function.

    `group` is None when the keyword is absent, and also when its value is not a plain string
    constant (e.g. computed) — either way this gate cannot vouch for it, which is the point: the
    runtime `_tool` wrapper in server.py raises on exactly this, so a passing gate and a working
    import should never disagree about which tools are grouped.
    """
    found: list[tuple[str, int, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr != "tool":
                continue
            name = node.name
            group: str | None = None
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name = str(kw.value.value)
                if kw.arg == "group":
                    group = (kw.value.value if isinstance(kw.value, ast.Constant)
                              and isinstance(kw.value.value, str) else None)
            found.append((name, node.lineno, group))
    return found


tree = _module_ast()
groups = known_groups(tree)
if len(groups) < MIN_GROUPS:
    fail(
        f"floor: parsed only {len(groups)} known group(s) from `KNOWN_GROUPS = frozenset({{...}})` "
        f"in {SERVER.name}, expected at least {MIN_GROUPS}. The assignment shape has moved, and an "
        f"empty set would otherwise make every group look 'known' by vacuous agreement."
    )

declared = declared_tool_groups(tree)
if len(declared) < MIN_TOOLS:
    fail(
        f"floor: parsed only {len(declared)} decorated tool(s) from {SERVER.name}, expected at "
        f"least {MIN_TOOLS}. The decorator shape or the file has moved."
    )

ungrouped = [(n, ln) for n, ln, g in declared if g is None]
if ungrouped:
    fail(
        f"{len(ungrouped)} tool(s) declared with no group= (or a non-literal one): "
        + "; ".join(f"{n} at line {ln}" for n, ln in ungrouped)
        + ". Such a tool is unreachable by ANY CODECALC_TOOLS value — dropped from every possible "
          "configuration, including the default. Add group=\"<name>\" from KNOWN_GROUPS."
    )

unknown_group = [(n, ln, g) for n, ln, g in declared if g not in groups]
if unknown_group:
    fail(
        f"{len(unknown_group)} tool(s) declared a group not in KNOWN_GROUPS: "
        + "; ".join(f"{n} at line {ln} -> {g!r}" for n, ln, g in unknown_group)
        + f". Known groups: {sorted(groups)}."
    )

print(f"ok   groups: all {len(declared)} declared tool(s) name one of the {len(groups)} known "
      f"group(s): {sorted(groups)}")

# ── live check: the DEFAULT (unset CODECALC_TOOLS) registers every declared tool ────────────
env = dict(os.environ)
env.pop("CODECALC_TOOLS", None)
_PROBE = (
    "from codecalc import server\n"
    "print(f'REGISTERED {len(server.mcp._tool_manager._tools)}')"
)
proc = subprocess.run(
    [sys.executable, "-c", _PROBE],
    cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
)
if proc.returncode != 0:
    fail(
        f"importing codecalc.server with CODECALC_TOOLS unset failed (rc={proc.returncode}):\n"
        f"{proc.stderr}"
    )

registered = None
for line in proc.stdout.splitlines():
    if line.startswith("REGISTERED "):
        registered = int(line.removeprefix("REGISTERED ").strip())
if registered is None:
    fail(
        f"the import subprocess printed no 'REGISTERED N' line — nothing to compare against the "
        f"{len(declared)} tools declared in {SERVER.name}.\nstdout={proc.stdout!r}"
    )

if registered != len(declared):
    fail(
        f"the DEFAULT active set (CODECALC_TOOLS unset) registered {registered} tool(s), but "
        f"{len(declared)} are declared in {SERVER.name}. A group filter that changes the default "
        f"count silently breaks the 'every tool is reachable without configuration' invariant."
    )

print(f"ok   default registration: CODECALC_TOOLS unset registers all {registered} declared "
      f"tool(s) — the group filter cannot silently shrink the default surface")
