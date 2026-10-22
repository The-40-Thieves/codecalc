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
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

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


def unresolved_quiet(got):
    """Not resolved and no reason: the caller decides, nothing is refused here."""
    return got is not None and got[2] is False and got[3] is None


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
check("gamma is strictly increasing above the edge", pole("gamma(3)")[0] > pole("gamma(2)")[0] > pole("gamma(1)")[0])
check("gamma(999/1000) refused just below z = 1", refused(pole("gamma(999/1000)"), "gamma()"))
check("gamma(1/2) refused below z = 1", refused(pole("gamma(1/2)"), "gamma()"))
check("digamma(1) resolved", pole("digamma(1)") is not None and pole("digamma(1)")[2] is True)
check("digamma(1/2) refused below z = 1", refused(pole("digamma(1/2)"), "digamma()"))

# polygamma(n, z): n is 0 or a positive integer; z >= 1.
check("polygamma(0, 1) resolved at the edge", pole("polygamma(0,1)")[2] is True)
check("polygamma(1, 1) resolved at the edge", pole("polygamma(1,1)")[2] is True)
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
