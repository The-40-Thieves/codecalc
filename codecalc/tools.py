"""Higher-order tools: cross-language comparison and empirical benchmarking.

benchmark() uses the "stdin-N contract": the submitted code must read an
integer N from stdin (first line) and do its work sized by N. codecalc runs it
at each requested size (min-of-3 to damp scheduler noise), then classifies the
empirical growth via doubling ratios + least-squares curve fitting.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable

from . import dependencies as dependencies_module
from . import errors, executor, registry

#: Shape every `on_progress` callback in this module takes: (done, total,
#: message), SYNCHRONOUS — these functions run inside `@mcp.tool`'s plain
#: `def`s, which the SDK already runs on a worker thread (see
#: server.py's own `_notify_resources_changed` comment for the same fact);
#: server.py's callback bridges to `ctx.report_progress` via
#: `anyio.from_thread.run(...)`, so nothing below this module needs to be
#: async or know the SDK exists at all.
ProgressFn = Callable[[int, int, str], None]


def compare_execution(snippets: dict[str, str], stdin: str = "", timeout: int = 15,
                      on_progress: ProgressFn | None = None) -> dict:
    """Run one code snippet per language; return a side-by-side result table.

    The OUTER `ok` is always `True` once the comparison itself ran to
    completion — it means "this tool produced a real table", not "every
    language succeeded", the same distinction the envelope's own `ok` draws
    for a single run (a program that exits non-zero is `ok: false` there too,
    but the REQUEST still succeeded). A language that failed is visible in
    its OWN row: `results[i]["ok"]` is that row's own success, and a row that
    failed for a REQUEST-level reason (no runtime installed, a timeout) also
    carries `error`/`code`/`remedy` — the same taxonomy `errors.py` uses
    everywhere else — via `errors.stamp_row`. An ordinary program failure
    (a real RTE/OLE `exit_code`) carries no `code`, matching the INTENDED
    convention that `code` means a failed request, not a failed program —
    the same convention `errors.ensure_code` applies to the top-level
    `execute_code` envelope itself (see its own docstring).

    No per-run dependency installs happen here (see the `compare_execution`
    MCP tool's own docstring for why) — but a python3 snippet carrying a PEP
    723 `# /// script` block is still detected and DISCLOSED, never silently
    dropped: only a REGEX presence check, deliberately not a full parse (a
    malformed block is exactly as inert here as a valid one, and this path
    has no reason to raise over it).

    `on_progress(done, total, message)`, if given, is called once PER
    LANGUAGE after that language's row is complete (`total` =
    `len(snippets)`, fixed up front) — monotone, 1..total, in the same
    order `snippets` iterates.
    """
    results = []
    total = len(snippets)
    for i, (language, code) in enumerate(snippets.items(), start=1):
        r = executor.execute(language, code, stdin=stdin, timeout=timeout)
        # a language can lose the wall-clock race to a globally slow
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
        # The executor's own `error` (a spawn failure — no runtime/compiler
        # installed) is carried through verbatim so a caller reading this
        # table does not have to re-run the language alone just to learn
        # WHY it failed. See `errors.stamp_row`'s docstring for the full
        # rule, including why a still-timed-out row is classified from a
        # message THIS function builds rather than from `stderr`.
        if r.get("error"):
            row["error"] = r["error"]
        errors.stamp_row(
            row, timed_out=row["timed_out"],
            timeout_message=f"{language} timed out after {timeout}s (wall-clock)",
        )
        if cold_retry:
            row["cold_retry_recovered"] = cold_retry_recovered
            row["first_attempt_ms"] = first_attempt_ms
        if (registry.canonical(language) == "python3"
                and dependencies_module.PEP723_REGEX.search(code)):
            row["dependencies"] = {
                "status": "unsupported",
                "reason": "this snippet carries a PEP 723 '# /// script' "
                          "block, but compare_execution never installs "
                          "dependencies (see this tool's own docstring); "
                          "the block was left unparsed and unhonoured",
            }
        results.append(row)
        if on_progress is not None:
            on_progress(i, total, f"ran {language} ({i}/{total})")
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

    # VANISHED OUTPUT (/ #42).
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

    # Classification half. A row still timed_out after the one warm
    # retry above is treated as authoritative here — it is ALWAYS flagged
    # (not only when a sibling produced output), and it carries the sibling
    # timing comparison that discriminates the two hypotheses:
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


#: `_fit_class` needs >=2 residual degrees of freedom (points minus the 2
#: fitted params, intercept and coefficient) before its ranking across the
#: 8-candidate family in `_CLASSES` means anything. At exactly 3 points (1 df)
#: the fit still RUNS — that is the GH #327 fix, `candidate_scores` is no
#: longer starved to `[]` at the documented minimum — but its "winner" is not
#: trustworthy: a 2-parameter line has enough freedom to land close to almost
#: any 3 noisy points for SEVERAL candidate shapes at once, so whichever shape
#: happens to graze closest is largely which shape the noise favoured, not
#: which one is true. Measured on the issue's own repro (49, 56, 107 ms @
#: 200k/400k/800k, a genuinely O(n) program): at 3 points the fit's #1 pick is
#: O(n^3) at relative_error 0.0038 -- a deceptively tight-looking fit for the
#: wrong answer -- while O(n) itself sits 24x worse in 5th place.
#: `_decide_estimate` below is what actually withholds a confident answer
#: until n_points clears this floor (or the ratio median clears its own,
#: `MIN_ROBUST_RATIOS`) -- AND, separately, until the fit's own quality signal
#: (`MAX_TRUSTED_RELATIVE_ERROR`, `INTERCEPT_NOISE_TOLERANCE_MS`) clears too.
MIN_FIT_POINTS = 4

#: A relative_error this high means the fit's own "best" candidate does not
#: actually describe the data — it only ranked ahead of seven worse options.
#: Cross-vendor review (Codex) of the #327 fix reproduced this on genuinely
#: EXPONENTIAL data with too few doubling ratios to use the median (see
#: `STRONG_EXPONENTIAL_RATIO` below): [10,20,40,80] / [1,10,200,5000] cleared
#: `MIN_FIT_POINTS` (4 points) and fell to the curve fit, whose 8 candidates
#: in `_CLASSES` do not include an exponential shape at all — every candidate
#: fit badly, but `_fit_class` still ranked them and handed back a "winner"
#: (O(n^3), relative_error 41.0 -- a 4100% average miss, worse than just
#: guessing the mean). 1.0 (100% average error) sits well above every genuine
#: fit measured during this fix (worst clean-signal winner seen: 0.0762 for a
#: 5-point O(n)/O(n log n) tie) and well below that failure's 41.0.
MAX_TRUSTED_RELATIVE_ERROR = 1.0

#: A fit's `intercept` models interpreter/subprocess startup overhead, which
#: cannot be negative — a small negative value is measurement noise (the same
#: ~5ms floor `benchmark()`'s own noise-floor check uses), but the same
#: exponential-data failure above put the winning candidate's intercept at
#: -162.4ms: nothing costs LESS than nothing to start up. A fit whose winner
#: needs a startup cost this impossible is not a fit, however low its
#: relative_error.
INTERCEPT_NOISE_TOLERANCE_MS = 5.0

#: A single doubling ratio this large is not ambiguous the way a ratio near a
#: polynomial boundary is: the largest polynomial candidate in `_CLASSES`
#: (O(n^3)) tops out around ratio 8 (2^3), and `_classify_by_ratio`'s own
#: `r < 11` boundary already treats anything above that as exponential. This
#: floor is set higher again, and — unlike every other path in
#: `_decide_estimate` — requires EVERY available ratio to agree, not just the
#: median, before it is allowed to decide with FEWER than `MIN_ROBUST_RATIOS`
#: ratios: the one deliberate exception to "too few samples, do not decide",
#: because a magnitude this large is unambiguous regardless of how few
#: samples produced it. Added after the same cross-vendor review: 2 ratios
#: ([22.11, 25.12], both genuinely exponential) is one short of
#: `MIN_ROBUST_RATIOS` and would otherwise have fallen all the way through to
#: the curve fit above, which has no exponential candidate to offer at all.
STRONG_EXPONENTIAL_RATIO = 12.0


def _fit_class(sizes: list[int], times_ms: list[float]) -> dict:
    """Least-squares fit of t = intercept + coefficient·f(n) (relative error).

    The additive `intercept` absorbs interpreter/subprocess startup directly
    inside the fit, so — unlike the multiplicative t = c·f(n) this replaced —
    NOTHING needs to be baseline-subtracted before calling this. That matters
    at exactly 3 sizes: the old multiplicative fit was fed `corrected` times
    (baseline = min(times) subtracted first), which forced the smallest
    size's corrected duration to exactly 0 and dropped it as `t > 0`-filtered
    noise, leaving only 2 points against this function's own `len(pts) >= 3`
    floor -- "insufficient data" at the one size count where this fit was the
    only fallback left (GH #327). Callers now pass RAW times; every requested
    size that produced a real measurement counts as a point.

    `coefficient < 0` (work shrinking as n grows) is rejected per candidate as
    physically nonsensical for a growth measurement — without that guard,
    fitting noise can hand a decreasing "best fit" to an unrelated shape.
    O(1) is exempt (its f(n) is a constant 1.0, so `var_f` is 0 and
    `coefficient` is pinned to 0 rather than solved for), which also
    guarantees `scores` is never empty once `len(pts) >= 3`.

    Each score's `c` field is the fit's `coefficient` — its PRE-#327 meaning,
    from the multiplicative model this replaced, where `c` was the only
    parameter (t = c·f(n)). A cross-vendor review of the #327 fix flagged an
    early draft that quietly repurposed `c` to mean the new `intercept`
    instead — a silent, undocumented meaning change on an existing field
    nothing else would have caught. `c` now keeps meaning "coefficient" so an
    existing consumer reading it is not silently misled; `intercept` and
    `coefficient` are the same two numbers under explicit, unambiguous names
    for anyone reading fresh. NEITHER number alone says whether a candidate
    is a GOOD fit — pair `relative_error` with `intercept`: a fit whose best
    candidate has a high `relative_error` or a deeply negative `intercept`
    (startup overhead cannot be negative) is not describing the data, only
    ranking ahead of seven worse options — `_decide_estimate` is what
    actually withholds trust from a fit like that.
    """
    pts = [(n, t) for n, t in zip(sizes, times_ms) if n > 0 and t >= 0]
    if len(pts) < 3:
        return {"estimate": "insufficient data (need >=3 sizes)", "scores": [], "n_points": len(pts)}

    n_pts = len(pts)
    mean_t = sum(t for _, t in pts) / n_pts
    scores = []
    for label, fn in _CLASSES.items():
        try:
            f = [fn(float(n)) for n, _ in pts]
        except Exception:
            continue
        if any(v != v or math.isinf(v) for v in f):  # NaN/inf candidate values
            continue
        mean_f = sum(f) / n_pts
        var_f = sum((fv - mean_f) ** 2 for fv in f)
        if var_f > 0:
            cov_ft = sum((fv - mean_f) * (t - mean_t) for (_, t), fv in zip(pts, f))
            coefficient = cov_ft / var_f
            intercept = mean_t - coefficient * mean_f
        else:
            coefficient, intercept = 0.0, mean_t  # O(1): f is constant, nothing to regress on
        if coefficient < 0:
            continue
        rel_err = sum(abs(t - (intercept + coefficient * fv)) / max(t, 1e-3)
                      for (_, t), fv in zip(pts, f)) / n_pts
        scores.append({"class": label, "relative_error": round(rel_err, 4),
                       "c": round(coefficient, 6), "coefficient": round(coefficient, 6),
                       "intercept": round(intercept, 4)})
    scores.sort(key=lambda s: s["relative_error"])
    best = scores[0] if scores else None
    return {
        "estimate": best["class"] if best else "unknown",
        "best_score": best,
        "scores": scores,
        "n_points": n_pts,
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
    three separate times, because dividing by a small denominator can
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


#: `estimate_basis` values `benchmark()` can report. Pure data, not code, but
#: kept next to `_decide_estimate` (the one function that produces them) so
#: the two stay in sync — a caller can `in` this without importing `tools`'s
#: whole module-level surface.
ESTIMATE_BASES = ("ratio-median", "curve-fit", "noise-floor", "inconclusive")


def _decide_estimate(ratios: list[float], fit: dict) -> tuple[str, str]:
    """Pick the growth-class estimate and say which estimator actually produced it.

    This is the exact decision GH #327 found broken: the old
    `_classify_by_ratio(ratios) if ratios else fit["estimate"]` trusted the
    ratio median the moment `ratios` was non-empty, with no floor on ITS
    length either — one doubling ratio (no median at all) outranked a fit
    that, at 3 sizes, could not even run (see `_fit_class`'s baseline-
    subtraction note). Two floors now gate the two estimators independently:
    `MIN_ROBUST_RATIOS` for the ratio median, `MIN_FIT_POINTS` plus a fit
    QUALITY check (`MAX_TRUSTED_RELATIVE_ERROR`/`INTERCEPT_NOISE_TOLERANCE_MS`)
    for the curve fit. When NEITHER clears its floor, that is said plainly in
    `estimate` itself — not only in the advisory `ratio_confidence` side
    field, which is what let a caller read `estimate` + `method: "empirical"`
    and reasonably believe a number nobody actually established.

    One deliberate exception sits ABOVE both floors: `STRONG_EXPONENTIAL_RATIO`.
    `_CLASSES` has no exponential candidate (the curve fit cannot ever name
    O(c^n)), so genuinely exponential data with too few ratios for a robust
    median used to fall all the way through to the curve fit and come back as
    a confident polynomial — cross-vendor review reproduced this on
    [10,20,40,80]/[1,10,200,5000] (2 ratios, both >20x, curve-fit "O(n^3)" at
    relative_error 41.0). A ratio magnitude this large, agreed on by EVERY
    available ratio, is not the kind of ambiguity `MIN_ROBUST_RATIOS` exists
    to guard against, so it is allowed to decide on its own.
    """
    if ratios and all(r >= STRONG_EXPONENTIAL_RATIO for r in ratios):
        return "O(c^n) (exponential or worse)", "ratio-median"

    if len(ratios) >= MIN_ROBUST_RATIOS:
        return _classify_by_ratio(ratios), "ratio-median"

    best = fit.get("best_score")
    n_pts = fit.get("n_points", 0)
    fit_is_trustworthy = (
        best is not None
        and n_pts >= MIN_FIT_POINTS
        and best["relative_error"] <= MAX_TRUSTED_RELATIVE_ERROR
        and best["intercept"] >= -INTERCEPT_NOISE_TOLERANCE_MS
    )
    if fit_is_trustworthy:
        return fit["estimate"], "curve-fit"

    reasons = [f"{len(ratios)} doubling ratio(s) (<{MIN_ROBUST_RATIOS} needed for a robust median)"]
    if not fit.get("scores"):
        reasons.append("the curve fit found no viable candidate class")
    elif n_pts < MIN_FIT_POINTS:
        reasons.append(
            f"the curve fit only had {n_pts} usable point(s) (<{MIN_FIT_POINTS} needed for a "
            f"2-parameter fit to discriminate reliably across {len(_CLASSES)} candidate classes)")
    else:
        reasons.append(
            f"the curve fit's best candidate ({fit['estimate']}) does not actually describe the "
            f"data (relative_error={best['relative_error']}, intercept={best['intercept']}ms) — "
            f"none of the {len(_CLASSES)} candidate shapes fit")
    return "inconclusive (" + "; ".join(reasons) + ")", "inconclusive"


def _measure(language: str, code: str, sizes: list[int], timeout: int, repeats: int,
            deadline: float | None = None,
            on_progress: ProgressFn | None = None) -> tuple[list[dict], dict | None]:
    """Run the program at each size (min-of-repeats). Returns (runs, error).

    `deadline` (an absolute `time.monotonic()` timestamp; optional, backward
    compatible — every existing caller passes none) is checked BEFORE each
    size's block of `repeats` executions, not before each individual
    execution: a per-execution `timeout` already bounds any single run, so
    the risk this closes is specifically STARTING a whole size's worth of
    work (`repeats` executions) once there is no longer enough shared budget
    left to plausibly finish it, not interrupting one already in flight.
    codecalc/optimization.py's `verify_optimization` threads one shared
    deadline through every `_measure` call it makes (both `_timed`
    invocations AND `_align_sizes`'s re-measurement) so the WHOLE
    measurement phase — not each call in isolation — shares one clock; see
    `optimization._MEASUREMENT_BUDGET_S`'s docstring for why a per-call-only
    budget left a gap (a genuinely slow baseline's OWN rescale rounds, with
    no alignment involved at all, had no equivalent backstop).

    `on_progress`, if given, fires once PER SIZE this call actually finishes
    measuring (`total` = `len(sizes)` for THIS call) — `benchmark()` below is
    the only caller that passes one, and only for its first (non-rescaled)
    pass; `optimization.py`'s callers never pass one, so this stays a no-op
    for every other caller by default.
    """
    runs = []
    total = len(sizes)
    for i, n in enumerate(sizes, start=1):
        if deadline is not None and time.monotonic() >= deadline:
            return runs, {"ok": False,
                          "error": f"measurement deadline exceeded before n={n} could run"}
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
            # The underlying executor result's OWN `error` (e.g. a spawn
            # failure: "runtime unavailable for the ... phase: ...") is
            # appended rather than dropped — server.py's `_coded` wrapper
            # classifies THIS function's return value via `errors.
            # ensure_code`, which matches on message text, so a generic
            # "program failed at n=100" with nothing else used to classify
            # every such failure `internal` regardless of the real cause.
            # `detail` (the raw `last` result) already carried the real
            # reason for a caller willing to dig for it; this puts the same
            # substring where the automatic classifier actually looks.
            detail_error = last.get("error")
            message = f"program failed at n={n}"
            if detail_error:
                message = f"{message}: {detail_error}"
            return runs, {"ok": False, "error": message, "detail": last}
        runs.append({
            "n": n,
            "ok": bool(durations),
            "duration_ms": min(durations) if durations else None,
            "all_runs_ms": durations,
            "stdout": (last or {}).get("stdout", "")[:200],
            "stderr": ((last or {}).get("stderr") or "")[:200],
        })
        if on_progress is not None:
            on_progress(i, total, f"measured n={n} ({i}/{total})")
    return runs, None


def benchmark(code: str, language: str = "python3", sizes: str = "100,1000,10000,100000",
              timeout: int = 30, repeats: int = 3,
              on_progress: ProgressFn | None = None) -> dict:
    """Empirically measure complexity.

    Contract: `code` must read an integer N from stdin (first line) and perform
    work sized by N. Runs it `repeats` times per size (min used — robust to
    scheduler noise), then classifies growth via doubling ratios + curve fit.

    Sizes auto-scale: if the measured work is below ~20ms at the largest size,
    sizes are multiplied by 10 and re-measured (up to 4x) so the fit sees real
    compute, not subprocess spawn noise.

    3 is the accepted MINIMUM (unchanged, for backward compatibility — see
    `MIN_FIT_POINTS`/`MIN_ROBUST_RATIOS`'s comments for the GH #327 history of
    why), but 3 sizes can NEVER decide a polynomial/log-family growth class:
    a 2-parameter curve fit against 3 points has only 1 residual degree of
    freedom, not enough to discriminate reliably across the 8 candidate
    classes in `_CLASSES` (`estimate_basis` never reaches `"curve-fit"`), and
    3 sizes can produce at most 1 doubling ratio, never the >= 3
    `MIN_ROBUST_RATIOS` needs for a median. What 3 sizes CAN return:
    `"O(1) (work below noise floor...)"` (`estimate_basis: "noise-floor"`,
    when the work is flat enough); `"O(c^n) (exponential or worse)"`
    (`estimate_basis: "ratio-median"`, ONLY when that lone ratio is itself
    extreme — see `STRONG_EXPONENTIAL_RATIO`, a deliberate exception because
    that magnitude is unambiguous regardless of sample size); or, for
    everything else, `"inconclusive (...)"`. At least 4 sizes are needed
    before the curve fit is actually trusted (`estimate_basis: "curve-fit"`);
    this module's own default `sizes` ("100,1000,10000,100000") is 4 sizes
    10x apart — no doubling pairs at all, so it always decides via curve-fit,
    never the ratio median. At least 5 DOUBLING sizes (n, 2n, 4n, 8n, 16n)
    are needed for a robust ratio median (`estimate_basis: "ratio-median"`
    via the general path, not the exponential exception above): N doubling
    sizes yield N-2 usable ratios (baseline subtraction always discards the
    first gap as sub-noise — see `_classify_by_ratio`'s docstring), so 5
    sizes is the floor for the 3 ratios `MIN_ROBUST_RATIOS` requires.

    `on_progress(done, total, message)`, if given, fires once PER SIZE
    (`total` = the requested size count, fixed) during the FIRST measurement
    pass only — an auto-scale re-measurement is a distinct, unpredictable-
    length retry phase (0 to 4 extra passes over the SAME `total` sizes), and
    firing a fresh 1..total sequence for each one would no longer be
    monotone across the whole call.
    """
    try:
        size_list = [int(s.strip()) for s in sizes.split(",") if s.strip()]
    except ValueError:
        return {"ok": False, "error": "sizes must be comma-separated integers"}
    if len(size_list) < 3:
        return {"ok": False, "error": "need at least 3 sizes (3 is accepted but can only "
                                      "return noise-floor O(1), an unambiguous O(c^n) "
                                      "override, or an inconclusive estimate — never any "
                                      "other growth class; 4+ sizes for a trusted curve fit, "
                                      "5+ doubling sizes for a robust ratio median; see "
                                      "benchmark's own docstring)"}
    repeats = max(1, min(repeats, 5))

    runs, error = _measure(language, code, size_list, timeout, repeats, on_progress=on_progress)
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
            # Neither estimator below ran: this is a third, earlier verdict,
            # not a stand-in for "curve-fit" or "ratio-median" (GH #327 wants
            # a caller able to tell which one produced the answer, and here
            # it is neither — the noise floor decided it before either ran).
            "estimate_basis": "noise-floor",
            "auto_scaled": scale_steps > 0,
            "candidate_scores": [],
            "doubling_ratios": [],
            "runs": runs,
        }

    # `_fit_class` gets the RAW times, not `corrected`: its additive-constant
    # model (t = intercept + coefficient·f(n)) fits its own startup offset, so
    # nothing needs zeroing out first. Feeding it `corrected` here is exactly the GH #327
    # bug — baseline subtraction forces `corrected[0]` to 0, which used to
    # cost the fit the third point it needed at the 3-size floor. `corrected`
    # stays reserved for the doubling-ratio computation below, which has
    # always deliberately discarded that same first gap as sub-noise.
    fit = _fit_class(sizes_n, times)
    ratios = []
    for (na, ta), (nb, tb) in zip(zip(sizes_n, corrected), zip(sizes_n[1:], corrected[1:])):
        if nb == 2 * na and ta > 3.0:  # ignore ratios from sub-noise baselines
            ratios.append(round(tb / ta, 2))
    estimate, estimate_basis = _decide_estimate(ratios, fit)

    return {
        "ok": True,
        "language": language,
        # MEASURED, not inferred. `analyze_complexity` returns
        # method="static-estimate" for the same question read off the source;
        # this one ran the program at increasing sizes and timed it. The two
        # sit next to each other in the tool list and a caller relaying either
        # as "the complexity" without saying which has lost the distinction
        # that makes one of them evidence. `method` staying "empirical" even
        # on an "inconclusive" `estimate` is deliberate, not a contradiction:
        # the MEASUREMENT still happened and is still real evidence, it is
        # only the CLASSIFICATION that came back honest instead of confident.
        "method": "empirical",
        "estimate": estimate,
        # Which estimator actually produced `estimate` — see
        # `_decide_estimate`'s docstring. A caller that only reads `estimate`
        # + `method` has no way to tell a robust ratio median apart from a
        # single noisy doubling ratio; this field is that distinction, made
        # explicit instead of buried in `ratio_confidence` (which only ever
        # spoke to the ratio path, and stayed silent when the curve fit or
        # neither estimator was the one deciding).
        "estimate_basis": estimate_basis,
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
