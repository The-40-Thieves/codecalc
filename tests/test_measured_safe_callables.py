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

        # THE-1095 round 16 (coordinator's own live spot-check of the
        # uncommitted round-16 tree): `--refresh-names`'s own merge put
        # `erf` (and eight siblings — see `_prune_handled_names`'s own
        # docstring) BACK on the allowlist, from an OLD raw report
        # written before those names got their own dedicated table/
        # pole-sensitive/elementary row — silently re-enabling the
        # generic `MAX_HEAVY_ARG` cap for a name whose dedicated bound
        # never needed one (`erf(10000)` wrongly refused again). A name
        # is either MEASURED-safe or ALREADY-handled, never both — this
        # asserts the invariant directly, independent of how the
        # allowlist was produced, so a future refresh path that forgets
        # to prune is caught here rather than by a downstream `test_bug_
        # sweep.py` symptom.
        from measure_safe_callables import _already_handled_names
        overlap = set(allowlist) & _already_handled_names()
        check("the allowlist and _already_handled_names() (pole-"
              "sensitive/table/elementary names, each with its OWN "
              "dedicated bound) are DISJOINT",
              not overlap, f"-> overlap={sorted(overlap)!r}")

        shard = _shard_id()
        shard_size = sum(1 for i, _n in enumerate(sorted(allowlist)) if i % NUM_SHARDS == shard)
        regressions = shard_check()
        check(f"this box's own shard ({shard}/{NUM_SHARDS}) of the allowlist "
              f"({shard_size} names) shows no TIMEOUT/CRASH/over-digit regression",
              not regressions, f"-> regressions={regressions!r}")

    # THE-1095 round 16 (coordinator addendum, item 6): the probe child
    # must NEVER exit with empty stdout — that is exactly what made a
    # genuine platform-setup failure (macOS's own RLIMIT_DATA raising)
    # indistinguishable from a real hang, both reading back as CRASH
    # with no way to tell them apart from CI output alone. Runs the
    # REAL child (this platform's own real setup path) for one ordinary
    # shape and asserts stdout is non-empty and starts with a
    # RECOGNIZED verdict word.
    import subprocess
    import sys as _sys

    from measure_safe_callables import _PROBE_CHILD, PROBE_CALL_TIMEOUT_S, PROBE_MEMORY_KB

    from codecalc.safe_expr import MAX_HEAVY_ARG as _MHA
    _child_src = _PROBE_CHILD.format(mem_kb=PROBE_MEMORY_KB, name="gcd",
                                      shape_name="two_ints", heavy=_MHA,
                                      call_timeout=PROBE_CALL_TIMEOUT_S)
    _proc = subprocess.run([_sys.executable, "-c", _child_src],
                           capture_output=True, text=True, timeout=10)
    _out = _proc.stdout.strip()
    check("the probe child never exits with EMPTY stdout (the exact "
          "shape a silent platform-setup failure took before this "
          "round: every shape read back as an unexplained CRASH)",
          bool(_out), f"-> stdout={_out!r} stderr={_proc.stderr[-300:]!r}")
    check("  ...and stdout starts with a RECOGNIZED verdict word",
          _out.split(maxsplit=1)[0] in
          ("OK", "UNSUPPORTED_TYPE", "UNSUPPORTED_VALUE", "TIMEOUT", "SETUP_FAILED"),
          f"-> stdout={_out!r}")

    print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
          "\n=== MEASURED-SAFE-CALLABLES SHARD CLEAN ===")
    sys.exit(1 if FAILS else 0)
