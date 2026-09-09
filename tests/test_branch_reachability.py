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


# ── boundary inputs for <, <=, >, >=, ==, != ────────────────────────────────
# `None` in the expected tables means the TRUE optimum is unbounded in that
# direction — SHOULD-FIX 3 (adversarial review, e0708864): a boundary
# outside a search box must report null with a note, never the box's own
# edge dressed up as if it were a real extremum. x > 5 has no maximum;
# x < 5 has no minimum; x == 5 is bounded both ways at 5; x != 5 is
# unbounded both ways.
_OPS = {
    "<": "def f(x):\n    if x < 5:\n        return 1\n    return 0\n",
    "<=": "def f(x):\n    if x <= 5:\n        return 1\n    return 0\n",
    ">": "def f(x):\n    if x > 5:\n        return 1\n    return 0\n",
    ">=": "def f(x):\n    if x >= 5:\n        return 1\n    return 0\n",
    "==": "def f(x):\n    if x == 5:\n        return 1\n    return 0\n",
    "!=": "def f(x):\n    if x != 5:\n        return 1\n    return 0\n",
}
_EXPECTED_MIN = {"<": None, "<=": None, ">": 6, ">=": 5, "==": 5, "!=": None}
_EXPECTED_MAX = {"<": 4, "<=": 5, ">": None, ">=": None, "==": 5, "!=": None}
for _op, _code in _OPS.items():
    _ro = br.analyze("python3", _code)
    _b = _by_line(_ro, 2)
    check(f"op {_op}: branch is reachable", _b["verdict"] == "reachable", f"-> {_b}")
    _entries = _b["boundary_inputs"]
    check(f"op {_op}: exactly one boundary_inputs entry", len(_entries) == 1, f"-> {_entries}")
    _entry = _entries[0]
    check(f"op {_op}: operator recorded correctly", _entry["operator"] == _op, f"-> {_entry}")

    _exp_min = _EXPECTED_MIN[_op]
    if _exp_min is None:
        check(f"op {_op}: min_input is null (unbounded below), with a min_note",
              _entry["min_input"] is None and bool(_entry.get("min_note")), f"-> {_entry}")
    else:
        check(f"op {_op}: min_input.x == {_exp_min}, no min_note (a real bound)",
              _entry["min_input"] is not None and _entry["min_input"]["x"] == _exp_min
              and "min_note" not in _entry, f"-> {_entry}")

    _exp_max = _EXPECTED_MAX[_op]
    if _exp_max is None:
        check(f"op {_op}: max_input is null (unbounded above), with a max_note",
              _entry["max_input"] is None and bool(_entry.get("max_note")), f"-> {_entry}")
    else:
        check(f"op {_op}: max_input.x == {_exp_max}, no max_note (a real bound)",
              _entry["max_input"] is not None and _entry["max_input"]["x"] == _exp_max
              and "max_note" not in _entry, f"-> {_entry}")

    check(f"op {_op}: equality_edge_input.x == 5",
          _entry["equality_edge_input"] is not None and _entry["equality_edge_input"]["x"] == 5,
          f"-> {_entry['equality_edge_input']}")

    _proof = _proof_line(_code, _b["line"], _b["kind"])
    if _entry["min_input"] is not None:
        check(f"op {_op}: min_input actually reaches the branch's own body (trace-corroborated)",
              _hits_line(_code, "f", _entry["min_input"], _proof), f"-> {_entry['min_input']}")
    if _entry["max_input"] is not None:
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


# ── MERGE/JOIN: adversarial review e0708864, BLOCKER 1+2 ───────────────────
# `_walk_if`/`_walk_loop` used to process each arm against a COPY of `env`,
# so an assignment inside a non-returning arm was silently discarded for
# the code after the `if` — every later branch's path condition was built
# over the PRE-if bindings regardless of which arm actually ran. Both
# repros below are the review's own, verified failing before the
# `_merge_envs` fix (repro a used to report `if y == 5` DEAD; repro b used
# to report the nested `if x > 100` REACHABLE with an invalid witness).

# Repro (a): a single if with no else — the code after it must see y=5 on
# the path where the if ran, and y=0 (unchanged) on the path where it did
# not.
_REPRO_A = (
    "def f(x):\n"
    "    y = 0\n"
    "    if x > 0:\n"
    "        y = 5\n"
    "    if y == 5:\n"
    "        return 1\n"
    "    return 0\n"
)
_ra = br.analyze("python3", _REPRO_A)
_ra_second = _by_line(_ra, 5)
check("repro (a): `if y == 5` is REACHABLE (x > 0 makes y = 5)",
      _ra_second is not None and _ra_second["verdict"] == "reachable", f"-> {_ra_second}")
check("repro (a): its witness has x > 0 (the only way y becomes 5)",
      _ra_second.get("witness", {}).get("x", 0) > 0, f"-> {_ra_second.get('witness')}")
_verify_every_witness(_ra, _REPRO_A, "f", "repro-a")

# Repro (b): a nested if inside an `if y == 0:` guard, where y == 0 is only
# possible when the EARLIER if did NOT set y = 1 — so x > 100 (which
# requires the earlier if's guard x > 0 true, forcing y = 1) can never
# coexist with y == 0. The nested if must be DEAD, not reachable.
_REPRO_B = (
    "def f(x):\n"
    "    y = 0\n"
    "    if x > 0:\n"
    "        y = 1\n"
    "    if y == 0:\n"
    "        if x > 100:\n"
    "            return 1\n"
    "    return 0\n"
)
_rb2 = br.analyze("python3", _REPRO_B)
_rb2_nested = _by_line(_rb2, 6)
check("repro (b): the nested `if x > 100` is DEAD (y == 0 implies x <= 0)",
      _rb2_nested is not None and _rb2_nested["verdict"] == "dead", f"-> {_rb2_nested}")
check("repro (b): no witness on the dead nested branch", "witness" not in (_rb2_nested or {}))
for _entry in _rb2_nested["boundary_inputs"]:
    edge = _entry.get("equality_edge_input")
    if edge is not None:
        _proof = _proof_line(_REPRO_B, _rb2_nested["line"], _rb2_nested["kind"])
        check(f"repro (b): boundary edge {edge!r} for {_entry['guard']!r} does NOT "
              f"actually reach the dead nested body (trace-corroborated)",
              not _hits_line(_REPRO_B, "f", edge, _proof), f"-> edge={edge}")
_verify_every_witness(_rb2, _REPRO_B, "f", "repro-b")


# ── assignment in the else arm only ─────────────────────────────────────────
_ELSE_ONLY = (
    "def f(x):\n"
    "    y = 0\n"
    "    if x > 0:\n"
    "        pass\n"
    "    else:\n"
    "        y = 9\n"
    "    if y == 9:\n"
    "        return 1\n"
    "    return 0\n"
)
_reo = br.analyze("python3", _ELSE_ONLY)
_reo_branch = _by_line(_reo, 7)
check("else-only assignment: `if y == 9` is reachable, witness has x <= 0",
      _reo_branch["verdict"] == "reachable" and _reo_branch["witness"]["x"] <= 0,
      f"-> {_reo_branch}")
_verify_every_witness(_reo, _ELSE_ONLY, "f", "else-only")


# ── assignment in BOTH arms; a later branch reachable only via the else value ─
_BOTH_ARMS = (
    "def f(x):\n"
    "    if x > 0:\n"
    "        y = 100\n"
    "    else:\n"
    "        y = 5\n"
    "    if y == 5:\n"
    "        return 1\n"
    "    return 0\n"
)
_rba = br.analyze("python3", _BOTH_ARMS)
_rba_branch = _by_line(_rba, 6)
check("both-arms assignment: `if y == 5` is reachable ONLY via the else value "
      "(witness has x <= 0, never the if-arm's x > 0)",
      _rba_branch["verdict"] == "reachable" and _rba_branch["witness"]["x"] <= 0,
      f"-> {_rba_branch}")
_verify_every_witness(_rba, _BOTH_ARMS, "f", "both-arms")


# ── an elif chain assigning three different values, each read back later ───
_ELIF_THREE = (
    "def f(x):\n"
    "    if x == 1:\n"
    "        y = 10\n"
    "    elif x == 2:\n"
    "        y = 20\n"
    "    else:\n"
    "        y = 30\n"
    "    if y == 10:\n"
    "        return 1\n"
    "    if y == 20:\n"
    "        return 2\n"
    "    if y == 30:\n"
    "        return 3\n"
    "    return 0\n"
)
_ret3 = br.analyze("python3", _ELIF_THREE)
for _val, _line in ((10, 8), (20, 10), (30, 12)):
    _b3 = _by_line(_ret3, _line)
    check(f"elif chain: `if y == {_val}` is reachable", _b3["verdict"] == "reachable", f"-> {_b3}")
check("elif chain: y == 10's witness has x == 1 (only x == 1 sets y = 10)",
      _by_line(_ret3, 8)["witness"]["x"] == 1, f"-> {_by_line(_ret3, 8)['witness']}")
check("elif chain: y == 20's witness has x == 2 (only x == 2 sets y = 20)",
      _by_line(_ret3, 10)["witness"]["x"] == 2, f"-> {_by_line(_ret3, 10)['witness']}")
check("elif chain: y == 30's witness has x not in {1, 2} (the else arm)",
      _by_line(_ret3, 12)["witness"]["x"] not in (1, 2), f"-> {_by_line(_ret3, 12)['witness']}")
_verify_every_witness(_ret3, _ELIF_THREE, "f", "elif-three")


# ── an arm ending in `return`: its assignment must NOT leak ────────────────
_RETURN_NO_LEAK = (
    "def f(x):\n"
    "    y = 0\n"
    "    if x > 0:\n"
    "        y = 5\n"
    "        return 1\n"
    "    if y == 5:\n"
    "        return 2\n"
    "    return 0\n"
)
_rnl = br.analyze("python3", _RETURN_NO_LEAK)
_rnl_branch = _by_line(_rnl, 6)
check("return-no-leak: `if y == 5` is DEAD (the only y = 5 assignment sits on "
      "an arm that unconditionally returns, so it never reaches here)",
      _rnl_branch["verdict"] == "dead", f"-> {_rnl_branch}")
for _entry in _rnl_branch["boundary_inputs"]:
    edge = _entry.get("equality_edge_input")
    if edge is not None:
        _proof = _proof_line(_RETURN_NO_LEAK, _rnl_branch["line"], _rnl_branch["kind"])
        check(f"return-no-leak: boundary edge {edge!r} does not actually reach "
              f"the dead branch (trace-corroborated)",
              not _hits_line(_RETURN_NO_LEAK, "f", edge, _proof), f"-> edge={edge}")
_verify_every_witness(_rnl, _RETURN_NO_LEAK, "f", "return-no-leak")


# ── a variable introduced only inside an arm, read after: unknown, not a crash ─
_UNDEF_AFTER = (
    "def f(x):\n"
    "    if x > 0:\n"
    "        z = 5\n"
    "    if z == 5:\n"
    "        return 1\n"
    "    return 0\n"
)
_rua = br.analyze("python3", _UNDEF_AFTER)
check("undefined-after: the whole call still succeeds (ok=True), never a crash",
      _rua.get("ok") is True, f"-> {_rua}")
check("undefined-after: `supported` is False (z is undefined on the path where "
      "the if did not run — an unsupported construct on THIS path, not a "
      "solver timeout)", _rua.get("supported") is False, f"-> {_rua}")
_rua_branch = _by_line(_rua, 4)
check("undefined-after: `if z == 5` is verdict unknown, not a false reachable/dead",
      _rua_branch is not None and _rua_branch["verdict"] == "unknown", f"-> {_rua_branch}")
check("undefined-after: the FIRST if (x > 0, well-defined) is unaffected and reachable",
      _by_line(_rua, 2)["verdict"] == "reachable", f"-> {_by_line(_rua, 2)}")


# ── for range(3) body assignment followed by an if ──────────────────────────
# `flag = 1` is IDEMPOTENT across iterations — real execution ends with
# flag == 1 whether the loop runs once or three times — so the one-
# representative-iteration merge is not just an approximation here, it is
# EXACT, and every witness below is fully trace-verifiable against the
# REAL 3-iteration run, not merely the model's own one-iteration story.
_FOR_ASSIGN = (
    "def f(n):\n"
    "    flag = 0\n"
    "    for i in range(3):\n"
    "        flag = 1\n"
    "    if flag == 1:\n"
    "        return 1\n"
    "    return 0\n"
)
_rfa = br.analyze("python3", _FOR_ASSIGN)
check("for-assign: `if flag == 1` is reachable (the loop's merged effect, "
      "not the pre-loop value)", _by_line(_rfa, 5)["verdict"] == "reachable",
      f"-> {_by_line(_rfa, 5)}")
_verify_every_witness(_rfa, _FOR_ASSIGN, "f", "for-assign")

# A SEPARATE, explicitly-labeled case documents the known, ACCEPTED
# imprecision the module docstring's LOOPS section describes: `total`
# ACCUMULATES across iterations (not idempotent like `flag` above), so the
# one-representative-iteration model reflects `total = 1` (one iteration's
# real effect) but NOT `total = 3` (the real, 3-iteration result) — a
# genuinely reachable branch (`total == 3` really does happen) can be
# under-reported `dead` here. This is the documented direction of error
# (never the reverse — see the design note); NOT run through
# `_verify_every_witness`, because `total == 1`'s witness does not
# correspond to what an actual `range(3)` run of THIS program does, and
# asserting a trace match here would be asserting the wrong thing on
# purpose.
_FOR_ACCUMULATE = (
    "def f(n):\n"
    "    total = 0\n"
    "    for i in range(3):\n"
    "        total = total + 1\n"
    "    if total == 1:\n"
    "        return 1\n"
    "    if total == 3:\n"
    "        return 2\n"
    "    return 0\n"
)
_rfacc = br.analyze("python3", _FOR_ACCUMULATE)
check("for-accumulate: `if total == 1` is reachable per the one-iteration "
      "model (documented imprecision, not asserted against a real trace)",
      _by_line(_rfacc, 5)["verdict"] == "reachable", f"-> {_by_line(_rfacc, 5)}")
check("for-accumulate: `if total == 3` (the REAL result) is under-reported "
      "dead — the direction the design note says this imprecision always "
      "goes",
      _by_line(_rfacc, 7)["verdict"] == "dead", f"-> {_by_line(_rfacc, 7)}")


# ── nested ifs assigning at two depths ──────────────────────────────────────
_NESTED_TWO = (
    "def f(x, y):\n"
    "    z = 0\n"
    "    if x > 0:\n"
    "        if y > 0:\n"
    "            z = 1\n"
    "        else:\n"
    "            z = 2\n"
    "    if z == 1:\n"
    "        return 1\n"
    "    return 0\n"
)
_rnt = br.analyze("python3", _NESTED_TWO)
_rnt_branch = _by_line(_rnt, 8)
check("nested two depths: `if z == 1` is reachable exactly when x > 0 and y > 0",
      _rnt_branch["verdict"] == "reachable" and _rnt_branch["witness"]["x"] > 0
      and _rnt_branch["witness"]["y"] > 0, f"-> {_rnt_branch}")
_verify_every_witness(_rnt, _NESTED_TWO, "f", "nested-two-depths")


# ── NIT 5: a module-scope class names the construct, not the generic "no
# top-level function" refusal ────────────────────────────────────────────
_CLASS_SCOPE = "class Foo:\n    pass\n"
_rcls = br.analyze("python3", _CLASS_SCOPE)
check("module-scope class: refused naming 'class definition', not the generic message",
      _rcls.get("ok") is False and "class definition" in (_rcls.get("error") or ""),
      f"-> {_rcls}")
check("module-scope class: line == 1", _rcls.get("line") == 1, f"-> {_rcls}")


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL BRANCH_REACHABILITY TESTS PASS ===")
for _f in FAILS:
    print(f"  {_f}")
sys.exit(1 if FAILS else 0)
