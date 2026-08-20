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
        # THE-802: a language can lose the wall-clock race to a globally slow
        # runner rather than to a defect in its own snippet — one repro had
        # ruby at 3452ms against python's 33ms for the same trivial script, a
        # 100x spread, with node (the heaviest cold-starter) first to cross
        # the fixed timeout. executor.execute's timeout stays a hard limit
        # (that boundary is load-bearing elsewhere); what changes here is
        # that ONE cold-start timeout no longer ends the story for a
        # language. Exactly one retry, only on timed_out — a deterministic
        # failure (compile error, non-zero exit) gets no retry, since
        # retrying it wastes 2x wall time and cannot change the answer.
        cold_retry = False
        cold_retry_recovered = False
        first_attempt_ms = None
        if r.get("timed_out"):
            cold_retry = True
            first_attempt_ms = r.get("duration_ms")
            retry = executor.execute(language, code, stdin=stdin, timeout=timeout)
            cold_retry_recovered = not retry.get("timed_out")
            r = retry
        row = {
            "language": language,
            "ok": r.get("ok"),
            "stdout": r.get("stdout", ""),
            "stderr": (r.get("stderr") or "")[:500],
            "exit_code": r.get("exit_code"),
            "duration_ms": r.get("duration_ms"),
            "timed_out": r.get("timed_out", False),
            "cold_retry": cold_retry,
        }
        if cold_retry:
            row["cold_retry_recovered"] = cold_retry_recovered
            row["first_attempt_ms"] = first_attempt_ms
        results.append(row)
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

    # VANISHED OUTPUT (THE-802 / #42).
    #
    # translation.py already refuses to score an empty-but-ok result as
    # evidence, and `vanished_output_side` there names the reason: an exit-0 run
    # with no stdout cannot be distinguished from one whose output was LOST.
    # `node` does exactly this on windows-latest — ok with empty stdout while
    # sibling languages print normally from the same snippet.
    #
    # That reasoning was applied to the translation gate and never to this tool,
    # which is the more dangerous of the two. Here the caller has asked to run
    # ONE computation several ways, so the siblings are a control the tool
    # already has and was throwing away: if three languages printed and one
    # printed nothing, the odd one out is a discrepancy, not a result.
    #
    # This does not guess a cause and does not fail the call. It surfaces the
    # disagreement, because the failure mode being guarded is a caller reading
    # `fastest` and a table of outputs and not noticing that one row is empty —
    # and a node-only snippet, with no sibling to disagree with it, is a
    # silently wrong answer rather than a red check.
    produced = [r for r in results if (r["stdout"] or "").strip()]
    silent = [r for r in results if not (r["stdout"] or "").strip()]
    discrepancies = []
    flagged_languages: set[str] = set()

    # THE-802, classification half. A row still timed_out after the one warm
    # retry above is treated as authoritative here — it is ALWAYS flagged
    # (not only when a sibling produced output), and it carries the sibling
    # timing comparison that discriminates the ticket's two hypotheses:
    # (1) this language's snippet is actually slow/broken, vs. (2) the whole
    # runner was under cold-start pressure and everything was slow. A row
    # entered here must not be re-added by the produced/silent loop below.
    ok_siblings_by_lang = {
        r["language"]: r["duration_ms"] for r in results
        if r["ok"] and isinstance(r["duration_ms"], (int, float))
    }
    for r in results:
        if not r["timed_out"]:
            continue
        siblings = {lang: ms for lang, ms in ok_siblings_by_lang.items()
                    if lang != r["language"]}
        if siblings:
            hi_lang = max(siblings, key=lambda k: siblings[k])
            lo_lang = min(siblings, key=lambda k: siblings[k])
            hi, lo = siblings[hi_lang], siblings[lo_lang]
            # Two independent signals that the RUNNER, not this language, was
            # the cause: (1) a high spread between siblings, and (2) a sibling
            # that itself ate a large fraction of the same wall-clock ceiling
            # this row hit. Signal (2) is what a lone ok sibling needs — with
            # one sibling the spread ratio is always 1.0 and would otherwise
            # always read "specific", even when that single sibling was
            # plainly slow. It also subsumes the lo==0 divide-by-zero cases:
            # a 0ms sibling can never look slow, and a genuinely slow one
            # trips signal (2) on its absolute time, not the ratio.
            timeout_ms = timeout * 1000
            high_variance = lo > 0 and hi / lo >= 10.0
            slow_sibling = hi >= 0.25 * timeout_ms
            if high_variance or slow_sibling:
                reasons = []
                if high_variance:
                    reasons.append(
                        f"high sibling variance ({hi / lo:.1f}x: slowest "
                        f"{hi_lang} {hi}ms vs fastest {lo_lang} {lo}ms)")
                if slow_sibling:
                    reasons.append(
                        f"slowest sibling {hi_lang} took {hi}ms "
                        f"({hi / timeout_ms * 100:.0f}% of the {timeout_ms}ms "
                        f"ceiling)")
                variance_note = (
                    "; ".join(reasons) + " — consistent with runner-wide "
                    "slowness/cold-start pressure rather than a defect "
                    f"specific to {r['language']}")
            else:
                span = (f"{hi / lo:.1f}x spread" if lo > 0
                        else f"slowest {hi_lang} {hi}ms")
                variance_note = (
                    f"siblings ran fast ({span}) — this timeout looks "
                    f"specific to {r['language']}")
        else:
            variance_note = "no successful sibling to compare timings against"
        discrepancies.append({
            "language": r["language"],
            "ok": False,
            "timed_out": True,
            "issue": "timed out and stayed timed out after one warm retry",
            "sibling_durations_ms": siblings,
            "variance_note": variance_note,
            "detail": ("a warm retry was attempted and also timed out; see "
                       "sibling_durations_ms/variance_note"),
        })
        flagged_languages.add(r["language"])

    if produced and silent:
        for r in silent:
            if r["language"] in flagged_languages:
                continue
            discrepancies.append({
                "language": r["language"],
                "issue": "no stdout while another language produced some",
                # `ok` is carried because the two cases need different
                # responses: ok=False is a run that failed and said so, ok=True
                # with no output is the one that can be mistaken for an answer.
                "ok": r["ok"],
                "exit_code": r["exit_code"],
                "timed_out": r["timed_out"],
                "detail": ("this run reported success and produced nothing, "
                           "which is indistinguishable from output that was "
                           "lost — do not read it as an empty result"
                           if r["ok"] else
                           "this run failed; its stderr says why"),
            })

    return {
        "ok": True,
        "count": len(results),
        "succeeded": len(ok_runs),
        "results": results,
        "fastest": fastest,
        "fastest_note": None if ok_runs else "no language ran successfully",
        # Always present, empty when there is nothing to disclose — the same
        # rule `unenforced` follows. A field that appears only on trouble is a
        # field a caller forgets to read.
        "discrepancies": discrepancies,
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


#: Below this many ratios, `statistics.median` cannot reject an outlier — with
#: two values it IS the mean. Callers are told rather than silently given a
#: number the docstring implies is robust. See `ratio_confidence` in the result.
MIN_ROBUST_RATIOS = 3


def _classify_by_ratio(ratios: list[float]) -> str:
    """Classify by doubling ratio: 1≈O(1), 2≈O(n), 2-3≈O(n log n), 4≈O(n²), 8≈O(n³).

    The median is what makes this robust to a single slow run — but only once
    there are at least three ratios to take a median OF. Note that baseline
    subtraction in `benchmark()` forces `corrected[0]` to 0, so the first gap is
    always discarded: N doubling sizes yield N-2 usable ratios, not N-1. Four
    sizes therefore give two, and a median of two is a mean.

    That is not hypothetical. On a shared macOS runner under sustained load, one
    4x-slow largest measurement moved this from O(n^2) to O(c^n) — two classes —
    three separate times (THE-808), because dividing by a small denominator can
    only ever inflate. `ratio_confidence` in the result now says when the median
    is carrying less weight than it appears to.
    """
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
            "method": "empirical",
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
        # MEASURED, not inferred. `analyze_complexity` returns
        # method="static-estimate" for the same question read off the source;
        # this one ran the program at increasing sizes and timed it. The two
        # sit next to each other in the tool list and a caller relaying either
        # as "the complexity" without saying which has lost the distinction
        # that makes one of them evidence.
        "method": "empirical",
        "estimate": estimate,
        "best_score": fit.get("best_score"),
        "candidate_scores": fit.get("scores", []),
        "doubling_ratios": ratios,
        # How much the median is actually doing. Two ratios make it a mean, so
        # a single slow run moves the answer with nothing to reject it against —
        # measured at two whole classes (O(n^2) -> O(c^n)) on a loaded runner.
        # Always present, like `unenforced`: a field that appears only on
        # trouble is a field a caller forgets to read.
        #
        # Note the arithmetic that surprises people: baseline subtraction forces
        # corrected[0] to 0, so the first gap is always discarded and N doubling
        # sizes give N-2 usable ratios. Five sizes is the floor for a real
        # median, not four.
        "ratio_confidence": (
            "robust" if len(ratios) >= MIN_ROBUST_RATIOS else
            f"low ({len(ratios)} ratio(s); a median needs >= {MIN_ROBUST_RATIOS} "
            f"to reject an outlier, which needs >= {MIN_ROBUST_RATIOS + 2} "
            f"doubling sizes because the first gap is always discarded)"
        ),
        "baseline_ms": round(baseline, 2),
        "auto_scaled": scale_steps > 0,
        "runs": runs,
    }
