"""compare_execution's cold-start retry + timeout classification, and its
per-row `error`/`code`/`remedy` classification.

Standalone, like the other suites: a check() accumulator, sys.exit(1) on any
failure. The bug this guards: `node` intermittently loses the wall-clock race
on a globally slow GitHub Actions windows-latest runner (one repro had ruby
at 3452ms vs python at 33ms for a trivial snippet — a 100x spread) and gets
misread as a defective snippet rather than a cold-start casualty of a slow
box. compare_execution now retries a timed_out language exactly once, and if
it STILL times out, flags it with a sibling-timing comparison that
discriminates "this language is broken" from "the runner was slow".

executor.execute is monkeypatched with a fake so timeouts are deterministic
and the retry count is directly assertable — no real timing involved. The
section near the bottom of this file, guarded "ROW CLASSIFICATION", uses the
same fakes to prove `errors.stamp_row`'s three outcomes deterministically —
a real hidden-runtime/real-timeout version of the same three cases, on both
execution backends, lives in tests/test_platform_contract.py instead, where
the native executor is actually built.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import errors, tools

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def ok_result(stdout: str, duration_ms: float) -> dict:
    return {
        "ok": True,
        "stdout": stdout,
        "stderr": "",
        "exit_code": 0,
        "duration_ms": duration_ms,
        "timed_out": False,
    }


def timeout_result(duration_ms: float) -> dict:
    return {
        "ok": False,
        "stdout": "",
        "stderr": "",
        "exit_code": None,
        "duration_ms": duration_ms,
        "timed_out": True,
    }


def fail_result(stderr: str, duration_ms: float = 25) -> dict:
    """A deterministic, non-timeout failure (e.g. a compile error)."""
    return {
        "ok": False,
        "stdout": "",
        "stderr": stderr,
        "exit_code": 1,
        "duration_ms": duration_ms,
        "timed_out": False,
    }


def spawn_failure_result(language: str, duration_ms: float = 5) -> dict:
    """A missing-runtime spawn failure, shaped the way executor.execute
    reports one (see codecalc/executor.py's `_runtime_unavailable_result` and
    the Rust-result mapping it shares the wording with) — `error` carries the
    same text as `stderr`, and `exit_code` is `None`, never Rust's internal
    `-2` sentinel."""
    detail = f'runtime unavailable for the run phase: {language!r} not found (No such file or directory)'
    return {
        "ok": False,
        "stdout": "",
        "stderr": detail,
        "error": detail,
        "exit_code": None,
        "duration_ms": duration_ms,
        "timed_out": False,
    }


def make_fake(plan: dict[str, list[dict]]):
    """plan maps language -> list of results, one per call, consumed in order.
    Also returns the call-count dict so tests can assert retry counts."""
    calls: dict[str, int] = {}

    def fake(language, code, stdin="", timeout=15, **kw):
        calls[language] = calls.get(language, 0) + 1
        idx = calls[language] - 1
        seq = plan[language]
        return seq[min(idx, len(seq) - 1)]

    return fake, calls


def row(results: list[dict], language: str) -> dict:
    return next(r for r in results if r["language"] == language)


def disc(discrepancies: list[dict], language: str) -> dict | None:
    return next((d for d in discrepancies if d["language"] == language), None)


# ── 1. node times out once, recovers on the warm retry ─────────────────────
fake, calls = make_fake({
    "node": [timeout_result(15000), ok_result("42", 40)],
    "python3": [ok_result("42", 33)],
})
tools.executor.execute = fake
r = tools.compare_execution({"node": "console.log(42)", "python3": "print(42)"})
node_row = row(r["results"], "node")
check("recovered node row is ok", node_row["ok"] is True, f"-> {node_row}")
check("recovered node row is not timed_out", node_row["timed_out"] is False)
check("recovered node row carries cold_retry_recovered=True",
      node_row.get("cold_retry_recovered") is True, f"-> {node_row}")
check("recovered node row carries first_attempt_ms from the FIRST call",
      node_row.get("first_attempt_ms") == 15000, f"-> {node_row.get('first_attempt_ms')}")
check("recovered node's duration_ms is the RETRY's, not the first attempt's",
      node_row["duration_ms"] == 40, f"-> {node_row['duration_ms']}")
check("node is NOT flagged as a timeout discrepancy", disc(r["discrepancies"], "node") is None,
      f"-> {r['discrepancies']}")
check("fastest can be the recovered node", r["fastest"] in ("node", "python3"), f"-> {r['fastest']}")
check("node's fake was called exactly twice", calls.get("node") == 2, f"-> {calls}")

# ── 2. node times out on BOTH calls; siblings fast + tight → flagged specific to node
fake, calls = make_fake({
    "node": [timeout_result(15000), timeout_result(15000)],
    "python3": [ok_result("42", 30)],
    "ruby": [ok_result("42", 40)],
})
tools.executor.execute = fake
r = tools.compare_execution({"node": "console.log(42)", "python3": "print(42)", "ruby": "puts 42"})
node_row = row(r["results"], "node")
check("still-timed-out node row is timed_out", node_row["timed_out"] is True)
check("still-timed-out node row is NOT ok", node_row["ok"] is False)
check("still-timed-out node row carries cold_retry_recovered=False",
      node_row.get("cold_retry_recovered") is False)
d = disc(r["discrepancies"], "node")
check("node has a discrepancy entry", d is not None, f"-> {r['discrepancies']}")
check("...with sibling_durations_ms for both siblings",
      d is not None and d["sibling_durations_ms"] == {"python3": 30, "ruby": 40},
      f"-> {d.get('sibling_durations_ms') if d else None}")
check("...and a variance_note calling it specific to node (tight sibling band)",
      d is not None and "specific to node" in d["variance_note"],
      f"-> {d.get('variance_note') if d else None}")
check("node's fake was called exactly twice", calls.get("node") == 2, f"-> {calls}")

# ── 3. THE REPRO: node times out twice, python 33ms, ruby 3452ms (~104x) ───
fake, calls = make_fake({
    "node": [timeout_result(15000), timeout_result(15000)],
    "python3": [ok_result("42", 33)],
    "ruby": [ok_result("42", 3452)],
})
tools.executor.execute = fake
r = tools.compare_execution({"node": "console.log(42)", "python3": "print(42)", "ruby": "puts 42"})
d = disc(r["discrepancies"], "node")
check("repro: node flagged as a discrepancy", d is not None, f"-> {r['discrepancies']}")
check("repro: variance_note reports high sibling variance / runner-wide slowness",
      d is not None and "high sibling variance" in d["variance_note"]
      and "runner-wide slowness" in d["variance_note"],
      f"-> {d.get('variance_note') if d else None}")
check("repro: variance_note reports ~104x", d is not None and "104." in d["variance_note"],
      f"-> {d.get('variance_note') if d else None}")

# ── 3b. SINGLE slow ok sibling: the spread ratio is 1.0, but its ABSOLUTE
#        time is a big fraction of the same ceiling → runner-wide, NOT
#        "specific" (the tightened single-sibling case #42) ─────────────────
fake, calls = make_fake({
    "node": [timeout_result(15000), timeout_result(15000)],
    "python3": [ok_result("42", 8000)],  # lone sibling, itself slow
})
tools.executor.execute = fake
r = tools.compare_execution({"node": "console.log(42)", "python3": "print(42)"})
d = disc(r["discrepancies"], "node")
check("single slow sibling: node flagged", d is not None, f"-> {r['discrepancies']}")
check("single slow sibling: variance_note is runner-wide via the absolute-time signal, not 'looks specific'",
      d is not None and "runner-wide slowness" in d["variance_note"]
      and "% of the" in d["variance_note"]
      and "looks specific to" not in d["variance_note"],
      f"-> {d.get('variance_note') if d else None}")

# ── 3c. SINGLE fast ok sibling: ratio 1.0 AND fast → correctly "specific" ──
fake, calls = make_fake({
    "node": [timeout_result(15000), timeout_result(15000)],
    "python3": [ok_result("42", 30)],
})
tools.executor.execute = fake
r = tools.compare_execution({"node": "console.log(42)", "python3": "print(42)"})
d = disc(r["discrepancies"], "node")
check("single fast sibling: variance_note reads specific to node",
      d is not None and "looks specific to node" in d["variance_note"],
      f"-> {d.get('variance_note') if d else None}")

# ── 4. a non-timeout ok=False (e.g. compile error) is NOT retried ──────────
fake, calls = make_fake({
    "perl": [fail_result("syntax error at -e line 1.")],
    "python3": [ok_result("42", 33)],
})
tools.executor.execute = fake
r = tools.compare_execution({"perl": "bad(", "python3": "print(42)"})
perl_row = row(r["results"], "perl")
check("perl's fake was called exactly once (no retry on a deterministic failure)",
      calls.get("perl") == 1, f"-> {calls}")
check("perl row carries cold_retry=False (or absent)", not perl_row.get("cold_retry", False))
check("perl is not added as a timeout discrepancy",
      disc(r["discrepancies"], "perl") is None or disc(r["discrepancies"], "perl")["timed_out"] is not True,
      f"-> {r['discrepancies']}")

# ── 5. empty-but-ok beside a printing sibling: unchanged, not retried ──────
fake, calls = make_fake({
    "python3": [ok_result("", 20)],
    "node": [ok_result("42", 30)],
})
tools.executor.execute = fake
r = tools.compare_execution({"python3": "pass", "node": "console.log(42)"})
check("python3's fake was called exactly once (ok, not timed_out -> no retry)",
      calls.get("python3") == 1, f"-> {calls}")
d = disc(r["discrepancies"], "python3")
check("empty-but-ok python3 is still flagged by the existing produced/silent logic",
      d is not None and "no stdout while another language produced some" in d["issue"],
      f"-> {d}")
check("...and it is NOT the timeout-classification shape",
      d is not None and "timed_out" in d and d["timed_out"] is False,
      f"-> {d}")

# ── 6. discrepancies is always a list; a language appears at most once ─────
fake, calls = make_fake({
    "python3": [ok_result("1", 5)],
})
tools.executor.execute = fake
happy = tools.compare_execution({"python3": "print(1)"})
check("discrepancies is a list on the happy path", isinstance(happy["discrepancies"], list),
      f"-> {type(happy['discrepancies']).__name__}")
check("discrepancies is empty on the happy path", happy["discrepancies"] == [], f"-> {happy['discrepancies']}")

# language-appears-once check, reusing case 2's still-timed-out-and-silent node
fake, calls = make_fake({
    "node": [timeout_result(15000), timeout_result(15000)],
    "python3": [ok_result("42", 30)],
})
tools.executor.execute = fake
r = tools.compare_execution({"node": "console.log(42)", "python3": "print(42)"})
node_entries = [d for d in r["discrepancies"] if d["language"] == "node"]
check("a language appears at most once in discrepancies",
      len(node_entries) == 1, f"-> {node_entries}")


# ── ROW CLASSIFICATION: errors.stamp_row's three outcomes ──────────────────
# 7. a missing-runtime row carries error/code/remedy; the working sibling
#    carries none of the three; the OUTER ok stays True.
fake, calls = make_fake({
    "lua": [spawn_failure_result("lua")],
    "python3": [ok_result("42", 33)],
})
tools.executor.execute = fake
r = tools.compare_execution({"lua": "print(42)", "python3": "print(42)"})
check("outer ok is True even though one language could not run",
      r["ok"] is True, f"-> {r['ok']}")
lua_row = row(r["results"], "lua")
check("a missing-runtime row is classified runtime_unavailable",
      lua_row.get("code") == errors.RUNTIME_UNAVAILABLE, f"-> {lua_row.get('code')}")
check("...and carries error text", bool(lua_row.get("error")), f"-> {lua_row.get('error')!r}")
check("...and a remedy", lua_row.get("remedy") == errors.REMEDIES[errors.RUNTIME_UNAVAILABLE],
      f"-> {lua_row.get('remedy')!r}")
check("...and is marked code_inferred (message-matched, not raised)",
      lua_row.get("code_inferred") is True, f"-> {lua_row.get('code_inferred')}")
python_row = row(r["results"], "python3")
check("a working row carries no code at all", "code" not in python_row, f"-> {python_row}")
check("...and no error/remedy either", "error" not in python_row and "remedy" not in python_row,
      f"-> {python_row}")

# 8. a row still timed_out after the warm retry is classified `timeout` —
# from a message THIS function builds, never from the row's own `stderr`
# (which the killed process is free to have left non-empty with its OWN
# output — see errors.stamp_row's docstring for why that is not safe to
# feed to a message-matching classifier).
fake, calls = make_fake({
    "node": [timeout_result(15000), timeout_result(15000)],
    "python3": [ok_result("42", 30)],
})
tools.executor.execute = fake
r = tools.compare_execution({"node": "console.log(42)", "python3": "print(42)"}, timeout=15)
node_row = row(r["results"], "node")
check("a still-timed-out row is classified timeout",
      node_row.get("code") == errors.TIMEOUT, f"-> {node_row.get('code')}")
check("...and its error text is built by compare_execution, not copied from stderr",
      node_row.get("error") == "node timed out after 15s (wall-clock)",
      f"-> {node_row.get('error')!r}")

# 9. a language that timed out once but RECOVERED on the warm retry is a
# success row (case 1's own row) and must carry no code.
fake, calls = make_fake({
    "node": [timeout_result(15000), ok_result("42", 40)],
    "python3": [ok_result("42", 33)],
})
tools.executor.execute = fake
r = tools.compare_execution({"node": "console.log(42)", "python3": "print(42)"})
node_row = row(r["results"], "node")
check("a recovered row carries no code", "code" not in node_row, f"-> {node_row}")

# 10. an ordinary deterministic failure (e.g. a compile error, a real exit
# code) is NOT a request-level failure and carries no code — `verdict`/
# `exit_code` already tell that story, per the INTENDED convention
# errors.stamp_row implements (a failed PROGRAM, not a failed REQUEST) — the
# same convention errors.ensure_code applies to the top-level execute_code
# envelope itself (see both functions' own docstrings).
fake, calls = make_fake({
    "perl": [fail_result("syntax error at -e line 1.")],
    "python3": [ok_result("42", 33)],
})
tools.executor.execute = fake
r = tools.compare_execution({"perl": "bad(", "python3": "print(42)"})
perl_row = row(r["results"], "perl")
check("an ordinary program failure carries no code",
      "code" not in perl_row, f"-> {perl_row}")
check("...and no error/remedy of its own (compare_execution's error/code/"
      "remedy are reserved for request-level failures)",
      "error" not in perl_row and "remedy" not in perl_row, f"-> {perl_row}")


print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== COLD-START RETRY / CLASSIFICATION OK ===")
sys.exit(1 if FAILS else 0)
