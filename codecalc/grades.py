"""A small, versioned grade vocabulary for verify_* results (THE-785).

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

  solver_proven   Z3 decided satisfiability of the given formula — sat or
                   unsat — within its timeout. Both are machine-checked
                   verdicts from a bounded decision procedure: unsat is
                   Z3's own refutation, sat is a concrete witness model a
                   caller can plug back in and check by hand. Only Z3's
                   third possible verdict, "unknown", produces no proof
                   either way and stays ungraded. `grade_basis` always
                   records the engine version and the timeout bound.

                   The ticket's own phrasing for this grade is "z3 returned
                   unsat for the negation / proof" — the classic pattern for
                   proving a property P by showing not-P is unsat. z3_check
                   has no way to know whether a caller's script encodes a
                   negation; it only ever sees an arbitrary SMT-LIB2 script.
                   Grading here therefore generalises to ANY decisive Z3
                   verdict on the script it was actually given, which is the
                   most that can be derived from the evidence z3_check emits
                   without guessing at caller intent. This is a documented,
                   deliberate reading — see the THE-785 report for the
                   alternative (unsat-only) considered and why it was
                   rejected as strictly narrower with no offsetting safety
                   benefit (a "sat" verdict is exactly as checkable as an
                   "unsat" one).

  ungraded        Explicit non-grade for every non-success state: a
                   translation mismatch or inconclusive run, a rejected
                   optimisation candidate, a measurement failure, or a Z3
                   "unknown" verdict. `ungraded` is a real value on `grade`,
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
GRADE_RULES_VERSION = "1"

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
    sizes_measured = len(speedup.get("per_size") or [])
    basis = (f"cross-checked: original and candidate independently agreed on "
             f"{matched}/{total} test input(s) under {language} execution; "
             f"speed measured at {ratio}x (median) across {sizes_measured} size(s)")
    return _graded(result, CROSS_CHECKED, basis)


def grade_z3_check(result: dict) -> dict:
    """Grade a `logic.z3_check` result.

    Only a decisive verdict (`sat` or `unsat`) is `solver_proven` — see the
    module docstring's "solver_proven" section for why both directions
    qualify. `unknown` (the solver ran out of its timeout budget without
    deciding) is an explicit non-success and stays ungraded, same as an
    outright rejection (empty script, missing `(check-sat)`, or a solver
    error). `engine`/`timeout_ms` are EVIDENCE `logic.z3_check` itself now
    records — this function only reads them, it does not compute them.
    """
    if not result.get("ok"):
        return _graded(result, UNGRADED, f"not graded: {result.get('error')}")
    verdict = result.get("result")
    if verdict not in ("sat", "unsat"):
        return _graded(result, UNGRADED,
                       f"not graded: z3 returned {verdict!r}, which decides nothing")
    engine = result.get("engine") or "z3 (version unknown)"
    timeout_ms = result.get("timeout_ms")
    basis = f"{engine} returned {verdict} within its {timeout_ms}ms timeout"
    return _graded(result, SOLVER_PROVEN, basis)
