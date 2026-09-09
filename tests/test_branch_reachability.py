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

# A SEPARATE case confirms that a `for` loop WITHIN the unroll cap is now
# EXACT even for an ACCUMULATOR (not merely an idempotent assignment like
# `flag` above) — `total` genuinely reaches 3 after three real iterations,
# and unrolling sees that directly rather than approximating it as one
# iteration's effect. Before the loop-soundness fix (confirmation review on
# d163c8f) this exact program was the one used to document a "known,
# accepted imprecision" that no longer exists for anything within the cap
# — see `_ABOVE_CAP`/`_WHILE_ACCUM` below for where that imprecision still
# genuinely applies (above the cap, and for `while`).
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
check("for-accumulate (within the unroll cap): `if total == 1` is DEAD — "
      "the loop always runs all 3 iterations, total is never left at 1",
      _by_line(_rfacc, 5)["verdict"] == "dead", f"-> {_by_line(_rfacc, 5)}")
check("for-accumulate (within the unroll cap): `if total == 3` (the REAL, "
      "exact result) is reachable",
      _by_line(_rfacc, 7)["verdict"] == "reachable", f"-> {_by_line(_rfacc, 7)}")
_verify_every_witness(_rfacc, _FOR_ACCUMULATE, "f", "for-accumulate")


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


# ── LOOP SOUNDNESS: confirmation-review BLOCKER on d163c8f ─────────────────
# `_walk_loop`'s old single-mechanism approach bound the loop variable to a
# fresh, RANGE-CONSTRAINED-but-otherwise-free symbol, walked the body once,
# then MERGED the result — which made a variable that is only ever "the
# last iteration's value" look like "any value in the range" for the code
# after the loop. The review's own repro, verified failing before the fix
# (`_merge_envs`/taint rewrite): `if x == 5` reported REACHABLE with witness
# `{}`, when `f()` is fully deterministic (zero parameters) and always has
# `x == 9` there — a false `reachable`, the one error class this tool
# promises never to produce.
_LOOP_REPRO = (
    "def f():\n"
    "    x = -1\n"
    "    for i in range(0, 10):\n"
    "        x = i\n"
    "    if x == 5:\n"
    "        return 1\n"
    "    return 0\n"
)
_rlr = br.analyze("python3", _LOOP_REPRO)
_rlr_branch = _by_line(_rlr, 5)
check("loop repro: `if x == 5` is DEAD (x is always 9 after the loop, "
      "never 5 — the exact confirmation-review repro)",
      _rlr_branch["verdict"] == "dead", f"-> {_rlr_branch}")
for _entry in _rlr_branch["boundary_inputs"]:
    edge = _entry.get("equality_edge_input")
    if edge is not None:
        _proof = _proof_line(_LOOP_REPRO, _rlr_branch["line"], _rlr_branch["kind"])
        check(f"loop repro: boundary edge {edge!r} does not actually reach the "
              f"dead branch (trace-corroborated)",
              not _hits_line(_LOOP_REPRO, "f", edge, _proof), f"-> edge={edge}")

# The positive control: `if x == 9` (the REAL post-loop value) must be
# reachable, with a real (here, trivially empty — zero parameters) witness,
# proving unrolling gives the EXACT post-loop value, not merely "not the
# wrong one".
_LOOP_REPRO_POS = _LOOP_REPRO.replace("x == 5", "x == 9")
_rlrp = br.analyze("python3", _LOOP_REPRO_POS)
_rlrp_branch = _by_line(_rlrp, 5)
check("loop repro positive control: `if x == 9` (the real post-loop value) "
      "is reachable", _rlrp_branch["verdict"] == "reachable", f"-> {_rlrp_branch}")
_verify_every_witness(_rlrp, _LOOP_REPRO_POS, "f", "loop-repro-positive")


# ── a range of 0: the loop is dead, post-loop state is the PRE-loop value ──
_RANGE_ZERO = (
    "def f():\n"
    "    x = 7\n"
    "    for i in range(0):\n"
    "        x = i\n"
    "    if x == 7:\n"
    "        return 1\n"
    "    return 0\n"
)
_rz = br.analyze("python3", _RANGE_ZERO)
check("range(0): the for-line is dead", _by_line(_rz, 3)["verdict"] == "dead", f"-> {_by_line(_rz, 3)}")
check("range(0): the loop never ran, so x is still 7 afterward (reachable)",
      _by_line(_rz, 5)["verdict"] == "reachable", f"-> {_by_line(_rz, 5)}")
_verify_every_witness(_rz, _RANGE_ZERO, "f", "range-zero")


# ── inner if reachable only at one specific unrolled iteration ─────────────
_INNER_IF_ITER3 = (
    "def f():\n"
    "    y = 0\n"
    "    for i in range(5):\n"
    "        if i == 3:\n"
    "            y = 1\n"
    "    if y == 1:\n"
    "        return 1\n"
    "    return 0\n"
)
_rii = br.analyze("python3", _INNER_IF_ITER3)
check("inner-if-iter3: the inner `if i == 3` is reachable (unrolling checks "
      "it at every concrete i, including 3)",
      _by_line(_rii, 4)["verdict"] == "reachable", f"-> {_by_line(_rii, 4)}")
check("inner-if-iter3: `if y == 1` after the loop is reachable (the merge of "
      "all 5 unrolled iterations correctly keeps the i==3 case's effect)",
      _by_line(_rii, 6)["verdict"] == "reachable", f"-> {_by_line(_rii, 6)}")
_verify_every_witness(_rii, _INNER_IF_ITER3, "f", "inner-if-iter3")


# ── above the unroll cap: post-loop read is unknown+reason; an in-body
# branch sat on the FIRST iteration is still reachable ─────────────────────
_ABOVE_CAP = (
    "def f(n):\n"
    "    total = 0\n"
    "    for i in range(100):\n"
    "        total = total + n\n"
    "    if total == 5:\n"
    "        return 1\n"
    "    return 0\n"
)
assert br._MAX_UNROLL_ITERATIONS < 100, "test assumes range(100) exceeds the unroll cap"
_rac = br.analyze("python3", _ABOVE_CAP)
_rac_for = _by_line(_rac, 3)
check("above-cap: the for-line's own reachability is unaffected by the cap "
      "(reachable, a real witness)",
      _rac_for["verdict"] == "reachable" and "witness" in _rac_for, f"-> {_rac_for}")
_verify_every_witness(_rac, _ABOVE_CAP, "f", "above-cap (for-line only)")
_rac_post = _by_line(_rac, 5)
check("above-cap: `if total == 5` (reads a value the loop computed) is "
      "unknown, NEVER dead or reachable, with the tainted-by-loop reason",
      _rac_post["verdict"] == "unknown"
      and _rac_post.get("reason") == br._REASON_TAINTED_BY_LOOP, f"-> {_rac_post}")
check("above-cap: the tainted branch carries no witness and no boundary_inputs",
      "witness" not in _rac_post and _rac_post["boundary_inputs"] == [], f"-> {_rac_post}")

_ABOVE_CAP_INNER = (
    "def f(n):\n"
    "    for i in range(100):\n"
    "        if n == 0:\n"
    "            return 1\n"
    "    return 0\n"
)
_raci = br.analyze("python3", _ABOVE_CAP_INNER)
_raci_inner = _by_line(_raci, 3)
check("above-cap: an in-body branch SAT on the representative (first) "
      "iteration is reachable — a real witness is a real witness regardless "
      "of which iteration produced it",
      _raci_inner["verdict"] == "reachable" and _raci_inner.get("witness", {}).get("n") == 0,
      f"-> {_raci_inner}")
_verify_every_witness(_raci, _ABOVE_CAP_INNER, "f", "above-cap-inner")


# ── while accumulator: neither dead nor reachable, tainted ─────────────────
_WHILE_ACCUM = (
    "def f():\n"
    "    total = 0\n"
    "    while total < 7:\n"
    "        total = total + 2\n"
    "    if total > 5:\n"
    "        return 1\n"
    "    return 0\n"
)
_rwa = br.analyze("python3", _WHILE_ACCUM)
_rwa_while = _by_line(_rwa, 3)
check("while-accumulator: the while's own line is reachable (its guard "
      "does not depend on anything the body computed YET)",
      _rwa_while["verdict"] == "reachable", f"-> {_rwa_while}")
_verify_every_witness(_rwa, _WHILE_ACCUM, "f", "while-accumulator (while-line only)")
_rwa_post = _by_line(_rwa, 5)
check("while-accumulator: `if total > 5` — total is REALLY 8 here, so this "
      "IS truly reachable, but this tool correctly refuses to claim either "
      "reachable or dead from a one-iteration model and reports unknown",
      _rwa_post["verdict"] == "unknown"
      and _rwa_post.get("reason") == br._REASON_TAINTED_BY_LOOP, f"-> {_rwa_post}")


# ── while whose inner branch is unsat on the first iteration: unknown,
# never dead ────────────────────────────────────────────────────────────────
# A COUNTER-BOUNDED while (not `while x > 0:` with nothing decrementing
# `x`, which never terminates for x > 0 and would hang the trace-
# corroboration run below) — `n < 3` guarantees real termination while
# still exercising the identical "unsat on the one checked iteration"
# mechanism.
_WHILE_UNSAT_FIRST = (
    "def f():\n"
    "    y = 0\n"
    "    n = 0\n"
    "    while n < 3:\n"
    "        if y == 999:\n"
    "            return 2\n"
    "        n = n + 1\n"
    "    return 0\n"
)
_rwuf = br.analyze("python3", _WHILE_UNSAT_FIRST)
_rwuf_while = _by_line(_rwuf, 4)
check("while-unsat-first: the while's own line is reachable",
      _rwuf_while["verdict"] == "reachable", f"-> {_rwuf_while}")
_verify_every_witness(_rwuf, _WHILE_UNSAT_FIRST, "f", "while-unsat-first (while-line only)")
_rwuf_inner = _by_line(_rwuf, 5)
check("while-unsat-first: `if y == 999` is unsat on the one iteration this "
      "tool checks (y is always 0 there), so it is UNKNOWN — never `dead` —"
      " with the first-iteration-only reason",
      _rwuf_inner["verdict"] == "unknown"
      and _rwuf_inner.get("reason") == br._REASON_FIRST_ITERATION_ONLY,
      f"-> {_rwuf_inner}")


# ── break inside a loop: refused, naming the construct ──────────────────────
_BREAK_LOOP = (
    "def f():\n"
    "    for i in range(5):\n"
    "        if i == 3:\n"
    "            break\n"
    "    return 0\n"
)
_rbrk = br.analyze("python3", _BREAK_LOOP)
check("break inside a loop: refused, naming 'break statement'",
      _rbrk.get("ok") is False and "break statement" in (_rbrk.get("error") or ""),
      f"-> {_rbrk}")
check("break inside a loop: line == 4", _rbrk.get("line") == 4, f"-> {_rbrk}")


# ── phi inside unrolling: an unrolled loop nested inside an if arm ─────────
_UNROLL_INSIDE_IF = (
    "def f(x):\n"
    "    total = 0\n"
    "    if x > 0:\n"
    "        for i in range(3):\n"
    "            total = i\n"
    "    if total == 2:\n"
    "        return 1\n"
    "    return 0\n"
)
_ruii = br.analyze("python3", _UNROLL_INSIDE_IF)
_ruii_branch = _by_line(_ruii, 6)
check("unroll-inside-if: `if total == 2` is reachable ONLY via x > 0 (the "
      "unrolled loop's last-iteration value, i == 2, merged through the "
      "enclosing if's own phi)",
      _ruii_branch["verdict"] == "reachable" and _ruii_branch["witness"]["x"] > 0,
      f"-> {_ruii_branch}")
_verify_every_witness(_ruii, _UNROLL_INSIDE_IF, "f", "unroll-inside-if")


# ── UNROLLED-LOOP RETURN CLOSURE: third-review BLOCKER on 59ff14d ───────────
# `_walk_for_unrolled` used to hand-thread the environment across the N
# unrolled copies but feed EVERY copy the SAME, unnarrowed path condition,
# discarding each copy's own `falls_through`/`continuation_cond` — the
# THIRD instance of "a copy's own control flow is not propagated," after
# the if/else merge (e0708864) and the loop-value taint (d163c8f) fixes.
# The review's own repro, verified failing before this fix: every real
# call returns at `i == 2`, so the loop body never even reaches `i == 4`,
# but the tool reported the post-loop `if y == 99` REACHABLE with a
# witness.
_UNROLL_RETURN_REPRO = (
    "def f(x):\n"
    "    y = 0\n"
    "    for i in range(5):\n"
    "        if i == 2:\n"
    "            return 100\n"
    "        if i == 4:\n"
    "            y = 99\n"
    "    if y == 99:\n"
    "        return 1\n"
    "    return 0\n"
)
_rurr = br.analyze("python3", _UNROLL_RETURN_REPRO)
_rurr_i2 = _by_line(_rurr, 4)
_rurr_i4 = _by_line(_rurr, 6)
_rurr_post = _by_line(_rurr, 8)
check("unroll-return repro: `if i == 2` (the return arm) is reachable",
      _rurr_i2["verdict"] == "reachable", f"-> {_rurr_i2}")
check("unroll-return repro: `if i == 4` is DEAD (the loop never gets past "
      "i == 2 — every input returns there first)",
      _rurr_i4["verdict"] == "dead", f"-> {_rurr_i4}")
check("unroll-return repro: the post-loop `if y == 99` is DEAD — the "
      "review's own repro, previously a false reachable with a "
      "fabricated witness",
      _rurr_post["verdict"] == "dead", f"-> {_rurr_post}")
for _branch in (_rurr_i4, _rurr_post):
    for _entry in _branch["boundary_inputs"]:
        edge = _entry.get("equality_edge_input")
        if edge is not None:
            _proof = _proof_line(_UNROLL_RETURN_REPRO, _branch["line"], _branch["kind"])
            check(f"unroll-return repro: boundary edge {edge!r} on line "
                  f"{_branch['line']} does NOT actually reach that dead "
                  f"branch (trace-corroborated)",
                  not _hits_line(_UNROLL_RETURN_REPRO, "f", edge, _proof), f"-> edge={edge}")
_verify_every_witness(_rurr, _UNROLL_RETURN_REPRO, "f", "unroll-return-repro")


# ── an input-dependent return partway through unrolling ────────────────────
_UNROLL_RETURN_COND = (
    "def f(x):\n"
    "    y = 0\n"
    "    for i in range(5):\n"
    "        if i == 2 and x > 0:\n"
    "            return 100\n"
    "        if i == 4:\n"
    "            y = 99\n"
    "    if y == 99:\n"
    "        return 1\n"
    "    return 0\n"
)
_rurc = br.analyze("python3", _UNROLL_RETURN_COND)
_rurc_post = _by_line(_rurc, 8)
check("unroll-return conditional: the post-loop `if y == 99` is reachable "
      "ONLY when x <= 0 (x > 0 always returns at i == 2 first)",
      _rurc_post["verdict"] == "reachable" and _rurc_post["witness"]["x"] <= 0,
      f"-> {_rurc_post}")
_verify_every_witness(_rurc, _UNROLL_RETURN_COND, "f", "unroll-return-cond")


# ── return at iteration 0: everything after is dead ─────────────────────────
_UNROLL_RETURN_ITER0 = (
    "def f(x):\n"
    "    y = 0\n"
    "    for i in range(3):\n"
    "        if i == 0:\n"
    "            return 1\n"
    "        if i == 1:\n"
    "            y = 5\n"
    "    if y == 5:\n"
    "        return 2\n"
    "    return 0\n"
)
_ruri0 = br.analyze("python3", _UNROLL_RETURN_ITER0)
check("unroll-return iter0: `if i == 0` is reachable",
      _by_line(_ruri0, 4)["verdict"] == "reachable", f"-> {_by_line(_ruri0, 4)}")
check("unroll-return iter0: `if i == 1` is dead (iteration 0 always returns first)",
      _by_line(_ruri0, 6)["verdict"] == "dead", f"-> {_by_line(_ruri0, 6)}")
check("unroll-return iter0: the post-loop `if y == 5` is dead",
      _by_line(_ruri0, 8)["verdict"] == "dead", f"-> {_by_line(_ruri0, 8)}")
_verify_every_witness(_ruri0, _UNROLL_RETURN_ITER0, "f", "unroll-return-iter0")


# ── return at the LAST iteration: post-loop dead, earlier arms unaffected ──
_UNROLL_RETURN_LAST = (
    "def f(x):\n"
    "    y = 0\n"
    "    for i in range(3):\n"
    "        if i == 0:\n"
    "            y = 5\n"
    "        if i == 2:\n"
    "            return 1\n"
    "    if y == 5:\n"
    "        return 2\n"
    "    return 0\n"
)
_rurl = br.analyze("python3", _UNROLL_RETURN_LAST)
check("unroll-return last-iter: `if i == 0` (an earlier arm) is unaffected "
      "— still reachable",
      _by_line(_rurl, 4)["verdict"] == "reachable", f"-> {_by_line(_rurl, 4)}")
check("unroll-return last-iter: `if i == 2` (the return, at the LAST "
      "iteration) is reachable",
      _by_line(_rurl, 6)["verdict"] == "reachable", f"-> {_by_line(_rurl, 6)}")
check("unroll-return last-iter: the post-loop `if y == 5` is dead (the "
      "last iteration always returns before falling out of the loop)",
      _by_line(_rurl, 8)["verdict"] == "dead", f"-> {_by_line(_rurl, 8)}")
_verify_every_witness(_rurl, _UNROLL_RETURN_LAST, "f", "unroll-return-last")


# ── a return nested two ifs deep inside the unrolled loop body ─────────────
_UNROLL_RETURN_NESTED = (
    "def f(x):\n"
    "    for i in range(3):\n"
    "        if i == 1:\n"
    "            if x > 0:\n"
    "                return 1\n"
    "    return 0\n"
)
_rurn = br.analyze("python3", _UNROLL_RETURN_NESTED)
_rurn_inner = _by_line(_rurn, 4)
check("unroll-return nested: the doubly-nested `if x > 0` (inside `if i "
      "== 1`, inside the unrolled loop) is reachable, witness x > 0",
      _rurn_inner["verdict"] == "reachable" and _rurn_inner["witness"]["x"] > 0,
      f"-> {_rurn_inner}")
_verify_every_witness(_rurn, _UNROLL_RETURN_NESTED, "f", "unroll-return-nested")


# ── differential-fuzz find #1: id()-reuse in _mentions_tainted's DAG walk ──
# z3's Python bindings mint a FRESH wrapper object on every `.children()`
# call rather than interning one per underlying (hash-consed) node, so a
# wrapper can be garbage-collected and its `id()` reused by an unrelated
# LATER node within the SAME walk. `_mentions_tainted` used to key its
# "already visited" set on `id(node)`; when that happened, a tainted
# symbol nested inside a z3.If's second branch could be skipped as an
# already-"seen" duplicate of a totally different node, laundering a
# genuinely-tainted guard into an ordinary `reachable` verdict with a
# fabricated witness. `tests/test_branch_reachability_differential.py`
# caught this non-deterministically (it depends on GC timing); this is
# the minimized, deterministic repro, fixed by keying on `node.get_id()`
# (z3's own id, stable across every wrapper around the same node) instead.
_TAINT_ID_REUSE = (
    "def f(x, y):\n"
    "    if x < 0:\n"
    "        n1 = 0\n"
    "        while n1 < 2:\n"
    "            x = ((x - x) - 1)\n"
    "            n1 = n1 + 1\n"
    "    else:\n"
    "        if (((y + -3) + (x + y)) >= ((3 * -3) + y) and y == (x * -4)):\n"
    "            v2 = x\n"
    "        else:\n"
    "            y = 1\n"
    "        y = -2\n"
    "    if y >= (x - 6):\n"
    "        return 1\n"
    "    return 0\n"
)
_tir = br.analyze("python3", _TAINT_ID_REUSE)
_tir_last = _by_line(_tir, 13)
check("taint-id-reuse: `if y >= (x - 6)` — downstream of a while-tainted "
      "`x` merged into an if/else — is `unknown`, not a fabricated "
      "`reachable`",
      _tir_last is not None and _tir_last["verdict"] == "unknown",
      f"-> {_tir_last}")


# ── differential-fuzz find #2: a tainted-guard `return` didn't close off
# what came after it ────────────────────────────────────────────────────
# A tainted (or untranslatable) if/elif/else guard is an opaque
# pass-through by design: neither arm is walked, so this tool cannot tell
# whether one of them held an unconditional `return`. The environment was
# correctly left unchanged either way, but CONTROL FLOW was not: code
# after such a pass-through was treated as certainly falling through, when
# in reality an unresolved `return` earlier could have closed it off —
# `tests/test_branch_reachability_differential.py` found a witness that,
# run for real, hit an earlier `return` first and never reached the branch
# it was supposedly proving reachable. Fixed by `ctx._unresolved_closure_
# depth`: `_decide` now reports `unknown` (never a `reachable` witness)
# for anything sequentially after such a pass-through in the same block —
# an `unsat` there still safely proves `dead`, since dropping a required
# conjunct only widens what solves.
_CLOSURE_TAINT_RETURN = (
    "def f(x, y):\n"
    "    n1 = 0\n"
    "    while n1 < 4:\n"
    "        if y != y:\n"
    "            x = x\n"
    "        n1 = n1 + 1\n"
    "    if x != ((-3 * -2) * 0):\n"
    "        if not (x == -3):\n"
    "            x = y\n"
    "    if (4 - x) != ((y - x) + (y - y)):\n"
    "        return 2\n"
    "    if y != ((5 * 1) * -4):\n"
    "        return 3\n"
    "    return 0\n"
)
_ctr = br.analyze("python3", _CLOSURE_TAINT_RETURN)
_ctr_last = _by_line(_ctr, 12)
check("closure-taint-return: `if y != -20` — downstream of an unconditional "
      "`return` gated on a tainted-adjacent guard — is `unknown`, not a "
      "fabricated `reachable`",
      _ctr_last is not None and _ctr_last["verdict"] == "unknown"
      and _ctr_last.get("reason") == br._REASON_AFTER_UNRESOLVED_RETURN,
      f"-> {_ctr_last}")


# ── fourth review's BLOCKER: `_walk_loop_conservative` hardcoded
# `falls_through = True` regardless of what the one-iteration walk itself
# said, so an unconditional `return` in a `while` (or a `for` above the
# unroll cap) never narrowed the path condition after the loop ──────────

# Exact repro 1: an unconditional `return` inside a `while` whose entry is
# ALWAYS taken (`n = 0; while n < 4`) means entering the loop at all
# means returning — case (a), EXACT closure.
_WHILE_UNCOND_RETURN_CLOSES = (
    "def f(x):\n"
    "    if x == 1:\n"
    "        n = 0\n"
    "        while n < 4:\n"
    "            return 6\n"
    "    if x == 1:\n"
    "        return 999\n"
    "    return 0\n"
)
_wucr = br.analyze("python3", _WHILE_UNCOND_RETURN_CLOSES)
check("while-uncond-return-closes: the SECOND `if x == 1` is dead — every "
      "call with x == 1 returns 6 at the while, never gets there",
      _by_line(_wucr, 6)["verdict"] == "dead", f"-> {_by_line(_wucr, 6)}")
_verify_every_witness(_wucr, _WHILE_UNCOND_RETURN_CLOSES, "f", "while-uncond-return-closes")

# Exact repro 2: same shape, a `for` ABOVE `_MAX_UNROLL_ITERATIONS` (so it
# goes through the SAME conservative mechanism, not the unroll one).
_FOR_ABOVE_CAP_UNCOND_RETURN_CLOSES = (
    "def f(x):\n"
    "    for i in range(40):\n"
    "        return 9\n"
    "    if x == 1:\n"
    "        return 999\n"
    "    return 0\n"
)
assert br._MAX_UNROLL_ITERATIONS < 40, "this repro needs the conservative (above-cap) path, not the unroll one"
_facr = br.analyze("python3", _FOR_ABOVE_CAP_UNCOND_RETURN_CLOSES)
check("for-above-cap-uncond-return-closes: `if x == 1` is dead — "
      "`range(40)` always enters and its body always returns first",
      _by_line(_facr, 4)["verdict"] == "dead", f"-> {_by_line(_facr, 4)}")
_verify_every_witness(_facr, _FOR_ABOVE_CAP_UNCOND_RETURN_CLOSES, "f",
                       "for-above-cap-uncond-return-closes")

# Case (b): the body falls through on the one representative iteration but
# CONTAINS a `return` (input-dependent) — later, unmodeled iterations
# might take it, so anything after must be `unknown`, never a fabricated
# `reachable` — EXCEPT a branch that is dead regardless (contradicts the
# one iteration's own necessary "didn't return" condition), which must
# still correctly come back `dead`.
_WHILE_COND_RETURN_POSTLOOP = (
    "def f(x):\n"
    "    n = 0\n"
    "    while n < 3:\n"
    "        if x > 0:\n"
    "            return 1\n"
    "        n = n + 1\n"
    "    if x == 100:\n"
    "        return 2\n"
    "    if x == -5:\n"
    "        return 3\n"
    "    return 0\n"
)
_wcrp = br.analyze("python3", _WHILE_COND_RETURN_POSTLOOP)
check("while-cond-return-postloop: `if x == 100` is dead — contradicts the "
      "one-iteration walk's own necessary `not (x > 0)`",
      _by_line(_wcrp, 7)["verdict"] == "dead", f"-> {_by_line(_wcrp, 7)}")
check("while-cond-return-postloop: `if x == -5` is `unknown`, not a "
      "fabricated `reachable` — a later, unmodeled iteration might have "
      "returned first",
      _by_line(_wcrp, 9)["verdict"] == "unknown", f"-> {_by_line(_wcrp, 9)}")
_verify_every_witness(_wcrp, _WHILE_COND_RETURN_POSTLOOP, "f", "while-cond-return-postloop")
_ns_wcrp: dict = {}
exec(compile(_WHILE_COND_RETURN_POSTLOOP, "<t>", "exec"), _ns_wcrp)  # exec is a global ruff-ignore (S102); check_no_eval.py is the real gate
check("while-cond-return-postloop: ground truth — f(1) == 1 (conditional "
      "return fires) and f(100) != 2 (the dead line's own return value "
      "never comes back; x == 100 > 0 also fires the SAME conditional "
      "return at the while, for a different reason than x == 1 does — "
      "either way it never reaches the dead line)",
      _ns_wcrp["f"](1) == 1 and _ns_wcrp["f"](100) != 2,
      f"-> f(1)={_ns_wcrp['f'](1)} f(100)={_ns_wcrp['f'](100)}")

# Same case (b), but for a `for` ABOVE the unroll cap.
_FOR_ABOVE_CAP_COND_RETURN = (
    "def f(x):\n"
    "    for i in range(40):\n"
    "        if x > 0:\n"
    "            return 1\n"
    "    if x == 5:\n"
    "        return 2\n"
    "    if x == -3:\n"
    "        return 4\n"
    "    return 0\n"
)
_facrd = br.analyze("python3", _FOR_ABOVE_CAP_COND_RETURN)
check("for-above-cap-cond-return: `if x == 5` is dead — `x == 5` "
      "contradicts the necessary `not (x > 0)`",
      _by_line(_facrd, 5)["verdict"] == "dead", f"-> {_by_line(_facrd, 5)}")
check("for-above-cap-cond-return: `if x == -3` is `unknown`, not a "
      "fabricated `reachable`",
      _by_line(_facrd, 7)["verdict"] == "unknown", f"-> {_by_line(_facrd, 7)}")
_verify_every_witness(_facrd, _FOR_ABOVE_CAP_COND_RETURN, "f", "for-above-cap-cond-return")

# A `while` nested INSIDE an unrolled `for`, whose body returns
# UNCONDITIONALLY: every unrolled copy's own while always fires (case (a),
# EXACT), which must close not just that one copy but every LATER copy
# AND the code after the whole outer loop.
_NESTED_WHILE_IN_UNROLLED_FOR = (
    "def f(x):\n"
    "    for i in range(3):\n"
    "        n = 0\n"
    "        while n < 2:\n"
    "            return 5\n"
    "        y = 1\n"
    "    if x == 1:\n"
    "        return 2\n"
    "    return 0\n"
)
_nwuf = br.analyze("python3", _NESTED_WHILE_IN_UNROLLED_FOR)
check("nested-while-in-unrolled-for: post-loop `if x == 1` is dead — the "
      "outer loop's every copy always enters the inner `while`, which "
      "always returns first",
      _by_line(_nwuf, 7)["verdict"] == "dead", f"-> {_by_line(_nwuf, 7)}")
_verify_every_witness(_nwuf, _NESTED_WHILE_IN_UNROLLED_FOR, "f", "nested-while-in-unrolled-for")
_ns_nwuf: dict = {}
exec(compile(_NESTED_WHILE_IN_UNROLLED_FOR, "<t>", "exec"), _ns_nwuf)  # exec is a global ruff-ignore (S102); check_no_eval.py is the real gate
check("nested-while-in-unrolled-for: ground truth — every call returns 5 "
      "at the inner while on the very first outer iteration",
      all(_ns_nwuf["f"](v) == 5 for v in (-9, 0, 1, 2, 999)),
      f"-> {[_ns_nwuf['f'](v) for v in (-9, 0, 1, 2, 999)]}")

# The reviewer's own SEED=111 differential-corpus program, verbatim, as a
# standing regression (recovered by rerunning that seed's generator
# against the pre-fix `branch_reachability.py`, at commit 676f55f, and
# taking the first program that failed): the unconditional `return`
# inside the `if (x + (x + 3)) == 5:`-guarded `while` used to leave later
# code free to pick a witness (`x == 1`) that in reality returns `6` at
# the `while` and never gets there.
_SEED_111_LOOP_RETURN = (
    "def f(x):\n"
    "    if (x + (x + 3)) == 5:\n"
    "        n1 = 0\n"
    "        while n1 < 4:\n"
    "            return 6\n"
    "            n1 = n1 + 1\n"
    "    else:\n"
    "        v2 = -3\n"
    "        v2 = (x * 0)\n"
    "    if (4 * -1) != x:\n"
    "        if x <= ((x * 1) + x):\n"
    "            if (x >= x and ((x * 5) + x) != x):\n"
    "                x = x\n"
    "                v3 = ((x + x) + (x + -3))\n"
    "            else:\n"
    "                v4 = x\n"
    "            if x <= -5:\n"
    "                x = (-3 + x)\n"
    "            else:\n"
    "                return 2\n"
    "    return 0\n"
)
_s111 = br.analyze("python3", _SEED_111_LOOP_RETURN)
check("seed-111: `if x <= -5` is dead — contradicts the necessary `not "
      "(x + (x + 3) == 5)` combined with `x <= x * 1 + x` (i.e. x >= 0)",
      _by_line(_s111, 17)["verdict"] == "dead", f"-> {_by_line(_s111, 17)}")
_verify_every_witness(_s111, _SEED_111_LOOP_RETURN, "f", "seed-111")
_ns_s111: dict = {}
exec(compile(_SEED_111_LOOP_RETURN, "<t>", "exec"), _ns_s111)  # exec is a global ruff-ignore (S102); check_no_eval.py is the real gate
check("seed-111: ground truth — f(1) == 6 (the old bad witness for a "
      "downstream branch actually returns 6 at the while)",
      _ns_s111["f"](1) == 6, f"-> f(1)={_ns_s111['f'](1)}")


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL BRANCH_REACHABILITY TESTS PASS ===")
for _f in FAILS:
    print(f"  {_f}")
sys.exit(1 if FAILS else 0)
