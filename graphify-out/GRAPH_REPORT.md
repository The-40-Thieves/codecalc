# Graph Report - codecalc  (2026-08-09)

## Corpus Check
- 58 files · ~115,474 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 743 nodes · 1203 edges · 73 communities (39 shown, 34 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 24 edges (avg confidence: 0.65)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `1e6e9c6f`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- exact.py
- optimization.py
- sessions.py
- unix.rs
- over_stdio
- logic.py
- parsing.py
- Per-platform unenforced reporting
- main.rs
- runtimes.py
- Contributing
- server.py
- tool
- units.py
- timeout_middleware
- guarded_call
- executor.py
- registry.py
- Fraction
- test_bug_sweep.py
- list_languages
- blocknet.c
- Machine-specific path gate
- Worker
- test_python_sweep.py
- _spawn_worker
- test_executor_sweep.py
- guarded.py
- benchmark
- bit_analysis
- bitop
- calc_exact
- test_session_jail.py
- compare_edge_cases
- compare_execution
- ExecutorBuildHook
- convert_units
- _first_non_finite
- evaluate_expression
- extract_function
- human_duration
- install_package
- execute
- percentage
- test_platform_contract.py
- radix_convert
- conftest.py
- session_stop
- test_calc_port.py
- base_repr
- session_write_file
- session_artifacts
- z3_check
- solve_linear
- runtimes_status
- collision_probability
- solve_expression
- simplify_expression
- compare_threshold
- gitleaks + trufflehog pair
- DCO sign-off requirement
- _worker_bootstrap.py
- codecalc
- execute_code
- int_widths
- physical_constants
- truth_table
- verify_translation
- verify_optimization

## God Nodes (most connected - your core abstractions)
1. `over_stdio()` - 14 edges
2. `execute()` - 13 edges
3. `_BoolParser` - 13 edges
4. `_session_dir()` - 13 edges
5. `reject_unsafe()` - 12 edges
6. `_run_step()` - 11 edges
7. `_execute_python()` - 11 edges
8. `guarded_call()` - 11 edges
9. `start()` - 11 edges
10. `_spawn_worker()` - 11 edges

## Surprising Connections (you probably didn't know these)
- `rejects()` --calls--> `_jail()`  [EXTRACTED]
  tests/test_session_jail.py → codecalc/sessions.py
- `main()` --calls--> `execute()`  [EXTRACTED]
  tests/test_smoke.py → codecalc/executor.py
- `main()` --calls--> `truth_table()`  [EXTRACTED]
  tests/test_smoke.py → codecalc/logic.py
- `loops()` --calls--> `analyse()`  [EXTRACTED]
  tests/test_parsing_vs_regex.py → codecalc/parsing.py
- `_worker_usable()` --calls--> `start()`  [EXTRACTED]
  tests/test_python_sweep.py → codecalc/sessions.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Gates that could report success while measuring nothing** — _github_workflows_ci_security_osv_lockfile_floor, _github_workflows_ci_rust_no_cargo_test, _github_workflows_ci_quality_excluded_tools, _github_workflows_ci_rust_blocknet_mechanism_check, _github_workflows_ci_python_protocol_gate [INFERRED 0.90]
- **Defects only a non-Linux target could reveal** — audit_maxrss_units, audit_platform_unenforced, _github_workflows_ci_rust_platform_matrix, _github_workflows_ci_python_windows_import_check, _github_workflows_ci_rust_blocknet_mechanism_check [EXTRACTED 1.00]
- **Security constants duplicated across the Rust and Python backends** — audit_env_allowlist, readme_runtime_path_config, audit_fork_bomb_guard, _github_workflows_ci_security_structural_gates [EXTRACTED 1.00]

## Communities (73 total, 34 thin omitted)

### Community 0 - "exact.py"
Cohesion: 0.10
Nodes (19): base_repr(), bit_analysis(), bitop(), collision_prob(), data_sizes(), epoch_time(), human_duration(), int_widths() (+11 more)

### Community 1 - "optimization.py"
Cohesion: 0.06
Nodes (38): codecalc — universal code & logic calculator for AI models., extract_function(), _generic_extract(), _py_extract(), Optimisation verification and function extraction. No LLM in this module.…, ast-based extraction for python: imports + referenced helpers + target., Best-effort extraction for non-python: keep imports + target function block by…, Extract `function_name` from `code` with its dependency closure, build a… (+30 more)

### Community 2 - "sessions.py"
Cohesion: 0.20
Nodes (17): Read a file from a session workspace. Text files return content. With…, Run a multi-file program in a session: execute `entry_file`, which may import…, session_read_file(), session_run(), artifacts(), _jail(), _list(), list_files() (+9 more)

### Community 3 - "unix.rs"
Cohesion: 0.08
Nodes (40): c_int, Drop, apply_no_net(), no_net_shim(), preload_env_var(), ResolvedLimits, Command, Option (+32 more)

### Community 4 - "over_stdio"
Cohesion: 0.07
Nodes (38): Exception, StdioServerParameters, data(), in_process(), over_stdio(), Shared helpers for connecting to the codecalc MCP server in tests. Two ways in,…, Adapt `stdio_client` to the `Transport` protocol Client expects. Client accepts…, Client bound to the server object; fastest, exercises handlers directly. (+30 more)

### Community 5 - "logic.py"
Cohesion: 0.07
Nodes (30): _BoolParser, _collect_vars(), _eval_bool(), evaluate_expression(), _math_transforms(), Logic layer: symbolic math (SymPy), truth tables, SMT solving (Z3). sympy and…, Tokenize a boolean expression: identifiers, parens, and keyword ops., Lazy sympy import; returns the module. (+22 more)

### Community 6 - "parsing.py"
Cohesion: 0.07
Nodes (31): analyze(), _count_loops(), _detect_recursion(), Complexity analyzer: static heuristic Big-O estimation. Language-agnostic…, Returns (loop_count, max_nesting_depth) via indentation-aware scan., Best-effort recursion detection: does any function call itself?, Estimate asymptotic complexity from the code's STRUCTURE. Loop counting and…, Check satisfiability of an SMT-LIB2 script; return sat/unsat/unknown + model.… (+23 more)

### Community 7 - "Per-platform unenforced reporting"
Cohesion: 0.08
Nodes (27): Rust toolchain coherence guard, Destructive step runs last, Negotiated-protocol conformance gate, Import-on-Windows regression check, Deliberately excluded quality tools, blocknet override-mechanism assertion, Static musl artifact assertion, No vacuous cargo test job (+19 more)

### Community 8 - "main.rs"
Cohesion: 0.14
Nodes (28): Default, canonical(), dir_identity(), executable_names(), execute(), first_cmd(), is_executable(), Lang (+20 more)

### Community 9 - "runtimes.py"
Cohesion: 0.13
Nodes (24): _check_apt(), _check_mise(), _check_npm(), _check_rustup(), _check_swiftly(), _check_uv(), elevated_apply_allowed(), _error() (+16 more)

### Community 10 - "Contributing"
Cohesion: 0.11
Nodes (17): Adding a language or a tool, Contributing, Counts are gated, Licence, Reporting security issues, Running the gates locally, Sign your commits, Style (+9 more)

### Community 11 - "server.py"
Cohesion: 0.13
Nodes (13): analyze_complexity(), epoch_time(), float_repr(), list_units(), MCP server exposing codecalc as model-usable tools. Built on the official SDK…, Start a persistent session. python3/node get a stateful REPL worker…, Estimate the asymptotic (Big-O) time complexity of a code snippet via…, Update language runtimes. SAFE BY DEFAULT: with apply=False this is a dry run —… (+5 more)

### Community 12 - "tool"
Cohesion: 0.13
Nodes (15): algebraic_equiv(), calc_stats(), data_sizes(), limit_expression(), percentiles(), List active sessions and their languages/state., List files in a session workspace (path is relative, '' = root)., mean, median, sample stdev, and coefficient of variation (CV). CV > 0.2 means… (+7 more)

### Community 13 - "units.py"
Cohesion: 0.14
Nodes (17): constants(), convert(), _is_number(), _is_unit(), list_units(), _parse_unit(), Unit conversion and physical constants. SymPy's physics.units powers…, Convert `value` from one unit to another (dimensional analysis). (+9 more)

### Community 14 - "timeout_middleware"
Cohesion: 0.21
Nodes (10): CallNext, Server middleware for the MCP 2.0 (protocol 2026-07-28) server.…, Enforce a per-tool response deadline. Non-tool methods (`tools/list`,…, timeout_middleware(), _tool_name(), HandlerResult, ServerRequestContext, check() (+2 more)

### Community 15 - "guarded_call"
Cohesion: 0.16
Nodes (17): _algebraic_equiv(), _limit_expression(), Are two expressions algebraically identical? (refactor verification) Identity…, Solve for a root or crossover: `x**2 - 4 = 0`, `2*x + 1 = 7`., Asymptotic behaviour: limit of EXPR as var -> point (default oo)., Simplified, factored and expanded forms of an expression., Warm the parent for the paths THESE tools take, once. Not shared with…, Are two expressions algebraically identical? (refactor verification) (+9 more)

### Community 16 - "executor.py"
Cohesion: 0.05
Nodes (53): backend(), _binary_candidates(), _BoundedDrain, _children_cpu_ms_since(), _children_cpu_seconds(), current_uid_tasks(), _dir_identity(), execute() (+45 more)

### Community 17 - "registry.py"
Cohesion: 0.18
Nodes (11): _env(), install(), Package installation inside sandbox workspaces. Installs packages with each…, Install a package for a language. Returns where it was installed., _c(), canonical(), Language registry: language -> execution plan (compile + run argv). Each entry…, Resolve any alias/display name to a registry key. (+3 more)

### Community 18 - "Fraction"
Cohesion: 0.14
Nodes (15): compare_threshold(), _ev(), eval_exact(), float_repr(), _int_op(), percentage(), _pow_exact(), Walk a closed set of AST nodes; anything else is refused. (+7 more)

### Community 19 - "test_bug_sweep.py"
Cohesion: 0.18
Nodes (6): _heavy_call_violation(), Screen every caller string before it reaches SymPy. SymPy evaluates the…, A heavy function applied to an oversized integer LITERAL, if any. Only literals…, Reason this string must not reach SymPy, or None if it may. Returns a message…, reject_unsafe(), Regressions for the bug sweep of 2026-08-08. Each block names the wrong…

### Community 20 - "list_languages"
Cohesion: 0.33
Nodes (6): probe(), Runtime availability per language (uses the Rust --probe when present)., all_languages(), Human-readable catalog for the MCP list_languages tool., list_languages(), List every language codecalc can execute, with extension, compile flag, and…

### Community 21 - "blocknet.c"
Cohesion: 0.54
Nodes (7): blocknet_addr_is_network(), blocknet_connect(), blocknet_is_network(), blocknet_socket(), connect(), socket(), socklen_t

### Community 22 - "Machine-specific path gate"
Cohesion: 0.50
Nodes (4): Machine-specific path gate, Graceful runtime degradation, No default LLM gateway, CODECALC_RUNTIME_PATH configuration

### Community 23 - "Worker"
Cohesion: 0.18
Nodes (6): Read one line from a pipe with a wall-clock timeout. None on timeout. Uses a…, Stateful interpreter: JSON-lines protocol on stdin/stdout. Globals persist…, Discard fd-1 output. It cannot be attributed to a call — it may be written by a…, Stop the worker and everything it started, then release its fds. Three things…, _readline_timeout(), Worker

### Community 24 - "test_python_sweep.py"
Cohesion: 0.20
Nodes (6): MCP resource: session workspace file. str for text, bytes for binary., session_file_resource(), resource, _maxrss_kib(), Regressions for the Python-side sweep of 2026-08-08. Fourteen defects across…, ru_maxrss in KiB on every platform. getrusage reports it in KiB on Linux and in…

### Community 26 - "_spawn_worker"
Cohesion: 0.22
Nodes (8): _proto_pipe(), Popen, `readline()` over a file another process appends to. A plain file object…, A pipe the child inherits for protocol responses, or None if impossible. The…, Create a session: fresh workspace dir; REPL worker for supported langs., _spawn_worker(), start(), _TailReader

### Community 27 - "test_executor_sweep.py"
Cohesion: 0.22
Nodes (8): check(), Regressions for the executor sweep (Rust + C + shell) of 2026-08-08. Ten…, No path may reach a shell as anything but a whole argv element. Stated over the…, String literals of a Rust `&[...]`, honouring backslash escapes., Parse every `Lang { ... }` entry by brace matching, not by line shape., _rust_strings(), _rust_table(), shell_invariant()

### Community 28 - "guarded.py"
Cohesion: 0.32
Nodes (7): _child(), Run SymPy where it can be KILLED, rather than where it must be trusted.…, Run `fn(*args, **kwargs)` in a killable child; return a result dict. Returns…, Wait for `pid`, without letting a stuck child block the caller forever., Run `fn` under rlimits and write its result back as JSON. Never returns. Every…, _reap(), run_guarded()

### Community 33 - "test_session_jail.py"
Cohesion: 0.25
Nodes (5): _can_symlink(), Path, Session workspace confinement. The bug these exist for: `_jail()` compared…, Probe the capability rather than assume it. Symlink creation needs a privilege…, rejects()

### Community 36 - "ExecutorBuildHook"
Cohesion: 0.29
Nodes (5): Any, BuildHookInterface, ExecutorBuildHook, Custom hatchling build hook: bundle the Rust executor into the wheel. Without…, force_includes the Rust executor (+ its --no-net shim) into the wheel.

### Community 38 - "_first_non_finite"
Cohesion: 0.33
Nodes (6): _first_non_finite(), percentiles(), Index of the first nan/inf in NUMS, or None if every value is finite. nan/inf…, mean, median, stdev (sample), and coefficient of variation (CV)., p50/p90/p95/p99 by nearest-rank AND linear interpolation., stats()

### Community 43 - "execute"
Cohesion: 0.33
Nodes (6): execute(), Run code in a session. Stateful langs go to the REPL worker; the rest run as…, Kill the worker (if any) and delete the workspace. Deletion is identity-checked…, stop(), Can a stateful worker for `lang` actually complete a call here? Presence on…, _worker_usable()

### Community 45 - "test_platform_contract.py"
Cohesion: 0.33
Nodes (3): enforced(), The `unenforced` array must tell the truth, on every platform. The executor's…, Does this platform claim to enforce `flag` on this run?

### Community 47 - "conftest.py"
Cohesion: 0.50
Nodes (3): pytest_report_header(), Why `pytest tests/` collects nothing here, and what to run instead. This suite…, Say why the run is empty, at the top, where the reason is still visible.…

### Community 62 - "_worker_bootstrap.py"
Cohesion: 0.25
Nodes (7): Stateful python3 REPL worker: JSON-lines request/response over a channel…, Point the OS-level standard handle at fd's handle; return the old one., Read a capture file, capped, reporting whether anything was dropped., _read_capped(), _respond(), _set_std_handle(), _write_proto()

## Knowledge Gaps
- **19 isolated node(s):** `codecalc`, `Sign your commits`, `Licence`, `The rule that matters most`, `Running the gates locally` (+14 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **34 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Worker` connect `Worker` to `sessions.py`, `_spawn_worker`?**
  _High betweenness centrality (0.016) - this node is a cross-community bridge._
- **Why does `reject_unsafe()` connect `test_bug_sweep.py` to `exact.py`, `logic.py`, `guarded_call`?**
  _High betweenness centrality (0.010) - this node is a cross-community bridge._
- **What connects `codecalc`, `Sign your commits`, `Licence` to the rest of the system?**
  _19 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `exact.py` be split into smaller, more focused modules?**
  _Cohesion score 0.1 - nodes in this community are weakly interconnected._
- **Should `optimization.py` be split into smaller, more focused modules?**
  _Cohesion score 0.05959183673469388 - nodes in this community are weakly interconnected._
- **Should `unix.rs` be split into smaller, more focused modules?**
  _Cohesion score 0.08139534883720931 - nodes in this community are weakly interconnected._
- **Should `over_stdio` be split into smaller, more focused modules?**
  _Cohesion score 0.0663265306122449 - nodes in this community are weakly interconnected._