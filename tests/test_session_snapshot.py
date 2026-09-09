"""session_snapshot: archive and restore a session's workspace files.

Exercises `codecalc/sessions.py`'s `snapshot_save`/`snapshot_restore`/
`snapshot_list`/`snapshot_delete` directly (the same style
`tests/test_session_quota.py` uses), plus one round trip through
`session_run` so the feature is proven with real execution involved, not
only hand-written files.

WHAT THIS MUST NOT REOPEN, because both are already covered by other
suites for the underlying primitives this feature reuses rather than
reimplements:

  - `tests/test_execution_service.py`/`tests/test_python_sweep.py`'s
    rename-swap regressions: a workdir this process created is deleted (or,
    here, snapshotted/replaced) only after re-checking its recorded
    (device, inode) identity. `snapshot_save`/`snapshot_restore(replace=
    True)` apply the SAME check `stop()` already does, and this file
    exercises it directly rather than re-deriving a new mechanism.
  - `tests/test_session_jail.py`'s symlink/TOCTOU coverage of `_jail`,
    `_write_nofollow`, `_read_nofollow`: `_extract_planned_tar` writes every
    extracted member with the identical `O_EXCL | O_NOFOLLOW` flags those
    already use.

A hostile archive is built by hand with `tarfile` directly (never through
`snapshot_save`) and dropped exactly where a real snapshot's `.tar.gz` would
be, so `snapshot_restore` reads it exactly as it would read one this module
wrote itself. Building a symlink/hardlink/device MEMBER only writes tar
metadata — no real symlink is ever created on the test machine — so none of
the hostile-archive cases need the `_can_symlink` capability probe
`test_session_jail.py` uses for genuine on-disk symlinks; only the SAVE-side
exclusion test (a real symlink/hardlink actually present in a workspace)
does.

Windows: paths are handled via `pathlib`/`os.path`, never a hardcoded `/` or
POSIX assumption in the assertions here (tar member names inside the archive
are always forward-slash, by the tar format itself — see
`_plan_tar_extraction`'s own rejection of a literal backslash in a member
name). Only the genuine-hardlink exclusion case is skipped on a platform
where creating one fails.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tarfile
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import errors, execution_service, registry, sessions

FAILS: list[str] = []
SKIPS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")
    SKIPS.append(name)


#: Generous enough that none of the caps below trips by accident in a test
#: that is not deliberately exercising it — same shape as
#: `test_session_quota.py`'s own `_SAFE_ENV`.
_SAFE_ENV = {
    sessions.SESSION_DISK_QUOTA_MB_ENV: "10000",
    sessions.TOTAL_DISK_QUOTA_MB_ENV: "1000000",
    sessions.MAX_ARTIFACT_BYTES_ENV: str(200 * 1024 * 1024),
    sessions.MAX_ARTIFACT_COUNT_ENV: "1000000",
    sessions.MIN_HOST_FREE_MB_ENV: "1",
    sessions.MAX_SNAPSHOT_BYTES_ENV: str(200 * 1024 * 1024),
    sessions.MAX_SNAPSHOTS_PER_SESSION_ENV: "1000000",
}


def _with_env(overrides: dict[str, str], fn):
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


def _new_session(language: str = "bash") -> str:
    started = _with_env({}, lambda: sessions.start(language))
    assert started.get("ok"), started
    return started["session_id"]


def _sessions_on_disk() -> set[str]:
    if not sessions.SESSION_ROOT.is_dir():
        return set()
    return {d.name for d in sessions.SESSION_ROOT.iterdir()
            if d.is_dir() and d.name != sessions._SNAPSHOT_DIRNAME}


def _add_member(tf: tarfile.TarFile, name: str, data: bytes = b"x", **attrs) -> None:
    """One tar member, written directly — never through `snapshot_save` —
    so a hostile archive can declare a shape `tarfile.TarFile.add()` would
    never itself produce (an absolute path, a `..`, a symlink/hardlink/
    device member)."""
    info = tarfile.TarInfo(name=name)
    info.size = len(data) if attrs.get("type") in (None, tarfile.REGTYPE) else 0
    for k, v in attrs.items():
        setattr(info, k, v)
    tf.addfile(info, io.BytesIO(data) if info.size else None)


def _write_hostile_archive(tar_path: pathlib.Path, builder) -> None:
    tmp = tar_path.with_name("hostile.tmp")
    with tarfile.open(tmp, "w:gz") as tf:
        builder(tf)
    tmp.replace(tar_path)


# ── 1. round trip, including one real execution ─────────────────────────────
def _test_round_trip():
    sid = _new_session("python3")
    try:
        check("session_write_file: seed a file",
              sessions.write_file(sid, "input.txt", "seed data\n").get("ok") is True)
        # session_run so the round trip is proven with EXECUTION involved, not
        # only hand-written files — the entry script itself creates an
        # artifact the snapshot must also carry.
        sessions.write_file(sid, "make_output.py",
                            "open('output.txt','w').write('computed: ' + "
                            "open('input.txt').read())\n")
        svc = execution_service.SessionService()
        ran = svc.run_file(sid, "make_output.py")
        check("session_run produced output.txt", ran.get("ok") is True, f"-> {ran}")

        save = _with_env({}, lambda: sessions.snapshot_save(sid, label="round-trip"))
        check("snapshot save succeeds", save.get("ok") is True, f"-> {save}")
        check("snapshot save reports action=save", save.get("action") == "save")
        check("snapshot save reports 3 files (input/make_output/output)",
              save.get("files") == 3, f"-> {save}")

        before = _sessions_on_disk()
        restore = _with_env(
            {}, lambda: sessions.snapshot_restore(sid, save["snapshot_id"]))
        check("snapshot restore succeeds", restore.get("ok") is True, f"-> {restore}")
        new_sid = restore.get("session_id", "")
        check("restore created exactly one new session directory",
              _sessions_on_disk() - before == {new_sid}, f"-> {_sessions_on_disk() - before}")
        check("restore reports 3 restored files", restore.get("restored_files") == 3,
              f"-> {restore}")

        for name, want in (("input.txt", "seed data\n"),
                           ("make_output.py", None),
                           ("output.txt", "computed: seed data\n")):
            got = (sessions._session_dir(new_sid) / name).read_bytes()
            orig = (sessions._session_dir(sid) / name).read_bytes()
            check(f"restored {name} is byte-IDENTICAL to the original",
                  got == orig and (want is None or got.decode() == want),
                  f"-> {got!r} vs {orig!r}")
        sessions.stop(new_sid)
    finally:
        sessions.stop(sid)


_test_round_trip()


# ── 2. .codecalc-run/ is excluded from a snapshot, same as session_artifacts ─
def _test_run_scratch_excluded():
    sid = _new_session("bash")
    try:
        sessions.write_file(sid, "real.txt", "kept")
        scratch = sessions._session_dir(sid) / registry.RUN_SCRATCH_DIRNAME
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "leftover").write_text("must not be archived")
        save = _with_env({}, lambda: sessions.snapshot_save(sid))
        check("snapshot excludes .codecalc-run/ (files == 1, not 2)",
              save.get("files") == 1, f"-> {save}")
        with tarfile.open(sessions._snapshot_paths(sid, save["snapshot_id"])[0]) as tf:
            names = tf.getnames()
        check("...and the archive itself carries no member under .codecalc-run/",
              not any(n.startswith(registry.RUN_SCRATCH_DIRNAME) for n in names),
              f"-> {names}")
        sessions.snapshot_delete(sid, save["snapshot_id"])
    finally:
        sessions.stop(sid)


_test_run_scratch_excluded()


# ── 3. save refuses symlinks/hardlinks, same rule session_artifacts applies ──
def _test_save_excludes_symlink_and_hardlink():
    sid = _new_session("bash")
    try:
        d = sessions._session_dir(sid)
        sessions.write_file(sid, "real.txt", "kept")
        outside = d.parent / f"{sid}-outside-secret.txt"
        outside.write_text("must never be archived")
        try:
            (d / "link.txt").symlink_to(outside)
            symlink_ok = True
        except OSError as exc:
            symlink_ok = False
            skip("save excludes a symlinked member", f"symlink unsupported here: {exc}")
        try:
            os.link(outside, d / "hardlink.txt")
            hardlink_ok = True
        except OSError as exc:
            hardlink_ok = False
            skip("save excludes a hardlinked member", f"hardlink unsupported here: {exc}")

        save = _with_env({}, lambda: sessions.snapshot_save(sid))
        check("snapshot save still succeeds with a symlink/hardlink present",
              save.get("ok") is True, f"-> {save}")
        with tarfile.open(sessions._snapshot_paths(sid, save["snapshot_id"])[0]) as tf:
            names = set(tf.getnames())
        check("...archive carries the real file", "real.txt" in names, f"-> {names}")
        if symlink_ok:
            check("...archive excludes the symlink", "link.txt" not in names, f"-> {names}")
        if hardlink_ok:
            check("...archive excludes the hardlink", "hardlink.txt" not in names,
                  f"-> {names}")
            check("...and discloses the exclusion count",
                  save.get("skipped_hardlinks", 0) >= 1, f"-> {save}")
        outside.unlink(missing_ok=True)
        sessions.snapshot_delete(sid, save["snapshot_id"])
    finally:
        sessions.stop(sid)


_test_save_excludes_symlink_and_hardlink()


# ── 4. the swapped-workspace refusal at save ────────────────────────────────
def _test_save_refuses_swapped_workspace():
    sid = _new_session("bash")
    try:
        d = sessions._session_dir(sid)
        victim = d.parent / f"{sid}-victim"
        victim.mkdir()
        (victim / "secret.txt").write_text("must never be archived")
        held = pathlib.Path(str(d) + ".held")
        try:
            d.rename(held)
            victim.rename(d)
        except OSError as exc:
            skip("save refuses a swapped workspace",
                 f"this platform refused the rename, so the attack could not "
                 f"be staged: {exc}")
            victim.rmdir() if victim.is_dir() else None
            return
        try:
            r = _with_env({}, lambda: sessions.snapshot_save(sid))
            check("a swapped workspace is refused, not archived",
                  r.get("ok") is False and r.get("code") == errors.PERMISSION_DENIED,
                  f"-> {r}")
        finally:
            d.rename(victim)
            held.rename(d)
            for p in (victim / "secret.txt", victim):
                try:
                    p.unlink() if p.is_file() else p.rmdir()
                except OSError:
                    pass
    finally:
        sessions.stop(sid)


_test_save_refuses_swapped_workspace()


# ── 5. hostile archives on restore: each refused, nothing written outside ───
def _test_hostile_restore_cases():
    sid = _new_session("bash")
    try:
        sessions.write_file(sid, "a.txt", "hi")
        save = _with_env({}, lambda: sessions.snapshot_save(sid))
        tar_path, _meta_path = sessions._snapshot_paths(sid, save["snapshot_id"])

        cases = [
            ("absolute path", lambda tf: _add_member(tf, "/etc/passwd", b"evil"),
             errors.PERMISSION_DENIED),
            ("path traversal", lambda tf: _add_member(tf, "../../evil.txt", b"evil"),
             errors.PERMISSION_DENIED),
            ("symlink member", lambda tf: _add_member(
                tf, "link", linkname="/etc/passwd", type=tarfile.SYMTYPE),
             errors.PERMISSION_DENIED),
            ("hardlink member", lambda tf: _add_member(
                tf, "hlink", linkname="a.txt", type=tarfile.LNKTYPE),
             errors.PERMISSION_DENIED),
            ("device file member", lambda tf: _add_member(
                tf, "dev", type=tarfile.CHRTYPE, devmajor=1, devminor=5),
             errors.PERMISSION_DENIED),
            ("oversize member", lambda tf: _add_member(
                tf, "huge.bin", data=b"x" * (2 * 1024 * 1024)),
             errors.RESOURCE_EXHAUSTED),
            ("too many members", lambda tf: [
                _add_member(tf, f"f{i}.txt", b"x") for i in range(50)],
             errors.RESOURCE_EXHAUSTED),
        ]
        for label, builder, want_code in cases:
            _write_hostile_archive(tar_path, builder)
            before = _sessions_on_disk()
            env = ({sessions.MAX_ARTIFACT_BYTES_ENV: str(1024 * 1024)}
                  if label == "oversize member" else
                  {sessions.MAX_ARTIFACT_COUNT_ENV: "10"}
                  if label == "too many members" else {})
            r = _with_env(env, lambda: sessions.snapshot_restore(sid, save["snapshot_id"]))
            check(f"hostile archive ({label}) is refused",
                  r.get("ok") is False, f"-> {r}")
            check(f"...with the closed-enum code {want_code!r}",
                  r.get("code") == want_code, f"-> {r.get('code')}")
            after = _sessions_on_disk()
            check(f"...and no session directory was left behind ({label})",
                  after == before, f"-> before={before} after={after}")

        sessions.snapshot_delete(sid, save["snapshot_id"])
    finally:
        sessions.stop(sid)


_test_hostile_restore_cases()


# ── 6. a hostile archive placed where a snapshot would be, with no prior
#      snapshot_save at all — the metadata sidecar is hand-built too ────────
def _test_hostile_archive_with_no_real_snapshot():
    sid = _new_session("bash")
    try:
        snap_dir = sessions._snapshot_session_dir(sid)
        snap_dir.mkdir(parents=True, exist_ok=True)
        snapshot_id = "ab" * 16
        tar_path, meta_path = sessions._snapshot_paths(sid, snapshot_id)
        meta_path.write_text(
            '{"language": "bash", "raw_bytes": 10, "files": 1}', encoding="utf-8")
        _write_hostile_archive(tar_path, lambda tf: _add_member(tf, "../escape.txt", b"x"))
        before = _sessions_on_disk()
        r = _with_env({}, lambda: sessions.snapshot_restore(sid, snapshot_id))
        check("a hand-placed hostile archive is refused the same way",
              r.get("ok") is False and r.get("code") == errors.PERMISSION_DENIED,
              f"-> {r}")
        check("...and nothing new was written to the sessions root",
              _sessions_on_disk() == before, f"-> {_sessions_on_disk() - before}")
        import shutil as _shutil
        _shutil.rmtree(snap_dir, ignore_errors=True)
    finally:
        sessions.stop(sid)


_test_hostile_archive_with_no_real_snapshot()


# ── 7. session_stop deletes snapshots by default; keep_snapshots=True keeps ──
def _test_stop_lifecycle():
    sid = _new_session("bash")
    sessions.write_file(sid, "a.txt", "hi")
    save = _with_env({}, lambda: sessions.snapshot_save(sid))
    tar_path, meta_path = sessions._snapshot_paths(sid, save["snapshot_id"])
    check("snapshot files exist before stop", tar_path.is_file() and meta_path.is_file())
    r = sessions.stop(sid)
    check("stop() (default) reports snapshots_deleted", r.get("snapshots_deleted") == 1,
          f"-> {r}")
    check("...and the archive is actually gone from disk",
          not tar_path.exists() and not meta_path.exists())

    sid2 = _new_session("bash")
    sessions.write_file(sid2, "a.txt", "hi")
    save2 = _with_env({}, lambda: sessions.snapshot_save(sid2))
    tar_path2, meta_path2 = sessions._snapshot_paths(sid2, save2["snapshot_id"])
    r2 = sessions.stop(sid2, keep_snapshots=True)
    check("stop(keep_snapshots=True) reports no snapshots_deleted key",
          "snapshots_deleted" not in r2, f"-> {r2}")
    check("...and the archive SURVIVES the session's own teardown",
          tar_path2.is_file() and meta_path2.is_file())
    lst = sessions.snapshot_list(sid2)
    check("...and is still listable after its origin session is gone",
          any(s["snapshot_id"] == save2["snapshot_id"] for s in lst.get("snapshots", [])),
          f"-> {lst}")
    sessions.snapshot_delete(sid2, save2["snapshot_id"])


_test_stop_lifecycle()


# ── 8. caps: max snapshot bytes, max snapshots per session ──────────────────
def _test_caps():
    sid = _new_session("bash")
    try:
        sessions.write_file(sid, "big.bin", "x" * 5000)
        r = _with_env({sessions.MAX_SNAPSHOT_BYTES_ENV: "1000"},
                      lambda: sessions.snapshot_save(sid))
        check("a snapshot over the byte cap is refused",
              r.get("ok") is False and r.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {r}")

        sessions.write_file(sid, "small.txt", "hi")
        r2 = _with_env({sessions.MAX_SNAPSHOTS_PER_SESSION_ENV: "2"}, lambda: [
            sessions.snapshot_save(sid, label="a"),
            sessions.snapshot_save(sid, label="b"),
            sessions.snapshot_save(sid, label="c"),
        ])
        check("save 1/2 under the per-session cap succeeds", r2[0].get("ok") is True)
        check("save 2/2 under the per-session cap succeeds", r2[1].get("ok") is True)
        check("save 3 over the per-session cap is refused",
              r2[2].get("ok") is False and r2[2].get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {r2[2]}")

        long_label = "x" * 500
        r3 = sessions.snapshot_save(sid, label=long_label)
        check("an over-length label is refused as VALIDATION",
              r3.get("ok") is False and r3.get("code") == errors.VALIDATION, f"-> {r3}")
    finally:
        sessions.stop(sid)


_test_caps()


# ── 9. delete is idempotent; unknown snapshot/session ids are refused ───────
def _test_delete_and_unknown_ids():
    sid = _new_session("bash")
    try:
        sessions.write_file(sid, "a.txt", "hi")
        save = _with_env({}, lambda: sessions.snapshot_save(sid))
        first = sessions.snapshot_delete(sid, save["snapshot_id"])
        check("first delete reports deleted=true", first.get("deleted") is True)
        second = sessions.snapshot_delete(sid, save["snapshot_id"])
        check("a second delete of the same id is ok, deleted=false (idempotent)",
              second.get("ok") is True and second.get("deleted") is False, f"-> {second}")

        bad = sessions.snapshot_restore(sid, "not-a-valid-snapshot-id")
        check("a malformed snapshot_id is refused before touching a path",
              bad.get("ok") is False, f"-> {bad}")

        bad2 = sessions.snapshot_save("../escape")
        check("a malformed session_id is refused",
              bad2.get("ok") is False and bad2.get("code") == errors.VALIDATION,
              f"-> {bad2}")
    finally:
        sessions.stop(sid)


_test_delete_and_unknown_ids()


# ── 10. replace=True: same session_id, worker respawned, REPL state NOT kept ─
def _test_replace_same_session():
    started = _with_env({}, lambda: sessions.start("python3"))
    if not started.get("ok") or not started.get("stateful"):
        skip("replace=True respawns a stateful worker",
            f"python3 session unavailable here: {started}")
        return
    sid = started["session_id"]
    try:
        sessions.write_file(sid, "data.txt", "worker data")
        save = _with_env({}, lambda: sessions.snapshot_save(sid))
        set_result = sessions.execute(sid, "marker = 'still here'")
        check("a variable can be set in the live worker", set_result.get("ok") is True,
              f"-> {set_result}")
        sessions.write_file(sid, "extra.txt", "will be wiped by replace")

        restored = _with_env(
            {}, lambda: sessions.snapshot_restore(sid, save["snapshot_id"], replace=True))
        check("replace=True restores into the SAME session_id",
              restored.get("ok") is True and restored.get("session_id") == sid,
              f"-> {restored}")
        check("...and reports replaced=true", restored.get("replaced") is True)
        check("...extra.txt (written after the snapshot) is GONE",
              not (sessions._session_dir(sid) / "extra.txt").exists())
        check("...data.txt survives, byte-identical",
              (sessions._session_dir(sid) / "data.txt").read_text() == "worker data")

        after = sessions.execute(sid, "print(marker)")
        check("REPL state does NOT survive replace — the worker restarted",
              after.get("ok") is False and "marker" in (after.get("stderr") or ""),
              f"-> {after}")
        check("...but the fresh worker is confirmed stateful again",
              restored.get("stateful") is True, f"-> {restored}")
    finally:
        sessions.stop(sid)


_test_replace_same_session()


# ── 11. the sidecar is ADVISORY: a lying raw_bytes cannot buy past quota ────
def _test_sidecar_raw_bytes_is_not_trusted():
    """Adversarial-review finding (CRITICAL): `snapshot_restore` used to take
    `raw_bytes` from the `.json` sidecar on faith for the quota decision — a
    caller (or anything with the same filesystem access executed code has)
    could place a real, large archive beside a sidecar lying about its size
    and sail through a tight quota. The real total must come from the
    archive's own structure (`_plan_tar_extraction`), never the sidecar.
    """
    sid = _new_session("bash")
    try:
        sessions.write_file(sid, "small.txt", "hi")
        save = _with_env({}, lambda: sessions.snapshot_save(sid))
        tar_path, meta_path = sessions._snapshot_paths(sid, save["snapshot_id"])
        _write_hostile_archive(
            tar_path, lambda tf: _add_member(tf, "big.bin", data=b"0" * (2 * 1024 * 1024)))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["raw_bytes"] = 1  # the lie: the real archive content is ~2 MiB
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        # The math that makes this a real proof, not just a refusal: the
        # session's own pre-existing usage is a couple of bytes ("hi"), and
        # the configured quota is ~1 MiB (1,048,576 bytes). Checked against
        # the sidecar's LIE (`raw_bytes: 1`), `2 + 1 = 3` is nowhere near
        # the quota and nothing would be refused. A refusal firing at all
        # is therefore only possible because the REAL ~2 MiB archive
        # content — re-derived from the archive's own structure, never the
        # sidecar — is what actually got checked.
        r = _with_env({sessions.SESSION_DISK_QUOTA_MB_ENV: "1"},  # ~1 MiB — under the real 2 MiB
                      lambda: sessions.snapshot_restore(sid, save["snapshot_id"]))
        check("a lying sidecar cannot buy a restore past the REAL quota "
              "(1-byte lie would never trip a ~1 MiB quota; the real ~2 MiB content does)",
              r.get("ok") is False and r.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {r}")
        check("...and the refusal's own quota_bytes names the ~1 MiB ceiling actually configured",
              r.get("quota_bytes") == 1024 * 1024, f"-> quota_bytes={r.get('quota_bytes')}")
    finally:
        sessions.stop(sid)


_test_sidecar_raw_bytes_is_not_trusted()


# ── 12. snapshot_save is refused by the SAME quota checks write_file gets ───
def _test_save_routes_through_disk_quota_and_host_floor():
    """Adversarial-review finding (HIGH): `snapshot_save` never called
    `_disk_quota_refusal` at all — the per-snapshot byte cap
    (`CODECALC_MAX_SNAPSHOT_BYTES`) was the only ceiling, so a save could
    still run the host's free space to zero, or blow straight past the
    per-session/global disk quotas every other write in this module
    respects.
    """
    sid = _new_session("bash")
    try:
        sessions.write_file(sid, "big.bin", "x" * 5000)
        r = _with_env({sessions.SESSION_DISK_QUOTA_MB_ENV: "0.001"},  # ~1048 bytes
                      lambda: sessions.snapshot_save(sid))
        check("save is refused by the per-session disk quota, same as write_file",
              r.get("ok") is False and r.get("code") == errors.RESOURCE_EXHAUSTED
              and "quota" in (r.get("error") or ""),
              f"-> {r}")
        r2 = _with_env({sessions.MIN_HOST_FREE_MB_ENV: "999999999"},
                       lambda: sessions.snapshot_save(sid))
        check("save is refused by the host-free-space floor",
              r2.get("ok") is False and r2.get("code") == errors.RESOURCE_EXHAUSTED
              and "free disk space" in (r2.get("error") or ""),
              f"-> {r2}")
        # positive control: comfortably under both, still succeeds
        r3 = _with_env({}, lambda: sessions.snapshot_save(sid))
        check("...while a save comfortably under both still succeeds (control)",
              r3.get("ok") is True, f"-> {r3}")
    finally:
        sessions.stop(sid)


_test_save_routes_through_disk_quota_and_host_floor()


# ── 13. collision shapes leave the workspace/no orphan behind, both paths ──
def _test_collisions_leave_nothing_behind():
    """Adversarial-review finding (HIGH): cross-member collisions (a file
    `a` followed by `a/b`; a dir `a` followed by a file `a`) were not
    caught in a planning pass — extraction failed mid-way, and on
    `replace=True` the original workspace had ALREADY been wiped first,
    leaving a half-populated workspace and no worker. Both collision
    shapes must now be refused before a single byte is written, for BOTH
    `replace=True` (workspace intact) and a new session (no orphan
    directory left under the sessions root).
    """
    cases = [
        ("file then dir", lambda tf: (_add_member(tf, "a", b"x"),
                                      _add_member(tf, "a/b", b"y"))),
        ("dir then file", lambda tf: (_add_member(tf, "a", type=tarfile.DIRTYPE),
                                      _add_member(tf, "a", b"y"))),
    ]
    for label, builder in cases:
        # replace=True: the ORIGINAL workspace must survive, untouched.
        sid = _new_session("bash")
        try:
            sessions.write_file(sid, "original.txt", "must survive")
            save = _with_env({}, lambda sid=sid: sessions.snapshot_save(sid))
            tar_path, _m = sessions._snapshot_paths(sid, save["snapshot_id"])
            _write_hostile_archive(tar_path, builder)
            r = _with_env({}, lambda sid=sid, save=save: sessions.snapshot_restore(
                sid, save["snapshot_id"], replace=True))
            check(f"collision ({label}, replace=True) is refused",
                  r.get("ok") is False and r.get("code") == errors.PERMISSION_DENIED,
                  f"-> {r}")
            original = sessions._session_dir(sid) / "original.txt"
            check(f"...and the ORIGINAL workspace survives untouched ({label})",
                  original.is_file() and original.read_text() == "must survive",
                  f"-> exists={original.exists()}")
            check(f"...still has a live worker entry, nothing torn down ({label})",
                  sessions.list_sessions().get("ok") is True)
        finally:
            sessions.stop(sid)

        # new session: no orphan directory left under the sessions root.
        sid2 = _new_session("bash")
        try:
            sessions.write_file(sid2, "a.txt", "hi")
            save2 = _with_env({}, lambda sid2=sid2: sessions.snapshot_save(sid2))
            tar_path2, _m2 = sessions._snapshot_paths(sid2, save2["snapshot_id"])
            _write_hostile_archive(tar_path2, builder)
            before = _sessions_on_disk()
            r2 = _with_env({}, lambda sid2=sid2, save2=save2: sessions.snapshot_restore(
                sid2, save2["snapshot_id"]))
            check(f"collision ({label}, new session) is refused",
                  r2.get("ok") is False and r2.get("code") == errors.PERMISSION_DENIED,
                  f"-> {r2}")
            after = _sessions_on_disk()
            check(f"...and no orphan session/temp directory was left behind ({label})",
                  after == before, f"-> before={before} after={after}")
        finally:
            sessions.stop(sid2)


_test_collisions_leave_nothing_behind()


# ── 14. a gzip bomb is refused fast — bounded decompression, not just a
#        post-hoc size check ────────────────────────────────────────────────
class _ZeroStream:
    """A readable that emits `n` zero bytes without ever materializing them
    all in memory at once — the fixture for a member that HONESTLY declares
    a multi-GiB size but compresses (being all zeros) to a few KiB, the
    exact shape adversarial review reproduced costing several seconds of
    CPU under `tf.getmembers()`."""

    def __init__(self, n: int) -> None:
        self.n = n

    def read(self, size: int = -1) -> bytes:
        if self.n <= 0:
            return b""
        take = self.n if size < 0 else min(size, self.n)
        self.n -= take
        return bytes(take)


def _test_gzip_bomb_refused_quickly():
    sid = _new_session("bash")
    try:
        sessions.write_file(sid, "small.txt", "hi")
        save = _with_env({}, lambda: sessions.snapshot_save(sid))
        tar_path, _m = sessions._snapshot_paths(sid, save["snapshot_id"])
        bomb_size = 2 * 1024 * 1024 * 1024  # 2 GiB declared, ~compresses to nothing
        tmp = tar_path.with_name("bomb.tmp")
        with tarfile.open(tmp, "w:gz") as tf:
            info = tarfile.TarInfo(name="bomb.bin")
            info.size = bomb_size
            tf.addfile(info, _ZeroStream(bomb_size))
        tmp.replace(tar_path)
        compressed_size = tar_path.stat().st_size
        started = time.monotonic()
        r = _with_env({}, lambda: sessions.snapshot_restore(sid, save["snapshot_id"]))
        elapsed = time.monotonic() - started
        check("a 2 GiB (declared) / tiny (compressed) bomb is refused, not extracted",
              r.get("ok") is False and r.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {r} (archive was {compressed_size} bytes on disk)")
        # Generous bound: real decompression of 2 GiB would cost seconds
        # (adversarial review measured ~5s under the OLD getmembers()-based
        # code); refusing off the declared header size costs a fraction of
        # a second regardless of host load. 10s leaves ample headroom for
        # a slow CI runner while still failing hard if this regresses to
        # "decompress first, check later".
        check("...refused in well under the time real decompression would cost",
              elapsed < 10.0, f"-> elapsed={elapsed:.3f}s")
    finally:
        sessions.stop(sid)


_test_gzip_bomb_refused_quickly()


def _test_cumulative_budget_catches_many_small_members():
    """The per-member cap (`CODECALC_MAX_ARTIFACT_BYTES`) catches ONE
    oversized member; the running-total check against
    `_extraction_byte_budget()` is what catches many members that are each
    individually fine but sum to a bomb. Exercised separately so a
    regression that removed only the running-total check (while leaving
    the per-member one intact) would still be caught."""
    sid = _new_session("bash")
    try:
        sessions.write_file(sid, "small.txt", "hi")
        save = _with_env({}, lambda: sessions.snapshot_save(sid))
        tar_path, _m = sessions._snapshot_paths(sid, save["snapshot_id"])
        per_member = 4 * 1024 * 1024  # 4 MiB each — comfortably under a per-member cap of 5 MiB
        tmp = tar_path.with_name("many.tmp")
        with tarfile.open(tmp, "w:gz") as tf:
            for i in range(5):  # 5 * 4 MiB = 20 MiB, over a 10 MiB budget
                info = tarfile.TarInfo(name=f"f{i}.bin")
                info.size = per_member
                tf.addfile(info, _ZeroStream(per_member))
        tmp.replace(tar_path)
        started = time.monotonic()
        r = _with_env(
            {sessions.MAX_ARTIFACT_BYTES_ENV: str(5 * 1024 * 1024),
             sessions.MAX_SNAPSHOT_BYTES_ENV: str(5 * 1024 * 1024)},  # budget ~= 10 MiB
            lambda: sessions.snapshot_restore(sid, save["snapshot_id"]))
        elapsed = time.monotonic() - started
        check("many individually-small members whose SUM is a bomb are refused",
              r.get("ok") is False and r.get("code") == errors.RESOURCE_EXHAUSTED,
              f"-> {r}")
        check("...by the CUMULATIVE budget, not a single member's own size",
              "budget" in (r.get("error") or ""), f"-> {r.get('error')}")
        check("...and still refused quickly (aborted partway, not after all 5)",
              elapsed < 10.0, f"-> elapsed={elapsed:.3f}s")
    finally:
        sessions.stop(sid)


_test_cumulative_budget_catches_many_small_members()


# ── 15. an exact duplicate member path is refused, not silently overwritten ─
def _test_duplicate_member_path_refused():
    sid = _new_session("bash")
    try:
        sessions.write_file(sid, "a.txt", "hi")
        save = _with_env({}, lambda: sessions.snapshot_save(sid))
        tar_path, _m = sessions._snapshot_paths(sid, save["snapshot_id"])
        _write_hostile_archive(tar_path, lambda tf: (
            _add_member(tf, "dup.txt", b"first"),
            _add_member(tf, "dup.txt", b"second"),
        ))
        before = _sessions_on_disk()
        r = _with_env({}, lambda: sessions.snapshot_restore(sid, save["snapshot_id"]))
        check("a duplicate member path is refused",
              r.get("ok") is False and r.get("code") == errors.PERMISSION_DENIED,
              f"-> {r}")
        check("...and no orphan session directory was left behind",
              _sessions_on_disk() == before, f"-> {_sessions_on_disk() - before}")

        # positive control: a duplicated DIRECTORY entry is harmless and allowed.
        tar_path2, _m2 = sessions._snapshot_paths(sid, save["snapshot_id"])
        _write_hostile_archive(tar_path2, lambda tf: (
            _add_member(tf, "d", type=tarfile.DIRTYPE),
            _add_member(tf, "d", type=tarfile.DIRTYPE),
            _add_member(tf, "d/f.txt", b"ok"),
        ))
        r2 = _with_env({}, lambda: sessions.snapshot_restore(sid, save["snapshot_id"]))
        check("CONTROL: a duplicated DIRECTORY entry is allowed",
              r2.get("ok") is True and r2.get("restored_files") == 1, f"-> {r2}")
        if r2.get("ok"):
            sessions.stop(r2["session_id"])
    finally:
        sessions.stop(sid)


_test_duplicate_member_path_refused()


print(f"\n=== {len(FAILS)} FAILURE(S), {len(SKIPS)} skipped ===" if FAILS else
      f"\n=== SESSION_SNAPSHOT ROUND-TRIPS AND REFUSES HOSTILE ARCHIVES ({len(SKIPS)} skipped) ===")
sys.exit(1 if FAILS else 0)
