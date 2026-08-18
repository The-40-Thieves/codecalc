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

import inspect
import os
import pathlib
import re as _re
import shutil
import sys
import threading

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import errors, sessions


class _FakeClock:
    """Deterministic stand-in for `sessions._now`, so idle expiry is driven by
    explicit advances instead of real `time.sleep()`.

    The prior version aged sessions with real sleeps against tiny TTLs (0.05s
    TTL, 0.12s gaps): on a loaded CI runner an operation could overrun the TTL
    and a session that should have stayed alive got reaped — timing luck, not a
    logic bug (it passed on Linux and macos-py3.14, flaked on macos-py3.11).
    Advancing this clock is instantaneous and EXACT, so the under-TTL / over-TTL
    boundary each test asserts is decided the same way on every runner.

    `sessions._now` feeds ONLY the idle-expiry decision (last-activity stamp and
    the reap comparison), so substituting it leaves the real worker subprocess —
    spawn, execute, teardown, and the OS-level PID checks these tests turn on —
    running in real time and fully exercised. `advance()` never moves the clock
    backwards, preserving the monotonic contract the production clock has.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        assert seconds >= 0, "monotonic clock never moves backwards"
        self._t += seconds


# Install before any session activity below (WORKER_LANGS probing starts real
# workers): with no TTL set during probing, the clock is never consulted for a
# reap, but the seam must already point at the fake before the timed sections.
_CLOCK = _FakeClock()
sessions._now = _CLOCK

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
            _CLOCK.advance(0.3)
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
        _CLOCK.advance(0.3)  # idle past the 0.05s TTL configured below
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
            _CLOCK.advance(0.12)
            r1 = sessions.execute(s, MARKER[lang])
            _CLOCK.advance(0.12)
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
        _CLOCK.advance(0.15)
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
_before = {t.ident for t in threading.enumerate()}
for lang in WORKER_LANGS[:1]:
    def _run(lang=lang):
        s = sessions.start(lang)["session_id"]
        sessions.execute(s, MARKER[lang])
        sessions.stop(s)
    _with_ttl("5", _run)
_after = {t.ident for t in threading.enumerate()}
_leaked_nondaemon = [t for t in threading.enumerate()
                     if t.ident in (_after - _before) and not t.daemon]
check("idle-expiry machinery starts no non-daemon thread",
      not _leaked_nondaemon, f"-> {[t.name for t in _leaked_nondaemon]}")

# ── 6. fix-round-1 CRITICAL: concurrent execute() must never silently
# respawn on an already-expired session ─────────────────────────────────────
# Reviewer-reported race: the old code checked `if session_id in _EXPIRED`
# UNLOCKED, then separately re-acquired `_lock` to read `_workers.get(...)`.
# A concurrent reap landing in the window between those two reads left a
# caller believing BOTH "not yet expired" (its own stale first read) and "no
# worker present" (a fresh second read, after another thread's reap deleted
# it) — which fell through to the workspace-only branch and silently re-ran
# the call as a fresh, UNCONFINED process while reporting ok=True. Reviewer
# reproduced it live: PID confirmed dead, yet ok=True from a fresh process.
#
# TWO tests for this, deliberately not one. The window is a handful of
# bytecode instructions wide — empirically measured (10 trials x 60 real
# threads, staggered right across the TTL boundary, against the actual
# PRE-FIX code) at 0/600 reproductions. Real thread scheduling did not land
# in it reliably enough to serve as regression evidence on its own, which is
# exactly what the reviewer's "inject the reap ... via a test seam if
# needed" anticipated.
#
# 6a is the DECISIVE one: a structural assertion (this repo's own
# convention — see test_python_sweep.py's inspect.getsource checks on
# Worker.run) that the `_EXPIRED` membership check and the `_workers`
# read/reap live inside the SAME, SINGLE `with _lock:` block. That is the
# property that makes the race impossible BY CONSTRUCTION (mutual exclusion
# admits no window to land in, full stop — no timing luck required) — and
# it fails immediately and reliably if the two-separate-acquisitions shape
# is ever reintroduced, which a timing test cannot promise.
#
# 6b is the concurrency stress test kept alongside it: real threads, run for
# due diligence and to catch anything the structural check does not
# encode, but its own empirical failure-to-reproduce above is why it is not
# this fix's sole evidence.
_gsrc = inspect.getsource(sessions._get_worker_or_expired)
_esrc = inspect.getsource(sessions.execute)
# Line-anchored, real-indentation regex — NOT a plain substring count, which
# would also match this same docstring's own backtick-quoted prose mentions
# of "with _lock:" (it has several, describing the bug it fixed). Only a
# line that IS a `with _lock:` statement in the actual code counts.
_lock_stmts = [m.start() for m in _re.finditer(r"^\s*with _lock:", _gsrc, _re.M)]
check("6a. _get_worker_or_expired: the EXPIRED check and the worker "
      "read/reap share exactly ONE `with _lock:` STATEMENT (no second "
      "acquisition) — docstring mentions of the phrase excluded",
      len(_lock_stmts) == 1, f"-> {len(_lock_stmts)} statement(s)")
_lock_pos = _lock_stmts[0]
# Searched STARTING FROM _lock_pos: the docstring above also quotes both
# phrases in prose (describing the bug), so an unanchored `.index()` would
# find those mentions instead of the real code.
_expired_pos = _gsrc.index("if session_id in _EXPIRED", _lock_pos)
_reap_pos = _gsrc.index("_maybe_reap_locked(session_id)", _lock_pos)
check("6a. ...the _EXPIRED check is textually INSIDE that lock (after it opens)",
      _expired_pos > _lock_pos)
check("6a. ...the worker reap is ALSO textually inside the SAME lock, "
      "not a separate one",
      _reap_pos > _lock_pos
      and not any(pos > _lock_pos and pos < _reap_pos for pos in _lock_stmts[1:]))
check("6a. execute() no longer reads _EXPIRED directly at all — it must go "
      "through the atomic helper, not repeat the check itself",
      "_EXPIRED" not in _esrc, f"-> execute() source mentions _EXPIRED: "
      f"{'_EXPIRED' in _esrc}")
check("6a. execute() calls the atomic helper for its worker-or-expired decision",
      "_get_worker_or_expired(" in _esrc)

# 6b — best-effort concurrency stress, real threads, against a session that
# is ALREADY idle-expired before any of them fire.
for lang in WORKER_LANGS[:1]:
    def _run(lang=lang):
        s = sessions.start(lang)["session_id"]
        pid = sessions._workers[s].proc.pid
        _CLOCK.advance(0.15)  # idle past the 0.05s TTL configured below

        n = 40
        barrier = threading.Barrier(n)
        results: list[dict | None] = [None] * n

        def _fire(i: int) -> None:
            barrier.wait()
            results[i] = sessions.execute(s, MARKER[lang])

        threads = [threading.Thread(target=_fire, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        respawned = [r for r in results
                     if r is not None and r.get("code") != errors.WORKER_FAILURE]
        check(f"6b. {lang}: {n} concurrent calls on an expired session NEVER "
              f"silently respawn (every one must be the expired error)",
              not respawned,
              f"-> {len(respawned)}/{n} were NOT the expired error, "
              f"e.g. {respawned[0] if respawned else None}")
        check(f"6b. {lang}: ...and the worker process is (still) actually gone",
              _pid_gone(pid))
        sessions.stop(s)
    _with_ttl("0.05", _run)

# ── 7. IMPORTANT fix-round-1: _EXPIRED is bounded, not a permanent leak ────
# "abandoned sessions are precisely the ones that never get session_stop" —
# so nothing ever removes their entry from an unbounded in-memory set. The
# fix caps the in-memory cache and persists a durable marker FILE in each
# session's own workspace on reap, so an EVICTED cache entry still resolves
# correctly (falls back to a stat() on the marker) instead of reopening the
# exact silent-respawn hole test 6 above just closed. `_EXPIRED_CAP` is
# monkeypatched down to a small number so this is fast and deterministic
# rather than needing hundreds of real sessions.
_orig_cap = sessions._EXPIRED_CAP
sessions._EXPIRED_CAP = 2
try:
    for lang in WORKER_LANGS[:1]:
        def _run(lang=lang):
            evicted_sid = sessions.start(lang)["session_id"]
            marker_path = sessions._session_dir(evicted_sid) / sessions._EXPIRED_MARKER_NAME
            _CLOCK.advance(0.15)
            # A call on it reaps it (test 2's mechanism) — this is the entry
            # that gets evicted once enough OTHER sessions expire after it.
            sessions.execute(evicted_sid, MARKER[lang])
            check(f"{lang}: reap wrote a durable marker into the session workspace",
                  marker_path.is_file(), f"-> {marker_path}")
            check(f"{lang}: the in-memory cache holds the id right after reaping",
                  evicted_sid in sessions._EXPIRED)

            # Push it out of the (now tiny) in-memory cache with more expiries.
            # Deliberately NOT session_stop()-ed yet: stop() itself discards a
            # session's own _EXPIRED entry (a torn-down session has nothing
            # left to remember), which would clean each filler up before it
            # ever got a chance to evict the original — exactly the
            # "abandoned sessions never call session_stop" shape this bound
            # exists for. They are all stopped together, below, once the
            # eviction this loop is meant to cause has been asserted.
            filler_sids = []
            for _ in range(sessions._EXPIRED_CAP + 1):
                sid = sessions.start(lang)["session_id"]
                _CLOCK.advance(0.15)
                sessions.execute(sid, MARKER[lang])
                filler_sids.append(sid)

            check(f"{lang}: the ORIGINAL id was evicted from the bounded in-memory cache",
                  evicted_sid not in sessions._EXPIRED,
                  f"-> cache size {len(sessions._EXPIRED)} (cap {sessions._EXPIRED_CAP})")
            check(f"{lang}: cache size never exceeds the configured cap",
                  len(sessions._EXPIRED) <= sessions._EXPIRED_CAP,
                  f"-> {len(sessions._EXPIRED)}")

            # A call on the EVICTED id must still be refused as expired — the
            # marker file, not the (now-missing) in-memory entry, is what
            # answers this. Without it, this would fall through to the
            # workspace-only branch: the same bug as test 6, reopened by
            # memory pressure instead of a thread race.
            r = sessions.execute(evicted_sid, MARKER[lang])
            check(f"{lang}: a call on an EVICTED-but-still-expired session is still refused",
                  r.get("ok") is False and r.get("code") == errors.WORKER_FAILURE,
                  f"-> {r}")
            sessions.stop(evicted_sid)
            for sid in filler_sids:
                sessions.stop(sid)
        _with_ttl("0.05", _run)
finally:
    sessions._EXPIRED_CAP = _orig_cap

# ── 8. EVERY session entry point is activity, not just execute() ───────────
# The first version updated the clock in `execute()` alone, so a session
# being actively used through session_write_file / session_files /
# session_read_file / session_artifacts aged out and had its worker reaped
# underneath a caller that had never stopped using it — while the README
# described the knob as reaping a session "untouched for longer than this".
# Each touch point below is exercised on its own: two waits that each sit
# under the TTL but together exceed it, with only that one call in between.
# Pre-fix every one of these reaped; only the `execute` control passed.
_TOUCH_POINTS = [
    ("write_file", lambda s: sessions.write_file(s, "staged.txt", "x")),
    ("list_files", lambda s: sessions.list_files(s)),
    ("resource_read", lambda s: sessions.resource_read(s, "staged.txt")),
    ("artifacts", lambda s: sessions.artifacts(s)),
    # The control: the ONE entry point that always counted. If this ever
    # fails, the timings below are wrong and the others prove nothing.
    ("execute", lambda s: sessions.execute(s, "1")),
]
for lang in WORKER_LANGS[:1]:
    for _name, _touch_call in _TOUCH_POINTS:
        def _run(lang=lang, _name=_name, _touch_call=_touch_call):
            s = sessions.start(lang)["session_id"]
            try:
                # `resource_read` needs something to read; staging it here
                # rather than inside the timed window keeps every touch point
                # on the same clock.
                sessions.write_file(s, "staged.txt", "x")
                pid = sessions._workers[s].proc.pid
                _CLOCK.advance(0.12)
                _touch_call(s)
                _CLOCK.advance(0.12)
                r = sessions.execute(s, MARKER[lang])
                check(f"{lang}: {_name}() counts as activity — the session survives "
                      f"0.24s of use under a 0.2s TTL",
                      r.get("ok") is True, f"-> {r}")
                check(f"{lang}: ...and {_name}() left the worker process alive",
                      not _pid_gone(pid))
            finally:
                sessions.stop(s)
        _with_ttl("0.2", _run)

# ── 9. F4: a non-execute touch AFTER expiry must NOT revive the worker ─────
# The mirror of test 8. There the touch lands BEFORE the TTL elapses and keeps
# the session alive. Here the worker has ALREADY sat idle past the TTL when the
# touch arrives: it must be REAPED (the documented "reaped on next access"),
# not revived by a bare workspace touch that refreshes the idle clock. Pre-fix,
# write_file / list_files / resource_read / artifacts called `_note_activity()`
# directly, refreshing the clock — so the worker survived and the NEXT execute()
# ran in the ORIGINAL worker instead of returning the expiry error. Only test 8
# covered access-before-expiry; this covers access-after.
_AFTER_EXPIRY_TOUCHES = [
    ("write_file", lambda s: sessions.write_file(s, "staged2.txt", "y")),
    ("list_files", lambda s: sessions.list_files(s)),
    ("resource_read", lambda s: sessions.resource_read(s, "staged.txt")),
    ("artifacts", lambda s: sessions.artifacts(s)),
]
for lang in WORKER_LANGS[:1]:
    for _name, _touch_call in _AFTER_EXPIRY_TOUCHES:
        def _run(lang=lang, _name=_name, _touch_call=_touch_call):
            s = sessions.start(lang)["session_id"]
            try:
                sessions.write_file(s, "staged.txt", "x")
                pid = sessions._workers[s].proc.pid
                _CLOCK.advance(0.10)  # idle past the 0.05s TTL configured below
                _touch_call(s)    # a touch AFTER expiry
                gone = _pid_gone(pid)
                check(f"{lang}: {_name}() after expiry REAPS the idle worker "
                      f"instead of reviving it",
                      gone, f"-> pid {pid} {'gone' if gone else 'still alive'}")
                r = sessions.execute(s, MARKER[lang])
                check(f"{lang}: after {_name}() the next execute() gets the expiry "
                      f"error, not a run in the original worker",
                      r.get("ok") is False and r.get("code") == errors.WORKER_FAILURE,
                      f"-> {r}")
            finally:
                sessions.stop(s)
        _with_ttl("0.05", _run)

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== SESSION IDLE-EXPIRY REAPS THE WORKER, NOT JUST THE BOOKKEEPING ===")
sys.exit(1 if FAILS else 0)
