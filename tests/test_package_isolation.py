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

# NOTHING under $HOME may be writable by a confined installer. An earlier
# version allowed ~/.npm, ~/.cargo and friends because managers fail without a
# writable cache; the caches are now redirected into the workspace instead, so
# the claim "confined to its workspace" is literally true. This is the guard
# against that allowance creeping back.
if _ll.available():
    _probe_ws = _tf.mkdtemp()
    _env = {k: v.replace("{target}", _probe_ws)
            for k, v in _pkgs._INSTALLERS["node"][2].items()}
    _fn, _reasons_probe = _pkgs._confinement("npm", _probe_ws, _env, "node")
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
