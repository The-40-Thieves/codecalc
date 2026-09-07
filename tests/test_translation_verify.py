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

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import executor, grades, optimization, stats, translation
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
    return {"ok": True, "sizes": list(sizes), "durations_ms": [runs[0]],
            "all_runs_ms": [runs]}


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
    _seq = iter([_timed_stub(100.0 * ratio), _timed_stub(100.0)])
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


def _mock_measure850(language, code, sizes, timeout, repeats):
    # original: visible immediately at the default size, no rescale
    if code == _ORIG850 and tuple(sizes) == (1000,):
        return [{"n": 1000, "ok": True, "duration_ms": 500,
                 "all_runs_ms": [498, 499, 500, 501, 502]}], None
    if code == _ORIG850 and tuple(sizes) == (10000,):
        # only reached by _align_sizes's re-measurement, once the candidate
        # forced 10000 into the picture -- a real O(n) baseline at 10x the
        # size takes roughly 10x as long.
        return [{"n": 10000, "ok": True, "duration_ms": 5000,
                 "all_runs_ms": [4980, 4990, 5000, 5010, 5020]}], None
    # candidate: invisible at 1000 (forces one rescale round), visible at 10000
    if code == _CAND850 and tuple(sizes) == (1000,):
        return [{"n": 1000, "ok": True, "duration_ms": 0.5,
                 "all_runs_ms": [0.4, 0.5, 0.5, 0.6, 0.5]}], None
    if code == _CAND850 and tuple(sizes) == (10000,):
        return [{"n": 10000, "ok": True, "duration_ms": 5,
                 "all_runs_ms": [4, 5, 5, 6, 5]}], None
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

check("_align_sizes makes both sides share the SAME sizes",
      _before850_aligned["sizes"] == _after850_aligned["sizes"],
      f"-> before={_before850_aligned['sizes']} after={_after850_aligned['sizes']}")
check("  ...at the LARGER (candidate-forced) size, not the smaller one",
      _before850_aligned["sizes"] == [10000], f"-> {_before850_aligned['sizes']}")

_sp850 = optimization._speedup(_before850_aligned, _after850_aligned)
check("_speedup's per-size entry pairs the RIGHT before/after values for n=10000",
      _sp850.get("per_size") == [{"n": 10000, "before_ms": 5000, "after_ms": 5, "ratio": 1000.0}],
      f"-> {_sp850.get('per_size')}")

_inf850 = optimization._infer_speedup(_before850_aligned, _after850_aligned)
check("_infer_speedup's per-size entry is also for n=10000, not the stale n=1000",
      len(_inf850["per_size"]) == 1 and _inf850["per_size"][0]["size"] == 10000,
      f"-> {_inf850.get('per_size')}")
check("  ...built from the aligned 5-vs-5 samples (n_before/n_after), not a leftover 1-vs-5",
      _inf850["per_size"][0]["n_before"] == 5 and _inf850["per_size"][0]["n_after"] == 5,
      f"-> {_inf850.get('per_size')}")

# Already-aligned input (the common case, e.g. every measured run in this
# suite's own live sections) must cost no extra `_measure` call.
_calls850 = []


def _counting_measure850(language, code, sizes, timeout, repeats):
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

    SLOW = "import sys\nn=int(sys.stdin.readline())\ns=0\nfor i in range(n): s+=i\nprint(s)"
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
