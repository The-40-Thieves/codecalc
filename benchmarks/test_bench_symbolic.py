"""Benchmarks for the SymPy/z3-backed symbolic tools (the `symbolic` extra).

The public entry points run their body in a killable child (`guarded_call`).
These benchmarks call the unguarded bodies directly so they measure the
parse/screen/solve work rather than fork overhead. SymPy's cache is cleared
before every round so repeated rounds are not memoised lookups.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sympy")
pytest.importorskip("z3")

from _helpers import expect

from codecalc import exact, linalg, logic, safe_expr

PEDANTIC = {"rounds": 5, "warmup_rounds": 1}


def test_safe_parse(benchmark, clear_sympy_cache):
    expr = "(x + 1)**5 - (x - 1)**5 + sin(x)**2 + cos(x)**2 + exp(log(y))"
    benchmark.pedantic(safe_expr.safe_parse, args=(expr,), setup=clear_sympy_cache, **PEDANTIC)


def test_evaluate_expression(benchmark, clear_sympy_cache):
    result = benchmark.pedantic(
        logic._evaluate_expression, args=("(x + 1)**3 - x**3",), setup=clear_sympy_cache, **PEDANTIC
    )
    expect(result["ok"], result)


def test_simplify_trig(benchmark, clear_sympy_cache):
    result = benchmark.pedantic(
        exact._simplify_expression, args=("sin(x)**2 + cos(x)**2 + (x**2 - 1)/(x - 1)",),
        setup=clear_sympy_cache, **PEDANTIC,
    )
    expect(result["ok"], result)


def test_solve_cubic(benchmark, clear_sympy_cache):
    result = benchmark.pedantic(
        exact._solve_expression, args=("x**3 - 6*x**2 + 11*x - 6",), setup=clear_sympy_cache, **PEDANTIC
    )
    expect(result["solutions"] == ["1", "2", "3"], result)


def test_algebraic_equiv(benchmark, clear_sympy_cache):
    result = benchmark.pedantic(
        exact._algebraic_equiv, args=("(x + y)**4", "x**4 + 4*x**3*y + 6*x**2*y**2 + 4*x*y**3 + y**4"),
        setup=clear_sympy_cache, **PEDANTIC,
    )
    expect(result["identical"], result)


def test_solve_linear_system(benchmark, clear_sympy_cache):
    result = benchmark.pedantic(
        logic._solve_linear, args=("x + y + z = 6; 2*x - y + z = 3; x + 2*y - z = 2", "x,y,z"),
        setup=clear_sympy_cache, **PEDANTIC,
    )
    expect(result["ok"], result)


def test_matrix_inverse_4x4(benchmark, clear_sympy_cache):
    rows = [[4, 1, 0, 2], [1, 3, 1, 0], [0, 1, 4, 1], [2, 0, 1, 5]]
    result = benchmark.pedantic(linalg._matrix, args=(rows, "inverse"), setup=clear_sympy_cache, **PEDANTIC)
    expect(result["ok"], result)


def test_truth_table_5_vars(benchmark):
    result = benchmark(logic.truth_table, "(a and b) or (c xor d) and not e")
    expect(result["row_count"] == 32, result)


def test_z3_check(benchmark):
    smt2 = """
    (declare-const x Int)
    (declare-const y Int)
    (assert (> x 5))
    (assert (< y 10))
    (assert (= (+ x y) 20))
    (assert (distinct x y))
    (check-sat)
    """
    result = benchmark(logic.z3_check, smt2)
    expect(result["result"] == "sat", result)
