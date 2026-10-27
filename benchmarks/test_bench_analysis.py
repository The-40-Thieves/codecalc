"""Benchmarks for the pure-Python hot paths that every request touches.

The input screen (`classify_unsafe` / `reject_unsafe`) runs ahead of every
symbolic tool, the spec hash is computed for every execution request, and the
complexity analyser parses source with tree-sitter (the `parsing` extra).
"""

from __future__ import annotations

import pytest
from _helpers import expect

from codecalc import complexity, providers, safe_expr, stats, units

SCREEN_INPUTS = [
    "x**2 + 2*x + 1",
    "sin(pi/4) * sqrt(2) + log(100, 10)",
    "Matrix([[1, 2], [3, 4]]).det()",
    "__import__('os').system('id')",
    "().__class__.__bases__[0].__subclasses__()",
    "factorial(10**9)",
    "lambda x: x" * 20,
]


def test_classify_unsafe(benchmark):
    def run():
        for expr in SCREEN_INPUTS:
            safe_expr.classify_unsafe(expr)
            safe_expr.reject_unsafe(expr)

    benchmark(run)


def test_spec_hash(benchmark):
    code = "\n".join(f"print({i} * {i})" for i in range(200))
    specs = [
        providers.ComputationSpec(language=lang, code=code, stdin="data\n" * 50, timeout=30, no_net=True)
        for lang in ("python3", "node", "rust", "go", "ruby")
    ]

    def run():
        return [s.spec_hash() for s in specs]

    hashes = benchmark(run)
    expect(len(set(hashes)) == len(specs))


PY_SOURCE = """
def matmul(a, b):
    n = len(a)
    out = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            for k in range(n):
                out[i][j] += a[i][k] * b[k][j]
    return out

def fib(n):
    return n if n < 2 else fib(n - 1) + fib(n - 2)

class Graph:
    def bfs(self, start):
        seen, queue = {start}, [start]
        while queue:
            node = queue.pop(0)
            for nxt in self.edges.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return seen
""" * 5

JS_SOURCE = """
function bubble(arr) {
  for (let i = 0; i < arr.length; i++) {
    for (let j = 0; j < arr.length - i - 1; j++) {
      if (arr[j] > arr[j + 1]) { [arr[j], arr[j + 1]] = [arr[j + 1], arr[j]]; }
    }
  }
  return arr;
}
const walk = (n) => n <= 0 ? 0 : 1 + walk(n - 1);
""" * 5


@pytest.mark.parametrize(("language", "source"), [("python3", PY_SOURCE), ("node", JS_SOURCE)], ids=["python", "js"])
def test_analyze_complexity(benchmark, language, source):
    pytest.importorskip("tree_sitter_language_pack")
    result = benchmark(complexity.analyze, source, language)
    expect(result["ok"], result)


def test_unit_conversions(benchmark):
    pairs = [
        (5, "km", "mile"),
        (100, "celsius", "fahrenheit"),
        (1, "GiB", "MB"),
        (3600, "s", "h"),
        (2.5, "kg", "lb"),
    ]

    def run():
        for value, src, dst in pairs:
            units.convert(value, src, dst)

    benchmark(run)


def test_mann_whitney_exact(benchmark):
    a = [1.1, 2.3, 3.2, 4.8, 5.0, 6.7, 7.1, 8.4, 9.9, 10.2]
    b = [2.0, 3.9, 5.5, 6.1, 7.7, 8.8, 9.3, 11.0, 12.4, 13.6]
    result = benchmark(stats.mann_whitney_u, a, b)
    expect(result["method"] == "exact")


def test_mann_whitney_normal_approx(benchmark):
    a = [float((i * 37) % 101) for i in range(300)]
    b = [float((i * 53) % 103) + 2 for i in range(300)]
    result = benchmark(stats.mann_whitney_u, a, b, "two-sided")
    expect(result["method"] != "exact")
