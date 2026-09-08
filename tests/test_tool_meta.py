"""Server-side policy `_meta` reaches `tools/list`, on exactly the named tools
and no others. Also covers two properties of the tool-DESCRIPTION surface
that `scripts/tool_select_eval.py` cannot: that surface only ever scores
`"<name> <description>"` per tool (see its own `_doc_text`/`load_tool_schemas`),
never `mcp.instructions`, so nothing else asserts on the instructions string
or on whether a description actually names its confusable siblings.

Evidence for how `_meta` is wired, so this test is not the only thing that
would catch a regression: `mcp.server.mcpserver.tools.base.Tool.from_function`
takes `meta: dict[str, Any] | None` and stores it on the `Tool`
(`tools/base.py`); `MCPServer.list_tools()` copies it straight onto the wire
as `MCPTool._meta` (`mcp/server/mcpserver/server.py:493`, `_meta=info.meta`) —
`mcp` 2.0.0, verified against the installed venv. `codecalc/server.py`'s
`_tool()` wrapper resolves `_tool_meta(name)` and passes it as `meta=` to the
real `mcp.tool(...)`, the same first-class kwarg.

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _mcp_client import in_process

from codecalc import server as codecalc_server

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


REQUIRES_USER_INTERACTION = {"install_package", "update_runtimes"}
ALWAYS_LOAD = {"calc_exact", "execute_code", "verify_translation", "verify_optimization",
               "list_languages"}
MAX_RESULT_SIZE_CHARS_TOOLS = {"execute_code", "execute_code_stream", "session_run",
                               "compare_execution", "run_inspect"}
# 240 KiB per stream: the largest static hint under Claude Code's documented
# hard maximum of 500,000 characters for `anthropic/maxResultSizeChars`
# (code.claude.com/docs/en/mcp) that this file can compute from a round KiB
# figure — see server.py's `_MAX_OUTPUT_KB_CEILING` for the full derivation.
MAX_OUTPUT_KB_CEILING = 240
EXPECTED_MAX_RESULT_SIZE_CHARS = 2 * MAX_OUTPUT_KB_CEILING * 1024 + 8_000  # = 499_520


async def main() -> None:
    async with in_process() as client:
        listed = {t.name: t for t in (await client.list_tools()).tools}
        check("tools/list served 52 tools", len(listed) == 52, f"-> {len(listed)}")

        def meta_of(name: str) -> dict:
            return getattr(listed[name], "meta", None) or {}

        # ── requiresUserInteraction: exactly install_package, update_runtimes ──
        has_rui = {n for n, t in listed.items()
                  if meta_of(n).get("anthropic/requiresUserInteraction") is True}
        check("anthropic/requiresUserInteraction is set on exactly install_package/update_runtimes",
              has_rui == REQUIRES_USER_INTERACTION, f"-> {sorted(has_rui)}")

        # ── alwaysLoad: exactly the 5 named entry-point tools ───────────────
        has_always = {n for n, t in listed.items()
                     if meta_of(n).get("anthropic/alwaysLoad") is True}
        check("anthropic/alwaysLoad is set on exactly the 5 named tools",
              has_always == ALWAYS_LOAD, f"-> {sorted(has_always)}")

        # ── maxResultSizeChars: exactly the 5 large-result tools, same value ──
        has_cap = {n: meta_of(n).get("anthropic/maxResultSizeChars")
                  for n, t in listed.items()
                  if meta_of(n).get("anthropic/maxResultSizeChars") is not None}
        check("anthropic/maxResultSizeChars is set on exactly the 5 named tools",
              set(has_cap) == MAX_RESULT_SIZE_CHARS_TOOLS, f"-> {sorted(has_cap)}")
        wrong_values = {n: v for n, v in has_cap.items() if v != EXPECTED_MAX_RESULT_SIZE_CHARS}
        check(f"every maxResultSizeChars equals {EXPECTED_MAX_RESULT_SIZE_CHARS} "
              "(derived from the 240 KiB per-stream ceiling, not codecalc's 64 KiB default)",
              not wrong_values, f"-> {wrong_values}")
        check("maxResultSizeChars equals 2 * ceiling_bytes + 8_000",
              EXPECTED_MAX_RESULT_SIZE_CHARS == 2 * MAX_OUTPUT_KB_CEILING * 1024 + 8_000,
              f"-> {EXPECTED_MAX_RESULT_SIZE_CHARS}")
        check("maxResultSizeChars stays at or under Claude Code's documented hard maximum "
              "of 500,000 characters for this _meta field",
              EXPECTED_MAX_RESULT_SIZE_CHARS <= 500_000, f"-> {EXPECTED_MAX_RESULT_SIZE_CHARS}")

        # ── no other _meta keys leaked onto an unrelated tool ───────────────
        expected_meta_bearers = REQUIRES_USER_INTERACTION | ALWAYS_LOAD | MAX_RESULT_SIZE_CHARS_TOOLS
        actual_meta_bearers = {n for n, t in listed.items() if meta_of(n)}
        check("no tool outside the three named sets carries any anthropic/* _meta",
              actual_meta_bearers == expected_meta_bearers,
              f"-> extra={sorted(actual_meta_bearers - expected_meta_bearers)}")

        # ── instructions is a routing map naming every tool group ───────────
        # scripts/tool_select_eval.py never scores `mcp.instructions` (it
        # reads `"<name> <description>"` per tool from the live registry
        # only — see `_doc_text`/`load_tool_schemas`), so nothing else in
        # this repo would notice `instructions` silently dropping a group.
        instructions = codecalc_server.mcp.instructions or ""
        missing_groups = sorted(g for g in codecalc_server.KNOWN_GROUPS if g not in instructions)
        check(f"instructions names every tool group ({sorted(codecalc_server.KNOWN_GROUPS)})",
              not missing_groups, f"-> missing: {missing_groups}")
        check(f"instructions stays under ~1,800 characters (measured: {len(instructions)})",
              len(instructions) <= 1800, f"-> {len(instructions)}")

        # ── each description-cluster tool names at least one of its own ─────
        # siblings — derived from `codecalc_server.DESCRIPTION_CLUSTERS`, not
        # hand-copied here, so this cannot silently drift from the list
        # server.py actually declares.
        for cluster in codecalc_server.DESCRIPTION_CLUSTERS:
            for name in sorted(cluster):
                desc = listed[name].description or ""
                siblings = cluster - {name}
                named = {s for s in siblings if s in desc}
                check(f"{name}'s description names at least one sibling from its "
                      f"cluster {sorted(cluster)}",
                      bool(named), f"-> named={sorted(named)}")


asyncio.run(main())
print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else "\n=== ALL TOOL-META TESTS PASS ===")
sys.exit(1 if FAILS else 0)
