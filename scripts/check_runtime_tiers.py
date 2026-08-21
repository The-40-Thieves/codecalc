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
  2. the `tested`-tier set in the registry matches, EXACTLY, the set two
     CI-wired source files actually exercise — not a hand-maintained list
     copied from what CI does, but a live regex read of those files'
     literal source, so editing either file without updating the other
     fails the build instead of drifting silently.

Why those two files and no others: contract_check.py's CANDIDATES list (also
naming ruby/perl/php/lua/deno) picks the FIRST available candidate, and
python3 is unconditionally installed before that probe runs on every CI leg
(actions/setup-python) — so those five never get picked in practice, and a
candidate CI cannot reach is not evidence of anything. `node`'s evidence comes
from a different harness entirely: tests/test_python_sweep.py dynamically
starts a real node worker session and asserts genuine stdout from it (state
round-trips through the worker, a captured fd-1 escape) on every PR, in the
`tests` job of ci-python.yml — not from contract_check.py, where node is
equally a dead candidate.

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
CONTRACT_CHECK = (REPO / "scripts" / "contract_check.py").read_text(encoding="utf-8")

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

registry_tested = {name for name, entry in registry.LANGUAGES.items()
                   if entry.get("tier") == "tested"}

check("registry `tested` tier == CI-wired WORKER_LANGS candidates",
      bool(ci_tested) and registry_tested == ci_tested,
      f"-> registry: {sorted(registry_tested)}, CI-wired: {sorted(ci_tested)}"
      + ("" if registry_tested == ci_tested else
         f" — MISMATCH: only-in-registry={sorted(registry_tested - ci_tested)}, "
         f"only-in-CI={sorted(ci_tested - registry_tested)}"))

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
