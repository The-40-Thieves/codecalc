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
  4. the test-file and assertion counts the README quotes
  5. the size of the env allowlist AUDIT.md describes

The last two were added after both drifted in a single day: the README's
"18 test files … 689 assertions" survived three PRs that changed both numbers,
and AUDIT.md described an 11-entry env allowlist for a day after it grew to 23 —
a security document understating what executed code is permitted to see.

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

from codecalc import executor as executor_mod  # noqa: E402 — needs the path above
from codecalc import registry  # noqa: E402

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

# ── 4. test-file and assertion counts ───────────────────────────────────────
# The assertion total is what the suite PRINTS, so it is recounted here rather
# than trusted: a `check()` that stops running still exists in the source.
test_files = sorted((REPO / "tests").glob("test_*.py"))
claimed_files = [int(n) for n in re.findall(r"(\d+) test files", README)]
if not claimed_files:
    fail("no 'N test files' claim found in README.md — the check matched nothing")
elif any(c != len(test_files) for c in claimed_files):
    fail(f"README claims {sorted(set(claimed_files))} test files; tests/ holds {len(test_files)}")
else:
    print(f"ok   test files: README and tests/ agree on {len(test_files)}")

claimed_asserts = [int(n) for n in re.findall(r"\*\*(\d+) assertions\*\*", README)]
if not claimed_asserts:
    fail("no '**N assertions**' claim found in README.md — the check matched nothing")
else:
    # Count `check(` call sites as the floor. An exact runtime count would mean
    # running the whole suite from a lint gate, which is the wrong place for it;
    # this catches the case that actually happened — the number left far behind.
    sites = sum(len(re.findall(r"^\s*check\(", f.read_text(encoding="utf-8"), re.M))
                for f in test_files)
    claimed = claimed_asserts[0]
    if sites == 0:
        fail("found no check() call sites — the extractor is broken")
    elif not (sites * 0.5) <= claimed <= (sites * 3):
        fail(f"README claims {claimed} assertions but tests/ has {sites} check() sites; "
             f"the number has drifted far enough to be wrong")
    else:
        print(f"ok   assertions: README claims {claimed}, {sites} check() sites back it")

# ── 5. env allowlist size ───────────────────────────────────────────────────
AUDIT = (REPO / "AUDIT.md").read_text(encoding="utf-8")
allow = executor_mod._ENV_ALLOWLIST
# Tokenise every inline-code SPAN, after removing FENCED blocks.
#
# Two wrong versions preceded this, both failing a correct document — the same
# defect the gate exists to catch, pointed the other way:
#   1. requiring a backtick immediately before each name, which missed the
#      comma-separated lists the document actually uses;
#   2. pairing backticks across the whole file, which ``` fences break — each
#      fence is THREE backticks, so every pairing after the first fence is off
#      by one and the "spans" become the gaps between real spans. `PATH`
#      resolved and `CARGO_HOME`, two lines later in the same span, did not.
_fenceless = re.sub(r"```.*?```", "", AUDIT, flags=re.S)
spans = re.findall(r"`([^`]+)`", _fenceless)
named = {tok for span in spans for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", span)} & allow
missing = allow - named
if len(allow) < 5:
    fail(f"env allowlist holds only {len(allow)} entries — the extractor is broken")
elif missing:
    fail(f"AUDIT.md does not name {len(missing)} allowed env var(s): {sorted(missing)} — "
         f"a security document must not understate what executed code can see")
else:
    print(f"ok   env allowlist: AUDIT.md names all {len(allow)} permitted variables")

if failures:
    print(f"\n=== {len(failures)} claim(s) out of date ===")
    sys.exit(1)
print("\n=== README claims match the code ===")
