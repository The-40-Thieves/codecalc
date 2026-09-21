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
import math
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


def _log10_of_int(n: int) -> float:
    """log10(abs(n)) for an arbitrary-precision int, computed without ever
    converting the full value to float.

    `float(n)` is itself an unbounded operation once `n` has enough digits:
    Python's own `int.__float__` raises `OverflowError`, but — measured
    live, and a crash this exists to avoid — SymPy's `Integer.__float__`
    instead returns `inf` WITHOUT raising, so a caller relying on the
    `except OverflowError` that catches the former silently sails past the
    latter.

    Shifts `n` down to its top 53 bits first (the exact width a double's
    mantissa holds, so `float()` of the shifted value loses nothing that
    matters), then adds back log10 of the shifted-off power of two. Accurate
    to within float precision for any size of `n`, unlike a cruder
    `bit_length() * log10(2)` estimate, which is off by up to a full bit
    (~0.3 decimal digits) — negligible on its own, but that error scales
    with the EXPONENT wherever this feeds a `digits = exponent *
    log10_magnitude` estimate, so a naive bit-count approximation here was
    measured to flip some ordinary, well-under-the-cap powers (`2**10000`,
    ~3011 true digits) to a false refusal.
    """
    n = abs(n)
    if n == 0:
        return float("-inf")
    shift = max(n.bit_length() - 53, 0)
    return math.log10(n >> shift) + shift * math.log10(2)


def _approx_decimal_digits(n: int) -> int:
    """~decimal digit count of `abs(n)`, without stringifying it.

    `str(n)` is itself an unbounded operation on an arbitrary-precision int:
    it hits Python's int->str conversion limit (4300 digits by default,
    `sys.set_int_max_str_digits`) and raises `ValueError` past it — so a
    refusal MESSAGE that interpolates a huge `n` directly can crash before
    it finishes reporting the refusal. Used here (a heavy-function argument
    literal — `factorial(0x` + `f`*4000 + `)` parses to a plain `int` with
    thousands of decimal digits even though the source LITERAL is short,
    because hex/octal/binary text->int conversion has no such length limit,
    only the decimal str() this avoids does) and by `reject_explosive`
    below. Used only for prose, not for gating a decision (`_log10_of_int`
    above does that), so it does not need `_log10_of_int`'s float precision
    — `int()` truncation is already "about N digits", not an exact count.
    """
    n = abs(n)
    return int(_log10_of_int(n)) + 1 if n else 1


#: Bit-length ceiling for `_bounded_numeric_value`'s OWN intermediate work —
#: not `MAX_NUMERIC_DIGITS` itself (~13,288 bits). Generous above it (20,000
#: bits, ~6,021 decimal digits) so an ordinary over-the-cap result still
#: resolves to an EXACT value — the refusal message can report a real digit
#: count instead of just "cannot be bounded" — while staying small enough
#: that computing it is milliseconds of arbitrary-precision arithmetic,
#: never the unbounded blow-up this function exists to avoid.
_SUBTREE_BIT_BUDGET = 20_000

#: `_SUBTREE_BIT_BUDGET` expressed in decimal digits (log10, not bits) —
#: `_resolve_numeric_exactly` uses it as the "safe to materialize an exact
#: value" line, comparing directly against a `_log10_magnitude` estimate
#: rather than converting a magnitude back to bits every call.
_SAFE_RECONSTRUCT_DIGITS = _SUBTREE_BIT_BUDGET * 0.3010299956639812  # log10(2)


class _NumericTooLarge:
    """Sentinel `_bounded_numeric_value` returns when it has PROVED, via
    exact bit-length arithmetic on an already-materialized value, that a
    numeric-only subtree's magnitude exceeds `bit_budget` — never a guess.

    Deliberately distinct from returning `None` (genuinely INCONCLUSIVE: a
    `Function` call, an irrational `NumberSymbol`, float-mode overflow, or
    any shape outside the safe grammar below). The two must not be
    conflated: a caller that cannot tell whether a subtree is dangerous has
    no business refusing it pre-parse — the correct move is to fall through
    to whatever the existing downstream screen already does (the same
    `guarded_call` CPU/wall-clock backstop and the callers' own
    `_RESOURCE_ERRORS` catches that handled every one of these shapes
    before this function existed) — but a caller that HAS proof (this
    sentinel) is right to refuse. Conflating the two was an earlier version
    of this fix's bug: refusing `2**cos(0)` (a `Function` — no evidence
    either way, and the true value is 2) exactly as if it had proved
    `2**cos(0)` explosive, which it never did.
    """

    __slots__ = ("bits",)

    def __init__(self, bits: int):
        self.bits = bits  # a LOWER bound on the true bit length, not exact


def _bounded_numeric_value(node, bit_budget: int = _SUBTREE_BIT_BUDGET):
    """Resolve a NUMERIC-ONLY subtree (no `free_symbols`) as far as it can
    be resolved cheaply. Returns one of three DIFFERENT things, and the
    difference is load-bearing — see `_NumericTooLarge` above:

      - an exact SymPy `Rational`/`Integer`, or a Python `float`: the
        resolved value (a `float` only when a `Float` literal took part
        anywhere in the subtree — see FLOAT MODE below).
      - `None`: inconclusive. A `Function` call, an irrational
        `NumberSymbol` (`pi`, `E`, ...), float-mode overflow/precision
        loss, or any shape this has no case for. NOT evidence of
        anything — a caller should fall through, not refuse.
      - a `_NumericTooLarge`: PROVED to exceed `bit_budget`, via exact
        bit-length arithmetic. A caller should refuse.

    `parse_expr(..., evaluate=False)` leaves `30000/2` as `Mul(30000,
    Pow(2, -1))` and `3/2` as `Mul(3, Pow(2, -1))` rather than folding them
    to a plain `Integer`/`Rational` — so `reject_explosive`'s own
    `isinstance(exponent, (Integer, Float))` / `isinstance(base, (Integer,
    Float))` checks skip them entirely, and the actual power gets computed
    later with no ceiling ever applied: `safe_parse("2**(30000/2)")`
    returned a computed 4,516-digit `Integer`, `safe_parse("(3/2)**30000")`
    a Rational with a 47,549-bit numerator — no refusal at all. This walks
    that kind of arithmetic-only subtree through a small safe grammar of
    cheap combinators (`Integer`, `Rational`, `Float`, `Add`, `Mul`, `Pow`
    with an integer exponent), checking each combination's SIZE against
    `bit_budget` on the ACTUAL (already-reduced) accumulator before
    continuing, never on a pessimistic pre-sum of the operands' own sizes —
    `2**(2**10000 * 2**-10000)` (magnitude exactly 1, the two factors
    cancel) must NOT be refused just because each individual factor's OWN
    bit length is ~10,000; SymPy's Rational multiplication always reduces
    via GCD, so the accumulator's TRUE size after each step is what this
    checks, and it reflects the cancellation correctly.

    FLOAT MODE: a `Float` leaf (e.g. `2*1.5`, `1.5+1.5`, `(1.5*2)**3`) is
    never a size hazard by itself — it is always fixed (double) precision —
    but it does not mix with exact `Rational` bookkeeping. The first time a
    `Float` appears anywhere in an `Add`/`Mul` chain, the WHOLE combination
    promotes to ordinary bounded Python `float` arithmetic (the same
    int-then-float promotion Python itself does), which needs no
    bit-length budget at all: a `float` op is always O(1), fixed-size, and
    the only failure mode is `OverflowError` on a value already too big
    for a double — treated as inconclusive (`None`), not as proof of being
    over `MAX_NUMERIC_DIGITS` (a double overflows past ~308 decimal
    digits, well under the 4,000-digit cap, so overflow alone proves
    nothing about the true digit count). This is a documented, narrow
    precision limit: a `Float` base combined with a very large exact
    integer exponent (untested by anything in this repo, and not shaped
    like any known attack) can lose the ability to prove a still-under-cap
    result safe and fall through instead of resolving exactly — falling
    through is always safe here, just less precise than exact mode.

    Why the size check has to come FIRST for a `Pow`: `factorial2(100000)/2`
    (this module's own worked caution elsewhere) is `Mul(factorial2(100000),
    Rational(1, 2))` structurally — and by the time this runs,
    `factorial2(100000)` is *already* a concrete, thousands-of-digits
    `Integer`: a function call on a literal evaluates during parsing (see
    the `_HEAVY_FUNCTIONS` comment above), before `reject_explosive` ever
    sees the tree. Multiplying that already-materialized Integer by `1/2`
    is cheap either way — the actual danger is a `Pow` INSIDE the subtree
    whose exponent is itself huge: `bit_length(base) * exponent` is checked
    before `base ** exponent` is ever called, the same guard
    `reject_explosive`'s own numeric-base branch already applies to the
    OUTER power, applied here to every power inside this one too — EXCEPT
    when `|base| <= 1` (base is `-1`, `0`, or `1`), where the result never
    grows no matter how large the exponent's VALUE is (fast exponentiation
    keeps every intermediate at the base's own tiny size, and the exponent
    itself is already known to fit `bit_budget` bits from its own
    resolution) — skipping the size check there is what a bare-Integer
    `MAX_SYMBOLIC_EXPONENT`/ceiling check does too; without it, `base_bits
    * exp_int` for `base_bits == 1` would falsely flag `1 ** (a genuinely
    huge but individually-bounded exponent)` as too large, when `1 ** n`
    is `1` regardless of `n`.
    """
    from sympy import Add, Float, Integer, Mul, Pow, Rational

    if isinstance(node, Integer):
        n = int(node)
        bl = n.bit_length()
        return _NumericTooLarge(bl) if bl > bit_budget else node
    if isinstance(node, Rational):  # Integer is also a Rational; caught above
        bl = max(node.p.bit_length(), node.q.bit_length())
        return _NumericTooLarge(bl) if bl > bit_budget else node
    if isinstance(node, Float):
        try:
            return float(node)
        except (OverflowError, ValueError):
            return None

    if isinstance(node, Mul):
        acc = Rational(1)
        float_mode = False
        for arg in node.args:
            resolved = _bounded_numeric_value(arg, bit_budget)
            if resolved is None or isinstance(resolved, _NumericTooLarge):
                return resolved  # propagate: inconclusive stays inconclusive, proved-large stays proved-large
            if isinstance(resolved, float) and not float_mode:
                try:
                    acc = float(acc)  # promote the exact accumulator so far -- see FLOAT MODE above
                except (OverflowError, ValueError):
                    return None
                float_mode = True
            if float_mode:
                try:
                    acc = acc * (resolved if isinstance(resolved, float) else float(resolved))
                except (OverflowError, ValueError, ZeroDivisionError):
                    return None
            else:
                acc = acc * resolved
                bl = max(acc.p.bit_length(), acc.q.bit_length())
                if bl > bit_budget:
                    return _NumericTooLarge(bl)  # the ACTUAL reduced product, not a pre-sum estimate
        return acc

    if isinstance(node, Add):
        acc = Rational(0)
        float_mode = False
        for arg in node.args:
            resolved = _bounded_numeric_value(arg, bit_budget)
            if resolved is None or isinstance(resolved, _NumericTooLarge):
                return resolved
            if isinstance(resolved, float) and not float_mode:
                try:
                    acc = float(acc)
                except (OverflowError, ValueError):
                    return None
                float_mode = True
            if float_mode:
                try:
                    acc = acc + (resolved if isinstance(resolved, float) else float(resolved))
                except (OverflowError, ValueError):
                    return None
            else:
                acc = acc + resolved
                bl = max(acc.p.bit_length(), acc.q.bit_length())
                if bl > bit_budget:
                    return _NumericTooLarge(bl)
        return acc

    if isinstance(node, Pow):
        exp_resolved = _bounded_numeric_value(node.exp, bit_budget)
        if exp_resolved is None or isinstance(exp_resolved, _NumericTooLarge):
            return exp_resolved
        base_resolved = _bounded_numeric_value(node.base, bit_budget)
        if base_resolved is None or isinstance(base_resolved, _NumericTooLarge):
            return base_resolved

        if isinstance(exp_resolved, float) or isinstance(base_resolved, float):
            # `float ** float` is O(1) regardless of the exponent's
            # magnitude (bounded double range) -- unlike int ** int, no
            # bit-length guard is needed, only an overflow catch. See
            # FLOAT MODE above for why overflow here means inconclusive,
            # not proved-too-large.
            try:
                base_f = base_resolved if isinstance(base_resolved, float) else float(base_resolved)
                exp_f = exp_resolved if isinstance(exp_resolved, float) else float(exp_resolved)
                return base_f ** exp_f
            except (OverflowError, ValueError, ZeroDivisionError):
                return None

        if exp_resolved.q != 1:
            return None  # a genuinely fractional exact exponent -- irrational result, not a huge integer
        exp_int = int(exp_resolved)
        if exp_int == 0:
            return Rational(1)
        base_bits = max(base_resolved.p.bit_length(), base_resolved.q.bit_length(), 1)
        if base_bits > 1:  # |base| not in {-1, 0, 1} -- growth is possible, bound BEFORE computing
            needed = base_bits * abs(exp_int)
            if needed > bit_budget:
                return _NumericTooLarge(needed)
        # |base| in {-1, 0, 1}, or already proved to fit above: safe to
        # compute directly. `x ** exp_int` for |x| <= 1 stays bounded no
        # matter how large exp_int's VALUE is (fast exponentiation keeps
        # every intermediate at |x|'s own size), and exp_int itself is
        # already known to fit bit_budget bits (it came from a
        # successfully-resolved exp_resolved above).
        try:
            return base_resolved ** exp_int
        except (ZeroDivisionError, ValueError):
            return None

    return None  # a Function call, pi/E/I, or any other shape -- inconclusive, not proof


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
                    # `str(value)` is safe and exact for the overwhelmingly
                    # common case (a decimal literal, or a short hex/octal/
                    # binary one) — but not in general. `int(s, 0)` parses a
                    # hex/octal/binary LITERAL in linear time regardless of
                    # length (unlike decimal text<->int conversion, which is
                    # exactly what str() below does, and exactly what
                    # carries a length limit), so a short-looking token like
                    # `factorial(0x` + `f`*4000 + `)` parses to a plain int
                    # with thousands of DECIMAL digits, and `str(value)`
                    # itself raises ValueError past Python's int->str
                    # conversion limit (4300 digits by default) before the
                    # refusal message could even be built. Fall back to the
                    # (cheap, bounded) digit count in that case — the same
                    # fix as `reject_explosive`'s refusal messages below.
                    try:
                        value_desc = str(value)
                    except ValueError:
                        value_desc = f"(~{_approx_decimal_digits(value)} digits)"
                    # Names the ARGUMENT, not `func(value)`. The latter reads
                    # as the whole call, which is wrong the moment the function
                    # takes two: `binomial(200000, 100000)` was reporting
                    # itself as "binomial(200000)".
                    return (f"argument {value_desc} to {tok.string}() exceeds the limit "
                            f"of {MAX_HEAVY_ARG}: computing it would take an "
                            "unbounded amount of time and memory")
    return None


#: Categories `classify_unsafe` hands back, for a CALLER to map to its own
#: error taxonomy. Deliberately neutral strings rather than
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
        #     by scripts/fuzz.py;
        #   - UnicodeDecodeError — a bare replacement char / truncated multibyte
        #     shape (e.g. "�\r�"), found by the ClusterFuzzLite
        #     coverage-guided harness (follow-up) that the seeded mutator
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
                # Named explicitly (GH #223) rather than left to the
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
    ceiling, or a plain validation mistake — they are not interchangeable)
    should call `classify_unsafe` instead; this wrapper exists
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
#: rather than logic.py/linalg.py/exact.py each keeping their own
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
    """The full safe-parse pipeline, in one place — the ONLY place
    in this package that calls SymPy's `parse_expr` on a caller string.
    Every symbolic-parsing tool delegates to this: `logic._evaluate_expression`,
    `logic._parse_solve_piece`, `linalg._parse_entry`, and
    `exact.py`'s `algebraic_equiv`/`solve_expression`/`limit_expression`/
    `simplify_expression`.

    `logic._evaluate_expression`, `logic._parse_solve_piece` and
    `linalg._parse_entry` used to each hand-roll the same three steps ahead
    of a caller string reaching SymPy — this is that logic, factored out:

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
    it differently — `exact.py`'s tools were the proof: each did
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


def _safe_log10_pow(base_mag: float, exp_int: int) -> float:
    """`base_mag * exp_int` without ever converting a HUGE `exp_int` to
    float directly.

    `float * int` coerces the int to float first, and CPython's own
    `int.__float__` raises `OverflowError` past roughly 1.8e308 — the exact
    crash class `reject_explosive` was already fixed for once (see
    `tests/test_bug_sweep.py`'s `_EXPLOSIVE_CRASH_INPUTS`:
    `"2**100000!!ubrNembeubrNember"` parses to `Pow(2, factorial2(100000))`,
    an exact Integer EXPONENT with thousands of digits — `!!` is postfix
    operator syntax, invisible to `_heavy_call_violation`'s pre-parse
    literal-argument scan, which looks for a `NAME(` call it can see in the
    token stream). Reproduced here: this function is exactly where that
    exponent's magnitude gets multiplied in, so it inherits the same risk.

    Bit length, not the value, decides: past ~1000 bits (~1.07e301, still
    safely under a double's ~1.8e308 range but already far larger than
    `MAX_NUMERIC_DIGITS` could ever need) the result is astronomically over
    any real digit cap for any nonzero `base_mag`, so the multiplication is
    skipped entirely in favour of a signed `inf` — never touching
    `float(exp_int)`. Same cutoff `reject_explosive`'s own numeric-base Pow
    branch already uses for the identical reason.
    """
    if base_mag == 0:
        return 0.0
    if abs(exp_int).bit_length() > 1000:
        huge = float("inf")
        return huge if (base_mag > 0) == (exp_int > 0) else -huge
    return base_mag * exp_int


def _factor_multiset(node, memo_ms: dict):
    """Decompose a numeric-only subtree into an EXACT signed multiset of
    `{base_magnitude_key: net_integer_exponent}`, or `None` if this node's
    shape is not decomposable this way (anything but `Integer` / `Mul` /
    `Pow` with a plain `Integer` exponent — a `Rational` literal, `Float`,
    `Add`, `Function`, symbol, or irrational `NumberSymbol`). Returns
    `(terms, sign)`, where `sign` is -1/0/+1 and the true value is
    `sign * prod(base**exp for base, exp in terms.items())` — `terms` never
    holds a zero exponent (a base that fully cancels is deleted, not kept
    at 0).

    THE INVARIANT THIS FILE NOW ENFORCES (#326, four cross-vendor review
    rounds in): no numeric subtree whose PRINTED numerator or denominator
    would exceed `MAX_NUMERIC_DIGITS` is ever evaluated — not "no numeric
    subtree whose VALUE is large" (round 1's bug: `str()` renders a
    Rational's numerator and denominator SEPARATELY, so a value near zero
    with an 8000-digit denominator — `1/(factorial(1463)*factorial(1463))`
    — is exactly as dangerous to print as a value with an 8000-digit
    numerator, even though its magnitude/log is deeply negative) and "no
    numeric subtree is checked in isolation from a combination that
    provably cancels it" (rounds 2 and 3's bug: an ORDERED accumulator —
    `_bounded_numeric_value`, still used elsewhere in this module for
    nothing `reject_explosive` calls anymore — blows its own intermediate
    budget on `factorial(1463)*factorial(1463)` before ever seeing the
    factor that cancels it back to 1, and `SymPy`'s `evaluate=True`
    reconstruction has the identical problem one level deeper: `Mul.flatten`
    computes `coeff *= Pow(base, exp)` for each integer power of a numeric
    base BEFORE the whole product is known to cancel, so `2**(2**N *
    3**(-N))` — final magnitude negligible — still tries to materialize the
    literal ~10**298-digit integer `2**N` on the way there for a ~300-digit
    `N`, whether that materialization happens through this module's own
    accumulator or through SymPy's).

    This function is the fix for the residual half of that: EXACT
    cancellation via matching a repeated base by VALUE (Python integer
    equality, not floating-point log comparison — `f*f/(f*f)`'s two `f`
    factors are the literal same materialized `Integer`, and grouping by
    `abs(int(base))` cancels their net exponent to precisely 0, not merely
    "close to 0"), followed by measuring the SURVIVING terms' magnitude in
    log space (`_safe_log10_pow`, never `base**exp` itself) — so a factor
    that does not cancel and is individually enormous (`2**N` with no
    matching `3**N` to pair against) is measured, never materialized,
    regardless of what the surrounding product's NET value turns out to be.

    Recurses through `Mul` (summing each arg's own multiset, net-cancelling
    matching bases as it merges) and through `Pow(base, k)` for a plain
    Integer `k` (recursing into `base`'s own multiset first, then scaling
    every net exponent by `k` — `Pow(Mul(f, f), -1)` becomes `{f: -2}` by
    resolving `Mul(f, f)` to `{f: 2}` first and scaling by -1). A `Pow`
    whose exponent is not a plain `Integer` — the compound/cancelling
    exponent shape above — is NOT resolved here: it is left for the caller
    (`_log10_num_den`) to treat as opaque, because the EXPONENT position
    needs a fundamentally different question answered (is this an exact
    small integer, safe to use as a multiplier?) than the base-position
    multiset this function builds.
    """
    from sympy import Integer, Mul, Pow

    key = id(node)
    if key in memo_ms:
        return memo_ms[key]
    result = None
    if isinstance(node, Integer):
        n = int(node)
        result = ({}, 0) if n == 0 else ({abs(n): 1}, 1 if n > 0 else -1)
    elif isinstance(node, Mul):
        terms: dict = {}
        sign = 1
        for arg in node.args:
            sub = _factor_multiset(arg, memo_ms)
            if sub is None:
                terms, sign = None, None
                break
            sub_terms, sub_sign = sub
            if sub_sign == 0:
                terms, sign = {}, 0
                break
            sign *= sub_sign
            for base_key, exp in sub_terms.items():
                net = terms.get(base_key, 0) + exp
                if net:
                    terms[base_key] = net
                else:
                    terms.pop(base_key, None)
        if terms is not None:
            result = (terms, sign)
    elif isinstance(node, Pow) and isinstance(node.exp, Integer):
        exp_int = int(node.exp)
        if exp_int == 0:
            result = ({}, 1)
        else:
            sub = _factor_multiset(node.base, memo_ms)
            if sub is not None:
                sub_terms, sub_sign = sub
                if sub_sign == 0:
                    # 0**positive is 0; 0**negative is undefined (would be
                    # a ZeroDivisionError at real evaluation) -- not this
                    # function's business to predict, so treat as opaque
                    # rather than guessing.
                    result = ({}, 0) if exp_int > 0 else None
                else:
                    scaled = {base_key: exp * exp_int for base_key, exp in sub_terms.items()}
                    # value = (sub_sign * positive)**exp_int = sub_sign**exp_int *
                    # positive**exp_int, and sub_sign is -1 or 1, so sub_sign**exp_int
                    # is 1 for an even exponent and sub_sign itself for an odd one
                    # (Python's `%` follows the divisor's sign, so this is correct
                    # for a negative exp_int too: -3 % 2 == 1, treated as odd).
                    new_sign = 1 if exp_int % 2 == 0 else sub_sign
                    result = (scaled, new_sign)
    memo_ms[key] = result
    return result


def _multiset_log_num_den(terms: dict) -> tuple:
    """log10(|numerator|), log10(|denominator|) for an exact `_factor_
    multiset` result, via `_safe_log10_pow` per term (never `base**exp`
    itself) — a positive net exponent is a numerator factor, negative is a
    denominator factor, matching how `str()` would actually render the
    reduced fraction.
    """
    num_parts = [_safe_log10_pow(_log10_of_int(base_key), exp)
                 for base_key, exp in terms.items() if exp > 0]
    den_parts = [_safe_log10_pow(_log10_of_int(base_key), -exp)
                 for base_key, exp in terms.items() if exp < 0]
    log_num = math.fsum(num_parts) if num_parts else 0.0
    log_den = math.fsum(den_parts) if den_parts else 0.0
    if math.isnan(log_num):
        log_num = float("inf")
    if math.isnan(log_den):
        log_den = float("inf")
    return log_num, log_den


def _safe_multiset_rational(terms: dict, sign: int):
    """The EXACT `Fraction` an `_factor_multiset` result represents, or
    `None` if any INDIVIDUAL term is too large to safely materialize — a
    per-term gate, not a check on the combined/final magnitude, and that
    distinction is the whole point (#326 finding 3, round 4): checking
    only the FINAL magnitude is exactly the bug in `_resolve_numeric_
    exactly`, the previous version of this idea, which let `2**(2**N *
    3**(-N))` through because the NET log was small even though the
    individual factor `2**N` (N a ~300-digit number) was not.

    Needed because `_factor_multiset`'s own cancellation is structural —
    it matches an IDENTICAL base by value, not by common factor — so
    `Mul(30000, Pow(2, -1))` (`30000/2`, an exact integer, 15000) does
    NOT reduce to `{}` the way `Pow(Mul(f, f), -1)` reduces against a
    matching `Mul(f, f)`: 30000 and 2 are different multiset keys, and
    `{30000: 1, 2: -1}` looks "genuinely fractional" (a negative net
    exponent survives) even though the TRUE value is an integer. Only
    Python's own `Fraction` (or SymPy's `Rational`) knows 30000 and 2
    share a factor of 2 — computing it is the only way to learn that —
    so this function exists to do so SAFELY: every term is checked
    against `_SAFE_RECONSTRUCT_DIGITS` (the same "generous but still
    cheap" bit-budget line `_SUBTREE_BIT_BUDGET` has used throughout this
    module) via `_safe_log10_pow` — never `base**exp` itself — BEFORE any
    of them are actually raised to a power. Only once every term
    individually clears that bound does this function touch real
    arithmetic (`Fraction`, which auto-reduces via `gcd` on every
    multiply), and at that point it is bounded: at most roughly 125 terms
    reach here at all (the 2000-char expression cap), each individually
    under ~6,021 decimal digits.
    """
    from fractions import Fraction

    for base_key, exp in terms.items():
        if _safe_log10_pow(_log10_of_int(base_key), abs(exp)) > _SAFE_RECONSTRUCT_DIGITS:
            return None
    value = Fraction(1)
    for base_key, exp in terms.items():
        value *= Fraction(base_key) ** exp
    return value * sign


def _log10_num_den(node, memo: dict) -> tuple:
    """`(log_num, log_den, fully_resolved)` for a subtree — log10 of the
    magnitude of the numerator and denominator `str()` would actually
    render if this subtree's value were printed as a reduced fraction, and
    whether that accounting is COMPLETE (no free symbol, `Function` call,
    irrational constant, or other opaque node anywhere inside).

    ALWAYS returns real numbers, never `None`: an opaque sub-node
    contributes `(0.0, 0.0)` — "nothing known, not assumed dangerous" — to
    whatever combines it, and `fully_resolved=False` propagates outward so
    a caller knows this is a PARTIAL answer (finding 1, cross-vendor review
    round 4 — a chain like `x * factorial(1463) * ... * factorial(1463)`
    is one flat `Mul`, and the danger is in the siblings that DO resolve,
    not in `x`; baking the partial-answer into this function's own return
    value, rather than a separate wrapper only the top of a scan remembers
    to call, is what makes every caller — the scan, an enclosing Mul/Add,
    a Pow's own base — see the same partial information automatically).

    Tries `_factor_multiset` first for an EXACT, cancellation-aware
    answer (the `Integer`/`Mul`/`Pow`-of-integer-exponent chain it
    handles). Falls back per node type otherwise:

      - `Rational` (rare from `evaluate=False` parsing, but not assumed
        impossible): `(log10|p|, log10|q|)`, exact, `fully_resolved=True`.
      - `Float`: SymPy prints a `Float` at fixed (default ~15 significant
        digit) precision regardless of its magnitude or exponent — it
        never routes through CPython's int->str ceiling at all, so it is
        never a hazard by this function's own definition. `(0.0, 0.0,
        True)`.
      - `Mul` whose full multiset did not resolve (a symbolic or opaque
        factor is mixed in): recurse into each arg and combine — num/den
        ADD independently across args (multiplication: `(a/b)*(c/d) =
        ac/bd`).
      - `Add`: a safe UPPER bound, not exact — combining fractions over a
        common denominator would need per-term GCD reduction, exactly the
        materialization this module exists to avoid. The combined
        denominator is bounded by the PRODUCT of every term's own
        denominator (summing logs), and each term's numerator, once
        placed over that common denominator, is scaled up by (up to) the
        OTHER terms' denominators too — so `log_num` is `max(term
        numerators) + log_den + log10(term count)`, not just the largest
        term's own numerator (omitting the `+ log_den` was a real bug
        caught writing this: `1/2 + 1/(factorial(1463)*factorial(1463))`
        has TINY per-term numerators (1 and 1) but an ~7996-digit
        COMBINED numerator once put over the shared denominator).
        `fully_resolved` is the AND of every arg's, either way.
      - `Pow(base, k)` for a plain Integer `k`: scale `base`'s own
        `(log_num, log_den)` by `_safe_log10_pow` (never `base**k`
        itself) — `k>0` grows the numerator side, `k<0` swaps and grows
        the denominator side. `fully_resolved` follows the base's.
      - `Pow` with any other exponent shape (compound, symbolic, or
        itself a `Pow` — a tower): `(0.0, 0.0, False)`. Deliberately NOT
        resolved here, even for the "exponent cancels to a small exact
        integer" case (`2**(f*f/(f*f))`) — attempting to combine an
        unresolved exponent's OWN value with a base is exactly the
        pattern that broke in three different ways across rounds 2-4.
        Instead, the SCAN (`_numeric_ceiling_scan`, which now descends
        into a `Pow`'s base AND exponent as ordinary child nodes — finding
        1) visits the exponent as its own subtree and checks IT through
        this exact same function; a fully-cancelling exponent resolves to
        `(0, 0, True)` there and is correctly let through, and a
        genuinely dangerous one (`2**N * 3**(-N)`, non-cancelling
        different bases) is caught there too, without this function ever
        needing to know which case it is.
      - Anything else (`Symbol`, `Function`, an irrational `NumberSymbol`,
        `Rational`-with-non-Integer-parts, ...): `(0.0, 0.0, False)`.
    """
    from sympy import Add, Float, Integer, Mul, Pow, Rational

    key = id(node)
    if key in memo:
        return memo[key]

    multiset = _factor_multiset(node, memo.setdefault("__multiset__", {}))
    if multiset is not None:
        terms, _sign = multiset
        log_num, log_den = _multiset_log_num_den(terms)
        result = (log_num, log_den, True)
    elif isinstance(node, Rational):
        log_num = _log10_of_int(node.p) if node.p else 0.0
        log_den = _log10_of_int(node.q) if node.q != 1 else 0.0
        result = (log_num, log_den, True)
    elif isinstance(node, Float):
        result = (0.0, 0.0, True)
    elif isinstance(node, Mul):
        parts = [_log10_num_den(arg, memo) for arg in node.args]
        log_num = math.fsum(p[0] for p in parts)
        log_den = math.fsum(p[1] for p in parts)
        result = (log_num, log_den, all(p[2] for p in parts))
    elif isinstance(node, Add):
        # Safe upper bound, not exact (combining fractions over a common
        # denominator would need per-term GCD reduction this module exists
        # to avoid materializing): the combined denominator is bounded by
        # the PRODUCT of every term's own denominator (summing their logs
        # is exact for that bound, never an under-count), and putting each
        # term's numerator over that common denominator SCALES IT UP by
        # (up to) the other terms' denominators too -- so the numerator
        # bound must add `log_den` in, not just the largest term's own
        # numerator. Omitting that was a real bug caught while writing
        # this: `1/2 + 1/(factorial(1463)*factorial(1463))` combines to
        # `(factorial(1463)*factorial(1463) + 2) / (2*factorial(1463)*
        # factorial(1463))` -- an ~7996-digit NUMERATOR -- and a formula
        # that only looked at each term's OWN numerator (1 and 1, both
        # tiny) would have reported `log_num=0`, missing it entirely.
        parts = [_log10_num_den(arg, memo) for arg in node.args]
        log_den = math.fsum(p[1] for p in parts)
        log_num = ((max(p[0] for p in parts) + log_den + math.log10(len(parts)))
                   if parts else 0.0)
        result = (log_num, log_den, all(p[2] for p in parts))
    elif isinstance(node, Pow) and isinstance(node.exp, Integer):
        exp_int = int(node.exp)
        if exp_int == 0:
            result = (0.0, 0.0, True)
        else:
            b_num, b_den, b_res = _log10_num_den(node.base, memo)
            if not b_res and abs(exp_int).bit_length() > 1000:
                # The base did not fully resolve (a free symbol, most
                # commonly — `x**20000`, whose Pow-loop-owned
                # MAX_SYMBOLIC_EXPONENT check is a COST ceiling, not a
                # print-digit one: `x**20000` is never rendered as a
                # decimal) AND the exponent is itself enormous, so there is
                # nothing sound to scale a merely PARTIAL base magnitude
                # by. Scaling an unresolved base's small partial numerator
                # by a huge `exp_int` was a real bug caught writing this:
                # `(x+1)**20000!!...` (`factorial2(20000)`, an exact
                # Integer with thousands of digits) scaled `(x+1)`'s own
                # tiny partial numerator (from its `+1` term alone) by
                # that huge exponent via `_safe_log10_pow`, producing a
                # spurious `+inf` and refusing on "unbounded digits in its
                # numerator" instead of correctly deferring to the Pow
                # loop's own, more accurate `MAX_SYMBOLIC_EXPONENT`
                # reason. `bit_length() > 1000` is the SAME threshold
                # `_safe_log10_pow` itself uses for "large enough that
                # scaling anything by this is not safely informative" —
                # below it, scaling a PARTIAL base's own known magnitude
                # (e.g. `(factorial(1463)*x)**1`'s base contributes
                # `factorial(1463)`'s own ~3998-digit numerator even
                # though `x` keeps the base itself unresolved) is exactly
                # the KNOWN-part accounting finding 1 already established
                # this file must not discard.
                result = (0.0, 0.0, False)
            elif exp_int > 0:
                result = (_safe_log10_pow(b_num, exp_int), _safe_log10_pow(b_den, exp_int), b_res)
            else:
                result = (_safe_log10_pow(b_den, -exp_int), _safe_log10_pow(b_num, -exp_int), b_res)
    elif isinstance(node, Pow):
        # A COMPOUND exponent -- not itself a bare Integer, but possibly
        # still an exact integer VALUE once reduced (`Mul(30000,
        # Pow(2, -1))` for `30000/2`, exactly 15000 — `_factor_multiset`'s
        # own cancellation cannot see that 30000 and 2 share a factor of
        # 2, since it only cancels an IDENTICAL repeated base, so this
        # needs `_safe_multiset_rational`'s per-term-gated exact value
        # instead (see its own docstring). A tower (`node.exp` itself a
        # `Pow`) or a symbolic exponent falls through to the final `else`
        # below via `_factor_multiset` returning `None` for it.
        multiset = _factor_multiset(node.exp, memo.setdefault("__multiset__", {}))
        if multiset is None:
            result = (0.0, 0.0, False)
        else:
            terms, sign = multiset
            value = _safe_multiset_rational(terms, sign) if sign else 0
            if value is None:
                # An individual term of the EXPONENT was itself too large
                # to safely materialize -- the scan's own independent
                # visit to this exponent (pushed alongside the base by
                # `_numeric_ceiling_scan`'s explicit Pow-descent) already
                # catches and refuses cases like `2**N * 3**(-N)` on the
                # exponent's own print-profile; this Pow's combined
                # contribution is simply unknown, not a hazard by itself.
                result = (0.0, 0.0, False)
            elif value.denominator != 1:
                result = (0.0, 0.0, False)  # genuinely fractional -- no huge-integer RESULT to bound
            else:
                exp_int = int(value)
                b_num, b_den, b_res = _log10_num_den(node.base, memo)
                if exp_int == 0 or not b_res:
                    result = (0.0, 0.0, exp_int == 0)
                elif exp_int > 0:
                    result = (_safe_log10_pow(b_num, exp_int), _safe_log10_pow(b_den, exp_int), True)
                else:
                    result = (_safe_log10_pow(b_den, -exp_int), _safe_log10_pow(b_num, -exp_int), True)
    else:
        result = (0.0, 0.0, False)
    memo[key] = result
    return result


def _ceiling_message_num_den(log_num: float, log_den: float) -> str | None:
    """Refusal text if EITHER side of a `(log_num, log_den)` pair already
    exceeds `MAX_NUMERIC_DIGITS`, or None. The printer renders a Rational's
    numerator and denominator separately, so both sides are checked
    independently — a huge denominator refuses exactly like a huge
    numerator (round 4's fix: `1/(factorial(1463)*factorial(1463))` has a
    NEGLIGIBLE value but an ~8000-digit denominator, and `str()` of that
    denominator alone hits CPython's ceiling regardless of how small the
    printed VALUE is).
    """
    for magnitude, side in ((log_num, "numerator"), (log_den, "denominator")):
        if magnitude <= 0:  # at most one digit on this side, never over cap
            continue
        # `magnitude` itself can be finite yet astronomically large (`2**
        # (1000000**6)`'s numerator has log10 ~ 3e35 -- a REAL, exact
        # answer, not an overflow), so `digit_count` would need dozens of
        # its own digits to spell out. Past this line, describing "how
        # many digits" is no longer useful information, so this reports
        # the same "unbounded" wording `not math.isfinite` uses below
        # rather than interpolating an unreadable number.
        if not math.isfinite(magnitude) or magnitude > 1e15:
            return (f"the result would have an unbounded number of digits in "
                    f"its {side}, over the limit of {MAX_NUMERIC_DIGITS}: it "
                    "cannot be rendered as a decimal string")
        digit_count = int(magnitude) + 1
        if digit_count > MAX_NUMERIC_DIGITS:
            return (f"the result would have about {digit_count} digits in its "
                    f"{side}, over the limit of {MAX_NUMERIC_DIGITS}: it cannot "
                    "be rendered as a decimal string")
    return None


def _numeric_ceiling_scan(tree, memo: dict) -> str | None:
    """Reason some numeric-only subtree of `tree` would print a numerator
    or denominator over `MAX_NUMERIC_DIGITS`, or None. A SEPARATE pass from
    `reject_explosive`'s own `Pow`-focused `_walk` loop below — see that
    function for why the two remain distinct passes.

    #326 (THE-1091), four cross-vendor review rounds: `reject_explosive`
    used to inspect only `Pow` nodes, on the assumption that unbounded
    growth enters exclusively through exponentiation. It does not — see
    `_factor_multiset`'s and `_log10_num_den`'s own docstrings for the
    full history (a flat n-ary product of pre-materialized `Integer`s with
    no `Pow` at all; an ordered accumulator that blows its own budget on
    an intermediate before a later factor cancels it; a value whose
    magnitude is tiny but whose printed DENOMINATOR is enormous; a sibling
    that resists resolution — symbolic, transcendental, a non-integer-
    exponent `Pow` — hiding an already-dangerous numeric partner in the
    SAME flat `Mul`; and `Pow` itself, entirely unvisited by this scan
    until this round, hiding the same dangers one level down).

    THE INVARIANT: no numeric subtree whose printed numerator or
    denominator would exceed `MAX_NUMERIC_DIGITS` is ever evaluated. Every
    `Integer`/`Mul`/`Add`/`Pow` node is checked via `_log10_num_den`, which
    ALWAYS returns a real `(log_num, log_den)` pair (see its own docstring
    for why: a partial answer from an unresolvable sibling is still
    checked, not discarded). Once a node's own accounting is COMPLETE
    (`fully_resolved=True` — every leaf underneath it was a real number,
    not a symbol/Function/irrational), that is the entire, correctly-
    cancelled answer for the WHOLE subtree rooted there, and this scan does
    not descend into its children: a piece of an already-resolved
    combination checked in isolation is exactly the round-2/3 mistake
    (the DENOMINATOR of a cancelling fraction, refused on its own ~8000
    digits even though the fraction it is part of reduces to 1). Only when
    a node's accounting is PARTIAL does the scan need to look inside for a
    smaller — or sibling — numeric hazard `_log10_num_den` could not
    already see from here.

    Iterative (a stack, like `_walk`), not recursive, for the SAME reason
    `_walk` is: a Python recursive descent over the scan's own traversal
    would reintroduce a `RecursionError` risk on a deep tree.
    `_log10_num_den`/`_factor_multiset` are themselves still Python
    recursive descents over one node's OWN subtree (bounded by that
    subtree's depth, memoized so no node is resolved twice) — guarded at
    `reject_explosive`'s call site, and again around the `Pow` loop below,
    which now also calls into this same recursive machinery.
    """
    from sympy import Add, Integer, Mul, Pow

    stack = [tree]
    seen = 0
    while stack:
        node = stack.pop()
        seen += 1
        # Same pathological-tree guard as _walk's own -- the length cap
        # upstream keeps an ordinary input far below this.
        if seen > 20_000:
            return None
        if isinstance(node, tuple):
            # See `_walk`'s own comment on this exact shape: a trailing
            # comma (`"2**1000000^6c6/Me,"`) parses to a bare Python
            # `tuple`, not a SymPy node, and a bare tuple has no `.args` of
            # its own -- `getattr` below would silently stop at its
            # surface and this scan would walk past whatever it wraps.
            stack.extend(node)
            continue
        if isinstance(node, (Integer, Mul, Add, Pow)):
            log_num, log_den, resolved = _log10_num_den(node, memo)
            violation = _ceiling_message_num_den(log_num, log_den)
            if violation:
                return violation
            if resolved:
                # Fully accounted for -- see this function's own docstring
                # for why checking a piece of it independently, below,
                # would be wrong rather than merely redundant.
                continue
            # Partial: the KNOWN part was just checked above and cleared.
            # Fall through to the generic descent so an unresolved sibling
            # (a Function argument, ...) still gets its own chance to
            # reveal a hazard `_log10_num_den` could not see from here —
            # EXCEPT a `Pow` node, which needs its OWN descent rule, not
            # the generic one below: a `Pow` with a plain Integer exponent
            # whose BASE did not resolve (a symbolic base, most commonly)
            # must push ONLY the base, never the exponent. The exponent
            # there is not a value about to be PRINTED — it is a count
            # `reject_explosive`'s own Pow loop uses for a completely
            # different ceiling (`MAX_SYMBOLIC_EXPONENT`, a CPU-cost bound
            # on `sp.expand`, never rendered as a decimal) — and pushing
            # it onto this stack anyway was a real bug caught writing
            # this: `(x+1)**20000!!...` (`factorial2(20000)`, an exact
            # Integer with thousands of digits, used AS AN EXPONENT on a
            # symbolic base) had its bare exponent independently checked
            # here as if it were a print target and refused on "digits in
            # its numerator" instead of correctly falling through to the
            # Pow loop's own, more accurate `MAX_SYMBOLIC_EXPONENT`
            # reason. A `Pow` whose exponent is NOT a plain Integer
            # (compound or itself a tower) still gets both children
            # pushed — `_log10_num_den` never even looked at the base in
            # that case, so both remain to be independently checked
            # (`2**(2**N * 3**(-N))`'s exponent is exactly this shape).
            if isinstance(node, Pow):
                if isinstance(node.exp, Integer):
                    stack.append(node.base)
                else:
                    stack.append(node.base)
                    stack.append(node.exp)
                continue
        args = getattr(node, "args", ())
        if isinstance(args, tuple):
            stack.extend(args)
    return None


def reject_explosive(tree) -> str | None:
    """Reason this PARSED expression must not be evaluated, or None.

    Takes the tree from `parse_expr(..., evaluate=False)`, which is cheap
    (1ms on a power tower) and, crucially, has not done the arithmetic yet.
    This is the half of the bound that the token screen cannot reach: `**` is
    an operator, so its cost is invisible until the operands are known.

    this is a FAST PATH for the power-tower shape, not a complete
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
    either alone as sufficient. See `tests/test_bug_sweep.py`'s block
    for the assertion that the backstop actually holds for this shape.
    """
    from sympy import Integer, Pow

    # #326 (THE-1091): a SEPARATE pass, before the Pow-only walk below,
    # covers a numeric-only Integer/Mul/Add/Pow that the Pow-loop's own
    # cost-focused logic does not try to (round 4 folded Pow INTO this
    # scan too — see `_numeric_ceiling_scan`'s own docstring for why the
    # two remain separate passes rather than one merged loop, and why
    # `Pow` needed to join the scan's trigger set: `("*".join(
    # ["factorial(1463)"] * 117) + "**1")`, `(f*f)**-1`, and `1/(f*f)`
    # each hid an over-cap numeric part ONLY reachable through a Pow node
    # — the old scan skipped Pow entirely, and the Pow-loop below only
    # ever looked past a TRIVIAL exponent (`|exp| <= 1`) without checking
    # what the base itself would print as).
    #
    # `_numeric_ceiling_scan` itself is iterative (an explicit stack, the
    # same reason `_walk` below is) specifically so a deep TREE cannot
    # `RecursionError` its own traversal — but `_log10_num_den`/
    # `_factor_multiset`, which it calls once per numeric-only node, are
    # genuine Python recursive descents over that SAME `evaluate=False`
    # tree (#326 finding 2, cross-vendor review round 3; the Pow loop
    # below now calls the identical machinery for its own symbolic-
    # exponent resolution, so it is wrapped the same way too — finding 4,
    # round 4). Ordinarily this never matters: a tree deep enough to
    # threaten it is, empirically, already deep enough that `parse_expr`
    # itself either failed to build it (caught, separately, by
    # `safe_parse`'s own `try/except` around THAT call) or hit its own
    # parser recursion ceiling well before this function's simpler
    # per-node recursion would — measured directly against a left-nested
    # `"1+" * n + "1"` chain, `parse_expr` starts raising `RecursionError`
    # around n=500 while `reject_explosive` still handles n=480 (the
    # deepest tree `parse_expr` will still build) in well under a
    # millisecond. But "empirically, on this interpreter, for the shapes
    # tried" is not the same claim as "cannot happen" — a different
    # Python recursion-limit setting, or an input shape not tried, is
    # exactly the gap a defensive guard costs nothing to close. Refused
    # the same way `logic.py`'s `_BoolParser` already refuses the
    # identical shape (`"expression too deeply nested to..."`, which
    # `errors._MESSAGE_HINTS`' `"too deeply nested"` entry already maps to
    # `resource_exhausted` — though `reject_explosive`'s own refusals get
    # there via `CATEGORY_CEILING` regardless of message text).
    #
    # One `memo` dict for BOTH passes below: `_log10_num_den` memoizes by
    # `id(node)`, so a node either pass visits (or the Pow loop's own
    # exponent resolution visits again) is resolved once total.
    memo: dict = {}
    try:
        _ceiling_violation = _numeric_ceiling_scan(tree, memo)
    except RecursionError:
        return "expression too deeply nested to evaluate safely"
    if _ceiling_violation:
        return _ceiling_violation

    try:
        for node in _walk(tree):
            if not isinstance(node, Pow):
                continue
            base, exponent = node.base, node.exp

            # A tower: the exponent is itself a power. `9**9**9**9` is four
            # characters of input and an integer with more digits than there
            # are atoms in the observable universe. There is no threshold
            # worth picking here — the shape itself is the problem. (Also
            # exactly the shape `_log10_num_den`'s own Pow branch declines to
            # resolve, deferring to this dedicated check.)
            if isinstance(exponent, Pow):
                return ("a power tower (an exponent that is itself a power) is not "
                        "evaluated: the result grows faster than any useful bound")

            if exponent.free_symbols:
                continue  # symbolic exponent, e.g. x**n — nothing to expand

            if not base.free_symbols:
                # A NUMERIC base with a NUMERIC exponent — the scan above
                # already refused this exact Pow node if its PRINTED result
                # (numerator or denominator) would be over cap, including a
                # compound/cancelling exponent like `f*f/(f*f)` (the scan
                # visited it as its own subtree via `_factor_multiset`'s
                # exact cancellation). Round 4 removed the digit-math that
                # used to live here entirely: the scan now covers every
                # numeric-base Pow case it used to, and several it did not
                # (findings 1-3) — this branch has nothing left to do.
                continue

            # A SYMBOLIC base with a NUMERIC exponent is the one case that
            # IS this loop's own business: not a print-digit ceiling (the
            # result never gets rendered as a decimal — `x**20000` stays
            # symbolic, or gets partially EXPANDED), but a CPU-cost ceiling
            # (`MAX_SYMBOLIC_EXPONENT`; see its own definition above for the
            # measured superlinear cost of `sp.expand`/`sp.factor` on a
            # large symbolic power). Needs the exponent's sign and
            # magnitude — `_factor_multiset` gives both WITHOUT ever
            # materializing the exponent's actual value (#326 finding 3,
            # round 4: the previous version of this branch used
            # `_resolve_numeric_exactly`, which reconstructed via SymPy's
            # own `evaluate=True` `Mul`/`Pow` constructor once a log-space
            # verdict said the FINAL magnitude was small — but
            # `Mul.flatten` computes `coeff *= Pow(b, e)` for each integer
            # power of a numeric base internally, so `2**(2**N * 3**(-N))`,
            # final log negligible, still tried to materialize the literal
            # ~10**298-digit integer `2**N` for a ~300-digit `N` on the way
            # there).
            if isinstance(exponent, Integer):
                exp_int = int(exponent)
                exp_magnitude = _log10_of_int(exp_int) if exp_int else 0.0
                exp_sign = 0 if exp_int == 0 else (1 if exp_int > 0 else -1)
            else:
                multiset = _factor_multiset(exponent, memo.setdefault("__multiset__", {}))
                if multiset is None:
                    continue  # inconclusive -- a Function call, irrational constant, ...
                terms, exp_sign = multiset
                if exp_sign != 0 and any(v < 0 for v in terms.values()):
                    continue  # genuinely fractional -- no huge-integer RESULT to bound
                exp_magnitude, _ = _multiset_log_num_den(terms)

            # abs(), because the danger is the MAGNITUDE — a large negative
            # exponent on a symbolic base is not expanded the same way a
            # large positive one is, so only the positive side is bounded
            # below, matching this loop's behavior since #214.
            if exp_magnitude <= 0:  # |value| <= 1 (or exactly 0) -- trivial
                continue
            if exp_sign > 0 and exp_magnitude > math.log10(MAX_SYMBOLIC_EXPONENT):
                # NEVER interpolate the exponent's own value: report the
                # (cheap, bounded) digit count instead — see
                # `_approx_decimal_digits`'s own docstring for why
                # interpolating a huge exponent directly can crash the
                # refusal MESSAGE before it is ever returned.
                return (f"a symbolic power with an exponent of about "
                        f"{int(exp_magnitude) + 1} digits exceeds the limit of "
                        f"{MAX_SYMBOLIC_EXPONENT}: expanding it is superlinear "
                        "in the exponent")
    except RecursionError:
        return "expression too deeply nested to evaluate safely"
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
        # `parse_expr(expr, evaluate=False)` hands back a plain Python
        # `tuple` — not a SymPy node — when `expr` ends in a trailing comma:
        # `"2**1000000^6c6/Me,"` is valid Python tuple syntax (`x,` means
        # `(x,)`), so it parses successfully as `(Mul(...),)` instead of
        # raising the syntax error its later garbage (`c6`, the unclosed
        # `Me`, ...) suggests it should. A bare `tuple` has no `.args` of
        # its own, so `getattr` below fell through to the `()` default and
        # the walk stopped at the tuple's surface without ever looking
        # inside — silently skipping whatever Pow nodes it wrapped, no
        # matter how explosive. Measured: this is what let
        # `"2**1000000^6c6/Me,"` reach the SECOND, real (`evaluate=True`)
        # parse in `safe_parse` with no ceiling ever applied — a 1.5+ GB
        # tracemalloc peak computing `2**(1000000**6)` before the trailing
        # comma's syntax error ever surfaced. Extend directly into a bare
        # tuple's own elements rather than relying on `.args` to find them.
        if isinstance(current, tuple):
            stack.extend(current)
            continue
        args = getattr(current, "args", ())
        # Found by scripts/fuzz.py: `parse_expr("binomial", ...)`
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
