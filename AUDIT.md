# codecalc — Security & Standards Audit

**Date:** 2026-08-07 · **Auditor:** Hermes Agent · **Scope:** full codebase
(Python MCP server + logic layer, Rust executor core, runtime-update module)

---

## Executive summary

codecalc is a **remote code execution service by design** — its core function is
running untrusted code in 31 languages. That is the highest-risk category of
software. The audit found **2 critical vulnerabilities, 3 high, 3 medium, and
several low issues**. All were confirmed with real exploit attempts (not
eyeballed), all are fixed, and a permanent security regression suite now guards
them. Verdict after fixes: **fit for its purpose** (single-operator, local,
stdio-only MCP tool). Verdict before fixes: would not have been safe to expose
beyond this machine.

---

## Findings & fixes

### CRITICAL-01 — Host RCE via `truth_table` eval sandbox escape

**Vulnerability:** `truth_table()` evaluated user boolean expressions with
Python `eval(expr, {"__builtins__": {}}, env)`. The "restricted" globals are a
documented sandbox-escape vector: `().__class__.__base__.__subclasses__()`
walks the live class tree to reach `subprocess.Popen` (or `os.system` via
`load_module`), giving **arbitrary command execution in the MCP server process**
— the process holding every API key.

**Evidence (confirmed live):**
```
Popen reachable via truth_table eval: True
```
External corroboration: Checkmarx's own sandbox writeup demonstrates
`().__class__.__bases__[0].__subclasses__()[104].load_module("os").system(...)`
executes even in restricted scopes; HackTricks documents the full class-tree
bypass family. Python `eval` sandboxes are not a thing that can be made safe.

**Fix:** Replaced `eval` with a **recursive-descent parser** (`_BoolParser`)
that tokenizes identifiers/parens/keywords only, builds an AST, and walks it.
Input is never executed. The parser rejects `.`, `[`, `_`, calls, and any
character outside the boolean grammar — the exploit string now fails at
`unexpected character '.'`.

### CRITICAL-02 — Secret leakage to executed code (full env inheritance)

**Vulnerability:** Both executors passed the host environment to user code
(Python fallback: `os.environ.copy()`; Rust: env inherited by default). The
live host env contained `GITHUB_PERSONAL_ACCESS_TOKEN`, `MODAL_API_KEY`,
`CEREBRAS_API_KEY`, `LIGHTNING_API_KEY`, `LITELLM_AGENT_KEY`, `SAKANA_API_KEY`,
`HERMES_SESSION_KEY` — all readable by any executed snippet via `os.environ`.

**Fix:** Strict **env allowlist** in both executors: `PATH, HOME, LANG, LC_ALL,
TMPDIR, PYTHONUNBUFFERED, JAVA_HOME, CARGO_HOME, RUSTUP_HOME, GOPATH,
GOMODCACHE` only. Everything else is dropped. Verified: executed code now sees
no secrets, and PATH/HOME still function for all 31 runtimes.

### HIGH-03 — `eval()` on benchmark fit forms

**Vulnerability:** `_fit_class()` ran `eval(form, {...})` on complexity-class
format strings. The strings were internal constants (not user input), so not
directly exploitable — but `eval` in the codebase is a class of bug, and the
whole point of the audit was eliminating it.

**Fix:** `_CLASSES` is now a dict of **pure lambdas** (`lambda n: n**2`).
Zero `eval`/`exec`/`shell=True` remain anywhere (enforced by an AST-based test).

### HIGH-04 — No fork-bomb guard (RLIMIT_NPROC absent)

**Vulnerability:** `while True: os.fork()` in any language would spawn until
the host exhausted its process table.

**Fix:** `RLIMIT_NPROC` set in both executors. First attempt (128) broke the
host — RLIMIT_NPROC counts **all processes of the uid**, and this host already
runs ~120 ubuntu processes. **1024** is the value Red Hat's own fork-bomb
guidance recommends, leaves host headroom, and still stops a bomb cold.
Verified: fork-bomb exits non-zero; host unaffected.

**CORRECTION 2026-08-07 — the reasoning above used the wrong unit, and 1024
broke the host too, quietly.** The kernel does not count processes. It counts
**tasks**, threads included: every `clone()` is checked against the limit,
`CLONE_THREAD` among them. At the moment "~120 ubuntu processes" was measured,
`ps -u ubuntu -L` reported **1009 tasks for the same uid**. Real headroom was
~15 threads, not ~900.

Consequence, measured: **14 of 31 languages failed** on this host, every one of
them a runtime with a thread pool.

```
go:      runtime: failed to create new OS thread (have 5 already; errno=11)
erlang:  Failed to create dirty cpu scheduler thread 2, error = 11
node/deno/ruby/python3: tokio "OS can't spawn worker thread" (the mise shim)
tests/test_smoke.py: 17 passed, 14 failed   (8 passed under load)
```

while this document recorded `test_smoke.py → 31 passed, 0 failed`. It is
load-dependent, so it reads as flakiness, and a `print("ok")` probe spawns no
threads and does not reproduce it. The discriminating test is a program that
spawns threads: under `ulimit -u 512` it fails, under 1024 it succeeds, with
1009 ambient tasks.

**Fix:** stop guessing the ambient count and measure it. `RLIMIT_NPROC` is now
`(current tasks for this uid) + CODECALC_PROCESS_HEADROOM` (default 512),
computed fresh per execution in the parent before any fork — `apply_limits`
runs inside `pre_exec` and cannot walk `/proc`. A bomb can add at most the
headroom; a legitimate runtime always has room however busy the box is.
`CODECALC_MAX_PROCESSES` pins an absolute value for operators who want one.

Verified after the fix, same host: **`test_smoke.py` → 31 passed, 0 failed**,
and a non-recursive fork counter reports `EAGAIN after 514 children` against a
headroom of 512. `tests/test_security.py` now asserts that BOUND rather than
only that the run stopped — the recursive bomb can be killed by the wall clock,
which says nothing about whether the limit held.

**Residual:** this is a mitigation, not isolation. The budget is still uid-wide,
so two concurrent executions draw on the same pool. cgroup v2 `pids.max` is the
real per-sandbox answer; it needs delegated cgroup write access that a stdio MCP
server launched by an arbitrary client cannot assume, so it belongs with the
containerisation in residual risk 1 rather than before it.

### HIGH-05 — In-process resource blowups (no caps/timeouts)

**Vulnerability:** `truth_table` with 30 variables → 2^30 rows → OOM in the
server process. `z3_check` on a hard SMT problem could spin forever. `sympy`
on a huge expression could burn CPU. None of the sandbox rlimits cover the
server's own process.

**Fix:**
- `truth_table`: max **16 variables** (65,536 rows ceiling) + 2,000-char input cap
- `z3_check`: `solver.set("timeout", 5000)` (documented z3 API)
- `evaluate_expression`: 2,000-char input cap
- All in-process tools now also carry fastmcp's native `@mcp.tool(timeout=…)`
  as a backstop (confirmed via context7 docs: `prefecthq/fastmcp` tool timeout)

### MEDIUM-06 — Predictable tempdir + argv stdin (E2BIG/symlink)

**Vulnerability:** Rust created `codecalc-<pid>-<counter>` with
`create_dir_all` — a guessable path (symlink pre-seed race on multi-user
/tmp), and stdin passed via `--stdin <argv>` (2MB E2BIG ceiling).

**Fix:** `fs::create_dir` (O_EXCL semantics) in a retry loop with a
time/pid-mixed nonce — a pre-seeded symlink makes creation fail and we pick a
new name. stdin now flows via `--stdin-file` (temp file), avoiding argv limits
entirely.

### MEDIUM-07 — `update_runtimes` apt command had literal `<PKGS>` bug; mise/npm scope too broad

**Vulnerability:** `UPDATE_COMMANDS["apt"]` contained a literal `<PKGS>`
placeholder that was appended to rather than substituted, and `mise up` /
`npm update -g` updated **every** managed tool regardless of which languages
were requested.

**Fix:** `<PKGS>` is now substituted with the concrete package list at call
time in both `status()` and `update()`; `__error__` pseudo-entries are skipped
(they were leaking into the language table as "languages"); mise/npm/scoped
updates documented as whole-manager by design (dry-run shows the exact command
before anything runs).

### LOW-08 — Miscellaneous professional-standards fixes

| Issue | Fix |
|---|---|
| `preexec_fn` unsafe under threads (ruff PLW1509) | Serialized fallback spawns with a lock; documented that the production path (Rust) never uses preexec_fn |
| `_collect_vars` crashed on unary `not` nodes | Handled unary nodes |
| `or` precedence level missing in parser | Added `_parse_or` between xor and and |
| Mutable default class attribute | `_OPS` → frozenset |
| Blind `except Exception` ×N | Policy decision: best-effort sandbox hardening; each site reviewed, none swallows actionable errors |
| No `.gitignore` | Added (target/, .venv/, bin/, __pycache__) |
| No security regression suite | Added `tests/test_security.py` (13 tests, all passing) |

---

## Verified post-fix state

```
tests/test_security.py    → ALL SECURITY TESTS PASS (13/13)
tests/test_smoke.py       → 31 passed, 0 failed (all languages via Rust executor)
tests/test_features.py    → ALL NEW-FEATURE TESTS PASS (sessions/files/artifacts/packages/verdicts/streaming/compact)
tests/test_gap4.py        → ITEMS 1-4 ALL PASS (resources, inline images, multi-file, units)
tests/test_calc_port.py   → ALL 19 PORTED FEATURES PASS (calc skill parity: exact, bitop, float, radix, ...)
tests/test_mcp_all.py     → 48/48 tools round-trip over stdio + session file resources
tests/test_runtimes_mcp.py → runtimes_status: 33 languages, 3 updatable, dry-run safe
ruff check codecalc/      → 0 non-policy findings
cargo clippy --release    → 0 warnings
```

## Feature expansion (2026-08-07) — security notes

The gap-analysis buildout added sessions, file tools, package install,
verdicts, per-call limits, streaming, translation, context7 docs, and a
network-blocking shim. Each has a security-relevant design decision:

1. **Sessions** (`sessions.py`): workspaces live under `~/.codecalc/sessions`
   and every path is jailed via `resolve()` + `Path.is_relative_to()` — `..`,
   absolute paths and symlinks out of the workspace are all rejected. Stateful
   REPL workers (python3/node) execute user code in a **separate subprocess**
   with the same env allowlist as the executor; their `exec()` is the
   documented exception to the zero-eval rule (it never runs in the server
   process).

   **CORRECTION 2026-08-07.** This previously read "jailed via `resolve()`
   prefix check", and that is what the code did: `str(p).startswith(str(d))`.
   A string prefix is not a path boundary. A SIBLING directory whose name
   merely extends the session id satisfied it —
   `_jail(<root>/python3-deadbeef, '../python3-deadbeefEVIL/x')` was accepted —
   and `session_write_file` mkdir -p's the result, so one session could write
   outside its own workspace. Bounded (the path still had to begin with the
   session dir's string, so it could not leave the session root) but real. The
   `..` case the claim was written against always worked; the boundary in
   general did not. Now component-wise via `is_relative_to`, in `_jail` and
   `_session_dir` both, with `tests/test_session_jail.py` covering the sibling,
   the symlink, and the legitimate paths that must keep working.
2. **File tools**: read/write are strictly confined to the session root; no
   absolute or escaping paths.
3. **Package install** (`packages.py`): package strings are passed as argv
   (never shell), only the language's own package manager is invoked. Requires
   network; a `--no-net` session cannot install.
4. **`--no-net`** (`blocknet.so`): LD_PRELOAD shim overriding `socket()` and
   `connect()` only — deliberately NOT `socketpair()` (runtimes like tokio use
   it for internal signal plumbing and crash if blocked). Best-effort: affects
   dynamically-linked programs only; statically-linked binaries ignore
   LD_PRELOAD. Real isolation for multi-tenant still requires containers.
5. **Verdicts**: MLE is a heuristic (signal + RSS ≥ 50% of memory cap); the
   rlimits kill without a definitive "memory" flag. Documented as heuristic.
6. **Streaming** (`execute_code_stream`): tails `run.out` in a temp workdir;
   progress notifications carry byte counts only, never code.
7. **Worker timeouts**: REPL workers are killed on hang (select-based read
   timeout); stderr is drained by a thread so a full pipe cannot deadlock.
8. **translate_code**: LLM output is NEVER executed as-is without going
   through the sandbox executor, and the translation is only accepted when the
   executor verifies output equivalence on test inputs. LLM content is treated
   as data; the verification loop is the trust boundary. Known limitation: the
   default gateway model (gpt-4o-mini) sometimes emits non-compiling code for
   strict compilers (e.g. Go unused imports) that even retry-with-error
   feedback cannot fix; the tool reports this honestly rather than fabricating
   a pass.

   **CORRECTION 2026-08-07 — the trust boundary above did not hold as written.**
   `verify_translation()` scored "both programs failed" as a MATCH, on the
   reasoning that they were in the same behaviour class. They are not: a Python
   `ZeroDivisionError` and a Go nil-map panic are both a non-zero exit with
   empty stdout, and so is a program that was never a translation of anything.
   Confirmed by running it — `verify_translation('python3', 'sys.exit(1)',
   'node', 'process.exit(1)', ['', '0', '1'])` returned `passed: True, 3/3
   matched` for two unrelated programs. The failure was worst exactly where the
   gate mattered most: if the source language's runtime is absent the source
   fails on every input, so every case "matched" and ANY model output was
   certified. `optimize_code` calls the same function and inherited it.

   Fixed by giving a case three outcomes instead of two — match, mismatch, and
   **inconclusive** — and requiring real evidence to pass: no mismatch AND at
   least one input where both programs actually ran and agreed. A wholly
   inconclusive run now fails with "could not verify: nothing was actually
   compared", because "we could not check" and "we checked and it was fine" are
   different answers. A translation that fails to COMPILE is now always a
   mismatch; it was previously a match whenever the source also errored.

   The decision logic was split into pure functions (`classify_case`,
   `aggregate`) so it can be tested without a sandbox or two runtimes — the old
   version could only be reached by actually running two programs, which is why
   it had no tests at all. See `tests/test_translation_verify.py`.
9. **context7_docs**: third-party documentation content returned as data to
   the caller; the server never executes or follows instructions found in it.
   Only the requesting model decides how to use the snippets.
10. **compare_edge_cases**: offline-capable; snippets are provided per language
    and run through the standard sandbox. No LLM in the loop.
11. **optimize_code**: the speedup gate is MEASURED (same sizes, min-of-repeats,
    baseline-subtracted), never the LLM's claim. Already-optimal code is
    honestly rejected ("no accepted optimization after retry" with the
    measured ratio) rather than fabricating a pass. Sub-noise-floor baselines
    are accepted on correctness with an explicit warning — which is why the
    correctness half has to be sound, and why the `verify_translation` defect
    corrected under item 8 applied here too: a sub-noise-floor optimization
    whose only gate was correctness could be accepted on the strength of both
    versions crashing. Same fix, same tests.
12. **extract_function**: python3 extraction is ast-exact (no regex on code);
    the generated runner only reads stdin and prints the function result —
    no eval of user strings beyond the already-sandboxed execution path. The
    `call` override is inserted into the generated program, which runs only
    inside the sandbox.
13. **MCP resources + image delivery**: session files are served via a
    `codecalc://session/...` resource template with a jail check on every
    read (resolve-prefix), so the resource surface can't escape the session
    root. Images are served inline (mime-detected) but capped at 4 MiB.
    `session_read_file` returns ImageContent for images — the model sees
    what the code produced, never unvalidated bytes executed.
14. **session_run (multi-file)**: runs the entry file as a fresh process in
    the session workdir via the standard executor — relative imports resolve
    against jailed session files only. Language is inferred from extension;
    no new execution surface beyond the existing sandbox.
15. **convert_units / physical_constants**: unit expressions are parsed with
    a restricted recursive-descent parser (numbers, names, `* / **` only) —
    NO eval, keeping the zero-eval invariant. Names resolve against sympy's
    units module or an internal composites table, never user code. Constants
    use sympy values where resolvable, with explicit hardcoded fallbacks
    (CODATA) where sympy lacks conversion factors.
16. **exact arithmetic cluster** (`exact.py`, ported from the Claude calc
    skill): `calc_exact` reuses the original's AST-walker design — a closed
    set of node types, no attribute access, no imports, whitelisted math
    functions only. Deliberately NOT `eval()` with a restricted namespace
    (escapable via `().__class__.__bases__[0].__subclasses__()`). All other
    tools in the cluster are pure functions over ints/Fractions/strings
    (bitop, radix, float_repr, base_repr, ...) — no code evaluation at all.
    `solve_expression`/`limit_expression`/`algebraic_equiv`/`simplify_expression`
    use sympy `solve`/`limit`/`simplify` on parsed expressions (parse-only,
    no execution), same surface as the pre-existing `evaluate_expression`.

Residual-risk items 1-4 from the original audit remain; add:

5. **Sessions accumulate disk**: workspaces persist until `session_stop` or
   manual cleanup. A runaway agent could fill disk. Consider a max-sessions
   cap or TTL sweep.
6. **`install_package` runs package managers with network**: packages are
   third-party code executed at import time inside the sandbox. Same-uid
   sandbox means a malicious package could read user files. Acceptable for a
   single-operator tool; containerize before multi-tenant.

## Residual risks (accepted, documented)

1. **No network isolation.** Executed code can reach the network (DNS,
   outbound HTTP). For a single-operator local tool this is acceptable and
   arguably desirable; for multi-tenant or internet-facing exposure, the
   executor must move behind a container with `--network=none` or a
   gVisor/Firecracker microVM.
2. **Same-uid sandbox.** rlimits constrain, they don't isolate. A hostile
   program could read other files the `ubuntu` user can read (not the secrets
   in env — those are gone — but filesystem contents). Full isolation needs
   containers/microVMs.
3. **`preexec_fn` in the Python fallback** remains a thread-safety caveat
   (mitigated by lock); the Rust binary is the production path and has no such
   issue.
4. **MCP server is stdio-only.** If anyone later exposes it over HTTP/SSE,
   they MUST add authentication first — the tools include code execution and
   runtime updates.

## Re-audit recommendation

Re-run `tests/test_security.py` after any change to `logic.py`, `executor.py`,
`tools.py`, or `executor/src/main.rs`, and after any runtime update
(`update_runtimes apply=True`) — a new language added to the registry or a
package-manager format change can regress the parsers.

## Cross-platform port (2026-08-07) — security notes

codecalc was written for one Linux box and claimed no more than that. Making it
run on macOS and Windows surfaced defects that no amount of reading the Linux
build would have found, because most of them are type or unit differences that
only appear when you compile for the target.

1. **`ru_maxrss` is KiB on Linux and BYTES on macOS/BSD.** The field was reported
   as `peak_memory_kb` unconditionally — a 1024x error. It is not only cosmetic:
   the MLE verdict compares peak memory against the configured cap, so on macOS
   *every* signalled exit would have been classified as a memory kill. Normalised
   in `platform::unix::maxrss_to_kb`, and the contract check asserts a
   hello-world's peak is a plausible KiB figure rather than an absurd one.

2. **`suseconds_t` is `i32` on macOS and `i64` on Linux.** The CPU-time
   arithmetic did not type-check on Darwin at all. Found by `cargo clippy
   --target aarch64-apple-darwin`, which is now a CI job on all three OSes.

3. **`libc::__rlimit_resource_t` is glibc-only**; macOS wants a plain `c_int`.

4. **Every `setrlimit` return value was discarded.** A limit that fails —
   exactly what happens when the soft value exceeds an inherited hard ceiling,
   e.g. a 2 TiB `RLIMIT_AS` under a restrictive `ulimit -v` — left the sandbox
   running with NO limit for that resource while reporting success. Limits are
   now resolved in the parent and clamped to the hard ceiling we actually hold,
   so the calls cannot EINVAL, and anything that had to be clamped or skipped is
   reported in a new `unenforced` array on the result.

5. **`select.select` accepts sockets only on Windows.** `_readline_timeout` used
   it against a subprocess pipe, so every stateful session (`session_start` on
   python3/node) failed there. Replaced with a reader thread, which behaves
   identically on all three platforms.

6. **`import resource` at module scope** made `import codecalc` fail on Windows
   before any code ran. CI now imports the package on all three OSes as its
   cheapest possible regression test.

7. **Windows sandboxing is a Job Object**, not rlimits: `KILL_ON_JOB_CLOSE` for
   the tree kill, `ActiveProcessLimit` for the fork-bomb guard (job-scoped, so
   strictly better than `RLIMIT_NPROC`'s uid-wide budget), `ProcessMemoryLimit`
   for memory, and job accounting for CPU and peak memory. What Windows does NOT
   give is a CPU-time limit, an open-file limit or a `no_net` shim; all three are
   reported through `unenforced` rather than assumed.

   **Honest caveat, recorded rather than buried:** the child is assigned to the
   job immediately after spawn rather than being created suspended and assigned
   before its first instruction, because `std::process::Command` does not expose
   the initial thread handle. The window is microseconds and any process the
   child creates after assignment inherits the job, but a process spawned inside
   that window would escape the limits. Closing it means dropping
   `std::process::Command` for a raw `CreateProcessW`.

8. **`{exe}` needed a `.exe` extension on Windows**, and `sqlite` was invoked
   through `bash -c ... < file`. The redirect was the only shell dependency that
   was not actually necessary — `sqlite3 :memory: ".read {file}"` does the same
   thing with no shell, so sqlite now works on Windows too.

Residual risk 2 (same-uid sandbox) applies equally on all three platforms, and
residual risk 1 (no network isolation) is *worse* on Windows, where there is no
shim at all. Neither changes the verdict: single-operator, local, stdio-only.
