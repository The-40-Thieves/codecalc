"""`branch_reachability` (codecalc/branch_reachability.py): every verdict it
prints is checked against something OTHER than its own model — a
`reachable` witness is run for real through `tracing.execute_trace` and its
branch line must actually fire; a `dead` branch's own boundary edges must
NOT fire it. `verdict`/`witness`/`boundary_inputs` are asserted by VALUE,
never merely by shape (CONTRIBUTING.md: "assert the value, not the shape").

Standalone script, not pytest — see tests/conftest.py.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import branch_reachability as br
from codecalc import tracing

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def _by_line(result: dict, line: int) -> dict | None:
    return next((b for b in result["branches"] if b["line"] == line), None)


def _proof_line(code: str, header_line: int, kind: str) -> int:
    """The line INSIDE the arm's own body that fires only when this specific
    arm is taken — never the if/elif/while/for header line itself, which
    fires whenever control merely REACHES that statement, regardless of
    which way (or whether) it branches. `branch_reachability`'s own `line`
    is that header for if/elif/while/for (matching `tracing.py`'s own
    `branches` convention — see its `_BRANCH_NODE_TYPES` docstring), so
    proving a branch was actually TAKEN needs one line further in. An
    `else` arm is the one exception: `branch_reachability` has no separate
    AST node for the bare `else:` keyword, so it already reports the
    body's own first line (see `_walk_if`'s `_record(..., line=node.orelse[0].lineno, ...)`
    call) — returned unchanged.
    """
    if kind == "else":
        return header_line
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.For)) and node.lineno == header_line:
            return node.body[0].lineno
    raise AssertionError(f"no {kind} node at line {header_line} in:\n{code}")


def _hits_line(code: str, func_name: str, call_kwargs: dict, line: int) -> bool:
    """Runs `code` for real (via tracing.execute_trace, the SAME mechanism
    `trace_execution` uses) with a trailing call to `func_name(**call_kwargs)`,
    and returns whether `line` actually fired. This is the corroboration
    every `witness` and every dead branch's own boundary edge is checked
    against below — never trusted from the z3 model alone. `line` should be
    a PROOF line (see `_proof_line`), not a branch's raw header line.
    """
    call_src = f"{code}\n{func_name}(**{call_kwargs!r})\n"
    result = tracing.execute_trace("python3", call_src)
    assert result.get("verdict") == "OK", f"trace run failed: {result}"
    return line in set(result.get("lines_executed") or [])


def _verify_every_witness(result: dict, code: str, func_name: str, label: str) -> None:
    for b in result["branches"]:
        if b["verdict"] != "reachable":
            continue
        proof = _proof_line(code, b["line"], b["kind"])
        hit = _hits_line(code, func_name, b["witness"], proof)
        check(f"{label}: witness for line {b['line']} ({b['kind']}) actually TAKES "
              f"the arm — its body's own line {proof} fires — via a real trace run",
              hit, f"-> witness={b['witness']}")


# ── if/elif/else + a static-range loop: reachable/dead/unknown ─────────────
_CLASSIFY = (
    "def classify(n):\n"
    "    if n % 2 == 0:\n"
    "        return \"even\"\n"
    "    elif n > 100:\n"
    "        return \"big\"\n"
    "    else:\n"
    "        return \"odd\"\n"
)
_r = br.analyze("python3", _CLASSIFY)
check("classify: ok", _r.get("ok") is True, f"-> {_r}")
check("classify: supported", _r.get("supported") is True)
check("classify: inputs resolved to int (unannotated default)",
      _r.get("inputs") == {"n": "int"}, f"-> {_r.get('inputs')}")
check("classify: all three arms are reachable",
      _r.get("reachable_count") == 3 and _r.get("dead_count") == 0
      and _r.get("unknown_count") == 0,
      f"-> reachable={_r.get('reachable_count')} dead={_r.get('dead_count')} "
      f"unknown={_r.get('unknown_count')}")
check("classify: three branches recorded (if/elif/else)",
      [b["kind"] for b in _r["branches"]] == ["if", "elif", "else"],
      f"-> {[b['kind'] for b in _r['branches']]}")
_verify_every_witness(_r, _CLASSIFY, "classify", "classify")

_LOOP = (
    "def f(n):\n"
    "    total = 0\n"
    "    for i in range(5):\n"
    "        if i > n:\n"
    "            total = total + 1\n"
    "    return total\n"
)
_rl = br.analyze("python3", _LOOP)
check("loop: for-line and nested if-line both recorded",
      [b["kind"] for b in _rl["branches"]] == ["for", "if"],
      f"-> {[b['kind'] for b in _rl['branches']]}")
check("loop: both reachable (the for always enters — range(5) is non-empty; "
      "the nested if is reachable for i in [0,4])",
      _rl.get("reachable_count") == 2, f"-> {_rl}")
_verify_every_witness(_rl, _LOOP, "f", "loop")

# A DEAD static-range loop: range(0) never enters.
_DEAD_LOOP = "def f(n):\n    for i in range(0):\n        n = n + 1\n    return n\n"
_rdl = br.analyze("python3", _DEAD_LOOP)
_for_branch = _by_line(_rdl, 2)
check("range(0): the for-line itself is dead (the loop can never be entered)",
      _for_branch is not None and _for_branch["verdict"] == "dead", f"-> {_rdl['branches']}")


# ── dead branch: x > 5 and x < 3 ────────────────────────────────────────────
_DEAD = "def f(x):\n    if x > 5 and x < 3:\n        return 1\n    return 0\n"
_rd = br.analyze("python3", _DEAD)
_dead_branch = _by_line(_rd, 2)
check("x>5 and x<3: verdict is dead", _dead_branch is not None
      and _dead_branch["verdict"] == "dead", f"-> {_dead_branch}")
check("x>5 and x<3: no witness on a dead branch", "witness" not in (_dead_branch or {}))
check("x>5 and x<3: two boundary_inputs entries, one per Compare",
      len(_dead_branch["boundary_inputs"]) == 2, f"-> {_dead_branch['boundary_inputs']}")
for _entry in _dead_branch["boundary_inputs"]:
    check(f"x>5 and x<3: dead branch's own guard {_entry['guard']!r} has no "
          f"satisfying min/max (the branch itself is unsatisfiable)",
          _entry["min_input"] is None and _entry["max_input"] is None,
          f"-> {_entry}")
    edge = _entry.get("equality_edge_input")
    if edge is not None:
        _dead_proof = _proof_line(_DEAD, _dead_branch["line"], _dead_branch["kind"])
        hit = _hits_line(_DEAD, "f", edge, _dead_proof)
        check(f"x>5 and x<3: the equality edge {edge!r} for {_entry['guard']!r} "
              f"does NOT actually reach the dead branch's own body (corroborated "
              f"by a real trace run, not just the solver)", not hit, f"-> hit={hit}")


# ── boundary inputs for <, <=, ==, != ───────────────────────────────────────
_OPS = {
    "<": "def f(x):\n    if x < 10:\n        return 1\n    return 0\n",
    "<=": "def f(x):\n    if x <= 10:\n        return 1\n    return 0\n",
    "==": "def f(x):\n    if x == 10:\n        return 1\n    return 0\n",
    "!=": "def f(x):\n    if x != 10:\n        return 1\n    return 0\n",
}
_EXPECTED_MIN = {"<": -1_000_000, "<=": -1_000_000, "==": 10, "!=": -1_000_000}
_EXPECTED_MAX = {"<": 9, "<=": 10, "==": 10, "!=": 1_000_000}
for _op, _code in _OPS.items():
    _ro = br.analyze("python3", _code)
    _b = _by_line(_ro, 2)
    check(f"op {_op}: branch is reachable", _b["verdict"] == "reachable", f"-> {_b}")
    _entries = _b["boundary_inputs"]
    check(f"op {_op}: exactly one boundary_inputs entry", len(_entries) == 1, f"-> {_entries}")
    _entry = _entries[0]
    check(f"op {_op}: operator recorded correctly", _entry["operator"] == _op, f"-> {_entry}")
    check(f"op {_op}: min_input.x == {_EXPECTED_MIN[_op]}",
          _entry["min_input"] is not None and _entry["min_input"]["x"] == _EXPECTED_MIN[_op],
          f"-> {_entry['min_input']}")
    check(f"op {_op}: max_input.x == {_EXPECTED_MAX[_op]}",
          _entry["max_input"] is not None and _entry["max_input"]["x"] == _EXPECTED_MAX[_op],
          f"-> {_entry['max_input']}")
    check(f"op {_op}: equality_edge_input.x == 10",
          _entry["equality_edge_input"] is not None and _entry["equality_edge_input"]["x"] == 10,
          f"-> {_entry['equality_edge_input']}")
    _proof = _proof_line(_code, _b["line"], _b["kind"])
    check(f"op {_op}: min_input actually reaches the branch's own body (trace-corroborated)",
          _hits_line(_code, "f", _entry["min_input"], _proof), f"-> {_entry['min_input']}")
    check(f"op {_op}: max_input actually reaches the branch's own body (trace-corroborated)",
          _hits_line(_code, "f", _entry["max_input"], _proof), f"-> {_entry['max_input']}")


# ── str equality and len() guards ───────────────────────────────────────────
_STR_PROGRAM = (
    "def g(s, flag):\n"
    "    if flag and s == \"hi\":\n"
    "        return 1\n"
    "    if len(s) > 3:\n"
    "        return 2\n"
    "    return 0\n"
)
_rs = br.analyze("python3", _STR_PROGRAM, inputs={"s": "str", "flag": "bool"})
check("str/len: inputs resolved to str/bool as given",
      _rs["inputs"] == {"s": "str", "flag": "bool"}, f"-> {_rs['inputs']}")
_eq_branch = _by_line(_rs, 2)
check("str eq guard: reachable, witness has s == 'hi' and flag True",
      _eq_branch["verdict"] == "reachable" and _eq_branch["witness"]["s"] == "hi"
      and _eq_branch["witness"]["flag"] is True, f"-> {_eq_branch}")
check("str eq guard ('==' only): no boundary_inputs (no numeric side to bound)",
      _eq_branch["boundary_inputs"] == [], f"-> {_eq_branch['boundary_inputs']}")
_len_branch = _by_line(_rs, 4)
check("len(s) > 3 guard: reachable", _len_branch["verdict"] == "reachable", f"-> {_len_branch}")
_len_entries = _len_branch["boundary_inputs"]
check("len(s) > 3: one boundary_inputs entry, operator '>'",
      len(_len_entries) == 1 and _len_entries[0]["operator"] == ">", f"-> {_len_entries}")
check("len(s) > 3: min_input has a 4-character string (the minimum satisfying length)",
      _len_entries[0]["min_input"] is not None and len(_len_entries[0]["min_input"]["s"]) == 4,
      f"-> {_len_entries[0]['min_input']}")
check("len(s) > 3: min_input actually reaches the branch's own body (trace-corroborated)",
      _hits_line(_STR_PROGRAM, "g", _len_entries[0]["min_input"],
                 _proof_line(_STR_PROGRAM, _len_branch["line"], _len_branch["kind"])),
      f"-> {_len_entries[0]['min_input']}")
_verify_every_witness(_rs, _STR_PROGRAM, "g", "str/len")


# ── bool inputs ──────────────────────────────────────────────────────────────
_BOOL_PROGRAM = "def h(flag):\n    if flag:\n        return 1\n    return 0\n"
_rb = br.analyze("python3", _BOOL_PROGRAM, inputs={"flag": "bool"})
check("bool input: resolved type is bool", _rb["inputs"] == {"flag": "bool"}, f"-> {_rb['inputs']}")
_bool_branch = _by_line(_rb, 2)
check("bool input: reachable with witness flag=True",
      _bool_branch["verdict"] == "reachable" and _bool_branch["witness"] == {"flag": True},
      f"-> {_bool_branch}")
_verify_every_witness(_rb, _BOOL_PROGRAM, "h", "bool")


# ── early return cutting a later branch dead ────────────────────────────────
_EARLY_RETURN = (
    "def f(x):\n"
    "    if x > 0:\n"
    "        return 1\n"
    "    y = x + 1\n"
    "    if y > 100:\n"
    "        return 2\n"
    "    return 3\n"
)
_re = br.analyze("python3", _EARLY_RETURN)
_first = _by_line(_re, 2)
_second = _by_line(_re, 5)
check("early return: the first if is reachable", _first["verdict"] == "reachable", f"-> {_first}")
check("early return: the second if (only reachable via the first's return) is DEAD "
      "— an unconditional return cuts that path, so y = x + 1 is bound under x <= 0, "
      "making y > 100 unsatisfiable", _second["verdict"] == "dead", f"-> {_second}")
_verify_every_witness(_re, _EARLY_RETURN, "f", "early-return")


# ── refusals: construct + exact line, remedy names trace_execution ─────────
_REFUSAL_CASES = {
    "float": ("def f(x):\n    if x > 1.5:\n        return 1\n    return 0\n", 2),
    "attribute": ("def f(s):\n    if s.strip():\n        return 1\n    return 0\n", 2),
    "data-dependent range": ("def f(n):\n    for i in range(n):\n        pass\n    return 0\n", 2),
    "try/except": ("def f(x):\n    try:\n        return x\n    except Exception:\n        return 0\n", 2),
    "comprehension": ("def f(xs):\n    y = [i for i in xs]\n    return y\n", 2),
    "I/O call": ("def f(x):\n    if x > 0:\n        print(x)\n    return 0\n", 3),
}
for _label, (_code, _expected_line) in _REFUSAL_CASES.items():
    _rr = br.analyze("python3", _code)
    check(f"refusal ({_label}): ok is False, code is validation",
          _rr.get("ok") is False and _rr.get("code") == "validation", f"-> {_rr}")
    check(f"refusal ({_label}): line == {_expected_line}",
          _rr.get("line") == _expected_line, f"-> {_rr}")
    check(f"refusal ({_label}): remedy names trace_execution",
          "trace_execution" in (_rr.get("remedy") or ""), f"-> {_rr.get('remedy')}")

# non-python refusal spawns nothing — a tripwire on optional.require (the one
# function that would import z3) proves branch_reachability never reaches the
# solver for a language it does not support, same tripwire shape
# tests/test_trace_execution.py uses on executor.execute.
_spawned = {"called": False}
_real_require = br.optional.require


def _tripwire_require(*args, **kwargs):
    _spawned["called"] = True
    return _real_require(*args, **kwargs)


br.optional.require = _tripwire_require
try:
    _refused_lang = br.analyze("javascript", "if (x > 1) {}")
finally:
    br.optional.require = _real_require

check("non-python language: refused with validation code",
      _refused_lang.get("ok") is False and _refused_lang.get("code") == "validation",
      f"-> {_refused_lang}")
check("non-python language: z3 (optional.require) was never touched",
      _spawned["called"] is False)
check("non-python language: no verdict/branches key at all (nothing ran)",
      "branches" not in _refused_lang and "verdict" not in _refused_lang)


# ── max_branches cap discloses truncated ────────────────────────────────────
_lines = ["def f(x):"]
for _i in range(10):
    _lines.append(f"    if x == {_i}:")
    _lines.append("        x = x + 1")
_MANY_BRANCHES = "\n".join(_lines) + "\n    return x\n"
_rt = br.analyze("python3", _MANY_BRANCHES, max_branches=3)
check("max_branches=3: exactly 3 branches recorded", len(_rt["branches"]) == 3,
      f"-> {len(_rt['branches'])}")
check("max_branches=3: truncated is True", _rt.get("truncated") is True, f"-> {_rt}")
_rt_full = br.analyze("python3", _MANY_BRANCHES, max_branches=64)
check("max_branches=64 (>= the 10 real branches): truncated is False, all 10 recorded",
      _rt_full.get("truncated") is False and len(_rt_full["branches"]) == 10,
      f"-> truncated={_rt_full.get('truncated')} n={len(_rt_full['branches'])}")


# ── timeout -> unknown, never a crash ───────────────────────────────────────
# The solver deadline is forced already-expired at Ctx construction time —
# the cheapest reproducible way to exercise "budget ran out mid-analysis"
# without depending on this host's own z3 being slow enough to time out for
# real. Discovery (the AST walk itself) still runs to completion; only the
# per-branch SOLVE degrades — see branch_reachability._Ctx.budget_exhausted.
import time as _time

_orig_ctx_init = br._Ctx.__init__


def _expired_ctx_init(self, z3mod, params, timeout_s, max_branches):
    _orig_ctx_init(self, z3mod, params, timeout_s, max_branches)
    self.deadline = _time.monotonic() - 1.0


br._Ctx.__init__ = _expired_ctx_init
try:
    _r_expired = br.analyze("python3", _CLASSIFY)
finally:
    br._Ctx.__init__ = _orig_ctx_init

check("expired budget: the call still returns ok=True, never a crash/exception",
      _r_expired.get("ok") is True, f"-> {_r_expired}")
check("expired budget: every discovered branch is present but verdict unknown",
      len(_r_expired["branches"]) == 3 and all(b["verdict"] == "unknown" for b in _r_expired["branches"]),
      f"-> {[(b['line'], b['verdict']) for b in _r_expired['branches']]}")
check("expired budget: unknown_count matches, no false reachable/dead claims",
      _r_expired.get("unknown_count") == 3 and _r_expired.get("reachable_count") == 0
      and _r_expired.get("dead_count") == 0, f"-> {_r_expired}")
check("expired budget: `supported` stays True (a timeout is not an unsupported "
      "construct)", _r_expired.get("supported") is True, f"-> {_r_expired.get('supported')}")


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL BRANCH_REACHABILITY TESTS PASS ===")
for _f in FAILS:
    print(f"  {_f}")
sys.exit(1 if FAILS else 0)
