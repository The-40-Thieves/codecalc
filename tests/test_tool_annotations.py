"""Every tool carries a complete ToolAnnotations, and the wire actually emits
it — plus the typed-return half of the same change: `-> dict[str, Any]`
flips the SDK onto its structured-output path, and the
contract stamp (`contract_version`, `code`) that `_coded`/`compact_result`
apply to the dict BEFORE the SDK sees it is visible in `structuredContent`.

Two things this deliberately does NOT do:
  * It does not re-derive the "right" annotation per tool from first
    principles — `codecalc/server.py`'s GROUP_ANNOTATIONS/
    TOOL_ANNOTATION_OVERRIDES module docstring is that derivation, with the
    evidence trail for each entry. This tests that every declared tool
    RESOLVES to something (no silent None), and pins the one invariant a
    reviewer would check by hand: no `calculator`-group tool is non-read-only
    (that group is pure arithmetic/symbolic/bit-twiddling by construction).
  * It does not assert every tool's exact hint values — TOOL_ANNOTATION_OVERRIDES
    already IS that table, in the source, next to the reasoning for each entry;
    duplicating every value here would just be a second copy that can drift
    from the first without either gate catching it.

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import expected_tool_count
from _mcp_client import in_process

from codecalc import server

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── every registered tool resolves to a complete ToolAnnotations ───────────
tools = server.mcp._tool_manager._tools
check(f"{expected_tool_count()} tools are registered (default CODECALC_TOOLS)", len(tools) == expected_tool_count(), f"-> {len(tools)}")

missing = [name for name, t in tools.items() if t.annotations is None]
check("every registered tool has a non-None annotations object", not missing, f"-> {missing}")

incomplete = [
    name for name, t in tools.items()
    if t.annotations is not None and (
        t.annotations.read_only_hint is None or t.annotations.destructive_hint is None
        or t.annotations.idempotent_hint is None or t.annotations.open_world_hint is None
    )
]
check("every annotation sets all four hints (none left None/default)", not incomplete,
      f"-> {incomplete}")

missing_title = [name for name, t in tools.items() if not t.title]
check("every registered tool has a non-empty title", not missing_title, f"-> {missing_title}")

# ── the invariant a reviewer checks by hand: calculator is pure ────────────
calculator_tools = [name for name, g in server.TOOL_GROUPS.items() if g == "calculator"]
check("the calculator group has 19 tools (matches the README table)",
      len(calculator_tools) == 19, f"-> {len(calculator_tools)}")
non_read_only_calc = [
    name for name in calculator_tools
    if not (tools[name].annotations and tools[name].annotations.read_only_hint is True)
]
check("no calculator-group tool is marked non-read-only",
      not non_read_only_calc, f"-> {non_read_only_calc}")

# ── the override table only ever narrows a KNOWN, registered tool ─────────
unknown_overrides = sorted(set(server.TOOL_ANNOTATION_OVERRIDES) - set(server.TOOL_GROUPS))
check("every TOOL_ANNOTATION_OVERRIDES key names a declared tool",
      not unknown_overrides, f"-> {unknown_overrides}")
check("every KNOWN_GROUPS name has a GROUP_ANNOTATIONS default",
      set(server.GROUP_ANNOTATIONS) == set(server.KNOWN_GROUPS),
      f"-> {sorted(set(server.KNOWN_GROUPS) - set(server.GROUP_ANNOTATIONS))} missing")


# ── the typed-return half: structuredContent carries the contract stamp ───
async def _structured_content_check() -> None:
    async with in_process() as client:
        result = await client.call_tool("calc_exact", {"expr": "2 + 2"})
        sc = getattr(result, "structured_content", None)
        check("calc_exact populates structured_content (dict[str, Any] flips the SDK path)",
              isinstance(sc, dict) and bool(sc), f"-> {sc!r}")
        if isinstance(sc, dict):
            check("structured_content carries contract_version (stamped at _coded, not lost)",
                  sc.get("contract_version") == server.contract.CONTRACT_VERSION,
                  f"-> {sc.get('contract_version')!r} vs {server.contract.CONTRACT_VERSION!r}")
            check("structured_content carries ok",
                  sc.get("ok") is True, f"-> {sc.get('ok')!r}")

        # A rejected/coded-failure result also carries `code` — `_coded` wraps
        # every tool, so this is not calc_exact-specific.
        bad = await client.call_tool("calc_exact", {"expr": "not a valid expr((("})
        bad_sc = getattr(bad, "structured_content", None)
        check("a failing result's structured_content carries `code` (errors.ensure_code, via _coded)",
              isinstance(bad_sc, dict) and bad_sc.get("ok") is False and bool(bad_sc.get("code")),
              f"-> {bad_sc!r}")

        # tools/list itself: outputSchema is now emitted, not None — except
        # two tools deliberately left untyped. session_read_file returns
        # either a dict OR a raw ImageContent (server.py, as_image=True /
        # image files); session_run returns either a dict OR a list of
        # content blocks (TextContent + artifact blocks) when a run produces
        # artifacts. Typing either union wraps EVERY reply in {"result": ...}
        # (measured against the pinned SDK) — a wire shape change to a tool
        # whose current path already works, and for session_run's list
        # branch specifically, the SDK's own output validation then REJECTS
        # the call ("validation error for DictModel ... Input should be a
        # valid dictionary") instead of returning a result.
        _UNTYPED_ON_PURPOSE = {"session_read_file", "session_run"}
        listed = {t.name: t for t in (await client.list_tools()).tools}
        no_schema = {n for n, t in listed.items() if t.output_schema is None}
        check("every tool's tools/list entry carries a non-None outputSchema, "
              "except the ones deliberately left untyped",
              no_schema == _UNTYPED_ON_PURPOSE, f"-> {sorted(no_schema)}")


asyncio.run(_structured_content_check())

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL TOOL-ANNOTATION TESTS PASS ===")
sys.exit(1 if FAILS else 0)
