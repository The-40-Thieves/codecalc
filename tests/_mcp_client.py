"""Shared helpers for connecting to the codecalc MCP server in tests.

Two ways in, and the difference matters:

  in_process()  -> Client(server.mcp).  Drives the real handlers, middleware,
                   schemas and cache hints through a direct dispatcher. No
                   subprocess, no JSON-RPC framing.
  over_stdio()  -> Client(StdioTransport(...)).  Spawns `python -m codecalc.server`
                   and speaks framed JSON-RPC down a pipe, which is how every
                   real client (Claude Desktop, LiteLLM, an agent runtime)
                   actually launches this server.

Both are needed. In-process is fast and exercises the logic; only stdio proves
the packaging works — that `python -m codecalc.server` starts, that nothing
prints to stdout and corrupts the framing, and that the entry point exists.

`mode="auto"` throughout: it negotiates protocol 2026-07-28 via `server/discover`
and falls back to the initialize handshake. Using `ClientSession.initialize()`
instead would silently pin every test to the legacy 2025-11-25 path — see
tests/test_mcp_protocol.py for why that is a trap rather than a detail.
"""

from __future__ import annotations

import pathlib
import sys
from contextlib import asynccontextmanager

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client


class StdioTransport:
    """Adapt `stdio_client` to the `Transport` protocol Client expects.

    Client accepts `Server | MCPServer | Transport | str`. A plain `str` is
    treated as an HTTP URL (verified: it raises UnsupportedProtocol on a command
    line), so a subprocess needs a Transport, and the SDK does not ship one for
    stdio — `stdio_client` is a bare async context manager over the streams.
    """

    def __init__(self, params: StdioServerParameters) -> None:
        self._params = params
        self._cm = None

    async def __aenter__(self):
        self._cm = stdio_client(self._params)
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc):
        assert self._cm is not None
        return await self._cm.__aexit__(*exc)


def _server_params(env: dict[str, str] | None = None) -> StdioServerParameters:
    base = {"PYTHONPATH": str(REPO_ROOT), "PATH": __import__("os").environ.get("PATH", "")}
    if env:
        base.update(env)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "codecalc.server"],
        env=base,
    )


@asynccontextmanager
async def in_process():
    """Client bound to the server object; fastest, exercises handlers directly."""
    from codecalc import server

    async with Client(server.mcp, mode="auto") as c:
        yield c


@asynccontextmanager
async def over_stdio(env: dict[str, str] | None = None):
    """Client bound to a real `python -m codecalc.server` subprocess.

    `env` is merged OVER the base PYTHONPATH/PATH, not a replacement for
    them — a test that needs, say, `CODECALC_RUNTIME_PATH` pointed at a
    scratch directory should not also have to re-derive PYTHONPATH/PATH
    itself just to add one variable.
    """
    async with Client(StdioTransport(_server_params(env)), mode="auto") as c:
        yield c


def data(result):
    """Parse a tool result's JSON payload.

    fastmcp's client exposed `result.data` — a parsed dict. The official SDK
    does not: it derives `structured_content` from the tool's return type
    annotation, populating it only when that annotation is schematisable.

    A later change moved which side of that line codecalc sits on. Most
    tools' return annotation went from a bare `-> dict` (not schematisable —
    the SDK cannot build a model from `dict` with no type args) to
    `-> dict[str, Any]` (schematisable), so `structured_content` is now
    populated for every tool except `session_read_file` and `session_run`
    (both left untyped on purpose — see codecalc/server.py). This helper
    already handled both cases before that change landed, for the
    in-process-vs-stdio asymmetry noted below; nothing here needed to
    change, only this comment, which used to describe the pre-change state
    as current.
    """
    import json

    sc = getattr(result, "structured_content", None)
    if sc:
        # A tool whose return type is not an object is wrapped as
        # {"result": <value>} — a list-returning tool like list_languages comes
        # back that way. Returning the wrapper looks like a successful parse and
        # then fails much later with "string indices must be integers", because
        # iterating a dict yields its keys.
        #
        # Note this differs BY TRANSPORT: in-process the field is None and the
        # content blocks carry the data; over stdio it is populated. A helper
        # written against only one of them works until the other is used.
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        return sc

    # A tool returning a LIST comes back as one content block PER ELEMENT, not
    # as a single block holding a JSON array — fastmcp's `.data` reassembled
    # that and the official SDK does not. Reading only content[0] therefore
    # yields the first element and silently looks like a successful parse:
    # `len()` returns that dict's key count and iteration yields strings.
    parsed = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            parsed.append(json.loads(text))
        except json.JSONDecodeError:
            parsed.append({"_raw": text})
    if not parsed:
        return {}
    return parsed[0] if len(parsed) == 1 else parsed
