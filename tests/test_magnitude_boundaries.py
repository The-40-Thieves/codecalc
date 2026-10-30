"""Boundary and exact-value checks for the five top mutation-survivor guards in
codecalc/safe_expr.py: `_pole_sensitive_magnitude`, `reject_explosive`,
`_summation_product_violation`, `_resolve_marker_magnitude` and
`_resolve_arg_magnitude`.

The mutation lane (mutmut over safe_expr.py) scored these functions poorly
because no test pinned their thresholds or their returned magnitudes: a `>=`
flipped to `>`, a constant moved by one, or an `and`/`or` swapped survived
every check. So each function is driven DIRECTLY, on the same deferred
inert-stand-in tree `safe_parse` builds, with one input exactly AT each
threshold and one just past it, the four returned slots asserted exactly, and
the refusal reason pinned by a stable substring. Magnitudes are checked against
values computed here from the docstring formulas, never copied from the code.
"""

from __future__ import annotations

import math
import pathlib
import random
import sys
import time
from fractions import Fraction

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import logic
from codecalc import safe_expr as se

FAILS = []
COUNT = [0]


def check(name, cond, detail=""):
    COUNT[0] += 1
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def tree(expr):
    """The deferred inert-stand-in tree: the same call `safe_parse` makes at its
    step 2, so these checks see the tree the guards really receive."""
    return se._parse_deferred(
        expr, local_dict=None, global_dict=se._deferred_global_dict(), transformations=se.math_transforms()
    )


def close(a, b):
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


LOG10E = math.log10(math.e)
LOG10_2 = math.log10(2)
EULER_GAMMA = 0.5772156649015329
MAX_HEAVY = se.MAX_HEAVY_ARG
MAX_DIGITS = se.MAX_NUMERIC_DIGITS
BELL_RANGE_CAP = se._SUMMATION_SUMMAND_TABLE_NAME_RANGE_CAP["bell"]


def pole(expr):
    return se._pole_sensitive_magnitude(tree(expr), {})


def resolve(expr):
    return se._resolve_arg_magnitude(tree(expr), {})


def marker(expr):
    return se._resolve_marker_magnitude(tree(expr), {})


def summation(expr):
    return se._summation_product_violation(tree(expr), {})


def explode(expr):
    return se.reject_explosive(tree(expr))


def explode_tree(node):
    return se.reject_explosive(node)


def value_is(got, log_num, log_den, resolved):
    """All four slots of a magnitude tuple, the violation slot being None."""
    return (
        got is not None and close(got[0], log_num) and close(got[1], log_den) and got[2] is resolved and got[3] is None
    )


def refused(got, substring):
    """A refusal: not resolved, a reason that contains `substring`."""
    return got is not None and got[2] is False and got[3] is not None and substring in got[3]


def says(text, substring):
    """A reason string (reject_explosive / summation) that contains `substring`."""
    return text is not None and substring in text


def resolved_ok(got):
    """Resolved with no reason; the magnitude itself is not pinned."""
    return got is not None and got[2] is True and got[3] is None


def unresolved_quiet(got):
    """Not resolved and no reason: the caller decides, nothing is refused here."""
    return got is not None and got[2] is False and got[3] is None


def refused_exact(got, reason):
    """A refusal with the COMPLETE reason text, and both magnitude slots zero: every
    refusal the guards return is `(0.0, 0.0, False, reason)`, so a slot-swapped or
    None-valued mutant fails here where `refused()` (substring, slots 2-3 only) passes."""
    return got is not None and got[0] == 0.0 and got[1] == 0.0 and got[2] is False and got[3] == reason


def unresolved_zero(got):
    """Not resolved, no reason, both magnitude slots zero (the symbolic-argument return)."""
    return got is not None and got[0] == 0.0 and got[1] == 0.0 and got[2] is False and got[3] is None


def about(got, log_num, tol):
    """Resolved, no reason, log10 numerator within `tol` of `log_num` (for values the
    module derives through a Stirling envelope, whose correction term is not exact)."""
    return got is not None and abs(got[0] - log_num) <= tol and got[1] == 0.0 and got[2] is True and got[3] is None


def gen_reason(name):
    """The `_unresolved_or_violation` / polygamma / zeta reason for a bare `name()` call."""
    return (
        f"an argument to {name}() cannot be safely bounded: computing it would take an "
        "unbounded amount of time and memory"
    )


UNBOUNDED = "computing it would take an unbounded amount of time and memory"


def deep_add(depth):
    """A right-nested `Add` chain of `depth` levels, built without evaluation."""
    from sympy import Add, Integer

    node = Integer(1)
    for _ in range(depth):
        node = Add(node, Integer(1), evaluate=False)
    return node


# ---------------------------------------------------------------- _pole_sensitive_magnitude

# not-mine path: a node of another name is not this function's business.
check("pole: factorial(3) is not a pole-sensitive name -> None", pole("factorial(3)") is None)
check("pole: sin(2) IS pole-sensitive -> a tuple", pole("sin(2)") is not None)

# exp(x): x >= 0 scales by log10(e); the cap is MAX_HEAVY_ARG, inclusive.
check("exp(2) = 2*log10(e)", value_is(pole("exp(2)"), 2 * LOG10E, 0.0, True))
check(
    "exp(MAX_HEAVY_ARG) accepted at the cap (exact value)",
    value_is(pole(f"exp({MAX_HEAVY})"), MAX_HEAVY * LOG10E, 0.0, True),
)
check(
    "exp(MAX_HEAVY_ARG + 1) refused just past the cap",
    refused(pole(f"exp({MAX_HEAVY + 1})"), f"exceeds the limit of {MAX_HEAVY}"),
)
check("exp(-10) is small: 0.0, resolved", value_is(pole("exp(-10)"), 0.0, 0.0, True))
check("exp(-1464) (negative, past the cap) still safe", value_is(pole(f"exp(-{MAX_HEAVY + 1})"), 0.0, 0.0, True))
check("exp(0) = 0.0 exactly at the x >= 0 boundary", value_is(pole("exp(0)"), 0.0, 0.0, True))

# sin/cos/tanh/erf: |value| <= 1 for any real argument.
check("sin(2) -> 0.0", value_is(pole("sin(2)"), 0.0, 0.0, True))
check("cos(3) -> 0.0", value_is(pole("cos(3)"), 0.0, 0.0, True))
check("tanh(2) -> 0.0", value_is(pole("tanh(2)"), 0.0, 0.0, True))
check("erf(1/2) -> 0.0", value_is(pole("erf(1/2)"), 0.0, 0.0, True))
check("non-real tanh(I) refused", refused(pole("tanh(I)"), "tanh()"))
check("non-real erf(I) refused", refused(pole("erf(I)"), "erf()"))
check("non-real sin(I) refused", refused(pole("sin(I)"), "sin()"))
check("sin(x) with a free symbol is left unresolved, no reason", unresolved_quiet(pole("sin(x)")))

# zeta(s): s == 1 is the pole (unresolved, no reason); s >= 2 -> log10(2).
check("zeta(2) = log10(2) at s >= 2 (lower edge)", value_is(pole("zeta(2)"), LOG10_2, 0.0, True))
check("zeta(3) = log10(2) (any s >= 2)", value_is(pole("zeta(3)"), LOG10_2, 0.0, True))
check("zeta(199/100) refused: non-integer just below 2", refused(pole("zeta(199/100)"), "zeta()"))
check("zeta(1) is the pole: unresolved, no reason", unresolved_quiet(pole("zeta(1)")))
check("zeta(0) = 1.0 (|s| + 1 < 2 branch)", value_is(pole("zeta(0)"), 1.0, 0.0, True))
check(
    "zeta(-1) resolved, magnitude clamped >= 0",
    (lambda g: g is not None and g[2] is True and g[0] >= 0.0 and g[3] is None)(pole("zeta(-1)")),
)
check("zeta(-1/2) refused: non-integer below 0", refused(pole("zeta(-1/2)"), "zeta()"))

# two-argument zeta(s, a): integer s >= 2 with a >= 1 is safe; a just below 1 is not.
check("zeta(2, 1) = log10(2) at a = 1 (lower edge)", value_is(pole("zeta(2,1)"), LOG10_2, 0.0, True))
check("zeta(2, 1/2) refused just below a = 1", refused(pole("zeta(2,1/2)"), "zeta()"))
check("zeta(2, 1/1000) refused", refused(pole("zeta(2, 1/1000)"), "zeta()"))
check("zeta(1, 1) is the pole: unresolved, no reason", unresolved_quiet(pole("zeta(1,1)")))
check("zeta(-1, 2) refused: two-arg with s < 2", refused(pole("zeta(-1,2)"), "zeta()"))

# gamma / loggamma / digamma: z >= 1 is the safe side.
check("gamma(1) resolved at the z = 1 lower edge", value_is(pole("gamma(1)"), 0.0, 0.0, True))
_gammas = [pole(f"gamma({z})") for z in (1, 2, 3)]
check(
    "gamma is strictly increasing above the edge",
    all(resolved_ok(g) for g in _gammas) and _gammas[2][0] > _gammas[1][0] > _gammas[0][0],
)
check("gamma(999/1000) refused just below z = 1", refused(pole("gamma(999/1000)"), "gamma()"))
check("gamma(1/2) refused below z = 1", refused(pole("gamma(1/2)"), "gamma()"))
check("digamma(1) resolved", resolved_ok(pole("digamma(1)")))
check("digamma(1/2) refused below z = 1", refused(pole("digamma(1/2)"), "digamma()"))

# polygamma(n, z): n is 0 or a positive integer; z >= 1.
check("polygamma(0, 1) resolved at the edge", resolved_ok(pole("polygamma(0,1)")))
check("polygamma(1, 1) resolved at the edge", resolved_ok(pole("polygamma(1,1)")))
check("polygamma(1, 1/2) refused: z below 1", refused(pole("polygamma(1,1/2)"), "polygamma()"))
check("polygamma(1/2, 2) refused: non-integer order", refused(pole("polygamma(1/2,2)"), "polygamma()"))

# expint(n, x): n a positive integer; x > 0; x >= 1 bounded by 0.0.
check("expint(1, 1) = 0.0 at x = 1 (x >= 1 lower edge)", value_is(pole("expint(1,1)"), 0.0, 0.0, True))
check("expint(1, 2) = 0.0 above the edge", value_is(pole("expint(1,2)"), 0.0, 0.0, True))
check("expint(1, 1/2) = log10(1 - ln(1/2))", value_is(pole("expint(1,1/2)"), math.log10(1 - math.log(0.5)), 0.0, True))
check("expint(1, 0) refused at x = 0 (x > 0 edge)", refused(pole("expint(1,0)"), "second argument to expint()"))
check("expint(1, -1) refused", refused(pole("expint(1,-1)"), "second argument to expint()"))
check(
    "expint(0, 2) refused: n = 0 is not a positive integer", refused(pole("expint(0,2)"), "first argument to expint()")
)
check("expint(1/2, 2) refused: non-integer n", refused(pole("expint(1/2,2)"), "first argument to expint()"))

# Ci(x): x <= 0 refused; x > 1 is log10(2); x <= 1 uses gamma + |ln x| + 1.
check("Ci(2) = log10(2) (x > 1)", value_is(pole("Ci(2)"), LOG10_2, 0.0, True))
check("Ci(1001/1000) = log10(2) just above 1", value_is(pole("Ci(1001/1000)"), LOG10_2, 0.0, True))
check(
    "Ci(1) uses the x <= 1 branch: log10(gamma + 1)", value_is(pole("Ci(1)"), math.log10(EULER_GAMMA + 1.0), 0.0, True)
)
check(
    "Ci(1/1000) = log10(gamma + |ln x| + 1)",
    value_is(pole("Ci(1/1000)"), math.log10(EULER_GAMMA + abs(math.log(1 / 1000)) + 1.0), 0.0, True),
)
check("Ci(0) refused at the x > 0 edge", refused(pole("Ci(0)"), "Ci()"))

# LambertW: [-1/e, inf) is safe; below -1/e refused.
check("LambertW(0) resolved at 0", value_is(pole("LambertW(0)"), 0.0, 0.0, True))
check("LambertW(-1/3) resolved (inside [-1/e, 0))", value_is(pole("LambertW(-1/3)"), 0.0, 0.0, True))
check("LambertW(-1/2) refused (below -1/e)", refused(pole("LambertW(-1/2)"), "below -1/e"))
check("LambertW(-1) refused", refused(pole("LambertW(-1)"), "below -1/e"))

# exp-bounded specials: |x| (or x*x for erfi) against MAX_HEAVY_ARG, inclusive.
check("Ei(MAX_HEAVY_ARG) accepted (exact)", value_is(pole(f"Ei({MAX_HEAVY})"), MAX_HEAVY * LOG10E, 0.0, True))
check("Ei(-MAX_HEAVY_ARG) accepted: uses |x|", value_is(pole(f"Ei(-{MAX_HEAVY})"), MAX_HEAVY * LOG10E, 0.0, True))
check(
    "Ei(MAX_HEAVY_ARG + 1) refused just past the cap",
    refused(pole(f"Ei({MAX_HEAVY + 1})"), f"exceeds the limit of {MAX_HEAVY}"),
)
check("Ei(0) refused at its own pole", refused(pole("Ei(0)"), "own pole"))
check("Li(1) refused at its own pole", refused(pole("Li(1)"), "own pole"))
check("erfi(38) accepted: 38*38 = 1444 <= cap (exact)", value_is(pole("erfi(38)"), 38 * 38 * LOG10E, 0.0, True))
check("erfi(39) refused: 39*39 = 1521 > cap", refused(pole("erfi(39)"), "erfi()"))

# airy: |x| ** 1.5 against the cap.
check("airyai(128) accepted: 128**1.5 <= cap (exact)", value_is(pole("airyai(128)"), (128**1.5) * LOG10E, 0.0, True))
check("airyai(129) refused: 129**1.5 > cap", refused(pole("airyai(129)"), "airyai()"))

# ---------------------------------------------------------------- _resolve_arg_magnitude

# Literal and Rational slots.
check("resolve 10**5 -> (5, 0, True, None)", value_is(resolve("10**5"), 5.0, 0.0, True))
check("resolve 1/3 -> (0, log10 3, True, None)", value_is(resolve("1/3"), 0.0, math.log10(3), True))
check("resolve 2**(-2) -> (0, 2*log10 2, True, None)", value_is(resolve("2**(-2)"), 0.0, 2 * LOG10_2, True))
check("resolve -1 -> sign ignored: (0, 0, True, None)", value_is(resolve("-1"), 0.0, 0.0, True))
check("resolve pi -> (1, 0, True, None): bounded by 10**1", value_is(resolve("pi"), 1.0, 0.0, True))

# Mul = sum of factors' magnitudes; Add = largest + log10(number of terms).
check("resolve 2*3 = log10 2 + log10 3 (Mul is a sum)", value_is(resolve("2*3"), LOG10_2 + math.log10(3), 0.0, True))
check(
    "resolve 2+3 = log10 3 + log10 2 (Add: largest + log10 n)",
    value_is(resolve("2+3"), math.log10(3) + LOG10_2, 0.0, True),
)
check(
    "resolve 2+2*3 = log10 6 + log10 2 (Add over a Mul term)",
    value_is(resolve("2+2*3"), math.log10(6) + LOG10_2, 0.0, True),
)
# A sum whose terms are not all rational reaches the resolver's own Add branch:
# the largest term plus log10(number of terms), with pi counting as 10**1.
check(
    "resolve pi + 2 = 1 + log10 2 (Add branch: largest term + log10 n)",
    value_is(resolve("pi + 2"), 1.0 + LOG10_2, 0.0, True),
)
check(
    "resolve pi + 10**5 = 5 + log10 2 (largest term dominates)",
    value_is(resolve("pi + 10**5"), 5.0 + LOG10_2, 0.0, True),
)
check("resolve x*2 is incomplete: resolved False, no reason", unresolved_quiet(resolve("x*2")))

# Pow with a resolved base and exponent: base magnitude * 10**exp.
check("resolve sqrt(2) = log10(2) / 2", value_is(resolve("sqrt(2)"), LOG10_2 / 2, 0.0, True))

# Abs keeps the magnitude; frac is always in [0, 1).
check("resolve Abs(-3) = log10 3", value_is(resolve("Abs(-3)"), math.log10(3), 0.0, True))
check("resolve frac(7/2) = 0.0 (frac is in [0, 1))", value_is(resolve("frac(7/2)"), 0.0, 0.0, True))

# A violation from a nested node is propagated, not swallowed.
check("resolve 2**20000 % x propagates the marker's violation", refused(resolve("2**20000 % x"), "too large to reduce"))

# ---------------------------------------------------------------- _resolve_marker_magnitude

# << : left magnitude + right * log10(2) (shifting by count doubles per bit).
check("marker 1 << 2 = 2*log10 2", value_is(marker("1 << 2"), 2 * LOG10_2, 0.0, True))
check("marker 1 << 200 = 200*log10 2 (exact)", value_is(marker("1 << 200"), 200 * LOG10_2, 0.0, True))
check("marker 1 << 2 << 3 chains: log10 32", value_is(marker("1 << 2 << 3"), math.log10(32), 0.0, True))
check("marker 1 << x: unknown count -> unresolved, no reason", unresolved_quiet(marker("1 << x")))
check("marker x << 1: unknown left -> unresolved, no reason", unresolved_quiet(marker("x << 1")))
check(
    "marker 1 << 10**5000: count overflows -> inf magnitude, resolved",
    (lambda g: g is not None and math.isinf(g[0]) and g[2] is True and g[3] is None)(marker("1 << 10**5000")),
)

# >> : bounded by the LEFT operand only.
check("marker 1 >> 3 = 0.0 (bounded by left 1)", value_is(marker("1 >> 3"), 0.0, 0.0, True))
check("marker 2**1000 >> 3 = log10(2**1000)", value_is(marker("2**1000 >> 3"), 1000 * LOG10_2, 0.0, True))
check("marker 1 >> x still bounded by left: 0.0", value_is(marker("1 >> x"), 0.0, 0.0, True))

# % : bounded by |divisor|.
check("marker -1 % 10**4 bounded by 10**4 (exact log10 = 4)", value_is(marker("-1 % 10**4"), 4.0, 0.0, True))
check("marker 5 % 3 bounded by 3", value_is(marker("5 % 3"), math.log10(3), 0.0, True))
check(
    "marker 2**20000 % 7 bounded by 7 (huge left, safe divisor)",
    value_is(marker("2**20000 % 7"), math.log10(7), 0.0, True),
)
check("marker 3 % x: divisor unknown -> unresolved, no reason", unresolved_quiet(marker("3 % x")))
check(
    "marker 2**20000 % x: huge left with unknown divisor refused",
    refused(marker("2**20000 % x"), "'%' is too large to reduce"),
)

# // : left magnitude - log10(divisor lower bound).
check(
    "marker 1 // 0.0005 = log10(1/0.0005) (float divisor)",
    value_is(marker("1 // 0.0005"), -math.log10(0.0005), 0.0, True),
)
check("marker 7 // 2 = log10 7 - log10 2", value_is(marker("7 // 2"), math.log10(7) - LOG10_2, 0.0, True))
check(
    "marker 2**20000 // x refused: huge left, unknown divisor",
    refused(marker("2**20000 // x"), "'//' is too large to reduce"),
)

# ---------------------------------------------------------------- reject_explosive

check("reject: 2 + 3 accepted", explode("2 + 3") is None)
check("reject: 1 << 2 << 3 accepted", explode("1 << 2 << 3") is None)
check("reject: -1 % 10**4 accepted", explode("-1 % 10**4") is None)
check("reject: 1 // 0.0005 accepted", explode("1 // 0.0005") is None)
check("reject: 2**1463 accepted", explode("2**1463") is None)
check("reject: x**2 accepted", explode("x**2") is None)

# Integer power: the NUMERATOR digit count is inclusive at MAX_NUMERIC_DIGITS.
check("reject: 10**(MAX_DIGITS-1) has exactly MAX_DIGITS digits -> accepted", explode(f"10**{MAX_DIGITS - 1}") is None)
check(
    "reject: 10**MAX_DIGITS has MAX_DIGITS+1 digits -> refused",
    says(explode(f"10**{MAX_DIGITS}"), f"about {MAX_DIGITS + 1} digits in its numerator"),
)
check("reject: 2**13287 (3999.9 log10) accepted", explode("2**13287") is None)
check(
    "reject: 2**13288 (4000.2 log10, 4001 digits) refused",
    says(explode("2**13288"), f"about {MAX_DIGITS + 1} digits in its numerator"),
)
check("reject: 1/2**13287 denominator at MAX_DIGITS digits accepted", explode("1/2**13287") is None)
check(
    "reject: 1/2**13288 denominator at MAX_DIGITS+1 digits refused", says(explode("1/2**13288"), "in its denominator")
)
check("reject: 2**20000 >> 1 refused on the shift result", says(explode("2**20000 >> 1"), "'>>'"))
check("reject: 1 >> 2**20000 refused on the numerator", says(explode("1 >> 2**20000"), "numerator"))
check("reject: 1 << 10**5000 refused: unbounded digits", says(explode("1 << 10**5000"), "unbounded number of digits"))
check("reject: 2**20000 % 7 refused on the 6021-digit left", says(explode("2**20000 % 7"), "digits"))

# Roots: the BASE digit count is inclusive at MAX_ROOT_ARG_DIGITS.
check("reject: sqrt(10**(ROOT-1)) accepted at the root cap", explode(f"sqrt(10**{se.MAX_ROOT_ARG_DIGITS - 1})") is None)
check(
    "reject: sqrt(10**ROOT) refused just past the root cap",
    says(explode(f"sqrt(10**{se.MAX_ROOT_ARG_DIGITS})"), f"more digits than the limit of {se.MAX_ROOT_ARG_DIGITS}"),
)

# Symbolic exponent: exponent magnitude is compared to MAX_SYMBOLIC_EXPONENT.
check(
    "reject: (x+1)**MAX_SYMBOLIC_EXPONENT accepted at the limit", explode(f"(x+1)**{se.MAX_SYMBOLIC_EXPONENT}") is None
)
check(
    "reject: (x+1)**(MAX_SYMBOLIC_EXPONENT+1) refused just past the limit",
    says(explode(f"(x+1)**{se.MAX_SYMBOLIC_EXPONENT + 1}"), f"exceeds the limit of {se.MAX_SYMBOLIC_EXPONENT}"),
)

# Power tower: an exponent that is itself a power is always refused.
check("reject: x**(x**2) power tower refused", says(explode("x**(x**2)"), "power tower"))


# ---------------------------------------------------------------- _summation_product_violation

check("summation: factorial(3) is not a summation/product -> None", summation("factorial(3)") is None)
check("summation: a bare-symbol limit is left alone -> None", summation("summation(x, x)") is None)

# Digit cap on each limit bound: 10**(DIGITS-1) has DIGITS digits, inclusive.
check(
    "summation: upper limit with MAX_DIGITS digits accepted",
    summation(f"summation(1, (x, 1, 10**{MAX_DIGITS - 1}))") is None,
)
check(
    "summation: upper limit with MAX_DIGITS+1 digits refused",
    says(
        summation(f"summation(1, (x, 1, 10**{MAX_DIGITS}))"),
        f"the upper limit of summation() exceeds the digit limit of {MAX_DIGITS}",
    ),
)
check(
    "summation: lower limit with MAX_DIGITS+1 digits refused",
    says(
        summation(f"summation(1, (x, 10**{MAX_DIGITS}, 10**{MAX_DIGITS}))"),
        f"the lower limit of summation() exceeds the digit limit of {MAX_DIGITS}",
    ),
)

# Table-bounded summand: the range length is capped per name, inclusive.
check(
    "summation: bell(x) over exactly BELL_RANGE_CAP terms accepted",
    summation(f"summation(bell(x), (x, 1, {BELL_RANGE_CAP}))") is None,
)
check(
    "summation: bell(x) over BELL_RANGE_CAP+1 terms refused on the range",
    says(summation(f"summation(bell(x), (x, 1, {BELL_RANGE_CAP + 1}))"), f"{BELL_RANGE_CAP + 1} total terms"),
)
check(
    "summation: the bell range refusal names the table-function reason",
    says(
        summation(f"summation(bell(x), (x, 1, {BELL_RANGE_CAP + 1}))"), "a summand containing a bounded table function"
    ),
)

# Boundary term: the summand at the extreme limit goes through the ordinary cap.
check(
    "summation: factorial(x) up to MAX_HEAVY_ARG accepted",
    summation(f"summation(factorial(x), (x, 1, {MAX_HEAVY}))") is None,
)
check(
    "summation: factorial(x) to MAX_HEAVY_ARG+1 refused on its boundary term",
    says(summation(f"summation(factorial(x), (x, 1, {MAX_HEAVY + 1}))"), "boundary term of summation()"),
)

# Polynomial summands have a closed form: no range cap.
check(
    "summation: x**2 over 10**6 terms accepted (no table name, no cap)",
    summation("summation(x**2, (x, 1, 10**6))") is None,
)

# product(): an identity growth cap applies even to a constant summand.
check(
    "product: product(1, (x, 1, MAX_HEAVY)) accepted at the cap", summation(f"product(1, (x, 1, {MAX_HEAVY}))") is None
)
check(
    "product: product(1, (x, 1, MAX_HEAVY+1)) refused just past the cap",
    says(summation(f"product(1, (x, 1, {MAX_HEAVY + 1}))"), "product()'s own identity growth"),
)

# product() digit cap: total_terms * worst term log10 against MAX_DIGITS.
check(
    "product: product(10**4, (x, 1, MAX_HEAVY)) refused: 4*MAX_HEAVY digits",
    says(summation(f"product(10**4, (x, 1, {MAX_HEAVY}))"), f"would have about {4 * MAX_HEAVY} digits"),
)
check(
    "product: product(x, (x, 1, 10**6)) refused: ~6e6 digits",
    says(summation("product(x, (x, 1, 10**6))"), "would have about 6000000 digits"),
)

# ---------------------------------------------------------------- exponent cancellation

# `_factor_multiset` keys bases by VALUE, so `10**5/2**5` is `{10: 5, 2: -5}`:
# a negative entry whose value is the integer 3125. The symbolic-power cap must
# judge integrality on the coprime refinement, not on the raw negative entry.
check(
    "exponent cancel: (x+1)**(10**5/2**5) refused, 3125 > 200 (PR #363 report)",
    says(explode("(x+1)**(10**5/2**5)"), f"exceeds the limit of {se.MAX_SYMBOLIC_EXPONENT}"),
)
check(
    "exponent cancel: (x+1)**(6**300/3**300) refused (= 2**300; the print-digit guard fires first)",
    explode("(x+1)**(6**300/3**300)") is not None,
)
# Same shape at a size the print-digit guard does not reach first: the cap is the reason.
check(
    "exponent cancel: (x+1)**(6**8/3**8) refused by the cap (= 2**8 = 256)",
    says(explode("(x+1)**(6**8/3**8)"), f"exceeds the limit of {se.MAX_SYMBOLIC_EXPONENT}"),
)
check("exponent cancel: (x+1)**(10**3/4) refused (= 250)", explode("(x+1)**(10**3/4)") is not None)
check("exponent cancel: (x+1)**(10**3/5) accepted at the cap (= 200)", explode("(x+1)**(10**3/5)") is None)
check("exponent cancel: (x+1)**(12**2/(2**4*3**2)) accepted (= 1)", explode("(x+1)**(12**2/(2**4*3**2))") is None)

# Genuinely fractional exponents stay out of the symbolic-power cap: a real
# denominator survives. (The first two are refused by the print-digit ceiling,
# "over the limit of 4000", a different guard; the cap must not be their reason.)
check(
    "exponent cancel: (x+1)**(10**5/3) not refused by the exponent cap (fractional)",
    not says(explode("(x+1)**(10**5/3)"), f"exceeds the limit of {se.MAX_SYMBOLIC_EXPONENT}"),
)
check(
    "exponent cancel: (x+1)**(999999999999/2) not refused by the exponent cap (fractional)",
    not says(explode("(x+1)**(999999999999/2)"), f"exceeds the limit of {se.MAX_SYMBOLIC_EXPONENT}"),
)
check("exponent cancel: (x+1)**(10**5/2**6) accepted (= 1562.5, fractional)", explode("(x+1)**(10**5/2**6)") is None)

# The helper itself: integrality is decided on the coprime refinement.
check(
    "coprime refine: {10: 5, 2: -5} -> {5: 5} (value 3125)",
    se._coprime_refine_multiset({10: 5, 2: -5}) == {5: 5},
)
check(
    "coprime refine: {6: 300, 3: -300} -> {2: 300} (value 2**300)",
    se._coprime_refine_multiset({6: 300, 3: -300}) == {2: 300},
)
check(
    "coprime refine: {12: 2, 2: -4, 3: -2} -> {} (value 1)",
    se._coprime_refine_multiset({12: 2, 2: -4, 3: -2}) == {},
)
check(
    "coprime refine: {10: 5, 3: -1} unchanged (coprime, real denominator)",
    se._coprime_refine_multiset({10: 5, 3: -1}) == {10: 5, 3: -1},
)

# ---------------------------------------------------------------- round 2: _pole_sensitive_magnitude

# Free-symbol and non-rational arguments: the unresolved-argument return and its reason.
check("pole2: gamma(x) with a free symbol: unresolved, both slots zero", unresolved_zero(pole("gamma(x)")))
for _name in ("gamma", "loggamma", "digamma"):
    check(
        f"pole2: {_name}(pi) refused with the exact unresolved-argument reason",
        refused_exact(pole(f"{_name}(pi)"), gen_reason(_name)),
    )
# Below the z = 1 edge: the exact reason, with the name spelled in the text.
for _name in ("gamma", "loggamma", "digamma"):
    check(
        f"pole2: {_name}(1/2) refused below z = 1 with the exact reason",
        refused_exact(pole(f"{_name}(1/2)"), gen_reason(_name)),
    )
# digamma's own formula, on the z > 1 side: log10(log(z) + 2).
check(
    "pole2: digamma(10) = log10(ln 10 + 2) (digamma's own formula)",
    value_is(pole("digamma(10)"), math.log10(math.log(10) + 2), 0.0, True),
)

# polygamma: the order and z gates, each refusal exact.
check(
    "pole2: polygamma(pi, 2) refused exactly (non-rational order)",
    refused_exact(pole("polygamma(pi,2)"), gen_reason("polygamma")),
)
check(
    "pole2: polygamma(-1, 2) refused exactly (negative order)",
    refused_exact(pole("polygamma(-1,2)"), gen_reason("polygamma")),
)
check(
    "pole2: polygamma(1/2, 2) refused exactly (non-integer order)",
    refused_exact(pole("polygamma(1/2,2)"), gen_reason("polygamma")),
)
check(
    "pole2: polygamma(1, 1/2) refused exactly (z below 1)",
    refused_exact(pole("polygamma(1,1/2)"), gen_reason("polygamma")),
)
# A symbolic order is treated as absent: the digamma-term bound alone.
check(
    "pole2: polygamma(x, 2) with a symbolic order: digamma-term bound log10(ln 2 + 2)",
    value_is(pole("polygamma(x,2)"), math.log10(math.log(2) + 2), 0.0, True),
)
# order 2 at z = 2 or 3: the factorial envelope log10(2!) + log10(2) beats the digamma term.
check(
    "pole2: polygamma(2, 2) ~ log10(2!) + log10(2), Stirling-tolerant", about(pole("polygamma(2,2)"), 2 * LOG10_2, 1e-3)
)
check("pole2: polygamma(2, 3) ~ the same factorial envelope", about(pole("polygamma(2,3)"), 2 * LOG10_2, 1e-3))
# order 0 (digamma) at z just above 1 takes the exact-zero flag path: the digamma term alone.
check(
    "pole2: polygamma(0, 1001/1000) = digamma term log10(ln(1.001) + 2)",
    value_is(pole("polygamma(0,1001/1000)"), math.log10(math.log(1.001) + 2), 0.0, True),
)

# zeta: the pole, the non-integer and two-argument refusals, and the Bernoulli envelope.
check("pole2: zeta(1) unresolved, both slots zero", unresolved_zero(pole("zeta(1)")))
check("pole2: zeta(-1) = 0.0 (the Bernoulli envelope, clamped at 0)", value_is(pole("zeta(-1)"), 0.0, 0.0, True))
check(
    "pole2: zeta(-20) = Bernoulli envelope log10(21!) - 21*log10(2*pi) + log10(4)",
    about(pole("zeta(-20)"), math.lgamma(22) / math.log(10) - 21 * math.log10(2 * math.pi) + math.log10(4), 1e-6),
)
check("pole2: zeta(2, 1/2) refused exactly (a < 1)", refused_exact(pole("zeta(2,1/2)"), gen_reason("zeta")))
check(
    "pole2: zeta(199/100) refused exactly (non-integer below 2)",
    refused_exact(pole("zeta(199/100)"), gen_reason("zeta")),
)

# expint: the order and the second-argument refusals, exact; the x >= 1 edge.
check(
    "pole2: expint(1/2, 2) refused exactly (first argument outside a positive integer)",
    refused_exact(
        pole("expint(1/2,2)"),
        "the first argument to expint() cannot be safely bounded outside a positive integer n: " + UNBOUNDED,
    ),
)
check(
    "pole2: expint(1, 0) refused exactly (second argument outside x > 0)",
    refused_exact(
        pole("expint(1,0)"),
        "the second argument to expint() cannot be safely bounded outside x > 0: " + UNBOUNDED,
    ),
)
check(
    "pole2: expint(1, 3/2) = 0.0 (x >= 1 bound holds up to 2, not only at 1)",
    value_is(pole("expint(1,3/2)"), 0.0, 0.0, True),
)

# Ci: the non-positive refusal, exact.
check(
    "pole2: Ci(0) refused exactly (non-positive argument)",
    refused_exact(
        pole("Ci(0)"),
        "the argument to Ci() cannot be safely bounded for a non-positive value (a pole at 0, a complex "
        "branch below it): " + UNBOUNDED,
    ),
)

# LambertW: the -1/e edge is inclusive on the float side; the tiny-argument log is 0.
check(
    "pole2: LambertW(-1/2) refused exactly (below -1/e)",
    refused_exact(
        pole("LambertW(-1/2)"),
        "the argument to LambertW() cannot be safely bounded below -1/e: " + UNBOUNDED,
    ),
)
check(
    "pole2: LambertW(-f) with f the float -1/e: resolved (inside [-1/e, 0), not below)",
    resolved_ok(pole("LambertW(-36787944117144233/10**17)")),
)
check(
    "pole2: LambertW(1/2) = log10(ln 1.5) (the x > 0 log bound)",
    value_is(pole("LambertW(1/2)"), math.log10(math.log(1.5)), 0.0, True),
)
check(
    "pole2: LambertW(1/10**20): log(1 + x) is 0.0 in floats, log10 gives 0.0",
    value_is(pole("LambertW(1/10**20)"), 0.0, 0.0, True),
)

# Chi / li / Li: the non-positive refusal names the function, exact.
for _name in ("Chi", "li", "Li"):
    check(
        f"pole2: {_name}(0) refused exactly (non-positive)",
        refused_exact(
            pole(f"{_name}(0)"),
            f"the argument to {_name}() cannot be safely bounded for a non-positive value (a complex branch "
            "this module does not reason about): " + UNBOUNDED,
        ),
    )
check(
    "pole2: li(1) refused exactly at its own pole",
    refused_exact(pole("li(1)"), f"the argument to li() cannot be safely bounded at its own pole: {UNBOUNDED}"),
)
check(
    "pole2: Li(1) refused exactly at its own pole",
    refused_exact(pole("Li(1)"), f"the argument to Li() cannot be safely bounded at its own pole: {UNBOUNDED}"),
)
check(
    "pole2: Ei(0) refused exactly at its own pole",
    refused_exact(pole("Ei(0)"), f"the argument to Ei() cannot be safely bounded at its own pole: {UNBOUNDED}"),
)
check(
    "pole2: Chi(1) = log10(gamma + 1) (Chi's x <= 1 branch, at the edge)",
    value_is(pole("Chi(1)"), math.log10(EULER_GAMMA + 1.0), 0.0, True),
)
check(
    "pole2: Chi(1/1000) = log10(gamma + |ln x| + 1)",
    value_is(pole("Chi(1/1000)"), math.log10(EULER_GAMMA + abs(math.log(1 / 1000)) + 1.0), 0.0, True),
)
check(
    "pole2: Ei(1) = log10(gamma + e) (Ei's |x| <= 1 branch)",
    value_is(pole("Ei(1)"), math.log10(EULER_GAMMA + math.e), 0.0, True),
)
check(
    "pole2: Ei(1/2) = log10(gamma + |ln(1/2)| + e)",
    value_is(pole("Ei(1/2)"), math.log10(EULER_GAMMA + abs(math.log(0.5)) + math.e), 0.0, True),
)
check(
    "pole2: Ei(-1/2) uses |x|: log10(gamma + |ln(1/2)| + e)",
    value_is(pole("Ei(-1/2)"), math.log10(EULER_GAMMA + abs(math.log(0.5)) + math.e), 0.0, True),
)
check(
    "pole2: li(2) = log10(|ln|ln 2|| + e) (li's x <= e branch)",
    value_is(pole("li(2)"), math.log10(abs(math.log(abs(math.log(2)))) + math.e), 0.0, True),
)
check(
    "pole2: Li(2) = log10(|ln|ln 2|| + e) (Li shares li's branch)",
    value_is(pole("Li(2)"), math.log10(abs(math.log(abs(math.log(2)))) + math.e), 0.0, True),
)

# The exp-bounded family's cap: exact refusal text one past it, exact value at it.
check(
    "pole2: Ei(MAX_HEAVY_ARG+1) refused exactly",
    refused_exact(
        pole(f"Ei({MAX_HEAVY + 1})"),
        f"the argument to Ei() exceeds the limit of {MAX_HEAVY}: {UNBOUNDED}",
    ),
)
check(
    "pole2: exp(1/2) = 0.5 * log10(e) (exp's x >= 0 bound, above the 0 edge)",
    value_is(pole("exp(1/2)"), 0.5 * LOG10E, 0.0, True),
)
check(
    "pole2: exp(MAX_HEAVY_ARG+1) refused exactly",
    refused_exact(
        pole(f"exp({MAX_HEAVY + 1})"), f"the argument to exp() exceeds the limit of {MAX_HEAVY}: {UNBOUNDED}"
    ),
)
check(
    "pole2: airyai(x) with x**1.5 exactly the cap (a float witness) accepted at the cap",
    value_is(pole("airyai(2267162728564753/17592186044416)"), MAX_HEAVY * LOG10E, 0.0, True),
)
check(
    "pole2: airyai(129) refused exactly",
    refused_exact(pole("airyai(129)"), f"the argument to airyai() exceeds the limit of {MAX_HEAVY}: {UNBOUNDED}"),
)
check(
    "pole2: airybi(4) = 4**1.5 * log10(e) (airy names are all in the |x|**1.5 family)",
    value_is(pole("airybi(4)"), 8 * LOG10E, 0.0, True),
)
check("pole2: airyaiprime(4) = 4**1.5 * log10(e)", value_is(pole("airyaiprime(4)"), 8 * LOG10E, 0.0, True))
check(
    "pole2: Chi(2) = 2*log10(e) (Chi above its x <= 1 branch: the |x| bound)",
    value_is(pole("Chi(2)"), 2 * LOG10E, 0.0, True),
)
check(
    "pole2: Ei(2) = 2*log10(e) (Ei above its |x| <= 1 branch: the |x| bound)",
    value_is(pole("Ei(2)"), 2 * LOG10E, 0.0, True),
)
check("pole2: airybiprime(4) = 4**1.5 * log10(e)", value_is(pole("airybiprime(4)"), 8 * LOG10E, 0.0, True))
check("pole2: erfc(2) = log10(2) (the unbounded-table constant)", value_is(pole("erfc(2)"), LOG10_2, 0.0, True))
check("pole2: Si(2) = log10(2)", value_is(pole("Si(2)"), LOG10_2, 0.0, True))
check("pole2: fresnelc(1/2) = 0.0 (constant 1.0)", value_is(pole("fresnelc(1/2)"), 0.0, 0.0, True))

# ---------------------------------------------------------------- round 2: reject_explosive, exact reasons

# Recursion inside the scan or the walk: the exact reason, and a shallow tree that is fine.
check(
    "reject2: a 5000-deep Add chain: the RecursionError reason, exact",
    explode_tree(deep_add(5000)) == "expression too deeply nested to evaluate safely",
)
check("reject2: a 10-deep Add chain is accepted", explode_tree(deep_add(10)) is None)

# Callables with an argument cap, dispatched by name: exact reasons, one per dispatch.
check(
    "reject2: factorint(10**25): 26 digits over the 25-digit cap, exact",
    explode("factorint(10**25)") == "an argument to factorint() exceeds the limit of 25: " + UNBOUNDED,
)
check("reject2: factorint(10**24): 25 digits, accepted at the cap", explode("factorint(10**24)") is None)
check(
    "reject2: root(10**1201, 4): exact root-cap reason via the Function dispatch",
    explode("root(10**1201, 4)")
    == f"an argument to root() exceeds the limit of {se.MAX_ROOT_ARG_DIGITS}: " + UNBOUNDED,
)
check(
    "reject2: N(x, 10**4): precision one past MAX_NUMERIC_DIGITS, exact",
    explode("N(x, 10**4)") == f"the precision argument to N() exceeds the limit of {MAX_DIGITS}: " + UNBOUNDED,
)
check("reject2: N(x, 10**3) accepted", explode("N(x, 10**3)") is None)
check(
    "reject2: N(x, I): the unresolved precision reason, exact",
    explode("N(x, I)") == "the precision argument to N() cannot be safely bounded: " + UNBOUNDED,
)
check(
    "reject2: series(x, x, 0, 10**3): order past 100, exact",
    explode("series(x, x, 0, 10**3)") == "the order argument to series() exceeds the limit of 100: " + UNBOUNDED,
)
check("reject2: series(x, x, 0, 100) accepted at the cap", explode("series(x, x, 0, 100)") is None)
check(
    "reject2: series(x, x, 0, I): the unresolved order reason, exact",
    explode("series(x, x, 0, I)") == "the order argument to series() cannot be safely bounded: " + UNBOUNDED,
)
check(
    "reject2: fps(x, x, 0, 1, 1, 10**3): order past 100, exact",
    explode("fps(x, x, 0, 1, 1, 10**3)") == "the order argument to fps() exceeds the limit of 100: " + UNBOUNDED,
)
check(
    "reject2: fps(x, x, 0, 1, 1, I): the unresolved order reason, exact",
    explode("fps(x, x, 0, 1, 1, I)") == "the order argument to fps() cannot be safely bounded: " + UNBOUNDED,
)
check(
    "reject2: integrate upper limit 10**4000 (4001 digits): exact digit-limit reason",
    explode("integrate(x, (x, 0, 10**4000))")
    == f"the upper limit of integrate() exceeds the digit limit of {MAX_DIGITS}: it cannot be rendered as a decimal string",
)
check(
    "reject2: integrate lower limit 10**4000: exact digit-limit reason",
    explode("integrate(x, (x, 10**4000, 1))")
    == f"the lower limit of integrate() exceeds the digit limit of {MAX_DIGITS}: it cannot be rendered as a decimal string",
)
check(
    "reject2: integrate upper limit 10**3999 (4000 digits) accepted", explode("integrate(x, (x, 0, 10**3999))") is None
)
check(
    "reject2: integrate with an unbounded upper limit I: the unresolved-limit reason, exact",
    explode("integrate(x, (x, 0, I))") == "the upper limit of integrate() cannot be safely bounded: " + UNBOUNDED,
)
check(
    "reject2: integrate with a two-element limit: the unrecognized-shape reason, exact",
    explode("integrate(x, (x, 0))")
    == "a limit argument to integrate() has an unrecognized shape (not a plain variable-and-two-bounds form "
    "this module can verify as safe): " + UNBOUNDED,
)
check(
    "reject2: expint(1, 0): the second-argument reason, exact, through the top-level dispatch",
    explode("expint(1, 0)") == "the second argument to expint() cannot be safely bounded outside x > 0: " + UNBOUNDED,
)
check(
    "reject2: binomial(x, 10**1464): the second-argument cap reason, exact",
    explode("binomial(x, 10**1464)")
    == f"the second argument to binomial() exceeds the limit of {MAX_HEAVY} for a non-integer or negative first "
    "argument: " + UNBOUNDED,
)
check(
    "reject2: binomial(5, 10**1464) accepted: a confirmed non-negative integer first argument",
    explode("binomial(5, 10**1464)") is None,
)
check(
    "reject2: binomial(1/2, 10**1464): the growth reason for the result, exact",
    explode("binomial(1/2, 10**1464)")
    == "the result of binomial(...) would have an unbounded number of digits, over the limit of "
    f"{MAX_DIGITS}: it cannot be rendered as a decimal string",
)
check(
    "reject2: hyper(1, 2, 3): the unbounded-generic reason, exact",
    explode("hyper(1, 2, 3)")
    == "hyper() is not in the bounded function set: computing it with a numeric argument is not safely bounded",
)

# Integer powers: the walk's power-tower and symbolic-exponent branches, and the root branch.
check(
    "reject2: 1 + x**(x**2): the power-tower reason, exact, after a non-Pow parent",
    explode("1 + x**(x**2)")
    == "a power tower (an exponent that is itself a power) is not evaluated: the result grows faster than any useful bound",
)
check(
    "reject2: x**n + x**(x**2): the power tower after a symbolic-exponent power is still reached",
    explode("x**n + x**(x**2)")
    == "a power tower (an exponent that is itself a power) is not evaluated: the result grows faster than any useful bound",
)
check("reject2: 2**pi: a non-rational constant exponent is accepted, not a crash", explode("2**pi") is None)
check(
    "reject2: (1+10**1201)**Rational(1,4): the 4-th root base reason, exact",
    explode("(10**1201+1)**Rational(1,4)")
    == f"the base of a 4-th root has more digits than the limit of {se.MAX_ROOT_ARG_DIGITS}: computing the root is not safely bounded",
)
check(
    "reject2: (1+10**1201)**Rational(1,3): the cube root base reason, exact",
    explode("(10**1201+1)**Rational(1,3)")
    == f"the base of a cube root has more digits than the limit of {se.MAX_ROOT_ARG_DIGITS}: computing the root is not safely bounded",
)
check(
    "reject2: sqrt(1/10**1201): the denominator side of a square-root base, exact",
    explode("sqrt(1/10**1201)")
    == f"the base of a square root has more digits than the limit of {se.MAX_ROOT_ARG_DIGITS}: computing the root is not safely bounded",
)
check(
    "reject2: (1+10**1201)**Rational(3,2): a non-root rational exponent is not a root refusal",
    explode("(10**1201+1)**Rational(3,2)") is None,
)
# An integer base is counted in exact digits: 10**1200 - 1 has 1200 digits (its log10
# rounds to 1200.0, which a magnitude-only count would read as 1201) and is accepted.
check(
    "reject2: sqrt(10**1200 - 1) accepted: exactly 1200 digits, counted exactly",
    explode(f"sqrt({10**1200 - 1})") is None,
)
check(
    "reject2: sqrt(Rational(10**1201, 3)): the NUMERATOR side of a rational base, exact",
    explode(f"sqrt(Rational({10**1201}, 3))")
    == f"the base of a square root has more digits than the limit of {se.MAX_ROOT_ARG_DIGITS}: computing the root is not safely bounded",
)
check(
    "reject2: sqrt(Rational(1, 10**1201)): the DENOMINATOR side of a rational base, exact",
    explode(f"sqrt(Rational(1, {10**1201}))")
    == f"the base of a square root has more digits than the limit of {se.MAX_ROOT_ARG_DIGITS}: computing the root is not safely bounded",
)
check(
    "reject2: (10**2001+1)**2: the integer-power ceiling reason, exact",
    explode("(10**2001+1)**2")
    == "the result would have about 4003 digits in its numerator, over the limit of 4000: it cannot be rendered as a decimal string",
)
check("reject2: (10**1999+1)**2 accepted (3999 digits)", explode("(10**1999+1)**2") is None)
check(
    "reject2: (x+1)**201: the symbolic-power reason, exact",
    explode("(x+1)**201")
    == f"a symbolic power with an exponent of about 3 digits exceeds the limit of {se.MAX_SYMBOLIC_EXPONENT}: "
    "expanding it is superlinear in the exponent",
)
# Flipped 2026-10-29: `evaluate_expression` expands `(x+1)**(-201)` as expensively as
# `(x+1)**201` (both measured at the 10 s CPU limit), so the cap now bounds the magnitude
# for either sign. See the "negative exponent" block below for the boundary itself.
check(
    "reject2: (x+1)**(-201) refused: a negative exponent is expanded just the same",
    says(explode("(x+1)**(-201)"), f"exceeds the limit of {se.MAX_SYMBOLIC_EXPONENT}"),
)


# A benign node visited before the symbolic power must not end the walk: each of
# these takes a `continue` path in the walk, and the symbolic power after it is
# still refused. Trees are built directly so the argument order is the one tested.
def _add(*terms):
    from sympy import Add

    return Add(*terms, evaluate=False)


_SYMBOLIC_REASON = (
    f"a symbolic power with an exponent of about 3 digits exceeds the limit of {se.MAX_SYMBOLIC_EXPONENT}: "
    "expanding it is superlinear in the exponent"
)
_symbolic_power = tree("(x+1)**201")
_benign = {
    "x**n": "x**n",
    "2**pi": "2**pi",
    "(2+pi)**3": "(2+pi)**3",
    "2**3": "2**3",
    "2**1": "2**1",
    "(x+1)**(1/10**300)": "(x+1)**(1/10**300)",
    "(x+1)**(-1)": "(x+1)**(-1)",
    "(x+1)**(pi/2)": "(x+1)**(pi/2)",
}
# `(x+1)**(bell(3)*I)` used to sit in this benign list: an inconclusive exponent was skipped.
# Since 2026-10-29 an exponent that cannot be bounded and is not provably non-integer fails
# closed, so it is refused on its own before the walk reaches anything else.
check(
    "reject2: (x+1)**(bell(3)*I) refused on its own: an unbounded exponent is no longer skipped",
    says(explode("(x+1)**(bell(3)*I)"), "could not be bounded"),
)
for _label, _expr in _benign.items():
    check(
        f"reject2: {_label} before a symbolic power: the walk still reaches the power",
        explode_tree(_add(_symbolic_power, tree(_expr))) == _SYMBOLIC_REASON,
    )

# Exponent carrying a table function: the untrusted path. bell is a growing table
# function, so its positive power refuses and its negative power does not.
check(
    "reject2: (x+1)**bell(7) refused on the untrusted-multiset path",
    says(explode("(x+1)**bell(7)"), "a symbolic power with an exponent of about"),
)
# Both flipped 2026-10-29: the cap bounds the magnitude for either sign, and an exponent
# that cannot be bounded (and is not provably non-integer) fails closed instead of skipping.
check(
    "reject2: (x+1)**(-bell(7)) refused: the negative sign no longer exempts it",
    says(explode("(x+1)**(-bell(7))"), "a symbolic power with an exponent of about"),
)
check(
    "reject2: (x+1)**(bell(7)*I) refused: an inconclusive exponent fails closed",
    says(explode("(x+1)**(bell(7)*I)"), "could not be bounded"),
)

# ---------------------------------------------------------------- coprime refinement: cost, property, rounds


def _crafted_exponent():
    """The 1996-char input from the review: 2**4472 over the odd primes below 1000."""
    from sympy import primerange

    pad = "*".join(map(str, primerange(3, 1000)))
    return f"x**({2**4471}*2/({pad}))"


def _multiset_value(terms):
    value = Fraction(1)
    for base, exp in terms.items():
        value *= Fraction(base) ** exp
    return value


# Cost: the worklist version settles this in milliseconds; the restart-from-0 scan took ~6 s.
_started = time.perf_counter()
_crafted_verdict = explode(_crafted_exponent())
_crafted_secs = time.perf_counter() - _started
check(
    f"coprime cost: reject_explosive on the crafted 1996-char exponent < 0.5 s ({_crafted_secs:.3f} s)",
    _crafted_verdict is None and _crafted_secs < 0.5,
    f"verdict={_crafted_verdict!r}",
)

# Property: seeded random multisets. Value kept (exact Fraction), keys pairwise coprime and > 1,
# no zero exponent, and the input dict itself is never mutated.
_rng = random.Random(0)  # noqa: S311 — seeded test input, not cryptography; determinism is the point
_property_bad = []
for _trial in range(300):
    _terms = {}
    for _ in range(_rng.randint(0, 6)):
        _base = _rng.choice(
            [
                _rng.randint(2, 60),
                _rng.randint(2, 5000),
                2 ** _rng.randint(1, 40),
                math.prod(_rng.sample([2, 3, 5, 7, 11, 13], 3)),
            ]
        )
        _terms[_base] = _terms.get(_base, 0) + _rng.choice([-3, -2, -1, 1, 2, 3, 7, -7])
        if _terms[_base] == 0:
            del _terms[_base]
    _snapshot = dict(_terms)
    _out = se._coprime_refine_multiset(_terms)
    _keys = list(_out)
    if not (
        _terms == _snapshot
        and _out is not _terms
        and _multiset_value(_out) == _multiset_value(_terms)
        and all(k > 1 for k in _keys)
        and all(v != 0 for v in _out.values())
        and all(math.gcd(a, b) == 1 for i, a in enumerate(_keys) for b in _keys[i + 1 :])
    ):
        _property_bad.append(_snapshot)
check(
    "coprime property: 300 seeded random multisets keep value, pairwise-coprime keys > 1, no zero exponent, input unmodified",
    not _property_bad,
    f"({len(_property_bad)} failures)",
)

# Rounds: 12**2*15/10 = 216 needs more than one pass of gcd splitting. A one-pass helper leaves
# {15: 1, 2: 1, 6: 2, 5: -1}, reads the negative 5 as a real denominator, and accepts the exponent.
check(
    "coprime rounds: {12: 2, 10: -1, 15: 1} -> {2: 3, 3: 3} (value 216, needs two rounds)",
    se._coprime_refine_multiset({12: 2, 10: -1, 15: 1}) == {2: 3, 3: 3},
)
check(
    "coprime rounds: (x+1)**(12**2*15/10) refused by the cap (216 > 200)",
    says(explode("(x+1)**(12**2*15/10)"), f"exceeds the limit of {se.MAX_SYMBOLIC_EXPONENT}"),
)

# ---------------------------------------------------------------- table-function exponents, through the bound

# A Mul/Pow/Add exponent CONTAINING a table call (not a bare call) is bounded too.
# Three of these are refused by the print-digit guard in `_numeric_ceiling_scan`,
# which runs before the symbolic-power loop, so the cap text is not their reason;
# they are pinned as refusals. The bound itself is pinned directly, below.
CAP_TEXT = f"exceeds the limit of {se.MAX_SYMBOLIC_EXPONENT}"
for _expr in (
    "(x+1)**(factorial(7)/factorial(3))",  # 840
    "(x+1)**(factorial(6)/2)",  # 360
    "(x+1)**(factorial(7)/factorial(4))",  # 210
    "(x+1)**(-10**5/-2**5)",  # 3125, trusted path
):
    check(f"table exponent: {_expr} refused by the cap", says(explode(_expr), CAP_TEXT))
for _expr in (
    "(x+1)**(factorial(5)**2/factorial(3))",  # 2400
    "(x+1)**(bell(10)/2)",  # 57987.5
    "(x+1)**(factorial(100)/factorial(98))",  # 9900
):
    check(f"table exponent: {_expr} refused (print-digit guard fires first)", explode(_expr) is not None)
for _expr in (
    "(x+1)**(factorial(5)/factorial(3))",  # 20
    "(x+1)**factorial(4)",  # 24
    "(x+1)**(1/factorial(7))",  # 1/5040, a small exponent
):
    check(f"table exponent: {_expr} accepted", explode(_expr) is None)

# The bound itself: resolved, an UPPER bound on log10 of the true exponent (numerator only for a
# table reciprocal, so `factorial(7)/factorial(3)` is bounded by log10(5040) >= log10(840)).
for _expr, _true in (
    ("factorial(7)/factorial(3)", 840),
    ("factorial(5)**2/factorial(3)", 2400),
    ("factorial(6)/2", 360),
    ("bell(10)/2", 57987.5),
    ("factorial(7)/factorial(4)", 210),
):
    _got = se._resolved_table_aware_magnitude(tree(_expr), {})
    check(
        f"table exponent bound: {_expr} resolved, log10 upper bound >= log10({_true})",
        _got[2] is True and _got[1] == 0.0 and _got[0] >= math.log10(_true) - 1e-9,
        f"(got {_got[0]:.4f})",
    )
check(
    "table exponent bound: 1/factorial(7) bounded by 0 (a reciprocal, magnitude <= 1)",
    close(se._resolved_table_aware_magnitude(tree("1/factorial(7)"), {})[0], 0.0),
)
check(
    "table sign: -bell(7) reads as negative, bell(7) does not",
    se._table_product_is_negative(tree("-bell(7)")) and not se._table_product_is_negative(tree("bell(7)")),
)

# ---------------------------------------------------------------- symbolic-exponent cap: four bypasses

# Each shape burned the full 10 s CPU budget in `logic.evaluate_expression` before the fix
# (measured 2026-10-22). Refused on the deferred tree, which is the tree `safe_parse` runs first.
UNBOUNDED_TEXT = f"exponent could not be bounded is treated as over the limit of {se.MAX_SYMBOLIC_EXPONENT}"

# H1: a constant exponent hidden behind zero-valued symbolic terms. Judged on its VALUE: the zero
# parts are stripped, and a residual that still has a free symbol stays on the symbolic skip.
for _expr in ("(x+1)**(3125+0*x)", "(x+1)**(10**5/2**5*x**0)"):
    check(f"hidden constant: {_expr} refused by the cap (H1)", says(explode(_expr), CAP_TEXT))
check("hidden constant: (x+1)**(200+0*x) accepted at the cap (H1)", explode("(x+1)**(200+0*x)") is None)
check(
    "hidden constant: (x+1)**(201+0*x) refused just past the cap (H1)",
    says(explode("(x+1)**(201+0*x)"), CAP_TEXT),
)
for _expr in ("(x+1)**(x+3125)", "(x+1)**(3125*y)"):
    check(f"hidden constant: {_expr} still skipped, a free symbol survives (H1)", explode(_expr) is None)

# H2: an exponent that cannot be bounded and has no free symbol is refused, not skipped.
for _expr in (
    "(x+1)**(re(3125))",
    "(x+1)**(3125*I**4)",
):
    check(f"unbounded exponent: {_expr} refused with the H2 message (H2)", says(explode(_expr), UNBOUNDED_TEXT))
# These three resolve to 3125 through the cap's own bound (the cap text, not the H2 message), so the
# assertion is the refusal itself: with the fix they are no longer accepted.
for _expr in ("(x+1)**(exp(0)*3125)", "(x+1)**(log(E)*3125)", "(x+1)**(Abs(-3125)*1)"):
    check(f"unbounded exponent: {_expr} refused (H2)", explode(_expr) is not None)
check(
    "unbounded exponent: (x+1)**(3125*cos(0)) stays refused by the cap, cos(0) resolves to 1 (H2)",
    says(explode("(x+1)**(3125*cos(0))"), CAP_TEXT),
)
# An exponent SymPy can prove non-integer is cheap whatever its size: no binomial expansion.
for _expr in ("x**pi", "x**E", "(x+1)**I", "x**(log(2))", "(x+1)**cos(1)", "x**(pi/2)"):
    check(f"unbounded exponent: {_expr} accepted, provably non-integer (H2)", explode(_expr) is None)
# Accepted cost: a benign constant that is neither boundable nor provably non-integer.
check("unbounded exponent: x**EulerGamma refused, the accepted false refusal (H2)", says(explode("x**EulerGamma"), UNBOUNDED_TEXT))
check("unbounded exponent: (x+1)**(x*re(3125)) still skipped, a free symbol survives (H2)", explode("(x+1)**(x*re(3125))") is None)
# H1 again: a bare `x**0` term inside the sum is 1, so `3125+x**0` is the constant 3126.
check("hidden constant: (x+1)**(3125+x**0) refused by the cap (H1)", says(explode("(x+1)**(3125+x**0)"), CAP_TEXT))

# H3: negative exponents are capped on MAGNITUDE, the same as positive ones.
for _expr in ("(x+1)**(-3125)", "(x+1)**(-10**5/2**5)", "(x+1)**(-factorial(7)/factorial(3))"):
    check(f"negative exponent: {_expr} refused by the cap (H3)", says(explode(_expr), CAP_TEXT))
check("negative exponent: (x+1)**(-200) accepted at the cap (H3)", explode("(x+1)**(-200)") is None)
check("negative exponent: (x+1)**(-201) refused just past the cap (H3)", says(explode("(x+1)**(-201)"), CAP_TEXT))

# H4: an irrational numeric base is a symbolic base as far as expansion cost goes.
for _expr in ("(pi+1)**3125", "(E+2)**201", "(sqrt(2)+1)**201"):
    check(f"irrational base: {_expr} refused by the cap (H4)", says(explode(_expr), CAP_TEXT))
for _expr in ("(pi+1)**200", "(E+2)**200", "(sqrt(2)+1)**200"):
    check(f"irrational base: {_expr} accepted at the cap (H4)", explode(_expr) is None)
check("irrational base: resolved numeric base 2**1000 still accepted (H4)", explode("2**1000") is None)
# The irrational base is routed to the cap for a COMPOUND exponent too, not only a bare Integer.
for _expr in ("(pi+1)**(10**5/2**5)", "(sqrt(2)+1)**(201/1)", "(pi+1)**(-201)"):
    check(f"irrational base: {_expr} refused by the cap, compound or negative exponent (H4)", says(explode(_expr), CAP_TEXT))
for _expr in ("(pi+1)**(1/2)", "(pi+1)**(3/2)", "(pi+1)**(-1)"):
    check(f"irrational base: {_expr} accepted, a fractional or trivial exponent is cheap (H4)", explode(_expr) is None)
# An irrational ATOM base has no sum to expand: `pi**1464` is what `polygamma(1463, 1)`
# evaluates to (tests/test_bug_sweep.py keeps that end-to-end case), and a first cut of
# this fix refused it. Only an irrational SUM base is routed to the cap.
for _expr in ("pi**1464", "(2*pi)**3125", "E**3125"):
    check(f"irrational base: {_expr} accepted, an atom or product base does not expand (H4)", explode(_expr) is None)
check(
    "irrational base: 10**5000 still refused by the digit ceiling, not the cap (H4)",
    explode("10**5000") is not None and not says(explode("10**5000"), CAP_TEXT),
)

# Both trees: `safe_parse` runs the deferred pass first, then the real-tree pass. One case per hole.
for _expr, _text in (
    ("(x+1)**(3125+0*x)", CAP_TEXT),
    ("(x+1)**(re(3125))", UNBOUNDED_TEXT),
    ("(x+1)**(-3125)", CAP_TEXT),
    ("(pi+1)**3125", CAP_TEXT),
):
    _got = se.safe_parse(_expr)
    check(
        f"public path: safe_parse refuses {_expr} (both trees)",
        _got[0] is None and _got[1] is not None and says(_got[1][1], _text),
        f"-> {_got[1]}",
    )


def timed_evaluate(expr):
    """Wall seconds and the result of `logic.evaluate_expression` on `expr`."""
    _t0 = time.perf_counter()
    _r = logic.evaluate_expression(expr)
    return time.perf_counter() - _t0, _r


# End to end, through the guarded child process: a refusal, and a fast one.
for _expr, _text in (
    ("(x+1)**(3125+0*x)", CAP_TEXT),
    ("(x+1)**(re(3125))", UNBOUNDED_TEXT),
    ("(x+1)**(-3125)", CAP_TEXT),
    ("(pi+1)**3125", CAP_TEXT),
):
    _secs, _r = timed_evaluate(_expr)
    check(
        f"end-to-end: {_expr} refused in under 1.0 s",
        _r.get("ok") is False and _text in str(_r.get("error", "")) and _secs < 1.0,
        f"-> {_secs:.2f}s ok={_r.get('ok')} code={_r.get('code')}",
    )

# ---------------------------------------------------------------- output and timing

# The module's own refusals name the exact count; spot-check that the shape of
# the boundary-term reason is stable (the substring the caller shows the user).
check("reject: a refusal is a str, never None", isinstance(explode("10**5000"), str))

print(
    f"\n=== {len(FAILS)} FAILURES in {COUNT[0]} checks ==="
    if FAILS
    else f"\n=== ALL {COUNT[0]} CHECKS PASS: magnitude boundaries hold ==="
)
sys.exit(1 if FAILS else 0)
