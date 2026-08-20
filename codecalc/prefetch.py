"""Download every tree-sitter grammar codecalc uses, before anything needs one.

WHY THIS EXISTS (THE-820). `tree-sitter-language-pack` does not bundle grammars.
It ships a ~4.7 MB native extension and downloads each grammar on FIRST USE into
a versioned on-disk cache — measured: deleting `libtree_sitter_elixir.so` and
asking for `elixir` again brought the file back, byte for byte, in 0.47s.

That is a per-language network call, which is why CI failed the way it did:

  * one language missing while the other 31 worked, because each is a separate
    fetch and only one of them failed;
  * intermittently, because it depends on the network;
  * on CI but not locally, because a developer box has a warm cache (89 MB
    here) and every runner starts cold.

The failure surfaced as five scattered assertion failures in the middle of a
test suite, which is the worst place for it: nothing there says "a download
failed", and `analyze_complexity` degrades to its regex fallback rather than
erroring, so the suite is the only thing that notices.

So this moves the fetch to a step of its own, BEFORE any suite runs. A network
problem then fails a step called "prefetch grammars" with the actual exception,
instead of failing assertions about loop counting.

It is also the answer for an offline install: run this once with a network, and
the cache is warm for every later run. `--print-cache-dir` emits the directory
to preserve — that is what CI keys its cache on, and what an operator would
copy into an air-gapped environment.

THE-877: this used to live only at `scripts/prefetch_grammars.py`, which is
NOT in the wheel (`[tool.hatch.build.targets.wheel] packages = ["codecalc"]`
does not carry `scripts/`), so an installed user (`uv tool install codecalc`)
had no way to run it — the README told them to, and the command did not exist.
Moving the logic here makes it importable and gives it a console-script entry
point (`codecalc-prefetch-grammars`, `[project.scripts]`) that ships with every
install. `scripts/prefetch_grammars.py` remains for the from-source path and
just calls `main()` here, so there is one implementation, not two.

Exit codes: 0 every grammar resolved · 1 at least one did not (the name and its
error are printed) · 2 the parsing extra is not installed at all, which is a
different problem with a different fix.
"""

from __future__ import annotations

import argparse
import sys
import time

from . import optional, parsing, registry

#: A failed download is usually transient, so one attempt is a coin flip on the
#: network rather than a measurement of it. Three attempts with a short backoff
#: is enough to distinguish "the network blipped" from "this grammar cannot be
#: fetched", which are the two cases a reader of the log needs told apart.
ATTEMPTS = 3
BACKOFF_SECONDS = 2


def cache_dir() -> str | None:
    """Where the pack keeps downloaded grammars, or None if it cannot say."""
    try:
        from tree_sitter_language_pack import cache_dir as _cache_dir

        return str(_cache_dir())
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--print-cache-dir", action="store_true",
                    help="print the grammar cache directory and exit")
    args = ap.parse_args()

    if args.print_cache_dir:
        d = cache_dir()
        if d is None:
            print("::error::cannot resolve the grammar cache directory — "
                  "is the 'parsing' extra installed?", file=sys.stderr)
            return 2
        print(d)
        return 0

    if not optional.have("tree_sitter_language_pack"):
        # Distinguished from a download failure ON PURPOSE: this needs
        # `pip install 'codecalc[parsing]'`, not a retry.
        print("::error::the 'parsing' extra is not installed; nothing to prefetch")
        return 2

    # Every language codecalc can be asked about, deduplicated by GRAMMAR —
    # `python3` and several aliases share one, and fetching it four times would
    # make the log lie about how many downloads happened.
    wanted: dict[str, list[str]] = {}
    for lang in sorted(registry.LANGUAGES):
        g = parsing.grammar_name(lang)
        if g:
            wanted.setdefault(g, []).append(lang)

    print(f"prefetching {len(wanted)} grammars for {len(registry.LANGUAGES)} "
          f"registry languages")
    d = cache_dir()
    print(f"cache: {d or '(unknown)'}")

    failed: dict[str, str] = {}
    for grammar in sorted(wanted):
        for attempt in range(1, ATTEMPTS + 1):
            # `_grammar_available` is what codecalc itself calls, so this warms
            # exactly the path the tools use rather than a parallel one. It is
            # cached, so clear it between attempts or a retry re-reads the
            # first answer instead of retrying.
            parsing._grammar_available.cache_clear()
            if parsing._grammar_available(grammar):
                failed.pop(grammar, None)
                break
            failed[grammar] = parsing.grammar_error(grammar) or "(no reason recorded)"
            if attempt < ATTEMPTS:
                time.sleep(BACKOFF_SECONDS * attempt)

    ok = len(wanted) - len(failed)
    print(f"\nok   {ok}/{len(wanted)} grammars available")
    if failed:
        print(f"::error::{len(failed)} grammar(s) could not be fetched after "
              f"{ATTEMPTS} attempts:")
        for grammar, why in sorted(failed.items()):
            print(f"  {grammar} (for {', '.join(wanted[grammar])}): {why}")
        print("\nThis is a DOWNLOAD failure, not a codecalc defect: "
              "tree-sitter-language-pack fetches grammars on first use. "
              "Re-run, or warm the cache above from a host with network access.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
