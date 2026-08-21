"""Optimisation verification and function extraction. No LLM in this module.

verify_optimization: given an original and a candidate the caller already has,
prove the candidate is a genuine improvement — same outputs, and measurably
faster at the same sizes. Both gates are measured; neither is an opinion.

extract_function: pull a named function plus its dependency closure into a
standalone program and run it (ast-exact for python3, best-effort elsewhere).

This module used to generate the candidate by calling a separately configured
model. The generation was never the differentiated part — the measurement was —
and requiring a second model made the measurement unreachable without one.
"""

from __future__ import annotations

import ast
import re

from . import executor, tools
from .translation import DEFAULT_EDGE_INPUTS, verify_translation


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
    # Carry the SIZE through the filter. per_size used to zip before["sizes"]
    # against the filtered pair list, so dropping any entry shifted every row:
    # a measurement taken at n=200 was reported as n=100.
    #
    # `aa > 0.0`, not `aa is not None`: a 0ms optimized run is reachable
    # (durations can be 0) and `bb / 0` raised ZeroDivisionError straight out of
    # the tool as an unhandled exception.
    sizes = before.get("sizes") or list(range(len(b)))
    triples = [(n, bb, aa) for n, bb, aa in zip(sizes, b, a)
               if bb is not None and bb > 1.0 and aa is not None and aa > 0.0]
    if not triples:
        return {"ratio": None, "measurable": False,
                "reason": "no size where both runs were measurable "
                          "(baseline below the 1ms noise floor, or the optimized "
                          "run measured 0ms)"}
    ratios = [bb / aa for _n, bb, aa in triples]
    import statistics
    median = statistics.median(ratios)
    return {
        "ratio": round(median, 2),
        "measurable": True,
        "per_size": [{"n": n, "before_ms": bb, "after_ms": aa,
                      "ratio": round(bb / aa, 2)}
                     for n, bb, aa in triples],
    }


def _accept_decision(sp: dict, min_speedup: float) -> tuple[bool, str]:
    """Decide accept/reject from a MEASURED speedup and the caller's threshold.

    Accepted requires the speedup to be REAL, not merely to clear an
    arithmetic comparison. `ratio >= min_speedup` alone certified a measured
    SLOWDOWN: with min_speedup=0 a measured 0.5x — a 2x slowdown — cleared
    `0.5 >= 0` and was returned accepted=True. So all three must hold:

      - the ratio was actually measured (measurable, numeric)
      - the threshold itself demands a speedup: min_speedup > 1
      - the measured ratio is an actual speedup AND clears it: ratio > 1
        and ratio >= min_speedup

    A ratio <= 1, or a min_speedup <= 1, can NEVER yield accepted=True; the
    grade side (grades.grade_verify_optimization) already refuses to certify a
    ratio <= 1, and this makes the tool agree at the source. `bool` is an `int`,
    but a measured ratio is never a bool.
    """
    ratio = sp.get("ratio")
    if not sp.get("measurable") or not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return False, "equivalent but not measurably faster"
    if min_speedup <= 1:
        return False, (f"min_speedup={min_speedup} does not demand a speedup "
                       f"(it must be > 1) — refusing to certify a non-improvement")
    if ratio <= 1:
        return False, (f"measured ratio {ratio}x is not a speedup (> 1x); "
                       f"equivalent but not faster")
    if ratio < min_speedup:
        return False, f"measured {ratio}x is below the required {min_speedup}x"
    return True, "verified faster"


def verify_optimization(original: str, candidate: str, language: str,
                        test_inputs: list[str] | None = None,
                        sizes: list[int] | None = None,
                        min_speedup: float = 1.15,
                        timeout: int = 30) -> dict:
    """Decide whether `candidate` is a genuine optimisation of `original`.

    Two gates, both measured, neither of them an opinion:

      correctness  both programs run on the same inputs and must agree
      speed        both are timed at the same sizes; the ratio must clear
                   `min_speedup`

    An accepted result means the executor watched it happen. A rejected one
    says which gate failed and by how much — "correct but only 1.09x" is a
    useful answer, and it is the one an optimiser that fabricates wins cannot
    give.

    The caller supplies both versions. This module used to generate the
    candidate by calling a second, separately configured model, which put the
    weakest link in the loop in charge of the creative half and left the
    measurement half unreachable on its own.
    """
    language = executor.registry.canonical(language) or language
# The FULL set, not DEFAULT_EDGE_INPUTS[:4]. The slice kept '', '0', '1', '-1'
    # and discarded '10', '100' and '0.1\n0.2' — the multi-digit cases and the
    # float one. Float formatting is among the most common genuine divergences
    # between ports, so the default evidence excluded the input most likely to
    # find a real bug.
    #
    # Demonstrated rather than argued: a python3 -> node port that agrees on
    # every integral sum and differs only in float rendering was CERTIFIED by
    # the old default and rejected by the full set.
    #
    #     D[:4]  passed=True   matched=4 mismatched=0
    #     D      passed=False  mismatched on '0.1\n0.2':
    #                          '0.30000000000000004' vs '0.3'
    #
    # Cost of the change, measured: 0.38s -> 0.67s per call.
    inputs = test_inputs if test_inputs else DEFAULT_EDGE_INPUTS
    size_list = sizes or [2000, 5000, 10000, 20000]

    ver = verify_translation(language, original, language, candidate, inputs,
                             timeout=timeout)
    if not ver.get("passed"):
        return {"ok": True, "accepted": False, "reason": "not equivalent",
                "verification": ver,
                "detail": "the candidate does not reproduce the original's "
                          "output; speed was not measured, because a faster "
                          "wrong answer is not an optimisation"}

    before = _timed(original, language, size_list, timeout=timeout)
    if not before.get("ok"):
        return {"ok": False, "error": f"baseline measurement failed: {before.get('error')}"}
    after = _timed(candidate, language, size_list, timeout=timeout)
    if not after.get("ok"):
        return {"ok": False, "error": f"candidate measurement failed: {after.get('error')}"}

    sp = _speedup(before, after)
    accepted, reason = _accept_decision(sp, min_speedup)
    return {
        "ok": True,
        "accepted": accepted,
        "reason": reason,
        "speedup": sp,
        "min_speedup": min_speedup,
        "verification": ver,
        "language": language,
    }


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

# The FULL set, not DEFAULT_EDGE_INPUTS[:4]. The slice kept '', '0', '1', '-1'
    # and discarded '10', '100' and '0.1\n0.2' — the multi-digit cases and the
    # float one. Float formatting is among the most common genuine divergences
    # between ports, so the default evidence excluded the input most likely to
    # find a real bug.
    #
    # Demonstrated rather than argued: a python3 -> node port that agrees on
    # every integral sum and differs only in float rendering was CERTIFIED by
    # the old default and rejected by the full set.
    #
    #     D[:4]  passed=True   matched=4 mismatched=0
    #     D      passed=False  mismatched on '0.1\n0.2':
    #                          '0.30000000000000004' vs '0.3'
    #
    # Cost of the change, measured: 0.38s -> 0.67s per call.
    inputs = test_inputs if test_inputs else DEFAULT_EDGE_INPUTS
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
