"""matrix (GH #223): det/inverse/eigenvalues/transpose/rank/trace,
validation, and per-entry RCE screening — values checkable by hand.

`evaluate_expression` refuses `Matrix([[1,2],[3,4]])` because `[`/`]` are
denied at the token level to block a subscript escape
(`().__class__.__bases__[0]`); a list literal is collateral from that
correctly-aimed screen. `matrix` is the structured replacement: `rows` never
passes through sympify as one caller string, so it never touches that screen
at all. What DOES need proving here is the thing that screen protects — a
malicious matrix ENTRY must be refused exactly as evaluate_expression refuses
it, per entry, before it reaches sympy.
"""
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from codecalc import errors, linalg, logic, safe_expr

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── the four operations, pinned to hand-checkable answers ──────────────────

r = linalg.matrix([[1, 2], [3, 4]], "det")
check("det [[1,2],[3,4]] == -2", r["ok"] and r["value"] == "-2", f"-> {r}")

r = linalg.matrix([[4, 7], [2, 6]], "inverse")
# Known inverse of [[4,7],[2,6]] (det=10): (1/10)*[[6,-7],[-2,4]]
check("inverse [[4,7],[2,6]]",
      r["ok"] and r["matrix"] == [["3/5", "-7/10"], ["-1/5", "2/5"]], f"-> {r}")

r = linalg.matrix([[2, 0], [0, 3]], "eigenvalues")
check("eigenvalues of a diagonal matrix are its diagonal, each multiplicity 1",
      r["ok"] and r["eigenvalues"] == {"2": 1, "3": 1}, f"-> {r}")

r = linalg.matrix([[1, 2, 3], [4, 5, 6]], "transpose")
check("transpose of a 2x3 is a 3x2",
      r["ok"] and r["matrix"] == [["1", "4"], ["2", "5"], ["3", "6"]]
      and r["shape"] == [3, 2], f"-> {r}")

r = linalg.matrix([[1, 2], [2, 4]], "rank")
check("rank of a rank-deficient 2x2 is 1", r["ok"] and r["value"] == "1", f"-> {r}")

r = linalg.matrix([[1, 2], [3, 4]], "trace")
check("trace [[1,2],[3,4]] == 1+4 == 5", r["ok"] and r["value"] == "5", f"-> {r}")

# ── string entries reach sympy too, screened the same way ──────────────────

r = linalg.matrix([["1/2", "sqrt(4)"], ["3", "4"]], "det")
# det([[1/2, 2], [3, 4]]) = 1/2*4 - 2*3 = 2 - 6 = -4
check("string entries evaluate through sympy: '1/2'*'sqrt(4)' style det",
      r["ok"] and r["value"] == "-4", f"-> {r}")

# ── validation: shape ────────────────────────────────────────────────────

r = linalg.matrix([[1, 2], [3]], "det")
check("non-rectangular rows are rejected",
      r["ok"] is False and r["code"] == errors.VALIDATION, f"-> {r}")

r = linalg.matrix([[1, 2, 3], [4, 5, 6]], "det")
check("non-square rejected for det",
      r["ok"] is False and r["code"] == errors.VALIDATION, f"-> {r}")

r = linalg.matrix([[1, 2, 3], [4, 5, 6]], "inverse")
check("non-square rejected for inverse",
      r["ok"] is False and r["code"] == errors.VALIDATION, f"-> {r}")

r = linalg.matrix([[1, 2, 3], [4, 5, 6]], "eigenvalues")
check("non-square rejected for eigenvalues",
      r["ok"] is False and r["code"] == errors.VALIDATION, f"-> {r}")

r = linalg.matrix([[1, 2, 3], [4, 5, 6]], "transpose")
check("non-square is FINE for transpose", r["ok"] is True, f"-> {r}")

r = linalg.matrix([[1, 2, 3], [4, 5, 6]], "rank")
check("non-square is FINE for rank", r["ok"] is True, f"-> {r}")

r = linalg.matrix([], "det")
check("empty rows rejected", r["ok"] is False and r["code"] == errors.VALIDATION, f"-> {r}")

r = linalg.matrix([[]], "det")
check("a row with no entries is rejected",
      r["ok"] is False and r["code"] == errors.VALIDATION, f"-> {r}")

r = linalg.matrix([[1, 2], [3, 4]], "frobnicate")
check("an unknown op is rejected, not silently ignored",
      r["ok"] is False and r["code"] == errors.VALIDATION, f"-> {r}")

# ── a singular matrix gets a clean error, not a traceback ──────────────────

r = linalg.matrix([[1, 2], [2, 4]], "inverse")
check("a singular matrix's inverse is a clean refusal",
      r["ok"] is False and r["code"] == errors.VALIDATION
      and "singular" in r["error"], f"-> {r}")

# ── the whole point: a malicious ENTRY is refused exactly like              ──
# ── evaluate_expression refuses the same string (same safety model) ────────

for _payload in ("().__class__", "__import__('os').system('id')", "x.__class__"):
    r = linalg.matrix([[1, _payload], [3, 4]], "det")
    check(f"malicious entry refused per-entry: {_payload[:30]!r}",
          r["ok"] is False and r["code"] == errors.PERMISSION_DENIED, f"-> {r}")

# The SAME string, run through evaluate_expression's own screen directly,
# must land in the same category — proving "refused exactly as
# evaluate_expression refuses it", not merely "also refused".
for _payload in ("().__class__", "__import__('os').system('id')"):
    _direct = safe_expr.classify_unsafe(_payload)
    r = linalg.matrix([[_payload]], "det")
    check(f"matrix's refusal category matches classify_unsafe's own: {_payload[:30]!r}",
          _direct is not None and r["code"] == errors.code_for_safe_expr_category(_direct[0]),
          f"-> direct={_direct} matrix={r.get('code')}")

# A malicious entry that is a NUMBER, not a string, cannot smuggle anything:
# JSON numbers never reach sympy's string parser at all.
r = linalg.matrix([[1, 2], [3, 4.5]], "det")
check("a plain float entry is not screened as a string (nothing to screen)",
      r["ok"] is True, f"-> {r}")

# A bare boolean is not silently treated as 0/1.
r = linalg.matrix([[True, 2], [3, 4]], "det")
check("a boolean entry is rejected rather than silently coerced to 0/1",
      r["ok"] is False and r["code"] == errors.VALIDATION, f"-> {r}")

# ── defense-in-depth gaps closed to match evaluate_expression's own bound ──
# (adversarial review): classify_unsafe alone waves `input`/
# `breakpoint`/`9**9**9**9` through — none of them carry '.', '[', a leading
# underscore or a denied keyword. What stops them is parsing string entries
# through safe_global_dict() (no live Python builtins in it) and running
# reject_explosive on the unevaluated shape, exactly like
# logic.evaluate_expression does for its own input.

# `input`/`breakpoint`/`quit` are live Python builtins with none of
# classify_unsafe's denied syntax. A plain sympify(v) would parse with
# sympy's DEFAULT global_dict (vars(builtins) copied in) and actually CALL
# the real builtin — input() would block reading the child's stdin (shared
# fd 0 with a stdio MCP server), breakpoint() would drop into pdb and hang
# until the wall clock kills it. Parsing through safe_global_dict() instead
# means neither name is bound to anything callable, so the parse either
# produces a harmless symbolic result or a clean parse error — either way,
# NOT a real call. evaluate_expression("input()") has this exact same
# outcome (verified directly against it) for the exact same reason; this
# proves matrix's per-entry parse now matches it.
for _builtin_call in ("input()", "breakpoint()", "quit()"):
    _direct = logic.evaluate_expression(_builtin_call)
    r = linalg.matrix([[_builtin_call]], "det")
    check(f"matrix entry {_builtin_call!r} does not invoke the real builtin",
          r["ok"] is False and r["code"] == errors.VALIDATION
          and "parse error" in r["error"], f"-> {r}")
    check(f"...and matches evaluate_expression's own outcome for {_builtin_call!r}",
          _direct["ok"] is False and "parse error" in str(_direct.get("error")),
          f"-> evaluate_expression={_direct}")

# A power tower burns real CPU seconds if evaluated blind (measured: ~11s on
# 9**9**9**9 before the CPU-time kill). reject_explosive on the unevaluated
# shape rejects it in milliseconds instead — assert BOTH the code and that it
# was actually fast, not just eventually correct.
_t0 = time.monotonic()
r = linalg.matrix([["9**9**9**9"]], "det")
_elapsed = time.monotonic() - _t0
check("a power-tower entry is resource_exhausted, not evaluated",
      r["ok"] is False and r["code"] == errors.RESOURCE_EXHAUSTED, f"-> {r}")
check(f"...and rejected promptly ({_elapsed:.3f}s), not after a multi-second burn",
      _elapsed < 2.0, f"-> {_elapsed:.3f}s")

# inf/nan entries: not silently accepted as a JSON-legal-but-meaningless value.
for _bad in (float("inf"), float("-inf"), float("nan")):
    r = linalg.matrix([[1, _bad], [3, 4]], "det")
    check(f"a non-finite entry ({_bad!r}) is rejected",
          r["ok"] is False and r["code"] == errors.VALIDATION
          and "finite" in r["error"], f"-> {r}")

# ── the improved refusal message on evaluate_expression's own '[' denial ───
# option 3: name matrices and point at the new tool, without
# changing what is refused (still CATEGORY_SECURITY -> permission_denied).

_cls = safe_expr.classify_unsafe("Matrix([[1,2],[3,4]])")
check("evaluate_expression's own '[' refusal still fires (unchanged reach)",
      _cls is not None and _cls[0] == safe_expr.CATEGORY_SECURITY, f"-> {_cls}")
check("...and now names the matrix tool instead of a bare \"'[' is not permitted\"",
      _cls is not None and "matrix tool" in _cls[1]
      and _cls[1] != "'[' is not permitted in an expression", f"-> {_cls}")


print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== ALL MATRIX TESTS PASS ===")
sys.exit(1 if FAILS else 0)
