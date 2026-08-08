"""Regressions for the executor sweep (Rust + C + shell) of 2026-08-08.

Seven defects, all reproduced on this box before being fixed. They share the
shape that has run through this whole codebase: a guard, a limit or a number
that was measuring something other than what it named.

The shell-injection block is deliberately BOTH static and dynamic. The dynamic
half needs dotnet/gleam/nix and is skipped where they are absent — which is most
CI runners, so a dynamic-only test would be a gate that silently scans nothing.
The static half needs no toolchain and states the actual invariant, so it also
covers languages nobody has added yet.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import executor, registry

EXE = REPO_ROOT / "bin" / ("codecalc-exec.exe" if os.name == "nt" else "codecalc-exec")

FAILS: list[str] = []
SKIPS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")
    SKIPS.append(name)


def run_exec(code: str, lang: str = "python3", timeout: int = 30, **flags) -> dict:
    argv = [str(EXE), "--lang", lang, "--timeout", str(timeout)]
    for k, v in flags.items():
        argv += [f"--{k.replace('_', '-')}"] + ([str(v)] if v is not True else [])
    p = subprocess.run(argv, input=code, capture_output=True, text=True, timeout=timeout + 60)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"_unparseable": p.stdout[:400], "_stderr": p.stderr[:400]}


# ═══ 1. a failed source write must not delete the caller's workdir ══════════
# `--workdir` is a SESSION directory holding the user's files. When the source
# write failed the cleanup path removed it wholesale, so a sandboxed program
# could destroy the whole session workspace with `rm -f main.sh && mkdir main.sh`
# — the write then fails against a directory and the workspace goes with it.
if EXE.exists():
    work = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-sweep-"))
    (work / "precious.txt").write_text("user data")
    (work / "main.py").mkdir()          # forces the source write to fail
    r = run_exec("print(1)", workdir=str(work))
    check("failed source write leaves the caller's workdir intact",
          (work / "precious.txt").is_file(), f"-> ok={r.get('ok')} err={str(r.get('error'))[:60]}")
    check("  ...and it still reports the failure", r.get("ok") is False)
    shutil.rmtree(work, ignore_errors=True)

    # The executor's OWN temp dir must still be cleaned up when it made it.
    before = {p.name for p in pathlib.Path(tempfile.gettempdir()).glob("codecalc-*")}
    run_exec("print(1)")
    after = {p.name for p in pathlib.Path(tempfile.gettempdir()).glob("codecalc-*")}
    check("an executor-created workdir is still cleaned up", after <= before,
          f"-> leaked {sorted(after - before)[:3]}")
else:
    skip("workdir-deletion regressions", "bin/codecalc-exec not built")

# ═══ 2. the tempdir retry loop must terminate ═══════════════════════════════
# It retried on ANY error, so a permanent one (an unwritable TMPDIR) spun
# forever: the process hung until the outer timeout killed it at exit 124.
src = (REPO_ROOT / "executor" / "src" / "main.rs").read_text()
check("tempdir retry is bounded", "MAX_TEMPDIR_ATTEMPTS" in src)
check("tempdir retry only retries a name collision",
      "AlreadyExists" in src, "-> must not retry permission errors")

if EXE.exists() and os.name != "nt":
    bad = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-rotmp-"))
    bad.chmod(0o500)                      # readable, NOT writable
    env = {**os.environ, "TMPDIR": str(bad)}
    t0 = time.monotonic()
    try:
        p = subprocess.run([str(EXE), "--lang", "python3", "--timeout", "10"],
                           input="print(1)", capture_output=True, text=True,
                           env=env, timeout=25)
        hung = False
    except subprocess.TimeoutExpired:
        hung = True
        p = None
    bad.chmod(0o700)
    shutil.rmtree(bad, ignore_errors=True)
    check("an unwritable TMPDIR fails fast instead of hanging", not hung,
          f"-> {time.monotonic() - t0:.1f}s")
    if p is not None:
        check("  ...and says what went wrong", "tmp" in (p.stdout + p.stderr).lower(),
              f"-> {(p.stdout or p.stderr)[:80]}")
else:
    skip("unwritable-TMPDIR regression", "needs a POSIX host and a built binary")

# ═══ 3. killing the executor must not orphan the whole process tree ═════════
# PR_SET_PDEATHSIG only reaches the DIRECT child. Verified: killing the executor
# reaped `python3` and left its `sleep` grandchild reparented to init, running
# with no wall clock on it at all. The executor now kills the child's process
# GROUP on SIGTERM — it is the only participant that knows the pgid, because
# process_group(0) puts the child in a group the Python caller is not in.
unix = (REPO_ROOT / "executor" / "src" / "platform" / "unix.rs")
if unix.exists():
    u = unix.read_text()
    check("executor kills the child's process GROUP on termination", "killpg" in u)
    check("  ...and PDEATHSIG remains as the SIGKILL backstop", "PR_SET_PDEATHSIG" in u)

if EXE.exists() and os.name != "nt":
    marker = pathlib.Path(tempfile.gettempdir()) / f"codecalc-orphan-{os.getpid()}"
    marker.unlink(missing_ok=True)
    # The grandchild writes the marker only if it OUTLIVES the kill. Checking a
    # file the descendant creates beats grepping `ps` for its command line: an
    # earlier version of this test used `pgrep -f` and matched the very shell
    # that held the pattern in its own argv, reporting a permanent false alarm.
    prog = (f"import subprocess,sys\n"
            f"subprocess.run([sys.executable,'-c',"
            f"\"import time,pathlib;time.sleep(6);"
            f"pathlib.Path({str(marker)!r}).write_text('orphan survived')\"])\n")
    proc = executor._popen_group([str(EXE), "--lang", "python3", "--timeout", "90"])
    proc.stdin.write(prog.encode())
    proc.stdin.close()
    time.sleep(3)
    executor._kill_group(proc)
    proc.wait(timeout=30)
    time.sleep(6)                          # past when the grandchild would fire
    check("no descendant survives the executor being killed", not marker.exists(),
          f"-> {marker.read_text() if marker.exists() else 'reaped'}")
    marker.unlink(missing_ok=True)
else:
    skip("orphan-reaping regression", "needs a POSIX host and a built binary")

# ═══ 4/5. the timeout is a total budget, and duration is the RUN ════════════
# Compile and run each got the FULL timeout, so `--timeout 10` could take 20s.
# And duration_ms included compile time, which made every compiled language look
# far slower than it is: C reported duration_ms=126 with cpu_ms=0.
check("run timeout is the remainder of the budget",
      "saturating_sub" in src and "spent_secs" in src)
check("compile time is reported separately", '"compile_ms"' in src and '"total_ms"' in src)

if EXE.exists() and shutil.which("gcc"):
    r = run_exec('#include <stdio.h>\nint main(){printf("hi\\n");return 0;}', lang="c")
    for k in ("compile_ms", "duration_ms", "total_ms"):
        check(f"c run reports {k}", isinstance(r.get(k), int), f"-> {r.get(k)}")
    check("duration_ms EXCLUDES compile time",
          r.get("duration_ms", 0) < r.get("total_ms", 0),
          f"-> run={r.get('duration_ms')} compile={r.get('compile_ms')} total={r.get('total_ms')}")
    check("total_ms accounts for both phases",
          r.get("total_ms", 0) >= r.get("compile_ms", 0) + r.get("duration_ms", 0) - 5,
          f"-> {r.get('total_ms')} vs {r.get('compile_ms')}+{r.get('duration_ms')}")
else:
    skip("compile/run timing split", "needs gcc and a built binary")

# ═══ 6. --no-net must block the NETWORK, not all sockets ═══════════════════
# It refused socket() for every address family, so AF_UNIX — local IPC, used by
# ordinary libraries with no network involved — failed too. The block now keys
# on AF_INET/AF_INET6.
blocknet = (REPO_ROOT / "executor" / "blocknet.c")
if blocknet.exists():
    b = blocknet.read_text()
    check("blocknet keys on the address family", "AF_INET" in b and "AF_INET6" in b)
    check("blocknet lets non-network families through",
          "blocknet_is_network" in b, "-> needs an explicit family predicate")
    check("blocknet forwards the calls it allows",
          "RTLD_NEXT" in b, "-> otherwise an allowed socket() still fails")

if EXE.exists() and sys.platform.startswith("linux") and blocknet.exists():
    # The AF_INET half is wrapped whole: the shim refuses at socket(), not at
    # connect(), so a test that only guarded connect() dies on line 1 of its own
    # try block and reports nothing either way.
    r = run_exec("import socket\n"
                 "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                 "print('AF_UNIX ok'); s.close()\n"
                 "try:\n"
                 "    t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
                 "    t.settimeout(4); t.connect(('1.1.1.1', 80))\n"
                 "    print('EGRESS REACHED')\n"
                 "except OSError as e:\n"
                 "    print('EGRESS blocked', e.errno)\n",
                 no_net=True)
    out = r.get("stdout", "")
    check("--no-net allows AF_UNIX (local IPC)", "AF_UNIX ok" in out, f"-> {out[:80]!r}")
    check("--no-net still blocks network egress", "EGRESS blocked" in out, f"-> {out[:80]!r}")
    check("--no-net reports nothing unenforced when the shim is present",
          r.get("unenforced") == [], f"-> {r.get('unenforced')}")

# ═══ 8. a STALE shim enforces the old policy and nothing notices ═══════════
# A MISSING shim is already honest — the executor emits
# `no_net_requested_but_no_shim_available` in `unenforced`, verified. A stale
# one cannot be caught that way: the file exists, so every "is the shim there?"
# check says yes while the policy actually running is the previous build.
#
# This is not hypothetical. It fired during this sweep: blocknet.c was edited to
# stop blocking AF_UNIX, the source assertions above passed, and AF_UNIX went on
# failing because the .so beside the binary predated the edit. The shim is now
# built by executor/build.rs as part of `cargo build`, so it cannot lag its
# source; this asserts that rather than trusting it.
check("the shim is built by the build system, not by hand",
      (REPO_ROOT / "executor" / "build.rs").is_file())

shim = EXE.parent / ("blocknet.dylib" if sys.platform == "darwin" else "blocknet.so")
if shim.exists() and blocknet.exists():
    check("the shim beside the executable is not older than its source",
          shim.stat().st_mtime >= blocknet.stat().st_mtime,
          f"-> shim {time.strftime('%H:%M:%S', time.localtime(shim.stat().st_mtime))} "
          f"vs source {time.strftime('%H:%M:%S', time.localtime(blocknet.stat().st_mtime))}")
elif os.name != "nt":
    skip("shim staleness check", "no shim built next to the executable")

# ═══ 7. no path may be interpolated into a shell script ════════════════════
# Three languages wrap their toolchain in `bash -c`. The paths were pasted into
# the script TEXT, so a workdir containing a space broke the command — a normal
# Windows home directory, `C:\Users\John Smith` — and a workdir containing
# `$(...)` was EXECUTED: `/tmp/cc$(id>/tmp/cc-pwned)x` ran `id`.
#
# This assertion is the invariant, not the instance: any future language that
# pastes a placeholder into a `bash -c` script fails here, with no toolchain
# needed to catch it.
PLACEHOLDER = re.compile(r"\{(file|exe|work)\}")
for lang, entry in registry.LANGUAGES.items():
    for phase in ("compile", "run"):
        argv = entry.get(phase)
        if not argv:
            continue
        for i, arg in enumerate(argv):
            # The argument AFTER `-c`/`-e`/`--run` is a script a shell parses.
            if i and argv[i - 1] in ("-c", "-e", "--run", "-command"):
                check(f"{lang}.{phase}: no path pasted into the shell script",
                      not PLACEHOLDER.search(arg), f"-> {arg[:70]}")

# Where a script IS used, the paths have to arrive as positional parameters.
for lang in ("csharp", "gleam", "haskell"):
    argv = registry.LANGUAGES[lang]["run"]
    check(f"{lang}: paths passed as positional parameters",
          any(PLACEHOLDER.fullmatch(a) for a in argv), f"-> {argv[-3:]}")
    script = argv[argv.index("-c") + 1]
    check(f"{lang}: the script reads $1/$2/$3", "$1" in script, f"-> {script[:60]}")

# The Rust table has to say the same thing; it is the one that actually runs.
for lang in ("csharp", "gleam", "haskell"):
    m = re.search(rf'name: "{re.escape(lang)}".*?run: &\[(.*?)\],\n', src, re.S)
    check(f"rust {lang}: no path pasted into the shell script",
          m is not None and not PLACEHOLDER.search(m.group(1).split('", "codecalc"')[0]),
          f"-> {(m.group(1)[:70] if m else 'ENTRY NOT FOUND')}")

# ── dynamic: the two real failures, where the toolchain exists ──────────────
TOOLCHAIN = {"csharp": "dotnet", "gleam": "gleam"}
SNIPPET = {
    "csharp": 'class P { static void Main() { System.Console.WriteLine("ok"); } }',
    "gleam": 'import gleam/io\npub fn main() { io.println("ok") }\n',
}
for lang, tool in TOOLCHAIN.items():
    if not (EXE.exists() and shutil.which(tool) and os.name != "nt"):
        skip(f"{lang} space/injection regression", f"{tool} not installed")
        continue

    work = pathlib.Path(tempfile.mkdtemp(prefix="codecalc sweep "))   # NOTE the spaces
    r = run_exec(SNIPPET[lang], lang=lang, timeout=180, workdir=str(work))
    check(f"{lang}: a workdir containing spaces works",
          r.get("ok") is True and "ok" in r.get("stdout", ""),
          f"-> exit={r.get('exit_code')} err={str(r.get('stderr'))[:70]}")
    shutil.rmtree(work, ignore_errors=True)

    pwned = pathlib.Path(tempfile.gettempdir()) / f"codecalc-pwned-{os.getpid()}"
    pwned.unlink(missing_ok=True)
    hostile = pathlib.Path(tempfile.gettempdir()) / f"cc$(id>{pwned})x"
    hostile.mkdir(exist_ok=True)
    run_exec(SNIPPET[lang], lang=lang, timeout=180, workdir=str(hostile))
    check(f"{lang}: a workdir containing $(...) is NOT executed", not pwned.exists(),
          f"-> {pwned.read_text()[:60] if pwned.exists() else 'inert'}")
    pwned.unlink(missing_ok=True)
    shutil.rmtree(hostile, ignore_errors=True)

print(f"\n=== {len(FAILS)} FAILURE(S), {len(SKIPS)} skipped ===" if FAILS else
      f"\n=== EXECUTOR SWEEP REGRESSIONS FIXED ({len(SKIPS)} skipped) ===")
sys.exit(1 if FAILS else 0)
