"""Complexity analyzer: static heuristic Big-O estimation.

Language-agnostic structural scan:
  - loop constructs per nesting depth  -> O(n^k) base
  - recursion detection                -> exponential / nlogn flags
  - linear-scan builtins (sort etc.)   -> n log n adjustments
  - hash/constant-time ops             -> n^1 stays n

Optionally refined by an LLM via the local LiteLLM gateway when
CODECALC_LLM_MODEL is set (off by default — deterministic heuristic first).
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

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

    # optional LLM refinement through the local LiteLLM gateway
    model = os.environ.get("CODECALC_LLM_MODEL")
    gateway = os.environ.get("CODECALC_LLM_GATEWAY", "http://100.78.123.100:4001/v1/chat/completions")
    if model:
        refined = _llm_refine(code, language, result, model, gateway)
        if refined:
            result["llm_refinement"] = refined
    return result


def _llm_refine(code: str, language: str, heuristic: dict, model: str, gateway: str) -> dict | None:
    prompt = (
        f"Analyze the asymptotic time complexity of this {language} code. "
        f"Structural heuristic says: {heuristic['estimate']} ({heuristic['basis']}).\n"
        "Give your verdict as strict JSON: {\"estimate\": \"O(...)\", "
        "\"reason\": \"<2 sentences>\"}. Code:\n```\n" + code[:2000] + "\n```"
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        gateway, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as exc:
        return {"error": f"LLM refinement unavailable: {exc}"}
