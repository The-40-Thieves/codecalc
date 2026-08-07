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
import resource
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import registry

MAX_OUTPUT_BYTES = 64 * 1024
AS_LIMIT_BYTES = 2048 * 1024**3      # 2 TiB VA — V8/JVM/tcmalloc reserve huge VA
FSIZE_LIMIT_BYTES = 256 * 1024**2    # 256 MiB output files
NFILE_LIMIT = 256
# RLIMIT_NPROC counts ALL processes of this uid, not just sandbox children —
# this host runs ~120 ubuntu processes already, so 128 would break everything.
NPROC_LIMIT = 1024
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
    env["PATH"] = registry.RUNTIME_PATH
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _limits(timeout: int):
    def _apply():
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (timeout + CPU_GRACE_SECONDS,) * 2)
            resource.setrlimit(resource.RLIMIT_AS, (AS_LIMIT_BYTES,) * 2)
            resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE_LIMIT_BYTES,) * 2)
            resource.setrlimit(resource.RLIMIT_NOFILE, (NFILE_LIMIT, NFILE_LIMIT))
            resource.setrlimit(resource.RLIMIT_NPROC, (NPROC_LIMIT, NPROC_LIMIT))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):
            pass

    return _apply


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except Exception:
            pass


def _run_step(argv: list[str], cwd: str, timeout: int, stdin: str) -> tuple[int, bytes, bytes, bool]:
    # NOTE: preexec_fn is unsafe under threads (PLW1509) — this is the PYTHON
    # FALLBACK path only, used when the Rust binary is absent. It serializes
    # spawns with a lock to avoid concurrent fork+preexec races; the production
    # path (Rust executor) never uses preexec_fn.
    with _FALLBACK_SPAWN_LOCK:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_limits(timeout),  # noqa: PLW1509 — documented above
        )
        try:
            out, err = proc.communicate(input=stdin.encode(), timeout=timeout)
            return proc.returncode, out, err, False
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()
            return -1, b"", b"<killed: exceeded wall-clock timeout>", True


def _trim(b: bytes) -> str:
    if len(b) > MAX_OUTPUT_BYTES:
        return b[:MAX_OUTPUT_BYTES].decode(errors="replace") + "\n…[truncated]"
    return b.decode(errors="replace")


def _execute_python(language: str, code: str, stdin: str = "", timeout: int = 10) -> dict:
    name = registry.canonical(language)
    if name is None:
        known = ", ".join(sorted(registry.LANGUAGES))
        return {"ok": False, "error": f"unknown language '{language}'. Available: {known}"}

    entry = registry.LANGUAGES[name]
    ext = registry.EXTENSIONS[name]
    workdir = tempfile.mkdtemp(prefix="codecalc-")
    started = time.monotonic()
    try:
        src = Path(workdir) / f"main.{ext}"
        src.write_text(code)
        fmt = {"file": str(src), "exe": str(Path(workdir) / "a.out"), "work": workdir}

        if entry["compile"]:
            argv = [a.format(**fmt) for a in entry["compile"]]
            rc, out, err, to = _run_step(argv, workdir, timeout, "")
            if to:
                return {"ok": False, "language": name, "phase": "compile",
                        "stdout": "", "stderr": _trim(err), "exit_code": None,
                        "duration_ms": 0, "timed_out": True}
            if rc != 0:
                return {"ok": False, "language": name, "phase": "compile",
                        "stdout": _trim(out), "stderr": _trim(err), "exit_code": rc,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "timed_out": False}

        argv = [a.format(**fmt) for a in entry["run"]]
        rc, out, err, to = _run_step(argv, workdir, timeout, stdin)
        return {"ok": rc == 0, "language": name, "phase": "run",
                "stdout": _trim(out), "stderr": _trim(err), "exit_code": rc,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "timed_out": to}
    finally:
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
            proc = subprocess.run(
                args + stdin_args,
                input=code.encode(),
                capture_output=True,
                timeout=timeout + 30,
            )
            result = json.loads(proc.stdout.decode(errors="replace"))
            if isinstance(result, dict) and "ok" in result:
                return result
            return {"ok": False, "error": f"executor produced invalid output: {proc.stderr[:200]!r}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "executor timed out",
                    "timed_out": True, "exit_code": None}
        except Exception as exc:
            return {"ok": False, "error": f"executor failed: {exc}"}
        finally:
            if stdin_path:
                try:
                    os.unlink(stdin_path)
                except OSError:
                    pass
    return _execute_python(language, code, stdin=stdin, timeout=timeout)


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
