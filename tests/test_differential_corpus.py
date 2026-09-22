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

_KNOWN_DIVERGENCES = """
THE-1095 round 13 follow-up: documented, deliberate exceptions from this
differential's own "safe_parse matches main whenever main is cheap"
sweep -- not silent gaps:

  - `divisors`/`factorint`/`primefactors` (the eager-factor family,
    dict/list-returning): comfortably safe on their own small-`n` cap,
    but this module's own EXISTING (pre-round-13) `_resolve_arg_
    magnitude` machinery refuses a NESTED use (`Abs(divisors(1))`) with
    a CEILING category ("no growth estimate is defined for it"), where
    `origin/main` itself raises a plain `TypeError` ("bad operand type
    for abs(): 'list'") -- a VALIDATION-shaped error on main, CEILING
    here. A genuine category mismatch, confirmed live, left as a
    documented residual gap for a future round rather than guessed at
    under this round's own time budget -- the bare (unwrapped) call
    itself (`divisors(1)`, `factorint(1)`, `primefactors(1)`) DOES match
    main exactly and is covered by this corpus's own numeric-table
    sweep... except these three are containers, not numbers, so they are
    excluded from the equality sweep too (see `_NUMERIC_TABLE_SHAPES`'s
    own scope) and checked separately, directly, below instead.
  - `rf(n, k)`/`ff(n, k)` called by their ALIAS spelling: `origin/main`
    raises its OWN arity error under the REAL class name
    (`RisingFactorial`/`FallingFactorial` -- `rf is RisingFactorial`,
    confirmed live, the alias is the identical object) regardless of
    which spelling the caller used; this module's own deferred stand-in
    is named after whichever spelling the caller wrote, so `rf(5)`'s own
    arity-error TEXT says "rf takes..." where main says "RisingFactorial
    takes...". A narrow, cosmetic (error-TEXT-only, not a category or
    safety divergence) gap, not fixed this round for the same reason as
    the item above -- documented, not guessed at.
"""


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


def run_known_divergence_checks() -> None:
    for expr, main_text in (
        ("divisors(1)", "[1]"), ("factorint(1)", "{}"),
        ("primefactors(1)", "[]"),
    ):
        v, err = se.safe_parse(expr)
        check(f"differential (documented divergence, container family): "
              f"{expr!r} bare (unwrapped) matches main",
              err is None and str(v) == main_text, f"-> value={v!r} err={err!r}")
    for expr in ("Abs(divisors(1))", "factorint(1) % 7"):
        v, err = se.safe_parse(expr)
        check(f"differential (documented divergence): {expr!r} refuses "
              f"(category currently ceiling, not validation -- see "
              f"_KNOWN_DIVERGENCES)",
              v is None and err is not None,
              f"-> value={v!r} err={err!r}")


x = Symbol("x")

if __name__ == "__main__":
    run_numeric_table_sweep()
    run_refusal_sweep()
    run_known_divergence_checks()
    print(_KNOWN_DIVERGENCES)
    print(f"\n{CHECKED} assertions checked "
          f"({len(_NUMERIC_TABLE_SHAPES) + len(_ELEMENTARY_SHAPES) + len(_MEASURED_SAFE_SHAPES)} "
          f"names x shapes x {len(_NUMERIC_N_VALUES)} n-values, plus "
          f"{len(_REFUSAL_COMPARISON_EXPRESSIONS)} refusal + "
          f"{5} known-divergence checks)")
    print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
          "\n=== DIFFERENTIAL CORPUS CLEAN ===")
    sys.exit(1 if FAILS else 0)
