"""Regression lock for scripts/tool_select_llm_eval.py — the MODEL-driven
half of the tool-selection eval (scripts/tool_select_eval.py is the lexical
BM25 half).

Fully OFFLINE, per CONTRIBUTING.md's "most gates run on a bare checkout with
no network, no token" — this suite never calls the real gateway. It starts a
local `http.server.ThreadingHTTPServer` on `127.0.0.1` that speaks just
enough of the OpenAI chat-completions wire format to exercise the real
script end to end: real subprocess invocation, real argument parsing, real
cache files on disk, real JSON report. Every property below was watched
FAILING before being kept, per CONTRIBUTING.md's "rule that matters most":

  - parsing: the fake server always answers a TOOLS-mode call with the
    FIRST tool `tool_select_llm_eval.py` itself put first in `tools=[...]`
    (alphabetical, per `build_tools_payload`'s own sort) — a labeled prompt
    set with one entry expecting that tool and one expecting a different
    one proves both a hit AND a miss (confusion-list) path are exercised,
    not just a run that never disagrees with itself.
  - cache resume: the fake server counts every request it receives; a
    second run pointed at the same `--cache-dir` is asserted to raise that
    count by exactly zero.
  - top-3 fallback: a second fake model always 400s a TOOLS-mode request
    (a real gateway behavior for a model with no function-calling support)
    but answers LIST-mode normally — `top1_source` for that model must show
    every prompt resolved via `"list_fallback"`, never `"tool"`.
  - advisory vs strict: a doctored baseline claiming an impossible top-1
    hit count is asserted to print a `::warning::` and exit 0 by default,
    then exit 1 once `--strict` is added — same doctored file, two runs.
  - the secret never appears: the fake server's expected bearer token is a
    distinctive, never-otherwise-occurring string; asserted absent from the
    script's stdout, stderr, and the written JSON report after every run
    above.

Standalone script, not pytest — see tests/conftest.py and
tests/test_tool_select_eval.py for why (module-scope assertions, `sys.exit`
at the end).
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import tool_select_eval as tse
import tool_select_llm_eval as tsle

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


#: Distinctive on purpose — a false negative ("the report just happens not to
#: contain a short common substring") is not a risk with a string this
#: specific and this unlikely to appear by coincidence anywhere else.
FAKE_API_KEY = "unit-test-fake-bearer-9f3c7a1e-do-not-leak"

#: A model name the fake server always fails TOOLS-mode for, simulating a
#: real gateway 400 for a model with no function-calling support.
NO_TOOLS_MODEL = "fake/no-tools-model"
TOOLS_MODEL = "fake/test-model"


# ── the fake OpenAI-compatible server ───────────────────────────────────────

class _FakeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.request_count = 0


class _FakeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # silence — CI output stays readable
        pass

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        state: _FakeState = self.server.fake_state  # type: ignore[attr-defined]
        with state.lock:
            state.request_count += 1

        if self.path != "/v1/chat/completions":
            self._send(404, {"error": {"message": f"no such path {self.path!r}"}})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {FAKE_API_KEY}":
            self._send(401, {"error": {"message": "bad bearer token"}})
            return

        body = json.loads(raw.decode("utf-8"))
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

        if "tools" in body:
            if body.get("model") == NO_TOOLS_MODEL:
                self._send(400, {"error": {"message": "this model does not support tools"}})
                return
            first_tool = body["tools"][0]["function"]["name"]
            self._send(200, {
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": "call_0", "type": "function",
                            "function": {"name": first_tool, "arguments": "{}"},
                        }],
                    },
                }],
                "usage": usage,
            })
            return

        # LIST mode: pull `name` back out of the inlined catalog (one JSON
        # object per line, exactly tool_select_llm_eval.build_catalog_text's
        # own shape) rather than hardcoding tool names, so this fake server
        # stays correct regardless of which --groups schemas were loaded.
        system_content = body["messages"][0]["content"]
        catalog_block = system_content.split("TOOL CATALOG:\n", 1)[1]
        names = [json.loads(line)["name"] for line in catalog_block.splitlines() if line.strip()]
        top3 = names[:3]
        self._send(200, {
            "choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": json.dumps(top3)}}],
            "usage": usage,
        })


class _FakeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


@contextlib.contextmanager
def fake_server():
    server = _FakeServer(("127.0.0.1", 0), _FakeHandler)
    server.fake_state = _FakeState()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def base_url_for(server) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}/v1"


def run_cli(server, tmp_data, tmp_cache_dir, tmp_json_out, *extra_args):
    cmd = [
        sys.executable, str(REPO_ROOT / "scripts" / "tool_select_llm_eval.py"),
        "--data", str(tmp_data),
        "--groups", "core",
        "--cache-dir", str(tmp_cache_dir),
        "--json", str(tmp_json_out),
        "--no-baseline",
        *extra_args,
    ]
    env = {
        "PATH": os.environ.get("PATH", ""),
        tsle.ENV_BASE_URL: base_url_for(server),
        tsle.ENV_API_KEY: FAKE_API_KEY,
    }
    return subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)


# ── fixture: a tiny labeled prompt set against the real 'core' schemas ─────

core_schemas = tsle.load_tool_schemas("core")
_sorted_core = sorted(core_schemas)
FIRST_TOOL = _sorted_core[0]          # what the fake server always "calls"
SECOND_TOOL = _sorted_core[1]         # deliberately mislabeled prompt below

FIXTURE_PROMPTS = [
    {"prompt": "Please work out a result for scenario Alpha, first case.", "expected": [FIRST_TOOL]},
    {"prompt": "Please work out a result for scenario Beta, second case.", "expected": [SECOND_TOOL]},
]
check("fixture prompts pass tool_select_eval's own name-leak validator "
      "(no expected tool's name/tokens appear in its own prompt text)",
      tse.validate_prompts(FIXTURE_PROMPTS) == [])


def write_fixture(path):
    path.write_text("\n".join(json.dumps(e) for e in FIXTURE_PROMPTS) + "\n", encoding="utf-8")


with tempfile.TemporaryDirectory(prefix="tool_select_llm_eval_test_") as _tmp:
    TMP = pathlib.Path(_tmp)
    data_path = TMP / "prompts.jsonl"
    write_fixture(data_path)
    cache_dir = TMP / "cache"
    json_out = TMP / "report.json"

    with fake_server() as server:
        # ── A: parsing — a real hit and a real miss, via the real CLI ──────
        proc1 = run_cli(server, data_path, cache_dir, json_out, "--models", TOOLS_MODEL)
        check("run A: the real CLI exits 0 against the fake server",
              proc1.returncode == 0, f"-> rc={proc1.returncode} stderr={proc1.stderr[-500:]!r}")

        report1 = json.loads(json_out.read_text(encoding="utf-8")) if json_out.is_file() else {}
        result1 = report1.get("results", {}).get(TOOLS_MODEL, {}).get("core", {})
        check("run A: n == 2 (both fixture prompts are applicable to 'core')",
              result1.get("n") == 2, f"-> {result1.get('n')}")
        check("run A: top1_hits == 1 (the fake server always calls the alphabetically-"
              "first tool, which matches only the FIRST fixture prompt's expected)",
              result1.get("top1_hits") == 1, f"-> {result1}")
        check("run A: the miss is recorded in the confusion list with got=FIRST_TOOL",
              any(c["got"] == FIRST_TOOL and c["expected"] == [SECOND_TOOL]
                  for c in result1.get("confusion", [])),
              f"-> {result1.get('confusion')}")
        check("run A: top1_source attributes both hits to real tool-calls, none to fallback",
              result1.get("top1_source") == {"tool": 2},
              f"-> {result1.get('top1_source')}")
        check("run A: the BM25 comparison number is present next to the model's own "
              "(tool_select_eval.evaluate() over the identical schemas/prompts)",
              result1.get("bm25", {}).get("n") == 2, f"-> {result1.get('bm25')}")
        check("run A: usage is summed from BOTH calls per prompt (2 prompts x 2 calls "
              "x 15 total_tokens/call = 60)",
              result1.get("usage", {}).get("total_tokens") == 60,
              f"-> {result1.get('usage')}")

        requests_after_a = server.fake_state.request_count
        check("run A: exactly 4 requests were made (2 prompts x 2 modes, nothing cached yet)",
              requests_after_a == 4, f"-> {requests_after_a}")

        # ── B: cache resume — an IDENTICAL second run makes zero new requests ──
        proc2 = run_cli(server, data_path, cache_dir, json_out, "--models", TOOLS_MODEL)
        check("run B: the real CLI exits 0 on the cached rerun",
              proc2.returncode == 0, f"-> rc={proc2.returncode}")
        requests_after_b = server.fake_state.request_count
        check("run B: the cached rerun made ZERO additional requests to the fake server",
              requests_after_b == requests_after_a,
              f"-> before={requests_after_a} after={requests_after_b}")
        report2 = json.loads(json_out.read_text(encoding="utf-8"))
        check("run B: the cached rerun reproduces the identical aggregate result",
              report2["results"][TOOLS_MODEL]["core"]["top1_hits"] == result1["top1_hits"])

        # ── C: top-3 fallback — a model with no tool-calling support ────────
        proc3 = run_cli(server, data_path, cache_dir, json_out, "--models", NO_TOOLS_MODEL)
        check("run C: the real CLI exits 0 even though every TOOLS-mode call 400s",
              proc3.returncode == 0, f"-> rc={proc3.returncode} stderr={proc3.stderr[-500:]!r}")
        report3 = json.loads(json_out.read_text(encoding="utf-8"))
        result3 = report3["results"][NO_TOOLS_MODEL]["core"]
        check("run C: every prompt's effective top-1 came from the list-mode FALLBACK, "
              "never from a real tool call",
              result3["top1_source"] == {"list_fallback": 2}, f"-> {result3['top1_source']}")
        check("run C: top-3 still hits — list-mode's own catalog-order answer contains "
              "both fixture tools",
              result3["top3_hits"] == 2, f"-> {result3}")

        # ── D: advisory vs strict exit codes ─────────────────────────────────
        doctored_baseline = TMP / "doctored_baseline.json"
        doctored_baseline.write_text(json.dumps({
            "prompt_set_sha256": tse.prompts_content_hash(FIXTURE_PROMPTS),
            "models": {TOOLS_MODEL: {"core": {"n": 2, "top1_hits": 2}}},  # impossible: run A got 1
        }), encoding="utf-8")

        # run_cli() always appends --no-baseline (every other run above wants
        # that), so this run builds its own argv without it — it is the ONE
        # scenario that needs the compare to actually run.
        # --epsilon 0: the fixture is only 2 prompts, so the max possible drop
        # is 2 — the script's own default epsilon (2) would tolerate that and
        # never warn, which would make this scenario indistinguishable from
        # "the compare never ran at all". 0 makes ANY drop a regression.
        cmd4 = [
            sys.executable, str(REPO_ROOT / "scripts" / "tool_select_llm_eval.py"),
            "--data", str(data_path), "--groups", "core", "--cache-dir", str(cache_dir),
            "--json", str(json_out), "--models", TOOLS_MODEL,
            "--baseline", str(doctored_baseline), "--epsilon", "0",
        ]
        env4 = {"PATH": os.environ.get("PATH", ""), tsle.ENV_BASE_URL: base_url_for(server),
                tsle.ENV_API_KEY: FAKE_API_KEY}
        proc4 = subprocess.run(cmd4, cwd=REPO_ROOT, env=env4, capture_output=True, text=True, timeout=120)
        check("run D (advisory, default): a doctored baseline claiming an impossible "
              "top1_hits count prints a ::warning:: but still exits 0",
              proc4.returncode == 0 and "::warning::" in proc4.stdout and "dropped by" in proc4.stdout,
              f"-> rc={proc4.returncode} stdout={proc4.stdout[-500:]!r}")

        cmd5 = [*cmd4, "--strict"]
        proc5 = subprocess.run(cmd5, cwd=REPO_ROOT, env=env4, capture_output=True, text=True, timeout=120)
        check("run D (--strict): the IDENTICAL doctored-baseline regression now exits 1",
              proc5.returncode == 1 and "::warning::" in proc5.stdout,
              f"-> rc={proc5.returncode} stdout={proc5.stdout[-500:]!r}")

        # ── E: the secret never appears, across every run above ─────────────
        all_stdout_stderr = "".join([
            proc1.stdout, proc1.stderr, proc2.stdout, proc2.stderr,
            proc3.stdout, proc3.stderr, proc4.stdout, proc4.stderr,
            proc5.stdout, proc5.stderr,
        ])
        check("the fake bearer token never appears in any run's stdout/stderr",
              FAKE_API_KEY not in all_stdout_stderr)
        all_reports_text = "".join(
            p.read_text(encoding="utf-8") for p in [json_out] if p.is_file()
        )
        check("the fake bearer token never appears in the written JSON report",
              FAKE_API_KEY not in all_reports_text)

# ── unit-level checks on the pure functions (no server, no subprocess) ──────

check("_scrub() redacts an exact secret match",
      tsle._scrub(f"HTTP 401: token {FAKE_API_KEY} rejected", FAKE_API_KEY)
      == "HTTP 401: token ***REDACTED*** rejected")
check("_scrub() is a no-op when there is nothing to redact",
      tsle._scrub("plain error, no secret here", FAKE_API_KEY) == "plain error, no secret here")
check("_scrub() is a no-op for an empty/None secret (never raises)",
      tsle._scrub("text", None) == "text" and tsle._scrub("text", "") == "text")

check("cache_key() is a function of ALL THREE of (model, group, prompt) — changing "
      "any one changes the key",
      len({
          tsle.cache_key("m1", "full", "p"), tsle.cache_key("m2", "full", "p"),
          tsle.cache_key("m1", "dev", "p"), tsle.cache_key("m1", "full", "q"),
      }) == 4)

check("_parse_ranked_names() extracts a JSON array embedded in prose/fences",
      tsle._parse_ranked_names(
          "Sure, here you go:\n```json\n[\"evaluate_expression\", \"calc_exact\", \"bogus_tool\"]\n```",
          {"evaluate_expression": {}, "calc_exact": {}},
      ) == ["evaluate_expression", "calc_exact"])
check("_parse_ranked_names() drops a hallucinated name absent from the schema set",
      "bogus_tool" not in tsle._parse_ranked_names(
          '["bogus_tool", "calc_exact"]', {"calc_exact": {}}))
check("_parse_ranked_names() returns [] for unparsable garbage rather than raising",
      tsle._parse_ranked_names("not json at all", {"calc_exact": {}}) == [])

# ── compare_baseline(): corpus mismatch, missing entries, real regression ──

_fake_report = {
    "prompt_set_sha256": "aaaa",
    "results": {"m": {"core": {"n": 10, "top1_hits": 5}}},
}
with tempfile.TemporaryDirectory(prefix="llm_eval_baseline_test_") as _btmp:
    bpath = pathlib.Path(_btmp) / "baseline.json"

    bpath.write_text(json.dumps({"prompt_set_sha256": "bbbb", "models": {}}), encoding="utf-8")
    warnings = tsle.compare_baseline(_fake_report, bpath, epsilon=2)
    check("compare_baseline() flags a corpus-hash mismatch distinctly",
          len(warnings) == 1 and "CORPUS CHANGED" in warnings[0], f"-> {warnings}")

    bpath.write_text(json.dumps({"prompt_set_sha256": "aaaa", "models": {}}), encoding="utf-8")
    warnings = tsle.compare_baseline(_fake_report, bpath, epsilon=2)
    check("compare_baseline() warns (not raises) when the baseline has no entry for a model",
          len(warnings) == 1 and "no baseline entry for" in warnings[0], f"-> {warnings}")

    bpath.write_text(json.dumps({
        "prompt_set_sha256": "aaaa", "models": {"m": {"core": {"n": 10, "top1_hits": 9}}},
    }), encoding="utf-8")
    warnings = tsle.compare_baseline(_fake_report, bpath, epsilon=2)
    check("compare_baseline() flags a real regression exceeding epsilon "
          "(baseline 9 -> current 5, drop=4 > epsilon=2)",
          len(warnings) == 1 and "dropped by 4" in warnings[0], f"-> {warnings}")

    warnings = tsle.compare_baseline(_fake_report, bpath, epsilon=10)
    check("compare_baseline() does NOT flag the same drop once epsilon covers it",
          warnings == [], f"-> {warnings}")

    bpath.write_text(json.dumps({
        "prompt_set_sha256": "aaaa", "models": {"m": {"core": {"n": 999, "top1_hits": 999}}},
    }), encoding="utf-8")
    warnings = tsle.compare_baseline(_fake_report, bpath, epsilon=2)
    check("compare_baseline() refuses to compare when the applicable-prompt count itself "
          "changed (n mismatch), rather than silently scoring against a different corpus slice",
          len(warnings) == 1 and "n=999" in warnings[0] and "n=10" in warnings[0], f"-> {warnings}")

print(f"\n=== {len(FAILS)} failures ===" if FAILS else "\n=== ALL TOOL-SELECT-LLM-EVAL TESTS PASS ===")
sys.exit(1 if FAILS else 0)
