"""Regressions for the executor sweep (Rust + C + shell) of 2026-08-08.

Ten defects, all reproduced on this box before being fixed. They share the shape
that has run through this whole codebase: a guard, a limit or a number that was
measuring something other than what it named.

Three of them came from a cross-vendor review of the first eight fixes, and two
of those were defects in the FIXES rather than in the original code — a signal
race the process-group kill opened, and a cleanup guard that keyed on where a
pathname came from instead of what it currently points at.

The shell-injection block is deliberately BOTH static and dynamic. The dynamic
half needs dotnet/gleam/nix and is skipped where they are absent — which is most
CI runners, so a dynamic-only test would be a gate that silently scans nothing.

The static half is stated over the ARGV and applied to BOTH language tables, so
it covers languages nobody has added yet. The first version did not: it keyed on
the argument following `-c`/`-e`/`--run` (so `bash -lc` walked past it) and named
three languages by hand on the Rust side. It asserted three instances while its
comment claimed a class. The Rust table is now parsed and required to be
identical to the Python one, with a floor on the parse: recovering fewer
languages than the registry defines is a failure, not a quiet skip.
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
    p = subprocess.run(argv, input=code, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout + 60)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"_unparsable": p.stdout[:400], "_stderr": p.stderr[:400]}


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

# ═══ 1b. the /proc walk must not truncate a status file ════════════════════
# Sizing RLIMIT_NPROC means reading /proc/<pid>/status for every process. An
# optimisation that read a single fixed 4096-byte buffer was wrong twice over,
# and cross-vendor review caught both: `read` may legally return fewer bytes
# than are available, and status can genuinely EXCEED 4 KiB, because the kernel
# emits the whole `Groups:` list before `Threads:` and Linux allows up to 65536
# supplementary groups.
#
# The consequence is silent and one-directional: a process whose `Threads:` fell
# outside the window counted as ONE task instead of its real thread count,
# under-sizing the fork-bomb ceiling. A truncated `Threads:\t12345` could even
# parse as 123, which is worse than missing.
unix_src = (REPO_ROOT / "executor" / "src" / "platform" / "unix.rs")
if unix_src.exists():
    u_src = unix_src.read_text(encoding="utf-8")
    check("the /proc walk reads to EOF, not into a fixed window",
          "read_to_end" in u_src and "vec![0u8; 4096]" not in u_src,
          "-> a fixed buffer silently under-counts a process with many groups")
    check("  ...and reuses one buffer rather than allocating per process",
          "Vec::with_capacity" in u_src and "buf.clear()" in u_src)

# The Python fallback walks /proc too, and read_text() is STRICT: a task Name:
# holding non-UTF-8 bytes raises UnicodeDecodeError, which is not an OSError, so
# one such process aborted the entire measurement and silently dropped the limit
# to the fixed fallback. The two backends had diverged on the same file.
exec_src = (REPO_ROOT / "codecalc" / "executor.py").read_text(encoding="utf-8")
check("the python /proc walk decodes leniently",
      'read_text(errors="replace")' in exec_src,
      "-> strict decoding lets one process abort the whole count")

# The two walks must agree. Comparing current_uid_tasks() to ITSELF would pass
# unconditionally — the first version of this check did exactly that. The Rust
# side's count is recoverable from the ceiling it applies: the child's
# RLIMIT_NPROC is ambient + headroom, so subtracting the headroom recovers what
# it measured, and that is what can be compared against the Python walk.
if EXE.exists() and pathlib.Path("/proc/self/status").is_file():
    r = run_exec("import resource; print(resource.getrlimit(resource.RLIMIT_NPROC)[0])")
    applied = int((r.get("stdout") or "0").strip() or 0)
    rust_ambient = applied - executor.DEFAULT_PROCESS_HEADROOM
    py_ambient = executor.current_uid_tasks() or 0
    check("both /proc walks measure the same machine",
          py_ambient > 0 and abs(rust_ambient - py_ambient) < 100,
          f"-> rust {rust_ambient}, python {py_ambient} "
          f"(a large gap means one backend skips processes the other counts)")

# ═══ 2. the tempdir retry loop must terminate ═══════════════════════════════
# It retried on ANY error, so a permanent one (an unwritable TMPDIR) spun
# forever: the process hung until the outer timeout killed it at exit 124.
src = (REPO_ROOT / "executor" / "src" / "main.rs").read_text(encoding="utf-8")
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
                           input="print(1)", capture_output=True, text=True, encoding="utf-8", errors="replace",
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
    u = unix.read_text(encoding="utf-8")
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
          f"-> {marker.read_text(encoding='utf-8') if marker.exists() else 'reaped'}")
    marker.unlink(missing_ok=True)
else:
    skip("orphan-reaping regression", "needs a POSIX host and a built binary")

# The handler is installed before spawn(), but the pgid cannot be published
# until spawn() returns a pid. A SIGTERM arriving in that window found pgid == 0,
# killed nothing and exited — leaving exactly the descendants this is meant to
# reap. Blocking the signals across spawn+publish leaves it PENDING instead.
if unix.exists():
    check("termination signals are blocked across spawn and pgid publication",
          "block_term_signals" in u and "pthread_sigmask" in u)
    check("the handler uses kill(), which POSIX lists as async-signal-safe",
          "kill(-pgid" in u, "-> killpg() is not on that list")
    check("the pgid is cleared on the wait4 error path too",
          u.count("CHILD_PGID.store(0") >= 2,
          f"-> {u.count('CHILD_PGID.store(0')} clearing stores; a stale pgid can be reused")

# The block must not reach the sandboxed program: the signal mask survives
# execve, so a child inheriting it would ignore the very signal used to stop it.
if EXE.exists() and os.name != "nt":
    r = run_exec("import signal\n"
                 "blocked = signal.pthread_sigmask(signal.SIG_BLOCK, [])\n"
                 "names = sorted(s.name for s in blocked)\n"
                 "print('BLOCKED', names)\n")
    out = r.get("stdout", "")
    check("the sandboxed child does NOT inherit a blocked SIGTERM",
          "SIGTERM" not in out and "BLOCKED" in out, f"-> {out.strip()[:90]}")

# ═══ 3b. cleanup keys on WHAT the path is, not where it came from ══════════
# The guard was "we created this path, so we may delete it" — a claim about the
# past that the executed program can invalidate, since it runs with the workdir
# as its cwd:
#     os.rename(work, work + ".held")            # move ours aside
#     os.rename("/tmp/victim", work)             # put someone else's there
# Cleanup then deleted a directory the executor never made. It now compares
# (device, inode) recorded at creation and refuses when they differ.
if EXE.exists() and os.name != "nt":
    victim = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-victim-"))
    (victim / "important.txt").write_text("caller data")
    r = run_exec("import os\n"
                 "work = os.getcwd()\n"
                 f"os.rename(work, work + '.held')\n"
                 f"os.rename({str(victim)!r}, work)\n"
                 "print('swapped')\n")
    swapped_to = pathlib.Path(r.get("workdir", "/nonexistent"))
    check("a directory swapped in by rename is NOT deleted",
          (swapped_to / "important.txt").is_file(),
          f"-> {swapped_to} {'holds the data' if swapped_to.exists() else 'DELETED'}")
    for leftover in (swapped_to, pathlib.Path(str(swapped_to) + ".held"), victim):
        shutil.rmtree(leftover, ignore_errors=True)

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
    b = blocknet.read_text(encoding="utf-8")
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

#: argv[0] values that make every later element shell SYNTAX rather than data.
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "cmd", "cmd.exe", "powershell", "pwsh"}


def shell_invariant(table: dict, label: str) -> None:
    """No path may reach a shell as anything but a whole argv element.

    Stated over the ARGV rather than over a list of known option spellings. An
    earlier version keyed on the argument following `-c`/`-e`/`--run`, which
    `bash -lc`, `bash -cSCRIPT`, or any other option form walks straight past —
    it protected three known instances while claiming to protect a class.

    An element that IS exactly a placeholder is fine: that is the safe form,
    where execve hands the path over as one argument with no parsing.
    """
    for lang, entry in table.items():
        for phase in ("compile", "run"):
            argv = entry.get(phase)
            if not argv or pathlib.PurePath(argv[0]).name not in SHELLS:
                continue
            for arg in argv:
                if PLACEHOLDER.fullmatch(arg):
                    continue
                check(f"{label} {lang}.{phase}: no path pasted into shell text",
                      not PLACEHOLDER.search(arg), f"-> {arg[:70]}")
            # Re-evaluation puts the argument back through a parser, which undoes
            # the whole point of passing it positionally.
            for arg in argv:
                check(f"{label} {lang}.{phase}: shell text does not eval",
                      not re.search(r"\beval\b", arg), f"-> {arg[:70]}")


shell_invariant(registry.LANGUAGES, "python")

# Where a script IS used, the paths have to arrive as positional parameters.
# Keyed on SHELL_WRAPPED rather than a hand-list: csharp left the wrapper set
# when .NET 10 made a shell unnecessary, and a hand-list here is how
# a test keeps asserting yesterday's registry.
for lang in sorted(registry.SHELL_WRAPPED):
    argv = registry.LANGUAGES[lang]["run"]
    check(f"{lang}: paths passed as positional parameters",
          any(PLACEHOLDER.fullmatch(a) for a in argv), f"-> {argv[-3:]}")
    script = argv[argv.index("-c") + 1]
    check(f"{lang}: the script reads $1/$2/$3", "$1" in script, f"-> {script[:60]}")


# ── the RUST table is the one that actually runs ───────────────────────────
# Rather than re-implement the invariant against Rust source with a regex — the
# previous attempt named three languages by hand and split on a literal
# `", "codecalc"`, so it was tied to today's formatting and blind to any new
# entry — parse the table and require it to be IDENTICAL to the Python one.
# Then the invariant above covers both, and divergence of any kind is caught.
#
# The parse has a FLOOR: it must recover exactly as many languages as the
# registry defines. A parser that stops understanding main.rs then fails loudly
# instead of quietly checking nothing, which is the failure mode that makes a
# gate worse than no gate.
def _rust_strings(block: str) -> list[str]:
    """String literals of a Rust `&[...]`, honouring backslash escapes."""
    out, i = [], 0
    while i < len(block):
        if block[i] != '"':
            i += 1
            continue
        i += 1
        buf = []
        while i < len(block) and block[i] != '"':
            if block[i] == "\\":
                buf.append(block[i + 1])
                i += 2
            else:
                buf.append(block[i])
                i += 1
        out.append("".join(buf))
        i += 1
    return out


def _rust_table(text: str) -> dict:
    """Parse every `Lang { ... }` entry by brace matching, not by line shape."""
    table: dict = {}
    for m in re.finditer(r"\bLang\s*\{", text):
        depth, j = 1, m.end()
        while j < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        body = text[m.end():j - 1]
        name = re.search(r'name:\s*"((?:[^"\\]|\\.)*)"', body)
        ext = re.search(r'ext:\s*"((?:[^"\\]|\\.)*)"', body)
        if not name or not ext:
            continue
        entry: dict = {"ext": ext.group(1), "compile": None, "run": None}
        for field in ("compile", "run"):
            a = re.search(rf"{field}:\s*(?:Some\()?&\[", body)
            if not a:
                continue
            depth, k = 1, a.end()
            while k < len(body) and depth:
                depth += {"[": 1, "]": -1}.get(body[k], 0)
                k += 1
            entry[field] = _rust_strings(body[a.end():k - 1])
        table[name.group(1)] = entry
    return table


rust_table = _rust_table(src)
check("the rust language table parses at all", len(rust_table) > 0, f"-> {len(rust_table)}")
check("rust table covers every registry language",
      set(rust_table) == set(registry.LANGUAGES),
      f"-> rust-only {sorted(set(rust_table) - set(registry.LANGUAGES))}, "
      f"python-only {sorted(set(registry.LANGUAGES) - set(rust_table))}")

# Same invariant, now against what the Rust binary really runs.
shell_invariant(rust_table, "rust")

for lang in sorted(set(rust_table) & set(registry.LANGUAGES)):
    py, rs = registry.LANGUAGES[lang], rust_table[lang]
    check(f"{lang}: rust and python agree on the run argv", py["run"] == rs["run"],
          f"-> rust {rs['run']}\n        python {py['run']}")
    check(f"{lang}: rust and python agree on the compile argv",
          (py["compile"] or None) == (rs["compile"] or None),
          f"-> rust {rs['compile']} vs python {py['compile']}")
    check(f"{lang}: rust and python agree on the extension",
          registry.EXTENSIONS[lang] == rs["ext"],
          f"-> rust {rs['ext']} vs python {registry.EXTENSIONS[lang]}")

# ── dynamic: the two real failures, where the toolchain exists ──────────────
# haskell is here because REASONING that printf %q survives nix-shell's second
# shell is not the same as watching it: that entry is the only one with two
# levels of evaluation, so it is the one most worth exercising rather than
# arguing about.
TOOLCHAIN = {"csharp": "dotnet", "gleam": "gleam", "haskell": "nix-shell"}
SNIPPET = {
    "csharp": 'class P { static void Main() { System.Console.WriteLine("ok"); } }',
    "gleam": 'import gleam/io\npub fn main() { io.println("ok") }\n',
    "haskell": 'main :: IO ()\nmain = putStrLn "ok"\n',
}
#: Toolchains that do first-run setup the first time they are invoked. dotnet
#: prints a "Welcome to .NET" banner and populates ~/.dotnet, and inside the
#: sandbox that work died at exit 134 on a cold CI runner. Warming it OUTSIDE
#: the sandbox first makes the assertion about what it claims to be about —
#: whether a path containing spaces works — rather than about first-run state.
WARMUP = {"csharp": ["dotnet", "--version"], "gleam": ["gleam", "--version"]}

for lang, tool in TOOLCHAIN.items():
    if not (EXE.exists() and shutil.which(tool) and os.name != "nt"):
        skip(f"{lang} space/injection regression", f"{tool} not installed")
        continue
    warm = WARMUP.get(lang)
    if warm:
        try:
            subprocess.run(warm, capture_output=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            skip(f"{lang} space/injection regression", f"{tool} would not warm up: {exc}")
            continue

    # BASELINE first. The assertion is "a path with spaces works", which only
    # means anything if the toolchain works at all in this sandbox. On a cold CI
    # runner dotnet died at exit 134 doing first-run setup — the executor's env
    # allowlist means it meets that state fresh however often it is warmed
    # outside — and the suite reported that as a spaces failure, which it was
    # not. Now a broken baseline is a SKIP that says so.
    plain = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-plain-"))
    base = run_exec(SNIPPET[lang], lang=lang, timeout=420, workdir=str(plain))
    shutil.rmtree(plain, ignore_errors=True)
    if not (base.get("ok") and "ok" in (base.get("stdout") or "")):
        skip(f"{lang} space/injection regression",
             f"{tool} does not run in the sandbox here: exit={base.get('exit_code')} "
             f"{str(base.get('stderr'))[:60]!r}")
        continue

    work = pathlib.Path(tempfile.mkdtemp(prefix="codecalc sweep "))   # NOTE the spaces
    r = run_exec(SNIPPET[lang], lang=lang, timeout=420, workdir=str(work))
    check(f"{lang}: a workdir containing spaces works",
          r.get("ok") is True and "ok" in r.get("stdout", ""),
          f"-> exit={r.get('exit_code')} err={str(r.get('stderr'))[:70]}")
    shutil.rmtree(work, ignore_errors=True)

    # The marker is named RELATIVE to the hostile directory, which is also the
    # cwd of anything the shell would run. Embedding the absolute path put
    # SLASHES inside the directory name, so `cc$(id>/tmp/x)y` was three nested
    # components rather than one directory — and it passed locally only because
    # earlier manual debugging had left those parents lying around. On a clean
    # runner it was FileNotFoundError, which is a test bug wearing the costume
    # of a platform difference.
    hostile = pathlib.Path(tempfile.mkdtemp(prefix="cc-hostile-")) / "cc$(id>pwned)x"
    hostile.mkdir(parents=True, exist_ok=True)
    pwned = hostile / "pwned"
    pwned.unlink(missing_ok=True)
    run_exec(SNIPPET[lang], lang=lang, timeout=420, workdir=str(hostile))
    check(f"{lang}: a workdir containing $(...) is NOT executed", not pwned.exists(),
          f"-> {pwned.read_text(encoding='utf-8')[:60] if pwned.exists() else 'inert'}")
    shutil.rmtree(hostile.parent, ignore_errors=True)

print(f"\n=== {len(FAILS)} FAILURE(S), {len(SKIPS)} skipped ===" if FAILS else
      f"\n=== EXECUTOR SWEEP REGRESSIONS FIXED ({len(SKIPS)} skipped) ===")
sys.exit(1 if FAILS else 0)
