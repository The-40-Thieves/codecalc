# Changelog

All notable changes to this project are documented here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## Two version numbers, on purpose

This project versions **two** things, and they are not the same number.

| What | Where | Current |
|---|---|---|
| The **package** — the tool surface, the CLI, the Python API | `pyproject.toml`, `executor/Cargo.toml`, this file | `0.1.0` |
| The **result contract** — the shape every tool result comes back in | `docs/contract/README.md`, `contract_version` on every result | `1.0.0` |

The contract is at `1.0.0` and the package is at `0.1.0` because those claims are
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

## [0.1.0] — unreleased

First public release. The tag will be `v0.1.0`; nothing has been published to
PyPI or crates.io before it, so there is no upgrade path to describe — only what
the thing is.

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

- **47 MCP tools across 31 languages.** Code execution, symbolic mathematics
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

### Known limitations

- **Windows: an opt-in path that supplies the job at process CREATION.**
  `CODECALC_WIN_JOB_AT_CREATION=1` builds the child with
  `PROC_THREAD_ATTRIBUTE_JOB_LIST` via `STARTUPINFOEX`, so codecalc's job is the
  child's **immediate** job rather than merely somewhere in its chain — which is
  the only arrangement where its `ActiveProcessLimit` is the one consulted.
  Off by default: it cannot be exercised on the machine it was written on, and
  it required hand-written MSVC argv quoting, the same class of bug as the
  `bash` failure above. The quoting function is compiled and unit-tested on
  every platform rather than only where it runs. A run that takes this path
  says so with `process_limit_job_assigned_at_creation_on_windows`, and a
  failure to create is reported rather than silently downgraded to the weaker
  path.

- **On Windows the process ceiling is now reported UNVERIFIED on every run.**
  `process_limit_enforcement_unverified_on_windows` is emitted whenever the
  child is assigned to the job after creation — which is every Windows run
  today. It is not a detected failure; it is an admission. Measured with the
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
  — the root cause is open (THE-818) — but a caller can now tell an applied
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

[0.1.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.1.0
