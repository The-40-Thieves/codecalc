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
    iteration). The N copies are CHAINED exactly like N consecutive `if`
    statements — copy `k+1` starts from copy `k`'s own `continuation_cond`,
    not the original path condition — so an unconditional `return` reached
    at some concrete iteration correctly closes off every later copy
    (their own branches still discovered and reported `dead`, never
    dropped) and whatever comes after the loop. Post-loop state is
    therefore EXACT, including the loop target variable itself, which —
    matching real Python scoping — ends up bound to its LAST iteration's
    value.
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
        # > 0 while walking statements sequentially AFTER a construct whose
        # own closure behaviour (does it unconditionally `return`, or not?)
        # this tool declined to resolve — see `_REASON_AFTER_UNRESOLVED_RETURN`
        # and `_walk_block`'s bookkeeping around this counter. `_decide`
        # downgrades EITHER a sat or an unsat result to `unknown` while this
        # is positive: unlike `_conservative_loop_depth` (where a `sat`
        # witness is still trustworthy), here a witness for code after an
        # unresolved-closure point may never be reached at runtime, and a
        # `dead` claim may be wrong whenever the unresolved return does not
        # actually fire.
        self._unresolved_closure_depth = 0
        # One-shot signal: set by `_walk_if` immediately before returning
        # from its tainted-guard opaque-pass-through path, consumed (and
        # cleared) by `_walk_block` right after the call that might have set
        # it. Plain instance state rather than a return value so it composes
        # with the existing `(falls_through, continuation_cond, env)`
        # contract every caller of `_walk_if`/`_walk_loop` already relies on.
        self._pending_closure_taint = False

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


#: Set only for the conservative-loop / tainted-guard cases `_decide` can
#: produce — never for a plain solver timeout or an ordinary dead/reachable
#: verdict, which say nothing more than the verdict itself already does.
_REASON_FIRST_ITERATION_ONLY = "loop body analysed for the first iteration only"
_REASON_TAINTED_BY_LOOP = "depends on a value computed by a loop"
#: A guard EARLIER in the same block had a tainted condition (see
#: `_REASON_TAINTED_BY_LOOP`) and was therefore treated as an opaque
#: pass-through: neither of ITS OWN arms was walked, so this tool does not
#: know whether one of them contained an unconditional `return` that would
#: have closed off everything after it. Trusting a `sat`/`unsat` verdict
#: for code sequentially AFTER that point would mean trusting a witness
#: that might never actually be reached at runtime (the earlier, unresolved
#: `return` may fire first) or a `dead` claim for code that IS reached
#: whenever that return does not fire — see `_Ctx._unresolved_closure_depth`
#: and the differential fuzz failure that found this: a `return` gated on a
#: tainted condition silently failed to close off a `reachable` claim (with
#: a witness that, in real execution, never got there) for code several
#: statements later in the same block.
_REASON_AFTER_UNRESOLVED_RETURN = "may be preceded by a return this tool could not resolve"


def _mentions_tainted(expr, ctx: _Ctx) -> bool:
    """Whether `expr`'s own z3 AST contains, ANYWHERE (including buried
    inside arithmetic built on top of it — `_apply_assign` never launders
    this away, since the new binding's z3 expression literally embeds the
    tainted term), a constant declared with one of `ctx.tainted_names`.

    A plain iterative walk over `.children()`, not a z3-provided utility:
    z3's own AST nodes are DAGs (a shared subterm appears once but is
    referenced from multiple parents), so visited-tracking is what keeps
    this from doing exponential re-work on a deeply-nested expression, the
    same reasoning `_boundary_for`'s own bounded work already applies
    elsewhere in this module. The visited key is `node.get_id()` — z3's
    OWN hash-consing id for the underlying AST node — never Python's
    `id()`: z3's Python bindings mint a FRESH wrapper object on every
    `.children()` call rather than interning one wrapper per underlying
    node, so a wrapper can be garbage-collected and its `id()` reused by
    an unrelated later node WITHIN THE SAME WALK. A confirmed differential
    fuzz failure traced to exactly this: a tainted symbol nested inside a
    z3.If's second branch was skipped as an already-"seen" duplicate of a
    completely different node that happened to reuse its freed Python
    object's memory address, silently laundering a false `reachable`
    verdict (with a fabricated witness) out of what should have been
    `unknown`. `get_id()` returns the SAME integer for every wrapper
    around the same underlying (hash-consed) node, so it has no such
    aliasing risk.
    """
    if not ctx.tainted_names:
        return False
    stack = [expr]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        try:
            node_id = node.get_id()
        except Exception:
            node_id = None
        if node_id is not None:
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


def _take_closure_taint(ctx: _Ctx) -> bool:
    """Read-and-clear `ctx._pending_closure_taint` — the one-shot signal a
    just-completed `_walk_if`/`_walk_loop`/`_walk_block` call may have left
    behind to mean "something I walked had an unresolved (tainted) guard
    that MIGHT have returned, and I could not tell". Every caller that
    makes such a call reads it with this helper immediately afterward,
    both to decide whether ITS OWN subsequent statements need
    `ctx._unresolved_closure_depth` raised (see `_decide`), and to fold
    into the aggregate signal it leaves for ITS OWN caller in turn before
    returning — read-and-clear rather than a plain read so two sibling
    calls (an `if`'s body, then separately its `else`) never see the
    other's leftover signal."""
    taken = ctx._pending_closure_taint
    ctx._pending_closure_taint = False
    return taken


def _decide(cond, ctx: _Ctx) -> tuple[str, dict[str, Any] | None, str | None]:
    """`(verdict, witness, reason)` for one path condition. `witness` is
    `None` unless `verdict == "reachable"`; `reason` is set for either
    conservative downgrade this can apply:

      * UNSAT while `ctx._conservative_loop_depth > 0` becomes `unknown`
        (`_REASON_FIRST_ITERATION_ONLY`) — see the module docstring's LOOPS
        section for why that only proves "not reachable on the first
        iteration", never "dead".
      * SAT while `ctx._unresolved_closure_depth > 0` becomes `unknown`
        (`_REASON_AFTER_UNRESOLVED_RETURN`) instead of `reachable`: `cond`
        here is missing a conjunct this tool could not model — "the
        earlier, tainted-guarded `return` it declined to resolve did NOT
        fire" — so `cond`'s solution set is a SUPERSET of the true
        reachable set. A `sat` witness in that superset might not be in
        the true set (not trustworthy), but an UNSAT result still proves
        the (smaller) true set is ALSO empty — dropping a required
        conjunct only WIDENS what solves, so unsatisfiability of the wider
        condition survives narrowing — which is why only the sat direction
        needs downgrading here, unlike the loop case above.
    """
    if ctx.budget_exhausted():
        return "unknown", None, None
    z3 = ctx.z3
    solver = z3.Solver()
    solver.set("timeout", ctx.call_timeout_ms())
    solver.add(cond)
    verdict = solver.check()
    if verdict == z3.sat:
        if ctx._unresolved_closure_depth > 0:
            return "unknown", None, _REASON_AFTER_UNRESOLVED_RETURN
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
        # `boundary_inputs`' own min/max/equality-edge inputs are each a
        # witness for a specific edge value — exactly as untrustworthy as
        # the branch's own `reachable` witness would be here, for the same
        # reason (see `_decide`'s docstring) — so skipped downstream of an
        # unresolved closure point, the same "no boundary_inputs" rule
        # `_record_tainted` already applies to its own directly-tainted
        # guard.
        boundary = (_boundary_for(guard_node, parent_cond, cond, env, ctx)
                    if guard_node is not None and ctx._unresolved_closure_depth == 0 else [])
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
        # Signal outward (see `_take_closure_taint`): neither arm was
        # walked, so this tool genuinely does not know whether one of them
        # contained an unconditional `return` — a confirmed differential
        # fuzz failure traced a false `reachable` verdict (with a witness
        # that never actually got there at runtime) to code SEVERAL
        # STATEMENTS LATER in the same block trusting a solve that this
        # opaque pass-through should have made untrustworthy. The caller
        # (`_walk_block`, or an enclosing `_walk_if` composing an
        # elif-chain) is responsible for turning this into
        # `ctx._unresolved_closure_depth` for whatever it walks next.
        ctx._pending_closure_taint = True
        return True, parent_cond, env
    if_cond = z3.And(parent_cond, guard_val)
    _record(ctx, line=node.lineno, kind=kind, condition=" and ".join(parent_pieces + [guard_text]) or guard_text,
            cond=if_cond, guard_node=node.test, parent_cond=parent_cond, env=env)
    body_falls, body_cont, body_env = _walk_block(node.body, dict(env), ctx, if_cond)
    body_taint = _take_closure_taint(ctx)

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
    else_taint = _take_closure_taint(ctx)

    conds = [c for ok, c in ((body_falls, body_cont), (else_falls, else_cont)) if ok and c is not None]
    if not conds:
        return False, None, env
    merge_cond = conds[0] if len(conds) == 1 else z3.Or(*conds)
    merged_env = _merge_envs(guard_val, body_env if body_falls else None,
                              else_env if else_falls else None, ctx)
    # This whole if/elif/else is closure-uncertain for WHATEVER COMES NEXT
    # if either SURVIVING arm (one that didn't itself definitely `return`)
    # was — exactly the same "only surviving arms contribute" rule
    # `merge_cond` above already applies to continuation conditions.
    ctx._pending_closure_taint = (body_taint and body_falls) or (else_taint and else_falls)
    return True, merge_cond, merged_env


def _walk_loop(node: ast.While | ast.For, env: dict[str, tuple[Any, str]], ctx: _Ctx,
               cur_cond) -> tuple[bool, Any, dict[str, tuple[Any, str]]]:
    """`(falls_through, continuation_cond, env)` — the SAME triple
    `_walk_block`/`_walk_if` return, for the SAME reason: a loop can close
    off everything after it exactly as a bare `return` can (see
    `_walk_for_unrolled`'s own docstring for the case that matters — an
    unconditional `return` reached at SOME concrete iteration). Dispatches
    to an exact unroll (`for` within `_MAX_UNROLL_ITERATIONS`) or the
    conservative, taint-based walk (`while`, or a `for` above the cap) —
    see the module docstring's LOOPS section for why these are two
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
                        values: range) -> tuple[bool, Any, dict[str, tuple[Any, str]]]:
    """Exact reachability for a `for x in range(<static>)` loop with at
    most `_MAX_UNROLL_ITERATIONS` iterations. The N concrete-value copies
    of the body are NOT independent: they are fed through the exact SAME
    sequential-statement machinery `_walk_block` already uses for two
    consecutive `if` statements — copy `k+1` is walked starting from copy
    `k`'s own `continuation_cond`, not from the ORIGINAL `cur_cond` — so
    real symbolic execution, not merging, produces the EXACT post-loop
    state, AND an unconditional `return` reached at some concrete
    iteration correctly closes off every later copy and whatever comes
    after the loop, exactly as it would for two consecutive `if`
    statements today.

    A prior cut hand-threaded the environment across copies but fed every
    copy the SAME, unnarrowed `cur_cond`, discarding each copy's own
    `falls_through`/`continuation_cond` — confirmed by a THIRD review to
    under-count what a return closes off: `x = -1; for i in range(5): if
    i == 2: return 100 \\n if i == 4: y = 99` reported the post-loop `if y
    == 99` REACHABLE with a witness, when every real call returns at
    `i == 2` and the loop body never even reaches `i == 4` — verified by
    direct execution. `closed` below is what fixes it: once ANY copy's own
    walk reports `falls_through=False` (a return with no wrapping
    condition left to narrow — the same structural signal `_walk_block`'s
    own `ast.Return` case reports), every REMAINING copy is still walked
    (so its own internal branches are discovered and correctly reported
    `dead`, rather than silently omitted) but under an UNSATISFIABLE path
    condition, and the loop's own final `(falls_through, continuation_
    cond)` reported to ITS caller is `(False, None, ...)` — the loop
    closes the enclosing block exactly as a bare `return` would.

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
        ctx._pending_closure_taint = False
        return True, cur_cond, env

    ctx._unroll_stack.append({})
    iter_cond = cur_cond
    cur_env = env
    closed = False
    closure_uncertain = False
    bumped = False
    last_falls, last_cont = True, cur_cond
    try:
        for v in values:
            iter_env = dict(cur_env)
            iter_env[node.target.id] = (z3.IntVal(v), "int")
            falls, cont, cur_env = _walk_block(node.body, iter_env, ctx, iter_cond)
            # This copy's own body may have hit a tainted-guard (or
            # untranslatable-guard) opaque pass-through — same signal
            # `_walk_block` leaves for any caller, see `_take_closure_taint`.
            # Once true, it stays true for every LATER copy too (chained
            # sequentially, exactly like consecutive statements — an
            # unresolved "might have returned" earlier in the chain makes
            # everything after it equally unresolved) and for whatever
            # comes after the whole loop.
            if _take_closure_taint(ctx):
                closure_uncertain = True
                if not bumped:
                    ctx._unresolved_closure_depth += 1
                    bumped = True
            last_falls, last_cont = falls, cont
            if falls:
                iter_cond = cont
            else:
                # A bare return in THIS copy closes everything from here on —
                # keep walking the remaining copies (for discovery: their own
                # branches must still be reported `dead`, not dropped), but
                # under a path condition nothing can ever satisfy.
                closed = True
                iter_cond = z3.BoolVal(False)
    finally:
        if bumped:
            ctx._unresolved_closure_depth -= 1
        ctx.flush_unroll_frame()
    if closed:
        ctx._pending_closure_taint = False  # falls=False — caller won't use it
        return False, None, cur_env
    ctx._pending_closure_taint = closure_uncertain
    return last_falls, last_cont, cur_env


def _contains_return(stmts: list[ast.stmt]) -> bool:
    """Whether ANY `return` appears anywhere in `stmts`, at any nesting
    depth (inside a nested if/while/for included) — a purely syntactic
    check, independent of whether that `return` is provably reachable.
    Used by `_walk_loop_conservative` to tell "this loop body can never
    return, so falling through is the only possibility" (no closure risk
    at all) apart from "it might, on some iteration this one-iteration
    walk does not model" (closure risk that must propagate downstream).
    """
    return any(isinstance(n, ast.Return) for stmt in stmts for n in ast.walk(stmt))


def _walk_loop_conservative(node: ast.While | ast.For, env: dict[str, tuple[Any, str]], ctx: _Ctx,
                             cur_cond, *, static_bounds: tuple[int, int, int] | None
                             ) -> tuple[bool, Any, dict[str, tuple[Any, str]]]:
    """`(falls_through, continuation_cond, env)` for the ONE-representative-
    iteration walk of a `while` loop, or a `for` above
    `_MAX_UNROLL_ITERATIONS` — see the module docstring's LOOPS section.

    A FOURTH review confirmed the first three fixes by direct execution and
    found this function was still unsound: it used to hardcode
    `falls_through = True` regardless of what the one-iteration walk
    itself reported, on the theory that this mechanism "does not attempt
    to reason about whether the body's own return closes off the loop,
    only about VALUE taint". That is fine for a body that CANNOT return at
    all, but wrong whenever it can: `if x == 1: n = 0; while n < 4: return
    6` reported the LATER `if x == 1: return 999` reachable with witness
    `{x: 1}`, when `f(1)` returns `6` at the `while` and never gets there —
    confirmed by direct execution, and by a multi-seed run of the
    differential corpus (the shipped seed/size happened not to hit it).

    The one-iteration body walk (`_walk_block` below) already tells us,
    via its own `(falls_through, continuation_cond)`, exactly how much a
    single representative iteration can be trusted to say about closure —
    the fix is to USE that instead of discarding it, in three cases:

      * The body has NO `return` anywhere (`_contains_return` is False):
        nothing to close with. Unchanged from before — falls through,
        `cur_cond` unchanged, only VALUE taint applies.
      * The body falls through this one iteration (`falls_through True`)
        but DOES contain a `return` somewhere: some OTHER, unmodeled
        iteration might take it — this walk proves nothing about whether
        it does. `ctx._pending_closure_taint` is set (see `_walk_if`'s own
        docstring for the mechanism: `_decide` then reports `unknown`,
        never `reachable`, for anything sequentially after this loop,
        while still allowing `dead` — dropping a required conjunct only
        WIDENS what solves). The one-iteration walk's OWN `continuation_
        cond` is also conjoined in, rather than discarded for bare
        `cur_cond`: not reaching a return in the FIRST iteration is a
        REAL necessary condition for ever falling out of the loop, so
        keeping it is strictly more precise, for free.
      * The body does NOT fall through this one iteration at all
        (`falls_through False`) — every path through it, for ANY input
        that enters the loop, reaches a `return`. This is EXACT, not a
        widening: entering the loop AT ALL means returning, so falling
        through the WHOLE loop requires never entering it —
        `z3.And(cur_cond, z3.Not(entry_guard))` — with no `_unresolved_
        closure_depth` bump needed, and branches inside the body keep
        whatever `reachable`/`unknown` verdicts the walk already gave
        them (`_conservative_loop_depth`'s existing UNSAT-downgrade is
        untouched).

    Two things distinguish this from a plain merge, in every case above:

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
            ctx._pending_closure_taint = True  # same opaque pass-through as `_walk_if`'s
            return True, cur_cond, env
        loop_cond = z3.And(cur_cond, guard_val)
        condition = _unparse(node.test)
        _record(ctx, line=node.lineno, kind="while", condition=condition, cond=loop_cond,
                guard_node=node.test, parent_cond=cur_cond, env=env)
        body_env = dict(env)
        entry_guard = guard_val
    else:
        start, stop, step = static_bounds
        entered = start < stop if step > 0 else start > stop
        entry_guard = z3.BoolVal(entered)
        loop_cond = z3.And(cur_cond, entry_guard)
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
        body_falls, body_cont, body_env_after = _walk_block(node.body, body_env, ctx, loop_cond)
    finally:
        ctx._conservative_loop_depth -= 1
    # The body itself may have hit its own opaque pass-through (a tainted
    # or untranslatable guard) — propagate that same uncertainty onward,
    # for the same reason `_walk_if` propagates it out of an if/else.
    body_taint = _take_closure_taint(ctx)

    tainted_env = dict(env)
    for name, entry in body_env_after.items():
        if name not in env or entry is env[name]:
            continue  # loop-local (e.g. the loop var), or never touched
        tainted_env[name] = (ctx.fresh_tainted_symbol(entry[1], name), entry[1])

    if not body_falls:
        # EXACT: entering the loop at all means returning during this
        # representative iteration, on ANY input that reaches it (that is
        # what `falls_through=False` from `_walk_block` already means) —
        # so falling through the whole loop requires never entering it.
        ctx._pending_closure_taint = body_taint
        return True, z3.And(cur_cond, z3.Not(entry_guard)), tainted_env
    if _contains_return(node.body):
        # WIDENING, not exact: this one modeled iteration happens not to
        # return, but some OTHER iteration this walk never sees might —
        # see this function's own docstring. `body_cont` (a necessary
        # condition for not returning on THIS iteration) is kept rather
        # than discarded for bare `cur_cond`, and the closure-uncertainty
        # signal downgrades anything sequentially after this loop from
        # `reachable` to `unknown` (never touches `dead`).
        ctx._pending_closure_taint = True
        return True, body_cont, tainted_env
    # No `return` anywhere in the body: nothing to close off with, so this
    # loop cannot narrow or taint the CONTROL FLOW of what follows, only
    # the VALUES the body touched (handled above).
    ctx._pending_closure_taint = body_taint
    return True, cur_cond, tainted_env


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

    Both of those "opaque pass-through" cases above — and a tainted guard
    (see `_walk_if`) — mean this function does not know whether the
    statement it just walked contained an unconditional `return`. Once
    one of them fires, every LATER statement in THIS SAME call has
    `ctx._unresolved_closure_depth` raised for as long as this call is on
    the stack (`_decide` then reports `unknown` rather than a `reachable`
    witness that a real run might never get to — see its own docstring),
    and the aggregate is left on `ctx._pending_closure_taint` for
    whichever caller (an enclosing `_walk_if`'s body/else, another
    `_walk_block`, or `_walk_for_unrolled`'s next copy) reads it via
    `_take_closure_taint` right after this call returns — exactly the same
    propagation `falls_through`/`continuation_cond` already do, just for
    "is the path we're returning even certain to exist" instead of "what
    condition reaches it".
    """
    closure_uncertain = False
    bumped = False
    try:
        for stmt in stmts:
            # Deliberately NOT short-circuited on `ctx.budget_exhausted()`
            # here: discovering a branch (walking the AST, translating a
            # guard) is cheap and unbounded-CPU-free, so it keeps happening
            # even once the time budget for SOLVING is gone.
            # `_decide`/`_boundary_for` are the only things that actually
            # spend wall-clock time, and each already degrades to `verdict:
            # "unknown"` on its own once the deadline passes — see
            # `_Ctx.budget_exhausted`. Skipping discovery here too would
            # silently DROP a branch instead of reporting it `unknown`,
            # which is exactly the "timeout -> unknown, never a missing
            # result" contract this tool promises.
            if isinstance(stmt, (ast.Pass, ast.Expr)):
                continue
            if isinstance(stmt, ast.Assign):
                _apply_assign(stmt, env, ctx)
                continue
            if isinstance(stmt, ast.Return):
                return False, None, env
            stmt_taint = False
            if isinstance(stmt, ast.If):
                try:
                    falls, cont, merged_env = _walk_if(stmt, env, [], cur_cond, ctx, kind="if")
                    stmt_taint = _take_closure_taint(ctx)
                except _TranslateError:
                    ctx.supported = False
                    _record_unknown(ctx, stmt.lineno, "if")
                    falls, cont, merged_env = True, cur_cond, env
                    stmt_taint = True  # same opaque pass-through risk as a tainted guard
                if not falls:
                    return False, None, env
                cur_cond = cont
                env = merged_env
            elif isinstance(stmt, ast.While):
                try:
                    falls, cont, env = _walk_loop(stmt, env, ctx, cur_cond)
                    stmt_taint = _take_closure_taint(ctx)
                except _TranslateError:
                    ctx.supported = False
                    _record_unknown(ctx, stmt.lineno, "while")
                    falls, cont = True, cur_cond
                    stmt_taint = True
                if not falls:
                    return False, None, env
                cur_cond = cont
            elif isinstance(stmt, ast.For):
                try:
                    falls, cont, env = _walk_loop(stmt, env, ctx, cur_cond)
                    stmt_taint = _take_closure_taint(ctx)
                except _TranslateError:
                    ctx.supported = False
                    _record_unknown(ctx, stmt.lineno, "for")
                    falls, cont = True, cur_cond
                    stmt_taint = True
                if not falls:
                    return False, None, env
                cur_cond = cont
            else:
                raise _TranslateError(f"unsupported statement {type(stmt).__name__}")
            if stmt_taint:
                closure_uncertain = True
                if not bumped:
                    ctx._unresolved_closure_depth += 1
                    bumped = True
        return True, cur_cond, env
    finally:
        if bumped:
            ctx._unresolved_closure_depth -= 1
        ctx._pending_closure_taint = closure_uncertain


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
