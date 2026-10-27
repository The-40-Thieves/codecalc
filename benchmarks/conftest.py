"""Shared fixtures for the CodSpeed benchmark suite.

Unlike tests/ (standalone scripts, collection disabled — see
tests/conftest.py), this directory IS a pytest suite, collected by
pytest-codspeed. pytest stays out of every manifest and lockfile: CI adds it
ad hoc with `uv run --with`, the same way the mutation lane does.

Run locally:

    uv sync --locked --all-extras
    PYTHONPATH=. uv run --no-sync --with pytest-codspeed pytest benchmarks/ --codspeed
"""

from __future__ import annotations

import pytest


@pytest.fixture
def clear_sympy_cache():
    """Return a setup callable that empties SymPy's memoisation cache.

    SymPy caches results by argument, so the second round of the same
    expression is a dict lookup rather than the computation the tool performs
    for a caller. Clearing before each round measures the real work.
    """
    sympy = pytest.importorskip("sympy")

    # pedantic() calls setup with the target's args; returning None keeps them.
    def setup(*_args, **_kwargs) -> None:
        sympy.core.cache.clear_cache()

    return setup
