"""Regressions for the bug sweep of 2026-08-08.

Each block names the wrong behaviour it locks out. They are grouped by the shape
of the defect rather than by module, because the same shape kept recurring:
most of these are a failure, or an absence, encoded as a valid-looking result.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import re
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import exact, executor, logic, mcp_middleware, optimization, tools, units

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

# ═══ #40: radix_convert generated digits from the wrong denominator ═════════
# rem/den was reduced to rn/rd to decide whether the expansion TERMINATES, but
# digits were then generated from rn divided by the UNREDUCED den — the
# expansion of rn/den, not of the input. Silent whenever rem/den was already
# in lowest terms (e.g. 0.1 -> base 2), which is why it went unnoticed.
for value, from_b, to_b, want in [
    ("0.5", 10, 2, "0.1"), ("0.25", 10, 2, "0.01"), ("0.75", 10, 2, "0.11"),
    ("0.125", 10, 2, "0.001"), ("0.2", 10, 5, "0.1"),
]:
    r = exact.radix_convert(value, from_b, to_b)
    check(f"radix_convert({value!r}, {from_b}, {to_b}) == {want!r}",
          r.get("value") == want, f"-> {r.get('value')}")
    check("  ...and terminates", r.get("non_terminating") is False)
# the one case that was already correct (rem/den already in lowest terms)
# must stay correct.
r = exact.radix_convert("0.1", 10, 2)
check("radix_convert('0.1', 10, 2) is still non-terminating",
      r.get("non_terminating") is True and r.get("value") == "0.00011…",
      f"-> {r.get('value')}")

# ═══ #33: bitop masked the shift COUNT by width instead of the value ════════
# bitop(a, "shl", b, width) masked b with the width mask before dispatch, so a
# shift count of 256 at width 8 became 256 & 0xff == 0 — "shift far past the
# width" silently became "don't shift at all".
r = exact.bitop(1, "shl", 256, 8)
check("bitop shl: an unmasked-huge count still overflows to 0",
      r.get("unsigned") == 0 and "overflow" in r, f"-> {r}")
r = exact.bitop(128, "shr", 256, 8)
check("bitop shr: an unmasked-huge count still zeroes out", r.get("unsigned") == 0, f"-> {r}")
r = exact.bitop(1, "shl", -1, 32)
check("bitop shl: a negative count is rejected, not reported as a huge shift",
      r.get("ok") is False and "negative" in (r.get("error") or ""), f"-> {r}")
# value operands (not shift counts) must still be masked to width.
r = exact.bitop(0xFF, "and", 0x1FF, 8)
check("bitop and: the VALUE operand is still masked to width", r.get("unsigned") == 0xFF, f"-> {r}")

# ═══ #35: float_repr(-0.0) reported NaN neighbours and a NaN ulp ════════════
# Neighbours were derived by incrementing/decrementing the raw bit pattern
# with no special case for the two zero encodings; decrementing -0.0's
# pattern (0x8000000000000000) underflowed into a NaN encoding.
r = exact.float_repr(-0.0)
check("float_repr(-0.0): prev is negative, not NaN and not GREATER than the input",
      r.get("prev") == -5e-324, f"-> prev={r.get('prev')}")
check("float_repr(-0.0): next is not NaN", r.get("next") == 5e-324, f"-> next={r.get('next')}")
check("float_repr(-0.0): ulp is not NaN", r.get("ulp") == 5e-324, f"-> ulp={r.get('ulp')}")
r = exact.float_repr(0.0)
# +0.0's `prev` DID change with this fix, from -0.0 to -5e-324, and the
# assertion below is the new value. Recorded rather than glossed: the old code
# special-cased it deliberately, on the reasoning that the adjacent bit PATTERN
# below +0.0 is -0.0. That is true of the encoding and false of the value, and
# mixing the two is what produced the -0.0 bug in the first place. `prev`/`next`/
# `ulp` are value fields; `bits_hex`/`stored` are the encoding fields and still
# report the pattern. math.nextafter answers the value question for every input
# including both zeros, so it is now the only source for the three value fields.
check("float_repr(0.0): prev is now -5e-324, NOT -0.0 (deliberate, see above)",
      r.get("prev") == -5e-324 and r.get("next") == 5e-324, f"-> {r}")
# non-finite input to percentiles must not escape as a bare NaN (not valid
# JSON) or be silently classified as a real percentile.
r = exact.percentiles([float("nan"), 1])
check("percentiles rejects a non-finite input with a structured error",
      r.get("ok") is False and "nums[0]" in (r.get("error") or ""), f"-> {r}")

# ═══ #36: non-finite input raised uncaught out of stats/human_duration ══════
r = exact.stats([1, float("nan")])
check("stats rejects a NaN element, naming its index",
      r.get("ok") is False and "nums[1]" in (r.get("error") or ""), f"-> {r}")
r = exact.stats([1, float("inf")])
check("stats rejects an inf element, naming its index",
      r.get("ok") is False and "nums[1]" in (r.get("error") or ""), f"-> {r}")
r = exact.human_duration(float("nan"))
check("human_duration rejects a NaN duration instead of raising",
      r.get("ok") is False, f"-> {r}")
# ...and ordinary finite input is unaffected by the new screening.
check("stats still computes on finite input", exact.stats([1, 2, 3]).get("ok") is True)
check("human_duration still computes on finite input",
      exact.human_duration(90061).get("ok") is True)

# truth_table: a RecursionError from pathologically nested parens must come
# back as a structured error, not crash the tool. 1999 chars is under
# _MAX_EXPR_LEN (2000), so the length guard never fires on this input.
r = logic.truth_table("(" * 999 + "a" + ")" * 999)
check("truth_table survives 999 levels of nesting without raising",
      r.get("ok") is False and "nest" in (r.get("error") or ""), f"-> {r}")
check("truth_table still parses ordinary nesting", logic.truth_table("((a))").get("ok") is True)

# ═══ #34: implies must be RIGHT-associative ══════════════════════════════════
# `a implies b implies a` under the conventional right-associative reading is
# `a -> (b -> a)`, a tautology. The left-associative parse taken before this
# fix built `(a -> b) -> a` (Peirce's formula), which is NOT a tautology.
r = logic.truth_table("a implies b implies a")
check("a implies b implies a is a tautology (right-associative)",
      r.get("tautology") is True, f"-> {r}")
# _parse_iff is genuinely associative (a iff b iff c has one true reading
# either way) and must be untouched.
r = logic.truth_table("a iff b iff c")
check("iff is still associative (unchanged)", r.get("ok") is True)

# ═══ #32: exponentiation has no bound, and a huge result leaks ValueError ═══
t0 = time.monotonic()
r = exact.eval_exact("2**10**6")
check("2**10**6 is rejected instead of taking seconds to compute",
      r.get("ok") is False, f"-> {r}")
check("  ...and rejected fast", time.monotonic() - t0 < 1.0)
t0 = time.monotonic()
r = exact.eval_exact("2**10**7")
check("2**10**7 is rejected instead of hanging",
      r.get("ok") is False, f"-> {r}")
check("  ...and rejected fast", time.monotonic() - t0 < 1.0)
r = exact.eval_exact("2**100000")
check("2**100000 returns a structured error instead of raising ValueError",
      r.get("ok") is False, f"-> {r}")
check("  ...still an ordinary power computes", exact.eval_exact("2**64")["value"] == str(2 ** 64))
# an expression over the DoS length cap is rejected before parsing.
r = exact.eval_exact("1+" * 1001 + "1")
check("eval_exact rejects an over-length expression",
      r.get("ok") is False and "too long" in (r.get("error") or ""), f"-> {r.get('error')}")
# calc_exact and its sympy-backed siblings must carry the same 20s deadline
# as their logic.py counterparts (evaluate_expression, solve_linear,
# analyze_complexity) instead of silently inheriting the 900s default.
for name in ("calc_exact", "algebraic_equiv", "solve_expression",
             "limit_expression", "simplify_expression"):
    check(f"{name} has a bounded TOOL_TIMEOUTS entry",
          mcp_middleware.TOOL_TIMEOUTS.get(name) == 20,
          f"-> {mcp_middleware.TOOL_TIMEOUTS.get(name)}")

# ═══ issue #24: execute()'s rust path never reported which backend answered ═
# Measured before this fix: `sorted(executor.execute("python3", "print(1)"))`
# on the rust path carried no "backend" key at all, while the python fallback
# already had `"backend": "python"` — so an absent key was indistinguishable
# from an older build that never added the field. Fixed at the one place both
# backends' results pass through a caller: where execute()'s rust branch
# parses the binary's own JSON (codecalc/executor.py). The binary's own
# contract (scripts/contract_check.py) is untouched — it still emits no
# "backend" key, by design.
#
# Compared against executor.backend() rather than hardcoded, because CI runs
# this file both with the rust binary built (backend()=="rust") and without
# it (backend()=="python", e.g. the plain OS/version matrix job that never
# builds bin/codecalc-exec) — a literal "rust" here would fail every run of
# the second kind for a reason that has nothing to do with this fix.
_live_backend = executor.backend()
_r = executor.execute("python3", "print(1)")
check(f"execute() backend field is present ({_live_backend!r} on this machine)",
      "backend" in _r, f"-> keys={sorted(_r)}")
if _live_backend == "rust":
    check("rust path: backend is the literal string 'rust', not merely present",
          _r.get("backend") == "rust", f"-> {_r.get('backend')!r}")
else:
    check("python fallback: backend is the literal string 'python'",
          _r.get("backend") == "python", f"-> {_r.get('backend')!r}")

# ═══ issue #24: CODECALC_REQUIRE_NATIVE must fail closed, not downgrade silently ═
# Measured before this fix: setting CODECALC_REQUIRE_NATIVE=1 with no working
# rust binary changed nothing observable — import succeeded, executor.backend()
# still said "python", and execute() still ran normally on the fallback whose
# own `unenforced` field admits it cannot apply no_net. codecalc/executor.py
# now runs _require_native_or_die() once, at import time (right after `_rust`
# is resolved), which raises RuntimeError naming the variable instead.
#
# Exercised by calling _require_native_or_die() directly against controlled
# `executor._rust` values rather than by re-importing the module (a second
# `import codecalc.executor` would hit sys.modules and never re-run the
# module-level check) or by spawning a subprocess (whose result would depend
# on whether THIS machine happens to already have bin/codecalc-exec built,
# which is exactly the kind of environment-dependent flake this suite avoids
# elsewhere). The manual, real-import demonstration of both branches — a
# fresh interpreter, CODECALC_REQUIRE_NATIVE=1, with and without bin/codecalc-exec
# present — is in this change's commit message.
_orig_rust = executor._rust
_orig_env = os.environ.get("CODECALC_REQUIRE_NATIVE")
try:
    os.environ.pop("CODECALC_REQUIRE_NATIVE", None)
    executor._rust = None
    try:
        executor._require_native_or_die()
        check("CODECALC_REQUIRE_NATIVE unset + no binary: does not raise", True)
    except RuntimeError as exc:
        check("CODECALC_REQUIRE_NATIVE unset + no binary: does not raise", False,
              f"-> raised {exc}")

    os.environ["CODECALC_REQUIRE_NATIVE"] = "1"
    executor._rust = "/fake/codecalc-exec"  # any truthy value stands in for "found"
    try:
        executor._require_native_or_die()
        check("CODECALC_REQUIRE_NATIVE=1 + binary present: does not raise", True)
    except RuntimeError as exc:
        check("CODECALC_REQUIRE_NATIVE=1 + binary present: does not raise", False,
              f"-> raised {exc}")

    executor._rust = None
    try:
        executor._require_native_or_die()
        check("CODECALC_REQUIRE_NATIVE=1 + no binary: raises RuntimeError", False,
              "-> did not raise")
    except RuntimeError as exc:
        check("CODECALC_REQUIRE_NATIVE=1 + no binary: raises RuntimeError", True)
        check("  ...and the message names CODECALC_REQUIRE_NATIVE",
              "CODECALC_REQUIRE_NATIVE" in str(exc), f"-> {exc}")
finally:
    executor._rust = _orig_rust
    if _orig_env is None:
        os.environ.pop("CODECALC_REQUIRE_NATIVE", None)
    else:
        os.environ["CODECALC_REQUIRE_NATIVE"] = _orig_env

# ...and the check is actually WIRED at import time, not just defined and
# never called — the same "defined but dead" gap scripts/check_parity.py
# already guards against for the Rust/Python identity-checked deletion pair.
# (`inspect` was already imported above, for tools._measure's source check.)
_executor_src = inspect.getsource(executor)
check("_require_native_or_die() is called at module scope (import time)",
      re.search(r"^_require_native_or_die\(\)", _executor_src, re.M) is not None)

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== ALL BUG-SWEEP REGRESSIONS FIXED ===")
sys.exit(1 if FAILS else 0)
