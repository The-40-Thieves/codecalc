#!/usr/bin/env python3
"""Offline, post-hoc detector for a SKILL.md-mandated tool call that never happened.

`codecalc/SKILL.md`'s "Call these — mandatory" table names situations where a
model must call a codecalc tool before stating a number, claiming equivalence,
claiming a speedup, or stating a complexity. This script reads a recorded
conversation and flags every assistant turn whose text matches one of those
situations while the turn's tool calls (and any tool results immediately
following it) never touch the tool the table requires.

It is a MEASUREMENT tool, not a runtime gate: it runs after the fact over a
saved transcript, imports nothing from `codecalc/`, and has no MCP dependency.
Nothing in the server calls this file, and nothing here can block a live
session — a skill-miss found here is a signal to improve SKILL.md or a
caller's habits, not something to enforce inline. That is also why it lives
in `scripts/`, off the shipped wheel, next to `scripts/fuzz.py` — the same
side-car shape: a standalone script imported by its test via a `sys.path`
insert (see `tests/test_fuzz_smoke.py`), not a package export.

TWO OPEN QUESTIONS, RESOLVED FOR v1:
  1. Where does this live? `scripts/skill_detect.py`, not `codecalc/`. A
     conversation-analysis tool has no runtime user — shipping it in the
     wheel would put an unused import surface in every install for a job
     nobody but a maintainer runs.
  2. What does it run against? No real conversation corpus exists yet for
     this repo (confirmed before writing this). v1 is therefore validated
     against a synthetic, hand-labeled fixture set under
     `tests/fixtures/skill_detect/` — not a claim about real-world precision
     or recall. Scoring against real logs, once any exist, is future work;
     the `score()` harness below is written to take any labeled folder, real
     or synthetic, so that work is "point it at a new folder", not a rewrite.

CONVERSATION INPUT SCHEMA. One `.jsonl` file is one conversation; one line is
one message:

    {"role": "user"|"assistant"|"tool",
     "content": "<text>",
     "tool_calls": [{"name": "<tool>"}, ...]}   # optional

`tool_calls` is how a message records that it invoked a tool — normally
present on `assistant` messages, but read wherever it appears (including on
a `tool`-role message that a specific log format attaches it to), never
assumed to exist only on one role. `content` is the text a matcher rule runs
against; only `assistant` messages are scanned for claims, since a "did the
model make a claim without calling a tool" question is about what the model
said, not what the user asked or a tool returned.

DESIGN CHOICE: conservative rules. A rule that fires on prose nobody acts on
is a rule someone disables, so every rule below is written to the
HIGH-PRECISION end on purpose, and `_looks_like_estimate` exists to suppress
the shapes SKILL.md itself says need no tool call ("order-of-magnitude
estimates you label as estimates"). A generic percentage/threshold rule is
easy to add and was deliberately left out of v1: SKILL.md's own "Do not call
these" list and this ticket's own warning both point at exactly that class
as the most false-positive-prone, and it has no fixture coverage here to
prove it clean. Add it once it can be shown conservative the same way the
rules below were.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# ── conservatism guard ───────────────────────────────────────────────────────

#: Phrases that mean "the caller already knows this is an estimate, not a
#: claim" — SKILL.md's own carve-out ("order-of-magnitude estimates you label
#: as estimates"). Checked in a window AROUND a match, not the whole message,
#: so one hedge early in a long turn cannot silently launder an unrelated
#: claim later in the same turn.
_ESTIMATE_MARKERS = (
    "~", "roughly", "approx", "about", "estimate", "order of magnitude",
    "for example", "e.g.",
)

#: Characters of context inspected on each side of a match. Wide enough to
#: catch "roughly ... 40% faster" as one hedged claim, narrow enough that a
#: hedge in one sentence cannot suppress a claim in the next.
_GUARD_WINDOW = 40


def _window(text: str, start: int, end: int, radius: int = _GUARD_WINDOW) -> str:
    return text[max(0, start - radius):min(len(text), end + radius)]


def _looks_like_estimate(text_window: str) -> bool:
    lowered = text_window.lower()
    return any(marker in lowered for marker in _ESTIMATE_MARKERS)


# ── rules ─────────────────────────────────────────────────────────────────────
# Each matcher is a pure function (text: str) -> list[str] of matched claim
# snippets. No rule here reads anything but the string it is given.

#: Arithmetic with a float operand: FLOAT op (INT|FLOAT), or INT op FLOAT,
#: with an optional "= <float>" result tacked on. Requires an actual decimal
#: point on at least one operand, so `2 + 3 + 4` (SKILL.md's own "never
#: wrong" example) cannot match no matter how many terms it chains — operand
#: TYPE is the test, not count, same as SKILL.md says.
_FLOAT = r"\d+\.\d+"
_NUM = r"\d+(?:\.\d+)?"
_OP = r"[-+*/]"
_ARITH_FLOAT_RE = re.compile(
    rf"(?:{_FLOAT}\s*{_OP}\s*{_NUM}|\d+\s*{_OP}\s*{_FLOAT})(?:\s*=\s*{_FLOAT})?"
)


def _rule_non_integer_arithmetic(text: str) -> list[str]:
    claims = []
    for m in _ARITH_FLOAT_RE.finditer(text):
        if _looks_like_estimate(_window(text, m.start(), m.end())):
            continue
        claims.append(m.group(0))
    return claims


#: 2^53 = 9007199254740992. Any run of 15 or fewer digits is below it (the
#: largest 15-digit number is 999,999,999,999,999), so a 16-digit prefilter
#: avoids parsing every small integer in a message before the real
#: comparison.
_TWO_POW_53 = 2**53
#: A 16+ digit run, but NOT one that is the fractional tail of a float
#: (`0.30000000000000004`) or the middle of a longer digit string — that is a
#: non-integer already caught by the arithmetic rule, and flagging it here as an
#: "integer past 2^53" double-reports one claim and mislabels it.
_LARGE_INT_RE = re.compile(r"(?<![\d.])\d{16,}")


def _rule_large_integer_2_53(text: str) -> list[str]:
    claims = []
    for m in _LARGE_INT_RE.finditer(text):
        if int(m.group(0)) <= _TWO_POW_53:
            continue
        if _looks_like_estimate(_window(text, m.start(), m.end())):
            continue
        claims.append(m.group(0))
    return claims


#: "equivalent/ported/rewrote/translated/behaves the same", scoped to within
#: ~200 chars of a fenced code block. That distance is what keeps this rule
#: off bare prose ("these two approaches are equivalent" with no code in
#: sight) — SKILL.md's trigger is a claim about CODE, and a fence is the
#: cheapest signal that code is actually present in the turn.
_EQUIV_KEYWORD_RE = re.compile(
    r"\b(equivalent|ported|rewrote|translated|behaves? the same)\b", re.IGNORECASE
)
_FENCE_RE = re.compile(r"```")
_EQUIV_FENCE_DISTANCE = 200


def _rule_equivalence_or_port_claim(text: str) -> list[str]:
    fence_positions = [m.start() for m in _FENCE_RE.finditer(text)]
    if not fence_positions:
        return []
    claims = []
    for m in _EQUIV_KEYWORD_RE.finditer(text):
        near_fence = any(abs(m.start() - fp) <= _EQUIV_FENCE_DISTANCE for fp in fence_positions)
        if not near_fence:
            continue
        if _looks_like_estimate(_window(text, m.start(), m.end())):
            continue
        claims.append(m.group(0))
    return claims


#: `O(...)` with the usual complexity alphabet — capital `O(` only, NOT
#: case-insensitive, because a single shared IGNORECASE flag across both
#: alternatives would also match lowercase `o(` inside ordinary prose
#: ("look into(this)") — exactly the false-positive class this file exists
#: to avoid. "time complexity is" is matched separately, case-insensitively,
#: since that phrase carries no such risk.
_BIG_O_RE = re.compile(r"O\(\s*[0-9a-zA-Z^*\s+!log]+\)")
_TIME_COMPLEXITY_RE = re.compile(r"time complexity is", re.IGNORECASE)


#: Concept-discussion markers. "O(n) notation is taught", "asymptotic notation",
#: "the term O(1)" — the model is DISCUSSING Big-O, not claiming a piece of code's
#: complexity, so it is not the "about to state a Big-O" trigger. Suppressing
#: these is the conservatism the rule needs: firing on every `O(...)` in prose is
#: exactly the false-positive class that gets a rule turned off.
_BIG_O_CONCEPT_RE = re.compile(
    r"notation|asymptotic|the term|concept of|is taught|textbook", re.IGNORECASE
)


def _rule_big_o_claim(text: str) -> list[str]:
    claims = []
    for pattern in (_BIG_O_RE, _TIME_COMPLEXITY_RE):
        for m in pattern.finditer(text):
            window = _window(text, m.start(), m.end())
            if _looks_like_estimate(window) or _BIG_O_CONCEPT_RE.search(window):
                continue
            claims.append(m.group(0))
    return claims


#: "N% faster" / "speedup" / "faster by" — a claim of measured improvement.
#: Medium severity: it is the rule most likely to fire on a hedged, already-
#: labeled estimate (see the estimate-guard fixture), so it needs the
#: conservatism guard more than the high-severity rules do.
_SPEEDUP_RE = re.compile(
    r"\b\d+(?:\.\d+)?%?\s*faster\b|\bspeedup\b|faster by\b", re.IGNORECASE
)


def _rule_speedup_claim(text: str) -> list[str]:
    claims = []
    for m in _SPEEDUP_RE.finditer(text):
        if _looks_like_estimate(_window(text, m.start(), m.end())):
            continue
        claims.append(m.group(0))
    return claims


#: A hex or binary literal. On its own a literal is usually a CONSTANT — a flag,
#: a mask, an address — not a value the model computed and is now claiming. The
#: SKILL trigger is a value STATED in another base as a claim, which in practice
#: carries a conversion cue nearby ("in hex", "= 0x..", "255 in binary is .."):
#: requiring that cue is the conservatism. "Set the flag to 0xFF" must NOT fire;
#: "255 in hex is 0xFF" must.
_BASE_RE = re.compile(r"\b0x[0-9a-fA-F]+\b|\b0b[01]+\b")
_BASE_CONVERSION_CUE_RE = re.compile(
    r"\b(hex|hexadecimal|binary|decimal|base\s*\d+)\b|=\s*0[xb]", re.IGNORECASE
)


def _rule_non_decimal_base_value(text: str) -> list[str]:
    claims = []
    for m in _BASE_RE.finditer(text):
        window = _window(text, m.start(), m.end())
        if _looks_like_estimate(window):
            continue
        # A bare literal with no conversion context is a constant, not a claim.
        if not _BASE_CONVERSION_CUE_RE.search(window):
            continue
        claims.append(m.group(0))
    return claims


#: (rule_id, matcher, expected_tools, severity). Derived from SKILL.md's
#: "Call these — mandatory" table, highest-precision rules first. Locked by
#: tests/test_skill_detect.py: non_integer_arithmetic, equivalence_or_port_claim
#: and big_o_claim are proven recall==1.0/precision==1.0 against the labeled
#: fixture set; the rest get direct unit assertions but no fixture/labels
#: coverage yet.
RULES: list[tuple[str, Callable[[str], list[str]], list[str], str]] = [
    ("non_integer_arithmetic", _rule_non_integer_arithmetic, ["calc_exact"], "high"),
    ("large_integer_2^53", _rule_large_integer_2_53, ["calc_exact", "float_repr"], "high"),
    ("equivalence_or_port_claim", _rule_equivalence_or_port_claim,
     ["verify_translation", "compare_edge_cases"], "high"),
    ("big_o_claim", _rule_big_o_claim, ["analyze_complexity", "benchmark"], "high"),
    ("speedup_claim", _rule_speedup_claim, ["verify_optimization"], "medium"),
    ("non_decimal_base_value", _rule_non_decimal_base_value,
     ["radix_convert", "base_repr"], "medium"),
]


# ── miss schema ───────────────────────────────────────────────────────────────

@dataclass
class Miss:
    rule_id: str
    claim: str
    expected_tools: list[str]
    called_tools: list[str]
    severity: str  # "high" | "medium"
    location: dict = field(default_factory=dict)  # {"file": str, "turn": int}

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "claim": self.claim,
            "expected_tools": self.expected_tools,
            "called_tools": self.called_tools,
            "severity": self.severity,
            "location": self.location,
        }


# ── detection ─────────────────────────────────────────────────────────────────

def _load_conversation(path: Path) -> list[dict]:
    messages = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            messages.append(json.loads(line))
    return messages


def _tool_names(msg: dict) -> list[str]:
    return [tc["name"] for tc in msg.get("tool_calls") or [] if tc.get("name")]


def _called_tools_after(messages: list[dict], i: int) -> list[str]:
    """Tool names for turn `i`: its own `tool_calls`, plus any immediately
    following `role: "tool"` messages' `tool_calls` (a tool-role message
    reporting the call it made, per the conversation schema documented in
    this module's docstring)."""
    called = list(_tool_names(messages[i]))
    j = i + 1
    while j < len(messages) and messages[j].get("role") == "tool":
        called.extend(_tool_names(messages[j]))
        j += 1
    return called


def detect_file(path: str | Path) -> list[Miss]:
    """Run every rule in RULES against every assistant turn in `path`.

    A MISS is a rule match where none of that rule's `expected_tools` appear
    among the turn's `called_tools`.
    """
    path = Path(path)
    messages = _load_conversation(path)
    misses: list[Miss] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content") or ""
        called = _called_tools_after(messages, i)
        for rule_id, matcher, expected_tools, severity in RULES:
            for claim in matcher(content):
                if any(t in called for t in expected_tools):
                    continue
                misses.append(Miss(
                    rule_id=rule_id,
                    claim=claim,
                    expected_tools=list(expected_tools),
                    called_tools=called,
                    severity=severity,
                    location={"file": path.name, "turn": i},
                ))
    return misses


# ── scoring harness ──────────────────────────────────────────────────────────

def score(folder: str | Path, labels: dict | None = None) -> dict:
    """Detect over every `.jsonl` conversation in `folder` and score against
    `labels` (the `<folder>/labels.json` shape if not passed explicitly):

        {"<filename>": [{"turn": <int>, "rule_id": "<id>"}, ...]}

    A detected miss is a true positive iff (file, turn, rule_id) is in
    labels. Precision/recall follow the usual definitions and are defined as
    1.0 on an empty comparison set (nothing detected and nothing labeled)
    rather than raising a ZeroDivisionError.
    """
    folder = Path(folder)
    if labels is None:
        labels_path = folder / "labels.json"
        labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.is_file() else {}

    truth = {
        (fname, entry["turn"], entry["rule_id"])
        for fname, entries in labels.items()
        for entry in entries
    }

    all_misses: list[Miss] = []
    for conv_path in sorted(folder.glob("*.jsonl")):
        all_misses.extend(detect_file(conv_path))

    detected = {(m.location["file"], m.location["turn"], m.rule_id) for m in all_misses}
    true_positives = detected & truth
    false_positives = detected - truth
    false_negatives = truth - detected

    precision = (len(true_positives) / len(detected)) if detected else 1.0
    recall = (len(true_positives) / len(truth)) if truth else 1.0

    return {
        "precision": precision,
        "recall": recall,
        "true_positives": len(true_positives),
        "false_positives": len(false_positives),
        "false_negatives": len(false_negatives),
        "misses": [m.as_dict() for m in all_misses],
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", help="directory of .jsonl conversations (+ optional labels.json)")
    args = parser.parse_args(argv)

    folder = Path(args.folder)
    labels_path = folder / "labels.json"

    all_misses: list[Miss] = []
    for conv_path in sorted(folder.glob("*.jsonl")):
        all_misses.extend(detect_file(conv_path))
    print(json.dumps([m.as_dict() for m in all_misses], indent=2))

    if labels_path.is_file():
        result = score(folder)
        summary = {k: v for k, v in result.items() if k != "misses"}
        print(json.dumps(summary, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
