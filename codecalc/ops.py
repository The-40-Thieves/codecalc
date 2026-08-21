"""Operational commands: `codecalc status` and `codecalc cleanup` (THE-898).

W3a (THE-894) gave sessions per-session/global disk quotas plus the on-disk
`.codecalc-session-expired` marker for a reaped idle worker — but nothing
LOOKS at either from outside a running server, and nothing RECLAIMS what an
abandoned session left on disk. This module is the operator-facing half:

  status()  — read-only. SESSION_ROOT, session count, per-session/global
              disk usage, which sessions are idle-expired, the configured
              quotas and headroom, the audit log's file+size, and a
              one-line runtime-tier summary. Changes NOTHING.
  cleanup() — the mutating counterpart. Reclaims disk from session
              directories that are either idle-expired (the on-disk marker)
              or old and abandoned-looking, with `write=False` (the CLI's
              `--dry-run`, the DEFAULT) only reporting what would be
              removed.

Both are CLI-only (server.py's `main()` dispatch), never MCP tools — see
server.py's KNOWN_GROUPS/`_tool`: a tool here would need a group and would
count toward the tool-surface gates this module has no business touching.

SAFETY, cleanup() specifically: this runs as a SEPARATE process from any
server that may be using SESSION_ROOT right now, so it has none of
sessions.py's in-memory bookkeeping (`_workers`, `_SESSION_DIR_IDENTITY`,
`_LAST_ACTIVITY`) to consult — only what is on disk.

DIRECTORY MTIME IS NOT A LIVENESS SIGNAL — fix-round CRITICAL, proven by an
adversarial review that had an earlier version of this module delete a
genuinely-live worker session's workspace. This module's own first
docstring claimed "a session actively in use touches its workspace far
more often than [the recency floor]" — that claim is FALSE and the review
disproved it live: a REPL worker doing purely in-memory work
(`x = compute()`) touches no file at all, and even a call that DOES write
touches the workspace without necessarily aging the DIRECTORY —
`sessions._write_nofollow`'s O_CREAT|O_TRUNC on an EXISTING filename
overwrites in place, which bumps only that file's own mtime, not its
parent directory's (a directory's mtime changes when entries are
added/removed/renamed, not when one's contents change). A worker that has
run the same script against the same output filename for days is, by
directory mtime alone, indistinguishable from one nobody has touched since
it started.

The REAL liveness signal is `sessions._lock_owner_alive`: a per-WORKER-
session lockfile (`sessions._LOCK_FILE_NAME`) written by the server at the
moment it spawns a worker, naming the server's own pid, and released the
moment that worker is actually gone (reaped or stopped — see
`sessions._remove_lock_file`'s call sites). `cleanup` checks this for
EVERY candidate, marker-based or age-based, before either path's own
reasoning runs: a lockfile naming a still-live pid refuses the directory
outright, independent of mtime, age threshold, or even the expiry marker.
This is what makes the guarantee "a session any running codecalc server is
using is never deleted" true for worker sessions.

Workspace-only sessions (no worker, so no lockfile is ever written for
them — see `_WORKER_LANGS`) have no such proof available, which is why the
marker-less, age-based candidate path is OPT-IN (`include_unmarked=`/
`--include-unmarked`), not part of the default scan: without a worker to
lock, directory mtime plus a generous age threshold plus the hard recency
floor (`_RECENT_MTIME_GUARD_SECONDS`, applying regardless of marker, age,
or lock) is the best available signal, and it is a heuristic, not a proof
— an operator opts into it deliberately rather than getting it by default.
Marker-based (idle-expired) candidates need no such caveat: a marker only
ever exists because sessions.py's own idle-TTL reaper already closed that
specific worker and released its lock (irreversibly — session ids are
never reused), so a marked directory has no live owner to protect against
by construction, independent of the lockfile check that still runs
against it anyway as defense in depth.

Removal itself goes through `executor._rmtree_checked` with the identity
recorded at scan time, re-verified immediately before the delete — the
same discipline `sessions.stop()` applies to its own workspace teardown,
just without a creation-time record to compare against (there is none to
have, in a separate process) — so a directory swapped out from under a
stale scan is refused rather than deleted. RESIDUAL (accepted, no code
change; same shape `sessions.stop()` already carries): inode reuse between
the scan-time identity capture and the delete is a TOCTOU window this
cannot structurally close without a creation-time record, same as
`executor._rmtree_checked`'s own docstring already states for `stop()`.

WARNING for an operator pointing `CODECALC_SESSION_ROOT` somewhere new:
`cleanup --write` is safe only while SESSION_ROOT is codecalc-private.
`sessions._SAFE_NAME` (the "looks like a session dir" gate for the
age-based path) matches almost any plain alphanumeric/`-`/`_` directory
name — it is not a strong filter. Never point SESSION_ROOT at a shared or
already-populated directory.
"""

from __future__ import annotations

import stat
import time
from pathlib import Path

#: THE-898: the mtime floor below which a directory is NEVER a cleanup
#: candidate, marker or no marker, age threshold or no age threshold, LOCK
#: or no lock. A BACKSTOP, not a liveness proof — see the module docstring's
#: "DIRECTORY MTIME IS NOT A LIVENESS SIGNAL" section for why an in-memory-
#: only worker or an in-place file overwrite never bumps a directory's
#: mtime at all, which this floor cannot see through. It exists for the
#: cases the lockfile check cannot cover (a workspace-only session, which
#: never gets a lock) and as a second, independent guard even where the
#: lock check already applies — erring toward "leave it" wherever any one
#: signal is ambiguous is the only safe default for a command that deletes
#: directories it did not create. Fixed, not an env knob: THE-894's own
#: per-artifact/session caps are configurable because an operator may
#: legitimately want a bigger or smaller BUDGET; this is a minimum safety
#: margin, and the right value for it does not vary by deployment the way a
#: quota does.
_RECENT_MTIME_GUARD_SECONDS = 300.0

#: THE-898: how old (and untouched) a MARKER-LESS, session-shaped directory
#: must be before cleanup() will consider it abandoned. A marker is a
#: precise, durable signal (sessions.py wrote it because it reaped an idle
#: worker); age alone is not — a workspace-only session (no worker, so no
#: idle-expiry machinery ever runs against it) never gets a marker no
#: matter how long it sits unused, and this is the only signal that can
#: ever reclaim one. Generous by default for the same "bounded, not
#: trigger-happy" reasoning THE-894's quota defaults state: a caller
#: legitimately staging a session across a long gap should not have its
#: workspace vanish underneath it.
CLEANUP_ABANDONED_AGE_HOURS_ENV = "CODECALC_CLEANUP_ABANDONED_AGE_HOURS"
_DEFAULT_CLEANUP_ABANDONED_AGE_HOURS = 24.0


def _abandoned_age_seconds() -> float:
    from . import sessions

    # Reuses sessions._env_positive_float rather than a second copy of the
    # same unset/invalid-safe read — the exact reuse this module's own
    # docstring asks for, and the same cross-module reach doctor.py already
    # takes into sessions' "private" accessors.
    hours = sessions._env_positive_float(
        CLEANUP_ABANDONED_AGE_HOURS_ENV, _DEFAULT_CLEANUP_ABANDONED_AGE_HOURS)
    return hours * 3600.0


def status_report() -> dict:
    """A read-only operational snapshot. Changes NOTHING on disk: no session
    is started, stopped, reaped, or written to — this only reads what is
    already there. `cleanup()` below is the mutating counterpart.
    """
    from . import audit as audit_module
    from . import doctor as doctor_module
    from . import sessions

    # doctor.report(deep=False) is itself read-only (resolution, never
    # execution) and already computes the disk-quota limits/usage and the
    # reliability-tier summary this wants — reused rather than re-derived,
    # so status and `codecalc doctor` can never quote two different numbers
    # for the same quota.
    dr = doctor_module.report(deep=False)

    listed = sessions.list_sessions().get("sessions", [])
    per_session = []
    idle_expired_ids = []
    for s in listed:
        sid = s["session_id"]
        usage = sessions._session_dir_size(sid)
        expired = False
        try:
            d = sessions._session_dir(sid)
            marker = d / sessions._EXPIRED_MARKER_NAME
            # lstat, not is_file(): the same symlink-safety rule every path
            # in sessions.py already applies to a session-controlled name —
            # a session could plant a symlink at this exact filename, and
            # following it would let executed code lie about its own
            # expiry state.
            marker_st = marker.lstat()
            expired = stat.S_ISREG(marker_st.st_mode)
        except (OSError, ValueError):
            expired = False
        per_session.append({
            "session_id": sid, "language": s["language"],
            "stateful": s["stateful"], "usage_bytes": usage,
            "idle_expired": expired,
        })
        if expired:
            idle_expired_ids.append(sid)

    quota = dr["disk_quota"]
    total_quota_bytes = int(quota["limits"]["total_disk_quota_mb"] * 1024 * 1024)

    audit_log = audit_module.from_env()
    audit_path = str(audit_log.path) if audit_log.path is not None else None
    audit_size = None
    if audit_log.path is not None:
        try:
            audit_size = audit_log.path.stat().st_size
        except OSError:
            audit_size = None  # not yet created (create-on-write, THE-848)

    return {
        "ok": True,
        "session_root": str(sessions.SESSION_ROOT),
        "session_count": len(listed),
        "sessions": per_session,
        "idle_expired_session_ids": idle_expired_ids,
        "global_usage_bytes": quota["global_usage_bytes"],
        "host_free_bytes": quota["host_free_bytes"],
        "quota": {
            "session_disk_quota_mb": quota["limits"]["session_disk_quota_mb"],
            "total_disk_quota_mb": quota["limits"]["total_disk_quota_mb"],
            "total_headroom_bytes": max(total_quota_bytes - quota["global_usage_bytes"], 0),
            "max_artifact_bytes": quota["limits"]["max_artifact_bytes"],
            "max_artifact_count": quota["limits"]["max_artifact_count"],
            "min_host_free_mb": quota["limits"]["min_host_free_mb"],
        },
        "audit_log": {
            "path": audit_path, "size_bytes": audit_size,
            "max_mb": audit_module._audit_max_bytes() / (1024 * 1024),
        },
        "runtime_tier_summary": dr["tier_summary"],
        "backend": dr["backend"]["kind"],
    }


def _cleanup_candidates(*, include_unmarked: bool = False) -> list[dict]:
    """Every direct child of SESSION_ROOT `cleanup()` would consider,
    classified. `dry_run` and `write` share this ONE scan so the two can
    never disagree about what qualifies — see the module docstring.

    `include_unmarked=False` (the default `cleanup()` also defaults to)
    considers ONLY marker-based (idle-expired) candidates — the path that
    needs no age heuristic because a marker only exists once sessions.py's
    own idle-TTL reaper has already closed that worker for good. Passing
    `include_unmarked=True` additionally considers marker-less,
    session-shaped directories past the age threshold — see the module
    docstring for why that path is opt-in rather than default.

    Every candidate, marker-based or age-based, is FIRST checked against
    `sessions._lock_owner_alive` — a live worker-session lock refuses the
    directory outright before either path's own reasoning runs.

    Each entry carries `eligible` (bool) and `reason` (str); an eligible
    entry additionally carries `size_bytes`, `age_seconds`, and an internal
    `_identity` (device, inode) consumed by `cleanup()` and stripped before
    a report leaves this module.
    """
    from . import executor, sessions

    root = sessions.SESSION_ROOT.resolve()
    out: list[dict] = []
    if not root.is_dir():
        return out

    now = time.time()
    age_threshold = _abandoned_age_seconds()

    for entry in sorted(root.iterdir()):
        name = entry.name
        try:
            st = entry.lstat()
        except OSError:
            continue  # raced away between the listing and the lstat

        if stat.S_ISLNK(st.st_mode):
            # Never followed — a symlink at SESSION_ROOT's top level could
            # point anywhere the server user can reach, and rm'ing through
            # it would delete whatever it names, not a session workspace.
            out.append({"session_id": name, "path": str(entry), "eligible": False,
                        "reason": "refused: symlink at SESSION_ROOT top level, not followed"})
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue  # a stray file at SESSION_ROOT's top level, not a session dir at all

        candidate_path = root / name
        # Defence in depth, the same rule sessions._session_dir applies:
        # the path this is about to consider must resolve to a DIRECT
        # child of SESSION_ROOT — never SESSION_ROOT itself, never
        # something a symlink further down redirected elsewhere.
        resolved = candidate_path.resolve()
        if resolved == root or resolved.parent != root:
            out.append({"session_id": name, "path": str(candidate_path), "eligible": False,
                        "reason": "refused: not a direct child of SESSION_ROOT"})
            continue

        # THE-894's own identity guard (executor._dir_identity mirrors
        # sessions._SESSION_DIR_IDENTITY's bookkeeping): lstat-based, so a
        # symlink or a filesystem with no stable inode both come back None,
        # and this candidate is refused rather than treated as removable.
        identity = executor._dir_identity(candidate_path)
        if identity is None:
            out.append({"session_id": name, "path": str(candidate_path), "eligible": False,
                        "reason": "refused: no stable directory identity available"})
            continue

        age = now - st.st_mtime
        if age < _RECENT_MTIME_GUARD_SECONDS:
            out.append({"session_id": name, "path": str(candidate_path), "eligible": False,
                        "reason": (f"skipped: modified {age:.0f}s ago, under the "
                                  f"{_RECENT_MTIME_GUARD_SECONDS:g}s active-session floor")})
            continue

        # THE-898 fix-round CRITICAL: the liveness PROOF, checked before
        # either eligibility path below runs — see the module docstring's
        # "DIRECTORY MTIME IS NOT A LIVENESS SIGNAL" section. A live
        # worker-session lock refuses this directory outright, full stop,
        # regardless of marker or age.
        if sessions._lock_owner_alive(candidate_path):
            out.append({"session_id": name, "path": str(candidate_path), "eligible": False,
                        "reason": "refused: a live codecalc server process still holds "
                                  "this session's worker lock"})
            continue

        try:
            marker_st = (candidate_path / sessions._EXPIRED_MARKER_NAME).lstat()
            has_marker = stat.S_ISREG(marker_st.st_mode)
        except OSError:
            has_marker = False

        if has_marker:
            reason = "idle-expired (on-disk marker present)"
        elif not include_unmarked:
            out.append({"session_id": name, "path": str(candidate_path), "eligible": False,
                        "reason": "skipped: no expiry marker (pass include_unmarked=True / "
                                  "--include-unmarked to also consider old, unmarked, "
                                  "session-shaped directories — see the module docstring "
                                  "for why that path is opt-in)"})
            continue
        elif bool(sessions._SAFE_NAME.match(name)) and age >= age_threshold:
            reason = (f"abandoned (untouched {age / 3600:.1f}h, over the "
                      f"{age_threshold / 3600:g}h threshold, "
                      f"{CLEANUP_ABANDONED_AGE_HOURS_ENV})")
        else:
            out.append({"session_id": name, "path": str(candidate_path), "eligible": False,
                        "reason": "skipped: no expiry marker and under the age threshold"})
            continue

        # Reuses the same regular-file walk _session_dir_size/
        # _global_disk_usage are built on (sessions._iter_regular_files) —
        # not a second, independently-drifting way to size a workspace.
        size = sum(fst.st_size for _, fst in sessions._iter_regular_files(candidate_path))
        out.append({
            "session_id": name, "path": str(candidate_path), "eligible": True,
            "reason": reason, "size_bytes": size, "age_seconds": age,
            "_identity": identity,
        })
    return out


def cleanup(write: bool = False, include_unmarked: bool = False) -> dict:
    """Reclaim disk from abandoned/idle-expired session directories under
    SESSION_ROOT.

    `write=False` (the CLI's `--dry-run`, and the default) only reports what
    WOULD be removed and how many bytes that would reclaim — nothing is
    deleted. `write=True` actually removes each eligible directory, through
    `executor._rmtree_checked` with the identity recorded at scan time
    re-verified immediately before the delete, so a directory swapped out
    from under this scan (the same rename-race `_rmtree_checked`'s own
    docstring describes) is refused rather than deleted.

    `include_unmarked=False` (the default) considers ONLY the on-disk
    `.codecalc-session-expired` marker — the path with no residual risk,
    since a marker only exists once sessions.py's own idle-TTL reaper has
    already closed and released that worker for good. `include_unmarked=True`
    additionally considers session-shaped, marker-less directories past a
    generous age threshold — see the module docstring's "DIRECTORY MTIME IS
    NOT A LIVENESS SIGNAL" section for why that path is opt-in rather than
    default. EVERY candidate on EITHER path is first checked against
    `sessions._lock_owner_alive`: a live worker-session lock refuses the
    directory outright before either path's own reasoning runs, which is
    what makes "a session any running codecalc server is using is never
    deleted" true for worker sessions specifically (see the module
    docstring for the case that check does not cover: a workspace-only
    session, which never holds a lock).
    """
    from . import executor, sessions

    candidates = _cleanup_candidates(include_unmarked=include_unmarked)
    eligible = [c for c in candidates if c["eligible"]]
    total_candidate_bytes = sum(c["size_bytes"] for c in eligible)

    removed: list[dict] = []
    removal_errors: list[dict] = []
    total_reclaimed_bytes = 0
    if write:
        for c in eligible:
            ok = executor._rmtree_checked(Path(c["path"]), c["_identity"])
            entry = {"session_id": c["session_id"], "path": c["path"],
                     "size_bytes": c["size_bytes"]}
            if ok:
                removed.append(entry)
                total_reclaimed_bytes += c["size_bytes"]
            else:
                # _rmtree_checked already printed WHY to stderr (identity
                # mismatch, or an incomplete removal) — recorded here too so
                # a caller reading the returned dict does not have to have
                # captured stderr to know a candidate was skipped.
                removal_errors.append({**entry,
                                       "error": "removal refused or incomplete; see stderr"})

    return {
        "ok": True,
        "write": write,
        "include_unmarked": include_unmarked,
        "session_root": str(sessions.SESSION_ROOT.resolve()),
        # `_identity` is this module's own bookkeeping, not a caller-facing
        # field — stripped here rather than never collected, so eligible/
        # removed above can still use it.
        "candidates": [{k: v for k, v in c.items() if not k.startswith("_")}
                       for c in candidates],
        "eligible_count": len(eligible),
        "total_candidate_bytes": total_candidate_bytes,
        "removed": removed,
        "removal_errors": removal_errors,
        "total_reclaimed_bytes": total_reclaimed_bytes,
    }
