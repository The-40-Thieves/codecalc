#!/usr/bin/env python3
"""Assert the `tier` claim on every LANGUAGES entry is honest.

registry.RELIABILITY_TIERS ("tested" / "best_effort" / "plan_only") is a claim
about codecalc's OWN CI, kept next to — but never confused with — the
RESOLUTION states (`supported`/`installed`/`unhealthy`/`available`) a live
probe of one machine produces. Resolution and reliability drift apart in
exactly the way this ticket exists to name: a review's smoke test found the
rust and csharp host TOOLCHAINS failing on a machine where both commands
resolved cleanly.

`tier: "tested"` is the strong claim, so it is the one gated in both
directions:

  1. every LANGUAGES entry carries a tier from RELIABILITY_TIERS at all
     (the floor — a typo'd tier string would otherwise sail through as
     falsy/"None" and every check below would compare against nothing);
  2. the `tested`-tier set in the registry matches, EXACTLY, the set the
     CI-wired source files actually exercise — not a hand-maintained list
     copied from what CI does, but a live regex read of those files'
     literal source, so editing any one without updating the others
     fails the build instead of drifting silently.

Why these files and no others: contract_check.py's CANDIDATES list (also
naming ruby/perl/php/lua/deno) picks the FIRST available candidate, and
python3 is unconditionally installed before that probe runs on every CI leg
(actions/setup-python) — so those five never get picked in practice, and a
candidate CI cannot reach is not evidence of anything. `node`'s evidence comes
from a different harness entirely: tests/test_python_sweep.py dynamically
starts a real node worker session and asserts genuine stdout from it (state
round-trips through the worker, a captured fd-1 escape) on every PR, in the
`tests` job of ci-python.yml — not from contract_check.py, where node is
equally a dead candidate. `rust` and `go` earn theirs from a third harness:
tests/test_tier_evidence.py compiles and runs a real program in each through
executor.execute() and asserts its computed stdout — and because that file
skips per-language when a toolchain is absent, check 2b below also requires
ci-python.yml's evidence STEP — the step block itself, not the file as a
whole — to invoke it with CODECALC_REQUIRE_TIER_EVIDENCE set (the flag that
promotes any skip to a failure), gate on the real Linux condition rather
than a disabling `if:`, and carry no `continue-on-error`. Without 2b the
harness could silently drop out of CI (or run skip-permissive, or run with
its failure eaten) while the tier claim stood on nothing.

FAIL-FIRST, proven by hand while writing this gate: flipping "node"'s tier
in codecalc/registry.py from "tested" to "best_effort" makes check 2 report a
set mismatch (registry {"python3"} vs CI-wired {"python3", "node"}) and the
script exits 1. Reverting the edit makes it exit 0 again. tests/test_runtime_
tiers.py automates exactly that round-trip against a scratch copy of the repo
so the proof does not rot into a comment nobody re-runs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from codecalc import registry  # noqa: E402 — needs the path above

TEST_PYTHON_SWEEP = (REPO / "tests" / "test_python_sweep.py").read_text(encoding="utf-8")
TEST_TIER_EVIDENCE = (REPO / "tests" / "test_tier_evidence.py").read_text(encoding="utf-8")
CONTRACT_CHECK = (REPO / "scripts" / "contract_check.py").read_text(encoding="utf-8")
CI_PYTHON_YML = (REPO / ".github" / "workflows" / "ci-python.yml").read_text(encoding="utf-8")

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        failures.append(name)


# ── 1. every entry carries a real tier ──────────────────────────────────────
missing_tier = sorted(name for name, entry in registry.LANGUAGES.items()
                      if entry.get("tier") not in registry.RELIABILITY_TIERS)
check(f"every LANGUAGES entry ({len(registry.LANGUAGES)}) has a valid tier",
      not missing_tier and len(registry.LANGUAGES) >= 10,
      f"-> invalid/missing on: {missing_tier}" if missing_tier else
      f"-> {len(registry.LANGUAGES)} entries checked")

# ── 2. the `tested` set matches what CI-wired source actually exercises ────
# Extracted from the literal source of test_python_sweep.py's WORKER_LANGS
# comprehension, not hand-copied — so a future edit to the tuple it filters
# from is what this check reads, always, rather than a snapshot of it.
_worker_match = re.search(
    r"WORKER_LANGS\s*=\s*\[lang for lang in \(([^)]*)\)\s+if _worker_usable\(lang\)\]",
    TEST_PYTHON_SWEEP,
)
if _worker_match is None:
    check("found tests/test_python_sweep.py's WORKER_LANGS candidate tuple",
          False, "-> regex matched nothing; the extractor is broken or the "
                 "source moved — this check proves nothing until it is fixed")
    ci_tested: set[str] = set()
else:
    ci_tested = {tok.strip().strip("'\"") for tok in _worker_match.group(1).split(",")
                if tok.strip()}
    check("found tests/test_python_sweep.py's WORKER_LANGS candidate tuple",
          bool(ci_tested), f"-> {sorted(ci_tested)}")

# Second CI-wired source: test_tier_evidence.py's literal tuple, same live
# read. Its languages have no stateful worker, so the sweep above can never
# be their evidence — this harness compiles and runs each one instead.
_evidence_match = re.search(
    r"TIER_EVIDENCE_LANGS\s*=\s*\(([^)]*)\)",
    TEST_TIER_EVIDENCE,
)
if _evidence_match is None:
    check("found tests/test_tier_evidence.py's TIER_EVIDENCE_LANGS tuple",
          False, "-> regex matched nothing; the extractor is broken or the "
                 "source moved — this check proves nothing until it is fixed")
    evidence_langs: set[str] = set()
else:
    evidence_langs = {tok.strip().strip("'\"") for tok in
                      _evidence_match.group(1).split(",") if tok.strip()}
    check("found tests/test_tier_evidence.py's TIER_EVIDENCE_LANGS tuple",
          bool(evidence_langs), f"-> {sorted(evidence_langs)}")
ci_tested |= evidence_langs

registry_tested = {name for name, entry in registry.LANGUAGES.items()
                   if entry.get("tier") == "tested"}

check("registry `tested` tier == CI-wired WORKER_LANGS + TIER_EVIDENCE_LANGS",
      bool(ci_tested) and registry_tested == ci_tested,
      f"-> registry: {sorted(registry_tested)}, CI-wired: {sorted(ci_tested)}"
      + ("" if registry_tested == ci_tested else
         f" — MISMATCH: only-in-registry={sorted(registry_tested - ci_tested)}, "
         f"only-in-CI={sorted(ci_tested - registry_tested)}"))

# ── 2b. the evidence harness is actually WIRED into CI, skip-proof ─────────
# test_tier_evidence.py skips per-language when a toolchain is absent (it has
# to, or no contributor could run the suite locally), so its mere existence
# proves nothing: CI must invoke it with CODECALC_REQUIRE_TIER_EVIDENCE set,
# the flag that turns any skip into a failure. Asserted against the STEP
# BLOCK, not the whole file — a bare substring search would still pass with
# the path living in a comment, the env var moved to an unrelated step, the
# step disabled by `if: false`, or its failure eaten by `continue-on-error`
# (cross-vendor review finding). The block is every line more-indented than
# the step's `- name:` line; the four properties below are checked inside it.
_wf_lines = CI_PYTHON_YML.splitlines()
_step_name = "- name: reliability-tier evidence (rust, go)"
_starts = [i for i, ln in enumerate(_wf_lines) if ln.strip() == _step_name]
check("ci-python.yml has exactly one reliability-tier evidence step",
      len(_starts) == 1, f"-> found {len(_starts)}")
if _starts:
    _indent = len(_wf_lines[_starts[0]]) - len(_wf_lines[_starts[0]].lstrip())
    _block: list[str] = []
    for _ln in _wf_lines[_starts[0] + 1:]:
        if _ln.strip() and (len(_ln) - len(_ln.lstrip())) <= _indent:
            break  # next step / comment / dedent ends the block
        _block.append(_ln)
    _stripped = [ln.strip() for ln in _block]
    check("  step gates on exactly `if: runner.os == 'Linux'` (not disabled)",
          "if: runner.os == 'Linux'" in _stripped,
          f"-> if-lines: {[ln for ln in _stripped if ln.startswith('if:')]}")
    check("  step sets CODECALC_REQUIRE_TIER_EVIDENCE: '1' in ITS env block",
          "CODECALC_REQUIRE_TIER_EVIDENCE: '1'" in _stripped)
    check("  step's run: line actually invokes tests/test_tier_evidence.py",
          any(ln.startswith("run:") and "tests/test_tier_evidence.py" in ln
              for ln in _stripped),
          f"-> run-lines: {[ln for ln in _stripped if ln.startswith('run:')]}")
    check("  step carries no continue-on-error",
          not any("continue-on-error" in ln for ln in _stripped))

# ── 3. corroborating evidence for python3 specifically ─────────────────────
# contract_check.py's CANDIDATES always resolves to whichever is FIRST and
# present; python3 is guaranteed present on every ci-rust.yml `contract` leg
# (actions/setup-python runs first), so it must lead the list for that
# guarantee to mean anything. Not proof on its own — check 2 is — but a
# reorder here would silently swap which language contract_check.py actually
# exercises, so it is worth catching independently.
_first_candidate = re.search(r'CANDIDATES = \[\s*\("([a-z0-9]+)"', CONTRACT_CHECK)
check("contract_check.py's CANDIDATES leads with python3",
      _first_candidate is not None and _first_candidate.group(1) == "python3",
      f"-> {_first_candidate.group(1) if _first_candidate else 'not found'}")
check("python3's registry tier is `tested`",
      registry.LANGUAGES.get("python3", {}).get("tier") == "tested")

# ── 4. the two identical compile plans agree ────────────────────────────────
# "cpp" and "c++" are the SAME g++ plan under two registry keys (README's
# ALIAS_ENTRIES treats them as one language). A tier that disagreed between
# them would be the same drift check_claims.py's language-count check exists
# to catch, one field over.
check("cpp and c++ (the same plan, two keys) carry the same tier",
      registry.LANGUAGES["cpp"]["tier"] == registry.LANGUAGES["c++"]["tier"],
      f"-> cpp={registry.LANGUAGES['cpp']['tier']!r} "
      f"c++={registry.LANGUAGES['c++']['tier']!r}")

print(f"\n=== {len(failures)} FAILURE(S) ===" if failures else
      "\n=== RUNTIME TIER CLAIMS HOLD ===")
sys.exit(1 if failures else 0)
