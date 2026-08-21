"""Per-session and global disk quotas for sessions (THE-894).

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
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys

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


print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== SESSION DISK QUOTAS ARE ENFORCED AND DISCOVERABLE ===")
sys.exit(1 if FAILS else 0)
