"""Complexity analyzer: static heuristic Big-O estimation.

Language-agnostic structural scan:
  - loop constructs per nesting depth  -> O(n^k) base
  - recursion detection                -> exponential / nlogn flags
  - linear-scan builtins (sort etc.)   -> n log n adjustments
  - hash/constant-time ops             -> n^1 stays n

Structural only: a tree-sitter parse plus growth heuristics. There is no LLM
in this path and no network call. An optional LLM "second opinion" used to sit
behind CODECALC_COMPLEXITY_LLM; it is gone, because a static analyser that can
make a network call is a different kind of tool, and the deterministic estimate
was always the product.
"""

from __future__ import annotations

import re

from . import parsing

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
    """Estimate asymptotic complexity from the code's STRUCTURE.

    Loop counting and recursion detection come from a real parser
    (`codecalc.parsing`). They used to come from regexes, which counted the word
    "for" inside strings, comments and identifiers like `format()`, read a
    chained `.map().filter().reduce()` as three loops, and called a docstring
    mentioning a function's own name "recursion". tests/test_parsing_vs_regex.py
    holds the divergent cases with the correct answer for each.

    `analysis` in the result says which path produced the numbers, so a caller
    can tell a parse from a fallback instead of assuming.
    """
    facts = parsing.analyse(code, language)
    if facts.parsed:
        loops, depth = facts.loops, facts.max_loop_depth
        recursive = bool(facts.recursive_functions)
        fn = facts.recursive_functions[0] if recursive else ""
        analysis = "tree-sitter"
    else:
        # No grammar for this language, or the parser rejected the input. Say so
        # rather than reporting a heuristic as though it were a parse.
        loops, depth = _count_loops(code)
        recursive, fn = _detect_recursion(code)
        analysis = "regex-fallback"

    # These two stay textual: "does this call a sort" and "does this use a hash
    # map" are library questions, not grammar ones, and a parser has no more
    # authority over them than a regex does.
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

    if analysis == "regex-fallback" and facts.reason:
        notes.append(f"structural heuristic only ({facts.reason})")
    if facts.parsed and facts.has_error:
        notes.append("source has syntax errors; the parse is partial")

    result = {
        "ok": True,
        "estimate": estimate,
        "basis": basis,
        "loops": loops,
        "max_nesting": depth,
        "recursion": recursive,
        "analysis": analysis,
        "grammar": facts.grammar,
        "functions": facts.functions if facts.parsed else [],
        "notes": notes,
    }

    return result


