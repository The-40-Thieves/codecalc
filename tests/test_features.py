"""Verify the new feature set over MCP: sessions, files, artifacts, packages,
verdicts, limits, streaming, compact mode.

`main()` below drives the actual MCP round trip (session lifecycle, file I/O,
artifacts, verdicts, compact mode, streaming, package install) — it used to
be dead code: `sys.exit(1 if FAILS else 0)` ran BEFORE `asyncio.run(main())`
at the bottom of the file, so `main()` was defined but never called and every
check inside it (everything this docstring claims to cover except the
module-level checks further down) never executed. `asyncio.run(main())` now
runs first; the final line this file prints ("N checks, M async") is the
floor a future regression of the same shape trips: it fails outright if the
async count is ever 0 again.
"""
import asyncio
import json
import pathlib
import re as _re_mod
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mcp_client import auto_confirm, over_stdio

FAILS = []
#: Every check() call, pass or fail — the raw material for the "N checks, M
#: async" floor line at the end of this file.
TOTAL = 0


def check(name, cond, detail=""):
    global TOTAL
    TOTAL += 1
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


async def _txt(r) -> str:
    """Best-effort text extraction from a CallToolResult."""
    if hasattr(r, "structured_content") and r.structured_content is not None:
        return json.dumps(r.structured_content)
    if hasattr(r, "content") and r.content:
        return r.content[0].text
    return str(r)


async def main():
    # install_package (step 7 below) is now gated behind a confirmation —
    # codecalc/confirmation.py — so this client needs an elicitation_callback
    # to get past it. `auto_confirm` always answers `confirm: true`; the
    # decline/cancel/malformed paths have their own coverage in
    # tests/test_confirmation_gate.py.
    async with over_stdio(elicitation_callback=auto_confirm) as client:
        tools = (await client.list_tools()).tools
        names = sorted(t.name for t in tools)
        print("tools:", len(names), names)
        for want in ["session_start", "session_stop", "session_list",
                     "session_files", "session_read_file", "session_write_file",
                     "session_artifacts", "install_package", "execute_code_stream"]:
            check(f"tool present: {want}", want in names)

        # 1. session lifecycle + stateful python
        s = await client.call_tool("session_start", {"language": "python3"})
        sid = json.loads(await _txt(s))["session_id"]
        check("session_start returns id", bool(sid), f"-> {sid}")

        r1 = await client.call_tool("execute_code", {"language": "python3",
                                                     "code": "x = 42", "session_id": sid})
        r2 = await client.call_tool("execute_code", {"language": "python3",
                                                     "code": "print(x)", "session_id": sid})
        check("stateful: x persists", "42" in await _txt(r2))

        # 2. file tools
        w = await client.call_tool("session_write_file", {"session_id": sid, "path": "in/data.csv", "content": "a,b\n1,2"})
        r = await client.call_tool("session_read_file", {"session_id": sid, "path": "in/data.csv"})
        check("write+read file", "a,b" in await _txt(r))
        f = await client.call_tool("session_files", {"session_id": sid})
        check("list files", "in/" in await _txt(f))

        # 3. artifact from executed code
        a = await client.call_tool("execute_code", {"language": "python3",
                                                    "code": "open('out.json','w').write('[9,9]')",
                                                    "session_id": sid})
        art = await client.call_tool("session_artifacts", {"session_id": sid})
        check("artifacts detected", "out.json" in await _txt(art))

        # 4. verdict + limits on plain execute
        v = await client.call_tool("execute_code", {"language": "python3",
                                                    "code": "print('hi')"})
        vt = await _txt(v)
        check("verdict present", '"verdict": "OK"' in vt, f"-> {vt[:80]}")
        t = await client.call_tool("execute_code", {"language": "python3",
                                                    "code": "while True: pass", "timeout": 2})
        tt = await _txt(t)
        check("TLE verdict", "TLE" in tt)

        # 5. compact mode
        c = await client.call_tool("execute_code", {"language": "python3",
                                                    "code": "print('z')", "compact": True})
        ct = await _txt(c)
        cj = json.loads(ct)
        # Assert the DECODED value, not the serialized spelling: Python emits
        # CRLF line endings on Windows, so a literal '"stdout": "z\\n"' search
        # against the raw JSON text never matches there even though the
        # decoded stdout is the same one line of output either way.
        check("compact mode",
              cj.get("stdout", "").strip() == "z" and "cpu_ms" not in cj,
              f"-> {ct[:80]}")

        # 6. streaming
        st = await client.call_tool("execute_code_stream", {"language": "python3",
                                                            "code": "import time\nfor i in range(3):\n print('tick', i); time.sleep(0.3)"})
        stt = await _txt(st)
        sj = json.loads(stt)
        if sj.get("streamed") is False:
            # The documented no-native fallback: execute_code_stream runs
            # non-streaming and says so via streamed=False plus a note,
            # rather than raising or silently omitting the field. That is a
            # supported outcome, not a failure — assert its actual content.
            check("streaming falls back to non-streaming (documented no-native mode)",
                  "tick" in sj.get("stdout", "") and "no native executor" in sj.get("note", ""),
                  f"-> streamed={sj.get('streamed')} note={sj.get('note')!r}")
        else:
            check("streaming returns result",
                  sj.get("streamed") is True and "tick" in stt and "streamed_partial" in sj,
                  f"-> streamed={sj.get('streamed')}")

        # 7. package install (network; small pure-python pkg)
        p = await client.call_tool("install_package", {"language": "python3",
                                                       "package": "six", "session_id": sid})
        pt = await _txt(p)
        check("install_package six", '"ok": true' in pt, f"-> {pt[:120]}")

        # 8. cleanup
        await client.call_tool("session_stop", {"session_id": sid})

    # ── #88: the two entry points and `doctor` ────────────────────────────────
# `uvx codecalc` and `pipx run codecalc` always worked — they use the console
# script in [project.scripts]. `python -m codecalc` did not, because there was
# no __main__.py, and that is the form people reach for inside a venv they have
# not activated.
import subprocess as _sp2
import sys as _sys2

_r = _sp2.run([_sys2.executable, "-m", "codecalc", "doctor"],
              capture_output=True, text=True, timeout=180)
check("`python -m codecalc doctor` runs", _r.returncode == 0,
      f"-> rc={_r.returncode} {(_r.stderr or '')[-90:]!r}")
for _needle in ("execution backend", "runtimes", "mcpServers"):
    check(f"  ...and reports {_needle!r}", _needle in _r.stdout,
          f"-> {_r.stdout[:70]!r}")

# The count it prints must be the one the README states and check_claims
# enforces. A raw len(LANGUAGES) is 32 because `c++` and `cpp` are one language
# written twice; printing that would put a third number into circulation.
import re as _re2

from codecalc import registry as _reg2

_readme_n = int(_re2.search(r"\*\*(\d+) languages\*\*",
                            pathlib.Path("README.md").read_text(encoding="utf-8")).group(1))
_m = _re2.search(r"runtimes\s+(\d+)/(\d+)", _r.stdout)
check("doctor's language count matches the README's",
      _m is not None and int(_m.group(2)) == _readme_n,
      f"-> doctor={_m.group(2) if _m else '?'} README={_readme_n} "
      f"raw_registry={len(_reg2.LANGUAGES)}")

# The no-argument path is what an MCP client spawns. Anything that made it
# print to stdout or exit would look like a protocol error to the client.
_probe_src = (
    "import sys; sys.argv=['codecalc']; "
    "from codecalc import server; print('MAIN_RESOLVES', callable(server.main))"
)
_r2 = _sp2.run([_sys2.executable, "-c", _probe_src],
               capture_output=True, text=True, timeout=120)
check("the no-argument entry point is still the MCP server",
      "MAIN_RESOLVES True" in _r2.stdout, f"-> {_r2.stdout[:60]!r}")

# ── #117: compact mode must not hide an unapplied guarantee ───────────────
# The old compact result was exactly {ok, verdict, stdout, exit_code}, so
# `execute_code(no_net=True, compact=True)` on a platform without the shim came
# back looking like a clean sandboxed run with no `unenforced` at all. Token
# efficiency that drops the disclosure is the defect SECURITY.md names by name:
# "anything that makes the server report a guarantee it did not apply".
#
# Driven through a stateful session, because that is the shape where
# `unenforced` is reliably NON-empty on every platform (#104 made the structural
# gaps unconditional). Asserting on a one-shot run would pass on Linux with the
# Rust backend, where `unenforced` is legitimately [] and the whole risk is
# invisible.
from codecalc import executor as _exec117
from codecalc import server as _srv117
from codecalc import sessions as _sess117

_s117 = _srv117.session_start("python3")
if not _s117.get("ok"):
    print(f"SKIP #117 compact disclosure (no python3 worker: {_s117.get('error')})")
else:
    _sid117 = _s117["session_id"]
    try:
        _full117 = _srv117.execute_code("python3", "print(1)", session_id=_sid117, no_net=True)
        _comp117 = _srv117.execute_code("python3", "print(1)", session_id=_sid117,
                                        no_net=True, compact=True)
        _fu = _full117.get("unenforced") or []
        _cu = _comp117.get("unenforced") or []
        check("a non-empty `unenforced` survives compact mode",
              bool(_cu), f"-> full={len(_fu)} compact={len(_cu)}")
        check("  ...naming every guarantee the full result named",
              len(_cu) == len(_fu) and
              all(e.split(":", 1)[0].strip() in _cu for e in _fu),
              f"-> full={[e.split(':',1)[0] for e in _fu]} compact={_cu}")
        check("  ...and points at where the explanations are",
              "unenforced_detail" in _comp117,
              f"-> {_comp117.get('unenforced_detail')!r}")
        check("compact is still smaller than full",
              len(json.dumps(_comp117)) < len(json.dumps(_full117)),
              f"-> {len(json.dumps(_comp117))} vs {len(json.dumps(_full117))} chars")
    finally:
        _sess117.stop(_sid117)

# An EMPTY disclosure is omitted — saying nothing costs tokens for nothing.
#
# Called through compact_result directly, the way the output_error pair below
# already is. Driving it through execute_code made the assertion depend on which
# BACKEND happened to be built: only the native executor enforces everything, so
# only there is `unenforced` empty and only there was this branch reachable at
# all. On a fallback host the Python backend truthfully discloses
# `peak_memory_kb`, compaction correctly RETAINS that disclosure, and this check
# failed — reporting a defect where the product was right, and reporting nothing
# at all about the rule it was named after. The rule belongs to compact_result,
# so compact_result is what gets called.
check("an empty `unenforced` is omitted from a compact result",
      "unenforced" not in _srv117.compact_result(
          {"ok": True, "verdict": "OK", "stdout": "42", "exit_code": 0,
           "unenforced": []}))
check("  ...and an empty one takes no `unenforced_detail` with it",
      "unenforced_detail" not in _srv117.compact_result(
          {"ok": True, "verdict": "OK", "stdout": "42", "exit_code": 0,
           "unenforced": []}))

# End-to-end on WHICHEVER backend is present: the disclosure is there exactly
# when there was something to disclose. That equivalence is true on both, so a
# fallback host gets a real assertion here rather than a skip or a false red.
_clean117 = _srv117.execute_code("python3", "print(6*7)", compact=True)
_fullclean117 = _srv117.execute_code("python3", "print(6*7)")
check("  ...and end-to-end, `unenforced` appears exactly when non-empty",
      ("unenforced" in _clean117) == bool(_fullclean117.get("unenforced")),
      f"-> backend={_exec117.backend()} compact_keys={sorted(_clean117)} "
      f"full_unenforced={_fullclean117.get('unenforced')}")
check("  ...with the program's own output intact",
      _clean117.get("stdout", "").strip() == "42",
      f"-> {_clean117.get('stdout', '')!r}")

# output_error is the other disclosure field and takes the same path.
check("compact_result keeps a non-empty output_error",
      _srv117.compact_result({"ok": False, "verdict": "RTE", "stdout": "",
                              "exit_code": 1, "output_error": "could not read stdout"}
                             ).get("output_error") == "could not read stdout")
check("  ...and omits a null one",
      "output_error" not in _srv117.compact_result(
          {"ok": True, "verdict": "OK", "stdout": "x", "exit_code": 0,
           "output_error": None}))

# ── #323: compact mode must not hide WHY an executed run failed ───────────
# Same defect class as #117, one shape over: #117 was `code`/`error`/`remedy`
# for a REJECTED-before-execution result (nothing to fall back on but those
# four); #323 is `stderr` for an EXECUTED-and-failed one. Before this fix
# `stderr` was unconditionally absent from `_COMPACT_ALWAYS`/
# `_COMPACT_DISCLOSURE`, so `execute_code(compact=True)` on a program that
# wrote its own failure reason to stderr and exited nonzero reported
# `ok: false` with no statement of why — the exact repro in the ticket.
import shutil as _shutil323

from codecalc import errors as _errors323

# 1. A real runtime failure: the program's own stderr must survive compact
# mode verbatim, matching the non-compact call byte for byte.
_rte_code = 'import sys\nsys.stderr.write("WHY IT FAILED\\n")\nsys.exit(7)'
_full323 = _srv117.execute_code("python3", _rte_code)
_comp323 = _srv117.execute_code("python3", _rte_code, compact=True)
check("compact keeps stderr on a runtime failure",
      _comp323.get("ok") is False and _comp323.get("exit_code") == 7
      and "WHY IT FAILED" in _comp323.get("stderr", ""),
      f"-> {_comp323}")
check("  ...verbatim against the non-compact call",
      _comp323.get("stderr") == _full323.get("stderr"),
      f"-> compact={_comp323.get('stderr')!r} full={_full323.get('stderr')!r}")

# 2. A compile-time failure: the compiler's own diagnostic, not the program's,
# is what stderr carries here — same rule, different phase. Skipped (not
# failed) when no C compiler is resolvable, the same shutil.which pattern
# test_platform_contract.py already uses for this exact language.
if _shutil323.which("gcc") or _shutil323.which("cc"):
    _c_src = "int main(){ this is not c ;}"
    _full323c = _srv117.execute_code("c", _c_src)
    _comp323c = _srv117.execute_code("c", _c_src, compact=True)
    check("compact keeps the compiler diagnostic on a C compile failure",
          _comp323c.get("ok") is False
          and bool(_comp323c.get("stderr", "").strip()),
          f"-> {_comp323c}")
    # Not a byte-for-byte "verbatim" comparison like the runtime-failure case
    # above: each call compiles into its OWN fresh temp workdir, so the
    # compiler embeds a different `codecalc-<run>-<hash>/.../main.c` path
    # per call even for the identical source — comparing full strings would
    # fail on that path, not on a real difference in what was kept. The
    # diagnostic TEXT gcc actually produced for this source is
    # call-invariant, so that is what gets compared instead, with the
    # per-run directory name (not a literal /tmp path — no S108 concern,
    # this only ever reads a string the sandbox already produced) normalized
    # out first.
    _rundir_re323 = _re_mod.compile(r"codecalc-\S+?/")
    check("  ...with the same diagnostic text as the non-compact call",
          "unknown type name" in _comp323c.get("stderr", "")
          and "unknown type name" in _full323c.get("stderr", "")
          and _rundir_re323.sub("<rundir>/", _comp323c.get("stderr", ""))
          == _rundir_re323.sub("<rundir>/", _full323c.get("stderr", "")),
          f"-> compact={_comp323c.get('stderr')!r} full={_full323c.get('stderr')!r}")
else:
    print("SKIP #323 C compile-failure case (no gcc/cc resolvable)")

# 3. A clean run drops stderr — compact mode still saves the tokens it was
# built for on the success path, which is the one this fix must not regress.
_clean323 = _srv117.execute_code("python3", "print(6*7)", compact=True)
check("compact still omits stderr on a successful run",
      "stderr" not in _clean323, f"-> {sorted(_clean323)}")

# 4. The #117 ordering property must still hold with the new stderr branch in
# place: a rejected-before-execution result (no real verdict/exit_code to
# trigger the new check) still classifies IDENTICALLY under compact=True.
# tests/test_error_codes.py pins this end-to-end already; re-asserted here,
# next to the fix, so a regression in this exact function is caught locally.
_reject323 = _srv117.execute_code("nosuchlang", "x", compact=True)
check("compact_result's stderr branch does not disturb the #117 ordering fix",
      _reject323.get("code") == _errors323.VALIDATION and bool(_reject323.get("error")),
      f"-> {_reject323}")

# 5. Unit-level: each of the three independent triggers keeps stderr, in
# isolation, called through compact_result directly the way output_error is
# above — this is the case a purely end-to-end test cannot reach, since a
# real backend never emits ok=false with exit_code=0 (only OLE does, and only
# on a truncated stream that would make the stderr assertion itself noisy).
check("ok=false alone keeps stderr",
      _srv117.compact_result({"ok": False, "verdict": "RTE", "stdout": "",
                              "stderr": "boom", "exit_code": None}
                             ).get("stderr") == "boom")
check("a nonzero exit_code alone keeps stderr",
      _srv117.compact_result({"ok": True, "verdict": "OK", "stdout": "",
                              "stderr": "boom", "exit_code": 3}
                             ).get("stderr") == "boom")
check("a non-OK verdict alone keeps stderr (the OLE/ok=true/exit_code=0 case)",
      _srv117.compact_result({"ok": True, "verdict": "OLE", "stdout": "",
                              "stderr": "boom", "exit_code": 0}
                             ).get("stderr") == "boom")
check("a failed run with genuinely empty stderr still carries the key",
      "stderr" in _srv117.compact_result({"ok": False, "verdict": "RTE", "stdout": "",
                                          "stderr": "", "exit_code": 1}),
      "-> the key must say 'nothing more to see', not go missing")


# ── #88 item 5: the extras degrade, they do not crash ─────────────────────
# The imports were ALWAYS lazy; what was missing is what happens when one
# fails. An ImportError escaping a tool says "this server is broken" when the
# truth is "you installed the small variant and asked for a tool outside it".
from codecalc import optional as _opt

check("optional.have() answers without importing",
      _opt.have("sympy") is True and _opt.have("definitely_not_a_module") is False,
      f"-> sympy={_opt.have('sympy')}")

# Simulated absence, because the test venv HAS sympy. Asserting the message
# rather than the type: the message is what a caller reads out of the error
# field, and it is the only part that tells them what to do.
try:
    raise _opt.MissingExtra("sympy", "symbolic")
except ImportError as _exc:          # MissingExtra subclasses ImportError
    _msg = str(_exc)
for _needle in ("sympy is not installed", "symbolic", "pip install", "codecalc[full]"):
    check(f"MissingExtra names {_needle!r}", _needle in _msg, f"-> {_msg[:80]!r}")
check("  ...and lists the tools it affects",
      "evaluate_expression" in _msg, f"-> {_msg[-90:]!r}")

# Every module that reaches a heavy dependency must go through require(), or a
# raw ImportError gets back out. Checked against the source, since the test
# environment has the extras installed and cannot prove it by running.
import pathlib as _pl2

for _mod in ("logic.py", "exact.py", "units.py", "parsing.py"):
    _src = _pl2.Path("codecalc") / _mod
    _text = _src.read_text(encoding="utf-8")
    _raw = [l.strip() for l in _text.splitlines()
            if l.strip().startswith(("import sympy", "import z3", "import tree_sitter"))]
    check(f"{_mod}: no raw import of a heavy dependency", not _raw, f"-> {_raw}")

check("pyproject declares the extras",
      all(x in _pl2.Path("pyproject.toml").read_text(encoding="utf-8")
          for x in ("[project.optional-dependencies]", "symbolic", "parsing", "full")),
      "-> optional-dependencies missing")

# ── estimate vs measurement, stated in the result ─────────────────────────
# analyze_complexity and benchmark answer the same question and sit next to
# each other in the tool list. One counts loops; the other runs the program at
# increasing sizes. `analysis: tree-sitter|regex-fallback` distinguished PARSED
# from GUESSED, which is a different axis and was the only one reported — so a
# caller could see analysis="tree-sitter" and reasonably hear "we determined
# the complexity" about a number nobody measured.
from codecalc import complexity as _cx
from codecalc import tools as _tools

_static = _cx.analyze("for i in range(n):\n    for j in range(n):\n        pass", "python3")
check("analyze_complexity declares itself an estimate",
      _static.get("method") == "static-estimate", f"-> {_static.get('method')!r}")
check("  ...while still reporting HOW it read the source",
      _static.get("analysis") in ("tree-sitter", "regex-fallback"),
      f"-> {_static.get('analysis')!r}")
# The two axes are orthogonal: parsed-vs-guessed does not imply measured.
check("  ...and the two are separate fields, not one",
      _static.get("method") != _static.get("analysis"),
      f"-> method={_static.get('method')!r} analysis={_static.get('analysis')!r}")

_bench = _tools.benchmark(
    "import sys\nn=int(sys.stdin.readline())\nx=[i*i for i in range(n)]\nprint(len(x))",
    "python3", sizes="2000,4000,8000")
check("benchmark declares itself empirical",
      _bench.get("ok") is True and _bench.get("method") == "empirical",
      f"-> ok={_bench.get('ok')} method={_bench.get('method')!r}")

# A failed run measured nothing, so it must not claim to have. This is the
# case that would otherwise let a caller relay "empirical" about an error.
_failed = _tools.benchmark("import sys\nraise SystemExit(1)", "python3", sizes="100,200")
check("a failed benchmark claims no method at all",
      _failed.get("ok") is False and "method" not in _failed,
      f"-> ok={_failed.get('ok')} method={_failed.get('method')!r}")

# `_measure`'s "program failed at n=N" message used to be the ONLY text the
# top-level `benchmark` tool's automatic `errors.ensure_code` (server.py's
# `_coded` wrapper) had to classify from — generic enough that a language
# with no runtime installed at all was misclassified `internal` rather than
# `runtime_unavailable`, the wrong remedy for "install the language". The
# underlying executor result's OWN `error` (e.g. a spawn failure) is now
# appended to that message, so the SAME automatic classification correctly
# reads it. `CODECALC_RUNTIME_PATH` (not the real PATH) is what both
# backends resolve a runtime from, so pointing it at an empty scratch
# directory makes `lua` unresolvable regardless of what is actually
# installed here.
import os as _os_bench
import shutil as _shutil_bench
import tempfile as _tempfile_bench

from codecalc import errors as _errors_bench
from codecalc import server as _server_bench

_no_runtime_dir = _tempfile_bench.mkdtemp(prefix="codecalc-bench-noruntime-")
_saved_runtime_path = _os_bench.environ.get("CODECALC_RUNTIME_PATH")
_os_bench.environ["CODECALC_RUNTIME_PATH"] = _no_runtime_dir
try:
    _no_runtime = _server_bench.benchmark(
        "n=int(input())\nprint(n)", language="lua", sizes="100,200,300")
finally:
    if _saved_runtime_path is None:
        _os_bench.environ.pop("CODECALC_RUNTIME_PATH", None)
    else:
        _os_bench.environ["CODECALC_RUNTIME_PATH"] = _saved_runtime_path
    _shutil_bench.rmtree(_no_runtime_dir, ignore_errors=True)
check("a benchmark against a missing runtime is classified runtime_unavailable, not internal",
      _no_runtime.get("code") == _errors_bench.RUNTIME_UNAVAILABLE,
      f"-> code={_no_runtime.get('code')} error={str(_no_runtime.get('error'))[:120]!r}")

# ── benchmark classifier: honest at the documented 3-size floor (GH #327) ──
# `benchmark()`'s own docstring says 3 sizes is the minimum; #327 showed that
# at exactly that minimum, `estimate` was decided by ONE doubling ratio (no
# floor gated it) while the curve fit that should have arbitrated could not
# even run (baseline subtraction zeroed the first of 3 points, leaving the
# fit's own `len(pts) >= 3` floor with only 2). The result: a genuinely O(n)
# program reported as O(n^3), O(n^2), or O(n log n) purely from how many
# sizes were requested. These checks hit `_fit_class` / `_decide_estimate`
# directly with SYNTHETIC timing tables -- the bug was in the decision, not
# in subprocess timing, so nothing here spawns a process. The one exception
# (below) runs the real O(n) program from the issue at 3 sizes, and asserts
# only the same non-negative claim these synthetic cases prove exactly.


def _decide(sizes, times):
    """Run the same two-estimator decision `benchmark()` runs, on canned data.

    Mirrors `benchmark()`'s own loop exactly, including `raw_ratios` (the
    SAME doubling pairs as `ratios`, from UNSUBTRACTED `times`) -- the
    round-3 cross-vendor-review cross-check `_decide_estimate`'s
    STRONG_EXPONENTIAL_RATIO shortcut now requires alongside `ratios` itself.
    """
    baseline = min(times)
    corrected = [max(t - baseline, 0.0) for t in times]
    fit = _tools._fit_class(sizes, times)  # raw times: see _fit_class's docstring
    ratios = []
    raw_ratios = []
    for (na, ta, tra), (nb, tb, trb) in zip(
            zip(sizes, corrected, times), zip(sizes[1:], corrected[1:], times[1:])):
        if nb == 2 * na and ta > 3.0:
            ratios.append(round(tb / ta, 2))
            raw_ratios.append(round(trb / tra, 2))
    estimate, basis = _tools._decide_estimate(ratios, fit, raw_ratios)
    return estimate, basis, fit, ratios


# The issue's own repro: 200k/400k/800k -> 49/56/107ms, previously "O(n^3)"
# with doubling_ratios=[8.29] and candidate_scores=[] (the fit couldn't run).
_e1, _basis1, _fit1, _r1 = _decide([200_000, 400_000, 800_000], [49, 56, 107])
check("3-size startup-dominated linear table does not claim O(n^2)/O(n^3)",
      not _e1.startswith("O(n^2)") and not _e1.startswith("O(n^3)"),
      f"-> estimate={_e1!r} basis={_basis1!r} ratios={_r1}")
check("  ...and the curve fit is REACHABLE at 3 sizes now (candidate_scores populated)",
      len(_fit1.get("scores", [])) > 0, f"-> scores={_fit1.get('scores')}")
check("  ...though at exactly 3 points (1 residual df over 8 classes) it is not trusted",
      _basis1 == "inconclusive" and "inconclusive" in _e1,
      f"-> basis={_basis1!r} estimate={_e1!r}")

# A second noisy rerun of the same 3 sizes (previously "O(n^2)" with a single
# ratio of 4.29) must land the same honest place, not a different wrong one.
_e2, _basis2, _fit2, _r2 = _decide([200_000, 400_000, 800_000], [47, 61, 107])
check("  ...a different noisy 3-size rerun of the same program agrees (not O(n^2)/O(n^3))",
      not _e2.startswith("O(n^2)") and not _e2.startswith("O(n^3)"),
      f"-> estimate={_e2!r} basis={_basis2!r} ratios={_r2}")

# The reporter's 5-size linear table (2M..32M doubling, 188..2546ms) has 3
# doubling ratios ([2.71, 2.36, 2.18] -- MIN_ROBUST_RATIOS is 3) and was
# already correctly "O(n)" via the ratio median even before this fix; pinned
# here so the fix does not regress the case that was already working.
_e3, _basis3, _fit3, _r3 = _decide(
    [2_000_000, 4_000_000, 8_000_000, 16_000_000, 32_000_000],
    [188, 358, 648, 1272, 2546])
check("5-size linear table classifies O(n) via the robust ratio median",
      _e3 == "O(n)" and _basis3 == "ratio-median",
      f"-> estimate={_e3!r} basis={_basis3!r} ratios={_r3}")

# A clean 5-size quadratic table (synthetic: t = 50ms startup + 0.0005*n^2)
# must still classify O(n^2) -- the fix must not make genuine quadratic work
# harder to detect, only stop the 3-size floor from claiming it falsely.
_quad_sizes = [1000, 2000, 4000, 8000, 16000]
_quad_times = [50 + 0.0005 * n * n for n in _quad_sizes]
_e4, _basis4, _fit4, _r4 = _decide(_quad_sizes, _quad_times)
check("5-size quadratic table still classifies O(n^2)",
      _e4 == "O(n^2)", f"-> estimate={_e4!r} basis={_basis4!r} ratios={_r4}")

# Cross-vendor review (Codex) of the fix above found the OPPOSITE failure:
# genuinely exponential growth with too few doubling ratios for a robust
# median (2, one short of MIN_ROBUST_RATIOS) used to fall all the way through
# to the curve fit -- which has no exponential shape in `_CLASSES` at all --
# and confidently reported "O(n^3)" at relative_error 41.0 (a 4100% average
# miss) with a physically-impossible negative intercept (startup overhead
# cannot be negative). Must now land on the exponential class itself (via the
# STRONG_EXPONENTIAL_RATIO override, since both ratios clear it) or say so
# honestly -- never a confident polynomial.
_eexp, _basisexp, _fitexp, _rexp = _decide([10, 20, 40, 80], [1, 10, 200, 5000])
check("exponential 4-size table (Codex review) classifies O(c^n), not a polynomial",
      _eexp.startswith("O(c^n)") or _basisexp == "inconclusive",
      f"-> estimate={_eexp!r} basis={_basisexp!r} ratios={_rexp}")
check("  ...and specifically does NOT trust the curve fit's bad-but-lowest-ranked O(n^3)",
      not _eexp.startswith("O(n^3)"), f"-> estimate={_eexp!r} best={_fitexp.get('best_score')}")

# The same exponential SHAPE with non-doubling sizes: no doubling_ratios exist
# at all (STRONG_EXPONENTIAL_RATIO cannot fire on an empty list), so this
# exercises the OTHER guard -- the curve fit's own quality gate
# (MAX_TRUSTED_RELATIVE_ERROR / INTERCEPT_NOISE_TOLERANCE_MS) rejecting a
# technically-ranked but not-actually-fitting "winner" on its own.
_eexp2, _basisexp2, _fitexp2, _rexp2 = _decide([10, 17, 29, 50], [1, 8, 190, 4800])
check("exponential shape, non-doubling sizes (no ratios at all): quality gate alone catches it",
      _basisexp2 == "inconclusive" and _rexp2 == [],
      f"-> estimate={_eexp2!r} basis={_basisexp2!r} best={_fitexp2.get('best_score')}")

# A SECOND cross-vendor review round found the STRONG_EXPONENTIAL_RATIO
# shortcut above still trusted a SINGLE ratio: "every available ratio
# agrees" with `all()` over a set of one is vacuously true. Repro: sizes
# [10,20,40] / times [1000,1004,1064] is quadratic-ish and startup-dominated
# -- NOT exponential -- but baseline-subtracts to corrected [0,4,64]; the
# first gap is dropped as always, leaving exactly ONE ratio (64/4 = 16.0), a
# legitimate `> 3.0` corrected denominator (4ms) still small enough to
# inflate. Must now be `inconclusive` (3 sizes: MIN_FIT_POINTS and
# MIN_ROBUST_RATIOS both out of reach, and 1 ratio is below
# MIN_STRONG_EXPONENTIAL_RATIOS too) -- never O(c^n), and never any other
# confident class either.
_equad, _basisquad, _fitquad, _rquad = _decide([10, 20, 40], [1000, 1004, 1064])
check("quadratic-ish 3-size table (round-3 review) does not fire the exponential shortcut",
      not _equad.startswith("O(c^n)"), f"-> estimate={_equad!r} basis={_basisquad!r} ratios={_rquad}")
check("  ...specifically: exactly 1 ratio, below MIN_STRONG_EXPONENTIAL_RATIOS (2)",
      len(_rquad) == 1 and len(_rquad) < _tools.MIN_STRONG_EXPONENTIAL_RATIOS,
      f"-> ratios={_rquad} MIN_STRONG_EXPONENTIAL_RATIOS={_tools.MIN_STRONG_EXPONENTIAL_RATIOS}")
check("  ...and lands honestly inconclusive (3 sizes can decide nothing else here)",
      _basisquad == "inconclusive", f"-> basis={_basisquad!r} estimate={_equad!r}")

# The documented COST of both round-3 guards together: a GENUINELY
# exponential 3-size table can no longer trigger the override either -- 3
# sizes structurally cannot produce the 2 ratios MIN_STRONG_EXPONENTIAL_
# RATIOS now requires (at most 1 doubling ratio exists at 3 sizes at all;
# see `_classify_by_ratio`'s "first gap always discarded" note). This is
# intentional, not a regression: a real single ratio and a noise-manufactured
# one are indistinguishable from inside `_decide_estimate` without a second
# ratio to check it against, so the guard costs real 3-size exponential
# detection to close the false-positive hole above. `inconclusive` here is
# the documented right answer, not a bug.
_e3xp, _basis3xp, _fit3xp, _r3xp = _decide([100_000, 200_000, 400_000], [10, 50, 900])
check("genuinely exponential 3-size table: cannot trigger the override either (documented cost)",
      _basis3xp == "inconclusive" and not _e3xp.startswith("O(c^n)"),
      f"-> estimate={_e3xp!r} basis={_basis3xp!r} ratios={_r3xp}")

# A 3-size table where interpreter/subprocess startup (~300ms) swamps a tiny
# real signal: the doubling ratio computed from it is unreliable (a single
# ratio, from near-noise-floor corrected times) and the fit is starved to 3
# points either way -- must not surface as a confident polynomial claim.
_e5, _basis5, _fit5, _r5 = _decide([10_000, 20_000, 40_000], [300.0, 305.0, 320.0])
check("3-size startup-dominated table (tiny real signal) is not a confident O(n^2)/O(n^3)/O(c^n) claim",
      not any(_e5.startswith(c) for c in ("O(n^2)", "O(n^3)", "O(c^n)")),
      f"-> estimate={_e5!r} basis={_basis5!r} ratios={_r5}")

# `estimate_basis` is new: a caller must be able to tell WHICH estimator
# produced `estimate` without parsing prose out of `ratio_confidence`.
check("estimate_basis is one of the documented values",
      {_basis1, _basis2, _basis3, _basis4, _basis5, _basisexp, _basisexp2,
       _basisquad, _basis3xp} <= set(_tools.ESTIMATE_BASES),
      f"-> {_tools.ESTIMATE_BASES}")

# One live run of the issue's actual O(n) program at exactly 3 sizes (this
# box, loaded, so the class itself is NOT asserted -- only the same
# non-negative claim proven exactly above with synthetic data).
_live = _tools.benchmark(
    "import sys\nn=int(sys.stdin.readline())\ns=0\nfor i in range(n):\n"
    "    s += i*i % 7919\nprint(s)",
    "python3", sizes="200000,400000,800000")
_live_est = _live.get("estimate", "")
check("live 3-size run of the issue's O(n) program: not a confident O(n^2)/O(n^3) claim",
      _live.get("ok") is True and
      (_live.get("estimate_basis") == "inconclusive" or
       not _live_est.startswith(("O(n^2)", "O(n^3)"))),
      f"-> estimate={_live_est!r} basis={_live.get('estimate_basis')!r} "
      f"ratios={_live.get('doubling_ratios')}")

# ── benchmark classifier: a PUBLIC-path regression, not just the private one ─
# Every check above calls `_tools._fit_class`/`_tools._decide_estimate`
# directly -- precise and fast, but on the OLD (pre-#327) code they die with
# `AttributeError` (`_decide_estimate` did not exist yet) instead of actually
# demonstrating the bad classification; a reviewer flagged that a test
# suite's own regression protection should not depend on a function that is
# part of the fix. This one drives the real public entry point, `benchmark()`
# itself, with only `_measure` (the subprocess-timing layer) stubbed to
# return the issue's own 3-size table deterministically -- on old code it
# still RUNS to completion and asserts the wrong class, a real failure
# instead of a crash.
def _public_benchmark(sizes_csv: str, durations_by_n: dict):
    """Call the real `tools.benchmark()` with only `_measure` stubbed to
    return `durations_by_n` deterministically -- everything else (auto-scale
    check, `_fit_class`, `_decide_estimate`, result assembly) runs for real.
    """
    orig_measure = _tools._measure

    def _stub_measure(language, code, sizes, timeout, repeats, deadline=None, on_progress=None):
        return ([{"n": n, "ok": True, "duration_ms": durations_by_n[n],
                  "all_runs_ms": [durations_by_n[n]], "stdout": "", "stderr": ""}
                 for n in sizes], None)

    _tools._measure = _stub_measure
    try:
        return _tools.benchmark("ignored -- _measure is stubbed, never actually run",
                                sizes=sizes_csv)
    finally:
        _tools._measure = orig_measure


_pub = _public_benchmark("200000,400000,800000",
                         {200_000: 49.0, 400_000: 56.0, 800_000: 107.0})
_pub_est = _pub.get("estimate", "")
check("public tools.benchmark(), with only timing stubbed, reproduces the same honest verdict",
      _pub.get("ok") is True and _pub.get("method") == "empirical" and
      not _pub_est.startswith(("O(n^2)", "O(n^3)")),
      f"-> ok={_pub.get('ok')} estimate={_pub_est!r} basis={_pub.get('estimate_basis')!r} "
      f"ratios={_pub.get('doubling_ratios')} runs={[(r['n'], r['duration_ms']) for r in _pub.get('runs', [])]}")
check("  ...and the durations it reasoned over are exactly the canned ones (the stub took effect)",
      [r["duration_ms"] for r in _pub.get("runs", [])] == [49.0, 56.0, 107.0],
      f"-> {[r.get('duration_ms') for r in _pub.get('runs', [])]}")

# Round-3 cross-vendor review's own repro, through the SAME public path: sizes
# [10,20,40] / durations [1000,1004,1064] (quadratic-ish, startup-dominated,
# NOT exponential) -- on the code this fixes, the lone ratio (16.0) vacuously
# "agreed with itself" and fired the STRONG_EXPONENTIAL_RATIO shortcut for a
# confident O(c^n). Required outcome per review: inconclusive or O(n^2),
# never O(c^n).
_pub2 = _public_benchmark("10,20,40", {10: 1000.0, 20: 1004.0, 40: 1064.0})
_pub2_est = _pub2.get("estimate", "")
check("public tools.benchmark() on the round-3 repro table: inconclusive or O(n^2), never O(c^n)",
      _pub2.get("ok") is True and
      (_pub2.get("estimate_basis") == "inconclusive" or _pub2_est.startswith("O(n^2)")) and
      not _pub2_est.startswith("O(c^n)"),
      f"-> ok={_pub2.get('ok')} estimate={_pub2_est!r} basis={_pub2.get('estimate_basis')!r} "
      f"ratios={_pub2.get('doubling_ratios')} runs={[(r['n'], r['duration_ms']) for r in _pub2.get('runs', [])]}")

# ── the shipped skill (#88) ───────────────────────────────────────────────
# The tools exist to stop a model asserting numbers it did not compute. Nothing
# made a model REACH for them: no skill, no prompting guide, nothing in the
# tree. codecalc/SKILL.md is that, and it ships inside the package so it
# travels with the tools rather than living in a README nobody pastes.
_skill = pathlib.Path("codecalc/SKILL.md")
check("the skill ships inside the package", _skill.is_file(), f"-> {_skill}")
# Named `_skill_txt`, not `_txt`: this used to be a MODULE-LEVEL `_txt`,
# shadowing the async helper of the same name defined near the top of this
# file for the whole rest of the module (Python resolves a global by NAME at
# call time, not at def time). `main()`'s own `await _txt(s)` calls would
# have raised `TypeError: 'str' object is not callable` the moment `main()`
# actually ran — which it never did until the sys.exit-before-asyncio.run bug
# above was fixed, so this second bug was invisible right alongside it.
_skill_txt = _skill.read_text(encoding="utf-8")

check("  ...with skill frontmatter a client can load",
      _skill_txt.startswith("---") and "name: codecalc" in _skill_txt
      and "description:" in _skill_txt,
      f"-> {_skill_txt[:40]!r}")

# The two halves have different costs and different strictness, and the file
# has to say so or it is just advice.
for _needle, _why in (
    ("mandatory, no exceptions", "the call-triggers are hard rules"),
    ("Do not call these", "the anti-triggers exist, so the skill is not noise"),
    ("Never drop a field you do not understand", "the relay rule is stated"),
):
    # Detail on BOTH outcomes: a "-> missing X" printed next to PASS reads as
    # a broken check, which is how the first version of this looked.
    check(f"the skill states {_why}", _needle in _skill_txt,
          f"-> {'found' if _needle in _skill_txt else 'MISSING'} {_needle!r}")

# Operand count was the WRONG threshold and the file must not reintroduce it:
# 0.1 + 0.2 is two operands and the canonical failure; 2 + 3 + 4 is three and
# never wrong. Type predicts error, length does not.
_typed = "Operand count is **not** the test" in _skill_txt and "0.1 + 0.2" in _skill_txt
check("  ...and keys on type rather than operand count", _typed,
      f"-> type-vs-length rule {'present' if _typed else 'MISSING'}")

# THE GATE ITSELF MUST EXIST. This is not belt-and-braces: the skill shipped
# once already claiming to be gated, while the gate had been destroyed by a
# `git checkout scripts/check_claims.py` at the end of a mutation test — which
# reverts to HEAD, not to the previous edit. check_claims.py passed, because a
# check that is absent raises nothing. Deleting the gate is invisible to the
# gate.
#
# So the assertion lives HERE, in the suite, pointed at the script. It is the
# "every scan counts its inputs first" rule from CONTRIBUTING applied to a scan
# that had no inputs at all.
#: Strip `#` comments so a floor cannot be satisfied by prose describing the
#: gate it guards. Deliberately simple: check_claims.py has no `#` inside a
#: string literal, and the assertion below is over identifiers, not sentences.
_re_comments = _re_mod.compile(r"#[^\n]*")

_claims_src = pathlib.Path("scripts/check_claims.py").read_text(encoding="utf-8")
check("check_claims.py actually gates the skill",
      "SKILL" in _claims_src and "names tools that server.py does not define" in _claims_src,
      f"-> SKILL mentions: {_claims_src.count('SKILL')}")
check("  ...in both directions",
      "relay fields no tool returns" in _claims_src,
      f"-> relay-field half {'present' if 'relay fields no tool returns' in _claims_src else 'MISSING'}")

# Same floor, same reason, for the SECURITY.md counts. Deleting that gate would
# not turn anything red on its own — check_claims.py would simply stop reading
# the file and keep exiting 0 — which is exactly how the skill gate above was
# lost once already.
#
# Matched against the source with COMMENTS STRIPPED, which is load-bearing. The
# first version of this floor tested `"SECURITY.md" in _claims_src`, and the
# gate it guards is introduced by a long comment block that says "SECURITY.md"
# three times. Deleting the executable checks and leaving that comment kept the
# floor green — verified, it passed with the gate gone. That is the same defect
# as the `killpg` marker in check_parity.py, which matched a comment explaining
# why killpg is NOT used: a check satisfied by prose about the check.
#
# The tokens below are variable names, which cannot plausibly appear in prose,
# and they are required in the stripped source rather than anywhere in the file.
#
# Identifier presence alone is NOT enough, which a second reviewer pointed out
# after the comment fix above: `_sec_allow = []` with the comparison deleted
# keeps both names and gates nothing. So the floor also requires fragments that
# exist only inside the fail() calls — a message cannot survive the removal of
# the branch that raises it, and comment-stripping does not touch string
# literals, so these prove the comparisons are still there.
_claims_code = _re_comments.sub("", _claims_src)
_sec_names = "_sec_allow" in _claims_code and "_sec_langs" in _claims_code
# Fragments chosen to sit INSIDE one string literal each. The first attempt
# spanned an implicit f-string concatenation ("...env vars; the " + "executor
# permits...") and so matched nothing, failing a correct tree — the same
# "watched it fail for the wrong reason" trap as the \b marker in
# check_parity.py earlier today.
_sec_compares = ("allowed env vars; the" in _claims_code
                 and "languages; the registry" in _claims_code)
check("check_claims.py actually gates SECURITY.md",
      _sec_names and _sec_compares,
      f"-> names={_sec_names} comparisons={_sec_compares}")

# doctor has to name it, or nobody installs it.
_doc = _sp2.run([_sys2.executable, "-m", "codecalc", "doctor"],
                capture_output=True, text=True, timeout=180)
check("doctor points at the skill file",
      "SKILL.md" in _doc.stdout and "(MISSING)" not in _doc.stdout,
      f"-> {[l for l in _doc.stdout.splitlines() if 'skill' in l.lower()]}")

# THE BUG THIS FILE ONCE HAD: `sys.exit()` used to sit here, before
# `asyncio.run(main())` — every check above this point is a module-level
# statement that runs on import/exec, so they always ran; `main()` is a
# function whose body only runs when awaited, and `sys.exit()` raises
# `SystemExit` immediately, so the process exited before `main()` was ever
# called. Every MCP-session check this file's own docstring claims to cover
# (session lifecycle, files, artifacts, verdicts, compact mode, streaming,
# package install) lived inside `main()` and had never actually executed.
#
# `asyncio.run(main())` now runs FIRST, so its checks land in `FAILS`/`TOTAL`
# before the summary below reads them.
_sync_checks = TOTAL
asyncio.run(main())
_async_checks = TOTAL - _sync_checks

# The existence floor for the bug above: a future regression of the same
# shape (an early return/exit reinserted before this line) makes this trip
# rather than pass silently — `_async_checks` would go back to 0. ci-python.yml
# greps the "N checks, M async" line printed below for exactly this number, so
# the floor holds even if this in-file check is itself the thing removed.
check("the async MCP section actually ran (main() was awaited, not skipped)",
      _async_checks > 0,
      f"-> {_async_checks} checks ran inside main()")

print(f"\n=== {len(FAILS)} failures ===" if FAILS else "\n=== ALL NEW-FEATURE TESTS PASS ===")
print(f"{TOTAL} checks, {_async_checks} async")
sys.exit(1 if FAILS else 0)
