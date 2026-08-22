"""Regression lock for scripts/tool_select_eval.py — the tool-SELECTION eval.

CONTRIBUTING.md: "A gate is not trusted until it has been watched failing."
Every assertion below was seeded against a defect and watched fail before
being kept, same as tests/test_skill_detect.py and tests/test_fuzz_smoke.py.
This suite is also the record of a cross-vendor (Codex) review of the v1
cut of this eval that found it "silently permissive for reduced-surface
regressions and corpus shrinkage" — every fix below has a corresponding
locked assertion, not just a prose claim that it was fixed:

  - self-check now SWEEPS every candidate tool (no sampling) and runs
    separately per `--tools` preset — v1's fixed seed=0 sample of 10 never
    once drew `limit_expression`/`analyze_complexity`/`extract_function`,
    so ablating any of the three passed CI unnoticed. Watched failing: an
    unreachable `SELF_CHECK_MIN_POOLED_LOST` correctly reports `inert=True`.
  - the checked-in baseline pins the exact prompt corpus by content hash
    (`prompt_set_sha256`) — deleting every 4th prompt from the v1 196-prompt
    set (156 left, still >= 3/tool) RAISED measured full-surface accuracy
    0.602 -> 0.6346 and would have passed the old per-tool-floor-only
    coverage check. Watched failing: a --data pointed at a 195-prompt
    (one line removed) copy is rejected with a distinct "CORPUS CHANGED"
    error, never scored against the old numbers.
  - `--baseline` now compares integer top-1 HIT COUNTS on the pinned
    corpus, not a float ratio: `--epsilon nan` used to fail OPEN (a real
    1.2pp regression passed); `--epsilon` is now `type=int`, so a
    non-numeric value is rejected at argument-parsing, before any
    comparison runs at all. Watched failing: `--epsilon nan` now exits 2
    with `invalid int value: 'nan'`, and a doctored baseline claiming a
    perfect 196/196 fails the compare with an exact `drop_hits` count.
  - the name-leak validator normalizes singular/plural on both sides —
    "list all unit aliases" (`list_units`) and "show runtime statuses"
    (`runtimes_status`) used to slip through (`units`/`unit` and
    `statuses`/`status` compared as different tokens). Watched failing: both
    planted phrasings are now rejected, and a THIRD planted case
    ("please evaluate this expression for me") is planted specifically to
    exercise the token-SUBSET branch rather than the literal-substring
    branch the first two plants both hit first — v1's own planted tests
    never actually exercised that branch.
  - 7 prompts were missing an obviously-acceptable alternate tool (found by
    the same review, each verified here against the REAL implementation —
    see the module-level comment below each one — not taken on the
    review's word alone).

Standalone script, not pytest — see tests/conftest.py and CONTRIBUTING.md's
"Running the gates locally" for why: this suite asserts at module scope and
calls sys.exit at the end, and pytest's import-based collection would abort
on the first one.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
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

# Pins the exact current corpus size as a FLOOR, not just ">= 3/tool" — the
# per-tool floor alone is exactly what let a 156-prompt shrink of the v1
# 196-prompt set (still >= 3/tool) pass while RAISING measured accuracy.
# Growing the corpus is fine (a floor, not an exact match); shrinking it
# without a deliberate re-baseline (which also updates this number) is not.
CHECKED_IN_PROMPT_COUNT_FLOOR = 196
check(f"tool_select_prompts.jsonl has >= {CHECKED_IN_PROMPT_COUNT_FLOOR} prompts "
      f"(pinned floor, not just a per-tool minimum)",
      len(prompts) >= CHECKED_IN_PROMPT_COUNT_FLOOR, f"-> {len(prompts)}")

# Coverage floor: every tool in the 'full' surface needs >= 3 prompts naming
# it as the PRIMARY (expected[0]) answer — this repo's own spec for the v1
# labeled set. Derived from the live registry, never a hardcoded 52.
full_schemas = tse.load_tool_schemas("full")
primary_counts = Counter(e["expected"][0] for e in prompts)
undercovered = sorted(name for name in full_schemas if primary_counts[name] < 3)
check(f"every tool in the full surface ({len(full_schemas)} tools) has >= 3 "
      f"primary-labeled prompts", not undercovered, f"-> undercovered: {undercovered}")

# ── the name-token validator flags PLANTED violations, then finds none ──────
# in the real set. Three plants, not two: the first two both hit the
# LITERAL-substring branch (they contain the underscore name verbatim), so
# neither ever exercised the TOKEN-SUBSET branch — a real gap a cross-vendor
# review caught in v1's own "effective" claim. The third plant has no
# underscore in it anywhere and can ONLY be caught by the token-subset path.

planted = [
    {"prompt": "Please call evaluate_expression on this formula", "expected": ["evaluate_expression"]},
    {"prompt": "I need the float_repr of this number", "expected": ["float_repr"]},
    {"prompt": "please evaluate this expression for me", "expected": ["evaluate_expression"]},
    {"prompt": "This one's fine — a real ask with no tool name in it", "expected": ["calc_exact"]},
]
planted_violations = tse.validate_prompts(planted)
check("validate_prompts() flags a prompt containing its own exact tool name",
      any("evaluate_expression" in v and "literal tool name" in v for v in planted_violations),
      f"-> {planted_violations}")
check("validate_prompts() flags a prompt containing every underscore-token of its tool name "
      "(literal-substring plant)",
      any("float_repr" in v for v in planted_violations), f"-> {planted_violations}")
check("validate_prompts() flags the TOKEN-SUBSET-only plant (no underscore substring present) "
      "via the token-subset branch specifically, not the literal-substring one",
      any("please evaluate this expression for me" in v and "underscore-token" in v
          for v in planted_violations),
      f"-> {planted_violations}")
check("validate_prompts() flags exactly the 3 planted violations, not the clean 4th entry",
      len(planted_violations) == 3, f"-> {planted_violations}")

# Singular/plural normalization, both directions — the two gaps a
# cross-vendor review found by construction (neither is in the checked-in
# set; both are planted here to prove the fix, then re-checked absent below).
plural_planted = [
    {"prompt": "Please list all unit aliases for me", "expected": ["list_units"]},
    {"prompt": "Show runtime statuses right now", "expected": ["runtimes_status"]},
]
plural_violations = tse.validate_prompts(plural_planted)
check("validate_prompts() catches 'unit aliases' (plural) against list_units "
      "('unit'/'alias' singular tool tokens)",
      any("list_units" in v for v in plural_violations), f"-> {plural_violations}")
check("validate_prompts() catches 'runtime statuses' (plural) against runtimes_status "
      "('runtimes'/'status' tool tokens)",
      any("runtimes_status" in v for v in plural_violations), f"-> {plural_violations}")
check("validate_prompts() flags exactly the 2 planted plural violations",
      len(plural_violations) == 2, f"-> {plural_violations}")

real_violations = tse.validate_prompts(prompts)
check("the checked-in prompt set has ZERO name-token violations "
      "(including after singular/plural normalization)",
      len(real_violations) == 0, f"-> {real_violations[:5]}")

# ── label-quality fixes: 7 prompts gained a verified acceptable alternate ───
# Each was checked against the REAL implementation (not taken on a review's
# word) — see the commit that added these for the exact verification run.
# Recorded here as content assertions, not just "the file changed", so a
# future edit that silently drops one of these is caught.
LABEL_FIXES = {
    # evaluate_expression's OWN docstring uses this exact input as its
    # example; simplify_expression("sqrt(144) + 2**10") independently
    # verified to return the same numeric answer under all three of its
    # forms (original/simplified/factored/expanded all "1036").
    "Please simplify this symbolic math formula for me: sqrt(144) + 2**10": "simplify_expression",
    # logic.evaluate_expression('2**64 - 1') verified to return the exact
    # arbitrary-precision integer '18446744073709551615'.
    "I need an arbitrary-precision rational computation of 2 to the 64th power minus 1": "evaluate_expression",
    "give me the exact rational result of this expression before I judge whether it clears a limit": "evaluate_expression",
    # logic.solve_linear('2*x + 1 = 7', ['x']) verified to return {x: 3} —
    # a system of ONE equation/ONE unknown is a valid degenerate input.
    "For 2*x + 1 = 7, what's x?": "solve_linear",
    # logic.evaluate_expression's own result shape carries BOTH 'simplified'
    # and 'expanded' keys (verified directly), matching this prompt's dual
    # ask ("simplest form" + "multiplied out") almost exactly.
    "Reduce this formula to its simplest form and also show it multiplied out": "evaluate_expression",
    "Take this messy algebraic formula and give me its cleanest possible form": "evaluate_expression",
    # update_runtimes' own docstring: "SAFE BY DEFAULT: with apply=False
    # this is a dry run — it returns the update commands that WOULD run
    # without changing anything" — exactly this prompt's ask.
    "Check, without changing anything, whether newer versions exist for my language toolchains and what command would upgrade them": "update_runtimes",
}
prompts_by_text = {e["prompt"]: e for e in prompts}
missing_fix = [p for p, alt in LABEL_FIXES.items()
               if p not in prompts_by_text or alt not in prompts_by_text[p]["expected"]]
check(f"all {len(LABEL_FIXES)} verified label fixes are present in the checked-in set",
      not missing_fix, f"-> missing: {missing_fix}")

# ── the eval meets its measured floor on the checked-in baseline ────────────
# Floor = the measured v1 baseline (top1_hits=119/196 on --tools full, see
# scripts/data/tool_select_baseline.json) minus a small margin, so BM25's
# alphabetical tie-break (a real source of run-to-run-STABLE but
# description-ORDER-sensitive variance — see BM25.rank's own docstring)
# cannot flip this red on a future description edit that is a net
# improvement elsewhere.
FULL_TOP1_FLOOR = 0.55

result = tse.evaluate(full_schemas, prompts)
check(f"--tools full: top1={result['top1']} ({result['top1_hits']}/{result['n']}) "
      f">= floor {FULL_TOP1_FLOOR} (measured v1 baseline: 0.6071)",
      result["top1"] >= FULL_TOP1_FLOOR)
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

RESULTS_BY_GROUP = {"full": result, "dev": dev_result, "core": core_result}
SCHEMAS_BY_GROUP = {"full": full_schemas, "dev": dev_schemas, "core": core_schemas}

# ── self-check: run the FULL ablation sweep for EVERY preset ────────────────
# Finding (cross-vendor review): a tool can be top-1-WRONG against `full`'s
# 51 distractors (zero headroom to lose) while having real, ablation-
# detectable headroom against `core`'s much smaller distractor set —
# `limit_expression` measured exactly this (full: 0 hits before AND after
# ablation; core: drops when ablated). Running self-check only once, on
# `full`, cannot see that. So this runs it three times, once per preset.

for group in tse.TOOL_GROUPS:
    sc = tse.run_self_check(group, prompts, SCHEMAS_BY_GROUP[group])
    check(f"--tools {group}: self-check sweeps EVERY candidate tool, no sampling "
          f"(n_candidates={sc['n_candidates']})",
          sc["n_candidates"] == len(SCHEMAS_BY_GROUP[group]),
          f"-> swept {sc['n_candidates']} of {len(SCHEMAS_BY_GROUP[group])} tools")
    check(f"--tools {group}: self-check pooled_lost={sc['pooled_lost']} "
          f">= floor {tse.SELF_CHECK_MIN_POOLED_LOST}, "
          f"tools_showing_loss={sc['tools_showing_loss']} "
          f">= floor {tse.SELF_CHECK_MIN_TOOLS_SHOWING_LOSS}",
          not sc["inert"], f"-> {sc}")
    # The three tools a cross-vendor review named as excluded by v1's fixed
    # sample must now actually be swept (present in per_tool at all) for at
    # least the presets where they are active candidates.
    for named_tool in ("limit_expression", "analyze_complexity", "extract_function"):
        if named_tool in SCHEMAS_BY_GROUP[group] and primary_counts[named_tool] > 0:
            check(f"--tools {group}: previously-excluded tool {named_tool!r} is now "
                  f"included in the self-check sweep",
                  named_tool in sc["per_tool"], f"-> per_tool keys: {sorted(sc['per_tool'])}")

# Watched failing: an unreachable pooled-loss floor must report inert=True.
# This is the seeded-defect proof CONTRIBUTING.md asks for, kept as a live
# assertion rather than a one-time manual check.
_orig_floor = tse.SELF_CHECK_MIN_POOLED_LOST
try:
    tse.SELF_CHECK_MIN_POOLED_LOST = 10**6  # no real ablation sweep can clear this
    unreachable = tse.run_self_check("core", prompts, core_schemas)
finally:
    tse.SELF_CHECK_MIN_POOLED_LOST = _orig_floor
check("self-check correctly reports inert=True when the pooled-loss floor is unreachable "
      "(proves the inert-detection path itself is live, not just its happy path)",
      unreachable["inert"] is True, f"-> pooled_lost={unreachable['pooled_lost']}")

# ── corpus pinning: prompts_content_hash() and the "CORPUS CHANGED" gate ────

real_baseline = json.loads(
    (REPO_ROOT / "scripts" / "data" / "tool_select_baseline.json").read_text(encoding="utf-8")
)
check("the checked-in baseline has an entry for every --tools group plus prompt_set_sha256",
      set(real_baseline) >= {"full", "dev", "core", "prompt_set_sha256"},
      f"-> {sorted(real_baseline)}")

current_hash = tse.prompts_content_hash(prompts)
check("prompts_content_hash() of the checked-in corpus matches the checked-in baseline "
      "(proves the baseline was generated FROM this exact file)",
      current_hash == real_baseline["prompt_set_sha256"],
      f"-> current={current_hash} baseline={real_baseline['prompt_set_sha256']}")

shrunk = prompts[1:]  # one prompt removed — still >= 3/tool for every affected tool
check("prompts_content_hash() changes when even ONE prompt is removed",
      tse.prompts_content_hash(shrunk) != current_hash)

# Watched failing via the real CLI (not just the hash function in isolation):
# a --data file that hashes differently from the checked-in baseline must be
# refused with the distinct "CORPUS CHANGED" message, exit 1, for EVERY
# preset — not scored against the old numbers under a shrunk/edited corpus.
_shrunk_path = REPO_ROOT / "scripts" / "data" / ".tool_select_prompts.shrunk_for_test.jsonl"
_shrunk_path.write_text(
    "\n".join(json.dumps(e) for e in shrunk) + "\n", encoding="utf-8"
)
try:
    for group in tse.TOOL_GROUPS:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "tool_select_eval.py"),
             "--tools", group, "--data", str(_shrunk_path),
             "--baseline", str(REPO_ROOT / "scripts" / "data" / "tool_select_baseline.json")],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
        check(f"--tools {group}: a corpus-hash mismatch exits non-zero via the real CLI",
              proc.returncode == 1, f"-> rc={proc.returncode} stderr={proc.stderr[-300:]!r}")
        check(f"--tools {group}: the corpus-hash mismatch names itself distinctly "
              f"('CORPUS CHANGED'), not a generic accuracy-drop message",
              "CORPUS CHANGED" in proc.stdout, f"-> stdout={proc.stdout[-500:]!r}")
finally:
    _shrunk_path.unlink(missing_ok=True)

# ── --baseline: integer hit-count compare, epsilon validated ────────────────
# Watched failing via the real CLI: --epsilon nan must be rejected at
# argument-parsing (type=int), never reach the comparison at all.
proc = subprocess.run(
    [sys.executable, str(REPO_ROOT / "scripts" / "tool_select_eval.py"),
     "--tools", "full", "--epsilon", "nan",
     "--baseline", str(REPO_ROOT / "scripts" / "data" / "tool_select_baseline.json")],
    cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
)
check("--epsilon nan is rejected at argument-parsing (exit 2), never reaches the compare",
      proc.returncode == 2 and "invalid int value: 'nan'" in proc.stderr,
      f"-> rc={proc.returncode} stderr={proc.stderr[-300:]!r}")

proc = subprocess.run(
    [sys.executable, str(REPO_ROOT / "scripts" / "tool_select_eval.py"),
     "--tools", "full", "--epsilon", "-1",
     "--baseline", str(REPO_ROOT / "scripts" / "data" / "tool_select_baseline.json")],
    cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
)
check("a negative --epsilon is rejected explicitly",
      proc.returncode == 1 and "must be >= 0" in proc.stdout,
      f"-> rc={proc.returncode} stdout={proc.stdout[-300:]!r}")

# Watched failing: a doctored baseline claiming a perfect (impossible) hit
# count must fail the compare with an EXACT integer drop_hits, for every
# preset — this is the actual regression gate other suites/CI rely on, run
# against all three surfaces, not just `full`.
for group in tse.TOOL_GROUPS:
    doctored = json.loads(json.dumps(real_baseline))  # deep copy via round-trip
    doctored[group]["top1_hits"] = doctored[group]["n"]  # impossibly perfect
    doctored_path = REPO_ROOT / "scripts" / "data" / f".tool_select_baseline.doctored_{group}.json"
    doctored_path.write_text(json.dumps(doctored), encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "tool_select_eval.py"),
             "--tools", group, "--baseline", str(doctored_path)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
        expected_drop = doctored[group]["n"] - RESULTS_BY_GROUP[group]["top1_hits"]
        check(f"--tools {group}: a doctored perfect-score baseline fails the compare "
              f"with drop_hits={expected_drop}",
              proc.returncode == 1 and f"dropped by {expected_drop} prompt" in proc.stdout,
              f"-> rc={proc.returncode} stdout={proc.stdout[-400:]!r}")
    finally:
        doctored_path.unlink(missing_ok=True)

    # The REAL, checked-in baseline must NOT regress against the current
    # code, for every preset.
    real_drop = real_baseline[group]["top1_hits"] - RESULTS_BY_GROUP[group]["top1_hits"]
    check(f"--tools {group}: the CURRENT eval does not regress against the checked-in "
          f"baseline (drop_hits={real_drop} <= epsilon={tse.DEFAULT_EPSILON_HITS})",
          real_drop <= tse.DEFAULT_EPSILON_HITS,
          f"-> baseline={real_baseline[group]['top1_hits']} "
          f"current={RESULTS_BY_GROUP[group]['top1_hits']}")

# The real CLI, real baseline, no doctoring — the exact command CI runs, for
# all three presets.
for group in tse.TOOL_GROUPS:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "tool_select_eval.py"),
         "--tools", group,
         "--baseline", str(REPO_ROOT / "scripts" / "data" / "tool_select_baseline.json")],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
    )
    check(f"--tools {group}: the real CLI against the real checked-in baseline exits 0",
          proc.returncode == 0, f"-> rc={proc.returncode} stdout={proc.stdout[-300:]!r}")

# ── BM25 unit checks: values, not shapes ─────────────────────────────────────

check("tokenize() lowercases, drops stopwords and single characters",
      tse.tokenize("The Quick a I") == ["quick"], f"-> {tse.tokenize('The Quick a I')}")

check("_token_matches() matches 'units'<->'unit' and 'statuses'<->'status' "
      "(the two confirmed name-leak gaps) both directions",
      tse._token_matches("units", "unit") and tse._token_matches("unit", "units")
      and tse._token_matches("statuses", "status") and tse._token_matches("status", "statuses")
      and tse._token_matches("runtimes", "runtime") and tse._token_matches("runtime", "runtimes"))
_MANGLED_STEM = "status"[:-1]  # the wrong 4-letter fragment a blind strip-s stemmer produced
check("_token_matches() does NOT match unrelated tokens, and 'status' matches only itself/"
      "'statuses' — never mangled into a shorter wrong form the way a blind "
      "strip-trailing-s stemmer would",
      not tse._token_matches("status", "state")
      and not tse._token_matches("units", "unify")
      and "status" in tse._plural_variants("status")
      and _MANGLED_STEM not in tse._plural_variants("status"),
      f"-> variants(status)={sorted(tse._plural_variants('status'))}")

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
