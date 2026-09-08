# Changelog

All notable changes to this project are documented here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## Two version numbers, on purpose

This project versions **two** things, and they are not the same number.

| What | Where | Current |
|---|---|---|
| The **package** — the tool surface, the CLI, the Python API | `pyproject.toml`, `executor/Cargo.toml`, this file | see `version` in [`pyproject.toml`](pyproject.toml) — this cell is not re-typed on every release |
| The **result contract** — the shape every tool result comes back in | `docs/contract/README.md`, `contract_version` on every result | `1.10.0` |

The contract is at `1.10.0` and the package is at `0.x` because those claims are
genuinely different. The result contract has a published JSON Schema, a
documented MAJOR/MINOR/PATCH policy, a twelve-month deprecation window, and a
gate that fails if the schema drifts from the code — it is stable and says so.
The package's tool surface is not: the roadmap restructures how requests are
described and how execution backends plug in, and calling it `1.0.0` today would
promise a stability nobody should rely on.

[Semver's own guidance](https://packaging.python.org/en/latest/discussions/versioning/)
is that reaching `1.0.0` means accepting MAJOR/MINOR/PATCH semantics, not
"enough features have landed". `0.x` here is a statement about the tool surface
only. Treat `0.y.z` as if it were `1.y.z` anyway: `y` bumps for breaking changes,
`z` for compatible ones.

If you pin one thing, pin `contract_version` — it is the number with a policy
behind it.

---

## [Unreleased]

## [0.10.0] — 2026-09-08

### Fixed

- `doctor --deep`'s version probe stored a failed probe's own stderr as the
  runtime's `version` and left `status` at `installed` — reproduced against
  Apple's `/usr/bin/java` stub on a macOS host with no JDK, which resolves on
  PATH and exits non-zero printing "The operation couldn't be completed.
  Unable to locate a Java Runtime." A version probe that never gets an
  answer at all (a spawn failure or a timeout) now demotes the row to
  `unhealthy`, counts in `runtime_summary.unhealthy`, and reports the
  failure under the new `probe_error` field — never under `version`. A
  NONZERO EXIT alone is trusted as evidence of brokenness only when the flag
  used is one this code has confirmed correct for that command (an explicit
  `_VERSION_FLAG` entry, as java's `-version` already was): `--version` is a
  GNU convention, not a universal one, and an earlier version of this fix
  trusted ANY nonzero exit, which reported go (`go --version` exits 2; `go
  version` is correct), lua (`lua --version` exits 1; `-v` is correct) and
  zig (`zig --version` exits 1; `zig version` is correct) `unhealthy` on a
  perfectly working host — go being `tested` tier, that also flipped
  `healthy: false`. All three now have confirmed `_VERSION_FLAG` entries;
  a command on the untested default that exits non-zero is reported as
  merely unmeasured (`probe_error` recorded, `status` untouched), and a
  language with a hello-world check (`_HELLO`) always has that run BEFORE
  the version probe gets a say, not after. `healthy` also goes `false` when
  a `tested`-tier runtime (python3/node/rust/go — the tier a CI job
  genuinely executes and checks on every PR) resolved and then proved
  broken via a TRUSTED probe failure; an uninstalled or broken
  `best_effort`/`plan_only` runtime, java and kotlin included, still leaves
  it untouched (#279).
- `kotlin` was registered with its resolution `command` set to `java` (the
  binary its `run` step invokes), so `doctor`/`list_languages` reported it
  `installed` from a JRE alone on any host with no Kotlin toolchain at all —
  `execute_code(language="kotlin")` then failed at spawn with a bare
  `exit_code -2`. `doctor`'s deciding command for a compile-then-run language
  is now always its compile tool (`kotlinc` for kotlin), and a plan whose
  `run` step needs a SECOND, different tool that the compile tool's
  resolution says nothing about is reported `installed` only when BOTH
  resolve — kotlin is the one instance of this in the registry today, and the
  check is derived mechanically from `compile`/`run` rather than
  hand-maintained, so a future language in the same shape is caught by
  construction. The Rust executor's spawn-failure message also now names the
  phase and the missing binary instead of a bare OS error — though that
  message still lands only in `stderr`, not in an `error` key, so
  `errors.ensure_code` still classifies a Rust-backend spawn failure as
  `internal` rather than `runtime_unavailable` (the Python fallback, whose
  envelope carries `error`, classifies it correctly); pre-existing, not
  fixed here, tracked separately (#280).
- Closing that gap: a Rust-backend spawn failure now sets an `error` key
  too (previously only `stderr`), worded to start "runtime unavailable for
  the ... phase: ..." so `errors.ensure_code` classifies it
  `runtime_unavailable` instead of `internal`, matching the pure-Python
  fallback's existing classification of the identical failure. `exit_code`
  is now `null` on both backends for this case (the fallback's existing
  convention — the prior Rust-only sentinel `-2` carried no meaning to a
  caller), and the Python fallback's mapping of a raw Rust result now also
  recognises an OLDER binary's stderr-only shape via a `"runtime
  unavailable"` prefix match, so a caller gets the same classification
  regardless of which binary answered (#280).
- **`tests/test_executor_sweep.py` and `tests/test_appcontainer.py` decided
  whether a native executor was present by checking `.exists()` on a
  hardcoded `bin/codecalc-exec` path, ignoring `CODECALC_EXEC_BIN`
  entirely.** A local run that pointed only `CODECALC_EXEC_BIN` at a binary
  built elsewhere (say `executor/target/release/codecalc-exec`, with `bin/`
  empty) silently skipped every "rust:" assertion with no SKIP line printed
  — the `if EXE.exists():` guard had no `else` branch — and setting
  `CODECALC_REQUIRE_NATIVE=1` alongside it did nothing to catch the
  mistake, since that variable was never consulted by the test's own
  hardcoded check. That is how a stale Rust-backend `exit_code` assertion
  passed locally and only failed in CI's sandbox job, which always builds
  straight into `bin/`. Both files now resolve the binary through a new
  shared helper, `tests/_helpers.resolve_native_executor`, which calls
  `codecalc/executor.py`'s own `_rust_binary()` (`CODECALC_EXEC_BIN` first,
  then the arch-aware `bin/` candidates) instead of duplicating that
  ordering — the two can no longer drift apart. It prints one loud `SKIP
  native executor: <reason>` line when nothing resolves, and raises instead
  of returning `None` when `CODECALC_REQUIRE_NATIVE=1` is set and nothing
  resolves, so a run that means to test the native backend cannot quietly
  fall through to skipping it. `.github/workflows/ci-python.yml`'s sandbox
  job now greps an existence floor for both files independently — `PASS
  rust:` for the first, the helper's own `native executor: <path>` line for
  the second, whose checks are not "rust:"-named — the same
  assert-from-outside-the-file pattern `test_features.py` uses (#277).
- A runtime with no confirmed `_VERSION_FLAG` entry that failed its version
  probe on the untested `--version` guess was disclosed in the JSON row only
  (`status: installed`, `probe_error` set) — the text renderer showed it
  under none of available/BROKEN/missing, and `_remedies()` said nothing
  about it either, so an operator reading `doctor` (rather than piping
  `--json`) never learned the probe had failed at all. `_VERSION_FLAG` now
  carries an explicit, audited entry for every registered runtime's command
  (`escript`/`tclsh` excluded — no non-interactive version flag exists to
  call), not just the five flag overrides, all confirmed live on this box
  with exit 0 on the default `--version` (audit table in the PR body) —
  leaving "unmeasured" the rare case of a genuinely new, not-yet-audited
  command rather than the common one. The trust rule itself is unchanged: a
  nonzero exit is still evidence of brokenness only for a command this code
  has confirmed the flag for. The text renderer now lists an
  installed-but-probe-failed row under its own "installed, version probe
  failed" heading, and `_remedies()` names it with the probe output,
  phrased as "the runtime may still work" so it is not overclaimed as
  broken. JSON shape unchanged — `remedies` was already `array of string`,
  and `probe_error` already existed on the row.

  Review caught that `awk`'s new confirmed-default entry was audited only
  against THIS box's GNU awk: `awk` is the one command name on the audited
  list that resolves to genuinely different, non-interoperable programs
  across hosts — BSD/macOS one-true-awk and busybox awk reject GNU long
  options like `--version` outright, which is exactly the shape #282 was
  written to fix, just for a command #282 never audited. `awk` is now also
  in `_HELLO` (`BEGIN{print "codecalc"}`, portable POSIX syntax) so a
  hello-world arbitrates its status BEFORE the version probe gets a say,
  regardless of which awk flavor answers; `sqlite3`/`jq`/`zsh` gained the
  same belt-and-suspenders treatment even though neither has a known
  divergent fork. Every other confirmed-default entry is a single-vendor,
  cross-platform-consistent CLI with `--version` documented in its own
  reference and is left without a hello-world backstop (per-command
  portability note in the PR body).
- `compare_execution` rebuilt each per-language row from a fixed field list
  (`language`, `ok`, `stdout`, `stderr`, `exit_code`, `duration_ms`,
  `timed_out`, `cold_retry`) and never carried `error`/`code`/`remedy` from
  the underlying `executor.execute()` result — a row for a language with no
  runtime installed on this host read `stderr: "runtime unavailable ..."`
  with nothing a caller could branch on, and the tool's OWN `ok` is `True`
  whenever the comparison ran (never "every language succeeded"), so
  `server.py`'s `_coded` wrapper (`errors.ensure_code`) never reached the
  rows either. A row that failed for a REQUEST-level reason — no runtime for
  that language, or a timeout that survived the one warm retry — now carries
  `error`/`code`/`remedy` via the new `errors.stamp_row`; an ordinary
  program failure (a real `RTE`/`OLE` `exit_code`) still carries none of the
  three, matching the INTENDED convention that `code` marks a failed
  request, not a failed program — the same convention `errors.ensure_code`
  now applies to the top-level `execute_code` envelope itself (see the
  Fixed entry below).
  `compare_edge_cases`'s per-language `runs[lang]` entries had the identical
  gap and are fixed the same way. `compare_execution`'s own result also
  never matched any branch
  in the published result contract — the same gap `edge_case_comparison`
  closed for `compare_edge_cases` in `1.5.0` — so contract `1.10.0` adds a
  `comparison_rows` branch for it; see `docs/contract/README.md`.
- `execute_code`/`execute_code_stream`/`run_inspect`/`session_run` (anything
  routed through `errors.ensure_code`, at `server.py`'s `_coded` wrapper)
  classified an ordinary program failure as `internal` — remedy "a defect
  in codecalc; the message is worth reporting verbatim" — whenever
  `executor.execute` set no `error` of its own: a plain `sys.exit(3)` (RTE)
  or a plain wall-clock timeout (TLE), on either backend, never set `error`
  at all (only `verdict`/`exit_code`/`timed_out`), so `ensure_code`'s
  message matcher had an empty string to classify and fell through to its
  fallback every time. A model reading that filed a bug for a program that
  did exactly what it was written to do. `docs/contract/README.md`'s own
  "The program ran and failed" example already documented the intended
  reading — a result with `verdict` present and no `error` is a failed
  PROGRAM, not a failed REQUEST, and gets no `code` at all — the same rule
  `errors.stamp_row` (above) already applied one level down, for
  `compare_execution`/`compare_edge_cases` rows. `ensure_code` now applies
  it at the envelope level too: a real `RTE`/`OLE`/`MLE` verdict with no
  `error` is left with no `code`, and a timeout (`verdict: "TLE"` or
  `timed_out: true`) is classified `timeout` instead — actionable (raise
  `timeout`) rather than a false defect report. A genuine unknown (`ok:
  false`, no `verdict`, no `error`) still falls to `internal`,
  `code_inferred: true`, unchanged. No schema or `CONTRACT_VERSION` change:
  `code`/`error`/`remedy`/`code_inferred` were already optional on the
  execution envelope, and the corrected behavior is what the published
  contract already documented — this fixes the code to match the docs, not
  the other way round. A COMPILE failure (`phase: "compile"`, e.g. a `c`/
  `rust` syntax error) follows the identical rule and is now a documented
  DECISION rather than an accident of the verdict-based gate also catching
  it: retrying the same request cannot succeed either way, so it stays
  codeless too — see `errors.ensure_code`'s docstring and
  `docs/contract/README.md`'s new compile-failure example. Neither backend
  emits a verdict distinct from an ordinary run's `RTE` for a compile
  failure; adding one to the closed 8-code enum (or a ninth `VERDICTS`
  entry) is a bigger, separate decision this fix does not make.
- On POSIX (Linux, macOS), the native (Rust) backend reported
  `exit_code: null` for a process killed BY A SIGNAL (a segfault, the OOM
  killer's `SIGKILL`, ...) — indistinguishable from one that never spawned
  at all, and disagreeing with the pure-Python fallback, which already used
  `subprocess.Popen`'s own convention: negative, the signal number (`-11`
  for `SIGSEGV` on Linux glibc; `-5` for `SIGTRAP` on macOS/Apple silicon
  for the identical null-deref — the exact number is platform-specific, the
  SIGN is not). `executor/src/main.rs`'s new `exit_code_json` aligns the
  native backend to the SAME convention the fallback already used on each
  OS — including Windows, which has no signals at all and was already
  correct on both backends: an access violation there is a real, POSITIVE
  exit code (the NTSTATUS itself, e.g. `3221225477` /
  `0xC0000005` for `STATUS_ACCESS_VIOLATION`), unaffected by this fix. A
  caller now tells "never ran" from "ran and was killed abnormally" from
  whether `exit_code` is `null` at all, on either backend, on any OS.
  `contract.py`'s `exit_code` description documents the convention per
  platform. No new field, no `code_inferred` involved: `exit_code`'s type
  (`integer | null`) does not change, only which integers a POSIX signal
  death can now produce on the native backend.
- The `exit_code: -N` fix directly above reopened `codecalc/executor.py`'s
  compat shim for pre-#283 binaries (which gates on the OLD wire format's
  literal `exit_code == -2` "nothing spawned" sentinel): SIGINT is signal
  2, so a program killed by SIGINT (`raise(SIGINT)` in C,
  `os.kill(os.getpid(), signal.SIGINT)` under python's default handler)
  now legitimately reports `exit_code: -2` too, and the shim could no
  longer tell the two apart from `exit_code` alone — a real, killed
  program came back `code: "runtime_unavailable"`, `error: "runtime
  unavailable for the run phase"`, `exit_code: null`, remedy "install the
  runtime". Found by cross-vendor review. Fixed with a THIRD, ALWAYS-
  present JSON key from the binary, `spawn_error` (`null` when nothing
  went wrong at spawn, the message otherwise — distinct from the existing
  `error`, which stays conditional and caller-facing): the shim now gates
  on `exit_code == -2` **AND** `"spawn_error" not in result` — a binary
  built after this fix carries the key on every result, so the shim can
  never fire against one regardless of what `exit_code` says; its absence
  is what proves the answering binary predates this fix. `spawn_error`
  itself is popped before a caller ever sees the result — internal to this
  one JSON handshake, never part of the published envelope, so no schema
  or `CONTRACT_VERSION` change. Popped on BOTH read paths: `execute_stream`
  (the `execute_code_stream` tool) reads the binary's JSON itself rather
  than through `_execute_uncontracted`, and the first cut of this fix left
  the key in that one surface's result (found by the adversarial review).
- The pure-Python fallback resolved a compile/run command against the
  sandbox's own PATH (`CODECALC_RUNTIME_PATH`) to decide whether it EXISTS,
  then still handed the bare name to `subprocess.Popen` — which on Windows
  lets `CreateProcess` search the CALLING process's PATH instead (Python's
  own docs: "env cannot override the PATH environment variable"). Measured
  on a hosted Windows runner: a fake `gcc` placed first on
  `CODECALC_RUNTIME_PATH` passed the existence check and the runner's own
  MinGW `gcc` ran anyway. The resolved absolute path is now what gets
  spawned, so the sandbox PATH decides which binary runs on every OS, not
  only the ones whose loader consults the child's environment. One
  observable side effect, fallback backend only: a runtime that exposes
  its raw `argv[0]` to the program (`process.argv0` under node/bun/deno;
  python/ruby/perl/bash discard it in favour of the script path) now sees
  the resolved absolute interpreter path rather than the bare name — the
  native backend already behaved this way.
- `execute_code(..., compact=True)` classified a "rejected before execution"
  failure (an unknown language, a validation refusal — anything with no
  `verdict`/`stdout`/`exit_code` to fall back on) as `internal` regardless
  of the real cause, while the identical call with `compact=False` correctly
  classified it: `compact_result` builds a FRESH dict naming only a fixed
  field list, which dropped `error` before `server.py`'s `_coded` wrapper
  ever got to run `errors.ensure_code` on it — by the time the classifier
  saw the result, the text it needed to classify FROM was already gone.
  `execute_code` now runs `ensure_code` BEFORE `compact_result`, and
  `code`/`error`/`remedy`/`code_inferred` are treated as non-droppable
  disclosure (same bucket as `unenforced`/`output_error`) so they survive
  the compaction that follows.
||||||| parent of dca51c2 (fix: stop verify_optimization certifying identical code as a speedup)
- **`verify_optimization` certified IDENTICAL before/after code as a verified
  speedup.** Reproduced live in CI (macOS sandbox job, native executor, main):
  `accepted=True` at a measured ratio of 1.21x, `sizes_rejecting` 2/3 —
  `_accept_decision`'s bare "a majority of sizes reject" rule let two
  false-positive per-size votes carry a three-size verdict outright. Both
  "rejecting" sizes were a tie routed to `stats.mann_whitney_u`'s normal
  approximation at `REPEATS=5` a side (p=0.023, p=0.047) — a sample size
  `stats.py`'s own module docstring says is "not a number worth calling
  alpha=0.05 against" — while the third size (p=0.898) plainly did not
  reject. Closed with five layered fixes in `optimization._infer_speedup`/
  `_accept_decision`, all measured, not just argued:
  - `_infer_speedup`'s per-size rows now carry `stats.mann_whitney_u`'s own
    `method` (`"exact"` / `"normal_approximation"`), previously computed and
    discarded, so a caller can tell which produced a given `p_value` (#285).
  - A size below `stats.min_testable_n(alpha)` observations a side (4 at the
    default alpha=0.05: the smallest n whose exact one-sided p can be < alpha
    at all, `1/C(2n,n)`) is structurally incapable of ever rejecting and no
    longer counts toward the vote it could never contribute a rejection to
    — excluded to `inference.sizes_below_floor` with a `reason` instead
    (replaces a flat `< 2` guard; #284).
  - A `normal_approximation`-method result on fewer than 8 runs a side is
    excluded the same way, but ONLY when the two samples' raw-run ranges
    overlap (`_ranges_overlap`) — the literal "the timer cannot always say
    which side a given pair of runs favoured" case, not merely "a tie
    happened somewhere." Gating on the tie alone (no overlap check) was
    measured LIVE as nearly blinding the tool: this tool's own integer-
    millisecond timings tie constantly even for an unambiguously fast,
    non-overlapping candidate (a real ~16x win's candidate arm produced
    `[42, 42, 45, 53, 42]` from ordinary process-spawn jitter), and excluding
    every tied result outright accepted a genuine win only 1 run in 10.
  - `_accept_decision` now requires EVERY counted size to reject when there
    are <= 3 of them (unanimity — the regime the CI incident's 3-size,
    2-rejecting case falls in, and enough on its own to have refused it),
    or a majority at a Bonferroni-corrected `alpha / k` when there are more
    than 3 (`_fwer_correction`) — a bare majority, always, is what let one
    false-positive size carry a three-size vote. And never fewer than two
    counted sizes at all: unanimity over ONE surviving size is a single
    uncorrected test at the nominal alpha, and adversarial review measured
    that road at a 0.357% false-accept rate on identical code (500,000-trial
    Monte Carlo, 97.6% of it through a lone survivor). A lone testable size
    now yields `accepted: false` with a `decision_basis` that says so and
    what to raise, not "verified faster" — which also means issue #284's
    two-size repro (one size structurally untestable) is now REJECTED for
    that stated reason rather than accepted on the one size that could
    reject; measuring one size fewer no longer flips the verdict either way.
  - A size counts as rejecting only when its OWN before/after ratio also
    clears `min_speedup`, not only its p-value — a size can be statistically
    significant on a difference too small to be the speedup the caller
    asked for.

  Measured before/after on this repo (identical code, 60 runs on the Rust
  backend under a 4-process CPU hog, the default and 3-size CI variant): see
  the PR body for the exact counts. The two live real-win regression tests
  in `tests/test_translation_verify.py` (a real O(n)->O(1) win and a
  calibrated O(n^2)->O(1) win, `nice -n 19` plus a saturating CPU load)
  still accept. Contract `1.11.0` adds `method`/`ratio` to
  `optimization_verification.inference.per_size`,
  `correction`/`effective_alpha` to `optimization_verification.inference`,
  and an optional `reason` to `inference.sizes_below_floor` entries excluded
  for one of the two new reasons above; see `docs/contract/README.md`.
- `execute_code_stream` on the Rust backend could hang forever, with the
  sandboxed program already finished and its full output already sitting on
  disk. `executor.execute_stream`'s progress loop polled `proc.wait()` in a
  0.25s cycle and only drained stdout/stderr afterward, via
  `proc.communicate()` — but `wait()` never reads a byte, and the Rust
  binary's one write to that pipe is its entire final JSON result, output
  and all. Once #269's ceiling raise (64 KiB to 240 KiB) let a result
  exceed the OS pipe buffer (64 KiB, `F_GETPIPE_SZ`'s Linux default) or
  asyncio's own `StreamReader` backpressure limit (also 64 KiB), the child
  blocked inside `write()` with room in the pipe left unfilled — and
  nothing was ever going to unblock it, because the loop's own exit
  condition (`proc.returncode` becoming set) was exactly the write it was
  blocked on. Reproduced live on this host: a `--timeout 30
  --max-output-kb 240` run sat 18 minutes with no child process, state S,
  `wchan anon_pipe_write`, and a workdir whose `run.out` already held the
  full ~960 KiB the program had printed — the kind of hang two earlier
  reports described as "a stray codecalc-exec `--timeout 30` outlived its
  own timeout by >500s", because the executor's `--timeout` bounds only
  the sandboxed program, never this write-the-result phase. Measured
  locally: the exact 983,040-byte shape hung the unpatched code on roughly
  1 run in 3 (asyncio's backpressure pause is itself timing-dependent — a
  synthetic 5 MB result hung it reliably) and a raw `subprocess.Popen` with
  nothing reading its stdout hung on every run, 20/20. `execute_stream` now
  starts `proc.communicate()` as a background task BEFORE the progress loop
  begins polling `run.out`, so the pipe is drained continuously from the
  moment the child is spawned regardless of how large the eventual result
  is; the loop's own exit condition is now the drain task finishing, not a
  bare `wait()`. Every other `subprocess`/`create_subprocess_exec` call
  site in `codecalc/*.py` was audited for the same "wait before drain"
  shape and found to already drain concurrently: the non-streaming Rust
  path (`_execute_uncontracted`) uses `proc.communicate()` directly (drains
  internally via threads), the pure-Python fallback (`_run_step`) spawns
  dedicated drain threads before its poll loop starts, and the session
  REPL workers (`sessions.Worker`) read their response pipe with a blocking
  `readline()` that pulls bytes continuously rather than waiting on process
  exit — none of those needed a change. A Rust-side bound on the write
  phase itself (so an executor whose parent has wedged, rather than died,
  cannot sit in `anon_pipe_write` forever either) was considered and left
  as a follow-up rather than added here: a dead parent's read end closes on
  process exit, and neither `executor/src/platform/unix.rs` nor any other
  Rust source in this repo installs a custom `SIGPIPE` handler, so the
  existing "parent already gone" case already fails fast (`EPIPE`/default
  `SIGPIPE`) rather than hanging — only a parent that is alive but stuck
  elsewhere (a materially different bug) would still find this phase
  unbounded, and a watchdog thread precise enough to bound it without ever
  firing on a merely-slow drain is not a small enough change to land
  alongside this fix.

## [0.9.0] — 2026-09-07

Headline changes since 0.8.0: the runner's own scratch files moved into a
private `.codecalc-run/` subdirectory on both backends, with symlink-safe
writes closing a cross-session overwrite before it shipped;
`verify_optimization` gained a per-size visibility floor, an asymmetric
comparability rule between the baseline and candidate, and one shared
measurement budget across every call it makes; the result contract moves
`1.5.0` → `1.7.0`; and `tests/test_features.py`'s async MCP section — most
of the file's coverage — is now actually executed.

### Fixed

- **`verify_optimization`'s auto-scale visibility floor was applied ACROSS
  ALL sizes combined, not per size** — `optimization._timed` broke out of
  its rescale loop as soon as `max(measured) >= 20ms or min(measured) >=
  5ms` over every tested size together, so once the LARGEST size cleared
  the floor, the loop stopped rescaling even though the SMALLEST sizes
  could still be sitting in timer noise. Real evidence from hosted macOS CI
  (main at `541528bb`'s `ci-python` sandbox job, and PR #270's): a genuine
  1.9-2.4x O(n)->O(1) speedup measured `n=200000` at p=0.65 and `n=400000`
  at p=0.058 (indistinguishable from noise) while the two largest sizes
  were always decisive (p<0.03) — the majority-of-sizes significance test
  (`_accept_decision`, #264) correctly called that "could be noise" and
  rejected a real win. #272 worked around this in the TEST's candidate pair
  only (giving it ~10x more work per element so even the smallest size
  cleared the old combined floor); this fixes the product. `_timed` now
  rescales each size INDEPENDENTLY until ITS OWN measurement clears the
  floor, keeping both arms on one ladder via `_align_sizes` (generalized
  to re-measure whichever side scaled less at each POSITION that still
  disagrees, not just when the two sides' whole size lists differ). A size
  excluded from the significance test for ANY reason — never clears the
  floor within the rescale budget (a genuinely O(1)-fast workload, for
  instance), is unmeasurable, or has too few comparable runs — is excluded
  from the majority vote and named in a new `inference.sizes_below_floor`
  field (`before_ms`/`after_ms` are `null` when no duration was ever
  recorded), so `len(sizes) == len(per_size) + len(sizes_below_floor)`
  always holds — never silently dropped from every field at once, which an
  earlier draft of this same fix did for a size an OLDER, narrower guard
  excluded first (caught in review, before merge: a size at or under the
  1ms noise floor `_speedup` already applies used to `continue` past the
  new disclosure entirely). `grade_verify_optimization`'s basis text
  discloses the exclusion when it happens.
  Verified live on the box, in one uninterrupted run (4 dedicated busy-loop
  processes matching `nproc`, started before and killed only after all 20
  calls completed — an EARLIER attempt at this same measurement is NOT the
  source of this number: it was invalidated when its CPU load died partway
  through, and this is the clean re-run): the ORIGINAL (pre-#272) `s+=i`-
  per-element candidate pair, run 20 times under `nice -n 19` plus that
  load, went from 2/20 accepted (documented in #272) to 13/20 accepted with
  this fix. The failures were NOT spread evenly: all 6 of the first 6 runs
  failed (0-2/4 sizes significant) while the 1-minute load average was
  still climbing from the fresh start, then 9 of the next 10 passed, then
  1 more failed (run 16, 2/4) before the final 4 passed — a real
  Mann-Whitney result on genuinely swamped timings under a harsher-than-
  realistic synthetic load (PR #272's own characterization of this same
  4-busy-loop harness), not a gap the auto-scale floor is meant to close;
  `sizes_below_floor` was empty on every failing run, confirming these were
  noise verdicts, not visibility-floor misses. This candidate pair is
  deliberately NOT added as a hard live CI assertion for the same reason —
  see `tests/test_translation_verify.py`'s comment at that spot. Re-measured
  the worst-case executor-call bound (unchanged at 234 — a size that already clears the floor is no
  longer re-measured for free, so the common case costs FEWER calls, but
  the worst case — every size stays below the floor every round on both
  sides — costs the same as the old whole-batch loop) and the worst-case
  wall time (forced live on this box's Rust backend: ~23.5s at 200 calls,
  ~16.4s at 160 calls including an alignment remeasurement, both
  ~0.11-0.12s/call; scaled to the full 234-call ceiling, ~27s — an ~6.7x
  margin under `verify_optimization`'s 180s MCP deadline). `CONTRACT_VERSION`
  bumped `1.5.0` -> `1.6.0` (MINOR: `sizes_below_floor` added to
  `inference`, always present, no existing field moved).
  `GRADE_RULES_VERSION` unchanged — the evidence-to-grade mapping did not
  move, only `grade_basis`'s text gained an optional clause when sizes were
  excluded.
- **`tests/test_features.py`'s entire async MCP section (session lifecycle,
  file I/O, artifacts, verdicts, compact mode, streaming, package install —
  everything the file's own docstring claims to cover except a handful of
  module-level checks) had never run.** `sys.exit(1 if FAILS else 0)` sat
  BEFORE `asyncio.run(main())` at the bottom of the file, and `sys.exit()`
  raises `SystemExit` immediately — so `main()` was defined but never
  awaited, in every CI run and every local run since it was added.
  `asyncio.run(main())` now runs first. Fixing the ordering surfaced a
  second, previously-invisible bug the dead code was hiding: a module-level
  `_txt = _skill.read_text(...)` (added later, near the bottom of the file)
  shadowed the async `_txt()` helper `main()` calls to extract MCP tool-call
  text — Python resolves a global by name at call time, so every `await
  _txt(...)` inside `main()` would have raised `TypeError: 'str' object is
  not callable` the moment it actually ran. Renamed to `_skill_txt`. Every
  check inside `main()` was re-verified against current tool behaviour on
  both the native Rust backend and the Python fallback and needed no other
  change — the checks already asserted the current documented shapes (e.g.
  `execute_code_stream`'s `streamed`/`streamed_partial`/`note` fields); they
  had just never been exercised. The file now prints an existence floor
  ("N checks, M async") that fails if the async section ever runs zero
  checks again, and `ci-python.yml`'s step for this file greps that line
  independently so a future regression of the same shape cannot silently
  disable it a second time.

- **`verify_optimization` could reject a genuine O(1) win as noise, and
  its alignment step could re-measure a real-cost baseline at a fast arm's
  UNVALIDATED, exhausted rescale ceiling.** Each side's auto-scale in
  `_timed` can exhaust `_MAX_RESCALE_ROUNDS` (10^4x the starting size)
  without ever clearing `_VISIBILITY_FLOOR_MS` — exactly what a genuine O(1)
  candidate does. The old `_align_sizes` saw that as "the other side scaled
  less" and re-measured the SLOWER side (often the baseline, with real
  per-n cost) AT that exhausted n — a legitimate huge win became "baseline
  re-measurement failed" or a re-measurement bounded only by
  `timeout x REPEATS` per position (up to 150s each at the default
  `timeout=30`). An intermediate version of this fix shrank the exhausted
  candidate back to the baseline's n and then EXCLUDED the position because
  the candidate was still below the floor there — driving `sizes_total` to
  0 and `accepted` to `False` for per-size ratios of 20x-2400x (caught in
  review before merge). The rule is now ASYMMETRIC (`_comparable_positions`,
  shared by `_speedup` and `_infer_speedup` so the headline ratio and the
  significance verdict can never disagree about which positions counted): a
  position counts whenever the BASELINE is measurable and cleared the floor,
  regardless of the candidate; a candidate too fast to register at a size
  the baseline needed real work to reach is the most decisive evidence the
  tool can produce (every candidate run beat every baseline run), so the
  already-collected samples feed the Mann-Whitney test directly, with the
  pairing disclosed as `size`/`size_after` (`per_size[].size_after` and
  `speedup.per_size[].n_after`, present only when the two n differ). A
  position is excluded and named in `inference.sizes_below_floor` only when
  the BASELINE itself is unmeasurable, never cleared the floor, or lacks
  the >=2 runs a test needs. Alignment only ever grows the CANDIDATE side, up to a baseline n that
  already cleared the floor; the baseline is never grown (an earlier
  version grew it to a padded O(1) candidate's rescaled n and timed out). Second, one shared
  wall-clock budget — `_MEASUREMENT_BUDGET_S` (120s, `time.monotonic()`) —
  is threaded through EVERY `tools._measure` call the tool makes, both
  `_timed` ladders and alignment (an earlier version bounded alignment
  only, leaving `_timed`'s own rescale rounds free to run toward the 180s
  tool deadline on a real-cost baseline); exhaustion fails fast with a
  coded, disclosed reason. Per-size executions divide the remaining budget
  by `repeats`, not by `repeats x len(sizes)`, so a legitimately slower
  largest size is not starved by cheaper siblings (measured: an even split
  left a real O(n^2) baseline's largest calibrated size 5.7s when it needed
  6.3s). The executor-call bound is at most the previous 234
  (candidate-below-floor positions are no longer re-measured). Measured on
  this box's Rust backend with a REAL payload — a `volatile`-guarded O(n^2)
  baseline against a genuine O(1) candidate at the default sizes
  `[2000, 5000, 10000, 20000]`: the full call completed in ~9.5s (~19x
  margin under 180s); a re-measurement the baseline could not afford
  failed via the budget in ~11s rather than up to 150s. The live
  O(n^2)->O(1) test now calibrates its sizes at test time from probes of
  BOTH arms (the baseline must cost >= 100 ms and >= 6x the O(1)
  candidate's spawn-plus-pad cost on that host) instead of fixed constants
  (the fixed-constant version measured a quadratic-vs-constant win at only
  1.55x on a hosted macOS runner and rejected it), and asserts the
  product's own majority rule rather than 4/4 sizes. `CONTRACT_VERSION` bumped `1.6.0` -> `1.7.0`
  (MINOR: `size_after`/`n_after` added, present only when the two arms
  ran at different n; `sizes_below_floor` semantics narrowed to the
  baseline side; no field removed or moved).

### Changed

- **The runner's own scratch files (the entry file's source copy, the
  compiled binary, and — on the Rust backend — the compile/run redirect
  files) now live inside a private `.codecalc-run/` subdirectory of the
  workdir, never at the workdir root.** Previously `session_run`/
  `execute_code(session_id=...)` unconditionally (re)wrote whatever entry
  file was currently executing to a root-level `main.<ext>`, so a session's
  own, unrelated `main.py` (or the equivalent for another language) at the
  session root was silently overwritten by running any OTHER entry file —
  documented as a known gap in 0.8.0's release notes, fixed here. The
  running PROGRAM's cwd is unchanged (still the workdir root, on both
  backends), so a program's own relative file access
  (`open("data.csv")`, a session's own files) keeps resolving exactly
  where it always has; only the runner's OWN copies move.

  **CRITICAL, closed before this shipped:** the scratch directory is now
  WIPED AND RECREATED FRESH on every call, never reused, and the whole run
  is refused with a coded `permission_denied` if anything already inside
  it (at any depth) is not a plain file or directory. An earlier version
  of this fix made `.codecalc-run/` itself symlink-safe to create but left
  every file WRITTEN inside an already-real one exposed: a session's own
  executed code, from a PRIOR call, could plant
  `.codecalc-run/main.<ext> -> <workdir>/important.txt` (the program runs
  with the workdir as its cwd, so it can reach both names), and the NEXT
  call's write of its own entry source would follow that symlink and
  overwrite `important.txt` — including, via a session_list-disclosed
  absolute workdir path, a file in a DIFFERENT session. Reproduced end to
  end on both backends before the fix; every write into the scratch
  directory now opens `O_EXCL`/`CREATE_NEW` (refusing ANY pre-existing
  entry atomically), and `tests/test_session_jail.py` carries the
  regressions (symlink, cross-session, and a planted FIFO).

  `python3`'s `sys.path[0]` would otherwise have silently stopped seeing a
  sibling module written via `session_write_file` (the entry copy's new
  directory holds nothing a caller ever wrote) — both backends now set
  `PYTHONPATH` to the workdir root for every step, restoring the same
  import resolution; a per-run `dependencies` install (which lands at the
  workdir root, same as before) is covered by the identical mechanism.
  `node` has the same class of gap for `require("./sibling")` (CommonJS
  resolves a relative specifier against the REQUIRING FILE's own
  directory; `NODE_PATH` does not reach it, only bare specifiers) — fixed
  by writing a small `Module._resolveFilename` shim into the scratch
  directory on every node run and loading it via
  `NODE_OPTIONS=--require=<path>`, retrying a failed relative lookup
  against the workdir root before giving up. `c`/`cpp`/`c++`/`fortran`'s
  compile commands gained an extra `-I`/include-search entry pointing at
  the workdir root for the same reason (`#include "helper.h"`/
  `include 'helper.inc'` resolve relative to the including file's own
  directory first).

  Three behaviour changes are DOCUMENTED, not fixed: rust's `mod helper;`
  has no search-path flag equivalent to C's `-I`, so a session's own
  sibling `.rs` file at the workdir root no longer resolves — single-file
  rust (the only shape CI's `tested`-tier evidence covers) is unaffected.
  `bun`'s `require`/`import` and `deno`/`typescript`'s ESM `import` use
  their own module resolvers (not Node's `Module` class), so the node fix
  above does not extend to them; a sibling file at the workdir root no
  longer resolves for those three either. Haskell is NOT in this list:
  GHC's default import search path is CWD-relative (`-i.`), and haskell's
  `run` cwd is the workdir root (it has no separate `compile` step): a sibling
  `Helper.hs` still resolves via `import Helper` exactly as before. What
  GHC's own build does, unrelated to this change, is write each module's
  `.hi`/`.o` next to ITS source — so `Helper.hi`/`Helper.o` land at the
  workdir root beside `Helper.hs`, a pre-existing latent collision risk
  with a same-named user file, noted here for completeness.

  `session_artifacts`/`artifacts_created` collapse
  their exclusion rule from a hand-maintained set of root-level basenames
  (`main.<ext>` per language, `a.out`/`a.exe`, Kotlin's `out.jar`, and the
  Rust backend's six `{compile,run}.{out,err,in}` files) to the single
  `.codecalc-run/` directory prefix, since the runner's files no longer
  share a directory with anything a caller can name at all. Sessionless
  `execute_code`/`execute_code_stream` share the identical layout inside
  their own per-run temp workdir.

## [0.8.0] — 2026-09-07

Three changes since 0.7.0: per-run `dependencies` extended to
`execute_code_stream`/`run_submit`, a hard `max_output_kb` ceiling with
matching result-size hints on all five large-result tools, and three new
result-contract shapes for verification/comparison tools plus the artifact
filter and session-walk fixes that came with them. The result contract
moves `1.4.0` → `1.5.0` (additive) for the three new shapes.

### Added

- **Three new result-contract shapes: `translation_verification`,
  `optimization_verification`, `edge_case_comparison`.** `verify_translation`
  and `verify_optimization` were always stamped `contract_version` at the
  same MCP tool boundary as every other tool, but matched none of the five
  published execution shapes — `docs/contract/README.md` explicitly scoped
  them OUT of the schema's "exactly one branch matches" claim rather than
  close the gap. `translation_verification` covers `verify_translation`'s
  result (`ok`/`passed`/`matched`/`mismatched`/`inconclusive`/`total`/`cases`
  plus `grade`/`grade_basis`/`grade_rules_version`). Also fixes a real
  omission in `translation.aggregate`, caught in review: it never set `ok`
  at all, when `verify_optimization`'s own `{"ok": True, "accepted": False,
  ...}` early return already established that `ok` means "the tool
  completed and produced a real answer", never the verdict — a mismatch is
  now `ok: true` exactly like a pass, and `aggregate` sets it
  unconditionally rather than the schema carving out an exception for its
  absence. `optimization_verification` covers
  `verify_optimization`'s result, including the full `inference` object
  (the per-size one-sided Mann-Whitney U test behind `accepted`) and the
  embedded `verification` field (verify_translation's BARE evidence, no
  grade/`contract_version` — that wrapper only happens at the MCP tool
  boundary, which this internal call never crosses). `compare_edge_cases`
  had the same gap in its success shape; `edge_case_comparison` closes it
  (its refusal already matched `rejected` and needed no new branch, same as
  a `verify_optimization` measurement failure). `CONTRACT_VERSION` bumped
  `1.4.0` -> `1.5.0` (MINOR: three new shapes, no existing shape moved).
  `tests/test_contract.py` runs all three tools against fixture code and
  validates the real output against the published schema.
- **Per-run `dependencies` extended to `execute_code_stream` and
  `run_submit`; `compare_execution` now discloses rather than silently
  drops.** Previously only `execute_code`/`session_run` honoured a PEP 723
  block or an explicit `dependencies` argument; the other three execution
  tools accepted neither, so a `# /// script` block in code sent to any of
  them was silently inert prose. `execute_code_stream` now takes the same
  `dependencies` argument execute_code does, with the same refusal codes
  (`capability_not_requested` under `no_net`/deny-network/strict), the same
  120s aggregate install budget, and the same sessionless workdir quota —
  the install runs BEFORE the first progress notification, so a refusal or
  a failed install is the stream's first and only event, never interleaved
  with output. `run_submit` also gained `dependencies`: the install runs on
  the SAME background worker as the code that follows it (a new
  `RunSupervisor.start(runner=...)` parameter replaces the normal
  `provider.execute` dispatch with a caller-supplied callable), so the call
  still returns a run_id immediately regardless of how long the install
  takes — only the (cheap, network-free) temp directory creation happens
  synchronously, before the run_id is minted. A failed install becomes the
  run's own terminal CODED error (a new `RunSupervisor`/`run_supervisor`
  exception, `CodedRunFailure`, carries it through `_collect()`'s existing
  exception path verbatim, with no execution receipt attached — the
  provider was never reached), inspectable via `run_inspect` exactly like
  any other outcome. The installed entries and the per-run workdir both
  ride on the run's OWN record from the moment `start()` returns, not on
  `run_submit`'s stack frame: `dependencies` appears on every terminal
  `run_inspect(run_id)` call (retained for the run's whole retention
  window), and the workdir is released on the run's first collection —
  whichever of `run_inspect`, `run_submit`'s own opportunistic reap of
  finished runs (so a fire-and-forget caller that never inspects a run
  still does not leak its workdir), or a startup sweep of the crash-recovery
  journal (a new `workdir`/`workdir_identity`/`workdir_cleaned` triple,
  persisted only for a run that has one) turns out to be. `compare_execution`
  fans out across several languages with no per-language install plumbing
  behind it (no capability broker, no per-language workdir), so a real
  per-language installer was judged out of scope; it instead REJECTS an
  explicit `dependencies` argument with a `validation` error naming the
  `dependencies` capability, and DISCLOSES — never silently drops — a
  python3 snippet's inline PEP 723 block via a new per-row
  `dependencies: {"status": "unsupported", "reason": ...}` field. No further
  result-contract bump: `execute_code_stream`/`run_submit`'s `dependencies`
  field reuses the SAME optional field the execution envelope already
  declares, and `compare_execution`'s result shape has never been part of
  the versioned contract.

### Fixed

- **`tests/test_translation_verify.py`'s live "a real O(n)->O(1) win is
  accepted" check flaked on hosted sandbox runners** (observed twice on
  macOS in `ci-python`, 0/2 on ubuntu/windows). The candidate pair's two
  smallest sizes sat close enough to process-startup/scheduling noise that
  the one-sided significance test correctly refused to certify them,
  dropping the run below a majority of sizes. The gate's math was not the
  bug — the test's candidate pair was too close to the noise floor at the
  smallest sizes. Fixed by giving the same O(n)->O(1) pair ~10x more work
  per element so every tested size clears the floor with margin, not by
  weakening `codecalc/optimization.py`'s acceptance rule.
- `run_inspect` did not carry `_meta["anthropic/maxResultSizeChars"]`, even
  though its terminal reply (once a `run_submit`-started run finishes) is the
  same execution envelope `execute_code` returns and can approach the same
  output cap — it now carries the same value the other four large-result
  tools do.
- `session_artifacts`/`artifacts_created` no longer hide a user's own file
  just because it shares a basename (`a.out`, `main.<ext>`, `run.out`, ...)
  with one of the runner's own scratch files somewhere else in the
  workspace — e.g. `gcc -o a.out program.c` run in a session subdirectory
  now reports the binary. The exclusion is now scoped to the exact,
  root-level paths the runner actually writes to, not the basename anywhere
  in the tree, and is now derived from the language registry (every
  `main.<ext>` the registry knows, not a hand-picked subset) rather than a
  hand-maintained list, so adding a language can no longer reopen this bug
  for it. `a.exe` (the compiled-output slot on Windows), Kotlin's
  compile-time `out.jar`, and the session lock file
  (`.codecalc-session-lock`) are now excluded too — none of them were
  previously in the excluded set at all. `run.out`/`run.err`/`run.in` and
  `compile.out`/`compile.err`/`compile.in`, all six of which the Rust
  execution backend genuinely writes on every call (`run_step()` in
  executor/src/main.rs, verified against a real Rust-backend run — a first
  pass at this fix wrongly concluded, from a plain-string grep that cannot
  see the Rust code's runtime `format!("{tag}.out")`, that none of the six
  were written anywhere and pruned them), stay excluded.
- A run whose output spills to `.codecalc-spill/` no longer reports the
  spill file itself as a new artifact in `artifacts_created` — it was
  duplicating `stdout_spill`/`stderr_spill`, which already point at it.
- Documented, not fixed here (a fix is filed separately): `session_run`
  and `execute_code(session_id=...)` always rewrite the session's
  root-level `main.<ext>` scratch file with whatever entry file's source is
  currently executing, regardless of that entry file's actual name — so a
  session's own, unrelated `main.py` (or the equivalent for another
  language) at the session root is silently overwritten by running any
  OTHER entry file.

### Changed

- **A session-scoped run (`session_run`, `execute_code(session_id=...)`) walked
  its own workspace directory up to 6 times per call** — `quota_precheck`'s
  disk-usage AND artifact-count checks (2 separate `rglob`s), the pre-run
  artifact snapshot #263 added, and `quota_postcheck`'s classify AND
  disk-usage AND artifact-count checks (3 more), none aware any of the
  others had already walked the same, unchanged-in-between directory.
  Measured with `hyperfine` (20 runs, Rust backend, min of run) on
  workspaces of 0 / 500 (the default `CODECALC_MAX_ARTIFACT_COUNT` cap) /
  5000 (10x the cap) small nested files: at the cap, #263 added 64ms
  (303.5ms at the pre-#263 commit -> 367.5ms on main, +17.4%); at 10x the
  cap, 599ms (572.2ms -> 1171.2ms, +51.1%) — material by both of this
  ticket's bars (>50ms absolute, >10% of trivial-run latency at the cap).
  This fix cuts that back to 329.3ms at the cap (-38.2ms/-10.4% from
  main) and 747.0ms at 10x (-424.2ms/-36.2%): a `_workspace_scan(d)`
  helper builds the filtered artifact listing AND the unfiltered
  disk-usage total in ONE `rglob` pass — a microbenchmark showed doing
  both costs no more than the filtered listing alone already did (163ms
  vs 165ms median at 5000 files) — and `quota_precheck`/`quota_postcheck`/
  `_artifact_snapshot`/`_classify_new_artifacts`/`_artifact_count_refusal`/
  `_disk_quota_refusal` now each accept an optional pre-computed scan (or
  its `entries`/`total_bytes` half) that `sessions.execute()`/
  `execution_service.SessionService.run_file()` share across their own
  before/after pair — 2 walks per call instead of 6 (`run_file()` still
  forces a fresh post-install scan when dependencies were actually
  installed, reusing the pre-install one otherwise — see
  `_artifact_snapshot`'s docstring). Every function still takes its own
  independent walk when called bare (bare is what every existing test
  does), so nothing outside that shared before/after pair changes shape or
  cost. A residual gap remains versus the pre-#263 numbers (329.3ms/747.0ms
  vs 303.5ms/572.2ms) — not walk COUNT (2 here vs pre-#263's 4) but
  per-file COST: #268's more precise runner-internal-file exclusion (a
  relative-path-parts check per file, not a top-level basename compare)
  made every filtered walk ~48% pricier standalone regardless of this fix
  (measured: 180.0ms vs 121.8ms median per call at 5000 files) — a
  separately-justified correctness fix, not something this change should
  undo.
- A caller-supplied `max_output_kb` used to reach the executor unclamped and
  push a tool's real output past the `anthropic/maxResultSizeChars` value it
  advertises (`execute_code`/`execute_code_stream`/`run_submit`, noted as a
  caveat in each docstring since 0.7.0); there was no other ceiling on this
  parameter, so it is now clamped at the MCP boundary on all three tools to
  a new hard ceiling of 240 KiB per stream — separate from the existing
  64 KiB DEFAULT `max_output_kb=0` selects — chosen as the largest
  round-KiB figure that keeps the advertised hint under Claude Code's
  documented 500,000-character maximum for this `_meta` field's TEXT
  content. `anthropic/maxResultSizeChars` moves from 139,072 to 499,520
  (`2 * 240 KiB + 8_000`) on all five large-result tools accordingly, and
  the docstrings' caveat has been rewritten to describe the ceiling rather
  than the gap. That value bounds the serialized text `content` block only:
  a typed tool (every one of the five except `session_run`) also carries an
  equal-sized `structuredContent`, so its total wire payload approaches
  twice this hint; `session_run`'s inlined artifact blocks are likewise
  separate from it.

## [0.7.0] — 2026-09-07

Four changes since 0.6.0: inline artifacts from session-scoped runs,
`verify_optimization`'s accept decision backed by a significance test,
per-tool `ToolAnnotations`/`outputSchema`/server-side policy `_meta`, and
per-run dependencies for `execute_code`/`session_run`. The result contract
moves `1.3.0` → `1.4.0` (additive) for the fields the last three add.

### Changed

- **`verify_optimization` now requires statistical significance, not just a
  ratio.** With 3 runs per side (the old `repeats`), the smallest one-sided
  p a single size could ever produce was 1/20 — no size could reach the
  conventional alpha=0.05 no matter how clean the separation was, so
  "median ratio cleared `min_speedup`" was an arithmetic fact about noisy
  timings, not evidence of a real difference. `repeats` is now 5 (floor
  1/252), `_timed` keeps every run (`all_runs_ms`), not just the min, and a
  new dependency-free `codecalc/stats.py` runs a one-sided Mann-Whitney U
  test per size (exact enumeration for n1+n2<=20, normal approximation with
  tie correction beyond that — ported from mrnh/rigor, MIT). `accepted`
  now additionally requires a MAJORITY of measured sizes to reject "not
  faster" at alpha=0.05; the result gains an `inference` field with the
  per-size U statistic, p-value, and rank-biserial effect size behind the
  verdict. Also fixes a latent size-misalignment bug the significance test
  exposed: `_timed(original)` and `_timed(candidate)` each auto-scale their
  own `sizes` independently, so when only one side needed to rescale (the
  common case — a slow baseline is visible immediately, a genuinely fast
  candidate is not) the two `sizes` lists diverged while staying the same
  length, and `_speedup`/`_infer_speedup` zipped them by position — a
  per-size ratio or p-value could be labelled with the wrong size entirely.
  New `_align_sizes` re-measures whichever side scaled less at the other
  side's final sizes before either function sees the pair. Confirmed the
  new worst case (~13.4s, 234 executor calls, up from 214 before this fix)
  stays well inside `verify_optimization`'s 180s deadline. `grades.py`'s
  `grade_verify_optimization` basis text now names the significance result
  (sizes significant / alpha) alongside the ratio when `inference` is
  present — text describing the same evidence, not a change to what
  evidence maps to what grade, so `GRADE_RULES_VERSION` stays `2`.
- **CI gap closed: `tests/test_translation_verify.py`'s native-executor-gated
  checks had never once run against the real executor.** The `tests` job
  never builds the Rust binary (that split is the `sandbox` job's whole
  reason to exist), so every `if executor._rust:` block in that file —
  including the live O(n)->O(1) win and the new identical-code
  false-accept-rate check — silently self-skipped in CI every time.
  `ci-python.yml`'s `sandbox` job now runs the same file a second time
  after confirming the Rust backend, same pattern as
  `test_platform_contract.py`'s and `test_mcp_all.py`'s existing two-job
  invocations.

### Added

- **Per-tool `ToolAnnotations` (readOnlyHint/destructiveHint/idempotentHint/
  openWorldHint) on all 52 tools, and typed return schemas on 49 of them.**
  Every tool now declares a group-derived (with named per-tool overrides)
  `ToolAnnotations` and a human-readable `title`. Every bare `-> dict`
  return became `-> dict[str, Any]` EXCEPT `session_read_file` and
  `session_run` (both can return something other than a dict — a raw
  `ImageContent`, or a list of content blocks when a run produced
  artifacts — and typing that union wraps every reply in `{"result": ...}`,
  which for `session_run` fails the SDK's own output validation on the list
  branch; both keep their pre-existing untyped return, exactly as before);
  `list_languages`/`list_execution_providers` were already `-> list[dict]`,
  which the SDK schematises fine without a change. `dict[str, Any]` is what
  actually flips a tool onto the SDK's structured-output path (verified: a
  bare `-> dict` cannot be schematised at all). `tools/list` now carries an
  `outputSchema` for every typed tool and every `tools/call` reply from one
  carries `structuredContent` alongside its unchanged text block — see
  `docs/contract/README.md`'s updated 2026-09-07 note for what that schema
  actually contains (a permissive `{"type": "object"}`, not this repo's
  detailed result contract), which two tools stay untyped and why, and the
  caveat that some MCP clients let `structuredContent` displace the text
  block's default display without dropping anything. `contract_version` and
  `code` are asserted present in `structuredContent` now that a caller can
  read it structurally. A caller-supplied `max_output_kb` above the default
  64 KiB can push `execute_code`/`execute_code_stream`/`run_submit`'s real
  output past their advertised `anthropic/maxResultSizeChars` below — noted
  in each tool's docstring.
  `CONTRACT_VERSION` bumped `1.3.0` -> `1.4.0` (MINOR: the wire gained a
  capability, no result shape moved). New `tests/test_tool_annotations.py`
  asserts every one of the 52 registered tools carries a complete
  annotation, that no `calculator`-group tool is marked non-read-only, and
  that `outputSchema` is `None` on exactly the two tools left untyped.
- **Server-side policy `_meta` on select tools, read by Claude Code
  (`code.claude.com/docs/en/mcp`, retrieved 2026-09-07).**
  `_meta["anthropic/requiresUserInteraction"] = true` on `install_package`
  and `update_runtimes` (both mutate the host and fetch from a registry) —
  forces a permission prompt on every call even under
  acceptEdits/auto/bypassPermissions. `_meta["anthropic/alwaysLoad"] = true`
  on `calc_exact`, `execute_code`, `verify_translation`,
  `verify_optimization`, `list_languages` — five entry-point tools that stay
  loaded when a client defers the rest of the surface via tool search.
  `_meta["anthropic/maxResultSizeChars"]` on `execute_code`,
  `execute_code_stream`, `session_run`, `compare_execution` — set to
  `2 * executor.MAX_OUTPUT_BYTES + 8_000` (139,072), derived from the two
  independently-capped output streams plus envelope overhead, not
  Anthropic's 500,000-char ceiling. New `tests/test_tool_meta.py` asserts
  `tools/list` carries exactly these `_meta` keys on exactly these tools and
  no others.
- **Inline artifacts from session-scoped runs.** `session_run` and
  `execute_code(session_id=...)` — both of which run in a session workspace
  that outlives the call, unlike sessionless `execute_code`'s Rust-owned temp
  directory — now report `artifacts_created`: the files a run just created
  or modified, as `{path, size, mime, resource}` (`resource` is the same
  `codecalc://session/{sid}/files/{path}` URI `session_read_file` already
  serves). `session_run` additionally inlines eligible artifacts as MCP
  content blocks alongside its JSON result: a PNG/JPEG/GIF/WebP up to 1 MiB
  raw as an `ImageContent` block, a CSV/plain-text/HTML/JSON file up to
  64 KiB as an `EmbeddedResource`, and everything else — including an
  oversize file of either kind — as a `ResourceLink` (a pointer, not
  attached bytes). At most 8 such blocks and 4 MiB of inline bytes ON THE
  WIRE per reply — an image's base64 encoding (~4/3 its raw size), not its
  raw file size, is what the budget charges, so a reply's actual attached
  bytes can never exceed the 4 MiB promised regardless of mime mix; beyond
  either cap the remaining artifacts still appear in `artifacts_created`
  and the result carries `truncated_inline: true`.
  `compact_result` never drops `artifacts_created`/`truncated_inline` for
  the same reason it never drops `unenforced`/`output_error` (#117): a
  compact caller is the one least able to discover a new file any other
  way. The content blocks themselves are transport-level and outside the
  result contract. This is also why `session_run` stays off the typed-return
  list above: it can return `[TextContent, *artifact blocks]` rather than a
  dict, and typing that union fails the SDK's own output validation on the
  list branch — see `codecalc/server.py`'s comment above the tool.
  `CONTRACT_VERSION` moves `1.3.0` → **`1.4.0`** (a MINOR add) for the two
  new JSON fields on the session/envelope/compact shapes — the same MINOR
  bump the annotations/`outputSchema`/`_meta` change above shares; both
  landed against the same `1.3.0` base, so there is one bump, not two.
- **Per-run dependencies for `execute_code` and `session_run`.** A PEP 723
  inline script metadata block (`# /// script` ... `# ///`, python3 only) or an
  explicit `dependencies: list[str]` tool argument (the only mechanism for
  node; a convenience for python3 that MERGES with the block, deduped by PEP
  503 normalized name with the argument winning a conflict) installs packages
  BEFORE the code runs. The fetch never runs inside the sandboxed step: every
  dependency goes through the existing confined `install_package` path
  (allowlist, argv-injection checks, `--only-binary=:all:`/`--ignore-scripts`,
  Landlock/Seatbelt confinement) first, targeting the run's own workdir — a
  sessionless run gets one pre-created for it and removed afterward via the
  same identity-checked deletion the executor uses for its own temp
  directories. A dependency-bearing run is refused (`capability_not_requested`,
  no fetch attempted) when `no_net=True` was requested or the active
  `CODECALC_CAPABILITY_POLICY` denies or strictly limits network — the
  sandboxed step's own `--no-net` is unrelated and unchanged. Two more
  ceilings, separate from the run's own `timeout`: a fixed, aggregate
  install-time BUDGET across every dependency of one run (120s,
  `DEFAULT_DEPENDENCY_INSTALL_BUDGET_SECONDS`; `packages.install()` gained a
  `timeout` parameter, default 600s unchanged, so this can bound each
  install by the remaining budget) — exceeding it refuses the run with a
  stamped `timeout` naming the budget and how far it got, before the run's
  own `timeout` clock even starts; and, for a sessionless run, a disk QUOTA
  on the per-run dependency workdir, reusing `CODECALC_SESSION_DISK_QUOTA_MB`
  (checked after each successful install) rather than a second constant —
  exceeding it refuses with a stamped `resource_exhausted` naming the
  measured size and the cap. A PEP 723 block ALONE, with no `dependencies`
  argument, is enough to trigger an install — this is audited distinctly
  (`audit.DEPENDENCY_INSTALL_IMPLICIT`) from an explicit
  `install_package`/`dependencies=` call, and `SessionService` gained an
  `audit` parameter (server.py now passes it) so `session_run`'s installs are
  audited too. New optional `dependencies` field on the execution result,
  landing under the SAME `1.4.0` (no further bump — an additional field on
  the version the sibling entries above already moved to):
  `[{spec, language, ok, installer, elapsed_ms, unenforced?}]`. New
  `codecalc/dependencies.py`; `packages.install()` also gained a `workdir`
  parameter for a sessionless target directory. `dependencies.refusal_result`
  names `network` in `requested_capabilities` (not `[]`), matching
  `capabilities.rejection_result`'s own shape. README's "Network boundary"
  table (plus a paragraph on the implicit trigger and the two ceilings) and
  SECURITY.md's "Explicitly out of scope" list each cover this;
  `execute_code_stream`/`run_submit`/`compare_execution` docstrings now say
  a PEP 723 block is inert there (none of them read one).

### Docs

- **Six weakest tool descriptions rewritten to disambiguate from a sibling,
  per Glama's coherence review.** `evaluate_expression`, `simplify_expression`,
  `solve_expression`, `calc_stats`, `data_sizes`, and `human_duration` were
  the shortest tool docstrings in `codecalc/server.py` and none named a
  sibling tool to distinguish itself from — `evaluate_expression`'s own text
  said "evaluate or simplify", directly claiming `simplify_expression`'s job.
  Each now follows a four-part shape (what it returns, the sibling it is the
  alternative to and the condition that decides between them, a natural
  follow-up tool, and the return shape) modelled on `matrix`'s docstring,
  which Glama scored 5/5 on disambiguation. The MCP tool description IS the
  docstring the SDK exposes in `tools/list` — this is a documentation-only
  change, no behavior moved. `scripts/tool_select_eval.py --baseline` (the
  lexical tool-selection regression gate) improved on all three `--tools`
  presets (full 118→123, dev 96→101, core 63→68 top-1 hits out of a prior
  measured run; net +4/+5/+5 against the checked-in baseline) — a sibling's
  name in a description adds discriminating vocabulary to the very prompts
  it exists to disambiguate. `scripts/data/tool_select_baseline.json`
  regenerated from this change.

## [0.6.0] — 2026-09-06

### Added

- **MCPB bundle (Claude Desktop extension), built and attached to every
  release** (#258). `mcpb/` at the repo root (manifest, `pyproject.toml`,
  `.mcpbignore`, `src/server.py`) packs a one-click Claude Desktop install.
  Uses the manifest's **`uv`** server type rather than the legacy `python`
  type: Claude Desktop's own managed `uv` resolves `codecalc[full]` from
  PyPI into an isolated venv on first launch, so nothing codecalc-specific
  is vendored into the bundle (3,845 bytes, 3 files). A PEP 508 environment
  marker (`sys_platform != 'win32' or platform_machine != 'ARM64'`) on the
  `codecalc[full]==<version>` dependency excludes Windows-on-ARM64, because
  `cryptography` (pulled in via `mcp -> pyjwt[crypto]`) ships no
  `win_arm64` wheel on PyPI; `mcpb/src/server.py` checks for that platform
  before importing `codecalc` at all and exits with a one-line cause and
  workaround instead of a bare `ModuleNotFoundError`. `manifest.json`'s
  `long_description` discloses the resulting undisclosed-until-now
  behaviour up front: first launch resolves ~120 MB from PyPI over the
  network and fails without one, with nothing cached yet. New
  `tests/test_mcpb_manifest.py` (58th test file) asserts the manifest
  shape, the version/pin match `codecalc.__version__`, the marker text, and
  — behaviourally, via a subprocess with `sys.platform`/`platform.machine()`
  monkeypatched — that the ARM64 guard actually fires clean and
  traceback-free. `scripts/check_version.py` now gates `mcpb/manifest.json`
  and `mcpb/pyproject.toml`'s pin alongside the other version sites. A new
  `build-mcpb` CI job builds the bundle and feeds it through the release
  job's existing SHA256SUMS/attestation/upload steps; publish-pypi/
  publish-testpypi/publish-crates do not depend on it.
- **`glama.json`** at the repo root, naming the maintainer for Glama author
  verification on the server's Glama listing (#255).
- **`MAINTAINERS.md`** now names the confirmed maintainer handle instead of
  a placeholder, agreeing with `glama.json` (#256).

### Changed

- **`rust` and `go` promoted to the `tested` reliability tier** (from
  `best_effort`), observable in `list_languages`, `runtimes_status`, and
  `doctor`. Not a label flip: a new CI-wired harness
  (`tests/test_tier_evidence.py`) compiles and runs a real program in each
  through the execute path and asserts the computed stdout on every PR, and
  on the Linux CI leg a missing toolchain **fails** rather than skips
  (`CODECALC_REQUIRE_TIER_EVIDENCE=1`). `scripts/check_runtime_tiers.py` now
  derives the `tested` set from that harness's literal source too, and
  additionally asserts the evidence STEP itself is live — invokes the
  harness, sets the skip-promoting flag in its own env, gates on the real
  Linux condition, no `continue-on-error` — proven fail-first in
  `tests/test_runtime_tiers.py` for a weakened registry claim, a dropped
  language, a stripped flag, and an `if:`-disabled step. The harness bakes a
  per-run nonce into each program and requires that exact computed line as
  the entire stdout, so a stale artifact or replayed result cannot pass.
  `csharp` deliberately stays `best_effort`: its recorded host-toolchain
  breakage is the tier system's founding counterexample, and promoting it is
  a separate decision.

### Fixed

- **`scripts/build_mcpb.py`'s retry now covers only the npx network fetch**
  (#259). The retry wrapper added in #258 retried *any*
  `subprocess.CalledProcessError` three times with linear backoff, which
  also retried `mcpb validate`/`mcpb pack` semantic failures — a genuinely
  broken manifest took three attempts and ~6s to report instead of failing
  on its first non-zero exit. Split into `retry_run()` (3 attempts, linear
  backoff, wraps only `npx -y <cli>@<version> --version`, the network step)
  and `run()` (executes `validate`/`pack` exactly once each, now that both
  are local/deterministic once the CLI is cached), so a broken manifest
  surfaces the CLI's own stderr immediately.

### Docs

- **Novice on-ramp: README reordered, new QUICKSTART.md added.** The README's
  top previously put ~50 lines of network-boundary caveats (prose, two
  tables, the grammar-download deep-dive) between the pitch and the Install
  section — a newcomer hit maintainer-facing detail before learning how to
  install. That content moved, verbatim, to its own "## Network boundary"
  section after Install; nothing was deleted or reworded for tone. A 2-line
  quickstart pointer now follows the pitch. The stale "After the first
  release, this becomes the install" wording (0.5.0 has been published since
  #251) is now present-tense ("The published install"), and it's ordered
  before "From source" so the simple path comes first. `QUICKSTART.md` is a
  new, standalone ≤80-line first-timer doc: what it is, install, connect to
  an MCP client (both `setup --write` and copy-paste JSON), `codecalc
  doctor`, a prominent untrusted-code safety note linking SECURITY.md, and
  where to go next.
- **One-click Cursor and VS Code MCP install badges, plus a Glama score
  badge, in the README** (#257). Both badges register the recommended
  `uvx 'codecalc[full]'` Full edition, matching the README's own per-client
  config blocks, rather than the Core edition (whose symbolic/parsing tools
  return `dependency_missing`).

## [0.5.0] — 2026-08-22

### Security

- **BREAKING (strict-policy hole closed): `network_control` no longer means
  "a native executor binary is present" — it now means "this host can
  enforce `no_net` in the kernel"**, and a `strict` capability policy that
  used to approve a network denial on a host it could not actually enforce
  now correctly rejects it with `CAPABILITY_UNENFORCEABLE`.
  `providers.LocalExecutionProvider.describe()`'s `network_control` capability
  was `executor.backend() == "rust"` alone — `true` for ANY host with the
  Rust binary present, including macOS (`no_net` there is only the
  best-effort DYLD symbol shim) and a Linux kernel without seccomp (the same
  shim fallback). `codecalc/capabilities.py`'s `strict` policy trusts that
  flag, unchecked, to decide whether a requested denial of `network` can be
  enforced — so on exactly the hosts where enforcement is weakest, `strict`
  APPROVED the run instead of refusing it, violating strict's own contract
  ("refuse rather than run unenforced"), and the approved run could then come
  back disclosing `no_net` in `unenforced` at the same time the broker's
  receipt claimed the denial was enforced. The Rust executor now answers a
  new `--capabilities` probe (`{"no_net_kernel_enforcement": bool}` — Linux
  `seccomp::available()`, `false` on macOS/Windows), `executor.py` caches it
  once at import the same way it caches `_rust`, and `network_control` is now
  `backend() == "rust" AND` that flag. Non-strict `deny-network` is
  unaffected — a host without kernel enforcement already took the
  disclose-the-leak path, not a hard failure; this only changes what
  `strict` does with a request it previously wrongly approved. Verified both
  ways: a faked shim-only host now gets `CAPABILITY_UNENFORCEABLE` under
  `strict` (the run never reaches the provider), and the live positive
  control on seccomp-capable Linux still gets approved and genuinely
  enforced, with no `no_net` disclosure in `unenforced` — the broker and the
  run's own disclosure now agree. (Hardened further below: the probe now
  proves the filter is INSTALLABLE, not merely configured, and the Python
  cache is bound to the binary's identity rather than its path.)
- **The `--capabilities` no_net probe now proves the seccomp filter is
  INSTALLABLE, and its Python-side cache no longer trusts a stale answer
  after the binary it probed is replaced.** A cross-vendor security review of
  the `network_control`-from-probe change above found no reportable
  vulnerability — every path already failed closed — but flagged three
  refinements, all fixed here:
  - The Rust probe (`seccomp::available()`, `PR_GET_SECCOMP`) proved seccomp
    was CONFIGURED, not that a filter was INSTALLABLE: `CONFIG_SECCOMP=y`
    with `CONFIG_SECCOMP_FILTER=n`, or an inherited policy denying
    `PR_SET_NO_NEW_PRIVS`, would report `true` while the real
    `PR_SET_SECCOMP` install still failed (a real run still failed closed —
    execution aborts pre-payload — but the *reported capability* would have
    been wrong). `no_net_kernel_enforcement_available()` now goes through
    `seccomp::installable()`: a disposable forked child installs the exact
    program `spawn_and_wait` installs (`seccomp::program()`, never a
    parallel copy) and reports success/failure via its exit code, running no
    payload. Startup-only — once per `--capabilities` invocation, not on the
    per-execution spawn path, which keeps using the cheaper `available()`
    check and its own existing fail-closed re-derivation.
  - `executor.py` cached the probe result once at import, keyed on nothing —
    if the binary at that path were later replaced (same-UID/deploy write
    access) with a weaker build, the stale answer would persist for the
    server's remaining life. The cache is now bound to the binary's on-disk
    identity (device, inode, size, mtime via `executor._binary_identity()`)
    and re-probes whenever that identity changes; a stat call is cheap
    enough to do on every read.
  - The import-time probe used a bare `subprocess.run(timeout=15)`, which on
    `TimeoutExpired` kills only the direct child — a wrapped or faulty
    binary that had already spawned a descendant would leak it, the exact
    class of bug `_popen_group`/`_kill_group` exist to close on every other
    path this module spawns the executor. The probe now uses those same
    process-group helpers.
- **A second cross-vendor pass on the probe-hardening fix directly above
  found three further defects in it, two of which defeated its own
  fail-safe goal — all fixed here:**
  - `bool(data.get("no_net_kernel_enforcement"))` read a malformed or failed
    probe as truthy in cases that must be `False`: `bool("false")` and
    `bool(1)` are both `True` in Python, and `proc.returncode` was never
    checked at all, so a binary that printed `true` and then exited nonzero
    was trusted anyway. `_probe_no_net_kernel_enforcement()` now requires
    `proc.returncode == 0` **and** `data.get("no_net_kernel_enforcement")
    is True` — an identity check, not a truthiness check.
  - The timeout-handling fix directly above could itself hang: `_kill_group`
    only `proc.wait()`s on the DIRECT child after SIGTERM, so it returns the
    instant that one process exits — even while a descendant that ignores
    SIGTERM is still alive holding the probe's captured stdout/stderr pipes
    open, which left the following `proc.communicate()` blocking forever
    waiting for an EOF that would never come (reproduced: direct child
    exits `-15`, grandchild alive, drain hangs). Fixed by unconditionally
    escalating to `_reap_group` (SIGKILL, which cannot be ignored) across
    the whole process group before a now BOUNDED final drain.
  - The cache-identity fix directly above published the new binary identity
    and the answer the re-probe produced as two separate steps. A
    concurrent reader could land in the window between them — a subprocess
    call releases the GIL for the whole time it blocks — see the
    already-updated identity, conclude no re-probe was needed, and hand back
    the OLD cached answer while the fresh probe for that very identity was
    still in flight (reproduced: a concurrent reader returned a stale `True`
    while the real re-probe was producing `False`). Fixed by publishing the
    identity and the answer together, inside
    `_NO_NET_KERNEL_ENFORCEMENT_LOCK`, so a concurrent reader hitting a
    changed identity now waits for the same fresh answer instead of racing
    past it.
- **`no_net`'s result now discloses that the Rust executor's block is a
  best-effort symbol shim, not a kernel egress block** (E-1).
  `--no-net` intercepts `socket()`/`connect()` via `LD_PRELOAD` on Linux and
  `DYLD_INSERT_LIBRARIES` on macOS — ELF/dyld symbol interposition, which only
  covers calls that resolve those names through the ordinary dynamic symbol
  table. Verified live: with the shim applied and `no_net=True`,
  `socket.socket(AF_INET)` gets `EACCES` as documented, but
  `ctypes.CDLL(find_library("c")).socket(2, 1, 0)` — which pulls the symbol
  out of libc's own table via `dlsym` on a specific handle rather than through
  that global scope — returns a working fd, and the result's `unenforced`
  still came back `[]`, falsely reading as fully enforced. `executor.py` now
  appends a disclosure to `unenforced` whenever `no_net` was requested and
  satisfied by the shim (as opposed to the shim being altogether unavailable,
  already disclosed separately): `"no_net: best-effort LD_PRELOAD/dyld symbol
  shim — a dynamically-linked ctypes/dlsym or raw-syscall network call
  bypasses it; use the strict (gVisor) backend for a real egress block"`.
  `AUDIT.md` and `executor/blocknet.c`'s own comments corrected to name this
  bypass alongside the already-documented static-linking one.
- **`no_net` is now enforced in the kernel on Linux by a seccomp-bpf filter,
  not just the bypassable symbol shim** (the strong close of E-1).
  The executor installs a seccomp filter in the child, in the async-signal-safe
  `pre_exec` alongside the rlimits, that refuses — in the kernel — every
  syscall-level way unprivileged code can open an inet socket: `socket()` by
  domain, **`io_uring`** (which can open and connect a socket inside the kernel
  without a `socket()`/`connect()` syscall — the path an adversarial review
  caught the first cut missing), and the **x32**/foreign-arch compat range. So
  the `ctypes`/`dlsym`/raw-syscall call that walked around `blocknet.so` (E-1)
  now takes `EACCES` at the kernel boundary. `AF_UNIX` and other domains are left
  alone (runtimes use them). Verified live on aarch64: `libc.socket(2, 1, 0)` →
  `EACCES`, `io_uring_setup` → `ENOSYS`, `socket.socketpair()` and `asyncio`
  still work. When the seccomp filter enforces `no_net`, the result reports it as
  genuinely applied — no best-effort disclosure, because the guarantee is now
  real; on macOS, or a Linux kernel without seccomp, it falls back to the symbol
  shim and keeps the
  E-1 disclosure. **Fail-closed:** a requested `no_net` the kernel refuses to
  enforce fails the run rather than executing the caller's code unprotected.
  The strict (gVisor) backend remains the stronger tier (egress blocked for the
  whole container).

### Added

- **`codecalc setup` now recommends a leaner `CODECALC_TOOLS` group** when
  the variable is unset: a line naming the real, live per-preset tool counts
  AND real per-preset group membership (`core`/`dev` — both read from
  `server.py`'s own `TOOL_GROUPS`/`PRESETS`, so neither the counts nor the
  displayed group list can drift from what `CODECALC_TOOLS=core`/`=dev`
  actually registers; cross-vendor review caught an earlier cut of this that
  derived the counts but still hardcoded the group-membership description)
  for callers that do not need sessions, package installs, or code
  execution. Purely informational: the default surface (`CODECALC_TOOLS`
  unset = all 52 tools) is unchanged, and the line is suppressed once
  `CODECALC_TOOLS` is already set. The `server.py` import this needs is
  scoped to exactly that branch and wrapped so a broken or absent import
  (e.g. a library caller that never went through the CLI entry point, `mcp`
  not installed) silently skips the tip rather than crashing `run_setup()`.
  See README's "Reducing the tool surface".
- **`scripts/fuzz.py`**: a mutation fuzzer over codecalc's two
  highest-risk caller-string surfaces — `safe_expr.classify_unsafe`/
  `safe_parse` (the screen between a caller expression and SymPy's
  `parse_expr`) and `sessions._jail`/`_session_dir` (the traversal guard
  between a caller path and a session workspace write). Asserts, for every
  generated input, that neither surface ever raises an uncaught exception,
  invokes a blocking builtin, or hangs unboundedly. `tests/test_fuzz_smoke.py`
  runs a small fixed-iteration pass in CI (wired into `ci-quality.yml`); the
  full multi-thousand-iteration campaign is a manual/deeper run (see the
  script's own `--help` and module docstring, including two DoS-shaped
  findings it surfaced past the space today's callers can reach, filed for
  follow-up rather than fixed here).
- **`scripts/tool_select_eval.py`**: an offline tool-SELECTION eval — a
  labeled set of 196 plain-language prompts, scored by whether a
  deterministic BM25 lexical selector picks the right codecalc tool against
  the CURRENT `tools/list` names and descriptions. It is the gate a future
  tool-description trim (see "Tool-definition token cost" in the README) has
  to clear before it ships: v1 measured baseline is top-1/top-3 accuracy of
  60.71%/75.51% on `--tools full` (62.75%/76.47% on `dev`, 63.0%/77.0% on
  `core`), checked in at `scripts/data/tool_select_baseline.json` alongside a
  `prompt_set_sha256` that PINS the exact labeled corpus — a `--baseline`
  compare against a corpus that no longer hashes to it fails with a distinct
  "corpus changed" error instead of silently scoring a smaller, easier
  prompt set against the old numbers (a real gap: deleting every 4th prompt
  from the 196-prompt v1 set still cleared the old "3 prompts/tool" floor
  while RAISING measured accuracy). The compare itself is over exact integer
  top-1 hit counts on that pinned corpus, not a float ratio, so a
  non-numeric or negative `--epsilon` fails at argument-parsing rather than
  silently comparing open. Every labeled prompt is validated to exclude its
  own target tool's name and underscore-tokens (singular/plural matched both
  directions), so the eval cannot be gamed by echoing a tool name back at
  it — it can only pass by the description carrying real discriminating
  vocabulary. `--self-check` is the positive control: a full one-at-a-time
  ablation SWEEP over every candidate tool (no sampling — a fixed random
  sample is exactly what let three real ablations pass CI unnoticed in an
  earlier cut), run separately against each of `full`/`dev`/`core` since a
  tool can be top-1-wrong against `full`'s 51 distractors (zero headroom to
  lose) while still showing real, ablation-detectable headroom against
  `core`'s much smaller distractor set. Measured pooled hit loss: 111/119
  (`full`), 87/95 (`dev`), 51/63 (`core`) baseline hits lost across their
  respective sweeps; exits non-zero if either pooled-loss or breadth floor
  is not cleared — proof the gate can actually detect description damage,
  not just report a number. `tests/test_tool_select_eval.py` wires the eval,
  the self-check, and the baseline-regression compare into CI
  (`ci-quality.yml`) for all three tool surfaces. It is a lexical proxy for
  model tool-selection, not a model — see the script's own module docstring
  for what it can and cannot detect.
- **ClusterFuzzLite coverage-guided fuzzing** (`.clusterfuzzlite/`, `fuzz/`,
  `.github/workflows/cflite_pr.yml`/`cflite_batch.yml`): the same OSS-Fuzz
  engine (libFuzzer + atheris + sanitizers) run in this repo's own CI, the
  complement to `scripts/fuzz.py`'s deterministic seeded smoke gate — one is a
  fast reproducible gate, the other discovers new paths the seed corpus never
  named. The atheris harnesses reuse `scripts/fuzz.py`'s seed corpus and
  contract (no duplication); actions are SHA-pinned like the rest of the repo.
  It found the `UnicodeDecodeError` crash fixed below on its first run.

### Fixed

- **`safe_expr.classify_unsafe`** no longer raises an uncaught
  `UnicodeEncodeError` on an expression containing a lone UTF-16 surrogate
  (e.g. `"\ud800"`); it now returns the same `(category, message)` refusal as
  any other unparsable input. Found by `scripts/fuzz.py`.
- **`safe_expr.classify_unsafe`** likewise no longer raises an uncaught
  `UnicodeDecodeError` on an expression containing a bare replacement or
  truncated-multibyte char (e.g. `"�\r�"`) — the same class of C
  tokenizer round-trip crash as the surrogate above, and now caught the same
  way, with the same validation verdict (never passed to SymPy as safe). Found
  by the new ClusterFuzzLite coverage-guided harness, which reached a shape the
  seeded mutator never produced.
- **`safe_expr._walk`** (used by `reject_explosive`, reached from every
  symbolic tool via `safe_parse`) no longer raises an uncaught `TypeError`
  when the parsed expression is a bare reference to a heavy-function name
  with no call parens (e.g. the expression `"binomial"`, not `"binomial(5,2)"`)
  — such a reference resolves to a SymPy `FunctionClass`, whose `.args`
  attribute is an unbound `property` object rather than a tuple. Found by
  `scripts/fuzz.py`.
- **`sessions._jail`** now refuses a session file `path` whose raw
  length exceeds 4096 chars or whose segment count exceeds 256, BEFORE
  calling `Path.resolve()` on it — a `resource_exhausted` refusal in
  microseconds instead of the multi-second-to-tens-of-seconds server CPU cost
  `Path.resolve()` itself takes on a many-segment string (measured: ~1.3s at
  10k segments, ~3.4s at 20k, worse than linear). Reachable from
  `session_write_file`, `session_files`, and `session_read_file` (via
  `_jail`), all caller-controlled. Found by `scripts/fuzz.py`. A
  legitimate session path is a handful of segments; both caps sit far above
  any real use.
- **`no_net`-mechanism descriptions that had gone stale after the seccomp-bpf
  enforcement change above, unqualified "LD_PRELOAD shim" claims that no
  longer match what Linux actually does.** `execute_code`'s own docstring
  said `no_net` was "LD_PRELOAD shim; dynamic binaries only" with no mention
  of seccomp at all — the exact thing a model reads at tool-selection time.
  Corrected there and in: the two session-worker `unenforced` disclosure
  messages in `codecalc/sessions.py` (a worker can't apply either mechanism
  post-spawn, not specifically LD_PRELOAD); the three fallback-path
  `unenforced` disclosures in `codecalc/executor.py`, said when there is no
  native executor at all to apply either mechanism, now a single
  `_NO_NET_NATIVE_MISSING` constant instead of three copy-pasted literals;
  `docs/contract/README.md`, `docs/contract/provider-v1.md`,
  `docs/deployment/README.md`, `docker/README.md`; and the platform-guarantee
  table in `executor/src/platform/mod.rs`'s module doc comment. `SECURITY.md`,
  `AUDIT.md` and the README's own per-platform guarantee table already
  described the seccomp/shim split correctly and are unchanged.
- **Cross-vendor review of the fix above found it incomplete on both axes.**
  First, several of the just-corrected strings themselves said "seccomp on
  Linux, a symbol shim elsewhere" — true on most Linux hosts but wrong on a
  Linux kernel that refuses a seccomp filter (`executor/src/platform/
  unix.rs`'s `seccomp::available()` gate), which also falls back to the
  shim; reworded to "seccomp where the Linux kernel supports it, a symbol
  shim otherwise" in `codecalc/executor.py`, `codecalc/sessions.py`, and
  `docs/contract/README.md`. Second, the "every stale instance" sweep had
  missed build-time and CI text that claims a *missing shim* leaves `no_net`
  unenforced with no platform qualifier — live-probed false on Linux with
  seccomp support (`codecalc-exec` copied without `blocknet.so` still
  returns `unenforced: []`, seccomp enforcing regardless of shim presence):
  `hatch_build.py`, `executor/build.rs` (module doc comment plus both
  build-warning strings), `executor/src/main.rs` (a field comment),
  `README.md` (two spots), `.github/workflows/ci-rust.yml`,
  `.github/workflows/release.yml`, and comments/check-labels in
  `tests/test_executor_sweep.py` and `tests/test_network_policy.py` that
  attributed clean enforcement to shim presence rather than to whichever
  mechanism actually held. `docs/contract/provider-v1.md` also credited the
  symbol shim with being able to "enforce" a network denial, which it
  cannot (best-effort, bypassable, disclosed via `unenforced`) — reworded to
  describe what the capability broker's `network_control` flag actually
  reports (`codecalc/providers.py`/`capabilities.py` behavior itself is
  unchanged; the flag does not yet distinguish real seccomp enforcement from
  the shim, tracked as a separate issue).
- **`safe_expr.reject_explosive`** no longer raises on three extreme-
  magnitude `Pow` shapes, violating this module's own documented contract
  that `classify_unsafe`/`safe_parse` never raise for hostile input. All
  three were a huge Python `int` reaching an unbounded int->float conversion
  or an unbounded int->str interpolation while the refusal itself was being
  computed or reported — not a gap in the refusal DECISION, which was
  already correct. Found by the 2026-08-22 ClusterFuzzLite batch:
  - `2**100000!!...` (`!!` is SymPy's double-factorial transform) made the
    exponent an exact Integer with thousands of digits; multiplying it by a
    float digit-count estimate coerced the int to float first, raising
    `OverflowError`.
  - `(x+1)**20000!!...` hit the same huge-exponent shape on a SYMBOLIC base,
    whose refusal MESSAGE interpolated the raw exponent directly — raising
    `ValueError` past Python's int->str conversion limit (4300 digits)
    before the message could even be built.
  - a base of 400 nines (`"9"*400 + "**12"`) exposed that SymPy's
    `Integer.__float__` returns `inf` for an oversized value instead of
    raising like Python's own `int.__float__` does, so the digit estimate's
    `try/except OverflowError` never fired; the resulting `inf` then broke
    `int(digits)` in the refusal message.
  All three now compute the digit/exponent magnitude via a `_log10_of_int`
  helper (exact to float precision — shifts an arbitrary-precision int down
  to its top 53 bits before ever calling `float()`, rather than a cruder
  `bit_length() * log10(2)` estimate, which was measured to flip some
  ordinary, well-under-the-cap powers like `2**10000` to a false refusal)
  and never interpolate the full value into a message, correctly returning
  a `resource_exhausted` refusal instead of crashing — verified not to
  waive the shape through (each is still refused, on 'digits'/'exponent'
  grounds). Added to `scripts/fuzz.py`'s `SEED_CORPUS_EXPR`.

  Cross-vendor (Codex) review of this fix caught two more in the same
  class, both fixed alongside it:
  - **off-by-one at `MAX_NUMERIC_DIGITS`**: the digit estimate IS
    `log10(result)`, not the digit count — a value with N digits has
    `log10` in `[N-1, N)`, so comparing the estimate itself against the cap
    admitted equality at the boundary. `10**4000` has 4001 digits but
    `log10(10**4000) == 4000` exactly, which is not `> 4000`, so it was
    incorrectly *allowed* — one digit over the cap the refusal message
    itself claims. Now compares `floor(digits) + 1` (the true count)
    against the cap; `10**3999` (exactly 4000 digits, genuinely at the
    boundary) is still correctly allowed.
  - **`classify_unsafe`'s heavy-function-argument refusal had the same
    unbounded int->str interpolation**, reachable independently of
    `reject_explosive`: `int(s, 0)` (base-0 auto-detect) parses a
    hex/octal/binary literal in linear time with no digit-count limit —
    unlike decimal text<->int conversion, which is exactly what carries
    the 4300-digit limit — so a short-looking token like `factorial(0x` +
    `f`*4000 + `)` parses to a plain int with thousands of DECIMAL digits,
    and the refusal message's own `str(value)` raised `ValueError` before
    it could report the refusal. Now falls back to a digit-count
    description only when `str(value)` would actually raise; an ordinary
    (small) literal's message is unchanged. Reachable with no length cap
    via `fuzz/safe_expr_fuzzer.py`'s atheris harness, which calls
    `classify_unsafe` directly. Added to `scripts/fuzz.py`'s
    `SEED_CORPUS_EXPR`.
- **`safe_expr.reject_explosive` closes the ceiling-coverage gap the fix
  above triaged and deliberately left open**: `2**(30000/2)`,
  `2**(-30000/2)` and `(3/2)**30000` now correctly refuse instead of
  silently computing an explosive value. `parse_expr(..., evaluate=False)`
  leaves `30000/2` as `Mul(30000, Pow(2, -1))` and `3/2` as `Mul(3, Pow(2,
  -1))` rather than folding them to a plain `Integer`/`Rational`, so the
  old `isinstance(exponent/base, (Integer, Float))` checks skipped them
  entirely and the real power got computed with no ceiling ever applied —
  a computed 4,516-digit `Integer` / a `Rational` with a 47,549-bit
  numerator, successfully, not a refusal. The six symbolic tools survived
  this only via each caller's own resource-error catch at stringification —
  a structured refusal, but at the wrong layer, after the full cost was
  already paid. A new `_bounded_numeric_value` helper resolves a
  numeric-only (no `free_symbols`) exponent or base subtree through the
  same `_log10_of_int`-based ceiling as a bare literal, but only via a
  small safe grammar of cheap combinators (`Integer`, `Rational`, `Float`,
  `Add`, `Mul`, `Pow` with an integer exponent) and only after checking
  each combination's bit-length against a budget BEFORE performing it —
  never after.
  A first version of this fix refused anything outside that grammar
  outright, which turned out to be too broad: a cross-vendor differential
  probe (main vs. the fix, over 17 inputs) found `2**cos(0)`, `2**(2*1.5)`,
  `2**(1.5+1.5)`, `(1.5*2)**3` and `2**(2**10000*2**-10000)` — all of
  which `main` evaluates fine (2, 8, 8, 27, 2) — got refused pre-parse
  instead. Fixed two ways: (1) `_bounded_numeric_value` now distinguishes
  returning `None` (INCONCLUSIVE — a `Function` call like `cos(0)`, an
  irrational constant, or float-mode overflow; not evidence of anything,
  so the caller falls through to the existing downstream screen rather
  than refusing) from a new `_NumericTooLarge` sentinel (PROVED via exact
  bit-length arithmetic to exceed the budget; still refused) — the earlier
  version conflated the two; (2) `Add`/`Mul` now check the bit length of
  the ACTUAL (SymPy auto-reduces via GCD) accumulator after each
  combination rather than a pessimistic pre-sum of each factor's own
  size, so `2**10000 * 2**-10000` (reciprocal factors, cancels to exactly
  1) resolves correctly instead of tripping the budget on the way there —
  and a `Float` anywhere in an `Add`/`Mul` chain now promotes the whole
  combination to ordinary bounded `float` arithmetic instead of refusing
  outright. The three genuine targets above still refuse; 12 additional
  boundary/edge inputs from the same differential probe (`10**3999`,
  `10**4000`, `2**10000`, `(1/2)**30000`, `sqrt(2)+1`, `(2/3)**5`,
  `x**(2*3)`, ...) are unaffected either way.
- **The same investigation found a separate parse-time memory bomb**:
  `"2**1000000^6c6/Me,"` returned the correct `('validation', 'parse
  error')` refusal, but only after allocating a 2977 MB tracemalloc peak
  (measured under a 3 GB `ulimit -v`; ClusterFuzzLite's own 2560 MB
  libFuzzer rss cap reported 1542 MB and OOM'd). A trailing comma is valid
  Python tuple syntax (`x,` means `(x,)`), so `parse_expr(..., evaluate=
  False)` hands back a bare Python `tuple` — not a SymPy node — wrapping
  the real `Mul(2**(1000000**6), ...)` shape. `safe_expr._walk`'s
  `getattr(current, "args", ())` found no `.args` on a plain tuple and
  stopped there, so `reject_explosive` never saw the `2**(1000000**6)`
  power tower buried inside and returned `None` as if the tree held
  nothing dangerous — the real, evaluating parse then computed
  `2**(1000000**6)` before the trailing comma's syntax error ever got a
  chance to surface. `_walk` now descends into a bare tuple's own elements
  directly; the input now refuses in milliseconds on power-tower grounds,
  under a 50 MB tracemalloc peak. All four inputs added to `scripts/
  fuzz.py`'s `SEED_CORPUS_EXPR`.

### Changed

- **Trimmed dev-history/rationale prose out of 3 heavy tool docstrings**
  (`execute_code_stream`, `install_package`, `runtimes_status`), gated by
  `scripts/tool_select_eval.py --baseline` so the trim could not quietly cut
  the vocabulary a model leans on to pick the right tool. Narrative moved to
  `#` comments right at the call site rather than deleted. `runtimes_status`'s
  `tier` paragraph, previously duplicated in full from `list_languages`, is
  now a one-line pointer to it (`list_languages` keeps the full explanation —
  it is the tool a model reaches for first to learn what `tier` means).
  Measured on the 52 live tool descriptions with `o200k_base` (matching the
  README's own "Tool-definition token cost" methodology): 4877 -> 4763
  tokens (-114, -2.3%; 19674 -> 19139 chars). Two further candidates in
  the same audit (`verify_translation`, `verify_optimization`) were trimmed
  and reverted:
  the removed prose scored as load-bearing for `tool_select_eval.py`'s
  baseline once BM25's corpus-wide length normalization was accounted for
  (`verify_translation` uniquely held the word "porting" in the entire
  52-tool corpus; removing it flipped an unrelated `solve_linear` prompt),
  so both stay unchanged. Full/dev/core eval: 118/96/63 top-1 hits
  (baseline 119/96/63, epsilon 1) and 149/118/77 top-3 (baseline
  148/117/77) — no regression under the gate's tolerance.

## [0.4.0] — 2026-08-21

### Added

- **`codecalc setup [--client=NAME] [--write]`**: guided onboarding
  from a clean install to a working MCP connection in one command, ending in
  a single verdict (`ready`/`degraded`/`not-ready`). Detects the calling
  client (`claude-desktop`/`claude-code`/`cursor`/`vscode`/`zed`) by probing
  each one's known config path, or takes `--client` explicitly when none or
  several are found; reuses `codecalc doctor`'s own executor-backend/extras/
  grammar-cache checks rather than re-deriving them; prints the exact MCP
  config block in the detected client's own shape (`mcpServers` for Claude
  Desktop/Cursor/Claude Code, `servers` for VS Code, `context_servers` for
  Zed) with absolute, machine-derived command/args (an absolute venv python
  for a source checkout, the resolved `codecalc` console script, or `uvx
  codecalc[full]`); runs a real `execute_code` and `evaluate_expression`
  canary in-process (no server spawned) to prove the connection would
  actually work. **Non-destructive by default**: without `--write` this only
  prints — the config file, the grammar-cache prefetch, and the skill copy
  are all untouched. `--write` merges the `codecalc` entry into the client's
  EXISTING config (every other server/setting passes through unchanged) and
  backs up the original to `<path>.codecalc-bak` first; an existing config
  file that is not valid JSON is refused rather than risked.
- **`CODECALC_TOOLS`: register a slice of the 52-tool surface**.
  `tools/list` costs ~9.2k tokens up front (see README "Tool-definition token
  cost"), and the fix that stayed deliberately unbuilt is a facade
  (`docs/design/2026-08-10-tool-facade.md`) — collapsing every tool behind one
  dispatcher erases per-tool typed schemas and per-tool approval prompts. This
  is a different mechanism: every `@mcp.tool()` in `server.py` now declares a
  `group=` (`calculator`/`verification`/`execution`/`sessions`/`analysis`/
  `admin`, 52 tools total, mapping in the new README section "Reducing the
  tool surface"), and `CODECALC_TOOLS` (comma-separated group and/or preset
  names — presets `core`/`dev`/`full`) restricts, at import, which groups
  `_tool()` actually hands to the MCP SDK. A tool outside the active set is
  never registered at all: absent from `tools/list` **and** rejected by
  `tools/call`, not a name a client could still guess and invoke through a
  hidden facade path. Unset/empty registers every group — 52 tools, unchanged
  default behaviour — and an unknown group or preset name is a loud
  `ValueError` at startup naming the bad value and every known group/preset,
  never a silent fallback to "everything" or "nothing" (either direction turns
  a typo into a footgun). `codecalc doctor` gained a `tool groups` block:
  active groups, the full group→tools mapping, and how many tools this
  process actually registered. Gated by the new
  `scripts/check_tool_groups.py`, wired into CI: statically asserts every
  declared tool names a known group, then re-derives — from a live subprocess
  import with `CODECALC_TOOLS` unset — that the default configuration still
  registers all 52, so the filter mechanism cannot silently shrink the
  no-configuration case the rest of CI's tool-count gates depend on.

- **Per-language RELIABILITY tiers**, orthogonal to the existing
  resolution states (`supported`/`installed`/`unhealthy`/`available`). A
  runtime could report `installed` (its command resolved on PATH) while its
  toolchain was actually broken — a review's own smoke test found the rust
  and csharp host toolchains failing on a machine where both commands
  resolved cleanly — and codecalc presented every resolved executable as
  equally operational. Every `codecalc/registry.py` language now carries a
  `tier`: `tested` (a CI job genuinely executes it and asserts on real
  output, on every PR — currently `python3` and `node` only, kept
  deliberately conservative), `best_effort` (declared, plausibly works on a
  normal install, never CI-checked — every other language, `rust` and
  `csharp` included), or `plan_only` (never validated on any runner, none
  today). Surfaced as a new `tier` field on `list_languages`,
  `runtimes_status`, and `codecalc doctor`'s `runtimes`/`tier_summary`
  (`list_execution_providers`' docstring now cross-references it — provider
  descriptors are about execution BACKENDS, not per-language reliability, so
  there is no per-provider field to add); `codecalc doctor`'s text output
  prints a `reliability tier` block naming every non-`tested` runtime as
  "toolchain may be broken; not exercised by codecalc's CI" so a resolved
  runtime never reads as equally trustworthy to a genuinely CI-verified one.
  Gated by the new `scripts/check_runtime_tiers.py`, wired into CI: it
  derives the `tested` set from the literal source of the two files that
  actually wire a language into CI (`tests/test_python_sweep.py`'s
  `WORKER_LANGS`, `scripts/contract_check.py`'s `CANDIDATES`) rather than
  trusting a hand-maintained list, so a language cannot claim `tested`
  without a CI check backing it, or silently drop out of CI while still
  claiming it. `contract_version` moves `1.2.0` → **`1.3.0`** (a MINOR add):
  `tier` on every `doctor` `runtimes` entry, plus a `tier_summary` block —
  the execution result shape itself is unchanged.

- **Per-session and global disk quotas for sessions**. Nothing
  previously bounded the TOTAL disk a session accumulates — only per-stream
  (`SPILL_CAPTURE_KB`, 4 MiB) and per-served-file (`RESOURCE_MAX_BYTES`,
  4 MiB) ceilings existed, so a session could fill the operator's disk one
  small write at a time. Five new, generous-by-default knobs close it:
  `CODECALC_SESSION_DISK_QUOTA_MB` (default 512), `CODECALC_TOTAL_DISK_QUOTA_MB`
  (default 8192, across every session workspace), `CODECALC_MAX_ARTIFACT_BYTES`
  (default 16 MiB, per write) and `CODECALC_MAX_ARTIFACT_COUNT` (default 500,
  per session), and `CODECALC_MIN_HOST_FREE_MB` (default 256, refuses a write
  when the host itself is low regardless of how generous the quotas above
  are). `session_write_file` and oversized-output spilling are checked BEFORE
  every write (`resource_exhausted`, no partial file ever written); code run
  via `execute_code(session_id=...)` or `session_run` is checked before it
  starts and, since executed code's own writes cannot be pre-checked, again
  after it finishes — an over-quota run still returns its real result, now
  carrying `disk_quota_exceeded` plus the measured usage/limit, and the
  session's next write or run is refused for as long as it stays over the
  line (re-measured fresh each time, so freeing space un-refuses it on its
  own — no flag to reset). `codecalc doctor` reports the configured limits
  and current per-session/global usage under a new `disk_quota` section.

  An adversarial review of the above found four enforcement gaps, all
  closed in the same change: `install_package` — the largest write vector
  of all, MB to GB per call — carried no quota check whatsoever, so an
  over-quota session installed freely; it now gets the same precheck (before
  the package manager runs) and postcheck (disclosing an install that pushed
  a session over quota) `execute_code`/`session_run` already had. The
  per-session artifact-COUNT cap only ran from this module's own writes, so
  code executed via `execute_code`/`session_run` could create arbitrarily
  many files — each individually under the byte quota — with nothing to
  catch the total; `disk_quota_exceeded`'s postcheck/precheck pair now
  carries the same disclose-then-block shape for the file count. An
  overwrite's disk-usage check compared the FULL new size against usage that
  already counted the file being replaced, wrongly refusing a same-size or
  shrinking overwrite near quota — fixed to compare the NET size delta.
  Finally, since `execute_code`/`session_run` are themselves refused once a
  session is over quota (so executed code cannot free space by running
  `rm`), a net-non-positive write (shrinking or same-size) is now ALWAYS
  permitted regardless of current usage — the in-band recovery path that
  makes `session_stop` no longer the only way out of a stuck session.

- **`codecalc status` and `codecalc cleanup`: operator-facing session
  disk-usage commands**, the operational half of the per-session disk
  quotas. `status [--json]` is a read-only snapshot — `SESSION_ROOT`,
  session count, per-session/global workspace disk usage, which sessions
  are idle-expired (the on-disk `.codecalc-session-expired` marker), the
  configured quotas and current headroom, the audit log's path and size,
  and a one-line runtime reliability-tier summary — that changes nothing.
  `cleanup [--dry-run|--write] [--include-unmarked]` reclaims disk from
  session directories under `SESSION_ROOT`; `--dry-run` is the DEFAULT and
  removes nothing, `--write` is the one flag that actually deletes. Both
  are CLI-only (`server.py`'s `main()` dispatch, logic in the new
  `codecalc/ops.py`) — neither is an MCP tool, so neither counts against
  the 52-tool surface.

  An adversarial review of the first version found a CRITICAL bug: it
  trusted session-directory MTIME as a liveness signal, and that signal is
  false — a REPL worker doing purely in-memory work touches no file, and
  even an in-place file overwrite bumps only the file's own mtime, never
  its parent directory's. Proven live: a genuinely-active worker session's
  workspace was removed out from under it. Fixed with a REAL liveness
  signal, a per-worker-session lockfile (`sessions._LOCK_FILE_NAME`) the
  server writes carrying its own pid at worker start and releases the
  moment that worker is actually gone (reaped or `session_stop`); `cleanup`
  refuses ANY candidate whose lock names a still-live pid, regardless of
  marker, age, or the mtime floor, cross-platform (`os.kill(pid, 0)` on
  POSIX, `OpenProcess`/`GetExitCodeProcess` on Windows — no `os.kill(pid,
  0)` equivalent exists there). Since a workspace-only session never holds
  a lock (no worker to protect), the marker-less age-based path is now
  OPT-IN (`--include-unmarked`) rather than default: `cleanup` alone only
  ever considers the on-disk `.codecalc-session-expired` marker, which
  carries no such residual risk (a marker only exists once sessions.py's
  own idle-TTL reaper has already closed that worker for good — session
  ids are never reused). `--include-unmarked` additionally sweeps old
  (`CODECALC_CLEANUP_ABANDONED_AGE_HOURS`, default 24h), session-shaped
  directories, still gated by the same lockfile check plus a hard recency
  floor (nothing modified in the last few minutes is ever touched).

  `cleanup` runs as a separate process from any server using
  `SESSION_ROOT`, so it has none of that server's in-memory bookkeeping to
  consult beyond the lockfile above; it remains deliberately conservative
  otherwise: only a direct child of `SESSION_ROOT` is ever a candidate,
  never `SESSION_ROOT` itself; a symlink there is refused, not followed;
  and removal goes through the same device/inode
  identity-check-immediately-before-delete discipline `session_stop`'s own
  workspace teardown already uses, re-verified at delete time rather than
  trusted from the scan (inode reuse in that window is an accepted
  residual, the same one `session_stop` already carries). README and
  `--help` now warn explicitly: keep `CODECALC_SESSION_ROOT`
  codecalc-private — the "looks like a session dir" name filter is loose,
  not strict.

- **Audit-log size-based rotation**. `AuditLog` (`codecalc/audit.py`)
  appended to one file forever — the same unbounded-growth gap already
  closed for session workspaces, here for the append-only broker-decision
  trail. `CODECALC_AUDIT_MAX_MB` (default 10 MiB, read fresh per call, same
  unset/invalid-safe shape as `CODECALC_SESSION_IDLE_TTL_SECONDS`) now caps
  the live file; crossing it rotates `audit.log` → `audit.log.1` →
  `audit.log.2` (a small, fixed 2-generation history — the same
  bounded-count-oldest-dropped shape session spill files already use) and
  starts a fresh file. Checked and performed inside `emit()`'s own existing
  try/except: a rotation failure is swallowed exactly like an ordinary
  write failure — it can never fail the run it is describing.
- **`codecalc --help` and `codecalc --version`** (GH #201). Both used
  to print nothing and exit 0 — `main()` recognised `doctor`/`serve-strict`/
  `serve-http` but treated the flags as "no subcommand" and started the stdio
  MCP server. They now print a usage block (naming the subcommands and that the
  default with no argument is the stdio server) / the version, and exit 0
  without starting anything.
- **`codecalc-prefetch-grammars`, a console script that warms the tree-sitter
  grammar cache** (GH #200). The offline warm-up the README told
  installed users to run lived only in `scripts/`, which the wheel does not
  ship — so the documented command did not exist for anyone who `pip
  install`ed rather than cloning. It now ships as a `[project.scripts]` entry
  point (`codecalc-prefetch-grammars`, `--print-cache-dir`) calling the same
  code the source script does.
- **`matrix` — structured matrix operations** (GH #223): det,
  inverse, eigenvalues, transpose, rank, trace. `evaluate_expression` has
  always refused `Matrix([[1,2],[3,4]])` with `"'[' is not permitted in an
  expression"` — `[`/`]` are denied at the token level to block a
  subscript-based RCE escape (`().__class__.__bases__[0]`), and a matrix
  literal was collateral from that correctly-aimed screen. `matrix` is the
  structured fix: `rows` arrives as a JSON array of arrays, never a caller
  string parsed through sympify, so the RCE screen never applies to it in
  the first place. Each entry is either a JSON number, used directly, or a
  scalar expression string screened individually through the same
  `classify_unsafe` check `evaluate_expression` uses, before it ever reaches
  SymPy — a malicious entry like `"().__class__"` is refused per entry with
  `permission_denied`, exactly as `evaluate_expression` refuses the same
  string. Non-rectangular and non-square (for det/inverse/eigenvalues/trace)
  input is rejected with a clear message; a singular matrix passed to
  `inverse` returns a clean `validation` error instead of a traceback.
  **51 → 52 MCP tools.**

### Changed

- **`evaluate_expression`'s `'[' is not permitted` refusal now names the
  `matrix` tool** (GH #223, modelled on GH #209's `bit_analysis`
  message style). What is refused is unchanged — `[`/`]` are still denied
  for the same RCE reason — only the message improves, from a bare `"'['
  is not permitted in an expression"` to one that says a matrix literal
  belongs in the new `matrix` tool instead.

### Security

- **A workspace-guard refusal (an out-of-workspace path, a malformed session
  id) now carries the full result contract** (#212a). Before this,
  `session_write_file`/`session_files`/`session_read_file`/`session_run`/
  `session_stop`/`execute_code(session_id=...)` REFUSED such a request by
  raising, uncaught, all the way past `SessionService` and the `@mcp.tool()`
  wrapper — a caller got a bare protocol-level error instead of the
  `ok`/`code`/`remedy` shape every other rejection in this package carries.
  These now return `{"ok": false, "code": "permission_denied"` (an escape) or
  `"validation"` (a malformed id) `, "remedy": ...}`, same as any other
  refusal.
- **A pydantic argument-validation error no longer echoes the caller's raw
  value** (#212b). A wrong-typed tool argument is rejected by the
  MCP SDK's own schema validation before a tool body ever runs, and its
  error text included `input_value=<exactly what was passed>` verbatim — a
  potential info leak into logs/transcripts. A new server middleware strips
  that bracketed diagnostic from the error text before it leaves the
  process; the field name and reason are kept.
- **`serve-http`'s DNS-rebinding protection now matches codecalc's own
  loopback allowlist** (#211). codecalc accepts any address in
  127.0.0.0/8 plus `::1`/`localhost`/`ip6-localhost` as loopback-safe (no
  `CODECALC_HTTP_TOKEN` required), but the MCP SDK's own DNS-rebinding
  auto-default only recognises the three literal strings
  `"127.0.0.1"`/`"localhost"`/`"::1"` — anything else codecalc accepted (for
  example `127.0.0.2`) silently got NO DNS-rebinding protection at all.
  `serve-http` now builds `TransportSecuritySettings` explicitly from the
  same host it just validated, so a rebinding `Host:` header is rejected
  (421) on every bind codecalc itself considers safe.
- **`session_files` no longer stats through a symlink** (#208). A
  session could plant a symlink pointing outside the workspace, and the
  listing reported the TARGET's size — disclosing the existence and size of
  a path `session_read_file` already refuses to touch. A symlink entry is
  now reported as `{"type": "symlink"}`, never followed to describe what it
  points at; `session_artifacts` excludes symlinks from its listing for the
  same reason.
- **A backgrounded descendant survived a NORMAL exit** (GH #207). The
  process-group/job kill only ever ran on the timeout/overflow path — a
  payload that spawned a detached child (`subprocess.Popen(['sleep',
  '1000'])`) and returned 0 hit neither, so the child outlived the run with
  no wall clock on it at all. Both backends now reap the whole group after
  EVERY exit, not only a timed-out one: the Rust executor unconditionally
  `killpg`s the child's process group after its `wait4` loop, on Unix, and
  the Python fallback does the same via a new `_reap_group` (Windows job
  objects were already correct here — `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
  reaps the whole job when its handle closes, on any exit path). The Python
  fallback's spawn also picked up `CREATE_NEW_PROCESS_GROUP` on Windows,
  where it was previously relying on `start_new_session`, a POSIX-only flag
  that Windows silently ignores.
- **`max_output_kb` enforced a ~1 MiB floor regardless of the request**
  (GH #206). The Rust executor's RLIMIT_FSIZE — the ceiling on how much the
  sandboxed child is actually allowed to write before being stopped — was
  computed as `max_output_kb * 1024 * 4` clamped to a **1 MiB floor**. A
  caller passing `--max-output-kb 1` got an enforced ceiling near 1 MiB
  (1024x the request), undisclosed in `unenforced`; measured, `stdout_bytes`
  came back `1048576` for a program that printed 5 MB. The RETURNED `stdout`
  text was always correctly capped at the literal request (`read_capped`
  truncates independently, with no floor) — only the underlying write
  ceiling was wrongly sized. The floor is now 4 KiB, which no longer binds for
  any `max_output_kb >= 1` (the existing 4x headroom always clears it on its
  own), so the enforced ceiling stays a small, proportional multiple of the
  request.
- **`solve_linear` no longer parses caller input through a live-builtins
  `sympify`**. Each side of each equation (and the single-expression
  no-`=` path) reached `sp.sympify(...)` directly, which uses SymPy's DEFAULT
  `global_dict` (`vars(builtins)` copied in) and skipped the unevaluated-shape
  check `evaluate_expression` already runs. `solve_linear('x = input()', 'x')`
  called the REAL `input()`, reading the child's stdin — shared fd 0 with a
  stdio MCP server — and `solve_linear('x = 9**9**9**9', 'x')` burned real CPU
  seconds evaluating a power tower blind, riding the guard's own timeout
  instead of a fast refusal. Both pieces now parse via `parse_expr(global_dict=
  safe_global_dict())` with `reject_explosive` run on the unevaluated shape
  first — the same fix gave the `matrix` tool's per-cell parse.
  `input()`/`breakpoint()`/`quit()` now parse to a clean error (matching
  `evaluate_expression`'s own outcome for the same string) instead of being
  called, and a power tower is now `resource_exhausted` in milliseconds
  instead of burning CPU seconds.
- **`algebraic_equiv`, `solve_expression`, `limit_expression` (including its
  `point` argument) and `simplify_expression` no longer parse caller input
  through a live-builtins `sympify`**. Each reached a bare
  `sp.sympify(...)` after `classify_unsafe` — the same gap closed
  in `matrix` and `solve_linear`: a screened, non-denylisted NAME can still
  be a live Python builtin. `simplify_expression('input()')` called the REAL
  `input()`, reading the child's stdin — shared fd 0 with a stdio MCP
  server — and `algebraic_equiv('9**9**9**9', '0')` burned real CPU seconds
  evaluating a power tower blind. All four now parse via a new shared
  `safe_expr.safe_parse()` — `parse_expr(global_dict=safe_global_dict())`
  with `reject_explosive` run on the unevaluated shape first — extracted
  from the three near-identical copies of this same pipeline that
  `logic._evaluate_expression`, `logic._parse_solve_piece` and
  `linalg._parse_entry` each hand-rolled; all three now delegate
  to the shared helper too, dropping the duplication (their own result
  shapes are unchanged), so `safe_parse` is the single place in the package
  that hands a caller string to SymPy's parser and a future caller cannot
  reintroduce this gap by doing `classify_unsafe` and forgetting the safe
  parse. `input()`/`breakpoint()`/`quit()` now parse to a clean error
  (matching `evaluate_expression`'s own outcome) instead of being called,
  and a power tower is now `resource_exhausted` in milliseconds instead of
  burning CPU seconds.
- **Side effect of the above: the four `exact.py` tools now parse with
  implicit multiplication**, the same `parse_expr` transformation
  `evaluate_expression`/`matrix`/`solve_linear` already enable, in place of
  `sp.sympify`'s stricter grammar. Ordinary input that used to require an
  explicit `*` is now accepted and reinterpreted as multiplication:
  `2(x+1)` (previously a parse error) now parses as `2*x + 2`, and
  function-call notation on an undeclared name — `f(x)` (previously the
  applied function `f(x)`) — now parses as `f*x`. Concretely,
  `algebraic_equiv('x(x+1)', 'x*(x+1)')` now reports `identical: true`; on
  the old `sp.sympify` grammar the left side was the applied function
  `x(x+1)`, not a product, and the two were not identical. This mirrors
  `evaluate_expression`'s existing behaviour for the same input and is a
  side effect of closing the parse gap above, not an independent feature.

### Fixed

- **A guard or policy refusal classified as `internal`, indistinguishable from
  an unhandled crash** (GH #214, the follow-up to an earlier
  argument-validation fix). `evaluate_expression`/`simplify_expression`/
  `solve_expression`/`solve_linear`'s guarded-evaluation allowlist
  successfully blocking a sandbox-escape attempt (`__import__(...)`,
  `.__class__.__bases__`, a string literal) now returns `"code":
  "permission_denied"` instead of `"internal"` — `INTERNAL`'s remedy is "a
  defect in codecalc; the message is worth reporting verbatim", which told an
  operator that a successfully blocked attack was a bug to file. A power-tower
  or oversized-exponent refusal (`evaluate_expression("9**9**9")`,
  `calc_exact("9**9**9")`) now returns `"resource_exhausted"` — a ceiling, not
  a defect. `calc_exact("1/0")` now returns `"validation"` with the message
  "division by zero" instead of leaking `ZeroDivisionError`'s constructor
  argument (`"Fraction(1, 0)"`) as if it were a sentence.
  `install_package(language="ruby", ...)`'s documented unsupported-language
  refusal now returns `"permission_denied"` instead of `"internal"`. The
  guarded-evaluation allowlist's screen (`safe_expr.py`) actually refuses two
  DIFFERENT things through one message string — the RCE token/keyword screen
  above, and, separately, a heavy-argument ceiling (`factorial(100000)`,
  `binomial(200000, 100000)`) that was already correctly `resource_exhausted`
  before this change and still is: `classify_unsafe` now names which of the
  two a given rejection is, so the security half moved to
  `permission_denied` without dragging the ceiling half along with it. Each
  code is chosen at the point the refusal is decided, not guessed back out of
  the message by `ensure_code` — the same raise-site principle already
  established, so a future refusal worded a new way cannot silently default
  to claiming a codecalc defect again.
- The MCP server's `instructions=` metadata said "30+ languages"; every
  other surface (README, SECURITY.md, the repo description) said the actual
  count, 31. `scripts/check_claims.py` now gates this string too
  (#213a).
- `data_sizes(n)` accepted a negative `n` and reported negative KiB/MB
  instead of rejecting it — the same bug shape `human_duration` already
  guards against for a negative duration. It now returns a validation error
  (#213b).
- `percentage`, `percentiles`, `collision_probability` and `human_duration`
  presented a `round()`ed float beside an exact one (a fraction string, an
  unrounded probability, the echoed input) with nothing marking which was
  which. Each result now carries a `"rounding"` field naming exactly which
  of its own keys were rounded and to how many decimal digits
  (#213c).
- **Ten tools blamed the caller for their own bad input** (GH #196).
  An argument-validation rejection — `percentage(total=0)`, `percentiles([])`,
  `calc_stats([5])`, `collision_probability(bits=0)`, `human_duration(-5)`,
  `epoch_time(-1)` among them — returned `code: internal`, whose remedy reads
  "a defect in codecalc; the message is worth reporting verbatim". These tools
  `return {ok:false}` rather than raise, so `errors._from_message`'s substring
  matching is the classification layer; its hint list gained the missing
  validation phrases ("at least"/"is zero"/"negative"/">="/"unknown") so a
  caller's bad argument classifies as `validation`, not a codecalc defect.
  (above finished the other half — the guard/policy refusals.)
- **`bit_analysis` on a negative `n`** (GH #197). `bit_analysis(-1)`
  counted `abs(n)` — reporting `popcount: 1` while its own `is_power_of_two`
  said false — and silently dropped `next_power_of_two`. A negative `n` is now
  a validation error pointing at `bitop(width=W)`; `n=0` discloses
  `next_power_of_two: None` rather than omitting the key.
- **`human_duration` sub-second and very-large inputs** (GH #198).
  `human_duration(0.5)` returned `"0s"` (losing everything under a second) and
  `human_duration(1e30)` emitted 26 digits off a 17-digit float — precision the
  float never held. Sub-second inputs now surface as `ms`/`µs`, and values at
  or beyond 2^53 are capped to a scientific form instead of fabricating digits.
- **`solve_linear` crashed on a single-variable system**:
  `solve_linear('2*x = 4', 'x')` raised `'Symbol' object is not iterable`.
  `sp.symbols(names)` returns a bare `Symbol` rather than a 1-tuple unless
  given more than one name or a trailing comma, and the code downstream
  assumed a sequence (`list(syms)`) unconditionally. `sp.symbols(...,
  seq=True)` now always returns a sequence, one variable or many, so
  single-variable systems solve like every other case; multi-variable
  behaviour is unchanged.

## [0.3.1] — 2026-08-20

### Fixed

- MCP registry publishing: `server.json`'s `description` shortened
  to the registry's 100-char limit (was 117, causing a 422), the namespace
  corrected to the GitHub org's actual casing (`io.github.The-40-Thieves/codecalc`,
  the OIDC grant is case-sensitive and rejected the lowercase form with a
  403), and the release workflow's `publish-mcp-registry` job made
  retriable — it now runs whenever the PyPI publish succeeded or was
  already skipped, so a registry-only re-dispatch can publish against an
  already-live PyPI release. This release exists to carry the corrected
  `mcp-name` marker into a fresh PyPI package description, since the
  marker check the registry runs against the published description is
  case-sensitive and the 0.3.0 description still had the old casing.

## [0.3.0] — 2026-08-20

### Added

- **A versioned extension SDK — language packs, renderers, verifiers.**
  Every extension kind now has a versioned `Protocol`, a registry
  enforcing identity/no-impersonation, interface-major compatibility, a
  permission allowlist and a `CODECALC_DISABLE_THIRD_PARTY_EXTENSIONS` kill
  switch, plus integrity verification wired into `register()`. Each kind
  ships a built-in reference implementation and a reference third-party
  extension as its own second-consumer conformance check; `doctor` gains an
  `extensions` discovery block and `docs/extensions/README.md` documents the
  trust model (trusted-by-installation, not a sandbox).
- **Strict gVisor runtime forwards program `stdin` to the guest**,
  mirroring the local Rust executor path via a read-only bind-mounted
  per-run file.
- **One-command Linux strict bootstrap**, `scripts/setup-strict.sh`
  — preflight, pull the pinned image, run the deep `gvisor-v1`
  canary, then launch.
- **MCP registry metadata** — `server.json`, an `mcp-name` PyPI-ownership
  marker, and a `uvx codecalc` client install snippet, preparing
  repo-side discovery readiness for the registry publish (the
  `mcp-publisher` publish itself is owner-gated and separate).
- **Operator deployment runbook**, `docs/deployment/README.md`,
  covering all three strict backends (Linux/gVisor+Docker,
  Windows/AppContainer, macOS/remote-Linux) plus startup canary, orphan
  recovery, fail-closed causes and quota tuning.
- **`scripts/win-verify.ps1`**, a one-command native-Windows verification
  bootstrap that builds the executor and runs every Windows sandbox probe
  with timestamped output.

### Changed

- **Strict service forwards `max_cpu` to the guest's `cpu_count`, clamped to
  `MAX_CPU_COUNT`**. It previously silently dropped the field, so
  every remote strict run used the default `cpu_count=1.0`.
- **README leads with a value proposition and a "when to use codecalc"
  section**, honest about when a hosted sandbox or the vendor's
  own interpreter is the better fit.

### Fixed

- `compare_execution`: a cold-start timeout on one language no
  longer ends the story for that language. If a run comes back `timed_out`,
  it gets exactly one warm retry with the same arguments; a recovered retry
  is reported as the row's result (with `cold_retry`, `cold_retry_recovered`,
  and `first_attempt_ms` added). A timeout that persists after the retry is
  always surfaced in `discrepancies`, with `sibling_durations_ms` for every
  other successful language and a `variance_note` that compares them — a
  wide spread (>=10x) reads as runner-wide slowness/cold-start pressure
  rather than a defect in the timed-out language's snippet, a tight band
  reads as specific to it. This addresses the case where `node`, the
  heaviest cold-starter, is first to cross the fixed wall-clock deadline on
  a globally slow GitHub Actions windows-latest runner (one repro: ruby at
  3452ms vs python at 33ms for a trivial snippet). `executor.execute`'s
  timeout remains an unchanged hard limit — only `compare_execution`'s
  handling of a `timed_out` result changed. A deterministic non-timeout
  failure (e.g. a compile error) is never retried.
- **Windows CI Defender path exclusions** cut interpreter
  cold-start scanning, the dominant contributor to the flake class.

### Docs

- README no longer carries the pre-publish warning — it reflects the live
  `0.2.0` release on PyPI and crates.io.

### Removed

- **Dropped the unused `serde` direct dependency from the executor crate**
  — only `serde_json` was ever used directly; trims the
  untrusted-code binary's dependency surface.

## [0.2.0] — 2026-08-19

The first release cut for publication to PyPI and crates.io. It changes the
**tool surface** (48 tools → 51) and adds to the **result
contract**, so it is a MINOR bump, not a patch. The
`contract_version` moves `1.0.0` → **`1.2.0`** for the same reason: `1.1.0`
ADDED a fifth result shape (`run_lifecycle`, for the background-run tools) and
fields (an execution receipt with `session_id`, grade metadata), and `1.2.0`
ADDS a `strict_runtime` prerequisites block to the `doctor` diagnostic document.
Additions are exactly what the contract's own policy defines as a MINOR bump — a
compatible addition bumps MINOR, it does not leave the version unchanged. A
`1.0.0` client keeps working against `1.2.0`.

### Added

- **`install_package` confinement extended to macOS**. Layer 2 of
  the #23 mitigation — bounding what the installer *binary itself* can reach
  on disk, on top of Layer 1's "do not run its install-time code at all" —
  was Linux-only (`landlock.abi_version()` returns 0 off Linux), leaving
  macOS and Windows installs running with the server user's full filesystem
  access behind an honest but unenforced disclosure token. macOS now gets a
  real boundary via `sandbox-exec` (`codecalc/sandbox_macos.py`): a generated
  Seatbelt profile scopes both reads and writes to the workspace, its
  redirected caches and a curated set of system paths, denying everything
  else — network stays open (an install needs it) and is reported, not
  enforced, via the same `install_tcp_egress_unrestricted` /
  `install_udp_egress_unrestricted` / `install_metadata_syscalls_unrestricted`
  tokens Linux already emits. `tests/test_package_isolation.py` runs the same
  write-outside-refused / canary-outside-unreadable assertions the Linux
  Landlock probe uses, gated to execute on `darwin` (CI's `macos-latest` leg)
  and to SKIP with a recorded reason everywhere else — the mechanism is
  proven by macOS CI, not argued from documentation. Windows gets no claimed
  confinement (the AppContainer job-object work is unverified on real Windows 11)
  but a documented no-op opt-in, `CODECALC_WIN_INSTALL_CONFINE`, adds a
  `package_install_confinement_unverified_on_windows` disclosure without
  claiming enforcement; the base `package_install_not_confined_no_landlock`
  disclosure keeps firing on Windows exactly as before.

- **Linux strict gVisor boundary made real**. A real executor
  container image (`docker/executor.Dockerfile`, multi-stage/minimal/non-root,
  carrying `codecalc-exec` + `blocknet.so` + python3);
  `DockerGVisorRuntime.recover_orphans()` reconciles owned strict containers by
  their immutable run-identity label when the remote strict execution service
  (which lives out of this repo) invokes it at that service's startup; `doctor`
  now CALLS the
  runtime's host probe and surfaces measured prerequisites in a `strict_runtime`
  block (Docker present, cgroup v2, `runsc` registered, image present, and a
  real startup canary under `--deep`), failing closed with a structured reason
  on a host without `runsc`. A hostile-workload conformance suite
  (`tests/test_gvisor_conformance.py`) launches the image under `--runtime=runsc`
  and proves fork-bomb/memory-bomb/descendant-escape/egress/filesystem
  containment on a runsc host, verifying the runtime OUT OF BAND. It skips
  without `runsc` (GitHub CI), and runs via `scripts/gvisor_conformance.sh` on
  Cave / a runsc host. The registry-published image residual is now closed (see
  the next entry); GitHub-CI-under-runsc remains out of reach — see
  `docs/contract/provider-v1.md`.

- **Published, digest-pinned strict executor image** (image residual).
  The `publish-executor-image` workflow (`workflow_dispatch`) builds
  `docker/executor.Dockerfile` for `linux/amd64` + `linux/arm64` (buildx + QEMU),
  pushes it to `ghcr.io/the-40-thieves/codecalc-exec` (GHCR, `GITHUB_TOKEN` with
  `packages: write` — no external secret), and commits the immutable index digest
  into `docker/executor-image.lock`, pushing it back to the branch. New
  `strict_runtime.published_strict_image()` resolves that lock (then a
  digest-pinned `CODECALC_STRICT_IMAGE`) as the production default, and
  `strict_execution_config()` builds the `GVisorConfig` from it. When no digest is
  published yet — the shipped state until the first dispatch — the execution path
  **fails closed** with `StrictImageUnavailable`, never falling back to the mutable
  local diagnostic tag `codecalc-exec:strict`, which `doctor` and the conformance
  suite keep using unchanged. The actual publish and first digest-pin still require
  an operator `workflow_dispatch` (there is no push/PR trigger).

- **Background runs: `run_submit`, `run_inspect`, `run_cancel`**.
  Submit code and get a `run_id` back immediately instead of holding an MCP
  call open for the whole computation; poll with `run_inspect`, stop early with
  `run_cancel`. Cancellation is honest about a provider that cannot cancel, and
  a cancelled run's result is still collectible rather than stranded. Admission
  is capped (`CODECALC_MAX_ACTIVE_RUNS`, default 64) and past the cap a
  submission is refused with `resource_exhausted` rather than growing the run
  table without bound.
- **Oversized session output spills to a workspace artifact**.
  Session output that the default 64 KiB cap would have truncated and DROPPED
  is captured up to 4 MiB and written into the session workspace instead;
  `stdout_spill` / `stderr_spill` carry a `codecalc://session/{id}/files/...`
  URI, and `stdout_spill_capped` / `stderr_spill_capped` say outright when the
  spill is fuller than the inline value but still not the whole stream. The
  inline value is byte-for-byte what it always was.
- **An execution receipt** naming WHAT ran and under WHICH conditions,
  alongside a published `ComputationSpec` schema with a content hash,
  so two runs of the same request are identifiable as such.
- **A grade vocabulary for `verify_*` results**. `z3_check`'s grading
  is narrowed to unsat-only rather than reading a `sat` answer as a proof.
- **Idle-expiry for abandoned stateful sessions**.
  `CODECALC_SESSION_IDLE_TTL_SECONDS`, unset by default: a session untouched
  for longer than this has its worker reaped on the next access, and a
  subsequent call gets `ok: false` with the stable `worker_failure` code —
  never a silent respawn. Every session entry point counts as a touch, not only
  `execute`.
- **A deny-by-default operator allowlist for package installs**.
- **A capability broker, deny-by-default network, and an audit stream**.
  A small policy layer whose one invariant is that the capabilities policy
  APPROVES for a job never exceed the ones the requester REQUESTED. Applied
  identically on the synchronous (`execute_code`/stream/session) and background
  (`run_submit`) paths — a policy the sync path enforces cannot be bypassed by
  moving the job to the background. Off by
  default (`CODECALC_CAPABILITY_POLICY` unset = today's behaviour). Set it and
  `deny-network` flips the default to network-denied unless a job requests and is
  granted network; `strict` refuses a job whose denial the provider cannot
  enforce; an escalation (policy granting a capability the request did not ask
  for) is refused with a stable `permission_denied` /
  `capability_not_requested`. The four sets — requested / approved /
  provider_supported / effective — are surfaced on a new `capabilities` block in
  the execution receipt (`receipt_version` `1.1.0` → **`1.2.0`**, a MINOR add
  inside the receipt; the result `contract_version` is unaffected because the
  block lives under the un-schema'd `provider` receipt). Broker decisions and
  security-relevant side effects (denied capability, refused install, cleanup)
  are appended to an audit stream at `~/.codecalc/audit/audit.log`
  (`CODECALC_AUDIT_LOG` relocates or disables it), each event source-safe
  (injected clock) and redacted of secrets.
- **A gate on the README's own gate-script count**, so the count of
  CI-invoked scripts cannot drift from the workflows the way the tool count
  once did.

### Fixed

- **Strict `/v1` service pinned a worker thread on a slow-drip body (slowloris)**.
  The pre-auth body read (`rfile.read(length)`, after the 1 MiB
  `MAX_CONTENT_LENGTH` check) had no read timeout, so a client that declared a
  legitimate sub-cap `Content-Length` then dribbled the bytes blocked a
  `ThreadingHTTPServer` worker indefinitely — before `dispatch()`, and unbounded
  by `MAX_CONCURRENT_RUNS`. `_read_body` now enforces a TOTAL wall-clock deadline
  (`MAX_BODY_READ_SECONDS`, 10s), recomputed each iteration over `rfile.read1()`
  so a slow trickle cannot slip under a per-`recv` gap; expiry drops the
  connection and frees the thread. The 413 oversized path and auth-gate ordering
  are unchanged.
- **Non-strict `deny-network` hard-errored on a provider that cannot enforce it**.
  The broker forced `no_net` onto the run whenever `network` was
  denied, regardless of whether the selected provider could enforce it. A
  provider that RAISES on an unenforceable `no_net` — the Piston adapter, whose
  network toggle is a server setting, not a per-request control — then returned a
  `validation` error, so a network-requesting job routed to it hard-errored
  instead of running-with-disclosure, contradicting the contract's "disclosed as
  effective where it cannot enforce." `capabilities.enforced_spec` now forces
  `no_net` only where the provider declared `network_control`; under a non-strict
  policy an unenforceable denial leaves the request as-asked and discloses the
  leak (`network` stays in the receipt's `effective` set). STRICT policy is
  unchanged — an unenforceable denial is still rejected before any side effect —
  and the native Linux shim path still enforces and blocks egress. No result
  shape or `contract_version` change: the disclosure already lived in the
  `effective` set.
- **The spill path wrote and deleted outside the session workspace.** The three
  spill helpers resolved the `.codecalc-spill` directory without the `_jail`
  guard every other session path uses. `mkdir(exist_ok=True)` does not follow a
  symlink at the final component, so executed code — which owns the workspace
  as its cwd — could replace that directory with a symlink and get both an
  arbitrary-location file CREATE and, through the retention prune's `*.bin`
  glob, an arbitrary `*.bin` UNLINK, in the unsandboxed server process.
- **A spill the server wrote could be impossible to read back.** The capture
  ceiling counts raw stream bytes; the file written is the `errors="replace"`
  re-encoding, where one invalid byte becomes a 3-byte U+FFFD. The write is
  now bounded by the same constant the resource read enforces, so any spill
  that exists is fetchable in full.
- **`CODECALC_MAX_ACTIVE_RUNS` set-but-empty crashed the server at import.**
  Empty, non-numeric and non-positive values now fall back to the default with
  a message on stderr, instead of `int("")` raising where nothing catches it.
- **A set-but-empty package allowlist denied all rather than allowing all.**

#### Cross-vendor review fix wave (correctness / API-design)

A second cross-vendor (Codex) review of the integrated branch found ten issues;
these nine were in this branch's diff and are fixed here (the tenth —
`optimization.py` accepting a candidate against an unvalidated `min_speedup ≤ 1`
— is pre-existing and out of this diff, ticketed separately; F7 below keeps the
GRADE honest in the meantime).

- **A background run whose provider RAISED was stranded forever** (F1). The run
  supervisor collected `future.result()` unchecked, so a provider error left the
  run stuck `running` with its admission slot held and its result unreachable
  via `run_inspect`. Any exception now becomes a terminal, coded failure —
  inspectable once, slot freed.
- **The result contract gained a fifth shape, `run_lifecycle`, and
  `contract_version` bumped `1.0.0` → `1.1.0`** (F2). `run_submit` /
  `run_inspect`-while-active / `run_cancel` responses were stamped a contract
  version but matched none of the four published shapes; they now validate, and
  the additive change bumps MINOR (the changelog previously claimed an addition
  left the version unchanged, which reversed semver).
- **Synchronous managed execution no longer discards a good result when cleanup
  fails** (F3). A `ProviderOperationFailure` from `cleanup()` replaced the
  collected stdout/verdict/receipt with an internal error; the result is now
  preserved and a `cleanup_error` field is appended, mirroring `run_inspect`.
- **A non-execute session touch after idle-expiry no longer revives the worker**
  (F4). `session_write_file` / `session_files` / `session_read_file` /
  `session_artifacts` refreshed the idle clock without first running the expiry
  gate, so a bare touch kept an expired worker alive; they now reap first, and
  the next `execute` gets the documented expiry error.
- **A completed-but-uninspected background run no longer holds admission
  capacity** (F5). Done futures are reaped before the admission count, so
  completion — not just inspection — frees a slot.
- **The execution receipt now records `session_id`** (F6, receipt
  `1.0.0` → `1.1.0`). The same spec runs in different sessions produced
  byte-identical receipts; the session is now named (workspace-state hashing is
  out of scope and stated as such).
- **`verify_optimization` grading no longer certifies a SLOWDOWN as
  `cross_checked`** (F7, `grade_rules_version` `1` → `2`). An accepted result
  whose measured speed ratio is not `> 1` is `ungraded` rather than graded as a
  speed-cross-checked optimisation.
- **The `max_output_kb` documentation no longer claims `0` means "uncapped"**
  (F8) — `0` selects the backends' 64 KiB default, and the doc now says so.
- **The first terminal `run_inspect` reports accurate cleanup state** (F9). It
  returned pre-cleanup status, so the first terminal read said `cleaned=false`
  and the next said `cleaned=true`; the status is now refreshed after cleanup.
- **The request-identity docs state identity is over the request AS SPELLED**
  (F10). Operationally-equivalent language aliases (`python` / `py` / `python3`)
  hash distinctly; the docs no longer imply two spellings are "the same
  computation".

---

## [0.1.0] — 2026-08-17

First public release. Nothing had been published to PyPI or crates.io before
it, so there was no upgrade path to describe — only what the thing is.

### Corrected before first publication

- **The Windows fork-bomb claim.** The README's platform table and `AUDIT.md`
  both described Windows' job-scoped `ActiveProcessLimit` as *better* than
  `RLIMIT_NPROC`'s uid-wide budget. Measurement says otherwise: 400 of 400
  spawns succeeded against a ceiling of 24 on Windows 11 Pro, because that limit
  comes from a process's immediate job and post-creation assignment does not
  guarantee ours is it. Both documents now state what is measured, and point at
  `process_limit_enforcement_unverified_on_windows`, which every post-creation
  Windows run declares. Corrected before `0.1.0` rather than after, because a
  rendered release page cannot be edited without cutting a new version.

### Added

- **Authenticated remote strict execution for macOS clients.** Setting
  `CODECALC_STRICT_URL` activates `<host>-strict`; the adapter verifies the
  Linux service's versioned `gvisor-v1` application-kernel, cgroup-v2,
  namespace, seccomp, filesystem, network, descendant, and resource-limit
  receipt before sending source. Missing or
  incomplete providers fail closed, managed run IDs are preserved through
  cancellation and cleanup, and authorization material is redacted.

- **Portable Linux gVisor launcher contract.** The strict service now has a
  shell-free Docker launcher for a digest-pinned executor image, explicit
  `runsc`, cgroup-v2 CPU/memory/PID limits, no network, read-only rootfs,
  bounded tmpfs, non-root UID, dropped capabilities, and
  `no-new-privileges`. It supports the x86_64 and ARM64 architectures supported
  by gVisor and fails closed when Docker, cgroup v2, or `runsc` is absent.

- **48 MCP tools across 31 languages.** Code execution, symbolic mathematics
  (SymPy), logic and SMT solving (Z3), exact decimal arithmetic, unit
  conversion, complexity analysis and benchmarking.
- **A Rust sandbox executor** (`codecalc-exec`) with rlimits, wall-clock and CPU
  ceilings, process-group kill, an output cap applied at the source, and an
  `LD_PRELOAD` shim that blocks network syscalls when `no_net` is requested.
- **A pure-Python fallback** for hosts without the built binary. It enforces
  strictly less and reports exactly what it could not apply in `unenforced`,
  rather than presenting a weaker sandbox as the same one.
- **A published result contract**, version `1.0.0`. Every result carries
  `contract_version`; the JSON Schema is at `docs/contract/result-v1.schema.json`
  (JSON Schema 2020-12, the dialect MCP `2026-07-28` defaults `outputSchema` to)
  and the policy is in `docs/contract/README.md`.
- **Eight stable error codes** with an actionable `remedy` on every failure —
  `validation`, `runtime_unavailable`, `timeout`, `resource_exhausted`,
  `permission_denied`, `dependency_missing`, `worker_failure`, `internal`.
  Branch on `code`; the prose in `error` is free to improve and is not a
  contract.
- **Byte counts behind truncation.** `stdout_bytes` / `stderr_bytes` report what
  the program produced before the response cap, so a caller can size a retry
  instead of guessing. Exact when `output_truncated` is false; a lower bound
  when it is true, because the two backends enforce the cap differently.
- **`codecalc doctor`** (also `python -m codecalc doctor`) — the install
  verification step. Reports the resolved backend and its binary, the install
  sandbox, extras, the status of every runtime, whether the workspace is
  writable, and the contract version, before a tool call has to. **Exits `0`
  when the install can execute and `1` when it cannot**, so it drops into a
  Dockerfile or a CI job unchanged. A missing extra or an uninstalled runtime
  does not fail it — those are facts about the host, not a broken install.
- **`codecalc doctor --json`** — the same report as machine-readable data,
  against a published schema (`docs/contract/doctor-v1.schema.json`) carrying
  the same `contract_version` and policy as a tool result. Each runtime reports
  `supported`, `installed`, `unhealthy` or `available`, and `status_basis` says
  whether those came from resolving the command or from running it. `--deep`
  executes them; without it nothing is ever reported `available`. `--deep` also
  reads each runtime's own `version`; `null` means **not measured** — no
  `--deep`, no version flag, or an unreadable answer — and never "no version".
- **`list_languages` reports what it measured.** Each entry carries `status`
  (`supported` or `installed`) and `status_basis` (`resolved`), the same
  vocabulary `doctor` uses. `available` is still there and unchanged, but it was
  computed from finding the command on `PATH` while being named for something
  stronger — on one host `bash` resolved, was reported available, and failed
  every time. Nothing is reported `available` without being run, which is what
  `doctor --deep` is for.
- **Verification gates instead of generation.** `verify_translation` and
  `verify_optimization` take a candidate *the caller wrote* and prove or
  disprove it by execution. codecalc runs and measures; it does not generate.
- **Warm sessions** with a persistent worker, workspace files and artifacts.
  Session results declare what a long-lived worker cannot enforce that a
  one-shot sandbox can.
- **Platform wheels carrying the executor**, plus standalone archives and a
  `SHA256SUMS` file for anyone not using pip.
- **`CODECALC_REQUIRE_NATIVE=1`** — refuse to start on the weaker fallback
  rather than silently downgrading.
- **Optional extras** so a minimal install stays small: `[symbolic]`,
  `[parsing]`. `codecalc doctor` names the exact command for anything missing.
- **A skill file** (`codecalc/SKILL.md`) shipped inside the wheel, describing
  when to call these tools and how to report their results.
- **`compare_execution` reports `discrepancies`.** Always present, empty when
  there is nothing to disclose. A language that produces no stdout while a
  sibling produces some is flagged rather than left as a blank row in a table —
  an exit-0 run with no output cannot be distinguished from one whose output was
  lost, and reading it as "the answer is empty" is how a lost result becomes a
  wrong one.

### Security

- **The Python fallback dropped `windir` on Windows.** `os.environ` upper-cases
  every key there, so the mixed-case allowlist entry never matched and the
  variable was filtered out of the child's environment — while the native
  executor passed it, because `std::env::var` is case-insensitive on Windows.
  `windir` is one of the two variables added because "node returned empty output
  with ok=false through the sandbox on Windows", so the fix that made node work
  there was only half-applied. Matching is now case-insensitive on Windows only;
  POSIX environment variables are genuinely case-sensitive and widening the
  allowlist there would weaken the boundary it exists to be.

- Apache-2.0 licensed. A pre-publication security audit ships in the repository
  as `AUDIT.md` — 2 critical, 3 high, 3 medium, every one confirmed with a real
  exploit attempt and fixed before the first release.
- **No `eval`, and `exec` in exactly one file**, gated by
  `scripts/check_no_eval.py` on every push.
- **Executed code sees an allowlisted environment only.** API keys and tokens in
  the host environment are never inherited.
- **Package installation is confined with Landlock** where the kernel provides
  it, and installer hooks are disabled by default.
- **Offline by construction.** The package contains no LLM client, no
  documentation fetch and no socket-capable import; `tests/test_offline.py`
  asserts this per module and cannot skip. Executed code still reaches the
  network unless `no_net` is both requested and enforceable — which the result
  says, per call.

### Fixed

- **Windows: the sandboxed child is now created suspended and assigned to the
  job before its first instruction.** It used to be spawned normally and
  assigned immediately after, leaving a window of microseconds in which it ran
  outside the job — anything it spawned there escaped every limit. AUDIT.md
  recorded this as a caveat that could only be closed by dropping
  `std::process::Command` for a raw `CreateProcessW`; that was wrong, and is why
  it stood. `CREATE_SUSPENDED` plus a PID-scoped thread resume closes the window
  to zero and keeps Command's pipes, environment, cwd and argument quoting. A
  child that cannot be placed in the job is killed rather than resumed.

  This does **not** fix the separate Windows failure where the process ceiling
  does not bind at all under an ambient job carrying `SILENT_BREAKAWAY_OK` —
  see the known limitation below, which is unchanged and still disclosed.

- **`analyze_complexity` downloads its grammars on first use, and the docs now
  say so.** tree-sitter grammars are not in the wheel: the pack ships a ~5 MB
  extension and fetches each grammar in-process into a local cache — 28
  grammars, 89 MB, ~15s cold. The README's network table said the package layer
  reaches the network `Never`, which was false, and the size table's `+5 MB` for
  `[parsing]` described the wheel rather than the cost. `codecalc doctor` now
  reports whether the cache is warm, and `scripts/prefetch_grammars.py` warms it
  for an offline install.

### Fixed

- **Windows process containment is creation-time and terminal by default.**
  `PROC_THREAD_ATTRIBUTE_JOB_LIST` makes CodeCalc's job the initial runtime's
  immediate job; `JOB_OBJECT_UILIMIT_EXITWINDOWS` prevents a launcher from
  hiding the payload behind a weaker nested job; and
  `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` restricts inherited handles to the three
  standard streams. Verified with a direct Python runtime on Windows 11 Pro:
  23 children against a total process limit of 24, then WinError 1816.
  `CODECALC_WIN_JOB_AT_CREATION=0` keeps the old post-creation route as an
  explicitly unverified compatibility escape hatch.

### Known limitations

- **On Windows the legacy process path is reported UNVERIFIED.**
  `process_limit_enforcement_unverified_on_windows` is emitted whenever the
  child is assigned to the job after creation. This route is now selected only
  with `CODECALC_WIN_JOB_AT_CREATION=0`. It is not a detected failure; it is an
  admission. Measured with the
  executor instrumented: three processes in one spawn chain reported three job
  contexts — the launcher `0x3000`, **the executor itself an EMPTY job `0x0`**,
  the sandboxed child `0x3000`, its grandchildren no job at all. The ambient
  check answers truthfully about a topology the child is not in, and no
  parent-side Win32 call returns another process's immediate job or effective
  `ActiveProcessLimit`. `ActiveProcessLimit` is also not combined across a
  nested chain — it comes from the *immediate* job — so the child's `APL 0`
  governs and codecalc's 24 is never consulted. The four detection strings below
  still fire: each can prove a failure, none can prove success, so their silence
  no longer implies enforcement.

- **On Windows the process ceiling may not bind, and now says so.** Measured on
  Windows 11 Pro from two unrelated launchers: the job-object `ActiveProcessLimit`
  is set, both API calls succeed, and 400 of 400 child spawns still go through.
  The run used to come back `ok: true` with nothing in `unenforced`. It now
  discloses one of `process_limit_not_enforced_child_escaped_the_job`,
  `process_limit_not_enforced_ambient_job_allows_breakaway`,
  `process_limit_membership_unverifiable_on_windows` or
  `process_limit_enforcement_unknown_on_windows`. The ceiling is **not** repaired
  — the root cause is open — but a caller can now tell an applied
  guarantee from an absent one, which is the difference that matters. GitHub's
  Server-SKU runner does bind it, which is why CI never showed this.

- **The distribution names are not claimed yet, and the README says so.**
  Measured live: `pypi.org/pypi/codecalc` returns 404 and
  `crates.io/api/v1/crates/codecalc-exec` returns 404. Both are free for anyone
  to register, which is why the README's install box leads with that rather
  than printing a `pip install` line that fetches whatever a stranger uploaded.
  Claiming them is a browser action on the maintainer's accounts and cannot be
  done from CI — [#91](https://github.com/The-40-Thieves/codecalc/issues/91).

  **npm `codecalc` is already taken** by an unrelated package ("a calculator
  created during NODE demo practicals", v1.0.0). This costs nothing: there is
  no `package.json` here and no JS client is planned. Recorded so the collision
  is not rediscovered as a surprise. If a JS client is ever published, the
  scoped `@the-40-thieves/codecalc` is available and is the name to use.

- **`bash` on Windows needs a Git-for-Windows-style build, and the path it is
  given is now separator-free.** Windows passes one command-line *string* and
  each runtime re-splits it; the MSYS2 runtime `bash` is built on treats `\` as
  an escape, so a workdir path arrived with every separator eaten. codecalc now
  hands those runtimes the bare file name, which resolves against the workdir
  that is already the child's cwd. The end-to-end case is **not gateable in CI** —
  the `windows-latest` image resolves `bash` through a Git-for-Windows install
  whose paths do not expose the stripping — so what CI gates is the argv
  rendering itself, on all three platforms.

- `no_net` needs the native executor's shim. The pure-Python fallback cannot
  apply it and says so in `unenforced`.
- `peak_memory_kb` is `null` on the fallback: `ru_maxrss` is a process-wide high
  water mark and cannot be attributed to one run. `null` means not measured,
  never zero.
- The `MLE` verdict is native-only. The fallback reports `RTE` for an
  out-of-memory kill rather than guessing at a ceiling it did not measure.
- Stateful sessions run in a plain subprocess, not under the Rust executor.
  Every session result lists the guarantees that therefore do not apply.
- A known intermittent fault on Windows: `node` can return empty stdout with
  `ok: false` through the sandbox. Tracked, with a dated reproduction, at
  [#42](https://github.com/The-40-Thieves/codecalc/issues/42).

[Unreleased]: https://github.com/The-40-Thieves/codecalc/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.10.0
[0.9.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.9.0
[0.8.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.8.0
[0.7.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.7.0
[0.6.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.6.0
[0.5.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.5.0
[0.4.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.4.0
[0.3.1]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.3.1
[0.3.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.3.0
[0.2.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.2.0
[0.1.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.1.0
