"""Server middleware for the MCP 2.0 (protocol 2026-07-28) server.

`MCPServer.tool()` has no `timeout=` parameter — fastmcp's did, and AUDIT.md
HIGH-05 records it as the backstop against in-process resource blowups. Porting
without replacing it would silently delete a documented mitigation, which is the
"declared but not enforced" pattern this repo keeps finding. The SDK's sanctioned
extension point is `ServerMiddleware`: `(ctx, call_next) -> result`, wrapping
every inbound request.

WHAT THIS ACTUALLY BOUNDS, stated precisely because the difference matters:

    It bounds the RESPONSE, not the CPU.

The tools it guards are synchronous, and the SDK runs them on a worker thread. A
deadline can stop *waiting* for that thread; it cannot interrupt it. A sympy call
that has gone quadratic keeps burning a core after the client has been told the
tool timed out. So this stops a client hanging forever on one call — worth
having — but it is not a resource limit.

The actual bounds on that work are the input caps inside the tools themselves,
and those are the load-bearing part of HIGH-05:

    truth_table          max 16 variables (65,536 rows), 2,000-char input
    evaluate_expression  2,000-char input
    z3_check             solver.set("timeout", 5000) — enforced by z3 itself

Only the z3 one actually stops computation, because z3 checks its own deadline.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError

#: JSON-RPC server-error code for "this tool blew its deadline".
#:
#: 2026-07-28 defines an error-code allocation policy: -32000..-32019 stays
#: implementation-defined, -32020..-32099 is reserved for the specification. So
#: an implementation-specific condition like this one belongs in the low block,
#: and must NOT squat on a spec-reserved code.
TOOL_TIMEOUT_ERROR = -32010

#: tool name -> response deadline in seconds. Mirrors the fastmcp
#: `@mcp.tool(timeout=N)` values these tools carried before the port; kept in one
#: table so a tool cannot quietly lose its deadline by someone editing a
#: decorator. tests/test_mcp_protocol.py asserts every name here still exists as
#: a registered tool — a deadline attached to a renamed or deleted tool is a
#: deadline that never fires. (This comment used to cite a check_tool_timeouts
#: gate under scripts/ that has never existed. The assertion was real; the
#: filename was not.)
TOOL_TIMEOUTS: dict[str, float] = {
    # in-process sympy / z3 work — AUDIT.md HIGH-05
    "evaluate_expression": 20,
    "truth_table": 20,
    "z3_check": 30,
    "solve_linear": 20,
    "analyze_complexity": 20,
    # calc_exact and its sympy-backed siblings in exact.py were absent here,
    # so they inherited DEFAULT_TIMEOUT_SECONDS (900s) instead of the 20s
    # deadline their logic.py counterparts (evaluate_expression, solve_linear,
    # analyze_complexity) get for the same class of in-process CPU work.
    "calc_exact": 20,
    "algebraic_equiv": 20,
    "solve_expression": 20,
    "limit_expression": 20,
    "simplify_expression": 20,
    # Verification gates: they run BOTH programs, and verify_optimization
    # additionally times each at four sizes. No network, but real execution.
    "verify_translation": 120,
    "verify_optimization": 180,
}

#: Applied to any tool not named above. Generous on purpose: `execute_code` and
#: friends carry their own per-call `timeout` argument and are bounded by the
#: sandbox, so a second ceiling here would only ever fire wrongly.
DEFAULT_TIMEOUT_SECONDS = 900.0


def _tool_name(ctx: ServerRequestContext) -> str | None:
    params: Any = ctx.params
    if isinstance(params, dict):
        return params.get("name")
    return getattr(params, "name", None)


async def timeout_middleware(ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
    """Enforce a per-tool response deadline.

    Non-tool methods (`tools/list`, `resources/read`, `server/discover`, …) pass
    through untouched: they are cheap and bounded, and putting them on a clock
    would add a failure mode without removing one.
    """
    if ctx.method != "tools/call":
        return await call_next(ctx)

    name = _tool_name(ctx)
    limit = TOOL_TIMEOUTS.get(name or "", DEFAULT_TIMEOUT_SECONDS)
    started = time.monotonic()
    try:
        return await asyncio.wait_for(call_next(ctx), timeout=limit)
    except TimeoutError as exc:
        elapsed = time.monotonic() - started
        # MCPError, not a bare TimeoutError. Any other exception is caught by the
        # dispatcher and flattened to "Internal server error" with no detail —
        # verified by raising TimeoutError here and watching the client receive
        # exactly that. A deadline the caller cannot distinguish from a crash is
        # not much of a diagnostic.
        #
        # The message says the work MAY still be running because it may: see the
        # module docstring. Claiming it was cancelled would be untrue.
        raise MCPError(
            code=TOOL_TIMEOUT_ERROR,
            message=(
                f"tool {name!r} exceeded its {limit:g}s response deadline "
                f"(elapsed {elapsed:.1f}s). The call was abandoned; if the tool "
                f"was CPU-bound, that work may still be running."
            ),
            data={"tool": name, "timeout_seconds": limit, "elapsed_seconds": round(elapsed, 2)},
        ) from exc
