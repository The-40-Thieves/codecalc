"""Structured matrix operations (GH #223).

`evaluate_expression` refuses `Matrix([[1,2],[3,4]])` with `"'[' is not
permitted in an expression"`. That is `safe_expr.classify_unsafe` doing its
job: `[`/`]` are denied at the TOKEN level because they are how a subscript
escape reaches the class tree (`().__class__.__bases__[0]`), and a list
literal is collateral damage from a screen correctly aimed at something else.
`safe_expr.py`'s own docstring is explicit about the fix: "stop passing caller
strings to an evaluator, not widen this list."

So this module never builds a matrix by parsing one caller string. `rows`
arrives pre-structured — a JSON array of arrays — and each entry is handled
individually: a JSON number is used directly (it never reaches SymPy's string
parser, so there is nothing for the token screen to see), and a string entry
is screened through `classify_unsafe` BEFORE it reaches SymPy's parser,
exactly the way `logic.solve_linear` screens each side of each equation it
splits out of its own caller string. A malicious entry like `"().__class__"`
is refused per entry, the same `permission_denied` classify_unsafe already
gives `evaluate_expression`.

TWO MORE THINGS `classify_unsafe` DOES NOT COVER, both matched to
`logic.evaluate_expression`'s own bound rather than reinvented:

  * A screened, non-denylisted NAME can still be a live Python builtin.
    `input`/`breakpoint`/`quit` carry no `.`, `[`, leading underscore or
    denied keyword, so `classify_unsafe` waves them through — but a plain
    `sympify(v)` parses with sympy's DEFAULT global_dict, which is `vars(
    builtins)` copied in, so `sympify("input()")` calls the REAL `input()`,
    reading the child's stdin (fd 0, shared with a stdio MCP server), and
    `sympify("breakpoint()")` drops into pdb and hangs the child until the
    wall clock kills it. `evaluate_expression` never has this problem
    because it parses via `parse_expr(..., global_dict=safe_global_dict())`
    (logic.py), which has no Python builtins in it at all — an undefined
    name stays a harmless symbolic Function. Every string entry here parses
    the same way, via the same `safe_global_dict()`.
  * A parsed shape can still be explosive before it is ever evaluated — a
    power tower like `9**9**9**9` costs 1ms to inspect unevaluated and
    burns real CPU seconds if evaluated blind. `evaluate_expression` checks
    `reject_explosive` on the unevaluated tree before evaluating; this
    module runs the identical check per STRING ENTRY, for the same reason.
"""

from __future__ import annotations

import math

from . import errors
from .guarded import guarded_call
from .optional import require
from .safe_expr import classify_unsafe, safe_parse

#: Same DoS cap as logic.py/exact.py's expression-length guards, applied per
#: CELL rather than to one big string — there is no single string here to cap.
_MAX_ENTRY_LEN = 2000

_SCALAR_OPS = frozenset({"det", "rank", "trace"})
_MATRIX_OPS = frozenset({"inverse", "transpose"})
_EIGEN_OP = "eigenvalues"
_ALL_OPS = _SCALAR_OPS | _MATRIX_OPS | {_EIGEN_OP}

#: Operations that are only defined for a square matrix. `transpose` and
#: `rank` are the two that are not.
_SQUARE_OPS = frozenset({"det", "inverse", "trace", _EIGEN_OP})


def _sympy():
    return require("sympy")


def _validate_rows(rows) -> dict | None:
    """Structural checks that do not need sympy at all: shape before content."""
    if not isinstance(rows, list) or not rows:
        return errors.error_result(
            errors.VALIDATION, "rows must be a non-empty list of rows")
    for i, row in enumerate(rows):
        if not isinstance(row, list) or not row:
            return errors.error_result(
                errors.VALIDATION, f"row {i} must be a non-empty list of entries")
    width = len(rows[0])
    for i, row in enumerate(rows):
        if len(row) != width:
            return errors.error_result(
                errors.VALIDATION,
                f"rows must be rectangular: row 0 has {width} column(s), "
                f"row {i} has {len(row)} column(s)")
    return None


def _screen_entry(entry, row_i: int, col_i: int):
    """(value ready for sympy, error dict) for one matrix cell.

    A JSON number (int/float, not bool) is returned as-is — it is a Python
    object, not a string SymPy's parser will tokenise, so classify_unsafe has
    nothing to screen. A string is length-capped and screened exactly like
    evaluate_expression screens its own input, per THIS entry, before it is
    allowed anywhere near sympify.
    """
    if isinstance(entry, bool):
        return None, errors.error_result(
            errors.VALIDATION,
            f"entry [{row_i}][{col_i}] is a boolean, not a number or an expression string")
    if isinstance(entry, (int, float)):
        if not math.isfinite(entry):
            return None, errors.error_result(
                errors.VALIDATION, f"entry [{row_i}][{col_i}] must be finite")
        return entry, None
    if isinstance(entry, str):
        if len(entry) > _MAX_ENTRY_LEN:
            return None, errors.error_result(
                errors.RESOURCE_EXHAUSTED,
                f"entry [{row_i}][{col_i}] too long (max {_MAX_ENTRY_LEN} chars)")
        cls = classify_unsafe(entry)
        if cls:
            return None, errors.error_result(
                errors.code_for_safe_expr_category(cls[0]),
                f"entry [{row_i}][{col_i}]: {cls[1]}")
        return entry, None
    return None, errors.error_result(
        errors.VALIDATION,
        f"entry [{row_i}][{col_i}] must be a number or an expression string, "
        f"got {type(entry).__name__}")


def _parse_entry(value: str, row_i: int, col_i: int):
    """(parsed sympy value, error dict) for one already-`classify_unsafe`-clean
    string entry.

    Mirrors `logic._evaluate_expression`'s own two-step parse exactly, per
    CELL instead of per whole expression string:

      1. Parse `evaluate=False` first — cheap (1ms on a power tower) — and
         run `reject_explosive` on the unevaluated shape before anything is
         computed. A power tower like `9**9**9**9` is rejected here in
         milliseconds rather than burning CPU seconds evaluating it blind.
      2. Parse again to actually get the value, still through
         `safe_global_dict()` — never sympy's default, builtins-populated
         dict. `input`/`breakpoint`/`quit` carry none of classify_unsafe's
         denied syntax, so they reach here; safe_global_dict() has no
         Python builtins in it at all, so an undefined name like `input`
         parses to a harmless symbolic `Function('input')` rather than
         calling the real one.

    delegates to the shared `safe_expr.safe_parse`, which runs
    exactly these two steps (`classify_unsafe` is re-run here too,
    redundantly but harmlessly, since the entry is already screened by
    `_screen_entry` before this is reached). Kept as a thin wrapper, adding
    the `entry [i][j]:` cell context this function's callers already depend
    on, so the result shape is unchanged.
    """
    parsed, err = safe_parse(value)
    if err is None:
        return parsed, None
    category, message = err
    return None, errors.error_result(
        errors.code_for_safe_expr_category(category),
        f"entry [{row_i}][{col_i}]: {message}")


def _matrix(rows, op: str) -> dict:
    """The actual work. Runs inside the guard, never called directly."""
    if not isinstance(op, str) or op not in _ALL_OPS:
        return errors.error_result(
            errors.VALIDATION,
            f"unknown op {op!r}; use one of {', '.join(sorted(_ALL_OPS))}")

    bad_shape = _validate_rows(rows)
    if bad_shape:
        return bad_shape

    n_rows, n_cols = len(rows), len(rows[0])
    if op in _SQUARE_OPS and n_rows != n_cols:
        return errors.error_result(
            errors.VALIDATION,
            f"'{op}' requires a square matrix, got {n_rows}x{n_cols}")

    screened = []
    for i, row in enumerate(rows):
        conv_row = []
        for j, entry in enumerate(row):
            value, err = _screen_entry(entry, i, j)
            if err:
                return err
            conv_row.append(value)
        screened.append(conv_row)

    sp = _sympy()
    built = []
    for i, row in enumerate(screened):
        conv_row = []
        for j, value in enumerate(row):
            if isinstance(value, str):
                parsed, err = _parse_entry(value, i, j)
                if err:
                    return err
                conv_row.append(parsed)
            else:
                # Only string entries reach the parser at all — a number was
                # already screened as safe above and is handed to
                # sympy.Matrix as-is; it never touches sympify's builtins.
                conv_row.append(value)
        built.append(conv_row)
    try:
        m = sp.Matrix(built)
    except Exception as exc:
        return errors.error_result(errors.VALIDATION, f"could not build matrix: {exc}")

    shape = [n_rows, n_cols]
    try:
        if op == "det":
            return {"ok": True, "op": op, "shape": shape, "value": str(m.det())}
        if op == "rank":
            return {"ok": True, "op": op, "shape": shape, "value": str(m.rank())}
        if op == "trace":
            return {"ok": True, "op": op, "shape": shape, "value": str(m.trace())}
        if op == "transpose":
            t = m.T
            return {"ok": True, "op": op, "shape": [n_cols, n_rows],
                    "matrix": [[str(v) for v in row] for row in t.tolist()]}
        if op == "inverse":
            from sympy.matrices.exceptions import NonInvertibleMatrixError
            try:
                inv = m.inv()
            except NonInvertibleMatrixError:
                return errors.error_result(
                    errors.VALIDATION,
                    "matrix is singular (determinant is 0): it has no inverse")
            return {"ok": True, "op": op, "shape": shape,
                    "matrix": [[str(v) for v in row] for row in inv.tolist()]}
        if op == _EIGEN_OP:
            eigvals = m.eigenvals()
            return {"ok": True, "op": op, "shape": shape,
                    "eigenvalues": {str(k): int(v) for k, v in eigvals.items()}}
    except _RESOURCE_ERRORS as exc:
        return {"ok": False,
                "error": f"'{op}' exceeded a resource limit while evaluating: {exc}"}
    except Exception as exc:
        return errors.error_result(errors.VALIDATION, f"'{op}' failed: {exc}")

    # Unreachable: op was checked against _ALL_OPS above.
    return errors.error_result(errors.INTERNAL, f"unhandled op {op!r}")


#: Same category of failure evaluate_expression bounds (logic.py): a deeply
#: nested or oversized symbolic result can exhaust the interpreter stack or
#: fail to render.
_RESOURCE_ERRORS = (ValueError, RecursionError, OverflowError, MemoryError)

_WARMED = False


def _warm() -> None:
    """Warm the parent for the sympy Matrix paths this module takes, once.

    Same reasoning as logic.py's _warm_sympy / exact.py's _warm_exact: a
    forked child inherits an already-imported, already-warmed sympy through
    copy-on-write and pays for none of the import; run cold, it re-imports the
    matrix submodules on every call and dies with the child.
    """
    global _WARMED
    if _WARMED:
        return
    _WARMED = True
    try:
        _matrix([[1, 2], [3, 4]], "det")
        _matrix([[1, 2], [3, 4]], "inverse")
        _matrix([[2, 0], [0, 2]], "eigenvalues")
        _matrix([["x+1", "2"], ["3", "4"]], "det")  # warms the parse_expr path too
    except Exception:
        pass


def matrix(rows, op: str) -> dict:
    """Structured matrix operations: det, inverse, eigenvalues, transpose, rank, trace.

    Guarded (#84, same bound as every other sympy-backed tool): the body runs
    where the parent can kill it, because classify_unsafe is a denylist and a
    denylist cannot bound what it did not anticipate.
    """
    _warm()
    return guarded_call(_matrix, rows, op)
