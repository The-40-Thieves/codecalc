"""Verification-gate tests for translate_code / optimize_code.

The bug these exist for: verify_translation() treated "both programs failed" as
a MATCH ("same behavior class"). That made the gate report success for a
translation with no relationship to the source — and it did so most reliably in
the case where verification is least meaningful, namely when the source
program's runtime is missing so it cannot run at all.

    verify_translation('python3', 'import sys; sys.exit(1)',
                       'node',    'process.exit(1)', ['', '0', '1'])
    ->  passed: True, matched 3/3

These tests drive `classify_case`, a PURE function over two executor result
dicts. That split is deliberate: the old logic could only be exercised by
actually running two programs in the sandbox, so it needed a built binary and
two working runtimes, and it was therefore never tested at all. A trust
boundary that can only be checked where the toolchain is installed is not one
that gets checked.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc.translation import aggregate, classify_case

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def ok(stdout="", **kw):
    return {"ok": True, "phase": "run", "verdict": "OK", "stdout": stdout,
            "stderr": "", "exit_code": 0, **kw}


def rte(stderr="boom", stdout="", **kw):
    return {"ok": False, "phase": "run", "verdict": "RTE", "stdout": stdout,
            "stderr": stderr, "exit_code": 1, **kw}


def compile_error(stderr="syntax error", **kw):
    return {"ok": False, "phase": "compile", "verdict": "RTE", "stdout": "",
            "stderr": stderr, "exit_code": 1, **kw}


def tle(**kw):
    return {"ok": False, "phase": "run", "verdict": "TLE", "stdout": "",
            "stderr": "<killed>", "exit_code": None, "timed_out": True, **kw}


# ── the regression: both-failed must never be positive evidence ─────────────
outcome, _ = classify_case(rte("ZeroDivisionError"), rte("panic: nil map"))
check("both failed -> INCONCLUSIVE, not match", outcome == "inconclusive",
      f"-> {outcome}")

outcome, _ = classify_case(rte(), tle())
check("both failed, different verdicts -> inconclusive", outcome == "inconclusive",
      f"-> {outcome}")

# ── a translation that does not compile is WRONG, never 'equivalent' ────────
outcome, _ = classify_case(rte(), compile_error())
check("target compile error -> MISMATCH even when source failed",
      outcome == "mismatch", f"-> {outcome}")
outcome, _ = classify_case(ok("7"), compile_error())
check("target compile error vs working source -> mismatch", outcome == "mismatch")

# ── a broken SOURCE means we cannot verify, not that we verified ────────────
outcome, _ = classify_case(compile_error(), ok("7"))
check("source compile error -> inconclusive", outcome == "inconclusive")

# ── the ordinary cases still behave ─────────────────────────────────────────
outcome, _ = classify_case(ok("42\n"), ok("42"))
check("both ok, same stdout (trailing ws normalised) -> match", outcome == "match")
outcome, _ = classify_case(ok("42"), ok("43"))
check("both ok, different stdout -> mismatch", outcome == "mismatch")
outcome, _ = classify_case(ok("42"), rte())
check("source ok, target failed -> mismatch", outcome == "mismatch")
outcome, _ = classify_case(rte(), ok("42"))
check("source failed, target ok -> mismatch", outcome == "mismatch")

# ── aggregation: inconclusive alone can never pass ──────────────────────────
r = aggregate([("inconclusive", "x"), ("inconclusive", "x"), ("inconclusive", "x")])
check("all inconclusive -> NOT passed", r["passed"] is False, f"-> {r['reason']}")
check("all inconclusive -> says why", "could not" in (r["reason"] or "").lower(),
      f"-> {r['reason']}")

r = aggregate([("match", ""), ("inconclusive", "x")])
check("one real match + one inconclusive -> passed", r["passed"] is True)

r = aggregate([("match", ""), ("mismatch", "differs")])
check("any mismatch -> not passed", r["passed"] is False)

r = aggregate([("match", ""), ("match", "")])
check("all match -> passed", r["passed"] is True)

r = aggregate([])
check("no cases at all -> NOT passed", r["passed"] is False,
      f"-> {r['reason']}")

# ── counts reported honestly ────────────────────────────────────────────────
r = aggregate([("match", ""), ("inconclusive", "x"), ("mismatch", "d")])
check("counts are broken out", (r["matched"], r["inconclusive"], r["mismatched"]) == (1, 1, 1),
      f"-> {r['matched']}/{r['inconclusive']}/{r['mismatched']}")

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL TRANSLATION-VERIFICATION TESTS PASS ===")
sys.exit(1 if FAILS else 0)
