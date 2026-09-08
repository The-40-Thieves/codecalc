"""Cross-language verification. No LLM anywhere in this module.

verify_translation: given TWO programs the caller already has — a source and a
claimed port — run both on the same inputs and decide whether they agree.
compare_edge_cases: run the same logic in N languages and report where they
diverge.

The tool used to generate the port itself, by calling a second, separately
configured model. That was backwards for a calculator whose caller is already
a language model: it made the differentiated half — the executor proving
equivalence — reachable only through a weaker proposer, and it meant the two
most distinctive tools were the only two that did not work on a fresh install.

The caller proposes. This module decides. See classify_case/aggregate for the
rules, which are deliberately separable from the sandbox so they can be tested
without two runtimes and a built binary.

Both tools compare stdout through the SAME `_normalize` — see
NORMALIZE_TOLERANCE for exactly what it does and does not treat as equivalent.
A per-case result also carries each side's RAW (un-normalized) stdout
alongside the normalized one, so a caller can see what a program actually
wrote even when the normalized comparison calls it a match (codecalc #286).
"""

from __future__ import annotations

from . import errors, executor

#: default edge-case inputs (each is stdin for the program)
DEFAULT_EDGE_INPUTS = ["", "0", "1", "-1", "10", "100", "0.1\n0.2"]



def _run(language: str, code: str, stdin: str, timeout: int = 15) -> dict:
    return executor.execute(language, code, stdin=stdin, timeout=timeout)


#: The ONLY cross-platform differences `_normalize` tolerates. Kept as one
#: named constant rather than left implicit in the function body, so there is
#: a single place that says what "the same output" means here.
#:
#: `str.splitlines()` used to be the implementation, and it splits on far more
#: than `\n`: VT (0x0B), FF (0x0C), FS/GS/RS (0x1C-0x1E), NEL (0x85), U+2028
#: LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR are ALL line boundaries to
#: Python, and the old `"\n".join(...)` rewrote every one of them to `\n` —
#: seven distinct characters silently canonicalised into one, so a source
#: printing a real `\n` and a port printing U+2028 compared equal (codecalc
#: issue #286). Almost no language treats those seven as line terminators on
#: output; they are DATA a program chose to emit, and a difference in them is
#: a difference this tool exists to catch, not a formatting quirk to smooth
#: over. `str.rstrip()`/`str.strip()` have the same problem one level down:
#: bare `.rstrip()` trims the same VT/FF/NEL/U+2028-class whitespace off the
#: end of a line as SPACE ALSO. `split("\n")` and `rstrip(" \t")` below name
#: exactly what is tolerated instead of reaching for "whitespace" in general.
NORMALIZE_TOLERANCE = (
    "\\r\\n and \\r are folded to \\n (line-ending convention); trailing "
    "spaces/tabs are trimmed per line; trailing blank lines at the end of "
    "the whole output are trimmed. Nothing else — no other character "
    "Python calls a line or whitespace boundary is touched."
)


def _normalize(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip(" \t") for line in s.split("\n")).strip(" \t\n")


def classify_case(a: dict, b: dict) -> tuple[str, str]:
    """Classify one (source_result, target_result) pair. Pure — no I/O.

    Returns (outcome, reason) where outcome is one of:

      "match"         positive evidence of equivalence: both programs RAN and
                      produced identical stdout.
      "mismatch"      positive evidence of NON-equivalence.
      "inconclusive"  no evidence either way. Critically, this is NOT a pass.

    The third outcome is the whole point. The previous implementation had only
    two, so every case had to be forced into one of them, and "both programs
    failed" was forced into "match" on the reasoning that they were in the same
    behaviour class. They are not: a Python ZeroDivisionError and a Go nil-map
    panic are both a non-zero exit with empty stdout, and so is a program that
    was never a translation of anything. Two failures are two absences of
    output, and an absence of output is not evidence.

    That mattered most in the case where the gate mattered most. If the source
    language's runtime is missing, the source fails on EVERY input, so every
    case "matched" and any LLM output whatsoever was certified as verified.
    """
    a_ok, b_ok = bool(a.get("ok")), bool(b.get("ok"))
    a_phase, b_phase = a.get("phase"), b.get("phase")

    # A source that will not even build gives nothing to compare against. Check
    # this BEFORE the target's compile status: if the source is broken, the
    # target's state tells us nothing about the translation.
    if not a_ok and a_phase == "compile":
        return "inconclusive", "the source program failed to compile; there is nothing to verify against"

    # A translation that does not compile is wrong, full stop. This is never
    # "the same behaviour" as anything — it is the single most common way an
    # LLM translation fails, and the old code scored it as a match whenever the
    # source happened to error too.
    if not b_ok and b_phase == "compile":
        return "mismatch", "the translation failed to compile"

    if a_ok and b_ok:
        a_out, b_out = _normalize(a.get("stdout", "")), _normalize(b.get("stdout", ""))
        if a_out != b_out:
            return "mismatch", "both ran; stdout differs"
        if a_out == "":
            # Both succeeded and both produced NOTHING. Agreeing on an
            # absence of output is not evidence of a match: an exit-0 result
            # with empty stdout is indistinguishable here from one whose
            # output was silently lost (issue #42 — compare_execution saw
            # `node` return ok=True/False with empty stdout on windows-latest
            # while sibling languages produced real output from the same
            # snippet). Scoring two such results as a "match" would let that
            # race certify a translation, or a "winner", on exactly the case
            # where nothing was actually compared — the same blind spot as
            # "both failed" above, one level up: a program that never
            # produced output gives no more evidence than one that crashed.
            return "inconclusive", ("both programs succeeded but produced no "
                                    "output; an absence of output is not "
                                    "evidence of a match")
        return "match", ""

    if a_ok != b_ok:
        which = "the translation" if a_ok else "the source program"
        return "mismatch", f"{which} failed while the other succeeded"

    # Both failed at run time. Possibly both "correctly" raise on this input —
    # but nothing observable here distinguishes that from two unrelated errors,
    # so it is recorded as no evidence rather than guessed either way.
    return "inconclusive", "both programs failed at run time; their errors are not comparable"


def aggregate(outcomes: list[tuple[str, str]]) -> dict:
    """Turn per-case outcomes into a verdict. Pure — no I/O.

    A translation passes only when there is REAL evidence for it: no mismatch,
    and at least one case where both programs actually ran and agreed. A run
    that is entirely inconclusive fails with a reason, because "we could not
    check" and "we checked and it was fine" are different answers and the
    caller is entitled to know which one it got.

    `ok` is always True here — it means "the tool completed and produced a
    real answer", never the verdict. `passed=False` (a mismatch, or nothing
    conclusive) is still `ok=True`: the tool did its job and reported a real
    result, the same distinction `verify_optimization`'s own `{"ok": True,
    "accepted": False, ...}` early return already draws between completion
    and the claim being verified. This was a real omission, not a
    documented shape: `aggregate` never set the key at all before, which
    made `verify_translation`'s result the one shape in the published
    contract with no `ok` — fixed here instead of carved into the schema.
    """
    matched = sum(1 for o, _ in outcomes if o == "match")
    mismatched = sum(1 for o, _ in outcomes if o == "mismatch")
    inconclusive = sum(1 for o, _ in outcomes if o == "inconclusive")

    if mismatched:
        reason = f"{mismatched} of {len(outcomes)} test input(s) showed different behaviour"
        passed = False
    elif matched == 0:
        # Includes the empty-input case: verifying against nothing is not a pass.
        reason = ("could not verify: no test input produced a successful run of BOTH programs, "
                  "so nothing was actually compared")
        passed = False
    else:
        reason = None
        passed = True

    return {"ok": True, "passed": passed, "reason": reason, "matched": matched,
            "mismatched": mismatched, "inconclusive": inconclusive,
            "total": len(outcomes)}


def vanished_output_side(a: dict, b: dict) -> str | None:
    """Which side reported SUCCESS with no output while the other produced some.

    `classify_case` already refuses to score two empty-but-ok results as a
    match, because an exit-0 run with no stdout cannot be distinguished from one
    whose output was lost (#42: `node` returns ok=True with empty stdout on
    windows-latest while sibling languages print normally from the same
    snippet). That reasoning was applied to BOTH sides being empty and not to
    ONE, so the one-sided case fell through to "both ran; stdout differs" and
    was reported as positive evidence of non-equivalence.

    It is not positive evidence. It is the same missing measurement, one level
    over — and it is what made the Windows translation gate flaky: a control
    whose four inputs all agree deterministically reported mismatched=1, then
    passed on re-run with no code change.

    Returning the side rather than a bool because the caller has to re-run
    exactly that one.
    """
    if not (bool(a.get("ok")) and bool(b.get("ok"))):
        return None
    a_out, b_out = _normalize(a.get("stdout", "")), _normalize(b.get("stdout", ""))
    if a_out and not b_out:
        return "target"
    if b_out and not a_out:
        return "source"
    return None


def verify_translation(source: str, source_code: str, target: str,
                       target_code: str, test_inputs: list[str],
                       timeout: int = 15) -> dict:
    """Run both versions on the same inputs and decide whether they agree.

    See classify_case/aggregate for the decision rules — they are separated out
    so they can be tested without a sandbox, two runtimes and a built binary.
    """
    cases = []
    outcomes: list[tuple[str, str]] = []
    for stdin in test_inputs:
        a = _run(source, source_code, stdin, timeout)
        b = _run(target, target_code, stdin, timeout)
        outcome, reason = classify_case(a, b)

        # One side succeeded and printed nothing while the other printed
        # something. Re-run THAT side once and let the two runs decide, rather
        # than reporting a missing measurement as a difference.
        #
        # The re-run is not a retry-until-green: a port that prints nothing is a
        # real divergence and must keep being reported as one. So the second run
        # is evidence about the FIRST, and only the two together produce a
        # verdict —
        #   still empty  -> reproducible, the mismatch stands (signal preserved)
        #   now non-empty -> the runtime is unstable on this input, which is not
        #                    evidence of equivalence either, so: inconclusive.
        #
        # Adopting the second result silently would be the wrong fix: a runtime
        # that produces output only sometimes has not shown the ports agree.
        side = vanished_output_side(a, b) if outcome == "mismatch" else None
        if side is not None:
            if side == "target":
                retry = _run(target, target_code, stdin, timeout)
            else:
                retry = _run(source, source_code, stdin, timeout)
            if _normalize(retry.get("stdout", "")):
                outcome = "inconclusive"
                reason = (f"the {side} produced no output on the first run and "
                          "output on a second run of the same input; the runtime "
                          "is not deterministic here, so neither result is "
                          "evidence about the translation")
            else:
                reason = f"{reason} (the empty side was re-run and was empty again)"
        outcomes.append((outcome, reason))
        cases.append({
            "input": stdin[:60],
            "outcome": outcome,
            "reason": reason,
            # Retained for callers that predate the three-way outcome. Only a
            # real "match" is true here; inconclusive is NOT a match.
            "match": outcome == "match",
            # "stdout" is the NORMALIZED string the match/mismatch decision was
            # made on; "stdout_raw" is exactly what the program wrote, before
            # NORMALIZE_TOLERANCE is applied. #286: the normalized string was
            # the ONLY evidence shown, so a match that folded a genuine
            # separator difference into agreement had nothing in the result
            # a reader could use to notice — both fields are now always
            # present, not only when they differ, so a reader never has to
            # guess whether normalization changed anything.
            "source": {"ok": bool(a.get("ok")), "phase": a.get("phase"),
                       "stdout": _normalize(a.get("stdout", ""))[:400],
                       "stdout_raw": (a.get("stdout") or "")[:400],
                       "stderr": (a.get("stderr") or "")[:200]},
            "target": {"ok": bool(b.get("ok")), "phase": b.get("phase"),
                       "stdout": _normalize(b.get("stdout", ""))[:400],
                       "stdout_raw": (b.get("stdout") or "")[:400],
                       "stderr": (b.get("stderr") or "")[:200]},
        })
    return {**aggregate(outcomes), "cases": cases}


def compare_edge_cases(snippets: dict[str, str],
                       inputs: list[str] | None = None,
                       timeout: int = 15,
                       order_sensitive: bool = True) -> dict:
    """Run the same logic in N languages (snippets: {language: code}) on
    edge-case inputs; report where behavior diverges.

    order_sensitive (default True) matches _normalize/verify_translation's own
    comparison: stdout is compared line-for-line, in order. Output ordering
    (map/dict iteration, sort stability, concurrency) is one of the most
    common genuine divergences between ports, so it is reported by default
    rather than masked. Each divergence carries a "kind" of "content" (the
    line sets themselves differ, or ok-status differs) or "order" (same
    lines, same ok-status, different order) so an order-only difference is
    never silently folded into agreement. Pass order_sensitive=False to
    restore the old order-insensitive comparison (only "content" divergences
    are then reported).

    Offline-capable: no LLM needed, snippets must be provided per language.

    Like `compare_execution`, the OUTER `ok` is always `True` once the sweep
    ran (its only `ok: false` is the empty-snippets refusal above) — it means
    "this tool produced a real matrix", not "every language succeeded". A
    per-language run that failed for a REQUEST-level reason (no runtime
    installed, a timeout) carries `error`/`code`/`remedy` on its OWN entry in
    `results[i]["runs"][lang]`, via `errors.stamp_row` — the same rule
    `compare_execution`'s rows follow, including leaving an ordinary program
    failure (a real RTE/OLE `exit_code`, no request-level cause) with no
    `code` at all.
    """
    if not snippets:
        return {"ok": False, "error": "provide at least one {language: code} snippet"}
    inputs = inputs if inputs else DEFAULT_EDGE_INPUTS
    results = []
    divergences = []
    for stdin in inputs:
        row = {"input": stdin[:60], "runs": {}}
        for lang, code in snippets.items():
            r = _run(lang, code, stdin, timeout)
            # "stdout" is normalized (NORMALIZE_TOLERANCE); "stdout_raw" is
            # exactly what the program wrote. Same reasoning as
            # verify_translation's per-case result (#286): the normalized
            # string alone erases whatever a separator-folding bug would have
            # hidden.
            run_entry = {
                "ok": r.get("ok"), "stdout": _normalize(r.get("stdout", ""))[:300],
                "stdout_raw": (r.get("stdout") or "")[:300],
                "verdict": r.get("verdict"), "stderr": (r.get("stderr") or "")[:150],
            }
            if r.get("error"):
                run_entry["error"] = r["error"]
            errors.stamp_row(
                run_entry, timed_out=bool(r.get("timed_out")),
                timeout_message=f"{lang} timed out after {timeout}s (wall-clock)",
            )
            row["runs"][lang] = run_entry
        # divergence = outputs differ, or error-status differs. Two keys are
        # built per run: the exact (order-sensitive) one and the sorted
        # (order-insensitive) one, so an order-only difference can be told
        # apart from a real content difference rather than collapsed into it.
        #
        # `.split("\n")`, NOT `.splitlines()`: `r["stdout"]` here is already
        # NORMALIZE_TOLERANCE-normalized, but normalizing only folds \r\n/\r
        # to \n — it does not remove or rewrite VT/FF/FS/GS/RS/NEL/U+2028/
        # U+2029, so a normalized string can still contain one verbatim.
        # `.splitlines()` would silently re-fold it into a line break at
        # EXACTLY this comparison, reintroducing #286 one call downstream of
        # the fix in `_normalize` itself.
        if len(row["runs"]) > 1:
            exact_behaviors = set()
            sorted_behaviors = set()
            for r in row["runs"].values():
                lines = tuple(r["stdout"].split("\n"))
                exact_behaviors.add((r["ok"], lines))
                sorted_behaviors.add((r["ok"], tuple(sorted(lines))))
            content_diverges = len(sorted_behaviors) > 1
            order_diverges = not content_diverges and len(exact_behaviors) > 1
            if content_diverges or (order_sensitive and order_diverges):
                kind = "content" if content_diverges else "order"
                divergences.append({"input": stdin[:60],
                                    "languages": list(row["runs"].keys()),
                                    "kind": kind,
                                    "runs": row["runs"]})
        results.append(row)

    return {
        "ok": True,
        "inputs": inputs,
        "languages": list(snippets.keys()),
        "divergence_count": len(divergences),
        "results": results,
        "divergences": divergences,
    }
