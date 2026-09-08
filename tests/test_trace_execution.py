"""`trace_execution` (codecalc/tracing.py): the harness never changes the
program's own behaviour, the trace is faithful, and the two recording caps
stop RECORDING without ever stopping the PROGRAM.

Standalone script, not pytest — see tests/conftest.py.

`PASS rust: ...` / `PASS python: ...` naming below (mirrors tests/
test_platform_contract.py's `_backend_name` convention) is what
CONTRIBUTING.md's "count `PASS rust:` lines" instruction reads to confirm
this suite actually exercised the native executor, not just the fallback.
"""

from __future__ import annotations

import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import executor, tracing

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")


BACKENDS = (("rust", False), ("python", True))


def _codecalc_exec_pids() -> set[int] | None:
    """PIDs currently running the resolved `codecalc-exec` binary, or `None`
    on a platform with no `/proc` (Windows, macOS) — copied from tests/
    test_execution_service.py's `_codecalc_exec_pids` rather than imported:
    that file is a standalone script that runs its whole suite at import
    time (see tests/conftest.py), so importing it here would re-run it as a
    side effect.
    """
    proc_root = pathlib.Path("/proc")
    if not proc_root.is_dir() or not executor._rust:
        return None
    target = pathlib.Path(executor._rust).resolve()
    pids: set[int] = set()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            exe = (entry / "exe").readlink()
        except OSError:
            continue
        if exe == target:
            pids.add(int(entry.name))
    return pids


# ── if/else + loop + function call ──────────────────────────────────────────
# Windows note: this source is a plain in-memory Python str, never written to
# disk by this file itself (tracing.py owns that write, via
# `executor._write_scratch_nofollow`, in binary/UTF-8 mode) — no newline='' or
# byte-comparison concern applies here.
_BRANCH_PROGRAM = (
    "def classify(n):\n"
    "    if n % 2 == 0:\n"
    "        return \"even\"\n"
    "    else:\n"
    "        return \"odd\"\n"
    "\n"
    "total = 0\n"
    "for i in range(3):\n"
    "    total += i\n"
    "    label = classify(i)\n"
    "print(total, label)\n"
)

for _name, _force_fallback in BACKENDS:
    _saved = executor._rust
    if _force_fallback:
        executor._rust = None
    try:
        _r = tracing.execute_trace("python3", _BRANCH_PROGRAM)
    finally:
        executor._rust = _saved

    check(f"{_name}: program ran to completion", _r.get("verdict") == "OK",
          f"-> verdict={_r.get('verdict')} stderr={_r.get('stderr')!r}")
    # i runs 0,1,2; total = 0+0+1+2 = 3; the LAST classify() call is
    # classify(2), which is even.
    check(f"{_name}: stdout is the real program output",
          _r.get("stdout") == "3 even\n", f"-> {_r.get('stdout')!r}")
    check(f"{_name}: not truncated", _r.get("truncated") is False)

    events = _r.get("events") or []
    check(f"{_name}: events were recorded", len(events) > 5, f"-> {len(events)}")
    # Event order: 'call' for classify() must appear before its own 'return',
    # and both must appear inside the module's own line-by-line trace in the
    # order the loop actually executed them — asserted on STEP numbers, not
    # list position, since step is the field a caller reads.
    calls = [e for e in events if e.get("event") == "call" and e.get("func") == "classify"]
    returns = [e for e in events if e.get("event") == "return" and e.get("func") == "classify"]
    check(f"{_name}: classify() was called 3 times (once per loop iteration)",
          len(calls) == 3, f"-> {len(calls)}")
    check(f"{_name}: every call has a matching return, in step order",
          len(returns) == 3
          and all(c["step"] < r["step"] for c, r in zip(calls, returns, strict=True)),
          f"-> calls={[c['step'] for c in calls]} returns={[r['step'] for r in returns]}")
    # Changed-locals: the call event for classify(0) must show n=0 bound as a
    # CHANGED local (nothing was there before), and the loop's own 'i'/'total'
    # updates must show up as changes on the module frame, not a full re-dump
    # of every local every step.
    check(f"{_name}: classify(0)'s call event shows n=0 as a changed local",
          calls[0].get("locals", {}).get("n") == "0", f"-> {calls[0].get('locals')}")
    module_lines = [e for e in events if e.get("func") == "<module>" and e.get("event") == "line"]
    check(f"{_name}: 'total' appears as a changed local exactly where it changes, "
          f"not on every module-level line",
          sum(1 for e in module_lines if "total" in e.get("locals", {})) < len(module_lines),
          f"-> total-changed={sum(1 for e in module_lines if 'total' in e.get('locals', {}))} "
          f"of {len(module_lines)} module lines")
    # return_value: classify(0) -> "even", classify(1) -> "odd", classify(2) -> "even".
    check(f"{_name}: return events carry the real return value repr",
          [r.get("return_value") for r in returns] == ["'even'", "'odd'", "'even'"],
          f"-> {[r.get('return_value') for r in returns]}")

    # branches: the `if` line (n % 2 == 0) executes 3 times (once per call);
    # both the `if` body and the `else` body are taken across the 3 calls.
    branches = _r.get("branches") or {}
    if_line = str(_BRANCH_PROGRAM.splitlines().index("    if n % 2 == 0:") + 1)
    check(f"{_name}: the if-line's branch count is 3 (once per classify() call)",
          branches.get(if_line) == 3, f"-> branches={branches}")

    # lines_never_executed: every statement in this program DOES execute (the
    # else branch fires for classify(1)==1, odd) — so this must be EMPTY, not
    # merely absent-from-the-dict — proving the field is a real computation,
    # not a placeholder that always reports "nothing" or "everything".
    check(f"{_name}: every line in this program executed at least once "
          f"(lines_never_executed is empty, not just present)",
          _r.get("lines_never_executed") == [], f"-> {_r.get('lines_never_executed')}")


# A SECOND program, where a whole branch genuinely never runs — proves
# lines_never_executed is not hardcoded empty.
_UNTAKEN_ELSE = (
    "def only_even(n):\n"
    "    if n % 2 == 0:\n"
    "        return True\n"
    "    else:\n"
    "        return False\n"
    "\n"
    "print(only_even(2), only_even(4))\n"
)
_r_untaken = tracing.execute_trace("python3", _UNTAKEN_ELSE)
_else_line = _UNTAKEN_ELSE.splitlines().index("        return False") + 1
check("the untaken else-branch line is reported in lines_never_executed",
      _else_line in (_r_untaken.get("lines_never_executed") or []),
      f"-> lines_never_executed={_r_untaken.get('lines_never_executed')}")
_if_line2 = _UNTAKEN_ELSE.splitlines().index("    if n % 2 == 0:") + 1
check("the always-taken if-branch line shows a nonzero, not a missing, count",
      (_r_untaken.get("branches") or {}).get(str(_if_line2)) == 2,
      f"-> branches={_r_untaken.get('branches')}")


# ── exception path ───────────────────────────────────────────────────────────
_EXC_PROGRAM = (
    "def inner():\n"
    "    raise ValueError(\"boom\")\n"
    "\n"
    "def outer():\n"
    "    inner()\n"
    "\n"
    "print(\"before\")\n"
    "outer()\n"
)
for _name, _force_fallback in BACKENDS:
    _saved = executor._rust
    if _force_fallback:
        executor._rust = None
    try:
        _r = tracing.execute_trace("python3", _EXC_PROGRAM)
    finally:
        executor._rust = _saved

    check(f"{_name}: an uncaught exception exits 1, verdict RTE",
          _r.get("exit_code") == 1 and _r.get("verdict") == "RTE",
          f"-> exit_code={_r.get('exit_code')} verdict={_r.get('verdict')}")
    check(f"{_name}: stdout printed before the raise is preserved",
          _r.get("stdout") == "before\n", f"-> {_r.get('stdout')!r}")
    check(f"{_name}: stderr carries a real traceback naming ValueError: boom",
          "ValueError: boom" in (_r.get("stderr") or "")
          and "Traceback (most recent call last):" in (_r.get("stderr") or ""),
          f"-> {_r.get('stderr')!r}")
    exc_events = [e for e in (_r.get("events") or []) if e.get("event") == "exception"]
    check(f"{_name}: an exception event is present, naming ValueError",
          bool(exc_events) and exc_events[0].get("exception_type") == "ValueError"
          and "boom" in exc_events[0].get("exception_message", ""),
          f"-> {exc_events[:1]}")
    # The interpreter's own exception-unwind quirk (a spurious 'return'
    # event with arg=None for every frame the exception passes through) must
    # not leak into `events` as if the function had genuinely returned None —
    # see codecalc/tracing.py's `_unwinding` tracking.
    spurious = [e for e in (_r.get("events") or [])
                if e.get("event") == "return" and e.get("func") in ("inner", "outer")]
    check(f"{_name}: no spurious return event for a frame an exception unwound "
          f"through (inner/outer never actually returned)",
          spurious == [], f"-> {spurious}")


# ── cap hit: the PROGRAM completes, only the TRACE is cut short ─────────────
_BIG_LOOP = "total = 0\nfor i in range(100000):\n    total += i\nprint(total)\n"
for _name, _force_fallback in BACKENDS:
    _saved = executor._rust
    if _force_fallback:
        executor._rust = None
    try:
        _r = tracing.execute_trace("python3", _BIG_LOOP, max_events=500)
    finally:
        executor._rust = _saved

    check(f"{_name}: the capped run still completes normally",
          _r.get("exit_code") == 0 and _r.get("verdict") == "OK",
          f"-> exit_code={_r.get('exit_code')} verdict={_r.get('verdict')}")
    check(f"{_name}: stdout is the FULL, uncut result (4999950000), "
          f"not truncated at the event cap",
          _r.get("stdout") == "4999950000\n", f"-> {_r.get('stdout')!r}")
    check(f"{_name}: truncated is True, reason is max_events",
          _r.get("truncated") is True and _r.get("truncated_reason") == "max_events",
          f"-> truncated={_r.get('truncated')} reason={_r.get('truncated_reason')}")
    check(f"{_name}: event_count and steps_before_truncation both stop at "
          f"exactly max_events (500), not beyond it",
          _r.get("event_count") == 500 == _r.get("steps_before_truncation"),
          f"-> event_count={_r.get('event_count')} "
          f"steps_before_truncation={_r.get('steps_before_truncation')}")
    check(f"{_name}: events itself holds exactly the recorded count, "
          f"no truncation-marker line leaked into it",
          len(_r.get("events") or []) == 500, f"-> {len(_r.get('events') or [])}")

# A program whose real trace is SMALLER than max_events must never be
# misreported as truncated — the marker-line design (codecalc/tracing.py's
# `_TRUNCATION_MARKER_KEY`) exists specifically so a trace that happens to
# have exactly N events without hitting either cap is not misread as capped.
_small = tracing.execute_trace("python3", 'print("hi")\n', max_events=2000)
check("a small, uncapped trace reports truncated=False",
      _small.get("truncated") is False and "truncated_reason" not in _small,
      f"-> truncated={_small.get('truncated')} reason={_small.get('truncated_reason')}")


# ── stdin passthrough ────────────────────────────────────────────────────────
_STDIN_PROGRAM = "x = input()\nprint(\"got\", x)\n"
for _name, _force_fallback in BACKENDS:
    _saved = executor._rust
    if _force_fallback:
        executor._rust = None
    try:
        _r = tracing.execute_trace("python3", _STDIN_PROGRAM, stdin="hello\n")
    finally:
        executor._rust = _saved
    check(f"{_name}: stdin reaches the traced program the same way it "
          f"reaches execute_code", _r.get("stdout") == "got hello\n",
          f"-> {_r.get('stdout')!r}")


# ── behaviour parity vs execute_code, 3 programs, both backends ─────────────
# stderr is compared with the source-file PATH stripped, not verbatim: the
# trace harness stages the user's source at a DIFFERENT path than
# execute_code's own `.codecalc-run/main.py` (see tracing.py's module
# docstring, step 3) — a deliberate, documented difference in WHERE the code
# runs from, never in what it prints, exits with, or is classified as.
def _strip_paths(text: str) -> list[str]:
    return [line.split("line ", 1)[-1] if ", line " in line else line
            for line in (text or "").splitlines()
            if not line.lstrip().startswith('File "')]


PARITY_PROGRAMS = [
    'print("hi")\nfor i in range(5):\n    print(i * i)\n',
    "import sys\nprint(\"err\", file=sys.stderr)\nsys.exit(7)\n",
    "x = 1 / 0\n",
]
for _name, _force_fallback in BACKENDS:
    _saved = executor._rust
    if _force_fallback:
        executor._rust = None
    try:
        for _prog in PARITY_PROGRAMS:
            _exec_r = executor.execute("python3", _prog)
            _trace_r = tracing.execute_trace("python3", _prog)
            check(f"{_name}: trace_execution's stdout matches execute_code's, "
                  f"byte for byte, for {_prog[:24]!r}",
                  _exec_r.get("stdout") == _trace_r.get("stdout"),
                  f"-> exec={_exec_r.get('stdout')!r} trace={_trace_r.get('stdout')!r}")
            check(f"{_name}: exit_code and verdict match for {_prog[:24]!r}",
                  _exec_r.get("exit_code") == _trace_r.get("exit_code")
                  and _exec_r.get("verdict") == _trace_r.get("verdict"),
                  f"-> exec=({_exec_r.get('exit_code')},{_exec_r.get('verdict')}) "
                  f"trace=({_trace_r.get('exit_code')},{_trace_r.get('verdict')})")
            check(f"{_name}: stderr matches modulo the harness's own source "
                  f"file path for {_prog[:24]!r}",
                  _strip_paths(_exec_r.get("stderr")) == _strip_paths(_trace_r.get("stderr")),
                  f"-> exec={_exec_r.get('stderr')!r} trace={_trace_r.get('stderr')!r}")
    finally:
        executor._rust = _saved


# ── non-python refusal: nothing spawned ─────────────────────────────────────
# Mocked rather than measured by absence-of-side-effect: `executor.execute` is
# monkeypatched to raise if called at all, so a refusal that accidentally fell
# through to a real spawn would fail LOUDLY here instead of merely "happening
# not to be observed" by a process-count check.
_spawned = {"called": False}
_real_execute = executor.execute


def _tripwire_execute(*args, **kwargs):
    _spawned["called"] = True
    return _real_execute(*args, **kwargs)


executor.execute = _tripwire_execute
try:
    _refused = tracing.execute_trace("ruby", "puts 1")
finally:
    executor.execute = _real_execute

check("a non-python language is refused with the validation code",
      _refused.get("ok") is False and _refused.get("code") == "validation",
      f"-> {_refused}")
check("...naming execute_code as the remedy",
      "execute_code" in (_refused.get("remedy") or ""), f"-> {_refused.get('remedy')}")
check("...and 'verdict' is absent (nothing ran)",
      "verdict" not in _refused, f"-> {_refused.get('verdict')}")
check("no process was spawned for the refused request",
      _spawned["called"] is False)

# An alias resolves the same way execute_code's own alias table does — this
# tool is pickier about LANGUAGE, not about spelling.
_alias = tracing.execute_trace("python", "print(1)")
check("a python3 alias ('python') is accepted, not refused",
      _alias.get("ok") is not False or _alias.get("verdict") is not None,
      f"-> {_alias}")


# ── SyntaxError parity ───────────────────────────────────────────────────────
_BROKEN = "def broken(\n    pass\n"
for _name, _force_fallback in BACKENDS:
    _saved = executor._rust
    if _force_fallback:
        executor._rust = None
    try:
        _exec_r = executor.execute("python3", _BROKEN)
        _trace_r = tracing.execute_trace("python3", _BROKEN)
    finally:
        executor._rust = _saved
    check(f"{_name}: a SyntaxError's exit_code/verdict match execute_code",
          _exec_r.get("exit_code") == _trace_r.get("exit_code") == 1
          and _exec_r.get("verdict") == _trace_r.get("verdict") == "RTE",
          f"-> exec=({_exec_r.get('exit_code')},{_exec_r.get('verdict')}) "
          f"trace=({_trace_r.get('exit_code')},{_trace_r.get('verdict')})")
    check(f"{_name}: neither stderr carries a 'Traceback' header "
          f"(CPython's own uncaught-SyntaxError convention)",
          "Traceback" not in (_exec_r.get("stderr") or "")
          and "Traceback" not in (_trace_r.get("stderr") or ""),
          f"-> exec={_exec_r.get('stderr')!r} trace={_trace_r.get('stderr')!r}")
    check(f"{_name}: both name the same SyntaxError message",
          "SyntaxError: '(' was never closed" in (_exec_r.get("stderr") or "")
          and "SyntaxError: '(' was never closed" in (_trace_r.get("stderr") or ""),
          f"-> exec={_exec_r.get('stderr')!r} trace={_trace_r.get('stderr')!r}")
    check(f"{_name}: a compile error traces nothing (empty events/branches)",
          _trace_r.get("events") == [] and _trace_r.get("branches") == {},
          f"-> events={_trace_r.get('events')} branches={_trace_r.get('branches')}")


# ── timeout: verdict TLE, partial events, never a fabricated truncated=True ──
_saved_t = executor._rust
try:
    _started = time.monotonic()
    _tle = tracing.execute_trace(
        "python3",
        "import time\ntotal = 0\nwhile True:\n    total += 1\n    time.sleep(0.01)\n",
        timeout=2, max_events=100_000,
    )
    _elapsed = time.monotonic() - _started
finally:
    executor._rust = _saved_t

check("a wall-clock timeout is classified TLE",
      _tle.get("verdict") == "TLE", f"-> {_tle.get('verdict')}")
check("the call itself did not hang past the requested timeout "
      "(bounded wait, not an accidental infinite block)",
      _elapsed < 30, f"-> {_elapsed:.1f}s")
check("a hard kill before either recording cap trips reports truncated=False "
      "— this is scoped to trace_execution's OWN caps, not to a sandbox kill "
      "(verdict/timed_out already carry that story)",
      _tle.get("truncated") is False, f"-> {_tle.get('truncated')}")
check("SOME partial trace survived the kill (each event is flushed as it "
      "is written)",
      len(_tle.get("events") or []) > 0, f"-> {len(_tle.get('events') or [])}")


# ── no leaked codecalc-exec ──────────────────────────────────────────────────
_before = _codecalc_exec_pids()
if _before is None:
    skip("no leaked codecalc-exec process after a run",
         "no /proc on this platform (Windows/macOS) — see _codecalc_exec_pids")
else:
    tracing.execute_trace("python3", 'print("ok")\n')
    tracing.execute_trace("python3", "while True:\n    pass\n", timeout=1)
    time.sleep(0.5)  # let a killed child finish exiting
    _after = _codecalc_exec_pids()
    check("no codecalc-exec process is left behind by trace_execution "
          "(normal run or a timeout kill)",
          not (_after - _before), f"-> leaked pids: {_after - _before}")


# ── TRUST BOUNDARY: trace sink forgery / server-side amplification ─────────
# Cross-vendor review, DO-NOT-MERGE #1. The traced program can derive its own
# trace sink path from `__file__` (a sibling of the workdir the envelope
# already discloses) and write to it directly — reproduced by the review with
# a forged trailing `return` event kept last via `os._exit(0)` to skip the
# harness's own cleanup entirely. These assert the load-bearing mitigations:
# a bounded parser read, per-event schema validation, and the harness's own
# `end` marker as a consistency check `os._exit` cannot fake.

# (a) 5 MB of junk appended to the trace path AFTER a normal, complete run —
# the parser must stay bounded (fast) regardless of how much extra data sits
# on disk, and must disclose that the file was bigger than the harness could
# legitimately have written.
_JUNK_PROGRAM = (
    "import atexit\n"
    "import os\n"
    "trace_path = os.path.join(os.path.dirname(__file__), '.codecalc-run', "
    "'trace_events.jsonl')\n"
    "def _append_junk():\n"
    "    with open(trace_path, 'a') as f:\n"
    "        f.write('x' * (5 * 1024 * 1024))\n"
    "atexit.register(_append_junk)\n"
    "print('done')\n"
)
_junk_started = time.monotonic()
_junk_result = tracing.execute_trace("python3", _JUNK_PROGRAM)
_junk_elapsed = time.monotonic() - _junk_started
check("5 MB appended to the trace sink: the parser stays bounded (fast), "
      "not proportional to the file size on disk",
      _junk_elapsed < 10, f"-> {_junk_elapsed:.2f}s")
check("...truncated_reason is trace_file_exceeded",
      _junk_result.get("truncated") is True
      and _junk_result.get("truncated_reason") == "trace_file_exceeded",
      f"-> truncated={_junk_result.get('truncated')} "
      f"reason={_junk_result.get('truncated_reason')}")
check("...the program's own real output is unaffected",
      _junk_result.get("stdout") == "done\n" and _junk_result.get("verdict") == "OK",
      f"-> stdout={_junk_result.get('stdout')!r} verdict={_junk_result.get('verdict')}")

# (b) A well-formed FORGED event, appended after disabling tracing (so
# nothing the harness itself writes afterward can clobber it) and skipping
# the harness's own `end`-line write via os._exit(0). This is the exact
# review reproduction. The forged event may pass per-line validation if it
# happens to guess the correct next `step` (as this one deliberately does),
# but `events_consistent` must be false because the harness's own trailing
# `end` marker — the one thing `os._exit` cannot fake — is missing.
_FORGE_PROGRAM = (
    "import sys, os, json\n"
    "sys.settrace(None)\n"
    "trace_path = os.path.join(os.path.dirname(__file__), '.codecalc-run', "
    "'trace_events.jsonl')\n"
    "with open(trace_path, 'a') as f:\n"
    "    f.write(json.dumps({'step': 4, 'line': 1, 'event': 'return', "
    "'func': 'forged', 'locals': {}, 'return_value': \"'PWNED'\"}) + chr(10))\n"
    "os._exit(0)\n"
)
_forge_result = tracing.execute_trace("python3", _FORGE_PROGRAM)
check("a well-formed forged trailing event is reported events_consistent=False "
      "(the harness's own 'end' line is missing — os._exit skipped it)",
      _forge_result.get("events_consistent") is False,
      f"-> events_consistent={_forge_result.get('events_consistent')} "
      f"events={_forge_result.get('events')}")
check("...the run itself still completed normally (the forgery does not "
      "crash or hang codecalc)",
      _forge_result.get("exit_code") == 0, f"-> {_forge_result.get('exit_code')}")

# (c) Malformed lines (not JSON, or JSON that isn't a dict) written after
# disabling tracing — must be silently discarded, counted, never raised.
_MALFORMED_PROGRAM = (
    "import sys, os\n"
    "sys.settrace(None)\n"
    "trace_path = os.path.join(os.path.dirname(__file__), '.codecalc-run', "
    "'trace_events.jsonl')\n"
    "with open(trace_path, 'a') as f:\n"
    "    f.write('not json at all\\n')\n"
    "    f.write('{unbalanced\\n')\n"
    "    f.write('null\\n')\n"
    "    f.write('42\\n')\n"
    "os._exit(0)\n"
)
_malformed_result = tracing.execute_trace("python3", _MALFORMED_PROGRAM)
check("malformed trace lines are discarded, counted, and never raise",
      _malformed_result.get("discarded_events", 0) > 0
      and _malformed_result.get("exit_code") == 0,
      f"-> discarded_events={_malformed_result.get('discarded_events')} "
      f"exit_code={_malformed_result.get('exit_code')}")
_clean_result = tracing.execute_trace("python3", 'print("clean")\n')
check("...a well-formed run with no tampering reports discarded_events=0 "
      "and events_consistent=True (the positive control for both checks "
      "above)",
      _clean_result.get("discarded_events") == 0
      and _clean_result.get("events_consistent") is True,
      f"-> discarded_events={_clean_result.get('discarded_events')} "
      f"events_consistent={_clean_result.get('events_consistent')}")


# ── THREADS: sys.settrace is per-thread ─────────────────────────────────────
# Cross-vendor review #2. A second thread's frames are invisible to this
# tracer regardless of filename; the harness must disclose that rather than
# silently returning an incomplete trace as if it were complete.
_THREAD_PROGRAM = (
    "import threading, time\n"
    "def worker():\n"
    "    time.sleep(0.05)\n"
    "t = threading.Thread(target=worker)\n"
    "t.start()\n"
    "t.join()\n"
    "print('done')\n"
)
_thread_result = tracing.execute_trace("python3", _THREAD_PROGRAM)
check("a program that starts a thread discloses the tracing limitation in "
      "unenforced",
      "threads: only the main thread is traced" in (_thread_result.get("unenforced") or []),
      f"-> unenforced={_thread_result.get('unenforced')}")
check("...and still runs to completion normally",
      _thread_result.get("stdout") == "done\n" and _thread_result.get("exit_code") == 0,
      f"-> stdout={_thread_result.get('stdout')!r} exit_code={_thread_result.get('exit_code')}")
_single_threaded = tracing.execute_trace("python3", 'print("solo")\n')
check("a single-threaded program does NOT get the threads disclosure "
      "(the check is real, not unconditional)",
      "threads: only the main thread is traced" not in (_single_threaded.get("unenforced") or []),
      f"-> unenforced={_single_threaded.get('unenforced')}")


# ── sys.modules['__main__'] must not leak the harness's own identity ───────
# Cross-vendor review #3.
_MODULES_PROGRAM = "import sys\nprint(sys.modules['__main__'].__file__ == __file__)\n"
_modules_trace = tracing.execute_trace("python3", _MODULES_PROGRAM)
_modules_exec = executor.execute("python3", _MODULES_PROGRAM)
check("trace_execution: sys.modules['__main__'].__file__ matches __file__ "
      "(the user's own source, not this harness's)",
      _modules_trace.get("stdout") == "True\n", f"-> {_modules_trace.get('stdout')!r}")
check("execute_code shows the identical invariant for the same program "
      "(parity of BEHAVIOUR, not of the literal temp path, which "
      "necessarily differs between the two mechanisms)",
      _modules_exec.get("stdout") == "True\n", f"-> {_modules_exec.get('stdout')!r}")


# ── Fallback-backend OLE exit_code race: Rust stays deterministic ──────────
# Cross-vendor review #4. The pure-Python fallback's own output-cap
# enforcement polls on a timer, racing the child's natural exit — confirmed
# PRE-EXISTING (plain execute_code on this backend already flips exit_code
# between 0 and a negative signal across repeated runs at sizes near the
# cap) and independently reproducible, not something a single run can pin.
# What IS pinned here: the Rust backend has no such race, so
# trace_execution's exit_code must match execute_code's on Rust, every
# time, and trace_execution must disclose the fallback's own limitation in
# unenforced whenever it actually hits an OLE verdict on that backend.
_OLE_PROGRAM = "for i in range(3000):\n    print('x' * 50)\n"
_saved_ole = executor._rust
try:
    _rust_mismatches = []
    for _ in range(8):
        a = executor.execute("python3", _OLE_PROGRAM, max_output_kb=1)
        b = tracing.execute_trace("python3", _OLE_PROGRAM, max_output_kb=1)
        if a.get("exit_code") != b.get("exit_code"):
            _rust_mismatches.append((a.get("exit_code"), b.get("exit_code")))
    check("rust: trace_execution's exit_code matches execute_code's on every "
          "OLE trial (the fallback's race does not exist on this backend)",
          not _rust_mismatches, f"-> mismatches={_rust_mismatches}")

    executor._rust = None
    _fallback_ole = None
    for _ in range(12):
        r = tracing.execute_trace("python3", _OLE_PROGRAM, max_output_kb=1)
        if r.get("verdict") == "OLE":
            _fallback_ole = r
            break
    check("python fallback: an OLE run was actually reached within the "
          "trial budget (an existence floor for the assertion below)",
          _fallback_ole is not None, "-> got verdict(s) up to the budget")
    if _fallback_ole is not None:
        check("python fallback: an OLE verdict discloses the fallback-only "
              "exit_code race in unenforced",
              "exit_code: fallback backend may differ on output-limit kills"
              in (_fallback_ole.get("unenforced") or []),
              f"-> unenforced={_fallback_ole.get('unenforced')}")
        check("...verdict and output_truncated still agree with execute_code "
              "regardless of the exit_code race",
              _fallback_ole.get("verdict") == "OLE"
              and _fallback_ole.get("output_truncated") is True,
              f"-> verdict={_fallback_ole.get('verdict')} "
              f"output_truncated={_fallback_ole.get('output_truncated')}")
finally:
    executor._rust = _saved_ole


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL TRACE_EXECUTION TESTS PASS ===")
for _f in FAILS:
    print(f"  {_f}")
sys.exit(1 if FAILS else 0)
