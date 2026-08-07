# codecalc — universal code & logic calculator for AI models

Run code in **31 languages**, evaluate symbolic math, solve logic problems, and
measure complexity — all exposed as MCP tools any AI model or agent can call.

## Architecture (language-per-strength)

| Layer | Language | Why |
|---|---|---|
| Executor core (`executor/`) | **Rust** | Sandbox + rlimits + process-group kill + JSON CLI. No `eval()` anywhere near user input; memory-safe host; single static binary |
| Logic layer (`codecalc/logic.py`) | **Python** | sympy (symbolic math, equation solving) and z3 (SMT) have no Rust equivalents |
| MCP server (`codecalc/server.py`) | **Python** | fastmcp auto-generates tool schemas from type hints |

Python orchestrates; Rust executes; sympy/z3 reason. Each layer does what it's
best at. The Rust binary is preferred automatically; a pure-Python executor is
the fallback if the binary is missing.

## Older-computer support

- `target-cpu=generic` — no modern instruction-set requirements
- **Static musl builds** run on any Linux regardless of glibc version:
  `bin/codecalc-exec-x86_64-musl` (421K), `bin/codecalc-exec-aarch64-musl` (453K)
- Size-optimized profile (`opt-level="z"`, LTO, panic=abort, stripped)
- **Lazy sympy/z3 imports**: server starts in ~40ms, not ~600ms
- `list_languages` probes runtime availability and reports which languages
  actually work on the machine (graceful degradation on minimal installs)

## Build the Rust core

```bash
cd executor
cargo build --release                          # native
cargo zigbuild --release --target x86_64-unknown-linux-musl   # static x86_64 (uses zig)
cargo zigbuild --release --target aarch64-unknown-linux-musl  # static arm64
cp target/release/codecalc-exec ../bin/        # Python picks it up from ../bin
```

Requires: Rust 1.97+, [cargo-zigbuild](https://github.com/rust-cross/cargo-zigbuild)
for the static cross-builds (zig is used as the linker; no x86_64 GCC needed).

## MCP tools (48) + MCP resources

Every session file is also exposed as an MCP resource:
`codecalc://session/<session_id>/files/<path>` — images render inline for the
model, text returns as text, other files download.

**Exact arithmetic & programmer-mode** (ported from the Claude calc skill —
`~/.claude/skills/calc/calc.py`): exact rationals, threshold checks, bit
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
| `translate_code` | **Port code between languages with verification**: LLM translates, executor runs both versions on the same test inputs, accepted only if outputs match (one retry with diff feedback) |
| `optimize_code` | **Optimize code with proof**: LLM proposes, executor verifies correctness AND measures speedup (same sizes, min-of-repeats); accepted only if correct AND measurably faster, else retried or honestly rejected |
| `extract_function` | Pull a named function + its dependency closure (imports, referenced helpers) into a standalone program and run it (ast-exact for python3, best-effort elsewhere) |
| `compare_edge_cases` | Run the same logic in N languages on edge-case inputs (empty, zero, negative, float precision) and flag behavioral divergence |
| `context7_docs` | Fetch up-to-date library docs from context7 (`/numpy/numpy`, `/golang/go`, `/Z3Prover/z3`...) — current API knowledge for any language |
| `convert_units` | Dimensional unit conversion via sympy: length, mass, time, speed, energy, power, force, pressure, temperature (°C/°F/K), volume, area, data, frequency |
| `physical_constants` | 22 physical constants with values (c, h, N_A, k_B, G, g, m_e, R, ...) |
| `list_units` | All 140+ unit aliases for convert_units |
| `evaluate_expression` | Symbolic math: `integrate(x**2, x)`, `sqrt(144) + 2**10` |
| `truth_table` | Boolean algebra: `a and b or not c`, `p xor q`, `a implies b` |
| `z3_check` | SMT-LIB2 satisfiability + model |
| `solve_linear` | Systems of equations: `x + y = 10; x - y = 2` |
| `analyze_complexity` | Static Big-O estimate from code structure |
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
cd ~/codecalc && .venv/bin/python -m codecalc.server
# stdio transport — register with any MCP client
```

Point an MCP client at it:

```json
{ "mcpServers": { "codecalc": { "command": "/home/ubuntu/codecalc/.venv/bin/python",
                                "args": ["-m", "codecalc.server"],
                                "env": { "PYTHONPATH": "/home/ubuntu/codecalc" } } } }
```

## Test

```bash
cd ~/codecalc
PYTHONPATH=. .venv/bin/python tests/test_smoke.py    # 31 languages via Rust executor
PYTHONPATH=. .venv/bin/python tests/test_mcp_all.py  # all 9 tools over MCP stdio
```

## Sandbox guarantees

- Fresh temp dir per run, deleted on exit (source + binaries + outputs)
- rlimits: CPU (timeout+8s), address space 2TiB (V8/JVM need huge VA),
  file size 256MiB, 256 FDs, core dumps off
- Wall-clock timeout kills the whole process group (SIGKILL)
- Output capped at 64KiB per stream
- No network namespace isolation (single-host tool; containerize for untrusted code)

## Language list

python3, node, bun, deno, typescript, ruby, php, perl, lua, tcl, r, elixir,
erlang, bash, zsh, mojo, swift, c, cpp/c++, rust, go, fortran, zig, java,
kotlin, csharp, gleam, haskell, sqlite, jq, awk — 31 runtimes, all local to
Cave (mise + apt + rustup + swiftly + nix).

## Notes

- Java uses single-file source launch (JEP 330). Kotlin compiles to a jar.
- csharp/gleam/haskell scaffold a temp project (dotnet new / gleam new / nix-shell).
- `benchmark` uses the stdin-N contract: code reads N from stdin, work sized by N.
