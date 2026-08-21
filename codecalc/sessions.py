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

import collections
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import errors, executor, landlock, registry

SESSION_ROOT = Path(os.environ.get("CODECALC_SESSION_ROOT", "~/.codecalc/sessions")).expanduser()

#: languages that get a stateful REPL worker (interpreters with exec()):
_WORKER_LANGS = {"python3", "node"}

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

#: Spill capture ceiling per stream (THE-783): bounded, not unbounded. When a
#: session-scoped execution would otherwise truncate and drop the tail, the
#: FULL stream is captured up to this cap (instead of the smaller default
#: truncation cap) and, if it is still larger than what stays inline, written
#: to a workspace file rather than discarded.
#:
#: This only ever REPLACES the DEFAULT truncation cap (`max_output_kb`
#: unset/0). A caller who passes an EXPLICIT `max_output_kb` gets exactly
#: that cap honoured, with no spill: #25 made `max_output_kb` a genuine
#: memory ceiling (`_BoundedDrain` — "a truncation applied after the fact,
#: not a ceiling"), and silently substituting a larger internal capture cap
#: for an explicit caller-chosen one would quietly undo that fix for session
#: calls specifically. Spilling on the UNSET default carries no such risk: a
#: caller who never asked for a tight ceiling had no memory expectation to
#: violate.
#:
#: A per-session total-disk quota now DOES exist (THE-894, below) — this
#: paragraph used to say it did not, and grepping quota/disk_usage/statvfs/
#: FSIZE across codecalc/*.py, README.md, SECURITY.md and AUDIT.md found
#: none. The closest existing thing is executor.FSIZE_LIMIT_BYTES (256 MiB),
#: a PER-PROCESS single-file rlimit applied to the SANDBOXED CHILD via
#: preexec_fn — it does not bound the SERVER process writing a spill file, or
#: the SUM of everything a session accumulates over its lifetime. So this
#: constant, plus _SPILL_RETENTION below, remain their own bound on the spill
#: mechanism specifically, kept well under FSIZE_LIMIT_BYTES's ceiling; the
#: session-wide and cross-session totals are enforced separately, see
#: `_disk_quota_refusal` and the `CODECALC_*_DISK_QUOTA_MB` accessors below.
SPILL_CAPTURE_KB = 4096  # 4 MiB per stream

#: Ceiling on a single session file served back as an MCP resource. Named
#: rather than repeated as a literal because the SPILL write path is now
#: bounded by the very same number: `resource_read` REFUSES an over-cap file
#: outright (returns None — not a short read), so a spill written above this
#: is a file the server advertises a URI for and can never serve. Two copies
#: of "4 MiB" in two files is exactly how those two caps drifted apart in the
#: first place.
RESOURCE_MAX_BYTES = 4 * 1024 * 1024

#: Spill files live in this subdirectory of a session workspace.
_SPILL_DIRNAME = ".codecalc-spill"

#: Spill files retained per session before the oldest is pruned — bounds the
#: total disk this feature can add over a session's lifetime, the same shape
#: of protection run_supervisor.RunSupervisor.max_completed gives its own
#: on-disk journal.
_SPILL_RETENTION = 20

# ── THE-894: per-session and global disk quotas ────────────────────────────
#: The gap SPILL_CAPTURE_KB's own docstring named above: nothing bounded the
#: TOTAL a session accumulates over its lifetime via session_write_file,
#: artifacts a running program creates, files EXECUTED CODE writes into the
#: workspace during session_run/execute, and packages `install_package`
#: fetches straight into it (packages.install — the LARGEST of the four:
#: MB to GB, where every other vector here writes one call's worth at a
#: time) — only the per-stream and per-file ceilings elsewhere in this
#: module. A session (or a caller opening many of them) could fill the
#: operator's disk one write at a time and nothing here would notice.
#:
#: Five independent knobs, not one: a per-session ceiling bounds any ONE
#: session; a global ceiling bounds the SUM across every session workspace on
#: disk right now (a caller could otherwise stay under the per-session cap by
#: opening many sessions); a per-artifact size/count pair bounds what a
#: single write can create, independent of the running total (a session
#: writing thousands of tiny files can sit under every byte ceiling here
#: while still exhausting inodes / directory-listing sanity); and the
#: host-free-space floor protects the disk even when every quota above is
#: generous — a shared host can be driven low by something that is not a
#: codecalc session at all. All five read their environment variable PER
#: CALL, never cached — same reasoning as `_idle_ttl_seconds`: an operator's
#: change should apply without a server restart.
SESSION_DISK_QUOTA_MB_ENV = "CODECALC_SESSION_DISK_QUOTA_MB"
TOTAL_DISK_QUOTA_MB_ENV = "CODECALC_TOTAL_DISK_QUOTA_MB"
MAX_ARTIFACT_BYTES_ENV = "CODECALC_MAX_ARTIFACT_BYTES"
MAX_ARTIFACT_COUNT_ENV = "CODECALC_MAX_ARTIFACT_COUNT"
MIN_HOST_FREE_MB_ENV = "CODECALC_MIN_HOST_FREE_MB"

#: Defaults are GENEROUS, not unlimited — "bounded by default" is the goal,
#: not "unlimited by default with a knob nobody turns". 512 MiB/session and
#: 8 GiB total are far above what a legitimate calculator session
#: accumulates (the existing per-file/per-stream ceilings above already sit
#: in the low single-digit MiB range) while still catching a runaway loop
#: long before it threatens a shared host. 16 MiB/artifact and 500
#: artifacts/session are deliberately generous relative to RESOURCE_MAX_BYTES
#: (4 MiB, the SERVED-file read cap) — this is a WRITE-time ceiling, not a
#: read one, and the two are allowed to differ, same as SPILL_CAPTURE_KB
#: already differs from a caller's own max_output_kb. 256 MiB free-host floor
#: is a small fraction of a typical disk, the same order-of-magnitude-
#: generous shape as FSIZE_LIMIT_BYTES.
_DEFAULT_SESSION_DISK_QUOTA_MB = 512.0
_DEFAULT_TOTAL_DISK_QUOTA_MB = 8192.0
_DEFAULT_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_ARTIFACT_COUNT = 500
_DEFAULT_MIN_HOST_FREE_MB = 256.0

#: Files this module's own runner writes, excluded from what counts as a
#: session's "artifacts" — same set `artifacts()` already excluded inline;
#: named here so the disk-quota artifact-count cap (below) and `artifacts()`
#: share ONE definition instead of two that can drift apart.
_RUNNER_INTERNAL_NAMES = frozenset({
    "main.py", "main.js", "run.out", "run.err", "run.in",
    "compile.out", "compile.err", "compile.in", "a.out",
})

#: Guards the "measure usage, then write" critical section in every
#: disk-quota-checked write path (`write_file`, the spill write in
#: `spill_if_truncated`). Deliberately a SEPARATE lock from the module's
#: `_lock` (worker/idle-expiry bookkeeping) rather than reusing it: computing
#: usage walks the filesystem (`_session_dir_size`/`_global_disk_usage`),
#: which can take real time on a large workspace, and `_reap_then_note` —
#: called by every one of these write paths just before this lock is taken —
#: already acquires `_lock` itself. `threading.Lock` is not reentrant, so
#: nesting the quota check inside `_lock` would deadlock the very call that
#: reaps an idle worker before writing; a dedicated lock removes that
#: coupling entirely while still making "check current usage" and "perform
#: this write" one atomic step, which is what closes the check-then-write
#: race between two concurrent writes on the same (or different) session.
_disk_lock = threading.Lock()


def _env_positive_float(name: str, default: float) -> float:
    """A positive float read from `name`, or `default` if unset/invalid/
    non-positive. Same shape as `_idle_ttl_seconds`: one indirected read, no
    crash on a bad value. Unlike the idle TTL, unset here does NOT mean "off"
    — every quota this feeds is bounded-by-default, so an absent or
    malformed value falls back to the generous default, never to unlimited.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_positive_int(name: str, default: int) -> int:
    """Integer counterpart of `_env_positive_float`, for byte/count ceilings
    that are naturally whole numbers rather than a size in MB."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _session_disk_quota_bytes() -> int:
    return int(_env_positive_float(SESSION_DISK_QUOTA_MB_ENV,
                                   _DEFAULT_SESSION_DISK_QUOTA_MB) * 1024 * 1024)


def _total_disk_quota_bytes() -> int:
    return int(_env_positive_float(TOTAL_DISK_QUOTA_MB_ENV,
                                   _DEFAULT_TOTAL_DISK_QUOTA_MB) * 1024 * 1024)


def _max_artifact_bytes() -> int:
    return _env_positive_int(MAX_ARTIFACT_BYTES_ENV, _DEFAULT_MAX_ARTIFACT_BYTES)


def _max_artifact_count() -> int:
    return _env_positive_int(MAX_ARTIFACT_COUNT_ENV, _DEFAULT_MAX_ARTIFACT_COUNT)


def _min_host_free_bytes() -> int:
    return int(_env_positive_float(MIN_HOST_FREE_MB_ENV,
                                   _DEFAULT_MIN_HOST_FREE_MB) * 1024 * 1024)


def _iter_regular_files(d: Path):
    """`(path, lstat_result)` for every REGULAR file under `d`, recursively.

    lstat, not stat — the same rule `artifacts()`/`_list()` already apply:
    stat() FOLLOWS a symlink, and a session can plant one pointing outside
    the workspace, which would make a size measurement either read an
    unrelated file's size (inflating/deflating the quota check) or leak
    whether some outside path exists at all. A symlink itself is not a
    regular file, so `S_ISREG` simply skips it here rather than reporting a
    bogus size for it.
    """
    for p in d.rglob("*"):
        try:
            st = p.lstat()
        except OSError:
            continue  # raced away between the listing and the lstat
        if stat.S_ISREG(st.st_mode):
            yield p, st


def _session_dir_size(session_id: str) -> int:
    """Total bytes `session_id`'s workspace occupies on disk right now.

    A missing/invalid/vanished session directory is 0 bytes, not an error —
    a quota check must never be the thing that raises for a caller who never
    checked existence first; `write_file`/`execute` already report "unknown
    session" on their own before this is ever consulted.
    """
    try:
        d = _session_dir(session_id)
    except ValueError:
        return 0
    if not d.is_dir():
        return 0
    return sum(st.st_size for _, st in _iter_regular_files(d))


def _global_disk_usage() -> int:
    """Sum of `_session_dir_size` across every session workspace ON DISK.

    Walks SESSION_ROOT directly rather than only the sessions this PROCESS
    remembers (`_workers`/`_SESSION_DIR_IDENTITY`): a workspace-only session
    holds no worker and is never in either dict, and `list_sessions()`
    already treats the on-disk directory listing as the ground truth this
    module trusts over its own bookkeeping — the global quota has to agree
    with that same ground truth, or a workspace-only session's disk usage
    would silently not count toward it.
    """
    root = SESSION_ROOT.resolve()
    if not root.is_dir():
        return 0
    total = 0
    for entry in root.iterdir():
        if entry.is_dir():
            total += sum(st.st_size for _, st in _iter_regular_files(entry))
    return total


def _artifact_entries(d: Path) -> list[tuple[Path, int]]:
    """`(path, size)` for every file `artifacts()` would list.

    Factored out of `artifacts()` itself so the disk-quota artifact-count/
    size caps below count exactly what a caller can already SEE via
    `session_artifacts`, rather than a second, independently-drifting
    definition of "artifact".
    """
    out = []
    for p in sorted(d.rglob("*")):
        if "__pycache__" in p.parts or p.name.endswith(".pyc"):
            continue
        try:
            st = p.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if p.name not in _RUNNER_INTERNAL_NAMES:
            out.append((p, st.st_size))
    return out


def _artifact_cap_refusal(session_id: str, d: Path, incoming_bytes: int, *,
                          is_new_file: bool) -> dict | None:
    """None if a write of `incoming_bytes` stays within the per-artifact
    size/count ceilings; otherwise the refusal to return verbatim.

    Independent of `_disk_quota_refusal` below: a single oversized write can
    sit comfortably under a generous session quota and still be one runaway
    file dominating the workspace, and a session writing one byte at a time
    into thousands of tiny files can stay under every byte ceiling here while
    exhausting inodes / directory-listing sanity — the count check is what
    catches that shape. `is_new_file` is supplied by the caller (rather than
    this function re-deriving it) because the two write paths that call this
    know it for free: `write_file` already resolved its jailed target path,
    and every spill file is unconditionally new (uuid4-named).
    """
    max_bytes = _max_artifact_bytes()
    if incoming_bytes > max_bytes:
        return errors.error_result(
            errors.RESOURCE_EXHAUSTED,
            f"write of {incoming_bytes} bytes exceeds the per-artifact size "
            f"cap of {max_bytes} bytes ({MAX_ARTIFACT_BYTES_ENV})",
            remedy=f"write a smaller file, or raise {MAX_ARTIFACT_BYTES_ENV}",
            incoming_bytes=incoming_bytes, max_artifact_bytes=max_bytes,
        )
    if is_new_file:
        max_count = _max_artifact_count()
        count = len(_artifact_entries(d))
        if count >= max_count:
            return errors.error_result(
                errors.RESOURCE_EXHAUSTED,
                f"session '{session_id}' already has {count} artifact "
                f"file(s), at the {max_count}-file cap ({MAX_ARTIFACT_COUNT_ENV})",
                remedy=("delete unneeded files (session_files to see what's "
                        f"there), or raise {MAX_ARTIFACT_COUNT_ENV}"),
                artifact_count=count, max_artifact_count=max_count,
            )
    return None


#: Named once so the refusal message and `codecalc doctor`'s text (server.py)
#: cannot say two different things about the one in-band way out (THE-894
#: fix round 4): `execute()`/`run_file()` are themselves refused by
#: `quota_precheck` once a session is over quota, so executed code cannot
#: `rm` its way out, and `session_stop` (which destroys the whole session)
#: was the only recovery. A write whose NET effect on disk usage is <= 0 —
#: overwriting an existing file with a same-size-or-smaller one — is always
#: permitted regardless of current usage (see `write_file`'s `net_bytes`),
#: which is the missing in-band path.
_QUOTA_RECOVERY_HINT = (
    "free space by overwriting a large file with a smaller one (a "
    "same-size-or-shrinking write is always allowed, even over quota), "
    "or session_stop"
)


def _disk_quota_refusal(session_id: str, incoming_bytes: int) -> dict | None:
    """None if `incoming_bytes` more may be written into `session_id`'s
    workspace right now under every disk ceiling except the per-artifact
    ones (`_artifact_cap_refusal` is separate — see its own docstring);
    otherwise the refusal to return verbatim.

    `incoming_bytes` is a NET delta, not a gross write size: `write_file`
    computes `new_size - existing_size` for an overwrite (THE-894 fix round
    3 — `_session_dir_size` already counts the file being overwritten at
    its CURRENT size, so comparing against the full new size double-counted
    it and wrongly refused a same-size or SHRINKING overwrite near quota).
    `incoming_bytes=0` is therefore a valid, meaningful call on its own: it
    asks "is this session already over quota right now" with nothing being
    added or removed, which is exactly what `quota_precheck` uses before
    letting code start running at all — see its docstring for why that,
    rather than a separate sticky flag, is what makes "refused until freed"
    true. A net-non-positive write is NOT routed through this function at
    all — see `_write_guard`'s fix-round-4 recovery-path comment.

    Checked smallest blast radius first: a session over its OWN quota should
    say so, not blame the shared global total it may not even be close to.
    The host-free-space floor runs last and unconditionally — it protects
    the host even when both quotas above are generous, because a shared host
    can be driven low by something that is not a codecalc session at all.
    """
    session_usage = _session_dir_size(session_id)
    session_quota = _session_disk_quota_bytes()
    if session_usage + incoming_bytes > session_quota:
        return errors.error_result(
            errors.RESOURCE_EXHAUSTED,
            f"session '{session_id}' disk quota exceeded: "
            f"{session_usage + incoming_bytes} bytes would exceed the "
            f"{session_quota}-byte per-session limit ({SESSION_DISK_QUOTA_MB_ENV})",
            remedy=(f"delete files in this session's workspace (session_files "
                    f"to see what's there), raise {SESSION_DISK_QUOTA_MB_ENV}, "
                    f"or {_QUOTA_RECOVERY_HINT}"),
            usage_bytes=session_usage, quota_bytes=session_quota, scope="session",
        )
    global_usage = _global_disk_usage()
    global_quota = _total_disk_quota_bytes()
    if global_usage + incoming_bytes > global_quota:
        return errors.error_result(
            errors.RESOURCE_EXHAUSTED,
            f"global session disk quota exceeded: {global_usage + incoming_bytes} "
            f"bytes across all sessions would exceed the {global_quota}-byte "
            f"total limit ({TOTAL_DISK_QUOTA_MB_ENV})",
            remedy=("stop idle sessions (session_stop) to free disk shared "
                    f"across every session, or raise {TOTAL_DISK_QUOTA_MB_ENV}"),
            usage_bytes=global_usage, quota_bytes=global_quota, scope="global",
        )
    try:
        SESSION_ROOT.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(SESSION_ROOT).free
    except OSError:
        # Cannot even measure the host — do not block on a check that could
        # not run; the quota checks above already ran and still apply.
        return None
    min_free = _min_host_free_bytes()
    if free < min_free:
        return errors.error_result(
            errors.RESOURCE_EXHAUSTED,
            f"host free disk space ({free} bytes) is below the configured "
            f"floor ({min_free} bytes, {MIN_HOST_FREE_MB_ENV})",
            remedy=f"free disk space on the host, or lower {MIN_HOST_FREE_MB_ENV}",
            free_bytes=free, min_free_bytes=min_free, scope="host",
        )
    return None


def _artifact_count_refusal(session_id: str) -> dict | None:
    """None unless `session_id` already has MORE artifact files than
    `CODECALC_MAX_ARTIFACT_COUNT` allows right now; otherwise the refusal.

    THE-894 fix round 2: `_artifact_cap_refusal` above only runs from this
    module's OWN writes (`write_file`, the spill write), each of which adds
    at most one new file per call — checking there catches a caller creating
    files one at a time, which is all it was ever asked to catch. Code
    executed via `execute()`/`run_file()`, or a package `install_package`
    fetches, can create hundreds of files in ONE call, none of which go
    through `write_file` at all, and each one can individually sit under
    `CODECALC_MAX_ARTIFACT_BYTES` — so the byte-quota checks alone never
    notice a session with a cap of 5 ending up with 1005 tiny files. This is
    the count cap's own precheck/postcheck counterpart to
    `_disk_quota_refusal`, consulted by `quota_precheck`/`quota_postcheck`
    the same way and for the same reason.
    """
    try:
        d = _session_dir(session_id)
    except ValueError:
        return None
    if not d.is_dir():
        return None
    max_count = _max_artifact_count()
    count = len(_artifact_entries(d))
    if count > max_count:
        return errors.error_result(
            errors.RESOURCE_EXHAUSTED,
            f"session '{session_id}' has {count} artifact file(s), over the "
            f"{max_count}-file cap ({MAX_ARTIFACT_COUNT_ENV})",
            remedy=("delete unneeded files (session_files to see what's "
                    f"there), or raise {MAX_ARTIFACT_COUNT_ENV}"),
            artifact_count=count, max_artifact_count=max_count,
        )
    return None


def _write_guard(session_id: str, d: Path, incoming_bytes: int, net_bytes: int, *,
                 is_new_file: bool) -> dict | None:
    """The combined THE-894 gate every server-side write goes through.

    `incoming_bytes` is the GROSS size of what is being written — what the
    per-artifact size cap cares about, since that cap bounds the size of the
    resulting FILE, not how much bigger the workspace got. `net_bytes` is
    the workspace's actual size DELTA — what the disk-quota checks care
    about (see `_disk_quota_refusal`'s docstring for why the two differ on
    an overwrite). For a brand-new file the two are always equal.

    Caller holds `_disk_lock` across this AND the write that follows it —
    see the lock's own docstring for why.
    """
    refusal = _artifact_cap_refusal(session_id, d, incoming_bytes,
                                    is_new_file=is_new_file)
    if refusal is not None:
        return refusal
    if net_bytes <= 0:
        # THE-894 fix round 4: the in-band recovery path. A write that can
        # only hold usage steady or SHRINK it must never be refused by the
        # byte-quota checks, even while the session is currently over
        # quota — otherwise, once execute()/run_file() are themselves
        # refused by quota_precheck, there is no way to free space from
        # inside a session short of session_stop destroying it outright.
        # The artifact-cap check above already ran and is UNAFFECTED by this
        # early return: overwriting an EXISTING file is never a "new" file
        # (the count cap cannot trip), and the per-artifact SIZE cap is
        # about the resulting file's size, not the delta, so a
        # still-oversized overwrite was already correctly refused above
        # rather than let through here.
        return None
    return _disk_quota_refusal(session_id, net_bytes)


def quota_precheck(session_id: str) -> dict | None:
    """None if `session_id` may execute code / write right now; otherwise the
    refusal to return verbatim, before anything runs.

    Called before EXECUTING code (`execute()`'s both branches,
    `execution_service.SessionService.run_file`) rather than only before this
    module's OWN writes: code a session runs can write files straight into
    the workspace, which no pre-write check on a specific call can bound
    (THE-894 point 4 — a stateful worker's `open("x", "w")` or a fresh
    process's own output files never go through `write_file` at all).

    Reusing `_disk_quota_refusal(..., incoming_bytes=0)` — "is this session
    already over quota" — is deliberate: it is the same measurement
    `quota_postcheck` takes after a run, so a session a run just pushed over
    stays refused on its NEXT call for exactly as long as it remains over,
    and starts working again the moment something frees enough space,
    without a separate sticky flag that could go stale relative to the disk
    it is supposed to describe. `_artifact_count_refusal` is the same shape
    for the count cap (THE-894 fix round 2) — checked second, after the byte
    quotas, for the same "smallest blast radius first" reason
    `_disk_quota_refusal` orders its own two checks.
    """
    refusal = _disk_quota_refusal(session_id, 0)
    if refusal is not None:
        return refusal
    return _artifact_count_refusal(session_id)


def quota_postcheck(session_id: str, result: dict) -> dict:
    """Disclose in `result` when a run just left `session_id` over its disk
    quota OR its artifact-count cap (THE-894 point 4; count cap added in fix
    round 2). Mutates and returns `result`.

    Never refuses and never touches stdout/exit_code/verdict — the run
    already happened and produced a real result; the only thing added is
    visibility, plus the fact (silent otherwise) that `quota_precheck` will
    refuse the NEXT call on this session for as long as usage, measured
    fresh, stays over either line.
    """
    usage = _session_dir_size(session_id)
    quota = _session_disk_quota_bytes()
    if usage > quota:
        result["disk_quota_exceeded"] = True
        result["disk_usage_bytes"] = usage
        result["disk_quota_bytes"] = quota
    global_usage = _global_disk_usage()
    global_quota = _total_disk_quota_bytes()
    if global_usage > global_quota:
        result["global_disk_quota_exceeded"] = True
        result["global_disk_usage_bytes"] = global_usage
        result["global_disk_quota_bytes"] = global_quota
    count_refusal = _artifact_count_refusal(session_id)
    if count_refusal is not None:
        result["artifact_count_exceeded"] = True
        result["artifact_count"] = count_refusal["artifact_count"]
        result["max_artifact_count"] = count_refusal["max_artifact_count"]
    return result


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

# ── THE-779 residual: idle-expiry for abandoned stateful sessions ──────────
#: "the one operational gap: a leaked session holds a worker process
#: forever." UNSET (default) is unchanged behaviour: no expiry, ever, same
#: as before this existed. SET (seconds, float-parseable) makes an idle
#: worker session get reaped the next time anything touches it.
#:
#: Default OFF, not a conservative-but-nonzero number, and that is a
#: decision worth stating rather than leaving implicit: every other
#: behaviour-changing knob in this module (CODECALC_SESSION_ROOT aside, which
#: only relocates a path) and its neighbours default to preserving current
#: behaviour until an operator opts in — runtimes.ALLOW_ELEVATED_ENV,
#: executor.REQUIRE_NATIVE_ENV, packages.ALLOWLIST_ENV all do this. A session
#: is explicitly the "calculator that remembers" primitive (module
#: docstring); an agentic caller can legitimately go quiet for minutes
#: between turns (thinking time, a human in the loop) without that being
#: "abandoned". Picking any specific default TTL risks silently killing a
#: live, wanted session for someone whose pacing it did not anticipate — a
#: worse failure than a slow leak of processes an operator can already see
#: and reap manually via `session_stop`/`session_list`. Unset costs nothing
#: to anyone not already opting in.
IDLE_TTL_ENV = "CODECALC_SESSION_IDLE_TTL_SECONDS"

#: Last-activity clock per WORKER session, `time.monotonic()`. Only sessions
#: with a worker are tracked: a workspace-only session holds no long-lived
#: process, so it is not the leak this ticket names, and monotonic rather
#: than wall-clock so a host clock adjustment cannot resurrect or age out a
#: session early.
_LAST_ACTIVITY: dict[str, float] = {}

#: The idle clock's single source, indirected through one name so the two
#: sites that read it (`_touch` records last-activity, `_maybe_reap_locked`
#: compares against it) share one seam. Production is `time.monotonic`;
#: tests substitute a deterministic fake clock (test_session_idle_expiry)
#: so idle expiry is exercised without real sleeps — the flake-free way to
#: drive the under-TTL/over-TTL boundary. Only these two call sites use it,
#: so a fake clock controls ONLY the idle decision and leaves real
#: subprocess execution (and its own wall-clock timeouts) untouched.
_now = time.monotonic

#: Session ids reaped for idle timeout, kept distinct from "never existed" and
#: from "worker died for some other reason". Without this, `execute()` on a
#: reaped session_id would fall through to `_workers.get(session_id) is
#: None` and re-run as a fresh, unconfined workspace-only process — a SILENT
#: RESPAWN, which is exactly what the ticket says a subsequent call must not
#: do.
#:
#: BOUNDED (fix-round-1, IMPORTANT): "abandoned sessions are precisely the
#: ones that never get session_stop" — so nothing ever removes their entry
#: from this set on its own, and an idle-expiry-heavy deployment would grow
#: it without limit. `_EXPIRED` is therefore a fast-path CACHE, capped at
#: `_EXPIRED_CAP` with FIFO eviction (`_EXPIRED_ORDER`); the DURABLE record
#: is a marker file written into the session's own workspace on reap
#: (`_write_expired_marker`), which costs no unbounded process memory — one
#: file per already-existing session directory, removed for free when
#: `stop()` deletes that directory. A cache miss falls back to a stat() on
#: the marker (`_is_expired_on_disk`) rather than assuming "not in the set"
#: means "never expired", which would silently reopen the exact fall-through
#: bug this set exists to close, just via memory pressure instead of a race.
_EXPIRED: set[str] = set()
_EXPIRED_ORDER: collections.deque[str] = collections.deque()
_EXPIRED_CAP = 512

#: Filename for the durable per-session expiry marker. Leading dot to match
#: this module's other internal, non-artifact files (`.tmp` in
#: `_worker_env`); `artifacts()` already excludes only a fixed set of
#: runner-internal names, not dotfiles generally, so this can appear in
#: `session_list`/`session_files` output — informational, not a defect: a
#: caller inspecting an idle-expired session's files can see why the worker
#: is gone without a separate lookup.
_EXPIRED_MARKER_NAME = ".codecalc-session-expired"


def _idle_ttl_seconds() -> float | None:
    """The configured idle TTL, or None when unset/invalid/non-positive.

    Read per call, not cached at import — same reasoning as
    runtimes.elevated_apply_allowed(): an operator's change should apply
    without a server restart.
    """
    raw = os.environ.get(IDLE_TTL_ENV, "").strip()
    if not raw:
        return None
    try:
        ttl = float(raw)
    except ValueError:
        return None
    return ttl if ttl > 0 else None


def _touch(session_id: str) -> None:
    """Record activity, resetting the idle clock. Caller holds `_lock`."""
    _LAST_ACTIVITY[session_id] = _now()


def _note_activity(session_id: str) -> None:
    """Reset the idle clock from any session entry point. Takes `_lock`.

    "Idle" has to mean "nothing has called into this session", not "nothing
    has EXECUTED in it". The first version updated the clock only in
    `execute()`, so a session being actively used through
    `session_write_file` / `session_files` / `session_read_file` /
    `session_artifacts` — staging inputs, polling for an artifact a long
    background job is producing — aged out and had its worker reaped
    underneath a caller that had never stopped using it. The README already
    described the knob as reaping a session "untouched for longer than
    this", and that is the behaviour worth having, so the code moved to meet
    it rather than the sentence being narrowed to match the code.

    Only worker sessions are tracked (`_LAST_ACTIVITY`), so the membership
    test is what keeps a workspace-only session — which holds no long-lived
    process and is not the leak this exists for — out of the table entirely.
    """
    with _lock:
        if session_id in _workers:
            _touch(session_id)


def _reap_then_note(session_id: str) -> None:
    """Run the idle-expiry gate, THEN record activity (THE-779 fix wave, F4).

    The non-execute entry points — write_file / list_files / resource_read /
    artifacts — recorded activity by calling `_note_activity` directly, which
    REFRESHES the idle clock. A worker already idle past the TTL was thereby
    revived by a bare workspace touch instead of reaped, and the next execute()
    then ran in the ORIGINAL worker rather than returning the documented expiry
    error — contradicting the README's "reaped on next access to ANY session".

    Reaping first (the same lazy check-on-access execute() runs) fixes it: an
    expired worker is torn down here, and `_note_activity`'s own
    `session_id in _workers` guard then declines to re-arm a clock for a worker
    that no longer exists. A still-live worker is NOT reaped (`_reap_if_idle`
    returns False) and its clock is refreshed exactly as before, so an in-use
    session is unaffected (test 8). These file paths never spawn a worker, so
    the two lock acquisitions here carry none of the silent-respawn risk that
    made execute() route through the single-critical-section
    `_get_worker_or_expired` instead: the worst a race can do is skip a clock
    refresh, which is the safe direction.
    """
    _reap_if_idle(session_id)
    _note_activity(session_id)


def _mark_expired_locked(session_id: str) -> None:
    """Add `session_id` to the bounded in-memory expired cache.

    Caller holds `_lock`. FIFO eviction once `_EXPIRED_CAP` is exceeded —
    the durable marker file (written by the caller, OUTSIDE the lock, right
    after this) is what an evicted lookup falls back to, so eviction here
    does not reopen the silent-respawn hole.
    """
    if session_id in _EXPIRED:
        return
    _EXPIRED.add(session_id)
    _EXPIRED_ORDER.append(session_id)
    while len(_EXPIRED_ORDER) > _EXPIRED_CAP:
        _EXPIRED.discard(_EXPIRED_ORDER.popleft())


def _discard_expired_locked(session_id: str) -> None:
    """Remove `session_id` from the bounded expired cache, if present.

    Caller holds `_lock`. Used by `start()` (a fresh id should never inherit
    a stale marker) and `stop()` (an explicitly torn-down session has
    nothing left to remember). `deque.remove` is O(n), which is fine here —
    neither caller is a hot path, unlike the per-call read in
    `_get_worker_or_expired`.
    """
    if session_id not in _EXPIRED:
        return
    _EXPIRED.discard(session_id)
    with contextlib.suppress(ValueError):
        _EXPIRED_ORDER.remove(session_id)


def _write_expired_marker(session_id: str) -> None:
    """Best-effort durable record that `session_id`'s worker was reaped.

    Best-effort deliberately: a failure to write it (a read-only mount, an
    already-deleted directory raced by something else) must not fail the
    reap itself. While the in-memory cache still holds the entry, callers
    get the right answer regardless; losing the marker only narrows, rather
    than removes, the protection an eviction later falls back on.
    """
    try:
        d = _session_dir(session_id)
        (d / _EXPIRED_MARKER_NAME).write_text(
            f"reaped {time.time():.3f} (idle-expiry, {IDLE_TTL_ENV})\n",
            encoding="utf-8")
    except (OSError, ValueError):
        pass


def _is_expired_on_disk(session_id: str) -> bool:
    """Durable fallback for a `session_id` evicted from the in-memory cache."""
    try:
        return (_session_dir(session_id) / _EXPIRED_MARKER_NAME).is_file()
    except (OSError, ValueError):
        return False


def _maybe_reap_locked(session_id: str) -> Worker | None:
    """If `session_id`'s worker is idle past the TTL, remove it from
    `_workers`/`_LAST_ACTIVITY`, mark it expired, and return the worker for
    the caller to `.close()` OUTSIDE the lock. None if there is nothing to
    reap (no TTL configured, no worker, or not yet idle).

    Caller holds `_lock`. Pure bookkeeping mutation only — no I/O, no
    process teardown — so this is safe to run inside the critical section.
    """
    ttl = _idle_ttl_seconds()
    if ttl is None:
        return None
    w = _workers.get(session_id)
    last = _LAST_ACTIVITY.get(session_id)
    if w is None or last is None:
        return None
    if _now() - last <= ttl:
        return None
    del _workers[session_id]
    del _LAST_ACTIVITY[session_id]
    _mark_expired_locked(session_id)
    return w


def _reap_if_idle(session_id: str) -> bool:
    """Kill `session_id`'s worker if it has been idle past the TTL.

    Lazy check-on-access: no timer, no thread, nothing that can outlive the
    process the way an un-unref()'d watcher does (this repo's own history on
    the Node side). The cost is that an idle worker is not reaped until
    SOMETHING calls this — `sweep_idle_sessions()` below is the explicit
    hook for a caller that wants to reap without waiting on a particular
    session's next call. `execute()` uses `_get_worker_or_expired` instead
    of this function directly — see its docstring for why.
    """
    with _lock:
        w = _maybe_reap_locked(session_id)
    if w is None:
        return False
    w.close()  # the existing teardown path — no new kill logic
    _write_expired_marker(session_id)
    return True


def _get_worker_or_expired(session_id: str) -> tuple[Worker | None, bool]:
    """(worker, is_expired) for `execute()` — ONE critical section.

    fix-round-1 CRITICAL: the previous shape called `_reap_if_idle()` (lock
    acquisition #1), then checked `if session_id in _EXPIRED` UNLOCKED, then
    separately did `with _lock: w = _workers.get(session_id)` (lock
    acquisition #2). A concurrent reap landing in the window between the
    unlocked `_EXPIRED` check and the second lock acquisition left a caller
    holding a STALE "not expired" answer from the first read alongside a
    FRESH "no worker" answer from the second — which fell through to the
    workspace-only branch and silently re-ran the call as a fresh,
    unconfined process. Reviewer reproduced it live: PID confirmed dead, yet
    ok=True from a fresh unconfined process.

    Collapsing the `_EXPIRED` check and the worker lookup/reap into ONE
    `with _lock:` block removes the window structurally: by mutual
    exclusion, nothing else can observe or mutate `_EXPIRED`/`_workers`
    between the two reads, because there is no longer a gap between them for
    anything to land in. `Worker.close()` still runs OUTSIDE the lock
    (unchanged reasoning: it waits up to 3s, and holding `_lock` across that
    would block every other session's calls) — but by the time it runs, the
    bookkeeping this function answers FROM is already fully committed.
    """
    to_close: Worker | None = None
    with _lock:
        if session_id in _EXPIRED:
            return None, True
        to_close = _maybe_reap_locked(session_id)
        if to_close is None:
            w = _workers.get(session_id)
    if to_close is not None:
        to_close.close()
        _write_expired_marker(session_id)
        return None, True
    if w is None and _idle_ttl_seconds() is not None and _is_expired_on_disk(session_id):
        # No worker, and this call did not just reap one. Could be a
        # genuine workspace-only session — OR a stateful session whose
        # expired-cache entry was evicted (bounded `_EXPIRED`, see above).
        # The marker file is the durable answer eviction cannot lose.
        return None, True
    return w, False


def sweep_idle_sessions() -> list[str]:
    """Reap every worker session idle past the configured TTL right now.

    A sweep hook, not a background thread: nothing here runs unless a caller
    invokes it (an operator's health check, a periodic MCP tool, a test —
    this module does not schedule one itself), so it cannot leak a thread
    that keeps the process alive after every session is stopped.

    Returns the session ids reaped.
    """
    if _idle_ttl_seconds() is None:
        return []
    with _lock:
        candidates = list(_workers)
    return [sid for sid in candidates if _reap_if_idle(sid)]


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


def _guard_error(exc: ValueError) -> dict:
    """`_session_dir`/`_jail` refusals, converted to the result contract.

    Both raise rather than return `{"ok": False, ...}` — the ordinary Python
    idiom for a defensive check reused from many call sites — but every
    PUBLIC session function in this module returns a result dict like the
    rest of it, never an exception. Before this (#212), the raise crossed
    every layer above it — SessionService, the `@mcp.tool()` wrapper, the SDK
    itself — uncaught, so the caller got a bare protocol-level error instead
    of the `ok`/`code`/`remedy` shape every other refusal here carries.

    "invalid session id" is a malformed argument (VALIDATION, the same
    bucket a bad expression or an unknown language lands in); the
    traversal/escape guards (`_jail`'s "path escapes session workspace",
    `_session_dir`'s defence-in-depth "session path escapes session root")
    are jail refusals (PERMISSION_DENIED) — the two codes errors.py already
    documents for exactly these situations.
    """
    message = str(exc)
    code = errors.VALIDATION if "invalid session id" in message else errors.PERMISSION_DENIED
    return errors.error_result(code, message)


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
        _touch(session_id)
        # A fresh uuid4-derived id will not collide with a previously-expired
        # one in practice, but a hygienic start() should not leave a stale
        # _EXPIRED entry a future id could theoretically inherit.
        _discard_expired_locked(session_id)
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
        _LAST_ACTIVITY.pop(session_id, None)
        # The marker file itself is removed for free below, when the whole
        # workspace directory is deleted — nothing left to fall back to on
        # disk, so the in-memory cache entry (if the eviction cap hasn't
        # already dropped it) has nothing left to duplicate either.
        _discard_expired_locked(session_id)
    if w is not None:
        w.close()
    try:
        d = _session_dir(session_id)
    except ValueError as exc:
        return _guard_error(exc)
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

#: The spill mechanism below (THE-783, see SPILL_CAPTURE_KB) is scoped to
#: this same "needs a fresh process" boundary: a stateful worker's own 64 KiB
#: cap is hardcoded inside its bootstrap protocol script and applied BEFORE
#: the bytes ever cross into this process, so there is nothing here to
#: capture a larger version of without changing that protocol — same reason
#: `max_output_kb` above cannot be honoured for a worker either. A caller
#: whose worker output is oversized gets the same remedy: session_run (or a
#: workspace session) instead of the stateful worker.

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
    try:
        d = _session_dir(session_id)
    except ValueError as exc:
        return _guard_error(exc)
    if not d.is_dir():
        return {"ok": False, "error": f"unknown session '{session_id}'"}
    # THE-894: refused BEFORE anything runs, not just before this module's
    # own writes — executed code can write straight into the workspace
    # (open("x", "w") in a worker, or a fresh process's own output files),
    # which no per-write check below can bound. See `quota_precheck`'s
    # docstring for why re-measuring here (rather than a sticky flag) is what
    # makes "refused until freed" self-healing.
    quota_refusal = quota_precheck(session_id)
    if quota_refusal is not None:
        return quota_refusal
    # Lazy check-on-access (THE-779): reap this session's worker if it has
    # sat idle past the configured TTL, and learn whether it already had
    # been. ONE call, ONE critical section — see `_get_worker_or_expired`'s
    # docstring for the fix-round-1 CRITICAL this replaced (two separate
    # lock acquisitions let a concurrent reap land in between them).
    w, expired = _get_worker_or_expired(session_id)
    if expired:
        # NOT a fall-through to the workspace-only branch below. Running the
        # call there would be a silent respawn of exactly the state
        # guarantee the caller lost when its worker was reaped.
        ttl = _idle_ttl_seconds()
        window = f"{ttl:g}s" if ttl is not None else "its configured TTL"
        return errors.error_result(
            errors.WORKER_FAILURE,
            f"session '{session_id}' expired after sitting idle past {window}; "
            "call session_stop and start a new one")
    if w is not None:
        if language and registry.canonical(language) != w.language:
            return {"ok": False, "error": f"session is {w.language}, not {language}"}
        asked = {"max_memory_mb": max_memory_mb, "max_cpu": max_cpu,
                 "no_net": no_net, "max_output_kb": max_output_kb}
        out = w.run(code, stdin=stdin, timeout=timeout)
        # Any call is activity, whether the executed code itself succeeded or
        # not — an idle SESSION is one nothing has called into, not one whose
        # last call happened to error. Shared with every other entry point:
        # see `_note_activity`.
        _note_activity(session_id)
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
        # THE-894 point 4: the worker just ran arbitrary code with the
        # workspace as its cwd — measure what it left behind and disclose an
        # over-quota session rather than let it grow silently forever.
        return quota_postcheck(session_id, out)
    # workspace-only session: fresh process in the session dir, fully sandboxed
    lang = registry.canonical(language) if language else "python3"
    if max_output_kb > 0:
        # An EXPLICIT caller ceiling is honoured exactly — see SPILL_CAPTURE_KB.
        result = executor.execute(lang, code, stdin=stdin, timeout=timeout, workdir=str(d),
                                  max_memory_mb=max_memory_mb, max_output_kb=max_output_kb,
                                  max_cpu=max_cpu, no_net=no_net)
        return quota_postcheck(session_id, result)
    result = executor.execute(lang, code, stdin=stdin, timeout=timeout, workdir=str(d),
                              max_memory_mb=max_memory_mb, max_output_kb=SPILL_CAPTURE_KB,
                              max_cpu=max_cpu, no_net=no_net)
    result = spill_if_truncated(session_id, result, 0)
    return quota_postcheck(session_id, result)


def _spill_dir(d: Path) -> Path:
    """The session's spill subdirectory, jailed like every other session path.

    `_jail` FIRST, `mkdir` second, and that order is the whole fix. The first
    version composed the path by hand (`d / _SPILL_DIRNAME`) and never jailed
    it, which was an arbitrary-directory write AND delete in the UNSANDBOXED
    server process:

    - `mkdir(exist_ok=True)` does not follow a symlink at the final component
      — it just swallows the EEXIST the link produces — so a `.codecalc-spill`
      SYMLINK survived it intact and every later operation resolved through it.
    - `O_NOFOLLOW` on the spill FILE guards only the final component. It never
      saw the swapped directory — and a parent component is exactly what
      `_jail`'s resolve() does check.
    - Executed code runs with the workspace as its cwd, so replacing
      `.codecalc-spill` with a symlink to anywhere the server user can reach
      is something a session can simply do.

    Routed through the same `_jail` that already guards write_file,
    list_files and resource_read rather than a bespoke check, so there is one
    definition of "inside this workspace" for every path in this module. A
    refusal raises ValueError, which is what `_jail` raises everywhere else
    and what `guarded_call` already classifies for callers.
    """
    spill = _jail(d, _SPILL_DIRNAME)
    spill.mkdir(exist_ok=True)
    return spill


def _prune_spill(d: Path) -> None:
    """Keep only the _SPILL_RETENTION most recently written spill files.

    Same shape as run_supervisor.RunSupervisor._prune: a bounded count,
    oldest first, used here because no other quota governs this artifact
    class (see SPILL_CAPTURE_KB's docstring).

    Takes the SESSION directory and re-derives the jailed spill path itself
    rather than accepting one a caller already resolved: the delete path is
    held to the same strictness as the write path (this module's rule), so it
    cannot become a standalone arbitrary-unlink primitive if some future
    caller reaches it without jailing first. `glob` is one level deep and
    `unlink` removes a symlink rather than its target, so with the directory
    itself jailed there is nothing left here that can name a file outside the
    workspace.
    """
    spill = _jail(d, _SPILL_DIRNAME)
    files = sorted(spill.glob("*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[_SPILL_RETENTION:]:
        stale.unlink(missing_ok=True)


def _write_spill(session_id: str, d: Path, stream: str, data: bytes) -> str | None:
    """Write one stream's captured bytes into the session workspace.

    Returns the path RELATIVE to the session dir — readable through the same
    `_jail`-guarded routes (resource_read, session_files) as any other
    workspace file. The filename is generated HERE (uuid4), never taken from
    a caller, so the only traversal surface is the DIRECTORY, which
    `_spill_dir` jails. The earlier version of this docstring argued that `d`
    already being the jailed session root left nothing to defend; that was
    wrong, because the component UNDER `d` is one executed code controls.

    THE-894: returns None, writing nothing, if this write would exceed the
    per-artifact or disk-quota ceilings (`_write_guard`), checked and written
    under `_disk_lock` as one step — see that lock's docstring. A spill is a
    best-effort EXTRA copy of output an execution already captured and
    already returns (truncated) inline; refusing the disk write must not
    turn an execution that already succeeded into a failed one, so the
    caller (`spill_if_truncated`) treats None as "kept the truncated inline
    value, skipped the extra copy on disk", never as an error to propagate.
    """
    with _disk_lock:
        # A spill file is always brand-new (uuid4-named), so its net effect
        # on disk usage always equals its full size — no overwrite case here.
        if _write_guard(session_id, d, len(data), len(data), is_new_file=True) is not None:
            return None
        spill = _spill_dir(d)
        name = f"{uuid.uuid4().hex}-{stream}.bin"
        target = spill / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
        fd = os.open(target, flags, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        _prune_spill(d)
        return f"{_SPILL_DIRNAME}/{name}"


def _requested_cap_bytes(max_output_kb: int) -> int:
    """The cap a caller asked for, or the executor's own default."""
    return max_output_kb * 1024 if max_output_kb > 0 else executor.MAX_OUTPUT_BYTES


def spill_if_truncated(session_id: str, result: dict, requested_cap_kb: int) -> dict:
    """Move oversized stdout/stderr from an inline truncation to a workspace
    artifact instead of dropping it (THE-783).

    Call this AFTER executing at a CAPTURE cap (SPILL_CAPTURE_KB) that is
    `>=` the caller's own requested cap, so `result['stdout']`/`['stderr']`
    may already hold more than the caller asked to see inline. This
    re-applies the caller's cap for the INLINE value — so a caller reading
    `stdout` sees exactly what it always would have, truncated at exactly
    the same length — and, only where that trims something real, writes the
    captured bytes to a session file and references it instead of the
    prefix being all a caller can ever get back.
    """
    d = _session_dir(session_id)
    if not d.is_dir():
        return result
    cap = _requested_cap_bytes(requested_cap_kb)
    spilled = False
    for stream in ("stdout", "stderr"):
        text = result.get(stream)
        if not isinstance(text, str):
            continue
        # Re-encoded rather than compared as a Python string length: the cap
        # (and the executor's own truncation this mirrors) is a BYTE budget,
        # and codecalc's whole pipeline already represents output as text
        # decoded with errors="replace" — this inherits that same fidelity
        # rather than promising a stronger one nothing upstream provides.
        data = text.encode("utf-8", errors="replace")
        if len(data) <= cap:
            continue
        # The capture itself may have hit ITS OWN ceiling, and the re-encoding
        # here may exceed what the resource route will ever serve. Either way
        # the spill is fuller than the inline value but is not proven to be
        # the WHOLE stream, which is what this one flag says.
        capped = len(data) >= SPILL_CAPTURE_KB * 1024
        if len(data) > RESOURCE_MAX_BYTES:
            # A spill nobody can fetch is not a spill. The capture ceiling is
            # SPILL_CAPTURE_KB of RAW stream bytes, but what lands on disk is
            # this errors="replace" RE-ENCODING of an already-decoded str,
            # where one invalid input byte becomes a 3-byte U+FFFD — so a
            # capture well under its own ceiling can still encode to 3x that.
            # `resource_read` refuses anything over RESOURCE_MAX_BYTES
            # OUTRIGHT (None, not a short read), so past this point the URI
            # below would name a file every fetch fails on. Bounding the
            # WRITE by the same constant the READ enforces makes "everything
            # this writes is fully readable back" true by construction,
            # instead of true only for output that happens to be ASCII.
            data = data[:RESOURCE_MAX_BYTES]
            capped = True
        rel = _write_spill(session_id, d, stream, data)
        result[stream] = data[:cap].decode("utf-8", errors="replace") + "\n…[truncated]"
        if rel is None:
            # THE-894: the disk-quota/artifact-cap gate refused the spill
            # WRITE. The execution already succeeded and already has a real
            # (truncated) inline value above — that stands; this only says
            # the fuller copy could not be kept on disk, and why, so a
            # caller does not have to guess why `..._spill` is absent this
            # time when it was present on an earlier, smaller run.
            result[f"{stream}_spill_refused"] = (
                f"disk-quota ceiling reached; only the truncated inline "
                f"{stream} above is available for this run")
        else:
            result[f"{stream}_spill"] = f"codecalc://session/{session_id}/files/{rel}"
            if capped:
                result[f"{stream}_spill_capped"] = True
        spilled = True
    if spilled:
        result["output_truncated"] = True
    return result


def write_file(session_id: str, path: str, content: str) -> dict:
    try:
        d = _session_dir(session_id)
        if not d.is_dir():
            return {"ok": False, "error": f"unknown session '{session_id}'"}
        _reap_then_note(session_id)  # F4: reap an expired worker, never revive it
        target = _jail(d, path)
    except ValueError as exc:
        # #212: `_jail`/`_session_dir` REFUSE an out-of-workspace or malformed
        # request by raising, same as every other guard in this module — but
        # this is a PUBLIC session function, which returns `{"ok": False,
        # ...}` like the rest of them, never an exception.
        return _guard_error(exc)
    data = content.encode("utf-8", errors="replace")
    # THE-894: the quota check and the write are ONE critical section under
    # `_disk_lock` — see that lock's docstring for why a race here would
    # otherwise let two concurrent writes both pass the check and together
    # exceed the quota. Never a partial write: refused entirely, or written
    # entirely, nothing in between.
    with _disk_lock:
        is_new = not target.exists()
        # fix round 3: `_session_dir_size` (inside `_disk_quota_refusal`)
        # already counts a PRE-EXISTING `target` at its current size, so
        # checking the FULL new size double-counts it and wrongly refuses a
        # same-size or SHRINKING overwrite near quota. The NET delta — new
        # size minus what is already there — is what actually changes
        # workspace usage. `lstat`, not `stat`: this must never be inflated
        # by following a symlink to an unrelated file's size, the same rule
        # `_iter_regular_files`/`_artifact_entries` apply everywhere else in
        # this module. A missing/unreadable target (races away, or is not a
        # plain file `_write_nofollow` can size this way) falls back to 0 —
        # the existing behaviour for a genuinely new file.
        try:
            existing_size = 0 if is_new else target.lstat().st_size
        except OSError:
            existing_size = 0
        net_bytes = len(data) - existing_size
        refusal = _write_guard(session_id, d, len(data), net_bytes, is_new_file=is_new)
        if refusal is not None:
            return refusal
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_nofollow(target, content)
    # `.as_posix()` for the same protocol-path reason as artifacts(): the
    # returned path is a caller-facing identity that must read back the same
    # on Windows, where `str(relative_to)` would emit backslashes for a nested
    # write while read_file/run_file speak forward slashes.
    return {"ok": True, "path": target.relative_to(d).as_posix()}


def list_files(session_id: str, path: str = "") -> dict:
    try:
        d = _session_dir(session_id)
        if not d.is_dir():
            return {"ok": False, "error": f"unknown session '{session_id}'"}
        _reap_then_note(session_id)  # F4: reap an expired worker, never revive it
        base = _jail(d, path)
    except ValueError as exc:
        return _guard_error(exc)  # #212, see write_file's comment above
    if not base.is_dir():
        return {"ok": False, "error": f"no such directory: {path}"}
    return {"ok": True, "path": path or ".", "files": _list(base)}


def resource_read(session_id: str, path: str,
                  max_bytes: int = RESOURCE_MAX_BYTES) -> tuple[bytes, str] | None:
    """Serve a session file as an MCP resource: (bytes, mime_type) or None.

    Image files are served as image/png|jpeg|gif|webp so MCP clients render
    them inline; everything else as application/octet-stream.
    """
    import mimetypes
    d = _session_dir(session_id)
    _reap_then_note(session_id)  # F4: reap an expired worker, never revive it
    target = _jail(d, path)
    data = _read_nofollow(target, max_bytes)
    if data is None:
        return None
    mime, _ = mimetypes.guess_type(str(target))
    if mime and mime.startswith("image/"):
        return data, mime
    return data, "application/octet-stream"


def artifacts(session_id: str) -> dict:
    """Files created by executed code (anything beyond the runner's own files)."""
    try:
        d = _session_dir(session_id)
    except ValueError as exc:
        return _guard_error(exc)  # #212, see write_file's comment
    if not d.is_dir():
        return {"ok": False, "error": f"unknown session '{session_id}'"}
    _reap_then_note(session_id)  # F4: reap an expired worker, never revive it
    # lstat, not is_file()/stat(): those FOLLOW a symlink, and a session can
    # plant one pointing outside the workspace (`ln -s /etc/shadow leak`) to
    # make this listing disclose the target's size or existence — the same
    # class of leak #208 fixed in `_list()` below, and exactly what
    # `session_read_file` already refuses via `_jail`'s
    # resolve()-then-boundary-check. A symlink is excluded rather than
    # reported: this function's contract is "files created inside the
    # workspace", and following a link to describe what may lie outside it is
    # not that. `_artifact_entries` (shared with the THE-894 artifact-count
    # cap, so the two never define "artifact" differently) applies this rule.
    files = [
        # `.as_posix()`, not `str()`: the workspace path is a protocol-facing
        # identity (it composes the file's `codecalc://session/.../files/<path>`
        # resource URI and is what a caller passes back to read_file/run_file).
        # `str(relative_to)` emits the OS separator, so a nested artifact
        # reported "data\\x" on Windows while write_file/read_file take
        # "data/x" — the same value the fallback matrix asserts. Forward
        # slashes keep the identity identical on ubuntu, macos and windows;
        # pathlib accepts "/" as input on every platform, so the reported path
        # round-trips.
        {"path": p.relative_to(d).as_posix(), "size": size}
        for p, size in _artifact_entries(d)
    ]
    return {"ok": True, "session_id": session_id, "artifacts": files}


def _list(d: Path) -> list[dict]:
    """The directory listing behind `session_files`.

    #208: `p.is_dir()`/`p.stat()` FOLLOW a symlink, so a session could plant
    one pointing outside the workspace (`ln -s /etc/shadow leak`) and this
    listing would disclose the target's size or existence — leaking
    information about a path `session_read_file` already refuses to touch
    (that refusal comes from `_jail`'s resolve()-then-boundary-check, which
    also follows the symlink, just to REJECT rather than report). `lstat`
    never follows, so a symlink entry is reported as a symlink — never as
    whatever it happens to point at, inside the workspace or out.
    """
    out = []
    for p in sorted(d.iterdir()):
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode):
            out.append({"path": p.name, "type": "symlink"})
        elif stat.S_ISDIR(st.st_mode):
            out.append({"path": p.name + "/", "type": "dir"})
        else:
            out.append({"path": p.name, "type": "file", "size": st.st_size})
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


def _read_nofollow(target: Path, max_bytes: int) -> bytes | None:
    """Read `target`, or None if it is not a regular file or is over the cap.

    The read counterpart of `_write_nofollow`, and it exists because #107 fixed
    the write path and left this one open — the same TOCTOU, one direction over.
    `_jail` resolves the path and this opens it, and code running in the session
    shares the workspace as its cwd, so it can drop a symlink into the resolved
    path in between. Here that would make the SERVER read a file outside the
    workspace and hand the bytes to the MCP client. The Landlock confinement
    added in #107 does not cover it: that confines the worker, and this read
    happens in the server process.

    Three things this closes, not one:

    - O_NOFOLLOW refuses a symlink at the final component, as on the write side.
    - `fstat` on the OPEN descriptor replaces `target.is_file()`. Checking the
      path and then opening it is two syscalls with the same window between them
      that the check is meant to remove; asking the descriptor is the only form
      that cannot be swapped underneath.
    - The size is read from that same `fstat` and refused BEFORE any content is
      read. The previous code called `read_bytes()` and then compared `len(data)`
      to the cap, so an oversized artifact was fully loaded into memory before
      being rejected. That is the defect `read_capped` had on the Rust side
      (#103) reproduced here in Python, and a session can create the file that
      triggers it.

    O_NONBLOCK so that a FIFO left in the workspace cannot park the server on
    open() waiting for a writer. It has no effect on a regular file, which the
    S_ISREG check has by then established this is.
    """
    flags = os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(target, flags)
    except OSError:
        # ELOOP (symlink), ENOENT, EACCES, ENXIO — all "not a file this may
        # serve", and none of them worth distinguishing to the caller, which
        # already treats None as "no such resource".
        return None
    # fstat the RAW descriptor, before fdopen. Ordering is load-bearing, not
    # style: `os.open` SUCCEEDS on a directory, and `os.fdopen` on that
    # descriptor then raises IsADirectoryError. With the fstat inside the
    # `with`, that exception escaped `_read_nofollow` entirely — the old
    # `is_file()` check had returned None for a directory — AND leaked the
    # descriptor, because os.open had already succeeded and nothing closed it.
    # Measured: `session_read_file` on a directory raised instead of reporting,
    # and every attempt burned an fd toward RLIMIT_NOFILE.
    #
    # Screening on S_ISREG here rejects directories, device nodes, sockets and
    # FIFOs before fdopen is ever reached, and the try/finally guarantees the
    # descriptor is closed on every path that does not hand it to fdopen.
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    with os.fdopen(fd, "rb") as fh:
        # max_bytes + 1, and reject on the extra byte. The fstat above rejects a
        # file that is ALREADY too big without reading it, but a session can
        # append to its own artifact between that fstat and this read — the
        # descriptor is open, and nothing about holding it freezes the size.
        # `fh.read(max_bytes)` would then return the first max_bytes of a file
        # that is now over the cap and report success, so the caller gets a
        # SILENTLY TRUNCATED resource while this function's contract says an
        # over-cap file returns None.
        #
        # One byte past the cap distinguishes "exactly at the limit" from
        # "longer than the limit" without reading the whole thing, which is the
        # same shape as `read_capped`'s `take(cap + 1)` on the Rust side (#103).
        data = fh.read(max_bytes + 1)
        return None if len(data) > max_bytes else data


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
        unresolved = Path(found)
        real = unresolved.resolve()
        ro.append(str(real))
        # <prefix>/bin/python3 -> <prefix>. The stdlib, shared objects and (for
        # node) lib/node_modules all hang off it.
        if real.parent.name in ("bin", "Scripts"):
            ro.append(str(real.parent.parent))
        # The UNRESOLVED prefix as well, when it differs. A venv's python3 is a
        # symlink into a base interpreter, and resolving it grants only the
        # base — but sys.executable stays the venv path, and CPython's
        # site.venv() reads <venv>/pyvenv.cfg BESIDE it during interpreter
        # startup (plus <venv>/lib for site-packages). With only the resolved
        # prefix granted, Landlock denies that read and the worker dies before
        # the bootstrap runs a single line. `uvx codecalc` and any MCP client
        # spawning the console script from a venv resolve python3 exactly this
        # way, so this is the COMMON deployment, not an edge case. Measured:
        # the worker exited 1 with `PermissionError: ... .venv/pyvenv.cfg`
        # while session_start reported ok=True (see _spawn_worker's warm-up).
        if unresolved != real and unresolved.parent.name in ("bin", "Scripts"):
            ro.append(str(unresolved.parent.parent))
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
        if not r.get("ok"):
            # ANY failed warm-up is fatal, not just one that carries `stderr`.
            # The old condition was `not ok AND r.get("stderr")` — but a worker
            # that dies BEFORE answering returns {"ok": False, "error":
            # "worker closed"} with no stderr key at all, so the gate passed
            # exactly the workers that were already dead, and session_start
            # reported ok=True for a session whose every later call would fail
            # with "worker died". A gate keyed on the wrong field is the shape
            # this repo keeps finding; this one was in the function whose job
            # is to be the gate. The worker's stderr tail is carried because it
            # holds the actual traceback (the venv/Landlock case prints a
            # PermissionError there that no other surface reports).
            detail = (r.get("stderr") or r.get("error") or "no detail")[:200]
            tail = w.stderr_tail(5)
            w.close()
            reason = f"worker failed on warm-up: {detail}"
            if tail:
                reason += f" | worker stderr: {tail[:400]}"
            return None, reason
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
