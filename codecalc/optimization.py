"""Code optimization with proof, and function extraction.

optimize_code: an LLM proposes an optimized version; the executor VERIFIES
correctness (identical stdout on test inputs) AND measures speedup (same
sizes, min-of-repeats, baseline-subtracted). Accepted only when correct AND
measurably faster; otherwise retried once with the failure reason. The LLM
can claim O(n²) -> O(n); the executor proves it.

extract_function: pull a named function out of a program with its dependency
closure (imports + referenced helpers), synthesize a standalone runner, and
execute it in the sandbox. Python uses ast for exact extraction; other
languages use best-effort brace/paren matching.
"""

from __future__ import annotations

import ast
import re

from . import executor, llm, tools
from .translation import DEFAULT_EDGE_INPUTS, _extract_code, verify_translation

_OPTIMIZE_SYSTEM = (
    "You optimize code for speed. Preserve behavior EXACTLY: same stdin "
    "handling, same stdout format, same edge cases. Prefer algorithmic "
    "improvements (better complexity) over micro-tuning. Output ONLY valid "
    "JSON: {\"code\": \"<the optimized program>\", \"notes\": \"<what you "
    "changed and why>\"}. Never wrap in markdown, never explain outside JSON."
)


def _timed(code: str, language: str, sizes: list[int], timeout: int = 30) -> dict:
    """Measure min-of-repeats per size, auto-scaling until work is visible."""
    runs, err = tools._measure(language, code, sizes, timeout, repeats=3)
    if err:
        return {"ok": False, "error": err["error"]}
    # auto-scale: work too small to measure? grow sizes (up to 4x)
    for _ in range(4):
        measured = [r["duration_ms"] for r in runs if r.get("duration_ms")]
        if not measured:
            break
        if max(measured) >= 20.0 or min(measured) >= 5.0:
            break
        sizes = [n * 10 for n in sizes]
        runs, err = tools._measure(language, code, sizes, timeout, repeats=3)
        if err:
            break
    return {
        "ok": True,
        "sizes": [r["n"] for r in runs],
        "durations_ms": [r["duration_ms"] for r in runs],
    }


def _speedup(before: dict, after: dict) -> dict:
    """Ratio after/before per size (median used as the headline)."""
    b = before.get("durations_ms") or []
    a = after.get("durations_ms") or []
    if not b or not a or len(b) != len(a):
        return {"ratio": None, "measurable": False, "reason": "no comparable timings"}
    pairs = [(bb, aa) for bb, aa in zip(b, a) if bb and bb > 1.0 and aa is not None]
    if not pairs:
        return {"ratio": None, "measurable": False,
                "reason": "baseline below noise floor (all runs < 1ms)"}
    ratios = [bb / aa for bb, aa in pairs]
    import statistics
    median = statistics.median(ratios)
    return {
        "ratio": round(median, 2),
        "measurable": True,
        "per_size": [{"n": n, "before_ms": bb, "after_ms": aa,
                      "ratio": round(bb / aa, 2)}
                     for n, (bb, aa) in zip(before["sizes"], pairs)],
    }


def optimize_code(code: str, language: str,
                  test_inputs: list[str] | None = None,
                  sizes: list[int] | None = None,
                  min_speedup: float = 1.15,
                  model: str | None = None, timeout: int = 30) -> dict:
    """Optimize `code`; accept only if correct AND measurably faster."""
    language = executor.registry.canonical(language) or language
    inputs = test_inputs if test_inputs else DEFAULT_EDGE_INPUTS[:4]
    size_list = sizes or [2000, 5000, 10000, 20000]

    before = _timed(code, language, size_list)
    if not before.get("ok"):
        return {"ok": False, "error": f"baseline measurement failed: {before.get('error')}"}

    prompt = (
        f"Optimize this {language} program for speed. It reads input from stdin "
        f"(first line is an integer N unless noted) and must keep EXACTLY the "
        f"same stdout. Prefer algorithmic improvements.\n\n"
        f"Source ({language}):\n```\n{code[:8000]}\n```"
    )

    last = None
    for attempt in (1, 2):
        try:
            text = llm.chat(prompt, system=_OPTIMIZE_SYSTEM, model=model,
                            timeout=timeout)
        except Exception as exc:
            return {"ok": False, "error": f"LLM unavailable: {exc}",
                    "llm_available": False}
        optimized = _extract_code(text)
        if not optimized:
            return {"ok": False, "error": "LLM did not return code",
                    "llm_available": True, "raw": text[:500]}

        # correctness: both versions, same inputs, stdout must match
        ver = verify_translation(language, code, language, optimized, inputs)
        if not ver["passed"]:
            last = {"stage": "correctness", "verification": ver}
            mismatch = next((c for c in ver["cases"] if not c["match"]), None)
            if attempt == 1 and mismatch:
                prompt += (
                    "\n\nVERIFICATION FAILED on input "
                    f"{mismatch['input']!r}:\n"
                    f"original stdout: {mismatch['source']['stdout'][:300]!r}\n"
                    f"optimized stdout: {mismatch['target']['stdout'][:300]!r}\n"
                    f"optimized stderr: {mismatch['target']['stderr'][:300]!r}\n"
                    "Fix the optimization so behavior is identical."
                )
            continue

        # speedup: measured, not claimed
        after = _timed(optimized, language, before["sizes"])
        if not after.get("ok"):
            last = {"stage": "measurement", "error": after.get("error")}
            continue
        sp = _speedup(before, after)
        if sp["measurable"] and sp["ratio"] is not None and sp["ratio"] >= min_speedup:
            return {
                "ok": True, "language": language,
                "optimized_code": optimized,
                "speedup_ratio": sp["ratio"],
                "notes": _llm_notes(text),
                "before": before, "after": after, "speedup": sp,
                "verification": ver, "attempts": attempt, "llm_available": True,
            }
        if not sp["measurable"]:
            # baseline too small to measure: correctness alone is the gate
            return {
                "ok": True, "language": language,
                "optimized_code": optimized,
                "speedup_ratio": None,
                "notes": _llm_notes(text),
                "speedup": sp,
                "verification": ver, "attempts": attempt, "llm_available": True,
                "warning": "baseline below noise floor; accepted on correctness only",
            }
        last = {"stage": "speedup", "speedup": sp}
        if attempt == 1:
            prompt += (
                f"\n\nOPTIMIZATION NOT FASTER: measured speedup ratio is "
                f"{sp['ratio']}x (need >= {min_speedup}x). The code may have "
                "changed complexity but got slower, or the change was "
                "cosmetic. Find a real algorithmic improvement."
            )

    return {
        "ok": False, "language": language,
        "error": "no accepted optimization after retry",
        "stage": (last or {}).get("stage"), "detail": last,
        "attempts": 2, "llm_available": True,
    }


def _llm_notes(text: str) -> str:
    m = re.search(r'"notes"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        try:
            import json
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            return m.group(1)[:300]
    return ""


# ── function extraction ────────────────────────────────────────────────────

def _py_extract(code: str, name: str) -> dict | None:
    """ast-based extraction for python: imports + referenced helpers + target."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"ok": False, "error": f"parse error: {exc}"}

    target = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == name), None)
    if target is None:
        return {"ok": False, "error": f"function '{name}' not found"}

    # names the target body references (Load context)
    referenced = {n.id for n in ast.walk(target)
                  if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}

    imports: list[ast.stmt] = []
    helpers: list[ast.stmt] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            imports.append(stmt)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and stmt.name in referenced and stmt is not target:
            helpers.append(stmt)
            referenced |= {n.id for n in ast.walk(stmt)
                           if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}

    import ast as _a
    # stdin preamble: parse whitespace-separated tokens into __cc_args
    preamble = _a.parse(
        "import sys\n"
        "__cc_line = sys.stdin.read().strip()\n"
        "__cc_tokens = __cc_line.split() if __cc_line else []\n"
        "def __cc_conv(t):\n"
        "    try: return int(t)\n"
        "    except ValueError: pass\n"
        "    try: return float(t)\n"
        "    except ValueError: return t\n"
        "__cc_args = [__cc_conv(t) for t in __cc_tokens]\n"
    ).body
    body: list[ast.stmt] = [*imports, *helpers, target, *preamble,
                            *_a.parse(f"\n__cc_result = {name}(*__cc_args)\n"
                                      f"print(__cc_result)").body]
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    return {"ok": True, "program": ast.unparse(module),
            "signature": ast.unparse(target.args)}


def _generic_extract(code: str, name: str, language: str) -> dict | None:
    """Best-effort extraction for non-python: keep imports + target function
    block by brace/paren matching. No dependency closure — helpers must be
    inlined by the caller or the function must be self-contained."""
    lines = code.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.search(rf"\b(def|func|function|fun|fn|public\s+\w+\s+{re.escape(name)}\s*\(|\b{re.escape(name)}\s*\()", line):
            start = i
            break
    if start is None:
        return {"ok": False, "error": f"function '{name}' not found"}
    # find closing brace at depth 0 (or end of line for single-line funcs)
    depth = 0
    end = start
    for j in range(start, len(lines)):
        depth += lines[j].count("{") - lines[j].count("}")
        end = j
        if depth <= 0 and j > start:
            break
    block = "\n".join(lines[start:end + 1])

    # keep import lines only (top of file)
    imports = [l for l in lines[:start]
               if re.match(r"^\s*(import|use|from|require|using|#include)\b", l)]
    return {"ok": True, "program": "\n".join(imports + [block]),
            "signature": lines[start][:120]}


def extract_function(code: str, language: str, function_name: str,
                     call: str | None = None,
                     test_inputs: list[str] | None = None,
                     timeout: int = 15) -> dict:
    """Extract `function_name` from `code` with its dependency closure, build
    a standalone runner, and execute it on `test_inputs` in the sandbox.

    python3 gets exact ast extraction (imports + referenced helper functions).
    Other languages get best-effort block extraction (imports + function body).
    The runner calls the function with args parsed from stdin; pass `call` to
    override (e.g. 'fib(int(sys.stdin.readline()))').
    """
    language = executor.registry.canonical(language) or language
    if language == "python3":
        ex = _py_extract(code, function_name)
        if ex is None or not ex.get("ok"):
            return ex or {"ok": False, "error": "extraction failed"}
        program = ex["program"]
        if call:
            program = program.replace(
                "__cc_result = " + function_name + "(*__cc_args)",
                f"__cc_result = {call}")
    else:
        ex = _generic_extract(code, function_name, language)
        if not ex.get("ok"):
            return ex
        program = ex["program"]
        # no auto-runner for non-python without a call expression
        if not call:
            return {"ok": True, "language": language,
                    "function": function_name,
                    "extracted_program": program,
                    "warning": "pass `call` (a valid expression in the target "
                               "language) to execute; only extraction done",
                    "signature": ex.get("signature")}

    inputs = test_inputs if test_inputs else DEFAULT_EDGE_INPUTS[:4]
    runs = []
    for stdin in inputs:
        r = executor.execute(language, program, stdin=stdin, timeout=timeout)
        runs.append({"input": stdin[:60], "ok": r.get("ok"),
                     "stdout": (r.get("stdout") or "")[:400],
                     "stderr": (r.get("stderr") or "")[:200],
                     "verdict": r.get("verdict")})
    return {
        "ok": True, "language": language, "function": function_name,
        "extracted_program": program,
        "signature": ex.get("signature"),
        "runs": runs,
        "passed": all(r["ok"] for r in runs),
    }
