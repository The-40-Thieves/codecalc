# codecalc — universal code & logic calculator for AI models

Run code in **31 languages**, evaluate symbolic math, solve logic problems, and
measure complexity — all exposed as MCP tools any AI model or agent can call.

**codecalc itself opens no sockets.** No model, no API key, no gateway, no
telemetry: the caller is already a language model, so codecalc is the part that
runs things and measures them. `tests/test_offline.py` asserts this
structurally — nothing under `codecalc/` imports anything that can open a
socket, asserted per module.

That is a claim about the **package**, not about every tool call, and the
difference is worth stating rather than leaving a reader to discover:

| layer | reaches the network? |
|---|---|
| the codecalc package itself | **Never.** No HTTP client, no gateway, no telemetry |
| `install_package` | **Yes, by design.** It runs uv / npm / gem / cargo, which fetch from their registries. Installer hooks also run *outside* the sandbox — see [SECURITY.md](SECURITY.md) |
| `runtimes_status`, `update_runtimes` | **Yes.** They shell out to mise / rustup / swiftly / npm, which check remote versions |
| code you execute | **Yes, unless `no_net=True`** — and that shim needs the native executor, so the pure-Python fallback reports it in `unenforced` instead of applying it. Set `CODECALC_REQUIRE_NATIVE=1` to turn "fallback in use" into a startup failure instead of a result you have to notice by reading `unenforced` |

The earlier wording here was an unqualified "it makes no network calls", which
the structural test cannot support and three of the tools above contradict. A
guarantee stated more broadly than it is enforced is the failure this repo keeps
correcting, so it is corrected here too.

## Install

```bash
uvx codecalc          # run it directly, no environment to manage
# or
pip install codecalc  # into your own virtualenv
```

`.github/workflows/release.yml` publishes a platform-tagged wheel per target
(Linux x86_64/aarch64 musl, macOS x86_64/aarch64, Windows x86_64), each
carrying the matching `codecalc-exec` binary and — where the platform has
one — its `--no-net` shim, so `executor.backend() == "rust"` on install
without a manual build step. No wheel for your platform, or installed from
source instead? Everything still runs; see the network table above for what
falls back and to `unenforced` in that case.

Point an MCP client at the installed command:

```json
{ "mcpServers": { "codecalc": { "command": "uvx", "args": ["codecalc"] } } }
```

Building the Rust core yourself, or running from a checkout? See "Build the
Rust core" and "Run the server" below.

## Architecture (language-per-strength)

| Layer | Language | Why |
|---|---|---|
| Executor core (`executor/`) | **Rust** | Sandbox + rlimits + process-group kill + JSON CLI. No `eval()` anywhere near user input; memory-safe host; single static binary |
| Logic layer (`codecalc/logic.py`) | **Python** | sympy (symbolic math, equation solving) and z3 (SMT) have no Rust equivalents |
| MCP server (`codecalc/server.py`) | **Python** | the official `mcp` SDK (2.0) generates tool schemas from type hints; protocol **2026-07-28** |

Python orchestrates; Rust executes; sympy/z3 reason. Each layer does what it's
best at. The Rust binary is preferred automatically; a pure-Python executor is
the fallback if the binary is missing.

## Older-computer support

- **No modern instruction-set requirements** — rustc targets a generic CPU by
  default and nothing overrides it. (`executor/.cargo/config.toml` explains why
  `-C target-cpu=generic` is deliberately NOT written there: it would be a
  no-op that reads like a guarantee.)
- **Static musl builds** run on any Linux regardless of glibc version:
  `bin/codecalc-exec-x86_64-musl`, `bin/codecalc-exec-aarch64-musl` (~430K each;
  the exact size moves with every toolchain bump, so it is not pinned here)
- Size-optimized profile (`opt-level="z"`, LTO, panic=abort, stripped) —
  **measured, not assumed**: against an otherwise identical `opt-level=3` build,
  `z` came out 1.02 ± 0.26 times faster on the executor's own path (i.e. no
  detectable difference) while being 16% smaller. The executor spends its time
  in syscalls, not arithmetic, so there was nothing for a higher optimisation
  level to speed up.
- **Lazy sympy/z3 imports.** Both are imported on first use, so a session that
  only executes code never pays for them. This claimed "~40ms, not ~600ms" for a
  long time while being wrong in both directions: the server took **1.9s** to
  start, and sympy was not actually lazy — `units.py` imported it at module
  scope and `server.py` imports `units`, so every start paid 437ms for it.
  Deferring that took spawn-to-first-response from **1888ms to 1243ms**
  (measured, median of 7). The remaining ~870ms is the `mcp` SDK's own import,
  which is not ours to remove.
- **The fork-bomb measurement is taken once, and only when it is needed.**
  Sizing `RLIMIT_NPROC` means reading `/proc/<pid>/status` for every process on
  the machine. That walk used to run during argument parsing and again for every
  step: a C compile-and-run opened 1767 status files on a 590-process box to
  answer one question three times, and `--lang notalanguage` paid the full cost
  to produce a one-line error. Measured lazily and cached, an error costs 1.1ms
  instead of 13.3ms and a compiled run 78ms instead of 104ms.
- `list_languages` probes runtime availability and reports which languages
  actually work on the machine (graceful degradation on minimal installs)

## Build the Rust core

```bash
cd executor
cargo build --release                          # native
cargo zigbuild --release --target x86_64-unknown-linux-musl   # static x86_64 (uses zig)
cargo zigbuild --release --target aarch64-unknown-linux-musl  # static arm64
# Copy the executable AND its --no-net shim together. build.rs rebuilds the
# shim whenever blocknet.c changes, but the executor looks for it beside the
# BINARY, so installing only the binary leaves the previous shim in place — and
# a stale shim silently enforces the old policy while every "is it there?"
# check still passes. Copy both or neither.
cp target/release/codecalc-exec target/release/blocknet.so ../bin/
```

Requires: Rust 1.97+, a C compiler for the `--no-net` shim (the build warns and
carries on without one; `--no-net` then reports itself in `unenforced` rather
than pretending), and [cargo-zigbuild](https://github.com/rust-cross/cargo-zigbuild)
for the static cross-builds (zig is used as the linker; no x86_64 GCC needed).

## MCP tools (47) + MCP resources

Every session file is also exposed as an MCP resource:
`codecalc://session/<session_id>/files/<path>` — images render inline for the
model, text returns as text, other files download.

**Exact arithmetic & programmer-mode**: exact rationals, threshold checks, bit
analysis, binary64 introspection.

| Tool | Description |
|---|---|
| `calc_exact` | EXACT arithmetic: `0.1+0.2 == 0.3` is True; arbitrary-precision ints, bitwise ops inline, whitelisted math funcs, pi/e/tau |
| `compare_threshold` | Exact threshold verdict with shortfall: `('1/25', '>', '0.05')` → False, shortfall 1/100 |
| `percentage` | Exact share and percentage of PART/TOTAL (rationals accepted) |
| `calc_stats` | mean, median, sample stdev, **CV** (CV > 0.2 = noise swamps the effect) |
| `percentiles` | p50/p90/p95/p99 by nearest-rank AND interpolation; warns n<100 |
| `collision_probability` | Birthday-bound hash collision: 1e5 items/32 bits ≈ 0.69, 1e6/64 ≈ 2.7e-8 |
| `data_sizes` | Byte sizes both ways: KiB/MiB (binary) AND KB/MB (decimal) |
| `human_duration` | Humanised duration + per-day/per-30d rates |
| `epoch_time` | Epoch s/ms/µs/ns → ISO 8601 UTC, implausible readings suppressed |
| `base_repr` | hex/oct/bin + two's complement at WIDTH + signed-overflow detection |
| `radix_convert` | Any base 2..36, fractions included, non-termination flagged (`0.1` base 2) |
| `float_repr` | What binary64 actually stores: exact value, raw bits, ULP, neighbours, representable-or-not |
| `int_widths` | Which i8..i64/u8..u64 hold N + wrapped values; 2^53 JS/JSON caveat |
| `bit_analysis` | popcount, bit length, trailing zeros, next pow2, alignment padding |
| `bitop` | Programmer mode: and/or/xor/nand/nor/xnor/not/shl/shr/sar/rol/ror at 8/16/32/64, unsigned+signed+hex+oct+bin; shr vs sar distinction; shift-overflow flagged |
| `algebraic_equiv` | Are `(a*b)/c` and `a*(b/c)` identical? refactor verification (with float/truncation caveat) |
| `solve_expression` | Solve roots/crossovers: `x**2 - 4 = 0`, `2*x + 1 = 7` |
| `limit_expression` | Asymptotic limits: `n*log(n)/n**2` → 0 (settles complexity arguments) |
| `simplify_expression` | Simplified + factored + expanded forms |

**Core tools**

| Tool | Description |
|---|---|
| `list_languages` | 31 languages with extension, compile flag, runtime availability |
| `execute_code` | Run code in any language → stdout/stderr/exit_code/**verdict** (OK/TLE/MLE/OLE/RTE)/cpu_ms/peak_memory_kb; per-call limits (`max_memory_mb`, `max_output_kb`, `max_cpu`), `no_net`, `compact` |
| `execute_code_stream` | Like execute_code but reports progress + partial output while running |
| `session_start` | Persistent session; python3/node get a stateful REPL worker (variables/imports persist across calls), other languages a workspace dir |
| `session_stop` / `session_list` | Session lifecycle |
| `session_files` / `session_read_file` / `session_write_file` | Workspace file tools, jailed to the session dir; `session_read_file` returns images inline (as_image) |
| `session_run` | **Multi-file programs**: execute an entry file that imports other session files (helper.py, data/...) in the workspace |
| `session_artifacts` | List files created by executed code (results, images, CSVs) |
| `install_package` | Install packages (uv pip/npm/gem/go/cargo...) into a session or shared cache |
| `verify_translation` | **Prove a port is equivalent**: you write the translation, the executor runs both versions on the same inputs and reports match / diverged / inconclusive per input |
| `verify_optimization` | **Prove an optimisation**: you write the candidate, the executor confirms it still agrees with the original AND times both — accepted only if equivalent and measurably faster |
| `extract_function` | Pull a named function + its dependency closure (imports, referenced helpers) into a standalone program and run it (ast-exact for python3, best-effort elsewhere) |
| `compare_edge_cases` | Run the same logic in N languages on edge-case inputs (empty, zero, negative, float precision) and flag behavioral divergence |
| `convert_units` | Dimensional unit conversion via sympy: length, mass, time, speed, energy, power, force, pressure, temperature (°C/°F/K), volume, area, data, frequency |
| `physical_constants` | 22 physical constants with values (c, h, N_A, k_B, G, g, m_e, R, ...) |
| `list_units` | All 140+ unit aliases for convert_units |
| `evaluate_expression` | Symbolic math: `integrate(x**2, x)`, `sqrt(144) + 2**10` |
| `truth_table` | Boolean algebra: `a and b or not c`, `p xor q`, `a implies b` |
| `z3_check` | SMT-LIB2 satisfiability + model |
| `solve_linear` | Systems of equations: `x + y = 10; x - y = 2` |
| `analyze_complexity` | Static Big-O estimate from code structure, parsed with **tree-sitter** (every supported language). Reports `analysis: tree-sitter\|regex-fallback` so you can tell a parse from a guess |
| `benchmark` | Empirical Big-O: runs code at increasing N, fits growth curve |
| `compare_execution` | Same code across N languages side-by-side |
| `runtimes_status` | **Non-mutating** update check: current vs latest for every language runtime, which package manager owns it, and the command that would run |
| `update_runtimes` | Update runtimes. **Dry-run by default** (`apply=False` returns the commands); `apply=True` executes them |

## Runtime self-update

Every language is mapped to its package manager, and codecalc can update its own
runtimes:

| Manager | Languages | Update command |
|---|---|---|
| mise | python3, node, bun, deno, ruby, go, erlang, elixir, gleam, zig, java, kotlin, sqlite, duckdb, gradle | `mise up` |
| rustup | rust (stable/nightly toolchains) | `rustup update` |
| swiftly | swift | `swiftly update` |
| apt | c, c++, fortran, csharp, php, perl, lua, tcl, r, jq, bash, zsh | `apt-get install --only-upgrade` (language packages only) |
| npm | typescript/tsc | `npm update -g` |
| uv | mojo | `uv tool upgrade mojo` |
| nix | haskell (on-demand) | nothing persistent |

`runtimes_status` is always safe. `update_runtimes` refuses to mutate unless
`apply=True` is passed explicitly — and it only touches the package manager
that owns each language (never the Rust sandbox, which has no update powers).

## Run the server

```bash
cd /path/to/codecalc && .venv/bin/python -m codecalc.server
# stdio transport — register with any MCP client
```

Point an MCP client at it:

```json
{ "mcpServers": { "codecalc": { "command": "/path/to/codecalc/.venv/bin/python",
                                "args": ["-m", "codecalc.server"],
                                "env": {
                                  "PYTHONPATH": "/path/to/codecalc",
                                  "CODECALC_RUNTIME_PATH": "/path/to/mise/shims:/usr/local/bin:/usr/bin:/bin"
                                } } } }
```

## MCP protocol

Protocol revision **2026-07-28**, on the official `mcp` SDK 2.0. Not fastmcp:
fastmcp 3.x pins `mcp>=1.24,<2.0` and so cannot reach this revision at all.

Verifying that is less obvious than it looks. `mcp.types.LATEST_PROTOCOL_VERSION`
reads `2026-07-28` regardless of what a given connection negotiated, and the
*same server* answers on either protocol depending only on how you connect:

| client | negotiated | cache hints |
|---|---|---|
| `ClientSession.initialize()` | `2025-11-25` | dropped |
| `Client(..., mode="auto")` | **`2026-07-28`** | applied |

So `tests/test_mcp_protocol.py` asserts the negotiated value from a real
connection. The legacy path still works — backward compatibility is a feature —
it just must not be mistaken for the new protocol.

Worth noting for anyone reading the spec's headline change: 2026-07-28 removes
protocol-level sessions, and directs servers needing cross-call state to use
"explicit, server-minted handles passed as ordinary tool arguments". That is
exactly what codecalc's `session_id` already is.

## Configuration

All optional. codecalc runs with none of these set.

| Variable | Default | What it does |
|---|---|---|
| `CODECALC_RUNTIME_PATH` | the server's own `PATH`, else `/usr/local/bin:/usr/bin:/bin` | The `PATH` executed code resolves runtimes on. **Set this when an MCP client spawns the server**: clients often launch with a stripped environment, so an inherited `PATH` can miss a toolchain manager's shims entirely and most languages silently become unavailable. `list_languages` reports what actually resolved. |
| `CODECALC_EXEC_BIN` | `bin/codecalc-exec` (arch-matched) | Override the sandbox binary. Without one, codecalc falls back to a pure-Python executor — `list_languages` and `execute_code` still work, but the Rust path is the production one. |
| `CODECALC_REQUIRE_NATIVE` | *(unset)* | Fail-closed: refuse to start if no usable `codecalc-exec` binary was found (checked at import, so this is also a server-start check), instead of silently answering every call on the weaker Python fallback. Raises naming `CODECALC_REQUIRE_NATIVE` and the paths that were checked. |
| `CODECALC_SESSION_ROOT` | `~/.codecalc/sessions` | Where session workspaces live. |
| `CODECALC_PROCESS_HEADROOM` | `512` | Fork-bomb guard. `RLIMIT_NPROC` is a **uid-wide task budget**, not a per-sandbox one — the kernel compares it against every thread your user owns, machine-wide. So codecalc measures the ambient count per execution and sets the limit to *ambient + headroom*: a bomb can add at most this many tasks, while a runtime wanting a few threads always has room however busy the box is. |
| `CODECALC_MAX_PROCESSES` | *(unset)* | Escape hatch: pin `RLIMIT_NPROC` to an absolute value and skip the measurement. |

Both backends resolve `CODECALC_RUNTIME_PATH` identically, and
`scripts/check_parity.py` fails CI if the Rust and Python copies of that
contract ever drift — including if a machine-specific home directory finds its
way back into the default.

## Test

Each file is a standalone script that prints one `PASS`/`FAIL` line per
assertion and exits non-zero if any failed — no test runner, no plugins.

```bash
cd /path/to/codecalc

# everything
for f in tests/test_*.py; do PYTHONPATH=. .venv/bin/python "$f" || break; done
for f in scripts/*.py;    do PYTHONPATH=. .venv/bin/python "$f" || break; done

# or individually
PYTHONPATH=. .venv/bin/python tests/test_smoke.py           # every language, via the Rust executor
PYTHONPATH=. .venv/bin/python tests/test_mcp_all.py         # every tool over MCP stdio, answers checked
PYTHONPATH=. .venv/bin/python tests/test_executor_sweep.py  # sandbox regressions
```

20 test files and 5 gate scripts, **748 assertions**. Nothing in the suite
needs the internet, so none of it is ever skipped for lack of a network.

It **can** skip for lack of a *capability*, and that is correct rather than a
regression: a machine without a symlink privilege, without a given language
runtime, or without a built native executor cannot exercise the cases that
need them. The suite reports three distinct outcomes — the property holds, the
property is broken, and this machine cannot exercise it — and every skip names
its real cause. A nonzero skip count on Windows or in fallback mode is the
healthy result; what would be wrong is a skip reading as a pass.

This paragraph previously claimed **zero skips** unconditionally. That became
false the moment the suite learned to distinguish the third outcome, and
nothing gated it: `check_claims.py` gates the counts below, not the prose
around them. The counts are gated by
`scripts/check_claims.py`: they were written by hand once and were stale within
three pull requests, which is exactly the failure the rest of that script
exists to prevent. Four of the files are regression suites named after the
sweep that produced them — `test_bug_sweep`, `test_executor_sweep`,
`test_python_sweep`, `test_network_modules` — and each one's docstring states
the defect it locks out and how it was reproduced, because a regression test
whose reason has been forgotten is the first one deleted.

Two rules the suite holds itself to, learned from breaking both:

- **Assert the value, not the shape.** Three of these files once had no
  assertions at all: they called tools, printed the output and exited 0. They
  caught a crash and never a wrong answer — a `runtimes_status` total replaced
  with `-999` passed, printing `total = -999`.
- **Don't pin what varies.** `benchmark` and `compare_execution` rank by
  measured time, so their winner moves under load; their structure is asserted
  and their timing is not. `runtimes_status` is checked against itself — the
  summary must agree with the data it summarises — so it holds on any machine
  rather than describing this one.

## Platform support

Linux, macOS and Windows. The three do not offer the same primitives, and the
executor reports which ones it could **not** apply in an `unenforced` array on
every result rather than letting a caller assume they all held.

| Guarantee | Linux | macOS | Windows |
|---|---|---|---|
| Wall-clock timeout | yes | yes | yes |
| Kill the whole process tree | `killpg` + `PDEATHSIG` | `killpg` | `TerminateJobObject` |
| Fork-bomb guard | `RLIMIT_NPROC` (uid-wide) | `RLIMIT_NPROC` (uid-wide) | Job `ActiveProcessLimit` (**job-scoped**) |
| Memory ceiling | `RLIMIT_AS` | reported unenforced¹ | Job `ProcessMemoryLimit` |
| CPU-time ceiling | `RLIMIT_CPU` | `RLIMIT_CPU` | Job `PerProcessUserTimeLimit`⁴ |
| Open-file ceiling | `RLIMIT_NOFILE` | `RLIMIT_NOFILE` | reported unenforced |
| Output cap | yes | yes | yes (on read) |
| `no_net` | `LD_PRELOAD` shim² | `DYLD_INSERT_LIBRARIES`²˒³ | reported unenforced |
| Stateful sessions | yes | yes | yes |

¹ Darwin accepts `setrlimit(RLIMIT_AS)` but does not enforce address space the
way Linux does, so setting it would buy an illusion.
² Dynamically-linked programs only — a statically linked binary (Go, by default)
ignores it.
⁴ Applied via `JOB_OBJECT_LIMIT_PROCESS_TIME`, which Windows has supported
since XP — this was reported as `cpu_limit_unavailable_on_windows` until
2026-08-08, and the table said the same, so code and docs agreed with each other
and disagreed with Windows. It is not identical to `RLIMIT_CPU` and the
difference is reported rather than glossed: it counts **user-mode time only**, so
a process burning kernel time is not capped by it, and the system checks
periodically rather than immediately. Runs on Windows carry
`cpu_limit_counts_user_time_only_on_windows` in `unenforced` to say so.

³ Weaker still on macOS, in two ways. SIP and the hardened runtime strip
`DYLD_INSERT_LIBRARIES` for protected and hardened-signed binaries (most signed
interpreters), and dyld interposing does not reach calls made *inside* the
shared cache where libSystem lives — a program's own `connect()` is intercepted,
a system framework opening a connection internally is not. Treat macOS `no_net`
as a speed bump, never as isolation.

Both are exercised by the suite on every platform. The fork-bomb probe measures
the EAGAIN boundary precisely but needs `os.fork`, so it is POSIX-only; a second
probe SPAWNS processes instead, which is the portable operation, and pins the
ceiling low through `CODECALC_MAX_PROCESSES` so it costs two dozen short-lived
processes rather than walking up to the fallback. Verified to track the limit
rather than something incidental: a headroom of 24 bounds it at 22 children and
a headroom of 300 bounds it at 298.

Windows' `ActiveProcessLimit` is scoped to the **job**, which makes it a
genuinely better fork-bomb guard than `RLIMIT_NPROC`'s uid-wide budget — the
failure mode that broke 14 of 31 runtimes on Linux cannot occur there.

Two things degrade rather than fail on a given platform: languages whose runtime
is absent (`list_languages` reports `available: false`), and the shell-wrapper
languages — `bash`, `zsh`, `csharp`, `gleam`, `haskell` — which need a POSIX
shell and so are unavailable on Windows unless one is installed.

## Sandbox guarantees

- Fresh temp dir per run, deleted on exit (source + binaries + outputs). The
  deletion is **identity-checked**: the directory's device and inode are
  recorded at creation and re-checked before removal, because executed code runs
  with that directory as its cwd and can rename another one into its place. A
  caller-supplied `--workdir` is a session workspace and is never deleted.
  If the filesystem supplies no file index to identify the directory by, the
  deletion is **refused** rather than performed unverified, so temp directories
  accumulate there instead of the wrong one being removed. That trade is stated
  because it is the one this guarantee actually makes: it was previously
  implemented in the Rust executor only, and the Python fallback deleted
  unconditionally, which CI caught on Windows.
- rlimits: CPU (timeout+8s), address space 2TiB (V8/JVM need huge VA),
  file size 256MiB, 256 FDs, core dumps off
- The **timeout is a total budget**: compile and run share it, so `--timeout 10`
  cannot take twenty seconds. `duration_ms` is the run alone; `compile_ms` and
  `total_ms` are reported separately.
- Wall-clock timeout kills the whole process **group** (SIGKILL). So does
  SIGTERM to the executor — `PR_SET_PDEATHSIG` reaches only the direct child, so
  a group kill is what covers its descendants, and the executor is the only
  participant that knows the group id.
- Output capped at 64KiB per stream, on every path including stateful sessions.
  Exceeding it is reported as `OLE`, and the file-size rlimit is kept strictly
  above the cap so that overflow stays *detectable* — tying the two together
  turned a truncated 4MB output into a silent `verdict: OK`.
- Fork-bomb guard via `RLIMIT_NPROC`, sized from the **measured** ambient task
  count plus headroom rather than a fixed number. This is a mitigation, not
  isolation: the budget is shared with every other process your user owns, so
  concurrent executions draw on the same pool. cgroup v2 `pids.max` is the real
  per-sandbox answer and needs delegated cgroup access a stdio MCP server cannot
  assume — reach for it when this moves behind a container.
- `no_net` blocks the **network**, not every socket: it refuses `AF_INET` and
  `AF_INET6` and forwards everything else, so `AF_UNIX` local IPC keeps working.
- No network namespace isolation (single-host tool; containerize for untrusted code)
- Every result carries a `backend` field (`"rust"` or `"python"`) so a caller
  never has to infer which sandbox actually ran from an absent key — that was
  possible to confuse with an older build that never reported it at all. The
  pure-Python fallback cannot provide everything above: it has no `no_net`
  shim (reported in `unenforced`, not silently dropped), and `peak_memory_kb`
  comes back `None` rather than a number, because `ru_maxrss` is a
  process-lifetime high-water mark this path has no way to attribute to one
  run. `CODECALC_REQUIRE_NATIVE=1` turns "running on the fallback" into a
  startup failure instead of a guarantee you have to notice was quietly
  weaker.

### Sessions

A session is a persistent workspace; `python3` and `node` additionally get a
long-lived REPL worker so variables and imports survive between calls. What that
does and does not buy you:

| | workspace session | stateful worker |
|---|---|---|
| Fresh sandboxed process per call | yes | no — one worker serves every call |
| `max_memory_mb` / `max_cpu` / `no_net` | applied | **reported in `unenforced`** |
| `RLIMIT_AS` / `NPROC` / `FSIZE` / `NOFILE` | per call | applied once, at worker start |
| Output cap + `OLE` | yes | yes |
| Per-call wall clock | yes | yes — a worker that blows it is killed |

A worker cannot take a per-call rlimit after the fact, and `--no-net` is decided
at exec time. Rather than accept those arguments and drop them, the result lists
them in `unenforced` — the same field the executor already uses to say "asked
for, not applied". Omit `session_id`, or use a workspace session, when a ceiling
has to be real.

The worker protocol does not share a file descriptor with executed code, and
every response carries the id of the request it answers. Both matter: `sys.stdout`
is a Python-level rebind that a subprocess writes straight past, and a corrupted
stream that is not resynchronised returns every later call the *previous* call's
result — a well-formed answer to a different question.

The channel differs by platform and the guarantee does not. POSIX hands the
worker an out-of-band pipe; Windows has neither `pass_fds` nor `preexec_fn`, so
the worker appends responses to a file whose path arrives in the environment.
Either way a child spawned with inherited stdio writes to fd 1 and cannot reach
the protocol. Tests force the file-backed channel on every platform, because an
unexercised fallback is one that works until it is needed.

## Language list

python3, node, bun, deno, typescript, ruby, php, perl, lua, tcl, r, elixir,
erlang, bash, zsh, mojo, swift, c, cpp/c++, rust, go, fortran, zig, java,
kotlin, csharp, gleam, haskell, sqlite, jq, awk — 31 runtimes.

codecalc does not install any of them. It runs whatever is already on
`CODECALC_RUNTIME_PATH`, and `list_languages` probes each one and reports which
actually resolved, so a minimal machine degrades to the subset it has rather
than failing opaquely.

## Notes

- Java uses single-file source launch (JEP 330). Kotlin compiles to a jar.
- csharp/gleam/haskell scaffold a temp project (dotnet new / gleam new / nix-shell).
- `benchmark` uses the stdin-N contract: code reads N from stdin, work sized by N.

## CI

Five workflows, each documented inline with what it gates and — where a tool was
considered and rejected — why it is not there.

| Workflow | Gates |
|---|---|
| `ci-rust` | clippy `-D warnings`; the executor's **JSON contract**, asserted by running the built binary (OK/TLE/OLE/unknown-language) and confirming a canary secret in the executor's own env does not reach executed code; both **static musl** cross-builds, checked with `file` for static linkage; `blocknet.so` built `-Werror`, symbol-checked, and confirmed to actually block an outbound connection |
| `ci-python` | `ruff` at a genuine zero residual (ruleset and every exception in `pyproject.toml`, each with a reason); calc parity on 3.11 and 3.14; the security suite against the **Rust** backend, with an assertion that the Rust backend is the one under test; MCP stdio round-trip |
| `ci-security` | `scripts/check_no_eval.py` (the CRITICAL-01 invariant), `scripts/check_parity.py` (the three security constants duplicated in Rust and Python must match), `scripts/check_claims.py` (README counts and licence), `actionlint`, `gitleaks`, `trufflehog`, `osv-scanner`, `cargo-deny`, `cargo-audit`, and `opengrep` on a schedule |
| `ci-quality` | `typos`. **Not** `shellcheck` — the repo's last shell script was removed with `executor/zig-cc.sh`, so the gate would have matched zero files and reported success for scanning nothing; `actionlint` in `ci-security` shellchecks every embedded `run:` block instead. The workflow says so inline. |
| `dco` | `Signed-off-by` on every non-merge commit |

Two conventions run through all of them, both borrowed from harder-won experience:

- **Actions are pinned by commit SHA and downloaded tools by SHA-256.** A tag is
  mutable; a digest is not.
- **Every scan asserts it scanned something.** A linter pointed at a renamed
  directory, a dependency scanner with no lockfile to read, and a clean repo all
  produce the same output — exit 0. Each gate counts its inputs first and fails
  if the count is implausible.

## Licence

Apache-2.0. See [LICENSE](LICENSE).

Contributions require a DCO sign-off (`git commit -s`); `dco.yml` enforces it.
