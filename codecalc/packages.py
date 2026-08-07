"""Package installation inside sandbox workspaces.

Installs packages with each language's native package manager into the
session workspace (or a shared per-language cache for ad-hoc runs), so
executed code can import them. Network is required; --no-net sessions
cannot install.

Security: package names/versions are passed as argv (never shell), and
only the language's own package manager is invoked — no arbitrary command
execution from package strings.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import executor, registry

#: language -> (manager binary, argv template with {pkg} placeholder, env hint)
_INSTALLERS: dict[str, tuple[str, list[str], dict[str, str]]] = {
    "python3": ("uv", ["uv", "pip", "install", "--system", "{pkg}"], {"UV_CACHE_DIR": "~/.cache/uv"}),
    "node": ("npm", ["npm", "install", "{pkg}", "--no-save", "--no-audit", "--no-fund"], {}),
    "bun": ("bun", ["bun", "add", "{pkg}"], {}),
    "deno": ("deno", ["deno", "install", "{pkg}"], {}),
    "ruby": ("gem", ["gem", "install", "{pkg}", "--no-document"], {}),
    "php": ("composer", ["composer", "require", "{pkg}"], {}),
    "go": ("go", ["go", "get", "{pkg}"], {}),
    "rust": ("cargo", ["cargo", "add", "{pkg}"], {}),
    "r": ("R", ["R", "-e", "install.packages('{pkg}', repos='https://cloud.r-project.org')"], {}),
    "java": ("", [], {}),  # Maven/Gradle projects only; skip
    "csharp": ("", [], {}),
}

#: languages whose installer is not yet supported (declared so we can tell the
#: caller why, instead of failing cryptically)
_UNSUPPORTED = {"java", "csharp", "kotlin", "swift", "zig", "elixir", "erlang",
                "gleam", "haskell", "fortran", "c", "cpp", "c++", "perl", "lua",
                "tcl", "bash", "zsh", "awk", "jq", "sqlite", "mojo", "typescript"}

#: shared cache dir for ad-hoc (non-session) installs; sessions get their own
CACHE_ROOT = Path("~/.codecalc/pkgs").expanduser()


def install(language: str, package: str, session_id: str | None = None,
            version: str | None = None) -> dict:
    """Install a package for a language. Returns where it was installed."""
    name = registry.canonical(language) or language
    if name in _UNSUPPORTED:
        return {"ok": False, "error": f"package install not supported for '{name}'"}

    installer = _INSTALLERS.get(name)
    if installer is None:
        return {"ok": False, "error": f"no installer defined for '{name}'"}
    bin_, tmpl, env_hint = installer
    if shutil.which(bin_, path=executor.registry.runtime_path()) is None and \
            shutil.which(bin_) is None:
        return {"ok": False, "error": f"package manager '{bin_}' not found"}

    spec = f"{package}=={version}" if version else package
    cmd = [p.replace("{pkg}", spec) for p in tmpl]

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
    return {"ok": True, "language": name, "package": spec,
            "target": str(cwd), "output_tail": tail[-600:]}
