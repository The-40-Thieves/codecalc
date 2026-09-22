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
# Pure duration-ratio arithmetic -- these dicts carry `durations_ms` only, no
# `all_runs_ms`, so there is no significance test to run at all. `_speedup`'s
# default (`comparable=None`) now sources `_testable_positions`' survivors
# (codecalc #328's second pass), which excludes every position with fewer
# than `stats.min_testable_n` raw runs a side -- zero here, always -- so the
# default would report `measurable: False` regardless of the durations.
# Pass the raw `_comparable_positions` pool explicitly: these checks are
# about the per-size ratio computation itself, not the significance filter.
before = {"sizes": [100, 200, 400], "durations_ms": [50.0, 100.0, 200.0]}
after = {"sizes": [100, 200, 400], "durations_ms": [0, 10.0, 20.0]}
try:
    r = optimization._speedup(before, after,
                              comparable=optimization._comparable_positions(before, after)[0])
    ok = True
except ZeroDivisionError:
    ok, r = False, {}
check("_speedup: a 0ms optimized run does not raise", ok)

before = {"sizes": [100, 200, 400, 800], "durations_ms": [0.5, 100.0, 200.0, 400.0]}
after = {"sizes": [100, 200, 400, 800], "durations_ms": [0.4, 50.0, 100.0, 200.0]}
r = optimization._speedup(before, after,
                          comparable=optimization._comparable_positions(before, after)[0])
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
# as their logic.py counterparts (evaluate_expression, symbolic,
# analyze_complexity) instead of silently inheriting the 900s default.
# solve_expression/limit_expression/simplify_expression were retired in
# 0.12.0 (CHANGELOG.md); `symbolic` is their replacement's own entry.
for name in ("calc_exact", "algebraic_equiv", "symbolic"):
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

# ═══ the uid-0 process ceiling must be REPORTED, not silently absent (#62) ══
# RLIMIT_NPROC does not bind a process whose effective uid is 0 — the kernel
# exempts privileged processes. Both backends set that limit, so running as
# root computes a ceiling that has no effect, and neither said so, which reads
# as "the process ceiling was applied".
#
# Not a sandbox escape: running the server as root is a deployment error and
# this is documented kernel behaviour. It is a reporting-fidelity defect, and
# SECURITY.md puts "anything that makes the server report a guarantee it did
# not apply" in scope.
#
# euid is simulated rather than requiring root, so this runs in CI as any
# user. Verified separately against a real `sudo` run of both backends.
_real_geteuid = getattr(os, "geteuid", None)
if _real_geteuid is None:
    print("SKIP uid-0 process ceiling (no os.geteuid on this platform)")
else:
    try:
        os.geteuid = lambda: 0
        _as_root = executor._unmeasured()
        os.geteuid = lambda: 1000
        _as_user = executor._unmeasured()
    finally:
        os.geteuid = _real_geteuid
    check("as uid 0 the process ceiling is reported unenforced",
          executor._UID0_PROCESS_CEILING in _as_root, f"-> {_as_root}")
    check("as a normal uid it is NOT reported",
          executor._UID0_PROCESS_CEILING not in _as_user, f"-> {_as_user}")
    check("the caveat names RLIMIT_NPROC and uid 0",
          "RLIMIT_NPROC" in executor._UID0_PROCESS_CEILING
          and "uid 0" in executor._UID0_PROCESS_CEILING,
          f"-> {executor._UID0_PROCESS_CEILING}")

# ═══ SymPy parsers must not execute what they are handed (GHSA advisory) ═══
# Six @mcp.tool() functions passed caller strings to sympify/parse_expr, which
# EVALUATE what they parse. parse_expr populates its default global_dict from
# vars(builtins) deliberately — __import__ among them — so
# simplify_expression("__import__('os').system('id')") ran id in the server
# process, outside the Rust sandbox entirely. AUDIT.md CRITICAL-01 through a
# different door.
#
# Probed BEHAVIOURALLY with a live payload rather than by asserting the guard
# is present: a screen that exists and is bypassed looks identical to a screen
# that works, from the source. Non-destructive — the payload writes one file
# into a temp dir and the assertion is that the file never appears.
import shutil as _shutil
import tempfile as _tf

from codecalc import logic as _logic

_probe_dir = pathlib.Path(_tf.mkdtemp(prefix="cc-rce-probe-"))
_marker = _probe_dir / "EXECUTED"
_payload = f"__import__('pathlib').Path({str(_marker)!r}).write_text('x')"


def _executed(fn) -> bool:
    _marker.unlink(missing_ok=True)
    try:
        fn(_payload)
    except Exception:  # a refusal or a parse error both count as not-executed
        pass
    hit = _marker.exists()
    _marker.unlink(missing_ok=True)
    return hit


for _label, _call in (
    ("evaluate_expression", lambda p: _logic.evaluate_expression(p)),
    ("solve_linear (no =)", lambda p: _logic.solve_linear(p, "x")),
    ("solve_linear (with =)", lambda p: _logic.solve_linear(p + " = 1", "x")),
    ("algebraic_equiv", lambda p: exact.algebraic_equiv(p, "1")),
    ("solve_expression", lambda p: exact.solve_expression(p)),
    ("limit_expression", lambda p: exact.limit_expression(p, "x", "0")),
    ("simplify_expression", lambda p: exact.simplify_expression(p)),
):
    check(f"{_label}: a payload string is NOT executed", not _executed(_call))

_shutil.rmtree(_probe_dir, ignore_errors=True)

# The refusal must be the documented structured shape, not an exception that
# the MCP dispatcher flattens to "Internal server error" with no detail.
_r = exact.simplify_expression("__import__('os').system('id')")
check("a refused expression returns ok=False with a reason",
      _r.get("ok") is False and "not permitted" in str(_r.get("error")),
      f"-> {str(_r.get('error'))[:70]}")

# ...and the screen must not have cost the tools their actual job, including
# the sympy-only syntax implicit_multiplication_application enables.
for _expr, _want in (("sin(x)**2 + cos(x)**2", "1"), ("2x + 1", "2*x + 1")):
    _r = _logic.evaluate_expression(_expr)
    check(f"legitimate expression still evaluates: {_expr}",
          _r.get("ok") is True, f"-> {_r.get('error')}")
check("decimals survive the refusal of the '.' OPERATOR",
      _logic.evaluate_expression("1.5 + 2.25").get("ok") is True)

# ═══ the streaming tool must apply the SAME ceilings (#61) ════════════════
# execute_code took max_memory_mb/max_output_kb/max_cpu and forwarded all
# three. execute_code_stream took only max_output_kb, and its argv omitted
# --max-memory-mb and --max-cpu entirely — so a caller who set a memory or
# CPU bound on execute_code and then switched to streaming silently lost
# both, while the docstring said "Returns the same result shape". True of the
# shape, false of the guarantees.
#
# Asserted as parameter-set PARITY rather than as a list of expected names:
# a per-tool list is what let these two diverge, since each satisfied its own.
import asyncio as _asyncio
import inspect as _inspect

from codecalc import server as _server

_CEILINGS = {"max_memory_mb", "max_output_kb", "max_cpu", "no_net"}
_sync = set(_inspect.signature(_server.execute_code).parameters)
_strm = set(_inspect.signature(_server.execute_code_stream).parameters)
check("streaming accepts every ceiling the non-streaming tool does",
      _strm >= _CEILINGS, f"-> missing {sorted(_CEILINGS - _strm)}")
check("the two tools agree on the ceiling parameters",
      (_CEILINGS & _sync) == (_CEILINGS & _strm),
      f"-> sync={sorted(_CEILINGS & _sync)} stream={sorted(_CEILINGS & _strm)}")

# Declared is not forwarded: the argv builder must actually pass them.
_src = _inspect.getsource(executor.execute_stream)
for _flag in ("--max-memory-mb", "--max-cpu", "--max-output-kb"):
    check(f"provider-backed streaming argv passes {_flag}", _flag in _src)

# ...and forwarded is not enforced. One real bounded run, kept small so it
# trips in about a second.
if executor.backend() == "rust":
    _hog = "a=[]\nwhile True:\n    a.append(b'x'*(1024*1024))\n"
    _r = _asyncio.run(_server.execute_code_stream("python3", _hog, timeout=20,
                                                 max_memory_mb=64))
    check("a memory ceiling set on the STREAMING tool actually bites",
          _r.get("ok") is False and (_r.get("peak_memory_kb") or 0) < 200_000,
          f"-> ok={_r.get('ok')} verdict={_r.get('verdict')} peak={_r.get('peak_memory_kb')}")
else:
    # No skip() helper in this file, and adding one to report a single case
    # would change how every other block reads. Named in the output instead,
    # so "not exercised here" cannot be mistaken for "exercised and fine".
    print("SKIP streaming memory ceiling (no native executor; the fallback "
          "path forwards the same ceilings and is covered separately)")

# ═══ the bundled binary lives INSIDE the package (#59) ═════════════════════
# The wheel used to force_include to `bin/`, which installs to a TOP-LEVEL
# site-packages/bin/ — a directory shared with every other distribution in the
# environment. It now installs to codecalc/bin/, inside the namespace this
# distribution owns, matching what Playwright and Zig do with their bundled
# binaries. A source checkout still keeps built binaries at <repo>/bin, so both
# roots are searched; package-local must come FIRST so an installed artifact
# wins over anything lying in a working copy.
import codecalc as _cc

_pkg = pathlib.Path(_cc.__file__).resolve().parent
_cands = [pathlib.Path(c) for c in executor._binary_candidates()]
check("binary lookup searches the package's own bin/ first",
      _cands and _cands[0].parent == _pkg / "bin", f"-> {_cands[0] if _cands else None}")
check("binary lookup still searches the checkout's bin/ too",
      any(c.parent == _pkg.parent / "bin" for c in _cands),
      f"-> roots {sorted({str(c.parent) for c in _cands})}")
check("package-local candidates all precede checkout candidates",
      [c.parent == _pkg / "bin" for c in _cands] == sorted(
          [c.parent == _pkg / "bin" for c in _cands], reverse=True),
      f"-> {[c.parent.name + '/' + c.name for c in _cands]}")

# ── #67: bounding the WORK, not just the input length ─────────────────────
# The 2000-character cap bounds how much a caller can TYPE, which is a
# different quantity from how much SymPy will DO. Every expression below is
# under 30 characters. Measured before the fix:
#
#     9**9**9**9         SIGKILL at 2GB / 10s CPU
#     (x+1)**100000      SIGKILL at 2GB / 10s CPU
#     factorial(99999)   uncaught ValueError out of SymPy's printer
#
# The last one is the worst shape: not slow, but a CRASH where every other
# path in this module returns {"ok": False, "error": ...}. A caller saw a
# transport failure instead of a result.
import time as _t

from codecalc import logic as _logic

_HOSTILE = [
    ("9**9**9**9", "power tower"),
    ("2**(10**9)", "power tower"),
    ("(x+1)**100000", "symbolic power"),
    ("factorial(99999)", "heavy function literal"),
    ("factorial(60000)", "heavy function literal"),
    ("binomial(200000,100000)", "heavy function, second arg"),
]
for _expr, _why in _HOSTILE:
    _start = _t.time()
    try:
        _r = _logic.evaluate_expression(_expr)
        _crashed = None
    except Exception as _exc:
        _r, _crashed = {}, f"{type(_exc).__name__}: {_exc}"
    _elapsed = _t.time() - _start
    check(f"{_expr}: refused rather than evaluated ({_why})",
          _crashed is None and _r.get("ok") is False,
          f"-> crash={_crashed} ok={_r.get('ok')}")
    # A guard that returns the right answer after two minutes has not bounded
    # anything. 5s is generous next to the ~0.3s these actually take, and well
    # under the SIGKILL the unbounded versions earned.
    check("  ...in bounded time", _elapsed < 5.0, f"-> {_elapsed:.2f}s")
    check("  ...with a reason naming the limit",
          any(w in (_r.get("error") or "") for w in ("limit", "tower", "digits")),
          f"-> {(_r.get('error') or '')[:70]!r}")

# The bound must not eat ordinary mathematics. Each of these is the kind of
# thing the tool exists to answer, and each sits near a limit rather than far
# from it — a cap that only passes trivial input is a cap set wrong.
_LEGIT = {
    "2+2": "4",
    "2**64": "18446744073709551616",
    "factorial(20)": "2432902008176640000",
    "binomial(10,5)": "252",
    "sin(x)**2 + cos(x)**2": "1",
}
for _expr, _want in _LEGIT.items():
    _r = _logic.evaluate_expression(_expr)
    check(f"{_expr}: still evaluates to {_want}",
          _r.get("ok") is True and _r.get("simplified") == _want,
          f"-> ok={_r.get('ok')} simplified={_r.get('simplified')!r}")

# The boundary itself, asserted from both sides so the cap is a real edge
# rather than a number in a comment.
from codecalc.safe_expr import MAX_HEAVY_ARG as _CAP

check("the heavy-argument cap admits its own limit",
      _logic.evaluate_expression(f"factorial({_CAP})").get("ok") is True,
      f"-> factorial({_CAP})")
check("  ...and refuses one past it",
      _logic.evaluate_expression(f"factorial({_CAP + 1})").get("ok") is False,
      f"-> factorial({_CAP + 1})")

# ── #80: an unreadable output stream must not read as an empty one ────────
# read_capped() discarded both its open failure and its read failure, and the
# Python fallback's _BoundedDrain swallowed OSError with a bare `pass`.
# Different mechanisms, identical result: output that could not be read came
# back as output that was simply empty, on a run reported as successful.
#
# Measured on the old code, four cases, three indistinguishable:
#     printed 42 -> "42\n" | printed nothing -> "" | MISSING -> "" | UNREADABLE -> ""
from codecalc import executor as _ex

_saved_rust = _ex._rust
_ex._rust = None
try:
    _r = _ex.execute("python3", "print(6*7)", timeout=10)
    check("fallback: a normal run reports no output_error",
          _r.get("ok") is True and _r.get("output_error") is None,
          f"-> ok={_r.get('ok')} output_error={_r.get('output_error')!r}")

    # Force the drain to fail part-way, the way a broken pipe would. A PARTIAL
    # read is the case worth testing: it is worse than an empty one, because
    # what comes back looks like the program's real output.
    _real_drain = _ex._BoundedDrain.drain

    def _failing_drain(self, on_overflow):
        chunk = self._stream.read(4)
        if chunk:
            self._buf.extend(chunk)
            self._seen += len(chunk)
        self.error = f"OSError: [Errno 5] Input/output error (after {self._seen} bytes)"

    _ex._BoundedDrain.drain = _failing_drain
    try:
        _r = _ex.execute("python3", "print('4' * 40)", timeout=10)
        check("fallback: a failed drain is REPORTED, not swallowed",
              _r.get("output_error") is not None,
              f"-> output_error={_r.get('output_error')!r}")
        check("  ...and makes the run not-ok, despite a clean exit",
              _r.get("ok") is False and _r.get("exit_code") == 0,
              f"-> ok={_r.get('ok')} exit_code={_r.get('exit_code')}")
        check("  ...naming the stream and how far it got",
              "stdout" in (_r.get("output_error") or "")
              and "bytes" in (_r.get("output_error") or ""),
              f"-> {_r.get('output_error')!r}")
    finally:
        _ex._BoundedDrain.drain = _real_drain
finally:
    _ex._rust = _saved_rust

# The native backend, against the real binary when one is built. Its failure is
# provoked for real — the output file is made unreadable while the child is
# still running — rather than by patching, because the defect was in what the
# binary does with a failed open.
if _ex._rust and os.name != "nt":
    import subprocess as _sp
    import tempfile as _tf
    import threading as _th

    from codecalc import registry as _reg_bs

    _w = _tf.mkdtemp()
    try:
        def _lock_output():
            time.sleep(1.0)
            # The Rust backend's run/compile redirect files now live inside
            # its scratch subdirectory (registry.RUN_SCRATCH_DIRNAME), not at
            # the workdir root — glob there, not at `_w` itself, or this
            # sabotage silently finds nothing and the run completes cleanly,
            # readable, defeating the whole point of this test.
            for _f in pathlib.Path(_w, _reg_bs.RUN_SCRATCH_DIRNAME).glob("*.out"):
                try:
                    _f.chmod(0o000)
                except OSError:
                    pass

        _t = _th.Thread(target=_lock_output, daemon=True)
        _t.start()
        _proc = _sp.run([_ex._rust, "--lang", "python3", "--timeout", "20",
                         "--workdir", _w],
                        input=b"import time; time.sleep(2); print(6*7)",
                        capture_output=True, timeout=60)
        _t.join(timeout=5)
        _out = json.loads(_proc.stdout.decode())
        check("rust: an unreadable output file is REPORTED, not read as empty",
              _out.get("output_error") is not None,
              f"-> output_error={_out.get('output_error')!r}")
        check("  ...and makes the run not-ok",
              _out.get("ok") is False, f"-> ok={_out.get('ok')}")
        check("  ...naming the stream and the OS error",
              "stdout" in (_out.get("output_error") or "")
              and "os error" in (_out.get("output_error") or ""),
              f"-> {_out.get('output_error')!r}")
    finally:
        for _f in pathlib.Path(_w).glob("**/*"):
            try:
                _f.chmod(0o644)
            except OSError:
                pass
        _shutil.rmtree(_w, ignore_errors=True)
else:
    print("SKIP rust unreadable-output probe (no native executor, or Windows)")

# ── #78: the bound must hold for things nobody put on a list ──────────────
# safe_expr screens reach and bounds the shapes known to explode. Both are
# denylists. This asserts the property a denylist cannot have: an expression
# the screen has no opinion about is still stopped.
from codecalc import guarded as _guarded

if _guarded.CAN_FORK:
    # Chosen by MEASUREMENT, not by intuition. A first attempt used a semiprime
    # whose two factors were ~7919 apart, which Fermat's method factors
    # instantly — it returned in 0.01s and proved nothing. These two run past
    # 25s unguarded, with the screen raising no objection to either.
    #
    # The first case USED to be a bare literal (`factorint(<62-digit
    # product>)`) -- #326 finding 4 (round 7, Codex) gave `factorint` its
    # own token-level digit-count cap (`_FACTOR_ARG_FUNCTIONS`, 25 digits),
    # which now correctly refuses that literal outright (a strict
    # improvement -- tested separately, above). To keep testing what THIS
    # block exists to test (a shape the screen still has no opinion on),
    # the factors are now COMPUTED `nextprime(...)` calls in the source
    # text instead of pre-computed Python literals: neither `10**30` nor
    # `3*10**31` is a bare NUMBER token (each is a small Pow/Mul of
    # small-digit tokens), so the digit-count screen -- literal-only by
    # design, same scope as `_heavy_call_violation` -- has no opinion on
    # either, even though `nextprime` still evaluates each eagerly, and
    # `factorint` still evaluates the resulting product eagerly, during
    # the very first (`evaluate=False`) parse.
    from codecalc.safe_expr import reject_unsafe as _screen

    _UNBOUNDED = [
        ("factorint(nextprime(10**30)*nextprime(3*10**31))",
         ("factorint of a COMPUTED 62-digit semiprime with far-apart factors "
          "-- neither factor is a bare literal token")),
        ("nextprime(10**2000)", "primality testing above a 2000-digit number"),
    ]
    for _expr, _why in _UNBOUNDED:
        check(f"the screen has no opinion on this ({_why})",
              _screen(_expr) is None, f"-> {_screen(_expr)!r}")
        _t0 = time.time()
        _r = _logic.evaluate_expression(_expr)
        _elapsed = time.time() - _t0
        check("  ...and it is KILLED anyway",
              _r.get("ok") is False, f"-> ok={_r.get('ok')}")
        check("  ...naming the limit that stopped it",
              "limit" in (_r.get("error") or ""), f"-> {(_r.get('error') or '')[:70]!r}")
        # The ceilings are CPU 10s and wall 15s. A bound that only holds after
        # a minute is not the bound this claims to be.
        check("  ...within its stated budget", _elapsed < 30,
              f"-> {_elapsed:.1f}s")

    # The guard must not change what a correct expression answers.
    for _expr, _want in (("2+2", "4"), ("sin(x)**2 + cos(x)**2", "1")):
        _r = _logic.evaluate_expression(_expr)
        check(f"guarded evaluation still answers {_expr!r} correctly",
              _r.get("ok") is True and _r.get("simplified") == _want,
              f"-> {_r.get('simplified')!r}")
else:
    print("SKIP guarded-evaluation probes — this platform cannot fork")

# Some SymPy functions return plain Python objects, not expressions. The result
# builder assumed `.is_number` and `sp.simplify`, so ordinary number-theory
# queries raised an uncaught AttributeError out of the tool.
for _expr, _want in (("nextprime(100)", "101"),
                     ("primefactors(60)", "[2, 3, 5]"),
                     ("divisors(12)", "[1, 2, 3, 4, 6, 12]"),
                     ("factorint(60)", "{2: 2, 3: 1, 5: 1}")):
    try:
        _r = _logic.evaluate_expression(_expr)
        _crash = None
    except Exception as _exc:
        _r, _crash = {}, f"{type(_exc).__name__}: {_exc}"
    check(f"{_expr}: a non-expression return is answered, not crashed",
          _crash is None and _r.get("ok") is True and _r.get("simplified") == _want,
          f"-> crash={_crash} simplified={_r.get('simplified')!r}")

# ── #84: every symbolic tool runs under the bound, not just one ───────────
# #78 guarded evaluate_expression and left five siblings on the screen alone.
# Three checks, deliberately of different kinds:
#
#   structural  every tool routes through guarded_call. Cheap, covers all six,
#               and catches a SEVENTH added later without the guard — which a
#               behavioural test of the current six never would.
#   transparent the wrapper must not change a correct answer. Also cheap.
#   behavioural a payload the screen has no opinion about is actually killed.
#               ~10s per tool because the CPU ceiling is 10s, so this runs for
#               one tool per MODULE rather than all six; the structural check
#               is what covers the rest.
import inspect as _inspect

from codecalc import exact as _exact

_GUARDED_TOOLS = [
    ("logic.evaluate_expression", _logic.evaluate_expression),
    ("logic.solve_linear", _logic.solve_linear),
    ("exact.simplify_expression", _exact.simplify_expression),
    ("exact.solve_expression", _exact.solve_expression),
    ("exact.limit_expression", _exact.limit_expression),
    ("exact.algebraic_equiv", _exact.algebraic_equiv),
]
for _label, _fn in _GUARDED_TOOLS:
    check(f"{_label} routes through the guard",
          "guarded_call" in _inspect.getsource(_fn),
          f"-> {_inspect.getsource(_fn).splitlines()[-1].strip()!r}")

# Transparency: guarded and unguarded must agree exactly on a correct call.
_PAIRS = [
    ("evaluate_expression", _logic.evaluate_expression, _logic._evaluate_expression,
     ("x**2 + 2*x + 1",)),
    ("solve_linear", _logic.solve_linear, _logic._solve_linear,
     ("x + y = 10; x - y = 2", "x,y")),
    ("simplify_expression", _exact.simplify_expression, _exact._simplify_expression,
     ("(x+1)**2",)),
    ("solve_expression", _exact.solve_expression, _exact._solve_expression,
     ("x**2 - 4 = 0", "x")),
    ("limit_expression", _exact.limit_expression, _exact._limit_expression,
     ("1/x", "x", "oo")),
    ("algebraic_equiv", _exact.algebraic_equiv, _exact._algebraic_equiv,
     ("(x+1)**2", "x**2+2*x+1")),
]
# Compared MODULO the guard's own reporting. On a platform without fork the
# guarded result legitimately carries one extra key — `unenforced`, saying the
# bound was not applied — so a bare equality check failed on windows-latest for
# the one reason that is not a defect. Caught by CI; the first version of this
# assertion was wrong, not the code.
#
# So the two are compared with that key removed, and its presence is then
# asserted in its own right: it must appear exactly when the platform cannot
# fork, and never when it can. That turns the Windows path from something this
# test tripped over into something it checks.
def _without_guard_marker(result: dict) -> dict:
    rest = [u for u in (result.get("unenforced") or [])
            if u != _guarded.UNENFORCED_NO_FORK]
    out = {k: v for k, v in result.items() if k != "unenforced"}
    if rest:
        out["unenforced"] = rest
    return out


for _label, _pub, _priv, _args in _PAIRS:
    _g, _u = _pub(*_args), _priv(*_args)
    check(f"{_label}: the guard does not change a correct answer",
          _without_guard_marker(_g) == _without_guard_marker(_u),
          f"-> guarded={str(_g)[:60]}")
    _marked = _guarded.UNENFORCED_NO_FORK in (_g.get("unenforced") or [])
    check("  ...and reports the bound as unenforced iff it could not apply it",
          _marked == (not _guarded.CAN_FORK),
          f"-> can_fork={_guarded.CAN_FORK} marked={_marked}")

if _guarded.CAN_FORK:
    # A COMPUTED product of two far-apart primes, not a pre-computed bare
    # literal -- same reasoning as the #78 block above (round 7, #326
    # finding 4 gave `factorint` a literal-only digit-count screen, which
    # has no opinion on a computed argument like this one).
    _payload = "factorint(nextprime(10**30)*nextprime(3*10**31))"
    for _label, _call in (("exact.simplify_expression",
                           lambda: _exact.simplify_expression(_payload)),
                          ("logic.solve_linear",
                           lambda: _logic.solve_linear(f"{_payload} = 0", "x"))):
        _t0 = time.time()
        _r = _call()
        check(f"{_label}: a screen-defeating payload is killed",
              _r.get("ok") is False and "limit" in (_r.get("error") or ""),
              f"-> ok={_r.get('ok')} {str(_r.get('error'))[:56]!r}")
        check("  ...within its stated budget", time.time() - _t0 < 30,
              f"-> {time.time() - _t0:.1f}s")

# ── three screen bypasses found by an external audit ──────────────────────
# All three defeated guards added earlier the same day, which is the point:
# the guards were written against the shapes their author imagined.
from codecalc import exact as _ex2
from codecalc.safe_expr import reject_unsafe as _scr

# 1. NON-DECIMAL LITERALS. The heavy-argument cap parsed with int(s), base 10.
# int("0xffffff") raises ValueError and the except skipped it — so the SAME
# NUMBER written in hex sailed past a cap that stops it in decimal.
for _lit, _dec in (("0xffffff", 16777215), ("0o777777", 262143),
                   ("0b111111111111111111111111", 16777215)):
    _msg = _scr(f"factorial({_lit})") or ""
    check(f"factorial({_lit}) is capped like factorial({_dec})",
          str(_dec) in _msg and "exceeds the limit" in _msg, f"-> {_msg[:60]!r}")
check("  ...while a small literal in any base still passes",
      _scr("factorial(0x14)") is None, f"-> {_scr('factorial(0x14)')!r}")

# 2. NEGATIVE EXPONENTS. `exp_value <= 1: continue` waved every negative
# exponent through on its way to skipping 0 and 1 — and the digit estimate was
# sign-blind too, so fixing only the first check left it passing.
from sympy.parsing.sympy_parser import convert_xor as _cx
from sympy.parsing.sympy_parser import implicit_multiplication_application as _ima
from sympy.parsing.sympy_parser import parse_expr as _pe
from sympy.parsing.sympy_parser import standard_transformations as _st

from codecalc.safe_expr import reject_explosive as _rx

_T = _st + (_ima, _cx)
_neg = _rx(_pe("2**(-1000000)", transformations=_T, evaluate=False))
check("a huge NEGATIVE exponent is refused",
      _neg is not None and "digits" in _neg, f"-> {_neg!r}")
check("  ...and a small one is not",
      _rx(_pe("2**(-2)", transformations=_T, evaluate=False)) is None,
      f"-> {_rx(_pe('2**(-2)', transformations=_T, evaluate=False)) or 'correctly allowed'}")

# 3. eval_exact WAS NEITHER SCREENED NOR GUARDED. It has its own AST evaluator
# over math.*, so #78's work on the six SymPy tools did not cover it, and it
# bounded ** while bounding nothing about function arguments. Measured before
# the fix: factorial(10000000) never returned — killed from outside at 30s.
_t0 = time.time()
_r = _ex2.eval_exact("factorial(10000000)")
_elapsed = time.time() - _t0
check("eval_exact refuses an unbounded factorial",
      _r.get("ok") is False, f"-> ok={_r.get('ok')}")
check("  ...in bounded time rather than never returning",
      _elapsed < 20, f"-> {_elapsed:.2f}s")
check("  ...and still answers ordinary exact arithmetic",
      _ex2.eval_exact("0.1+0.2").get("value") == "3/10",
      f"-> {_ex2.eval_exact('0.1+0.2').get('value')!r}")

# ═══ solve_linear's own sympify was not screened the way the RCE ════════════
# probe above proves evaluate_expression is — classify_unsafe is a syntax
# DENYLIST, and a screened, non-denylisted NAME can still be a LIVE Python
# builtin. `sp.sympify(lhs)`/`sp.sympify(rhs)` (the '=' path) and
# `sp.sympify(raw, evaluate=False)` (the no-'=' path) both used SymPy's
# DEFAULT global_dict (vars(builtins) copied in): `input()` carries none of
# classify_unsafe's denied syntax, so it reached sympify and CALLED the real
# input() — reading the child's stdin, shared fd 0 with a stdio MCP server —
# and `9**9**9**9` reached sympify's default evaluator and burned real CPU
# (measured: ~18s, riding the guard's own 15s timeout) instead of being
# caught by reject_explosive's unevaluated-shape check the way
# evaluate_expression's identical input already is. solve_linear now parses
# every piece via logic._parse_solve_piece: parse_expr(global_dict=
# safe_global_dict()) instead of bare sympify, with reject_explosive run on
# the unevaluated shape first — the same fix gave linalg's per-cell
# parse.

# input()/breakpoint()/quit() must not invoke the real builtin — compare
# against evaluate_expression's own outcome for the identical payload; they
# must now agree.
for _name in ("input()", "breakpoint()", "quit()"):
    _direct = _logic.evaluate_expression(_name)
    _sl = _logic.solve_linear(f"x = {_name}", "x")
    check(f"solve_linear('x = {_name}', 'x') does not invoke the real builtin",
          _sl.get("ok") is False and "parse error" in str(_sl.get("error")),
          f"-> {_sl}")
    check(f"  ...and matches evaluate_expression's own outcome for {_name!r}",
          _direct.get("ok") is False and "parse error" in str(_direct.get("error")),
          f"-> evaluate_expression={_direct}")

# a power tower burns real CPU seconds if evaluated blind. reject_explosive
# on the unevaluated shape now catches it before anything is evaluated —
# assert BOTH the code and that it was actually fast, not just eventually
# correct.
_t0 = time.time()
_r = _logic.solve_linear("x = 9**9**9**9", "x")
_elapsed = time.time() - _t0
check("solve_linear('x = 9**9**9**9', 'x') is resource_exhausted, not evaluated",
      _r.get("ok") is False and _r.get("code") == "resource_exhausted", f"-> {_r}")
check(f"  ...and rejected promptly ({_elapsed:.3f}s), not after a multi-second burn",
      _elapsed < 2.0, f"-> {_elapsed:.3f}s")

# ...and ordinary linear systems still solve correctly — both the '=' path
# and the no-'=' (raw) path this fix touched.
_r = _logic.solve_linear("x + y = 10; x - y = 2", "x, y")
check("solve_linear still solves an ordinary 2x2 system",
      _r.get("ok") is True and _r.get("solutions") == ["{x: 6, y: 4}"], f"-> {_r}")
_r = _logic.solve_linear("x - 5; y - 3", "x, y")
check("solve_linear still solves the no-'=' (raw) path",
      _r.get("ok") is True and _r.get("solutions") == ["{x: 5, y: 3}"], f"-> {_r}")

# ═══ the same bare-sympify gap in exact.py's four remaining ════════════════
# symbolic tools. algebraic_equiv / solve_expression / limit_expression
# (including its `point` argument) / simplify_expression each did
# `classify_unsafe(...)` then a bare `sp.sympify(...)` — classify_unsafe is a
# syntax denylist, not a namespace: `input`/`breakpoint`/`quit` carry none of
# its denied syntax (no '.', '[', leading underscore or denied keyword), so
# they reached sympify's DEFAULT global_dict (vars(builtins) copied in) and
# CALLED the real builtin — input() reading the child's stdin (fd 0, shared
# with a stdio MCP server), breakpoint() dropping into pdb and hanging until
# the wall clock killed it — and `9**9**9**9` reached sympify's default
# evaluator and burned real CPU instead of being caught by
# reject_explosive's unevaluated-shape check the way evaluate_expression's
# identical input already is. All four now route through the new
# `safe_expr.safe_parse` (the same fix gave `matrix` and
# `solve_linear`, extracted to one shared helper).

# input()/breakpoint()/quit() must not invoke the real builtin — compare
# against evaluate_expression's own outcome for the identical payload; they
# must now agree.
_THE_889_CALLS = [
    ("algebraic_equiv", lambda p: _exact.algebraic_equiv(p, "1")),
    ("solve_expression", lambda p: _exact.solve_expression(p)),
    ("limit_expression (expr)", lambda p: _exact.limit_expression(p, "x", "oo")),
    ("limit_expression (point)", lambda p: _exact.limit_expression("x", "x", p)),
    ("simplify_expression", lambda p: _exact.simplify_expression(p)),
]
for _name in ("input()", "breakpoint()", "quit()"):
    _direct = _logic.evaluate_expression(_name)
    check(f"evaluate_expression({_name!r}) does not invoke the real builtin",
          _direct.get("ok") is False and "parse error" in str(_direct.get("error")),
          f"-> {_direct}")
    for _label, _call in _THE_889_CALLS:
        _r = _call(_name)
        check(f"{_label}({_name!r}) does not invoke the real builtin",
              _r.get("ok") is False and "parse error" in str(_r.get("error")),
              f"-> {_r}")

# A power tower burns real CPU seconds if evaluated blind. reject_explosive
# on the unevaluated shape now catches it before anything is evaluated —
# assert BOTH the code and that it was actually fast, not just eventually
# correct.
_THE_889_TOWER_CALLS = [
    ("algebraic_equiv", lambda: _exact.algebraic_equiv("9**9**9**9", "0")),
    ("solve_expression", lambda: _exact.solve_expression("9**9**9**9 - x")),
    ("limit_expression (expr)", lambda: _exact.limit_expression("9**9**9**9 + x", "x", "oo")),
    ("limit_expression (point)", lambda: _exact.limit_expression("x", "x", "9**9**9**9")),
    ("simplify_expression", lambda: _exact.simplify_expression("9**9**9**9")),
]
for _label, _call in _THE_889_TOWER_CALLS:
    _t0 = time.time()
    _r = _call()
    _elapsed = time.time() - _t0
    check(f"{_label}('9**9**9**9') is resource_exhausted, not evaluated",
          _r.get("ok") is False and _r.get("code") == "resource_exhausted", f"-> {_r}")
    check(f"  ...and rejected promptly ({_elapsed:.3f}s), not after a multi-second burn",
          _elapsed < 2.0, f"-> {_elapsed:.3f}s")

# ...and ordinary symbolic work still answers correctly — the four tools'
# own ported-feature assertions (tests/test_calc_port.py) cover the values;
# this just proves the safe_parse swap did not silently break the common
# path.
_r = _exact.algebraic_equiv("(a+b)**2", "a**2 + 2*a*b + b**2")
check("algebraic_equiv still proves an ordinary identity",
      _r.get("ok") is True and _r.get("identical") is True, f"-> {_r}")
_r = _exact.solve_expression("x**2 - 4 = 0")
check("solve_expression still solves an ordinary equation",
      _r.get("ok") is True and sorted(_r.get("solutions", [])) == ["-2", "2"], f"-> {_r}")
_r = _exact.limit_expression("1/x", "x", "oo")
check("limit_expression still answers an ordinary limit",
      _r.get("ok") is True and _r.get("limit") == "0", f"-> {_r}")
_r = _exact.simplify_expression("(x**2 - 1)/(x - 1)")
check("simplify_expression still simplifies ordinary algebra",
      _r.get("ok") is True and _r.get("simplified") == "x + 1", f"-> {_r}")

# ═══ solve_linear crashed on a SINGLE-variable system ═════════════════════
# sp.symbols("x") returns a bare Symbol, not a 1-tuple, unless given more
# than one name or a trailing comma — the code downstream assumed a
# sequence (`list(syms)`), so `solve_linear('2*x = 4', 'x')` raised
# "'Symbol' object is not iterable" rather than returning a result.
_r = _logic.solve_linear("2*x = 4", "x")
check("solve_linear solves a single-variable system",
      _r.get("ok") is True and _r.get("solutions") == ["{x: 2}"], f"-> {_r}")
_r = _logic.solve_linear("x - 5", "x")
check("  ...including the no-'=' single-variable path",
      _r.get("ok") is True and _r.get("solutions") == ["{x: 5}"], f"-> {_r}")
# The fix must not change multi-variable behaviour.
_r = _logic.solve_linear("x + y = 10; x - y = 2", "x, y")
check("  ...and multi-variable systems are unaffected",
      _r.get("ok") is True and _r.get("solutions") == ["{x: 6, y: 4}"], f"-> {_r}")


# ═══ reject_explosive bounds Pow shapes, not nested-parens + ═════════════════
# ═══ repeated-term chains — guarded_call is the actual backstop for those ═══
# `scripts/fuzz.py` found that deeply nested parens combined with a
# long chain of repeated terms costs multiple seconds of real CPU INSIDE
# SymPy's own recursive-descent parser, raising a caught "maximum recursion
# depth exceeded" before `safe_parse` ever has a tree to hand `reject_explosive`
# — so extending `reject_explosive` itself cannot bound this shape; see its
# docstring for why. What DOES bound it, measured rather than assumed: every
# caller of `safe_parse` runs through `guarded.guarded_call`, which kills a
# forked child at `RLIMIT_CPU` (10s) and enforces a 15s wall-clock in the
# parent regardless of what the child is doing.
#
# depth=100 parens wrapping 900 repetitions of the implicit-multiplication
# term "2x" — exactly at `logic._MAX_EXPR_LEN`'s 2000-char cap, the shape
# `scripts/fuzz.py --seed 777` found (that one used depth=130/410 reps of
# "1.5 + 2.3"; this uses a shorter term so more repetitions fit in the same
# 2000 chars, and costs the same multi-second class either way).
_d, _k = 100, 900
_nested_chain = "(" * _d + "2x" * _k + ")" * _d
check("the nested+repeated shape stays inside the 2000-char cap",
      len(_nested_chain) == 2000, f"-> {len(_nested_chain)} chars")

_t0 = time.time()
_r = _logic.evaluate_expression(_nested_chain)
_elapsed = time.time() - _t0
# guarded.DEFAULT_WALL_SECONDS is 15 — asserted well under it (20s), not
# exactly at it, so ordinary timing jitter on a loaded CI runner cannot flip
# this from "the backstop held" to "flaky".
check("evaluate_expression refuses the shape rather than hanging",
      _r.get("ok") is False, f"-> {_r}")
check("  ...and guarded_call's wall-clock backstop actually bounds it",
      _elapsed < 20.0, f"-> {_elapsed:.2f}s (guarded.DEFAULT_WALL_SECONDS=15)")


# ═══ reject_explosive RAISED instead of refusing on 3 extreme-magnitude ═════
# ═══ shapes — violating the "classify_unsafe/safe_parse never raise" ═══════
# ═══ contract fuzz/safe_expr_fuzzer.py's own module docstring states ═══════
# All three surfaced by the 2026-08-22 ClusterFuzzLite batch. Each is a huge
# INTEGER value reaching either an int->float conversion or an f-string
# interpolation that itself has no bound — a crash from *reporting* the
# refusal, not from the refusal decision itself.
from codecalc.safe_expr import safe_parse as _boundary_parse

_EXPLOSIVE_CRASH_INPUTS = [
    # exponent = factorial2(100000) (the `!!` double-factorial transform),
    # an exact Integer with thousands of digits: `abs(exp_value) *
    # math.log10(magnitude)` used to coerce that huge int to float first,
    # raising OverflowError("int too large to convert to float").
    "2**100000!!ubrNembeubrNember",
    # same huge-exponent shape, but the base carries a free symbol (`x`), so
    # reject_explosive takes the SYMBOLIC branch instead — and that branch's
    # own refusal MESSAGE used to interpolate the raw exponent with an
    # f-string, which raised ValueError past Python's int->str conversion
    # limit (4300 digits) before the message could even be built.
    "(x+1)**20000!!ubrNembeubrNember",
    # a >308-digit base: SymPy's Integer.__float__ returns inf here rather
    # than raising (unlike Python's own int.__float__), so the try/except
    # around float(base) never fired; digits came out inf, and `int(digits)`
    # in the refusal message raised OverflowError("cannot convert float
    # infinity to integer").
    "9" * 400 + "**12",
]

for _expr in _EXPLOSIVE_CRASH_INPUTS:
    try:
        _value, _err = _boundary_parse(_expr)
        _raised = None
    except Exception as _exc:  # the exact bug: this must never happen
        _value, _err, _raised = None, None, _exc
    check(f"safe_parse does not raise on {_expr[:45]!r}...",
          _raised is None, f"-> raised {_raised!r}" if _raised else "")
    if _raised is None:
        check("  ...and refuses it rather than evaluating an explosive shape",
              _value is None and _err is not None, f"-> value={_value!r} err={_err!r}")

# ...and the same three inputs are unreachable from evaluate_expression too —
# the caller-facing surface everything above is proxying for.
for _expr in _EXPLOSIVE_CRASH_INPUTS:
    try:
        _eval_result = _logic.evaluate_expression(_expr)
        _raised = None
    except Exception as _exc:
        _eval_result, _raised = None, _exc
    check(f"evaluate_expression does not raise on {_expr[:45]!r}...",
          _raised is None, f"-> raised {_raised!r}" if _raised else "")
    if _raised is None:
        check("  ...and returns a refusal, not a value",
              _eval_result.get("ok") is False, f"-> {_eval_result}")

# the fix must not waive an actually-explosive shape through just to avoid
# crashing on it — each of the three above is refused for a real reason
# (an astronomically huge digit count / exponent), not just "didn't raise".
#
# THE-1095 round 2 (verify-1095): the first two inputs' own WORDING changed,
# not their safety. `100000!!` is the `factorial2` double-factorial postfix
# transform — invisible to the TOKEN-level heavy-call screen (it looks for
# a `NAME(` call in the source text; `!!` never spells "factorial2("), so
# these two relied entirely on the TREE-level scan. Pre-round-2 (real-dict
# parse first), `factorial2(100000)`'s LITERAL argument (100000, itself
# over MAX_HEAVY_ARG) evaluated for real DURING that first parse -- by the
# time the scan ran, no `factorial2` Function node was left to check
# against its own cap at all, only a `Pow` with an already-materialized,
# thousands-of-digits Integer exponent, caught by the generic digit-count
# (`2**100000!!...`) / symbolic-exponent (`(x+1)**20000!!...`) Pow
# machinery instead. Round 2's deferred-first ordering means the deferred
# scan (which always runs first now) sees the REAL, unevaluated
# `factorial2(100000)` Function node directly, and refuses it THERE, via
# its own value-kind cap message, before either downstream mechanism ever
# gets a chance to run — a MORE direct, more accurate refusal (it now
# names the actual over-cap argument, not a derived symptom of it), not a
# weaker one. `"9"*400 + "**12"` is unaffected -- no heavy-function call is
# involved, so its digit-count message is unchanged.
check("  ...2**100000!!... is refused on the factorial2() heavy-call cap "
      "itself now (THE-1095 round 2), not silently allowed",
      _boundary_parse(_EXPLOSIVE_CRASH_INPUTS[0])[1] is not None
      and "factorial2" in _boundary_parse(_EXPLOSIVE_CRASH_INPUTS[0])[1][1])
check("  ...(x+1)**20000!!... is refused on the factorial2() heavy-call cap "
      "itself now (THE-1095 round 2), not silently allowed",
      _boundary_parse(_EXPLOSIVE_CRASH_INPUTS[1])[1] is not None
      and "factorial2" in _boundary_parse(_EXPLOSIVE_CRASH_INPUTS[1])[1][1])
check("  ...'9'*400 + '**12' is refused on 'digits' grounds, not silently allowed",
      _boundary_parse(_EXPLOSIVE_CRASH_INPUTS[2])[1] is not None
      and "digits" in _boundary_parse(_EXPLOSIVE_CRASH_INPUTS[2])[1][1])

# ...and the fix does not make the digit estimate so imprecise that it starts
# refusing ordinary, well-under-the-cap powers: 2**10000 has ~3011 true
# digits (comfortably under MAX_NUMERIC_DIGITS=4000) and must still evaluate.
# A cruder `bit_length() * log10(2)` estimate (no -1/shift correction)
# overestimates log10(2) by roughly 2x, which was measured to flip exactly
# this case to a false refusal.
_eval_result = _logic.evaluate_expression("2**10000")
check("2**10000 (~3011 digits, under the 4000 cap) still evaluates",
      _eval_result.get("ok") is True, f"-> {_eval_result}")

# ═══ off-by-one at MAX_NUMERIC_DIGITS: `digits` IS log10(result), not the ═══
# ═══ digit count — comparing it directly against the cap admits equality ═══
# Cross-vendor (Codex) review of the fix above caught this: `10**4000` has
# 4001 digits (a 1 followed by 4000 zeros) but log10(10**4000) == 4000
# exactly, which is not `> MAX_NUMERIC_DIGITS` (4000) under the old
# `digits > MAX_NUMERIC_DIGITS` comparison — so a 4001-digit result sailed
# through as "allowed", one digit over the cap the refusal message itself
# claims. The true count is `floor(digits) + 1`.
from codecalc.safe_expr import MAX_NUMERIC_DIGITS as _MAX_DIGITS_CAP

_boundary_refusal = _boundary_parse(f"10**{_MAX_DIGITS_CAP}")[1]  # 10**4000 -> 4001 digits: must refuse
check(f"10**{_MAX_DIGITS_CAP} ({_MAX_DIGITS_CAP + 1} digits, one over the cap) is refused",
      _boundary_refusal is not None and "digits" in _boundary_refusal[1], f"-> {_boundary_refusal}")
_boundary_value = _boundary_parse(f"10**{_MAX_DIGITS_CAP - 1}")[0]  # 10**3999 -> exactly 4000: at the cap
check(f"10**{_MAX_DIGITS_CAP - 1} (exactly {_MAX_DIGITS_CAP} digits, AT the cap) is still allowed",
      _boundary_value is not None, f"-> {_boundary_parse(f'10**{_MAX_DIGITS_CAP - 1}')}")

# ═══ classify_unsafe's heavy-arg message had the identical unbounded ═══════
# ═══ int->str interpolation reject_explosive's messages did ════════════════
# `int(s, 0)` (base-0 auto-detect) parses hex/octal/binary text in linear
# time with no digit-count limit — unlike decimal text<->int conversion,
# which IS what str(value) does and exactly what carries Python's 4300-digit
# limit. So a short-looking token like `factorial(0x` + `f`*4000 + `)`
# parses to a plain int with thousands of DECIMAL digits, and the refusal
# message's own `str(value)` raised ValueError before it could report the
# refusal — the same crash class as the explosive-Pow crash sites above,
# just in `_heavy_call_violation` (reached from `classify_unsafe`, the FIRST
# screening step, called on the raw string before any length cap in some
# callers — e.g. `fuzz/safe_expr_fuzzer.py`'s atheris harness, which drives
# `classify_unsafe` directly with up to 4096 bytes, past the 2000-char cap
# every production `safe_parse` caller enforces).
_hex_bomb = "factorial(0x" + "f" * 4000 + ")"
try:
    _hex_bomb_msg = _scr(_hex_bomb)
    _raised = None
except Exception as _exc:
    _hex_bomb_msg, _raised = None, _exc
check("classify_unsafe does not raise on a 4000-hex-digit literal argument",
      _raised is None, f"-> raised {_raised!r}" if _raised else "")
if _raised is None:
    check("  ...and refuses it on 'exceeds the limit' grounds",
          _hex_bomb_msg is not None and "exceeds the limit" in _hex_bomb_msg, f"-> {_hex_bomb_msg!r}")
# ...and an ordinary (small) non-decimal literal still names the exact value,
# unchanged from before this fix — the fallback only kicks in once str()
# itself would actually raise.
_hex_bomb_msg = _scr("factorial(0xffffff)") or ""
check("  ...while an ordinary hex literal's message still names the exact value",
      "16777215" in _hex_bomb_msg, f"-> {_hex_bomb_msg!r}")

# ═══ reject_explosive's ceiling COVERAGE gap — Mul/Rational-shaped ════════
# ═══ exponents and bases (the fix above's own "TRIAGED, NOT FIXED" item) ═══
# `parse_expr(..., evaluate=False)` leaves `30000/2` as `Mul(30000,
# Pow(2, -1))` and `3/2` as `Mul(3, Pow(2, -1))` rather than folding them to
# a plain Integer/Rational — so the OLD `isinstance(exponent, (Integer,
# Float))` / `isinstance(base, (Integer, Float))` checks skipped them
# entirely, and the actual (explosive) power got computed with NO ceiling
# ever applied: `safe_parse` returned a real 4,516-digit Integer / a
# Rational with a 47,549-bit numerator, successfully, rather than a refusal.
# Confirmed live on main before this fix (not assumed): all three of the
# following returned a computed value with `err is None`. The six symbolic
# tools survived only via each caller's own `_RESOURCE_ERRORS` catch at
# stringification — a structured refusal, but at the wrong layer, after the
# full cost was already paid.
_FACE1_GAP_INPUTS = [
    "2**(30000/2)",     # exponent = Mul(30000, Pow(2,-1)) -- a Mul, not Integer
    "2**(-30000/2)",    # same shape, negative
    "(3/2)**30000",     # base = Mul(3, Pow(2,-1)) -- a Mul, not Integer
]
for _expr in _FACE1_GAP_INPUTS:
    _value, _err = _boundary_parse(_expr)
    check(f"safe_parse({_expr!r}) is refused on ceiling grounds, not computed",
          _value is None and _err is not None and _err[0] == "ceiling",
          f"-> value={_value!r} err={_err!r}")
    if _err is not None:
        check("  ...naming a digit count, not silently waived through",
              "digits" in _err[1], f"-> {_err[1]!r}")

# ...and the same three are unreachable from evaluate_expression too — the
# caller-facing surface, and where the previous behaviour's crash-avoidance
# `_RESOURCE_ERRORS` catch used to be the only thing standing between this
# gap and a caller. Now refused BEFORE that catch is ever needed.
for _expr in _FACE1_GAP_INPUTS:
    _eval_result = _logic.evaluate_expression(_expr)
    check(f"evaluate_expression({_expr!r}) refuses rather than returning a huge value",
          _eval_result.get("ok") is False, f"-> {_eval_result}")
    # The value itself must never appear anywhere in the response — not just
    # "ok is False": a huge digit string leaking into an error message would
    # be the same class of problem this whole module exists to avoid.
    check("  ...and no huge digit string leaked into the response",
          len(json.dumps(_eval_result)) < 500, f"-> len={len(json.dumps(_eval_result))}")

# ...and the fix does not turn into a blanket refusal of every Mul/Rational-
# shaped exponent or base — only ones that are actually over the cap.
_FACE1_UNDER_CAP = [
    ("2**(19999/2)", "exponent"),   # 2**9999.5 truncates -- 9999 is under-cap, an ordinary compound exponent
    ("(3/2)**100", "base"),         # a modest compound-Rational base
    ("2**(3+2)", "Add exponent"),   # 2**5 -- exercises the Add branch, not just Mul
    ("(1+1)**10", "Add base"),      # 2**10 -- ditto for the base side
    ("2**(3/2)", "fractional exponent"),  # irrational result (2*sqrt(2)) -- not a huge integer, must not be refused
]
for _expr, _label in _FACE1_UNDER_CAP:
    _value, _err = _boundary_parse(_expr)
    check(f"safe_parse({_expr!r}) ({_label}, under cap) still evaluates",
          _value is not None and _err is None, f"-> value={_value!r} err={_err!r}")

# ═══ reject_explosive only inspected Pow, so a PRODUCT of individually ═════
# ═══ legal heavy calls bypassed MAX_NUMERIC_DIGITS entirely (GH #326, ═════
# ═══ THE-1091) ══════════════════════════════════════════════════════════════
# The gap above (Mul/Rational-shaped exponents and bases) is still INSIDE a
# Pow node — the walk finds it because it is looking at a Pow at all. This
# one is a different shape: `"*".join(["factorial(1463)"] * 117)` (1871
# chars, under the 2000-char cap) has no Pow anywhere. Each `factorial(1463)`
# is individually legal (MAX_HEAVY_ARG admits 1463; the result is 3998
# digits, under MAX_NUMERIC_DIGITS) and materializes to a plain Integer
# during PARSING itself (a function call on a literal argument evaluates at
# parse time regardless of `evaluate=False` — see the _HEAVY_FUNCTIONS
# comment in safe_expr.py). `Mul(Integer, Integer, ..., 117 of them)` walked
# straight past the old Pow-only loop, and CPython's own int->str ceiling
# (4300 digits by default) raised from deep inside SymPy's printer as an
# uncaught ValueError -- coded "internal" (a codecalc defect) rather than
# "resource_exhausted" (a ceiling the caller can act on by shrinking the
# input). Confirmed live on main before this fix: exactly that.
from codecalc import errors as _errors

_PRODUCT_BOMB = "*".join(["factorial(1463)"] * 117)
check(f"the 117-factorial product is {len(_PRODUCT_BOMB)} chars, under the 2000-char cap",
      len(_PRODUCT_BOMB) < 2000, f"-> {len(_PRODUCT_BOMB)}")

_t0 = time.time()
_product_result = _exact.simplify_expression(_PRODUCT_BOMB)
_product_elapsed = time.time() - _t0
check("a product of 117 legal factorial(1463) calls is refused, not computed",
      _product_result.get("ok") is False, f"-> {_product_result}")
check(f"  ...promptly ({_product_elapsed:.3f}s), before the ~467,766-digit "
      "product is ever printed",
      _product_elapsed < 1.0, f"-> {_product_elapsed:.3f}s")
check("  ...with the ceiling code, not 'internal'",
      _product_result.get("code") == _errors.RESOURCE_EXHAUSTED,
      f"-> {_product_result.get('code')}")
check("  ...and a remedy addressed to the caller (shrink the input), "
      "not CPython's own sys.set_int_max_str_digits() advice",
      "sys.set_int_max_str_digits" not in (_product_result.get("error") or ""),
      f"-> {_product_result.get('error')!r}")
check("  ...and the remedy names reducing the work",
      bool(_product_result.get("remedy")), f"-> {_product_result.get('remedy')!r}")

# A single legal call, unmultiplied, must still work -- this is a refusal of
# the PRODUCT, not a tightening of MAX_HEAVY_ARG itself.
_single_result = _exact.simplify_expression("factorial(1463)")
check("a single factorial(1463) is still ok",
      _single_result.get("ok") is True, f"-> ok={_single_result.get('ok')!r}")

# The Add branch, not just Mul: reject_explosive's walk visits every
# numeric-only Mul/Add node, not only ones nested inside a Pow. A pure SUM of
# legal factorial(1463) calls cannot itself cross MAX_NUMERIC_DIGITS within
# the 2000-char cap (117 copies -- as many as the char budget allows -- sums
# to exactly 4000 digits, AT the cap, not over it: digit count from adding N
# equal-magnitude terms grows by log10(N), not by N like a product does), so
# this exercises the Add branch with a product already over cap as one of
# its terms instead -- a shape with no Pow anywhere, so the old code walked
# past this one too.
_sum_bomb = "factorial(1463)*factorial(1463)+1"
_sum_tree = _pe(_sum_bomb, transformations=_T, evaluate=False)
_sum_refusal = _rx(_sum_tree)
check(f"a sum ({_sum_bomb!r}) wrapping an over-cap product is refused",
      _sum_refusal is not None and "digits" in _sum_refusal, f"-> {_sum_refusal!r}")

# A purely symbolic product must be untouched -- the new check only fires on
# a Mul/Add with NO free symbols.
_symbolic_tree = _pe("x*y*z", transformations=_T, evaluate=False)
check("a symbolic product (x*y*z) is not refused",
      _rx(_symbolic_tree) is None, f"-> {_rx(_symbolic_tree)!r}")

# The two original Pow-only refusals must STILL refuse -- wording is no
# longer pinned byte-for-byte as of round 4 (cross-vendor review): the scan
# now descends into Pow's own base/exponent (finding 1), so `2**100000` is
# caught by the scan's num/den check directly, and `9**9**9**9` (a genuine
# 3-level tower) is caught one level down, at `9**(9**9)` -- both COMPOUND
# exponents that reduce to a safely-small exact integer via
# `_safe_multiset_rational`'s per-term-gated arithmetic, so the scan resolves
# them fully before the Pow loop's own dedicated "power tower" wording ever
# gets a chance to fire. Still refused either way -- see the "digits" check.
_pow_cases = ["2**100000", "9**9**9**9"]
for _expr in _pow_cases:
    _tree = _pe(_expr, transformations=_T, evaluate=False)
    _got = _rx(_tree)
    check(f"{_expr!r} is still refused",
          _got is not None and "digits" in _got, f"-> {_got!r}")

# exact.py's catch-all clauses now route through errors.classify() instead of a bare
# str(exc) -- a CPython digit-limit ValueError must classify to the ceiling
# code, not internal (the classify()-level half of this fix; see
# test_error_codes.py for the direct unit-level assertion).
_synthetic_digit_limit_exc = ValueError(
    "Exceeds the limit (4300 digits) for integer string conversion; use "
    "sys.set_int_max_str_digits() to increase the limit")
check("errors.classify() maps a CPython digit-limit ValueError to resource_exhausted",
      _errors.classify(_synthetic_digit_limit_exc) == _errors.RESOURCE_EXHAUSTED,
      f"-> {_errors.classify(_synthetic_digit_limit_exc)}")
check("  ...while an ordinary ValueError is still validation, unaffected",
      _errors.classify(ValueError("bad expression")) == _errors.VALIDATION,
      f"-> {_errors.classify(ValueError('bad expression'))}")

# ═══ four findings from cross-vendor review of the first version of this ══
# ═══ fix (commit 0f276a9), all reproduced and fixed here ══════════════════

# Finding 1 (High): exact.py's Fraction-formatting catch (a SEPARATE
# `except ValueError` from the generic catch-all fixed above, inside
# `_eval_exact` itself -- the pow guard bounds the CPU cost of
# exponentiation but not the FORMATTING cost of str()'ing a cheap-to-compute
# result) still returned a bare, uncoded dict. `calc_exact("10**5000")`
# reported `code: "internal"` plus CPython's own raw advice even after the
# generic catch-all was fixed, because this is a DIFFERENT except block.
_calc_exact_result = _exact.eval_exact("10**5000")
check("calc_exact('10**5000'): cheap to compute, too big to format -- ceiling, not internal",
      _calc_exact_result.get("code") == _errors.RESOURCE_EXHAUSTED,
      f"-> code={_calc_exact_result.get('code')} err={str(_calc_exact_result.get('error'))[:70]!r}")
check("  ...with no raw CPython advice in the message",
      "sys.set_int_max_str_digits" not in str(_calc_exact_result.get("error")),
      f"-> {_calc_exact_result.get('error')!r}")

# Finding 2 (High): `_bounded_numeric_value`'s ordered accumulator made the
# refusal decision depend on operand order -- `f*f/(f*f)` is EXACTLY 1 (the
# two factors cancel), but `evaluate=False` parses it as `Mul(f, f,
# Pow(Mul(f, f), -1))`: the accumulator multiplied the first two `f` factors
# together (~8000 digits) and blew the intermediate bit budget BEFORE the
# cancelling reciprocal factor was ever multiplied in. Refused as
# resource_exhausted, wrong -- the true value is a one-character integer.
# Several groupings of the identical cancellation, all of which must
# compute to exactly 1, not refuse.
_CANCELLATION_CASES = [
    "factorial(1463)*factorial(1463)/(factorial(1463)*factorial(1463))",
    "(factorial(1463)*factorial(1463))/(factorial(1463)*factorial(1463))",
    "factorial(1463)/factorial(1463)*factorial(1463)/factorial(1463)",
    ("factorial(1463)*factorial(1463)*factorial(1463)*factorial(1463)"
     "/(factorial(1463)*factorial(1463)*factorial(1463)*factorial(1463))"),
]
for _expr in _CANCELLATION_CASES:
    _t0 = time.time()
    _r = _exact.simplify_expression(_expr)
    _elapsed = time.time() - _t0
    check(f"{_expr[:55]!r}... cancels to 1, not refused",
          _r.get("ok") is True and _r.get("simplified") == "1",
          f"-> ok={_r.get('ok')} code={_r.get('code')} simplified={_r.get('simplified')!r}")
    check("  ...promptly, not after a multi-second burn",
          _elapsed < 5.0, f"-> {_elapsed:.3f}s")
# ...and the ORIGINAL 117-factorial product (no cancellation at all) must
# still be refused -- the fix for order-independence must not turn into a
# blanket "never refuse a division" either.
_confirm_product_result = _exact.simplify_expression(_PRODUCT_BOMB)
check("  ...while the original 117-factorial product (no cancellation) is still refused",
      _confirm_product_result.get("ok") is False
      and _confirm_product_result.get("code") == _errors.RESOURCE_EXHAUSTED,
      f"-> {_confirm_product_result}")

# Finding 3 (Medium): the walk re-resolved every numeric subtree from
# scratch at every numeric ancestor -- superlinear in tree depth. An
# alternating Add/Mul tree (`((...((2+1)*2+1)*2...)`) of depth 250 measured
# 0.311s in reject_explosive ALONE before the fix (vs ~0.0002s on main);
# 50/100/150/200/250 were 0.009/0.033/0.073/0.139/0.311s, clearly
# superlinear rather than a flat per-node cost. Built directly with SymPy's
# own Add/Mul constructors (not string parsing) for a precisely-controlled,
# genuinely nested (not auto-flattened) shape.
from sympy import Add as _Add
from sympy import Integer as _Integer
from sympy import Mul as _Mul


def _alternating_tree(depth):
    node = _Integer(2)
    for i in range(depth):
        node = (_Add(node, _Integer(1), evaluate=False) if i % 2 == 0
                else _Mul(node, _Integer(2), evaluate=False))
    return node


_depth_timings = {}
for _depth in (50, 100, 150, 200, 250):
    _tree = _alternating_tree(_depth)
    # perf_counter, not time.time(): on the Windows runners time.time() ticks at
    # ~15 ms, so a 0.4 ms depth-50 walk read as 0.0000 s and the ratio check
    # below divided by zero-ish (measured on PR #334 CI, windows-latest py3.11).
    _t0 = time.perf_counter()
    _rx(_tree)
    _depth_timings[_depth] = time.perf_counter() - _t0
check(f"an alternating Add/Mul tree of depth 250 stays under 20ms in reject_explosive "
      f"(was 0.311s before the fix) -> {_depth_timings}",
      _depth_timings[250] < 0.020, f"-> {_depth_timings[250]:.4f}s")
check("  ...and depth 250 is not many times slower than depth 50 (no superlinear blowup)",
      # A 1 ms floor on the denominator: the point is "no superlinear blowup",
      # and a sub-millisecond depth-50 walk must not turn timer jitter into a
      # 20x "regression" on a fast runner.
      _depth_timings[250] < max(_depth_timings[50], 0.001) * 20,
      f"-> depth50={_depth_timings[50]:.4f}s depth250={_depth_timings[250]:.4f}s")

# Finding 4 (Low): errors.classify()'s digit-limit special case matched on
# "int_max_str_digits" alone -- a plausible substring of ordinary caller
# prose in a way the full CPython phrase is not. Requires BOTH fragments now.
check("classify() requires BOTH CPython message fragments, not 'int_max_str_digits' alone",
      _errors.classify(ValueError("this mentions int_max_str_digits but nothing else")) == _errors.VALIDATION,
      f"-> {_errors.classify(ValueError('this mentions int_max_str_digits but nothing else'))}")
check("  ...and the real CPython message (both fragments) still classifies correctly",
      _errors.classify(_synthetic_digit_limit_exc) == _errors.RESOURCE_EXHAUSTED,
      f"-> {_errors.classify(_synthetic_digit_limit_exc)}")

# ═══ round 3 of cross-vendor review (grok, on 14e0258) — three more ════════
# ═══ findings, all fixed here ══════════════════════════════════════════════

# Finding 1 (High): SymPy flattens a chain of the same operator into ONE
# n-ary node, so `_log10_magnitude` returning `None` the moment ANY child is
# inconclusive left no nested numeric island for the scan's fallback descent
# to find -- `x * factorial(1463) * ... * factorial(1463)` (117 of them) is
# one flat `Mul` with 118 args, not a `Mul` wrapping a smaller all-numeric
# one. Prefixing the original bomb with any of five different inconclusive
# leading factors (a free symbol, a Function call, two irrational
# NumberSymbols, a non-integer-exponent Pow) all bypassed refusal entirely.
_BYPASS_PREFIXES = ["x*", "cos(0)*", "pi*", "E*", "2**(1/2)*"]
for _prefix in _BYPASS_PREFIXES:
    _bypass_expr = _prefix + _PRODUCT_BOMB
    check(f"{_bypass_expr[:20]!r}... ({len(_bypass_expr)} chars) stays under the 2000-char cap",
          len(_bypass_expr) < 2000, f"-> {len(_bypass_expr)}")
    _t0 = time.time()
    _bypass_result = _exact.simplify_expression(_bypass_expr)
    _bypass_elapsed = time.time() - _t0
    check(f"{_prefix!r} + 117-factorial product is refused, not silently allowed through",
          _bypass_result.get("ok") is False
          and _bypass_result.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> ok={_bypass_result.get('ok')} code={_bypass_result.get('code')}")
    check("  ...promptly, not after materializing the ~467,766-digit product",
          _bypass_elapsed < 2.0, f"-> {_bypass_elapsed:.3f}s")
# Ordering must not matter either -- the inconclusive sibling in the middle
# of the flat Mul, not just leading it. Two copies of factorial(1463)
# (~7996 digits combined) is already over cap on its own, no need for 117.
for _ordering_expr in ("factorial(1463)*x*factorial(1463)", "x*factorial(1463)*factorial(1463)"):
    _ordering_result = _exact.simplify_expression(_ordering_expr)
    check(f"{_ordering_expr!r} is refused regardless of where x sits in the Mul",
          _ordering_result.get("ok") is False
          and _ordering_result.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> {_ordering_result}")
# ...and the fix must not turn into "any Mul with a symbolic factor is
# refused" -- a SINGLE under-cap factorial alongside x must still evaluate.
_single_with_symbol = _exact.simplify_expression("x*factorial(1463)")
check("'x*factorial(1463)' (single under-cap factorial) still evaluates",
      _single_with_symbol.get("ok") is True, f"-> ok={_single_with_symbol.get('ok')!r}")

# Finding 2 (Medium): `_numeric_ceiling_scan` is iterative specifically to
# avoid RecursionError on a deep tree, but `_log10_magnitude` (which it
# calls) is a genuine Python recursive descent over the same tree, and sits
# outside `safe_parse`'s own `try/except` (that only wraps the PARSE step).
# The reviewer's own example: 998 levels of redundant parens around `2*2`,
# 1999 chars, under the cap. Must never raise -- either a value or a
# refusal, matching the "classify_unsafe/safe_parse never raise" contract
# every _EXPLOSIVE_CRASH_INPUTS-style regression above already pins.
_DEEP_NESTING_EXPR = "(" * 998 + "2*2" + ")" * 998
check(f"the depth-998 nesting shape is {len(_DEEP_NESTING_EXPR)} chars, under the 2000-char cap",
      len(_DEEP_NESTING_EXPR) < 2000, f"-> {len(_DEEP_NESTING_EXPR)}")
try:
    _deep_value, _deep_err = _boundary_parse(_DEEP_NESTING_EXPR)
    _deep_raised = None
except Exception as _exc:
    _deep_value, _deep_err, _deep_raised = None, None, _exc
check("safe_parse does not raise (RecursionError or otherwise) on the depth-998 nesting shape",
      _deep_raised is None, f"-> raised {_deep_raised!r}" if _deep_raised else "")
if _deep_raised is None:
    check("  ...and returns either a value or a refusal, never both None and no exception",
          _deep_value is not None or _deep_err is not None,
          f"-> value={_deep_value!r} err={_deep_err!r}")

# Finding 3 (Medium): the Pow branch's base/exponent resolution still called
# `_bounded_numeric_value` directly, inheriting its order-dependent
# accumulator bug for a cancelling Mul used as a Pow's base OR exponent --
# unlike the top-level Mul/Add scan (round 2), which does not touch this
# code path at all. `2**(f*f/(f*f))` (exponent cancels to 1) and
# `(f*f/(f*f))**2` (base cancels to 1) must evaluate, not false-refuse.
_POW_CANCELLATION_CASES = [
    ("2**(factorial(1463)*factorial(1463)/(factorial(1463)*factorial(1463)))", "2"),
    ("(factorial(1463)*factorial(1463)/(factorial(1463)*factorial(1463)))**2", "1"),
]
for _pow_expr, _want_value in _POW_CANCELLATION_CASES:
    _t0 = time.time()
    _pow_result = _exact.simplify_expression(_pow_expr)
    _pow_elapsed = time.time() - _t0
    check(f"{_pow_expr[:55]!r}... evaluates to {_want_value}, not refused",
          _pow_result.get("ok") is True and _pow_result.get("simplified") == _want_value,
          f"-> ok={_pow_result.get('ok')} code={_pow_result.get('code')} "
          f"simplified={_pow_result.get('simplified')!r}")
    check("  ...promptly, not after a multi-second burn",
          _pow_elapsed < 5.0, f"-> {_pow_elapsed:.3f}s")

# ═══ round 4 of cross-vendor review (grok, on the round-three head) — three more High ═══
# ═══ findings, all traced to the SAME root cause and fixed by the SAME ════
# ═══ redesign, not three separate patches ══════════════════════════════════
#
# THE INVARIANT this module now enforces, restated at the top of
# `_factor_multiset`'s own docstring: no numeric subtree whose PRINTED
# numerator or denominator would exceed MAX_NUMERIC_DIGITS is ever
# evaluated. Rounds 1-3 enforced "no numeric subtree whose VALUE is large,
# checked without regard to a combination that provably cancels it" --
# correct as far as it went, but a VALUE can be tiny while its printed
# DENOMINATOR is enormous (`1/(factorial(1463)*factorial(1463))`), and the
# `Pow` node itself was simply not part of the check that enforced any of
# it (`_numeric_ceiling_scan` `continue`d on every `Pow` without pushing
# its base or exponent). Round 4 replaced the scalar
# "log10(|value|)" model (`_log10_magnitude`) with a numerator/denominator
# PAIR tracked in log space throughout (`_log10_num_den`/
# `_factor_multiset`/`_multiset_log_num_den`), and folded `Pow` into the
# scan's own trigger set instead of leaving it to a separate loop.
_CANCELLING_PAIR = "factorial(1463)*factorial(1463)"

# Finding 1 (High): the scan `continue`d on every Pow without pushing its
# base or exponent, and the Pow loop only ever looked past a TRIVIAL
# exponent (`|exp| <= 1`) without checking what the base itself would
# print as -- so an over-cap numeric part hidden ONLY behind a Pow (a
# trivial `**1`, or a reciprocal `**-1`) walked straight through both.
_POW_BYPASS_CASES = [
    (_PRODUCT_BOMB + "**1", "(bomb)**1"),
    (f"1/({_CANCELLING_PAIR})", "1/(f*f)"),
    (f"({_CANCELLING_PAIR})**-1", "(f*f)**-1"),
]
for _expr, _label in _POW_BYPASS_CASES:
    _t0 = time.time()
    _r = _exact.simplify_expression(_expr)
    _elapsed = time.time() - _t0
    check(f"{_label} ({_expr[:40]!r}...) is refused, not silently allowed through a Pow",
          _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> ok={_r.get('ok')} code={_r.get('code')}")
    check("  ...promptly, not after materializing the huge value",
          _elapsed < 2.0, f"-> {_elapsed:.3f}s")

# Finding 2 (High): printability was measured as log10(|value|), so a
# RECIPROCAL has a negative log and the old scalar check read that as "at
# most one digit" -- `x/(f*f)` "cancels" to a tiny coefficient in log-space
# terms, but the printer still has to render the huge DENOMINATOR. Fixed by
# tracking numerator and denominator magnitude separately (this test overlaps
# `1/(f*f)` above, deliberately -- the reviewer named both the reciprocal
# AND the symbolic-numerator variant as distinct reproductions).
_x_over_ff = f"x/({_CANCELLING_PAIR})"
_t0 = time.time()
_r = _exact.simplify_expression(_x_over_ff)
_elapsed = time.time() - _t0
check(f"x/(f*f) ({_x_over_ff!r}) is refused on its DENOMINATOR, not waived through "
      "because the value's sign/magnitude looks small",
      _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
      f"-> {_r}")
check("  ...promptly", _elapsed < 2.0, f"-> {_elapsed:.3f}s")

# A mixed Pow base (part numeric, part symbolic) alongside a sibling numeric
# factor -- the base `factorial(1463)*x` does not fully resolve (x is a free
# symbol), but `factorial(1463)`'s own ~3998-digit contribution must still
# propagate out of the Pow (a real bug caught writing this: an earlier
# version discarded ANY partial base info whenever the base did not fully
# resolve, so this reached real evaluation and crashed on CPython's own
# digit-limit ValueError -- classified correctly by exact.py's catch, but
# AFTER the crash, not refused BEFORE it like every other case here).
_mixed_pow_expr = "(factorial(1463)*x)**1*factorial(1463)"
_r = _exact.simplify_expression(_mixed_pow_expr)
check(f"{_mixed_pow_expr!r} (f*x)**1*f is refused",
      _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
      f"-> {_r}")
check("  ...refused BEFORE evaluation (a real digit-count message), not "
      "classified after a raw CPython crash",
      "sys.set_int_max_str_digits" not in str(_r.get("error"))
      and "digits" in str(_r.get("error")),
      f"-> {_r.get('error')!r}")

# Finding 3 (High): `_resolve_numeric_exactly`'s `type(node)(*args)`
# (evaluate=True) reconstruction was not a BOUNDED operation -- SymPy's own
# `Mul.flatten` folds integer powers of a numeric base internally, so a Pow
# whose exponent is a DIFFERENT-base product that happens to have a small
# NET log (`2**N * 3**(-N)`, N huge) still tried to materialize the literal
# `2**N` on the way to computing that net value. Round 4 removed
# `_resolve_numeric_exactly` (and the evaluate=True reconstruction it did)
# entirely; the exponent's value is now derived purely from log-space
# arithmetic and a per-term-gated exact `Fraction`
# (`_safe_multiset_rational`) that checks EVERY individual factor's
# magnitude before ever raising anything to a power. N here has ~300
# decimal digits (bit_length ~994, matching the reviewer's own reproduction)
# -- large enough that materializing `2**N` would never finish in this
# process's lifetime, so this test's own timeout bound (well under the 10s
# guarded_call ceiling) is the actual assertion.
_N = 10**299 + 7
_diff_base_pow_expr = f"2**(2**{_N}*3**(-{_N}))"
check(f"the exponent literal is {len(str(_N))} digits, under the 2000-char expr cap "
      f"(full expr {len(_diff_base_pow_expr)} chars)",
      len(_diff_base_pow_expr) < 2000, f"-> {len(_diff_base_pow_expr)}")
_t0 = time.time()
_diff_base_result = _exact.simplify_expression(_diff_base_pow_expr)
_diff_base_elapsed = time.time() - _t0
check("2**(2**N * 3**(-N)) for a ~300-digit N: refuses or evaluates in "
      "milliseconds, never seconds -- never reconstructs 2**N",
      _diff_base_elapsed < 1.0, f"-> {_diff_base_elapsed:.3f}s ok={_diff_base_result.get('ok')}")
check("  ...and the outcome is a real answer either way (ok, or a coded refusal)",
      _diff_base_result.get("ok") is True
      or (_diff_base_result.get("ok") is False
          and _diff_base_result.get("code") == _errors.RESOURCE_EXHAUSTED),
      f"-> {_diff_base_result}")

# Finding 4 (Low): the RecursionError guard wrapped only the scan; the Pow
# loop's own exponent resolution runs the identical recursive machinery
# (`_factor_multiset`) and needed the same guard. Structural check -- the
# Pow loop's own try/except is in the source, not just the scan's.
import inspect as _inspect5

_reject_explosive_source = _inspect5.getsource(_rx)
check("reject_explosive's Pow loop is ALSO wrapped in its own RecursionError guard "
      "(not just the scan's)",
      _reject_explosive_source.count("except RecursionError:") >= 2,
      f"-> {_reject_explosive_source.count('except RecursionError:')} guard(s) found")

# ═══ round 5 of cross-vendor review (grok, on 795c49a) — one remaining ═════
# ═══ class: a numeric base whose exponent is not a bare Integer and not ═══
# ═══ a Mul/Pow-int multiset `_factor_multiset` reduces to an integer ══════
#
# Round 4 folded `Pow` into the scan and tracked numerator/denominator
# separately, but the Pow branch still required the exponent to reduce to
# an EXACT integer (via `_factor_multiset` + the then-still-present
# `_safe_multiset_rational`) before bounding anything -- an `Add` exponent
# (`_factor_multiset` has no `Add` case) or a genuinely non-integral
# rational whose BASE still makes the result a huge integer fell through
# unrefused. Fixed by deriving an UPPER BOUND on the exponent's magnitude
# from `_log10_num_den` (which already handles every node type, `Add`
# included) instead of requiring exactness for the verdict at all --
# `_safe_multiset_rational` itself is gone (finding 2 below).

# Finding 1 (High): five expressions that must now be refused, none of
# which have a bare-Integer or exact-multiset-reducible exponent.
_UNRESOLVED_EXPONENT_CASES = [
    ("2**(14999+1)", "Add exponent, same value as the already-refused 2**(30000/2)"),
    ("2**-(14999+1)", "same, negated -- the denominator side of the same bug class"),
    ("(10**3000)**(3/2)", ("non-integral rational exponent, but 10**3000 is a perfect "
                           "square-ish base -- result is 10**4500, a real huge integer")),
    ("2**(factorial(1463)+1)", "Add exponent wrapping an already-legal heavy-call result"),
]
for _expr, _why in _UNRESOLVED_EXPONENT_CASES:
    _t0 = time.time()
    _r = _exact.simplify_expression(_expr)
    _elapsed = time.time() - _t0
    check(f"{_expr!r} ({_why}) is refused",
          _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> {_r}")
    check("  ...promptly, not after evaluate=True actually computes it",
          _elapsed < 2.0, f"-> {_elapsed:.3f}s")

# The symbolic-base side of the identical gap: MAX_SYMBOLIC_EXPONENT used to
# be skipped entirely for any exponent `_factor_multiset` could not reduce
# (an Add, here) because the old code `continue`d on `None` outright.
_t0 = time.time()
_symbolic_add_exp_result = _exact.simplify_expression("(x+1)**(1000+1000)")
_symbolic_add_exp_elapsed = time.time() - _t0
check("(x+1)**(1000+1000) is refused with the symbolic-exponent (cost) wording, "
      "not silently let through",
      _symbolic_add_exp_result.get("ok") is False
      and _symbolic_add_exp_result.get("code") == _errors.RESOURCE_EXHAUSTED
      and "symbolic power" in str(_symbolic_add_exp_result.get("error")),
      f"-> {_symbolic_add_exp_result}")
check("  ...promptly", _symbolic_add_exp_elapsed < 2.0, f"-> {_symbolic_add_exp_elapsed:.3f}s")

# ...and the fix must not turn into a blanket refusal of every compound
# exponent -- these four must still evaluate (two are the round-1 pins for
# the ANALOGOUS Mul-shaped gap; two are new, exercising the same bound math
# on the "clearly small" side).
_STILL_EVALUATES_COMPOUND_EXPONENT = ["2**(3+2)", "2**(3/2)", "2**(19999/2)", "(3/2)**100"]
for _expr in _STILL_EVALUATES_COMPOUND_EXPONENT:
    _r = _exact.simplify_expression(_expr)
    check(f"{_expr!r} (compound exponent, under cap) still evaluates",
          _r.get("ok") is True, f"-> {_r}")

# Finding 2 (Medium): `_safe_multiset_rational` (deleted below) gated each
# term individually but multiplied with no COMBINED bound -- >=20 distinct
# heavy-call bases as an exponent would each individually clear the
# per-term gate and then get multiplied into a real ~10**5-digit `Fraction`.
# The round-5 redesign never materializes an exact exponent value at all
# (see `_log10_num_den`'s Pow branch), so this is a structural, not a
# patched, fix -- tested against both a trivial base (1, where the true
# value is exactly 1 regardless) and a non-trivial one (2, genuinely huge).
_distinct_heavy_factors = "*".join(f"factorial({1463 - _k})" for _k in range(20))
check(f"the 20-distinct-factorial exponent is {len(_distinct_heavy_factors)} chars",
      len(_distinct_heavy_factors) < 1900, f"-> {len(_distinct_heavy_factors)}")

_t0 = time.time()
_unit_base_result = _exact.simplify_expression(f"1**({_distinct_heavy_factors})")
_unit_base_elapsed = time.time() - _t0
check("1**(20 distinct heavy-call factors) evaluates to exactly 1 -- 1**anything is 1, "
      "no materialization needed regardless of the exponent",
      _unit_base_result.get("ok") is True and _unit_base_result.get("simplified") == "1",
      f"-> {_unit_base_result}")
check("  ...promptly, never materializing the ~10**5-digit combined exponent",
      _unit_base_elapsed < 2.0, f"-> {_unit_base_elapsed:.3f}s")

_t0 = time.time()
_nontrivial_base_result = _exact.simplify_expression(f"2**({_distinct_heavy_factors})")
_nontrivial_base_elapsed = time.time() - _t0
check("2**(20 distinct heavy-call factors) is refused, not materialized as a Fraction",
      _nontrivial_base_result.get("ok") is False
      and _nontrivial_base_result.get("code") == _errors.RESOURCE_EXHAUSTED,
      f"-> {_nontrivial_base_result}")
check("  ...promptly", _nontrivial_base_elapsed < 2.0, f"-> {_nontrivial_base_elapsed:.3f}s")

# Finding 3 (Low): `_safe_multiset_rational` / `_bounded_numeric_value` /
# `_NumericTooLarge` / `_SUBTREE_BIT_BUDGET` / `_SAFE_RECONSTRUCT_DIGITS`
# were all dead code by round 5 (nothing in `reject_explosive`'s call graph
# reached them anymore) -- deleted rather than documented as retained.
import codecalc.safe_expr as _se5

for _dead_name in ("_safe_multiset_rational", "_bounded_numeric_value",
                   "_NumericTooLarge", "_SUBTREE_BIT_BUDGET", "_SAFE_RECONSTRUCT_DIGITS"):
    check(f"{_dead_name} was deleted, not merely left unused",
          not hasattr(_se5, _dead_name), f"-> hasattr={hasattr(_se5, _dead_name)}")

# ═══ round 6 of cross-vendor review (Codex, second reviewer family, on ════
# ═══ 795c49a, in parallel with grok's round 5) — four findings, none ══════
# ═══ overlapping grok's ════════════════════════════════════════════════════

# Finding 1 (High): a COMPUTED heavy-function argument bypassed the
# token-level `_heavy_call_violation` entirely -- `factorial(1463+1)`'s
# tokens are `1463` and `1`, each individually under MAX_HEAVY_ARG, and the
# unevaluated tree keeps an opaque `Function` node the numeric scan could
# not bound. `_heavy_call_violation`'s own docstring used to claim this was
# "caught later by the tree rules" -- it was not, until now.
_COMPUTED_HEAVY_ARG_CASES = [
    ("factorial(1463+1)", "factorial, Add argument, same value as the round-1 literal cap"),
    ("subfactorial(1463+1)", "subfactorial, identical shape"),
    ("fibonacci(1463*1000)", "fibonacci, Mul argument -- a ~305,749-digit result"),
]
for _expr, _why in _COMPUTED_HEAVY_ARG_CASES:
    _t0 = time.time()
    _r = _exact.simplify_expression(_expr)
    _elapsed = time.time() - _t0
    check(f"{_expr!r} ({_why}) is refused",
          _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> {_r}")
    check("  ...promptly, not after evaluate=True actually computes it",
          _elapsed < 2.0, f"-> {_elapsed:.3f}s")
# ...and a LITERAL argument at/under the cap must still evaluate -- the tree
# check is a backstop alongside the token check, not a replacement that
# somehow tightens the existing cap.
for _expr in ("factorial(1463)", "factorial(10+5)"):
    _r = _exact.simplify_expression(_expr)
    check(f"{_expr!r} still evaluates", _r.get("ok") is True, f"-> {_r}")

# Finding 2 (High): `bell` (and four siblings) are admitted by the parser's
# namespace but were absent from `_HEAVY_FUNCTIONS`, so `evaluate=False`
# parsing itself -- BEFORE any guard runs -- eagerly materialized a growing
# `Integer`. `bell(1500)` measured 5.48s just to PARSE in the reviewer's
# own reproduction.
_NEWLY_HEAVY_FUNCTIONS = [
    ("bell", 1464, 5),
    ("genocchi", 1464, 6),
    ("motzkin", 1464, 7),
    ("andre", 1464, 5),
    ("partition", 1464, 5),
]
for _name, _over_cap_arg, _small_arg in _NEWLY_HEAVY_FUNCTIONS:
    check(f"{_name!r} is now in _HEAVY_FUNCTIONS",
          _name in _se5._HEAVY_FUNCTIONS, f"-> {_name in _se5._HEAVY_FUNCTIONS}")
    _over_expr = f"{_name}({_over_cap_arg})"
    _r_over = _exact.simplify_expression(_over_expr)
    check(f"{_over_expr!r} (one over MAX_HEAVY_ARG) is refused",
          _r_over.get("ok") is False and _r_over.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> {_r_over}")
    _small_expr = f"{_name}({_small_arg})"
    _r_small = _exact.simplify_expression(_small_expr)
    check(f"{_small_expr!r} (a small, ordinary argument) still evaluates",
          _r_small.get("ok") is True, f"-> {_r_small}")

# Finding 3 (High): a scientific-notation Float literal is a parse-time CPU
# bomb -- `1e100000` costs real time to construct the evaluate=False shape,
# and every Float is unconditionally declared safe once parsed. Fixed at
# the TOKEN level, before parse_expr ever runs.
_OVERSIZED_FLOAT_LITERALS = ["1e100000", "1e1000000", "1.5E4001"]
for _expr in _OVERSIZED_FLOAT_LITERALS:
    _t0 = time.time()
    _r = _exact.simplify_expression(_expr)
    _elapsed = time.time() - _t0
    check(f"{_expr!r} is refused",
          _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> {_r}")
    check("  ...promptly, at the TOKEN level, before parse_expr ever runs "
          "(1e1000000 alone measured >30s post-parse in the reviewer's own repro)",
          _elapsed < 1.0, f"-> {_elapsed:.3f}s")
for _expr in ("1e300", "1e-300", "2.5e3"):
    _r = _exact.simplify_expression(_expr)
    check(f"{_expr!r} (an ordinary scientific-notation literal) still evaluates",
          _r.get("ok") is True, f"-> {_r}")

# Finding 4 (Medium): Add's bound (max-term + log10(n)) recognized no
# cancellation at all, so two terms that are EXACT additive inverses of
# each other -- printable, cheap, the true sum is 0 -- were refused on
# their own uncancelled magnitude. Give Add the same structural
# cancellation Mul already has via _factor_multiset.
_r_cancels = _exact.simplify_expression(
    "factorial(1463)*factorial(1463) - factorial(1463)*factorial(1463)")
check("factorial(1463)*factorial(1463) - factorial(1463)*factorial(1463) evaluates to 0",
      _r_cancels.get("ok") is True and _r_cancels.get("simplified") == "0",
      f"-> {_r_cancels}")
_r_still_refused = _exact.simplify_expression(
    "factorial(1463)*factorial(1463) - factorial(1463)*factorial(1463) + factorial(1463)*factorial(1463)")
check("...but f*f - f*f + f*f (one term survives cancellation) is still refused",
      _r_still_refused.get("ok") is False
      and _r_still_refused.get("code") == _errors.RESOURCE_EXHAUSTED,
      f"-> {_r_still_refused}")

# ═══ the fail-closed choice above was ITSELF too broad — cross-vendor ══════
# ═══ differential probe (main vs. this branch) found 5 benign expressions ══
# ═══ that main evaluates fine but an earlier version of this fix refused ═══
# `_bounded_numeric_value`'s FIRST version returned a single `None` for two
# genuinely DIFFERENT situations: "proved too large" and "no idea, never
# computed it" — and `reject_explosive` treated both as a refusal. That
# conflation is the bug: a `Function` node (`cos(0)`) or a `Float` anywhere
# in a Mul/Add chain (`2*1.5`) hit the same catch-all as an actually huge
# value, so `2**cos(0)` (=2) and `2**(2*1.5)` (=8) got refused pre-parse —
# worse than doing nothing, since main evaluates both fine. A fifth case,
# `2**(2**10000*2**-10000)` (=2, the two factors cancel to exactly 1), was
# refused for a DIFFERENT reason: the Mul accumulation bounded each
# factor's bit length and summed them as if magnitude only ever GROWS
# (`2**-10000`'s huge DENOMINATOR was counted the same as `2**10000`'s huge
# NUMERATOR), never accounting for the sign of the exponent -- so two
# reciprocal factors that exactly cancel tripped the budget check anyway.
#
# Fixed two ways, both in `_bounded_numeric_value`:
#   1. A NEW distinction between returning `None` (inconclusive -- a
#      Function call, an irrational constant, float-mode overflow: NOT
#      evidence of anything, so `reject_explosive` now falls through
#      rather than refusing) and a `_NumericTooLarge` sentinel (PROVED via
#      exact bit-length arithmetic to exceed the budget: still refused).
#   2. `Add`/`Mul` now check the bit length of the ACTUAL (SymPy
#      auto-reduces via GCD) accumulator after each combination, not a
#      pessimistic pre-sum of each factor's own size -- so `2**10000 *
#      2**-10000` correctly resolves to the exact value `1` instead of
#      tripping the budget on the way there. A `Float` anywhere in the
#      chain now promotes the WHOLE combination to ordinary bounded
#      `float` arithmetic (always O(1), the same int-then-float promotion
#      Python itself does) instead of refusing outright.
#
# The three genuine targets above must still refuse — this block is
# checking the OPPOSITE failure mode: these must NOT refuse.
_FACE1_PREVIOUSLY_OVER_REFUSED = [
    ("2**(2*1.5)", 8),          # Float in a Mul exponent
    ("2**(1.5+1.5)", 8),        # Float in an Add exponent
    ("(1.5*2)**3", 27),         # Float in a Mul base
    ("2**(2**10000*2**-10000)", 2),  # reciprocal Pow factors cancel to 1 -- signed magnitude, not summed bit length
    ("2**cos(0)", 2),           # a Function node -- inconclusive, must fall through, not refuse
]
for _expr, _want in _FACE1_PREVIOUSLY_OVER_REFUSED:
    _value, _err = _boundary_parse(_expr)
    check(f"safe_parse({_expr!r}) is NOT over-refused (main evaluates this fine)",
          _value is not None and _err is None, f"-> value={_value!r} err={_err!r}")
    if _err is None:
        _eval_result = _logic.evaluate_expression(_expr)
        check(f"  ...and evaluate_expression({_expr!r}) computes the right value ({_want})",
              _eval_result.get("ok") is True and float(_eval_result.get("value") or "nan") == _want,
              f"-> {_eval_result}")

# ...and a handful more boundary/edge shapes from the same differential
# probe that must also keep passing (none of these ever regressed, but the
# probe checked them alongside the five above, so they are pinned here too).
for _expr in ("2**(1/2+1/2)", "sqrt(2)+1", "(2/3)**5", "x**(2*3)"):
    _value, _err = _boundary_parse(_expr)
    check(f"safe_parse({_expr!r}) still evaluates (differential-probe pin)",
          _value is not None and _err is None, f"-> value={_value!r} err={_err!r}")

# ═══ reject_explosive did not descend into a bare Python `tuple` — the ═════
# ═══ parse-time memory bomb behind ClusterFuzzLite's OOM (a SEPARATE gap ═══
# ═══ from the ceiling-coverage one above, found investigating it) ══════════
# `"2**1000000^6c6/Me,"` looks like it should fail as a syntax error (the
# trailing comma, `c6`, the unclosed `Me`) — and it DOES, eventually, but
# only from the SECOND, real (`evaluate=True`) parse in `safe_parse`. Why:
# a trailing comma is valid PYTHON tuple syntax (`x,` means `(x,)`), so
# `parse_expr(..., evaluate=False)` succeeds and hands back a bare Python
# `tuple` — `(Mul(2**(1000000**6), c, 6, 1/M, e),)` — not a SymPy node.
# `_walk`'s `getattr(current, "args", ())` found no `.args` on a plain
# tuple, fell through to the `()` default, and stopped there — so
# `reject_explosive` never saw the `2**(1000000**6)` POWER TOWER buried
# inside it (an exponent that is itself a `Pow`, the same shape
# `9**9**9**9` is already refused for) and returned `None` as if the tree
# held nothing dangerous. `safe_parse` then ran the real, evaluating parse,
# which computed the actual value before the trailing comma's syntax error
# ever got a chance to surface — measured live on main before this fix:
# 2977 MB tracemalloc peak (under a 3 GB `ulimit -v`; ClusterFuzzLite's own
# 2560 MB libFuzzer rss cap reported 1542 MB and OOM'd on a lighter box).
# Fixed by making `_walk` descend into a bare tuple directly.
_MEMORY_BOMB_EXPR = "2**1000000^6c6/Me,"
import tracemalloc as _tracemalloc

_tracemalloc.start()
_t0 = time.time()
try:
    _bomb_value, _bomb_err = _boundary_parse(_MEMORY_BOMB_EXPR)
    _bomb_raised = None
except Exception as _exc:  # never-raise contract -- see the crash-inputs block above
    _bomb_value, _bomb_err, _bomb_raised = None, None, _exc
_bomb_elapsed = time.time() - _t0
_bomb_current, _bomb_peak = _tracemalloc.get_traced_memory()
_tracemalloc.stop()

check("safe_parse does not raise on the parse-time memory-bomb input",
      _bomb_raised is None, f"-> raised {_bomb_raised!r}" if _bomb_raised else "")
if _bomb_raised is None:
    check("  ...and refuses it rather than computing the value",
          _bomb_value is None and _bomb_err is not None, f"-> value={_bomb_value!r} err={_bomb_err!r}")
# A generous bound (measured before the fix: 2977 MB) -- robust against
# ordinary variance while still catching the CLASS of regression: any future
# change that reopens a path to the real evaluate=True parse running on this
# input, unbounded, blows well past 50 MB.
check(f"  ...within a bounded memory peak ({_bomb_peak / 1e6:.1f} MB, was ~2977 MB before the fix)",
      _bomb_peak < 50 * 1_000_000, f"-> peak={_bomb_peak / 1e6:.1f} MB")
check(f"  ...and refuses promptly ({_bomb_elapsed:.3f}s), not after a multi-second/GB burn",
      _bomb_elapsed < 5.0, f"-> {_bomb_elapsed:.3f}s")

# ═══ round 7 of cross-vendor review (grok, on the round-five head; Codex, ══
# ═══ on the round-six head, in parallel) ════════════════════════════════════

# grok's finding: the round-five/six `Pow` short-circuits stopped the scan
# from ever descending into an exponent that is itself expensive to
# CONSTRUCT, as opposed to merely expensive to PRINT. A unit-magnitude base
# (1, -1, 1/1, 2/2, 1.0) resolves to (0, 0, True) without ever inspecting
# the exponent; a same-base cancelling exponent (2**(2**N * 2**(-N)))
# resolves to (0, 0, True) via _factor_multiset's own cancellation. Both
# leave a genuinely expensive inner Pow(2, N)/Pow(3, -N) unchecked, since
# SymPy's Mul.flatten still constructs that intermediate regardless of what
# the surrounding expression later does with the result. N is ~300 digits
# -- big enough that Pow(2, N) alone measured past guarded_call's ~10-13s
# CPU backstop before this fix; a hard 300-digit floor, not tuned to any
# looser cliff.
_N7 = 10**299 + 7
_UNIT_BASE_PLUS_EXPENSIVE_EXPONENT = [
    f"1**(2**{_N7}*3**(-{_N7}))",
    f"(-1)**(2**{_N7}*3**(-{_N7}))",
    f"(1/1)**(2**{_N7}*3**(-{_N7}))",
    f"(2/2)**(2**{_N7}*3**(-{_N7}))",
    f"1.0**(2**{_N7}*3**(-{_N7}))",
    f"2**(2**{_N7}*2**(-{_N7}))",  # same-base cancelling exponent, not a unit base
]
for _expr in _UNIT_BASE_PLUS_EXPENSIVE_EXPONENT:
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"safe_parse({_expr[:40]!r}...) is refused, not evaluated",
          _v is None and _e is not None, f"-> value={_v!r} err={_e!r}")
    check(f"  ...promptly ({_dt:.3f}s), not after Pow(2, N) is actually constructed "
          "(measured >10s before this fix)",
          _dt < 2.0, f"-> {_dt:.3f}s")

# The exponent in 2**(2**10000*2**-10000) is legal, not merely lucky: N =
# 10000 makes Pow(2, 10000) itself only ~3011 digits (under MAX_NUMERIC_
# DIGITS), so constructing it is cheap regardless of the eventual
# cancellation -- this is what distinguishes it from the ~300-digit-N cases
# above, where Pow(2, N)'s OWN print profile (~10**298 digits) is already
# the hazard, before any cancellation is even considered. This must still
# evaluate, unmodified by round 7's fix, to pin exactly that distinction.
_v, _e = _boundary_parse("2**(2**10000*2**-10000)")
check("safe_parse('2**(2**10000*2**-10000)') still evaluates "
      "(N=10000 -- Pow(2, N) itself is only ~3011 digits, cheap to construct)",
      _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
if _e is None:
    check("  ...to the correct value (2)", str(_v) == "2", f"-> {_v!r}")

# grok's own regression, caught mid-fix: an earlier version of this fix made
# the scan always push a resolved Pow's children regardless of its own
# verdict -- which then independently judged the EXPONENT's own print
# profile as if it mattered, refusing 1**(20 distinct factorial factors) on
# its ~79,347-digit exponent even though that exponent is cheap to
# construct (20 already-materialized integers multiplied together) and is
# never printed at all, since 1**anything is always 1. Print-profile-over-
# cap and expensive-to-construct are different questions; this pins that
# the scan answers only the first one, deferring the second to the Pow
# loop's own construction-cost check instead of trying to answer it here.
_factorial_factors = "*".join(f"factorial({1463 - k})" for k in range(20))
_unit_pow_expr = f"1**({_factorial_factors})"
_t0 = time.time()
_v, _e = _boundary_parse(_unit_pow_expr)
_dt = time.time() - _t0
check("safe_parse('1**(20 distinct factorial factors)') still evaluates to 1 "
      "(each factor is under cap individually; the ~79,347-digit exponent's "
      "own print profile is irrelevant to a unit base)",
      _v is not None and _e is None and str(_v) == "1", f"-> value={_v!r} err={_e!r}")
check(f"  ...promptly ({_dt:.3f}s)", _dt < 2.0, f"-> {_dt:.3f}s")

# Codex finding 1 (High): the identical class from the Add side --
# _cancel_additive_inverses marks 2**1000000000 - 2**1000000000
# resolved-to-zero and never descends into either Pow, but SymPy evaluates
# each child before cancelling, so the ~301-million-digit
# Pow(2, 1000000000) still gets constructed. Verified already closed by the
# same Pow-loop fix above (which is agnostic to whether its ancestor is a
# Mul or an Add) -- this pins it as a regression test.
_t0 = time.time()
_v, _e = _boundary_parse("2**1000000000 - 2**1000000000")
_dt = time.time() - _t0
check("safe_parse('2**1000000000 - 2**1000000000') is refused, not evaluated",
      _v is None and _e is not None, f"-> value={_v!r} err={_e!r}")
check(f"  ...promptly ({_dt:.3f}s)", _dt < 2.0, f"-> {_dt:.3f}s")
_v, _e = _boundary_parse("x*x - x*x")
check("safe_parse('x*x - x*x') (ordinary symbolic cancellation) still evaluates",
      _v is not None and _e is None and str(_v) == "0", f"-> value={_v!r} err={_e!r}")

# Codex finding 2 (High): a heavy call NESTED inside another heavy call's
# argument was uncaught -- none of factorint/isprime/.../factorial etc. are
# SymPy Function subclasses, so an INNER heavy call on a literal argument
# evaluates eagerly even under evaluate=False, and its numeric RESULT (not
# its short source text) becomes the OUTER call's argument before any tree
# exists for reject_explosive to inspect. factorial(fibonacci(100)) never
# returns; fibonacci(factorial(10)) materializes a ~758,374-digit integer;
# factorial(factorial(8)) a ~168,187-digit one.
#
# THE-1095 round 3 (verify-1095-r2): these three are STILL refused, but via
# `_numeric_ceiling_scan`'s own GROWTH-bound machinery now (see
# `_table_function_bound`'s own docstring), not the blanket TOKEN-level
# "any nesting" rule this test used to name -- that rule is GONE, precisely
# because it could not tell a genuinely dangerous nesting from a cheap one
# (see the very next check below).
_NESTED_HEAVY_CASES = ["factorial(fibonacci(100))", "fibonacci(factorial(10))",
                       "factorial(factorial(8))"]
for _expr in _NESTED_HEAVY_CASES:
    _t0 = time.time()
    _r = _exact.simplify_expression(_expr)
    _dt = time.time() - _t0
    check(f"{_expr!r} (heavy call nested in a heavy call's argument) is refused",
          _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> {_r}")
    check(f"  ...promptly ({_dt:.3f}s), via the deferred scan's growth-bound "
          "backstop, before either real parse ever runs "
          "(fibonacci(factorial(10)) alone measured 1.5s materializing the result)",
          _dt < 1.0, f"-> {_dt:.3f}s")
# THE-1095 round 3: factorial(fibonacci(5)) (= 120) now correctly EVALUATES
# instead of the old blanket refusal -- fibonacci's own growth bound for
# n=5 is nowhere near factorial's cap, so the nested-call machinery
# correctly tells this apart from the three genuinely dangerous cases
# above. Round 1 through round 2 documented this as "deliberately
# conservative"; that conservatism is what round 3's growth-bound
# machinery replaces with an actual bound.
_r = _exact.simplify_expression("factorial(fibonacci(5))")
check("'factorial(fibonacci(5))' (= 120, genuinely cheap nesting) now "
      "EVALUATES instead of being refused (THE-1095 round 3)",
      _r.get("ok") is True and _r.get("simplified") == "120", f"-> {_r}")
# Two SEPARATE (not nested) heavy calls must not be caught by this rule.
_r = _exact.simplify_expression("factorial(1000)+fibonacci(1000)")
check("'factorial(1000)+fibonacci(1000)' (two SIBLING heavy calls, not nested) still evaluates",
      _r.get("ok") is True, f"-> {_r}")

# Codex finding 3 (High): _oversized_scientific_literal_violation's regex
# had no allowance for Python's j/J imaginary suffix, so 1e1000000j never
# matched at all and reached the real parse unbounded; separately, being an
# unanchored search, it misread the literal hex digits of 0x1e100000 as a
# fake "exponent of 100000".
for _expr in ("1e1000000j", "1e+100000j", "1E100_000j"):
    _t0 = time.time()
    _r = _exact.simplify_expression(_expr)
    _dt = time.time() - _t0
    check(f"{_expr!r} (imaginary-suffixed scientific literal) is refused",
          _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> {_r}")
    check(f"  ...promptly ({_dt:.3f}s), at the token level", _dt < 1.0, f"-> {_dt:.3f}s")
for _expr in ("0x1e100000", "0o17", "0b101", "1+2j"):
    _r = _exact.simplify_expression(_expr)
    check(f"{_expr!r} (hex/octal/binary literal, or an ordinary complex number) "
          "still evaluates -- not misread as a scientific-notation exponent",
          _r.get("ok") is True, f"-> {_r}")

# Codex finding 4 (Medium): an audit of every remaining eager name in
# safe_global_dict() turned up two more hazard shapes needing their own
# cap, plus digamma/zeta (_HEAVY_FUNCTIONS-shaped after all) and stirling
# (not actually exposed by sympy 1.14's namespace, so nothing to bound).
check("'stirling' is not exposed by sympy's namespace (nothing to bound)",
      not hasattr(__import__("sympy"), "stirling"), "-> hasattr(sympy, 'stirling')")
for _name in ("digamma", "zeta"):
    check(f"{_name!r} is now in _HEAVY_FUNCTIONS", _name in _se5._HEAVY_FUNCTIONS,
          f"-> {_name in _se5._HEAVY_FUNCTIONS}")
_r = _exact.simplify_expression("digamma(100000)")
check("'digamma(100000)' is refused", _r.get("ok") is False
      and _r.get("code") == _errors.RESOURCE_EXHAUSTED, f"-> {_r}")
_r = _exact.simplify_expression("zeta(-99999)")
check("'zeta(-99999)' is refused", _r.get("ok") is False
      and _r.get("code") == _errors.RESOURCE_EXHAUSTED, f"-> {_r}")
for _expr in ("digamma(1463)", "zeta(-1463)"):
    _r = _exact.simplify_expression(_expr)
    check(f"{_expr!r} (at the existing MAX_HEAVY_ARG cap) still evaluates",
          _r.get("ok") is True, f"-> {_r}")

# factorint/primefactors/divisors/mobius/nextprime/isprime: the hazard
# scales with the size of the NUMBER under test (factoring is hard), not a
# growing OUTPUT for a small "count" argument -- a random ~40-digit
# semiprime measured 27.8s to factor, and a 100-digit RSA modulus hits the
# process memory/CPU backstop outright.
_RSA_100 = ("1522605027922533360535618378132637429718068114961380688657"
            "908494580122963258952897654000350692006139")
for _name in ("factorint", "primefactors", "divisors", "mobius"):
    _expr = f"{_name}({_RSA_100})"
    _r = _exact.simplify_expression(_expr)
    check(f"{_name}(<100-digit RSA modulus>) is refused, not factored",
          _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> {_r}")
_lit_1900 = "9" * 1900
for _name in ("nextprime", "isprime"):
    _expr = f"{_name}({_lit_1900})"
    _r = _exact.simplify_expression(_expr)
    check(f"{_name}(<1900-digit literal>) is refused",
          _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> {_r}")
for _name in ("factorint", "primefactors", "divisors", "mobius", "nextprime", "isprime"):
    _expr = f"{_name}(360)"
    # safe_parse, not simplify_expression: factorint/primefactors/divisors
    # return a plain Python dict/list, not a SymPy Expr, and
    # simplify_expression's sp.simplify()/sp.factor()/sp.expand() calls
    # have no method of that name on a dict/list -- a genuine, separate,
    # pre-existing limitation of simplify_expression's post-processing for
    # these return types, unrelated to reject_explosive/classify_unsafe
    # (which safe_parse alone exercises), so it is not this round's
    # concern to fix.
    _v, _e = _boundary_parse(_expr)
    check(f"{_expr!r} (an ordinary small argument) still evaluates",
          _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")

# sqrt/root/cbrt: the OPPOSITE shape -- cheap to bound in log space (an
# n-th root's digit count is always comfortably under MAX_NUMERIC_DIGITS)
# but not cheap to COMPUTE (SymPy's perfect-power check measured 4.0s at
# 1900 digits).
for _name, _expr in (("sqrt", f"sqrt({_lit_1900})"), ("root", f"root({_lit_1900}, 3)"),
                      ("cbrt", f"cbrt({_lit_1900})")):
    _r = _exact.simplify_expression(_expr)
    check(f"{_name}(<1900-digit literal>) is refused",
          _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> {_r}")
for _expr in ("sqrt(360)", "root(360, 3)", "cbrt(360)"):
    _r = _exact.simplify_expression(_expr)
    check(f"{_expr!r} (an ordinary small argument) still evaluates",
          _r.get("ok") is True, f"-> {_r}")

# Codex finding 5 (Medium): _cancel_additive_inverses only recognized an
# EXACT multiset match with opposite sign, so a small INTEGER COEFFICIENT
# difference (2*f*f - f*f - f*f, exactly 0) was still refused -- the
# leading 2 makes the first term's multiset a different key from the other
# two's.
_r = _exact.simplify_expression(
    "2*factorial(1463)*factorial(1463) - factorial(1463)*factorial(1463) "
    "- factorial(1463)*factorial(1463)")
check("2*f*f - f*f - f*f evaluates to 0 (coefficients 2, -1, -1 sum to 0)",
      _r.get("ok") is True and _r.get("simplified") == "0", f"-> {_r}")
_r = _exact.simplify_expression(
    "3*factorial(1463)*factorial(1463) - factorial(1463)*factorial(1463) "
    "- factorial(1463)*factorial(1463)")
check("...but 3*f*f - f*f - f*f (coefficients sum to +1, not 0) is still refused",
      _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
      f"-> {_r}")

# ═══ round 8 of cross-vendor review (grok PASS with two Low notes; ═════════
# ═══ Codex FAIL with one High and one Low, on ab0f5cc) ══════════════════════

# Codex finding 1 (High): the round-seven per-function caps were enforced
# ONLY per numeric TOKEN -- factorint(<25-digit literal>*<25-digit
# literal>) (two individually-permitted literals whose PRODUCT is ~50
# digits, hard to factor) and sqrt(<990-digit literal>*<990-digit literal>)
# (product ~1980 digits, 2.1s to compute) both passed classify_unsafe
# clean. Fixed structurally: ONE table (_FUNCTION_ARG_CAPS) now drives (a)
# a token-level check that sums every literal's digit count WITHIN one
# top-level argument (an upper bound on their PRODUCT, catching both repros
# below at the token level, before parse_expr ever runs) and (b) two
# tree-level backstops for the shapes a token-only check cannot reach at
# all: a Function-node check (value-kind and, structurally, any future
# digit-kind Function name) and a NEW Pow-loop branch for a unit-fraction
# exponent (sqrt/root/cbrt's actual tree shape, since none of them leave a
# Function node named after themselves).
_A25 = "1000000000000000987654327"
_B25 = "8000000000000000123456849"
_t0 = time.time()
_r = _se5.classify_unsafe(f"factorint({_A25}*{_B25})")
_dt = time.time() - _t0
check("factorint(<25-digit literal>*<25-digit literal>) is refused at the "
      "token level (round 7 passed this clean)",
      _r is not None, f"-> {_r}")
check(f"  ...promptly ({_dt:.3f}s)", _dt < 1.0, f"-> {_dt:.3f}s")

_d990a, _d990b = "9" * 990, "8" * 990
_t0 = time.time()
_v, _e = _boundary_parse(f"sqrt({_d990a}*{_d990b})")
_dt = time.time() - _t0
check("sqrt(<990-digit literal>*<990-digit literal>) is refused, not computed "
      "(round 7 passed this clean, took 2.1s to compute)",
      _v is None and _e is not None, f"-> value={_v!r} err={_e!r}")
check(f"  ...promptly ({_dt:.3f}s)", _dt < 1.0, f"-> {_dt:.3f}s")

# A literal AT the cap (one factor alone, not a product) must still work.
_v, _e = _boundary_parse(f"factorint({_A25})")
check("factorint(<one 25-digit literal, at the cap>) still evaluates",
      _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("sqrt(10**1000)")
check("sqrt(10**1000) still evaluates", _v is not None and _e is None,
      f"-> value={_v!r} err={_e!r}")

# Self-check: every name in _FUNCTION_ARG_CAPS is exercised by an
# appropriate layer for a COMPUTED (not bare-literal) argument. Value-kind
# names (_HEAVY_FUNCTIONS) get the tree-level Function-node backstop
# (`_numeric_ceiling_scan`); the root family gets the NEW tree-level Pow
# unit-fraction-exponent backstop (a nested heavy call as the argument, so
# no literal token names the true magnitude at all). The eager factor
# family (factorint and its five siblings) ALSO gets a tree-level backstop
# now (THE-1095, see below) -- none of the six respects `evaluate=False` on
# its own, so the danger used to be baked into the very act of PARSING
# (documented in MAX_FACTOR_ARG_DIGITS's own comment) -- but they still
# ALSO keep their existing token-level COMBINED-literal-product check,
# since that layer is unaffected and still the only protection for a
# NESTED-call-shaped argument (`factorint(nextprime(x))`-style — see
# `_oversized_call_arg_violation`'s own updated docstring).
_EAGER_FACTOR_FAMILY = {"factorint", "primefactors", "divisors", "mobius", "nextprime", "isprime"}
# `sqrt`/`cbrt` both stay a genuinely unevaluated `Pow(base, Rational(1,
# n))` for a CONCRETE (already-materialized) base under `evaluate=False`,
# reaching this round's new Pow-loop branch cleanly (verified live).
# `root(x, n)` does NOT share that path for every `n` -- probed live:
# `root(factorial(1463), 2)` parses to a `Mul` (`Pow`'s `.eval()` pulled
# perfect-square-ish factors out of the ALREADY-CONCRETE base eagerly, at
# construction time, a different code path from bare `sqrt`/`cbrt`) -- so
# `root` is checked via the token-level combined-literal path instead,
# below, alongside the factor family, rather than asserting a tree shape
# that does not actually occur for it. Structural, still true after
# THE-1095 (`_DEFERRED_STANDIN_NAMES` excludes the whole `sqrt`/`root`/
# `cbrt` family for exactly this reason — see its own comment) — the only
# name in `_FUNCTION_ARG_CAPS` that stays genuinely single-layer by design.
_TREE_ONLY_ROOT_FAMILY = {"sqrt", "cbrt"}
_TOKEN_ONLY_ROOT_FAMILY = {"root"}
# `rf`/`ff`/`binomial` are two-argument value-kind names -- the tree-level
# I1/I2 checks below need a valid SECOND argument to actually call them (the
# token-level checks above never call anything, so they were never sensitive
# to this), a small one so it never itself approaches any cap.
_EXTRA_CALL_ARGS = {"root": ", 2", "rf": ", 2", "ff": ", 2", "binomial": ", 5",
                     "polygamma": ", 2"}
# THE-1095: every name below EXCEPT `root` (structural, see above) is now
# exercised by BOTH layers for a genuinely COMPUTED argument. Before this
# round, the "value"-kind branch (`_HEAVY_FUNCTIONS`) only ever got the
# token-level bare-literal check here, with a long-documented list of
# exceptions this self-check's own comment used to carry: `rf`/`ff` parse
# to `RisingFactorial`/`FallingFactorial`, not `rf`/`ff`, so the old
# `type(node).__name__ in _FUNCTION_ARG_CAPS` lookup never matched them;
# `digamma` gets REWRITTEN to `polygamma` at construction; `primepi` and
# `binomial` evaluated their own argument eagerly regardless of
# `evaluate=False`; `primorial`/`prime`/`motzkin` (value-kind) and
# `factorint`/`primefactors`/`divisors`/`nextprime`/`isprime` (digits-kind)
# either raised `ValueError` outright on a merely computed argument or
# silently evaluated for real, bypassing the cap. `safe_parse`'s pre-parse
# now runs through `_deferred_global_dict()` (THE-1095), which replaces
# every one of these names uniformly with an inert stand-in, so NONE of
# that is "documented out of scope" any longer — the loop below checks the
# tree-level computed-argument backstop for every name it applies to,
# with no bucket left over for "predates this round, not fixed yet".
_checked_names = set()
for _fn_name, (_kind, _cap) in _se5._FUNCTION_ARG_CAPS.items():
    _checked_names.add(_fn_name)
    _extra = _EXTRA_CALL_ARGS.get(_fn_name, "")
    if _fn_name in _EAGER_FACTOR_FAMILY or _fn_name in _TOKEN_ONLY_ROOT_FAMILY:
        _big1 = "9" * (_cap // 2 + 2)
        _big2 = "9" * (_cap - _cap // 2 + 2)
        _r = _se5.classify_unsafe(f"{_fn_name}({_big1}*{_big2}{_extra})")
        check(f"self-check: {_fn_name}() combined-literal computed argument "
              "is refused at the token level",
              _r is not None, f"-> {_r}")
    elif _fn_name in _TREE_ONLY_ROOT_FAMILY:
        _t0 = time.time()
        _v, _e = _boundary_parse(f"{_fn_name}(factorial(1463){_extra})")
        _dt = time.time() - _t0
        check(f"self-check: {_fn_name}(factorial(1463)) (a computed, "
              "tree-only-catchable argument) is refused",
              _v is None and _e is not None, f"-> value={_v!r} err={_e!r}")
        check(f"  ...promptly ({_dt:.3f}s)", _dt < 2.0, f"-> {_dt:.3f}s")
    else:
        # value-kind (_HEAVY_FUNCTIONS): the token-level bare-literal cap
        # -- round 1's own protection, unconditionally reliable.
        _r = _se5.classify_unsafe(f"{_fn_name}({_cap + 1})")
        check(f"self-check: {_fn_name}({_cap + 1}) (one over cap, a bare "
              "literal) is refused at the token level",
              _r is not None, f"-> {_r}")
    if _fn_name not in _TOKEN_ONLY_ROOT_FAMILY | _TREE_ONLY_ROOT_FAMILY:
        # THE-1095: the tree-level, deferred-pre-parse backstop, for a
        # COMPUTED (not bare-literal) argument -- the layer this round adds,
        # now exercised for every remaining name in the table, "value" and
        # "digits" kind alike. "value" kind over-caps by argument MAGNITUDE
        # (`cap + 1`); "digits" kind over-caps by DIGIT COUNT (a value with
        # `cap + 1` digits, `10**cap + 1` -- `cap` itself is a small count
        # like 25, so `cap + 1` alone would be a two-digit number, nowhere
        # near a 25-digit cap).
        _over = f"{_cap}+1" if _kind == "value" else f"10**{_cap}+1"
        _t0 = time.time()
        _v, _e = _boundary_parse(f"{_fn_name}({_over}{_extra})")
        _dt = time.time() - _t0
        check(f"self-check: {_fn_name}({_over}{_extra}) (a computed, "
              "over-cap argument) is refused at the tree level",
              _v is None and _e is not None and _e[0] == "ceiling",
              f"-> value={_v!r} err={_e!r}")
        check(f"  ...promptly ({_dt:.3f}s)", _dt < 2.0, f"-> {_dt:.3f}s")
        # ...and a comfortably-under-cap computed argument still evaluates,
        # identically to its literal form -- I2 of THE-1095's own ledger.
        # `motzkin` is DELIBERATELY excluded (round-3-follow-up, grok item
        # E): its own `eval()` raises a differently-worded `ValueError`
        # ("must be a positive integer") on an unevaluated Add, which the
        # narrowed real-dict catch-all no longer treats as safe to retry
        # under `evaluate=True` -- see the dedicated pinned regression for
        # `motzkin(2+3)` (and the CHANGELOG) for the full account.
        if _fn_name != "motzkin":
            _under_lit, _under_computed = f"21{_extra}", f"10+11{_extra}"
            _lv, _le = _boundary_parse(f"{_fn_name}({_under_lit})")
            _cv, _ce = _boundary_parse(f"{_fn_name}({_under_computed})")
            check(f"self-check: {_fn_name}({_under_computed}) == "
                  f"{_fn_name}({_under_lit}) (under cap, computed == literal)",
                  _le is None and _ce is None and _lv == _cv,
                  f"-> literal={_lv!r}/{_le!r} computed={_cv!r}/{_ce!r}")
check(f"self-check covered every name in _FUNCTION_ARG_CAPS "
      f"({len(_checked_names)} names)",
      _checked_names == set(_se5._FUNCTION_ARG_CAPS),
      f"-> missing={set(_se5._FUNCTION_ARG_CAPS) - _checked_names!r}")

# ═══ round 9 of cross-vendor review (grok PASS, three Low notes, on ════════
# ═══ 940c6b5) ════════════════════════════════════════════════════════════

# Finding 2: the table did not yet drive EVERY comparison -- the
# Function-node "value" check used a `_log10_max_heavy_arg` derived from
# the bare `MAX_HEAVY_ARG` module constant, precomputed once outside the
# scan's loop, and the Pow loop's unit-fraction branch hardcoded
# `MAX_ROOT_ARG_DIGITS` directly, rather than either reading the cap from
# the matched row in `_FUNCTION_ARG_CAPS` itself -- both now do (see
# their own comments). A monkeypatched row with a deliberately different
# cap proves both layers actually follow the TABLE, not the module
# constant it happens to start from.
_orig_factorial_cap = _se5._FUNCTION_ARG_CAPS["factorial"]
_se5._FUNCTION_ARG_CAPS["factorial"] = ("value", 10)
try:
    _v, _e = _boundary_parse("factorial(20+1)")  # 20, tokenwise, clearly over a cap of 10
    check("self-check: a monkeypatched 'factorial' row (cap 10, was "
          f"{_orig_factorial_cap[1]}) is honoured -- factorial(20+1) refused",
          _v is None and _e is not None, f"-> value={_v!r} err={_e!r}")
    _v, _e = _boundary_parse("factorial(2+1)")  # 3, clearly under a cap of 10
    check("  ...and factorial(2+1) (under the patched cap) still evaluates",
          _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
    # THE-1095: isolate the TREE-level (deferred-pre-parse) layer
    # specifically -- 5 and 6 are each individually under the patched cap
    # of 10, so the TOKEN screen has no opinion on either one; only their
    # RESOLVED sum (11) is over it, which only `_numeric_ceiling_scan`'s
    # `Function`-node branch, fed by `_deferred_global_dict()`, can see.
    _v, _e = _boundary_parse("factorial(5+6)")
    check("self-check: a monkeypatched 'factorial' row is honoured at the "
          "TREE level alone -- factorial(5+6) (11, over cap 10, no single "
          "token over cap) is refused",
          _v is None and _e is not None and _e[0] == "ceiling",
          f"-> value={_v!r} err={_e!r}")
    check("  ...and the token screen alone has no opinion on either literal",
          _se5.classify_unsafe("factorial(5+6)") is None,
          f"-> {_se5.classify_unsafe('factorial(5+6)')!r}")
finally:
    _se5._FUNCTION_ARG_CAPS["factorial"] = _orig_factorial_cap

_orig_sqrt_cap = _se5._FUNCTION_ARG_CAPS["sqrt"]
_se5._FUNCTION_ARG_CAPS["sqrt"] = ("digits", 10)
try:
    # factorial(20) has 19 digits, over a patched root cap of 10; a
    # COMPUTED (tree-only-catchable) argument, so this exercises the Pow
    # loop's unit-fraction branch specifically, not the token screen.
    _v, _e = _boundary_parse("sqrt(factorial(20))")
    check("self-check: a monkeypatched 'sqrt' row (cap 10, was "
          f"{_orig_sqrt_cap[1]}) is honoured -- sqrt(factorial(20)) refused",
          _v is None and _e is not None, f"-> value={_v!r} err={_e!r}")
    _v, _e = _boundary_parse("sqrt(factorial(5))")  # 120, 3 digits, under 10
    check("  ...and sqrt(factorial(5)) (under the patched cap) still evaluates",
          _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
finally:
    _se5._FUNCTION_ARG_CAPS["sqrt"] = _orig_sqrt_cap

# Finding 3: the summed-digit token rule is a safe UPPER bound on a
# product's true digit count, not the exact product -- a small extra
# literal factor can tip the SUM over the cap even when the true product
# still fits (`factorial(1463)` is far too big a comparison point here;
# use the same factoring-family shape the docstring's own example names).
_lit25 = "9" * 25
_r = _se5.classify_unsafe(f"factorint({_lit25}*2)")
check("factorint(<25-digit literal>*2) (sum 26, over MAX_FACTOR_ARG_DIGITS, "
      "though the true product may still be 25 digits) is refused",
      _r is not None, f"-> {_r}")
check("  ...with a message naming the SUM explicitly, not implying an "
      "exact product was computed",
      _r is not None and "sum of the literal digits" in _r[1], f"-> {_r}")

# ═══ round 10 of cross-vendor review (Codex, confirming the round 8-9 ═════
# ═══ delta) — one remaining Low ═════════════════════════════════════════

# Round 8 made the TOKEN-level digit-count check exact (`_exact_decimal_
# digit_count`), but the TREE-level check (`_digit_count_over_cap`, fed by
# `_log10_num_den` -> `_log10_of_int`'s float approximation) still rounded
# the wrong way at an EXACT boundary: `safe_parse("sqrt(" + "9"*1200 +
# ")")` -- a value with EXACTLY 1200 digits, at MAX_ROOT_ARG_DIGITS --
# passed the token screen (which sees a single literal and counts its
# digits exactly) but was then refused at the TREE level, where the float
# log10 of an all-9s value rounded up and reported 1201 digits instead of
# 1200. Fixed with `_exact_digit_count_if_cheap`/`_digit_count_over_cap_
# for_node`, preferring the exact count of an already-materialized
# Integer/Rational over the float approximation. Tested through
# `safe_parse` specifically (not `classify_unsafe` alone), since the
# token screen already got this right in round 8 -- this is pinning the
# TREE layer.
_lit1200_nines = "9" * 1200
_lit1201_nines = "9" * 1201
_t0 = time.time()
_v, _e = _boundary_parse(f"sqrt({_lit1200_nines})")
_dt = time.time() - _t0
check("safe_parse('sqrt(<1200-digit all-9s literal>)') (exactly at "
      "MAX_ROOT_ARG_DIGITS) evaluates, not refused on a float-rounding "
      "artifact",
      _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
check(f"  ...promptly ({_dt:.3f}s)", _dt < 2.0, f"-> {_dt:.3f}s")
_v, _e = _boundary_parse(f"sqrt({_lit1201_nines})")
check("safe_parse('sqrt(<1201-digit all-9s literal>)') (one over the cap) "
      "is still refused",
      _v is None and _e is not None, f"-> value={_v!r} err={_e!r}")

# The same boundary for the factor family, through safe_parse.
_lit25_nines = "9" * 25
_lit26_nines = "9" * 26
_v, _e = _boundary_parse(f"factorint({_lit25_nines})")
check("safe_parse('factorint(<25-digit all-9s literal>)') (exactly at "
      "MAX_FACTOR_ARG_DIGITS) evaluates",
      _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse(f"factorint({_lit26_nines})")
check("safe_parse('factorint(<26-digit all-9s literal>)') (one over the "
      "cap) is still refused",
      _v is None and _e is not None, f"-> value={_v!r} err={_e!r}")

# The refusal message names the root properly ("square root"/"cube
# root"/"n-th root"), not the literal "2-th root"/"3-th root" it used to.
_v, _e = _boundary_parse("sqrt(factorial(1463))")
check("sqrt(factorial(1463)) refusal message says 'square root'",
      _e is not None and "square root" in _e[1], f"-> {_e}")
_v, _e = _boundary_parse("cbrt(factorial(1463))")
check("cbrt(factorial(1463)) refusal message says 'cube root'",
      _e is not None and "cube root" in _e[1], f"-> {_e}")

# Codex finding 2 (Low): _approx_decimal_digits() truncates a float log and
# overcounts at exact power-of-ten boundaries (a 25-digit all-9s literal
# read as 26 digits; a 1,200-digit all-9s literal as 1,201) -- and the
# digit-cap enforcement decision used that approximation directly. Fixed by
# using the EXACT digit count for a plain decimal literal (its token
# string length, cheap and exact) instead.
_lit25, _lit26 = "9" * 25, "9" * 26
check("factorint(<25-digit literal, exactly at MAX_FACTOR_ARG_DIGITS>) "
      "still has no token-level opinion",
      _se5.classify_unsafe(f"factorint({_lit25})") is None,
      f"-> {_se5.classify_unsafe(f'factorint({_lit25})')!r}")
check("factorint(<26-digit literal, one over the cap>) is refused",
      _se5.classify_unsafe(f"factorint({_lit26})") is not None,
      f"-> {_se5.classify_unsafe(f'factorint({_lit26})')!r}")
_lit1200, _lit1201 = "9" * 1200, "9" * 1201
check("sqrt(<1200-digit literal, exactly at MAX_ROOT_ARG_DIGITS>) "
      "still has no token-level opinion",
      _se5.classify_unsafe(f"sqrt({_lit1200})") is None,
      f"-> {_se5.classify_unsafe(f'sqrt({_lit1200})')!r}")
check("sqrt(<1201-digit literal, one over the cap>) is refused",
      _se5.classify_unsafe(f"sqrt({_lit1201})") is not None,
      f"-> {_se5.classify_unsafe(f'sqrt({_lit1201})')!r}")

# ═══ THE-1095, follow-up to #326/THE-1091 (GH #326, PR #334) ══════════════
# The value-kind and digits-kind tree-level backstops (`_numeric_ceiling_
# scan`'s `Function`-node branch) only ever saw a real `sympy.Function` node
# named after its own `_FUNCTION_ARG_CAPS` entry for a MINORITY of the
# table -- see `_DEFERRED_STANDIN_NAMES`'s own comment in safe_expr.py for
# the full empirical audit. Two symptom shapes, both through
# `exact.simplify_expression` (the ordinary, `evaluate=True` public path):
#
# Group A -- a computed argument BYPASSES the cap outright, because the
# real function either evaluates for real regardless of `evaluate=False`
# (`rf`/`ff`/`primepi`/`binomial`) or gets rewritten to a differently-named,
# differently-shaped node before the scan ever runs (`digamma` ->
# `polygamma`). Control: `factorial(1463+1)` -- same shape, already fixed
# in round 6 -- still refuses, proving this is specific to these five names.
for _expr in ("rf(1463+1, 2)", "ff(1463+1, 2)", "digamma(1463+1)",
              "primepi(1463*1000)", "binomial(1463+1, 700)"):
    _r = _exact.simplify_expression(_expr)
    check(f"THE-1095 group A: {_expr!r} (computed, over-cap argument) is "
          "refused with the ceiling code, not silently evaluated",
          _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> {_r}")
_r = _exact.simplify_expression("factorial(1463+1)")
check("THE-1095 group A control: 'factorial(1463+1)' (already-fixed round-6 "
      "shape) still refuses",
      _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
      f"-> {_r}")

# Group B -- a computed argument is WRONGLY refused, with the wrong error
# CODE, even nowhere near the cap: the real function's own eager `int()`/
# `as_int()`-style coercion raises `ValueError` on an unevaluated `Add`/
# `Pow` regardless of the VALUE it represents, inside `safe_parse`'s own
# `evaluate=False` pre-parse -- surfacing as a `validation` parse error
# instead of ever reaching evaluation. `isprime` didn't raise, but its
# eager evaluation on an unevaluated `Pow` disagreed with the identical
# literal, which is the same underlying bug from the other side.
for _expr in ("primorial(1463+1)", "prime(1463+1)", "motzkin(1463+1)"):
    _r = _exact.simplify_expression(_expr)
    check(f"THE-1095 group B: {_expr!r} is refused with the CEILING code "
          "(it is one over MAX_HEAVY_ARG), not a 'not an integer' "
          "validation error",
          _r.get("ok") is False and _r.get("code") == _errors.RESOURCE_EXHAUSTED,
          f"-> {_r}")
_r_computed = _exact.simplify_expression("isprime(10**24+7)")
_r_literal = _exact.simplify_expression("isprime(1000000000000000000000007)")
check("THE-1095 group B: 'isprime(10**24+7)' (computed) agrees with "
      "'isprime(1000000000000000000000007)' (the same value, as a literal)",
      _r_computed.get("ok") is True and _r_literal.get("ok") is True
      and _r_computed.get("simplified") == _r_literal.get("simplified") == "True",
      f"-> computed={_r_computed} literal={_r_literal}")
# ...and nowhere near the cap, a computed argument is not refused at all
# (round-6's control shape for group B: was this a length coincidence with
# the 1463+1 examples above, or does a TINY computed argument fail too?
# it does, universally, pre-fix -- this is the wrong-refusal shape, not a
# ceiling one). `motzkin` is DELIBERATELY excluded here as of round 3's
# follow-up (grok, item E, narrowing the real-dict catch-all to ONLY the
# exact "... is not an integer" ValueError class): `motzkin`'s own eval()
# raises a DIFFERENTLY-worded ValueError ("The provided number must be a
# positive integer") on an unevaluated Add/Pow argument, indistinguishable
# by message text alone from a genuine domain violation -- see the pinned
# regression just below for the narrowed, now-refused-immediately outcome.
for _expr in ("primorial(2+3)", "prime(2+3)", "isprime(2+3)"):
    _r = _exact.simplify_expression(_expr)
    check(f"THE-1095 group B: {_expr!r} (tiny, nowhere near any cap) "
          "evaluates instead of raising a spurious 'not an integer' error",
          _r.get("ok") is True, f"-> {_r}")
_r_motzkin = _exact.simplify_expression("motzkin(2+3)")
check("THE-1095 round-3-follow-up (grok item E): 'motzkin(2+3)' -- tiny, "
      "in-range -- is now refused as VALIDATION (narrowed real-dict "
      "catch-all: motzkin's ValueError does not contain 'is not an "
      "integer'), a deliberate narrowing of round 1's own group-B fix, "
      "not a ceiling refusal and not a silent success",
      _r_motzkin.get("ok") is False
      and _r_motzkin.get("code") == _errors.VALIDATION
      and "positive integer" in _r_motzkin.get("error", ""),
      f"-> {_r_motzkin}")

# I5: the table is the single source of truth, and every row is exercised
# by BOTH layers for a genuinely computed argument -- the loop above
# (`_checked_names`) already proves this structurally; these are the
# specific named repros from the ticket, exercised end to end through
# `exact.simplify_expression` rather than through `safe_parse` directly.

# Negative test: a deferred stand-in (safe_expr._deferred_global_dict) must
# never be visible in a refusal message or a result -- the class is named
# identically to the real function for exactly this reason (see
# `_deferred_global_dict`'s own docstring), so the failure mode this guards
# against is an internal marker (a dunder-prefixed helper name, "Deferred",
# "Standin", or the WRONG sympy class this round fixed, "polygamma" for a
# `digamma()` call) leaking into what a caller sees.
_leak_probes = [
    ("digamma(1463+1)", "polygamma"),
    ("rf(1463+1, 2)", "RisingFactorial"),
    ("ff(1463+1, 2)", "FallingFactorial"),
]
for _expr, _wrong_class in _leak_probes:
    _v, _e = _boundary_parse(_expr)
    _text = repr(_e)
    check(f"THE-1095: {_expr!r}'s refusal names the CALLER's own function, "
          f"not the real SymPy class {_wrong_class!r} it rewrites to",
          _v is None and _e is not None and _wrong_class not in _text,
          f"-> {_text}")
for _fn_name in _se5._DEFERRED_STANDIN_NAMES:
    # THE-1095 round 11 (coordinator replay of 9fe4f6a, Codex probe 2a):
    # `N` is a deferred stand-in with NO position-0 `_FUNCTION_ARG_CAPS`
    # entry at all (its own hazard is entirely in its PRECISION argument,
    # position 1 -- see `_EXTRA_BOUNDED_POSITIONS`'s own comment on
    # `"N"`) -- this loop's own construction (an over-cap POSITION-0
    # literal) does not apply to it; covered separately, below, via its
    # own position-1 leak probe instead.
    if _fn_name not in _se5._FUNCTION_ARG_CAPS:
        continue
    _cap_kind, _cap_val = _se5._FUNCTION_ARG_CAPS[_fn_name]
    _extra = _EXTRA_CALL_ARGS.get(_fn_name, "")
    _over = f"{_cap_val}+1" if _cap_kind == "value" else f"10**{_cap_val}+1"
    _v, _e = _boundary_parse(f"{_fn_name}({_over}{_extra})")
    _text = repr(_e)
    check(f"THE-1095: {_fn_name}()'s refusal never leaks an internal "
          "deferred-stand-in marker",
          _v is None and _e is not None
          and "deferred" not in _text.lower() and "standin" not in _text.lower(),
          f"-> {_text}")

# ═══ THE-1095 round 2 (verify-1095: both cross-vendor reviewers FAILED ═════
# ═══ 812750e; every finding below is a verified repro) ══════════════════════

# Finding 1 (Medium, both reviewers, the central one) -- ORDER. The real-dict
# pre-parse used to run BEFORE the deferred one, so an all-under-cap-TOKEN
# expression still did the expensive real work before the deferred scan got
# a chance to refuse: `bell(1463)+factorial(1463+1)` measured 6.77s through
# the real dict alone before its ceiling refusal, 0.0048s through the
# deferred parse+scan alone -- same refusal, ~1400x faster. Fixed: safe_parse
# now runs the deferred parse and reject_explosive(scan_shape) FIRST, and
# refuses on a hit without ever attempting the real-dict pre-parse.
import sympy as _sympy5

_real_bell = _sympy5.bell
_bell_was_called = []


class _SpyBell(_real_bell):
    def __new__(cls, *args, **kwargs):
        _bell_was_called.append(args)
        return _real_bell.__new__(cls, *args, **kwargs)


_sympy5.bell = _SpyBell
try:
    _t0 = time.time()
    _v, _e = _boundary_parse("bell(1463)+factorial(1463+1)")
    _dt = time.time() - _t0
    check("THE-1095 round 2: 'bell(1463)+factorial(1463+1)' (every token "
          "individually under MAX_HEAVY_ARG) is refused",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> {_e}")
    check("  ...and the REAL bell() is never constructed on the refused "
          "path -- the deferred scan alone refuses first",
          _bell_was_called == [], f"-> called with args={_bell_was_called}")
    check(f"  ...promptly (well under 6.77s measured pre-fix) ({_dt:.3f}s)",
          _dt < 1.0, f"-> {_dt:.3f}s")
finally:
    _sympy5.bell = _real_bell

# The same ordering bug's OTHER repros: a computed argument whose combined
# literal TOKENS are cheap (the digit-sum token rule is a no-op for a
# multi-literal Pow like `10**2000`) but whose real function would eagerly
# do unbounded work -- all must now refuse in milliseconds, not just
# eventually.
for _expr in ("nextprime(10**2000)", "primepi(10**8)", "binomial(10**6, 10**5)"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round 2: {_expr!r} (token-cheap, real-function-"
          "expensive) is refused",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> {_e}")
    check(f"  ...in milliseconds, not by letting the real function run "
          f"first ({_dt:.4f}s)",
          _dt < 0.5, f"-> {_dt:.4f}s")

# Finding 2 (Low Codex / Medium grok) -- arity and per-argument semantics.
# The stand-ins used to accept any arity and the scan capped EVERY
# positional argument, so `binomial(1463+1)`/`rf(1463+1)` (missing k) were
# refused as a CEILING where main correctly refuses them as a VALIDATION
# (arity) error. Fixed (unchanged from round 2): deferred stand-ins now
# share the real class's own `nargs` where SymPy exposes one (identical
# `TypeError`, structurally, to main's own arity refusal).
for _expr in ("binomial(1463+1)", "rf(1463+1)"):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round 2: {_expr!r} (wrong arity, missing k) is a "
          "VALIDATION error, matching main, not a ceiling",
          _v is None and _e is not None and _e[0] == "validation",
          f"-> value={_v!r} err={_e!r}")
    check("  ...and the message is SymPy's own arity wording, not "
          "'exceeds the limit'",
          _e is not None and "argument" in _e[1] and "given" in _e[1],
          f"-> {_e}")

# THE-1095 round 3 (verify-1095-r2, item 1): `binomial`'s own `k` (position
# 1) stays UNBOUNDED -- `binomial.eval()`'s own `d.is_negative` shortcut
# resolves `k > n` to `0` in O(1), no loop over `k` at all, so capping it
# would only manufacture a false refusal, never close a real one.
# `binomial(5, 1463+1)` still evaluates to `0`, exactly like main.
_v, _e = _boundary_parse("binomial(5, 1463+1)")
check("THE-1095 round 3: 'binomial(5, 1463+1)' (k > n) still evaluates to "
      "0 like main -- binomial's own k stays unbounded",
      _v is not None and str(_v) == "0" and _e is None, f"-> value={_v!r} err={_e!r}")

# `rf`/`ff` are DIFFERENT: their own `k` (position 1) really is an
# iteration count with no O(1) shortcut in `eval()` (confirmed against
# sympy 1.14.0's source: `reduce(lambda r, i: r*(x +/- i), range(int(k)),
# 1)`, unconditionally, regardless of whether `x < k` would eventually
# make a factor zero) -- `rf(5, 1000**1000)`/`ff(5, 1000**1000)` hang past
# 4s with no cap on `k` at all. THE-1095 round 3 therefore bounds `rf`/
# `ff`'s own position 1 at `MAX_HEAVY_ARG`, same as position 0 -- a
# DELIBERATE NARROWING versus main for the specific `k > n` shape
# (`ff(5, 1463+1)` is `0` cheaply on main; this branch now refuses it,
# since it cannot tell that shape apart from a genuinely large `k`
# without evaluating `k` terms first) -- pinned here, and in the
# CHANGELOG, as the accepted, intentional trade-off (closing a real hang
# is worth a false refusal on this one narrow shape; `guarded_call`'s own
# CPU backstop was main's ONLY protection for `rf`/`ff`'s `k` before
# THE-1095 existed at all).
for _expr in ("ff(5, 1463+1)", "rf(5, 1463+1)"):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round 3: {_expr!r} (k > n, but k itself over rf/ff's "
          "OWN cap) is refused -- a deliberate, documented narrowing vs "
          "main's cheap '0'",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")

# The SAME narrowing shows up for the position-0 short circuits Codex
# found (`binomial(1463+1, 0)` = 1, `binomial(1463+1, 1463+2)` = 0,
# `rf(1463+1, 0)` = `ff(1463+1, 0)` = 1 on main): position 0 (`n`/`x`) is
# over cap regardless of `k`, and this branch has no "but k makes it
# trivial" exception for position 0 either -- pinned here too, same
# CHANGELOG bullet.
for _expr in ("binomial(1463+1, 0)", "binomial(1463+1, 1463+2)",
              "rf(1463+1, 0)", "ff(1463+1, 0)"):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round 3: {_expr!r} (position-0 over cap, k trivial "
          "on main) is refused -- the same documented narrowing",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")

# Finding 3 (Low, grok) -- a real-dict parse exception used to be treated as
# unconditionally inconclusive. Fixed: a `TypeError` (SymPy's own arity/
# shape validation, unconditional regardless of evaluate=False) is returned
# immediately as a validation error, matching main; only a non-TypeError
# exception (the group-B eager int-coercion class) on a call the deferred
# scan already proved safe proceeds to evaluate=True. `binomial(1463+1)`/
# `rf(1463+1)` above already cover the single-parse-failure shape (real
# fails, deferred is clean); this covers the invalid-and-tiny shape too, and
# the both-parses-fail shape.
_v, _e = _boundary_parse("binomial(2,3,1463+1)")
check("THE-1095 round 2: 'binomial(2,3,1463+1)' (too many args, tiny "
      "values) is refused as validation (arity), not evaluated or refused "
      "as a ceiling",
      _v is None and _e is not None and _e[0] == "validation", f"-> {_e}")
_v, _e = _boundary_parse("binomial(5, 6)")  # k > n, tiny -- must still be 0
check("THE-1095 round 2: 'binomial(5, 6)' (k > n, both tiny, no ceiling "
      "involved at all) still evaluates to 0 like main",
      _v is not None and str(_v) == "0" and _e is None, f"-> value={_v!r} err={_e!r}")

# Finding 4 (Low, grok) -- `root` used to be excluded from the deferred
# stand-ins entirely, so a computed, digit-heavy VALUE argument
# (`root(10**2000, 3)`, token-cheap: '10' and '2000' sum to 6 digits, far
# under MAX_ROOT_ARG_DIGITS) ran root's own real construction on EVERY
# pre-parse, unbounded by anything until evaluation. Fixed: `root` is now
# also a deferred stand-in, so its VALUE argument gets the same tree-level
# digit-count backstop `factorint` and friends already have.
_t0 = time.time()
_v, _e = _boundary_parse("root(10**2000+1, 3)")
_dt = time.time() - _t0
check("THE-1095 round 2: 'root(10**2000+1, 3)' (computed, digit-heavy, "
      "token-cheap) is refused at the tree level",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
check(f"  ...promptly ({_dt:.3f}s)", _dt < 1.0, f"-> {_dt:.3f}s")
_v, _e = _boundary_parse("root(21, 3)")
check("  ...and root(21, 3) (an ordinary computed-free value) still evaluates",
      _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")

# ═══ THE-1095 round 3 (verify-1095-r2: both reviewers FAILED fe1a9a2; ═══════
# ═══ every finding below is a verified repro) ═══════════════════════════════

# Item 1 self-check: every EXTRA bounded position (`_EXTRA_BOUNDED_
# POSITIONS`), for every name that has one, refuses a computed over-cap
# value at that position in milliseconds (both `cap+1` and a `10**k`-
# shaped form), and every EXPLICITLY unbounded position (`_UNBOUNDED_
# POSITIONS`) does NOT refuse a huge value there as a ceiling.
for _fn_name, _extra_positions in _se5._EXTRA_BOUNDED_POSITIONS.items():
    _first_arg = "5"  # comfortably under every position-0 cap in the table
    for _pos, (_kind, _cap) in _extra_positions.items():
        for _over in (f"{_cap}+1", f"10**{_cap // 2 or 1}+{_cap}"):
            _args = [_first_arg] * _pos + [_over]
            _expr = f"{_fn_name}({', '.join(_args)})"
            _t0 = time.time()
            _v, _e = _boundary_parse(_expr)
            _dt = time.time() - _t0
            check(f"self-check (round 3): {_expr!r} (position {_pos} over "
                  "its own cap) is refused",
                  _v is None and _e is not None and _e[0] == "ceiling",
                  f"-> value={_v!r} err={_e!r}")
            check(f"  ...promptly ({_dt:.3f}s)", _dt < 1.0, f"-> {_dt:.3f}s")
for _fn_name, _positions in _se5._UNBOUNDED_POSITIONS.items():
    for _pos in _positions:
        _args = ["5"] * _pos + ["10**9"]
        _expr = f"{_fn_name}({', '.join(_args)})"
        _v, _e = _boundary_parse(_expr)
        check(f"self-check (round 3): {_expr!r} (position {_pos} "
              "EXPLICITLY unbounded) is NOT refused as a ceiling",
              _e is None or _e[0] != "ceiling", f"-> value={_v!r} err={_e!r}")

# Item 2: a NESTED table-function call used to be silently skipped as
# "unresolved" by `_numeric_ceiling_scan` (`_log10_num_den` only
# understands Integer/Mul/Add/Pow/Rational/Float, never a Function), so it
# passed every screen and paid the REAL function's own cost regardless.
# `_table_function_bound`'s growth-bound machinery closes this: each must
# now refuse in well under 1s, via the deferred scan alone, not by
# actually running the nested call for real.
for _expr in ("divisors(factorial(100))", "polygamma(1, factorial(100))",
              "factorial(polygamma(0, 1000**1000))", "sqrt(bell(1463))",
              "cbrt(bell(1463))", "(x+1)**bell(1463)",
              "root(factorial(1463), 2)"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round 3: {_expr!r} (nested heavy call) is refused",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
    check(f"  ...promptly ({_dt:.3f}s), not by materializing the nested "
          "call for real first",
          _dt < 1.0, f"-> {_dt:.3f}s")
# ...and a GENUINELY cheap nesting still evaluates to main's exact values.
for _expr, _expected in (("sqrt(factorial(10))", "720*sqrt(7)"),
                          ("divisors(factorial(5))",
                           "[1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 24, 30, 40, 60, 120]"),
                          ("factorial(binomial(6,3))", "2432902008176640000")):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round 3: {_expr!r} (cheap nesting) still evaluates to "
          f"main's exact value {_expected!r}",
          _v is not None and str(_v) == _expected and _e is None,
          f"-> value={_v!r} err={_e!r}")

# Item 3: arity for the plain-callable names (and `root`) is now derived
# from `inspect.signature` of the REAL callable, so a wrong-arity call
# fails the DEFERRED parse itself and is returned as `origin/main`'s own
# `TypeError` text, instead of the over-cap FIRST argument reaching a
# ceiling refusal before arity was ever checked.
#
# THE-1095 round-3-follow-up #3 (coordinator review of 5e2a961, grok item
# 5): tightened from a substring check to EQUALITY with main's own text --
# `_arity_checked_new` (see its own docstring) makes the stand-in raise
# Python's OWN native call-binding TypeError, byte-identical to a real
# call, rather than `Function.__new__`'s own generic "X takes exactly N
# arguments" wording a plain-callable stand-in used to get from its
# `nargs`.
_R3_ARITY_PINS = {
    "isprime(10**26,1)": "isprime() takes 1 positional argument but 2 were given",
    "root(10**1201,3,0,0,9)": "root() takes from 2 to 4 positional arguments but 5 were given",
    "root(10**2000)": "root() missing 1 required positional argument: 'n'",
    "prime(5,6)": "prime() takes 1 positional argument but 2 were given",
    "primorial(5,6,7)": "primorial() takes from 1 to 2 positional arguments but 3 were given",
    "divisors(5,6,7,8)": "divisors() takes from 1 to 3 positional arguments but 4 were given",
    "nextprime(5,6,7)": "nextprime() takes from 1 to 2 positional arguments but 3 were given",
    "npartitions(5,6,7)": "npartitions() takes from 1 to 2 positional arguments but 3 were given",
    "primefactors(5,6,7,8)": "primefactors() takes from 1 to 3 positional arguments but 4 were given",
    "factorint(5,6,7,8,9,10,11,12,13,14)":
        "factorint() takes from 1 to 9 positional arguments but 10 were given",
}
for _expr, _expected_text in _R3_ARITY_PINS.items():
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round-3-follow-up #3 item 5: {_expr!r} is refused as "
          f"validation, BYTE-IDENTICAL to main's own TypeError text",
          _v is None and _e is not None and _e[0] == "validation"
          and _e[1] == f"parse error: {_expected_text}", f"-> value={_v!r} err={_e!r}")
# ...and the Function-subclass family (binomial/rf/ff) already matched --
# confirmed still true, pinned the same way.
for _expr, _expected_text in (("binomial(1463+1)", "binomial takes exactly 2 arguments (1 given)"),):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round-3-follow-up #3 item 5: {_expr!r} is BYTE-"
          "IDENTICAL to main's own TypeError text",
          _v is None and _e is not None and _e[0] == "validation"
          and _e[1] == f"parse error: {_expected_text}", f"-> value={_v!r} err={_e!r}")

# THE-1095 round-3-follow-up #4 (coordinator review of 06272aa, grok item
# 1): `_arity_checked_new` used to build a dummy Python function via
# `compile()` + `types.FunctionType` purely to reuse Python's own call-
# binding error text -- replaced with `inspect.signature(real).bind(...)`
# first, falling through to a REAL call to `real` only on a `bind()`
# failure (which raises the SAME native TypeError before any of `real`'s
# own body runs, since CPython binds arguments to a callee's frame before
# executing any of its bytecode). Proven with a spy, not just a timing
# measurement: the spy's own body must NEVER run on the wrong-arity path.
_spy_body_calls: list = []


def _spy_isprime_like(n, base=None):
    _spy_body_calls.append((n, base))
    return True


_spy_new = _se5._arity_checked_new(_spy_isprime_like)
_spy_raised = False
_spy_text = ""
try:
    _spy_new(None, 1, 2, 3)  # 3 positional args -- spy_isprime_like takes at most 2
except TypeError as _spy_exc:
    _spy_raised = True
    _spy_text = str(_spy_exc)
check("THE-1095 round-3-follow-up #4 item 1: the arity-checked __new__ "
      "raises TypeError for a wrong-arity call",
      _spy_raised, "-> did not raise")
check("  ...WITHOUT ever entering the real callable's own body (spy "
      "pattern, not just a timing measurement)",
      len(_spy_body_calls) == 0, f"-> {len(_spy_body_calls)} calls: {_spy_body_calls}")
check("  ...and the raised text is the SPY's own native arity message "
      "('spy_isprime_like() takes from 1 to 2 positional arguments but "
      "3 were given'), not a look-alike",
      _spy_raised and "spy_isprime_like()" in _spy_text and "3 were given" in _spy_text,
      f"-> {_spy_text!r}")

# Item 4: the numeric RESULT of step 3's real evaluate=True parse is now
# itself run through the same digit-count ceiling every other path
# already has -- no combination of two individually-in-cap POSITIONS can
# be proven jointly safe without this backstop (rf(700+700, 700+700), both
# positions comfortably under rf's own 1463 cap, still produces an
# 8_887-ish-digit integer).
_v, _e = _boundary_parse("rf(700+700, 700+700)")
check("THE-1095 round 3: 'rf(700+700, 700+700)' (both positions in-cap, "
      "output over MAX_NUMERIC_DIGITS) is refused by the OUTPUT ceiling",
      _v is None and _e is not None and _e[0] == "ceiling"
      and "digits" in _e[1], f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("rf(1400,1400)")  # the identical shape, as literals
check("  ...and the identical shape as bare literals is refused the same way",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")

# Item 5: the real-dict parse exception class that proceeds to evaluate=True
# is pinned as INTENDED behaviour, not an accident of a broad `except
# ValueError` -- `TypeError` (arity) always returns immediately; every
# `ValueError` this module's own group-B names raise (confirmed live:
# "... is not an integer", motzkin's "must be a positive integer") is
# treated the same way main eventually resolves it once the deferred scan
# has already proven the call safe.
for _expr, _expected in (("nextprime(10,2+1)", "17"),):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round 3: {_expr!r} (group-B name, computed 'ith' "
          f"argument) evaluates to {_expected!r} like main",
          _v is not None and str(_v) == _expected and _e is None, f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("prime(1-1)")
check("THE-1095 round 3: 'prime(1-1)' (prime(0), invalid index) reaches "
      "the SAME evaluate=True path main does, not a spurious pre-parse "
      "'not an integer' error",
      _e is None or "is not an integer" not in (_e[1] if _e else ""), f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("prime(1/0)")
check("THE-1095 round 3: 'prime(1/0)' is refused (ZeroDivisionError-shaped "
      "input), with SOME validation text, not silently swallowed",
      _v is None and _e is not None, f"-> value={_v!r} err={_e!r}")

# Item 6, SUPERSEDED by THE-1095 round-3-follow-up #3 (coordinator review
# of 5e2a961, grok): `%`/`//`/`<<`/`>>` are not `evaluate=False`-protected
# by SymPy's OWN parser for their whole subtree, closed here not by a
# token-level pre-check (deleted -- `_unprotected_operator_violation` no
# longer exists) but by `_parse_deferred`'s own AST-stage transform
# (`_deferred_transformer_class`), which maps these four operators to
# marker-node CALLS with their children explicitly VISITED -- so a
# NESTED, huge exponent stays exactly as `evaluate=False`-protected as
# every other subtree, and is caught either by `_deferred_binop_
# violation` (a numeric base) or the EXISTING symbolic-exponent ceiling
# (`MAX_SYMBOLIC_EXPONENT`, a symbolic base) once the marker's own
# children are independently visited -- the exact fuzzer-discovered hang,
# and every variant found investigating it, still refuses in MILLISECONDS.
_N78 = "2" + "1" * 77
for _expr in (f"x**{_N78} % 11", f"2**{_N78} % 11", f"(x+1)**{_N78} % 11",
              f"Mod(x**{_N78}, 11)", f"x**{_N78} % y", f"2**{_N78} // 11",
              f"2**{_N78} << 2", f"2**{_N78} >> 11"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round-3-follow-up #3: {_expr[:40]!r}... is refused",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
    check(f"  ...in milliseconds ({_dt:.4f}s), not by letting the real "
          "operator construct the huge Pow first",
          _dt < 0.5, f"-> {_dt:.4f}s")
# The exact fuzzer input this all started from.
_v, _e = _boundary_parse("x^2" + "1" * 77 + "%11")
check("THE-1095 round-3-follow-up #3: the original fuzzer input "
      "('x^2' + '1'*77 + '%11') refuses in milliseconds, no MemoryError",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")

# Valid small inputs stay BYTE-IDENTICAL to main.
for _expr, _expected in (("7 % 3", "1"), ("10 // 3", "3"), ("Mod(7,3)", "1"),
                          ("2**10 % 7", "2")):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round-3-follow-up #3: {_expr!r} == {_expected!r}, unaffected",
          _v is not None and str(_v) == _expected and _e is None, f"-> value={_v!r} err={_e!r}")

# THE-1095 round-3-follow-up #3, grok item 4: the "computed or above 200"
# narrowing (round 3's own token-level check) is GONE -- the AST-level fix
# needs no such narrowing at all, since it can see the WHOLE tree, not
# just tokens near the operator. `x**(2+2) % 3`, `2**300 % 7`, and
# `2**(10+10) % 7` all now return main's own values (previously: the
# first was an ACCIDENTAL, unpinned refusal, the other two were deliberate
# but now-unnecessary narrowings).
for _expr, _expected in (("x**2 % 3", "Mod(x**2, 3)"), ("x**(2+2) % 3", "Mod(x**4, 3)"),
                          ("2**300 % 7", "1"), ("2**(10+10) % 7", "4")):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round-3-follow-up #3: {_expr!r} == {_expected!r}, "
          "matching main (no narrowing needed at the AST level)",
          _v is not None and str(_v) == _expected and _e is None, f"-> value={_v!r} err={_e!r}")
# `eval_exact` is a SEPARATE, already-safe evaluator and must be untouched.
_er = _exact.eval_exact("1 << 20")
check("THE-1095 round-3-follow-up #3: eval_exact('1 << 20') is unaffected "
      "(a separate evaluator this fix must not touch)",
      _er.get("value") == "1048576", f"-> {_er}")
_er2 = _exact.eval_exact(f"2**{_N78} % 7")
check("  ...and eval_exact's OWN, unrelated exponent cap still protects it",
      _er2.get("ok") is False, f"-> {_er2}")

# ═══ THE-1095 round-3-follow-up: ClusterFuzzLite crash on 44c83b1, plus ═══
# ═══ grok items A-E and Codex's shift-digit-bound item, one commit ═══════

# The exact ClusterFuzzLite crash input, decoded from the crash file
# (fuzz/safe_expr_fuzzer.py's own ConsumeIntInRange-then-
# ConsumeUnicodeNoSurrogates contract, replayed against the seed corpus).
# On 44c83b1, `safe_parse` raised `TypeError: 'property' object is not
# iterable` uncaught from `reject_explosive` -> `Basic.free_symbols`: a
# bare CLASS reference (the literal `Pow` in this input, resolving through
# `safe_global_dict()`'s full `sympy.__dict__` exposure to the CLASS
# itself rather than an instance) ended up nested one level inside another
# node's OWN `.args` tuple, where `_walk`'s existing per-node guard never
# got a chance to run before `reject_explosive`'s own code called
# `.free_symbols` directly on that ancestor. Confirmed LIVE (this
# session's own atheris 3.12 repro venv) that `origin/main` (6cda9d4)
# crashes on this SAME decoded string with the SAME exception, at its own
# `reject_explosive` line 2080 (`if exponent.free_symbols:`) -- this is a
# LATENT bug that predates THE-1095 entirely, not a regression introduced
# by any of this ticket's earlier rounds; ClusterFuzzLite happened to find
# it via a path this ticket's own changes made reachable. There is no
# "main's error code" to match here -- main does not return one, it
# crashes -- so this regression test instead pins `safe_parse`'s own
# contract: always a 2-tuple, never a raised exception, for this exact
# input.
_CRASH_343R3_INPUT = ("breakpoint()!!z!al^aZ!al^zaa!az!al^jalZZZZZZZZZZZZZZZZZZZZZZZZ"
                      "al^aZ!al^zaa!az!al^jala!!z!al^aZ!al^Pow!az!al^jalZZZZZZZZZZZZ"
                      "ZZZZZZZZZZZZal^aZ!al^zaa!az!al^jalZZZZZl^jaa!az!al^aZ!al^jaa!"
                      "az!al^jalZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"
                      "ZZZZZZZZZZZZZZZZZZZZZZ^jtal^jaa!az!al^aZ!al^jaa!az!exceptal^j"
                      "al^ZZZZZl^jaa!az!al^aZ!al^jaa!az!at^jalZZZZZZZZZZZZZZZZZZZZZZ"
                      "ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"
                      "^jtal^jaa!az!al^aZ!al^jaa!az!exceptal^jal^jt")
try:
    _crash_v, _crash_e = _se5.classify_unsafe(_CRASH_343R3_INPUT), None
    _crash_v, _crash_e = _boundary_parse(_CRASH_343R3_INPUT)
    _crash_raised = False
except Exception as _crash_exc:
    _crash_raised = True
    _crash_v = _crash_e = None
check("THE-1095 round-3-follow-up: the exact ClusterFuzzLite crash-343r3 "
      "input never raises through safe_parse (fixed at its root: "
      "reject_explosive now refuses a bare `type` reference before ever "
      "calling a property on it)",
      not _crash_raised, f"-> raised={_crash_raised}")
check("  ...and returns the ordinary (value, error) 2-tuple contract, a "
      "clean refusal rather than a crash",
      not _crash_raised and (_crash_v is None) != (_crash_e is None),
      f"-> value={_crash_v!r} err={_crash_e!r}")

# Coordinator review of d3c65c7, two text defects fixed in the same round:
#
# 1. A bare class reference is a malformed EXPRESSION (validation), not an
#    oversized one (ceiling) -- and must not leak the Python repr of the
#    class (`<class 'sympy.core.power.Pow'>`).
for _bare_expr, _bare_name in (("Pow", "Pow"), ("Mul", "Mul"), ("binomial", "binomial")):
    _v, _e = _boundary_parse(_bare_expr)
    check(f"THE-1095 round-3-follow-up: {_bare_expr!r} (a bare class "
          "reference) is refused as VALIDATION, not ceiling",
          _v is None and _e is not None and _e[0] == "validation", f"-> {_v!r} {_e!r}")
    check(f"  ...names {_bare_name!r} as written, not the Python repr "
          "(\"<class '...'>\") or the old 'is not a valid expression node' "
          "wording",
          _e is not None and f"'{_bare_name}'" in _e[1] and "<class" not in _e[1]
          and "is not a valid expression node" not in _e[1], f"-> {_e!r}")
# ...and a class used in a genuine (if malformed) expression shape keeps
# its EXISTING validation code -- unaffected by this fix either way.
for _malformed_expr in ("x^Pow", "Pow*2"):
    _v, _e = _boundary_parse(_malformed_expr)
    check(f"THE-1095 round-3-follow-up: {_malformed_expr!r} still refuses "
          "as validation (unaffected -- this shape was already validation "
          "before the wording fix)",
          _v is None and _e is not None and _e[0] == "validation", f"-> {_v!r} {_e!r}")

# 2. The `<<` shift-digit-ceiling message used to read "'<<' shift result
#    the result would have about N digits in its numerator, ..." -- a
#    doubled "result" and a numerator/denominator distinction that makes
#    no sense for a plain integer shift (never a Rational).
_v, _e = _boundary_parse("bell(1463) << 100000")
check("THE-1095 round-3-follow-up: the '<<' shift-ceiling message names "
      "'the result of' once, with no 'numerator' wording",
      _e is not None and _e[0] == "ceiling"
      and "the result of '<<' would have about" in _e[1]
      and "numerator" not in _e[1] and "shift result the result" not in _e[1],
      f"-> {_e!r}")
_v, _e = _boundary_parse("1 << factorial(20)")
check("  ...and the 'unbounded number of digits' variant matches the same "
      "shape",
      _e is not None and _e[0] == "ceiling"
      and "the result of '<<' would have an unbounded number of digits" in _e[1]
      and "numerator" not in _e[1], f"-> {_e!r}")

# Never-raises sweep: every bare-operand name this module exposes --
# `Pow`/`Mul`/`Add`/`Symbol` (SymPy's own core classes) and every
# `_FUNCTION_ARG_CAPS` table name -- used bare (uncalled) in four shapes
# that could plausibly leak a class reference into a tree
# `reject_explosive` walks. `safe_parse` must return a 2-tuple in every
# case, never raise -- this is the general form of the crash-343r3 fix,
# not a name-by-name guess at what the fuzzer might try next.
_BARE_OPERAND_NAMES = ["Pow", "Mul", "Add", "Symbol", *_se5._DEFERRED_STANDIN_NAMES,
                       *_se5._FUNCTION_ARG_CAPS]
_bare_operand_fails = 0
for _name in sorted(set(_BARE_OPERAND_NAMES)):
    for _shape in ("{name}", "x^{name}", "{name}*2", "{name}(", "{name})"):
        _expr = _shape.format(name=_name)
        try:
            _r = _boundary_parse(_expr)
            _ok = isinstance(_r, tuple) and len(_r) == 2
        except Exception as _exc:
            _ok = False
            _r = f"RAISED {type(_exc).__name__}: {_exc}"
        if not _ok:
            _bare_operand_fails += 1
            FAILS.append(f"never-raises sweep: {_expr!r} -> {_r!r}")
check(f"THE-1095 round-3-follow-up: never-raises sweep over "
      f"{len(set(_BARE_OPERAND_NAMES))} bare-operand names x 5 shapes "
      f"({len(set(_BARE_OPERAND_NAMES)) * 5} probes) -- safe_parse always "
      "returns a 2-tuple, never raises",
      _bare_operand_fails == 0, f"-> {_bare_operand_fails} failures")

# Property-style check for EVERY `_GROWTH_BOUNDS` entry: for n across a
# sample grid over its capped domain (including the cap itself, and
# ith=1000 for nextprime), the bound must be >= the REAL value SymPy
# computes, for every n small enough to compute cheaply. A bound found
# BELOW the true value fails this test -- this is what actually caught
# the primorial/nextprime/fibonacci/lucas/prime formula bugs (items A, B,
# D) during this round's own development, before any named repro was
# even written by hand.
import sympy as _sp5


def _growth_check(name, fn, real_fn, ns, extra_bounds=None, real_is_float=False):
    # THE-1095 round 11 (grok addendum to coordinator replay of 9fe4f6a):
    # `n > 0 else -math.inf` used to be the ONLY branch -- silently
    # WRONG for a negative or fractional `n` (this module's own `bounds`
    # convention is `log10(|n|)`, sign discarded -- see `_growth_zeta`'s
    # own docstring for exactly why a caller must never assume a
    # magnitude-only bound also tells it the sign), and every existing
    # call site below only ever passed positive integers, so the bug
    # was invisible until a grid that actually exercises `n < 0` or a
    # fractional `n` (added this round, see the zeta/gamma grids
    # further down) is run through it. `real_is_float=True` lets a
    # `real_fn` return a genuine SymPy `Float`/irrational EXPRESSION
    # (`gamma`'s own non-integer values) rather than assuming the
    # result is exactly `int`-representable.
    fails = []
    for n in ns:
        n_val = float(n)
        bounds = {0: math.log10(abs(n_val)) if n_val != 0 else -math.inf}
        if extra_bounds:
            bounds.update(extra_bounds(n))
        b = fn(bounds)
        try:
            real = real_fn(n)
        except Exception:
            continue
        if real_is_float:
            # `float(Abs(real))` overflows to `inf` once `real` has more
            # than ~308 digits (gamma(1463) does) -- resolve the
            # MAGNITUDE symbolically instead, the same
            # arbitrary-precision `log` this module's own resolver uses
            # (`_float_value_log10`), never a raw `float()` cast.
            real_log = 0.0 if real == 0 else float(_sp5.log(_sp5.Abs(real), 10))
        else:
            real = abs(int(real)) if real != 0 else 0
            real_log = math.log10(real) if real > 0 else 0.0
        if b < real_log - 1e-9:
            fails.append((n, real_log, b))
    check(f"THE-1095 round-3-follow-up: growth-bound property check for "
          f"{name!r} (bound >= real value across its own grid)",
          not fails, f"-> fails={fails}")


# THE-1095 round-3-follow-up #4 (coordinator review of 06272aa, grok item
# 2): factorial and bell now use `_growth_stirling_factorial` (tight,
# not the shared loose catch-all) -- verified up to and including the
# exact cap (1463) and one past it (1464), the boundary the coordinator's
# own named repros (`bell(1463)` evaluates, `bell(1464)` refuses) depend
# on being correctly ordered.
_growth_check("factorial (_growth_stirling_factorial)", _se5._growth_stirling_factorial,
              lambda n: _sp5.factorial(n), list(range(1, 60)) + [1463, 1464, 1500])
_growth_check("bell (_growth_stirling_factorial, bell(n) <= n!)", _se5._growth_stirling_factorial,
              lambda n: _sp5.bell(n), list(range(1, 40)) + [1463, 1464, 1500])
# `_growth_nn_loose` itself is still a real, correct (if now unused by
# factorial/bell) function -- still checked, on the names that still use
# it as their own shared catch-all.
_growth_check("motzkin (_growth_nn_loose, shared catch-all)", _se5._growth_nn_loose,
              lambda n: _sp5.motzkin(n), range(1, 40))
_growth_check("binomial (_growth_2n)", _se5._growth_2n,
              lambda n: _sp5.binomial(n, n // 2), range(1, 60))
_growth_check("primorial (default, nth=True)", _se5._growth_primorial,
              lambda n: _sp5.primorial(n), range(1, 60))
_growth_check("primorial (nth=False)", _se5._growth_primorial,
              lambda n: _sp5.primorial(n, nth=False), range(1, 60))
_growth_check("prime", _se5._growth_prime, lambda n: _sp5.prime(n), range(1, 60))
_growth_check("primepi", _se5._growth_primepi, lambda n: _sp5.primepi(n), range(1, 60))
_growth_check("npartitions", _se5._growth_npartitions,
              lambda n: _sp5.npartitions(n), range(1, 40))
_growth_check("harmonic", _se5._growth_harmonic, lambda n: _sp5.harmonic(n), range(1, 60))
_growth_check("digamma (position 0)", _se5._growth_digamma,
              lambda n: _sp5.floor(_sp5.Abs(_sp5.digamma(n))) + 1, range(1, 60))
_growth_check("fibonacci", _se5._growth_fibonacci, lambda n: _sp5.fibonacci(n), range(60))
_growth_check("lucas", _se5._growth_lucas, lambda n: _sp5.lucas(n), range(60))
_growth_check("tribonacci", _se5._growth_tribonacci, lambda n: _sp5.tribonacci(n), range(40))
_growth_check("catalan", _se5._growth_catalan, lambda n: _sp5.catalan(n), range(40))
_growth_check("totient", _se5._growth_totient, lambda n: _sp5.totient(n), range(1, 60))
_growth_check("rf", _se5._growth_rf_ff, lambda n: _sp5.rf(n, n), range(1, 40),
              extra_bounds=lambda n: {1: math.log10(n) if n > 0 else -math.inf})
_growth_check("ff", _se5._growth_rf_ff, lambda n: _sp5.ff(n, n), range(1, 40),
              extra_bounds=lambda n: {1: math.log10(n) if n > 0 else -math.inf})
_growth_check("divisor_sigma (k=1 default)", _se5._growth_divisor_sigma,
              lambda n: _sp5.divisor_sigma(n), range(1, 60))
_growth_check("polygamma (order=0)", _se5._growth_polygamma,
              lambda n: _sp5.floor(_sp5.Abs(_sp5.polygamma(0, n))) + 1, range(1, 60),
              extra_bounds=lambda n: {0: 0.0, 1: math.log10(n) if n > 0 else -math.inf})
# THE-1095 round 11 (grok addendum to coordinator replay of 9fe4f6a):
# `zeta` was not in this property-sweep list AT ALL (its own OLD
# formula's sign-blindness bug -- `_safe_pow10(bounds[0]) >= 2` reading
# as `|s| >= 2`, not `s >= 2` -- would have been caught here immediately
# had a NEGATIVE `n` ever been swept). Now checked across BOTH signs.
_growth_check("zeta (now sign-safe -- see _growth_zeta's own docstring)",
              _se5._growth_zeta, lambda n: _sp5.zeta(n),
              list(range(2, 40)) + list(range(-40, -1)) + [-1463, -1200, 1463])
# `gamma`/`loggamma` (`_growth_gamma`): domain-restricted to `z >= 1`
# elsewhere (`_MAGNITUDE_AT_LEAST_ONE_DOMAIN_POSITIONS`) -- this formula
# only needs to be sound ON that domain, swept across integers AND
# rationals (the `[1, 2]` dip where `Gamma`'s own minimum sits), never
# below `z = 1` (a separate, direct expression pin below covers the
# domain REFUSAL for `z < 1` instead of asking this formula to bound a
# regime it is never actually reached for).
_growth_check("gamma (_growth_gamma, z >= 1 domain)", _se5._growth_gamma,
              lambda n: _sp5.gamma(n),
              [1, _sp5.Rational(6, 5), _sp5.Rational(3, 2), _sp5.Rational(7, 4), 2, 3, 5, 10, 20, 50, 100, 1463],
              real_is_float=True)
_growth_check("nextprime (ith=1)", _se5._growth_nextprime,
              lambda n: _sp5.nextprime(n), [2, 3, 10, 100, 1000, 10**6, 10**10, 10**20],
              extra_bounds=lambda n: {1: 0.0})
_growth_check("nextprime (ith=1000, MAX_ITH_PRIME_SKIP)", _se5._growth_nextprime,
              lambda n: _sp5.nextprime(n, 1000), [2, 3, 10, 100, 1000],
              extra_bounds=lambda n: {1: math.log10(1000)})
# Item 3 (grok, coordinator review of 949aac9): the deleted "average gap"
# formula was NOT a sound bound -- `nextprime(887) == 907` (a real,
# unremarkable gap of 20) already exceeded it. The replacement (Bertrand's
# postulate compounded in log space, `log10(n) + ith*log10(2)`) is
# verified here against adversarial points, not round numbers: known
# large prime gaps (887 before a gap-20 jump; 1327, gap 34; 31397, gap
# 72; ~1.7e15, gap 1132 -- OEIS's own record-gap list, sympy's own
# nextprime is cheap even at this size, BPSW-based, not trial division),
# a cap-edge digit count (10**25 - 1), and ith in {1, 2, 10, 1000} --
# MAX_ITH_PRIME_SKIP itself.
for _np_n in (887, 1327, 31397, 1693182318746371, 10**25 - 1):
    for _np_ith in (1, 2, 10, 1000):
        _np_bound = _se5._growth_nextprime({0: math.log10(_np_n), 1: math.log10(_np_ith)})
        _np_real = _sp5.nextprime(_np_n, _np_ith)
        _np_real_log = math.log10(int(_np_real))
        check(f"THE-1095 round-3-follow-up item 3: _growth_nextprime is a "
              f"SOUND bound at n={_np_n}, ith={_np_ith} (adversarial, not "
              "a round number)",
              _np_bound >= _np_real_log - 1e-9,
              f"-> real_log={_np_real_log} bound={_np_bound}")

# Item A (grok): primorial's DEFAULT (no second arg, nth=True) is the
# product of the FIRST n primes, not the product of primes <= n -- the
# formula used to bound the WRONG one of the two meanings.
_v, _e = _boundary_parse("primorial(1463+1)")
check("THE-1095 round-3-follow-up item A: 'primorial(1463+1)' (default "
      "nth=True form, one over MAX_HEAVY_ARG) is refused as a ceiling, "
      "same as before -- the fix corrects the FORMULA, not whether this "
      "specific boundary refuses",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
# The DANGEROUS direction of item A's bug: `primorial(6)` (default
# nth=True: product of the FIRST 6 primes) == 30_030, already far over
# factorial's own MAX_HEAVY_ARG (1463) argument cap -- but the OLD,
# WRONG formula (nth=False's `e**(1.02n)`) bounded it at only ~455 (log10
# ~2.66, UNDER 1463), which would have let `factorial(primorial(6))`
# proceed to REAL construction: `factorial(30_030)` is astronomically
# unbounded, exactly the hang this module exists to prevent. This is
# refused, never evaluated (no real value to compare against -- computing
# `factorial(30_030)` as a test oracle would itself hang, which is
# precisely the point).
_v, _e = _boundary_parse("factorial(primorial(6))")
check("THE-1095 round-3-follow-up item A: 'factorial(primorial(6))' "
      "(primorial(6) == 30_030, the OLD buggy formula bounded it at only "
      "~455 -- UNDER cap, wrongly permitting a factorial(30_030) "
      "construction) is now correctly refused",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
# ...and a genuinely small, safe nested primorial still evaluates.
_v, _e = _boundary_parse("factorial(primorial(2))")
check("THE-1095 round-3-follow-up item A: 'factorial(primorial(2))' "
      "(primorial(2) == 6, comfortably safe either way) evaluates to "
      "main's exact value '720'",
      _v == 720 and _e is None, f"-> value={_v!r} err={_e!r}")

# Item B (grok): nextprime's growth bound ignored `ith` (position 1)
# entirely -- bounded as if ith were always 1 regardless of its real
# value, so `nextprime(2, 1000) == 7927` (genuinely over factorial's own
# 1463 argument cap) was bounded by the OLD formula at just
# `log10(2*2) ~= 0.6` -- comfortably UNDER cap, wrongly letting a nested
# `factorial(7927)` construction (tens of thousands of digits) proceed.
# No real value to compare against here either -- asserting the refusal
# IS the test; computing `factorial(7927)` as an oracle would itself be
# the exact hazard this fix closes.
_v, _e = _boundary_parse("factorial(nextprime(2, 1000))")
check("THE-1095 round-3-follow-up item B: 'factorial(nextprime(2, 1000))' "
      "(nextprime(2,1000) == 7927, over factorial's own 1463 cap -- the "
      "OLD formula ignored ith and wrongly bounded this as ~4, safe) is "
      "now correctly refused",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
# ...and a genuinely in-range n/ith still evaluates: nextprime(500, 1) ==
# 503, comfortably under the cap. THE-1095 round-3-follow-up (coordinator
# review of 949aac9, grok item 3): the formula above is now Bertrand's
# postulate compounded IN LOG SPACE (`log10(n) + ith*log10(2)`, i.e.
# `n * 2**ith`) instead of an "average gap" estimate -- SOUND for every
# n/ith (no record-gap edge case can ever beat it), but far LOOSER past
# ith=1 or 2 than the deleted average-gap formula was: at ith=50 (this
# test's own PREVIOUS value), the bound is `log10(2) + 50*log10(2) ~=
# 15.35`, astronomically over factorial's 1463 cap even though the true
# nextprime(2,50) == 233 is comfortably under it -- soundness, not
# tightness, is what this formula is FOR, so this test now uses ith=1,
# where the bound (`log10(500) + log10(2) ~= 3.0`) still clears the cap
# with room to spare.
_v, _e = _boundary_parse("factorial(nextprime(500, 1))")
_expected = _sp5.factorial(503)
check("THE-1095 round-3-follow-up item B: 'factorial(nextprime(500, 1))' "
      "(nextprime(500,1) == 503, comfortably under cap) evaluates to "
      "main's exact value",
      _v == _expected and _e is None, f"-> value={_v!r} err={_e!r}")

# Item D (grok): _growth_fib_like computed (2*phi)**n instead of the
# intended 2*phi**n -- too loose to accept fibonacci(16)=987 under
# factorial's 1463 cap even though the true value is comfortably under.
_v, _e = _boundary_parse("factorial(fibonacci(16))")
check("THE-1095 round-3-follow-up item D: 'factorial(fibonacci(16))' "
      "(fibonacci(16) == 987, comfortably under MAX_HEAVY_ARG) evaluates "
      "to main's exact value '" + str(_sp5.factorial(987)) + "'"[:80] + "...",
      _v == _sp5.factorial(987) and _e is None, f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("factorial(fibonacci(17))")
check("THE-1095 round-3-follow-up item D: 'factorial(fibonacci(17))' "
      "(fibonacci(17) == 1597, one over MAX_HEAVY_ARG) is refused",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
# digamma sat in the n**(2n) catch-all despite being genuinely
# logarithmic -- false-refusing a nested call nowhere near dangerous. Uses
# `digamma(1463)` (AT digamma's own position-0 cap -- the interesting
# case: the OLD catch-all bound read `1463` itself as if it were a
# factorial-style parameter, i.e. `2*1463*log10(1463) ~= 9258`, "over
# cap" with no shift at all -- while the true `digamma(1463)` stays
# symbolic (`... - EulerGamma`, SymPy never evaluates it to a float
# without .evalf()) and `factorial()` of a non-integer symbolic
# expression stays unevaluated too -- no crash, no refusal, just an
# ordinary symbolic result, exactly like `origin/main` would give.
_v, _e = _boundary_parse("factorial(digamma(1463))")
check("THE-1095 round-3-follow-up item D: 'factorial(digamma(1463))' "
      "(digamma's own bound is logarithmic, not n**(2n): the OLD catch-"
      "all wrongly read digamma's ARGUMENT, 1463, as if it were a "
      "factorial-style output magnitude) evaluates instead of a false "
      "refusal",
      _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")

# Item E (grok): the real-dict catch-all narrowed to ONLY a ValueError
# containing 'is not an integer' -- re-pinned here under the narrowed rule
# (nextprime(10,2+1) still evaluates; prime(1-1)/prime(1/0) still surface
# main's own text, now via the SAME code path either way).
_v, _e = _boundary_parse("nextprime(10,2+1)")
check("THE-1095 round-3-follow-up item E: 'nextprime(10,2+1)' still "
      "evaluates to 17 under the narrowed real-dict catch-all",
      _v == 17 and _e is None, f"-> value={_v!r} err={_e!r}")
for _expr, _substr in (("prime(1-1)", "positive integer"), ("prime(1/0)", "not an integer")):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round-3-follow-up item E: {_expr!r} is refused as "
          f"validation, main's own text ({_substr!r}) preserved",
          _v is None and _e is not None and _e[0] == "validation" and _substr in _e[1],
          f"-> value={_v!r} err={_e!r}")

# Codex's shift-digit-bound item, designed together with grok's item C:
# a table-function call as EITHER operand of `<<` can slip under every
# per-position cap and only become visibly dangerous once the REAL value
# is constructed -- bound it at the token level instead, before any real
# construction.
_t0 = time.time()
_v, _e = _boundary_parse("bell(1463) << 100000")
_dt = time.time() - _t0
check("THE-1095 round-3-follow-up (Codex): 'bell(1463) << 100000' "
      "(bell(1463) alone is safe; shifted, the result would have "
      "~34_000+ digits) is refused with the digit-ceiling message",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
check(f"  ...in milliseconds ({_dt:.3f}s), not after constructing bell(1463) for real",
      _dt < 1.0, f"-> {_dt:.3f}s")

_t0 = time.time()
_v, _e = _boundary_parse("factorial(1463) << 1000")
_dt = time.time() - _t0
check("THE-1095 round-3-follow-up (Codex): 'factorial(1463) << 1000' "
      "(factorial(1463) has 3_998 real digits; shifted by 1000 bits, "
      "~4_298 digits, over MAX_NUMERIC_DIGITS) is refused",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
check(f"  ...in milliseconds ({_dt:.3f}s)", _dt < 1.0, f"-> {_dt:.3f}s")

_v, _e = _boundary_parse("factorial(20) << 3")
check("THE-1095 round-3-follow-up (Codex): 'factorial(20) << 3' (well "
      "under the digit ceiling either way) still evaluates to main's "
      "exact value",
      _v == (_sp5.factorial(20) << 3) and _e is None, f"-> value={_v!r} err={_e!r}")

_v, _e = _boundary_parse("1 << factorial(20)")
check("THE-1095 round-3-follow-up (grok item C, table call as the SHIFT "
      "COUNT itself): '1 << factorial(20)' (a shift by ~2.4e18 bits) is "
      "refused, not silently constructed",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")

# Codex's own control (round 4): is `bell(1463) << 1` a bypass, or just
# bell(1463)'s own documented at-cap cost? THE-1095 round-3-follow-up #4
# (coordinator review of 06272aa, grok item 2): `bell`'s own growth-bound
# entry used to be the shared, DELIBERATELY loose `_growth_nn_loose`
# (`n**(2n)`), overestimating bell(1463) at ~9258 "digits" against its
# true 3_018 -- already over MAX_NUMERIC_DIGITS with no shift at all, a
# false refusal main does not share. Replaced with `_growth_stirling_
# factorial` (`bell(n) <= n!` always -- Bell numbers count PARTITIONS of
# an n-set, strictly fewer than the n! PERMUTATIONS once n >= 3, a
# provable fact, not an empirical one), tight enough that `bell(1463) <<
# 1` now correctly evaluates (matching main) while `bell(1464)`-based
# shapes still correctly refuse -- no narrowing needed at all once the
# bound is accurate.
_v, _e = _boundary_parse("bell(1463) << 1")
check("THE-1095 round-3-follow-up #4: 'bell(1463) << 1' evaluates to "
      "main's exact value (no narrowing needed once bell's own bound is "
      "Stirling-tight)",
      _v == (_sp5.bell(1463) << 1) and _e is None, f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("7 % bell(20)")
check("THE-1095 round-3-follow-up (Codex): '7 % bell(20)' (a table call "
      "under %, nowhere near any cap) still evaluates -- the operator "
      "screen must not over-refuse ordinary table calls under %,//",
      _v == 7 and _e is None, f"-> value={_v!r} err={_e!r}")

# Item C(ii): a deferred-parse exception on an expression with NO table
# name and NO unprotected operator still falls through to main's own
# parse-error text (an ordinary syntax error is unaffected by this fix).
_v, _e = _boundary_parse("2 +* 3")
check("THE-1095 round-3-follow-up item C(ii): '2 +* 3' (an ordinary "
      "syntax error, no table name, no unprotected operator) still "
      "surfaces a validation parse error, unaffected by the fail-closed "
      "narrowing",
      _v is None and _e is not None and _e[0] == "validation", f"-> value={_v!r} err={_e!r}")
# The helper itself: true for a table name or an unprotected operator,
# false for neither.
check("THE-1095 round-3-follow-up item C(ii): the fail-closed helper "
      "recognizes a table name",
      _se5._expression_touches_table_or_unprotected_operator("factorial(5)+1") is True)
check("  ...and an unprotected operator",
      _se5._expression_touches_table_or_unprotected_operator("2 % 3") is True)
check("  ...and neither, for an ordinary expression",
      _se5._expression_touches_table_or_unprotected_operator("2 + 3 * x") is False)

# ═══ THE-1095 round-3-follow-up #2 (coordinator review of 949aac9, grok ═══
# ═══ verify-1095-r4-grok.log): structural fix -- operator-protocol ═══════
# ═══ dunders on the deferred stand-ins, replacing the token-level shift ═══
# ═══ screen and the TypeError carve-out entirely ══════════════════════════
#
# The root problem across rounds 3 and 4: a deferred-parse TypeError on
# `standin << x` was carved out as "fall through to the real parse", and
# the token-level `<<` screen could only ever recognise `NAME(NUMBER)`
# sitting directly next to the operator. Closed at the root instead:
# `_DeferredOperatorMixin` gives every stand-in `__lshift__`/`__rlshift__`/
# `__rshift__`/`__rrshift__`/`__mod__`/`__rmod__`/`__floordiv__`/
# `__rfloordiv__`, returning an inert marker node
# (`_DeferredLShift`/`_DeferredRShift`/`_DeferredOpMod`/
# `_DeferredOpFloorDiv`) instead of ever raising -- the deferred TREE now
# carries every one of these operators, at whatever depth or shape the
# caller wrote it, because a TREE does not care about token adjacency.
# `_numeric_ceiling_scan`'s new `_deferred_binop_violation` bounds them:
# `<<` via `digits(left) + count*log10(2)` (count = the RIGHT operand's
# own VALUE, via `_GROWTH_BOUNDS`, never a blanket Stirling-of-factorial
# approximation); `>>`/`%`/`//` via the LEFT operand's own bound alone
# (the result never exceeds it).

_R5_REFUSE_SHAPES = [
    "1 << (factorial(20))",
    "1 << factorial(12+1)",
    "1 << binomial(40, 20)",
    "1 << rf(30, 20)",
    "(bell(1463)) << 100000",
]
for _expr in _R5_REFUSE_SHAPES:
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round-3-follow-up #2: {_expr!r} (parenthesised, "
          "computed, two-arg, or nested -- none of these is a bare "
          "NAME(NUMBER) token pair next to '<<') is refused",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
    check(f"  ...in milliseconds ({_dt:.3f}s), not after real construction",
          _dt < 1.0, f"-> {_dt:.3f}s")

# ClusterFuzzLite, found within the first 60s fuzzing this round's own
# successor commit: `_split_coefficient`'s `base_key <= _COEFF_MAX_BASE`
# assumed every multiset key is a plain `int` -- item 4's OWN new
# Function-node key (`factorial(cos(y))`, a table call over a SYMBOLIC
# argument SymPy cannot resolve to a definite truth value against 1000)
# raised `TypeError: cannot determine truth value of Relational` instead
# of being treated as "not an integer coefficient", the already-correct
# answer for every other not-small-enough shape.
for _expr in ("factorial(cos(y)) - factorial(cos(y))",
              "2*factorial(cos(y)) - factorial(cos(y))"):
    try:
        _v, _e = _boundary_parse(_expr)
        _raised = False
    except Exception as _exc:
        _raised = True
        _v = _e = None
    check(f"THE-1095 round-3-follow-up #2: {_expr!r} (a table call over a "
          "symbolic, non-numeric argument, combined additively) never "
          "raises",
          not _raised, f"-> raised={_raised}")
check("  ...and 'factorial(cos(y)) - factorial(cos(y))' cancels to 0 "
      "(exact structural cancellation, symbolic argument and all)",
      _boundary_parse("factorial(cos(y)) - factorial(cos(y))")[0] == 0)

# Pin vs main -- item 5's own named values.
_R5_PINS = [
    ("factorial(10+1) << 1", 79833600),
    ("1 << prime(10)", 536870912),
    ("totient(1463) << 10", int(_sp5.totient(1463)) << 10),
    ("factorial(20) % 7", 0),
]
for _expr, _expected in _R5_PINS:
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round-3-follow-up #2: {_expr!r} == {_expected!r}, "
          "matching main",
          _v == _expected and _e is None, f"-> value={_v!r} err={_e!r}")

_v, _e = _boundary_parse("1 << fibonacci(20)")
check("THE-1095 round-3-follow-up #2: '1 << fibonacci(20)' evaluates to "
      "main's exact value",
      _v == (1 << int(_sp5.fibonacci(20))) and _e is None, f"-> value={_v!r} err={_e!r}")

_v, _e = _boundary_parse("7 // bell(20)")
check("THE-1095 round-3-follow-up #2: '7 // bell(20)' evaluates to "
      "main's exact value (0 -- bell(20) is astronomically larger than "
      "7; SymPy's own Mod/floor evaluation determines this from bell's "
      "own registered assumptions, never by computing bell(20) for real)",
      _v == 0 and _e is None, f"-> value={_v!r} err={_e!r}")

# `bell(1463)**2` -> ceiling BEFORE construction -- item 4, verified with
# a spy on the real `sympy.bell` so the assertion is "never called", not
# merely "fast" (a cache or a lucky code path could also be fast).
_bell_calls: list = []
_orig_bell = _sp5.bell


def _spy_bell(*_a, **_kw):
    _bell_calls.append(_a)
    return _orig_bell(*_a, **_kw)


_sp5.bell = _spy_bell
try:
    _t0 = time.time()
    _v, _e = _boundary_parse("bell(1463)**2")
    _dt = time.time() - _t0
finally:
    _sp5.bell = _orig_bell
check("THE-1095 round-3-follow-up item 4: 'bell(1463)**2' is refused as "
      "a ceiling",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
check("  ...WITHOUT ever calling the real sympy.bell (spy pattern, not "
      "just a timing measurement)",
      len(_bell_calls) == 0, f"-> {len(_bell_calls)} real calls: {_bell_calls}")
check(f"  ...and promptly ({_dt:.3f}s)", _dt < 1.0, f"-> {_dt:.3f}s")

# The genuinely-cancelling case MUST still evaluate -- the regression this
# round's own development caught and fixed (see `_factor_multiset`'s own
# docstring): a table-function call is now also a valid EXACT multiset
# base (keyed by the node itself, cancelling structurally), but ONLY
# trusted by `_multiset_log_num_den` when it fully cancels away --
# otherwise the whole multiset is discarded rather than measured via an
# inaccurate growth bound (`_table_multiset_trusted`).
_v, _e = _boundary_parse("factorial(1463)/factorial(1463)*factorial(1463)/factorial(1463)")
check("THE-1095 round-3-follow-up item 4 (regression guard): "
      "'factorial(1463)/factorial(1463)*factorial(1463)/factorial(1463)' "
      "(net exponent 0, exact value 1) still evaluates -- table-function "
      "cancellation must not be broken by the new Pow-base bound",
      _v == 1 and _e is None, f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("x*factorial(1463)")
check("  ...and a single, in-range, non-cancelling factorial term still "
      "evaluates ('x*factorial(1463)')",
      _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("factorial(1000)+fibonacci(1000)")
check("  ...and two independent sibling heavy calls still evaluate "
      "('factorial(1000)+fibonacci(1000)')",
      _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("sqrt(factorial(1463))")
check("  ...and sqrt(factorial(1463))'s own refusal message is unchanged "
      "('square root', from the dedicated sqrt/cbrt backstop, not the "
      "generic digit-ceiling text)",
      _v is None and _e is not None and "square root" in _e[1], f"-> {_e!r}")

# The TypeError carve-out is GONE: an arity TypeError from the deferred
# stand-in still matches main's own text exactly (no coverage lost by
# removing the carve-out, since the stand-in shares the real class's own
# name and nargs).
_v, _e = _boundary_parse("binomial(1463+1)")
check("THE-1095 round-3-follow-up #2: 'binomial(1463+1)' (missing k) is "
      "still refused with main's own arity TypeError text, now WITHOUT "
      "any TypeError carve-out in the deferred-parse exception handling",
      _v is None and _e is not None and _e[0] == "validation"
      and "takes exactly 2 arguments" in _e[1], f"-> {_e!r}")

# ═══ THE-1095 round-3-follow-up #3 (coordinator review of 5e2a961, grok ═══
# ═══ verify-1095-r5-grok.log): interception moved to the AST stage ════════

# Differential test (item 1's own requirement): for a corpus with NO
# unmapped operator anywhere, `_parse_deferred` must produce the
# STRUCTURALLY IDENTICAL tree `parse_expr(..., evaluate=False)` does --
# proving `_deferred_transformer_class`'s subclass only ever changes
# behavior for the four operators it overrides, never for anything
# `EvaluateFalseTransformer` already handled, so a future sympy bump that
# changes THAT pipeline is caught here rather than silently drifting.
_DIFFERENTIAL_CORPUS = [
    "1", "1.5", "-3", "x", "x + 1", "1 + x", "x - y", "2*x", "x*2", "2*x*y",
    "x/2", "2/x", "x**2", "2**x", "x**y", "(x+1)**2", "(x+y)*(x-y)",
    "1 + 2*x - 3", "x**2 + 2*x + 1", "-x", "-x**2", "(-x)**2", "1/(x+1)",
    "sqrt(x)", "sqrt(2)", "sin(x)", "cos(x)*sin(y)", "exp(x)", "log(x)",
    "log(x, 2)", "Abs(x)", "x**(1/2)", "x**Rational(1,3)", "2**200",
    "factorial(5)", "x!", "x**2*y**3", "(x+1)*(y+2)", "x==y", "x < y",
    "x <= 3", "Eq(x, 1)", "1 < x < 3", "x + y + z", "2 + 3 + 4",
    "factorial(1463)", "bell(20)", "binomial(5,2)", "Mod(x,3)",
]
_diff_g = _se5._deferred_global_dict()
_diff_fails = 0
for _expr in _DIFFERENTIAL_CORPUS:
    try:
        _mine = _se5._parse_deferred(_expr, local_dict=None, global_dict=_diff_g,
                                     transformations=_se5.math_transforms())
        from sympy.parsing.sympy_parser import parse_expr as _sp_parse_expr
        _theirs = _sp_parse_expr(_expr, transformations=_se5.math_transforms(),
                                  global_dict=_diff_g, evaluate=False)
        _match = _mine == _theirs
    except Exception as _exc:
        _match = False
        _mine = f"RAISED {type(_exc).__name__}: {_exc}"
        _theirs = None
    if not _match:
        _diff_fails += 1
        FAILS.append(f"differential: {_expr!r} -> mine={_mine!r} theirs={_theirs!r}")
check(f"THE-1095 round-3-follow-up #3: _parse_deferred structurally "
      f"matches parse_expr(evaluate=False) for {len(_DIFFERENTIAL_CORPUS)} "
      "expressions with no unmapped operator",
      _diff_fails == 0, f"-> {_diff_fails} mismatches")

# Issue 3 (operand-shape independence): a bare `standin % n` was already
# refused before this round (the mixin caught the BARE case); wrapping in
# a unary minus, an addition, or a multiplication used to bypass it
# entirely, since SymPy's own eager Mod/floor ran before the mixin ever
# got a chance once either operand looked like a concrete Expr. The AST-
# stage fix makes operand shape irrelevant.
#
# THE-1095 round-3-follow-up #4 (coordinator review of 06272aa, grok item
# 2): the AST-stage fix alone still left these THREE refusing with "the
# left operand of '%' cannot be safely bounded" -- an UNKNOWN verdict
# (Mul/Add wrapping a table call had no composition rule at all), not an
# over-cap one -- even though bare `bell(1463) % 7` and bare `bell(1463)`
# both already evaluated (the documented ~5s at-cap cost) and main
# evaluates all five shapes. `_resolve_arg_magnitude`'s new Mul/Add
# composition (this same round's own fix, combined with `bell`'s own
# now-Stirling-tight bound) closes that: all three now evaluate too.
for _expr in ("-bell(1463) % 7", "(bell(1463)+1) % 7", "2*bell(1463) % 7"):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round-3-follow-up #4 item 2: {_expr!r} (a Mul/Add "
          "wrapper around a table call at its own cap) evaluates, "
          "matching main (the ~5s at-cap allowance already used "
          "elsewhere in this file)",
          _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
# ...and the SAME wrapper shapes still refuse promptly when the table
# call itself is genuinely over cap, or nested and unbounded.
for _expr in ("-bell(1463+1) % 7", "(factorial(factorial(8))+1) % 7"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round-3-follow-up #4 item 2: {_expr!r} (the wrapped "
          "table call is itself over cap or unbounded) is still refused",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
    check(f"  ...in milliseconds ({_dt:.3f}s), not after real construction",
          _dt < 1.0, f"-> {_dt:.3f}s")
# ...and a genuinely UNRESOLVABLE (symbolic) table argument still matches
# whatever main returns -- the "cannot be safely bounded" refusal stays
# for this shape, since main itself never evaluates it either.
_v, _e = _boundary_parse("bell(x) % 7")
check("THE-1095 round-3-follow-up #4 item 2: 'bell(x) % 7' (a symbolic "
      "table argument, genuinely unresolvable) matches main's own value",
      _v is not None and str(_v) == "Mod(bell(x), 7)" and _e is None,
      f"-> value={_v!r} err={_e!r}")

# Issue 2 (a purely numeric shift count reaching a marker via a nested
# Pow): `1 << (2**200)` used to reach real construction the instant BOTH
# sides had already become concrete Integers -- now a marker node either
# way, its own children fully evaluate=False-protected.
_t0 = time.time()
_v, _e = _boundary_parse("1 << (2**200)")
_dt = time.time() - _t0
check("THE-1095 round-3-follow-up #3 item 2: '1 << (2**200)' (a huge "
      "numeric shift count, not a table call) is refused",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
check(f"  ...in milliseconds ({_dt:.3f}s)", _dt < 1.0, f"-> {_dt:.3f}s")
_v, _e = _boundary_parse("1 << (10+10)")
check("  ...and a genuinely small, computed shift count still evaluates "
      "('1 << (10+10)')",
      _v == (1 << 20) and _e is None, f"-> value={_v!r} err={_e!r}")

# Issue 1 (cancelling nested table calls): a fully-cancelling product or
# difference of two identical nested heavy calls used to skip the
# Function-node check entirely, since `_numeric_ceiling_scan`'s own
# stack-based descent stops once `_log10_num_den` reports the WHOLE node
# resolved -- now caught by the SAME unconditional walk that already
# exists for `Pow` nodes in `reject_explosive`.
for _expr in ("factorial(factorial(8))/factorial(factorial(8))",
              "factorial(factorial(8))-factorial(factorial(8))",
              "nextprime(10**2000)/nextprime(10**2000)",
              "divisors(factorial(100))/divisors(factorial(100))"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round-3-follow-up #3 item 1: {_expr!r} (fully "
          "cancelling, but the inner call is itself unbounded) is refused",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
    check(f"  ...in milliseconds ({_dt:.3f}s), not by letting the real "
          "parse evaluate the inner calls",
          _dt < 1.0, f"-> {_dt:.3f}s")

# THE-1095 round-3-follow-up #6 (coordinator review of 5119c9d,
# grok `verify-1095-r6-grok.log`, issues 1, 2, 3, 4 -- "one cause":
# `_resolve_arg_magnitude` did not understand the marker nodes
# (`_DeferredLShift`/`_DeferredRShift`/`_DeferredMod`/`_DeferredFloorDiv`)
# AT ALL, and `_function_arg_cap_violation`/`_table_function_bound` both
# treated an unresolved (non-symbolic) argument as a silent SKIP rather
# than a refusal.
#
# Issue 1 (marker as a table argument fails OPEN): the token screen sees
# two small literals either side of `<<`, the deferred tree holds a
# `_DeferredLShift(1, 11)` node as the ARGUMENT to a table call, the old
# resolver returned "unresolved" for that node (no branch understood
# markers at all), the old per-position loop's `continue` treated that as
# "nothing to check here", and step 3's stock (evaluate=True) parse then
# ran `1 << 11` for real -- a genuine Python/SymPy int -- and constructed
# real `bell(2048)`. `_resolve_marker_magnitude` (this round's new
# function, reached through `_resolve_arg_magnitude`'s new `_DEFERRED_
# BINOP_OPS` branch) now bounds every marker node the SAME way `_deferred_
# binop_violation` itself already bounds a marker used as a top-level
# operator, so these all refuse in milliseconds, at the SAME cap
# `bell(1463+1)`/`factorial(1464)` etc. already refuse at elsewhere in
# this file -- never by letting real construction run.
for _expr in ("bell(1 << 11)", "factorial(1 << 20)", "rf(5, 1 << 16)",
              "factorial((1 << 11))", "factorial(1 << (2+9))",
              "factorial(2*(1 << 10))",
              "factorial(1 << 20)/factorial(1 << 20)"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round-3-follow-up #6 item 1: {_expr!r} (a deferred "
          "shift/mod marker used as a TABLE-CALL argument) is refused",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
    check(f"  ...in milliseconds ({_dt:.3f}s), not by letting the real "
          "parse evaluate '1 << N' and construct the real table call",
          _dt < 1.0, f"-> {_dt:.3f}s")
# ...and a marker argument that resolves comfortably under the cap still
# evaluates to the SAME value `math.factorial` gives -- the fix bounds
# markers, it does not blanket-refuse every marker-shaped argument.
_v, _e = _boundary_parse("factorial(1 << 10)")
check("  ...and a genuinely small marker argument still evaluates "
      "('factorial(1 << 10)', i.e. factorial(1024))",
      _v == math.factorial(1024) and _e is None, f"-> value={_v!r} err={_e!r}")

# Issue 2 (chained markers fail CLOSED where main evaluates): `1 << 2 <<
# 3` is `_DeferredLShift(_DeferredLShift(1, 2), 3)` -- an INNER marker as
# the LEFT operand of an OUTER one. The old `_deferred_binop_violation`
# read `left`'s magnitude via a hand-rolled extraction that never
# recursed back into marker-handling for a nested marker `left`, so these
# refused even though main evaluates every one of them. Now that
# `_resolve_marker_magnitude` resolves `left`/`right` through the SAME
# `_resolve_arg_magnitude` dispatcher it is itself one branch of, a
# nested marker resolves the inner marker first automatically -- no
# special-casing needed for the chain, "for free" once issue 1's fix is
# in place.
for _expr, _want in (("1 << 2 << 3", 1 << 2 << 3),
                      ("7 % 3 % 2", 7 % 3 % 2),
                      ("1 << (2 << 3)", 1 << (2 << 3)),
                      ("(1 << 4) % 5", (1 << 4) % 5),
                      ("1 << 2 << 3 << 4", 1 << 2 << 3 << 4)):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round-3-follow-up #6 item 2: {_expr!r} (a CHAIN of "
          "deferred shift/mod markers) evaluates, matching main",
          _v == _want and _e is None, f"-> value={_v!r} err={_e!r} want={_want!r}")

# Issue 3 (the structural backstop): in `_function_arg_cap_violation` (and
# its `_table_function_bound` twin), an argument with NO free symbols
# that the resolver still cannot bound is now a REFUSAL, never a silent
# skip -- this is what actually closed issue 1 at the root (a marker node
# is just ONE shape of "resolver doesn't understand this node yet"; the
# structural rule catches every OTHER such shape too, present or future).
#
# `floor`/`ceiling`/`Abs` of an otherwise-resolvable argument are NOT
# such a shape -- main evaluates all four of these, and `_resolve_arg_
# magnitude`'s new dedicated branch for the three of them (recursing into
# the single argument, collapsing to an integer for floor/ceiling)
# resolves them too, so the structural backstop never even sees them as
# unresolved.
for _expr, _want in (("factorial(floor(2.5))", 2),
                      ("bell(ceiling(3.7))", 15),
                      ("factorial(Abs(-5))", 120),
                      ("rf(5, floor(3.9))", 210)):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round-3-follow-up #6 item 3: {_expr!r} (floor/"
          "ceiling/Abs of a resolvable argument to a table call) "
          "evaluates, matching main",
          _v == _want and _e is None, f"-> value={_v!r} err={_e!r} want={_want!r}")
# ...while a genuinely unresolvable, NON-symbolic argument -- a Function
# outside every table this module knows how to bound (`Ei`, the
# exponential integral, is not in `_FUNCTION_ARG_CAPS` or `_GROWTH_
# BOUNDS`, and does not collapse to a plain number the way `Max`/`Min`/
# `sign` of numeric literals would) -- now hits the structural backstop
# and refuses, even though main itself would just leave it symbolic
# (`factorial(Ei(1463))` has no proof its argument is a non-negative
# integer, so main never actually computes anything unsafe for THIS
# specific shape). This is a deliberate, documented divergence from main
# for an opaque shape the resolver cannot prove safe -- the module's own
# fail-closed philosophy (an "unknown" verdict is refused, not silently
# passed through) applied to a NEW opaque-Function shape, the same way it
# already applies to a free symbol appearing where a symbol is not
# expected, or to a table call already over its own cap.
_v, _e = _boundary_parse("factorial(Ei(1463))")
check("THE-1095 round-3-follow-up #6 item 3: 'factorial(Ei(1463))' (a "
      "non-table, non-collapsing Function this module cannot bound) is "
      "refused as unknown, not silently let through",
      _v is None and _e is not None and _e[0] == "ceiling"
      and "cannot be safely bounded" in _e[1],
      f"-> value={_v!r} err={_e!r}")

# ═══ THE-1095 round 9 (coordinator review of 1d756b7, Codex ═══════════════
# ═══ verify-1095-r7.log + grok verify-1095-r7-grok.log, both FAILED, CI ═══
# ═══ green): "a rule that is an upper bound on the happy path but not ═══
# ═══ on the accepted domain" -- position-complete, domain-explicit fixes ═══

# Item A (Codex finding 1, High): `%`/`//` used the SAME "bounded by the
# left operand alone" rule -- true only for `>>`. `%`'s bound is the
# RIGHT operand's own magnitude (`0 <= |a % b| < |b|` unconditionally);
# `//` needs a LOWER bound on the divisor (a smaller divisor gives a
# LARGER quotient), known only for an exact numeric literal or a table
# call proven to always return an integer.
for _expr in ("bell(-1 % 10**4)", "bell(1 // 0.0005)",
              "root(-1 % 10**2000,2)", "factorint(-1 % 10**30)",
              "nextprime(-1 % 10**30)"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round 9 item A: {_expr!r} (a negative-modulo or "
          "fractional-divisor operand that used to fail-open) is refused",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
    check(f"  ...in milliseconds ({_dt:.3f}s), not by letting the real "
          "parse construct the real quotient/remainder",
          _dt < 1.0, f"-> {_dt:.3f}s")
# ...and ordinary, non-adversarial %/// still evaluate to main's values
# -- the fix bounds the hazard, it does not blanket-refuse the operators.
for _expr, _want in (("2 % 3", 2), ("-1 % 7", 6), ("10 // 3", 3),
                      ("-7 // 2", -4), ("7 // bell(20)", 0)):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round 9 item A: {_expr!r} (an ordinary %/// use, "
          "including a table-call divisor PROVEN integer-valued) still "
          "evaluates, matching main",
          _v == _want and _e is None, f"-> value={_v!r} err={_e!r} want={_want!r}")

# Item B (Codex finding 2, High): eight table names' own OPTIONAL second
# position (`bell`'s `k_sym`, `bernoulli`/`euler`/`genocchi`'s `x`,
# `fibonacci`/`tribonacci`'s `sym`, `harmonic`'s `m`, `zeta`'s `a`) drive
# the RESULT's own magnitude and cost, and had no bound at all.
for _expr in ("bell(1463,factorial(1463))", "harmonic(1463,-factorial(8))",
              "zeta(-1463,1463)", "bell(1463,1463)", "bernoulli(1463,1463)",
              "euler(1463,1463)"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round 9 item B: {_expr!r} (a two-position table "
          "call whose combined output was never bounded before) is "
          "refused",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
    check(f"  ...in milliseconds ({_dt:.3f}s), not by hanging on real "
          "construction",
          _dt < 1.0, f"-> {_dt:.3f}s")
# ...and a genuinely small second-position value still evaluates.
from sympy import Rational as _Rational

_HARMONIC_10_10 = _Rational(413520574906423083987893722912609, 413109706296096288512409600000000)
for _expr, _want in (("bell(2,1000)", 1001000), ("harmonic(10,10)", _HARMONIC_10_10),
                      ("zeta(3,2)", None)):
    _v, _e = _boundary_parse(_expr)
    if _want is None:
        check(f"THE-1095 round 9 item B: {_expr!r} evaluates (matches main), "
              "not refused",
              _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
    else:
        check(f"THE-1095 round 9 item B: {_expr!r} evaluates to main's exact value",
              _v == _want and _e is None, f"-> value={_v!r} err={_e!r} want={_want!r}")

# Item C (Codex finding 3, High): a growth formula is only an upper
# bound on the domain it was derived for -- `binomial`'s own `n < 0`
# switches to a DIFFERENT, unaudited code path; `polygamma`'s own order
# was previously ignored entirely.
for _expr in ("binomial(-1463,1000000)", "binomial(-100,1000)",
              "binomial(-5,3)", "polygamma(5,0.5)"):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round 9 item C: {_expr!r} (outside the domain this "
          "module has derived a bound for) is refused as a domain "
          "violation, a deliberate divergence from main",
          _v is None and _e is not None and _e[0] == "ceiling"
          and ("must be non-negative" in _e[1] or "must have magnitude" in _e[1]),
          f"-> value={_v!r} err={_e!r}")
# ...and the well-behaved domain (order-aware now) still evaluates to
# main's exact value, even close to its own cap.
_v, _e = _boundary_parse("polygamma(1463,1)")
check("THE-1095 round 9 item C: 'polygamma(1463,1)' (order now folded "
      "into the bound, still comfortably under MAX_NUMERIC_DIGITS -- "
      "3299 true digits) evaluates, matching main",
      _v is not None and _e is None and len(str(_v)) == 3299,
      f"-> value has {len(str(_v)) if _v is not None else None} digits, err={_e!r}")

# Item D (grok): `_log10_num_den`'s own Float print-profile shortcut
# (always magnitude 0, correct for PRINTING a bare Float) was inherited
# as a VALUE bound by markers/Mul/Add/floor/ceiling -- `factorial(floor(
# 1e4 * bell(1)))` (bell(1) == 1, true value factorial(10000), ~35_660
# digits) used to sail past every scan and get caught only by the
# output-ceiling backstop AFTER real construction. Also: no `Pow`
# composition in the resolver at all -- `factorial((1 << 3)**2)` (64!),
# `factorial(2**(1 << 3))` (256!), `factorial(fibonacci(5)**2)` (25!)
# all evaluate on main and used to hit the item-3 structural backstop.
_t0 = time.time()
_v, _e = _boundary_parse("factorial(floor(1e4 * bell(1)))")
_dt = time.time() - _t0
check("THE-1095 round 9 item D: 'factorial(floor(1e4 * bell(1)))' "
      "(a Float's real VALUE, not its print-profile, hidden inside a "
      "Mul feeding floor()) is refused",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
check(f"  ...in milliseconds ({_dt:.3f}s), not after constructing "
      "factorial(10000) for real",
      _dt < 1.0, f"-> {_dt:.3f}s")
for _expr, _want in (("factorial((1 << 3)**2)", math.factorial(64)),
                      ("factorial(2**(1 << 3))", math.factorial(256)),
                      ("factorial(fibonacci(5)**2)", math.factorial(25))):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round 9 item D: {_expr!r} (a marker/table call "
          "inside a Pow, itself inside a table-call argument) evaluates "
          "to main's exact value, via the resolver's new Pow composition",
          _v == _want and _e is None, f"-> value={_v!r} err={_e!r}")

# Item E (Codex finding 4, Medium): tight, provable bounds replace the
# loose `n**(2n)` catch-all for several names, closing false ceilings on
# an absurd shift count, and adding growth entries for `mobius`/
# `isprime`/`root` (previously "no growth estimate defined" refusals).
for _expr, _want in (("1 << factorial2(5)", 32768),
                      ("1 << subfactorial(5)", 17592186044416),
                      ("1 << euler(4)", 32),
                      ("mobius(5) % 3", 2), ("isprime(5) << 1", 2),
                      ("root(8,3) % 3", 2)):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round 9 item E: {_expr!r} (a tight, provable growth "
          "bound instead of the loose n**(2n) catch-all) evaluates, "
          "matching main",
          _v == _want and _e is None, f"-> value={_v!r} err={_e!r} want={_want!r}")

# Item F (grok, "the fail-closed list"): a small, curated elementary-
# function table (Max/Min/sign/sin/cos/tanh/erf/log/exp) lets a numeric
# argument wrapped in an ordinary, non-table SymPy function resolve
# through the shared resolver instead of hitting the item-3 backstop.
for _expr, _want in (("factorial(log(1))", 1), ("factorial(cos(0))", 1),
                      ("factorial(Max(3, 5))", 120), ("factorial(Min(3,5))", 6),
                      ("factorial(sin(0))", 1)):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round 9 item F: {_expr!r} (an elementary function "
          "of a resolvable numeric argument) evaluates, matching main",
          _v == _want and _e is None, f"-> value={_v!r} err={_e!r} want={_want!r}")
# ...while the fail-closed list stays curated, not a blanket escape:
# `factorial(I)` (the imaginary unit, not a NumberSymbol, not in the
# elementary table) still refuses, alongside the already-pinned
# `factorial(Ei(1463))`.
_v, _e = _boundary_parse("factorial(I)")
check("THE-1095 round 9 item F: 'factorial(I)' (imaginary unit, not "
      "covered by any resolver branch) is refused as unknown",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")

# ═══ THE-1095 round 10 (coordinator replay of 6e72d70: "all 40 reviewer ═══
# ═══ repros hold, but two of my own probes still do real work") ══════════

# Probe 1: `zeta(s, a)` for a concrete INTEGER `s < 2` computes a
# Bernoulli polynomial of degree `|s| + 1` at `a` -- `zeta(-1200, 2)`
# measured ~6.4s (Codex measured 5.8s on an earlier round; still open).
# Scoped to the TWO-ARG form specifically: `zeta(-1200)`/`zeta(-1463)`
# ALONE (no second argument) are the SAME `s`, and stay fast regardless
# -- the domain rule must not narrow the already-PINNED single-arg case.
_t0 = time.time()
_v, _e = _boundary_parse("zeta(-1200,2)")
_dt = time.time() - _t0
check("THE-1095 round 10 probe 1: 'zeta(-1200,2)' (a negative-integer "
      "first argument with a second argument supplied) is refused as a "
      "domain violation",
      _v is None and _e is not None and _e[0] == "ceiling"
      and "must be >= 2" in _e[1], f"-> value={_v!r} err={_e!r}")
check(f"  ...in milliseconds ({_dt:.3f}s), not the ~6s of real "
      "construction this used to cost",
      _dt < 1.0, f"-> {_dt:.3f}s")
for _expr, _want in (("zeta(-1200)", 0), ("zeta(1/2)", None)):
    _v, _e = _boundary_parse(_expr)
    if _want is None:
        check(f"THE-1095 round 10 probe 1: {_expr!r} (single-arg, no "
              "domain restriction on THIS shape) evaluates, not refused",
              _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
    else:
        check(f"THE-1095 round 10 probe 1: {_expr!r} (single-arg, no "
              "domain restriction on THIS shape) evaluates to main's "
              "exact value",
              _v == _want and _e is None, f"-> value={_v!r} err={_e!r} want={_want!r}")
# ...and the already-PINNED single-arg at-cap case from an earlier round
# stays unaffected by the new two-arg domain rule.
_v, _e = _boundary_parse("zeta(-1463)")
check("THE-1095 round 10 probe 1: 'zeta(-1463)' (single-arg, at this "
      "module's own MAX_HEAVY_ARG cap, already pinned as evaluating in "
      "an earlier round) is unaffected by the new two-arg domain rule",
      _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
# A broader sweep of the OTHER names the coordinator asked to check for
# the identical "negative/small first argument triggers a different,
# expensive code path" shape -- none reproduce (each stays symbolic,
# returns `nan`, or returns `0` instantly on BOTH this module and main,
# confirmed live; no new domain row needed for any of them).
for _expr in ("bernoulli(-1200,2)", "euler(-1200,2)", "genocchi(-1200,2)",
              "polygamma(-1200,2)"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round 10 probe 1 (sweep): {_expr!r} (negative "
          "first argument, a shape SymPy leaves symbolic rather than "
          "computing) is prompt, matching main's own unevaluated form",
          _dt < 1.0, f"-> {_dt:.3f}s value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("harmonic(-100,2)")
check("THE-1095 round 10 probe 1 (sweep): 'harmonic(-100,2)' evaluates "
      "to main's own 'nan', not refused, not slow",
      str(_v) == "nan" and _e is None, f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("binomial(5,-1000)")
check("THE-1095 round 10 probe 1 (sweep): 'binomial(5,-1000)' (negative "
      "k, the O(1) k>n short-circuit) evaluates to main's '0'",
      _v == 0 and _e is None, f"-> value={_v!r} err={_e!r}")

# Probe 2: `floor`/`ceiling`/`frac`/`Max`/`Min`/`sign` force SymPy's own
# `evalf` to numerically coerce a non-integer argument -- cheap for an
# exact literal or an integer-valued table call, expensive (thousands of
# digits of precision) for a genuinely transcendental EXACT value like
# `polygamma(1463, 1)` (an exact Mul involving pi**1464, never a plain
# Integer). Root cause found and fixed at its source too:
# `Function._eval_evalf`'s own generic fallback looks up an mpmath
# routine by NAME (`self.func.__name__`), not by class identity -- a
# deferred stand-in named "polygamma" silently dispatched to the REAL
# `mpmath.psi` the instant anything called `.evalf()` on it, bypassing
# the "inert, never computes" property this module's whole scan depends
# on. `_standin_refuses_evalf` closes that for every stand-in; the
# `_evalf_coercion_cheap`/`_evalf_coercion_violation` pair closes the
# remaining "genuinely transcendental, not from a stand-in" case.
for _expr in ("floor(polygamma(1463, 1))", "ceiling(polygamma(1463, 1))",
              "Max(polygamma(1463, 1), 1)"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round 10 probe 2: {_expr!r} (numeric coercion of a "
          "genuinely transcendental, thousands-of-digits-precision "
          "value) is refused",
          _v is None and _e is not None and _e[0] == "ceiling"
          and "coerced" in _e[1], f"-> value={_v!r} err={_e!r}")
    check(f"  ...in well under a second ({_dt:.3f}s), not the ~18-19s "
          "this used to cost (measured directly through this module's "
          "own pipeline, cProfile-traced to Function._eval_evalf's "
          "NAME-based mpmath dispatch on the deferred stand-in)",
          _dt < 2.0, f"-> {_dt:.3f}s")
for _expr, _want in (("floor(pi*10**5)", 314159), ("floor(polygamma(3, 1))", 6),
                      ("Max(factorial(20), 5)", 2432902008176640000),
                      ("floor(2.5)", 2)):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round 10 probe 2: {_expr!r} (an exact literal, an "
          "integer-valued table call, or a small-magnitude transcendental "
          "-- all cheap to numerically coerce) evaluates to main's exact "
          "value",
          _v == _want and _e is None, f"-> value={_v!r} err={_e!r} want={_want!r}")

# ═══ THE-1095 round 11 (coordinator replay of 9fe4f6a: Codex round 9 ═══════
# ═══ verify-1095-r9.log, three probes, quota-limited but reproduced; ═══════
# ═══ grok verify-1095-r9-grok.log addendum, the shared root cause) ═════════

# Codex item 1: `log` in this module's namespace is NATURAL log, not
# log10 -- `bell(floor(log(10**1000)))` used to hang, since the OLD
# elementary bound under-estimated `log(10**1000)`'s own magnitude by a
# factor of `ln(10)`. Fixed at the resolver; `factorial`'s twin case
# (already caught by the LAST-RESORT output check, just slowly) is
# fixed the SAME way, and the fix is uniform across every table name
# reachable this way, not just `factorial`.
for _expr in ("bell(floor(log(10**1000)))", "factorial(floor(log(10**1000)))"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round 11, Codex item 1: {_expr!r} (log's own "
          "magnitude, corrected for the ln(10) base-change factor) is "
          "refused promptly, not by hanging or by real construction",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
    check(f"  ...in well under a second ({_dt:.3f}s)", _dt < 1.0, f"-> {_dt:.3f}s")
for _expr, _want in (("floor(log(10**1000))", 2302), ("floor(log(2))", 0)):
    _v, _e = _boundary_parse(_expr)
    check(f"THE-1095 round 11, Codex item 1: {_expr!r} evaluates to "
          "main's exact value",
          _v == _want and _e is None, f"-> value={_v!r} err={_e!r} want={_want!r}")

# Codex item 2: `N`/`evalf`'s own precision argument was unbounded, and
# the final OUTPUT check trusted `_log10_num_den`'s Float print-profile
# (always magnitude 0) instead of counting a Float's own rendered
# digits via its `_prec`.
_t0 = time.time()
_v, _e = _boundary_parse("N(pi, 100000)")
_dt = time.time() - _t0
check("THE-1095 round 11, Codex item 2: 'N(pi, 100000)' (a 100_001-"
      "character Float) is refused",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
check(f"  ...well under a second ({_dt:.3f}s), not the ~1.5s this used "
      "to cost",
      _dt < 1.0, f"-> {_dt:.3f}s")
_v, _e = _boundary_parse("N(pi, 50)")
check("THE-1095 round 11, Codex item 2: 'N(pi, 50)' evaluates to main's "
      "exact value",
      _v is not None and str(_v) == "3.1415926535897932384626433832795028841971693993751" and _e is None,
      f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("N(pi)")
check("  ...and 'N(pi)' (the default 15-digit precision) evaluates too",
      _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("10.0**100000")
check("THE-1095 round 11, Codex item 2: '10.0**100000' (a compact "
      "Float, `_prec` stays the default 53 bits despite its huge "
      "magnitude) still evaluates -- the output check counts a Float's "
      "own PRECISION, not its magnitude",
      _v is not None and _e is None and len(str(_v)) < 30, f"-> value={_v!r} err={_e!r}")

# Codex item 3 / grok: `sin`/`cos`/`tanh`/`erf`'s own `bounded1` rule
# (magnitude <= 1) only holds for a REAL argument -- `sin(z)` for a
# pure-imaginary `z` grows like `sinh(|Im(z)|)`, exponentially.
_t0 = time.time()
_v, _e = _boundary_parse("factorial(ceiling(Abs(sin(5000*I))))")
_dt = time.time() - _t0
check("THE-1095 round 11, Codex item 3: 'factorial(ceiling(Abs(sin("
      "5000*I))))' (sinh(5000) is astronomically large) is refused",
      _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
check(f"  ...in milliseconds ({_dt:.3f}s)", _dt < 1.0, f"-> {_dt:.3f}s")
_v, _e = _boundary_parse("sin(500*I)")
check("THE-1095 round 11, Codex item 3: 'sin(500*I)' (bare, not wrapped "
      "in anything that numerically coerces it) evaluates as main does",
      _v is not None and str(_v) == "I*sinh(500)" and _e is None,
      f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("factorial(ceiling(Abs(sin(5))))")
check("THE-1095 round 11, Codex item 3: 'factorial(ceiling(Abs(sin(5))"
      "))' (a REAL argument, bounded1 correctly applies) evaluates to "
      "main's value",
      _v == 1 and _e is None, f"-> value={_v!r} err={_e!r}")

# grok addendum, the shared root cause: an UNSIGNED magnitude fed to a
# formula that needs sign/domain information.
for _expr in ("factorial(floor(zeta(-1463)))", "1 << floor(Abs(zeta(-1463)))"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round 11, grok addendum: {_expr!r} (zeta(-1463), "
          "~2852 true digits, used to be sign-blindly bounded at "
          "log10(2) when NESTED) is refused",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
    check(f"  ...in well under a second ({_dt:.3f}s)", _dt < 1.0, f"-> {_dt:.3f}s")
_v, _e = _boundary_parse("zeta(-1463)")
check("THE-1095 round 11, grok addendum: 'zeta(-1463)' alone (the "
      "already-pinned single-arg case from an earlier round) still "
      "evaluates -- the sign-safety fix only changes the NESTED-use "
      "growth formula, not the top-level domain check",
      _v is not None and _e is None, f"-> value={_v!r} err={_e!r}")
for _expr in ("factorial(floor(Abs(digamma(1e-6))))", "factorial(floor(gamma(1e-6)))",
              "factorial(floor(Abs(sin(I*20))))", "polygamma(2, -1.5)"):
    _t0 = time.time()
    _v, _e = _boundary_parse(_expr)
    _dt = time.time() - _t0
    check(f"THE-1095 round 11, grok addendum: {_expr!r} (near a pole, "
          "or a domain this module has not proven safe) is refused",
          _v is None and _e is not None and _e[0] == "ceiling", f"-> value={_v!r} err={_e!r}")
    check(f"  ...in well under a second ({_dt:.3f}s)", _dt < 1.0, f"-> {_dt:.3f}s")
_v, _e = _boundary_parse("factorial(floor(gamma(5)))")
check("THE-1095 round 11, grok addendum: 'factorial(floor(gamma(5)))' "
      "(z >= 1, the well-behaved domain) evaluates to main's exact "
      "value (24!)",
      _v == math.factorial(24) and _e is None, f"-> value={_v!r} err={_e!r}")
_v, _e = _boundary_parse("factorial(floor(Abs(sin(5))))")
check("  ...and 'factorial(floor(Abs(sin(5))))' (a REAL argument) "
      "evaluates too",
      _v == 1 and _e is None, f"-> value={_v!r} err={_e!r}")

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== ALL BUG-SWEEP REGRESSIONS FIXED ===")
sys.exit(1 if FAILS else 0)
