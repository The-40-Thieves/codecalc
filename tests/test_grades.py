"""Derivation-table tests for codecalc/grades.py.

codecalc's verify_* tools already refuse to fabricate confidence — see
tests/test_translation_verify.py for the classify_case/aggregate rules this
module NAMES rather than re-derives. What none of them did, before this,
was surface how strong that evidence is in one small, versioned vocabulary:
`executed` / `cross_checked` / `solver_proven`, plus the explicit non-grade
`ungraded`.

Two rules are load-bearing and get their own section below:

  1. NEVER OVER-GRADE. A result never gets a stronger grade than its evidence
     supports. Tested by feeding grade_verify_translation/optimization/z3_check
     synthetic evidence dicts shaped exactly like a REAL failure (mismatch,
     rejected candidate, solver timeout) — and, deliberately, a z3 `sat`
     verdict, which is a real DECIDED answer and not a failure at all but is
     still excluded from `solver_proven` (narrowed post-review: see
     codecalc/grades.py's "solver_proven" docstring section) — and asserting
     the grade is `ungraded`, never one of the two real positive grades.

  2. NON-SUCCESS STAYS UNGRADED. Same evidence, restated as its own section so
     a reader looking for "what happens to a failure" does not have to infer
     it from the over-grade tests.

Every derivation test constructs the evidence dict BY HAND rather than by
calling the real verifiers with real code, mirroring
tests/test_translation_verify.py's own reasoning: these are PURE functions
over a dict shape, and pinning that shape here means the grading rules are
tested without a sandbox, two runtimes or z3 actually running — z3_check's
own evidence-emission (engine/timeout_ms) gets a couple of real calls at the
bottom, because that plumbing genuinely needs z3 running to be honest.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import grades, logic

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── fixtures: evidence dicts shaped exactly like the real verifiers' output ──

def translation_pass(matched=3, total=4, mismatched=0, inconclusive=1):
    return {"passed": True, "reason": None, "matched": matched,
            "mismatched": mismatched, "inconclusive": inconclusive,
            "total": total, "cases": []}


def translation_mismatch():
    return {"passed": False, "reason": "1 of 4 test input(s) showed different behaviour",
            "matched": 2, "mismatched": 1, "inconclusive": 1, "total": 4, "cases": []}


def translation_all_inconclusive():
    return {"passed": False,
            "reason": "could not verify: no test input produced a successful run "
                      "of BOTH programs, so nothing was actually compared",
            "matched": 0, "mismatched": 0, "inconclusive": 4, "total": 4, "cases": []}


def optimization_accepted(language="python3", ratio=1.8, n_sizes=4, matched=4, total=4):
    return {"ok": True, "accepted": True, "reason": "verified faster",
            "speedup": {"ratio": ratio, "measurable": True,
                        "per_size": [{"n": n, "before_ms": 10, "after_ms": 5, "ratio": ratio}
                                     for n in range(n_sizes)]},
            "min_speedup": 1.15,
            "verification": {"passed": True, "matched": matched, "total": total},
            "language": language}


def optimization_not_equivalent():
    return {"ok": True, "accepted": False, "reason": "not equivalent",
            "verification": {"passed": False, "matched": 0, "mismatched": 1, "total": 4},
            "detail": "the candidate does not reproduce the original's output; "
                      "speed was not measured, because a faster wrong answer is "
                      "not an optimisation"}


def optimization_not_faster_enough():
    return {"ok": True, "accepted": False, "reason": "equivalent but not measurably faster",
            "speedup": {"ratio": 1.02, "measurable": True, "per_size": []},
            "min_speedup": 1.15,
            "verification": {"passed": True, "matched": 4, "total": 4},
            "language": "python3"}


def optimization_measurement_failed():
    return {"ok": False, "error": "baseline measurement failed: language not available"}


def z3_sat(timeout_ms=5000):
    return {"ok": True, "result": "sat", "model": {"x": "6"}, "assertions": 2,
            "engine": "z3 5.0.0.0", "timeout_ms": timeout_ms}


def z3_unsat(timeout_ms=5000):
    return {"ok": True, "result": "unsat", "model": {"unsat_core": None}, "assertions": 2,
            "engine": "z3 5.0.0.0", "timeout_ms": timeout_ms}


def z3_unknown(timeout_ms=1):
    return {"ok": True, "result": "unknown", "model": None, "assertions": 2,
            "engine": "z3 5.0.0.0", "timeout_ms": timeout_ms}


def z3_rejected():
    return {"ok": False, "error": "empty SMT-LIB2 script: nothing to check."}


# ═══ 1. derivation table: verify_translation ════════════════════════════════

g = grades.grade_verify_translation(translation_pass(), "python3", "node")
check("translation pass -> cross_checked", g["grade"] == grades.CROSS_CHECKED, f"-> {g['grade']}")
check("translation grade_basis names BOTH runtimes",
      "python3" in g["grade_basis"] and "node" in g["grade_basis"], f"-> {g['grade_basis']}")
check("translation grade carries the rules version",
      g["grade_rules_version"] == grades.GRADE_RULES_VERSION)
check("translation grading does not mutate the input dict",
      "grade" not in translation_pass())

g = grades.grade_verify_translation(translation_mismatch(), "python3", "node")
check("translation mismatch -> ungraded, never cross_checked",
      g["grade"] == grades.UNGRADED, f"-> {g['grade']}")

g = grades.grade_verify_translation(translation_all_inconclusive(), "python3", "go")
check("translation all-inconclusive -> ungraded",
      g["grade"] == grades.UNGRADED, f"-> {g['grade']}")


# ═══ 2. derivation table: verify_optimization ═══════════════════════════════

g = grades.grade_verify_optimization(optimization_accepted(language="rust", ratio=2.3), "rust")
check("optimization accepted -> cross_checked", g["grade"] == grades.CROSS_CHECKED, f"-> {g['grade']}")
check("optimization grade_basis names the runtime AND the measured speedup",
      "rust" in g["grade_basis"] and "2.3" in g["grade_basis"], f"-> {g['grade_basis']}")

g = grades.grade_verify_optimization(optimization_not_equivalent(), "python3")
check("optimization rejected for correctness -> ungraded, never cross_checked",
      g["grade"] == grades.UNGRADED, f"-> {g['grade']}")

g = grades.grade_verify_optimization(optimization_not_faster_enough(), "python3")
check("optimization equivalent-but-not-faster -> ungraded "
      "(correctness alone does not earn a grade for THIS claim)",
      g["grade"] == grades.UNGRADED, f"-> {g['grade']}")

g = grades.grade_verify_optimization(optimization_measurement_failed(), "python3")
check("optimization measurement failure -> ungraded", g["grade"] == grades.UNGRADED, f"-> {g['grade']}")


# ── F7: an ACCEPTED result with a measured SLOWDOWN is never cross_checked ──
# optimization.py accepts on ratio >= min_speedup with min_speedup unvalidated
# (it may be <= 1 — a pre-existing defect, ticketed separately), so an
# `accepted=True` result can carry a measured SLOWDOWN. The GRADE must not
# certify that as cross_checked "on the speed dimension" — a ratio that is not a
# real speedup (> 1) leaves the optimisation claim unproven and stays ungraded,
# exactly like an equivalent-but-not-faster candidate.
def optimization_accepted_but_slower(ratio=0.5):
    return {"ok": True, "accepted": True, "reason": "accepted (min_speedup unvalidated)",
            "speedup": {"ratio": ratio, "measurable": True,
                        "per_size": [{"n": 1, "before_ms": 5, "after_ms": 10, "ratio": ratio}]},
            "min_speedup": 0,
            "verification": {"passed": True, "matched": 4, "total": 4},
            "language": "python3"}


g = grades.grade_verify_optimization(optimization_accepted_but_slower(0.5), "python3")
check("F7: accepted-but-slower (ratio 0.5x) is NOT cross_checked",
      g["grade"] != grades.CROSS_CHECKED, f"-> {g['grade']}")
check("F7: ...it is ungraded — a slowdown never wears a speed grade",
      g["grade"] == grades.UNGRADED, f"-> {g['grade']}")
check("F7: the ungraded basis names the non-speedup ratio, not a fake speedup",
      "0.5" in g["grade_basis"] and "cross-checked" not in g["grade_basis"],
      f"-> {g['grade_basis']}")
g_eq = grades.grade_verify_optimization(optimization_accepted_but_slower(1.0), "python3")
check("F7: accepted at exactly 1.0x (no speedup) is also ungraded",
      g_eq["grade"] == grades.UNGRADED, f"-> {g_eq['grade']}")


# ═══ 3. derivation table: z3_check ═══════════════════════════════════════════

g = grades.grade_z3_check(z3_unsat())
check("z3 unsat -> solver_proven", g["grade"] == grades.SOLVER_PROVEN, f"-> {g['grade']}")
check("z3 grade_basis names engine version AND the timeout bound",
      "5.0.0.0" in g["grade_basis"] and "5000" in g["grade_basis"], f"-> {g['grade_basis']}")

g = grades.grade_z3_check(z3_sat())
check("z3 sat -> ungraded, NOT solver_proven (narrowed: solver_proven is "
      "reserved for unsat so a counterexample can never wear a proof grade "
      "— see grades.py's module docstring 'solver_proven' section)",
      g["grade"] == grades.UNGRADED, f"-> {g['grade']}")
check("z3 sat's grade_basis states BOTH facts: decided, and why reserved",
      "sat" in g["grade_basis"] and "decided" in g["grade_basis"]
      and "solver_proven is reserved for unsat" in g["grade_basis"],
      f"-> {g['grade_basis']}")

g = grades.grade_z3_check(z3_unknown())
check("z3 unknown -> ungraded (no proof either way)", g["grade"] == grades.UNGRADED, f"-> {g['grade']}")

g = grades.grade_z3_check(z3_rejected())
check("z3 rejected input (empty script) -> ungraded", g["grade"] == grades.UNGRADED, f"-> {g['grade']}")


# ═══ 4. NEVER OVER-GRADE: the hard rule, restated as its own explicit check ═
# Every non-success fixture above must land on the SAME weakest grade,
# regardless of which tool or which flavour of failure produced it. A grader
# that special-cased just one failure shape into a stronger grade would pass
# the individual checks above but fail this loop.
_NON_SUCCESS_FIXTURES = [
    ("verify_translation: mismatch",
     grades.grade_verify_translation(translation_mismatch(), "python3", "node")),
    ("verify_translation: all-inconclusive",
     grades.grade_verify_translation(translation_all_inconclusive(), "python3", "go")),
    ("verify_optimization: not equivalent",
     grades.grade_verify_optimization(optimization_not_equivalent(), "python3")),
    ("verify_optimization: not faster enough",
     grades.grade_verify_optimization(optimization_not_faster_enough(), "python3")),
    ("verify_optimization: measurement failed",
     grades.grade_verify_optimization(optimization_measurement_failed(), "python3")),
    ("z3_check: unknown", grades.grade_z3_check(z3_unknown())),
    ("z3_check: rejected", grades.grade_z3_check(z3_rejected())),
    # sat is a real, DECIDED answer (not a failure the way the others above
    # are) but it is deliberately excluded from solver_proven — narrowed
    # per the fix round below. It belongs in this sweep precisely because a
    # grader that folded it back into solver_proven would pass every check
    # above except this one.
    ("z3_check: sat (decided, but not solver_proven — narrowed on purpose)",
     grades.grade_z3_check(z3_sat())),
]
for _label, _graded in _NON_SUCCESS_FIXTURES:
    check(f"never-over-grade: {_label} never outranks ungraded",
          _graded["grade"] == grades.UNGRADED, f"-> {_graded['grade']}")


# ═══ 5. every graded result carries a basis and a rules version ════════════
_ALL_GRADED = [g for _, g in _NON_SUCCESS_FIXTURES] + [
    grades.grade_verify_translation(translation_pass(), "python3", "node"),
    grades.grade_verify_optimization(optimization_accepted(), "python3"),
    grades.grade_z3_check(z3_unsat()),
]
check("every graded result has a non-empty grade_basis",
      all(r.get("grade_basis") for r in _ALL_GRADED))
check("every graded result names the rules version this module ships",
      all(r.get("grade_rules_version") == grades.GRADE_RULES_VERSION for r in _ALL_GRADED))
check("grade is always one of the four vocabulary values, never something ad hoc",
      all(r.get("grade") in grades.GRADES for r in _ALL_GRADED))


# ═══ 6. z3_check itself emits the evidence grading depends on (real z3) ═════
# Not a synthetic fixture: logic.z3_check must genuinely record engine/
# timeout_ms, because grade_z3_check only READS them — see codecalc/logic.py.
_real_unsat = logic.z3_check(
    "(declare-const x Int)(assert (> x 5))(assert (< x 3))(check-sat)")
check("logic.z3_check emits an engine string", bool(_real_unsat.get("engine")),
      f"-> {_real_unsat.get('engine')!r}")
check("logic.z3_check emits the timeout bound it ran under",
      _real_unsat.get("timeout_ms") == 5000, f"-> {_real_unsat.get('timeout_ms')}")
_real_graded = grades.grade_z3_check(_real_unsat)
check("...and grade_z3_check turns that into solver_proven end to end",
      _real_graded.get("grade") == grades.SOLVER_PROVEN, f"-> {_real_graded.get('grade')}")
check("...with the REAL z3 engine string in grade_basis, not a fixture's fake one",
      _real_unsat["engine"] in _real_graded.get("grade_basis", ""),
      f"-> {_real_graded.get('grade_basis')}")


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL GRADE-VOCABULARY TESTS PASS ===")
sys.exit(1 if FAILS else 0)
