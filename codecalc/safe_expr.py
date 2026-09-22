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

#: Ceiling on the DIGIT COUNT of a purely numeric power's result. A numeric
#: power is cheap to compute and ruinous to print — Python refuses to render an
#: integer over 4300 digits at all (sys.set_int_max_str_digits), which is how
#: `factorial(99999)` escaped as an uncaught ValueError from deep inside
#: SymPy's printer rather than as a result. This bounds the answer to something
#: that can actually be returned.
#:
#: Moved here (was originally defined much later in the file, next to
#: `math_transforms()`) so `_EXTRA_BOUNDED_POSITIONS`'s own module-level
#: dict literal, below, can reference it directly for `N`'s own precision
#: position — #326 finding (coordinator round 11 replay of 9fe4f6a, Codex
#: round 9 probe 2a) — rather than a second, drift-prone constant with the
#: same value.
MAX_NUMERIC_DIGITS = 4_000

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
#: #326 finding B (Codex, round 9 review of 5119c9d, `verify-1095-r7.log`,
#: finding 2 — High, "position-complete spec"): SymPy 1.14's own optional
#: SECOND positions on eight more table names each drive the RESULT's own
#: magnitude and cost exactly the same way `rf`/`ff`'s `k` already does —
#: audited live via `inspect.signature(real.eval)` for every name in
#: `_FUNCTION_ARG_CAPS` — `bell(n, k_sym)` (the `n`-th Bell/Touchard
#: POLYNOMIAL evaluated at `k_sym`), `bernoulli(n, x)`/`euler(n, x)`/
#: `genocchi(n, x)` (the corresponding POLYNOMIAL at `x`), `fibonacci(n,
#: sym)`/`tribonacci(n, sym)` (ditto), `harmonic(n, m)` (the GENERALIZED
#: harmonic number, order `m`), `zeta(s, a)` (the HURWITZ zeta's second
#: parameter) — none of these had ANY bound on their own second position
#: before this round: `bell(1463, factorial(1463))`, `harmonic(1463,
#: -factorial(8))`, `zeta(-1463, 1463)` all hang past several seconds.
#: Capped at `MAX_HEAVY_ARG`, the SAME flat "value" cap `divisor_sigma`'s
#: own `k` (position 1) already uses — each of these eight names' own
#: GROWTH formula (see `_GROWTH_BOUNDS`, below) is ALSO made two-position-
#: aware so the combined output of a call with BOTH positions individually
#: under this same flat cap (`bell(1463, 1463)`, still comfortably within
#: EACH position's own limit) is still proven safe BEFORE real
#: construction — see `_table_function_growth_violation`'s own docstring
#: for why a flat per-position cap alone is not, on its own, sufficient
#: for a two-position name the way it always was for a one-position one.
_EXTRA_BOUNDED_POSITIONS: dict = {
    "rf": {1: ("value", MAX_HEAVY_ARG)},
    "ff": {1: ("value", MAX_HEAVY_ARG)},
    "polygamma": {1: ("value", MAX_HEAVY_ARG)},
    "divisor_sigma": {1: ("value", MAX_HEAVY_ARG)},
    "nextprime": {1: ("value", MAX_ITH_PRIME_SKIP)},
    "bell": {1: ("value", MAX_HEAVY_ARG)},
    "bernoulli": {1: ("value", MAX_HEAVY_ARG)},
    "euler": {1: ("value", MAX_HEAVY_ARG)},
    "fibonacci": {1: ("value", MAX_HEAVY_ARG)},
    "genocchi": {1: ("value", MAX_HEAVY_ARG)},
    "harmonic": {1: ("value", MAX_HEAVY_ARG)},
    "tribonacci": {1: ("value", MAX_HEAVY_ARG)},
    "zeta": {1: ("value", MAX_HEAVY_ARG)},
    # #326 finding (coordinator round 11 replay of 9fe4f6a, Codex round 9
    # probe 2a): `N(x, n)`'s own second argument, `n`, is the requested
    # PRECISION in decimal digits, unbounded on `origin/main` itself
    # (`N(pi, 100000)` renders a 100_001-character `Float` in ~1.5s) --
    # capped at `MAX_NUMERIC_DIGITS`, the same ceiling this module
    # already enforces on every other rendered result, so a precision
    # this module could never hand back anyway is refused before real
    # construction rather than after. `N`'s own position 0 (the
    # expression to approximate) is deliberately NOT given a "value"/
    # "digits" cap here -- an arbitrarily large but otherwise legitimate
    # RESOLVABLE expression is fine to approximate; the hazard is
    # entirely in the PRECISION requested, not the expression's own
    # size (which the OUTPUT digit-count check, `safe_parse`'s own
    # Float-`_prec` branch, still catches for the fully-constructed
    # result either way).
    "N": {1: ("value", MAX_NUMERIC_DIGITS)},
}
#: name -> frozenset of positions that are members of `_HEAVY_FUNCTIONS`'
#: usual "value"-kind position-0 table by construction, but this specific
#: name's own argument at this position is EXPLICITLY exempt from any cap
#: (see `binomial`'s own comment above) — read by both the token screen and
#: the tree scan so neither accidentally reintroduces the false refusal the
#: OTHER one was fixed to avoid.
#:
#: #326 finding B (round 9): also where a POSITION-COMPLETE spec records
#: "audited, no numeric hazard, deliberately unbounded" for a position
#: that is neither `binomial`'s own `k` shape (an O(1) short-circuit) nor
#: a plain skip candidate — `bell`'s own THIRD position (`symbols`) is a
#: LIST of `Symbol` objects passed to `_bell_incomplete_poly`, never a
#: magnitude to bound at all (a numeric value there does not match
#: `bell.eval()`'s own expected shape and stays symbolic on `origin/
#: main` too — nothing this module's cap machinery needs to touch).
_UNBOUNDED_POSITIONS: dict = {
    "binomial": frozenset({1}),
    "bell": frozenset({2}),
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


#: #326 finding C (Codex, round 9 review of 5119c9d, `verify-1095-r7.log`,
#: finding 3 — High, "domains"): a growth/cap formula is only a valid
#: UPPER BOUND on the domain it was derived for. `_growth_2n`'s own
#: `binomial(n, k) <= 2**n` assumes `n >= 0` — `binomial`'s own `eval()`
#: switches to a DIFFERENT, unaudited code path for a negative `n` (the
#: generalized identity `C(n,k) = (-1)**k * C(k-n-1, k)`), whose
#: magnitude this module has never derived a bound for at all —
#: `binomial(-100, 1000)`'s true log10 magnitude (~143.11) already
#: exceeds the OLD formula's own claim (~30.10, computed from `|n|=100`
#: as if it were non-negative, itself already wrong on its face before
#: even considering the identity switch), and `binomial(-1463, 1000000)`
#: passed the deferred screen and hung. `origin/main` itself DOES accept
#: a negative `n` (`binomial(-5, 3) == -35`, verified live) — refusing
#: this domain is a deliberate, documented divergence from main for a
#: shape this module has not (yet) derived a sound bound for, the same
#: "unknown != safe" bar this module already holds every other
#: unresolvable-but-numeric shape to, not a claim that main itself
#: rejects it.
#:
#: name -> frozenset of positions that must resolve to a NON-NEGATIVE
#: value (an exact integer is not required here — `binomial`'s `eval()`
#: itself only branches on the SIGN of `n`, not its exact type) or the
#: call is refused as a domain violation.
#: `harmonic`'s own `m` (position 1) joins this table for a DIFFERENT
#: reason than `binomial`'s `n`: `_resolve_arg_magnitude`'s entire
#: `bounds` convention stores a MAGNITUDE (`arg_log_num - arg_log_den`,
#: sign discarded — the same "abs value" treatment this module already
#: gives a plain `Integer` throughout), never a signed value, so a
#: growth formula reading `bounds.get(1)` back via `_safe_pow10` cannot
#: tell a `m = -1463` from a `m = 1463` — both arrive as the SAME
#: magnitude. `harmonic(n, m)` for a negative `m` is `sum_{k=1}^n
#: k**|m| <= n**(|m|+1)`, a GENUINELY different (much larger) growth
#: shape than the classical `m >= 0` case's own `~ln(n)+1` — silently
#: reading `bounds.get(1)`'s magnitude as if it were always non-negative
#: `m` left `harmonic(1463, -1463)` hanging even after `_growth_
#: harmonic` was made two-position-aware (a sign-blind formula computed
#: the TINY classical bound for what was actually the LARGE negative-`m`
#: shape). Refusing the domain outright — the same "unknown != safe"
#: tradeoff `binomial`'s own negative `n` and `polygamma`'s own `|z| < 1`
#: already make — sidesteps needing to thread a SIGNED value through a
#: `bounds` dict this module's whole magnitude-resolution machinery
#: was built around carrying only a magnitude in. `origin/main` DOES
#: accept a negative `m` (`harmonic(10, -10) == 14914341925`) — a
#: deliberate, documented divergence, not a claim that main rejects it.
_NONNEGATIVE_DOMAIN_POSITIONS: dict = {
    "binomial": frozenset({0}),
    "harmonic": frozenset({1}),
}

#: #326 finding (grok, round 12 review of 4d94871, `verify-1095-r10-
#: grok.log`): `digamma`/`gamma`/`loggamma`/`polygamma` used to share
#: THIS table (rounds 9 and 11's own fix), restricting `z`'s TOP-LEVEL
#: magnitude to `>= 1`. Two problems: it only checked MAGNITUDE, never
#: SIGN (`gamma(-1 - 10**-6)`, magnitude ~1.000001 >= 1, PASSED right
#: next to the pole at `z = -1`) — the identical bug class already named
#: for `_growth_zeta`; and separately, `origin/main` itself essentially
#: NEVER performs a genuinely unbounded computation for a bare,
#: TOP-LEVEL call to any of these four at an arbitrary exact rational —
#: confirmed live: `gamma(-1-10**-6)`, `zeta(1+10**-6)`, `gamma(1/2)`,
#: `digamma(1/2)` (which REWRITES to `polygamma(0, 1/2)` at real
#: construction — the module's own long-documented rename quirk) all
#: either stay symbolic instantly or compute a small, fast, exact
#: closed form; even `polygamma(1463, z)` at a large ORDER only
#: computes for a `z` SymPy already has a closed form for (an integer,
#: or a handful of special rationals like `1/2`), and every such case
#: measured is bounded, fast, and well under `MAX_NUMERIC_DIGITS` (its
#: own position-0 "value" cap on the ORDER, unaffected by anything
#: here, is what actually keeps that bounded — `_HEAVY_FUNCTIONS`'
#: existing `MAX_HEAVY_ARG` entry). There never was a genuine TOP-LEVEL
#: `z`-driven hazard for any of these four to guard against — the REAL
#: hazard is entirely in a WRAPPER (`floor`/`ceiling`/`factorial`'s own
#: argument position) that forces a numeric coercion on the result,
#: handled instead by `_pole_sensitive_magnitude`'s own EXACT-value-
#: gated domain checks, which apply uniformly to the NESTED case
#: regardless of which wrapper does the coercing. Removed here (top
#: level) entirely for all four — `gamma(1/2) == sqrt(pi)`,
#: `digamma(1/2)`, `loggamma(1/2)`, `gamma(-1.5)` (away from any pole),
#: and `polygamma(2, -1.5)` (stays symbolic on main) all evaluate
#: exactly as main does again.
#:
#: name -> frozenset of positions that must resolve to a value with
#: magnitude >= 1 (`log10(|value|) >= 0`) or the call is refused.
_MAGNITUDE_AT_LEAST_ONE_DOMAIN_POSITIONS: dict = {}


def _domain_violation(name: str, position: int, arg, arg_log_num: float, arg_log_den: float) -> str | None:
    """Reason `arg` (already known non-symbolic, already resolved to
    `(arg_log_num, arg_log_den)` via `_resolve_arg_magnitude`) falls
    outside `name`'s own declared domain at `position`, or `None` if
    `position` has no domain rule at all, or `arg` satisfies the one it
    has. Called from BOTH `_function_arg_cap_violation` and `_table_
    function_bound`'s own per-position loops, alongside their existing
    cap check — a domain violation is checked FIRST, since a value the
    growth formula was never derived for is not safely comparable to a
    cap at all (see `_NONNEGATIVE_DOMAIN_POSITIONS`'s and `_MAGNITUDE_
    AT_LEAST_ONE_DOMAIN_POSITIONS`'s own comments for why each exists).
    """
    if position in _NONNEGATIVE_DOMAIN_POSITIONS.get(name, ()) and arg.is_negative:
        return (f"the argument to {name}() at position {position} must be "
                "non-negative: this module has not derived a safe bound "
                "for that domain")
    if (position in _MAGNITUDE_AT_LEAST_ONE_DOMAIN_POSITIONS.get(name, ())
            and (arg_log_num - arg_log_den) < 0):
        return (f"the argument to {name}() at position {position} must have "
                "magnitude >= 1: this module has not derived a safe bound "
                "for that domain")
    return None


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
    """`harmonic(n)` (`m` absent, or `m >= 0`) `<= ln(n) + 1` — the
    ordinary harmonic series' own logarithmic growth, always tiny.
    `harmonic(n, 0) == n` exactly. A NEGATIVE `m` is refused outright as
    a domain violation before this formula is ever reached — see
    `_NONNEGATIVE_DOMAIN_POSITIONS`'s own comment on `"harmonic"` for
    why (the `bounds` dict this formula reads carries only a MAGNITUDE,
    never a sign, so `m = -1463` and `m = 1463` are indistinguishable
    here — `harmonic(n, m)` for `m < 0` is `sum_{k=1}^n k**|m| <=
    n**(|m|+1)`, a genuinely different, much larger growth shape than
    this formula's own `m >= 0` case, and could not be told apart from
    it through this dict even if a formula here tried)."""
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    m_log = bounds.get(1)
    if m_log is not None:
        m = _safe_pow10(m_log)
        if m is None:
            return math.inf
        if m == 0:
            return math.log10(n) if n > 0 else 0.0
        # m >= 1 (the domain check upstream already ruled out m < 0):
        # the classical generalized harmonic number, bounded the SAME
        # logarithmic way as the ordinary (m absent) case below — H(n,
        # m) <= H(n, 1) for any m >= 1, n >= 1.
    if n <= 1:
        return 1.0
    return math.log10(math.log(n) + 1) if math.log(n) + 1 > 1 else 0.0


def _growth_polygamma(bounds: dict) -> float:
    """`polygamma(order, z)` at order 0 (`digamma`) is `harmonic(z-1) -
    EulerGamma`, itself tiny (logarithmic in `z`) for `z >= 1`, and
    `~ -1/z` near the pole at `z = 0` for ANY `z` — bounded either way
    by `max(log10(log(z)+2), log10(1/z))`. For `order >= 1`, `polygamma(
    order, z) = (-1)**(order+1) * order! * zeta(order+1, z)` exactly,
    and `zeta(s, z) <= zeta(2) < 2` for any `z >= 1`, `s >= 2` (#326
    finding B, Codex round 9: the OLD formula ignored `order` entirely,
    reading `z` alone — `polygamma(1463, 1)`'s true log10 magnitude is
    ~3997.36, the OLD formula claimed ~1) — but that identity needs
    `z >= 1` to hold, so `order >= 1` with `z < 1` refuses (`inf`) as a
    domain this module has not derived a safe bound for, the same "an
    unknown, not merely a magnitude, is what a pole-sensitive formula
    needs" bar `_pole_sensitive_magnitude` already holds `gamma`/
    `digamma`/`loggamma` to.

    #326 finding (grok, round 12 review of 4d94871): `order = 0` (a bare
    `digamma` call, which REWRITES to `polygamma(0, z)` at real
    construction regardless of `evaluate=False` — the module's own long-
    documented rename quirk) used to ALSO refuse outright for `z < 1`
    (`math.inf`, unconditionally) — wrong: `digamma(1/2)` (main:
    `-2*log(2) - EulerGamma`, tiny) hit this via the TOP-LEVEL
    `_table_function_growth_violation` walk, which reads THIS formula
    directly (not `_pole_sensitive_magnitude`'s own, now-EXACT-value-
    gated `digamma` branch — that branch is only reached when `digamma`
    is NESTED, before the real-construction rewrite happens). Order 0
    (or an unresolved/absent order — the same "no evidence of a large
    order" default `digamma_term` already covers) now gets the SAME
    logarithmic-or-near-pole bound regardless of `z`'s own side of 1,
    since `digamma` genuinely has no factorial-scale blowup at ANY
    finite `z` — only order >= 1's own Bernoulli-zeta identity does,
    and ONLY once `z >= 1` is confirmed.
    """
    z = _safe_pow10(bounds.get(1))
    if z is None:
        return math.inf
    digamma_term = (math.log10(math.log(z) + 2) if z > 1
                     else max(-math.log10(z), 0.0) + 0.5 if z > 0
                     else math.inf)
    if bounds.get("order_exact_zero"):
        # #326 finding (grok, round 12 review of 4d94871): a caller that
        # resolved `order` EXACTLY (`_pole_sensitive_magnitude`'s own
        # `polygamma` branch) and found it genuinely `0` sets this flag
        # — `_safe_pow10(bounds.get(0))` cannot itself distinguish
        # `order = 0` from `order = 1` (both round-trip through this
        # module's own "exact zero maps to magnitude 0.0" convention to
        # `1.0`), so a caller that already knows the TRUE value bypasses
        # that ambiguity here directly rather than feeding it back
        # through the lossy magnitude round-trip.
        return digamma_term
    order_log = bounds.get(0)
    if order_log is None:
        return digamma_term
    order = _safe_pow10(order_log)
    if order is None or order < 1:
        return digamma_term
    if z < 1:
        return math.inf  # order >= 1's own Bernoulli-zeta identity needs z >= 1
    # `order! * zeta(order+1, z)`, `zeta(...) < 2` for `z >= 1` -- a
    # `log10(2)` safety margin on top of the factorial term covers it.
    factorial_term = _growth_stirling_factorial({0: order_log}) + math.log10(2)
    return max(digamma_term, factorial_term)


def _growth_poly_second_arg(base_growth, degree_scale: float = 1.0):
    """Wraps a single-position growth function for a table name whose
    OPTIONAL second position evaluates an `n`-degree(ish) polynomial or
    generating function AT that point (#326 finding B, Codex round 9,
    "position-complete spec"): `bell`'s `k_sym` (the Bell/Touchard
    POLYNOMIAL evaluated at `k_sym`), `bernoulli`/`euler`/`genocchi`'s
    `x` (the corresponding POLYNOMIAL at `x`), `fibonacci`/`tribonacci`'s
    `sym` (ditto). The polynomial's own COEFFICIENTS are already bounded
    by `base_growth` (the single-arg sequence itself — Bell/Bernoulli/
    Euler/Genocchi numbers, or `fibonacci`/`tribonacci` themselves), and
    evaluating a degree-`~degree_scale*n` polynomial at a point `x`
    scales that by `|x| ** (degree_scale*n)` in the worst case (every
    coefficient landing on the single highest-degree term) — deliberately
    loose (never claims tighter than that), but always a safe upper
    bound: `log10(coefficient bound) + degree_scale*n*log10(max(1,
    |x|))`. `bell(2, 1000)`'s true log10 magnitude is ~6.0, this gives
    `log10(2!) + 1*2*log10(1000) = 0.301 + 6 = 6.301` — safe, and no
    longer the OLD single-position formula's own claim of ~0.3."""
    def growth(bounds: dict) -> float:
        base = base_growth(bounds)
        x_log = bounds.get(1)
        if x_log is None:
            return base  # single-arg call -- unaffected
        n = _safe_pow10(bounds.get(0))
        if n is None:
            return math.inf
        x = _safe_pow10(x_log)
        if x is None:
            return math.inf
        return base + degree_scale * n * math.log10(max(1.0, x))
    return growth


def _growth_2_factorial(bounds: dict) -> float:
    """`|x(n)| <= 2 * n!` — the standard asymptotic envelope for Euler,
    Bernoulli, and Genocchi numbers (`|E_n|, |B_n|, |G_n| = O(n! /
    (2*pi)**n)`, comfortably under `2*n!` for every practical `n`).
    #326 finding E (Codex round 9, "tight bounds instead of a loose
    catch-all"): replaces the far looser `n**(2n)` shared catch-all
    these three used to fall into — `1 << euler(4)` (`origin/main`: `32`) used
    to claim an absurd ~19_729-"digit" shift count from that catch-all;
    this gives a claim close to the true ~1.5 digits instead."""
    return _growth_stirling_factorial(bounds) + math.log10(2)


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


def _growth_fibonacci_base(bounds: dict) -> float:
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


_growth_tribonacci_base = _growth_base_n(math.log10(2))  # same base as _growth_2n
_growth_catalan = _growth_base_n(math.log10(4))
#: #326 finding E (Codex round 9, "tight bounds instead of a loose
#: catch-all"): `motzkin(n) <= 3**n` (the standard envelope — Motzkin numbers grow
#: strictly slower than the central trinomial coefficient) replaces the
#: far looser `n**(2n)` shared catch-all `motzkin` used to fall into —
#: `1 << motzkin(...)`-shaped compositions no longer claim an absurd
#: shift count for a small, in-range `motzkin` value.
_growth_motzkin = _growth_base_n(math.log10(3))

#: #326 finding B (Codex round 9, "position-complete spec"): `fibonacci`/
#: `tribonacci`'s own OPTIONAL second position (`sym`) evaluates the
#: corresponding POLYNOMIAL at that point — see `_growth_poly_second_
#: arg`'s own docstring. `tribonacci(n, x)`'s own degree is `~2*(n-1)`
#: (`tribonacci(5, t) == t**8 + 3*t**5 + 3*t**2`, degree 8 for n=5); `2 *
#: n` is used as a safe, round overestimate of that.
_growth_fibonacci = _growth_poly_second_arg(_growth_fibonacci_base, 1.0)
_growth_tribonacci = _growth_poly_second_arg(_growth_tribonacci_base, 2.0)


def _growth_totient(bounds: dict) -> float:
    """`totient(n) <= n` — Euler's totient never exceeds its own argument."""
    n = _safe_pow10(bounds.get(0))
    if n is None:
        return math.inf
    return math.log10(n) if n > 0 else 0.0


def _growth_gamma(bounds: dict) -> float:
    """`Gamma(z) <= ceil(z)!` for `z >= 1` — Stirling's own envelope,
    still a safe upper bound across Gamma's own dip below 1 on `[1, 2]`
    (its minimum, `Gamma(1.46...) ~= 0.8856`, is comfortably under
    `ceil(1.46)! = 2! = 2`), verified against SymPy across a grid
    spanning `z` from `1` to `1463` (integer and rational). #326 finding
    (grok, round 11 addendum to coordinator replay of 9fe4f6a): `gamma`/
    `loggamma` used to share the loose `n**(2n)` catch-all, which reads
    `n <= 1 -> 0.0` — backwards for a function whose OWN pole is AT `z =
    0`, not `z = 1` (`Gamma(z) ~ 1/z` as `z -> 0`) — `factorial(floor(
    gamma(1e-6)))` used to pass every screen with a claimed bound of
    `0.0` when the true magnitude is `~6`. This formula is only ever
    reached once `z >= 1` is already enforced separately (`_MAGNITUDE_
    AT_LEAST_ONE_DOMAIN_POSITIONS`'s own `"gamma"`/`"loggamma"` rows,
    checked BEFORE any growth formula for this position) — `z < 1` never
    reaches this function at all for `gamma`'s/`loggamma`'s own bounded
    position, so no separate fallback for that regime is needed here.
    `loggamma`'s own output (`ln(Gamma(z))`) is far SMALLER than
    `Gamma(z)` itself for any `z` — this same, much looser bound is
    still SOUND for it, just more conservative than a dedicated formula
    would be (no pinned case needs tighter).

    `z - 1e-9` before `ceil`, not `z` directly: `_safe_pow10(log10(5))`
    round-trips to `5.000000000000001` (ordinary float error), and a
    bare `math.ceil` on that returns `6`, not `5` — silently borrowing
    an extra Stirling step for EVERY integer `z` and pushing `gamma(5)`
    (true magnitude ~1.38) to a claimed ~3.70, over `factorial`'s own
    `MAX_HEAVY_ARG` cap when nested (`factorial(floor(gamma(5)))`, a
    plain `24!` on `origin/main`, was falsely refused by exactly this
    before the epsilon — caught by this file's own property-style
    verification, not by inspection)."""
    z = _safe_pow10(bounds.get(0))
    if z is None:
        return math.inf
    if z < 1:
        return 1.0  # domain-restricted before this is ever reached; defensive fallback
    n = math.ceil(z - 1e-9)
    return _growth_stirling_factorial({0: math.log10(n)})


def _growth_zeta(bounds: dict) -> float:
    """`zeta(s)`'s own magnitude, for a NESTED `zeta` call (`bounds`
    carries only `s`'s MAGNITUDE, never its sign — see this function's
    own history below for why that matters).

    #326 finding (grok, round 11 addendum to coordinator replay of
    9fe4f6a): the PREVIOUS version of this formula tested `_safe_pow10(
    bounds.get(0)) >= 2` — since `bounds[0]` is `log10(|s|)` (this
    module's own established, module-wide "magnitude, sign discarded"
    convention — see `_log10_num_den`'s own `Integer` handling and
    `_resolve_arg_magnitude`'s own Mul composition for the SAME
    convention applied elsewhere), `_safe_pow10(bounds[0])` recovers
    `|s|`, NOT `s` — so this test was actually `|s| >= 2`, true for
    `s = -1463` exactly as much as `s = 1463`. `zeta(-1463)` (a real,
    ~2852-digit value on `origin/main`, PINNED as evaluating at the
    module's own top-level `MAX_HEAVY_ARG` cap in an earlier round) was
    silently bounded at `log10(2)` by this formula whenever NESTED
    (`factorial(floor(zeta(-1463)))`, `1 << floor(Abs(zeta(-1463)))`),
    passing every downstream check and reaching real, unbounded
    construction.

    Fixed by DROPPING the small-bound fast path entirely rather than
    trying to recover `s`'s sign from a magnitude-only `bounds` dict (the
    STRUCTURAL fix grok asked for: a formula must not be trusted with
    sign/domain information it cannot actually have) — `zeta(s)`'s
    magnitude is now ALWAYS bounded via the conservative `n! / (2*pi)**n`
    Bernoulli-number envelope, using `n = |s|` (round 9's own formula,
    unchanged, still sound for a negative `s`), REGARDLESS of `s`'s true
    sign: for `s >= 2` (where `zeta(s) < 2` is the true, tight bound),
    this envelope is merely LOOSER, never unsafe (`2!/( 2*pi)**2 ~=
    0.05`-scale terms only shrink the envelope for small `n`, and the
    formula's own `max(base, 0.0)` floor keeps it non-negative either
    way) — soundness, not tightness, is what a magnitude-only input can
    ever promise here. The TOP-LEVEL, non-nested case (`zeta(s, a)` as
    the caller's own whole expression) still gets the TIGHT `s >= 2`
    treatment where it is actually safe to assume — `_zeta_two_arg_
    domain_violation` inspects the REAL, signed `s` node directly, not
    a magnitude, and is checked BEFORE this formula is ever reached for
    that shape.

    `a` (position 1, the Hurwitz zeta's second parameter — #326 finding
    B) is folded in the same polynomial-at-a-point way `_growth_poly_
    second_arg` already handles for bell/bernoulli/euler/genocchi:
    `zeta(s, a) ~ a**(-s)` dominates for `a >= 1`, so `|s| *
    log10(max(1, a))` is a safe additive term.
    """
    s = _safe_pow10(bounds.get(0))
    if s is None:
        return math.inf
    n1 = abs(s) + 1
    if n1 < 2:
        base = 1.0
    else:
        base = (_growth_stirling_factorial({0: math.log10(n1)})
                 - n1 * math.log10(2 * math.pi) + math.log10(4))
        base = max(base, 0.0)
    a_log = bounds.get(1)
    if a_log is None:
        return base
    a = _safe_pow10(a_log)
    if a is None:
        return math.inf
    return base + abs(s) * math.log10(max(1.0, a))


def _growth_pass_through_arg0(bounds: dict) -> float:
    """`f(arg) <= arg` in magnitude — #326 finding E (Codex round 9):
    `root`'s own OUTPUT never exceeds its RADICAND's own magnitude (an
    n-th root of `arg`, `n >= 1`, is never larger than `arg` itself for
    `|arg| >= 1`; for `|arg| < 1` it only shrinks further toward 0) —
    position 0's own already-resolved bound, unchanged, is a safe upper
    bound on the OUTPUT too. `sqrt`/`cbrt` never reach `_table_function_
    bound` at all (they compile to a `Pow`, never a `Function` node named
    after themselves — see `MAX_ROOT_ARG_DIGITS`'s own comment), so only
    `root` needs this entry."""
    v = bounds.get(0)
    return v if v is not None else math.inf


def _growth_tiny_bounded(max_abs: float):
    """Builds a `bounds -> log10(upper bound)` growth function for a name
    whose result is ALWAYS a small, FIXED-magnitude value regardless of
    its argument — #326 finding E (Codex round 9): `mobius(n)` is always
    in `{-1, 0, 1}`; `isprime(n)` is always `True`/`False` (`0`/`1` under
    arithmetic). Neither had a `_GROWTH_BOUNDS` entry at all before this
    round, so `mobius(5) % 3` / `isprime(5) << 1` (both evaluate on
    `origin/main`, `2` either way) refused with "no growth estimate is
    defined for it" — position 0's own bound is irrelevant to the OUTPUT
    here, unlike every other entry in this table."""
    log_bound = math.log10(max_abs) if max_abs > 0 else 0.0

    def growth(bounds: dict) -> float:
        return log_bound
    return growth


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
#: #326 finding B (Codex round 9): the three POLYNOMIAL-at-a-point
#: names (`bernoulli`/`euler`/`genocchi`'s own `x`) and `bell`'s own
#: `k_sym`, wrapped via `_growth_poly_second_arg` so their SECOND
#: position feeds the bound too, not just their first.
_growth_bell = _growth_poly_second_arg(_growth_stirling_factorial, 1.0)
_growth_bernoulli = _growth_poly_second_arg(_growth_2_factorial, 1.0)
_growth_euler = _growth_poly_second_arg(_growth_2_factorial, 1.0)
_growth_genocchi = _growth_poly_second_arg(_growth_2_factorial, 1.0)

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
    "factorial": _growth_stirling_factorial, "bell": _growth_bell,
    "bernoulli": _growth_bernoulli, "euler": _growth_euler,
    "genocchi": _growth_genocchi,
    "zeta": _growth_zeta,
    "motzkin": _growth_motzkin,
    # #326 finding E (Codex round 9): `factorial2`/`subfactorial` are
    # both provably `<= n!` (a double factorial is a strict SUBPRODUCT
    # of every-other-integer, far smaller than `n!`; a derangement count
    # is `n! * sum_{k=0}^n (-1)**k/k! < n!` always) -- the shared bare-
    # `n!` bound (`_growth_stirling_factorial`, unwrapped -- neither
    # takes a second position) replaces the far looser `n**(2n)` catch-
    # all these two used to share.
    "factorial2": _growth_stirling_factorial,
    "subfactorial": _growth_stirling_factorial,
    "root": _growth_pass_through_arg0,
    "mobius": _growth_tiny_bounded(1.0),
    "isprime": _growth_tiny_bounded(1.0),
    # #326 finding (grok, round 11 addendum): `gamma`/`loggamma` pulled
    # OUT of the shared `n**(2n)` catch-all into their own domain-aware,
    # pole-correct formula — see `_growth_gamma`'s own docstring.
    "gamma": _growth_gamma,
    "loggamma": _growth_gamma,
    "andre": _growth_nn_loose,
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
#: #326 finding (coordinator round 11 replay of 9fe4f6a, Codex round 9
#: probe 2a): `N` bounds ONLY its position-1 precision argument
#: (`_EXTRA_BOUNDED_POSITIONS`, above) — it has no position-0 "value"/
#: "digits" cap of its own, so it is not a `_FUNCTION_ARG_CAPS` key at
#: all, and the line below alone would have left it OUT of this set —
#: `N` is ALSO a plain, eager callable (like `factorint`/`primorial`/...
#: above), so without a stand-in its real precision argument would be
#: consumed for real during THIS SAME deferred pre-parse, defeating the
#: whole point of bounding it. `| frozenset(_EXTRA_BOUNDED_POSITIONS)`
#: folds in any name with an EXTRA bounded position regardless of
#: whether it also has a position-0 one — a no-op for every OTHER name
#: already in `_FUNCTION_ARG_CAPS` (`rf`, `ff`, `polygamma`, ... are all
#: already members via the line below on their own), so `N` is the only
#: name this actually changes anything for today.
_DEFERRED_STANDIN_NAMES = (
    frozenset(_FUNCTION_ARG_CAPS) | frozenset(_EXTRA_BOUNDED_POSITIONS)
) - {"sqrt", "cbrt"}
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
            UnicodeEncodeError, UnicodeDecodeError, SystemError) as exc:
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
        #
        # THE-1095 round 13 follow-up (60s atheris pass against fad081f/
        # 72e951b): `SystemError` is a FOURTH escaping shape, found the
        # same way as the two `Unicode*` ones above — CPython 3.12's C
        # tokenizer (`_generate_tokens_from_c_tokenizer`) raises a bare
        # `SystemError: <built-in method __new__ of type object at
        # 0x...> returned a result with an exception set` for one
        # narrow embedded-NUL-byte shape (`'   *AAA\n/\x00\x00'`) —
        # confirmed this is CPython's own tokenizer implementation
        # leaking an internal C-level failure, not this module's own
        # bug, and confirmed it is Python-VERSION specific (3.14's
        # tokenizer converts the identical input to a clean
        # `TokenError` instead). `str(SystemError(...))` is a raw
        # memory address with no information about the INPUT at all —
        # must never reach a caller verbatim, the same "never leak
        # implementation detail" bar this file's own parse-exception
        # handling already holds elsewhere (see `safe_parse`'s own
        # exception-relay sites). `origin/main`'s own text for the
        # underlying condition — a NUL byte in the source, which every
        # OTHER Python version's tokenizer (and even 3.12's, for a
        # DIFFERENT NUL-byte shape — confirmed live: `'\x00'` and
        # `'2+\x002'` both raise a clean `TokenError` even on 3.12)
        # reports as `TokenError: ('source code cannot contain null
        # bytes', ...)` — is substituted here, so every tokenizer
        # failure, on every Python version, reports the identical
        # wording for the identical underlying condition.
        if isinstance(exc, SystemError):
            exc = "source code cannot contain null bytes"
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


def _standin_refuses_evalf(self, prec):
    """`_eval_evalf` for EVERY deferred stand-in class — always refuses
    (`None`, SymPy's own "I don't know how" signal), never delegating to
    `Function`'s own generic implementation.

    #326 finding (coordinator round 10 replay of 6e72d70, probe 2):
    `Function._eval_evalf`'s generic fallback (`sympy/core/function.py`)
    looks up an mpmath routine by NAME (`getattr(mpmath, self.func.
    __name__)`, via `MPMATH_TRANSLATIONS` for a few), not by CLASS
    IDENTITY — so a stand-in named `"polygamma"` (this module builds
    each stand-in class with the SAME NAME as the real SymPy class it
    stands in for, deliberately, so error text and this scan's own
    `type(node).__name__` lookups stay correct) resolves to the REAL
    `mpmath.psi`/`mpmath.polygamma` the INSTANT anything calls `.evalf()`
    on it — completely bypassing the "inert, never computes" property
    every OTHER part of this module relies on. `floor(polygamma(1463,
    1))` measured ~18-19s THIS WAY, not from `floor`'s own construction
    cost (bare `sympy.floor(sympy.polygamma(1463, 1))`, already
    constructed, is ~0.15-0.2s) but because `floor.eval()`'s own
    `get_integer_part()` needs a numeric approximation to decide the
    integer boundary, calls `.evalf()` on its (stand-in) argument DURING
    THE DEFERRED SCAN ITSELF (`floor` is not in `EvaluateFalseTransformer
    .functions`'s whitelist — confirmed live against sympy 1.14's own
    parser source — so it is NOT `evaluate=False`-protected the way
    `Abs`/`sign` are, and constructs eagerly exactly like every other
    un-whitelisted function this module has already had to special-case),
    landing on the REAL, expensive `mpmath.psi` at ~1464 digits of
    precision purely because of a NAME COLLISION with the real class.

    This affects EVERY stand-in, not just `polygamma` — `floor(zeta(
    1464))`, `ceiling(gamma(1463))`, `Max(digamma(1463), 1)`, any
    numeric-coercing wrapper around any `_FUNCTION_ARG_CAPS` name, all
    share the identical bypass. Fixed ONCE, here, for every stand-in this
    module ever builds, rather than gating each CONSUMER (`floor`/
    `ceiling`/`Max`/`Min`/`sign`) against it individually — the
    consumer-side "cheap argument" gate (see `_evalf_coercion_cheap`)
    still exists as a SEPARATE, independent layer for arguments that
    were never a stand-in to begin with (a bare `NumberSymbol` `Pow`,
    say), but this closes the STAND-IN half of the hazard at its root:
    with `.evalf()` refused, `floor.eval()`'s own `get_integer_part()`
    gets `None` back and gives up gracefully, leaving `floor(...)`
    correctly UNEVALUATED during the scan — no different from any other
    inert stand-in argument this module already relies on staying
    symbolic.
    """
    return None


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
                attrs = {"nargs": tuple(real_nargs), "_eval_evalf": _standin_refuses_evalf}
            else:
                attrs = {"_eval_evalf": _standin_refuses_evalf}
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
        _DEFERRED_STANDINS["Mod"] = type("Mod", (Function,), {"nargs": (2,), "_eval_evalf": _standin_refuses_evalf})
        # #326 finding (coordinator round 10 replay of 6e72d70, probe
        # 2): `floor`/`ceiling`/`frac`/`Max`/`Min` are the SAME shape as
        # `Mod` above — none are in `EvaluateFalseTransformer.functions`'
        # whitelist (`Abs`/`sign` ARE, confirmed live against sympy
        # 1.14's own parser source, so those two stay genuinely inert
        # under the ordinary `evaluate=False` protection and need no
        # stand-in of their own), so each evaluates EAGERLY during the
        # deferred parse regardless of `evaluate=False`, and each one's
        # own `eval()` needs a concrete NUMERIC comparison to do its job
        # (an integer boundary for floor/ceiling/frac, an ordering for
        # Max/Min) — attempted against whatever ELSE this scan already
        # deferred (typically a table-call stand-in), which is neither a
        # real number nor comparable to one, throwing DURING the scan's
        # own construction (`safe_parse` then surfaces a spurious
        # "parse error", refusing an entirely legitimate expression like
        # `Max(factorial(20), 5)`). Deferred here for the identical
        # reason `Mod` already is: inert is all a SCAN-ONLY construction
        # needs — `_resolve_arg_magnitude`'s own floor/ceiling/Abs/frac
        # branch and `_elementary_function_magnitude`'s own Max/Min/sign
        # handling (see each one's own docstring) do the REAL bound-
        # checking work on this now-inert node afterward, and the
        # `_evalf_coercion_violation` walk (below) catches a BARE,
        # top-level use the same way `_table_function_growth_violation`
        # already does for a bare table call. `Max`/`Min` are real
        # `sympy.Function` subclasses whose own `nargs` is `Naturals0`
        # (variadic — confirmed live), not a finite set `tuple()` could
        # consume, so they are left WITHOUT an explicit `nargs`, exactly
        # like every other name this loop could not characterise a
        # finite arity for.
        for _name in ("floor", "ceiling", "frac"):
            _DEFERRED_STANDINS[_name] = type(_name, (Function,), {"nargs": (1,), "_eval_evalf": _standin_refuses_evalf})
        for _name in ("Max", "Min"):
            _DEFERRED_STANDINS[_name] = type(_name, (Function,), {"_eval_evalf": _standin_refuses_evalf})
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
            UnicodeEncodeError, UnicodeDecodeError, SystemError):
        # `SystemError` added alongside the others for the SAME reason
        # `classify_unsafe`'s own identical tokenizer guard added it —
        # see that function's own comment for the full account (CPython
        # 3.12's C tokenizer, one narrow embedded-NUL-byte shape).
        return False  # not this check's job -- classify_unsafe already ran
    for tok in tokens:
        if tok.type == tokenize.NAME and tok.string in _FUNCTION_ARG_CAPS:
            return True
        if tok.type == tokenize.OP and tok.string in _UNPROTECTED_OPERATORS:
            return True
    return False


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


#: THE-1095 round 13 follow-up (coordinator's own probe had an extra `)`
#: — `2+2)` is a genuinely unbalanced-parenthesis SYNTAX error, and
#: `origin/main` itself raises `IndexError: list index out of range` for
#: it (CPython's own `ast`/`tokenize` machinery, not this module's own
#: code) — confirmed: `sympify('2+2)')` on bare SymPy 1.14.0 raises the
#: identical `IndexError` with the identical text. The round-13 fix
#: above this comment (`_classify_parse_exception`, an exception-TYPE
#: allowlist applied at every raw `parse_expr`/`_parse_deferred` call
#: site) was accordingly the WRONG locus, and a real regression: it
#: reclassified THIS exact, legitimate, `main`-identical `IndexError` as
#: a `CATEGORY_CEILING` refusal instead of relaying it — `2+2)` must
#: report `('validation', 'parse error: list index out of range')`,
#: byte-identical to `main`, never a resource ceiling; a user syntax
#: error is a validation problem, never a bounding problem, regardless
#: of which exception TYPE CPython's own parser happens to raise for it.
#:
#: Deleted (`_classify_parse_exception`/`_SYMPY_PARSE_EXCEPTION_TYPES`)
#: entirely — every site below is back to `f"parse error: {exc}"`,
#: unconditionally, for ANY exception `parse_expr`/`_parse_deferred`
#: (SymPy's own tokenizer/parser/evaluator) raises, exactly as before
#: THE-1095 round 13 ever touched this function, and exactly matching
#: `origin/main`'s own behavior for the identical raw string. The
#: correct locus for "an exception from OUR OWN code after a parse
#: already succeeded must never look like a user syntax error" is CALL
#: SITE, not exception TYPE: `reject_explosive` (on both `scan_shape`
#: and `shape`) is the one place in this function that calls into this
#: module's OWN post-parse scanning/resolver code
#: (`_numeric_ceiling_scan`, `_resolve_arg_magnitude`,
#: `_resolve_exact_rational`, the marker/binop checks, the output
#: ceiling) — see its own two call sites, just below, for the guard now
#: wrapping them.
_INTERNAL_SCAN_FAILURE_MESSAGE = (
    "this expression cannot be safely bounded: an unexpected internal "
    "error occurred while scanning it"
)


def _reject_explosive_safely(tree) -> str | None:
    """`reject_explosive(tree)`'s own `str | None` contract, except an
    exception raised by OUR OWN post-parse scanning/resolver code
    (`_numeric_ceiling_scan`, `_resolve_arg_magnitude`,
    `_resolve_exact_rational`, the marker/binop checks, the output
    ceiling, ... — everything `reject_explosive` itself calls into) is
    now ALSO a ceiling finding — `_INTERNAL_SCAN_FAILURE_MESSAGE` — the
    unknown-refusal `unknown != safe` already uses for every other
    unresolvable shape in this module, never propagated uncaught and
    never confusable with a `parse_expr`/`_parse_deferred` exception
    (SymPy's own tokenizer/parser/evaluator, which `safe_parse`'s two
    `try`/`except` blocks around THOSE calls handle separately, and
    deliberately still relay verbatim — see this function's own
    module-level comment, just above, for why THAT locus was the wrong
    place for this same guard).
    """
    try:
        return reject_explosive(tree)
    except Exception:
        return _INTERNAL_SCAN_FAILURE_MESSAGE


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
        explosive = _reject_explosive_safely(scan_shape)
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
        explosive = _reject_explosive_safely(shape)
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
    # #326 finding (coordinator round 11 replay of 9fe4f6a, Codex round 9
    # probe 2b): `_log10_num_den`'s own `Float` handling reports magnitude
    # 0 unconditionally — correct for a Float LITERAL in the source (it
    # always prints at its own default, ~15-significant-digit precision
    # regardless of the VALUE it represents), but a Float's own RENDERED
    # length is governed by its PRECISION (how many significant digits it
    # carries), not its magnitude — and precision is exactly what `N(x,
    # n)`'s own second argument controls: `N(pi, 100000)` renders
    # 100_001 characters even though `pi`'s own magnitude is tiny.
    # Checked SEPARATELY, and FIRST, for the fully-constructed `value` —
    # never reused for `_log10_num_den`'s OWN, different purpose
    # elsewhere in this file (an unevaluated Float LITERAL sitting
    # somewhere inside a pre-parse tree, which is never a hazard on its
    # own, only ever a factor whose VALUE might feed a later stage). `
    # _prec` is the Float's own internal BINARY precision (bits) SymPy
    # already carries; `_prec * log10(2)` is the equivalent DECIMAL digit
    # count — `10.0**100000` (a compact Float, `_prec` stays the
    # default 53 bits regardless of its huge MAGNITUDE, ~16 decimal
    # digits once rendered) still evaluates; `N(pi, 100000)` (`_prec` ~
    # 332_196 bits, ~100_001 decimal digits) now refuses.
    from sympy import Float as _Float

    if isinstance(value, _Float):
        # `_prec` is SymPy's own internal (underscore-prefixed, but
        # public-in-practice — no public accessor exists for it) binary
        # precision field for a `Float`, in bits.
        float_digit_count = value._prec * math.log10(2)
        if float_digit_count > MAX_NUMERIC_DIGITS:
            digit_message = (f"the result would have about {int(float_digit_count) + 1} "
                              f"significant digits, over the limit of {MAX_NUMERIC_DIGITS}: "
                              "it cannot be rendered as a decimal string")
            return None, (CATEGORY_CEILING, digit_message)
        return value, None
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
    # THE-1095 round 13 follow-up: `_log10_num_den` is OUR OWN post-parse
    # code (the "output ceiling" backstop) too — same guard, same reason,
    # as `_reject_explosive_safely` above: an exception it raises must
    # become the unknown-refusal ceiling, never an uncaught crash and
    # never confusable with a `parse_expr`/`_parse_deferred` exception.
    try:
        result_log_num, result_log_den, result_resolved = _log10_num_den(value, {})
    except Exception:
        return None, (CATEGORY_CEILING, _INTERNAL_SCAN_FAILURE_MESSAGE)
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


def _table_function_growth_violation(node, memo: dict) -> str | None:
    """Reason the OUTPUT of a `Function` node named in `_GROWTH_BOUNDS`
    would itself print over `MAX_NUMERIC_DIGITS`, checked REGARDLESS of
    nesting — walked unconditionally alongside `_function_arg_cap_
    violation` (same two call sites: `_numeric_ceiling_scan`'s own
    descent, and `reject_explosive`'s own unconditional walk), or `None`
    if `node` is not such a call, or its OWN output stays safely under.

    #326 finding B (Codex round 9, "position-complete spec"): a single-
    position table name's own safety was always a BYPRODUCT of its ONE
    cap being calibrated so the output stays under `MAX_NUMERIC_DIGITS`
    (`MAX_HEAVY_ARG`'s own docstring literally derives 1463 as "the
    largest n under MAX_NUMERIC_DIGITS" for `factorial`) — true only
    because a SINGLE position drove the whole output. Once a name has a
    SECOND cost-driving position (`bell(n, k)`, `harmonic(n, m)`,
    `zeta(s, a)`, ...), the flat per-position caps on EACH position
    individually no longer guarantee the COMBINED output stays bounded:
    `bell(1463, 1463)`, `bernoulli(1463, 1463)`, `harmonic(1463,
    -1463)`, `zeta(-1463, 1463)` each pass EVERY existing per-position
    cap (each individual argument is, on its own, comfortably within its
    own limit) and then HANG constructing the real, genuinely unbounded-
    for-this-module's-purposes result — no promptness backstop existed
    for this shape at all before this check. `_table_function_bound`
    already computes a safe UPPER BOUND for any such node's own output
    (its own per-position caps first, then the growth formula) — this
    just ALSO checks that bound against `MAX_NUMERIC_DIGITS` directly,
    the same way an over-cap ARGUMENT already refuses, so a call's OWN
    combined output size is proven safe BEFORE real construction, not
    merely each position in isolation. Harmless, redundant work for a
    single-position name (its own cap already guarantees this check
    never fires) — cheap enough not to special-case away.
    """
    from sympy import Function

    if not (isinstance(node, Function) and type(node).__name__ in _GROWTH_BOUNDS):
        return None
    if node.free_symbols:
        # A genuinely SYMBOLIC bounded position (`bell(x) % 7`,
        # `factorial(cos(y)) - factorial(cos(y))`) is skipped by `_table_
        # function_bound`'s own per-position loop (`if arg.free_symbols:
        # continue`) the same way `_function_arg_cap_violation`'s twin
        # loop already does — correctly: a symbolic argument never
        # materializes, matching `origin/main`, which leaves it symbolic
        # too. But a SKIPPED position never reaches `bounds` at all, so
        # `growth(bounds)` below can end up called with `bounds` missing
        # EVERY position it needed — `_safe_pow10(None)` reads back as
        # `math.inf` from an empty dict, and this check would then refuse
        # a call this module was never in danger from, purely because
        # nothing could be measured (not because anything WAS measured
        # and found large). Declining outright whenever the WHOLE node
        # still carries a free symbol anywhere avoids threading a
        # separate "was every bounded position genuinely supplied and
        # non-symbolic" signal out of `_table_function_bound` — the
        # existing per-position cap check (`_function_arg_cap_
        # violation`) already covers any position that DOES resolve to a
        # concrete, over-cap value even in a partially-symbolic call.
        return None
    name = type(node).__name__
    if name in _POLE_SENSITIVE_GROWTH_TOP_LEVEL_NAMES:
        # #326 finding (grok, round 12 review of 4d94871): `gamma`/
        # `loggamma`/`digamma`/single-arg `zeta` have NO genuine
        # TOP-LEVEL hazard at all (confirmed live: `origin/main` never
        # performs a genuinely unbounded computation for a bare call to
        # any of these four at an arbitrary exact rational — see
        # `_MAGNITUDE_AT_LEAST_ONE_DOMAIN_POSITIONS`'s own comment for
        # the full account) — this walk SKIPS them entirely (`return
        # None`) rather than reuse their own (nested-only) `_pole_
        # sensitive_magnitude` domain check, which would otherwise
        # incorrectly narrow `gamma(1/2)`, `digamma(1/2)`, `zeta(1 +
        # 10**-6)`, etc. at the TOP level too.
        return None
    if name == "polygamma":
        # `polygamma`, unlike the four above, DOES have a genuine
        # TOP-LEVEL hazard — but only for a NONZERO order (`polygamma(
        # 1463, 1)` alone has a true log10 magnitude of ~3997.36); order
        # 0 is exactly `digamma` (either written directly, or reached
        # via `digamma`'s own real-construction rewrite to `polygamma(0,
        # z)` — the module's own long-documented rename quirk) and
        # shares digamma's OWN "no genuine top-level hazard" exemption.
        # Resolved EXACTLY (not via magnitude) specifically to tell
        # order 0 apart from order 1 — see `_pole_sensitive_magnitude`'s
        # own `polygamma` branch for the full account of why a magnitude
        # alone cannot make that distinction at all.
        if not node.args:
            return None
        order_v = _resolve_exact_rational(node.args[0], memo)
        if order_v == 0:
            return None
        if order_v == -1 and len(node.args) >= 2:
            # #326 finding (grok, round 13 review of fad081f): `polygamma(
            # -1, z)` rewrites, at REAL construction, to `loggamma(z) -
            # log(2*pi)/2` — for a POSITIVE INTEGER `z`, SymPy further
            # rewrites `loggamma` to `log(factorial(z-1))`, MATERIALIZING
            # an exact `factorial(z-1)` integer eagerly, even bare
            # (unwrapped) at the top level (confirmed live:
            # `polygamma(-1, 1463)` alone prints a 4000+-digit literal).
            # For a NON-integer `z`, the identical call evaluates to a
            # compact numeric expression (a `Float`/symbolic radical --
            # `polygamma(-1, 700.5)` -> one ~15-digit `Float`) regardless
            # of `z`'s own magnitude — confirmed live, no hazard, the
            # SAME "no genuine top-level hazard" exemption
            # `_POLE_SENSITIVE_GROWTH_TOP_LEVEL_NAMES` already gives
            # `gamma`/`loggamma`/`digamma`. So a POSITIVE INTEGER `z`
            # needs the SAME cap `factorial`'s own position-0 cap
            # already uses (`MAX_HEAVY_ARG`, calibrated as "the largest
            # n under MAX_NUMERIC_DIGITS" for `factorial(n)` — here,
            # `factorial(z - 1)`, so `z <= MAX_HEAVY_ARG + 1`); anything
            # else (a `z` that is not a positive integer, or does not
            # resolve at all) falls through to `_pole_sensitive_
            # magnitude`'s own general domain check below, which refuses
            # this exact shape unconditionally — correct there, since
            # THAT function answers a different question ("give a safe
            # BOUND for an arbitrary NESTED argument," never proven for
            # negative order at all) than this one ("is `origin/main`'s
            # own bare top-level call itself safe").
            z_v = _resolve_exact_rational(node.args[1], memo)
            if z_v is not None and z_v.q == 1 and 1 <= z_v <= MAX_HEAVY_ARG + 1:
                return None
        pole_result = _pole_sensitive_magnitude(node, memo)
        if pole_result is None:
            return None
        pole_log_num, pole_log_den, pole_resolved, pole_violation = pole_result
        if pole_violation:
            return pole_violation
        if not pole_resolved:
            return None
        return _digit_ceiling_text(pole_log_num - pole_log_den, f"the result of {name}(...)")
    bound, violation = _table_function_bound(node, memo)
    if violation:
        return violation
    if bound is None:
        return None
    return _digit_ceiling_text(bound, f"the result of {name}(...)")


#: `floor`/`ceiling`/`Abs`/`frac`/`sign`/`Max`/`Min` — the names
#: `_evalf_coercion_cheap` (and `_elementary_function_magnitude`'s own
#: `sign`/`Max`/`Min` handling) gate. Used by `_evalf_coercion_violation`
#: (below) to walk every occurrence UNCONDITIONALLY, the same shape as
#: `_table_function_growth_violation`'s own walk.
_EVALF_COERCION_NAMES = frozenset({"floor", "ceiling", "Abs", "frac", "sign", "Max", "Min"})


def _evalf_coercion_violation(node, memo: dict) -> str | None:
    """Reason a `floor`/`ceiling`/`Abs`/`frac`/`sign`/`Max`/`Min` node's
    own argument(s) are not cheap enough to numerically coerce, or
    `None` if `node` is not such a call, or its argument(s) pass.

    #326 finding (coordinator round 10 replay of 6e72d70, probe 2):
    `_resolve_arg_magnitude`'s own floor/ceiling/Abs/frac branch (and
    `_elementary_function_magnitude`'s own sign/Max/Min handling) only
    ever run when something ELSE calls `_resolve_arg_magnitude` on this
    node while resolving ITS OWN magnitude (a table call's own argument,
    a `Mul`/`Add` factor, ...) — a BARE, TOP-LEVEL `floor(polygamma(
    1463, 1))`, standing alone as the WHOLE parsed expression, is never
    such a callee. Walked unconditionally here instead, the identical
    shape `_table_function_growth_violation` already uses for the same
    "a bare top-level use is not automatically covered" gap.
    """
    from sympy import Function

    name = type(node).__name__
    if not (isinstance(node, Function) and name in _EVALF_COERCION_NAMES):
        return None
    if node.free_symbols:
        # Genuinely symbolic -- never materializes, matches `origin/
        # main`, the same scope every other check in this module already
        # gives a free symbol (see `_table_function_growth_violation`'s
        # own identical guard for the full account of why this matters:
        # an empty/partial resolution must not be misread as "measured
        # and found large").
        return None
    _log_num, _log_den, _resolved, violation = _resolve_arg_magnitude(node, memo)
    return violation


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
            # THE-1095 round-3-follow-up #5 (coordinator review of
            # 5119c9d, grok items 3 and 4): a NUMERIC (non-symbolic)
            # argument the resolver still cannot bound used to fall
            # through this loop SILENTLY (`continue`, never added to
            # `bounds`) — `growth(bounds)` below then read the missing
            # position back out as `None` via `bounds.get(pos)`, and
            # every growth formula's own `_safe_pow10(None)` check
            # happens to return `math.inf` for a MISSING position 0 —
            # working by ACCIDENT for the common case, not by design
            # (grok's own note: a formula reading a DIFFERENT position,
            # like `_growth_nextprime`'s own `ith` at position 1,
            # silently DEFAULTS instead — a real unresolved value there
            # would have been misread as "ith=1", not refused). Refusing
            # explicitly here, the same way `_function_arg_cap_
            # violation`'s own identical loop now does, means `growth`
            # below is NEVER called with a position missing for any
            # reason other than "not supplied" or "genuinely symbolic" —
            # the ONLY two cases a growth formula's own default is
            # actually meant to cover.
            return None, (f"an argument to {name}() cannot be safely bounded: "
                           "computing it would take an unbounded amount of time "
                           "and memory")
        domain_violation = _domain_violation(name, pos, arg, arg_log_num, arg_log_den)
        if domain_violation:
            return None, domain_violation
        if kind == "value":
            over_cap = (arg_log_num - arg_log_den) > math.log10(cap)
        else:
            over_cap = _digit_count_over_cap_for_node(arg, "num", arg_log_num, cap)
        if over_cap:
            return None, (f"an argument to {name}() exceeds the limit of {cap}: "
                           "computing it would take an unbounded amount of time "
                           "and memory")
        bounds[pos] = arg_log_num - arg_log_den
    zeta_violation = _zeta_two_arg_domain_violation(node)
    if zeta_violation:
        return None, zeta_violation
    growth = _GROWTH_BOUNDS.get(name)
    if growth is None:
        return None, (f"{name}() cannot be safely bounded as a nested argument: "
                       "no growth estimate is defined for it")
    return growth(bounds), None


def _zeta_two_arg_domain_violation(node) -> str | None:
    """Reason a TWO-ARGUMENT `zeta(s, a)` call falls outside a domain
    this module has a safe bound for, or `None` if `node` is not such a
    call, or is and its domain is fine.

    #326 finding (coordinator round 10 replay of 6e72d70, probe 1):
    `zeta(s, a)` for a concrete INTEGER `s < 2` computes a Bernoulli
    POLYNOMIAL of degree `|s| + 1` evaluated at `a` — measured up to
    ~6.4s near this module's own `MAX_HEAVY_ARG` cap (`zeta(-1200, 2)`;
    Codex measured 5.8s on an earlier round, still open at the time of
    this replay) — a cost the SAME `s`, alone (no `a` supplied), never
    pays: `zeta(-1200)` alone is instant (`0`, a trivial zero for an
    even negative integer), and `zeta(-1463)` alone (odd, the module's
    own already-PINNED at-cap single-arg test from an earlier round)
    measures ~0.3s. Scoped to the TWO-ARG form specifically, not a
    blanket restriction on position 0, so that existing single-arg pin
    stays unaffected — confirmed live that the cost genuinely needs BOTH
    a negative/small integer `s` AND a concrete `a` present; `zeta(s)`
    alone at any `s` this module's own `MAX_HEAVY_ARG` cap admits stays
    fast regardless of sign.

    A non-integer `s` (`zeta(1/2, 2)`, `zeta(-1463.5, 2)`) stays
    unevaluated on `origin/main` regardless of value — confirmed live,
    no hazard, not refused — matching this module's own general "a
    computation main itself never performs needs no cap" stance.
    """
    if type(node).__name__ != "zeta" or len(node.args) < 2:
        return None
    s, a = node.args[0], node.args[1]
    if s.free_symbols or a.free_symbols:
        return None
    if s.is_integer and not bool(s >= 2):
        return ("the first argument to zeta() with a second argument supplied "
                "must be >= 2 (a smaller integer order computes a Bernoulli "
                "polynomial of the corresponding degree): this module has not "
                "derived a safe bound for that domain")
    return None


def _n_precision_violation(node, memo: dict) -> str | None:
    """Reason `N(x, n)`'s own PRECISION argument (position 1) exceeds
    this module's own digit ceiling, or `None` if `node` is not an `N`
    call, or its precision argument is absent, genuinely symbolic, or in
    range.

    #326 finding (coordinator round 11 replay of 9fe4f6a, Codex round 9
    probe 2a): `N`/`evalf`'s own PRECISION parameter is unbounded on
    `origin/main` — `N(pi, 100000)` returns a `Float` whose rendered
    text is 100_001 characters, in ~1.5s. `N` is a plain, eager callable
    (like `factorint`/`primorial`/...), never in `EvaluateFalseTransformer
    .functions`'s whitelist, so it needs the SAME deferred-stand-in
    treatment those already get (see `_DEFERRED_STANDIN_NAMES`'s own
    comment on why `N` is folded in via `_EXTRA_BOUNDED_POSITIONS` even
    though it has no position-0 cap of its own) — this is the DEDICATED
    check function for its one bounded position, walked UNCONDITIONALLY
    (the same shape as `_evalf_coercion_violation`/`_table_function_
    growth_violation`) rather than routed through the general `_function_
    arg_cap_violation`/`_table_function_bound` machinery, which assumes
    a `_FUNCTION_ARG_CAPS` position-0 entry and a `_GROWTH_BOUNDS` growth
    formula neither of which meaningfully apply to `N` (its own OUTPUT
    hazard — a high-precision `Float` — is caught separately, by
    `safe_parse`'s own `Float._prec`-aware output check, not a growth
    formula). Reads the cap from `_EXTRA_BOUNDED_POSITIONS` via
    `_position_spec` rather than hardcoding it, so the table stays the
    one source of truth for the value.
    """
    if type(node).__name__ != "N" or len(node.args) < 2:
        return None
    spec = _position_spec("N", 1)
    if spec is None:
        return None
    _kind, cap = spec
    arg = node.args[1]
    if arg.free_symbols:
        return None
    arg_log_num, arg_log_den, arg_resolved, arg_violation = _resolve_arg_magnitude(arg, memo)
    if arg_violation:
        return arg_violation
    if not arg_resolved:
        return ("the precision argument to N() cannot be safely bounded: "
                "computing it would take an unbounded amount of time and memory")
    over_cap = (arg_log_num - arg_log_den) > math.log10(cap)
    if over_cap:
        return (f"the precision argument to N() exceeds the limit of {cap}: "
                "computing it would take an unbounded amount of time and memory")
    return None


def _float_value_log10(node) -> float:
    """`log10(|value|)` for a sympy `Float` node — the actual numeric
    VALUE's magnitude, never `_log10_num_den`'s own `(0.0, 0.0, True)`
    print-profile shortcut for a `Float` (correct for ITS purpose —
    printing a bare `Float` is always safe, SymPy renders one at a fixed
    ~15 significant digits regardless of true size — but wrong as a
    VALUE bound: #326 finding (grok, `verify-1095-r7-grok.log`), `_resolve_
    arg_magnitude` used to inherit that same print-profile shortcut for a
    `Float` used as a marker operand, a `Mul`/`Add` factor, or a `floor`/
    `ceiling` argument, silently reporting "magnitude ~1" for e.g.
    `1e4`, `1e30`, or `1464.9` regardless of how large the actual VALUE
    is — `factorial(floor(1e4 * bell(1)))` (bell(1) == 1, so the true
    value is `factorial(10000)`, ~35_660 digits) sailed past this
    resolver's own Mul-composition check with a claimed magnitude of 0
    and only got caught LATE, by the output-ceiling backstop, AFTER
    `factorial(10000)` was already fully constructed for real).

    Computed via SymPy's own arbitrary-precision `log` (never `float(node)`
    directly — that raises `OverflowError` past Python's own ~1.8e308
    ceiling even though a SymPy `Float` has no such limit of its own, e.g.
    `Float('1e400')` prints and stores fine, no IEEE-754 double involved);
    `float()` of the resulting `log(...)` expression forces mpmath's own
    arbitrary-precision numeric evaluation, safe at any exponent this
    module would ever admit past its own digit caps.
    """
    if node.is_zero:
        # `_log10_of_int`'s own convention for an exact-zero magnitude
        # elsewhere in this file (`Rational.p == 0`): treat as "one
        # digit", not `-inf` — no caller here needs to distinguish an
        # exact zero from a merely tiny value, and `-inf` propagating
        # through later arithmetic (`Mul`/`Add` composition, a `Pow`
        # exponent) is more failure surface than this is worth.
        return 0.0
    from sympy import Abs as _Abs
    from sympy import log as _log

    return float(_log(_Abs(node), 10))


#: #326 finding F (grok, round 9 review of 5119c9d — "the fail-closed
#: list"): a small, curated table of ORDINARY (non-table) elementary
#: functions whose OWN output magnitude is derivable from their
#: argument's, used by `_elementary_function_magnitude` (below) so a
#: numeric argument wrapped in one of these — `factorial(log(1))`,
#: `factorial(cos(0))`, `factorial(Max(3, 5))` (all evaluate, all `=1`
#: or `120`, on `origin/main`) — resolves through the shared magnitude
#: resolver instead of hitting the item-3 structural backstop
#: ("unresolved, no free symbols, refuse") as an unrecognised opaque
#: shape. Deliberately NOT exhaustive: anything else non-table with
#: numeric args stays a pinned refusal (`factorial(Ei(1463))`,
#: `factorial(I)`) — `unknown != safe` is still the default, this table
#: is the curated, individually-justified exception list, not a general
#: "assume every SymPy function is safe" escape hatch.
#:
#: name -> rule kind:
#:   "bounded1"    — output magnitude is ALWAYS <= 1 regardless of the
#:                    argument (`sign` in {-1,0,1}).
#:   "passthrough" — output never exceeds the LARGEST argument's own
#:                    magnitude (`Max`/`Min`, any arity).
#:   "log"         — output grows far slower than the argument (`log`'s
#:                    own magnitude is `~log10(log(argument's value))`,
#:                    negligible next to any cap this module compares
#:                    against).
#:
#: #326 finding (grok, round 12 review of 4d94871): `sin`/`cos`/`tanh`/
#: `erf`/`exp` used to live here too (`"bounded1"` for the first four,
#: `"exp"` for the last) — moved OUT entirely, to `_pole_sensitive_
#: magnitude`, which requires an EXACT value and checks a REAL domain
#: on it (see that function's own docstring). The round-11 fix that
#: lived here — bounding a non-real `sin`/`cos`/`tanh`/`erf` argument by
#: `e**|k|` for an exactly-resolvable MODULUS — was itself WRONG:
#: `erf(7*I)` (`erfi(7) ~= 1.6*10**20`) vastly exceeds `e**7 ~= 1096`,
#: and `tanh`'s own poles (on the imaginary axis) are not a MAGNITUDE
#: question at all — `tanh(I*15707/10000)` (`i*tan(15707/10000)`, `y`
#: within a hair of `pi/2`) has no bounded-by-`e**|k|` growth rate to
#: speak of. Deleted rather than patched a second time.
_ELEMENTARY_BOUND_RULES: dict = {
    "sign": "bounded1",
    "Max": "passthrough", "Min": "passthrough",
    "log": "log",
}


def _elementary_function_magnitude(node, memo: dict) -> tuple[float, bool, str | None] | None:
    """`(log10_upper_bound, resolved, violation)` for a `Function` node
    whose class name is a key of `_ELEMENTARY_BOUND_RULES`, or `None` if
    every argument stayed unresolved (the caller's own "leave unresolved,
    let something else decide" fallback applies exactly as if this table
    did not recognise the name at all).
    """
    rule = _ELEMENTARY_BOUND_RULES[type(node).__name__]
    if rule == "bounded1":
        # Only "sign" reaches this rule now (see `_ELEMENTARY_BOUND_
        # RULES`'s own comment for why `sin`/`cos`/`tanh`/`erf` moved
        # out).
        if len(node.args) != 1:
            return 0.0, True, None
        # #326 finding (coordinator round 10 replay of 6e72d70,
        # probe 2): `sign` DOES attempt to determine positivity/
        # negativity for a concrete argument, which can fall back
        # to the SAME expensive `evalf` floor/ceiling's own gate
        # exists to avoid — gated identically.
        a_log_num, a_log_den, a_resolved, a_violation = _resolve_arg_magnitude(node.args[0], memo)
        if a_violation:
            return 0.0, False, a_violation
        if not a_resolved:
            return None
        if not _evalf_coercion_cheap(node.args[0], a_log_num, a_log_den):
            return 0.0, False, ("the argument to sign() cannot be safely coerced "
                                 "to a number: computing it would take an "
                                 "unbounded amount of time and memory")
        return 0.0, True, None
    if rule == "passthrough":
        if not node.args:
            return 0.0, True, None
        magnitudes = []
        for arg in node.args:
            a_log_num, a_log_den, a_resolved, a_violation = _resolve_arg_magnitude(arg, memo)
            if a_violation:
                return 0.0, False, a_violation
            if not a_resolved:
                return None
            # #326 finding (coordinator round 10 replay of 6e72d70,
            # probe 2): `Max`/`Min` compare their arguments NUMERICALLY
            # (an evalf-based comparison for anything not structurally
            # orderable) — the SAME cheap-argument gate floor/ceiling
            # use, applied per argument, same reasoning as `sign` above.
            if not _evalf_coercion_cheap(arg, a_log_num, a_log_den):
                return 0.0, False, (f"an argument to {type(node).__name__}() cannot "
                                     "be safely coerced to a number: computing it "
                                     "would take an unbounded amount of time and "
                                     "memory")
            magnitudes.append(a_log_num - a_log_den)
        return max(magnitudes), True, None
    # rule == "log" -- the only remaining single-argument rule (`exp`
    # moved to `_pole_sensitive_magnitude` — see `_ELEMENTARY_BOUND_
    # RULES`'s own comment).
    if len(node.args) != 1:
        return None
    a_log_num, a_log_den, a_resolved, a_violation = _resolve_arg_magnitude(node.args[0], memo)
    if a_violation:
        return 0.0, False, a_violation
    if not a_resolved:
        return None
    arg_log = a_log_num - a_log_den
    # #326 finding (coordinator round 11 replay of 9fe4f6a, Codex
    # round 9 probe 1): `log` in this module's namespace is the
    # NATURAL log (`ln`), not `log10` — the OLD formula (`log10(
    # abs(arg_log) + 10.0)`) treated `arg_log` (already `log10(
    # |x|)`) as if it were roughly `|ln(x)|` itself, under-
    # estimating by a factor of `ln(10) ≈ 2.303`: `log(10**1000)`'s
    # true magnitude is `log10(|ln(10**1000)|) = log10(1000*ln(10))
    # ≈ 3.362` (`|ln(x)| ≈ 2302.6`); the OLD formula claimed
    # `log10(1010) ≈ 3.004` (`≈ 1009`), comfortably UNDER `bell`'s
    # own `MAX_HEAVY_ARG` (1463) cap when the TRUE value (2302) is
    # well OVER it — `bell(floor(log(10**1000)))` passed every
    # screen and hung. `|ln(x)| = |arg_log| * ln(10)` exactly
    # (`arg_log = log10(|x|)`, a base change), so `log10(|ln(x)|) =
    # log10(|arg_log|) + log10(ln(10))` — verified against SymPy
    # across a grid (`log(10**k)`, `log(2)`, `log(Rational(1,
    # 10**k))`) to within float precision.
    abs_arg_log = abs(arg_log)
    if abs_arg_log < 1e-15:
        return 0.0, True, None  # x ~= 1 -- ln(x) ~= 0, trivially tiny
    return math.log10(abs_arg_log) + math.log10(math.log(10)), True, None


#: #326 finding (coordinator round 10 replay of 6e72d70, probe 2): the
#: digit-magnitude ceiling under which `floor`/`ceiling`/`frac`/`Max`/
#: `Min`/`sign`'s own numeric coercion (an `evalf`, or an evalf-based
#: comparison) is cheap regardless of what it is approximating — reuses
#: `MAX_SYMBOLIC_EXPONENT` (200), an existing, already-vetted "this
#: module trusts an evalf/expansion at this scale to stay fast" ceiling
#: (see its own docstring), rather than inventing a second one.
_MAX_EVALF_ARG_DIGITS = MAX_SYMBOLIC_EXPONENT


def _evalf_coercion_cheap(arg, arg_log_num: float, arg_log_den: float) -> bool:
    """Whether `arg` — already known non-symbolic, already resolved to
    `(arg_log_num, arg_log_den)` via `_resolve_arg_magnitude` — is CHEAP
    for `floor`/`ceiling`/`frac`/`Max`/`Min`/`sign` to numerically
    coerce, i.e. whether real construction can determine the answer
    WITHOUT an expensive high-precision `evalf`.

    #326 finding (coordinator round 10 replay of 6e72d70, probe 2):
    `_resolve_arg_magnitude`'s own floor/ceiling/Abs branch (and the
    `Max`/`Min`/`sign` entries in `_ELEMENTARY_BOUND_RULES`) treated ANY
    resolvable argument as safe to wrap, on the theory that "off by at
    most 1" bounds the RESULT's own digit count. True for the printed
    digit count, false for the CONSTRUCTION cost: `floor(polygamma(1463,
    1))` — an EXACT but genuinely TRANSCENDENTAL `Mul` (`-factorial(
    1463) * zeta(1464)`, `zeta(1464)` itself an exact multiple of
    `pi**1464` — never a plain `Integer`) — forces SymPy's own `evalf`
    at thousands of digits of precision to determine the integer
    boundary, measured ~18-19s through this module's own pipeline even
    after `_standin_refuses_evalf` closes the STAND-IN half of that cost
    (its own name-collision bypass — see that function's own docstring):
    the REAL `origin/main`-shaped construction in this module's own
    step-3 parse still pays this cost for real, since `floor`/`ceiling`
    are not in `EvaluateFalseTransformer.functions`'s whitelist and so
    are never `evaluate=False`-protected regardless of stand-ins.

    CHEAP (`True`) in exactly two shapes: an exact `Integer`/`Rational`/
    `Float` LITERAL (any magnitude — reading off the integer part of an
    exact rational, or of a fixed-precision `Float`, is O(1), never an
    approximation), or a nested call to a table function PROVEN to
    always return a plain `Integer` (`_INTEGER_VALUED_TABLE_NAMES` — the
    SAME set `//`'s own divisor-lower-bound check already trusts;
    floor/ceiling/frac of an ALREADY-integer value is a no-op, no
    rounding decision to make). Anything else — a bare irrational
    `NumberSymbol`, a `Mul`/`Add`/`Pow` combining one, a non-integer-
    valued table call (`digamma`/`zeta`/`gamma`/`loggamma`/`polygamma`)
    — is cheap ONLY if its own resolved MAGNITUDE stays under `_MAX_
    EVALF_ARG_DIGITS` digits: below that, an `evalf` is fast regardless
    of what it is approximating (`floor(pi*10**5)`, `floor(polygamma(3,
    1))` both stay comfortably under it); above it, this module has no
    basis to assume it stays fast.
    """
    from sympy import Float, Function, Integer, Rational

    if isinstance(arg, (Integer, Rational, Float)):
        return True
    if isinstance(arg, Function) and type(arg).__name__ in _INTEGER_VALUED_TABLE_NAMES:
        return True
    return not _digit_count_over_cap(arg_log_num - arg_log_den, _MAX_EVALF_ARG_DIGITS)


#: #326 finding (grok, round 12 review of 4d94871): the structural
#: defect named across rounds 11 and 12 is the SAME each time — a
#: MAGNITUDE-only value (this module's own `bounds`/`_resolve_arg_
#: magnitude` convention, sign and small-value detail both discarded)
#: fed to a formula whose correctness genuinely depends on the SIGN or
#: the EXACT value, not merely an upper bound on `|value|`:
#: `_growth_zeta` treating `|s| >= 2` as `s >= 2` (round 11), `gamma`/
#: `digamma`'s own `|z| >= 1` domain row never checking SIGN at all
#: (round 12 — `gamma(-1-10**-6)`, next to the pole at `z = -1`, has
#: magnitude `~1.000001 >= 1` and passed), `_growth_zeta`'s own two-arg
#: `a` term silently clamped to `max(1.0, a)` rather than REFUSING
#: `a < 1` (`zeta(2, 1/1000)`'s true `a**-s` term is `10**6`, not
#: bounded by `zeta(2) < 2`), and the `e**|k|` complex-trig bound
#: (round 11) being flatly WRONG for `erf`/`tanh` (`erfi(7) ~= 10**20`,
#: far past `e**7 ~= 1096`; `tan` has its OWN poles a magnitude bound on
#: the coefficient alone cannot see at all).
#:
#: The fix, per the coordinator's own round-12 mandate: `gamma`/
#: `loggamma`/`digamma`/`zeta`(both arities)/`sin`/`cos`/`tanh`/`erf`/
#: `exp` may NEVER be bounded from a magnitude-only input again — each
#: one, wherever it is NESTED (a table/elementary call inside another
#: bounded position, or under `floor`/`ceiling`/`Abs`/`Max`/`Min`/a
#: marker that itself feeds one), requires its OWN argument(s) to
#: resolve to an EXACT `Rational` value first (`_resolve_exact_rational`,
#: below), then checks ITS OWN domain directly ON that exact value
#: (never a derived magnitude) before computing anything — refusing
#: with the "cannot be safely bounded" message for ANY value outside
#: that domain, OR for an argument that resolves (no free symbols) but
#: is NOT exactly rational at all (a `NumberSymbol`, an irrational
#: `Pow`, a transcendental table result like `zeta(2)` itself — genuinely
#: unresolvable exactly, not merely large). `_growth_stirling_
#: factorial`-family formulas for MONOTONE names (`factorial`/`bell`/
#: `binomial`/`rf`/`ff`/`primorial`/`prime`/`nextprime`/`fibonacci`/...)
#: are UNAFFECTED — those are sound from a magnitude (an UPPER bound on
#: a known-non-negative-integer value) precisely because they are
#: monotone increasing in that magnitude; none of the nine names here
#: are (each has a pole, a sign-dependent branch, or both), so a
#: magnitude alone is never enough for them.
_POLE_SENSITIVE_NAMES = frozenset({
    "gamma", "loggamma", "digamma", "polygamma", "zeta",
    "sin", "cos", "tanh", "erf", "exp",
})

#: Of the ten `_POLE_SENSITIVE_NAMES`, ONLY these four are also members
#: of `_GROWTH_BOUNDS` (so `_table_function_growth_violation`'s own
#: unconditional walk — a DIFFERENT check from `_resolve_arg_magnitude`'s
#: own pole-sensitive interception, used for the NESTED case — would
#: otherwise reach them for a BARE, TOP-LEVEL call too). `_table_
#: function_growth_violation` skips all four entirely there — see its
#: own comment for why none of them has a genuine top-level hazard.
#: `polygamma` (also in `_GROWTH_BOUNDS`) is handled by its OWN
#: dedicated branch in that same function instead, since ITS top-level
#: exemption depends on whether `order` is exactly `0` (`digamma` in
#: disguise) or not. `sin`/`cos`/`tanh`/`erf`/`exp` were never
#: `_GROWTH_BOUNDS` members at all, so `_table_function_growth_
#: violation`'s own gate already excludes them before this even matters.
_POLE_SENSITIVE_GROWTH_TOP_LEVEL_NAMES = frozenset({"gamma", "loggamma", "digamma", "zeta"})

#: log10(cap) for the "value"-kind arg-cap entries these nine names
#: already carry via `_FUNCTION_ARG_CAPS`/`_HEAVY_FUNCTIONS` (all
#: `MAX_HEAVY_ARG`) — reused so `_pole_sensitive_magnitude`'s own
#: growing branches (zeta's Bernoulli envelope, exp's `e**x`) stay
#: capped consistently with the ordinary per-position check that ALSO
#: still runs for these names' position 0, rather than a second,
#: independently-drifting threshold.
_POLE_SENSITIVE_GROWTH_CAP_LOG10 = math.log10(MAX_HEAVY_ARG)


def _exact_rational_too_big(value) -> bool:
    """Whether an exact `Rational`'s own numerator or denominator has
    grown past a small digit guard — checked after EVERY compose step
    in `_resolve_exact_rational` (never only at the end), so composing
    a chain of exact values can never itself become the unbounded
    operation this module exists to prevent. `_MAX_EVALF_ARG_DIGITS`
    (200, `MAX_SYMBOLIC_EXPONENT`) is reused rather than a fourth guard
    constant with the same intent."""
    return (value.p.bit_length() > 700 or value.q.bit_length() > 700)


def _resolve_exact_rational(node, memo: dict):
    """The EXACT `sympy.Rational` value of `node`, if — and only if —
    it is computable from small literals without ever approximating or
    guessing: an `Integer`/`Rational` literal directly; a `Float`
    literal via its own EXACT binary-to-rational conversion (a `Float`
    always represents one finite binary fraction exactly, never an
    approximation, unlike converting it through `log`/`pow`); a `Mul`/
    `Add`/`Pow`(integer exponent)/marker (`<<`/`>>`/`%`/`//`) of
    exactly-resolvable pieces, composed for real (never estimated) with
    `_exact_rational_too_big` checked after every step so an
    adversarial chain cannot blow up the composition itself. `None` for
    anything else — a free symbol, a `NumberSymbol` (`pi`/`E`/...,
    genuinely irrational, no exact rational value exists), an
    irrational `Pow` (non-integer exponent), a transcendental table
    result (`zeta(2)`, `gamma(5.5)`, ...), or a chain that grew past the
    size guard — `unknown/irrational != safe`, the same bar this
    module already holds every other unresolvable shape to.

    Used ONLY by `_pole_sensitive_magnitude` (below) — every OTHER
    consumer in this module keeps using `_resolve_arg_magnitude`'s own
    magnitude-only contract, which is sufficient (and cheaper) for a
    monotone, non-negative-integer-valued name.

    NEVER raises — `_resolve_exact_rational_uncached`'s own composition
    (`Mul`/`Add`/`Pow`/marker arithmetic on arbitrary-precision Python
    `Fraction`-backed `Rational`s) is exercised on caller-controlled
    shapes this module has not exhaustively enumerated. THE-1095 round
    13 (coordinator review of 9a35eb6) chased a `factorial(floor(Abs(
    1/(1-1+10**(-6)))))` repro that turned out, on the coordinator's own
    later correction, to be an unbalanced parenthesis in the PROBE
    itself (a genuine `IndexError` from `origin/main`'s own tokenizer,
    not this module) — but the hardening below is kept regardless: this
    function's own established contract is already `None` for "not
    exactly resolvable," and catching any exception the composition
    itself raises and returning `None` for it too is the same outcome
    as every OTHER unresolvable shape, for the same "unknown != safe"
    reason every other branch here already returns `None` rather than
    guessing. See `safe_parse`'s own two `reject_explosive` call sites
    for where THIS module's own post-parse code (which calls into this
    function) is guarded against ever surfacing as a misleading "parse
    error" — the correct locus turned out to be CALL SITE (only code
    that runs after a parse has already succeeded), never exception
    TYPE.
    """

    if node.free_symbols:
        return None
    key = id(node)
    cache = memo.setdefault("__exact_rational__", {})
    if key in cache:
        return cache[key]
    try:
        result = _resolve_exact_rational_uncached(node, memo)
    except Exception:
        result = None
    cache[key] = result
    return result


def _resolve_exact_rational_uncached(node, memo: dict):
    from sympy import Add, Float, Integer, Mul, Pow, Rational

    if isinstance(node, Integer):
        return Rational(int(node))
    if isinstance(node, Rational):
        return node
    if isinstance(node, Float):
        try:
            value = Rational(node)
        except (TypeError, ValueError):
            return None
        return None if _exact_rational_too_big(value) else value
    if isinstance(node, Mul):
        total = Rational(1)
        for factor in node.args:
            fv = _resolve_exact_rational(factor, memo)
            if fv is None:
                return None
            total *= fv
            if _exact_rational_too_big(total):
                return None
        return total
    if isinstance(node, Add):
        total = Rational(0)
        for term in node.args:
            tv = _resolve_exact_rational(term, memo)
            if tv is None:
                return None
            total += tv
            if _exact_rational_too_big(total):
                return None
        return total
    if isinstance(node, Pow):
        base, exp = node.args
        if not isinstance(exp, Integer):
            return None  # non-integer exponent: not generally exactly rational
        exp_int = int(exp)
        if abs(exp_int) > 2000:
            return None
        base_v = _resolve_exact_rational(base, memo)
        if base_v is None:
            return None
        if base_v == 0 and exp_int < 0:
            return None
        result = base_v ** exp_int
        return None if _exact_rational_too_big(result) else result
    type_name = type(node).__name__
    if type_name in _DEFERRED_BINOP_OPS:
        left_v = _resolve_exact_rational(node.args[0], memo)
        right_v = _resolve_exact_rational(node.args[1], memo)
        if left_v is None or right_v is None:
            return None
        op = _DEFERRED_BINOP_OPS[type_name]
        if op == "<<":
            if not (right_v == int(right_v) and right_v >= 0 and right_v <= 2000):
                return None
            result = left_v * (2 ** int(right_v))
        elif op == ">>":
            if not (left_v == int(left_v) and right_v == int(right_v) and right_v >= 0):
                return None
            result = Rational(int(left_v) >> int(right_v))
        elif op == "%":
            if right_v == 0 or not (left_v == int(left_v) and right_v == int(right_v)):
                return None
            result = Rational(int(left_v) % int(right_v))
        elif op == "//":
            if right_v == 0:
                return None
            result = Rational((left_v.p * right_v.q) // (left_v.q * right_v.p))
        else:
            return None
        return None if _exact_rational_too_big(result) else result
    return None


def _pole_sensitive_magnitude(node, memo: dict) -> tuple[float, float, bool, str | None] | None:
    """`_resolve_arg_magnitude`'s own `(log_num, log_den, resolved,
    violation)` contract, for a `gamma`/`loggamma`/`digamma`/`zeta`/
    `sin`/`cos`/`tanh`/`erf`/`exp` node — or `None` if `node`'s own name
    is not one of these nine (the caller falls through to whatever it
    would otherwise have done, unaffected). See `_POLE_SENSITIVE_NAMES`'s
    own comment for the full rationale.

    Domain, per name (checked on the EXACT value, never a magnitude):
      - `gamma`/`loggamma`/`digamma` (position 0): exact `z >= 1`.
        `Gamma(z) <= ceil(z)!` (`_growth_gamma`'s own Stirling envelope,
        reused directly); `loggamma` shares it (looser than needed, but
        sound — `ln(Gamma(z))` is far smaller than `Gamma(z)` itself);
        `digamma` uses its own existing logarithmic formula.
      - `zeta` (position 0 only, single-arg): exact INTEGER `s <= 0` ->
        the Bernoulli envelope on `|s|` (`_growth_zeta`'s own formula,
        now always sound for a KNOWN sign); exact `s >= 2` (integer or
        not) -> `zeta(s) < 2` (`log10(2)`); anything else (the pole at
        `s = 1`, `s` in `(0, 2)` non-`>= 2`, ...) refuses.
      - `zeta` (positions 0 AND 1, two-arg): exact integer `s >= 2` AND
        exact `a >= 1` -> `zeta(s, a) <= zeta(s) < 2` (`a >= 1` only
        ever SHRINKS the sum from its `a = 1` value, so no separate `a`
        term is needed once both are in-domain); anything else refuses
        — this closes `zeta(2, 1/1000)` (`a < 1` inflates the `a**-s`
        term to `~10**6`, silently clamped to `max(1.0, a)` by the
        round-11 formula instead of being refused).
      - `sin`/`cos`: exact REAL rational `x` (an exact `Rational` IS a
        real number by construction — no separate realness check
        needed) -> `|sin(x)|, |cos(x)| <= 1` always, for ANY real `x`.
      - `tanh`: exact real rational `x` -> `|tanh(x)| < 1` always for
        REAL `x` (`tanh`'s only poles are on the imaginary axis,
        `i*(pi/2 + k*pi)` — a non-real argument is refused outright,
        sidestepping the pole entirely rather than trying to bound
        `tan`'s own value near it).
      - `erf`: exact real rational `x` -> `|erf(x)| < 1` always for real
        `x` (`erfi`, `erf`'s own imaginary-axis restriction, grows like
        `e**(x**2)` — `erf(7*I)` `~= 1.6*10**20` — refused the same way,
        never estimated by an `e**|k|` bound, which the round-11 fix
        used and grok showed is simply the WRONG growth rate for this
        family).
      - `exp`: exact real rational `x`, capped at `MAX_HEAVY_ARG` for
        `x >= 0` (the ordinary "small argument, growing output" shape;
        `x < 0` shrinks toward 0, always safe regardless of magnitude)
        -> `exp(x) ~ 10**(x*log10(e))`, computed from the SIGNED exact
        `x`, never a sign-discarded magnitude (`exp(-10)`'s true value
        is tiny — `~4.5*10**-5` — but the round-11 formula read `-10`
        back as magnitude `10`, i.e. as if it were `+10`, claiming an
        astronomically large bound and false-refusing `factorial(floor(
        exp(-10)))`).

    A node whose relevant argument(s) are NOT exactly resolvable (not a
    free symbol, but not exactly rational either — a bare `I`,
    `zeta(2)` itself, `pi`, ...) refuses with the standard "cannot be
    safely bounded" message — `unknown != safe`, never silently passed
    through. A genuinely SYMBOLIC argument (has free symbols) is left
    unresolved with no violation, matching this module's established
    scope for a value that never materializes at all.
    """
    name = type(node).__name__
    if name not in _POLE_SENSITIVE_NAMES:
        return None

    def _unresolved_or_violation(arg):
        """`(handled, result)` — `handled=True` means the caller should
        `return result` immediately (either "genuinely symbolic, leave
        unresolved" or "cannot be safely bounded"); `handled=False`
        means `arg` resolved to an exact Rational, available via the
        SECOND call below."""
        if arg.free_symbols:
            return True, (0.0, 0.0, False, None)
        value = _resolve_exact_rational(arg, memo)
        if value is None:
            message = (f"an argument to {name}() cannot be safely bounded: "
                       "computing it would take an unbounded amount of time "
                       "and memory")
            return True, (0.0, 0.0, False, message)
        return False, value

    if name in ("gamma", "loggamma", "digamma"):
        if not node.args:
            return None
        handled, result = _unresolved_or_violation(node.args[0])
        if handled:
            return result
        z = result
        if z < 1:
            message = (f"an argument to {name}() cannot be safely bounded: computing "
                       "it would take an unbounded amount of time and memory")
            return 0.0, 0.0, False, message
        if name == "digamma":
            return _growth_digamma({0: math.log10(z)}), 0.0, True, None
        return _growth_gamma({0: math.log10(z)}), 0.0, True, None

    if name == "polygamma":
        # `z` (position 1) needs the exact-value gate for the SAME pole
        # reason as `gamma`/`digamma`/`loggamma` (`polygamma(order, z)
        # ~ -order! / z**(order+1)` as `z -> 0`). `order` (position 0)
        # ALSO needs an exact value here — not for a pole/sign reason
        # (its own dependence really is monotone, an upper-bound
        # magnitude would normally suffice), but because this module's
        # own "exact zero maps to magnitude 0.0" convention (`_log10_
        # num_den`'s own `Integer` handling, established for the print-
        # profile question, long before this round) makes `order = 0`
        # (a bare `digamma` call — see `_table_function_growth_
        # violation`'s own comment on the rewrite) INDISTINGUISHABLE
        # from `order = 1` through a magnitude alone: both round-trip
        # to `_safe_pow10(0.0) == 1.0`. `digamma(1/2)` (`order` truly 0,
        # genuinely safe) was wrongly treated as `order = 1` (needing
        # `z >= 1`, which `1/2` fails) by exactly this ambiguity before
        # `order` was resolved exactly here.
        if len(node.args) < 2:
            return None
        order_v = _resolve_exact_rational(node.args[0], memo)
        if order_v is None and node.args[0].free_symbols:
            order_v = None  # genuinely symbolic order -- treated as absent below
        elif order_v is None:
            message = ("an argument to polygamma() cannot be safely bounded: "
                       "computing it would take an unbounded amount of time and "
                       "memory")
            return 0.0, 0.0, False, message
        elif order_v != 0 and not (order_v.q == 1 and order_v >= 1):
            # #326 finding (grok, round 13 review of fad081f): a RESOLVED
            # order that is neither exactly `0` (digamma) nor a positive
            # INTEGER has no growth formula this module has derived --
            # most critically, a NEGATIVE order: `_growth_polygamma`'s
            # own `order! * zeta(order+1, z)` identity is for `order >=
            # 1` only, but the caller used to feed it `abs(order_v)`,
            # discarding the sign entirely, so `polygamma(-1, z)`
            # resolved as if `order` were `+1` -- a ~1-digit bound for a
            # call SymPy 1.14 actually evaluates as `loggamma(z) -
            # log(2*pi)/2` (an ANTIDERIVATIVE, `~ z*log(z)`, gamma(z)-
            # scale for a large `z`): `factorial(floor(polygamma(-1,
            # 700)))`, `bell(floor(Abs(polygamma(-1, 1463))))`, and
            # `rf(5, floor(polygamma(-1, 700)))` all passed this
            # screen's own cap with a claimed ~1-digit bound and then
            # constructed a gamma(1463)-scale value for real. A
            # non-integer order (`polygamma(3/2, z)`) is equally
            # unbounded here -- `order_v.q == 1` rejects it too, not
            # only a negative `order_v.q == 1 and order_v < 1` integer.
            message = ("an argument to polygamma() cannot be safely bounded: "
                       "computing it would take an unbounded amount of time and "
                       "memory")
            return 0.0, 0.0, False, message
        handled, z_result = _unresolved_or_violation(node.args[1])
        if handled:
            return z_result
        z = z_result
        if z < 1:
            message = ("an argument to polygamma() cannot be safely bounded: "
                       "computing it would take an unbounded amount of time and "
                       "memory")
            return 0.0, 0.0, False, message
        # `order_v` reaching here is guaranteed EXACTLY `0` or a positive
        # INTEGER `>= 1` (the `elif` above refuses everything else) --
        # `abs()` is a no-op now, kept only because `_growth_polygamma`'s
        # own magnitude convention (`_safe_pow10`) is always non-negative
        # regardless.
        bounds = {1: math.log10(z)}
        if order_v is not None:
            bounds[0] = math.log10(abs(order_v)) if order_v != 0 else 0.0
            # order == 0 stored as an EXACT flag, not the ambiguous
            # magnitude-0.0 convention -- `_growth_polygamma` reads
            # `_safe_pow10(bounds[0])` back as 1.0 for order 0 too, so
            # pass the TRUE order value forward via a dedicated key
            # instead of relying on that round-trip for this one case.
            bounds["order_exact_zero"] = (order_v == 0)
        return _growth_polygamma(bounds), 0.0, True, None

    if name == "zeta":
        if not node.args:
            return None
        handled, s_result = _unresolved_or_violation(node.args[0])
        if handled:
            return s_result
        s = s_result
        if len(node.args) >= 2:
            handled_a, a_result = _unresolved_or_violation(node.args[1])
            if handled_a:
                return a_result
            a = a_result
            if s.is_integer and s >= 2 and a >= 1:
                return math.log10(2), 0.0, True, None
            message = ("an argument to zeta() cannot be safely bounded: computing "
                       "it would take an unbounded amount of time and memory")
            return 0.0, 0.0, False, message
        if s.is_integer and s <= 0:
            n1 = abs(int(s)) + 1
            if n1 < 2:
                return 1.0, 0.0, True, None
            base = (_growth_stirling_factorial({0: math.log10(n1)})
                     - n1 * math.log10(2 * math.pi) + math.log10(4))
            return max(base, 0.0), 0.0, True, None
        if s >= 2:
            return math.log10(2), 0.0, True, None
        message = ("an argument to zeta() cannot be safely bounded: computing it "
                   "would take an unbounded amount of time and memory")
        return 0.0, 0.0, False, message

    # sin / cos / tanh / erf / exp -- all single-argument.
    if len(node.args) != 1:
        return None
    handled, result = _unresolved_or_violation(node.args[0])
    if handled:
        return result
    x = result
    if name in ("sin", "cos", "tanh", "erf"):
        return 0.0, 0.0, True, None  # |value| <= 1 for any exact real x
    # name == "exp": the SIGNED exact value drives the bound directly --
    # never a magnitude (see this function's own docstring for the
    # `exp(-10)` false-refusal this replaces).
    if x >= 0:
        if x > MAX_HEAVY_ARG:
            message = (f"the argument to exp() exceeds the limit of {MAX_HEAVY_ARG}: "
                       "computing it would take an unbounded amount of time and memory")
            return 0.0, 0.0, False, message
        return float(x) * math.log10(math.e), 0.0, True, None
    return 0.0, 0.0, True, None  # x < 0: exp(x) shrinks toward 0, always safe


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

    THE-1095 round-3-follow-up #5 (coordinator review of 5119c9d, grok
    `verify-1095-r6-grok.log`, issue 1 — the CENTRAL finding): this
    function did not understand a `_DeferredLShift`/`_DeferredRShift`/
    `_DeferredMod`/`_DeferredFloorDiv` marker node AT ALL — its class
    name is not a key of `_FUNCTION_ARG_CAPS`, not `Mul`, not `Add` — so
    `bell(1 << 11)` (a table call whose ARGUMENT is a marker) left this
    function reporting "unresolved" for that argument, and `_function_
    arg_cap_violation`'s own per-position loop (see its own comment)
    used to treat an unresolved, NON-symbolic argument as "skip, nothing
    to check" instead of "unknown, refuse" — the token screen sees two
    small literals (`1`, `11`), the deferred scan sees an opaque marker
    it shrugs off, and step 3's REAL parse (stock `EvaluateFalseTransformer`,
    which leaves `<<` unvisited) evaluates `1 << 11` for real and calls
    `bell(2048)`. `_resolve_marker_magnitude` (below) closes this by
    making THIS function — the ONE resolver every consumer already
    shares (table-argument caps, a `Pow`'s base, `Mul`/`Add`
    composition, and now `_deferred_binop_violation` itself, reduced to
    a thin caller of this same function) — understand markers too,
    recursively, with the identical `<<`/`>>`/`%`/`//` rules
    `_deferred_binop_violation` already had. This is also what fixes
    CHAINED markers (`1 << 2 << 3`, `7 % 3 % 2`) — the inner marker is
    now just another node this function resolves before the outer one
    needs its magnitude.

    Also handles `floor`/`ceiling`/`Abs` of a single, otherwise-
    resolvable argument (grok item 3): none of the three can make a
    result's magnitude EXCEED its argument's own (`Abs` is exactly equal;
    `floor`/`ceiling` round toward an adjacent integer, off by at most
    1 — negligible at the digit scale this module bounds), so
    `factorial(floor(2.5))` (`== factorial(2)`, main evaluates it) is not
    left unresolved just because its argument is wrapped.
    """
    from sympy import Add, Float, Function, Mul, NumberSymbol, Pow

    if isinstance(node, Float):
        # #326 finding (grok, round 9 / verify-1095-r7-grok.log): checked
        # BEFORE `_log10_num_den`'s own fast path, which reports every
        # `Float` "fully resolved" at print-profile magnitude 0 — correct
        # for THAT question (is printing this bare `Float` safe — always
        # yes), wrong for THIS one (how large is this VALUE, for a
        # consumer that is about to convert it to an `Integer` via
        # `floor`/`ceiling`, use it as a `Mul`/`Add` factor, or shift by
        # it) — see `_float_value_log10`'s own docstring for the full
        # account and a live repro.
        return _float_value_log10(node), 0.0, True, None
    log_num, log_den, resolved = _log10_num_den(node, memo)
    if resolved:
        return log_num, log_den, True, None
    type_name = type(node).__name__
    if type_name in _DEFERRED_BINOP_OPS:
        return _resolve_marker_magnitude(node, memo)
    if isinstance(node, NumberSymbol):
        # THE-1095 round-3-follow-up #5 (coordinator review of 5119c9d,
        # grok item 3's own false-refusal side effect): an irrational
        # constant (`pi`/`E`/`EulerGamma`/`GoldenRatio`/`Catalan`/...) is
        # `Basic.free_symbols`-EMPTY (a constant, not a variable) but was
        # never handled by `_log10_num_den` (only `Integer`/`Mul`/`Add`/
        # `Pow`/`Rational`/`Float`) — "unresolved, no free symbols" is
        # EXACTLY the shape item 3's new refusal targets, so `factorial(
        # digamma(1463))` (whose real, eager `digamma.eval()` produces an
        # exact `hugely-precise-rational - EulerGamma`, itself an `Add`
        # this function's own composition then tries and fails to resolve
        # because of the bare `EulerGamma` term) newly REFUSED instead of
        # correctly staying symbolic (`factorial()` of a provably non-
        # Integer argument never materializes anything, regardless of how
        # large that argument's own EXACT form happens to be — there is
        # no real hazard here at all). Every built-in irrational
        # `NumberSymbol` SymPy ships is a small, O(1) constant (all under
        # 10 in absolute value) — a generous, safe, constant bound, not a
        # growth formula, since none of them scale with anything.
        return math.log10(10), 0.0, True, None
    if isinstance(node, Function) and type_name in _POLE_SENSITIVE_NAMES:
        # #326 finding (grok, round 12 review of 4d94871): checked BEFORE
        # both the generic `_FUNCTION_ARG_CAPS` table-growth path AND the
        # `_ELEMENTARY_BOUND_RULES` path below — this REPLACES both for
        # these nine names specifically, never falls through to either
        # (a magnitude-only bound is never sound for a pole/sign-
        # dependent formula — see `_pole_sensitive_magnitude`'s own
        # docstring for the full account).
        pole_result = _pole_sensitive_magnitude(node, memo)
        if pole_result is not None:
            return pole_result
    if isinstance(node, Function) and type_name in _FUNCTION_ARG_CAPS:
        bound, violation = _table_function_bound(node, memo)
        if violation:
            return 0.0, 0.0, False, violation
        if bound is not None:
            return bound, 0.0, True, None
    if isinstance(node, Function) and type_name in ("floor", "ceiling", "Abs", "frac") and len(node.args) == 1:
        a_log_num, a_log_den, a_resolved, a_violation = _resolve_arg_magnitude(node.args[0], memo)
        if a_violation:
            return 0.0, 0.0, False, a_violation
        if a_resolved:
            # #326 finding (coordinator round 10 replay of 6e72d70,
            # probe 2): "off by at most 1" bounds the RESULT's own
            # digit count, but says nothing about the CONSTRUCTION cost
            # -- floor/ceiling/frac of a genuinely transcendental exact
            # value (`polygamma(1463, 1)`, an exact `Mul` involving
            # `pi**1464`, never a plain `Integer`) forces an expensive
            # high-precision `evalf` to determine the integer boundary.
            # See `_evalf_coercion_cheap`'s own docstring for the full
            # account and the two shapes that stay genuinely cheap
            # regardless of magnitude.
            if not _evalf_coercion_cheap(node.args[0], a_log_num, a_log_den):
                return 0.0, 0.0, False, (
                    f"the argument to {type_name}() cannot be safely coerced "
                    "to a number: computing it would take an unbounded "
                    "amount of time and memory")
            if type_name == "frac":
                return 0.0, 0.0, True, None  # frac(x) is always in [0, 1)
            if type_name == "Abs":
                return a_log_num, a_log_den, True, None
            return a_log_num - a_log_den, 0.0, True, None  # floor/ceiling: always an integer
    if isinstance(node, Function) and type_name in _ELEMENTARY_BOUND_RULES:
        elem_bound = _elementary_function_magnitude(node, memo)
        if elem_bound is not None:
            e_log, e_resolved, e_violation = elem_bound
            if e_violation:
                return 0.0, 0.0, False, e_violation
            if e_resolved:
                return e_log, 0.0, True, None
    if isinstance(node, Pow):
        # #326 finding D (grok, round 9): `_log10_num_den`'s own `Pow`
        # handling (both its bare-integer-exponent branch and its
        # compound-exponent branch) resolves a BASE via `_log10_num_den`
        # directly, or via `_resolved_table_aware_magnitude` for the
        # EXPONENT only — neither one recurses through THIS function, so
        # neither understands a marker or an irrational `NumberSymbol` as
        # a base, and the integer-exponent branch does not even try
        # `_table_function_bound` for a table-call base at all (only the
        # SEPARATE, non-`_log10_num_den`-integrated `_pow_table_base_
        # violation` check does, and only as a REFUSAL-or-nothing
        # verdict, never a magnitude other callers can compose with).
        # `factorial((1 << 3)**2)`, `factorial(2**(1 << 3))`, `factorial(
        # fibonacci(5)**2)` (all plain integers on `origin/main` — 64!,
        # 256!, 25!) stayed "unresolved" here and hit the item-3
        # structural backstop instead. Fixed the same way markers/
        # `NumberSymbol`/table calls already are — as this function's OWN
        # branch, recursing base and exponent through itself, so ANY
        # shape either one resolves (marker, table call, `NumberSymbol`,
        # nested `Pow`, ...) composes correctly. Deliberately narrower
        # than `_log10_num_den`'s own `Pow` handling: only fires for a
        # NON-NEGATIVE, resolvable exponent and a base whose own
        # magnitude is >= 1 (`base_log >= 0`) — a shrinking base (`|base|
        # < 1`) raised to a growing power does not grow without bound the
        # way this function needs to guard against, and getting a SAFE
        # bound for that shape would need the exponent's LOWER bound, not
        # its upper one (this resolver only ever computes upper bounds);
        # left unresolved instead, exactly as before this branch existed,
        # so an existing caller's own fallback keeps deciding that shape.
        base, exp = node.args
        if not (base.free_symbols or exp.free_symbols):
            base_log_num, base_log_den, base_resolved, base_violation = _resolve_arg_magnitude(base, memo)
            if base_violation:
                return 0.0, 0.0, False, base_violation
            if base_resolved:
                base_log = base_log_num - base_log_den
                if base_log >= 0:
                    exp_log_num, exp_log_den, exp_resolved, exp_violation = _resolve_arg_magnitude(exp, memo)
                    if exp_violation:
                        return 0.0, 0.0, False, exp_violation
                    if exp_resolved:
                        exp_log = exp_log_num - exp_log_den
                        exp_value = _safe_pow10(exp_log)
                        if exp_value is not None and exp_value >= 0:
                            return base_log * exp_value, 0.0, True, None
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


def _exact_literal_log10(node) -> float | None:
    """`log10(|value|)` for `node`, ONLY if `node` is an EXACT numeric
    literal (`Integer`, `Rational`, or `Float`) whose value is fully
    known without any composition or growth-bound estimation. Used as a
    LOWER bound on a `//` marker's divisor by `_resolve_marker_magnitude`
    — an UPPER bound (what `_resolve_arg_magnitude` normally computes) is
    the WRONG direction for a divisor: a SMALLER divisor gives a LARGER
    quotient, so bounding `a // b` needs to know `|b|` cannot be
    SMALLER than some value, not that it cannot be LARGER. An exact
    literal's own value doubles as its own exact lower bound; nothing
    else here can (a table call's growth bound, a marker's own bound, a
    `Mul`/`Add` composition are all upper bounds by construction — see
    each one's own docstring — never a sound lower bound on anything).
    `None` for anything else (a table call, a nested marker, a `Mul`/
    `Add`, a `NumberSymbol`, a free symbol, or an exact zero — `a // 0`
    raises `ZeroDivisionError` at real construction, not a growth
    hazard, and `log10(0)` is undefined) — deliberately narrow, not a
    general resolver.
    """
    from sympy import Float, Integer, Rational

    if node.is_zero:
        return None
    if isinstance(node, Integer):
        return _log10_of_int(int(node))
    if isinstance(node, Rational):
        den_log = _log10_of_int(node.q) if node.q != 1 else 0.0
        return _log10_of_int(node.p) - den_log
    if isinstance(node, Float):
        return _float_value_log10(node)
    return None


#: Table-function names PROVEN to always return a plain INTEGER (never a
#: genuine fraction strictly between -1 and 1, excluding 0) for their
#: whole accepted domain — the standard combinatorial/number-theoretic
#: sequences this table already bounds. Deliberately EXCLUDES `bernoulli`
#: (rational, `bernoulli(1) == -1/2`), `harmonic` (rational, can be < 1
#: for a small `n`), and every irrational/transcendental entry (`digamma`,
#: `zeta`, `gamma`, `loggamma`, `polygamma`) — none of those three
#: shapes can serve as a `//` divisor's lower bound (see
#: `_divisor_lower_log10`'s own docstring).
_INTEGER_VALUED_TABLE_NAMES = frozenset({
    "factorial", "factorial2", "subfactorial", "binomial", "fibonacci",
    "lucas", "tribonacci", "catalan", "primorial", "prime", "primepi",
    "rf", "ff", "npartitions", "totient", "divisor_sigma", "bell",
    "genocchi", "motzkin", "andre", "partition", "nextprime", "mobius",
    "isprime",
})


def _divisor_lower_log10(node, memo: dict) -> float | None:
    """A sound LOWER bound on `log10(|node|)`, for `_resolve_marker_
    magnitude`'s own `//` handling — `_exact_literal_log10`'s own
    docstring explains why an UPPER bound (what `_resolve_arg_magnitude`
    normally computes) is the wrong direction for a divisor. Two sources:
    an EXACT numeric literal's own value, or a nested call to a table
    function PROVEN to always return an INTEGER (`_INTEGER_VALUED_TABLE_
    NAMES`) — such a function's result is either exactly 0 (safe
    regardless: `a // 0` raises `ZeroDivisionError` at real construction,
    not a growth hazard) or a nonzero integer with `|value| >= 1`, so
    `0.0` (`log10(1)`) is a sound lower bound either way — `7 // bell(20)`
    (`bell(20)` is comfortably within its own cap, an INTEGER, `origin/
    main` gives `0` since `bell(20)` dwarfs `7`) is what this closes:
    without it, item A's own new `//` fix would fail CLOSED on this
    previously-passing, genuinely safe shape (the table CALL itself was
    never the danger here — `1 // 0.0005`, a genuine FRACTIONAL literal
    divisor, is). `None` (no lower bound claimed) for anything else — a
    `bernoulli`/`harmonic`/`digamma`/`zeta`/`gamma`/`loggamma`/
    `polygamma` call CAN genuinely return a value strictly between -1 and
    1 (`bernoulli(1) == -1/2`), so no lower bound holds for those.
    """
    literal = _exact_literal_log10(node)
    if literal is not None:
        return literal
    from sympy import Function

    name = type(node).__name__
    if isinstance(node, Function) and name in _INTEGER_VALUED_TABLE_NAMES:
        _bound, violation = _table_function_bound(node, memo)
        if violation is None:
            return 0.0
    return None


def _resolve_marker_magnitude(node, memo: dict) -> tuple[float, float, bool, str | None]:
    """`_resolve_arg_magnitude`'s own contract, for a `_DeferredLShift`/
    `_DeferredRShift`/`_DeferredMod`/`_DeferredFloorDiv` marker node, now
    shared by EVERY consumer of `_resolve_arg_magnitude` instead of
    living only in `_deferred_binop_violation`'s own body — see that
    function's own docstring, and `_resolve_arg_magnitude`'s own round-
    3-follow-up #5 paragraph, for why this had to move here: a marker
    node can appear ANYWHERE a number can (a table call's own argument,
    a `Pow`'s base, one factor of a `Mul`, ...), not only as the direct
    target `_deferred_binop_violation` itself is checking.

    Recurses through `_resolve_arg_magnitude` for both operands, so a
    CHAINED marker (`1 << 2 << 3` == `_DeferredLShift(_DeferredLShift(1,
    2), 3)`) resolves the inner one first, the same way a nested `Mul`/
    `Add`/table call already does. An operand with a free symbol is left
    "unresolved, no violation" (`return ..., False, None`) — genuinely
    symbolic, never materializes, matching `_table_function_bound`'s own
    per-position `if arg.free_symbols: continue`; the CALLER decides
    whether "unresolved AND not symbolic" is a refusal (item 3's own
    fix, in `_function_arg_cap_violation` and `_deferred_binop_
    violation`), not this function.

    #326 finding A (Codex, round 9 review of 5119c9d,
    `verify-1095-r7.log`, finding 1 — High): `%` and `//` both used the
    IDENTICAL "bounded by the left operand alone" rule this docstring
    used to claim for all three of `>>`/`%`/`//`. That rule is only true
    for `>>`. `%`: `0 <= |a % b| < |b|` unconditionally (Python's `%`
    always returns a result with the SAME SIGN as `b`, strictly smaller
    in magnitude) — the bound is the RIGHT operand's own magnitude, not
    the left's: `-1 % 10**4 == 9999`, nowhere near `|-1| == 1`. `//`: `a
    // b` can EXCEED `|a|` when `|b| < 1` (`1 // 0.0005 == 2000`) — a
    sound bound needs a LOWER bound on `|b|`, which `_exact_literal_
    log10` (above) supplies only for an exact numeric literal; anything
    else for the divisor fails CLOSED (`unknown != safe`, this module's
    own established bar, not a new one invented for this case). Both
    operands are still resolved regardless of which one a given
    operator's OWN formula reads, so a hazard hiding in the operand the
    formula does not need (an over-cap nested table call, say) still
    surfaces as a `violation` — `_deferred_binop_violation`'s own caller
    only ever sees THIS function's return value.
    """
    op = _DEFERRED_BINOP_OPS[type(node).__name__]
    left, right = node.args[0], node.args[1]

    left_known = False
    left_log = 0.0
    if not left.free_symbols:
        l_num, l_den, left_resolved, left_violation = _resolve_arg_magnitude(left, memo)
        if left_violation:
            return 0.0, 0.0, False, left_violation
        if left_resolved:
            left_known = True
            left_log = l_num - l_den

    right_known = False
    right_log = 0.0
    if not right.free_symbols:
        r_num, r_den, right_resolved, right_violation = _resolve_arg_magnitude(right, memo)
        if right_violation:
            return 0.0, 0.0, False, right_violation
        if right_resolved:
            right_known = True
            right_log = r_num - r_den

    if op == "<<":
        if not (left_known and right_known):
            return 0.0, 0.0, False, None
        count = _safe_pow10(right_log)
        if count is None or count < 0:
            return math.inf, 0.0, True, None
        return left_log + count * math.log10(2), 0.0, True, None

    if op == ">>":
        # `a >> b` for a non-negative shift count `b` never exceeds `a`'s
        # own magnitude — a NEGATIVE `b` raises `ValueError` at real
        # construction (Python itself refuses it), not a growth hazard.
        if not left_known:
            return 0.0, 0.0, False, None
        return left_log, 0.0, True, None

    if op == "%":
        if not right_known:
            return 0.0, 0.0, False, None
        return right_log, 0.0, True, None

    # op == "//"
    if not left_known:
        return 0.0, 0.0, False, None
    divisor_lower_log = _divisor_lower_log10(right, memo)
    if divisor_lower_log is None:
        return 0.0, 0.0, False, None  # caller decides: refuse if non-symbolic
    return left_log - divisor_lower_log, 0.0, True, None


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

    THE-1095 round-3-follow-up #5 (coordinator review of 5119c9d, grok
    `verify-1095-r6-grok.log`): a THIN caller of `_resolve_arg_magnitude`
    now — the actual `<<`/`>>`/`%`/`//` bound RULES moved to that
    function's own `_resolve_marker_magnitude` helper, so every consumer
    of `_resolve_arg_magnitude` (a table call's own argument, a `Pow`'s
    base, a `Mul`/`Add` composition) understands a marker node too, not
    only this ONE call site. `bell(1 << 11)` used to sail past every
    screen this module has: the token screen sees two small literals
    (`1`, `11`), the deferred tree has `_DeferredLShift(1, 11)` as
    `bell`'s own argument, and `_resolve_arg_magnitude` — before this
    round — had never heard of a marker node, so `_function_arg_cap_
    violation`'s own per-position loop read that as "unresolved, skip",
    and step 3's real parse then evaluated `1 << 11` for real and called
    `bell(2048)`. See `_resolve_arg_magnitude`'s own docstring for the
    full account of the fix, and `_function_arg_cap_violation`'s own for
    the matching "unresolved-but-not-symbolic is now a refusal, never a
    skip" half of it.
    """
    op = _DEFERRED_BINOP_OPS.get(type(node).__name__)
    if op is None:
        return None
    log_num, log_den, resolved, violation = _resolve_arg_magnitude(node, memo)
    if violation:
        return violation
    if not resolved:
        if node.free_symbols:
            return None  # genuinely symbolic -- left to the real parse
        return (f"the result of '{op}' cannot be safely bounded: computing it "
                "would take an unbounded amount of time and memory")
    return _digit_ceiling_text(log_num - log_den, f"the result of '{op}'")


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
            # THE-1095 round-3-follow-up #5 (coordinator review of
            # 5119c9d, grok item 3): a NUMERIC (no free symbols already
            # ruled that out, above) argument the resolver STILL cannot
            # bound (an irrational constant, a non-table function this
            # module has no growth formula for, ...) is `unknown != safe`
            # here, the same bar every other finding in this module
            # already holds to — refusing, never silently skipping, is
            # what closes the structural half of grok's own finding: a
            # marker node used to be exactly this "unresolved, skip"
            # shape (before `_resolve_arg_magnitude` learned to resolve
            # one), so `bell(1 << 11)` passed this loop with NOTHING
            # bounded, and step 3's real parse evaluated `1 << 11` and
            # called `bell(2048)` for real. Resolving markers (this
            # round's own main fix) closes THAT specific gap already;
            # this refusal is what keeps the NEXT unrecognised numeric
            # shape from reopening the identical door instead of a
            # symbolic argument, which stays a legitimate skip.
            return (f"an argument to {type(node).__name__}() cannot be safely "
                    "bounded: computing it would take an unbounded amount of "
                    "time and memory")
        domain_violation = _domain_violation(type(node).__name__, pos, arg, arg_log_num, arg_log_den)
        if domain_violation:
            return domain_violation
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
    zeta_violation = _zeta_two_arg_domain_violation(node)
    if zeta_violation:
        return zeta_violation
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
        growth_violation = _table_function_growth_violation(node, memo)
        if growth_violation:
            return growth_violation
        evalf_violation = _evalf_coercion_violation(node, memo)
        if evalf_violation:
            return evalf_violation
        n_violation = _n_precision_violation(node, memo)
        if n_violation:
            return n_violation
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
                growth_violation = _table_function_growth_violation(node, memo)
                if growth_violation:
                    return growth_violation
            if isinstance(node, Function) and type(node).__name__ in _EVALF_COERCION_NAMES:
                evalf_violation = _evalf_coercion_violation(node, memo)
                if evalf_violation:
                    return evalf_violation
            if isinstance(node, Function) and type(node).__name__ == "N":
                n_violation = _n_precision_violation(node, memo)
                if n_violation:
                    return n_violation
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
