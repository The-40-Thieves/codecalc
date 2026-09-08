#!/usr/bin/env python3
"""llms.txt must only link things that exist, and llms-full.txt must match it.

llms.txt (https://llmstxt.org/) is a curated index for LLM clients — a
title, a summary, and a `## Section` list of `[title](url)` entries. Nothing
in this repo enforced that those links stay real: a file renamed or deleted
in the same PR that forgot to touch llms.txt would leave a dangling link that
only a live fetch would ever notice, and this repo's other gates are
deliberately the kind that run on a bare checkout with no network (see
CONTRIBUTING.md and check_portability.py's docstring for why).

This gate stays in that family. It never makes a network request — every
`raw.githubusercontent.com/.../<branch>/<path>` URL is converted back to a
repo-relative PATH and checked for existence on disk, the same file the URL
would eventually serve. That is what "offline" buys here: a link that is
wrong is wrong on this machine too, not just wrong on GitHub.

It also re-derives llms-full.txt (scripts/build_llms_full.py) and fails if
the committed file does not match byte-for-byte — the same "generated file
drifted from its generator" shape as check_parity.py, one file over.

FLOOR: refuses to pass if it parsed zero links, or if none of them were
recognized raw-content URLs — either would mean the regex silently stopped
matching and this file would report a clean tree with nothing checked.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from build_llms_full import LLMS_TXT, OUTPUT, build, extract_entries  # noqa: E402

failures: list[str] = []


def fail(msg: str) -> None:
    print(f"FAIL {msg}")
    failures.append(msg)


llms_txt_text = LLMS_TXT.read_text(encoding="utf-8")
entries = extract_entries(llms_txt_text)

if not entries:
    fail("parsed zero '[title](raw.githubusercontent.com/...)' links out of "
         "llms.txt — the extractor matched nothing, so this gate proves "
         "nothing rather than passing")
else:
    missing = [rel_path for _, _, rel_path in entries if not (REPO / rel_path).is_file()]
    if missing:
        fail(f"llms.txt links {len(missing)} path(s) that do not exist in this "
             f"checkout: {sorted(set(missing))}")
    else:
        print(f"ok   llms.txt: all {len(entries)} linked paths exist "
              f"({len({p for *_, p in entries})} distinct files)")

# Every H2 section in llms.txt must contain at least one recognized link —
# a section with a github.com/blob link or a bare URL that never matches
# RAW_URL_RE would otherwise sit in the file contributing zero checked links,
# looking gated while nothing in it is.
sections_with_links = {section for section, *_ in entries}
declared_sections = {
    line[3:].strip() for line in llms_txt_text.splitlines() if line.startswith("## ")
}
unchecked_sections = sorted(declared_sections - sections_with_links)
if unchecked_sections:
    fail(f"llms.txt section(s) {unchecked_sections} contain no link this gate "
         "could verify (not a raw.githubusercontent.com URL) — every entry "
         "must point at a real file in this repo")
elif declared_sections:
    print(f"ok   llms.txt: all {len(declared_sections)} sections "
          f"({sorted(declared_sections)}) have at least one verifiable link")

# llms-full.txt must be exactly what build_llms_full.py would produce today.
if not OUTPUT.is_file():
    fail(f"{OUTPUT.name} does not exist — run "
         "`uv run python scripts/build_llms_full.py` and commit it")
else:
    rebuilt = build()
    committed = OUTPUT.read_text(encoding="utf-8")
    if rebuilt != committed:
        fail(f"{OUTPUT.name} is stale — it does not match what "
             "scripts/build_llms_full.py generates from the CURRENT llms.txt "
             "and the files it links. Regenerate with "
             "`uv run python scripts/build_llms_full.py` and commit the result")
    else:
        print(f"ok   {OUTPUT.name}: matches scripts/build_llms_full.py's output "
              f"({len(committed)} bytes)")

if failures:
    print(f"\n=== {len(failures)} llms.txt claim(s) out of date ===")
    sys.exit(1)
print("\n=== llms.txt and llms-full.txt match the repo ===")
