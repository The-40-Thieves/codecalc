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
  6. the post-fix suite snapshot AUDIT.md quotes
  7. the GitHub repo description, when CI supplies it with --description
  8. the CI-invoked gate-script count the README quotes (THE-842)

The last two were added after both drifted in a single day: the README's
"18 test files … 689 assertions" survived three PRs that changed both numbers,
and AUDIT.md described an 11-entry env allowlist for a day after it grew to 23 —
a security document understating what executed code is permitted to see.

Checks 6 and 7 were added after the count reached 47 and TWO surfaces stayed at
48 for a day: AUDIT.md's own post-fix snapshot, and the GitHub repo description.
Both were stale by exactly the `context7_docs` removal. The README was correct
throughout — because it was the only thing gated. A gate that covers one surface
does not make the claim true, it makes the OTHER surfaces the ones that rot.

Check 8 exists because "11 gate scripts" was UNPARSED PROSE — a number typed
once, matching nothing else in the tree, no different from "18 test files"
before check 4 existed. See that check's own comment for why the workflow-
reference definition was chosen over the two others measured.

FLOOR: each check asserts it actually found the claim in the README. A heading
reworded from "MCP tools (48)" to "Tools" would otherwise make this pass by
matching nothing.
"""

from __future__ import annotations

import argparse
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

#: The repo description lives on GitHub, not in the tree, so it is passed IN
#: rather than fetched. Every other check here is a static assertion over files
#: and must keep running on a bare checkout with no network and no token, which
#: is the property that lets ci-security gate on them without an external feed.
_parser = argparse.ArgumentParser(description="Check that stated counts match the code.")
_parser.add_argument("--description", metavar="TEXT",
                     help="GitHub repo description to check "
                          "(CI: gh repo view --json description -q .description)")
args = _parser.parse_args()

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

# The DECLARATION agreeing is not the same as the TEXT shipping. `cargo package`
# can only include files under the crate root, so the repo-root LICENSE — which
# the wheel ships via `license-files` — is not in the .crate at all unless a
# copy lives in executor/. Verified with `cargo package --list`: the crate had
# 14 files and none of them was a licence, while its metadata said Apache-2.0.
#
# The copy is what cargo requires, so the drift it creates gets a gate rather
# than a comment. Compared byte-for-byte: a licence that has been edited on one
# side only is worse than one that is merely absent, because both look present.
_root_license = (REPO / "LICENSE").read_bytes()
_crate_license_path = REPO / "executor" / "LICENSE"
if not _crate_license_path.is_file():
    fail("executor/LICENSE does not exist, so `cargo package` ships a crate whose "
         f"metadata declares {rs_license} with no licence text in it — cargo cannot "
         "include a file from outside the crate root")
elif _crate_license_path.read_bytes() != _root_license:
    fail("executor/LICENSE and the repo-root LICENSE differ — the crate and the "
         "wheel would ship different licence text under the same declared licence")
else:
    print(f"ok   license: the crate ships the licence text, byte-identical to the "
          f"repo root ({len(_root_license)} bytes)")

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
    per_file = {f.name: len(re.findall(r"^\s*check\(", f.read_text(encoding="utf-8"), re.M))
                for f in test_files}
    sites = sum(per_file.values())
    claimed = claimed_asserts[0]
    if sites == 0:
        fail("found no check() call sites — the extractor is broken")
    elif not (sites * 0.5) <= claimed <= (sites * 3):
        fail(f"README claims {claimed} assertions but tests/ has {sites} check() sites; "
             f"the number has drifted far enough to be wrong")
    else:
        print(f"ok   assertions: README claims {claimed}, {sites} check() sites make that "
              f"plausible (a band, not a measurement — scripts/count_assertions.py measures)")

    # PER-SUITE FLOOR. The band above is a statement about the TOTAL, so a suite
    # whose assertions are all deleted disappears into it: 623 sites and 624 both
    # sit comfortably inside it. That is not hypothetical here — three suites once
    # shipped printing output and exiting 0 while asserting nothing at all (PR
    # #13), and a total-shaped check could not have caught them, because a suite
    # that asserts nothing subtracts from a number that was never measured.
    #
    # This is the part of THE-776 that can run on a bare checkout. The measured
    # total cannot: no CI job runs all 21 suites (ci-python splits them across
    # `tests` and `sandbox + security`), and manufacturing one by teeing each step
    # would silently disable them, since GitHub's default shell is `bash -e` and
    # not `bash -o pipefail`. scripts/count_assertions.py does the measuring, and
    # says so.
    # Two suites legitimately assert without the check() helper: they print a
    # PASS/FAIL line per language and exit nonzero themselves. Named explicitly
    # rather than detected, so widening this is a reviewable diff — the same
    # shape as check_no_eval.py's EXEC_ALLOWED.
    #
    # "check() sites OR a nonzero exit" was tried first and is WRONG, because
    # nearly every suite ends `if FAILS: sys.exit(1)` and FAILS is only appended
    # by check(). Strip a suite's assertions and that exit survives, unable to
    # fire — a file that looks like it can fail and cannot. Verified by deleting
    # every check() from test_error_codes.py: the OR form passed it.
    HANDROLLED = {"test_smoke.py", "test_hybrid.py"}
    CAN_FAIL = re.compile(r"sys\.exit\(\s*[1-9]|raise\s+SystemExit\(\s*[1-9]")

    missing_allowed = sorted(HANDROLLED - {f.name for f in test_files})
    if missing_allowed:
        fail(f"the hand-rolled allowlist names {', '.join(missing_allowed)}, which no longer "
             f"exist(s) under tests/. An allowlist entry for a missing file silently widens "
             f"the policy.")

    silent = []
    for f in test_files:
        if f.name in HANDROLLED:
            if not CAN_FAIL.search(f.read_text(encoding="utf-8")):
                silent.append(f"{f.name} (hand-rolled, but has no nonzero exit)")
        elif per_file[f.name] == 0:
            silent.append(f"{f.name} (no check() sites)")
    if silent:
        fail(f"{len(silent)} test file(s) can never fail the build: {', '.join(sorted(silent))}. "
             f"Every assertion in them could go red and the suite would still report success — "
             f"the shape PR #13 removed from three other suites.")
    else:
        print(f"ok   all {len(per_file)} test files can fail the build "
              f"({len(HANDROLLED)} hand-rolled, checked for a nonzero exit instead)")

# ── 4b. CI-invoked script count (THE-842) ───────────────────────────────────
# The README said "11 gate scripts", and nothing tied that number to any
# property of the repo — the same shape as the test-file/assertion counts
# above before check 4 existed. Three ways to derive "gate script" were
# measured before picking one:
#
#   every *.py under scripts/                          -> 13
#     Rejected: includes count_assertions.py (a MEASURING tool this file's
#     own comment above distinguishes from a gate) and diag_windows_job.py
#     (a CI job helper, not a check).
#   files named check_*.py                              -> 9
#     Rejected: excludes contract_check.py, which IS invoked as a gate in
#     ci-rust.yml and release.yml — it is simply named check LAST — and would
#     silently drop it (or any future gate) from the count for a naming
#     accident rather than a behavioural one.
#   referenced by `scripts/[a-z_]+\.py` in a workflow    -> 11  (chosen)
#     A REPO PROPERTY rather than a naming convention: "does CI actually run
#     this script." A script renamed out of the check_* pattern but still
#     wired into a workflow keeps counting; one deleted from every workflow
#     stops, in both cases without touching this file. It also happens to be
#     the number the README already carried — which is exactly how "11" went
#     unnoticed as prose for as long as it did: it was RIGHT, and nothing was
#     gating it, so a future drift in either direction would have looked the
#     same as this one did.
#
# FLOOR, both directions: zero references means the extractor broke (a
# workflow rewrite that stopped naming scripts by path would otherwise pass
# silently), and an absent README claim means this proves nothing rather
# than passing by matching nothing — same shape as every other check here.
WORKFLOWS_DIR = REPO / ".github" / "workflows"
_workflow_text = "\n".join(
    p.read_text(encoding="utf-8") for p in sorted(WORKFLOWS_DIR.glob("*.yml")))
ci_scripts = set(re.findall(r"scripts/[a-z_]+\.py", _workflow_text))
if not ci_scripts:
    fail("found zero 'scripts/<name>.py' references across "
         ".github/workflows/*.yml — the extractor is broken")
else:
    claimed_gates = [int(n) for n in re.findall(r"(\d+)\s+CI-invoked scripts", README)]
    if not claimed_gates:
        fail("no 'N CI-invoked scripts' claim found in README.md — the check "
             "matched nothing")
    elif any(c != len(ci_scripts) for c in claimed_gates):
        fail(f"README claims {sorted(set(claimed_gates))} CI-invoked scripts; "
             f"{len(ci_scripts)} distinct scripts/*.py paths are referenced by "
             f".github/workflows/*.yml: {sorted(ci_scripts)}")
    else:
        print(f"ok   CI-invoked scripts: README and the workflows agree on "
              f"{len(ci_scripts)} ({sorted(ci_scripts)})")

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

# ── 6. AUDIT.md's own suite snapshot ────────────────────────────────────────
# The README is not the only document quoting a tool count. AUDIT.md carries a
# post-fix snapshot of what the suite reported, and it read `48/48 tools` while
# server.py defined 47 — stale by exactly the context7_docs removal, on the same
# day and for the same reason as the repo description. A security document
# overstating its own coverage is the mirror of the env-allowlist drift above:
# there the document understated what executed code could see, here it
# overstated what the suite checked.
snapshot = re.findall(r"test_mcp_all\.py\s*→\s*(\d+)\s*/\s*(\d+)\s+tools", AUDIT)
if not snapshot:
    fail("no 'test_mcp_all.py → N/N tools' snapshot found in AUDIT.md — "
         "the check matched nothing")
else:
    wrong = [(a, b) for a, b in snapshot if int(a) != actual_tools or int(b) != actual_tools]
    if wrong:
        fail(f"AUDIT.md snapshot says {wrong[0][0]}/{wrong[0][1]} tools; "
             f"server.py defines {actual_tools}")
    else:
        print(f"ok   AUDIT.md snapshot: {actual_tools}/{actual_tools} tools matches server.py")

# ── 7. the GitHub repo description ──────────────────────────────────────────
# The single most-read text in the project, rendered on every search result, and
# the one surface no file in this tree can see. It claimed 48 tools for a day
# after the count became 47. Supplied rather than fetched (see `args` above), so
# the COMPARISON lives here with the others while the network stays in CI.
if args.description is not None:
    _before = len(failures)
    d_tools = [int(n) for n in re.findall(r"(\d+)\s+MCP tools", args.description)]
    d_langs = [int(n) for n in re.findall(r"(\d+)\s+languages", args.description)]
    if not d_tools and not d_langs:
        fail("--description was supplied but names no tool or language count — "
             "it was reworded, or the wrong text was passed; either way this "
             "check is no longer reading what it claims to read")
    else:
        if any(c != actual_tools for c in d_tools):
            fail(f"repo description claims {sorted(set(d_tools))} MCP tools; "
                 f"server.py defines {actual_tools}")
        if any(c != actual_langs for c in d_langs):
            fail(f"repo description claims {sorted(set(d_langs))} languages; "
                 f"registry defines {actual_langs}")
    if len(failures) == _before:
        print(f"ok   repo description: agrees on {actual_tools} tools "
              f"and {actual_langs} languages")

# ── "Typing :: Typed" is a PEP 561 promise, and needs the marker ───────────
# #111 added the classifier and no py.typed file. The classifier IS the claim
# "this package ships usable type information"; without the marker, mypy and
# pyright ignore every annotation in it, so the package has 196 annotated defs
# that no consumer's type checker will look at. Found by a cross-vendor review
# of #111 — a false claim introduced by the PR whose subject was metadata
# accuracy, which is worth recording rather than quietly fixing.
#
# Gated in BOTH directions: the classifier without the marker is a promise that
# is not kept, and the marker without the classifier is type information nobody
# is told about.
_pyproject_txt = (REPO / "pyproject.toml").read_text(encoding="utf-8")
_typed_classifier = "Typing :: Typed" in _pyproject_txt
_typed_marker = (REPO / "codecalc" / "py.typed").is_file()
if _typed_classifier and not _typed_marker:
    fail("pyproject declares the 'Typing :: Typed' classifier but codecalc/py.typed "
         "does not exist — PEP 561 requires the marker, and without it every type "
         "checker ignores the package's annotations while the metadata advertises them")
elif _typed_marker and not _typed_classifier:
    fail("codecalc/py.typed exists but pyproject does not declare 'Typing :: Typed' — "
         "the package ships type information and does not say so")
else:
    print(f"ok   PEP 561: classifier and py.typed marker agree "
          f"({'both present' if _typed_marker else 'both absent'})")


# ── SECURITY.md carries numbers too, and nothing read it ───────────────────
# Gate 5 above requires AUDIT.md to NAME every allowed environment variable.
# SECURITY.md states the same allowlist as a COUNT — "the 23-entry allowlist" —
# in its threat model, and until this gate no script read the file at all.
#
# That is the wrong way round. SECURITY.md is the document a reader reaches
# first, it is linked from the PyPI page (#111), and a threat model that
# understates what executed code can see is worse than an audit appendix that
# does. The comment at the top of this file already records AUDIT.md carrying
# an 11-entry claim for a day after the allowlist grew to 23 — same class of
# drift, one file over, and that file was the one being watched.
#
# Counts only. The prose is not gated and should not be: a check that tried to
# validate the threat model's ARGUMENT would be a check nobody could keep
# green. These are the two numbers in it that a code change can silently
# falsify.
SECURITY = (REPO / "SECURITY.md").read_text(encoding="utf-8")

_sec_allow = [int(n) for n in re.findall(r"(\d+)-entry allowlist", SECURITY)]
if not _sec_allow:
    fail("SECURITY.md no longer states an 'N-entry allowlist' — the extractor "
         "matched nothing, so this gate proves nothing rather than passing")
elif any(n != len(allow) for n in _sec_allow):
    fail(f"SECURITY.md claims {sorted(set(_sec_allow))} allowed env vars; the "
         f"executor permits {len(allow)}. A threat model that understates what "
         "executed code can see is a security defect in the document")
else:
    print(f"ok   SECURITY.md: allowlist count matches the code ({len(allow)})")

_sec_langs = [int(n) for n in re.findall(r"(\d+) languages", SECURITY)]
if not _sec_langs:
    fail("SECURITY.md no longer states 'N languages' — the extractor matched "
         "nothing, so this gate proves nothing rather than passing")
elif any(n != actual_langs for n in _sec_langs):
    fail(f"SECURITY.md claims {sorted(set(_sec_langs))} languages; the registry "
         f"defines {actual_langs}")
else:
    print(f"ok   SECURITY.md: language count matches the registry ({actual_langs})")


# ── the shipped skill may only name things that exist ─────────────────────
# codecalc/SKILL.md tells a model which tools to call and which result fields
# to relay. It is prose about code, which is the same drift risk as the
# README's counts — and it was the one surface here nothing checked.
#
# This gate was written once before, watched failing in both directions, and
# then destroyed by `git checkout scripts/check_claims.py` at the end of a
# mutation test — which reverts to HEAD, not to the previous edit. The skill
# shipped claiming to be gated by a gate that no longer existed. That is the
# defect this file exists to prevent, committed by this file's own author, so
# it is worth naming here rather than in a changelog.
#
# Two directions, because they fail differently. A tool name that does not
# exist sends a model looking for something it cannot call. A field that does
# not exist tells it to relay something no tool returns, which reads as a
# promise the output silently breaks.
SKILL = (REPO / "codecalc" / "SKILL.md").read_text(encoding="utf-8")
SERVER_SRC = (REPO / "codecalc" / "server.py").read_text(encoding="utf-8")

_skill_tools = set(re.findall(r"`([a-z][a-z0-9_]+)`", SKILL))
_declared_tools = set(re.findall(r"^def ([a-z][a-z0-9_]+)\(", SERVER_SRC, re.M))
#: Backticked words that are fields, values or prose rather than tool names.
#: Explicit so widening it is a reviewable diff.
_NOT_TOOLS = {
    "passed", "total", "inconclusive", "unenforced", "backend", "analysis",
    "method", "output_error", "ok", "error", "stdout", "stderr", "no_net",
    "peak_memory_kb", "stored", "mismatched", "identical", "divergences",
    "static-estimate", "empirical", "regex-fallback", "python", "true", "false",
    "int_widths", "DEFAULT_EDGE_INPUTS", "algebraic_equiv",
}
_named_but_absent = sorted(_skill_tools - _declared_tools - _NOT_TOOLS)
if _named_but_absent:
    fail(f"SKILL.md names tools that server.py does not define: {_named_but_absent}")
else:
    print(f"ok   SKILL.md: every tool it names is served ({len(_skill_tools & _declared_tools)} checked)")

_RELAY_FIELDS = ["passed", "total", "inconclusive", "unenforced", "backend",
                 "analysis", "method", "output_error"]
_pkg = "\n".join(f.read_text(encoding="utf-8") for f in (REPO / "codecalc").glob("*.py"))
_unproduced = [f for f in _RELAY_FIELDS if f'"{f}"' not in _pkg]
if _unproduced:
    fail(f"SKILL.md tells a model to relay fields no tool returns: {_unproduced}")
else:
    print(f"ok   SKILL.md: all {len(_RELAY_FIELDS)} relay fields are produced by the code")

if failures:
    print(f"\n=== {len(failures)} claim(s) out of date ===")
    sys.exit(1)
print("\n=== README claims match the code ===")
