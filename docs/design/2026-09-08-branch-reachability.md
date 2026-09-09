# branch_reachability — static, z3-decided branch reachability

**Status:** shipped, 2026-09-08. Implementation: `codecalc/branch_reachability.py`,
registered as `branch_reachability` (`execution` group) in `codecalc/server.py`.

## What this answers, and what it does not

`trace_execution` runs a program once and reports which branches THAT
input took. `branch_reachability` never runs the program at all — it
translates the function's control flow to an SMT formula and asks z3, for
every `if`/`elif`/`else` arm and every `while`/`for(range, static bounds)`
loop, whether ANY input reaches it. The two are complementary, not
competing: this tool proves "dead" or produces a `witness`;
`trace_execution` is the tool for "run this exact input and show me what
happened", including anything outside this tool's subset.

## The supported subset, and why it stops there

The translator supports `int`/`bool`/`str` values; `+ - * // %` on ints;
`and`/`or`/`not` on bools; `== != < <= > >=`; `==`/`!=` plus `len()` on
strings (z3's `String` sort, `z3.Length`); and `abs`/`min`/`max` built from
`z3.If`. Assignment is SSA-lite — `env[name]` is rebuilt as a compound z3
expression over the ORIGINAL parameter symbols on every reassignment,
never a fresh symbol, so a model for any path condition already speaks in
terms of the function's own parameters.

Everything else is refused, and refused BEFORE a single z3 call —
`_first_unsupported` walks the WHOLE parsed source once and returns the
first disallowed construct in source order: floats/`None`/bytes/complex
literals, attribute access, f-strings, comprehensions, classes, `async`,
`try`/`except`, imports, subscripts, chained or `is`/`in` comparisons, any
call outside `abs`/`min`/`max`/`len`, a data-dependent loop bound, and
`//`/`%` by anything but a positive integer literal. That scan is
deliberately BROADER than what the translator technically cannot handle —
one refusal shape for the whole unsupported surface, always naming the
construct and the line it lives on, with `trace_execution` as the remedy
in every case (run the concrete input instead).

### `//` and `%` need a positive literal divisor — measured, not assumed

z3's integer division/modulo is Euclidean (the remainder is always
non-negative); Python's `//`/`%` are floor-division (the remainder takes
the SIGN of the divisor). Measured directly against the installed z3
(5.0.0):

| a | b | z3 `a/b`, `a%b` | Python `a//b`, `a%b` |
|---|---|---|---|
| -7 | 2 | -4, 1 | -4, 1 |
| 7 | -2 | -3, 1 | -4, -1 |
| -7 | -2 | 4, 1 | 3, -1 |

The two agree exactly when the divisor is positive, and disagree
otherwise. Rather than special-case the translation for a negative or
symbolic divisor, the divisor is required to be a positive integer
LITERAL — the common case (`n % 2`, `x // 3`) — and anything else is
refused up front with the reason spelled out, so a caller is told
"z3 and Python disagree on the sign convention here" rather than getting
a silently wrong verdict.

## Path conditions, and why `elif` needed no special case

Each arm's z3 condition is the conjunction of every ancestor guard on the
path to it, negated for an else/elif arm exactly the way Python's own
`not` negates a condition. `elif` is nothing more than a nested `ast.If`
inside `orelse` — exactly what CPython's own parser already produces — so
`_walk_if` handles the whole chain by recursing into that nested node with
the accumulated "not (...)" pieces threaded through, rather than needing a
separate elif-chain walker.

An unconditional `return` on every path through an arm cuts that arm's
condition out of what reaches the code AFTER the enclosing statement:
`_walk_block` returns `(falls_through, continuation_cond, env)`, `None`
when every arm returns, and the `or` of whichever arms did not otherwise.
This is what lets

```python
def f(x):
    if x > 0:
        return 1
    y = x + 1
    if y > 100:
        return 2
    return 3
```

correctly report the second `if` as **dead**: the only way past the first
`if` is `x <= 0`, so `y = x + 1` is bound to `x + 1` under `x <= 0`, and
`y > 100` is unsatisfiable given that — proven, not guessed from the
source's shape.

### Merging environments at a join point — CORRECTNESS, not just an
optimization

The `env` half of that same return value matters just as much as the
condition half, and an earlier cut of this tool got it wrong. An
assignment inside an if/elif/else arm is only visible to the code after
the join **conditionally on which arm ran** — mutating one shared dict per
arm and discarding the copies, the first version's approach, silently
reverted every such assignment back to its pre-if value for anything
downstream, in both directions: a later branch that only became reachable
because of an arm's assignment was wrongly reported `dead`, AND a later
branch that only became UNREACHABLE because of an arm's assignment was
wrongly reported `reachable`, with an invalid `witness`. A cross-vendor
review caught both with these two programs:

```python
def f(x):                      def f(x):
    y = 0                          y = 0
    if x > 0:                      if x > 0:
        y = 5                          y = 1
    if y == 5:                     if y == 0:
        return 1                       if x > 100:
    return 0                               return 1
                                    return 0
```

The first cut reported the left program's `if y == 5` **dead** (wrong —
`f(1)` returns 1) and the right program's nested `if x > 100` **reachable**
with witness `{x: 101}` (wrong — `f(101)` returns 0, and `y == 0` requires
`x <= 0`, which contradicts `x > 100`).

The fix (`_merge_envs`) builds a real phi node: for each variable either
arm assigned, the post-join value is `z3.If(guard, value_if_arm_ran,
value_otherwise)`, where `guard` is the arm's own LOCAL condition (not the
accumulated ancestor path — that is already baked into whatever later
formula the merged value participates in). An arm that ends in an
unconditional `return` contributes nothing to the merge — exactly the set
`continuation_cond` is already built from. A variable assigned in only
SOME of the surviving arms is dropped from the merged environment rather
than guessed at: a later reference to it hits the ordinary "undefined
name" `_TranslateError` `_translate` already raises for any unbound name,
which downgrades that ONE branch to `verdict: "unknown"` (see `_record_
unknown`) instead of aborting the whole call — the exactly correct answer,
since Python itself would raise `UnboundLocalError` on the path that never
assigned it. Two envs agreeing on a variable BY IDENTITY (neither arm
reassigned it) skip the `If` entirely, which is what keeps a program with
few reassignments from growing an `If` for every untouched parameter.

## Loops: exact where cheap, `unknown` where not — never a false verdict

**This section was rewritten after a confirmation review found the
previous design (merging a single representative iteration for EVERY
loop) was unsound, not merely imprecise.** The failure mode: a `for`
loop's variable was bound to a fresh symbolic `Int` constrained to the
loop's own real range, the body was walked once, and the result was
MERGED with the pre-loop state using `entered` (true for any non-empty
static range) as the condition — which collapses to "trust the one-
iteration walk unconditionally" for every `for` loop, every time. A
variable that is only ever *the last iteration's value* in reality then
looked, to everything after the loop, like *any value in the range* — a
real, adversarially-confirmed false `reachable`, checked by direct
execution:

```python
def f():
    x = -1
    for i in range(0, 10):
        x = i
    if x == 5:
        return 1
    return 0
```

reported `if x == 5` **reachable** with witness `{}` — `f()` is fully
deterministic, `x` is always `9` at that line, and the branch never runs.
That is exactly the error class this tool's own docstring and this
document promised never to produce, so "only narrows a reachable branch
into a false dead" (this section's own earlier claim) was wrong: the old
design could widen too.

The fix uses TWO genuinely different mechanisms rather than patching the
one:

**`for x in range(<static>)` within `_MAX_UNROLL_ITERATIONS` (32) is
unrolled EXACTLY.** The body is walked once per concrete value of `x`,
threading the environment sequentially — real symbolic execution, not an
approximation. A branch inside the body is `reachable` if ANY unrolled
iteration's own path condition is sat, `dead` only if EVERY iteration's
is unsat; `_Ctx.commit_branch`/`flush_unroll_frame` aggregate this per
source line, since the same AST node is visited once per iteration.
Post-loop state is therefore EXACT — including an accumulator like
`total = total + 1` run three times, which this design's own earlier
draft used as an example of an accepted imprecision and which is now
simply correct within the cap (see `tests/test_branch_reachability.py`'s
`for-accumulate` case). A pleasant side effect of real sequential
threading rather than merging: the loop's target variable ends up bound,
after the loop, to its LAST iteration's value — matching Python's own
scoping — for free.

**A `while` loop, or a `for` above the cap, keeps the one-representative-
iteration walk, made SOUND by dropping the merge for it.** A branch
INSIDE the body is `reachable` when this one iteration's path condition
is sat (a real witness is a real witness regardless of which iteration
produced it), and `unknown` — never `dead` — when it is unsat: proof on
one iteration is not proof for all of them (`_REASON_FIRST_ITERATION_
ONLY`). (What happens to the loop's OWN closure and to code AFTER it is
a separate question a fourth review found this walk originally got
wrong — see "a fourth review pass" below.) The real fix, though, is what
happens to the VALUES the code AFTER the loop can see:
every variable the body assigns anywhere — directly, or through a nested
arm's own merge — is TAINTED, rebound to a brand-new, entirely
unconstrained z3 symbol (`_Ctx.fresh_tainted_symbol`) that carries no
information at all, in place of the range-bound-but-still-informative
symbol that caused the false positive above. A later guard whose
translated z3 expression mentions a tainted symbol ANYWHERE — including
buried inside arithmetic, since `_apply_assign` never launders it away —
is caught by `_mentions_tainted` (a plain iterative walk of the z3 AST,
visited-tracking keyed on `node.get_id()` — z3's own hash-consing id,
NOT Python's `id()`, since z3 terms are a DAG — see this section's own
"a third review pass" subsection below for why that distinction turned
out to matter) and reported
`unknown` (`_REASON_TAINTED_BY_LOOP`) WITHOUT ever being solved: the
fresh symbol is unconstrained, so z3 would call it `sat` for nearly
anything, which is exactly the false-confidence failure mode being
closed. A guard's OWN reassignment to something that does not reference
the tainted symbol (`total = 10` after a tainted `total`, say) correctly
clears the taint with no extra bookkeeping, because the check is
STRUCTURAL — it walks whatever z3 expression the variable currently
holds, not a per-name flag that would need to be reset by hand.

Both mechanisms honor the one invariant this tool promises: a
`reachable` verdict always carries a witness that is a real witness
(every one in the test suite is corroborated by `tracing.execute_trace`
against the real program). Whether the conservative path can ALSO
produce `dead` — for the loop's OWN closure, not for a branch INSIDE the
body — turned out to be more subtle than the original design assumed;
see "a fourth review pass" below.

### A third review pass: unrolling threaded copies independently instead of sequentially

Confirmation of everything above by direct execution ALSO found that
`_walk_for_unrolled` — the exact-unroll mechanism two paragraphs up —
was itself unsound in a narrower way. It hand-threaded the environment
across the N concrete-value copies of the body, but fed every copy the
SAME, unnarrowed path condition, discarding each copy's own
`falls_through`/`continuation_cond`:

```python
def f(x):
    y = 0
    for i in range(5):
        if i == 2:
            return 100
        if i == 4:
            y = 99
    if y == 99:
        return 1
    return 0
```

reported the post-loop `if y == 99` **reachable**, when every real call
returns `100` at `i == 2` and the loop body never even reaches `i == 4` —
confirmed by direct execution for `x` in `{0, 1, -5, 999}`. The fix feeds
the N copies through the EXACT SAME sequential-statement machinery
`_walk_block` already uses for two consecutive `if` statements: copy
`k+1` is walked starting from copy `k`'s own `continuation_cond`, not the
loop's original entry condition, so a copy whose own walk reports
`falls_through=False` — a bare `return`, the same structural signal
`_walk_block`'s `ast.Return` case reports — closes every LATER copy (each
still walked, so its own internal branches are discovered and correctly
reported `dead` rather than silently dropped) and the loop's own
`(falls_through, continuation_cond)` reported to its caller becomes
`(False, None, ...)`, closing the enclosing block exactly as a bare
`return` would. `tests/test_branch_reachability.py`'s `unroll-return-*`
cases cover a return at the first iteration, at the last, nested two
`if`s deep, and a return gated on an INPUT-dependent condition (whose
post-loop reachability then correctly depends on that same input) — every
witness trace-corroborated as above.

### `tests/test_branch_reachability_differential.py`, and the two bugs it found that three hand-written reviews had not

Three review rounds each found a DIFFERENT instance of the same underlying
failure mode — a hand-written test suite only tries the combinations
someone thought to write down. `tests/test_branch_reachability_
differential.py` exists to try hundreds of small, randomly generated ones
instead: a deterministic (fixed-seed) generator over the supported
subset, checked against ground truth from exhaustive concrete execution
(every witness is run for real and must hit the arm's own body line;
every `dead` line must never fire for any input in a bounded domain).
Running it surfaced two MORE instances, in mechanisms none of the three
reviews above had touched:

1. **`_mentions_tainted`'s visited-set was keyed on Python's `id()`.**
   z3's Python bindings mint a fresh wrapper object on every
   `.children()` call rather than interning one per underlying
   (hash-consed) node, so a wrapper can be garbage-collected and its
   `id()` reused by an unrelated LATER node within the SAME walk. When
   that happened, a tainted symbol nested inside a `z3.If`'s second
   branch could be skipped as an already-"seen" duplicate of a
   completely different node — a guard that genuinely depended on a
   tainted value then solved as an ordinary `reachable`, with a witness
   that pinned the "unconstrained" tainted symbol to whatever value made
   the query satisfiable. This reproduced non-deterministically (it
   depends on GC timing, not on the input), which is exactly why the
   fixed-seed differential corpus — run repeatedly — caught it where a
   one-shot hand-written case would not have. Fixed by keying the
   visited set on `node.get_id()` instead — z3's OWN id for the
   underlying node, identical across every wrapper around it, with no
   such aliasing risk.
2. **A tainted (or untranslatable) guard's "opaque pass-through" was sound
   for the environment but not for control flow.** Neither arm of such a
   guard is walked — by design, since guessing which one executes is
   exactly the false confidence this mechanism exists to avoid — so the
   tool genuinely does not know whether one of them held an unconditional
   `return`. The existing code returned `falls_through=True` regardless,
   which is correct for "nothing was applied to the environment" but
   wrong for "so execution definitely continues past here": a `return`
   inside an unwalked arm closes off everything after it in reality, and
   trusting a `reachable` witness for code several statements later
   amounts to trusting that a `return` this tool declined to resolve did
   not fire. Fixed with a new ambient counter, `ctx._unresolved_closure_
   depth` (parallel to the existing `ctx._conservative_loop_depth`),
   raised for every statement sequentially after such a pass-through
   within the same block. `_decide` reports `unknown` — never `reachable`
   — for a `sat` result found under it: the full path condition used
   there is missing a conjunct this tool could not model ("the earlier,
   unresolved `return` did not fire"), so its solution set is a SUPERSET
   of the true reachable set, and a witness in a superset need not be in
   the true set. An `unsat` result still safely proves `dead`, because
   dropping a required conjunct only WIDENS what solves — unsatisfiability
   of the wider condition survives narrowing to the true one. The signal
   itself (`ctx._pending_closure_taint`, one-shot: set by whichever
   construct hit the pass-through, read-and-cleared by
   `_take_closure_taint`) threads through `_walk_if`, `_walk_block`,
   `_walk_for_unrolled`, and `_walk_loop_conservative` exactly the way
   `falls_through`/`continuation_cond` already do — composing correctly
   through elif chains, nested ifs, and chained unrolled-loop copies
   without leaking into an unrelated SIBLING arm (an `if`'s `else`, or a
   different branch of an enclosing construct), the same "only surviving
   arms contribute" rule `_walk_if`'s own continuation-condition merge
   already follows.

Both are covered by minimized, deterministic regressions in
`tests/test_branch_reachability.py` (`taint-id-reuse`,
`closure-taint-return`) in addition to the differential corpus that found
them — the fixed-seed corpus is not a substitute for pinning down the
specific failure once found, since a corpus regenerated with a different
seed, or a future code change that shifts which programs it happens to
generate, would not by itself guarantee re-trying the exact case that
failed.

### A fourth review pass: the shipped seed was not a soundness argument

The paragraph immediately above turned out to be exactly right, sooner
than expected: a FOURTH review ran this file's OWN generator with the
`SEED` changed (`111`, `20260101`, and the shipped seed at
`CORPUS_SIZE=500`) and got real failures every time, while the SHIPPED
seed/size — the one this repo actually runs on every push — happened not
to generate the triggering shape at all. The bug itself was in
`_walk_loop_conservative` (every `while`, and every `for` above the
unroll cap): it hardcoded `falls_through = True` regardless of what the
one-iteration body walk itself reported, on the theory (stated in its own
docstring, quoted two sections up) that this mechanism "does not attempt
to reason about whether the body's own return closes off the loop, only
about VALUE taint". That is fine for a body that CANNOT return at all,
but wrong whenever it can:

```python
def f(x):
    if x == 1:
        n = 0
        while n < 4:
            return 6
    if x == 1:
        return 999
    return 0
```

reported the SECOND `if x == 1` **reachable** with witness `{x: 1}`, when
`f(1)` returns `6` at the `while` and never gets there — the same shape
with `for i in range(40): return 9` reproduced identically for the
above-cap `for` path, confirming this was the SAME underlying bug in the
SAME function, not two.

The fix uses the one-iteration body walk's own `(falls_through,
continuation_cond)` — already computed, previously discarded — instead
of hardcoding `True`:

- **The body never falls through on this one iteration** (`_walk_block`
  itself already proved every path through it, for ANY input that
  reaches it, returns): EXACT, not a widening. Entering the loop at all
  means returning, so falling through the WHOLE loop requires never
  entering it — `z3.And(cur_cond, z3.Not(entry_guard))` — and branches
  INSIDE the body keep whatever verdict the one-iteration walk already
  gave them (the UNSAT-downgrade rule two sections up is untouched).
- **The body falls through this one iteration but contains a `return`
  SOMEWHERE** (`_contains_return`, a plain structural `ast.walk` over the
  body — nested included, checked independently of whether that
  `return` is provably reachable): some OTHER, unmodeled iteration might
  take it, which this one-iteration walk cannot rule out. Handled with
  the SAME `ctx._unresolved_closure_depth` mechanism the third review's
  fix introduced for tainted-guard pass-throughs — `_decide` reports
  `unknown` for a `sat` result downstream, never `reachable`, while an
  `unsat` result still safely proves `dead` (dropping a required
  conjunct only WIDENS what solves). The one-iteration walk's own
  `continuation_cond` — a real necessary condition for falling through
  the FIRST iteration — is conjoined in rather than discarded for bare
  `cur_cond`, for free extra precision.
- **The body contains no `return` at all**: unchanged from before —
  falls through, only VALUE taint applies, nothing to close off with.

`while`/`for`-`else` needed no separate handling: `scripts/
check_no_eval.py`'s upfront scan already refuses `while/else` and
`for/else` by name, before the walker ever sees one.

Covered by the two repros above (now standing regressions in `tests/
test_branch_reachability.py`), a `while` whose body returns only under an
input-dependent condition followed by a post-loop branch that must
correctly come back `unknown` (never a fabricated `reachable`) alongside
a SEPARATE post-loop branch that is truly dead (contradicts the
one-iteration walk's own necessary condition) and must still correctly
come back `dead`, the identical pair for a `for` above the cap, a `while`
NESTED inside an unrolled `for` whose body returns unconditionally
(every outer copy closes, `dead` after the whole thing), and the exact
`SEED=111` differential-corpus program the review's sweep found —
recovered, since the review did not hand over its own generated source,
by rerunning that seed's generator against the pre-fix code (checked out
at the commit the review was reviewing) and taking the first program that
failed, verbatim.

This also hardened the differential corpus itself: `SEED`/`CORPUS_SIZE`
are now overridable via `CODECALC_DIFF_SEED`/`CODECALC_DIFF_CORPUS` env
vars (shipped defaults unchanged, the effective seed printed at the start
of every run) so a multi-seed sweep before a push is a command-line
matter, not a file edit; and the generator itself now produces `for`
loops ABOVE the unroll cap (previously never generated at all — the
exact gap this BLOCKER lived in undetected), a `return` biased directly
into loop bodies (bare and `if`-guarded, since that specific combination
is what this bug needed), and nested loops (one `for`/`while` inside
another's body, budgeted via `_Fresh.loops_remaining` so the compounding
two earlier sections warn about stays bounded). None of this makes the
shipped seed a soundness argument on its own — see the differential
test's own module docstring — only running several, including ones
nobody has run before, does that.

## Why z3 `Optimize` for boundary inputs, and no box around it

For every `Compare` in an arm's OWN guard (not its ancestors — see below)
with a numeric side, `boundary_inputs` reports the MINIMUM and MAXIMUM
value of that side reachable under the arm's full path condition, via a
z3 `Optimize` call per direction, plus an "equality edge" — a witness
where the OTHER side (when it is a literal) is hit exactly, checked
against the guards ABOVE this one only, so the edge appears even when it
sits just outside the arm being reported on.

`Optimize` rather than binary-searching `Solver` calls: it is the API z3
ships for exactly this question ("what is the extreme value of this
expression subject to these constraints"), and it is what a boundary-value
test generator wants directly — the same reasoning `z3_check` already
applies to `Solver` for a plain sat/unsat question.

An earlier cut boxed every objective to `±1,000,000` "so an unbounded
objective still terminates" — and then reported the box's OWN edge
(`max_input: {x: 1000000}` for a guard as simple as `x > 5`) as if it were
a real maximum, indistinguishable from a genuine bound. A review flagged
this directly: `x > 5` has no maximum, full stop, and a client reading
`1000000` back has no way to tell "this is the true extreme" from "this
is where the search gave up." The fix drops the box and reads the
`Optimize` handle's own verdict instead — measured against the installed
z3: `handle.lower()`/`handle.upper()` come back as a concrete `IntNumRef`
when the direction IS bounded, and as a non-concrete `oo`/`-1*oo` term
(`z3.is_int_value` false on it) when it is not. `_optimum_input` checks
exactly that, so `null` means "unsatisfiable, a solver timeout, OR
confirmed unbounded" and a `min_note`/`max_note` names the unbounded case
specifically — a caller never mistakes an internal ceiling for a proof.
No box is needed for termination either: z3 detects unboundedness on
these (linear-ish) objectives quickly on its own, and the existing
per-call `timeout` remains the real backstop for anything pathological.

Boundary computation is scoped to the arm's OWN guard, not the full
accumulated path — a compound guard like `if x > 5 and x < 3:` gets one
`boundary_inputs` entry per `Compare` inside it (both report no
satisfying `min_input`/`max_input`, since the arm itself is dead, but each
still reports its own `equality_edge_input` — `x = 5` and `x = 3`
respectively — checked against the guards ABOVE this whole `if`, which is
empty here, so both edges exist even though the conjunction of the two
does not). Extending this to also walk ancestor guards was considered and
cut for scope: it would need rebuilding the path formula with one conjunct
at a time swapped for an equality, which is meaningfully more machinery for
a feature that is already useful scoped to the arm's own guard.

Refusal ordering matters too: the whole-source unsupported-construct scan
now runs BEFORE the function-count/`inputs` structural checks, not after.
A module-scope `class`, for instance, has zero top-level `FunctionDef`s —
running the structural check first refused it with the generic "no
top-level function and no `inputs`" message, which is true but not the
most useful thing to say when the actual reason is a specific, nameable
construct on a specific line. The scan is a single cheap AST walk with no
z3 involved, so running it first costs nothing and gives every
unsupported-construct case, including this one, the same specific
refusal.

## Refusal is a first-class result, never an exception

Every refusal this tool can produce — an unsupported construct, a
non-python `language`, more than one top-level function, no function and
no `inputs` — is `errors.VALIDATION`, stamped via `contract.stamp`, naming
the construct and its line where one applies, with a `remedy` pointing at
`trace_execution`. Nothing here raises past the tool boundary; a client
never has to distinguish "the server crashed" from "your program uses a
construct this tool does not model".

## Tool-selection vocabulary: STATIC ANSWER vs. ONE INPUT

Adding `branch_reachability` to the tool-select corpus regressed an
EXISTING labeled prompt on `main` — "Which branch of this if/elif
actually fired when I ran this input, and which one never did" (expects
`trace_execution`) — because `branch_reachability`'s own description used
`branch`/`input`/`when` heavily enough (partly baked in by the tool's own
NAME: `_doc_text` scores `"{name} {description}"`, so `branch_reachability`
contributes "branch" and "reachability" tokens on every match regardless
of the prose) to out-score `trace_execution` on that query under the
unstemmed BM25 scorer `scripts/tool_select_eval.py` uses. Copying
`trace_execution`'s own phrasing ("actually fired", "when I ran") INTO
`branch_reachability`'s disambiguation sentence, the first fix attempted,
made it WORSE — BM25 counts raw term frequency per document with no sense
of which tool "deserves" a match, so repeating the query's own words in
the WRONG tool's description only helps the wrong tool. The fix that
worked went the other way: trim `branch_reachability`'s own repetition of
`branch`/`when`/`input` (including a redundant second self-mention of its
own full name inside its text, which doubles the free "branch" +
"reachability" tokens the name prefix already contributes), and add
`trace_execution`'s missing word — literally `input`, absent from its
description entirely before this — in a natural, accurate sentence at the
front of ITS OWN docstring instead. Re-measured with `scripts/
tool_select_eval.py` before/after on the unmodified `main` prompt set (see
the changelog entry's own numbers); the target prompt routes to
`trace_execution` again, and the checked-in baseline was regenerated from
the final corpus and descriptions together, per that script's own
documented policy ("regenerate ... after an intentional description OR
corpus change, never to paper over a regression").

**Round two** (a confirmation review) found that the emphatic first
sentence this round-one fix landed on — "Decide STATICALLY, WITHOUT
running the program ... proved by z3 for every possible input" — while it
fixed the `trace_execution` misroute, now pulled TWO of `main`'s OTHER
labeled prompts onto `branch_reachability`: one whose vocabulary
("formula", "cleanest possible form") belongs to `symbolic`/
`evaluate_expression`, and one ("rewrite", "preserve ... behavior") that
belongs to `verify_translation`. `proved`/`prove` and `formula` turned out
to be exactly the words those two tools' own descriptions are built
around, so ANY generic "z3 proves things" framing in `branch_reachability`
inevitably leaks into their territory. The rewritten lead sentence
("Which if/elif/else arms and while/for loops of this python3 function
can ever run, which are dead code, and what inputs reach each") avoids
both words entirely while keeping the `if`/`elif`/`while`/`for` vocabulary
6 of the corpus's own `branch_reachability`-labeled prompts depend on —
dropping that vocabulary (an earlier, even-shorter draft of this same
lead sentence) fixed the `main`-prompt regressions but broke 4 of those 6
in the process, confirming the two goals pull in different directions and
both need their own words present. Two more direct steals (a
`compare_edge_cases` prompt, an `update_runtimes`/`session_read_file`
pair caused by a courtesy cross-reference this round ALSO added to
`trace_execution`'s own docstring — dropped again, since a "see the other
tool" sentence is worth less than the false steals its extra tokens
caused) were closed the same way round one's was: trim the specific
overlapping word (`check` appeared twice — once from the tool name
`z3_check`, unavoidable, once from "to check directly", replaced with "to
solve directly"; `before`/`after` similarly reworded to `prior to`/`past`)
rather than reach for a wholesale rewrite. One regression proved
structurally unrelated to any word choice: "What does the computer
actually store in memory for the number 0.1?" flips from `float_repr` to
`evaluate_expression` (2.93 vs 2.88 on `main`; 2.92 vs 2.77 here) purely
because BM25's length normalization is corpus-relative — adding ANY 57th
document shifts the average document length every score is normalized
against, regardless of whether that document shares a single word with
the query. Confirmed by testing: no wording change to `branch_reachability`
moved this pair at all. This is the same coupling this codebase's own
memory index already names (`reference_lexical_eval_corpus_coupling`) —
not a defect in this change, a property of an unstemmed, corpus-relative
lexical scorer that any 57th tool addition would trigger for SOME
near-tied pair somewhere in a 228-prompt corpus.
