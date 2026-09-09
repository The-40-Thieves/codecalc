"""Server-side policy `_meta` reaches `tools/list`, on exactly the named tools
and no others. Also covers three properties of the tool-DESCRIPTION/
instructions surface that `scripts/tool_select_eval.py` cannot: that surface
only ever scores `"<name> <description>"` per tool (see its own
`_doc_text`/`load_tool_schemas`), never `mcp.instructions`, so nothing else
asserts on the instructions string, on whether a description actually names
its confusable siblings, or on whether `instructions` stays honest under a
narrowed `CODECALC_TOOLS`.

PR #299 review: the first `instructions` routing map was a STATIC literal
built at import time, naming every tool group unconditionally — but
`CODECALC_TOOLS` gates which groups actually get REGISTERED, later, via
`_active_groups()`. A `CODECALC_TOOLS=core` (calculator only) or `dev`
process told a routing client about `z3_check`/`execute_code`/sessions
tools it never registers. `_build_instructions()` (server.py) now builds
`instructions` from `_ACTIVE_GROUPS` after every `@mcp.tool()` line has run,
so this file spawns a FRESH subprocess per `CODECALC_TOOLS` value (the
in-process client below only ever sees the unconfigured default) and
asserts no absent group or tool name leaks into that narrower instructions
string.

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
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import expected_tool_count
from _mcp_client import in_process

from codecalc import server as codecalc_server

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def _names_word(haystack: str, word: str) -> bool:
    """Whether `word` appears in `haystack` as a whole word, not a
    substring — `\\b` treats `_` as a word character (no boundary either
    side of it), so `\\banalysis\\b` does NOT match inside `bit_analysis`
    the way a plain `word in haystack` check does. Caught live: the first
    cut of the CODECALC_TOOLS=core check below reported the `analysis`
    GROUP as leaked into a calculator-only instructions string purely
    because `bit_analysis` (a calculator TOOL, correctly present) contains
    the substring "analysis".
    """
    return re.search(rf"\b{re.escape(word)}\b", haystack) is not None


REQUIRES_USER_INTERACTION = {"install_package", "update_runtimes"}
ALWAYS_LOAD = {"calc_exact", "execute_code", "verify_translation", "verify_optimization",
               "list_languages"}
MAX_RESULT_SIZE_CHARS_TOOLS = {"execute_code", "execute_code_stream", "session_run",
                               "compare_execution", "run_inspect",
    "trace_execution",  # events + stdout: same result-size shape as execute_code
}
# 240 KiB per stream: the largest static hint under Claude Code's documented
# hard maximum of 500,000 characters for `anthropic/maxResultSizeChars`
# (code.claude.com/docs/en/mcp) that this file can compute from a round KiB
# figure — see server.py's `_MAX_OUTPUT_KB_CEILING` for the full derivation.
MAX_OUTPUT_KB_CEILING = 240
EXPECTED_MAX_RESULT_SIZE_CHARS = 2 * MAX_OUTPUT_KB_CEILING * 1024 + 8_000  # = 499_520


async def main() -> None:
    async with in_process() as client:
        listed = {t.name: t for t in (await client.list_tools()).tools}
        check(f"tools/list served {expected_tool_count()} tools", len(listed) == expected_tool_count(), f"-> {len(listed)}")

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
        check(f"anthropic/maxResultSizeChars is set on exactly the {len(MAX_RESULT_SIZE_CHARS_TOOLS)} named tools",
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
        missing_groups = sorted(g for g in codecalc_server.KNOWN_GROUPS
                               if not _names_word(instructions, g))
        check(f"instructions names every tool group ({sorted(codecalc_server.KNOWN_GROUPS)})",
              not missing_groups, f"-> missing: {missing_groups}")
        check(f"instructions stays under ~1,800 characters (measured: {len(instructions)})",
              len(instructions) <= 1800, f"-> {len(instructions)}")

        # ── existence floor: DESCRIPTION_CLUSTERS is not silently empty ─────
        # A gate that loops over an empty collection reports "0 checks, 0
        # failures" — indistinguishable from "every check passed" by its
        # exit code alone. Pin BOTH that clusters exist and that the loop
        # below actually runs checks, so a future edit that empties/shrinks
        # `DESCRIPTION_CLUSTERS` fails LOUDLY here instead of just quietly
        # running fewer checks with no red anywhere.
        #
        # This floor was 9 (5 + 4 tools) while the eight retired bit/symbolic
        # aliases were still registered: `bits`/`symbolic` were each their
        # own 5-member cluster against their own (still-live) aliases. Once
        # those eight names were removed in 0.12.0 (CHANGELOG.md), `bits`
        # and `symbolic` had no lexically-similar sibling left, so those two
        # clusters were removed rather than kept as meaningless singletons —
        # the one surviving cluster (evaluate_expression/calc_exact/
        # symbolic/z3_check) covers 4. The floor moved down with it because
        # the confusability surface genuinely shrank; not to paper over a
        # regression, since removed-alias descriptions cannot be scored at
        # all any more.
        check(f"DESCRIPTION_CLUSTERS is non-empty ({len(codecalc_server.DESCRIPTION_CLUSTERS)} "
              "cluster(s))",
              len(codecalc_server.DESCRIPTION_CLUSTERS) > 0)
        cluster_member_count = sum(len(c) for c in codecalc_server.DESCRIPTION_CLUSTERS)
        check(f"DESCRIPTION_CLUSTERS covers >= 4 tools (existence floor, not just "
              f"non-empty): {cluster_member_count}",
              cluster_member_count >= 4, f"-> {cluster_member_count}")

        # ── each description-cluster tool names at least one of its own ─────
        # siblings — derived from `codecalc_server.DESCRIPTION_CLUSTERS`, not
        # hand-copied here, so this cannot silently drift from the list
        # server.py actually declares.
        cluster_checks_run = 0
        for cluster in codecalc_server.DESCRIPTION_CLUSTERS:
            for name in sorted(cluster):
                desc = listed[name].description or ""
                siblings = cluster - {name}
                named = {s for s in siblings if s in desc}
                check(f"{name}'s description names at least one sibling from its "
                      f"cluster {sorted(cluster)}",
                      bool(named), f"-> named={sorted(named)}")
                cluster_checks_run += 1
        check(f"the cluster-sibling loop above actually ran >= 4 checks "
              f"(not vacuously zero): {cluster_checks_run}",
              cluster_checks_run >= 4, f"-> {cluster_checks_run}")

    # ── CODECALC_TOOLS=core/dev: instructions never names an absent ─────────
    # group or tool. Fresh subprocess per value — tool REGISTRATION is
    # decided once, at import, from `CODECALC_TOOLS` (see
    # `codecalc/server.py`'s `_active_groups()`), so the in-process client
    # above (no env override) can only ever exercise the unconfigured
    # default and would never catch this class of leak.
    for tools_env, floor_note in (("core", "calculator only"), ("dev", "4 of 6 groups")):
        info = _probe_instructions(tools_env)
        active = set(info["active_groups"])
        all_groups = set(info["all_groups"])
        absent_groups = sorted(all_groups - active)
        leaked_groups = sorted(g for g in absent_groups if _names_word(info["instructions"], g))
        check(f"CODECALC_TOOLS={tools_env} ({floor_note}): instructions names no "
              f"absent group (absent: {absent_groups})",
              not leaked_groups, f"-> leaked: {leaked_groups}")

        absent_tools = sorted(t for t, g in info["tool_groups"].items() if g not in active)
        leaked_tools = sorted(t for t in absent_tools if _names_word(info["instructions"], t))
        check(f"CODECALC_TOOLS={tools_env}: instructions names no absent tool "
              f"({len(absent_tools)} absent, checked)",
              not leaked_tools, f"-> leaked: {leaked_tools}")

        check(f"CODECALC_TOOLS={tools_env}: instructions is non-empty and shorter "
              f"than the full-surface one ({len(info['instructions'])} chars)",
              0 < len(info["instructions"]) < 1800, f"-> {len(info['instructions'])}")


#: Runs in a FRESH subprocess — same reasoning as
#: scripts/tool_select_eval.py's `load_tool_schemas`/`_PROBE`: `codecalc.server`
#: decides its registered surface once, at import, from `CODECALC_TOOLS`, so
#: this process's own (already-imported, unconfigured) `codecalc_server`
#: cannot be re-imported into a different configuration.
_INSTRUCTIONS_PROBE = (
    "import json\n"
    "from codecalc import server\n"
    "print(json.dumps({\n"
    "    'instructions': server.mcp.instructions,\n"
    "    'active_groups': sorted(server._ACTIVE_GROUPS),\n"
    "    'all_groups': sorted(server.KNOWN_GROUPS),\n"
    "    'tool_groups': server.TOOL_GROUPS,\n"
    "}))\n"
)


def _probe_instructions(tools_env: str) -> dict:
    env = dict(os.environ)
    env["CODECALC_TOOLS"] = tools_env
    proc = subprocess.run(
        [sys.executable, "-c", _INSTRUCTIONS_PROBE],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"instructions probe for CODECALC_TOOLS={tools_env!r} failed "
            f"(rc={proc.returncode}): {proc.stderr[-2000:]}"
        )
    return json.loads(proc.stdout)


asyncio.run(main())
print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else "\n=== ALL TOOL-META TESTS PASS ===")
sys.exit(1 if FAILS else 0)
