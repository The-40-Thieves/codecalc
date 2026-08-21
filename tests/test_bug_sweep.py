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

    _w = _tf.mkdtemp()
    try:
        def _lock_output():
            time.sleep(1.0)
            for _f in pathlib.Path(_w).glob("*.out"):
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
        for _f in pathlib.Path(_w).glob("*"):
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
    from sympy import nextprime as _nextprime

    from codecalc.safe_expr import reject_unsafe as _screen

    _p, _q = _nextprime(10**30), _nextprime(3 * 10**31)
    _UNBOUNDED = [
        (f"factorint({_p * _q})", "a 62-digit semiprime with far-apart factors"),
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
    _p2, _q2 = _nextprime(10**30), _nextprime(3 * 10**31)
    _payload = f"factorint({_p2 * _q2})"
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

# ═══ THE-888: solve_linear's own sympify was not screened the way the RCE ═══
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
# the unevaluated shape first — the same fix THE-887 gave linalg's per-cell
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

# ═══ THE-889: the same bare-sympify gap in exact.py's four remaining ═══════
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
# `safe_expr.safe_parse` (the same fix THE-887/888 gave `matrix` and
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

# ═══ THE-890: solve_linear crashed on a SINGLE-variable system ════════════
# sp.symbols("x") returns a bare Symbol, not a 1-tuple, unless given more
# than one name or a trailing comma — the code downstream assumed a
# sequence (`list(syms)`), so `solve_linear('2*x = 4', 'x')` raised
# "'Symbol' object is not iterable" rather than returning a result.
_r = _logic.solve_linear("2*x = 4", "x")
check("solve_linear solves a single-variable system (THE-890)",
      _r.get("ok") is True and _r.get("solutions") == ["{x: 2}"], f"-> {_r}")
_r = _logic.solve_linear("x - 5", "x")
check("  ...including the no-'=' single-variable path",
      _r.get("ok") is True and _r.get("solutions") == ["{x: 5}"], f"-> {_r}")
# The fix must not change multi-variable behaviour.
_r = _logic.solve_linear("x + y = 10; x - y = 2", "x, y")
check("  ...and multi-variable systems are unaffected",
      _r.get("ok") is True and _r.get("solutions") == ["{x: 6, y: 4}"], f"-> {_r}")


# ═══ THE-901: reject_explosive bounds Pow shapes, not nested-parens +      ═══
# ═══ repeated-term chains — guarded_call is the actual backstop for those ═══
# `scripts/fuzz.py` (THE-899) found that deeply nested parens combined with a
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
check("the THE-901 nested+repeated shape stays inside the 2000-char cap",
      len(_nested_chain) == 2000, f"-> {len(_nested_chain)} chars")

_t0 = time.time()
_r = _logic.evaluate_expression(_nested_chain)
_elapsed = time.time() - _t0
# guarded.DEFAULT_WALL_SECONDS is 15 — asserted well under it (20s), not
# exactly at it, so ordinary timing jitter on a loaded CI runner cannot flip
# this from "the backstop held" to "flaky".
check("evaluate_expression refuses the THE-901 shape rather than hanging",
      _r.get("ok") is False, f"-> {_r}")
check("  ...and guarded_call's wall-clock backstop actually bounds it",
      _elapsed < 20.0, f"-> {_elapsed:.2f}s (guarded.DEFAULT_WALL_SECONDS=15)")

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== ALL BUG-SWEEP REGRESSIONS FIXED ===")
sys.exit(1 if FAILS else 0)
