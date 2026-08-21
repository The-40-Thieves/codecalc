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
import re
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
#:
#: The lookup below keys on `_tool_name(ctx)` — the INBOUND `tools/call` name —
#: not on whatever ends up executing. That is only correct because there is no
#: dispatcher anywhere in this package: the tool a client asked for is always
#: the tool that runs. If any tool implementation ever called another tool
#: through the MCP layer (a facade, a router, a batching tool), the middleware
#: would keep seeing the OUTER name, and every one of the deadlines above would
#: silently fall back to DEFAULT_TIMEOUT_SECONDS (900s) for the dispatched call.
#: scripts/check_tool_dispatch.py is what keeps that assumption enforced rather
#: than merely assumed — it fails the build the moment a `.call_tool(...)`
#: re-entry appears anywhere under codecalc/. That is also the gate this very
#: comment block used to cite and that did not exist; it does now.
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
    "matrix": 20,
    # Verification gates: they run BOTH programs, and verify_optimization
    # additionally times each at four sizes. No network, but real execution.
    "verify_translation": 120,
    "verify_optimization": 180,
    # run_submit/run_inspect/run_cancel are RunSupervisor control-
    # plane calls: submit-and-return, a non-blocking status read, and a
    # cancellation signal. None of them wait for the submitted computation to
    # finish — that is what polling run_inspect is FOR — so they get a short
    # deadline like the in-process calls above, rather than execute_code's
    # generous DEFAULT_TIMEOUT_SECONDS fallback, which exists because THAT
    # tool's own `timeout` argument already bounds the work it waits on.
    # Named explicitly rather than left to the default specifically because
    # is exactly this table silently going quiet for an unlisted name.
    "run_submit": 15,
    "run_inspect": 10,
    "run_cancel": 15,
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


# ── GH #212(b): pydantic argument-validation errors echoed raw ─────
#
# A call with a wrong-typed argument never reaches a tool body at all: the
# SDK validates `arguments` against the tool's pydantic arg-model BEFORE
# `fn(**kwargs)` runs (`FuncMetadata.call_fn_with_arg_validation`), and a
# failure there raises `pydantic.ValidationError`. `Tool.run` (mcp SDK) wraps
# whatever it catches as `ToolError(f"Error executing tool {name}: {e}")`,
# and `_handle_call_tool` flattens THAT into `CallToolResult(is_error=True,
# content=[TextContent(text=str(exc))])` — str(e) on a pydantic
# ValidationError includes `input_value=<the caller's actual argument>`
# verbatim, e.g. passing a string where `max_bytes: int` was declared echoes
# that exact string back in the error text.
#
# Every OTHER rejection in this package either never carries the caller's
# raw value (errors.py's VALIDATION bucket names the field, not the value)
# or redacts it deliberately (audit.py's secret redaction) — this is the one
# path where it leaks, and it leaks BEFORE codecalc's own code ever sees the
# call: there is no earlier seam inside this package to catch it at, because
# the failure happens one layer up, inside the SDK's own arg-model
# validation. This middleware is therefore the last point before the text
# leaves the process — it does not (and cannot) stop pydantic from building
# the message, only strip the caller-supplied value back out of it before a
# client, a log, or a transcript ever sees it.
_PYDANTIC_CONTEXT_RE = re.compile(r"\[type=.*\]")


def _is_argument_validation_error(text: str) -> bool:
    """True for the specific SDK message shape this middleware redacts.

    Both substrings are pydantic's own vocabulary (`"N validation error(s)
    for <Model>"`, `"input_value=..."`), so an ordinary codecalc tool error
    that happens to contain one word or the other is not enough to match —
    matching an unrelated message would strip legitimate diagnostic text for
    no reason, which is the failure mode `errors.py`'s own message-hint list
    already warns is worse than missing a case.
    """
    return "validation error for" in text and "input_value=" in text


def _redact_argument_validation_error(text: str) -> str:
    """Strip the caller-supplied value out of a pydantic validation message.

    Keeps the field name and the reason ("which field, what kind of value
    was expected" — both harmless and useful for fixing the call) and
    replaces only the bracketed diagnostic that carries the actual input.
    Greedy `.*` is deliberate: it matches to the LAST `]` on the line, which
    is where pydantic always closes this bracket even when the input's own
    repr contains a `]` of its own (a list, a dict, a path) — a lazy match
    would stop at the first one and leave the tail of the raw value exposed.
    """
    return _PYDANTIC_CONTEXT_RE.sub("[redacted]", text)


async def redact_validation_errors_middleware(
    ctx: ServerRequestContext, call_next: CallNext
) -> HandlerResult:
    """Strip caller-supplied argument values out of validation-error text.

    Runs for every `tools/call`; everything else (and every OTHER shape of
    tool failure — codecalc's own `{"ok": false, ...}` results never reach
    here as `is_error`, only a pre-tool-body validation failure does) passes
    through untouched.

    Operates on the WIRE dict, not a `CallToolResult` object: `_inner`
    (mcp.server.runner.ServerRunner) already serialises the handler's result
    via `model_dump(by_alias=True, mode="json")` before it ever reaches this
    middleware — this is the innermost of the two custom entries in
    `server.py`'s `middleware=[...]` list, so by the time `call_next` returns,
    `result` is the same `{"content": [...], "isError": true, ...}` shape
    that goes out over the wire, camelCase keys included. Measured: an
    `isinstance(result, CallToolResult)` check here never once matched.
    """
    if ctx.method != "tools/call":
        return await call_next(ctx)
    result = await call_next(ctx)
    if not isinstance(result, dict) or not result.get("isError"):
        return result
    content = result.get("content")
    if not isinstance(content, list):
        return result
    changed = False
    new_content = []
    for block in content:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str) and _is_argument_validation_error(text):
            block = {**block, "text": _redact_argument_validation_error(text)}
            changed = True
        new_content.append(block)
    if not changed:
        return result
    return {**result, "content": new_content}
