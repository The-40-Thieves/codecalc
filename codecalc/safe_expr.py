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
import re
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
    # #326 finding 2 (cross-vendor review, round 6, Codex): audited every
    # eager combinatorial/number-theoretic name `safe_global_dict()`
    # actually exposes (sympy 1.14's `functions.combinatorial.numbers`,
    # plus the top-level namespace) for one that materializes a growing
    # `Integer`/`Rational` DURING PARSING for a literal argument — the
    # exact hazard the rest of this set already exists to cap, just
    # missing these five names. Measured at the SAME `MAX_HEAVY_ARG`
    # (1463) cap the rest of this set uses (all comfortably under
    # `MAX_NUMERIC_DIGITS`, and — like every other member — some
    # individual calls near the cap are still slow, bounded only by
    # `guarded_call`'s CPU backstop, same as `binomial(200_000, 100_000)`
    # already was before this fix):
    #
    #     bell(1463)       3_018 digits   (bell(1463) itself ~5s to PARSE --
    #                                      slow but bounded, not unbounded)
    #     genocchi(1462)   3_269 digits   (genocchi(1463) is a degenerate
    #                                      1-digit odd-index value; 1462,
    #                                      the nearest even index, is the
    #                                      real worst case near the cap --
    #                                      same odd/even-index asymmetry as
    #                                      the ALREADY-capped `euler`)
    #     motzkin(1463)      693 digits
    #     andre(1463)      3_711 digits
    #     partition(1463)     39 digits   (the sympy>=1.13 replacement
    #                                      name for the already-capped
    #                                      `npartitions`, deprecated but
    #                                      still reachable — kept alongside
    #                                      it, not instead of it)
    "bell", "genocchi", "motzkin", "andre", "partition",
    # #326 finding 4 (cross-vendor review, round 7, Codex): `digamma`/`zeta`
    # of an integer belong to the SAME hazard shape as the rest of this
    # set (a small ARGUMENT growing an expensive OUTPUT, not the "hazard
    # scales with the size of the argument itself" shape the digit-cap
    # entries in `_FUNCTION_ARG_CAPS` below cover) — `digamma(1463)` is
    # 0.055s, `zeta(-1463)` 0.008s (both comfortably under the cap this
    # module's other members already accept slow-near-the-cap for), but
    # `digamma(10000)` raises CPython's own int->str conversion limit from
    # INSIDE the computation itself (not merely while printing the
    # result), well past anything `guarded_call`'s backstop should have to
    # catch instead of this cap. `zeta` takes negative arguments in normal
    # use (`zeta(-1463)` above) — the existing token check already handles
    # that correctly (a leading `-` in the source is its own `OP` token,
    # never part of the `NUMBER` token this set's cap compares against, so
    # sign never evades the cap).
    "digamma", "zeta",
    # THE-1095 round 3 (verify-1095-r2, both reviewers): `polygamma` itself
    # (not just `digamma`, which is `polygamma(0, z)` under the hood) is
    # directly callable by name, and its own hazard is the SAME shape as
    # `digamma`'s, at ARGUMENT POSITION 1 (`z`; position 0 is the order,
    # always small in practice) -- `polygamma(0, 1000**1000)` hangs walking
    # `harmonic(z-1)`'s recurrence. Added here so it gets the deferred
    # stand-in, the nested-call token check, and (via `_EXTRA_BOUNDED_
    # POSITIONS` below) its own position-1 bound; position 0 (order) is
    # ALSO nominally capped at `MAX_HEAVY_ARG` through this set's normal
    # position-0 machinery -- harmless, since a real order argument is
    # always a tiny integer (0, 1, 2, ...), never realistically near 1463.
    "polygamma",
})

#: #326 finding 4 (cross-vendor review, round 7, Codex): a full audit of
#: `safe_global_dict()` (sympy 1.14's namespace) for every remaining eager
#: number-theoretic/arithmetic name not already in `_HEAVY_FUNCTIONS`
#: turned up two MORE hazard shapes needing their own cap, plus one name
#: (`stirling`) that turned out to be a non-issue: `hasattr(sympy,
#: "stirling")` is `False` — the audit's own hypothesis was that it might
#: be an omission, but sympy 1.14 does not expose it under that name at
#: all, so `safe_global_dict()` cannot reach it and there is nothing here
#: to bound.
#:
#: `factorint`, `primefactors`, `divisors`, `mobius`, `nextprime`,
#: `isprime` are a fundamentally different shape from `_HEAVY_FUNCTIONS`
#: above: the hazard there is a small COUNT-like argument growing an
#: expensive OUTPUT (`factorial(1463)` -> a big but bounded integer); the
#: hazard here is that the argument IS the number under scrutiny, and
#: testing/factoring an ARBITRARY large number has no bound this module
#: can derive from the literal's magnitude the way it can for a "count" —
#: `MAX_HEAVY_ARG` (1463) would be far too tight (legitimate use wants to
#: factor or primality-test numbers much larger than that), but there is
#: no value cap that is both generous and safe, because hardness depends
#: on the number's FACTORS, not its size, and those are exactly what is
#: unknown without doing the work this cap exists to avoid. A random
#: ~40-digit semiprime measured 27.8s to factor (a 25-digit one, 0.3s;
#: growth is unpredictable with digit count, not merely slow) — RSA-100
#: (100 digits) hits the process memory/CPU backstop outright.
#: `mobius(n)` needs to know whether `n` is squarefree, which needs `n`'s
#: factorization, so it shares the family even though it is not framed as
#: "factor this" on its face. `isprime`/`nextprime` measured cheap at the
#: sizes tried here (nextprime, the more expensive of the two: 1.25s at
#: 300 digits, 4.9s at 800), but neither has a hardness bound derivable
#: from digit count alone either (`nextprime` walks a range of candidate
#: primes past `n`, and prime gaps are not uniformly bounded), so all six
#: get ONE shared, conservative cap tuned to the hardest member
#: (factoring), not the cheapest.
#:
#: Five of these six (`mobius` excepted — see below) are plain Python
#: callables, not `sympy.Function` subclasses, and each coerces its
#: argument to a concrete int internally (`int()`/`sympify()`) regardless
#: of `evaluate=False` — confirmed live: `parse_expr("factorint(1000*2)",
#: evaluate=False)` still returns the already-computed factorization
#: `dict`, even though `Mul(1000, 2, evaluate=False)` on its own stays a
#: symbolic, unevaluated `Mul` (`factorial`, a real `sympy.Function`
#: subclass, respects `evaluate=False` on the identical shape:
#: `factorial(1000*2)` stays `factorial(1000*2)`, unevaluated). So a
#: COMPUTED argument forced the SAME eager evaluation a bare literal
#: does — for `factorint`/`primefactors`/`divisors`/`nextprime`/`isprime`
#: this either silently completed the real, unbounded work (a cap bypass)
#: or raised on a merely computed, not oversized, argument (THE-1095's
#: group-B wrong refusal — `factorint(2+3)` failed with "2 + 3 is not an
#: integer" even though 5 is nowhere near this cap). `mobius`, re-audited
#: for THE-1095, turns out to be the one member of this six that IS a real
#: `sympy.Function` subclass on sympy 1.14.0 (`isinstance(safe_global_
#: dict()["mobius"], type) and issubclass(..., sympy.Function)` is `True`)
#: and already respects `evaluate=False` correctly like `factorial` does —
#: the "none of these six" claim above was accurate when written (an
#: earlier sympy version, or a misreading at the time) but is stale for
#: 1.14.0; left uncorrected in the surrounding prose and noted here
#: instead, since THE-1095's fix (below) treats it the same as the other
#: five regardless — see `_DEFERRED_STANDIN_NAMES`'s own comment for the
#: full empirical audit of every name in the table, not just this family.
#: THE-1095: the danger used to be baked into the very act of PARSING,
#: before `reject_explosive` was ever invoked, with no tree-level rule able
#: to run early enough to prevent it (there was no tree yet) — `safe_parse`
#: now runs its `evaluate=False` pre-parse through `_deferred_global_dict()`
#: instead of `safe_global_dict()` specifically to give this family (and
#: every other eager name in `_FUNCTION_ARG_CAPS`) an inert stand-in for
#: that one parse, so a tree — and the tree-level backstop in
#: `_numeric_ceiling_scan`'s `Function`-node branch — now exists before any
#: real work runs. See `_oversized_call_arg_violation`'s own docstring for
#: the (unchanged, still literal-only) token-level layer's account of a
#: computed argument to this family, and `_deferred_global_dict`'s for the
#: tree-level one.
MAX_FACTOR_ARG_DIGITS = 25

#: `sqrt`/`root`/`cbrt` are the OPPOSITE failure mode from the factoring
#: family just above: cheap to BOUND in log space (an n-th root has
#: roughly `digits/n` digits — always comfortably under `MAX_NUMERIC_
#: DIGITS` for any argument this module would otherwise admit at all) but
#: not cheap to COMPUTE. SymPy's perfect-power check on a huge integer
#: measured 0.02s at 500 digits, 0.41s at 1000, 0.80s at 1500, 4.0s at
#: 1900 — growing faster than linearly in digit count. This is the exact
#: same CLASS of hazard `_numeric_ceiling_scan`'s own `Pow` handling exists
#: to catch generally (an intermediate that is expensive to CONSTRUCT
#: regardless of how small the printed RESULT turns out to be, #326
#: findings 1-2, round 7). Unlike the six above, `sqrt`/`cbrt` DO respect
#: `evaluate=False` — `sqrt(x)` IS `Pow(x, Rational(1, 2))`, and stays
#: that way, unevaluated, under `evaluate=False` (confirmed live) — so
#: their construction-cost hazard gets a genuine TREE-level backstop
#: instead, in `reject_explosive`'s Pow loop (see its own comment on the
#: unit-fraction-exponent branch). `root` does NOT share that path for
#: every call shape (probed live: `root(factorial(1463), 2)` parses to a
#: `Mul`, not a `Pow` — a different, more eager construction path — see
#: `_oversized_call_arg_violation`'s own comment). No `Function` node named
#: `sqrt`/`cbrt` ever appears in the tree to look up in `_FUNCTION_ARG_CAPS`
#: by name, so those two stay recognized structurally (their `Pow` shape),
#: never by a `Function`-node name lookup. `root` USED to stay on the
#: token-level layer alone for the identical structural reason — until
#: THE-1095 round 2 (`verify-1095`, both cross-vendor reviewers) gave it a
#: DEFERRED stand-in specifically so a `Function` node named `root` DOES
#: now exist for one parse (the scan-only one — see `_DEFERRED_STANDIN_
#: NAMES`'s own comment), closing the tree-level gap for its VALUE argument
#: (position 0) the same way every other deferred name gets one, while its
#: REAL construction (the `Mul`-vs-`Pow` split above) still only ever runs
#: on the real-dict parse, unaffected. `root` takes an optional second
#: (integer index) argument — `root(n, 2)` is `sqrt(n)` under a different
#: name — and that INDEX position (1, not 0) stays token-level-only, same
#: residual scope every other multi-argument deferred name's non-first
#: position now has (see the position-0-only comment on `_numeric_ceiling_
#: scan`'s `Function`-node branch).
MAX_ROOT_ARG_DIGITS = 1_200

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

#: #326 finding 1 (cross-vendor review, round 8, Codex): the round-seven
#: fix gave each new function family its OWN separate frozenset plus its
#: OWN separate token-only enforcement function — exactly the "recurred
#: because the new caps were added to one layer" pattern this finding
#: named: `factorint(<25-digit literal>*<25-digit literal>)` (product
#: ~50 digits, hard to factor) and `sqrt(<990-digit literal>*<990-digit
#: literal>)` (product ~1980 digits, 2.1s to compute) both passed
#: `classify_unsafe` clean, because EACH literal token is individually at
#: or under its own family's cap and nothing combined them.
#:
#: ONE table now names every function this module bounds an argument
#: for, by NAME -> `(cap_kind, cap_value)`: `"value"` means the
#: argument's own MAGNITUDE must not exceed `cap_value` (`_HEAVY_
#: FUNCTIONS`' shape — a small argument growing an expensive OUTPUT);
#: `"digits"` means the argument's DIGIT COUNT must not exceed it (the
#: factoring/root families' shape — the hazard scales with the size of
#: the NUMBER itself). Every enforcement site in this module — the token
#: screen's per-literal check, the token screen's per-argument COMBINED
#: check (`_oversized_call_arg_violation`, below), the tree-level
#: `Function`-node check in `_numeric_ceiling_scan`, and the tree-level
#: unit-fraction-`Pow` check in `reject_explosive`'s Pow loop — reads
#: this ONE table, so a future new cap needs one entry here, not a new
#: set plus new logic copy-pasted into however many of those sites
#: happen to apply to its particular shape.
_FUNCTION_ARG_CAPS: dict = {
    **dict.fromkeys(_HEAVY_FUNCTIONS, ("value", MAX_HEAVY_ARG)),
    **dict.fromkeys(("factorint", "primefactors", "divisors", "mobius", "nextprime", "isprime"), ("digits", MAX_FACTOR_ARG_DIGITS)),
    **dict.fromkeys(("sqrt", "root", "cbrt"), ("digits", MAX_ROOT_ARG_DIGITS)),
}

#: THE-1095 round 3 (verify-1095-r2, both reviewers, the recurring meta-
#: finding across THE-1091 and both rounds of THE-1095 so far: a bound
#: added at ONE position or ONE layer). `_FUNCTION_ARG_CAPS` above names,
#: per FUNCTION, the "value"/"digits" cap for argument POSITION 0 — round 2
#: made the tree-level scan check position 0 ONLY, closing a false-refusal
#: (`binomial(5, 1463+1)`, `k > n`, legitimately `0`) but opening a real
#: gap: a position OTHER than 0 can ALSO drive real cost, and round 2 never
#: bounded any of them. Verified live (sympy 1.14.0, this module's own
#: `guarded_call`-free timing): `rf(5, 1000**1000)`/`ff(5, 1000**1000)`
#: hang past 4s (`reduce(..., range(int(k)), 1)`, no early exit, k IS an
#: iteration count — see `RisingFactorial`/`FallingFactorial.eval`'s own
#: source, quoted in `_DEFERRED_STANDIN_NAMES`'s empirical-audit comment);
#: `rf(5, 1463+1463)` completes but returns an 8_887-digit integer (over
#: `MAX_NUMERIC_DIGITS`, closed structurally by the output ceiling on
#: `safe_parse`'s own evaluate=True step instead — see that function's own
#: docstring — since NO combination of two individually-capped positions
#: can be proven jointly safe without either a real growth bound or a
#: post-hoc check); `polygamma(0, 1000**1000)` hangs the identical way
#: through `harmonic(z-1)`'s recurrence, `z` at position 1; `nextprime(5,
#: 1000000)` walks a million primality tests, `ith` at position 1;
#: `divisor_sigma(5, 10**6)` builds `5**(10**6)` inside `eval()` itself,
#: `k` at position 1. `binomial`'s own `k` (also position 1) is
#: DELIBERATELY left OUT of this table: `binomial.eval()` (quoted in
#: `_growth_2n`'s own neighbor, `_growth_rf_ff`) returns `S.Zero` in O(1)
#: the instant a concrete `n` and `d = n - k` show `k > n`, with no loop
#: over `k` at all regardless of its magnitude — verified live,
#: `binomial(5, 10**9)` returns `0` in milliseconds — so capping it would
#: only manufacture a FALSE refusal, never close a real one.
#:
#: name -> {position: (kind, cap)} for every BOUNDED position other than 0
#: (0's own spec stays in `_FUNCTION_ARG_CAPS` above, unchanged, so every
#: existing position-0-only reader keeps working without modification).
#: `_position_spec` below is the ONE function that actually reads BOTH
#: tables — token screen, tree scan, and the self-check all call it, never
#: either table directly, so a name's position-1 (or beyond) bound is
#: exactly as visible to the cheap pre-parse screen as it is to the tree.
MAX_ITH_PRIME_SKIP = 1_000
#: `nextprime(n, ith)` walks `ith` successive primes past `n` — a plain
#: iteration count, the SAME shape as `rf`/`ff`'s `k`, not a digit-count
#: (`nextprime`'s own position-0 cap, `MAX_FACTOR_ARG_DIGITS`, means
#: something entirely different: the NUMBER under test can have up to 25
#: digits, `ith` is a plain small integer). Measured: `nextprime(2, 1000)`
#: (1000 steps from the smallest possible start) is comfortably sub-second;
#: chosen well under `MAX_HEAVY_ARG` (1463) since each step here is itself
#: a full primality search, unlike `rf`/`ff`'s cheap per-step multiply.
_EXTRA_BOUNDED_POSITIONS: dict = {
    "rf": {1: ("value", MAX_HEAVY_ARG)},
    "ff": {1: ("value", MAX_HEAVY_ARG)},
    "polygamma": {1: ("value", MAX_HEAVY_ARG)},
    "divisor_sigma": {1: ("value", MAX_HEAVY_ARG)},
    "nextprime": {1: ("value", MAX_ITH_PRIME_SKIP)},
}
#: name -> frozenset of positions that are members of `_HEAVY_FUNCTIONS`'
#: usual "value"-kind position-0 table by construction, but this specific
#: name's own argument at this position is EXPLICITLY exempt from any cap
#: (see `binomial`'s own comment above) — read by both the token screen and
#: the tree scan so neither accidentally reintroduces the false refusal the
#: OTHER one was fixed to avoid.
_UNBOUNDED_POSITIONS: dict = {
    "binomial": frozenset({1}),
}


def _position_spec(name: str, position: int) -> tuple[str, int] | None:
    """The `(kind, cap)` bound for `name`'s argument at `position`, or
    `None` if that position is not bounded for this name at all (an
    ordinary, never-checked argument, OR one explicitly exempted — see
    `_UNBOUNDED_POSITIONS`'s own comment). THE ONE function every
    enforcement site — token screen, tree scan, self-check — reads instead
    of `_FUNCTION_ARG_CAPS`/`_EXTRA_BOUNDED_POSITIONS` directly, so a
    name's spec lives in exactly one place regardless of how many
    positions it bounds.
    """
    if position in _UNBOUNDED_POSITIONS.get(name, ()):
        return None
    if position == 0:
        return _FUNCTION_ARG_CAPS.get(name)
    return _EXTRA_BOUNDED_POSITIONS.get(name, {}).get(position)


def _bounded_positions(name: str):
    """Every bounded position for `name`, in order — `(position, kind,
    cap)` triples. Position 0 first (if bounded), then any extras, so a
    caller that only wants "the primary cap" can still just take the
    first result, matching every position-0-only reader's existing
    behaviour.
    """
    spec0 = _position_spec(name, 0)
    if spec0 is not None:
        yield 0, spec0[0], spec0[1]
    for pos in sorted(_EXTRA_BOUNDED_POSITIONS.get(name, {})):
        kind, cap = _EXTRA_BOUNDED_POSITIONS[name][pos]
        yield pos, kind, cap


def _safe_pow10(log_value: float | None) -> float | None:
    """`10 ** log_value`, or `None` if `log_value` is missing or already so
    large that any of this module's GROWTH bounds (below) would come out
    astronomically over every real cap regardless of the exact figure —
    the same "decide from the magnitude, never attempt the operation on a
    value that might be too large" shape `_exp_upper_bound_value` already
    uses elsewhere in this file. 50 is nowhere near a real boundary (every
    cap this module compares a growth bound against tops out at
    `MAX_NUMERIC_DIGITS` = 4_000, i.e. log10 ~3.6) — it exists purely so a
    growth function's own arithmetic (`n * log10(n)`, `2*n*log10(n)`, ...)
    never approaches `float`'s own ~1.8e308 ceiling.
    """
    if log_value is None or log_value > 50:
        return None
    return 10.0 ** log_value


def _growth_nn(exponent_scale: float):
    """Builds a `bounds -> log10(upper bound)` growth function for `c * n
    ** n` shapes (`c` a small constant multiplier on the EXPONENT, not the
    base) from position 0's own bound alone — `factorial`/`bell`-scale
    growth, and (with `exponent_scale=2`) a deliberately LOOSE default
    catch-all for every OTHER single-position value-kind name in the table
    that has no tighter formula from this round's own repro list: verified
    (see this function's own call sites) that `n**(2n)` safely dominates
    `bernoulli`/`euler`'s true asymptotic growth (`~(2n)!/(2*pi)**(2n)`),
    which itself exceeds the plain `n**n` bound past `n ~ 73` — a real gap
    a tighter-looking default would have silently under-bounded.
    """
    def growth(bounds: dict) -> float:
        n = _safe_pow10(bounds.get(0))
        if n is None:
            return math.inf
        if n <= 1:
            return 0.0
        return exponent_scale * n * math.log10(n)
    return growth


_growth_nn_tight = _growth_nn(1.0)
_growth_nn_loose = _growth_nn(2.0)


def _growth_stirling_factorial(bounds: dict) -> float:
    """`log10(n!)` via Stirling's formula with its standard correction
    term — a safe (over-, never under-) approximation, verified against
    SymPy's own real digit counts (`factorial(1_463) = 3_998`,
    `factorial(1_500) = 4_115`, `factorial(20) = 19`) to within ONE digit
    at every scale checked.

    THE-1095 round-3-follow-up #4 (coordinator review of 06272aa, grok
    item 2): `_growth_nn_loose` (`n**(2n)`) is a deliberately LOOSE
    catch-all, safe for names this module has no tighter formula for, but
    it overestimates `bell(1463)` at ~9258 "digits" against its true
    3_018 — already claiming "over `MAX_NUMERIC_DIGITS`" with NOTHING
    else contributing at all, so `bell(1463) % 7` (and any wrapper
    around it — `-bell(1463)`, `2*bell(1463)`, `bell(1463)+1`) was
    refused even though the true value is comfortably under the cap and
    `origin/main` evaluates it (the documented ~5s at-cap construction
    cost, already accepted elsewhere in this module for bare
    `bell(1463)`). `bell(n) <= n!` for every `n >= 0` (Bell numbers count
    SET PARTITIONS of an n-set, strictly fewer than the n! PERMUTATIONS
    once n >= 3) — a well-known, provable combinatorial fact, not an
    empirical observation — so Stirling's own `log10(n!)` is a valid,
    and now the TIGHTEST available, safe upper bound for `bell` too:
    `_growth_stirling_factorial(1463)` correctly stays under
    `MAX_NUMERIC_DIGITS` (`bell(1463)`, safe, evaluates) while
    `_growth_stirling_factorial(1464)` correctly clears it (`bell(1464)`,
    genuinely over cap, refuses) — verified against the adjacent-integer
    boundary this specific pair straddles, not just an order-of-magnitude
    check.

    The `+ (1/(12n))*log10(e)` term is NOT decorative: the bare Stirling
    approximation (`sqrt(2*pi*n)*(n/e)**n`, no correction) is only
    asymptotically an upper bound as `n -> infinity` — Robbins' own,
    tighter theorem brackets `n!` as `sqrt(2*pi*n)*(n/e)**n*exp(1/(12n+1))
    < n! < sqrt(2*pi*n)*(n/e)**n*exp(1/(12n))`, and the bare version
    (equivalent to dropping the `exp(1/(12n))` factor, i.e. using the
    LOWER bracket's own leading term) measurably UNDER-shot the true
    value for small `n` when first written — caught by this file's own
    property-style check, not by inspection: `n=2` through `n=59` all
    failed by a fraction of a digit (`factorial(2)`: real `log10 ~=
    0.301`, bare-Stirling bound `~= 0.283`, UNDER). Robbins' own UPPER
    bracket is what this now computes, sound for every `n >= 1`, not
    merely for `n` large enough that the omitted correction term
    stops mattering.
    """
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    if n < 2:
        return 0.0
    return (n * math.log10(n) - n * math.log10(math.e) + 0.5 * math.log10(2 * math.pi * n)
            + (1.0 / (12 * n)) * math.log10(math.e))


def _growth_2n(bounds: dict) -> float:
    """`binomial(n, k) <= 2**n` (position 0 only — `k`, position 1, is
    deliberately unbounded; see `_UNBOUNDED_POSITIONS`'s own comment)."""
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    return n * math.log10(2)


def _growth_rf_ff(bounds: dict) -> float:
    """`rf(x, k)`/`ff(x, k) <= (x + k) ** k` — both positions feed the
    bound (position 1, `k`, is the iteration count; see
    `_EXTRA_BOUNDED_POSITIONS`'s own comment for why it needed capping at
    all)."""
    x = _safe_pow10(bounds.get(0))
    k = _safe_pow10(bounds.get(1))
    if x is None or k is None:
        return math.inf
    if k <= 0:
        return 0.0
    base = x + k
    if base <= 1:
        return 0.0
    return k * math.log10(base)


def _growth_primorial(bounds: dict) -> float:
    """`primorial(n, nth=True)` — the DEFAULT, no-second-arg form — is the
    product of the FIRST `n` primes, NOT the product of primes `<= n`
    (that second meaning, `nth=False`, is `n#` in the traditional
    number-theory sense, and is what `e ** (1.02 * n)` actually bounds;
    grok's round-3-follow-up review caught this call site using the wrong
    one of the two). By Rosser's theorem `log(product of first n primes)
    ~ p_n` (the n-th prime itself), so this reuses `_growth_prime`'s own
    `p_n <= n*(ln n + ln ln n)` bound and applies the SAME `e**(1.02*x)`
    safety factor the old formula already used, just against `p_n`
    instead of `n` directly.

    `nth` (position 1) is a boolean flag, not a magnitude — this bound
    does not read it — but it is still SAFE for the `nth=False` call too:
    for the SAME `n`, "primes <= n" is a subset of magnitude up to `n`,
    while "the first n primes" runs up to roughly `n * ln(n)` (much
    larger for any `n` where the distinction matters), so the first-n-
    primes bound computed here is never smaller than the primes-<=-n
    product for the same `n` — one formula safely covers both meanings.
    """
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    if n < 1:
        return 0.0
    if n < 6:
        # `_growth_prime`'s own `p_n` formula needs `ln(ln(n))`, undefined
        # below `n = e` -- a simple, verified-safe stand-in for this tiny
        # domain (primorial(1..5) = 2, 6, 30, 210, 2310).
        return n * math.log10(3 * n)
    p_n_bound = n * (math.log(n) + math.log(math.log(n)))
    return 1.02 * p_n_bound * math.log10(math.e)


def _growth_prime(bounds: dict) -> float:
    """`prime(n) <= n * (ln n + ln ln n)` for `n >= 6` (the standard prime-
    counting bound); a tiny constant below that, since `prime(1..5)` are
    each a single-digit value."""
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    if n < 6:
        return 1.1  # prime(1..5) = 2, 3, 5, 7, 11 -- 11 needs > log10(11) ~= 1.04
    value = n * (math.log(n) + math.log(math.log(n)))
    return math.log10(value) if value > 1 else 0.0


def _growth_nextprime(bounds: dict) -> float:
    """`nextprime(n, ith=1)` — the `ith`-th prime after `n`. Bertrand's
    postulate (`< 2*n`) alone is only a valid bound at `ith=1`.

    THE-1095 round-3-follow-up (coordinator review of 949aac9, grok item
    3): the PREVIOUS formula here (an "average prime gap" estimate,
    `n + ith*(ln(n+ith+1) + 10)`) is an AVERAGE-case approximation, not a
    proven upper BOUND — grok's own repro: at `n=887, ith=1`, it computes
    `903.79`, while `nextprime(887) == 907` — the true value EXCEEDS the
    claimed bound, an actual unsafe under-estimate on a real, unremarkable
    prime gap (20, nothing close to a record), not merely an edge case.
    An average-gap formula can always be beaten by SOME gap larger than
    the local average — a "generous" additive slack constant is not the
    same claim as "sound for every n".

    Replaced with Bertrand's postulate compounded `ith` times IN LOG
    SPACE (`nextprime(n, ith) < n * 2**ith`, since each successive prime
    is less than double the one before it — true for every n >= 1, ith
    >= 0, no exceptions, no record-gap edge cases to worry about):
    `log10(n) + ith * log10(2)`. Far looser than the deleted average-gap
    formula for anything past `ith=1` or `2` (by `MAX_ITH_PRIME_SKIP`,
    1000, the bound is astronomically larger than any real value could
    ever be), but SOUND — the only property this bound actually needs to
    have, verified against the adversarial grid in `tests/test_bug_
    sweep.py` (887, 1327, 31397, a ~1.7e15 record-gap prime, `10**25 -
    1`, `ith` in `{1, 2, 10, 1000}`), not just round numbers.
    """
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    if n <= 0:
        n = 2.0
    ith = _safe_pow10(bounds.get(1))
    if ith is None:
        ith = 1.0
    if ith < 0:
        ith = 0.0
    return math.log10(n) + ith * math.log10(2)


def _growth_primepi(bounds: dict) -> float:
    """`primepi(n) <= n` (the prime-counting function never exceeds its
    own argument)."""
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    return math.log10(n) if n > 0 else 0.0


def _growth_npartitions(bounds: dict) -> float:
    """`npartitions(n)`/`partition(n) <= e ** (pi * sqrt(2n/3))` (the
    Hardy-Ramanujan asymptotic, used as a strict upper bound)."""
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    return math.pi * math.sqrt(2 * n / 3) * math.log10(math.e)


def _growth_divisor_sigma(bounds: dict) -> float:
    """`divisor_sigma(n, k) <= n ** (k + 1)` — loose (the true bound is
    tighter for composite `n`, but this holds for every `n`, including a
    prime, where `divisor_sigma(n, k) = 1 + n**k` exactly) — using `k = 1`
    when position 1 was not itself bounded (unresolved or absent; the
    default single-argument call, `divisor_sigma(n)`, is `k = 1`)."""
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    if n <= 1:
        return 0.0
    k = _safe_pow10(bounds.get(1))
    if k is None:
        k = 1.0
    return (k + 1) * math.log10(n)


def _growth_harmonic(bounds: dict) -> float:
    """`harmonic(n) <= ln(n) + 1` — the harmonic series' own logarithmic
    growth; always tiny relative to every cap this bounds feed into, kept
    as an actual formula (not a constant) so it stays correct if
    `MAX_HEAVY_ARG` itself ever changes."""
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    if n <= 1:
        return 1.0
    return math.log10(math.log(n) + 1) if math.log(n) + 1 > 1 else 0.0


def _growth_polygamma(bounds: dict) -> float:
    """`polygamma(order, z)` at order 0 (`digamma`) is `harmonic(z-1) -
    EulerGamma`, itself tiny (logarithmic in `z`) — bounded from position
    1 (`z`) alone, since `z` is what the module actually caps (see
    `_EXTRA_BOUNDED_POSITIONS`'s own comment); a small constant covers
    every order this table admits (order is capped at `MAX_HEAVY_ARG`
    through the ordinary position-0 path, but never realistically
    approaches it — see `_HEAVY_FUNCTIONS`'s own comment)."""
    z = _safe_pow10(bounds.get(1))
    if z is None:
        return math.inf
    if z <= 1:
        return 1.0
    return math.log10(math.log(z) + 2)


def _growth_base_n(log10_base: float):
    """Builds a `bounds -> log10(upper bound)` growth function for a plain
    `base ** n` shape (position 0's own bound is the exponent, `base` is a
    small FIXED constant) — `fibonacci`/`lucas` (`<= 2 * phi**n`, `phi`
    ~1.618), `tribonacci` (`<= 2**n`, the tribonacci constant itself is
    ~1.839 < 2), `catalan` (`<= 4**n`). Split out from `_growth_2n`
    (binomial's own, position-0-only, base exactly 2) once `factorial(
    fibonacci(5))` — genuinely `= 120`, safe to evaluate — measured
    REFUSED: the `n**(2n)` default below is only a safe (if loose) bound
    for `n**n`-SCALE growth (`factorial`/`bell`/...); applying it to
    `fibonacci` overestimated `fibonacci(5)`'s own bound by ORDERS of
    magnitude (`10**7` claimed for the true value `5`), tripping
    `factorial`'s outer cap on a nested call that was never actually
    dangerous — `unknown != safe` only ever means "an unresolved value",
    never license for a growth formula that is not actually a valid upper
    bound.
    """
    def growth(bounds: dict) -> float:
        n = _safe_pow10(bounds.get(0))
        if n is None:
            return math.inf
        return n * log10_base
    return growth


_PHI = 1.6180339887498949  # (1 + sqrt(5)) / 2


def _growth_fibonacci(bounds: dict) -> float:
    """`fibonacci(n) = round(phi**n / sqrt(5))` exactly (Binet's formula;
    the omitted `psi**n` term, `psi = -1/phi`, has magnitude < 0.5 for
    every `n >= 1` and never changes the rounded result) — so `log10(
    fibonacci(n)) ~= n*log10(phi) - log10(sqrt(5))`, plus a small additive
    safety epsilon for the omitted term and float rounding.

    Round-3-follow-up (grok): the PREVIOUS formula here, built through
    `_growth_base_n(log10(2 * phi))`, computed `n * log10(2*phi)` —
    `log10((2*phi)**n)`, i.e. `(2*phi)**n`, not the intended `2 *
    phi**n` (multiplying the base by 2 inside the log instead of adding a
    constant outside it). That direction of error is CONSERVATIVE
    (over-large, never unsafe) but verified too loose to accept even
    `fibonacci(16) = 987` under `factorial`'s `MAX_HEAVY_ARG = 1463` cap
    (`(2*phi)**16 ~= 4416 > 1463`, though the true value is well under);
    the exact Binet form fixes that, correctly accepting `fibonacci(16)`
    and correctly still refusing `fibonacci(17) = 1597` (`> 1463`)."""
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    if n < 4:
        return 0.5  # fibonacci(0..3) = 0, 1, 1, 2 -- all comfortably < 10**0.5
    return n * math.log10(_PHI) - math.log10(5**0.5) + 0.01


def _growth_lucas(bounds: dict) -> float:
    """`lucas(n) = round(phi**n + psi**n) ~= phi**n` (no `sqrt(5)`
    division, unlike `fibonacci`) — the same exact Binet-style base,
    minus the `_growth_fibonacci` divisor, still a safe, tight bound
    (`lucas(n) > fibonacci(n)` always, consistent with the real
    relationship between the two sequences)."""
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    if n < 3:
        return 0.5  # lucas(0..2) = 2, 1, 3 -- all comfortably < 10**0.5
    return n * math.log10(_PHI) + 0.01


def _growth_digamma(bounds: dict) -> float:
    """`digamma(z) == polygamma(0, z)` exactly (this module's own
    `math_transforms` already rewrites one to the other) — the SAME tiny,
    logarithmic bound as `_growth_polygamma`, just read from position 0
    (digamma's own single argument) instead of position 1 (polygamma's
    `z`, next to its `order`). Round-3-follow-up (grok): `digamma` sat in
    the `n**(2n)` catch-all bucket below despite being genuinely
    logarithmic, false-refusing e.g. `factorial(digamma(10**100))`."""
    z = _safe_pow10(bounds.get(0))
    if z is None:
        return math.inf
    if z <= 1:
        return 1.0
    return math.log10(math.log(z) + 2)


_growth_tribonacci = _growth_base_n(math.log10(2))  # same base as _growth_2n
_growth_catalan = _growth_base_n(math.log10(4))


def _growth_totient(bounds: dict) -> float:
    """`totient(n) <= n` — Euler's totient never exceeds its own argument."""
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    return math.log10(n) if n > 0 else 0.0


#: name -> `bounds: dict[position, log10] -> log10(upper bound on the
#: result's magnitude)`, used ONLY when a table name appears as a NESTED
#: argument to another table name (`divisors(factorial(100))`,
#: `factorial(polygamma(0, 1000**1000))`, ...) — see `_table_function_
#: bound`'s own docstring. A table name with NO entry here (the `sqrt`/
#: `root`/`cbrt` family, and the eager-factor family — `factorint` and its
#: five siblings return a `dict`/`list`/`bool`, not a number, so "growth"
#: has no meaning for them) fails CLOSED if nested: `unknown != safe`, not
#: silently skipped the way a non-table function (`sin`, `exp`, ...)
#: still is. `n**(2n)` (`_growth_nn_loose`) is the catch-all default for
#: anything left below with no tighter formula, but ONLY for names that
#: are genuinely `n**n`-SCALE growers (verified against this module's own
#: measured digit counts at the existing cap, `_HEAVY_FUNCTIONS`'s own
#: comment) — `fibonacci`/`lucas`/`tribonacci`/`catalan`/`totient` grow far
#: slower (exponential-base or linear) and get their OWN, tighter formula
#: above instead, precisely because the loose default is not merely
#: imprecise for them, it is wrong enough to cause a false refusal on an
#: ordinary, cheap, in-range nested call.
_GROWTH_BOUNDS: dict = {
    "binomial": _growth_2n,
    "rf": _growth_rf_ff, "ff": _growth_rf_ff,
    "primorial": _growth_primorial,
    "prime": _growth_prime,
    "nextprime": _growth_nextprime,
    "primepi": _growth_primepi,
    "npartitions": _growth_npartitions, "partition": _growth_npartitions,
    "divisor_sigma": _growth_divisor_sigma,
    "harmonic": _growth_harmonic,
    "polygamma": _growth_polygamma,
    "digamma": _growth_digamma,
    "fibonacci": _growth_fibonacci, "lucas": _growth_lucas,
    "tribonacci": _growth_tribonacci,
    "catalan": _growth_catalan,
    "totient": _growth_totient,
    "factorial": _growth_stirling_factorial, "bell": _growth_stirling_factorial,
    **dict.fromkeys(
        ("subfactorial", "gamma", "factorial2", "loggamma",
         "zeta", "andre", "genocchi", "motzkin", "bernoulli", "euler"),
        _growth_nn_loose,
    ),
}

#: THE-1095, follow-up to #326/THE-1091 (GH #326, PR #334): every fix above
#: this point bounds a LITERAL argument at the token level, or a real
#: `sympy.Function` node's argument at the tree level in `_numeric_ceiling_
#: scan` below, by looking up `type(node).__name__` in `_FUNCTION_ARG_CAPS`.
#: Both assume a COMPUTED argument (`factorial(1463+1)`) either (a) stays an
#: unevaluated `Function` node named after its own table entry through the
#: `evaluate=False` pre-parse, so the tree-level check can see and bound it,
#: or (b) simply parses without incident. Audited empirically (sympy 1.14.0,
#: `parse_expr(..., evaluate=False)` through this module's own
#: `safe_global_dict()`/`math_transforms()`) against every name in the table
#: above, and both assumptions are false for a working minority of it — not
#: because `evaluate=False` fails to suppress `Add`/`Mul`/`Pow`/`Sub`/`Div`
#: (it does, correctly), but because of WHAT `evaluate=False` actually is:
#: `sympy.parsing.sympy_parser.EvaluateFalseTransformer` (the AST pass
#: `parse_expr` runs when `evaluate=False`) appends an explicit
#: `evaluate=False` keyword ONLY to `Add`/`Mul`/`Pow`/`Sub`/`Div` BinOp nodes
#: and to a fixed whitelist of trig/log/`sqrt`/`cbrt` names
#: (`EvaluateFalseTransformer.functions`) — every OTHER function call in the
#: source, `NAME(...)` for any `NAME` this module screens, is compiled with
#: NO `evaluate` keyword at all, so `Function.__new__` falls back to its own
#: ambient default, `global_parameters.evaluate`, which is `True` unless a
#: `with sympy.evaluate(False):` context manager is active — and `parse_expr`
#: never opens one; it only rewrites those specific AST nodes. So a heavy
#: function's OWN `eval()` classmethod runs exactly as if `evaluate=False`
#: had never been passed at all, and whether that produces the (correct, and
#: relied upon) unevaluated `Function` node this module's tree-level checks
#: expect depends entirely on what that classmethod does with a non-`Integer`
#: (`Add(1463, 1, evaluate=False)`-shaped) argument — a per-function accident
#: of implementation, not something this module or `evaluate=False` controls:
#:
#:   `factorial`, `mobius`, `totient`, `bell`, `harmonic`, `npartitions`
#:   (deprecated alias for `partition`), and every other `_HEAVY_FUNCTIONS`
#:   member not named below: `eval()` checks `is_Integer` (concrete-type-
#:   only) on its argument, which an unevaluated `Add`/`Pow` is not, so it
#:   declines to evaluate and the existing tree-level check already worked —
#:   `mobius` in particular is a genuine `sympy.Function` subclass on 1.14.0
#:   (`isinstance(safe_global_dict()["mobius"], type) and issubclass(...,
#:   sympy.Function)` is `True`), contradicting this file's own older
#:   comment above (`MAX_FACTOR_ARG_DIGITS`'s docstring) that groups it with
#:   the eager plain-callable family below — stale for this sympy version,
#:   left uncorrected there and noted here instead since deferring it
#:   (below) makes the distinction moot either way.
#:
#:   `rf`/`ff` (parse to classes `RisingFactorial`/`FallingFactorial`,
#:   confirmed via `type(parse_expr("rf(x,y)", evaluate=False)).__name__`)
#:   and `binomial`/`primepi`: their `eval()` classmethods, for an
#:   integer-valued second argument (`k` for `rf`/`ff`/`binomial`; always for
#:   `primepi`), build the result via a Python `reduce`/loop doing ordinary
#:   `+`/`*` on the (unevaluated) argument — and THAT arithmetic, being
#:   plain Python operator calls with no `evaluate=False` keyword anywhere
#:   near it, runs under the ambient default and evaluates for real. Measured
#:   live: `rf(1463+1, 2)` parses straight to `Integer(2144760)` — no
#:   `Function` node ever exists for the tree-level check to inspect, and
#:   `_heavy_call_violation`'s token screen has no notion of `+` either (its
#:   own docstring already says this precisely for the "literal, computed"
#:   distinction) — this is the group-A cap BYPASS.
#:
#:   `digamma` is the SAME bypass shape wearing a rename: its `eval()`
#:   unconditionally REWRITES to `polygamma(0, x)` at construction — verified
#:   live, `type(parse_expr("digamma(x)", evaluate=False)).__name__ ==
#:   "polygamma"` even for a plain symbol `x`, so this is not evaluate-
#:   dependent at all. `type(node).__name__` is `"polygamma"`, never
#:   `"digamma"`, so the table lookup never matches regardless of what the
#:   argument is, and the argument that WOULD carry the bound has also moved
#:   from position 0 (`digamma`'s own single argument) to position 1
#:   (`polygamma`'s second argument, `nu` being the order, fixed at 0 by the
#:   rewrite) — a second, independent reason the old lookup could never work
#:   for this name even if it matched by class.
#:
#:   `primorial`, `prime`, `motzkin`, `isprime`, `factorint` (a `_HEAVY_
#:   FUNCTIONS` trio plus two of the "digits"-kind family): none of these
#:   five is a `sympy.Function` subclass at all (`primorial`/`prime` are
#:   plain module-level callables despite living in `_HEAVY_FUNCTIONS`, and
#:   `motzkin`/`isprime`/`factorint` likewise) — each coerces its argument
#:   with something equivalent to `sympy.core.numbers.igcd`/`as_int`, which
#:   raises `ValueError` outright on an unevaluated `Add`/`Pow` REGARDLESS OF
#:   MAGNITUDE: `primorial(2+3)` (a trivially small, nowhere-near-the-cap
#:   computed argument) fails identically to `primorial(1463+1)` — this is
#:   the group-B WRONG REFUSAL, and it happens inside `safe_parse`'s OWN
#:   `evaluate=False` pre-parse (the call meant only to build a shape for
#:   `reject_explosive` to inspect before anything real runs), so it surfaces
#:   as a `validation` parse error rather than ever reaching evaluation.
#:
#:   `nextprime`, `divisors`, `primefactors` (the remaining "digits"-kind
#:   trio): plain callables like the five above, but each happens to accept
#:   an unevaluated numeric `Add`/`Pow` without raising and returns the REAL
#:   answer directly (`divisors(1463+1)` -> the actual factor list of 1464,
#:   `nextprime(1463+1)` -> the actual `int` 1471) — the group-A bypass shape
#:   again, just silent instead of loud, and for the "digits" cap
#:   (`MAX_FACTOR_ARG_DIGITS`) rather than "value": `nextprime(10**2000)`
#:   (an unevaluated `Pow`, its computed VALUE ~2001 digits, both digit-sum
#:   tokens in the CALL individually tiny) already hangs past a 20s timeout
#:   in this same pre-parse on an unpatched tree — bounded only by
#:   `guarded_call`'s backstop today, closed as a side effect below.
#:
#: The fix (see `_deferred_global_dict`, used by `safe_parse`'s pre-parse
#: step in place of `safe_global_dict`) does not chase these five different
#: root causes individually or hand-maintain a class-name/argument-position
#: table to compensate for the ones that rename or reshuffle position
#: (`digamma`) — a future name added to `_HEAVY_FUNCTIONS`/`_FUNCTION_ARG_
#: CAPS` might have any of these accidents or a new one entirely, and this
#: module has no way to audit sympy's internals for it automatically. It
#: replaces EVERY name in `_FUNCTION_ARG_CAPS`, uniformly, with an inert
#: stand-in for the ONE parse this module uses to decide safety, so the
#: table lookup by name and the argument POSITIONS the caller actually wrote
#: are always what the scan sees, regardless of what the real function's
#: `eval()` would have done — sidestepping the rename (`digamma`), the
#: reduce-based eager arithmetic (`rf`/`ff`/`binomial`/`primepi`), and the
#: `as_int`-raises-on-symbolic-input family (`primorial`/`prime`/`motzkin`/
#: `isprime`/`factorint`) all with the same mechanism, rather than three.
_DEFERRED_STANDIN_NAMES = frozenset(_FUNCTION_ARG_CAPS) - {"sqrt", "cbrt"}
#: `sqrt`/`cbrt` ALONE are excluded because they ALREADY respect
#: `evaluate=False` correctly (their own module-level function is written to
#: hand back an unevaluated `Pow(base, Rational(1, n), evaluate=False)` for
#: exactly this reason — see `MAX_ROOT_ARG_DIGITS`'s own comment) and
#: already have a working tree-level backstop for a computed argument,
#: `reject_explosive`'s unit-fraction-exponent `Pow` branch — replacing them
#: with a `Function` stand-in would swap that working `Pow` shape for an
#: inert `Function` node and break it, not fix anything.
#:
#: `root` USED to be excluded for a related but different reason: it does
#: not reliably produce EITHER shape (`Pow` or a `Function` node named after
#: itself) for every call — `root(x, 2)` parses to a `Mul` when SymPy's own
#: construction pulls perfect-square-ish factors out of an already-concrete
#: base eagerly (see `_oversized_call_arg_violation`'s own comment) — so a
#: stand-in named `root` was never reachable on the path that mattered.
#: THE-1095 round 2 (verify-1095, both reviewers): with `safe_parse` now
#: running the DEFERRED parse FIRST (see its own docstring), `root` staying
#: on the REAL dict for that first parse meant a computed, digit-heavy
#: VALUE argument (`root(10**2000, 3)`) still ran root's own real,
#: sometimes-expensive construction before any scan could refuse it — the
#: exact ordering hazard this round exists to close, just for the one name
#: that was still exempt from it. Deferring `root` too closes that: its
#: stand-in is a `Function` node named `root`, which the "digits"-kind
#: branch below already matches by table lookup like any other name in
#: `_FUNCTION_ARG_CAPS` — this bounds root's VALUE argument (position 0,
#: same as every other deferred name — see the position-0-only comment on
#: `_numeric_ceiling_scan`'s `Function`-node branch) at the TREE level for
#: the first time, closing exactly the `root(10**2000, 3)`-shaped gap.
#: `root`'s own INDEX argument (position 1 — how the "`root(n, 2)` is
#: `sqrt(n)` under a different name" comment on `MAX_ROOT_ARG_DIGITS` refers
#: to) stays token-level-only, same residual scope every other deferred
#: name's non-zero-index arguments now have (see that same comment) — an
#: astronomically large root INDEX is a real, separate hazard, but not one
#: this round's own repros named, and the TOKEN screen already bounds it
#: for a literal index; unaffected either way by deferring `root` itself.
#: `root`'s REAL construction (the `Mul`-vs-`Pow` inconsistency) still only
#: ever runs on the REAL-dict parse, now reached only once the deferred
#: scan has already confirmed the value argument is within
#: `MAX_ROOT_ARG_DIGITS` (1200) — comfortably fast at that size per
#: `MAX_ROOT_ARG_DIGITS`'s own measurements (<=0.8s at 1500 digits),
#: regardless of which of the two shapes that construction happens to take.


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


def _exact_decimal_digit_count(token_string: str) -> int | None:
    """The EXACT decimal digit count of a `tokenize.NUMBER` token's text if
    it is a plain decimal integer literal, or `None` if it is not (a
    `0x`/`0o`/`0b`-prefixed literal, or anything `int(token_string, 0)`
    would reject) — the character count of a hex/octal/binary literal
    bears no relation to the DECIMAL digit count of its value once
    converted (`_oversized_call_arg_violation`'s own comment covers this
    same trap for `_heavy_call_violation`), so those fall back to
    `_approx_decimal_digits` at the call site instead.

    #326 finding 2 (cross-vendor review, round 8, Codex): `_approx_decimal_
    digits`' own docstring already says it is for PROSE, not for gating a
    decision — but a digit-count CAP comparison (`_oversized_call_arg_
    violation`, `MAX_FACTOR_ARG_DIGITS`/`MAX_ROOT_ARG_DIGITS`) is exactly a
    decision, and it used that approximation anyway. `_log10_of_int`'s
    float `log10` is accurate almost everywhere but was measured to round
    the WRONG way at an EXACT power-of-ten boundary: a 25-digit all-9s
    literal (`10**25 - 1`) reads as 26 digits, a 1,200-digit all-9s
    literal as 1,201 — precisely the boundary a cap enforcement decision
    sits on. This is exact and just as cheap for the overwhelmingly common
    case (a plain decimal literal): strip PEP-515 underscores and any
    leading zeros (`007` is a 1-digit value; `0` alone stays a single `0`)
    and count what is left — never converts the token to an `int` at all,
    so there is nothing here for `sys.set_int_max_str_digits` to object
    to, and the token itself is already bounded by `_MAX_EXPR_LEN` (2000
    chars), so this is O(2000) at the absolute worst.
    """
    s = token_string.replace("_", "")
    if len(s) >= 2 and s[0] == "0" and s[1] in "xXoObB":
        return None
    s = s.lstrip("0")
    return len(s) if s else 1


def _heavy_call_violation(tokens: list) -> str | None:
    """A heavy function applied to an oversized integer LITERAL, if any.

    Only literals are checked here, because only literals are what THIS
    (token-level, pre-parse) pass can see before anything else bounds them —
    this function exists to stop work that would otherwise happen DURING
    parsing, cheaply, without building a tree first.

    A COMPUTED argument (`factorial(1463+1)`) is NOT caught here — every
    token (`1463`, `1`) is individually under `MAX_HEAVY_ARG`, and this
    function has no notion of `+`. That claim used to be "caught later by
    the tree rules" without qualification, which was FALSE (#326 finding 1,
    cross-vendor review, round 6): `evaluate=False` parsing leaves
    `factorial(1463+1)` as a `Function` wrapping an unevaluated
    `Add(1463, 1)` — nothing in the numeric scan's `Integer`/`Mul`/`Add`/
    `Pow` trigger set matches a `Function` node, so it stayed opaque and the
    SECOND (real, `evaluate=True`) parse computed the actual 4001-digit
    result unrefused. `_numeric_ceiling_scan` now ALSO bounds a heavy
    `Function` node's own argument directly (via `_log10_num_den`, the same
    machinery — see its own call site there), closing the gap this
    docstring used to claim was already closed.

    A HEAVY CALL NESTED INSIDE ANOTHER HEAVY CALL'S argument
    (`factorial(fibonacci(100))`, `fibonacci(factorial(10))`,
    `factorial(factorial(8))`) is a THIRD, different hazard from either of
    the two above (#326 finding 2, cross-vendor review, round 7, Codex):
    ON MAIN, before THE-1095 existed, SymPy's `parse_expr`, even with
    `evaluate=False`, still evaluated a heavy function whose argument was
    already a plain integer LITERAL eagerly, bottom-up, while building the
    tree, so the INNER call's numeric RESULT (not the source text) became
    the OUTER call's argument before `reject_explosive` ever got a tree to
    inspect — `fibonacci(factorial(10))` materializes a ~758,374-digit
    integer, `factorial(fibonacci(100))` never returns, both INSIDE the
    `evaluate=False` parse itself. THIS FUNCTION USED TO refuse any such
    nesting unconditionally, right here, at the token level, before that
    parse ever ran — the only check that COULD run early enough at the
    time.

    THE-1095 round 3 (`verify-1095-r2`): closed the eager-evaluation half
    of this structurally instead, and REMOVED the blanket token-level
    nested-call refusal that used to live in this loop — `safe_parse`'s
    deferred pre-parse (see its own docstring) hands BOTH names an inert
    stand-in with no `eval()`, so NEITHER ever evaluates eagerly regardless
    of nesting, and `_numeric_ceiling_scan`'s `Function`-node branch now
    recurses into a nested table-function argument via `_table_function_
    bound`'s own GROWTH-bound machinery instead, PRECISELY:
    `factorial(fibonacci(100))` is still refused promptly (fibonacci's own
    growth bound for `n=100` is already far over factorial's cap; no
    growth function at all for an UNCOVERED name fails CLOSED the same
    way, `unknown != safe`, see `_table_function_bound`'s own docstring),
    while `factorial(fibonacci(5))` (`= 120`) and `factorial(binomial(6,
    3))` (`= 2_432_902_008_176_640_000`) now correctly EVALUATE instead of
    the old blanket "any nesting" refusal, since neither inner result is
    remotely close to factorial's own cap — a real correctness gain the
    OLD, deliberately-conservative token-only rule structurally could not
    offer (bounding an inner call's result at the token level would have
    required evaluating it). This function's remaining job, below, is
    purely the per-literal MAGNITUDE check (a bare oversized number, not a
    nested call).
    """
    for i, tok in enumerate(tokens):
        if tok.type != tokenize.NAME or tok.string not in _HEAVY_FUNCTIONS:
            continue
        if i + 1 >= len(tokens) or tokens[i + 1].string != "(":
            continue  # a bare mention like `gamma` as a symbol, not a call
        # THE-1095 round 3 (verify-1095-r2): reads `_position_spec` per
        # ARGUMENT POSITION now, not one blanket cap for the whole call —
        # `binomial`'s own `k` (position 1) is deliberately UNBOUNDED (see
        # `_UNBOUNDED_POSITIONS`'s own comment: `binomial.eval()` resolves
        # `k > n` to `0` in O(1), so capping the literal there only
        # manufactured a false refusal), while `rf`/`ff`/`divisor_sigma`/
        # `polygamma`'s own position 1 — previously checked here only by
        # the accident of this loop not distinguishing position at all —
        # is now checked deliberately, against ITS OWN (possibly
        # different) cap via `_EXTRA_BOUNDED_POSITIONS`.
        position = 0
        depth = 0
        for j in range(i + 1, len(tokens)):
            nxt = tokens[j]
            if nxt.type == tokenize.OP and nxt.string == "(":
                depth += 1
            elif nxt.type == tokenize.OP and nxt.string == ")":
                depth -= 1
                if depth == 0:
                    break
            elif nxt.type == tokenize.OP and nxt.string == "," and depth == 1:
                position += 1
            elif nxt.type == tokenize.NUMBER:
                spec = _position_spec(tok.string, position)
                if spec is None:
                    continue  # this position is not bounded for this name
                max_arg = spec[1]
                try:
                    # base 0, not base 10. `int("0xffffff")` raises ValueError,
                    # and the except below skipped it — so factorial(0xffffff)
                    # sailed past a cap that stops factorial(16777215), the
                    # same number written differently. Octal and binary
                    # literals had the identical hole.
                    value = int(nxt.string, 0)
                except ValueError:
                    continue  # a float or complex literal — not this hazard
                if value > max_arg:
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
                            f"of {max_arg}: computing it would take an "
                            "unbounded amount of time and memory")
    return None


def _call_arg_cap_violation(func_name: str, kind: str, cap: int, literals: list) -> str | None:
    """The refusal message for ONE call argument's literal `(value, digit_
    count)` pairs (`literals`, possibly empty — nothing to check then), or
    `None` if it is within `cap` for its `kind`. Shared by the "value" and
    "digits" branches of `_oversized_call_arg_violation`'s per-argument
    accounting, so the two enforcement RULES stay in exactly one place
    even though their COMPARISON differs — see that function's own
    docstring for why.
    """
    if not literals:
        return None
    if kind == "value":
        # An exact per-token comparison, and only when this ARGUMENT held
        # exactly one literal — combining several literals into one
        # "value" the way `MAX_HEAVY_ARG` compares against has no
        # principled meaning (unlike summing DIGIT counts across a
        # product, below); a multi-literal argument to a "value"-kind
        # function (`factorial(1463+1)`) is left to `_numeric_ceiling_
        # scan`'s tree-level backstop instead, exactly as before this
        # function existed.
        if len(literals) != 1:
            return None
        value, _ = literals[0]
        if value <= cap:
            return None
        try:
            # `str(value)` is safe and exact for the overwhelmingly common
            # case (a decimal literal, or a short hex/octal/binary one) —
            # but not in general: a short-looking token like `factorial(0x`
            # + `f`*4000 + `)` parses to a plain int with thousands of
            # DECIMAL digits, and `str(value)` itself raises ValueError
            # past Python's int->str conversion limit before the refusal
            # message could even be built.
            value_desc = str(value)
        except ValueError:
            value_desc = f"(~{_approx_decimal_digits(value)} digits)"
        return (f"argument {value_desc} to {func_name}() exceeds the limit "
                f"of {cap}: computing it would take an unbounded amount of "
                "time and memory")
    # kind == "digits"
    total_digits = sum(digits for _, digits in literals)
    if total_digits <= cap:
        return None
    if len(literals) == 1:
        return (f"an argument to {func_name}() has about {total_digits} "
                f"digits, over the limit of {cap}: computing it is not "
                "safely bounded")
    # #326 finding 3 (cross-vendor review, round 9, grok): `total_digits`
    # is the SUM of each literal's own digit count, a safe UPPER BOUND on
    # their product's true digit count (never an under-count, off by at
    # most one over) — not the exact product, which this module never
    # materializes to check. That means a small multiplier can tip an
    # otherwise-legal literal over the cap on the SUM alone even though
    # the true product still fits (`factorint(<25-digit literal>*2)`:
    # 25 + 1 = 26 > 25, refused, though the real product is still exactly
    # 25 or 26 digits either way) — accepted, documented conservatism,
    # not a bug: the message says "the sum of ... digits", explicitly,
    # so a caller sees why a small extra factor was enough to refuse.
    return (f"the sum of the literal digits combined in one argument to "
            f"{func_name}() is about {total_digits}, over the limit of "
            f"{cap}: computing their product is not safely bounded")


def _oversized_call_arg_violation(tokens: list) -> str | None:
    """A call to a name in `_FUNCTION_ARG_CAPS` with an oversized
    argument, if any — driven entirely by that ONE table, "value" kind
    (`_HEAVY_FUNCTIONS`' shape) and "digits" kind (the factoring/root
    families' shape) alike; see the table's own comment for why each
    needs a different COMPARISON, and `_call_arg_cap_violation` for where
    that comparison itself lives.

    #326 finding 1 (cross-vendor review, round 8, Codex): round seven's
    digit-count screen (then two separate, per-family functions) checked
    each NUMBER token in isolation — `factorint(<25-digit literal>*<25-
    digit literal>)` (a ~50-digit PRODUCT, hard to factor) passed clean,
    because EACH literal is individually at or under `MAX_FACTOR_ARG_
    DIGITS` and nothing combined them. This now accounts for every
    literal within the SAME top-level argument together (split on a
    depth-1 comma, so `root(<huge>, 2)`'s index argument is never
    conflated with its first): for a "digits"-kind function, `total_
    digits` sums every literal's digit count in that one argument (an
    upper bound on the digit count of their PRODUCT, always >= the true
    value and off by at most one — safe to over-refuse by, never to
    under-refuse) — SAFE regardless of what actually connects them
    syntactically (`*`, `+`, a nested call's own arguments, ...), since a
    sum is a looser bound than any of those individual operations would
    produce; for a "value"-kind function, only a genuinely single-literal
    argument is compared (see `_call_arg_cap_violation`'s own docstring
    for why combining several literals into one VALUE has no equivalent
    principled meaning).

    #326 finding 3 (cross-vendor review, round 9, grok): being an upper
    BOUND, not the exact product, means the sum can tip a literal over
    the cap by one digit even when the true product would not —
    `factorint(<25-digit literal>*2)` sums to 26 (over `MAX_FACTOR_ARG_
    DIGITS`) and is refused, even though the real product is at most 26
    and possibly still exactly 25 digits. Accepted, documented
    conservatism, not a bug — this module already trades a rare false
    refusal for never materializing the product to check exactly (see
    `_oversized_scientific_literal_violation`'s own docstring for the
    same trade made elsewhere) — and `_call_arg_cap_violation`'s refusal
    message says "the sum of the literal digits", explicitly, so a
    caller who hits this can see why a small extra factor alone was
    enough to refuse.

    Literal-only, same scope and same reasoning as `_heavy_call_violation`:
    a call-shaped COMPUTED argument (`factorint(nextprime(10**2000))`,
    `sqrt(nextprime(10**2000))`) is still NOT caught here — no token-level
    screen can evaluate a nested function call without becoming the
    parser. For a SIMPLE computed argument (`factorint(1463+1)`,
    `factorint(<25-digit literal>+<25-digit literal>)`, no nested call), the
    factoring family (`factorint` and its five siblings) now has a
    tree-level backstop THE-1095 added alongside this token-level one: none
    of the six respects `evaluate=False` on its own (each coerces its
    argument to a concrete int internally regardless — see
    `MAX_FACTOR_ARG_DIGITS`'s own comment, updated for THE-1095), so
    `safe_parse`'s pre-parse now runs through `_deferred_global_dict()`
    instead, which gives all six an inert stand-in for exactly that one
    parse — see that function's docstring and `_numeric_ceiling_scan`'s
    `Function`-node branch, which now genuinely reaches these six names
    for a resolvable numeric argument. The NESTED-call shape named at the
    top of this paragraph is different and NOT closed by that fix: the
    inner call (`nextprime(10**2000)`) is ALSO a deferred stand-in inside
    the SAME pre-parse, so it stays an unresolved symbolic `Function`
    node — `_numeric_ceiling_scan`'s own per-argument resolution check
    (`arg_resolved`) treats an unresolved argument as inconclusive, not a
    violation, and moves on — so this shape still relies on `guarded_call`'s
    CPU/wall-clock backstop alone once the REAL (non-deferred) step-3 parse
    runs, same as before THE-1095, just one parse step later than it did
    previously (the deferred pre-parse no longer hangs on it; the real
    evaluated parse still can — `root(factorial(1463), 2)`, a NESTED
    heavy call as root's own value argument, is the identical shape for
    the SAME reason: `_deferred_factorial(1463)` stays an unresolved
    `Function` node to the scan, at-cap-not-over on its own, so `root`'s
    NEW tree-level backstop below has nothing concrete to bound and the
    real, sometimes-expensive `root` construction still runs once the
    real-dict parse is reached — not a regression, the identical cost
    main already paid, just relocated the same one parse step later).
    `sqrt`/`cbrt` are different: they DO respect `evaluate=False` (stay an
    unevaluated `Pow`), so THEIR computed-argument gap is closed at the
    tree level instead, in `reject_explosive`'s Pow loop — see its own
    comment on the unit-fraction-exponent branch. `root` does NOT share
    that path for every call shape — probed live (and pinned in
    `tests/test_bug_sweep.py`'s round-8 self-check): `root(factorial(
    1463), 2)` parses to a `Mul`, not a `Pow`, because `root`'s own
    construction pulls perfect-square-ish factors out of an ALREADY-
    CONCRETE base eagerly, a different code path from bare `sqrt`/`cbrt`.
    THE-1095 round 2 (`verify-1095`) closes the DIRECT (non-nested) half
    of that gap instead: `root` is now ALSO a deferred stand-in (see
    `_DEFERRED_STANDIN_NAMES`'s own comment), so a genuinely computed,
    digit-heavy VALUE argument with no nested heavy call in it
    (`root(10**2000, 3)`) is bound at the tree level, same as every other
    deferred name's first argument (`_numeric_ceiling_scan`'s `Function`-
    node branch) — this TOKEN-level layer stays the ONLY protection for
    the combined-literal shape (`root(<huge literal>*<huge literal>, n)`)
    and the nested-heavy-call shape just described, same as the factoring
    family, not a full replacement for either.
    """
    for i, tok in enumerate(tokens):
        if tok.type != tokenize.NAME or tok.string not in _FUNCTION_ARG_CAPS:
            continue
        if i + 1 >= len(tokens) or tokens[i + 1].string != "(":
            continue  # a bare mention like `sqrt` as a symbol, not a call
        # THE-1095 round 3: per-ARGUMENT-POSITION now, via `_position_spec`
        # — `nextprime`'s own position 1 (`ith`, a "value"-kind iteration
        # count) is a DIFFERENT kind from its position 0 (`n`, "digits");
        # a single `(kind, cap)` for the whole call could never express
        # that. A position with no spec (`_position_spec` returns `None`)
        # is simply never checked — same as before this round for every
        # name that only ever had one bounded position.
        position = 0
        depth = 0
        arg_literals: list = []
        for nxt in tokens[i + 1:]:
            if nxt.type == tokenize.OP and nxt.string == "(":
                depth += 1
                continue
            if nxt.type == tokenize.OP and nxt.string == ")":
                depth -= 1
                if depth == 0:
                    spec = _position_spec(tok.string, position)
                    if spec:
                        violation = _call_arg_cap_violation(tok.string, spec[0], spec[1], arg_literals)
                        if violation:
                            return violation
                    break
                continue
            if nxt.type == tokenize.OP and nxt.string == "," and depth == 1:
                spec = _position_spec(tok.string, position)
                if spec:
                    violation = _call_arg_cap_violation(tok.string, spec[0], spec[1], arg_literals)
                    if violation:
                        return violation
                arg_literals = []
                position += 1
                continue
            if nxt.type == tokenize.NUMBER:
                try:
                    # base 0, not base 10 — same hex/octal/binary reasoning
                    # as `_heavy_call_violation` just above.
                    value = int(nxt.string, 0)
                except ValueError:
                    continue  # a float or complex literal — not this hazard
                exact = _exact_decimal_digit_count(nxt.string)
                digits = exact if exact is not None else _approx_decimal_digits(value)
                arg_literals.append((value, digits))
    return None


#: Matches the exponent of a scientific-notation numeric literal
#: (`1e100000`, `1.5E4001`, `1e-100000`, `1e1_000` with a PEP-515
#: underscore, `1e100000j`/`1e100000J` with Python's imaginary suffix) as
#: `tokenize.NUMBER` hands the token back — sign and underscores included,
#: an optional trailing `j`/`J` allowed after the digits, anchored to the
#: end of the token string so it cannot match an exponent-shaped substring
#: inside a longer token by accident. Round-7 finding 3 (cross-vendor
#: review, Codex): the original pattern had no `[jJ]?` before the `$`, so
#: `1e1000000j` (a valid Python imaginary literal) never matched at all —
#: the trailing `j` put the exponent digits one character short of the end
#: — and reached the real parse unbounded (>1s measured).
_SCI_NOTATION_EXPONENT_RE = re.compile(r"[eE]([+-]?[0-9](?:_?[0-9])*)[jJ]?$")


def _oversized_scientific_literal_violation(tokens: list) -> str | None:
    """A scientific-notation numeric literal whose EXPONENT magnitude alone
    already exceeds `MAX_NUMERIC_DIGITS`, if any.

    #326 finding 3 (cross-vendor review, round 6): a `Float` is declared
    unconditionally safe once parsed (`_log10_num_den`'s own Float branch —
    SymPy prints one at fixed precision regardless of magnitude, so it never
    routes through CPython's int->str ceiling) — true of the RESULT, but
    said nothing about the COST of PARSING one. `1e100000` (nine characters)
    measured ~725ms just to construct the `evaluate=False` shape; `1e1000000`
    ran past 30s. Every other numeric literal this module screens is
    length-bounded by the 2000-char expression cap doing double duty as a
    magnitude cap — a 1990-digit bare integer literal cannot itself exceed
    ~1990 digits — but scientific notation is EXACTLY the escape from that:
    a short token can name an arbitrarily large exponent, decoupling text
    length from magnitude the same way this module's other screens
    (`_heavy_call_violation`'s own hex/octal/binary literal comment,
    `_bounded_numeric_value`'s bit-length checks elsewhere) already exist to
    prevent for other shapes.

    Checked at the TOKEN level, before `parse_expr` ever runs — the
    exponent's magnitude is read directly off the token text via regex, not
    by asking SymPy (or even Python's own `float()`, which is fast here —
    the cost is inside SymPy's own arbitrary-precision `Float` construction,
    not CPython's). `abs(exponent) > MAX_NUMERIC_DIGITS` is the same
    threshold every other ceiling in this module uses, deliberately: not
    tuned to the exact measured performance cliff (`1.5E4001`, only one over
    the cap, parses in ~1ms — fast today, but this is a hard boundary, not
    a moving one that would need re-measuring if SymPy's Float construction
    cost profile ever changes), and NEGATIVE exponents are bounded the same
    as positive ones (`1e-100000` measured ~130ms — smaller than the
    positive case but still well outside "cheap token check" territory, and
    there is no principled reason an astronomically small Float would be
    cheaper to construct than an astronomically large one).
    """
    for tok in tokens:
        if tok.type != tokenize.NUMBER:
            continue
        text = tok.string
        # Round-7 finding 3 (cross-vendor review, Codex): a hex/octal/binary
        # integer literal (`0x1e100000`) can contain a literal `e`/`E` as an
        # ordinary DIGIT in that base, not a scientific-notation exponent
        # marker — Python's int-literal grammar has no exponent syntax in
        # any of those bases, so `0x1e100000` was being misread as `1e100000`
        # with the leading `0x1` discarded and refused on a fabricated
        # "exponent of 100000" that the literal does not actually have (its
        # real value, ~5*10**8, is an ordinary 9-digit integer). Excluded
        # here rather than tightened in the regex itself, since none of
        # these prefixes can ever legitimately contain an exponent suffix.
        if len(text) >= 2 and text[0] == "0" and text[1] in "xXoObB":
            continue
        match = _SCI_NOTATION_EXPONENT_RE.search(text)
        if not match:
            continue
        try:
            exponent = int(match.group(1).replace("_", ""))
        except ValueError:
            continue  # unreachable for a token tokenize.NUMBER already validated
        if abs(exponent) > MAX_NUMERIC_DIGITS:
            return (f"the exponent of {tok.string!r} has a magnitude of "
                    f"{abs(exponent)}, over the limit of {MAX_NUMERIC_DIGITS}: "
                    "constructing it is not safely bounded")
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
    # #326 finding 1 (round 8, Codex): the factoring/root families are a
    # DIFFERENT hazard shape from `_HEAVY_FUNCTIONS` above (a digit-COUNT
    # cap, not a digit-VALUE one) — driven by the SAME `_FUNCTION_ARG_
    # CAPS` table `_heavy_call_violation` reads from, though the
    # comparison itself lives in a separate function; see its own
    # docstring for why.
    violation = _oversized_call_arg_violation(tokens)
    if violation:
        return (CATEGORY_CEILING, violation)
    # #326 finding 3 (round 6): a scientific-notation literal is a
    # parse-time cost bomb this module's other length-bounds-magnitude
    # assumption does not cover — see `_oversized_scientific_literal_
    # violation`'s own docstring. Checked here, at the token level, for
    # the same reason `_heavy_call_violation` is: before `parse_expr`
    # ever runs.
    violation = _oversized_scientific_literal_violation(tokens)
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

#: THE-1095 round-3-follow-up #3 (coordinator review of 5e2a961, grok
#: `verify-1095-r5-grok.log`): rounds 3 and 4 both intercepted `%`/`//`/
#: `<<`/`>>` AFTER Python had already dispatched them — round 3's mixin
#: dunders, round 4's `_op_priority` trick — so interception depended on
#: which operand shape happened to reach which method. `-bell(1463) % 7`
#: (unary minus first, so `%`'s left operand is a `Mul`, not the
#: stand-in), `(bell(1463)+1) % 7`, `2*bell(1463) % 7` (same shape) all
#: bypassed the mixin: SymPy's own `Expr.__mod__`/`Integer.__floordiv__`
#: dispatch to a REAL, eager `Mod`/`floor` the instant BOTH operands are
#: already concrete SymPy objects, never asking the mixin at all.
#:
#: Moved to the AST stage instead, where operand SHAPE cannot matter:
#: `_deferred_transformer_class()` lazily builds an `EvaluateFalseTransformer`
#: subclass whose `visit_BinOp` maps `ast.Mod`/`ast.FloorDiv`/`ast.LShift`/
#: `ast.RShift` to CALLS of the marker constructors below — SymPy's own
#: `visit_BinOp` (confirmed reading its 1.14.0 source) does not even have
#: those four in its `operators` dict, so it returns the node COMPLETELY
#: UNVISITED, meaning it never even visits the node's own CHILDREN either
#: (`ast.NodeTransformer.generic_visit` is what would normally do that,
#: and SymPy's own implementation short-circuits before ever calling it
#: for these four). This subclass visits both children explicitly, so a
#: **nested** unmapped operator, or a heavy call anywhere inside either
#: side, gets the exact same `evaluate=False` treatment `Add`/`Mul`/`Pow`
#: already have. `_parse_deferred` (below) runs the same three-step
#: pipeline `parse_expr(..., evaluate=False)` itself uses internally
#: (`stringify_expr` -> a transformer -> `compile` -> `eval_expr`, all
#: public names in `sympy.parsing.sympy_parser`, verified against the
#: installed sympy 1.14.0's own source before writing this), substituting
#: this subclass for the one it hardcodes. Built LAZILY, like every other
#: sympy-touching class in this module (`_DEFERRED_STANDINS`, ...) — this
#: module imports no part of sympy at module load time.
_DEFERRED_TRANSFORMER_CLASS = None


def _deferred_transformer_class():
    global _DEFERRED_TRANSFORMER_CLASS
    if _DEFERRED_TRANSFORMER_CLASS is None:
        import ast

        from sympy.parsing.sympy_parser import EvaluateFalseTransformer

        marker_ops = {
            ast.Mod: "_DeferredMod",
            ast.FloorDiv: "_DeferredFloorDiv",
            ast.LShift: "_DeferredLShift",
            ast.RShift: "_DeferredRShift",
        }

        def visit_BinOp(self, node):
            marker_name = marker_ops.get(node.op.__class__)
            if marker_name is None:
                return EvaluateFalseTransformer.visit_BinOp(self, node)
            # VISIT THE CHILDREN — the one line SymPy's own version skips
            # for these four operators, since it returns `node` before
            # ever reaching `generic_visit`. Recursing here is what makes
            # a NESTED unmapped operator (`x**N % 11`'s own `x**N`) and a
            # heavy call on either side both come out `evaluate=False`-
            # protected, same as every other operator already gets.
            left = self.visit(node.left)
            right = self.visit(node.right)
            return ast.Call(
                func=ast.Name(id=marker_name, ctx=ast.Load()),
                args=[left, right],
                keywords=[],
            )

        _DEFERRED_TRANSFORMER_CLASS = type(
            "_DeferredEvaluateFalseTransformer", (EvaluateFalseTransformer,),
            {"visit_BinOp": visit_BinOp},
        )
    return _DEFERRED_TRANSFORMER_CLASS


def _parse_deferred(expression: str, *, local_dict: dict | None, global_dict: dict,
                     transformations: tuple):
    """`parse_expr(expression, local_dict=local_dict, global_dict=global_dict,
    transformations=transformations, evaluate=False)`'s own THREE steps
    (`stringify_expr` -> a transformer -> `compile` -> `eval_expr`),
    replicated here ONLY to substitute `_deferred_transformer_class()` for
    the transformer SymPy's own `evaluateFalse()` hardcodes — every other
    line matches `parse_expr`'s own source (sympy 1.14.0) exactly,
    including the `null`-marked `local_dict` restoration on both the
    success and exception paths, so a differential test can compare this
    function's own output against `parse_expr(..., evaluate=False)`
    directly for any expression with none of the four operators this
    function treats specially (see `tests/test_bug_sweep.py`'s own
    corpus for that comparison).
    """
    import ast

    from sympy.parsing.sympy_parser import eval_expr, null, stringify_expr

    if local_dict is None:
        local_dict = {}
    code_str = stringify_expr(expression, local_dict, global_dict, transformations)
    tree = ast.parse(code_str)
    transformed = _deferred_transformer_class()().visit(tree)
    transformed_expr = ast.Expression(transformed.body[0].value)
    ast.fix_missing_locations(transformed_expr)
    code = compile(transformed_expr, "<string>", "eval")
    try:
        rv = eval_expr(code, local_dict, global_dict)
        for i in local_dict.pop(null, ()):
            local_dict[i] = null
        return rv
    except Exception as exc:
        for i in local_dict.pop(null, ()):
            local_dict[i] = null
        raise exc from ValueError(f"Error from _parse_deferred with transformed code: {code!r}")


#: name -> the marker `Function` subclass `_parse_deferred`'s own AST
#: transform constructs a CALL to (`_deferred_transformer_class()`'s own
#: `marker_ops` maps an operator to this same NAME) — a plain `Function`
#: subclass with no `eval()`, exactly like every other deferred stand-in,
#: so it is always left exactly as constructed. `None` until first built
#: (same lazy pattern as `_DEFERRED_STANDINS` below).
_DEFERRED_BINOP_CLASSES: dict | None = None


def _deferred_binop_classes() -> dict:
    global _DEFERRED_BINOP_CLASSES
    if _DEFERRED_BINOP_CLASSES is None:
        from sympy import Function

        _DEFERRED_BINOP_CLASSES = {
            class_name: type(class_name, (Function,), {"nargs": (2,)})
            for class_name in ("_DeferredLShift", "_DeferredRShift",
                                "_DeferredMod", "_DeferredFloorDiv")
        }
    return _DEFERRED_BINOP_CLASSES



#: Built once (like `_MATH_TRANSFORMS`), not once per parse: the classes
#: themselves are stateless and immutable, only the returned `dict` in
#: `_deferred_global_dict()` below is fresh per call (parse_expr's own
#: `local_dict`/`global_dict` contract mutates it — see `null` handling in
#: `parse_expr`'s source — so sharing the dict ITSELF across calls, not just
#: the classes populating it, would be the hazard, same reasoning as
#: `safe_global_dict()`'s own fresh-dict-per-call shape).
_DEFERRED_STANDINS: dict | None = None


def _arity_checked_new(real_obj):
    """`__new__` for a plain-callable deferred stand-in that raises the
    REAL callable's OWN native Python `TypeError` for a wrong argument
    count — see `_deferred_global_dict`'s own comment for why this
    exists (byte-identical arity-error text vs `origin/main`, not
    `Function.__new__`'s own generic "X takes exactly N arguments"
    wording).

    Coordinator review of 06272aa: the PREVIOUS version of this function
    built a dummy Python function via `compile()` + `types.FunctionType`
    purely to reuse Python's own call-binding error text — generating
    code objects at IMPORT TIME to mimic an error message is the wrong
    tool, and sits one step from the `exec`/`eval` invariant this module
    exists to guard, even though it never actually ran anything dynamic.

    Simpler, and using the REAL callable itself rather than a look-alike:
    `inspect.signature(real_obj).bind(*args, **kwargs)` raises the SAME
    `TypeError` Python's own call protocol would for a wrong argument
    count. On that failure, calling `real_obj(*args, **kwargs)` raises
    the exact NATIVE `TypeError` a real call would — byte-identical, not
    a look-alike — because CPython binds arguments to a callee's frame
    BEFORE executing any of its bytecode: the real function's own BODY
    never runs, regardless of what `args`/`kwargs` actually contain (an
    unevaluated SymPy expression included), since the call never gets
    that far. On a SUCCESSFUL `bind()` (arity is fine), `real_obj` is
    never touched at all — this only ever runs on the WRONG-arity path.
    """
    import inspect

    def __new__(cls, *args, **kwargs):
        try:
            inspect.signature(real_obj).bind(*args, **kwargs)
        except TypeError:
            real_obj(*args, **kwargs)  # raises the native TypeError; never reached otherwise
        from sympy import Function

        return Function.__new__(cls, *args, **kwargs)

    return __new__


def _deferred_global_dict() -> dict:
    """`safe_global_dict()`, with every name in `_DEFERRED_STANDIN_NAMES`
    (== `_FUNCTION_ARG_CAPS` minus the `sqrt`/`cbrt` family — see that
    constant's own comment for which names and why) replaced by an inert
    stand-in: a `sympy.Function` subclass named after the table entry, with
    no `eval()` classmethod, so `Function.__new__` has nothing to call and
    the node it builds is ALWAYS left as-is — a `Function` node whose class
    name is exactly the caller's own spelling and whose `.args` are exactly
    the arguments the caller wrote, at the POSITIONS the caller wrote them,
    regardless of what the real function's own `eval()` would have done with
    them (see `_DEFERRED_STANDIN_NAMES`'s own comment for the different
    ways the real functions fail to give `_numeric_ceiling_scan` that shape
    on their own).

    THE-1095 round 2 (`verify-1095`, both cross-vendor reviewers, Low/Medium):
    a bare `Function` subclass with no `nargs` set accepts ANY number of
    arguments — real SymPy classes do not: `binomial`/`rf`/`ff` (`nargs`
    of `{2}`) raise `TypeError` from `Function.__new__` itself for a wrong
    argument count, UNCONDITIONALLY, regardless of `evaluate=False` (this is
    a structural check, not part of `eval()`, which a stand-in never even
    defines) — `binomial(1463+1)` (one arg, missing `k`) is refused on
    `origin/main` with that `TypeError`, not a ceiling. A stand-in with no
    arity of its own would happily build `standin_binomial(1464)`, the scan
    would see an in-position-0, over-cap argument, and refuse it as a
    CEILING instead — a genuine, `origin/main`-observable behavior change
    for a malformed call, not merely a cosmetic difference. Every name that
    IS a real `sympy.Function` subclass exposes its own `nargs` (a
    `FiniteSet`, confirmed live for every member of the table); copied onto
    the stand-in (`is_sequence` needs a plain container, not a `FiniteSet`
    itself — `tuple(real.nargs)` is what `FunctionClass.__init__` actually
    accepts, checked against sympy 1.14.0's source), it raises the
    IDENTICAL message, with the caller's own spelling in place of the real
    class name where those differ (`rf` vs `RisingFactorial`) — itself
    consistent with this module never surfacing an internal SymPy class
    name to begin with.

    THE-1095 round 3 (`verify-1095-r2`, both reviewers, finding 3): the
    eight names that are plain Python callables, not `sympy.Function`
    subclasses (`primorial`, `prime`, `npartitions`, `factorint`,
    `primefactors`, `divisors`, `nextprime`, `isprime`) — plus `root`,
    also plain — used to be left WITHOUT any `nargs` of their own, on the
    reasoning that "their arity is validated the same way it already is
    on `origin/main` ... once the deferred scan is clean". That assumed
    the deferred scan always REACHES a clean verdict before the real-dict
    parse's own `TypeError` gets a chance to run. It does not:
    `isprime(10**26, 1)` (two args; `isprime` takes exactly one) has an
    over-cap FIRST argument (`10**26`, 27 digits, over `MAX_FACTOR_ARG_
    DIGITS`), so the arity-less stand-in happily accepted both, the scan
    refused it as a CEILING on that argument, and the real-dict parse —
    which would have raised `origin/main`'s own `TypeError` for the wrong
    arg count first — never ran at all. `inspect.signature` on the REAL
    callable, derived once here rather than hand-maintained, gives the
    honest answer instead: every POSITIONAL-capable parameter with no
    default is required, and each one WITH a default extends the valid
    count upward by one (`nextprime(n, ith=1)` -> valid counts `{1, 2}`;
    `root(arg, n, k=0, evaluate=None)` -> `{2, 3, 4}`) — `tuple(range(
    required, maximum + 1))` is exactly the same SHAPE `FunctionClass.
    __init__` already accepts for a real `sympy.Function`'s `nargs` (see
    the paragraph above), so applying it needs no new machinery, only the
    derivation. A signature this cannot characterise this way (a bare
    `*args`) is left without `nargs`, same as before this round — none of
    the names in this table have one today, verified live, but a FUTURE
    one added without a finite arity would correctly fall back to
    "accepts anything" rather than raise while deriving it.

    Used ONLY for `safe_parse`'s pre-parse step (the `evaluate=False` parse
    that exists purely to build a shape for `reject_explosive` to inspect
    before anything real runs) — never for the actual evaluated result: once
    that scan passes, `safe_parse` reparses through the REAL `safe_global_
    dict()` to compute (or return the genuinely unevaluated shape of) the
    caller's expression, so a deferred stand-in never reaches a caller's
    result or an error message. `classify_unsafe`'s deny-listed FUNCTION
    node type never surfaces to a user for the same reason `sqrt`/`cbrt`
    are excluded from this substitution at all: nothing downstream of the
    scan ever sees one.
    """
    global _DEFERRED_STANDINS
    if _DEFERRED_STANDINS is None:
        import inspect

        from sympy import Function

        real = safe_global_dict()
        _DEFERRED_STANDINS = {}
        for name in _DEFERRED_STANDIN_NAMES:
            real_obj = real.get(name)
            real_nargs = getattr(real_obj, "nargs", None)
            if real_nargs is not None:
                attrs = {"nargs": tuple(real_nargs)}
            else:
                attrs = {}
                try:
                    sig = inspect.signature(real_obj)
                except (TypeError, ValueError):
                    sig = None
                if sig is not None:
                    # THE-1095 round-3-follow-up #3 (coordinator review
                    # of 5e2a961, grok item 5): a plain-callable stand-in
                    # used to set `nargs` too, giving `Function.__new__`'s
                    # OWN generic arity check ("X takes exactly N
                    # arguments (M given)") — SymPy's own wording, not
                    # the REAL callable's: `isprime(10**26, 1)` on
                    # `origin/main` raises Python's own native "isprime()
                    # takes 1 positional argument but 2 were given" (a
                    # plain function, not a `Function.__new__`-checked
                    # class). `_arity_checked_new`'s own `__new__` tries
                    # `inspect.signature(real_obj).bind(*args, **kwargs)`
                    # first (cheap, `sig` here only gates WHETHER this
                    # path applies at all — the real check happens fresh,
                    # against `real_obj`, on every call), falling through
                    # to a REAL call to `real_obj` only on a `bind()`
                    # failure — Python's own call-binding machinery is
                    # what then raises the byte-identical `TypeError`,
                    # since it is the SAME mechanism a real call to
                    # `isprime` would hit, before any of `isprime`'s own
                    # body ever runs.
                    attrs["__new__"] = _arity_checked_new(real_obj)
            _DEFERRED_STANDINS[name] = type(name, (Function,), attrs)
        # THE-1095 round 3, item 6: `Mod` is directly callable BY NAME
        # (`Mod(x**N, 11)`, not just via the `%` operator
        # `_parse_deferred`'s own transform screens) and its own `eval()`
        # is the exact `gcd()`-based hazard that check exists for — a
        # bare literal `%` rewrite could not fix this even if this module
        # attempted one (`Mod` itself is eager, not just its arguments).
        # Not part of
        # `_FUNCTION_ARG_CAPS` (its hazard is a CONSTRUCTION-cost one, not
        # an argument-magnitude one — there is no "over-cap argument" to
        # name), so it is added directly here instead: inert is all it
        # needs, since a `Pow` ARGUMENT to `Mod` already gets its own
        # `evaluate=False` protection from `visit_Call`'s ordinary
        # recursion (confirmed live), and once `Mod` itself never calls
        # `gcd()`, that protected `Pow` is exactly what `_numeric_ceiling_
        # scan`'s own generic descent (pushing every `Function` node's
        # `.args`) finds and correctly bounds moments later in the same
        # scan.
        _DEFERRED_STANDINS["Mod"] = type("Mod", (Function,), {"nargs": (2,)})
    g = safe_global_dict()
    g.update(_DEFERRED_STANDINS)
    # THE-1095 round-3-follow-up #3: the marker classes `_parse_deferred`'s
    # own AST transform constructs CALLS to (`_DeferredLShift`, ...) must
    # be resolvable names in the `global_dict` that same call is `eval`'d
    # against — see `_deferred_transformer_class`'s own module-level
    # comment for why these are built at the AST stage now, not via
    # operator dunders on the stand-ins above.
    g.update(_deferred_binop_classes())
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

#: THE-1095 round-3-follow-up #3 (coordinator review of 5e2a961, grok):
#: the token-level pre-check that used to live here (`_unprotected_
#: operator_violation`) is GONE -- `_parse_deferred` (above) now
#: intercepts `%`/`//`/`<<`/`>>` at the AST stage, before SymPy's own
#: eager dispatch ever runs, so no token-level guess at "is the nearby
#: exponent related to this operator" is needed anymore -- see that
#: function's own docstring, and `_deferred_transformer_class`'s
#: module-level comment, for the replacement. Still used by
#: `_expression_touches_table_or_unprotected_operator` below, to decide
#: whether a deferred-parse exception is safe to treat as inconclusive.
_UNPROTECTED_OPERATORS = frozenset({"%", "//", "<<", ">>"})


def _expression_touches_table_or_unprotected_operator(expression: str) -> bool:
    """True if `expression` contains a NAME token that is a key of
    `_FUNCTION_ARG_CAPS` (a table name this module screens specially) or
    an OP token in `_UNPROTECTED_OPERATORS` — used ONLY to decide whether
    a FAILED deferred pre-parse (`scan_shape` in `safe_parse`) may safely
    treat its own exception as merely "inconclusive" (an ordinary syntax
    error, unrelated to anything this module bounds, that the real-dict
    parse will raise identically) or must instead refuse immediately, on
    its OWN exception text, rather than risk falling through to a real
    parse THE-1095 round 2 established must never run un-vetted — see
    that call site's own comment for the full reasoning, designed
    together with the ClusterFuzzLite crash fix in `reject_explosive`
    (the two are the same "an unresolved exception must never silently
    mean 'safe'" principle, applied at the two different places an
    exception from this module's own scanning can occur).
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(expression).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError,
            UnicodeEncodeError, UnicodeDecodeError):
        return False  # not this check's job -- classify_unsafe already ran
    for tok in tokens:
        if tok.type == tokenize.NAME and tok.string in _FUNCTION_ARG_CAPS:
            return True
        if tok.type == tokenize.OP and tok.string in _UNPROTECTED_OPERATORS:
            return True
    return False


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
      2. `parse_expr(..., evaluate=False)` through `_deferred_global_dict()`
         (`scan_shape`, inert stand-ins — see that function's own docstring
         and `_DEFERRED_STANDIN_NAMES`'s), fed to `reject_explosive`, FIRST
         and ALONE. THE-1095 round 2 (`verify-1095`, both cross-vendor
         reviewers, the central finding): this MUST run, and refuse on a
         hit, before anything else — a heavy call whose real implementation
         evaluates for real regardless of `evaluate=False` (`rf`/`ff`/
         `primepi`/`binomial`/`nextprime`/`divisors`/`primefactors`) does
         that eager, potentially unbounded work the moment ANY parse
         through the REAL dict touches it, even one whose own tokens are
         all individually under cap (`bell(1463)+factorial(1463+1)`
         measured 6.77s through the real dict alone, 0.0048s through
         `scan_shape` — same refusal, one thousand times faster; a
         COMPUTED, digit-heavy argument like `nextprime(10**2000)` is worse
         still, unbounded). Running a real-dict parse FIRST — round 1's
         mistake — meant the cost this module exists to bound already
         happened by the time anything could refuse it.
      3. Only once step 2 is clean: a second `evaluate=False` pre-parse
         through `safe_global_dict()` (`shape`, the REAL functions), ALSO
         fed to `reject_explosive`. Still needed — not redundant with step
         2 — because existing checks depend on a literal heavy-function
         argument evaluating for real DURING this parse:
         `sqrt(factorial(1463))`'s inner `factorial` call,
         `factorial(1463)*x`'s own coefficient, .... `shape`'s own PARSE
         can itself raise here — `TypeError` for a genuine arity mismatch
         (`binomial(1463+1)`, missing `k` — SymPy's own `Function.__new__`
         validates `nargs` unconditionally, so this is IDENTICAL to
         `origin/main`'s own refusal, not new), or `ValueError` for the
         "group B" eager `int`-coercion class on a MERELY computed (not
         itself oversized) argument (`primorial`/`prime`/`motzkin`/
         `isprime`/`factorint`, confirmed already safe by step 2's clean
         scan) — see the branching just below the parse for exactly which
         exception proceeds to step 4 and which is returned immediately.
      4. A third, real parse — through `safe_global_dict()`, never SymPy's
         default builtins-populated dict AND never `_deferred_global_dict()`'s
         stand-ins either — only if step 3 passed (or its own exception was
         the group-B class) and the caller wants the evaluated value (or,
         for a `evaluate=False` caller, the genuinely unevaluated real
         shape — see below; often just `shape` from step 3, reused rather
         than reparsed).

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

    `evaluate=False` returns the unevaluated `shape` from step 3 (the REAL
    one, reused rather than reparsed when it parsed cleanly) — the caller
    wants the tree, not a value (matches the original `sp.sympify(raw,
    evaluate=False)` no-`=` path in `logic._solve_linear`, which feeds this
    straight into `sp.solve` — it needs the REAL `factorial`/`rf`/...
    semantics, never THE-1095's inert scanning stand-ins). If step 3 itself
    raised the group-B `ValueError` class, `shape` cannot be reconstructed
    at all under `evaluate=False` — the real function's own eager `int`
    coercion fails identically on ANY retry, since only `evaluate=True`
    ever turns the caller's computed argument into the concrete `Integer`
    that coercion needs — so this one narrow combination (an
    `evaluate=False` caller, a group-B name, a genuinely computed argument)
    still surfaces that `ValueError` as its `CATEGORY_VALIDATION` result,
    identical to its behavior before THE-1095 and to `origin/main`'s;
    unexercised by any caller today (`logic._solve_linear` is the only
    `evaluate=False` caller, and only for a piece with no `=`), documented
    here rather than silently worked around with a fabricated shape.
    """
    cls = classify_unsafe(expression)
    if cls:
        return None, cls
    from sympy.parsing.sympy_parser import parse_expr

    # THE-1095 round 2: this MUST be the only parse that runs before a
    # ceiling violation can refuse — see this function's own docstring,
    # step 2, for the measured cost of getting the order wrong.
    #
    # THE-1095 round-3-follow-up #3 (coordinator review of 5e2a961, grok):
    # `_parse_deferred`, not `parse_expr` — see that function's own
    # docstring, and `_deferred_transformer_class`'s module-level comment,
    # for why `%`/`//`/`<<`/`>>` need their OWN transformer rather than a
    # token-level pre-check (the deleted `_unprotected_operator_
    # violation`): a token screen can only ever see operand shapes that
    # happen to sit next to the operator in the SOURCE TEXT, and SymPy's
    # own eager dispatch for these four operators does not care about
    # source position at all.
    try:
        scan_shape = _parse_deferred(expression, local_dict=local_dict,
                                     global_dict=_deferred_global_dict(),
                                     transformations=math_transforms())
    except Exception as exc:
        # THE-1095 round-3-follow-up (grok item C(ii), then grok's OWN
        # follow-up review of 949aac9 — `verify-1095-r4-grok.log` — found
        # the TypeError carve-out this comment used to describe was
        # ITSELF the recurring structural gap, the same door left open
        # through rounds 3 and 4): "the deferred parse raised" is safely
        # inconclusive ONLY when nothing in the expression could have
        # triggered the hazards this pre-parse exists to catch — an
        # ordinary syntax error (unbalanced parens, a stray token, ...)
        # with no table name and no unprotected operator anywhere just
        # falls through, and the real-dict parse below raises the
        # identical error for the caller to see. An expression that DOES
        # touch a table name or an unprotected operator, and STILL made
        # the ALL-INERT-stand-in parse itself raise before there was even
        # a tree to hand `reject_explosive`, is now UNCONDITIONALLY a
        # refusal — no exception, not even `TypeError`. The carve-all for
        # `TypeError` used to exist because a deferred stand-in had no
        # `__lshift__`/`__mod__`/`__floordiv__`/`__rshift__` of its own,
        # so `factorial(20) << 3` raised `TypeError` from Python's own
        # operator dispatch for ANY argument, dangerous or not — but
        # every deferred stand-in now implements all eight operator-
        # protocol dunders (`_DeferredOperatorMixin`, above), returning an
        # inert marker node instead of ever raising for these four
        # operators, so that specific `TypeError` simply does not happen
        # anymore. A genuine arity `TypeError` (`binomial(5)`, missing
        # `k`) still reaches here exactly as before, and is now refused
        # HERE, immediately, on the deferred stand-in's own text — which
        # is IDENTICAL to `origin/main`'s own text for the same malformed
        # call (the stand-in shares the real class's own name and
        # `nargs`), so no observable behavior changes for that case
        # either, confirmed by the arity tests in `tests/test_bug_sweep.
        # py`. Fail closed on everything else: `unknown != safe`.
        scan_shape = None
        if _expression_touches_table_or_unprotected_operator(expression):
            return None, (CATEGORY_VALIDATION, f"parse error: {exc}")
    else:
        # THE-1095 round-3-follow-up (coordinator review of d3c65c7): a
        # bare class reference is a malformed EXPRESSION (validation), not
        # an oversized one (ceiling) — checked separately from, and before,
        # `reject_explosive`'s own numeric-ceiling scan; see `_bare_class_
        # violation`'s own docstring for why this lives outside that
        # function rather than bending its "always a ceiling finding"
        # contract.
        bare_class = _bare_class_violation(scan_shape)
        if bare_class:
            return None, (CATEGORY_VALIDATION, bare_class)
        explosive = reject_explosive(scan_shape)
        if explosive:
            return None, (CATEGORY_CEILING, explosive)

    # Only reached once `scan_shape` is either clean or itself failed to
    # parse (a genuine, non-table-related syntax issue — classify_unsafe
    # already screens the common ones, but is a denylist, not a grammar).
    # `shape` (real functions) is what step 4 below reuses, and what
    # `sqrt(factorial(1463))`-style checks depend on (see this function's
    # own docstring, step 3).
    real_exc: Exception | None = None
    try:
        shape = parse_expr(expression, transformations=math_transforms(),
                           local_dict=local_dict, global_dict=safe_global_dict(),
                           evaluate=False)
    except Exception as exc:
        # Saved to a plain variable, not the `except ... as exc` binding
        # itself -- Python deletes that name when the except block exits
        # (it would otherwise pin the traceback via a reference cycle), so
        # using `exc` below, past this block, would raise `NameError`.
        shape = None
        real_exc = exc
    else:
        bare_class = _bare_class_violation(shape)
        if bare_class:
            return None, (CATEGORY_VALIDATION, bare_class)
        explosive = reject_explosive(shape)
        if explosive:
            return None, (CATEGORY_CEILING, explosive)

    if shape is None:
        if scan_shape is None:
            # BOTH parses failed: `origin/main`'s own error, verbatim —
            # this is what a caller already saw before THE-1095 existed.
            return None, (CATEGORY_VALIDATION, f"parse error: {real_exc}")
        # THE-1095 round 2 (verify-1095, finding 2): `TypeError` is SymPy's
        # OWN arity/shape validation (`Function.__new__`'s `nargs` check,
        # or a plain Python callable's own argument-count enforcement) —
        # unconditional, regardless of `evaluate=False`, and IDENTICAL to
        # `origin/main`'s refusal for the same malformed call
        # (`binomial(1463+1)`, missing `k`) — never the group-B eager
        # `int`-coercion class (confirmed live: that one is always
        # `ValueError` — `1 + 1463 is not an integer`,
        # `The provided number must be a positive integer`, ... — see
        # `_deferred_global_dict`'s own docstring for why the stand-ins
        # cannot mask this: most either share the real class's own
        # `nargs`, or have none at all and defer to this exact `TypeError`
        # from Python's own call semantics). Returned immediately, exactly
        # like a `TypeError` from `scan_shape` above would have been, had
        # `scan_shape`'s own stand-in shared that arity.
        if isinstance(real_exc, TypeError):
            # Kept separate from the `elif` below deliberately: same
            # RETURN, but two independently-documented conditions (arity
            # vs. message-text narrowing) -- collapsing them into one
            # `or` would bury which reasoning applies to which exception
            # shape.
            return None, (CATEGORY_VALIDATION, f"parse error: {real_exc}")
        # THE-1095 round-3-follow-up (grok, item E): the class this branch
        # is meant to fall through for is narrow and specific — group B's
        # OWN eager `int`-coercion `ValueError`, always phrased "... is not
        # an integer" (`sympy.core.numbers.Integer.__new__`'s own message
        # for a non-`Integer` argument that never got the chance to
        # resolve to a concrete value under `evaluate=False`). Previously
        # this branch fell through for ANY exception other than
        # `TypeError`, on the reasoning that `scan_shape` already proved
        # every table-driven ARGUMENT safe — true, but that says nothing
        # about exceptions unrelated to argument coercion at all:
        # `motzkin`'s own `ValueError` is worded differently
        # ("must be a positive integer") and was never proven to be the
        # int-coercion class rather than, say, a genuine domain violation;
        # `isprime`/`factorint` can also raise `ZeroDivisionError` (a
        # literal `/0` argument) or `RecursionError`/`OverflowError` from
        # deep in their own algorithms — none of those are "safe to retry
        # under evaluate=True", they are `origin/main`'s own refusal for a
        # genuinely bad input and belong right here, immediately, not
        # silently swallowed into a retry that then raises the SAME error
        # a second time from inside the `evaluate=True` parse below (or,
        # worse, a DIFFERENT one). Matching on the message text is the
        # correct fail-closed default is `unknown != safe`: only the
        # EXACT, previously-verified class proceeds; everything else
        # returns `origin/main`'s own text immediately.
        elif not (isinstance(real_exc, ValueError) and "is not an integer" in str(real_exc)):
            return None, (CATEGORY_VALIDATION, f"parse error: {real_exc}")
        # Group B's own eager `int`-coercion `ValueError`: `scan_shape`
        # already parsed AND scanned clean above, so every table-driven
        # argument here is proven safe regardless of why the REAL
        # function's own eager coercion choked on it — proceed to
        # `evaluate=True` below, which supplies the concrete `Integer`
        # that coercion needed and computes correctly.

    if not evaluate:
        if shape is not None:
            return shape, None
        # `shape` raised the group-B `ValueError` above — see this
        # function's own docstring for why it cannot be reconstructed
        # under `evaluate=False` at all, and returns that same error here.
        return None, (CATEGORY_VALIDATION, f"parse error: {real_exc}")
    try:
        value = parse_expr(expression, transformations=math_transforms(),
                           local_dict=local_dict, global_dict=safe_global_dict())
    except Exception as exc:
        return None, (CATEGORY_VALIDATION, f"parse error: {exc}")
    # THE-1095 round 3 (verify-1095-r2, finding 4): no combination of two
    # individually-capped POSITIONS can be proven jointly safe without
    # either a real growth bound (which `_table_function_bound` now
    # supplies for the NESTED case) or a check on the actual result —
    # `rf(5, 1463+1463)` (`k = 2926`, itself over `rf`'s own position-1
    # cap were one not already caught, but even a `k` that slips UNDER
    # every individual cap can still combine into an over-limit RESULT:
    # `(5+2926)**2926` alone is an 8_887-digit integer) evaluates cleanly
    # through every check above and only becomes visibly dangerous once
    # `value` itself exists. `_log10_num_den` resolves a concrete,
    # already-evaluated `value` exactly (no approximation, unlike a
    # GROWTH bound) — this is the DIGIT-COUNT backstop every other path
    # through this function already has, applied to whatever finally
    # comes out, regardless of which table name (or combination) produced
    # it.
    result_log_num, result_log_den, result_resolved = _log10_num_den(value, {})
    if result_resolved:
        output_violation = _ceiling_message_num_den(result_log_num, result_log_den)
        if output_violation:
            return None, (CATEGORY_CEILING, output_violation)
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


def _exp_upper_bound_value(exp_log_num: float, exp_log_den: float) -> float:
    """A plain float upper bound on `|exponent value|`, given only the
    exponent's OWN `(log_num, log_den)` from `_log10_num_den` — itself
    possibly already an upper bound (an `Add` exponent's is), never the
    exponent's exact value. `+inf` when the bound is too large to
    represent as a finite float at all: `float ** float` RAISES
    `OverflowError` past roughly 1e308, so this is checked BEFORE ever
    attempting `10.0 ** exp_log`, not caught after — the same "decide from
    the magnitude, never attempt the operation on a value that might be
    too large" shape `_safe_log10_pow` above uses for the identical
    reason, one level up: here the risk is materializing the EXPONENT's
    approximate size as a float, there it was multiplying by one.

    #326 finding 1 (cross-vendor review, round 5): the Pow branch below
    used to require the exponent be either a bare `Integer` or an exact
    `Mul`/`Pow`-of-`Integer` multiset `_safe_multiset_rational` could
    reduce to one — an `Add` exponent (`2**(14999+1)`, same value as the
    already-refused `2**(30000/2)`) or a genuinely non-integral one whose
    BASE still makes the result a huge integer (`(10**3000)**(3/2)` =
    `10**4500`) fell through as "no huge-integer result to bound" and
    reached real `evaluate=True` evaluation unrefused. Neither assumption
    was needed: the VERDICT ("would `base**exponent` print over cap")
    only ever needed an upper bound on the exponent's MAGNITUDE, which
    `_log10_num_den` already computes for every node type — Add included
    — without ever resolving an exact value.
    """
    exp_log = exp_log_num - exp_log_den
    if not math.isfinite(exp_log) or exp_log > 300:
        return float("inf")
    return 10.0 ** exp_log


def _safe_scale_log10(base_log: float, exp_value_upper: float) -> float:
    """`base_log * exp_value_upper` — an upper bound on the log10
    magnitude of one side (numerator or denominator) of `base**exponent`,
    given the corresponding side of `base`'s own `(log_num, log_den)` and
    an upper bound on `|exponent|` (`_exp_upper_bound_value`, never the
    exact exponent). Safe for `exp_value_upper` that is `+inf` (the
    exponent's own magnitude already too large to represent as a float)
    and for a `base_log` of `0` (a base of magnitude <= 1 on this side
    never grows, however large the exponent — `1 ** anything` stays `1`,
    short-circuited here before ever multiplying by an `inf`, which would
    otherwise produce `nan`, not `0`).

    Deliberately sign-agnostic, same as every other magnitude in this
    module: `exp_value_upper` bounds `|exponent|`, not the signed value,
    so this scales BOTH the numerator and denominator side of `base` by
    the same amount regardless of whether the true exponent is positive
    or negative — a negative exponent's true result has numerator and
    denominator SWAPPED relative to what this computes, but
    `_ceiling_message_num_den` checks both sides independently, so a
    refusal still fires correctly; only the message's "numerator" vs
    "denominator" label could be imprecise for that shape, never the
    refuse/allow verdict itself.
    """
    if base_log <= 0:
        return 0.0
    if not math.isfinite(exp_value_upper):
        return float("inf")
    result = base_log * exp_value_upper
    return result if math.isfinite(result) else float("inf")


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
    `_bounded_numeric_value`, deleted in round 5 once nothing in this
    module called it anymore — blows its own intermediate budget on
    `factorial(1463)*factorial(1463)` before ever seeing the
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
    from sympy import Function, Integer, Mul, Pow

    key = id(node)
    if key in memo_ms:
        return memo_ms[key]
    result = None
    if isinstance(node, Integer):
        n = int(node)
        # `abs(n) == 1` (n is 1 or -1) gets an EMPTY terms dict, not
        # `{1: 1}`: `1 ** anything` is always `1`, a magnitude-neutral
        # element that contributes nothing to `_multiset_log_num_den`
        # either way (`log10(1) == 0`) -- but a spurious `{1: 1}` entry
        # DOES change the RESULT of comparing two multisets for equality
        # (`_cancel_additive_inverses`'s `frozenset(terms.items())` key),
        # which is exactly what broke doing that: `f*f` resolves to
        # `{f: 2}` but `-1*f*f` resolved to `{1: 1, f: 2}` -- two
        # structurally-cancelling terms that no longer compared equal
        # because of this one spurious key from encoding the sign.
        result = ({}, 0) if n == 0 else ({} if abs(n) == 1 else {abs(n): 1}, 1 if n > 0 else -1)
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
    elif isinstance(node, Function) and type(node).__name__ in _FUNCTION_ARG_CAPS:
        # THE-1095 round-3-follow-up (coordinator review of 949aac9, grok
        # item 4's own regression risk): a table-function CALL is a valid
        # multiset "base" too, keyed by the NODE itself rather than a
        # computed value — SymPy's own structural equality/hashing on a
        # `Function` node (its class plus its `.args`) already guarantees
        # two SEPARATE occurrences of the identical call (`factorial(
        # 1463)` written twice) compare equal and hash equal (its own
        # construction cache typically hands back the literal same object
        # too), so `factorial(1463) / factorial(1463)` cancels EXACTLY to
        # 1 via this SAME net-zero-exponent mechanism the `Integer` branch
        # above already uses — WITHOUT this function, or anything it
        # feeds, ever needing to know `factorial(1463)`'s own numeric
        # magnitude at all. Only ever used for EXACT cancellation, never
        # for measuring a SURVIVOR's own magnitude (see the check right
        # below, before this function returns) — `_multiset_log_num_den`'s
        # own contract is "exact", and a table-function call's own
        # digit-count is, at best, a deliberately LOOSE bound (see that
        # function's own docstring for the regression trying to use one
        # here caused). This function stays PURELY structural — it does
        # NOT decide whether a surviving (non-cancelling) table-function
        # key is trustworthy to measure; that decision belongs to each
        # CALLER that treats a returned multiset as final (see `_table_
        # multiset_trusted`, used at both of this function's own top-
        # level call sites), never to this function itself, which is
        # RECURSIVE — a sibling factor elsewhere in the SAME `Mul` may
        # yet cancel a term that looks like a lone survivor from inside
        # one single recursive call (discarding `result` HERE, before the
        # `Mul` branch above ever got a chance to try, was tried first and
        # is exactly why nothing ever cancelled: this branch reaching a
        # bare `factorial(1463)` in ISOLATION discarded before its
        # `Mul` parent could ever combine it against a matching `1/
        # factorial(1463)` sibling).
        result = ({node: 1}, 1)
    memo_ms[key] = result
    return result


def _table_multiset_trusted(multiset) -> bool:
    """`True` if a `_factor_multiset` result is trustworthy for `_multiset_
    log_num_den`'s own EXACT-accounting contract — `False` if a table-
    function key SURVIVED cancellation (a non-zero net exponent; one that
    fully cancels is deleted from `terms` entirely and never reaches
    here). Checked ONLY at `_factor_multiset`'s own top-level call sites,
    never inside that RECURSIVE function itself (tried first, and
    reverted: discarding a lone survivor from inside one single recursive
    call — before its `Mul` parent ever got a chance to combine it
    against a matching inverse elsewhere in the SAME product — is exactly
    why `factorial(1463)/factorial(1463)*factorial(1463)/factorial(1463)`
    never cancelled at all under that version). A table-function call's
    own digit count is, at best, a deliberately LOOSE growth bound — not
    accurate enough to stand in for this function's own "exact" promise
    (see `_multiset_log_num_den`'s own docstring for the regression using
    one here caused) — so a caller sees `False` here as "fall back to
    whatever this module's other checks already do with an unresolved
    node" (the generic per-argument bound, `_pow_table_base_violation`
    for a `Pow` with a table-function base, ...), not as a refusal in
    its own right.
    """
    from sympy import Function

    if multiset is None:
        return False
    terms, _sign = multiset
    return not any(isinstance(base_key, Function) for base_key in terms)


def _multiset_log_num_den(terms: dict) -> tuple:
    """log10(|numerator|), log10(|denominator|) for an exact `_factor_
    multiset` result, via `_safe_log10_pow` per term (never `base**exp`
    itself) — a positive net exponent is a numerator factor, negative is a
    denominator factor, matching how `str()` would actually render the
    reduced fraction.

    Every key here is a plain Python `int` — THE-1095 round-3-follow-up
    (coordinator review of 949aac9, grok item 4): `_factor_multiset` DOES
    also key a table-function `Function` node (for EXACT, structural
    cancellation — see its own comment), but only ever lets that survive
    into a RETURNED result when it fully cancels to net exponent 0, i.e.
    is ABSENT from `terms` by the time this runs — a growth-bound
    APPROXIMATION for a node key that does NOT cancel is not the same
    claim as this function's own contract ("exact"), and treating it as
    one regressed two independent, previously-correct cases: `x*factorial
    (1463)` (single, in-range, no cancellation possible) and `factorial(
    1000)+fibonacci(1000)` (two independent siblings) — both wrongly
    refused, because the deliberately LOOSE `_growth_nn_loose` bound for
    `factorial(1463)` (~9258) is nowhere near its true digit count
    (3998, under the 4000 cap) when treated as if it WERE the exact
    value, rather than a safe-but-loose upper bound for a DIFFERENT
    purpose (deciding whether a NESTED argument is too big to look at
    further, not "how many digits does this print"). See `_pow_table_
    base_violation`'s own docstring for where a table-function base's
    OWN digit-ceiling risk (`bell(1463)**2`) is caught instead — a
    separate, narrower check that never feeds back into this shared,
    EXACT-only accounting.
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


#: #326 finding 5 (cross-vendor review, round 7, Codex): the bound on how
#: large a base/exponent pair may be before `_split_coefficient` folds it
#: into a plain Python int "coefficient" rather than leaving it as part of
#: a term's big/exact identity. `base_key ** exp` for anything within both
#: bounds (at most `1_000 ** 20`, ~10**60) is instant and never itself the
#: ~4000-digit hazard this file exists to catch — a REAL coefficient in an
#: ordinary expression (`2*`, `100*`, `2**10*`) is nowhere near either
#: bound; a heavy-function RESULT like `factorial(1463)` (~3998 digits) is
#: is, by a huge margin, on the base axis alone.
_COEFF_MAX_BASE = 1_000
_COEFF_MAX_EXP = 20


def _split_coefficient(terms: dict) -> tuple:
    """Split a `_factor_multiset` terms dict into `(coeff, rest)`.

    `coeff` is the product (a real Python `int`, always safe and instant to
    compute — see `_COEFF_MAX_BASE`/`_COEFF_MAX_EXP` above) of every
    base/exponent pair small enough on BOTH axes to read as an integer
    multiplier rather than part of the term's big/exact identity; `rest`
    is every other pair, unchanged, still an exact `_factor_multiset`-shape
    dict. Only a POSITIVE exponent on a small base is ever folded in — a
    negative one (`base_key ** -3`, a fraction) is not an integer
    coefficient and stays in `rest` instead, matched only by the existing
    exact-multiset-equality path. `coeff` does NOT carry the term's overall
    sign; the caller (`_cancel_additive_inverses`) already tracks that
    separately, from `_factor_multiset`'s own returned `sign`.

    THE-1095 round-3-follow-up #2 (ClusterFuzzLite, found within the first
    60s of a fresh fuzzing run against 949aac9's own successor): `base_key
    <= _COEFF_MAX_BASE` assumed `base_key` is always a plain Python `int`
    — true before `_factor_multiset` also started keying a table-function
    `Function` node (this round's own item 4 fix, for EXACT cancellation).
    `factorial(cos(y)) <= 1000` (a table call over a SYMBOLIC, non-numeric
    argument) is not a plain `int` comparison at all — it is a SymPy
    `Relational` SymPy itself cannot resolve to a definite truth value
    (`cos(y)`'s own value is unknown), and using it directly in a boolean
    `and`/`if` context raises `TypeError: cannot determine truth value of
    Relational` — uncaught, crashing this function instead of just
    treating the term as "not an integer coefficient" (`rest`, the
    already-correct answer for every OTHER not-small-enough shape this
    function handles). `isinstance(base_key, int)` first, matching how
    `_multiset_log_num_den`'s own docstring already documents every key
    being a plain `int` — a table-function key can never legitimately
    reach the small-enough-to-fold-in branch regardless (see `_growth_nn_
    loose`'s own comment: even `factorial(20)`, tiny by this module's own
    standard, is already `2.4e18`, nowhere near `_COEFF_MAX_BASE`, 1_000)
    — so this guard costs nothing on the ordinary numeric path.
    """
    coeff = 1
    rest: dict = {}
    for base_key, exp in terms.items():
        if (isinstance(base_key, int) and 0 < exp <= _COEFF_MAX_EXP
                and base_key <= _COEFF_MAX_BASE):
            coeff *= base_key**exp
        else:
            rest[base_key] = exp
    return coeff, rest


def _cancel_additive_inverses(args: tuple, memo_ms: dict) -> tuple:
    """`args` (an `Add`'s own direct terms) with groups of terms that sum
    to EXACTLY zero removed entirely, or `args` unchanged if none do.

    #326 finding 4 (cross-vendor review, round 6): `Mul` already cancels a
    repeated base against its own reciprocal structurally
    (`_factor_multiset`'s whole reason to exist), but `Add`'s bound
    (`_log10_num_den`'s own `Add` branch) never recognized that two terms
    could be additive inverses of each other at all — `factorial(1463)*
    factorial(1463) - factorial(1463)*factorial(1463)` is exactly `0`
    (cheap, trivially printable), but the old bound saw two ~7996-digit
    terms and refused on that basis alone, the Add-shaped analogue of the
    exact bug `_factor_multiset` was built to fix for `Mul`.

    Two terms are "the same up to sign" here when `_factor_multiset`
    resolves BOTH to the identical factor multiset with OPPOSITE sign —
    the SAME exact-match bar `_factor_multiset`'s own cancellation already
    holds a `Mul` to (matching by VALUE, via a hashable
    `frozenset(terms.items())` key, never by float comparison). A term
    `_factor_multiset` cannot resolve at all (a free symbol, a `Function`,
    ...) is left untouched, in its original position, exactly as before —
    this only ever REMOVES terms, so a caller that could already fall back
    safely on the unmodified `args` can do the identical thing here.
    Genuinely NON-identical terms that happen to sum to something small
    (`f - (f - 1)`, say) still are not recognized and stay fail-closed —
    documented, not fixed, the same trade `_log10_num_den`'s own `Add`
    docstring already makes for its upper-bound formula in general.

    #326 finding 5 (cross-vendor review, round 7, Codex): a plain sign
    (`+1`/`-1`) match was not the whole story either — `2*f*f - f*f - f*f`
    (exactly `0`) was still refused, because `2*f*f`'s multiset (an extra
    `{2: 1}` entry) and `f*f`'s are DIFFERENT keys, never compared against
    each other at all. Grouping now happens on `_split_coefficient`'s
    `rest` key instead of the raw multiset, with each term's signed
    `coeff` (`_factor_multiset`'s own `sign`, times `_split_coefficient`'s
    extracted integer) summed PER GROUP. Cancellation stays ALL-OR-NOTHING,
    exactly as the docstring above already promises ("this only ever
    REMOVES terms"): a group whose coefficients sum to exactly zero is
    dropped in full; any other net sum leaves EVERY term in that group
    untouched — never replaced by one combined term — so a group that does
    not fully cancel falls through to the unmodified per-term upper-bound
    formula exactly as before it existed (`3*f*f - f*f - f*f`, net
    coefficient `+1`, stays three untouched ~7996-digit terms and is
    correctly refused on that basis, same as if this function had done
    nothing to it at all).
    """
    groups: dict = {}
    kept = []
    for arg in args:
        multiset = _factor_multiset(arg, memo_ms)
        if multiset is None:
            kept.append(arg)
            continue
        terms, sign = multiset
        if sign == 0:
            continue  # this term IS exactly zero -- drop unconditionally
        coeff, rest = _split_coefficient(terms)
        key = frozenset(rest.items())
        groups.setdefault(key, []).append((sign * coeff, arg))
    for group in groups.values():
        if sum(c for c, _ in group) == 0:
            continue  # every term in this group cancels -- drop all of them
        kept.extend(arg for _, arg in group)
    return tuple(kept)


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
        itself a `Pow` — a tower): the VERDICT (never the CONSTRUCTION-
        COST question — see `reject_explosive`'s separate Pow loop for
        that) on the COMBINED `base ** exponent`, via three cases, in
        order (round 7's own body, current as of that round):
        (1) `base`'s own magnitude is EXACTLY 1 either side (confirmed via
        THIS function, recursively) — `1 ** anything` (and
        `(-1) ** anything`, for a real result) is `1` regardless of the
        exponent, so the exponent is never even inspected, matching
        SymPy's own `Pow` constructor, which special-cases a unit base
        the identical way. `(0.0, 0.0, True)`.
        (2) Otherwise, the EXPONENT's own `(log_num, log_den)` is resolved
        first (recursing into THIS function again) — if that is
        inconclusive, OR the exponent's own print profile is ALREADY over
        cap (`2**(2**N * 3**(-N))`'s exponent, non-cancelling different
        bases), this `Pow` stays UNRESOLVED, `(0.0, 0.0, False)`, even
        though the COMBINED value can still come out tiny or negligible —
        deliberately: attempting to combine an unresolved OR over-cap
        exponent's value with the base is exactly the pattern that broke
        in three different ways across rounds 2-4. Staying unresolved
        here means the exponent gets independently checked as its own
        subtree wherever a caller falls through to it (the scan's own
        Pow-handling, when this whole `Pow` did not already resolve, and
        `reject_explosive`'s Pow loop, unconditionally).
        (3) Otherwise, `base` is scaled by an UPPER BOUND on the
        exponent's magnitude (`_exp_upper_bound_value` + `_safe_scale_
        log10`, never `base**exponent` itself), and `fully_resolved`
        follows the BASE's own — an unresolved base (a free symbol, most
        commonly) whose exponent bound is not even representable as a
        finite float also falls back to `(0.0, 0.0, False)` rather than
        scaling a merely partial numerator by something unbounded.
      - Anything else (`Symbol`, `Function`, an irrational `NumberSymbol`,
        `Rational`-with-non-Integer-parts, ...): `(0.0, 0.0, False)`.
    """
    from sympy import Add, Float, Integer, Mul, Pow, Rational

    key = id(node)
    if key in memo:
        return memo[key]

    multiset = _factor_multiset(node, memo.setdefault("__multiset__", {}))
    if _table_multiset_trusted(multiset):
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
        #
        # BEFORE any of that: cancel exact additive inverses among the
        # direct terms (#326 finding 4, round 6 — see
        # `_cancel_additive_inverses`'s own docstring). `factorial(1463)*
        # factorial(1463) - factorial(1463)*factorial(1463)` is exactly 0,
        # but the bound below, applied to the UNcancelled two ~7996-digit
        # terms, refused it — the Add-shaped analogue of the exact
        # cancellation `Mul` already gets via `_factor_multiset`.
        surviving_args = _cancel_additive_inverses(node.args, memo.setdefault("__multiset__", {}))
        if not surviving_args:
            result = (0.0, 0.0, True)  # every term cancelled -- the sum is exactly 0
        else:
            parts = [_log10_num_den(arg, memo) for arg in surviving_args]
            log_den = math.fsum(p[1] for p in parts)
            log_num = max(p[0] for p in parts) + log_den + math.log10(len(parts))
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
        # A COMPOUND exponent -- not itself a bare Integer (that case is
        # the branch above). #326 finding 1 (cross-vendor review, round
        # 5): this USED to require the exponent reduce to an EXACT
        # integer (via `_factor_multiset` + the now-deleted
        # `_safe_multiset_rational`) before bounding anything, so an
        # `Add` exponent (`_factor_multiset` has no `Add` case — see its
        # own docstring) or a genuinely non-integral one whose BASE still
        # makes the result a huge integer (`(10**3000)**(3/2) ==
        # 10**4500`) fell through as "no huge-integer result to bound"
        # and reached real evaluation unrefused.
        #
        # Fixed by never requiring exactness for the VERDICT at all: get
        # an UPPER BOUND on the exponent's magnitude from the exponent's
        # own `(log_num, log_den)` (recursing into THIS function --
        # already correct for every node type, `Add`'s conservative upper
        # bound included, and this is the only place that bound is
        # needed at anything past `MAX_NUMERIC_DIGITS`-scale precision),
        # then scale the base's own `(log_num, log_den)` by it
        # (`_safe_scale_log10`, never `base**exponent` itself — see its
        # own docstring for why a `+inf`/`0` base is short-circuited
        # before ever multiplying). No Fraction, no `Mul.flatten`, no
        # materialized exponent value anywhere in this branch.
        b_num, b_den, b_res = _log10_num_den(node.base, memo)
        if b_res and b_num == 0.0 and b_den == 0.0:
            # base's magnitude is EXACTLY 1 either side (|base| == 1,
            # confirmed, not merely small) — `1 ** anything` (and
            # `(-1) ** anything`, for real-valued results) is `1` in
            # magnitude regardless of the exponent, so there is nothing
            # to gain from inspecting the exponent at all: doing so would
            # otherwise refuse `1 ** (20 distinct heavy-call results)`
            # (#326 finding 2's own reproduction) even though the true
            # value is trivially, immediately 1 — SymPy's own `Pow`
            # constructor special-cases a unit base the identical way,
            # never touching the exponent either.
            result = (0.0, 0.0, True)
        else:
            # THE-1095 round 3 (verify-1095-r2): a NESTED table-function
            # exponent (`(x+1)**bell(1463)`) used to be plain "unresolved"
            # to `_log10_num_den` (it only understands `Integer`/`Mul`/
            # `Add`/`Pow`/`Rational`/`Float`), so this branch fell through
            # to "inconclusive" and the caller (`reject_explosive`'s Pow
            # loop) never got a chance to refuse it before the real-dict
            # parse actually materialized `bell(1463)` for real (measured
            # ~5s). `_resolved_table_aware_magnitude` tries the SAME growth-
            # bound machinery `_numeric_ceiling_scan`'s own Function-node
            # branch uses, ignoring any violation IT would raise here
            # (deliberately — that same node is independently visited, and
            # correctly refused, by this scan's own generic descent; this
            # call site only needs a MAGNITUDE, not a refusal decision).
            exp_log_num, exp_log_den, exp_res = _resolved_table_aware_magnitude(node.exp, memo)
            if not exp_res or _ceiling_message_num_den(exp_log_num, exp_log_den):
                # Either inconclusive, OR the EXPONENT's own print-profile
                # is already over cap. The second case matters even
                # though the COMBINED result (`base**exponent`) can still
                # come out tiny: `2**(2**N * 3**(-N))` for a ~300-digit N
                # has a NEGLIGIBLE final value (the two factors nearly
                # cancel), so scaling the base by the bound below would
                # report this Pow as safely resolved and the scan would
                # stop descending into it — but SymPy's real
                # `evaluate=True` parse still has to REDUCE that exponent
                # fraction first, which means materializing `2**N` (an
                # ~10**298-digit integer) whether or not the ratio it
                # feeds into ends up small. A real regression caught
                # writing this: computing `exp_value_upper` from
                # `exp_log_num - exp_log_den` UNDERFLOWS to ~0 for a
                # deeply negative difference (`10.0 ** -1.8e298` is not
                # an error, just a number indistinguishable from 0.0), so
                # the base looked unscaled and safe. Staying UNRESOLVED
                # here instead means the scan's own explicit Pow-descent
                # (which pushes a compound exponent as a separate child)
                # visits this exponent AGAIN as its own subtree and
                # refuses it directly on ITS OWN over-cap print-profile —
                # restoring the protection this branch's "final magnitude
                # is small, so combine and stop descending" shortcut had
                # removed.
                result = (0.0, 0.0, False)
            else:
                exp_value_upper = _exp_upper_bound_value(exp_log_num, exp_log_den)
                if not b_res and not math.isfinite(exp_value_upper):
                    # An UNRESOLVED base (a free symbol, most commonly)
                    # times an exponent bound too large to represent as a
                    # finite float -- nothing sound to scale a merely
                    # PARTIAL base magnitude by. A real bug caught fixing
                    # this shape at the bare-Integer-exponent branch
                    # above: scaling an unresolved base's tiny partial
                    # numerator by an unbounded exponent produces a
                    # spurious "unbounded digits" refusal instead of
                    # correctly falling through to the Pow loop's own
                    # `MAX_SYMBOLIC_EXPONENT` reason.
                    result = (0.0, 0.0, False)
                else:
                    result = (_safe_scale_log10(b_num, exp_value_upper),
                              _safe_scale_log10(b_den, exp_value_upper), b_res)
    else:
        result = (0.0, 0.0, False)
    memo[key] = result
    return result


def _digit_count_over_cap(magnitude: float, cap: int) -> bool:
    """Whether a log10 `magnitude` represents a digit count over `cap`.

    The "digits"-kind half of `_FUNCTION_ARG_CAPS`' two comparisons (see
    its own comment) — same digit-count formula `_ceiling_message_num_den`
    uses for `MAX_NUMERIC_DIGITS` (`int(magnitude) + 1`, never `magnitude`
    directly, since a log10 magnitude and a digit COUNT differ by up to
    one), parameterized by `cap` instead of hardcoding that one global
    constant, and with the same "too large to even usefully count" guard
    (`not finite`, or so large that spelling out the digit count would
    itself take dozens of digits) folded in as unconditionally over cap.
    Shared by `_numeric_ceiling_scan`'s `Function`-node branch and
    `reject_explosive`'s Pow loop's unit-fraction-exponent branch, so the
    ONE comparison a "digits"-kind cap needs lives in one place regardless
    of which tree shape triggered it.

    #326 finding (cross-vendor review, round 10, Codex): `magnitude` is a
    FLOAT log10 approximation (`_log10_of_int`'s bit-shifting estimate,
    for anything past 53 bits), accurate to within float precision almost
    everywhere but measured to round the WRONG way at an EXACT boundary:
    `safe_parse("sqrt(" + "9"*1200 + ")")` — a value with EXACTLY 1200
    digits, at `MAX_ROOT_ARG_DIGITS` — computed a magnitude that floors to
    1200 (not 1199), reporting 1201 digits and refusing what the exact
    count admits. `_digit_count_over_cap_for_node`, below, is the fix —
    prefer the EXACT digit count whenever the underlying value is cheap
    to get one from — and calls THIS function only as its fallback for
    whatever it could not get an exact count for. This function's own
    float-only contract is otherwise unchanged: it still ALWAYS gets the
    right answer for the two directions that matter here (never mistaking
    "moderately over cap" for "under," which is one-sided risk this ONLY
    approximation-caused an OVER-refusal, never an under-refusal).
    """
    return (not math.isfinite(magnitude) or magnitude > 1e15
            or int(magnitude) + 1 > cap)


def _exact_digit_count_if_cheap(n: int) -> int | None:
    """The EXACT decimal digit count of `abs(n)`, or `None` if `n` is too
    large to materialize as a decimal string cheaply and safely.

    #326 finding (cross-vendor review, round 10, Codex): the counterpart,
    for an ALREADY-PARSED integer value, of `_exact_decimal_digit_count`
    (which does the identical job for a TOKEN's literal source text) —
    see that function's own docstring for the boundary bug both exist to
    close, one layer apart. Safe and cheap whenever `n`'s magnitude is
    small enough that `str()` itself would succeed well within CPython's
    own int->str conversion limit (`sys.set_int_max_str_digits`, default
    4300 decimal digits) — the bit-length cutoff here is comfortably
    under that, not tuned to the specific 25/1200 caps this currently
    serves, so it stays correct if either grows. `int(a_sympy_Integer)`
    to get here in the first place is itself O(1) regardless of size —
    the only cost this avoids is the DECIMAL conversion, not the int
    conversion.
    """
    n = abs(n)
    if n.bit_length() > 14_000:  # ~4,214 decimal digits, under the 4,300 default
        return None
    return len(str(n)) if n else 1


def _digit_count_over_cap_for_node(node, side: str, magnitude: float, cap: int) -> bool:
    """Whether `node`'s `side` ("num" or "den") is a digit count over
    `cap` — prefers the EXACT digit count when `node` is a bare, already-
    materialized `Integer`/`Rational` (cheap and safe — see
    `_exact_digit_count_if_cheap`'s own docstring), falling back to
    `_digit_count_over_cap`'s float-log comparison (`magnitude`, the
    caller's own already-computed `_log10_num_den` result for the SAME
    `side`) for anything else. An `Integer`'s own denominator is always
    exactly `1` — never over any cap this module would set above `0` —
    so `side="den"` short-circuits to `False` for one without consulting
    `magnitude` at all.

    A `Mul`/`Pow`-of-`Integer` CHAIN `node` is not reconstructed into a
    concrete value here even when `_factor_multiset` resolved it exactly
    — multiplying it out just to count digits could itself be the
    expensive operation this whole file exists to avoid (the same reason
    `_log10_num_den` measures a `Pow`'s magnitude via `_safe_log10_pow`
    rather than `base ** exponent`) — so those stay on the float-log path,
    unchanged, same as before this function existed.
    """
    from sympy import Integer, Rational

    if isinstance(node, Integer):
        if side == "den":
            return False
        exact = _exact_digit_count_if_cheap(int(node))
        if exact is not None:
            return exact > cap
    elif isinstance(node, Rational):
        exact = _exact_digit_count_if_cheap(node.p if side == "num" else node.q)
        if exact is not None:
            return exact > cap
    return _digit_count_over_cap(magnitude, cap)


def _digit_ceiling_text(magnitude: float, subject: str, location: str = "") -> str | None:
    """Refusal text if `magnitude` (a log10 value) is over `MAX_NUMERIC_
    DIGITS`, phrased as "`subject` would have about N digits`location`,
    over the limit of `MAX_NUMERIC_DIGITS`: it cannot be rendered as a
    decimal string" (or the "unbounded number of digits" variant for a
    magnitude too astronomically large to spell out as a plain count), or
    `None` if it is not over. `location` is an optional " in its
    numerator"/" in its denominator" suffix for a `Rational`'s two
    independently-checked sides (see `_ceiling_message_num_den`); empty
    for a plain integer result with no such distinction (see
    `_shift_digit_ceiling_violation`). THE-1095 round-3-follow-up
    (coordinator review of d3c65c7): extracted so the wording is built in
    exactly ONE place — the shift check used to build its own text by
    string-prefixing `_ceiling_message_num_den`'s OWN "numerator"-phrased
    output ("'<<' shift result the result would have about N digits in
    its numerator, ..."), which read as a doubled "result" with a
    numerator/denominator distinction that makes no sense for a plain
    integer shift.
    """
    if magnitude <= 0:  # at most one digit, never over cap
        return None
    # `magnitude` itself can be finite yet astronomically large (`2**
    # (1000000**6)`'s numerator has log10 ~ 3e35 -- a REAL, exact answer,
    # not an overflow), so `digit_count` would need dozens of its own
    # digits to spell out. Past this line, describing "how many digits"
    # is no longer useful information, so this reports the same
    # "unbounded" wording `not math.isfinite` uses below rather than
    # interpolating an unreadable number.
    if not math.isfinite(magnitude) or magnitude > 1e15:
        return (f"{subject} would have an unbounded number of digits"
                f"{location}, over the limit of {MAX_NUMERIC_DIGITS}: it "
                "cannot be rendered as a decimal string")
    digit_count = int(magnitude) + 1
    if digit_count > MAX_NUMERIC_DIGITS:
        return (f"{subject} would have about {digit_count} digits{location}, "
                f"over the limit of {MAX_NUMERIC_DIGITS}: it cannot be "
                "rendered as a decimal string")
    return None


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
        text = _digit_ceiling_text(magnitude, "the result", f" in its {side}")
        if text:
            return text
    return None


def _table_function_bound(node, memo: dict) -> tuple[float | None, str | None]:
    """`(log10_upper_bound, None)` for `node` — a `Function` node whose
    class name is a key of `_FUNCTION_ARG_CAPS` — or `(None, message)` on a
    violation found while resolving it, or a `"cannot be bounded"` refusal
    if this name has no `_GROWTH_BOUNDS` entry of its own.

    THE-1095 round 3 (`verify-1095-r2`, both reviewers, finding 2):
    `_numeric_ceiling_scan`'s own resolver, `_log10_num_den`, only
    understands `Integer`/`Mul`/`Add`/`Pow`/`Rational`/`Float` — a NESTED
    table-function call (`factorial(100)` as `divisors`' own argument) was
    always "unresolved" to it, so `divisors(factorial(100))` sailed past
    every cap this module has (measured 11.9s on the public path) even
    though `factorial`'s OWN position-0 cap was never itself violated
    (100 is nowhere near 1463) — the danger is `factorial(100)`'s RESULT
    (158 digits) feeding `divisors`' own "digits" cap (25), which nothing
    ever computed. This function is what closes that: it checks `node`'s
    OWN bounded positions against their OWN caps first (recursing through
    `_resolve_arg_magnitude`, so a chain of nested calls — `factorial(
    polygamma(0, 1000**1000))` — is bounded all the way down, each level
    refusing immediately if IT is over cap), then hands the (now proven
    safe) per-position magnitudes to `_GROWTH_BOUNDS[name]` to get a safe
    UPPER BOUND on `node`'s own result — which the CALLER (either this
    function again, one level up, or `_numeric_ceiling_scan`'s own
    Function-node branch) then treats exactly like a directly-resolved
    numeric magnitude.

    A table name with no `_GROWTH_BOUNDS` entry (`sqrt`/`root`/`cbrt`, and
    the eager-factor family, which return a `dict`/`list`/`bool`, not a
    number `divisors`/`factorint`-nested-in-anything makes no numeric
    sense either way) fails CLOSED here — `unknown != safe`, per this
    module's own established bar — with a message distinct from a plain
    unresolved argument (a free symbol, or a non-table function like
    `sin`/`exp`, which this function is never even called for and which
    stay silently skipped, exactly as before this round).
    """
    name = type(node).__name__
    bounds: dict[int, float] = {}
    for pos, kind, cap in _bounded_positions(name):
        if pos >= len(node.args):
            continue
        arg = node.args[pos]
        if arg.free_symbols:
            continue
        arg_log_num, arg_log_den, arg_resolved, arg_violation = _resolve_arg_magnitude(arg, memo)
        if arg_violation:
            return None, arg_violation
        if not arg_resolved:
            continue
        if kind == "value":
            over_cap = (arg_log_num - arg_log_den) > math.log10(cap)
        else:
            over_cap = _digit_count_over_cap_for_node(arg, "num", arg_log_num, cap)
        if over_cap:
            return None, (f"an argument to {name}() exceeds the limit of {cap}: "
                           "computing it would take an unbounded amount of time "
                           "and memory")
        bounds[pos] = arg_log_num - arg_log_den
    growth = _GROWTH_BOUNDS.get(name)
    if growth is None:
        return None, (f"{name}() cannot be safely bounded as a nested argument: "
                       "no growth estimate is defined for it")
    return growth(bounds), None


def _resolve_arg_magnitude(node, memo: dict) -> tuple[float, float, bool, str | None]:
    """`(log_num, log_den, resolved, violation)` for `node` — `_log10_num_
    den`'s own three, plus a fourth slot for a hard refusal found while
    resolving a NESTED table-function call (see `_table_function_bound`'s
    own docstring). `violation` is `None` on every ordinary path
    (including the "just plain unresolved" one this module already had);
    a caller checks it FIRST and returns immediately when set, the same
    way `_numeric_ceiling_scan`'s main loop already returns on its own
    direct cap violations.

    THE-1095 round-3-follow-up #4 (coordinator review of 06272aa, grok
    item 2): a `Mul`/`Add` WRAPPING a table call (`-bell(1463)` ==
    `Mul(-1, bell(1463))`; `bell(1463)+1`; `2*bell(1463)`) used to stay
    plain "unresolved" here, even though `bell(1463)` alone — the ONE
    NESTED piece not already resolvable via `_log10_num_den` — resolves
    fine via the branch just above. `_deferred_binop_violation`'s own
    left/right operand resolution reads exactly this "unresolved" result
    as "cannot be safely bounded" and refuses on an UNKNOWN — not an
    over-cap verdict — even for `-bell(1463) % 7`, where `bell(1463)`
    alone is comfortably safe and `origin/main` evaluates it. Composed
    the same way `_log10_num_den` already composes a `Mul`/`Add` of
    ordinary numeric pieces, just recursing through THIS function (so a
    nested table call inside either gets the SAME growth-bound
    treatment) instead of `_log10_num_den` directly: a `Mul`'s own
    magnitude is the SUM of its factors' own magnitudes (sign discarded
    — the SAME "abs, not signed" treatment `_log10_num_den`'s own
    `Integer` handling already gives a plain `-1` factor); an `Add`'s own
    magnitude is bounded by its LARGEST term's own magnitude, scaled up
    by `log10(number of terms)` — the SAME safe-upper-bound formula
    `_log10_num_den`'s own `Add` branch already uses for a purely
    numeric sum, reused here rather than a second, parallel formula.
    """
    from sympy import Add, Function, Mul

    log_num, log_den, resolved = _log10_num_den(node, memo)
    if resolved:
        return log_num, log_den, True, None
    if isinstance(node, Function) and type(node).__name__ in _FUNCTION_ARG_CAPS:
        bound, violation = _table_function_bound(node, memo)
        if violation:
            return 0.0, 0.0, False, violation
        if bound is not None:
            return bound, 0.0, True, None
    if isinstance(node, Mul):
        total = 0.0
        for factor in node.args:
            f_log_num, f_log_den, f_resolved, f_violation = _resolve_arg_magnitude(factor, memo)
            if f_violation:
                return 0.0, 0.0, False, f_violation
            if not f_resolved:
                return log_num, log_den, False, None
            total += f_log_num - f_log_den
        return total, 0.0, True, None
    if isinstance(node, Add):
        magnitudes = []
        for term in node.args:
            t_log_num, t_log_den, t_resolved, t_violation = _resolve_arg_magnitude(term, memo)
            if t_violation:
                return 0.0, 0.0, False, t_violation
            if not t_resolved:
                return log_num, log_den, False, None
            magnitudes.append(t_log_num - t_log_den)
        if not magnitudes:
            return log_num, log_den, False, None
        return max(magnitudes) + math.log10(len(magnitudes)), 0.0, True, None
    return log_num, log_den, False, None


def _resolved_table_aware_magnitude(node, memo: dict) -> tuple[float, float, bool]:
    """`(log_num, log_den, resolved)` — `_log10_num_den`'s own three-tuple
    contract, called FROM `_log10_num_den` itself for BOTH a compound-Pow
    EXPONENT and (THE-1095 round-3-follow-up, coordinator review of
    949aac9, grok item 4) a Pow's own BASE, so a NESTED table-function
    call in EITHER position (`(x+1)**bell(1463)`, or `bell(1463)**2`)
    resolves via `_table_function_bound`'s growth bound instead of
    staying plain "unresolved" — see each call site's own comment for
    why this matters. Before item 4's fix, `bell(1463)**2` sailed past
    the deferred scan (its BASE, unlike its exponent, was never routed
    through this same resolver) and paid `bell(1463)`'s own real ~5s
    construction cost during the REAL `evaluate=True` parse before the
    OUTPUT ceiling (the last-resort backstop, not a promptness one) ever
    caught it — the exact "construct first, refuse after" shape every
    other fix in this module exists to avoid. Unlike `_resolve_arg_
    magnitude`, a violation found while resolving is DISCARDED here,
    deliberately: this helper only ever needs to report the node's
    magnitude to `_log10_num_den`'s own verdict logic, not decide the
    tree's fate — the same nested node is independently visited (and
    correctly refused, if it deserves to be) by `_numeric_ceiling_scan`'s
    own generic descent, which DOES see the violation, moments later in
    the same scan.
    """
    log_num, log_den, resolved = _log10_num_den(node, memo)
    if resolved:
        return log_num, log_den, True
    from sympy import Function

    if isinstance(node, Function) and type(node).__name__ in _GROWTH_BOUNDS:
        bound, _violation = _table_function_bound(node, memo)
        if bound is not None:
            return bound, 0.0, True
    return log_num, log_den, False


#: marker class name (`_deferred_binop_classes()`'s own keys, by NAME
#: since this scan only ever sees `type(node).__name__`, never the class
#: object itself) -> the operator it stands for.
_DEFERRED_BINOP_OPS = {
    "_DeferredLShift": "<<",
    "_DeferredRShift": ">>",
    "_DeferredMod": "%",
    "_DeferredFloorDiv": "//",
}


def _deferred_binop_violation(node, memo: dict) -> str | None:
    """Reason a `_DeferredLShift`/`_DeferredRShift`/`_DeferredMod`/
    `_DeferredFloorDiv` marker node (`_deferred_transformer_class`'s own
    AST transform — see its module-level comment for why these exist at
    all) would produce a result over `MAX_NUMERIC_DIGITS`, or `None` if
    `node` is not one of these four marker types, or is and stays safely
    under.

    THE-1095 round-3-follow-up (coordinator review of 949aac9, grok):
    replaces the deleted TOKEN-level `_shift_digit_ceiling_violation` —
    this is the SAME bound, moved to the TREE, so token adjacency no
    longer matters: `1 << (factorial(20))`, `1 << factorial(12+1)`,
    `1 << binomial(40, 20)`, `1 << rf(30, 20)`, `(bell(1463)) <<
    100000` all reach this branch now, at whatever depth or shape the
    caller wrote them, because the DEFERRED TREE carries a marker node
    for every one of them, at every depth, since round-3-follow-up #3's
    AST-level fix (`_deferred_transformer_class`'s own module-level
    comment) closed the operand-shape dependency the mixin-based
    round-3-follow-up #2 version still had (`-bell(1463) % 7`,
    `(bell(1463)+1) % 7`, `2*bell(1463) % 7` all bypassed IT, since
    SymPy's own eager `Mod`/`floor` ran before the mixin ever got a
    chance once either operand was already a concrete-looking `Expr`).
    Also now correctly bounds a purely NUMERIC right operand
    (`1 << (2**200)`) the same way, since `2**200` reaching this branch
    is ALSO a marker-node child now, evaluate=False-protected the same
    as every other subtree, resolved via `_log10_num_den` below.

    `<<` (the only one of the four that can GROW its operand): `digits(
    left) + count * log10(2)` against `MAX_NUMERIC_DIGITS`, where `count`
    is the RIGHT operand's own VALUE (via `_safe_pow10`, converting its
    resolved log10 bound back to a plain float) — NEVER a blanket
    Stirling-of-factorial approximation (grok's own finding: `1 <<
    prime(10)` was bounded as `1 << 10!` and refused, though `prime(10)
    == 29` and `origin/main` returns `536870912` cleanly — Stirling is
    only ever a valid digit bound for a factorial-SCALE operand, and
    `prime`/`totient`/`fibonacci`/`harmonic`/`primepi` all grow far
    slower; `_resolve_arg_magnitude` below already resolves each NAME
    through its own `_GROWTH_BOUNDS` entry, exactly the "one spec drives
    everything" bound every other check in this module already uses —
    no second, parallel approximation needed here).

    `>>`/`%`/`//`: the result's own magnitude never EXCEEDS the LEFT
    operand's own magnitude (`a >> n <= a`, `a % b <= a`, `a // b <= a`
    for the positive integers this module's own hazards are about) — so
    only the left operand needs bounding; the right operand (if it is
    ALSO a table call) gets its own, independent check from this same
    scan's generic descent moments later, unrelated to this node's own
    verdict.

    An operand that resolves with a free symbol anywhere in it is left
    alone (`return None` for THIS node — the generic descent below still
    reaches inside it for any OTHER hazard): a genuinely symbolic operand
    never gets materialized into a concrete value, so there is no digit
    count to bound here at all — matching `_table_function_bound`'s own
    per-position `if arg.free_symbols: continue`. Anything else that does
    not resolve (an irrational constant, a non-table function this module
    does not know how to bound, ...) fails CLOSED instead — `unknown !=
    safe`, the same bar every other finding in this module already holds
    to.
    """
    op = _DEFERRED_BINOP_OPS.get(type(node).__name__)
    if op is None:
        return None
    left, right = node.args[0], node.args[1]
    if left.free_symbols:
        return None
    left_log_num, left_log_den, left_resolved, left_violation = _resolve_arg_magnitude(left, memo)
    if left_violation:
        return left_violation
    if not left_resolved:
        return (f"the left operand of '{op}' cannot be safely bounded: "
                "computing it would take an unbounded amount of time and memory")
    left_log = left_log_num - left_log_den
    if op != "<<":
        # result <= left operand's own magnitude for >>, %, //
        return _digit_ceiling_text(left_log, f"the result of '{op}'")
    if right.free_symbols:
        return None
    right_log_num, right_log_den, right_resolved, right_violation = _resolve_arg_magnitude(right, memo)
    if right_violation:
        return right_violation
    if not right_resolved:
        return (f"the right operand of '{op}' cannot be safely bounded: "
                "computing it would take an unbounded amount of time and memory")
    right_log = right_log_num - right_log_den
    count = _safe_pow10(right_log)
    if count is None or count < 0:
        return _digit_ceiling_text(math.inf, "the result of '<<'")
    total_log = left_log + count * math.log10(2)
    return _digit_ceiling_text(total_log, "the result of '<<'")


def _pow_table_base_violation(node, memo: dict) -> str | None:
    """Reason a `Pow` node whose BASE is a table-function call would
    produce a result over `MAX_NUMERIC_DIGITS`, or `None` — or if `node`
    is not such a `Pow` at all.

    THE-1095 round-3-follow-up (coordinator review of 949aac9, grok item
    4): `bell(1463)**2` used to sail past the deferred scan (its exponent
    already resolved a nested table-function call via `_resolved_table_
    aware_magnitude`, but its BASE never did) and pay `bell(1463)`'s own
    real ~5s construction cost during the REAL `evaluate=True` parse
    before the output ceiling — the last-resort backstop, not a
    promptness one — ever caught it.

    Checked HERE, SEPARATELY from `_log10_num_den`'s own Pow-handling
    (never feeds into it, never touches `_factor_multiset`'s own Mul-
    cancellation accounting) — tried routing the base through `_log10_
    num_den`'s OWN shared base-resolution first (mirroring the exponent's
    own `_resolved_table_aware_magnitude`) and it REGRESSED a genuinely-
    cancelling product: `factorial(1463)/factorial(1463)*factorial(1463)
    /factorial(1463)` (net exponent 0, true value exactly 1) was reported
    as an ~18_000-digit, uncancelled denominator instead, because giving
    ONE Pow factor's base a growth-bound APPROXIMATION (rather than
    leaving it plain "unresolved", its previous behavior) makes `_log10_
    num_den`'s Mul branch treat it as a real contribution to a
    conservative, non-cancelling sum — `_factor_multiset`'s own EXACT
    cancellation only recognizes a repeated base by Python integer
    equality, never an approximate bound, so it could not use this
    information at all, and the surrounding fallback math actively
    misused it. This function answers a NARROWER question instead —
    "is THIS Pow node's own base large enough, on its own, to make
    raising it to THIS exponent unsafe" — independent of whatever any
    enclosing `Mul` is doing with it, so it can never interfere with
    cancellation the way sharing `_log10_num_den`'s own resolver did.
    """
    from sympy import Function, Integer, Pow

    if not (isinstance(node, Pow) and isinstance(node.base, Function)
            and type(node.base).__name__ in _GROWTH_BOUNDS):
        return None
    if node.base.free_symbols or node.exp.free_symbols:
        return None
    base_log_num, base_log_den, base_resolved, base_violation = _resolve_arg_magnitude(node.base, memo)
    if base_violation:
        return base_violation
    if not base_resolved:
        return None  # left to this module's OTHER backstops, not guessed at here
    base_log = base_log_num - base_log_den
    if not isinstance(node.exp, Integer):
        return None  # the compound/symbolic-exponent case has its own machinery
    exp_int = int(node.exp)
    if exp_int == 0:
        return None  # `anything ** 0 == 1`, trivially safe regardless of the base
    total_log = base_log * abs(exp_int)
    name = type(node.base).__name__
    if exp_int > 0:
        return _digit_ceiling_text(total_log, f"the result of {name}(...)**{exp_int}")
    return _digit_ceiling_text(total_log, f"the result of {name}(...)**{exp_int}",
                                " in its denominator")


def _function_arg_cap_violation(node, memo: dict) -> str | None:
    """Reason a `Function` node whose class name is a key of
    `_FUNCTION_ARG_CAPS` has an over-cap argument at one of its own
    bounded positions, or `None` if `node` is not such a call, or is and
    every bounded position stays under cap.

    THE-1095 round-3-follow-up #3 (coordinator review of 5e2a961, grok
    item 1): extracted from `_numeric_ceiling_scan`'s own main loop so it
    can ALSO be walked unconditionally, the same way `reject_explosive`'s
    own Pow loop already walks every `Pow` node via `_walk` regardless of
    whether an ancestor resolved cheaply — `_numeric_ceiling_scan`'s own
    stack-based descent STOPS at a node `_log10_num_den` reports fully
    resolved (correctly, for an exact numeric verdict — see that scan's
    own docstring), but round-3-follow-up #2's own `_factor_multiset`
    extension (keying a table-function call for EXACT cancellation) means
    a fully-CANCELLING product of two table calls now also reports
    `resolved=True` with a trivial value (`factorial(factorial(8))/
    factorial(factorial(8)) == 1`) — correct about the OUTER value, but
    silent about whether the CHILDREN (`factorial(8)` -> `factorial(
    40320)`, a real, unbounded construction) are themselves safe to
    build, exactly the same class of bug the Pow loop's own unconditional
    walk already exists to close for `2**1000000000 - 2**1000000000`.
    See `_numeric_ceiling_scan`'s own call site, and the NEW unconditional
    walk in `reject_explosive`, for where this now runs twice — once from
    the stack-based descent (cheap, most of the time all that is needed),
    once unconditionally (closing the cancellation gap specifically).
    """
    from sympy import Function

    if not (isinstance(node, Function) and type(node).__name__ in _FUNCTION_ARG_CAPS):
        return None
    # #326 finding 1 (cross-vendor review, round 6): a COMPUTED
    # argument (`factorial(1463+1)`) stays an opaque `Function`
    # node through the whole `evaluate=False` parse -- nothing in
    # the trigger set below matches it, and even if its argument
    # (an unevaluated `Add(1463, 1)`) got pushed via the generic
    # descent at the bottom of this loop, the generic per-node
    # check there compares against `MAX_NUMERIC_DIGITS` (4000),
    # not `MAX_HEAVY_ARG` (1463) -- 1464 is a perfectly printable
    # 4-digit number, so that check would never fire even though
    # `factorial(1464)` is exactly the unbounded-work hazard
    # `_heavy_call_violation`'s own TOKEN-level check exists to
    # stop for a LITERAL argument. `_heavy_call_violation`'s
    # docstring used to claim a computed argument was "caught
    # later by the tree rules" -- false until this branch existed
    # (see its own docstring for the full history). Kept as a
    # backstop alongside the cheap token-level first pass, not a
    # replacement for it.
    #
    # #326 finding 1 (round 8, Codex): reads `_FUNCTION_ARG_CAPS`
    # generally now, not just `_HEAVY_FUNCTIONS` -- "value" kind
    # unchanged from the round-6 logic above; a "digits"-kind name
    # (the factoring family) is included here for the SAME
    # structural reason the table drives every enforcement site.
    # Round 8 noted that none of the nine "digits"-kind names could
    # actually reach this branch THEN: the factoring family
    # (`factorint` and its five siblings) are plain Python callables
    # (or, `mobius` excepted, ones that never leave an unevaluated
    # `Function` node behind for a computed argument -- see
    # `MAX_FACTOR_ARG_DIGITS`'s own comment), and `sqrt`/`root`/
    # `cbrt` compile to a `Pow`, never a `Function` named after
    # themselves (see `MAX_ROOT_ARG_DIGITS`'s own comment).
    # THE-1095 changed the first half of that: `node` here is always
    # built from `_deferred_global_dict()` (`safe_parse`'s own
    # pre-parse step, the only caller that ever hands a tree to this
    # scan), which replaces every "digits"-kind name EXCEPT `sqrt`/
    # `cbrt` with an inert stand-in `Function` subclass named
    # after itself (see `_DEFERRED_STANDIN_NAMES`'s own comment for
    # why those two stay excluded) -- so `factorint`,
    # `primefactors`, `divisors`, `mobius`, `nextprime`, `isprime`,
    # and (round 2, `verify-1095`) `root` all genuinely reach this
    # branch now, for the exact reason `sqrt`/`cbrt`'s own backstop
    # stays the Pow loop's unit-fraction-exponent branch instead
    # (they are never deferred, so a `Function` node named
    # `sqrt`/`cbrt` still never occurs). A future name added to the
    # table with this family's shape is covered here automatically,
    # without a second copy of this branch to remember to add, as
    # long as it is not added to the `sqrt`/`cbrt` exclusion set for
    # the same structural reason those two are.
    #
    # THE-1095 round 2 bounded ONLY `node.args[:1]` (position 0) --
    # measurably wrong for `binomial(n, k)`'s own `k` (`eval()`
    # resolves `k > n` to `0` in O(1), so capping it only
    # manufactured a false refusal), but it also left `rf`/`ff`'s
    # own `k` (a genuine, uncapped iteration count) and several
    # OTHER names' non-first positions unbounded. THE-1095 round 3
    # (`verify-1095-r2`, both reviewers): `_bounded_positions`
    # below is the per-name, per-POSITION spec (`_FUNCTION_ARG_
    # CAPS` + `_EXTRA_BOUNDED_POSITIONS`, minus `_UNBOUNDED_
    # POSITIONS` -- see their own comments) that replaces the flat
    # `args[:1]` this round: every position that actually drives
    # cost is bounded, `binomial`'s `k` stays deliberately exempt,
    # and a position with no spec at all (most of a call's
    # arguments, for most names) is skipped exactly as before.
    #
    # `_resolve_arg_magnitude`, not `_log10_num_den` directly, is
    # what actually resolves each bounded argument now: a NESTED
    # table-name call (`divisors(factorial(100))`) used to be
    # unconditionally "unresolved" here (`_log10_num_den` only
    # understands `Integer`/`Mul`/`Add`/`Pow`/`Rational`/`Float`,
    # never a `Function`) and fall through to the generic descent
    # below, silently untested for THIS node's own cap — see
    # `_resolve_arg_magnitude`'s own docstring for how it closes
    # that by recursing into the nested name's own GROWTH bound.
    for pos, kind, cap in _bounded_positions(type(node).__name__):
        if pos >= len(node.args):
            continue  # this call did not supply that many arguments
        arg = node.args[pos]
        if arg.free_symbols:
            continue  # symbolic argument -- left alone, same scope as the token check
        arg_log_num, arg_log_den, arg_resolved, arg_violation = _resolve_arg_magnitude(arg, memo)
        if arg_violation:
            return arg_violation
        if not arg_resolved:
            continue  # inconclusive -- an irrational constant, a free-standing non-table Function, ...
        if kind == "value":
            # #326 finding 2 (cross-vendor review, round 9, grok):
            # read from THIS name's own row (`cap`, already looked
            # up above), not a module-level `MAX_HEAVY_ARG`
            # constant precomputed once outside the loop -- every
            # "value"-kind row happens to share that same cap
            # today, but computing `log10(cap)` HERE, per node,
            # means a future per-name override in the table is
            # honoured automatically rather than silently ignored
            # by a stale precomputed log10.
            over_cap = (arg_log_num - arg_log_den) > math.log10(cap)
        else:
            over_cap = _digit_count_over_cap_for_node(arg, "num", arg_log_num, cap)
        if over_cap:
            return (f"an argument to {type(node).__name__}() exceeds the "
                    f"limit of {cap}: computing it would take an "
                    "unbounded amount of time and memory")
    # An argument that IS a free symbol, or one this bound could not
    # resolve, might still hide a SEPARATE numeric hazard nested
    # inside it (`factorial(x*f*f)`, say) -- this function only
    # answers for THIS node's own bounded positions; a caller's own
    # descent (generic, or the new unconditional walk) is what finds
    # that separate hazard, not a repeated call here.
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

    THE-1095, one cross-vendor review round later: this scan's `Function`-
    node branch (below) is only ever handed the tree from `safe_parse`'s
    pre-parse step, which — since THE-1095 — is built through
    `_deferred_global_dict()`, not `safe_global_dict()`, precisely so that
    branch has a real `Function` node to inspect, correctly named and
    positioned, for every entry in `_FUNCTION_ARG_CAPS` except `sqrt`/
    `root`/`cbrt` (see that dict's own docstring for the empirical audit of
    why the REAL functions could not be trusted to leave one behind on
    their own — a rename, an eager evaluation regardless of `evaluate=
    False`, or an outright exception on a merely computed argument, was
    each already a live gap this scan could not have closed just by
    matching harder on the node it happened to be handed).

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
    from sympy import Add, Function, Integer, Mul, Pow

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
        if isinstance(node, Function) and type(node).__name__ in _DEFERRED_BINOP_OPS:
            # THE-1095 round-3-follow-up: a marker node produced by
            # `_DeferredOperatorMixin` (`<<`/`>>`/`%`/`//` touching a
            # deferred stand-in, anywhere in the tree, any shape) — see
            # `_deferred_binop_violation`'s own docstring for the bound.
            binop_violation = _deferred_binop_violation(node, memo)
            if binop_violation:
                return binop_violation
            # Fall through to the generic descent below regardless: each
            # operand might independently hide its OWN hazard (a nested
            # table call as either side), which this node's own operator-
            # specific check above does not replace, only supplements.
        pow_base_violation = _pow_table_base_violation(node, memo)
        if pow_base_violation:
            return pow_base_violation
        function_cap_violation = _function_arg_cap_violation(node, memo)
        if function_cap_violation:
            return function_cap_violation
        if isinstance(node, (Integer, Mul, Add, Pow)):
            log_num, log_den, resolved = _log10_num_den(node, memo)
            violation = _ceiling_message_num_den(log_num, log_den)
            if violation:
                return violation
            if resolved:
                # Fully accounted for -- see this function's own docstring
                # for why checking a piece of it independently, below,
                # would be wrong rather than merely redundant. This
                # includes a `Pow` whose base is a unit value (`1`, `-1`,
                # `1/1`, `2/2`, `1.0`) or whose exponent structurally
                # cancels (`2**(2**N * 2**(-N))`): `_log10_num_den`'s
                # PRINT-PROFILE verdict of `(0, 0, True)` for either shape
                # says nothing about whether CONSTRUCTING the exponent is
                # cheap (#326 findings 1-2, cross-vendor review round 7:
                # `1 ** (2**N * 3**(-N))` and `2**(2**N * 2**(-N))`, for a
                # ~300-digit `N`, both hang building the inner `Pow(2,
                # N)`/`Pow(3, -N)` even though the OUTER result is
                # trivially safe, or the exponent's OWN print profile
                # cancels to nothing). Closing that gap is NOT this scan's
                # job, though: a first attempt at making this scan push
                # a resolved Pow's children anyway regressed
                # `1**(20 distinct factorial factors)` -- the scan would
                # then independently check the EXPONENT Mul's own print
                # profile (correctly ~79347 digits, since 20 distinct
                # factorials do not structurally cancel) and refuse, even
                # though that Mul is cheap to CONSTRUCT (just multiplying
                # 20 already-materialized integers) and its print profile
                # is irrelevant -- it is never rendered, only raised to,
                # and `1 ** anything` is always `1`. Print-profile-over-
                # cap and expensive-to-construct are different questions;
                # this scan answers only the first. The construction-cost
                # hazard is instead closed by `reject_explosive`'s
                # separate Pow-only loop (see its own comment), which
                # visits every `Pow(numeric_base, integer_exponent)` node
                # in the tree via `_walk` -- an unconditional walk with no
                # "stop descending" optimization at all -- so it reaches
                # the inner `Pow(2, N)`/`Pow(3, -N)` regardless of what
                # this scan does with any Mul/Add ancestor's cancellation
                # or unit-base status.
                continue
            # Partial: the KNOWN part was just checked above and cleared.
            # Fall through to the generic descent so an unresolved sibling
            # (a Function argument, ...) still gets its own chance to
            # reveal a hazard `_log10_num_den` could not see from here --
            # EXCEPT a `Pow` node, which needs its OWN descent rule, not
            # the generic one below: a `Pow` with a plain Integer exponent
            # whose BASE did not resolve (a symbolic base, most commonly)
            # must push ONLY the base, never the exponent. The exponent
            # there is not a value about to be PRINTED — it is a count
            # `reject_explosive`'s own Pow loop uses for a completely
            # different ceiling (`MAX_SYMBOLIC_EXPONENT` for a symbolic
            # base, or its own numeric-base construction-cost check, for
            # exactly the gap this scan leaves to that loop — see that
            # loop's own comment) — and pushing a bare exponent onto THIS
            # stack anyway was a real bug caught fixing a different
            # round: `(x+1)**20000!!...` (`factorial2(20000)`, an exact
            # Integer with thousands of digits, used AS AN EXPONENT on a
            # symbolic base) had its bare exponent independently checked
            # here as if it were a print target and refused on "digits in
            # its numerator" instead of correctly falling through to the
            # Pow loop's own, more accurate `MAX_SYMBOLIC_EXPONENT`
            # reason. A `Pow` whose exponent is NOT a plain Integer
            # (compound or itself a tower) still gets both children
            # pushed whenever the WHOLE node did not resolve — that node
            # reaching here at all already means `_log10_num_den`'s
            # compound-Pow branch loaded the base FIRST (its very first
            # line, checking the unit-base shortcut) and still came back
            # unresolved, so "the base was never looked at" is not why
            # both are pushed: the base's own UNIT check is not the same
            # as the base's own PRINT-PROFILE/multiset being indepen-
            # dently verified, and a non-unit base still needs that
            # (`(f*f)**(2**N*3**(-N))`'s base, not just its exponent, is a
            # numeric hazard in its own right) — while the exponent needs
            # it for the reasons above (`2**(2**N * 3**(-N))`'s exponent
            # is exactly that shape).
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


def _bare_class_violation(tree) -> str | None:
    """Reason `tree` (or something nested inside it) is a bare reference to
    a Python class this module exposes, rather than a genuine expression
    node — or `None`. THE-1095 round-3-follow-up (ClusterFuzzLite crash on
    44c83b1, sympy 1.14.0): a bare reference to a class in `safe_global_
    dict()` (`binomial`, `Pow`, `Mul`, ... — every name this module's own
    namespace exposes without calling it) used to be caught only at the
    TOP of a tree (`_walk`'s own `isinstance(args, tuple)` guard, its own
    comment) — but `parse_expr` can ALSO embed that same bare CLASS one
    level DEEPER, as an ELEMENT of some OTHER node's `.args` (a `Mul`/`Add`
    built around it, confirmed live against the crash input), where
    `_walk`'s per-NODE guard never got a chance to run before `reject_
    explosive`'s own code called `.free_symbols` (or any other property)
    directly on that compound node — SymPy's `Basic.free_symbols` property
    getter (`core/basic.py`) blindly iterates `self.args` assuming every
    element is a well-formed `Basic` instance, and `SomeClass.free_symbols`
    (accessed on the CLASS, not an instance) returns the unbound `property`
    object, which `empty.union(*(...))` cannot iterate — `TypeError:
    'property' object is not iterable`, uncaught, crashing `safe_parse`
    instead of refusing.

    Checked ONCE, here, for the WHOLE tree, separately from — and before —
    `reject_explosive`'s own numeric-ceiling scan, so neither pass in that
    function ever calls a property on a node it did not itself construct.
    A bare class reference is a malformed EXPRESSION, not an oversized
    one — `safe_parse`'s own call sites report this as `CATEGORY_
    VALIDATION`, never `CATEGORY_CEILING` (moved out of `reject_explosive`
    itself, whose OWN contract is "a message string is always a ceiling
    finding", precisely so this one distinct category does not have to
    bend that contract).

    Every node `_walk` yields must NOT be a bare Python `type` (a class
    object itself, referenced rather than called or constructed) — a
    `sympy.Basic` instance, the bare `tuple` `_walk` already special-cases,
    or any other ordinary already-computed leaf value a table function's
    own eager `eval()` can legitimately hand back (`bool`/`dict`/`list`/
    plain `int` — see the loop's own comment below for why the check is
    `isinstance(_node, type)` and specifically NOT `not isinstance(_node,
    Basic)`) all pass through unexamined here. A bare `type` is `unknown
    != safe`, fail-closed here exactly as every other finding in this
    module is.
    """
    for _node in _walk(tree):
        if isinstance(_node, tuple):
            continue  # `_walk`'s own documented exception, not a hazard
        # NOT `not isinstance(_node, Basic)` — tried that first, and it
        # rejected every ALREADY-EAGERLY-EVALUATED plain-Python result a
        # heavy function's own `eval()` can hand back as `tree` ITSELF
        # (see this module's own long docstring on `Function.__new__`'s
        # default-`evaluate=True` fallback): `isprime(21)` is a plain
        # `bool`, `factorint(21)` a plain `dict`, `divisors`/`primefactors`
        # a plain `list`, `prime(21)`/`primorial(21)` a plain Python `int`
        # — none of those is a `sympy.Basic` instance, and all four are
        # ordinary, SAFE, already-fully-computed leaves this module's own
        # tables intentionally return. The actual hazard is narrower: a
        # bare CLASS reference (`type` instance — `Pow`, `binomial`, any
        # name `safe_global_dict()`/`_deferred_global_dict()` expose,
        # referenced without being called) is what carries the broken
        # `property`-descriptor `.args`/`.free_symbols` that crashed
        # `reject_explosive`. Every real, legitimate leaf value this
        # module's own functions can produce is an ordinary Python
        # instance, never a class itself.
        if isinstance(_node, type):
            # THE-1095 round-3-follow-up (coordinator review of d3c65c7):
            # the ORIGINAL wording here leaked the Python repr of the
            # class (`<class 'sympy.core.power.Pow'>`) — an internal
            # implementation detail, not something a caller wrote — use
            # the plain name as it would be written in an expression
            # instead (`_node.__name__`, e.g. `'Pow'`).
            return (f"{_node.__name__!r} is a bare reference to a SymPy "
                    "class, not a value — call it or use a number/symbol")
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
    from sympy import Function, Integer, Pow, Rational

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
            # THE-1095 round-3-follow-up #3 (coordinator review of
            # 5e2a961, grok item 1): `_function_arg_cap_violation`
            # walked UNCONDITIONALLY here, the same shape as the `Pow`
            # loop right below it (which exists for the EXACT same
            # reason: `2**1000000000 - 2**1000000000` cancels to 0 at
            # the `_numeric_ceiling_scan` level, but its own inner `Pow`
            # nodes are still a construction-cost hazard on their own,
            # so that scan's own "stop descending once resolved"
            # shortcut cannot be the only check). A fully-cancelling
            # product of two table calls (`factorial(factorial(8))/
            # factorial(factorial(8))`) has the identical shape: cheap
            # to bound as a WHOLE (net value 1), but silent about
            # whether either `factorial(8)` -> `factorial(40320)` child
            # is itself safe to construct — see `_function_arg_cap_
            # violation`'s own docstring for the full account.
            if isinstance(node, Function) and type(node).__name__ in _FUNCTION_ARG_CAPS:
                function_cap_violation = _function_arg_cap_violation(node, memo)
                if function_cap_violation:
                    return function_cap_violation
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
                if not isinstance(exponent, Integer):
                    # A UNIT-FRACTION exponent (1/2, 1/3, ... 1/n for an
                    # integer n >= 2) is exactly the shape `sqrt`/`cbrt`/
                    # `root` compile down to -- `sqrt(x)` IS `Pow(x,
                    # Rational(1, 2))`, with no distinct `Function` node
                    # named "sqrt" ever appearing in the tree at all, so
                    # the `_FUNCTION_ARG_CAPS` lookup that catches a
                    # computed argument to `factorint`-shaped functions
                    # (`_numeric_ceiling_scan`'s `Function`-node branch)
                    # cannot reach this family the same way. THIS branch
                    # is its tree-level backstop instead (#326 finding 1,
                    # cross-vendor review, round 8, Codex): `sqrt(<990-
                    # digit literal>*<990-digit literal>)` bounds fine in
                    # log space alone (the RESULT is only ~990 digits,
                    # comfortably under `MAX_NUMERIC_DIGITS`) but measured
                    # 2.1s to actually COMPUTE -- the same construction-
                    # cost-independent-of-print-profile class the Integer-
                    # exponent branch below exists to catch, just for a
                    # fractional exponent instead of a huge integer one.
                    # A NEGATIVE unit fraction (`x**(-1/2)`, `1/sqrt(x)`)
                    # is left unhandled here -- `exponent.p == 1` excludes
                    # it -- not because it is safe, but because it is
                    # outside this round's two given repros; it still has
                    # `_numeric_ceiling_scan`'s own print-profile bound as
                    # a (looser, not construction-cost-aware) backstop.
                    if (isinstance(exponent, Rational) and exponent.p == 1
                            and exponent.q >= 2):
                        # #326 finding 2 (cross-vendor review, round 9,
                        # grok): read the cap from `_FUNCTION_ARG_CAPS`'s
                        # own "sqrt" row (its "cbrt"/"root" rows share the
                        # identical value by construction -- see that
                        # table's own comment) rather than the bare
                        # `MAX_ROOT_ARG_DIGITS` module constant -- this
                        # shape is reached structurally, by the Pow node's
                        # own exponent, with no function-name token to key
                        # a per-name lookup off (`sqrt(x)` and a bare
                        # `x**(1/2)` in the SOURCE produce the identical
                        # `Pow` either way), so "sqrt" stands in as this
                        # family's canonical row rather than this branch
                        # keeping its own second copy of the cap value.
                        _root_cap = _FUNCTION_ARG_CAPS["sqrt"][1]
                        # THE-1095 round 3 (verify-1095-r2): `_resolve_arg_
                        # magnitude`, not `_log10_num_den` directly, so a
                        # NESTED table-function base (`sqrt(bell(1463))`)
                        # is bounded via `bell`'s own GROWTH bound instead
                        # of falling through to the slow, real `bell(1463)`
                        # construction on the real-dict parse (measured
                        # ~5s) before this branch got a chance to run at
                        # all — see `_table_function_bound`'s own docstring.
                        base_num, base_den, base_res, base_violation = _resolve_arg_magnitude(base, memo)
                        if base_violation:
                            return base_violation
                        if base_res:
                            # #326 finding (cross-vendor review, round 10,
                            # Codex): prefer the EXACT digit count of the
                            # already-materialized `base` over the plain
                            # float-log comparison — see `_digit_count_
                            # over_cap_for_node`'s own docstring for the
                            # boundary bug this closes (an all-9s literal
                            # at EXACTLY the cap, refused on a rounding
                            # error in the float approximation alone).
                            over_cap = (_digit_count_over_cap_for_node(base, "num", base_num, _root_cap)
                                        or _digit_count_over_cap_for_node(base, "den", base_den, _root_cap))
                            if over_cap:
                                _root_name = {2: "square root", 3: "cube root"}.get(
                                    exponent.q, f"{exponent.q}-th root")
                                return (f"the base of a {_root_name} has "
                                        f"more digits than the limit of "
                                        f"{_root_cap}: computing the "
                                        "root is not safely bounded")
                    # A COMPOUND exponent on a numeric base, any other
                    # shape: nothing new for THIS iteration to do. The
                    # scan (above) already accounts for the OUTER combined
                    # print profile (including a cancelling exponent like
                    # `f*f/(f*f)`, via `_factor_multiset`'s exact
                    # cancellation), and any dangerous Pow-with-integer-
                    # exponent node buried INSIDE this compound exponent
                    # gets its OWN separate iteration in this SAME
                    # `_walk`-based loop — see the comment just below for
                    # why that independence from any Mul/Add ancestor's
                    # cancellation is exactly the point of this branch,
                    # not a redundant re-check.
                    continue
                # #326 findings 1 and 2 (cross-vendor review, round 7): a
                # NUMERIC base with a BARE INTEGER exponent is exactly
                # the shape SymPy's own `Mul.flatten` computes EAGERLY
                # (`coeff *= Pow(base, exp)`) during REAL evaluate=True
                # construction — and it does so REGARDLESS of whether an
                # ENCLOSING Mul/Add later cancels the result away.
                # `_numeric_ceiling_scan`'s own "stop descending once a
                # node resolves" rule (needed, correctly, so the
                # DENOMINATOR of a cancelling fraction like `f*f/(f*f)`
                # is never independently refused) means a `Pow` buried
                # inside a STRUCTURALLY-CANCELLING Mul/Add is never
                # independently visited by the scan at all: `1**(2**N *
                # 3**(-N))` never even looks past the unit-base
                # short-circuit (`1 ** anything` resolves to `(0, 0,
                # True)` before the scan's Pow branch touches the
                # exponent), and `2**(2**N * 2**(-N))` resolves its
                # SAME-base exponent to EXACTLY `(0, 0, True)` via
                # `_factor_multiset`'s own cancellation — both leave the
                # inner `Pow(2, N)` (or `Pow(3, -N)`) node completely
                # unchecked by the scan, even though `Mul.flatten` still
                # computes it as an intermediate, for a ~300-digit `N`
                # measured to hang past `guarded_call`'s 10s CPU ceiling.
                #
                # This loop is where that gets closed: `_walk` visits
                # EVERY node unconditionally, with no "stop descending"
                # optimization at all, so it reaches this exact
                # `Pow(2, N)` node as its OWN iteration regardless of
                # what any ancestor Mul/Add does with the result.
                # Bounding it here, via the SAME `_log10_num_den` +
                # `_safe_log10_pow` machinery the scan itself uses (never
                # `base ** exponent` directly), closes the gap without
                # touching the scan's own — still correct, for the
                # print-PROFILE question — cancellation logic at all.
                exp_int = int(exponent)
                abs_exp = abs(exp_int)
                if abs_exp <= 1:
                    # `base**0 == 1`, `base**1 == base` (already
                    # materialized, zero additional work), and `base**-1`
                    # is a formal reciprocal -- SymPy wraps the ALREADY-
                    # materialized `base` as a numerator/denominator swap,
                    # no repeated-squaring-style computation happens at
                    # any of the three. A regression caught fixing this
                    # (round 7, Codex review): `factorial(1463)*
                    # factorial(1463) / (factorial(1463)*factorial(1463))`
                    # is `Mul(f*f, Pow(f*f, -1))` once parsed -- the
                    # DENOMINATOR is a `Pow` with a numeric (non-symbolic)
                    # `Mul` base and exponent `-1`, and without this
                    # skip, checking that Pow's own print profile here
                    # (base_num ~7996 digits, scaled by `abs_exp=1` ->
                    # unchanged) refused it as over-cap in isolation --
                    # the exact round-2/3 mistake this whole file exists
                    # to avoid, reintroduced for the ONE exponent
                    # magnitude (0, 1, -1) that was never actually
                    # expensive to construct in the first place. The
                    # print-PROFILE question for a `Pow` like this one
                    # (is the surrounding combination, cancellation
                    # included, over cap) is the SCAN's job, not this
                    # loop's -- this loop exists only for the
                    # construction-COST question, which does not apply
                    # here at all.
                    continue
                base_num, base_den, base_res = _log10_num_den(base, memo)
                if not base_res:
                    continue  # inconclusive -- a Function call, irrational constant, ...
                # UNSWAPPED regardless of `exponent`'s sign, and always
                # scaled by `abs_exp`, not `exp_int` directly: the genuine
                # computational work for `base**exp_int`, POSITIVE or
                # NEGATIVE, is computing `base**abs_exp` via repeated
                # squaring -- SymPy inverts the result AFTERWARD for a
                # negative exponent, an O(1) numerator/denominator swap
                # with no additional squaring, so checking whether THAT
                # intermediate (not the final, possibly-swapped, printed
                # shape) is over cap is what actually answers "is this
                # expensive to CONSTRUCT" for both signs alike. Swapping
                # `result_num`/`result_den` for a negative exponent (an
                # earlier version of this fix did) reconstructs the FINAL
                # Pow node's own print profile instead — back to checking
                # a subtree's printability in isolation, the same mistake
                # `abs_exp <= 1` above exists to avoid, just for a wider
                # range of exponents.
                result_num = _safe_log10_pow(base_num, abs_exp)
                result_den = _safe_log10_pow(base_den, abs_exp)
                pow_violation = _ceiling_message_num_den(result_num, result_den)
                if pow_violation:
                    return pow_violation
                continue

            # A SYMBOLIC base with a NUMERIC exponent is the one case that
            # IS this loop's own business: not a print-digit ceiling (the
            # result never gets rendered as a decimal — `x**20000` stays
            # symbolic, or gets partially EXPANDED), but a CPU-cost ceiling
            # (`MAX_SYMBOLIC_EXPONENT`; see its own definition above for the
            # measured superlinear cost of `sp.expand`/`sp.factor` on a
            # large symbolic power). Needs the exponent's sign and an
            # UPPER BOUND on its magnitude, WITHOUT ever materializing the
            # exponent's actual value (#326 finding 3, round 4, and finding
            # 1, round 5: an exact `Mul.flatten`-based reconstruction, and
            # later an exact-multiset-or-nothing requirement, both left an
            # `Add` exponent — `(x+1)**(1000+1000)` — unresolved and
            # skipped this whole check entirely).
            #
            # `_factor_multiset` first, when it applies: for a `Mul`/`Pow`-
            # of-`Integer` chain it gives an EXACT sign and (via
            # `_multiset_log_num_den`) an exact magnitude — tighter than
            # the general bound below, and unchanged from round 4. Falls
            # back to `_log10_num_den`'s general (possibly-upper-bound)
            # magnitude for anything `_factor_multiset` cannot decompose
            # (an `Add` exponent, most commonly) — sign-agnostic by
            # design (see `_safe_scale_log10`'s own docstring), so sign
            # comes from SymPy's own assumption system instead
            # (`.is_negative`, cheap — no evaluation, just the expression's
            # own cached/derivable assumptions) with the SAFE default
            # (assume positive, i.e. do not skip the check) when even that
            # is undetermined, matching this ceiling's own "a false
            # refusal is far cheaper than an unbounded `sp.expand`" trade.
            multiset = _factor_multiset(exponent, memo.setdefault("__multiset__", {}))
            if _table_multiset_trusted(multiset):
                terms, exp_sign = multiset
                if exp_sign != 0 and any(v < 0 for v in terms.values()):
                    # Genuinely fractional (a real denominator survives
                    # cancellation) — `sp.expand`/`sp.factor` do not
                    # binomially expand a SYMBOLIC base raised to a
                    # fractional power the way they do an integer one
                    # (`(x+1)**(999999999999/2)` stays unevaluated, no
                    # expansion cost), so there is nothing this COST
                    # ceiling needs to bound here. Unlike the numeric-base
                    # branch above, which finding 1 fixed for the
                    # opposite reason — a fractional exponent CAN still
                    # print a huge integer RESULT there
                    # (`(10**3000)**(3/2)`) — this ceiling is about
                    # expansion cost, not digit count, and a fractional
                    # power is cheap either way.
                    continue
                exp_magnitude, _ = _multiset_log_num_den(terms)
            else:
                # THE-1095 round 3 (verify-1095-r2): `_resolved_exponent_
                # magnitude`, not `_log10_num_den` directly — a bare NESTED
                # table-function exponent (`(x+1)**bell(1463)`) needs its
                # own GROWTH bound here too, for the identical reason the
                # OTHER `_log10_num_den` call site in this same loop (the
                # `sqrt`/`cbrt` base check, above) already switched to it.
                exp_log_num, exp_log_den, exp_res = _resolved_table_aware_magnitude(exponent, memo)
                if not exp_res:
                    continue  # inconclusive -- a Function call, irrational constant, ...
                exp_magnitude = exp_log_num - exp_log_den
                exp_sign = -1 if exponent.is_negative else 1

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
