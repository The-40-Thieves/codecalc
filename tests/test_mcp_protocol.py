"""The MCP surface, asserted over a real connection.

This is the gate that says codecalc is on protocol **2026-07-28**, and it exists
because the obvious way to check that is wrong.

`mcp.types.LATEST_PROTOCOL_VERSION` reads "2026-07-28" whatever any given
connection actually negotiates. Worse, the SAME server answers on either
protocol depending only on how the client connects:

    ClientSession.initialize()   ->  2025-11-25   (the pre-2026 handshake)
    Client(..., mode="auto")     ->  2026-07-28   (stateless core)

Measured, not assumed — and the difference is not cosmetic. `cache_hints`
configured on the server are DROPPED on the legacy path: the identical server
returns ttl_ms=0/private under the old handshake and 60000/public under
2026-07-28. A test written against ClientSession would have "verified" a
conformant server while proving the opposite.

So every assertion below runs through `mcp.Client`, and the protocol version is
asserted explicitly rather than inferred from a constant.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from mcp import Client

from codecalc import server
from codecalc.mcp_middleware import TOOL_TIMEOUTS

EXPECTED_PROTOCOL = "2026-07-28"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


async def main() -> None:
    async with Client(server.mcp, mode="auto") as c:
        # ── the headline claim ──────────────────────────────────────────────
        check("negotiated protocol is 2026-07-28",
              str(c.protocol_version) == EXPECTED_PROTOCOL,
              f"-> {c.protocol_version}")

        # ── tools ───────────────────────────────────────────────────────────
        listed = await c.list_tools()
        names = [t.name for t in listed.tools]
        declared = len([ln for ln in (REPO_ROOT / "codecalc" / "server.py")
                        .read_text(encoding="utf-8").splitlines() if ln.startswith("@mcp.tool")])
        check("every declared tool reached the client",
              len(names) == declared, f"-> {len(names)} served / {declared} declared")
        check("no duplicate tool names", len(names) == len(set(names)))

        # 2026-07-28 minor change 3: tools SHOULD come back in a deterministic
        # order so clients can cache. Registration order satisfies that — it is
        # stable across runs — but only if nothing sorts or re-registers, so it
        # is asserted rather than assumed.
        again = [t.name for t in (await c.list_tools()).tools]
        check("tool order is stable across calls", names == again)

        # ── cache hints (REQUIRED by SEP-2549 on these methods) ─────────────
        env = listed.model_dump(mode="json", exclude={"tools"})
        check("tools/list carries a non-zero ttl_ms", (env.get("ttl_ms") or 0) > 0,
              f"-> ttl_ms={env.get('ttl_ms')} scope={env.get('cache_scope')}")
        check("tools/list is publicly cacheable", env.get("cache_scope") == "public")
        check("results carry resultType", env.get("result_type") == "complete")

        # ── the resource template, and the reason it changed ────────────────
        tpl = await c.list_resource_templates()
        uris = [t.uri_template for t in tpl.resource_templates]
        check("session-file resource template is advertised", len(uris) == 1, f"-> {uris}")
        # {path*} is rejected outright by the SDK matcher and plain {path} will
        # not cross a '/', so nested workspace files need RFC 6570 reserved
        # expansion. This asserts the operator survived, because losing it
        # breaks nested reads while a flat read keeps working.
        check("template uses reserved expansion {+path}",
              any("{+path}" in u for u in uris), f"-> {uris}")

        # ── a real round-trip through a tool ────────────────────────────────
        r = await c.call_tool("calc_exact", {"expr": "0.1+0.2 == 0.3"})
        text = "".join(getattr(b, "text", "") for b in r.content)
        check("calc_exact round-trips", "true" in text.lower(), f"-> {text[:60]}")

        # ── the timeout backstop still covers what it used to ───────────────
        # AUDIT.md HIGH-05. MCPServer.tool() has no timeout= parameter, so these
        # moved to middleware; an entry naming a tool that no longer exists is a
        # deadline nobody is getting.
        missing = sorted(set(TOOL_TIMEOUTS) - set(names))
        check("every tool with a deadline still exists", not missing, f"-> {missing}")

    # ── and the trap this file exists for ───────────────────────────────────
    # The legacy path must still WORK (backward compatibility is a feature), it
    # just must not be mistaken for the new protocol.
    async with Client(server.mcp, mode="legacy") as c:
        check("legacy mode still serves clients", str(c.protocol_version) == "2025-11-25",
              f"-> {c.protocol_version}")

    print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
          "\n=== MCP 2.0 / protocol 2026-07-28 SURFACE OK ===")


asyncio.run(main())
sys.exit(1 if FAILS else 0)
