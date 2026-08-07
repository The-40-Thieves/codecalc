"""Logic layer: symbolic math (SymPy), truth tables, SMT solving (Z3).

sympy and z3 are imported lazily inside the functions that need them — they
are the two heaviest imports in the project (~600ms together), so lazy loading
keeps server startup and pure-execution calls fast on older machines.
"""

from __future__ import annotations

import itertools
import re

_MATH_TRANSFORMS = None
_BOOL_TRANSFORMS = None


def _sympy():
    """Lazy sympy import; returns the module."""
    import sympy as sp
    return sp


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
    """Evaluate/simplify a math or boolean expression symbolically."""
    if len(expression) > _MAX_EXPR_LEN:
        return {"ok": False, "error": f"expression too long (max {_MAX_EXPR_LEN} chars)"}
    sp = _sympy()
    from sympy.parsing.sympy_parser import parse_expr

    try:
        expr = parse_expr(expression, transformations=_math_transforms(), local_dict=variables)
    except Exception as exc:  # sympy raises many specific types
        return {"ok": False, "error": f"parse error: {exc}"}

    result = {
        "ok": True,
        "expression": str(expr),
        "simplified": str(sp.simplify(expr)),
        "type": str(type(expr).__name__),
    }
    if expr.is_number:
        result["value"] = str(expr.evalf(12))
    try:
        result["expanded"] = str(sp.expand(expr))
    except Exception:
        pass
    return result


def _to_bool_syntax(expression: str) -> str:
    """Translate English boolean words into pure-Python boolean operators.

    Python's `and`/`or`/`not`/`^` (xor on bools)/`<=` (implies)/`==` (iff)
    are exactly boolean algebra when operands are bools, so a restricted
    per-row eval gives the truth table without sympy's parser quirks.
    """
    s = expression
    s = s.replace(" iff ", " == ").replace(" implies ", " <= ")
    s = re.sub(r"\bxor\b", "^", s)
    s = re.sub(r"\bnot\s+", "not ", s)
    s = re.sub(r"\band\b", "and", s)
    s = re.sub(r"\bor\b", "or", s)
    return s


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

    def __init__(self, text: str):
        self.tokens = _tokenize(text)
        self.pos = 0

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
        node = self._parse_xor()
        while self._peek() == "implies":
            self.pos += 1
            node = ("implies", node, self._parse_xor())
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
            node = self._parse_iff()
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


def z3_check(smt2: str, timeout_ms: int = 5000) -> dict:
    """Check satisfiability of an SMT-LIB2 script; return sat/unsat/unknown + model."""
    try:
        import z3
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
        }
    except Exception as exc:
        return {"ok": False, "error": f"z3 error: {exc}"}


def solve_linear(system: str, variables: str | list[str]) -> dict:
    """Solve a ';'-separated system of equations ('x + y = 10; x - y = 2')."""
    try:
        sp = _sympy()
        if isinstance(variables, str):
            variables = [v.strip() for v in variables.split(",") if v.strip()]
        syms = sp.symbols(",".join(variables))
        eqs = []
        for raw in system.split(";"):
            raw = raw.strip()
            if not raw:
                continue
            if "=" in raw:
                lhs, rhs = raw.split("=", 1)
                eqs.append(sp.Eq(sp.sympify(lhs), sp.sympify(rhs)))
            else:
                eqs.append(sp.sympify(raw, evaluate=False))
        sol = sp.solve(eqs, list(syms), dict=True)
        return {"ok": True, "solutions": [str(s) for s in sol], "count": len(sol)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
