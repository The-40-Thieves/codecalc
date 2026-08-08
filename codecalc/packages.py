"""Package installation inside sandbox workspaces.

Installs packages with each language's native package manager into the
session workspace (or a shared per-language cache for ad-hoc runs), so
executed code can import them.

Security: package names/versions are passed as argv (never shell), and only
the language's own package manager is invoked — no arbitrary command
execution from package strings.

THE INSTALL MUST NOT ESCAPE THE WORKSPACE, and this is the part that was
wrong. python3 used `uv pip install --system`, which ignores cwd entirely and
targets the interpreter: on this host that resolved to
/data/tools/mise/installs/python/3.14.6 — the very interpreter the sandbox runs
untrusted code on. One tool call therefore installed third-party code into
every future sandboxed run, for every session, permanently, and survived
session_stop. The docstring claimed workspace scoping the whole time.

`--target <dir>` fixes it and needs no environment plumbing: CPython puts the
script's own directory on sys.path[0], and executed code runs from the session
workdir, so a package installed there is importable. Verified both halves — a
package installed with --target is importable from that directory and is NOT
visible to the host interpreter.

`ruby` and `r` had the same shape (`gem install` -> the mise gem dir,
`install.packages` -> /usr/local/lib/R/site-library). Neither can be made
importable from a workspace without adding GEM_HOME/R_LIBS to the executor's
environment allowlist, and that allowlist is the fix for AUDIT.md CRITICAL-02 —
not something to widen for a convenience feature. They are declined instead,
with the reason, rather than silently mutating the host.

Network: an install needs it. There is no --no-net interaction to enforce
because installs do not run inside the sandbox at all; they are a direct
subprocess from the server. The previous docstring claimed --no-net sessions
could not install, which was never checked anywhere.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import executor, registry

#: language -> (manager binary, argv template with {pkg} placeholder, env hint)
#: {target} is substituted with the workspace directory. Every entry must be
#: workspace-scoped: an installer that writes outside it does not belong here.
_INSTALLERS: dict[str, tuple[str, list[str], dict[str, str]]] = {
    "python3": ("uv", ["uv", "pip", "install", "--target", "{target}", "{pkg}"], {"UV_CACHE_DIR": "~/.cache/uv"}),
    "node": ("npm", ["npm", "install", "{pkg}", "--no-save", "--no-audit", "--no-fund"], {}),
    "bun": ("bun", ["bun", "add", "{pkg}"], {}),
    "deno": ("deno", ["deno", "install", "{pkg}"], {}),
    "php": ("composer", ["composer", "require", "{pkg}"], {}),
    "go": ("go", ["go", "get", "{pkg}"], {}),
    "rust": ("cargo", ["cargo", "add", "{pkg}"], {}),
}

#: languages whose installer is not yet supported (declared so we can tell the
#: caller why, instead of failing cryptically)
_UNSUPPORTED = {"java", "csharp", "kotlin", "swift", "zig", "elixir", "erlang",
                "gleam", "haskell", "fortran", "c", "cpp", "c++", "perl", "lua",
                "tcl", "bash", "zsh", "awk", "jq", "sqlite", "mojo", "typescript",
                # Declined rather than host-mutating — see the module docstring.
                "ruby", "r"}

#: Why a language is declined, when the reason is not simply "no installer".
_DECLINED_REASON = {
    "ruby": "gem installs into the interpreter's global gem directory; scoping it "
            "to a workspace needs GEM_HOME in the executor env allowlist, which is "
            "the CRITICAL-02 fix and is not widened for this",
    "r": "install.packages writes to the global R site-library; scoping it needs "
         "R_LIBS in the executor env allowlist, same reason",
}

#: shared cache dir for ad-hoc (non-session) installs; sessions get their own
CACHE_ROOT = Path("~/.codecalc/pkgs").expanduser()


def install(language: str, package: str, session_id: str | None = None,
            version: str | None = None) -> dict:
    """Install a package for a language. Returns where it was installed."""
    name = registry.canonical(language) or language
    if name in _UNSUPPORTED:
        reason = _DECLINED_REASON.get(name)
        msg = f"package install not supported for '{name}'"
        return {"ok": False, "error": f"{msg}: {reason}" if reason else msg}

    installer = _INSTALLERS.get(name)
    if installer is None:
        return {"ok": False, "error": f"no installer defined for '{name}'"}
    bin_, tmpl, env_hint = installer
    if shutil.which(bin_, path=executor.registry.runtime_path()) is None and \
            shutil.which(bin_) is None:
        return {"ok": False, "error": f"package manager '{bin_}' not found"}

    spec = f"{package}=={version}" if version else package

    # session installs go into the session workspace; ad-hoc into the cache
    if session_id:
        from . import sessions
        d = sessions._session_dir(session_id)
        if not d.is_dir():
            return {"ok": False, "error": f"unknown session '{session_id}'"}
        cwd = d
    else:
        cwd = CACHE_ROOT
        cwd.mkdir(parents=True, exist_ok=True)

    # {target} AFTER cwd is known. Substituted separately from {pkg} so a package
    # name can never masquerade as the target path.
    cmd = [p.replace("{target}", str(cwd)).replace("{pkg}", spec) for p in tmpl]

    env = dict(executor._env())
    env.update(env_hint)
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                           text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "package install timed out (600s)"}
    except Exception as exc:
        return {"ok": False, "error": f"install failed: {exc}"}

    tail = (p.stdout + p.stderr)[-1200:]
    if p.returncode != 0:
        return {"ok": False, "error": f"install failed (rc={p.returncode})",
                "output_tail": tail}
    # Executed code finds a package only when it lands in the directory the
    # program runs from — sys.path[0] for python, node_modules lookup for node.
    # An ad-hoc install goes to the shared cache, which is NOT that directory, so
    # say so instead of reporting a success the caller cannot use.
    importable = session_id is not None
    return {"ok": True, "language": name, "package": spec,
            "target": str(cwd), "importable": importable,
            "note": None if importable else
                    "installed into the shared cache, which executed code does NOT "
                    "have on its import path — pass session_id to install somewhere "
                    "your code can import from",
            "output_tail": tail[-600:]}
