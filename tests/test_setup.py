"""`codecalc setup`: guided onboarding, and its non-destructive
default above everything else.

The property this file exists to prove: default mode is READ-ONLY. Every
other check here is secondary to "run it with no flags and nothing on disk
changes" — a setup command that could silently write to a stranger's real
client config on first run would be worse than no setup command at all.

Every filesystem check runs against a `tempfile.TemporaryDirectory()` used as
BOTH `home` and `cwd` (via setup.py's injectable `home=`/`cwd=` seams), never
against the real HOME of whatever machine runs this suite — a test that wrote
to a developer's actual `~/.config/Claude/...` to prove "nothing gets written"
would be exactly backwards.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import executor, server, setup

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def _all_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


# ── 1. default mode writes NOTHING ──────────────────────────────────────────
with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
    home = pathlib.Path(d) / "home"
    cwd = pathlib.Path(d) / "cwd"
    home.mkdir()
    cwd.mkdir()
    before = _all_files(pathlib.Path(d))
    check("a clean temp tree starts with zero files", before == [], f"-> {before}")

    buf = io.StringIO()
    code = setup.run_setup(client="claude-code", do_write=False, out=buf,
                           home=home, cwd=cwd, platform="linux")
    after = _all_files(pathlib.Path(d))
    check("default (no --write) run creates NO files on disk", after == [],
          f"-> {after}")
    check("default run still prints a config block",
          '"mcpServers"' in buf.getvalue())
    check("default run exits 0 or 1 (a real verdict, not a crash)",
          code in (0, 1), f"-> {code}")
    check("default run tells the reader nothing was written",
          "nothing was written" in buf.getvalue() or code == 1,
          "-> reminder missing and verdict was not not-ready")


# ── 2. --write MERGES into an existing config, preserving other servers ────
with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
    home = pathlib.Path(d) / "home"
    cwd = pathlib.Path(d) / "cwd"
    home.mkdir()
    cwd.mkdir()

    mcp_path = cwd / ".mcp.json"
    seeded = {
        "mcpServers": {
            "other-server": {"command": "foo", "args": ["--bar"]},
        },
        "unrelatedTopLevelKey": {"nested": True},
    }
    mcp_path.write_text(json.dumps(seeded, indent=2), encoding="utf-8")

    buf = io.StringIO()
    setup.run_setup(client="claude-code", do_write=True, out=buf,
                    home=home, cwd=cwd, platform="linux")

    written = json.loads(mcp_path.read_text(encoding="utf-8"))
    check("codecalc entry was added", "codecalc" in written.get("mcpServers", {}),
          f"-> keys={list(written.get('mcpServers', {}))}")
    check("the PRE-EXISTING server entry survives byte-for-byte",
          written.get("mcpServers", {}).get("other-server") == seeded["mcpServers"]["other-server"],
          f"-> {written.get('mcpServers', {}).get('other-server')!r}")
    check("an unrelated top-level key survives untouched",
          written.get("unrelatedTopLevelKey") == seeded["unrelatedTopLevelKey"],
          f"-> {written.get('unrelatedTopLevelKey')!r}")

    backup_path = cwd / ".mcp.json.codecalc-bak"
    check("a .codecalc-bak backup of the ORIGINAL file exists", backup_path.is_file())
    if backup_path.is_file():
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        check("the backup is the pre-write content, not the merged content",
              backup == seeded, f"-> {backup}")
        check("the backup does NOT contain codecalc (that would mean it "
              "was written to before the backup was taken)",
              "codecalc" not in backup.get("mcpServers", {}))

    check("--write reports success in its output", "wrote" in buf.getvalue())


# ── 3. invalid-JSON existing config is refused, not corrupted ──────────────
with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
    bad_path = pathlib.Path(d) / "bad.mcp.json"
    original = "{not valid json at all"
    bad_path.write_text(original, encoding="utf-8")

    result = setup.write_config(bad_path, "claude-code")
    check("write_config refuses on unparsable JSON", result["ok"] is False,
          f"-> {result}")
    check("no backup file was created for a refused write",
          not (pathlib.Path(d) / "bad.mcp.json.codecalc-bak").exists())
    check("the original file is byte-for-byte UNCHANGED",
          bad_path.read_text(encoding="utf-8") == original,
          f"-> {bad_path.read_text(encoding='utf-8')!r}")

# A JSON value that parses but is not an object (a list, a string) must also
# be refused — merging a `codecalc` key into a JSON array makes no sense and
# would otherwise raise deep inside merge_config instead of being reported.
with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
    list_path = pathlib.Path(d) / "list.mcp.json"
    list_path.write_text("[1, 2, 3]", encoding="utf-8")
    result = setup.write_config(list_path, "claude-code")
    check("write_config refuses a non-object top level", result["ok"] is False,
          f"-> {result}")
    check("...and leaves the file untouched",
          list_path.read_text(encoding="utf-8") == "[1, 2, 3]")

# A config that parses, and IS an object, but whose OWN client section is not
# an object (`{"mcpServers": [1, 2, 3]}`) must also be refused — and refused
# BEFORE the backup is written, or a malformed section leaves an orphan
# `.codecalc-bak` next to a file that was never actually touched (
# review, FIX 2).
with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
    shape_path = pathlib.Path(d) / "bad-shape.mcp.json"
    original = json.dumps({"mcpServers": [1, 2, 3]})
    shape_path.write_text(original, encoding="utf-8")

    result = setup.write_config(shape_path, "claude-code")
    check("write_config refuses a non-object CLIENT SECTION",
          result["ok"] is False, f"-> {result}")
    check("...naming the bad key in the error",
          "mcpServers" in (result["error"] or ""), f"-> {result['error']}")
    check("no backup file was created (refused before backup, not after)",
          not (pathlib.Path(d) / "bad-shape.mcp.json.codecalc-bak").exists())
    check("the original file is byte-for-byte UNCHANGED",
          shape_path.read_text(encoding="utf-8") == original,
          f"-> {shape_path.read_text(encoding='utf-8')!r}")

    # The string-section shape ({"servers": "x"}) is the same defect for a
    # different client key — checked once more so the validation is proven
    # keyed off THIS client's own section, not hardcoded to "mcpServers".
    str_path = pathlib.Path(d) / "bad-shape.vscode.json"
    str_path.write_text(json.dumps({"servers": "not an object"}), encoding="utf-8")
    result = setup.write_config(str_path, "vscode")
    check("write_config refuses a STRING client section too (vscode/'servers')",
          result["ok"] is False, f"-> {result}")
    check("no backup for the vscode case either",
          not (pathlib.Path(d) / "bad-shape.vscode.json.codecalc-bak").exists())


# ── 3c. the write is ATOMIC: temp-file-then-replace, never a truncating ────
# in-place write (review, FIX 1). Proven two ways: (a) a normal
# write leaves no stray .tmp file behind and the config is correct, and (b)
# instrumenting Path.replace shows the write actually landed in a SIBLING
# temp file in the SAME directory, with the real target still holding its
# OLD content right up until the atomic rename — the property that makes a
# crash mid-write leave either the old file or the new file, never a
# truncated one.
with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
    d_path = pathlib.Path(d)
    target = d_path / ".mcp.json"
    target.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}),
                      encoding="utf-8")

    real_replace = pathlib.Path.replace
    spy: dict = {}

    def _spy_replace(self, dest, _real=real_replace, _spy=spy):
        _spy["tmp_dir"] = self.parent
        _spy["dest_dir"] = pathlib.Path(dest).parent
        _spy["tmp_existed"] = self.exists()
        _spy["dest_old_content"] = pathlib.Path(dest).read_text(encoding="utf-8")
        return _real(self, dest)

    pathlib.Path.replace = _spy_replace
    try:
        result = setup.write_config(target, "claude-code")
    finally:
        pathlib.Path.replace = real_replace

    check("atomic write reports success", result["ok"] is True, f"-> {result}")
    check("the temp file lived in the SAME directory as the target "
          "(same filesystem -> os.replace is atomic)",
          spy.get("tmp_dir") == spy.get("dest_dir"),
          f"-> tmp_dir={spy.get('tmp_dir')} dest_dir={spy.get('dest_dir')}")
    check("the temp file existed as a real file before the rename",
          spy.get("tmp_existed") is True)
    check("the TARGET still held its OLD content right up until the atomic "
          "rename (proves the write went to a separate file, never in-place)",
          "other" in spy.get("dest_old_content", "")
          and "codecalc" not in spy.get("dest_old_content", ""),
          f"-> {spy.get('dest_old_content')!r}")
    check("no leftover .tmp file remains after a successful write",
          list(d_path.glob("*.tmp")) == [], f"-> {list(d_path.glob('*.tmp'))}")
    check("the target now holds the merged content",
          "codecalc" in target.read_text(encoding="utf-8")
          and "other" in target.read_text(encoding="utf-8"))


# ── 3d. an unknown --client fails CLEANLY (no traceback, no file touched) ──
with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
    home = pathlib.Path(d) / "home"
    cwd = pathlib.Path(d) / "cwd"
    home.mkdir()
    cwd.mkdir()
    before = _all_files(pathlib.Path(d))

    buf = io.StringIO()
    code = setup.run_setup(client="not-a-real-client", do_write=True, out=buf,
                           home=home, cwd=cwd, platform="linux")
    out = buf.getvalue()
    check("an unknown --client exits NON-ZERO", code != 0, f"-> exit {code}")
    check("...specifically 2 (a usage error, distinct from not-ready's 1)",
          code == 2, f"-> exit {code}")
    check("...and names the valid clients in a ONE-LINE message",
          all(c in out for c in setup.CLIENTS), f"-> {out!r}")
    check("...with no traceback reaching the caller",
          "Traceback" not in out, f"-> {out!r}")
    check("...and NOTHING was written, even with --write requested",
          _all_files(pathlib.Path(d)) == before,
          f"-> {_all_files(pathlib.Path(d))}")

# Same check at the real CLI boundary (subprocess, real stderr) — a unit
# call into run_setup proves the function; this proves argv parsing in
# server.py's dispatch doesn't reintroduce a traceback on the way in.
_cli_bad_client = subprocess.run(
    [sys.executable, "-m", "codecalc", "setup", "--client=not-a-real-client"],
    capture_output=True, text=True, cwd=REPO_ROOT,
)
check("CLI: `codecalc setup --client=bogus` exits non-zero",
      _cli_bad_client.returncode != 0, f"-> exit {_cli_bad_client.returncode}")
check("CLI: ...with no traceback on stderr",
      "Traceback" not in _cli_bad_client.stderr, f"-> {_cli_bad_client.stderr!r}")
check("CLI: ...and a one-line message naming the valid clients",
      all(c in _cli_bad_client.stdout for c in setup.CLIENTS),
      f"-> {_cli_bad_client.stdout!r}")


# ── 4. verdict is `degraded` when the backend is the python fallback ───────
_saved_rust = executor._rust
executor._rust = None
try:
    with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
        home = pathlib.Path(d) / "home"
        cwd = pathlib.Path(d) / "cwd"
        home.mkdir()
        cwd.mkdir()
        buf = io.StringIO()
        code = setup.run_setup(client="claude-code", do_write=False, out=buf,
                               home=home, cwd=cwd, platform="linux")
        out = buf.getvalue()
        check("python-fallback backend reports DEGRADED in the verdict line",
              "verdict: DEGRADED" in out, f"-> {out.splitlines()[-4:] if out else []}")
        check("...and exits 0 (degraded still works, unlike not-ready)",
              code == 0, f"-> exit {code}")
        check("...and names the weaker guarantee",
              "no_net" in out, "-> no_net not mentioned")
finally:
    executor._rust = _saved_rust


# ── 4b. tool-surface recommendation names the real group counts, and is ────
# suppressed once CODECALC_TOOLS is already set. Counts are read from
# server.TOOL_GROUPS/PRESETS rather than hardcoded, so this test (like
# setup.py's own line) tracks the real registry instead of a second,
# driftable copy of "25"/"39".
_saved_tools_env = os.environ.pop("CODECALC_TOOLS", None)
try:
    _core_n = sum(1 for g in server.TOOL_GROUPS.values()
                  if g in server.PRESETS["core"])
    _dev_n = sum(1 for g in server.TOOL_GROUPS.values()
                 if g in server.PRESETS["dev"])

    with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
        home = pathlib.Path(d) / "home"
        cwd = pathlib.Path(d) / "cwd"
        home.mkdir()
        cwd.mkdir()
        buf = io.StringIO()
        setup.run_setup(client="claude-code", do_write=False, out=buf,
                        home=home, cwd=cwd, platform="linux")
        out = buf.getvalue()
        check("unset CODECALC_TOOLS: setup recommends the core preset with "
              "the real tool count",
              f"CODECALC_TOOLS=core ({_core_n} tools" in out, f"-> {out}")
        check("...and the dev preset with the real tool count",
              f"CODECALC_TOOLS=dev ({_dev_n} tools" in out, f"-> {out}")
        check("...and points at the README section",
              "Reducing the tool surface" in out)

    os.environ["CODECALC_TOOLS"] = "core"
    with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
        home = pathlib.Path(d) / "home"
        cwd = pathlib.Path(d) / "cwd"
        home.mkdir()
        cwd.mkdir()
        buf = io.StringIO()
        setup.run_setup(client="claude-code", do_write=False, out=buf,
                        home=home, cwd=cwd, platform="linux")
        check("CODECALC_TOOLS already set: the recommendation is not repeated",
              "tool surface:" not in buf.getvalue())
finally:
    if _saved_tools_env is None:
        os.environ.pop("CODECALC_TOOLS", None)
    else:
        os.environ["CODECALC_TOOLS"] = _saved_tools_env


# ── 5. client auto-detect + explicit --client ───────────────────────────────
with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
    home = pathlib.Path(d) / "home"
    cwd = pathlib.Path(d) / "cwd"
    home.mkdir()
    cwd.mkdir()

    none_found = setup.detect_client(None, platform="linux", home=home, cwd=cwd)
    check("no client files present -> client is None, not a guess",
          none_found["client"] is None, f"-> {none_found}")
    check("...and reports zero found, not ambiguous",
          none_found["found"] == [] and none_found["ambiguous"] is False)

    explicit = setup.detect_client("zed", platform="linux", home=home, cwd=cwd)
    check("--client always wins, even with no file on disk yet",
          explicit["client"] == "zed", f"-> {explicit}")
    check("...and resolves to zed's real config path",
          explicit["path"] == home / ".config" / "zed" / "settings.json",
          f"-> {explicit['path']}")

    # Seed exactly one client's config; auto-detect must find that one.
    vscode_dir = cwd / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "mcp.json").write_text("{}", encoding="utf-8")
    single = setup.detect_client(None, platform="linux", home=home, cwd=cwd)
    check("exactly one config present -> auto-detected",
          single["client"] == "vscode", f"-> {single}")

    # Seed a second client's config; now it must refuse to guess.
    (cwd / ".mcp.json").write_text("{}", encoding="utf-8")
    ambiguous = setup.detect_client(None, platform="linux", home=home, cwd=cwd)
    check("two configs present -> ambiguous, client stays None",
          ambiguous["client"] is None and ambiguous["ambiguous"] is True,
          f"-> {ambiguous}")
    check("...and both are named in `found`",
          set(ambiguous["found"]) == {"vscode", "claude-code"},
          f"-> {ambiguous['found']}")
    # --client still wins outright over an ambiguous auto-detect.
    forced = setup.detect_client("cursor", platform="linux", home=home, cwd=cwd)
    check("--client overrides an ambiguous auto-detect",
          forced["client"] == "cursor", f"-> {forced}")

try:
    setup.detect_client("not-a-real-client")
    check("  ...(unreachable: expected ValueError)", False)
except ValueError:
    check("  detect_client('not-a-real-client') raises ValueError", True)


# ── 6. emitted JSON is valid and has the right shape per client ────────────
_EXPECTED_KEY = {
    "claude-desktop": "mcpServers",
    "claude-code": "mcpServers",
    "cursor": "mcpServers",
    "vscode": "servers",
    "zed": "context_servers",
}
for client_name in setup.CLIENTS:
    config = setup.build_client_config(client_name)
    # Round-trips through json.dumps/loads without error == "valid JSON",
    # the same standard build_client_config itself is held to when printed.
    reparsed = json.loads(json.dumps(config))
    check(f"{client_name}: config round-trips through JSON", reparsed == config,
          f"-> {config}")
    expected_key = _EXPECTED_KEY[client_name]
    check(f"{client_name}: top-level key is '{expected_key}'",
          expected_key in config and list(config) == [expected_key],
          f"-> keys={list(config)}")
    entry = config[expected_key].get("codecalc")
    check(f"{client_name}: has a 'codecalc' entry under '{expected_key}'",
          isinstance(entry, dict), f"-> {config[expected_key]}")
    check(f"{client_name}: entry has 'command' and 'args'",
          entry is not None and "command" in entry and "args" in entry,
          f"-> {entry}")
    check(f"{client_name}: 'command' is a non-empty string",
          isinstance(entry, dict) and isinstance(entry.get("command"), str)
          and entry["command"] != "")


# ── config_path: cross-platform, derived, never a literal machine path ─────
with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
    home = pathlib.Path(d) / "home"
    cwd = pathlib.Path(d) / "cwd"
    home.mkdir()
    cwd.mkdir()

    mac_path = setup.config_path("claude-desktop", platform="darwin", home=home, cwd=cwd)
    check("claude-desktop on macOS uses ~/Library/Application Support",
          "Library" in mac_path.parts and "Application Support" in mac_path.parts,
          f"-> {mac_path}")

    win_path = setup.config_path("claude-desktop", platform="win32", home=home,
                                 appdata=str(home / "AppData" / "Roaming"), cwd=cwd)
    check("claude-desktop on Windows uses %APPDATA%\\Claude",
          win_path == home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
          f"-> {win_path}")

    linux_path = setup.config_path("claude-desktop", platform="linux", home=home, cwd=cwd)
    check("claude-desktop on Linux falls back to ~/.config/Claude",
          linux_path == home / ".config" / "Claude" / "claude_desktop_config.json",
          f"-> {linux_path}")

    zed_path = setup.config_path("zed", platform="linux", home=home, cwd=cwd)
    check("zed uses ~/.config/zed/settings.json",
          zed_path == home / ".config" / "zed" / "settings.json", f"-> {zed_path}")

    cursor_path = setup.config_path("cursor", platform="linux", home=home, cwd=cwd)
    check("cursor is PROJECT-scoped (cwd, not home)",
          cursor_path == cwd / ".cursor" / "mcp.json", f"-> {cursor_path}")

    vscode_path = setup.config_path("vscode", platform="linux", home=home, cwd=cwd)
    check("vscode is PROJECT-scoped (cwd, not home)",
          vscode_path == cwd / ".vscode" / "mcp.json", f"-> {vscode_path}")

    try:
        setup.config_path("not-a-client")
        check("config_path rejects an unknown client", False)
    except ValueError:
        check("config_path rejects an unknown client", True)


# ── skill_destination: known conventions only, honest about the rest ───────
with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
    home = pathlib.Path(d) / "home"
    cwd = pathlib.Path(d) / "cwd"
    home.mkdir()
    cwd.mkdir()

    cc_dest = setup.skill_destination("claude-code", home=home, cwd=cwd)
    check("claude-code skill destination is PROJECT-scoped",
          cc_dest == cwd / ".claude" / "skills" / "codecalc" / "SKILL.md",
          f"-> {cc_dest}")
    cd_dest = setup.skill_destination("claude-desktop", home=home, cwd=cwd)
    check("claude-desktop skill destination is USER-scoped",
          cd_dest == home / ".claude" / "skills" / "codecalc" / "SKILL.md",
          f"-> {cd_dest}")
    for client_name in ("cursor", "vscode", "zed"):
        none_dest = setup.skill_destination(client_name, home=home, cwd=cwd)
        check(f"{client_name} has NO fabricated skill destination "
              "(no known convention)", none_dest is None, f"-> {none_dest}")


# ── write_skill: same non-destructive care as write_config ─────────────────
with tempfile.TemporaryDirectory(prefix="codecalc-setup-test-") as d:
    dest = pathlib.Path(d) / "nested" / "skills" / "codecalc" / "SKILL.md"
    result = setup.write_skill(dest)
    check("write_skill creates parent directories and the file",
          result["ok"] is True and dest.is_file(), f"-> {result}")
    check("no backup on a first write (nothing to back up)",
          result["backup"] is None)

    original_bytes = dest.read_bytes()
    dest.write_text("PRE-EXISTING CONTENT THAT MUST BE BACKED UP", encoding="utf-8")
    result2 = setup.write_skill(dest)
    check("write_skill backs up an existing destination before overwriting",
          result2["ok"] is True and result2["backup"] is not None, f"-> {result2}")
    if result2["backup"]:
        backup_bytes = pathlib.Path(result2["backup"]).read_bytes()
        check("the backup holds the OVERWRITTEN content",
              backup_bytes == b"PRE-EXISTING CONTENT THAT MUST BE BACKED UP")
        check("the destination now holds the real SKILL.md again",
              dest.read_bytes() == original_bytes)


# ── canaries and command resolution (fast, no filesystem writes needed) ────
canaries = setup.run_canaries()
check("execute_code canary passes on this host",
      canaries["execute_code"]["ok"] is True, f"-> {canaries['execute_code']}")
check("evaluate_expression canary passes on this host",
      canaries["evaluate_expression"]["ok"] is True,
      f"-> {canaries['evaluate_expression']}")

cmd = setup.resolve_command()
check("resolve_command reports a known kind",
      cmd["kind"] in ("source", "console_script", "uvx"), f"-> {cmd['kind']}")
check("resolve_command's command is an absolute path or a bare executable name",
      isinstance(cmd["command"], str) and cmd["command"] != "")
# This suite runs from a checkout (executor/Cargo.toml exists in this repo),
# so the source branch specifically must be the one detected here.
check("running from THIS checkout is detected as a 'source' install",
      cmd["kind"] == "source", f"-> {cmd}")
check("...and PYTHONPATH points at the repo root",
      cmd["env"].get("PYTHONPATH") == str(REPO_ROOT), f"-> {cmd['env']}")


# ── compute_verdict: the three states directly ──────────────────────────────
ok_client = {"client": "claude-code", "ambiguous": False, "found": []}
ok_canaries = {"execute_code": {"ok": True}, "evaluate_expression": {"ok": True}}
verdict, reasons = setup.compute_verdict(
    client=ok_client, canaries=ok_canaries, backend_kind="rust",
    workspace_writable=True)
check("everything healthy -> READY with no reasons",
      verdict == "ready" and reasons == [], f"-> {verdict}, {reasons}")

verdict, reasons = setup.compute_verdict(
    client=ok_client, canaries=ok_canaries, backend_kind="python",
    workspace_writable=True)
check("python backend only -> DEGRADED", verdict == "degraded", f"-> {verdict}")

bad_canaries = {"execute_code": {"ok": False}, "evaluate_expression": {"ok": True}}
verdict, reasons = setup.compute_verdict(
    client=ok_client, canaries=bad_canaries, backend_kind="rust",
    workspace_writable=True)
check("a failing canary -> NOT-READY", verdict == "not-ready", f"-> {verdict}")

no_client = {"client": None, "ambiguous": False, "found": []}
verdict, reasons = setup.compute_verdict(
    client=no_client, canaries=ok_canaries, backend_kind="rust",
    workspace_writable=True)
check("no client resolved -> NOT-READY", verdict == "not-ready", f"-> {verdict}")

verdict, reasons = setup.compute_verdict(
    client=ok_client, canaries=ok_canaries, backend_kind="rust",
    workspace_writable=False)
check("unwritable workspace -> NOT-READY", verdict == "not-ready", f"-> {verdict}")


# ── the CLI surface: `codecalc setup --help` lists it, subprocess round-trip ─
help_out = subprocess.run([sys.executable, "-m", "codecalc", "--help"],
                          capture_output=True, text=True, cwd=REPO_ROOT)
check("`codecalc --help` lists the setup subcommand",
      "setup" in help_out.stdout, f"-> {help_out.stdout[:200]}")

cli = subprocess.run(
    [sys.executable, "-m", "codecalc", "setup", "--client=zed"],
    capture_output=True, text=True, cwd=REPO_ROOT,
)
check("`codecalc setup --client=zed` runs end-to-end without a traceback",
      "Traceback" not in cli.stderr, f"-> stderr={cli.stderr[:300]}")
check("...and prints a verdict line",
      "verdict:" in cli.stdout, f"-> {cli.stdout[-200:]}")
check("...and exits 0 or 1, never a crash code",
      cli.returncode in (0, 1), f"-> exit {cli.returncode}")


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else "\n=== SETUP IS NON-DESTRUCTIVE AND CORRECT ===")
for f in FAILS:
    print(f"  {f}")
sys.exit(1 if FAILS else 0)
