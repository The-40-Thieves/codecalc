"""Regression lock for scripts/tool_select_eval.py — the tool-SELECTION eval.

CONTRIBUTING.md: "A gate is not trusted until it has been watched failing."
Every assertion below was seeded against a defect and watched fail before
being kept, same as tests/test_skill_detect.py and tests/test_fuzz_smoke.py:

  - the floor below was watched failing against `SELF_CHECK_MIN_DELTA = 0.99`
    (an unreachable threshold), which correctly reported `inert=True`;
  - the baseline-compare smoke below is ITSELF that seeded defect, kept as a
    permanent regression lock: a doctored baseline claiming top1=0.99 is
    watched failing the compare on every run, not just once by hand;
  - the name-token validator was watched flagging a deliberately planted
    violation ("evaluate_expression" typed into a prompt) before any real
    prompt was written, then re-run clean once the checked-in set passed.

Standalone script, not pytest — see tests/conftest.py and CONTRIBUTING.md's
"Running the gates locally" for why: this suite asserts at module scope and
calls sys.exit at the end, and pytest's import-based collection would abort
on the first one.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import Counter

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import tool_select_eval as tse

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── the checked-in prompt set loads and validates clean ─────────────────────

prompts = tse.load_prompts()
check(f"tool_select_prompts.jsonl parses ({len(prompts)} entries)", len(prompts) > 0)

# Coverage floor: every tool in the 'full' surface needs >= 3 prompts naming
# it as the PRIMARY (expected[0]) answer — this repo's own spec for the v1
# labeled set. Derived from the live registry, never a hardcoded 52.
full_schemas = tse.load_tool_schemas("full")
primary_counts = Counter(e["expected"][0] for e in prompts)
undercovered = sorted(name for name in full_schemas if primary_counts[name] < 3)
check(f"every tool in the full surface ({len(full_schemas)} tools) has >= 3 "
      f"primary-labeled prompts", not undercovered, f"-> undercovered: {undercovered}")

# ── the name-token validator flags a PLANTED violation, then finds none ─────
# in the real set. Order matters here: proving the detector CAN fire (on a
# violation deliberately inserted below) is what makes "zero violations on
# the real file" mean something instead of "the check never fires".

planted = [
    {"prompt": "Please call evaluate_expression on this formula", "expected": ["evaluate_expression"]},
    {"prompt": "I need the float_repr of this number", "expected": ["float_repr"]},
    {"prompt": "This one's fine — a real ask with no tool name in it", "expected": ["calc_exact"]},
]
planted_violations = tse.validate_prompts(planted)
check("validate_prompts() flags a prompt containing its own exact tool name",
      any("evaluate_expression" in v for v in planted_violations), f"-> {planted_violations}")
check("validate_prompts() flags a prompt containing every underscore-token of its tool name",
      any("float_repr" in v for v in planted_violations), f"-> {planted_violations}")
check("validate_prompts() flags exactly the 2 planted violations, not the clean 3rd entry",
      len(planted_violations) == 2, f"-> {planted_violations}")

real_violations = tse.validate_prompts(prompts)
check("the checked-in prompt set has ZERO name-token violations",
      len(real_violations) == 0, f"-> {real_violations[:5]}")

# ── the eval meets its measured floor on the checked-in baseline ────────────
# Floor = the measured v1 baseline (top1=0.602 on --tools full, 196 prompts,
# see scripts/data/tool_select_baseline.json) minus a small margin, so BM25's
# alphabetical tie-break (a real source of run-to-run-STABLE but description-
# ORDER-sensitive variance — see BM25.rank's own docstring) cannot flip this
# red on a future description edit that is a net improvement elsewhere.
FULL_TOP1_FLOOR = 0.55

result = tse.evaluate(full_schemas, prompts)
check(f"--tools full: top1={result['top1']} >= floor {FULL_TOP1_FLOOR} "
      f"(measured v1 baseline: 0.602)", result["top1"] >= FULL_TOP1_FLOOR)
check(f"--tools full: top3={result['top3']} >= top1 (top3 must never be lower)",
      result["top3"] >= result["top1"])
check("--tools full: every prompt was applicable (none skipped)",
      result["skipped"] == 0, f"-> skipped={result['skipped']}")

# ── --tools dev / core score a real SUBSET, never crash on a missing tool ───

dev_schemas = tse.load_tool_schemas("dev")
dev_result = tse.evaluate(dev_schemas, prompts)
check("--tools dev: some prompts are skipped (session/admin tools are absent)",
      dev_result["skipped"] > 0, f"-> skipped={dev_result['skipped']}")
check("--tools dev: applicable + skipped == total prompts",
      dev_result["n"] + dev_result["skipped"] == len(prompts))

core_schemas = tse.load_tool_schemas("core")
core_result = tse.evaluate(core_schemas, prompts)
check("--tools core: skips strictly more than dev (a smaller surface)",
      core_result["skipped"] > dev_result["skipped"],
      f"-> core={core_result['skipped']} dev={dev_result['skipped']}")

# ── self-check: the positive control this whole gate depends on ─────────────

self_check = tse.run_self_check("full", prompts, full_schemas)
check(f"self-check: ablating {len(self_check['sample'])} tools' descriptions drops "
      f"top1 by {self_check['delta']} (>= {tse.SELF_CHECK_MIN_DELTA} required)",
      not self_check["inert"], f"-> {self_check}")
check("self-check: the sample meets the documented minimum size",
      len(self_check["sample"]) >= tse.SELF_CHECK_MIN_SAMPLE)

# Watched failing: an unreachable threshold must report inert=True. This is
# the seeded-defect proof CONTRIBUTING.md asks for, kept as a live assertion
# rather than a one-time manual check — see the module docstring.
_orig_threshold = tse.SELF_CHECK_MIN_DELTA
try:
    tse.SELF_CHECK_MIN_DELTA = 0.99  # no real ablation can clear this
    unreachable = tse.run_self_check("full", prompts, full_schemas)
finally:
    tse.SELF_CHECK_MIN_DELTA = _orig_threshold
check("self-check correctly reports inert=True when the threshold is unreachable "
      "(proves the inert-detection path itself is live, not just its happy path)",
      unreachable["inert"] is True, f"-> delta={unreachable['delta']}")

# ── baseline-compare: a doctored, inflated baseline must FAIL the compare ───
# This is the seeded defect from the module docstring, kept as a permanent
# regression lock rather than a one-time manual check.

real_baseline = json.loads(
    (REPO_ROOT / "scripts" / "data" / "tool_select_baseline.json").read_text(encoding="utf-8")
)
check("the checked-in baseline has an entry for every --tools group",
      set(real_baseline) >= {"full", "dev", "core"}, f"-> {sorted(real_baseline)}")

doctored = json.loads(json.dumps(real_baseline))  # deep copy via round-trip
doctored["full"]["top1"] = 0.99  # impossibly high — no honest run clears this
drop = doctored["full"]["top1"] - (result["top1"] or 0.0)
check("baseline-compare: a doctored top1=0.99 baseline shows a drop past epsilon "
      "(this is what --baseline would exit non-zero on)",
      drop > tse.DEFAULT_EPSILON, f"-> drop={drop}")

# The real, checked-in baseline must NOT regress against the current code —
# this is the actual regression gate other suites/CI rely on.
real_drop = real_baseline["full"]["top1"] - (result["top1"] or 0.0)
check(f"the CURRENT eval does not regress against the checked-in baseline "
      f"(drop={round(real_drop, 4)} <= epsilon={tse.DEFAULT_EPSILON})",
      real_drop <= tse.DEFAULT_EPSILON, f"-> baseline={real_baseline['full']['top1']} "
      f"current={result['top1']}")

# ── BM25 unit checks: values, not shapes ─────────────────────────────────────

check("tokenize() lowercases, drops stopwords and single characters",
      tse.tokenize("The Quick a I") == ["quick"], f"-> {tse.tokenize('The Quick a I')}")

bm25 = tse.BM25({"a": "evaluate a symbolic math expression", "b": "run code in a sandbox"})
ranked = bm25.rank("evaluate this symbolic expression")
check("BM25.rank() puts the lexically-overlapping doc first",
      ranked[0][0] == "a", f"-> {ranked}")
tied = tse.BM25({"z": "nothing in common", "a": "nothing in common either"})
tied_ranked = tied.rank("completely unrelated query terms")
check("BM25.rank() ties break alphabetically, deterministically",
      tied_ranked[0][0] == "a", f"-> {tied_ranked}")

print(f"\n=== {len(FAILS)} failures ===" if FAILS else "\n=== ALL TOOL-SELECT-EVAL TESTS PASS ===")
sys.exit(1 if FAILS else 0)
