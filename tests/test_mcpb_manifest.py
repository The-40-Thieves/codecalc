"""The MCPB bundle (mcpb/) matches the package it packages.

Standalone, like the other suites: a check() accumulator, sys.exit(1) on any
failure. scripts/check_version.py is the cross-file version-consistency gate
(pyproject.toml, Cargo.toml, CHANGELOG.md, README.md, the Dockerfile, and now
mcpb/manifest.json + mcpb/pyproject.toml — see that script's own docstring);
this suite is the complementary check that mcpb/manifest.json is itself a
STRUCTURALLY VALID MCPB manifest, not just a JSON file with the right version
number in it. A manifest that parses and agrees on version but is missing a
required key, or points `mcp_config` at the wrong dependency, would pass
check_version.py and still produce a broken .mcpb — this is the gate that
would catch that class of drift, since `mcpb validate` itself (the packaging
tool's own schema check) needs network access (`npx`) and is not run here.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tomllib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import __version__

MCPB_DIR = REPO_ROOT / "mcpb"
MANIFEST_PATH = MCPB_DIR / "manifest.json"
MCPB_PYPROJECT_PATH = MCPB_DIR / "pyproject.toml"
STUB_SERVER_PATH = MCPB_DIR / "src" / "server.py"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── manifest.json parses ─────────────────────────────────────────────────────
manifest: dict = {}
try:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    check("mcpb/manifest.json parses as JSON", True)
except (OSError, json.JSONDecodeError) as exc:
    check("mcpb/manifest.json parses as JSON", False, f"-> {exc}")

# ── required top-level keys (the ones mcpb validate would also reject on) ───
for key in ("manifest_version", "name", "version", "description", "author", "server"):
    check(f"manifest has top-level {key!r}", key in manifest, f"-> keys: {sorted(manifest)}")

server = manifest.get("server", {})
for key in ("type", "entry_point", "mcp_config"):
    check(f"manifest.server has {key!r}", key in server, f"-> server keys: {sorted(server)}")

# ── the "uv" runtime type, and the mcp_config that actually launches it ─────
# See mcpb/manifest.json's own comments for why "uv" (not the legacy "python"
# type, which expects vendored site-packages) is the right choice here: it
# lets Claude Desktop's own managed uv/Python resolve the published
# `codecalc[full]` wheel — including its bundled Rust executor binary — from
# PyPI on first launch, so nothing is vendored into the bundle.
check("server.type is 'uv'", server.get("type") == "uv", f"-> {server.get('type')!r}")
check("server.entry_point is the stub script", server.get("entry_point") == "src/server.py",
      f"-> {server.get('entry_point')!r}")
mcp_config = server.get("mcp_config", {})
check("mcp_config.command is 'uv'", mcp_config.get("command") == "uv",
      f"-> {mcp_config.get('command')!r}")
check("mcp_config.args resolves the bundle dir and the stub script",
      mcp_config.get("args") == ["run", "--directory", "${__dirname}", "src/server.py"],
      f"-> {mcp_config.get('args')!r}")

# ── the stub the manifest's entry_point actually points at exists ──────────
check("mcpb/src/server.py (the declared entry_point) exists on disk",
      STUB_SERVER_PATH.is_file(), f"-> {STUB_SERVER_PATH}")

# ── version agreement (the fast, structural half; scripts/check_version.py
# is the cross-repo gate that also covers pyproject.toml/Cargo.toml/etc.) ───
check("manifest.json version matches codecalc.__version__",
      manifest.get("version") == __version__,
      f"-> manifest={manifest.get('version')!r} codecalc={__version__!r}")

mcpb_pyproject: dict = {}
try:
    mcpb_pyproject = tomllib.loads(MCPB_PYPROJECT_PATH.read_text(encoding="utf-8"))
    check("mcpb/pyproject.toml parses as TOML", True)
except (OSError, tomllib.TOMLDecodeError) as exc:
    check("mcpb/pyproject.toml parses as TOML", False, f"-> {exc}")

deps = mcpb_pyproject.get("project", {}).get("dependencies", [])
expected_pin = f"codecalc[full]=={__version__}"
check("mcpb/pyproject.toml pins the running codecalc[full] version",
      expected_pin in deps, f"-> deps={deps!r}, expected {expected_pin!r}")

# ── user_config, if declared, is well-formed (title/description on every
# entry — an MCPB field that renders directly in Claude Desktop's install UI,
# so a missing one is a UI bug, not just an internal inconsistency) ─────────
user_config = manifest.get("user_config", {})
check("manifest declares at least one user_config field", len(user_config) > 0,
      f"-> {sorted(user_config)}")
for field_name, field in user_config.items():
    check(f"user_config.{field_name} has a 'type'", "type" in field, f"-> {field}")
    check(f"user_config.{field_name} has a 'title'", "title" in field, f"-> {field}")
    check(f"user_config.{field_name} has a 'description'", "description" in field, f"-> {field}")


print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== MCPB MANIFEST OK ===")
sys.exit(1 if FAILS else 0)
