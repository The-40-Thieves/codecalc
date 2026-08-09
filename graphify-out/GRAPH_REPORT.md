# Graph Report - .  (2026-08-09)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 743 nodes · 1203 edges · 73 communities (39 shown, 34 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 24 edges (avg confidence: 0.65)
- Token cost: 68,321 input · 1,128 output

## Graph Freshness
- Built from commit: `ea7dfc51`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Exact & Programmer-Mode Arithmetic
- Package Entry & Optimization Verification
- Session Workspace File Access
- Rust Executor Core & Limits
- MCP Client Test Helpers
- Boolean Expression Parser
- Static Complexity Analysis
- CI Gates & Build Matrix
- Executor CLI & Time Budget
- Language Runtime Self-Update
- Contributing & Security Docs
- MCP Server Tool Surface
- Stats & Symbolic Tool Descriptions
- Unit Conversion & Constants
- MCP Middleware & Protocol Tests
- Guarded Symbolic Math Tools
- Sandboxed Code Execution Backend
- Language Registry & Package Install
- Exact Expression Evaluation
- SymPy Input Safety Screening
- Language Availability Probing
- Network Blocking C Shim
- Runtime Path Configuration
- REPL Worker Process Management
- Session Resource & Python Tests
- Session Spawn & Pipe Reading
- Executor Shell Safety Tests
- Killable Child Process Guard
- Empirical Complexity Benchmarking
- Bit Analysis
- Bitwise Operations
- Exact Arithmetic Tool
- Session Workspace Jail Tests
- Cross-Language Edge Case Comparison
- Cross-Language Execution Comparison
- Rust Executor Build Hook
- Unit Conversion Tool
- Statistics & Percentiles
- Symbolic Expression Evaluation
- Function Extraction Tool
- Duration Humanization
- Package Installation Tool
- Session Execution & Teardown
- Percentage Calculation
- Platform Enforcement Contract Tests
- Radix Conversion
- Pytest Collection Explainer
- Session Stop
- Calc Feature Parity Tests
- Base Representation Tool
- Session File Write
- Session Artifact Listing
- Z3 SMT Solving
- Linear System Solving
- Runtime Update Status
- Hash Collision Probability
- Equation Root Solving
- Expression Simplification
- Threshold Comparison
- Secret Scanning Tools
- DCO Sign-Off Policy
- Python REPL Worker Bootstrap
- Codecalc Project Root
- Sandboxed Code Execution Tool
- Integer Width Fitting
- Physical Constant Lookup
- Truth Table Generation
- Translation Equivalence Proof
- Optimization Speedup Proof

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

### Community 0 - "Exact & Programmer-Mode Arithmetic"
Cohesion: 0.10
Nodes (19): base_repr(), bit_analysis(), bitop(), collision_prob(), data_sizes(), epoch_time(), human_duration(), int_widths() (+11 more)

### Community 1 - "Package Entry & Optimization Verification"
Cohesion: 0.06
Nodes (38): codecalc — universal code & logic calculator for AI models., extract_function(), _generic_extract(), _py_extract(), Optimisation verification and function extraction. No LLM in this module.…, ast-based extraction for python: imports + referenced helpers + target., Best-effort extraction for non-python: keep imports + target function block by…, Extract `function_name` from `code` with its dependency closure, build a… (+30 more)

### Community 2 - "Session Workspace File Access"
Cohesion: 0.20
Nodes (17): Read a file from a session workspace. Text files return content. With…, Run a multi-file program in a session: execute `entry_file`, which may import…, session_read_file(), session_run(), artifacts(), _jail(), _list(), list_files() (+9 more)

### Community 3 - "Rust Executor Core & Limits"
Cohesion: 0.08
Nodes (40): c_int, Drop, apply_no_net(), no_net_shim(), preload_env_var(), ResolvedLimits, Command, Option (+32 more)

### Community 4 - "MCP Client Test Helpers"
Cohesion: 0.07
Nodes (38): Exception, StdioServerParameters, data(), in_process(), over_stdio(), Shared helpers for connecting to the codecalc MCP server in tests. Two ways in,…, Adapt `stdio_client` to the `Transport` protocol Client expects. Client accepts…, Client bound to the server object; fastest, exercises handlers directly. (+30 more)

### Community 5 - "Boolean Expression Parser"
Cohesion: 0.07
Nodes (30): _BoolParser, _collect_vars(), _eval_bool(), evaluate_expression(), _math_transforms(), Logic layer: symbolic math (SymPy), truth tables, SMT solving (Z3). sympy and…, Tokenize a boolean expression: identifiers, parens, and keyword ops., Lazy sympy import; returns the module. (+22 more)

### Community 6 - "Static Complexity Analysis"
Cohesion: 0.07
Nodes (31): analyze(), _count_loops(), _detect_recursion(), Complexity analyzer: static heuristic Big-O estimation. Language-agnostic…, Returns (loop_count, max_nesting_depth) via indentation-aware scan., Best-effort recursion detection: does any function call itself?, Estimate asymptotic complexity from the code's STRUCTURE. Loop counting and…, Check satisfiability of an SMT-LIB2 script; return sat/unsat/unknown + model.… (+23 more)

### Community 7 - "CI Gates & Build Matrix"
Cohesion: 0.08
Nodes (27): Rust toolchain coherence guard, Destructive step runs last, Negotiated-protocol conformance gate, Import-on-Windows regression check, Deliberately excluded quality tools, blocknet override-mechanism assertion, Static musl artifact assertion, No vacuous cargo test job (+19 more)

### Community 8 - "Executor CLI & Time Budget"
Cohesion: 0.14
Nodes (28): Default, canonical(), dir_identity(), executable_names(), execute(), first_cmd(), is_executable(), Lang (+20 more)

### Community 9 - "Language Runtime Self-Update"
Cohesion: 0.13
Nodes (24): _check_apt(), _check_mise(), _check_npm(), _check_rustup(), _check_swiftly(), _check_uv(), elevated_apply_allowed(), _error() (+16 more)

### Community 10 - "Contributing & Security Docs"
Cohesion: 0.11
Nodes (17): Adding a language or a tool, Contributing, Counts are gated, Licence, Reporting security issues, Running the gates locally, Sign your commits, Style (+9 more)

### Community 11 - "MCP Server Tool Surface"
Cohesion: 0.13
Nodes (13): analyze_complexity(), epoch_time(), float_repr(), list_units(), MCP server exposing codecalc as model-usable tools. Built on the official SDK…, Start a persistent session. python3/node get a stateful REPL worker…, Estimate the asymptotic (Big-O) time complexity of a code snippet via…, Update language runtimes. SAFE BY DEFAULT: with apply=False this is a dry run —… (+5 more)

### Community 12 - "Stats & Symbolic Tool Descriptions"
Cohesion: 0.13
Nodes (15): algebraic_equiv(), calc_stats(), data_sizes(), limit_expression(), percentiles(), List active sessions and their languages/state., List files in a session workspace (path is relative, '' = root)., mean, median, sample stdev, and coefficient of variation (CV). CV > 0.2 means… (+7 more)

### Community 13 - "Unit Conversion & Constants"
Cohesion: 0.14
Nodes (17): constants(), convert(), _is_number(), _is_unit(), list_units(), _parse_unit(), Unit conversion and physical constants. SymPy's physics.units powers…, Convert `value` from one unit to another (dimensional analysis). (+9 more)

### Community 14 - "MCP Middleware & Protocol Tests"
Cohesion: 0.21
Nodes (10): CallNext, Server middleware for the MCP 2.0 (protocol 2026-07-28) server.…, Enforce a per-tool response deadline. Non-tool methods (`tools/list`,…, timeout_middleware(), _tool_name(), HandlerResult, ServerRequestContext, check() (+2 more)

### Community 15 - "Guarded Symbolic Math Tools"
Cohesion: 0.16
Nodes (17): _algebraic_equiv(), _limit_expression(), Are two expressions algebraically identical? (refactor verification) Identity…, Solve for a root or crossover: `x**2 - 4 = 0`, `2*x + 1 = 7`., Asymptotic behaviour: limit of EXPR as var -> point (default oo)., Simplified, factored and expanded forms of an expression., Warm the parent for the paths THESE tools take, once. Not shared with…, Are two expressions algebraically identical? (refactor verification) (+9 more)

### Community 16 - "Sandboxed Code Execution Backend"
Cohesion: 0.05
Nodes (53): backend(), _binary_candidates(), _BoundedDrain, _children_cpu_ms_since(), _children_cpu_seconds(), current_uid_tasks(), _dir_identity(), execute() (+45 more)

### Community 17 - "Language Registry & Package Install"
Cohesion: 0.18
Nodes (11): _env(), install(), Package installation inside sandbox workspaces. Installs packages with each…, Install a package for a language. Returns where it was installed., _c(), canonical(), Language registry: language -> execution plan (compile + run argv). Each entry…, Resolve any alias/display name to a registry key. (+3 more)

### Community 18 - "Exact Expression Evaluation"
Cohesion: 0.14
Nodes (15): compare_threshold(), _ev(), eval_exact(), float_repr(), _int_op(), percentage(), _pow_exact(), Walk a closed set of AST nodes; anything else is refused. (+7 more)

### Community 19 - "SymPy Input Safety Screening"
Cohesion: 0.18
Nodes (6): _heavy_call_violation(), Screen every caller string before it reaches SymPy. SymPy evaluates the…, A heavy function applied to an oversized integer LITERAL, if any. Only literals…, Reason this string must not reach SymPy, or None if it may. Returns a message…, reject_unsafe(), Regressions for the bug sweep of 2026-08-08. Each block names the wrong…

### Community 20 - "Language Availability Probing"
Cohesion: 0.33
Nodes (6): probe(), Runtime availability per language (uses the Rust --probe when present)., all_languages(), Human-readable catalog for the MCP list_languages tool., list_languages(), List every language codecalc can execute, with extension, compile flag, and…

### Community 21 - "Network Blocking C Shim"
Cohesion: 0.54
Nodes (7): blocknet_addr_is_network(), blocknet_connect(), blocknet_is_network(), blocknet_socket(), connect(), socket(), socklen_t

### Community 22 - "Runtime Path Configuration"
Cohesion: 0.50
Nodes (4): Machine-specific path gate, Graceful runtime degradation, No default LLM gateway, CODECALC_RUNTIME_PATH configuration

### Community 23 - "REPL Worker Process Management"
Cohesion: 0.18
Nodes (6): Read one line from a pipe with a wall-clock timeout. None on timeout. Uses a…, Stateful interpreter: JSON-lines protocol on stdin/stdout. Globals persist…, Discard fd-1 output. It cannot be attributed to a call — it may be written by a…, Stop the worker and everything it started, then release its fds. Three things…, _readline_timeout(), Worker

### Community 24 - "Session Resource & Python Tests"
Cohesion: 0.20
Nodes (6): MCP resource: session workspace file. str for text, bytes for binary., session_file_resource(), resource, _maxrss_kib(), Regressions for the Python-side sweep of 2026-08-08. Fourteen defects across…, ru_maxrss in KiB on every platform. getrusage reports it in KiB on Linux and in…

### Community 26 - "Session Spawn & Pipe Reading"
Cohesion: 0.22
Nodes (8): _proto_pipe(), Popen, `readline()` over a file another process appends to. A plain file object…, A pipe the child inherits for protocol responses, or None if impossible. The…, Create a session: fresh workspace dir; REPL worker for supported langs., _spawn_worker(), start(), _TailReader

### Community 27 - "Executor Shell Safety Tests"
Cohesion: 0.22
Nodes (8): check(), Regressions for the executor sweep (Rust + C + shell) of 2026-08-08. Ten…, No path may reach a shell as anything but a whole argv element. Stated over the…, String literals of a Rust `&[...]`, honouring backslash escapes., Parse every `Lang { ... }` entry by brace matching, not by line shape., _rust_strings(), _rust_table(), shell_invariant()

### Community 28 - "Killable Child Process Guard"
Cohesion: 0.32
Nodes (7): _child(), Run SymPy where it can be KILLED, rather than where it must be trusted.…, Run `fn(*args, **kwargs)` in a killable child; return a result dict. Returns…, Wait for `pid`, without letting a stuck child block the caller forever., Run `fn` under rlimits and write its result back as JSON. Never returns. Every…, _reap(), run_guarded()

### Community 33 - "Session Workspace Jail Tests"
Cohesion: 0.25
Nodes (5): _can_symlink(), Path, Session workspace confinement. The bug these exist for: `_jail()` compared…, Probe the capability rather than assume it. Symlink creation needs a privilege…, rejects()

### Community 36 - "Rust Executor Build Hook"
Cohesion: 0.29
Nodes (5): Any, BuildHookInterface, ExecutorBuildHook, Custom hatchling build hook: bundle the Rust executor into the wheel. Without…, force_includes the Rust executor (+ its --no-net shim) into the wheel.

### Community 38 - "Statistics & Percentiles"
Cohesion: 0.33
Nodes (6): _first_non_finite(), percentiles(), Index of the first nan/inf in NUMS, or None if every value is finite. nan/inf…, mean, median, stdev (sample), and coefficient of variation (CV)., p50/p90/p95/p99 by nearest-rank AND linear interpolation., stats()

### Community 43 - "Session Execution & Teardown"
Cohesion: 0.33
Nodes (6): execute(), Run code in a session. Stateful langs go to the REPL worker; the rest run as…, Kill the worker (if any) and delete the workspace. Deletion is identity-checked…, stop(), Can a stateful worker for `lang` actually complete a call here? Presence on…, _worker_usable()

### Community 45 - "Platform Enforcement Contract Tests"
Cohesion: 0.33
Nodes (3): enforced(), The `unenforced` array must tell the truth, on every platform. The executor's…, Does this platform claim to enforce `flag` on this run?

### Community 47 - "Pytest Collection Explainer"
Cohesion: 0.50
Nodes (3): pytest_report_header(), Why `pytest tests/` collects nothing here, and what to run instead. This suite…, Say why the run is empty, at the top, where the reason is still visible.…

### Community 62 - "Python REPL Worker Bootstrap"
Cohesion: 0.25
Nodes (7): Stateful python3 REPL worker: JSON-lines request/response over a channel…, Point the OS-level standard handle at fd's handle; return the old one., Read a capture file, capped, reporting whether anything was dropped., _read_capped(), _respond(), _set_std_handle(), _write_proto()

## Knowledge Gaps
- **19 isolated node(s):** `codecalc`, `Sign your commits`, `Licence`, `The rule that matters most`, `Running the gates locally` (+14 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **34 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Worker` connect `REPL Worker Process Management` to `Session Workspace File Access`, `Session Spawn & Pipe Reading`?**
  _High betweenness centrality (0.016) - this node is a cross-community bridge._
- **Why does `reject_unsafe()` connect `SymPy Input Safety Screening` to `Exact & Programmer-Mode Arithmetic`, `Boolean Expression Parser`, `Guarded Symbolic Math Tools`?**
  _High betweenness centrality (0.010) - this node is a cross-community bridge._
- **What connects `codecalc`, `Sign your commits`, `Licence` to the rest of the system?**
  _19 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Exact & Programmer-Mode Arithmetic` be split into smaller, more focused modules?**
  _Cohesion score 0.1 - nodes in this community are weakly interconnected._
- **Should `Package Entry & Optimization Verification` be split into smaller, more focused modules?**
  _Cohesion score 0.05959183673469388 - nodes in this community are weakly interconnected._
- **Should `Rust Executor Core & Limits` be split into smaller, more focused modules?**
  _Cohesion score 0.08139534883720931 - nodes in this community are weakly interconnected._
- **Should `MCP Client Test Helpers` be split into smaller, more focused modules?**
  _Cohesion score 0.0663265306122449 - nodes in this community are weakly interconnected._