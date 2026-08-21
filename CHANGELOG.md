# Changelog

All notable changes to this project are documented here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## Two version numbers, on purpose

This project versions **two** things, and they are not the same number.

| What | Where | Current |
|---|---|---|
| The **package** — the tool surface, the CLI, the Python API | `pyproject.toml`, `executor/Cargo.toml`, this file | `0.1.0` |
| The **result contract** — the shape every tool result comes back in | `docs/contract/README.md`, `contract_version` on every result | `1.3.0` |

The contract is at `1.3.0` and the package is at `0.1.0` because those claims are
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

### Security

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

### Added

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
  disk-usage commands**, the operational half of THE-894's disk
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
  appended to one file forever — the same unbounded-growth gap THE-894
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
- **`max_output_kb` enforced a ~1 MiB floor regardless of the request** (
  GH #206). The Rust executor's RLIMIT_FSIZE — the ceiling on how much the
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
  an unhandled crash** (GH #214, the follow-up to THE-875's
  argument-validation half). `evaluate_expression`/`simplify_expression`/
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
  the message by `ensure_code` — the same raise-site principle THE-781
  established, so a future refusal worded a new way cannot silently default
  to claiming a codecalc defect again.
- The MCP server's `instructions=` metadata said "30+ languages"; every
  other surface (README, SECURITY.md, the repo description) said the actual
  count, 31. `scripts/check_claims.py` now gates this string too (
  #213a).
- `data_sizes(n)` accepted a negative `n` and reported negative KiB/MB
  instead of rejecting it — the same bug shape `human_duration` already
  guards against for a negative duration. It now returns a validation error
  (#213b).
- `percentage`, `percentiles`, `collision_probability` and `human_duration`
  presented a `round()`ed float beside an exact one (a fraction string, an
  unrounded probability, the echoed input) with nothing marking which was
  which. Each result now carries a `"rounding"` field naming exactly which
  of its own keys were rounded and to how many decimal digits (
  #213c).
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

- **A versioned extension SDK — language packs, renderers, verifiers**
 . Every extension kind now has a versioned `Protocol`, a registry
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
  confinement ('s job-object work is unverified on real Windows 11)
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
- **An execution receipt** naming WHAT ran and under WHICH conditions
 , alongside a published `ComputationSpec` schema with a content hash
 , so two runs of the same request are identifiable as such.
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

- **Strict `/v1` service pinned a worker thread on a slow-drip body (slowloris)**
 . The pre-auth body read (`rfile.read(length)`, after the 1 MiB
  `MAX_CONTENT_LENGTH` check) had no read timeout, so a client that declared a
  legitimate sub-cap `Content-Length` then dribbled the bytes blocked a
  `ThreadingHTTPServer` worker indefinitely — before `dispatch()`, and unbounded
  by `MAX_CONCURRENT_RUNS`. `_read_body` now enforces a TOTAL wall-clock deadline
  (`MAX_BODY_READ_SECONDS`, 10s), recomputed each iteration over `rfile.read1()`
  so a slow trickle cannot slip under a per-`recv` gap; expiry drops the
  connection and frees the thread. The 413 oversized path and auth-gate ordering
  are unchanged.
- **Non-strict `deny-network` hard-errored on a provider that cannot enforce it**
 . The broker forced `no_net` onto the run whenever `network` was
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

[Unreleased]: https://github.com/The-40-Thieves/codecalc/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.4.0
[0.3.1]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.3.1
[0.3.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.3.0
[0.2.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.2.0
[0.1.0]: https://github.com/The-40-Thieves/codecalc/releases/tag/v0.1.0
