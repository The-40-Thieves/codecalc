#!/bin/bash -eu
# .clusterfuzzlite/build.sh — the standard ClusterFuzzLite Python build
# (context7 /google/clusterfuzzlite docs, "Python Build Script for
# ClusterFuzzLite"): install the project, then package every *_fuzzer.py
# with pyinstaller and wrap each with the sanitizer-preload launcher
# libFuzzer/OSS-Fuzz expects to find at $OUT/<fuzzer_basename>.
#
# `-eu`, not `set -euo pipefail`: this is verbatim the upstream shebang/flag
# convention (no pipeline in this script needs pipefail).
#
# `.[symbolic]`, not a bare `.`: the safe_expr harness fuzzes `safe_parse`,
# which lazy-imports SymPy (codecalc/safe_expr.py). A base install has no
# SymPy, so `safe_parse` raises `ModuleNotFoundError` on the FIRST input —
# atheris reports that as an uncaught crash and libFuzzer marks the target
# broken, failing the build (observed in CI before this line changed). We want
# the harness to exercise the real screen→SymPy path, not skip it, so SymPy
# has to be present. The extra (not a pinned `sympy==`) tracks pyproject's own
# `symbolic` bound so it cannot drift from what the package declares.
pip3 install '.[symbolic]'

# The loop variable is only ever a path under this repo's own fuzz/ dir
# (COPY'd in by the Dockerfile), never externally-controlled input, so the
# word-splitting hazard SC2044 warns about does not apply here; kept as the
# exact upstream loop shape (context7 /google/clusterfuzzlite docs) rather
# than rewritten to a `find ... -print0 | while read -d ''` form.
# shellcheck disable=SC2044
for fuzzer in $(find "$SRC" -name '*_fuzzer.py'); do
  fuzzer_basename=$(basename -s .py "$fuzzer")
  fuzzer_package="${fuzzer_basename}.pkg"

  pyinstaller --distpath "$OUT" --onefile --name "$fuzzer_package" "$fuzzer"

  # Wrapper script that preloads the sanitizer library ahead of the frozen
  # interpreter — same wrapper shape as the upstream docs example. $OUT/
  # $fuzzer_basename (no extension) is what run_fuzzers/libFuzzer invoke;
  # $fuzzer_package is the actual pyinstaller-frozen harness it execs into.
  cat >"$OUT/$fuzzer_basename" <<WRAPPER
#!/bin/sh
# LLVMFuzzerTestOneInput for fuzzer detection.
this_dir=\$(dirname "\$0")
LD_PRELOAD=\$this_dir/sanitizer_with_fuzzer.so \\
  ASAN_OPTIONS=\$ASAN_OPTIONS:symbolize=1:external_symbolizer_path=\$this_dir/llvm-symbolizer:detect_leaks=0 \\
  \$this_dir/$fuzzer_package "\$@"
WRAPPER
  chmod +x "$OUT/$fuzzer_basename"
done

# Seed corpus: GENERATED here from scripts/fuzz.py's own SEED_CORPUS_* lists,
# not checked into fuzz/corpus/ as a second copy that could drift from them —
# see fuzz/_make_seed_corpus.py and fuzz/README.md.
python3 "$SRC/codecalc/fuzz/_make_seed_corpus.py" "$OUT"
