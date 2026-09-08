"""Verification-gate tests for verify_translation / verify_optimization.

The bug these exist for: verify_translation() treated "both programs failed" as
a MATCH ("same behavior class"). That made the gate report success for a
translation with no relationship to the source — and it did so most reliably in
the case where verification is least meaningful, namely when the source
program's runtime is missing so it cannot run at all.

    verify_translation('python3', 'import sys; sys.exit(1)',
                       'node',    'process.exit(1)', ['', '0', '1'])
    ->  passed: True, matched 3/3

These tests drive `classify_case`, a PURE function over two executor result
dicts. That split is deliberate: the old logic could only be exercised by
actually running two programs in the sandbox, so it needed a built binary and
two working runtimes, and it was therefore never tested at all. A trust
boundary that can only be checked where the toolchain is installed is not one
that gets checked.
"""

from __future__ import annotations

import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import errors, executor, grades, optimization, stats, translation
from codecalc import server as _server
from codecalc.translation import aggregate, classify_case, compare_edge_cases

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def ok(stdout="", **kw):
    return {"ok": True, "phase": "run", "verdict": "OK", "stdout": stdout,
            "stderr": "", "exit_code": 0, **kw}


def rte(stderr="boom", stdout="", **kw):
    return {"ok": False, "phase": "run", "verdict": "RTE", "stdout": stdout,
            "stderr": stderr, "exit_code": 1, **kw}


def compile_error(stderr="syntax error", **kw):
    return {"ok": False, "phase": "compile", "verdict": "RTE", "stdout": "",
            "stderr": stderr, "exit_code": 1, **kw}


def tle(**kw):
    return {"ok": False, "phase": "run", "verdict": "TLE", "stdout": "",
            "stderr": "<killed>", "exit_code": None, "timed_out": True, **kw}


# ── the regression: both-failed must never be positive evidence ─────────────
outcome, _ = classify_case(rte("ZeroDivisionError"), rte("panic: nil map"))
check("both failed -> INCONCLUSIVE, not match", outcome == "inconclusive",
      f"-> {outcome}")

outcome, _ = classify_case(rte(), tle())
check("both failed, different verdicts -> inconclusive", outcome == "inconclusive",
      f"-> {outcome}")

# ── a translation that does not compile is WRONG, never 'equivalent' ────────
outcome, _ = classify_case(rte(), compile_error())
check("target compile error -> MISMATCH even when source failed",
      outcome == "mismatch", f"-> {outcome}")
outcome, _ = classify_case(ok("7"), compile_error())
check("target compile error vs working source -> mismatch", outcome == "mismatch")

# ── a broken SOURCE means we cannot verify, not that we verified ────────────
outcome, _ = classify_case(compile_error(), ok("7"))
check("source compile error -> inconclusive", outcome == "inconclusive")

# ── the empty-output guard (#42): agreeing on NOTHING is not a match ────────
# Observed on windows-latest: compare_execution's harness returned an empty
# stdout for `node` on a snippet that was supposed to print, while sibling
# languages produced the real answer from the same code. That surfaced only
# because a sibling disagreed; the dangerous case is when nothing disagrees
# because BOTH sides silently lost their output the same way. Two ok=True,
# stdout="" results must not read as equivalence any more than two RTEs do.
outcome, reason = classify_case(ok(""), ok(""))
check("both ok but BOTH produced no output -> inconclusive, not match",
      outcome == "inconclusive", f"-> {outcome} {reason!r}")
check("  ...and says why", "no output" in reason.lower(), f"-> {reason!r}")

outcome, _ = classify_case(ok("42"), ok(""))
check("one side produced output, the other silently did not -> mismatch",
      outcome == "mismatch", f"-> {outcome}")
outcome, _ = classify_case(ok(""), ok("42"))
check("  ...regardless of which side is the empty one -> still mismatch",
      outcome == "mismatch", f"-> {outcome}")

# the exact bug-report shape: one language ok=False/empty while siblings ok
outcome, _ = classify_case(ok("42"), rte(stdout=""))
check("a sibling that ran fine vs one that failed empty -> mismatch, not scored a winner",
      outcome == "mismatch", f"-> {outcome}")

# end to end: a whole verification run that only ever agreed on emptiness
# must not pass either, for the same reason an all-inconclusive run doesn't.
r = aggregate([classify_case(ok(""), ok("")) for _ in range(3)])
check("an entire run of empty-vs-empty 'agreement' is NOT a pass",
      r["passed"] is False, f"-> {r['reason']}")

# ── the ordinary cases still behave ─────────────────────────────────────────
outcome, _ = classify_case(ok("42\n"), ok("42"))
check("both ok, same stdout (trailing ws normalised) -> match", outcome == "match")
outcome, _ = classify_case(ok("42"), ok("43"))
check("both ok, different stdout -> mismatch", outcome == "mismatch")
outcome, _ = classify_case(ok("42"), rte())
check("source ok, target failed -> mismatch", outcome == "mismatch")
outcome, _ = classify_case(rte(), ok("42"))
check("source failed, target ok -> mismatch", outcome == "mismatch")

# ── aggregation: inconclusive alone can never pass ──────────────────────────
r = aggregate([("inconclusive", "x"), ("inconclusive", "x"), ("inconclusive", "x")])
check("all inconclusive -> NOT passed", r["passed"] is False, f"-> {r['reason']}")
check("all inconclusive -> says why", "could not" in (r["reason"] or "").lower(),
      f"-> {r['reason']}")

r = aggregate([("match", ""), ("inconclusive", "x")])
check("one real match + one inconclusive -> passed", r["passed"] is True)

r = aggregate([("match", ""), ("mismatch", "differs")])
check("any mismatch -> not passed", r["passed"] is False)

r = aggregate([("match", ""), ("match", "")])
check("all match -> passed", r["passed"] is True)

r = aggregate([])
check("no cases at all -> NOT passed", r["passed"] is False,
      f"-> {r['reason']}")

# ── counts reported honestly ────────────────────────────────────────────────
r = aggregate([("match", ""), ("inconclusive", "x"), ("mismatch", "d")])
check("counts are broken out", (r["matched"], r["inconclusive"], r["mismatched"]) == (1, 1, 1),
      f"-> {r['matched']}/{r['inconclusive']}/{r['mismatched']}")


# ── compare_edge_cases must not hide output-ORDER divergence ────────────────
# The regression: a divergence key built from sorted(stdout.splitlines())
# makes two languages that emit the same lines in a different order compare
# equal, so no divergence is ever reported for exactly the class of bug this
# tool exists to catch (map/dict iteration order, sort stability,
# concurrency). classify_case/_normalize in this same module compare stdout
# order-sensitively; compare_edge_cases must agree by default.
_orig_run = translation._run
try:
    translation._run = lambda lang, code, stdin, timeout=15: {
        "ok": True, "stdout": code, "verdict": "OK", "stderr": ""}

    r = compare_edge_cases({"a": "x\ny\n", "b": "y\nx\n"}, inputs=["i"])
    check("same lines, different order -> IS a divergence by default",
          r["divergence_count"] == 1, f"-> {r['divergence_count']}")
    check("  ...classified as an 'order' divergence, not folded into agreement",
          r["divergences"] and r["divergences"][0]["kind"] == "order",
          f"-> {r.get('divergences')}")

    r = compare_edge_cases({"a": "x\ny\n", "b": "y\nx\n"}, inputs=["i"],
                           order_sensitive=False)
    check("order_sensitive=False restores the old order-insensitive comparison",
          r["divergence_count"] == 0, f"-> {r['divergence_count']}")

    r = compare_edge_cases({"a": "x\ny\n", "b": "x\nz\n"}, inputs=["i"])
    check("a real content difference is still a divergence",
          r["divergence_count"] == 1 and r["divergences"][0]["kind"] == "content",
          f"-> {r.get('divergences')}")

    r = compare_edge_cases({"a": "x\ny\n", "b": "x\ny\n"}, inputs=["i"])
    check("identical output in identical order -> no divergence",
          r["divergence_count"] == 0, f"-> {r['divergence_count']}")
finally:
    translation._run = _orig_run


# ── compare_edge_cases: per-run error/code/remedy classification ───────────
# Same rule compare_execution's rows follow (errors.stamp_row) — a run that
# failed for a REQUEST-level reason (no runtime, a timeout) carries
# error/code/remedy on its OWN entry in results[i]["runs"][lang]; an
# ordinary program failure carries none of the three; the OUTER `ok` stays
# `True`. Deterministic fakes, keyed by language, same pattern as above.
_orig_run = translation._run
try:
    def _fake_run(lang, code, stdin, timeout=15):
        if lang == "lua":
            detail = "runtime unavailable for the run phase: 'lua' not found (No such file or directory)"
            return {"ok": False, "stdout": "", "stderr": detail, "error": detail,
                    "exit_code": None, "verdict": "RTE", "timed_out": False}
        if lang == "node":
            return {"ok": False, "stdout": "", "stderr": "<killed: exceeded wall-clock timeout>",
                    "exit_code": None, "verdict": "TLE", "timed_out": True}
        if lang == "perl":
            return {"ok": False, "stdout": "", "stderr": "syntax error at -e line 1.",
                    "exit_code": 1, "verdict": "RTE", "timed_out": False}
        return {"ok": True, "stdout": code, "verdict": "OK", "stderr": "", "timed_out": False}

    translation._run = _fake_run

    r = compare_edge_cases({"lua": "x", "python3": "x"}, inputs=["i"])
    check("compare_edge_cases outer ok is True even though a language could not run",
          r["ok"] is True, f"-> {r['ok']}")
    _runs = r["results"][0]["runs"]
    check("a missing-runtime run is classified runtime_unavailable",
          _runs["lua"].get("code") == errors.RUNTIME_UNAVAILABLE, f"-> {_runs['lua'].get('code')}")
    check("...and carries error text and a remedy",
          bool(_runs["lua"].get("error")) and _runs["lua"].get("remedy") is not None,
          f"-> {_runs['lua']}")
    check("a working run carries no code/error/remedy",
          not ({"code", "error", "remedy"} & _runs["python3"].keys()),
          f"-> {_runs['python3']}")

    r = compare_edge_cases({"node": "x", "python3": "x"}, inputs=["i"])
    check("a timed-out run is classified timeout",
          r["results"][0]["runs"]["node"].get("code") == errors.TIMEOUT,
          f"-> {r['results'][0]['runs']['node'].get('code')}")

    r = compare_edge_cases({"perl": "x", "python3": "x"}, inputs=["i"])
    check("an ordinary program failure carries no code",
          "code" not in r["results"][0]["runs"]["perl"],
          f"-> {r['results'][0]['runs']['perl']}")
finally:
    translation._run = _orig_run


# ═══ verify_optimization must not certify a MEASURED slowdown ═══════════════
# The accept decision used to be `ratio >= min_speedup`, with neither the
# threshold nor the measured ratio required to be an actual speedup. With
# min_speedup=0 a measured ratio of 0.5 — a 2x SLOWDOWN — cleared `0.5 >= 0` and
# was returned accepted=True: the executor watched the candidate get SLOWER and
# the tool certified it as an accepted optimisation. Driven through the real
# verify_optimization with equivalence and timing stubbed, so the accept path
# itself is exactly what these exercise — no executor required, so they run on
# the fallback matrix too.
_orig_vt843 = optimization.verify_translation
_orig_timed843 = optimization._timed


def _timed_stub(min_ms, n=5, sizes=(1000,)):
    """Build a `_timed`-shaped dict with `n` distinct, evenly-spaced runs per
    size, minimum exactly `min_ms` — separated widely enough from whatever
    the OTHER side's stub uses that the two samples do not overlap, so the
    Mann-Whitney test on them is unambiguous (see `_run843`'s docstring for
    when overlap is wanted instead)."""
    runs = [min_ms + i for i in range(n)]
    return {"ok": True, "sizes": list(sizes),
            "durations_ms": [runs[0]] * len(sizes),
            "all_runs_ms": [list(runs) for _ in sizes]}


def _run843(ratio, min_speedup):
    """Call verify_optimization with a MEASURED before/after that yields `ratio`.

    _speedup's ratio is before/after and keeps only before>1.0, after>0.0; pick
    mins (100*ratio, 100) so the measured pair's median-of-mins ratio is
    exactly `ratio`. Each side gets REPEATS-shaped `all_runs_ms` too (5
    distinct, widely-separated values), so `_infer_speedup` has enough data to
    run — for every `ratio` used below except the "genuine win" case, the
    accept/reject verdict is already decided by the ratio checks before the
    significance test is even consulted, so its exact shape does not matter
    there; for the win case the wide separation this builds makes the test
    unambiguously significant, matching what a REAL 2x win would look like.
    """
    optimization.verify_translation = lambda *a, **k: {"passed": True, "matched": 2, "total": 2}
    # Two sizes: a single counted size can never be accepted
    # (`optimization._MIN_COUNTED_SIZES`), and the "genuine win" case below
    # is here to exercise the accept path, not that floor.
    _seq = iter([_timed_stub(100.0 * ratio, sizes=(1000, 2000)),
                 _timed_stub(100.0, sizes=(1000, 2000))])
    optimization._timed = lambda *a, **k: next(_seq)
    try:
        return optimization.verify_optimization("orig", "cand", "python3",
                                                min_speedup=min_speedup)
    finally:
        optimization.verify_translation = _orig_vt843
        optimization._timed = _orig_timed843


_slow843 = _run843(0.5, 0.0)
check("a measured 2x slowdown (ratio 0.5, min_speedup=0) is NOT accepted",
      _slow843.get("accepted") is False,
      f"-> accepted={_slow843.get('accepted')} {_slow843.get('reason')!r}")
_slow843b = _run843(0.5, 1.15)
check("a measured slowdown is refused even under a real threshold",
      _run843(0.5, 1.15).get("accepted") is False, f"-> {_slow843b.get('reason')!r}")
_tie843 = _run843(1.0, 1.0)
check("an exact tie (ratio 1.0) is NOT a speedup and is not accepted",
      _tie843.get("accepted") is False, f"-> {_tie843.get('reason')!r}")
_nothr843 = _run843(2.0, 1.0)
check("min_speedup<=1 can never accept, even a genuine 2x ratio",
      _nothr843.get("accepted") is False, f"-> {_nothr843.get('reason')!r}")
_win843 = _run843(2.0, 1.15)
check("a genuine 2x win clearing a real min_speedup>1 IS accepted",
      _win843.get("accepted") is True and _win843.get("reason", "").startswith("verified faster"),
      f"-> accepted={_win843.get('accepted')} {_win843.get('reason')!r}")
check("  ...and the inference object names a majority of significant sizes",
      _win843.get("inference", {}).get("sizes_rejecting", 0) > 0
      and _win843["inference"]["sizes_rejecting"] == _win843["inference"]["sizes_total"],
      f"-> {_win843.get('inference')}")
# The pure decision, checked directly for the unmeasurable case.
_empty_inference = {"sizes_total": 0, "sizes_rejecting": 0, "decision_basis": "n/a"}
check("an unmeasurable speedup is never accepted",
      optimization._accept_decision({"ratio": None, "measurable": False}, 1.15,
                                    _empty_inference)[0] is False)


# ═══ a ratio that clears the threshold on NOISE alone is still rejected ═════
# This is the fix #843 above did not have: `ratio >= min_speedup` used to be
# the WHOLE decision, so a measured ratio inflated by noise — the
# min-of-repeats happened to land favourably even though the two full
# distributions overlap heavily and nothing about the candidate is actually
# faster — was certified "verified faster" as long as the arithmetic cleared
# the bar. Measured on this repo: reproduced across 30 LIVE runs of identical
# O(1) code, the median-of-mins ratio ranged 0.71x-1.53x and 2/30 runs
# exceeded the default min_speedup=1.15 and were certified. That is exactly
# the false-accept rate a significance test exists to bound. Built here
# DETERMINISTICALLY (an injected, overlapping full sample) rather than by
# waiting for a live run to get unlucky: the two samples below give a
# median-of-mins ratio that clears min_speedup=1.15, but their FULL
# distributions interleave enough that the one-sided Mann-Whitney test cannot
# reject "after is not faster" at alpha=0.05 — real noise, not a fabricated
# input the accept logic special-cases.
_before_noise = [95, 100, 130, 135, 140]  # min 95
_after_noise = [80, 105, 110, 115, 120]  # min 80 -> ratio 95/80 = 1.1875
_noise_ratio = min(_before_noise) / min(_after_noise)
check("control: the injected ratio clears min_speedup=1.15 on the raw numbers",
      _noise_ratio >= 1.15, f"-> ratio={_noise_ratio}")
_noise_mwu = stats.mann_whitney_u(_after_noise, _before_noise, alternative="less")
check("control: the full samples are NOT significant at alpha=0.05",
      _noise_mwu["p_value"] >= 0.05, f"-> p={_noise_mwu['p_value']}")


def _run_noise():
    optimization.verify_translation = lambda *a, **k: {"passed": True, "matched": 2, "total": 2}
    _seq = iter([
        {"ok": True, "sizes": [1000], "durations_ms": [min(_before_noise)],
         "all_runs_ms": [_before_noise]},
        {"ok": True, "sizes": [1000], "durations_ms": [min(_after_noise)],
         "all_runs_ms": [_after_noise]},
    ])
    optimization._timed = lambda *a, **k: next(_seq)
    try:
        return optimization.verify_optimization("orig", "cand", "python3", min_speedup=1.15)
    finally:
        optimization.verify_translation = _orig_vt843
        optimization._timed = _orig_timed843


_noise845 = _run_noise()
check("a ratio that clears min_speedup on noise alone is NOT accepted",
      _noise845.get("accepted") is False, f"-> {_noise845.get('reason')!r}")
check("  ...even though the median ratio itself did clear the threshold",
      _noise845.get("speedup", {}).get("ratio", 0) >= 1.15,
      f"-> ratio={_noise845.get('speedup', {}).get('ratio')}")
check("an unchanged/noisy candidate is never certified 'verified faster'",
      not (_noise845.get("reason") or "").startswith("verified faster"))
check("and it is never graded cross_checked",
      grades.grade_verify_optimization(_noise845, "python3").get("grade") != grades.CROSS_CHECKED)


# ═══ before/after must be paired by MEASURED SIZE, not by list position ════
# `_timed(original)` and `_timed(candidate)` each auto-scale their own copy
# of `sizes` independently (see `optimization._align_sizes`'s docstring).
# The common case is exactly the one that matters: a slow baseline is
# visible at the default sizes (no rescale), a genuinely fast candidate is
# not (one or more rescales) -- so the two `sizes` lists converge to the
# SAME LENGTH but DIFFERENT VALUES, and `_speedup`/`_infer_speedup` zip
# before/after BY POSITION. Reproduced here with a mocked `tools._measure`:
# the candidate needs one rescale round (1000 -> 10000) that the original
# never does, so an unfixed pairing would compare original@1000 against
# candidate@10000 and label the row "n: 1000" (or "n: 10000", depending on
# which side's list is trusted) -- either way, the WRONG pair of numbers
# under one size.
_ORIG850 = "original code"
_CAND850 = "candidate code"


def _mock_measure850(language, code, sizes, timeout, repeats, deadline=None):
    # original: visible immediately at the default size, no rescale
    if code == _ORIG850 and tuple(sizes) == (1000,):
        return [{"n": 1000, "ok": True, "duration_ms": 500,
                 "all_runs_ms": [498, 499, 500, 501, 502]}], None
    if code == _ORIG850 and tuple(sizes) == (10000,):
        # The BASELINE is never grown by alignment (it cleared the floor at
        # 1000); reaching here means a regression to the version that grew
        # a real-cost baseline to the candidate's rescaled n.
        raise AssertionError("alignment re-measured the BASELINE at the candidate's n")
    # candidate: invisible at 1000 (forces one rescale round), visible at 10000
    if code == _CAND850 and tuple(sizes) == (1000,):
        return [{"n": 1000, "ok": True, "duration_ms": 0.5,
                 "all_runs_ms": [0.4, 0.5, 0.5, 0.6, 0.5]}], None
    if code == _CAND850 and tuple(sizes) == (10000,):
        # Distinct values (no ties): a tie anywhere in the COMBINED sample
        # routes stats.mann_whitney_u to the normal approximation (see
        # stats.py), which optimization._infer_speedup now excludes below
        # `_NORMAL_APPROX_MIN_N` observations a side (#285) -- a tie here
        # would make this position untestable and is not what this section
        # is pinning down (the size/size_after disclosure on a mismatched
        # pairing), so the mock stays exact-method by construction.
        return [{"n": 10000, "ok": True, "duration_ms": 5,
                 "all_runs_ms": [4, 4.5, 5.5, 6, 5.2]}], None
    raise AssertionError(f"unexpected _measure call: code={code!r} sizes={sizes!r}")


_orig_measure850 = optimization.tools._measure
optimization.tools._measure = _mock_measure850
try:
    _before850 = optimization._timed(_ORIG850, "python3", [1000])
    _after850 = optimization._timed(_CAND850, "python3", [1000])
finally:
    optimization.tools._measure = _orig_measure850

check("control: the two sides auto-scaled to DIFFERENT sizes",
      _before850["sizes"] != _after850["sizes"]
      and len(_before850["sizes"]) == len(_after850["sizes"]),
      f"-> before={_before850['sizes']} after={_after850['sizes']}")

optimization.tools._measure = _mock_measure850
try:
    _before850_aligned, _after850_aligned = optimization._align_sizes(
        _before850, _after850, _ORIG850, _CAND850, "python3", 30)
finally:
    optimization.tools._measure = _orig_measure850

check("_align_sizes leaves the floor-clearing BASELINE at its own n (never grown)",
      _before850_aligned["sizes"] == [1000] and _after850_aligned["sizes"] == [10000],
      f"-> before={_before850_aligned['sizes']} after={_after850_aligned['sizes']}")
check("  ...and re-measured neither side (the mock raises if the baseline is asked for 10000)",
      _before850_aligned is _before850 and _after850_aligned is _after850,
      "-> alignment returned the inputs untouched")

_sp850 = optimization._speedup(_before850_aligned, _after850_aligned)
check("_speedup pairs baseline@1000 with candidate@10000 and DISCLOSES n_after",
      _sp850.get("per_size") == [{"n": 1000, "before_ms": 500, "after_ms": 5,
                                  "ratio": 100.0, "n_after": 10000}],
      f"-> {_sp850.get('per_size')}")

_inf850 = optimization._infer_speedup(_before850_aligned, _after850_aligned)
check("_infer_speedup's per-size entry is the mismatched pair, disclosed as size/size_after",
      len(_inf850["per_size"]) == 1 and _inf850["per_size"][0]["size"] == 1000
      and _inf850["per_size"][0].get("size_after") == 10000
      and _inf850["sizes_total"] == 1 and _inf850["sizes_below_floor"] == [],
      f"-> {_inf850.get('per_size')}")
check("  ...built from the two arms' own 5-vs-5 samples (n_before/n_after), not a leftover 1-vs-5",
      _inf850["per_size"][0]["n_before"] == 5 and _inf850["per_size"][0]["n_after"] == 5,
      f"-> {_inf850.get('per_size')}")

# Already-aligned input (the common case, e.g. every measured run in this
# suite's own live sections) must cost no extra `_measure` call.
_calls850 = []


def _counting_measure850(language, code, sizes, timeout, repeats, deadline=None):
    _calls850.append((code, tuple(sizes)))
    return [{"n": n, "ok": True, "duration_ms": 10, "all_runs_ms": [10] * 5} for n in sizes], None


optimization.tools._measure = _counting_measure850
try:
    _same850 = optimization._timed("x", "python3", [1000])
    _calls850.clear()
    optimization._align_sizes(_same850, dict(_same850), "x", "x", "python3", 30)
finally:
    optimization.tools._measure = _orig_measure850
check("aligning two ALREADY-matching sides makes zero extra _measure calls",
      _calls850 == [], f"-> {_calls850}")


# ═══ a genuine O(1) candidate that never clears the floor, paired against a ═
#     floor-clearing baseline, is ACCEPTED — not excluded to sizes_total=0 ══
# History: an earlier version of this fix reacted to the OLD bug (alignment
# forcing a real-cost baseline up to a fast arm's UNVALIDATED, exhausted
# ceiling — see `_align_sizes`'s docstring) by shrinking the exhausted
# candidate back down and marking the position EXCLUDED. Reviewed and found
# WRONG: a baseline that clears the floor at its own n, paired against a
# candidate too fast to register even after exhausting its ENTIRE rescale
# budget (`_MAX_RESCALE_ROUNDS`, 10**4x the default size), is not a
# measurement failure — it is the single most decisive result this tool can
# produce. The exclude-and-shrink version drove `sizes_total` to 0 for
# EXACTLY the genuine-huge-win case this whole auto-scale system exists to
# certify: reproduced here with per-size ratios spanning the reviewer's
# 20x-2400x range, `accepted` must be `True`, not `False` with "no size had
# enough comparable runs".
#
# Built via `_timed` stand-ins (mocking the FUNCTION, not `tools._measure`):
# the baseline is comfortably visible at the default sizes (no rescale
# needed, matching a real O(n) or O(n^2) baseline in practice); the
# candidate has already exhausted `_MAX_RESCALE_ROUNDS` on its own (sizes
# scaled 10**4x) and is STILL below the floor at every one of them — the
# `_timed`-level outcome a genuinely O(1) candidate produces. `_align_sizes`
# must make ZERO `tools._measure` calls here (asserted via the call log): no
# re-measurement is needed OR safe once the candidate has exhausted its
# budget without validating anything.
_ORIG_ASYM = "orig-asym"
_CAND_ASYM = "cand-asym"
_ASYM_BEFORE = {"ok": True, "sizes": [2000, 5000, 10000, 20000],
               "durations_ms": [8.0, 20.0, 50.0, 120.0],
               "all_runs_ms": [[7.6, 7.8, 8.0, 8.2, 8.4],
                               [19.0, 19.5, 20.0, 20.5, 21.0],
                               [48.0, 49.0, 50.0, 51.0, 52.0],
                               [116.0, 118.0, 120.0, 122.0, 124.0]]}
_ASYM_AFTER = {"ok": True,
              "sizes": [n * 10 ** optimization._MAX_RESCALE_ROUNDS
                        for n in (2000, 5000, 10000, 20000)],
              "durations_ms": [0.4, 0.2, 0.1, 0.05],
              "all_runs_ms": [[0.35, 0.38, 0.40, 0.42, 0.45],
                              [0.18, 0.19, 0.20, 0.21, 0.22],
                              [0.08, 0.09, 0.10, 0.11, 0.12],
                              [0.04, 0.045, 0.05, 0.055, 0.06]]}
check("control: every candidate duration is below the visibility floor "
      "(a genuinely O(1) workload, exhausted without ever clearing it)",
      all(d < optimization._VISIBILITY_FLOOR_MS for d in _ASYM_AFTER["durations_ms"]),
      f"-> {_ASYM_AFTER['durations_ms']}")
check("control: every baseline duration clears the floor (no rescale needed)",
      all(d >= optimization._VISIBILITY_FLOOR_MS for d in _ASYM_BEFORE["durations_ms"]),
      f"-> {_ASYM_BEFORE['durations_ms']}")

_calls_asym = []


def _counting_measure_asym(language, code, sizes, timeout, repeats, deadline=None):
    _calls_asym.append((code, tuple(sizes)))
    raise AssertionError(
        f"_align_sizes should not re-measure anything here: code={code!r} sizes={sizes!r}")


_orig_measure_asym = optimization.tools._measure
optimization.tools._measure = _counting_measure_asym
try:
    _before_asym_aligned, _after_asym_aligned = optimization._align_sizes(
        dict(_ASYM_BEFORE), dict(_ASYM_AFTER), _ORIG_ASYM, _CAND_ASYM, "python3", 30)
finally:
    optimization.tools._measure = _orig_measure_asym

check("_align_sizes makes ZERO re-measurement calls for an exhausted "
      "candidate against a floor-clearing baseline",
      _calls_asym == [], f"-> {_calls_asym}")
check("  ...and leaves both sides' sizes exactly as measured (intentionally "
      "mismatched, not forced together)",
      _before_asym_aligned["sizes"] == _ASYM_BEFORE["sizes"]
      and _after_asym_aligned["sizes"] == _ASYM_AFTER["sizes"],
      f"-> before={_before_asym_aligned['sizes']} after={_after_asym_aligned['sizes']}")

_inf_asym = optimization._infer_speedup(_before_asym_aligned, _after_asym_aligned)
check("all 4 positions are comparable — none excluded to sizes_below_floor",
      _inf_asym["sizes_total"] == 4 and _inf_asym["sizes_below_floor"] == [],
      f"-> total={_inf_asym['sizes_total']} below_floor={_inf_asym['sizes_below_floor']}")
check("  ...and each comparable entry discloses the mismatched pairing via size_after",
      all(row["size_after"] > row["size"] for row in _inf_asym["per_size"]),
      f"-> {_inf_asym['per_size']}")

_sp_asym = optimization._speedup(_before_asym_aligned, _after_asym_aligned)
check("_speedup's per-size ratios span the reviewer's 20x-2400x range",
      [row["ratio"] for row in _sp_asym["per_size"]] == [20.0, 100.0, 500.0, 2400.0],
      f"-> {_sp_asym.get('per_size')}")
check("  ...disclosed via n_after (the candidate's larger, unaligned n)",
      all(row["n_after"] > row["n"] for row in _sp_asym["per_size"]),
      f"-> {_sp_asym.get('per_size')}")

_accepted_asym, _reason_asym = optimization._accept_decision(_sp_asym, 1.15, _inf_asym)
check("reviewer repro: ACCEPTED, not excluded to 'no size had enough "
      "comparable runs' (the bug this fix closes)",
      _accepted_asym is True, f"-> accepted={_accepted_asym} {_reason_asym!r}")

# End to end through `verify_optimization` itself, mocking `_timed` (not
# `tools._measure`) so `_align_sizes`/`_speedup`/`_infer_speedup` all run for
# real against these exact numbers.
_orig_vt_asym = optimization.verify_translation
_orig_timed_asym = optimization._timed


def _mock_timed_asym(code, language, sizes, timeout=30, deadline=None):
    if code == _ORIG_ASYM:
        return dict(_ASYM_BEFORE)
    if code == _CAND_ASYM:
        return dict(_ASYM_AFTER)
    raise AssertionError(f"unexpected _timed call: code={code!r}")


optimization.verify_translation = lambda *a, **k: {"passed": True, "matched": 2, "total": 2}
optimization._timed = _mock_timed_asym
try:
    _e2e_asym = optimization.verify_optimization(_ORIG_ASYM, _CAND_ASYM, "python3")
finally:
    optimization.verify_translation = _orig_vt_asym
    optimization._timed = _orig_timed_asym

check("end to end: verify_optimization accepts the reviewer's exhausted-"
      "candidate-vs-floor-clearing-baseline repro",
      _e2e_asym.get("accepted") is True,
      f"-> accepted={_e2e_asym.get('accepted')} {_e2e_asym.get('reason')!r}")
check("  ...inference.sizes_total == 4, sizes_below_floor empty",
      _e2e_asym.get("inference", {}).get("sizes_total") == 4
      and _e2e_asym.get("inference", {}).get("sizes_below_floor") == [],
      f"-> {_e2e_asym.get('inference')}")


# ═══ the auto-scale floor is PER SIZE, not combined across all sizes ═══════
# The bug: `_timed`'s old rescale loop broke out as soon as
# `max(measured) >= 20.0 or min(measured) >= 5.0` ACROSS ALL FOUR sizes
# together, so once the two LARGEST sizes here cleared the floor on their
# own, the loop stopped rescaling even though the two SMALLEST sizes were
# still sitting well under 5ms — real evidence from hosted macOS CI showed
# exactly this shape (see `_VISIBILITY_FLOOR_MS`'s docstring in
# `codecalc/optimization.py`). Reproduced here with a mocked
# `tools._measure`: sizes 100/200 start under the floor, 300/400 start
# comfortably over it. The fix rescales ONLY the lagging positions (asserted
# via the call log below, not just the outcome) and the result is that all
# four sizes clear the floor and all four are decisive, not just the two
# that happened to start big.
_ORIG_RESCUE = "print(1)  # orig-rescue"
_CAND_RESCUE = "print(1)  # cand-rescue"

_RESCUE_TABLE = {
    _ORIG_RESCUE: {
        100: (2.0, [1.8, 1.9, 2.0, 2.1, 2.2]),
        200: (3.0, [2.8, 2.9, 3.0, 3.1, 3.2]),
        300: (100.0, [98, 99, 100, 101, 102]),
        400: (120.0, [118, 119, 120, 121, 122]),
        1000: (20.0, [18, 19, 20, 21, 22]),
        2000: (25.0, [23, 24, 25, 26, 27]),
    },
    _CAND_RESCUE: {
        100: (1.0, [0.8, 0.9, 1.0, 1.1, 1.2]),
        200: (1.5, [1.3, 1.4, 1.5, 1.6, 1.7]),
        300: (10.0, [9, 9.5, 10, 10.5, 11]),
        400: (12.0, [11, 11.5, 12, 12.5, 13]),
        1000: (6.0, [5.6, 5.8, 6.0, 6.2, 6.4]),
        2000: (7.5, [7.0, 7.2, 7.5, 7.8, 8.0]),
    },
}
_rescue_calls = []


def _mock_measure_rescue(language, code, sizes, timeout, repeats, deadline=None):
    _rescue_calls.append((code, tuple(sizes)))
    table = _RESCUE_TABLE[code]
    return [{"n": n, "ok": True, "duration_ms": table[n][0], "all_runs_ms": list(table[n][1])}
            for n in sizes], None


_orig_measure_rescue = optimization.tools._measure
optimization.tools._measure = _mock_measure_rescue
try:
    _before_rescue = optimization._timed(_ORIG_RESCUE, "python3", [100, 200, 300, 400])
    _rescue_calls.clear()
    _after_rescue = optimization._timed(_CAND_RESCUE, "python3", [100, 200, 300, 400])
finally:
    optimization.tools._measure = _orig_measure_rescue

check("control: the two smallest sizes needed one rescale round, the two largest did not",
      _before_rescue["sizes"] == [1000, 2000, 300, 400],
      f"-> {_before_rescue['sizes']}")
check("  ...and only the LAGGING positions were re-measured, never all four",
      _rescue_calls == [(_CAND_RESCUE, (100, 200, 300, 400)),
                        (_CAND_RESCUE, (1000, 2000))],
      f"-> {_rescue_calls}")
check("  ...every size now clears the visibility floor",
      all(d >= optimization._VISIBILITY_FLOOR_MS for d in _before_rescue["durations_ms"])
      and all(d >= optimization._VISIBILITY_FLOOR_MS for d in _after_rescue["durations_ms"]),
      f"-> before={_before_rescue['durations_ms']} after={_after_rescue['durations_ms']}")

_inf_rescue = optimization._infer_speedup(_before_rescue, _after_rescue)
check("the fix makes ALL FOUR sizes decisive, including the two the OLD "
      "combined floor would have left untested",
      _inf_rescue["sizes_total"] == 4 and _inf_rescue["sizes_rejecting"] == 4
      and not _inf_rescue["sizes_below_floor"],
      f"-> {_inf_rescue}")
check("  ...invariant: every size is accounted for in per_size or sizes_below_floor",
      len(_before_rescue["sizes"]) == _inf_rescue["sizes_total"] + len(_inf_rescue["sizes_below_floor"]),
      f"-> len(sizes)={len(_before_rescue['sizes'])} sizes_total={_inf_rescue['sizes_total']} "
      f"sizes_below_floor={len(_inf_rescue['sizes_below_floor'])}")


# ═══ a size that can NEVER clear the floor is disclosed, not silently
#     dropped or folded into the majority vote ═══════════════════════════════
# The disclosure layer: if a size still cannot clear `_VISIBILITY_FLOOR_MS`
# after `_timed` exhausts its rescale budget (e.g. a genuinely O(1)-fast
# workload at every achievable size), it must not be silently excluded —
# `inference.sizes_below_floor` names it, and the majority-of-sizes vote is
# computed over the REMAINING sizes only, never padded or shrunk silently.
_ORIG_NEVER = "print(1)  # orig-never"
_CAND_NEVER = "print(1)  # cand-never"
_NEVER_TABLE = {
    _ORIG_NEVER: {
        10: (8.0, [7.0, 7.5, 8.0, 8.5, 9.0]),
        20: (9.0, [8.0, 8.5, 9.0, 9.5, 10.0]),
        30: (10.0, [9.0, 9.5, 10.0, 10.5, 11.0]),
        # Between the OLD 1ms noise floor `_speedup` already applies and the
        # NEW 5ms visibility floor: clears the former (so it is not dropped
        # by the pre-existing `bb <= 1.0` guard below) but never the latter,
        # by construction (a fixed cost regardless of n, as a genuinely
        # O(1)-fast workload would measure) -- this is what must land in
        # `sizes_below_floor`, not fall through the OLDER, coarser filter.
        **{n: (2.0, [1.8, 1.9, 2.0, 2.1, 2.2])
           for n in (50, 500, 5000, 50000, 500000)},
    },
    _CAND_NEVER: {
        10: (5.5, [5.3, 5.4, 5.5, 5.6, 5.7]),
        20: (6.5, [6.3, 6.4, 6.5, 6.6, 6.7]),
        30: (7.5, [7.3, 7.4, 7.5, 7.6, 7.7]),
        **{n: (1.5, [1.3, 1.4, 1.5, 1.6, 1.7])
           for n in (50, 500, 5000, 50000, 500000)},
    },
}


def _mock_measure_never(language, code, sizes, timeout, repeats, deadline=None):
    table = _NEVER_TABLE[code]
    return [{"n": n, "ok": True, "duration_ms": table[n][0], "all_runs_ms": list(table[n][1])}
            for n in sizes], None


_orig_measure_never = optimization.tools._measure
optimization.tools._measure = _mock_measure_never
try:
    _before_never = optimization._timed(_ORIG_NEVER, "python3", [10, 20, 30, 50])
    _after_never = optimization._timed(_CAND_NEVER, "python3", [10, 20, 30, 50])
finally:
    optimization.tools._measure = _orig_measure_never

check("control: the never-visible size exhausted the rescale budget "
      "(4 rounds, 10^4x) on both sides, so no alignment is needed",
      _before_never["sizes"] == [10, 20, 30, 500000]
      and _after_never["sizes"] == [10, 20, 30, 500000],
      f"-> before={_before_never['sizes']} after={_after_never['sizes']}")

_inf_never = optimization._infer_speedup(_before_never, _after_never)
check("a size that can never clear the floor is named in sizes_below_floor, not tested",
      _inf_never["sizes_below_floor"] == [{"size": 500000, "before_ms": 2.0, "after_ms": 1.5}],
      f"-> {_inf_never.get('sizes_below_floor')}")
check("  ...and the majority rule is computed over the remaining 3 sizes, not padded to 4",
      _inf_never["sizes_total"] == 3 and _inf_never["sizes_rejecting"] == 3,
      f"-> total={_inf_never['sizes_total']} rejecting={_inf_never['sizes_rejecting']}")
check("  ...and decision_basis discloses the exclusion, never silently",
      "excluded" in _inf_never["decision_basis"] and "sizes_below_floor" in _inf_never["decision_basis"],
      f"-> {_inf_never['decision_basis']!r}")
check("  ...invariant: every size is accounted for in per_size or sizes_below_floor",
      len(_before_never["sizes"]) == _inf_never["sizes_total"] + len(_inf_never["sizes_below_floor"]),
      f"-> len(sizes)={len(_before_never['sizes'])} sizes_total={_inf_never['sizes_total']} "
      f"sizes_below_floor={len(_inf_never['sizes_below_floor'])}")


# ═══ a size excluded for an OLDER reason (the pre-existing 1ms noise floor
#     `_speedup` already applies) must ALSO land in sizes_below_floor, not
#     vanish from BOTH per_size and sizes_below_floor ═══════════════════════
# Reviewer repro on PR #275: sizes [10, 20, 30] with before [0.5, 10, 12],
# after [0.3, 4, 5]. Size 10's before_ms (0.5) is at/under the OLDER
# `bb <= 1.0` guard `_speedup` already applies (baseline unmeasurable —
# excluded under the ASYMMETRIC rule too, since it is the baseline that
# fails here). Size 20's after_ms (4) is under the NEWER
# `_VISIBILITY_FLOOR_MS` (5), but its BASELINE (before_ms=10) clears the
# floor fine — under the asymmetric rule (a later fix: a position excludes
# only on the BASELINE's failure, never the candidate's) this is now
# COMPARABLE, not excluded: a baseline visibly costing 10ms against a
# candidate too fast to reach 4ms is decisive evidence, not a measurement
# gap. Size 30 clears everything regardless. `_infer_speedup` used to check
# the OLDER guard first with a bare `continue` -- no `sizes_below_floor`
# entry -- so size 10 disappeared from BOTH `per_size` (correctly excluded)
# AND `sizes_below_floor` (incorrectly not told about); that invariant is
# what this test still pins, on the one size (10) that IS excluded.
_before_1ms = {"sizes": [10, 20, 30], "durations_ms": [0.5, 10, 12],
              "all_runs_ms": [[0.4, 0.45, 0.5, 0.55, 0.6],
                              [9, 9.5, 10, 10.5, 11],
                              [11, 11.5, 12, 12.5, 13]]}
_after_1ms = {"sizes": [10, 20, 30], "durations_ms": [0.3, 4, 5],
             "all_runs_ms": [[0.25, 0.28, 0.3, 0.32, 0.35],
                             [3.8, 3.9, 4, 4.1, 4.2],
                             [4.8, 4.9, 5, 5.1, 5.2]]}
_inf_1ms = optimization._infer_speedup(_before_1ms, _after_1ms)
check("a size excluded by the OLDER 1ms-noise-floor guard (an unmeasurable "
      "BASELINE) is named in sizes_below_floor, not dropped from every field",
      _inf_1ms["sizes_below_floor"] == [{"size": 10, "before_ms": 0.5, "after_ms": 0.3}],
      f"-> {_inf_1ms.get('sizes_below_floor')}")
check("  ...but size 20 (baseline clears the floor, candidate does not) is "
      "now COMPARABLE under the asymmetric rule, not excluded",
      _inf_1ms["sizes_total"] == 2
      and {r["size"] for r in _inf_1ms["per_size"]} == {20, 30},
      f"-> {_inf_1ms.get('per_size')}")
check("  ...invariant: every size is accounted for in per_size or sizes_below_floor",
      len(_before_1ms["sizes"]) == _inf_1ms["sizes_total"] + len(_inf_1ms["sizes_below_floor"]),
      f"-> len(sizes)=3 sizes_total={_inf_1ms['sizes_total']} "
      f"sizes_below_floor={len(_inf_1ms['sizes_below_floor'])}")


# ═══ THE PRIMARY REGRESSION: a deterministic reproduction of the CI ═════════
#     false accept — identical code certified as a verified speedup ═════════
# Reported live (macOS sandbox job, native executor, main): `verify_optimization`
# on IDENTICAL before/after code returned `accepted=True` at a measured ratio
# of 1.21x, sizes [50000, 100000, 150000], `sizes_rejecting` 2/3 — the bare
# "a majority of sizes reject" rule let two false-positive per-size votes
# carry a three-size verdict. Both "rejecting" sizes were a tie routed to the
# normal approximation at REPEATS=5 a side (p=0.023, p=0.047 — a sample size
# stats.py's own module docstring says is "not a number worth calling
# alpha=0.05 against"); the third (p=0.898) plainly did not reject.
#
# The incident's raw timings were never preserved, only its summary
# statistics (u, p, method, sizes_rejecting/sizes_total) — so this rebuilds a
# sample pair that MEASURABLY reproduces the same shape (checked below as
# "control" facts, not assumed) rather than the identical bytes: two sizes
# with a tie-routed normal-approximation p just under 0.05 (u=2.5/p=0.020 and
# u=4.0/p=0.044, both close to the reported 0.023/0.047), and a third with a
# p far from significant (u=18.0/p=0.889, matching the reported 0.898 almost
# exactly) — deliberately built with OVERLAPPING before/after ranges (see
# `optimization._ranges_overlap`), the same "the timer cannot always order
# these" shape a run of literally identical code produces.
_ci843_a1, _ci843_b1 = [20, 20, 20, 20, 44], [40, 42, 44, 46, 48]
_ci843_a2, _ci843_b2 = [21, 21, 21, 44, 46], [42, 44, 46, 48, 50]
_ci843_a3, _ci843_b3 = [40, 44, 47, 48, 49], [41, 42, 43, 45, 46]

_ci843_r1 = stats.mann_whitney_u(_ci843_a1, _ci843_b1, alternative="less")
_ci843_r2 = stats.mann_whitney_u(_ci843_a2, _ci843_b2, alternative="less")
_ci843_r3 = stats.mann_whitney_u(_ci843_a3, _ci843_b3, alternative="less")
check("control: size 1 reproduces the reported shape (tied, normal_approximation, p just under 0.05)",
      _ci843_r1["method"] == "normal_approximation" and _ci843_r1["u"] == 2.5
      and _ci843_r1["p_value"] < 0.05, f"-> {_ci843_r1}")
check("control: size 2 reproduces the reported shape (tied, normal_approximation, p just under 0.05)",
      _ci843_r2["method"] == "normal_approximation" and _ci843_r2["u"] == 4.0
      and _ci843_r2["p_value"] < 0.05, f"-> {_ci843_r2}")
check("control: size 3 reproduces the reported shape (clearly not significant)",
      _ci843_r3["p_value"] > 0.8, f"-> {_ci843_r3}")
check("control: sizes 1 and 2's raw samples OVERLAP (the ambiguous, "
      "below-timer-resolution case) — not just tied within one side",
      optimization._ranges_overlap(_ci843_a1, _ci843_b1)
      and optimization._ranges_overlap(_ci843_a2, _ci843_b2))
_ci843_p = [_ci843_r1["p_value"], _ci843_r2["p_value"], _ci843_r3["p_value"]]
check("control: under the OLD bare-majority rule (no method/overlap exclusion, "
      "no per-size ratio gate, no unanimity/Bonferroni) this reproduction "
      "would have been ACCEPTED — 2 of 3 sizes reject at alpha=0.05, a "
      "majority, exactly the reported incident's shape",
      sum(1 for p in _ci843_p if p < 0.05) * 2 > len(_ci843_p),
      f"-> p_values={_ci843_p}")

_ci843_before = {"sizes": [50000, 100000, 150000],
                 "durations_ms": [min(_ci843_b1), min(_ci843_b2), min(_ci843_b3)],
                 "all_runs_ms": [_ci843_b1, _ci843_b2, _ci843_b3]}
_ci843_after = {"sizes": [50000, 100000, 150000],
                "durations_ms": [min(_ci843_a1), min(_ci843_a2), min(_ci843_a3)],
                "all_runs_ms": [_ci843_a1, _ci843_a2, _ci843_a3]}
_ci843_inf = optimization._infer_speedup(_ci843_before, _ci843_after)
_ci843_sp = optimization._speedup(_ci843_before, _ci843_after)
check("control: the reproduction's overall median ratio clears min_speedup, "
      "same as the reported incident's 1.21x",
      _ci843_sp.get("ratio", 0) >= 1.15, f"-> {_ci843_sp.get('ratio')}")
check("the two tied, overlapping-range sizes are excluded to sizes_below_floor "
      "with a reason, never silently counted",
      {e["size"] for e in _ci843_inf["sizes_below_floor"]} == {50000, 100000}
      and all("normal approximation" in e.get("reason", "")
              for e in _ci843_inf["sizes_below_floor"]),
      f"-> {_ci843_inf['sizes_below_floor']}")
_ci843_accepted, _ci843_reason = optimization._accept_decision(_ci843_sp, 1.15, _ci843_inf)
check("THE FIX: the same shape that was reported accepted=True live is now "
      "correctly rejected",
      _ci843_accepted is False, f"-> accepted={_ci843_accepted} {_ci843_reason!r}")


# ═══ a SECOND, independent CI false accept, same guard, different sizes ════
# A second live occurrence (macOS sandbox job, a later PR, main content):
# identical code again `accepted=True`, ratio 1.2x, size 50000 u=4.0/p=0.045
# (tied, normal_approximation — reproduced below almost exactly: u=4.0,
# p=0.0443), size 100000 u=15.5/p=0.77 (NOT rejecting — reproduced exactly:
# u=15.5, p=0.7690), and a third size reported only as "presumably
# significant" (no u/p given) — 2 of 3 reject, the same majority-satisfied
# shape as the first incident, on DIFFERENT raw numbers. Two independent
# live false accepts on the same guard is why this is the PRIMARY
# regression, not a one-off: the realised false-accept rate on that runner
# is well above the nominal 5%. The third size here reuses the first
# incident's own u=2.5/p=0.020 construction (a stand-in for "presumably
# significant" — the live report gave no numbers for it).
_ci290_a1, _ci290_b1 = [21, 21, 21, 44, 46], [42, 44, 46, 48, 50]
_ci290_a2, _ci290_b2 = [43, 43, 44, 46, 48], [40, 42, 44, 45, 47]
_ci290_a3, _ci290_b3 = _ci843_a1, _ci843_b1

_ci290_r1 = stats.mann_whitney_u(_ci290_a1, _ci290_b1, alternative="less")
_ci290_r2 = stats.mann_whitney_u(_ci290_a2, _ci290_b2, alternative="less")
check("control: size 1 reproduces the second incident's reported shape "
      "(tied, normal_approximation, u=4.0, p~0.045)",
      _ci290_r1["method"] == "normal_approximation" and _ci290_r1["u"] == 4.0
      and abs(_ci290_r1["p_value"] - 0.045) < 0.001, f"-> {_ci290_r1}")
check("control: size 2 reproduces the second incident's NON-rejecting size "
      "(u=15.5, p=0.77, exactly)",
      _ci290_r2["method"] == "normal_approximation" and _ci290_r2["u"] == 15.5
      and abs(_ci290_r2["p_value"] - 0.77) < 0.001, f"-> {_ci290_r2}")

_ci290_before = {"sizes": [50000, 100000, 150000],
                 "durations_ms": [min(_ci290_b1), min(_ci290_b2), min(_ci290_b3)],
                 "all_runs_ms": [_ci290_b1, _ci290_b2, _ci290_b3]}
_ci290_after = {"sizes": [50000, 100000, 150000],
                "durations_ms": [min(_ci290_a1), min(_ci290_a2), min(_ci290_a3)],
                "all_runs_ms": [_ci290_a1, _ci290_a2, _ci290_a3]}
_ci290_inf = optimization._infer_speedup(_ci290_before, _ci290_after)
_ci290_sp = optimization._speedup(_ci290_before, _ci290_after)
_ci290_p = [_ci290_r1["p_value"], _ci290_r2["p_value"], _ci843_r1["p_value"]]
check("control: under the OLD bare-majority rule this SECOND reproduction "
      "would ALSO have been accepted — 2 of 3 sizes reject at alpha=0.05",
      sum(1 for p in _ci290_p if p < 0.05) * 2 > len(_ci290_p),
      f"-> p_values={_ci290_p}")
_ci290_accepted, _ci290_reason = optimization._accept_decision(_ci290_sp, 1.15, _ci290_inf)
check("THE FIX: the SECOND live false-accept shape is also correctly rejected",
      _ci290_accepted is False, f"-> accepted={_ci290_accepted} {_ci290_reason!r}")


# ═══ codecalc issue #284: a size below the exact test's own resolution ══════
#     floor counted against the majority it could never contribute a ════════
#     rejection to — measuring ONE FEWER size flipped reject to accept ══════
# The issue's own repro, verbatim: size 4000 kept only 3 of 5 repeats a side
# (two runs lost) — 1/C(6,3) = 1/20 = 0.05 is the best ONE-SIDED p a 3-vs-3
# comparison can ever produce, and 0.05 is not `< 0.05`, so that size could
# NEVER reject no matter how clean the separation, yet the OLD `< 2` guard
# admitted it into `sizes_total` anyway — a guaranteed vote against, not an
# abstention. Size 2000 (5 of 5 repeats, u=0, perfect separation) rejects on
# its own; before this fix, `sizes_rejecting=1, sizes_total=2` was NOT a
# majority (`1*2 <= 2`) and the reported 10x win was rejected as "could be
# noise". `stats.min_testable_n(0.05) == 4`: size 4000's 3 runs a side is
# below it and is now excluded to `sizes_below_floor` instead of counted.
_284_before = {"sizes": [2000, 4000], "durations_ms": [100.0, 200.0],
              "all_runs_ms": [[100., 101., 102., 103., 104.], [200., 201., 202.]]}
_284_after = {"sizes": [2000, 4000], "durations_ms": [10.0, 20.0],
             "all_runs_ms": [[10., 11., 12., 13., 14.], [20., 21., 22.]]}
_284_inf = optimization._infer_speedup(_284_before, _284_after)
check("control: stats.min_testable_n(0.05) == 4, matching the issue's own derivation",
      stats.min_testable_n(0.05) == 4, f"-> {stats.min_testable_n(0.05)}")
check("size 4000 (3 runs a side, structurally unable to reject at alpha=0.05) "
      "is excluded to sizes_below_floor, not counted against the vote",
      _284_inf["sizes_total"] == 1
      and _284_inf["sizes_below_floor"] == [
          {"size": 4000, "before_ms": 200.0, "after_ms": 20.0,
           "reason": _284_inf["sizes_below_floor"][0]["reason"]}]
      and "fewer than 4 runs" in _284_inf["sizes_below_floor"][0]["reason"],
      f"-> {_284_inf}")
check("size 2000's perfect separation is the ONLY counted size and rejects "
      "on its own",
      _284_inf["sizes_total"] == 1 and _284_inf["sizes_rejecting"] == 1,
      f"-> {_284_inf.get('per_size')}")
_284_accepted, _284_reason = optimization._accept_decision(
    {"measurable": True, "ratio": 10.0}, 1.15, _284_inf)
# Unanimity over ONE counted size is a single uncorrected test (see
# `optimization._MIN_COUNTED_SIZES`): the honest verdict for the issue's
# repro is "only one size was testable", stated as such — NOT "could be
# noise" (the old, misleading reason) and NOT an accept on one size's say-so.
check("THE FIX: the two-size repro is refused for the STATED reason that only "
      "one size was testable, not as 'could be noise'",
      _284_accepted is False and "only 1 size was testable" in _284_reason
      and "could be noise" not in _284_reason.split("—")[0],
      f"-> accepted={_284_accepted} {_284_reason!r}")
# The issue's headline complaint — measuring ONE FEWER size flipped the
# verdict — cannot recur: with only the testable size measured, the verdict
# and its stated reason are the same.
_284_one_inf = optimization._infer_speedup(
    {"sizes": [2000], "durations_ms": [100.0], "all_runs_ms": [[100., 101., 102., 103., 104.]]},
    {"sizes": [2000], "durations_ms": [10.0], "all_runs_ms": [[10., 11., 12., 13., 14.]]})
_284_one_accepted, _284_one_reason = optimization._accept_decision(
    {"measurable": True, "ratio": 10.0}, 1.15, _284_one_inf)
check("  ...and measuring one size fewer gives the SAME verdict (no flip either way)",
      _284_one_accepted is _284_accepted
      and "only 1 size was testable" in _284_one_reason,
      f"-> accepted={_284_one_accepted} {_284_one_reason!r}")
# Adversarial review's own single-survivor false-accept shape: identical
# code whose jitter happened to separate the ranges at one size (p=0.0037
# on its own) while the other two sizes were excluded as tied/overlapping.
# One survivor, one uncorrected test — never an accept.
_lone_before = {"sizes": [1000, 2000, 4000], "durations_ms": [45.0, 50.0, 50.0],
                "all_runs_ms": [[45., 45., 45., 45., 46.], [50., 50., 51., 50., 52.],
                                [50., 51., 50., 50., 52.]]}
_lone_after = {"sizes": [1000, 2000, 4000], "durations_ms": [42.0, 50.0, 50.0],
               "all_runs_ms": [[42., 42., 42., 42., 44.], [50., 51., 50., 52., 50.],
                               [50., 50., 52., 51., 50.]]}
_lone_inf = optimization._infer_speedup(_lone_before, _lone_after, min_speedup=1.05)
_lone_accepted, _lone_reason = optimization._accept_decision(
    {"measurable": True, "ratio": 1.07}, 1.05, _lone_inf)
check("control: the review's shape leaves exactly one counted size, and it rejects",
      _lone_inf["sizes_total"] == 1 and _lone_inf["sizes_rejecting"] == 1
      and len(_lone_inf["sizes_below_floor"]) == 2,
      f"-> {_lone_inf}")
check("a lone surviving size that rejects by chance is NEVER an accept",
      _lone_accepted is False and "only 1 size was testable" in _lone_reason,
      f"-> accepted={_lone_accepted} {_lone_reason!r}")


# ═══ codecalc issue #285: _infer_speedup dropped mann_whitney_u's `method`, ═
#     hiding that a tied sample used the normal approximation at n=5 ════════
# A genuine, decisively-separated win (non-overlapping ranges — the OPPOSITE
# of the false-accept shape above) whose raw samples still tie internally
# (integer-ms timings, the issue's own third example): before this fix, a
# caller reading `inference.per_size` could not tell this p-value came from
# the asymptotic approximation rather than the exact distribution. It now
# does, AND — because the ranges do not overlap — this size still correctly
# counts as rejecting: (b)'s exclusion is narrower than "any tie".
_285_after, _285_before = [2, 2, 2, 3, 2], [5, 5, 6, 5, 5]
_285_raw = stats.mann_whitney_u(_285_after, _285_before, alternative="less")
check("control: the issue's own example ties and falls back to the normal approximation",
      _285_raw["method"] == "normal_approximation", f"-> {_285_raw}")
_285_inf = optimization._infer_speedup(
    {"sizes": [1000], "durations_ms": [min(_285_before)], "all_runs_ms": [_285_before]},
    {"sizes": [1000], "durations_ms": [min(_285_after)], "all_runs_ms": [_285_after]})
check("THE FIX: `method` is carried onto the per_size row, previously discarded",
      len(_285_inf["per_size"]) == 1 and _285_inf["per_size"][0]["method"] == "normal_approximation",
      f"-> {_285_inf.get('per_size')}")
check("  ...and a decisive, non-overlapping win still counts as rejecting "
      "despite the tie (not swept into sizes_below_floor with the noise case)",
      _285_inf["sizes_total"] == 1 and _285_inf["sizes_rejecting"] == 1
      and _285_inf["sizes_below_floor"] == [],
      f"-> {_285_inf}")


# ═══ identical before/after code is never accepted (the false-accept rate) ══
# The strongest form of the guarantee above: original and candidate are the
# SAME program, run through the REAL executor with REAL timing noise, not an
# injected sample. There is no true speedup to find, so `accepted` must be
# False regardless of what a single min-of-repeats measurement happens to
# read. Before the significance test, this was exactly the flaky case
# documented above (2/30 live runs certified). With the majority-of-sizes
# Mann-Whitney gate, a false accept now requires a MAJORITY of independent
# per-size tests to each false-positive at alpha=0.05 simultaneously — for 4
# sizes that is under 0.05% by chance, not 1-in-15 (2/30).
if executor._rust:
    _IDENTICAL = ("import sys\nn=int(sys.stdin.readline())\ns=0\n"
                  "for i in range(n): s+=i\nprint(s)")
    _ident = optimization.verify_optimization(_IDENTICAL, _IDENTICAL, "python3",
                                              test_inputs=["10", "100"],
                                              sizes=[50000, 100000, 150000])
    check("identical before/after code is never accepted",
          _ident.get("accepted") is False,
          f"-> accepted={_ident.get('accepted')} "
          f"ratio={(_ident.get('speedup') or {}).get('ratio')} "
          f"inference={_ident.get('inference')}")
else:
    print("SKIP identical-code false-accept-rate check (no native executor built)")


# ═══ the gates are callable on their own, with no model anywhere ═══════════
# They used to run only as the second half of a tool that first asked a
# separately configured model to write the candidate. The caller of this server
# IS a language model; making it supply the candidate removes the dependency
# and puts the strongest model in the loop in charge of the creative half.
if executor._rust:
    SRC = "import sys\nn=int(sys.stdin.readline())\nprint(n*2)"
    GOOD = 'const n=+require("fs").readFileSync(0,"utf8").trim();console.log(n*2)'

    r = translation.verify_translation("python3", SRC, "node", GOOD, ["3", "7", "0"])
    check("a correct port passes", r.get("passed") is True, f"-> {r.get('summary')}")
    r = translation.verify_translation("python3", SRC, "node", "console.log(1)", ["3", "7"])
    check("a wrong port fails", r.get("passed") is False)

    # `s+=i` once per element left the two SMALLEST sizes here close enough to
    # the process-startup/scheduling floor that a contended sandbox runner
    # could not tell before from after: observed on a hosted macOS runner
    # (twice — main at 541528b and a later PR, both in ci-python's sandbox
    # job), n=200000 measured p~0.38-0.65 (indistinguishable from noise) and
    # n=400000 measured p~0.058-0.11 (short of alpha=0.05), while the two
    # LARGEST sizes were decisive every time (p<0.03). The measured median
    # ratio (1.9-2.4x) still cleared min_speedup on all of these — this was
    # exactly the "ratio clears the threshold but the significance test
    # correctly refuses to certify it as real" case the majority-of-sizes
    # gate exists to catch, not a bug in the gate (see `_accept_decision`).
    #
    # Fix: give the SAME O(n)->O(1) win ~10x more work per element (an inner
    # loop, unrolled, accumulating the same sum ten times over and dividing
    # the total back down) so even the SMALLEST tested size's baseline
    # duration comfortably clears the noise floor rather than sitting on it —
    # a large effect at EVERY size, not a bigger `min_speedup` or a weaker
    # acceptance rule. Measured locally (nice -n 19, a saturating CPU load on
    # every core): the unmodified pair accepted 2/20 runs; this pair accepted
    # 20/20, every one of them with a majority of sizes significant and most
    # with all four.
    SLOW = ("import sys\nn=int(sys.stdin.readline())\ns=0\n"
            "for i in range(n):\n"
            "    for _ in range(10):\n"
            "        s+=i\n"
            "print(s//10)")
    FAST = "import sys\nn=int(sys.stdin.readline())\nprint(n*(n-1)//2)"
    SIZES = [200000, 400000, 800000, 1600000]

    o = optimization.verify_optimization(SLOW, FAST, "python3",
                                         test_inputs=["10", "100", "1000"], sizes=SIZES)
    check("a real O(n)->O(1) win is accepted", o.get("accepted") is True,
          f"-> ratio={(o.get('speedup') or {}).get('ratio')} {o.get('reason')!r}")
    check("  ...and equivalence was checked first",
          (o.get("verification") or {}).get("passed") is True)
    check("  ...and a majority of sizes were significant at alpha=0.05",
          (o.get("inference") or {}).get("sizes_total", 0) > 0
          and o["inference"]["sizes_rejecting"] * 2 > o["inference"]["sizes_total"],
          f"-> {o.get('inference')}")

    # The ORIGINAL, weaker O(n)->O(1) pair (before #272 gave it ~10x more
    # work per element to work around this exact bug in the TEST rather than
    # the product) -- `git show e2067cb2^:tests/test_translation_verify.py`.
    # This is the candidate pair that flaked on hosted macOS CI: `s+=i` once
    # per element left the two SMALLEST of four sizes close enough to
    # process-startup/scheduling noise that the OLD combined auto-scale floor
    # never rescaled them (the largest size alone cleared it), so the
    # significance test saw only 2/4 decisive sizes and correctly called a
    # real 1.9-2.4x speedup "could be noise". The per-size floor fix rescales
    # EVERY size until IT is visible, independent of how big the others are —
    # measured on this box, 20 runs under `nice -n 19` plus a saturating CPU
    # load on every core: 13/20 accepted, up from 2/20 on the unfixed
    # product (see CHANGELOG.md). NOT asserted here as a hard live check,
    # deliberately: this candidate pair sits close enough to the noise floor
    # by construction (that is WHY #272 replaced it for the CI-gated test
    # above) that even the fix does not make EVERY run decisive on a shared,
    # variably-loaded runner — an unconditional `accepted is True` here would
    # reintroduce exactly the flaky live-CI assertion #272 existed to remove,
    # now on a different candidate pair. The heavier SLOW/FAST pair above
    # (which the fix does not need to rescue — it was already comfortably
    # over the old COMBINED floor at every size) stays the CI-gated live
    # evidence that a real win is accepted; the deterministic mocked tests
    # elsewhere in this file are what actually pin the per-size floor
    # mechanism down.

    # The unchanged-candidate rejection is asserted DETERMINISTICALLY above
    # (the "false-accept rate" section), and again LIVE further above with
    # identical code — not here: a live FAST-vs-FAST timing measures noise
    # (identical O(1) code, runtime dominated by process-startup jitter) and
    # its median ratio occasionally exceeds min_speedup on its own, which is
    # exactly the case the significance test (already exercised in the other
    # two spots) is for. The rejections below carry what an optimiser that
    # fabricates wins cannot: WHICH gate failed.
    o = optimization.verify_optimization(SLOW, "print(999)", "python3",
                                         test_inputs=["10", "100"])
    check("a faster-but-wrong candidate is rejected on correctness",
          o.get("accepted") is False and o.get("reason") == "not equivalent",
          f"-> {o.get('reason')!r}")
    check("  ...and its speed was never measured", "speedup" not in o,
          "-> a faster wrong answer is not an optimisation")
    check("  ...and inference was never run either",
          "inference" not in o, "-> a faster wrong answer is not an optimisation")
else:
    print("SKIP live verification gates (no native executor built)")


# ═══ a REAL O(n^2) baseline vs a REAL O(1) candidate, at CALIBRATED sizes ══
# The mocked test above pins the ALIGNMENT INVARIANT down deterministically;
# this measures the thing that invariant protects — REAL wall time — with a
# real payload instead of a duration pinned below the floor. The
# `mcp_middleware.TOOL_TIMEOUTS["verify_optimization"]` comment's "~27s worst
# case" was measured with every reported duration pinned below the floor (so
# the rescale ladder ran its full budget purely on subprocess-spawn cost);
# it does not bound a baseline whose measured time actually GROWS with n —
# exactly the shape this test uses.
#
# `original` is a genuine O(n^2): a `volatile` accumulator defeats the
# compiler's usual strength-reduction of a trivial counting loop into a
# closed form (`gcc -O2` — see codecalc/registry.py — will otherwise fold
# `for i: for j: s+=1` into `s = n*n` and the "baseline" would silently
# become O(1) too). `candidate` keeps a small FIXED (n-independent) busy-loop
# pad: the asymmetric visibility rule (this fix) no longer needs padding to
# avoid a false EXCLUSION, but an unpadded candidate's own duration can still
# occasionally dip below the floor on ONE size purely from subprocess-spawn
# jitter (measured directly: happened during this fix's own development),
# triggering a real rescale that can VALIDATE at a size the real O(n^2)
# baseline then gets grown up to and cannot afford — the padding keeps the
# candidate's duration a stable, comfortable margin above the floor so that
# alignment path is never exercised by accident; the mocked test above pins
# the exhausted-candidate path deterministically instead.
#
# Sizes are CALIBRATED AT TEST TIME, not hardcoded, after this exact test
# failed on a hosted macOS runner: the previous fixed default sizes
# ([2000, 5000, 10000, 20000]) measured a real but weak 1.55x with NO sizes
# excluded, i.e. even the BASELINE's smallest size was too small next to
# that runner's (higher) subprocess-spawn overhead to produce a decisive
# signal — a hardware-dependent flake, not a code bug. Fixed here with the
# simplest calibration that works: probe the baseline at the smallest
# default size (2000), double it until its duration clears 50ms (10x
# `_VISIBILITY_FLOOR_MS`), capped at 8 doublings, then scale the other three
# default-ratio sizes (2.5x/5x/10x) off that same calibrated unit. Printed
# UNCONDITIONALLY (not just on failure) so a CI failure on a
# differently-calibrated runner is diagnosable from the log alone.
if executor._rust:
    _QUAD_BASE = (
        "#include <stdio.h>\n"
        "int main(){\n"
        "    long long n; if (scanf(\"%lld\", &n) != 1) return 1;\n"
        "    if (n < 0) n = 0;\n"
        "    volatile long long s = 0;\n"
        "    for (long long i = 0; i < n; i++) {\n"
        "        for (long long j = 0; j < n; j++) {\n"
        "            s += 1;\n"
        "        }\n"
        "    }\n"
        "    printf(\"%lld\\n\", (long long)s);\n"
        "    return 0;\n"
        "}\n")
    _QUAD_CAND = (
        "#include <stdio.h>\n"
        "int main(){\n"
        "    long long n; if (scanf(\"%lld\", &n) != 1) return 1;\n"
        "    if (n < 0) n = 0;\n"
        "    volatile long long pad = 0;\n"
        "    for (long long k = 0; k < 800000; k++) pad += 1;\n"
        "    printf(\"%lld\\n\", n*n);\n"
        "    return 0;\n"
        "}\n")

    def _quad_probe_ms(code, n, reps=3, timeout=20):
        vals = []
        for _ in range(reps):
            r = executor.execute("c", code, stdin=f"{n}\n", timeout=timeout)
            d = r.get("duration_ms")
            if d is not None:
                vals.append(d)
        return min(vals) if vals else None

    # Calibrate against BOTH arms: the unit size must make the baseline
    # cost at least 100ms AND at least 6x what the O(1) candidate costs on
    # this host (spawn overhead plus its fixed pad). A hosted macOS runner
    # probed the baseline at 51ms, then measured it at 34ms against a 14ms
    # candidate at the smallest size: p=0.93 there, so the majority rule
    # still accepted (3/4) but the smallest size was pure spawn jitter. A
    # single absolute threshold cannot know the host's spawn cost; the
    # candidate probe does.
    _quad_cand_ms = _quad_probe_ms(_QUAD_CAND, 2000) or 10.0
    _quad_floor_ms = max(100.0, 6.0 * _quad_cand_ms)
    _quad_unit = 2000
    _quad_unit_ms = _quad_probe_ms(_QUAD_BASE, _quad_unit)
    _quad_calib_steps = 0
    while (_quad_unit_ms is None or _quad_unit_ms < _quad_floor_ms) and _quad_calib_steps < 8:
        _quad_unit *= 2
        _quad_unit_ms = _quad_probe_ms(_QUAD_BASE, _quad_unit)
        _quad_calib_steps += 1
    # A QUADRATIC payload squares the spread: 10x the unit is 100x the work,
    # which put the largest size at ~13s per run (x REPEATS) on this box and
    # spent 93s of the 180s tool deadline on one call. (1, 1.5, 2, 3) keeps
    # the largest size at 9x the unit's work (~1.2s per run at a 131ms unit)
    # while the four sizes still span a 9x cost range for the ladder.
    _QUAD_SIZES = [int(_quad_unit * r) for r in (1, 1.5, 2, 3)]
    print(f"[calibration] unit={_quad_unit} "
          f"(after {_quad_calib_steps} doubling step(s), cap 8) "
          f"baseline@unit={_quad_unit_ms}ms candidate={_quad_cand_ms}ms "
          f"floor={_quad_floor_ms}ms sizes={_QUAD_SIZES}")

    _quad_t0 = time.perf_counter()
    _quad_result = optimization.verify_optimization(
        _QUAD_BASE, _QUAD_CAND, "c", test_inputs=["0", "1", "10", "100"],
        sizes=_QUAD_SIZES)
    _quad_wall_s = time.perf_counter() - _quad_t0
    print(f"[calibration] verify_optimization: ok={_quad_result.get('ok')} "
          f"accepted={_quad_result.get('accepted')} "
          f"speedup={_quad_result.get('speedup')} "
          f"inference_totals=({(_quad_result.get('inference') or {}).get('sizes_rejecting')}/"
          f"{(_quad_result.get('inference') or {}).get('sizes_total')}) "
          f"wall={_quad_wall_s:.1f}s")

    check("a real O(n^2)->O(1) win at CALIBRATED sizes is accepted",
          _quad_result.get("ok") is True and _quad_result.get("accepted") is True,
          f"-> ok={_quad_result.get('ok')} accepted={_quad_result.get('accepted')} "
          f"reason={_quad_result.get('reason')!r} error={_quad_result.get('error')!r} "
          f"code={_quad_result.get('code')!r} sizes={_QUAD_SIZES} wall={_quad_wall_s:.1f}s")
    # The product's own rule is a MAJORITY of sizes; asserting 4/4 here
    # made the test stricter than the tool it exercises, and a hosted
    # runner's spawn jitter at the smallest size is exactly what the
    # majority rule exists to absorb. Every size must still be COUNTED
    # (none below the floor) and the two largest must be decisive.
    _quad_inf = _quad_result.get("inference") or {}
    _quad_ps = _quad_inf.get("per_size") or []
    check("  ...every CALIBRATED size counted, a majority decisive, the two largest decisive",
          _quad_inf.get("sizes_total") == 4
          and _quad_inf.get("sizes_rejecting", 0) >= 3
          and _quad_inf.get("sizes_below_floor") == []
          and len(_quad_ps) == 4
          and all(row["p_value"] < 0.05 for row in _quad_ps[-2:]),
          f"-> {_quad_inf}")
    check("  ...ratio measured, not asserted",
          isinstance((_quad_result.get("speedup") or {}).get("ratio"), (int, float))
          and _quad_result["speedup"]["ratio"] > 1,
          f"-> {_quad_result.get('speedup')}")
    # mcp_middleware.TOOL_TIMEOUTS["verify_optimization"] = 180 (see that
    # table's comment, corrected alongside this fix). A generous margin
    # below it, not a number tuned to just barely pass, catches a real
    # regression without making the check flaky on a loaded CI runner.
    check("  ...and the REAL wall time stays well under the 180s tool deadline",
          _quad_wall_s < 90.0,
          f"-> wall={_quad_wall_s:.1f}s (180s deadline, "
          f"margin={180.0 - _quad_wall_s:.0f}s)")
else:
    print("SKIP real O(n^2)-vs-O(1) wall-time measurement (no native executor built)")


# ═══ _timed's OWN rescale rounds share the SAME measurement deadline as ═══
#     _align_sizes — the gap fix 2 in the review closes ══════════════════
# An earlier version of the shared budget covered only `_align_sizes`'s
# re-measurement calls; `_timed`'s own rescale rounds (on EITHER side, no
# alignment involved at all) had no equivalent backstop, so a baseline that
# needed several REAL rescale rounds could still spend `timeout x REPEATS`
# per round with nothing capping the total.
#
# Measured directly on this box (see the calibration probe above): a REAL
# compiled C payload cannot actually be forced through SEVERAL real rescale
# rounds here — subprocess-spawn overhead alone (~10ms, measured via an
# empty program) already exceeds `_VISIBILITY_FLOOR_MS` (5ms) for any n>=~10,
# so `_timed(_QUAD_BASE, "c", [1])` needed exactly ONE round (n=1 -> n=10,
# already visible) in practice, never several — an environmental fact, not
# something this test can override with a different C program. To still
# exercise and TIME several real rescale rounds deterministically, each
# mocked `tools._measure` call below actually `time.sleep`s for a duration
# standing in for genuine O(n^2) per-element cost — REAL wall-clock time is
# spent, just not by spawning an actual subprocess — while reporting "still
# below the floor" for the first three sizes tried and clearing it only on
# the fourth, the exact shape a real, very-cheap-per-element O(n^2) baseline
# at small n would produce.
_rescale_round_calls = []


def _mock_measure_multi_round(language, code, sizes, timeout, repeats, deadline=None):
    _rescale_round_calls.append((tuple(sizes), timeout))
    runs = []
    for n in sizes:
        real_ms = {1000: 2.0, 10000: 3.0, 100000: 4.0}.get(n, 60.0)
        for _ in range(repeats):
            time.sleep(real_ms / 1000.0)
        runs.append({"n": n, "ok": True, "duration_ms": real_ms,
                     "all_runs_ms": [real_ms] * repeats})
    return runs, None


_orig_measure_multi_round = optimization.tools._measure
optimization.tools._measure = _mock_measure_multi_round
_multi_t0 = time.perf_counter()
try:
    _multi_result = optimization._timed(
        "multi-round-baseline", "c", [1000],
        deadline=time.monotonic() + optimization._MEASUREMENT_BUDGET_S)
finally:
    optimization.tools._measure = _orig_measure_multi_round
_multi_wall_s = time.perf_counter() - _multi_t0

check("a baseline needing THREE real rescale rounds clears the floor on the "
      "fourth call (1000 -> 10000 -> 100000 -> 1000000), not silently "
      "truncated or capped at fewer rounds",
      _multi_result.get("ok") is True and _multi_result["sizes"] == [1000000]
      and _multi_result["durations_ms"][0] == 60.0,
      f"-> {_multi_result}")
check("  ...4 tools._measure calls total (initial + 3 rescale rounds) — "
      "the recomputed worst-case shape for a SINGLE size is unchanged: "
      "1 + _MAX_RESCALE_ROUNDS calls, still inside the existing 234-call "
      "ceiling for the whole verify_optimization call",
      len(_rescale_round_calls) == 4,
      f"-> {_rescale_round_calls}")
check("  ...and the REAL wall time (actual time.sleep calls, not a "
      "zero-cost mock) stays comfortably under the shared "
      "_MEASUREMENT_BUDGET_S",
      _multi_wall_s < optimization._MEASUREMENT_BUDGET_S,
      f"-> wall={_multi_wall_s:.3f}s budget={optimization._MEASUREMENT_BUDGET_S}s")


# ═══ the shared measurement-deadline guard bounds a REAL runaway ══════════
#     re-measurement ═══════════════════════════════════════════════════════
# The residual risk `_MEASUREMENT_BUDGET_S` exists for: a re-measurement
# target IS a size the other side validated (it genuinely cleared the floor
# there), but that size is still expensive for the SPECIFIC program being
# asked to run at it — a real O(n^2) baseline can be cheap for a fast
# candidate and ruinous for itself at the same n. Built directly rather than
# hoping live subprocess-spawn jitter reproduces it (it does, but not
# reliably — see the comment on this file's `_ORIG850`/`_CAND850` mock for
# why that path is tested deterministically instead): a REAL baseline
# measurement, then an alignment call against a HAND-BUILT "after" standing
# in for a candidate that validated a huge n (2,000,000 — a size the
# baseline never chose for itself), with the SAME shared deadline
# `verify_optimization` would construct passed explicitly (this call bypasses
# `verify_optimization`, so nothing computes one automatically). Before this
# fix, that could run for `timeout x REPEATS` per position (up to 150s at the
# default timeout=30) with no defence beyond a per-execution timeout that a
# slow-but-COMPLETING run never trips. With the fix, `_remeasure_positions`
# divides what's left of the shared budget (120s) across the executions it
# is about to run, so a single real O(n^2) baseline asked to run at
# 2,000,000 (4 * 10**12 operations) fails FAST with a coded, disclosed
# reason.
if executor._rust:
    # The only arm alignment ever grows is the CANDIDATE (the baseline is
    # never grown -- see _align_sizes). So the runaway this guard bounds is
    # a real-cost program in the candidate role asked to catch up to a
    # baseline n that cleared the floor: the quadratic program plays the
    # candidate, converging at n=2000, against a hand-built baseline that
    # validated n=2,000,000 (a size the quadratic program never chose).
    _guard_after = optimization._timed(_QUAD_BASE, "c", [2000])
    check("control: the (quadratic) candidate converges immediately at n=2000",
          _guard_after.get("ok") is True and _guard_after["sizes"] == [2000],
          f"-> {_guard_after}")

    _guard_before = {"ok": True, "sizes": [2_000_000], "durations_ms": [60.0],
                     "all_runs_ms": [[58.0, 59.0, 60.0, 61.0, 62.0]]}
    _guard_deadline = time.monotonic() + optimization._MEASUREMENT_BUDGET_S

    _guard_t0 = time.perf_counter()
    _guard_before_aligned, _guard_after_aligned = optimization._align_sizes(
        _guard_before, _guard_after, "unused (baseline side is never "
        "re-measured)", _QUAD_BASE, "c", 30, deadline=_guard_deadline)
    _guard_wall_s = time.perf_counter() - _guard_t0

    check("a real re-measurement request the candidate cannot afford fails, "
          "rather than completing (or hanging) at 4*10**12 operations",
          _guard_after_aligned.get("ok") is False,
          f"-> {_guard_after_aligned}")
    check("  ...with a coded, disclosed reason naming the deadline/timeout, "
          "not a bare crash or silent hang",
          "timed out" in (_guard_after_aligned.get("error") or "")
          or "deadline" in (_guard_after_aligned.get("error") or ""),
          f"-> {_guard_after_aligned.get('error')!r}")
    check("  ...and it fails FAST: bounded by the shared _MEASUREMENT_BUDGET_S, "
          "not by timeout(30) x REPEATS(5) = 150s",
          _guard_wall_s < optimization._MEASUREMENT_BUDGET_S + 5.0,
          f"-> wall={_guard_wall_s:.1f}s (_MEASUREMENT_BUDGET_S="
          f"{optimization._MEASUREMENT_BUDGET_S}s)")
else:
    print("SKIP alignment deadline guard live check (no native executor built)")


# ── the default evidence must include the discriminating inputs ───────────
# The MCP tools defaulted to DEFAULT_EDGE_INPUTS[:4] — '', '0', '1', '-1' —
# discarding '10', '100' and '0.1\n0.2'. Float formatting is one of the most
# common genuine divergences between ports, so the default excluded the input
# most likely to find a real bug.
#
# This is the demonstration, not an argument: the port below agrees on every
# integral sum and differs only in float rendering. Under the old default it
# was CERTIFIED equivalent.
_SRC_F = "import sys\nv=[float(x) for x in sys.stdin.read().split()]\nprint(float(sum(v)))"
_TGT_F = ("const t=require('fs').readFileSync(0,'utf8').trim();\n"
          "const v=t?t.split(/\\s+/).map(Number):[];\n"
          "const s=v.reduce((a,b)=>a+b,0);\n"
          "console.log(s.toFixed(1));")

if executor.probe().get("node"):
    _full = translation.verify_translation("python3", _SRC_F, "node", _TGT_F,
                                           translation.DEFAULT_EDGE_INPUTS)
    # The control is the deterministic property the regression concerns: the
    # old four-item slice omitted the discriminating float input. Executing the
    # slice again added no evidence and made this test depend on four extra
    # Windows runtime launches, which intermittently returned unrelated output.
    check("control: the truncated set omits the float-divergence input",
          "0.1\n0.2" not in translation.DEFAULT_EDGE_INPUTS[:4])
    check("the full set catches the float divergence the slice hid",
          _full.get("passed") is False and _full.get("mismatched") >= 1,
          f"-> passed={_full.get('passed')} mismatched={_full.get('mismatched')} "
          f"inconclusive={_full.get('inconclusive')}")
    _bad = [c for c in _full.get("cases", []) if c.get("outcome") == "mismatch"]
    check("  ...and names the input that did it",
          bool(_bad) and "0.1" in _bad[0].get("input", ""),
          f"-> {_bad[0].get('input')!r} {_bad[0].get('source',{}).get('stdout')!r} vs "
          f"{_bad[0].get('target',{}).get('stdout')!r}" if _bad else "-> no mismatch recorded")
else:
    print("SKIP float-divergence probe — node runtime not available")

# ── a lost measurement is not a divergence (#42, the flaky Windows gate) ───
# One side exits 0 with no output while the other prints something. That is the
# same missing measurement classify_case already refuses to score when BOTH
# sides are empty — the one-sided case fell through to "stdout differs" and was
# reported as positive evidence of non-equivalence, which is what made the
# Windows translation gate flaky.
#
# Driven through a stubbed _run so both branches are deterministic and need
# neither node nor the flake itself to reproduce.
_orig_run = translation._run


def _stub_run(target_outputs):
    calls = {"n": 0}

    def _run(lang, code, stdin, timeout):
        if lang == "python3":
            return {"ok": True, "stdout": "0.0", "phase": "run", "stderr": ""}
        i = calls["n"]
        calls["n"] += 1
        return {"ok": True, "stdout": target_outputs[min(i, len(target_outputs) - 1)],
                "phase": "run", "stderr": ""}
    return _run, calls


for _label, _outs, _want, _runs in [
    ("unstable runtime is INCONCLUSIVE, not a mismatch", ["", "0.0"], "inconclusive", 2),
    ("a reproducibly empty port is still a MISMATCH", ["", ""], "mismatch", 2),
    ("agreement costs no extra run", ["0.0"], "match", 1),
]:
    translation._run, _calls = _stub_run(_outs)
    try:
        _res = translation.verify_translation("python3", "s", "node", "t", [""])
    finally:
        translation._run = _orig_run
    _case = _res["cases"][0]
    check(_label, _case["outcome"] == _want and _calls["n"] == _runs,
          f"-> outcome={_case['outcome']} target_runs={_calls['n']}")

# The re-run must never turn a lost measurement into a PASS. Certifying a
# translation because the runtime produced output the second time would be the
# exact failure this repo keeps closing.
translation._run, _ = _stub_run(["", "0.0"])
try:
    _flaky = translation.verify_translation("python3", "s", "node", "t", [""])
finally:
    translation._run = _orig_run
check("an unstable runtime never certifies the translation",
      _flaky.get("passed") is False,
      f"-> passed={_flaky.get('passed')} inconclusive={_flaky.get('inconclusive')}")

# Detector shape check: only ok=True + empty-vs-nonempty qualifies. A real
# failure on one side is a genuine divergence and must stay one.
_full_r = {"ok": True, "stdout": "0.0"}
_empty_r = {"ok": True, "stdout": ""}
check("vanished-output detector ignores a genuine failure",
      translation.vanished_output_side(_full_r, {"ok": False, "stdout": ""}) is None)
check("vanished-output detector ignores two empty results",
      translation.vanished_output_side(_empty_r, _empty_r) is None)
check("vanished-output detector names the empty side",
      translation.vanished_output_side(_full_r, _empty_r) == "target"
      and translation.vanished_output_side(_empty_r, _full_r) == "source")


# The tools must not re-introduce the slice. Checked through the SERVER layer,
# because that is the one a model calls and the one that carried the slice —
# the module function never did.
_srv = _server.verify_translation("print(1)", "python3", "console.log(1)", "node")
check("the MCP tool tests the full default set",
      _srv.get("total") == len(translation.DEFAULT_EDGE_INPUTS),
      f"-> total={_srv.get('total')} of {len(translation.DEFAULT_EDGE_INPUTS)}")

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL TRANSLATION-VERIFICATION TESTS PASS ===")
sys.exit(1 if FAILS else 0)
