"""`codecalc setup`: guided onboarding to a working MCP connection (THE-897).

The gap this fills: README.md documents every piece a stranger needs — which
extra to install, which JSON key their client wants, where the config file
lives, what `codecalc doctor` reports — but reading and assembling all of that
by hand is the ~30-minute version of a task that should take under 10. This
walks the same ground `codecalc doctor` already measures (backend, extras,
grammar cache, workspace) plus what doctor does not: which client is present
on THIS machine, the exact config block in THAT client's shape, and two real
canaries proving the connection would actually work — then reduces all of it
to one verdict: `ready` / `degraded` / `not-ready`.

REUSE, NOT REIMPLEMENT: the executor-backend/extras/grammar-cache/workspace
checks all come from `doctor.report()`. This module adds only what doctor does
not already know — client detection, per-client config shape, and canaries —
and renders them alongside doctor's own findings.

NON-DESTRUCTIVE BY DEFAULT, the most important property here. Without
`--write` this only prints: the config block, the exact file path, the skill
destination. Nothing on disk changes — not the client config, not a skill
file, not a grammar cache. `--write` is what turns each of those into an
action, and even then only after a backup (see `write_config`/`write_skill`),
and never by asking a caller to trust an in-place overwrite: `merge_config`
touches only the `codecalc` key under the client's own top-level key and
leaves every other key in the file exactly as it was.

Every path below is DERIVED from `Path.home()`, `os.environ`, `Path.cwd()`
and `__file__` — never a literal machine path — because this module ships in
the wheel to every install; a hardcoded path here would fail
`scripts/check_portability.py` and be wrong on every machine but the one it
was written on.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from . import doctor, executor, logic

#: Every client this command knows how to configure. Ordered to match the
#: README's own client-config table, so `--client` help text and detection
#: probe order both read the same as the docs a user already has open.
CLIENTS: tuple[str, ...] = ("claude-desktop", "claude-code", "cursor", "vscode", "zed")

#: The top-level JSON key each client expects the server entry under. Three
#: different spellings for the same concept — README.md states this table in
#: prose; this is the machine-readable copy `build_client_config` reads from,
#: so the two cannot drift the way the tool/language counts once did before
#: check_claims.py existed for them.
_CLIENT_KEY: dict[str, str] = {
    "claude-desktop": "mcpServers",
    "claude-code": "mcpServers",
    "cursor": "mcpServers",
    "vscode": "servers",
    "zed": "context_servers",
}

#: Where each client's shipped-skill destination is a KNOWN convention.
#: Claude Code and Claude Desktop both read project/user `skills/` trees;
#: Cursor, VS Code and Zed have no equivalent "drop a SKILL.md here" feature
#: as of this writing — inventing a path for those would be a guess dressed
#: up as a fact, so they are deliberately absent from this table and
#: `skill_destination` reports that honestly instead of fabricating one.
_SKILL_DEST: dict[str, str] = {
    "claude-code": "project",  # <cwd>/.claude/skills/codecalc/SKILL.md
    "claude-desktop": "user",  # <home>/.claude/skills/codecalc/SKILL.md
}


# ── client detection & config paths ─────────────────────────────────────────

def config_path(client: str, *, platform: str = sys.platform,
                home: Path | None = None, appdata: str | None = None,
                cwd: Path | None = None) -> Path:
    """Where `client`'s MCP config file lives, cross-platform.

    Mirrors README.md's own client-config section — this derives the same
    paths the docs already state rather than inventing a second source of
    truth for them. `home`/`appdata`/`cwd`/`platform` are injectable so tests
    exercise every platform branch without touching a real HOME or APPDATA.
    """
    if client not in CLIENTS:
        raise ValueError(f"unknown client {client!r}; choose one of {CLIENTS}")
    home = home if home is not None else Path.home()
    cwd = cwd if cwd is not None else Path.cwd()
    appdata = appdata if appdata is not None else os.environ.get("APPDATA", "")

    if client == "claude-desktop":
        if platform == "darwin":
            return (home / "Library" / "Application Support" / "Claude"
                    / "claude_desktop_config.json")
        if platform.startswith("win"):
            base = Path(appdata) if appdata else home / "AppData" / "Roaming"
            return base / "Claude" / "claude_desktop_config.json"
        # Linux: Anthropic ships Claude Desktop for macOS/Windows only, but
        # the unofficial Linux build follows the same ~/.config/<App>
        # convention Zed uses below — the best available default, named as
        # such rather than presented as documented upstream.
        return home / ".config" / "Claude" / "claude_desktop_config.json"
    if client == "claude-code":
        # Project-scoped, per README ("Claude Code (.mcp.json)").
        return cwd / ".mcp.json"
    if client == "cursor":
        return cwd / ".cursor" / "mcp.json"
    if client == "vscode":
        return cwd / ".vscode" / "mcp.json"
    # client == "zed"
    if platform.startswith("win"):
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "Zed" / "settings.json"
    return home / ".config" / "zed" / "settings.json"


def detect_client(explicit: str | None = None, *, platform: str = sys.platform,
                  home: Path | None = None, appdata: str | None = None,
                  cwd: Path | None = None) -> dict:
    """Which client to configure.

    `explicit` always wins (and need not already exist on disk — a client
    setup is creating a config for the first time is the common case, not the
    exception). Otherwise every known client's config path is probed for an
    EXISTING file; exactly one match resolves it, zero or several leave
    `client: None` so the caller asks the user to pass `--client` rather than
    guess which of several installed clients they meant.
    """
    if explicit is not None:
        if explicit not in CLIENTS:
            raise ValueError(f"unknown client {explicit!r}; choose one of {CLIENTS}")
        return {
            "client": explicit,
            "path": config_path(explicit, platform=platform, home=home,
                                appdata=appdata, cwd=cwd),
            "found": [], "ambiguous": False,
        }
    found = [c for c in CLIENTS
             if config_path(c, platform=platform, home=home, appdata=appdata,
                            cwd=cwd).is_file()]
    if len(found) == 1:
        return {
            "client": found[0],
            "path": config_path(found[0], platform=platform, home=home,
                                appdata=appdata, cwd=cwd),
            "found": found, "ambiguous": False,
        }
    return {"client": None, "path": None, "found": found, "ambiguous": len(found) > 1}


def skill_destination(client: str, *, home: Path | None = None,
                      cwd: Path | None = None) -> Path | None:
    """Where to copy `codecalc/SKILL.md` for `client`, or None when this
    client has no known skills convention (see `_SKILL_DEST`)."""
    scope = _SKILL_DEST.get(client)
    if scope is None:
        return None
    home = home if home is not None else Path.home()
    cwd = cwd if cwd is not None else Path.cwd()
    root = cwd if scope == "project" else home
    return root / ".claude" / "skills" / "codecalc" / "SKILL.md"


# ── command/args: uvx vs console script vs a source checkout ───────────────

def _is_source_checkout() -> bool:
    """True when this process is running from a codecalc git checkout.

    `pyproject.toml` and `executor/Cargo.toml` sitting beside the `codecalc/`
    package directory is a fingerprint only a checkout has — an installed
    wheel's `codecalc/` lives in `site-packages` with neither file next to
    it. Derived from `__file__`, so this is true on whichever machine is
    actually running it and never a literal path.
    """
    root = Path(__file__).resolve().parent.parent
    return (root / "pyproject.toml").is_file() and (root / "executor" / "Cargo.toml").is_file()


def resolve_command() -> dict:
    """The command/args/env a client config should launch codecalc with.

    Three cases, most-specific first:

    - `source`: running from a git checkout — `uvx` would fetch whatever is
      on PyPI, not the code in this tree, so this uses the absolute venv
      interpreter plus `-m codecalc` and points `PYTHONPATH` at the checkout,
      matching README's own "Run the server" config example.
    - `console_script`: `codecalc` already resolves on PATH (a prior `uvx
      tool install`, `pipx install`, or `pip install` into an active venv) —
      README's bare "Installed with pip install codecalc" form.
    - `uvx`: neither of the above — README's own default recommendation for
      a reader who has installed nothing yet.
    """
    if _is_source_checkout():
        root = Path(__file__).resolve().parent.parent
        return {"kind": "source", "command": sys.executable,
                "args": ["-m", "codecalc"], "env": {"PYTHONPATH": str(root)}}
    exe = shutil.which("codecalc")
    if exe:
        return {"kind": "console_script", "command": exe, "args": [], "env": {}}
    return {"kind": "uvx", "command": "uvx", "args": ["codecalc[full]"], "env": {}}


def build_server_entry() -> dict:
    """The `{"command": ..., "args": ..., ["env": ...]}` block for the
    `codecalc` server, independent of which client's key it sits under."""
    cmd = resolve_command()
    entry: dict = {"command": cmd["command"], "args": cmd["args"]}
    if cmd["env"]:
        entry["env"] = cmd["env"]
    return entry


def build_client_config(client: str) -> dict:
    """A full config document containing ONLY the codecalc entry — what
    `--write` merges into, and what default mode prints standalone."""
    if client not in CLIENTS:
        raise ValueError(f"unknown client {client!r}; choose one of {CLIENTS}")
    return {_CLIENT_KEY[client]: {"codecalc": build_server_entry()}}


# ── --write: merge, never overwrite ─────────────────────────────────────────

def merge_config(existing: dict, client: str) -> dict:
    """`existing` with ONLY the `codecalc` entry under this client's own
    top-level key added or replaced. Every other key — other MCP servers,
    unrelated client settings — passes through byte-for-byte unchanged."""
    key = _CLIENT_KEY[client]
    merged = dict(existing)
    section = dict(merged.get(key) or {})
    section["codecalc"] = build_server_entry()
    merged[key] = section
    return merged


def write_config(path: Path, client: str) -> dict:
    """Write the merged config to `path`, backing up first. Refuses rather
    than corrupts on unparsable existing JSON.

    Three cases:
    - `path` does not exist: create it with just the codecalc entry.
    - `path` exists and parses as JSON (or is empty): back it up to
      `<path>.codecalc-bak`, THEN write the merge — the backup happens before
      any byte of the real file changes.
    - `path` exists and does NOT parse: refuse. Writing a best-effort guess
      over a file this cannot understand is how a user's real config gets
      corrupted; reporting the problem and touching nothing is the only safe
      move (`ok: false`, no backup, no write).
    """
    backup: Path | None = None
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
        try:
            existing = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            return {"ok": False, "path": str(path), "backup": None,
                    "error": f"existing config is not valid JSON ({exc}); "
                             "refusing to write — fix or remove it by hand first"}
        if not isinstance(existing, dict):
            return {"ok": False, "path": str(path), "backup": None,
                    "error": "existing config's top level is not a JSON object; "
                             "refusing to write over an unrecognised shape"}
        backup = path.with_name(path.name + ".codecalc-bak")
        backup.write_text(raw, encoding="utf-8")
    else:
        existing = {}
        path.parent.mkdir(parents=True, exist_ok=True)

    merged = merge_config(existing, client)
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(path), "backup": str(backup) if backup else None,
            "error": None}


def write_skill(dest: Path) -> dict:
    """Copy `codecalc/SKILL.md` to `dest`, backing up any file already
    there. Same care as `write_config`: never a silent overwrite."""
    src = Path(__file__).resolve().parent / "SKILL.md"
    if not src.is_file():
        return {"ok": False, "path": str(dest), "backup": None,
                "error": f"{src} does not exist in this install"}
    backup: Path | None = None
    if dest.is_file():
        backup = dest.with_name(dest.name + ".codecalc-bak")
        backup.write_bytes(dest.read_bytes())
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    return {"ok": True, "path": str(dest), "backup": str(backup) if backup else None,
            "error": None}


# ── canaries: real calls, in-process, no server spawned ────────────────────

def run_canaries() -> dict:
    """A real `execute_code` and a real `evaluate_expression`, called
    in-process against the same functions the MCP tools call — proof the
    connection would actually work, not just that the process starts.

    Deliberately calls `executor.execute`/`logic.evaluate_expression`
    directly rather than spawning `codecalc.server` and speaking MCP over a
    pipe to itself: this needs to know THIS process's backend/extras worked,
    which a subprocess round-trip would only re-measure indirectly and much
    more slowly.
    """
    exec_result = executor.execute("python3", 'print("codecalc-setup-canary")', timeout=10)
    exec_ok = bool(exec_result.get("ok")) and "codecalc-setup-canary" in (exec_result.get("stdout") or "")

    expr_result = logic.evaluate_expression("sqrt(144) + 2**10")
    expr_ok = bool(expr_result.get("ok"))

    return {
        "execute_code": {"ok": exec_ok, "result": exec_result},
        "evaluate_expression": {"ok": expr_ok, "result": expr_result},
    }


# ── verdict ──────────────────────────────────────────────────────────────

def compute_verdict(*, client: dict, canaries: dict, backend_kind: str,
                    workspace_writable: bool) -> tuple[str, list[str]]:
    """One of `ready`/`degraded`/`not-ready`, plus WHY.

    `not-ready`: something is broken enough that a connection would not work
    at all — no writable workspace, a failing canary, or no client resolved
    to configure. `degraded`: the connection works but with a known-weaker
    guarantee (the python fallback backend, no `no_net` enforcement). `ready`:
    neither.
    """
    blocking: list[str] = []
    if not workspace_writable:
        blocking.append("workspace is not writable — nothing can execute here")
    if not canaries["execute_code"]["ok"]:
        blocking.append("execute_code canary failed")
    if not canaries["evaluate_expression"]["ok"]:
        blocking.append("evaluate_expression canary failed")
    if client["client"] is None:
        if client["ambiguous"]:
            blocking.append(
                f"multiple client configs found ({', '.join(client['found'])}) — "
                "pass --client to say which one")
        else:
            blocking.append(
                f"no client config detected — pass --client=NAME "
                f"(one of {', '.join(CLIENTS)})")
    if blocking:
        return "not-ready", blocking

    weak: list[str] = []
    if backend_kind != "rust":
        weak.append("execution backend is the python fallback — no_net and "
                    "peak_memory_kb are not enforced")
    if weak:
        return "degraded", weak
    return "ready", []


# ── the command itself ──────────────────────────────────────────────────

def run_setup(client: str | None = None, do_write: bool = False, *,
             out=None, home: Path | None = None, cwd: Path | None = None,
             platform: str | None = None, appdata: str | None = None) -> int:
    """Print (and, with `do_write`, apply) a full onboarding walkthrough.

    `home`/`cwd`/`platform`/`appdata` are the same injectable seams
    `detect_client`/`config_path` take, threaded through so a test can run
    the WHOLE command against a temp directory tree without touching the
    real HOME or the real client configs on the machine running the suite.
    Left at their defaults (None), each falls back to the live environment —
    exactly what a person running `codecalc setup` at a shell wants.

    Returns the process exit code: 0 for `ready`/`degraded`, 1 for
    `not-ready` — the same narrow-healthy convention `codecalc doctor`
    already uses, so a script gating on this in CI needs no new convention.
    """
    out = out or sys.stdout
    p = lambda *a: print(*a, file=out)  # noqa: E731 — single call site below
    platform = platform if platform is not None else sys.platform

    rep = doctor.report(deep=False)

    p("codecalc setup")
    p()

    # 1. client detection
    detected = detect_client(client, platform=platform, home=home,
                             appdata=appdata, cwd=cwd)
    if detected["client"] is not None:
        origin = "explicit --client" if client else "detected"
        p(f"✓ client: {detected['client']} ({origin}) -> {detected['path']}")
    elif detected["ambiguous"]:
        p(f"⚠ client: AMBIGUOUS — config files found for "
          f"{', '.join(detected['found'])}")
        p(f"  pass --client=NAME to pick one ({', '.join(CLIENTS)})")
    else:
        p("⚠ client: none detected")
        p(f"  pass --client=NAME ({', '.join(CLIENTS)}), or create the client "
          "first and re-run")
        for c in CLIENTS:
            p(f"    {c:16} "
              f"{config_path(c, platform=platform, home=home, appdata=appdata, cwd=cwd)}")

    # 2. executor backend
    backend_kind = rep["backend"]["kind"]
    if backend_kind == "rust":
        p(f"✓ executor: rust ({rep['backend']['binary']}) — full sandbox, no_net enforced")
    else:
        p("⚠ executor: python-fallback — DEGRADED: no_net and peak_memory_kb "
          "are not enforced")
        p("  build the native executor: cargo build --release --manifest-path "
          "executor/Cargo.toml && mkdir -p bin && cp "
          "executor/target/release/codecalc-exec bin/")

    # 3. extras / grammar cache
    for extra in rep["extras"]:
        if extra["installed"]:
            p(f"✓ extra: {extra['name']}")
        else:
            p(f"⚠ extra: {extra['name']} MISSING — {extra['remedy']}")
    gc = rep["grammar_cache"]
    if gc["extra_installed"] and not gc["cached"]:
        p("⚠ grammar cache: COLD — first analyze_complexity call per "
          "language will download a grammar")
        p("  warm it: codecalc-prefetch-grammars  (or, from source: "
          "python scripts/prefetch_grammars.py)")
        if do_write:
            from . import prefetch
            p("  running codecalc-prefetch-grammars (--write)...")
            code = prefetch.main()
            p("  ✓ grammar cache warmed" if code == 0
              else f"  ✗ prefetch exited {code}")
    elif gc["extra_installed"]:
        p(f"✓ grammar cache: {gc['grammars']} cached at {gc['path']}")

    # 4. client config
    if detected["client"] is not None:
        config = build_client_config(detected["client"])
        p(f"\n  MCP client config ({_CLIENT_KEY[detected['client']]}) for "
          f"{detected['path']}:")
        p(json.dumps(config, indent=2))
        if do_write:
            result = write_config(detected["path"], detected["client"])
            if result["ok"]:
                bak = f" (backup: {result['backup']})" if result["backup"] else " (new file)"
                p(f"✓ wrote {result['path']}{bak}")
            else:
                p(f"✗ refused to write {result['path']}: {result['error']}")

    # 5. skill
    skill_path = rep["skill_file"]
    p(f"\n  skill file: {skill_path or '(MISSING from this install)'}")
    if detected["client"] is not None:
        dest = skill_destination(detected["client"], home=home, cwd=cwd)
        if dest is not None:
            p(f"  destination for {detected['client']}: {dest}")
            if do_write and skill_path:
                result = write_skill(dest)
                if result["ok"]:
                    bak = f" (backup: {result['backup']})" if result["backup"] else " (new file)"
                    p(f"✓ copied skill to {result['path']}{bak}")
                else:
                    p(f"✗ could not copy skill: {result['error']}")
        else:
            p(f"  {detected['client']} has no known skills-directory convention — "
              "if it supports project/custom instructions, add SKILL.md's "
              "calling rules there by hand")

    # 6. canaries
    canaries = run_canaries()
    ec = canaries["execute_code"]
    ee = canaries["evaluate_expression"]
    p(f"\n{'✓' if ec['ok'] else '✗'} canary: execute_code (python3) "
      f"{'passed' if ec['ok'] else 'FAILED — ' + str(ec['result'])}")
    p(f"{'✓' if ee['ok'] else '✗'} canary: evaluate_expression "
      f"{'passed' if ee['ok'] else 'FAILED — ' + str(ee['result'])}")

    # 7. security level
    p(f"\n  security: default rlimit sandbox is ALWAYS on "
      f"({backend_kind} backend). Strict isolation (gVisor+Docker on Linux, "
      "AppContainer on Windows) is opt-in and currently "
      f"{'available' if rep['strict_runtime']['available'] else 'UNAVAILABLE'} "
      "on this host — see `codecalc doctor` for detail.")

    verdict, reasons = compute_verdict(
        client=detected, canaries=canaries, backend_kind=backend_kind,
        workspace_writable=rep["workspace"]["writable"])
    p(f"\nverdict: {verdict.upper()}")
    for r in reasons:
        p(f"  - {r}")
    if not do_write and verdict != "not-ready":
        p("\n(nothing was written — re-run with --write to apply the config "
          "and skill changes above)")

    return 0 if verdict != "not-ready" else 1
