"""Screen every caller string before it reaches SymPy.

SymPy evaluates the expressions it parses. `parse_expr` does not leak builtins
by accident — it populates its default `global_dict` from `vars(builtins)` on
purpose, copying the builtin functions in, `__import__` among them. On sympy
1.14.0 that dictionary holds 967 names. `sympify` on a string routes through
the same parser.

So `simplify_expression("__import__('os').system('id')")` ran `id`. The
SympifyError raised afterwards is SymPy failing to convert the RETURN VALUE;
the call already happened. That trailing exception is why this reads as a
rejected input when it is not.

This is AUDIT.md CRITICAL-01 through a different door. That one was
`truth_table` calling `eval` with a restricted globals dict, and the lesson
written down then was that restricting names does not contain an evaluator,
because the escape uses the object graph rather than the namespace. The same
applies here: narrowing `global_dict` does not make an evaluator safe, it just
moves the search. Hence a screen on the INPUT, in the same spirit as the
recursive-descent `_BoolParser` that replaced that eval.

WHY TOKENS AND NOT `ast.parse`:
`_math_transforms()` enables `implicit_multiplication_application`, so SymPy
legitimately accepts `2x`, `sin x` and `x y` — none of which are valid Python.
Screening with `ast.parse` would reject expressions the tool is documented to
support. Tokenising checks the raw string against a closed grammar without
requiring it to be a Python expression.

WHAT IS REFUSED, and why each one:
  attribute access (`.`)   the class-tree walk and every `os.system` shape
  leading underscore       `__import__`, `__class__`, `__subclasses__`
  string literals          `__import__('os')` needs one; maths does not
  subscripts (`[`, `]`)    `.__subclasses__()[N]`
  statement syntax         `=`, `;`, `:`, `@`, walrus — an expression tool
                           has no use for them, and `lambda`/comprehensions
                           reintroduce arbitrary evaluation
Numbers keep their dots: `1.5` is a single NUMBER token, so refusing the `.`
OPERATOR does not refuse decimals. That is asserted in the tests rather than
assumed.

This is a screen, not a sandbox. It is deliberately a denylist of syntax
rather than an allowlist of names, because the names SymPy accepts are open
(every symbol a caller invents) while the syntax an attacker needs is small
and closed. If a bypass is found the answer is to stop passing caller strings
to an evaluator, not to widen this list.

UPSTREAM AGREES, AND HAS TRIED THIS:
That last sentence was written from first principles here. SymPy's own
maintainers reached it about their own attempt at the same thing. PR #12524
added a `safe=` flag to sympify() built on an AST whitelist plus a name
blacklist — structurally this module — and it was NEVER MERGED, abandoned
since 2020:

    "I am sure that someone malicious would be able to circumvent what we
     have here so I would still describe this as unsafe rather than
     'mostly safe'."                                     -- oscarbenjamin

    "security theater that leads users into a false sense of security,
     because it can still be bypassed"     -- asmeurer, the PR's own author

sympy/sympy#10805 ("sympify shouldn't use eval") is still open, and the fix
advocated there is a complete direct evaluator rather than any screening. So
this file should be read as buying time against the obvious attacks, not as a
boundary. Do not let its passing be mistaken for the input being safe.

Also verified against the pinned version rather than assumed: sympy 1.14.0's
sympify takes (a, locals, convert_xor, strict, rational, evaluate). There is
no `safe=` to pass — code written against that PR raises TypeError.
"""

from __future__ import annotations

import io
import tokenize

#: Keywords that would reintroduce evaluation or control flow. `and`, `or`,
#: `not`, `True` and `False` are deliberately ABSENT: evaluate_expression is
#: documented to take boolean expressions and they are the whole point of it.
_DENIED_KEYWORDS = frozenset({
    "lambda", "import", "from", "for", "while", "if", "else", "elif",
    "class", "def", "return", "yield", "await", "async", "global",
    "nonlocal", "with", "as", "assert", "del", "raise", "try", "except",
    "finally", "pass", "break", "continue", "is", "in",
})

#: STRING covers every string literal on every supported Python. FSTRING_START
#: is a 3.12 addition (PEP 701 split f-strings into their own token types), and
#: naming it unconditionally made this module fail to IMPORT on 3.11 with
#: AttributeError — which took codecalc down entirely there, since exact.py
#: imports this. The project declares requires-python >=3.11 and CI runs a
#: py3.11 matrix leg; both said so immediately. Resolved once, at import, so
#: the hot path stays a set lookup.
_STRING_TOKENS = frozenset(
    t for t in (getattr(tokenize, name, None) for name in ("STRING", "FSTRING_START"))
    if t is not None
)

#: Operators with no place in a mathematical expression. `.` is the important
#: one; the rest close off subscripting and statement syntax.
_DENIED_OPS = frozenset({
    ".", "[", "]", "{", "}", ":", ";", "=", ":=", "@", "->", "...",
})


#: Functions that turn a small integer argument into an enormous one. These
#: are the parse-time hazard, and they are a SEPARATE problem from the RCE
#: screen above: none of them reach outside SymPy, they just do an unbounded
#: amount of work.
#:
#: Why the check is here, before SymPy is handed the string at all: measured,
#: `parse_expr(..., evaluate=False)` does NOT stop them. `evaluate=False`
#: suppresses OPERATOR evaluation — `9**9**9**9` stays an unevaluated Pow tree
#: — but a function applied to a literal still runs during parsing:
#:
#:     parse_expr("factorial(99999)", evaluate=False)  -> Integer, 0.117s
#:     parse_expr("factorial(10**6)", evaluate=False)  -> factorial, 0.001s
#:
#: The second stays symbolic only because `10**6` is itself an unevaluated Pow
#: by then, so there is no literal for factorial to consume. That difference is
#: why inspecting the parsed tree is not enough on its own — for a literal
#: argument the work has already happened by the time a tree exists.
_HEAVY_FUNCTIONS = frozenset({
    "factorial", "factorial2", "subfactorial", "binomial", "fibonacci",
    "lucas", "tribonacci", "catalan", "bernoulli", "euler", "harmonic",
    "primorial", "prime", "primepi", "gamma", "loggamma", "rf", "ff",
    "npartitions", "totient", "divisor_sigma",
})

#: This started at 50_000, chosen from measured PARSE time:
#:
#:     factorial(50_000)             0.038s
#:     factorial(100_000)            0.122s
#:     binomial(200_000, 100_000)    2.581s
#:
#: The test asserting the cap admits its own limit then failed, which was the
#: assertion doing its job: `factorial(50_000)` parses in 38ms and produces an
#: integer of 213_237 digits, and Python refuses to render an integer over
#: 4300 digits at all. So it was admitted by this cap and then rejected by the
#: printer — a limit that let through what the next stage could not return.
#:
#: The binding constraint is therefore not how long the work takes, it is
#: whether the ANSWER can be handed back. Computed rather than guessed:
#:
#:     factorial(1_000)  ->  2_568 digits
#:     factorial(1_463)  ->  3_998 digits   <- largest under MAX_NUMERIC_DIGITS
#:     factorial(1_500)  ->  4_115 digits
#:
#: Conservative for the cheaper members of the set (`prime`, `totient` return
#: something small for a large argument), and deliberately so: one cap that is
#: right for the worst grower beats twenty per-function caps that drift.
MAX_HEAVY_ARG = 1_463


def _heavy_call_violation(tokens: list) -> str | None:
    """A heavy function applied to an oversized integer LITERAL, if any.

    Only literals are checked, because only literals are what the parser can
    consume before anything else bounds them. A computed argument
    (`factorial(10**6)`) survives this and is caught later by the tree rules,
    which is the right division: this function exists to stop work that would
    otherwise happen DURING parsing.
    """
    for i, tok in enumerate(tokens):
        if tok.type != tokenize.NAME or tok.string not in _HEAVY_FUNCTIONS:
            continue
        if i + 1 >= len(tokens) or tokens[i + 1].string != "(":
            continue  # a bare mention like `gamma` as a symbol, not a call
        depth = 0
        for nxt in tokens[i + 1:]:
            if nxt.type == tokenize.OP and nxt.string == "(":
                depth += 1
            elif nxt.type == tokenize.OP and nxt.string == ")":
                depth -= 1
                if depth == 0:
                    break
            elif nxt.type == tokenize.NUMBER:
                try:
                    # base 0, not base 10. `int("0xffffff")` raises ValueError,
                    # and the except below skipped it — so factorial(0xffffff)
                    # sailed past a cap that stops factorial(16777215), the
                    # same number written differently. Octal and binary
                    # literals had the identical hole.
                    value = int(nxt.string, 0)
                except ValueError:
                    continue  # a float or complex literal — not this hazard
                if value > MAX_HEAVY_ARG:
                    # Names the ARGUMENT, not `func(value)`. The latter reads
                    # as the whole call, which is wrong the moment the function
                    # takes two: `binomial(200000, 100000)` was reporting
                    # itself as "binomial(200000)".
                    return (f"argument {value} to {tok.string}() exceeds the limit "
                            f"of {MAX_HEAVY_ARG}: computing it would take an "
                            "unbounded amount of time and memory")
    return None


#: Categories `classify_unsafe` hands back, for a CALLER to map to its own
#: error taxonomy (THE-881). Deliberately neutral strings rather than
#: `errors.PERMISSION_DENIED` etc.: this module has no business knowing the
#: result-contract's taxonomy, only which of three DIFFERENT KINDS of "no" an
#: expression got. Conflating them under one code was the bug — `factorial(
#: 100000)` (CATEGORY_CEILING) and `__import__(...)` (CATEGORY_SECURITY) are
#: both refusals but not the same refusal: one is "this will not succeed on
#: retry because it is a jail", the other is "raise the ceiling and retry".
CATEGORY_VALIDATION = "validation"
CATEGORY_SECURITY = "security"
CATEGORY_CEILING = "ceiling"


def classify_unsafe(expression: str) -> tuple[str, str] | None:
    """(category, reason) this string must not reach SymPy, or None if it may.

    The single source of truth for what `reject_unsafe` also returns — see
    that function, kept as a thin wrapper so its existing `str | None`
    contract (and the exact message text callers/tests already match on)
    does not change.
    """
    if not isinstance(expression, str):
        return (CATEGORY_VALIDATION,
                f"expression must be a string, got {type(expression).__name__}")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(expression).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError,
            UnicodeEncodeError, UnicodeDecodeError) as exc:
        # Unparsable at the token level is not automatically hostile — SymPy
        # accepts things Python does not — so say what happened and let the
        # caller's own parse error be the verdict, rather than claiming this
        # was an attack.
        #
        # Both Unicode errors are the same shape, found the same way — CPython's
        # C tokenizer round-trips the source through UTF-8 internally and raises
        # rather than returning a token error, escaping a function every caller
        # treats as returning `None` or `(category, message)`, never raising:
        #   - UnicodeEncodeError — a lone UTF-16 surrogate (e.g. "\ud800"), found
        #     by scripts/fuzz.py (THE-899);
        #   - UnicodeDecodeError — a bare replacement char / truncated multibyte
        #     shape (e.g. "�\r�"), found by the ClusterFuzzLite
        #     coverage-guided harness (THE-899 follow-up) that the seeded mutator
        #     never reached — the exact case that infra exists to catch.
        # Both are as "unparsable, not hostile" as the three parser exceptions
        # already caught here, and get the same validation verdict — never passed
        # on to SymPy as safe.
        return (CATEGORY_VALIDATION, f"expression could not be tokenised: {exc}")

    for tok in tokens:
        if tok.type == tokenize.NAME:
            if tok.string.startswith("_"):
                return (CATEGORY_SECURITY,
                        (f"identifier {tok.string!r} is not permitted: leading "
                         "underscores reach Python internals"))
            if tok.string in _DENIED_KEYWORDS:
                return (CATEGORY_SECURITY,
                        f"keyword {tok.string!r} is not permitted in an expression")
        elif tok.type in _STRING_TOKENS:
            return (CATEGORY_SECURITY, "string literals are not permitted in an expression")
        elif tok.type == tokenize.OP and tok.string in _DENIED_OPS:
            if tok.string == ".":
                return (CATEGORY_SECURITY, "attribute access is not permitted in an expression")
            if tok.string in ("[", "]"):
                # Named explicitly (THE-887, GH #223) rather than left to the
                # generic message below: `[`/`]` are denied to block a
                # subscript escape (`().__class__.__bases__[0]`), and a matrix
                # literal like `Matrix([[1,2],[3,4]])` is the collateral from
                # that correctly-aimed screen. The underlying refusal is
                # unchanged — this only tells the caller where to go instead
                # of leaving them staring at a bare "'[' is not permitted".
                return (CATEGORY_SECURITY,
                        ("matrices can't be written in evaluate_expression "
                         "(the [ ] used for a subscript escape are refused here "
                         "for the same reason a list literal is); use the "
                         "matrix tool instead (det/inverse/eigenvalues/"
                         "transpose/rank/trace)"))
            return (CATEGORY_SECURITY, f"{tok.string!r} is not permitted in an expression")
    # Last, because reach beats cost: an expression that is both hostile and
    # expensive should be reported as hostile.
    violation = _heavy_call_violation(tokens)
    if violation:
        return (CATEGORY_CEILING, violation)
    return None


def reject_unsafe(expression: str) -> str | None:
    """Reason this string must not reach SymPy, or None if it may.

    Returns a message rather than raising so callers can fold it into the
    structured `{"ok": False, "error": ...}` shape every tool already uses.
    A caller that also needs to know WHICH kind of refusal this is (a jail, a
    ceiling, or a plain validation mistake — they are not interchangeable,
    see THE-881) should call `classify_unsafe` instead; this wrapper exists
    so the message-only contract callers and tests already depend on does
    not change.
    """
    result = classify_unsafe(expression)
    return result[1] if result else None


def safe_global_dict() -> dict:
    """A `global_dict` for parse_expr with the builtins left out.

    Defence in depth only. `reject_unsafe` above is what actually holds the
    line — this narrows what an unforeseen bypass would land in. It is not
    load-bearing and must not be treated as if it were: SymPy's default
    populates from `vars(builtins)`, and the CRITICAL-01 finding is precisely
    that a narrowed namespace does not contain an evaluator.
    """
    from sympy import __dict__ as sympy_ns

    g = {k: v for k, v in sympy_ns.items() if not k.startswith("_")}
    # An explicit empty __builtins__ stops Python re-populating it during eval.
    g["__builtins__"] = {}
    return g


#: Ceiling on a SYMBOLIC power's exponent, i.e. one whose base contains a free
#: symbol. Measured cost of evaluating `(x+1)**n`:
#:
#:     n = 100      ~0.2s
#:     n = 1_000     6.2s
#:     n = 20_000    killed at 25s / 3GB
#:
#: The growth is superlinear in n and the work is a polynomial expansion, so a
#: bound here is the difference between a slow answer and a server that stops
#: answering. 200 keeps it comfortably sub-second.
MAX_SYMBOLIC_EXPONENT = 200

#: Ceiling on the DIGIT COUNT of a purely numeric power's result. A numeric
#: power is cheap to compute and ruinous to print — Python refuses to render an
#: integer over 4300 digits at all (sys.set_int_max_str_digits), which is how
#: `factorial(99999)` escaped as an uncaught ValueError from deep inside
#: SymPy's printer rather than as a result. This bounds the answer to something
#: that can actually be returned.
MAX_NUMERIC_DIGITS = 4_000


#: The `parse_expr` transformations every safe-parse call site enables:
#: implicit multiplication (`2x`) and `^` as power. A single cached tuple
#: (THE-889) rather than logic.py/linalg.py/exact.py each keeping their own
#: copy — three copies is how one of them drifts unnoticed.
_MATH_TRANSFORMS = None


def math_transforms():
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


def safe_parse(expression: str, *, evaluate: bool = True, local_dict: dict | None = None):
    """The full safe-parse pipeline, in one place (THE-889) — the ONLY place
    in this package that calls SymPy's `parse_expr` on a caller string.
    Every symbolic-parsing tool delegates to this: `logic._evaluate_expression`,
    `logic._parse_solve_piece` (THE-888), `linalg._parse_entry` (THE-887), and
    `exact.py`'s `algebraic_equiv`/`solve_expression`/`limit_expression`/
    `simplify_expression` (THE-889).

    Before THE-889, `logic._evaluate_expression`, `logic._parse_solve_piece`
    and `linalg._parse_entry` each hand-rolled the same three steps ahead of
    a caller string reaching SymPy — this is that logic, factored out:

      1. `classify_unsafe` — the syntax denylist above (attribute access,
         string literals, leading underscores, ...).
      2. `parse_expr(..., evaluate=False)` through `safe_global_dict()`, then
         `reject_explosive` on the UNEVALUATED shape. Cheap (1ms on a power
         tower) and, crucially, has not done the arithmetic yet — `9**9**9**9`
         is refused in milliseconds instead of burning real CPU seconds.
      3. A second, real parse — still through `safe_global_dict()`, never
         SymPy's default builtins-populated dict — only if step 2 passed and
         the caller wants the evaluated value.

      `safe_global_dict()` is what step 3 buys beyond step 1: a screened,
      non-denylisted NAME can still be a live Python builtin.
      `input`/`breakpoint`/`quit` carry none of classify_unsafe's denied
      syntax, so they pass step 1 — but SymPy's DEFAULT `global_dict` is
      `vars(builtins)` copied in, so a bare `sympify("input()")` would CALL
      the real `input()`, reading stdin (fd 0, shared with a stdio MCP
      server). `safe_global_dict()` has no Python builtins in it at all, so
      an undefined name parses to a harmless symbolic `Function` or a clean
      parse error instead of a live call.

    Three call sites doing this by hand is three chances for a fourth to do
    it differently — THE-889's `exact.py` tools were the proof: each did
    `classify_unsafe` then a bare `sp.sympify`, skipping steps 2 and 3
    entirely. This is the anti-regression move: one entry point, so a future
    caller can't do classify_unsafe-but-forget-the-safe-parse.

    Returns `(value, None)` on success, or `(None, (category, message))` on
    failure — the SAME `(category, message)` shape `classify_unsafe` returns
    on its own, so a caller maps it to its own error code with
    `errors.code_for_safe_expr_category(category)` exactly as it already
    does for a bare `classify_unsafe` call. A parse-time exception and an
    explosive shape are reported as `CATEGORY_VALIDATION` /
    `CATEGORY_CEILING` respectively — the same two categories a caller
    already maps for classify_unsafe's own findings.

    `evaluate=False` returns the unevaluated shape from step 2 instead of
    doing step 3 — the caller wants the tree, not a value (matches the
    original `sp.sympify(raw, evaluate=False)` no-`=` path in
    `logic._solve_linear`).
    """
    cls = classify_unsafe(expression)
    if cls:
        return None, cls
    from sympy.parsing.sympy_parser import parse_expr

    try:
        shape = parse_expr(expression, transformations=math_transforms(),
                           local_dict=local_dict, global_dict=safe_global_dict(),
                           evaluate=False)
    except Exception as exc:
        return None, (CATEGORY_VALIDATION, f"parse error: {exc}")
    explosive = reject_explosive(shape)
    if explosive:
        return None, (CATEGORY_CEILING, explosive)
    if not evaluate:
        return shape, None
    try:
        value = parse_expr(expression, transformations=math_transforms(),
                           local_dict=local_dict, global_dict=safe_global_dict())
    except Exception as exc:
        return None, (CATEGORY_VALIDATION, f"parse error: {exc}")
    return value, None


def reject_explosive(tree) -> str | None:
    """Reason this PARSED expression must not be evaluated, or None.

    Takes the tree from `parse_expr(..., evaluate=False)`, which is cheap
    (1ms on a power tower) and, crucially, has not done the arithmetic yet.
    This is the half of the bound that the token screen cannot reach: `**` is
    an operator, so its cost is invisible until the operands are known.

    THE-901: this is a FAST PATH for the power-tower shape, not a complete
    bound on every way an expression can be expensive — worth stating
    explicitly, because "the shape guard" is an easy thing for a future
    reader to over-trust. `9**9**9**9` and `2**100000` are caught here
    because their cost is invisible until AFTER a cheap, already-parsed tree
    is inspected: the danger is in the VALUE two operands produce, not in
    building the tree that holds them.

    Deeply nested parens combined with a long chain of repeated terms
    (`"("*130 + "1.5+2.3"*410 + ")"*130`, found by `scripts/fuzz.py`) is a
    DIFFERENT shape of expensive, and this function cannot bound it: the cost
    there is inside SymPy's own recursive-descent parser BUILDING the tree —
    it shows up as a caught `RecursionError` ("maximum recursion depth
    exceeded") that this function never gets a chance to inspect, because
    `safe_parse` only calls `reject_explosive` on a tree that `parse_expr`
    already finished constructing. A structural check here, however cheap,
    cannot bound work that happens before there is a tree to check.

    A pre-parse guard that estimated nesting depth x repeated-term count on
    the RAW STRING was considered instead, to bound the recursion before
    `parse_expr` ever runs. Measured, that interaction is noisy and
    superlinear in ways that don't reduce to a safe closed-form cap without
    either being toothless (too high to catch the slow cases) or rejecting
    ordinary expressions with a handful of legitimate nested parens — the
    same "denylist that has to anticipate everything" trade `guarded.py`'s
    own module docstring already rejected for the *evaluation* side, here on
    the *parsing* side instead.

    What actually bounds this shape is measured, not assumed: every caller of
    `safe_parse` (`evaluate_expression`, `algebraic_equiv`, `solve_expression`,
    `limit_expression`, `simplify_expression`, `matrix`, `solve_linear`) runs
    it through `guarded.guarded_call`, which enforces `RLIMIT_CPU` (10s) on
    the forked child and a 15s wall-clock in the parent regardless — and the
    2000-char `_MAX_EXPR_LEN` every one of those callers already enforces
    before a string reaches here keeps the worst measured cost at this shape
    (~5.3s, `scripts/fuzz.py --seed 777`) comfortably under both. Past that
    length cap the same shape gets worse without an obvious ceiling
    (`scripts/fuzz.py`'s module docstring measured 3.5s at roughly 2x the
    length with no sign of levelling off) — which is exactly why every caller
    keeps the length cap AND the guarded_call backstop, rather than treating
    either alone as sufficient. See `tests/test_bug_sweep.py`'s THE-901 block
    for the assertion that the backstop actually holds for this shape.
    """
    from sympy import Float, Integer, Pow

    for node in _walk(tree):
        if not isinstance(node, Pow):
            continue
        base, exponent = node.base, node.exp

        # A tower: the exponent is itself a power. `9**9**9**9` is four
        # characters of input and an integer with more digits than there are
        # atoms in the observable universe. There is no threshold worth
        # picking here — the shape itself is the problem.
        if isinstance(exponent, Pow):
            return ("a power tower (an exponent that is itself a power) is not "
                    "evaluated: the result grows faster than any useful bound")

        if not isinstance(exponent, (Integer, Float)):
            continue  # symbolic exponent, e.g. x**n — nothing to expand
        try:
            exp_value = int(exponent)
        except (TypeError, ValueError, OverflowError):
            return "the exponent is not a value this can bound"
        # abs(), because the danger is the MAGNITUDE. `2**(-1000000)` is a
        # rational with a million-bit denominator, and `exp_value <= 1` waved
        # every negative exponent through on its way to catching 0 and 1.
        if abs(exp_value) <= 1:
            continue

        if base.free_symbols:
            if exp_value > MAX_SYMBOLIC_EXPONENT:
                return (f"a symbolic power with exponent {exp_value} exceeds the "
                        f"limit of {MAX_SYMBOLIC_EXPONENT}: expanding it is "
                        "superlinear in the exponent")
        elif isinstance(base, (Integer, Float)):
            # Cheap to compute, impossible to print. Estimated by digit count
            # rather than by computing it, which would be the very thing this
            # is here to avoid.
            try:
                magnitude = abs(float(base))
            except (TypeError, ValueError, OverflowError):
                continue
            if magnitude > 1:
                import math
                # abs() here too. A negative exponent produces a rational
                # whose DENOMINATOR has that many digits, which costs the same
                # to build; without it `digits` came out negative and every
                # negative exponent compared under the cap.
                digits = abs(exp_value) * math.log10(magnitude)
                if digits > MAX_NUMERIC_DIGITS:
                    return (f"the result would have about {int(digits)} digits, "
                            f"over the limit of {MAX_NUMERIC_DIGITS}: it cannot be "
                            "rendered as a decimal string")
    return None


def _walk(node):
    """Every node in a SymPy expression tree, parents before children."""
    stack = [node]
    seen = 0
    while stack:
        current = stack.pop()
        seen += 1
        # A guard against a pathological tree rather than a real expectation:
        # the length cap upstream bounds how big this can get, and 20k nodes is
        # far past anything a 2000-character expression produces.
        if seen > 20_000:
            return
        yield current
        args = getattr(current, "args", ())
        # Found by scripts/fuzz.py (THE-899): `parse_expr("binomial", ...)`
        # (a heavy-function NAME used bare, with no call parens) resolves to
        # the sympy FunctionClass itself rather than an instance — and
        # `FunctionClass.args`, accessed on the class rather than an
        # instance, returns the unbound `property` DESCRIPTOR object, not a
        # tuple. `getattr` above does not catch this: the attribute exists,
        # it just is not the shape every other node's `.args` is. Without
        # this check, `stack.extend(a_property_object)` raised an uncaught
        # `TypeError: 'property' object is not iterable` for ANY bare
        # reference to a name in `_HEAVY_FUNCTIONS` (binomial, factorial,
        # gamma, ...) reachable through every symbolic tool that calls
        # `safe_parse`. A node with a malformed `args` has no children this
        # walk can trust, so it is treated as a leaf rather than expanded.
        if isinstance(args, tuple):
            stack.extend(args)
