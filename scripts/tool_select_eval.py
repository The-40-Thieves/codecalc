#!/usr/bin/env python3
"""Offline tool-SELECTION eval: does a lexical ranker pick the right codecalc
tool for a plain-language ask, scored against the CURRENT tool schemas
(names + descriptions) exactly as an MCP client sees them?

WHY THIS EXISTS. `codecalc/server.py` carries 52 hand-written tool
descriptions, and a future PR will trim them to cut `tools/list` token cost
(README: "Tool-definition token cost"). Nothing today would notice a trim
that also deletes the vocabulary a model actually leans on to pick between
`calc_stats` and `percentiles`, or between `execute_code` and `run_submit`.
This is that gate: a labeled set of prompts, a deterministic lexical
selector, and a baseline to diff a future description change against.

HONEST EXPECTATIONS — read this before trusting a green run. The selector
below is BM25, a lexical/statistical ranker, not a language model. It can
tell you:
  - whether a description still carries the discriminating VOCABULARY that
    separates one tool from its siblings (its entire reason for existing);
  - whether a proposed trim measurably weakens that, via `--baseline`.
It CANNOT tell you:
  - whether an actual LLM tool-selector would pick correctly — a real model
    reasons over meaning, synonymy and world knowledge no term-frequency
    ranker has access to;
  - whether a description that reads clearly to a human but shares little
    surface vocabulary with its own likely prompts would still work for an
    LLM (this eval would score that badly even though a model might not).
A pass here is necessary evidence for "this trim did not throw away
discriminating vocabulary." It is not sufficient evidence that model-level
tool selection is unaffected. Treat every number below as a lexical PROXY.

DESIGN, mirroring scripts/skill_detect.py and scripts/fuzz.py: a standalone
side-car under `scripts/`, imported by its test via a `sys.path` insert (see
tests/test_tool_select_eval.py), never imported by `codecalc/` itself and
never on the server's own call path. It reads `codecalc.server`'s live tool
registry (never hand-copies a description) but adds no socket-capable
import to the package — `codecalc/server.py`'s own import graph is what
gets read here, not extended.

SELECTOR. Okapi BM25 over the tokenized `"<name> <description>"` text of
every active tool (that string is exactly what an MCP `tools/list` response
carries — a real client sees the name too, so scoring on both is the
faithful proxy). Implemented in ~50 lines below, no dependency beyond the
stdlib.

NOT GAMEABLE BY NAME. A prompt like "call evaluate_expression" would score
top-1 against ANY description, including a one-word stub, because the name
token alone would dominate — that would make this eval blind to exactly the
description damage it exists to catch. `validate_prompts()` below rejects
any labeled prompt containing its own expected tool's exact name, or every
one of that name's underscore-split tokens as whole words (singular/plural
normalized both directions — "list all unit aliases" is rejected for
`list_units` exactly as "list all unit alias" would be). See "checked-in
DATA" for how this is enforced on every run.

CHECKED-IN DATA. `scripts/data/tool_select_prompts.jsonl` — one JSON object
per line: `{"prompt": str, "expected": [tool_name, ...]}`, `expected[0]` is
the primary answer, any later entries are additional acceptable tools for a
genuinely ambiguous ask. `scripts/data/tool_select_baseline.json` is the v1
baseline this file's own `--baseline` mode compares against, keyed by
`--tools` group (`full`/`dev`/`core`) plus a top-level `prompt_set_sha256`
PINNING the exact labeled corpus (see `prompts_content_hash`) — regenerate
the whole file with `--tools <group> --json` after an intentional
description OR corpus change, never to paper over a regression. A baseline
compare against a corpus that no longer hashes to `prompt_set_sha256` fails
loudly with a distinct "corpus changed" error rather than silently scoring
a different, possibly easier, prompt set against the old numbers.

USAGE
    python scripts/tool_select_eval.py                        # --tools full, human report
    python scripts/tool_select_eval.py --tools dev             # score only the 'dev' preset's tools
    python scripts/tool_select_eval.py --json                  # machine-readable report
    python scripts/tool_select_eval.py --baseline scripts/data/tool_select_baseline.json
                                                                 # regression-gate: exit 1 on a drop > epsilon
    python scripts/tool_select_eval.py --self-check             # positive control (ablation) — see below

Every one of `full`/`dev`/`core` needs its OWN `--self-check` run and its
OWN `--baseline` compare — a tool can already be top-1-WRONG against the
`full` surface's 51 other distractors (zero headroom to lose) while still
having real, ablation-detectable headroom against `core`'s much smaller
distractor set. `tests/test_tool_select_eval.py` runs both for all three.

SELF-CHECK (the positive control this gate needs to be trusted at all, per
CONTRIBUTING.md's "a gate is not trusted until it has been watched
failing"). `--self-check` runs a FULL one-at-a-time ablation SWEEP: every
candidate tool, in turn, has ONLY its own description replaced with a
generic stub ("Runs a computation.") — every sibling keeps its real
description as a live confusor — and the eval reruns on just that tool's
own prompts. No sampling: a fixed-size random sample is exactly what let
three real ablations (`limit_expression`, `analyze_complexity`,
`extract_function`) go completely unexercised by this gate in v1 — a fixed
seed=0 draw of 10 tools out of 52 never once selected any of the three.
Reported as POOLED INTEGER hit counts, not a float percentage: a per-tool
percentage threshold is meaningless for a tool with only 3 prompts, and a
tool BM25 already gets wrong with its REAL description has zero headroom to
show a drop no matter how sensitive the eval is — pooling across the whole
sweep is what stays a meaningful signal despite both effects. If the pooled
loss (or the count of tools showing any loss at all) does not clear its
documented floor, this exits non-zero rather than reporting a green, inert
gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PROMPTS = REPO / "scripts" / "data" / "tool_select_prompts.jsonl"
DEFAULT_BASELINE = REPO / "scripts" / "data" / "tool_select_baseline.json"

#: `CODECALC_TOOLS` presets this eval mirrors 1:1 — see codecalc/server.py's
#: own `PRESETS`. Kept as a literal tuple, not re-derived from server.py,
#: because the whole point of `--tools` is to select a value FOR that
#: module's own env var from a separate process (see `load_tool_schemas`);
#: re-importing server.py here to read its own PRESETS would still leave
#: this list needing to name them for argparse `choices=`.
TOOL_GROUPS = ("full", "dev", "core")

#: What a description-damage ablation replaces a real description with.
#: Deliberately information-free: no tool-specific noun survives, so any
#: residual top-1 correctness after ablation is coming from the tool NAME
#: (which every prompt is validated to avoid) or from chance, not from a
#: description leaking through.
ABLATION_STUB = "Runs a computation."

#: `--self-check` floors, set well below the TIGHTEST of the three measured
#: v1 sweeps (`--tools core`: pooled_lost=51/63 baseline hits,
#: tools_showing_loss=23/24 with headroom; `dev` and `full` both measure
#: comfortably higher — see tests/test_tool_select_eval.py for the exact
#: numbers on every preset). Both must clear, not either: a high pooled-loss
#: count from just one or two very-confusable tools would still read as
#: "inert" for everything else if `tools_showing_loss` were not checked too.
SELF_CHECK_MIN_POOLED_LOST = 30
SELF_CHECK_MIN_TOOLS_SHOWING_LOSS = 15

#: `--baseline` regression gate: fail if top-1 HIT COUNT (an exact integer
#: over the pinned corpus, never a float ratio — see `prompts_content_hash`
#: and the module docstring) drops by more than this many prompts relative
#: to the checked-in baseline. 1 prompt (~0.5pp on the 196-prompt `full`
#: corpus) — tight enough to catch a real regression, loose enough that
#: BM25's own alphabetical tie-break cannot flip it by chance. An integer,
#: not a float: `--epsilon nan` is a `ValueError` at argument-parsing time
#: (argparse's own `int("nan")`), not a comparison that fails open.
DEFAULT_EPSILON_HITS = 1


# ── tokenizer ─────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Small, deliberately generic — removing these helps BM25's IDF do its job
#: over a 52-document corpus without needing a bigger stopword list than
#: that corpus can justify. Not a claim of completeness.
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "to", "in", "is", "are", "for", "on", "and",
    "or", "with", "this", "that", "it", "its", "be", "as", "by", "from",
    "at", "into", "using", "any", "each", "per", "not", "than", "then",
    "what", "which", "you", "your", "my", "me", "i", "can", "does",
})


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, stopwords and single characters dropped.

    Whole-word only — `"int"` never matches inside `"integer"`. Used for
    BOTH BM25 scoring and (via `_name_tokens`/`_normalize_plural` below)
    the name-leak validator; the validator additionally normalizes plurals
    on top of this, but never the other way around — BM25 itself stays
    un-stemmed, since stemming the SCORER would change accuracy, not just
    the leak check.
    """
    return [t for t in _TOKEN_RE.findall(text.lower())
            if t not in _STOPWORDS and len(t) > 1]


# ── BM25 (Okapi), ~50 lines, no dependency ──────────────────────────────────

class BM25:
    """Standard Okapi BM25 over a fixed set of named documents.

    `k1`/`b` are the textbook defaults (Robertson/Sparck Jones). Ties in
    `rank()` break alphabetically on tool name so a run is byte-for-byte
    reproducible — real text ties are rare, and a run that changes without a
    real code change would defeat `--baseline` entirely.
    """

    def __init__(self, docs: dict[str, str], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_ids = sorted(docs)
        self.doc_tokens = {did: tokenize(docs[did]) for did in self.doc_ids}
        self.doc_len = {did: len(toks) for did, toks in self.doc_tokens.items()}
        self.avgdl = (sum(self.doc_len.values()) / len(self.doc_ids)) if self.doc_ids else 0.0
        self.tf = {did: Counter(toks) for did, toks in self.doc_tokens.items()}
        df: Counter = Counter()
        for toks in self.doc_tokens.values():
            df.update(set(toks))
        n = len(self.doc_ids)
        # +1 inside the log keeps idf non-negative for a term in every doc —
        # the classic Robertson-Sparck-Jones-with-floor variant, so a term
        # common to the whole small corpus (e.g. "codecalc") cannot get a
        # NEGATIVE weight and start penalizing the documents it appears in.
        self.idf = {term: math.log(1 + (n - freq + 0.5) / (freq + 0.5))
                    for term, freq in df.items()}

    def _score_doc(self, did: str, query_tokens: list[str]) -> float:
        score = 0.0
        dl = self.doc_len[did]
        tf = self.tf[did]
        for term in query_tokens:
            idf = self.idf.get(term)
            if idf is None:
                continue
            f = tf.get(term, 0)
            if f == 0:
                continue
            denom = f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            score += idf * (f * (self.k1 + 1)) / denom
        return score

    def rank(self, query: str) -> list[tuple[str, float]]:
        """[(doc_id, score), ...] best first, ties broken alphabetically."""
        q = tokenize(query)
        scored = [(did, self._score_doc(did, q)) for did in self.doc_ids]
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return scored


# ── live tool registry (never hand-copied) ──────────────────────────────────

#: Runs in a FRESH subprocess, same pattern as tests/test_tool_groups.py and
#: scripts/check_tool_groups.py: `codecalc.server` decides its registered
#: surface once, at import, from `CODECALC_TOOLS` — re-importing an already-
#: imported module in this process just returns the first import's cached
#: result. `TOOL_GROUPS` (the codecalc.server dict, name -> group) is dumped
#: too, only reported, never trusted over `_active_groups()`'s own filtering.
_PROBE = (
    "import json\n"
    "from codecalc import server\n"
    "tools = server.mcp._tool_manager._tools\n"
    "print(json.dumps({\n"
    "    name: {'description': t.description, 'group': server.TOOL_GROUPS.get(name)}\n"
    "    for name, t in tools.items()\n"
    "}))\n"
)


def load_tool_schemas(tools_group: str) -> dict[str, dict]:
    """`{tool_name: {"description": str, "group": str}}` for every tool
    `CODECALC_TOOLS=<tools_group>` registers, read from a live import of
    `codecalc.server` — the same names/descriptions/groups an MCP client
    connecting to that configuration would see over `tools/list`.
    """
    env = dict(os.environ)
    env["CODECALC_TOOLS"] = tools_group
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"loading tool schemas for --tools {tools_group!r} failed "
            f"(rc={proc.returncode}): {proc.stderr[-2000:]}"
        )
    return json.loads(proc.stdout)


def _doc_text(name: str, description: str) -> str:
    return f"{name} {description}"


# ── labeled prompt set ───────────────────────────────────────────────────────

def load_prompts(path: Path = DEFAULT_PROMPTS) -> list[dict]:
    """One `{"prompt": str, "expected": [tool_name, ...]}` per line."""
    prompts = []
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if "prompt" not in entry or "expected" not in entry:
                raise ValueError(f"{path}:{lineno}: entry missing 'prompt' or 'expected'")
            if not entry["expected"]:
                raise ValueError(f"{path}:{lineno}: 'expected' is empty")
            prompts.append(entry)
    return prompts


def prompts_content_hash(prompts: list[dict]) -> str:
    """SHA-256 over the exact ORDERED `(prompt, expected)` sequence.

    Pins the labeled corpus so `--baseline` compare cannot be satisfied by a
    smaller or reordered file that still clears the ">= 3 prompts per tool"
    floor: deleting every 4th prompt from the v1 196-prompt set leaves 156
    (still >= 3/tool) and RAISES measured top-1 accuracy 0.602 -> 0.6346 —
    i.e. the easiest way to make a shrunken, description-blind corpus look
    like an improvement. A per-tool count floor alone cannot catch that;
    this hash can, because it changes on ANY edit — an add, a delete, a
    reorder, or a single character.

    `json.dumps(..., sort_keys=True)` per line (not `sort_keys` across the
    whole list — the list's OWN order is part of what is pinned, since a
    reorder is exactly the kind of "same set, different corpus" edit this
    exists to catch) gives a canonical per-entry serialization independent
    of how the dict happened to be constructed in memory.
    """
    canon = "\n".join(
        json.dumps({"prompt": e["prompt"], "expected": e["expected"]}, sort_keys=True)
        for e in prompts
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _plural_variants(tok: str) -> frozenset[str]:
    """Every plausible singular/plural spelling of `tok`, ADDITIVE rather
    than a single lossy stem — used ONLY by the name-leak validator below,
    never by `tokenize()`/BM25 scoring (which stays un-stemmed on purpose:
    stemming the SCORER would change measured accuracy, not just what
    counts as a leaked name).

    A blind "strip a trailing s" stemmer was tried first and is WRONG: it
    turns "status" (already singular) into a mangled 4-letter fragment, and separately turns
    "runtimes" into "runtim" via an over-eager "-es" rule meant for
    "statuses" -> "status" — two different bugs that between them made
    `runtimes_status`'s own two name tokens fail to match EITHER form of
    themselves. Generating variants ADDITIVELY (never stripping the
    original token itself) avoids that class of bug entirely: "status"
    always stays a valid member of its own variant set, so a comparison
    against it can only ever gain matches, never lose the exact form.

    Two tokens are considered the same word if `_plural_variants(a) &
    _plural_variants(b)` is non-empty — see `_token_matches`.
    """
    v = {tok}
    v.add(tok + "s")                                    # unit -> units
    if tok.endswith(("s", "x", "z", "ch", "sh")):
        v.add(tok + "es")                                # status -> statuses
    # Strip a REGULAR trailing -s (units -> unit, runtimes -> runtime).
    # Guarded so a word that merely ENDS in -s in its base form (status,
    # class, bus) is never stripped to a shorter, wrong form — it keeps
    # matching only via the ADDITIVE rules above instead.
    if (len(tok) > 3 and tok.endswith("s")
            and not tok.endswith(("ss", "us", "xs", "zs"))):
        v.add(tok[:-1])                                  # units -> unit
    # Strip a sibilant -es (statuses -> status, aliases -> alias).
    if len(tok) > 5 and tok.endswith(("ses", "xes", "zes", "ches", "shes")):
        v.add(tok[:-2])                                  # statuses -> status
    return frozenset(v)


def _token_matches(name_tok: str, prompt_tok: str) -> bool:
    return bool(_plural_variants(name_tok) & _plural_variants(prompt_tok))


def _name_tokens(tool_name: str) -> list[str]:
    """Underscore-split tokens of a tool name, in order."""
    return tool_name.split("_")


def validate_prompts(prompts: list[dict]) -> list[str]:
    """Every violation of the "not gameable by tool names" rule (see module
    docstring): a prompt containing its own expected tool's exact name, or
    every one of that name's underscore-split tokens as whole words
    (singular/plural matched both directions via `_token_matches`).

    Checked against EVERY name in `expected`, not just the primary — an
    acceptable-alternate tool being named in the prompt text is exactly as
    game-able as the primary one being named.
    """
    violations = []
    for i, entry in enumerate(prompts):
        prompt_lower = entry["prompt"].lower()
        prompt_tokens = tokenize(entry["prompt"])
        for tool_name in entry["expected"]:
            if tool_name.lower() in prompt_lower:
                violations.append(
                    f"prompt #{i} contains the literal tool name {tool_name!r}: "
                    f"{entry['prompt']!r}"
                )
                continue
            name_toks = _name_tokens(tool_name)
            if all(any(_token_matches(nt, pt) for pt in prompt_tokens) for nt in name_toks):
                violations.append(
                    f"prompt #{i} contains every underscore-token of "
                    f"{tool_name!r} ({name_toks}, singular/plural matched): "
                    f"{entry['prompt']!r}"
                )
    return violations


# ── evaluation ───────────────────────────────────────────────────────────────

def evaluate(schemas: dict[str, dict], prompts: list[dict]) -> dict:
    """Run every applicable prompt through BM25 over `schemas`.

    A prompt is APPLICABLE to this `schemas` set if at least one of its
    `expected` tools is present — e.g. a session_* prompt is skipped
    entirely against `--tools core`, where no session tool is even a
    candidate. Skipped prompts are counted, never silently dropped from the
    report.

    Returns both the float `top1`/`top3` ratios (for human/JSON reporting)
    and the exact integer `top1_hits`/`top3_hits` counts — `--baseline`
    compares the integers (see `DEFAULT_EPSILON_HITS`'s docstring for why a
    float ratio comparison is the wrong tool for a regression gate).
    """
    docs = {name: _doc_text(name, info["description"]) for name, info in schemas.items()}
    bm25 = BM25(docs)

    applicable = [e for e in prompts if any(t in schemas for t in e["expected"])]
    skipped = len(prompts) - len(applicable)

    per_tool: dict[str, dict] = {}
    failures = []
    top1_hits = 0
    top3_hits = 0

    for entry in applicable:
        expected = set(entry["expected"]) & set(schemas)
        ranked = bm25.rank(entry["prompt"])
        top1 = ranked[0][0] if ranked else None
        top3 = {name for name, _ in ranked[:3]}
        primary = entry["expected"][0]
        bucket = per_tool.setdefault(primary, {"n": 0, "top1": 0, "top3": 0})
        bucket["n"] += 1

        hit1 = top1 in expected
        hit3 = bool(top3 & expected)
        if hit1:
            top1_hits += 1
            bucket["top1"] += 1
        if hit3:
            top3_hits += 1
            bucket["top3"] += 1
        if not hit1:
            failures.append({
                "prompt": entry["prompt"],
                "expected": sorted(expected),
                "got": top1,
                "top3": sorted(top3),
            })

    n = len(applicable)
    per_tool_report = {
        name: {
            "n": b["n"],
            "top1": round(b["top1"] / b["n"], 4) if b["n"] else None,
            "top3": round(b["top3"] / b["n"], 4) if b["n"] else None,
        }
        for name, b in sorted(per_tool.items())
    }

    return {
        "n": n,
        "skipped": skipped,
        "top1_hits": top1_hits,
        "top3_hits": top3_hits,
        "top1": round(top1_hits / n, 4) if n else None,
        "top3": round(top3_hits / n, 4) if n else None,
        "per_tool": per_tool_report,
        "failures": failures,
    }


def run_self_check(tools_group: str, prompts: list[dict], schemas: dict[str, dict]) -> dict:
    """Positive control: a full one-at-a-time ablation SWEEP over every
    candidate tool — see the module docstring's "SELF-CHECK" for why this
    replaced a fixed random sample. Returns pooled integer hit-loss counts
    plus a per-tool breakdown; `inert` is True (and the CLI exits non-zero)
    if either floor (`SELF_CHECK_MIN_POOLED_LOST`,
    `SELF_CHECK_MIN_TOOLS_SHOWING_LOSS`) is not cleared.
    """
    candidates = sorted({e["expected"][0] for e in prompts if e["expected"][0] in schemas})
    if not candidates:
        raise ValueError(
            f"no candidate tool has a primary-labeled prompt against "
            f"--tools {tools_group!r} — nothing to sweep"
        )

    docs = {name: _doc_text(name, info["description"]) for name, info in schemas.items()}
    bm25_real = BM25(docs)

    per_tool: dict[str, dict] = {}
    pooled_baseline_hits = 0
    pooled_ablated_hits = 0
    pooled_lost = 0
    tools_with_headroom = 0
    tools_showing_loss = 0

    for name in candidates:
        subset = [e for e in prompts
                  if e["expected"][0] == name and any(t in schemas for t in e["expected"])]
        if not subset:
            continue

        base_hits = sum(
            1 for e in subset
            if bm25_real.rank(e["prompt"])[0][0] in (set(e["expected"]) & set(schemas))
        )

        ablated_docs = dict(docs)
        ablated_docs[name] = _doc_text(name, ABLATION_STUB)
        bm25_ablated = BM25(ablated_docs)
        ablated_hits = sum(
            1 for e in subset
            if bm25_ablated.rank(e["prompt"])[0][0] in (set(e["expected"]) & set(schemas))
        )

        lost = base_hits - ablated_hits
        per_tool[name] = {
            "n": len(subset), "baseline_hits": base_hits,
            "ablated_hits": ablated_hits, "lost": lost,
        }
        pooled_baseline_hits += base_hits
        pooled_ablated_hits += ablated_hits
        # A rare INCREASE (ablation accidentally helps a tool win a tie it
        # was previously losing) never offsets a real loss found elsewhere
        # in the sweep — clamped so one lucky tool cannot mask ten damaged
        # ones in the pooled total.
        pooled_lost += max(lost, 0)
        if base_hits > 0:
            tools_with_headroom += 1
            if lost > 0:
                tools_showing_loss += 1

    inert = (pooled_lost < SELF_CHECK_MIN_POOLED_LOST
             or tools_showing_loss < SELF_CHECK_MIN_TOOLS_SHOWING_LOSS)

    return {
        "tools_group": tools_group,
        "n_candidates": len(per_tool),
        "pooled_baseline_hits": pooled_baseline_hits,
        "pooled_ablated_hits": pooled_ablated_hits,
        "pooled_lost": pooled_lost,
        "tools_with_headroom": tools_with_headroom,
        "tools_showing_loss": tools_showing_loss,
        "min_pooled_lost": SELF_CHECK_MIN_POOLED_LOST,
        "min_tools_showing_loss": SELF_CHECK_MIN_TOOLS_SHOWING_LOSS,
        "per_tool": per_tool,
        "inert": inert,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_report(tools_group: str, result: dict) -> None:
    print(f"tool-select-eval  --tools {tools_group}")
    print(f"  n={result['n']} (skipped={result['skipped']})  "
          f"top1={result['top1']} ({result['top1_hits']}/{result['n']})  "
          f"top3={result['top3']} ({result['top3_hits']}/{result['n']})")
    if result["failures"]:
        print(f"  {len(result['failures'])} top-1 failure(s):")
        for f in result["failures"]:
            print(f"    prompt={f['prompt']!r}")
            print(f"      expected={f['expected']} got={f['got']!r} top3={f['top3']}")
    weak = sorted(
        ((name, b["top1"]) for name, b in result["per_tool"].items() if b["top1"] is not None),
        key=lambda kv: kv[1],
    )[:10]
    if weak:
        print("  weakest tools (top1):")
        for name, acc in weak:
            print(f"    {acc:.2f}  {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tools", choices=TOOL_GROUPS, default="full",
                         help="tool surface to score against, mirrors CODECALC_TOOLS "
                              "(default: full)")
    parser.add_argument("--data", type=Path, default=DEFAULT_PROMPTS,
                         help=f"labeled prompt file (default: {DEFAULT_PROMPTS})")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--baseline", type=Path,
                         help="compare top-1 hit count against this baseline JSON; "
                              "exit 1 on a drop > --epsilon prompts, or on a corpus-hash "
                              "mismatch")
    parser.add_argument("--epsilon", type=int, default=DEFAULT_EPSILON_HITS,
                         help="regression tolerance for --baseline, as an integer number "
                              f"of prompts (default: {DEFAULT_EPSILON_HITS}). Must be >= 0; "
                              "an int type means a non-numeric or NaN value fails at "
                              "argument-parsing, never silently at comparison time.")
    parser.add_argument("--self-check", action="store_true",
                         help="run the full ablation-sweep positive control instead of a "
                              "normal eval")
    args = parser.parse_args(argv)

    prompts = load_prompts(args.data)
    violations = validate_prompts(prompts)
    if violations:
        print(f"::error::{len(violations)} prompt(s) violate the name-token rule:")
        for v in violations[:20]:
            print(f"  {v}")
        return 1

    schemas = load_tool_schemas(args.tools)

    if args.self_check:
        result = run_self_check(args.tools, prompts, schemas)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"self-check (full sweep)  --tools {args.tools}  "
                  f"n_candidates={result['n_candidates']}")
            print(f"  pooled: baseline_hits={result['pooled_baseline_hits']} "
                  f"ablated_hits={result['pooled_ablated_hits']} "
                  f"lost={result['pooled_lost']} "
                  f"(floor: {result['min_pooled_lost']})")
            print(f"  tools_with_headroom={result['tools_with_headroom']}  "
                  f"tools_showing_loss={result['tools_showing_loss']} "
                  f"(floor: {result['min_tools_showing_loss']})")
        if result["inert"]:
            print(f"::error::self-check FAILED for --tools {args.tools}: pooled_lost="
                  f"{result['pooled_lost']} (need >= {result['min_pooled_lost']}) / "
                  f"tools_showing_loss={result['tools_showing_loss']} (need >= "
                  f"{result['min_tools_showing_loss']}). This eval is INERT for this tool "
                  f"surface — it cannot be trusted to detect real description damage.")
            return 1
        print(f"ok   self-check (--tools {args.tools}): ablation sweep lost "
              f"{result['pooled_lost']} hit(s) across {result['tools_showing_loss']} tool(s) "
              f"— the gate is live")
        return 0

    result = evaluate(schemas, prompts)

    if args.baseline is not None:
        if args.epsilon < 0:
            print(f"::error::--epsilon must be >= 0 (got {args.epsilon})")
            return 1
        base = json.loads(args.baseline.read_text(encoding="utf-8"))
        current_hash = prompts_content_hash(prompts)
        base_hash = base.get("prompt_set_sha256")
        if not base_hash:
            print(f"::error::baseline {args.baseline} has no 'prompt_set_sha256' — it "
                  f"predates corpus pinning and cannot be trusted for a compare")
            return 1
        if current_hash != base_hash:
            print(
                f"::error::CORPUS CHANGED — re-baseline explicitly. {args.data} hashes to "
                f"{current_hash[:16]}… but {args.baseline} was generated from a corpus "
                f"hashing to {base_hash[:16]}…. Accuracy is not comparable across two "
                f"different prompt sets — regenerate the baseline (--tools <group> --json) "
                f"once the corpus change is reviewed and intentional; a hash mismatch is "
                f"never something to pass through."
            )
            return 1
        base_entry = base.get(args.tools)
        if base_entry is None or "top1_hits" not in base_entry or "n" not in base_entry:
            print(f"::error::baseline {args.baseline} has no usable entry for "
                  f"--tools {args.tools!r} (need 'n' and 'top1_hits')")
            return 1
        if base_entry["n"] != result["n"]:
            print(f"::error::baseline n={base_entry['n']} but current run n={result['n']} "
                  f"for --tools {args.tools!r} — the corpus hash matched but the applicable "
                  f"prompt count for this preset did not; this should be impossible and is a "
                  f"bug, not a pass")
            return 1
        base_hits = base_entry["top1_hits"]
        drop_hits = base_hits - result["top1_hits"]
        if args.json:
            print(json.dumps({**result, "baseline_top1_hits": base_hits,
                               "drop_hits": drop_hits, "epsilon": args.epsilon}, indent=2))
        else:
            _print_report(args.tools, result)
            print(f"  baseline top1_hits={base_hits}/{base_entry['n']}  "
                  f"drop_hits={drop_hits}  epsilon={args.epsilon}")
        if drop_hits > args.epsilon:
            print(f"::error::top-1 hit count dropped by {drop_hits} prompt(s), more than "
                  f"epsilon={args.epsilon}, against baseline {args.baseline}")
            return 1
        print("ok   no regression against baseline")
        return 0

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_report(args.tools, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
