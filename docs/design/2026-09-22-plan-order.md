# plan_order: exact dependency ordering and minimum parallel waves

**Status:** proposed, 2026-09-22 (draft PR; open to redesign)

## What this answers, and what it does not

`plan_order` answers one bounded question: given a set of step ids and the
declared dependency edges between them, what deterministic linear order is
valid, and what is the minimum number of parallel waves? It also names an exact
dependency cycle rather than returning only "cyclic", and computes CPM critical
path and slack when every step has a cost.

It proves the order is correct GIVEN the dependencies. It cannot tell whether
the dependency list itself is right, whether a missing edge should exist, or
whether two real-world operations are safe to run together. `exclusive_with`
is caller-supplied policy, not something this tool infers.

`branch_reachability` answers a different question about Python control flow.
`z3_check` remains the right tool when a caller already has SMT-LIB2 or needs a
constraint outside dependency precedence, pairwise same-wave exclusion, and a
per-wave capacity.

## Why a new tool rather than a mode on z3_check

The capability already exists in principle: declare one integer wave variable
per step, add precedence/cardinality/exclusion constraints, and minimize the
maximum wave. In practice that means hand-writing SMT-LIB2, recovering an
ordered model, proving Optimize closed its bound rather than returning a
best-so-far model, and separately finding a concrete cycle. That is the exact
translation work an MCP tool should make unnecessary. A model that is planning
mid-task does not stop to author and audit that encoding.

This does spend tool-surface tokens, and GH #118's concern is real. The
project's tool-facade design records why a generic facade is worse than client
tool search, not why new definitions are free. Measured from a live
`tools/list` entry with `model_dump(by_alias=True, mode="json",
exclude_none=True)`, ordinary `json.dumps`, and `o200k_base`, this definition
adds **1,827 bytes / 446 tokens**. The full 52-tool response under the identical
method is **83,589 bytes / 22,129 tokens**. Those figures include the docstring,
input schema, output schema, annotations, and metadata carried by the MCP Tool
object. The proposal is therefore a trade: 446 startup tokens for a structured
operation that replaces a nontrivial SMT translation that models otherwise do not
perform. The draft status leaves that trade open to the maintainer.

## Two mechanisms, selected before solving

With neither `exclusive_with` nor `max_parallel`, the graph is enough. An
input-stable Kahn pass emits every currently-ready step as one wave. The wave
index of a node is its longest dependency-path level, so the number of waves is
minimal by construction. No z3 module is loaded on this path. Flattening the
waves gives the linear order, with every tie in original input order.

When either extra constraint is present, z3 Optimize receives integer wave
variables, strict precedence constraints, symmetric pairwise exclusions, and
one cardinality constraint per possible wave. It minimizes a `wave_count`
variable. A dependency-height lower bound, `ceil(step_count / max_parallel)`
when applicable, and exact symmetry breaks for genuinely interchangeable steps
cut renamed copies of the same model without removing any schedule.

Optimize's `sat` alone is not accepted as proof. The primary objective's lower
and upper bounds must be equal concrete integers. A second Solver pass fixes
that proven minimum and chooses the earliest satisfiable wave for each step in
input order, committing each choice before the next. This produces the unique
lexicographic tie-break without asking Optimize to prove one objective per
step. If either phase exhausts the shared caller timeout, the result is
`unknown` with no schedule. A best-so-far model is never labelled optimal.

A deterministic iterative depth-first walk runs before either scheduling path
and returns the first input-order cycle as `[A, B, C, A]`. Iterative rather than
recursive is load-bearing at the 1,000-step pure-path cap: the first
implementation recursed through a 1,000-step chain and hit CPython's recursion
limit during the measurement run.

## Critical path and slack

When every step carries a finite non-negative `cost`, a CPM forward pass
computes earliest starts/finishes and an input-stable predecessor on the
duration-setting chain. A backward pass from total duration computes latest
starts/finishes; `slack[id]` is latest start minus earliest start.

Costs do not change wave placement. Waves count unit scheduling rounds;
critical path reports dependency duration. Resource capacity and exclusivity
can add waves without changing the dependency-only CPM chain, and the result
keeps those two claims separate.

If any cost is missing, both `critical_path` and `slack` are explicitly null.
They are not omitted: null says the computation does not apply to this input,
while absence would be indistinguishable from a result produced before those
fields existed.

## Refusals and caps

Duplicate ids, an unknown dependency/exclusivity id, self-dependency, an empty
step list, an invalid capacity, and a negative/non-finite cost are coded
`validation`. The message and structured extras name the offending id/field
where one exists. A step-count cap is `resource_exhausted`, not `internal`:
the request is valid in kind but exceeds the bounded work this tool accepts.

The pure path caps at **1,000 steps**. It is O(V + E), does not load z3, and
the cap also bounds a caller's possible edge set. On this Apple-silicon host,
five fresh calls on dependency chains measured:

| Steps | Median | Min to max |
|---:|---:|---:|
| 100 | 0.350 ms | 0.333 to 0.386 ms |
| 500 | 4.010 ms | 3.886 to 4.059 ms |
| 1,000 | 14.638 ms | 13.851 to 15.548 ms |

Method: `time.perf_counter()` around `plan_order`, five calls per size in one
Python 3.14.6 process, chain `s0 -> ... -> sN`, z3 5.1.0 installed but unused.

The constrained path caps at **100 steps**. Its model has up to one capacity
constraint per step and Optimize complexity depends on the constraint graph,
not only N; the existing 1 to 120 second solver timeout is the real per-call
backstop. Three fresh calls on deliberately symmetric independent steps with
`max_parallel=2` measured:

| Steps | Median | Min to max | Verdict |
|---:|---:|---:|---|
| 10 | 37.809 ms | 36.780 to 140.931 ms | ordered |
| 25 | 1,224.514 ms | 1,153.442 to 1,525.964 ms | ordered |
| 50 | 19,718.320 ms | 19,478.008 to 19,992.252 ms | ordered |

Method: the same process and timer, three calls per size, z3 5.1.0, default
30-second timeout. The curve is why the z3 cap is one tenth of the pure cap and
why timeout returns `unknown` instead of relaxing correctness. The 100-step cap
is a model-construction ceiling, not a promise that every 100-step constraint
set decides inside 30 seconds; callers may raise the solver budget to the
120-second clamp.

## Result and proof grade

`ordered`, `cyclic`, and solver-proven `infeasible` results are exact and carry
the project's `solver_proven` grade with a basis naming Kahn, the graph cycle
walk, or z3 and its timeout. `unknown` is always `ungraded`. The new result
shape is additive, so the result contract moves from 1.18.0 to 1.19.0 under the
contract's MINOR policy; argument failures remain the existing `rejected`
shape.
