"""Cross-language translation with verification, and edge-case comparison.

translate_code: an LLM ports code between languages; the Rust executor then
VERIFIES the translation by running both versions against identical test
inputs and comparing stdout. Accepted only when outputs match (one retry
feeding the diff back). This is the "Rosetta Stone" use case with a ground
truth check — no tool in the space does translate-then-verify.

compare_edge_cases: runs a snippet across N languages on edge-case inputs
(empty, zero, negative, float precision, large N) and flags behavioral
divergence — where languages disagree on the same input.

Both are best-effort when the LLM gateway is down: translate_code returns the
LLM error clearly; compare_edge_cases works fully offline when given
per-language snippets (no translation needed).
"""

from __future__ import annotations

import json
import re

from . import context7, executor, llm

#: default edge-case inputs (each is stdin for the program)
DEFAULT_EDGE_INPUTS = ["", "0", "1", "-1", "10", "100", "0.1\n0.2"]

_TRANSLATE_SYSTEM = (
    "You port code between programming languages. Preserve behavior EXACTLY: "
    "same stdin handling, same output format, same edge-case behavior. "
    "Output ONLY valid JSON: {\"code\": \"<the translated program>\"}. "
    "Never wrap in markdown, never explain."
)


def _extract_code(text: str) -> str | None:
    """Pull code from LLM output: fenced block, JSON code field, or raw."""
    m = re.search(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).rstrip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "code" in obj:
            return obj["code"]
    except Exception:
        pass
    m = re.search(r'"code"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        try:
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            pass
    return None


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


def _worst_case(cases: list[dict]) -> dict | None:
    """The most informative failing case: a real mismatch if there is one, else
    an inconclusive one. Used to build retry feedback for the model."""
    return (next((c for c in cases if c["outcome"] == "mismatch"), None)
            or next((c for c in cases if c["outcome"] == "inconclusive"), None))


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


def translate_code(code: str, source: str, target: str,
                   test_inputs: list[str] | None = None,
                   model: str | None = None, timeout: int = 30) -> dict:
    """Port `code` from `source` to `target`, then verify equivalence on
    test inputs. Retries once with the mismatch diff if verification fails."""
    source = executor.registry.canonical(source) or source
    target = executor.registry.canonical(target) or target
    if source == target:
        return {"ok": False, "error": "source and target are the same language"}
    inputs = test_inputs if test_inputs else DEFAULT_EDGE_INPUTS[:4]

    # context7: give the LLM current API knowledge for the target language
    # and any libraries the code imports
    docs_parts = []
    lang_docs = context7.docs_for_language(target)
    if lang_docs.get("ok"):
        docs_parts.append(f"## {target} standard library reference\n{lang_docs['content'][:2500]}")
    lib_docs = context7.docs_for_code(code, source)
    if lib_docs.get("ok"):
        docs_parts.append(f"## library reference\n{lib_docs['content'][:2500]}")
    docs_block = "\n\n".join(docs_parts)

    test_inputs_repr = json.dumps(inputs)
    prompt = (
        f"Port this {source} program to {target}. Keep behavior EXACTLY identical:\n"
        f"- same stdin reading (first line is N unless noted)\n"
        f"- same stdout format\n- same edge-case behavior (division by zero, negatives)\n"
        f"- use idiomatic {target}, not a line-by-line transliteration\n\n"
        f"{docs_block}\n\n"
        f"Verification inputs (will be fed to both versions as stdin): "
        f"{test_inputs_repr}\n\n"
        f"Source code ({source}):\n```\n{code[:8000]}\n```"
    )

    last_error = None
    for attempt in (1, 2):
        try:
            text = llm.chat(prompt, system=_TRANSLATE_SYSTEM, model=model,
                            timeout=timeout)
        except Exception as exc:
            return {"ok": False, "error": f"LLM unavailable: {exc}",
                    "llm_available": False}
        translated = _extract_code(text)
        if not translated:
            return {"ok": False, "error": "LLM did not return code",
                    "llm_available": True, "raw": text[:500]}

        ver = verify_translation(source, code, target, translated, inputs)
        if ver["passed"]:
            return {
                "ok": True, "source_language": source, "target_language": target,
                "translated_code": translated,
                "verification": ver,
                "attempts": attempt,
                "llm_available": True,
            }
        last_error = ver
        # Retry feedback. Prefer a real mismatch — that is the only case with a
        # concrete difference to show. Fall back to an inconclusive one, which
        # needs a DIFFERENT message: telling the model to "make the outputs
        # match" when neither program produced output sends it chasing a
        # difference that was never observed.
        worst = _worst_case(ver["cases"])
        if worst and attempt == 1:
            if worst["outcome"] == "mismatch":
                prompt += (
                    "\n\nVERIFICATION FAILED on input "
                    f"{json.dumps(worst['input'])} ({worst['reason']}):\n"
                    f"source stdout: {json.dumps(worst['source']['stdout'][:300])}\n"
                    f"target stdout: {json.dumps(worst['target']['stdout'][:300])}\n"
                    f"target stderr: {json.dumps(worst['target']['stderr'][:300])}\n"
                    "Fix the translation so outputs match exactly."
                )
            else:
                prompt += (
                    "\n\nCOULD NOT VERIFY on input "
                    f"{json.dumps(worst['input'])} ({worst['reason']}).\n"
                    f"source stderr: {json.dumps(worst['source']['stderr'][:300])}\n"
                    f"target stderr: {json.dumps(worst['target']['stderr'][:300])}\n"
                    "Both programs failed, so nothing could be compared. Emit a "
                    "translation that RUNS on these inputs."
                )

    return {
        "ok": False, "source_language": source, "target_language": target,
        "translated_code": translated if "translated" in locals() else None,
        "verification": last_error,
        # The reason is carried through rather than asserting "outputs differ",
        # which was wrong whenever nothing had been compared in the first place.
        "error": f"verification failed after retry: {last_error['reason']}"
                 if last_error else "verification failed after retry",
        "attempts": 2, "llm_available": True,
    }


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
