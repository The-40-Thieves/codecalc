"""install_package must not mutate the host toolchain.

The bug: python3 installs used `uv pip install --system`, which ignores cwd and
targets the interpreter — on a mise-managed host, the toolchain-managed
interpreter, which is the same one the sandbox runs untrusted code on. One tool
call
installed third-party code into every future sandboxed run, for every session,
permanently, and it survived session_stop. The module docstring and AUDIT.md
both claimed the install went into the session workspace.

`ruby` and `r` had the same shape (`gem install` -> the global gem directory,
`install.packages` -> the global R site-library) and are now declined,
because scoping either one needs GEM_HOME/R_LIBS in the executor's environment
allowlist — and that allowlist is the CRITICAL-02 fix, not something to widen
for a convenience feature.

These assertions are STATIC: they read the installer table rather than running
a real install. A test that actually installed something would need the network
and would, if it regressed, do the very damage it is checking for.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import packages

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── no installer may write outside the workspace ────────────────────────────
#: Flags that make a package manager write to a shared/global location instead
#: of the directory it was pointed at. Any of these in a template is the bug.
GLOBAL_FLAGS = {"--system", "--global", "-g", "--user"}

for lang, (_binary, template, _env) in packages._INSTALLERS.items():
    offending = sorted(set(template) & GLOBAL_FLAGS)
    check(f"{lang}: no global-install flag", not offending, f"-> {offending}")

# ── every installer must be told WHERE to install ───────────────────────────
# Either explicitly via {target}, or implicitly by being cwd-scoped. The ones
# that are cwd-scoped are listed so adding a new installer forces a decision
# rather than defaulting into the old bug.
CWD_SCOPED = {"node", "bun", "deno", "php", "go", "rust"}
for lang, (_b, template, _e) in packages._INSTALLERS.items():
    scoped = "{target}" in " ".join(template) or lang in CWD_SCOPED
    check(f"{lang}: install is workspace-scoped", scoped, f"-> {template[:4]}")

# ── the specific regression ─────────────────────────────────────────────────
py = packages._INSTALLERS.get("python3")
check("python3 installer exists", py is not None)
if py:
    check("python3 uses --target, not --system",
          "--target" in py[1] and "--system" not in py[1], f"-> {py[1]}")

# ── the two declined languages stay declined, WITH a reason ────────────────
for lang in ("ruby", "r"):
    check(f"{lang} is declined", lang in packages._UNSUPPORTED)
    check(f"{lang} says why", bool(packages._DECLINED_REASON.get(lang)),
          f"-> {(packages._DECLINED_REASON.get(lang) or '')[:60]}")
    r = packages.install(lang, "anything")
    check(f"{lang} install returns a reasoned refusal",
          r.get("ok") is False and len(r.get("error", "")) > 40,
          f"-> {r.get('error', '')[:70]}")

# ── an ad-hoc install must not claim to be importable ───────────────────────
# The shared cache is not on any executed program's import path. Reporting a
# plain success there is the "failure encoded as a valid result" shape.
import inspect

src = inspect.getsource(packages.install)
check("install() reports an `importable` flag", "importable" in src)
check("ad-hoc installs are flagged non-importable",
      "session_id is not None" in src)

# ── the env allowlist was NOT widened to make any of this work ─────────────
from codecalc import executor

for var in ("GEM_HOME", "GEM_PATH", "R_LIBS", "PYTHONPATH"):
    check(f"executor env allowlist still excludes {var}",
          var not in executor._ENV_ALLOWLIST)

# ── #23: the install is CONFINED, and the flags stop the code running ─────
# Two layers, tested separately because they fail differently.
#
# Layer 1 is the cheap one: do not execute third-party code at install time.
# npm shipped this as its own default in v12 after a year of supply-chain
# attacks through postinstall hooks; the npm on a given host may be older, so
# the flag is passed rather than assumed.
import os as _os
import subprocess as _sp
import sys as _sys
import sysconfig as _sysconfig
import tempfile as _tf

from codecalc import landlock as _ll
from codecalc import packages as _pkgs

_FLAGGED = {
    "python3": "--only-binary=:all:",
    "node": "--ignore-scripts",
    "bun": "--ignore-scripts",
    "php": "--no-scripts",
}
for _lang, _flag in _FLAGGED.items():
    _entry = _pkgs._INSTALLERS.get(_lang)
    check(f"{_lang} installer refuses to run package code ({_flag})",
          _entry is not None and _flag in _entry[1],
          f"-> {_entry[1] if _entry else 'MISSING'}")

# npm walks UP from cwd for a package root. Without --prefix it found none in
# the workspace and asked to mkdir $HOME/node_modules/<pkg> — outside
# the workspace this table promises to stay inside.
check("node install is pinned to the workspace with --prefix",
      "--prefix" in _pkgs._INSTALLERS["node"][1],
      f"-> {_pkgs._INSTALLERS['node'][1]}")

# Layer 2: what still runs is confined. Tested against the real kernel
# interface rather than a mock — a mocked sandbox proves nothing about whether
# the kernel would have stopped anything.
if _ll.available():
    check("Landlock reports an ABI version", _ll.abi_version() >= 1,
          f"-> ABI {_ll.abi_version()}")

    _ws = _tf.mkdtemp()
    _canary_path = pathlib.Path(_tf.mkdtemp()) / "secret.canary"
    _canary_path.write_text("SECRET-CANARY")
    # Deliberately OUTSIDE the workspace — that is what is being tested. Built
    # from gettempdir() rather than a literal so this is not a hardcoded /tmp
    # path (ruff S108, and check_portability would object too).
    _outside = str(pathlib.Path(_tf.gettempdir()) / "codecalc-escape-probe")
    _probe = f'''
import os
def t(fn):
    try:
        fn(); return "ALLOWED"
    except Exception as e:
        return type(e).__name__
_outside = {_outside!r}\nprint(t(lambda: open({_ws!r}+"/in.txt","w").write("ok")),
      t(lambda: open(_outside, "w").write("x")),
      t(lambda: open({str(_canary_path)!r}).read()))
'''
    _RO = ["/usr", "/lib", "/lib64", "/bin", "/etc", "/proc", "/run",
           _sys.prefix, _sys.base_prefix, _sysconfig.get_paths()["purelib"],
           "/data/tools"]

    def _confine():
        _ll.restrict_self(read_write=[_ws], read_only=_RO)

    try:
        _free = _sp.run([_sys.executable, "-c", _probe], capture_output=True,
                        text=True, timeout=60).stdout.split()
        _held = _sp.run([_sys.executable, "-c", _probe], capture_output=True,
                        text=True, timeout=60, preexec_fn=_confine).stdout.split()
        # The control matters: if the unconfined run cannot write either, the
        # confined result proves nothing about Landlock.
        check("control: unconfined, all three succeed",
              _free == ["ALLOWED", "ALLOWED", "ALLOWED"], f"-> {_free}")
        check("confined: the workspace stays writable",
              len(_held) == 3 and _held[0] == "ALLOWED", f"-> {_held}")
        check("confined: a write OUTSIDE the workspace is refused",
              len(_held) == 3 and _held[1] == "PermissionError", f"-> {_held}")
        check("confined: a same-UID canary outside it is unreadable",
              len(_held) == 3 and _held[2] == "PermissionError", f"-> {_held}")
    finally:
        _canary_path.unlink(missing_ok=True)
        _canary_path.parent.rmdir()
        for _f in pathlib.Path(_ws).glob("*"):
            _f.unlink()
        pathlib.Path(_ws).rmdir()
        pathlib.Path(_outside).unlink(missing_ok=True)
else:
    print(f"SKIP Landlock confinement probes — unavailable on {_sys.platform}")

# THE-819: the macOS counterpart, via sandbox-exec (Seatbelt) rather than
# Landlock. Same shape as the Landlock probe above, on purpose — a write
# outside the workspace refused, a same-UID canary outside it unreadable —
# and gated to run its POSITIVE assertions only on darwin, where the real
# kernel/Seatbelt interaction can happen. `ci-python.yml`'s `tests` job runs
# on `macos-latest`, so this is where the mechanism actually gets measured;
# everywhere else it SKIPS with a recorded reason rather than silently
# passing, per THE-819's own instruction not to fake a pass on Linux.
from codecalc import sandbox_macos as _sm

if _sys.platform == "darwin" and _sm.available():
    _ws_mac = _tf.mkdtemp()
    _canary_mac_dir = pathlib.Path(_tf.mkdtemp())
    _canary_mac = _canary_mac_dir / "secret.canary"
    _canary_mac.write_text("SECRET-CANARY")
    _outside_mac = str(pathlib.Path(_tf.gettempdir()) / "codecalc-escape-probe-macos")
    _probe_mac = f'''
import os
def t(fn):
    try:
        fn(); return "ALLOWED"
    except Exception as e:
        return type(e).__name__
_outside = {_outside_mac!r}
print(t(lambda: open({_ws_mac!r}+"/in.txt","w").write("ok")),
      t(lambda: open(_outside, "w").write("x")),
      t(lambda: open({str(_canary_mac)!r}).read()))
'''
    _RO_mac = ["/usr", "/bin", "/sbin", "/System", "/Library", "/private/etc",
               "/private/var", "/dev", "/opt", _sys.prefix, _sys.base_prefix,
               _sysconfig.get_paths()["purelib"]]
    _profile_path_mac = pathlib.Path(_ws_mac) / "probe.sb"
    _profile_path_mac.write_text(
        _sm.build_profile(read_write=[_ws_mac], read_only=_RO_mac), encoding="utf-8")

    try:
        _free_mac = _sp.run([_sys.executable, "-c", _probe_mac], capture_output=True,
                             text=True, timeout=60).stdout.split()
        _held_mac = _sp.run(_sm.command_prefix(str(_profile_path_mac)) +
                             [_sys.executable, "-c", _probe_mac],
                             capture_output=True, text=True, timeout=60).stdout.split()
        # The control matters: if the unconfined run cannot write either, the
        # confined result proves nothing about sandbox-exec.
        check("control: unconfined, all three succeed (macOS)",
              _free_mac == ["ALLOWED", "ALLOWED", "ALLOWED"], f"-> {_free_mac}")
        check("confined (sandbox-exec): the workspace stays writable",
              len(_held_mac) == 3 and _held_mac[0] == "ALLOWED", f"-> {_held_mac}")
        check("confined (sandbox-exec): a write OUTSIDE the workspace is refused",
              len(_held_mac) == 3 and _held_mac[1] == "PermissionError", f"-> {_held_mac}")
        check("confined (sandbox-exec): a same-UID canary outside it is unreadable",
              len(_held_mac) == 3 and _held_mac[2] == "PermissionError", f"-> {_held_mac}")
    finally:
        _canary_mac.unlink(missing_ok=True)
        _canary_mac_dir.rmdir()
        for _f in pathlib.Path(_ws_mac).glob("*"):
            _f.unlink()
        pathlib.Path(_ws_mac).rmdir()
        pathlib.Path(_outside_mac).unlink(missing_ok=True)
elif _sys.platform == "darwin":
    print("SKIP sandbox-exec confinement probes — sandbox-exec not found on this macOS host")
else:
    print(f"SKIP sandbox-exec confinement probes — not darwin ({_sys.platform})")

# The disclosure vocabulary itself needs no macOS host to check — pure string
# logic, same as the Landlock "truthful reporting" checks near the bottom of
# this file.
check("sandbox-exec unavailable is disclosed by its own token",
      "package_install_not_confined_no_sandbox_exec" in _sm.unenforced_reasons(False),
      f"-> {_sm.unenforced_reasons(False)}")
_sm_applied = _sm.unenforced_reasons(True)
check("sandbox-exec applied still discloses the network + metadata gaps",
      {"install_metadata_syscalls_unrestricted", "install_tcp_egress_unrestricted",
       "install_udp_egress_unrestricted"} <= set(_sm_applied), f"-> {_sm_applied}")
check("  ...and does NOT repeat the 'not confined' token once applied",
      "package_install_not_confined_no_sandbox_exec" not in _sm_applied,
      f"-> {_sm_applied}")

# NOTHING under $HOME may be writable by a confined installer. An earlier
# version allowed ~/.npm, ~/.cargo and friends because managers fail without a
# writable cache; the caches are now redirected into the workspace instead, so
# the claim "confined to its workspace" is literally true. This is the guard
# against that allowance creeping back.
if _ll.available():
    _probe_ws = _tf.mkdtemp()
    _env = {k: v.replace("{target}", _probe_ws)
            for k, v in _pkgs._INSTALLERS["node"][2].items()}
    _prefix, _fn, _reasons_probe = _pkgs._confinement("npm", _probe_ws, _env, "node")
    import inspect as _inspect
    _writable = _inspect.getclosurevars(_fn).nonlocals.get("writable", []) if _fn else []
    _home = str(pathlib.Path("~").expanduser())
    _leaks = [w for w in _writable
              if w.startswith(_home) and not w.startswith(_probe_ws)]
    check("no path under $HOME is writable by a confined installer",
          not _leaks, f"-> {_leaks}")
    check("  ...and the workspace itself is",
          any(w.startswith(_probe_ws) for w in _writable), f"-> {_writable}")
    for _f in sorted(pathlib.Path(_probe_ws).rglob("*"), reverse=True):
        _f.rmdir() if _f.is_dir() else _f.unlink()
    pathlib.Path(_probe_ws).rmdir()

# Every installer in the table is confined, python3 included. It was NOT for a
# while — uv failed under the ruleset and the honest answer at the time was to
# run it unconfined and report `install_not_confined_python3`. Both causes were
# mine (#90): /dev/null was not granted, and restrict_self applied
# directory-only rights to file paths, which made the whole ruleset fail to
# apply the moment a device node was added.
#
# Asserted as a SET rather than per-manager: a manager added later is confined
# by default or this fails, which is the right way round for a security list.
_declared = set(_pkgs._INSTALLERS)
_confinable = set(_pkgs._CONFINABLE)
check("every declared installer is confined",
      _declared <= _confinable, f"-> unconfined: {sorted(_declared - _confinable)}")

# THE-819: same claim, same warrant, for the macOS mechanism.
_confinable_darwin = set(_pkgs._CONFINABLE_DARWIN)
check("every declared installer is confined on macOS too",
      _declared <= _confinable_darwin,
      f"-> unconfined on macOS: {sorted(_declared - _confinable_darwin)}")

if _sys.platform == "darwin" and _sm.available():
    _ws3 = _tf.mkdtemp()
    _env3 = {k: v.replace("{target}", _ws3)
             for k, v in _pkgs._INSTALLERS["python3"][2].items()}
    _prefix3, _fn3, _reasons3 = _pkgs._confinement("uv", _ws3, _env3, "python3")
    check("  ...including python3, which gets a sandbox-exec profile",
          bool(_prefix3) and _prefix3[0] == "sandbox-exec" and _fn3 is None,
          f"-> {_prefix3}")
    check("  ...and the applied result does NOT claim 'not confined'",
          "package_install_not_confined_no_sandbox_exec" not in _reasons3,
          f"-> {_reasons3}")
    for _f in sorted(pathlib.Path(_ws3).rglob("*"), reverse=True):
        _f.rmdir() if _f.is_dir() else _f.unlink()
    pathlib.Path(_ws3).rmdir()

if _ll.available():
    _ws2 = _tf.mkdtemp()
    _env2 = {k: v.replace("{target}", _ws2)
             for k, v in _pkgs._INSTALLERS["python3"][2].items()}
    _, _fn2, _ = _pkgs._confinement("uv", _ws2, _env2, "python3")
    check("  ...including python3, which now gets a ruleset",
          _fn2 is not None, f"-> {_fn2}")
    for _f in sorted(pathlib.Path(_ws2).rglob("*"), reverse=True):
        _f.rmdir() if _f.is_dir() else _f.unlink()
    pathlib.Path(_ws2).rmdir()

# THE-819 Deliverable C: the Windows OFF-by-default flag is a documented
# NO-OP. There is no real Windows box here, so this is tested the way
# tests/test_platform_contract.py tests Windows-only Rust source from
# Linux — by exercising the LOGIC directly rather than needing the platform.
#
# `sys.platform`, `executor.IS_WINDOWS` AND `landlock.abi_version` are all
# monkeypatched, and through the real `_confinement()` dispatcher rather than
# a piece of it in isolation:
#   - `_confinement` checks `sys.platform == "darwin"` FIRST, so on an actual
#     macOS runner calling it with only `IS_WINDOWS` patched would silently
#     take the macOS branch instead and this block would assert nothing
#     about Windows at all.
#   - `landlock.abi_version()` decides Landlock availability from
#     `os.uname().sysname`, NOT from `sys.platform` — so on a Linux box
#     running this suite (true here), patching only `sys.platform` would
#     leave Landlock genuinely available and this block would exercise a
#     REAL Landlock ruleset instead of the "no Landlock on Windows" path it
#     means to test.
# Both are the same failure shape this suite's own docstring warns about: a
# check() that never reaches the code it claims to test. All three are
# restored in `finally`.
_real_platform = _sys.platform
_real_is_windows = _pkgs.executor.IS_WINDOWS
_real_abi_version = _ll.abi_version
try:
    _sys.platform = "win32"
    _pkgs.executor.IS_WINDOWS = True
    _ll.abi_version = lambda *_a, **_k: 0
    _ws4 = _tf.mkdtemp()
    _, _, _reasons_win_off = _pkgs._confinement("npm", _ws4, {}, "node")
    check("WIN_INSTALL_CONFINE_ENV unset: no extra disclosure token",
          "package_install_confinement_unverified_on_windows" not in _reasons_win_off,
          f"-> {_reasons_win_off}")
    check("  ...and the base 'unconfined on Windows' disclosure still fires",
          "package_install_not_confined_no_landlock" in _reasons_win_off,
          f"-> {_reasons_win_off}")

    # Empty-but-set counts as set — same convention as CODECALC_PACKAGE_ALLOWLIST
    # (see _allowlist()'s comment) and CODECALC_ALLOW_RUNTIME_APPLY.
    _os.environ[_pkgs.WIN_INSTALL_CONFINE_ENV] = ""
    try:
        _, _, _reasons_win_on = _pkgs._confinement("npm", _ws4, {}, "node")
    finally:
        del _os.environ[_pkgs.WIN_INSTALL_CONFINE_ENV]
    check("WIN_INSTALL_CONFINE_ENV set (even empty): the unverified token fires",
          "package_install_confinement_unverified_on_windows" in _reasons_win_on,
          f"-> {_reasons_win_on}")
    check("  ...WITHOUT claiming enforcement — the base disclosure fires too",
          "package_install_not_confined_no_landlock" in _reasons_win_on,
          f"-> {_reasons_win_on}")
    check("  ...so the flag can only ADD a disclosure, never remove one",
          len(_reasons_win_on) > len(_reasons_win_off), f"-> {_reasons_win_on}")
finally:
    _sys.platform = _real_platform
    _pkgs.executor.IS_WINDOWS = _real_is_windows
    _ll.abi_version = _real_abi_version
    pathlib.Path(_ws4).rmdir()

# Truthful reporting is its own requirement: the gaps are real and stated.
_reasons = _ll.unenforced_reasons()
check("the confinement reports what it does NOT cover",
      bool(_reasons), f"-> {_reasons}")
# Two different truths, and the first version conflated them. Where Landlock
# is unavailable at all (ABI 0 — macOS, Windows) there is no confinement to
# have gaps IN, and demanding a UDP caveat there failed the platforms that were
# behaving correctly. Where it IS applied, the UDP gap is real below ABI 10 and
# must be named. Asserted as the two cases they are.
if _ll.abi_version() == 0:
    check("  ...saying plainly that nothing was confined",
          "package_install_not_confined_no_landlock" in _reasons,
          f"-> {_reasons}")
else:
    check("  ...naming UDP where the ABI cannot restrict it",
          _ll.abi_version() >= 10 or "install_udp_egress_unrestricted" in _reasons,
          f"-> ABI {_ll.abi_version()} reasons={_reasons}")

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== PACKAGE INSTALLS ARE WORKSPACE-SCOPED ===")
sys.exit(1 if FAILS else 0)
