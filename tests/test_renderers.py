"""The renderer extension kind (codecalc/renderers.py).

Standalone, like tests/test_extensions.py: a check() accumulator, sys.exit(1)
on any failure. Runs every built-in renderer plus the reference third-party
CSV renderer through `_renderer_conformance.run_renderer_conformance`, then
exercises discovery and the `RendererRegistry.render` convenience.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from _renderer_conformance import SAMPLE_RESULT, run_renderer_conformance

from codecalc import extensions as ext
from codecalc import renderers
from examples.extensions.csv_renderer import CsvRenderer

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── conformance: every built-in + the reference third-party renderer ────────
for renderer in (renderers.TextRenderer(), renderers.MarkdownTableRenderer(), CsvRenderer()):
    label = renderer.manifest.extension_id

    def recording_check(name: str, cond: bool, detail: str = "", _label=label) -> None:
        check(f"[{_label}] {name}", cond, detail)

    run_renderer_conformance(renderer, recording_check)

# ── registration + discovery across origins ─────────────────────────────────
registry = renderers.RendererRegistry()
registry.register(renderers.TextRenderer())
registry.register(renderers.MarkdownTableRenderer())
registry.register(CsvRenderer())

descs = registry.descriptors()
by_id = {d["extension_id"]: d for d in descs}
check("discovery lists all three renderers",
      set(by_id) == {"builtin:text", "builtin:markdown-table", "example.csv-renderer"},
      f"-> {sorted(by_id)}")
check("the built-in text renderer is origin builtin",
      by_id["builtin:text"]["origin"] == "builtin")
check("the built-in markdown renderer is origin builtin",
      by_id["builtin:markdown-table"]["origin"] == "builtin")
check("the reference CSV renderer is origin third_party",
      by_id["example.csv-renderer"]["origin"] == "third_party")

# ── RendererRegistry.render convenience ──────────────────────────────────────
text_output = registry.render("builtin:text", SAMPLE_RESULT, "text")
check("registry.render dispatches to the text renderer",
      text_output.format == "text" and "verdict: OK" in text_output.body,
      f"-> {text_output.body[:40]!r}")

csv_output = registry.render("example.csv-renderer", SAMPLE_RESULT, "csv")
check("registry.render dispatches to the third-party CSV renderer",
      csv_output.format == "csv" and "verdict" in csv_output.body,
      f"-> {csv_output.body[:40]!r}")

try:
    registry.render("builtin:text", SAMPLE_RESULT, "definitely-not-a-format")
except ext.UnsupportedCapability as exc:
    check("registry.render raises UnsupportedCapability for a bad format",
          exc.capability == "definitely-not-a-format", f"-> {exc.capability!r}")
else:
    check("registry.render cannot silently accept a bad format", False)


print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== RENDERERS OK ===")
sys.exit(1 if FAILS else 0)
