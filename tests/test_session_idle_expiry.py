"""Idle-expiry for abandoned stateful sessions (THE-779 residual).

"the one operational gap: a leaked session holds a worker process forever."
`session_start` on python3/node spawns a real subprocess (see sessions.py's
`Worker`) that lives until `session_stop` calls `Worker.close()`. Nothing
called that automatically, so a caller that forgot — or crashed before it
could — left the worker running for the life of the server.

`CODECALC_SESSION_IDLE_TTL_SECONDS` closes that. UNSET (default) is
unchanged behaviour — assert 1. SET makes an idle session's worker get
reaped on the next access to ANY session (the "lazy check-on-access"
design — no background thread, so nothing here can leak a thread that
outlives the process the way the vault watcher's `unref()` bug did on the
Node side of this same repo's history).

Reaping reuses `Worker.close()` — "the existing teardown path", no new kill
logic — so what these tests actually assert is that the OS-level PROCESS is
gone, not just that a dict entry was dropped: a reap that flipped a flag
without touching the process would pass a bookkeeping-only assertion and
leave the leak this ticket exists to close.

Both worker languages are covered independently: python3 and node have
separate bootstraps (sessions.py's own module docstring) and every defect
found in one so far has needed checking against the other.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import errors, sessions

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
        skip(f"{_lang} idle-expiry", "no working stateful worker on this platform")

MARKER = {"python3": "print('alive')", "node": "console.log('alive')"}


def _pid_gone(pid: int) -> bool:
    """OS-level: is `pid` actually gone, not just absent from our bookkeeping?"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # exists, owned by someone else — should not happen here
    return False


def _with_ttl(seconds: str | None, fn):
    old = os.environ.get(sessions.IDLE_TTL_ENV)
    if seconds is None:
        os.environ.pop(sessions.IDLE_TTL_ENV, None)
    else:
        os.environ[sessions.IDLE_TTL_ENV] = seconds
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop(sessions.IDLE_TTL_ENV, None)
        else:
            os.environ[sessions.IDLE_TTL_ENV] = old


# ── 1. UNSET = today's behaviour: no expiry, ever ───────────────────────────
for lang in WORKER_LANGS:
    def _run(lang=lang):
        s = sessions.start(lang)["session_id"]
        try:
            pid = sessions._workers[s].proc.pid
            time.sleep(0.3)
            r = sessions.execute(s, MARKER[lang])
            check(f"{lang}: unset TTL never expires an idle session",
                  r.get("ok") is True, f"-> {r}")
            check(f"{lang}: unset TTL: the worker process is still running",
                  not _pid_gone(pid))
        finally:
            sessions.stop(s)
    _with_ttl(None, _run)

# ── 2. SET: an idle session's worker is reaped on the NEXT access ──────────
for lang in WORKER_LANGS:
    def _run(lang=lang):
        s = sessions.start(lang)["session_id"]
        pid = sessions._workers[s].proc.pid
        check(f"{lang}: worker process exists right after session_start",
              not _pid_gone(pid))
        time.sleep(0.3)  # idle past the 0.05s TTL configured below
        try:
            r = sessions.execute(s, MARKER[lang])
            check(f"{lang}: a call on an expired session gets ok=False, not a hang",
                  r.get("ok") is False, f"-> {r}")
            check(f"{lang}: the error carries the stable WORKER_FAILURE code",
                  r.get("code") == errors.WORKER_FAILURE, f"-> {r.get('code')}")
            check(f"{lang}: expiry is NOT a silent respawn "
                  f"(no verdict claiming the code above actually ran)",
                  "verdict" not in r or r.get("verdict") != "OK", f"-> {r}")
            gone = _pid_gone(pid)
            check(f"{lang}: the worker PROCESS is actually gone (not just the dict entry)",
                  gone, f"-> pid {pid} {'gone' if gone else 'still alive'}")
        finally:
            sessions.stop(s)
    _with_ttl("0.05", _run)

# ── 3. activity resets the clock ────────────────────────────────────────────
for lang in WORKER_LANGS:
    def _run(lang=lang):
        s = sessions.start(lang)["session_id"]
        try:
            pid = sessions._workers[s].proc.pid
            # Two calls, each well under the TTL, spanning MORE than the TTL
            # in total — if activity did not reset the clock, this would
            # still expire.
            time.sleep(0.12)
            r1 = sessions.execute(s, MARKER[lang])
            time.sleep(0.12)
            r2 = sessions.execute(s, MARKER[lang])
            check(f"{lang}: activity resets the idle clock (first call still succeeds)",
                  r1.get("ok") is True, f"-> {r1}")
            check(f"{lang}: activity resets the idle clock (second call still succeeds)",
                  r2.get("ok") is True, f"-> {r2}")
            check(f"{lang}: ...and the worker was never reaped in between",
                  not _pid_gone(pid))
        finally:
            sessions.stop(s)
    _with_ttl("0.2", _run)

# ── 4. sweep hook: an explicit reap that needs no per-call trigger ─────────
for lang in WORKER_LANGS[:1]:
    def _run(lang=lang):
        s = sessions.start(lang)["session_id"]
        pid = sessions._workers[s].proc.pid
        time.sleep(0.15)
        reaped = sessions.sweep_idle_sessions()
        check(f"{lang}: sweep_idle_sessions() reaps an idle worker directly",
              s in reaped, f"-> {reaped}")
        check(f"{lang}: ...and the process is gone",
              _pid_gone(pid))
        sessions.stop(s)
    _with_ttl("0.05", _run)

# ── 5. no background thread survives the idle-expiry machinery ─────────────
# The repo's own history: fs.watch's unref() did not release a recursive
# watcher and kept the event loop alive. The Python equivalent this file
# guards against is a Timer/Thread started per-session that is not daemonic
# — that would keep the interpreter alive even after every session is
# stopped. Lazy-on-access needs no such thread at all.
import threading as _threading

_before = {t.ident for t in _threading.enumerate()}
for lang in WORKER_LANGS[:1]:
    def _run(lang=lang):
        s = sessions.start(lang)["session_id"]
        sessions.execute(s, MARKER[lang])
        sessions.stop(s)
    _with_ttl("5", _run)
_after = {t.ident for t in _threading.enumerate()}
_leaked_nondaemon = [t for t in _threading.enumerate()
                     if t.ident in (_after - _before) and not t.daemon]
check("idle-expiry machinery starts no non-daemon thread",
      not _leaked_nondaemon, f"-> {[t.name for t in _leaked_nondaemon]}")

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== SESSION IDLE-EXPIRY REAPS THE WORKER, NOT JUST THE BOOKKEEPING ===")
sys.exit(1 if FAILS else 0)
