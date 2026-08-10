"""Sessions: persistent workspaces + stateful interpreter workers.

A session is a directory under the session root that survives between calls.
Code executed with `session_id=` runs in that directory (via the Rust
executor's --workdir), so files/artifacts persist. For languages that support
it (python3, node), a long-lived REPL worker keeps interpreter state
(variables, imports, data) across calls — the "calculator that remembers"
primitive.

Security: session dirs live under a dedicated root owned by this process;
file tools (list/read/write) are strictly confined to that root.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from . import executor, landlock, registry

SESSION_ROOT = Path(os.environ.get("CODECALC_SESSION_ROOT", "~/.codecalc/sessions")).expanduser()

#: languages that get a stateful REPL worker (interpreters with exec()):
_WORKER_LANGS = {"python3", "node"}

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_lock = threading.Lock()
_workers: dict[str, Worker] = {}
#: Session workdir identity (device, inode), recorded the moment each session
#: directory is created and consulted by `stop()` before deleting it — mirrors
#: executor/src/main.rs's created_identity/remove_own_workdir. A session's
#: directory is the cwd of every call executed against it (via the Rust
#: executor's --workdir), so executed code can rename another directory into
#: its place; checking identity right before removal, instead of trusting that
#: the path still names what this process created, is what keeps `stop()` from
#: deleting whatever a swap left there. Entries for orphaned session dirs that
#: predate this process (found by `list_sessions()` but never `start()`-ed
#: here) are simply absent, which `stop()` treats as "no identity to compare".
_SESSION_DIR_IDENTITY: dict[str, tuple[int, int] | None] = {}


def _session_dir(session_id: str) -> Path:
    if not _SAFE_NAME.match(session_id):
        raise ValueError("invalid session id")
    root = SESSION_ROOT.resolve()
    d = (root / session_id).resolve()
    # Defence in depth: _SAFE_NAME already forbids '/' and '.', so this cannot
    # currently fire. Component-wise rather than string-prefix for the same
    # reason as _jail — if the id charset is ever widened, the weaker check
    # would have silently become the hole.
    if not d.is_relative_to(root):
        raise ValueError("session path escapes session root")
    return d


def start(language: str = "python3", name: str | None = None) -> dict:
    """Create a session: fresh workspace dir; REPL worker for supported langs."""
    name = registry.canonical(language) or "python3"
    if name not in _WORKER_LANGS:
        # workspace-only session (no stateful worker)
        session_id = f"{name}-{uuid.uuid4().hex[:8]}"
        d = _session_dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        # Recorded immediately after creation, before any code ever runs with
        # this directory as its cwd — see _SESSION_DIR_IDENTITY.
        with _lock:
            _SESSION_DIR_IDENTITY[session_id] = executor._dir_identity(d)
        return {
            "ok": True, "session_id": session_id, "language": name,
            "stateful": False, "workdir": str(d), "files": _list(d),
        }
    session_id = f"{name}-{uuid.uuid4().hex[:8]}"
    d = _session_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    with _lock:
        _SESSION_DIR_IDENTITY[session_id] = executor._dir_identity(d)
    w, why = _spawn_worker(name, d)
    if w is None:
        return {"ok": False,
                "error": f"failed to start {name} REPL worker: {why or 'unknown cause'}"}
    with _lock:
        _workers[session_id] = w
    return {
        "ok": True, "session_id": session_id, "language": name,
        "stateful": True, "workdir": str(d), "files": _list(d),
        # Stated at creation as well as on every result: a caller deciding
        # whether to put untrusted code in a session needs this BEFORE it runs
        # any, not only in the output afterwards.
        "confined": w.confined,
        "unenforced": _session_unenforced(w),
    }


def stop(session_id: str) -> dict:
    """Kill the worker (if any) and delete the workspace.

    Deletion is identity-checked (device, inode recorded at creation,
    re-checked here) — session code runs with this directory as its cwd, so
    it can rename another directory into this path before stop() is called.
    `deleted` reports whether removal actually happened rather than assuming
    it did.
    """
    with _lock:
        w = _workers.pop(session_id, None)
        created = _SESSION_DIR_IDENTITY.pop(session_id, None)
    if w is not None:
        w.close()
    d = _session_dir(session_id)
    deleted = executor._rmtree_checked(d, created)
    return {"ok": True, "session_id": session_id, "deleted": deleted}


def list_sessions() -> dict:
    out = []
    with _lock:
        for sid, w in _workers.items():
            out.append({"session_id": sid, "language": w.language, "stateful": True,
                        "workdir": str(_session_dir(sid)), "alive": w.alive()})
    for d in SESSION_ROOT.iterdir() if SESSION_ROOT.is_dir() else []:
        if d.is_dir() and not any(s["session_id"] == d.name for s in out):
            out.append({"session_id": d.name, "language": "?", "stateful": False,
                        "workdir": str(d), "alive": False})
    return {"ok": True, "sessions": out}


#: Per-call ceilings a STATEFUL worker cannot honour, and why.
#:
#: The worker is one long-lived process shared by every call in the session, so
#: a per-call rlimit cannot be applied to it after the fact, and `--no-net` is
#: an LD_PRELOAD decision taken at exec time. These used to be accepted and
#: silently dropped: `no_net=True` with a session_id reached the network, and
#: `max_memory_mb=32` allocated 300 MB. Reported through `unenforced` — the same
#: field the Rust executor already uses to say "asked for, not applied" —
#: instead of being discarded.
_WORKER_CANNOT_ENFORCE = {
    "max_memory_mb": "fixed at worker start; use a workspace session or omit session_id",
    "max_cpu": "cumulative across a long-lived worker; the per-call wall clock applies instead",
    "no_net": "LD_PRELOAD is applied at exec; start the session without a worker to use it",
    "max_output_kb": "the worker caps at 64 KiB; a custom cap needs a fresh process",
}

#: Guarantees a ONE-SHOT execution carries that a stateful worker does not,
#: keyed by the name `check_parity.py` matches against the executor.
#:
#: These are reported UNCONDITIONALLY, which is the opposite of the rule
#: `executor._unmeasured` follows ("a ceiling is only disclaimed when it was
#: REQUESTED... a field that always says something is a field callers stop
#: reading"). The distinction is who could have known to ask.
#:
#: `max_memory_mb` is a ceiling the caller passes, so naming it unasked is
#: noise. The Rust sandbox is not: it is what `execute_code()` gives you by
#: default, and adding `session_id=` silently takes it away. A caller cannot
#: ask for a guarantee it does not know it just lost, so the only place that
#: fact can appear is in every result that lacks it. #104.
_SESSION_STRUCTURAL_GAPS = {
    "sandbox_backend": (
        "the worker is a plain subprocess, not codecalc-exec: the Rust "
        "executor's ceilings, argv/env handling and its own unenforced "
        "reporting do not apply to this call"),
    "no_net": (
        "the LD_PRELOAD network shim is applied at exec and a worker is "
        "long-lived, so this session reaches the network"),
    "process_group_kill": (
        "applied per call on the one-shot path; here the worker's group is "
        "reaped at timeout or session_stop, so a background child outlives "
        "the call that started it"),
    "peak_memory_kb": (
        "not measured for worker calls; the field is null rather than zero"),
}

#: The filesystem half, which is conditional because it is the one gap that
#: CAN close. Landlock confines the worker to its workspace where the kernel
#: supports it (#23 built the module; ABI is checked at spawn).
_NO_FS_CONFINEMENT = (
    "filesystem_confinement: the worker can read any path this user can; "
    "Landlock is unavailable here ({})")


def _session_unenforced(w: Worker | None) -> list[str]:
    """Everything a stateful session does not carry, whether or not asked.

    `w is None` means a workspace-only session, which runs a fresh sandboxed
    process per call and therefore carries all of it — the empty list is a
    real answer, not a missing one.
    """
    if w is None:
        return []
    out = [f"{k}: {v}" for k, v in _SESSION_STRUCTURAL_GAPS.items()]
    if not w.confined:
        abi = landlock.abi_version()
        why = "kernel reports Landlock ABI 0" if abi == 0 else f"ABI {abi}"
        out.append(_NO_FS_CONFINEMENT.format(why))
    else:
        # Confined is not unconfined, but it is also not total. Landlock leaves
        # metadata syscalls and (below ABI 10) UDP alone at every version, and
        # saying "confined" without that reads as more than it is.
        out.extend(landlock.unenforced_reasons(scope="session"))
    return out


def execute(session_id: str, code: str, language: str | None = None,
            stdin: str = "", timeout: int = 30, max_memory_mb: int = 0,
            max_output_kb: int = 0, max_cpu: int = 0, no_net: bool = False) -> dict:
    """Run code in a session. Stateful langs go to the REPL worker; the rest
    run as fresh processes in the session workdir.

    Per-call ceilings are passed through to the workspace path, which runs a
    fresh sandboxed process and can honour all of them. A stateful worker
    cannot, so anything it cannot apply comes back in `unenforced` rather than
    being dropped on the floor.
    """
    d = _session_dir(session_id)
    if not d.is_dir():
        return {"ok": False, "error": f"unknown session '{session_id}'"}
    with _lock:
        w = _workers.get(session_id)
    if w is not None:
        if language and registry.canonical(language) != w.language:
            return {"ok": False, "error": f"session is {w.language}, not {language}"}
        asked = {"max_memory_mb": max_memory_mb, "max_cpu": max_cpu,
                 "no_net": no_net, "max_output_kb": max_output_kb}
        out = w.run(code, stdin=stdin, timeout=timeout)
        # Two lists, deliberately: what the caller asked for and did not get,
        # and what the caller never asked for because it did not know it was
        # being dropped. `no_net` appears in both vocabularies; dict ordering
        # plus the dedup below keeps the per-call phrasing, which is the more
        # actionable of the two because it names the fix.
        unenforced = [f"{k}: {_WORKER_CANNOT_ENFORCE[k]}" for k, v in asked.items() if v]
        seen = {e.split(":", 1)[0] for e in unenforced}
        unenforced += [e for e in _session_unenforced(w)
                       if e.split(":", 1)[0] not in seen]
        out.setdefault("unenforced", []).extend(unenforced)
        out["backend"] = "session-worker"
        out["confined"] = w.confined
        return out
    # workspace-only session: fresh process in the session dir, fully sandboxed
    lang = registry.canonical(language) if language else "python3"
    return executor.execute(lang, code, stdin=stdin, timeout=timeout, workdir=str(d),
                            max_memory_mb=max_memory_mb, max_output_kb=max_output_kb,
                            max_cpu=max_cpu, no_net=no_net)


def write_file(session_id: str, path: str, content: str) -> dict:
    d = _session_dir(session_id)
    if not d.is_dir():
        return {"ok": False, "error": f"unknown session '{session_id}'"}
    target = _jail(d, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_nofollow(target, content)
    return {"ok": True, "path": str(target.relative_to(d))}


def list_files(session_id: str, path: str = "") -> dict:
    d = _session_dir(session_id)
    if not d.is_dir():
        return {"ok": False, "error": f"unknown session '{session_id}'"}
    base = _jail(d, path)
    if not base.is_dir():
        return {"ok": False, "error": f"no such directory: {path}"}
    return {"ok": True, "path": path or ".", "files": _list(base)}


def resource_read(session_id: str, path: str, max_bytes: int = 4 * 1024 * 1024) -> tuple[bytes, str] | None:
    """Serve a session file as an MCP resource: (bytes, mime_type) or None.

    Image files are served as image/png|jpeg|gif|webp so MCP clients render
    them inline; everything else as application/octet-stream.
    """
    import mimetypes
    d = _session_dir(session_id)
    target = _jail(d, path)
    if not target.is_file():
        return None
    data = target.read_bytes()
    if len(data) > max_bytes:
        return None
    mime, _ = mimetypes.guess_type(str(target))
    if mime and mime.startswith("image/"):
        return data, mime
    return data, "application/octet-stream"


def artifacts(session_id: str) -> dict:
    """Files created by executed code (anything beyond the runner's own files)."""
    d = _session_dir(session_id)
    if not d.is_dir():
        return {"ok": False, "error": f"unknown session '{session_id}'"}
    files = []
    for p in sorted(d.rglob("*")):
        if "__pycache__" in p.parts or p.name.endswith(".pyc"):
            continue
        if p.is_file() and p.name not in {"main.py", "main.js", "run.out", "run.err", "run.in",
                                          "compile.out", "compile.err", "compile.in", "a.out"}:
            rel = str(p.relative_to(d))
            files.append({"path": rel, "size": p.stat().st_size})
    return {"ok": True, "session_id": session_id, "artifacts": files}


def _list(d: Path) -> list[dict]:
    out = []
    for p in sorted(d.iterdir()):
        if p.is_dir():
            out.append({"path": p.name + "/", "type": "dir"})
        else:
            out.append({"path": p.name, "type": "file", "size": p.stat().st_size})
    return out


#: O_NOFOLLOW does not exist on Windows, where the value 0 makes the flag a
#: no-op. That is not silently equivalent: see `_write_nofollow`.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _write_nofollow(target: Path, content: str) -> None:
    """Write `target`, refusing to follow a symlink at the final component.

    `_jail` resolves the path and the caller then opens it, and those are two
    separate syscalls. Code running inside the session shares the workspace as
    its cwd, so it can delete the resolved file and drop a symlink to, say,
    /path/to/home/.ssh/authorized_keys in its place in between — the check
    passes on the real path and the write lands on the attacker's target.
    Reported alongside #104.

    O_NOFOLLOW closes the final component, which is the component `_jail`'s
    resolve() cannot re-verify. Parent components are still a window in
    principle: fully closing it needs openat2(RESOLVE_BENEATH), which is Linux
    5.6+ and has no portable equivalent, and the parents here are inside a
    workspace that resolve() already walked. On Windows the flag is absent
    entirely and this degrades to an ordinary open — which is why the Landlock
    confinement above matters even for a path this function guards.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)


def _jail(d: Path, path: str) -> Path:
    """Resolve `path` under the session dir; refuse anything outside it.

    `is_relative_to` compares path COMPONENTS. The previous check was
    `str(p).startswith(str(d))`, and a string prefix is not a path boundary: a
    sibling directory whose name merely extends the session id satisfied it, so
    `../python3-deadbeefEVIL/pwned.txt` resolved out of the workspace and
    `session_write_file` would mkdir it into existence.

    resolve() first, so a symlink planted inside the workspace by executed code
    is followed BEFORE the comparison and cannot be used as a write primitive.
    """
    base = d.resolve()
    p = (base / path).resolve()
    if not p.is_relative_to(base):
        raise ValueError("path escapes session workspace")
    return p


# ── REPL workers ───────────────────────────────────────────────────────────

def _readline_timeout(stream, timeout: float) -> str | None:
    """Read one line from a pipe with a wall-clock timeout. None on timeout.

    Uses a reader THREAD rather than select(). On Windows `select.select` is
    provided by WinSock and accepts sockets only — handing it a subprocess pipe
    raises, so the previous implementation made every stateful session
    (session_start on python3/node) fail there outright. A blocking readline on
    a daemon thread is the portable primitive: it behaves identically on all
    three platforms and needs no non-blocking pipe support.

    The thread is a daemon and the queue is bounded to one line, so a worker that
    never answers leaks one parked thread rather than blocking interpreter exit.
    """
    import queue
    import threading as _th

    q: queue.Queue = queue.Queue(maxsize=1)

    def _read():
        try:
            line = stream.readline()
        except Exception:
            line = b""
        try:
            q.put_nowait(line)
        except queue.Full:  # pragma: no cover — caller already timed out
            pass

    t = _th.Thread(target=_read, daemon=True)
    t.start()
    try:
        line = q.get(timeout=timeout)
    except queue.Empty:
        return None
    if not line:
        return ""  # EOF: the worker closed its stdout
    return line.decode(errors="replace").rstrip("\r\n")


class Worker:
    """Stateful interpreter: JSON-lines protocol on stdin/stdout. Globals
    persist across run() calls because exec() reuses one dict."""

    def __init__(self, language: str, proc: subprocess.Popen, proto=None):
        self.language = language
        self.proc = proc
        #: Whether Landlock confined this worker to its workspace. Set by
        #: `_spawn_worker` after the process is up; read by `execute` so a
        #: result can say which filesystem guarantee the caller actually got
        #: rather than describing the one the code hoped to apply.
        self.confined = False
        #: Where RESPONSES arrive. Four routes, and on none of them can output
        #: written to fd 1 by a child process land in the protocol stream:
        #:
        #:   node, POSIX      an out-of-band pipe handed over with `pass_fds`
        #:   node, Windows    a `_TailReader` over a file the worker appends to,
        #:                    because there is no `pass_fds` or `preexec_fn`
        #:                    there to place a descriptor
        #:   python3, POSIX   the child's stdout, safe because the worker dups
        #:                    the ORIGINAL fd 1 at startup and points fd 1 at a
        #:                    capture file while executing (`_worker_bootstrap`)
        #:   python3, Windows a `_TailReader` over a file, same as node — the
        #:                    fd-1 dup above is not trusted there: a child can
        #:                    still be handed the OS-level standard HANDLE
        #:                    instead of the redirected CRT descriptor
        #:
        #: The echoed request id is a backstop on all four, not the guarantee
        #: on any of them. It was the only guard on Windows before the
        #: file-backed route existed; it is not any more.
        self._proto = proto if proto is not None else proc.stdout
        self._wlock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._seq = 0
        self._stderr_log: list[str] = []
        # drain stderr so a full pipe can never deadlock the worker
        self._drain = threading.Thread(target=self._drain_stderr, daemon=True)
        self._drain.start()
        # When the protocol has its own pipe, the worker's stdout carries only
        # whatever executed code wrote to fd 1 directly, and NOTHING reads it.
        # A session that writes enough there would fill the pipe and block the
        # worker forever — the same deadlock the stderr drain exists to prevent.
        if self._proto is not proc.stdout and proc.stdout is not None:
            self._raw = threading.Thread(target=self._drain_stdout, daemon=True)
            self._raw.start()

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                self._stderr_log.append(line.decode(errors="replace").rstrip())
                if len(self._stderr_log) > 200:
                    self._stderr_log.pop(0)
        except Exception:
            pass

    def _drain_stdout(self):
        """Discard fd-1 output. It cannot be attributed to a call — it may be
        written by a background process started several calls earlier — so it is
        dropped rather than reported against whichever request is in flight."""
        try:
            for _ in self.proc.stdout:
                pass
        except Exception:
            pass

    def stderr_tail(self, n: int = 5) -> str:
        return "\n".join(self._stderr_log[-n:])

    def alive(self) -> bool:
        return self.proc.poll() is None

    def run(self, code: str, stdin: str = "", timeout: int = 30) -> dict:
        if not self.alive():
            return {"ok": False, "error": f"{self.language} worker died",
                    "verdict": "RTE"}
        with self._wlock:
            self._seq += 1
            req_id = self._seq
            req = json.dumps({"id": req_id, "code": code, "stdin": stdin,
                              "timeout_ms": int(max(1, timeout) * 1000)})
            try:
                self.proc.stdin.write((req + "\n").encode())
                self.proc.stdin.flush()
                line = _readline_timeout(self._proto, timeout)
                if line is None:
                    # worker hung — kill it; the session is unusable
                    self.close()
                    return {"ok": False, "error": f"{self.language} worker timed out",
                            "verdict": "TLE"}
                if line == "":
                    return {"ok": False, "error": "worker closed", "verdict": "RTE"}
                out = json.loads(line)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                # OSError, not just BrokenPipeError. Writing to a worker that has
                # already exited raises EPIPE on POSIX and EINVAL on Windows, and
                # EINVAL is a plain OSError — so on Windows this escaped run()
                # entirely and reached the MCP caller as an unhandled exception
                # instead of the structured {"ok": false} every other failure
                # path returns. Found the first time these tests ran there.
                # KILL the worker. Returning an error while leaving the stream
                # desynced was far worse than the corruption itself: the real
                # reply stayed queued, so every later call returned the PREVIOUS
                # call's result with ok=True. Measured — a call asking for
                # `marker-4` came back with the output of the call before it.
                # A dead session is honest; an off-by-one session is not.
                self.close()
                return {"ok": False, "verdict": "RTE",
                        "error": f"worker protocol error, session terminated: {exc}"}
            # The worker echoes the id it answered. Without this, ANY future
            # desync silently returns a well-formed answer to a different
            # question, which no caller can detect.
            if out.get("id") != req_id:
                self.close()
                return {"ok": False, "verdict": "RTE",
                        "error": (f"worker replied to request {out.get('id')!r} while "
                                  f"{req_id!r} was outstanding; session terminated "
                                  f"rather than return another call's result")}
            out.pop("id", None)
        out.setdefault("session", True)
        return out

    def close(self):
        """Stop the worker and everything it started, then release its fds.

        Three things this did not do, all found in review after the fix above
        was already written:

        * It signalled only the worker. The worker is spawned with
          `start_new_session=True`, so anything it forked lives in ITS process
          group and simply carried on — the same one-hop mistake the native
          executor had, where killing the parent reaped the direct child and
          left the grandchild running against no clock at all.
        * After falling through to `kill()` it never reaped, leaving a zombie.
        * It closed no descriptors, so every dead session held on to its pipes
          until the server exited.

        Idempotent: `stop()` calls it, the timeout path calls it, and a protocol
        error calls it, so it must tolerate being called on an already-dead
        worker.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            executor._kill_group(self.proc)
        except Exception:
            with contextlib.suppress(Exception):
                self.proc.kill()
        with contextlib.suppress(Exception):
            self.proc.wait(timeout=3)
        # Close the streams AFTER the process is gone: closing the read end of
        # a pipe a live worker is writing to would hand it SIGPIPE instead of a
        # clean exit.
        for stream in (self._proto, self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()


_WORKER_BOOTSTRAP = r'''
import sys, json, io, traceback, contextlib

ns = {"__name__": "__main__", "__builtins__": __builtins__}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue
    code, stdin_data = req.get("code", ""), req.get("stdin", "")
    out_buf, err_buf = io.StringIO(), io.StringIO()
    prev_stdin, prev_out, prev_err = sys.stdin, sys.stdout, sys.stderr
    try:
        sys.stdin = io.StringIO(stdin_data)
        sys.stdout, sys.stderr = out_buf, err_buf
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            exec(compile(code, "<session>", "exec"), ns)
        ok, err = True, ""
    except BaseException:
        ok, err = False, traceback.format_exc()
    finally:
        sys.stdin, sys.stdout, sys.stderr = prev_stdin, prev_out, prev_err
    print(json.dumps({"ok": ok, "stdout": out_buf.getvalue(),
                      "stderr": err_buf.getvalue() or err,
                      "exit_code": 0 if ok else 1}), flush=True)
'''


#: Test seam: force the file-backed protocol channel on a platform that would
#: otherwise use a pipe (node) or a dup'd fd 1 (python3). The Windows path is
#: otherwise unreachable from CI on Linux or macOS, and an unexercised
#: fallback is one that works until it is needed. Read by `_spawn_worker` for
#: BOTH worker languages.
_FORCE_FILE_PROTOCOL = False


class _TailReader:
    """`readline()` over a file another process appends to.

    A plain file object returns b"" at EOF rather than waiting, so it cannot
    stand in for a pipe — `_readline_timeout` expects a blocking readline. This
    polls, and gives up when the worker is gone so a dead worker leaves a
    thread that exits rather than one that spins.
    """

    def __init__(self, path: Path, proc: subprocess.Popen):
        self._path = path
        self._proc = proc
        self._offset = 0
        self._buf = b""

    def readline(self) -> bytes:
        import time as _time

        while True:
            try:
                with self._path.open("rb") as fh:
                    fh.seek(self._offset)
                    chunk = fh.read()
            except OSError:
                chunk = b""
            if chunk:
                self._offset += len(chunk)
                self._buf += chunk
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                return line + b"\n"
            if self._proc.poll() is not None and not chunk:
                return b""          # worker exited and wrote nothing more
            _time.sleep(0.005)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._path.unlink()


def _proto_pipe() -> tuple[int, int] | None:
    """A pipe the child inherits for protocol responses, or None if impossible.

    The child is told the descriptor NUMBER through the environment rather than
    having it dup2'd to a fixed 3. subprocess calls preexec_fn BEFORE closing
    inherited descriptors, so a dup2 there is undone a moment later —
    verified: fd 3 came back EBADF in the child. `pass_fds` keeps the original
    number open and inheritable, which is the supported way to do this.

    Windows has neither preexec_fn nor pass_fds, so no descriptor can be handed
    over and this returns None. That is NOT a fallback to stdout: the caller
    gives the worker a file to append to instead (`CODECALC_PROTO_PATH`, read
    back through `_TailReader`), which keeps fd 1 out of the protocol by a
    different route. `_FORCE_FILE_PROTOCOL` takes that same path on POSIX so the
    suite exercises it where Windows is not available to.
    """
    if os.name == "nt" or _FORCE_FILE_PROTOCOL:
        return None
    return os.pipe()


def _confinement_paths(workdir: Path, proto_file: Path | None) -> tuple[list[str], list[str]]:
    """(read_only, read_write) for a worker confined to its session workspace.

    Read-only covers what an interpreter needs in order to START: its own
    prefix, the loader and shared libraries, and the character devices that
    everything opens. It is deliberately a superset across layouts — a path
    that does not exist is skipped by `landlock.restrict_self` rather than
    being fatal, because refusing to confine at all because /opt is absent
    would be the worst outcome.

    The interpreter prefix is resolved from the RUNTIME path, not from
    `sys.prefix`. The server's own interpreter and the `python3` a worker
    execs are frequently not the same install (here: a mise-managed 3.14.6
    under /data), and granting the server's prefix would confine the worker
    out of its own stdlib.

    Read-write is the workspace and nothing else. That is why TMPDIR is
    repointed into the workspace instead of /tmp being granted: /tmp holds
    every other session's scratch files, and a confinement that leaves them
    readable has not confined the thing worth confining.
    """
    ro = [
        str(Path(__file__).resolve().parent),   # the bootstrap the worker execs
        "/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/opt",
        # platform.libc_ver() and os.cpu_count() read these; denying /proc
        # makes an interpreter fail in ways that name libc rather than the
        # sandbox, which is a diagnostic dead end.
        "/proc",
        "/dev/null", "/dev/zero", "/dev/urandom", "/dev/random",
    ]
    path = registry.runtime_path()
    for exe in ("python3", "node"):
        found = shutil.which(exe, path=path)
        if not found:
            continue
        real = Path(found).resolve()
        ro.append(str(real))
        # <prefix>/bin/python3 -> <prefix>. The stdlib, shared objects and (for
        # node) lib/node_modules all hang off it.
        if real.parent.name in ("bin", "Scripts"):
            ro.append(str(real.parent.parent))
    rw = [str(workdir)]
    if proto_file is not None:
        # The protocol file lives in the system temp dir, so it is granted as a
        # FILE. landlock.restrict_self narrows the mask for a non-directory;
        # passing the directory mask for a file returns EINVAL and takes the
        # whole ruleset down, leaving the worker unconfined.
        rw.append(str(proto_file))
    return ro, rw


def _worker_preexec(workdir: Path, proto_file: Path | None):
    """preexec_fn applying rlimits and then Landlock, or None where neither runs.

    Order matters only in that both must happen before exec; Landlock is
    applied last because it is irreversible and a failed setrlimit should not
    leave a confined-but-unlimited child.

    Failure to confine is FATAL here. `restrict_self` raising propagates out of
    preexec_fn, `Popen` raises in the parent, and `_spawn_worker` reports it —
    which is the whole point of the module: a worker that silently starts
    unconfined is the outcome this exists to remove. The caller still learns
    that confinement is unavailable rather than broken, because
    `landlock.available()` is checked before the spawn, not inside the child.
    """
    base = executor.session_worker_limits()
    if not landlock.available():
        return base
    ro, rw = _confinement_paths(workdir, proto_file)

    def _apply() -> None:
        if base is not None:
            base()
        landlock.restrict_self(read_write=rw, read_only=ro)

    return _apply


def _worker_env(workdir: Path) -> dict:
    """Worker environment with TMPDIR inside the workspace.

    `TMPDIR`/`TEMP`/`TMP` are already on the executor's env allowlist on both
    the Rust and Python sides, so this needs no parity change; `packages.py`
    already does the same thing for installs. Without it, confined session code
    calling `tempfile.mkstemp()` gets EACCES, because /tmp is not granted.
    """
    env = executor._env()
    tmp = workdir / ".tmp"
    with contextlib.suppress(OSError):
        tmp.mkdir(parents=True, exist_ok=True)
    for key in ("TMPDIR", "TEMP", "TMP"):
        env[key] = str(tmp)
    return env


def _spawn_worker(language: str, workdir: Path) -> tuple[Worker | None, str | None]:
    bootstrap = Path(__file__).resolve().with_name("_worker_bootstrap.py")
    # Only node needs an out-of-band pipe. The python worker keeps a private dup
    # of the original fd 1 taken before anything is redirected, which gives it
    # the same guarantee without an extra descriptor — except on Windows (or
    # forced), where the file-backed route below stands in for it, for the same
    # reason node needs one there: a subprocess can still write through the
    # process's inherited OS stdout HANDLE rather than the redirected CRT
    # descriptor the dup trick relies on.
    pipe = _proto_pipe() if language != "python3" else None
    proto = None
    try:
        if language == "python3":
            env = _worker_env(workdir)
            proto_file: Path | None = None
            if os.name == "nt" or _FORCE_FILE_PROTOCOL:
                # Same file-backed arrangement node uses on Windows (see the
                # `else` branch below): a path executed code cannot reach by
                # writing to fd 1, because on Windows the dup'd-fd-1 protocol
                # _worker_bootstrap.py otherwise relies on is not trustworthy.
                fd, name = tempfile.mkstemp(prefix="codecalc-proto-", suffix=".jsonl")
                os.close(fd)
                proto_file = Path(name)
                env["CODECALC_PROTO_PATH"] = str(proto_file)
            proc = subprocess.Popen(
                ["python3", "-u", str(bootstrap)],
                cwd=str(workdir),
                env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                preexec_fn=_worker_preexec(workdir, proto_file),
            )
            if proto_file is not None:
                proto = _TailReader(proto_file, proc)
        else:  # node
            env = _worker_env(workdir)
            proto_file: Path | None = None
            if pipe is not None:
                read_fd, write_fd = pipe
                env["CODECALC_PROTO_FD"] = str(write_fd)
                extra = {"pass_fds": (write_fd,)}
            else:
                # No pass_fds and no preexec_fn here (Windows), so the protocol
                # cannot be handed over as a descriptor. A file the worker
                # appends to is the portable equivalent: executed code writing
                # to fd 1 cannot reach it, which is the property that matters —
                # previously the protocol shared fd 1 and any child spawned with
                # inherited stdio corrupted it.
                fd, name = tempfile.mkstemp(prefix="codecalc-proto-", suffix=".jsonl")
                os.close(fd)
                proto_file = Path(name)
                env["CODECALC_PROTO_PATH"] = str(proto_file)
                extra = {}
            # Computed after the branch, because the ruleset has to grant the
            # protocol FILE when that is the channel — and which channel it is
            # is only settled above.
            pre = _worker_preexec(workdir, proto_file)
            if pre is not None:
                extra["preexec_fn"] = pre
            proc = subprocess.Popen(
                ["node", "-e", _WORKER_BOOTSTRAP_NODE],
                cwd=str(workdir),
                env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                **extra,
            )
            if pipe is not None:
                os.close(write_fd)
                proto = os.fdopen(read_fd, "rb")
                pipe = None          # ownership transferred to `proto`
            elif proto_file is not None:
                proto = _TailReader(proto_file, proc)
        w = Worker(language, proc, proto=proto)
        w.confined = landlock.available()
        # warm up: verify the worker answers before returning. Both failure
        # paths go through w.close(), which reaps the process group and closes
        # the protocol pipe — `proc.kill()` alone left the read end open on
        # every failed start, and nothing else was going to close it.
        try:
            r = w.run("", timeout=10)
        except Exception as exc:
            w.close()
            return None, f"worker did not answer the warm-up call: {exc}"
        if not r.get("ok") and r.get("stderr"):
            w.close()
            return None, f"worker failed on warm-up: {r['stderr'][:400]}"
        return w, None
    except Exception as exc:
        # The reason is returned rather than swallowed. A confinement allow-list
        # that is wrong makes `landlock_add_rule` fail inside preexec_fn, which
        # surfaces here as a spawn failure — and "failed to start python3 REPL
        # worker" with no cause is the diagnostic dead end this repo keeps
        # closing elsewhere (#62, #66, #80). Name what actually went wrong.
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        # If the pipe was created but never handed to a Worker, neither end has
        # an owner: leaking a read end here would eventually exhaust NOFILE.
        if pipe is not None:
            for fd in pipe:
                with contextlib.suppress(OSError):
                    os.close(fd)


_WORKER_BOOTSTRAP_NODE = r'''
const readline = require('readline');
const vm = require('vm');
const fs = require('fs');

// The protocol channel: an inherited pipe whose NUMBER the parent passes in the
// environment. Responses used to go to fd 1 via console.log, which any child
// spawned with stdio:'inherit' writes to as well. The number is not fixed at 3
// on purpose — subprocess runs preexec_fn before closing inherited descriptors,
// so a dup2 to a fixed number there is undone a moment later. When the parent
// cannot pass one at all (Windows), PROTO_FD is 1 and the echoed request id is
// what keeps a corrupted stream from being answered with a stale result.
const PROTO_FD = Number(process.env.CODECALC_PROTO_FD || 1);
// A path wins over a descriptor: it is used where the parent could not hand
// over an fd at all (Windows has neither pass_fds nor preexec_fn), and it is
// the only arrangement executed code cannot reach by writing to fd 1.
const PROTO_PATH = process.env.CODECALC_PROTO_PATH || '';
// The Buffer form with an EXPLICIT null position. `fs.writeSync(fd, string)`
// throws EINVAL on a pipe, because the string overload supplies a file position
// and a pipe is not seekable — verified, it killed the worker on the first
// response. The loop is because a pipe write can be partial.
function respond(obj) {
  const buf = Buffer.from(JSON.stringify(obj) + '\n', 'utf8');
  if (PROTO_PATH) { fs.appendFileSync(PROTO_PATH, buf); return; }
  let off = 0;
  while (off < buf.length) {
    off += fs.writeSync(PROTO_FD, buf, off, buf.length - off, null);
  }
}
const rl = readline.createInterface({ input: process.stdin });

// Persistent context: top-level let/const/var and globals survive across
// calls (runInNewContext would throw them away each time). Output is
// captured into a holder object reset before every call.
const holder = { stdout: '', stderr: '' };
const sb = {
  console: {
    log:   (...a) => { holder.stdout += a.map(String).join(' ') + '\n'; },
    info:  (...a) => { holder.stdout += a.map(String).join(' ') + '\n'; },
    warn:  (...a) => { holder.stderr += a.map(String).join(' ') + '\n'; },
    error: (...a) => { holder.stderr += a.map(String).join(' ') + '\n'; },
  },
  process: { stdout: { write: (s) => { holder.stdout += String(s); return true; } },
             stderr: { write: (s) => { holder.stderr += String(s); return true; } } },
  require, setTimeout, clearTimeout, setInterval, clearInterval,
};
sb.globalThis = sb;
const context = vm.createContext(sb);

// Same cap as the Rust executor and the python worker. A stateful session is
// not exempt from the output limit just because it keeps state.
const MAX_OUTPUT_BYTES = 64 * 1024;
function cap(s) {
  if (s.length <= MAX_OUTPUT_BYTES) return [s, false];
  return [s.slice(0, MAX_OUTPUT_BYTES) + '\n...[truncated]', true];
}

rl.on('line', (line) => {
  line = line.trim();
  if (!line) return;
  let req;
  try { req = JSON.parse(line); } catch { return; }
  holder.stdout = '';
  holder.stderr = '';
  let ok = true, err = '';
  try {
    // The caller's deadline, not a hardcoded 30s. `timeout: 30000` ignored the
    // timeout argument entirely, so a session call asking for 5s could burn 30
    // and one asking for 120s was cut off at 30.
    vm.runInContext(req.code, context, { timeout: Math.max(1, (req.timeout_ms | 0) || 30000) });
  } catch (e) {
    ok = false;
    err = (e && e.stack) ? e.stack : String(e);
  }
  const [out, outTrunc] = cap(holder.stdout);
  const [errOut, errTrunc] = cap(holder.stderr || err);
  // `id` is echoed so the reader can tell an answer to THIS request from a
  // stale one left in the pipe by an earlier corruption. Without it a desynced
  // stream returns a well-formed answer to a different question and no caller
  // can detect it.
  respond({
    id: req.id, ok, stdout: out, stderr: errOut,
    exit_code: ok ? 0 : 1,
    output_truncated: outTrunc || errTrunc,
    verdict: (outTrunc || errTrunc) ? 'OLE' : (ok ? 'OK' : 'RTE'),
  });
});
'''
