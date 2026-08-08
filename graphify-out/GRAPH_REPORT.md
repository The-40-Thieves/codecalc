# Graph Report - codecalc  (2026-08-07)

## Corpus Check
- 52 files · ~53,223 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 564 nodes · 902 edges · 66 communities (29 shown, 37 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 17 edges (avg confidence: 0.71)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Exact Arithmetic Cluster
- LLM Gateway Client
- Session Workspaces & Packages
- Rust Platform Sandbox Layer
- MCP Test Harness
- Boolean Parser (Zero-Eval)
- Complexity Analysis
- CI Gate Rationale
- Rust Executor Core
- Runtime Self-Update
- Benchmark & Cross-Language Compare
- MCP Server Tool Surface
- Programmer-Mode Number Tools
- Units & Physical Constants
- MCP Timeout Middleware
- Context7 Documentation Fetch
- Executor Backend Selection
- Registry & Parity Gates
- Community 18
- Community 19
- Community 20
- Community 21
- Community 22
- Community 23
- Community 24
- Community 26
- Community 27
- Community 28
- Community 29
- Community 30
- Community 31
- Community 32
- Community 33
- Community 34
- Community 35
- Community 36
- Community 37
- Community 38
- Community 39
- Community 40
- Community 41
- Community 42
- Community 43
- Community 44
- Community 45
- Community 46
- Community 47
- Community 48
- Community 49
- Community 50
- Community 51
- Community 52
- Community 53
- Community 54
- Community 55
- Community 56
- Community 57
- Community 58
- Community 59
- Community 60
- Community 61
- Community 63

## God Nodes (most connected - your core abstractions)
1. `_session_dir()` - 14 edges
2. `over_stdio()` - 14 edges
3. `_BoolParser` - 13 edges
4. `_jail()` - 10 edges
5. `execute()` - 9 edges
6. `optimize_code()` - 9 edges
7. `start()` - 9 edges
8. `Worker` - 9 edges
9. `verify_translation()` - 9 edges
10. `run_step()` - 9 edges

## Surprising Connections (you probably didn't know these)
- `main()` --calls--> `analyze()`  [EXTRACTED]
  tests/test_smoke.py → codecalc/complexity.py
- `main()` --calls--> `execute()`  [EXTRACTED]
  tests/test_smoke.py → codecalc/executor.py
- `loops()` --calls--> `analyse()`  [EXTRACTED]
  tests/test_parsing_vs_regex.py → codecalc/parsing.py
- `rejects()` --calls--> `_jail()`  [EXTRACTED]
  tests/test_session_jail.py → codecalc/sessions.py
- `Language-per-strength architecture` --rationale_for--> `Zero-eval invariant`  [INFERRED]
  README.md → AUDIT.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Gates that could report success while measuring nothing** — _github_workflows_ci_security_osv_lockfile_floor, _github_workflows_ci_rust_no_cargo_test, _github_workflows_ci_quality_excluded_tools, _github_workflows_ci_rust_blocknet_mechanism_check, _github_workflows_ci_python_protocol_gate [INFERRED 0.90]
- **Defects only a non-Linux target could reveal** — audit_maxrss_units, audit_platform_unenforced, _github_workflows_ci_rust_platform_matrix, _github_workflows_ci_python_windows_import_check, _github_workflows_ci_rust_blocknet_mechanism_check [EXTRACTED 1.00]
- **Security constants duplicated across the Rust and Python backends** — audit_env_allowlist, readme_runtime_path_config, audit_fork_bomb_guard, _github_workflows_ci_security_structural_gates [EXTRACTED 1.00]

## Communities (66 total, 37 thin omitted)

### Community 0 - "Exact Arithmetic Cluster"
Cohesion: 0.05
Nodes (46): algebraic_equiv(), base_repr(), bit_analysis(), bitop(), collision_prob(), compare_threshold(), data_sizes(), epoch_time() (+38 more)

### Community 1 - "LLM Gateway Client"
Cohesion: 0.07
Nodes (39): codecalc — universal code & logic calculator for AI models., available(), chat(), _key(), Thin LLM client for the local LiteLLM gateway (OpenAI-compatible). Used by…, One chat completion through the gateway. Raises on failure., True when the gateway answers (cheap probe)., extract_function() (+31 more)

### Community 2 - "Session Workspaces & Packages"
Cohesion: 0.08
Nodes (37): install(), Package installation inside sandbox workspaces. Installs packages with each…, Install a package for a language. Returns where it was installed., canonical(), Resolve any alias/display name to a registry key., Read a file from a session workspace. Text files return content. With…, Run a multi-file program in a session: execute `entry_file`, which may import…, session_read_file() (+29 more)

### Community 3 - "Rust Platform Sandbox Layer"
Cohesion: 0.10
Nodes (32): Drop, apply_no_net(), no_net_shim(), preload_env_var(), ResolvedLimits, Command, Option, Path (+24 more)

### Community 4 - "MCP Test Harness"
Cohesion: 0.10
Nodes (26): StdioServerParameters, data(), in_process(), over_stdio(), Shared helpers for connecting to the codecalc MCP server in tests. Two ways in,…, Adapt `stdio_client` to the `Transport` protocol Client expects. Client accepts…, Client bound to the server object; fastest, exercises handlers directly., Client bound to a real `python -m codecalc.server` subprocess. (+18 more)

### Community 5 - "Boolean Parser (Zero-Eval)"
Cohesion: 0.11
Nodes (22): _BoolParser, _collect_vars(), _eval_bool(), evaluate_expression(), _math_transforms(), Logic layer: symbolic math (SymPy), truth tables, SMT solving (Z3). sympy and…, Recursive-descent parser for boolean expressions. NO eval — the input is parsed…, Lazy sympy import; returns the module. (+14 more)

### Community 6 - "Complexity Analysis"
Cohesion: 0.09
Nodes (26): analyze(), _count_loops(), _detect_recursion(), _llm_refine(), Complexity analyzer: static heuristic Big-O estimation. Language-agnostic…, Ask the configured gateway to second-guess the structural estimate. Goes…, Returns (loop_count, max_nesting_depth) via indentation-aware scan., Best-effort recursion detection: does any function call itself? (+18 more)

### Community 7 - "CI Gate Rationale"
Cohesion: 0.08
Nodes (27): Rust toolchain coherence guard, Destructive step runs last, Negotiated-protocol conformance gate, Import-on-Windows regression check, Deliberately excluded quality tools, blocknet override-mechanism assertion, Static musl artifact assertion, No vacuous cargo test job (+19 more)

### Community 8 - "Rust Executor Core"
Cohesion: 0.17
Nodes (25): Default, canonical(), executable_names(), execute(), first_cmd(), is_executable(), Lang, Limits (+17 more)

### Community 9 - "Runtime Self-Update"
Cohesion: 0.17
Nodes (15): _check_apt(), _check_mise(), _check_npm(), _check_rustup(), _check_swiftly(), _check_uv(), Runtime self-update for every language codecalc can execute. Each language is…, Parse `rustup check`: '<tc> - up to date: X' or '<tc> - update available: X ->… (+7 more)

### Community 10 - "Benchmark & Cross-Language Compare"
Cohesion: 0.17
Nodes (13): execute(), benchmark(), _classify_by_ratio(), compare_execution(), _fit_class(), _measure(), Higher-order tools: cross-language comparison and empirical benchmarking.…, Run the program at each size (min-of-repeats). Returns (runs, error). (+5 more)

### Community 11 - "MCP Server Tool Surface"
Cohesion: 0.15
Nodes (11): collision_probability(), data_sizes(), execute_code(), int_widths(), physical_constants(), MCP server exposing codecalc as model-usable tools. Built on the official SDK…, Execute `code` in `language` in a sandbox. Returns stdout, stderr, exit_code,…, Look up a physical constant (speed_of_light, planck, avogadro, gravity,… (+3 more)

### Community 12 - "Programmer-Mode Number Tools"
Cohesion: 0.15
Nodes (13): base_repr(), compare_threshold(), float_repr(), limit_expression(), optimize_code(), Build the truth table for a boolean expression: 'a and b or not c', 'p xor q',…, Exact threshold check with a verdict and the shortfall when it fails. `a OP b`…, hex/oct/bin of N; with WIDTH, two's complement and signed-overflow detection.… (+5 more)

### Community 13 - "Units & Physical Constants"
Cohesion: 0.19
Nodes (12): constants(), convert(), list_units(), _parse_unit(), Unit conversion and physical constants. SymPy's physics.units powers…, Convert `value` from one unit to another (dimensional analysis)., Parse a unit expression string into a sympy quantity. Restricted grammar only:…, Look up physical constants by name (or list all). (+4 more)

### Community 14 - "MCP Timeout Middleware"
Cohesion: 0.21
Nodes (10): CallNext, Server middleware for the MCP 2.0 (protocol 2026-07-28) server.…, Enforce a per-tool response deadline. Non-tool methods (`tools/list`,…, timeout_middleware(), _tool_name(), HandlerResult, ServerRequestContext, check() (+2 more)

### Community 15 - "Context7 Documentation Fetch"
Cohesion: 0.21
Nodes (11): discover(), docs(), docs_for_code(), docs_for_language(), _extract_imports(), Context7 integration: up-to-date library documentation for any language.…, Fetch LLM-reranked documentation context for a library., Docs for a language's standard library (best-effort). (+3 more)

### Community 16 - "Executor Backend Selection"
Cohesion: 0.24
Nodes (9): backend(), _binary_candidates(), _execute_python(), Sandboxed multi-language executor. Primary backend: the Rust `codecalc-exec`…, # NOTE: preexec_fn is unsafe under threads (PLW1509) — this is the PYTHON, rust' when the native binary is in use, else 'python'., Ordered candidate paths for the Rust executor, arch-aware., _rust_binary() (+1 more)

### Community 17 - "Registry & Parity Gates"
Cohesion: 0.22
Nodes (6): _c(), Language registry: language -> execution plan (compile + run argv). Each entry…, Build a registry entry from shell strings (compile may be None)., fail(), floor(), Refuse to compare an empty extraction — that is a broken gate, not a pass.

### Community 18 - "Community 18"
Cohesion: 0.33
Nodes (7): _env(), _kill_group(), Popen, Kill the child AND anything it spawned. Killing only the direct child orphans…, _run_step(), PATH handed to executed code. Precedence: CODECALC_RUNTIME_PATH, then this…, runtime_path()

### Community 19 - "Community 19"
Cohesion: 0.33
Nodes (6): current_uid_tasks(), _limits(), nproc_limit(), RLIMIT_NPROC for one execution: measured ambient tasks + headroom.…, preexec_fn applying rlimits, or None where there are no rlimits to apply.…, Total tasks (THREADS, not processes) owned by this real uid, machine-wide — the…

### Community 20 - "Community 20"
Cohesion: 0.33
Nodes (6): probe(), Runtime availability per language (uses the Rust --probe when present)., all_languages(), Human-readable catalog for the MCP list_languages tool., list_languages(), List every language codecalc can execute, with extension, compile flag, and…

### Community 21 - "Community 21"
Cohesion: 0.40
Nodes (3): blocknet_connect(), connect(), socklen_t

### Community 22 - "Community 22"
Cohesion: 0.50
Nodes (4): Machine-specific path gate, Graceful runtime degradation, No default LLM gateway, CODECALC_RUNTIME_PATH configuration

### Community 23 - "Community 23"
Cohesion: 0.67
Nodes (3): execute_code_stream(), Execute code and STREAM progress + partial output as it runs. Unlike…, Context

### Community 24 - "Community 24"
Cohesion: 0.67
Nodes (3): MCP resource: session workspace file. str for text, bytes for binary., session_file_resource(), resource

## Knowledge Gaps
- **5 isolated node(s):** `codecalc`, `Verified post-fix state`, `Platform support matrix`, `Sandbox guarantees`, `MCP protocol 2026-07-28`
  These have ≤1 connection - possible missing edges or undocumented components.
- **37 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `timeout_middleware()` connect `MCP Timeout Middleware` to `MCP Server Tool Surface`?**
  _High betweenness centrality (0.013) - this node is a cross-community bridge._
- **What connects `codecalc`, `Verified post-fix state`, `Platform support matrix` to the rest of the system?**
  _5 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Exact Arithmetic Cluster` be split into smaller, more focused modules?**
  _Cohesion score 0.04931972789115646 - nodes in this community are weakly interconnected._
- **Should `LLM Gateway Client` be split into smaller, more focused modules?**
  _Cohesion score 0.06547619047619048 - nodes in this community are weakly interconnected._
- **Should `Session Workspaces & Packages` be split into smaller, more focused modules?**
  _Cohesion score 0.07585568917668825 - nodes in this community are weakly interconnected._
- **Should `Rust Platform Sandbox Layer` be split into smaller, more focused modules?**
  _Cohesion score 0.09841269841269841 - nodes in this community are weakly interconnected._
- **Should `MCP Test Harness` be split into smaller, more focused modules?**
  _Cohesion score 0.0957983193277311 - nodes in this community are weakly interconnected._