"""Static, z3-decided reachability for `branch_reachability` (MCP tool in
server.py).

WHAT THIS IS. `trace_execution` answers "which branch did THIS input take".
This answers "which branches CAN ever be taken, for ANY input" — every
if/elif/else arm and every while/for(range, static bounds) loop body in a
single Python function is translated to an SMT path condition and handed to
z3; the verdict (`reachable`/`dead`/`unknown`) is a decided answer, not a
guess from source shape. `verify_optimization`'s asymmetric-floor style of
"nothing here is measured; it is proven or it says why not" is the model.

SUBSET, DECIDED UP FRONT. `_first_unsupported` walks the WHOLE parsed source
once, before any z3 call, and returns the first disallowed construct it
finds (float/complex/bytes/None literals, attribute access, f-strings,
comprehensions, classes, async, try/except, imports, subscripts, chained or
`is`/`in` comparisons, any call outside abs/min/max/len, a data-dependent
loop bound, `//`/`%` by anything but a positive integer literal, ...). This
is deliberately BROADER than what the translator below could technically
choke on — the ambition is one refusal shape for the whole unsupported
surface, found BEFORE spending a single solver call, always naming the
construct and the line it lives on. See docs/design/2026-09-08-
branch-reachability.md for why this line was drawn where it was.

TRANSLATION. Only after that scan passes does `_translate` turn an
expression into a z3 term: `int`/`bool`/`str` sorts, `+ - * // %`
arithmetic (Int), `and`/`or`/`not` (Bool), `== != < <= > >=` (Int/Bool),
`== !=` plus `len()` (String, via `z3.Length`), `abs()`/`min()`/`max()`
(built from `z3.If`, matching Python's tie-break for `min`/`max` on the
FIRST equal argument the same z3.If-chain shape produces). Assignment is
SSA-lite: `env[name]` is rebuilt as a compound z3 expression over the
ORIGINAL parameter symbols on every `x = <expr>`, never a fresh symbol — so
a model for any path condition already speaks in terms of the function's
own parameters, and a witness is read straight off it with no back-
substitution step.

PATH CONDITIONS. Each if/elif/else arm's z3 condition is the conjunction of
every ancestor guard on the way to it, negated for an else/elif arm exactly
the way `not` negates a Python condition — `_walk_if` builds this
recursively, with an `elif` handled as nothing more than a nested `If` in
`orelse` (which is exactly what CPython's own parser already produced) so
the negation chain falls out of the recursion instead of needing separate
bookkeeping. An unconditional `return` on every path through an arm cuts
that arm's condition out of what reaches the CODE AFTER the enclosing
if/elif/else — `_walk_block` returns `(falls_through, continuation_cond,
env)` for exactly that reason: `continuation_cond` is `None` when every arm
returns, and the `or` of whichever arms didn't otherwise.

MERGING ENVIRONMENTS AT A JOIN POINT. A variable assigned inside an if/elif/
else arm is NOT visible to the code after it merely by mutating a shared
dict — the two (or more) arms are MUTUALLY EXCLUSIVE at runtime, so the
value reaching the code after the join depends on which arm actually ran.
`_merge_envs` builds that value as a z3 `If(guard, value_if_arm_ran,
value_otherwise)` per variable, where `guard` is the LOCAL condition for
this if/elif/else only (not the accumulated ancestor path — that is already
baked into the branch conditions the merged value later participates in).
An arm that ends in an unconditional `return` contributes NOTHING to the
merge (`_walk_if` passes `None` for that side): the phi is built from
whichever arm(s) actually fall through, exactly mirroring how
`continuation_cond` itself is built from the same set. A variable assigned
in only SOME of the surviving arms is deliberately DROPPED from the merged
environment rather than guessed at — a later reference to it then raises
the ordinary "undefined name" `_TranslateError` `_translate`'s `ast.Name`
case already raises, which is exactly correct: Python itself would raise
`UnboundLocalError` on the very path that never assigned it. Two envs
agreeing on a variable BY IDENTITY (neither arm reassigned it) skip the
`If` entirely and keep the original binding, which is what keeps a program
with few reassignments from growing an `If` for every untouched parameter.

LOOPS: EXACT WHERE CHEAP, `unknown` WHERE NOT — NEVER A FALSE VERDICT.
Two genuinely different mechanisms, chosen by `_walk_loop`:

  * `for x in range(<static>)` with at most `_MAX_UNROLL_ITERATIONS`
    (32) iterations is UNROLLED EXACTLY (`_walk_for_unrolled`): the body
    is walked once per concrete value of `x`, threading the environment
    SEQUENTIALLY (real symbolic execution, not an approximation). A
    branch inside the body is `reachable` if ANY iteration's own path
    condition is sat, `dead` only if EVERY iteration's is unsat
    (aggregated per source line by `_Ctx.commit_branch`/
    `flush_unroll_frame`, since the same AST node is visited once per
    iteration). Post-loop state is therefore EXACT, including the loop
    target variable itself, which — matching real Python scoping — ends
    up bound to its LAST iteration's value.
  * A `while` loop, or a `for` above that cap, is walked ONE
    representative iteration (`_walk_loop_conservative`), and the earlier
    cut's real defect lived here: binding the loop variable to a fresh,
    RANGE-CONSTRAINED but otherwise unconstrained symbol and then MERGING
    the result made a value that is only ever "the last iteration's
    value" look like "any value in the range" — a real, adversarially-
    confirmed false `reachable` for code after the loop. The fix drops
    the merge for this path entirely: a branch INSIDE the body is
    `reachable` when this one iteration's path condition is sat (a real
    witness is always a real witness, regardless of which iteration
    produced it — sound), and `unknown` — never `dead` — when it is
    unsat (proof on ONE iteration is not proof for all of them;
    `_REASON_FIRST_ITERATION_ONLY`). EVERY variable the body assigns
    anywhere, directly or through a nested arm's own merge, is TAINTED
    afterward: rebound to a brand-new, entirely unconstrained symbol
    (`_Ctx.fresh_tainted_symbol`) carrying no information at all, rather
    than the range-bound-but-still-informative symbol that caused the
    false positive. A later guard whose translated z3 expression mentions
    a tainted symbol ANYWHERE — including buried inside arithmetic, since
    `_apply_assign` never launders it away — is caught by
    `_mentions_tainted` and reported `unknown` (`_REASON_TAINTED_BY_LOOP`)
    WITHOUT ever being solved: the fresh symbol is unconstrained, so z3
    would call it `sat` for nearly anything, which is exactly the false-
    confidence failure mode being closed here.

Both mechanisms are sound in the sense this tool promises: a `reachable`
verdict always carries a witness that is a witness, and only unrolling
(never the conservative path) is trusted to produce `dead`.

BOUNDARY INPUTS. For every `Compare` node appearing in an arm's OWN guard
(not the accumulated ancestors — see the design note for why that line was
drawn there) with at least one int/str-length side, `_boundary_for` uses a
z3 `Optimize` to find the MINIMUM and MAXIMUM value of that side reachable
under the arm's full path condition, plus, when the OTHER side is a
literal constant, the "equality edge" — a witness where the compared
expression equals that literal, checked against the guards ABOVE this one
only, so the edge shows up even when it sits just outside the branch
currently being reported on. No artificial box is placed on the objective:
z3's own `Optimize` handle reports an unbounded direction directly
(`.lower()`/`.upper()` come back as a non-concrete `oo`/`-1*oo` term,
`z3.is_int_value` false on it — measured, not assumed, against the
installed z3), so `_optimum_input` reads THAT rather than a box edge that
would otherwise be indistinguishable from a real extremum. `null` means
"unbounded or unsatisfiable", never a value quietly capped at some
internal constant.

REFUSAL IS THE RESULT. Every refusal — an unsupported construct, a
non-python `language`, more than one top-level function, no function and no
`inputs` — is `errors.VALIDATION`, stamped, naming the construct and its
line where one applies, with a `remedy` pointing at `trace_execution`
(concrete inputs, actually run) — never a raised exception a client has to
catch.
"""

from __future__ import annotations

import ast
import time
from typing import Any

from . import contract, errors, optional, registry

#: See tracing.py's identical constant + reasoning; this tool refuses the
#: same way for the same reason (no uniform AST across the other 30
#: registered languages).
SUPPORTED_LANGUAGE = "python3"

REMEDY = "call trace_execution to run this on concrete inputs instead"

_DEFAULT_TIMEOUT_S = 30
_MAX_TIMEOUT_S = 120
_DEFAULT_MAX_BRANCHES = 64
_HARD_MAX_BRANCHES = 256
#: A `for x in range(<static>)` loop with at most this many iterations is
#: UNROLLED exactly (see `_walk_for_unrolled`) rather than analyzed as one
#: representative iteration — see the module docstring's LOOPS section for
#: why an unconstrained-but-ranged loop variable is unsound for anything
#: AFTER the loop, and why a real, if imprecise, taint-based fallback is
#: used above this cap and for `while`.
_MAX_UNROLL_ITERATIONS = 32
#: `verdict` ranking used to merge the same source line's outcome across
#: unrolled iterations (see `_Ctx.commit_branch`): reachable (proof exists,
#: from ANY iteration) beats unknown beats dead (proof of absence, needed
#: from EVERY iteration).
_VERDICT_RANK = {"dead": 0, "unknown": 1, "reachable": 2}
#: Per-z3-call deadline never exceeds this, regardless of how much of the
#: caller's own `timeout` budget remains — one pathological call must not
#: eat the whole response, the same reasoning logic.z3_check's fixed
#: 5000ms default embodies, just derived from the caller's own budget here
#: instead of a constant.
_PER_CALL_TIMEOUT_MS_CAP = 5_000

_TYPE_NAMES = ("int", "bool", "str")

#: node type -> human name, for the single-pass refusal scan. Deliberately
#: broader than what the translator technically cannot handle (see module
#: docstring) — one refusal shape for the whole unsupported surface.
_DISALLOWED_NODE_TYPES: tuple[tuple[type, str], ...] = (
    (ast.Try, "try/except"),
    (ast.ListComp, "list comprehension"),
    (ast.SetComp, "set comprehension"),
    (ast.DictComp, "dict comprehension"),
    (ast.GeneratorExp, "generator expression"),
    (ast.ClassDef, "class definition"),
    (ast.AsyncFunctionDef, "async function definition"),
    (ast.Attribute, "attribute access"),
    (ast.JoinedStr, "f-string"),
    (ast.Lambda, "lambda"),
    (ast.With, "with statement"),
    (ast.AsyncWith, "async with statement"),
    (ast.AsyncFor, "async for loop"),
    (ast.Global, "global statement"),
    (ast.Nonlocal, "nonlocal statement"),
    (ast.Import, "import statement"),
    (ast.ImportFrom, "import statement"),
    (ast.Delete, "del statement"),
    (ast.Subscript, "subscript"),
    (ast.Starred, "starred expression"),
    (ast.AugAssign, "augmented assignment"),
    (ast.IfExp, "conditional expression"),
    (ast.Yield, "yield"),
    (ast.YieldFrom, "yield from"),
    (ast.Await, "await"),
    (ast.NamedExpr, "walrus assignment"),
    (ast.Break, "break statement"),
    (ast.Continue, "continue statement"),
    (ast.Match, "match statement"),
)
if hasattr(ast, "TryStar"):  # 3.11+ exception groups
    _DISALLOWED_NODE_TYPES = (*_DISALLOWED_NODE_TYPES, (ast.TryStar, "try/except*"))

#: Binary operators this tool translates. `/` (true division, produces a
#: float), `**`, `@`, shifts and bitwise ops are refused up front — not
#: because z3 cannot express them, but because the input subset this tool
#: promises is `+ - * // %` (module docstring), and a caller who wants more
#: is told to run the concrete case instead of getting a silently narrower
#: answer.
_ALLOWED_BINOPS: frozenset[type] = frozenset({ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod})
_BINOP_LABELS: dict[type, str] = {
    ast.Div: "true division (produces a float; use // for integer division)",
    ast.Pow: "exponentiation",
    ast.MatMult: "matrix multiplication",
    ast.LShift: "bit shift", ast.RShift: "bit shift",
    ast.BitAnd: "bitwise and", ast.BitOr: "bitwise or", ast.BitXor: "bitwise xor",
}

_COMPARE_OPS: frozenset[type] = frozenset({ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE})
_COMPARE_TEXT: dict[type, str] = {
    ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
}

_ALLOWED_CALLS = frozenset({"abs", "min", "max", "len"})
#: `range` is not in `_ALLOWED_CALLS` (the translator never accepts it as a
#: value-producing expression — there is no z3 sort for a range object), but
#: it is legitimate in exactly one syntactic position: a `for`'s own
#: `iter`, validated separately by the `ast.For` branch below. Without this,
#: the generic "unsupported call" check would flag every `for x in
#: range(...)` as an unsupported call to `range` before ever reaching that
#: dedicated, more specific check — found by running this scan against the
#: module docstring's own `for` example.
_ALLOWED_CALLS_IN_SCAN = _ALLOWED_CALLS | {"range"}


class _Refused(Exception):
    """Internal signal carrying the construct name and line for the one
    refusal `analyze()` turns into a result. Never escapes this module.
    """

    def __init__(self, construct: str, line: int | None) -> None:
        self.construct = construct
        self.line = line
        super().__init__(construct)


class _TranslateError(Exception):
    """A branch-local translation failure the pre-scan did not catch —
    defensive, not expected to fire given `_first_unsupported` runs first.
    Caught per-branch and turned into `verdict: "unknown"`.
    """


def _refusal(message: str, *, line: int | None = None) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if line is not None:
        extra["line"] = line
    return contract.stamp(errors.error_result(errors.VALIDATION, message, remedy=REMEDY, **extra))


def _refuse_unsupported_language(language: str) -> dict[str, Any] | None:
    """A refusal result if `language` is not python3, else `None` — same
    shape and reasoning as `tracing._refuse_unsupported_language`: resolved
    through `registry.canonical()` so an alias is accepted the same way, and
    the remedy points at the one tool that actually runs code.
    """
    canon = registry.canonical(language)
    if canon == SUPPORTED_LANGUAGE:
        return None
    return _refusal(
        f"branch_reachability does not support language {language!r} "
        f"(resolved: {canon!r}); python3 only, since this tool works from a "
        f"static parse of the source, not an interpreter."
    )


def _first_unsupported(tree: ast.AST) -> tuple[str, int] | None:
    """`(construct, line)` for the first disallowed node in `tree`'s whole
    subtree, in source order, or `None` if nothing is disallowed. See the
    module docstring's SUBSET section for why this scan is broader than the
    translator's own needs.
    """
    hits: list[tuple[int, int, str]] = []

    def add(node: ast.AST, label: str) -> None:
        hits.append((getattr(node, "lineno", 0), getattr(node, "col_offset", 0), label))

    for node in ast.walk(tree):
        for cls, label in _DISALLOWED_NODE_TYPES:
            if isinstance(node, cls):
                add(node, label)
                break
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, float):
                add(node, "float literal")
            elif not isinstance(v, (bool, int, str)):
                add(node, f"unsupported literal ({type(v).__name__})")
        elif isinstance(node, ast.BinOp):
            op_t = type(node.op)
            if op_t not in _ALLOWED_BINOPS:
                add(node, _BINOP_LABELS.get(op_t, "unsupported binary operator"))
            elif op_t in (ast.FloorDiv, ast.Mod):
                r = node.right
                positive_literal = (
                    isinstance(r, ast.Constant) and isinstance(r.value, int)
                    and not isinstance(r.value, bool) and r.value > 0
                )
                if not positive_literal:
                    add(node, "// or % by anything but a positive integer literal "
                              "(z3 and Python disagree on the sign convention otherwise)")
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert):
            add(node, "bitwise not")
        elif isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                add(node, "chained comparison")
            elif type(node.ops[0]) not in _COMPARE_OPS:
                add(node, "unsupported comparison operator (only == != < <= > >=)")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                pass  # the Attribute node itself is already flagged, with a clearer label
            elif not (isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_CALLS_IN_SCAN):
                fname = node.func.id if isinstance(node.func, ast.Name) else None
                add(node, f"call to {fname!r}" if fname else "unsupported call")
        elif isinstance(node, ast.While) and node.orelse:
            add(node, "while/else")
        elif isinstance(node, ast.For) and node.orelse:
            add(node, "for/else")
        elif isinstance(node, ast.For):
            if not isinstance(node.target, ast.Name):
                add(node, "unpacking loop target")
            it = node.iter
            if not (isinstance(it, ast.Call) and isinstance(it.func, ast.Name)
                    and it.func.id == "range" and 1 <= len(it.args) <= 3
                    and not it.keywords):
                add(node, "data-dependent loop bounds (only for x in range(<literal ints>) "
                          "is supported)")
            else:
                for a in it.args:
                    static = isinstance(a, ast.Constant) and isinstance(a.value, int) and not isinstance(a.value, bool)
                    static = static or (
                        isinstance(a, ast.UnaryOp) and isinstance(a.op, ast.USub)
                        and isinstance(a.operand, ast.Constant) and isinstance(a.operand.value, int)
                        and not isinstance(a.operand.value, bool)
                    )
                    if not static:
                        add(node, "data-dependent loop bounds (only for x in "
                                  "range(<literal ints>) is supported)")
                        break

    if not hits:
        return None
    hits.sort(key=lambda h: (h[0], h[1]))
    _, _, label = hits[0]
    return label, hits[0][0]


def _static_range_bounds(it: ast.Call) -> tuple[int, int, int]:
    """`(start, stop, step)` for a pre-validated `range(...)` call."""
    def val(a: ast.expr) -> int:
        if isinstance(a, ast.UnaryOp):
            return -a.operand.value
        return a.value
    args = [val(a) for a in it.args]
    if len(args) == 1:
        return 0, args[0], 1
    if len(args) == 2:
        return args[0], args[1], 1
    return args[0], args[1], args[2]


class _Ctx:
    """Per-call state threaded through the recursive walk: the z3 module
    (imported once), the parameter symbols (for witnesses), the discovered
    branches, and the time/count budgets.
    """

    def __init__(self, z3mod, params: dict[str, Any], timeout_s: int, max_branches: int) -> None:
        self.z3 = z3mod
        self.params = params  # name -> (z3 const, dtype)
        self.branches: list[dict[str, Any]] = []
        self.max_branches = max_branches
        self.truncated = False
        self.deadline = time.monotonic() + timeout_s
        self.supported = True  # flips false on a per-branch translation failure
        # Aggregation frames for `for`-unrolling — see `_walk_for_unrolled` and
        # `commit_branch`. One dict per ACTIVE unroll (nested unrolled loops
        # push a frame each), keyed by source line, so the same line visited
        # once per unrolled iteration collapses to ONE entry.
        self._unroll_stack: list[dict[int, dict[str, Any]]] = []
        # > 0 while walking a loop body that is NOT being exactly unrolled
        # (a `while`, or a `for` above `_MAX_UNROLL_ITERATIONS`) — see the
        # module docstring's LOOPS section: an UNSAT verdict found in this
        # mode only proves "not reachable on the first iteration", not "dead
        # forever", so `_decide` downgrades it to `unknown` instead.
        self._conservative_loop_depth = 0
        # Declared NAMES of the fresh, unconstrained symbols `commit_taint`
        # binds a variable to after a conservative loop — see that method's
        # docstring. Checked structurally (does a guard's own z3 expression
        # tree mention one of these names ANYWHERE, including nested inside
        # arithmetic) by `_mentions_tainted`, never by the ORIGINAL variable
        # name, so a later, clean reassignment correctly clears the taint.
        self.tainted_names: set[str] = set()
        self._taint_counter = 0

    @property
    def unrolling(self) -> bool:
        return bool(self._unroll_stack)

    def time_left_ms(self) -> int:
        remaining = self.deadline - time.monotonic()
        return max(0, int(remaining * 1000))

    def call_timeout_ms(self) -> int:
        return max(1, min(self.time_left_ms(), _PER_CALL_TIMEOUT_MS_CAP))

    def budget_exhausted(self) -> bool:
        return time.monotonic() >= self.deadline

    def commit_branch(self, entry: dict[str, Any]) -> None:
        """Append `entry` to the result, OR — while `unrolling` — merge it
        into the top aggregation frame keyed by `entry["line"]` instead.

        The same source line is visited once per unrolled iteration; the
        merged verdict is `reachable` if ANY iteration's own path condition
        was sat (keeping that iteration's witness/boundary — the FIRST such
        iteration, since iterations commit in order and a later, higher-rank
        entry only replaces a strictly lower-ranked existing one), else
        `unknown` if any iteration could not be decided, else `dead` only
        when EVERY iteration proved it unsatisfiable — see `_VERDICT_RANK`.
        A nested unrolled loop's own frame flushes into the frame below it
        on the stack (or into `branches`, once the stack is empty), so
        aggregation composes correctly to any nesting depth.
        """
        if self._unroll_stack:
            frame = self._unroll_stack[-1]
            existing = frame.get(entry["line"])
            if existing is None or _VERDICT_RANK[entry["verdict"]] > _VERDICT_RANK[existing["verdict"]]:
                frame[entry["line"]] = entry
            return
        if len(self.branches) >= self.max_branches:
            self.truncated = True
            return
        self.branches.append(entry)

    def flush_unroll_frame(self) -> None:
        """Pop the top aggregation frame and commit each of its (already
        cross-iteration-merged) entries — via `commit_branch` again, so a
        NESTED unroll's frame lands in its parent frame rather than
        `branches` directly if one is still active.
        """
        frame = self._unroll_stack.pop()
        for entry in frame.values():
            self.commit_branch(entry)

    def fresh_tainted_symbol(self, dtype: str, hint: str):
        """A brand-new, entirely unconstrained z3 constant of `dtype`,
        registered in `tainted_names` so `_mentions_tainted` recognizes any
        later guard built from it (directly, or buried inside arithmetic —
        the check walks the whole expression tree). `hint` (the original
        variable name) is cosmetic, folded into the generated name only to
        make a raw z3 dump readable; nothing compares against it.
        """
        self._taint_counter += 1
        name = f"__tainted_{self._taint_counter}_{hint}"
        self.tainted_names.add(name)
        if dtype == "int":
            return self.z3.Int(name)
        if dtype == "bool":
            return self.z3.Bool(name)
        return self.z3.String(name)

    def witness(self, model) -> dict[str, Any]:
        """One concrete input dict off `model` — every parameter, not just
        the ones that turned out to be free variables in the formula z3
        actually solved. `model[sym]` (indexing, not the model's own
        `.eval(..., model_completion=True)` — this package's zero-`eval`
        invariant is enforced by `scripts/check_no_eval.py`'s AST scan,
        which cannot tell z3's `Model.eval` from the dynamic-execution
        builtin of the same name, so this avoids the name entirely) is
        `None` for a parameter that never appears in the path condition at
        all (an ignored argument, for instance) — measured to match
        `model_completion=True`'s own defaults exactly: `0` for Int, `False`
        for Bool, `""` for String.
        """
        out: dict[str, Any] = {}
        for name, (sym, dtype) in self.params.items():
            val = model[sym]
            if val is None:
                out[name] = 0 if dtype == "int" else False if dtype == "bool" else ""
            elif dtype == "int":
                out[name] = val.as_long()
            elif dtype == "bool":
                out[name] = bool(self.z3.is_true(val))
            else:
                out[name] = val.as_string()
        return out


def _translate(node: ast.expr, env: dict[str, tuple[Any, str]], ctx: _Ctx) -> tuple[Any, str]:
    """`(z3_expr, dtype)` for a pre-validated expression node. Raises
    `_TranslateError` on anything the upfront scan should already have
    refused — defensive, see that class's docstring.
    """
    z3 = ctx.z3
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return z3.BoolVal(v), "bool"
        if isinstance(v, int):
            return z3.IntVal(v), "int"
        if isinstance(v, str):
            return z3.StringVal(v), "str"
        raise _TranslateError(f"unsupported literal {v!r}")
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise _TranslateError(f"undefined name {node.id!r}")
        return env[node.id]
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.UAdd):
            return _translate(node.operand, env, ctx)
        val, dtype = _translate(node.operand, env, ctx)
        if isinstance(node.op, ast.USub):
            if dtype != "int":
                raise _TranslateError("unary - on a non-int value")
            return -val, "int"
        if isinstance(node.op, ast.Not):
            if dtype != "bool":
                raise _TranslateError("not on a non-bool value")
            return z3.Not(val), "bool"
        raise _TranslateError("unsupported unary operator")
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _ALLOWED_BINOPS:
            raise _TranslateError("unsupported binary operator")
        l, ldt = _translate(node.left, env, ctx)
        r, rdt = _translate(node.right, env, ctx)
        if ldt != "int" or rdt != "int":
            raise _TranslateError("arithmetic on a non-int operand")
        if isinstance(node.op, ast.Add):
            return l + r, "int"
        if isinstance(node.op, ast.Sub):
            return l - r, "int"
        if isinstance(node.op, ast.Mult):
            return l * r, "int"
        if isinstance(node.op, ast.FloorDiv):
            return l / r, "int"
        return l % r, "int"
    if isinstance(node, ast.BoolOp):
        vals = []
        for v in node.values:
            val, dt = _translate(v, env, ctx)
            if dt != "bool":
                raise _TranslateError("and/or on a non-bool value")
            vals.append(val)
        return (z3.And(*vals), "bool") if isinstance(node.op, ast.And) else (z3.Or(*vals), "bool")
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise _TranslateError("chained comparison")
        l, ldt = _translate(node.left, env, ctx)
        r, rdt = _translate(node.comparators[0], env, ctx)
        op_t = type(node.ops[0])
        if op_t not in _COMPARE_OPS:
            raise _TranslateError("unsupported comparison operator")
        if ldt != rdt:
            raise _TranslateError("comparison between two different types")
        if ldt == "str" and op_t not in (ast.Eq, ast.NotEq):
            raise _TranslateError("only == and != are supported for str")
        if op_t is ast.Eq:
            return l == r, "bool"
        if op_t is ast.NotEq:
            return l != r, "bool"
        if op_t is ast.Lt:
            return l < r, "bool"
        if op_t is ast.LtE:
            return l <= r, "bool"
        if op_t is ast.Gt:
            return l > r, "bool"
        return l >= r, "bool"
    if isinstance(node, ast.Call):
        if not (isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_CALLS):
            raise _TranslateError("unsupported call")
        fname = node.func.id
        args = [_translate(a, env, ctx) for a in node.args]
        if fname == "len":
            if len(args) != 1 or args[0][1] != "str":
                raise _TranslateError("len() needs exactly one str argument")
            return z3.Length(args[0][0]), "int"
        if fname == "abs":
            if len(args) != 1 or args[0][1] != "int":
                raise _TranslateError("abs() needs exactly one int argument")
            val = args[0][0]
            return z3.If(val >= 0, val, -val), "int"
        if fname in ("min", "max"):
            if len(args) < 2 or any(dt != "int" for _, dt in args):
                raise _TranslateError(f"{fname}() needs >= 2 int arguments")
            acc = args[0][0]
            for val, _ in args[1:]:
                acc = z3.If(val < acc, val, acc) if fname == "min" else z3.If(val > acc, val, acc)
            return acc, "int"
    raise _TranslateError(f"unsupported expression node {type(node).__name__}")


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<unprintable>"


#: Set only for the two conservative-loop cases `_decide`/`_walk_if` can
#: produce — never for a plain solver timeout or an ordinary dead/reachable
#: verdict, which say nothing more than the verdict itself already does.
_REASON_FIRST_ITERATION_ONLY = "loop body analysed for the first iteration only"
_REASON_TAINTED_BY_LOOP = "depends on a value computed by a loop"


def _mentions_tainted(expr, ctx: _Ctx) -> bool:
    """Whether `expr`'s own z3 AST contains, ANYWHERE (including buried
    inside arithmetic built on top of it — `_apply_assign` never launders
    this away, since the new binding's z3 expression literally embeds the
    tainted term), a constant declared with one of `ctx.tainted_names`.

    A plain iterative walk over `.children()`, not a z3-provided utility:
    z3's own AST nodes are DAGs (a shared subterm appears once but is
    referenced from multiple parents), so `id()`-based visited-tracking is
    what keeps this from doing exponential re-work on a deeply-nested
    expression, the same reasoning `_boundary_for`'s own bounded work
    already applies elsewhere in this module.
    """
    if not ctx.tainted_names:
        return False
    stack = [expr]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        node_id = id(node)
        if node_id in seen:
            continue
        seen.add(node_id)
        try:
            if node.num_args() == 0 and node.decl().name() in ctx.tainted_names:
                return True
        except Exception:
            pass
        try:
            stack.extend(node.children())
        except Exception:
            pass
    return False


def _decide(cond, ctx: _Ctx) -> tuple[str, dict[str, Any] | None, str | None]:
    """`(verdict, witness, reason)` for one path condition. `witness` is
    `None` unless `verdict == "reachable"`; `reason` is set only for the
    conservative-loop UNSAT-downgraded-to-`unknown` case (see
    `_REASON_FIRST_ITERATION_ONLY`) — see the module docstring's LOOPS
    section for why an UNSAT found while `ctx._conservative_loop_depth > 0`
    proves only "not reachable on the first iteration", never "dead".
    """
    if ctx.budget_exhausted():
        return "unknown", None, None
    z3 = ctx.z3
    solver = z3.Solver()
    solver.set("timeout", ctx.call_timeout_ms())
    solver.add(cond)
    verdict = solver.check()
    if verdict == z3.sat:
        return "reachable", ctx.witness(solver.model()), None
    if verdict == z3.unsat:
        if ctx._conservative_loop_depth > 0:
            return "unknown", None, _REASON_FIRST_ITERATION_ONLY
        return "dead", None, None
    return "unknown", None, None


def _optimum_input(full_cond, objective, direction: str, ctx: _Ctx) -> tuple[dict[str, Any] | None, str | None]:
    """`(input_or_None, note_or_None)` for the MIN/MAX value of `objective`
    subject to `full_cond`. `input_or_None` is `None` when unsatisfiable, a
    solver timeout, OR the true optimum is UNBOUNDED in that direction —
    see the module docstring's BOUNDARY INPUTS section for how the
    unbounded case is detected (`z3.is_int_value` on the `Optimize`
    handle's own `.lower()`/`.upper()`) rather than assumed from an
    artificial box. `note_or_None` is set ONLY for the confirmed-unbounded
    case — a plain unsat or timeout says nothing more than the existing
    `null` already does, and would be noise on top of it.
    """
    z3 = ctx.z3
    opt = z3.Optimize()
    opt.set("timeout", ctx.call_timeout_ms())
    opt.add(full_cond)
    handle = opt.minimize(objective) if direction == "min" else opt.maximize(objective)
    if opt.check() != z3.sat:
        return None, None
    bound = handle.lower() if direction == "min" else handle.upper()
    if not z3.is_int_value(bound):
        word = "below" if direction == "min" else "above"
        return None, f"unbounded {word}: no {direction}imum exists under this branch's own path condition"
    return ctx.witness(opt.model()), None


def _boundary_for(node: ast.expr, parent_cond, full_cond, env: dict[str, tuple[Any, str]], ctx: _Ctx) -> list[dict[str, Any]]:
    """One `boundary_inputs` entry per `Compare` found in `node` (an arm's
    OWN guard — see the module docstring for why ancestor guards are not
    also walked here) that has at least one int/str-length side. Skipped
    entirely — not an error — for a guard with no numeric side to bound
    (a bare `s == "x"` string-equality guard, for instance).
    """
    z3 = ctx.z3
    out: list[dict[str, Any]] = []
    for cmp in ast.walk(node):
        if not isinstance(cmp, ast.Compare) or len(cmp.ops) != 1 or ctx.budget_exhausted():
            continue
        try:
            l, ldt = _translate(cmp.left, env, ctx)
            r, rdt = _translate(cmp.comparators[0], env, ctx)
        except _TranslateError:
            continue
        if ldt != rdt or (ldt != "int" and rdt != "int"):
            continue  # str==str has no numeric side to bound
        left_is_const = isinstance(cmp.left, ast.Constant)
        right_is_const = isinstance(cmp.comparators[0], ast.Constant)
        if left_is_const and right_is_const:
            continue  # both literal — nothing to vary
        objective, threshold = (r, l) if left_is_const else (l, None if not right_is_const else r)
        entry: dict[str, Any] = {"guard": _unparse(cmp), "operator": _COMPARE_TEXT[type(cmp.ops[0])]}

        entry["min_input"], min_note = _optimum_input(full_cond, objective, "min", ctx)
        if min_note is not None:
            entry["min_note"] = min_note
        entry["max_input"], max_note = _optimum_input(full_cond, objective, "max", ctx)
        if max_note is not None:
            entry["max_note"] = max_note

        if threshold is not None:
            edge = z3.Solver()
            edge.set("timeout", ctx.call_timeout_ms())
            edge.add(parent_cond, objective == threshold)
            entry["equality_edge_input"] = ctx.witness(edge.model()) if edge.check() == z3.sat else None
        else:
            entry["equality_edge_input"] = None
        out.append(entry)
    return out


def _record(ctx: _Ctx, *, line: int, kind: str, condition: str, cond, guard_node: ast.expr | None,
            parent_cond, env: dict[str, tuple[Any, str]]) -> None:
    # The early truncation check only applies OUTSIDE an active unroll: the
    # final, merged branch count is not known until the whole unrolled loop
    # has been walked and its frame flushed (see `commit_branch`), so
    # solving must continue through every iteration regardless of how many
    # entries are ALREADY in `ctx.branches`.
    if not ctx.unrolling and len(ctx.branches) >= ctx.max_branches:
        ctx.truncated = True
        return
    reason = None
    try:
        verdict, witness, reason = _decide(cond, ctx)
        boundary = _boundary_for(guard_node, parent_cond, cond, env, ctx) if guard_node is not None else []
    except _TranslateError:
        verdict, witness, boundary = "unknown", None, []
        ctx.supported = False
    entry: dict[str, Any] = {"line": line, "kind": kind, "condition": condition, "verdict": verdict}
    if witness is not None:
        entry["witness"] = witness
    if reason is not None:
        entry["reason"] = reason
    entry["boundary_inputs"] = boundary
    ctx.commit_branch(entry)


def _record_tainted(ctx: _Ctx, *, line: int, kind: str, condition: str) -> None:
    """A branch whose OWN guard mentions a value a loop computed — see
    `_mentions_tainted`. Never solved: the fresh symbol is by construction
    unconstrained, so z3 would report `sat` for almost anything, which
    would silently launder a genuinely unknown answer into a false
    `reachable`. Recorded `unknown` directly instead, with `reason` naming
    why, and NO `boundary_inputs` (nothing to optimize over that means
    anything).
    """
    if not ctx.unrolling and len(ctx.branches) >= ctx.max_branches:
        ctx.truncated = True
        return
    ctx.commit_branch({
        "line": line, "kind": kind, "condition": condition,
        "verdict": "unknown", "reason": _REASON_TAINTED_BY_LOOP, "boundary_inputs": [],
    })


def _merge_envs(local_guard, env_true: dict[str, tuple[Any, str]] | None,
                 env_false: dict[str, tuple[Any, str]] | None, ctx: _Ctx) -> dict[str, tuple[Any, str]]:
    """The post-join environment for a LOCAL two-way split — an if/else's
    two arms, or a loop's "entered" vs "never entered" split. See the
    module docstring's MERGING ENVIRONMENTS section.

    `env_true`/`env_false` is `None` when that side's own path is CLOSED
    (an unconditional `return` — never applicable to the loop case, which
    always calls this with both sides present) and contributes nothing to
    the merge; the other side is then used AS IS, with no `If` needed.

    A variable present in only one of the two (surviving) envs — assigned
    in only one arm — is dropped from the result rather than guessed at:
    `_translate`'s `ast.Name` case already raises `_TranslateError`
    ("undefined name") for a later reference to it, which is the exactly
    correct answer on the path that never assigned it (Python itself would
    raise `UnboundLocalError` there).
    """
    if env_true is None:
        return env_false
    if env_false is None:
        return env_true
    z3 = ctx.z3
    merged: dict[str, tuple[Any, str]] = {}
    for name in set(env_true) & set(env_false):
        entry_true = env_true[name]
        entry_false = env_false[name]
        if entry_true is entry_false:
            merged[name] = entry_true  # neither arm reassigned it — no If needed
            continue
        val_t, dt_t = entry_true
        val_f, dt_f = entry_false
        if dt_t != dt_f:
            raise _TranslateError(
                f"{name!r} holds different types across branches ({dt_t} vs {dt_f})"
            )
        merged[name] = (z3.If(local_guard, val_t, val_f), dt_t)
    return merged


def _walk_if(node: ast.If, env: dict[str, tuple[Any, str]], parent_pieces: list[str],
             parent_cond, ctx: _Ctx, *, kind: str) -> tuple[bool, Any, dict[str, tuple[Any, str]]]:
    z3 = ctx.z3
    guard_val, guard_dt = _translate(node.test, env, ctx)
    if guard_dt != "bool":
        raise _TranslateError("if/elif condition is not a bool expression")
    guard_text = _unparse(node.test)
    if _mentions_tainted(guard_val, ctx):
        # This guard depends on a value a (non-unrolled) loop computed —
        # see the module docstring's LOOPS section. z3 would call the
        # fresh, unconstrained symbol `sat` for almost anything, which
        # would silently launder "we do not know" into a false
        # `reachable`, so this is recorded `unknown` directly and NEITHER
        # arm is walked: whatever either arm would assign is exactly the
        # unresolved question this conservative approximation declines to
        # guess at. The whole if/elif/else is treated as an opaque
        # pass-through — falls through unconditionally, with the
        # environment UNCHANGED — the same "over-approximate rather than
        # guess" rule an untranslatable guard already follows.
        _record_tainted(ctx, line=node.lineno, kind=kind,
                         condition=" and ".join(parent_pieces + [guard_text]) or guard_text)
        return True, parent_cond, env
    if_cond = z3.And(parent_cond, guard_val)
    _record(ctx, line=node.lineno, kind=kind, condition=" and ".join(parent_pieces + [guard_text]) or guard_text,
            cond=if_cond, guard_node=node.test, parent_cond=parent_cond, env=env)
    body_falls, body_cont, body_env = _walk_block(node.body, dict(env), ctx, if_cond)

    not_text = f"not ({guard_text})"
    else_cond = z3.And(parent_cond, z3.Not(guard_val))
    else_pieces = parent_pieces + [not_text]
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        else_falls, else_cont, else_env = _walk_if(
            node.orelse[0], dict(env), else_pieces, else_cond, ctx, kind="elif")
    elif node.orelse:
        _record(ctx, line=node.orelse[0].lineno, kind="else", condition=" and ".join(else_pieces),
                cond=else_cond, guard_node=None, parent_cond=parent_cond, env=env)
        else_falls, else_cont, else_env = _walk_block(node.orelse, dict(env), ctx, else_cond)
    else:
        # An implicit, empty else: its "arm" is simply the pre-if bindings,
        # unchanged — exactly the fallback `_merge_envs` needs for a variable
        # only assigned in the `if` body.
        else_falls, else_cont, else_env = True, else_cond, dict(env)

    conds = [c for ok, c in ((body_falls, body_cont), (else_falls, else_cont)) if ok and c is not None]
    if not conds:
        return False, None, env
    merge_cond = conds[0] if len(conds) == 1 else z3.Or(*conds)
    merged_env = _merge_envs(guard_val, body_env if body_falls else None,
                              else_env if else_falls else None, ctx)
    return True, merge_cond, merged_env


def _walk_loop(node: ast.While | ast.For, env: dict[str, tuple[Any, str]], ctx: _Ctx,
               cur_cond) -> dict[str, tuple[Any, str]]:
    """Dispatch to an exact unroll (`for` within `_MAX_UNROLL_ITERATIONS`)
    or the conservative, taint-based walk (`while`, or a `for` above the
    cap) — see the module docstring's LOOPS section for why these are two
    genuinely different mechanisms rather than one approximation applied
    uniformly.
    """
    if isinstance(node, ast.For):
        start, stop, step = _static_range_bounds(node.iter)
        count = len(range(start, stop, step))
        if count <= _MAX_UNROLL_ITERATIONS:
            return _walk_for_unrolled(node, env, ctx, cur_cond, range(start, stop, step))
        return _walk_loop_conservative(node, env, ctx, cur_cond, static_bounds=(start, stop, step))
    return _walk_loop_conservative(node, env, ctx, cur_cond, static_bounds=None)


def _walk_for_unrolled(node: ast.For, env: dict[str, tuple[Any, str]], ctx: _Ctx, cur_cond,
                        values: range) -> dict[str, tuple[Any, str]]:
    """Exact reachability for a `for x in range(<static>)` loop with at
    most `_MAX_UNROLL_ITERATIONS` iterations: the body is walked ONCE PER
    CONCRETE VALUE of the loop variable, threading the environment
    sequentially (real symbolic execution, not the merge-based
    approximation the conservative path uses) — so the post-loop state is
    EXACT, not an over- or under-approximation. See the module docstring's
    LOOPS section.

    Branches inside the body are recorded once PER SOURCE LINE, aggregated
    across iterations by `ctx.commit_branch`/`flush_unroll_frame`: reachable
    if any iteration's own path condition was sat, dead only if every
    iteration's was unsat.

    A pleasant side effect of real threading rather than merging: the loop
    target variable (`node.target.id`) ends up bound, after the loop, to
    its LAST iteration's concrete value — exactly Python's own scoping
    (a `for` target survives the loop) — for free, with no special-casing.
    An empty range leaves it unbound, also matching Python: nothing in
    `values` means the body, and the assignment to the target, never ran.
    """
    z3 = ctx.z3
    entered = len(values) > 0
    loop_entry_cond = z3.And(cur_cond, z3.BoolVal(entered))
    condition = _unparse(node.iter)
    _record(ctx, line=node.lineno, kind="for", condition=condition, cond=loop_entry_cond,
            guard_node=None, parent_cond=cur_cond, env=env)
    if not entered:
        return env

    ctx._unroll_stack.append({})
    cur_env = env
    for v in values:
        iter_env = dict(cur_env)
        iter_env[node.target.id] = (z3.IntVal(v), "int")
        # The body's own `falls_through`/`continuation_cond` are not used
        # here: an unconditional `return` inside an unrolled loop body is a
        # narrow, undocumented-elsewhere edge case this tool does not
        # attempt to reason about precisely (which concrete iterations
        # would even reach the return depends on the free parameters, not
        # just the loop variable) — env threading continues regardless,
        # the same "over-approximate rather than guess" default this
        # module already applies to constructs it cannot fully resolve.
        _, _, cur_env = _walk_block(node.body, iter_env, ctx, cur_cond)
    ctx.flush_unroll_frame()
    return cur_env


def _walk_loop_conservative(node: ast.While | ast.For, env: dict[str, tuple[Any, str]], ctx: _Ctx,
                             cur_cond, *, static_bounds: tuple[int, int, int] | None
                             ) -> dict[str, tuple[Any, str]]:
    """The ONE-representative-iteration walk for a `while` loop, or a `for`
    above `_MAX_UNROLL_ITERATIONS` — see the module docstring's LOOPS
    section. Two things distinguish this from a plain merge:

      * A branch inside the body that is UNSAT on this one iteration is
        `unknown` (`_REASON_FIRST_ITERATION_ONLY`), never `dead` — see
        `_decide`'s own handling of `ctx._conservative_loop_depth`. Only
        `sat` is trusted here: a real witness IS a real witness regardless
        of which iteration produced it.
      * Every variable the body assigns ANYWHERE (directly, or via a
        nested arm's own merge) is TAINTED afterward — rebound to a fresh,
        entirely unconstrained symbol (`ctx.fresh_tainted_symbol`) rather
        than merged with `_merge_envs`, which is exactly the mechanism
        that produced the BLOCKER this design replaces: an unconstrained-
        but-RANGE-BOUND loop variable made a value look like "any value in
        the range" instead of "some value this tool cannot pin down at
        all". A later guard mentioning a tainted symbol is caught by
        `_walk_if`'s own `_mentions_tainted` check and reported `unknown`
        (`_REASON_TAINTED_BY_LOOP`) without ever being solved.
    """
    z3 = ctx.z3
    if isinstance(node, ast.While):
        guard_val, guard_dt = _translate(node.test, env, ctx)
        if guard_dt != "bool":
            raise _TranslateError("while condition is not a bool expression")
        if _mentions_tainted(guard_val, ctx):
            _record_tainted(ctx, line=node.lineno, kind="while", condition=_unparse(node.test))
            return env
        loop_cond = z3.And(cur_cond, guard_val)
        condition = _unparse(node.test)
        _record(ctx, line=node.lineno, kind="while", condition=condition, cond=loop_cond,
                guard_node=node.test, parent_cond=cur_cond, env=env)
        body_env = dict(env)
    else:
        start, stop, step = static_bounds
        entered = start < stop if step > 0 else start > stop
        loop_cond = z3.And(cur_cond, z3.BoolVal(entered))
        condition = _unparse(node.iter)
        _record(ctx, line=node.lineno, kind="for", condition=condition, cond=loop_cond,
                guard_node=None, parent_cond=cur_cond, env=env)
        body_env = dict(env)
        # The loop variable is bound to an UNCONSTRAINED-but-in-range
        # symbolic Int for the purpose of analyzing the body once — sound
        # for branches INSIDE the body (a real value in the real range),
        # but everything the body assigns using it is tainted below rather
        # than merged, which is exactly what makes this sound for the code
        # AFTER the loop too.
        var = z3.Int(f"__loop_{node.lineno}_{node.target.id}")
        lo, hi = (start, stop - 1) if step > 0 else (stop + 1, start)
        body_env[node.target.id] = (var, "int")
        loop_cond = z3.And(loop_cond, var >= lo, var <= hi)

    ctx._conservative_loop_depth += 1
    try:
        _, _, body_env_after = _walk_block(node.body, body_env, ctx, loop_cond)
    finally:
        ctx._conservative_loop_depth -= 1

    tainted_env = dict(env)
    for name, entry in body_env_after.items():
        if name not in env or entry is env[name]:
            continue  # loop-local (e.g. the loop var), or never touched
        tainted_env[name] = (ctx.fresh_tainted_symbol(entry[1], name), entry[1])
    return tainted_env


def _apply_assign(stmt: ast.Assign, env: dict[str, tuple[Any, str]], ctx: _Ctx) -> None:
    if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
        raise _TranslateError("only a single Name target is supported for assignment")
    val, dt = _translate(stmt.value, env, ctx)
    env[stmt.targets[0].id] = (val, dt)


def _record_unknown(ctx: _Ctx, line: int, kind: str) -> None:
    """A branch whose OWN guard failed to translate — the "unsupported
    construct on the path" half of `verdict: "unknown"` (the module
    docstring's `_first_unsupported` scan is meant to catch this before it
    ever reaches here; this is the belt to that scan's braces — an
    undefined name is the one case it does not attempt, since name binding
    is a flow property, not a node-type property).
    """
    if not ctx.unrolling and len(ctx.branches) >= ctx.max_branches:
        ctx.truncated = True
        return
    ctx.commit_branch({"line": line, "kind": kind,
                        "condition": "<could not translate this guard>",
                        "verdict": "unknown", "boundary_inputs": []})


def _walk_block(stmts: list[ast.stmt], env: dict[str, tuple[Any, str]], ctx: _Ctx,
                 cur_cond) -> tuple[bool, Any, dict[str, tuple[Any, str]]]:
    """`(falls_through, continuation_cond, env)` for a straight-line list of
    statements — see the module docstring's PATH CONDITIONS and MERGING
    ENVIRONMENTS sections. The returned `env` is the (possibly merged)
    environment after every statement in `stmts` — the caller (an
    enclosing `_walk_if`/`_walk_loop`/`_walk_block`) must use IT for
    whatever comes next, not the `env` object it originally passed in:
    an `If`/`While`/`For` inside `stmts` can rebind `env` to a freshly
    merged dict, and mutating the ORIGINAL object further would silently
    discard that join.

    A guard that fails to translate (see `_record_unknown`) does not abort
    the whole analysis: that one branch is recorded `unknown` and the block
    is conservatively treated as falling through with the condition and
    environment UNCHANGED — this may under-report nested branches inside
    it, but never turns a real dead branch into a falsely-reachable one,
    the same "over-approximate rather than guess" rule the loop-body
    handling follows. An assignment that fails to translate is different
    in kind — every LATER statement in this function depends on `env`
    staying accurate — so that one propagates out to a top-level refusal
    instead (see `analyze`'s own try/except around this call).
    """
    for stmt in stmts:
        # Deliberately NOT short-circuited on `ctx.budget_exhausted()` here:
        # discovering a branch (walking the AST, translating a guard) is
        # cheap and unbounded-CPU-free, so it keeps happening even once the
        # time budget for SOLVING is gone. `_decide`/`_boundary_for` are the
        # only things that actually spend wall-clock time, and each already
        # degrades to `verdict: "unknown"` on its own once the deadline
        # passes — see `_Ctx.budget_exhausted`. Skipping discovery here too
        # would silently DROP a branch instead of reporting it `unknown`,
        # which is exactly the "timeout -> unknown, never a missing result"
        # contract this tool promises.
        if isinstance(stmt, (ast.Pass, ast.Expr)):
            continue
        if isinstance(stmt, ast.Assign):
            _apply_assign(stmt, env, ctx)
            continue
        if isinstance(stmt, ast.Return):
            return False, None, env
        if isinstance(stmt, ast.If):
            try:
                falls, cont, merged_env = _walk_if(stmt, env, [], cur_cond, ctx, kind="if")
            except _TranslateError:
                ctx.supported = False
                _record_unknown(ctx, stmt.lineno, "if")
                falls, cont, merged_env = True, cur_cond, env
            if not falls:
                return False, None, env
            cur_cond = cont
            env = merged_env
            continue
        if isinstance(stmt, ast.While):
            try:
                env = _walk_loop(stmt, env, ctx, cur_cond)
            except _TranslateError:
                ctx.supported = False
                _record_unknown(ctx, stmt.lineno, "while")
            continue
        if isinstance(stmt, ast.For):
            try:
                env = _walk_loop(stmt, env, ctx, cur_cond)
            except _TranslateError:
                ctx.supported = False
                _record_unknown(ctx, stmt.lineno, "for")
            continue
        raise _TranslateError(f"unsupported statement {type(stmt).__name__}")
    return True, cur_cond, env


def _param_type(name: str, annotation: ast.expr | None, inputs: dict[str, str] | None) -> str:
    if inputs and name in inputs:
        return inputs[name]
    if isinstance(annotation, ast.Name) and annotation.id in _TYPE_NAMES:
        return annotation.id
    return "int"


def analyze(language: str, code: str, inputs: dict[str, str] | None = None,
            timeout: int = _DEFAULT_TIMEOUT_S, max_branches: int = _DEFAULT_MAX_BRANCHES) -> dict[str, Any]:
    """Entry point `server.branch_reachability` calls. See the module
    docstring for the mechanism end to end.
    """
    refusal = _refuse_unsupported_language(language)
    if refusal is not None:
        return refusal

    if inputs is not None:
        bad = {k: v for k, v in inputs.items() if v not in _TYPE_NAMES}
        if bad:
            return _refusal(f"inputs {bad!r} must each be one of {_TYPE_NAMES}")

    timeout = max(1, min(int(timeout), _MAX_TIMEOUT_S))
    max_branches = max(1, min(int(max_branches), _HARD_MAX_BRANCHES))

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return _refusal(f"source does not parse: {exc}", line=getattr(exc, "lineno", None))

    # Runs BEFORE any structural (function-count / inputs-required) check —
    # a module-scope `class`, for instance, has zero top-level FunctionDefs,
    # and used to be refused with the generic "no top-level function and no
    # `inputs`" message instead of naming the actual construct and its line.
    # This scan is cheap (a single AST walk, no z3), so running it first
    # costs nothing and gives every unsupported-construct case the same,
    # more specific refusal.
    hit = _first_unsupported(tree)
    if hit is not None:
        construct, line = hit
        return _refusal(f"unsupported construct: {construct}", line=line)

    top_funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if len(top_funcs) > 1:
        return _refusal(
            f"branch_reachability analyzes exactly one top-level function; found {len(top_funcs)}",
            line=top_funcs[1].lineno,
        )

    if top_funcs:
        fn = top_funcs[0]
        args = fn.args
        if args.vararg or args.kwarg or args.kwonlyargs or args.posonlyargs:
            return _refusal("only plain positional-or-keyword parameters are supported "
                             "(no *args/**kwargs/keyword-only/positional-only)", line=fn.lineno)
        param_names = [a.arg for a in args.args]
        body = fn.body
    else:
        if not inputs:
            return _refusal(
                "no top-level function and no `inputs` — declare the program's free "
                "variables via `inputs` (name -> 'int'/'bool'/'str'), or write it as a "
                "single function whose parameters are the inputs"
            )
        param_names = list(inputs)
        body = tree.body
        fn = None

    z3 = optional.require("z3")

    resolved_inputs: dict[str, str] = {}
    params: dict[str, tuple[Any, str]] = {}
    for name in param_names:
        annotation = None
        if fn is not None:
            annotation = next((a.annotation for a in fn.args.args if a.arg == name), None)
        dtype = _param_type(name, annotation, inputs)
        resolved_inputs[name] = dtype
        if dtype == "int":
            sym = z3.Int(name)
        elif dtype == "bool":
            sym = z3.Bool(name)
        else:
            sym = z3.String(name)
        params[name] = (sym, dtype)

    ctx = _Ctx(z3, params, timeout, max_branches)
    env = dict(params)
    try:
        _walk_block(body, env, ctx, z3.BoolVal(True))
    except _TranslateError as exc:
        return _refusal(f"unsupported construct: {exc}")

    dead = sum(1 for b in ctx.branches if b["verdict"] == "dead")
    reachable = sum(1 for b in ctx.branches if b["verdict"] == "reachable")
    unknown = sum(1 for b in ctx.branches if b["verdict"] == "unknown")

    seen: set[tuple] = set()
    suggested: list[dict[str, Any]] = []
    for b in sorted(ctx.branches, key=lambda x: x["line"]):
        candidates = []
        if "witness" in b:
            candidates.append(b["witness"])
        for entry in b["boundary_inputs"]:
            for key in ("min_input", "max_input", "equality_edge_input"):
                if entry.get(key) is not None:
                    candidates.append(entry[key])
        for c in candidates:
            key = tuple(sorted(c.items()))
            if key not in seen:
                seen.add(key)
                suggested.append(c)

    return contract.stamp({
        "ok": True,
        "supported": ctx.supported,
        "inputs": resolved_inputs,
        "branches": ctx.branches,
        "dead_count": dead,
        "reachable_count": reachable,
        "unknown_count": unknown,
        "truncated": ctx.truncated,
        "suggested_test_inputs": suggested,
    })
