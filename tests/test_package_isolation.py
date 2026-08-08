"""install_package must not mutate the host toolchain.

The bug: python3 installs used `uv pip install --system`, which ignores cwd and
targets the interpreter. On the dev host that resolved to

    /data/tools/mise/installs/python/3.14.6

— the same interpreter the sandbox runs untrusted code on. So one tool call
installed third-party code into every future sandboxed run, for every session,
permanently, and it survived session_stop. The module docstring and AUDIT.md
both claimed the install went into the session workspace.

`ruby` and `r` had the same shape (`gem install` -> the mise gem dir,
`install.packages` -> /usr/local/lib/R/site-library) and are now declined,
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

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== PACKAGE INSTALLS ARE WORKSPACE-SCOPED ===")
sys.exit(1 if FAILS else 0)
