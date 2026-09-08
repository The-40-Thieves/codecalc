#!/usr/bin/env python3
"""Model-driven tool-SELECTION eval: does a real LLM pick the right codecalc
tool for a labeled prompt when shown the tool list exactly as an MCP client
sees it — names, descriptions, AND `inputSchema` — per `CODECALC_TOOLS`
group (`full`/`dev`/`core`)?

WHY THIS EXISTS. `scripts/tool_select_eval.py`'s own "HONEST EXPECTATIONS"
section says its BM25 selector "cannot tell you whether an actual LLM
tool-selector would pick correctly — a real model reasons over meaning,
synonymy and world knowledge no term-frequency ranker has access to." This
script is that missing half. It reuses that script's checked-in labeled
corpus (`scripts/data/tool_select_prompts.jsonl`), its live tool-schema
loader pattern, and its `BM25`/`evaluate()` so every number this script
prints sits next to the exact BM25 figure for the same group and prompt set
— not a hand-copied one liable to drift.

TRANSPORT. OpenAI-compatible `POST {base_url}/chat/completions`, stdlib
`urllib` only — `openai` is not a dependency of this project (see
pyproject.toml) and this script does not add one. Both `CODECALC_EVAL_BASE_URL`
and `CODECALC_EVAL_API_KEY` come from the environment ONLY — the key is never
accepted as a CLI argument, never printed, never logged, never written to the
cache or the JSON report; every string derived from an HTTP response is
scrubbed for an exact match on the key before it is stored or printed (see
`_scrub`). If either variable is unset, `main()` refuses to make any network
call and exits 2 — the script stays fully importable/testable and its own
test suite runs entirely offline against a local fake server.

TWO CALLS PER PROMPT, not one. A tool-calling request only ever returns ONE
chosen function, so it cannot produce a top-3 the way BM25's `rank()` can.
Every applicable prompt therefore gets:
  1. a TOOLS-MODE call — the real tool catalog passed as OpenAI `tools=[...]`
     (name + description + the exact MCP `inputSchema`), `tool_choice`
     forced to `"required"` so the model must call exactly one — this is
     top-1.
  2. a LIST-MODE call — no `tools` param; the same catalog is inlined as
     text and the model is asked to return a ranked JSON array of its top
     three tool names — this is top-3.
If the tools-mode call fails outright (HTTP error, or a response with no
`tool_calls` at all — some models 400 on an unrecognized `tools` field,
others silently reply in prose instead), list-mode's own #1 pick is used as
the FALLBACK top-1, and the record's `top1_source` says which mode won
(`"tool"` vs `"list_fallback"` vs `"none"` if both failed) — this is the
"ranked JSON list... when the model does not support tool calls" mode the
brief asks for, generalized to any tools-mode failure rather than a single
model-name allowlist that would need maintaining.

DETERMINISM, loosely. `temperature=0` and `seed=0` on every call — "loosely"
because `seed` is an OpenAI-specific best-effort hint the gateway may pass
through, drop, or ignore per-provider; nothing here depends on it being
honored, and BM25's own tie-break is the only fully reproducible half of
this comparison.

CACHE. Resumable, keyed by `sha256(f"{model}\\x1f{group}\\x1f{prompt}")`,
one JSON file per key under `scripts/data/.llm_eval_cache/` (gitignored). A
rerun with the same cache dir makes zero network calls for anything already
recorded — `--no-cache` forces a refresh.

CONCURRENCY. A single `ThreadPoolExecutor(max_workers=--concurrency)` shared
across every (model, group, prompt) unit in the run — `--concurrency`
defaults to 4. Retries on HTTP 429/5xx and on a connection error, capped
exponential backoff, `--max-retries` attempts before the call is recorded as
a failure (never silently dropped — a failed call still produces a record
with `*_error` set, and the aggregate `top1_source` counts how many prompts
fell back or failed outright).

OUTPUT. Per (model, group): top-1/top-3 counts and ratios NEXT TO the BM25
numbers for the identical schemas and prompt set (via `tool_select_eval`'s
own `BM25`/`evaluate`), the confusion list (expected vs chosen, top-1 misses
only), the prompts where BM25's top-1 and the model's effective top-1
disagree, and summed token usage from every call's `usage` field (the
gateway reports usage; no live pricing feed exists here, so this is tokens,
not dollars). `--json PATH` writes the full report; a human summary always
prints to stdout.

BASELINE, ADVISORY BY DEFAULT. `--baseline` (default
`scripts/data/tool_select_llm_baseline.json`) compares current top-1 hit
counts against a checked-in snapshot, same integer-hit-count shape as
`tool_select_eval.py --baseline` (see that script's own `DEFAULT_EPSILON_HITS`
docstring for why hit counts, not float ratios). UNLIKE that script, a
regression here prints `::warning::` and still exits 0 — an LLM's answer to
the same prompt is not perfectly reproducible run to run the way BM25's is,
so a hard CI gate on it would be noisy by construction. `--strict` turns the
same warnings into a nonzero exit for a reviewer who wants that stricter
behavior locally.

USAGE
    # env: CODECALC_EVAL_BASE_URL, CODECALC_EVAL_API_KEY must both be set
    python scripts/tool_select_llm_eval.py
    python scripts/tool_select_llm_eval.py --models anthropic/claude-haiku-4-5-20251001,openai/gpt-4o-mini
    python scripts/tool_select_llm_eval.py --groups full,dev,core --json report.json
    python scripts/tool_select_llm_eval.py --baseline scripts/data/tool_select_llm_baseline.json --strict
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]

# Mirrors tool_select_eval.py's own `REPO / "scripts"` insertion (see that
# script's test for the same pattern) — this lets `tool_select_llm_eval.py`
# reuse the checked-in BM25 selector and prompt loader rather than
# re-implementing or hand-copying either.
sys.path.insert(0, str(REPO / "scripts"))
import tool_select_eval as tse  # noqa: E402 — needs the path insert above

ENV_BASE_URL = "CODECALC_EVAL_BASE_URL"
ENV_API_KEY = "CODECALC_EVAL_API_KEY"

#: Two model families by default (never a single vendor) — an Anthropic model
#: and a cheap OpenAI one, both confirmed present on this deployment's
#: `GET /v1/models` at the time this script was written. Overridable with
#: `--models`; nothing here hardcodes a gateway address (see check_portability.py
#: — a private tailnet address in a tracked file fails that gate on sight, so
#: `CODECALC_EVAL_BASE_URL` has NO default value in this file, only in the
#: environment of whoever runs it).
DEFAULT_MODELS = ("anthropic/claude-haiku-4-5-20251001", "openai/gpt-4o-mini")

DEFAULT_CACHE_DIR = REPO / "scripts" / "data" / ".llm_eval_cache"
DEFAULT_LLM_BASELINE = REPO / "scripts" / "data" / "tool_select_llm_baseline.json"

DEFAULT_TIMEOUT = 30.0
DEFAULT_CONCURRENCY = 4
DEFAULT_MAX_RETRIES = 5
#: Regression tolerance for --baseline, same integer-hit-count shape as
#: tool_select_eval.py's DEFAULT_EPSILON_HITS — see that constant's docstring
#: for why an integer hit count, not a float ratio. Looser here (2 vs BM25's
#: 1) because an LLM's answer is not bit-for-bit reproducible the way BM25's
#: alphabetical tie-break is.
DEFAULT_EPSILON_HITS = 2
#: HTTP statuses worth retrying — transient rate-limit/server-side failures.
#: 400/401/403/404 etc. are never retried: retrying a malformed request or a
#: bad credential just burns the retry budget on a call that will never
#: succeed.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

TOOL_MODE_SYSTEM = (
    "You are the tool-selection layer for an MCP client connected to codecalc, "
    "a code-execution and math/logic calculator server. Read the user's request "
    "and call EXACTLY ONE of the available tools -- the single best match. Do "
    "not call more than one tool, and do not reply with plain text."
)

LIST_MODE_SYSTEM_TEMPLATE = (
    "You are the tool-selection layer for an MCP client connected to codecalc, "
    "a code-execution and math/logic calculator server. Below is the exact "
    "tool catalog an MCP client would see, one JSON object per line "
    '(`name`, `description`, `inputSchema`). Given the user\'s request, reply '
    "with a JSON array of EXACTLY THE TOP THREE tool names from the catalog, "
    "ranked best match first -- for example [\"tool_a\", \"tool_b\", \"tool_c\"]. "
    "Reply with ONLY the JSON array: no prose, no markdown code fences, no "
    "explanation.\n\nTOOL CATALOG:\n{catalog}"
)


class LLMCallError(RuntimeError):
    """A chat-completions call failed after exhausting its retry budget."""


def _scrub(text: str, secret: str | None) -> str:
    """Redact an exact occurrence of `secret` from `text`.

    Defense in depth for the secrets rule: normally `api_key` only ever
    appears in an outgoing `Authorization` header we build ourselves, never
    in a response body — but nothing here trusts that a broken proxy or a
    misconfigured fake server in a test could not echo a header back into an
    error body. Every string derived from an HTTP response passes through
    this before it is stored in the cache, the report, or printed.
    """
    if not secret or not text:
        return text
    return text.replace(secret, "***REDACTED***")


def _post_json(base_url: str, api_key: str, payload: dict, timeout: float,
                max_retries: int) -> dict:
    """POST `payload` to `{base_url}/chat/completions`, retrying transient
    failures with capped exponential backoff. Raises `LLMCallError` (message
    scrubbed of `api_key`) once the retry budget is exhausted or on a
    non-retryable HTTP status.
    """
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            f"{ENV_BASE_URL} must be an absolute HTTP(S) URL, got {base_url!r}"
        )
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    attempt = 0
    while True:
        request = Request(url, data=body, headers=headers, method="POST")  # noqa: S310 -- scheme validated above
        try:
            with urlopen(request, timeout=timeout) as resp:  # noqa: S310 -- scheme validated above
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = _scrub(exc.read().decode("utf-8", errors="replace")[:2000], api_key)
            if exc.code in RETRY_STATUSES and attempt < max_retries:
                attempt += 1
                time.sleep(min(2**attempt, 30))
                continue
            raise LLMCallError(f"HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            if attempt < max_retries:
                attempt += 1
                time.sleep(min(2**attempt, 30))
                continue
            raise LLMCallError(f"connection error: {_scrub(str(exc.reason), api_key)}") from exc


# ── live tool registry, WITH input schemas (tool_select_eval.py's own probe
#    stops at description) ───────────────────────────────────────────────────

#: Same fresh-subprocess pattern as tool_select_eval.py's `_PROBE` and for the
#: same reason (`CODECALC_TOOLS` is read once, at import, so a second import
#: in this process would return the first import's cached surface) — extended
#: to also carry `t.parameters`, the exact JSON-schema `inputSchema` an MCP
#: `tools/list` response carries, which this script needs and the BM25 eval
#: does not.
_SCHEMA_PROBE = (
    "import json\n"
    "from codecalc import server\n"
    "tools = server.mcp._tool_manager._tools\n"
    "print(json.dumps({\n"
    "    name: {\n"
    "        'description': t.description,\n"
    "        'input_schema': t.parameters,\n"
    "        'group': server.TOOL_GROUPS.get(name),\n"
    "    }\n"
    "    for name, t in tools.items()\n"
    "}))\n"
)


def load_tool_schemas(tools_group: str) -> dict[str, dict]:
    """`{tool_name: {"description", "input_schema", "group"}}` for every tool
    `CODECALC_TOOLS=<tools_group>` registers, read from a live import of
    `codecalc.server` — the same names/descriptions/schemas an MCP client
    connecting to that configuration would see over `tools/list`.
    """
    import subprocess

    env = dict(os.environ)
    env["CODECALC_TOOLS"] = tools_group
    proc = subprocess.run(
        [sys.executable, "-c", _SCHEMA_PROBE],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"loading tool schemas for --tools {tools_group!r} failed "
            f"(rc={proc.returncode}): {proc.stderr[-2000:]}"
        )
    return json.loads(proc.stdout)


def build_tools_payload(schemas: dict[str, dict]) -> list[dict]:
    """OpenAI `tools=[...]` — the exact name/description/inputSchema triple,
    sorted by name so a run is byte-for-byte reproducible in what is SENT
    (the model's own answer is a separate matter).
    """
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": info["description"] or "",
                "parameters": info["input_schema"] or {"type": "object", "properties": {}},
            },
        }
        for name, info in sorted(schemas.items())
    ]


def build_catalog_text(schemas: dict[str, dict]) -> str:
    """One JSON object per line — name/description/inputSchema — sorted by
    name. This is the text inlined into the LIST-MODE prompt; it carries the
    identical information `build_tools_payload` structures for TOOLS-MODE, so
    a model sees the same catalog either way.
    """
    lines = [
        json.dumps(
            {"name": name, "description": info["description"], "inputSchema": info["input_schema"]},
            sort_keys=True,
        )
        for name, info in sorted(schemas.items())
    ]
    return "\n".join(lines)


# ── one chat-completions call, each mode ────────────────────────────────────

def call_tool_mode(base_url: str, api_key: str, model: str, tools_payload: list[dict],
                    prompt: str, timeout: float, max_retries: int) -> dict:
    """Returns `{"top1": str|None, "error": str|None, "usage": dict|None}`."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": TOOL_MODE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "tools": tools_payload,
        "tool_choice": "required",
        "temperature": 0,
        "seed": 0,
    }
    try:
        resp = _post_json(base_url, api_key, payload, timeout, max_retries)
    except LLMCallError as exc:
        return {"top1": None, "error": str(exc), "usage": None}
    choice = (resp.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    usage = resp.get("usage")
    if not tool_calls:
        return {
            "top1": None,
            "error": f"no tool_calls in response (finish_reason={choice.get('finish_reason')!r})",
            "usage": usage,
        }
    name = (tool_calls[0].get("function") or {}).get("name")
    return {"top1": name, "error": None, "usage": usage}


#: The model's reply may still wrap the array in prose or a markdown fence
#: despite being told not to — this pulls out the first `[...]` span rather
#: than requiring the whole reply to be valid JSON on its own.
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.S)


def _parse_ranked_names(text: str, schemas: dict[str, dict]) -> list[str]:
    """Best-effort extraction of a ranked tool-name list from free text.

    Filters to names actually present in `schemas` (a hallucinated tool name
    cannot be a "hit" against anything) and de-duplicates, preserving order,
    capped to 3 — the shape `evaluate_prompt`'s `top3` slot needs regardless
    of how many names the model actually returned.
    """
    match = _JSON_ARRAY_RE.search(text or "")
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    seen: list[str] = []
    for item in parsed:
        if isinstance(item, str) and item in schemas and item not in seen:
            seen.append(item)
        if len(seen) == 3:
            break
    return seen


def call_list_mode(base_url: str, api_key: str, model: str, catalog_text: str,
                    schemas: dict[str, dict], prompt: str, timeout: float,
                    max_retries: int) -> dict:
    """Returns `{"top3": list[str], "error": str|None, "usage": dict|None}`."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": LIST_MODE_SYSTEM_TEMPLATE.format(catalog=catalog_text)},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "seed": 0,
    }
    try:
        resp = _post_json(base_url, api_key, payload, timeout, max_retries)
    except LLMCallError as exc:
        return {"top3": [], "error": str(exc), "usage": None}
    choice = (resp.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    usage = resp.get("usage")
    ranked = _parse_ranked_names(content, schemas)
    if not ranked:
        return {"top3": [], "error": f"could not parse a ranked tool list from: {content[:300]!r}",
                "usage": usage}
    return {"top3": ranked, "error": None, "usage": usage}


# ── resumable cache ──────────────────────────────────────────────────────────

def tools_payload_hash(tools_payload: list[dict]) -> str:
    """sha256 over the EXACT `tools=[...]` payload sent for a group — names,
    descriptions, and `inputSchema`, in the sorted order `build_tools_payload`
    already produces. Folded into `cache_key` below so an edited description
    (the whole point of this eval, per the README's "Tool-definition token
    cost" section) invalidates every cached call for that group instead of
    silently replaying a stale response scored against a description that no
    longer exists.
    """
    return hashlib.sha256(json.dumps(tools_payload, sort_keys=True).encode()).hexdigest()


def cache_key(model: str, group: str, prompt: str, tools_hash: str) -> str:
    """sha256 over `(model, group, tools_hash, prompt)`, unit-separator
    joined so no concatenation of the fields could collide with a different
    split. `tools_hash` (see `tools_payload_hash`) is what makes this cache
    description-sensitive, not just model/group/prompt-sensitive.
    """
    return hashlib.sha256(f"{model}\x1f{group}\x1f{tools_hash}\x1f{prompt}".encode()).hexdigest()


def cache_load(cache_dir: Path, key: str) -> dict | None:
    path = cache_dir / f"{key}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def cache_save(cache_dir: Path, key: str, record: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{key}.json"
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(path)  # atomic on the same filesystem


# ── per-prompt evaluation ────────────────────────────────────────────────────

def evaluate_prompt(base_url: str, api_key: str, model: str, group: str, prompt: str,
                     tools_payload: list[dict], tools_hash: str, catalog_text: str,
                     schemas: dict[str, dict], timeout: float, max_retries: int,
                     cache_dir: Path, use_cache: bool) -> tuple[dict, bool]:
    """One `(model, group, prompt)` unit: both calls (unless cached), scrubbed
    of the API key, written back to the cache. Returns `(record, from_cache)`
    — the bool is never itself cached (it describes THIS run's cache
    behavior, not the record's), only used by `main()` to warn when a group
    got zero cache hits despite an existing baseline (see `tools_payload_hash`).
    """
    key = cache_key(model, group, prompt, tools_hash)
    if use_cache:
        cached = cache_load(cache_dir, key)
        if cached is not None:
            return cached, True

    tool_result = call_tool_mode(base_url, api_key, model, tools_payload, prompt, timeout, max_retries)
    list_result = call_list_mode(base_url, api_key, model, catalog_text, schemas, prompt, timeout, max_retries)

    record = {
        "prompt": prompt,
        "tool_top1": tool_result["top1"],
        "tool_error": _scrub(tool_result["error"], api_key) if tool_result["error"] else None,
        "tool_usage": tool_result["usage"],
        "list_top3": list_result["top3"],
        "list_error": _scrub(list_result["error"], api_key) if list_result["error"] else None,
        "list_usage": list_result["usage"],
    }
    if use_cache:
        cache_save(cache_dir, key, record)
    return record, False


# ── aggregation ──────────────────────────────────────────────────────────────

def _usage_add(totals: dict, usage: dict | None) -> None:
    if not usage:
        return
    totals["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
    totals["completion_tokens"] += usage.get("completion_tokens", 0) or 0
    totals["total_tokens"] += usage.get("total_tokens", 0) or 0
    totals["calls"] += 1


def aggregate(schemas: dict[str, dict], prompt_records: list[tuple[dict, dict]],
              bm25_result: dict) -> dict:
    """`prompt_records`: `[(prompt_entry, evaluate_prompt() record), ...]` for
    one (model, group). Returns top-1/top-3 next to the BM25 numbers for the
    same schemas/prompts, the confusion list, and the BM25-disagreement list.

    `top3` is STRICT: hit iff `expected` intersects `list_top3` — the LIST
    call's OWN ranked answer, nothing else. A prior cut of this function
    unioned in `effective_top1` (which is usually `tool_top1`, from a
    DIFFERENT call/mode) before checking the intersection, which counted a
    correct TOOLS-mode call as a "top-3" even on the 147/898 records (first
    live run) where `tool_top1` was not one of LIST mode's own three picks —
    inflating full/haiku from a real 134/196 to a reported 179/196. That
    union is still reported, under its own honest name
    (`top1_or_list_top3`), never folded into `top3` again.
    """
    docs = {name: f"{name} {info['description']}" for name, info in schemas.items()}
    bm25 = tse.BM25(docs)

    n = len(prompt_records)
    top1_hits = 0
    top3_hits = 0
    top1_or_list_top3_hits = 0
    top1_source: Counter[str] = Counter()
    confusion = []
    disagreements = []
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}

    for entry, record in prompt_records:
        expected = set(entry["expected"]) & set(schemas)
        tool_top1 = record["tool_top1"]
        list_top3 = record["list_top3"] or []

        if tool_top1 is not None:
            effective_top1 = tool_top1
            top1_source["tool"] += 1
        elif list_top3:
            effective_top1 = list_top3[0]
            top1_source["list_fallback"] += 1
        else:
            effective_top1 = None
            top1_source["none"] += 1

        hit1 = effective_top1 in expected
        hit3_strict = bool(set(list_top3) & expected)
        union_candidates = set(list_top3)
        if effective_top1 is not None:
            union_candidates.add(effective_top1)
        hit3_union = bool(union_candidates & expected)

        if hit1:
            top1_hits += 1
        if hit3_strict:
            top3_hits += 1
        if hit3_union:
            top1_or_list_top3_hits += 1
        if not hit1:
            confusion.append({
                "prompt": entry["prompt"], "expected": sorted(expected),
                "got": effective_top1, "top3": list_top3,
            })

        bm25_top1 = bm25.rank(entry["prompt"])[0][0]
        if bm25_top1 != effective_top1:
            disagreements.append({
                "prompt": entry["prompt"], "expected": sorted(expected),
                "bm25_top1": bm25_top1, "model_top1": effective_top1,
            })

        _usage_add(usage_totals, record.get("tool_usage"))
        _usage_add(usage_totals, record.get("list_usage"))

    return {
        "n": n,
        "top1_hits": top1_hits, "top1": round(top1_hits / n, 4) if n else None,
        "top3_hits": top3_hits, "top3": round(top3_hits / n, 4) if n else None,
        "top1_or_list_top3_hits": top1_or_list_top3_hits,
        "top1_or_list_top3": round(top1_or_list_top3_hits / n, 4) if n else None,
        "top1_source": dict(top1_source),
        "confusion": confusion,
        "disagreements_with_bm25": disagreements,
        "disagreements_with_bm25_count": len(disagreements),
        "usage": usage_totals,
        "bm25": {
            "n": bm25_result["n"], "top1_hits": bm25_result["top1_hits"], "top1": bm25_result["top1"],
            "top3_hits": bm25_result["top3_hits"], "top3": bm25_result["top3"],
        },
    }


# ── baseline compare (advisory) ──────────────────────────────────────────────

def compare_baseline(report: dict, baseline_path: Path, epsilon: int) -> list[str]:
    """Returns a list of warning strings — empty means "no regression found".
    Never raises for a missing file or a corpus mismatch; both are reported
    as warnings, same as an accuracy drop, because this compare is advisory
    by design (see module docstring).
    """
    if not baseline_path.is_file():
        return [f"baseline file {baseline_path} does not exist — nothing to compare against"]
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"baseline file {baseline_path} is not valid JSON: {exc}"]

    warnings: list[str] = []
    if baseline.get("prompt_set_sha256") != report["prompt_set_sha256"]:
        return warnings + [
            (
                "CORPUS CHANGED since the baseline was generated — top-1 hit counts "
                "are not comparable across two different prompt sets; regenerate the "
                "baseline once the corpus change is reviewed and intentional"
            ),
        ]

    for model, by_group in report["results"].items():
        base_model = baseline.get("models", {}).get(model)
        if base_model is None:
            warnings.append(f"no baseline entry for model {model!r} — nothing to compare")
            continue
        for group, result in by_group.items():
            base_entry = base_model.get(group)
            if base_entry is None:
                warnings.append(f"no baseline entry for {model!r}/{group!r} — nothing to compare")
                continue
            if base_entry["n"] != result["n"]:
                warnings.append(
                    f"{model!r}/{group!r}: baseline n={base_entry['n']} but current "
                    f"n={result['n']} — applicable-prompt count changed, compare skipped"
                )
                continue
            drop = base_entry["top1_hits"] - result["top1_hits"]
            if drop > epsilon:
                warnings.append(
                    f"{model!r}/{group!r}: top1_hits dropped by {drop} "
                    f"(baseline {base_entry['top1_hits']}/{base_entry['n']} -> "
                    f"current {result['top1_hits']}/{result['n']}), more than epsilon={epsilon}"
                )
    return warnings


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_summary(report: dict) -> None:
    for model, by_group in report["results"].items():
        print(f"model={model}")
        for group, result in by_group.items():
            bm25 = result["bm25"]
            print(f"  --tools {group}: n={result['n']}  "
                  f"model top1={result['top1']} ({result['top1_hits']}/{result['n']})  "
                  f"top3={result['top3']} ({result['top3_hits']}/{result['n']})  "
                  f"[top1_or_list_top3={result['top1_or_list_top3']} "
                  f"({result['top1_or_list_top3_hits']}/{result['n']})]   |   "
                  f"BM25 top1={bm25['top1']} ({bm25['top1_hits']}/{bm25['n']})  "
                  f"top3={bm25['top3']} ({bm25['top3_hits']}/{bm25['n']})")
            print(f"    top1_source={result['top1_source']}  "
                  f"disagreements_with_bm25={result['disagreements_with_bm25_count']}  "
                  f"usage={result['usage']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS),
                         help=f"comma-separated model ids (default: {','.join(DEFAULT_MODELS)})")
    parser.add_argument("--groups", default=",".join(tse.TOOL_GROUPS),
                         help=f"comma-separated tool groups, from {tse.TOOL_GROUPS} "
                              f"(default: all three)")
    parser.add_argument("--data", type=Path, default=tse.DEFAULT_PROMPTS,
                         help=f"labeled prompt file (default: {tse.DEFAULT_PROMPTS})")
    parser.add_argument("--base-url", default=None,
                         help=f"override {ENV_BASE_URL} (falls back to that env var; "
                              "never put a private/tailnet address in a tracked file)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--no-cache", action="store_true", help="ignore and overwrite any cached calls")
    parser.add_argument("--json", type=Path, metavar="PATH", help="write the full JSON report to PATH")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_LLM_BASELINE,
                         help=f"advisory regression compare (default: {DEFAULT_LLM_BASELINE})")
    parser.add_argument("--no-baseline", action="store_true", help="skip the baseline compare entirely")
    parser.add_argument("--epsilon", type=int, default=DEFAULT_EPSILON_HITS,
                         help=f"regression tolerance in hit count (default: {DEFAULT_EPSILON_HITS})")
    parser.add_argument("--strict", action="store_true",
                         help="exit 1 on a baseline regression instead of a warning")
    args = parser.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    if not models:
        print("::error::--models resolved to an empty list")
        return 1
    bad_groups = sorted(set(groups) - set(tse.TOOL_GROUPS))
    if bad_groups:
        print(f"::error::unknown --groups value(s) {bad_groups}; choices are {tse.TOOL_GROUPS}")
        return 1

    base_url = args.base_url or os.environ.get(ENV_BASE_URL)
    api_key = os.environ.get(ENV_API_KEY)
    if not base_url or not api_key:
        print(f"::error::{ENV_BASE_URL} and {ENV_API_KEY} must both be set in the environment "
              "(or --base-url passed for the URL half) — refusing to make any live model call. "
              "This script stays committable and its own test suite runs offline regardless.")
        return 2

    prompts = tse.load_prompts(args.data)
    violations = tse.validate_prompts(prompts)
    if violations:
        print(f"::error::{len(violations)} prompt(s) violate the name-token rule — see "
              f"tool_select_eval.py's validate_prompts()")
        return 1
    prompt_set_sha256 = tse.prompts_content_hash(prompts)

    schemas_by_group = {group: load_tool_schemas(group) for group in groups}
    applicable_by_group = {
        group: [e for e in prompts if any(t in schemas_by_group[group] for t in e["expected"])]
        for group in groups
    }
    bm25_by_group = {
        group: tse.evaluate(schemas_by_group[group], prompts) for group in groups
    }

    tools_hash_by_group = {
        group: tools_payload_hash(build_tools_payload(schemas_by_group[group])) for group in groups
    }

    futures: dict[cf.Future, tuple[str, str, dict]] = {}
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        for model in models:
            for group in groups:
                schemas = schemas_by_group[group]
                tools_payload = build_tools_payload(schemas)
                catalog_text = build_catalog_text(schemas)
                for entry in applicable_by_group[group]:
                    fut = executor.submit(
                        evaluate_prompt, base_url, api_key, model, group, entry["prompt"],
                        tools_payload, tools_hash_by_group[group], catalog_text, schemas,
                        args.timeout, args.max_retries, args.cache_dir, not args.no_cache,
                    )
                    futures[fut] = (model, group, entry)

        collected: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
        cache_hits: Counter[tuple[str, str]] = Counter()
        total = len(futures)
        for done, fut in enumerate(cf.as_completed(futures), 1):
            model, group, entry = futures[fut]
            record, from_cache = fut.result()
            collected.setdefault((model, group), []).append((entry, record))
            if from_cache:
                cache_hits[(model, group)] += 1
            if done % 25 == 0 or done == total:
                print(f"  ... {done}/{total} calls complete", file=sys.stderr)

    # A group/model that got ZERO cache hits despite an existing baseline
    # entry for it is a strong signal the tool catalog changed since that
    # baseline was measured (tools_payload_hash folds the description +
    # inputSchema into the cache key precisely so this is detectable rather
    # than silently replaying — or, here, silently NOT replaying — a stale
    # response). A cleared cache directory produces the identical signal;
    # both are worth a human noticing, so this does not try to tell them
    # apart.
    baseline_for_warning = None
    if args.baseline.is_file():
        try:
            baseline_for_warning = json.loads(args.baseline.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            baseline_for_warning = None
    if baseline_for_warning is not None:
        for model in models:
            for group in groups:
                n_applicable = len(applicable_by_group[group])
                if n_applicable == 0 or cache_hits[(model, group)] > 0:
                    continue
                if baseline_for_warning.get("models", {}).get(model, {}).get(group):
                    print(
                        f"::warning::zero cache hits for {model!r}/{group!r} "
                        f"(0/{n_applicable}) even though a baseline entry exists for it — "
                        f"every prompt required a fresh call. Likely cause: the tool "
                        f"descriptions/inputSchema changed since that baseline was measured "
                        f"(cache_key folds in a hash of the exact tools payload), or the "
                        f"cache directory was cleared."
                    )

    results: dict[str, dict[str, dict]] = {}
    for model in models:
        results[model] = {}
        for group in groups:
            prompt_records = collected.get((model, group), [])
            results[model][group] = aggregate(schemas_by_group[group], prompt_records, bm25_by_group[group])

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "models": models,
        "groups": groups,
        "prompt_set_sha256": prompt_set_sha256,
        "prompt_count": len(prompts),
        "results": results,
    }

    _print_summary(report)

    if args.json is not None:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote report to {args.json}")

    exit_code = 0
    if not args.no_baseline:
        warnings = compare_baseline(report, args.baseline, args.epsilon)
        if warnings:
            for w in warnings:
                print(f"::warning::{w}")
            if args.strict:
                print("::error::--strict set; the warning(s) above are treated as failures")
                exit_code = 1
        else:
            print("ok   no regression against the LLM baseline (advisory)")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
