"""Benchmarks for the exact-arithmetic and number-representation tools.

`eval_exact` normally runs inside a killable child process (`guarded_call`);
the benchmark calls the unguarded body `_eval_exact` so it measures the
evaluator itself rather than process-spawn overhead.
"""

from __future__ import annotations

import pytest
from _helpers import expect

from codecalc import exact

EXACT_EXPRESSIONS = {
    "rational": "0.1 + 0.2 - 0.3 + 1/3 + 2/7",
    "bigint_pow": "2**200 + 3**150 - 7**90",
    "math_funcs": "sqrt(2) * sin(pi/7) + log(10, 2) + factorial(30)",
    "bitwise": "(0xdead ^ 0xbeef) & 0xff | (1 << 20) >> 3",
}


@pytest.mark.parametrize("expr", EXACT_EXPRESSIONS.values(), ids=EXACT_EXPRESSIONS.keys())
def test_eval_exact(benchmark, expr):
    result = benchmark(exact._eval_exact, expr)
    expect(result["ok"], result)


def test_percentage_and_change(benchmark):
    def run():
        exact.percentage("37.5", "1250")
        exact.percent_change("1250", "1587.5")

    benchmark(run)


def test_stats_and_percentiles_1k(benchmark):
    nums = [float((i * 7919) % 1009) / 3.0 for i in range(1000)]

    def run():
        exact.stats(nums)
        return exact.percentiles(nums)

    result = benchmark(run)
    expect(result["ok"])


def test_radix_convert(benchmark):
    def run():
        exact.radix_convert("deadbeefcafebabe1234567890abcdef", 16, 2)
        exact.radix_convert("12345678901234567890123456789", 10, 36)
        return exact.radix_convert("0.1", 10, 2)

    result = benchmark(run)
    expect(result["ok"])


def test_bit_tools(benchmark):
    def run():
        for n in (0, 1, 255, 65535, 2**31 - 1, 2**63 - 1, -1, -(2**31)):
            exact.base_repr(n)
            exact.int_widths(n)
            exact.bit_analysis(n, align=8)
        exact.bitop(0xDEADBEEF, "xor", 0xCAFEBABE)
        exact.bitop(0xDEADBEEF, "rotl", 13, width=32)

    benchmark(run)


def test_float_repr(benchmark):
    values = [0.1, 1 / 3, 1e-310, 1.7976931348623157e308, -0.0, 3.141592653589793]

    def run():
        for v in values:
            exact.float_repr(v)

    benchmark(run)


def test_data_sizes_and_durations(benchmark):
    def run():
        for n in (1, 1023, 1 << 20, 123456789012, 1 << 50):
            exact.data_sizes(n)
        for s in (0.000123, 59.9, 3600 * 26 + 61, 86400 * 400.5):
            exact.human_duration(s)
        exact.collision_prob(1_000_000, 64)

    benchmark(run)
