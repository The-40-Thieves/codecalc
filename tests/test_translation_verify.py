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

from codecalc import executor, grades, optimization, translation
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


def _run843(ratio, min_speedup):
    """Call verify_optimization with a MEASURED before/after that yields `ratio`.

    _speedup's ratio is before/after and keeps only before>1.0, after>0.0; pick
    (100*ratio, 100) so the single measured pair's median ratio is exactly
    `ratio`.
    """
    optimization.verify_translation = lambda *a, **k: {"passed": True, "matched": 2, "total": 2}
    _seq = iter([
        {"ok": True, "sizes": [1000], "durations_ms": [100.0 * ratio]},
        {"ok": True, "sizes": [1000], "durations_ms": [100.0]},
    ])
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
      _win843.get("accepted") is True and _win843.get("reason") == "verified faster",
      f"-> accepted={_win843.get('accepted')} {_win843.get('reason')!r}")
# The pure decision, checked directly for the unmeasurable case.
check("an unmeasurable speedup is never accepted",
      optimization._accept_decision({"ratio": None, "measurable": False}, 1.15)[0] is False)


# ═══ identical code plus timing noise is never certified ════════════════════
# The unchanged-candidate guarantee used to be checked through a LIVE timing
# measurement of FAST vs. FAST (identical O(1) code). That program does almost
# no work, so its runtime is dominated by fixed process-startup jitter and its
# measured before/after median ratio swings wildly: reproduced here across 30
# runs it ranged 0.71x to 1.53x, and 2/30 exceeded the default min_speedup=1.15 and
# were CERTIFIED "verified faster" — a flaky false accept, not a bug in the
# accept logic (those runs genuinely MEASURED >1.15; an epsilon over min_speedup
# would not have caught 1.53x and would only harm genuine small wins). The fix
# is to assert the DECISION against an INJECTED within-noise ratio (
# pattern), so the guarantee never depends on runner jitter. A realistic default
# threshold (1.15) already rejects a within-noise ratio: the accept logic is
# sound — what was flaky was measuring noise and feeding it back in.
_noise845 = _run843(1.03, 1.15)
check("a within-noise ratio (1.03) under a realistic threshold is NOT accepted",
      _noise845.get("accepted") is False, f"-> {_noise845.get('reason')!r}")
check("an unchanged/noisy candidate is never certified 'verified faster'",
      _noise845.get("reason") != "verified faster")
check("and it is never graded cross_checked",
      grades.grade_verify_optimization(_noise845, "python3").get("grade") != grades.CROSS_CHECKED)


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

    # The unchanged-candidate rejection is asserted DETERMINISTICALLY in the
    # block above, not here: a live FAST-vs-FAST timing measures noise
    # (identical O(1) code, runtime dominated by process-startup jitter) and its
    # median ratio occasionally exceeds min_speedup, flaking this assertion. The
    # rejections below carry what an optimiser that fabricates wins cannot: WHICH
    # gate failed.
    o = optimization.verify_optimization(SLOW, "print(999)", "python3",
                                         test_inputs=["10", "100"])
    check("a faster-but-wrong candidate is rejected on correctness",
          o.get("accepted") is False and o.get("reason") == "not equivalent",
          f"-> {o.get('reason')!r}")
    check("  ...and its speed was never measured", "speedup" not in o,
          "-> a faster wrong answer is not an optimisation")
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
