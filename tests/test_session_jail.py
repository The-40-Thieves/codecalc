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

print(f"\n=== {len(FAILS)} FAILURES, {len(SKIPS)} skipped ===" if FAILS else
      f"\n=== ALL SESSION-JAIL TESTS PASS ({len(SKIPS)} skipped) ===")
sys.exit(1 if FAILS else 0)
