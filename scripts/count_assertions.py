#!/usr/bin/env python3
"""Count the assertions a run actually EMITS, rather than the sites that could.

`scripts/check_claims.py` compares the README's assertion total to the number of
static `check(` sites. That is a plausibility check: loops multiply, so 624 sites
could back 1062 assertions or 700 or 900, and it cannot tell which. Nothing
compared the claim to what a run emits.

It matters here more than it would elsewhere, because these suites are not
pytest. They count at runtime through a `check(name, cond, detail)` helper that
prints `PASS`/`FAIL`, and this repo has already shipped three suites that
printed output, exited 0 and asserted NOTHING (PR #13). A site-count gate would
not have caught those: the sites existed.

So this runs every suite and counts the PASS/FAIL lines they print.

    python scripts/count_assertions.py                 # run everything, report
    python scripts/count_assertions.py --floor 1063    # and fail below a floor

A FLOOR, not equality, so adding tests does not churn the README. Assertions
disappearing is the failure worth catching; assertions appearing is not.

WHAT THIS NUMBER IS. The count on THIS machine. Suites skip when a capability is
absent — no native executor, no symlink privilege, a missing runtime — so a
Windows or fallback host legitimately emits fewer, and the README's figure is
the full-capability Linux count. Do not read a lower number here as a
regression without checking the skips.

WHY IT IS NOT WIRED INTO CI AS A TOTAL. No CI job runs all 21 suites: ci-python
splits them across `tests` (12) and `sandbox + security` (5), so no single job
can sum them. Making one by teeing each step would be worse than the problem —
GitHub's default shell is `bash -e`, not `bash -o pipefail`, so
`python tests/x.py | tee -a log` returns tee's exit status and every one of
those steps would stop failing the build. The per-suite floor that CAN run on a
bare checkout lives in check_claims.py instead: every suite file must contain at
least one check() site.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"

#: A suite prints one of these per assertion. Anchored: `PASS` inside a detail
#: string or a docstring must not inflate the count.
ASSERTION_RE = re.compile(r"^(PASS|FAIL) ", re.MULTILINE)

#: Ordered last, deliberately. Its fork-bomb assertions drive the process count
#: to the limit, and on a slow host the reaping lags long enough that the NEXT
#: subprocess cannot fork. ci-python.yml runs it last for the same reason.
RUN_LAST = "test_security.py"

#: Per-suite ceiling. test_security's fork-bomb work is genuinely slow; the rest
#: are far below this.
TIMEOUT_S = 900


def suites() -> list[Path]:
    found = sorted(TESTS.glob("test_*.py"))
    last = [p for p in found if p.name == RUN_LAST]
    return [p for p in found if p.name != RUN_LAST] + last


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--floor", type=int, help="fail if the measured total is below this")
    ap.add_argument("--quiet", action="store_true", help="print only the total")
    args = ap.parse_args()

    found = suites()
    if len(found) < 10:
        print(f"::error::floor: found only {len(found)} suite(s) under {TESTS}. The tests "
              f"directory has moved, and zero suites would otherwise report a total of 0.")
        return 1

    total = 0
    empty: list[str] = []
    for path in found:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=REPO, capture_output=True, text=True, timeout=TIMEOUT_S,
            env={"PYTHONPATH": str(REPO), "PATH": __import__("os").environ.get("PATH", "")},
        )
        n = len(ASSERTION_RE.findall(proc.stdout))
        total += n
        if n == 0:
            empty.append(path.name)
        if not args.quiet:
            print(f"{path.stem:<34} {n:>5}")

    if not args.quiet:
        print("-" * 40)
    print(f"{'MEASURED TOTAL':<34} {total:>5}")

    if empty:
        print(f"::error::{len(empty)} suite(s) emitted NO assertions: {', '.join(empty)}. "
              f"A suite that prints output and exits 0 without asserting anything is the "
              f"failure this exists to catch; the check() sites being present is not enough.")
        return 1

    if args.floor is not None and total < args.floor:
        print(f"::error::measured {total} assertions, floor is {args.floor}. Assertions "
              f"disappeared. If that is deliberate, lower the floor in the same change "
              f"so the drop is reviewable rather than silent.")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
