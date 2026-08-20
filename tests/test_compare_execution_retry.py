"""THE-802: compare_execution's cold-start retry + timeout classification.

Standalone, like the other suites: a check() accumulator, sys.exit(1) on any
failure. The bug this guards: `node` intermittently loses the wall-clock race
on a globally slow GitHub Actions windows-latest runner (one repro had ruby
at 3452ms vs python at 33ms for a trivial snippet — a 100x spread) and gets
misread as a defective snippet rather than a cold-start casualty of a slow
box. compare_execution now retries a timed_out language exactly once, and if
it STILL times out, flags it with a sibling-timing comparison that
discriminates "this language is broken" from "the runner was slow".

executor.execute is monkeypatched with a fake so timeouts are deterministic
and the retry count is directly assertable — no real timing involved.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import tools

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


print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== COLD-START RETRY / CLASSIFICATION OK ===")
sys.exit(1 if FAILS else 0)
