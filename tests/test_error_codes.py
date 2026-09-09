"""Stable error codes on the result contract.

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

import asyncio
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
check("the taxonomy is the 8 categories names",
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

# backstop: CPython >=3.14 reworded the interpreter-level recursion
# failure to "stack overflow" (older versions said "maximum recursion depth
# exceeded"). classify() already maps RecursionError by TYPE (tested above), but
# if a message ever reaches message-classification instead — a sympify site that
# swallows the RecursionError and returns its text — this hint keeps it in
# resource_exhausted rather than letting the reworded message fall to internal.
check("'stack overflow' message -> resource_exhausted (3.14 wording backstop)",
      errors._from_message("Fatal Python error: stack overflow") == errors.RESOURCE_EXHAUSTED,
      f"-> {errors._from_message('stack overflow')}")

# ── the failures a model actually reaches ──────────────────────────────────
# INTERNAL is the fallback and must be RARE: its remedy tells the caller to
# report a bug, so classifying a mistyped expression that way sends them to
# file an issue about their own typo. The first hint list did that for 4 of
# these — a wrong code is worse than a missing one, because it is actionable
# in the wrong direction.
for _label, _call, _want in [
    ("unknown language", lambda: server.execute_code("klingon", "x"), errors.VALIDATION),
    ("bad expression", lambda: server.symbolic(op="simplify", expr="x +++ ***"), errors.VALIDATION),
    ("truth_table parse error", lambda: server.truth_table("A &&& B"), errors.VALIDATION),
    ("oversized expression", lambda: server.symbolic(op="simplify", expr="x*" * 60000 + "x"),
     errors.RESOURCE_EXHAUSTED),
]:
    _r = _call()
    check(f"{_label} -> {_want}", _r.get("code") == _want, f"-> {_r.get('code')}")
    check("  ...and carries an actionable remedy", bool(_r.get("remedy")))

# argument-validation rejections were blaming the USER for their own
# bad input. `_from_message`'s hint list had no vocabulary for "is zero",
# "need at least", "negative" or ">=", so every one of these landed on
# INTERNAL — whose remedy tells the caller "a defect in codecalc; the message
# is worth reporting verbatim". A caller who passed a zero total or a
# negative duration was being told to go file a bug about someone else's
# mistake. None of these raise a typed exception (they `return {"ok": False,
# ...}` directly, same as the cases above), so classify()'s type
# dispatch never sees them — only ensure_code()'s message match does.
for _label, _call in [
    ("benchmark: too few sizes", lambda: server.benchmark("x", sizes="1,2")),
    ("compare_threshold: bad operator", lambda: server.compare_threshold("1", "~=", "2")),
    ("percentage: zero total", lambda: server.percentage("1", "0")),
    ("calc_stats: too few numbers", lambda: server.calc_stats([1.0])),
    ("percentiles: no numbers", lambda: server.percentiles([])),
    ("collision_probability: zero bits", lambda: server.collision_probability(10, 0)),
    ("human_duration: negative seconds", lambda: server.human_duration(-5)),
    ("epoch_time: negative epoch", lambda: server.epoch_time("-5")),
    ("bitop: negative shift count", lambda: server.bits(mode="op", a=1, op="shl", b=-1)),
    ("bitop: unknown op", lambda: server.bits(mode="op", a=1, op="zzz", b=2)),
    ("compare_edge_cases: no snippets", lambda: server.compare_edge_cases({})),
]:
    _r = _call()
    check(f"{_label} -> validation, not internal",
          _r.get("code") == errors.VALIDATION,
          f"-> code={_r.get('code')} err={_r.get('error')!r}")

# the length cap must gate EVERY sympify entry point, not only the one
# in _eval_exact. Before the fix, algebraic_equiv / solve_expression /
# limit_expression reached sp.sympify with a 120k-char string, blew the
# recursion limit, and — because RecursionError's message wording is
# interpreter-specific — landed on `internal` on CPython >=3.14 instead of
# `resource_exhausted`. Each now rejects "expression too long" before any parse,
# on every interpreter, so the code is deterministic across the version matrix.
_huge = "x*" * 60000 + "x"
for _label, _call in [
    ("algebraic_equiv oversized a", lambda: server.algebraic_equiv(_huge, "x")),
    ("algebraic_equiv oversized b", lambda: server.algebraic_equiv("x", _huge)),
    ("solve_expression oversized", lambda: server.symbolic(op="solve", expr=_huge)),
    ("limit_expression oversized expr", lambda: server.symbolic(op="limit", expr=_huge)),
    ("limit_expression oversized point", lambda: server.symbolic(op="limit", expr="x", var="x", point=_huge)),
]:
    _r = _call()
    # Assert the CAP's own message, not merely the code: "too long" proves the
    # length gate rejected the input BEFORE sympify. Matching only the code
    # would also pass if the parser blew up and the "stack overflow" hint caught
    # it — that is the backstop, tested separately above, not this gate.
    check(f"{_label} -> resource_exhausted, capped before sympify",
          _r.get("code") == errors.RESOURCE_EXHAUSTED
          and "too long" in str(_r.get("error")),
          f"-> code={_r.get('code')} err={str(_r.get('error'))[:50]!r}")

# the GUARD/POLICY half of an earlier error-classification bug
# (GH #214) that fixed argument-validation refusals landing on
# `internal`; this is the other 8 of 10 reachable cases — a guarded-eval
# allowlist SUCCESSFULLY blocking a sandbox-escape attempt, a ceiling, a
# leaked exception repr and a documented policy refusal, all of which
# `_from_message`'s hint list has no vocabulary for and so fell to
# `internal` — indistinguishable from an unhandled crash, which is the exact
# pair an operator needs to tell apart after an incident.
#
# The gate: no rejection from the guarded-evaluation allowlist may classify
# as `internal`. Representative rather than exhaustive — one per screen rule
# (leading underscore, attribute access, string literal) across more than
# one tool, so a fix that only patched `evaluate_expression` would not pass.
for _label, _call in [
    ("evaluate_expression: __import__", lambda: server.evaluate_expression(
        "__import__('os').system('x')")),
    ("evaluate_expression: attribute access", lambda: server.evaluate_expression(
        "().__class__.__bases__")),
    ("simplify_expression: string literal", lambda: server.symbolic(op="simplify", expr=
        "open('/tmp/x','w')")),
    ("solve_expression: attribute access", lambda: server.symbolic(op="solve", expr=
        "os.system('x')")),
    ("solve_linear: attribute access", lambda: server.symbolic(op="solve_linear", system=
        "os.system(1) = 0", variables="x")),
]:
    _r = _call()
    check(f"{_label} -> not internal", _r.get("code") != errors.INTERNAL,
          f"-> code={_r.get('code')} err={str(_r.get('error'))[:60]!r}")
    check(f"{_label} -> permission_denied",
          _r.get("code") == errors.PERMISSION_DENIED, f"-> {_r.get('code')}")

# Group 2: a ceiling is a ceiling, not a defect. RESOURCE_EXHAUSTED's
# own comment is "memory, output or process ceiling hit" — an exponent tower
# or an oversized result is exactly that.
for _label, _call in [
    ("evaluate_expression: power tower", lambda: server.evaluate_expression("9**9**9")),
    ("calc_exact: exponent ceiling", lambda: server.calc_exact("9**9**9")),
]:
    _r = _call()
    check(f"{_label} -> resource_exhausted, not internal",
          _r.get("code") == errors.RESOURCE_EXHAUSTED,
          f"-> code={_r.get('code')} err={str(_r.get('error'))[:60]!r}")

# REGRESSION CAUGHT IN REVIEW: safe_expr.reject_unsafe screens for TWO
# different things through one message-string return — the RCE token/keyword
# screen (a jail: `permission_denied`) and, in its own last line
# (`_heavy_call_violation`), a heavy-argument CEILING like `factorial(100000)`
# (a resource cap: `resource_exhausted`, same family as the power-tower cases
# just above). A first pass at this fix mapped every `reject_unsafe`
# rejection to `permission_denied` uniformly, which flipped
# `factorial(100000)` from its PRE-EXISTING correct `resource_exhausted`
# (GH #214 names this case as one of the two that already worked) into a
# wrong `permission_denied` — a resource ceiling reading as a security
# refusal. `classify_unsafe` (safe_expr.py) now hands back which of the two
# this is, and exact.py/logic.py map "ceiling" to RESOURCE_EXHAUSTED and
# "security" to PERMISSION_DENIED separately, so this must never regress
# again alongside Group 1's guard-refusal gate above.
for _label, _call in [
    ("calc_exact: factorial(100000) heavy-arg ceiling",
     lambda: server.calc_exact("factorial(100000)")),
    ("evaluate_expression: binomial heavy-arg ceiling",
     lambda: server.evaluate_expression("binomial(200000, 100000)")),
]:
    _r = _call()
    check(f"{_label} -> resource_exhausted, NOT permission_denied",
          _r.get("code") == errors.RESOURCE_EXHAUSTED,
          f"-> code={_r.get('code')} err={str(_r.get('error'))[:60]!r}")

# Group 3: `Fraction(1, 1) / Fraction(0, 1)` raises ZeroDivisionError
# whose message IS `str(Fraction(1, 0))` — the constructor argument, not a
# sentence. It reached the caller verbatim as `"error": "Fraction(1, 0)"`.
_r = server.calc_exact("1/0")
check("calc_exact('1/0') -> validation, not internal",
      _r.get("code") == errors.VALIDATION, f"-> {_r.get('code')}")
check("  ...with a human message, not the exception's constructor arg",
      _r.get("error") == "division by zero", f"-> {_r.get('error')!r}")

# Group 4: install_package's unsupported-language refusal is a
# documented POLICY decision (packages.py's _UNSUPPORTED/_DECLINED_REASON),
# same taxonomy as the allowlist denial a few lines below it in packages.py
# that already returned permission_denied.
#
# install_package is `async def` (it awaits ctx.elicit on a legacy connection
# with elicitation — codecalc/confirmation.py); called directly like this,
# with no `ctx`, the confirmation gate is a no-op (require_confirmation
# returns None outside an active MCP request) and the unsupported-language
# refusal below is unaffected by that gate — it fires from packages.install()
# either way.
_r = asyncio.run(server.install_package(language="ruby", package="nokogiri"))
check("install_package(ruby) -> permission_denied, not internal",
      _r.get("code") == errors.PERMISSION_DENIED, f"-> {_r.get('code')}")

# ── ensure_code(): a real per-run verdict must not be message-matched ──────
# `executor.execute` never sets `error` for an ordinary program failure (a
# plain `sys.exit(3)` RTE) or a plain wall-clock timeout (TLE) — only
# `verdict`/`exit_code`/`timed_out` say so (see executor.py's run-phase
# result and `_fallback_verdict`). Before this fix, `ensure_code` had an
# empty string to message-match and fell through to `internal` regardless —
# a model whose program correctly reported its own failure was told to go
# file a bug about codecalc. Synthetic envelope-shaped dicts here (no real
# execution needed to prove `ensure_code`'s own decision); a REAL exit-3 run
# and a real timeout, on both backends plus a stdio round-trip, live in
# tests/test_platform_contract.py instead.
_rte = errors.ensure_code({"ok": False, "verdict": "RTE", "exit_code": 3,
                           "timed_out": False, "stderr": "boom"})
check("a real RTE verdict with no error carries no code at all",
      "code" not in _rte, f"-> {_rte.get('code')!r}")
check("...and no remedy/code_inferred either",
      "remedy" not in _rte and "code_inferred" not in _rte, f"-> {_rte}")

# `phase` plays no part in the decision — a COMPILE failure is the identical
# rule as a RUN failure (a failed PROGRAM, not a failed REQUEST): retrying
# the same request cannot succeed either way. Real compiler invocations on
# both backends live in tests/test_platform_contract.py.
_ce = errors.ensure_code({"ok": False, "verdict": "RTE", "exit_code": 1,
                          "phase": "compile", "stderr": "syntax error"})
check("a compile-phase RTE with no error carries no code either",
      "code" not in _ce, f"-> {_ce.get('code')!r}")

for _label, _shape in [
    ("verdict == TLE alone", {"ok": False, "verdict": "TLE", "exit_code": None}),
    ("verdict == TLE with timed_out (compact mode drops timed_out, not verdict)",
     {"ok": False, "verdict": "TLE", "timed_out": True, "exit_code": None}),
]:
    _tle = errors.ensure_code(dict(_shape))
    check(f"a timeout ({_label}) is classified timeout, not internal",
          _tle.get("code") == errors.TIMEOUT, f"-> {_tle.get('code')}")
    check("...and marked code_inferred (nothing chose it at a raise site)",
          _tle.get("code_inferred") is True, f"-> {_tle.get('code_inferred')}")
    check("...with the timeout remedy",
          _tle.get("remedy") == errors.REMEDIES[errors.TIMEOUT], f"-> {_tle.get('remedy')!r}")

# OLE/MLE follow the identical RTE rule: a real verdict, no error, no code.
for _verdict in ("OLE", "MLE"):
    _r = errors.ensure_code({"ok": False, "verdict": _verdict, "exit_code": None})
    check(f"a real {_verdict} verdict with no error carries no code either",
          "code" not in _r, f"-> {_r.get('code')!r}")

# A spawn failure (executor DOES set `error`, and still carries `verdict` —
# "the code ran as far as it could") is unaffected: classified from the
# message exactly as before this fix.
_spawn = errors.ensure_code({
    "ok": False, "verdict": "RTE", "exit_code": None,
    "error": "runtime unavailable for the run phase: 'lua' not found",
})
check("a spawn failure (verdict present, error already set) is unaffected",
      _spawn.get("code") == errors.RUNTIME_UNAVAILABLE, f"-> {_spawn.get('code')}")

# A genuine unknown — ok: False, no verdict, no error, no exit_code — still
# falls to internal/code_inferred, exactly as before this fix.
_unknown = errors.ensure_code({"ok": False})
check("a genuine unknown (no verdict, no error) is still internal",
      _unknown.get("code") == errors.INTERNAL, f"-> {_unknown.get('code')}")
check("...and marked code_inferred",
      _unknown.get("code_inferred") is True, f"-> {_unknown.get('code_inferred')}")

# A code chosen at the raise site is never overwritten, verdict or not —
# same regression guard as the earlier "not overwritten" check, extended to
# a result that also carries a verdict.
_pre_coded = errors.ensure_code({"ok": False, "verdict": "RTE", "code": errors.WORKER_FAILURE})
check("a pre-coded verdict-bearing result is left alone",
      _pre_coded.get("code") == errors.WORKER_FAILURE
      and "code_inferred" not in _pre_coded, f"-> {_pre_coded}")

# End-to-end: a real python3 process that calls sys.exit(3) must round-trip
# through execute_code -> server._coded -> errors.ensure_code with no
# fabricated code — the exact model-facing symptom this fix closes.
_real_rte = server.execute_code("python3", "import sys; sys.exit(3)")
check("execute_code: a real sys.exit(3) carries no code",
      _real_rte.get("ok") is False and _real_rte.get("verdict") == "RTE"
      and _real_rte.get("exit_code") == 3 and "code" not in _real_rte,
      f"-> ok={_real_rte.get('ok')} verdict={_real_rte.get('verdict')!r} "
      f"exit_code={_real_rte.get('exit_code')!r} code={_real_rte.get('code')!r}")
check("...and no remedy/code_inferred either",
      "remedy" not in _real_rte and "code_inferred" not in _real_rte,
      f"-> {_real_rte}")

# `compact=True` must classify IDENTICALLY to `compact=False` — a "rejected
# before execution" shape (no verdict/stdout/exit_code) has NOTHING else to
# classify from, so `compact_result` dropping `error` before `ensure_code`
# ran left it `internal` regardless of the real cause. Real execution on
# both backends (missing runtime, timeout, plain RTE too) lives in
# tests/test_platform_contract.py; this pins the exact symptom reported.
_full_reject = server.execute_code("nosuchlang", "x")
_compact_reject = server.execute_code("nosuchlang", "x", compact=True)
check("execute_code(compact=True) on an unknown language matches compact=False",
      _compact_reject.get("code") == _full_reject.get("code") == errors.VALIDATION,
      f"-> full={_full_reject.get('code')} compact={_compact_reject.get('code')}")
check("...and still carries error/remedy (the ENTIRE content of this shape)",
      bool(_compact_reject.get("error")) and bool(_compact_reject.get("remedy")),
      f"-> {_compact_reject}")

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL ERROR-CODE TESTS PASS ===")
sys.exit(1 if FAILS else 0)
