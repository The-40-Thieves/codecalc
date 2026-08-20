---
name: codecalc
description: Use when about to state a number, claim two programs are equivalent, claim a speedup, or state a complexity — codecalc computes these exactly instead of estimating them, and this file says when calling is mandatory and how results must be reported.
---

# codecalc

You are a language model. You are good at writing code and bad at arithmetic,
and the failure is not that you are unsure — it is that you are confident. This
file exists because "call a tool when you are uncertain" is worthless advice to
a model that feels certain about `0.1 + 0.2`.

So the rules below key on the SITUATION, never on how sure you feel.

## Call these — mandatory, no exceptions

Not a preference. If the situation matches, the call happens before you write
the number.

| Situation | Tool |
|---|---|
| Any **non-integer** operand or result | `calc_exact` |
| Any **comparison** whose verdict you will state | `compare_threshold` |
| Integers past **2^53** (9007199254740992) | `calc_exact`, and `float_repr` if a float is involved |
| Percentages, ratios, shares | `percentage` |
| Unit, byte, duration or epoch conversion | `convert_units`, `data_sizes`, `human_duration`, `epoch_time` |
| Any number that appears in your answer **as a claim** rather than an illustration | whichever of the above fits |
| You ported code A → B and are about to call it a port | `verify_translation` |
| You are about to state that a rewrite is **faster** | `verify_optimization` |
| You are about to say two languages **behave the same** | `compare_edge_cases` |
| You are about to state a **Big-O** | `analyze_complexity` (inferred) or `benchmark` (measured) — and say which |
| Any **bitwise op, shift, or mask** result you will state | `bitop`, `bit_analysis` |
| Any value you will state in **hex, binary, or another base** | `radix_convert`, `base_repr` |
| Any **summary statistic** (mean, median, stdev, percentile) you will state | `calc_stats`, `percentiles` |
| Before stating the **solution to an equation or system** | `solve_expression`, `solve_linear` |

**2^53 is not academic.** `float_repr(9007199254740993)` reports
`stored: "9007199254740992"`. The value changed and nothing raised. That is the
boundary, and `int_widths` will not show it to you — that tool reports machine
integer widths (i8…u64), which is a different question.

## Do not call these — no justification needed

Calling a tool for these is noise, and a rule that fires constantly is a rule
someone turns off:

- small-integer intermediate arithmetic — `2 + 3 + 4` needs no tool
- order-of-magnitude estimates you label as estimates
- arithmetic inside prose that nobody will act on
- `execute_code` to confirm syntax you can read

Operand count is **not** the test. `0.1 + 0.2` has two operands and is the
canonical failure; `2 + 3 + 4` has three and is never wrong. Type predicts
error. Length does not.

## Report what came back — mandatory, no exceptions

This is the second-order failure: call the tool correctly, then misreport it.
Every field below exists because the result would otherwise imply something
untrue, and each was added after that implication bit someone.

| Field | What you must say |
|---|---|
| `passed: true` | State `total`. "Equivalent on 7 inputs" — never "verified equivalent" |
| `inconclusive > 0` | Surface it. Not a pass, not a failure, and the caller is entitled to know which they got |
| `unenforced: [...]` non-empty | The guarantee you are about to describe **did not hold**. Name what was not applied |
| `backend: "python"` | Weaker sandbox. `no_net` was not applied and `peak_memory_kb` is `None` |
| `analysis: "regex-fallback"` | The structure was **guessed**, not parsed |
| `method: "static-estimate"` | Nobody measured this. `benchmark` reports `method: "empirical"` |
| `output_error` present | `stdout`/`stderr` are **not** what the program produced |
| `ok: false` | Say what failed. The `error` field is written to be quoted |

**Never drop a field you do not understand. Quote it.**

That rule is not for careless models. In one session an agent verifying this
very codebase — carefully, with the source open — made three probe errors in a
row: reversed arguments that made a nested loop report `O(1)`, a read of a
`divergences` key that does not exist (it is `mismatched`), and the same
wrong-key mistake on `algebraic_equiv` (it is `identical`). Each was caught only
by printing the whole result instead of the field it expected. Confidence about
a result's shape is exactly as unreliable as confidence about arithmetic.

## What this tool set will not do for you

- It does not check that a package or function **exists**. A fabricated API is
  still your error; `execute_code` with an import is the closest available check.
- `verify_translation` proves equivalence **on the inputs it ran**, which are
  `DEFAULT_EDGE_INPUTS` unless you pass your own. It is evidence, not proof.
  Pass inputs that would distinguish your port if it were wrong.
- `analyze_complexity` reads structure. It cannot see work hidden inside a
  library call.
