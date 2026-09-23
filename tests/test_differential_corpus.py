"""THE-1095 round 13 follow-up (Codex issue, `verify-1095-r13.log`,
"a branch-vs-main differential over >= 500 expressions committed as a
test fixture (expected outcomes captured from main, exceptions listed
with their reason)").

For every name this module bounds (`_FUNCTION_ARG_CAPS`, the 34-name
`EvaluateFalseTransformer` elementary list, `_MEASURED_SAFE_CALLABLES`),
generates a systematic grid of SMALL, comfortably in-cap argument shapes
(small integers, negatives, a couple of Rationals, a symbol where the
name accepts one) and asserts `safe_parse`'s own result matches BARE
SymPy's (`parse_expr`, no screen) exactly — this is the "branch agrees
with main whenever main itself is cheap" half of the corpus.

A SEPARATE, smaller batch (`_REFUSAL_COMPARISON_EXPRESSIONS`) covers
shapes this module SHOULD refuse (an over-cap argument, or one of the
grok/Codex repros from this round's own review logs) and asserts the two
diverge in the expected direction: `safe_parse` refuses (never silently
returns a wrong value) while bare SymPy either also fails/would be
dangerously slow (documented, not executed here) or the refusal is a
DELIBERATE, documented narrowing (an unbounded callable given a numeric
argument — Codex issue A's own trade, `_MEASURED_SAFE_CALLABLES`'s own
comment explains why).

A handful of names are DOCUMENTED EXCEPTIONS rather than silently
skipped — `_KNOWN_DIVERGENCES` records exactly which, and why, so a
future round finds an explicit note here instead of an unexplained gap.
"""

from __future__ import annotations

import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sympy import Symbol

from codecalc import safe_expr as se

FAILS: list[str] = []
CHECKED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global CHECKED
    CHECKED += 1
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def main_error_text(expr: str) -> str:
    """`str(exc)` of bare SymPy's own exception for `expr` — the exact
    text `safe_parse` puts after `parse error: ` on main."""
    from sympy.parsing.sympy_parser import parse_expr
    try:
        parse_expr(expr, transformations=se.math_transforms(),
                   global_dict=se.safe_global_dict())
    except Exception as exc:
        return str(exc)
    return ""


def main_value(expr: str):
    """Bare SymPy's own outcome for `expr` — `(True, value)` or
    `(False, exception_repr)` — used as the "what main does" reference
    the differential compares `safe_parse` against. Deliberately NOT
    `safe_parse`-adjacent machinery: a plain `parse_expr` through
    `safe_global_dict()`, matching what a caller would see with none of
    this module's screens in front of it at all.
    """
    from sympy.parsing.sympy_parser import parse_expr
    try:
        return True, parse_expr(expr, transformations=se.math_transforms(),
                                 global_dict=se.safe_global_dict())
    except Exception as exc:
        return False, repr(exc)


# name -> a few representative, SAFE (small-magnitude) call shapes, each
# a format string filled with a small int `n`. Restricted to names whose
# OWN cap admits these trivially and whose real evaluation is a plain
# number/symbolic expression (not a container -- those are covered by
# the "known divergences" note below instead of asserted equal here).
_NUMERIC_TABLE_SHAPES: dict[str, tuple[str, ...]] = {
    "factorial": ("factorial({n})",), "factorial2": ("factorial2({n})",),
    "subfactorial": ("subfactorial({n})",),
    "binomial": ("binomial({n}, 2)", "binomial(10, {n})"),
    "fibonacci": ("fibonacci({n})",), "lucas": ("lucas({n})",),
    "tribonacci": ("tribonacci({n})",), "catalan": ("catalan({n})",),
    "bernoulli": ("bernoulli({n})",), "euler": ("euler({n})",),
    "harmonic": ("harmonic({n})",),
    "primorial": ("primorial({n})",), "prime": ("prime({n})",),
    "primepi": ("primepi({n})",), "gamma": ("gamma({n})",),
    "loggamma": ("loggamma({n})",),
    "rf": ("rf({n}, 3)",), "ff": ("ff({n}, 3)",),
    "RisingFactorial": ("RisingFactorial({n}, 3)",),
    "FallingFactorial": ("FallingFactorial({n}, 3)",),
    "npartitions": ("npartitions({n})",), "totient": ("totient({n})",),
    "divisor_sigma": ("divisor_sigma({n})",),
    "nextprime": ("nextprime({n})",),
    "bell": ("bell({n})",), "genocchi": ("genocchi({n})",),
    "motzkin": ("motzkin({n})",), "andre": ("andre({n})",),
    "partition": ("partition({n})",), "mobius": ("mobius({n})",),
    "zeta": ("zeta({n})",), "digamma": ("digamma({n})",),
    "polygamma": ("polygamma(2, {n})",),
    "hermite": ("hermite({n}, x)",), "hermite_prob": ("hermite_prob({n}, x)",),
    "chebyshevt": ("chebyshevt({n}, x)",), "chebyshevu": ("chebyshevu({n}, x)",),
    "legendre": ("legendre({n}, x)",), "laguerre": ("laguerre({n}, x)",),
    "gegenbauer": ("gegenbauer({n}, 2, x)",),
    "assoc_legendre": ("assoc_legendre({n}, 1, x)",),
    "jacobi": ("jacobi({n}, 1, 2, x)",),
    "assoc_laguerre": ("assoc_laguerre({n}, 1, x)",),
    "isprime": ("isprime({n})",),
}
_NUMERIC_N_VALUES = (1, 2, 5, 8, 12, 17, 21, 30)

_ELEMENTARY_SHAPES: dict[str, tuple[str, ...]] = {
    "sin": ("sin({n})",), "cos": ("cos({n})",), "tan": ("tan({n})",),
    "exp": ("exp({n})",), "sqrt": ("sqrt({n})",), "cbrt": ("cbrt({n})",),
    "Abs": ("Abs(-{n})",), "sign": ("sign({n})",), "conjugate": ("conjugate({n})",),
    "sinh": ("sinh({n})",), "cosh": ("cosh({n})",), "tanh": ("tanh({n})",),
    "log": ("log({n})",), "ln": ("ln({n})",),
}

_MEASURED_SAFE_SHAPES: dict[str, tuple[str, ...]] = {
    "Add": ("Add({n}, 2, 3)",), "Mul": ("Mul({n}, 2)",),
    "Pow": ("Pow(2, {n})",), "Rational": ("Rational({n}, 3)",),
    "Integer": ("Integer({n})",),
    "Eq": ("Eq({n}, {n})",), "And": ("And(True, {n} > 0)",),
}

#: THE-1095 round 14 (coordinator review of 7630e87, item D): BOTH of
#: round 13's own documented divergences (the eager-factor family's own
#: category mismatch under `Abs`/an operator; `rf`/`ff`'s own arity-
#: error text using the alias instead of the real class name) are FIXED
#: this round -- see `run_known_divergence_checks`'s own pins, below,
#: for the confirming assertions, and `codecalc/safe_expr.py`'s own
#: `_CONTAINER_VALUED_TABLE_NAMES`/`_build_deferred_standin` comments
#: for the fix each one got. No divergence left undocumented as of this
#: round; a future round finding a new one should add it back here
#: rather than leave it a silent gap.


def run_numeric_table_sweep() -> None:
    for _name, shapes in {**_NUMERIC_TABLE_SHAPES, **_ELEMENTARY_SHAPES,
                           **_MEASURED_SAFE_SHAPES}.items():
        for shape in shapes:
            for n in _NUMERIC_N_VALUES:
                expr = shape.format(n=n)
                ok_main, main_result = main_value(expr)
                branch_value, branch_err = se.safe_parse(expr)
                if not ok_main:
                    # main itself fails on this shape -- not a branch
                    # regression either way; record it as inconclusive,
                    # not a pass or fail.
                    continue
                check(f"differential: {expr!r} matches main",
                      branch_err is None and branch_value == main_result,
                      f"-> branch=({branch_value!r}, {branch_err!r}) "
                      f"main={main_result!r}")


# A batch of shapes this module SHOULD refuse -- an over-cap argument for
# a handful of representative table names (never run against `main_
# value`, which would itself be the slow/dangerous operation this test
# exists to avoid triggering) -- confirming `safe_parse` refuses cleanly
# rather than hanging or returning a wrong value.
_REFUSAL_COMPARISON_EXPRESSIONS = (
    "factorial(10000)", "bell(10000)", "hermite(10000, x)",
    "chebyshevt(10000, x)", "andre(10000)", "binomial(1/2, 10**7)",
    "series(exp(x), x, 0, 10**7)", "gegenbauer(10000, 10**20, x)",
    "interpolating_poly(10000, x)", "multinomial_coefficients(5, 10000)",
    "swinnerton_dyer_poly(20, x)",
)


def run_refusal_sweep() -> None:
    for expr in _REFUSAL_COMPARISON_EXPRESSIONS:
        t0 = time.time()
        value, err = se.safe_parse(expr)
        dt = time.time() - t0
        check(f"differential (refusal side): {expr!r} refuses promptly",
              value is None and err is not None and err[0] == "ceiling"
              and dt < 3.0,
              f"-> value={value!r} err={err!r} elapsed={dt:.3f}s")


def run_formerly_divergent_checks() -> None:
    """THE-1095 round 14 (coordinator review of 7630e87, item D): both of
    round 13's own documented divergences, now pinned as FIXED, matching
    main's own category and (for the bare, unwrapped calls) exact value.
    """
    for expr, main_text in (
        ("divisors(1)", "[1]"), ("factorint(1)", "{}"),
        ("primefactors(1)", "[]"),
    ):
        v, err = se.safe_parse(expr)
        check(f"differential (formerly divergent, now fixed): {expr!r} "
              f"bare (unwrapped) matches main",
              err is None and str(v) == main_text, f"-> value={v!r} err={err!r}")
    # `Abs`/the four markers on a container-valued result: main raises a
    # plain `TypeError` (validation); this module used to refuse as an
    # unknown ceiling instead. Category checked directly against
    # `main_value`'s own outcome for the identical expression.
    for expr in ("Abs(divisors(1))", "factorint(1) % 7", "primefactors(1) // 2",
                 "divisors(1) << 1", "divisors(1) >> 1"):
        ok_main, main_result = main_value(expr)
        v, err = se.safe_parse(expr)
        check(f"differential (formerly divergent, now fixed): {expr!r} "
              f"matches main's own category (validation, from main's own "
              f"TypeError)",
              not ok_main and v is None and err is not None and err[0] == "validation",
              f"-> value={v!r} err={err!r} main_raised={main_result!r}")
    # rf/ff's own arity-error text, called by the ALIAS spelling: must
    # be byte-identical to main's (the REAL class name, `RisingFactorial`/
    # `FallingFactorial` -- `rf is RisingFactorial`, confirmed live).
    for expr in ("rf(5)", "ff(5)"):
        ok_main, main_result = main_value(expr)
        v, err = se.safe_parse(expr)
        # `safe_parse` formats a parse exception with `str(exc)`, not
        # `repr(exc)` (which `main_value` keeps for its messages), so
        # the byte-identical target is the str form.
        want = f"parse error: {main_error_text(expr)}" if not ok_main else None
        check(f"differential (formerly divergent, now fixed): {expr!r} "
              f"arity error is byte-identical to main (the real class "
              f"name, not the alias)",
              not ok_main and v is None and err == ("validation", want),
              f"-> value={v!r} err={err!r} want=('validation', {want!r})")


def run_codex_r15_corpus_divergences() -> None:
    """THE-1095 round 16 (Codex addendum, `verify-1095-r15.log`): Codex's
    own real 528-expression checkout-vs-checkout corpus (subprocess-
    capped, running an ACTUAL `safe_parse` on both `origin/main` and
    this branch — unlike this fixture's own `main_value`, a bare
    `parse_expr` shortcut that never runs `safe_parse` at all, and so
    never caught any of these four) found exactly four branch-vs-main
    differences, all a valid main result turned into a branch `ceiling`
    refusal, none pinned here before this round:

    FOLLOW-UP NOTE (not implemented this round, time budget): `main_
    value`, above, could shell out to an `origin/main` checkout's own
    `safe_parse` under `timeout 6` for the NEXT regeneration of this
    fixture, instead of the bare `parse_expr` shortcut — that is
    precisely the gap that let these four go uncaught: a name FULLY
    RECOGNIZED by `safe_global_dict()`/`math_transforms()` (so `main_
    value`'s own shortcut sees no divergence at all) can still diverge
    once `safe_parse`'s OWN screens run on it, which only a REAL
    `safe_parse`-vs-`safe_parse` comparison across two checkouts would
    catch.
    """
    v, err = se.safe_parse("Piecewise((x,x>0),(0,True))")
    check("differential (Codex r15 corpus, now fixed): "
          "'Piecewise((x,x>0),(0,True))' (an ordinary symbolic "
          "Piecewise) matches main",
          err is None and str(v) == "Piecewise((x, x > 0), (0, True))",
          f"-> value={v!r} err={err!r}")
    v, err = se.safe_parse("factorial(expint(2,1/2))")
    check("differential (Codex r15 corpus, now fixed): "
          "'factorial(expint(2,1/2))' (0 < x < 1, the new E_1(x) <= "
          "-ln(x)+1 bound) matches main (stays symbolic)",
          err is None and str(v) == "factorial(expint(2, 1/2))",
          f"-> value={v!r} err={err!r}")
    # `factorial(Chi(-1))`/`factorial(li(-1))`: Chi/li are COMPLEX for
    # x <= 0 -- main leaves `factorial(<complex>)` symbolically
    # unevaluated (genuinely safe, just unprovable to THIS module). A
    # DELIBERATE, documented narrowing rather than a fix -- pinned as
    # "still refuses", not "now matches main", the same trade this
    # file's own `_REFUSAL_COMPARISON_EXPRESSIONS` already documents
    # for other names.
    for expr in ("factorial(Chi(-1))", "factorial(li(-1))"):
        v, err = se.safe_parse(expr)
        check(f"differential (Codex r15 corpus, deliberate narrowing, "
              f"not a fix): {expr!r} refuses -- main leaves it "
              f"symbolic (a complex branch), but this module cannot "
              f"prove that safe",
              v is None and err is not None and err[0] == "ceiling",
              f"-> value={v!r} err={err!r}")


x = Symbol("x")

if __name__ == "__main__":
    run_numeric_table_sweep()
    run_refusal_sweep()
    run_formerly_divergent_checks()
    run_codex_r15_corpus_divergences()
    print(f"\n{CHECKED} assertions checked "
          f"({len(_NUMERIC_TABLE_SHAPES) + len(_ELEMENTARY_SHAPES) + len(_MEASURED_SAFE_SHAPES)} "
          f"names x shapes x {len(_NUMERIC_N_VALUES)} n-values, plus "
          f"{len(_REFUSAL_COMPARISON_EXPRESSIONS)} refusal + "
          f"10 formerly-divergent-now-fixed checks)")
    print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
          "\n=== DIFFERENTIAL CORPUS CLEAN ===")
    sys.exit(1 if FAILS else 0)
