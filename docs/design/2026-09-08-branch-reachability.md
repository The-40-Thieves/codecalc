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

## Loops are analyzed once, not unrolled — but their effect IS merged

A `for`/`while` body is walked exactly ONE time, against a copy of the
environment. For a `for` loop the loop variable is bound to a fresh
symbolic `Int`, constrained to the loop's own REAL static range (from its
literal `range(...)` bounds) — sound for the loop's own branch and for
whatever is nested directly inside it, since the model has exactly the
values the real loop would produce available to it, not an arbitrary one.

The loop's own effect on the code AFTER it now goes through the identical
`_merge_envs` machinery an if/else does: the post-loop value of every
variable the body touched is `If(entered, value-after-one-iteration,
value-before-the-loop)`, where `entered` is the loop's own guard (the
`while` test, or whether the `for`'s static range is non-empty) evaluated
against the PRE-loop environment. This is a real, if imprecise, account
of the loop's effect — no longer the strictly-worse "the loop's own
bindings never leave it at all" the first cut implemented.

What is STILL not modeled is more than one iteration's worth of change: a
variable that only stabilizes after two-or-more iterations (an
accumulator like `total = total + 1` run three times) is modeled as if
the loop ran AT MOST ONCE, so a later branch reachable only via the
loop's fully-accumulated effect (`total == 3` after three iterations, for
instance) can still be reported `dead` here even though it is not. This
is a known, documented imprecision — a real account would need loop-
invariant reasoning this tool does not attempt — not a hidden one: it
never widens a `verdict: reachable` into something false, only narrows a
genuinely reachable branch into a false `dead`, and every `reachable`
verdict this tool DOES produce still ships a `witness`, which the test
suite corroborates by actually running the program (`tracing.execute_
trace`) rather than trusting the model — EXCEPT for the one class of
witness this imprecision itself produces (a post-loop value that reflects
one iteration, not the real trip count), which the test suite labels
explicitly rather than asserting a trace match that would fail for an
unrelated, expected reason.

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
