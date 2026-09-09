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
if/elif/else — `_walk_block` returns `(falls_through, continuation_cond)`
for exactly that reason: `continuation_cond` is `None` when every arm
returns, and the `or` of whichever arms didn't otherwise.

LOOPS ARE NOT UNROLLED. `while` and `for` bodies are analyzed as ONE
representative iteration — real reachability of a branch nested inside a
loop, real refusal of anything unsupported inside it, but the loop's own
effect on variables is NOT carried into the code AFTER the loop (this
module walks the loop body against a COPY of the environment and discards
it afterward). A precise account would need loop-invariant reasoning this
tool does not attempt; the conservative choice — the body's own bindings do
not leak out — never turns a genuinely dead branch into a falsely reachable
one and is documented, not hidden, in the design note.

BOUNDARY INPUTS. For every `Compare` node appearing in an arm's OWN guard
(not the accumulated ancestors — see the design note for why that line was
drawn there) with at least one int/str-length side, `_boundary_for` uses a
z3 `Optimize` to find the MINIMUM and MAXIMUM value of that side reachable
under the arm's full path condition (boxed to
`_OPT_BOUND` so an unbounded objective still terminates — see the design
note), plus, when the OTHER side is a literal constant, the "equality
edge" — a witness where the compared expression equals that literal,
checked against the guards ABOVE this one only, so the edge shows up even
when it sits just outside the branch currently being reported on.

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
#: Per-z3-call deadline never exceeds this, regardless of how much of the
#: caller's own `timeout` budget remains — one pathological call must not
#: eat the whole response, the same reasoning logic.z3_check's fixed
#: 5000ms default embodies, just derived from the caller's own budget here
#: instead of a constant.
_PER_CALL_TIMEOUT_MS_CAP = 5_000
#: Box constraint added ONLY to the Optimize calls behind `boundary_inputs`
#: (never to the reachability check itself) so a min/max search over an
#: unbounded int objective terminates instead of running forever looking
#: for a smaller (or larger) satisfying value that does not exist. Python
#: ints are unbounded; z3 Ints are too. A real boundary outside this box
#: is reported as absent, not wrong — see the design note.
_OPT_BOUND = 1_000_000

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

    def time_left_ms(self) -> int:
        remaining = self.deadline - time.monotonic()
        return max(0, int(remaining * 1000))

    def call_timeout_ms(self) -> int:
        return max(1, min(self.time_left_ms(), _PER_CALL_TIMEOUT_MS_CAP))

    def budget_exhausted(self) -> bool:
        return time.monotonic() >= self.deadline

    def witness(self, model) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, (sym, dtype) in self.params.items():
            val = model.eval(sym, model_completion=True)
            if dtype == "int":
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


def _decide(cond, ctx: _Ctx) -> tuple[str, dict[str, Any] | None]:
    """`(verdict, witness)` for one path condition. `witness` is `None`
    unless `verdict == "reachable"`.
    """
    if ctx.budget_exhausted():
        return "unknown", None
    z3 = ctx.z3
    solver = z3.Solver()
    solver.set("timeout", ctx.call_timeout_ms())
    solver.add(cond)
    verdict = solver.check()
    if verdict == z3.sat:
        return "reachable", ctx.witness(solver.model())
    if verdict == z3.unsat:
        return "dead", None
    return "unknown", None


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

        opt = z3.Optimize()
        opt.set("timeout", ctx.call_timeout_ms())
        opt.add(full_cond, objective >= -_OPT_BOUND, objective <= _OPT_BOUND)
        h_min = opt.minimize(objective)
        entry["min_input"] = ctx.witness(opt.model()) if opt.check() == z3.sat else None

        opt2 = z3.Optimize()
        opt2.set("timeout", ctx.call_timeout_ms())
        opt2.add(full_cond, objective >= -_OPT_BOUND, objective <= _OPT_BOUND)
        opt2.maximize(objective)
        entry["max_input"] = ctx.witness(opt2.model()) if opt2.check() == z3.sat else None

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
    if len(ctx.branches) >= ctx.max_branches:
        ctx.truncated = True
        return
    try:
        verdict, witness = _decide(cond, ctx)
        boundary = _boundary_for(guard_node, parent_cond, cond, env, ctx) if guard_node is not None else []
    except _TranslateError:
        verdict, witness, boundary = "unknown", None, []
        ctx.supported = False
    entry: dict[str, Any] = {"line": line, "kind": kind, "condition": condition, "verdict": verdict}
    if witness is not None:
        entry["witness"] = witness
    entry["boundary_inputs"] = boundary
    ctx.branches.append(entry)


def _walk_if(node: ast.If, env: dict[str, tuple[Any, str]], parent_pieces: list[str],
             parent_cond, ctx: _Ctx, *, kind: str) -> tuple[bool, Any]:
    z3 = ctx.z3
    guard_val, guard_dt = _translate(node.test, env, ctx)
    if guard_dt != "bool":
        raise _TranslateError("if/elif condition is not a bool expression")
    guard_text = _unparse(node.test)
    if_cond = z3.And(parent_cond, guard_val)
    _record(ctx, line=node.lineno, kind=kind, condition=" and ".join(parent_pieces + [guard_text]) or guard_text,
            cond=if_cond, guard_node=node.test, parent_cond=parent_cond, env=env)
    body_falls, body_cont = _walk_block(node.body, dict(env), ctx, if_cond)

    not_text = f"not ({guard_text})"
    else_cond = z3.And(parent_cond, z3.Not(guard_val))
    else_pieces = parent_pieces + [not_text]
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        else_falls, else_cont = _walk_if(node.orelse[0], dict(env), else_pieces, else_cond, ctx, kind="elif")
    elif node.orelse:
        _record(ctx, line=node.orelse[0].lineno, kind="else", condition=" and ".join(else_pieces),
                cond=else_cond, guard_node=None, parent_cond=parent_cond, env=env)
        else_falls, else_cont = _walk_block(node.orelse, dict(env), ctx, else_cond)
    else:
        else_falls, else_cont = True, else_cond

    conds = [c for ok, c in ((body_falls, body_cont), (else_falls, else_cont)) if ok and c is not None]
    if not conds:
        return False, None
    return True, conds[0] if len(conds) == 1 else z3.Or(*conds)


def _walk_loop(node: ast.While | ast.For, env: dict[str, tuple[Any, str]], ctx: _Ctx, cur_cond) -> None:
    z3 = ctx.z3
    if isinstance(node, ast.While):
        guard_val, guard_dt = _translate(node.test, env, ctx)
        if guard_dt != "bool":
            raise _TranslateError("while condition is not a bool expression")
        loop_cond = z3.And(cur_cond, guard_val)
        condition = _unparse(node.test)
        _record(ctx, line=node.lineno, kind="while", condition=condition, cond=loop_cond,
                guard_node=node.test, parent_cond=cur_cond, env=env)
        body_env = dict(env)
    else:
        start, stop, step = _static_range_bounds(node.iter)
        entered = start < stop if step > 0 else start > stop
        loop_cond = z3.And(cur_cond, z3.BoolVal(entered))
        condition = _unparse(node.iter)
        _record(ctx, line=node.lineno, kind="for", condition=condition, cond=loop_cond,
                guard_node=None, parent_cond=cur_cond, env=env)
        body_env = dict(env)
        # The loop variable is bound to an UNCONSTRAINED-but-in-range symbolic
        # Int for the purpose of analyzing the body once — see the module
        # docstring's LOOPS section: this is a single representative
        # iteration, not an unrolling.
        var = z3.Int(f"__loop_{node.lineno}_{node.target.id}")
        lo, hi = (start, stop - 1) if step > 0 else (stop + 1, start)
        body_env[node.target.id] = (var, "int")
        loop_cond = z3.And(loop_cond, var >= lo, var <= hi)

    _walk_block(node.body, body_env, ctx, loop_cond)


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
    if len(ctx.branches) >= ctx.max_branches:
        ctx.truncated = True
        return
    ctx.branches.append({"line": line, "kind": kind,
                          "condition": "<could not translate this guard>",
                          "verdict": "unknown", "boundary_inputs": []})


def _walk_block(stmts: list[ast.stmt], env: dict[str, tuple[Any, str]], ctx: _Ctx, cur_cond) -> tuple[bool, Any]:
    """`(falls_through, continuation_cond)` for a straight-line list of
    statements — see the module docstring's PATH CONDITIONS section.

    A guard that fails to translate (see `_record_unknown`) does not abort
    the whole analysis: that one branch is recorded `unknown` and the block
    is conservatively treated as falling through with the condition
    UNCHANGED — this may under-report nested branches inside it, but never
    turns a real dead branch into a falsely-reachable one, the same
    "over-approximate rather than guess" rule the loop-body handling above
    follows. An assignment that fails to translate is different in kind —
    every LATER statement in this function depends on `env` staying
    accurate — so that one propagates out to a top-level refusal instead
    (see `analyze`'s own try/except around this call).
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
            return False, None
        if isinstance(stmt, ast.If):
            try:
                falls, cont = _walk_if(stmt, env, [], cur_cond, ctx, kind="if")
            except _TranslateError:
                ctx.supported = False
                _record_unknown(ctx, stmt.lineno, "if")
                falls, cont = True, cur_cond
            if not falls:
                return False, None
            cur_cond = cont
            continue
        if isinstance(stmt, ast.While):
            try:
                _walk_loop(stmt, env, ctx, cur_cond)
            except _TranslateError:
                ctx.supported = False
                _record_unknown(ctx, stmt.lineno, "while")
            continue
        if isinstance(stmt, ast.For):
            try:
                _walk_loop(stmt, env, ctx, cur_cond)
            except _TranslateError:
                ctx.supported = False
                _record_unknown(ctx, stmt.lineno, "for")
            continue
        raise _TranslateError(f"unsupported statement {type(stmt).__name__}")
    return True, cur_cond


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

    hit = _first_unsupported(tree)
    if hit is not None:
        construct, line = hit
        return _refusal(f"unsupported construct: {construct}", line=line)

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
