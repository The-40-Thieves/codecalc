"""A small, versioned grade vocabulary for verify_* results.

The verifiers in this repo already refuse to fabricate confidence:
`translation.classify_case` returns match/mismatch/inconclusive rather than
forcing "both failed" into a pass, `verify_optimization` never times a
candidate that failed the correctness gate, and `logic.z3_check` refuses to
call an empty script satisfiable. What none of them do is NAME, in one place,
how strong the evidence behind a passing result actually is. This module does
that naming — it derives a grade from evidence a verifier already emitted; it
never re-derives the evidence itself and it never runs anything.

Design rule the whole module exists to enforce: VERIFIERS EMIT EVIDENCE, THIS
MODULE ASSIGNS GRADES. `translation.py`, `optimization.py` and `logic.py` are
untouched by this ticket except where they were already recording a fact
(z3's engine version, the timeout bound it ran under) that this module needs
and had nowhere else to come from. None of them compute a grade inline.

## The three grades

  executed        The claimed computation actually ran, under a real
                   interpreter/compiler, and produced the reported result.
                   Bare execution evidence with no independent second
                   opinion. No tool below currently emits ONLY this grade —
                   every verify_* tool graded here happens to also clear the
                   bar for `cross_checked` — but it is part of the vocabulary
                   because a future single-implementation "did this actually
                   run" tool (e.g. a graded `execute_code`) belongs here, not
                   folded into a stronger grade it did not earn.

  cross_checked   Two INDEPENDENTLY authored programs were both actually run
                   and their outputs compared, and they agreed:
                   `verify_translation`'s source vs. target, or
                   `verify_optimization`'s original vs. candidate. Per the
                   ticket, `grade_basis` always NAMES the runtime(s) that did
                   the checking — even when both sides happen to share one
                   language (verify_optimization always does), because the
                   independence being certified is of the IMPLEMENTATION,
                   not necessarily the interpreter.

  solver_proven   Z3 returned `unsat` for the given formula within its
                   timeout — Z3's own refutation, a machine-checked verdict
                   from a bounded decision procedure. `grade_basis` always
                   records the engine version and the timeout bound.

                   ONLY `unsat`. This is deliberately NARROWER than "any
                   decisive Z3 verdict": an earlier version of this module
                   graded `sat` `solver_proven` too, reasoning that a
                   witness model is exactly as checkable by hand as a
                   refutation. That is true in isolation, but it does not
                   survive the ticket's own motivating pattern — proving a
                   property P by asserting not-P and checking unsat. A
                   caller running exactly that pattern who gets back `sat`
                   has learned P is FALSE, and a reader skimming `grade`
                   without also reading `result` would see `solver_proven`
                   on a result that just found a counterexample to what
                   they were trying to prove. `sat` is graded `ungraded`
                   instead (see below) — a real, checkable answer, just not
                   a proof-shaped one. Widening `sat` back to
                   `solver_proven` later is additive and safe to reconsider;
                   narrowing it after callers exist would be a breaking
                   semantic change to a grade already in use, so this ships
                   narrow. Only Z3's third possible verdict, "unknown",
                   also stays ungraded — it produces no proof either way.

  ungraded        Explicit non-grade for every non-success state: a
                   translation mismatch or inconclusive run, a rejected
                   optimisation candidate, a measurement failure, a Z3
                   "unknown" verdict, and — deliberately — a Z3 `sat`
                   verdict (see "solver_proven" above for why `sat` does
                   not qualify). `ungraded` is a real value on `grade`,
                   never an absent key — a caller can tell "graded and found
                   no evidence" apart from "this result predates grading"
                   only if the key is always present with an honest value.
                   `ungraded` is NEVER a softened stand-in for one of the
                   three grades above: nothing here ever downgrades match to
                   less than what the evidence shows, and this module never
                   upgrades a non-success into any of the three real grades
                   — see `grade_verify_translation`/`grade_verify_optimization`/
                   `grade_z3_check` below, each of which checks the success
                   flag FIRST and returns `ungraded` before looking at
                   anything else.

## Deliberately left ungraded

  algebraic_equiv  Compares two expressions via `sympy.simplify(a - b) == 0`.
                   That is a symbolic CAS transformation, not a decision
                   procedure with a checkable certificate the way Z3's
                   sat/unsat verdicts are, and it is not two independent
                   implementations agreeing — it is one simplifier's opinion
                   about one difference expression. None of the three grades
                   describes that evidence honestly, so `algebraic_equiv`'s
                   result shape is untouched by this ticket. (Making it
                   `solver_proven` by routing it through Z3 as well would be
                   a real capability change to the central claims engine,
                   which THE-785's reconciled scope explicitly excludes.)

## Versioning

`GRADE_RULES_VERSION` is bumped whenever a rule above changes what evidence
maps to what grade (not on prose-only edits). It travels on every graded
result as `grade_rules_version` so a caller comparing two results graded
under different rule versions can tell they are not directly comparable.
"""

from __future__ import annotations

#: Bump on any change to what evidence maps to what grade. See "Versioning"
#: above — this is a RULES version, not the package version.
#:
#: "2" (cross-vendor fix wave, F7): a `verify_optimization` result that is
#: `accepted` but whose measured speed ratio is not > 1 is now `ungraded` rather
#: than `cross_checked`. optimization.py can accept a slowdown (min_speedup is
#: unvalidated); the grade no longer certifies one as a speed-cross-checked
#: optimisation. This changes what evidence maps to what grade, hence the bump.
GRADE_RULES_VERSION = "2"

EXECUTED = "executed"
CROSS_CHECKED = "cross_checked"
SOLVER_PROVEN = "solver_proven"
#: The explicit non-grade. A real value, never an absent key — see the
#: "ungraded" section of the module docstring.
UNGRADED = "ungraded"

#: The whole vocabulary. Any other string on `grade` is a bug in this module.
GRADES = frozenset({EXECUTED, CROSS_CHECKED, SOLVER_PROVEN, UNGRADED})


def _graded(result: dict, grade: str, basis: str) -> dict:
    """Return a COPY of `result` with grade/grade_basis/grade_rules_version
    attached. Never mutates the verifier's own dict."""
    if grade not in GRADES:
        raise ValueError(f"unknown grade {grade!r} — not in codecalc.grades.GRADES")
    return {**result, "grade": grade, "grade_basis": basis,
            "grade_rules_version": GRADE_RULES_VERSION}


def grade_verify_translation(result: dict, source_language: str,
                             target_language: str) -> dict:
    """Grade a `translation.verify_translation` result.

    The evidence is already fully formed: `passed` is True only when at
    least one test input made BOTH programs actually run and agree, with no
    mismatch anywhere (see `translation.aggregate`). That is exactly
    `cross_checked` evidence — two independently authored programs, run and
    compared. A non-pass (mismatch, or nothing but inconclusive cases) is
    NEVER graded, regardless of how many cases were merely inconclusive.
    """
    if not result.get("passed"):
        reason = result.get("reason") or "no test input produced a comparable run"
        return _graded(result, UNGRADED, f"not graded: {reason}")
    matched, total = result.get("matched"), result.get("total")
    basis = (f"cross-checked: {source_language} source and {target_language} "
             f"port independently agreed on {matched}/{total} test input(s)")
    return _graded(result, CROSS_CHECKED, basis)


def grade_verify_optimization(result: dict, language: str) -> dict:
    """Grade an `optimization.verify_optimization` result.

    `accepted` is True only after the embedded correctness verification
    passed (itself `cross_checked`-shaped evidence: original vs. candidate,
    independently agreed) AND the speedup was actually measured and cleared
    `min_speedup`. Both halves are folded into one `cross_checked` grade
    because the top-level claim being graded — "candidate IS a genuine
    optimisation" — depends on both; `grade_basis` names both facts so
    neither is hidden. A candidate that is equivalent but not measurably
    faster (`accepted=False`) is a non-success for THIS claim even though its
    own correctness check passed, so it stays ungraded — the grade is about
    the tool's verdict, not about how far the candidate got.
    """
    if not result.get("accepted"):
        reason = result.get("reason") or result.get("error") or "not accepted"
        return _graded(result, UNGRADED, f"not graded: {reason}")
    verification = result.get("verification") or {}
    matched, total = verification.get("matched"), verification.get("total")
    speedup = result.get("speedup") or {}
    ratio = speedup.get("ratio")
    # F7 (cross-vendor): `cross_checked` folds a correctness check AND a real
    # speedup into ONE grade, so it may not be issued unless the speedup is
    # real. optimization.py now refuses accepted=True unless min_speedup > 1 AND
    # the measured ratio > 1 (fixed on this branch), so an accepted
    # result can no longer carry a measured SLOWDOWN. This branch is kept as a
    # defensive floor at the grade boundary: even if a future caller of
    # grade_verify_optimization hands in an accepted result with ratio <= 1, a
    # ratio that is not > 1 is not a speedup, leaving the "genuine optimisation"
    # claim unproven exactly like an equivalent-but-not-faster candidate — so it
    # stays ungraded rather than certifying a slowdown as cross_checked on the
    # speed dimension. (`bool` is an `int`, but a ratio is never a bool.)
    if not isinstance(ratio, (int, float)) or ratio <= 1:
        return _graded(result, UNGRADED,
                       f"not graded: accepted, and the correctness check passed, "
                       f"but the measured speed ratio {ratio}x is not a speedup "
                       f"(> 1x) — 'genuine optimisation' is unproven")
    sizes_measured = len(speedup.get("per_size") or [])
    basis = (f"cross-checked: original and candidate independently agreed on "
             f"{matched}/{total} test input(s) under {language} execution; "
             f"speed measured at {ratio}x (median) across {sizes_measured} size(s)")
    return _graded(result, CROSS_CHECKED, basis)


#: `sat`'s ungraded basis. NOT folded into `solver_proven` alongside `unsat`:
#: the ticket's motivating pattern is proving P by asserting not-P and
#: checking unsat, and under that pattern a `sat` result means P is FALSE —
#: grading it `solver_proven` would let a reader who skims `grade` without
#: `result` read a counterexample as a proof. Narrowing (this) is safe to
#: widen later; widening first and narrowing after callers exist would be a
#: breaking semantic change, so this ships narrow. See the module docstring's
#: "solver_proven" section for the full reasoning.
_SAT_BASIS = ("sat: a model was found — satisfiability is decided, but "
             "solver_proven is reserved for unsat verdicts so a counterexample "
             "can never wear a proof grade; widening sat to solver_proven "
             "would be a deliberate, separate decision")


def grade_z3_check(result: dict) -> dict:
    """Grade a `logic.z3_check` result.

    Only `unsat` is `solver_proven` — see `_SAT_BASIS` above and the module
    docstring's "solver_proven" section for why `sat`, despite also being a
    decisive verdict, is graded `ungraded` instead. `unknown` (the solver ran
    out of its timeout budget without deciding) is an explicit non-success
    and stays ungraded too, same as an outright rejection (empty script,
    missing `(check-sat)`, or a solver error). `engine`/`timeout_ms` are
    EVIDENCE `logic.z3_check` itself records — this function only reads
    them, it does not compute them.
    """
    if not result.get("ok"):
        return _graded(result, UNGRADED, f"not graded: {result.get('error')}")
    verdict = result.get("result")
    if verdict == "unsat":
        engine = result.get("engine") or "z3 (version unknown)"
        timeout_ms = result.get("timeout_ms")
        basis = f"{engine} returned unsat within its {timeout_ms}ms timeout"
        return _graded(result, SOLVER_PROVEN, basis)
    if verdict == "sat":
        return _graded(result, UNGRADED, _SAT_BASIS)
    return _graded(result, UNGRADED,
                   f"not graded: z3 returned {verdict!r}, which decides nothing")
