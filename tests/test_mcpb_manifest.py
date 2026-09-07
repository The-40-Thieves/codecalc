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
import re
import subprocess
import sys
import tomllib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import __version__

MCPB_DIR = REPO_ROOT / "mcpb"
MANIFEST_PATH = MCPB_DIR / "manifest.json"
MCPB_PYPROJECT_PATH = MCPB_DIR / "pyproject.toml"
STUB_SERVER_PATH = MCPB_DIR / "src" / "server.py"

#: mcpb/pyproject.toml's own extractor regex — see that file's comment and
#: scripts/check_version.py's mcpb_dependency_pin_version() for why this is
#: `!=`/`or` (De Morgan's law) rather than a `not (...)` PEP 508 has no
#: grammar for.
_DEPENDENCY_PIN_RE = re.compile(r"^codecalc\[full\]==([^\s;]+)(?:\s*;\s*(.*))?$")
_WIN_ARM64_MARKER = "sys_platform != 'win32' or platform_machine != 'ARM64'"

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
pin_match = next((m for d in deps if (m := _DEPENDENCY_PIN_RE.match(d))), None)
check("mcpb/pyproject.toml pins the running codecalc[full] version",
      pin_match is not None and pin_match.group(1) == __version__,
      f"-> deps={deps!r}, expected version {__version__!r}")

# ── the Windows ARM64 exclusion marker, structurally ────────────────────────
# codecalc's own dependency chain (mcp -> pyjwt[crypto] -> cryptography) ships
# no win_arm64 wheel on PyPI (confirmed against cryptography 50.0.1; see
# mcpb/pyproject.toml's own comment and release.yml's windows-aarch64 leg,
# which documents the same gap). Without this marker `uv run` would attempt
# to build cryptography from source on that platform instead of failing
# cleanly.
check("mcpb/pyproject.toml's codecalc[full] pin excludes Windows on ARM64",
      pin_match is not None and pin_match.group(2) == _WIN_ARM64_MARKER,
      f"-> marker={pin_match.group(2) if pin_match else None!r}, "
      f"expected {_WIN_ARM64_MARKER!r}")

# ── the Windows ARM64 guard in mcpb/src/server.py, BEHAVIORALLY ─────────────
# Per this repo's own testing rule ("assert the value, not the shape" — see
# README.md's "## Test" section): a text grep for `platform.machine()` would
# pass even if the guard's condition were backwards, or if it forgot to
# `raise SystemExit` before falling through to the codecalc import. This
# actually RUNS the stub in a subprocess with sys.platform/platform.machine()
# monkeypatched to Windows ARM64 — matching the marker above exactly — and
# asserts it exits non-zero and never reaches the `codecalc` import (a
# ModuleNotFoundError in stderr would mean the guard ran too late, after
# `uv run` had already skipped installing codecalc on this platform).
_WIN_ARM64_PROBE = (
    "import sys, runpy\n"
    "sys.platform = 'win32'\n"
    "import platform\n"
    "platform.machine = lambda: 'ARM64'\n"
    "try:\n"
    f"    runpy.run_path({str(STUB_SERVER_PATH)!r}, run_name='__main__')\n"
    "except SystemExit as e:\n"
    "    sys.exit(e.code if isinstance(e.code, int) else 1)\n"
)
try:
    _probe = subprocess.run([sys.executable, "-c", _WIN_ARM64_PROBE],
                             capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    check("mcpb/src/server.py exits non-zero on a simulated Windows ARM64",
          _probe.returncode != 0, f"-> exit_code={_probe.returncode}")
    # A clean `raise SystemExit(1)` after a `print(..., file=sys.stderr)`
    # produces NO Python traceback — just the printed message. Any traceback
    # in stderr means the guard did NOT short-circuit and something further
    # down (the `codecalc` import, or anything past it) blew up instead —
    # regardless of which exception class that happens to be on this
    # particular dev machine (verified: deliberately breaking the guard's
    # condition here crashed with an unrelated _ssl ImportError, not
    # ModuleNotFoundError — a traceback either way, which is the actual
    # invariant this checks).
    check("...and never falls through to the codecalc import (a clean exit, no traceback)",
          "Traceback" not in _probe.stderr, f"-> stderr={_probe.stderr!r}")
    check("...and names the actual cause in its message",
          "ARM64" in _probe.stderr or "cryptography" in _probe.stderr,
          f"-> stderr={_probe.stderr!r}")
except subprocess.TimeoutExpired:
    check("mcpb/src/server.py exits non-zero on a simulated Windows ARM64", False,
          "-> the probe subprocess hung for 30s, which means the guard did not "
          "short-circuit before reaching main()'s stdio server")

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

# ── the manifest discloses first-launch network behaviour ──────────────────
# There is no vendored fallback: `uv run` resolves `codecalc[full]` (~120 MB)
# from PyPI on first launch, and an offline first launch fails rather than
# degrading. That is worth a user reading BEFORE they install, not just in
# this repo's own README, so it belongs in the manifest's own long_description
# (rendered on Claude Desktop's install screen).
long_description = manifest.get("long_description", "")
check("manifest.long_description discloses the first-launch network dependency",
      "network" in long_description.lower() or "pypi" in long_description.lower(),
      f"-> {long_description!r}")


print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== MCPB MANIFEST OK ===")
sys.exit(1 if FAILS else 0)
