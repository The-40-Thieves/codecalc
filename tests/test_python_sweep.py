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
import io
import os
import pathlib
import shutil
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import executor, registry, sessions

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
        # PREVENTED on every platform now, not merely detected. POSIX hands the
        # node worker an out-of-band pipe; Windows has neither pass_fds nor
        # preexec_fn, so it gets a file the worker appends to instead — either
        # way fd 1 cannot reach the protocol. The echoed request id remains as
        # the backstop, but it is no longer what Windows depends on.
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

# The file-backed channel is what Windows uses, and CI cannot reach it from
# Linux or macOS. Forcing it here exercises the same code on every runner — an
# unexercised fallback is one that works until it is needed, which is precisely
# how the node worker came to be broken on Windows in the first place.
#
# python3 gets the identical exercise (issue #27): on Windows its usual
# dup'd-fd-1 protocol is not trustworthy either — a subprocess started by
# executed code can be handed the OS-level standard HANDLE instead of the
# redirected CRT descriptor the dup relies on — so it falls back to the same
# file-backed route as node. Before _worker_bootstrap.py understood
# CODECALC_PROTO_PATH, forcing this for python3 did not reproduce that fd-1
# escape (POSIX's fd dup is not the thing that is broken); it instead showed
# the worker had NO file-backed route at all: the warm-up call in
# `_spawn_worker` timed out waiting on a file nothing wrote to, closed the
# worker, and every later call failed with "python3 worker died". That is the
# gap this block now proves closed on Linux — what remains unverified here is
# the Windows-specific handle-vs-fd divergence itself, which cannot be staged
# on POSIX at all; only a Windows runner settles that part.
for _forced_lang in ("python3", "node"):
    if _forced_lang not in WORKER_LANGS:
        continue
    sessions._FORCE_FILE_PROTOCOL = True
    try:
        s = sessions.start(_forced_lang)["session_id"]
        try:
            sessions.execute(s, SET_STATE[_forced_lang])
            r = sessions.execute(s, FD1_ESCAPE[_forced_lang])
            check(f"file-backed protocol ({_forced_lang}): fd-1 writes cannot reach it",
                  r.get("ok") is True, f"-> {str(r.get('error'))[:70]}")
            if _forced_lang == "python3":
                check("file-backed protocol (python3): subprocess output is still CAPTURED",
                      "FROM_FD1" in (r.get("stdout") or ""), f"-> {r.get('stdout')!r}")
            r = sessions.execute(s, USE_STATE[_forced_lang])
            check(f"file-backed protocol ({_forced_lang}): state survives and answers match",
                  "state 42" in (r.get("stdout") or ""), f"-> {r.get('stdout')!r}")
        finally:
            sessions.stop(s)
        leaked = list(pathlib.Path(tempfile.gettempdir()).glob("codecalc-proto-*"))
        check(f"file-backed protocol ({_forced_lang}): the channel file is cleaned up",
              not leaked, f"-> {[p.name for p in leaked][:3]}")
    finally:
        sessions._FORCE_FILE_PROTOCOL = False

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

# ═══ 12. workdir deletion is identity-checked, not ownership-only (#38) ═══
# The Rust executor has always refused to delete a workdir whose (device,
# inode) no longer match what it recorded at creation — the executed program
# runs with that directory as its cwd, so it can rename another one into its
# place. The Python fallback enforced only ownership ("did WE create this
# path"), not identity, so the same rename-swap attack made it delete a
# directory it never made. Same repro as test_executor_sweep.py's "cleanup
# keys on WHAT the path is, not where it came from", against the fallback.
saved_rust = executor._rust
try:
    executor._rust = None
    victim = pathlib.Path(tempfile.mkdtemp(prefix="cc-py-victim-"))
    (victim / "important.txt").write_text("caller data")
    code = ("import os\n"
            "work = os.getcwd()\n"
            f"os.rename(work, work + '.held')\n"
            f"os.rename({str(victim)!r}, work)\n"
            "print('swapped')\n")
    r = executor._execute_python("python3", code, timeout=20)
    swapped_to = pathlib.Path(r.get("workdir", "/nonexistent"))
    # Whether the attack was actually STAGED, detected by the victim no longer
    # being at its original path. Not by stdout: the swap renames the directory
    # the output capture lives in, so "swapped" does not reliably come back
    # even when the rename succeeded. Not by `ok` either, which is True on a
    # successful swap AND on a platform that refused it.
    staged = not victim.exists()
    if not staged:
        # The PLATFORM refused to stage the attack: Windows will not rename a
        # directory that is a running process's current directory. The workdir
        # is therefore still the one this run created, and deleting it is
        # correct behaviour, not a breach.
        #
        # Asserting "the victim survived" here failed on windows-latest for a
        # reason that had nothing to do with the guard — the victim was never
        # moved anywhere. A regression test that cannot tell "the attack was
        # blocked" from "the attack never happened" is measuring something
        # other than what it names, which is the defect this file exists to
        # catch.
        skip("python fallback rename-swap",
             "the platform refused the rename, so the attack could not be "
             f"staged: ok={r.get('ok')} stderr={str(r.get('stderr'))[:70]!r}")
    else:
        check("a directory swapped in by rename is NOT deleted (python fallback)",
              (swapped_to / "important.txt").is_file(),
              f"-> {swapped_to} {'holds the data' if swapped_to.exists() else 'DELETED'}")
    for leftover in (swapped_to, pathlib.Path(str(swapped_to) + ".held"), victim):
        shutil.rmtree(leftover, ignore_errors=True)
finally:
    executor._rust = saved_rust

# The same attack against a SESSION workdir: sessions.stop() must refuse to
# delete it once the rename swap has happened, and must say so honestly via
# `deleted` rather than reporting deletion that did not occur.
if "bash" in registry.LANGUAGES:
    victim = pathlib.Path(tempfile.mkdtemp(prefix="cc-sess-victim-"))
    (victim / "important.txt").write_text("caller data")
    started = sessions.start("bash")
    sid = started["session_id"]
    script = ('work="$(pwd)"\n'
              'mv "$work" "$work.held"\n'
              f'mv {str(victim)!r} "$work"\n'
              'echo swapped\n')
    out = sessions.execute(sid, script, language="bash")
    stopped = sessions.stop(sid)
    d = pathlib.Path(started["workdir"])
    # Same distinction as the fallback case above: if the shell could not
    # perform the rename, the attack was never staged and stop() deleting its
    # own workdir is correct. Only assert the refusal where the swap actually
    # happened.
    if victim.exists():
        skip("session workdir rename-swap",
             "the platform refused the rename, so the victim is still at its "
             f"original path and the attack was never staged: ok={out.get('ok')}")
    else:
        check("session stop() does not delete a directory swapped in by rename",
              (d / "important.txt").is_file(),
              f"-> ran ok={out.get('ok')}, dir exists={d.exists()}")
        check("  ...and stop() reports deleted=False rather than lying",
              stopped.get("deleted") is False, f"-> {stopped}")
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(pathlib.Path(str(d) + ".held"), ignore_errors=True)
    shutil.rmtree(victim, ignore_errors=True)
else:
    skip("session workdir rename-swap regression", "bash not registered")

# ═══ 13. a missing runtime returns a result, not a raised exception (#26) ═══
# The fallback validated the language NAME but never checked that its
# interpreter/compiler executable actually existed before spawning it, and
# spawn-time OSError (FileNotFoundError for a missing binary, PermissionError
# for one that is not executable) was not caught — executor.execute("ruby",
# code) on a machine without ruby raised instead of returning {"ok": false}.
# Forced onto the fallback: the Rust binary reads its OWN compiled-in language
# table and would ignore this mutation of registry.LANGUAGES entirely, running
# the REAL ruby/gcc and masking the defect this test exists to catch.
saved_rust = executor._rust
saved_entry = dict(registry.LANGUAGES["ruby"])
try:
    executor._rust = None
    registry.LANGUAGES["ruby"] = {**saved_entry, "run": ["/nonexistent/bin/codecalc-no-ruby", "{file}"]}
    try:
        r = executor.execute("ruby", "puts 1")
        raised = None
    except Exception as exc:  # the exact defect under test: this must NOT raise
        r, raised = {}, exc
    check("a missing run-phase runtime returns a result instead of raising",
          raised is None, f"-> raised {raised!r}")
    check("  ...with ok False", r.get("ok") is False, f"-> {r.get('ok')}")
    check("  ...naming the phase", r.get("phase") == "run", f"-> {r.get('phase')}")
    check("  ...and a verdict", r.get("verdict") == "RTE", f"-> {r.get('verdict')}")
    check("  ...and the platform field other fallback results carry",
          r.get("platform") == sys.platform, f"-> {r.get('platform')}")
    check("  ...naming the missing executable",
          "codecalc-no-ruby" in (r.get("error") or ""), f"-> {r.get('error')!r}")
finally:
    registry.LANGUAGES["ruby"] = saved_entry
    executor._rust = saved_rust

saved_rust = executor._rust
saved_entry = dict(registry.LANGUAGES["c"])
try:
    executor._rust = None
    registry.LANGUAGES["c"] = {**saved_entry,
                              "compile": ["/nonexistent/bin/codecalc-no-gcc", "{file}", "-o", "{exe}"]}
    r = executor.execute("c", "int main(){return 0;}")
    check("a missing compile-phase runtime returns a result instead of raising",
          r.get("ok") is False and r.get("phase") == "compile",
          f"-> ok={r.get('ok')} phase={r.get('phase')}")
    check("  ...with a verdict too", r.get("verdict") == "RTE", f"-> {r.get('verdict')}")
finally:
    registry.LANGUAGES["c"] = saved_entry
    executor._rust = saved_rust

# ═══ 14. the output cap is enforced WHILE reading, not after (#25) ═════════
# communicate() read a child to completion before max_output_kb was applied,
# so a child writing far more than the cap made the PARENT allocate
# proportionally to total output before the result could be classified OLE.
# Measured before the fix: an 8 KiB cap against a 200 MiB write grew this
# process's RSS by ~400 MB. The fix drains both streams incrementally and
# kills the process group the moment either crosses the cap.
saved_rust = executor._rust
try:
    executor._rust = None
    if os.name != "nt":
        import resource as _resource
        before_kb = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
        r = executor._execute_python(
            "python3", "import sys\nsys.stdout.write('x' * (200 * 1024 * 1024))\n",
            max_output_kb=8, timeout=30)
        after_kb = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
        delta_kb = after_kb - before_kb
        check("a child writing 200MiB against an 8KiB cap is reported OLE",
              r.get("verdict") == "OLE", f"-> {r.get('verdict')}")
        check("  ...with stdout actually bounded near the cap",
              len(r.get("stdout") or "") < 20_000, f"-> {len(r.get('stdout') or '')} bytes")
        # A generous ceiling, not a tight one: this asserts the ENFORCEMENT
        # exists (bytes are not proportional to the 200MiB written), not a
        # specific allocator number, which is not stable across platforms.
        check("  ...and parent memory did not grow proportionally to total output",
              delta_kb < 50_000, f"-> RSS delta {delta_kb} KiB (200MiB write would show ~200000)")
    else:
        skip("bounded-output-cap memory regression", "ru_maxrss needs POSIX")

    # Both streams, independently — stderr must be bounded too, not just stdout.
    r = executor._execute_python(
        "python3",
        "import sys\nsys.stderr.write('e' * (5 * 1024 * 1024))\nsys.stdout.write('ok')\n",
        max_output_kb=4, timeout=30)
    check("stderr overflow is independently capped and reported OLE",
          r.get("verdict") == "OLE" and len(r.get("stderr") or "") < 10_000,
          f"-> verdict={r.get('verdict')} stderr_len={len(r.get('stderr') or '')}")

    # A child that stays under the cap must be completely unaffected.
    r = executor._execute_python("python3", "print('hi')", max_output_kb=64, timeout=20)
    check("output under the cap is unaffected", r.get("verdict") == "OK" and "hi" in r.get("stdout", ""),
          f"-> verdict={r.get('verdict')} stdout={r.get('stdout')!r}")
finally:
    executor._rust = saved_rust

# ═══ 9. a comment must not cite a gate that does not exist ═══════════════
mw = (REPO_ROOT / "codecalc" / "mcp_middleware.py").read_text(encoding="utf-8")
import re

for cited in re.findall(r"(?:scripts|tests)/[a-z_0-9]+\.py", mw):
    check(f"middleware cites a file that exists: {cited}", (REPO_ROOT / cited).is_file())

# ═══ 10. three defects found by cross-vendor review of the #25/#38 fixes ═══
# All three were introduced BY the fixes for #25 and #38, which is why they are
# grouped: a fix is not exempt from the defect class it was written to close.

# (a) _rmtree_checked returned True after rmtree(ignore_errors=True), which
#     swallows every removal failure. So sessions.stop() reported deleted:true
#     for a deletion that did not happen — the same "success it had not
#     earned" shape #38 existed to end.
_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
if os.name == "nt" or _IS_ROOT:
    # 0o000 does not stop removal for root, and Windows ignores it entirely, so
    # the undeletable directory cannot be staged. Named rather than silently
    # passed: not exercising the case is a different result from exercising it.
    skip("rmtree reports the real outcome",
         "root or Windows: a 0o000 directory is still removable here")
else:
    _d = pathlib.Path(tempfile.mkdtemp(prefix="cc-undeletable-"))
    _sub = _d / "sub"
    _sub.mkdir()
    (_sub / "f").write_text("x")
    _ident = executor._dir_identity(_d)
    _sub.chmod(0o000)
    try:
        _res = executor._rmtree_checked(_d, _ident)
        check("_rmtree_checked returns False when the directory is still there",
              _res is False and _d.exists(), f"-> returned {_res}, exists={_d.exists()}")
    finally:
        _sub.chmod(0o700)
        shutil.rmtree(_d, ignore_errors=True)
_d2 = pathlib.Path(tempfile.mkdtemp(prefix="cc-deletable-"))
check("_rmtree_checked still returns True on a real deletion",
      executor._rmtree_checked(_d2, executor._dir_identity(_d2)) is True and not _d2.exists())

# (b) the overflow signal was `len(chunk) > room`, which is false at exactly
#     cap+1 — the child was never killed at the boundary even though the
#     output had crossed the cap. Decided on total bytes seen now.
for _n, _cap, _want in ((5, 4, True), (4, 4, False), (6, 4, True), (1, 4, False)):
    _fired = []
    _dr = executor._BoundedDrain(io.BytesIO(b"z" * _n), _cap)
    _dr.drain(lambda f=_fired: f.append(1))
    check(f"drain: cap={_cap} bytes={_n} signals overflow={_want}",
          bool(_fired) is _want, f"-> fired={bool(_fired)} retained={len(_dr.data())}")

# (c) the COMPILE path called _trim() without the caller's cap, so it used the
#     64 KiB default while the drain had already bounded at the caller's cap.
#     Under-cap: truncated with NO marker (silent). Over-cap: falsely cut.
if "c" in registry.LANGUAGES and shutil.which("gcc"):
    _saved = executor._rust
    try:
        executor._rust = None
        _bad = "this is not valid c " * 200
        _small = executor._execute_python("c", _bad, timeout=60, max_output_kb=1)
        check("compile stderr truncated to the caller's cap IS marked truncated",
              "[truncated]" in _small.get("stderr", ""),
              f"-> {len(_small.get('stderr', ''))} bytes, marker="
              f"{'[truncated]' in _small.get('stderr', '')}")
        _big = executor._execute_python("c", _bad, timeout=60, max_output_kb=128)
        check("compile stderr under a LARGER cap is not falsely truncated",
              "[truncated]" not in _big.get("stderr", ""),
              f"-> {len(_big.get('stderr', ''))} bytes")
    finally:
        executor._rust = _saved
else:
    skip("compile-path output cap", "no c compiler on this machine")

print(f"\n=== {len(FAILS)} FAILURE(S), {len(SKIPS)} skipped ===" if FAILS else
      f"\n=== PYTHON SWEEP REGRESSIONS FIXED ({len(SKIPS)} skipped) ===")
sys.exit(1 if FAILS else 0)
