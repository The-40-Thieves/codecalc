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


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "codecalc.server"],
        env={"PYTHONPATH": str(REPO_ROOT), "PATH": __import__("os").environ.get("PATH", "")},
    )


@asynccontextmanager
async def in_process():
    """Client bound to the server object; fastest, exercises handlers directly."""
    from codecalc import server

    async with Client(server.mcp, mode="auto") as c:
        yield c


@asynccontextmanager
async def over_stdio():
    """Client bound to a real `python -m codecalc.server` subprocess."""
    async with Client(StdioTransport(_server_params()), mode="auto") as c:
        yield c


def data(result):
    """Parse a tool result's JSON payload.

    fastmcp's client exposed `result.data` — a parsed dict. The official SDK
    does not: codecalc's tools return `dict`, which the SDK serialises into a
    text content block, leaving `structured_content` None unless a tool opts in
    with `structured_output=True`.

    Deliberately NOT opting in during this port. Doing so would add an
    `outputSchema` and a `structuredContent` field to every tool's wire format,
    which is a change to the public surface and belongs in its own change, not
    smuggled into a protocol migration. The text block is unchanged, so existing
    consumers see exactly what they saw before.
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
