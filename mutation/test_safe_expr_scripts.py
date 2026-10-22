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

import json
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# mutmut's stats run (MUTANT_UNDER_TEST=stats) maps code to tests by recording trampoline hits in
# mutmut's state()._stats and crediting them to the running pytest test at teardown
# (mutmut/runners/harness.py, run_stats). Hits made in a child process land in the CHILD's
# state and are lost, so without this every mutant looked uncovered and mutmut stopped early
# ("could not find any test case for any mutant"). During the stats run each script therefore
# starts through this wrapper, which writes the child's hits to a file at exit; the test then adds
# them to this process's state() before teardown. Mutant runs (any other value) are unchanged.
_STATS_CHILD = """\
import atexit, json, runpy, sys
out, script = sys.argv[1], sys.argv[2]
def _dump():
    from mutmut.state import state
    with open(out, "w", encoding="utf-8") as f:
        json.dump(sorted(state()._stats), f)
atexit.register(_dump)
sys.argv = [script]
runpy.run_path(script, run_name="__main__")
"""

SCRIPTS = [
    "test_matrix.py",
    "test_fuzz_smoke.py",
    "test_differential_corpus.py",
    "test_error_codes.py",
    "test_security.py",
]


def _child_env():
    # Under mutmut REPO_ROOT is mutants/: putting it first on PYTHONPATH makes the child import
    # mutants/codecalc (the mutated copy) ahead of the venv's editable install of the original.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
    return env


@pytest.fixture(scope="session", autouse=True)
def _compiled_safe_expr():
    # The mutated safe_expr.py holds every mutant (~46 MB): its first import compiles it, measured
    # at 406 s and 1.2 GB on Cave (0.36 s once the .pyc is cached). Pay that once here, with its own
    # timeout, so it never counts against a script's 300 s timeout below.
    subprocess.run(
        [sys.executable, "-c", "import codecalc.safe_expr"],
        env=_child_env(),
        timeout=1800,
        check=True,
    )


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_exits_clean(name):
    path = REPO_ROOT / "tests" / name
    assert path.is_file(), f"missing script: {path}"
    collecting = os.environ.get("MUTANT_UNDER_TEST") == "stats"
    with tempfile.TemporaryDirectory() as tmp:
        hits_file = pathlib.Path(tmp) / "hits.json"
        if collecting:
            cmd = [sys.executable, "-c", _STATS_CHILD, str(hits_file), str(path)]
        else:
            cmd = [sys.executable, str(path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False, env=_child_env())
        tail = (proc.stdout + proc.stderr)[-800:]
        assert proc.returncode == 0, f"{name} exited {proc.returncode}:\n{tail}"
        if collecting:
            assert hits_file.is_file(), f"{name}: stats wrapper wrote no hits file:\n{tail}"
            from mutmut.state import state

            state()._stats.update(json.loads(hits_file.read_text(encoding="utf-8")))
