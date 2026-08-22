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
one of that name's underscore-split tokens as whole words. See "checked-in
DATA" for how this is enforced on every run.

CHECKED-IN DATA. `scripts/data/tool_select_prompts.jsonl` — one JSON object
per line: `{"prompt": str, "expected": [tool_name, ...]}`, `expected[0]` is
the primary answer, any later entries are additional acceptable tools for a
genuinely ambiguous ask. `scripts/data/tool_select_baseline.json` is the
v1 baseline this file's own `--baseline` mode compares against — regenerate
it with `--tools <group> --json > ...` after an intentional description
change, never to paper over a regression.

USAGE
    python scripts/tool_select_eval.py                        # --tools full, human report
    python scripts/tool_select_eval.py --tools dev             # score only the 'dev' preset's tools
    python scripts/tool_select_eval.py --json                  # machine-readable report
    python scripts/tool_select_eval.py --baseline scripts/data/tool_select_baseline.json
                                                                 # regression-gate: exit 1 on a drop > epsilon
    python scripts/tool_select_eval.py --self-check             # positive control (ablation) — see below

SELF-CHECK (the positive control this gate needs to be trusted at all, per
CONTRIBUTING.md's "a gate is not trusted until it has been watched
failing"). `--self-check` samples >= 10 tools, replaces THEIR descriptions
in-memory with a generic stub ("Runs a computation."), reruns the eval on
just their prompts, and asserts top-1 accuracy drops by at least
`SELF_CHECK_MIN_DELTA`. If it does not, the eval cannot be detecting
description damage at all, and this exits non-zero rather than reporting a
green, inert gate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
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

#: `--self-check` must show at least this much of a top-1 accuracy drop on
#: the ablated tools' own prompts, or the gate is reported inert. 0.15 (15
#: points) is well below what ablating ~half the corpus's tools actually
#: measures (see the module test for the real number) and well above noise
#: from the handful of prompts a 10-15 tool sample carries.
SELF_CHECK_MIN_DELTA = 0.15
SELF_CHECK_MIN_SAMPLE = 10

#: `--baseline` regression gate: fail if top-1 accuracy drops by more than
#: this many points relative to the checked-in baseline. 0.5pp — tight
#: enough to catch a real regression, loose enough that BM25's own tie-
#: breaking (alphabetical on an exact score tie) cannot flip it by chance.
DEFAULT_EPSILON = 0.005


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

    Whole-word only — `"int"` never matches inside `"integer"` — which is
    what makes `validate_prompts`' token-subset check meaningful rather than
    a substring trap.
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


def _name_tokens(tool_name: str) -> set[str]:
    return set(tool_name.split("_"))


def validate_prompts(prompts: list[dict]) -> list[str]:
    """Every violation of the "not gameable by tool names" rule (see module
    docstring): a prompt containing its own expected tool's exact name, or
    every one of that name's underscore-split tokens as whole words.

    Checked against EVERY name in `expected`, not just the primary — an
    acceptable-alternate tool being named in the prompt text is exactly as
    game-able as the primary one being named.
    """
    violations = []
    for i, entry in enumerate(prompts):
        prompt_lower = entry["prompt"].lower()
        prompt_tokens = set(tokenize(entry["prompt"]))
        for tool_name in entry["expected"]:
            if tool_name.lower() in prompt_lower:
                violations.append(
                    f"prompt #{i} contains the literal tool name {tool_name!r}: "
                    f"{entry['prompt']!r}"
                )
                continue
            if _name_tokens(tool_name) <= prompt_tokens:
                violations.append(
                    f"prompt #{i} contains every underscore-token of "
                    f"{tool_name!r} ({sorted(_name_tokens(tool_name))}): "
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
        "top1": round(top1_hits / n, 4) if n else None,
        "top3": round(top3_hits / n, 4) if n else None,
        "per_tool": per_tool_report,
        "failures": failures,
    }


def run_self_check(tools_group: str, prompts: list[dict], schemas: dict[str, dict],
                    sample_size: int = SELF_CHECK_MIN_SAMPLE, seed: int = 0) -> dict:
    """The positive control: ablate `sample_size` tools' descriptions to a
    generic stub, in-memory, and prove top-1 accuracy on THEIR OWN prompts
    drops by at least `SELF_CHECK_MIN_DELTA`. If it does not, this eval
    cannot be trusted to detect real description damage — see the module
    docstring's "SELF-CHECK".
    """
    candidates = sorted({e["expected"][0] for e in prompts if e["expected"][0] in schemas})
    if len(candidates) < sample_size:
        raise ValueError(
            f"only {len(candidates)} candidate tool(s) have a primary-labeled "
            f"prompt against --tools {tools_group!r}; need >= {sample_size} for "
            f"a self-check sample"
        )
    rng = random.Random(seed)  # noqa: S311 — sample selection, not cryptography
    sample = sorted(rng.sample(candidates, sample_size))

    subset = [e for e in prompts if e["expected"][0] in sample]

    baseline = evaluate(schemas, subset)

    ablated_schemas = {
        name: ({"description": ABLATION_STUB, "group": info["group"]} if name in sample else info)
        for name, info in schemas.items()
    }
    ablated = evaluate(ablated_schemas, subset)

    delta = (baseline["top1"] or 0.0) - (ablated["top1"] or 0.0)
    inert = delta < SELF_CHECK_MIN_DELTA

    return {
        "sample": sample,
        "n_prompts": len(subset),
        "baseline_top1": baseline["top1"],
        "ablated_top1": ablated["top1"],
        "delta": round(delta, 4),
        "min_required_delta": SELF_CHECK_MIN_DELTA,
        "inert": inert,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_report(tools_group: str, result: dict) -> None:
    print(f"tool-select-eval  --tools {tools_group}")
    print(f"  n={result['n']} (skipped={result['skipped']})  "
          f"top1={result['top1']}  top3={result['top3']}")
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
                         help="compare top-1 accuracy against this baseline JSON; "
                              "exit 1 on a drop > --epsilon")
    parser.add_argument("--epsilon", type=float, default=DEFAULT_EPSILON,
                         help=f"regression tolerance for --baseline, in accuracy "
                              f"points (default: {DEFAULT_EPSILON})")
    parser.add_argument("--self-check", action="store_true",
                         help="run the ablation positive control instead of a normal eval")
    parser.add_argument("--self-check-sample", type=int, default=SELF_CHECK_MIN_SAMPLE)
    parser.add_argument("--seed", type=int, default=0)
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
        result = run_self_check(args.tools, prompts, schemas,
                                 sample_size=args.self_check_sample, seed=args.seed)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"self-check  --tools {args.tools}  sample={result['sample']}")
            print(f"  n_prompts={result['n_prompts']}  "
                  f"baseline_top1={result['baseline_top1']}  "
                  f"ablated_top1={result['ablated_top1']}  delta={result['delta']}")
        if result["inert"]:
            print(f"::error::self-check FAILED: ablating {len(result['sample'])} tool(s)' "
                  f"descriptions moved top-1 accuracy by only {result['delta']}, below the "
                  f"required {result['min_required_delta']}. This eval is INERT — it cannot "
                  f"be trusted to detect real description damage.")
            return 1
        print(f"ok   self-check: ablation dropped top-1 accuracy by {result['delta']} "
              f"(>= {result['min_required_delta']} required) — the gate is live")
        return 0

    result = evaluate(schemas, prompts)

    if args.baseline is not None:
        base = json.loads(args.baseline.read_text(encoding="utf-8"))
        base_entry = base.get(args.tools)
        if base_entry is None:
            print(f"::error::baseline {args.baseline} has no entry for --tools {args.tools!r}")
            return 1
        base_top1 = base_entry["top1"]
        drop = base_top1 - (result["top1"] or 0.0)
        if args.json:
            print(json.dumps({**result, "baseline_top1": base_top1, "drop": round(drop, 4),
                               "epsilon": args.epsilon}, indent=2))
        else:
            _print_report(args.tools, result)
            print(f"  baseline top1={base_top1}  drop={round(drop, 4)}  "
                  f"epsilon={args.epsilon}")
        if drop > args.epsilon:
            print(f"::error::top-1 accuracy dropped by {round(drop, 4)}, more than "
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
