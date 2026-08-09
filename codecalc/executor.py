"""Sandboxed multi-language executor.

Primary backend: the Rust `codecalc-exec` binary (memory-safe, no eval in the
host, rlimits + timeout + process-group kill in the sandbox). Falls back to a
pure-Python implementation when the binary isn't built yet.

Binary selection is architecture-aware so the right artifact is picked on both
arm64 and x86_64 hosts (including the static musl builds for older machines).

Set `CODECALC_REQUIRE_NATIVE=1` to turn that fallback into a startup failure
instead of a silent downgrade. The check runs once, at IMPORT time: this
module resolves the binary at module scope (`_rust = _rust_binary()` below),
so the earliest point a missing/unusable binary can be known is also the
earliest point to refuse — which, since `codecalc/server.py` imports this
module before calling `mcp.run()`, makes "at import" and "at server start"
the same moment for the MCP server, without a second check living in
server.py. A raised `RuntimeError` names the variable and what was missing
rather than leaving an operator to notice later that `no_net` was never
enforced.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import registry

#: POSIX-only stdlib. `resource` does not exist on Windows at all, so importing
#: it unconditionally made `import codecalc` fail there before any code ran.
IS_WINDOWS = os.name == "nt"
if not IS_WINDOWS:
    import resource
else:  # pragma: no cover - exercised on the Windows CI runner
    resource = None

MAX_OUTPUT_BYTES = 64 * 1024
AS_LIMIT_BYTES = 2048 * 1024**3      # 2 TiB VA — V8/JVM/tcmalloc reserve huge VA
FSIZE_LIMIT_BYTES = 256 * 1024**2    # 256 MiB output files
NFILE_LIMIT = 256
# ── fork-bomb guard ─────────────────────────────────────────────────────────
# RLIMIT_NPROC is not a per-sandbox limit: the kernel compares it against the
# real uid's TOTAL task count, machine-wide, and it counts THREADS. Any fixed
# constant is therefore a bet on how busy the rest of the box is, and this one
# lost — 1024 was picked from a process count of ~120 while the kernel was
# counting 1009 tasks, leaving ~15 threads of headroom and killing every
# runtime with a thread pool. Measured per execution instead; see
# executor/src/main.rs for the full note. scripts/check_parity.py gates that
# the two backends keep the same env vars and defaults.
DEFAULT_PROCESS_HEADROOM = 512
FALLBACK_NPROC_LIMIT = 4096
MAX_PROCESSES_ENV = "CODECALC_MAX_PROCESSES"
PROCESS_HEADROOM_ENV = "CODECALC_PROCESS_HEADROOM"
CPU_GRACE_SECONDS = 8

#: Fail-closed switch: refuse to start on the weaker Python fallback rather
#: than silently downgrade. See _require_native_or_die() below.
REQUIRE_NATIVE_ENV = "CODECALC_REQUIRE_NATIVE"


def _binary_candidates() -> list[str]:
    """Ordered candidate paths for the Rust executor, arch-aware."""
    machine = platform.machine().lower()
    exe_name = "codecalc-exec.exe" if os.name == "nt" else "codecalc-exec"
    # Two roots, package-local FIRST.
    #
    # An installed wheel puts the executor at `<site-packages>/codecalc/bin/`,
    # inside the namespace this distribution owns. It used to land at
    # `<site-packages>/bin/` — a top-level directory shared with every other
    # distribution in the environment, which is nobody's to own and which two
    # packages could each believe they installed. Playwright and Zig both ship
    # their bundled binaries inside their own package directory for the same
    # reason. See issue #59.
    #
    # A source checkout keeps built binaries at `<repo>/bin/`, which is where
    # README's build instructions, .gitignore and CI all put them, so that root
    # stays supported rather than being migrated in the same change. The two
    # never both exist in practice: a checkout has no `codecalc/bin/`, and an
    # installed package has no repo above it. Package-local is checked first so
    # that if they ever DID coexist, the installed artifact wins over whatever
    # happens to be lying in a working copy.
    pkg = Path(__file__).resolve().parent
    roots = [pkg / "bin", pkg.parent / "bin"]
    if machine in ("x86_64", "amd64"):
        names = [exe_name, "codecalc-exec-x86_64-musl"]
    elif machine in ("aarch64", "arm64"):
        names = [exe_name, "codecalc-exec-native-aarch64", "codecalc-exec-aarch64-musl"]
    else:
        names = [exe_name]
    return [str(root / n) for root in roots for n in names]


def _rust_binary() -> str | None:
    env = os.environ.get("CODECALC_EXEC_BIN")
    if env and Path(env).is_file() and os.access(env, os.X_OK):
        return env
    for cand in _binary_candidates():
        if Path(cand).is_file() and os.access(cand, os.X_OK):
            return cand
    return None


# ── Python fallback (used only when the Rust binary is absent) ─────────────
# preexec_fn (rlimits) is not thread-safe; serialize fallback spawns.
_FALLBACK_SPAWN_LOCK = threading.Lock()

#: env allowlist for executed code. SECURITY: user code must NEVER inherit API
#: keys/tokens from the host env — only what a runtime needs to function.
#: Environment variables executed code is allowed to see. Everything else is
#: dropped — the CRITICAL-02 fix against secret leakage.
#:
#: The Windows names are here because their absence made Windows a second-class
#: platform rather than a secured one: a process started without SystemRoot
#: fails inside winsock and crypto initialisation, and `node` returned empty
#: output with ok=false through the sandbox on Windows while probing as
#: available. These are OS plumbing, not credentials — SystemRoot and windir
#: locate the OS itself, COMSPEC and PATHEXT are how Windows resolves a command
#: at all, and USERPROFILE/APPDATA are the Windows spelling of HOME, which this
#: list has always allowed.
#:
#: The list is shared by both platforms rather than branched: a name that does
#: not exist in the environment is simply not copied, so the Windows entries are
#: inert on POSIX and scripts/check_parity.py can keep comparing the two
#: backends as one set.
_ENV_ALLOWLIST = {
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONUNBUFFERED",
    "JAVA_HOME", "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOMODCACHE",
    # Windows
    "SystemRoot", "SYSTEMROOT", "windir", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
}


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}
    env["PATH"] = registry.runtime_path()
    env["PYTHONUNBUFFERED"] = "1"
    return env


def current_uid_tasks() -> int | None:
    """Total tasks (THREADS, not processes) owned by this real uid, machine-wide
    — the number the kernel actually compares RLIMIT_NPROC against.

    Linux only. Returns None where it cannot be measured (Windows has no uid and
    no RLIMIT_NPROC; macOS has no cheap /proc equivalent), which the caller
    treats as "unknown" rather than zero.
    """
    proc = Path("/proc")
    if IS_WINDOWS or not proc.is_dir() or not hasattr(os, "getuid"):
        return None
    uid = os.getuid()
    total = 0
    seen_any = False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # errors="replace", not strict. A task Name: may hold bytes that
            # are not valid UTF-8, and read_text() raises UnicodeDecodeError —
            # which is NOT an OSError, so a single such process aborted the
            # whole measurement and dropped the count to the fixed fallback.
            # Found by cross-vendor review while checking why the Rust and
            # Python walks had diverged; the Rust side reads bytes and decodes
            # lossily, so it counted the process the Python side choked on.
            status = (entry / "status").read_text(errors="replace")
        except OSError:
            continue  # exited between listing and reading
        this_uid = threads = None
        for line in status.splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                this_uid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            elif line.startswith("Threads:"):
                val = line.split()[-1]
                threads = int(val) if val.isdigit() else None
            if this_uid is not None and threads is not None:
                break
        if this_uid == uid:
            seen_any = True
            total += threads if threads is not None else 1
    # This process is itself a task owned by this uid, so a zero total means the
    # walk found nothing usable.
    return total if seen_any else None


def nproc_limit() -> int:
    """RLIMIT_NPROC for one execution: measured ambient tasks + headroom.

    CODECALC_MAX_PROCESSES pins an absolute value and skips the measurement.
    """
    override = os.environ.get(MAX_PROCESSES_ENV)
    if override and override.isdigit():
        return int(override)
    headroom_env = os.environ.get(PROCESS_HEADROOM_ENV)
    headroom = int(headroom_env) if headroom_env and headroom_env.isdigit() \
        else DEFAULT_PROCESS_HEADROOM
    tasks = current_uid_tasks()
    return tasks + headroom if tasks is not None else FALLBACK_NPROC_LIMIT


def _limits(timeout: int, max_memory_mb: int = 0, max_cpu: int = 0):
    """preexec_fn applying rlimits, or None where there are no rlimits to apply.

    Windows has no setrlimit and no fork, so there is nothing to hook: the
    fallback executor there gets a wall-clock timeout and a process-tree kill
    and nothing else. The Rust executor is the one that carries real limits on
    Windows (a Job Object), which is why `backend()` matters more there.
    """
    if IS_WINDOWS:
        return None

    # Measured in the PARENT, before fork: _apply runs after fork in the child
    # via preexec_fn, where walking /proc is not something to be doing.
    nproc = nproc_limit()

    cpu = max_cpu if max_cpu > 0 else timeout + CPU_GRACE_SECONDS
    mem = max_memory_mb * 1024 * 1024 if max_memory_mb > 0 else AS_LIMIT_BYTES

    def _apply():
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu,) * 2)
            resource.setrlimit(resource.RLIMIT_AS, (mem,) * 2)
            resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE_LIMIT_BYTES,) * 2)
            resource.setrlimit(resource.RLIMIT_NOFILE, (NFILE_LIMIT, NFILE_LIMIT))
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):
            pass

    return _apply


def session_worker_limits():
    """preexec_fn for a LONG-LIVED session worker, or None on Windows.

    Deliberately not `_limits()`: RLIMIT_CPU is CUMULATIVE, so a per-call
    ceiling applied to a worker that lives for hours would kill the session
    partway through an unrelated call. Wall-clock is enforced per call by
    `Worker.run`'s read deadline instead, and the worker is killed if it blows
    it.

    Everything that IS meaningful for a long-lived process is applied. It was
    applying none of them: measured, a worker session ran with RLIMIT_AS,
    RLIMIT_CPU and RLIMIT_FSIZE all unlimited and RLIMIT_NPROC at the system
    default of 95498, against 1559 on the sandboxed path. Passing `session_id=`
    to the same tool dropped every resource ceiling it documents.
    """
    if IS_WINDOWS:
        return None

    nproc = nproc_limit()

    def _apply():
        try:
            resource.setrlimit(resource.RLIMIT_AS, (AS_LIMIT_BYTES,) * 2)
            resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE_LIMIT_BYTES,) * 2)
            resource.setrlimit(resource.RLIMIT_NOFILE, (NFILE_LIMIT, NFILE_LIMIT))
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):
            pass

    return _apply


def _popen_group(argv: list[str]) -> subprocess.Popen:
    """Spawn with the child at the head of its own process group/job.

    So a timeout can kill the child AND its descendants. Without this, killing
    the executor orphans every sandboxed process it started.
    """
    kwargs: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the child AND anything it spawned.

    Killing only the direct child orphans its tree, which is how a "timed out"
    run leaves work running on the host. There is no portable primitive for
    this: POSIX has process groups, Windows has `taskkill /T`.
    """
    # SIGTERM first: the executor catches it and kills its child's process
    # GROUP, which is the only way to reach grandchildren (the child is a group
    # leader in a different group from ours, so our killpg cannot see them).
    # SIGKILL follows as the backstop, and PR_SET_PDEATHSIG covers the child
    # itself if we never get to send anything.
    if not IS_WINDOWS:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=3)
                return
            except subprocess.TimeoutExpired:
                pass
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if IS_WINDOWS:
        # /T kills the tree, /F forces it. Spawned with CREATE_NEW_PROCESS_GROUP
        # so the child is the root of its own tree rather than ours.
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except Exception:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except Exception:
            pass


# ── identity-checked workdir deletion (mirrors executor/src/main.rs) ───────
#
# The Rust executor's cleanup guard used to key on how the pathname
# ORIGINATED — "we created it, so we may delete it". That is a claim about
# the past, and the executed program can invalidate it: it runs with the
# workdir as its cwd, so
#
#     os.rename(work, work + ".held")                 # move ours aside
#     os.rename("/tmp/something-i-want-gone", work)   # put a victim there
#
# leaves an unconditional rmtree(work) deleting a directory this process
# never made. Recording the identity at creation and re-checking it
# immediately before removal keys on what the path IS at the moment of the
# delete instead of on where it came from. This pair ports that guard to the
# three places the Python side deletes a workdir it created: the pure-Python
# fallback executor, execute_code_stream's --workdir, and session teardown.
def _dir_identity(path: str | Path) -> tuple[int, int] | None:
    """Identity (device, inode) of a directory, on platforms that have one.

    Attempted on EVERY platform, not just POSIX. The first version of this
    returned None on Windows outright, on the reasoning that Windows has no
    cheap stable (dev, ino) — and CI disproved it the only way that counts: the
    rename-swap regression ran on windows-latest and reported

        FAIL a directory swapped in by rename is NOT deleted (python fallback)
             -> C:\\Users\\...\\codecalc-r7oiykyq DELETED

    A guard that returns None on a platform, feeding a caller that treats None
    as "delete unconditionally", is not a recorded gap. It is the guard being
    inert exactly where nobody looks, while the README states the guarantee
    flatly.

    `os.stat` on Windows does populate st_ino (the NTFS file index) and st_dev
    (the volume serial number); the docs call st_ino "inode number or file
    index" for precisely this reason. Where a filesystem cannot supply an index
    it comes back as 0, which is not an identity and must not be compared as
    one — hence the explicit rejection below rather than trusting a zero.
    """
    try:
        # lstat, not stat: a symlink swapped into the path must not be
        # followed to the directory it points at.
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    if not st.st_ino:
        # 0 means the filesystem did not supply an index. Two different
        # directories would both compare equal on it, so returning it would
        # produce a check that always passes: the shape of defect this repo
        # keeps finding, in the one place where passing means deleting.
        return None
    return (st.st_dev, st.st_ino)


def _rmtree_checked(path: str | Path, created: tuple[int, int] | None) -> bool:
    """Delete a directory this process created, refusing if it is no longer
    the same directory that was created.

    Mirrors `remove_own_workdir` in executor/src/main.rs. `created` is the
    identity recorded at creation time.

    `created is None` means no identity could be obtained, and this REFUSES
    rather than deleting. That is the opposite of the first version, which
    deleted unconditionally in that case, and the reversal is deliberate: this
    is a delete path reached with executed code's cwd as its target, so
    "I could not verify" has to mean "I do not delete". Failing open here
    turns an unmeasurable filesystem into a silent loss of the guarantee the
    README states flatly.

    The cost is real and is stated rather than hidden: on a filesystem that
    supplies no file index, temp directories accumulate instead of being
    cleaned up. A visible leak is recoverable; deleting a directory someone
    renamed into place is not.

    Returns whether the directory was actually removed, so callers report the
    real outcome instead of an assumed one.
    """
    if created is None:
        print(f"codecalc: refusing to delete {path} — no directory identity "
              "was available on this filesystem, so ownership cannot be "
              "verified", file=sys.stderr)
        return False
    if _dir_identity(path) != created:
        print(f"codecalc: refusing to delete {path} — it is not the "
              "directory this run created", file=sys.stderr)
        return False
    shutil.rmtree(path, ignore_errors=True)
    # ignore_errors=True swallows EVERY removal failure, so `return True` after
    # it was a claim about what was attempted, not about what happened: an
    # unreadable descendant on POSIX, or an open handle on Windows, left the
    # directory in place while sessions.stop() reported `deleted: true`. That
    # is the same "reported success it had not earned" defect this function was
    # written to end, one layer down. Measured: returned True with the
    # directory still on disk. Ask the filesystem instead of assuming.
    # lexists, not exists: exists() FOLLOWS symlinks, so a dangling symlink
    # left at the path would report False — i.e. "successfully deleted" — for a
    # path that still has something on it. The whole point of this function is
    # that something else may have put a different object where our directory
    # was, so the absence check must not be the one that resolves it.
    removed = not os.path.lexists(path)
    if not removed:
        print(f"codecalc: could not fully delete {path}", file=sys.stderr)
    return removed


def _feed_stdin(proc: subprocess.Popen, data: bytes) -> None:
    """Write stdin and close it, in its own thread.

    So a large stdin payload can never block the caller: with the read side
    drained by _BoundedDrain threads running concurrently, the child is free
    to consume stdin while producing output, but nothing here assumes that —
    a write that blocks on a full pipe just blocks THIS thread, same as an
    unread stdout/stderr pipe blocking _BoundedDrain.drain would.
    """
    try:
        if data:
            proc.stdin.write(data)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass


class _BoundedDrain:
    """Reads one pipe, in a background thread, into a buffer capped at
    cap+1 bytes — never more, regardless of how much the child writes.

    This is the fix for #25: `communicate()` reads a pipe to EOF before
    anything looks at how much came back, so max_output_kb was a truncation
    applied after the fact, not a ceiling — a child writing far more than the
    cap made THIS process allocate proportionally to total output before the
    result could even be classified OLE. Reading incrementally and refusing
    to retain past the cap bounds that allocation to the cap itself, and
    `on_overflow()` (set once the cap is first crossed) is what lets the
    caller kill the child's process group promptly instead of waiting for it
    to finish writing on its own.

    +1, not `cap`: `_trim()` downstream decides whether to append the
    "[truncated]" marker by checking `len(bytes) > cap`; retaining one byte
    past the cap keeps that check meaningful without keeping anywhere near
    the full output in memory.

    Keeps reading — and discarding anything past the cap — until EOF
    regardless of overflow, so an unread pipe can never deadlock the child.
    This is the same hazard sessions.py's _drain_stderr/_drain_stdout guard
    against for the long-lived REPL workers; a bounded buffer that stopped
    reading at the cap would reintroduce it here.
    """

    def __init__(self, stream, cap: int):
        self._stream = stream
        self._cap = cap
        self._retain = cap + 1
        self._buf = bytearray()
        self._seen = 0
        self._signalled = False

    def drain(self, on_overflow) -> None:
        try:
            while True:
                chunk = self._stream.read(65536)
                if not chunk:
                    return
                self._seen += len(chunk)
                room = self._retain - len(self._buf)
                if room > 0:
                    self._buf.extend(chunk[:room])
                # Overflow is decided on TOTAL BYTES SEEN against the cap, not
                # on whether one chunk outran the remaining room. The latter
                # was off by one at exactly the boundary: with cap=4 the buffer
                # retains cap+1=5, so a single 5-byte chunk found room==5 and
                # `5 > 5` was false — measured, retained=5 with the overflow
                # never signalled. The output HAD crossed the cap, so `_trim`
                # marked it truncated downstream while the child was never
                # killed and ran on to the wall-clock timeout.
                if not self._signalled and self._seen > self._cap:
                    self._signalled = True
                    on_overflow()
        except (OSError, ValueError):
            pass

    def data(self) -> bytes:
        return bytes(self._buf)


def _run_step(argv: list[str], cwd: str, timeout: int, stdin: str,
              max_memory_mb: int = 0, max_cpu: int = 0,
              max_output_bytes: int = MAX_OUTPUT_BYTES) -> tuple[int, bytes, bytes, bool, int]:
    # NOTE: preexec_fn is unsafe under threads (PLW1509) — this is the PYTHON
    # FALLBACK path only, used when the Rust binary is absent. It serializes
    # spawns with a lock to avoid concurrent fork+preexec races; the production
    # path (Rust executor) never uses preexec_fn.
    with _FALLBACK_SPAWN_LOCK:
        # Both rusage samples are taken INSIDE the lock. Taking the first one at
        # the call site meant another fallback thread could reap its child in
        # the window before this one read the end value, charging its CPU to
        # this run. Still not exact — RUSAGE_CHILDREN is process-global, so a
        # child reaped elsewhere in the host process during this window is
        # counted here too — which is why the docstring says approximate rather
        # than claiming a per-process measurement this path cannot make.
        cpu_before = _children_cpu_seconds()
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_limits(timeout, max_memory_mb, max_cpu),
        )
        overflow = threading.Event()
        out_reader = _BoundedDrain(proc.stdout, max_output_bytes)
        err_reader = _BoundedDrain(proc.stderr, max_output_bytes)
        threads = [
            threading.Thread(target=_feed_stdin, args=(proc, stdin.encode()), daemon=True),
            threading.Thread(target=out_reader.drain, args=(overflow.set,), daemon=True),
            threading.Thread(target=err_reader.drain, args=(overflow.set,), daemon=True),
        ]
        for t in threads:
            t.start()

        # Polled rather than one blocking proc.wait(timeout=timeout): the kill
        # on overflow has to happen from THIS thread. Popen.wait() is not
        # something this codebase calls from two threads on the same process
        # at once, and a reader thread calling _kill_group the instant it sees
        # overflow would do exactly that against the wait() below.
        deadline = time.monotonic() + timeout
        timed_out = False
        while proc.poll() is None:
            if overflow.is_set():
                _kill_group(proc)
                proc.wait()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_group(proc)
                proc.wait()
                break
            time.sleep(0.02)

        for t in threads:
            t.join(timeout=5)
        cpu_ms = _children_cpu_ms_since(cpu_before)
        if timed_out:
            return (-1, b"", b"<killed: exceeded wall-clock timeout>", True, cpu_ms)
        return (proc.returncode, out_reader.data(), err_reader.data(), False, cpu_ms)


def _trim(b: bytes, cap: int = MAX_OUTPUT_BYTES) -> str:
    if len(b) > cap:
        return b[:cap].decode(errors="replace") + "\n…[truncated]"
    return b.decode(errors="replace")


def _fallback_verdict(rc: int, timed_out: bool, truncated: bool) -> str:
    """Same classification the Rust executor applies, in the same order.

    MLE is deliberately absent: the Rust path infers it from a signal plus an
    RSS reading near the cap, and this path has neither, so claiming it would be
    a guess. An OOM here lands in RTE, which is at least true.
    """
    if timed_out:
        return "TLE"
    if truncated:
        return "OLE"
    if rc != 0:
        return "RTE"
    return "OK"


#: Sandbox features the pure-Python fallback cannot provide, reported rather
#: than left for a caller to discover. `--no-net` needs the LD_PRELOAD shim the
#: Rust executor applies, and peak RSS needs per-child rusage this path has no
#: way to attribute.
_FALLBACK_UNMEASURED: list[str] = [
    "peak_memory_kb: ru_maxrss is a high-water mark and cannot be attributed to one run",
]

#: RLIMIT_NPROC does not bind a process whose EFFECTIVE uid is 0. The kernel
#: exempts privileged processes, so as root the ceiling is computed, set, and
#: has no effect — while the result said nothing about it, which reads as "the
#: process ceiling was applied".
#:
#: Running the server as root is a deployment error and the kernel behaviour is
#: documented, so this is not a sandbox escape. It is a reporting-fidelity
#: defect, and SECURITY.md puts "anything that makes the server report a
#: guarantee it did not apply" in scope. Reported rather than fixed, because
#: the fix for running as root is to not run as root.
_UID0_PROCESS_CEILING = (
    "max_processes: RLIMIT_NPROC does not bind a process running as uid 0"
)


#: Ceilings the fallback CANNOT apply on a given platform. `_limits()` has
#: always said so in its own docstring — "the fallback executor there gets a
#: wall-clock timeout and a process-tree kill and nothing else" — but the
#: RESULT never did, so a Windows caller asking for a 64MB ceiling got a
#: successful run that allocated 400MB and an empty `unenforced`. The code knew
#: and the output did not, which is the exact class SECURITY.md puts in scope.
#:
#: macOS is the subtler one: setrlimit(RLIMIT_AS) SUCCEEDS there and is then
#: ignored, so nothing raises and the ceiling looks applied. The Rust backend
#: has carried `memory_limit_not_enforced_on_macos` for this reason; the
#: fallback never did.
_NO_RLIMITS_AT_ALL = "the Python fallback applies no {} ceiling on Windows"
_MACOS_AS_IGNORED = (
    "max_memory_mb: setrlimit(RLIMIT_AS) is accepted and then ignored on macOS")


def _unmeasured(max_memory_mb: int = 0, max_cpu: int = 0) -> list[str]:
    """`unenforced` entries for the pure-Python fallback, for THIS process.

    Computed per call rather than at import: the euid check is one syscall, a
    module-level constant would be wrong for anything that drops privileges
    after import, and the ceilings depend on what this particular call asked
    for.

    A ceiling is only disclaimed when it was REQUESTED, in the same way `no_net`
    is. Naming a limit the caller never asked for would put permanent noise in
    every result, and a field that always says something is a field callers
    stop reading.
    """
    out = list(_FALLBACK_UNMEASURED)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        out.append(_UID0_PROCESS_CEILING)

    if IS_WINDOWS:
        # No setrlimit and no fork, so there is no hook to apply anything
        # through. The process ceiling is unconditional because the fork-bomb
        # guard is always meant to be on — the caller never asks for it, which
        # is precisely why its absence has to be stated.
        out.append("max_processes: " + _NO_RLIMITS_AT_ALL.format("process"))
        if max_memory_mb > 0:
            out.append("max_memory_mb: " + _NO_RLIMITS_AT_ALL.format("memory"))
        if max_cpu > 0:
            out.append("max_cpu: " + _NO_RLIMITS_AT_ALL.format("CPU"))
    elif sys.platform == "darwin" and max_memory_mb > 0:
        out.append(_MACOS_AS_IGNORED)
    return out


def _runtime_unavailable_result(name: str, phase: str, argv: list[str], exc: OSError,
                                workdir: str, started: float, no_net: bool) -> dict:
    """The documented result shape for a runtime/compiler that failed to spawn.

    The Rust executor never needs this: `Command::spawn()` failing there flows
    through the SAME StepResult -> JSON envelope as any other outcome, so a
    missing binary still comes back with the full shape a caller can inspect.
    This path did not have that for free — `subprocess.Popen` raising OSError
    (FileNotFoundError for a missing executable, PermissionError for one that
    is not executable) went uncaught and escaped `execute()` entirely, so
    asking the fallback for an uninstalled runtime raised an exception instead
    of returning a result. Same keys as the run-phase success path (see the
    comment there): a caller switching on `verdict` or reading `platform`
    should not need a different code path for this failure than for any other.
    """
    missing = argv[0] if argv else "?"
    detail = f"runtime unavailable for the {phase} phase of {name!r}: {missing!r} ({exc})"
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": False, "language": name, "phase": phase,
        "stdout": "", "stderr": detail, "exit_code": None,
        "duration_ms": elapsed_ms, "workdir": workdir,
        "compile_ms": elapsed_ms if phase == "compile" else 0,
        "total_ms": elapsed_ms,
        "timed_out": False,
        "output_truncated": False,
        "verdict": "RTE",
        "cpu_ms": 0,
        "peak_memory_kb": None,
        "platform": sys.platform,
        # No ceilings are named here: nothing spawned, so there is nothing
        # that could have been enforced or not. Disclaiming a limit for a run
        # that never happened is noise, not honesty.
        "unenforced": (["no_net: needs the native executor's LD_PRELOAD shim"]
                       if no_net else []) + _unmeasured(),
        "backend": "python",
        "error": detail,
    }


def _children_cpu_seconds() -> float:
    """Cumulative CPU of reaped children, or 0.0 where rusage is unavailable."""
    if IS_WINDOWS:
        return 0.0
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime


def _children_cpu_ms_since(before: float) -> int:
    """CPU consumed since `before`. APPROXIMATE.

    Sampled inside _FALLBACK_SPAWN_LOCK, so no other run through this path can
    reap a child inside the window. RUSAGE_CHILDREN is process-global, though,
    so a child reaped elsewhere in the host process during the same window is
    counted here as well. Unlike ru_maxrss it is at least additive, which is why
    differencing it is meaningful at all — but it is an upper bound, not a
    per-process measurement.
    """
    if IS_WINDOWS:
        return 0
    return max(0, int((_children_cpu_seconds() - before) * 1000))


def _execute_python(language: str, code: str, stdin: str = "", timeout: int = 10,
                    workdir: str | None = None, max_memory_mb: int = 0,
                    max_output_kb: int = 0, max_cpu: int = 0,
                    no_net: bool = False) -> dict:
    name = registry.canonical(language)
    if name is None:
        known = ", ".join(sorted(registry.LANGUAGES))
        return {"ok": False, "error": f"unknown language '{language}'. Available: {known}"}

    entry = registry.LANGUAGES[name]
    ext = registry.EXTENSIONS[name]
    # A caller-supplied workdir is a SESSION directory and must be used as-is,
    # and never deleted. This path ignored it entirely: a workspace session
    # running on the fallback backend wrote its files into a throwaway tempdir
    # that was then removed, so the session looked successful and stayed empty.
    # Truthiness, not `is not None`: workdir="" falls through to a fresh
    # tempdir on the next line, so treating it as caller-supplied would leave
    # that tempdir undeleted. The two tests must agree on what "supplied" means.
    caller_workdir = bool(workdir)
    workdir = workdir or tempfile.mkdtemp(prefix="codecalc-")
    # Recorded BEFORE the source is written or anything runs in the directory,
    # exactly like main.rs's created_identity: the code about to execute has
    # this directory as its cwd and can rename another one into its place
    # before the `finally` below gets to clean up. None for a caller-supplied
    # workdir, which this function never deletes.
    created_identity = None if caller_workdir else _dir_identity(workdir)
    cap = max_output_kb * 1024 if max_output_kb > 0 else MAX_OUTPUT_BYTES
    started = time.monotonic()
    try:
        src = Path(workdir) / f"main.{ext}"
        src.write_text(code)
        # Windows needs the .exe extension on the compiled artifact.
        exe_name = "a.exe" if IS_WINDOWS else "a.out"
        fmt = {"file": str(src), "exe": str(Path(workdir) / exe_name), "work": workdir}

        compile_ms = 0
        if entry["compile"]:
            argv = [a.format(**fmt) for a in entry["compile"]]
            try:
                rc, out, err, to, _ = _run_step(argv, workdir, timeout, "",
                                                max_memory_mb, max_cpu, cap)
            except OSError as exc:
                # The compiler binary itself is missing or not executable.
                # Unhandled, this escaped as a raised exception instead of the
                # documented result shape every other failure path returns.
                return _runtime_unavailable_result(name, "compile", argv, exc,
                                                   workdir, started, no_net)
            if to:
                return {"ok": False, "language": name, "phase": "compile",
                        # _trim MUST get the caller's cap here. Without it the
                        # compile path fell back to the 64 KiB default while
                        # the drain had already bounded the bytes at the
                        # caller's cap, so the two disagreed in both
                        # directions: max_output_kb=1 against a 2 KiB compiler
                        # error retained 1025 bytes and added NO truncation
                        # marker (1025 < 65536), i.e. silent truncation
                        # presented as complete output; and max_output_kb=128
                        # against 70 KiB was falsely cut at 64 KiB.
                        "stdout": "", "stderr": _trim(err, cap), "exit_code": None,
                        "duration_ms": 0, "timed_out": True}
            compile_ms = int((time.monotonic() - started) * 1000)
            if rc != 0:
                return {"ok": False, "language": name, "phase": "compile",
                        "stdout": _trim(out, cap), "stderr": _trim(err, cap), "exit_code": rc,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "timed_out": False}

        argv = [a.format(**fmt) for a in entry["run"]]
        run_started = time.monotonic()
        try:
            rc, out, err, to, cpu_ms = _run_step(argv, workdir, timeout, stdin,
                                                 max_memory_mb, max_cpu, cap)
        except OSError as exc:
            # The interpreter/runtime binary itself is missing or not
            # executable — same shape gap as the compile phase above.
            return _runtime_unavailable_result(name, "run", argv, exc,
                                               workdir, started, no_net)
        duration_ms = int((time.monotonic() - run_started) * 1000)
        # out/err are already capped at cap+1 bytes by _run_step's incremental
        # drain (see _BoundedDrain) — this comparison is what decides whether
        # _trim() below should append the truncation marker, not what enforces
        # the cap. The cap itself is enforced DURING the read, not here.
        truncated = len(out) > cap or len(err) > cap
        # The documented return shape is the SAME on both backends. It was not:
        # this path omitted verdict, cpu_ms, peak_memory_kb, output_truncated,
        # unenforced, total_ms, compile_ms and platform — eight fields, one of
        # them (`verdict`) named in execute_code's own docstring. A caller
        # switching on verdict could not tell a timeout from a crash here, and
        # CI never noticed because contract_check only exercises the binary.
        return {"ok": rc == 0, "language": name, "phase": "run",
                "stdout": _trim(out, cap), "stderr": _trim(err, cap), "exit_code": rc,
                "duration_ms": duration_ms, "workdir": workdir,
                "compile_ms": compile_ms,
                "total_ms": int((time.monotonic() - started) * 1000),
                "timed_out": to,
                "output_truncated": truncated,
                "verdict": _fallback_verdict(rc, to, truncated),
                "cpu_ms": cpu_ms,
                # A high-water mark across every child this process has reaped,
                # not this run's peak — ru_maxrss is a maximum, so it cannot be
                # differenced. None means "not measured", never zero, because a
                # zero here would read as "used no memory".
                "peak_memory_kb": None,
                "platform": sys.platform,
                "unenforced": (["no_net: needs the native executor's LD_PRELOAD shim"]
                               if no_net else []) + _unmeasured(max_memory_mb, max_cpu),
                "backend": "python"}
    finally:
        # Only a directory this function created, and only if it is STILL
        # that directory. "Caller-supplied workdir belongs to the caller" is
        # the ownership rule and was the only one enforced here; the Rust
        # executor enforces a second one — identity, re-checked right before
        # removal — because the code that just ran had this directory as its
        # cwd and could have renamed a victim into its place. See
        # _rmtree_checked.
        if not caller_workdir:
            _rmtree_checked(workdir, created_identity)


# ── public API ─────────────────────────────────────────────────────────────

_rust = _rust_binary()


def _require_native_or_die() -> None:
    """Fail loudly, at import time, if CODECALC_REQUIRE_NATIVE=1 and no usable
    Rust binary was found.

    Without this, an operator who sets the variable gets nothing: `_rust` is
    simply None and every call quietly runs `_execute_python` instead — the
    same fallback whose own `unenforced` field admits it cannot apply `no_net`.
    "Require native" has to mean the process never answers a single call on
    the weaker backend, and the only way to guarantee that is to refuse to
    come up at all rather than gate it per-call: a per-call check would still
    let `list_languages` or `probe()` succeed and look like a healthy server.

    Any value other than unset/"0" counts as enabled, matching the informal
    truthy convention this repo's own env vars already use elsewhere
    (CODECALC_MAX_PROCESSES, CODECALC_PROCESS_HEADROOM are "set means use it").
    """
    if os.environ.get(REQUIRE_NATIVE_ENV, "0") == "0":
        return
    if _rust is not None:
        return
    raise RuntimeError(
        f"{REQUIRE_NATIVE_ENV} is set, but no usable codecalc-exec binary was "
        f"found (checked $CODECALC_EXEC_BIN and {_binary_candidates()}). "
        "The Python fallback cannot enforce no_net and is not the sandbox this "
        "variable asks for, so codecalc refuses to start rather than downgrade "
        "silently. Build the Rust executor (README: 'Build the Rust core'), "
        "point CODECALC_EXEC_BIN at a working binary, or unset "
        f"{REQUIRE_NATIVE_ENV} to accept the weaker fallback."
    )


_require_native_or_die()


def backend() -> str:
    """'rust' when the native binary is in use, else 'python'."""
    return "rust" if _rust else "python"


def execute(language: str, code: str, stdin: str = "", timeout: int = 10,
            workdir: str | None = None, max_memory_mb: int = 0,
            max_output_kb: int = 0, max_cpu: int = 0, no_net: bool = False) -> dict:
    if _rust:
        stdin_path = None
        try:
            # stdin via a file: argv has ~2MB E2BIG limit; programs may read
            # large inputs. Also avoids any shell-quoting concerns entirely.
            if stdin:
                import tempfile as _tf
                with _tf.NamedTemporaryFile(mode="w", prefix="codecalc-stdin-",
                                            suffix=".txt", delete=False) as f:
                    f.write(stdin)
                    stdin_path = f.name
                stdin_args = ["--stdin-file", stdin_path]
            else:
                stdin_args = []
            args = [_rust, "--lang", language, "--timeout", str(timeout)]
            if workdir:
                args += ["--workdir", workdir]
            if max_memory_mb > 0:
                args += ["--max-memory-mb", str(max_memory_mb)]
            if max_output_kb > 0:
                args += ["--max-output-kb", str(max_output_kb)]
            if max_cpu > 0:
                args += ["--max-cpu", str(max_cpu)]
            if no_net:
                args += ["--no-net"]
            # Popen + kill the GROUP, not subprocess.run. On TimeoutExpired
            # subprocess.run kills only the direct child — the executor — and
            # the sandboxed processes it spawned are in their own process group
            # (the executor calls process_group(0)), so they survive. Verified:
            # killing the executor left `sleep` running with no parent to reap
            # it and no wall clock on it at all.
            #
            # start_new_session puts the executor at the head of its own group so
            # killpg here reaches the executor AND everything under it.
            proc = _popen_group(args + stdin_args)
            try:
                out, err = proc.communicate(input=code.encode(), timeout=timeout + 30)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                out, err = proc.communicate()
                return {"ok": False, "error": "executor timed out",
                        "timed_out": True, "exit_code": None,
                        "stderr": (err or b"").decode(errors="replace")[:400]}
            result = json.loads(out.decode(errors="replace"))
            if isinstance(result, dict) and "ok" in result:
                # The binary's own JSON carries no "backend" key — it has no
                # concept of the Python fallback to distinguish itself from,
                # and scripts/contract_check.py asserts its shape directly
                # against the binary, so that shape must not change. Added
                # HERE instead: this is the one place both backends' results
                # pass through before a caller sees them, so it is the one
                # place that can make them agree. Without it, an absent
                # "backend" key on this path was indistinguishable from an
                # older build that never reported the field at all — a caller
                # could not tell "rust, unreported" from "rust, not present".
                result["backend"] = "rust"
                return result
            return {"ok": False, "error": f"executor produced invalid output: {err[:200]!r}"}
        except Exception as exc:
            return {"ok": False, "error": f"executor failed: {exc}"}
        finally:
            if stdin_path:
                Path(stdin_path).unlink(missing_ok=True)
    return _execute_python(language, code, stdin=stdin, timeout=timeout,
                           workdir=workdir, max_memory_mb=max_memory_mb,
                           max_output_kb=max_output_kb, max_cpu=max_cpu,
                           no_net=no_net)


def probe() -> dict:
    """Runtime availability per language (uses the Rust --probe when present)."""
    if _rust:
        try:
            proc = subprocess.run([_rust, "--probe"], capture_output=True, timeout=15)
            data = json.loads(proc.stdout.decode())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    # Python fallback: check the primary command of each language
    out = {}
    for name, entry in registry.LANGUAGES.items():
        cmd = entry["run"][0] if entry["run"] else ""
        if cmd.startswith("{"):
            cmd = (entry["compile"] or ["bash"])[0] if entry["compile"] else "bash"
        if cmd.startswith(("bash", "sh")):
            cmd = "bash"
        out[name] = shutil.which(cmd) is not None
    return out
