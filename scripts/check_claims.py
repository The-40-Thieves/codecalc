#!/usr/bin/env python3
"""README numbers must match the code they describe.

The README is the only thing most readers of a public repo will check, and its
headline claims are counts: "48 MCP tools", "31 languages". Counts drift the
moment a tool is added, and nothing notices — the repo shipped with README,
AUDIT.md and the registry stating 31, 33 and 32 languages respectively, all in
the same commit.

Checked here:
  1. the `@mcp.tool` count matches every "N MCP tools"/"N tools" claim
  2. the language count matches the registry, after collapsing the alias
     entries the README itself writes as one item
  3. the crate's declared license matches the project license

FLOOR: each check asserts it actually found the claim in the README. A heading
reworded from "MCP tools (48)" to "Tools" would otherwise make this pass by
matching nothing.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from codecalc import registry  # noqa: E402 — needs the path above

README = (REPO / "README.md").read_text(encoding="utf-8")
SERVER = (REPO / "codecalc" / "server.py").read_text(encoding="utf-8")

#: Registry keys that are aliases of another entry, which the README lists as a
#: single item ("cpp/c++"). Kept explicit so widening it is a reviewable diff
#: rather than a silent adjustment to make the number come out right.
ALIAS_ENTRIES = {"c++"}

failures: list[str] = []


def fail(msg: str) -> None:
    print(f"FAIL {msg}")
    failures.append(msg)


# ── 1. tool count ───────────────────────────────────────────────────────────
actual_tools = len(re.findall(r"^@mcp\.tool\b", SERVER, re.M))
if actual_tools < 10:
    fail(f"found only {actual_tools} @mcp.tool decorators — the extractor is broken")
else:
    claims = [int(n) for n in re.findall(r"(\d+)\s+MCP tools", README)]
    claims += [int(n) for n in re.findall(r"MCP tools \((\d+)\)", README)]
    if not claims:
        fail("no 'N MCP tools' claim found in README.md — the check matched nothing")
    elif any(c != actual_tools for c in claims):
        fail(f"README claims {sorted(set(claims))} MCP tools; server.py defines {actual_tools}")
    else:
        print(f"ok   MCP tools: README and server.py agree on {actual_tools}")

# ── 2. language count ───────────────────────────────────────────────────────
actual_langs = len(set(registry.LANGUAGES) - ALIAS_ENTRIES)
if actual_langs < 10:
    fail(f"registry holds only {actual_langs} languages — the extractor is broken")
else:
    claims = [int(n) for n in re.findall(r"(\d+)\s+languages", README)]
    claims += [int(n) for n in re.findall(r"(\d+)\s+runtimes", README)]
    if not claims:
        fail("no 'N languages' claim found in README.md — the check matched nothing")
    elif any(c != actual_langs for c in claims):
        fail(f"README claims {sorted(set(claims))} languages; registry defines "
             f"{actual_langs} (excluding alias entries {sorted(ALIAS_ENTRIES)})")
    else:
        print(f"ok   languages: README and registry agree on {actual_langs}")

# ── 3. license coherence ────────────────────────────────────────────────────
# The crate shipped `license = "MIT"` while the repo LICENSE and pyproject were
# Apache-2.0. A compiled artifact distributed under the wrong licence is the
# kind of thing found at the worst possible moment.
py_license = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["license"]
cargo = tomllib.loads((REPO / "executor" / "Cargo.toml").read_text(encoding="utf-8"))
rs_license = cargo["package"].get("license", "")
if not rs_license:
    fail("executor/Cargo.toml declares no license")
elif rs_license != py_license:
    fail(f"license mismatch — pyproject.toml: {py_license!r}, executor/Cargo.toml: {rs_license!r}")
else:
    print(f"ok   license: pyproject and Cargo.toml agree on {py_license}")

if failures:
    print(f"\n=== {len(failures)} claim(s) out of date ===")
    sys.exit(1)
print("\n=== README claims match the code ===")
