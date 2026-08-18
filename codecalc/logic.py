"""Logic layer: symbolic math (SymPy), truth tables, SMT solving (Z3).

sympy and z3 are imported lazily inside the functions that need them — they
are the two heaviest imports in the project (~600ms together), so lazy loading
keeps server startup and pure-execution calls fast on older machines.
"""

from __future__ import annotations

import itertools
import re

from .guarded import guarded_call
from .optional import require
from .safe_expr import reject_explosive, reject_unsafe, safe_global_dict

_MATH_TRANSFORMS = None
_BOOL_TRANSFORMS = None


def _sympy():
    """Lazy sympy import; returns the module."""
    return require("sympy")


def _math_transforms():
    global _MATH_TRANSFORMS
    if _MATH_TRANSFORMS is None:
        from sympy.parsing.sympy_parser import (
            convert_xor,
            implicit_multiplication_application,
            standard_transformations,
        )
        _MATH_TRANSFORMS = standard_transformations + (
            implicit_multiplication_application,
            convert_xor,
        )
    return _MATH_TRANSFORMS


def evaluate_expression(expression: str, **variables) -> dict:
    """Evaluate/simplify a math or boolean expression symbolically.

    The work runs in a process that can be KILLED (see `guarded`), because the
    screens below are denylists and a denylist cannot bound what it did not
    anticipate. Everything the screens do still happens — they are cheap, they
    give better error messages, and they stop hostile input before it reaches
    an evaluator at all — but the bound no longer depends on them being
    complete.
    """
    _warm_sympy()
    return guarded_call(_evaluate_expression, expression, **variables)


_WARMED = False


def _warm_sympy() -> None:
    """Touch the lazy paths ONCE, in the parent, before anything is forked.

    Measured, and the reason this exists: guarding made `2+2` take 291ms
    against 0.68ms unguarded, while a HARDER expression cost only 19ms more.
    Backwards, and the explanation is that SymPy imports submodules lazily and
    caches them. In a forked child that warming happens, is used once, and dies
    with the child — so every call re-imported, and the first call paid the
    most because it warmed the most.

    Doing it here means children inherit a warm parent through copy-on-write
    and pay for none of it. It runs the same path a real call takes rather than
    importing a list of module names, because what needs warming is whatever
    simplify/expand actually reach, which is not something to guess at.
    """
    global _WARMED
    if _WARMED:
        return
    _WARMED = True
    try:
        # A REPRESENTATIVE SET, not one expression. Warming with "1+1" alone
        # left `sin(x)**2 + cos(x)**2` at 196ms of overhead, because trig
        # simplification pulls in modules arithmetic never touches and each
        # child was importing them again. These cover the paths the tool is
        # actually asked for: integer arithmetic, polynomial expansion, trig
        # identities and radicals.
        for _seed in ("1+1", "x**2 + 2*x + 1", "sin(x)**2 + cos(x)**2", "sqrt(8)"):
            _evaluate_expression(_seed)
    except Exception:
        # Warming is an optimisation. A failure here must not take out the call
        # the user actually made — that one runs guarded a moment later and
        # will report its own error properly.
        pass


def _evaluate_expression(expression: str, **variables) -> dict:
    """The actual evaluation. Runs inside the guard, never called directly."""
    if len(expression) > _MAX_EXPR_LEN:
        return {"ok": False, "error": f"expression too long (max {_MAX_EXPR_LEN} chars)"}
    # parse_expr EVALUATES what it parses, and its default global_dict is
    # populated from vars(builtins) on purpose — 967 names on sympy 1.14.0,
    # __import__ among them. The length cap above bounds work, not reach:
    # `__import__('os').system('id')` is 31 characters. See safe_expr.
    bad = reject_unsafe(expression)
    if bad:
        return {"ok": False, "error": bad}
    sp = _sympy()
    from sympy.parsing.sympy_parser import parse_expr

    # Look at the shape BEFORE doing the arithmetic. `evaluate=False` builds
    # the tree without evaluating operators, so a power tower costs 1ms to
    # inspect and nothing to reject. It does not stop FUNCTION evaluation —
    # that hazard is handled at the token level in reject_unsafe, above.
    try:
        shape = parse_expr(expression, transformations=_math_transforms(),
                           local_dict=variables, global_dict=safe_global_dict(),
                           evaluate=False)
    except Exception as exc:
        return {"ok": False, "error": f"parse error: {exc}"}
    explosive = reject_explosive(shape)
    if explosive:
        return {"ok": False, "error": explosive}

    try:
        expr = parse_expr(expression, transformations=_math_transforms(),
                          local_dict=variables, global_dict=safe_global_dict())
    except Exception as exc:  # sympy raises many specific types
        return {"ok": False, "error": f"parse error: {exc}"}

    # NOT EVERY SymPy CALL RETURNS A SymPy OBJECT. `nextprime(100)` returns a
    # plain int, `primefactors(60)` and `divisors(12)` return lists, and
    # `factorint(60)` returns a dict. The code below assumed `.is_number` and
    # `sp.simplify`, so every one of those ordinary, entirely legitimate
    # queries crashed the tool with an uncaught AttributeError:
    #
    #     nextprime(100)   -> 'int' object has no attribute 'is_number'
    #     primefactors(60) -> 'list' object has no attribute 'replace'
    #
    # They are answered here rather than merely not-crashing: the value IS the
    # result, there is just nothing to simplify.
    if not isinstance(expr, sp.Basic):
        return {
            "ok": True,
            "expression": str(expr),
            "simplified": str(expr),
            "type": type(expr).__name__,
            "value": str(expr),
        }

    # Building the result is itself unbounded work, and it used to sit outside
    # any try: `factorial(99999)` parsed fine and then raised
    # `ValueError: Exceeds the limit (4300 digits) for integer string
    # conversion` out of str(expr), from inside SymPy's printer. The tool
    # CRASHED rather than returning the {"ok": False, "error": ...} shape every
    # other path uses, so a caller saw a transport-level failure instead of a
    # result. The bounds above make that far less reachable; this makes it
    # survivable when it is reached anyway.
    try:
        result = {
            "ok": True,
            "expression": str(expr),
            "simplified": str(sp.simplify(expr)),
            "type": str(type(expr).__name__),
        }
        if expr.is_number:
            result["value"] = str(expr.evalf(12))
    except _RESOURCE_ERRORS as exc:
        return {"ok": False,
                "error": f"expression exceeded a resource limit while evaluating: {exc}"}
    try:
        result["expanded"] = str(sp.expand(expr))
    except Exception:
        pass
    return result


#: Raised when an expression is well-formed and simply too big. Distinguished
#: from a parse error because the caller's fix is different: this one means
#: "ask for less", not "fix your syntax".
#:
#: RecursionError is in here because a deeply nested expression exhausts the
#: interpreter stack inside SymPy's own recursion, and OverflowError because
#: float conversion of a large exact value raises rather than returning inf.
_RESOURCE_ERRORS = (ValueError, RecursionError, OverflowError, MemoryError)


_MAX_VARS = 16          # 2^16 = 65536 rows ceiling for truth tables
_MAX_EXPR_LEN = 2000    # cap on expression size (sympy/parser DoS guard)


def _tokenize(text: str) -> list[str]:
    """Tokenize a boolean expression: identifiers, parens, and keyword ops."""
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(text[i:j].lower())
            i = j
        elif c == "(" or c == ")":
            tokens.append(c)
            i += 1
        else:
            raise ValueError(f"unexpected character {c!r} at position {i}")
    return tokens


class _BoolParser:
    """Recursive-descent parser for boolean expressions. NO eval — the input
    is parsed into an AST and evaluated by walking the tree. The old
    implementation used eval() with a restricted globals dict, which was
    escapable via `().__class__.__base__.__subclasses__()` (host RCE)."""

    _OPS: frozenset = frozenset({"and", "or", "not", "xor", "implies", "iff"})
    #: Parenthesis-nesting cap. Each "(" recurses through six mutually
    #: recursive _parse_* methods before reaching _parse_primary again, so
    #: `"(" * 999 + "a" + ")" * 999` (1999 chars — under _MAX_EXPR_LEN, so
    #: the length guard never fires) drove ~6000 stack frames and raised a
    #: bare RecursionError out of truth_table. This is comfortably below
    #: CPython's default recursion limit even with the six-frame multiplier,
    #: so the tool's own bound is what fires, not the interpreter's.
    _MAX_NESTING = 100

    def __init__(self, text: str):
        self.tokens = _tokenize(text)
        self.pos = 0
        self._depth = 0

    def parse(self) -> tuple:
        if not self.tokens:
            raise ValueError("empty expression")
        node = self._parse_iff()
        if self.pos != len(self.tokens):
            raise ValueError(f"unexpected token {self.tokens[self.pos]!r}")
        return node

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _parse_iff(self):
        node = self._parse_implies()
        while self._peek() == "iff":
            self.pos += 1
            node = ("iff", node, self._parse_implies())
        return node

    def _parse_implies(self):
        # Right-associative: `a implies b implies a` means `a -> (b -> a)`,
        # the conventional reading. A `while` loop here (as `_parse_iff`
        # above genuinely wants, since `iff` IS associative) folds LEFT
        # instead: `(a -> b) -> a`, which is Peirce's formula — a different,
        # non-tautologous statement for the example above. Recursing on the
        # right operand instead of looping builds the tree right-associated.
        node = self._parse_xor()
        if self._peek() == "implies":
            self.pos += 1
            node = ("implies", node, self._parse_implies())
        return node

    def _parse_xor(self):
        node = self._parse_or()
        while self._peek() == "xor":
            self.pos += 1
            node = ("xor", node, self._parse_or())
        return node

    def _parse_or(self):
        node = self._parse_and()
        while self._peek() == "or":
            self.pos += 1
            node = ("or", node, self._parse_and())
        return node

    def _parse_and(self):
        node = self._parse_not()
        while self._peek() == "and":
            self.pos += 1
            node = ("and", node, self._parse_not())
        return node

    def _parse_not(self):
        if self._peek() == "not":
            self.pos += 1
            return ("not", self._parse_not())
        return self._parse_primary()

    def _parse_primary(self):
        tok = self._peek()
        if tok == "(":
            self.pos += 1
            self._depth += 1
            if self._depth > self._MAX_NESTING:
                raise ValueError(f"expression nesting too deep (max {self._MAX_NESTING})")
            node = self._parse_iff()
            self._depth -= 1
            if self._peek() != ")":
                raise ValueError("missing closing parenthesis")
            self.pos += 1
            return node
        if tok == "true":
            self.pos += 1
            return ("const", True)
        if tok == "false":
            self.pos += 1
            return ("const", False)
        if tok is not None and tok not in self._OPS:
            self.pos += 1
            return ("var", tok)
        raise ValueError(f"unexpected token {tok!r}")


def _eval_bool(node: tuple, env: dict[str, bool]) -> bool:
    op = node[0]
    if op == "const":
        return node[1]
    if op == "var":
        if node[1] not in env:
            raise ValueError(f"unknown variable {node[1]!r}")
        return env[node[1]]
    if op == "not":
        return not _eval_bool(node[1], env)
    if op == "and":
        return _eval_bool(node[1], env) and _eval_bool(node[2], env)
    if op == "or":
        return _eval_bool(node[1], env) or _eval_bool(node[2], env)
    if op == "xor":
        return _eval_bool(node[1], env) != _eval_bool(node[2], env)
    if op == "implies":
        return (not _eval_bool(node[1], env)) or _eval_bool(node[2], env)
    if op == "iff":
        return _eval_bool(node[1], env) == _eval_bool(node[2], env)
    raise ValueError(f"unknown operator {op!r}")


def _collect_vars(node: tuple) -> set[str]:
    if node[0] == "var":
        return {node[1]}
    if node[0] == "const":
        return set()
    if node[0] == "not":
        return _collect_vars(node[1])
    return _collect_vars(node[1]) | _collect_vars(node[2])


def truth_table(expression: str) -> dict:
    """Build the truth table for a boolean expression (and/or/not/xor/implies/iff).

    Parsed with a recursive-descent parser — user input is NEVER eval'd, so no
    code execution is possible from the expression (CVE fix: the old eval-based
    implementation allowed host RCE via __subclasses__()).
    """
    if len(expression) > _MAX_EXPR_LEN:
        return {"ok": False, "error": f"expression too long (max {_MAX_EXPR_LEN} chars)"}
    try:
        tree = _BoolParser(expression).parse()
    except ValueError as exc:
        return {"ok": False, "error": f"parse error: {exc}"}
    except RecursionError as exc:
        # The nesting cap on `(` above should catch this first, but it is a
        # cap on ONE recursive path (parenthesis depth); this is the
        # backstop for any other input shape that recurses too deep, same
        # spirit as the length cap two lines up.
        return {"ok": False, "error": f"expression too deeply nested to parse: {exc}"}

    vars_ = sorted(_collect_vars(tree))
    if len(vars_) > _MAX_VARS:
        return {
            "ok": False,
            "error": f"too many variables ({len(vars_)} > {_MAX_VARS}); table would have 2^{len(vars_)} rows",
        }

    if not vars_:
        val = _eval_bool(tree, {})
        return {
            "ok": True, "expression": expression, "variables": [],
            "row_count": 1, "rows": [{"result": val}],
            "satisfiable": val, "tautology": val,
        }

    rows = []
    for combo in itertools.product([False, True], repeat=len(vars_)):
        env = dict(zip(vars_, combo))
        rows.append(env | {"result": _eval_bool(tree, env)})

    return {
        "ok": True,
        "expression": expression,
        "variables": vars_,
        "row_count": len(rows),
        "rows": rows,
        "satisfiable": any(r["result"] for r in rows),
        "tautology": all(r["result"] for r in rows),
    }


#: Comments and whitespace, which carry no assertions.
_SMT_NOISE = re.compile(r";[^\n]*|\s+")


def z3_check(smt2: str, timeout_ms: int = 5000) -> dict:
    """Check satisfiability of an SMT-LIB2 script; return sat/unsat/unknown + model.

    An EMPTY script is not a satisfiable one. z3 answers `sat` for a script with
    no constraints, which is vacuously true and a terrible thing to hand back:
    empty input, whitespace, a comment, and a script missing `(check-sat)` all
    returned `{"ok": true, "result": "sat"}`. A caller whose generator produced
    nothing — or who forgot the check — was told YES. `truth_table` has always
    rejected an empty expression; this now matches it.
    """
    if not _SMT_NOISE.sub("", smt2 or ""):
        return {"ok": False, "error": "empty SMT-LIB2 script: nothing to check. "
                                      "An empty script is trivially satisfiable, "
                                      "which is not an answer to any question."}
    if "check-sat" not in smt2:
        return {"ok": False, "error": "script has no (check-sat); z3 would report "
                                      "the state of an unchecked solver, not a verdict"}
    try:
        z3 = require("z3")
        solver = z3.Solver()
        solver.set("timeout", timeout_ms)  # in-process hang guard
        solver.from_string(smt2)
        verdict = solver.check()
        model = None
        if verdict == z3.sat:
            m = solver.model()
            model = {str(d): str(m[d]) for d in m}
        elif verdict == z3.unsat:
            core = None
            try:
                core = [str(c) for c in solver.unsat_core()]
            except Exception:
                pass
            model = {"unsat_core": core}
        return {
            "ok": True,
            "result": str(verdict),
            "model": model,
            "assertions": len(solver.assertions()),
            # Evidence for codecalc/grades.py (THE-785): WHICH engine decided
            # this and within WHAT bound. Recorded here because z3_check is
            # the only place that knows either — grades.py only reads them.
            "engine": f"z3 {z3.get_version_string()}",
            "timeout_ms": timeout_ms,
        }
    except Exception as exc:
        return {"ok": False, "error": f"z3 error: {exc}"}


def _solve_linear(system: str, variables: str | list[str]) -> dict:
    """Solve a ';'-separated system of equations ('x + y = 10; x - y = 2')."""
    try:
        # The DoS length cap, same as _evaluate_expression and truth_table above
        # (THE-844). solve_linear reached sp.sympify below with only reject_unsafe
        # (a safety screen, not a length gate), so a 120k-char system blew the
        # parser's recursion limit and rode the "stack overflow" message hint to
        # resource_exhausted — the fragile, interpreter-wording-dependent path the
        # cap exists to eliminate. Cap the whole system string before any parse.
        if len(system) > _MAX_EXPR_LEN:
            return {"ok": False, "error": f"expression too long (max {_MAX_EXPR_LEN} chars)"}
        sp = _sympy()
        if isinstance(variables, str):
            variables = [v.strip() for v in variables.split(",") if v.strip()]
        syms = sp.symbols(",".join(variables))
        eqs = []
        for raw in system.split(";"):
            raw = raw.strip()
            if not raw:
                continue
            # Screened per PIECE: this splits on ';' and '=' before sympify,
            # and the screen refuses both, so the whole system string would
            # never pass. What reaches the parser is what must be checked.
            if "=" in raw:
                lhs, rhs = raw.split("=", 1)
                for _part in (lhs, rhs):
                    _bad = reject_unsafe(_part)
                    if _bad:
                        return {"ok": False, "error": _bad}
                eqs.append(sp.Eq(sp.sympify(lhs), sp.sympify(rhs)))
            else:
                _bad = reject_unsafe(raw)
                if _bad:
                    return {"ok": False, "error": _bad}
                eqs.append(sp.sympify(raw, evaluate=False))
        sol = sp.solve(eqs, list(syms), dict=True)
        # A variable the caller ASKED for and the system does not constrain is
        # dropped by sympy without comment: solve("x+y=10; x-y=2", "x, y, z")
        # returned {x: 6, y: 4}, count 1, and never mentioned z. Naming the
        # unsolved ones turns a silently partial answer into a stated one.
        requested = [str(s) for s in (syms if isinstance(syms, (list, tuple)) else [syms])]
        solved = {k for s in sol for k in map(str, s)}
        unsolved = [v for v in requested if v not in solved]
        out = {"ok": True, "solutions": [str(s) for s in sol], "count": len(sol)}
        if unsolved and sol:
            out["unsolved"] = unsolved
            out["note"] = (f"the system does not determine {', '.join(unsolved)}; "
                           f"the solution covers the remaining variable(s) only")
        return out
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def solve_linear(system: str, variables: str | list[str]) -> dict:
    """Solve a system of linear equations for the named variables.

    Guarded (#84): the body is unchanged, it just runs somewhere the parent can
    kill it. Warmed through the same set evaluate_expression uses, because both
    reach SymPy's core solving and simplification paths.
    """
    _warm_sympy()
    return guarded_call(_solve_linear, system, variables)
