"""`codecalc status` / `codecalc cleanup` + audit-log rotation.

W3a (merged) gave sessions a per-session/global disk quota and the
on-disk `.codecalc-session-expired` idle-expiry marker — but nothing outside
a running server could SEE either, and nothing reclaimed what an abandoned
session left behind. This is the operational half: `codecalc status` (a
read-only snapshot) and `codecalc cleanup` (the mutating reclaimer), plus
size-based rotation for the audit log so it does not grow unbounded either.

`cleanup` is the dangerous half — it `rm`s directories a SEPARATE CLI process
did not create, with none of sessions.py's in-memory bookkeeping to consult.
Most of this file is therefore SAFETY tests: dry-run removes nothing; write
removes ONLY a directory carrying the on-disk expiry marker by default (the
marker-less, age-based path is opt-in via `include_unmarked=`); a symlink at
SESSION_ROOT's top level is refused, never followed; anything outside
SESSION_ROOT — sibling directories, a symlink's target — is untouched
regardless of what is inside it; a directory touched within the last few
minutes is never removed; and — fix-round CRITICAL, sections 4b/4c below —
directory MTIME is NOT trusted as liveness: a session whose worker is
GENUINELY alive is refused via its on-disk pid lockfile even when its
directory looks old and abandoned by every other signal, which an
adversarial review proved was not true of an earlier version of this file.

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import audit as audit_module
from codecalc import ops, sessions

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── isolation: every test gets its own SESSION_ROOT, never the real one ────
@contextlib.contextmanager
def _isolated_root():
    """Monkeypatch `sessions.SESSION_ROOT` to a fresh temp dir for the
    duration of the block, restoring it after. `ops.py` reads
    `sessions.SESSION_ROOT` as a module attribute (never a locally-bound
    copy), so patching the attribute on the `sessions` module is enough for
    every function in both modules to see the swap — the same technique
    this repo's other tests use for module-level config.
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-ops-"))
    old_root = sessions.SESSION_ROOT
    sessions.SESSION_ROOT = tmp / "sessions"  # deliberately NOT pre-created
    # Also isolate the audit log: status_report() reads audit.from_env(),
    # which defaults to the REAL ~/.codecalc/audit/audit.log — a machine
    # that has ever run codecalc for real already has one, and asserting
    # "status creates nothing" against that shared file would be testing
    # this box's history, not this module's behaviour.
    old_audit_env = os.environ.get("CODECALC_AUDIT_LOG")
    os.environ["CODECALC_AUDIT_LOG"] = str(tmp / "audit" / "audit.log")
    try:
        yield sessions.SESSION_ROOT, tmp
    finally:
        sessions.SESSION_ROOT = old_root
        if old_audit_env is None:
            os.environ.pop("CODECALC_AUDIT_LOG", None)
        else:
            os.environ["CODECALC_AUDIT_LOG"] = old_audit_env


def _mkdir_session(root: pathlib.Path, name: str, *, content: bytes = b"",
                   marker: bool = False, age_seconds: float | None = None) -> pathlib.Path:
    """Build a fake session directory directly on disk (no sessions.start()
    — these tests are about a directory tree cleanup() finds cold, exactly
    as a separate CLI process would).

    Both files are written with `write_bytes`, never `write_text`: THE-898
    Windows CI failed here first — `write_text`'s default text-mode newline
    translation turns an embedded `\\n` into `\\r\\n` on Windows, so a marker
    written with `write_text("reaped (test)\\n")` is 15 bytes there and 14
    everywhere else. `write_bytes` never translates, so the marker's
    on-disk size is the same fixed number of bytes on every platform.
    """
    d = root / name
    d.mkdir(parents=True)
    if content:
        (d / "data.bin").write_bytes(content)
    if marker:
        (d / sessions._EXPIRED_MARKER_NAME).write_bytes(b"reaped (test)\n")
    if age_seconds is not None:
        stamp = time.time() - age_seconds
        for p in [d, *d.rglob("*")]:
            os.utime(p, (stamp, stamp))
    return d


def _dir_actual_bytes(d: pathlib.Path) -> int:
    """Sum the ACTUAL on-disk `stat().st_size` of every regular file under
    `d` — ground truth for a byte-count assertion below, computed here
    independently of `sessions._iter_regular_files`/`_session_dir_size`
    (even though it walks the same tree) so the assertion is a real check
    against the filesystem rather than the code checking itself.

    Call this BEFORE any assertion that reads it, and before `cleanup`
    might have removed the directory it names — there is nothing left to
    stat afterwards. Never assume a size from the length of a Python
    string/bytes object handed to a write call instead: see
    `_mkdir_session`'s own docstring for why that assumption broke on
    Windows CI.
    """
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file())


# ── 1. status(): read-only, creates nothing ─────────────────────────────────
with _isolated_root() as (root, tmp):
    rep = ops.status_report()
    check("status on an empty/nonexistent SESSION_ROOT reports 0 sessions",
          rep["ok"] is True and rep["session_count"] == 0)
    check("status does NOT create SESSION_ROOT as a side effect",
          not root.exists(), f"-> {root} exists={root.exists()}")
    check("status reports no idle-expired sessions",
          rep["idle_expired_session_ids"] == [])
    check("status's audit_log.path is set even though nothing was written yet",
          rep["audit_log"]["path"] is not None)
    check("status does not create the audit log file (create-on-write)",
          rep["audit_log"]["size_bytes"] is None)
    check("status reports the configured quota limits",
          rep["quota"]["session_disk_quota_mb"] > 0 and rep["quota"]["total_disk_quota_mb"] > 0)
    check("status carries a runtime-tier summary",
          set(rep["runtime_tier_summary"]) >= {"tested", "best_effort", "plan_only"})


# ── 2. status(): real sessions, correct usage + idle-expired classification ─
with _isolated_root() as (root, tmp):
    root.mkdir(parents=True)
    fresh = _mkdir_session(root, "python3-freshaaa", content=b"x" * 100)
    old = _mkdir_session(root, "python3-oldbbb", content=b"y" * 250, marker=True,
                         age_seconds=2 * 86400)
    # Ground truth from the ACTUAL on-disk sizes — see _dir_actual_bytes'
    # docstring for why this must never be assumed from content length.
    fresh_bytes = _dir_actual_bytes(fresh)
    old_bytes = _dir_actual_bytes(old)
    rep = ops.status_report()
    check("status counts both session directories",
          rep["session_count"] == 2, f"-> {rep['session_count']}")
    by_id = {s["session_id"]: s for s in rep["sessions"]}
    check("the fresh session's usage matches its actual bytes on disk",
          by_id["python3-freshaaa"]["usage_bytes"] == fresh_bytes,
          f"-> {by_id['python3-freshaaa']['usage_bytes']} vs {fresh_bytes}")
    check("the marked session's usage matches its actual bytes on disk "
          "(data.bin + the marker file itself, both real files)",
          by_id["python3-oldbbb"]["usage_bytes"] == old_bytes,
          f"-> {by_id['python3-oldbbb']['usage_bytes']} vs {old_bytes}")
    check("only the marked session is reported idle-expired",
          rep["idle_expired_session_ids"] == ["python3-oldbbb"])
    check("the fresh session is NOT reported idle-expired",
          by_id["python3-freshaaa"]["idle_expired"] is False)
    check("global_usage_bytes sums both sessions",
          rep["global_usage_bytes"] == fresh_bytes + old_bytes,
          f"-> {rep['global_usage_bytes']}")


# ── 3. cleanup(dry-run): removes NOTHING, lists the expired dir ────────────
with _isolated_root() as (root, tmp):
    root.mkdir(parents=True)
    fresh = _mkdir_session(root, "python3-freshaaa", content=b"a" * 10)
    old = _mkdir_session(root, "python3-oldbbb", content=b"b" * 512, marker=True,
                         age_seconds=2 * 86400)
    outside_target = tmp / "outside-target"
    outside_target.mkdir()
    (outside_target / "secret.txt").write_text("must never be touched", encoding="utf-8")
    symlink = root / "evil-symlink"
    symlink.symlink_to(outside_target, target_is_directory=True)
    outside_sibling = tmp / "outside-sibling-with-marker"
    _mkdir_session(tmp, "outside-sibling-with-marker", content=b"z" * 999,
                   marker=True, age_seconds=3 * 86400)
    old_bytes = _dir_actual_bytes(old)  # ground truth, computed from real stat() sizes

    rep = ops.cleanup(write=False)
    check("dry-run reports write=False", rep["write"] is False)
    check("dry-run: the fresh session dir still exists", fresh.is_dir())
    check("dry-run: the expired session dir still exists (nothing removed)", old.is_dir())
    check("dry-run: the symlink itself still exists, unfollowed",
          symlink.is_symlink())
    check("dry-run: the symlink's target directory is untouched",
          (outside_target / "secret.txt").is_file())
    check("dry-run: a sibling OUTSIDE SESSION_ROOT is never even considered",
          outside_sibling.is_dir() and (outside_sibling / sessions._EXPIRED_MARKER_NAME).is_file())
    check("dry-run: total_reclaimed_bytes is 0 (nothing removed yet)",
          rep["total_reclaimed_bytes"] == 0)
    check("dry-run: exactly one eligible candidate — the expired dir",
          rep["eligible_count"] == 1, f"-> {rep['eligible_count']}")
    check("dry-run: the eligible candidate's size matches the expired dir's bytes "
          "(data.bin + the marker file)",
          rep["total_candidate_bytes"] == old_bytes,
          f"-> {rep['total_candidate_bytes']} vs {old_bytes}")
    by_id = {c["session_id"]: c for c in rep["candidates"]}
    check("the fresh dir is reported ineligible (active-session recency floor)",
          by_id["python3-freshaaa"]["eligible"] is False)
    check("the expired dir is reported eligible with the marker reason",
          by_id["python3-oldbbb"]["eligible"] is True
          and "marker" in by_id["python3-oldbbb"]["reason"])
    check("the symlink is reported ineligible, refused rather than followed",
          by_id["evil-symlink"]["eligible"] is False
          and "symlink" in by_id["evil-symlink"]["reason"])
    check("the OUTSIDE sibling never appears in candidates at all",
          "outside-sibling-with-marker" not in by_id)


# ── 4. cleanup(write=True): removes ONLY the expired dir ───────────────────
with _isolated_root() as (root, tmp):
    root.mkdir(parents=True)
    fresh = _mkdir_session(root, "python3-freshaaa", content=b"a" * 10)
    old = _mkdir_session(root, "python3-oldbbb", content=b"b" * 512, marker=True,
                         age_seconds=2 * 86400)
    outside_target = tmp / "outside-target"
    outside_target.mkdir()
    (outside_target / "secret.txt").write_text("must never be touched", encoding="utf-8")
    symlink = root / "evil-symlink"
    symlink.symlink_to(outside_target, target_is_directory=True)
    # recency floor overrides the marker: touched just now, must survive
    recent_but_marked = _mkdir_session(root, "python3-recentccc", content=b"c" * 77,
                                       marker=True)
    old_bytes = _dir_actual_bytes(old)  # ground truth, BEFORE removal

    rep = ops.cleanup(write=True)
    check("write=True reports write=True", rep["write"] is True)
    check("--write removed the expired dir", not old.exists(), f"-> {old} exists={old.exists()}")
    check("--write left the fresh dir untouched", fresh.is_dir())
    check("--write left the symlink itself untouched (still a symlink)",
          symlink.is_symlink())
    check("--write left the symlink's OUTSIDE target untouched",
          (outside_target / "secret.txt").is_file())
    check("--write left a RECENTLY-touched dir alone even though it carries the marker "
          "(active-session recency floor beats the marker)",
          recent_but_marked.is_dir())
    check("--write reports exactly one removed session",
          [r["session_id"] for r in rep["removed"]] == ["python3-oldbbb"],
          f"-> {rep['removed']}")
    check("--write reports the correct bytes reclaimed",
          rep["total_reclaimed_bytes"] == old_bytes,
          f"-> {rep['total_reclaimed_bytes']} vs {old_bytes}")
    check("--write reports no removal errors on a clean run",
          rep["removal_errors"] == [])
    check("SESSION_ROOT itself still exists after cleanup",
          root.is_dir())


# ── 4b. CRITICAL: a REAL live-pid lock beats a stale directory mtime ─
# Direct reproduction of the adversarial-review finding: a directory whose
# mtime looks abandoned (backdated, exactly the shape a genuinely-idle
# in-memory-only worker leaves) must NOT be removed while its liveness
# lockfile names a pid that is actually still running — and must become
# removable the moment that pid is confirmed dead. A REAL child process
# stands in for "a live codecalc server", not a guessed pid number, so this
# is a genuine repro rather than a mocked assertion.
with _isolated_root() as (root, tmp):
    root.mkdir(parents=True)
    _stamp = time.time() - 2 * 86400
    live_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        locked_dir = _mkdir_session(root, "python3-liveworker", content=b"s" * 40,
                                    age_seconds=2 * 86400)  # stale mtime — the exact bug shape
        (locked_dir / sessions._LOCK_FILE_NAME).write_bytes(str(live_proc.pid).encode("ascii"))
        # Writing the LOCK FILE for the first time creates a NEW directory
        # entry, which — unlike an in-place overwrite — DOES bump the parent
        # directory's mtime (exactly the asymmetry this whole fix is about).
        # Re-backdate after it so the directory is stale by the time cleanup
        # looks at it, matching the real bug shape rather than accidentally
        # defeating the recency floor with this test's own setup.
        os.utime(locked_dir, (_stamp, _stamp))

        check("sessions._lock_owner_alive() reports the REAL live pid as alive",
              sessions._lock_owner_alive(locked_dir) is True)

        dry = ops.cleanup(write=False, include_unmarked=True)  # the most permissive mode
        by_id = {c["session_id"]: c for c in dry["candidates"]}
        check("CRITICAL repro: a stale-mtime dir with a LIVE-pid lock is NOT eligible "
              "even under --include-unmarked",
              by_id["python3-liveworker"]["eligible"] is False
              and "live" in by_id["python3-liveworker"]["reason"],
              f"-> {by_id.get('python3-liveworker')}")

        write_rep = ops.cleanup(write=True, include_unmarked=True)
        check("CRITICAL repro: --write --include-unmarked does NOT delete a "
              "genuinely-live session's workspace",
              locked_dir.is_dir())
        check("...and it is not reported removed",
              "python3-liveworker" not in
              [r["session_id"] for r in write_rep["removed"]])
    finally:
        live_proc.terminate()
        live_proc.wait(timeout=10)

    # Same directory, same stale mtime — but now the lock names a pid this
    # test KNOWS is dead (the process above, confirmed exited via wait()
    # above). Nothing else about the directory changed.
    dead_pid = live_proc.pid
    (locked_dir / sessions._LOCK_FILE_NAME).write_bytes(str(dead_pid).encode("ascii"))
    # This second write OVERWRITES an existing filename (O_TRUNC in place),
    # which should NOT bump the directory's mtime — but re-backdate anyway
    # rather than lean on that assumption holding identically across every
    # filesystem this suite runs on; the point of this section is the lock
    # check, not a second proof of the mtime asymmetry.
    os.utime(locked_dir, (_stamp, _stamp))
    check("sessions._lock_owner_alive() reports a STALE (dead-pid) lock as not alive",
          sessions._lock_owner_alive(locked_dir) is False)

    dry2 = ops.cleanup(write=False, include_unmarked=True)
    by_id2 = {c["session_id"]: c for c in dry2["candidates"]}
    check("a stale (dead-pid) lock does NOT block eligibility",
          by_id2["python3-liveworker"]["eligible"] is True,
          f"-> {by_id2.get('python3-liveworker')}")

    write_rep2 = ops.cleanup(write=True, include_unmarked=True)
    check("once the lock's pid is confirmed dead, --write --include-unmarked "
          "DOES reclaim the directory",
          not locked_dir.exists())


# ── 4c. CRITICAL, integration: sessions.start() writes/removes the
# lock for real, and cleanup honours it end to end (no manually-crafted
# lockfile — this exercises the actual _write_lock_file/_remove_lock_file
# call sites in sessions.py: start()'s worker branch, and stop()).
with _isolated_root() as (root, tmp):
    started = sessions.start("python3")
    check("sessions.start('python3') succeeds (needed for this integration check)",
          started.get("ok") is True, f"-> {started}")
    if started.get("ok"):
        sid = started["session_id"]
        try:
            d = sessions._session_dir(sid)
            lock_path = d / sessions._LOCK_FILE_NAME
            check("a WORKER session gets a liveness lockfile at start()",
                  lock_path.is_file())
            check("the lockfile names THIS process's own pid (the server)",
                  lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()))
            check("sessions._lock_owner_alive() reports this session's worker as live",
                  sessions._lock_owner_alive(d) is True)

            # Reproduce the bug shape directly: backdate the DIRECTORY (never
            # touched by pure in-memory worker activity) while the worker
            # itself is genuinely still running.
            stamp = time.time() - 2 * 86400
            os.utime(d, (stamp, stamp))
            rep = ops.cleanup(write=True, include_unmarked=True)  # most aggressive mode
            by_id = {c["session_id"]: c for c in rep["candidates"]}
            check("CRITICAL: an ACTUALLY-live worker's session (stale dir mtime) is "
                  "refused even by cleanup --write --include-unmarked",
                  by_id[sid]["eligible"] is False, f"-> {by_id.get(sid)}")
            check("...and its workspace genuinely still exists on disk", d.is_dir())
        finally:
            stopped = sessions.stop(sid)
        check("sessions.stop() reports a clean teardown",
              stopped.get("ok") is True and stopped.get("deleted") is True,
              f"-> {stopped}")
        check("after stop(), the directory (and its lock) is gone", not d.exists())


# ── 5. audit log rotation: caps the live file, keeps a bounded history ─────
with tempfile.TemporaryDirectory(prefix="codecalc-audit-rotate-") as _d:
    _p = pathlib.Path(_d) / "audit.log"
    _old_env = os.environ.get(audit_module.AUDIT_MAX_MB_ENV)
    os.environ[audit_module.AUDIT_MAX_MB_ENV] = "0.001"  # ~1 KiB — trips fast
    try:
        _log = audit_module.AuditLog(_p, clock=lambda: 0.0)
        for i in range(300):
            _log.emit(audit_module.CLEANUP, run_id=f"r{i}", reason="x" * 40)
        check("rotation: the live file exists after many emits", _p.is_file())
        # The cap is checked BEFORE each append (see AuditLog.emit's THE-898
        # comment), so the live file may sit up to one line's worth OVER the
        # cap right after the write that crossed it — rotation is what the
        # NEXT emit does. One more emit forces that pending rotation, and
        # ONLY THEN is "under the cap" a fact this can assert rather than a
        # race against exactly when the loop above happened to stop.
        _log.emit(audit_module.CLEANUP, run_id="settle", reason="y" * 10)
        check("rotation: the live file settles under the configured cap "
              "once one more emit forces the pending rotation",
              _p.stat().st_size < audit_module._audit_max_bytes(),
              f"-> {_p.stat().st_size} vs cap {audit_module._audit_max_bytes()}")
        check("rotation: a first-generation rotated file exists",
              (_p.with_name("audit.log.1")).is_file())
        check("rotation: a second-generation rotated file exists",
              (_p.with_name("audit.log.2")).is_file())
        check("rotation: history is BOUNDED — no third generation beyond keep-N",
              not (_p.with_name("audit.log.3")).exists())
        _lines = [json.loads(line) for line in
                  _p.read_text(encoding="utf-8").splitlines() if line.strip()]
        check("rotation: every line in the live file is still valid JSON",
              all(ln["event_type"] == audit_module.CLEANUP for ln in _lines))
    finally:
        if _old_env is None:
            os.environ.pop(audit_module.AUDIT_MAX_MB_ENV, None)
        else:
            os.environ[audit_module.AUDIT_MAX_MB_ENV] = _old_env


# ── 5b. _audit_max_bytes(): unset/invalid-safe, mirrors sessions' shape ────
_old_env = os.environ.pop(audit_module.AUDIT_MAX_MB_ENV, None)
try:
    check("unset CODECALC_AUDIT_MAX_MB falls back to the default cap",
          audit_module._audit_max_bytes() == int(audit_module._DEFAULT_AUDIT_MAX_MB * 1024 * 1024))
    os.environ[audit_module.AUDIT_MAX_MB_ENV] = "not-a-number"
    check("a non-numeric CODECALC_AUDIT_MAX_MB falls back to the default, never crashes",
          audit_module._audit_max_bytes() == int(audit_module._DEFAULT_AUDIT_MAX_MB * 1024 * 1024))
    os.environ[audit_module.AUDIT_MAX_MB_ENV] = "-5"
    check("a non-positive CODECALC_AUDIT_MAX_MB falls back to the default",
          audit_module._audit_max_bytes() == int(audit_module._DEFAULT_AUDIT_MAX_MB * 1024 * 1024))
    os.environ[audit_module.AUDIT_MAX_MB_ENV] = "5"
    check("a valid CODECALC_AUDIT_MAX_MB is honoured exactly",
          audit_module._audit_max_bytes() == 5 * 1024 * 1024)
finally:
    if _old_env is None:
        os.environ.pop(audit_module.AUDIT_MAX_MB_ENV, None)
    else:
        os.environ[audit_module.AUDIT_MAX_MB_ENV] = _old_env


# ── 6. a rotation failure is swallowed, never propagated (BEST EFFORT) ─────
with tempfile.TemporaryDirectory(prefix="codecalc-audit-rotate-fail-") as _d:
    _p = pathlib.Path(_d) / "audit.log"
    _p.write_bytes(b"x" * 2000)  # over the tiny cap set below, so rotation fires
    _old_env = os.environ.get(audit_module.AUDIT_MAX_MB_ENV)
    os.environ[audit_module.AUDIT_MAX_MB_ENV] = "0.001"  # ~1 KiB — 2000 bytes IS over it
    try:
        _log = audit_module.AuditLog(_p, clock=lambda: 0.0)
        _log._rotate = lambda: (_ for _ in ()).throw(OSError("simulated rotation failure"))
        _stderr = io.StringIO()
        _raised = False
        try:
            with contextlib.redirect_stderr(_stderr):
                _ev = _log.emit(audit_module.CLEANUP, run_id="rX")
        except OSError:
            _raised = True
            _ev = None
        check("a rotation failure does NOT raise out of emit()", not _raised)
        check("emit() still returns the event dict when rotation fails",
              _ev is not None and _ev["event_type"] == audit_module.CLEANUP)
        check("a rotation failure is reported on stderr (best-effort, not silent)",
              "not writable" in _stderr.getvalue(), f"-> {_stderr.getvalue()!r}")
    finally:
        if _old_env is None:
            os.environ.pop(audit_module.AUDIT_MAX_MB_ENV, None)
        else:
            os.environ[audit_module.AUDIT_MAX_MB_ENV] = _old_env


# ── 7. CLI surface: --help lists status/cleanup ─────────────────────────────
_help = subprocess.run([sys.executable, "-m", "codecalc", "--help"],
                       cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
check("codecalc --help exits 0", _help.returncode == 0)
check("codecalc --help documents the status command",
      "status" in _help.stdout, f"-> {_help.stdout!r}")
check("codecalc --help documents the cleanup command",
      "cleanup" in _help.stdout, f"-> {_help.stdout!r}")


# ── 8. CLI surface: status/cleanup end-to-end through the real subprocess ──
with tempfile.TemporaryDirectory(prefix="codecalc-ops-cli-") as _d:
    _tmp = pathlib.Path(_d)
    _sroot = _tmp / "sessions"
    _sroot.mkdir()
    _mkdir_session(_sroot, "python3-cliexpired", content=b"q" * 321, marker=True,
                   age_seconds=2 * 86400)
    _env = dict(os.environ)
    _env["CODECALC_SESSION_ROOT"] = str(_sroot)
    _env["CODECALC_AUDIT_LOG"] = str(_tmp / "audit" / "audit.log")
    _env["CODECALC_RUN_STATE_DIR"] = str(_tmp / "runs")

    _status_json = subprocess.run(
        [sys.executable, "-m", "codecalc", "status", "--json"],
        cwd=REPO_ROOT, env=_env, capture_output=True, text=True, timeout=60)
    check("codecalc status --json exits 0", _status_json.returncode == 0,
          f"-> stderr={_status_json.stderr!r}")
    _parsed = json.loads(_status_json.stdout)
    check("codecalc status --json sees the seeded session",
          _parsed["session_count"] == 1 and _parsed["idle_expired_session_ids"] == ["python3-cliexpired"])

    _dry = subprocess.run([sys.executable, "-m", "codecalc", "cleanup"],
                          cwd=REPO_ROOT, env=_env, capture_output=True, text=True, timeout=60)
    check("codecalc cleanup (dry-run default) exits 0", _dry.returncode == 0)
    check("codecalc cleanup dry-run output says 'would remove'",
          "would remove" in _dry.stdout, f"-> {_dry.stdout!r}")
    check("codecalc cleanup dry-run removed nothing via the CLI",
          (_sroot / "python3-cliexpired").is_dir())

    _write = subprocess.run([sys.executable, "-m", "codecalc", "cleanup", "--write"],
                            cwd=REPO_ROOT, env=_env, capture_output=True, text=True, timeout=60)
    check("codecalc cleanup --write exits 0", _write.returncode == 0)
    check("codecalc cleanup --write actually removed the expired dir via the CLI",
          not (_sroot / "python3-cliexpired").exists())


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL OPS/AUDIT-ROTATION TESTS PASS ===")
sys.exit(1 if FAILS else 0)
