"""Session workspace confinement.

The bug these exist for: `_jail()` compared resolved paths with
`str(p).startswith(str(d))`. A string prefix is not a path boundary — a SIBLING
directory whose name merely extends the session id satisfies it:

    _jail(<root>/python3-deadbeef, '../python3-deadbeefEVIL/pwned.txt')
    ->  <root>/python3-deadbeefEVIL/pwned.txt      (accepted)

`session_write_file` then mkdir -p's that path into existence, so one session
could write outside its own workspace. AUDIT.md claimed "every path is jailed
via resolve() prefix check — `..` escapes are rejected", which was true of the
`..` case it was tested against and false of the boundary in general.

`Path.is_relative_to` compares path COMPONENTS, so `python3-deadbeefEVIL` is
not under `python3-deadbeef` no matter how the strings line up.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import sessions

FAILS = []
SKIPS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")
    SKIPS.append(name)


def _can_symlink(base: pathlib.Path) -> bool:
    """Probe the capability rather than assume it.

    Symlink creation needs a privilege (SeCreateSymbolicLinkPrivilege) that an
    unprivileged Windows account does not hold by default, and attempting one
    there raises `OSError: [WinError 1314] A required privilege is not held
    by the client`. That is a fact about this machine, not a defect in
    `_jail` — so it is probed and skipped explicitly rather than letting the
    whole file abort on the first symlink call.
    """
    probe = base / "codecalc-symlink-probe"
    try:
        probe.symlink_to(base)
        return True
    except OSError:
        return False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def rejects(d, path) -> bool:
    try:
        sessions._jail(d, path)
    except ValueError:
        return True
    return False


# .resolve() the temp root up front. On Windows `mkdtemp` can hand back a short
# 8.3 path (C:\Users\RUNNER~1\...) while `_jail` returns the resolved long form,
# so comparing its output against an UNRESOLVED expected path failed for every
# legitimate path — a bad assertion in this file, not a bug in the jail. macOS
# has the same shape via /var -> /private/var.
tmp = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-jail-test-")).resolve()
session = tmp / "python3-deadbeef"
session.mkdir()
# A sibling whose name EXTENDS the session id — the string-prefix blind spot.
sibling = tmp / "python3-deadbeefEVIL"
sibling.mkdir()

# ── the regression ──────────────────────────────────────────────────────────
check("sibling dir extending the session id is REJECTED",
      rejects(session, "../python3-deadbeefEVIL/pwned.txt"))
check("sibling dir, no traversal component, is REJECTED",
      rejects(session, str(sibling / "pwned.txt")))

# ── the escapes that were already handled, still handled ────────────────────
check("parent traversal rejected", rejects(session, "../../etc/passwd"))
check("absolute path rejected", rejects(session, "/etc/passwd"))
check("deep traversal rejected", rejects(session, "a/b/../../../../etc/passwd"))

# ── a symlink out of the workspace must not become a write primitive ────────
# This case alone is skippable when the account cannot create symlinks; the
# traversal tests above (and the nested/flat/root/inner cases below) need no
# symlink privilege at all and stay mandatory regardless — they are the
# security-relevant checks and must never be skipped along with this one.
if _can_symlink(tmp):
    (session / "escape").symlink_to(tmp)
    check("symlink pointing outside is rejected", rejects(session, "escape/pwned.txt"))
else:
    skip("symlink pointing outside is rejected",
         "this account lacks symlink privilege (WinError 1314 or equivalent); "
         "the traversal tests remain mandatory and are unaffected")

# ── legitimate paths still work ─────────────────────────────────────────────
try:
    p = sessions._jail(session, "data/input.csv")
    ok_nested = p == (session / "data" / "input.csv").resolve()
except ValueError:
    ok_nested = False
check("nested path inside the workspace is allowed", ok_nested)

try:
    p = sessions._jail(session, "main.py")
    ok_flat = p == (session / "main.py").resolve()
except ValueError:
    ok_flat = False
check("plain filename is allowed", ok_flat)

try:
    p = sessions._jail(session, "")
    ok_root = p == session.resolve()
except ValueError:
    ok_root = False
check("empty path resolves to the workspace root", ok_root)

try:
    p = sessions._jail(session, "sub/../main.py")
    ok_inner = p == (session / "main.py").resolve()
except ValueError:
    ok_inner = False
check("traversal that stays INSIDE is allowed", ok_inner)

# ── session ids themselves ──────────────────────────────────────────────────
for bad in ("../evil", "a/b", "", "x" * 65, "we!rd"):
    try:
        sessions._session_dir(bad)
        rejected = False
    except ValueError:
        rejected = True
    check(f"session id {bad[:14]!r} rejected", rejected)

# ── a many-segment path must be refused WITHOUT reaching resolve() ──────────
# `_jail` used to run `Path.resolve()` on the raw caller string before doing
# any shape check. resolve() is worse-than-linear in segment count, and this
# runs in the SERVER process, outside `guarded_call`'s ceilings — a fuzzer
# input of `"a/" * 20000 + "x"` measured multiple seconds of server CPU per
# call, unbounded by anything else in the stack. The fix rejects on raw
# length/segment count BEFORE resolve() ever runs, so refusing an oversized
# path costs microseconds, not seconds — asserted here as "well under a
# second", not just "eventually returns", because a fix that merely made the
# resolve() call itself would still leave the DoS.
_many_segments = "a/" * 20000 + "x"
_t0 = time.time()
_many_segments_rejected = rejects(session, _many_segments)
_many_segments_elapsed = time.time() - _t0
check("a 20000-segment path is refused",
      _many_segments_rejected, f"-> elapsed={_many_segments_elapsed:.4f}s")
check("refusing a 20000-segment path takes well under a second",
      _many_segments_elapsed < 1.0, f"-> elapsed={_many_segments_elapsed:.4f}s")

_long_flat = "x" * 20000
_t0 = time.time()
_long_flat_rejected = rejects(session, _long_flat)
_long_flat_elapsed = time.time() - _t0
check("a 20000-char single-segment path is refused",
      _long_flat_rejected, f"-> elapsed={_long_flat_elapsed:.4f}s")
check("refusing a 20000-char path takes well under a second",
      _long_flat_elapsed < 1.0, f"-> elapsed={_long_flat_elapsed:.4f}s")

# The caps must not bite a legitimate deep-but-reasonable path.
check("a path well under both caps is still allowed",
      not rejects(session, "/".join(f"d{i}" for i in range(10)) + "/file.txt"))

# `_session_dir` takes `session_id`, already bounded by `_SAFE_NAME` to <=64
# chars with no separators — confirming the same DoS shape does not reach it.
_session_dir_rejected = False
try:
    sessions._session_dir("x" * 65)
    _session_dir_rejected = False
except ValueError:
    _session_dir_rejected = True
check("_session_dir still rejects an over-length session id (via _SAFE_NAME)",
      _session_dir_rejected)

# ── #104: a stateful session does not carry the one-shot guarantees ─────────
# `execute_code()` runs through codecalc-exec. `execute_code(session_id=...)`
# on python3/node runs a long-lived `subprocess.Popen`, which cannot have the
# Rust executor's ceilings, its per-call process-group kill or its --no-net
# shim. That difference is allowed. Being silent about it is not.
#
# The canary check is a DISJUNCTION on purpose, matching the issue's acceptance
# wording: either the file outside the workspace is unreadable, or the result
# says it is readable. Landlock is Linux-only, so a suite that demanded
# confinement would fail on macOS and Windows for being honest there.

_root = tempfile.mkdtemp(prefix="codecalc-104-")
sessions.SESSION_ROOT = pathlib.Path(_root) / "sessions"
_canary_dir = tempfile.mkdtemp(prefix="codecalc-canary-")
_canary = pathlib.Path(_canary_dir) / "canary.txt"
_canary.write_text("TOP-SECRET", encoding="utf-8")

_started = sessions.start("python3")
if not _started.get("ok"):
    skip("stateful session confinement", f"python3 worker unavailable: {_started.get('error')}")
else:
    _sid = _started["session_id"]
    try:
        _res = sessions.execute(_sid, "\n".join([
            "try:",
            f"    print('READ:' + open({str(_canary)!r}).read())",
            "except Exception as e:",
            "    print('DENIED:' + type(e).__name__)",
        ]))
        _out = _res.get("stdout", "")
        _unenf = _res.get("unenforced", [])
        _readable = _out.startswith("READ:")
        _says_readable = any(e.startswith("filesystem_confinement:") for e in _unenf)

        check("canary outside the workspace is unreadable, or the result says it is readable",
              (not _readable) or _says_readable,
              f"-> readable={_readable} disclosed={_says_readable}")

        # The disjunction must not be satisfied by BOTH halves being wrong in a
        # way that cancels: if the session claims confinement, the read has to
        # actually fail. This is the direction that catches a `confined: True`
        # set by a spawn path that never applied the ruleset.
        check("a session claiming confinement really is confined",
              (not _res.get("confined")) or (not _readable),
              f"-> confined={_res.get('confined')} readable={_readable}")

        # Unconditional: nothing above asked for a single ceiling.
        for _key in sessions._SESSION_STRUCTURAL_GAPS:
            check(f"structural gap {_key!r} disclosed without being asked for",
                  any(e.startswith(_key + ":") for e in _unenf),
                  f"-> {len(_unenf)} entries")

        check("a session-backed result names its backend",
              _res.get("backend") == "session-worker", f"-> {_res.get('backend')!r}")

        # Landlock is inherited across exec, so a child process is covered too.
        # Without this a session could shell out to read what it cannot open.
        if _started.get("confined"):
            _child = sessions.execute(_sid, "\n".join([
                "import subprocess",
                f"p = subprocess.run(['cat', {str(_canary)!r}], capture_output=True)",
                "print('RC:' + str(p.returncode))",
            ]))
            check("confinement is inherited by child processes",
                  "RC:0" not in _child.get("stdout", ""),
                  f"-> {_child.get('stdout', '').strip()!r}")
        else:
            skip("confinement is inherited by child processes", "worker not confined here")

        # TOCTOU: _jail() resolves the path, and the caller then opens it —
        # two syscalls with a window between them. Code inside the session
        # shares the workspace as its cwd, so it can drop a symlink into the
        # resolved path during that window.
        #
        # Two guards, and they cover different orderings. A symlink planted
        # BEFORE the call is caught by _jail, because resolve() follows it and
        # the target is then outside the workspace. A symlink planted AFTER
        # resolve() returns is invisible to that check, and the only thing left
        # is O_NOFOLLOW on the open. The race itself is not schedulable from a
        # test, so the second guard is exercised against the STATE the race
        # produces rather than by trying to win it.
        _wdir = pathlib.Path(_started["workdir"])
        _outside = pathlib.Path(_canary_dir) / "escaped.txt"
        _planted = _wdir / "planted.txt"
        try:
            _planted.symlink_to(_outside)
        except OSError as exc:
            skip("write_file symlink swap", f"symlink unsupported: {exc}")
        else:
            # #212: write_file used to let `_jail`'s ValueError escape
            # uncaught; it now catches it and returns the same {"ok": False,
            # "code": "permission_denied", ...} contract every other refusal
            # in this module carries (see sessions._guard_error).
            _write_result = sessions.write_file(_sid, "planted.txt", "pwned")
            _jail_caught = (_write_result.get("ok") is False
                            and _write_result.get("code") == "permission_denied")
            check("a symlink present before the call is refused by _jail",
                  _jail_caught and not _outside.exists(),
                  f"-> escaped={_outside.exists()} result={_write_result}")

            # The post-resolve case: hand _write_nofollow the exact path state
            # the race leaves behind and require it to refuse.
            try:
                sessions._write_nofollow(_planted, "pwned")
                _nofollow_refused = False
            except OSError:
                _nofollow_refused = True
            if not hasattr(os, "O_NOFOLLOW"):
                skip("_write_nofollow refuses a symlinked final component",
                     "O_NOFOLLOW does not exist on this platform")
            else:
                check("_write_nofollow refuses a symlinked final component",
                      _nofollow_refused and not _outside.exists(),
                      f"-> refused={_nofollow_refused} escaped={_outside.exists()}")
    finally:
        sessions.stop(_sid)

# ── the READ path must be as strict as the write path ──────────────────────
# #107 gave `write_file` O_NOFOLLOW and left `resource_read` doing
# `target.read_bytes()` straight after `_jail`. Same TOCTOU, one direction
# over, and the read side is the one that hands bytes back to the MCP client.
# The Landlock confinement does not cover it: that confines the WORKER, and
# this read happens in the server process.
_rd = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-read-"))
_outside_secret = _rd / "outside.txt"
_outside_secret.write_text("OUTSIDE-CANARY", encoding="utf-8")
_ws = _rd / "ws"
_ws.mkdir()

if not hasattr(os, "O_NOFOLLOW"):
    # Windows has no O_NOFOLLOW, so `_O_NOFOLLOW` is 0, the flag is a no-op and
    # the open follows the link. That is a documented platform gap — stated in
    # `_write_nofollow`'s docstring — and it is why the write case above is
    # skipped here too rather than asserted. Enshrining "it follows the symlink"
    # as expected behaviour would turn a known weakness into a guarantee.
    #
    # Mirroring the write guard is the fix for a red Windows run this file
    # caused on its first push: the write half had the check and the read half,
    # written minutes later, did not.
    skip("_read_nofollow refuses a symlinked final component",
         "O_NOFOLLOW does not exist on this platform")
elif _can_symlink(_ws):
    (_ws / "link.bin").symlink_to(_outside_secret)
    # Hand _read_nofollow the exact state the race leaves behind: a symlink
    # sitting at the already-resolved path.
    check("_read_nofollow refuses a symlinked final component",
          sessions._read_nofollow(_ws / "link.bin", 1 << 20) is None)
else:
    skip("_read_nofollow refuses a symlinked final component",
         "this account lacks symlink privilege")

# A regular file still reads, or the guard above would be indistinguishable
# from a function that always returns None.
(_ws / "real.bin").write_bytes(b"hello")
check("_read_nofollow still reads a regular file",
      sessions._read_nofollow(_ws / "real.bin", 1 << 20) == b"hello")

(_ws / "big.bin").write_bytes(b"x" * 4096)
check("_read_nofollow refuses a file over the cap",
      sessions._read_nofollow(_ws / "big.bin", 1024) is None)

# The cap has to be applied BEFORE the content is read, not after. The old
# implementation called `read_bytes()` and then compared `len(data)` to the
# cap, so an oversized artifact was fully resident in memory before being
# rejected — the same defect `read_capped` had on the Rust side (#103), and a
# session can create the file that triggers it.
#
# Asserting `is None` does NOT distinguish the two: both return None. Timing a
# SPARSE file does. 1 GiB costs no disk and no page cache, but reading it costs
# a second or more and a gigabyte of RSS, while rejecting it from `fstat` is
# effectively instant. Measured at 0.001s here against a 2s ceiling, so the
# margin does not depend on this machine being fast.
# POSIX only. `truncate()` gives a sparse file on ext4/APFS at no disk cost,
# but NTFS does not guarantee sparseness, so on Windows this would write a real
# 1 GiB of zeros to a CI runner's disk to test platform-independent logic.
if os.name == "posix":
    _sparse = _ws / "sparse.bin"
    with _sparse.open("wb") as _fh:
        _fh.truncate(1 << 30)
    _t0 = time.monotonic()
    _sparse_result = sessions._read_nofollow(_sparse, 1024)
    _elapsed = time.monotonic() - _t0
    # 0.5s: measured 0.000s for the fstat path and 1.07s for the read-then-check
    # path on this machine. The threshold sits an order of magnitude above the
    # former and below the latter, so it FAILS the old implementation rather
    # than merely reporting a nicer number next to it — which the first draft,
    # at 2.0s, did not.
    check("_read_nofollow rejects an oversized file without reading it",
          _sparse_result is None and _elapsed < 0.5,
          f"-> {_elapsed:.3f}s for a 1 GiB sparse file")
    _sparse.unlink()
else:
    skip("_read_nofollow rejects an oversized file without reading it",
         "needs sparse-file support; NTFS would write a real 1 GiB")

# A file that grows AFTER the fstat and before the read must be refused, not
# served truncated. The descriptor being open does not freeze the size, and a
# session can append to its own artifact in that window. `read(max_bytes)` would
# hand back the cap-sized prefix of a now-oversized file and report success —
# a silent truncation, against a contract that says over-cap returns None.
#
# Made deterministic by patching os.fstat to append during the call rather than
# by racing a real writer. Found by a review bot on the PR, which also built
# this reproduction; the version before it read exactly max_bytes and passed.
_grow = _ws / "grow.bin"
_grow.write_bytes(b"A" * 8)
_real_fstat = sessions.os.fstat


def _fstat_then_grow(fd):
    _st = _real_fstat(fd)
    with _grow.open("ab") as _gh:
        _gh.write(b"BBBBB")
    sessions.os.fstat = _real_fstat          # once, so the read itself is normal
    return _st


sessions.os.fstat = _fstat_then_grow
try:
    _grown = sessions._read_nofollow(_grow, 8)
finally:
    sessions.os.fstat = _real_fstat
check("_read_nofollow refuses a file that grew past the cap after fstat",
      _grown is None,
      f"-> returned {None if _grown is None else f'{len(_grown)} bytes'}, file is now {_grow.stat().st_size}")

# ...while a file exactly AT the cap still reads, so the +1 probe above cannot
# be satisfied by rejecting everything.
_exact = _ws / "exact.bin"
_exact.write_bytes(b"C" * 8)
check("_read_nofollow still reads a file exactly at the cap",
      sessions._read_nofollow(_exact, 8) == b"C" * 8)

# Everything that is NOT a regular file must come back as None, never as an
# exception. `os.open` SUCCEEDS on a directory and `os.fdopen` then raises
# IsADirectoryError, so the first version of this helper let that escape and
# leaked the descriptor with it — a regression against the `is_file()` check it
# replaced, found by running these shapes rather than by reading the code.
_shapes = [("directory", _ws), ("missing", _ws / "nope.bin")]
(_ws / "dangling").symlink_to(_ws / "no-such-target") if _can_symlink(_ws) else None
if (_ws / "dangling").is_symlink():
    _shapes.append(("dangling symlink", _ws / "dangling"))
if pathlib.Path("/dev/null").exists():
    _shapes.append(("character device", pathlib.Path("/dev/null")))
for _label, _p in _shapes:
    try:
        _r = sessions._read_nofollow(_p, 1 << 20)
        check(f"_read_nofollow returns None for a {_label}", _r is None, f"-> {_r!r}")
    except Exception as _exc:
        check(f"_read_nofollow returns None for a {_label}", False,
              f"-> RAISED {type(_exc).__name__}")

# And none of those paths may leak a descriptor. The leak was invisible per
# call; it only shows as a count that climbs, which is why this asserts the
# count rather than any single result.
_fd_dir = pathlib.Path(f"/proc/{os.getpid()}/fd")
if _fd_dir.is_dir():
    _fd_before = len(list(_fd_dir.iterdir()))
    for _ in range(200):
        for _, _p in _shapes:
            sessions._read_nofollow(_p, 8)
    _fd_after = len(list(_fd_dir.iterdir()))
    check("_read_nofollow leaks no descriptor on the reject paths",
          _fd_after <= _fd_before + 2, f"-> {_fd_before} -> {_fd_after} over 200 rounds")
else:
    skip("_read_nofollow leaks no descriptor on the reject paths", "no /proc on this platform")

# A FIFO must not park the server waiting for a writer.
#
# Stated honestly: the OLD code also declined this, because `is_file()` is
# False for a FIFO. This is a floor on the NEW implementation rather than proof
# of a fix — `fstat`+S_ISREG replaced `is_file()`, and O_NONBLOCK is what stops
# the open() itself from blocking before either check can run.
if hasattr(os, "mkfifo"):
    os.mkfifo(_ws / "pipe")
    check("_read_nofollow refuses a FIFO instead of blocking on it",
          sessions._read_nofollow(_ws / "pipe", 1 << 20) is None)
else:
    skip("_read_nofollow refuses a FIFO", "os.mkfifo unavailable on this platform")

# A workspace-only session runs a fresh sandboxed process per call, so it
# carries all of it. The empty list is a real answer, not a missing one, and
# asserting it keeps the disclosure from being pasted onto every session shape.
check("a workspace-only session declares no structural gaps",
      sessions._session_unenforced(None) == [])

# ── the warm-up gate must fail CLOSED, and a venv worker must survive ────────
#
# Two defects found together on a Landlock-capable host running codecalc from a
# venv — which is what `uvx codecalc` and every MCP client spawning the console
# script produce, i.e. the most common real deployment:
#
#   1. `_spawn_worker`'s warm-up rejected a worker only when the failing result
#      carried a `stderr` key. A worker that died BEFORE answering returns
#      {"ok": False, "error": "worker closed"} with no stderr at all, so the
#      gate passed it and session_start reported ok=True for a session whose
#      worker was already a corpse. Every later call then failed with
#      "worker died" — and the #104 confinement check above passed VACUOUSLY on
#      exactly that shape, because a dead worker reads nothing.
#
#   2. The reason the worker died: `_confinement_paths` grants the RESOLVED
#      interpreter prefix, but a venv's python3 is a symlink, sys.executable
#      stays the venv path, and CPython's site.venv() reads <venv>/pyvenv.cfg
#      beside it during startup. With only the resolved prefix granted,
#      Landlock denies that read and the interpreter dies before the bootstrap
#      runs a single line.

if os.name == "nt":
    skip("warm-up gate fails closed on a worker that dies unanswered",
         "the fake-interpreter fixture is a shell script")
    skip("a venv-resolved python3 worker survives confinement",
         "venv layout differs on Windows (Scripts/, no python3 name)")
else:
    import shutil as _shutil
    import subprocess as _subprocess
    import sys as _sys

    # (1) A worker that exits before answering must fail session_start LOUDLY.
    _fake_dir = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-fakepy-"))
    _fake = _fake_dir / "python3"
    _fake.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    _fake.chmod(0o755)
    _saved_path = os.environ.get("PATH", "")
    _saved_runtime = os.environ.get("CODECALC_RUNTIME_PATH")
    try:
        # Both PATHs, so resolution is deterministic regardless of which one
        # subprocess consults on this platform.
        os.environ["PATH"] = f"{_fake_dir}{os.pathsep}{_saved_path}"
        os.environ["CODECALC_RUNTIME_PATH"] = os.environ["PATH"]
        _dead = sessions.start("python3")
        check("warm-up gate fails closed on a worker that dies unanswered",
              _dead.get("ok") is False,
              f"-> ok={_dead.get('ok')} error={str(_dead.get('error'))[:80]}")
    finally:
        os.environ["PATH"] = _saved_path
        if _saved_runtime is None:
            os.environ.pop("CODECALC_RUNTIME_PATH", None)
        else:
            os.environ["CODECALC_RUNTIME_PATH"] = _saved_runtime
        _shutil.rmtree(_fake_dir, ignore_errors=True)

    # (2) A worker resolved through a venv must start AND answer. Exercises the
    # pyvenv.cfg read under Landlock where the kernel has it; on kernels
    # without Landlock the worker is unconfined and this asserts the same
    # behaviour, which is the contract either way.
    _venv_dir = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-venv-")) / "v"
    _made = _subprocess.run(
        [_sys.executable, "-m", "venv", "--without-pip", str(_venv_dir)],
        capture_output=True, timeout=120,
    )
    if _made.returncode != 0 or not (_venv_dir / "bin" / "python3").exists():
        skip("a venv-resolved python3 worker survives confinement",
             f"could not build a venv here: rc={_made.returncode}")
    else:
        _saved_path = os.environ.get("PATH", "")
        _saved_runtime = os.environ.get("CODECALC_RUNTIME_PATH")
        try:
            os.environ["PATH"] = f"{_venv_dir / 'bin'}{os.pathsep}{_saved_path}"
            os.environ["CODECALC_RUNTIME_PATH"] = os.environ["PATH"]
            _vs = sessions.start("python3")
            if not _vs.get("ok"):
                check("a venv-resolved python3 worker survives confinement",
                      False, f"-> start failed: {str(_vs.get('error'))[:200]}")
            else:
                _vr = sessions.execute(_vs["session_id"], "print(6*7)")
                check("a venv-resolved python3 worker survives confinement",
                      _vr.get("ok") is True and "42" in (_vr.get("stdout") or ""),
                      f"-> ok={_vr.get('ok')} stdout={(_vr.get('stdout') or '')!r:.40} "
                      f"error={str(_vr.get('error'))[:80]}")
                sessions.stop(_vs["session_id"])
        finally:
            os.environ["PATH"] = _saved_path
            if _saved_runtime is None:
                os.environ.pop("CODECALC_RUNTIME_PATH", None)
            else:
                os.environ["CODECALC_RUNTIME_PATH"] = _saved_runtime

# ── C1: spill helpers are jailed like every other session path ──────────────
# Reproduced before the fix, both directions, in the UNSANDBOXED SERVER
# process (the Rust sandbox has already returned by the time a spill is
# written). `_spill_dir` resolved `.codecalc-spill` without `_jail`, and
# `mkdir(exist_ok=True)` does NOT follow a symlink at the final component —
# it swallows the EEXIST the link produces and hands back a path that now
# names the link's target. `O_NOFOLLOW` on the spill FILE guards only the
# final component, so it never saw the swapped DIRECTORY. Executed code owns
# the workspace as its cwd, so planting that symlink is a thing a session can
# actually do, and it bought two primitives:
#
#   WRITE  — `_write_spill` creates an attacker-sized, attacker-content
#            `*.bin` in any directory the server user can reach.
#   DELETE — `_prune_spill` globs `*.bin` through the same unresolved path
#            and unlinks everything past `_SPILL_RETENTION`, including files
#            it never created and did not write.
#
# The delete half matters on its own: this repo's convention is that a delete
# path is held to the same strictness as the write path, and a guard that
# only covered `_write_spill` would have left `_prune_spill` a standalone
# arbitrary-unlink primitive.
_spill_root = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-spill-jail-")).resolve()
sessions.SESSION_ROOT = _spill_root / "sessions"
sessions.SESSION_ROOT.mkdir(parents=True, exist_ok=True)

if not _can_symlink(_spill_root):
    for _name in ("_write_spill refuses a symlinked spill directory",
                  "no spill payload lands outside the session workspace",
                  "a pre-existing file outside the workspace is NOT unlinked",
                  "_prune_spill refuses a symlinked spill directory",
                  "an oversized session run cannot write through a symlinked spill dir",
                  "an ordinary spill still writes inside the workspace"):
        skip(_name, "this account lacks symlink privilege, so the spill helpers "
                    "cannot be attacked this way here")
else:
    _victim = _spill_root / "victim"
    _victim.mkdir()
    # A pre-existing file the prune path must never be able to reach. Named
    # `*.bin` because that is exactly the glob `_prune_spill` unlinks through.
    _bystander = _victim / "pre-existing.bin"
    _bystander.write_bytes(b"NOT THE SERVER'S TO DELETE")

    _spill_sid = sessions.start("bash")["session_id"]
    _spill_d = sessions._session_dir(_spill_sid)
    # What executed code does with its own cwd.
    (_spill_d / sessions._SPILL_DIRNAME).symlink_to(_victim, target_is_directory=True)

    def _escapes(fn) -> bool:
        """True when the helper REFUSED with the jail's own error."""
        try:
            fn()
        except ValueError:
            return False
        return True

    # ONE call reproduces BOTH halves: `_write_spill` creates the payload and
    # then prunes. Retention of 1 means the newly written file is the only one
    # kept, so the pre-existing bystander — older, and reachable only through
    # the symlink — is what the prune unlinks. Measured pre-fix: the payload
    # appeared in `victim/` and `pre-existing.bin` was gone.
    _old_retention = sessions._SPILL_RETENTION
    sessions._SPILL_RETENTION = 1
    try:
        _escaped_write = _escapes(
            lambda: sessions._write_spill(_spill_sid, _spill_d, "stdout", b"P" * 4096))
    finally:
        sessions._SPILL_RETENTION = _old_retention
    check("_write_spill refuses a symlinked spill directory",
          not _escaped_write, f"-> escaped={_escaped_write}")
    check("no spill payload lands outside the session workspace",
          [p.name for p in sorted(_victim.glob("*.bin"))] == ["pre-existing.bin"],
          f"-> {[p.name for p in sorted(_victim.glob('*.bin'))]}")
    check("a pre-existing file outside the workspace is NOT unlinked",
          _bystander.is_file() and _bystander.read_bytes() == b"NOT THE SERVER'S TO DELETE",
          f"-> exists={_bystander.exists()}")

    # And the DELETE path refuses on its own, not merely because the write
    # ahead of it did. Same strictness as the write path is this repo's rule,
    # and a guard that only sat in `_write_spill` would leave `_prune_spill`
    # an arbitrary-unlink primitive for the next caller that reaches it.
    check("_prune_spill refuses a symlinked spill directory",
          not _escapes(lambda: sessions._prune_spill(_spill_d)))

    # End-to-end through the real entry point: oversized output from executed
    # code, spilled by `sessions.execute`, with the trap already in place.
    #
    # A REDUNDANT confirmation over the direct `_write_spill`/`_prune_spill`
    # calls above (which already assert the security invariant unconditionally
    # on synthetic bytes), but a valuable one because it exercises the whole
    # execute -> spill_if_truncated -> _write_spill path against the live
    # symlink trap. The producer is a portable `python3` program — no bash, no
    # /dev/zero, no MSYS binary-pipe — so the fallback runs it natively and
    # emits the same oversized stream on Linux, macOS AND Windows, keeping this
    # security check live on the CI runners that have no native executor. (The
    # earlier bash `head -c /dev/zero | tr` form measured stdout_bytes=0 under
    # git-bash, which would have made "not escaped" pass vacuously there.)
    _big = 'import sys; sys.stdout.write("A" * 100000)'
    try:
        _res = sessions.execute(_spill_sid, _big, language="python3")
        _e2e_escaped = "stdout_spill" in _res
    except ValueError:
        _e2e_escaped = False
    check("an oversized session run cannot write through a symlinked spill dir",
          not _e2e_escaped
          and [p.name for p in sorted(_victim.glob("*.bin"))] == ["pre-existing.bin"],
          f"-> escaped={_e2e_escaped} "
          f"victim={[p.name for p in sorted(_victim.glob('*.bin'))]}")

    # The legitimate case still works: a real directory, no symlink.
    _ok_sid = sessions.start("bash")["session_id"]
    _ok_d = sessions._session_dir(_ok_sid)
    try:
        _rel = sessions._write_spill(_ok_sid, _ok_d, "stdout", b"Q" * 1024)
        _wrote_inside = (_ok_d / _rel).is_file()
    except ValueError:
        _wrote_inside = False
    check("an ordinary spill still writes inside the workspace", _wrote_inside)

print(f"\n=== {len(FAILS)} FAILURES, {len(SKIPS)} skipped ===" if FAILS else
      f"\n=== ALL SESSION-JAIL TESTS PASS ({len(SKIPS)} skipped) ===")
sys.exit(1 if FAILS else 0)
