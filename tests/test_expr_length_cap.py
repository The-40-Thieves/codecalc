"""Behavioral floor: EVERY public sympy-backed tool caps oversized input BEFORE
it reaches sp.sympify (THE-844).

The length cap (`_MAX_EXPR_LEN`, 2000) is the load-bearing gate: it rejects an
oversized expression as "expression too long" on every interpreter, before any
parse. Without it, a 120k-char string reaches SymPy's parser, blows the
recursion limit, and lands on an interpreter-specific failure whose message
wording differs across CPython versions ("maximum recursion depth exceeded" on
<3.14, "stack overflow" on >=3.14) — so the SAME input classified as
`resource_exhausted` on 3.11 and `internal` on 3.14.

The cap was first added to `_eval_exact` only, then to the four exact.py sympy
tools, and STILL missed `solve_linear` in logic.py — which reached sp.sympify
uncapped and only returned `resource_exhausted` by riding the "stack overflow"
message hint, the exact fragile mechanism THE-844 exists to eliminate.

So this floor enumerates every public sympy-backed tool and asserts each rejects
oversized input VIA THE CAP, not via the message backstop. The distinction is
the whole point: the cap's message is "expression too long (max N chars)"; the
backstop fires on "stack overflow". Asserting the cap's own marker proves the
input was rejected before parse (the fast path), and a tool that loses its cap
fails here even though its error CODE is still `resource_exhausted` via the
backstop — which is exactly how uncapped `solve_linear` slipped through.
"""

from __future__ import annotations

import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import errors, server

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ~120k chars — the size the ticket used, and far past _MAX_EXPR_LEN (2000).
# Chosen so that a tool which LOST its cap would spend ~2s in SymPy's parser
# (measured) instead of rejecting instantly, i.e. the length matters only if the
# cap is absent. A capped tool never parses it.
_HUGE = "x*" * 60000 + "x"                    # arithmetic / algebraic
_HUGE_SYS = "x*" * 60000 + "x" + "=1"         # a linear-system equation
_HUGE_BOOL = "A & " * 30000 + "A"             # boolean expression (truth_table)

# Warm the symbolic import once, untimed, so the first cap rejection below is not
# charged for SymPy's ~0.4s module load.
server.simplify_expression("x + 1")

# (label, thunk) for every public tool that hands a caller string to sp.sympify.
_CASES = [
    ("evaluate_expression", lambda: server.evaluate_expression(_HUGE)),
    ("simplify_expression", lambda: server.simplify_expression(_HUGE)),
    ("solve_expression", lambda: server.solve_expression(_HUGE)),
    ("solve_linear", lambda: server.solve_linear(_HUGE_SYS, "x")),
    ("limit_expression", lambda: server.limit_expression(_HUGE)),
    ("limit_expression (point arg)", lambda: server.limit_expression("x", "x", _HUGE)),
    ("truth_table", lambda: server.truth_table(_HUGE_BOOL)),
    ("algebraic_equiv (a)", lambda: server.algebraic_equiv(_HUGE, "x")),
    ("algebraic_equiv (b)", lambda: server.algebraic_equiv("x", _HUGE)),
]

for _label, _thunk in _CASES:
    _t0 = time.monotonic()
    _r = _thunk()
    _elapsed = time.monotonic() - _t0
    _msg = str(_r.get("error") or _r.get("reason") or "")
    # THE cap fired if BOTH hold: the stable code, AND the cap's own message.
    # "too long" is unique to the length gate; the RecursionError/stack-overflow
    # backstop never produces it, so this rejects a tool that reaches the parser
    # even when the backstop keeps its CODE correct.
    check(f"{_label}: oversized rejected by the length cap (before sympify)",
          _r.get("code") == errors.RESOURCE_EXHAUSTED and "too long" in _msg,
          f"-> code={_r.get('code')} t={_elapsed*1000:.0f}ms msg={_msg[:48]!r}")

# The floor is only meaningful if the cap's message is actually distinct from the
# backstop's — assert the two mechanisms cannot be confused.
check("the cap message and the stack-overflow backstop are distinguishable",
      "too long" not in "Stack overflow (used 8144 kB) during compilation"
      and errors._from_message("Stack overflow during compilation") == errors.RESOURCE_EXHAUSTED)

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== EVERY PUBLIC SYMPY TOOL CAPS OVERSIZED INPUT BEFORE PARSE ===")
sys.exit(1 if FAILS else 0)
