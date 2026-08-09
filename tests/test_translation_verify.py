"""Verification-gate tests for verify_translation / verify_optimization.

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

from codecalc import executor, optimization, translation
from codecalc.translation import aggregate, classify_case, compare_edge_cases

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


# ── compare_edge_cases must not hide output-ORDER divergence ────────────────
# The regression: a divergence key built from sorted(stdout.splitlines())
# makes two languages that emit the same lines in a different order compare
# equal, so no divergence is ever reported for exactly the class of bug this
# tool exists to catch (map/dict iteration order, sort stability,
# concurrency). classify_case/_normalize in this same module compare stdout
# order-sensitively; compare_edge_cases must agree by default.
_orig_run = translation._run
try:
    translation._run = lambda lang, code, stdin, timeout=15: {
        "ok": True, "stdout": code, "verdict": "OK", "stderr": ""}

    r = compare_edge_cases({"a": "x\ny\n", "b": "y\nx\n"}, inputs=["i"])
    check("same lines, different order -> IS a divergence by default",
          r["divergence_count"] == 1, f"-> {r['divergence_count']}")
    check("  ...classified as an 'order' divergence, not folded into agreement",
          r["divergences"] and r["divergences"][0]["kind"] == "order",
          f"-> {r.get('divergences')}")

    r = compare_edge_cases({"a": "x\ny\n", "b": "y\nx\n"}, inputs=["i"],
                           order_sensitive=False)
    check("order_sensitive=False restores the old order-insensitive comparison",
          r["divergence_count"] == 0, f"-> {r['divergence_count']}")

    r = compare_edge_cases({"a": "x\ny\n", "b": "x\nz\n"}, inputs=["i"])
    check("a real content difference is still a divergence",
          r["divergence_count"] == 1 and r["divergences"][0]["kind"] == "content",
          f"-> {r.get('divergences')}")

    r = compare_edge_cases({"a": "x\ny\n", "b": "x\ny\n"}, inputs=["i"])
    check("identical output in identical order -> no divergence",
          r["divergence_count"] == 0, f"-> {r['divergence_count']}")
finally:
    translation._run = _orig_run


# ═══ the gates are callable on their own, with no model anywhere ═══════════
# They used to run only as the second half of a tool that first asked a
# separately configured model to write the candidate. The caller of this server
# IS a language model; making it supply the candidate removes the dependency
# and puts the strongest model in the loop in charge of the creative half.
if executor._rust:
    SRC = "import sys\nn=int(sys.stdin.readline())\nprint(n*2)"
    GOOD = 'const n=+require("fs").readFileSync(0,"utf8").trim();console.log(n*2)'

    r = translation.verify_translation("python3", SRC, "node", GOOD, ["3", "7", "0"])
    check("a correct port passes", r.get("passed") is True, f"-> {r.get('summary')}")
    r = translation.verify_translation("python3", SRC, "node", "console.log(1)", ["3", "7"])
    check("a wrong port fails", r.get("passed") is False)

    SLOW = "import sys\nn=int(sys.stdin.readline())\ns=0\nfor i in range(n): s+=i\nprint(s)"
    FAST = "import sys\nn=int(sys.stdin.readline())\nprint(n*(n-1)//2)"
    SIZES = [200000, 400000, 800000, 1600000]

    o = optimization.verify_optimization(SLOW, FAST, "python3",
                                         test_inputs=["10", "100", "1000"], sizes=SIZES)
    check("a real O(n)->O(1) win is accepted", o.get("accepted") is True,
          f"-> ratio={(o.get('speedup') or {}).get('ratio')} {o.get('reason')!r}")
    check("  ...and equivalence was checked first",
          (o.get("verification") or {}).get("passed") is True)

    # The rejections carry what an optimiser that fabricates wins cannot: WHICH
    # gate failed, and by how much.
    o = optimization.verify_optimization(FAST, FAST, "python3",
                                         test_inputs=["10", "100"], sizes=SIZES)
    check("an unchanged candidate is rejected as not faster",
          o.get("accepted") is False and "not measurably faster" in (o.get("reason") or ""),
          f"-> {o.get('reason')!r}")

    o = optimization.verify_optimization(SLOW, "print(999)", "python3",
                                         test_inputs=["10", "100"])
    check("a faster-but-wrong candidate is rejected on correctness",
          o.get("accepted") is False and o.get("reason") == "not equivalent",
          f"-> {o.get('reason')!r}")
    check("  ...and its speed was never measured", "speedup" not in o,
          "-> a faster wrong answer is not an optimisation")
else:
    print("SKIP live verification gates (no native executor built)")


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL TRANSLATION-VERIFICATION TESTS PASS ===")
sys.exit(1 if FAILS else 0)
