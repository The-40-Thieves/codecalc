"""THE-1095 round 14 (coordinator review of 7630e87, item 1): re-runs
`scripts/measure_safe_callables.py`'s own classification for a SAMPLE of
30 already-allowlisted names on the RUNNING box, and asserts none
exceeds its own `FAST_MS` threshold at the shapes it was originally
measured accepting.

`codecalc/_measured_safe_callables.json` (the committed measurement
record `codecalc.safe_expr._MEASURED_SAFE_CALLABLES` loads from) was
generated on ONE box, at ONE point in time — this is the guard against
that record going stale (a slower CI runner, a SymPy version bump that
changed one name's own cost) rather than being trusted forever: every
run of the full test suite re-measures a sample and fails loudly if any
allowlisted name no longer measures fast, rather than silently letting
`_measured_safe_argument_cap_violation`'s own generic cap be the only
thing standing between a caller and a now-slow "safe" callable.
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
    from measure_safe_callables import DATA_PATH, measure_name

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

        import random

        sample = random.Random(0).sample(allowlist, min(30, len(allowlist)))  # noqa: S311 -- deterministic, not crypto
        for name in sample:
            result = measure_name(name)
            check(f"re-measured: {name!r} still allowlist-fast on this box",
                  result["allowlisted"],
                  f"-> {result['shapes']}")

    print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
          "\n=== MEASURED-SAFE-CALLABLES SAMPLE CLEAN ===")
    sys.exit(1 if FAILS else 0)
