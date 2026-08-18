"""Deny-by-default operator allowlist for package installs (THE-791 residual).

`install_package` already refused to write outside the workspace (#23) and
refused argv injection via a leading `-` in the package name. What it never
had was a way for an OPERATOR to say "only these packages may be installed at
all" — every syntactically valid name for a supported ecosystem was
installable by anyone who could call the tool.

`CODECALC_PACKAGE_ALLOWLIST` closes that. UNSET is unchanged behaviour
(assert 1). SET makes it deny-by-default: a package not on the list is
refused before `packages.subprocess.run` is ever called — assert that
directly, with a stubbed `subprocess.run` that records whether it fired,
rather than trusting the return value alone (a `code` on the result would
prove nothing if the subprocess had ALREADY spawned by the time it fires).

Two more properties this file exists to hold, both named in the ticket:

  - "a package allowlist that ignores --index-url is a fence with a gate" —
    a package/version string shaped like a flag (embedded ` --something`)
    must be refused whether or not the allowlist is even configured, because
    argv-list semantics already make it inert but a name that LOOKS like a
    flag injection is still the wrong shape to accept silently.
  - the denial carries a STABLE code (errors.PERMISSION_DENIED), not just a
    prose string ensure_code() would have to guess back out of later.
"""

from __future__ import annotations

import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import errors, packages

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


class _FakeCompleted:
    def __init__(self):
        self.returncode = 0
        self.stdout = "Successfully installed\n"
        self.stderr = ""


def _stub_run(calls):
    def _run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()
    return _run


class _env:
    """Set (or clear) CODECALC_PACKAGE_ALLOWLIST for the duration of a `with`."""

    def __init__(self, value: str | None):
        self.value = value

    def __enter__(self):
        self.old = os.environ.get(packages.ALLOWLIST_ENV)
        if self.value is None:
            os.environ.pop(packages.ALLOWLIST_ENV, None)
        else:
            os.environ[packages.ALLOWLIST_ENV] = self.value
        return self

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop(packages.ALLOWLIST_ENV, None)
        else:
            os.environ[packages.ALLOWLIST_ENV] = self.old


# `uv` must actually resolve on PATH for these to reach the allowlist gate
# rather than failing earlier with "package manager not found" — probed, not
# assumed, same reasoning test_python_sweep.py uses for worker capability.
import shutil as _shutil

if _shutil.which("uv") is None:
    print("SKIP package allowlist suite (uv not on PATH)")
    sys.exit(0)

_orig_run = packages.subprocess.run

# ── 1. UNSET = today's behaviour: nothing new is refused ───────────────────
with _env(None):
    calls: list = []
    packages.subprocess.run = _stub_run(calls)
    try:
        r = packages.install("python3", "anything-goes")
    finally:
        packages.subprocess.run = _orig_run
    check("unset allowlist: install proceeds (backward compatible)",
          r.get("ok") is True, f"-> {r}")
    check("unset allowlist: subprocess was actually invoked",
          len(calls) == 1, f"-> {calls}")

# ── 2. SET, package NOT listed: denied before any subprocess work ──────────
with _env("requests,lodash"):
    calls = []
    packages.subprocess.run = _stub_run(calls)
    try:
        r = packages.install("python3", "not-on-the-list")
    finally:
        packages.subprocess.run = _orig_run
    check("configured allowlist: unlisted package is refused",
          r.get("ok") is False, f"-> {r}")
    check("configured allowlist: denial is PRE-side-effect (no subprocess spawned)",
          len(calls) == 0, f"-> {calls}")
    check("configured allowlist: denial carries the stable PERMISSION_DENIED code",
          r.get("code") == errors.PERMISSION_DENIED, f"-> {r.get('code')}")
    check("configured allowlist: denial is not code_inferred (attached at the raise site)",
          r.get("code_inferred") is not True, f"-> {r}")
    check("configured allowlist: denial names the allowlist env var",
          packages.ALLOWLIST_ENV in r.get("error", ""), f"-> {r.get('error')}")

# ── 3. SET, package IS listed: allowed, subprocess runs ────────────────────
with _env("requests,lodash"):
    calls = []
    packages.subprocess.run = _stub_run(calls)
    try:
        r = packages.install("python3", "requests")
    finally:
        packages.subprocess.run = _orig_run
    check("configured allowlist: a listed package installs",
          r.get("ok") is True, f"-> {r}")
    check("configured allowlist: and the subprocess actually ran",
          len(calls) == 1, f"-> {calls}")

# ── 4. bare-name matching ignores extras/version-pin syntax ────────────────
with _env("requests"):
    calls = []
    packages.subprocess.run = _stub_run(calls)
    try:
        r = packages.install("python3", "requests[security]==2.31.0")
    finally:
        packages.subprocess.run = _orig_run
    check("allowlist matches the BARE name, ignoring [extras] and ==version",
          r.get("ok") is True, f"-> {r}")

# ── 5. per-ecosystem scoping: `language:name` restricts to one ecosystem ───
with _env("node:lodash"):
    calls = []
    packages.subprocess.run = _stub_run(calls)
    try:
        r_wrong_eco = packages.install("python3", "lodash")
    finally:
        packages.subprocess.run = _orig_run
    check("ecosystem-scoped entry does NOT allow the same name in another ecosystem",
          r_wrong_eco.get("ok") is False, f"-> {r_wrong_eco}")

    calls = []
    packages.subprocess.run = _stub_run(calls)
    try:
        r_right_eco = packages.install("node", "lodash")
    finally:
        packages.subprocess.run = _orig_run
    check("ecosystem-scoped entry allows the matching ecosystem",
          r_right_eco.get("ok") is True, f"-> {r_right_eco}")

# ── 6. npm scoped package name (`@scope/name`) survives bare-name stripping ─
with _env("@babel/core"):
    calls = []
    packages.subprocess.run = _stub_run(calls)
    try:
        r = packages.install("node", "@babel/core")
    finally:
        packages.subprocess.run = _orig_run
    check("npm scoped package name matches the allowlist verbatim",
          r.get("ok") is True, f"-> {r}")

# ── 7. embedded flag-shaped tokens are refused regardless of the allowlist ──
# Not exploitable via subprocess's argv-list semantics (no shell re-parses the
# string), but "a package allowlist that ignores --index-url is a fence with
# a gate" — refuse the SHAPE outright rather than relying on that mechanics
# detail to keep holding.
with _env(None):
    calls = []
    packages.subprocess.run = _stub_run(calls)
    try:
        r = packages.install("python3", "requests --index-url=http://evil.example/simple")
    finally:
        packages.subprocess.run = _orig_run
    check("a package name with an embedded flag-shaped token is refused",
          r.get("ok") is False, f"-> {r}")
    check("  ...and no subprocess was spawned for it",
          len(calls) == 0, f"-> {calls}")

    calls = []
    packages.subprocess.run = _stub_run(calls)
    try:
        r = packages.install("python3", "requests", version="1.0 --index-url=http://evil.example")
    finally:
        packages.subprocess.run = _orig_run
    check("a VERSION with an embedded flag-shaped token is refused too",
          r.get("ok") is False, f"-> {r}")
    check("  ...and no subprocess was spawned for it",
          len(calls) == 0, f"-> {calls}")

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== PACKAGE ALLOWLIST IS DENY-BY-DEFAULT WHEN CONFIGURED ===")
sys.exit(1 if FAILS else 0)
