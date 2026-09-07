"""Optimisation verification and function extraction. No LLM in this module.

verify_optimization: given an original and a candidate the caller already has,
prove the candidate is a genuine improvement — same outputs, and measurably
AND SIGNIFICANTLY faster at the same sizes (a one-sided Mann-Whitney U test,
not just a ratio clearing a threshold — see `_infer_speedup`). Both gates are
measured; neither is an opinion.

extract_function: pull a named function plus its dependency closure into a
standalone program and run it (ast-exact for python3, best-effort elsewhere).

This module used to generate the candidate by calling a separately configured
model. The generation was never the differentiated part — the measurement was —
and requiring a second model made the measurement unreachable without one.
"""

from __future__ import annotations

import ast
import re

from . import executor, stats, tools
from .translation import DEFAULT_EDGE_INPUTS, verify_translation

#: Runs per size. Raised from 3: with 3-vs-3 timings the smallest one-sided
#: Mann-Whitney p a single size can ever produce is 1/C(6,3) = 1/20, so no
#: size could reach the conventional alpha=0.05 no matter how clean the
#: separation was — the inference in `_infer_speedup` below had no floor to
#: stand on. At 5-vs-5 the floor is 1/C(10,5) = 1/252, comfortably past 0.05.
REPEATS = 5


def _timed(code: str, language: str, sizes: list[int], timeout: int = 30) -> dict:
    """Measure min-of-repeats per size, auto-scaling until work is visible.

    Keeps `all_runs_ms` per size (not just the min) alongside `durations_ms`:
    `_infer_speedup` needs the individual runs to test before-vs-after, not
    just the headline minimum `_speedup` uses for the ratio.
    """
    runs, err = tools._measure(language, code, sizes, timeout, repeats=REPEATS)
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
        runs, err = tools._measure(language, code, sizes, timeout, repeats=REPEATS)
        if err:
            break
    return {
        "ok": True,
        "sizes": [r["n"] for r in runs],
        "durations_ms": [r["duration_ms"] for r in runs],
        "all_runs_ms": [r["all_runs_ms"] for r in runs],
    }


def _remeasured(code: str, language: str, sizes: list[int], timeout: int) -> dict:
    """A single `_measure` pass at an EXACT size list, no auto-scale loop.

    Used only by `_align_sizes` below, once we already know which sizes the
    OTHER side found comparable — there is nothing left to discover, so
    re-running the auto-scale ladder here would just add calls for no reason.
    """
    runs, err = tools._measure(language, code, sizes, timeout, repeats=REPEATS)
    if err:
        return {"ok": False, "error": err["error"]}
    return {
        "ok": True,
        "sizes": [r["n"] for r in runs],
        "durations_ms": [r["duration_ms"] for r in runs],
        "all_runs_ms": [r["all_runs_ms"] for r in runs],
    }


def _align_sizes(before: dict, after: dict, original: str, candidate: str,
                 language: str, timeout: int) -> tuple[dict, dict]:
    """Make `before["sizes"] == after["sizes"]`, re-measuring one side if not.

    `_timed(original, ...)` and `_timed(candidate, ...)` each auto-scale their
    OWN copy of `sizes` independently. Each rescale round multiplies every
    entry by 10, so the two calls converge to the SAME LENGTH list but not
    necessarily the same VALUES — the common case is exactly the one that
    matters most here: a slow baseline is visible at the default sizes (no
    rescale), a genuinely fast candidate is not (one or more rescales).
    `_speedup` and `_infer_speedup` both zip `before`/`after` by POSITION, so
    an undetected mismatch silently pairs before@2000 against after@20000 —
    a per-size ratio and p-value both labelled with the wrong size, the ratio
    bug from `_speedup`'s own comment history repeated one layer up, now
    hiding under a significance test instead of a plain number.

    Fix: when the two `sizes` lists differ, re-measure the side that scaled
    LESS at the OTHER side's final sizes — one extra `_measure` round (not
    a full re-run of the auto-scale ladder: we already know these sizes are
    at least as visible as whatever the less-scaled side already confirmed,
    since bigger sizes make invisible work MORE visible, never less). This
    costs calls only when a divergence actually happened; the common case
    (both sides settle on the same sizes, which every measured run on this
    box did) pays nothing extra.

    An empty-intersection approach (skip the mismatched sizes entirely) was
    considered and rejected: the scenario this fix exists for — a dramatic,
    genuine speedup — is exactly the one most likely to diverge by MORE than
    one rescale round, so intersecting on already-measured sizes would most
    often produce nothing to compare for the very wins this tool exists to
    certify, silently downgrading `sizes_total` to 0 and `accepted` to False
    on real optimisations. Re-measuring costs more calls (bounded — see
    `verify_optimization`'s docstring); the accuracy is worth it.
    """
    if before.get("sizes") == after.get("sizes") or not before.get("sizes") or not after.get("sizes"):
        return before, after
    # Both lists are the SAME original sizes multiplied by a (possibly
    # different) power of 10, so comparing any one shared position is enough
    # to tell which side scaled more.
    if before["sizes"][0] < after["sizes"][0]:
        return _remeasured(original, language, after["sizes"], timeout), after
    return before, _remeasured(candidate, language, before["sizes"], timeout)


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


#: Two-tailed significance level for the per-size Mann-Whitney test. The test
#: itself is one-sided (`alternative="less"`: after < before), but 0.05 is the
#: conventional name for "this could plausibly be noise" regardless of
#: sidedness, and callers reading `inference.alpha` expect the familiar number.
ALPHA = 0.05


def _infer_speedup(before: dict, after: dict, alpha: float = ALPHA) -> dict:
    """Test, per size, whether `after` is stochastically faster than `before`.

    `_speedup`'s median ratio says HOW MUCH faster the measured runs were; it
    says nothing about whether that gap could be noise. With `REPEATS` runs
    per size this runs a one-sided Mann-Whitney U test — H1: after's runtimes
    are stochastically LESS than before's — at each size independently, using
    the raw `all_runs_ms` samples `_timed` now keeps rather than only the
    per-size minimum.

    Per-size, not pooled across sizes: the sizes are different workloads (an
    O(n) algorithm at n=2000 and n=20000 are not exchangeable observations of
    "the same thing"), so pooling their raw ratios into one test would treat
    a real complexity-class difference as within-test noise. Combining
    per-size verdicts into ONE decision instead — "does a MAJORITY of sizes
    reject the null" — is the simple, honestly-stated middle ground: it does
    not require every size to be individually significant (a single size with
    few distinguishable values, e.g. after auto-scale collapsed to n=1 usable
    triple, can be underpowered on its own) but does require the win to show
    up at more sizes than not, not just in the aggregate median.

    Sizes where `_speedup` itself could not compute a ratio (baseline below
    the 1ms noise floor, or fewer than 2 runs on either side) are skipped —
    the same floor `_speedup` already applies, so a size excluded from the
    ratio is excluded from the significance count too, rather than being
    read as either a pass or a fail.
    """
    b = before.get("durations_ms") or []
    a = after.get("durations_ms") or []
    b_runs = before.get("all_runs_ms") or []
    a_runs = after.get("all_runs_ms") or []
    sizes = before.get("sizes") or list(range(len(b)))

    per_size = []
    for idx, n in enumerate(sizes):
        if idx >= len(b) or idx >= len(a):
            continue
        bb, aa = b[idx], a[idx]
        # same floor as _speedup: a baseline at/under the noise floor, or an
        # optimized run that failed to produce a duration, is not comparable.
        if bb is None or bb <= 1.0 or aa is None or aa <= 0.0:
            continue
        b_sample = b_runs[idx] if idx < len(b_runs) else []
        a_sample = a_runs[idx] if idx < len(a_runs) else []
        if len(b_sample) < 2 or len(a_sample) < 2:
            continue
        mwu = stats.mann_whitney_u(a_sample, b_sample, alternative="less")
        rb = stats.rank_biserial_correlation(mwu["u"], mwu["n1"], mwu["n2"])
        per_size.append({
            "size": n,
            "n_before": len(b_sample),
            "n_after": len(a_sample),
            "u": mwu["u"],
            "p_value": mwu["p_value"],
            "rank_biserial": round(rb, 4),
        })

    sizes_total = len(per_size)
    sizes_rejecting = sum(1 for r in per_size if r["p_value"] < alpha)
    if sizes_total == 0:
        decision_basis = "no size had enough comparable runs for a significance test"
    else:
        decision_basis = (f"{sizes_rejecting}/{sizes_total} size(s) reject "
                          f"the null (after not faster) at alpha={alpha}; "
                          f"a majority is required")
    return {
        "test": "mann_whitney_u",
        "alternative": "after_faster",
        "alpha": alpha,
        "per_size": per_size,
        "sizes_rejecting": sizes_rejecting,
        "sizes_total": sizes_total,
        "decision_basis": decision_basis,
    }


def _accept_decision(sp: dict, min_speedup: float, inference: dict) -> tuple[bool, str]:
    """Decide accept/reject from a MEASURED speedup, the caller's threshold,
    AND a significance test — not the threshold comparison alone.

    Accepted requires ALL of:

      - the ratio was actually measured (measurable, numeric)
      - the threshold itself demands a speedup: min_speedup > 1
      - the measured ratio is an actual speedup AND clears it: ratio > 1
        and ratio >= min_speedup
      - a MAJORITY of sizes reject the null (after not faster) in
        `_infer_speedup`'s per-size Mann-Whitney test at `inference["alpha"]`

    The last bullet is the fix this function exists for. `ratio >= min_speedup`
    used to be the whole decision, but with REPEATS runs per size the smallest
    one-sided p a single size can ever produce is 1/C(2*REPEATS, REPEATS) — no
    size could clear alpha=0.05 by construction at REPEATS=3, so "the median
    ratio cleared 1.15x" was an arithmetic fact about noisy timings, not
    evidence the difference was real. A ratio can still clear min_speedup on
    pure noise (see tests/test_translation_verify.py's false-accept-rate
    assertion on IDENTICAL before/after code); requiring the test to also
    reject closes exactly that gap.

    A ratio <= 1, a min_speedup <= 1, or a non-majority-rejecting test result
    can NEVER yield accepted=True; the grade side
    (grades.grade_verify_optimization) already refuses to certify a ratio <=
    1, and this makes the tool agree at the source. `bool` is an `int`, but a
    measured ratio is never a bool.
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
    sizes_total = inference.get("sizes_total", 0)
    sizes_rejecting = inference.get("sizes_rejecting", 0)
    if sizes_total == 0 or sizes_rejecting * 2 <= sizes_total:
        return False, (f"measured {ratio}x clears {min_speedup}x, but the "
                       f"one-sided significance test does not: "
                       f"{inference.get('decision_basis')} — could be noise")
    return True, (f"verified faster: {ratio}x median, {sizes_rejecting}/"
                  f"{sizes_total} size(s) significant at "
                  f"alpha={inference.get('alpha')}")


def verify_optimization(original: str, candidate: str, language: str,
                        test_inputs: list[str] | None = None,
                        sizes: list[int] | None = None,
                        min_speedup: float = 1.15,
                        timeout: int = 30) -> dict:
    """Decide whether `candidate` is a genuine optimisation of `original`.

    Two gates, both measured, neither of them an opinion:

      correctness  both programs run on the same inputs and must agree
      speed        both are timed at the same sizes (`REPEATS` runs each);
                   the median ratio must clear `min_speedup` AND a
                   one-sided Mann-Whitney U test must reject "candidate is
                   not faster" (alpha=0.05) at a MAJORITY of the measured
                   sizes — see `_infer_speedup` and `_accept_decision`.

    The significance test exists because the ratio check alone has no
    statistical footing at the sample sizes this tool can afford: with
    `REPEATS` runs per side, the smallest one-sided p a single size can ever
    produce is `1/C(2*REPEATS, REPEATS)`, so a ratio that merely clears
    `min_speedup` can still be pure noise (see `_accept_decision`'s
    docstring, and the false-accept-rate tests in
    tests/test_translation_verify.py). The result's `inference` field carries
    the full per-size evidence — U statistic, p-value, and rank-biserial
    effect size — so a caller can see WHY a candidate that looks 1.3x faster
    was still rejected, not just that it was.

    An accepted result means the executor watched it happen and the
    difference cleared a stated significance test. A rejected one says which
    gate failed and by how much — "correct, 1.3x median, but only 1/4 sizes
    significant" is a useful answer, and it is the one an optimiser that
    fabricates wins cannot give.

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

    # Each side's own auto-scale in `_timed` runs independently, so they can
    # converge on different sizes (see `_align_sizes`'s docstring). Fix that
    # BEFORE either `_speedup` or `_infer_speedup` sees these dicts — both
    # zip before/after by position, so an unaligned pair mislabels every
    # per-size ratio and p-value with the wrong size.
    before, after = _align_sizes(before, after, original, candidate, language, timeout)
    if not before.get("ok"):
        return {"ok": False, "error": f"baseline re-measurement failed: {before.get('error')}"}
    if not after.get("ok"):
        return {"ok": False, "error": f"candidate re-measurement failed: {after.get('error')}"}

    sp = _speedup(before, after)
    inference = _infer_speedup(before, after)
    accepted, reason = _accept_decision(sp, min_speedup, inference)
    return {
        "ok": True,
        "accepted": accepted,
        "reason": reason,
        "speedup": sp,
        "inference": inference,
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
