# Changelog

All notable changes to this project are documented here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## Two version numbers, on purpose

This project versions **two** things, and they are not the same number.

| What | Where | Current |
|---|---|---|
| The **package** — the tool surface, the CLI, the Python API | `pyproject.toml`, `executor/Cargo.toml`, this file | see `version` in [`pyproject.toml`](pyproject.toml) — this cell is not re-typed on every release |
| The **result contract** — the shape every tool result comes back in | `docs/contract/README.md`, `contract_version` on every result | `1.14.0` |

The contract is at `1.14.0` and the package is at `0.x` because those claims are
genuinely different. The result contract has a published JSON Schema, a
documented MAJOR/MINOR/PATCH policy, a twelve-month deprecation window, and a
gate that fails if the schema drifts from the code — it is stable and says so.
The package's tool surface is not: the roadmap restructures how requests are
described and how execution backends plug in, and calling it `1.0.0` today would
promise a stability nobody should rely on.

[Semver's own guidance](https://packaging.python.org/en/latest/discussions/versioning/)
is that reaching `1.0.0` means accepting MAJOR/MINOR/PATCH semantics, not
"enough features have landed". `0.x` here is a statement about the tool surface
only. Treat `0.y.z` as if it were `1.y.z` anyway: `y` bumps for breaking changes,
`z` for compatible ones.

If you pin one thing, pin `contract_version` — it is the number with a policy
behind it.

---

## [Unreleased]

### Fixed

- **The heavy-call ceiling matched a `Function` node by name, and several
  names never leave a matching one behind** (THE-1095; follow-up to GH
  #326): `rf(1463+1, 2)`, `ff(1463+1, 2)`, `primepi(1463*1000)`, and
  `binomial(1463+1, 700)` evaluated for real regardless of `evaluate=False`
  (their own `eval()` does its own Python arithmetic on the argument,
  un-gated by the parser's `evaluate=False` rewrite, which only reaches
  `Add`/`Mul`/`Pow`/`Sub`/`Div` and a fixed trig/log/`sqrt`/`cbrt`
  whitelist), so the cap never saw a computed argument to bound — a
  bypass. `digamma(1463+1)` gets REWRITTEN to `polygamma(0, ...)` at
  construction, a different class at a different argument position, so
  the by-name lookup never matched it either. In the other direction,
  `primorial`, `prime`, `motzkin`, `isprime`, and `factorint` raised
  `ValueError` ("... is not an integer") on ANY computed argument, even
  one nowhere near the cap (`primorial(2+3)`) — a wrong refusal, since
  each coerces its argument to a concrete `int` during `safe_parse`'s own
  `evaluate=False` pre-parse regardless of the value it represents.
  `safe_parse` now also runs that pre-parse through a second,
  `_deferred_global_dict()`-backed shape — an inert stand-in for every
  name in `_FUNCTION_ARG_CAPS` (`sqrt`/`cbrt` excepted, unaffected and
  already covered elsewhere), named and positioned exactly as the caller
  wrote it, that never evaluates and never raises — feeding
  `_numeric_ceiling_scan` a real tree to bound in both directions, while
  the original real-function shape keeps every existing check (including
  `sqrt`/`cbrt`'s construction-cost backstop, which depends on a literal
  heavy-function argument evaluating for real) untouched. **Revised after
  cross-vendor review of the first version of this fix** (`verify-1095`,
  both reviewers FAILED it): the deferred, inert-stand-in parse now runs
  and refuses FIRST, before the real-function parse ever touches the
  expression at all — the first version ran the real parse first, so a
  token-cheap-but-real-function-expensive call (`bell(1463)+
  factorial(1463+1)`, `nextprime(10**2000)`) still paid the real, un-bounded
  cost before its eventual refusal (measured 6.77s vs 0.005s). The
  deferred stand-ins now also carry the real class's own arity (`nargs`,
  copied from SymPy where it exposes one) so a malformed call
  (`binomial(1463+1)`, missing `k`) is refused as the SAME validation
  error `origin/main` gives, not miscategorized as a ceiling; and the
  tree-level cap now bounds only the FIRST positional argument, not
  every one, so `binomial(5, 1463+1)`/`ff(5, 1463+1)` (`k > n`, cheap
  and legitimately `0` on `origin/main`) evaluate instead of being
  wrongly refused. `root` is now also a deferred stand-in, closing the
  same real-function-runs-first gap for its own value argument.
  **Revised again after a second cross-vendor review** (`verify-1095-r2`,
  both reviewers FAILED it, on overlapping findings): the ONE per-name
  spec (`_FUNCTION_ARG_CAPS` + `_EXTRA_BOUNDED_POSITIONS`, read through
  `_position_spec`/`_bounded_positions`) now drives the token screen, the
  tree scan, arity, AND nesting together, closing four more gaps the
  "bound one position, one layer" pattern kept reopening: (1) `rf`/`ff`'s
  own `k` (position 1, the iteration count `reduce(..., range(int(k)),
  1)` actually loops over — no O(1) shortcut the way `binomial`'s `k`
  has) is now capped too — `rf(5, 1000**1000)`/`ff(5, 1000**1000)` used
  to hang past 4s; `polygamma` (not just `digamma`, its rewrite target)
  is now in the table, bounded at its own `z` (position 1); `nextprime`'s
  `ith` and `divisor_sigma`'s `k` (both position 1) are bounded too.
  DELIBERATE NARROWING, pinned in tests: `binomial`'s own `k` stays
  unbounded (`eval()`'s `k > n` shortcut is genuinely O(1)), but `rf`/
  `ff`'s `k` has no such shortcut, so `ff(5, 1463+1)`/`rf(5, 1463+1)`
  (`k > n`, `0` cheaply on `origin/main`) — and the position-0 short
  circuits Codex found (`binomial(1463+1, 0)` = `1`, `binomial(1463+1,
  1463+2)` = `0`, `rf(1463+1, 0)` = `ff(1463+1, 0)` = `1` on
  `origin/main`) — are now refused instead: closing a real hang is worth
  a false refusal on this one narrow shape, and `guarded_call`'s CPU
  backstop was `origin/main`'s only protection for it regardless.
  (2) A NESTED table-function argument (`divisors(factorial(100))`,
  measured 11.9s; `polygamma(1, factorial(100))`; `sqrt(bell(1463))`,
  ~5s; `root(factorial(1463), 2)`; `(x+1)**bell(1463)`) used to be
  silently "unresolved" and skipped — `_table_function_bound` now gives
  each table name a safe, loose-but-finite GROWTH-bound formula (`n**n`
  for `factorial`/`bell`/..., `2**n` for `binomial`, `(x+k)**k` for
  `rf`/`ff`, the standard asymptotic bounds for `primorial`/`prime`/
  `nextprime`/`primepi`/`npartitions`/`harmonic`, exponential-base bounds
  for `fibonacci`/`lucas`/`tribonacci`/`catalan`, `<= n` for `totient`),
  so a nested call is bounded from its OWN argument caps without ever
  materializing it — refusing in milliseconds instead. The blanket
  TOKEN-level "any nesting of two heavy calls is refused" rule this
  replaced was ALSO wrong the other way: `factorial(fibonacci(5))` (`=
  120`) and `factorial(binomial(6, 3))` now correctly EVALUATE instead
  of a refusal that could not tell a cheap nesting from a dangerous one.
  A table name with no growth formula (the eager-factor family, `sqrt`/
  `root`/`cbrt`) still fails closed if nested — `unknown != safe`.
  (3) The eight plain-callable names (and `root`) now derive their own
  arity from `inspect.signature` of the real callable, so a wrong-arity
  call (`isprime(10**26, 1)`) fails the DEFERRED parse itself with
  `origin/main`'s own `TypeError` text, instead of reaching a ceiling
  refusal on an over-cap first argument before arity was ever checked.
  (4) `rf(5, 1463+1463)` (`k = 2926`) evaluates cleanly through every
  per-position cap yet still produces an 8_887-digit integer
  (`(5+2926)**2926`) — no combination of two individually-in-cap
  positions can be proven jointly safe without either a growth bound or
  a check on the actual result, so `safe_parse`'s own evaluate=True
  step now runs its numeric RESULT through the same digit-count ceiling
  every other path already has. Also fixed, found investigating the
  ClusterFuzzLite OOM this round: `%`/`//`/`<<`/`>>` are not
  `evaluate=False`-protected by SymPy's own parser for their WHOLE
  subtree (`EvaluateFalseTransformer.visit_BinOp` only special-cases
  `Add`/`Mult`/`Pow`/`Sub`/`Div`/`BitOr`/`BitAnd`/`BitXor`, and returns
  any OTHER operator's node completely unchanged, without visiting its
  children at all) — `x**<78-digit literal> % 11` hangs inside
  `sympy.core.mod.Mod.eval`'s own `gcd()` call with genuinely growing
  memory, and the numeric-base form (`2**<...> % 11` / `// 11` / `<< 2`)
  hangs materializing the literal integer itself. `safe_parse` now
  refuses, at the token level, before either pre-parse, when one of
  these four operators is combined with an exponent (`**`/`^`) that is
  not provably small (a literal at or under 200) — `7 % 3`, `10 // 3`,
  `2**10 % 7` stay byte-identical; `Mod` (reachable by NAME, not just the
  `%` operator, and just as eager) is now also a deferred stand-in.
  DELIBERATE NARROWING, pinned in tests: `2**300 % 7` is refused even
  though `origin/main` evaluates it fine — the token screen cannot tell
  a merely-over-200 exponent apart from a genuinely dangerous one
  without parsing more than tokens. `eval_exact` (a separate, already-
  independently-bounded evaluator that also uses `%`/`<<`/`>>`
  natively, `tests/test_calc_port.py`) is untouched — this fix lives
  only in `safe_parse`'s own pipeline, never the shared `classify_unsafe`.
  Also fixed, the refusal message for the four operators above used to
  interpolate the `**`/`^` TOKEN it was keyed off (always literally
  `'**'`), not the eager operator actually responsible — `x**N % 11`
  read as "'**' combined with exponentiation", naming no real operator
  at all; each of the four now names ITSELF.

  **A ClusterFuzzLite crash on the commit above** (`TypeError: 'property'
  object is not iterable`, `reject_explosive` -> `Basic.free_symbols`):
  a bare CLASS reference (`Pow`, or any other name this module's
  `safe_global_dict()`/`_deferred_global_dict()` expose, referenced
  without being called) can end up nested one level inside ANOTHER
  node's own `.args` tuple, where `_walk`'s existing per-node guard
  (which already caught a bare class at the TOP of a tree) never got a
  chance to run before `reject_explosive`'s own code called
  `.free_symbols` directly on that ancestor — `SomeClass.free_symbols`,
  accessed on the class rather than an instance, is the unbound
  `property` descriptor, and SymPy's own `Basic.free_symbols` getter
  cannot iterate it. Confirmed this is a LATENT bug predating THE-1095
  entirely: `origin/main` (6cda9d4) crashes identically on the same
  decoded fuzz input, at its own (differently-numbered) `reject_
  explosive` line. `reject_explosive` now walks the whole tree once,
  up front, and fails closed (a `ceiling` refusal, not a crash) on any
  node that is a bare Python `type` — deliberately NOT the broader
  "not a `sympy.Basic` instance" (tried first, and found to reject every
  ALREADY-EAGERLY-EVALUATED plain-Python result a heavy function's own
  eager `eval()` can legitimately hand back as the whole tree — `bool`
  from `isprime`, `dict` from `factorint`, `list` from `divisors`/
  `primefactors`, plain `int` from `prime`/`primorial` — none of which
  is a `Basic` instance, and all four are ordinary, safe, already-
  computed leaves this module's own tables intentionally return).

  **Cross-vendor review of the crash fix, folded into the same commit**
  (grok, `verify-1095-r3-grok.log`; Codex): (A) `_growth_primorial`
  bounded `primorial(n, nth=False)` (primes `<= n`, `~e**(1.02n)`), not
  the DEFAULT no-second-arg form (`nth=True`: the product of the FIRST
  `n` primes, asymptotically `~e**p_n`, `p_n` the n-th prime itself,
  far bigger) — `factorial(primorial(6))` (`primorial(6) == 30_030` in
  the default form) used to be bounded at only ~455, UNDER factorial's
  own 1_463 cap, wrongly letting a `factorial(30_030)` construction
  proceed. Fixed by reusing `_growth_prime`'s own `p_n` bound; safe for
  the `nth=False` form too, since "first n primes" always runs to a
  larger magnitude than "primes <= n" for the same `n`. (B)
  `_growth_nextprime` ignored `ith` (position 1) entirely, bounding
  `nextprime(n, ith)` as if `ith` were always 1 (Bertrand's postulate,
  `< 2n`) — `nextprime(2, 1000) == 7_927`, over factorial's own cap,
  was bounded at ~4, safe. Fixed by folding `ith` into the bound via the
  prime number theorem's average gap near `n`, generously slackened.
  (C) A table-function call as `<<`'s SHIFT COUNT bypassed the operator
  screen entirely (no `**`/`^` token involved): `1 << factorial(20)`
  attempts to construct a number with ~2.4e18 bits. Fixed at the root,
  designed together with the crash fix (same "an exception must never
  silently mean safe" principle, at the two different places it can
  occur): (i) a new token-level check, `_shift_digit_ceiling_violation`,
  bounds a `<<`'s left operand (a literal or a single-literal-argument
  table call, via a Stirling-accurate `log10(n!)` approximation — NOT
  the existing, deliberately loose `_growth_nn_loose`/`_growth_nn_tight`
  nested-argument bounds, which overestimate `factorial`/`bell` at
  their own cap enough to falsely refuse an unshifted, safe
  `bell(1463) << 1`) and right operand (the shift count itself, same
  two shapes) before any real construction; (ii) a deferred-pre-parse
  exception (`scan_shape` in `safe_parse`) that used to be silently
  treated as "inconclusive, fall through to the real-dict parse" now
  fails closed instead — UNLESS the expression contains neither a table
  name nor an unprotected operator (an ordinary syntax error still
  falls through to `origin/main`'s own text) — EXCLUDING `TypeError`
  specifically, since an inert deferred stand-in never becoming a
  concrete number makes `factorial(20) << 3`-shaped `TypeError:
  unsupported operand type(s)` an expected, harmless, unconditional
  consequence of the stand-in design, not a hazard signal; refusing on
  it would have broken the ENTIRE working combination of a heavy
  function and `%`/`//`/`<<`/`>>` (found live testing this exact
  branch). (D) `_growth_fib_like` computed `(2*phi)**n` (multiplying
  the base by 2 inside the exponentiation) instead of the intended
  `2*phi**n` — over-conservative enough to falsely refuse
  `factorial(fibonacci(16))` (`fibonacci(16) == 987`, comfortably under
  cap). Replaced with an exact Binet-formula bound
  (`fibonacci(n) = round(phi**n / sqrt(5))`) for `fibonacci`, and a
  matching but un-divided bound for `lucas`; `digamma` moved out of the
  `n**(2n)` catch-all bucket into its own logarithmic bound (the same
  shape `polygamma`'s already had, just read from position 0 instead of
  1) — the catch-all had been reading digamma's own ARGUMENT as if it
  were a factorial-style output magnitude, false-refusing
  `factorial(digamma(1463))`. (E) The real-dict catch-all (any
  exception besides `TypeError` from the real, `evaluate=False`
  pre-parse used to fall through to `evaluate=True` unconditionally) is
  now narrowed to EXACTLY a `ValueError` whose message contains "is not
  an integer" (group B's own documented eager-coercion class) —
  everything else, INCLUDING `motzkin`'s differently-worded ValueError
  ("must be a positive integer"), `ZeroDivisionError`, `RecursionError`,
  `OverflowError`, now returns `origin/main`'s own text immediately.
  DELIBERATE NARROWING, pinned in tests: `motzkin(2+3)` — tiny,
  in-range, previously part of group B's own "evaluates instead of a
  spurious refusal" fix — is now itself refused as validation, since
  `motzkin`'s own ValueError is textually indistinguishable from a
  genuine domain violation without retrying (which risks re-running
  whatever made it fail in the first place). `nextprime(10,2+1)` (still
  evaluates to `17`), `prime(1-1)`, and `prime(1/0)` (both still surface
  `origin/main`'s own text) are re-pinned under the narrowed rule.

  Every `_GROWTH_BOUNDS` formula now has a property-style test: for `n`
  across a sample grid over its own capped domain (including
  `MAX_ITH_PRIME_SKIP` for `nextprime`), the bound must be `>=` the REAL
  value SymPy computes — this is what caught items A, B, and D's
  formula bugs during this round's own development, not a hand-picked
  repro list.

  **Two text defects in the above, fixed in the same round**: (1) the
  `<<` shift-digit-ceiling message read "'<<' shift result the result
  would have about N digits in its numerator, ..." — a doubled "result"
  and a numerator/denominator distinction that makes no sense for a
  plain integer shift (never a `Rational`); the shared digit-ceiling
  wording is now built in one place (`_digit_ceiling_text`, extracted
  from `_ceiling_message_num_den`) and reads "the result of '<<' would
  have about N digits, over the limit of 4000: ...". (2) a bare class
  reference (`safe_parse("Pow")`) returned a `ceiling` code with a
  message leaking the Python repr (`<class 'sympy.core.power.Pow'>`) —
  a malformed expression is a `validation` finding, not an oversized
  one, so the check moved out of `reject_explosive` (whose own contract
  is "a message string is always a ceiling finding") into its own
  `_bare_class_violation`, called separately by `safe_parse` and
  reported as `validation`, naming the class as written ("'Pow' is a
  bare reference to a SymPy class, not a value — call it or use a
  number/symbol").

  **A structural fix, replacing the token-level `<<`/`>>`/`%`/`//` screen
  and its TypeError carve-out entirely** (coordinator review of
  949aac9, grok `verify-1095-r4-grok.log`): the root problem recurring
  across rounds 3 and 4 was a deferred-parse `TypeError` on `standin <<
  x` carved out as "safe to fall through", and a token screen that could
  only ever recognise a bare `NAME(NUMBER)` sitting directly next to the
  operator — `1 << (factorial(20))`, `1 << factorial(12+1)`, `1 <<
  binomial(40, 20)`, `1 << rf(30, 20)`, `(bell(1463)) << 100000` all
  reached real, unbounded construction regardless, since none of them is
  that one bare shape. Closed at the root: every deferred stand-in (and
  the marker nodes these methods themselves produce, so a CHAINED
  expression — `(1 << factorial(20)) << 3` — stays inert too) now
  implements `__lshift__`/`__rlshift__`/`__rshift__`/`__rrshift__`/
  `__mod__`/`__rmod__`/`__floordiv__`/`__rfloordiv__`
  (`_DeferredOperatorMixin`), returning an inert marker node
  (`_DeferredLShift`/`_DeferredRShift`/`_DeferredOpMod`/
  `_DeferredOpFloorDiv`) instead of ever raising — closing the
  reflected-operand direction (`3 % factorial(5)`) needed discovering
  that SymPy's OWN binary operators are not Python's plain
  `NotImplemented` protocol at all, but a PRIORITY-based dispatch
  (`@call_highest_priority`, deferring to the other operand's method
  only when its `_op_priority` is STRICTLY greater than the default
  10.0 every bare `Function` subclass inherits — the mixin sets 10.1).
  The deferred TREE now carries every one of these operators at
  whatever depth or shape the caller wrote it, so `_numeric_ceiling_
  scan`'s new `_deferred_binop_violation` bounds them regardless of
  token adjacency: `<<` via `digits(left) + count*log10(2)`, `count`
  the RIGHT operand's own VALUE resolved through `_GROWTH_BOUNDS` (item
  2, below); `>>`/`%`/`//` via the LEFT operand's own bound alone (the
  result never exceeds it). The deferred-parse-exception TypeError
  carve-out is now GONE outright — an expression touching a table name
  or an unprotected operator that still makes the deferred parse raise
  is unconditionally a refusal; a genuine arity `TypeError` still
  reaches the caller with `origin/main`'s own text unchanged, since it
  no longer depends on that carve-out to do so.

  **Item 2 (grok): the shift/mod/floordiv bound must never use Stirling
  as a UNIVERSAL digit-count approximation.** `1 << prime(10)` was
  bounded as `1 << 10!` and refused, though `prime(10) == 29` and
  `origin/main` returns `536870912` cleanly — Stirling's `log10(n!)` is
  only ever a valid bound for a factorial-SCALE operand; `prime`/
  `totient`/`fibonacci`/`harmonic`/`primepi` all grow far slower.
  `_deferred_binop_violation` resolves each operand through `_GROWTH_
  BOUNDS` (via `_resolve_arg_magnitude`), the SAME "one spec drives
  everything" formula every other nested-argument bound in this module
  already uses — no second, parallel approximation. DELIBERATE
  NARROWING, pinned in tests: `bell`'s own entry (the shared,
  deliberately loose `_growth_nn_loose`) overestimates `bell(1463)` at
  ~9258 "digits" against its true 3018 — already over
  `MAX_NUMERIC_DIGITS` with no shift at all — so `bell(1463) << 1` is
  now refused, the same false-refusal trade this module's own
  established pattern already makes elsewhere (motzkin's own
  narrowing, rf/ff's own narrowing) rather than adding another
  per-name special case to a shared bound.

  **Item 3 (grok): `_growth_nextprime` was an average-gap ESTIMATE, not
  a proven bound.** `n + ith*(ln(n+ith+1) + 10)` gives `903.79` at
  `n=887, ith=1`, while `nextprime(887) == 907` — an actual, unsafe
  under-estimate on a real, unremarkable gap (20), not an edge case.
  Replaced with Bertrand's postulate compounded `ith` times IN LOG
  SPACE (`log10(n) + ith*log10(2)`, i.e. `n * 2**ith` — sound for every
  `n`/`ith`, no record-gap exception needed, though far looser past
  `ith=1` or `2`). Verified against an adversarial grid, not round
  numbers: 887, 1327 (gap 34), 31397 (gap 72), a ~1.7e15 prime with a
  1132 record gap, `10**25 - 1`, and `ith` in `{1, 2, 10, 1000}`.

  **Item 4 (grok): a table-function call as a `Pow`'s own BASE was not
  growth-resolved** — `bell(1463)**2` sailed past the deferred scan
  (its EXPONENT already resolved a nested table call via `_resolved_
  table_aware_magnitude`, renamed from `_resolved_exponent_magnitude`
  now that it serves both positions; its BASE never did) and paid
  `bell(1463)`'s own real ~5s construction cost before the output
  ceiling — the last-resort backstop, not a promptness one — ever
  caught it. Fixed with a NEW, separate check
  (`_pow_table_base_violation`) rather than routing the base through
  the SAME shared resolver the exponent uses: tried that first, and it
  regressed a genuinely-cancelling product — `factorial(1463)/
  factorial(1463)*factorial(1463)/factorial(1463)` (net exponent 0,
  exact value 1) was reported as an ~18,000-digit uncancelled
  denominator, because giving ONE `Pow` factor's base a growth-bound
  approximation (rather than leaving it "unresolved", as before) fed a
  resolved/unresolved MISMATCH straight into the surrounding `Mul`'s
  own non-cancelling fallback math. Landed alongside a SEPARATE,
  principled fix for that same cancellation gap: `_factor_multiset`
  now also treats a table-function call as a valid EXACT multiset base
  (keyed by the node itself — SymPy's own structural equality/hashing
  on a `Function` node's class-plus-args already guarantees two
  identical calls compare and hash equal), so `factorial(1463) /
  factorial(1463)` cancels EXACTLY via the SAME net-zero-exponent
  mechanism `Integer` bases already use, without needing to know
  `factorial(1463)`'s own numeric magnitude at all — but a table-
  function key that does NOT fully cancel is never trusted as "exact"
  (`_table_multiset_trusted`, checked at `_factor_multiset`'s own two
  top-level call sites, never inside that recursive function itself —
  tried inside first, and it discarded a lone survivor before its
  `Mul` parent ever got the chance to cancel it against a matching
  inverse elsewhere in the product, which is exactly why nothing
  cancelled under that version). Regression-tested: `x*factorial(1463)`
  (single, in-range, non-cancelling) and `factorial(1000)+
  fibonacci(1000)` (two independent siblings) both still evaluate.

  **Item 5: pinned against `origin/main`** — `factorial(10+1) << 1 ==
  79_833_600`, `1 << prime(10) == 536_870_912`, `1 << fibonacci(20)`,
  `totient(1463) << 10`, `7 // bell(20)` (== 0 — SymPy's own `Mod`/
  `floor` evaluation determines this from `bell`'s own registered
  assumptions, never by computing `bell(20)` for real), `factorial(20)
  % 7 == 0`; the five refusal shapes above refuse in milliseconds;
  `bell(1463)**2` refuses before construction, verified with a spy on
  the real `sympy.bell` (never called), not merely a timing
  measurement.

  **Interception moved to the AST stage, replacing the mixin dunders
  entirely** (coordinator review of 5e2a961, grok `verify-1095-r5-grok.
  log`): every one of the round-3-follow-up-#2 mixin's five findings
  traced to the same fact — interception happened AFTER Python had
  already dispatched `%`/`//`/`<<`/`>>`, so it depended on operand
  shape. `-bell(1463) % 7` (unary minus first — `%`'s own left operand
  is a `Mul`, not the stand-in), `(bell(1463)+1) % 7`, `2*bell(1463) %
  7` (same shape) all bypassed the mixin: SymPy's own `Expr.__mod__`/
  `Integer.__floordiv__` dispatch to a real, eager `Mod`/`floor` the
  instant BOTH operands already look like concrete `Expr`s, never
  asking the mixin at all. Fixed by owning the parse pipeline instead:
  `_parse_deferred` replicates `parse_expr(..., evaluate=False)`'s own
  three steps (`stringify_expr` -> a transformer -> `compile` ->
  `eval_expr`, all public names in `sympy.parsing.sympy_parser`,
  verified against the installed sympy 1.14.0's own source), substituting
  a `_deferred_transformer_class()`-built `EvaluateFalseTransformer`
  subclass whose `visit_BinOp` maps `ast.Mod`/`ast.FloorDiv`/
  `ast.LShift`/`ast.RShift` to CALLS of the marker constructors —
  crucially, VISITING THE CHILDREN of those nodes (SymPy's own version
  returns the node completely unvisited for these four, confirmed
  reading its source, which is why nothing nested inside them was ever
  `evaluate=False`-protected before this). Operand shape cannot matter
  anymore — the marker node exists the moment the AST is built, before
  any operator dispatch ever runs. The mixin dunders and the `_op_
  priority` trick are deleted outright (no longer needed once
  interception moved earlier in the pipeline).

  Three more issues folded into the same commit: (1) a purely NUMERIC
  shift count reaching a marker via a nested `Pow` (`1 << (2**200)`)
  used to reach real construction the instant both sides became
  concrete `Integer`s under the old design — now bounded the same way a
  table-call count already was, since `2**200` is ALSO a marker node's
  child now, fully `evaluate=False`-protected; (2) DELETED
  `_unprotected_operator_violation` and the "computed or above 200"
  narrowing outright — the AST-level fix needs no token-level pre-check
  at all, since it can see the whole tree, not just tokens near the
  operator: `x**(2+2) % 3`, `2**300 % 7`, `2**(10+10) % 7` all now
  return `origin/main`'s own values instead of a narrowed or
  accidental refusal; (3) a fully-cancelling product or difference of
  two IDENTICAL nested heavy calls (`factorial(factorial(8))/
  factorial(factorial(8))`, `factorial(factorial(8))-factorial(
  factorial(8))`, `nextprime(10**2000)/nextprime(10**2000)`,
  `divisors(factorial(100))/divisors(factorial(100))`) used to skip the
  Function-node check entirely — `_numeric_ceiling_scan`'s own stack-
  based descent stops once `_log10_num_den` reports a node fully
  resolved (correct, for an exact numeric verdict), but round-3-
  follow-up #2's own `_factor_multiset` extension means a cancelling
  PRODUCT of two table calls now also reports "resolved" with a
  trivial value, silently skipping whether the CHILDREN (`factorial(8)`
  -> `factorial(40320)`, unbounded) are themselves safe to construct.
  Fixed the same way `reject_explosive`'s own `Pow` loop already
  handles the identical class of bug for `2**1000000000 -
  2**1000000000`: `_function_arg_cap_violation` (extracted from
  `_numeric_ceiling_scan`'s own main loop) is now ALSO walked
  UNCONDITIONALLY, in the same pass as that `Pow` loop.

  Also fixed: a plain-callable stand-in's arity `TypeError` used to read
  `Function.__new__`'s own generic SymPy-style wording ("X takes
  exactly N arguments (M given)"), not `origin/main`'s own native
  Python wording for these REAL, plain-function callables ("X() takes N
  positional argument but M were given") — `_arity_checked_new` builds
  a genuine Python function with the identical parameter signature
  (names, defaults, `*args`) via `compile()` + `types.FunctionType`
  (never `exec()` — this module's own zero-`eval`/`exec` invariant is
  unconditional, no exemption for a template string built entirely from
  `inspect.signature`'s own structured data) and calls it FIRST, so
  Python's own call-binding machinery raises the byte-identical error a
  real call would, before `Function.__new__` is ever reached.

  Verified with a NEW differential test: for a ~45-expression corpus
  with no unmapped operator anywhere, `_parse_deferred` must produce
  the STRUCTURALLY IDENTICAL tree `parse_expr(..., evaluate=False)`
  does, proving the subclass changes behavior ONLY for the four
  operators it overrides — a future sympy bump that changes that shared
  pipeline is caught here instead of silently drifting.

  **Two more fixes, coordinator review of 06272aa** (both replayed 34
  probes clean first): (1) `_arity_checked_new`'s own `compile()` +
  `types.FunctionType` stub — built purely to reuse Python's own call-
  binding error text — is GONE: generating code objects at import time
  to mimic an error message is the wrong tool, one step from the
  `exec`/`eval` invariant this module exists to guard, even though it
  never ran anything dynamic. Replaced with `inspect.signature(real).
  bind(*args, **kwargs)` first; on a `TypeError` from `bind()`, calling
  `real(*args, **kwargs)` raises the SAME native `TypeError` a real call
  would, for free — CPython binds arguments to a callee's frame BEFORE
  executing any of its bytecode, so the real function's own body never
  runs regardless of what the arguments actually are; on a successful
  `bind()`, `real` is never touched at all. Proven with a spy, not just
  reading the code: the real callable's own body is confirmed NEVER
  entered on the wrong-arity path.

  (2) `-bell(1463) % 7`, `(bell(1463)+1) % 7`, `2*bell(1463) % 7` used
  to refuse with "the left operand of '%' cannot be safely bounded" — an
  UNKNOWN verdict, not an over-cap one — even though bare `bell(1463) %
  7` and bare `bell(1463)` both already evaluated (the documented ~5s
  at-cap cost) and `origin/main` evaluates all five shapes.
  `_resolve_arg_magnitude` now composes a `Mul`/`Add` WRAPPING a table
  call the same way `_log10_num_den` already composes one of ordinary
  numeric pieces: a `Mul`'s own magnitude is the SUM of its factors' own
  magnitudes (sign discarded, the same "abs, not signed" treatment a
  plain `-1` factor already gets); an `Add`'s own magnitude is its
  LARGEST term's own magnitude scaled up by `log10(number of terms)` —
  reusing `_log10_num_den`'s own established safe-upper-bound formula
  rather than a second, parallel one. Composing alone was not enough,
  though: `bell`'s own growth-bound entry was still the shared,
  DELIBERATELY loose `_growth_nn_loose` (`n**(2n)`), overestimating
  `bell(1463)` at ~9258 "digits" against its true 3_018 — already over
  `MAX_NUMERIC_DIGITS` with nothing else contributing at all. Replaced
  with `_growth_stirling_factorial` (`bell(n) <= n!` for every `n >= 0`
  — Bell numbers count PARTITIONS of an n-set, strictly fewer than the
  n! PERMUTATIONS once n >= 3, a provable fact) — Stirling's own
  `log10(n!)`, WITH Robbins' own correction term (`+ (1/(12n)) *
  log10(e)`; the bare approximation is only asymptotically an upper
  bound, and measurably under-shot the true value for small `n` when
  first written, caught by this file's own property-style check across
  its full domain, not by inspection), tight enough that `bell(1463) %
  7` and every wrapper around it now evaluate while `bell(1464)`-based
  shapes (one over the cap) still correctly refuse in milliseconds — the
  same Stirling formula now also used for `factorial`'s own growth-bound
  entry, tighter than the shared catch-all there too. A genuinely
  unresolvable (symbolic) table argument — `bell(x) % 7` — still matches
  whatever `origin/main` itself returns, unaffected either way.

  **One more fix, coordinator review of 5119c9d — two findings, one
  cause**: `_resolve_arg_magnitude` did not understand a deferred
  shift/mod marker node (`_DeferredLShift`/`_DeferredRShift`/
  `_DeferredMod`/`_DeferredFloorDiv`) at all, and `_function_arg_cap_
  violation`/`_table_function_bound` both treated an argument the
  resolver could not bound as a silent SKIP rather than a refusal. (1)
  A marker used as a TABLE-CALL argument failed OPEN: `bell(1 << 11)`,
  `factorial(1 << 20)`, `rf(5, 1 << 16)`, `factorial(1 << (2+9))`,
  `factorial(2*(1 << 10))` — the token screen sees two small literals,
  the deferred tree holds `_DeferredLShift(1, 11)` as the argument, the
  old resolver returned "unresolved", the per-position loop's `continue`
  read that as "nothing to check", and the real (`evaluate=True`) parse
  then ran `1 << 11` for real and constructed the actual heavy call. (2)
  Chained markers (`1 << 2 << 3`, `7 % 3 % 2`, `1 << (2 << 3)`,
  `(1 << 4) % 5`) failed CLOSED where `origin/main` evaluates them — the
  old `_deferred_binop_violation` hand-extracted its left/right operands
  rather than recursing back through the shared resolver, so a marker
  nested as another marker's own operand was never resolved.

  Fixed at the root, per the coordinator's own framing: ONE magnitude
  resolver for every consumer. `_resolve_arg_magnitude` gained a branch
  for marker nodes (delegating to a new `_resolve_marker_magnitude`,
  which implements the `<<`/`>>`/`%`/`//` bound rules — `<<`:
  `left_log + shift_count * log10(2)`; the other three: bounded by the
  left operand alone — recursing back through `_resolve_arg_magnitude`
  for both operands, so a nested marker resolves automatically, fixing
  the chained case "for free"), a branch for `floor`/`ceiling`/`Abs` of
  a resolvable argument (so `factorial(floor(2.5))`, `bell(ceiling(3.7))`,
  `factorial(Abs(-5))`, `rf(5, floor(3.9))` keep evaluating, matching
  main), and a branch for a `NumberSymbol` (`pi`, `E`, `EulerGamma`,
  `GoldenRatio`, ...; every one of SymPy's built-in irrational constants
  is O(1) in absolute value, so a small constant bound is always safe —
  needed because the REAL, eagerly-evaluated `digamma(1463)` reduces to
  `harmonic(1462) - EulerGamma`, and without this branch the new
  Mul/Add composition could not resolve the bare `EulerGamma` term,
  regressing a previously-pinned test). `_deferred_binop_violation`
  itself shrank to a thin caller of the shared resolver. Then the
  structural backstop: in `_function_arg_cap_violation` and its
  `_table_function_bound` twin, an argument with NO free symbols that
  the resolver still cannot bound is now a REFUSAL ("cannot be safely
  bounded", the unknown-not-over-cap message this module already uses
  elsewhere) rather than a skip — the one rule that would have caught
  the marker-as-argument bypass regardless of which node shape caused
  it, present or future. (This is also what makes `_table_function_
  bound`'s own use of `_safe_pow10(None)` an intentional refusal for a
  genuinely missing bound, rather than an accidental `inf` the position
  simply never got written to.) A non-table, non-collapsing `Function`
  this module cannot bound (`factorial(Ei(1463))`, the exponential
  integral) now refuses too — a deliberate divergence from `origin/main`
  (which leaves it symbolic, since `Ei(1463)` isn't provably a
  non-negative integer) for an opaque shape nothing here can prove safe,
  the same fail-closed stance already applied to a free symbol or an
  over-cap table call.

  **Round 9 (coordinator review of 1d756b7 — both cross-vendor reviewers
  FAILED it, Codex `verify-1095-r7.log` and grok `verify-1095-r7-grok.
  log`): a rule that is an upper bound on the happy path but not on the
  accepted domain**, closed position-complete and domain-explicit so
  every accepted shape now has a declared row.

  (A) `%`/`//` used the SAME "bounded by the left operand alone" rule —
  true only for `>>`. `%`'s bound is now the RIGHT operand's own
  magnitude (`0 <= |a % b| < |b|` unconditionally — `-1 % 10**4 ==
  9999`, nowhere near `|-1|`, the old bound). `//` needs a LOWER bound
  on its divisor (a smaller divisor gives a LARGER quotient — `1 //
  0.0005 == 2000`), known only for an exact numeric literal or a table
  call PROVEN to always return an integer (`_INTEGER_VALUED_TABLE_
  NAMES` — deliberately excludes `bernoulli`/`harmonic`, which can be
  genuinely fractional, e.g. `bernoulli(1) == -1/2`); anything else
  fails closed (`7 // bell(20)` still evaluates to main's `0`, since
  `bell` is integer-valued; `bell(-1 % 10**4)`, `bell(1 // 0.0005)`,
  `root(-1 % 10**2000, 2)`, `factorint(-1 % 10**30)`, `nextprime(-1 %
  10**30)` all now refuse in milliseconds instead of hanging or
  bypassing their own cap).

  (B) Eight table names' own OPTIONAL second position drives the
  RESULT's magnitude and cost and had no bound at all: `bell`'s
  `k_sym`, `bernoulli`/`euler`/`genocchi`'s `x` (the corresponding
  POLYNOMIAL evaluated at that point), `fibonacci`/`tribonacci`'s
  `sym`, `harmonic`'s `m`, `zeta`'s `a`. Each position is now capped at
  `MAX_HEAVY_ARG` (the same flat cap `divisor_sigma`'s own `k` already
  used), and each name's growth formula is two-position-aware (`_growth_
  poly_second_arg`, a shared wrapper: coefficient bound + `degree *
  log10(max(1, x))`), closing both a direct hang (`bell(1463,
  factorial(1463))`, `harmonic(1463, -factorial(8))`) and a COMBINED
  over-cap output where each position individually passes its own flat
  cap (`bell(1463, 1463)`, `bernoulli(1463, 1463)`, `zeta(-1463,
  1463)`) — the latter needed a NEW check, `_table_function_growth_
  violation`, walked unconditionally (same shape as `_function_arg_cap_
  violation`'s own walk) since a single-position name's safety used to
  be a byproduct of its ONE cap being calibrated to keep the output
  under `MAX_NUMERIC_DIGITS`, an assumption that stops holding once a
  SECOND position also drives the output.

  (C) A growth formula is only valid on the domain it was derived for.
  `binomial(n, k) <= 2**n` assumed `n >= 0`; a negative `n` switches
  `binomial.eval()` to a different, unaudited identity (`binomial(-100,
  1000)`'s true log10 magnitude is ~143, the old formula's own claim
  ~30; `binomial(-1463, 1000000)` passed screening and hung) — refused
  as a domain violation now (`origin/main` itself accepts a negative
  `n`; this is a deliberate, documented divergence for a domain this
  module has not derived a bound for). `polygamma`'s own growth formula
  used to ignore its ORDER entirely, reading only `z` (`polygamma(1463,
  1)`'s true log10 magnitude is ~3997 vs the old claim of ~1) — now
  `order! * zeta(order+1, z)`-shaped, valid because `z`'s own domain
  (`|z| >= 1`) is enforced separately (`|z| < 1`, near the Hurwitz
  zeta's pole, refuses).

  (D) `_log10_num_den`'s own `Float` print-profile shortcut (always
  magnitude 0 — correct for PRINTING a bare `Float`, since SymPy always
  renders one at a fixed ~15 significant digits regardless of true
  size) was inherited as a VALUE bound by the marker/`Mul`/`Add`/
  `floor`/`ceiling` resolver, silently reporting "magnitude ~1" for any
  `Float` literal regardless of its real size (`factorial(floor(1e4 *
  bell(1)))`, true value `factorial(10000)`, sailed past every scan and
  was caught only by the LAST-RESORT output-ceiling backstop, after
  real construction). `_resolve_arg_magnitude` now computes a `Float`'s
  real VALUE magnitude via SymPy's own arbitrary-precision `log`
  (`_float_value_log10`), never `float(node)` (which overflows past
  Python's own ~1.8e308 ceiling even though a SymPy `Float` has none).
  The resolver also gained `Pow` composition (base and exponent both
  recursed through the same resolver, so a marker or table call in
  EITHER position resolves) — `factorial((1 << 3)**2)` (64!),
  `factorial(2**(1 << 3))` (256!), `factorial(fibonacci(5)**2)` (25!),
  all plain integers on `origin/main`, no longer hit the item-3
  structural backstop.

  (E) Several names' growth bound was the deliberately loose `n**(2n)`
  catch-all, turning into absurd false ceilings once composed into a
  shift count (`1 << euler(4)` — main: `32` — used to claim ~19_729
  "digits"). Replaced with provable, tight bounds: `factorial2`/
  `subfactorial` `<= n!`; `euler`/`bernoulli`/`genocchi` `<= 2*n!`
  (`_growth_2_factorial`, verified against SymPy across `n` in `0..199`
  for all three); `motzkin <= 3**n`; `zeta(s) <= 2` for `s >= 2`, an
  `n!/(2*pi)**n`-scale Bernoulli-asymptotic bound for `s < 2` (tighter
  than the bare `2*n!` bound, needed to keep the existing pinned
  `zeta(-1463)` test — true magnitude ~2852 digits — safely under
  `MAX_NUMERIC_DIGITS`). `mobius`/`isprime` (always `{-1,0,1}`/boolean)
  and `root` (`<= its own radicand`) gained a `_GROWTH_BOUNDS` entry at
  all — `mobius(5) % 3`, `isprime(5) << 1`, `root(8,3) % 3` now evaluate
  to main's values instead of refusing with "no growth estimate is
  defined for it".

  (F) A small, curated elementary-function table (`Max`/`Min`/`sign`/
  `sin`/`cos`/`tanh`/`erf`/`log`/`exp`) lets a numeric argument wrapped
  in an ordinary, non-table SymPy function resolve through the shared
  resolver instead of hitting the item-3 structural backstop —
  `factorial(log(1))`, `factorial(cos(0))` (both `= 1` on `origin/
  main`) now evaluate. Deliberately NOT exhaustive: `factorial(Ei(
  1463))` (already pinned) and `factorial(I)` (the imaginary unit —
  newly pinned) stay refused, since neither is in the curated table.

  A `_table_function_growth_violation` false positive surfaced while
  fixing (B): the new unconditional walk read an EMPTY `bounds` dict
  (every bounded position skipped for being genuinely symbolic) as
  `math.inf` via `_safe_pow10(None)`, wrongly refusing `bell(x) % 7`
  and `factorial(cos(y)) - factorial(cos(y))` (both symbolic, both
  matching `origin/main` by staying unevaluated) — fixed by declining
  the check outright whenever the whole node still carries a free
  symbol, the same "genuinely symbolic never materializes" scope every
  other check in this module already gives one.

  **Round 10 (coordinator replay of 6e72d70: "all 40 reviewer repros
  hold, but two of my probes still do real work"), two more fixes.**

  Probe 1: `zeta(-1200, 2)` still took ~6.4s. A concrete INTEGER first
  argument `s < 2` with a SECOND argument supplied makes SymPy compute
  a Bernoulli polynomial of degree `|s| + 1` at `a` — a cost the SAME
  `s`, alone (no second argument), never pays (`zeta(-1200)` alone: a
  trivial zero, instant; the already-pinned `zeta(-1463)` alone: ~0.3s).
  New domain rule, scoped to the TWO-ARG form specifically so the
  existing single-arg pin is unaffected: a concrete integer `s < 2`
  with a second argument present refuses (`origin/main` itself stays
  symbolic for a non-integer `s` at any value, confirmed live — no
  hazard, not refused). Swept the other names for the same "negative/
  small first argument triggers a different, expensive code path"
  shape (`bernoulli`/`euler`/`genocchi`/`harmonic`/`polygamma` with a
  negative order, `binomial` with a negative `k`) — none reproduce;
  each stays symbolic, returns `nan`, or returns `0` instantly, on both
  this module and main.

  Probe 2: `floor(polygamma(1463, 1))` — `polygamma(1463, 1)` itself is
  ~13ms and a ~3299-digit exact expression well under the 4000-digit
  ceiling — took ~18-19s. Root cause, found via `cProfile`: SymPy's own
  `Function._eval_evalf` generic fallback looks up an mpmath routine by
  NAME (`self.func.__name__`), not by class identity — a deferred
  stand-in named `"polygamma"` (this module deliberately names every
  stand-in class after the real one it replaces) silently dispatches to
  the REAL `mpmath.psi` the instant anything calls `.evalf()` on it,
  bypassing the "inert, never computes" property this module's whole
  scan depends on — `floor.eval()`'s own `get_integer_part()` does
  exactly that to determine an integer boundary. Fixed at the root for
  EVERY stand-in this module builds (`_standin_refuses_evalf`, a shared
  `_eval_evalf` returning `None`), not gated per consumer. A second,
  independent layer for arguments that were never a stand-in to begin
  with (a bare `NumberSymbol` `Pow`, or the REAL, non-deferred
  `polygamma(1463, 1)` once the scan has already proven it safe):
  `floor`/`ceiling`/`frac`/`Max`/`Min`/`sign` now accept an argument
  only when it resolves to an exact `Integer`/`Rational`/`Float`
  literal (any magnitude — reading an integer part off an exact number
  is O(1)) or a nested call to a table function PROVEN to always return
  a plain integer (`_INTEGER_VALUED_TABLE_NAMES`, the same set `//`'s
  own divisor-lower-bound check already trusts); anything else refuses
  once its own resolved magnitude exceeds `MAX_SYMBOLIC_EXPONENT` (200)
  digits. `floor`/`ceiling`/`frac`/`Max`/`Min` also gained a deferred
  stand-in of their own (`Abs`/`sign` already stay `evaluate=False`-
  protected via SymPy's own parser whitelist and did not need one) —
  without it, `Max(factorial(20), 5)` (a completely benign call)
  regressed to a spurious parse error, since `Max.eval()` tried to
  numerically compare the now-inert `factorial` stand-in against `5`
  during the scan's own construction and failed outright; deferring
  `Max`/`Min` themselves closes that. Pinned: `floor(polygamma(1463,
  1))`, `ceiling(polygamma(1463, 1))`, `Max(polygamma(1463, 1), 1)`
  refuse in well under a second (measured, not merely asserted fast);
  `floor(pi*10**5)`, `floor(polygamma(3, 1))`, `Max(factorial(20), 5)`,
  `floor(2.5)` evaluate to main's exact values. `round` is not reachable
  through this module's own `safe_global_dict()` at all (checked, not
  assumed) — no coverage needed for it.

  **Round 11 (coordinator replay of 9fe4f6a: Codex `verify-1095-r9.log`,
  three probes reproduced despite a quota-limited run; grok
  `verify-1095-r9-grok.log` addendum, naming the shared root cause).**

  Codex item 1: `log` in this module's namespace is the NATURAL log
  (`ln`), not `log10` — the elementary `log` bound treated `arg_log`
  (already `log10(|x|)`) as if it were roughly `|ln(x)|` itself,
  under-estimating by a factor of `ln(10) ≈ 2.303`: `log(10**1000)`'s
  true magnitude is `log10(1000*ln(10)) ≈ 3.362` (`|ln(x)| ≈ 2302.6`),
  the old formula claimed `≈ 3.004` (`≈ 1009`) — comfortably under
  `bell`'s own `MAX_HEAVY_ARG` (1463) cap when the true value (2302) is
  well over it, so `bell(floor(log(10**1000)))` hung. Fixed (`log10(|ln(
  x)|) = log10(|arg_log|) + log10(ln(10))`, verified against SymPy
  across a grid of `log(10**k)`, `log(2)`, `log(Rational(1, 10**k))`);
  once fixed, `bell`'s own arg-cap check refuses PROMPTLY, the same as
  `factorial`'s twin case already did (slowly, via the last-resort
  output check) — no separate "combined-output check" fix was needed
  once the underlying magnitude was correct.

  Codex item 2: `N(x, n)`'s own PRECISION argument (position 1) was
  unbounded (`N(pi, 100000)` returns a 100_001-character `Float` in
  ~1.5s) — capped at `MAX_NUMERIC_DIGITS` via a dedicated stand-in and
  check (`N` needed its OWN deferred stand-in, being a plain eager
  callable like `factorint`/`primorial`; `_DEFERRED_STANDIN_NAMES` now
  folds in any name with an EXTRA bounded position, not just a
  position-0 one). Separately, the FINAL output check trusted `_log10_
  num_den`'s `Float` print-profile (always magnitude 0 — correct for a
  Float LITERAL, wrong for a Float's own rendered length, which tracks
  its PRECISION, not its magnitude) — now counts a `Float`'s own `_prec`
  (binary precision bits, `* log10(2)` for the decimal digit count)
  directly. `N(pi, 50)`/`N(pi)` still evaluate; `10.0**100000` (`_prec`
  stays the default 53 bits despite its huge magnitude) still evaluates.

  Codex item 3 / grok: `sin`/`cos`/`tanh`/`erf`'s `bounded1` rule
  (magnitude `<= 1`) holds only for a REAL argument — `sin(z)` for a
  pure-imaginary `z` grows like `sinh(|Im(z)|)`, exponentially
  (`factorial(ceiling(Abs(sin(5000*I))))`, `sinh(5000)` astronomically
  large). Now gated on `arg.is_real`; for a non-real argument with an
  exactly-resolvable modulus (`I * k` or `k * I`), bounded via `e**|k|`
  in log space; otherwise refused. `sin(500*I)` alone (unwrapped)
  evaluates as main does; `factorial(ceiling(Abs(sin(5))))` (real) still
  evaluates to `1`.

  grok's addendum named the shared root cause across its own findings
  and Codex item 3: an UNSIGNED magnitude fed to a formula that needs
  sign or domain information the resolver's own `bounds` dict (log10 of
  magnitude, sign discarded — a module-wide, deliberate convention) can
  never carry. `_growth_zeta` tested `_safe_pow10(bounds[0]) >= 2`,
  actually `|s| >= 2` — `zeta(-1463)` (a real, ~2852-digit value on
  main, pinned as evaluating at this module's own top-level cap) was
  silently bounded at `log10(2)` whenever NESTED (`factorial(floor(zeta(
  -1463)))`, `1 << floor(Abs(zeta(-1463)))`). Fixed by DROPPING the
  small-bound fast path from the magnitude-only formula entirely — it
  now always uses the conservative Bernoulli-envelope bound regardless
  of `s`'s true sign, sound (if looser) for `s >= 2` too; the TOP-LEVEL
  domain check (`_zeta_two_arg_domain_violation`, added an earlier
  round) already inspects the real, SIGNED `s` node directly and is
  unaffected, so `zeta(-1463)` alone still evaluates. `_growth_digamma`
  returned `1.0` for `z <= 1` while `psi(z) ~ -1/z` near the pole at
  `z = 0` (`digamma(1e-6)`'s true magnitude is `~6`, not `~0`); `gamma`/
  `loggamma` shared the same backwards assumption via the generic
  `n**(2n)` catch-all (`Gamma(z) ~ 1/z`, same pole). All three now
  reuse the `|z| >= 1` domain-row mechanism `polygamma`'s own `z`
  already had (added an earlier round), refusing below it rather than
  trusting a formula whose own derivation assumed the opposite regime;
  `gamma`/`loggamma` additionally got their OWN tight, domain-aware
  growth formula (`Gamma(z) <= ceil(z)!`, Stirling's envelope, verified
  sound across `z` from `1` to `1463` including the `[1, 2]` dip where
  `Gamma`'s own minimum sits — pulled out of the shared loose catch-all
  `andre` still uses). `polygamma`'s own Hurwitz-zeta identity
  additionally assumes a POSITIVE `z`, not merely `|z| >= 1`
  (`polygamma(2, -1.5)` stays symbolic on main despite `|z| = 1.5 >=
  1`) — `z` now also joins the non-negative domain table alongside
  `binomial`'s `n` and `harmonic`'s `m`. The property-style sweep
  (`_growth_check`, `tests/test_bug_sweep.py`) that already exists for
  every `_GROWTH_BOUNDS` entry gained support for a negative or
  fractional grid point (it silently assumed `n > 0` before, hiding
  exactly this class of bug from itself) and new grids for `zeta`
  (across both signs) and `gamma` (its own `z >= 1` domain, integers and
  rationals spanning the dip).

  **Round 12 (grok FAILED 4d94871, `verify-1095-r10-grok.log`): "every
  one of these is a magnitude-only bound fed to a sign/pole-dependent
  formula" — the round-10/11 structural mandate, now implemented in
  full.**

  New repros: `gamma(-1-10**-6)` (magnitude ~1.000001 >= 1, right next
  to the pole at `z = -1` — the round-11 `|z| >= 1` domain row only
  ever checked MAGNITUDE, never sign), `zeta(2, 1/1000)` (`a < 1` drops
  the `a**-s` term instead of refusing — its true value is `~10**6`,
  not `< zeta(2)`), `zeta(1 + 10**-6)` (the pole at `s = 1`, which the
  round-11 sign-safe envelope formula never excluded), `erf(7*I)`
  (`erfi(7) ~= 1.6*10**20`, vastly exceeding the round-11 `e**|k|`
  complex-trig bound's own `e**7 ~= 1096` — that bound is simply the
  WRONG growth rate for `erf`/`tanh`), `sin(I)` refused (a bare `I` is
  not a `Mul`, so the round-11 modulus check never even tried), and
  `gamma(1/2)`/`digamma(1/2)`/`loggamma(1/2)` refused at the TOP level
  vs `sqrt(pi)`/etc. on main (round 11's domain rows were too broad —
  they restricted position 0 there too, where no hazard exists).

  Structural fix, not patched name by name: `_resolve_exact_rational`
  (new) returns the EXACT `Rational` value of a subtree — an `Integer`/
  `Rational`/`Float` literal directly, or a `Mul`/`Add`/`Pow`(integer
  exponent)/marker of exactly-resolvable pieces, composed for real with
  a digit-count guard checked after every step — or `None` if the value
  is not exactly rational at all (a `NumberSymbol`, an irrational `Pow`,
  a transcendental table result, a bare `I`). `gamma`/`loggamma`/
  `digamma`/`polygamma`/`zeta`(both arities)/`sin`/`cos`/`tanh`/`erf`/
  `exp` — `_pole_sensitive_magnitude`, new — now REQUIRE this exact
  value for their own argument(s) whenever NESTED (inside another
  bounded position, or under `floor`/`ceiling`/`Abs`/`Max`/`Min`/a
  marker that itself feeds one), checking a real domain directly on it
  (`z >= 1` for the gamma family; exact integer `s <= 0` or exact
  `s >= 2` for single-arg zeta; exact integer `s >= 2` AND exact `a >=
  1` for two-arg zeta — `a >= 1` only ever SHRINKS the sum from its own
  `a = 1` value, so no separate `a` term is needed once both are
  in-domain, unlike round 11's own silently-clamped `max(1.0, a)`; an
  exact real rational for `sin`/`cos`/`tanh`/`erf` — `|value| <= 1`
  always, for ANY real argument, sidestepping `tan`'s own poles and
  `erfi`'s own `e**(x**2)` growth entirely rather than trying to bound
  either — and for `exp`, whose SIGNED exact value drives `e**x`
  directly, fixing `exp(-10)`'s own false refusal: the round-11 formula
  read `-10` back as magnitude `10`, i.e. as if it were `+10`) — never
  merely a magnitude. The round-11 `e**|k|` complex-trig bound is
  DELETED, not patched a second time.

  Top-level domain rows for `gamma`/`loggamma`/`digamma` removed
  entirely (`_MAGNITUDE_AT_LEAST_ONE_DOMAIN_POSITIONS` now empty) —
  confirmed live that `origin/main` never performs a genuinely unbounded
  computation for a bare call to any of these three at an arbitrary
  exact rational (always stays symbolic instantly, or computes a small
  closed form) — so `gamma(1/2)`, `digamma(1/2)`, `loggamma(1/2)`,
  `gamma(-1.5)` (away from any pole), and `zeta(1 + 10**-6)` (single-
  arg, already unrestricted) evaluate exactly as main does again.
  `digamma`'s own real-construction rewrite to `polygamma(0, z)` (a
  long-documented quirk) meant `polygamma`'s OWN top-level `z` row had
  to move too — but polygamma genuinely has a top-level hazard for a
  NONZERO order (`polygamma(1463, 1)` alone: ~3997 true digits), so its
  own walk now resolves `order` EXACTLY (not via magnitude — this
  module's own "exact zero maps to magnitude 0.0" print-profile
  convention, established for an unrelated purpose, made `order = 0`
  and `order = 1` indistinguishable through a magnitude alone) and
  skips the check entirely when `order` is confirmed exactly `0`
  (`digamma` in disguise), applying the exact-domain `z >= 1` gate only
  when it is genuinely nonzero.

  Pinned: all seven grok repros refuse in well under a second;
  `factorial(floor(gamma(5)))` = 24!, `factorial(ceiling(Abs(sin(5))))`
  = 1, `factorial(floor(exp(-10)))` = 1 (`exp(-10)` shrinks toward 0,
  no longer false-refused), `factorial(floor(zeta(2)*10))` = 16!
  (`zeta(2)` itself is irrational — not exactly rational — but the
  CONSUMER here is `floor`, whose own cheap-coercion gate only needs a
  small resolved magnitude, and `zeta(2)`'s own `s = 2 >= 2` domain
  still resolves that magnitude safely), `factorial(ceiling(Abs(sin(
  I))))` refuses (main: `2` — a deliberate, documented complex
  narrowing, the same "unknown != safe" bar already applied elsewhere).
  A focused property sweep (`tests/test_bug_sweep.py`) exercises
  negatives, `(0, 1)` rationals, half-integers, `s` near `1`, `a` in
  `(0, 1)`, and imaginary arguments as OUTSIDE-domain points (all
  refuse when nested) alongside a small inside-domain point per name
  (all evaluate).

- **An exception raised by THIS MODULE'S OWN post-parse scanning code
  could be reported to the caller as if it were their own malformed
  syntax — and the first fix attempted for it was itself a regression**
  (THE-1095, follow-up to GH #326): `factorial(floor(Abs(1/(1-1+10**(
  -6)))))` — a reciprocal of an exact-zero-plus-epsilon `Add` — surfaced
  as `('validation', 'parse error: list index out of range')`. The
  balanced-paren expression itself was never the cause (it resolves
  exactly, and correctly refuses on the pre-existing ~5.6M-digit
  ceiling backstop); the coordinator's OWN replay had an extra `)`, and
  `2+2)` (unbalanced parens) genuinely makes `origin/main` itself raise
  that identical `IndexError` — inherited from SymPy's own
  `evaluateFalse()` internals under this module's implicit-
  multiplication/`^`-as-power transforms, confirmed live against bare
  SymPy. An exception-TYPE allowlist applied at every raw `parse_expr`/
  `_parse_deferred` call site (`_classify_parse_exception`, this
  round's own first attempt) was accordingly the WRONG locus — it
  reclassified that legitimate, `main`-identical `IndexError` as a
  ceiling refusal instead of relaying it, a real regression — and was
  deleted. The correct locus is CALL SITE, not exception type: every
  exception `parse_expr`/`_parse_deferred` (SymPy's own tokenizer/
  parser/evaluator) raises is relayed verbatim, unconditionally,
  exactly as before this round ever touched the function; only an
  exception raised by THIS module's own code AFTER a parse has already
  succeeded — `reject_explosive` on either the deferred or the real
  shape (`_numeric_ceiling_scan`, `_resolve_arg_magnitude`/
  `_resolve_exact_rational`, the marker/binop checks) and the
  output-ceiling `_log10_num_den` call on the final evaluated value —
  now becomes the unknown-refusal ceiling (`_reject_explosive_safely`/
  `_INTERNAL_SCAN_FAILURE_MESSAGE`). `_resolve_exact_rational`'s own
  hardening (catching any exception its arithmetic composition raises
  and returning `None` — "not exactly resolvable" — for it) is kept
  regardless, the same "unknown != safe" contract every other branch
  there already holds. Pinned: five syntax-error probes (`2+2)`,
  `(2+2`, `2 +* 3`, `sin(`, and the original repro with a sixth closing
  paren) now match `main`'s own category AND text byte-for-byte; a
  fuzz-style sweep over reciprocals of exact-zero and near-zero
  subtrees (`1/(1-1)`, `1/(2-2+10**(-9))`, `x/(0+0)`, `1/(10**(-6))`,
  `1/(Rational(1,10**6))`) confirms each still returns either main's
  own outcome or a clean validation/ceiling refusal, never an
  internal-looking message; and `_numeric_ceiling_scan` monkeypatched
  to raise confirms the internal guard itself fires on an otherwise
  ordinary expression.

- **`polygamma`'s pole-sensitive domain check discarded the SIGN of a
  resolved order, silently treating a negative order as if it were the
  same positive magnitude** (THE-1095, follow-up to GH #326; grok
  review of fad081f): `_pole_sensitive_magnitude`'s `polygamma` branch
  fed `math.log10(abs(order_v))` into `order! * zeta(order+1, z)` —
  correct only for `order >= 1`, but SymPy 1.14 evaluates
  `polygamma(-1, z)` as `loggamma(z) - log(2*pi)/2` (`gamma(z)`-scale
  for a large `z`), so `factorial(floor(polygamma(-1, 700)))`,
  `bell(floor(Abs(polygamma(-1, 1463))))`, and `rf(5,
  floor(polygamma(-1, 700)))` all passed this screen with a false
  ~1-digit bound and then constructed a genuinely huge value for real.
  Fixed by checking the order's sign and integer-ness BEFORE ever
  computing a magnitude from it: a resolved order now needs to be
  exactly `0` (digamma) or a positive integer `>= 1` (with `z >= 1`) to
  be accepted at all — every other resolved order (negative,
  non-integer) refuses outright, as unknown, rather than being
  estimated via `abs()`. `_table_function_growth_violation`'s own
  TOP-LEVEL walk needed a narrower companion fix: `polygamma(-1, 5)`
  bare (unwrapped) is genuinely safe on `origin/main` and must still
  evaluate — a new `order_v == -1` branch there exempts a POSITIVE
  INTEGER `z` under the same cap `factorial`'s own position-0 argument
  already uses (`z <= MAX_HEAVY_ARG + 1`, since `polygamma(-1, z)`
  materializes an exact `factorial(z - 1)` for integer `z`) and
  otherwise falls through to the same refusal as the nested case.
  Pinned: the three repros refuse in well under a second; `polygamma(
  -1, 5)` at the top level matches main.

- **The round-13 parse-error-text pins hardcoded exception text that is
  PYTHON-VERSION dependent, failing CI on py3.11/macOS/Windows** (grok
  review of fad081f): `2+2)` raises `IndexError: list index out of
  range` on Python 3.14 (deep in SymPy's own `evaluateFalse()`/`ast`
  handling), but on 3.11 the same string never reaches that code at
  all — CPython's own `tokenize` module raises a `TokenError` for it
  first, caught earlier by `classify_unsafe`'s own guard instead, with
  its own (also Python-version-dependent) message text. A literal pin
  is only ever right for the interpreter it was captured on. Fixed by
  computing the expected `(category, message)` on the RUNNING
  interpreter inside the test, via the identical entry points
  `safe_parse` itself calls (`classify_unsafe`'s tokenizer guard first,
  then whichever of `_parse_deferred`/the real `parse_expr(...,
  evaluate=False)` `safe_parse`'s own control flow would actually
  reach and relay for that string) — the pin is "identical to what
  main says on THIS Python," never a captured literal.

- **`classify_unsafe`'s own tokenizer guard did not catch `SystemError`,
  so a bare CPython internal-implementation-detail message could escape
  it uncaught — breaking the ONE promise the atheris/ClusterFuzzLite
  harness holds `classify_unsafe`/`safe_parse` to** (THE-1095, follow-up
  to GH #326; found by this project's own 60s atheris pass): Python
  3.12's C tokenizer (`_generate_tokens_from_c_tokenizer`) raises
  `SystemError: <built-in method __new__ of type object at 0x...>
  returned a result with an exception set` — a raw memory address, no
  information about the input at all — for one narrow embedded-NUL-byte
  shape (`'   *AAA\n/\x00\x00'`); every OTHER NUL-byte shape, and this
  SAME shape on Python 3.14, already raised a clean, already-caught
  `TokenError: ('source code cannot contain null bytes', ...)`. Fixed
  by adding `SystemError` to both of this module's tokenizer-guard
  `except` tuples (`classify_unsafe`'s own, and `_expression_touches_
  table_or_unprotected_operator`'s identical one), and substituting
  `origin/main`'s own text for the underlying condition — never
  `str(SystemError(...))` verbatim — so every tokenizer failure, on
  every Python version, reports the identical `validation` "could not
  be tokenised" wording. Pinned the exact repro plus `'2+\x002'` (a NUL
  byte mid-expression) and `'\x00'` (a bare NUL byte alone); reran the
  60s atheris pass with the exact crashing input seeded into the corpus
  — clean, no crash.

- **The NUL-byte fix above still depended on the RUNNING interpreter's
  own tokenizer, so CI stayed red on the three py3.11 jobs** (THE-1095,
  follow-up to GH #326; grok review of 923f9f7): CPython 3.11's tokenizer
  (the old pure-Python implementation) does not check for a NUL byte —
  or any other non-whitespace C0 control character — AT ALL; the input
  tokenizes cleanly, falls all the way through `classify_unsafe`, and
  only fails LATER, at `compile()` (inside `_parse_deferred`/the real
  `parse_expr`), as a `SyntaxError` with YET ANOTHER wording
  (`'source code string cannot contain null bytes'`, note the extra
  "string") — caught by `safe_parse`'s own exception-relay sites
  instead, so a caller saw `'parse error: ...'` on 3.11 and
  `'expression could not be tokenised: ...'` on 3.12/3.14 for the
  IDENTICAL input. Root-caused, not patched with a third per-version
  branch: `_control_character_violation`, a new pre-screen `classify_
  unsafe` runs BEFORE any tokenizer or `compile()` call, character by
  character, rejecting `\x00` (and any other C0 control character —
  `\x01`-`\x1f` minus tab/LF/form-feed/CR, plus DEL `\x7f` — Python's
  own grammar does not allow as whitespace) with ONE deterministic
  message, synthesized once rather than relayed from whichever
  exception the running tokenizer happened to raise: `"source code
  cannot contain null bytes"` for `\x00` (picked as the canonical
  wording — `tokenize`'s own dedicated case on the newer C tokenizer,
  documented here as the chosen unified text) and `"invalid
  non-printable character U+XXXX"` for every other one (already
  version-INDEPENDENT text, confirmed live — CPython's own `compile()`
  uses the identical wording on every version tested). Confirmed live
  across Python 3.11/3.12/3.14: all three original NUL-byte repros, and
  a fourth (a non-NUL control character), now return byte-identical
  `(category, message)` tuples on every version. The tokenizer guards'
  own `SystemError` catch is narrowed to match (grok's own point): with
  the pre-screen in place, the ONE known NUL-byte `SystemError` shape
  can no longer reach it at all, so any `SystemError` that still does
  is characterized as this module's OWN contract violation — the
  internal-unknown ceiling refusal, never guessed to also be a
  NUL-byte shape and mislabeled `validation`.

- **The orthogonal-polynomial family — `hermite`/`hermite_prob`/
  `chebyshevt`/`chebyshevu`/`gegenbauer`/`legendre`/`assoc_legendre`/
  `jacobi`/`laguerre`/`assoc_laguerre` — could materialize a
  degree-thousands polynomial during the supposedly `evaluate=False`
  deferred parse, and a free-symbol result skipped the output digit
  ceiling entirely** (THE-1095, follow-up to GH #326; grok issue 2,
  review of 923f9f7): none of these ten names are in `EvaluateFalse
  Transformer.functions` (SymPy's own whitelist of names left
  genuinely unevaluated under `evaluate=False`), and every one of
  their `eval()` methods materializes the polynomial the instant the
  order (`n`, always position 0) is a concrete `Number` — `hermite(
  2000, x)`, a 14-character token string, built a degree-2000
  polynomial (coefficients up to `~2**2000`) during the FIRST parse,
  before this module's own scan ever had a tree to inspect, and the
  RESULT (a polynomial in `x`, carrying a free symbol) made `_log10_
  num_den`'s generic "digit count of the final value" backstop report
  `resolved=False` and skip the check entirely. Fixed by adding all
  ten names to the table this module already uses for every other
  eager-`eval()` callable: a per-family, MEASURED position-0 cap on
  the order (the largest `n` at which construction reaches ~1 second
  on this box, halved — see `MAX_HERMITE_ORDER`'s own comment for the
  full measurement table; caps range from 140 (`laguerre`/`assoc_
  laguerre`, markedly the most expensive per step) to 900 (
  `chebyshevu`) — one shared cap would have been dangerously loose for
  the slow half or needlessly tight for the fast half), which also
  gets every one of them the SAME deferred stand-in treatment (an
  inert `Function` subclass, no `eval()`) the rest of this module's
  table already gets. The output ceiling now applies to a polynomial
  RESULT too: each name's own `_GROWTH_BOUNDS` entry reuses the bare
  `n!` envelope (`_growth_stirling_factorial`, already used for
  `factorial`/`factorial2`/`subfactorial`) as a safe, generous upper
  bound on the largest coefficient's own magnitude — every family's
  true leading-coefficient growth (`O(2**n)`-to-`O(4**n)`-ish) is far
  smaller than `n!` for any `n` these caps admit, so this never
  false-refuses anything the argument cap alone would already accept,
  while closing the NESTED case (`chebyshevt(hermite(600, y), x)`,
  say) an argument cap alone cannot see. Pinned: `hermite(50, x)` and
  `legendre(5, 1/2)` match main exactly; `hermite(2000, x)` and
  `chebyshevt(2000, x)` refuse in well under a second.

- **Top-level `polygamma(-1, z)`'s own exemption was narrower than
  `origin/main`, refusing two shapes `origin/main` evaluates for free —
  and any OTHER negative order was refused unconditionally, when
  `origin/main` never evaluates one into anything at all** (THE-1095,
  follow-up to GH #326; grok issue 1, review of 923f9f7): the PREVIOUS
  fix only exempted order `-1` for a POSITIVE-INTEGER `z` (the
  `factorial(z - 1)`-materializing case) — but SymPy 1.14's `polygamma.
  eval` rewrites EVERY order `-1` call to `loggamma(z) - log(2*pi)/2`
  unconditionally, and for any NON-integer `z` (`1/2`, `700.5`, ...)
  that stays a compact `Float`/symbolic radical REGARDLESS of `z`'s own
  magnitude — no hazard, confirmed live — so `polygamma(-1, 1/2)` and
  `polygamma(-1, 700.5)` were wrongly refused, unpinned. And ANY order
  `<= -2` (`-2`, `-3`, `-5`, ...) has NO rewrite rule at all in SymPy
  1.14 — confirmed live across a range of orders and `z` magnitudes —
  it stays symbolic/unevaluated, unconditionally, instantly, so
  `polygamma(-2, 5)` was ALSO wrongly refused. Fixed with the precise
  top-level rule `origin/main`'s own behavior actually has: order `-1`
  with a non-integer (or unresolved) `z` is safe; order `-1` with a
  positive-integer `z` still needs the factorial-style cap; any other
  negative order is unconditionally safe at top level. The NESTED case
  (any negative order, any `z`) is unaffected — `_pole_sensitive_
  magnitude`'s own contract answers a different question ("give a safe
  bound for an arbitrary NESTED argument," never derived for a
  negative order at all) and still refuses exactly as before. Pinned
  all three repros matching main.

- **`andre`'s growth bound (`n**(2n)`) was too loose to admit the
  module's own documented at-cap value, refusing `andre(1463)` even
  though this module's own table already documents it at 3711 true
  digits — comfortably under the 4000 cap** (THE-1095, follow-up to GH
  #326; grok issue 3, review of 923f9f7): `_table_function_growth_
  violation` applies `_GROWTH_BOUNDS["andre"]` at the TOP level, not
  only when nested, and the shared `n**(2n)` catch-all claims `andre(
  1463)`'s own output would need `2 * 1463 * log10(1463) ~= 9261`
  digits — an order of magnitude over the true value, and over the
  cap, so a value this module's own comment already documented as safe
  was refused. Andre numbers are `|E_n|` (the Euler zigzag numbers) on
  even indices — the SAME `2 * n!` envelope `euler`/`bernoulli`/
  `genocchi` already share (`andre` has no optional second
  (polynomial) argument, `nargs == {1}`, so the bare formula applies
  directly, not the `euler`-style wrapped one) — replaces the loose
  catch-all, the same "a tight bound exists, stop using the loose one"
  fix already applied to `bell`/`factorial2`/`motzkin`/`subfactorial`.
  Confirmed live: `2 * 1463!` is 3998 digits (under cap, `andre(1463)`
  now evaluates); `2 * 1464!` is 4001 digits (over cap, `andre(1464)`
  still correctly refuses). Pinned both.

- **The orthogonal-polynomial family's own new cap (above) bounded only
  the ORDER position — `_GROWTH_BOUNDS` for all ten was bare `n!`, a
  bound on the STANDARD polynomial's own leading coefficient, never on
  `|P_n(x)|` for a numeric `x` nor on a coefficient inflated by an extra
  numeric parameter, and `_table_function_growth_violation` returned
  immediately whenever the node carried ANY free symbol — so the
  coefficient bound never ran on `hermite(n, x)` at all, the ordinary,
  intended usage** (THE-1095, follow-up to GH #326; grok review of
  562a026, `verify-1095-r13-grok.log`): `hermite(700, 10**50)` (`n!`
  alone claims ~1747 digits; `(2x)**n` — the true dominant term — is
  ~35,000); `gegenbauer(850, 10**20, x)` (`alpha` enters the recurrence
  directly, `~alpha**n`-scale coefficients, ~8500 digits, `x` free so
  the growth check never even ran) all passed every existing screen.
  Fixed with a real, position-complete growth formula
  (`_growth_orthogonal_poly`): `n!` (the coefficient-count envelope) +
  `n * log10(|alpha/a/b| + n)` for each extra numeric parameter
  (`gegenbauer`'s `a`, `assoc_legendre`'s `m`, `jacobi`'s `a`/`b`,
  `assoc_laguerre`'s `alpha`) + `n * log10(2|x| + 1)` for a NUMERIC `x`
  (the coordinator's own supplied envelope, `|P_n(x)| <= n! * (2|x| +
  1)**n`) — each term added only when that position resolved to a
  concrete magnitude, so a symbolic `x` (the ordinary case) still gets
  the coefficient-only bound instead of being skipped. That required
  fixing `_table_function_growth_violation`'s own free-symbol check too:
  narrowed from "the WHOLE node carries a free symbol anywhere" to "the
  ORDER position specifically" — every other position's own magnitude
  is now used when resolved, matching the per-position pattern `_table_
  function_bound` (called right after it) already used internally; a
  symbolic order (the one case where every growth formula in this table
  genuinely needs a value) still declines exactly as before. `x`/
  `alpha`/`a`/`b`/`m` also gained their own per-position cap
  (`MAX_HEAVY_ARG`, catching an OBVIOUSLY oversized single value
  immediately and cheaply; the growth formula is what catches an
  individually-under-cap COMBINATION that is still too large together —
  the same two-layer shape `bell`/`zeta`/`harmonic` already use).
  Pinned all six of grok's own repros refusing in well under a second,
  and `hermite(50, x)`/`legendre(5, 1/2)`/`chebyshevt(10, 3)`/
  `gegenbauer(5, 2, x)`/`jacobi(4, 1, 2, x)`/`assoc_laguerre(3, 1, x)`
  all matching main exactly.

- **`safe_global_dict()` re-exports every public SymPy name — 929,
  874 callable — and only the ones this module had explicitly tabled
  ever got a deferred stand-in; every OTHER callable ran EAGERLY, for
  real, the instant the deferred (supposedly `evaluate=False`) parse
  merely SCANNED a call to it, because SymPy's own AST transform
  (`EvaluateFalseTransformer.visit_Call`) appends an `evaluate=False`
  keyword ONLY for its own 34-name elementary list** (THE-1095, follow-
  up to GH #326; Codex issue A, "THE ROOT CAUSE", `verify-1095-r13.log`):
  measured live — `interpolating_poly(100, x)` (5.0s, accepted);
  `multinomial_coefficients(5, 100)` (timeout, 383MB); `ones(5000,
  5000)`/`randMatrix(5000, 5000)`/`N(ones(5000,5000), 20)` (timeout);
  `divisor_count`/`primenu`/`reduced_totient` on an RSA-scale literal
  (timeout); `expand((x+1)**10000)`/`series(exp(x), x, 0, 100000)`/
  `chebyshevt_poly(10000, x)`/`swinnerton_dyer_poly(6, x)` (timeout) —
  none of these names had ever been tabled or reasoned about by this
  module at all. Fixed as a DEFAULT-DENY posture for this whole surface:
  every callable NOT already bounded, and NOT one of the 34 elementary
  names (genuinely protected by the AST transform itself), now gets a
  GENERIC inert stand-in too, refused outright by a new scan-time check
  (`_unbounded_generic_call_violation`) the instant it carries so much
  as ONE non-symbolic argument — `unknown != safe` applied to an
  unrecognized CALLABLE the same way it already applies to an
  unrecognized VALUE shape everywhere else in this module. A call whose
  arguments are ALL symbolic stays unaffected (`expand(x+1)`/
  `simplify(sin(x)**2+cos(x)**2)` keep working). `_MEASURED_SAFE_
  CALLABLES` names the ONLY callables exempted from the generic stand-in
  — restricted, after a same-round self-review caught a real regression
  (see below), to TRUE, argument-content-independent constructors and
  predicates (`Add`/`Mul`/`Pow`/`Number`/`Rational`/`Integer`/`Float`/
  `Symbol`/`symbols`/`var`/`Eq`/`Ne`/`Lt`/`Le`/`Gt`/`Ge`/`And`/`Or`/
  `Not`/`Xor`/`Implies`), each measured directly at well under 100ms
  regardless of a numeric argument's own magnitude. A general RESULT-
  SIZE ceiling was added too, independent of the scan-time check: a
  `Matrix`'s own `.shape`, a container's own `len()`, or a symbolic
  expression's own `count_ops` (never rendering the result to check it —
  that could itself be the dangerous operation) is refused past a
  generous but finite limit, so a container or polynomial can never be
  returned past this module's own size ceiling even if some other bound
  turns out to be wrong.

  Two more fixes surfaced building this: `series(expr, x, x0, n, dir)`'s
  own `n` (the order — a plain callable, correctly needing SOME cap, but
  the blanket generic refusal was too broad, wrongly refusing
  `series(sin(x), x, 0, 6)` too) got its own dedicated position-3 cap
  (100, measured — `series(exp(x), x, 0, n)` reaches ~1s around
  `n=150-200`), the same `N`-shaped "no meaningful position-0 cap, no
  growth formula" treatment `_n_precision_violation` already uses.
  `RisingFactorial`/`FallingFactorial` — the REAL class names `rf`/`ff`
  are aliases FOR (confirmed live, `type(node).__name__` for a parsed
  `rf(5, 3)` call is `RisingFactorial`, never `"rf"`) — needed their own
  entries in every table `rf`/`ff` already had one in: `jacobi(21, 1, 1,
  x)`'s own `a == b` branch constructs a `RisingFactorial` node directly
  during evaluation, and the new generic check refused it as an
  "unbounded callable" before this fix, a false regression for an
  entirely ordinary, comfortably in-cap call.

  A same-round self-review (before this round's own commit) also caught
  the first version of `_MEASURED_SAFE_CALLABLES` reopening exactly the
  hole it was meant to help close: `expand`/`factor`/`together`/
  `cancel`/`collect`/`diff`/`degree`/`primitive`/`fraction`/`prod` (and
  `gcd`/`lcm`/`igcd`/`ilcm`, `erf`/`erfi`/`Ei`) were exempted from the
  stand-in on the strength of a SIMPLE test argument's own speed — but
  these are ALGORITHMS whose cost scales with their argument's
  STRUCTURE, not merely its magnitude, and `expand((x+1)**10000)` (a
  free-symbol argument, so the new generic refusal's own numeric-
  argument trigger never applied to it either way) still hung, because
  exempting `expand` from the stand-in ALSO exempted it from ever being
  made inert during the scan. Removed; they get the generic stand-in
  like everything else not in the (now much narrower) allowlist, which
  is what lets the EXISTING, unconditional `MAX_SYMBOLIC_EXPONENT`
  `Pow`-loop reach and refuse the same `Pow(Add(x,1),10000)` node —
  nested inside an inert stand-in instead of a real, eagerly-executing
  call.

- **`binomial`'s own second argument (`k`) is deliberately unbounded —
  `binomial.eval()` resolves `k > n` to `0` in O(1) — but that
  short-circuit is an INTEGER-`n`-only code path, and the exemption
  applied unconditionally regardless of `n`'s own type** (THE-1095,
  follow-up to GH #326; Codex issue B): `binomial(1/2, 1000000)` hangs;
  `binomial(1/2, 10000)` builds a 6,013-digit numerator before any
  backstop gets a chance to refuse it. Fixed: `k` is capped
  (`MAX_HEAVY_ARG`, confirmed live — `binomial(1/2, 1463)`/
  `binomial(-3/2, 1463)` both stay comfortably under `MAX_NUMERIC_
  DIGITS`, ~15ms) whenever `n` does not resolve to a confirmed non-
  negative integer; the existing unconditional exemption is otherwise
  unaffected (`binomial(5, 10**9)` still the instant `k > n` `0`).

- **"Position-complete" used to mean "every position `_bounded_
  positions` iterates has a row" — a position OUTSIDE that iteration (no
  cap, no `_UNBOUNDED_POSITIONS` entry either) was silently
  indistinguishable from an audited-safe one** (THE-1095, follow-up to
  GH #326; Codex issue C): `primorial`/`npartitions`/`factorint`/
  `primefactors`/`divisors`/`root`/`sqrt`/`cbrt`'s own remaining
  positions (every flag and optional parameter `inspect.signature`
  reveals, sympy 1.14) had never been individually audited. Each
  measured directly against an adversarial value — none is a numeric-
  magnitude hazard (`primorial(100, 10**9)`, `factorint(1234567891,
  10**9)`, `root(8, 3, 10**9)` all instant) — and recorded explicitly in
  `_UNBOUNDED_POSITIONS`, closing the class rather than the eight named
  instances: `tests/test_bug_sweep.py` now audits EVERY position of
  EVERY `_FUNCTION_ARG_CAPS` name against its own real arity and fails
  if any lacks either a cap row or an explicit unbounded entry.

- **Two main-parity mismatches from Codex's own 738-expression corpus**
  (THE-1095, follow-up to GH #326; Codex issue D): the pole at `s == 1`
  is its own special case in SymPy's own `zeta.eval()` for either arity
  — `zeta(1)`/`zeta(1, a)` both return `zoo` INSTANTLY regardless of
  `a`'s own magnitude (confirmed live up to a 300-digit literal), never
  the Bernoulli-polynomial-scale construction this module's own domain
  checks otherwise guard — but both checks refused it anyway, treating
  the pole the same as a genuinely expensive small integer order. Fixed:
  `s == 1` is exempted, in both the two-argument domain check and the
  nested single/two-argument magnitude resolver, before the general
  refusal that would otherwise still catch it. `factorint(2+3)` ->
  `{5: 1}` was already correct (pinned as the intended, documented
  outcome, not a regression).

  Two further divergences Codex's own corpus surfaced are documented,
  not fixed, this round (see `tests/test_differential_corpus.py`'s own
  `_KNOWN_DIVERGENCES`): the eager-factor family (`divisors`/
  `factorint`/`primefactors`) nested under an operator this module
  cannot bound (`Abs(divisors(1))`) refuses with a `CATEGORY_CEILING`
  message where `origin/main` itself raises a plain `TypeError` (a
  `CATEGORY_VALIDATION`-shaped error) — a category mismatch predating
  this round, left for a future one; and `rf`/`ff` called by their alias
  spelling produce an arity-error TEXT using that alias
  (`"rf takes..."`) where `origin/main` always uses the underlying real
  class name (`"RisingFactorial takes..."`, since `rf is RisingFactorial`
  — confirmed live) regardless of which name was used to reach it — a
  narrow, cosmetic (text-only) gap, also left documented rather than
  guessed at under this round's own time budget.

- **A branch-vs-main differential corpus, committed as a test fixture**
  (Codex's own request): `tests/test_differential_corpus.py`, 544
  assertions across every table/elementary/measured-safe name at a
  systematic grid of small, in-cap argument shapes, each compared
  against a bare `parse_expr` (no screen) call — plus a refusal-side
  batch (over-cap shapes, never run against bare SymPy, which would
  itself be the dangerous operation) and the two documented divergences
  above, checked explicitly rather than silently skipped.

- **The round-13 hand-picked `_MEASURED_SAFE_CALLABLES` allowlist
  under-covered ordinary calculator input** (THE-1095, follow-up to GH
  #326; coordinator review of 7630e87): `gcd(12, 18)`, `lcm(4, 6)`,
  `limit(sin(x)/x, x, 0)`, and `nsimplify(0.5)` all refused as "not in
  the bounded function set" where `origin/main` evaluates them — "which
  names occurred to a human reviewer" was never a measurement. Replaced
  with `scripts/measure_safe_callables.py` (new, committed, regenerable):
  probes every candidate callable (every name in `safe_global_dict()`
  not already tabled, elementary, or AST-transform-reserved — see
  below) against a fixed grid of argument shapes — small values AND,
  found needed mid-round (see bug (3), below), a "heavy" variant at the
  actual enforced cap boundary — each ISOLATED
  in its own `ulimit -v`-capped subprocess whose CALL is bounded by an
  in-child 1 s timer (interpreter start-up and the sympy import are
  outside the timer — the first version used a 1 s process wall clock,
  which on a loaded box turned trivial calls into false TIMEOUT
  verdicts and made the re-measure test flaky; the script's new
  `--refresh-excluded` mode re-measures only the names a CRASH or a
  non-heavy TIMEOUT had excluded and merges them into the committed raw
  report, `scripts/_measured_safe_callables_raw.json`, which is
  gitignored: 1.2 MB of per-shape timings that only matter for
  auditing why a name did or did not make the list), and
  allowlists a name only when every shape it accepts finishes
  comfortably under 100ms with a result under this module's own digit
  ceiling. The generated result — `codecalc/_measured_safe_callables.json`,
  committed alongside the measurement date — is what `_MEASURED_SAFE_
  CALLABLES` now actually loads from (an unreadable/missing file fails
  CLOSED to an empty set, never a hand-picked fallback).

  A measured-safe name is no longer EXEMPT from the deferred-scan
  stand-in either (round 13's own design): the measurement only
  confirms SMALL representative arguments stay fast — a caller-supplied
  LARGE one (`randMatrix(5000, 5000)`, one of Codex's own named repros)
  was never probed at all, and exempting the name from the stand-in
  entirely would let it execute EAGERLY during the scan regardless.
  Every measured-safe name now gets the SAME inert stand-in every
  unbounded name does; a new `_measured_safe_argument_cap_violation`
  applies a GENERIC per-argument cap instead of an outright refusal — an
  INTEGER argument capped at `MAX_HEAVY_ARG`, a `Rational`/`Float`/
  symbolic one left alone (the coordinator's own spec) — so a small,
  measured call proceeds and a large, unmeasured one still refuses.

  FOUR critical bugs surfaced building this, all caught and fixed in
  the SAME round, before any commit: (1) `Add`/`Mul`/`Pow`/`Or`/`And`/
  `Not`/`Eq`/`Ne`/`Lt`/`Le`/`Gt`/`Ge` (what `EvaluateFalseTransformer`'s
  own AST transform constructs INTERNALLY for EVERY `+`/`*`/`**`/
  comparison in ANY parsed expression) and `Integer`/`Float`/`Symbol`
  (what `auto_number`/`auto_symbol` — both always applied — construct
  for EVERY plain numeric literal or undefined name) are not ordinary
  "a caller chose to call this NAME" callables at all; giving ANY of
  them a stand-in (as an early draft of the fix above briefly did)
  corrupted the parse tree for every expression using the corresponding
  token — `2+2` itself started failing with "an argument to Add()
  cannot be safely bounded." These twelve names are now UNCONDITIONALLY
  excluded from ever getting a stand-in OR being a measurement
  candidate, regardless of anything else. (2) `_measured_safe_
  argument_cap_violation` itself initially missed the `isinstance(node,
  Function)` gate `_unbounded_generic_call_violation` already has, so
  `type(node).__name__ in _MEASURED_SAFE_CALLABLES` matched a GENUINE
  `Add`/`Mul`/`Pow` expression node too (not merely a call named that)
  — `2+2` again, refused as "an argument to Add() cannot be safely
  bounded" for the identical underlying reason. Fixed with the same
  gate every other generic check in this module already uses.

  (3) **Found AFTER the allowlist first measured clean**, spot-checking
  the coordinator's own named repro class against the fresh list:
  `_measured_safe_argument_cap_violation`'s cap applies PER ARGUMENT,
  independently, so `ones(1463, 1463)` is exactly as permitted as
  `ones(5, 3)` — each argument is individually AT, not over,
  `MAX_HEAVY_ARG`. The FIRST version of `scripts/measure_safe_
  callables.py` only ever probed a small, friendly value (`5`, `3`,
  `2`) — it answers "is this callable fast at a TRIVIAL input", never
  the question the cap's own safety actually depends on: "is this
  callable still fast at the LARGEST input the cap will ever let
  through." `sympy.ones(1463, 1463)` (a 1463x1463 = ~2.14M-element
  matrix of `Integer(1)`) does not return within 3 seconds — the
  small-shapes-only measurement allowlisted `ones`/`zeros`/
  `randMatrix`/`eye` anyway (their tiny `(5, 3)` shape finishes in
  under a millisecond), and `safe_parse('ones(1463, 1463)')` hung past
  15s as a direct result — reopening the EXACT class of hang
  (`ones(5000,5000)`, one of Codex's own named round-13 repros) this
  whole mechanism exists to close, just at a somewhat higher argument
  value. Fixed: `PROBE_SHAPES` now also probes a "_heavy" variant of
  every integer-carrying shape AT `MAX_HEAVY_ARG` itself (1463), not a
  small value — `ones`/`zeros`/`randMatrix`/`eye` now correctly drop
  off the allowlist (their heavy two-int shape times out past the
  probe's own 1s cap), staying default-deny, matching `evaluate_
  expression`'s own existing design (matrix work goes through the
  dedicated matrix tool, never a bare NAME call here) — `diag` stays
  allowlisted (its own cost is linear in the DIAGONAL length, not the
  product of two dimensions, and measures fast even at the heavy
  shape).

  (4) The heavy shapes' FIRST version reused the SAME value twice/
  thrice (`(1463, 1463)`, `(1463, 1463, 1463)`) rather than distinct
  ones — which, for any name whose real signature reads extra
  positional arguments as sympy "generators" (`factor`, `Poly`, ...),
  raises `GeneratorsError: duplicated generators` for two EQUAL
  arguments regardless of their magnitude, a shape-mismatch artifact
  with nothing to do with whether a large VALUE is itself slow.
  Confirmed live: `factor(1463, 1463, 1463)` raised that error while
  `factor(1463, 999, 500)` (identical magnitudes, distinct values) did
  not — the duplicate-value probe wrongly excluded `factor` from the
  allowlist over it, and `factor(30)` (ordinary calculator input,
  trivially safe on `origin/main`) started refusing outright as "not
  in the bounded function set," the exact under-coverage class this
  whole round exists to close. Fixed: the heavy multi-argument shapes
  now use DISTINCT values near `MAX_HEAVY_ARG` (`1463`, `1462`, `1461`)
  instead of one value repeated, and the probe child's own "this shape
  does not apply" exception set now also catches sympy's own
  `BasePolynomialError` (the `GeneratorsError` base), the same
  defensive broadening `gcd(5, 3, 2)`'s own `AttributeError` already
  needed for an unrelated reason.

  The measurement script's OWN output is now split in two, found
  needing a fix for the same reason: the FIRST version wrote one 1.3MB
  committed file — the ~10KB of names `_MEASURED_SAFE_CALLABLES`
  actually loads at runtime, packaged with a ~1.3MB per-candidate,
  per-shape raw timing log next to it, useless at runtime and wasteful
  to ship. `measure_all()` now returns `(data, raw_results)`
  separately: `codecalc/_measured_safe_callables.json` (committed, the
  ONLY file the runtime loads or a built package needs — names, the
  measurement date, the two thresholds, no per-shape timing) and
  `scripts/_measured_safe_callables_raw.json` — regenerable via
  `--write` for audit (WHY a name was or wasn't allowlisted, without
  re-running by hand), kept OUTSIDE `codecalc/` so it never ships in a
  package, and (round 15: gitignored, not committed — the same
  reasoning that keeps it out of the shipped package applies to the
  repository itself; regenerate on demand rather than diffing a
  ~1.3-1.7MB file on every measurement run).

  `scripts/measure_safe_callables.py`'s own coordinator-supplied list
  of names expected to come out allowlisted, checked against the FINAL
  (post-fix) measurement: `gcd`/`lcm`/`igcd`/`ilcm`, `limit`,
  `nsimplify`, `Rational`, `diff`, `integrate`/`solve`/`simplify`/
  `expand`/`factor`/`together`/`apart`/`cancel` (symbolic usage; a
  numeric-arg call to any of these still gets the generic cap, not a
  blanket exemption — only symbolic usage is unconditionally safe for
  an algorithm whose own cost scales with argument STRUCTURE) all
  measure ALLOWLISTED, confirming the coordinator's own expectation.
  `Abs`/`sign`/`Max`/`Min`/`floor`/`ceiling`/`re`/`im`/`conjugate`/
  `sqrt`/`cbrt`/`root`/`series` are excluded from MEASUREMENT entirely
  — already handled elsewhere (the 34-name `EvaluateFalseTransformer`
  elementary list, `_FUNCTION_ARG_CAPS`, or the hardcoded `_recognized_
  function_names()` set — never candidates in the first place, not
  rejected by a probe). `round` is not even a name `safe_global_dict()`
  exposes at all (Python's builtin `round` is not injected into this
  module's parser namespace) — never a candidate for an entirely
  different reason than the twelve above. `summation`/`Sum`/`Product`
  are MEASURED and stay OFF the allowlist: their real signature
  (`Sum(expr, (var, lo, hi))`, a tuple second argument) does not match
  ANY shape in this script's own plain-positional grid — every probe
  shape returns `UNSUPPORTED` — so they were never confirmed safe at
  ANY shape at all, correctly `unknown != safe`, not "measured slow."
  `ones`/`zeros`/`randMatrix`/`eye` (fast on a small shape, hang on the
  cap-boundary one — see bug (3), above) and the Matrix-CLASS family
  (`Matrix`/`BlockMatrix`/`ImmutableMatrix`/`MutableMatrix`/similar —
  no probe shape in this grid applies to their own constructor
  signature at all, `UNSUPPORTED` across the board) both correctly stay
  OFF the allowlist, for two DIFFERENT reasons.

  `tests/test_measured_safe_callables.py` (new) re-measures a random
  sample of 30 already-allowlisted names on the RUNNING box as part of
  the regular suite, catching the list going stale on a slower machine
  or after a SymPy version bump, rather than trusting a one-time
  measurement forever.

- **`rf`/`ff`'s own arity-error text used the CALLER's own alias
  spelling (`"rf takes..."`) where `origin/main` always uses the real
  class name (`"RisingFactorial takes..."`, since `rf is RisingFactorial`
  — confirmed live, the alias is the identical object)** (THE-1095,
  follow-up to GH #326; coordinator review of 7630e87, item 3, byte-
  identical to main as requested): fixed by reusing `_arity_checked_new`
  — the SAME mechanism that already gets a plain callable's own native
  arity error right — for the finite-`nargs` case too, rather than
  relying on `Function.__new__`'s own generic check (which reports the
  STAND-IN class's own name, the caller's spelling). Deliberately NOT a
  rename of the stand-in class itself: an earlier draft of this fix did
  rename it, which broke every one of THIS module's OWN ceiling/growth
  messages (they correctly keep the caller's own spelling — an existing
  pinned regression test in `tests/test_bug_sweep.py`, predating this
  round, caught the break immediately). `rf(5)`/`ff(5)` now raise
  byte-identical text to `RisingFactorial(5)`/`FallingFactorial(5)`,
  and this module's own ceiling messages for `rf(1463+1, 2)` still say
  `rf()`, not `RisingFactorial()`, exactly as before.

- **`divisors`/`factorint`/`primefactors` nested under `Abs`/an
  operator refused with a `CATEGORY_CEILING` message where `origin/
  main` raises a plain `TypeError`** (THE-1095, follow-up to GH #326;
  coordinator review of 7630e87, item 4): these three names' own real
  return type is a `list`/`dict`, never a number, so "what growth bound
  applies to this call's own magnitude" was never a missing ANSWER, it
  was not a QUESTION that applies at all — the old "no growth estimate
  is defined for it" ceiling refusal answered the wrong question.
  Fixed: `_table_function_bound` now returns "unresolved, no violation"
  (the SAME shape a genuinely symbolic argument already gets) for these
  three specifically, letting `_evalf_coercion_violation` (`Abs`/
  `floor`/`ceiling`) and `_deferred_binop_violation` (`%`/`//`/`<<`/
  `>>`) fall through to the real parse instead of refusing outright —
  their own ARGUMENT cap (`_function_arg_cap_violation`, checked
  unconditionally and independently) still applies first, so a
  genuinely oversized argument is still refused before real
  construction; only a safe, in-cap call reaches real evaluation, where
  SymPy's own native `TypeError` surfaces and `safe_parse`'s own
  exception-relay reports it as `CATEGORY_VALIDATION`, matching main.
  A related crash surfaced fixing this: `Abs(divisors(1))`'s own
  argument, once let through to the REAL-dict parse (these three are
  eager, plain callables that do not respect `evaluate=False` at all,
  and that parse deliberately uses the real dict), is a genuine Python
  `list`, not a sympy object — `.free_symbols` (a `Basic` method)
  raised `AttributeError` recursing into it, caught by the existing
  round-13 `_reject_explosive_safely` safety net but reported as an
  internal-unknown ceiling instead of letting the shape through; now
  caught locally and treated as "cannot determine, not a violation"
  instead, so the real parse's own native `TypeError` surfaces as
  intended. Both of round 13's own documented divergences (this one and
  the `rf`/`ff` one above) are now fixed and removed from `tests/
  test_differential_corpus.py`'s own `_KNOWN_DIVERGENCES`, replaced with
  pinned "now fixed" assertions.

- **`tests/test_differential_corpus.py` had no CI step at all, so CI
  never ran it** (THE-1095, follow-up to GH #326; coordinator review of
  7630e87, item 2): `.github/workflows/ci-python.yml` runs test files as
  explicit steps; a committed test file with no step for it is invisible
  to CI regardless of how many assertions it carries. Added, right next
  to `bug-sweep regressions` (the same matrix, the same shape) — and
  `tests/test_measured_safe_callables.py` (new this round) got its own
  step too, to avoid recreating the identical gap for a file that did
  not exist when this one was found. `actionlint` clean on the updated
  workflow; `scripts/check_claims.py` confirmed at the new 73-test-file
  count (was 72).

- **SCOPE (THE-1095, round 15, grok issue 2, `verify-1095-r14-grok.log`;
  also stated in `codecalc/safe_expr.py`'s own module docstring):**
  this ticket closes the class "a NUMERIC argument (its own VALUE
  magnitude, or its printed DIGIT count) drives unbounded construction"
  — `factorial(10**9)`, `ones(5000,5000)`, `bell(1463)+1`. It does NOT
  bound cost that lives in an expression's own SYMBOLIC STRUCTURE
  instead — `integrate(1/(x**7-x-1), x)`, `galois_group(x**8+x+1)`,
  `diophantine`/`satisfiable` on a large instance, `nroots`/`resultant`
  at degree 200 — every one of which has ALWAYS been allowed on
  `origin/main`, bounded only by the public tools' own 10-second
  execution guard, same as here. That class is THE-1097's own job (a
  resource-limited worker), a different mechanism; grok's own issue-2
  review table named several structural-cost examples of this kind and
  they are correctly out of scope for this ticket.

- **grok issue 1 (`verify-1095-r14-grok.log`): `_growth_2n` (`binomial(
  n,k) <= 2**n`) is sound ONLY for a confirmed non-negative INTEGER
  `n`, but round 13/14 accepted a non-integer/negative `n` too (`k`
  capped at `MAX_HEAVY_ARG` independently, via `_binomial_k_
  violation`) — a NESTED use of that exact shape
  (`factorial(binomial(1/2, 1463))`) let `_table_function_bound`'s own
  growth check see `_growth_2n`'s wrong, tiny estimate (`n=0.5`, ~0.15
  digits) instead of anything reflecting `k`, so the OUTER function's
  own cap check never had a chance to see the true magnitude before
  real construction.** Fixed: a new `_growth_binomial_general` (`|C(n,
  k)| <= (|n|+k)**k` in log space, valid for ANY real `n` and
  non-negative integer `k` — deliberately loose, no `/k!` term, so it
  stays correct without needing its own factorial sub-bound) is used
  instead of `_growth_2n` whenever `n` is not a confirmed non-negative
  integer (`_binomial_n_is_confirmed_nonneg_int`, a new shared helper —
  three call sites, `_binomial_k_violation`/`_table_function_bound`/
  `_divisor_lower_log10`, each used to re-derive this domain check
  independently); `binomial` also LEAVES `_INTEGER_VALUED_TABLE_NAMES`
  unconditionally now (`C(1/2, 1) = 1/2` is a plain fraction, not an
  integer, so the frozenset's own guarantee was never sound for that
  domain either) — `_divisor_lower_log10` special-cases it back in via
  the SAME new helper, for the confirmed-safe domain only.

  Pinned live: `factorial(binomial(1/2, 1463))`, `bell(binomial(1/2,
  1463))`, `1 << binomial(1/2, 1463)` now all refuse in under a
  millisecond. **Investigated and worth recording**: none of the three
  actually HANGS on `origin/main` either (confirmed against d713821's
  own pre-round-15 code too) — `factorial` of a genuine `Rational`
  stays symbolically unevaluated in ~0.2ms, `bell`/`<<` raise a native
  `ValueError`/`TypeError` in under a millisecond, and the true VALUE
  of `binomial(1/2, 1463)` is a small fraction (`~5.3e-6`), not the
  astronomically large NUMBER the old, unsound estimate implied either
  way — the "1754 digits" this module's own round-13/14 comments cited
  was the RENDERED fraction's own numerator+denominator digit count,
  never its magnitude. This module now refuses all three anyway,
  DELIBERATELY more conservative than main: `_growth_2n` was never a
  SOUND bound outside a confirmed non-negative-integer `n`, and this
  file's own "unknown != safe" bar means a formula that cannot be
  proven correct does not get to stay in service on the strength of
  one example not currently exploiting it. DELIBERATE NARROWING, pinned
  in tests: the bare, TOP-level `binomial(1/2, 1463)` (no longer
  nested) used to evaluate on the strength of the SAME unsound
  estimate happening to stay under the digit ceiling by accident — it
  now conservatively refuses too, since `_table_function_bound` is the
  SAME function used for a name's own self-output check and for a
  NESTED argument's contribution to an outer cap, and the now-sound
  bound is loose enough to cross the ceiling at this one k-at-the-cap
  shape; `binomial(1/2, 10)` (`origin/main`'s exact `-2431/262144`) and
  `factorial(binomial(6, 3))` (`= 20!`, the confirmed-safe-integer
  domain `_growth_2n` stays dedicated to) are UNAFFECTED, both still
  evaluate exactly as before.

- **grok issue 2a (`verify-1095-r14-grok.log`): `_measured_safe_
  argument_cap_violation`'s generic cap was value-kind on `Integer`
  arguments only — a `Rational`/`Float` argument (INCLUDING one whose
  own magnitude, not merely its digit count, is huge) was exempted
  entirely, on the strength of round 14's own probe grid measuring a
  SMALL `Rational(1,3)`/`Float(0.5)` fast, never a large one.** Fixed:
  the `MAX_HEAVY_ARG` magnitude cap now applies to every resolved,
  non-symbolic argument uniformly (Integer, Rational, or Float alike) —
  `_resolve_arg_magnitude` already reports `(log_num, log_den)`
  identically for all three, so there was never a technical reason for
  the exemption, only the coordinator's own original (narrower) round-
  14 spec text, which this finding supersedes.

- **grok issue 2b (`verify-1095-r14-grok.log`): the probe grid never
  tested a Rational with a large NUMERATOR, a Float with a large
  EXPONENT, a WIDE range between two arguments, or a shape with more
  than THREE positions — and a `ValueError`/`NotImplementedError` at a
  HEAVY shape was silently treated the same as one at a trivial shape
  (`UNSUPPORTED`, no signal either way), so a name's heavy-boundary
  safety could go completely unmeasured while it still made the
  allowlist on a small-shape success alone (`discrete_log` is the
  concrete case grok's own review found).** Fixed, in `scripts/
  measure_safe_callables.py`: four new probe shapes — `small_heavy`/
  `heavy_small` (`(2, 1463)`/`(1463, 2)`, a WIDE span rather than two
  values both near the cap), `float_heavy_exp` (`Float('1e300')`),
  `rational_heavy_numerator` (a 25-digit numerator), and
  `four_arg_heavy_last` (the `fps(sin(x), x, 0, 1463)` class — no
  shape before this round had more than three positions at all). The
  probe child now distinguishes a `TypeError` (`UNSUPPORTED_TYPE` — an
  arity/type mismatch, never a signal either way, at any shape) from a
  `ValueError`/`NotImplementedError`/similar domain exception
  (`UNSUPPORTED_VALUE`); `measure_name`'s own classification gained a
  third gate (on top of "every OK shape stayed fast" and "at least one
  shape applied at all"): at least one HEAVY shape must have come back
  `OK` or `UNSUPPORTED_TYPE` — a name whose every heavy shape is
  `UNSUPPORTED_VALUE` (ran, at the cap-boundary value, and failed fast
  — reassuring, but not proof of safety at every OTHER nearby value)
  is left off the allowlist, closing the exact gap `discrete_log`
  measured into under the old rules (a caveat found applying this
  rule: `discrete_log` itself still passes, since one of its OTHER
  heavy shapes — wrong-arity, not the matching one — resolves via
  `UNSUPPORTED_TYPE`; confirmed separately that `discrete_log` is
  genuinely fast up to the cap regardless, so this is a documented gap
  in the rule's own generality, not a live hazard).

- **The full allowlist was regenerated with the fixed measurement
  (`scripts/measure_safe_callables.py --write`, ~122 minutes): 415
  names allowlisted of 767 measured** (was 457 of 767 in d713821,
  round 14's own coordinator-authored commit — this file's own
  intermediate self-report of "387" mid-round was a stale in-progress
  number from before that commit, never the comparison baseline).
  `codecalc/_measured_safe_callables.json` (the committed, SHIPPED
  record) is now the TRIMMED form only — names, the measurement date,
  the two thresholds, no per-shape timing data (was ~1.3MB of raw
  probe data committed inside the shipped package directory; now
  ~7.6KB) — `scripts/_measured_safe_callables_raw.json` carries the
  full per-candidate, per-shape log instead, regenerable via `--write`
  and (round 15: gitignored, not committed — the same reasoning that
  keeps it out of the shipped package applies to the repository
  itself).

  **59 names LEFT the allowlist**, none previously-named by the
  coordinator's own expected-safe list, grouped by why:
  - **TIMEOUT at a heavy shape** (13): `ComplexField`, `Range`,
    `RealField`, `airybi`, `airybiprime`, `binomial_coefficients`,
    `composite`, `compositepi`, `continued_fraction_periodic`,
    `egyptian_fraction`, `fresnelc`, `fresnels`, `quadratic_residues`.
  - **CRASH at a heavy shape** (3): `binomial_coefficients_list`,
    `erfc`, `nroots`.
  - **`OK` but at or over `FAST_MS`** (20): `Chi`, `Ei`, `LambertW`,
    `Li`, `Shi`, `Unequality`, `ccode`, `chebyshevt_root`, `composite`
    (also here — two independent disqualifying shapes), `cxxcode`,
    `difference_delta`, `divisor_count`, `erfi`, `exp_polar`, `fcode`,
    `field_isomorphism`, `lambdify`, `maple_code`, `periodic_argument`,
    `principal_branch`.
  - **Rule-3 gate (every HEAVY shape came back `UNSUPPORTED_VALUE`,
    none `OK`/`UNSUPPORTED_TYPE` — the heavy boundary was never
    actually confirmed safe)** (22): `Integral`, `LC`, `LM`, `LT`,
    `Poly`, `PurePoly`, `RootSum`, `content`, `decompose`,
    `degree_list`, `differentiate_finite`, `discriminant`,
    `galois_group`, `gff_list`, `ground_roots`, `integrate`,
    `is_convex`, `monic`, `poly`, `poly_from_expr`, `primitive`,
    `sqf_part`, `sturm`.
  - **No shape ever matched this name's real signature at all** (1):
    `are_similar`.

  **17 names JOINED**: `Array`, `Atom`, `CC`, `EX`, `FF`,
  `InverseLaplaceTransform`, `Limit`, `S`, `Ynm`, `Ynm_c`, `Znm`,
  `atan2`, `betainc`, `betainc_regularized`, `random_poly`,
  `refine_root`, `to_number_field` — most plausibly the SAME class of
  fix that closed `factor`'s own false exclusion (round 15's own
  DISTINCT-heavy-values fix; the round-14 heavy shapes reused one
  value twice/thrice, which can trip a completely unrelated "duplicate
  argument" code path in some SymPy internals, unrelated to real
  magnitude cost) and the SIGALRM-to-thread timeout switch (a signal
  landing mid-call in a C-extension boundary can itself produce a
  spurious failure a thread-based join simply does not risk) — not
  individually re-verified name-by-name given the time budget; each
  is still subject to `_measured_safe_argument_cap_violation`'s own
  generic `MAX_HEAVY_ARG` cap regardless, the same safety net every
  measured-safe name gets.

  **One name (`jacobi_normalized`) that JOINED under round 15's own
  measurement was found to FAIL OPEN after the fact** (coordinator's
  own addendum, caught spot-checking the regenerated list): it is a
  PLAIN, EAGER function (`jacobi(n,a,b,x) * normalization`, computes
  for real the instant called, `evaluate=False` or not — the same
  shape `divisors`/`isprime` already needed a table row for), and its
  own real 4-positional-argument signature never matched any HEAVY
  probe shape with the heavy value in POSITION 0 — only `four_arg_
  heavy_last` (heavy LAST) existed at 4 args, so `jacobi_normalized
  (1463, 1, 2, x)` measured allowlisted despite hanging past 8s live.
  Given a dedicated `_FUNCTION_ARG_CAPS`/`_GROWTH_BOUNDS` row instead
  (mirrors `jacobi`'s own cap/growth exactly) and removed from the
  allowlist by hand (415 already reflects this — the JSON was patched
  directly rather than waiting on another ~2-hour full regeneration;
  `scripts/measure_safe_callables.py`'s own `_already_handled_names`
  now excludes it from candidacy on any future run too). The probe
  grid's own "heavy value only at position 0 alone or position LAST of
  four" gap (never every position of every arity) is NOT fixed this
  round — deferred, see the note at the end of this entry.

  `fps`/`nsimplify`/`continued_fraction`/`continued_fraction_
  convergents`/`continued_fraction_iterator` all SURVIVE the new,
  stricter measurement (`egyptian_fraction`/`continued_fraction_
  periodic`/`continued_fraction_reduce` do not — they stay default-
  deny). The coordinator's own item 2c asked survivors to get an
  explicit order/precision cap "like `series` has" — these five
  already get one: `_measured_safe_argument_cap_violation`'s own
  generic `MAX_HEAVY_ARG` cap IS that cap for a measured-safe name
  (unlike `series`, which is not measured-safe at all and needed a
  bespoke mechanism for that reason alone), and it was VALIDATED
  directly at the 1463 boundary by the very heavy-shape probes this
  round added — a SEPARATE, duplicate mechanism would only re-implement
  what the generic cap already enforces for these five specifically.

- **grok issue 3 (`verify-1095-r14-grok.log`): `tests/test_measured_
  safe_callables.py` was red on EVERY run of `ci-python.yml`'s own
  `windows-latest`/`macos-latest` legs (PR #343, `gh pr checks 343`) —
  not flaky, universally: every sampled name, every shape, came back
  `CRASH` at the exact process-level wall-clock bound.** Root cause,
  confirmed live pulling the actual job logs (`gh run view --job
  <id> --log`): the probe child's `import resource` raises
  `ModuleNotFoundError` immediately on Windows (the module does not
  exist there), and `RLIMIT_AS` is documented to misbehave on macOS —
  both crash the child before a single shape ever runs, on every
  platform but Linux. `signal.SIGALRM`/`setitimer` (used for the
  in-child call timeout) are POSIX-only too, a second, independent
  Windows-only crash. Fixed in `scripts/measure_safe_callables.py`'s
  own `_PROBE_CHILD`: the address-space limit is now platform-
  conditional (`RLIMIT_AS` on Linux, `RLIMIT_DATA` on macOS, none at
  all on Windows — a documented, weaker guarantee on that one platform
  only, not silently pretended away), and the call timeout is now a
  daemon THREAD (`.join(timeout=...)`) instead of `SIGALRM` — portable
  across all three platforms; a thread that is still running when the
  join gives up cannot be forcibly aborted the way a signal can, but
  the isolated CHILD PROCESS itself still dies (taking the daemon
  thread with it) the moment the PARENT's own `subprocess.run(timeout=
  PROBE_TIMEOUT_S)` gives up waiting, the same hard backstop this
  script already had.

  Also fixed, the SAME issue's second half: the re-check itself (`tests/
  test_measured_safe_callables.py`) used to re-apply `FAST_MS` (60ms) at
  full strictness on a random sample of 30 — a loaded CI runner crossing
  that tight bar is not evidence of a real regression (`continued_
  fraction_periodic` measured 57.789ms on one otherwise-clean run), and
  a random 30 of 415 could leave some name completely
  unchecked by any CI job, by chance. `scripts/measure_safe_callables.
  py` gained `shard_check`/`_shard_id`: this runner's own SHARD of the
  FULL allowlist (deterministic from platform + Python minor version,
  matching `ci-python.yml`'s own 3-OS x 2-Python `tests` matrix — the
  six jobs collectively cover every allowlisted name each CI run), and
  a regression now means only a `TIMEOUT`, a `CRASH`, or an over-
  `MAX_NUMERIC_DIGITS` result — never a merely-slower-than-`FAST_MS`
  `OK` result, as long as it stays under `REGRESSION_MS_TOLERANCE` (4x
  `FAST_MS`). `tests/test_measured_safe_callables.py` calls `shard_
  check` now instead of a random sample.

- **A Tuple/list/set argument that MIXES a symbol with a literal number
  hid the literal from every check that only ever asked "is this whole
  argument symbolic"** (coordinator addendum, confirmed PRE-EXISTING on
  `origin/main` too — no ceiling of any kind there): `arg.free_symbols`
  on `Tuple(x, 1, 10**6)` is non-empty purely because it CONTAINS the
  bound variable `x`, so `summation(factorial(x), (x, 1, 10**6))`,
  `summation(x**x, (x, 1, 10**5))`, and `summation(binomial(1463, x),
  (x, 0, 1463))` all hung past 8s — `summation` itself left `_MEASURED_
  SAFE_CALLABLES` this round (Rule-3 gate), so nothing else was
  standing between the literal `10**6`/`10**5`/`1463` hiding in the
  Tuple and real construction. Two fixes:

  (a) **Generic**: `_arg_hides_numeric` (new) — `_unbounded_generic_
  call_violation`'s own default-deny now walks a `Tuple`/`list`/`set`/
  `frozenset` argument's own MEMBERS, not just the container as a
  whole, so an UNKNOWN callable with a similar "container mixes a
  symbol with a literal" shape refuses like any other numeric call;
  `_measured_safe_argument_cap_violation`'s own generic `MAX_HEAVY_ARG`
  cap walks container members the same way, for a MEASURED-safe name.

  (b) **Dedicated rows**: `summation`/`product`/`Sum`/`Product` (new
  `_summation_product_violation`) and `integrate`/`Integral` (new
  `_integrate_limit_violation`) — both PLAIN, EAGER functions like
  `jacobi_normalized` above (`summation`/`product`/`integrate` compute
  for real the instant called), so these checks run on the DEFERRED
  (inert-stand-in) tree, the same "refuse before the real parse ever
  touches it" ordering every eager-callable fix in this module depends
  on. Each Tuple-shaped limit `(var, lower, upper)` gets: both bounds
  digit-capped (`oo`/`-oo` exempted — a genuine symbolic special value,
  no literal magnitude to be a hazard at all — `summation(1/x**2, (x,
  1, oo)) == pi**2/6` stays exact); the BOUNDARY term (`var` replaced
  by the resolved upper limit) walked through this module's own
  ordinary per-function cap machinery (`_numeric_ceiling_scan`) —
  `summation(factorial(x), (x, 1, 1464))` refuses on `factorial`'s OWN
  cap this way, independent of range length; and (summation/product
  only) the RANGE LENGTH itself capped per a MEASURED, per-table-name
  limit when the summand contains a table-bounded function — an
  ordinary polynomial/rational summand (`x**2`, `1/x`) gets NO range
  cap at all (SymPy's own closed-form formulas handle a huge range
  instantly, confirmed live: `summation(x**2, (x, 1, 10**6))`; the
  digit-based OUTPUT ceiling alone protects an oversized result from a
  table-free summand, confirmed live for both `summation(2**x, (x, 1,
  10**5))` and `integrate(x**200, (x, 0, 10**1000))`).

  The per-table-name range cap (coordinator's own correction: an
  earlier "range <= 50, always" guess was WRONG — `summation(bell(x),
  (x, 1, 1463))` does not literally evaluate 1463 independent Bell
  numbers and completes in 4.9s on `origin/main`) is now MEASURED:
  `summation(NAME(x), (x, 1, N))` timed at `N` in `{50, 200, 1463}`,
  `timeout 8`, for every value-kind table name (`tests/test_bug_sweep.
  py`'s own property check pins the result at each name's own cap
  edge). Twelve names stayed comfortably under 1s even at the FULL
  `MAX_HEAVY_ARG` and get that cap; four measured borderline-to-
  dangerous at 1463 (`catalan` 2.427s, `bell` 6.129s, `genocchi`
  1.590s, `andre` TIMEOUT past 8s) get a tighter, individually-
  measured-safe cap of 200 instead; an unmeasured table name (not part
  of the sweep — a digits-kind, pole-sensitive, or orthogonal-
  polynomial name) defaults to a conservative 50, `unknown != safe`.

  DEFERRED to a follow-up round, given the time budget — none of these
  are active hangs, each is either a still-safe (conservative)
  exclusion or a lower-severity robustness gap, not a hole in the
  class this ticket closes:
  - The probe grid still does not put the HEAVY value in EVERY
    position of every arity 1-4 (only first/last/pairs) — `jacobi_
    normalized`'s own fail-open (fixed by table row, above) was found
    BECAUSE of this gap; another name with a similarly-shaped signature
    (cost driven by an EARLY positional argument, at an arity/shape the
    current heavy probes do not cover) could still measure a false
    allowlisting. A `--refresh-names <file>` (or an extended `--refresh-
    excluded`) to re-check only affected names cheaply, plus the wider
    shape grid itself, are not yet implemented.
  - `LambertW`/`Ei`/`lambdify` (marginal `OK`, 76-79ms — just over
    `FAST_MS`) and the 8000ms-exact `CRASH`/`TIMEOUT` verdicts (`erfc`,
    `nroots`, `Range`, `ComplexField`, `RealField`, ...) were measured
    while a LEFTOVER investigation probe (an unrelated, accidentally-
    orphaned process from earlier in this round) had pinned one CPU
    core at 100% for over 10 hours, plus the box's own unrelated
    background suites — load artifacts, not necessarily real exclusions.
    `refresh_excluded()` gained a wider target selection (a `TIMEOUT`/
    `CRASH` at the OUTER process-wall bound specifically, or an `OK`
    between `FAST_MS` and `3 x FAST_MS`) but the actual re-run did not
    finish inside a 600s budget on this box's own persistent background
    load and was not restarted as a full run per instruction; these
    names stay conservatively excluded (the safe default) until a
    dedicated re-measurement pass. `quadratic_residues`, `egyptian_
    fraction`, and `continued_fraction_periodic` are NOT load artifacts
    — genuine heavy-shape TIMEOUTs at the cap — and stay excluded.
  (`erfc`/the `erf` family/`Ei`/`LambertW`/`Li`/`Chi`/`Shi`/`fresnels`/
  `fresnelc`/`airybi` — the "arguably belongs in the elementary bound
  table" note this bullet originally carried is now DONE, immediately
  below, same commit.)

- **A MEASURED list must never be the only thing standing between an
  ORDINARY special function and a refusal** (coordinator's own live
  spot-check of the previous round-15 commit, item 3 of the original round-15 message):
  `erfc(1)`, `LambertW(1)`, `Ei(1)` all refused as "not in the bounded
  function set" where `origin/main` evaluates them (stays symbolically
  unevaluated, exactly as `erf(1)` — already pole-sensitive-protected
  since round 12 — already correctly did). Seventeen more names
  (`erfc`, `erfi`, `Ei`, `Li`, `li`, `Chi`, `Shi`, `LambertW`,
  `fresnels`, `fresnelc`, `airyai`, `airybi`, `airyaiprime`,
  `airybiprime`, `expint`, `Si`, `Ci`) join `_POLE_SENSITIVE_NAMES`
  with a PROVABLE, closed-form analytic bound each — independent of
  any measurement, the same "exact-rational gate" mechanism (round
  11/12: bound only when the argument resolves to an exact real
  `Rational`; symbolic stays unresolved; anything else — complex,
  irrational, unresolvable — refuses as unknown) `gamma`/`zeta`/`sin`/
  `cos`/`tanh`/`erf`/`exp` already use:
  - `erfc(x) <= 2`; `Si(x)`/`Ci(x) <= 2`; `fresnels(x)`/`fresnelc(x)
    <= 1` — a fixed constant, any real `x`, the identical shape
    `sin`/`cos`/`tanh`/`erf` already have.
  - `erfi(x) <= e**(x**2)`; `Ei(x)`/`Shi(x) <= e**|x|` (real for any
    real `x`) — gated at `MAX_HEAVY_ARG` on the exponent itself.
  - `Chi(x)`/`li(x) <= e**|x|`, restricted to `x > 0` — found in this
    round's own property-check self-review BEFORE commit: both are
    COMPLEX-valued for `x <= 0` (`Chi(-1) == 0.838 + pi*I`, magnitude
    `~3.25`, already over the real-valued `e**1 ~= 2.72` bound a naive
    `abs(x)` treatment would have claimed) — refused for `x <= 0`
    instead of bounding a value on the branch cut.
  - `LambertW(x) <= log(x+1)` for `x >= 0` (refused negative — the
    principal branch's own domain).
  - `airyai`/`airybi`/`airyaiprime`/`airybiprime(x) <= e**(|x|**1.5)`,
    same `MAX_HEAVY_ARG` exponent gate on `|x|**1.5`.
  - `expint(n, x) <= 1` for `x >= 1` (refused otherwise; independent
    of `n`).

  `scripts/measure_safe_callables.py`'s own `_already_handled_names`
  now excludes every `_POLE_SENSITIVE_NAMES` member from measurement
  candidacy — closing a gap that predates this round: `erf` had been
  measured-safe-listed ANYWAY despite already being pole-sensitive-
  protected, letting the measured list's own unconditional `MAX_HEAVY_
  ARG` argument cap WRONGLY refuse `erf(10000)` (main evaluates it
  fine, `|erf(x)| < 1` for every real `x` regardless of magnitude) —
  found and fixed live spot-checking this round's own new code before
  commit. Seven names (`erf`, `li`, `airyai`, `airyaiprime`, `expint`,
  `Si`, `Ci`) were removed from the CURRENT allowlist JSON by hand for
  the identical reason (408 of 767, was 415 — the other eleven of the
  eighteen were already excluded from the round-15 measurement for
  unrelated reasons, load-artifact or genuine).

  Pinned live against `origin/main`, including the coordinator's own
  three named nested hangs (`factorial(floor(Abs(Ei(2000))))`,
  `factorial(ceiling(erfi(50)))`, `bell(floor(LambertW(10**1000)))`,
  all refusing in under a millisecond) and a property check —
  `tests/test_bug_sweep.py` evaluates SymPy's own `N()` across a grid
  inside each name's accepted domain and asserts every claimed bound
  actually holds, not merely eyeballed at one point (this is what
  caught the `Chi`/`li` complex-branch issue above, before commit).

## [0.13.0] — 2026-09-21

### Fixed

- **`codecalc setup --write` died before writing anything, on a cold grammar
  cache — which is every fresh install** (GH #339, THE-1096). `setup` calls
  `prefetch.main()` in-process to warm the cache; `prefetch.main` took no
  argument and parsed `sys.argv` itself, so inside `setup --write` it saw
  `setup`'s own argv (`['setup', '--write']`), argparse rejected both
  tokens, and the resulting `SystemExit(2)` was never caught — it propagated
  out of `setup` and killed the process before the MCP client config was
  written or the skill copied. `prefetch.main` now takes an explicit
  `argv: list[str] | None = None` (still `sys.argv`-parsing by default, so
  the `codecalc-prefetch-grammars` console script is unchanged) and `setup`
  calls `prefetch.main([])`; a `SystemExit` or any other exception from that
  call is now caught, mapped to an exit code, and reported through the
  `✗ prefetch exited N` branch that already existed to handle exactly this
  but could never be reached. This is issue #204's class recurring — a
  README headline command nothing verified — and is now covered by an
  end-to-end test in `tests/test_setup.py` that forces a cold cache and a
  failing prefetch and asserts the config and skill still land. Changed
  alongside the fix: the grammar-cache prefetch — the one `--write` action
  that makes a network call and can fail for reasons unrelated to codecalc
  — now runs *after* the client config and skill are written, not before
  (neither of those two writes depends on it), which renumbers `setup`'s
  printed sections (now 1-9, was 1-8); `setup` also now prints an `other
  clients: re-run with --client=...` line right after the detected-client
  line, naming every other client it knows how to configure (previously
  that hint was in `--help` only).

- **`branch_reachability` on `for i in range(a, b, 0)` raised an uncaught
  `ValueError` instead of the documented refusal** (GH #322, THE-1087).
  `_first_unsupported`'s up-front scan accepted a `for` loop as statically
  bounded once every `range()` argument was confirmed a literal int (or a
  negated one), never checking the step was non-zero; `_walk_loop` then
  handed those bounds to `len(range(start, stop, step))`, and CPython
  raises `ValueError: range() arg 3 must not be zero` at construction —
  `analyze()` caught only its own `_TranslateError`, so the `ValueError`
  escaped as `isError: true` with no result envelope, for one specific
  loop shape a caller correctly handling every other documented refusal
  still had to wrap in a try/except. The scan now rejects a literal step
  of 0 (including `-0`, the same int as `0`) as `unsupported construct:
  range() with a step of 0`, before a single z3 call — the same coded
  `validation` refusal, naming the construct and its line, every other
  unsupported construct already gets. Belt and braces: `_walk_loop` now
  refuses a step of 0 on its own too, narrowly, right where it would
  otherwise build `range(start, stop, 0)` — raising the ordinary
  `_TranslateError` `analyze()` already catches — so the scan and the
  walker agreeing is not the only thing standing between this input and a
  raised exception, in case the two drift apart again on some future
  construct. A blanket `except Exception` around the whole walk in
  `analyze()` was considered and rejected: cross-vendor review (Codex, PR
  #330) showed it would just as readily launder a genuine internal defect
  (an injected `RuntimeError`) into an "unsupported construct" refusal as
  it caught the intended `ValueError` — a regression test now pins that a
  real internal error still propagates out of `analyze()` instead.

- **`analyze_complexity` raised an uncaught `RecursionError` on deeply nested
  source instead of a coded refusal** (GH #324, THE-1089). `parsing.analyse`'s
  docstring promises "Never raises", but its post-parse tree walk was plain
  unguarded recursion with no depth cap — `analyze_complexity(code="("*4000 +
  "1" + ")"*4000, language="python3")` blew CPython's stack and the MCP
  dispatcher flattened it to a bare "Internal server error" with no `code` or
  `remedy`. `walk` now carries a 300-level tree-depth cap (measured against a
  realistic 300-line file at depth 20 and the issue's own depth-4000 repro),
  plus a belt-and-braces `except RecursionError`; either returns
  `ParseFacts(parsed=False, too_deep=True, reason=...)`, the same shape a
  grammar failure already produces. `complexity.analyze`/`analyze_complexity`
  turn that into `resource_exhausted` — a coded refusal, not an internal
  error — rather than silently falling back to the regex heuristic over
  adversarial input the way every other `parsed=False` reason still does.

- **`execute_code(compact=True)` dropped `stderr` unconditionally, so a
  failed run could say THAT it failed but never WHY** (GH #323, THE-1088).
  Same defect class as #117: that fix made `code`/`error`/`remedy` non-
  droppable because they are the entire content of a rejected-before-
  execution failure; `stderr` is the executed-and-failed counterpart and
  was still dropped, along with the compiler diagnostic on a compile
  failure. `stderr` is now kept, whole, whenever the run did not succeed
  (`ok` is `false`, `exit_code` is a nonzero integer, or `verdict` is not
  `OK` — the third catches an OLE run, which can otherwise report
  `ok: true, exit_code: 0`) and dropped on a clean run exactly as before,
  so the token saving compact mode exists for is unaffected on the success
  path. The `compact` parameter's schema description now says so. New field
  `stderr` on the `compact_result` shape (`docs/contract/README.md`,
  `CONTRACT_VERSION` `1.16.0` -> `1.17.0`, MINOR — additive, no shape a
  `1.16.0` client already validates changes on the success path), and
  `docs/contract/result-v1.schema.json`/`doctor-v1.schema.json` regenerated
  to match.

  Cross-vendor review of that fix (PR #333) found the bounding it relies on
  did not hold on every backend: the Piston provider's output cap
  (`codecalc/providers.py`) only applied when a caller passed an EXPLICIT
  `max_output_kb`, so a caller who left it at its documented default (0)
  got Piston's raw response back uncapped — reproduced with a simulated
  1,000,000-byte stderr coming back whole, worst of all in a compact
  result. Piston now applies `executor.MAX_OUTPUT_BYTES` (64 KiB) as its
  own default when `max_output_kb` is 0, the same default the native and
  fallback executors already apply, so all three providers agree on what
  "the default cap" means. The `compact` parameter's description also said
  stderr was "dropped only on ok=true" — true of the common case but not
  of an OLE run, which can be `ok: true` — corrected to name the real
  condition (`ok=true`, `exit_code` 0, `verdict` `OK`).

- **`verify_optimization`'s headline `speedup.ratio` could be set entirely by
  a size the significance test itself had already excluded as timer-noise.**
  `_speedup` sourced its pool from `_comparable_positions` directly, but
  `_infer_speedup` narrows that pool twice further (a `min_testable_n`
  floor, and a normal-approximation-reliability gate on tied, overlapping
  samples — #284/#285) and neither narrowing fed back, so a size excluded
  to `inference.sizes_below_floor` could still dominate the median a clean
  `accepted: true, reason: "verified faster: …"` sentence quoted. `_speedup`
  now accepts the exact survivor list `_infer_speedup`'s filters produce
  (factored into shared `_testable_positions`), and `verify_optimization`
  passes it explicitly, so the ratio and `speedup.per_size` can only ever be
  built from a size the significance test also kept (GH #328, THE-1093).

- **`benchmark` could report a confident `O(n^2)`/`O(n^3)`/`O(n log n)` for a
  genuinely `O(n)` program at its own documented 3-size minimum** — `estimate
  = _classify_by_ratio(ratios) if ratios else fit["estimate"]` trusted the
  ratio median the moment `ratios` was non-empty, with no floor on how many
  (one noisy doubling ratio decided the answer outright), while the curve fit
  that should have arbitrated could not even run: baseline subtraction forced
  the smallest of 3 sizes' corrected duration to exactly 0, leaving `_fit_class`
  with 2 points against its own `len(pts) >= 3` floor and an empty
  `candidate_scores`. Reproduced on the issue's own O(n) program: 200k/400k/
  800k sizes reported `O(n^3)` (`doubling_ratios: [8.29]`) on one run and
  `O(n^2)` (`[4.29]`) on a rerun, both with `method: "empirical"` and
  `candidate_scores: []`. `_fit_class` now fits an additive model
  (`t = intercept + coefficient·f(n)`) directly against raw measured times
  instead of baseline-subtracted ones, so nothing is zeroed out and the fit
  is reachable at exactly 3 sizes.
  A new `_decide_estimate` gates BOTH estimators independently —
  `MIN_ROBUST_RATIOS` (3) for the ratio median, a new `MIN_FIT_POINTS` (4,
  the degrees-of-freedom floor for a 2-parameter fit to discriminate across 8
  candidate classes) for the curve fit — and when neither clears its floor,
  `estimate` now reads `"inconclusive (...)"` with the reason, instead of a
  guess dressed as a measurement. A new `estimate_basis` field
  (`"ratio-median"` | `"curve-fit"` | `"noise-floor"` | `"inconclusive"`)
  names which estimator actually produced `estimate`, so a caller no longer
  has to parse `ratio_confidence` prose to tell a robust answer from a guess.
  `method` stays `"empirical"` throughout — the measurement is still real
  evidence even when the classification is honestly inconclusive.
  (GH #327, THE-1092)
- **Cross-vendor review (Codex) of the fix above found the opposite failure
  mode: genuinely exponential growth confidently misclassified as
  polynomial.** `_CLASSES` (the curve fit's 8 candidate shapes) has no
  exponential entry, so data with too few doubling ratios for a robust median
  fell all the way through to the curve fit, which ranked its 8 bad options
  and handed back the least-bad one regardless of how bad. Reproduced:
  `[10,20,40,80]` / `[1,10,200,5000]` (genuinely exponential; 2 doubling
  ratios, `[22.11, 25.12]`, one short of `MIN_ROBUST_RATIOS`) returned
  `O(n^3)` at `relative_error: 41.0` (a 4100% average miss) with a
  physically-impossible negative `intercept` (`-162.4`ms — startup overhead
  cannot be negative). Two independent guards now cover this: (1)
  `_decide_estimate` no longer trusts a curve fit whose winning candidate's
  `relative_error` exceeds `MAX_TRUSTED_RELATIVE_ERROR` (1.0) or whose
  `intercept` is more negative than `INTERCEPT_NOISE_TOLERANCE_MS` allows for
  noise (-5ms) — such a fit now reports `"inconclusive (...)"` instead of its
  nominal "winner"; (2) a new `STRONG_EXPONENTIAL_RATIO` (12) lets an
  unambiguous exponential SIGNAL decide with fewer than `MIN_ROBUST_RATIOS`
  ratios — the one deliberate exception to "too few samples, do not decide" —
  but only when EVERY available ratio clears it, not just the median.
  (THE-1092)
- **`candidate_scores[*].c` silently changed meaning in the fix above** — it
  was the coefficient of `f(n)` in the old multiplicative fit (`t = c·f(n)`);
  an early draft of the additive-model fix quietly repurposed it to mean the
  new intercept instead, an undocumented field-meaning change an existing
  consumer would never have noticed. `c` now keeps its PRE-#327 meaning
  (the coefficient); the new intercept is exposed only under its own name,
  `intercept`, alongside an explicit `coefficient` alias for `c` so a fresh
  reader is not stuck decoding a single letter. (THE-1092)
- **A second cross-vendor review round found the `STRONG_EXPONENTIAL_RATIO`
  shortcut above still trusted a SINGLE ratio.** "Every available ratio
  agrees" was checked with `all()` over whatever `ratios` held, with no floor
  on its length — a set of one vacuously "agrees with itself". Reproduced on
  data that is quadratic-ish and startup-dominated, NOT exponential: sizes
  `[10,20,40]` / times `[1000,1004,1064]` baseline-subtract to corrected
  `[0,4,64]`; the first gap is dropped as always, leaving exactly one ratio
  (`64/4 = 16.0`) — a legitimate `> 3.0` corrected denominator (4ms) still
  small enough to inflate, exactly the failure mode `_classify_by_ratio`'s
  own docstring already warns about for the ordinary median path, now
  reachable by a shortcut allowed to decide on FEWER samples than that path
  ever is. Two independent guards, both required: a new
  `MIN_STRONG_EXPONENTIAL_RATIOS` (2) — the shortcut cannot fire below it,
  so (since 3 sizes yield at most 1 doubling ratio) 3-size data can never
  trigger the exponential override at all, only `noise-floor` or
  `"inconclusive"`; and a new `raw_ratios` cross-check — for each doubling
  pair, `benchmark()` now also computes that pair's ratio from UNSUBTRACTED
  times, and `_decide_estimate` requires BOTH the corrected ratio AND its
  raw counterpart to independently clear `STRONG_EXPONENTIAL_RATIO`. A raw
  ratio, dominated by real subprocess/interpreter overhead, does not look
  exponential from noise alone the way a small corrected denominator can —
  confirmed on the original passing repro (`[10,20,40,80]`/`[1,10,200,5000]`:
  raw ratios `20.0`/`25.0`, both still clear the floor) and on the new
  quadratic-ish one (raw ratio `1.06`, nowhere close). (THE-1092)
- `benchmark`'s docs still called 3 sizes a plain "minimum" after the first
  fix above made 3 sizes unable to decide a polynomial/log-family growth
  class — the `sizes` size-count floor, its `Field` description, and
  `benchmark`'s docstrings (`server.py` and `tools.py`) now say so
  explicitly: 3 is still accepted (unchanged, backward compatible), but can
  only return noise-floor `O(1)` or `"inconclusive"` — never any OTHER
  growth class, not even exponential (see the guard above: 3 sizes cannot
  reach `MIN_STRONG_EXPONENTIAL_RATIOS` either). 4+ sizes are needed before
  the curve fit is trusted, 5+ doubling sizes before the general ratio median
  is. The default `sizes` ("100,1000,10000,100000", 4 sizes 10x apart)
  always decides via `estimate_basis: "curve-fit"` — verified against 3 live
  runs, never `"ratio-median"` (10x-apart sizes produce no doubling pairs at
  all) and never `"inconclusive"` (4 points clears `MIN_FIT_POINTS`).
  (THE-1092)

- **`trace_execution` reported lines that demonstrably ran as `lines_never_executed`**
  whenever one legitimate trace event's `locals` diff exceeded
  `_MAX_LOCALS_ENTRIES` (200) — a function called with more than 200
  parameters is the simplest trigger (GH #321, THE-1086). `_parse_trace_
  file`'s `expected_step` only advanced on ACCEPTANCE, while the harness's
  own step counter advanced on EMISSION, so the single over-cap event was
  discarded without advancing the parser's counter and every later,
  perfectly ordinary event then failed the step-continuity check too — the
  rest of the trace silently vanished, `stdout` proved the "never executed"
  lines had in fact run, and `events_consistent` went `false` for a program
  nobody tampered with. A matching-step event that fails only a size cap is
  now ACCEPTED as a bounded stub — its real `step`/`line`/`event`/`func`
  kept, `locals` replaced with `{}`, and a new `detail_dropped: true`
  marker added — rather than discarded outright, so a real, correctly
  positioned coverage sample is never lost along with the oversized detail
  (a cross-vendor review of the first version of this fix, which dropped
  the whole event, caught that a program creating its over-cap locals on
  line N and nothing else there still came back with line N in
  `lines_never_executed`). The drop is disclosed via a new
  `truncated_reason` value, `"event_detail_over_cap"`, and the stub is
  never counted in `discarded_events` — only a genuinely malformed or
  tampered line is. The same review also found that accepting oversized
  events this way reopened a forgery gap: a sandboxed program can pre-write
  a forged, step-matching event for a step the harness has not emitted yet,
  which displaces the harness's own later, genuine event for that step
  (rejected as stale) while the count/step arithmetic alone still balances.
  `events_consistent` now additionally requires `discarded_events == 0` —
  an honest harness never produces a discarded event on its own — which
  closes that gap without costing the legitimate over-cap case.
  `CONTRACT_VERSION` `1.17.0` -> `1.18.0`, MINOR — additive: `truncated_reason` gains
  `"event_detail_over_cap"` and `events[]` items gain the optional `detail_dropped: true`
  marker; no shape a `1.17.0` client already validates changes otherwise.

### Added

- **`percent_change(from_value, to_value)`** (calculator group) — exact
  percent change between two values, `(to - from) / abs(from) * 100`, the
  conventional finance definition (relative to the MAGNITUDE of the base,
  so a negative `from_value` does not flip the sign). `percentage`
  answered PART/TOTAL and had no before/after equivalent, so models were
  hand-building `(b - a) / a * 100` in `calc_exact` — the exact guessing
  this package exists to remove, and the easy place to get the base
  wrong. Returns `percent_exact` (exact rational), `percent_decimal`
  (rounded), `absolute_change`, `direction`
  ("increase"/"decrease"/"unchanged"), and `multiplier` (`to_value /
  from_value`, exact); refuses `from_value=0` (coded `validation`, remedy
  "report the absolute change instead") rather than returning infinity,
  and adds a `note` spelling out the sign convention whenever
  `from_value` is negative. The served surface moves from 49 to **50
  tools** (GH #329, THE-1094).

- **`session_delete_file(session_id, path)`** (sessions group, 50th MCP
  tool) — remove one file (or symlink entry, never its target) from a
  session workspace. `_jail_nofollow` (sessions.py) is pure STRING
  validation — length, segments, absolute-path/`..` escape — and never
  touches the filesystem; it returns the path as workspace-relative
  COMPONENTS, not a resolved `Path`, specifically so nothing here ever
  resolves a symlink at (or through) the final component the way `_jail`
  does for a read/write (which is right for THOSE — never disclose or
  overwrite what an outside-pointing link targets — and wrong for a
  delete, which removes the in-workspace entry regardless of where it
  points, so `unlink()` on a symlink already never following it is what
  makes the target survive). The actual delete (`_unlink_pinned`) walks
  the real filesystem exactly ONCE: an `openat`-style directory-fd hop per
  path component from the workspace root, `O_NOFOLLOW|O_DIRECTORY` on
  every hop, `lstat`/`unlink` on the final name relative to the last hop's
  fd — the portable equivalent of `openat2(RESOLVE_BENEATH|
  RESOLVE_NO_SYMLINKS)`, needing no identity/inode comparison because
  there is no second walk of the same path left for a racing session
  worker (`rename`/`symlink` a component mid-delete — the server process
  is not Landlocked) to win against (found and closed across four rounds
  of review before this ever shipped; `_DIR_FD_SUPPORTED` gates the
  platforms where this applies — requiring `O_NOFOLLOW` explicitly, not
  just `O_DIRECTORY`/`dir_fd` support, so a hypothetical host with the
  latter but not the former takes the documented Windows-style fallback
  instead of silently following symlinks per hop — with a documented
  residual on Windows). The workspace-root open itself is guarded the
  same way every per-component hop already is, so a failure there is a
  coded refusal, never an uncaught `OSError`. Refuses exactly what
  `session_artifacts` already excludes (`.codecalc-run/`, `.codecalc-spill/`,
  the session lock file, the idle-expiry marker, `__pycache__`/`*.pyc` —
  `_is_runner_internal`, factored out of `_workspace_scan` so the two
  definitions cannot drift) and a directory — one file/symlink per call,
  the same granularity `session_write_file` writes at. Every one of those
  checks — the two root-level reserved files AND the two runner-directory
  PREFIXES — now goes through one shared `_normalized_component_matches`
  (casefold, trailing-`.`/` ` strip, Windows 8.3-short-name shape refused
  outright at the session root), so `.CODECALC-RUN/main.py` or
  `.codecalc-run./main.py` are refused the same way `.CODECALC-SESSION-
  LOCK` already was, instead of the prefix check being a separate, less
  strict, bare `==`. The Windows fallback path additionally resolves the
  final component to its long form before that check runs, for an 8.3
  alias a casefold/strip comparison alone cannot enumerate. Deliberately
  exempt from BOTH the byte and artifact-count quota gates: a delete can
  only shrink usage, never grow it, so gating it on a cap it can only
  relieve would refuse the one call that fixes the refusal (see "Fixed"
  below).

### Changed

- **`symbolic(op="solve_linear")` and `evaluate_expression`'s
  descriptions now say what they already did.** `solve_linear` has always
  reached sympy's general `solve()`, so non-linear polynomial systems
  ('x**2 + y**2 = 5; x - y = -1') solve the same way linear ones do — the
  docstring and the `system` parameter's schema description previously
  showed only a linear example and read as though the name were a
  behavioural promise. `evaluate_expression`'s docstring mentioned
  `integrate` once as an example and never named `diff` or `series` at
  all, despite all three working today (`diff(expr, x)`,
  `integrate(expr, x)` / `integrate(expr, (x, a, b))`, `series(expr, x,
  x0, n)`) — a model reading the schema had no way to discover calculus
  was reachable at all. No behaviour changed; every example quoted in
  the new prose was run and its output checked before being written down
  (GH #329, THE-1094).
- `symbolic`/`evaluate_expression`'s new prose (above) trimmed shorter
  after cross-vendor review measured it costing top-1/top-3 hits on
  `scripts/tool_select_eval.py`'s baseline (BM25 penalises added
  vocabulary that dilutes a document's existing terms) — the worked
  `diff`/`integrate`/`series` example values moved out of the docstring
  into the `expression` parameter's own schema description only, and the
  `solve_linear` non-linear example dropped its "-> two solutions"/
  "Ordinary linear example:" framing. `scripts/data/tool_select_baseline.json`
  regenerated. GH #337 round-2 review: a first pass at this bullet
  reported approximate numbers from an earlier, pre-trim measurement.
  Below is a from-scratch recomputation (`origin/main`'s `codecalc/`
  checked out via `git archive` to a scratch dir, scored against the
  identical unchanged 234-prompt corpus with the same BM25 evaluator) —
  net top-1/top-3 deltas, and every prompt whose hit/miss status
  actually changed (a much larger set of prompts change RANK without
  changing hit/miss status at all — pure BM25 score noise from adding a
  50th candidate document; those are not listed):

  | surface | top-1 delta | top-3 delta |
  |---|---|---|
  | full | -2 | -1 |
  | dev | 0 | -1 |
  | core | 0 | +1 |

  **full** (5 prompts changed hit/miss status):
  - top-1 LOST, "I have a formula and I just want its algebraic value
    worked out, not solved for a variable — sqrt(2)*sqrt(8)" (expected
    `evaluate_expression`): old top1=`evaluate_expression`
    top3=[`evaluate_expression`,`compare_threshold`,`calc_exact`] ->
    new top1=`compare_threshold`
    top3=[`compare_threshold`,`evaluate_expression`,`calc_exact`] (still
    top-3).
  - top-3 LOST, "I need both unknowns pinned down from two equations
    that share them" (expected `symbolic`): old
    top3=[`percentage`,`symbolic`,`data_sizes`] -> new
    top3=[`percentage`,`data_sizes`,`bits`] (`symbolic` drops to rank 4).
  - top-1 LOST, "Take this messy algebraic formula and give me its
    cleanest possible form" (expected `evaluate_expression`/`symbolic`):
    old top1=`evaluate_expression`
    top3=[`evaluate_expression`,`algebraic_equiv`,`symbolic`] -> new
    top1=`algebraic_equiv`
    top3=[`algebraic_equiv`,`evaluate_expression`,`symbolic`] (still
    top-3 on both).
  - top-3 GAINED, "Fetch a file from the persistent workspace, and if
    it's a picture, hand it back as something I can actually view"
    (expected `session_read_file`): old
    top3=[`session_start`,`evaluate_expression`,`session_write_file`] ->
    new top3=[`session_start`,`session_write_file`,`session_read_file`].
  - top-3 LOST, "Compute the exact asymptotic value of n over log n as n
    becomes arbitrarily large." (expected `symbolic`): old
    top3=[`percentage`,`calc_exact`,`symbolic`] -> new
    top3=[`percentage`,`percent_change`,`calc_exact`] (`percent_change`
    — a 50th candidate document that did not exist in the old corpus at
    all — takes the slot `symbolic` held; `symbolic` drops to rank 4).

  **dev** (3 prompts changed hit/miss status, net top-1 delta 0 — one
  lost, one gained):
  - top-1 LOST, same "sqrt(2)*sqrt(8)" prompt as full, same ranks.
  - top-1 GAINED, "What does the computer actually store in memory for
    the number 0.1?" (expected `float_repr`): old
    top1=`evaluate_expression`
    top3=[`evaluate_expression`,`float_repr`,`list_languages`] -> new
    top1=`float_repr`
    top3=[`float_repr`,`list_languages`,`calc_exact`].
  - top-3 LOST, same "asymptotic value of n over log n" prompt as full,
    same ranks (`percent_change` again).

  **core** (1 prompt changed hit/miss status):
  - top-3 GAINED, "Is 3/7 strictly greater than 0.4, exactly, no float
    error?" (expected `compare_threshold`): old
    top3=[`float_repr`,`collision_probability`,`calc_exact`] -> new
    top3=[`float_repr`,`collision_probability`,`compare_threshold`].

  Correction to the round-1 version of this bullet: `symbolic` does
  **not** gain top-1 on the "two unknowns" prompt on dev/core — measured
  directly (both surfaces, both old and new schemas), `symbolic` sits at
  rank 4 on both, unchanged. Net top-1 vs the pre-PR baseline on the
  unchanged 234-prompt corpus: full -2, dev 0, core 0 (`percent_change`'s
  own 4 new prompts, 4/4 top-1 on every surface, are additive on top of
  this and are why the checked-in baseline's TOTALS still read as a net
  gain — see GH #337 review discussion; `tests/test_tool_select_eval.py`
  now also compares per-prompt outcomes against the pre-PR baseline over
  this same unchanged-prompt intersection whenever the corpus hash has
  moved, rather than trusting a self-referential "drop_hits=0" against a
  baseline just regenerated from the current state).
- **`docker/mcp-catalog/tools.json` and `scripts/data/tool_select_baseline.json`
  regenerated for `session_delete_file` (51st tool) — 3 new labeled
  prompts added to `scripts/data/tool_select_prompts.jsonl`** (241 total,
  none containing `session_delete_file`'s own name or underscore tokens —
  `validate_prompts()` confirms zero violations), scoring 2/3 top-1, 3/3
  top-3 on `full` (`{"n": 3, "top1": 0.6667, "top3": 1.0}`); the one
  top-1 miss ranks 3rd behind `session_snapshot`/`session_stop`, both
  genuine lexical competitors for "too many files in a workspace, clear
  some out."

  `docker/mcp-catalog/tools.json` was regenerated from a live in-process
  `tools/list` capture (`tests/_mcp_client.py`'s `in_process()`, no Docker
  build needed) rather than hand-edited — the structural diff against the
  committed file is **not** "only `session_delete_file` added": three
  OTHER tools' descriptions had also drifted from their own already-merged
  docstring changes that never got a catalog regen (`execute_code`
  THE-1088, `trace_execution` THE-1086, `benchmark` THE-1092) — all three
  resynced to their current live text as a byproduct of doing this
  regeneration honestly rather than hand-patching in only the one new
  entry.

  Regression check on the 238 PRE-EXISTING prompts, against `origin/main`'s
  own checked-in baseline (`tests/test_tool_select_eval.py`'s corpus-change
  guard, `full` only — that guard does not run per-surface): **zero**
  newly-missed top-1 hits (`shared=238 newly_missed=0`). Verified further
  by hand across all three surfaces with BOTH sides freshly measured —
  `origin/main`'s own code re-run live, not its checked-in baseline FILE —
  since `session_delete_file` sits in the `sessions` group only, it is not
  even a candidate document for `dev`/`core` scoring, and on `full` every
  one of the 238 pre-existing prompts' hit/miss status is bit-for-bit
  unchanged: **0 hit/miss transitions, gained or lost, on any surface.**

  The checked-in baseline FILE's totals move `full` 152->156 / `dev`
  134->133 / `core` 82->82 top1_hits (`n` 238->241 on `full`, unchanged on
  `dev`/`core` since none of the 3 new prompts are applicable there). Do
  not read that `dev` number as a regression this PR caused: re-running
  `origin/main`'s OWN current code live (rather than trusting its checked-in
  baseline file) scores `full` at 154/238 and `dev` at 133/238, not the
  checked-in 152/238 and 134/238 — the checked-in baseline UNDERSTATES
  `full` by 2 hits and OVERSTATES `dev` by 1, a pre-existing drift between
  that file and the code it describes, predating this PR (most likely
  earlier PRs' own docstring edits that were never re-baselined). Diffing
  against the checked-in file alone would read as "full gains 2, dev drops
  1"; diffing both sides freshly measured — the check that actually rules
  out a regression — shows neither is real: `full`'s "+2" is fully
  accounted for by the 3 new prompts' own 2 top-1 hits, and `dev`'s "-1"
  does not exist at all once its own baseline number is corrected to what
  `origin/main`'s code actually produces today.

### Fixed

- **`percent_change` on extreme-magnitude inputs.** `'1e400'` returned a
  non-finite `percent_decimal: Infinity` (invalid JSON — `Fraction`'s
  scientific-notation parsing builds the exact integer directly, so it
  never overflows through `float` the way a bare `float(s)` call would);
  `'1e5000'` raised an UNCAUGHT `ValueError` (Python's int<->str
  conversion ceiling, 4300 digits) with no coded result at all; and two
  individually-in-bounds inputs with opposite-sign extreme exponents
  (e.g. `'1e-3999'` / `'1e3999'`) could still combine into a computed
  result over that same ceiling. `from_value`/`to_value` are now screened
  for their implied digit count (mantissa digits + `abs(exponent)`,
  mirroring `MAX_NUMERIC_DIGITS` — the same ceiling `calc_exact`'s own
  literal screen enforces) BEFORE `Fraction()` ever runs, and the actual
  computation is wrapped in a `ValueError` backstop for the residual
  combined-inputs case; `percent_decimal` is now always finite JSON —
  `null` with a `note` when the exact value has no finite float
  representation, never a bare `Infinity` (GH #337 cross-vendor review).

- **A session over `CODECALC_MAX_ARTIFACT_COUNT` was permanently
  unrunnable** (GH #325, THE-1090): `quota_precheck` refuses
  `execute()`/`session_run` up front once a session's artifact count is
  over the cap, and `write_file` can only create or overwrite content,
  never remove it — so nothing inside the session could ever shrink the
  count back under the cap, short of `session_stop` destroying the whole
  workspace. The refusal's own remedy ("delete unneeded files ... or raise
  CODECALC_MAX_ARTIFACT_COUNT") named an action no tool in the package
  could perform. `session_delete_file` above is the in-band recovery path
  the byte quota already had (`_write_guard`'s `net_bytes <= 0` early
  return) and the count cap lacked; both artifact-count refusals'
  `remedy` strings now name it instead of a `session_files` listing tool
  that was never able to act on what it showed.

- **`reject_explosive` only inspected `Pow` nodes, so a product (or sum) of
  individually legal heavy calls bypassed `MAX_NUMERIC_DIGITS` and reached
  CPython's own int->str ceiling instead** (GH #326, THE-1091).
  `symbolic(op="simplify", expr="*".join(["factorial(1463)"] * 117))` —
  1871 chars, under the 2000-char cap, every `factorial(1463)` individually
  legal at 3998 digits — materializes to `Mul(Integer, Integer, ..., 117 of
  them)` during PARSING itself (a function call on a literal argument
  evaluates at parse time regardless of `evaluate=False`), a shape with no
  `Pow` anywhere for the old Pow-only walk to catch. It returned
  `"code": "internal"` with a raw CPython message ("...use
  sys.set_int_max_str_digits()...") addressed to nobody the caller can act
  on. `reject_explosive` now runs a SEPARATE pass — an order-independent,
  memoized, single-visit-per-node log-magnitude scan (`_numeric_ceiling_
  scan`/`_log10_magnitude`) — over every numeric-only `Mul`/`Add`/`Integer`
  subtree, refusing an over-cap product or sum BEFORE evaluation with
  `"resource_exhausted"` and a remedy addressed to the caller, without
  touching the pre-existing `Pow`-only loop at all. Two follow-on bugs, both
  caught by cross-vendor review before release: the first version delegated
  to the exact-accumulation bit-budget check the `Pow` branch already used,
  which is ORDER-DEPENDENT — `factorial(1463)*factorial(1463)/
  (factorial(1463)*factorial(1463))` is exactly 1, but was refused as
  over-cap because the numerator's intermediate product blew the budget
  before the cancelling denominator was multiplied in — and re-resolved
  every numeric subtree at every numeric ancestor, superlinear in tree
  depth (an alternating Add/Mul chain of depth 250 measured 0.311s in
  `reject_explosive` alone, now under 20ms). Separately, `exact.py`'s
  `except Exception`/`except ValueError` catches that returned a bare
  `{"ok": False, "error": ...}` (the four generic catch-all clauses in
  `eval_exact`, `solve_expression`, `limit_expression`,
  `simplify_expression`, plus `eval_exact`'s own Fraction-formatting catch
  and `radix_convert`'s digit-parsing catch) now route through
  `errors.classify`, which requires BOTH fragments of CPython's digit-limit
  message (not a single overly-broad substring) to map a `ValueError` to
  `resource_exhausted` instead of falling through to the generic
  `ValueError` -> `validation` mapping or, previously, `internal`. A third
  cross-vendor review round found three more gaps in the round-two design:
  (1) `_log10_magnitude` on a `Mul`/`Add` returned `None` — inconclusive,
  fall through — the moment ANY child was inconclusive, but SymPy flattens
  a chain of the same operator into one n-ary node, so `x *
  factorial(1463) * ... * factorial(1463)` (117 of them; also reproduced
  with a `cos(0)*`, `pi*`, `E*`, or `2**(1/2)*` prefix) kept every
  `factorial(1463)` sibling individually under the digit cap and bypassed
  refusal entirely, still materializing the ~467,766-digit product. Fixed
  by refusing on the RESOLVABLE part of a Mul/Add alone once the whole
  node is inconclusive — a symbolic or unresolvable sibling cannot make an
  already-over-cap numeric part smaller in any way the printer would
  rescue. (2) `_log10_magnitude`'s recursive descent sat outside
  `safe_parse`'s own `try/except` (which only wraps the parse step), a
  latent `RecursionError` risk on a sufficiently deep tree; now guarded at
  the `reject_explosive` call site, refusing with the same "too deeply
  nested" wording `logic.py`'s parser already uses for the identical
  shape. (3) `reject_explosive`'s `Pow` branch still resolved a
  Mul/Rational-shaped base or exponent via the order-DEPENDENT
  `_bounded_numeric_value` directly, so a cancelling Mul used as a Pow's
  base or exponent (`2**(f*f/(f*f))`, `(f*f/(f*f))**2`) still
  false-refused even though the identical cancellation at the top level
  was already fixed; both now resolve through `_log10_magnitude` first
  (`_resolve_numeric_exactly`), falling back to an exact reconstruction
  only once that confirms it is safe (cheap) to. A fourth review round
  found the residual gap was architectural, not three more instances:
  printability was still being measured as a single signed
  log10(|value|), when the actual invariant this module needs is that no
  numeric subtree's PRINTED numerator or denominator ever exceeds
  `MAX_NUMERIC_DIGITS` — a value can be tiny (even negative in log space,
  a reciprocal) while its denominator alone is thousands of digits
  (`1/(factorial(1463)*factorial(1463))`), and `Pow` itself was not part
  of the ceiling scan at all, so a trivial `**1` or a reciprocal `**-1`
  walked an over-cap numeric part straight through it
  (`("*".join(["factorial(1463)"]*117)+"**1")`, `(f*f)**-1`). Replaced
  the scalar magnitude model with a numerator/denominator pair tracked in
  log space throughout (`_log10_num_den`/`_factor_multiset`/
  `_multiset_log_num_den`/`_safe_multiset_rational`), folded `Pow` into
  the scan's own trigger set instead of a separate loop, and removed
  `_resolve_numeric_exactly`'s `evaluate=True` reconstruction entirely —
  SymPy's own `Mul.flatten` folds integer powers of a numeric base
  internally regardless of the final combined magnitude, so
  `2**(2**N * 3**(-N))` for a large `N` tried to materialize the literal
  `2**N` on the way to a negligible net value; the exponent's value is
  now derived purely from log-space arithmetic and a per-term-gated exact
  `Fraction`, never SymPy's own evaluator. A fifth review round found one
  more residual class in that same Pow branch: a numeric base whose
  exponent is neither a bare `Integer` nor a `Mul`/`Pow`-of-`Integer`
  multiset reducible to an exact integer (an `Add` exponent —
  `2**(14999+1)`, the same value as the already-refused `2**(30000/2)`,
  or `2**(factorial(1463)+1)` — or a genuinely non-integral rational
  whose base still makes the result a huge integer — `(10**3000)**(3/2)
  == 10**4500`) fell through unrefused, and the symbolic-base
  `MAX_SYMBOLIC_EXPONENT` cost ceiling had the identical gap
  (`(x+1)**(1000+1000)`). Fixed by never requiring exactness for the
  refusal VERDICT at all: an upper bound on the exponent's magnitude,
  derived from the same numerator/denominator machinery every other node
  already uses, is enough to decide whether `base**exponent` would print
  over cap, without ever resolving the exponent's actual value — which
  also closes a second, narrower gap in the exact-value path it replaces
  (`_safe_multiset_rational` gated each individual factor's size but not
  their combined product, so an exponent built from 20+ distinct heavy-
  call results, each individually small, still multiplied into a real
  ~10**5-digit `Fraction`). `_safe_multiset_rational` — along with
  `_bounded_numeric_value`, `_NumericTooLarge`, `_SUBTREE_BIT_BUDGET`,
  and `_SAFE_RECONSTRUCT_DIGITS`, all dead code by this point — is
  deleted rather than patched. A second, independent review family
  (Codex) found four more findings in parallel, none overlapping: (1) a
  COMPUTED heavy-function argument (`factorial(1463+1)`,
  `fibonacci(1463*1000)`) bypassed the token-level `_heavy_call_
  violation` entirely — every token is individually under
  `MAX_HEAVY_ARG`, and the resulting `Function` node stayed opaque to the
  numeric scan; fixed by bounding a heavy function's own argument at the
  TREE level too, via the same `_log10_num_den` machinery, with the
  token check kept as the cheap first pass. (2) `bell`, `genocchi`,
  `motzkin`, `andre`, and `partition` are admitted by the parser's
  namespace but were missing from `_HEAVY_FUNCTIONS` — `bell(1500)` took
  5.48s and produced a 3,107-digit `Integer` DURING PARSING, before any
  guard ran; added at the same `MAX_HEAVY_ARG` cap after auditing every
  eager combinatorial/number-theoretic name sympy 1.14 actually exposes.
  (3) a scientific-notation `Float` literal (`1e100000`, nine
  characters) is a parse-time CPU bomb — `1e1000000` ran past 30s just
  constructing the `evaluate=False` shape, before `reject_explosive` (or
  even `reject_unsafe`'s existing SECURITY screen) ever got a chance —
  now refused at the TOKEN level, before `parse_expr` runs at all, for
  any literal whose scientific-notation exponent magnitude exceeds
  `MAX_NUMERIC_DIGITS`. (4) `Add`'s upper bound recognized no
  cancellation at all, so two terms that are EXACT additive inverses of
  each other (`factorial(1463)*factorial(1463) -
  factorial(1463)*factorial(1463)`, printable, cheap, truly `0`) were
  refused on their own uncancelled magnitude — `Add` now gets the same
  structural cancellation `Mul` already has via `_factor_multiset`,
  which also fixed a latent bug in how a bare `-1`/`1` Integer encoded
  its sign in that multiset (a spurious `{1: 1}` entry broke exact
  structural-equality matching between a term and its own negation).
  A sixth review round (grok, reviewing the round-five head) found the
  round-five/six `Pow` short-circuits both stopped the scan from ever
  descending into an exponent that would itself be expensive to
  CONSTRUCT, as opposed to merely expensive to PRINT: a unit-magnitude
  base (`1`, `-1`, `1/1`, `2/2`, `1.0`) resolves to `(0, 0, True)` without
  ever inspecting the exponent at all, and a same-base cancelling
  exponent (`2**(2**N * 2**(-N))`) resolves to `(0, 0, True)` via
  `_factor_multiset`'s own cancellation — both cases leave a genuinely
  expensive inner `Pow(2, N)`/`Pow(3, -N)` (for a ~300-digit `N`, hangs
  past `guarded_call`'s CPU backstop) completely unchecked, since
  `Mul.flatten` still constructs that intermediate during real evaluation
  regardless of what the surrounding expression later does with the
  result. Closed not in the scan (an attempt to make the scan always push
  a resolved `Pow`'s children regardless of its own verdict regressed
  `1**(20 distinct factorial factors)`, previously legal: the scan would
  then independently judge the EXPONENT's own ~79,347-digit print
  profile as if it mattered, when it is cheap to construct — 20 already-
  materialized integers multiplied together — and never printed at all,
  since `1 ** anything` is always `1`; print-profile-over-cap and
  expensive-to-construct are different questions, and the scan only ever
  answered the first one correctly) but in `reject_explosive`'s separate
  Pow-only loop, which already walks every node via `_walk` — with no
  "stop descending" optimization at all — regardless of any Mul/Add
  ancestor's cancellation or unit-base status: it now bounds a numeric-
  base `Pow` with a bare integer exponent's own construction cost
  directly, closing the gap the scan's cancellation-aware "stop
  descending" rule correctly cannot close on its own. A second,
  independent review (Codex, reviewing the round-six head in parallel)
  found five more issues, none overlapping: (1) the identical class from
  the `Add` side — `_cancel_additive_inverses` marks `2**1000000000 -
  2**1000000000` resolved-to-zero and never descends into either `Pow`,
  but SymPy evaluates each child before cancelling, so the ~301-million-
  digit `Pow(2, 1000000000)` still gets constructed — verified already
  closed by the same Pow-loop fix above, which is agnostic to whether its
  ancestor is a `Mul` or an `Add`; added as a regression test rather than
  a code change. (2) a heavy call NESTED inside another heavy call's
  argument (`factorial(fibonacci(100))`, `fibonacci(factorial(10))`,
  `factorial(factorial(8))`) was uncaught: SymPy evaluates a heavy call
  on an integer-literal argument eagerly even under `evaluate=False`, so
  the INNER call's numeric result (not its short source text) becomes
  the OUTER call's argument before any tree exists for `reject_explosive`
  to inspect — no tree-level rule can run early enough; `_heavy_call_
  violation` now refuses outright, at the token level, any heavy call
  whose own argument span contains another heavy-function call token,
  regardless of either call's individual argument size. (3) `_oversized_
  scientific_literal_violation`'s regex had no allowance for Python's
  `j`/`J` imaginary suffix, so `1e1000000j` never matched at all and
  reached the real parse unbounded; separately, being an unanchored
  search, it misread the literal hex digits of `0x1e100000` as a fake
  "exponent of 100000". Fixed by allowing an optional trailing `j`/`J`
  before the anchor and excluding `0x`/`0o`/`0b`-prefixed tokens (which
  have no exponent syntax in any of those bases) up front. (4) an audit
  of every remaining eager name in `safe_global_dict()` turned up two
  more hazard shapes: `factorint`/`primefactors`/`divisors`/`mobius`/
  `nextprime`/`isprime` are not `_HEAVY_FUNCTIONS`-shaped at all (the
  hazard scales with the size of the NUMBER under test, not a growing
  OUTPUT for a small "count" argument — a random ~40-digit semiprime
  measured 27.8s to factor, and RSA-100 hits the process memory/CPU
  backstop outright) and now get their own digit-count cap
  (`_FACTOR_ARG_FUNCTIONS`, 25 digits); `sqrt`/`root`/`cbrt` are the
  opposite shape (cheap to bound in log space — an n-th root's digit
  count is always comfortably under `MAX_NUMERIC_DIGITS` — but not cheap
  to COMPUTE: SymPy's perfect-power check measured 4.0s at 1900 digits)
  and get a separate, more generous cap (`_ROOT_ARG_FUNCTIONS`, 1,200
  digits); `digamma`/`zeta` turned out to be `_HEAVY_FUNCTIONS`-shaped
  after all and were simply added to that set at the existing
  `MAX_HEAVY_ARG` cap; `stirling`, the audit's one open hypothesis, is
  not actually exposed by sympy 1.14's namespace at all, so there was
  nothing there to bound. Both new families are token-level-literal-only,
  same scope as the pre-existing `_heavy_call_violation` — a COMPUTED
  argument to any of these nine functions has no tree-level backstop yet
  (documented in `_oversized_factor_or_root_arg_violation`'s own
  docstring as a known gap, not an oversight). (5) `_cancel_additive_
  inverses` only recognized an EXACT multiset match with opposite sign,
  so `2*factorial(1463)*factorial(1463) - factorial(1463)*factorial(1463)
  - factorial(1463)*factorial(1463)` (exactly `0`) was still refused —
  the leading `2` makes the first term's multiset a different key from
  the other two's. Fixed by splitting each term into a small-integer
  `coeff` (`_split_coefficient`, folding only a base/exponent pair small
  enough on both axes to be a real literal multiplier, never a heavy-
  function result) and a `rest` key used for grouping instead of the raw
  multiset, then summing signed coefficients per group; a group's
  coefficients summing to exactly zero drops every term in it, any other
  net sum leaves every term in that group untouched (never replaced by
  one combined term), so a genuinely over-cap residual
  (`3*factorial(1463)*factorial(1463) - factorial(1463)*factorial(1463) -
  factorial(1463)*factorial(1463)`) still falls through to the
  unmodified per-term upper bound and is correctly refused.
  An eighth review round (grok PASS with two Low notes; Codex FAIL with
  one High and one Low, both on the round-seven head) closed the last
  three: (High, Codex) round seven's new per-function caps (the 25-digit
  factoring family, the 1,200-digit root family) were enforced ONLY per
  numeric TOKEN — `factorint(<25-digit literal>*<25-digit literal>)` (two
  individually-permitted literals whose PRODUCT is ~50 digits, hard to
  factor) and `sqrt(<990-digit literal>*<990-digit literal>)` (product
  ~1980 digits, 2.1s to compute) both passed `classify_unsafe` clean —
  the same "new caps added to one layer" pattern as before, just one
  layer over. Fixed structurally rather than patched a third time: ONE
  table (`_FUNCTION_ARG_CAPS`, name -> `(cap_kind, cap_value)`) now
  drives every enforcement site in the module — the token screen's
  per-literal check, a NEW token-screen check that sums every literal's
  digit count within one top-level ARGUMENT (an upper bound on their
  product, closing both repros above at the token level), the tree-level
  `Function`-node check in `_numeric_ceiling_scan` (generalized from
  `_HEAVY_FUNCTIONS` alone), and a NEW tree-level `Pow`-loop branch for a
  unit-fraction exponent (`1/2`, `1/3`, ...) on a numeric base — the
  actual tree shape `sqrt`/`cbrt` compile to, since neither ever appears
  as a `Function` node named after itself; `root` shares the token-level
  path instead, since its own construction was found (live) to take a
  different, more eager code path than `sqrt`/`cbrt` for at least one
  concrete-base case. (Low, Codex) `_approx_decimal_digits()` truncates a
  float `log10` and overcounts at an exact power-of-ten boundary — a
  25-digit all-9s literal read as 26 digits, a 1,200-digit one as 1,201 —
  and the digit-cap enforcement decision used that approximation
  directly. Fixed with a new `_exact_decimal_digit_count`: the EXACT
  digit count of a plain decimal literal is just its token string length
  (stripped of underscores and leading zeros), cheap and exact, with the
  log-based approximation kept only for a hex/octal/binary literal (whose
  character count bears no relation to its decimal value) and for prose.
  (Low, grok) two stale docstring passages rewritten to describe current
  behaviour: `_log10_num_den`'s compound-`Pow` case no longer claims a
  flat `(0.0, 0.0, False)` (it resolves a unit-magnitude base to
  `(0.0, 0.0, True)` without ever reading the exponent, and otherwise
  scales the base by an upper bound on the exponent's magnitude); the
  scan's own comment no longer claims `_log10_num_den` "never even looked
  at the base" for a non-Integer exponent (the compound-`Pow` branch
  loads the base first, for its own unit-base check, before the "push
  both children" case this comment explains is even reached).
  A ninth review round (grok, PASS with no remaining High — three Low
  notes) closed the last three: a docstring overclaimed that `sqrt`,
  `root`, and `cbrt` alike "DO respect `evaluate=False`" — true for
  `sqrt`/`cbrt`, false for `root` (probed live: `root(factorial(1463),
  2)` parses to a `Mul`, not a `Pow`, a more eager construction path —
  the docstring now says so and scopes `root` to the token-level layer
  explicitly, matching its own test and the round-8 entry above); the
  table did not yet drive EVERY comparison — the `Function`-node "value"
  check still read a `_log10_max_heavy_arg` derived from the bare
  `MAX_HEAVY_ARG` module constant rather than the matched row's own
  `cap`, and the Pow loop's unit-fraction branch hardcoded `MAX_ROOT_
  ARG_DIGITS` directly — both now read from `_FUNCTION_ARG_CAPS` itself
  (a monkeypatched row with a deliberately different cap, in a new
  self-check test, proves both layers actually follow the table); and
  the summed-digit token rule's refusal message now says "the sum of the
  literal digits", explicitly, since it is a safe UPPER bound on a
  product's true digit count, not the exact product — a small extra
  literal factor can tip the sum over the cap even when the true product
  still fits (`factorint(<25-digit literal>*2)`, accepted as documented
  conservatism, not a bug).
  A tenth review round (Codex, confirming the round 8-9 delta) verified
  every prior claim and returned one remaining Low (its one Medium,
  `isprime(10**24+7)` rejected as "not an integer", behaves identically
  on `main` and is pre-existing — tracked separately as THE-1095, not
  touched here): round 8 made the TOKEN-level digit-count check exact,
  but the TREE-level check (fed by `_log10_of_int`'s float
  approximation) still rounded the wrong way at an EXACT boundary —
  `safe_parse("sqrt(" + "9"*1200 + ")")`, a value with EXACTLY 1200
  digits at `MAX_ROOT_ARG_DIGITS`, passed the token screen but was then
  refused at the tree level, where the float log10 of an all-9s value
  rounded up and reported 1201 digits instead of 1200. Fixed with
  `_exact_digit_count_if_cheap`/`_digit_count_over_cap_for_node`,
  preferring the exact digit count of an already-materialized
  `Integer`/`Rational` over the float approximation (a `Mul`/`Pow` chain
  still uses the float path, unchanged — reconstructing it into a
  concrete value just to count digits could itself be the expensive
  operation this file exists to avoid). While there: the unit-fraction
  root refusal message said "the base of a 2-th root"/"3-th root" —
  now says "square root"/"cube root"/"n-th root" properly.

## [0.12.0] — 2026-09-09

### Removed

- **`bit_analysis`, `bitop`, `int_widths`, `base_repr`, `solve_expression`,
  `solve_linear`, `simplify_expression`, `limit_expression`** — the eight
  tools 0.11.0 deprecated as one-release aliases for `bits(mode=...)`/
  `symbolic(op=...)` are now removed, completing that deprecation:
  - `bit_analysis` -> `bits(mode="analysis")`
  - `bitop` -> `bits(mode="op")`
  - `int_widths` -> `bits(mode="widths")`
  - `base_repr` -> `bits(mode="repr")`
  - `solve_expression` -> `symbolic(op="solve")`
  - `solve_linear` -> `symbolic(op="solve_linear")`
  - `simplify_expression` -> `symbolic(op="simplify")`
  - `limit_expression` -> `symbolic(op="limit")`

  Calling one of the eight old names now gets the MCP SDK's own unknown-tool
  result, not a result shaped like the tool used to return. The served
  surface moves from 57 to **49 tools**.

### Changed

- **Every tool parameter now carries a schema `description`** — all 152
  parameters across the 49 served tools, via `Annotated[<type>,
  Field(description=...)]`, so a client that renders `inputSchema` (or an
  LLM tool-selector that reads it) sees per-argument guidance instead of a
  bare name/type. No parameter's type, default, or name changed. `Literal[...]`
  enums and `Field(ge=/le=)` ranges were deliberately NOT added anywhere in
  this pass: measured against the running server, both turn a closed-set
  or out-of-range argument's existing graceful `{"ok": false, "error":
  ...}` result into a hard MCP protocol-level tool-call error instead — a
  caller-visible behaviour change this pass rules out.
- **Tool DOCSTRINGS edited, in the same branch as the schema-description
  pass above, now that per-parameter syntax/default/range no longer needs
  restating in prose**: `execute_code`/`execute_code_stream`/`run_submit`/
  `session_run` each got a one-sentence "use X, not the other three, when
  Z" disambiguation (Glama's "all run code" review comment);
  `verify_optimization`'s docstring was cut to about 60% of its length
  with no loss of the two-gate/statistical-rule/grading content;
  `truth_table`/`session_files`/`algebraic_equiv`/`compare_threshold`/
  `percentage`/`percentiles` each gained a sibling-tool usage sentence;
  and sentences that only restated a parameter's schema description
  (an explicit default, range, or enumerated-values list already carried
  by `Field(description=...)`) were removed elsewhere in the file. Several
  of those removals had to be partly walked back after
  `scripts/tool_select_eval.py` showed they deleted vocabulary the BM25
  selector actually discriminates on (`bits(mode="op")`'s operator list,
  `percentiles`' rank-cutoff wording) — the eval's own module docstring
  warns a description that "reads clearly to a human" can still score
  badly if it loses the words a prompt matches on, and that is exactly
  what happened here first. `docker/mcp-catalog/tools.json` was
  regenerated from the live server for both passes, so every argument's
  `desc` is filled and every changed tool `description` matches, with the
  same `optional: true` flags and argument order the file already carried.
  The served `tools/list` JSON went 64,643 bytes / 17,277 tokens
  (o200k_base) at the start of this branch, to 78,586 bytes / 20,630
  tokens after the schema-description pass alone, to **77,237 bytes /
  20,439 tokens** after the docstring pass on top of it — a net
  **-1,576 bytes / -412 tokens** from the schema-description peak, short
  of the ~19,000-token goal the docstring pass aimed for, because
  restoring the eval-flagged vocabulary gave back more than the rest of
  the trim saved; see README's "Tool-definition token cost" for the full
  accounting. `scripts/data/tool_select_baseline.json` was regenerated:
  `full`/`dev`/`core` move from 151/234, 128/184, 79/116 to 150/234,
  130/184, 78/116 top-1 hits — every surface within the eval's own
  `DEFAULT_EPSILON_HITS` of its `main` baseline, dev actually improved.

## [0.11.0] — 2026-09-09

### Added

- **`session_snapshot(session_id, action="save"|"restore"|"list"|"delete", snapshot_id=None, label=None, replace=False)`** — archive a session's workspace files to a `tar.gz` and restore one later, into a new session or back into the same one. One tool with an `action` parameter rather than four separate tools, to keep the 53-tool surface from growing by three at once for one feature; `action`'s four shapes are close enough in signature (`session_id` plus an optional `snapshot_id`/`label`/`replace`) that splitting them would mostly duplicate the same three parameters four times over.
  - `action="save"` applies the EXACT artifact rules `session_artifacts` already uses (`.codecalc-run/`, `.codecalc-spill/`, and the session lock file excluded) plus one more: a hardlinked file is excluded too, because — unlike a symlink, which `_workspace_scan` already drops before this ever runs — a hardlink is `S_ISREG` and would otherwise be archived, silently smuggling the bytes of whatever it aliases outside the workspace into a snapshot a caller can later restore anywhere. The archive is written OUTSIDE the jailed session workspace, in its own subtree of the sessions root (`.codecalc-snapshots/<session_id>/`), specifically so a session's own sandboxed program can never read, tamper with, or delete an archive of its own past state through the ordinary workspace path. Identity-checked before anything is read (`_dir_identity`, the same check `session_stop` already applies before deleting a workspace) — a session whose directory was swapped out from under it (the same rename-swap attack `tests/test_python_sweep.py`/`tests/test_execution_service.py` already regression-test for `stop()`) is refused rather than archived.
  - `action="restore"` extracts through a safe extractor that refuses, before a single byte of ANY member is written: an absolute path, a `..` component, a symlink or hardlink member, a device/FIFO/socket member, a member over the existing per-artifact byte cap (`CODECALC_MAX_ARTIFACT_BYTES`), and an archive over the existing per-session artifact-count cap (`CODECALC_MAX_ARTIFACT_COUNT`) — reusing both existing caps rather than inventing new ones for the restore path specifically. Every member is written with `O_EXCL | O_NOFOLLOW`, so a hostile archive can never overwrite or follow an existing file. Without `replace=True`, restores into a brand-new session (same language the snapshot was saved from) and returns that session's own `session_start` shape plus `restored_files`/`bytes`; with `replace=True`, wipes and recreates the SAME session's workspace first (identity-checked the same way `save` is) and, for a stateful python3/node session, kills and respawns its REPL worker — restoring FILES only, never REPL variables/imports, which nothing in this module keeps around once a worker process exits.
  - Two new caps, env-configurable like the existing session disk quotas: `CODECALC_MAX_SNAPSHOT_BYTES` (default 256 MiB) bounds one archive's raw content size, and `CODECALC_MAX_SNAPSHOTS_PER_SESSION` (default 10) bounds how many a session accumulates — both because a snapshot lives OUTSIDE any session's own disk quota, so nothing existing would otherwise bound it.
  - Lifecycle: `session_stop` now also deletes every snapshot the session ever saved, unless called with the new `keep_snapshots=True` — the simplest lifecycle with no separate "orphaned snapshot" state to reason about, since a snapshot only ever existed because its origin session did. `session_stop`'s result carries a new `snapshots_deleted` count when it did so.
  - `CONTRACT_VERSION` bumped `1.14.0` -> `1.15.0` (MINOR): adds the `session_snapshot_result` shape (discriminated by the new `action` field, which collides with no existing shape) — purely additive, no existing tool's result changed shape or meaning.
- **`trace_execution`** (53rd MCP tool, `execution` group): line-level execution
  tracing for python3 — answers "which lines ran, in what order, and why did
  this input produce that output", where `execute_code` only answers "what did
  it print". Runs the submitted code through the SAME sandboxed executor
  `execute_code` uses (Rust with the pure-Python fallback, both backends,
  identical `stdout`/`stderr`/`exit_code`/`verdict`/timing) via a generated
  harness that installs `sys.settrace`, execs the user's source from a
  sibling file written at the run's workdir root, and streams JSON-lines
  trace events into `.codecalc-run/` — the same scratch-directory-write/read-
  after/caller-deletes pattern `execute_code_stream` already uses for
  `run.out` (see `codecalc/tracing.py`'s module docstring). Returns the
  standard envelope PLUS `events` (ordered `{step, line, event, func,
  locals}` for user-code frames only, `event` one of line/call/return/
  exception, `locals` holding only the names that CHANGED since the frame's
  previous event, each repr capped at ~200 chars; `return` events also carry
  `return_value`, `exception` events carry `exception_type`/
  `exception_message`), `branches` (hit count per `if`/`elif`/`while`/`for`/
  `try` line from a static AST parse), `lines_executed`/
  `lines_never_executed`, and `truncated`/`truncated_reason` (`max_events` or
  an internal trace-byte ceiling — RECORDING stops, the program always runs
  to completion, so `stdout`/`exit_code`/`verdict` are always the real,
  complete ones even when `events` is partial). A CPython trace-protocol
  quirk — a spurious `return` event with `arg=None` fired for every frame an
  exception is UNWINDING through, indistinguishable read naively from a
  function that genuinely returned `None` — is tracked and suppressed, so a
  `return` in `events` always means the function actually returned.
  Python3-only in v1 (`sys.settrace` has no cross-language equivalent this
  package can drive uniformly); any other `language` is refused
  (`code: validation`, naming `execute_code` as the remedy) before anything
  is spawned. A non-`local` `provider` is refused the same way: the harness's
  own workdir-staging/reading contract only the local Rust/Python-fallback
  executor can satisfy. New result contract shape `execution_trace`
  (`docs/contract/README.md`, `CONTRACT_VERSION` `1.13.0` -> `1.14.0`, MINOR —
  additive), discriminated from the plain `execution_envelope` shape by a
  new `not: {required: [events]}` exclusion on that def (mirrors how
  `compact`/`rejected` already exclude `backend`/`verdict`), so `oneOf`'s
  "exactly one shape matches" claim still holds. `tests/test_trace_execution.py`
  covers both backends: event order/changed-locals/branch counts/
  `lines_never_executed` on an if/else+loop+function-call program, the
  exception-unwind suppression above, the `max_events` cap (program
  completes, trace is cut, `truncated_reason: "max_events"`), stdin
  passthrough, byte-for-byte `stdout`/`exit_code`/`verdict` parity against
  `execute_code` on three programs, `SyntaxError` parity (no `Traceback`
  header, matching CPython's own uncaught-syntax-error convention), a
  non-python refusal that spawns nothing (asserted via a tripwire on
  `executor.execute`, not by absence-of-observation), a wall-clock timeout
  (`verdict: TLE`, partial events, `truncated: false` — a sandbox kill is
  disclosed via `verdict`/`timed_out`, not this tool's own two recording
  caps), and no leaked `codecalc-exec` process after either a normal run or
  a timeout kill.

  **Hardened after cross-vendor review flagged the first cut DO NOT MERGE**,
  before this tool ever shipped:
  * **Trace-sink forgery / server-side amplification.** The traced program
    can derive its own trace file's path from `__file__` and write to it
    directly — the review reproduced a forged trailing `return` event kept
    last via `os._exit(0)` to skip the harness's own cleanup. Mitigated,
    not eliminated (in-process code sharing the traced program's own uid is
    not a boundary this package can construct — see `codecalc/tracing.py`'s
    "TRUST BOUNDARY" section): the parser now reads AT MOST
    `_MAX_TRACE_BYTES + 4 KiB` off disk (`os.open`/`os.read` in a bounded
    loop, never `Path.read_text()` of the whole file — a program appending
    megabytes cannot force this UNSANDBOXED parser into unbounded work;
    `truncated_reason: "trace_file_exceeded"` when the file on disk is
    bigger than that), every event is schema-validated (exact key set,
    correct types, `step` continuing the harness's own monotonic sequence —
    anything else is discarded into a new `discarded_events` count, never
    raised), and the harness now writes a final `{"event": "end", "step":
    N, "emitted": N}` line on every path it returns through normally — a
    new `events_consistent` result field is `false` whenever that line is
    missing, its count disagrees with what was actually accepted, or
    anything follows it, which an `os._exit` bypass cannot fake by
    definition.
  * **Threads.** `sys.settrace` is a per-thread hook; a second thread's
    frames were silently absent from `events` with no disclosure. The
    harness now checks `threading.active_count()` cheaply per `call` event
    and once more at exit, adding `"threads: only the main thread is
    traced"` to the result's own `unenforced` array the first time it
    observes more than one thread.
  * **`sys.modules['__main__']` leaked the harness's own module and temp
    file**, not the user's — `sys.modules['__main__'].__file__` showed this
    harness's internal path instead of matching `execute_code`. Fixed by
    installing a FRESH `__main__` module (the user's own `__file__`) before
    `exec()`-ing their code, restored afterward.
  * **Fallback-backend OLE `exit_code` race.** The pure-Python fallback's
    own output-cap enforcement (`executor._run_step`) polls a flag on a
    20ms timer, racing the traced child's natural exit — confirmed
    PRE-EXISTING and already nondeterministic for plain `execute_code` on
    this backend (the identical program's `exit_code` flips between `0`
    and a negative signal across repeated runs at sizes near the cap).
    `trace_execution`'s extra per-event file I/O shifts that race's timing
    enough to make its own `exit_code` on an OLE verdict, fallback backend
    only, unreliable to compare against `execute_code`'s — `verdict`/
    `output_truncated` are unaffected and always agree. Not fixed at the
    root (the race lives in shared executor code every tool depends on);
    disclosed instead via a new `"exit_code: fallback backend may differ
    on output-limit kills"` entry in `unenforced`, added only when
    `backend == "python"` and `verdict == "OLE"` — confirmed the Rust
    backend has no such race across dozens of trials.

  New result fields (`discarded_events`, `events_consistent`) and the third
  `truncated_reason` enum member (`trace_file_exceeded`) are declared in the
  `execution_trace` contract shape alongside the rest of it — still additive,
  landing before this tool's own first release.
- **`branch_reachability`** (`execution` group): decides,
  with z3, which `if`/`elif`/`else` arms and `while`/`for(range, static
  bounds)` loops in ONE python3 function can ever be taken — for ANY input,
  not the one you happened to try, which is what `trace_execution` already
  answers. Parses with the stdlib `ast`, builds each arm's path condition as
  the conjunction of every ancestor guard on the way to it (negated for an
  elif/else exactly the way Python's own `not` negates it, with an
  unconditional `return` on every arm of an earlier `if` cutting that path
  out of what reaches the code after it), and hands each condition to z3:
  `+ - * // %` on ints, `and`/`or`/`not` on bools, `== != < <= > >=`, `==`/
  `!=` plus `len()` on strings (`z3.Length`), and `abs`/`min`/`max` built
  from `z3.If` — the same solver-setup shape `z3_check` already uses.
  Returns, per branch, `line`/`kind`/`condition` (source text, negations
  spelled out) and `verdict` (`reachable`/`dead`/`unknown` — a solver
  timeout or an unsupported construct that slipped past the upfront scan
  on THIS path, never a crash), a `witness` input dict when reachable, and
  `boundary_inputs`: for each `Compare` in the arm's own guard with a
  numeric side, the MINIMUM and MAXIMUM satisfying value (z3 `Optimize`,
  no artificial box — an unbounded direction is detected straight from the
  `Optimize` handle's own `.lower()`/`.upper()`, `null` plus a `min_note`/
  `max_note` rather than a box edge quietly standing in for a real
  extremum) and the "equality edge" (a witness where the guard's own
  literal threshold is hit exactly) — every input dict shaped to drop
  straight into `compare_edge_cases`'s `test_inputs`. Top level adds
  `supported` (`inputs`/`dead_count`/`reachable_count`/`unknown_count`/
  `truncated`/`suggested_test_inputs` — a deduped pool of every witness and
  boundary input, ordered by line). An if/elif/else's arms are joined back
  at a real phi/`If`-merge on every variable either arm assigned — an arm
  that ends in an unconditional `return` contributes nothing to the merge
  (its path is closed), and a variable assigned in only some surviving
  arms is dropped rather than guessed at, so a later reference to it
  raises the same "undefined name" `unknown` a genuine `UnboundLocalError`
  path would. Loops use TWO mechanisms, chosen by iteration count (see
  `docs/design/2026-09-08-branch-reachability.md` for why one mechanism
  applied to both was unsound, not merely imprecise, confirmed by direct
  execution): a `for x in range(<static>)` loop of at most 32 iterations
  is UNROLLED exactly — the body walked once per concrete value, threading
  the environment sequentially, so post-loop state (including an
  accumulator like `total = total + 1` run three times) is EXACT, and a
  branch inside the body is `reachable` if ANY unrolled iteration's own
  path condition is sat, `dead` only if EVERY one is. A `while` loop, or a
  `for` above that cap, keeps a ONE-iteration walk, but a branch inside it
  that is UNSAT on that one iteration is `unknown` — never `dead` — and
  every variable the body assigns anywhere is TAINTED afterward (rebound
  to a fresh, entirely unconstrained symbol); a later guard whose z3
  expression mentions a tainted symbol anywhere, including buried inside
  arithmetic, is `unknown` too, without ever being solved. `witness`es are
  never taken on faith regardless — every `verdict:
  reachable` in the test suite is corroborated by actually running the
  program through `tracing.execute_trace` on its witness and checking the
  branch's own line fired. Refuses up front, before ever calling z3, the
  unsupported-construct scan running BEFORE any function-count/`inputs`
  structural check (so a module-scope `class`, for instance, is refused
  naming `class definition` and its line, not a generic "no function
  found" message), naming the
  construct and its line: floats/`None`/bytes/complex literals, attribute
  access, f-strings, comprehensions, classes, `async`, `try`/`except`,
  imports, subscripts, chained or `is`/`in` comparisons, any call outside
  `abs`/`min`/`max`/`len`, a data-dependent loop bound, `//`/`%` by
  anything but a positive integer literal (z3's Euclidean division and
  Python's floor division disagree on the sign convention otherwise —
  measured, not assumed), and more than one top-level function; a
  non-python `language` is refused the same shape `trace_execution` uses.
  Every refusal names `trace_execution` as the remedy: run the concrete
  case instead. New result contract shape `branch_reachability`
  (`docs/contract/README.md`, `CONTRACT_VERSION` `1.15.0` -> `1.16.0`,
  MINOR — additive, the twelfth shape, `session_snapshot_result` above
  being the eleventh), discriminated from every execution shape by carrying neither `verdict`
  nor `backend`. `tests/test_branch_reachability.py` covers reachable/
  dead/unknown verdicts on an if/elif/else-plus-static-loop program, a
  dead branch (`x > 5 and x < 3`), every reachable witness verified by
  running it through `tracing.execute_trace` and checking the branch's own
  line actually executed, boundary inputs (including confirmed-unbounded
  `null`s) for `<`/`<=`/`>`/`>=`/`==`/`!=` guards, `str` equality and
  `len()` guards, `bool` inputs, early return cutting a later branch dead,
  every refusal case (asserting the `validation` code and the exact line,
  a module-scope `class` among them), the non-python refusal, the
  `max_branches` cap (`truncated: true`), a timeout landing on `unknown`
  rather than a crash, and — closing a cross-vendor review's two BLOCKER
  findings against the first cut (an assignment inside a non-returning
  if/elif/else arm, or a loop body, was silently discarded for the code
  after it, so a later branch's path condition never saw it) — the
  environment-merge fix itself: an assignment in the else arm only, in
  both arms with a later branch reachable ONLY via the else value, an
  elif chain assigning three different values each read back separately,
  an arm ending in `return` whose assignment must not leak, a variable
  introduced in only one arm producing `unknown` (not a false verdict) on
  a later reference, a `for range(3)` body assignment merged into a later
  `if`, and nested ifs assigning at two depths — every one of those
  witnesses is ALSO corroborated by `tracing.execute_trace` on the real
  program, not merely on the model. A SECOND, confirmation review found
  that same merge, applied to loops, was unsound — a `for` loop's
  variable being both "fresh" and "range-constrained" made a value that is
  only ever the LAST iteration's real value look, after the loop, like
  ANY value in the range: `x = -1; for i in range(0, 10): x = i; if x ==
  5: ...` reported the `if` reachable with witness `{}`, though `f()` is
  fully deterministic and `x` is always `9` there — checked by direct
  execution. Fixed by unrolling any `for` within a 32-iteration cap
  exactly and, above that cap or for `while`, tainting every loop-body-
  assigned variable to a fresh unconstrained symbol instead of merging it
  (see the loop mechanism described above); covered by the review's own
  exact repro (now `dead`) plus its positive control (`if x == 9`,
  reachable), a `range(0)` loop, an inner `if` reachable only at one
  specific unrolled iteration, a loop above the cap (a post-loop read is
  `unknown` with its reason; an in-body branch sat on the first checked
  iteration is still `reachable`), a `while` accumulator and a `while`
  whose inner branch is unsat on the first iteration (both `unknown`,
  never `dead`), `break` inside a loop (still refused, unchanged), and an
  unrolled loop nested inside an `if` arm together with an `if` nested
  inside an unrolled loop (phi composing correctly through unrolling in
  both directions) — every reachable witness trace-corroborated as above,
  every dead/unknown verdict checked against what the reason claims. The
  tool's own description went through two rounds of tuning against
  `scripts/tool_select_eval.py`'s BM25 corpus: the first fixed a regression
  it introduced on an EXISTING `trace_execution` prompt; a confirmation
  review found the fix's own emphatic wording then pulled two of `main`'s
  OTHER prompts (one `symbolic`, one `verify_translation`) onto
  `branch_reachability` instead, closed by avoiding vocabulary those two
  tools' own descriptions are built around (`formula`, `prove`/`proved`,
  `simplest`/`cleanest`, `rewrite`, `preserve`) rather than a further
  rewrite. `full` moved 137/234 (58.55%) on `main` -> 133/234 mid-fix ->
  142/234 (60.68%) final; `dev` 113/184 (61.41%) -> 111/184 -> 119/184
  (64.67%); `core` 74/116 (63.79%) unchanged throughout (neither tool is
  in that group). One `main` prompt ("What does the computer actually
  store in memory for the number 0.1?", `float_repr` vs `evaluate_
  expression`) stays flipped regardless of wording — a corpus-relative
  BM25 length-normalization artifact any 57th tool addition would trigger
  for SOME near-tied pair, not a defect this change introduced; see the
  design note's own measurement. A THIRD review pass confirmed everything
  above by direct execution and found one more BLOCKER of the same class:
  `_walk_for_unrolled` hand-threaded the environment across an unrolled
  `for`'s N concrete-value copies but fed every copy the SAME, unnarrowed
  path condition, discarding each copy's own `falls_through`/
  `continuation_cond` — `x = 0; for i in range(5): if i == 2: return
  100 \n if i == 4: y = 99` reported the post-loop `if y == 99` REACHABLE
  with a witness, when every real call returns `100` at `i == 2` and the
  loop body never reaches `i == 4` — checked by direct execution for
  `x` in `{0, 1, -5, 999}`. Fixed by feeding the N copies through the
  exact same sequential-statement machinery `_walk_block` already uses
  for two consecutive `if` statements — copy `k+1` starts from copy `k`'s
  own `continuation_cond`, and a copy whose own walk reports
  `falls_through=False` (a bare `return`) closes every later copy (still
  walked, so ITS OWN branches are discovered and correctly `dead`, never
  silently dropped) and the code after the whole loop — covered by the
  review's exact repro (post-loop `dead`, the `i == 4` arm `dead`, the
  `return 100` arm `reachable`), an input-dependent return (`if i == 2 and
  x > 0: return`) whose post-loop read is reachable only for `x <= 0`
  (asserted on the witness), a return at the first iteration (everything
  after `dead`), a return at the LAST iteration (post-loop `dead`, an
  earlier arm still `reachable`), and a return nested two `if`s deep —
  every reachable witness trace-corroborated as above.

  That third pass also required `tests/test_branch_reachability_
  differential.py`: a deterministic (fixed-seed) generator of small
  programs over the supported subset — sequential and nested `if`/`else`
  with comparisons and linear arithmetic, reassignment before and inside
  arms, conditional and unconditional early `return`, `for` over static
  ranges up to 6 (including `range(0)`), and a counter-bounded `while` —
  checked against GROUND TRUTH from exhaustive concrete execution (every
  int input in `[-12, 12]`, both params for a 2-arg program): every
  `reachable` witness is run for real and must actually execute that
  arm's own body line; every `dead` line must never execute for ANY input
  in the domain; `unknown` is allowed anywhere. `scripts/check_no_eval.py`
  only scans `codecalc/`, so ground truth runs the generated function
  in-process (`sys.settrace` scoped to its own code object) rather than
  through the sandboxed executor, keeping the full 150-program corpus
  under ten seconds. Multiplying two live variables (`y * y`, `x * y`)
  puts z3 in genuinely slow nonlinear arithmetic, and a generated `while`
  body could reassign its own bound counter, both hanging individual
  `analyze()` calls — the generator restricts `*` to a variable times a
  literal and keeps the loop counter out of its own body's assignment
  scope, so every generated program is fast AND provably terminating.

  Running this corpus caught TWO more real bugs — a fourth and fifth
  instance of the same "false `reachable` with a fabricated witness"
  class, in two mechanisms neither prior review had touched:

  1. `_mentions_tainted`'s DAG-dedup used Python's `id()` of each z3 AST
     wrapper as its "already visited" key. z3's Python bindings mint a
     FRESH wrapper object on every `.children()` call rather than
     interning one per underlying (hash-consed) node, so a wrapper could
     be garbage-collected and its `id()` reused by an unrelated LATER
     node within the SAME walk — a tainted symbol nested inside a
     `z3.If`'s second branch was then skipped as an already-"seen"
     duplicate of a completely different node, so a guard that genuinely
     depended on a tainted (loop-computed) value solved as an ordinary
     `reachable` with a real-looking witness instead of `unknown`.
     Reproduced with `if x < 0: while n < 2: x = ((x - x) - 1); n += 1
     \n else: if <guard mentioning x>: ... \n if y >= (x - 6): ...` — the
     last `if` came back `reachable` more often than not, non-
     deterministically, because `id()` reuse depends on GC timing.
     Fixed by keying the visited set on `node.get_id()` (z3's own
     hash-consing id, stable across every wrapper around the same node)
     instead.
  2. A tainted (or untranslatable) `if`/`elif`/`else` guard is, by
     design, an opaque pass-through: neither arm is walked, so this tool
     cannot tell whether one of them held an unconditional `return`. The
     existing code treated that as "falls through unconditionally,
     nothing changes" — sound for the ENVIRONMENT (nothing WAS applied),
     but not for CONTROL FLOW: a `return` inside either unwalked arm
     would close off everything after it, and the tool had no way to
     know it hadn't. `if x < 0: while n < 2: x = 5; n += 1 \n else: if
     <tainted-adjacent guard>: ... else: ... \n if y != -20: <reachable
     with a witness>` — the final `if` came back `reachable` with a
     witness that, run for real, hit an EARLIER unconditional `return`
     first and never got there. Fixed with a new ambient counter,
     `_unresolved_closure_depth` (alongside the existing
     `_conservative_loop_depth`), raised for every statement sequentially
     AFTER such a pass-through within the same block: `_decide` now
     reports `unknown` (not `reachable`) for a SAT result found under it
     — an UNSAT result still safely proves `dead`, since dropping a
     required conjunct only WIDENS what solves, so the narrower true
     condition being unsatisfiable follows from the wider one being
     unsatisfiable. The signal (`ctx._pending_closure_taint`, one-shot,
     read-and-cleared by `_take_closure_taint`) threads through
     `_walk_if`, `_walk_block`, `_walk_for_unrolled`, and
     `_walk_loop_conservative` exactly the way `falls_through`/
     `continuation_cond` already do, so it composes correctly through
     elif chains, nested ifs, and unrolled-loop copies without leaking
     into an unrelated sibling arm. `boundary_inputs`' own min/max/
     equality-edge witnesses are suppressed under the same condition, for
     the same reason. Both fixes are covered by the differential suite
     (which found them) and by the two repros above, added to
     `tests/test_branch_reachability.py` as standing regressions; the
     full 211-check hand-written suite and the 150-program differential
     corpus both pass, deterministically, on both execution backends.

  A FOURTH review pass confirmed all three prior rounds' fixes by direct
  execution — including that `dead` downstream of an unresolved-closure
  point is correctly preserved — and found ONE MORE instance of the same
  class, this time in `_walk_loop_conservative` (every `while`, and every
  `for` above the unroll cap): it hardcoded `falls_through = True`
  regardless of what the one-iteration body walk itself reported, per its
  own docstring's stated design ("does not attempt to reason about
  whether the body's own return closes off the loop"). `def f(x): if x
  == 1: n = 0; while n < 4: return 6 \n if x == 1: return 999 \n return
  0` reported the SECOND `if x == 1` reachable with witness `{x: 1}`, when
  `f(1)` returns `6` at the `while` and never gets there; the same shape
  with `for i in range(40): return 9` reproduced identically for the
  above-cap `for` path. The reviewer's own multi-seed sweep of THIS
  file's differential corpus (copied to scratch with `SEED` changed)
  failed at seed `111` (4 failures), `20260101` (6), and the shipped seed
  at `CORPUS_SIZE=500` (8+) — the shipped seed at its shipped size simply
  never happened to generate the triggering shape.

  Fixed by USING the one-iteration body walk's own `(falls_through,
  continuation_cond)` instead of discarding it, in three cases: (a) the
  body does not fall through AT ALL on the one modeled iteration — every
  path through it, for any input that reaches it, returns — which is
  EXACT, not a widening: entering the loop at all means returning, so the
  post-loop continuation becomes `cur_cond AND NOT(entry_guard)`, and
  branches inside the body keep whatever verdicts the walk already gave
  them; (b) the body falls through this one iteration but contains a
  `return` SOMEWHERE (checked structurally, `_contains_return`, nested
  included) — some OTHER, unmodeled iteration might take it, so
  `ctx._unresolved_closure_depth` (the same mechanism the third review's
  fix introduced) is raised for everything sequentially after the loop
  (`sat` downgrades to `unknown`, `unsat` still safely proves `dead`),
  and the one-iteration walk's own `continuation_cond` — a real necessary
  condition for falling through that first iteration — is conjoined
  rather than discarded for bare `cur_cond`; (c) the body contains no
  `return` at all — unchanged, falls through, only VALUE taint applies.
  `while`/`for`-`else` was already refused by name upfront (`while/else`,
  `for/else`), so no separate handling was needed there. Covered by the
  two repros above (now standing regressions), a `while` whose body
  returns only under an input-dependent condition followed by a
  post-loop branch that is correctly `unknown` (never a fabricated
  `reachable`) alongside a SEPARATE post-loop branch that is truly dead
  and correctly stays `dead`, the same pair for a `for` above the cap, a
  `while` nested inside an unrolled `for` whose body returns
  unconditionally (every outer copy closes, post-loop `dead`), and the
  exact SEED=111 program the reviewer's sweep found, recovered by
  rerunning that seed's generator against the pre-fix code and taking the
  first failing program verbatim.

  The differential corpus itself was hardened per the review: `SEED` and
  `CORPUS_SIZE` are now overridable via `CODECALC_DIFF_SEED`/
  `CODECALC_DIFF_CORPUS` env vars (shipped defaults unchanged, seed
  printed at start) for ad hoc multi-seed sweeps without editing the
  file, and the generator now also produces `for` loops ABOVE the unroll
  cap (previously never generated — the exact gap this BLOCKER lived in),
  returns biased directly into loop bodies (bare and `if`-guarded, since
  that specific combination is what this bug needed), and nested loops
  (one `for`/`while` inside another's body, budgeted to keep compounding
  bounded). Verified before push with seeds `20260908` (shipped), `111`,
  `20260101`, `999999`, `4242` at `CORPUS_SIZE=150`, plus the shipped seed
  at `CORPUS_SIZE=500` — all six green (see the design note for the exact
  counts). Full hand-written suite: 239 checks (was 211).
- MCP Apps (`io.modelcontextprotocol/ui`) graphical views for
  `verify_translation` and `verify_optimization`: each tool now carries
  `_meta.ui.resourceUri` pointing at a self-contained `ui://` HTML resource
  (inline CSS/JS, no external assets, no network) that a supporting host
  (Claude, ChatGPT, VS Code, and others per the ext-apps spec's own host
  list) renders alongside the tool's answer — a per-case diff table with
  first-differing-line highlighting for the translation proof, and a
  per-size before/after timing chart plus the significance table for the
  optimization proof. `tools/call` is unchanged for hosts without Apps
  support — the `_meta` key is additive and ignorable — confirmed once
  during development by byte-comparing a live run of `main`'s server, and
  guarded going forward by a live in-process equivalence check plus a
  structural diff against `origin/main`'s merge-base (best-effort: only
  where `origin/main` is fetchable, which the CI job that runs it is not
  currently guaranteed to be). See
  `docs/design/2026-09-08-mcp-apps-verification-views.md` for the spec
  research this was built from.
- `serve-http --oauth-issuer` (or `CODECALC_OAUTH_ISSUER`): optional, off by
  default, JWT bearer-token validation as an alternative to the static
  `CODECALC_HTTP_TOKEN`. Given an issuer, tokens are verified as JWTs
  (RS256/ES256) against that issuer's JWKS — discovered once from
  `<issuer>/.well-known/openid-configuration`, or pinned with
  `--oauth-jwks-url` — checking issuer, audience (`--oauth-audience`,
  defaulting to this server's own resource URL), expiry, not-before, and
  optionally required scopes (`--oauth-scopes`). The server also publishes
  RFC 9728 Protected Resource Metadata at
  `/.well-known/oauth-protected-resource/mcp` and returns
  `WWW-Authenticate: Bearer resource_metadata="..."` on an unauthenticated or
  invalid request, per the MCP authorization spec. The static-token path is
  unchanged when no issuer is configured; if both end up set, the issuer wins
  and the static token is rejected, with a startup warning naming both. The
  issuer and JWKS URLs must be `https://` unless the host is loopback, and
  the OAuth verifier is built ONLY inside `serve-http`'s own startup path —
  never at module import — so `CODECALC_OAUTH_ISSUER` set in the environment
  costs `doctor`, `--help`, `serve-strict`, and the bare stdio server no
  network call; `serve-http` itself fails closed with a clear stderr message
  if the configured issuer cannot be reached or is not HTTPS off loopback.
- `llms.txt` at the repo root (the [llmstxt.org](https://llmstxt.org/)
  convention) indexing README, QUICKSTART, the result contract docs and
  schemas, SECURITY.md, AUDIT.md, CONTRIBUTING.md, and the packaged skill, so
  an LLM client can find codecalc's own docs without crawling the repo.
  `scripts/build_llms_full.py` builds `llms-full.txt` (the full text of
  everything `llms.txt` links) from that index; it is not committed —
  instead `.github/workflows/release.yml`'s `release-assets` job builds it
  fresh and attaches it to each GitHub Release next to the SBOM, and
  `llms.txt`'s own entry for it points at the stable
  `releases/latest/download/llms-full.txt` URL. `scripts/check_llms_txt.py`
  gates the index offline: every linked path must exist, every section must
  carry at least one verifiable link, and the generator must still run
  cleanly end to end. README.md gained a "Where to find codecalc" table
  (PyPI, crates.io, GitHub Releases, the MCP registry, Smithery, Glama,
  MCPB) and `docs/distribution.md` records the submission steps for the two
  directories that do not list codecalc yet (PulseMCP, mcp.so).
- `docker/mcp-catalog/server.yaml` (+ `tools.json`, `readme.md`): a prepared
  submission for the [Docker MCP Catalog](https://hub.docker.com/mcp),
  targeting `docker/mcp-server.Dockerfile` (already shipped) as a
  Docker-built image (`mcp/codecalc`) so it gets Docker's own signatures,
  SBOM, provenance and Docker Desktop listing rather than a self-hosted
  image. `tools.json` is the live `tools/list` response from that exact
  image (52 tools — `CODECALC_TOOLS` is unset in the image, which registers
  every group; the symbolic-extra tools among them already report "extra
  not installed" rather than erroring, per the image's existing documented
  tradeoff), so the registry's own `build --tools` reads it instead of
  spinning up the container. Validated against the registry's own
  `task validate`/`task build --tools` tooling — see `docs/distribution.md`
  for the exact submission steps; this commit does not open that PR.
- **`bits(mode=...)` and `symbolic(op=...)`**, replacing the two lexically-
  overlapping clusters `docs/design/2026-08-10-tool-facade.md`'s "Scope
  amendment on tool count" flagged for a same-signature-union merge:
  `bits` folds `bit_analysis`/`bitop`/`int_widths`/`base_repr` behind
  `mode="analysis"/"op"/"widths"/"repr"`, and `symbolic` folds
  `solve_expression`/`solve_linear`/`simplify_expression`/`limit_expression`
  behind `op="solve"/"solve_linear"/"simplify"/"limit"`. Each mode/op takes
  the union of its four predecessors' parameters (documented per mode as
  "used by mode X"), returns EXACTLY that predecessor's own result shape
  plus one additive key (`mode` on `bits`, `op` on `symbolic`), and rejects
  a mismatched parameter combination with a closed-enum `validation` error
  naming the missing or extra field — not the generic
  `call_capability(name, args)` facade that design document's §2 rejected:
  nothing here erases a per-operation schema, annotation, or approval
  boundary. See [Deprecated](#deprecated) below for the eight retired
  names, still registered as thin aliases for this release.
- **Argument completion (`completion/complete`) for `language`, `unit`,
  `provider`, `session_id` and `run_id`.** The 2026-07-28 wire only lets a
  completion request name a prompt or a resource template
  (`mcp_types.CompleteRequestParams.ref` has no `ref/tool` variant), and
  this server has one resource template and no prompts — so the new
  `@mcp.completion()` handler (`server.py`'s `_complete_argument`) dispatches
  on `argument.name` alone rather than on `ref`, and serves any of the five
  names regardless of which tool or template the request nominally targets.
  `language` completes registry keys plus every alias (`registry.py`);
  `unit` completes `units.list_units()`; `provider` completes
  `_provider_registry.descriptors()`'s ids; `session_id` completes live and
  on-disk sessions (`SessionService.list_sessions()`); `run_id` completes
  `RunSupervisor.known_run_ids()` (new — the alternative was server.py
  reaching into `RunSupervisor`'s private `_runs` table directly). Matching
  is prefix-only and case-sensitive, capped at the SDK's own 100-item
  ceiling on `Completion.values`, with `total`/`has_more` reporting the full
  match count and whether the cap actually dropped anything. A getter that
  raises (each reads live server state — sessions, the run supervisor, the
  provider registry) is caught and answered with an empty completion rather
  than surfacing a raw internal error to a client that only asked for
  completions.
- **`resources/list`-changed and resource-updated notifications on every
  tool that mutates a session's workspace.** `session_run`,
  `execute_code(session_id=...)`, `session_write_file`, `session_stop`
  (only when it actually removed a workspace — a second stop on an
  already-gone session is idempotent and stays silent), and
  `install_package(session_id=...)` each now fire one
  `ctx.notify_resources_changed()` (best effort) on success;
  `session_write_file` additionally fires `ctx.notify_resource_updated()`
  for the exact `codecalc://session/{session_id}/files/{path}` URI it just
  rewrote, since it is the one mutating tool here that names a single file
  rather than an unbounded set. Both are coroutines published onto a
  `subscriptions/listen` stream (2026-07-28, SEP-2575); every tool above is
  a plain synchronous `def` that the SDK runs on a worker thread, so the new
  `_notify_resources_changed`/`_notify_resource_updated` helpers bridge back
  to the event loop via `anyio.from_thread.run(...)` rather than making
  these tools async. `resources/list` itself still carries the existing 10s
  cache TTL (`cache_hints=` on the `MCPServer` construction) — a client
  refetching inside that window can still see stale content even though the
  notification arrived immediately. Documented in a code comment on each of
  the five tools above, not their docstrings: the docstring is each tool's
  served `description`, and `scripts/tool_select_eval.py` scores tool
  selection against it — measured, an earlier draft of this same paragraph
  in the docstrings cost 1-2 top-1 hits against the checked-in `full`/`dev`
  baselines (one flip: a CSV-save prompt started picking `session_read_file`
  over `session_write_file`) before it was moved out, the same fix already
  applied to the progress-notification comments below.
- **A server `icons` entry (2025-11-25+) and `website_url`.**
  `MCPServer(icons=[...], website_url=...)` carries one server-level icon (a
  monochrome, under-300-byte inline `data:image/svg+xml;base64,...` glyph)
  and a `website_url` pointing at this repository. Both are inline/self-
  contained data, never an external `src` — the same no-phone-home reasoning
  `tests/test_offline.py` already enforces elsewhere in this package. Both
  literals are written plainly (not string-split to dodge that test's
  outbound-URL scan); `tests/test_offline.py` instead gained an explicit,
  commented `_URL_EXEMPTIONS` entry for each — the SVG namespace declaration
  every standalone SVG carries, and this repository's own homepage — the
  same mechanism already used for example.com/localhost/127.0.0.1. Both
  ride on `initialize`, once per **connection** —
  measured before/after, `tools/list`'s served payload is byte-identical
  (59,902 bytes / 15,952 tokens, `o200k_base`, either way): **+0** tokens on
  the number README's "Tool-definition token cost" section exists to track.

  A per-GROUP `Tool.icons` entry on every tool was tried first, and pulled
  after measuring its real cost: `Tool.icons` is a per-TOOL field, so each
  of the 52 tools repeated its group's full base64 payload on the wire, and
  base64 tokenizes far worse than prose under a BPE encoder — **+6,665
  tokens** (`o200k_base`, +11,540 bytes) on the full served `tools/list`
  payload, on a server whose whole pitch (see README's "Reducing the tool
  surface" and `docs/design/2026-08-10-tool-facade.md`) is that tool
  SELECTION accuracy matters more than a marginal token saving elsewhere.
  Not an acceptable trade; removed before release.
- **Progress notifications on `benchmark`, `verify_optimization` and
  `compare_execution`**, the same `ctx.report_progress` mechanism
  `execute_code_stream` already used. `benchmark` reports once per
  requested size, during the first (non-rescaled) measurement pass only —
  an auto-scale retry is a distinct, unpredictable-length phase, and giving
  it its own 1..total sequence would stop the WHOLE call being monotone.
  `compare_execution` reports once per language, in `snippets`' own
  iteration order. `verify_optimization` reports once after each of four
  phases COMPLETES — correctness, baseline sizes, candidate sizes,
  alignment (`optimization.PROGRESS_PHASES`) — so a phase that fails
  reports nothing for itself, and the phases after it never ran. All three
  tools are plain synchronous `def`s that the SDK runs on a worker thread,
  so `tools.py`/`optimization.py` gained a synchronous `on_progress(done,
  total, message)` callback parameter (`tools.ProgressFn`) with no SDK
  dependency of its own; server.py's new `_sync_progress(ctx)` is the one
  place that bridges it to the async `ctx.report_progress` via
  `anyio.from_thread.run(...)`, the same pattern the resource-change
  notifications above use.
- `scripts/tool_select_llm_eval.py` — the MODEL-driven half of the
  tool-selection eval `scripts/tool_select_eval.py`'s own docstring says its
  BM25 selector "cannot tell you whether an actual LLM tool-selector would
  pick correctly." This calls a real chat model over an OpenAI-compatible
  `/chat/completions` endpoint (`CODECALC_EVAL_BASE_URL`/`CODECALC_EVAL_API_KEY`,
  env only — never a CLI argument, never logged), offering the exact tool
  catalog (name + description + `inputSchema`) an MCP client would see via
  `tools/list`, per `full`/`dev`/`core` group. Two calls per prompt (a real
  `tools=[...]` call for top-1, a ranked-list call for a STRICT top-3 — a
  hit iff LIST mode's own three names intersect `expected`, never unioned
  with the separate TOOLS-mode pick, which is reported on its own honest
  `top1_or_list_top3` column instead — with LIST mode's own #1 serving as
  the top-1 fallback when a model has no function-calling support), a
  resumable on-disk cache keyed by `(model, group, prompt, tools_hash)` —
  `tools_hash` is a hash of the exact `tools=[...]` payload, so an edited
  description or `inputSchema` invalidates the cache instead of silently
  replaying a stale response — and an advisory (non-blocking by default;
  `--strict` to fail) regression compare against a checked-in baseline that
  also warns when a group gets zero cache hits despite an existing baseline
  entry (a likely description change). See `docs/tool-selection-eval.md`
  for the measured numbers next to the BM25 baseline. A new
  `tool-select-llm-eval` CI job (`workflow_dispatch` only, gated on the
  `CODECALC_EVAL_API_KEY` secret) runs it live and uploads the JSON report.

### Deprecated

- **`bit_analysis`, `bitop`, `int_widths`, `base_repr`, `solve_expression`,
  `solve_linear`, `simplify_expression`, `limit_expression`** — replaced by
  `bits(mode=...)`/`symbolic(op=...)` above. Each alias is now a one-line
  delegation to its replacement with the mode/op preset; its own
  description is prefixed "Deprecated alias for `bits(mode=...)`/
  `symbolic(op=...)`; removed in the next minor release." Every alias keeps
  its own registered name, schema and annotations through this release —
  calling it by the old name is byte-identical to calling the replacement
  directly, tool timeouts included. Removed in **0.12.0**.

### Fixed

- `codecalc doctor --deep` demoted a `tested`-tier toolchain to `unhealthy`
  (flipping `healthy` false) whenever its version probe merely TIMED OUT,
  treating "the runner did not answer in time" the same as "the runner
  answered and is broken". Reproduced live four times on 2026-09-08 alone,
  on cold `windows-latest` hosted runners, main among them: three were
  `rustc --version` exceeding the 10-second probe deadline because a fresh
  runner's FIRST `rustc` invocation goes through the rustup proxy — a tiny
  arg-forwarding shim that has to locate and re-exec the real toolchain
  component before it can answer anything, a cost a warm process never
  pays again — and a fourth, on a later commit, was plain `go version`
  doing the same with no proxy involved at all. `_probe_version` used to
  return `hard_failure=True` for a timeout, identical to a genuine spawn
  failure, and `report()` trusted that the same way it trusts a nonzero
  exit on a command with a confirmed `_VERSION_FLAG` entry (both `rustc`
  and `go` are audited, GNU-style `--version`/`version` commands) — so a
  perfectly working toolchain read `unhealthy`, and the two real-host
  `--deep` assertions added in #282 for the go/lua/zig regression failed
  alongside it. A timeout now stays `hard_failure=False` and is never
  trusted as evidence on its own, for any command, confirmed flag or not:
  the row stays at `installed`/`available`, `probe_error` still records
  what happened, and it is disclosed under doctor's existing "installed,
  version probe failed" heading. The one thing that still demotes a row
  after a timeout is a `_HELLO` runtime whose own hello-world execution
  independently fails — arbitrated exactly as before, completely
  independent of how the version probe went. Two mitigations reduce how
  often the timeout fires at all, on top of the classification fix: one
  retry (a slow-start proxy is warm on its second call, so `rustc`/`go`
  alike usually answer well inside the deadline on the retry), and a
  raised per-attempt timeout (10s to 25s) for the specific toolchains
  audited as routing through this shape on a cold host — `rustc` (rustup),
  `dotnet` and `swift` (their own first-call driver discovery), `java` and
  `kotlinc` (cold JVM class loading and JIT warmup) — which is deliberately
  NOT the safety net: the retry and the non-demotion rule apply to every
  command whether or not it is in that table, which is what let `go`'s
  failure (no table entry, no proxy) get fixed by the same two changes. The
  report also now carries `probe_ms` on every row a `--deep` version probe
  was attempted for — the total wall time across every attempt, including a
  retried timeout — so a runner-speed flake is diagnosable from the report
  itself instead of only from a stack of identical `probe_error` strings
  with no way to tell "answered in 40ms" from "answered after 24s and a
  retry". `probe_ms` is a MINOR (additive) bump of the doctor schema.

- `verify_translation` and `compare_edge_cases` certified byte-different
  program output as equivalent. `translation._normalize` decided whether two
  programs agreed with `"\n".join(line.rstrip() for line in
  s.splitlines()).strip()` — and `str.splitlines()` treats far more than
  `\n` as a line boundary: VT (0x0B), FF (0x0C), FS/GS/RS (0x1C-0x1E), NEL
  (0x85), U+2028 LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR all counted,
  and the `"\n".join(...)` rewrote every one of them to a real `\n`. Almost
  no language treats those seven as line terminators on output, so a source
  printing one and a port printing an actual newline compared equal. Reported
  end to end (#286): a python3 program writing `A<U+2028>B` and a node port
  writing `A\nB` — two different byte streams — verified as `matched: 1,
  mismatched: 0`, graded `cross_checked`, the tool's strongest grade, whose
  own definition is "run and agreeing". They did not agree. The `stdout`
  shown in the result was also the NORMALIZED string on both sides, so a
  reader auditing the match had nothing to see — the difference had already
  been removed from the evidence along with the verdict. `compare_edge_cases`
  shares the same `_normalize`, so its divergence comparison and its own
  displayed `stdout` had the identical defect, one call downstream: its
  order/content divergence keys were ALSO built with `.splitlines()`, so an
  exotic separator would have re-folded into a line break at that second
  site even after `_normalize` alone was fixed — both are fixed together
  here. `_normalize` now tolerates exactly three things and nothing else:
  `\r\n`/`\r` folded to `\n` (the real cross-platform line-ending
  difference), trailing spaces/tabs per line (`rstrip(" \t")`, not a bare
  `rstrip()`, which strips the same exotic whitespace off the end of a
  line), and trailing blank lines at the end of the whole output — spelled
  out once as `translation.NORMALIZE_TOLERANCE` so there is a single place
  that says what "the same output" means here. Every per-case result (both
  tools) now also carries `stdout_raw`, the exact bytes a side wrote, next
  to the normalized `stdout` — so a match that folds a real, tolerated
  difference (CRLF vs LF, say) still leaves the raw bytes visible to a
  reader, which the normalized string and the verdict alone cannot do by
  design. `verify_optimization`'s correctness gate calls
  `translation.verify_translation()` directly and is fixed by the same
  change; `compare_execution` (`tools.py`) never normalized or compared
  stdout across languages at all (only truthiness, for its "vanished
  output" discrepancy check) and does not share the defect.

- `execute_code_stream` on the Rust backend could hang forever, with the
  sandboxed program already finished and its full output already sitting on
  disk. `executor.execute_stream`'s progress loop polled `proc.wait()` in a
  0.25s cycle and only drained stdout/stderr afterward, via
  `proc.communicate()` — but `wait()` never reads a byte, and the Rust
  binary's one write to that pipe is its entire final JSON result, output
  and all. Once #269's ceiling raise (64 KiB to 240 KiB) let a result
  exceed the OS pipe buffer (64 KiB, `F_GETPIPE_SZ`'s Linux default) or
  asyncio's own `StreamReader` backpressure limit (also 64 KiB), the child
  blocked inside `write()` with room in the pipe left unfilled — and
  nothing was ever going to unblock it, because the loop's own exit
  condition (`proc.returncode` becoming set) was exactly the write it was
  blocked on. Reproduced live on this host: a `--timeout 30
  --max-output-kb 240` run sat 18 minutes with no child process, state S,
  `wchan anon_pipe_write`, and a workdir whose `run.out` already held the
  full ~960 KiB the program had printed — the kind of hang two earlier
  reports described as "a stray codecalc-exec `--timeout 30` outlived its
  own timeout by >500s", because the executor's `--timeout` bounds only
  the sandboxed program, never this write-the-result phase. The exact
  983,040-byte shape's hang rate on the unpatched code is host/timing-
  dependent, not a fixed number: asyncio's backpressure pause only engages
  once the buffered amount crosses its 64 KiB limit, and how much of the
  JSON the Rust binary manages to write before the loop's first
  `proc.wait()` call decides whether that ever happens — a function of
  scheduling, not of the code path taken. Two independent 10-run batches
  on this host each hung 2 of 10 (4 of 20 total); an adversarial review
  pass, on this same host at a different time, reported 10 of 10 for the
  identical shape — both figures are real, and the gap between them is
  itself evidence of how narrow and timing-sensitive the race is. A larger
  5 MB result, well past the same 64 KiB thresholds either way, closed
  that gap: two independent 5-run batches both hung 5 of 5 (10 of 10
  total) on this host, and a raw `subprocess.Popen` with nothing reading
  its stdout at all hung on every run, 20 of 20. `execute_stream` now
  starts `proc.communicate()` as a background task BEFORE the progress loop
  begins polling `run.out`, so the pipe is drained continuously from the
  moment the child is spawned regardless of how large the eventual result
  is; the loop's own exit condition is now the drain task finishing, not a
  bare `wait()`. Every other `subprocess`/`create_subprocess_exec` call
  site in `codecalc/*.py` was audited for the same "wait before drain"
  shape and found to already drain concurrently: the non-streaming Rust
  path (`_execute_uncontracted`) uses `proc.communicate()` directly (drains
  internally via threads), the pure-Python fallback (`_run_step`) spawns
  dedicated drain threads before its poll loop starts, and the session
  REPL workers (`sessions.Worker`) read their response pipe with a blocking
  `readline()` that pulls bytes continuously rather than waiting on process
  exit — none of those needed a change. A Rust-side bound on the write
  phase itself (so an executor whose parent has wedged, rather than died,
  cannot sit in `anon_pipe_write` forever either) was considered and left
  as a follow-up rather than added here: a dead parent's read end closes on
  process exit, and neither `executor/src/platform/unix.rs` nor any other
  Rust source in this repo installs a custom `SIGPIPE` handler, so the
  existing "parent already gone" case already fails fast (`EPIPE`/default
  `SIGPIPE`) rather than hanging — only a parent that is alive but stuck
  elsewhere (a materially different bug) would still find this phase
  unbounded, and a watchdog thread precise enough to bound it without ever
  firing on a merely-slow drain is not a small enough change to land
  alongside this fix. Adversarial review also caught the same cleanup
  missing on a second, more realistic path: `except Exception` never
  catches `asyncio.CancelledError` (a `BaseException` since Python 3.8), so
  an MCP client cancelling `execute_code_stream` mid-run skipped the
  kill/cancel cleanup entirely — reproduced live as a `time.sleep(15)` run,
  cancelled after 1s, whose codecalc-exec was still alive 2.5s later with
  its workdir already deleted out from under it. That cleanup now lives in
  `finally` itself, guarded and ordered BEFORE the workdir is removed, so
  it runs on every exit from the stream — cancelled, failed, or
  successful — and the cancellation still propagates to the caller rather
  than coming back as a `stream failed:` result.

### Changed

- **Docs:** `docs/security/ostif-application.md` brought current with 0.10.0
  (version, size, sandbox tiers, the seccomp-bpf/sigstore/SBOM/ClusterFuzzLite
  work landed since the draft), a named-incident-class threat model, a
  prioritized audit scope, and a submission checklist with the OSTIF intake
  channel. `SECURITY.md` now says the application is submission-ready and
  pending submission.
- **`install_package` and `update_runtimes(apply=True)` now gate on a
  protocol-level confirmation instead of only the `anthropic/
  requiresUserInteraction` `_meta` hint.** That hint is read by exactly one
  client (Claude Code) and forces a permission prompt there; every other
  client, or that one with the hint stripped, previously called straight
  through with nothing asking whether the install/apply should happen. On a
  2026-07-28 connection the tool now returns the SDK's multi-round-trip
  `input_required` result on the first call, asking the caller to confirm
  (echoing back the package/language, or the languages being updated); the
  client's retry carries the answer, and the tool proceeds on `confirm: true`
  or refuses — `permission_denied` for a decline/cancel, `validation` for a
  missing or malformed answer — with no install or update attempted either
  way. On an older connection whose client declared the elicitation
  capability, the same question goes out as a standalone `elicitation/create`
  request instead. A client on an older connection that never declared
  elicitation is unaffected: the gate is skipped and behaviour is unchanged,
  `_meta` hint included — which is also why that hint stays on both tools
  rather than being replaced outright. Every outcome (confirmed, declined,
  cancelled, malformed, or skipped) is recorded in the audit log.
  `update_runtimes(apply=False)` — the default, dry-run form — is never
  gated, since nothing runs.
- **Tool descriptions now name the sibling a caller would otherwise confuse
  them with, and the MCP `instructions` string is a routing map instead of
  a 9-tool digest.** Glama's public review scored `human_duration` and
  `simplify_expression` lowest (2.4/5) and flagged two clusters a model
  cannot tell apart from description text alone: {`evaluate_expression`,
  `calc_exact`, `solve_expression`, `solve_linear`, `z3_check`} and
  {`bit_analysis`, `bitop`, `int_widths`, `base_repr`}. Every tool in both
  clusters now leads with "use X, not Y, when Z" naming the sibling and the
  discriminating condition (e.g. `calc_exact` for literal arithmetic with
  no symbols vs `evaluate_expression` for a symbolic expression;
  `bit_analysis` for facts about one value's bits vs `bitop` to apply an
  operation — not "combine two operands", the first cut's wording, which a
  cross-vendor review caught as false for `bitop`'s own unary `not`);
  `human_duration` and `simplify_expression` lead with the vocabulary an
  agent would actually search on instead of a noun-phrase summary.
  `scripts/tool_select_eval.py`'s checked-in baseline (the lexical proxy
  for whether a description still carries its discriminating vocabulary,
  regenerated with this change) does not regress on any of
  `full`/`dev`/`core` — all three measured better after the rewrite.
  Separately, `instructions` previously named only 9 of the (then) 52
  tools; it now maps every ACTIVE tool group (calculator, verification,
  execution, sessions, analysis, admin) to its member tools by intent,
  under ~1,800 characters, so a client that defers tool loading (tool
  search / progressive disclosure) has a starting point for the rest.
  "Active" is load-bearing: the first cut built this string once, at
  import, before `CODECALC_TOOLS` had decided which groups actually get
  registered, so a `core`/`dev` server advertised tools (`z3_check`,
  `execute_code`, whole session/admin groups) it never registers — caught
  by the same review. `instructions` is now built AFTER every tool has
  registered, filtered to `_ACTIVE_GROUPS`, with its "N tools in K groups"
  lead counted from the live registration, not a hand-typed number.
  `instructions` is not part of the tool-select eval's corpus — that eval
  scores only `"<name> <description>"` per tool from the live registry —
  so this is gated separately: every active group is named in
  `instructions` and no absent group or tool leaks in under
  `CODECALC_TOOLS=core`/`dev`, plus every cluster tool names a sibling,
  all three checked against constants `codecalc/server.py` declares rather
  than hand-copied lists that could drift from them.

## [0.10.0] — 2026-09-08

### Fixed

- `doctor --deep`'s version probe stored a failed probe's own stderr as the
  runtime's `version` and left `status` at `installed` — reproduced against
  Apple's `/usr/bin/java` stub on a macOS host with no JDK, which resolves on
  PATH and exits non-zero printing "The operation couldn't be completed.
  Unable to locate a Java Runtime." A version probe that never gets an
  answer at all (a spawn failure or a timeout) now demotes the row to
  `unhealthy`, counts in `runtime_summary.unhealthy`, and reports the
  failure under the new `probe_error` field — never under `version`. A
  NONZERO EXIT alone is trusted as evidence of brokenness only when the flag
  used is one this code has confirmed correct for that command (an explicit
  `_VERSION_FLAG` entry, as java's `-version` already was): `--version` is a
  GNU convention, not a universal one, and an earlier version of this fix
  trusted ANY nonzero exit, which reported go (`go --version` exits 2; `go
  version` is correct), lua (`lua --version` exits 1; `-v` is correct) and
  zig (`zig --version` exits 1; `zig version` is correct) `unhealthy` on a
  perfectly working host — go being `tested` tier, that also flipped
  `healthy: false`. All three now have confirmed `_VERSION_FLAG` entries;
  a command on the untested default that exits non-zero is reported as
  merely unmeasured (`probe_error` recorded, `status` untouched), and a
  language with a hello-world check (`_HELLO`) always has that run BEFORE
  the version probe gets a say, not after. `healthy` also goes `false` when
  a `tested`-tier runtime (python3/node/rust/go — the tier a CI job
  genuinely executes and checks on every PR) resolved and then proved
  broken via a TRUSTED probe failure; an uninstalled or broken
  `best_effort`/`plan_only` runtime, java and kotlin included, still leaves
  it untouched (#279).
- `kotlin` was registered with its resolution `command` set to `java` (the
  binary its `run` step invokes), so `doctor`/`list_languages` reported it
  `installed` from a JRE alone on any host with no Kotlin toolchain at all —
  `execute_code(language="kotlin")` then failed at spawn with a bare
  `exit_code -2`. `doctor`'s deciding command for a compile-then-run language
  is now always its compile tool (`kotlinc` for kotlin), and a plan whose
  `run` step needs a SECOND, different tool that the compile tool's
  resolution says nothing about is reported `installed` only when BOTH
  resolve — kotlin is the one instance of this in the registry today, and the
  check is derived mechanically from `compile`/`run` rather than
  hand-maintained, so a future language in the same shape is caught by
  construction. The Rust executor's spawn-failure message also now names the
  phase and the missing binary instead of a bare OS error — though that
  message still lands only in `stderr`, not in an `error` key, so
  `errors.ensure_code` still classifies a Rust-backend spawn failure as
  `internal` rather than `runtime_unavailable` (the Python fallback, whose
  envelope carries `error`, classifies it correctly); pre-existing, not
  fixed here, tracked separately (#280).
- Closing that gap: a Rust-backend spawn failure now sets an `error` key
  too (previously only `stderr`), worded to start "runtime unavailable for
  the ... phase: ..." so `errors.ensure_code` classifies it
  `runtime_unavailable` instead of `internal`, matching the pure-Python
  fallback's existing classification of the identical failure. `exit_code`
  is now `null` on both backends for this case (the fallback's existing
  convention — the prior Rust-only sentinel `-2` carried no meaning to a
  caller), and the Python fallback's mapping of a raw Rust result now also
  recognises an OLDER binary's stderr-only shape via a `"runtime
  unavailable"` prefix match, so a caller gets the same classification
  regardless of which binary answered (#280).
- **`tests/test_executor_sweep.py` and `tests/test_appcontainer.py` decided
  whether a native executor was present by checking `.exists()` on a
  hardcoded `bin/codecalc-exec` path, ignoring `CODECALC_EXEC_BIN`
  entirely.** A local run that pointed only `CODECALC_EXEC_BIN` at a binary
  built elsewhere (say `executor/target/release/codecalc-exec`, with `bin/`
  empty) silently skipped every "rust:" assertion with no SKIP line printed
  — the `if EXE.exists():` guard had no `else` branch — and setting
  `CODECALC_REQUIRE_NATIVE=1` alongside it did nothing to catch the
  mistake, since that variable was never consulted by the test's own
  hardcoded check. That is how a stale Rust-backend `exit_code` assertion
  passed locally and only failed in CI's sandbox job, which always builds
  straight into `bin/`. Both files now resolve the binary through a new
  shared helper, `tests/_helpers.resolve_native_executor`, which calls
  `codecalc/executor.py`'s own `_rust_binary()` (`CODECALC_EXEC_BIN` first,
  then the arch-aware `bin/` candidates) instead of duplicating that
  ordering — the two can no longer drift apart. It prints one loud `SKIP
  native executor: <reason>` line when nothing resolves, and raises instead
  of returning `None` when `CODECALC_REQUIRE_NATIVE=1` is set and nothing
  resolves, so a run that means to test the native backend cannot quietly
  fall through to skipping it. `.github/workflows/ci-python.yml`'s sandbox
  job now greps an existence floor for both files independently — `PASS
  rust:` for the first, the helper's own `native executor: <path>` line for
  the second, whose checks are not "rust:"-named — the same
  assert-from-outside-the-file pattern `test_features.py` uses (#277).
- A runtime with no confirmed `_VERSION_FLAG` entry that failed its version
  probe on the untested `--version` guess was disclosed in the JSON row only
  (`status: installed`, `probe_error` set) — the text renderer showed it
  under none of available/BROKEN/missing, and `_remedies()` said nothing
  about it either, so an operator reading `doctor` (rather than piping
  `--json`) never learned the probe had failed at all. `_VERSION_FLAG` now
  carries an explicit, audited entry for every registered runtime's command
  (`escript`/`tclsh` excluded — no non-interactive version flag exists to
  call), not just the five flag overrides, all confirmed live on this box
  with exit 0 on the default `--version` (audit table in the PR body) —
  leaving "unmeasured" the rare case of a genuinely new, not-yet-audited
  command rather than the common one. The trust rule itself is unchanged: a
  nonzero exit is still evidence of brokenness only for a command this code
  has confirmed the flag for. The text renderer now lists an
  installed-but-probe-failed row under its own "installed, version probe
  failed" heading, and `_remedies()` names it with the probe output,
  phrased as "the runtime may still work" so it is not overclaimed as
  broken. JSON shape unchanged — `remedies` was already `array of string`,
  and `probe_error` already existed on the row.

  Review caught that `awk`'s new confirmed-default entry was audited only
  against THIS box's GNU awk: `awk` is the one command name on the audited
  list that resolves to genuinely different, non-interoperable programs
  across hosts — BSD/macOS one-true-awk and busybox awk reject GNU long
  options like `--version` outright, which is exactly the shape #282 was
  written to fix, just for a command #282 never audited. `awk` is now also
  in `_HELLO` (`BEGIN{print "codecalc"}`, portable POSIX syntax) so a
  hello-world arbitrates its status BEFORE the version probe gets a say,
  regardless of which awk flavor answers; `sqlite3`/`jq`/`zsh` gained the
  same belt-and-suspenders treatment even though neither has a known
  divergent fork. Every other confirmed-default entry is a single-vendor,
  cross-platform-consistent CLI with `--version` documented in its own
  reference and is left without a hello-world backstop (per-command
  portability note in the PR body).
- `compare_execution` rebuilt each per-language row from a fixed field list
  (`language`, `ok`, `stdout`, `stderr`, `exit_code`, `duration_ms`,
  `timed_out`, `cold_retry`) and never carried `error`/`code`/`remedy` from
  the underlying `executor.execute()` result — a row for a language with no
  runtime installed on this host read `stderr: "runtime unavailable ..."`
  with nothing a caller could branch on, and the tool's OWN `ok` is `True`
  whenever the comparison ran (never "every language succeeded"), so
  `server.py`'s `_coded` wrapper (`errors.ensure_code`) never reached the
  rows either. A row that failed for a REQUEST-level reason — no runtime for
  that language, or a timeout that survived the one warm retry — now carries
  `error`/`code`/`remedy` via the new `errors.stamp_row`; an ordinary
  program failure (a real `RTE`/`OLE` `exit_code`) still carries none of the
  three, matching the INTENDED convention that `code` marks a failed
  request, not a failed program — the same convention `errors.ensure_code`
  now applies to the top-level `execute_code` envelope itself (see the
  Fixed entry below).
  `compare_edge_cases`'s per-language `runs[lang]` entries had the identical
  gap and are fixed the same way. `compare_execution`'s own result also
  never matched any branch
  in the published result contract — the same gap `edge_case_comparison`
  closed for `compare_edge_cases` in `1.5.0` — so contract `1.10.0` adds a
  `comparison_rows` branch for it; see `docs/contract/README.md`.
- `execute_code`/`execute_code_stream`/`run_inspect`/`session_run` (anything
  routed through `errors.ensure_code`, at `server.py`'s `_coded` wrapper)
  classified an ordinary program failure as `internal` — remedy "a defect
  in codecalc; the message is worth reporting verbatim" — whenever
  `executor.execute` set no `error` of its own: a plain `sys.exit(3)` (RTE)
  or a plain wall-clock timeout (TLE), on either backend, never set `error`
  at all (only `verdict`/`exit_code`/`timed_out`), so `ensure_code`'s
  message matcher had an empty string to classify and fell through to its
  fallback every time. A model reading that filed a bug for a program that
  did exactly what it was written to do. `docs/contract/README.md`'s own
  "The program ran and failed" example already documented the intended
  reading — a result with `verdict` present and no `error` is a failed
  PROGRAM, not a failed REQUEST, and gets no `code` at all — the same rule
  `errors.stamp_row` (above) already applied one level down, for
  `compare_execution`/`compare_edge_cases` rows. `ensure_code` now applies
  it at the envelope level too: a real `RTE`/`OLE`/`MLE` verdict with no
  `error` is left with no `code`, and a timeout (`verdict: "TLE"` or
  `timed_out: true`) is classified `timeout` instead — actionable (raise
  `timeout`) rather than a false defect report. A genuine unknown (`ok:
  false`, no `verdict`, no `error`) still falls to `internal`,
  `code_inferred: true`, unchanged. No schema or `CONTRACT_VERSION` change:
  `code`/`error`/`remedy`/`code_inferred` were already optional on the
  execution envelope, and the corrected behavior is what the published
  contract already documented — this fixes the code to match the docs, not
  the other way round. A COMPILE failure (`phase: "compile"`, e.g. a `c`/
  `rust` syntax error) follows the identical rule and is now a documented
  DECISION rather than an accident of the verdict-based gate also catching
  it: retrying the same request cannot succeed either way, so it stays
  codeless too — see `errors.ensure_code`'s docstring and
  `docs/contract/README.md`'s new compile-failure example. Neither backend
  emits a verdict distinct from an ordinary run's `RTE` for a compile
  failure; adding one to the closed 8-code enum (or a ninth `VERDICTS`
  entry) is a bigger, separate decision this fix does not make.
- On POSIX (Linux, macOS), the native (Rust) backend reported
  `exit_code: null` for a process killed BY A SIGNAL (a segfault, the OOM
  killer's `SIGKILL`, ...) — indistinguishable from one that never spawned
  at all, and disagreeing with the pure-Python fallback, which already used
  `subprocess.Popen`'s own convention: negative, the signal number (`-11`
  for `SIGSEGV` on Linux glibc; `-5` for `SIGTRAP` on macOS/Apple silicon
  for the identical null-deref — the exact number is platform-specific, the
  SIGN is not). `executor/src/main.rs`'s new `exit_code_json` aligns the
  native backend to the SAME convention the fallback already used on each
  OS — including Windows, which has no signals at all and was already
  correct on both backends: an access violation there is a real, POSITIVE
  exit code (the NTSTATUS itself, e.g. `3221225477` /
  `0xC0000005` for `STATUS_ACCESS_VIOLATION`), unaffected by this fix. A
  caller now tells "never ran" from "ran and was killed abnormally" from
  whether `exit_code` is `null` at all, on either backend, on any OS.
  `contract.py`'s `exit_code` description documents the convention per
  platform. No new field, no `code_inferred` involved: `exit_code`'s type
  (`integer | null`) does not change, only which integers a POSIX signal
  death can now produce on the native backend.
- The `exit_code: -N` fix directly above reopened `codecalc/executor.py`'s
  compat shim for pre-#283 binaries (which gates on the OLD wire format's
  literal `exit_code == -2` "nothing spawned" sentinel): SIGINT is signal
  2, so a program killed by SIGINT (`raise(SIGINT)` in C,
  `os.kill(os.getpid(), signal.SIGINT)` under python's default handler)
  now legitimately reports `exit_code: -2` too, and the shim could no
  longer tell the two apart from `exit_code` alone — a real, killed
  program came back `code: "runtime_unavailable"`, `error: "runtime
  unavailable for the run phase"`, `exit_code: null`, remedy "install the
  runtime". Found by cross-vendor review. Fixed with a THIRD, ALWAYS-
  present JSON key from the binary, `spawn_error` (`null` when nothing
  went wrong at spawn, the message otherwise — distinct from the existing
  `error`, which stays conditional and caller-facing): the shim now gates
  on `exit_code == -2` **AND** `"spawn_error" not in result` — a binary
  built after this fix carries the key on every result, so the shim can
  never fire against one regardless of what `exit_code` says; its absence
  is what proves the answering binary predates this fix. `spawn_error`
  itself is popped before a caller ever sees the result — internal to this
  one JSON handshake, never part of the published envelope, so no schema
  or `CONTRACT_VERSION` change. Popped on BOTH read paths: `execute_stream`
  (the `execute_code_stream` tool) reads the binary's JSON itself rather
  than through `_execute_uncontracted`, and the first cut of this fix left
  the key in that one surface's result (found by the adversarial review).
- The pure-Python fallback resolved a compile/run command against the
  sandbox's own PATH (`CODECALC_RUNTIME_PATH`) to decide whether it EXISTS,
  then still handed the bare name to `subprocess.Popen` — which on Windows
  lets `CreateProcess` search the CALLING process's PATH instead (Python's
  own docs: "env cannot override the PATH environment variable"). Measured
  on a hosted Windows runner: a fake `gcc` placed first on
  `CODECALC_RUNTIME_PATH` passed the existence check and the runner's own
  MinGW `gcc` ran anyway. The resolved absolute path is now what gets
  spawned, so the sandbox PATH decides which binary runs on every OS, not
  only the ones whose loader consults the child's environment. One
  observable side effect, fallback backend only: a runtime that exposes
  its raw `argv[0]` to the program (`process.argv0` under node/bun/deno;
  python/ruby/perl/bash discard it in favour of the script path) now sees
  the resolved absolute interpreter path rather than the bare name — the
  native backend already behaved this way.
- `execute_code(..., compact=True)` classified a "rejected before execution"
  failure (an unknown language, a validation refusal — anything with no
  `verdict`/`stdout`/`exit_code` to fall back on) as `internal` regardless
  of the real cause, while the identical call with `compact=False` correctly
  classified it: `compact_result` builds a FRESH dict naming only a fixed
  field list, which dropped `error` before `server.py`'s `_coded` wrapper
  ever got to run `errors.ensure_code` on it — by the time the classifier
  saw the result, the text it needed to classify FROM was already gone.
  `execute_code` now runs `ensure_code` BEFORE `compact_result`, and
  `code`/`error`/`remedy`/`code_inferred` are treated as non-droppable
  disclosure (same bucket as `unenforced`/`output_error`) so they survive
  the compaction that follows.
- **`verify_optimization` certified IDENTICAL before/after code as a verified
  speedup.** Reproduced live in CI (macOS sandbox job, native executor, main):
  `accepted=True` at a measured ratio of 1.21x, `sizes_rejecting` 2/3 —
  `_accept_decision`'s bare "a majority of sizes reject" rule let two
  false-positive per-size votes carry a three-size verdict outright. Both
  "rejecting" sizes were a tie routed to `stats.mann_whitney_u`'s normal
  approximation at `REPEATS=5` a side (p=0.023, p=0.047) — a sample size
  `stats.py`'s own module docstring says is "not a number worth calling
  alpha=0.05 against" — while the third size (p=0.898) plainly did not
  reject. Closed with five layered fixes in `optimization._infer_speedup`/
  `_accept_decision`, all measured, not just argued:
  - `_infer_speedup`'s per-size rows now carry `stats.mann_whitney_u`'s own
    `method` (`"exact"` / `"normal_approximation"`), previously computed and
    discarded, so a caller can tell which produced a given `p_value` (#285).
  - A size below `stats.min_testable_n(alpha)` observations a side (4 at the
    default alpha=0.05: the smallest n whose exact one-sided p can be < alpha
    at all, `1/C(2n,n)`) is structurally incapable of ever rejecting and no
    longer counts toward the vote it could never contribute a rejection to
    — excluded to `inference.sizes_below_floor` with a `reason` instead
    (replaces a flat `< 2` guard; #284).
  - A `normal_approximation`-method result on fewer than 8 runs a side is
    excluded the same way, but ONLY when the two samples' raw-run ranges
    overlap (`_ranges_overlap`) — the literal "the timer cannot always say
    which side a given pair of runs favoured" case, not merely "a tie
    happened somewhere." Gating on the tie alone (no overlap check) was
    measured LIVE as nearly blinding the tool: this tool's own integer-
    millisecond timings tie constantly even for an unambiguously fast,
    non-overlapping candidate (a real ~16x win's candidate arm produced
    `[42, 42, 45, 53, 42]` from ordinary process-spawn jitter), and excluding
    every tied result outright accepted a genuine win only 1 run in 10.
  - `_accept_decision` now requires EVERY counted size to reject when there
    are <= 3 of them (unanimity — the regime the CI incident's 3-size,
    2-rejecting case falls in, and enough on its own to have refused it),
    or a majority at a Bonferroni-corrected `alpha / k` when there are more
    than 3 (`_fwer_correction`) — a bare majority, always, is what let one
    false-positive size carry a three-size vote. And never fewer than two
    counted sizes at all: unanimity over ONE surviving size is a single
    uncorrected test at the nominal alpha, and adversarial review measured
    that road at a 0.357% false-accept rate on identical code (500,000-trial
    Monte Carlo, 97.6% of it through a lone survivor). A lone testable size
    now yields `accepted: false` with a `decision_basis` that says so and
    what to raise, not "verified faster" — which also means issue #284's
    two-size repro (one size structurally untestable) is now REJECTED for
    that stated reason rather than accepted on the one size that could
    reject; measuring one size fewer no longer flips the verdict either way.
  - A size counts as rejecting only when its OWN before/after ratio also
    clears `min_speedup`, not only its p-value — a size can be statistically
    significant on a difference too small to be the speedup the caller
    asked for.

  Measured before/after on this repo (identical code, 60 runs on the Rust
  backend under a 4-process CPU hog, the default and 3-size CI variant): see
  the PR body for the exact counts. The two live real-win regression tests
  in `tests/test_translation_verify.py` (a real O(n)->O(1) win and a
  calibrated O(n^2)->O(1) win, `nice -n 19` plus a saturating CPU load)
  still accept. Contract `1.11.0` adds `method`/`ratio` to
  `optimization_verification.inference.per_size`,
  `correction`/`effective_alpha` to `optimization_verification.inference`,
  and an optional `reason` to `inference.sizes_below_floor` entries excluded
  for one of the two new reasons above; see `docs/contract/README.md`.
## [0.9.0] — 2026-09-07

Headline changes since 0.8.0: the runner's own scratch files moved into a
private `.codecalc-run/` subdirectory on both backends, with symlink-safe
writes closing a cross-session overwrite before it shipped;
`verify_optimization` gained a per-size visibility floor, an asymmetric
comparability rule between the baseline and candidate, and one shared
measurement budget across every call it makes; the result contract moves
`1.5.0` → `1.7.0`; and `tests/test_features.py`'s async MCP section — most
of the file's coverage — is now actually executed.

### Fixed

- **`verify_optimization`'s auto-scale visibility floor was applied ACROSS
  ALL sizes combined, not per size** — `optimization._timed` broke out of
  its rescale loop as soon as `max(measured) >= 20ms or min(measured) >=
  5ms` over every tested size together, so once the LARGEST size cleared
  the floor, the loop stopped rescaling even though the SMALLEST sizes
  could still be sitting in timer noise. Real evidence from hosted macOS CI
  (main at `541528bb`'s `ci-python` sandbox job, and PR #270's): a genuine
  1.9-2.4x O(n)->O(1) speedup measured `n=200000` at p=0.65 and `n=400000`
  at p=0.058 (indistinguishable from noise) while the two largest sizes
  were always decisive (p<0.03) — the majority-of-sizes significance test
  (`_accept_decision`, #264) correctly called that "could be noise" and
  rejected a real win. #272 worked around this in the TEST's candidate pair
  only (giving it ~10x more work per element so even the smallest size
  cleared the old combined floor); this fixes the product. `_timed` now
  rescales each size INDEPENDENTLY until ITS OWN measurement clears the
  floor, keeping both arms on one ladder via `_align_sizes` (generalized
  to re-measure whichever side scaled less at each POSITION that still
  disagrees, not just when the two sides' whole size lists differ). A size
  excluded from the significance test for ANY reason — never clears the
  floor within the rescale budget (a genuinely O(1)-fast workload, for
  instance), is unmeasurable, or has too few comparable runs — is excluded
  from the majority vote and named in a new `inference.sizes_below_floor`
  field (`before_ms`/`after_ms` are `null` when no duration was ever
  recorded), so `len(sizes) == len(per_size) + len(sizes_below_floor)`
  always holds — never silently dropped from every field at once, which an
  earlier draft of this same fix did for a size an OLDER, narrower guard
  excluded first (caught in review, before merge: a size at or under the
  1ms noise floor `_speedup` already applies used to `continue` past the
  new disclosure entirely). `grade_verify_optimization`'s basis text
  discloses the exclusion when it happens.
  Verified live on the box, in one uninterrupted run (4 dedicated busy-loop
  processes matching `nproc`, started before and killed only after all 20
  calls completed — an EARLIER attempt at this same measurement is NOT the
  source of this number: it was invalidated when its CPU load died partway
  through, and this is the clean re-run): the ORIGINAL (pre-#272) `s+=i`-
  per-element candidate pair, run 20 times under `nice -n 19` plus that
  load, went from 2/20 accepted (documented in #272) to 13/20 accepted with
  this fix. The failures were NOT spread evenly: all 6 of the first 6 runs
  failed (0-2/4 sizes significant) while the 1-minute load average was
  still climbing from the fresh start, then 9 of the next 10 passed, then
  1 more failed (run 16, 2/4) before the final 4 passed — a real
  Mann-Whitney result on genuinely swamped timings under a harsher-than-
  realistic synthetic load (PR #272's own characterization of this same
  4-busy-loop harness), not a gap the auto-scale floor is meant to close;
  `sizes_below_floor` was empty on every failing run, confirming these were
  noise verdicts, not visibility-floor misses. This candidate pair is
  deliberately NOT added as a hard live CI assertion for the same reason —
  see `tests/test_translation_verify.py`'s comment at that spot. Re-measured
  the worst-case executor-call bound (unchanged at 234 — a size that already clears the floor is no
  longer re-measured for free, so the common case costs FEWER calls, but
  the worst case — every size stays below the floor every round on both
  sides — costs the same as the old whole-batch loop) and the worst-case
  wall time (forced live on this box's Rust backend: ~23.5s at 200 calls,
  ~16.4s at 160 calls including an alignment remeasurement, both
  ~0.11-0.12s/call; scaled to the full 234-call ceiling, ~27s — an ~6.7x
  margin under `verify_optimization`'s 180s MCP deadline). `CONTRACT_VERSION`
  bumped `1.5.0` -> `1.6.0` (MINOR: `sizes_below_floor` added to
  `inference`, always present, no existing field moved).
  `GRADE_RULES_VERSION` unchanged — the evidence-to-grade mapping did not
  move, only `grade_basis`'s text gained an optional clause when sizes were
  excluded.
- **`tests/test_features.py`'s entire async MCP section (session lifecycle,
  file I/O, artifacts, verdicts, compact mode, streaming, package install —
  everything the file's own docstring claims to cover except a handful of
  module-level checks) had never run.** `sys.exit(1 if FAILS else 0)` sat
  BEFORE `asyncio.run(main())` at the bottom of the file, and `sys.exit()`
  raises `SystemExit` immediately — so `main()` was defined but never
  awaited, in every CI run and every local run since it was added.
  `asyncio.run(main())` now runs first. Fixing the ordering surfaced a
  second, previously-invisible bug the dead code was hiding: a module-level
  `_txt = _skill.read_text(...)` (added later, near the bottom of the file)
  shadowed the async `_txt()` helper `main()` calls to extract MCP tool-call
  text — Python resolves a global by name at call time, so every `await
  _txt(...)` inside `main()` would have raised `TypeError: 'str' object is
  not callable` the moment it actually ran. Renamed to `_skill_txt`. Every
  check inside `main()` was re-verified against current tool behaviour on
  both the native Rust backend and the Python fallback and needed no other
  change — the checks already asserted the current documented shapes (e.g.
  `execute_code_stream`'s `streamed`/`streamed_partial`/`note` fields); they
  had just never been exercised. The file now prints an existence floor
  ("N checks, M async") that fails if the async section ever runs zero
  checks again, and `ci-python.yml`'s step for this file greps that line
  independently so a future regression of the same shape cannot silently
  disable it a second time.

- **`verify_optimization` could reject a genuine O(1) win as noise, and
  its alignment step could re-measure a real-cost baseline at a fast arm's
  UNVALIDATED, exhausted rescale ceiling.** Each side's auto-scale in
  `_timed` can exhaust `_MAX_RESCALE_ROUNDS` (10^4x the starting size)
  without ever clearing `_VISIBILITY_FLOOR_MS` — exactly what a genuine O(1)
  candidate does. The old `_align_sizes` saw that as "the other side scaled
  less" and re-measured the SLOWER side (often the baseline, with real
  per-n cost) AT that exhausted n — a legitimate huge win became "baseline
  re-measurement failed" or a re-measurement bounded only by
  `timeout x REPEATS` per position (up to 150s each at the default
  `timeout=30`). An intermediate version of this fix shrank the exhausted
  candidate back to the baseline's n and then EXCLUDED the position because
  the candidate was still below the floor there — driving `sizes_total` to
  0 and `accepted` to `False` for per-size ratios of 20x-2400x (caught in
  review before merge). The rule is now ASYMMETRIC (`_comparable_positions`,
  shared by `_speedup` and `_infer_speedup` so the headline ratio and the
  significance verdict can never disagree about which positions counted): a
  position counts whenever the BASELINE is measurable and cleared the floor,
  regardless of the candidate; a candidate too fast to register at a size
  the baseline needed real work to reach is the most decisive evidence the
  tool can produce (every candidate run beat every baseline run), so the
  already-collected samples feed the Mann-Whitney test directly, with the
  pairing disclosed as `size`/`size_after` (`per_size[].size_after` and
  `speedup.per_size[].n_after`, present only when the two n differ). A
  position is excluded and named in `inference.sizes_below_floor` only when
  the BASELINE itself is unmeasurable, never cleared the floor, or lacks
  the >=2 runs a test needs. Alignment only ever grows the CANDIDATE side, up to a baseline n that
  already cleared the floor; the baseline is never grown (an earlier
  version grew it to a padded O(1) candidate's rescaled n and timed out). Second, one shared
  wall-clock budget — `_MEASUREMENT_BUDGET_S` (120s, `time.monotonic()`) —
  is threaded through EVERY `tools._measure` call the tool makes, both
  `_timed` ladders and alignment (an earlier version bounded alignment
  only, leaving `_timed`'s own rescale rounds free to run toward the 180s
  tool deadline on a real-cost baseline); exhaustion fails fast with a
  coded, disclosed reason. Per-size executions divide the remaining budget
  by `repeats`, not by `repeats x len(sizes)`, so a legitimately slower
  largest size is not starved by cheaper siblings (measured: an even split
  left a real O(n^2) baseline's largest calibrated size 5.7s when it needed
  6.3s). The executor-call bound is at most the previous 234
  (candidate-below-floor positions are no longer re-measured). Measured on
  this box's Rust backend with a REAL payload — a `volatile`-guarded O(n^2)
  baseline against a genuine O(1) candidate at the default sizes
  `[2000, 5000, 10000, 20000]`: the full call completed in ~9.5s (~19x
  margin under 180s); a re-measurement the baseline could not afford
  failed via the budget in ~11s rather than up to 150s. The live
  O(n^2)->O(1) test now calibrates its sizes at test time from probes of
  BOTH arms (the baseline must cost >= 100 ms and >= 6x the O(1)
  candidate's spawn-plus-pad cost on that host) instead of fixed constants
  (the fixed-constant version measured a quadratic-vs-constant win at only
  1.55x on a hosted macOS runner and rejected it), and asserts the
  product's own majority rule rather than 4/4 sizes. `CONTRACT_VERSION` bumped `1.6.0` -> `1.7.0`
  (MINOR: `size_after`/`n_after` added, present only when the two arms
  ran at different n; `sizes_below_floor` semantics narrowed to the
  baseline side; no field removed or moved).

### Changed

- **The runner's own scratch files (the entry file's source copy, the
  compiled binary, and — on the Rust backend — the compile/run redirect
  files) now live inside a private `.codecalc-run/` subdirectory of the
  workdir, never at the workdir root.** Previously `session_run`/
  `execute_code(session_id=...)` unconditionally (re)wrote whatever entry
  file was currently executing to a root-level `main.<ext>`, so a session's
  own, unrelated `main.py` (or the equivalent for another language) at the
  session root was silently overwritten by running any OTHER entry file —
  documented as a known gap in 0.8.0's release notes, fixed here. The
  running PROGRAM's cwd is unchanged (still the workdir root, on both
  backends), so a program's own relative file access
  (`open("data.csv")`, a session's own files) keeps resolving exactly
  where it always has; only the runner's OWN copies move.

  **CRITICAL, closed before this shipped:** the scratch directory is now
  WIPED AND RECREATED FRESH on every call, never reused, and the whole run
  is refused with a coded `permission_denied` if anything already inside
  it (at any depth) is not a plain file or directory. An earlier version
  of this fix made `.codecalc-run/` itself symlink-safe to create but left
  every file WRITTEN inside an already-real one exposed: a session's own
  executed code, from a PRIOR call, could plant
  `.codecalc-run/main.<ext> -> <workdir>/important.txt` (the program runs
  with the workdir as its cwd, so it can reach both names), and the NEXT
  call's write of its own entry source would follow that symlink and
  overwrite `important.txt` — including, via a session_list-disclosed
  absolute workdir path, a file in a DIFFERENT session. Reproduced end to
  end on both backends before the fix; every write into the scratch
  directory now opens `O_EXCL`/`CREATE_NEW` (refusing ANY pre-existing
  entry atomically), and `tests/test_session_jail.py` carries the
  regressions (symlink, cross-session, and a planted FIFO).

  `python3`'s `sys.path[0]` would otherwise have silently stopped seeing a
  sibling module written via `session_write_file` (the entry copy's new
  directory holds nothing a caller ever wrote) — both backends now set
  `PYTHONPATH` to the workdir root for every step, restoring the same
  import resolution; a per-run `dependencies` install (which lands at the
  workdir root, same as before) is covered by the identical mechanism.
  `node` has the same class of gap for `require("./sibling")` (CommonJS
  resolves a relative specifier against the REQUIRING FILE's own
  directory; `NODE_PATH` does not reach it, only bare specifiers) — fixed
  by writing a small `Module._resolveFilename` shim into the scratch
  directory on every node run and loading it via
  `NODE_OPTIONS=--require=<path>`, retrying a failed relative lookup
  against the workdir root before giving up. `c`/`cpp`/`c++`/`fortran`'s
  compile commands gained an extra `-I`/include-search entry pointing at
  the workdir root for the same reason (`#include "helper.h"`/
  `include 'helper.inc'` resolve relative to the including file's own
  directory first).

  Three behaviour changes are DOCUMENTED, not fixed: rust's `mod helper;`
  has no search-path flag equivalent to C's `-I`, so a session's own
  sibling `.rs` file at the workdir root no longer resolves — single-file
  rust (the only shape CI's `tested`-tier evidence covers) is unaffected.
  `bun`'s `require`/`import` and `deno`/`typescript`'s ESM `import` use
  their own module resolvers (not Node's `Module` class), so the node fix
  above does not extend to them; a sibling file at the workdir root no
  longer resolves for those three either. Haskell is NOT in this list:
  GHC's default import search path is CWD-relative (`-i.`), and haskell's
  `run` cwd is the workdir root (it has no separate `compile` step): a sibling
  `Helper.hs` still resolves via `import Helper` exactly as before. What
  GHC's own build does, unrelated to this change, is write each module's
  `.hi`/`.o` next to ITS source — so `Helper.hi`/`Helper.o` land at the
  workdir root beside `Helper.hs`, a pre-existing latent collision risk
  with a same-named user file, noted here for completeness.

  `session_artifacts`/`artifacts_created` collapse
  their exclusion rule from a hand-maintained set of root-level basenames
  (`main.<ext>` per language, `a.out`/`a.exe`, Kotlin's `out.jar`, and the
  Rust backend's six `{compile,run}.{out,err,in}` files) to the single
  `.codecalc-run/` directory prefix, since the runner's files no longer
  share a directory with anything a caller can name at all. Sessionless
  `execute_code`/`execute_code_stream` share the identical layout inside
  their own per-run temp workdir.

## [0.8.0] — 2026-09-07

Three changes since 0.7.0: per-run `dependencies` extended to
`execute_code_stream`/`run_submit`, a hard `max_output_kb` ceiling with
matching result-size hints on all five large-result tools, and three new
result-contract shapes for verification/comparison tools plus the artifact
filter and session-walk fixes that came with them. The result contract
moves `1.4.0` → `1.5.0` (additive) for the three new shapes.

### Added

- **Three new result-contract shapes: `translation_verification`,
  `optimization_verification`, `edge_case_comparison`.** `verify_translation`
  and `verify_optimization` were always stamped `contract_version` at the
  same MCP tool boundary as every other tool, but matched none of the five
  published execution shapes — `docs/contract/README.md` explicitly scoped
  them OUT of the schema's "exactly one branch matches" claim rather than
  close the gap. `translation_verification` covers `verify_translation`'s
  result (`ok`/`passed`/`matched`/`mismatched`/`inconclusive`/`total`/`cases`
  plus `grade`/`grade_basis`/`grade_rules_version`). Also fixes a real
  omission in `translation.aggregate`, caught in review: it never set `ok`
  at all, when `verify_optimization`'s own `{"ok": True, "accepted": False,
  ...}` early return already established that `ok` means "the tool
  completed and produced a real answer", never the verdict — a mismatch is
  now `ok: true` exactly like a pass, and `aggregate` sets it
  unconditionally rather than the schema carving out an exception for its
  absence. `optimization_verification` covers
  `verify_optimization`'s result, including the full `inference` object
  (the per-size one-sided Mann-Whitney U test behind `accepted`) and the
  embedded `verification` field (verify_translation's BARE evidence, no
  grade/`contract_version` — that wrapper only happens at the MCP tool
  boundary, which this internal call never crosses). `compare_edge_cases`
  had the same gap in its success shape; `edge_case_comparison` closes it
  (its refusal already matched `rejected` and needed no new branch, same as
  a `verify_optimization` measurement failure). `CONTRACT_VERSION` bumped
  `1.4.0` -> `1.5.0` (MINOR: three new shapes, no existing shape moved).
  `tests/test_contract.py` runs all three tools against fixture code and
  validates the real output against the published schema.
- **Per-run `dependencies` extended to `execute_code_stream` and
  `run_submit`; `compare_execution` now discloses rather than silently
  drops.** Previously only `execute_code`/`session_run` honoured a PEP 723
  block or an explicit `dependencies` argument; the other three execution
  tools accepted neither, so a `# /// script` block in code sent to any of
  them was silently inert prose. `execute_code_stream` now takes the same
  `dependencies` argument execute_code does, with the same refusal codes
  (`capability_not_requested` under `no_net`/deny-network/strict), the same
  120s aggregate install budget, and the same sessionless workdir quota —
  the install runs BEFORE the first progress notification, so a refusal or
  a failed install is the stream's first and only event, never interleaved
  with output. `run_submit` also gained `dependencies`: the install runs on
  the SAME background worker as the code that follows it (a new
  `RunSupervisor.start(runner=...)` parameter replaces the normal
  `provider.execute` dispatch with a caller-supplied callable), so the call
  still returns a run_id immediately regardless of how long the install
  takes — only the (cheap, network-free) temp directory creation happens
  synchronously, before the run_id is minted. A failed install becomes the
  run's own terminal CODED error (a new `RunSupervisor`/`run_supervisor`
  exception, `CodedRunFailure`, carries it through `_collect()`'s existing
  exception path verbatim, with no execution receipt attached — the
  provider was never reached), inspectable via `run_inspect` exactly like
  any other outcome. The installed entries and the per-run workdir both
  ride on the run's OWN record from the moment `start()` returns, not on
  `run_submit`'s stack frame: `dependencies` appears on every terminal
  `run_inspect(run_id)` call (retained for the run's whole retention
  window), and the workdir is released on the run's first collection —
  whichever of `run_inspect`, `run_submit`'s own opportunistic reap of
  finished runs (so a fire-and-forget caller that never inspects a run
  still does not leak its workdir), or a startup sweep of the crash-recovery
  journal (a new `workdir`/`workdir_identity`/`workdir_cleaned` triple,
  persisted only for a run that has one) turns out to be. `compare_execution`
  fans out across several languages with no per-language install plumbing
  behind it (no capability broker, no per-language workdir), so a real
  per-language installer was judged out of scope; it instead REJECTS an
  explicit `dependencies` argument with a `validation` error naming the
  `dependencies` capability, and DISCLOSES — never silently drops — a
  python3 snippet's inline PEP 723 block via a new per-row
  `dependencies: {"status": "unsupported", "reason": ...}` field. No further
  result-contract bump: `execute_code_stream`/`run_submit`'s `dependencies`
  field reuses the SAME optional field the execution envelope already
  declares, and `compare_execution`'s result shape has never been part of
  the versioned contract.

### Fixed

- **`tests/test_translation_verify.py`'s live "a real O(n)->O(1) win is
  accepted" check flaked on hosted sandbox runners** (observed twice on
  macOS in `ci-python`, 0/2 on ubuntu/windows). The candidate pair's two
  smallest sizes sat close enough to process-startup/scheduling noise that
  the one-sided significance test correctly refused to certify them,
  dropping the run below a majority of sizes. The gate's math was not the
  bug — the test's candidate pair was too close to the noise floor at the
  smallest sizes. Fixed by giving the same O(n)->O(1) pair ~10x more work
  per element so every tested size clears the floor with margin, not by
  weakening `codecalc/optimization.py`'s acceptance rule.
- `run_inspect` did not carry `_meta["anthropic/maxResultSizeChars"]`, even
  though its terminal reply (once a `run_submit`-started run finishes) is the
  same execution envelope `execute_code` returns and can approach the same
  output cap — it now carries the same value the other four large-result
  tools do.
- `session_artifacts`/`artifacts_created` no longer hide a user's own file
  just because it shares a basename (`a.out`, `main.<ext>`, `run.out`, ...)
  with one of the runner's own scratch files somewhere else in the
  workspace — e.g. `gcc -o a.out program.c` run in a session subdirectory
  now reports the binary. The exclusion is now scoped to the exact,
  root-level paths the runner actually writes to, not the basename anywhere
  in the tree, and is now derived from the language registry (every
  `main.<ext>` the registry knows, not a hand-picked subset) rather than a
  hand-maintained list, so adding a language can no longer reopen this bug
  for it. `a.exe` (the compiled-output slot on Windows), Kotlin's
  compile-time `out.jar`, and the session lock file
  (`.codecalc-session-lock`) are now excluded too — none of them were
  previously in the excluded set at all. `run.out`/`run.err`/`run.in` and
  `compile.out`/`compile.err`/`compile.in`, all six of which the Rust
  execution backend genuinely writes on every call (`run_step()` in
  executor/src/main.rs, verified against a real Rust-backend run — a first
  pass at this fix wrongly concluded, from a plain-string grep that cannot
  see the Rust code's runtime `format!("{tag}.out")`, that none of the six
  were written anywhere and pruned them), stay excluded.
- A run whose output spills to `.codecalc-spill/` no longer reports the
  spill file itself as a new artifact in `artifacts_created` — it was
  duplicating `stdout_spill`/`stderr_spill`, which already point at it.
- Documented, not fixed here (a fix is filed separately): `session_run`
  and `execute_code(session_id=...)` always rewrite the session's
  root-level `main.<ext>` scratch file with whatever entry file's source is
  currently executing, regardless of that entry file's actual name — so a
  session's own, unrelated `main.py` (or the equivalent for another
  language) at the session root is silently overwritten by running any
  OTHER entry file.

### Changed

- **A session-scoped run (`session_run`, `execute_code(session_id=...)`) walked
  its own workspace directory up to 6 times per call** — `quota_precheck`'s
  disk-usage AND artifact-count checks (2 separate `rglob`s), the pre-run
  artifact snapshot #263 added, and `quota_postcheck`'s classify AND
  disk-usage AND artifact-count checks (3 more), none aware any of the
  others had already walked the same, unchanged-in-between directory.
  Measured with `hyperfine` (20 runs, Rust backend, min of run) on
  workspaces of 0 / 500 (the default `CODECALC_MAX_ARTIFACT_COUNT` cap) /
  5000 (10x the cap) small nested files: at the cap, #263 added 64ms
  (303.5ms at the pre-#263 commit -> 367.5ms on main, +17.4%); at 10x the
  cap, 599ms (572.2ms -> 1171.2ms, +51.1%) — material by both of this
  ticket's bars (>50ms absolute, >10% of trivial-run latency at the cap).
  This fix cuts that back to 329.3ms at the cap (-38.2ms/-10.4% from
  main) and 747.0ms at 10x (-424.2ms/-36.2%): a `_workspace_scan(d)`
  helper builds the filtered artifact listing AND the unfiltered
  disk-usage total in ONE `rglob` pass — a microbenchmark showed doing
  both costs no more than the filtered listing alone already did (163ms
  vs 165ms median at 5000 files) — and `quota_precheck`/`quota_postcheck`/
  `_artifact_snapshot`/`_classify_new_artifacts`/`_artifact_count_refusal`/
  `_disk_quota_refusal` now each accept an optional pre-computed scan (or
  its `entries`/`total_bytes` half) that `sessions.execute()`/
  `execution_service.SessionService.run_file()` share across their own
  before/after pair — 2 walks per call instead of 6 (`run_file()` still
  forces a fresh post-install scan when dependencies were actually
  installed, reusing the pre-install one otherwise — see
  `_artifact_snapshot`'s docstring). Every function still takes its own
  independent walk when called bare (bare is what every existing test
  does), so nothing outside that shared before/after pair changes shape or
  cost. A residual gap remains versus the pre-#263 numbers (329.3ms/747.0ms
  vs 303.5ms/572.2ms) — not walk COUNT (2 here vs pre-#263's 4) but
  per-file COST: #268's more precise runner-internal-file exclusion (a
  relative-path-parts check per file, not a top-level basename compare)
  made every filtered walk ~48% pricier standalone regardless of this fix
  (measured: 180.0ms vs 121.8ms median per call at 5000 files) — a
  separately-justified correctness fix, not something this change should
  undo.
- A caller-supplied `max_output_kb` used to reach the executor unclamped and
  push a tool's real output past the `anthropic/maxResultSizeChars` value it
  advertises (`execute_code`/`execute_code_stream`/`run_submit`, noted as a
  caveat in each docstring since 0.7.0); there was no other ceiling on this
  parameter, so it is now clamped at the MCP boundary on all three tools to
  a new hard ceiling of 240 KiB per stream — separate from the existing
  64 KiB DEFAULT `max_output_kb=0` selects — chosen as the largest
  round-KiB figure that keeps the advertised hint under Claude Code's
  documented 500,000-character maximum for this `_meta` field's TEXT
  content. `anthropic/maxResultSizeChars` moves from 139,072 to 499,520
  (`2 * 240 KiB + 8_000`) on all five large-result tools accordingly, and
  the docstrings' caveat has been rewritten to describe the ceiling rather
  than the gap. That value bounds the serialized text `content` block only:
  a typed tool (every one of the five except `session_run`) also carries an
  equal-sized `structuredContent`, so its total wire payload approaches
  twice this hint; `session_run`'s inlined artifact blocks are likewise
  separate from it.

## [0.7.0] — 2026-09-07

Four changes since 0.6.0: inline artifacts from session-scoped runs,
`verify_optimization`'s accept decision backed by a significance test,
per-tool `ToolAnnotations`/`outputSchema`/server-side policy `_meta`, and
per-run dependencies for `execute_code`/`session_run`. The result contract
moves `1.3.0` → `1.4.0` (additive) for the fields the last three add.

### Changed

- **`verify_optimization` now requires statistical significance, not just a
  ratio.** With 3 runs per side (the old `repeats`), the smallest one-sided
  p a single size could ever produce was 1/20 — no size could reach the
  conventional alpha=0.05 no matter how clean the separation was, so
  "median ratio cleared `min_speedup`" was an arithmetic fact about noisy
  timings, not evidence of a real difference. `repeats` is now 5 (floor
  1/252), `_timed` keeps every run (`all_runs_ms`), not just the min, and a
  new dependency-free `codecalc/stats.py` runs a one-sided Mann-Whitney U
  test per size (exact enumeration for n1+n2<=20, normal approximation with
  tie correction beyond that — ported from mrnh/rigor, MIT). `accepted`
  now additionally requires a MAJORITY of measured sizes to reject "not
  faster" at alpha=0.05; the result gains an `inference` field with the
  per-size U statistic, p-value, and rank-biserial effect size behind the
  verdict. Also fixes a latent size-misalignment bug the significance test
  exposed: `_timed(original)` and `_timed(candidate)` each auto-scale their
  own `sizes` independently, so when only one side needed to rescale (the
  common case — a slow baseline is visible immediately, a genuinely fast
  candidate is not) the two `sizes` lists diverged while staying the same
  length, and `_speedup`/`_infer_speedup` zipped them by position — a
  per-size ratio or p-value could be labelled with the wrong size entirely.
  New `_align_sizes` re-measures whichever side scaled less at the other
  side's final sizes before either function sees the pair. Confirmed the
  new worst case (~13.4s, 234 executor calls, up from 214 before this fix)
  stays well inside `verify_optimization`'s 180s deadline. `grades.py`'s
  `grade_verify_optimization` basis text now names the significance result
  (sizes significant / alpha) alongside the ratio when `inference` is
  present — text describing the same evidence, not a change to what
  evidence maps to what grade, so `GRADE_RULES_VERSION` stays `2`.
- **CI gap closed: `tests/test_translation_verify.py`'s native-executor-gated
  checks had never once run against the real executor.** The `tests` job
  never builds the Rust binary (that split is the `sandbox` job's whole
  reason to exist), so every `if executor._rust:` block in that file —
  including the live O(n)->O(1) win and the new identical-code
  false-accept-rate check — silently self-skipped in CI every time.
  `ci-python.yml`'s `sandbox` job now runs the same file a second time
  after confirming the Rust backend, same pattern as
  `test_platform_contract.py`'s and `test_mcp_all.py`'s existing two-job
  invocations.

### Added

- **Per-tool `ToolAnnotations` (readOnlyHint/destructiveHint/idempotentHint/
  openWorldHint) on all 52 tools, and typed return schemas on 49 of them.**
  Every tool now declares a group-derived (with named per-tool overrides)
  `ToolAnnotations` and a human-readable `title`. Every bare `-> dict`
  return became `-> dict[str, Any]` EXCEPT `session_read_file` and
  `session_run` (both can return something other than a dict — a raw
  `ImageContent`, or a list of content blocks when a run produced
  artifacts — and typing that union wraps every reply in `{"result": ...}`,
  which for `session_run` fails the SDK's own output validation on the list
  branch; both keep their pre-existing untyped return, exactly as before);
  `list_languages`/`list_execution_providers` were already `-> list[dict]`,
  which the SDK schematises fine without a change. `dict[str, Any]` is what
  actually flips a tool onto the SDK's structured-output path (verified: a
  bare `-> dict` cannot be schematised at all). `tools/list` now carries an
  `outputSchema` for every typed tool and every `tools/call` reply from one
  carries `structuredContent` alongside its unchanged text block — see
  `docs/contract/README.md`'s updated 2026-09-07 note for what that schema
  actually contains (a permissive `{"type": "object"}`, not this repo's
  detailed result contract), which two tools stay untyped and why, and the
  caveat that some MCP clients let `structuredContent` displace the text
  block's default display without dropping anything. `contract_version` and
  `code` are asserted present in `structuredContent` now that a caller can
  read it structurally. A caller-supplied `max_output_kb` above the default
  64 KiB can push `execute_code`/`execute_code_stream`/`run_submit`'s real
  output past their advertised `anthropic/maxResultSizeChars` below — noted
  in each tool's docstring.
  `CONTRACT_VERSION` bumped `1.3.0` -> `1.4.0` (MINOR: the wire gained a
  capability, no result shape moved). New `tests/test_tool_annotations.py`
  asserts every one of the 52 registered tools carries a complete
  annotation, that no `calculator`-group tool is marked non-read-only, and
  that `outputSchema` is `None` on exactly the two tools left untyped.
- **Server-side policy `_meta` on select tools, read by Claude Code
  (`code.claude.com/docs/en/mcp`, retrieved 2026-09-07).**
  `_meta["anthropic/requiresUserInteraction"] = true` on `install_package`
  and `update_runtimes` (both mutate the host and fetch from a registry) —
  forces a permission prompt on every call even under
  acceptEdits/auto/bypassPermissions. `_meta["anthropic/alwaysLoad"] = true`
  on `calc_exact`, `execute_code`, `verify_translation`,
  `verify_optimization`, `list_languages` — five entry-point tools that stay
  loaded when a client defers the rest of the surface via tool search.
  `_meta["anthropic/maxResultSizeChars"]` on `execute_code`,
  `execute_code_stream`, `session_run`, `compare_execution` — set to
  `2 * executor.MAX_OUTPUT_BYTES + 8_000` (139,072), derived from the two
  independently-capped output streams plus envelope overhead, not
  Anthropic's 500,000-char ceiling. New `tests/test_tool_meta.py` asserts
  `tools/list` carries exactly these `_meta` keys on exactly these tools and
  no others.
- **Inline artifacts from session-scoped runs.** `session_run` and
  `execute_code(session_id=...)` — both of which run in a session workspace
  that outlives the call, unlike sessionless `execute_code`'s Rust-owned temp
  directory — now report `artifacts_created`: the files a run just created
  or modified, as `{path, size, mime, resource}` (`resource` is the same
  `codecalc://session/{sid}/files/{path}` URI `session_read_file` already
  serves). `session_run` additionally inlines eligible artifacts as MCP
  content blocks alongside its JSON result: a PNG/JPEG/GIF/WebP up to 1 MiB
  raw as an `ImageContent` block, a CSV/plain-text/HTML/JSON file up to
  64 KiB as an `EmbeddedResource`, and everything else — including an
  oversize file of either kind — as a `ResourceLink` (a pointer, not
  attached bytes). At most 8 such blocks and 4 MiB of inline bytes ON THE
  WIRE per reply — an image's base64 encoding (~4/3 its raw size), not its
  raw file size, is what the budget charges, so a reply's actual attached
  bytes can never exceed the 4 MiB promised regardless of mime mix; beyond
  either cap the remaining artifacts still appear in `artifacts_created`
  and the result carries `truncated_inline: true`.
  `compact_result` never drops `artifacts_created`/`truncated_inline` for
  the same reason it never drops `unenforced`/`output_error` (#117): a
  compact caller is the one least able to discover a new file any other
  way. The content blocks themselves are transport-level and outside the
  result contract. This is also why `session_run` stays off the typed-return
  list above: it can return `[TextContent, *artifact blocks]` rather than a
  dict, and typing that union fails the SDK's own output validation on the
  list branch — see `codecalc/server.py`'s comment above the tool.
  `CONTRACT_VERSION` moves `1.3.0` → **`1.4.0`** (a MINOR add) for the two
  new JSON fields on the session/envelope/compact shapes — the same MINOR
  bump the annotations/`outputSchema`/`_meta` change above shares; both
  landed against the same `1.3.0` base, so there is one bump, not two.
- **Per-run dependencies for `execute_code` and `session_run`.** A PEP 723
  inline script metadata block (`# /// script` ... `# ///`, python3 only) or an
  explicit `dependencies: list[str]` tool argument (the only mechanism for
  node; a convenience for python3 that MERGES with the block, deduped by PEP
  503 normalized name with the argument winning a conflict) installs packages
  BEFORE the code runs. The fetch never runs inside the sandboxed step: every
  dependency goes through the existing confined `install_package` path
  (allowlist, argv-injection checks, `--only-binary=:all:`/`--ignore-scripts`,
  Landlock/Seatbelt confinement) first, targeting the run's own workdir — a
  sessionless run gets one pre-created for it and removed afterward via the
  same identity-checked deletion the executor uses for its own temp
  directories. A dependency-bearing run is refused (`capability_not_requested`,
  no fetch attempted) when `no_net=True` was requested or the active
  `CODECALC_CAPABILITY_POLICY` denies or strictly limits network — the
  sandboxed step's own `--no-net` is unrelated and unchanged. Two more
  ceilings, separate from the run's own `timeout`: a fixed, aggregate
  install-time BUDGET across every dependency of one run (120s,
  `DEFAULT_DEPENDENCY_INSTALL_BUDGET_SECONDS`; `packages.install()` gained a
  `timeout` parameter, default 600s unchanged, so this can bound each
  install by the remaining budget) — exceeding it refuses the run with a
  stamped `timeout` naming the budget and how far it got, before the run's
  own `timeout` clock even starts; and, for a sessionless run, a disk QUOTA
  on the per-run dependency workdir, reusing `CODECALC_SESSION_DISK_QUOTA_MB`
  (checked after each successful install) rather than a second constant —
  exceeding it refuses with a stamped `resource_exhausted` naming the
  measured size and the cap. A PEP 723 block ALONE, with no `dependencies`
  argument, is enough to trigger an install — this is audited distinctly
  (`audit.DEPENDENCY_INSTALL_IMPLICIT`) from an explicit
  `install_package`/`dependencies=` call, and `SessionService` gained an
  `audit` parameter (server.py now passes it) so `session_run`'s installs are
  audited too. New optional `dependencies` field on the execution result,
  landing under the SAME `1.4.0` (no further bump — an additional field on
  the version the sibling entries above already moved to):
  `[{spec, language, ok, installer, elapsed_ms, unenforced?}]`. New
  `codecalc/dependencies.py`; `packages.install()` also gained a `workdir`
  parameter for a sessionless target directory. `dependencies.refusal_result`
  names `network` in `requested_capabilities` (not `[]`), matching
  `capabilities.rejection_result`'s own shape. README's "Network boundary"
  table (plus a paragraph on the implicit trigger and the two ceilings) and
  SECURITY.md's "Explicitly out of scope" list each cover this;
  `execute_code_stream`/`run_submit`/`compare_execution` docstrings now say
  a PEP 723 block is inert there (none of them read one).

### Docs

- **Six weakest tool descriptions rewritten to disambiguate from a sibling,
  per Glama's coherence review.** `evaluate_expression`, `simplify_expression`,
  `solve_expression`, `calc_stats`, `data_sizes`, and `human_duration` were
  the shortest tool docstrings in `codecalc/server.py` and none named a
  sibling tool to distinguish itself from — `evaluate_expression`'s own text
  said "evaluate or simplify", directly claiming `simplify_expression`'s job.
  Each now follows a four-part shape (what it returns, the sibling it is the
  alternative to and the condition that decides between them, a natural
  follow-up tool, and the return shape) modelled on `matrix`'s docstring,
  which Glama scored 5/5 on disambiguation. The MCP tool description IS the
  docstring the SDK exposes in `tools/list` — this is a documentation-only
  change, no behavior moved. `scripts/tool_select_eval.py --baseline` (the
  lexical tool-selection regression gate) improved on all three `--tools`
  presets (full 118→123, dev 96→101, core 63→68 top-1 hits out of a prior
  measured run; net +4/+5/+5 against the checked-in baseline) — a sibling's
  name in a description adds discriminating vocabulary to the very prompts
  it exists to disambiguate. `scripts/data/tool_select_baseline.json`
  regenerated from this change.

## [0.6.0] — 2026-09-06

### Added

- **MCPB bundle (Claude Desktop extension), built and attached to every
  release** (#258). `mcpb/` at the repo root (manifest, `pyproject.toml`,
  `.mcpbignore`, `src/server.py`) packs a one-click Claude Desktop install.
  Uses the manifest's **`uv`** server type rather than the legacy `python`
  type: Claude Desktop's own managed `uv` resolves `codecalc[full]` from
  PyPI into an isolated venv on first launch, so nothing codecalc-specific
  is vendored into the bundle (3,845 bytes, 3 files). A PEP 508 environment
  marker (`sys_platform != 'win32' or platform_machine != 'ARM64'`) on the
  `codecalc[full]==<version>` dependency excludes Windows-on-ARM64, because
  `cryptography` (pulled in via `mcp -> pyjwt[crypto]`) ships no
  `win_arm64` wheel on PyPI; `mcpb/src/server.py` checks for that platform
  before importing `codecalc` at all and exits with a one-line cause and
  workaround instead of a bare `ModuleNotFoundError`. `manifest.json`'s
  `long_description` discloses the resulting undisclosed-until-now
  behaviour up front: first launch resolves ~120 MB from PyPI over the
  network and fails without one, with nothing cached yet. New
  `tests/test_mcpb_manifest.py` (58th test file) asserts the manifest
  shape, the version/pin match `codecalc.__version__`, the marker text, and
  — behaviourally, via a subprocess with `sys.platform`/`platform.machine()`
  monkeypatched — that the ARM64 guard actually fires clean and
  traceback-free. `scripts/check_version.py` now gates `mcpb/manifest.json`
  and `mcpb/pyproject.toml`'s pin alongside the other version sites. A new
  `build-mcpb` CI job builds the bundle and feeds it through the release
  job's existing SHA256SUMS/attestation/upload steps; publish-pypi/
  publish-testpypi/publish-crates do not depend on it.
- **`glama.json`** at the repo root, naming the maintainer for Glama author
  verification on the server's Glama listing (#255).
- **`MAINTAINERS.md`** now names the confirmed maintainer handle instead of
  a placeholder, agreeing with `glama.json` (#256).

### Changed

- **`rust` and `go` promoted to the `tested` reliability tier** (from
  `best_effort`), observable in `list_languages`, `runtimes_status`, and
  `doctor`. Not a label flip: a new CI-wired harness
  (`tests/test_tier_evidence.py`) compiles and runs a real program in each
  through the execute path and asserts the computed stdout on every PR, and
  on the Linux CI leg a missing toolchain **fails** rather than skips
  (`CODECALC_REQUIRE_TIER_EVIDENCE=1`). `scripts/check_runtime_tiers.py` now
  derives the `tested` set from that harness's literal source too, and
  additionally asserts the evidence STEP itself is live — invokes the
  harness, sets the skip-promoting flag in its own env, gates on the real
  Linux condition, no `continue-on-error` — proven fail-first in
  `tests/test_runtime_tiers.py` for a weakened registry claim, a dropped
  language, a stripped flag, and an `if:`-disabled step. The harness bakes a
  per-run nonce into each program and requires that exact computed line as
  the entire stdout, so a stale artifact or replayed result cannot pass.
  `csharp` deliberately stays `best_effort`: its recorded host-toolchain
  breakage is the tier system's founding counterexample, and promoting it is
  a separate decision.

### Fixed

- **`scripts/build_mcpb.py`'s retry now covers only the npx network fetch**
  (#259). The retry wrapper added in #258 retried *any*
  `subprocess.CalledProcessError` three times with linear backoff, which
  also retried `mcpb validate`/`mcpb pack` semantic failures — a genuinely
  broken manifest took three attempts and ~6s to report instead of failing
  on its first non-zero exit. Split into `retry_run()` (3 attempts, linear
  backoff, wraps only `npx -y <cli>@<version> --version`, the network step)
  and `run()` (executes `validate`/`pack` exactly once each, now that both
  are local/deterministic once the CLI is cached), so a broken manifest
  surfaces the CLI's own stderr immediately.

### Docs

- **Novice on-ramp: README reordered, new QUICKSTART.md added.** The README's
  top previously put ~50 lines of network-boundary caveats (prose, two
  tables, the grammar-download deep-dive) between the pitch and the Install
  section — a newcomer hit maintainer-facing detail before learning how to
  install. That content moved, verbatim, to its own "## Network boundary"
  section after Install; nothing was deleted or reworded for tone. A 2-line
  quickstart pointer now follows the pitch. The stale "After the first
  release, this becomes the install" wording (0.5.0 has been published since
  #251) is now present-tense ("The published install"), and it's ordered
  before "From source" so the simple path comes first. `QUICKSTART.md` is a
  new, standalone ≤80-line first-timer doc: what it is, install, connect to
  an MCP client (both `setup --write` and copy-paste JSON), `codecalc
  doctor`, a prominent untrusted-code safety note linking SECURITY.md, and
  where to go next.
- **One-click Cursor and VS Code MCP install badges, plus a Glama score
  badge, in the README** (#257). Both badges register the recommended
  `uvx 'codecalc[full]'` Full edition, matching the README's own per-client
  config blocks, rather than the Core edition (whose symbolic/parsing tools
  return `dependency_missing`).

## [0.5.0] — 2026-08-22

### Security

- **BREAKING (strict-policy hole closed): `network_control` no longer means
  "a native executor binary is present" — it now means "this host can
  enforce `no_net` in the kernel"**, and a `strict` capability policy that
  used to approve a network denial on a host it could not actually enforce
  now correctly rejects it with `CAPABILITY_UNENFORCEABLE`.
  `providers.LocalExecutionProvider.describe()`'s `network_control` capability
  was `executor.backend() == "rust"` alone — `true` for ANY host with the
  Rust binary present, including macOS (`no_net` there is only the
  best-effort DYLD symbol shim) and a Linux kernel without seccomp (the same
  shim fallback). `codecalc/capabilities.py`'s `strict` policy trusts that
  flag, unchecked, to decide whether a requested denial of `network` can be
  enforced — so on exactly the hosts where enforcement is weakest, `strict`
  APPROVED the run instead of refusing it, violating strict's own contract
  ("refuse rather than run unenforced"), and the approved run could then come
  back disclosing `no_net` in `unenforced` at the same time the broker's
  receipt claimed the denial was enforced. The Rust executor now answers a
  new `--capabilities` probe (`{"no_net_kernel_enforcement": bool}` — Linux
  `seccomp::available()`, `false` on macOS/Windows), `executor.py` caches it
  once at import the same way it caches `_rust`, and `network_control` is now
  `backend() == "rust" AND` that flag. Non-strict `deny-network` is
  unaffected — a host without kernel enforcement already took the
  disclose-the-leak path, not a hard failure; this only changes what
  `strict` does with a request it previously wrongly approved. Verified both
  ways: a faked shim-only host now gets `CAPABILITY_UNENFORCEABLE` under
  `strict` (the run never reaches the provider), and the live positive
  control on seccomp-capable Linux still gets approved and genuinely
  enforced, with no `no_net` disclosure in `unenforced` — the broker and the
  run's own disclosure now agree. (Hardened further below: the probe now
  proves the filter is INSTALLABLE, not merely configured, and the Python
  cache is bound to the binary's identity rather than its path.)
- **The `--capabilities` no_net probe now proves the seccomp filter is
  INSTALLABLE, and its Python-side cache no longer trusts a stale answer
  after the binary it probed is replaced.** A cross-vendor security review of
  the `network_control`-from-probe change above found no reportable
  vulnerability — every path already failed closed — but flagged three
  refinements, all fixed here:
  - The Rust probe (`seccomp::available()`, `PR_GET_SECCOMP`) proved seccomp
    was CONFIGURED, not that a filter was INSTALLABLE: `CONFIG_SECCOMP=y`
    with `CONFIG_SECCOMP_FILTER=n`, or an inherited policy denying
    `PR_SET_NO_NEW_PRIVS`, would report `true` while the real
    `PR_SET_SECCOMP` install still failed (a real run still failed closed —
    execution aborts pre-payload — but the *reported capability* would have
    been wrong). `no_net_kernel_enforcement_available()` now goes through
    `seccomp::installable()`: a disposable forked child installs the exact
    program `spawn_and_wait` installs (`seccomp::program()`, never a
    parallel copy) and reports success/failure via its exit code, running no
    payload. Startup-only — once per `--capabilities` invocation, not on the
    per-execution spawn path, which keeps using the cheaper `available()`
    check and its own existing fail-closed re-derivation.
  - `executor.py` cached the probe result once at import, keyed on nothing —
    if the binary at that path were later replaced (same-UID/deploy write
    access) with a weaker build, the stale answer would persist for the
    server's remaining life. The cache is now bound to the binary's on-disk
    identity (device, inode, size, mtime via `executor._binary_identity()`)
    and re-probes whenever that identity changes; a stat call is cheap
    enough to do on every read.
  - The import-time probe used a bare `subprocess.run(timeout=15)`, which on
    `TimeoutExpired` kills only the direct child — a wrapped or faulty
    binary that had already spawned a descendant would leak it, the exact
    class of bug `_popen_group`/`_kill_group` exist to close on every other
    path this module spawns the executor. The probe now uses those same
    process-group helpers.
- **A second cross-vendor pass on the probe-hardening fix directly above
  found three further defects in it, two of which defeated its own
  fail-safe goal — all fixed here:**
  - `bool(data.get("no_net_kernel_enforcement"))` read a malformed or failed
    probe as truthy in cases that must be `False`: `bool("false")` and
    `bool(1)` are both `True` in Python, and `proc.returncode` was never
    checked at all, so a binary that printed `true` and then exited nonzero
    was trusted anyway. `_probe_no_net_kernel_enforcement()` now requires
    `proc.returncode == 0` **and** `data.get("no_net_kernel_enforcement")
    is True` — an identity check, not a truthiness check.
  - The timeout-handling fix directly above could itself hang: `_kill_group`
    only `proc.wait()`s on the DIRECT child after SIGTERM, so it returns the
    instant that one process exits — even while a descendant that ignores
    SIGTERM is still alive holding the probe's captured stdout/stderr pipes
    open, which left the following `proc.communicate()` blocking forever
    waiting for an EOF that would never come (reproduced: direct child
    exits `-15`, grandchild alive, drain hangs). Fixed by unconditionally
    escalating to `_reap_group` (SIGKILL, which cannot be ignored) across
    the whole process group before a now BOUNDED final drain.
  - The cache-identity fix directly above published the new binary identity
    and the answer the re-probe produced as two separate steps. A
    concurrent reader could land in the window between them — a subprocess
    call releases the GIL for the whole time it blocks — see the
    already-updated identity, conclude no re-probe was needed, and hand back
    the OLD cached answer while the fresh probe for that very identity was
    still in flight (reproduced: a concurrent reader returned a stale `True`
    while the real re-probe was producing `False`). Fixed by publishing the
    identity and the answer together, inside
    `_NO_NET_KERNEL_ENFORCEMENT_LOCK`, so a concurrent reader hitting a
    changed identity now waits for the same fresh answer instead of racing
    past it.
- **`no_net`'s result now discloses that the Rust executor's block is a
  best-effort symbol shim, not a kernel egress block** (E-1).
  `--no-net` intercepts `socket()`/`connect()` via `LD_PRELOAD` on Linux and
  `DYLD_INSERT_LIBRARIES` on macOS — ELF/dyld symbol interposition, which only
  covers calls that resolve those names through the ordinary dynamic symbol
  table. Verified live: with the shim applied and `no_net=True`,
  `socket.socket(AF_INET)` gets `EACCES` as documented, but
  `ctypes.CDLL(find_library("c")).socket(2, 1, 0)` — which pulls the symbol
  out of libc's own table via `dlsym` on a specific handle rather than through
  that global scope — returns a working fd, and the result's `unenforced`
  still came back `[]`, falsely reading as fully enforced. `executor.py` now
  appends a disclosure to `unenforced` whenever `no_net` was requested and
  satisfied by the shim (as opposed to the shim being altogether unavailable,
  already disclosed separately): `"no_net: best-effort LD_PRELOAD/dyld symbol
  shim — a dynamically-linked ctypes/dlsym or raw-syscall network call
  bypasses it; use the strict (gVisor) backend for a real egress block"`.
  `AUDIT.md` and `executor/blocknet.c`'s own comments corrected to name this
  bypass alongside the already-documented static-linking one.
- **`no_net` is now enforced in the kernel on Linux by a seccomp-bpf filter,
  not just the bypassable symbol shim** (the strong close of E-1).
  The executor installs a seccomp filter in the child, in the async-signal-safe
  `pre_exec` alongside the rlimits, that refuses — in the kernel — every
  syscall-level way unprivileged code can open an inet socket: `socket()` by
  domain, **`io_uring`** (which can open and connect a socket inside the kernel
  without a `socket()`/`connect()` syscall — the path an adversarial review
  caught the first cut missing), and the **x32**/foreign-arch compat range. So
  the `ctypes`/`dlsym`/raw-syscall call that walked around `blocknet.so` (E-1)
  now takes `EACCES` at the kernel boundary. `AF_UNIX` and other domains are left
  alone (runtimes use them). Verified live on aarch64: `libc.socket(2, 1, 0)` →
  `EACCES`, `io_uring_setup` → `ENOSYS`, `socket.socketpair()` and `asyncio`
  still work. When the seccomp filter enforces `no_net`, the result reports it as
  genuinely applied — no best-effort disclosure, because the guarantee is now
  real; on macOS, or a Linux kernel without seccomp, it falls back to the symbol
  shim and keeps the
  E-1 disclosure. **Fail-closed:** a requested `no_net` the kernel refuses to
  enforce fails the run rather than executing the caller's code unprotected.
  The strict (gVisor) backend remains the stronger tier (egress blocked for the
  whole container).

### Added

- **`codecalc setup` now recommends a leaner `CODECALC_TOOLS` group** when
  the variable is unset: a line naming the real, live per-preset tool counts
  AND real per-preset group membership (`core`/`dev` — both read from
  `server.py`'s own `TOOL_GROUPS`/`PRESETS`, so neither the counts nor the
  displayed group list can drift from what `CODECALC_TOOLS=core`/`=dev`
  actually registers; cross-vendor review caught an earlier cut of this that
  derived the counts but still hardcoded the group-membership description)
  for callers that do not need sessions, package installs, or code
  execution. Purely informational: the default surface (`CODECALC_TOOLS`
  unset = all 52 tools) is unchanged, and the line is suppressed once
  `CODECALC_TOOLS` is already set. The `server.py` import this needs is
  scoped to exactly that branch and wrapped so a broken or absent import
  (e.g. a library caller that never went through the CLI entry point, `mcp`
  not installed) silently skips the tip rather than crashing `run_setup()`.
  See README's "Reducing the tool surface".
- **`scripts/fuzz.py`**: a mutation fuzzer over codecalc's two
  highest-risk caller-string surfaces — `safe_expr.classify_unsafe`/
  `safe_parse` (the screen between a caller expression and SymPy's
  `parse_expr`) and `sessions._jail`/`_session_dir` (the traversal guard
  between a caller path and a session workspace write). Asserts, for every
  generated input, that neither surface ever raises an uncaught exception,
  invokes a blocking builtin, or hangs unboundedly. `tests/test_fuzz_smoke.py`
  runs a small fixed-iteration pass in CI (wired into `ci-quality.yml`); the
  full multi-thousand-iteration campaign is a manual/deeper run (see the
  script's own `--help` and module docstring, including two DoS-shaped
  findings it surfaced past the space today's callers can reach, filed for
  follow-up rather than fixed here).
- **`scripts/tool_select_eval.py`**: an offline tool-SELECTION eval — a
  labeled set of 196 plain-language prompts, scored by whether a
  deterministic BM25 lexical selector picks the right codecalc tool against
  the CURRENT `tools/list` names and descriptions. It is the gate a future
  tool-description trim (see "Tool-definition token cost" in the README) has
  to clear before it ships: v1 measured baseline is top-1/top-3 accuracy of
  60.71%/75.51% on `--tools full` (62.75%/76.47% on `dev`, 63.0%/77.0% on
  `core`), checked in at `scripts/data/tool_select_baseline.json` alongside a
  `prompt_set_sha256` that PINS the exact labeled corpus — a `--baseline`
  compare against a corpus that no longer hashes to it fails with a distinct
  "corpus changed" error instead of silently scoring a smaller, easier
  prompt set against the old numbers (a real gap: deleting every 4th prompt
  from the 196-prompt v1 set still cleared the old "3 prompts/tool" floor
  while RAISING measured accuracy). The compare itself is over exact integer
  top-1 hit counts on that pinned corpus, not a float ratio, so a
  non-numeric or negative `--epsilon` fails at argument-parsing rather than
  silently comparing open. Every labeled prompt is validated to exclude its
  own target tool's name and underscore-tokens (singular/plural matched both
  directions), so the eval cannot be gamed by echoing a tool name back at
  it — it can only pass by the description carrying real discriminating
  vocabulary. `--self-check` is the positive control: a full one-at-a-time
  ablation SWEEP over every candidate tool (no sampling — a fixed random
  sample is exactly what let three real ablations pass CI unnoticed in an
  earlier cut), run separately against each of `full`/`dev`/`core` since a
  tool can be top-1-wrong against `full`'s 51 distractors (zero headroom to
  lose) while still showing real, ablation-detectable headroom against
  `core`'s much smaller distractor set. Measured pooled hit loss: 111/119
  (`full`), 87/95 (`dev`), 51/63 (`core`) baseline hits lost across their
  respective sweeps; exits non-zero if either pooled-loss or breadth floor
  is not cleared — proof the gate can actually detect description damage,
  not just report a number. `tests/test_tool_select_eval.py` wires the eval,
  the self-check, and the baseline-regression compare into CI
  (`ci-quality.yml`) for all three tool surfaces. It is a lexical proxy for
  model tool-selection, not a model — see the script's own module docstring
  for what it can and cannot detect.
- **ClusterFuzzLite coverage-guided fuzzing** (`.clusterfuzzlite/`, `fuzz/`,
  `.github/workflows/cflite_pr.yml`/`cflite_batch.yml`): the same OSS-Fuzz
  engine (libFuzzer + atheris + sanitizers) run in this repo's own CI, the
  complement to `scripts/fuzz.py`'s deterministic seeded smoke gate — one is a
  fast reproducible gate, the other discovers new paths the seed corpus never
  named. The atheris harnesses reuse `scripts/fuzz.py`'s seed corpus and
  contract (no duplication); actions are SHA-pinned like the rest of the repo.
  It found the `UnicodeDecodeError` crash fixed below on its first run.

### Fixed

- **`safe_expr.classify_unsafe`** no longer raises an uncaught
  `UnicodeEncodeError` on an expression containing a lone UTF-16 surrogate
  (e.g. `"\ud800"`); it now returns the same `(category, message)` refusal as
  any other unparsable input. Found by `scripts/fuzz.py`.
- **`safe_expr.classify_unsafe`** likewise no longer raises an uncaught
  `UnicodeDecodeError` on an expression containing a bare replacement or
  truncated-multibyte char (e.g. `"�\r�"`) — the same class of C
  tokenizer round-trip crash as the surrogate above, and now caught the same
  way, with the same validation verdict (never passed to SymPy as safe). Found
  by the new ClusterFuzzLite coverage-guided harness, which reached a shape the
  seeded mutator never produced.
- **`safe_expr._walk`** (used by `reject_explosive`, reached from every
  symbolic tool via `safe_parse`) no longer raises an uncaught `TypeError`
  when the parsed expression is a bare reference to a heavy-function name
  with no call parens (e.g. the expression `"binomial"`, not `"binomial(5,2)"`)
  — such a reference resolves to a SymPy `FunctionClass`, whose `.args`
  attribute is an unbound `property` object rather than a tuple. Found by
  `scripts/fuzz.py`.
- **`sessions._jail`** now refuses a session file `path` whose raw
  length exceeds 4096 chars or whose segment count exceeds 256, BEFORE
  calling `Path.resolve()` on it — a `resource_exhausted` refusal in
  microseconds instead of the multi-second-to-tens-of-seconds server CPU cost
  `Path.resolve()` itself takes on a many-segment string (measured: ~1.3s at
  10k segments, ~3.4s at 20k, worse than linear). Reachable from
  `session_write_file`, `session_files`, and `session_read_file` (via
  `_jail`), all caller-controlled. Found by `scripts/fuzz.py`. A
  legitimate session path is a handful of segments; both caps sit far above
  any real use.
- **`no_net`-mechanism descriptions that had gone stale after the seccomp-bpf
  enforcement change above, unqualified "LD_PRELOAD shim" claims that no
  longer match what Linux actually does.** `execute_code`'s own docstring
  said `no_net` was "LD_PRELOAD shim; dynamic binaries only" with no mention
  of seccomp at all — the exact thing a model reads at tool-selection time.
  Corrected there and in: the two session-worker `unenforced` disclosure
  messages in `codecalc/sessions.py` (a worker can't apply either mechanism
  post-spawn, not specifically LD_PRELOAD); the three fallback-path
  `unenforced` disclosures in `codecalc/executor.py`, said when there is no
  native executor at all to apply either mechanism, now a single
  `_NO_NET_NATIVE_MISSING` constant instead of three copy-pasted literals;
  `docs/contract/README.md`, `docs/contract/provider-v1.md`,
  `docs/deployment/README.md`, `docker/README.md`; and the platform-guarantee
  table in `executor/src/platform/mod.rs`'s module doc comment. `SECURITY.md`,
  `AUDIT.md` and the README's own per-platform guarantee table already
  described the seccomp/shim split correctly and are unchanged.
- **Cross-vendor review of the fix above found it incomplete on both axes.**
  First, several of the just-corrected strings themselves said "seccomp on
  Linux, a symbol shim elsewhere" — true on most Linux hosts but wrong on a
  Linux kernel that refuses a seccomp filter (`executor/src/platform/
  unix.rs`'s `seccomp::available()` gate), which also falls back to the
  shim; reworded to "seccomp where the Linux kernel supports it, a symbol
  shim otherwise" in `codecalc/executor.py`, `codecalc/sessions.py`, and
  `docs/contract/README.md`. Second, the "every stale instance" sweep had
  missed build-time and CI text that claims a *missing shim* leaves `no_net`
  unenforced with no platform qualifier — live-probed false on Linux with
  seccomp support (`codecalc-exec` copied without `blocknet.so` still
  returns `unenforced: []`, seccomp enforcing regardless of shim presence):
  `hatch_build.py`, `executor/build.rs` (module doc comment plus both
  build-warning strings), `executor/src/main.rs` (a field comment),
  `README.md` (two spots), `.github/workflows/ci-rust.yml`,
  `.github/workflows/release.yml`, and comments/check-labels in
  `tests/test_executor_sweep.py` and `tests/test_network_policy.py` that
  attributed clean enforcement to shim presence rather than to whichever
  mechanism actually held. `docs/contract/provider-v1.md` also credited the
  symbol shim with being able to "enforce" a network denial, which it
  cannot (best-effort, bypassable, disclosed via `unenforced`) — reworded to
  describe what the capability broker's `network_control` flag actually
  reports (`codecalc/providers.py`/`capabilities.py` behavior itself is
  unchanged; the flag does not yet distinguish real seccomp enforcement from
  the shim, tracked as a separate issue).
- **`safe_expr.reject_explosive`** no longer raises on three extreme-
  magnitude `Pow` shapes, violating this module's own documented contract
  that `classify_unsafe`/`safe_parse` never raise for hostile input. All
  three were a huge Python `int` reaching an unbounded int->float conversion
  or an unbounded int->str interpolation while the refusal itself was being
  computed or reported — not a gap in the refusal DECISION, which was
  already correct. Found by the 2026-08-22 ClusterFuzzLite batch:
  - `2**100000!!...` (`!!` is SymPy's double-factorial transform) made the
    exponent an exact Integer with thousands of digits; multiplying it by a
    float digit-count estimate coerced the int to float first, raising
    `OverflowError`.
  - `(x+1)**20000!!...` hit the same huge-exponent shape on a SYMBOLIC base,
    whose refusal MESSAGE interpolated the raw exponent directly — raising
    `ValueError` past Python's int->str conversion limit (4300 digits)
    before the message could even be built.
  - a base of 400 nines (`"9"*400 + "**12"`) exposed that SymPy's
    `Integer.__float__` returns `inf` for an oversized value instead of
    raising like Python's own `int.__float__` does, so the digit estimate's
    `try/except OverflowError` never fired; the resulting `inf` then broke
    `int(digits)` in the refusal message.
  All three now compute the digit/exponent magnitude via a `_log10_of_int`
  helper (exact to float precision — shifts an arbitrary-precision int down
  to its top 53 bits before ever calling `float()`, rather than a cruder
  `bit_length() * log10(2)` estimate, which was measured to flip some
  ordinary, well-under-the-cap powers like `2**10000` to a false refusal)
  and never interpolate the full value into a message, correctly returning
  a `resource_exhausted` refusal instead of crashing — verified not to
  waive the shape through (each is still refused, on 'digits'/'exponent'
  grounds). Added to `scripts/fuzz.py`'s `SEED_CORPUS_EXPR`.

  Cross-vendor (Codex) review of this fix caught two more in the same
  class, both fixed alongside it:
  - **off-by-one at `MAX_NUMERIC_DIGITS`**: the digit estimate IS
    `log10(result)`, not the digit count — a value with N digits has
    `log10` in `[N-1, N)`, so comparing the estimate itself against the cap
    admitted equality at the boundary. `10**4000` has 4001 digits but
    `log10(10**4000) == 4000` exactly, which is not `> 4000`, so it was
    incorrectly *allowed* — one digit over the cap the refusal message
    itself claims. Now compares `floor(digits) + 1` (the true count)
    against the cap; `10**3999` (exactly 4000 digits, genuinely at the
    boundary) is still correctly allowed.
  - **`classify_unsafe`'s heavy-function-argument refusal had the same
    unbounded int->str interpolation**, reachable independently of
    `reject_explosive`: `int(s, 0)` (base-0 auto-detect) parses a
    hex/octal/binary literal in linear time with no digit-count limit —
    unlike decimal text<->int conversion, which is exactly what carries
    the 4300-digit limit — so a short-looking token like `factorial(0x` +
    `f`*4000 + `)` parses to a plain int with thousands of DECIMAL digits,
    and the refusal message's own `str(value)` raised `ValueError` before
    it could report the refusal. Now falls back to a digit-count
    description only when `str(value)` would actually raise; an ordinary
    (small) literal's message is unchanged. Reachable with no length cap
    via `fuzz/safe_expr_fuzzer.py`'s atheris harness, which calls
    `classify_unsafe` directly. Added to `scripts/fuzz.py`'s
    `SEED_CORPUS_EXPR`.
- **`safe_expr.reject_explosive` closes the ceiling-coverage gap the fix
  above triaged and deliberately left open**: `2**(30000/2)`,
  `2**(-30000/2)` and `(3/2)**30000` now correctly refuse instead of
  silently computing an explosive value. `parse_expr(..., evaluate=False)`
  leaves `30000/2` as `Mul(30000, Pow(2, -1))` and `3/2` as `Mul(3, Pow(2,
  -1))` rather than folding them to a plain `Integer`/`Rational`, so the
  old `isinstance(exponent/base, (Integer, Float))` checks skipped them
  entirely and the real power got computed with no ceiling ever applied —
  a computed 4,516-digit `Integer` / a `Rational` with a 47,549-bit
  numerator, successfully, not a refusal. The six symbolic tools survived
  this only via each caller's own resource-error catch at stringification —
  a structured refusal, but at the wrong layer, after the full cost was
  already paid. A new `_bounded_numeric_value` helper resolves a
  numeric-only (no `free_symbols`) exponent or base subtree through the
  same `_log10_of_int`-based ceiling as a bare literal, but only via a
  small safe grammar of cheap combinators (`Integer`, `Rational`, `Float`,
  `Add`, `Mul`, `Pow` with an integer exponent) and only after checking
  each combination's bit-length against a budget BEFORE performing it —
  never after.
  A first version of this fix refused anything outside that grammar
  outright, which turned out to be too broad: a cross-vendor differential
  probe (main vs. the fix, over 17 inputs) found `2**cos(0)`, `2**(2*1.5)`,
  `2**(1.5+1.5)`, `(1.5*2)**3` and `2**(2**10000*2**-10000)` — all of
  which `main` evaluates fine (2, 8, 8, 27, 2) — got refused pre-parse
  instead. Fixed two ways: (1) `_bounded_numeric_value` now distinguishes
  returning `None` (INCONCLUSIVE — a `Function` call like `cos(0)`, an
  irrational constant, or float-mode overflow; not evidence of anything,
  so the caller falls through to the existing downstream screen rather
  than refusing) from a new `_NumericTooLarge` sentinel (PROVED via exact
  bit-length arithmetic to exceed the budget; still refused) — the earlier
  version conflated the two; (2) `Add`/`Mul` now check the bit length of
  the ACTUAL (SymPy auto-reduces via GCD) accumulator after each
  combination rather than a pessimistic pre-sum of each factor's own
  size, so `2**10000 * 2**-10000` (reciprocal factors, cancels to exactly
  1) resolves correctly instead of tripping the budget on the way there —
  and a `Float` anywhere in an `Add`/`Mul` chain now promotes the whole
  combination to ordinary bounded `float` arithmetic instead of refusing
  outright. The three genuine targets above still refuse; 12 additional
  boundary/edge inputs from the same differential probe (`10**3999`,
  `10**4000`, `2**10000`, `(1/2)**30000`, `sqrt(2)+1`, `(2/3)**5`,
  `x**(2*3)`, ...) are unaffected either way.
- **The same investigation found a separate parse-time memory bomb**:
  `"2**1000000^6c6/Me,"` returned the correct `('validation', 'parse
  error')` refusal, but only after allocating a 2977 MB tracemalloc peak
  (measured under a 3 GB `ulimit -v`; ClusterFuzzLite's own 2560 MB
  libFuzzer rss cap reported 1542 MB and OOM'd). A trailing comma is valid
  Python tuple syntax (`x,` means `(x,)`), so `parse_expr(..., evaluate=
  False)` hands back a bare Python `tuple` — not a SymPy node — wrapping
  the real `Mul(2**(1000000**6), ...)` shape. `safe_expr._walk`'s
  `getattr(current, "args", ())` found no `.args` on a plain tuple and
  stopped there, so `reject_explosive` never saw the `2**(1000000**6)`
  power tower buried inside and returned `None` as if the tree held
  nothing dangerous — the real, evaluating parse then computed
  `2**(1000000**6)` before the trailing comma's syntax error ever got a
  chance to surface. `_walk` now descends into a bare tuple's own elements
  directly; the input now refuses in milliseconds on power-tower grounds,
  under a 50 MB tracemalloc peak. All four inputs added to `scripts/
  fuzz.py`'s `SEED_CORPUS_EXPR`.

### Changed

- **Trimmed dev-history/rationale prose out of 3 heavy tool docstrings**
  (`execute_code_stream`, `install_package`, `runtimes_status`), gated by
  `scripts/tool_select_eval.py --baseline` so the trim could not quietly cut
  the vocabulary a model leans on to pick the right tool. Narrative moved to
  `#` comments right at the call site rather than deleted. `runtimes_status`'s
  `tier` paragraph, previously duplicated in full from `list_languages`, is
  now a one-line pointer to it (`list_languages` keeps the full explanation —
  it is the tool a model reaches for first to learn what `tier` means).
  Measured on the 52 live tool descriptions with `o200k_base` (matching the
  README's own "Tool-definition token cost" methodology): 4877 -> 4763
  tokens (-114, -2.3%; 19674 -> 19139 chars). Two further candidates in
  the same audit (`verify_translation`, `verify_optimization`) were trimmed
  and reverted:
  the removed prose scored as load-bearing for `tool_select_eval.py`'s
  baseline once BM25's corpus-wide length normalization was accounted for
  (`verify_translation` uniquely held the word "porting" in the entire
  52-tool corpus; removing it flipped an unrelated `solve_linear` prompt),
  so both stay unchanged. Full/dev/core eval: 118/96/63 top-1 hits
  (baseline 119/96/63, epsilon 1) and 149/118/77 top-3 (baseline
  148/117/77) — no regression under the gate's tolerance.

## [0.4.0] — 2026-08-21

### Added

- **`codecalc setup [--client=NAME] [--write]`**: guided onboarding
  from a clean install to a working MCP connection in one command, ending in
  a single verdict (`ready`/`degraded`/`not-ready`). Detects the calling
  client (`claude-desktop`/`claude-code`/`cursor`/`vscode`/`zed`) by probing
  each one's known config path, or takes `--client` explicitly when none or
  several are found; reuses `codecalc doctor`'s own executor-backend/extras/
  grammar-cache checks rather than re-deriving them; prints the exact MCP
  config block in the detected client's own shape (`mcpServers` for Claude
  Desktop/Cursor/Claude Code, `servers` for VS Code, `context_servers` for
  Zed) with absolute, machine-derived command/args (an absolute venv python
  for a source checkout, the resolved `codecalc` console script, or `uvx
  codecalc[full]`); runs a real `execute_code` and `evaluate_expression`
  canary in-process (no server spawned) to prove the connection would
  actually work. **Non-destructive by default**: without `--write` this only
  prints — the config file, the grammar-cache prefetch, and the skill copy
  are all untouched. `--write` merges the `codecalc` entry into the client's
  EXISTING config (every other server/setting passes through unchanged) and
  backs up the original to `<path>.codecalc-bak` first; an existing config
  file that is not valid JSON is refused rather than risked.
- **`CODECALC_TOOLS`: register a slice of the 52-tool surface**.
  `tools/list` costs ~9.2k tokens up front (see README "Tool-definition token
  cost"), and the fix that stayed deliberately unbuilt is a facade
  (`docs/design/2026-08-10-tool-facade.md`) — collapsing every tool behind one
  dispatcher erases per-tool typed schemas and per-tool approval prompts. This
  is a different mechanism: every `@mcp.tool()` in `server.py` now declares a
  `group=` (`calculator`/`verification`/`execution`/`sessions`/`analysis`/
  `admin`, 52 tools total, mapping in the new README section "Reducing the
  tool surface"), and `CODECALC_TOOLS` (comma-separated group and/or preset
  names — presets `core`/`dev`/`full`) restricts, at import, which groups
  `_tool()` actually hands to the MCP SDK. A tool outside the active set is
  never registered at all: absent from `tools/list` **and** rejected by
  `tools/call`, not a name a client could still guess and invoke through a
  hidden facade path. Unset/empty registers every group — 52 tools, unchanged
  default behaviour — and an unknown group or preset name is a loud
  `ValueError` at startup naming the bad value and every known group/preset,
  never a silent fallback to "everything" or "nothing" (either direction turns
  a typo into a footgun). `codecalc doctor` gained a `tool groups` block:
  active groups, the full group→tools mapping, and how many tools this
  process actually registered. Gated by the new
  `scripts/check_tool_groups.py`, wired into CI: statically asserts every
  declared tool names a known group, then re-derives — from a live subprocess
  import with `CODECALC_TOOLS` unset — that the default configuration still
  registers all 52, so the filter mechanism cannot silently shrink the
  no-configuration case the rest of CI's tool-count gates depend on.

- **Per-language RELIABILITY tiers**, orthogonal to the existing
  resolution states (`supported`/`installed`/`unhealthy`/`available`). A
  runtime could report `installed` (its command resolved on PATH) while its
  toolchain was actually broken — a review's own smoke test found the rust
  and csharp host toolchains failing on a machine where both commands
  resolved cleanly — and codecalc presented every resolved executable as
  equally operational. Every `codecalc/registry.py` language now carries a
  `tier`: `tested` (a CI job genuinely executes it and asserts on real
  output, on every PR — currently `python3` and `node` only, kept
  deliberately conservative), `best_effort` (declared, plausibly works on a
  normal install, never CI-checked — every other language, `rust` and
  `csharp` included), or `plan_only` (never validated on any runner, none
  today). Surfaced as a new `tier` field on `list_languages`,
  `runtimes_status`, and `codecalc doctor`'s `runtimes`/`tier_summary`
  (`list_execution_providers`' docstring now cross-references it — provider
  descriptors are about execution BACKENDS, not per-language reliability, so
  there is no per-provider field to add); `codecalc doctor`'s text output
  prints a `reliability tier` block naming every non-`tested` runtime as
  "toolchain may be broken; not exercised by codecalc's CI" so a resolved
  runtime never reads as equally trustworthy to a genuinely CI-verified one.
  Gated by the new `scripts/check_runtime_tiers.py`, wired into CI: it
  derives the `tested` set from the literal source of the two files that
  actually wire a language into CI (`tests/test_python_sweep.py`'s
  `WORKER_LANGS`, `scripts/contract_check.py`'s `CANDIDATES`) rather than
  trusting a hand-maintained list, so a language cannot claim `tested`
  without a CI check backing it, or silently drop out of CI while still
  claiming it. `contract_version` moves `1.2.0` → **`1.3.0`** (a MINOR add):
  `tier` on every `doctor` `runtimes` entry, plus a `tier_summary` block —
  the execution result shape itself is unchanged.

- **Per-session and global disk quotas for sessions**. Nothing
  previously bounded the TOTAL disk a session accumulates — only per-stream
  (`SPILL_CAPTURE_KB`, 4 MiB) and per-served-file (`RESOURCE_MAX_BYTES`,
  4 MiB) ceilings existed, so a session could fill the operator's disk one
  small write at a time. Five new, generous-by-default knobs close it:
  `CODECALC_SESSION_DISK_QUOTA_MB` (default 512), `CODECALC_TOTAL_DISK_QUOTA_MB`
  (default 8192, across every session workspace), `CODECALC_MAX_ARTIFACT_BYTES`
  (default 16 MiB, per write) and `CODECALC_MAX_ARTIFACT_COUNT` (default 500,
  per session), and `CODECALC_MIN_HOST_FREE_MB` (default 256, refuses a write
  when the host itself is low regardless of how generous the quotas above
  are). `session_write_file` and oversized-output spilling are checked BEFORE
  every write (`resource_exhausted`, no partial file ever written); code run
  via `execute_code(session_id=...)` or `session_run` is checked before it
  starts and, since executed code's own writes cannot be pre-checked, again
  after it finishes — an over-quota run still returns its real result, now
  carrying `disk_quota_exceeded` plus the measured usage/limit, and the
  session's next write or run is refused for as long as it stays over the
  line (re-measured fresh each time, so freeing space un-refuses it on its
  own — no flag to reset). `codecalc doctor` reports the configured limits
  and current per-session/global usage under a new `disk_quota` section.

  An adversarial review of the above found four enforcement gaps, all
  closed in the same change: `install_package` — the largest write vector
  of all, MB to GB per call — carried no quota check whatsoever, so an
  over-quota session installed freely; it now gets the same precheck (before
  the package manager runs) and postcheck (disclosing an install that pushed
  a session over quota) `execute_code`/`session_run` already had. The
  per-session artifact-COUNT cap only ran from this module's own writes, so
  code executed via `execute_code`/`session_run` could create arbitrarily
  many files — each individually under the byte quota — with nothing to
  catch the total; `disk_quota_exceeded`'s postcheck/precheck pair now
  carries the same disclose-then-block shape for the file count. An
  overwrite's disk-usage check compared the FULL new size against usage that
  already counted the file being replaced, wrongly refusing a same-size or
  shrinking overwrite near quota — fixed to compare the NET size delta.
  Finally, since `execute_code`/`session_run` are themselves refused once a
  session is over quota (so executed code cannot free space by running
  `rm`), a net-non-positive write (shrinking or same-size) is now ALWAYS
  permitted regardless of current usage — the in-band recovery path that
  makes `session_stop` no longer the only way out of a stuck session.

- **`codecalc status` and `codecalc cleanup`: operator-facing session
  disk-usage commands**, the operational half of the per-session disk
  quotas. `status [--json]` is a read-only snapshot — `SESSION_ROOT`,
  session count, per-session/global workspace disk usage, which sessions
  are idle-expired (the on-disk `.codecalc-session-expired` marker), the
  configured quotas and current headroom, the audit log's path and size,
  and a one-line runtime reliability-tier summary — that changes nothing.
  `cleanup [--dry-run|--write] [--include-unmarked]` reclaims disk from
  session directories under `SESSION_ROOT`; `--dry-run` is the DEFAULT and
  removes nothing, `--write` is the one flag that actually deletes. Both
  are CLI-only (`server.py`'s `main()` dispatch, logic in the new
  `codecalc/ops.py`) — neither is an MCP tool, so neither counts against
  the 52-tool surface.

  An adversarial review of the first version found a CRITICAL bug: it
  trusted session-directory MTIME as a liveness signal, and that signal is
  false — a REPL worker doing purely in-memory work touches no file, and
  even an in-place file overwrite bumps only the file's own mtime, never
  its parent directory's. Proven live: a genuinely-active worker session's
  workspace was removed out from under it. Fixed with a REAL liveness
  signal, a per-worker-session lockfile (`sessions._LOCK_FILE_NAME`) the
  server writes carrying its own pid at worker start and releases the
  moment that worker is actually gone (reaped or `session_stop`); `cleanup`
  refuses ANY candidate whose lock names a still-live pid, regardless of
  marker, age, or the mtime floor, cross-platform (`os.kill(pid, 0)` on
  POSIX, `OpenProcess`/`GetExitCodeProcess` on Windows — no `os.kill(pid,
  0)` equivalent exists there). Since a workspace-only session never holds
  a lock (no worker to protect), the marker-less age-based path is now
  OPT-IN (`--include-unmarked`) rather than default: `cleanup` alone only
  ever considers the on-disk `.codecalc-session-expired` marker, which
  carries no such residual risk (a marker only exists once sessions.py's
  own idle-TTL reaper has already closed that worker for good — session
  ids are never reused). `--include-unmarked` additionally sweeps old
  (`CODECALC_CLEANUP_ABANDONED_AGE_HOURS`, default 24h), session-shaped
  directories, still gated by the same lockfile check plus a hard recency
  floor (nothing modified in the last few minutes is ever touched).

  `cleanup` runs as a separate process from any server using
  `SESSION_ROOT`, so it has none of that server's in-memory bookkeeping to
  consult beyond the lockfile above; it remains deliberately conservative
  otherwise: only a direct child of `SESSION_ROOT` is ever a candidate,
  never `SESSION_ROOT` itself; a symlink there is refused, not followed;
  and removal goes through the same device/inode
  identity-check-immediately-before-delete discipline `session_stop`'s own
  workspace teardown already uses, re-verified at delete time rather than
  trusted from the scan (inode reuse in that window is an accepted
  residual, the same one `session_stop` already carries). README and
  `--help` now warn explicitly: keep `CODECALC_SESSION_ROOT`
  codecalc-private — the "looks like a session dir" name filter is loose,
  not strict.

- **Audit-log size-based rotation**. `AuditLog` (`codecalc/audit.py`)
  appended to one file forever — the same unbounded-growth gap already
  closed for session workspaces, here for the append-only broker-decision
  trail. `CODECALC_AUDIT_MAX_MB` (default 10 MiB, read fresh per call, same
  unset/invalid-safe shape as `CODECALC_SESSION_IDLE_TTL_SECONDS`) now caps
  the live file; crossing it rotates `audit.log` → `audit.log.1` →
  `audit.log.2` (a small, fixed 2-generation history — the same
  bounded-count-oldest-dropped shape session spill files already use) and
  starts a fresh file. Checked and performed inside `emit()`'s own existing
  try/except: a rotation failure is swallowed exactly like an ordinary
  write failure — it can never fail the run it is describing.
- **`codecalc --help` and `codecalc --version`** (GH #201). Both used
  to print nothing and exit 0 — `main()` recognised `doctor`/`serve-strict`/
  `serve-http` but treated the flags as "no subcommand" and started the stdio
  MCP server. They now print a usage block (naming the subcommands and that the
  default with no argument is the stdio server) / the version, and exit 0
  without starting anything.
- **`codecalc-prefetch-grammars`, a console script that warms the tree-sitter
  grammar cache** (GH #200). The offline warm-up the README told
  installed users to run lived only in `scripts/`, which the wheel does not
  ship — so the documented command did not exist for anyone who `pip
  install`ed rather than cloning. It now ships as a `[project.scripts]` entry
  point (`codecalc-prefetch-grammars`, `--print-cache-dir`) calling the same
  code the source script does.
- **`matrix` — structured matrix operations** (GH #223): det,
  inverse, eigenvalues, transpose, rank, trace. `evaluate_expression` has
  always refused `Matrix([[1,2],[3,4]])` with `"'[' is not permitted in an
  expression"` — `[`/`]` are denied at the token level to block a
  subscript-based RCE escape (`().__class__.__bases__[0]`), and a matrix
  literal was collateral from that correctly-aimed screen. `matrix` is the
  structured fix: `rows` arrives as a JSON array of arrays, never a caller
  string parsed through sympify, so the RCE screen never applies to it in
  the first place. Each entry is either a JSON number, used directly, or a
  scalar expression string screened individually through the same
  `classify_unsafe` check `evaluate_expression` uses, before it ever reaches
  SymPy — a malicious entry like `"().__class__"` is refused per entry with
  `permission_denied`, exactly as `evaluate_expression` refuses the same
  string. Non-rectangular and non-square (for det/inverse/eigenvalues/trace)
  input is rejected with a clear message; a singular matrix passed to
  `inverse` returns a clean `validation` error instead of a traceback.
  **51 → 52 MCP tools.**

### Changed

- **`evaluate_expression`'s `'[' is not permitted` refusal now names the
  `matrix` tool** (GH #223, modelled on GH #209's `bit_analysis`
  message style). What is refused is unchanged — `[`/`]` are still denied
  for the same RCE reason — only the message improves, from a bare `"'['
  is not permitted in an expression"` to one that says a matrix literal
  belongs in the new `matrix` tool instead.

### Security

- **A workspace-guard refusal (an out-of-workspace path, a malformed session
  id) now carries the full result contract** (#212a). Before this,
  `session_write_file`/`session_files`/`session_read_file`/`session_run`/
  `session_stop`/`execute_code(session_id=...)` REFUSED such a request by
  raising, uncaught, all the way past `SessionService` and the `@mcp.tool()`
  wrapper — a caller got a bare protocol-level error instead of the
  `ok`/`code`/`remedy` shape every other rejection in this package carries.
  These now return `{"ok": false, "code": "permission_denied"` (an escape) or
  `"validation"` (a malformed id) `, "remedy": ...}`, same as any other
  refusal.
- **A pydantic argument-validation error no longer echoes the caller's raw
  value** (#212b). A wrong-typed tool argument is rejected by the
  MCP SDK's own schema validation before a tool body ever runs, and its
  error text included `input_value=<exactly what was passed>` verbatim — a
  potential info leak into logs/transcripts. A new server middleware strips
  that bracketed diagnostic from the error text before it leaves the
  process; the field name and reason are kept.
- **`serve-http`'s DNS-rebinding protection now matches codecalc's own
  loopback allowlist** (#211). codecalc accepts any address in
  127.0.0.0/8 plus `::1`/`localhost`/`ip6-localhost` as loopback-safe (no
  `CODECALC_HTTP_TOKEN` required), but the MCP SDK's own DNS-rebinding
  auto-default only recognises the three literal strings
  `"127.0.0.1"`/`"localhost"`/`"::1"` — anything else codecalc accepted (for
  example `127.0.0.2`) silently got NO DNS-rebinding protection at all.
  `serve-http` now builds `TransportSecuritySettings` explicitly from the
  same host it just validated, so a rebinding `Host:` header is rejected
  (421) on every bind codecalc itself considers safe.
- **`session_files` no longer stats through a symlink** (#208). A
  session could plant a symlink pointing outside the workspace, and the
  listing reported the TARGET's size — disclosing the existence and size of
  a path `session_read_file` already refuses to touch. A symlink entry is
  now reported as `{"type": "symlink"}`, never followed to describe what it
  points at; `session_artifacts` excludes symlinks from its listing for the
  same reason.
- **A backgrounded descendant survived a NORMAL exit** (GH #207). The
  process-group/job kill only ever ran on the timeout/overflow path — a
  payload that spawned a detached child (`subprocess.Popen(['sleep',
  '1000'])`) and returned 0 hit neither, so the child outlived the run with
  no wall clock on it at all. Both backends now reap the whole group after
  EVERY exit, not only a timed-out one: the Rust executor unconditionally
  `killpg`s the child's process group after its `wait4` loop, on Unix, and
  the Python fallback does the same via a new `_reap_group` (Windows job
  objects were already correct here — `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
  reaps the whole job when its handle closes, on any exit path). The Python
  fallback's spawn also picked up `CREATE_NEW_PROCESS_GROUP` on Windows,
  where it was previously relying on `start_new_session`, a POSIX-only flag
  that Windows silently ignores.
- **`max_output_kb` enforced a ~1 MiB floor regardless of the request**
  (GH #206). The Rust executor's RLIMIT_FSIZE — the ceiling on how much the
  sandboxed child is actually allowed to write before being stopped — was
  computed as `max_output_kb * 1024 * 4` clamped to a **1 MiB floor**. A
  caller passing `--max-output-kb 1` got an enforced ceiling near 1 MiB
  (1024x the request), undisclosed in `unenforced`; measured, `stdout_bytes`
  came back `1048576` for a program that printed 5 MB. The RETURNED `stdout`
  text was always correctly capped at the literal request (`read_capped`
  truncates independently, with no floor) — only the underlying write
  ceiling was wrongly sized. The floor is now 4 KiB, which no longer binds for
  any `max_output_kb >= 1` (the existing 4x headroom always clears it on its
  own), so the enforced ceiling stays a small, proportional multiple of the
  request.
- **`solve_linear` no longer parses caller input through a live-builtins
  `sympify`**. Each side of each equation (and the single-expression
  no-`=` path) reached `sp.sympify(...)` directly, which uses SymPy's DEFAULT
  `global_dict` (`vars(builtins)` copied in) and skipped the unevaluated-shape
  check `evaluate_expression` already runs. `solve_linear('x = input()', 'x')`
  called the REAL `input()`, reading the child's stdin — shared fd 0 with a
  stdio MCP server — and `solve_linear('x = 9**9**9**9', 'x')` burned real CPU
  seconds evaluating a power tower blind, riding the guard's own timeout
  instead of a fast refusal. Both pieces now parse via `parse_expr(global_dict=
  safe_global_dict())` with `reject_explosive` run on the unevaluated shape
  first — the same fix gave the `matrix` tool's per-cell parse.
  `input()`/`breakpoint()`/`quit()` now parse to a clean error (matching
  `evaluate_expression`'s own outcome for the same string) instead of being
  called, and a power tower is now `resource_exhausted` in milliseconds
  instead of burning CPU seconds.
- **`algebraic_equiv`, `solve_expression`, `limit_expression` (including its
  `point` argument) and `simplify_expression` no longer parse caller input
  through a live-builtins `sympify`**. Each reached a bare
  `sp.sympify(...)` after `classify_unsafe` — the same gap closed
  in `matrix` and `solve_linear`: a screened, non-denylisted NAME can still
  be a live Python builtin. `simplify_expression('input()')` called the REAL
  `input()`, reading the child's stdin — shared fd 0 with a stdio MCP
  server — and `algebraic_equiv('9**9**9**9', '0')` burned real CPU seconds
  evaluating a power tower blind. All four now parse via a new shared
  `safe_expr.safe_parse()` — `parse_expr(global_dict=safe_global_dict())`
  with `reject_explosive` run on the unevaluated shape first — extracted
  from the three near-identical copies of this same pipeline that
  `logic._evaluate_expression`, `logic._parse_solve_piece` and
  `linalg._parse_entry` each hand-rolled; all three now delegate
  to the shared helper too, dropping the duplication (their own result
  shapes are unchanged), so `safe_parse` is the single place in the package
  that hands a caller string to SymPy's parser and a future caller cannot
  reintroduce this gap by doing `classify_unsafe` and forgetting the safe
  parse. `input()`/`breakpoint()`/`quit()` now parse to a clean error
  (matching `evaluate_expression`'s own outcome) instead of being called,
  and a power tower is now `resource_exhausted` in milliseconds instead of
  burning CPU seconds.
- **Side effect of the above: the four `exact.py` tools now parse with
  implicit multiplication**, the same `parse_expr` transformation
  `evaluate_expression`/`matrix`/`solve_linear` already enable, in place of
  `sp.sympify`'s stricter grammar. Ordinary input that used to require an
  explicit `*` is now accepted and reinterpreted as multiplication:
  `2(x+1)` (previously a parse error) now parses as `2*x + 2`, and
  function-call notation on an undeclared name — `f(x)` (previously the
  applied function `f(x)`) — now parses as `f*x`. Concretely,
  `algebraic_equiv('x(x+1)', 'x*(x+1)')` now reports `identical: true`; on
  the old `sp.sympify` grammar the left side was the applied function
  `x(x+1)`, not a product, and the two were not identical. This mirrors
  `evaluate_expression`'s existing behaviour for the same input and is a
  side effect of closing the parse gap above, not an independent feature.

### Fixed

- **A guard or policy refusal classified as `internal`, indistinguishable from
  an unhandled crash** (GH #214, the follow-up to an earlier
  argument-validation fix). `evaluate_expression`/`simplify_expression`/
  `solve_expression`/`solve_linear`'s guarded-evaluation allowlist
  successfully blocking a sandbox-escape attempt (`__import__(...)`,
  `.__class__.__bases__`, a string literal) now returns `"code":
  "permission_denied"` instead of `"internal"` — `INTERNAL`'s remedy is "a
  defect in codecalc; the message is worth reporting verbatim", which told an
  operator that a successfully blocked attack was a bug to file. A power-tower
  or oversized-exponent refusal (`evaluate_expression("9**9**9")`,
  `calc_exact("9**9**9")`) now returns `"resource_exhausted"` — a ceiling, not
  a defect. `calc_exact("1/0")` now returns `"validation"` with the message
  "division by zero" instead of leaking `ZeroDivisionError`'s constructor
  argument (`"Fraction(1, 0)"`) as if it were a sentence.
  `install_package(language="ruby", ...)`'s documented unsupported-language
  refusal now returns `"permission_denied"` instead of `"internal"`. The
  guarded-evaluation allowlist's screen (`safe_expr.py`) actually refuses two
  DIFFERENT things through one message string — the RCE token/keyword screen
  above, and, separately, a heavy-argument ceiling (`factorial(100000)`,
  `binomial(200000, 100000)`) that was already correctly `resource_exhausted`
  before this change and still is: `classify_unsafe` now names which of the
  two a given rejection is, so the security half moved to
  `permission_denied` without dragging the ceiling half along with it. Each
  code is chosen at the point the refusal is decided, not guessed back out of
  the message by `ensure_code` — the same raise-site principle already
  established, so a future refusal worded a new way cannot silently default
  to claiming a codecalc defect again.
- The MCP server's `instructions=` metadata said "30+ languages"; every
  other surface (README, SECURITY.md, the repo description) said the actual
  count, 31. `scripts/check_claims.py` now gates this string too
  (#213a).
- `data_sizes(n)` accepted a negative `n` and reported negative KiB/MB
  instead of rejecting it — the same bug shape `human_duration` already
  guards against for a negative duration. It now returns a validation error
  (#213b).
- `percentage`, `percentiles`, `collision_probability` and `human_duration`
  presented a `round()`ed float beside an exact one (a fraction string, an
  unrounded probability, the echoed input) with nothing marking which was
  which. Each result now carries a `"rounding"` field naming exactly which
  of its own keys were rounded and to how many decimal digits
  (#213c).
- **Ten tools blamed the caller for their own bad input** (GH #196).
  An argument-validation rejection — `percentage(total=0)`, `percentiles([])`,
  `calc_stats([5])`, `collision_probability(bits=0)`, `human_duration(-5)`,
  `epoch_time(-1)` among them — returned `code: internal`, whose remedy reads
  "a defect in codecalc; the message is worth reporting verbatim". These tools
  `return {ok:false}` rather than raise, so `errors._from_message`'s substring
  matching is the classification layer; its hint list gained the missing
  validation phrases ("at least"/"is zero"/"negative"/">="/"unknown") so a
  caller's bad argument classifies as `validation`, not a codecalc defect.
  (above finished the other half — the guard/policy refusals.)
- **`bit_analysis` on a negative `n`** (GH #197). `bit_analysis(-1)`
  counted `abs(n)` — reporting `popcount: 1` while its own `is_power_of_two`
  said false — and silently dropped `next_power_of_two`. A negative `n` is now
  a validation error pointing at `bitop(width=W)`; `n=0` discloses
  `next_power_of_two: None` rather than omitting the key.
- **`human_duration` sub-second and very-large inputs** (GH #198).
  `human_duration(0.5)` returned `"0s"` (losing everything under a second) and
  `human_duration(1e30)` emitted 26 digits off a 17-digit float — precision the
  float never held. Sub-second inputs now surface as `ms`/`µs`, and values at
  or beyond 2^53 are capped to a scientific form instead of fabricating digits.
- **`solve_linear` crashed on a single-variable system**:
  `solve_linear('2*x = 4', 'x')` raised `'Symbol' object is not iterable`.
  `sp.symbols(names)` returns a bare `Symbol` rather than a 1-tuple unless
  given more than one name or a trailing comma, and the code downstream
  assumed a sequence (`list(syms)`) unconditionally. `sp.symbols(...,
  seq=True)` now always returns a sequence, one variable or many, so
  single-variable systems solve like every other case; multi-variable
  behaviour is unchanged.

## [0.3.1] — 2026-08-20

### Fixed

- MCP registry publishing: `server.json`'s `description` shortened
  to the registry's 100-char limit (was 117, causing a 422), the namespace
  corrected to the GitHub org's actual casing (`io.github.The-40-Thieves/codecalc`,
  the OIDC grant is case-sensitive and rejected the lowercase form with a
  403), and the release workflow's `publish-mcp-registry` job made
  retriable — it now runs whenever the PyPI publish succeeded or was
  already skipped, so a registry-only re-dispatch can publish against an
  already-live PyPI release. This release exists to carry the corrected
  `mcp-name` marker into a fresh PyPI package description, since the
  marker check the registry runs against the published description is
  case-sensitive and the 0.3.0 description still had the old casing.

## [0.3.0] — 2026-08-20

### Added

- **A versioned extension SDK — language packs, renderers, verifiers.**
  Every extension kind now has a versioned `Protocol`, a registry
  enforcing identity/no-impersonation, interface-major compatibility, a
  permission allowlist and a `CODECALC_DISABLE_THIRD_PARTY_EXTENSIONS` kill
  switch, plus integrity verification wired into `register()`. Each kind
  ships a built-in reference implementation and a reference third-party
  extension as its own second-consumer conformance check; `doctor` gains an
  `extensions` discovery block and `docs/extensions/README.md` documents the
  trust model (trusted-by-installation, not a sandbox).
- **Strict gVisor runtime forwards program `stdin` to the guest**,
  mirroring the local Rust executor path via a read-only bind-mounted
  per-run file.
- **One-command Linux strict bootstrap**, `scripts/setup-strict.sh`
  — preflight, pull the pinned image, run the deep `gvisor-v1`
  canary, then launch.
- **MCP registry metadata** — `server.json`, an `mcp-name` PyPI-ownership
  marker, and a `uvx codecalc` client install snippet, preparing
  repo-side discovery readiness for the registry publish (the
  `mcp-publisher` publish itself is owner-gated and separate).
- **Operator deployment runbook**, `docs/deployment/README.md`,
  covering all three strict backends (Linux/gVisor+Docker,
  Windows/AppContainer, macOS/remote-Linux) plus startup canary, orphan
  recovery, fail-closed causes and quota tuning.
- **`scripts/win-verify.ps1`**, a one-command native-Windows verification
  bootstrap that builds the executor and runs every Windows sandbox probe
  with timestamped output.

### Changed

- **Strict service forwards `max_cpu` to the guest's `cpu_count`, clamped to
  `MAX_CPU_COUNT`**. It previously silently dropped the field, so
  every remote strict run used the default `cpu_count=1.0`.
- **README leads with a value proposition and a "when to use codecalc"
  section**, honest about when a hosted sandbox or the vendor's
  own interpreter is the better fit.

### Fixed

- `compare_execution`: a cold-start timeout on one language no
  longer ends the story for that language. If a run comes back `timed_out`,
  it gets exactly one warm retry with the same arguments; a recovered retry
  is reported as the row's result (with `cold_retry`, `cold_retry_recovered`,
  and `first_attempt_ms` added). A timeout that persists after the retry is
  always surfaced in `discrepancies`, with `sibling_durations_ms` for every
  other successful language and a `variance_note` that compares them — a
  wide spread (>=10x) reads as runner-wide slowness/cold-start pressure
  rather than a defect in the timed-out language's snippet, a tight band
  reads as specific to it. This addresses the case where `node`, the
  heaviest cold-starter, is first to cross the fixed wall-clock deadline on
  a globally slow GitHub Actions windows-latest runner (one repro: ruby at
  3452ms vs python at 33ms for a trivial snippet). `executor.execute`'s
  timeout remains an unchanged hard limit — only `compare_execution`'s
  handling of a `timed_out` result changed. A deterministic non-timeout
  failure (e.g. a compile error) is never retried.
- **Windows CI Defender path exclusions** cut interpreter
  cold-start scanning, the dominant contributor to the flake class.

### Docs

- README no longer carries the pre-publish warning — it reflects the live
  `0.2.0` release on PyPI and crates.io.

### Removed

- **Dropped the unused `serde` direct dependency from the executor crate**
  — only `serde_json` was ever used directly; trims the
  untrusted-code binary's dependency surface.

## [0.2.0] — 2026-08-19

The first release cut for publication to PyPI and crates.io. It changes the
**tool surface** (48 tools → 51) and adds to the **result
contract**, so it is a MINOR bump, not a patch. The
`contract_version` moves `1.0.0` → **`1.2.0`** for the same reason: `1.1.0`
ADDED a fifth result shape (`run_lifecycle`, for the background-run tools) and
fields (an execution receipt with `session_id`, grade metadata), and `1.2.0`
ADDS a `strict_runtime` prerequisites block to the `doctor` diagnostic document.
Additions are exactly what the contract's own policy defines as a MINOR bump — a
compatible addition bumps MINOR, it does not leave the version unchanged. A
`1.0.0` client keeps working against `1.2.0`.

### Added

- **`install_package` confinement extended to macOS**. Layer 2 of
  the #23 mitigation — bounding what the installer *binary itself* can reach
  on disk, on top of Layer 1's "do not run its install-time code at all" —
  was Linux-only (`landlock.abi_version()` returns 0 off Linux), leaving
  macOS and Windows installs running with the server user's full filesystem
  access behind an honest but unenforced disclosure token. macOS now gets a
  real boundary via `sandbox-exec` (`codecalc/sandbox_macos.py`): a generated
  Seatbelt profile scopes both reads and writes to the workspace, its
  redirected caches and a curated set of system paths, denying everything
  else — network stays open (an install needs it) and is reported, not
  enforced, via the same `install_tcp_egress_unrestricted` /
  `install_udp_egress_unrestricted` / `install_metadata_syscalls_unrestricted`
  tokens Linux already emits. `tests/test_package_isolation.py` runs the same
  write-outside-refused / canary-outside-unreadable assertions the Linux
  Landlock probe uses, gated to execute on `darwin` (CI's `macos-latest` leg)
  and to SKIP with a recorded reason everywhere else — the mechanism is
  proven by macOS CI, not argued from documentation. Windows gets no claimed
  confinement (the AppContainer job-object work is unverified on real Windows 11)
  but a documented no-op opt-in, `CODECALC_WIN_INSTALL_CONFINE`, adds a
  `package_install_confinement_unverified_on_windows` disclosure without
  claiming enforcement; the base `package_install_not_confined_no_landlock`
  disclosure keeps firing on Windows exactly as before.

- **Linux strict gVisor boundary made real**. A real executor
  container image (`docker/executor.Dockerfile`, multi-stage/minimal/non-root,
  carrying `codecalc-exec` + `blocknet.so` + python3);
  `DockerGVisorRuntime.recover_orphans()` reconciles owned strict containers by
  their immutable run-identity label when the remote strict execution service
  (which lives out of this repo) invokes it at that service's startup; `doctor`
  now CALLS the
  runtime's host probe and surfaces measured prerequisites in a `strict_runtime`
  block (Docker present, cgroup v2, `runsc` registered, image present, and a
  real startup canary under `--deep`), failing closed with a structured reason
  on a host without `runsc`. A hostile-workload conformance suite
  (`tests/test_gvisor_conformance.py`) launches the image under `--runtime=runsc`
  and proves fork-bomb/memory-bomb/descendant-escape/egress/filesystem
  containment on a runsc host, verifying the runtime OUT OF BAND. It skips
  without `runsc` (GitHub CI), and runs via `scripts/gvisor_conformance.sh` on
  Cave / a runsc host. The registry-published image residual is now closed (see
  the next entry); GitHub-CI-under-runsc remains out of reach — see
  `docs/contract/provider-v1.md`.

- **Published, digest-pinned strict executor image** (image residual).
  The `publish-executor-image` workflow (`workflow_dispatch`) builds
  `docker/executor.Dockerfile` for `linux/amd64` + `linux/arm64` (buildx + QEMU),
  pushes it to `ghcr.io/the-40-thieves/codecalc-exec` (GHCR, `GITHUB_TOKEN` with
  `packages: write` — no external secret), and commits the immutable index digest
  into `docker/executor-image.lock`, pushing it back to the branch. New
  `strict_runtime.published_strict_image()` resolves that lock (then a
  digest-pinned `CODECALC_STRICT_IMAGE`) as the production default, and
  `strict_execution_config()` builds the `GVisorConfig` from it. When no digest is
  published yet — the shipped state until the first dispatch — the execution path
  **fails closed** with `StrictImageUnavailable`, never falling back to the mutable
  local diagnostic tag `codecalc-exec:strict`, which `doctor` and the conformance
  suite keep using unchanged. The actual publish and first digest-pin still require
  an operator `workflow_dispatch` (there is no push/PR trigger).

- **Background runs: `run_submit`, `run_inspect`, `run_cancel`**.
  Submit code and get a `run_id` back immediately instead of holding an MCP
  call open for the whole computation; poll with `run_inspect`, stop early with
  `run_cancel`. Cancellation is honest about a provider that cannot cancel, and
  a cancelled run's result is still collectible rather than stranded. Admission
  is capped (`CODECALC_MAX_ACTIVE_RUNS`, default 64) and past the cap a
  submission is refused with `resource_exhausted` rather than growing the run
  table without bound.
- **Oversized session output spills to a workspace artifact**.
  Session output that the default 64 KiB cap would have truncated and DROPPED
  is captured up to 4 MiB and written into the session workspace instead;
  `stdout_spill` / `stderr_spill` carry a `codecalc://session/{id}/files/...`
  URI, and `stdout_spill_capped` / `stderr_spill_capped` say outright when the
  spill is fuller than the inline value but still not the whole stream. The
  inline value is byte-for-byte what it always was.
- **An execution receipt** naming WHAT ran and under WHICH conditions,
  alongside a published `ComputationSpec` schema with a content hash,
  so two runs of the same request are identifiable as such.
- **A grade vocabulary for `verify_*` results**. `z3_check`'s grading
  is narrowed to unsat-only rather than reading a `sat` answer as a proof.
- **Idle-expiry for abandoned stateful sessions**.
  `CODECALC_SESSION_IDLE_TTL_SECONDS`, unset by default: a session untouched
  for longer than this has its worker reaped on the next access, and a
  subsequent call gets `ok: false` with the stable `worker_failure` code —
  never a silent respawn. Every session entry point counts as a touch, not only
  `execute`.
- **A deny-by-default operator allowlist for package installs**.
- **A capability broker, deny-by-default network, and an audit stream**.
  A small policy layer whose one invariant is that the capabilities policy
  APPROVES for a job never exceed the ones the requester REQUESTED. Applied
  identically on the synchronous (`execute_code`/stream/session) and background
  (`run_submit`) paths — a policy the sync path enforces cannot be bypassed by
  moving the job to the background. Off by
  default (`CODECALC_CAPABILITY_POLICY` unset = today's behaviour). Set it and
  `deny-network` flips the default to network-denied unless a job requests and is
  granted network; `strict` refuses a job whose denial the provider cannot
  enforce; an escalation (policy granting a capability the request did not ask
  for) is refused with a stable `permission_denied` /
  `capability_not_requested`. The four sets — requested / approved /
  provider_supported / effective — are surfaced on a new `capabilities` block in
  the execution receipt (`receipt_version` `1.1.0` → **`1.2.0`**, a MINOR add
  inside the receipt; the result `contract_version` is unaffected because the
  block lives under the un-schema'd `provider` receipt). Broker decisions and
  security-relevant side effects (denied capability, refused install, cleanup)
  are appended to an audit stream at `~/.codecalc/audit/audit.log`
  (`CODECALC_AUDIT_LOG` relocates or disables it), each event source-safe
  (injected clock) and redacted of secrets.
- **A gate on the README's own gate-script count**, so the count of
  CI-invoked scripts cannot drift from the workflows the way the tool count
  once did.

### Fixed

- **Strict `/v1` service pinned a worker thread on a slow-drip body (slowloris)**.
  The pre-auth body read (`rfile.read(length)`, after the 1 MiB
  `MAX_CONTENT_LENGTH` check) had no read timeout, so a client that declared a
  legitimate sub-cap `Content-Length` then dribbled the bytes blocked a
  `ThreadingHTTPServer` worker indefinitely — before `dispatch()`, and unbounded
  by `MAX_CONCURRENT_RUNS`. `_read_body` now enforces a TOTAL wall-clock deadline
  (`MAX_BODY_READ_SECONDS`, 10s), recomputed each iteration over `rfile.read1()`
  so a slow trickle cannot slip under a per-`recv` gap; expiry drops the
  connection and frees the thread. The 413 oversized path and auth-gate ordering
  are unchanged.
- **Non-strict `deny-network` hard-errored on a provider that cannot enforce it**.
  The broker forced `no_net` onto the run whenever `network` was
  denied, regardless of whether the selected provider could enforce it. A
  provider that RAISES on an unenforceable `no_net` — the Piston adapter, whose
  network toggle is a server setting, not a per-request control — then returned a
  `validation` error, so a network-requesting job routed to it hard-errored
  instead of running-with-disclosure, contradicting the contract's "disclosed as
  effective where it cannot enforce." `capabilities.enforced_spec` now forces
  `no_net` only where the provider declared `network_control`; under a non-strict
  policy an unenforceable denial leaves the request as-asked and discloses the
  leak (`network` stays in the receipt's `effective` set). STRICT policy is
  unchanged — an unenforceable denial is still rejected before any side effect —
  and the native Linux shim path still enforces and blocks egress. No result
  shape or `contract_version` change: the disclosure already lived in the
  `effective` set.
- **The spill path wrote and deleted outside the session workspace.** The three
  spill helpers resolved the `.codecalc-spill` directory without the `_jail`
  guard every other session path uses. `mkdir(exist_ok=True)` does not follow a
  symlink at the final component, so executed code — which owns the workspace
  as its cwd — could replace that directory with a symlink and get both an
  arbitrary-location file CREATE and, through the retention prune's `*.bin`
  glob, an arbitrary `*.bin` UNLINK, in the unsandboxed server process.
- **A spill the server wrote could be impossible to read back.** The capture
  ceiling counts raw stream bytes; the file written is the `errors="replace"`
  re-encoding, where one invalid byte becomes a 3-byte U+FFFD. The write is
  now bounded by the same constant the resource read enforces, so any spill
  that exists is fetchable in full.
- **`CODECALC_MAX_ACTIVE_RUNS` set-but-empty crashed the server at import.**
  Empty, non-numeric and non-positive values now fall back to the default with
  a message on stderr, instead of `int("")` raising where nothing catches it.
- **A set-but-empty package allowlist denied all rather than allowing all.**

#### Cross-vendor review fix wave (correctness / API-design)

A second cross-vendor (Codex) review of the integrated branch found ten issues;
these nine were in this branch's diff and are fixed here (the tenth —
`optimization.py` accepting a candidate against an unvalidated `min_speedup ≤ 1`
— is pre-existing and out of this diff, ticketed separately; F7 below keeps the
GRADE honest in the meantime).

- **A background run whose provider RAISED was stranded forever** (F1). The run
  supervisor collected `future.result()` unchecked, so a provider error left the
  run stuck `running` with its admission slot held and its result unreachable
  via `run_inspect`. Any exception now becomes a terminal, coded failure —
  inspectable once, slot freed.
- **The result contract gained a fifth shape, `run_lifecycle`, and
  `contract_version` bumped `1.0.0` → `1.1.0`** (F2). `run_submit` /
  `run_inspect`-while-active / `run_cancel` responses were stamped a contract
  version but matched none of the four published shapes; they now validate, and
  the additive change bumps MINOR (the changelog previously claimed an addition
  left the version unchanged, which reversed semver).
- **Synchronous managed execution no longer discards a good result when cleanup
  fails** (F3). A `ProviderOperationFailure` from `cleanup()` replaced the
  collected stdout/verdict/receipt with an internal error; the result is now
  preserved and a `cleanup_error` field is appended, mirroring `run_inspect`.
- **A non-execute session touch after idle-expiry no longer revives the worker**
  (F4). `session_write_file` / `session_files` / `session_read_file` /
  `session_artifacts` refreshed the idle clock without first running the expiry
  gate, so a bare touch kept an expired worker alive; they now reap first, and
  the next `execute` gets the documented expiry error.
- **A completed-but-uninspected background run no longer holds admission
  capacity** (F5). Done futures are reaped before the admission count, so
  completion — not just inspection — frees a slot.
- **The execution receipt now records `session_id`** (F6, receipt
  `1.0.0` → `1.1.0`). The same spec runs in different sessions produced
  byte-identical receipts; the session is now named (workspace-state hashing is
  out of scope and stated as such).
- **`verify_optimization` grading no longer certifies a SLOWDOWN as
  `cross_checked`** (F7, `grade_rules_version` `1` → `2`). An accepted result
  whose measured speed ratio is not `> 1` is `ungraded` rather than graded as a
  speed-cross-checked optimisation.
- **The `max_output_kb` documentation no longer claims `0` means "uncapped"**
  (F8) — `0` selects the backends' 64 KiB default, and the doc now says so.
- **The first terminal `run_inspect` reports accurate cleanup state** (F9). It
  returned pre-cleanup status, so the first terminal read said `cleaned=false`
  and the next said `cleaned=true`; the status is now refreshed after cleanup.
- **The request-identity docs state identity is over the request AS SPELLED**
  (F10). Operationally-equivalent language aliases (`python` / `py` / `python3`)
  hash distinctly; the docs no longer imply two spellings are "the same
  computation".

---

## [0.1.0] — 2026-08-17

First public release. Nothing had been published to PyPI or crates.io before
it, so there was no upgrade path to describe — only what the thing is.

### Corrected before first publication

- **The Windows fork-bomb claim.** The README's platform table and `AUDIT.md`
  both described Windows' job-scoped `ActiveProcessLimit` as *better* than
  `RLIMIT_NPROC`'s uid-wide budget. Measurement says otherwise: 400 of 400
  spawns succeeded against a ceiling of 24 on Windows 11 Pro, because that limit
  comes from a process's immediate job and post-creation assignment does not
  guarantee ours is it. Both documents now state what is measured, and point at
  `process_limit_enforcement_unverified_on_windows`, which every post-creation
  Windows run declares. Corrected before `0.1.0` rather than after, because a
  rendered release page cannot be edited without cutting a new version.

### Added

- **Authenticated remote strict execution for macOS clients.** Setting
  `CODECALC_STRICT_URL` activates `<host>-strict`; the adapter verifies the
  Linux service's versioned `gvisor-v1` application-kernel, cgroup-v2,
  namespace, seccomp, filesystem, network, descendant, and resource-limit
  receipt before sending source. Missing or
  incomplete providers fail closed, managed run IDs are preserved through
  cancellation and cleanup, and authorization material is redacted.

- **Portable Linux gVisor launcher contract.** The strict service now has a
  shell-free Docker launcher for a digest-pinned executor image, explicit
  `runsc`, cgroup-v2 CPU/memory/PID limits, no network, read-only rootfs,
  bounded tmpfs, non-root UID, dropped capabilities, and
  `no-new-privileges`. It supports the x86_64 and ARM64 architectures supported
  by gVisor and fails closed when Docker, cgroup v2, or `runsc` is absent.

- **48 MCP tools across 31 languages.** Code execution, symbolic mathematics
  (SymPy), logic and SMT solving (Z3), exact decimal arithmetic, unit
  conversion, complexity analysis and benchmarking.
- **A Rust sandbox executor** (`codecalc-exec`) with rlimits, wall-clock and CPU
  ceilings, process-group kill, an output cap applied at the source, and an
  `LD_PRELOAD` shim that blocks network syscalls when `no_net` is requested.
- **A pure-Python fallback** for hosts without the built binary. It enforces
  strictly less and reports exactly what it could not apply in `unenforced`,
  rather than presenting a weaker sandbox as the same one.
- **A published result contract**, version `1.0.0`. Every result carries
  `contract_version`; the JSON Schema is at `docs/contract/result-v1.schema.json`
  (JSON Schema 2020-12, the dialect MCP `2026-07-28` defaults `outputSchema` to)
  and the policy is in `docs/contract/README.md`.
- **Eight stable error codes** with an actionable `remedy` on every failure —
  `validation`, `runtime_unavailable`, `timeout`, `resource_exhausted`,
  `permission_denied`, `dependency_missing`, `worker_failure`, `internal`.
  Branch on `code`; the prose in `error` is free to improve and is not a
  contract.
- **Byte counts behind truncation.** `stdout_bytes` / `stderr_bytes` report what
  the program produced before the response cap, so a caller can size a retry
  instead of guessing. Exact when `output_truncated` is false; a lower bound
  when it is true, because the two backends enforce the cap differently.
- **`codecalc doctor`** (also `python -m codecalc doctor`) — the install
  verification step. Reports the resolved backend and its binary, the install
  sandbox, extras, the status of every runtime, whether the workspace is
  writable, and the contract version, before a tool call has to. **Exits `0`
  when the install can execute and `1` when it cannot**, so it drops into a
  Dockerfile or a CI job unchanged. A missing extra or an uninstalled runtime
  does not fail it — those are facts about the host, not a broken install.
- **`codecalc doctor --json`** — the same report as machine-readable data,
  against a published schema (`docs/contract/doctor-v1.schema.json`) carrying
  the same `contract_version` and policy as a tool result. Each runtime reports
  `supported`, `installed`, `unhealthy` or `available`, and `status_basis` says
  whether those came from resolving the command or from running it. `--deep`
  executes them; without it nothing is ever reported `available`. `--deep` also
  reads each runtime's own `version`; `null` means **not measured** — no
  `--deep`, no version flag, or an unreadable answer — and never "no version".
- **`list_languages` reports what it measured.** Each entry carries `status`
  (`supported` or `installed`) and `status_basis` (`resolved`), the same
  vocabulary `doctor` uses. `available` is still there and unchanged, but it was
  computed from finding the command on `PATH` while being named for something
  stronger — on one host `bash` resolved, was reported available, and failed
  every time. Nothing is reported `available` without being run, which is what
  `doctor --deep` is for.
- **Verification gates instead of generation.** `verify_translation` and
  `verify_optimization` take a candidate *the caller wrote* and prove or
  disprove it by execution. codecalc runs and measures; it does not generate.
- **Warm sessions** with a persistent worker, workspace files and artifacts.
  Session results declare what a long-lived worker cannot enforce that a
  one-shot sandbox can.
- **Platform wheels carrying the executor**, plus standalone archives and a
  `SHA256SUMS` file for anyone not using pip.
- **`CODECALC_REQUIRE_NATIVE=1`** — refuse to start on the weaker fallback
  rather than silently downgrading.
- **Optional extras** so a minimal install stays small: `[symbolic]`,
  `[parsing]`. `codecalc doctor` names the exact command for anything missing.
- **A skill file** (`codecalc/SKILL.md`) shipped inside the wheel, describing
  when to call these tools and how to report their results.
- **`compare_execution` reports `discrepancies`.** Always present, empty when
  there is nothing to disclose. A language that produces no stdout while a
  sibling produces some is flagged rather than left as a blank row in a table —
  an exit-0 run with no output cannot be distinguished from one whose output was
  lost, and reading it as "the answer is empty" is how a lost result becomes a
  wrong one.

### Security

- **The Python fallback dropped `windir` on Windows.** `os.environ` upper-cases
  every key there, so the mixed-case allowlist entry never matched and the
  variable was filtered out of the child's environment — while the native
  executor passed it, because `std::env::var` is case-insensitive on Windows.
  `windir` is one of the two variables added because "node returned empty output
  with ok=false through the sandbox on Windows", so the fix that made node work
  there was only half-applied. Matching is now case-insensitive on Windows only;
  POSIX environment variables are genuinely case-sensitive and widening the
  allowlist there would weaken the boundary it exists to be.

- Apache-2.0 licensed. A pre-publication security audit ships in the repository
  as `AUDIT.md` — 2 critical, 3 high, 3 medium, every one confirmed with a real
  exploit attempt and fixed before the first release.
- **No `eval`, and `exec` in exactly one file**, gated by
  `scripts/check_no_eval.py` on every push.
- **Executed code sees an allowlisted environment only.** API keys and tokens in
  the host environment are never inherited.
- **Package installation is confined with Landlock** where the kernel provides
  it, and installer hooks are disabled by default.
- **Offline by construction.** The package contains no LLM client, no
  documentation fetch and no socket-capable import; `tests/test_offline.py`
  asserts this per module and cannot skip. Executed code still reaches the
  network unless `no_net` is both requested and enforceable — which the result
  says, per call.

### Fixed

- **Windows: the sandboxed child is now created suspended and assigned to the
  job before its first instruction.** It used to be spawned normally and
  assigned immediately after, leaving a window of microseconds in which it ran
  outside the job — anything it spawned there escaped every limit. AUDIT.md
  recorded this as a caveat that could only be closed by dropping
  `std::process::Command` for a raw `CreateProcessW`; that was wrong, and is why
  it stood. `CREATE_SUSPENDED` plus a PID-scoped thread resume closes the window
  to zero and keeps Command's pipes, environment, cwd and argument quoting. A
  child that cannot be placed in the job is killed rather than resumed.

  This does **not** fix the separate Windows failure where the process ceiling
  does not bind at all under an ambient job carrying `SILENT_BREAKAWAY_OK` —
  see the known limitation below, which is unchanged and still disclosed.

- **`analyze_complexity` downloads its grammars on first use, and the docs now
  say so.** tree-sitter grammars are not in the wheel: the pack ships a ~5 MB
  extension and fetches each grammar in-process into a local cache — 28
  grammars, 89 MB, ~15s cold. The README's network table said the package layer
  reaches the network `Never`, which was false, and the size table's `+5 MB` for
  `[parsing]` described the wheel rather than the cost. `codecalc doctor` now
  reports whether the cache is warm, and `scripts/prefetch_grammars.py` warms it
  for an offline install.

### Fixed

- **Windows process containment is creation-time and terminal by default.**
  `PROC_THREAD_ATTRIBUTE_JOB_LIST` makes CodeCalc's job the initial runtime's
  immediate job; `JOB_OBJECT_UILIMIT_EXITWINDOWS` prevents a launcher from
  hiding the payload behind a weaker nested job; and
  `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` restricts inherited handles to the three
  standard streams. Verified with a direct Python runtime on Windows 11 Pro:
  23 children against a total process limit of 24, then WinError 1816.
  `CODECALC_WIN_JOB_AT_CREATION=0` keeps the old post-creation route as an
  explicitly unverified compatibility escape hatch.

### Known limitations

- **On Windows the legacy process path is reported UNVERIFIED.**
  `process_limit_enforcement_unverified_on_windows` is emitted whenever the
  child is assigned to the job after creation. This route is now selected only
  with `CODECALC_WIN_JOB_AT_CREATION=0`. It is not a detected failure; it is an
  admission. Measured with the
  executor instrumented: three processes in one spawn chain reported three job
  contexts — the launcher `0x3000`, **the executor itself an EMPTY job `0x0`**,
  the sandboxed child `0x3000`, its grandchildren no job at all. The ambient
  check answers truthfully about a topology the child is not in, and no
  parent-side Win32 call returns another process's immediate job or effective
  `ActiveProcessLimit`. `ActiveProcessLimit` is also not combined across a
  nested chain — it comes from the *immediate* job — so the child's `APL 0`
  governs and codecalc's 24 is never consulted. The four detection strings below
  still fire: each can prove a failure, none can prove success, so their silence
  no longer implies enforcement.

- **On Windows the process ceiling may not bind, and now says so.** Measured on
  Windows 11 Pro from two unrelated launchers: the job-object `ActiveProcessLimit`
  is set, both API calls succeed, and 400 of 400 child spawns still go through.
  The run used to come back `ok: true` with nothing in `unenforced`. It now
  discloses one of `process_limit_not_enforced_child_escaped_the_job`,
  `process_limit_not_enforced_ambient_job_allows_breakaway`,
  `process_limit_membership_unverifiable_on_windows` or
  `process_limit_enforcement_unknown_on_windows`. The ceiling is **not** repaired
  — the root cause is open — but a caller can now tell an applied
  guarantee from an absent one, which is the difference that matters. GitHub's
  Server-SKU runner does bind it, which is why CI never showed this.

- **The distribution names are not claimed yet, and the README says so.**
  Measured live: `pypi.org/pypi/codecalc` returns 404 and
  `crates.io/api/v1/crates/codecalc-exec` returns 404. Both are free for anyone
  to register, which is why the README's install box leads with that rather
  than printing a `pip install` line that fetches whatever a stranger uploaded.
  Claiming them is a browser action on the maintainer's accounts and cannot be
  done from CI — [#91](https://github.com/The-40-Thieves/codecalc/issues/91).

  **npm `codecalc` is already taken** by an unrelated package ("a calculator
  created during NODE demo practicals", v1.0.0). This costs nothing: there is
  no `package.json` here and no JS client is planned. Recorded so the collision
  is not rediscovered as a surprise. If a JS client is ever published, the
  scoped `@the-40-thieves/codecalc` is available and is the name to use.

- **`bash` on Windows needs a Git-for-Windows-style build, and the path it is
  given is now separator-free.** Windows passes one command-line *string* and
  each runtime re-splits it; the MSYS2 runtime `bash` is built on treats `\` as
  an escape, so a workdir path arrived with every separator eaten. codecalc now
  hands those runtimes the bare file name, which resolves against the workdir
  that is already the child's cwd. The end-to-end case is **not gateable in CI** —
  the `windows-latest` image resolves `bash` through a Git-for-Windows install
  whose paths do not expose the stripping — so what CI gates is the argv
  rendering itself, on all three platforms.

- `no_net` needs the native executor's shim. The pure-Python fallback cannot
  apply it and says so in `unenforced`.
- `peak_memory_kb` is `null` on the fallback: `ru_maxrss` is a process-wide high
  water mark and cannot be attributed to one run. `null` means not measured,
  never zero.
- The `MLE` verdict is native-only. The fallback reports `RTE` for an
  out-of-memory kill rather than guessing at a ceiling it did not measure.
- Stateful sessions run in a plain subprocess, not under the Rust executor.
  Every session result lists the guarantees that therefore do not apply.
- A known intermittent fault on Windows: `node` can return empty stdout with
  `ok: false` through the sandbox. Tracked, with a dated reproduction, at
  [#42](https://github.com/The-40-Thieves/codecalc/issues/42).

[Unreleased]: https://github.com/The-40-Thieves/codecalc/compare/v0.13.0...HEAD
[0.13.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.13.0
[0.12.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.12.0
[0.11.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.11.0
[0.10.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.10.0
[0.9.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.9.0
[0.8.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.8.0
[0.7.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.7.0
[0.6.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.6.0
[0.5.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.5.0
[0.4.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.4.0
[0.3.1]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.3.1
[0.3.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.3.0
[0.2.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.2.0
[0.1.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.1.0
