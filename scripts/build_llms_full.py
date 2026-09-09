#!/usr/bin/env python3
"""Build llms-full.txt: the full text of every doc llms.txt links, concatenated.

llms.txt (the llmstxt.org convention) is a curated table of contents — titles,
one-line descriptions, and links. It deliberately does not carry the content
itself, so a client with a small context window can read it cheaply. A client
with room for more gets `llms-full.txt`: the actual text of every linked doc,
in the same order, so it does not have to fetch each URL itself.

That means llms-full.txt is *derived*, not authored — it says nothing that
llms.txt and the files under it do not already say. A hand-maintained copy
would drift the moment any linked file changed, which is exactly the class of
staleness CONTRIBUTING.md's "rule that matters most" exists to catch.

It is NOT committed to the repository. A generated file whose content is a
function of every doc it links would need regenerating (and re-verifying
byte-for-byte) on every unrelated doc PR, which is a worse trade than the
convenience of having it in the tree — the same reasoning
CONTRIBUTING.md's SECURITY.md/AUDIT.md counts already trades the other way,
except there the source of truth is small and stable and here it is the
whole doc set. Instead it is built fresh in CI and attached to each GitHub
release (`.github/workflows/release.yml`, `release-assets` job, next to the
SBOM); `scripts/check_llms_txt.py` proves this script still runs cleanly and
that everything llms.txt links still exists, without needing a committed
copy to diff against. Regenerate a local copy with:

    uv run python scripts/build_llms_full.py
    uv run python scripts/build_llms_full.py --output /tmp/llms-full.txt

FLOOR: refuses to write an empty or suspiciously small file. A parser that
silently matched zero links would otherwise happily "build" a one-line file
and exit 0.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LLMS_TXT = REPO / "llms.txt"
#: Default target for a local, manual regen. NOT committed — see module
#: docstring — so this path only ever exists as a gitignored scratch file or
#: inside a CI runner's ephemeral workspace.
OUTPUT = REPO / "llms-full.txt"

#: `[title](url)` markdown links, one per file list entry. `re.M` is not
#: needed — a link never spans a newline in llms.txt's own format.
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

#: llms.txt links to raw.githubusercontent.com so a fetch returns plain text
#: instead of GitHub's rendered HTML. The repo-relative path a `check_*`
#: script can verify offline is everything after the `<branch>/` segment.
RAW_URL_RE = re.compile(
    r"^https://raw\.githubusercontent\.com/[^/]+/[^/]+/[^/]+/(?P<path>.+)$"
)


def extract_entries(llms_txt_text: str) -> list[tuple[str, str, str]]:
    """Return (section, title, repo_relative_path) for every link in llms.txt."""
    entries: list[tuple[str, str, str]] = []
    section = ""
    for line in llms_txt_text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if not line.startswith("- "):
            continue
        m = LINK_RE.search(line)
        if not m:
            continue
        title, url = m.group(1), m.group(2)
        path_m = RAW_URL_RE.match(url)
        if not path_m:
            # Not a raw-content link (e.g. a bare github.com URL with no repo
            # file behind it) — nothing to inline, so llms-full.txt skips it
            # rather than guessing a path.
            continue
        entries.append((section, title, path_m.group("path")))
    return entries


def build() -> str:
    llms_txt_text = LLMS_TXT.read_text(encoding="utf-8")
    entries = extract_entries(llms_txt_text)
    if not entries:
        raise SystemExit("build_llms_full: parsed zero linked files out of llms.txt "
                          "— the link/URL pattern matched nothing, which means this "
                          "would silently produce an empty document")

    header = (
        "# codecalc — full documentation set\n\n"
        "> Generated from llms.txt by scripts/build_llms_full.py. Do not edit by "
        "hand — regenerate it instead, and see scripts/check_llms_txt.py for the "
        "gate that keeps it in sync.\n"
    )
    parts = [header]
    current_section = None
    for section, title, rel_path in entries:
        file_path = REPO / rel_path
        if not file_path.is_file():
            raise SystemExit(f"build_llms_full: llms.txt links {rel_path!r}, which "
                              "does not exist in this checkout")
        if section != current_section:
            parts.append(f"\n---\n\n# {section}\n")
            current_section = section
        content = file_path.read_text(encoding="utf-8").rstrip("\n")
        parts.append(f"\n## {title} (`{rel_path}`)\n\n{content}\n")
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=OUTPUT,
                        help=f"where to write the generated file (default: {OUTPUT})")
    args = parser.parse_args()

    text = build()
    if len(text) < 1000:
        raise SystemExit(f"build_llms_full: produced only {len(text)} bytes — "
                          "too small to be the real doc set, refusing to write it")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    n_files = len(extract_entries(LLMS_TXT.read_text(encoding="utf-8")))
    print(f"wrote {args.output} ({len(text)} bytes from {n_files} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
