"""Parity tests: every ported calc feature vs known-good values."""
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from codecalc import exact, logic

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# exact arithmetic
r = exact.eval_exact("0.1+0.2 == 0.3")
check("exact 0.1+0.2==0.3", r["ok"] and r["value"] is True)
r = exact.eval_exact("2**64 - 1")
check("exact 2**64-1", r["value"] == "18446744073709551615")
r = exact.eval_exact("comb(52,5)")
check("comb(52,5)", r["ok"] and r["value"] == "2598960")
r = exact.eval_exact("0xff & 0x0f")
check("bitwise inline", r["value"] == "15")
r = exact.eval_exact("1 << 20")
check("shift inline", r["value"] == "1048576")

# threshold
r = exact.compare_threshold("1/25", ">", "0.05")
check("cmp 1/25 > 0.05 false", r["ok"] and r["holds"] is False and r["shortfall"] == "1/100")
r = exact.compare_threshold("6", ">=", "6")
check("cmp 6>=6", r["holds"] is True)

# percentage
r = exact.percentage("3", "8")
check("pct 3/8", r["ok"] and abs(r["percent"] - 37.5) < 1e-6)
# GH #213(c): `percent` is a rounded float sitting right next to the
# EXACT `share` fraction — nothing used to say which of the two was which.
check("pct discloses percent is rounded, and to how many digits",
      r.get("rounding") == {"percent": 6}, f"-> {r}")

# percent_change (GH #329): before/after change, distinct from percentage's
# PART/TOTAL share. Exact rational relative to abs(from_value) — see
# exact.percent_change's own module comment for the sign-convention
# reasoning (the conventional finance definition, not plain (b-a)/a).
r = exact.percent_change("100", "150")
check("percent_change 100->150",
      r["ok"] and r["percent_exact"] == "50" and r["absolute_change"] == "50"
      and r["direction"] == "increase" and r["multiplier"] == "3/2", f"-> {r}")
r = exact.percent_change("150", "100")
check("percent_change 150->100 (exact fraction, not a rounded float)",
      r["ok"] and r["percent_exact"] == "-100/3" and r["direction"] == "decrease"
      and abs(r["percent_decimal"] - (-33.333333)) < 1e-5, f"-> {r}")
r = exact.percent_change("5", "5")
check("percent_change 5->5 unchanged",
      r["ok"] and r["direction"] == "unchanged" and r["percent_exact"] == "0"
      and r["multiplier"] == "1", f"-> {r}")
r = exact.percent_change("0", "5")
check("percent_change refuses from_value=0, coded, with a remedy",
      r["ok"] is False and r.get("code") == "validation"
      and "report the absolute change" in r.get("remedy", ""), f"-> {r}")
# Negative base: relative to abs(from_value), so `direction` follows the
# ALGEBRAIC sign of the change, not the change in |value| — GH #329 asked
# this be decided explicitly and documented; `note` is where it is.
r = exact.percent_change("-100", "-50")
check("percent_change -100->-50: direction 'increase' (algebraic, -50 > "
      "-100), documented sign convention in `note`",
      r["ok"] and r["percent_exact"] == "50" and r["direction"] == "increase"
      and "note" in r, f"-> {r}")
r = exact.percent_change("-100", "50")
check("percent_change -100->50",
      r["ok"] and r["percent_exact"] == "150" and r["direction"] == "increase"
      and r["multiplier"] == "-1/2" and "note" in r, f"-> {r}")
r = exact.percent_change("3/4", "1")
check("percent_change accepts expression inputs ('3/4' -> '1')",
      r["ok"] and r["percent_exact"] == "100/3", f"-> {r}")
r = exact.percent_change("abc", "1")
check("percent_change refuses a non-numeric from_value",
      r["ok"] is False and "error" in r, f"-> {r}")

# PR #337 cross-vendor review (Codex): `Fraction`'s own scientific-notation
# parsing builds the exact integer directly, never through float, so a
# short string like '1e5000' does not overflow to inf the way a plain
# float(s) call would — it demands a >4300-digit integer and raised an
# UNCAUGHT ValueError, and '1e400' (under that ceiling) "succeeded" with a
# percent_decimal silently overflowing to a non-finite `Infinity` (invalid
# JSON). Both are now refused/degraded gracefully — see exact.py's
# _oversized_numeric_literal and the math.isfinite guard on percent_decimal.
r = exact.percent_change("1", "1e400")
check("percent_change('1', '1e400'): finite percent_decimal (null, not "
      "inf/Infinity), exact value still reported",
      r["ok"] and r["percent_decimal"] is None and r["percent_exact"].startswith("999")
      and "note" in r, f"-> percent_decimal={r.get('percent_decimal')!r} "
      f"note={r.get('note')!r}")
r = exact.percent_change("1", "1e5000")
check("percent_change('1', '1e5000'): refused BEFORE Fraction() construction, "
      "not an uncaught ValueError",
      r["ok"] is False and r.get("code") == "resource_exhausted"
      and "digits" in r.get("error", ""), f"-> {r}")
r = exact.percent_change("1", "1" * 5000)
check("percent_change: a 5000-char plain-digit literal (over "
      "MAX_NUMERIC_DIGITS on digit count alone) is refused, not a crash",
      r["ok"] is False and r.get("code") == "resource_exhausted", f"-> {r}")
r = exact.percent_change("1", "1e3")
check("percent_change('1', '1e3'): an ordinary scientific-notation input "
      "still works, finite percent_decimal",
      r["ok"] and r["percent_decimal"] == 99900.0 and "note" not in r, f"-> {r}")
# Two inputs each individually under the per-input digit cap can still
# combine into a computed result over Python's int->str ceiling (opposite-
# sign exponents near the boundary) — the try/except backstop around the
# actual computation, not just the upfront per-input check, is what catches
# this.
r = exact.percent_change("1e-3999", "1e3999")
check("percent_change: two individually-in-bounds inputs whose COMBINED "
      "result overflows str() are still refused, not a crash",
      r["ok"] is False and r.get("code") == "resource_exhausted", f"-> {r}")

# PR #337 round 2 (Codex): the exponent regex required `\d+` with no `_`,
# so '1e4_001' (Fraction's own grammar accepts '_' as a digit-group
# separator, PEP 515) silently read as "no exponent found" and scored 5
# raw digit characters instead of the true ~4002 the value implies — the
# cap bypassed by exactly the syntax it claimed to bound. '_' is now
# refused outright (see exact.py's _oversized_numeric_literal for why not
# just taught to parse it). Every spelling the review listed:
r = exact.percent_change("1", "1e4_001")
check("percent_change: '1e4_001' no longer bypasses the digit cap via "
      "underscore grouping — refused, not a 4002-digit result",
      r["ok"] is False and r.get("code") == "resource_exhausted"
      and "_" in r.get("error", ""), f"-> {r}")
r = exact.percent_change("1", "1e100_000")
check("percent_change: '1e100_000' is refused BEFORE Fraction() runs "
      "(not merely caught after constructing a 100,001-digit integer)",
      r["ok"] is False and r.get("code") == "resource_exhausted"
      and "_" in r.get("error", ""), f"-> {r}")
r = exact.percent_change("1", "1_000")
check("percent_change: '1_000' (an ordinary small number, just spelled "
      "with an underscore) is refused cleanly, not silently mis-scored",
      r["ok"] is False and r.get("code") == "resource_exhausted", f"-> {r}")
r = exact.percent_change("1", "+1e3")
check("percent_change: a leading '+' sign still works",
      r["ok"] and r["to_value"] == "1000", f"-> {r}")
r = exact.percent_change("1", "1E400")
check("percent_change: uppercase 'E' scientific notation is recognised "
      "the same as lowercase 'e' (null percent_decimal, not Infinity)",
      r["ok"] and r["percent_decimal"] is None, f"-> {r}")
r = exact.percent_change("1", " 1e3 ")
check("percent_change: surrounding whitespace (Fraction's own grammar "
      "accepts it) still works",
      r["ok"] and r["to_value"] == "1000", f"-> {r}")
r = exact.percent_change("1", "0.000001e4000")
check("percent_change: a decimal mantissa with leading zeros then a huge "
      "exponent is refused (conservative digit-count estimate), not a crash",
      r["ok"] is False and r.get("code") == "resource_exhausted", f"-> {r}")
# Exactly at the boundary: 3999 mantissa digits + exponent 1 = 4000, not
# over MAX_NUMERIC_DIGITS — allowed, and the 4001-character string is well
# under _MAX_NUMERIC_LITERAL_LEN (unlike the old, unrelated _MAX_EXPR_LEN
# reuse a round-1 version of this fix had, which would have refused this
# for string length alone despite being digit-cap-safe).
r = exact.percent_change("1", "1" * 3999 + "e1")
check("percent_change: a 3999-digit mantissa with 'e1' sits exactly at "
      "the MAX_NUMERIC_DIGITS boundary (4000) and is allowed",
      r["ok"] and len(r["percent_exact"]) in (4001, 4002), f"-> ok={r.get('ok')} "
      f"len={len(r.get('percent_exact', ''))}")
r = exact.percent_change("1", "1/1e400")
check("percent_change: '1/1e400' (Fraction's own grammar rejects mixing "
      "'/' with exponent notation) fails cleanly, not a crash",
      r["ok"] is False and "error" in r, f"-> {r}")
for _bad in ("inf", "nan"):
    r = exact.percent_change("1", _bad)
    check(f"percent_change: {_bad!r} is refused cleanly (Fraction itself "
          "rejects it), not treated as a real value",
          r["ok"] is False and "error" in r, f"-> {r}")

# evaluate_expression's calculus forms (GH #329): documented in server.py's
# docstring but previously undiscoverable from the schema — diff/integrate/
# series worked all along; these prove the doc's own examples still run.
r = logic.evaluate_expression("diff(sin(x)*x, x)")
check("evaluate_expression diff(sin(x)*x, x)",
      r["ok"] and r["expression"] == "x*cos(x) + sin(x)", f"-> {r}")
r = logic.evaluate_expression("integrate(x**2, (x, 0, 1))")
check("evaluate_expression integrate(x**2, (x, 0, 1)) definite integral",
      r["ok"] and r["expression"] == "1/3", f"-> {r}")
r = logic.evaluate_expression("series(sin(x), x, 0, 6)")
check("evaluate_expression series(sin(x), x, 0, 6)",
      r["ok"] and r["expression"] == "x - x**3/6 + x**5/120 + O(x**6)", f"-> {r}")

# symbolic(op="solve_linear")'s documented non-linear example (GH #329) —
# the name says 'linear', the docstring now says non-linear polynomial
# systems work too; prove it with the ticket's own example.
r = logic.solve_linear("x**2 + y**2 = 5; x - y = -1", ["x", "y"])
check("solve_linear accepts a non-linear polynomial system, as documented",
      r["ok"] and r["count"] == 2
      and sorted(r["solutions"]) == sorted(["{x: -2, y: -1}", "{x: 1, y: 2}"]),
      f"-> {r}")

# stats
r = exact.stats([1, 2, 3, 4, 5])
check("stats mean", r["mean"] == 3.0)
# sample stdev = sqrt(2.5) = 1.5811; CV = 1.5811/3 = 0.527
check("stats cv", abs(r["cv"] - 0.527046) < 1e-4)

# percentiles
r = exact.percentiles(list(range(1, 101)))
check("pctl p50 nearest", r["percentiles"]["p50"]["nearest_rank"] == 50)
check("pctl warning n=100 none", r["warning"] is None)
# GH #213(c): `interpolated` is rounded; `nearest_rank` is an exact
# value straight out of the input, so only the former is named.
check("pctl discloses interpolated is rounded, nearest_rank is not",
      r["percentiles"]["p50"].get("rounding") == {"interpolated": 6},
      f"-> {r['percentiles']['p50']}")
r = exact.percentiles([1, 2, 3])
check("pctl warns n<100", r["warning"] is not None)

# collision
r = exact.collision_prob(100000, 32)
check("collide 1e5/32 ~0.69", abs(r["probability"] - 0.6878) < 0.01)
# GH #213(c): `percent` is rounded; `probability` (the full float) is
# not, so only `percent` is named.
check("collision_prob discloses percent is rounded, probability is not",
      r.get("rounding") == {"percent": 6}, f"-> {r}")
r = exact.collision_prob(1000000, 64)
check("collide 1e6/64 ~2.7e-8", abs(r["probability"] - 2.71e-8) < 1e-9)

# bytes / duration / epoch
r = exact.data_sizes(291 * 1024 * 1024)
check("bytes MiB vs MB", abs(r["binary"]["MiB"] - 291) < 1e-6 and abs(r["decimal"]["MB"] - 305.135) < 0.01)
# GH #213(b): a negative byte count used to divide through cleanly
# and report negative KiB/MB instead of being rejected, same bug shape
# human_duration already guards against for a negative duration.
r = exact.data_sizes(-1024)
check("bytes rejects a negative n", r["ok"] is False and "negative" in r.get("error", ""), f"-> {r}")
r = exact.human_duration(90061)
check("dur humanized", "1d" in r["human"] and "1h" in r["human"] and "1m" in r["human"] and "1s" in r["human"])
# GH #213(c): per_day/per_30d are rounded; `seconds` (echoed input)
# is not, so only the two rates are named.
check("dur discloses per_day/per_30d are rounded, seconds is not",
      r.get("rounding") == {"per_day": 6, "per_30d": 6}, f"-> {r}")
# #198: human_duration(0.5) used to report "0s", silently dropping
# the only information a sub-second call carried. Sub-second input now
# carries a ms/us component instead of flooring away.
r = exact.human_duration(0.5)
check("dur 0.5s -> 500ms, not the old silent '0s'", r["ok"] and r["human"] == "500ms", f"-> {r}")
r = exact.human_duration(0.00025)
check("dur 250us sub-millisecond duration", r["ok"] and r["human"] == "250us", f"-> {r}")
r = exact.human_duration(1.5)
check("dur 1.5s keeps the whole-second part alongside the fraction",
      r["ok"] and r["human"] == "1s 500ms", f"-> {r}")
# #198: human_duration(1e30) used to floor a float64 into
# int(s)//86400 and print ~26 digits of "days" — far past the ~15-17
# significant decimal digits a float64 actually carries. Above the exactness
# threshold it must report scientific notation instead of garbage digits.
r = exact.human_duration(1e30)
check("dur 1e30s does not print a day count with 20+ digits",
      r["ok"] and not re.search(r"\d{18,}d", r["human"]), f"-> {r['human']!r}")
check("dur 1e30s uses scientific notation instead",
      r["ok"] and "e+30" in r["human"], f"-> {r['human']!r}")
r = exact.epoch_time("1700000000")
check("epoch seconds->ISO", "2023-11-14T22:13:20" in r["interpretations"]["seconds"])

# base / radix
r = exact.base_repr(255)
check("base hex", r["hex"] == "ff" and r["bin"] == "11111111")
r = exact.base_repr(3000000000, 32)
check("base overflow flagged", "overflow" in r or "signed_overflow" in r)
r = exact.radix_convert("0.1", 10, 2)
check("radix 0.1 base2 non-terminating", r["non_terminating"] is True and "…" in r["value"])
r = exact.radix_convert("ff", 16, 10)
check("radix ff->255", r["value"] == "255")
r = exact.radix_convert("zz", 36, 7)
check("radix zz 36->7", r["ok"])

# float / int / bits
r = exact.float_repr(0.25)
check("float 0.25 exact", r["exact"] is True)
r = exact.float_repr(0.1)
check("float 0.1 not exact", r["exact"] is False and "0.1000000000000000055511151231257827" in r["stored"])
check("float bits", r["bits_hex"] == "0x3fb999999999999a")
r = exact.int_widths(3000000000)
check("int_widths i32 wrap", any("i32: wraps to -1294967296" in w for w in r["wraps"]))
r = exact.int_widths(2**60)
check("int_widths 2^60 note", r["note"] is not None)
r = exact.bit_analysis(12)
check("bits popcount(12)=2", r["popcount"] == 2)
check("bits next pow2(12)=16", r["next_power_of_two"] == 16)
check("bits trailing zeros(12)=2", r["trailing_zeros"] == 2)
r = exact.bit_analysis(7, align=8)
check("bits align pad 7->8", r["padding_needed"] == 1 and r["aligned_value"] == 8)
# #197: bit_analysis(-1) used to report popcount=1 (int.bit_count()
# silently counts the ABSOLUTE VALUE's bits) while is_power_of_two correctly
# said False for the same input, and next_power_of_two vanished from the
# result with no field explaining why. Negative n is now a validation error.
r = exact.bit_analysis(-1)
check("bit_analysis(-1) is a validation error, not a self-contradicting result",
      r.get("ok") is False and "error" in r, f"-> {r}")
check("  ...it's a validation-style error (not an internal one)",
      "n >= 0" in r.get("error", ""), f"-> {r.get('error')!r}")
r = exact.bit_analysis(-(2 ** 70))
check("bit_analysis rejects any negative n, not just -1", r.get("ok") is False, f"-> {r}")
# next_power_of_two must never be silently dropped: n=0 discloses None
# instead of the key being absent.
r = exact.bit_analysis(0)
check("bit_analysis(0): next_power_of_two is disclosed as None, not omitted",
      r.get("ok") is True and "next_power_of_two" in r and r["next_power_of_two"] is None,
      f"-> {r}")

# bitop programmer mode
r = exact.bitop(0x80, "shr", 1, 8)
check("bitop shr logical", r["unsigned"] == 0x40 and r["signed"] == 64)
r = exact.bitop(0x80, "sar", 1, 8)
check("bitop sar arithmetic", r["unsigned"] == 0xC0 and r["signed"] == -64)
r = exact.bitop(0xFF, "and", 0x0F, 8)
check("bitop and", r["unsigned"] == 15)
r = exact.bitop(0x8000000000000000, "shl", 1, 64)
check("bitop shl overflow", r.get("overflow") is True and r.get("unbounded") == 2 ** 64)
r = exact.bitop(0b1010, "xor", 0b0110, 8)
check("bitop xor", r["unsigned"] == 12)
r = exact.bitop(0, "not", None, 8)
check("bitop not 0x00 -> 0xFF", r["unsigned"] == 255)
r = exact.bitop(0x0F, "rol", 4, 8)
check("bitop rol 0x0F<<4", r["unsigned"] == 0xF0)
r = exact.bitop(0xF0, "ror", 4, 8)
check("bitop ror 0xF0>>4", r["unsigned"] == 0x0F)

# symbolic
r = exact.algebraic_equiv("(a*b)/c", "a*(b/c)")
check("eq identity", r["identical"] is True)
r = exact.algebraic_equiv("(a+b)**2", "a**2 + 2*a*b + b**2")
check("eq expand identity", r["identical"] is True)
r = exact.solve_expression("x**2 - 4 = 0")
check("solve x^2=4", sorted(r["solutions"]) == sorted(["-2", "2"]))
r = exact.limit_expression("n*log(n)/n**2", "n")
check("limit nlogn/n^2 -> 0", r["limit"] == "0")
r = exact.simplify_expression("(x**2 - 1)/(x - 1)")
check("simplify", r["simplified"] == "x + 1")

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else "\n=== ALL 19 PORTED FEATURES PASS ===")
sys.exit(1 if FAILS else 0)
