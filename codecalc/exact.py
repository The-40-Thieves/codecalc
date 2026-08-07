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
_CONSTS = {"pi": Fraction(math.pi), "e": Fraction(math.e),
           "tau": Fraction(math.tau)}

_BIN = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
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


def eval_exact(expr: str) -> dict:
    """Evaluate an expression with EXACT rational arithmetic.

    `0.1 + 0.2 == 0.3` is True here (False in plain Python). Supports
    arithmetic, comparisons, bitwise ops on integers, math.* functions,
    pi/e/tau. Returns the exact value plus decimal approximation.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        return {"ok": False, "error": f"parse error: {exc.msg}"}
    try:
        result = _ev(tree)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    if isinstance(result, bool):
        return {"ok": True, "value": result, "type": "bool"}
    if isinstance(result, Fraction):
        d = Decimal(result.numerator) / Decimal(result.denominator)
        approx = f"{d:.12f}".rstrip("0").rstrip(".")
        exact = str(result) if result.denominator != 1 else str(result.numerator)
        return {"ok": True, "value": exact, "approx": approx,
                "exact": exact != approx, "type": "rational"}
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

def stats(nums: list[float]) -> dict:
    """mean, median, stdev (sample), and coefficient of variation (CV)."""
    if len(nums) < 2:
        return {"ok": False, "error": "need at least 2 numbers"}
    vals = [float(n) for n in nums]
    mean = statistics.fmean(vals)
    stdev = statistics.stdev(vals) if len(vals) > 1 else 0.0
    cv = stdev / mean if mean else float("nan")
    return {"ok": True, "n": len(vals), "mean": mean,
            "median": statistics.median(vals),
            "stdev": stdev, "cv": cv,
            "cv_note": ("run-to-run noise swamps the effect (CV > 0.2)"
                        if cv > 0.2 else "within noise budget")}


def percentiles(nums: list[float]) -> dict:
    """p50/p90/p95/p99 by nearest-rank AND linear interpolation."""
    if not nums:
        return {"ok": False, "error": "need at least 1 number"}
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


def human_duration(seconds: float) -> dict:
    """Humanised duration plus per-day and per-30d rates."""
    s = float(seconds)
    if s < 0:
        return {"ok": False, "error": "negative duration"}
    units = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    parts = []
    for name, size in units:
        if s >= size or name == "s":
            q = int(s // size)
            parts.append(f"{q}{name}")
            s -= q * size
    return {"ok": True, "seconds": float(seconds),
            "human": " ".join(parts),
            "per_day": round(float(seconds) / 86400, 6) if seconds else 0.0,
            "per_30d": round(float(seconds) / 2592000, 6) if seconds else 0.0}


def epoch_time(n: str) -> dict:
    """Epoch seconds/millis/micros/nanos to ISO 8601 UTC."""
    try:
        v = int(n)
    except ValueError:
        return {"ok": False, "error": "epoch must be an integer"}
    if v < 0:
        return {"ok": False, "error": "negative epoch"}
    if v > 10 ** 17:  # too big for any sane unit
        return {"ok": False, "error": "implausibly large epoch"}
    results = {}
    for name, unit in (("seconds", 10 ** 0), ("millis", 10 ** 3),
                       ("micros", 10 ** 6), ("nanos", 10 ** 9)):
        secs = v / unit
        if 0 <= secs <= 10 ** 10:  # plausible year range ~ 1970..2286
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
    # reduce rd's prime factors to those in tb
    t = tb
    while rd > 1:
        d = math.gcd(rd, t)
        if d == 1:
            break
        rd //= d
    terminates = rd == 1

    frac_digits = []
    r = rn
    seen = {}
    while r and not terminates and len(frac_digits) < 64:
        if r in seen:
            break
        seen[r] = len(frac_digits)
        r *= tb
        frac_digits.append(_RADIX_DIGITS[r // den])
        r %= den
    if terminates:
        guard = 0
        while r and guard < 100:
            r *= tb
            frac_digits.append(_RADIX_DIGITS[r // den])
            r %= den
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
    # ULP: distance to next representable
    next_bits = bits + 1 if not (sign and bits) else bits - 1
    next_val = struct.unpack(">d", struct.pack(">Q", next_bits))[0]
    ulp = abs(next_val - x)
    # neighbours
    prev_bits = bits - 1 if not sign else bits + 1
    prev_val = struct.unpack(">d", struct.pack(">Q", prev_bits))[0]
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
        elif n <= uhi:
            fits.append(f"u{w}")
            wraps.append(f"i{w}: wraps to {n - 2 ** w}")
        else:
            wraps.append(f"i{w}/u{w}: out of range")
    out["fits"] = fits
    out["wraps"] = wraps
    out["note"] = (">2^53 cannot round-trip through a JS number or JSON float"
                   if abs(n) > 2 ** 53 else None)
    return out


def bit_analysis(n: int, align: int | None = None) -> dict:
    """popcount, trailing zeros, next pow2, alignment padding."""
    n = int(n)
    out = {"ok": True, "value": n,
           "popcount": n.bit_count(),
           "bit_length": n.bit_length() if n >= 0 else None,
           "trailing_zeros": (n & -n).bit_length() - 1 if n else None,
           "is_power_of_two": n > 0 and (n & (n - 1)) == 0}
    if n > 0:
        out["next_power_of_two"] = 1 << (n - 1).bit_length()
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
    import sympy as sp
    return sp


def algebraic_equiv(a: str, b: str) -> dict:
    """Are two expressions algebraically identical? (refactor verification)

    Identity over symbolic reals says nothing about float rounding, integer
    truncation or modular overflow — usually where the real difference lives.
    """
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


def solve_expression(expr: str, var: str = "x") -> dict:
    """Solve for a root or crossover: `x**2 - 4 = 0`, `2*x + 1 = 7`."""
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


def limit_expression(expr: str, var: str = "x", point: str = "oo") -> dict:
    """Asymptotic behaviour: limit of EXPR as var -> point (default oo)."""
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


def simplify_expression(expr: str) -> dict:
    """Simplified, factored and expanded forms of an expression."""
    sp = _sympy()
    try:
        e = sp.sympify(expr)
        return {"ok": True, "original": str(e),
                "simplified": str(sp.simplify(e)),
                "factored": str(sp.factor(e)),
                "expanded": str(sp.expand(e))}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
