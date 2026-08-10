"""Stable error codes on the result contract (THE-781).

Every failing tool result carried prose and nothing else — 121 distinct
`"error":` strings across the package. A caller could show one to a human and
could do nothing else with it: "unknown language", "expression too long" and
"session worker died" are three situations a program must tell apart, and the
only way to was to match sentences nobody promised to keep stable.

Two mechanisms, and these tests keep them distinguishable:

  classify(exc)     maps an EXCEPTION TYPE, chosen at the raise site. Strong.
  ensure_code(res)  matches the MESSAGE. Weak, and marked `code_inferred`.

The second exists because the first was not enough. Attaching classify() to
`guarded_call` reached ZERO of the eight most reachable failures, because those
paths return `{"ok": False, ...}` rather than raising. That was an assumption
about control flow, disproved by running it.

A separate file rather than appended to test_features.py: that module ends with
`asyncio.run(main())` and a summary/exit, so anything appended after them either
never runs or has to be threaded through an async fixture it does not need.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import errors, server
from codecalc.optional import MissingExtra

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── the taxonomy itself ────────────────────────────────────────────────────
check("every code has a remedy and every remedy a code",
      set(errors.REMEDIES) == errors.ALL_CODES,
      f"-> {sorted(set(errors.REMEDIES) ^ errors.ALL_CODES)}")
check("the taxonomy is the 8 categories THE-781 names",
      len(errors.ALL_CODES) == 8, f"-> {len(errors.ALL_CODES)}")

# A typo'd code must not pass through. A caller branching on it takes the wrong
# branch and nothing raises, so this fails loudly at the point of the mistake.
_bogus = errors.error_result("not_a_real_code", "boom")
check("an unknown code degrades to internal and says so",
      _bogus["code"] == errors.INTERNAL and "unclassified" in _bogus["error"],
      f"-> {_bogus['code']}")

# ── classify(): exception type first ───────────────────────────────────────
# MissingExtra subclasses ImportError deliberately (optional.py), so it must be
# tested BEFORE the ImportError branch. Get the order wrong and every absent
# extra reports as an internal defect — unactionable, and the opposite of true,
# since a missing extra is precisely what the caller CAN fix.
check("MissingExtra -> dependency_missing, not internal",
      errors.classify(MissingExtra("sympy", "symbolic")) == errors.DEPENDENCY_MISSING,
      f"-> {errors.classify(MissingExtra('sympy', 'symbolic'))}")
for _exc, _want in [
    (TimeoutError(), errors.TIMEOUT),
    (MemoryError(), errors.RESOURCE_EXHAUSTED),
    (RecursionError(), errors.RESOURCE_EXHAUSTED),
    (PermissionError(), errors.PERMISSION_DENIED),
    (FileNotFoundError(), errors.RUNTIME_UNAVAILABLE),
    (ValueError("x"), errors.VALIDATION),
]:
    check(f"classify({type(_exc).__name__}) -> {_want}",
          errors.classify(_exc) == _want, f"-> {errors.classify(_exc)}")

# ── ensure_code(): only failures, and honest about provenance ──────────────
check("a successful result is left alone",
      errors.ensure_code({"ok": True, "stdout": "x"}) == {"ok": True, "stdout": "x"})
check("an inferred code is MARKED inferred",
      errors.ensure_code({"ok": False, "error": "unknown language 'zz'"})["code_inferred"] is True)
check("a code chosen at the raise site is not overwritten or relabelled",
      errors.ensure_code({"ok": False, "code": errors.TIMEOUT, "error": "x"}).get("code_inferred") is None)

# ── the failures a model actually reaches ──────────────────────────────────
# INTERNAL is the fallback and must be RARE: its remedy tells the caller to
# report a bug, so classifying a mistyped expression that way sends them to
# file an issue about their own typo. The first hint list did that for 4 of
# these — a wrong code is worse than a missing one, because it is actionable
# in the wrong direction.
for _label, _call, _want in [
    ("unknown language", lambda: server.execute_code("klingon", "x"), errors.VALIDATION),
    ("bad expression", lambda: server.simplify_expression("x +++ ***"), errors.VALIDATION),
    ("truth_table parse error", lambda: server.truth_table("A &&& B"), errors.VALIDATION),
    ("oversized expression", lambda: server.simplify_expression("x*" * 60000 + "x"),
     errors.RESOURCE_EXHAUSTED),
]:
    _r = _call()
    check(f"{_label} -> {_want}", _r.get("code") == _want, f"-> {_r.get('code')}")
    check("  ...and carries an actionable remedy", bool(_r.get("remedy")))

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL ERROR-CODE TESTS PASS ===")
sys.exit(1 if FAILS else 0)
