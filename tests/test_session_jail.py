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
            try:
                sessions.write_file(_sid, "planted.txt", "pwned")
                _jail_caught = False
            except ValueError:
                _jail_caught = True
            check("a symlink present before the call is refused by _jail",
                  _jail_caught and not _outside.exists(),
                  f"-> escaped={_outside.exists()}")

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

# A workspace-only session runs a fresh sandboxed process per call, so it
# carries all of it. The empty list is a real answer, not a missing one, and
# asserting it keeps the disclosure from being pasted onto every session shape.
check("a workspace-only session declares no structural gaps",
      sessions._session_unenforced(None) == [])

print(f"\n=== {len(FAILS)} FAILURES, {len(SKIPS)} skipped ===" if FAILS else
      f"\n=== ALL SESSION-JAIL TESTS PASS ({len(SKIPS)} skipped) ===")
sys.exit(1 if FAILS else 0)
