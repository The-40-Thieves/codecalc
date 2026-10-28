"""Same-scale temperature conversions are identities (issue #350).

`convert_units(25, "celsius", "celsius")` used to fall through
`units._temp_convert`'s six cross-scale branches and come back as
"incompatible temperature scales". This file drives the public `convert_units`
tool in codecalc/server.py and asserts, for every alias the normaliser already
accepts:

  * every pair of temperature aliases that resolve to the same canonical scale
    (identical strings, mixed case, surrounding whitespace included) returns
    the value unchanged, in the usual result shape;
  * the six cross-scale anchors still hold;
  * temperature <-> non-temperature conversions are still rejected.

The alias list is read from `units._UNITS` (flagged `is_temperature`), not
hand-copied, so a new temperature alias is covered without editing this file.

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention
(see CONTRIBUTING.md and tests/conftest.py for why).
"""

from __future__ import annotations

import pathlib
import sys
from collections import defaultdict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import server, units

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# canonical scale -> every alias that normalises to it
SCALES: dict[str, list[str]] = defaultdict(list)
for alias, (canonical, is_temp) in units._UNITS.items():
    if is_temp:
        SCALES[canonical].append(alias)

check("temperature aliases discovered for all three scales",
      sorted(SCALES) == ["celsius", "fahrenheit", "kelvin"], f"-> {sorted(SCALES)}")

# Identity: every ordered pair within a scale, in three spellings of each side.
VALUES = (0, 25, -40)
pairs = 0
for aliases in SCALES.values():
    for src in aliases:
        for dst in aliases:
            for src_form, dst_form in (
                (src, dst),
                (src.upper(), dst.upper()),
                (f"  {src} ", f"\t{dst}  "),
            ):
                for v in VALUES:
                    pairs += 1
                    r = server.convert_units(v, src_form, dst_form)
                    ok = (r.get("ok") is True and r.get("value") == float(v)
                          and r.get("unit") == dst_form.strip()
                          and r.get("from") == f"{float(v)} {src_form.strip()}")
                    if not ok:
                        check(f"identity {src_form!r} -> {dst_form!r} @ {v}", False, f"-> {r}")
check(f"same-scale identity holds for every alias pair ({pairs} calls)", not any(
    f.startswith("identity ") for f in FAILS), f"-> {pairs} pairs checked")

# Result shape for the reported case.
r = server.convert_units(25, "celsius", "celsius")
shape = {k: r.get(k) for k in ("ok", "value", "unit", "from")}
check("convert_units(25, celsius, celsius) result shape",
      shape == {"ok": True, "value": 25.0, "unit": "celsius", "from": "25.0 celsius"},
      f"-> {shape}")

# Cross-scale anchors must still hold.
ANCHORS = [
    (0, "celsius", "fahrenheit", 32),
    (0, "celsius", "kelvin", 273.15),
    (-40, "celsius", "fahrenheit", -40),
    (-40, "fahrenheit", "celsius", -40),
    (273.15, "kelvin", "celsius", 0),
    (32, "fahrenheit", "kelvin", 273.15),
    (100, "celsius", "fahrenheit", 212),
    (300, "kelvin", "fahrenheit", 80.33),
]
for v, src, dst, want in ANCHORS:
    r = server.convert_units(v, src, dst)
    check(f"anchor {v} {src} -> {dst} = {want}",
          r.get("ok") is True and abs(r["value"] - want) < 1e-6, f"-> {r}")

# Temperature <-> non-temperature stays rejected, both directions.
for v, src, dst in [(25, "celsius", "meter"), (25, "meter", "k"),
                    (1, "kelvin", "kg"), (25, "fahrenheit", "liter")]:
    r = server.convert_units(v, src, dst)
    check(f"reject mixing {src} -> {dst}",
          r.get("ok") is False and "cannot mix temperature" in r.get("error", ""), f"-> {r}")

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL TEMPERATURE IDENTITY TESTS PASS ===")
sys.exit(1 if FAILS else 0)
