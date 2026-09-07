#!/usr/bin/env python3
"""Build the codecalc MCPB bundle: dist/codecalc-<version>.mcpb.

The MCPB ("MCP Bundle") format is Claude Desktop's one-click extension
archive. `mcpb/` holds the checked-in bundle sources — see `mcpb/manifest.json`
for why the server is declared as the "uv" runtime type rather than "python":
the bundle vendors nothing (no site-packages, no copy of the Rust
`codecalc-exec` binary). It ships a two-line stub (`mcpb/src/server.py`) and a
`mcpb/pyproject.toml` that pins the one dependency that matters —
`codecalc[full]==<version>`, the SAME wheel `pip install 'codecalc[full]'`
gets, executor binary included — and lets Claude Desktop's managed `uv`
runtime resolve and cache it into an isolated venv on first launch. That
keeps the bundle itself a few kilobytes regardless of codecalc's own size.

This script does three things, in order:

  1. FLOOR, before anything else runs: confirms mcpb/manifest.json's
     "version" and mcpb/pyproject.toml's pinned "codecalc[full]==X.Y.Z"
     dependency already agree with pyproject.toml's own version. Both are
     hand-maintained literals, gated in CI by scripts/check_version.py (see
     that script's own docstring) — this is the fast, local half of the same
     check, so a stale pin fails a build here instead of shipping a bundle
     that silently launches last release's codecalc forever.
  2. `mcpb validate mcpb/` — the packaging tool's own schema check.
  3. `mcpb pack mcpb/ dist/codecalc-<version>.mcpb`.

The `@anthropic-ai/mcpb` CLI is pinned to an EXACT version below and run via
`npx -y <pkg>@<version>`, never `@latest`: an unpinned packaging tool could
change what a release-tagged commit produces with no diff anywhere in this
repo to review.

Usage: python scripts/build_mcpb.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MCPB_DIR = REPO / "mcpb"
DIST_DIR = REPO / "dist"

#: Pinned exact version of the `@anthropic-ai/mcpb` packaging CLI (the tool
#: that builds the bundle, unrelated to codecalc's own version above). Bump
#: deliberately, in a reviewable diff.
MCPB_CLI_VERSION = "2.1.2"


def codecalc_version() -> str:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def run(args: list[str]) -> None:
    print(f"$ {' '.join(args)}")
    subprocess.run(args, check=True)


def main() -> int:
    version = codecalc_version()
    print(f"building the MCPB bundle for codecalc {version}")

    manifest = json.loads((MCPB_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest_version = manifest.get("version")
    if manifest_version != version:
        print(f"::error::mcpb/manifest.json version {manifest_version!r} does not match "
              f"pyproject.toml version {version!r} — run scripts/check_version.py")
        return 1

    mcpb_pyproject = tomllib.loads((MCPB_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    deps = mcpb_pyproject.get("project", {}).get("dependencies", [])
    expected_pin = f"codecalc[full]=={version}"
    if expected_pin not in deps:
        print(f"::error::mcpb/pyproject.toml does not pin {expected_pin!r} "
              f"(dependencies: {deps!r}) — run scripts/check_version.py")
        return 1
    print(f"ok   mcpb/manifest.json and mcpb/pyproject.toml agree on {version}")

    DIST_DIR.mkdir(exist_ok=True)
    output = DIST_DIR / f"codecalc-{version}.mcpb"
    cli = f"@anthropic-ai/mcpb@{MCPB_CLI_VERSION}"

    run(["npx", "-y", cli, "validate", str(MCPB_DIR)])
    run(["npx", "-y", cli, "pack", str(MCPB_DIR), str(output)])

    if not output.is_file():
        print(f"::error::{output} was not produced")
        return 1
    print(f"ok   built {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
