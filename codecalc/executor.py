"""Sandboxed multi-language executor.

Primary backend: the Rust `codecalc-exec` binary (memory-safe, no eval in the
host, rlimits + timeout + process-group kill in the sandbox). Falls back to a
pure-Python implementation when the binary isn't built yet.

Binary selection is architecture-aware so the right artifact is picked on both
arm64 and x86_64 hosts (including the static musl builds for older machines).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
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


def _binary_candidates() -> list[str]:
    """Ordered candidate paths for the Rust executor, arch-aware."""
    machine = platform.machine().lower()
    exe_name = "codecalc-exec.exe" if os.name == "nt" else "codecalc-exec"
    base = Path(__file__).resolve().parent.parent / "bin"
    if machine in ("x86_64", "amd64"):
        names = [exe_name, "codecalc-exec-x86_64-musl"]
    elif machine in ("aarch64", "arm64"):
        names = [exe_name, "codecalc-exec-native-aarch64", "codecalc-exec-aarch64-musl"]
    else:
        names = [exe_name]
    return [str(base / n) for n in names]


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
_ENV_ALLOWLIST = {
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONUNBUFFERED",
    "JAVA_HOME", "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOMODCACHE",
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
            status = (entry / "status").read_text()
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


def _run_step(argv: list[str], cwd: str, timeout: int, stdin: str,
              max_memory_mb: int = 0, max_cpu: int = 0) -> tuple[int, bytes, bytes, bool, int]:
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
        try:
            out, err = proc.communicate(input=stdin.encode(), timeout=timeout)
            return (proc.returncode, out, err, False,
                    _children_cpu_ms_since(cpu_before))
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()
            return (-1, b"", b"<killed: exceeded wall-clock timeout>", True,
                    _children_cpu_ms_since(cpu_before))


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
            rc, out, err, to, _ = _run_step(argv, workdir, timeout, "",
                                            max_memory_mb, max_cpu)
            if to:
                return {"ok": False, "language": name, "phase": "compile",
                        "stdout": "", "stderr": _trim(err), "exit_code": None,
                        "duration_ms": 0, "timed_out": True}
            compile_ms = int((time.monotonic() - started) * 1000)
            if rc != 0:
                return {"ok": False, "language": name, "phase": "compile",
                        "stdout": _trim(out), "stderr": _trim(err), "exit_code": rc,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "timed_out": False}

        argv = [a.format(**fmt) for a in entry["run"]]
        run_started = time.monotonic()
        rc, out, err, to, cpu_ms = _run_step(argv, workdir, timeout, stdin,
                                             max_memory_mb, max_cpu)
        duration_ms = int((time.monotonic() - run_started) * 1000)
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
                               if no_net else []) + _FALLBACK_UNMEASURED,
                "backend": "python"}
    finally:
        # Only a directory this function created. Same rule as the Rust
        # executor: a caller-supplied workdir belongs to the caller.
        if not caller_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


# ── public API ─────────────────────────────────────────────────────────────

_rust = _rust_binary()


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
