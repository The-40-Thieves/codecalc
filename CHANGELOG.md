# Changelog

All notable changes to this project are documented here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## Two version numbers, on purpose

This project versions **two** things, and they are not the same number.

| What | Where | Current |
|---|---|---|
| The **package** — the tool surface, the CLI, the Python API | `pyproject.toml`, `executor/Cargo.toml`, this file | `0.1.0` |
| The **result contract** — the shape every tool result comes back in | `docs/contract/README.md`, `contract_version` on every result | `1.2.0` |

The contract is at `1.2.0` and the package is at `0.1.0` because those claims are
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

### Fixed

- `compare_execution` (THE-802): a cold-start timeout on one language no
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

- **`install_package` confinement extended to macOS** (THE-819). Layer 2 of
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
  confinement (THE-818's job-object work is unverified on real Windows 11)
  but a documented no-op opt-in, `CODECALC_WIN_INSTALL_CONFINE`, adds a
  `package_install_confinement_unverified_on_windows` disclosure without
  claiming enforcement; the base `package_install_not_confined_no_landlock`
  disclosure keeps firing on Windows exactly as before.

- **Linux strict gVisor boundary made real** (THE-828). A real executor
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

- **Published, digest-pinned strict executor image** (THE-828, image residual).
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

- **Background runs: `run_submit`, `run_inspect`, `run_cancel`** (THE-778).
  Submit code and get a `run_id` back immediately instead of holding an MCP
  call open for the whole computation; poll with `run_inspect`, stop early with
  `run_cancel`. Cancellation is honest about a provider that cannot cancel, and
  a cancelled run's result is still collectible rather than stranded. Admission
  is capped (`CODECALC_MAX_ACTIVE_RUNS`, default 64) and past the cap a
  submission is refused with `resource_exhausted` rather than growing the run
  table without bound.
- **Oversized session output spills to a workspace artifact** (THE-783).
  Session output that the default 64 KiB cap would have truncated and DROPPED
  is captured up to 4 MiB and written into the session workspace instead;
  `stdout_spill` / `stderr_spill` carry a `codecalc://session/{id}/files/...`
  URI, and `stdout_spill_capped` / `stderr_spill_capped` say outright when the
  spill is fuller than the inline value but still not the whole stream. The
  inline value is byte-for-byte what it always was.
- **An execution receipt** naming WHAT ran and under WHICH conditions
  (THE-782), alongside a published `ComputationSpec` schema with a content hash
  (THE-793), so two runs of the same request are identifiable as such.
- **A grade vocabulary for `verify_*` results** (THE-785). `z3_check`'s grading
  is narrowed to unsat-only rather than reading a `sat` answer as a proof.
- **Idle-expiry for abandoned stateful sessions** (THE-779).
  `CODECALC_SESSION_IDLE_TTL_SECONDS`, unset by default: a session untouched
  for longer than this has its worker reaped on the next access, and a
  subsequent call gets `ok: false` with the stable `worker_failure` code —
  never a silent respawn. Every session entry point counts as a touch, not only
  `execute`.
- **A deny-by-default operator allowlist for package installs** (THE-791).
- **A capability broker, deny-by-default network, and an audit stream** (THE-787).
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
- **A gate on the README's own gate-script count** (THE-842), so the count of
  CI-invoked scripts cannot drift from the workflows the way the tool count
  once did.

### Fixed

- **Strict `/v1` service pinned a worker thread on a slow-drip body (slowloris)**
  (THE-851). The pre-auth body read (`rfile.read(length)`, after the 1 MiB
  `MAX_CONTENT_LENGTH` check) had no read timeout, so a client that declared a
  legitimate sub-cap `Content-Length` then dribbled the bytes blocked a
  `ThreadingHTTPServer` worker indefinitely — before `dispatch()`, and unbounded
  by `MAX_CONCURRENT_RUNS`. `_read_body` now enforces a TOTAL wall-clock deadline
  (`MAX_BODY_READ_SECONDS`, 10s), recomputed each iteration over `rfile.read1()`
  so a slow trickle cannot slip under a per-`recv` gap; expiry drops the
  connection and frees the thread. The 413 oversized path and auth-gate ordering
  are unchanged.
- **Non-strict `deny-network` hard-errored on a provider that cannot enforce it**
  (THE-847). The broker forced `no_net` onto the run whenever `network` was
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

[Unreleased]: https://github.com/The-40-Thieves/codecalc/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.2.0
[0.1.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.1.0
