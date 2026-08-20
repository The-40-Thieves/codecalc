"""Exact arithmetic and programmer-mode tools, ported from the Claude `calc`
skill (~/.claude/skills/calc/calc.py) into codecalc as structured (JSON)
MCP tools.

Everything numeric is exact by default (Fraction / sympy), mirrors the
original's design: print the exact value first, the decimal approximation
second, because the two disagreeing is usually the bug.
"""

from __future__ import annotations

import ast
import math
import statistics
import struct
from datetime import UTC, datetime
from decimal import Decimal, getcontext
from fractions import Fraction

from .guarded import guarded_call
from .optional import require
from .safe_expr import reject_unsafe

getcontext().prec = 50

# ─────────────────────────── exact AST evaluator ───────────────────────────

# Whitelisted callables for the AST evaluator. Deliberately NOT eval(): parsing
# to an AST and walking a closed set of node types means no attribute access,
# imports, comprehensions, or lambdas are reachable — same safety design as the
# original calc.py.
_FUNCS = {k: getattr(math, k) for k in dir(math)
          if not k.startswith("_") and callable(getattr(math, k))}
_FUNCS.update({"abs": abs, "min": min, "max": max, "round": round,
               "int": int, "float": float})

#: Functions that are EXACT over rationals and must never see a float.
#: math.* has to go through float — that is inherent — but these do not, and
#: routing them through float silently destroyed exactness in the one module
#: whose entire purpose is exactness: abs(-1/3) returned
#: 3333333333333333/10000000000000000. Worse, the loss was data-dependent:
#: max(1/3, 1/2) came back as exactly 1/2 because 1/2 is binary-representable,
#: so the bug looked absent about half the time.
_EXACT_FUNCS = {"abs", "min", "max", "int"}
#: pi/e/tau are IRRATIONAL — no Fraction is equal to them. These are the exact
#: rational value of the binary64 approximation, which is the best a rational
#: evaluator can do, but a result computed from one is not exact and must not be
#: labelled as such. `_uses_approx_const` tracks that so eval_exact can report it.
_CONSTS = {"pi": Fraction(math.pi), "e": Fraction(math.e),
           "tau": Fraction(math.tau)}

_BIN = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: _pow_exact(a, b),
    ast.BitAnd: lambda a, b: _int_op(a, b, "&"),
    ast.BitOr: lambda a, b: _int_op(a, b, "|"),
    ast.BitXor: lambda a, b: _int_op(a, b, "^"),
    ast.LShift: lambda a, b: _int_op(a, b, "<<"),
    ast.RShift: lambda a, b: _int_op(a, b, ">>"),
}
_CMP = {
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}


def _int_op(a: Fraction, b: Fraction, op: str) -> Fraction:
    """Bitwise on exact rationals: legal only when both sides are integers."""
    for side, v in (("left", a), ("right", b)):
        if v.denominator != 1:
            raise ValueError(f"bitwise {op} needs integers; {side} operand is {v}")
    ai, bi = int(a), int(b)
    if op in ("<<", ">>") and bi < 0:
        raise ValueError(f"negative shift count {bi}")
    return Fraction({"<<": ai << bi, ">>": ai >> bi,
                     "&": ai & bi, "|": ai | bi, "^": ai ^ bi}[op])


#: Exponent magnitude and predicted-result-size guards for `**`. Both are
#: checked BEFORE the exponentiation runs, not after: `a ** b` on Fraction has
#: no magnitude guard of its own, so `2**10**6` (the inner `10**6` evaluates
#: first, then `2**1000000`) takes seconds and `2**10**7` does not return in
#: any reasonable time, both reachable from ordinary expression syntax with no
#: big-integer literals involved.
_MAX_POW_EXPONENT = 10_000
_MAX_POW_RESULT_BITS = 50_000


def _pow_exact(a: Fraction, b: Fraction) -> Fraction:
    """a ** b, bounded before the computation runs rather than after.

    The exponent's own magnitude is checked first (catches `2**10**7` for
    free, since the huge exponent itself is rejected before any power is
    taken). For an integer exponent, the result's bit length is then
    predicted from the base's bit length — O(1) work — and rejected if it
    would blow the budget, which catches a huge BASE with a modest exponent
    too.
    """
    if abs(b) > _MAX_POW_EXPONENT:
        raise ValueError(
            f"exponent {b} exceeds the limit ({_MAX_POW_EXPONENT}) for exact exponentiation"
        )
    if b.denominator == 1 and a != 0:
        base_bits = max(abs(a.numerator).bit_length(), abs(a.denominator).bit_length())
        predicted_bits = base_bits * abs(int(b))
        if predicted_bits > _MAX_POW_RESULT_BITS:
            raise ValueError(
                f"{a}**{b} would need ~{predicted_bits} bits to represent, "
                f"exceeding the limit ({_MAX_POW_RESULT_BITS}) for exact exponentiation"
            )
    return a ** b


#: Names consulted during the current eval_exact() call, reset per call.
_APPROX_CONSTS_USED: list[str] = []
#: math.* functions called during this eval. They must go through float — there
#: is no exact rational sqrt — so a NON-INTEGER result from one is an
#: approximation however exactly the Fraction prints. sqrt(144) -> 12 is still
#: the true answer; sqrt(2) -> 1414.../1000... is not.
_FLOAT_FUNCS_USED: list[str] = []


def _ev(node):
    """Walk a closed set of AST nodes; anything else is refused."""
    if isinstance(node, ast.Expression):
        return _ev(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise TypeError(f"unsupported literal {node.value!r}")
        return Fraction(str(node.value))
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            _APPROX_CONSTS_USED.append(node.id)
            return _CONSTS[node.id]
        raise ValueError(f"unknown name {node.id!r}")
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.UAdd):
            return _ev(node.operand)
        if isinstance(node.op, ast.USub):
            return -_ev(node.operand)
        if isinstance(node.op, ast.Invert):
            return Fraction(~int(_ev(node.operand)))
        raise ValueError("unsupported unary op")
    if isinstance(node, ast.BinOp):
        fn = _BIN.get(type(node.op))
        if fn is None:
            raise ValueError(f"unsupported operator {type(node.op).__name__}")
        return fn(_ev(node.left), _ev(node.right))
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise ValueError("chained comparisons not supported")
        fn = _CMP.get(type(node.ops[0]))
        if fn is None:
            raise ValueError(f"unsupported comparison {type(node.ops[0]).__name__}")
        return fn(_ev(node.left), _ev(node.comparators[0]))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ValueError("only whitelisted math functions are callable")
        args = [_ev(a) for a in node.args]
        fn = _FUNCS[node.func.id]
        if node.func.id == "round" and len(args) == 2:
            return Fraction(round(args[0] * 10 ** args[1]) / 10 ** args[1])
        if node.func.id in _EXACT_FUNCS:
            # Fraction supports these directly; keep the exact value.
            return Fraction(fn(*args))
        _FLOAT_FUNCS_USED.append(node.func.id)
        try:
            result = fn(*[float(a) for a in args])
        except (TypeError, ValueError, OverflowError):
            # functions needing exact ints (comb, factorial, ...): coerce
            coerced = []
            for a in args:
                if isinstance(a, Fraction) and a.denominator == 1:
                    coerced.append(int(a))
                else:
                    coerced.append(a)
            result = fn(*coerced)
        if isinstance(result, float):
            if not math.isfinite(result):
                raise ValueError(f"{node.func.id} produced non-finite result")
            return Fraction(str(result))
        return Fraction(result)
    raise ValueError(f"unsupported syntax: {type(node).__name__}")


#: DoS guard on input size, consistent with logic.py's _MAX_EXPR_LEN.
_MAX_EXPR_LEN = 2000


def _too_long(*parts: str) -> dict | None:
    """Reject before sympify if any part exceeds the length cap (THE-844).

    The cap used to be enforced in exactly one sympify entry point (_eval_exact).
    The others — _algebraic_equiv, _solve_expression, _limit_expression,
    _simplify_expression — reached sp.sympify with no cap, so a 120k-char
    expression blew the parser's recursion limit. The resulting failure's
    message wording is interpreter-specific ("maximum recursion depth exceeded"
    on <3.14, "stack overflow" on >=3.14), so it classified as `resource_exhausted`
    on older CPython and `internal` on 3.14 — the same input, two different codes.
    A length gate ahead of every parser rejects oversized input as "expression
    too long" on EVERY interpreter, before anything reaches sympify.
    """
    for p in parts:
        if p is not None and len(p) > _MAX_EXPR_LEN:
            return {"ok": False, "error": f"expression too long (max {_MAX_EXPR_LEN} chars)"}
    return None


def eval_exact(expr: str) -> dict:
    """Evaluate an expression with EXACT rational arithmetic.

    `0.1 + 0.2 == 0.3` is True here (False in plain Python). Supports
    arithmetic, comparisons, bitwise ops on integers, math.* functions,
    pi/e/tau. Returns the exact value plus decimal approximation.

    GUARDED, and it was the last symbolic tool that was not. #78 put the six
    SymPy tools in a killable child; this one has its own AST evaluator over
    math.*, so it was not in that set and nobody noticed. It bounded `**`
    through _MAX_POW_EXPONENT and bounded nothing about function arguments —
    `factorial(10000000)` ran until the process was killed from outside.

    Two layers, same as everywhere else here. reject_unsafe screens the string
    (it carries the heavy-argument cap); the guard bounds whatever the screen
    did not anticipate, which for this tool is every one of the ~50 callables
    dir(math) exposes.
    """
    _warm_exact()
    return guarded_call(_eval_exact, expr)


def _eval_exact(expr: str) -> dict:
    """The evaluation itself. Runs inside the guard, never called directly."""
    if len(expr) > _MAX_EXPR_LEN:
        return {"ok": False, "error": f"expression too long (max {_MAX_EXPR_LEN} chars)"}
    # The screen the other four functions in this file already call, and this
    # one never did. It carries the heavy-argument cap, which is what stops
    # factorial(<huge>) before the evaluator reaches math.factorial.
    _bad = reject_unsafe(expr)
    if _bad:
        return {"ok": False, "error": _bad}
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        return {"ok": False, "error": f"parse error: {exc.msg}"}
    _APPROX_CONSTS_USED.clear()
    _FLOAT_FUNCS_USED.clear()
    try:
        result = _ev(tree)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    approx_consts = sorted(set(_APPROX_CONSTS_USED))
    float_funcs = sorted(set(_FLOAT_FUNCS_USED))

    if isinstance(result, bool):
        return {"ok": True, "value": result, "type": "bool"}
    if isinstance(result, Fraction):
        # The pow guard above bounds the CPU cost of exponentiation, but its
        # budget is looser than Python's own int->str conversion ceiling
        # (4300 digits by default), so a result can still be too big to
        # FORMAT even though it was cheap to compute. Catch that here rather
        # than let it escape as a bare ValueError with no tool-level context.
        try:
            d = Decimal(result.numerator) / Decimal(result.denominator)
            approx = f"{d:.12f}".rstrip("0").rstrip(".")
            exact = str(result) if result.denominator != 1 else str(result.numerator)
        except ValueError as exc:
            return {"ok": False, "error": f"result too large to format: {exc}"}
        out = {"ok": True, "value": exact, "approx": approx,
               # `exact` previously meant "the exact form differs from the
               # 12-dp decimal", which is a different claim from the one the
               # name makes. It now means what it says: is this value the true
               # answer? An expression touching pi/e/tau is not, because those
               # are irrational and were substituted with the rational value of
               # their binary64 approximation.
               # A float-path function that produced a non-integer gives a
               # rational that merely PRINTS exactly. Integer results (sqrt(144))
               # are still the true answer, so they stay exact.
               "exact": not approx_consts and not (float_funcs and result.denominator != 1),
               "differs_from_decimal": exact != approx,
               "type": "rational"}
        if float_funcs and result.denominator != 1:
            out.setdefault("approximated", []).extend(float_funcs)
            out["note"] = (f"{', '.join(float_funcs)} has no exact rational form; "
                           "the value shown is the exact rational of a binary64 "
                           "approximation, so this result is NOT exact")
        if approx_consts:
            out.setdefault("approximated", []).extend(approx_consts)
            out["note"] = (f"{', '.join(approx_consts)} is irrational; the value "
                           "used is the exact rational form of its binary64 "
                           "approximation, so this result is NOT exact")
        return out
    return {"ok": True, "value": str(result), "type": type(result).__name__}


# ────────────────────────────── threshold cmp ──────────────────────────────

_CMP_SYMS = {">": _CMP[ast.Gt], ">=": _CMP[ast.GtE], "<": _CMP[ast.Lt],
             "<=": _CMP[ast.LtE], "==": _CMP[ast.Eq], "=": _CMP[ast.Eq],
             "!=": _CMP[ast.NotEq]}


def compare_threshold(a: str, op: str, b: str) -> dict:
    """Exact threshold comparison: does `a OP b` hold?

    Prints both sides as exact fractions plus the verdict and the shortfall,
    so a threshold comparison written out cannot be gotten backwards.
    `op` is one of ==, !=, >, >=, <, <= (= accepted for ==).
    """
    op = op.strip()
    if op not in _CMP_SYMS:
        return {"ok": False, "error": f"bad operator {op!r}; use ==, !=, >, >=, <, <="}
    ra = eval_exact(a)
    rb = eval_exact(b)
    if not (ra.get("ok") and rb.get("ok")):
        return {"ok": False, "error": f"left: {ra.get('error')}; right: {rb.get('error')}"}
    try:
        fa = Fraction(ra["value"] if ra["type"] == "rational" else str(ra["value"]))
        fb = Fraction(rb["value"] if rb["type"] == "rational" else str(rb["value"]))
    except (ValueError, ZeroDivisionError):
        return {"ok": False, "error": "operands must be numeric"}
    holds = _CMP_SYMS[op](fa, fb)
    diff = fa - fb
    return {"ok": True, "a": str(fa), "b": str(fb), "op": op,
            "holds": bool(holds), "difference": str(diff),
            "shortfall": None if holds else str(-diff)}


def percentage(part: str, total: str) -> dict:
    """Exact share and percentage of PART / TOTAL."""
    try:
        p, t = Fraction(part), Fraction(total)
    except (ValueError, ZeroDivisionError):
        return {"ok": False, "error": "part and total must be numbers"}
    if t == 0:
        return {"ok": False, "error": "total is zero"}
    share = p / t
    d = Decimal(share.numerator) / Decimal(share.denominator)
    return {"ok": True, "part": str(p), "total": str(t),
            "share": f"{share.numerator}/{share.denominator}",
            "percent": round(float(d) * 100, 6)}


# ─────────────────────────────── stats / pctl ──────────────────────────────

def _first_non_finite(nums: list[float]) -> int | None:
    """Index of the first nan/inf in NUMS, or None if every value is finite.

    nan/inf reach these tools as ordinary floats and used to raise out of
    them uncaught — Fraction()/int() conversions elsewhere in this module
    assume a finite value and blow up on the ones that are not
    (AttributeError from statistics.stdev on a NaN-poisoned sum, ValueError
    converting NaN to int, ...). Naming the offending index turns that into
    an answerable question about the caller's input.
    """
    for i, n in enumerate(nums):
        if not math.isfinite(float(n)):
            return i
    return None


def stats(nums: list[float]) -> dict:
    """mean, median, stdev (sample), and coefficient of variation (CV)."""
    if len(nums) < 2:
        return {"ok": False, "error": "need at least 2 numbers"}
    idx = _first_non_finite(nums)
    if idx is not None:
        return {"ok": False,
                "error": f"nums[{idx}] = {nums[idx]!r} is not finite; stats needs finite numbers"}
    vals = [float(n) for n in nums]
    mean = statistics.fmean(vals)
    stdev = statistics.stdev(vals) if len(vals) > 1 else 0.0
    # CV is undefined when the mean is 0. It used to be float("nan"), which is
    # (a) not valid JSON — json.dumps emits a bare NaN that strict parsers
    # reject — and (b) silently classified as fine, because `nan > 0.2` is
    # False, so the note read "within noise budget" for a number that does not
    # exist. None serialises as null and says what it means.
    cv = stdev / mean if mean else None
    if cv is None:
        note = "CV undefined: the mean is 0, so relative variation has no meaning"
    elif cv > 0.2:
        note = "run-to-run noise swamps the effect (CV > 0.2)"
    else:
        note = "within noise budget"
    return {"ok": True, "n": len(vals), "mean": mean,
            "median": statistics.median(vals),
            "stdev": stdev, "cv": cv, "cv_note": note}


def percentiles(nums: list[float]) -> dict:
    """p50/p90/p95/p99 by nearest-rank AND linear interpolation."""
    if not nums:
        return {"ok": False, "error": "need at least 1 number"}
    idx = _first_non_finite(nums)
    if idx is not None:
        return {"ok": False,
                "error": f"nums[{idx}] = {nums[idx]!r} is not finite; percentiles needs finite numbers"}
    vals = sorted(float(n) for n in nums)
    n = len(vals)
    out = {}
    for p in (50, 90, 95, 99):
        # nearest-rank: ceil(p/100 * n), 1-indexed
        rank = max(1, math.ceil(p / 100 * n))
        nr = vals[rank - 1]
        # linear interpolation (numpy-style)
        idx = p / 100 * (n - 1)
        lo, hi = math.floor(idx), math.ceil(idx)
        interp = vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)
        out[f"p{p}"] = {"nearest_rank": nr, "interpolated": round(interp, 6)}
    warn = "n < 100: p99 is just the maximum wearing a label" if n < 100 else None
    return {"ok": True, "n": n, "percentiles": out, "warning": warn}


def collision_prob(items: int, bits: int) -> dict:
    """Birthday-bound hash collision probability: 1 - exp(-n^2 / (2*2^b))."""
    n, b = int(items), int(bits)
    if b <= 0 or n < 0:
        return {"ok": False, "error": "items >= 0 and bits >= 1"}
    if n < 2:
        return {"ok": True, "probability": 0.0, "items": n, "bits": b}
    p = 1.0 - math.exp(-(n * n) / (2.0 * (2 ** b)))
    return {"ok": True, "items": n, "bits": b, "probability": p,
            "percent": round(p * 100, 6)}


# ────────────────────────────── bytes / dur / time ─────────────────────────

def data_sizes(n: int) -> dict:
    """Byte sizes both ways: binary (KiB/MiB) and decimal (KB/MB)."""
    n = int(n)
    return {"ok": True, "bytes": n,
            "binary": {"KiB": n / 1024, "MiB": n / 1024 ** 2,
                       "GiB": n / 1024 ** 3, "TiB": n / 1024 ** 4},
            "decimal": {"KB": n / 1000, "MB": n / 1000 ** 2,
                        "GB": n / 1000 ** 3, "TB": n / 1000 ** 4}}


#: Largest integer a float64 still represents exactly. Past this, int(s) is
#: still an EXACT integer, but its low digits are an artifact of float64's
#: binary encoding rather than measured precision — a double only carries
#: ~15-17 significant decimal digits. human_duration(1e30) used to floor that
#: into a day/hour breakdown and print ~26 digits of "days", almost all of
#: them noise past the 17th. Same threshold int_widths() already uses for its
#: "cannot round-trip through a JS number" note, reused here rather than
#: picked fresh.
_DURATION_EXACT_LIMIT = 2 ** 53


def human_duration(seconds: float) -> dict:
    """Humanised duration plus per-day and per-30d rates.

    Sub-second input carries a ms/us component instead of flooring to "0s":
    human_duration(0.5) used to report "0s", silently losing the only
    information the call had. Durations at or beyond _DURATION_EXACT_LIMIT
    switch to a capped scientific-notation report instead of a day/hour
    breakdown, since a float64 cannot back up that many decimal digits.
    """
    s = float(seconds)
    if not math.isfinite(s):
        return {"ok": False, "error": f"seconds = {s!r} is not finite"}
    if s < 0:
        return {"ok": False, "error": "negative duration"}
    per_day = round(s / 86400, 6) if s else 0.0
    per_30d = round(s / 2592000, 6) if s else 0.0
    if s >= _DURATION_EXACT_LIMIT:
        return {"ok": True, "seconds": s,
                "human": f"~{s:.6e}s (too large for an exact day/hour "
                         f"breakdown: a float64 carries ~15-17 significant "
                         f"digits and this value needs more; per_day/per_30d "
                         f"below carry the same limit)",
                "per_day": per_day, "per_30d": per_30d}
    whole = int(s)
    frac = s - whole
    units = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    parts = []
    remaining = whole
    for name, size in units:
        if remaining >= size or name == "s":
            q, remaining = divmod(remaining, size)
            parts.append(f"{q}{name}")
    if frac > 0:
        us = round(frac * 1_000_000)
        if us >= 1000:
            sub = f"{us / 1000:.3f}".rstrip("0").rstrip(".") + "ms"
        elif us > 0:
            sub = f"{us}us"
        else:
            sub = None  # sub-microsecond remainder: below reportable resolution
        if sub is not None:
            if parts == ["0s"]:
                parts = [sub]
            else:
                parts.append(sub)
    return {"ok": True, "seconds": s, "human": " ".join(parts),
            "per_day": per_day, "per_30d": per_30d}


def epoch_time(n: str) -> dict:
    """Epoch seconds/millis/micros/nanos to ISO 8601 UTC."""
    try:
        v = int(n)
    except ValueError:
        return {"ok": False, "error": "epoch must be an integer"}
    if v < 0:
        return {"ok": False, "error": "negative epoch"}
    # 10**17 was below a real nanosecond timestamp (now ~1.8e18), so the
    # advertised ns support was unreachable: every ns value was rejected as
    # "implausibly large". The true ceiling is the largest value that is
    # plausible in the COARSEST unit still accepted — nanoseconds — which is
    # ~3e20 for the year 11000. Anything beyond that is not a timestamp.
    if v > 10 ** 21:
        return {"ok": False, "error": "implausibly large epoch"}
    results = {}
    for name, unit in (("seconds", 10 ** 0), ("millis", 10 ** 3),
                       ("micros", 10 ** 6), ("nanos", 10 ** 9)):
        secs = v / unit
        # Lower bound raised off zero: interpreting a microsecond timestamp as
        # nanoseconds put it in 1970, and that 1970 reading was reported
        # alongside the real one. "Implausible readings suppressed" has to mean
        # both ends. 10**8 ~ 1973-03; anything earlier is a unit mismatch, not a
        # date someone meant.
        if 10 ** 8 <= secs <= 10 ** 10:  # plausible range ~1973..2286
            try:
                results[name] = datetime.fromtimestamp(secs, tz=UTC).isoformat()
            except (OverflowError, OSError, ValueError):
                pass
    if not results:
        return {"ok": False, "error": "no plausible unit found"}
    return {"ok": True, "input": v, "interpretations": results}


# ────────────────────────────── base / radix ───────────────────────────────

def base_repr(n: int, width: int | None = None) -> dict:
    """hex/oct/bin, two's complement at WIDTH, and signed-overflow detection."""
    n = int(n)
    w = width if width is not None else None
    out = {"ok": True, "value": n,
           "hex": f"{n:x}", "oct": f"{n:o}", "bin": f"{n:b}"}
    if w is not None:
        w = int(w)
        if w <= 0:
            return {"ok": False, "error": "width must be positive"}
        lo, hi = -(2 ** (w - 1)), 2 ** (w - 1) - 1
        uhi = 2 ** w - 1
        if not (lo <= n <= uhi):
            out["overflow"] = (f"does not fit in signed i{w} "
                               f"(range {lo}..{hi}) nor unsigned u{w}")
        elif lo <= n <= hi:
            out["signed_i" + str(w)] = n
            out["unsigned_u" + str(w)] = n if n >= 0 else n + 2 ** w
            out["two_complement_bin"] = f"{n & (2 ** w - 1):0{w}b}"
        else:  # only fits unsigned
            out["signed_i" + str(w)] = n - 2 ** w
            out["unsigned_u" + str(w)] = n
            out["two_complement_bin"] = f"{n:0{w}b}"
            out["wrapped_signed"] = n - 2 ** w
            out["signed_overflow"] = (f"does not fit in signed i{w} "
                                      f"(range {lo}..{hi}); fits u{w} only")
    return out


_RADIX_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def radix_convert(value: str, from_base: int = 10, to_base: int = 10) -> dict:
    """Convert a value between any bases 2..36, fractions included.

    Non-terminating results (e.g. 0.1 in base 2) are flagged.
    """
    fb, tb = int(from_base), int(to_base)
    if not (2 <= fb <= 36 and 2 <= tb <= 36):
        return {"ok": False, "error": "bases must be 2..36"}
    s = value.strip().lower()
    neg = s.startswith("-")
    if neg:
        s = s[1:]

    def _parse_digit(c):
        v = _RADIX_DIGITS.index(c) if c in _RADIX_DIGITS else -1
        if v < 0 or v >= fb:
            raise ValueError(f"invalid digit {c!r} for base {fb}")
        return v

    if "." in s:
        ip, fp = s.split(".", 1)
    else:
        ip, fp = s, ""
    try:
        whole = 0
        for c in ip:
            whole = whole * fb + _parse_digit(c)
        frac_num, frac_den = 0, 1
        for c in fp:
            frac_num = frac_num * fb + _parse_digit(c)
            frac_den *= fb
        num = whole * frac_den + frac_num
        den = frac_den
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if den == 0:
        return {"ok": True, "value": "0", "non_terminating": False}

    # integer part to target base
    whole = num // den
    rem = num % den
    int_digits = []
    if whole == 0:
        int_digits = ["0"]
    while whole:
        int_digits.append(_RADIX_DIGITS[whole % tb])
        whole //= tb
    int_str = "".join(reversed(int_digits))

    # fractional part; terminates iff denominator (reduced) has only prime
    # factors that also divide tb
    from math import gcd
    g = gcd(rem, den)
    rn, rd = rem // g, den // g
    # Reduce a SEPARATE copy of rd's prime factors to those in tb to decide
    # `terminates`. This must not mutate rd itself: rd (paired with rn) is the
    # denominator digits get generated from below, and rn/rd == rem/den (the
    # true value) only while rd stays the reduced denominator. Stripping rd
    # down to 1 in-place — the previous bug — left digit generation dividing
    # the reduced numerator rn by the UNREDUCED den, i.e. computing rn/den,
    # which is rem/den divided by g an extra time and is not the input at all.
    check = rd
    t = tb
    while check > 1:
        d = math.gcd(check, t)
        if d == 1:
            break
        check //= d
    terminates = check == 1

    frac_digits = []
    r = rn
    seen = {}
    while r and not terminates and len(frac_digits) < 64:
        if r in seen:
            break
        seen[r] = len(frac_digits)
        r *= tb
        frac_digits.append(_RADIX_DIGITS[r // rd])
        r %= rd
    if terminates:
        guard = 0
        while r and guard < 100:
            r *= tb
            frac_digits.append(_RADIX_DIGITS[r // rd])
            r %= rd
            guard += 1
        frac_str = "".join(frac_digits) if frac_digits else "0"
    else:
        # finite-length truncation marked with ellipsis
        frac_str = "".join(frac_digits) + "…"

    sign = "-" if neg else ""
    if frac_digits:
        return {"ok": True, "value": f"{sign}{int_str}.{frac_str}",
                "non_terminating": not terminates}
    return {"ok": True, "value": f"{sign}{int_str}", "non_terminating": False}

# ────────────────────────────── float / int / bits ─────────────────────────

def float_repr(x: float) -> dict:
    """What binary64 actually stores for x: exact value, bits, ULP, neighbours."""
    x = float(x)
    if math.isnan(x) or math.isinf(x):
        return {"ok": True, "literal": repr(x), "stored": repr(x),
                "bits": "nan" if math.isnan(x) else ("inf" if x > 0 else "-inf"),
                "exact": True, "note": "non-finite value"}
    bits = struct.unpack(">Q", struct.pack(">d", x))[0]
    sign = bits >> 63
    exp = (bits >> 52) & 0x7FF
    frac = bits & 0xFFFFFFFFFFFFF
    # exact rational value
    if exp == 0:  # subnormal
        value = Fraction(frac, 2 ** 52) * Fraction(1, 2 ** 1022)
    else:
        mant = Fraction(2 ** 52 + frac, 2 ** 52)
        e = exp - 1023
        value = mant * (Fraction(2) ** e if e >= 0 else Fraction(1, 2 ** (-e)))
    if sign:
        value = -value
    # Neighbours and ULP: math.nextafter, not the raw bit pattern. The bit
    # pattern for -0.0 is 0x8000000000000000; decrementing that (the old
    # "move away from zero" step for a negative sign) underflows to
    # 0x7fffffffffffffff, which is a NaN encoding, not a neighbour. -0.0 is
    # also the likeliest negative-zero input to a float-inspection tool.
    # math.nextafter has no such special case: it is defined in terms of the
    # real number line, so ±0.0 (which compare equal) both correctly step to
    # ±5e-324. The bit-level path stays for bits_hex/exact/stored, which are
    # legitimately about the stored encoding rather than the value's
    # neighbours.
    #
    # This deliberately CHANGES +0.0 too, whose `prev` was -0.0 and is now
    # -5e-324. The old special case reasoned that the adjacent bit pattern
    # below +0.0 is -0.0, which is true of the ENCODING and false of the
    # VALUE: -0.0 == 0.0, so it is not a neighbour on the number line. Mixing
    # those two questions in one field is what produced the -0.0 bug. The
    # encoding question is still answered, by bits_hex/sign/stored.
    next_val = math.nextafter(x, math.inf)
    prev_val = math.nextafter(x, -math.inf)
    ulp = abs(next_val - x)
    # is the literal representable? (compare exact rational of literal vs stored)
    exact = (Decimal(str(x)) == Decimal(value.numerator) / Decimal(value.denominator))
    d = Decimal(value.numerator) / Decimal(value.denominator)
    stored = f"{d:.40f}".rstrip("0").rstrip(".")
    err = value - Fraction(str(x))
    rel = abs(float(err) / float(value)) if value else 0.0
    return {"ok": True, "literal": repr(x), "stored": stored,
            "bits_hex": f"0x{bits:016x}", "sign": sign, "exponent": exp,
            "fraction_hex": f"0x{frac:013x}",
            "exact": bool(exact),
            "error": str(err), "relative_error": rel,
            "ulp": ulp, "prev": prev_val, "next": next_val,
            "note": (">2^53: consecutive integers no longer distinguishable"
                     if abs(x) > 2 ** 53 else None)}


def int_widths(n: int) -> dict:
    """Which widths hold n, and the wrapped value where they do not."""
    n = int(n)
    out = {"ok": True, "value": n}
    fits = []
    wraps = []
    for w in (8, 16, 32, 64):
        lo, hi = -(2 ** (w - 1)), 2 ** (w - 1) - 1
        uhi = 2 ** w - 1
        if lo <= n <= hi:
            fits.append(f"i{w}")
            if n >= 0:
                fits.append(f"u{w}")
        elif 0 <= n <= uhi:
            # Fits unsigned only. The `n >= 0` guard is the fix: the test used
            # to be `n <= uhi`, which is true for EVERY negative number, so
            # int_widths(-200) claimed u8 and int_widths(-2**70) claimed
            # u8/u16/u32/u64. A negative value fits no unsigned width at all.
            fits.append(f"u{w}")
            wraps.append(f"i{w}: wraps to {n - 2 ** w}")
        elif n < lo:
            wraps.append(f"i{w}/u{w}: too negative (below {lo})")
        else:
            wraps.append(f"i{w}/u{w}: too large (above {uhi})")
    out["fits"] = fits
    out["wraps"] = wraps
    out["note"] = (">2^53 cannot round-trip through a JS number or JSON float"
                   if abs(n) > 2 ** 53 else None)
    return out


def bit_analysis(n: int, align: int | None = None) -> dict:
    """popcount, trailing zeros, next pow2, alignment padding.

    n must be >= 0. popcount/is_power_of_two/next_power_of_two are magnitude
    concepts with no single meaning for a negative int absent a fixed width —
    Python's int.bit_count() silently counts the ABSOLUTE VALUE's bits, so
    bit_analysis(-1) used to report popcount=1 (identical to bit_analysis(1))
    while is_power_of_two correctly said False for the same input, and
    next_power_of_two (defined here only for n>0) vanished from the result
    with no field saying why. Rejecting negatives as a validation error
    removes all three inconsistencies at once; use bitop(n, ..., width=W) for
    two's-complement bits at an explicit width instead.
    """
    n = int(n)
    if n < 0:
        return {"ok": False,
                "error": "bit_analysis requires n >= 0 (bit semantics are "
                         "undefined for a negative int without a fixed "
                         "width); use bitop(n, ..., width=W) for "
                         "two's-complement bits at a specific width"}
    out = {"ok": True, "value": n,
           "popcount": n.bit_count(),
           "bit_length": n.bit_length(),
           "trailing_zeros": (n & -n).bit_length() - 1 if n else None,
           "is_power_of_two": n > 0 and (n & (n - 1)) == 0,
           # n=0 has no next power of two under this definition (the formula
           # below is undefined at n=0) — disclosed explicitly as None rather
           # than the key silently missing from the result.
           "next_power_of_two": (1 << (n - 1).bit_length()) if n > 0 else None}
    if align is not None:
        a = int(align)
        if a <= 0:
            return {"ok": False, "error": "alignment must be positive"}
        pad = (-n) % a
        out["aligned_to"] = a
        out["padding_needed"] = pad
        out["aligned_value"] = n + pad
    return out


# ────────────────────────────── bitop (programmer mode) ────────────────────

_BITOP_OPS = ("and", "or", "xor", "nand", "nor", "xnor", "not",
              "shl", "shr", "sar", "rol", "ror")


def bitop(a: int, op: str, b: int | None = None, width: int = 64) -> dict:
    """Programmer-mode bit operations at a fixed width.

    Every result prints unsigned, signed, hex, octal and nibble-grouped
    binary at the declared width (8/16/32/64). shr = logical (zero-fill),
    sar = arithmetic (sign-propagating). A left shift that drops bits is
    flagged OVERFLOW with the unbounded answer shown.
    """
    op = op.lower()
    w = int(width)
    if w not in (8, 16, 32, 64):
        return {"ok": False, "error": "width must be 8/16/32/64"}
    mask = (1 << w) - 1
    sign_bit = 1 << (w - 1)
    a = int(a) & mask

    def to_signed(v):
        return v - (1 << w) if v & sign_bit else v

    def show(v):
        u = v & mask
        return {"unsigned": u, "signed": to_signed(u),
                "hex": f"0x{u:0{w // 4}x}",
                "oct": f"0o{u:0{(w + 2) // 3}o}",
                "bin": f"{u:0{w}b}"}

    if op == "not":
        r = ~a & mask
        return {"ok": True, "op": op, "width": w, **show(r)}
    if b is None:
        return {"ok": False, "error": f"{op} needs two operands"}
    # For shl/shr/sar/rol/ror, `b` is a SHIFT COUNT, not a value at this
    # width — masking it with the width mask truncated the count itself:
    # bitop(1, "shl", 256, 8) masked 256 to 0 and shifted by 0, silently
    # turning "shift far past the width" into "don't shift at all". Keep the
    # count as a real (unmasked) integer and reject a negative one outright;
    # only and/or/xor/nand/nor/xnor treat `b` as a VALUE, which does get
    # masked to the declared width like `a` above.
    if op in ("shl", "shr", "sar", "rol", "ror"):
        b = int(b)
        if b < 0:
            return {"ok": False, "error": f"negative shift/rotate count {b}"}
    else:
        b = int(b) & mask
    if op == "and":
        return {"ok": True, "op": op, "width": w, **show(a & b)}
    if op == "or":
        return {"ok": True, "op": op, "width": w, **show(a | b)}
    if op == "xor":
        return {"ok": True, "op": op, "width": w, **show(a ^ b)}
    if op == "nand":
        return {"ok": True, "op": op, "width": w, **show(~(a & b))}
    if op == "nor":
        return {"ok": True, "op": op, "width": w, **show(~(a | b))}
    if op == "xnor":
        return {"ok": True, "op": op, "width": w, **show(~(a ^ b))}
    if op == "shl":  # logical left
        if b >= w:
            return {"ok": True, "op": op, "width": w, **show(0),
                    "overflow": f"shift {b} >= width {w}"}
        r = a << b
        if r > mask:
            return {"ok": True, "op": op, "width": w, **show(r),
                    "overflow": True, "unbounded": r}
        return {"ok": True, "op": op, "width": w, **show(r)}
    if op == "shr":  # logical right: zero-fill
        if b >= w:
            return {"ok": True, "op": op, "width": w, "unsigned": 0,
                    "signed": 0, "hex": "0x0", "oct": "0o0",
                    "bin": "0" * w}
        return {"ok": True, "op": op, "width": w, **show(a >> b)}
    if op == "sar":  # arithmetic right: sign-propagating
        sa = to_signed(a)
        r = sa >> b if b < w else (-1 if sa < 0 else 0)
        return {"ok": True, "op": op, "width": w, **show(r & mask)}
    if op == "rol":
        b %= w
        r = ((a << b) | (a >> (w - b))) & mask if b else a
        return {"ok": True, "op": op, "width": w, **show(r)}
    if op == "ror":
        b %= w
        r = ((a >> b) | (a << (w - b))) & mask if b else a
        return {"ok": True, "op": op, "width": w, **show(r)}
    return {"ok": False, "error": f"unknown op {op!r}; use {', '.join(_BITOP_OPS)}"}

# ────────────────────────────── symbolic (sympy) ───────────────────────────

def _sympy():
    sp = require("sympy")
    return sp


def _algebraic_equiv(a: str, b: str) -> dict:
    """Are two expressions algebraically identical? (refactor verification)

    Identity over symbolic reals says nothing about float rounding, integer
    truncation or modular overflow — usually where the real difference lives.
    """
    # SymPy evaluates what it parses (see codecalc/safe_expr.py). Screen the
    # caller's string BEFORE it reaches sympify, not after.
    if (_long := _too_long(a, b)):
        return _long
    for _name, _val in (("a", a), ("b", b)):
        _bad = reject_unsafe(_val)
        if _bad:
            return {"ok": False, "error": f"{_name}: {_bad}"}
    sp = _sympy()
    try:
        ea = sp.sympify(a)
        eb = sp.sympify(b)
    except Exception as exc:
        return {"ok": False, "error": f"parse: {exc}"}
    diff = sp.simplify(ea - eb)
    identical = diff == 0
    return {"ok": True, "a": str(ea), "b": str(eb),
            "identical": bool(identical), "difference": str(diff),
            "caveat": ("identity over symbolic reals says nothing about float "
                       "rounding, integer truncation or modular overflow") if not identical else None}


def _solve_expression(expr: str, var: str = "x") -> dict:
    """Solve for a root or crossover: `x**2 - 4 = 0`, `2*x + 1 = 7`."""
    # SymPy evaluates what it parses (see codecalc/safe_expr.py). Screen the
    # caller's string BEFORE it reaches sympify, not after.
    # Screened per SIDE, because this splits on '=' before sympify and the
    # screen refuses '=' — the parts are what reach the parser, so the parts
    # are what must be checked.
    if (_long := _too_long(expr)):
        return _long
    for _part in (expr.split("=", 1) if ("=" in expr and "==" not in expr) else [expr]):
        _bad = reject_unsafe(_part)
        if _bad:
            return {"ok": False, "error": _bad}
    sp = _sympy()
    try:
        if "=" in expr and "==" not in expr:
            lhs, rhs = expr.split("=", 1)
            eq = sp.Eq(sp.sympify(lhs), sp.sympify(rhs))
        else:
            eq = sp.Eq(sp.sympify(expr), 0)
        sym = sp.Symbol(var.strip())
        solutions = sp.solve(eq, sym)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "equation": str(eq), "variable": var,
            "solutions": [str(s) for s in solutions]}


def _limit_expression(expr: str, var: str = "x", point: str = "oo") -> dict:
    """Asymptotic behaviour: limit of EXPR as var -> point (default oo)."""
    # SymPy evaluates what it parses (see codecalc/safe_expr.py). Screen the
    # caller's string BEFORE it reaches sympify, not after.
    if (_long := _too_long(expr, point)):
        return _long
    for _name, _val in (("expr", expr), ("point", point)):
        _bad = reject_unsafe(_val)
        if _bad:
            return {"ok": False, "error": f"{_name}: {_bad}"}
    sp = _sympy()
    try:
        e = sp.sympify(expr)
        sym = sp.Symbol(var.strip())
        pt = sp.oo if point.strip() == "oo" else sp.sympify(point.strip())
        result = sp.limit(e, sym, pt)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "expression": str(e), "variable": var,
            "point": str(pt), "limit": str(result)}


def _simplify_expression(expr: str) -> dict:
    """Simplified, factored and expanded forms of an expression."""
    # SymPy evaluates what it parses (see codecalc/safe_expr.py). Screen the
    # caller's string BEFORE it reaches sympify, not after.
    if (_long := _too_long(expr)):
        return _long
    _bad = reject_unsafe(expr)
    if _bad:
        return {"ok": False, "error": _bad}
    sp = _sympy()
    try:
        e = sp.sympify(expr)
        return {"ok": True, "original": str(e),
                "simplified": str(sp.simplify(e)),
                "factored": str(sp.factor(e)),
                "expanded": str(sp.expand(e))}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── the bound (#84) ────────────────────────────────────────────────────────
# Each tool above reaches SymPy's parser, which evaluates what it parses. The
# screen in safe_expr runs first and still does the work it always did; these
# wrappers add the part a denylist cannot provide, which is a ceiling that did
# not have to anticipate the expression.
#
# The bodies are untouched — renamed to _name and called through guarded_call,
# so the result shapes a caller already depends on are unchanged except for an
# `unenforced` entry where the platform has no fork.

_EXACT_WARMED = False


def _warm_exact() -> None:
    """Warm the parent for the paths THESE tools take, once.

    Not shared with logic.py's warm set, and not merged into it: measured,
    solve/limit/simplify reach different SymPy submodules than
    evaluate_expression does, and warming the wrong ones costs the same startup
    while leaving the fork to re-import the right ones on every call. #78
    measured that mistake at +196ms on a single expression.
    """
    global _EXACT_WARMED
    if _EXACT_WARMED:
        return
    _EXACT_WARMED = True
    try:
        _simplify_expression("x**2 + 2*x + 1")
        _solve_expression("x**2 - 4 = 0", "x")
        _limit_expression("1/x", "x", "oo")
        _algebraic_equiv("(x+1)**2", "x**2 + 2*x + 1")
    except Exception:
        # Warming is an optimisation; a failure here must not take down the
        # call the caller actually made.
        pass


def algebraic_equiv(a: str, b: str) -> dict:
    """Are two expressions algebraically identical? (refactor verification)"""
    _warm_exact()
    return guarded_call(_algebraic_equiv, a, b)


def solve_expression(expr: str, var: str = "x") -> dict:
    """Solve for a root or crossover: `x**2 - 4 = 0`, `2*x + 1 = 7`."""
    _warm_exact()
    return guarded_call(_solve_expression, expr, var)


def limit_expression(expr: str, var: str = "x", point: str = "oo") -> dict:
    """Asymptotic behaviour: limit of EXPR as var -> point (default oo)."""
    _warm_exact()
    return guarded_call(_limit_expression, expr, var, point)


def simplify_expression(expr: str) -> dict:
    """Simplified, factored and expanded forms of an expression."""
    _warm_exact()
    return guarded_call(_simplify_expression, expr)
