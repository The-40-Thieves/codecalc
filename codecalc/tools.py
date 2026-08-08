"""Higher-order tools: cross-language comparison and empirical benchmarking.

benchmark() uses the "stdin-N contract": the submitted code must read an
integer N from stdin (first line) and do its work sized by N. codecalc runs it
at each requested size (min-of-3 to damp scheduler noise), then classifies the
empirical growth via doubling ratios + least-squares curve fitting.
"""

from __future__ import annotations

import math
import statistics

from . import executor


def compare_execution(snippets: dict[str, str], stdin: str = "", timeout: int = 15) -> dict:
    """Run one code snippet per language; return a side-by-side result table."""
    results = []
    for language, code in snippets.items():
        r = executor.execute(language, code, stdin=stdin, timeout=timeout)
        results.append({
            "language": language,
            "ok": r.get("ok"),
            "stdout": r.get("stdout", ""),
            "stderr": (r.get("stderr") or "")[:500],
            "exit_code": r.get("exit_code"),
            "duration_ms": r.get("duration_ms"),
            "timed_out": r.get("timed_out", False),
        })
    # `fastest` must mean the fastest run that WORKED. It used to be the minimum
    # duration over all results, so a language that failed instantly won: perl
    # dying in 25ms beat a working python3 at 344ms, and the tool's headline
    # field named a program that never ran.
    #
    # `x["duration_ms"] or 1e12` was a second bug in the same line — a genuine
    # 0ms run is falsy, so the fastest possible result was treated as the
    # slowest. Compare against None explicitly.
    ok_runs = [r for r in results if r["ok"] and r["duration_ms"] is not None]
    fastest = min(ok_runs, key=lambda x: x["duration_ms"])["language"] if ok_runs else None
    return {
        "ok": True,
        "count": len(results),
        "succeeded": len(ok_runs),
        "results": results,
        "fastest": fastest,
        "fastest_note": None if ok_runs else "no language ran successfully",
    }


#: candidate complexity classes as pure functions of n (NO eval — the old
#: version eval'd format strings; these are plain lambdas instead)
_CLASSES: dict[str, object] = {
    "O(1)": lambda n: 1.0,
    "O(log n)": lambda n: math.log(max(n, 2.0)),
    "O(sqrt n)": lambda n: math.sqrt(n),
    "O(n)": lambda n: n,
    "O(n log n)": lambda n: n * math.log(max(n, 2.0)),
    "O(n^2)": lambda n: n ** 2,
    "O(n^2 log n)": lambda n: n ** 2 * math.log(max(n, 2.0)),
    "O(n^3)": lambda n: n ** 3,
}


def _fit_class(sizes: list[int], times_ms: list[float]) -> dict:
    """Least-squares fit of t = c·f(n) over candidate classes (relative error)."""
    pts = [(n, max(t, 1e-3)) for n, t in zip(sizes, times_ms) if n > 0 and t > 0]
    if len(pts) < 3:
        return {"estimate": "insufficient data (need >=3 sizes)", "scores": []}

    scores = []
    for label, fn in _CLASSES.items():
        try:
            f = [fn(float(n)) for n, _ in pts]
        except Exception:
            continue
        if any(v <= 0 for v in f):
            continue
        num = sum(t * fv for (_, t), fv in zip(pts, f))
        den = sum(fv * fv for fv in f)
        if den == 0:
            continue
        c = num / den
        rel_err = sum(abs(t - c * fv) / t for (_, t), fv in zip(pts, f)) / len(pts)
        scores.append({"class": label, "relative_error": round(rel_err, 4), "c": round(c, 4)})
    scores.sort(key=lambda s: s["relative_error"])
    best = scores[0] if scores else None
    return {
        "estimate": best["class"] if best else "unknown",
        "best_score": best,
        "scores": scores,
    }


def _classify_by_ratio(ratios: list[float]) -> str:
    """Classify by doubling ratio: 1≈O(1), 2≈O(n), 2-3≈O(n log n), 4≈O(n²), 8≈O(n³)."""
    if not ratios:
        return "unknown"
    r = statistics.median(ratios)
    if r < 1.5:
        return "O(1)"
    if r < 2.5:
        return "O(n)"
    if r < 3.2:
        return "O(n log n)"
    if r < 5.5:
        return "O(n^2)"
    if r < 11:
        return "O(n^3)"
    return "O(c^n) (exponential or worse)"


def _measure(language: str, code: str, sizes: list[int], timeout: int, repeats: int) -> tuple[list[dict], dict | None]:
    """Run the program at each size (min-of-repeats). Returns (runs, error)."""
    runs = []
    for n in sizes:
        durations = []
        last = None
        timed_out = False
        for _ in range(repeats):
            r = executor.execute(language, code, stdin=f"{n}\n", timeout=timeout)
            last = r
            if r.get("timed_out"):
                # A timeout is NOT a timing. The old guard was
                # `if not ok and not timed_out: break`, so a timed-out run fell
                # through and its duration — the timeout wall clock — was
                # appended as data. The curve fit was then computed against the
                # timeout value and reported as a complexity class.
                timed_out = True
                break
            if not r.get("ok"):
                break
            d = r.get("duration_ms")
            if d is not None:
                durations.append(d)
        if timed_out:
            return runs, {"ok": False,
                          "error": f"program timed out at n={n} ({timeout}s); "
                                   "no growth estimate is possible from a timeout",
                          "detail": last}
        if last is not None and not last.get("ok"):
            return runs, {"ok": False, "error": f"program failed at n={n}", "detail": last}
        runs.append({
            "n": n,
            "ok": bool(durations),
            "duration_ms": min(durations) if durations else None,
            "all_runs_ms": durations,
            "stdout": (last or {}).get("stdout", "")[:200],
            "stderr": ((last or {}).get("stderr") or "")[:200],
        })
    return runs, None


def benchmark(code: str, language: str = "python3", sizes: str = "100,1000,10000,100000",
              timeout: int = 30, repeats: int = 3) -> dict:
    """Empirically measure complexity.

    Contract: `code` must read an integer N from stdin (first line) and perform
    work sized by N. Runs it `repeats` times per size (min used — robust to
    scheduler noise), then classifies growth via doubling ratios + curve fit.

    Sizes auto-scale: if the measured work is below ~20ms at the largest size,
    sizes are multiplied by 10 and re-measured (up to 4x) so the fit sees real
    compute, not subprocess spawn noise.
    """
    try:
        size_list = [int(s.strip()) for s in sizes.split(",") if s.strip()]
    except ValueError:
        return {"ok": False, "error": "sizes must be comma-separated integers"}
    if len(size_list) < 3:
        return {"ok": False, "error": "need at least 3 sizes for a meaningful fit"}
    repeats = max(1, min(repeats, 5))

    runs, error = _measure(language, code, size_list, timeout, repeats)
    if error:
        error["runs"] = runs
        return error

    # auto-scale: work too small to measure? grow sizes and re-measure.
    # Scale until the spread (work signal) clears the subprocess-noise floor
    # (~280ms interpreter boot + scheduler jitter) or we hit the size cap.
    # `is not None`, not truthiness: a 0ms duration is a real measurement and
    # was being dropped from every one of these filters.
    measured = [r["duration_ms"] for r in runs if r.get("duration_ms") is not None]
    scale_steps = 0
    while measured and (max(measured) - min(measured)) < 50.0 and scale_steps < 5:
        if max(size_list) >= 100_000_000:
            break  # don't let an O(1)/log program balloon into a timeout
        size_list = [n * 10 for n in size_list]
        runs, error = _measure(language, code, size_list, timeout, repeats)
        if error:
            error["runs"] = runs
            return error
        measured = [r["duration_ms"] for r in runs if r.get("duration_ms") is not None]
        scale_steps += 1

    valid = [r for r in runs if r.get("duration_ms") is not None]
    if len(valid) < 3:
        return {"ok": False, "error": "too few successful runs (timeouts or failures)", "runs": runs}

    sizes_n = [r["n"] for r in valid]
    times = [r["duration_ms"] for r in valid]

    # Baseline subtraction (interpreter startup ~300ms masquerades as O(1)).
    baseline = min(times)
    corrected = [max(t - baseline, 0.0) for t in times]

    # If even the largest corrected time is below the measurement noise floor,
    # the work is effectively constant regardless of n -> O(1).
    if max(corrected) < 5.0:
        return {
            "ok": True,
            "language": language,
            "estimate": "O(1) (work below noise floor at all sizes)",
            "auto_scaled": scale_steps > 0,
            "candidate_scores": [],
            "doubling_ratios": [],
            "runs": runs,
        }

    fit = _fit_class(sizes_n, corrected)
    ratios = []
    for (na, ta), (nb, tb) in zip(zip(sizes_n, corrected), zip(sizes_n[1:], corrected[1:])):
        if nb == 2 * na and ta > 3.0:  # ignore ratios from sub-noise baselines
            ratios.append(round(tb / ta, 2))
    estimate = _classify_by_ratio(ratios) if ratios else fit["estimate"]

    return {
        "ok": True,
        "language": language,
        "estimate": estimate,
        "best_score": fit.get("best_score"),
        "candidate_scores": fit.get("scores", []),
        "doubling_ratios": ratios,
        "baseline_ms": round(baseline, 2),
        "auto_scaled": scale_steps > 0,
        "runs": runs,
    }
