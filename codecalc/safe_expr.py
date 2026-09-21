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


def _log10_magnitude(node, memo: dict) -> float | None:
    """log10(|value|) of a numeric-only subtree, or None if inconclusive
    (free symbols, a `Function` call, an irrational `NumberSymbol`, or
    float-mode overflow — NOT evidence of anything, same convention as
    `_bounded_numeric_value`). Memoized per node (keyed by `id`, not
    structural equality — cheap and correct for a tree that is never
    mutated during one `reject_explosive` call) so a node shared by
    several ancestors is resolved once, not once per ancestor.

    Two bugs, found by cross-vendor review of the first version of this
    fix (#326/THE-1091), both traced to the SAME root cause: the first
    version delegated to `_bounded_numeric_value`, which accumulates an
    EXACT Rational product/sum step by step and checks the INTERMEDIATE
    accumulator's bit length against a budget after every step.

      1. ORDER-DEPENDENT false refusal. `factorial(1463) * factorial(1463)
         / (factorial(1463) * factorial(1463))` is exactly 1 — the two
         factors cancel — but `evaluate=False` parses it as `Mul(f, f,
         Pow(Mul(f, f), -1))`: the accumulator multiplies the first two `f`
         factors together (~8000 digits) and exceeds `_SUBTREE_BIT_BUDGET`
         BEFORE the reciprocal third factor is ever multiplied in and
         cancels it back down to 1. Refused as `resource_exhausted`, wrong.
      2. SUPERLINEAR walk. `reject_explosive`'s loop calls the ceiling
         check on every `Integer`/`Mul`/`Add` node `_walk` visits, and the
         old check re-resolved its ENTIRE subtree from scratch every time —
         so a node at depth d got re-resolved once for itself and again for
         every numeric ancestor above it. An alternating Add/Mul chain of
         depth 250 measured 0.311s in `reject_explosive` alone (vs ~0.0002s
         on main), and the growth was clearly superlinear (0.009s at depth
         50, not ~5x that at depth 250).

    This function fixes both by construction, not by tuning the old one's
    budget. Logarithms turn a product into a SUM and a reciprocal into a
    SUBTRACTION: `f * f / (f * f)` becomes `log(f) + log(f) - log(f) -
    log(f)`, which is exactly `0.0` in IEEE float — not an approximation,
    because it is the identical float value added then subtracted twice, so
    there is no intermediate to blow a budget on in the first place, and no
    dependence on which order the Mul's args happen to be visited in. And
    because every node's result is cached by `id(node)` in the caller-owned
    `memo` dict, an ancestor's magnitude is a handful of dict lookups plus
    O(number of direct children) float additions, never a re-walk of the
    whole subtree — the walk becomes bottom-up and single-pass overall,
    each node resolved exactly once regardless of how many ancestors ask.

    The trade against `_bounded_numeric_value` (still used, UNCHANGED, by
    the `Pow` branch below for a base/exponent `evaluate=False` left
    un-folded — that machinery is proven and this function does not touch
    it): this is magnitude-only, never an exact value, so a refusal message
    always says "about N digits" (matching the `Pow` branch's own numeric-
    base wording) rather than sometimes reporting an exact count. Good
    enough here: nothing downstream of this function's caller needs the
    exact value, only whether to refuse.

    `Add`'s case is the one place this is a DELIBERATE OVER-approximation
    rather than exact: log does not distribute over addition, so this uses
    the safe bound `|sum(x_i)| <= sum(|x_i|) <= max(|x_i|) * len(args)`,
    i.e. `log10|sum| <= max(log10|x_i|) + log10(len(args))`. That can
    over-refuse an Add that happens to cancel (`huge - huge + 1`, the
    Add-shaped analogue of bug 1 above) — never a false NEGATIVE, only a
    conceivably-too-cautious refusal — and is not reachable from a
    legitimate input in the first place: summing bounded-size heavy-call
    results grows the digit count by at most `log10(term count)`, and the
    2000-char expression cap admits at most roughly 125 heavy-call terms,
    nowhere near enough addition alone to cross `MAX_NUMERIC_DIGITS` from
    UNDER the cap — see `reject_explosive`'s own test coverage for the
    measured numbers. A future caller relying on Add being exact here
    should not assume it.
    """
    key = id(node)
    if key in memo:
        return memo[key]
    from sympy import Add, Float, Integer, Mul, Pow, Rational

    result: float | None
    if isinstance(node, Integer):
        n = int(node)
        result = _log10_of_int(n) if n else float("-inf")
    elif isinstance(node, Rational):  # Integer is also a Rational; caught above
        result = (_log10_of_int(node.p) - _log10_of_int(node.q)) if node.p else float("-inf")
    elif isinstance(node, Float):
        try:
            v = abs(float(node))
        except (OverflowError, ValueError):
            result = None
        else:
            result = math.log10(v) if v else float("-inf")
    elif isinstance(node, Mul):
        parts = [_log10_magnitude(arg, memo) for arg in node.args]
        if any(p is None for p in parts):
            result = None
        else:
            total = math.fsum(parts)
            # `inf + (-inf)` is `nan` in IEEE float -- reachable if this Mul
            # combines two DIFFERENT astronomically-extreme Pow factors (see
            # the Pow branch's own `_safe_log10_pow` for where +-inf comes
            # from at all). `nan > 0` is always False, so an unguarded nan
            # would silently read as "not over cap" -- the wrong direction
            # for two colliding extreme magnitudes we have no real evidence
            # about. Treat it as the maximally cautious answer instead.
            result = float("inf") if math.isnan(total) else total
    elif isinstance(node, Add):
        parts = [_log10_magnitude(arg, memo) for arg in node.args]
        if any(p is None for p in parts):
            result = None
        else:
            finite = [p for p in parts if p != float("-inf")]
            result = (max(finite) + math.log10(len(node.args))) if finite else float("-inf")
    elif isinstance(node, Pow) and isinstance(node.exp, Integer):
        # Only a plain Integer exponent -- a Mul/Rational-shaped one
        # (`evaluate=False`'s `Pow(base, Mul(30000, Pow(2,-1)))` for a
        # fractional exponent that happens to reduce to an integer) is left
        # to the `Pow` branch below, which already resolves that case via
        # `_bounded_numeric_value`; this function returning None for it is
        # inconclusive, not a regression -- the outer Mul/Add this Pow sits
        # in simply does not get refused by THIS check, and the existing
        # Pow-only logic still inspects the Pow node directly regardless.
        exp_int = int(node.exp)
        if exp_int == 0:
            result = 0.0
        else:
            base_mag = _log10_magnitude(node.base, memo)
            result = None if base_mag is None else _safe_log10_pow(base_mag, exp_int)
    else:
        result = None
    memo[key] = result
    return result


def _resolve_numeric_exactly(node, memo: dict):
    """`_bounded_numeric_value`'s own contract (an exact `Rational`/
    `Integer`/`float`, `None` if inconclusive, or `_NumericTooLarge` if too
    large to bother with) but order-independent: `_log10_magnitude` — the
    same order-independent log-space arithmetic `_numeric_ceiling_scan`
    already trusts — decides the SIZE verdict FIRST, and an exact value is
    only reconstructed once that verdict says doing so is safe (cheap).

    #326 finding 3 (cross-vendor review, round 3): `reject_explosive`'s Pow
    branch below calls this (replacing a direct call to
    `_bounded_numeric_value`) for a Mul/Rational-shaped base or exponent
    `evaluate=False` left un-folded (`Pow(base, Mul(30000, Pow(2, -1)))`
    for a fractional-looking exponent, say) — and `_bounded_numeric_value`
    inherits the SAME order-dependent accumulator bug round 2 fixed at the
    top level: `2**(f*f/(f*f))` (an exponent that cancels to exactly 1) and
    `(f*f/(f*f))**2` (a base that cancels to exactly 1) both false-refused,
    for the identical reason — the accumulator multiplied the numerator's
    two `f` factors together BEFORE the cancelling reciprocal factor was
    ever multiplied in.

    Reconstructing the exact value — `type(node)(*node.args)`, SymPy's
    ORDINARY evaluate=True constructor, which reduces a Rational via GCD
    the normal way, not through this module's own incremental bit-budget
    check — is only attempted once `_log10_magnitude` has already shown
    the true magnitude is small enough to be worth materializing (reusing
    `_SUBTREE_BIT_BUDGET`'s own "generous but still cheap" line, expressed
    in decimal digits as `_SAFE_RECONSTRUCT_DIGITS`). A magnitude beyond
    that — or one `_log10_magnitude` could not determine at all — is
    treated exactly like `_bounded_numeric_value`'s own two failure modes
    (`_NumericTooLarge` / `None` respectively), without ever attempting to
    materialize a value that large.
    """
    from sympy import Float, Integer, Rational

    magnitude = _log10_magnitude(node, memo)
    if magnitude is None:
        return None
    if not math.isfinite(magnitude) or magnitude > _SAFE_RECONSTRUCT_DIGITS:
        return _NumericTooLarge(_SUBTREE_BIT_BUDGET)
    args = getattr(node, "args", ())
    if not args:
        # A leaf `_log10_magnitude` resolved (an Integer/Rational/Float
        # would already have been a bare Integer/Float upstream and never
        # reached this function at all -- see the Pow branch's own
        # `isinstance(exponent, (Integer, Float))` gate) but that has no
        # `.args` to reconstruct from, e.g. a NumberSymbol with a resolved
        # numeric magnitude by some path this function has no case for.
        return None
    try:
        exact = type(node)(*args)
    except Exception:
        # Reconstruction is not expected to fail for anything
        # `_log10_magnitude` already proved numeric -- but this function's
        # whole contract is "fall through when uncertain", so a defect in
        # that expectation degrades to inconclusive rather than raising.
        return None
    if isinstance(exact, Float):
        try:
            return float(exact)
        except (OverflowError, ValueError):
            return None
    if isinstance(exact, (Integer, Rational)):
        return exact
    return None  # reconstruction did not land on a plain number -- inconclusive


def _ceiling_message(magnitude: float) -> str | None:
    """Refusal text for a resolved log10 magnitude already over
    `MAX_NUMERIC_DIGITS`, or None. Shared by `_numeric_ceiling_scan`'s
    full-node check and `_partial_ceiling_violation`'s partial one, so the
    "±inf/nan-safe, digit-count-plus-one" logic exists in exactly one
    place rather than twice with a chance to drift.
    """
    if magnitude <= 0:  # at most one digit, never over cap
        return None
    if not math.isfinite(magnitude):
        # `_safe_log10_pow`'s own +-inf shortcut for an astronomically huge
        # exponent, or (rarer) two such Pow factors colliding into `nan`
        # inside a Mul -- `int(magnitude)` below would raise OverflowError
        # on the former and give a wrong answer on the latter (nan compares
        # False to everything, but is caught upstream by `_log10_magnitude`
        # 's own nan-to-inf fallback before it ever reaches here).
        return ("the result would have an unbounded number of digits, over "
                f"the limit of {MAX_NUMERIC_DIGITS}: it cannot be rendered as "
                "a decimal string")
    digit_count = int(magnitude) + 1
    if digit_count > MAX_NUMERIC_DIGITS:
        return (f"the result would have about {digit_count} digits, over the "
                f"limit of {MAX_NUMERIC_DIGITS}: it cannot be rendered as a "
                "decimal string")
    return None


def _partial_ceiling_violation(node, memo: dict) -> str | None:
    """Refusal text if the RESOLVABLE part of a `Mul`/`Add` node ALONE
    already exceeds `MAX_NUMERIC_DIGITS`, ignoring any sibling
    `_log10_magnitude` cannot resolve, or None.

    #326 finding 1 (cross-vendor review, round 3): `_log10_magnitude` on a
    `Mul`/`Add` returns `None` the moment ANY child is inconclusive — a
    free symbol, a `Function` call, an irrational `NumberSymbol`, a
    non-integer-exponent `Pow` — because the TRUE combined magnitude
    genuinely cannot be known without evaluating that child. But SymPy
    FLATTENS a chain of the same operator into one n-ary node: `x *
    factorial(1463) * factorial(1463) * ...` (117 of them) is ONE `Mul`
    with 118 args, not a `Mul` wrapping a smaller, fully-numeric `Mul` —
    there is no nested numeric island for `_numeric_ceiling_scan`'s
    fallback descent to find. Prefixing the original 117-factorial bomb
    with `x*`, `cos(0)*`, `pi*`, `E*`, or `2**(1/2)*` (a non-integer-
    exponent `Pow`, which `_log10_magnitude`'s own `Pow` branch does not
    handle) all kept it 1990 characters or fewer, under the 2000-char cap,
    and every `factorial(1463)` sibling stayed individually under 4000
    digits — so the scan fell through to the generic descent and checked
    each `Integer` sibling ALONE, finding nothing, while `simplify_
    expression` still materialized the same ~467,766-digit product before
    `str()`'ing it.

    A symbolic factor cannot make an already-over-cap numeric PART smaller
    in any way the printer would rescue — `x` stays `x` (never resolves to
    a magnitude-shrinking value here; SymPy still combines the OTHER,
    purely numeric siblings into one coefficient during `simplify`/
    `expand`/`factor`), and a `Function`/irrational sibling this module
    cannot evaluate is not evidence its magnitude is small either. So the
    right rule is not "the whole node is inconclusive, therefore safe" —
    it is "the part that DOES resolve is refused on its own if it is
    already too large, independent of what the rest might be." Called
    only after `_numeric_ceiling_scan` has already found the FULL node's
    own magnitude to be `None` — computing this unconditionally for every
    Mul/Add would repeat that same `None` check for nothing.
    """
    from sympy import Mul

    parts = [_log10_magnitude(arg, memo) for arg in node.args]
    resolved = [p for p in parts if p is not None]
    if not resolved:
        return None
    if isinstance(node, Mul):
        magnitude = math.fsum(resolved)
        if math.isnan(magnitude):  # see _log10_magnitude's own Mul branch
            magnitude = float("inf")
    else:  # Add -- the only other type this function is ever called with
        finite = [p for p in resolved if p != float("-inf")]
        magnitude = (max(finite) + math.log10(len(resolved))) if finite else float("-inf")
    return _ceiling_message(magnitude)


def _numeric_ceiling_scan(tree, memo: dict) -> str | None:
    """Reason some numeric-only (no free symbols) `Integer`/`Mul`/`Add`
    subtree of `tree` already exceeds `MAX_NUMERIC_DIGITS`, or None. A
    SEPARATE pass from `reject_explosive`'s own `Pow`-focused `_walk` loop
    below — see that function for why the two are not merged into one.

    #326 (THE-1091): `reject_explosive` used to inspect only `Pow` nodes, on
    the assumption that unbounded growth enters exclusively through
    exponentiation. `"*".join(["factorial(1463)"] * 117)` disproved that.
    Each `factorial(1463)` is individually legal — `MAX_HEAVY_ARG` admits an
    argument of 1463, and the result is 3998 digits, under
    `MAX_NUMERIC_DIGITS` — and it materializes to a plain `Integer` during
    PARSING itself (see the `_HEAVY_FUNCTIONS` comment above: a function
    call on a literal argument evaluates at parse time regardless of
    `evaluate=False`). `Mul(Integer, Integer, ..., 117 of them)` contains no
    `Pow` at all, so the old loop walked straight past a 467,766-digit
    product and let CPython's own int->str ceiling (4300 digits by default)
    raise from deep inside SymPy's printer — an uncaught `ValueError`, not a
    refusal.

    TWO FOLLOW-ON BUGS, both found by cross-vendor review of the first
    version of this fix, drove this shape specifically:

      1. Checking every Mul/Add node the walk happened to visit,
         independently, is WRONG once a node can be part of a larger
         numeric combination: `factorial(1463) * factorial(1463) /
         (factorial(1463) * factorial(1463))` is exactly 1 (the ORIGINAL
         report, via `_bounded_numeric_value`'s order-dependent
         accumulation — see `_log10_magnitude`'s docstring), but even
         after fixing the magnitude computation to be order-independent,
         the OLD per-node walk still separately visited the DENOMINATOR's
         own `Mul(f, f)` (the base of the `Pow(..., -1)` reciprocal) and
         refused IT on its own ~7996-digit magnitude — correct if that
         `Mul` were going to be printed standalone (a lone `1/(f*f)`, with
         nothing to cancel it, genuinely cannot be rendered: `str()` on
         an 8000-digit DENOMINATOR hits the exact same CPython ceiling),
         but wrong here, where an ENCLOSING `Mul` already accounts for it
         and reduces to 1.
      2. The same per-node-independent design is where the superlinear
         walk (finding 3 below `reject_explosive`) came from: every
         numeric ancestor of a node re-triggered a check of that node.

    The fix for both: once a node's OWN magnitude is known (not `None` —
    i.e. it is fully numeric, `_log10_magnitude` did not bottom out on a
    free symbol, a `Function` call, or float-mode overflow anywhere inside
    it), that ONE number is the complete, correctly-cancelled answer for
    the ENTIRE subtree rooted there — `_log10_magnitude` already resolved
    every descendant to get it. So this scan does NOT descend into a
    node's children once its magnitude resolves: whatever is inside has
    already been accounted for, and looking at a piece of it in isolation
    (the denominator bug above) is exactly the mistake to avoid. Only when
    a node's magnitude is `None` (mixed with a free symbol, or otherwise
    inconclusive) does the scan need to look inside FOR A SMALLER numeric
    island that might still be dangerous on its own (`x +
    "*".join(["factorial(1463)"] * 117)`, say).

    Iterative (a stack, like `_walk`), not recursive: a Python recursive
    descent here would reintroduce the exact `RecursionError` risk
    `_walk`'s own docstring already rules out for deep trees. Cheap by
    construction rather than by tuning: a fully-numeric tree (the common
    dangerous case) resolves in ONE `_log10_magnitude` call at the ROOT and
    the scan's own stack never grows past that; a symbolic tree with no
    numeric island anywhere pushes every node once and finds nothing,
    matching the old loop's own baseline cost.
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
        if isinstance(node, Pow):
            # Entirely the Pow-focused loop's domain (below — it visits
            # every node in the tree via `_walk` regardless of what this
            # scan chooses to skip, so nothing is lost by deferring). This
            # scan's job is the coverage gap where NO Pow node exists at
            # all; reaching into a Pow's own base/exponent here duplicates
            # that loop's job with DIFFERENT, less specific wording, and
            # for a SYMBOLIC-base Pow with a huge numeric exponent, this
            # scan would treat the bare exponent Integer as if it were a
            # value about to be PRINTED (refusing it on `MAX_NUMERIC_
            # DIGITS` grounds) when the loop below already has a dedicated,
            # more accurate reason for that exact shape (`MAX_SYMBOLIC_
            # EXPONENT` — expanding a symbolic power is superlinear in the
            # exponent, a cost bound, not a print-ceiling one). Confirmed
            # by `tests/test_bug_sweep.py`'s `_EXPLOSIVE_CRASH_INPUTS[1]`
            # (`(x+1)**20000!!...`), which this scan used to intercept
            # with the wrong reason before this fix.
            #
            # `_log10_magnitude` (used above and below by an ENCLOSING
            # Mul/Add) still resolves straight through a Pow when it needs
            # to — e.g. `factorial(1463) * factorial(1463) * 2**50`'s
            # combined magnitude correctly includes the `2**50` factor.
            # This skip only affects where THIS scan independently
            # descends and checks IN ISOLATION, not what a parent's own
            # magnitude computation is allowed to look through.
            continue
        if isinstance(node, (Integer, Mul, Add)):
            magnitude = _log10_magnitude(node, memo)
            if magnitude is not None:
                # Fully numeric: already the complete, cancellation-correct
                # answer for this whole subtree. Do not push its children —
                # checking a piece of an already-resolved combination
                # independently is finding 1 above.
                violation = _ceiling_message(magnitude)
                if violation:
                    return violation
                continue
            # magnitude is None: mixed with a free symbol, or otherwise
            # inconclusive as a WHOLE -- but the part that DOES resolve
            # might already be dangerous on its own (finding 1, round 3):
            # SymPy flattens a chain of the same operator into one n-ary
            # node, so there is no nested numeric-only island to find by
            # descending when the danger is a sibling AWAY, not a level
            # down. Checked before falling through to the generic descent
            # below (which still runs regardless — an inconclusive sibling
            # like `cos(...)` or a nested Function argument can itself
            # hide a separate numeric bomb one level further in, and this
            # partial check only looks at `node`'s OWN direct args).
            if isinstance(node, (Mul, Add)):
                violation = _partial_ceiling_violation(node, memo)
                if violation:
                    return violation
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
    from sympy import Float, Integer, Pow

    # #326 (THE-1091): a SEPARATE pass, before the Pow-only walk below,
    # covers a numeric-only Mul/Add/Integer that the Pow-only loop cannot
    # see at all — see `_numeric_ceiling_scan`'s own docstring for the
    # `factorial(1463)` product this closes, and for why it is a genuinely
    # separate pass rather than folded into the loop below: that loop's
    # `_walk` visits every node unconditionally (needed for Pow, which can
    # appear anywhere), while the scan must NOT independently re-examine a
    # node once an ancestor has already resolved it — the two have
    # different, incompatible descent rules, not just different node-type
    # filters.
    # `_numeric_ceiling_scan` itself is iterative (an explicit stack, the
    # same reason `_walk` below is) specifically so a deep TREE cannot
    # `RecursionError` its own traversal -- but `_log10_magnitude`, which
    # it calls once per numeric-only node, is a genuine Python recursive
    # descent over that SAME `evaluate=False` tree (#326 finding 2,
    # cross-vendor review round 3). Ordinarily this never matters: a tree
    # deep enough to threaten it is, empirically, already deep enough that
    # `parse_expr` itself either failed to build it (caught, separately,
    # by `safe_parse`'s own `try/except` around THAT call) or hit its own
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
    try:
        # Shared with the Pow loop below (#326 finding 3, round 3): a Pow's
        # base/exponent is now also resolved through `_log10_magnitude`,
        # and reusing this dict means a node visited by both passes (or by
        # the scan and then again through a Pow's own base/exponent) is
        # only ever resolved once.
        memo: dict = {}
        _ceiling_violation = _numeric_ceiling_scan(tree, memo)
    except RecursionError:
        return "expression too deeply nested to evaluate safely"
    if _ceiling_violation:
        return _ceiling_violation

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
            if exponent.free_symbols:
                continue  # symbolic exponent, e.g. x**n — nothing to expand
            # A NUMERIC exponent that is not already a bare Integer/Float —
            # `evaluate=False` leaves `30000/2` as `Mul(30000, Pow(2, -1))`
            # rather than folding it to a plain Integer, even though its
            # VALUE is the ordinary integer 15000. Resolve it through
            # `_resolve_numeric_exactly` (order-independent, via
            # `_log10_magnitude` — #326 finding 3, round 3: a bare
            # `_bounded_numeric_value` call here inherited its ORDER-
            # DEPENDENT accumulator bug, so `2**(f*f/(f*f))`, an exponent
            # that cancels to exactly 1, used to false-refuse) rather than
            # `int(exponent)` directly, which — unbounded — is the exact
            # mistake this module exists to avoid if the subtree turns out
            # to hide something enormous.
            resolved_exp = _resolve_numeric_exactly(exponent, memo)
            if resolved_exp is None:
                # INCONCLUSIVE, not proof of anything — a Function call
                # (`2**cos(0)`, the true value is 2), an irrational
                # NumberSymbol, or float-mode precision loss. Refusing here
                # would be worse than doing nothing: fall through to
                # whatever the existing downstream screen already does
                # (guarded_call's CPU/wall backstop, and each caller's own
                # resource-error catch), exactly as main's behavior for any
                # exponent shape this function has no case for.
                continue
            if isinstance(resolved_exp, _NumericTooLarge):
                # The OPPOSITE case: PROVED too large by exact bit-length
                # arithmetic (e.g. `2**(2**20000 * 3**20000)`, factors that
                # do not cancel) — real evidence, refuse.
                return (f"the exponent is a numeric expression whose magnitude "
                        f"already exceeds the limit of {MAX_NUMERIC_DIGITS} "
                        "digits: it cannot be evaluated safely")
            if isinstance(resolved_exp, float):
                if not resolved_exp.is_integer():
                    continue  # a genuinely fractional exponent — no huge-integer result to bound
                exponent = Integer(int(resolved_exp))
            else:
                if resolved_exp.q != 1:
                    continue  # a genuinely fractional exponent — no huge-integer result to bound
                exponent = resolved_exp
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
                # NEVER interpolate exp_value itself: `2**100000!!` makes it
                # `factorial2(100000)`, an exact Integer with thousands of
                # digits, and formatting that directly hits Python's int->str
                # conversion limit (4300 digits) — the refusal MESSAGE would
                # raise before it could even report the refusal. Report the
                # (cheap, bounded) digit count instead of the value.
                return (f"a symbolic power with an exponent of about "
                        f"{_approx_decimal_digits(exp_value)} digits exceeds "
                        f"the limit of {MAX_SYMBOLIC_EXPONENT}: expanding it "
                        "is superlinear in the exponent")
        else:
            # Cheap to compute, impossible to print. Estimated by digit count
            # rather than by computing it, which would be the very thing this
            # is here to avoid.
            #
            # `log10_magnitude` is computed via `_log10_of_int` for an
            # `Integer` base rather than the previous `abs(float(base))`:
            # for a base with hundreds of digits, `float(base)` either
            # raises `OverflowError` (as Python's own `int` would) or — the
            # crash this fixes — returns `inf` WITHOUT raising, because
            # SymPy's `Integer.__float__` differs from `int.__float__` here.
            # `_log10_of_int` gets to the same log10(magnitude) with no float
            # conversion of the (potentially huge) base at all. A `Float`
            # base is already double-precision internally, so converting it
            # is the cheap, ordinary case.
            try:
                if isinstance(base, Integer):
                    log10_magnitude = _log10_of_int(int(base))
                elif isinstance(base, Float):
                    magnitude = abs(float(base))
                    log10_magnitude = (math.log10(magnitude) if magnitude
                                        else float("-inf"))
                else:
                    # A NUMERIC base that is not already a bare Integer/
                    # Float — `evaluate=False` leaves `3/2` as `Mul(3,
                    # Pow(2, -1))`, bypassing the isinstance checks above
                    # even though its value is the ordinary Rational 3/2.
                    # `_log10_magnitude` directly (order-independent —
                    # #326 finding 3, round 3: the old `_bounded_numeric_
                    # value` call here inherited its ORDER-DEPENDENT
                    # accumulator bug, so `(f*f/(f*f))**2`, a base that
                    # cancels to exactly 1, used to false-refuse), not
                    # `_resolve_numeric_exactly`: this branch only ever
                    # needs a MAGNITUDE, never an exact value the way the
                    # exponent branch above does (to test integer-ness), so
                    # there is no reason to pay for reconstructing one.
                    log10_magnitude = _log10_magnitude(base, memo)
                    if log10_magnitude is None:
                        # Inconclusive (a Function call, an irrational
                        # constant, float-mode overflow, ...) — no ceiling
                        # check for THIS node; fall through the same way an
                        # unresolvable exponent does above.
                        continue
            except (TypeError, ValueError, OverflowError):
                continue
            if log10_magnitude > 0:  # equivalent to magnitude > 1
                # abs() here too. A negative exponent produces a rational
                # whose DENOMINATOR has that many digits, which costs the same
                # to build; without it `digits` came out negative and every
                # negative exponent compared under the cap.
                #
                # `exp_value` itself can be an enormous Integer (again,
                # `2**100000!!` — the exponent, not the base, is the huge one
                # this time): `abs(exp_value) * log10_magnitude` is int-times-
                # float, and Python resolves that by converting the int to
                # float FIRST — which is the original `OverflowError` this
                # fixes if `exp_value` doesn't fit a double. `bit_length()`
                # again sidesteps ever attempting that conversion: past ~1000
                # bits the result is already astronomically over
                # `MAX_NUMERIC_DIGITS` for any `log10_magnitude > 0`, so
                # there is nothing the exact product could add.
                abs_exp = abs(exp_value)
                if abs_exp.bit_length() > 1000:
                    over_cap, digit_desc = True, "an unbounded number of"
                else:
                    digits = abs_exp * log10_magnitude
                    if math.isfinite(digits):
                        # `digits` IS log10(result), not the digit count: a
                        # value with N digits has log10 in [N-1, N), so the
                        # true count is floor(digits) + 1. Comparing `digits`
                        # itself against the cap (the previous code) admits
                        # EQUALITY at the boundary — `10**4000` has 4001
                        # digits but log10(10**4000) == 4000 exactly, which
                        # is not > MAX_NUMERIC_DIGITS(4000), so it slipped
                        # through as "allowed" one digit over the stated cap.
                        digit_count = int(digits) + 1
                        over_cap = digit_count > MAX_NUMERIC_DIGITS
                        digit_desc = f"about {digit_count}"
                    else:
                        over_cap, digit_desc = True, "an unbounded number of"
                if over_cap:
                    return (f"the result would have {digit_desc} digits, "
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
