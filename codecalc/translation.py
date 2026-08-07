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


def verify_translation(source: str, source_code: str, target: str,
                       target_code: str, test_inputs: list[str],
                       timeout: int = 15) -> dict:
    """Run both versions on the same inputs; report per-input match/mismatch."""
    cases = []
    for stdin in test_inputs:
        a = _run(source, source_code, stdin, timeout)
        b = _run(target, target_code, stdin, timeout)
        a_ok, b_ok = a.get("ok"), b.get("ok")
        a_out, b_out = _normalize(a.get("stdout", "")), _normalize(b.get("stdout", ""))
        # treat both-error as consistent (same behavior class), else require
        # same ok status AND identical stdout
        if not a_ok and not b_ok:
            match = True
        else:
            match = a_ok == b_ok and a_out == b_out
        cases.append({
            "input": stdin[:60],
            "match": match,
            "source": {"ok": a_ok, "stdout": a_out[:400],
                       "stderr": (a.get("stderr") or "")[:200]},
            "target": {"ok": b_ok, "stdout": b_out[:400],
                       "stderr": (b.get("stderr") or "")[:200]},
        })
    matched = sum(1 for c in cases if c["match"])
    return {"matched": matched, "total": len(cases),
            "passed": matched == len(cases), "cases": cases}


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
        # retry with the diff as feedback
        mismatch = next((c for c in ver["cases"] if not c["match"]), None)
        if mismatch and attempt == 1:
            prompt += (
                "\n\nVERIFICATION FAILED on input "
                f"{json.dumps(mismatch['input'])}:\n"
                f"source stdout: {json.dumps(mismatch['source']['stdout'][:300])}\n"
                f"target stdout: {json.dumps(mismatch['target']['stdout'][:300])}\n"
                f"target stderr: {json.dumps(mismatch['target']['stderr'][:300])}\n"
                "Fix the translation so outputs match exactly."
            )

    return {
        "ok": False, "source_language": source, "target_language": target,
        "translated_code": translated if "translated" in locals() else None,
        "verification": last_error,
        "error": "verification failed after retry: outputs differ",
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
            for lang, r in row["runs"].items():
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
