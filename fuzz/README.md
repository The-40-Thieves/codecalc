# fuzz/ — ClusterFuzzLite coverage-guided fuzzing

Two fuzzers, coverage-guided via [ClusterFuzzLite](https://google.github.io/clusterfuzzlite/)
(libFuzzer + [atheris](https://github.com/google/atheris) + AddressSanitizer),
running in this repo's own GitHub Actions:

- `.github/workflows/cflite_pr.yml` — every pull request, `mode: code-change`
  (exits on first crash), a bounded time budget.
- `.github/workflows/cflite_batch.yml` — scheduled, longer runs that extend
  the corpus over time.

Each fuzzer targets exactly one of scripts/fuzz.py's two logical targets:

| Harness | Target | Guard |
| --- | --- | --- |
| `safe_expr_fuzzer.py` | `codecalc.safe_expr` | `classify_unsafe` / `safe_parse` — the denylist screen before SymPy's eval-based parser |
| `sessions_path_fuzzer.py` | `codecalc.sessions` | `_jail` / `_session_dir` — the path-traversal guard |

## This is NOT a replacement for `scripts/fuzz.py`

The two are complementary, not redundant:

- **`scripts/fuzz.py`** is a fast, deterministic, seeded-mutation **gate**. A
  small fixed slice of it (`tests/test_fuzz_smoke.py`) runs on every PR in
  seconds; a deeper multi-thousand-iteration run is a manual/CI-opt-in pass.
  Deterministic on purpose: a crash reproduces exactly from the
  seed it printed.
- **ClusterFuzzLite (this directory)** is coverage-guided **discovery**.
  libFuzzer's mutation engine is steered by which branches an input actually
  reaches, exploring input space no fixed mutation stream will — at the cost
  of not being reproducible from a single seed the way `scripts/fuzz.py` is;
  a ClusterFuzzLite crash reproduces from the artifact it uploads, not a
  seed number.

Keep both. A regression in the guarded surface should fail fast in the
`fuzz-smoke` PR gate; a genuinely novel input shape is what the
coverage-guided engine is for.

## No duplicated seed corpus

Both harnesses `import fuzz` (`scripts/fuzz.py`) and reuse its
`SEED_CORPUS_EXPR` / `SEED_CORPUS_PATH` / `SEED_CORPUS_SESSION_ID` lists and
its `_plant_symlink_trap` helper directly — nothing here re-lists those
payloads. `.clusterfuzzlite/Dockerfile` puts `scripts/` on `PYTHONPATH` so
`import fuzz` resolves the same way at pyinstaller build time and at fuzzer
runtime; see that file's comments for why.

The initial libFuzzer seed corpus (`$OUT/<fuzzer>_seed_corpus.zip`) is
likewise **generated at build time** from those same lists by
`fuzz/_make_seed_corpus.py` — not checked into `fuzz/corpus/` as a second,
driftable copy.

## Contract each harness holds targets to

Same contract `scripts/fuzz.py`'s own module docstring states in full:
never raise an exception the target does not itself define as its result,
never actually call a builtin like `input()`/`breakpoint()`, never run
unboundedly. `classify_unsafe`/`safe_parse` are documented to never raise at
all (any exception is a finding); `_jail`/`_session_dir` are documented to
raise `ValueError` as their refusal (caught as safe) and otherwise return a
`Path` that must stay inside the workspace/root it was jailed to (checked
explicitly, since an escaped path — not just a crash — is the actual bug
class these guards exist to prevent).

## Corpus / crash persistence

`cflite_batch.yml` does not currently wire a `storage-repo` (that needs a
`PERSONAL_ACCESS_TOKEN` secret pointing at a separate storage repo this repo
does not yet have). Corpus and crash artifacts from batch runs are **not
persisted across runs** today — each scheduled run starts from the seed
corpus above plus whatever GitHub Actions cache survives, not an
accumulated history. See the comment in `cflite_batch.yml` for how to
enable persistence once a storage repo + secret exist.
