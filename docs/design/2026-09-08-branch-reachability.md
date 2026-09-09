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
`_walk_block` returns `(falls_through, continuation_cond)`, `None` when
every arm returns, and the `or` of whichever arms did not otherwise. This
is what lets

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

## Loops are analyzed once, not unrolled

A `for`/`while` body is walked exactly ONE time, against a copy of the
environment. For a `for` loop the loop variable is bound to a fresh
symbolic `Int`, constrained to the loop's own REAL static range (from its
literal `range(...)` bounds) — sound for the loop's own branch and for
whatever is nested directly inside it, since the model has exactly the
values the real loop would produce available to it, not an arbitrary one.

What is NOT modeled is the loop's own effect on the code AFTER it: this
tool walks the loop body against a COPY of the environment and discards
it, so a later branch reachable only via the loop's accumulated effect
(a running total crossing a threshold after several iterations, for
instance) can be reported `dead` here even though it is not. This is a
known, documented imprecision — a real account would need loop-invariant
reasoning this tool does not attempt — not a hidden one: it never widens a
`verdict: reachable` into something false, only narrows a genuinely
reachable branch into a false `dead`, and every `reachable` verdict this
tool DOES produce still ships a `witness`, which the test suite corroborates
by actually running the program (`tracing.execute_trace`) rather than
trusting the model.

## Why z3 `Optimize` for boundary inputs, and the box around it

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
applies to `Solver` for a plain sat/unsat question. Python integers (and
z3 `Int`s) are unbounded, so a minimize/maximize call over an unconstrained
objective can run forever looking for a smaller (or larger) value that does
not exist; every `Optimize` call adds a `±1,000,000` box around the
objective so it always terminates, at the cost of not reporting a real
boundary that happens to sit outside that box. Reported as absent
(`null`), never as a wrong value.

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

## Refusal is a first-class result, never an exception

Every refusal this tool can produce — an unsupported construct, a
non-python `language`, more than one top-level function, no function and
no `inputs` — is `errors.VALIDATION`, stamped via `contract.stamp`, naming
the construct and its line where one applies, with a `remedy` pointing at
`trace_execution`. Nothing here raises past the tool boundary; a client
never has to distinguish "the server crashed" from "your program uses a
construct this tool does not model".
