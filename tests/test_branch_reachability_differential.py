"""Differential test for `branch_reachability`: ~150 deterministically
GENERATED small programs over the supported subset, checked against
GROUND TRUTH from exhaustive concrete execution — not hand-picked
examples. Three review rounds each found a DIFFERENT false-verdict bug
(the if/elif/else merge, the loop-value taint, the unrolled-loop return
closure) that a hand-written suite missed because nobody happened to
write the specific combination that triggered it. This suite exists to
catch the FOURTH one, whatever shape it takes, by trying hundreds of small
combinations instead of a dozen carefully-chosen ones.

`scripts/check_no_eval.py` only scans `codecalc/` (`PKG = REPO /
"codecalc"`), not `tests/` — confirmed by reading the script before
writing this file — so an in-process `exec()` of each generated program is
in scope here and keeps the full ~150-program corpus fast (no
sandbox/executor round trip per concrete run).

GROUND TRUTH, per generated program: for every combination of its int
input(s) in `DOMAIN` (an EXHAUSTIVE sweep, not a sample), run the actual
generated function in-process under a tiny `sys.settrace` tracer scoped to
just that function's own code object, and record which source lines fire.
Union across the whole domain gives "ever executed anywhere in this
domain" per line — the ground truth a `dead` verdict is checked against.
A `reachable` verdict is checked more precisely, against its OWN witness
specifically (not just "was ever executed for SOME input") — exactly
mirroring how `tests/test_branch_reachability.py`'s hand-written cases are
corroborated via `tracing.execute_trace`, just with a lighter, in-process
mechanism suited to running hundreds of them quickly. `unknown` is never
checked against ground truth (allowed anywhere by design — see the module
docstring's LOOPS section for the two real limitations still `unknown`).

Standalone script (check()/FAILS/sys.exit), no pytest — the repo
convention. Deterministic: SEED is fixed, so a failure here reproduces
identically on any machine.
"""

from __future__ import annotations

import ast
import pathlib
import random
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import branch_reachability as br

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


SEED = 20260908
CORPUS_SIZE = 150
DOMAIN = list(range(-12, 13))  # exhaustive per int input, as specified
_MAX_GENERATION_ATTEMPTS = CORPUS_SIZE * 20  # generous — a refused/degenerate
# draw is regenerated, not counted; this bounds the retry loop itself.
_MAX_TRACE_EVENTS = 200_000  # a defensive circuit breaker — every generated
# `while` terminates BY CONSTRUCTION (an unconditional counter increment
# against a static bound is always the last body statement), so this
# should never fire; it exists so a generator bug fails LOUDLY as a test
# failure instead of hanging the suite.


# ── program generation ───────────────────────────────────────────────────
#: Kept to `+ - *` only (never `//`/`%`) — this corpus exists to fuzz
#: CONTROL-FLOW soundness (the class of bug all three review rounds found),
#: not operator coverage, which the hand-written suite already exercises;
#: skipping `//`/`%` sidesteps having to also guarantee a positive-literal
#: divisor on every generated instance for no soundness-testing benefit.
_ARITH_OPS = ("+", "-", "*")
_COMPARE_OPS = ("<", "<=", ">", ">=", "==", "!=")
_MAX_STMT_DEPTH = 3  # caps if/elif nesting so generation terminates —
# without this, "if" being in _gen_stmt's choices at every depth makes the
# recursion depth unbounded (each level rolls a fresh chance of going one
# deeper), and Python's own call-stack limit turns that into a crash
# rather than just an unusually deep program.


def _indent(text: str, pad: str = "    ") -> str:
    return "\n".join(pad + line for line in text.split("\n"))


def _gen_leaf(rng: random.Random, scope: list[str]) -> str:
    if scope and rng.random() < 0.7:
        return rng.choice(scope)
    return str(rng.randint(-5, 5))


def _gen_arith(rng: random.Random, scope: list[str], depth: int = 0) -> str:
    if depth >= 2 or rng.random() < 0.55:
        return _gen_leaf(rng, scope)
    op = rng.choice(_ARITH_OPS)
    if op == "*":
        # variable*variable (e.g. `y * y`, `x * y`) puts z3 in NONLINEAR
        # integer arithmetic, which is not even decidable in general and
        # can make a single `check()` call hang rather than just being
        # slow — a soundness/performance distinction this test has no
        # business exercising. Multiplying by a plain literal keeps every
        # generated program strictly LINEAR, which z3 always decides fast,
        # while still exercising `*` as an operator.
        return f"({_gen_arith(rng, scope, depth + 1)} * {rng.randint(-5, 5)})"
    return f"({_gen_arith(rng, scope, depth + 1)} {op} {_gen_arith(rng, scope, depth + 1)})"


def _gen_compare(rng: random.Random, scope: list[str]) -> str:
    op = rng.choice(_COMPARE_OPS)
    return f"{_gen_arith(rng, scope)} {op} {_gen_arith(rng, scope)}"


def _gen_cond(rng: random.Random, scope: list[str], depth: int = 0) -> str:
    if depth >= 1 or rng.random() < 0.65:
        return _gen_compare(rng, scope)
    kind = rng.choice(("and", "or", "not"))
    if kind == "not":
        return f"not ({_gen_cond(rng, scope, depth + 1)})"
    return f"({_gen_cond(rng, scope, depth + 1)} {kind} {_gen_cond(rng, scope, depth + 1)})"


class _Fresh:
    """A monotonic counter for generated variable/loop-var names, unique
    across one whole program so nested constructs never accidentally
    shadow an outer name (which would be legal Python but would muddy what
    a mismatch is actually testing). Also carries `loops_remaining`: a
    per-PROGRAM budget (not per-branch) on how many `for`/`while`
    constructs may be generated at all. Sequentially chaining several
    range(6)-unrolled loops (each itself containing nested ifs) compounds
    the symbolic environment expression built for one variable across
    every copy — genuinely slow for z3 to solve, not a bug, but exactly
    the kind of case this fuzz corpus must not spend its ~90s budget
    stumbling into by accident. One loop per generated program keeps that
    compounding bounded while still exercising the unroll/taint paths
    every generated iteration.
    """

    def __init__(self) -> None:
        self.n = 0
        self.loops_remaining = 1

    def var(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"


def _gen_stmt(rng: random.Random, scope: list[str], fresh: _Fresh, depth: int, allow_loop: bool) -> str:
    """One statement's source text, UNINDENTED (the caller adds a
    `_indent` per nesting level). May append to `scope` IN PLACE — but
    only with a name that is guaranteed bound on every path reaching the
    code after this statement (see the if/for/while cases below): the
    generator deliberately never produces a "maybe unbound" reference,
    since exercising THAT refusal/`unknown` path is what the hand-written
    suite already covers, and an accidental one here would just shrink the
    valid corpus for no soundness-testing benefit.
    """
    choices = ["assign"]
    if depth < _MAX_STMT_DEPTH:
        choices.append("if")
    if allow_loop and depth < 2 and fresh.loops_remaining > 0:
        choices += ["for", "while"]
    if rng.random() < 0.12:
        choices.append("return")
    kind = rng.choice(choices)

    if kind == "assign":
        reuse = scope and rng.random() < 0.5
        name = rng.choice(scope) if reuse else fresh.var("v")
        # The RHS is generated against the OLD scope (before `name` is
        # added) when `name` is fresh — otherwise a freshly-picked name
        # could reference ITSELF on its own defining right-hand side,
        # which is legal syntax but an UnboundLocalError at runtime (the
        # name has no value yet). Reusing an already-scoped name is fine
        # either way since it is already bound.
        rhs = _gen_arith(rng, scope)
        if name not in scope:
            scope.append(name)
        return f"{name} = {rhs}"

    if kind == "return":
        return f"return {rng.randint(0, 9)}"

    if kind == "for":
        fresh.loops_remaining -= 1
        n = rng.choice([0, 1, 2, 3, 4, 5, 6])
        loop_var = fresh.var("i")
        body_scope = [*scope, loop_var]
        # Body statements are capped at `_MAX_STMT_DEPTH - 1` regardless of
        # the CURRENT nesting depth — see `_Fresh`'s docstring: this keeps
        # an unrolled loop's per-iteration body shallow (at most one level
        # of if-nesting) even when the loop itself sits deep inside outer
        # ifs, since it is BODY nesting x ITERATION count that compounds.
        body_depth = max(depth + 1, _MAX_STMT_DEPTH - 1)
        body_lines = [_gen_stmt(rng, body_scope, fresh, body_depth, allow_loop=False)]
        # Nothing from body_scope is propagated to `scope`: the loop
        # variable is loop-local by this generator's own choice (never
        # referenced after), and anything ELSE the body assigns is only
        # bound when the loop actually ran at least once — not guaranteed
        # for range(0) — so it stays out of scope for later code, the same
        # "never generate a maybe-unbound reference" rule as the if case.
        return f"for {loop_var} in range({n}):\n" + _indent("\n".join(body_lines))

    if kind == "while":
        fresh.loops_remaining -= 1
        bound = rng.randint(1, 4)
        counter = fresh.var("n")
        # `counter` is deliberately kept OUT of scope entirely (not just
        # out of the top-level body statement) — this loop's termination
        # guarantee is "nothing but the trailing `+= 1` below ever
        # touches it". If a generated body statement (including one
        # nested inside an if/else arm) could ALSO pick `counter` as an
        # assignment target — e.g. reset it back to 0, or to some
        # input-dependent value — the loop could run forever for some
        # concrete input, and ground truth for everything AFTER it would
        # never be computable. Excluding it from scope up front rules
        # that out structurally rather than trying to pattern-match
        # generated source for a reassignment after the fact.
        body_depth = max(depth + 1, _MAX_STMT_DEPTH - 1)
        body_lines = [_gen_stmt(rng, list(scope), fresh, body_depth, allow_loop=False)]
        body_lines.append(f"{counter} = {counter} + 1")  # guarantees termination
        return f"{counter} = 0\nwhile {counter} < {bound}:\n" + _indent("\n".join(body_lines))

    # kind == "if"
    cond = _gen_cond(rng, scope)
    body_scope = list(scope)
    body_lines = [_gen_stmt(rng, body_scope, fresh, depth + 1, allow_loop=allow_loop and depth < 1)
                  for _ in range(rng.randint(1, 2))]
    lines = [f"if {cond}:", _indent("\n".join(body_lines))]
    if rng.random() < 0.5:
        else_scope = list(scope)
        else_lines = [_gen_stmt(rng, else_scope, fresh, depth + 1, allow_loop=False)
                      for _ in range(rng.randint(1, 2))]
        lines += ["else:", _indent("\n".join(else_lines))]
        # A name assigned in BOTH arms is bound on every path past the
        # whole if/else — safe to propagate. Anything assigned in only one
        # arm is deliberately left out of `scope` (see this function's
        # own docstring).
        both = (set(body_scope) - set(scope)) & (set(else_scope) - set(scope))
        scope.extend(sorted(both - set(scope)))
    return "\n".join(lines)


def _gen_program(rng: random.Random) -> tuple[str, list[str]]:
    """`(source, param_names)` for one small program: 1 or 2 int
    parameters, a body of 2-5 top-level statements, always ending with a
    trailing `return 0` so the function has a real, unconditional final
    statement regardless of what the generated body did.
    """
    n_params = 1 if rng.random() < 0.7 else 2
    params = ["x", "y"][:n_params]
    fresh = _Fresh()
    scope = list(params)
    n_stmts = rng.randint(2, 5)
    lines = [_gen_stmt(rng, scope, fresh, depth=0, allow_loop=True) for _ in range(n_stmts)]
    lines.append("return 0")
    body = _indent("\n".join(lines))
    source = f"def f({', '.join(params)}):\n{body}\n"
    return source, params


# ── ground truth: exhaustive concrete execution, in-process ────────────────

def _ever_executed(func, domain_combos: list[tuple[int, ...]]) -> set[int]:
    """Union of every line `func` executes, across EVERY input combination
    in `domain_combos` — the ground truth a `dead` verdict is checked
    against below. `sys.settrace` is scoped to `func.__code__` specifically
    (an `id()` check, not a filename match — these are all freshly
    `exec`'d into distinct code objects, so identity is unambiguous and
    cheaper than a string compare), so nothing else in the process is
    traced.
    """
    code = func.__code__
    executed: set[int] = set()
    event_count = 0

    def tracer(frame, event, _arg):
        nonlocal event_count
        if frame.f_code is code:
            event_count += 1
            if event_count > _MAX_TRACE_EVENTS:
                raise RuntimeError("generated program exceeded the trace-event circuit breaker")
            if event == "line":
                executed.add(frame.f_lineno)
        return tracer

    for combo in domain_combos:
        sys.settrace(tracer)
        try:
            func(*combo)
        except Exception:
            pass  # a real runtime failure (e.g. an unreachable-in-practice
            # overflow) still leaves whatever ran up to it in `executed` —
            # exactly what ground truth should reflect.
        finally:
            sys.settrace(None)
    return executed


def _hits_line(func, args: tuple[int, ...], line: int) -> bool:
    return line in _ever_executed(func, [args])


def _proof_line(tree: ast.Module, header_line: int, kind: str) -> int:
    """The line INSIDE an arm's own body — see `tests/
    test_branch_reachability.py`'s identical helper for the full
    reasoning (copied, not imported: that file is a standalone script that
    runs its whole suite at import time, so importing from it would run it
    as a side effect).
    """
    if kind == "else":
        return header_line
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.For)) and node.lineno == header_line:
            return node.body[0].lineno
    raise AssertionError(f"no {kind} node at line {header_line}")


# ── build the corpus ─────────────────────────────────────────────────────

_rng = random.Random(SEED)  # noqa: S311 — corpus generation, not cryptography; determinism is the point
_corpus: list[tuple[str, list[str], dict]] = []  # (source, params, analyze_result)
_attempts = 0
while len(_corpus) < CORPUS_SIZE and _attempts < _MAX_GENERATION_ATTEMPTS:
    _attempts += 1
    _source, _params = _gen_program(_rng)
    _result = br.analyze("python3", _source)  # never caught: a crash here IS a failure
    if _result.get("ok") is not True:
        continue  # a rare unsupported draw — regenerate, don't count it
    if not _result.get("branches"):
        continue  # a program with no branch points tests nothing here
    _corpus.append((_source, _params, _result))

check(f"generated a full {CORPUS_SIZE}-program corpus (seed {SEED}, "
      f"{_attempts} draws attempted)", len(_corpus) == CORPUS_SIZE,
      f"-> {len(_corpus)}/{CORPUS_SIZE}")


# ── check every program against ground truth ────────────────────────────

_start = time.monotonic()
_checked_reachable = 0
_checked_dead = 0
_checked_unknown = 0
_first_failure_source: str | None = None

for _source, _params, _result in _corpus:
    _tree = ast.parse(_source)
    _ns: dict = {}
    exec(compile(_source, "<generated>", "exec"), _ns)  # S102 is a global ruff ignore; check_no_eval.py's own AST scan (codecalc/ only) is the real gate
    _func = _ns["f"]

    _domain_combos = [(v,) for v in DOMAIN] if len(_params) == 1 else \
        [(a, b) for a in DOMAIN for b in DOMAIN]
    _ground_truth: set[int] | None = None  # computed lazily, only if a `dead` verdict needs it

    for _branch in _result["branches"]:
        _line = _proof_line(_tree, _branch["line"], _branch["kind"])
        _verdict = _branch["verdict"]

        if _verdict == "reachable":
            _checked_reachable += 1
            _witness = _branch["witness"]
            _args = tuple(_witness[p] for p in _params)
            _hit = _hits_line(_func, _args, _line)
            if not _hit and _first_failure_source is None:
                _first_failure_source = _source
            check(f"differential: reachable witness {_witness} for line "
                  f"{_branch['line']} ({_branch['kind']}) actually executes "
                  f"body line {_line}", _hit,
                  f"-> source:\n{_source}")

        elif _verdict == "dead":
            _checked_dead += 1
            if _ground_truth is None:
                _ground_truth = _ever_executed(_func, _domain_combos)
            _never_hit = _line not in _ground_truth
            if not _never_hit and _first_failure_source is None:
                _first_failure_source = _source
            check(f"differential: dead line {_branch['line']} ({_branch['kind']}) "
                  f"-> body line {_line} is NEVER executed for any input in "
                  f"[{DOMAIN[0]}, {DOMAIN[-1]}]" + ("^2" if len(_params) == 2 else ""),
                  _never_hit, f"-> source:\n{_source}")

        else:
            _checked_unknown += 1  # `unknown` is allowed anywhere — no ground-truth check

_elapsed = time.monotonic() - _start

check(f"differential: exhaustive-execution checks completed within budget "
      f"({_elapsed:.1f}s)", _elapsed < 90.0, f"-> {_elapsed:.1f}s")
check("differential: every verdict class was actually exercised (an "
      "existence floor — a corpus that never produced a `dead` or "
      "`reachable` verdict would make this whole file vacuous)",
      _checked_reachable > 0 and _checked_dead > 0,
      f"-> reachable={_checked_reachable} dead={_checked_dead} unknown={_checked_unknown}")

if FAILS and _first_failure_source is not None:
    print(f"\nfirst failing program (seed {SEED}):\n{_first_failure_source}")

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL BRANCH_REACHABILITY_DIFFERENTIAL TESTS PASS ===")
for _f in FAILS:
    print(f"  {_f}")
sys.exit(1 if FAILS else 0)
