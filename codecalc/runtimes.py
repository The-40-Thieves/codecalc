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
import os
import re
import subprocess
from dataclasses import dataclass

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


@dataclass
class _RunResult:
    """Structured outcome of one subprocess call, in place of the bare string
    `_run` used to return.

    `_run` collapsed a missing executable, a timeout, and any other spawn
    failure to the SAME empty string a perfectly healthy tool returns when it
    genuinely has nothing to report — a checker parsing "" could not tell
    "mise is not installed" from "mise ran and found nothing outdated", and
    several read the former as the latter, which is how `update_runtimes`
    reported a broken package manager as up to date.

    `spawned` is deliberately not "succeeded": several of the commands this
    module runs use a nonzero exit code as SIGNAL, not failure — `npm
    outdated` exits 1 precisely when it found something outdated. Only the
    caller knows whether a given command's exit code means anything, so `_run`
    reports whether the process ran at all and leaves exit-code interpretation
    to each checker.
    """
    spawned: bool
    output: str
    exit_code: int | None
    error: str | None = None


def _run(cmd: list[str], timeout: int = 60) -> _RunResult:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return _RunResult(True, (p.stdout or "") + (p.stderr or ""), p.returncode)
    except FileNotFoundError as exc:
        return _RunResult(False, "", None, f"{cmd[0]}: not found ({exc})")
    except subprocess.TimeoutExpired:
        return _RunResult(False, "", None, f"{cmd[0]}: timed out after {timeout}s")
    except OSError as exc:
        return _RunResult(False, "", None, f"{cmd[0]}: {exc}")


def _error(manager: str, msg: str) -> dict[str, dict]:
    """A checker's tool could not even be run. Distinct from a checker that
    ran fine and genuinely found nothing: returning {} for both is what let a
    broken package manager read as "up to date" downstream."""
    return {"__error__": {"manager": manager, "error": msg}}


def _check_mise() -> dict[str, dict]:
    """Merge `mise outdated` (outdated/missing tools) with `mise ls --installed`
    (current versions of everything installed) so every language appears."""
    installed_r = _run(["mise", "ls", "--installed"], timeout=60)
    outdated_r = _run(["mise", "outdated"], timeout=60)
    if not installed_r.spawned and not outdated_r.spawned:
        return _error("mise", installed_r.error or outdated_r.error or "mise unavailable")

    # installed: "<tool> <version> <config>..."  (current state)
    installed: dict[str, dict] = {}
    for line in installed_r.output.splitlines():
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
    for line in outdated_r.output.splitlines():
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
    r = _run(["rustup", "check"], timeout=60)
    if not r.spawned:
        return _error("rustup", r.error)
    best: dict | None = None
    for line in r.output.splitlines():
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
    ver_r = _run(["swift", "--version"], timeout=30)
    avail_r = _run(["swiftly", "list-available"], timeout=60)
    if not ver_r.spawned and not avail_r.spawned:
        # Neither half ran: no information at all, not "up to date". A partial
        # gap (one of the two spawned) falls through to the existing "?"
        # handling below, which was already honest about not knowing.
        return _error("swiftly", ver_r.error or avail_r.error)
    cur = "?"
    for line in ver_r.output.splitlines():
        m = re.search(r"Swift version (\S+)", line)
        if m:
            cur = m.group(1)
            break
    versions = []
    for line in avail_r.output.splitlines():
        for m in re.finditer(r"\b(\d+)\.(\d+)(?:\.(\d+))?\b", line):
            versions.append((int(m.group(1)), int(m.group(2)), int(m.group(3) or "0")))
    latest = ".".join(str(x) for x in max(versions)) if versions else "?"
    return {"swift": {
        "manager": "swiftly", "tool": "swift",
        "current": cur, "latest": latest,
        "updatable": cur != "?" and latest != "?" and cur != latest,
    }}


def _check_apt() -> dict[str, dict]:
    r = _run(["apt", "list", "--upgradable"], timeout=60)
    if not r.spawned:
        return _error("apt", r.error)
    upgradable = set()
    for line in r.output.splitlines():
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
    outdated_r = _run(["npm", "-g", "outdated", "--json"], timeout=60)
    if not outdated_r.spawned:
        return _error("npm", outdated_r.error)
    # `npm outdated` exits 1 precisely when it found something outdated —
    # that is signal, not failure, so only `spawned` (checked above) gates
    # this, never the exit code.
    try:
        data = json.loads(outdated_r.output) or {}
    except Exception:
        data = {}
    if "typescript" in data:
        v = data["typescript"]
        return {"tsc": {
            "manager": "npm", "tool": "typescript",
            "current": v.get("current"), "latest": v.get("latest"),
            "updatable": v.get("current") != v.get("latest"),
        }}
    # typescript absent from `outdated` genuinely means up to date, but only
    # once we know npm itself ran — confirmed above.
    cur_r = _run(["npm", "-g", "ls", "typescript", "--depth=0"], timeout=30)
    if not cur_r.spawned:
        return _error("npm", cur_r.error)
    m = re.search(r"typescript@(\S+)", cur_r.output)
    return {"tsc": {
        "manager": "npm", "tool": "typescript",
        "current": m.group(1) if m else "?",
        "latest": m.group(1) if m else "?",
        "updatable": False,
    }}


def _check_uv() -> dict[str, dict]:
    """`uv tool list` does not reliably see mojo when UV_TOOL_DIR is configured
    outside the invoking shell's environment, so ask the binary directly."""
    r = _run(["mojo", "--version"], timeout=30)
    if not r.spawned:
        return _error("uv", r.error)
    m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", r.output)
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

#: Binaries that raise privilege. Checked against every element of a command
#: rather than just the first, so `env FOO=1 sudo ...` or any future entry that
#: does not lead with the elevator is still recognised. A denylist is right here
#: because the set of commands is OURS — it is the constant below, not caller
#: input — so this only has to cover what we might write, not what an attacker
#: might send.
_ELEVATORS = frozenset({"sudo", "doas", "pkexec", "run0"})

#: Host-side opt-in for the elevated branch of `update(apply=True)`. Set on the
#: MACHINE, which is the whole point: `apply=True` is an argument a connected
#: model can flip on its own, and an environment variable is not.
ALLOW_ELEVATED_ENV = "CODECALC_ALLOW_RUNTIME_APPLY"

#: An empty-but-defined variable is NOT consent. `CODECALC_ALLOW_RUNTIME_APPLY=`
#: in a compose file or a systemd unit is the shape of a variable someone
#: cleared, and `os.environ.get(...) is not None` would read it as enabled.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _is_elevated(cmd: list[str]) -> bool:
    """Whether running `cmd` would raise privilege."""
    return any(part in _ELEVATORS for part in cmd)


def elevated_apply_allowed() -> bool:
    """Whether the host has opted in to running elevated update commands.

    Read per call, not cached at import: a long-lived server should pick up an
    operator's decision without a restart, and caching would make the answer
    depend on when the module happened to be imported.
    """
    return os.environ.get(ALLOW_ELEVATED_ENV, "").strip().lower() in _TRUTHY


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
    # An empty or whitespace-only filter means "no filter", exactly like None.
    # It used to produce an empty `want` set that matched nothing, so
    # `update("")` returned ok=true with total=0 — the same silently-did-nothing
    # shape as the unknown-name case below.
    if isinstance(languages, str):
        names = {l.strip().lower() for l in languages.split(",") if l.strip()}
        want = names or None
    else:
        want = set(languages) if languages else None
    results: dict[str, dict] = {}
    #: Requested names that matched something. A name nobody recognises used to
    #: filter everything out and return a cheerful empty result: `update
    #: "notalanguage"` answered {"ok": true, ... "Dry run: nothing was updated"},
    #: so a typo was indistinguishable from a no-op. The executor has always
    #: returned a structured error for an unknown language; this now does too.
    matched: set = set()
    #: manager -> why its checker could not run at all. Populated either by a
    #: checker explicitly reporting its tool is missing (_error()) or by one
    #: raising outright.
    manager_errors: dict[str, str] = {}
    for manager, checker in _CHECKERS.items():
        try:
            found = checker()
        except Exception as exc:
            found = {"__error__": {"manager": manager, "error": str(exc)}}
        err = found.get("__error__")
        if err:
            manager_errors[err.get("manager", manager)] = err.get("error") or "check failed"
            continue  # checker failure is surfaced via manager_errors, not a language entry
        for lang, info in found.items():
            if want is None or lang in want or info.get("tool") in want:
                results[lang] = info
            matched.update({lang, info.get("tool")} & (want or set()))

    for info in results.values():
        mgr = info.get("manager")
        cmd = list(UPDATE_COMMANDS.get(mgr, []))
        if mgr == "apt" and info.get("packages"):
            pkgs = info["packages"]
            cmd = [("<PKGS>" if part == "<PKGS>" else part) for part in cmd]
            cmd = [p for p in cmd if p != "<PKGS>"] + pkgs
        info["update_command"] = " ".join(cmd) if cmd else None
        # The command string has always CONTAINED "sudo" for apt, so this is
        # not new information — it is the same fact as a field instead of a
        # substring, so a caller can act on it without parsing a shell line.
        info["elevated"] = _is_elevated(cmd)

    # A requested name that never matched is either a typo (not a recognised
    # language at all) or a language whose MANAGER's check just failed — very
    # different situations that used to collapse into the same "unknown" verdict,
    # which is exactly backwards for the second case: the name is perfectly
    # valid, the tooling to check it is what is missing.
    unresolved = (want or set()) - matched
    unknown = sorted(n for n in unresolved if n not in LANGUAGE_MANAGER)
    check_failed = sorted(
        n for n in unresolved
        if n in LANGUAGE_MANAGER and LANGUAGE_MANAGER[n] in manager_errors
    )

    out = {
        "ok": True,
        "dry_run": True,
        "languages": results,
        "summary": {
            "updatable": sum(1 for r in results.values() if r.get("updatable")),
            "total": len(results),
        },
    }
    if manager_errors:
        out["manager_errors"] = manager_errors
    if unknown:
        out["unknown"] = unknown
    if check_failed:
        out["check_failed"] = check_failed
    if unknown or check_failed:
        # Nothing usable came back for anything that was asked for, so saying
        # "ok" would claim a result this call does not have — a typo and a
        # broken package manager are different reasons, but neither is success.
        if not results:
            out["ok"] = False
            reasons = []
            if unknown:
                reasons.append(f"no known runtime named {', '.join(unknown)}")
            if check_failed:
                reasons.append(f"the check failed for {', '.join(check_failed)}: "
                               + "; ".join(manager_errors[LANGUAGE_MANAGER[n]] for n in check_failed))
            out["error"] = ". ".join(reasons) + ". Nothing was checked or updated."
        else:
            notes = []
            if unknown:
                notes.append(f"ignored unrecognised name(s): {', '.join(unknown)}")
            if check_failed:
                notes.append(f"could not check (manager unavailable): {', '.join(check_failed)}")
            out["note"] = "; ".join(notes)
    return out


def update(languages: str | list[str] | None = None, apply: bool = False,
           timeout: int = 600) -> dict:
    """apply=False -> dry run (returns status + commands). apply=True -> execute."""
    st = status(languages)
    if not apply:
        st["dry_run"] = True
        st["message"] = "Dry run: nothing was updated. Call with apply=True to execute."
        return st

    if not st.get("ok"):
        # status() already explains why — an unrecognised name, or a manager
        # whose check itself failed. Proceeding into apply from here used to
        # return {"ok": True, "dry_run": False, "executed": []} regardless: a
        # typo'd language name and a broken package manager both reported
        # success for having updated nothing, indistinguishable from a
        # legitimate no-op.
        st["dry_run"] = False
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

        # An elevated command is one tool call away from a connected model:
        # `apply` is an argument it controls. The environment variable is not,
        # so this is the line between "the model decided" and "the operator
        # decided". `sudo -n` already fails closed where a password is needed;
        # this covers the passwordless rule that is common on dev machines and
        # in CI images, which is precisely where -n does NOT stop it.
        #
        # Only the elevated managers are gated. mise/rustup/npm/uv touch a
        # user-owned toolchain and stay reachable, so this does not break the
        # documented apply=True workflow on a host without apt-managed runtimes.
        if _is_elevated(cmd) and not elevated_apply_allowed():
            executed.append({
                "manager": mgr, "languages": langs, "command": " ".join(cmd),
                "exit_code": None, "elevated": True, "skipped": True,
                "output_tail": (
                    f"refused: this command raises privilege and {ALLOW_ELEVATED_ENV} "
                    f"is not set on the host. Set {ALLOW_ELEVATED_ENV}=1 in the "
                    "server's environment to allow it."
                ),
                # NOT ok. Reporting a refusal as success would be the same
                # silently-did-nothing shape #28 was filed about — the caller
                # asked for an update and did not get one, and has to be able
                # to tell that from a genuine no-op.
                "ok": False,
            })
            continue

        run = _run(cmd, timeout=timeout)
        executed.append({
            "manager": mgr, "languages": langs, "command": " ".join(cmd),
            "elevated": _is_elevated(cmd),
            "exit_code": run.exit_code,
            "output_tail": run.output[-1500:] if run.spawned else (run.error or "command did not run"),
            # An updater that never even started (missing executable, timed
            # out, ...) used to land here as exit_code=None while the
            # function's TOP-LEVEL ok stayed hardcoded True — the caller had
            # no way to learn the update it just "successfully" ran did not
            # happen at all.
            "ok": run.spawned and run.exit_code == 0,
        })

    if executed:
        succeeded = sum(1 for e in executed if e["ok"])
        all_ok = succeeded == len(executed)
    else:
        # Nothing was updatable — status() already confirmed the request was
        # valid, so an empty executed list is a legitimate no-op, not a failure.
        all_ok = True

    result = {"ok": all_ok, "dry_run": False, "executed": executed}
    if executed and not all_ok and succeeded > 0:
        result["partial_failure"] = True
    # Lifted to the top level so a caller sees WHY without walking `executed`.
    # A refusal that reads as a generic failure invites a retry; one that names
    # the variable tells the operator exactly what to do, on the host, on purpose.
    skipped = [e for e in executed if e.get("skipped")]
    if skipped:
        result["blocked_by_policy"] = [e["manager"] for e in skipped]
        # Phrasing note: "update ... set" reads as `UPDATE x SET y` to ruff's
        # S608 SQL-injection heuristic, which fired here on plain English.
        # Reworded rather than silenced with a noqa — the ambiguity was real,
        # just not a security one.
        result["message"] = (
            f"{len(skipped)} elevated command(s) were refused: "
            f"{ALLOW_ELEVATED_ENV} is not enabled on this host."
        )
    return result
