"""Per-session and global disk quotas for sessions.

The gap this closes, in `sessions.py`'s own words before this ticket: no
per-session total-disk quota existed — only `SPILL_CAPTURE_KB` (4 MiB/stream),
`RESOURCE_MAX_BYTES` (4 MiB/served file) and `_SPILL_RETENTION` (20 spills)
bounded any ONE thing a session could write. Nothing summed the total a
session accumulates across `session_write_file` calls, artifacts a running
program creates, and files EXECUTED CODE writes into the workspace during
`session_run`/`execute_code(session_id=...)`. A session could fill the
operator's disk one small write at a time and none of the existing caps would
ever notice.

Five independent ceilings are exercised here, each in isolation: the other
four are always set to a value generous enough that it cannot itself trip
during that test (`_SAFE_ENV`), so a failure names the ONE knob under test
rather than an interaction between several.

- per-session quota (`CODECALC_SESSION_DISK_QUOTA_MB`): `write_file` refused,
  no partial file left behind.
- per-artifact size cap (`CODECALC_MAX_ARTIFACT_BYTES`): a single write over
  the cap refused; a write under it still succeeds (positive control).
- per-artifact count cap (`CODECALC_MAX_ARTIFACT_COUNT`): the Nth+1 NEW file
  refused; overwriting an EXISTING file at the cap still succeeds, because it
  creates nothing new.
- post-run over-quota disclosure: code executed via `execute()` writes the
  session over its quota; the result DISCLOSES that (`disk_quota_exceeded`)
  without failing the run that already happened, and the session's next
  write/run is refused until the workspace is back under the line — with no
  separate sticky flag, so freeing the space un-refuses it on its own.
- host-free-space floor (`CODECALC_MIN_HOST_FREE_MB`): a floor set absurdly
  high refuses writes regardless of how generous every quota above is.

`codecalc doctor` reporting the configured limits and current usage is
checked last, against a session whose exact on-disk size this file already
controls.

FIX ROUND 2 (adversarial review found the enforcement above had holes that
still left the DoS achievable):

- `install_package` was completely unguarded — the LARGEST write vector
  (packages are MB-GB), with no `quota_precheck`/`quota_postcheck` at all.
  An over-quota session installed freely. Now refused before the subprocess
  even runs (asserted via a stubbed `subprocess.run` that must never fire),
  and an install that pushes a session over quota discloses it.
- the artifact COUNT cap only ran from THIS module's own writes
  (`write_file`/the spill write); code executed via `execute()`/
  `run_file()` could create arbitrarily many tiny files — each under the
  BYTE quota — with nothing to catch the file count. Now
  `quota_postcheck`/`quota_precheck` carry a count check too, the same
  disclose-then-block shape the byte quota already had.
- an overwrite double-counted: `_session_dir_size` already counts the file
  being overwritten at its CURRENT size, so comparing against the FULL new
  size wrongly refused a same-size or shrinking overwrite near quota. Fixed
  to compare the NET delta.
- with no separate recovery path, a session stuck over quota had `execute`/
  `run_file` refused (so code cannot `rm` its way out) and, pre-fix,
  shrinking overwrites refused too — `session_stop` (destroying the whole
  session) was the only way out. A net-non-positive write is now ALWAYS
  permitted, even while over quota — the in-band recovery path.

GH #325 / THE-1090: the artifact-COUNT cap never got the in-band recovery
path FIX 4 above gave the byte quota. `write_file` can only create or
overwrite content, never remove it, so once a session was over
`CODECALC_MAX_ARTIFACT_COUNT` — reachable one call at a time via `write_file`
or, faster, via executed code creating hundreds of tiny files in one
`execute()` (FIX 2's own scenario) — nothing inside the session could ever
shrink the count back under the cap short of `session_stop` destroying the
whole workspace, and the refusal's own remedy named an action ("delete
unneeded files") no tool in the package could perform. `session_delete_file`
closes that gap; exercised here:

- the exact repro shape: `execute()` pushes a session over the count cap in
  one call, the next `execute()` is refused (same as FIX 2), and
  `session_delete_file` — itself exempt from the cap it exists to relieve —
  frees enough files that `execute()` succeeds again with no `session_stop`.
- `session_delete_file` refuses a `../` path escape, an absolute path, a
  runner-internal path (`.codecalc-run/...`, the session lock file — the
  same set `session_artifacts` already excludes), and a directory, each
  with the jail/validation refusal shape every other session tool uses.
- a missing file is a CODED not-found refusal, not a crash or a silent
  no-op.
- a symlink INSIDE the workspace pointing OUTSIDE it is deleted as the link
  it is — the target survives untouched. This is `_jail_nofollow`'s reason
  to exist: `_jail`'s ordinary resolve() follows the final path component
  too, so a link to an outside target reads as the delete itself
  "escaping" and would be wrongly refused.

FIX ROUND 2 (grok verify-security review of the first commit found the
delete path itself was a new confused-deputy host-file destructor):

- **HIGH — parent-swap TOCTOU.** `_jail_nofollow` resolved the parent once
  and `Path.unlink()` re-walked that same path string; a racing SESSION
  WORKER (the server process is not Landlocked — `_write_nofollow`'s own
  docstring) could swap a parent component for a symlink pointing outside
  the workspace between the two, and the server's own `unlink()` would
  follow it and delete a HOST file. Manually reproduced against the
  pre-fix commit before writing the fix (a background thread doing
  `rename/symlink/unlink/rename` on the parent while 4000 `delete_file`
  calls raced it deleted the outside target on the first commit; zero
  escapes and the target survives after the fix, asserted below). Fixed by
  pinning the parent to an `O_DIRECTORY|O_NOFOLLOW` fd and comparing its
  `(st_dev, st_ino)` against the identity recorded at resolve time, never
  a fresh `lstat` of the swappable path — see `_jail_nofollow`/
  `delete_file`'s own comments.
- **MEDIUM — the idle-expiry marker was deletable.** `_is_runner_internal`
  refused the lock file but not `_EXPIRED_MARKER_NAME` — deleting it made
  the next `execute()` miss `_is_expired_on_disk` and silently respawn a
  worker `_get_worker_or_expired` had already reaped. Added to
  `_is_runner_internal` alongside the lock file. `__pycache__`/`*.pyc`
  (LOW, in-workspace only) were hidden from `session_artifacts` by
  `_workspace_scan` but not refused by `_is_runner_internal` — aligned too.
- **MEDIUM — case/trailing-dot denylist bypass.** `.CODECALC-SESSION-LOCK`
  or `.codecalc-session-lock.` pass a plain `==` denylist while naming the
  SAME file as `.codecalc-session-lock` on a case-insensitive volume
  (default APFS, NTFS) or after Windows strips a trailing `.`/` ` at the
  API boundary. `_reserved_root_name` now compares `str.casefold()` of
  both the raw and dot/space-stripped spelling, checked unconditionally
  (not gated on this host's own case-sensitivity), so both spellings are
  provably refused as pure denylist checks even on a case-sensitive Linux
  CI runner.

FIX ROUND 3 (a second grok verify-security pass on round 2's commit found
round 2's own fix for the HIGH was still incomplete, plus two smaller gaps):

- **HIGH — round 2's "pin" was still two SEPARATE filesystem walks of the
  same path string** (`_jail_nofollow`'s `resolve()`, then `delete_file`'s
  own `parent.stat()` + `os.open(parent, O_NOFOLLOW)`), so a worker racing
  an INTERMEDIATE path component (not the immediate parent) between the
  two walks made both land on the attacker's directory and silently
  agree — `O_NOFOLLOW` on the second walk only refuses a symlink at ITS
  OWN final component, and for a nested path like `a/b/passwd` with `a`
  swapped, that final component (`b`) is never itself the symlink.
  Manually reproduced against round 2's commit (559d3d0) before writing
  this fix (the nested race deleted an outside file, 1/6000 escapes);
  zero escapes and the target survives after this fix, asserted below.
  Fixed by replacing BOTH re-walks with exactly ONE: `_unlink_pinned`
  opens the workspace root once and `openat`s each remaining path
  component in turn, `dir_fd=` of the PREVIOUS hop, `O_NOFOLLOW|
  O_DIRECTORY` on every hop — a symlink anywhere in the chain fails THAT
  hop's own open with `ELOOP`, so there is no later re-walk left for it to
  hide from. `_jail_nofollow` no longer resolves anything at all; it is
  pure string validation now (length, segments, absolute-path/`..`
  escape) and returns path COMPONENTS, never a `Path`.
- **MEDIUM — Windows 8.3 short names.** `CODECA~1` names the SAME file as
  `.codecalc-session-lock` on NTFS with short names enabled, and bears no
  textual relationship to the long name a casefold/strip comparison could
  catch. `_reserved_root_name` now refuses anything SHAPED like an 8.3
  short name (a literal `~` immediately followed by a digit) at the
  session root outright; `delete_file`'s Windows fallback path
  additionally resolves the final component to its long form
  (`_final_component_long_name`, via `os.path.realpath`) before the
  denylist runs.
- **LOW — `__pycache__`/`*.pyc` were still a byte-exact match**, unlike
  the reserved-name check, which already casefolds. `.PYC`/`__PYCACHE__`
  now get the same `str.casefold()` treatment.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import threading

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import doctor, errors, packages, sessions

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


#: Values generous enough that none of them can itself refuse a write —
#: every test below overrides only the ONE knob it means to exercise, so a
#: failure is never actually a different ceiling tripping first.
_SAFE_ENV = {
    sessions.SESSION_DISK_QUOTA_MB_ENV: "10000",
    sessions.TOTAL_DISK_QUOTA_MB_ENV: "1000000",
    sessions.MAX_ARTIFACT_BYTES_ENV: str(200 * 1024 * 1024),
    sessions.MAX_ARTIFACT_COUNT_ENV: "1000000",
    # 1 MB is comfortably below any CI runner's free space, so this knob
    # never refuses on its own unless a test explicitly sets it absurdly high.
    sessions.MIN_HOST_FREE_MB_ENV: "1",
}


def _with_env(overrides: dict[str, str], fn):
    """Set `_SAFE_ENV` merged with `overrides`, run `fn`, restore exactly."""
    merged = {**_SAFE_ENV, **overrides}
    old = {k: os.environ.get(k) for k in merged}
    os.environ.update(merged)
    try:
        return fn()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _new_session() -> str:
    """A workspace-only session — no worker, so no runtime needs to be
    installed just to create one. `bash` is registered but never executed by
    the write/artifact tests; only the post-run test runs real python3 code,
    for which this session type is equally usable (`execute()`'s
    workspace-only branch ignores the session's own start language and runs
    whatever `language=` it is given — see `execute()`'s docstring)."""
    started = sessions.start("bash")
    assert started.get("ok"), started
    return started["session_id"]


# ── 1. per-session quota: write_file refused, no partial file ──────────────
def _test_session_quota():
    sid = _new_session()
    try:
        target = sessions._session_dir(sid) / "big.txt"
        r = _with_env(
            {sessions.SESSION_DISK_QUOTA_MB_ENV: "0.001"},  # ~1048 bytes
            lambda: sessions.write_file(sid, "big.txt", "x" * 20000),
        )
        check("session quota: an over-quota write is refused",
              r.get("ok") is False, f"-> {r}")
        check("session quota: refusal carries the stable RESOURCE_EXHAUSTED code",
              r.get("code") == errors.RESOURCE_EXHAUSTED, f"-> {r.get('code')}")
        check("session quota: refusal names usage_bytes and quota_bytes",
              isinstance(r.get("usage_bytes"), int) and isinstance(r.get("quota_bytes"), int),
              f"-> {r}")
        check("session quota: NO partial file was left behind",
              not target.exists(), f"-> {target}")

        # positive control: a small write under the SAME tiny quota succeeds
        r2 = _with_env(
            {sessions.SESSION_DISK_QUOTA_MB_ENV: "0.001"},
            lambda: sessions.write_file(sid, "small.txt", "ok"),
        )
        check("session quota: a write comfortably under quota still succeeds",
              r2.get("ok") is True, f"-> {r2}")
    finally:
        sessions.stop(sid)


_test_session_quota()


# ── 2. per-artifact size cap ────────────────────────────────────────────────
def _test_artifact_size_cap():
    sid = _new_session()
    try:
        r = _with_env(
            {sessions.MAX_ARTIFACT_BYTES_ENV: "100"},
            lambda: sessions.write_file(sid, "oversized.bin", "x" * 500),
        )
        check("artifact size cap: a write over the per-artifact cap is refused",
              r.get("ok") is False and r.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {r}")
        check("artifact size cap: no partial file was left behind",
              not (sessions._session_dir(sid) / "oversized.bin").exists())

        r2 = _with_env(
            {sessions.MAX_ARTIFACT_BYTES_ENV: "100"},
            lambda: sessions.write_file(sid, "fits.bin", "x" * 50),
        )
        check("artifact size cap: a write under the cap still succeeds (control)",
              r2.get("ok") is True, f"-> {r2}")
    finally:
        sessions.stop(sid)


_test_artifact_size_cap()


# ── 3. per-artifact count cap ───────────────────────────────────────────────
def _test_artifact_count_cap():
    sid = _new_session()
    try:
        def _run():
            r1 = sessions.write_file(sid, "a.txt", "1")
            r2 = sessions.write_file(sid, "b.txt", "2")
            r3 = sessions.write_file(sid, "c.txt", "3")  # the Nth+1 NEW file
            # overwriting an EXISTING file creates nothing new, so it must
            # still succeed even though the session is AT the count cap.
            r4 = sessions.write_file(sid, "a.txt", "1-updated")
            return r1, r2, r3, r4

        r1, r2, r3, r4 = _with_env({sessions.MAX_ARTIFACT_COUNT_ENV: "2"}, _run)
        check("artifact count cap: file 1/2 under the cap succeeds",
              r1.get("ok") is True, f"-> {r1}")
        check("artifact count cap: file 2/2 (at the cap) succeeds",
              r2.get("ok") is True, f"-> {r2}")
        check("artifact count cap: the 3rd NEW file is refused",
              r3.get("ok") is False and r3.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {r3}")
        check("artifact count cap: no partial file was left behind for the refusal",
              not (sessions._session_dir(sid) / "c.txt").exists())
        check("artifact count cap: overwriting an EXISTING file at the cap still succeeds",
              r4.get("ok") is True, f"-> {r4}")
    finally:
        sessions.stop(sid)


_test_artifact_count_cap()


# ── 4. post-run over-quota: disclosed, then blocks the session until freed ─
def _test_post_run_quota():
    sid = _new_session()
    try:
        # Comfortably under quota before anything runs (an empty fresh
        # workspace), but a 200 KB write during the run blows well past it.
        write_code = "open('big.bin', 'wb').write(b'0' * 200000)"

        def _run():
            return sessions.execute(sid, write_code, language="python3")

        result = _with_env({sessions.SESSION_DISK_QUOTA_MB_ENV: "0.05"}, _run)
        check("post-run quota: the run itself still succeeded",
              result.get("ok") is True, f"-> {result}")
        check("post-run quota: the over-quota state is DISCLOSED in the result",
              result.get("disk_quota_exceeded") is True, f"-> {result}")
        check("post-run quota: usage/quota bytes are both reported and usage > quota",
              isinstance(result.get("disk_usage_bytes"), int)
              and isinstance(result.get("disk_quota_bytes"), int)
              and result["disk_usage_bytes"] > result["disk_quota_bytes"],
              f"-> {result}")

        # The session is now over quota. The NEXT write/run must be refused
        # — re-measured fresh each time, not a separate sticky flag (see
        # `quota_precheck`'s docstring) — for as long as it stays that way.
        def _next_write():
            return sessions.write_file(sid, "more.txt", "x")

        blocked_write = _with_env({sessions.SESSION_DISK_QUOTA_MB_ENV: "0.05"}, _next_write)
        check("post-run quota: the NEXT write on this session is refused",
              blocked_write.get("ok") is False
              and blocked_write.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {blocked_write}")

        def _next_run():
            return sessions.execute(sid, "1", language="python3")

        blocked_run = _with_env({sessions.SESSION_DISK_QUOTA_MB_ENV: "0.05"}, _next_run)
        check("post-run quota: the NEXT execute() on this session is ALSO refused",
              blocked_run.get("ok") is False
              and blocked_run.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {blocked_run}")

        # Free the space directly (the on-disk truth changes), and the very
        # next call succeeds again — nothing had to be told to "unblock".
        (sessions._session_dir(sid) / "big.bin").unlink()

        def _after_free():
            return sessions.write_file(sid, "recovered.txt", "ok")

        recovered = _with_env({sessions.SESSION_DISK_QUOTA_MB_ENV: "0.05"}, _after_free)
        check("post-run quota: freeing space un-refuses the session on its own "
              "(no sticky flag to clear)",
              recovered.get("ok") is True, f"-> {recovered}")
    finally:
        sessions.stop(sid)


_test_post_run_quota()


# ── 5. host-free-space floor ────────────────────────────────────────────────
def _test_host_free_floor():
    sid = _new_session()
    try:
        r = _with_env(
            {sessions.MIN_HOST_FREE_MB_ENV: "999999999999"},  # no host has this much free
            lambda: sessions.write_file(sid, "x.txt", "y"),
        )
        check("host-free floor: an absurdly high floor refuses the write",
              r.get("ok") is False and r.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {r}")
        check("host-free floor: refusal names the host scope",
              r.get("scope") == "host", f"-> {r}")
        check("host-free floor: no partial file was left behind",
              not (sessions._session_dir(sid) / "x.txt").exists())
    finally:
        sessions.stop(sid)


_test_host_free_floor()


# ── 6. doctor: configured limits + current usage ────────────────────────────
def _test_doctor():
    sid = _new_session()
    try:
        content = "z" * 12345

        def _run():
            sessions.write_file(sid, "measured.bin", content)
            return doctor.report(deep=False)

        rep = _with_env(
            {sessions.SESSION_DISK_QUOTA_MB_ENV: "777",
             sessions.TOTAL_DISK_QUOTA_MB_ENV: "8888"},
            _run,
        )
        dq = rep.get("disk_quota")
        check("doctor: report carries a disk_quota section",
              isinstance(dq, dict), f"-> {rep.keys()}")
        limits = dq.get("limits", {}) if isinstance(dq, dict) else {}
        check("doctor: configured session quota is reported back exactly",
              limits.get("session_disk_quota_mb") == 777.0, f"-> {limits}")
        check("doctor: configured total quota is reported back exactly",
              limits.get("total_disk_quota_mb") == 8888.0, f"-> {limits}")
        rows = {row["session_id"]: row["usage_bytes"] for row in dq.get("sessions", [])}
        check("doctor: the session this test just wrote to appears in usage",
              sid in rows, f"-> {sorted(rows)}")
        # Exactly the bytes just written: a fresh session has nothing else in
        # it, so this is a real measurement, not a shape check.
        check("doctor: reported usage for that session equals what was written",
              rows.get(sid) == len(content.encode("utf-8")),
              f"-> {rows.get(sid)} vs {len(content.encode('utf-8'))}")
        check("doctor: global usage is a non-negative measurement",
              isinstance(dq.get("global_usage_bytes"), int) and dq["global_usage_bytes"] >= 0,
              f"-> {dq.get('global_usage_bytes')}")
    finally:
        sessions.stop(sid)


_test_doctor()


# ── FIX 1: install_package is quota-checked (precheck + postcheck) ─────────
# `uv` must actually resolve on PATH for `packages.install` to reach the
# quota gate rather than failing earlier with "package manager not found" —
# probed, not assumed, same reasoning tests/test_package_allowlist.py uses.
_HAVE_UV = shutil.which("uv") is not None
if not _HAVE_UV:
    print("SKIP install_package quota tests (uv not on PATH)")


class _FakeCompleted:
    """Stands in for `subprocess.run`'s return value — no real network call,
    same shape tests/test_package_allowlist.py already uses for this
    module."""

    def __init__(self, returncode: int = 0, stdout: str = "Successfully installed\n"):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _test_install_package_precheck():
    """Reproduces the adversarial-review finding directly: a session already
    over its disk quota installed FREELY, because `packages.install` never
    called `quota_precheck`. Asserted via a stubbed `subprocess.run` that
    must NEVER fire — proving the refusal happens before any install
    process even starts, not merely that the final result says `ok: false`."""
    sid = _new_session()
    orig_run = packages.subprocess.run
    calls: list = []

    def _stub(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()

    try:
        # Push the session over a tiny quota first (under a generous env so
        # THIS write is not itself refused), then shrink the quota.
        _with_env({}, lambda: sessions.write_file(sid, "big.bin", "x" * 20000))
        packages.subprocess.run = _stub

        def _install():
            return packages.install("python3", "requests", session_id=sid)

        r = _with_env({sessions.SESSION_DISK_QUOTA_MB_ENV: "0.001"}, _install)
        check("install_package: an over-quota session is refused BEFORE installing",
              r.get("ok") is False and r.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {r}")
        check("install_package: the refusal happens BEFORE any subprocess runs "
              "(repro: pre-fix this installed freely)",
              calls == [], f"-> subprocess.run called {len(calls)} time(s): {calls}")
    finally:
        packages.subprocess.run = orig_run
        sessions.stop(sid)


def _test_install_package_postcheck():
    """An install that succeeds and pushes the session OVER quota discloses
    it, the same shape `execute()`'s `quota_postcheck` already has — a
    package's on-disk footprint cannot be known before the manager runs, so
    this can only be a post-check. `subprocess.run` is stubbed to behave
    like a real installer would: it WRITES into the given `cwd` and reports
    success, without touching the network."""
    sid = _new_session()
    orig_run = packages.subprocess.run
    calls: list = []

    def _stub(cmd, cwd=None, **kwargs):
        calls.append(cmd)
        if cwd:
            (pathlib.Path(cwd) / "fake-package.bin").write_bytes(b"P" * 50000)
        return _FakeCompleted()

    try:
        packages.subprocess.run = _stub

        def _install():
            return packages.install("python3", "requests", session_id=sid)

        # Comfortably under quota before the "install"; the stub's own write
        # (50000 bytes) is what pushes it over.
        r = _with_env({sessions.SESSION_DISK_QUOTA_MB_ENV: "0.02"}, _install)
        check("install_package: the install itself still succeeded",
              r.get("ok") is True, f"-> {r}")
        check("install_package: pushing the session over quota is DISCLOSED",
              r.get("disk_quota_exceeded") is True, f"-> {r}")
        check("install_package: usage/quota bytes are both reported and usage > quota",
              isinstance(r.get("disk_usage_bytes"), int)
              and isinstance(r.get("disk_quota_bytes"), int)
              and r["disk_usage_bytes"] > r["disk_quota_bytes"],
              f"-> {r}")

        # And the NEXT write on this session is refused, same as any other
        # over-quota session — install_package is not a special case.
        blocked = _with_env({sessions.SESSION_DISK_QUOTA_MB_ENV: "0.02"},
                            lambda: sessions.write_file(sid, "more.txt", "x"))
        check("install_package: the session is refused on its NEXT write, same "
              "as any other over-quota session",
              blocked.get("ok") is False and blocked.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {blocked}")
    finally:
        packages.subprocess.run = orig_run
        sessions.stop(sid)


if _HAVE_UV:
    _test_install_package_precheck()
    _test_install_package_postcheck()


# ── FIX 2: the artifact COUNT cap binds on executed-code output too ────────
def _test_executed_code_count_cap():
    """Reproduces the finding directly: executed code creating far more files
    than `CODECALC_MAX_ARTIFACT_COUNT`, each individually tiny (so the BYTE
    quota never notices), went completely unrefused pre-fix — 1005 files
    against a cap of 5. Now the count is checked post-run (disclosed) AND
    pre-run on the session's next call (blocked), the same shape the byte
    quota already had."""
    sid = _new_session()
    try:
        write_many = "\n".join(f"open('f{i}.txt', 'w').write('x')" for i in range(20))

        def _run():
            return sessions.execute(sid, write_many, language="python3")

        result = _with_env({sessions.MAX_ARTIFACT_COUNT_ENV: "5"}, _run)
        check("count cap: the run itself still succeeded",
              result.get("ok") is True, f"-> {result}")
        check("count cap: creating 20 files against a cap of 5 is DISCLOSED",
              result.get("artifact_count_exceeded") is True, f"-> {result}")
        check("count cap: artifact_count/max_artifact_count are both reported "
              "and count > max",
              isinstance(result.get("artifact_count"), int)
              and result.get("max_artifact_count") == 5
              and result["artifact_count"] > 5,
              f"-> {result}")

        # The NEXT execute() on this session is refused pre-run — the gap
        # the finding named: pre-fix, only write_file's OWN per-write count
        # check existed, so a session already over the count cap via
        # EXECUTED code could keep executing more code freely.
        blocked_run = _with_env({sessions.MAX_ARTIFACT_COUNT_ENV: "5"},
                                lambda: sessions.execute(sid, "1", language="python3"))
        check("count cap: the NEXT execute() on this session is refused",
              blocked_run.get("ok") is False
              and blocked_run.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {blocked_run}")
    finally:
        sessions.stop(sid)


_test_executed_code_count_cap()


# ── FIX 3 & 4: net-delta overwrites, and the in-band recovery path ─────────
def _test_overwrite_net_delta():
    sid = _new_session()
    try:
        # A baseline file under a generous quota.
        r0 = _with_env({}, lambda: sessions.write_file(sid, "f.bin", "x" * 2000))
        check("overwrite setup: the baseline 2000-byte file writes",
              r0.get("ok") is True, f"-> {r0}")

        # NEAR quota: comfortably more than the 2000 bytes already there, but
        # nowhere near double it — pre-fix, `_disk_quota_refusal` compared
        # usage(2000, already counting f.bin) + incoming(2000, the FULL new
        # size) = 4000 against this quota and wrongly refused both writes
        # below.
        near_quota_mb = 2500 / (1024 * 1024)

        r_same = _with_env(
            {sessions.SESSION_DISK_QUOTA_MB_ENV: repr(near_quota_mb)},
            lambda: sessions.write_file(sid, "f.bin", "y" * 2000),
        )
        check("fix3: a SAME-SIZE overwrite near quota succeeds (net=0)",
              r_same.get("ok") is True, f"-> {r_same}")

        r_smaller = _with_env(
            {sessions.SESSION_DISK_QUOTA_MB_ENV: repr(near_quota_mb)},
            lambda: sessions.write_file(sid, "f.bin", "z" * 500),
        )
        check("fix3: a SHRINKING overwrite near quota succeeds (net<0)",
              r_smaller.get("ok") is True, f"-> {r_smaller}")
        check("fix3: usage after the shrink reflects the smaller file, not "
              "the bigger one it replaced",
              sessions._session_dir_size(sid) == 500,
              f"-> {sessions._session_dir_size(sid)}")

        # Push the session OVER a much smaller quota (a second, bigger file,
        # written while the quota is generous so IT is not itself refused).
        _with_env({}, lambda: sessions.write_file(sid, "big2.bin", "w" * 5000))
        tiny_quota_mb = 200 / (1024 * 1024)  # well under current usage (~5500B)
        over = _with_env({sessions.SESSION_DISK_QUOTA_MB_ENV: repr(tiny_quota_mb)},
                         lambda: sessions.quota_precheck(sid))
        check("fix4 setup: the session is now confirmed OVER quota",
              over is not None, f"-> {over}")

        # FIX 4: a net<=0 write is the in-band recovery path — allowed even
        # though the session is CURRENTLY over quota, which nothing else
        # (execute/run_file are refused by quota_precheck) permits.
        r_recover = _with_env(
            {sessions.SESSION_DISK_QUOTA_MB_ENV: repr(tiny_quota_mb)},
            lambda: sessions.write_file(sid, "big2.bin", "v" * 100),
        )
        check("fix4: a net<=0 write succeeds even while the session is OVER "
              "quota (in-band recovery, no session_stop needed)",
              r_recover.get("ok") is True, f"-> {r_recover}")

        # And a write that GROWS usage is still refused while over quota —
        # the recovery path is net<=0 ONLY, not a blanket bypass.
        r_still_blocked = _with_env(
            {sessions.SESSION_DISK_QUOTA_MB_ENV: repr(tiny_quota_mb)},
            lambda: sessions.write_file(sid, "growing.txt", "q" * 50),
        )
        check("fix4: a GROWING write is still refused while over quota "
              "(the recovery path is net<=0 only)",
              r_still_blocked.get("ok") is False
              and r_still_blocked.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {r_still_blocked}")
    finally:
        sessions.stop(sid)


_test_overwrite_net_delta()


# ── GH #325 / THE-1090: session_delete_file, the count cap's in-band
#    recovery path ──────────────────────────────────────────────────────────
def _can_symlink() -> bool:
    """Probe symlink-creation capability rather than assume it — an
    unprivileged Windows account lacks SeCreateSymbolicLinkPrivilege by
    default and `symlink_to` raises there. Same reasoning
    tests/test_session_jail.py's own `_can_symlink` probe uses (a fresh,
    file-local copy rather than an import across test files, matching how
    every test file here is a standalone script)."""
    probe_dir = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-quota-symlink-probe-"))
    probe = probe_dir / "probe"
    try:
        probe.symlink_to(probe_dir)
        return True
    except OSError:
        return False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
        shutil.rmtree(probe_dir, ignore_errors=True)


def _test_delete_file_recovers_over_count_session():
    """The exact #325 repro: executed code creates more files than the cap
    in ONE `execute()` call (so `write_file`'s own per-write count check
    never fires — this is FIX 2's scenario), the NEXT `execute()` is
    refused pre-run, and — the gap #325 reports — nothing inside the
    session could shrink the count back under the cap. `session_delete_file`
    is that path: it is itself NOT refused by the cap it exists to relieve,
    and deleting enough files lets `execute()` succeed again with no
    `session_stop`."""
    sid = _new_session()
    try:
        write_many = "\n".join(f"open('f{i}.txt', 'w').write('x')" for i in range(6))

        def _run():
            return sessions.execute(sid, write_many, language="python3")

        result = _with_env({sessions.MAX_ARTIFACT_COUNT_ENV: "3"}, _run)
        check("delete recovery: the run itself still succeeded",
              result.get("ok") is True, f"-> {result}")
        check("delete recovery: 6 files against a cap of 3 pushes the session over",
              result.get("artifact_count_exceeded") is True, f"-> {result}")

        blocked = _with_env(
            {sessions.MAX_ARTIFACT_COUNT_ENV: "3"},
            lambda: sessions.execute(sid, "1", language="python3"))
        check("delete recovery: the NEXT execute() is refused over the cap",
              blocked.get("ok") is False and blocked.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {blocked}")
        check("delete recovery: the refusal's remedy now names session_delete_file "
              "(pre-fix it named a tool that could not act on it)",
              "session_delete_file" in blocked.get("remedy", ""), f"-> {blocked.get('remedy')}")

        for name in ("f3.txt", "f4.txt", "f5.txt"):
            d = _with_env({sessions.MAX_ARTIFACT_COUNT_ENV: "3"},
                          lambda name=name: sessions.delete_file(sid, name))
            check(f"delete recovery: session_delete_file({name!r}) succeeds while "
                  "the session is OVER the count cap (exempt from the cap it relieves)",
                  d.get("ok") is True, f"-> {d}")

        recovered = _with_env(
            {sessions.MAX_ARTIFACT_COUNT_ENV: "3"},
            lambda: sessions.execute(sid, "1", language="python3"))
        check("delete recovery: execute() succeeds again once the count is back "
              "at/under the cap — no session_stop needed",
              recovered.get("ok") is True, f"-> {recovered}")
    finally:
        sessions.stop(sid)


_test_delete_file_recovers_over_count_session()


def _test_delete_file_refusals():
    sid = _new_session()
    try:
        r_escape = sessions.delete_file(sid, "../x")
        check("delete: a '../' path escape is refused",
              r_escape.get("ok") is False and r_escape.get("code") == errors.PERMISSION_DENIED,
              f"-> {r_escape}")

        abs_path = str((sessions._session_dir(sid).parent / "x").resolve())
        r_abs = sessions.delete_file(sid, abs_path)
        check("delete: an absolute path escaping the workspace is refused",
              r_abs.get("ok") is False, f"-> {r_abs}")

        r_internal = sessions.delete_file(sid, f"{sessions._RUNNER_SCRATCH_DIRNAME}/main.py")
        check("delete: a runner-internal path (.codecalc-run/...) is refused, "
              "the same set session_artifacts already excludes",
              r_internal.get("ok") is False
              and r_internal.get("code") == errors.PERMISSION_DENIED,
              f"-> {r_internal}")

        r_lock = sessions.delete_file(sid, sessions._LOCK_FILE_NAME)
        check("delete: the session lock file is refused",
              r_lock.get("ok") is False
              and r_lock.get("code") == errors.PERMISSION_DENIED,
              f"-> {r_lock}")

        r_missing = sessions.delete_file(sid, "nope.txt")
        check("delete: a missing file is a CODED not-found refusal, not a crash",
              r_missing.get("ok") is False and r_missing.get("code") == errors.VALIDATION,
              f"-> {r_missing}")

        wr = sessions.write_file(sid, "dir/inside.txt", "x")
        check("delete refusal setup: a nested file writes",
              wr.get("ok") is True, f"-> {wr}")
        r_dir = sessions.delete_file(sid, "dir")
        check("delete: a directory is refused, not silently recursed",
              r_dir.get("ok") is False and r_dir.get("code") == errors.VALIDATION,
              f"-> {r_dir}")

        # positive control: the file nested under that same directory still
        # deletes cleanly — the refusals above are about THOSE paths, not a
        # blanket failure of nested deletes.
        r_ok = sessions.delete_file(sid, "dir/inside.txt")
        check("delete: an ordinary nested file deletes (control)",
              r_ok.get("ok") is True, f"-> {r_ok}")
        check("delete: the file is actually gone from disk",
              not (sessions._session_dir(sid) / "dir" / "inside.txt").exists())
    finally:
        sessions.stop(sid)


_test_delete_file_refusals()


def _test_delete_file_refuses_nul_names():
    """THE-1103: a NUL byte in the path used to reach a real filesystem call
    (`_unlink_pinned`'s `os.lstat`/`os.open`) unrefused, raising a bare
    `ValueError` straight past `delete_file`'s own guarded `try` — over MCP,
    a bare `ToolError` instead of the `{"ok": False, "code": ..., ...}`
    shape #212 requires every public session function to return.
    `write_file` already refused the identical input (`_jail`'s `resolve()`
    raises the same `ValueError`, and every `_jail` caller already catches
    it) — the fix makes `_jail_nofollow` raise it too, at the same
    string-only validation stage, so `delete_file` gets exactly the same
    PERMISSION_DENIED refusal `write_file` does.

    NUL rows only assert that parity: both hit the identical explicit
    `"\\x00" in p` check, so the same code on both sides is a real
    invariant, not a coincidence — unlike the surrogate case, where
    `write_file`'s refusal is a POSIX accident of `_jail`'s `resolve()`
    (see `_test_delete_file_refuses_surrogate_names`, which does not make
    that comparison).
    """
    sid = _new_session()
    try:
        # setup: a real nested file, so an over-eager component match on the
        # NUL-containing sibling name below would be visible as data loss,
        # not just a wrong return value.
        setup = sessions.write_file(sid, "sub/real.txt", "keep me")
        check("nul setup: a real nested file writes",
              setup.get("ok") is True, f"-> {setup}")

        bad_names = ["a\x00b", "sub/a\x00b", "\x00"]
        for name in bad_names:
            wr = sessions.write_file(sid, name, "x")
            check(f"nul {name!r}: write_file refuses with PERMISSION_DENIED "
                  "(the sibling this must match)",
                  wr.get("ok") is False and wr.get("code") == errors.PERMISSION_DENIED,
                  f"-> {wr}")

            dr = sessions.delete_file(sid, name)
            check(f"nul {name!r}: delete_file returns a result dict, never raises",
                  isinstance(dr, dict), f"-> {dr!r}")
            check(f"nul {name!r}: delete_file refuses",
                  dr.get("ok") is False, f"-> {dr}")
            check(f"nul {name!r}: delete_file's code matches write_file's",
                  dr.get("code") == wr.get("code") == errors.PERMISSION_DENIED,
                  f"-> delete={dr.get('code')} write={wr.get('code')}")

        check("nul: the real nested file was never touched",
              (sessions._session_dir(sid) / "sub" / "real.txt").read_text() == "keep me")
    finally:
        sessions.stop(sid)


_test_delete_file_refuses_nul_names()


def _test_delete_file_refuses_surrogate_names():
    """THE-1103 round 2 (grok verify-security + Opus review, HIGH): the
    surrogate refusal must not depend on `os.fsencode` raising — that
    depends on the platform's own filesystem-encoding error mode
    (`surrogateescape` on POSIX, `surrogatepass`/`mbcs` on Windows), which
    do NOT agree on a bare surrogate. See `_jail_nofollow`'s / round 2's own
    docstring for the reproduction (`os.fsencode` monkeypatched to
    `surrogatepass` semantics let the pre-round-2 check through).

    This asserts the CONTRACT directly — `_jail_nofollow` raises for an
    unescaped surrogate, and `delete_file` returns a coded refusal — rather
    than through `write_file`'s parity, which round 1 asserted here and
    which is a POSIX accident of `_jail`'s `resolve()`, not a portable
    invariant: this suite's Windows CI legs run this file too, and
    `write_file`'s refusal there is not guaranteed to carry the same code
    (or refuse at all, pre-fix) for the identical string.

    Also the round-2 positive control: a LEGITIMATE `surrogateescape`'d
    byte (U+DC80, the low end of the range `surrogateescape` actually
    produces for a real undecodable byte) is not a lone surrogate this
    function should refuse — `_unlink_pinned` must still be able to delete
    a file genuinely named that way.
    """
    sid = _new_session()
    try:
        setup = sessions.write_file(sid, "sub/real.txt", "keep me")
        check("surrogate setup: a real nested file writes",
              setup.get("ok") is True, f"-> {setup}")

        bad_name = "foo" + chr(0xd800)  # lone high surrogate: never a legitimate surrogateescape byte
        try:
            sessions._jail_nofollow(bad_name)
        except ValueError as exc:
            check(f"surrogate {bad_name!r}: _jail_nofollow raises ValueError",
                  True, f"-> {exc}")
        else:
            check(f"surrogate {bad_name!r}: _jail_nofollow raises ValueError",
                  False, "-> did not raise")

        dr = sessions.delete_file(sid, bad_name)
        check(f"surrogate {bad_name!r}: delete_file returns a result dict, never raises",
              isinstance(dr, dict), f"-> {dr!r}")
        check(f"surrogate {bad_name!r}: delete_file refuses with PERMISSION_DENIED",
              dr.get("ok") is False and dr.get("code") == errors.PERMISSION_DENIED,
              f"-> {dr}")

        # positive control (round 2): the surrogateescape range itself must
        # stay usable.
        escaped_name = "foo" + chr(0xdc80)
        parts = sessions._jail_nofollow(escaped_name)
        check(f"surrogateescape {escaped_name!r}: _jail_nofollow accepts it "
              "(a real byte's own representation, not a lone surrogate)",
              parts == ["foo\udc80"], f"-> {parts}")

        dr2 = sessions.delete_file(sid, escaped_name)
        check(f"surrogateescape {escaped_name!r}: delete_file returns a result dict, "
              "never raises",
              isinstance(dr2, dict), f"-> {dr2!r}")
        check(f"surrogateescape {escaped_name!r}: delete_file does NOT refuse as "
              "PERMISSION_DENIED (a missing-file VALIDATION refusal is fine — the "
              "name is well-formed, the file just does not exist)",
              dr2.get("code") != errors.PERMISSION_DENIED, f"-> {dr2}")

        check("surrogate: the real nested file was never touched",
              (sessions._session_dir(sid) / "sub" / "real.txt").read_text() == "keep me")
    finally:
        sessions.stop(sid)


_test_delete_file_refuses_surrogate_names()


def _test_delete_file_windows_fallback_refuses_nul_and_surrogate():
    """THE-1103 round 2: the refusal must hold on the `not
    _DIR_FD_SUPPORTED` (Windows) fallback branch too, not only the
    `_unlink_pinned` `dir_fd` path this suite otherwise exercises on Linux.
    `_jail_nofollow` runs inside `delete_file`'s OWN guarded `try`, before
    `_DIR_FD_SUPPORTED` is ever consulted, so the refusal must fire
    identically regardless of which branch runs after it — this
    monkeypatches the platform flag to force `delete_file` onto the
    fallback code (`_final_component_long_name`'s `os.path.realpath`,
    `target.lstat()`/`target.unlink()`) and proves the bad name never even
    reaches it, rather than assuming module-level symmetry untested.
    """
    sid = _new_session()
    orig = sessions._DIR_FD_SUPPORTED
    sessions._DIR_FD_SUPPORTED = False
    try:
        setup = sessions.write_file(sid, "sub/real.txt", "keep me")
        check("windows-fallback setup: a real nested file writes",
              setup.get("ok") is True, f"-> {setup}")

        for name in ("a\x00b", "foo" + chr(0xd800)):
            dr = sessions.delete_file(sid, name)
            check(f"windows-fallback {name!r}: delete_file returns a result dict, "
                  "never raises",
                  isinstance(dr, dict), f"-> {dr!r}")
            check(f"windows-fallback {name!r}: delete_file refuses with PERMISSION_DENIED",
                  dr.get("ok") is False and dr.get("code") == errors.PERMISSION_DENIED,
                  f"-> {dr}")

        check("windows-fallback: the real nested file was never touched",
              (sessions._session_dir(sid) / "sub" / "real.txt").read_text() == "keep me")
    finally:
        sessions._DIR_FD_SUPPORTED = orig
        sessions.stop(sid)


_test_delete_file_windows_fallback_refuses_nul_and_surrogate()


def _test_delete_file_symlink_removes_link_not_target():
    """#325's own requirement: a symlink INSIDE the workspace pointing
    OUTSIDE it must be deletable — removing the link, never following it —
    and the external target must survive untouched. `_jail` alone cannot do
    this: its resolve() follows the final path component too, so a link
    pointing outside reads as the call itself "escaping" and `_jail` would
    refuse the delete outright — correct for a read/write, wrong for a
    delete. `_jail_nofollow` exists for exactly this case."""
    if not _can_symlink():
        print("SKIP delete symlink test (no symlink privilege on this host)")
        return
    sid = _new_session()
    outside_dir = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-quota-symlink-target-"))
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("do not touch")
    try:
        link = sessions._session_dir(sid) / "escape-link"
        link.symlink_to(outside_file)

        r = sessions.delete_file(sid, "escape-link")
        check("delete symlink: a symlink pointing OUTSIDE the workspace is "
              "deleted, not refused as an 'escape'",
              r.get("ok") is True, f"-> {r}")
        check("delete symlink: result reports it deleted a symlink, not a file",
              r.get("deleted") == "symlink", f"-> {r}")
        check("delete symlink: the link entry is gone from the workspace",
              not link.exists() and not link.is_symlink())
        check("delete symlink: the OUTSIDE target file SURVIVES, untouched",
              outside_file.exists() and outside_file.read_text() == "do not touch")
    finally:
        sessions.stop(sid)
        shutil.rmtree(outside_dir, ignore_errors=True)


_test_delete_file_symlink_removes_link_not_target()


# ── GH #325 fix round 2 (grok verify-security) ──────────────────────────────
def _test_delete_file_parent_swap_toctou():
    """HIGH (fix round 2 — the IMMEDIATE-parent case only; see fix round
    3's nested-parent test right after this one for the case round 2's
    own fix missed): a racing session WORKER can swap a parent path
    component for a symlink pointing OUTSIDE the workspace between an
    earlier resolve/observation and the eventual unlink — the server
    process is not Landlocked (see `_write_nofollow`'s own docstring), so
    nothing stops it. Manually confirmed against the round-1 commit
    (8bb24fa) before round 2's fix: the identical race below, run against
    that commit's `sessions.py` directly, deleted the OUTSIDE target on
    its very first escape. Round 2 closed exactly this shape (a symlink
    swapped in for the file's DIRECT parent) via `_unlink_pinned`'s single
    `openat`-per-component walk from the workspace root (round 3) — an
    intermediate symlink at ANY position fails that hop's own open with
    `ELOOP`, which for a one-component-deep path like `d/passwd` means
    the very first (and only) hop already refuses it.

    Races directly against `sessions.delete_file` rather than through a
    real sandboxed `session_run` worker: the vulnerable window is entirely
    inside sessions.py, so a background thread racing repeated
    `delete_file` calls exercises the identical window the reviewer's
    worker-thread sketch does, without the cost of a real subprocess per
    iteration. `d/passwd` never legitimately exists inside the workspace —
    only OUTSIDE it — so ANY successful delete of it during the race is
    proof the swapped parent was followed. Only Unix-relevant
    (`_DIR_FD_SUPPORTED`): the fix's `dir_fd` fast path is what this test
    exercises, and Windows keeps the same documented residual `_jail`/
    `_write_nofollow` already accept there.
    """
    if not _can_symlink():
        print("SKIP TOCTOU parent-swap test (no symlink privilege on this host)")
        return
    if not sessions._DIR_FD_SUPPORTED:
        print("SKIP TOCTOU parent-swap test (no dir_fd support on this platform "
              "— documented residual, see _jail_nofollow's docstring)")
        return
    sid = _new_session()
    outside_dir = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-quota-toctou-target-"))
    outside_file = outside_dir / "passwd"
    outside_file.write_text("do not touch")
    try:
        base = sessions._session_dir(sid)
        d_path = base / "d"
        d_real = base / "d.real"
        d_path.mkdir()
        (d_path / "inside.txt").write_text("inside")  # a real, unrelated file

        stop = threading.Event()

        def _race():
            while not stop.is_set():
                try:
                    d_path.rename(d_real)
                    d_path.symlink_to(outside_dir)
                    d_path.unlink()
                    d_real.rename(d_path)
                except OSError:
                    pass  # expected: the two threads step on each other's renames

        racer = threading.Thread(target=_race, daemon=True)
        racer.start()
        try:
            iterations = 4000
            escaped = 0
            for _ in range(iterations):
                r = sessions.delete_file(sid, "d/passwd")
                if r.get("ok") is True:
                    escaped += 1
        finally:
            stop.set()
            racer.join(timeout=5)

        check(f"TOCTOU parent-swap: {iterations} racing delete_file calls, "
              "zero reported deleting the OUTSIDE file",
              escaped == 0, f"-> {escaped} call(s) reported ok:true")
        check("TOCTOU parent-swap: the OUTSIDE target survives, untouched",
              outside_file.exists() and outside_file.read_text() == "do not touch")
    finally:
        # 'd' may currently be a dangling symlink mid-race; restore a real
        # directory first so session_stop's own workspace cleanup does not
        # itself have to reason about a symlink named 'd'.
        try:
            if (base / "d").is_symlink():
                (base / "d").unlink()
            if not (base / "d").exists() and (base / "d.real").is_dir():
                (base / "d.real").rename(base / "d")
        except OSError:
            pass
        sessions.stop(sid)
        shutil.rmtree(outside_dir, ignore_errors=True)


_test_delete_file_parent_swap_toctou()


def _test_delete_file_nested_parent_swap_toctou():
    """HIGH, fix round 3 (grok verify-security): round 2's "pin" was
    `parent.stat()` followed by a SEPARATE `os.open(parent, O_NOFOLLOW)` —
    both re-walk the path STRING, and `O_NOFOLLOW` refuses a symlink only
    at the FINAL component of each of THOSE opens. For a NESTED path
    (`a/b/passwd`, `a` swapped) the immediate parent (`b`) is never itself
    a symlink, so both re-walks follow the swapped `a` and land on the
    SAME attacker directory, agree with each other, and the delete
    proceeds. Manually confirmed against round 2's commit (559d3d0) before
    writing this fix: the identical race below, run against that commit's
    `sessions.py` directly, deleted a file OUTSIDE the workspace (escaped
    1/6000). Round 3's `_unlink_pinned` walks `openat`-style ONE component
    at a time from a pinned workspace-root fd — `a` becoming a symlink
    fails ITS OWN hop's open with `ELOOP`, checked at the exact hop where
    it matters, so there is no later hop left that could silently follow
    it.

    Same shape as the immediate-parent test above, one directory deeper:
    `a/etc/passwd` never legitimately exists inside the workspace (only
    `outside_dir/etc/passwd` does), so any successful delete during the
    race is proof `a` was followed through as a symlink.
    """
    if not _can_symlink():
        print("SKIP TOCTOU nested-parent-swap test (no symlink privilege on this host)")
        return
    if not sessions._DIR_FD_SUPPORTED:
        print("SKIP TOCTOU nested-parent-swap test (no dir_fd support on this "
              "platform — documented residual, see _jail_nofollow's docstring)")
        return
    sid = _new_session()
    outside_dir = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-quota-nested-toctou-target-"))
    (outside_dir / "etc").mkdir()
    outside_file = outside_dir / "etc" / "passwd"
    outside_file.write_text("do not touch")
    try:
        base = sessions._session_dir(sid)
        a_path = base / "a"
        a_real = base / "a.real"
        (a_path / "etc").mkdir(parents=True)  # a REAL, unrelated nested dir

        stop = threading.Event()

        def _race():
            while not stop.is_set():
                try:
                    a_path.rename(a_real)
                    a_path.symlink_to(outside_dir)
                    a_path.unlink()
                    a_real.rename(a_path)
                except OSError:
                    pass  # expected: the two threads step on each other's renames

        racer = threading.Thread(target=_race, daemon=True)
        racer.start()
        try:
            iterations = 6000
            escaped = 0
            for _ in range(iterations):
                r = sessions.delete_file(sid, "a/etc/passwd")
                if r.get("ok") is True:
                    escaped += 1
        finally:
            stop.set()
            racer.join(timeout=5)

        check(f"TOCTOU nested-parent-swap: {iterations} racing delete_file calls "
              "on a/etc/passwd, zero reported deleting the OUTSIDE file",
              escaped == 0, f"-> {escaped} call(s) reported ok:true")
        check("TOCTOU nested-parent-swap: the OUTSIDE target survives, untouched",
              outside_file.exists() and outside_file.read_text() == "do not touch")
    finally:
        try:
            if (base / "a").is_symlink():
                (base / "a").unlink()
            if not (base / "a").exists() and (base / "a.real").is_dir():
                (base / "a.real").rename(base / "a")
        except OSError:
            pass
        sessions.stop(sid)
        shutil.rmtree(outside_dir, ignore_errors=True)


_test_delete_file_nested_parent_swap_toctou()


def _test_delete_file_refuses_expired_marker_and_pycache():
    """MEDIUM: the idle-expiry marker is a plain regular file
    `_workspace_scan` already hides from `session_artifacts`, but pre-fix
    `_is_runner_internal` never refused DELETING it — doing so would make
    the next `execute()` miss `_is_expired_on_disk` and silently respawn a
    worker for a session that was already reaped. LOW: `__pycache__`/
    `*.pyc` get the same treatment, alignment rather than a host-facing
    risk. LOW, fix round 3 (grok verify-security): that pycache/pyc check
    was still a byte-exact match, unlike the reserved-name check, which
    already casefolds (fix round 2) — `.PYC`/`__PYCACHE__` (a real
    spelling on NTFS, which preserves but does not enforce case) slipped
    through; the casefolded variants below prove it no longer does."""
    sid = _new_session()
    try:
        marker = sessions._session_dir(sid) / sessions._EXPIRED_MARKER_NAME
        marker.write_text("expired")
        r_marker = sessions.delete_file(sid, sessions._EXPIRED_MARKER_NAME)
        check("delete: the idle-expiry marker is refused as runner-internal",
              r_marker.get("ok") is False
              and r_marker.get("code") == errors.PERMISSION_DENIED,
              f"-> {r_marker}")
        check("delete: the marker file itself was NOT removed",
              marker.exists())

        pycache_file = sessions._session_dir(sid) / "__pycache__" / "mod.cpython-312.pyc"
        pycache_file.parent.mkdir(parents=True, exist_ok=True)
        pycache_file.write_bytes(b"\x00")
        r_pycache = sessions.delete_file(sid, "__pycache__/mod.cpython-312.pyc")
        check("delete: a __pycache__ entry is refused as runner-internal",
              r_pycache.get("ok") is False
              and r_pycache.get("code") == errors.PERMISSION_DENIED,
              f"-> {r_pycache}")

        pyc_at_root = sessions._session_dir(sid) / "mod.pyc"
        pyc_at_root.write_bytes(b"\x00")
        r_pyc = sessions.delete_file(sid, "mod.pyc")
        check("delete: a root-level *.pyc file is refused as runner-internal",
              r_pyc.get("ok") is False
              and r_pyc.get("code") == errors.PERMISSION_DENIED,
              f"-> {r_pyc}")

        upper_pycache = sessions._session_dir(sid) / "__PYCACHE__" / "mod.cpython-312.PYC"
        upper_pycache.parent.mkdir(parents=True, exist_ok=True)
        upper_pycache.write_bytes(b"\x00")
        r_upper_pycache = sessions.delete_file(sid, "__PYCACHE__/mod.cpython-312.PYC")
        check("delete: an UPPERCASE __PYCACHE__ entry is refused too (casefold)",
              r_upper_pycache.get("ok") is False
              and r_upper_pycache.get("code") == errors.PERMISSION_DENIED,
              f"-> {r_upper_pycache}")

        upper_pyc_at_root = sessions._session_dir(sid) / "MOD.PYC"
        upper_pyc_at_root.write_bytes(b"\x00")
        r_upper_pyc = sessions.delete_file(sid, "MOD.PYC")
        check("delete: an UPPERCASE root-level *.PYC file is refused too (casefold)",
              r_upper_pyc.get("ok") is False
              and r_upper_pyc.get("code") == errors.PERMISSION_DENIED,
              f"-> {r_upper_pyc}")
    finally:
        sessions.stop(sid)


_test_delete_file_refuses_expired_marker_and_pycache()


def _test_prefix_dir_normalized_denylist():
    """MEDIUM, fix round 4 (grok verify-security, in-workspace only —
    case-insensitive volumes): the prefix-directory check
    (`.codecalc-run`/`.codecalc-spill`) used to be a bare `in (...)`
    exact-string match, while the ROOT-level reserved-file check right
    next to it (`_reserved_root_name`) already casefolded and stripped a
    trailing `.`/` `. On a case-insensitive volume, `.CODECALC-RUN` and
    `.codecalc-run.` name the SAME on-disk directory as `.codecalc-run`
    but read as different strings to a bare `in` check — reaching the
    runner scratch directory past what `session_artifacts` shows. Both
    now go through the shared `_normalized_component_matches` the
    reserved-file check uses. Pure denylist checks (via
    `_is_runner_internal` directly, and end to end through
    `delete_file`), so provable on Linux without a case-insensitive
    filesystem, the same reasoning the round-2 case/trailing-dot test
    above uses."""
    variants = [
        sessions._RUNNER_SCRATCH_DIRNAME,
        sessions._RUNNER_SCRATCH_DIRNAME.upper(),
        sessions._RUNNER_SCRATCH_DIRNAME.swapcase(),
        sessions._RUNNER_SCRATCH_DIRNAME + ".",
    ]
    for v in variants:
        check(f"_is_runner_internal: {v!r}/main.py is refused (runner scratch prefix)",
              sessions._is_runner_internal((v, "main.py")), f"-> False for {v!r}")

    spill_variants = [
        sessions._SPILL_DIRNAME,
        sessions._SPILL_DIRNAME.upper(),
        sessions._SPILL_DIRNAME + ".",
    ]
    for v in spill_variants:
        check(f"_is_runner_internal: {v!r}/x is refused (spill dir prefix)",
              sessions._is_runner_internal((v, "x")), f"-> False for {v!r}")

    check("_is_runner_internal: an ordinary prefix directory is NOT refused",
          not sessions._is_runner_internal(("mydir", "file.txt")))

    # End to end through delete_file(): the case-mangled/trailing-dot
    # spelling of the prefix directory must be refused the same way the
    # exact spelling already is.
    sid = _new_session()
    try:
        r = sessions.delete_file(sid, f"{sessions._RUNNER_SCRATCH_DIRNAME.upper()}/main.py")
        check("delete: an UPPERCASE .codecalc-run/... path is refused "
              "end to end through delete_file()",
              r.get("ok") is False and r.get("code") == errors.PERMISSION_DENIED,
              f"-> {r}")
        r2 = sessions.delete_file(sid, f"{sessions._RUNNER_SCRATCH_DIRNAME}./main.py")
        check("delete: a trailing-dot .codecalc-run./... path is refused "
              "end to end through delete_file()",
              r2.get("ok") is False and r2.get("code") == errors.PERMISSION_DENIED,
              f"-> {r2}")
    finally:
        sessions.stop(sid)


_test_prefix_dir_normalized_denylist()


def _test_reserved_root_name_denylist_bypass():
    """MEDIUM: a plain `==` denylist lets `.CODECALC-SESSION-LOCK` through
    on a case-insensitive volume (default APFS, NTFS — the SAME file as
    `.codecalc-session-lock` there) and `.codecalc-session-lock.` /
    `.codecalc-session-lock ` through on Windows, which strips a trailing
    `.`/` ` off a filename at the API boundary. `_reserved_root_name` is
    checked directly here as a pure denylist function — host-independent by
    design (see its own docstring), so both spellings are provably caught
    on Linux CI without needing a case-insensitive filesystem to prove it."""
    variants = [
        sessions._LOCK_FILE_NAME,
        sessions._LOCK_FILE_NAME.upper(),
        sessions._LOCK_FILE_NAME.swapcase(),
        sessions._LOCK_FILE_NAME + ".",
        sessions._LOCK_FILE_NAME + " ",
        sessions._LOCK_FILE_NAME.upper() + ".",
    ]
    for v in variants:
        check(f"_reserved_root_name: {v!r} matches the lock file",
              sessions._reserved_root_name(v), f"-> False for {v!r}")

    marker_variants = [
        sessions._EXPIRED_MARKER_NAME,
        sessions._EXPIRED_MARKER_NAME.upper(),
        sessions._EXPIRED_MARKER_NAME + ".",
    ]
    for v in marker_variants:
        check(f"_reserved_root_name: {v!r} matches the expiry marker",
              sessions._reserved_root_name(v), f"-> False for {v!r}")

    check("_reserved_root_name: an ordinary filename does NOT match",
          not sessions._reserved_root_name("notes.txt"))
    check("_reserved_root_name: a filename merely CONTAINING the reserved "
          "name does NOT match (no accidental substring match)",
          not sessions._reserved_root_name("prefix." + sessions._LOCK_FILE_NAME))

    # End to end through delete_file(), not just the denylist helper: the
    # case-mangled spelling must be refused the same way the exact name is.
    sid = _new_session()
    try:
        r = sessions.delete_file(sid, sessions._LOCK_FILE_NAME.upper())
        check("delete: an UPPERCASE spelling of the lock file is refused "
              "end to end through delete_file()",
              r.get("ok") is False and r.get("code") == errors.PERMISSION_DENIED,
              f"-> {r}")
        r2 = sessions.delete_file(sid, sessions._LOCK_FILE_NAME + ".")
        check("delete: a trailing-dot spelling of the lock file is refused "
              "end to end through delete_file()",
              r2.get("ok") is False and r2.get("code") == errors.PERMISSION_DENIED,
              f"-> {r2}")
    finally:
        sessions.stop(sid)


_test_reserved_root_name_denylist_bypass()


def _test_eight_dot_three_shape_denylist():
    """MEDIUM, fix round 3 (grok verify-security): a Windows 8.3 short name
    (`CODECA~1` for `.codecalc-session-lock`, `LOCKFI~2.TXT`) is a THIRD
    on-disk spelling of a reserved file that bears no textual relationship
    to the long name at all — casefold/trailing-dot-strip cannot enumerate
    it. `_reserved_root_name` refuses anything shaped like a short name
    (a literal `~` immediately followed by a digit) at the session root
    outright, regardless of what it would otherwise resolve to. Pure
    string-SHAPE check, so provable on Linux without an actual NTFS/FAT
    short-name filesystem — `delete_file`'s own long-name resolution
    (`_final_component_long_name`) is the platform-specific half this
    cannot exercise here (it needs a real Windows short-name alias to do
    anything at all; see that function's own docstring)."""
    eight_dot_three_names = [
        "CODECA~1",
        "CODECA~1.TXT",
        "LOCKFI~2",
        "a~9",
        "~1",
    ]
    for name in eight_dot_three_names:
        check(f"_reserved_root_name: 8.3-shaped {name!r} is refused at the root",
              sessions._reserved_root_name(name), f"-> False for {name!r}")

    not_eight_dot_three = [
        "notes.txt",
        "backup-1.txt",  # hyphen, not tilde
        "my~file.txt",   # tilde with no digit immediately after
        "~",              # tilde alone, no digit
    ]
    for name in not_eight_dot_three:
        check(f"_reserved_root_name: ordinary {name!r} is NOT refused as 8.3-shaped",
              not sessions._reserved_root_name(name), f"-> True for {name!r}")

    # End to end through delete_file(): an 8.3-shaped root name is refused
    # even though it is not literally the lock/marker spelling.
    sid = _new_session()
    try:
        r = sessions.delete_file(sid, "CODECA~1")
        check("delete: an 8.3-shaped root name is refused end to end "
              "through delete_file()",
              r.get("ok") is False and r.get("code") == errors.PERMISSION_DENIED,
              f"-> {r}")
    finally:
        sessions.stop(sid)


_test_eight_dot_three_shape_denylist()


print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== SESSION DISK QUOTAS ARE ENFORCED AND DISCOVERABLE ===")
sys.exit(1 if FAILS else 0)
