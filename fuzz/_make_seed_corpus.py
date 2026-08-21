#!/usr/bin/env python3
"""Build the libFuzzer seed-corpus zips from scripts/fuzz.py's own corpus
lists. Called by .clusterfuzzlite/build.sh at BUILD time — not imported by
the harnesses, and not a `*_fuzzer.py` file itself (build.sh's `find ...
-name '*_fuzzer.py'` loop must not try to package this as a fuzz target).

The zip is generated from scripts/fuzz.py on every build rather than
committed as a second copy of the same strings under fuzz/corpus/: a copy
that could drift from scripts/fuzz.py's corpus is exactly the defect
fuzz/README.md warns about. OSS-Fuzz/ClusterFuzzLite's own convention is a
zip named `<fuzzer_basename>_seed_corpus.zip` dropped in $OUT next to the
built fuzzer binary.

Usage: python3 fuzz/_make_seed_corpus.py <OUT dir>
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import fuzz as fuzz_corpus  # noqa: E402 — needs the path inserts above


def _write_zip(path: Path, strings: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for i, s in enumerate(strings):
            zf.writestr(f"seed_{i:03d}", s.encode("utf-8"))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: _make_seed_corpus.py <OUT dir>", file=sys.stderr)
        return 2
    out = Path(argv[1])
    _write_zip(out / "safe_expr_fuzzer_seed_corpus.zip", fuzz_corpus.SEED_CORPUS_EXPR)
    _write_zip(
        out / "sessions_path_fuzzer_seed_corpus.zip",
        fuzz_corpus.SEED_CORPUS_PATH + fuzz_corpus.SEED_CORPUS_SESSION_ID,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
