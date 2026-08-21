"""Smoke test for scripts/fuzz.py (THE-899): a small, fast, deterministic run
that CI can afford on every PR, standing in for the full multi-thousand-
iteration campaign a human runs before trusting a change to `safe_expr.py`'s
screen or `sessions.py`'s path guards.

Small and fixed-iteration on purpose: this is a REGRESSION gate, not the fuzz
campaign itself. It exists so a future change that reopens an
uncaught-exception escape — the exact shape scripts/fuzz.py was built to
catch, and did catch once already (see that module's own UnicodeEncodeError
finding, fixed in codecalc/safe_expr.py) — fails a normal CI run instead of
waiting for someone to remember to run the fuzzer by hand.

A DIFFERENT seed from scripts/fuzz.py's own default (12345, not 0): the point
of this suite is that the fuzzer's *machinery* stays correct across an
arbitrary stream, not that one specific stream stays clean forever. Both are
still fully deterministic, so either failing reproduces exactly by rerunning
scripts/fuzz.py with the seed this file prints.
"""

import pathlib
import random
import sys
import tempfile
from pathlib import Path

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import fuzz  # needs the path inserts above (E402 is ignored repo-wide for tests/*)

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


SEED = 12345
# Modest counts, not scripts/fuzz.py's own multi-thousand default: this file
# runs on every PR, the standalone script is for a deeper manual/CI-opt-in
# pass. sessions gets fewer than safe_expr — fuzz.py's own module docstring
# documents that _jail's path-resolve cost grows worse than linear, so this
# smoke test stays fast by keeping that target's count modest rather than by
# weakening what each individual call checks.
ITERATIONS_SAFE_EXPR = 200
ITERATIONS_SESSIONS = 100
TIMEOUT = 10.0  # matches scripts/fuzz.py's own default; see its module docstring

print(f"fuzz smoke test: seed={SEED}, safe_expr={ITERATIONS_SAFE_EXPR} sessions={ITERATIONS_SESSIONS}x2 "
      f"(reproduce a failure with: python scripts/fuzz.py --seed {SEED})")

rng = random.Random(SEED)  # noqa: S311 — corpus mutation, not cryptography; determinism is the point

crashes, n = fuzz.fuzz_safe_expr(rng, ITERATIONS_SAFE_EXPR, TIMEOUT)
check(f"safe_expr (classify_unsafe + safe_parse): {n} inputs, 0 crashes", len(crashes) == 0,
      f"-> {crashes[:3]}" if crashes else "")

with tempfile.TemporaryDirectory(prefix="codecalc-fuzz-smoke-") as tmp:
    base = Path(tmp) / "session-workspace"
    base.mkdir(parents=True, exist_ok=True)
    fuzz._plant_symlink_trap(base)
    crashes, n = fuzz.fuzz_jail(rng, ITERATIONS_SESSIONS, TIMEOUT, base)
    check(f"sessions._jail: {n} inputs, 0 crashes", len(crashes) == 0,
          f"-> {crashes[:3]}" if crashes else "")

    root = Path(tmp) / "session-root"
    root.mkdir(parents=True, exist_ok=True)
    crashes, n = fuzz.fuzz_session_dir(rng, ITERATIONS_SESSIONS, TIMEOUT, root)
    check(f"sessions._session_dir: {n} inputs, 0 crashes", len(crashes) == 0,
          f"-> {crashes[:3]}" if crashes else "")

print(f"\n=== {len(FAILS)} failures ===" if FAILS else "\n=== ALL FUZZ SMOKE TESTS PASS ===")
sys.exit(1 if FAILS else 0)
