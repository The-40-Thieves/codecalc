"""Complexity analyzer: static heuristic Big-O estimation.

Language-agnostic structural scan:
  - loop constructs per nesting depth  -> O(n^k) base
  - recursion detection                -> exponential / nlogn flags
  - linear-scan builtins (sort etc.)   -> n log n adjustments
  - hash/constant-time ops             -> n^1 stays n

Optionally refined by an LLM when CODECALC_COMPLEXITY_LLM is set AND a gateway
is configured. Off by default: the deterministic heuristic is the product, and
a static analyzer that quietly makes a network call per invocation is not.
"""

from __future__ import annotations

import json
import os
import re

from . import llm

#: Opt-in flag for the LLM second opinion. Deliberately its OWN variable rather
#: than "a gateway is configured": CODECALC_LLM_GATEWAY exists for translate_code
#: and optimize_code, and reusing it here would mean configuring those two tools
#: silently added a network round-trip to every analyze_complexity call.
REFINE_ENV = "CODECALC_COMPLEXITY_LLM"

LOOP_RE = re.compile(
    r"\b(for|while|foreach|until|loop|repeat)\b"
    r"|\bfor\s*\("          # C-style for(
    r"|\.forEach\(|\.map\(|\.filter\(|\.reduce\("   # JS iterators
    r"|for\s+\w+\s+in\b"    # python/ruby for x in
    r"|for\s+\w+\s*:="      # go for x :=
    r"|for\s+\w+\s+of\b"    # JS for x of
)
RECURSION_RE = re.compile(
    r"\b(def|func(?:tion)?|fn|sub)\s+(\w+)\s*\("
    r"|public\s+(?:static\s+)?\w+\s+(\w+)\s*\("
)
CALL_RE = re.compile(r"\b(\w+)\s*\(")
SORT_RE = re.compile(r"\.sort\(|\.sort_by\(|\bsort\(|\bsorted\(|\bsort_by\b")
BINARY_RE = re.compile(r"\bbinary_search|bisect|\bmid\s*=\s*\(|while\s+\w+\s*<=\s*\w+.*(?:mid|lo|hi)")
HASH_RE = re.compile(r"\bdict\b|\bHashMap\b|\bMap\b|\bHashSet\b|\bSet\b|\b{}|\bHash\b")
NESTED_RE = re.compile(r"^\s+")   # indentation marker


def _count_loops(code: str) -> tuple[int, int]:
    """Returns (loop_count, max_nesting_depth) via indentation-aware scan."""
    lines = code.splitlines()
    indent_stack: list[int] = []
    loops = 0
    max_depth = 0
    for raw in lines:
        if not raw.strip() or raw.strip().startswith(("#", "//", "/*", "*")):
            continue
        indent = len(raw) - len(raw.lstrip())
        while indent_stack and indent <= indent_stack[-1]:
            indent_stack.pop()
        if LOOP_RE.search(raw) and not raw.strip().startswith(("}", ")")):
            loops += 1
            indent_stack.append(indent)
            max_depth = max(max_depth, len(indent_stack))
    return loops, max_depth


def _detect_recursion(code: str) -> tuple[bool, str]:
    """Best-effort recursion detection: does any function call itself?"""
    funcs = []
    for m in RECURSION_RE.finditer(code):
        name = m.group(2) or m.group(3)
        if name:
            funcs.append(name)
    for fn in funcs:
        # find the function body and look for self-calls
        m = re.search(rf"\b{fn}\s*\([^)]*\)[^#\n]*\n((?:.|\n)*?)(?=\n\S|\Z)", code)
        if m and re.search(rf"\b{fn}\s*\(", m.group(1)):
            return True, fn
    return False, ""


def analyze(code: str, language: str = "python3") -> dict:
    loops, depth = _count_loops(code)
    recursive, fn = _detect_recursion(code)
    has_sort = bool(SORT_RE.search(code))
    has_hash = bool(HASH_RE.search(code))

    # base estimate
    if recursive:
        estimate = "O(2^n) — recursion detected"
        basis = f"recursive function '{fn}'"
    elif depth >= 2:
        estimate = f"O(n^{depth})"
        basis = f"{depth} nested loop levels ({loops} loop constructs)"
    elif loops == 1:
        estimate = "O(n) — with sort: O(n log n)"
        basis = "1 loop level"
    elif has_sort and loops == 0:
        estimate = "O(n log n)"
        basis = "sorting call, no loops"
    else:
        estimate = "O(1)"
        basis = "no loops, no recursion"

    notes = []
    if has_sort:
        notes.append("sort/builtin detected (O(n log n) worst case)")
    if has_hash:
        notes.append("hash structures → average-case O(1) lookups")
    if loops == 0 and not recursive and not has_sort:
        notes.append("constant-time surface; may hide loops in library calls")

    result = {
        "ok": True,
        "estimate": estimate,
        "basis": basis,
        "loops": loops,
        "max_nesting": depth,
        "recursion": recursive,
        "notes": notes,
    }

    # Optional LLM refinement — opt-in, and only if a gateway exists to ask.
    # The structural estimate above is always what you get by default.
    if os.environ.get(REFINE_ENV) and llm.GATEWAY:
        refined = _llm_refine(code, language, result)
        if refined:
            result["llm_refinement"] = refined
    return result


def _llm_refine(code: str, language: str, heuristic: dict) -> dict | None:
    """Ask the configured gateway to second-guess the structural estimate.

    Goes through llm.chat rather than its own urllib call. It previously carried
    a duplicate of the gateway plumbing, including a second hardcoded private
    address and — because it built the request by hand — no Authorization
    header at all, so it could never have worked against a gateway that
    required auth.
    """
    prompt = (
        f"Analyze the asymptotic time complexity of this {language} code. "
        f"Structural heuristic says: {heuristic['estimate']} ({heuristic['basis']}).\n"
        "Give your verdict as strict JSON: {\"estimate\": \"O(...)\", "
        "\"reason\": \"<2 sentences>\"}. Reply with the JSON object only, no "
        "markdown fence. Code:\n```\n" + code[:2000] + "\n```"
    )
    try:
        content = llm.chat(prompt, timeout=20, max_tokens=512).strip()
        # Tolerate a fenced reply: response_format=json_object is not portable
        # across OpenAI-compatible gateways, so the format is requested in the
        # prompt and the fence stripped here rather than relied upon.
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(content)
    except Exception as exc:
        return {"error": f"LLM refinement unavailable: {exc}"}
