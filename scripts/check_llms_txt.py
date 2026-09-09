#!/usr/bin/env python3
"""llms.txt must only link things that exist, and its generator must run clean.

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
would eventually serve.

llms-full.txt (the full text of everything llms.txt links, built by
scripts/build_llms_full.py) is NOT committed to this repo — see that
script's module docstring for why — so there is no checked-in copy to diff
against here. What this gate proves instead is that the generator still
RUNS CLEANLY end to end: it is invoked as a real subprocess against a
throwaway temp path, exactly the way `.github/workflows/release.yml`'s
`release-assets` job invokes it for an actual release, and the result must
exit 0 and produce a plausibly-sized file. A generator that raises, or that
silently starts producing 40 bytes, would otherwise only be discovered when
a release runs it for real.

FLOOR: refuses to pass if it parsed zero links, or if none of them were
recognized raw-content URLs — either would mean the regex silently stopped
matching and this file would report a clean tree with nothing checked.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from build_llms_full import LLMS_TXT, extract_entries  # noqa: E402

BUILD_SCRIPT = REPO / "scripts" / "build_llms_full.py"

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
# a section holding only a github.com/blob link, or a stable release-asset
# URL like llms-full.txt's own entry, would otherwise sit in the file
# contributing zero checked links, looking gated while nothing in it is.
sections_with_links = {section for section, *_ in entries}
declared_sections = {
    line[3:].strip() for line in llms_txt_text.splitlines() if line.startswith("## ")
}
unchecked_sections = sorted(declared_sections - sections_with_links)
if unchecked_sections:
    fail(f"llms.txt section(s) {unchecked_sections} contain no link this gate "
         "could verify (not a raw.githubusercontent.com URL) — every section "
         "must have at least one entry pointing at a real file in this repo")
elif declared_sections:
    print(f"ok   llms.txt: all {len(declared_sections)} sections "
          f"({sorted(declared_sections)}) have at least one verifiable link")

# The generator must run cleanly to a throwaway path — the same invocation
# shape release.yml uses for the real release asset, minus the network and
# the upload.
with tempfile.TemporaryDirectory() as tmpdir:
    out_path = Path(tmpdir) / "llms-full.txt"
    proc = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--output", str(out_path)],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        fail(f"scripts/build_llms_full.py exited {proc.returncode} building to a "
             f"temp path — stdout: {proc.stdout!r} stderr: {proc.stderr!r}")
    elif not out_path.is_file():
        fail("scripts/build_llms_full.py exited 0 but wrote no file at --output "
             "— the CLI's own success message cannot be trusted")
    else:
        size = out_path.stat().st_size
        if size < 1000:
            fail(f"scripts/build_llms_full.py produced only {size} bytes at a "
                 "temp path — too small to be the real doc set")
        else:
            print(f"ok   scripts/build_llms_full.py: ran cleanly to a temp path "
                  f"({size} bytes)")

if failures:
    print(f"\n=== {len(failures)} llms.txt claim(s) out of date ===")
    sys.exit(1)
print("\n=== llms.txt links resolve and its generator runs clean ===")
