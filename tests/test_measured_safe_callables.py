"""THE-1095 round 14 (coordinator review of 7630e87, item 1) + round 15
(grok issue 3, `verify-1095-r14-grok.log`): re-runs `scripts/measure_
safe_callables.py`'s own classification for THIS process's own SHARD of
the committed allowlist, and asserts none shows a genuine regression
(a TIMEOUT, a CRASH, or an over-digit result) on the RUNNING box.

`codecalc/_measured_safe_callables.json` (the committed measurement
record `codecalc.safe_expr._MEASURED_SAFE_CALLABLES` loads from) was
generated on ONE box, at ONE point in time — this is the guard against
that record going stale (a slower CI runner, a SymPy version bump that
changed one name's own cost) rather than being trusted forever.

Round 15 changed HOW that re-check works, after `verify-1095-r14-grok`
found the round-14 version broken on `ci-python.yml`'s own
`windows-latest`/`macos-latest` legs (`resource`/`signal.SIGALRM` — used
by the probe child this test drives — do not exist on Windows and
misbehave with `RLIMIT_AS` on macOS; both are now fixed at the source,
in `scripts/measure_safe_callables.py`'s own `_PROBE_CHILD`, not here)
and that the OLD all-or-nothing `FAST_MS` re-check would flag a merely-
SLOWER-on-a-loaded-runner result as a false regression. This test now
calls `shard_check` (not a random sample of 30): it covers every
allowlisted name across the CI matrix, split deterministically by
platform + Python minor version so each of `ci-python.yml`'s six
`tests` jobs re-measures a DISJOINT shard, and it fails only on a
TIMEOUT, a CRASH, or an over-`MAX_RESULT_DIGITS` result — never on an
`OK` result that is merely slower than the tight `FAST_MS` bar the
ORIGINAL measurement used, as long as it stays under `REGRESSION_MS_
TOLERANCE` (4x `FAST_MS`). See `shard_check`'s own docstring for the
full rationale.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


if __name__ == "__main__":
    from measure_safe_callables import DATA_PATH, NUM_SHARDS, _shard_id, shard_check

    from codecalc import safe_expr as se

    if not DATA_PATH.exists():
        check(f"{DATA_PATH} exists (run `python scripts/measure_safe_"
              f"callables.py --write` to generate it)",
              False, "-> missing")
    else:
        import json

        data = json.loads(DATA_PATH.read_text())
        allowlist = data["allowlist"]
        check(f"the committed allowlist ({len(allowlist)} names) matches "
              f"what codecalc.safe_expr._MEASURED_SAFE_CALLABLES actually "
              f"loaded",
              set(allowlist) == se._MEASURED_SAFE_CALLABLES,
              f"-> file has {len(allowlist)}, loaded has "
              f"{len(se._MEASURED_SAFE_CALLABLES)}")

        shard = _shard_id()
        shard_size = sum(1 for i, _n in enumerate(sorted(allowlist)) if i % NUM_SHARDS == shard)
        regressions = shard_check()
        check(f"this box's own shard ({shard}/{NUM_SHARDS}) of the allowlist "
              f"({shard_size} names) shows no TIMEOUT/CRASH/over-digit regression",
              not regressions, f"-> regressions={regressions!r}")

    print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
          "\n=== MEASURED-SAFE-CALLABLES SHARD CLEAN ===")
    sys.exit(1 if FAILS else 0)
