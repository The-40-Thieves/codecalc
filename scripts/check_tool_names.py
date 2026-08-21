#!/usr/bin/env python3
"""The tool surface is the same SET of names in server.py and over the wire.

Replaces a count comparison. `served -ne $declared` proved that the same NUMBER
of tools reached the client, never that they were the same tools, so it passed
in every one of these cases:

  - one tool lost while an unrelated one is added in the same change (net zero)
  - a tool registered somewhere other than a decorator, offsetting a decorator
    that stopped registering
  - the client served the right number of tools under the wrong names

Both sides of that comparison also came from the same assumption — that
decorator sites and registered tools correspond one to one — so when THAT is
what breaks, the check compared a wrong number to itself.

It also asserts the thing the old check's comment CLAIMED to protect against and
did not. That comment said a tool whose type hints the SDK cannot schematise is
"dropped from list_tools at registration, with no error anywhere". That is not
MCP SDK 2.0.0's behaviour: `Tool.from_function` RAISES (`ValueError`,
`InvalidSignature`), so such a tool fails loudly at import. The genuine silent
path is a DUPLICATE NAME — `ToolManager.add_tool` logs a warning, returns the
existing tool, and the served count quietly falls by one while the decorator
count does not. Nothing read that warning in CI. So duplicates are now their own
assertion, named as such.

Unlike the gates under ci-security, this one CANNOT run on a bare checkout: the
served side comes from a live stdio round-trip. It consumes the captured stdout
of tests/test_mcp_all.py rather than re-running it, which keeps the property the
old inline step existed for — the declared side is derived independently of the
test, so a test that stops running, stops printing, or is deleted still fails
the build instead of passing vacuously.

FLOOR: asserts a plausible number of decorated tools was parsed, that the
round-trip line exists at all, and that the count the test printed matches the
length of the list it printed. A parse that finds nothing must not read as
agreement between two empty sets.

Usage: check_tool_names.py <path-to-round-trip-stdout>
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "codecalc" / "server.py"

#: Fewer decorated tools than this means the parse missed them, not that the
#: surface shrank. The server carried 47 when this was written.
MIN_TOOLS = 20

#: `tools (N): ['a', 'b', ...]` — printed by tests/test_mcp_all.py. The count and
#: the list are both parsed so they can be checked against each other.
SERVED_RE = re.compile(r"^tools \((\d+)\): (\[.*\])\s*$", re.MULTILINE)


def declared_tools() -> list[tuple[str, int]]:
    """(tool name, line) for every @mcp.tool()-decorated function, in file order.

    A LIST, not a set: two decorators declaring the same tool name is exactly
    the silent failure this exists to catch, and a set would hide it.

    The name is the decorator's `name=` when given, else the function name.
    Every decorator is bare today, but reading the keyword means a future
    `@mcp.tool(name="x")` cannot silently break the mapping between the two
    sides of this comparison.
    """
    tree = ast.parse(SERVER.read_text(encoding="utf-8"), filename=str(SERVER))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr != "tool":
                continue
            name = node.name
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name = str(kw.value.value)
            found.append((name, node.lineno))
    return found


def fail(msg: str) -> None:
    print(f"::error::{msg}")
    sys.exit(1)


if len(sys.argv) != 2:
    fail("usage: check_tool_names.py <path-to-round-trip-stdout>")

out_path = Path(sys.argv[1])
if not out_path.is_file():
    fail(f"{out_path} does not exist; the round-trip produced no output to check.")

declared = declared_tools()
if len(declared) < MIN_TOOLS:
    fail(
        f"floor: parsed only {len(declared)} decorated tool(s) from {SERVER.name}, expected at "
        f"least {MIN_TOOLS}. The decorator shape or the file has moved, and two empty sets would "
        f"otherwise compare equal."
    )

names = [n for n, _ in declared]
dupes = sorted({n for n in names if names.count(n) > 1})
if dupes:
    lines = {n: [ln for nm, ln in declared if nm == n] for n in dupes}
    fail(
        f"{len(dupes)} duplicate tool name(s) declared: "
        + "; ".join(f"{n} at lines {lines[n]}" for n in dupes)
        + ". ToolManager.add_tool keeps the FIRST registration, logs a warning nothing reads, and "
          "discards the rest — the served surface silently loses a tool while the decorator count "
          "does not change."
    )

text = out_path.read_text(encoding="utf-8", errors="replace")
match = SERVED_RE.search(text)
if not match:
    fail(
        f"floor: no 'tools (N): [...]' line in {out_path}. The round-trip printed nothing this "
        f"check can assert on, which is not the same as agreeing."
    )

served_count = int(match.group(1))
try:
    served = [str(x) for x in ast.literal_eval(match.group(2))]
except (ValueError, SyntaxError) as exc:
    fail(f"could not parse the served tool list: {exc}")

if served_count != len(served):
    fail(
        f"the round-trip printed 'tools ({served_count})' but listed {len(served)} names. Its own "
        f"count and its own list disagree, so neither can be trusted here."
    )

declared_set, served_set = set(names), set(served)
if declared_set != served_set:
    missing = sorted(declared_set - served_set)
    extra = sorted(served_set - declared_set)
    fail(
        "the declared and served tool surfaces are not the same set.\n"
        f"  declared in {SERVER.name} but never reached the client: {missing or 'none'}\n"
        f"  served to the client but not declared there: {extra or 'none'}\n"
        "A count comparison passes when one tool is lost and another gained; this does not."
    )

print(
    f"ok   tool names: {len(served)} declared in {SERVER.name} and served over stdio are the same "
    f"set, with no duplicate declarations"
)
