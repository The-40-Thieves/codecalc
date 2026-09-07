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

Only the step that touches the network retries — 3 attempts, linear
backoff, the same shape as codecalc/prefetch.py's grammar-download retry
(see that module's own comment for the reasoning: one attempt is a coin
flip on the network, not a measurement of it). That step is a cheap
`npx -y <cli>@<version> --version`: `npx` resolves and caches the pinned
CLI from the npm registry on first invocation, so this warms the same
cache `validate`/`pack` will read from. release.yml's release-assets job
(checksums, build-provenance attestation, `gh release upload`) depends on
this script succeeding, so a transient npm fetch failure would otherwise
skip drafting the entire GitHub release even though every platform wheel
built and verified cleanly.

`validate` and `pack` run exactly once each, with no retry: once the CLI is
cached, both are local, deterministic operations, so a non-zero exit is
mcpb's own schema check or packer rejecting the bundle — a genuinely broken
manifest — and retrying it three times would only burn ~6s reproducing the
same failure before finally surfacing it.

Usage: python scripts/build_mcpb.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MCPB_DIR = REPO / "mcpb"
DIST_DIR = REPO / "dist"

#: Pinned exact version of the `@anthropic-ai/mcpb` packaging CLI (the tool
#: that builds the bundle, unrelated to codecalc's own version above). Bump
#: deliberately, in a reviewable diff.
MCPB_CLI_VERSION = "2.1.2"

#: Same shape as codecalc/prefetch.py's ATTEMPTS/BACKOFF_SECONDS: 3 attempts,
#: linear backoff, enough to distinguish "the network blipped" from "npm is
#: actually down" without hanging a release build indefinitely.
ATTEMPTS = 3
BACKOFF_SECONDS = 2

#: `codecalc[full]==X.Y.Z`, optionally followed by a PEP 508 environment
#: marker (mcpb/pyproject.toml carries one to exclude Windows on ARM64 — see
#: that file's own comment). Mirrors scripts/check_version.py's own
#: extractor; this script only needs the VERSION out of it, not the marker
#: text — tests/test_mcpb_manifest.py is what asserts the marker itself.
_DEPENDENCY_PIN_RE = re.compile(r"^codecalc\[full\]==([^\s;]+)(?:\s*;.*)?$")


def codecalc_version() -> str:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def run(args: list[str]) -> None:
    """Run args once. No retry: a non-zero exit here is a semantic failure
    (mcpb rejecting the manifest, a bad bundle directory, ...) and should
    surface immediately with the CLI's own stderr, not be retried."""
    print(f"$ {' '.join(args)}")
    subprocess.run(args, check=True)


def retry_run(args: list[str]) -> None:
    """Run args with retry — for the one step that touches the network
    (resolving/caching the pinned mcpb CLI via npx). 3 attempts, linear
    backoff; see this module's docstring for why only this step retries."""
    print(f"$ {' '.join(args)}")
    last_exc: subprocess.CalledProcessError | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            subprocess.run(args, check=True)
            return
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            if attempt < ATTEMPTS:
                wait = BACKOFF_SECONDS * attempt
                print(f"::warning::{args[0]} failed (attempt {attempt}/{ATTEMPTS}, "
                      f"exit {exc.returncode}); retrying in {wait}s")
                time.sleep(wait)
    print(f"::error::{args[0]} failed after {ATTEMPTS} attempts")
    raise last_exc


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
    pinned_version = next((m.group(1) for d in deps if (m := _DEPENDENCY_PIN_RE.match(d))), None)
    if pinned_version != version:
        print(f"::error::mcpb/pyproject.toml pins codecalc[full]=={pinned_version!r}, "
              f"expected {version!r} (dependencies: {deps!r}) — run scripts/check_version.py")
        return 1
    print(f"ok   mcpb/manifest.json and mcpb/pyproject.toml agree on {version}")

    DIST_DIR.mkdir(exist_ok=True)
    output = DIST_DIR / f"codecalc-{version}.mcpb"
    cli = f"@anthropic-ai/mcpb@{MCPB_CLI_VERSION}"

    # The only network step: resolves and caches the pinned CLI from the npm
    # registry. `--version` is the cheapest no-op the CLI offers (see this
    # module's docstring); this is the retried call, so a transient npm
    # hiccup here doesn't fail the whole build.
    retry_run(["npx", "-y", cli, "--version"])

    # Now local and deterministic — run once each, no retry, so a genuine
    # validate/pack failure surfaces immediately.
    run(["npx", "-y", cli, "validate", str(MCPB_DIR)])
    run(["npx", "-y", cli, "pack", str(MCPB_DIR), str(output)])

    if not output.is_file():
        print(f"::error::{output} was not produced")
        return 1
    print(f"ok   built {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
