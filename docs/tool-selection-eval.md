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

## Measured — 2026-09-08

Models: `anthropic/claude-haiku-4-5-20251001` and `openai/gpt-4o-mini` — one
Anthropic model, one non-Anthropic model, so the numbers below are not a
single vendor's self-report. Both were live on the gateway's `GET /v1/models`
at the time of this run; see `scripts/tool_select_llm_eval.py --models` to
substitute others.

| Model | Group | n | Model top-1 | Model top-3 | BM25 top-1 | BM25 top-3 | Disagreements | Tokens |
|---|---|---|---|---|---|---|---|---|
| `anthropic/claude-haiku-4-5-20251001` | full | 196 | 83.7% (164/196) | 91.3% (179/196) | 62.8% (123/196) | 79.1% (155/196) | 96 | 5,534,354 |
| `anthropic/claude-haiku-4-5-20251001` | dev | 153 | 96.1% (147/153) | 96.7% (148/153) | 65.4% (100/153) | 81.7% (125/153) | 59 | 3,046,461 |
| `anthropic/claude-haiku-4-5-20251001` | core | 100 | 98.0% (98/100) | 99.0% (99/100) | 68.0% (68/100) | 83.0% (83/100) | 36 | 898,457 |
| `openai/gpt-4o-mini` | full | 196 | 91.3% (179/196) | 100.0% (196/196) | 62.8% (123/196) | 79.1% (155/196) | 89 | 4,162,849 |
| `openai/gpt-4o-mini` | dev | 153 | 96.1% (147/153) | 100.0% (153/153) | 65.4% (100/153) | 81.7% (125/153) | 59 | 2,275,477 |
| `openai/gpt-4o-mini` | core | 100 | 98.0% (98/100) | 100.0% (100/100) | 68.0% (68/100) | 83.0% (83/100) | 35 | 645,957 |

**Total tokens across the whole run: 16,563,555** (prompt + completion, both
modes, both models, all three groups; the gateway's own `usage` field, summed
— no live pricing feed exists here, so this is tokens, not dollars). TOOLS
mode was **100% reliable for both models** (`top1_source` is `{"tool": n}`
for every model/group — zero `list_fallback`, zero `none`), so every top-1
number above came from a real tool call, never the list-mode fallback.

Both models beat BM25 by a wide margin on every surface, and the model's
score gets BETTER as the surface shrinks — `core`'s 25-tool catalog scores
~98% for both models against a distractor set 1/2 the size of `full`'s — the
same "less headroom to lose on `full`, more on `core`" shape
`tool_select_eval.py`'s own self-check documents for BM25. `openai/gpt-4o-mini`
reached a perfect 100% top-3 on every single group; `anthropic/claude-haiku-4-5-20251001`
came close (91-99%) but not perfect on `full`/`core`. Neither model's TOOLS-mode
call ever failed, so top-1 is not diluted by any fallback path here — see
the LIST-mode caveat below for where top-3 (not top-1) has a real, measured
limitation from this eval's own method rather than the model's ability.

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
(gitignored), keyed by `(model, group, prompt)` — an interrupted or partial
run resumes without re-paying for calls it already made. `--no-cache` forces
a refresh.

## A finding from the first live run: LIST mode sometimes declines

TOOLS mode (which drives top-1) was 100% reliable across both models in the
first live run — every prompt produced a real `tool_calls` response, zero
`tool_error`s. LIST mode is not: without a forced `tool_choice`, a model
shown a prompt like "summarize these latency numbers" with no actual numbers
attached sometimes replies conversationally asking for the missing data
instead of complying with the "just return a ranked JSON array" system
instruction — a genuine, reproducible model behavior, not a parsing bug (see
`_parse_ranked_names`'s own handling: it returns `[]` rather than raising).
This does not lower top-1 (TOOLS mode supplies it directly, unaffected), but
it does mean `top-3` sometimes gets exactly one candidate (`{tool_top1}`,
via `aggregate()`'s candidate-set union) rather than three, on prompts where
LIST mode declined — a real, documented limitation of this eval's own
method, not evidence the model could not rank three tools if asked
differently (e.g. with a forced JSON response format, which this script does
not use so its behavior stays comparable across gateways that may not
support it).

## What this does NOT prove

Same caveat `tool_select_eval.py`'s own docstring states for BM25, mirrored
here for the model half: a single run against one prompt set and two models
is evidence, not a guarantee, that a *different* model, prompt phrasing, or
tool-description edit behaves the same way. The `--baseline` compare exists
to catch a regression against *this* corpus and *these* models over time —
it is not a claim that the numbers generalize beyond them.
