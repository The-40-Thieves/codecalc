# Tool-selection eval — BM25 vs. real models

Two scripts answer two different questions about the same labeled corpus
(`scripts/data/tool_select_prompts.jsonl`, 196 hand-labeled prompts, none
containing their own target tool's name):

| | `scripts/tool_select_eval.py` | `scripts/tool_select_llm_eval.py` |
|---|---|---|
| Selector | Okapi BM25 (lexical, deterministic, no network) | a real chat model, over a live OpenAI-compatible gateway |
| Answers | "did this description trim throw away discriminating vocabulary?" | "would an actual MCP client's model pick the right tool?" |
| CI | every PR (`ci-quality.yml`, offline) | `workflow_dispatch` only, opt-in, needs a live gateway + API key |
| Gate | hard (`--baseline` fails the build) | advisory (`--baseline` warns; `--strict` for a hard local check) |

Neither replaces the other. BM25 is a cheap, reproducible proxy that runs on
every PR with no external dependency; a model call is the real question but
costs money, is not bit-for-bit reproducible, and needs a live endpoint — so
it stays opt-in rather than gating every PR.

## Method

For every prompt applicable to a `CODECALC_TOOLS` group (`full`/`dev`/`core`
— a prompt is "applicable" if at least one of its labeled `expected` tools
is present in that group's surface), `tool_select_llm_eval.py` makes two
calls to the model, both `temperature=0`:

1. **TOOLS mode** — the real tool catalog as OpenAI `tools=[...]`
   (name + description + the exact MCP `inputSchema`), `tool_choice`
   forced to `"required"`. The tool the model calls is **top-1**.
2. **LIST mode** — no `tools` param; the same catalog inlined as text, the
   model asked for a ranked JSON array of its top three tool names. This is
   **top-3**, and it also supplies the top-1 **fallback** for any model (or
   prompt) where TOOLS mode fails outright — a live gateway 400 for a model
   with no function-calling support, or a response with no `tool_calls` at
   all.

Every applicable prompt makes both calls regardless of surface size, so
`full`'s 52-tool catalog and `core`'s 25-tool one cost roughly the same
*number* of calls but very different token counts per call (see the token
usage numbers below).

**`top-3` is STRICT**: a hit iff `expected` intersects LIST mode's own
three-name answer, nothing else. `aggregate()` also reports
`top1_or_list_top3` — the union of TOOLS mode's single pick with LIST mode's
three — under its own name, because the two modes are genuinely different
questions ("what does LIST mode's own ranking contain" vs. "does either mode
get it right") and folding one into the other silently overstates the first.
A cross-vendor review of the first live run caught exactly this: v1's `top3`
unioned in `effective_top1` (usually TOOLS mode's answer, from a DIFFERENT
call) before checking the hit, which counted a correct TOOLS-mode call as
"top-3" even on prompts where LIST mode's own three picks never named the
tool at all (147 of 898 records in the first live run). See "Measured"
below for how large that gap actually was, per model.

**The cache key folds in a hash of the exact `tools=[...]` payload**
(`tools_payload_hash`) alongside `(model, group, prompt)` — editing a tool's
description or `inputSchema` invalidates every cached call for that group
rather than silently replaying a response scored against wording that no
longer exists. A run that gets zero cache hits for a `(model, group)` with
an existing baseline entry prints a `::warning::` naming it, since that
shape means either the catalog changed since the baseline was measured or
the cache directory was cleared — either way, worth a human noticing before
trusting the regression compare.

## Measured — 2026-09-08 (corrected 2026-09-08: `top-3` is now strict)

Models: `anthropic/claude-haiku-4-5-20251001` and `openai/gpt-4o-mini` — one
Anthropic model, one non-Anthropic model, so the numbers below are not a
single vendor's self-report. Both were live on the gateway's `GET /v1/models`
at the time of this run; see `scripts/tool_select_llm_eval.py --models` to
substitute others.

These numbers come from the SAME live run as the first cut of this eval —
the `tool_top1`/`list_top3` data itself did not change, only how `top-3` is
computed from it (see "Method" above) — reprocessed from the on-disk cache
with **zero new network calls** (verified: a rerun against a deliberately
wrong API key completed instantly, 898/898 cache hits, since a real call
would have failed authentication rather than succeeding).

| Model | Group | n | Model top-1 | Model top-3 (strict) | Model top-1-or-list-top-3 | BM25 top-1 | BM25 top-3 | Disagreements | Tokens |
|---|---|---|---|---|---|---|---|---|---|
| `anthropic/claude-haiku-4-5-20251001` | full | 196 | 83.7% (164/196) | 68.4% (134/196) | 91.3% (179/196) | 62.8% (123/196) | 79.1% (155/196) | 96 | 5,534,354 |
| `anthropic/claude-haiku-4-5-20251001` | dev | 153 | 96.1% (147/153) | 69.3% (106/153) | 96.7% (148/153) | 65.4% (100/153) | 81.7% (125/153) | 59 | 3,046,461 |
| `anthropic/claude-haiku-4-5-20251001` | core | 100 | 98.0% (98/100) | 78.0% (78/100) | 99.0% (99/100) | 68.0% (68/100) | 83.0% (83/100) | 36 | 898,457 |
| `openai/gpt-4o-mini` | full | 196 | 91.3% (179/196) | 98.5% (193/196) | 100.0% (196/196) | 62.8% (123/196) | 79.1% (155/196) | 89 | 4,162,849 |
| `openai/gpt-4o-mini` | dev | 153 | 96.1% (147/153) | 98.7% (151/153) | 100.0% (153/153) | 65.4% (100/153) | 81.7% (125/153) | 59 | 2,275,477 |
| `openai/gpt-4o-mini` | core | 100 | 98.0% (98/100) | 100.0% (100/100) | 100.0% (100/100) | 68.0% (68/100) | 83.0% (83/100) | 35 | 645,957 |

**Total tokens across the whole run: 16,563,555** (prompt + completion, both
modes, both models, all three groups; the gateway's own `usage` field, summed
— no live pricing feed exists here, so this is tokens, not dollars). TOOLS
mode was **100% reliable for both models** (`top1_source` is `{"tool": n}`
for every model/group — zero `list_fallback`, zero `none`), so every top-1
number above came from a real tool call, never the list-mode fallback.

Both models beat BM25's top-1 by a wide margin on every surface, and top-1's
score gets BETTER as the surface shrinks — `core`'s 25-tool catalog scores
~98% for both models against a distractor set 1/2 the size of `full`'s — the
same "less headroom to lose on `full`, more on `core`" shape
`tool_select_eval.py`'s own self-check documents for BM25.

**Strict top-3 is where the two models genuinely diverge, and it is not
close.** `openai/gpt-4o-mini`'s LIST-mode ranking is excellent on its own
(98.5-100% strict top-3, matching its `top1_or_list_top3` numbers almost
exactly — its ranked list nearly always contains its own top-1 pick).
`anthropic/claude-haiku-4-5-20251001`'s strict top-3 is markedly worse
(68.4-78.0%) despite a strong top-1 (83.7-98.0%) — the ~15-30pp gap between
its top-1 and its own top-1-or-list-top-3 column is exactly the set of
prompts where LIST mode's three picks did not include what TOOLS mode
correctly called directly. This is a real, measured difference in how the
two models rank under a plain-text ask with no forced schema, not an
artifact of this eval's method (see the LIST-mode caveat below for the
separate, smaller effect of LIST mode sometimes declining outright).

The **disagreements** column counts prompts where BM25's own top-1 pick and
the model's effective top-1 pick differ — most of these are not the model
being *wrong*, they are BM25 being wrong where a model's semantic read of
the prompt succeeds; the full per-prompt list is in the run's `--json`
report (`disagreements_with_bm25` per model/group), not reproduced here.

## Reproducing this

```bash
export CODECALC_EVAL_BASE_URL=https://your-openai-compatible-gateway/v1
export CODECALC_EVAL_API_KEY=...          # read via os.environ only — never a CLI flag
python scripts/tool_select_llm_eval.py --groups full,dev,core --json report.json
```

Reruns are cheap: results are cached under `scripts/data/.llm_eval_cache/`
(gitignored), keyed by `(model, group, prompt, tools_hash)` — an interrupted
or partial run resumes without re-paying for calls it already made, and an
edited description or `inputSchema` (a different `tools_hash`) is a cache
miss rather than a stale hit. `--no-cache` forces a refresh regardless.

## A finding from the first live run: LIST mode sometimes declines

TOOLS mode (which drives top-1) was 100% reliable across both models in the
first live run — every prompt produced a real `tool_calls` response, zero
`tool_error`s. LIST mode is not: without a forced `tool_choice`, a model
shown a prompt like "summarize these latency numbers" with no actual numbers
attached sometimes replies conversationally asking for the missing data
instead of complying with the "just return a ranked JSON array" system
instruction — a genuine, reproducible model behavior, not a parsing bug (see
`_parse_ranked_names`'s own handling: it returns `[]` rather than raising).
This does not lower top-1 (TOOLS mode supplies it directly, unaffected). It
does cost the STRICT `top-3` metric directly: a decline means `list_top3` is
empty, so strict top-3 is a guaranteed miss on that prompt regardless of
whether TOOLS mode got it right — one contributor (alongside genuinely
ranking a correct tool outside its own top three) to the top-1 vs. strict
top-3 gap the table above shows for `anthropic/claude-haiku-4-5-20251001`.
`top1_or_list_top3` recovers a decline the same way it recovers any other
list_top3 miss, since it unions in `effective_top1` regardless of why LIST
mode came back empty — which is exactly why that column is reported
separately rather than folded into `top-3`, per the "Method" section above.
Not evidence the model could not rank three tools if asked differently (e.g.
with a forced JSON response format, which this script does not use so its
behavior stays comparable across gateways that may not support it).

## What this does NOT prove

Same caveat `tool_select_eval.py`'s own docstring states for BM25, mirrored
here for the model half: a single run against one prompt set and two models
is evidence, not a guarantee, that a *different* model, prompt phrasing, or
tool-description edit behaves the same way. The `--baseline` compare exists
to catch a regression against *this* corpus and *these* models over time —
it is not a claim that the numbers generalize beyond them.
