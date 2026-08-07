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

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from . import executor, registry

SESSION_ROOT = Path(os.environ.get("CODECALC_SESSION_ROOT", "~/.codecalc/sessions")).expanduser()

#: languages that get a stateful REPL worker (interpreters with exec()):
_WORKER_LANGS = {"python3", "node"}

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_lock = threading.Lock()
_workers: dict[str, Worker] = {}


def _session_dir(session_id: str) -> Path:
    if not _SAFE_NAME.match(session_id):
        raise ValueError("invalid session id")
    d = (SESSION_ROOT / session_id).resolve()
    # jail: must stay under SESSION_ROOT
    if not str(d).startswith(str(SESSION_ROOT.resolve())):
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
        return {
            "ok": True, "session_id": session_id, "language": name,
            "stateful": False, "workdir": str(d), "files": _list(d),
        }
    session_id = f"{name}-{uuid.uuid4().hex[:8]}"
    d = _session_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    w = _spawn_worker(name, d)
    if w is None:
        return {"ok": False, "error": f"failed to start {name} REPL worker"}
    with _lock:
        _workers[session_id] = w
    return {
        "ok": True, "session_id": session_id, "language": name,
        "stateful": True, "workdir": str(d), "files": _list(d),
    }


def stop(session_id: str) -> dict:
    """Kill the worker (if any) and delete the workspace."""
    with _lock:
        w = _workers.pop(session_id, None)
    if w is not None:
        w.close()
    d = _session_dir(session_id)
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True, "session_id": session_id, "deleted": True}


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


def execute(session_id: str, code: str, language: str | None = None,
            stdin: str = "", timeout: int = 30) -> dict:
    """Run code in a session. Stateful langs go to the REPL worker; the rest
    run as fresh processes in the session workdir."""
    d = _session_dir(session_id)
    if not d.is_dir():
        return {"ok": False, "error": f"unknown session '{session_id}'"}
    with _lock:
        w = _workers.get(session_id)
    if w is not None:
        if language and registry.canonical(language) != w.language:
            return {"ok": False, "error": f"session is {w.language}, not {language}"}
        return w.run(code, stdin=stdin, timeout=timeout)
    # workspace-only session: fresh process in the session dir
    lang = registry.canonical(language) if language else "python3"
    return executor.execute(lang, code, stdin=stdin, timeout=timeout, workdir=str(d))


def write_file(session_id: str, path: str, content: str) -> dict:
    d = _session_dir(session_id)
    if not d.is_dir():
        return {"ok": False, "error": f"unknown session '{session_id}'"}
    target = _jail(d, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return {"ok": True, "path": str(target.relative_to(d))}


def read_file(session_id: str, path: str, max_bytes: int = 64 * 1024) -> dict:
    d = _session_dir(session_id)
    if not d.is_dir():
        return {"ok": False, "error": f"unknown session '{session_id}'"}
    target = _jail(d, path)
    if not target.is_file():
        return {"ok": False, "error": f"no such file: {path}"}
    data = target.read_bytes()
    truncated = len(data) > max_bytes
    return {"ok": True, "path": path, "size": len(data),
            "content": data[:max_bytes].decode(errors="replace"),
            "truncated": truncated}


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


def _jail(d: Path, path: str) -> Path:
    """Resolve path under session dir; refuse escapes (.., absolute)."""
    p = (d / path).resolve()
    if not str(p).startswith(str(d.resolve())):
        raise ValueError("path escapes session workspace")
    return p


# ── REPL workers ───────────────────────────────────────────────────────────

def _readline_timeout(stream, timeout: float) -> str | None:
    """Read one line from a buffered reader with a wall-clock timeout.
    Returns None on timeout. Works on Unix (select on the fd)."""
    import select
    import time as _t
    deadline = _t.monotonic() + timeout
    buf = b""
    while _t.monotonic() < deadline:
        r, _, _ = select.select([stream], [], [], 0.1)
        if r:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                return buf.decode(errors="replace") if buf else ""
            buf += chunk
            if b"\n" in buf:
                line, _rest = buf.split(b"\n", 1)
                return line.decode(errors="replace")
    return None


class Worker:
    """Stateful interpreter: JSON-lines protocol on stdin/stdout. Globals
    persist across run() calls because exec() reuses one dict."""

    def __init__(self, language: str, proc: subprocess.Popen):
        self.language = language
        self.proc = proc
        self._wlock = threading.Lock()
        self._stderr_log: list[str] = []
        # drain stderr so a full pipe can never deadlock the worker
        self._drain = threading.Thread(target=self._drain_stderr, daemon=True)
        self._drain.start()

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                self._stderr_log.append(line.decode(errors="replace").rstrip())
                if len(self._stderr_log) > 200:
                    self._stderr_log.pop(0)
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
        req = json.dumps({"code": code, "stdin": stdin})
        with self._wlock:
            try:
                self.proc.stdin.write((req + "\n").encode())
                self.proc.stdin.flush()
                line = _readline_timeout(self.proc.stdout, timeout)
                if line is None:
                    # worker hung — kill it; the session is unusable
                    self.close()
                    return {"ok": False, "error": f"{self.language} worker timed out",
                            "verdict": "TLE"}
                if line == "":
                    return {"ok": False, "error": "worker closed", "verdict": "RTE"}
                out = json.loads(line)
            except (BrokenPipeError, json.JSONDecodeError, ValueError) as exc:
                return {"ok": False, "error": f"worker protocol error: {exc}",
                        "verdict": "RTE"}
        out.setdefault("session", True)
        return out

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


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


def _spawn_worker(language: str, workdir: Path) -> Worker | None:
    bootstrap = Path(__file__).resolve().with_name("_worker_bootstrap.py")
    try:
        if language == "python3":
            proc = subprocess.Popen(
                ["python3", "-u", str(bootstrap)],
                cwd=str(workdir),
                env=executor._env(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:  # node
            proc = subprocess.Popen(
                ["node", "-e", _WORKER_BOOTSTRAP_NODE],
                cwd=str(workdir),
                env=executor._env(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        w = Worker(language, proc)
        # warm up: verify the worker answers before returning
        try:
            r = w.run("", timeout=10)
        except Exception:
            proc.kill()
            return None
        if not r.get("ok") and r.get("stderr"):
            proc.kill()
            return None
        return w
    except Exception:
        return None


_WORKER_BOOTSTRAP_NODE = r'''
const readline = require('readline');
const vm = require('vm');
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

rl.on('line', (line) => {
  line = line.trim();
  if (!line) return;
  let req;
  try { req = JSON.parse(line); } catch { return; }
  holder.stdout = '';
  holder.stderr = '';
  let ok = true, err = '';
  try {
    vm.runInContext(req.code, context, { timeout: 30000 });
  } catch (e) {
    ok = false;
    err = (e && e.stack) ? e.stack : String(e);
  }
  console.log(JSON.stringify({ ok, stdout: holder.stdout, stderr: holder.stderr || err, exit_code: ok ? 0 : 1 }));
});
'''
