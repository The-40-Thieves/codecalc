"""Parity tests: every ported calc feature vs known-good values."""
import sys
sys.path.insert(0, "/home/ubuntu/codecalc")
from codecalc import exact

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

# stats
r = exact.stats([1, 2, 3, 4, 5])
check("stats mean", r["mean"] == 3.0)
# sample stdev = sqrt(2.5) = 1.5811; CV = 1.5811/3 = 0.527
check("stats cv", abs(r["cv"] - 0.527046) < 1e-4)

# percentiles
r = exact.percentiles(list(range(1, 101)))
check("pctl p50 nearest", r["percentiles"]["p50"]["nearest_rank"] == 50)
check("pctl warning n=100 none", r["warning"] is None)
r = exact.percentiles([1, 2, 3])
check("pctl warns n<100", r["warning"] is not None)

# collision
r = exact.collision_prob(100000, 32)
check("collide 1e5/32 ~0.69", abs(r["probability"] - 0.6878) < 0.01)
r = exact.collision_prob(1000000, 64)
check("collide 1e6/64 ~2.7e-8", abs(r["probability"] - 2.71e-8) < 1e-9)

# bytes / duration / epoch
r = exact.data_sizes(291 * 1024 * 1024)
check("bytes MiB vs MB", abs(r["binary"]["MiB"] - 291) < 1e-6 and abs(r["decimal"]["MB"] - 305.135) < 0.01)
r = exact.human_duration(90061)
check("dur humanized", "1d" in r["human"] and "1h" in r["human"] and "1m" in r["human"] and "1s" in r["human"])
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
