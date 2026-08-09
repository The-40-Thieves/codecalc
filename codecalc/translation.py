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
"""

from __future__ import annotations

from . import executor

#: default edge-case inputs (each is stdin for the program)
DEFAULT_EDGE_INPUTS = ["", "0", "1", "-1", "10", "100", "0.1\n0.2"]



def _run(language: str, code: str, stdin: str, timeout: int = 15) -> dict:
    return executor.execute(language, code, stdin=stdin, timeout=timeout)


def _normalize(s: str) -> str:
    return "\n".join(line.rstrip() for line in s.splitlines()).strip()


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
        if _normalize(a.get("stdout", "")) == _normalize(b.get("stdout", "")):
            return "match", ""
        return "mismatch", "both ran; stdout differs"

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

    return {"passed": passed, "reason": reason, "matched": matched,
            "mismatched": mismatched, "inconclusive": inconclusive,
            "total": len(outcomes)}


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
        outcomes.append((outcome, reason))
        cases.append({
            "input": stdin[:60],
            "outcome": outcome,
            "reason": reason,
            # Retained for callers that predate the three-way outcome. Only a
            # real "match" is true here; inconclusive is NOT a match.
            "match": outcome == "match",
            "source": {"ok": bool(a.get("ok")), "phase": a.get("phase"),
                       "stdout": _normalize(a.get("stdout", ""))[:400],
                       "stderr": (a.get("stderr") or "")[:200]},
            "target": {"ok": bool(b.get("ok")), "phase": b.get("phase"),
                       "stdout": _normalize(b.get("stdout", ""))[:400],
                       "stderr": (b.get("stderr") or "")[:200]},
        })
    return {**aggregate(outcomes), "cases": cases}


def compare_edge_cases(snippets: dict[str, str],
                       inputs: list[str] | None = None,
                       timeout: int = 15) -> dict:
    """Run the same logic in N languages (snippets: {language: code}) on
    edge-case inputs; report where behavior diverges.

    Offline-capable: no LLM needed, snippets must be provided per language.
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
            row["runs"][lang] = {
                "ok": r.get("ok"), "stdout": _normalize(r.get("stdout", ""))[:300],
                "verdict": r.get("verdict"), "stderr": (r.get("stderr") or "")[:150],
            }
        # divergence = outputs differ, or error-status differs
        if len(row["runs"]) > 1:
            behaviors = set()
            for r in row["runs"].values():
                behaviors.add((r["ok"], tuple(sorted(r["stdout"].splitlines()))))
            if len(behaviors) > 1:
                divergences.append({"input": stdin[:60],
                                    "languages": list(row["runs"].keys()),
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
