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

import math
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

# --- #349: the numeric value keeps full precision (no 6-dp rounding, no
# 10-significant-digit cap). Relative tolerance only: abs_tol=0, so a zero
# result can never pass as "close to 1e-9".
def close(got, want, rel=1e-12):
    return isinstance(got, float) and math.isclose(got, want, rel_tol=rel, abs_tol=0)


r = server.convert_units(1, "nm", "m")
check("349 1 nm -> m is ~1e-9, not 0", r.get("ok") is True and close(r.get("value"), 1e-9), f"-> {r}")
r = server.convert_units(1, "byte", "gb")
check("349 1 byte -> gb is ~1e-9, not 0", r.get("ok") is True and close(r.get("value"), 1e-9), f"-> {r}")

for v, unit in [(0.123456789012345, "m"), (1234567890123, "m")]:
    r = server.convert_units(v, unit, unit)
    check(f"349 identity {v} {unit} -> {unit} is exact", r.get("ok") is True and r.get("value") == v, f"-> {r}")

for v, a, b in [(1, "nm", "m"), (7.5, "nm", "m"), (1, "byte", "gb"), (12345, "byte", "gb")]:
    fwd = server.convert_units(v, a, b)
    back = server.convert_units(fwd.get("value", 0.0), b, a)
    check(f"349 round trip {a}->{b}->{a} of {v} within rel 1e-15",
          fwd.get("ok") is True and back.get("ok") is True and close(back.get("value"), float(v), rel=1e-15),
          f"-> {fwd} {back}")

r = server.convert_units(2.5, "km", "m")
check("349 keep km -> m (2.5 km = 2500 m)", r.get("ok") is True and close(r.get("value"), 2500.0), f"-> {r}")
r = server.convert_units(60, "mph", "km/h")
check("349 keep mph -> km/h (60 mph = 96.56064 km/h)", r.get("ok") is True and close(r.get("value"), 96.56064), f"-> {r}")

# Temperature: exact anchors, no 6-dp rounding on the offset arithmetic.
for v, src, dst, want in [(0, "celsius", "kelvin", 273.15),
                          (0, "celsius", "fahrenheit", 32.0),
                          (-40, "celsius", "fahrenheit", -40.0),
                          (-40, "fahrenheit", "celsius", -40.0),
                          (0.1, "celsius", "kelvin", 273.25),
                          (0.123456789, "celsius", "kelvin", 273.273456789)]:
    r = server.convert_units(v, src, dst)
    check(f"349 temperature {v} {src} -> {dst} == {want} exactly",
          r.get("ok") is True and r.get("value") == want, f"-> {r}")

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL TEMPERATURE IDENTITY TESTS PASS ===")
sys.exit(1 if FAILS else 0)
