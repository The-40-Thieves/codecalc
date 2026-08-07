"""Runtime self-update for every language codecalc can execute.

Each language is mapped to the package manager that owns its runtime on this
machine (mise / rustup / swiftly / apt / npm / uv / nix-on-demand). Two
operations:

  - status():        NON-MUTATING. Current vs latest per language + the exact
                     commands that WOULD be run to update.
  - update(apply):   apply=False -> same as status (dry run, safe default).
                     apply=True  -> actually executes the update commands.

The Rust executor NEVER runs these — runtime management runs on the host, in
the Python process, explicitly requested. `update_runtimes` in the MCP server
defaults to dry-run for the same reason.
"""

from __future__ import annotations

import json
import re
import subprocess

#: language (registry name) -> package manager that owns its runtime
LANGUAGE_MANAGER: dict[str, str] = {
    # mise-managed runtimes
    "python3": "mise", "node": "mise", "bun": "mise", "deno": "mise",
    "typescript": "mise", "ruby": "mise", "erlang": "mise", "elixir": "mise",
    "gleam": "mise", "go": "mise", "zig": "mise", "java": "mise",
    "kotlin": "mise", "sqlite": "mise", "duckdb": "mise", "gradle": "mise",
    # rust toolchains (mise rust is a symlink into rustup)
    "rust": "rustup",
    # swiftly-managed Swift toolchain
    "swift": "swiftly",
    # apt system runtimes
    "c": "apt", "cpp": "apt", "c++": "apt", "fortran": "apt", "csharp": "apt",
    "php": "apt", "perl": "apt", "lua": "apt", "tcl": "apt", "r": "apt",
    "jq": "apt", "bash": "apt", "zsh": "apt",
    # npm global (TypeScript compiler + friends)
    "tsc": "npm",
    # uv-managed tools
    "mojo": "uv",
    # on-demand via nix-shell; nothing persistent to update
    "haskell": "nix",
}

#: apt package globs to consider for each apt-managed language
APT_GLOBS: dict[str, list[str]] = {
    "c": ["gcc-13", "gcc"], "cpp": ["g++-13", "g++"], "c++": ["g++-13", "g++"],
    "fortran": ["gfortran*"], "csharp": ["dotnet-sdk-10.0", "dotnet-runtime-10.0"],
    "php": ["php8.5-*"], "perl": ["perl"], "lua": ["lua5.4", "lua5.1", "luajit"],
    "tcl": ["tcl8.6"], "r": ["r-base*"], "jq": ["jq"], "bash": ["bash"],
    "zsh": ["zsh"],
}

#: mise tool name -> registry language name
MISE_ALIAS = {
    "python": "python3", "node": "node", "go": "go", "ruby": "ruby",
    "erlang": "erlang", "elixir": "elixir", "gleam": "gleam", "zig": "zig",
    "bun": "bun", "deno": "deno", "java": "java", "kotlin": "kotlin",
    "sqlite": "sqlite", "duckdb": "duckdb", "gradle": "gradle",
}


def _run(cmd: list[str], timeout: int = 60) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception:
        return ""


def _check_mise() -> dict[str, dict]:
    """Merge `mise outdated` (outdated/missing tools) with `mise ls --installed`
    (current versions of everything installed) so every language appears."""
    # installed: "<tool> <version> <config>..."  (current state)
    installed: dict[str, dict] = {}
    for line in _run(["mise", "ls", "--installed"], timeout=60).splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        lang = MISE_ALIAS.get(parts[0])
        if lang is None:
            continue
        installed[lang] = {
            "manager": "mise", "tool": parts[0],
            "current": parts[1].replace("(symlink)", "").strip() or "?",
            "latest": parts[1].replace("(symlink)", "").strip() or "?",
            "updatable": False,
        }

    # outdated: "<tool> <wanted> <current> <latest> <config>" — overlays updates
    for line in _run(["mise", "outdated"], timeout=60).splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] in ("Tool", "Package"):
            continue
        lang = MISE_ALIAS.get(parts[0])
        if lang is None:
            continue
        current, latest = parts[2], parts[3]
        if current == "[MISSING]":
            installed[lang] = {
                "manager": "mise", "tool": parts[0],
                "current": "not installed", "latest": latest, "updatable": True,
            }
        else:
            installed[lang] = {
                "manager": "mise", "tool": parts[0],
                "current": current, "latest": latest,
                "updatable": current != latest,
            }
    return installed


def _check_rustup() -> dict[str, dict]:
    """Parse `rustup check`: '<tc> - up to date: X' or '<tc> - update available: X -> Y'.
    Prefers the stable toolchain; ignores the rustup-binary line."""
    out = _run(["rustup", "check"], timeout=60)
    best: dict | None = None
    for line in out.splitlines():
        m = re.search(r"(\S+)\s*-\s*(?:up to date|update available)\s*:?\s*([^\s,]+)(?:\s*->\s*([^\s,]+))?", line)
        if not m:
            continue
        toolchain, cur, latest = m.group(1), m.group(2), m.group(3) or m.group(2)
        if toolchain == "rustup":
            continue  # rustup-binary self-update line, not a language runtime
        entry = {
            "manager": "rustup", "tool": "rust",
            "current": cur, "latest": latest,
            "updatable": cur != latest,
            "toolchain": toolchain,
        }
        if best is None or "stable" in toolchain:
            best = entry
    return {"rust": best} if best else {}


def _check_swiftly() -> dict[str, dict]:
    current = _run(["swift", "--version"], timeout=30)
    cur = "?"
    for line in current.splitlines():
        m = re.search(r"Swift version (\S+)", line)
        if m:
            cur = m.group(1)
            break
    avail = _run(["swiftly", "list-available"], timeout=60).splitlines()
    versions = []
    for line in avail:
        for m in re.finditer(r"\b(\d+)\.(\d+)(?:\.(\d+))?\b", line):
            versions.append((int(m.group(1)), int(m.group(2)), int(m.group(3) or "0")))
    latest = ".".join(str(x) for x in max(versions)) if versions else "?"
    return {"swift": {
        "manager": "swiftly", "tool": "swift",
        "current": cur, "latest": latest,
        "updatable": cur != "?" and latest != "?" and cur != latest,
    }}


def _check_apt() -> dict[str, dict]:
    out = _run(["apt", "list", "--upgradable"], timeout=60)
    upgradable = set()
    for line in out.splitlines():
        m = re.match(r"(\S+?)/(\S+)\s+(\S+)\s", line)
        if m:
            upgradable.add(m.group(1).split(":")[0])
    found: dict[str, dict] = {}
    for lang, globs in APT_GLOBS.items():
        matched = []
        for g in globs:
            pat = re.compile("^" + g.replace("*", ".*") + "$")
            matched += [p for p in upgradable if pat.match(p)]
        found[lang] = {
            "manager": "apt", "tool": lang,
            "current": "apt-managed",
            "latest": ", ".join(dict.fromkeys(matched)) or "up to date",
            "updatable": bool(matched),
            "packages": list(dict.fromkeys(matched)),
        }
    return found


def _check_npm() -> dict[str, dict]:
    out = _run(["npm", "-g", "outdated", "--json"], timeout=60)
    try:
        data = json.loads(out) or {}
    except Exception:
        data = {}
    if "typescript" in data:
        v = data["typescript"]
        return {"tsc": {
            "manager": "npm", "tool": "typescript",
            "current": v.get("current"), "latest": v.get("latest"),
            "updatable": v.get("current") != v.get("latest"),
        }}
    # typescript absent from outdated = up to date; report its installed version
    cur = _run(["npm", "-g", "ls", "typescript", "--depth=0"], timeout=30)
    m = re.search(r"typescript@(\S+)", cur)
    return {"tsc": {
        "manager": "npm", "tool": "typescript",
        "current": m.group(1) if m else "?",
        "latest": m.group(1) if m else "?",
        "updatable": False,
    }}


def _check_uv() -> dict[str, dict]:
    """mojo lives under /data/tools/uv-tools but `uv tool list` in this shell
    doesn't see it (tool dir configured elsewhere); report its version directly."""
    out = _run(["mojo", "--version"], timeout=30)
    m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", out)
    return {"mojo": {
        "manager": "uv", "tool": "mojo",
        "current": m.group(1) if m else "unknown",
        "latest": "check via `uv tool upgrade mojo`",
        "updatable": None,
    }}


def _check_nix() -> dict[str, dict]:
    return {"haskell": {
        "manager": "nix", "tool": "haskell",
        "current": "on-demand (nix-shell -p ghc)", "latest": "nixpkgs tracks upstream",
        "updatable": False,
    }}


_CHECKERS = {
    "mise": _check_mise, "rustup": _check_rustup, "swiftly": _check_swiftly,
    "apt": _check_apt, "npm": _check_npm, "uv": _check_uv, "nix": _check_nix,
}

#: per-manager update commands (apply=True); "<PKGS>" is substituted with the
#: concrete apt package list at call time — never passed literally.
UPDATE_COMMANDS: dict[str, list[str]] = {
    "mise": ["mise", "up"],
    "rustup": ["rustup", "update"],
    "swiftly": ["swiftly", "update"],
    "apt": ["sudo", "-n", "apt-get", "install", "--only-upgrade", "-y", "<PKGS>"],
    "npm": ["npm", "update", "-g"],
    "uv": ["uv", "tool", "upgrade", "mojo"],
    "nix": [],  # nothing persistent
}


def status(languages: str | list[str] | None = None) -> dict:
    """Non-mutating: current vs latest for each language runtime."""
    want = {l.strip().lower() for l in languages.split(",")} if isinstance(languages, str) else (
        set(languages) if languages else None
    )
    results: dict[str, dict] = {}
    for manager, checker in _CHECKERS.items():
        try:
            found = checker()
        except Exception as exc:
            found = {"__error__": {"manager": manager, "error": str(exc)}}
        for lang, info in found.items():
            if lang == "__error__":
                continue  # checker failure is surfaced, not a language entry
            if want is None or lang in want or info.get("tool") in want:
                results[lang] = info

    for info in results.values():
        mgr = info.get("manager")
        cmd = list(UPDATE_COMMANDS.get(mgr, []))
        if mgr == "apt" and info.get("packages"):
            pkgs = info["packages"]
            cmd = [("<PKGS>" if part == "<PKGS>" else part) for part in cmd]
            cmd = [p for p in cmd if p != "<PKGS>"] + pkgs
        info["update_command"] = " ".join(cmd) if cmd else None

    return {
        "ok": True,
        "dry_run": True,
        "languages": results,
        "summary": {
            "updatable": sum(1 for r in results.values() if r.get("updatable")),
            "total": len(results),
        },
    }


def update(languages: str | list[str] | None = None, apply: bool = False,
           timeout: int = 600) -> dict:
    """apply=False -> dry run (returns status + commands). apply=True -> execute."""
    st = status(languages)
    if not apply:
        st["dry_run"] = True
        st["message"] = "Dry run: nothing was updated. Call with apply=True to execute."
        return st

    by_manager: dict[str, list[str]] = {}
    for lang, info in st["languages"].items():
        mgr = info.get("manager")
        if mgr and mgr != "nix" and info.get("updatable"):
            by_manager.setdefault(mgr, []).append(lang)

    executed = []
    for mgr, langs in by_manager.items():
        cmd = list(UPDATE_COMMANDS.get(mgr, []))
        if not cmd:
            continue
        if mgr == "apt":
            pkgs = []
            for l in langs:
                pkgs += st["languages"][l].get("packages", [])
            pkgs = list(dict.fromkeys(pkgs))
            cmd = [p for p in cmd if p != "<PKGS>"] + pkgs
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            executed.append({
                "manager": mgr, "languages": langs, "command": " ".join(cmd),
                "exit_code": p.returncode,
                "output_tail": (p.stdout + p.stderr)[-1500:],
            })
        except subprocess.TimeoutExpired:
            executed.append({
                "manager": mgr, "languages": langs, "command": " ".join(cmd),
                "exit_code": None, "output_tail": f"timed out after {timeout}s",
            })
        except Exception as exc:
            executed.append({
                "manager": mgr, "languages": langs, "command": " ".join(cmd),
                "exit_code": None, "output_tail": str(exc),
            })

    return {"ok": True, "dry_run": False, "executed": executed}
