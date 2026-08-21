#!/usr/bin/env python3
"""CHANGELOG gate: caller-facing source changed, `[Unreleased]` did not.

CONTRIBUTING.md's "Changelog entries" section says a change a caller can
observe needs a line in `CHANGELOG.md` in the same PR. Nothing enforced that:
several past PRs each merged caller-observable changes — new CLI flags, a new
console script, error-code reclassifications — with zero changelog lines, and
the 0.3.2 release had to reconstruct the notes from the commit log after the
fact instead of from anything written at review time.

WHAT THIS IS, HONESTLY: a reminder with an opt-out, not a classifier of
"caller-observable". It cannot tell a public API change from a private
helper's docstring tweak — both are `codecalc/*.py`, and a change to a
comment inside a caller-facing module still trips it. The escape hatch below
exists precisely because that false-positive rate is not zero; tighten the
trigger set only against a real false positive in hand, not in anticipation
of one.

TRIGGER: any changed file matching `codecalc/**.py`. `tests/` and `scripts/`
are not under `codecalc/`, so the glob already excludes them; both are named
explicitly wherever this file talks about the trigger, so a reader does not
have to infer the exclusion from a directory layout.

WHEN TRIGGERED: `CHANGELOG.md`'s `[Unreleased]` section — the text between
`## [Unreleased]` and the next `## [` heading — must have GAINED content
between `<base>` and `HEAD`: a line present at HEAD and absent at base, or
(failing that) simply longer non-whitespace text, so an edit that rewrites a
bullet without adding a new line still counts. A cut or a reorder with
nothing new does not.

ESCAPE HATCH: a `Skip-Changelog: <reason>` trailer (same shape as
`Signed-off-by:`) on any commit in `<base>..HEAD` passes the gate
unconditionally, and the failure message says so. Read with git's own
trailer parser (`%(trailers:key=Skip-Changelog,valueonly)`,
pretty-formats(7)) rather than a substring search over the raw message — a
first cut of this gate grepped the message body for the literal phrase and a
commit that merely QUOTED or DISCUSSED it (a policy conversation, a review
response, this gate's own PR narrating its escape hatch) accidentally
tripped the opt-out. git only recognises a trailing block of `Key: value`
lines as trailers, so a body paragraph that mentions 'Skip-Changelog' in
prose does not qualify; only a real trailer does. Git-only — no GitHub API,
no label, so it works from a bare checkout with no token, and this script
never makes a network call.

STDLIB ONLY — this runs before dependencies exist.

FLOOR: the `[Unreleased]` extractor must find the section heading at BOTH
refs before anything is compared. An absent section on both sides means "the
extractor found nothing", not "nothing changed" — a missing `CHANGELOG.md`,
or a renamed heading, fails the gate instead of comparing two empty strings
and reporting agreement.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHANGELOG_NAME = "CHANGELOG.md"

DEFAULT_BASE_REF = "origin/main"
TRAILER_KEY = "Skip-Changelog"

failures: list[str] = []


def fail(msg: str) -> None:
    print(f"FAIL {msg}")
    failures.append(msg)


def ok(msg: str) -> None:
    print(f"ok   {msg}")


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print(f"::error::`git {' '.join(args)}` failed: {result.stderr.strip()}")
        sys.exit(1)
    return result.stdout


def read_at(ref: str, rel_path: str) -> str | None:
    """`rel_path` as it existed at `ref`, or None if it did not exist there."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{rel_path}"], cwd=REPO,
        capture_output=True, text=True, check=False,
    )
    return result.stdout if result.returncode == 0 else None


def is_caller_facing(rel_path: str) -> bool:
    """`codecalc/**.py` — see TRIGGER in the module docstring."""
    parts = Path(rel_path).parts
    return len(parts) >= 2 and parts[0] == "codecalc" and rel_path.endswith(".py")


def unreleased_section(text: str) -> str | None:
    """Text between `## [Unreleased]` and the next `## [` heading.

    None if the `## [Unreleased]` heading itself is absent — the FLOOR in the
    module docstring depends on this distinction from "" (heading present,
    section empty).
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "## [Unreleased]":
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("## ["):
            end = i
            break
    return "\n".join(lines[start:end]).strip()


def skip_changelog_reason(base_ref: str) -> str | None:
    """The value of a `Skip-Changelog:` trailer on any commit in
    `<base>..HEAD`, or None if no commit carries one with a non-empty value.

    Uses git's own trailer parser rather than grepping the raw message for a
    phrase — see ESCAPE HATCH in the module docstring for why that
    distinction is the whole point. `%(trailers:key=...,valueonly)` only
    returns a line for a commit whose FINAL paragraph is itself shaped like a
    trailer block; a mid-message sentence that happens to contain the key
    name is invisible to it.
    """
    out = run_git("log", f"--format=%(trailers:key={TRAILER_KEY},valueonly)", f"{base_ref}..HEAD")
    for line in out.splitlines():
        value = line.strip()
        if value:
            return value
    return None


def gained_content(base_text: str, head_text: str) -> bool:
    """HEAD's `[Unreleased]` section is a strict superset / longer / carries a
    new bullet, relative to base's. See WHEN TRIGGERED above."""
    base_lines = {line.strip() for line in base_text.splitlines() if line.strip()}
    head_lines = {line.strip() for line in head_text.splitlines() if line.strip()}
    if head_lines - base_lines:
        return True
    return len(head_text) > len(base_text) and head_text != base_text


base_ref = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CHANGELOG_GATE_BASE_REF", DEFAULT_BASE_REF)

changed = [line for line in run_git("diff", "--name-only", f"{base_ref}...HEAD").splitlines() if line]
triggering = sorted(f for f in changed if is_caller_facing(f))

if not triggering:
    ok(f"{len(changed)} file(s) changed against {base_ref}, none under codecalc/**.py — not triggered")
    print("\n=== changelog gate: not triggered ===")
    sys.exit(0)

print(f"note {len(triggering)} caller-facing file(s) changed against {base_ref}:")
for f in triggering:
    print(f"       {f}")

skip_reason = skip_changelog_reason(base_ref)
if skip_reason is not None:
    ok(f"'{TRAILER_KEY}: {skip_reason}' trailer found on a commit in {base_ref}..HEAD — opt-out honoured")
    print("\n=== changelog gate: escape hatch used ===")
    sys.exit(0)

base_changelog = read_at(base_ref, CHANGELOG_NAME)
head_changelog_path = REPO / CHANGELOG_NAME
head_changelog = head_changelog_path.read_text(encoding="utf-8") if head_changelog_path.is_file() else None

if base_changelog is None:
    fail(f"{CHANGELOG_NAME} does not exist at {base_ref}")
if head_changelog is None:
    fail(f"{CHANGELOG_NAME} does not exist at HEAD")

if failures:
    print(f"\n=== {len(failures)} CHANGELOG FAILURE(S) ===")
    sys.exit(1)

base_section = unreleased_section(base_changelog)  # type: ignore[arg-type]
head_section = unreleased_section(head_changelog)  # type: ignore[arg-type]

# ── FLOOR: the extractor must have found the section at BOTH refs before ────
# anything is compared. Two `None`s would otherwise compare equal and read as
# "no change needed" for a repository whose CHANGELOG.md lost its heading.
if base_section is None:
    fail(f"no '## [Unreleased]' heading found in {CHANGELOG_NAME} at {base_ref} — "
         "the extractor found nothing to compare, which is not the same as agreeing")
if head_section is None:
    fail(f"no '## [Unreleased]' heading found in {CHANGELOG_NAME} at HEAD — "
         "the extractor found nothing to compare, which is not the same as agreeing")

if failures:
    print(f"\n=== {len(failures)} CHANGELOG FAILURE(S) ===")
    sys.exit(1)

if gained_content(base_section, head_section):  # type: ignore[arg-type]
    ok(f"[Unreleased] gained content between {base_ref} and HEAD "
       f"({len(base_section)} -> {len(head_section)} chars)")  # type: ignore[arg-type]
    print("\n=== changelog gate: [Unreleased] updated ===")
    sys.exit(0)

fail(f"{len(triggering)} caller-facing file(s) changed under codecalc/**.py but "
     f"CHANGELOG.md's [Unreleased] section did not gain any content between "
     f"{base_ref} and HEAD.")
print(f"       Add a bullet under '## [Unreleased]' describing what a caller would "
      f"observe, or add a '{TRAILER_KEY}: <reason>' trailer to a commit if this change "
      f"is not caller-observable (internal refactor, test-only, comment fix — see "
      f"CONTRIBUTING.md's 'Changelog entries' section).")
print(f"\n=== {len(failures)} CHANGELOG FAILURE(S) ===")
sys.exit(1)
