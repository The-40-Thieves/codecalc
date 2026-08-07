#!/usr/bin/env python3
"""No machine-specific paths or private addresses in a public repo.

codecalc shipped with one developer's machine compiled into it:

  * `RUNTIME_PATH`, in BOTH the Rust binary and the Python fallback, was
    `/data/tools/mise/shims:/home/ubuntu/.local/bin:...`. On any other machine
    it resolved almost nothing, while the README promised static musl builds
    that "run on any Linux".
  * `llm.py` defaulted to `http://100.78.123.100:4001` — a LiteLLM gateway on a
    private tailnet — so `translate_code` hung and then failed for everyone else.
  * `complexity.py` carried a SECOND copy of that address, which is how this
    class of thing survives: the first one gets fixed and the duplicate does not.
  * Five test files hardcoded `/home/ubuntu/codecalc` in `sys.path`.

None of those were caught by any linter, because none of them are wrong as
Python or Rust. They are only wrong as a published artifact.

FLOOR: asserts it scanned a plausible number of files, and that its own patterns
still match a known-bad string. A regex that stops matching reports a clean
tree, which is the failure mode this repo keeps re-finding in source scanners.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCAN_SUFFIXES = {".py", ".rs", ".toml", ".md", ".yml", ".yaml", ".c", ".sh", ".json"}
SKIP_DIRS = {".git", ".venv", "target", "__pycache__", ".ruff_cache", "node_modules", "bin"}

#: AUDIT.md is the project's security record. Its corrections quote the offending
#: values verbatim — that is the point of a correction — so it is exempt.
SKIP_FILES = {"AUDIT.md", "uv.lock", "check_portability.py"}

PATTERNS = [
    (re.compile(r"/home/[a-z][a-z0-9_-]*/"), "a user's home directory"),
    (re.compile(r"/Users/[A-Za-z][A-Za-z0-9_-]*/"), "a macOS home directory"),
    # Tailscale CGNAT (100.64.0.0/10) and RFC1918. A private address in a public
    # repo is either a leak or a default that cannot work for anyone else.
    (re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"), "a tailnet address"),
    (re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"), "a private LAN address"),
    (re.compile(r"/data/tools/"), "a machine-specific toolchain root"),
]

findings: list[str] = []
scanned = 0

for path in sorted(REPO.rglob("*")):
    if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
        continue
    if set(path.relative_to(REPO).parts) & SKIP_DIRS or path.name in SKIP_FILES:
        continue
    scanned += 1
    for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        for pattern, what in PATTERNS:
            m = pattern.search(line)
            if m:
                findings.append(f"{path.relative_to(REPO)}:{lineno}: {what} — {m.group(0)}")

# ── floors ──────────────────────────────────────────────────────────────────
MIN_FILES = 25
if scanned < MIN_FILES:
    print(f"::error::scanned only {scanned} file(s) — expected >= {MIN_FILES}. "
          "The walk is broken, and an empty scan is not a clean repo.")
    sys.exit(1)

# Prove the patterns still fire. Without this, a regex broken by an edit reports
# success over a repo full of the thing it was meant to find.
CANARIES = [
    ("/home/ubuntu/.local/bin", "a user's home directory"),
    ("/Users/someone/code", "a macOS home directory"),
    ("http://100.78.123.100:4001", "a tailnet address"),
    ("http://192.168.1.10:8080", "a private LAN address"),
    ("/data/tools/mise/shims", "a machine-specific toolchain root"),
]
for canary, expected in CANARIES:
    if not any(p.search(canary) for p, what in PATTERNS if what == expected):
        print(f"::error::pattern for {expected!r} no longer matches {canary!r} — "
              "the check has been silently disarmed.")
        sys.exit(1)

if findings:
    print(f"::error::{len(findings)} machine-specific reference(s) in a public repo:")
    for f in findings:
        print(f"  {f}")
    print("\nUse an env var with a machine-neutral default (see registry.runtime_path), "
          "or a documented placeholder like /path/to/codecalc in prose.")
    sys.exit(1)

print(f"ok   {scanned} files scanned; no home directories, private addresses or "
      f"machine-specific toolchain roots ({len(CANARIES)} canaries still fire)")
