"""pytest harness for the mutation lane: runs a subset of tests/*.py scripts, one subprocess each.

This file exists only because mutmut 3.8.0 always drives pytest (it has no custom
test runner). The suite itself stays script-based: tests/conftest.py turns pytest
collection off and issue #68 tracks converting the suite, a separate and larger
change. Nothing in CI's normal jobs imports this file, and pytest is not a
project dependency; the mutation workflow adds it ad hoc.

Each case runs one standalone script in a fresh interpreter, exactly as CI does
(`python tests/<f>.py`), and asserts exit status 0. The fresh process matters:
run in-process, test_security.py's parse_expr spy control fails after an earlier
script has already evaluated "2+2" (its result is cached in the same process).
mutmut's MUTANT_UNDER_TEST environment variable is inherited by the child, so the
mutant still reaches the script.

Subset: the scripts whose serial runtime is short enough to run once per mutant.
Excluded on purpose: test_bug_sweep.py (~53 s serial) and
test_measured_safe_callables.py (> 240 s, unusable per mutant).
"""

import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

SCRIPTS = [
    "test_matrix.py",
    "test_fuzz_smoke.py",
    "test_differential_corpus.py",
    "test_error_codes.py",
    "test_security.py",
]


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_exits_clean(name):
    path = REPO_ROOT / "tests" / name
    assert path.is_file(), f"missing script: {path}"
    proc = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    tail = (proc.stdout + proc.stderr)[-800:]
    assert proc.returncode == 0, f"{name} exited {proc.returncode}:\n{tail}"
