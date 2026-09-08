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
import time

from . import executor, stats, tools
from .translation import DEFAULT_EDGE_INPUTS, verify_translation

#: Runs per size. Raised from 3: with 3-vs-3 timings the smallest one-sided
#: Mann-Whitney p a single size can ever produce is 1/C(6,3) = 1/20, so no
#: size could reach the conventional alpha=0.05 no matter how clean the
#: separation was — the inference in `_infer_speedup` below had no floor to
#: stand on. At 5-vs-5 the floor is 1/C(10,5) = 1/252, comfortably past 0.05.
REPEATS = 5

#: Per-size auto-scale floor (ms). `_timed` used to break out of its rescale
#: loop as soon as `max(measured) >= 20.0 or min(measured) >= 5.0` ACROSS ALL
#: SIZES COMBINED, so once the LARGEST tested size cleared the floor, the
#: loop stopped rescaling even though the SMALLEST sizes could still be
#: sitting in timer noise. Real evidence (main@541528bb's `ci-python` sandbox
#: job, and PR #270's, both on a hosted macOS runner): a genuine 1.9-2.4x
#: O(n)->O(1) speedup measured n=200000 at p=0.65 and n=400000 at p=0.058
#: (indistinguishable from noise) while n=800000 and n=1600000 were always
#: decisive (p<0.03) — the two largest sizes clearing the combined floor
#: silently left the two smallest never rescaled, and the majority-of-sizes
#: rule (`_accept_decision`) correctly called that "could be noise" and
#: rejected a real win.
#:
#: Fix: apply the floor PER SIZE (`_timed`, below) instead of combined across
#: sizes. With one duration to test rather than several, the old combined
#: check `d >= 20.0 or d >= 5.0` reduces to `d >= 5.0` — the `20.0` branch
#: only ever did anything when comparing the LARGEST of several sizes against
#: the SMALLEST, which is exactly the failure mode this constant now closes.
_VISIBILITY_FLOOR_MS = 5.0

#: Auto-scale rounds, per size. Unchanged from the old combined loop's "up to
#: 4x": each round multiplies a still-invisible size by 10, so the worst case
#: (a size that never clears the floor) still bottoms out at the same 10**4
#: ceiling.
_MAX_RESCALE_ROUNDS = 4

#: Wall-clock ceiling for the WHOLE measurement phase of ONE
#: `verify_optimization` call — both `_timed` calls (original AND candidate,
#: including every auto-scale round each makes) AND `_align_sizes`'s
#: re-measurement — independent of the per-execution `timeout` argument.
#:
#: A single shared budget, not one per phase: `_align_sizes` (below) already
#: avoids asking either side to run at a size the OTHER side has not itself
#: validated as a real, floor-clearing measurement — see its docstring — so
#: this is a backstop against the residual case, not the primary defence: a
#: re-measurement (or, just as easily, an ordinary `_timed` rescale round on
#: a program that is simply slow) can target a size that IS legitimate but
#: is still expensive for the SPECIFIC program running it (a real O(n) or
#: worse baseline can be cheap for a fast candidate and ruinous for itself at
#: the same n, and `_timed`'s own auto-scale ladder can walk a genuinely slow
#: baseline through several such rounds on its own, no alignment involved).
#: An earlier version of this budget covered only `_align_sizes`'s calls;
#: `_timed`'s own rescale rounds had no equivalent backstop, so a baseline
#: that needed several real rescale rounds (see the live test using one)
#: could still spend `timeout x REPEATS` per round with nothing capping the
#: total. `_bounded_timeout` (below) is threaded into every `tools._measure`
#: call this module makes — both `_timed` invocations and `_align_sizes` — so
#: the whole measurement phase shares one clock: exhausting it fails fast
#: with a coded, disclosed reason instead of running toward the whole tool's
#: 180s deadline (mcp_middleware.py's `TOOL_TIMEOUTS["verify_optimization"]`).
_MEASUREMENT_BUDGET_S = 120.0


def _bounded_timeout(timeout: int, deadline: float | None,
                     repeats: int) -> tuple[int | None, str | None]:
    """Cap a per-execution timeout to whatever remains of `deadline`
    (`time.monotonic()`-based, see `_MEASUREMENT_BUDGET_S`), or refuse
    outright once it is exhausted. Two layers, not one:

      - THIS function bounds one SIZE's `repeats` executions (all of which
        have the same expected cost, so dividing the remaining budget by
        `repeats` — not also by how many DIFFERENT sizes are in the same
        batch — is a fair split for that dimension: it stops one
        pathologically slow execution among the `repeats` from starving its
        own siblings, without artificially shrinking every size's budget by
        however many OTHER, possibly much cheaper, sizes happen to share
        the call). A batch with an ascending size list (the common case —
        `_timed`'s sizes only ever grow) can have wildly different costs
        per size; dividing by the total execution count across ALL of them
        starved the largest, legitimately-slower size even when the shared
        budget had plenty of room (measured: a real O(n^2) baseline's
        largest calibrated size needed ~6.3s but an even
        `repeats x len(sizes)` split left it only ~5.7s, timing out a call
        that was well within the actual remaining budget).
      - `tools._measure`'s own `deadline` parameter is the SECOND layer: it
        checks BEFORE STARTING each size's block (not each individual
        execution) and refuses outright once the shared clock is already
        exhausted, so a batch never even ATTEMPTS a size it cannot possibly
        afford, however many sizes came before it.

    `deadline=None` (a caller with no shared budget) returns `timeout`
    unchanged: this function is a pass-through, not a new requirement.
    """
    if deadline is None:
        return timeout, None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, "measurement deadline exceeded before this batch could run"
    return max(1, int(min(timeout, remaining / max(1, repeats)))), None


def _timed(code: str, language: str, sizes: list[int], timeout: int = 30,
          deadline: float | None = None) -> dict:
    """Measure min-of-repeats per size, auto-scaling each size INDEPENDENTLY
    until ITS OWN measurement clears the visibility floor.

    Keeps `all_runs_ms` per size (not just the min) alongside `durations_ms`:
    `_infer_speedup` needs the individual runs to test before-vs-after, not
    just the headline minimum `_speedup` uses for the ratio.

    Only the sizes still below `_VISIBILITY_FLOOR_MS` are re-measured each
    round (not the whole list, as the old combined check did) — a size that
    already cleared the floor costs nothing further, and a size that never
    clears it within `_MAX_RESCALE_ROUNDS` is left as measured; `_infer_speedup`
    reports it in `sizes_below_floor` rather than silently dropping it or
    letting a still-noisy sample drag the significance test down for sizes
    that DID clear the floor (see `_VISIBILITY_FLOOR_MS`'s comment for why the
    old combined check missed exactly this case).

    `deadline` (see `_MEASUREMENT_BUDGET_S`) bounds every `tools._measure`
    call this makes — the initial batch AND every rescale round — through
    `_bounded_timeout`. Exhausting it fails the WHOLE call with a coded,
    disclosed reason rather than silently truncating the rescale ladder: a
    genuinely slow baseline can spend real, n-dependent time on several
    rescale rounds with no alignment involved, so this backstop belongs here
    too, not only in `_align_sizes`.
    """
    call_timeout, err_msg = _bounded_timeout(timeout, deadline, REPEATS)
    if err_msg:
        return {"ok": False, "error": err_msg}
    runs, err = tools._measure(language, code, sizes, call_timeout, repeats=REPEATS,
                               deadline=deadline)
    if err:
        return {"ok": False, "error": err["error"]}
    for _ in range(_MAX_RESCALE_ROUNDS):
        pending = [i for i, r in enumerate(runs)
                  if r.get("duration_ms") is None or r["duration_ms"] < _VISIBILITY_FLOOR_MS]
        if not pending:
            break
        rescaled = [runs[i]["n"] * 10 for i in pending]
        call_timeout, err_msg = _bounded_timeout(timeout, deadline, REPEATS)
        if err_msg:
            return {"ok": False, "error": err_msg}
        sub_runs, err = tools._measure(language, code, rescaled, call_timeout, repeats=REPEATS,
                                       deadline=deadline)
        if err:
            break
        for i, r in zip(pending, sub_runs):
            runs[i] = r
    return {
        "ok": True,
        "sizes": [r["n"] for r in runs],
        "durations_ms": [r["duration_ms"] for r in runs],
        "all_runs_ms": [r["all_runs_ms"] for r in runs],
    }


def _remeasure_positions(data: dict, code: str, language: str, timeout: int,
                         fixes: list[tuple[int, int]],
                         deadline: float | None = None) -> dict:
    """Re-measure just the given `(index, new_n)` positions of `data`.

    One `_measure` call across every position that needs fixing (not one call
    per position), no auto-scale ladder: `_align_sizes` only calls this once
    it already knows the target n is a size the side being grown TOWARD has
    already run to completion and found floor-clearing — there is nothing
    left to discover, only to confirm.

    `deadline` (see `_MEASUREMENT_BUDGET_S`) is the SAME shared budget
    `_timed` uses, threaded through `_bounded_timeout` exactly the same way.
    """
    new_sizes = [n for _i, n in fixes]
    call_timeout, err_msg = _bounded_timeout(timeout, deadline, REPEATS)
    if err_msg:
        return {"ok": False, "error": err_msg}
    runs, err = tools._measure(language, code, new_sizes, call_timeout, repeats=REPEATS,
                               deadline=deadline)
    if err:
        return {"ok": False, "error": err["error"]}
    sizes = list(data["sizes"])
    durations = list(data["durations_ms"])
    all_runs = list(data["all_runs_ms"])
    for (i, _n), r in zip(fixes, runs):
        sizes[i] = r["n"]
        durations[i] = r["duration_ms"]
        all_runs[i] = r["all_runs_ms"]
    return {"ok": True, "sizes": sizes, "durations_ms": durations, "all_runs_ms": all_runs}


def _align_sizes(before: dict, after: dict, original: str, candidate: str,
                 language: str, timeout: int,
                 deadline: float | None = None) -> tuple[dict, dict]:
    """Grow the CANDIDATE side up to the baseline's n at each position where
    the two sides disagree and the baseline cleared the floor there; never
    grow the baseline. A position where the candidate scaled further than
    the baseline is left mismatched on purpose (baseline@bn vs candidate@an,
    disclosed as `size_after`); see below for why forcing
    alignment there is both unsafe and unnecessary.

    `_timed(original, ...)` and `_timed(candidate, ...)` each auto-scale every
    size INDEPENDENTLY (see `_timed`'s docstring) — so the two sides can now
    disagree at SOME positions and agree at others, not just "same length,
    every value different" the way a single combined rescale ladder produced.

    For a position where the two sides disagree, re-measure whichever side
    has the SMALLER n, at the OTHER side's n — one `_measure` call per side,
    batched across every position that needs fixing on that side — but ONLY
    when the LARGER side's own duration at its own n already cleared
    `_VISIBILITY_FLOOR_MS`: that n is then a real, informative, floor-
    clearing size, not just an arithmetic ceiling, and it is SAFE to ask the
    smaller side to run there too (unchanged from this function's original
    version — the whole other side of this decision is the fix below).

    It is NOT safe, and it turns out not to be NECESSARY either, when the
    larger side never cleared the floor there — i.e. it exhausted
    `_MAX_RESCALE_ROUNDS` in `_timed` without becoming visible (a genuine
    O(1) candidate at n up to `10**_MAX_RESCALE_ROUNDS` times the default, or
    symmetrically an unusually fast baseline). That n was never validated as
    useful (the side that reached it is still invisible there) or as safe
    for the other side (only the side that reached it has ever run at that
    n). An earlier version of this function reacted by forcing the OTHER
    side up to it anyway (unsafe: a real O(n) or O(n^2) baseline forced to
    10**4x its converged n) or, once that was fixed, by shrinking the
    exhausted side back down and marking the position excluded — which
    turned out to be WRONG whenever the smaller side is the baseline: a
    baseline that clears the floor at its own n, paired against a candidate
    too fast to register even after exhausting its entire rescale budget, is
    not a measurement failure. It is the single most decisive result this
    tool can produce, and excluding it drove `sizes_total` to 0 for the
    exact genuine-O(1)-win case this whole auto-scale system exists to
    certify (reviewer repro: per-size ratios 20x-2400x, `accepted=False`,
    "no size had enough comparable runs").

    So, per mismatched position:

      - candidate n < baseline n, and the baseline's own duration at its n
        cleared the floor: grow the candidate up to match it (a real,
        floor-clearing size the baseline already ran; the candidate is the
        arm expected to be cheaper there, and the shared budget bounds the
        case where it is not).
      - candidate n > baseline n (the candidate rescaled further, typically
        because it is too fast to register): do NOTHING here. Never grow
        the baseline to meet it — measured live, a real O(n^2) baseline
        asked to run at a padded O(1) candidate's rescaled n timed out and
        turned a genuine win into "baseline re-measurement failed". No
        re-measurement, no shrinking. `_speedup` and
        `_infer_speedup` (below) apply an ASYMMETRIC rule downstream: a
        position is usable whenever the BASELINE side is itself valid and
        floor-clearing, regardless of whether the candidate is — pairing
        baseline@bn against candidate@an directly (an >= bn is guaranteed
        whenever the candidate is the exhausted side, and a comparison at a
        LARGER candidate n than the baseline's is conservative by
        monotonicity: if candidate is still faster while doing MORE work,
        it is faster, full stop) — and BOTH `size` and `size_after` are
        recorded so the mismatched pairing is disclosed, never silently
        presented as same-n. A position is excluded (`sizes_below_floor`)
        only when the BASELINE side is the one that is invalid or never
        cleared the floor: shrinking it further (it already tried its own
        SMALLER starting sizes and failed) could not help, so no
        re-measurement is useful there either.

    Every re-measurement this function still makes goes through
    `_bounded_timeout`'s SHARED `deadline` (see `_MEASUREMENT_BUDGET_S`) —
    the residual risk of a re-measurement target that IS validated but is
    still expensive for the specific program catching up to it (a real
    O(n) or worse baseline can be cheap for a fast candidate and ruinous for
    itself at the same n) is bounded there, not here.
    """
    b_sizes, a_sizes = before.get("sizes"), after.get("sizes")
    if not b_sizes or not a_sizes or b_sizes == a_sizes:
        return before, after
    b_durs = before.get("durations_ms") or []
    fix_after: list[tuple[int, int]] = []
    for i, (bn, an) in enumerate(zip(b_sizes, a_sizes)):
        if bn == an:
            continue
        bd = b_durs[i] if i < len(b_durs) else None
        if bn < an:
            # `after` scaled more. NEVER grow the baseline to meet it: the
            # baseline is floor-clearing at bn (or `_timed` would have
            # rescaled it itself), so baseline@bn vs candidate@an is already
            # a valid, conservative comparison (`_comparable_positions`
            # discloses the pairing as `size_after`). Growing the SLOW arm
            # to the FAST arm's n is exactly the runaway this fix exists to
            # stop — measured live: a real O(n^2) baseline asked to run at
            # a padded O(1) candidate's 5x-rescaled n timed out at 21s and
            # turned a genuine 37x win into "baseline re-measurement
            # failed". Nothing to do at this position.
            continue
        else:
            # Symmetric: `before` scaled more. Grow `after` up to `bn` only
            # if `before` actually cleared the floor at its own bn.
            # Otherwise the BASELINE is the exhausted side — leave both as
            # they are; the asymmetric downstream rule excludes on a
            # baseline that never cleared the floor regardless of the
            # candidate, and shrinking it further cannot change that.
            if bd is not None and bd >= _VISIBILITY_FLOOR_MS:
                fix_after.append((i, bn))
    if fix_after:
        after = _remeasure_positions(after, candidate, language, timeout, fix_after, deadline)
        if not after.get("ok"):
            return before, after
    return before, after


def _comparable_positions(before: dict, after: dict) -> tuple[list[dict], list[dict]]:
    """Pair `before`/`after` per position under the ASYMMETRIC visibility
    rule `_speedup` and `_infer_speedup` both need — computed ONCE so the
    headline `speedup.ratio` and the significance test can never disagree
    about which positions counted (an earlier version let `_speedup` apply
    its own, looser floor than `_infer_speedup`'s, so the ratio could be
    built from durations the significance test had already excluded).

    A position is COMPARABLE whenever the BASELINE side is measurable and
    cleared `_VISIBILITY_FLOOR_MS` — regardless of whether the candidate
    did. A baseline that is genuinely slower tells you something real the
    moment it is visible; a candidate too fast to register even at a size
    the baseline needed real rescaling to reach is not a measurement
    failure, it is the most decisive evidence this tool can produce (see
    `_align_sizes`'s docstring — the mismatched `size`/`size_after` pair
    at such a position is intentional, never re-measured away). A position
    is EXCLUDED only when the baseline itself is unmeasurable, never
    cleared the floor, has no recorded measurement, or either side lacks
    the >=2 raw runs a significance test needs — this is the single set of
    reasons; `_speedup` and `_infer_speedup` differ only in what they DO
    with a comparable position; `sizes_total (_infer_speedup) + ratio's
    triple count (_speedup)` are identical.

    Returns `(comparable, excluded)`. Each `comparable` entry carries
    `size` (the baseline's n), `before_ms`, `after_ms`, `b_sample`,
    `a_sample`, and `size_after` ONLY when the candidate's n differs from
    `size` (the common, aligned case stays a single number — see
    `_align_sizes`). Each `excluded` entry is `sizes_below_floor`-shaped:
    `size`, `before_ms`, `after_ms` (either `None` when never recorded).
    `len(sizes) == len(comparable) + len(excluded)` always holds, counting
    `sizes` as `max(len(before["sizes"]), len(after["sizes"]))` positions.
    """
    b_sizes = before.get("sizes") or []
    a_sizes = after.get("sizes") or []
    b = before.get("durations_ms") or []
    a = after.get("durations_ms") or []
    b_runs = before.get("all_runs_ms") or []
    a_runs = after.get("all_runs_ms") or []
    total = max(len(b_sizes), len(a_sizes))

    comparable: list[dict] = []
    excluded: list[dict] = []
    for i in range(total):
        if i >= len(b_sizes) or i >= len(b) or i >= len(a):
            # No recorded baseline measurement at all for this position (a
            # before/after length mismatch) — still named, never silently
            # skipped.
            size = b_sizes[i] if i < len(b_sizes) else (a_sizes[i] if i < len(a_sizes) else i)
            excluded.append({"size": size, "before_ms": None, "after_ms": None})
            continue
        bn = b_sizes[i]
        an = a_sizes[i] if i < len(a_sizes) else bn
        bb, aa = b[i], a[i]
        b_sample = b_runs[i] if i < len(b_runs) else []
        a_sample = a_runs[i] if i < len(a_runs) else []
        # The asymmetric rule: only the BASELINE side's floor/measurability
        # excludes a position. `aa`'s own visibility floor is deliberately
        # NOT checked here — that was the bug (reviewer repro: a genuine
        # O(1) candidate drove `sizes_total` to 0 because the OLD, symmetric
        # rule excluded on EITHER side being below the floor). `aa > 0.0`
        # only, same as `_speedup` always required: a 0ms optimized run is
        # reachable and would raise `ZeroDivisionError` on the ratio.
        if (bb is None or bb <= 1.0 or bb < _VISIBILITY_FLOOR_MS
                or aa is None or aa <= 0.0):
            excluded.append({"size": bn, "before_ms": bb, "after_ms": aa})
            continue
        # Raw-run sufficiency (>= 2 runs a side for a significance test) is
        # NOT a comparability reason: the headline ratio needs only the
        # medians (`_speedup` is called on duration-only dicts by callers
        # and tests alike). `_infer_speedup` checks the samples itself and
        # files a comparable-but-untestable position under
        # `sizes_below_floor`, so its invariant still holds.
        entry = {"size": bn, "before_ms": bb, "after_ms": aa,
                 "b_sample": b_sample, "a_sample": a_sample}
        if an != bn:
            entry["size_after"] = an
        comparable.append(entry)
    return comparable, excluded


def _speedup(before: dict, after: dict) -> dict:
    """Ratio after/before per size (median used as the headline).

    Built from EXACTLY the positions `_comparable_positions` (shared with
    `_infer_speedup`) calls comparable — the headline ratio can no longer be
    computed from a duration the significance test excluded (an earlier
    version applied its own, looser floor independently).
    """
    comparable, _excluded = _comparable_positions(before, after)
    if not comparable:
        return {"ratio": None, "measurable": False, "per_size": [],
                "reason": "no size where both runs were measurable "
                          "(baseline below the visibility floor, or the "
                          "optimized run measured 0ms) — see "
                          "inference.sizes_below_floor"}
    ratios = [e["before_ms"] / e["after_ms"] for e in comparable]
    import statistics
    median = statistics.median(ratios)
    per_size = []
    for e in comparable:
        row = {"n": e["size"], "before_ms": e["before_ms"], "after_ms": e["after_ms"],
              "ratio": round(e["before_ms"] / e["after_ms"], 2)}
        if "size_after" in e:
            # The candidate's n differs from the baseline's — an
            # intentionally unaligned, disclosed pairing (see
            # `_align_sizes`'s docstring), not a mislabelled row.
            row["n_after"] = e["size_after"]
        per_size.append(row)
    return {
        "ratio": round(median, 2),
        "measurable": True,
        "per_size": per_size,
    }


#: Two-tailed significance level for the per-size Mann-Whitney test. The test
#: itself is one-sided (`alternative="less"`: after < before), but 0.05 is the
#: conventional name for "this could plausibly be noise" regardless of
#: sidedness, and callers reading `inference.alpha` expect the familiar number.
ALPHA = 0.05

#: `verify_optimization`'s own default, promoted to a module constant so
#: `_infer_speedup` can share it (see (e) below) rather than the two
#: functions carrying independently-chosen literals that could silently
#: drift apart.
DEFAULT_MIN_SPEEDUP = 1.15

#: Below this many observations A SIDE, a `normal_approximation`-method
#: p-value (see `stats.mann_whitney_u`) is not treated as informative enough
#: to count toward a rejection WHEN the two samples' ranges overlap (see
#: `_ranges_overlap`) — even when it clears alpha. `REPEATS` (5) is BELOW
#: this floor on purpose: the approximation is asymptotic, and `stats.py`'s
#: own module docstring says so explicitly — "at n=m=5 (10 observations) it
#: is a rough guide, not a number worth calling alpha=0.05 against" — yet
#: ties (coarse wall-clock resolution, or two genuinely-equal runs) route
#: straight to it regardless of n, and wall-clock timings tie constantly at
#: REPEATS=5. This is what closed a REAL false accept: CI measured
#: IDENTICAL before/after code as accepted=True at ratio 1.21, sizes_rejecting
#: 2/3, both "rejecting" sizes tied and normal_approximation at n=5 a side
#: (p=0.023, p=0.047) — an approximation the module that computed it does not
#: trust at that sample size, deciding the vote. The overlap qualifier
#: (`_ranges_overlap`) matters in practice, not just in theory: gating on the
#: method+n alone (no overlap check) measured LIVE as nearly BLINDING the
#: tool — a real ~16x algorithmic win accepted only 1/10 runs, because this
#: tool's own integer-millisecond timings tie constantly even for a
#: genuinely, unambiguously fast candidate (every one of its runs can land on
#: the same rounded ms with zero overlap against the baseline). A size
#: excluded here is disclosed in `sizes_below_floor` with a `reason`, not
#: silently dropped (see codecalc issue #285, which is about the missing
#: `method` field this constant now acts on, not just surfaces).
_NORMAL_APPROX_MIN_N = 8

#: At or below this many COUNTED sizes, `_accept_decision` requires EVERY one
#: to reject rather than a bare majority. With few, independent per-size
#: tests, "a majority" (e.g. 2 of 3) still lets ONE false-positive size carry
#: the accept vote — exactly the CI incident this whole layered fix closes
#: (2/3 sizes "rejecting" on IDENTICAL code, majority satisfied, accepted).
#: Requiring unanimity at small k needs no per-test alpha correction to
#: control the family-wise error: three independent tests that must ALL
#: reject at alpha=0.05 have a chance false-accept probability far below
#: 0.05 on their own (assuming independence, ~0.05^3). See `_fwer_correction`.
_FWER_UNANIMITY_MAX = 3

#: Fewer COUNTED sizes than this can never yield accepted=True. Unanimity
#: over ONE size is no correction at all: it is a single uncorrected test at
#: the nominal alpha — exactly the shape the layered fix above exists to
#: refuse, reached by a different road (the other planned sizes excluded as
#: tied/overlapping/under-populated, the one survivor rejecting by chance).
#: Measured by adversarial review with a 500,000-trial Monte Carlo of
#: IDENTICAL code (Gaussian jitter, integer ms, REPEATS=5, 3 sizes): a
#: 0.357% false-accept rate, 97.6% of it through a lone surviving size. A
#: single testable size is a data-poverty condition, and the honest verdict
#: for it is "not enough was measurable to certify", stated as such in
#: `decision_basis`, not "verified faster". A caller who hits it can add
#: sizes (`REPEATS` is a module constant, not a tool parameter); the
#: `sizes_below_floor` reasons say which sizes fell out and why.
_MIN_COUNTED_SIZES = 2


def _fwer_correction(k: int, alpha: float) -> tuple[float, str]:
    """Family-wise error control for the "how many of `k` counted sizes must
    reject" decision `_accept_decision` makes — (d) in the false-accept fix.

    Two regimes, chosen by `k` (the number of sizes that survived every
    other exclusion in `_infer_speedup` — visibility floor, testability,
    normal-approximation reliability):

      k <= `_FWER_UNANIMITY_MAX`   No per-test correction; every counted
                                   size must reject (unanimity). Cheap
                                   sizes make unanimity itself the control —
                                   see `_FWER_UNANIMITY_MAX`'s docstring.

      k >  `_FWER_UNANIMITY_MAX`   `alpha / k` per size (Bonferroni), and
                                   still only a MAJORITY of those harder-to-
                                   clear tests is required. Unanimity across
                                   many sizes would be brittle against
                                   ordinary per-size timing noise (a single
                                   size out of, say, 8 landing on a bad
                                   sample would veto an otherwise-decisive
                                   win); Bonferroni-correcting each test's
                                   own alpha keeps the chance of ANY single
                                   size falsely rejecting at ~`alpha` total
                                   across all `k` of them, so requiring only
                                   a majority of those corrected tests still
                                   keeps a chance false accept much rarer
                                   than the uncorrected majority rule this
                                   whole fix replaces.

    Returns `(effective_alpha, correction_name)`. `k <= 0` returns
    `(alpha, "n/a")` — `_fwer_satisfied` always returns False for
    `sizes_total == 0` regardless of what this returns, so the value here is
    never actually consulted, but IS documented.
    """
    if k <= 0:
        return alpha, "n/a"
    if k <= _FWER_UNANIMITY_MAX:
        return alpha, "unanimity"
    return alpha / k, "bonferroni"


def _ranges_overlap(a_sample: list[float], b_sample: list[float]) -> bool:
    """Do two raw-run samples' VALUE RANGES overlap at all — used to decide
    whether (b)'s normal-approximation reliability gate actually applies.

    A tie ANYWHERE in the combined sample routes `stats.mann_whitney_u` to
    the normal approximation (see `stats.py`), and this tool's own wall-clock
    timings (integer milliseconds) tie constantly — measured directly: a
    genuine ~6x O(1) win's candidate arm alone produced `[42, 42, 45, 53,
    42]`, three ties, from ordinary process-spawn jitter clustering on the
    same rounded millisecond, with NO overlap at all against the baseline's
    `[243..266]` range. Excluding EVERY tied result below
    `_NORMAL_APPROX_MIN_N` (the first version of this fix) is not just
    conservative, it is nearly BLIND: measured live on this box, that
    version accepted a real 16x algorithmic win only 1 time in 10 runs,
    because REPEATS=5 at integer-ms resolution ties on almost every size
    regardless of whether the two arms are genuinely different.

    What actually makes the normal approximation's tie correction untrustworthy
    at n=5 is not "a tie happened somewhere" — it is a comparison the timer
    could not have ordered even in principle: two runs, one from EACH side,
    landing on the same rounded millisecond (or the two sides' ranges
    crossing at all). That is the literal, operational meaning of "below the
    timer's resolution" — a tie confined to duplicate readings WITHIN one
    side (the fast-candidate case above) does not create that ambiguity: the
    two arms are still cleanly, unambiguously ordered.

    Returns True when the two samples' `[min, max]` ranges intersect
    (inclusive — landing on the SAME boundary value counts as ambiguous,
    not narrowly avoided).
    """
    return not (max(a_sample) < min(b_sample) or max(b_sample) < min(a_sample))


def _fwer_satisfied(sizes_total: int, sizes_rejecting: int, correction: str | None) -> bool:
    """Does `sizes_rejecting` clear the bar `correction` (see
    `_fwer_correction`) demands out of `sizes_total`? Shared by
    `_infer_speedup` (to state the rule in `decision_basis`) and
    `_accept_decision` (to actually gate `accepted`) so the two can never
    disagree about what "enough" means.

    `correction=None` (an older or hand-built `inference` dict that predates
    this fix — see tests/test_grades.py's fixtures) falls back to the
    ORIGINAL bare-majority rule rather than raising: a caller that never
    knew about unanimity/Bonferroni gets the same answer it always did.
    """
    if sizes_total == 0:
        return False
    if correction is not None and sizes_total < _MIN_COUNTED_SIZES:
        # A lone counted size is a single uncorrected test — see
        # `_MIN_COUNTED_SIZES`. Only the legacy (correction=None) path keeps
        # its original answer, for the same reason it keeps the majority rule.
        return False
    if correction == "unanimity":
        return sizes_rejecting == sizes_total
    return sizes_rejecting * 2 > sizes_total


def _infer_speedup(before: dict, after: dict, alpha: float = ALPHA,
                   min_speedup: float = DEFAULT_MIN_SPEEDUP) -> dict:
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
    a real complexity-class difference as within-test noise. The per-size
    verdicts are combined into ONE decision by `_fwer_satisfied`: every
    counted size must reject when there are `_FWER_UNANIMITY_MAX` or fewer,
    a majority at a Bonferroni-corrected alpha above that, and never fewer
    than `_MIN_COUNTED_SIZES` counted sizes at all. (The original rule here
    was a bare "majority of sizes reject"; the CI incident described below is
    what that rule let through.)

    Comparability is `_comparable_positions`'s ASYMMETRIC rule: a position
    counts whenever the BASELINE is itself measurable and floor-clearing,
    regardless of the candidate — see that function's docstring for why the
    OLD, symmetric "either side below the floor excludes" rule was a bug,
    not a safety margin (it drove `sizes_total` to 0 for a genuine O(1) win).
    A comparable position where the candidate's n differs from the
    baseline's (the candidate exhausted its own rescale budget without
    clearing the floor — see `_align_sizes`) is tested directly: the
    baseline's sample at its own n against the candidate's sample at ITS OWN
    (larger) n, which is CONSERVATIVE by monotonicity — if the candidate is
    still faster while doing at least as much work, it is faster — and
    `size_after` in that entry discloses the mismatch rather than silently
    presenting it as same-n. Every excluded position is named in
    `sizes_below_floor` (with whatever `before_ms`/`after_ms` it has; `None`
    where a duration was never available), never silently dropped.
    `len(sizes) == len(inference["per_size"]) +
    len(inference["sizes_below_floor"])` holds for every result this
    function returns: a size is read as a pass, a fail, or an explicit
    "excluded, and here is why" — never as nothing at all.

    Three further gates, all part of the same false-accept fix (a live CI
    run certified IDENTICAL before/after code as `accepted=True`):

      (b) a size whose test used `stats.mann_whitney_u`'s `normal_approximation`
          method (routed there by a tie — see `stats.py`'s module docstring)
          with fewer than `_NORMAL_APPROX_MIN_N` observations a side does not
          count toward `sizes_rejecting`, however small its p-value: the
          approximation itself is not trustworthy there, per `stats.py`'s own
          reasoning. Excluded to `sizes_below_floor` with a `reason` (#285).
      (c) a size below `stats.min_testable_n(alpha)` observations a side is
          STRUCTURALLY unable to reject at `alpha`, so it cannot be allowed to
          count against a majority/unanimity vote it could never contribute a
          rejection to (replaces the old flat `< 2` guard). Also excluded to
          `sizes_below_floor` with a `reason` (#284).
      (e) a size counts as "rejecting" only when ITS OWN ratio (`before_ms /
          after_ms`, the same computation `_speedup` uses for the headline)
          also clears `min_speedup` — a size can be "significant" (the
          samples don't overlap much) while the actual difference is tiny;
          `p_value < alpha` alone is not "this size is a speedup of the size
          the caller asked for".

    `method` and `ratio` are carried on every surviving `per_size` row
    (method: (a), #285 — a caller can now tell an exact p from an
    asymptotic one; ratio: needed to disclose (e)'s own gate, and a strict
    superset of what `_speedup`'s `per_size` already reports per position).

    (d), the family-wise correction on how many surviving sizes must
    reject, is `_fwer_correction`/`_fwer_satisfied` — see those docstrings.
    `correction` and `effective_alpha` are always present here alongside the
    original `alpha` (which stays the NOMINAL, uncorrected level throughout
    — `effective_alpha` is what a size's OWN p-value is actually compared to
    for `sizes_rejecting`).
    """
    comparable, excluded = _comparable_positions(before, after)
    # (c): a size below the smallest n whose exact p COULD be < alpha is not
    # merely untested, it is structurally incapable of rejecting — counting
    # it against the vote it can never contribute a rejection to is exactly
    # backwards (codecalc #284: dropping one such size flipped a genuine 10x
    # win from rejected to accepted). Replaces the old flat `< 2` guard,
    # computed from `alpha` rather than hard-coded so the two never drift.
    min_n = stats.min_testable_n(alpha)
    testable = []
    for e in comparable:
        if len(e["b_sample"]) < min_n or len(e["a_sample"]) < min_n:
            excluded.append({
                "size": e["size"], "before_ms": e["before_ms"], "after_ms": e["after_ms"],
                "reason": (f"fewer than {min_n} runs per side ({len(e['b_sample'])} "
                          f"before / {len(e['a_sample'])} after) — the smallest n "
                          f"whose exact one-sided p can be < alpha={alpha} at all "
                          f"is {min_n} (1/C(2n,n)); a smaller sample cannot reject "
                          f"the null no matter how clean the separation"),
            })
        else:
            testable.append(e)
    comparable = testable

    per_size = []
    for e in comparable:
        mwu = stats.mann_whitney_u(e["a_sample"], e["b_sample"], alternative="less")
        n_before, n_after = len(e["b_sample"]), len(e["a_sample"])
        # (b): a tie routed this size to the normal approximation, and that
        # approximation is asymptotic — `stats.py`'s own docstring: not "a
        # number worth calling alpha against" at n=5. Below
        # `_NORMAL_APPROX_MIN_N` a side, distrust it — but ONLY when the two
        # samples' ranges actually overlap (`_ranges_overlap`): a tie
        # confined to duplicate readings WITHIN one side (this tool's own
        # measurements do this constantly at integer-ms resolution — see
        # that function's docstring) does not make the comparison ambiguous,
        # and excluding it anyway measured as nearly BLINDING the tool to
        # genuine wins, not just conservative.
        if (mwu["method"] == "normal_approximation"
                and (n_before < _NORMAL_APPROX_MIN_N or n_after < _NORMAL_APPROX_MIN_N)
                and _ranges_overlap(e["a_sample"], e["b_sample"])):
            excluded.append({
                "size": e["size"], "before_ms": e["before_ms"], "after_ms": e["after_ms"],
                "reason": (f"a tie routed this size to the normal approximation "
                          f"(stats.mann_whitney_u's method) AND the two samples' "
                          f"ranges overlap — below {_NORMAL_APPROX_MIN_N} runs per "
                          f"side ({n_before} before / {n_after} after here) that is "
                          f"below the timer's resolution: the timer cannot always "
                          f"say which side a given pair of runs favoured — see "
                          f"stats.py's own module docstring on why n=5 is 'not a "
                          f"number worth calling alpha against'"),
            })
            continue
        rb = stats.rank_biserial_correlation(mwu["u"], mwu["n1"], mwu["n2"])
        row = {
            "size": e["size"],
            "n_before": n_before,
            "n_after": n_after,
            "u": mwu["u"],
            "p_value": mwu["p_value"],
            "rank_biserial": round(rb, 4),
            "method": mwu["method"],
            "ratio": round(e["before_ms"] / e["after_ms"], 2),
        }
        if "size_after" in e:
            row["size_after"] = e["size_after"]
        per_size.append(row)

    sizes_total = len(per_size)
    effective_alpha, correction = _fwer_correction(sizes_total, alpha)
    # (e): a size only counts as rejecting when its OWN ratio also clears
    # min_speedup — "the samples barely overlap" is not "this size sped up
    # by the amount the caller asked for".
    sizes_rejecting = sum(1 for r in per_size
                          if r["p_value"] < effective_alpha and r["ratio"] >= min_speedup)
    if sizes_total == 0:
        decision_basis = "no size had enough comparable runs for a significance test"
    elif sizes_total < _MIN_COUNTED_SIZES:
        decision_basis = (f"only {sizes_total} size was testable (it "
                          f"{'rejects' if sizes_rejecting else 'does not reject'} "
                          f"the null at alpha={alpha}), and a single counted size "
                          f"is one uncorrected test, not a verified speedup: at "
                          f"least {_MIN_COUNTED_SIZES} counted sizes are required "
                          f"— add sizes so more of them clear "
                          f"the floor")
    elif correction == "unanimity":
        decision_basis = (f"{sizes_rejecting}/{sizes_total} size(s) reject the null "
                          f"(after not faster, at its own ratio >= {min_speedup}x) at "
                          f"alpha={alpha}; {sizes_total} counted size(s) (<= "
                          f"{_FWER_UNANIMITY_MAX}) requires EVERY one to reject, not "
                          f"a bare majority — with this few independent tests a "
                          f"majority still lets one false-positive size carry the "
                          f"vote")
    else:
        decision_basis = (f"{sizes_rejecting}/{sizes_total} size(s) reject the null "
                          f"(after not faster, at its own ratio >= {min_speedup}x) at "
                          f"the Bonferroni-corrected alpha={effective_alpha:.4g} "
                          f"(alpha={alpha} / {sizes_total} sizes); a majority of "
                          f"those corrected tests is required")
    if excluded:
        decision_basis += (f"; {len(excluded)} size(s) excluded — the baseline "
                           f"was unmeasurable, never cleared the "
                           f"{_VISIBILITY_FLOOR_MS}ms visibility floor within "
                           f"the rescale budget, had fewer than {min_n} runs per "
                           f"side, or used the normal approximation on fewer than "
                           f"{_NORMAL_APPROX_MIN_N} runs per side — see "
                           f"sizes_below_floor")
    return {
        "test": "mann_whitney_u",
        "alternative": "after_faster",
        "alpha": alpha,
        "effective_alpha": effective_alpha,
        "correction": correction,
        "per_size": per_size,
        "sizes_rejecting": sizes_rejecting,
        "sizes_total": sizes_total,
        "sizes_below_floor": excluded,
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
      - `_fwer_satisfied` on `_infer_speedup`'s per-size Mann-Whitney test:
        EVERY counted size rejects "after not faster" (<= 3 counted sizes),
        or a MAJORITY do at the Bonferroni-corrected alpha (> 3) — see
        `_fwer_correction`'s docstring for why the cutover and why each
        regime is enough on its own.

    The last bullet is the fix this function exists for, TWICE now.
    `ratio >= min_speedup` used to be the whole decision, but with REPEATS
    runs per size the smallest one-sided p a single size can ever produce is
    1/C(2*REPEATS, REPEATS) — no size could clear alpha=0.05 by construction
    at REPEATS=3, so "the median ratio cleared 1.15x" was an arithmetic fact
    about noisy timings, not evidence the difference was real (see
    tests/test_translation_verify.py's false-accept-rate assertion on
    IDENTICAL before/after code). A bare MAJORITY of sizes rejecting was the
    first fix, and it was not enough on its own: a live CI run on a hosted
    macOS sandbox measured IDENTICAL before/after code as accepted=True at
    ratio 1.21 with 2 of 3 sizes "rejecting" — both of them a tie routed to
    the normal approximation at REPEATS=5, a sample size `stats.py`'s own
    docstring says is not trustworthy there. `_infer_speedup` now excludes
    that kind of vote entirely (see its docstring, gates (b)/(c)/(e)); this
    function's OWN fix is (d) — replacing "a bare majority, always" with the
    unanimity-or-Bonferroni split `_fwer_satisfied` applies, so a small
    battery of sizes cannot be swung by one noisy vote the way 2-of-3 was.

    A ratio <= 1, a min_speedup <= 1, or an FWER-unsatisfied test result can
    NEVER yield accepted=True; the grade side
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
    if not _fwer_satisfied(sizes_total, sizes_rejecting, inference.get("correction")):
        return False, (f"measured {ratio}x clears {min_speedup}x, but the "
                       f"one-sided significance test does not: "
                       f"{inference.get('decision_basis')} — could be noise")
    return True, (f"verified faster: {ratio}x median, {sizes_rejecting}/"
                  f"{sizes_total} size(s) significant at "
                  f"alpha={inference.get('effective_alpha', inference.get('alpha'))}")


def verify_optimization(original: str, candidate: str, language: str,
                        test_inputs: list[str] | None = None,
                        sizes: list[int] | None = None,
                        min_speedup: float = DEFAULT_MIN_SPEEDUP,
                        timeout: int = 30) -> dict:
    """Decide whether `candidate` is a genuine optimisation of `original`.

    Two gates, both measured, neither of them an opinion:

      correctness  both programs run on the same inputs and must agree
      speed        both are timed at the same sizes (`REPEATS` runs each);
                   the median ratio must clear `min_speedup`, EACH counted
                   size's OWN ratio must also clear it, AND a one-sided
                   Mann-Whitney U test must reject "candidate is not
                   faster" at every counted size (<= 3 of them) or a
                   majority at a Bonferroni-corrected alpha (> 3) — see
                   `_infer_speedup`, `_fwer_correction` and
                   `_accept_decision`.

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

    Each size's own timing is auto-scaled independently until it clears a
    visibility floor (`_timed`) before the test ever sees it — a size whose
    work stays too fast to measure through the whole rescale budget is
    excluded from the majority vote and named in `inference.sizes_below_floor`
    rather than tested on a noisy sample or silently dropped (see
    `_VISIBILITY_FLOOR_MS`'s docstring for the bug this closes).

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

    # One shared wall-clock budget for the ENTIRE measurement phase — both
    # `_timed` calls (including every auto-scale round each makes) and
    # `_align_sizes` — not a fresh one per phase. See
    # `_MEASUREMENT_BUDGET_S`'s docstring for why a per-phase budget left a
    # gap: `_timed`'s own rescale rounds, on a genuinely slow baseline, can
    # spend real n-dependent time with no alignment involved at all.
    deadline = time.monotonic() + _MEASUREMENT_BUDGET_S

    before = _timed(original, language, size_list, timeout=timeout, deadline=deadline)
    if not before.get("ok"):
        return {"ok": False, "error": f"baseline measurement failed: {before.get('error')}"}
    after = _timed(candidate, language, size_list, timeout=timeout, deadline=deadline)
    if not after.get("ok"):
        return {"ok": False, "error": f"candidate measurement failed: {after.get('error')}"}

    # Each side's own auto-scale in `_timed` runs independently, so they can
    # converge on different sizes (see `_align_sizes`'s docstring). Bring the
    # two into agreement where it is safe and useful to do so BEFORE either
    # `_speedup` or `_infer_speedup` sees these dicts — a position left
    # deliberately mismatched (see `_align_sizes`) is handled explicitly by
    # both, via `_comparable_positions`'s `size_after` disclosure, never
    # silently mislabelled.
    before, after = _align_sizes(before, after, original, candidate, language,
                                 timeout, deadline=deadline)
    if not before.get("ok"):
        return {"ok": False, "error": f"baseline re-measurement failed: {before.get('error')}"}
    if not after.get("ok"):
        return {"ok": False, "error": f"candidate re-measurement failed: {after.get('error')}"}

    sp = _speedup(before, after)
    inference = _infer_speedup(before, after, min_speedup=min_speedup)
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
