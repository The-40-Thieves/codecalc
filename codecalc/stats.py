"""A small, dependency-free nonparametric-testing kit for `verify_optimization`.

Why this exists: `_speedup`'s median-of-ratios only ever compared a number to
a threshold. With `repeats` per size, the smallest one-sided p a same-size
comparison could ever produce is `1/C(2*repeats, repeats)` — for 3-vs-3 that
floor is 1/20 — so no single size could reach the conventional alpha=0.05, and
the accept/reject line had no statistical footing at all: "the median ratio
cleared 1.15x" is an arithmetic fact, not evidence the difference is real
rather than noise. This module adds the missing test.

Ported from mrnh/rigor on GitHub, MIT licensed (no URL here on purpose —
tests/test_offline.py bans every outbound URL literal from this package):

    Copyright (c) 2026 Marian Heidtmann

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to permit
    persons to whom the Software is furnished to do so, subject to the
    following conditions: the above copyright notice and this permission
    notice shall be included in all copies or substantial portions of the
    Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

`_ranks_with_ties` is `rigor/nonparametric.py`'s function verbatim.
`normal_cdf` is `rigor/distributions.py`'s function verbatim (itself a thin
wrapper over `statistics.NormalDist`, which is why it is trivial to re-verify
rather than merely trust — see the bottom of this docstring).
`rank_biserial_correlation` is `rigor/effect_size.py`'s function verbatim.
`mann_whitney_u` here is NOT a straight port: rigor's version only computes a
two-sided p-value from the normal approximation. This module needs a
ONE-SIDED test (`alternative="less"`/`"greater"`, not just `"two-sided"`) and,
for the small samples `verify_optimization` actually produces (n=m=5 per
size, well under rigor's own "~20 per group" accuracy caveat), an EXACT
p-value rather than an approximation — so the tail-selection and the exact
enumeration below (`_exact_u_counts`) are new, built from the standard
Mann-Whitney recurrence and cross-checked against the textbook n=m=3 case in
tests/test_stats.py (U=0 -> exact one-sided p=1/20) rather than trusted
because the port looked right.

WHY EXACT AT ALL: the normal approximation is asymptotic. At n=m=5 (10
observations) it is a rough guide, not a number worth calling alpha=0.05
against — this is exactly the regime the exact permutation distribution
covers without approximation error. `n1 + n2 <= 20` is used as the cutover
(matching the ticket): 20 observations gives C(20,10) = 184,756 arrangements,
enumerated here in O(n1*n2) states via dynamic programming, not brute force.

TIES: the exact enumeration counts arrangements of DISTINCT values. Wall-clock
timings can tie (coarse timer resolution, or two genuinely-equal runs), and a
tied value breaks the one-argument-per-rank-position assumption the exact
recurrence relies on. Rather than build a second, tie-corrected exact table,
a tie anywhere in the combined sample routes to the normal approximation
(which already carries its own tie correction, ported from rigor unchanged)
regardless of n1+n2 -- exact-with-ties is a materially different and more
intricate distribution, and silently pretending the untied recurrence still
applies would be worse than falling back to the (correctly tie-corrected)
approximation one sample size early.
"""

from __future__ import annotations

import math
from functools import cache
from statistics import NormalDist

_NORMAL = NormalDist(0, 1)

#: Above this many total observations, exact enumeration is not attempted —
#: not for a performance reason (the DP is cheap well past this) but because
#: the ticket's stated cutover is n1+n2<=20, matching the sample sizes this
#: module actually sees (n=m=5 per size -> 10 total, comfortably inside it).
EXACT_MAX_N = 20


def normal_cdf(x: float) -> float:
    """Standard normal CDF. Ported from rigor/distributions.py verbatim."""
    return _NORMAL.cdf(x)


def _ranks_with_ties(xs: list[float]) -> tuple[list[float], list[int]]:
    """Average ('fractional') ranks, 1-indexed, ties sharing the mean rank.

    Also returns the size of each tied group, for tie-correction terms.
    Ported from rigor/nonparametric.py verbatim.
    """
    n = len(xs)
    order = sorted(range(n), key=lambda i: xs[i])
    ranks = [0.0] * n
    tie_sizes = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        tie_sizes.append(j - i + 1)
        i = j + 1
    return ranks, tie_sizes


def rank_biserial_correlation(u1_statistic: float, n1: int, n2: int) -> float:
    """Effect size for a Mann-Whitney U test (Wendt, 1972), in [-1, 1].

    Call with U for sample1 (`mann_whitney_u`'s `u` field, which follows this
    same sample1-first convention) and the two sample sizes. Positive means
    sample1's values tend to exceed sample2's; negative means the reverse; 0
    is no tendency either way. Ported from rigor/effect_size.py verbatim.
    """
    if n1 <= 0 or n2 <= 0:
        raise ValueError("n1 and n2 must be positive")
    return (2.0 * u1_statistic) / (n1 * n2) - 1.0


@cache
def _exact_u_counts(n1: int, n2: int) -> tuple[int, ...]:
    """Number of ways to arrange n1+n2 DISTINCT values, split into groups of
    size n1 and n2, that produce each possible U (for group 1), U in
    [0, n1*n2].

    Standard Mann-Whitney recurrence (e.g. Mann & Whitney 1947's own
    derivation, restated in most nonparametric-statistics texts):

        f(n1, n2, u) = f(n1-1, n2, u-n2) + f(n1, n2-1, u)
        f(0, n2, u)  = 1 if u == 0 else 0
        f(n1, 0, u)  = 1 if u == 0 else 0

    Read as: the largest of the n1+n2 combined ranks either belongs to group
    1 (contributing n2 to U, since it exceeds all n2 group-2 values — leaving
    the f(n1-1, n2, u-n2) sub-problem) or to group 2 (contributing 0 — leaving
    f(n1, n2-1, u)). Verified against `sum(counts) == C(n1+n2, n1)` and
    against a brute-force enumeration from the definition of U in
    tests/test_stats.py, not trusted from the recurrence alone.
    """
    total = [0] * (n1 * n2 + 1)
    if n1 == 0 or n2 == 0:
        total[0] = 1
        return tuple(total)
    left = _exact_u_counts(n1 - 1, n2)
    right = _exact_u_counts(n1, n2 - 1)
    for u, count in enumerate(left):
        if count:
            total[u + n2] += count
    for u, count in enumerate(right):
        if u <= n1 * n2:
            total[u] += count
    return tuple(total)


def _exact_p(u1: float, n1: int, n2: int, alternative: str) -> float:
    counts = _exact_u_counts(n1, n2)
    total_arrangements = math.comb(n1 + n2, n1)
    u_int = round(u1)
    if alternative == "less":
        tail = sum(counts[: u_int + 1])
    elif alternative == "greater":
        tail = sum(counts[u_int:])
    else:
        raise ValueError(f"unknown alternative {alternative!r}")
    return min(1.0, tail / total_arrangements)


def _normal_p(u1: float, n1: int, n2: int, tie_sizes: list[int], alternative: str) -> float:
    n = n1 + n2
    tie_term = sum(t**3 - t for t in tie_sizes)
    mean_u = n1 * n2 / 2.0
    var_u = (n1 * n2 / 12.0) * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0.0
    if var_u <= 0.0:
        # every value tied: no power to detect a difference either way.
        return 1.0
    if alternative == "less":
        # P(U1 <= u1), continuity correction moves the boundary UP by 0.5.
        z = (u1 + 0.5 - mean_u) / math.sqrt(var_u)
        return min(1.0, normal_cdf(z))
    if alternative == "greater":
        z = (u1 - 0.5 - mean_u) / math.sqrt(var_u)
        return min(1.0, 1.0 - normal_cdf(z))
    raise ValueError(f"unknown alternative {alternative!r}")


def mann_whitney_u(sample1: list[float], sample2: list[float],
                   alternative: str = "less") -> dict:
    """One-sided (or two-sided) Mann-Whitney U test, no scipy required.

    `alternative`:
      "less"      H1: sample1 is stochastically LESS than sample2
      "greater"   H1: sample1 is stochastically GREATER than sample2
      "two-sided" H1: sample1 and sample2 differ (either direction)

    `u` in the result is U for sample1 (the scipy/rigor convention: the
    number of pairs (s1_i, s2_j) with s1_i > s2_j). Feed it straight into
    `rank_biserial_correlation(u, n1, n2)` for the effect size.

    Method: exact enumeration of the U-distribution when there are no ties
    and n1+n2 <= EXACT_MAX_N (20); the normal approximation with a tie
    correction otherwise. Both are documented in the module docstring above,
    including why ties always fall back to the approximation.
    """
    if alternative not in ("less", "greater", "two-sided"):
        raise ValueError(f"alternative must be 'less', 'greater', or 'two-sided', got {alternative!r}")
    n1, n2 = len(sample1), len(sample2)
    if n1 < 1 or n2 < 1:
        raise ValueError("need at least 1 observation per sample")
    combined = list(sample1) + list(sample2)
    ranks, tie_sizes = _ranks_with_ties(combined)
    r1 = sum(ranks[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2.0
    has_ties = len(tie_sizes) < len(combined)

    exact = not has_ties and (n1 + n2) <= EXACT_MAX_N
    if alternative == "two-sided":
        if exact:
            p_less = _exact_p(u1, n1, n2, "less")
            p_greater = _exact_p(u1, n1, n2, "greater")
        else:
            p_less = _normal_p(u1, n1, n2, tie_sizes, "less")
            p_greater = _normal_p(u1, n1, n2, tie_sizes, "greater")
        p = min(1.0, 2 * min(p_less, p_greater))
    elif exact:
        p = _exact_p(u1, n1, n2, alternative)
    else:
        p = _normal_p(u1, n1, n2, tie_sizes, alternative)

    return {
        "u": u1,
        "p_value": p,
        "n1": n1,
        "n2": n2,
        "method": "exact" if exact else "normal_approximation",
        "alternative": alternative,
    }
