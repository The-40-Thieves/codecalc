"""Why `pytest tests/` collects nothing here, and what to run instead.

This suite is 20 standalone SCRIPTS. Each one runs its assertions at module
scope, prints a PASS/FAIL/SKIP line per property, and ends with
`sys.exit(1 if FAILS else 0)`. That is the whole design: a suite file is a
program you can run directly and read the output of, and CI runs each one as
its own named step so a failure names itself.

pytest collects a test file by IMPORTING it, so importing any of these runs
the suite and then exits the interpreter mid-collection. pytest reports that
as `INTERNALERROR> SystemExit` and stops — no tests ran, nothing after the
first file was even looked at. That is issue #68, and it reproduces exactly.

WHY NOT JUST WRAP THE `sys.exit` IN `if __name__ == "__main__":`
Because it would replace a loud failure with a quiet one. Measured on this
tree:

    20 test files
    16 with a module-level sys.exit   <- fixing one moves the abort to the next
     0 with any `def test_` function  <- so there is nothing to collect anyway

With every exit wrapped, pytest would import 20 modules, find zero test
functions, and report "no tests ran". The acceptance criterion "collection
completes without aborting" would be satisfied by a run that tests NOTHING.
An INTERNALERROR at least tells you something is wrong; a green "no tests ran"
does not. Converting the suite to pytest is a different and much larger
change, and not one this file should make quietly.

So collection is turned off deliberately and says so out loud. `pytest tests/`
now exits 5 (no tests collected) with the header below, rather than exiting on
an internal error partway through the first file.

This module is inert without pytest, which is the normal case: pytest is not a
dependency of this project, appears in no manifest, no lockfile and no CI job,
and nothing here imports it.
"""

from __future__ import annotations

#: Every file in this directory. pytest never imports them, so none of them
#: runs its assertions and calls sys.exit during collection.
collect_ignore_glob = ["*"]

_EXPLANATION = (
    "codecalc's tests are standalone scripts, not pytest cases — collection is "
    "disabled on purpose (see tests/conftest.py, issue #68).\n"
    "  run one:        python tests/test_security.py\n"
    "  run all:        for f in tests/test_*.py; do python \"$f\" || fail=1; done; "
    "[ -z \"${fail:-}\" ]\n"
    "Each script exits non-zero on failure and prints one line per property."
)


def pytest_report_header(config) -> str:
    """Say why the run is empty, at the top, where the reason is still visible.

    Without this, `pytest tests/` reports "no tests ran" — technically true,
    indistinguishable from a directory with no tests in it, and no more
    actionable than the INTERNALERROR it replaced.
    """
    return _EXPLANATION
