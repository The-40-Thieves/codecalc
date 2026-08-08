"""Regressions for the bug sweep of 2026-08-08.

Each block names the wrong behaviour it locks out. They are grouped by the shape
of the defect rather than by module, because the same shape kept recurring:
most of these are a failure, or an absence, encoded as a valid-looking result.
"""

from __future__ import annotations

import json
import math
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import exact, optimization, tools, units

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ═══ exactness that was silently lost ═══════════════════════════════════════
# abs/min/max/int are exact over rationals, but every call went through float:
# abs(-1/3) returned 3333333333333333/10000000000000000. The loss was
# data-dependent — max(1/3,1/2) came back as exactly 1/2 because 1/2 is
# binary-representable — so it looked absent about half the time.
for expr, want in [("abs(-1/3)", "1/3"), ("min(1/3, 1/2)", "1/3"),
                   ("max(1/3, 1/2)", "1/2"), ("int(7/2)", "3"),
                   ("abs(-2/7)", "2/7")]:
    r = exact.eval_exact(expr)
    check(f"exact: {expr} = {want}", r.get("value") == want, f"-> {r.get('value')}")

# ═══ "exact" must mean exact ════════════════════════════════════════════════
# pi/e/tau are irrational; the value used is the rational form of their binary64
# approximation. It was returned with exact=True.
for expr in ("pi", "2*pi", "e", "tau"):
    r = exact.eval_exact(expr)
    check(f"{expr} is not claimed exact", r.get("exact") is False,
          f"-> exact={r.get('exact')}")
    check(f"{expr} says what was approximated", bool(r.get("approximated")))

# sqrt has no exact rational form either — but sqrt(144)=12 IS the true answer.
check("sqrt(2) is not claimed exact", exact.eval_exact("sqrt(2)")["exact"] is False)
check("sqrt(144) IS exact (integer result)", exact.eval_exact("sqrt(144)")["exact"] is True)
check("1/3 is still exact", exact.eval_exact("1/3")["exact"] is True)

# ═══ negative numbers fit no unsigned width ═════════════════════════════════
# The test was `n <= uhi`, true for every negative n, so int_widths(-200)
# claimed u8 and int_widths(-2**70) claimed u8/u16/u32/u64.
for n in (-1, -200, -(2 ** 70)):
    fits = exact.int_widths(n)["fits"]
    check(f"int_widths({n}): no unsigned width",
          not any(f.startswith("u") for f in fits), f"-> {fits}")
check("int_widths(200) still fits u8", "u8" in exact.int_widths(200)["fits"])
check("int_widths(-200) still fits i16", "i16" in exact.int_widths(-200)["fits"])
check("int_widths(-2**70) fits nothing", exact.int_widths(-(2 ** 70))["fits"] == [])

# ═══ crashes on ordinary inputs ═════════════════════════════════════════════
# float_repr(0.0) raised struct.error: prev_bits was -1. Zero is the likeliest
# input to a float-inspection tool.
for v in (0.0, -0.0, 5e-324, 1.7976931348623157e308, 0.1):
    try:
        r = exact.float_repr(v)
        ok = r.get("ok") is True
    except Exception as exc:
        ok = False
        r = {"error": f"{type(exc).__name__}: {exc}"}
    check(f"float_repr({v!r}) does not raise", ok, f"-> {str(r)[:60]}")

# ═══ a dead feature ═════════════════════════════════════════════════════════
# The guard was `v > 10**17`; a real nanosecond timestamp is ~1.8e18, so every
# ns value was rejected and the advertised ns support was unreachable.
ns = time.time_ns()
r = exact.epoch_time(str(ns))
check("epoch_time accepts nanoseconds", r.get("ok") is True, f"-> {r.get('error')}")
check("epoch_time reads ns as the current year",
      "nanos" in (r.get("interpretations") or {}) and
      str(time.gmtime().tm_year) in r["interpretations"]["nanos"],
      f"-> {list(r.get('interpretations') or {})}")
# ...and must not offer a 1970 reading alongside the real one.
r = exact.epoch_time(str(ns // 1000))
# startswith, not `"1970" in v`. The year is the first four characters of an
# ISO timestamp; a substring search matches the MICROSECONDS too, so
# `2026-08-08T12:09:47.331970+00:00` counted as a 1970 reading. About one run in
# a thousand — reproduced at 1 in 4000 samples — which is exactly long enough
# for the failure to look like an unrelated flake rather than an assertion that
# checks something other than what it names.
check("epoch_time suppresses 1970 wrong-unit readings",
      all(not v.startswith("1970") for v in r["interpretations"].values()),
      f"-> {r['interpretations']}")

# ═══ NaN is not a measurement, and is not JSON ══════════════════════════════
r = exact.stats([-1.0, 1.0])          # mean is 0 -> CV undefined
check("stats: undefined CV is None, not NaN", r["cv"] is None, f"-> {r['cv']!r}")
check("stats: undefined CV is not called 'within budget'",
      "undefined" in r["cv_note"], f"-> {r['cv_note']}")
check("stats output is valid JSON", "NaN" not in json.dumps(r))
r = exact.stats([10.0, 10.5, 9.5])
check("stats: a real CV still computes", isinstance(r["cv"], float) and math.isfinite(r["cv"]))

# ═══ 'fastest' must mean fastest SUCCESSFUL ═════════════════════════════════
fake = [{"language": "crashed", "ok": False, "duration_ms": 5},
        {"language": "worked", "ok": True, "duration_ms": 300},
        {"language": "instant", "ok": True, "duration_ms": 0}]
ok_runs = [r for r in fake if r["ok"] and r["duration_ms"] is not None]
winner = min(ok_runs, key=lambda x: x["duration_ms"])["language"]
check("compare_execution: a failed run cannot win", winner != "crashed")
check("compare_execution: a 0ms run is not treated as slowest", winner == "instant",
      f"-> {winner}")

# ═══ a timeout is not a timing ══════════════════════════════════════════════
import inspect

src = inspect.getsource(tools._measure)
check("benchmark: a timed-out run aborts instead of being recorded",
      'if r.get("timed_out")' in src and "no growth estimate" in
      inspect.getsource(tools._measure))

# ═══ speedup: division by zero, and misaligned sizes ════════════════════════
before = {"sizes": [100, 200, 400], "durations_ms": [50.0, 100.0, 200.0]}
after = {"sizes": [100, 200, 400], "durations_ms": [0, 10.0, 20.0]}
try:
    r = optimization._speedup(before, after)
    ok = True
except ZeroDivisionError:
    ok, r = False, {}
check("_speedup: a 0ms optimized run does not raise", ok)

before = {"sizes": [100, 200, 400, 800], "durations_ms": [0.5, 100.0, 200.0, 400.0]}
after = {"sizes": [100, 200, 400, 800], "durations_ms": [0.4, 50.0, 100.0, 200.0]}
r = optimization._speedup(before, after)
rows = {row["n"]: row["before_ms"] for row in r["per_size"]}
check("_speedup: per_size keeps n aligned with its measurement",
      rows.get(200) == 100.0 and 100 not in rows, f"-> {rows}")

# ═══ documented constant names must resolve ═════════════════════════════════
# The README advertises "c, h, N_A, k_B, G, g, m_e, R" and NOT ONE resolved.
for sym in ("c", "h", "N_A", "k_B", "G", "g", "m_e", "R"):
    r = units.constants(sym)
    check(f"constant {sym!r} resolves", r.get("ok") is True, f"-> {r.get('error')}")
# G and g are different constants; case must survive the lookup.
check("G is the gravitational constant, not little-g",
      units.constants("G")["name"] == "gravitational_constant")
check("g is little-g", units.constants("g")["name"] == "gravity")

# ═══ unit parser: numbers it tokenises, units only ══════════════════════════
check("unit parser accepts scientific notation it tokenises",
      units._parse_unit("1e3*meter") is not None)
for nm in ("convert_to", "Quantity"):
    try:
        units._parse_unit(nm)
        rejected = False
    except ValueError:
        rejected = True
    check(f"unit parser rejects non-unit attribute {nm!r}", rejected)
check("unit conversion still correct (1 km -> 1000 m)",
      units.convert(1, "km", "m")["value"] == 1000.0)

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== ALL BUG-SWEEP REGRESSIONS FIXED ===")
sys.exit(1 if FAILS else 0)
