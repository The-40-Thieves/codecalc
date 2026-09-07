"""Unit tests for codecalc/stats.py — the Mann-Whitney U primitive
`verify_optimization` bases its significance test on (see
codecalc/optimization.py's `_infer_speedup`).

This module was ported from mrnh/rigor (MIT) rather than written from
scratch, and the module docstring says explicitly that the port is
re-verified here rather than trusted: every assertion below checks the
result against a value that can be confirmed BY HAND (the textbook n=m=3
table, `sum(counts) == C(n1+n2, n1)`, the sign of the effect size), not
against another run of the same code.
"""

from __future__ import annotations

import math
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import stats

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ═══ the textbook n=m=3 case: U=0, exact one-sided p = 1/20 ════════════════
# The standard worked example for the exact Mann-Whitney distribution: every
# value in sample1 is below every value in sample2, so U(sample1) — the
# number of pairs where sample1 exceeds sample2 — is 0, the most extreme
# value it can take. There is exactly ONE way (out of C(6,3)=20) to split six
# distinct ranks into two groups of three such that group 1 gets every one of
# the bottom three ranks, so the exact one-sided p testing "sample1 is
# stochastically less than sample2" is exactly 1/20 — not a simulated
# estimate, a count.
r = stats.mann_whitney_u([1, 2, 3], [4, 5, 6], alternative="less")
check("U=0 for a fully-separated n=m=3 sample", r["u"] == 0, f"-> {r}")
check("exact method chosen (n1+n2=6 <= EXACT_MAX_N, no ties)",
      r["method"] == "exact", f"-> {r['method']}")
check("one-sided p is EXACTLY 1/20 for the textbook U=0 case",
      abs(r["p_value"] - 1 / 20) < 1e-12, f"-> p={r['p_value']}")

# The mirror image: sample1 entirely ABOVE sample2 -> U = n1*n2 (the other
# extreme), and testing "greater" gives the same exact p by symmetry.
r_hi = stats.mann_whitney_u([4, 5, 6], [1, 2, 3], alternative="greater")
check("U=n1*n2 for the mirrored fully-separated sample",
      r_hi["u"] == 9, f"-> {r_hi}")
check("its one-sided p is also exactly 1/20 (symmetry)",
      abs(r_hi["p_value"] - 1 / 20) < 1e-12, f"-> p={r_hi['p_value']}")

# Two-sided must double the one-sided extreme-tail p (capped at 1).
r_two = stats.mann_whitney_u([1, 2, 3], [4, 5, 6], alternative="two-sided")
check("two-sided p for the fully-separated case is 2/20 = 0.1",
      abs(r_two["p_value"] - 0.1) < 1e-12, f"-> p={r_two['p_value']}")


# ═══ the exact recurrence sums to the total arrangement count ══════════════
# `_exact_u_counts(n1, n2)` counts, for every U in [0, n1*n2], how many of the
# C(n1+n2, n1) ways to split n1+n2 distinct values into two groups produce
# that U. If the recurrence is right, the counts must sum to EXACTLY that
# binomial coefficient for every (n1, n2) — this is the check the module
# docstring promises rather than trusting the recurrence transcribed
# correctly from a textbook.
_all_sums_ok = True
for n1 in range(8):
    for n2 in range(8):
        counts = stats._exact_u_counts(n1, n2)
        expected = math.comb(n1 + n2, n1)
        if sum(counts) != expected or len(counts) != n1 * n2 + 1:
            _all_sums_ok = False
            print(f"     mismatch at n1={n1} n2={n2}: sum={sum(counts)} expected={expected}")
check("exact U-distribution counts sum to C(n1+n2, n1) for every n1,n2 in 0..7",
      _all_sums_ok)


def _brute_force_u_counts(n1: int, n2: int) -> tuple[int, ...]:
    """Re-derive `_exact_u_counts` from the DEFINITION of U — for every way
    to split n1+n2 distinct ranks into two groups, count pairs where group1
    exceeds group2 — rather than trusting the recurrence transcribed from a
    textbook. Only used here, at small n, as the independent check the
    module docstring promises; the real code path never brute-forces it (it
    would not scale past a handful of observations)."""
    import itertools
    total = [0] * (n1 * n2 + 1)
    for combo in itertools.combinations(range(n1 + n2), n1):
        g2 = [v for v in range(n1 + n2) if v not in combo]
        u1 = sum(1 for a in combo for b in g2 if a > b)
        total[u1] += 1
    return tuple(total)


for _n1, _n2 in [(3, 3), (2, 4), (1, 5), (4, 3), (5, 5)]:
    _recurrence = stats._exact_u_counts(_n1, _n2)
    _brute = _brute_force_u_counts(_n1, _n2)
    check(f"n1={_n1},n2={_n2}: recurrence matches brute-force enumeration "
          f"from the DEFINITION of U (not just its own math)",
          _recurrence == _brute, f"-> recurrence={_recurrence} brute={_brute}")

_counts_3_3 = stats._exact_u_counts(3, 3)
check("n1=n2=3 exact counts are symmetric (U and n1*n2-U equally likely)",
      _counts_3_3 == tuple(reversed(_counts_3_3)), f"-> {_counts_3_3}")
check("n1=n2=3: the extremes (U=0 and U=9) are the rarest, each 1 way",
      _counts_3_3[0] == 1 and _counts_3_3[9] == 1, f"-> {_counts_3_3}")


# ═══ ties fall back to the normal approximation, never the exact table ═════
r_tied = stats.mann_whitney_u([1, 1, 1], [1, 1, 1], alternative="two-sided")
check("all-tied samples use the normal approximation, not exact enumeration",
      r_tied["method"] == "normal_approximation", f"-> {r_tied['method']}")
check("all-tied samples have no power to detect a difference (p=1.0)",
      r_tied["p_value"] == 1.0, f"-> p={r_tied['p_value']}")

r_one_tie = stats.mann_whitney_u([1, 2, 3], [3, 4, 5], alternative="two-sided")
check("a SINGLE shared value (n1+n2=6, still <= EXACT_MAX_N) still falls back",
      r_one_tie["method"] == "normal_approximation", f"-> {r_one_tie}")


# ═══ beyond EXACT_MAX_N, the normal approximation is used regardless of ties
r_big = stats.mann_whitney_u(list(range(15)), list(range(100, 115)),
                             alternative="less")
check(f"n1+n2={15 + 15} > EXACT_MAX_N ({stats.EXACT_MAX_N}) uses the normal approximation",
      r_big["method"] == "normal_approximation", f"-> {r_big['method']}")
check("a huge, unambiguous separation still yields a tiny p under the approximation",
      r_big["p_value"] < 0.001, f"-> p={r_big['p_value']}")


# ═══ rank_biserial_correlation: sign and magnitude ═════════════════════════
check("U at the minimum extreme (sample1 always smaller) -> effect size -1",
      stats.rank_biserial_correlation(0, 3, 3) == -1.0)
check("U at the maximum extreme (sample1 always larger) -> effect size +1",
      stats.rank_biserial_correlation(9, 3, 3) == 1.0)
check("U at the midpoint (no tendency either way) -> effect size 0",
      stats.rank_biserial_correlation(4.5, 3, 3) == 0.0)


# ═══ input validation ═══════════════════════════════════════════════════════
try:
    stats.mann_whitney_u([], [1], alternative="less")
    check("empty sample1 raises", False)
except ValueError:
    check("empty sample1 raises ValueError", True)

try:
    stats.mann_whitney_u([1], [2], alternative="sideways")
    check("an unknown alternative raises", False)
except ValueError:
    check("an unknown alternative raises ValueError", True)


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL STATS TESTS PASS ===")
sys.exit(1 if FAILS else 0)
