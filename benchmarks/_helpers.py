"""Helpers shared by the benchmark modules."""

from __future__ import annotations

import pytest


def expect(condition: object, detail: object = "") -> None:
    """Sanity-check a benchmark's result without a bare `assert`.

    `assert` is stripped under `python -O`, and the repo's static analysers
    flag it outside tests/. A benchmark that silently measured an error path
    would be worse than no benchmark, so the check must always run.
    """
    if not condition:
        pytest.fail(f"unexpected benchmark result: {detail!r}")
