#!/usr/bin/env python3
"""One version, in every place that declares one.

`check_claims.py` gates LICENCE coherence across `LICENSE`, `pyproject.toml` and
`executor/Cargo.toml`. It never gated VERSION, and both manifests are edited by
hand.

Why that matters here more than in most repos: `release.yml` builds the wheel
AND the crate from one dispatch, off one commit, and its own comment says the
reason is that "the two registries cannot end up carrying different code under
the same version". Two manifests with two different numbers defeat that in the
one place where it cannot be taken back — a version, once uploaded to PyPI, is
burned even if the file is deleted.

This is the same defect one level up: a value that lives in more places than
the gate can reach. That one shipped a repo description reading "48 tools" for a
day while the README correctly said 47.

WHAT IS COMPARED
  pyproject.toml        [project] version
  executor/Cargo.toml   [package] version
  CHANGELOG.md          the newest `## [x.y.z]` heading
  README.md             "Published as **`codecalc` X.Y.Z**" and
                         "**`codecalc-exec` X.Y.Z**" (this drifted
                         to 0.2.0 while pyproject.toml had already moved to
                         0.3.1, because nothing compared the README's own
                         install instructions against the version they claim
                         to install)
  docker/mcp-server.Dockerfile  `ARG CODECALC_VERSION=` (the image
                        pinned 0.3.1 while the manifests had moved to 0.3.2, so
                        it would `pip install codecalc==0.3.1` on a 0.3.2 cut)
  the git tag           only when running on a tag (CI sets GITHUB_REF)

The changelog is included because a release whose notes describe a different
version than it ships is the same failure wearing a friendlier face, and because
`CHANGELOG.md` is the one of the four a human actually reads.

STDLIB ONLY. Gates here run on a bare checkout, before dependencies exist; a
TOML parser is available from 3.11 as `tomllib`, and the Cargo file is read with
a narrow regex rather than a dependency.

FLOOR: every extractor asserts it found something before anything is compared.
Two absent versions compare equal, and "" == "" is a green check for a scan that
found nothing — the failure mode this repo keeps rediscovering.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"
CARGO = REPO / "executor" / "Cargo.toml"
CHANGELOG = REPO / "CHANGELOG.md"
README = REPO / "README.md"
DOCKERFILE = REPO / "docker" / "mcp-server.Dockerfile"

#: A version has to look like one. This is the floor against an extractor that
#: silently matches an empty string: `""` would satisfy "they are all equal".
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"FAIL {msg}")


def ok(msg: str) -> None:
    print(f"ok   {msg}")


def pyproject_version() -> str | None:
    try:
        return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        fail(f"could not read [project] version from {PYPROJECT.name}: {exc}")
        return None


def cargo_version() -> str | None:
    """The `[package]` version, and ONLY that one.

    Read with a section-anchored regex rather than by parsing the whole file:
    `Cargo.toml` carries `version = "..."` under `[dependencies]` entries too,
    and a naive first-match would return serde's version and compare it against
    the package's. That is not a hypothetical — it is the ordinary shape of a
    Cargo manifest.
    """
    try:
        text = CARGO.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"could not read {CARGO}: {exc}")
        return None
    section = re.search(r"^\[package\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    if not section:
        fail(f"{CARGO.name} has no [package] section — the extractor is broken")
        return None
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', section.group(1), re.M)
    if not m:
        fail(f"{CARGO.name}'s [package] section declares no version")
        return None
    return m.group(1)


def changelog_version() -> str | None:
    """The newest `## [x.y.z]` heading. `## [Unreleased]` is skipped."""
    try:
        text = CHANGELOG.read_text(encoding="utf-8")
    except OSError:
        fail(f"{CHANGELOG.name} does not exist — a release with no notes is a defect")
        return None
    for m in re.finditer(r"^##\s*\[([^\]]+)\]", text, re.M):
        candidate = m.group(1).strip()
        if SEMVER.match(candidate):
            return candidate
    fail(f"{CHANGELOG.name} has no `## [x.y.z]` heading — the extractor found "
         "no version to compare, which is not the same as agreeing")
    return None


def tag_version() -> str | None:
    """`vX.Y.Z` from GITHUB_REF, when running on a tag. None otherwise.

    Included because `release.yml` triggers on `push: tags: ['v*']`, so a tag
    that disagrees with the manifests is the same defect at the one moment it
    becomes permanent.
    """
    ref = os.environ.get("GITHUB_REF", "")
    if not ref.startswith("refs/tags/"):
        return None
    return ref.removeprefix("refs/tags/").lstrip("v")


def init_version() -> str | None:
    """The `__version__` in `codecalc/__init__.py`, exposed as
    `codecalc.__version__`. A SEPARATE hardcode from pyproject's, so it drifts
    silently unless gated — and it did: it sat at `0.1.0` through the `0.2.0` cut,
    which is exactly the class of miss this gate exists to catch."""
    init = REPO / "codecalc" / "__init__.py"
    try:
        text = init.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"could not read codecalc/__init__.py: {exc}")
        return None
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        fail("codecalc/__init__.py declares no __version__")
        return None
    return m.group(1)


def readme_versions() -> tuple[str | None, str | None]:
    """The Install section's "Published as **`codecalc` X.Y.Z**" note and its
    "**`codecalc-exec` X.Y.Z**" companion — the two version numbers a reader
    who follows README.md's own install instructions is trusting.

    this drifted to 0.2.0 while pyproject.toml had already moved to
    0.3.1, and nothing compared the README's claim against the version it
    actually describes. Read as a pair, not merged into one regex, because a
    fix that only updates one of the two names would still leave the other
    silently wrong.
    """
    try:
        text = README.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"could not read {README}: {exc}")
        return None, None
    pkg = re.search(r"Published as \*\*`codecalc`\s+([0-9][^\s*]*)\*\*", text)
    exe = re.search(r"\*\*`codecalc-exec`\s+([0-9][^\s*]*)\*\*", text)
    if not pkg:
        fail(f"{README.name} has no 'Published as **`codecalc` X.Y.Z**' claim — "
             "the extractor matched nothing")
    if not exe:
        fail(f"{README.name} has no '**`codecalc-exec` X.Y.Z**' claim — "
             "the extractor matched nothing")
    return (pkg.group(1) if pkg else None, exe.group(1) if exe else None)


def dockerfile_version() -> str | None:
    """The `ARG CODECALC_VERSION=X.Y.Z` the MCP-server image installs.

    It drifted to 0.3.1 while the six manifests above moved to 0.3.2 — the
    image would `pip install codecalc==0.3.1` on a 0.3.2 release, because
    nothing compared the Dockerfile's pinned version against the rest. It is
    part of the release surface a user installs from, so it is gated with the
    others rather than left to drift until a report."""
    try:
        text = DOCKERFILE.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"could not read {DOCKERFILE}: {exc}")
        return None
    m = re.search(r"^ARG\s+CODECALC_VERSION=(\S+)", text, re.M)
    if not m:
        fail(f"{DOCKERFILE.name} declares no `ARG CODECALC_VERSION=` — "
             "the extractor matched nothing")
        return None
    return m.group(1)


readme_pkg_version, readme_exec_version = readme_versions()

sources: dict[str, str | None] = {
    "pyproject.toml": pyproject_version(),
    "executor/Cargo.toml": cargo_version(),
    "codecalc/__init__.py": init_version(),
    "CHANGELOG.md": changelog_version(),
    "README.md (codecalc)": readme_pkg_version,
    "README.md (codecalc-exec)": readme_exec_version,
    "docker/mcp-server.Dockerfile": dockerfile_version(),
}
tag = tag_version()
if tag is not None:
    sources["git tag"] = tag
else:
    print("note not running on a tag; the tag/manifest comparison is skipped "
          "(GITHUB_REF is not refs/tags/*)")

# ── the floor, before any comparison ────────────────────────────────────────
missing = sorted(name for name, value in sources.items() if not value)
if missing:
    fail(f"no version extracted from {missing} — refusing to compare, because "
         "absent values compare EQUAL and would report agreement between two "
         "things neither of which was read")
else:
    malformed = sorted(f"{n}={v!r}" for n, v in sources.items()
                       if not SEMVER.match(str(v)))
    if malformed:
        fail(f"not a version: {malformed}")
    else:
        ok(f"extracted {len(sources)} version(s): "
           + ", ".join(f"{n}={v}" for n, v in sources.items()))

        distinct = sorted({str(v) for v in sources.values()})
        if len(distinct) != 1:
            detail = ", ".join(f"{n}={v}" for n, v in sources.items())
            fail(f"versions disagree: {detail}. One dispatch publishes the wheel "
                 "and the crate together so the two registries cannot carry "
                 "different code under the same version; two numbers defeat "
                 "that at the one point it cannot be undone.")
        else:
            ok(f"every declared version agrees on {distinct[0]}")

if failures:
    print(f"\n=== {len(failures)} VERSION FAILURE(S) ===")
    sys.exit(1)
print("\n=== one version, everywhere it is declared ===")
