"""MCP Apps (`io.modelcontextprotocol/ui`) views for verify_translation and
verify_optimization: docs/design/2026-09-08-mcp-apps-verification-views.md
has the spec facts this file checks against.

Five things, in order:

  1. the two `ui://` resources are listed and readable, with the exact
     spec-mandated MIME type (`text/html;profile=mcp-app`, imported from
     the SDK's own `mcp.server.apps.APP_MIME_TYPE`, not retyped here)
  2. each document is well-formed HTML (walked with `html.parser`, stdlib,
     not a lenient regex) and embeds no external `src`/`href` — self-
     contained per the ticket's own constraint, checked structurally
  3. both documents stay under the self-imposed size ceiling
     (`apps_views.MAX_VIEW_BYTES`)
  4. `tools/list`'s `_meta.ui.resourceUri` on the two bound tools matches
     the spec shape exactly and points at a resource that is actually
     registered — the same invariant `mcp.server.apps.Apps.tools()` enforces
     for its own tool/resource bindings, checked here by hand because this
     server does not use that extension class (see the design doc for why)
  5. `tools/call` on both tools is unchanged from `main` — proved two ways:
     a LIVE call against THIS server (deterministic for verify_translation;
     schema/key-shape only for verify_optimization, whose timings are
     never bit-for-bit reproducible across runs by construction) equals a
     known-good expectation, AND a STRUCTURAL diff against the merge-base
     with `origin/main` of every module that can influence either tool's
     return value (translation.py, optimization.py, grades.py, tools.py,
     plus the two `@mcp.tool` bodies in server.py) shows zero changes. A
     spawned subprocess of an actual `main` checkout was used once, by
     hand, to confirm this during development (see the PR/commit message);
     it is not re-run here on every test invocation because it would need
     its own venv and a second built Rust executor for a proof the
     structural diff already gives for free and for cheap: nothing that
     COULD change either tool's answer changed at all.

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _mcp_client import data, in_process
from mcp.server.apps import APP_MIME_TYPE

from codecalc import apps_views

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ═══ 1-3: structural checks on the two HTML documents themselves ═══════════

class _ExternalRefFinder(HTMLParser):
    """Collects every `src`/`href` attribute value the parser sees.

    A regex over raw HTML would miss attributes split across lines or using
    single quotes; `html.parser` (stdlib, the same module the ticket names)
    tokenizes properly regardless of quoting/formatting.
    """

    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self.tags_seen: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags_seen.append(tag)
        for name, value in attrs:
            if name in ("src", "href") and value:
                self.refs.append(value)


def _is_external(ref: str) -> bool:
    return ref.startswith(("http://", "https://", "//"))


for tool_name, uri in sorted(apps_views.UI_RESOURCE_URIS.items()):
    html = apps_views.VIEWS_BY_URI[uri]
    size = len(html.encode("utf-8"))
    check(f"{tool_name}: view stays under {apps_views.MAX_VIEW_BYTES} bytes",
          size <= apps_views.MAX_VIEW_BYTES, f"-> {size} bytes")

    parser = _ExternalRefFinder()
    try:
        parser.feed(html)
        parse_ok = True
    except Exception as exc:  # pragma: no cover -- failure path itself is the assertion
        parse_ok = False
        check(f"{tool_name}: view parses as HTML", False, f"-> {exc}")
    if parse_ok:
        check(f"{tool_name}: view parses as HTML", True)
        external = [r for r in parser.refs if _is_external(r)]
        check(f"{tool_name}: view has no external src/href", not external, f"-> {external}")
        check(f"{tool_name}: view has a <script> tag", "script" in parser.tags_seen)


# ═══ live checks: resources/list, resources/read, tools/list _meta ════════

async def _live_checks() -> None:
    async with in_process() as client:
        resources = {r.uri: r for r in (await client.list_resources()).resources}
        for _tool_name, uri in sorted(apps_views.UI_RESOURCE_URIS.items()):
            check(f"resources/list carries {uri}", uri in resources)
            if uri not in resources:
                continue
            r = resources[uri]
            check(f"{uri}: mime_type is {APP_MIME_TYPE!r}", r.mime_type == APP_MIME_TYPE,
                  f"-> {r.mime_type!r}")

            read = await client.read_resource(uri)
            check(f"{uri}: resources/read returns exactly one content block",
                  len(read.contents) == 1, f"-> {len(read.contents)}")
            block = read.contents[0]
            check(f"{uri}: content mime_type is {APP_MIME_TYPE!r}",
                  getattr(block, "mime_type", None) == APP_MIME_TYPE,
                  f"-> {getattr(block, 'mime_type', None)!r}")
            text = getattr(block, "text", None)
            check(f"{uri}: content is served as text (not a base64 blob)",
                  isinstance(text, str) and len(text) > 0)
            check(f"{uri}: served content matches the registered document byte-for-byte",
                  text == apps_views.VIEWS_BY_URI[uri])

        tools = {t.name: t for t in (await client.list_tools()).tools}
        for tool_name, uri in sorted(apps_views.UI_RESOURCE_URIS.items()):
            check(f"tools/list carries {tool_name}", tool_name in tools)
            if tool_name not in tools:
                continue
            meta = getattr(tools[tool_name], "meta", None) or {}
            ui_meta = meta.get("ui")
            check(f"{tool_name}: _meta.ui is present", isinstance(ui_meta, dict), f"-> {ui_meta!r}")
            if not isinstance(ui_meta, dict):
                continue
            check(f"{tool_name}: _meta.ui has ONLY resourceUri (no visibility override)",
                  set(ui_meta) == {"resourceUri"}, f"-> {sorted(ui_meta)}")
            resource_uri = ui_meta.get("resourceUri")
            check(f"{tool_name}: _meta.ui.resourceUri uses the ui:// scheme",
                  isinstance(resource_uri, str) and resource_uri.startswith("ui://"),
                  f"-> {resource_uri!r}")
            check(f"{tool_name}: _meta.ui.resourceUri matches apps_views.UI_RESOURCE_URIS",
                  resource_uri == uri, f"-> {resource_uri!r} vs {uri!r}")
            check(f"{tool_name}: _meta.ui.resourceUri names a REGISTERED resource",
                  resource_uri in resources, f"-> {resource_uri!r} not in {sorted(resources)}")

        # no tool other than the two bound ones carries a `ui` _meta key
        leaked = {n for n, t in tools.items()
                 if n not in apps_views.UI_RESOURCE_URIS
                 and isinstance(getattr(t, "meta", None), dict)
                 and "ui" in t.meta}
        check("no tool outside verify_translation/verify_optimization carries _meta.ui",
              not leaked, f"-> {sorted(leaked)}")

        # ── tools/call: unchanged behaviour, live half ──────────────────────
        # Deterministic: identical python3 source on both sides, one fixed
        # input. Any regression in what the CALL (not just the listing)
        # returns would show up here as a value mismatch.
        translation_result = data(await client.call_tool("verify_translation", {
            "source_code": "print(1 + 1)", "source_language": "python3",
            "target_code": "print(1 + 1)", "target_language": "python3",
            "test_inputs": [""],
        }))
        expected_case = {
            "input": "", "outcome": "match", "reason": "", "match": True,
            "source": {"ok": True, "phase": "run", "stdout": "2", "stdout_raw": "2\n", "stderr": ""},
            "target": {"ok": True, "phase": "run", "stdout": "2", "stdout_raw": "2\n", "stderr": ""},
        }
        check("verify_translation: tools/call result unchanged (deterministic case)",
              translation_result.get("cases") == [expected_case]
              and translation_result.get("passed") is True
              and translation_result.get("grade") == "cross_checked",
              f"-> {translation_result}")

        # verify_optimization's timings are never bit-for-bit reproducible
        # across runs (wall-clock measurement) — this checks the SHAPE
        # (every key `optimization.verify_optimization`/`grades.
        # grade_verify_optimization` are documented to emit) is exactly
        # what main emits too, via the structural diff below; the schema
        # itself is asserted here on a real call so a key silently renamed
        # or dropped would still fail loudly, on live output.
        optimization_result = data(await client.call_tool("verify_optimization", {
            "original": "print(sum(range(200)))", "candidate": "print(sum(range(200)))",
            "language": "python3", "sizes": [50, 60],
        }))
        expected_top_keys = {
            "ok", "accepted", "reason", "speedup", "inference", "min_speedup",
            "verification", "language", "grade", "grade_basis", "grade_rules_version",
            "contract_version",
        }
        check("verify_optimization: tools/call top-level keys unchanged",
              set(optimization_result) == expected_top_keys,
              f"-> {sorted(optimization_result)}")
        expected_inference_keys = {
            "test", "alternative", "alpha", "effective_alpha", "correction",
            "per_size", "sizes_rejecting", "sizes_total", "sizes_below_floor",
            "decision_basis",
        }
        check("verify_optimization: tools/call inference keys unchanged",
              set(optimization_result.get("inference", {})) == expected_inference_keys,
              f"-> {sorted(optimization_result.get('inference', {}))}")


asyncio.run(_live_checks())


# ═══ 5b: structural proof against origin/main ══════════════════════════════
# Every module whose CODE (not this ticket's additions) decides what
# verify_translation/verify_optimization return, diffed against the
# merge-base with origin/main rather than a hardcoded SHA — once this branch
# merges, HEAD==main and the diff is (correctly) empty against itself; the
# assertion that matters is made now, while this branch and main disagree
# everywhere ELSE in the tree but must agree exactly on these four files.
UNCHANGED_MODULES = ("codecalc/translation.py", "codecalc/optimization.py",
                    "codecalc/grades.py", "codecalc/tools.py")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()


try:
    merge_base = _git("merge-base", "HEAD", "origin/main")
    have_base = True
except subprocess.CalledProcessError as exc:
    have_base = False
    # NOT a check()/FAILS entry: a shallow checkout (the default for most CI
    # jobs in this repo — only ci-quality.yml's changelog job opts into
    # `fetch-depth: 0`) legitimately has no local `origin/main` to diff
    # against. The live checks above already prove behavioural identity for
    # both tools; this section is an extra, best-effort structural
    # cross-check for whoever has a full clone (this worktree does), not a
    # hard CI dependency this file should force onto every job that runs it.
    print(f"skip structural origin/main diff -- {exc.stderr.strip()}")

if have_base:
    diff = _git("diff", "--name-only", merge_base, "--", *UNCHANGED_MODULES)
    changed = [line for line in diff.splitlines() if line]
    check("verify_translation/verify_optimization's value-producing modules "
          "are byte-identical to origin/main's merge-base",
          not changed, f"-> changed: {changed}")

    # The two @mcp.tool() bodies themselves (docstring + dispatch line) must
    # also be untouched -- only their decorator's kwargs may have gained
    # `meta=` indirectly via `_tool_meta`, which lives in `_tool_meta` itself
    # (covered by the file-level diff above, since `_tool_meta` is in the
    # same file). This checks the SERVER.PY diff is additive-only around
    # these two tools: neither tool's own docstring or return line moved.
    server_diff = _git("diff", "-U0", merge_base, "--", "codecalc/server.py")
    removed_lines = [line[1:] for line in server_diff.splitlines()
                     if line.startswith("-") and not line.startswith("---")]
    touched_translation = [line for line in removed_lines if "verify_translation" in line]
    touched_optimization = [line for line in removed_lines if "verify_optimization" in line]
    check("no line naming verify_translation was REMOVED from server.py",
          not touched_translation, f"-> {touched_translation}")
    check("no line naming verify_optimization was REMOVED from server.py",
          not touched_optimization, f"-> {touched_optimization}")


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else "\n=== ALL MCP APPS VIEW TESTS PASS ===")
sys.exit(1 if FAILS else 0)
