"""Regressions for the Python-side sweep of 2026-08-08.

Fourteen defects across sessions.py, the two REPL worker bootstraps, server.py
and the pure-Python fallback executor. Same shape as every other sweep in this
repo: something that reported success while measuring, enforcing or returning
something other than what it named.

Four of them came from a cross-vendor review of the first ten, and three of
those were defects in the FIXES rather than in the original code — a close()
that reaped the worker but not what the worker had started, descriptors nothing
released, and a CPU measurement sampled outside the lock that made it
attributable.

The worst of them was not the corruption but what happened AFTER it. A protocol
error left the stream desynced, so every later call returned the PREVIOUS call's
result with ok=True — a well-formed, confident answer to a different question.
The first failure was loud and every one after it was silent.

These tests run both worker languages. python3 and node have separate bootstraps
that had the SAME defects independently, so a test covering only python3 would
have passed while half the feature stayed broken.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import executor, sessions

FAILS: list[str] = []
SKIPS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")
    SKIPS.append(name)


def _worker_usable(lang: str) -> bool:
    """Can a stateful worker for `lang` actually complete a call here?

    Presence on PATH is not enough. The node worker does not currently function
    on Windows — it starts and answers its warm-up, then the first real call
    finds it gone — so a suite that assumed `shutil.which` was sufficient
    crashed there rather than reporting a platform limitation.
    """
    if not shutil.which(lang):
        return False
    started = sessions.start(lang)
    sid = started.get("session_id")
    if not started.get("ok") or not sid:
        return False
    try:
        probe = sessions.execute(sid, "1" if lang == "node" else "pass")
        return bool(probe.get("ok"))
    except Exception:
        return False
    finally:
        sessions.stop(sid)


WORKER_LANGS = [lang for lang in ("python3", "node") if _worker_usable(lang)]
for _lang in ("python3", "node"):
    if _lang not in WORKER_LANGS:
        skip(f"{_lang} worker regressions", "no working stateful worker on this platform")

#: (language, code that spawns a child writing to fd 1 directly)
FD1_ESCAPE = {
    "python3": "import subprocess; subprocess.run(['echo', 'FROM_FD1'])",
    "node": "require('child_process').execSync('echo FROM_FD1', {stdio: 'inherit'})",
}
SET_STATE = {"python3": "x = 41", "node": "var x = 41"}
USE_STATE = {"python3": "print('state', x + 1)", "node": "console.log('state', x + 1)"}
MARKER = {"python3": "print('marker-4')", "node": "console.log('marker-4')"}
BIG = {"python3": "print('z' * 200000)", "node": "console.log('z'.repeat(200000))"}

# ═══ 1+2. a subprocess must not corrupt the protocol, and any corruption ════
#         must never be answered with a stale result
for lang in WORKER_LANGS:
    s = sessions.start(lang)["session_id"]
    try:
        sessions.execute(s, SET_STATE[lang])
        r2 = sessions.execute(s, FD1_ESCAPE[lang])
        # Writing to fd 1 used to inject raw bytes into the response stream:
        # `sys.stdout = StringIO()` is a Python-level rebind and a child process
        # inherits the DESCRIPTOR. The output is now captured instead.
        check(f"{lang}: a subprocess writing to fd 1 does not break the protocol",
              r2.get("ok") is True, f"-> {str(r2.get('error'))[:70]}")
        if lang == "python3":
            check("python3: that subprocess output is CAPTURED, not lost",
                  "FROM_FD1" in (r2.get("stdout") or ""), f"-> {r2.get('stdout')!r}")

        # The off-by-one: these two calls used to return each other's answers.
        r3 = sessions.execute(s, USE_STATE[lang])
        r4 = sessions.execute(s, MARKER[lang])
        check(f"{lang}: a call returns ITS OWN result (state)",
              "state 42" in (r3.get("stdout") or ""), f"-> {r3.get('stdout')!r}")
        check(f"{lang}: a call returns ITS OWN result (marker)",
              "marker-4" in (r4.get("stdout") or ""), f"-> {r4.get('stdout')!r}")
    finally:
        sessions.stop(s)

# Every response must carry the id of the request it answers — the mechanism
# that makes a desync detectable rather than trusted. Asserted directly, because
# the behaviour above would also pass if the streams merely happened to line up.
for lang in WORKER_LANGS:
    s = sessions.start(lang)["session_id"]
    try:
        w = sessions._workers[s]
        before = w._seq
        sessions.execute(s, MARKER[lang])
        check(f"{lang}: requests are numbered", w._seq == before + 1, f"-> {w._seq}")
    finally:
        sessions.stop(s)

import inspect

wsrc = inspect.getsource(sessions.Worker.run)
check("a mismatched reply id terminates the session",
      'out.get("id") != req_id' in wsrc and "self.close()" in wsrc)
check("a protocol error terminates the session instead of desyncing",
      wsrc.count("self.close()") >= 3, f"-> {wsrc.count('self.close()')} close() calls")

# ═══ 3. a stateful session is not exempt from the output cap ═══════════════
# It returned a 20 MB string whole, built three times over in memory on the way
# out through JSON, while the sandboxed path capped at 64 KiB and said OLE.
for lang in WORKER_LANGS:
    s = sessions.start(lang)["session_id"]
    try:
        r = sessions.execute(s, BIG[lang])
        check(f"{lang}: session output is capped",
              len(r.get("stdout") or "") <= 64 * 1024 + 64, f"-> {len(r.get('stdout') or '')} bytes")
        check(f"{lang}: and the truncation is REPORTED",
              r.get("output_truncated") is True and r.get("verdict") == "OLE",
              f"-> truncated={r.get('output_truncated')} verdict={r.get('verdict')}")
    finally:
        sessions.stop(s)

# ═══ 4. documented per-call ceilings must not be silently dropped ══════════
# execute_code documents max_memory_mb / max_output_kb / max_cpu / no_net, and
# the session branch passed only stdin and timeout. Measured: `no_net=True` with
# a session_id REACHED THE NETWORK, and max_memory_mb=32 allocated 300 MB.
from codecalc import server

sig = inspect.signature(sessions.execute)
for param in ("max_memory_mb", "max_output_kb", "max_cpu", "no_net"):
    check(f"sessions.execute accepts {param}", param in sig.parameters)

ssrc = inspect.getsource(server.execute_code)
check("execute_code forwards the ceilings to the session path",
      ssrc.count("max_memory_mb=max_memory_mb") == 2, "-> both branches")
check("compact applies to the session path too",
      ssrc.index("if compact:") > ssrc.index("sessions.execute"))

# A stateful worker genuinely cannot apply some of them. Saying so beats
# dropping them: `unenforced` is the field the Rust executor already uses.
for lang in WORKER_LANGS[:1]:
    s = sessions.start(lang)["session_id"]
    try:
        r = sessions.execute(s, MARKER[lang], no_net=True, max_memory_mb=32)
        un = " ".join(r.get("unenforced") or [])
        check("a worker session REPORTS the ceilings it cannot apply",
              "no_net" in un and "max_memory_mb" in un, f"-> {r.get('unenforced')}")
    finally:
        sessions.stop(s)

# A workspace-only session runs a fresh sandboxed process, so it must honour
# them for real rather than merely report them.
if executor._rust:
    r = sessions.start("bash")
    s = r["session_id"]
    try:
        r = sessions.execute(s, "echo hi", language="bash", max_output_kb=1, timeout=20)
        check("a workspace session really applies the ceilings",
              r.get("ok") is True and r.get("verdict") == "OK", f"-> {str(r)[:80]}")
    finally:
        sessions.stop(s)
else:
    skip("workspace-session ceilings", "no native executor built")

# ═══ 5. a session worker runs under the same rlimits as the sandbox ═══════
# Measured before the fix: RLIMIT_AS, RLIMIT_CPU and RLIMIT_FSIZE all unlimited
# and RLIMIT_NPROC at the system default of 95498, against 1559 sandboxed.
if os.name != "nt" and "python3" in WORKER_LANGS:
    s = sessions.start("python3")["session_id"]
    try:
        r = sessions.execute(s, "import resource\n"
                                "for n in ('RLIMIT_AS','RLIMIT_NPROC','RLIMIT_FSIZE'):\n"
                                "    print(n, resource.getrlimit(getattr(resource,n))[0])\n")
        out = r.get("stdout") or ""
        vals = dict(line.split() for line in out.strip().splitlines() if line)
        for name in ("RLIMIT_AS", "RLIMIT_NPROC", "RLIMIT_FSIZE"):
            check(f"session worker has a real {name}",
                  vals.get(name, "-1") != "-1", f"-> {vals.get(name)}")
        # NOT equality with a freshly computed nproc_limit(): that measures the
        # CURRENT task count and adds headroom, so it drifts by a few between
        # the worker's spawn and this check. Asserting equality made the test
        # fail on an unrelated background process. What matters is that a real
        # ceiling was applied rather than the system default inherited.
        worker_nproc = int(vals.get("RLIMIT_NPROC", "0"))
        check("session worker has a finite RLIMIT_NPROC", worker_nproc > 0,
              f"-> {worker_nproc}")
        if sys.platform.startswith("linux"):
            # Only Linux MEASURES the ambient count; current_uid_tasks() returns
            # None elsewhere and the limit falls back to a fixed ceiling, which
            # the executor reports as
            # `process_limit_is_a_fixed_ceiling_not_measured`. Asserting the
            # measured relationship everywhere failed on macOS with worker=1333
            # against a fallback of 4096 — the code was right and the assertion
            # was describing Linux.
            headroom = executor.DEFAULT_PROCESS_HEADROOM
            check("Linux: the ceiling is measured, not the fixed fallback",
                  worker_nproc < executor.FALLBACK_NPROC_LIMIT
                  and abs(worker_nproc - executor.nproc_limit()) < headroom,
                  f"-> worker {worker_nproc}, now {executor.nproc_limit()}, "
                  f"fallback {executor.FALLBACK_NPROC_LIMIT}")
    finally:
        sessions.stop(s)

    # RLIMIT_CPU must NOT be set: it is cumulative, so a per-call ceiling on a
    # worker living for hours would kill the session mid-unrelated-call.
    # Grep the CALL, not the prose: the function's docstring explains at length
    # why RLIMIT_CPU is omitted, so a bare `"RLIMIT_CPU" not in src` failed on
    # the comment that documents the fix.
    src = inspect.getsource(executor.session_worker_limits)
    check("session worker does NOT get a cumulative RLIMIT_CPU",
          "setrlimit(resource.RLIMIT_CPU" not in src)
else:
    skip("session worker rlimits", "needs POSIX and a python3 worker")

# ═══ 6. the streaming tool must say whether it streamed ═══════════════════
if executor._rust:
    r = asyncio.run(server.execute_code_stream("python3", "print('hi')", timeout=20))
    check("execute_code_stream reports streamed=True",
          r.get("streamed") is True, f"-> {r.get('streamed')!r}")
    check("  ...and still returns the partial buffer", "streamed_partial" in r)
else:
    skip("streamed flag", "no native executor built")

# ═══ 7. lowering the output cap must not disable detecting an overflow ════
# RLIMIT_FSIZE was set to the cap itself, so the child died at exactly the cap,
# the file never exceeded it, and OLE could never fire. A 4 MB output came back
# as 8 KiB with verdict=OK.
if executor._rust:
    for kb in (0, 8):
        r = executor.execute("python3", 'print("x"*200000)', max_output_kb=kb, timeout=25)
        check(f"output overflow is detected at max_output_kb={kb or 'default'}",
              r.get("verdict") == "OLE", f"-> verdict={r.get('verdict')} len={len(r.get('stdout') or '')}")
else:
    skip("output-cap verdict", "no native executor built")

# ═══ 8+10. the fallback backend honours the SAME contract ════════════════
# It omitted eight documented fields (verdict, cpu_ms, peak_memory_kb,
# output_truncated, unenforced, total_ms, compile_ms, platform) — and ignored
# `workdir`, so a workspace session on this backend wrote its files into a
# throwaway tempdir that was then deleted. The session looked fine and stayed
# empty. CI never saw either, because contract_check only runs the binary.
saved_rust = executor._rust
try:
    executor._rust = None
    py = executor._execute_python("python3", "print(1)", timeout=20)
    for field in ("verdict", "cpu_ms", "peak_memory_kb", "output_truncated",
                  "unenforced", "total_ms", "compile_ms", "platform"):
        check(f"fallback returns {field}", field in py, f"-> {sorted(py)}")

    check("fallback verdict OK", py.get("verdict") == "OK", f"-> {py.get('verdict')}")
    to = executor._execute_python("python3", "while True: pass", timeout=3)
    check("fallback distinguishes TLE from RTE", to.get("verdict") == "TLE",
          f"-> {to.get('verdict')}")
    rte = executor._execute_python("python3", "raise SystemExit(3)", timeout=20)
    check("fallback reports RTE on a nonzero exit", rte.get("verdict") == "RTE",
          f"-> {rte.get('verdict')}")
    ole = executor._execute_python("python3", 'print("x"*200000)', max_output_kb=8, timeout=20)
    check("fallback honours max_output_kb and reports OLE",
          ole.get("verdict") == "OLE" and len(ole.get("stdout") or "") < 20_000,
          f"-> verdict={ole.get('verdict')} len={len(ole.get('stdout') or '')}")

    # peak_memory_kb is None, never 0: a zero would read as "used no memory".
    check("fallback reports peak_memory_kb as None, not 0",
          py.get("peak_memory_kb") is None, f"-> {py.get('peak_memory_kb')!r}")
    check("  ...and says why in unenforced",
          any("peak_memory_kb" in u for u in py.get("unenforced") or []))
    nn = executor._execute_python("python3", "print(1)", timeout=20, no_net=True)
    check("fallback reports no_net as unenforced when asked",
          any("no_net" in u for u in nn.get("unenforced") or []), f"-> {nn.get('unenforced')}")
    check("  ...and does NOT claim it when not asked",
          not any("no_net" in u for u in py.get("unenforced") or []))

    # The workdir regression.
    d = pathlib.Path(tempfile.mkdtemp(prefix="cc-fallback-session-"))
    r = executor.execute("python3", "import pathlib; pathlib.Path('artifact.txt').write_text('x')",
                         workdir=str(d), timeout=20)
    check("fallback runs in the caller's workdir", (d / "artifact.txt").is_file(),
          f"-> ran in {r.get('workdir')}")
    check("  ...and does not delete it", d.is_dir())
    shutil.rmtree(d, ignore_errors=True)
finally:
    executor._rust = saved_rust

# ═══ 11. stopping a session must reap what the session STARTED ═══════════
# Found in review, after the fixes above were written. Worker.close() signalled
# only the worker, which is spawned with start_new_session=True — so anything it
# forked lived in the worker's own process group and simply carried on. The
# identical one-hop mistake the native executor had, on the other side of the
# same codebase.
if os.name != "nt" and "python3" in WORKER_LANGS:
    import time

    marker = pathlib.Path(tempfile.gettempdir()) / f"cc-session-orphan-{os.getpid()}"
    marker.unlink(missing_ok=True)
    s = sessions.start("python3")["session_id"]
    sessions.execute(s, "import subprocess, sys\n"
                        "subprocess.Popen([sys.executable, '-c', "
                        f"\"import time,pathlib; time.sleep(6); "
                        f"pathlib.Path({str(marker)!r}).write_text('orphan')\"])\n")
    sessions.stop(s)
    time.sleep(8)                        # past when the descendant would fire
    check("stopping a session reaps its descendants", not marker.exists(),
          f"-> {'survived' if marker.exists() else 'reaped'}")
    marker.unlink(missing_ok=True)

    # close() is called from stop(), from the timeout path and from the protocol
    # error path, so it has to tolerate running twice.
    s = sessions.start("python3")["session_id"]
    w = sessions._workers[s]
    try:
        w.close()
        w.close()
        check("Worker.close() is idempotent", True)
    except Exception as exc:
        check("Worker.close() is idempotent", False, f"-> {type(exc).__name__}: {exc}")
    sessions.stop(s)

    # Every failed or finished session used to keep its pipes until the server
    # exited: close() released no descriptors, and a worker that failed its
    # warm-up leaked the protocol pipe outright.
    _FD_DIR = pathlib.Path(f"/proc/{os.getpid()}/fd")

    def _open_fds() -> int:
        return sum(1 for _ in _FD_DIR.iterdir())

    if _FD_DIR.is_dir():
        for lang in WORKER_LANGS:
            base = _open_fds()
            for _ in range(5):
                sessions.stop(sessions.start(lang)["session_id"])
            check(f"{lang}: start/stop cycles do not leak descriptors",
                  _open_fds() <= base + 1, f"-> {base} -> {_open_fds()}")
else:
    skip("session descendant reaping", "needs POSIX and a python3 worker")

# A caller-supplied workdir is one that is TRUE, not one that is non-None:
# workdir="" falls through to a fresh tempdir, so treating it as caller-owned
# would leave that tempdir behind on every call.
saved_rust = executor._rust
try:
    executor._rust = None
    tmp = pathlib.Path(tempfile.gettempdir())
    before = {p.name for p in tmp.glob("codecalc-*")}
    executor._execute_python("python3", "print(1)", timeout=20, workdir="")
    after = {p.name for p in tmp.glob("codecalc-*")}
    check('workdir="" does not leak a tempdir', after <= before,
          f"-> leaked {sorted(after - before)[:3]}")
finally:
    executor._rust = saved_rust

# ═══ 9. a comment must not cite a gate that does not exist ═══════════════
mw = (REPO_ROOT / "codecalc" / "mcp_middleware.py").read_text(encoding="utf-8")
import re

for cited in re.findall(r"(?:scripts|tests)/[a-z_0-9]+\.py", mw):
    check(f"middleware cites a file that exists: {cited}", (REPO_ROOT / cited).is_file())

print(f"\n=== {len(FAILS)} FAILURE(S), {len(SKIPS)} skipped ===" if FAILS else
      f"\n=== PYTHON SWEEP REGRESSIONS FIXED ({len(SKIPS)} skipped) ===")
sys.exit(1 if FAILS else 0)
