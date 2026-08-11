#!/usr/bin/env python3
"""The tool-dispatch invariant: the tool a client asks for is the tool that runs.

`codecalc/mcp_middleware.py` resolves a tool's response deadline from the
INBOUND `tools/call` name: `TOOL_TIMEOUTS.get(_tool_name(ctx), ...)`. Those
deadlines are the AUDIT.md HIGH-05 mitigation for in-process sympy and z3 work.

That lookup is correct today ONLY because the inbound name and the tool that
actually executes are the same thing. There is no dispatcher anywhere in this
package. The moment one tool implementation calls ANOTHER tool through the MCP
layer (a facade, a router, a batching tool), the middleware keeps seeing the
OUTER name while a different tool's code runs. The outer name is not in
TOOL_TIMEOUTS, so every one of the twelve deadlines falls back to the 900s
DEFAULT_TIMEOUT_SECONDS for the dispatched call, and nothing anywhere reports
it. The mitigation goes quiet without a single decorator being touched, which
is the exact outcome TOOL_TIMEOUTS' own comment claims the table prevents.

So this asserts the precondition instead of trusting it.

This is a SEPARATE script rather than another section inside check_no_eval.py
on purpose. That file gates the zero-eval invariant; folding an unrelated
invariant into it makes a green `check_no_eval` cover more than its name says,
which is a problem this repo already has elsewhere and does not need a second
instance of. It also misreported the finding: a tool dispatch is not a
"forbidden dynamic-execution construct", and a gate that names the wrong cause
is a gate the next reader reasons about wrongly.

It is also the gate TOOL_TIMEOUTS' comment used to cite and that never existed
(it named `check_tool_timeouts`). The assertion was always real; now the file
is too.

FLOOR: asserts it parsed a plausible number of files and that the module it
exists to protect is among them. `rglob` over a moved package would otherwise
walk zero files and report a clean tree, which is what a source scan with no
floor reports when it has scanned nothing at all.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "codecalc"

#: Re-entering tool dispatch means calling `.call_tool(...)` on something.
#: `MCPServer.call_tool`, `ToolManager.call_tool` and `mcp.Client(...).call_tool`
#: are all reached that way, so matching the attribute alone catches every one
#: regardless of what the receiver is named. This is the same reasoning the
#: eval/exec scan uses for `.eval(...)`.
DISPATCH_METHOD = "call_tool"

#: The module whose correctness depends on this invariant. Part of the floor:
#: if the scan does not reach this file, it has not checked the thing it exists
#: to check, whatever else it found.
MUST_SCAN = "mcp_middleware.py"

#: A scan that walks fewer files than this has almost certainly been pointed at
#: the wrong tree. The package carried 23 modules when this was written, so this
#: leaves room to delete a few without a false alarm and none to scan nothing.
MIN_FILES = 15

sites: list[str] = []
scanned: list[str] = []

for path in sorted(PKG.rglob("*.py")):
    scanned.append(path.name)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == DISPATCH_METHOD
        ):
            sites.append(f"{path.relative_to(REPO)}:{node.lineno}")

if len(scanned) < MIN_FILES:
    print(
        f"::error::floor: scanned only {len(scanned)} file(s) under {PKG}, expected "
        f"at least {MIN_FILES}. The package has moved or the glob is wrong; a scan "
        f"that reaches nothing reports a clean tree."
    )
    sys.exit(1)

if MUST_SCAN not in scanned:
    print(
        f"::error::floor: {MUST_SCAN} was not among the {len(scanned)} files scanned. "
        f"That is the module this invariant protects, so a pass here would mean nothing."
    )
    sys.exit(1)

if sites:
    print(f"::error::{len(sites)} site(s) re-enter MCP tool dispatch via .{DISPATCH_METHOD}(...):")
    for s in sites:
        print(f"  {s}")
    print(
        "mcp_middleware.timeout_middleware keys TOOL_TIMEOUTS off the INBOUND tools/call "
        "name. A tool that dispatches another tool makes the middleware see the OUTER "
        "name while a different tool runs, silently dropping every deadline in "
        "TOOL_TIMEOUTS to the 900s DEFAULT_TIMEOUT_SECONDS and turning the AUDIT.md "
        "HIGH-05 mitigation into a no-op nobody would notice."
    )
    print(
        "If a dispatcher is being added deliberately, this gate is the reminder that "
        "the deadline lookup has to move to the capability that executes FIRST."
    )
    sys.exit(1)

print(
    f"ok   tool dispatch: no codecalc module calls .{DISPATCH_METHOD}(...) "
    f"({len(scanned)} files scanned, {MUST_SCAN} among them) — the inbound "
    f"tools/call name is still the tool that executes"
)
