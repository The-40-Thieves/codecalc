"""Language registry: language -> execution plan (compile + run argv).

Each entry maps a language name to file extension and optional compile/run
argv templates. Placeholders:
  {file}  absolute path to the runner's OWN COPY of the source, inside the
          scratch subdirectory (RUN_SCRATCH_DIRNAME below) — never the
          workdir root itself.
  {exe}   absolute path to the compiled binary (compile step output), also
          inside the scratch subdirectory.
  {work}  absolute path to the scratch subdirectory itself (NOT the workdir
          root — see RUN_SCRATCH_DIRNAME). Kotlin's compile command and the
          gleam/haskell wrapper scripts use this to place their own build
          output alongside {exe} rather than at the workdir root.
  {proj}  generated project dir name (dotnet / gleam wrappers)

The workdir ROOT is a separate thing from all four placeholders above: it is
the RUNNING PROGRAM's cwd (both `codecalc/executor.py` and
`executor/src/main.rs` set it for the run step specifically — the compile
step's cwd is the scratch subdirectory, since nothing about compiling is the
user's own I/O), so a program's own relative file access
(`open("data.csv")`, a session's own files written via `session_write_file`)
resolves there, exactly where a caller placed them — never inside the
runner's scratch subdirectory, which the caller never sees as a workspace
path at all.
"""

from __future__ import annotations

import ntpath
import os
import shlex

#: Env var an operator sets to pin the PATH executed code resolves runtimes on.
#: Mirrored in executor/src/main.rs; scripts/check_parity.py gates that they match.
RUNTIME_PATH_ENV = "CODECALC_RUNTIME_PATH"

#: Last-resort PATH. Deliberately minimal and machine-neutral.
#:
#: This used to be a hardcoded list of one developer's home directory and mise
#: shims, in a PUBLIC repo. On any other machine it resolved almost nothing.
DEFAULT_RUNTIME_PATH = "/usr/local/bin:/usr/bin:/bin"


def runtime_path() -> str:
    """PATH handed to executed code.

    Precedence: CODECALC_RUNTIME_PATH, then this process's own PATH, then the
    minimal default. Inheriting the caller's PATH is the right default because
    the caller is the codecalc server, launched by the operator — not the
    untrusted program.

    Pin it explicitly when the server is spawned by an MCP client with a
    stripped environment: an inherited PATH can miss a toolchain manager's shims
    entirely. `list_languages` probes each runtime, so that surfaces as
    `available: false` rather than silently.
    """
    return (os.environ.get(RUNTIME_PATH_ENV)
            or os.environ.get("PATH")
            or DEFAULT_RUNTIME_PATH)


#: The three RELIABILITY tiers, orthogonal to RUNTIME_STATES below.
#:
#: RUNTIME_STATES answers "did this machine resolve the command" — a fact
#: about THIS HOST, recomputed on every probe. RELIABILITY_TIERS answers "how
#: much has codecalc's own CI actually verified this language's toolchain
#: works" — a fact about THE PROJECT, fixed per language until the evidence
#: changes. The two can and do disagree: a review's own smoke test found the
#: rust and csharp host toolchains FAILING on a machine where `rustc` and
#: `dotnet` both resolved cleanly — `installed` was true and the toolchain was
#: still broken. Resolution says nothing about that; only a real execution
#: does, and codecalc's CI only really executes a couple of these languages.
#:
#:     tested        A CI job genuinely EXECUTES this language and asserts on
#:                   its real output, on every PR. Evidence, not aspiration —
#:                   scripts/check_runtime_tiers.py derives this set from the
#:                   CI-wired source files themselves (test_python_sweep.py's
#:                   WORKER_LANGS, test_tier_evidence.py's TIER_EVIDENCE_LANGS,
#:                   contract_check.py's first CANDIDATES entry) rather than
#:                   trusting a hand-maintained list that could drift from
#:                   what CI actually runs.
#:     best_effort   Declared and plausibly works on a normal install with the
#:                   right toolchain present — codecalc ships a plan for it
#:                   and a local smoke fixture exists (tests/test_smoke.py) —
#:                   but no CI job runs it, so nothing would notice it
#:                   silently breaking.
#:     plan_only     A registry entry that has never been validated on any
#:                   runner, anywhere, not even locally. Empty today: every
#:                   declared language has at least a local smoke exercise.
#:                   The tier exists so a FUTURE language added without one is
#:                   forced to say so instead of quietly borrowing
#:                   best_effort's implied "probably fine".
#:
#: Kept deliberately conservative on `tested`. contract_check.py's CANDIDATES
#: list also names ruby/perl/php/lua/deno, but it picks the FIRST available
#: candidate and python3 is unconditionally installed on every CI leg before
#: that probe runs (actions/setup-python) — so those five are never actually
#: the one exercised. A candidate CI never reaches is not evidence. `node`
#: earns `tested` from a DIFFERENT harness: tests/test_python_sweep.py
#: dynamically probes a real node worker session on every PR and asserts real
#: stdout from it (state round-trips, a captured fd-1 escape) — not from
#: contract_check.py, where it is equally a dead candidate. `rust` and `go`
#: earn it from a third: tests/test_tier_evidence.py compiles and runs a real
#: program in each through executor.execute() and asserts its computed
#: stdout, with skips promoted to failures on the CI leg that carries the
#: evidence.
RELIABILITY_TIERS = ("tested", "best_effort", "plan_only")


def _c(compile_: str | None, run: str, tier: str) -> dict:
    """Build a registry entry from shell strings (compile may be None).

    `tier` is a required argument, not a default: RELIABILITY_TIERS above is
    the only definition of what the three values mean, and a call site must
    not be able to add a language to the registry without taking a position
    on how much codecalc has actually verified it works.
    """
    if tier not in RELIABILITY_TIERS:
        raise ValueError(f"unknown reliability tier {tier!r}; must be one of {RELIABILITY_TIERS}")
    return {
        "compile": shlex.split(compile_) if compile_ else None,
        "run": shlex.split(run),
        "tier": tier,
    }


#: The four runtime states, ordered weakest to strongest CLAIM. Defined here
#: rather than in doctor.py because three modules now read them — doctor, the
#: executor's catalog, and contract.py's schema — and registry is the one they
#: all already import. `doctor.RUNTIME_STATES` stays bound to this tuple, so it
#: is one vocabulary with three readers instead of three transcriptions.
#:
#:     supported   codecalc knows this language; nothing for it resolves here
#:     installed   its command resolves on the sandbox PATH and is executable
#:     unhealthy   it resolves and is NOT executable, or was run and failed
#:     available   it was actually RUN here and answered
RUNTIME_STATES = ("supported", "installed", "unhealthy", "available")


#: Languages whose runtime re-parses the raw Windows command line with POSIX
#: escaping rules instead of taking argv as handed to it.
#:
#: WINDOWS HAS NO ARGV. `CreateProcess` takes one command-line STRING and each
#: child's C runtime decides how to split it back up. MSVC-style parsing treats
#: a backslash literally unless it precedes a quote. The MSYS2 runtime that
#: Git-for-Windows' `bash` is built on does not: it treats `\` as an ESCAPE. So
#:
#:     C:\Users\me\AppData\Local\Temp\codecalc-ab12\main.sh
#:
#: arrives at bash as `C:UsersmeAppDataLocalTempcodecalc-ab12main.sh` — every
#: backslash eaten, exit 127, 100% reproducible on a desktop Git-for-Windows
#: install. Nothing on the Python side is wrong; `shlex.split` produces a
#: correct argv and Windows flattens it before the callee re-splits it.
#:
#: The repair is to hand these runtimes a path with NO BACKSLASH left in it —
#: not "no separator": since the runner's own copy of the source lives inside
#: RUN_SCRATCH_DIRNAME (below), one level under the run step's own cwd (the
#: workdir root — see that constant's docstring for why the two differ), a
#: bare basename is no longer enough on its own to name the same file from
#: that cwd. `source_arg` instead renders a scratch-relative path with a
#: forward slash (`RUN_SCRATCH_DIRNAME/main.<ext>`) — a `/` is not a `\`, so
#: MSYS's re-tokenization passes it through untouched, which is the whole
#: property this set exists to guarantee; MSYS bash treats `/` as a directory
#: separator regardless of host OS. It also immunises the spaced-profile case
#: (`C:\Users\John Smith\`), which the same re-parse would split on — untested
#: by CI and by the box that found this one, so it is a property of the fix
#: rather than a verified claim.
#:
#: SCOPED TO THE SHELLS BECAUSE THAT IS WHERE IT WAS MEASURED. A MinGW `gcc` is
#: the same kind of program and plausibly shares the mechanism, but nobody has
#: run it; this set exists to stop exactly that guess. Mirrored in main.rs and
#: gated by scripts/check_parity.py.
POSIX_ARGV_LANGUAGES = frozenset({"bash", "zsh"})

#: The runner's own scratch subdirectory of a workdir/session root: the
#: entry source copy (`main.<ext>`), the compiled binary (`a.out`/`a.exe`),
#: any compile output a language template names via `{work}` (Kotlin's
#: `out.jar`, gleam's scaffolded `proj/` tree), and — on the Rust backend
#: only — the compile/run redirect files all live HERE, never at the workdir
#: root.
#:
#: The workdir root itself stays the RUNNING PROGRAM's cwd (both backends set
#: this for the run step specifically), so a user's own relative file access
#: (`open("data.csv")`, a session's own `main.py` written via
#: `session_write_file`) keeps resolving exactly where it always has, and a
#: session's own files are never silently overwritten by the runner's next
#: run of some OTHER entry file — the bug this subdirectory exists to close.
#: `sessions.py`'s artifact listing excludes this one directory prefix
#: (plus the session lock and the spill directory) rather than a
#: hand-maintained set of root-level basenames, so a language added to the
#: registry can never reopen that bug for itself.
#:
#: Mirrored in `executor/src/main.rs`; `scripts/check_parity.py` gates that
#: the two agree.
RUN_SCRATCH_DIRNAME = ".codecalc-run"

#: Languages whose CANONICAL PLAN still needs a POSIX shell to prepare its
#: workspace — the `bash -c` wrappers. csharp left this set when
#: .NET 10's file-based execution made a shell unnecessary; these two remain:
#: gleam has no single-file mode (a project must be scaffolded), and haskell's
#: plan runs through `nix-shell`, which is a POSIX-only environment manager.
#:
#: The set exists so the WRAPPER requirement is a checkable fact instead of a
#: string buried in an argv template. Before it, all three wrapper languages
#: probed as `bash` — so on a Windows box with Git-for-Windows they advertised
#: `available: true` for plans that were structurally unable to run (the same
#: lie fixed for bash itself, one level up). Mirrored in
#: executor/src/main.rs; scripts/check_parity.py gates the two copies.
SHELL_WRAPPED = frozenset({"gleam", "haskell"})

#: The tool that does the real work inside each wrapper. Probing THIS is what
#: makes availability honest: `bash` being present says nothing about whether
#: gleam is, and it was the only thing being checked.
WRAPPED_TOOL = {"gleam": "gleam", "haskell": "nix-shell"}


def secondary_command(entry: dict) -> str | None:
    """A SECOND executable `entry`'s plan needs, beyond the one that decides
    whether it resolves at all — or None when there isn't one.

    Most compile-then-run plans (c/cpp/rust/fortran) `run` is literally
    `{exe}`, the binary the compile step itself just produced, so resolving
    the compile tool is already proof the whole plan can run: nothing more to
    check. Kotlin's `run` step instead launches `java -jar ...` directly — a
    toolchain `kotlinc` resolving says NOTHING about whether a JRE is present
    — and that gap is exactly what let kotlin report `installed` from `java`
    alone (`command: "java"`) on a host with no Kotlin toolchain at all: the
    resolving-decider and the thing `run` actually invokes were silently two
    different binaries.

    Computed mechanically from the registry entry rather than a hand-kept
    per-language table, so a future compile-then-run language that ALSO needs
    a distinct run-time tool is caught by the same rule instead of quietly
    inheriting this bug: any entry with a compile step whose `run`'s first
    token is a literal command (not a `{exe}`/`{file}`/`{work}` placeholder)
    different from that compile tool needs both probed.
    """
    compile_ = entry.get("compile")
    run = entry.get("run") or []
    if not compile_ or not run:
        return None
    run_cmd = run[0]
    if run_cmd.startswith("{") or run_cmd == compile_[0]:
        return None
    return run_cmd


def plan_supported(name: str, *, windows: bool) -> bool:
    """Whether `name`'s canonical plan can execute on this platform AT ALL.

    Distinct from "is the runtime installed": a shell-wrapped plan on Windows
    is unsupported no matter what is installed, and reporting it as merely
    missing invites installing things that will not help.

    `windows` is a parameter rather than a platform read, for the same reason
    `source_arg` takes one: a branch only the breaking platform can reach is a
    branch CI never checks.
    """
    canon = canonical(name)
    if canon is None:
        return False
    return not (windows and canon in SHELL_WRAPPED)


def source_arg(language: str, path: str, *, windows: bool) -> str:
    r"""What `{file}` becomes for `language` — see POSIX_ARGV_LANGUAGES.

    `windows` is a parameter rather than a read of `sys.platform` so the
    Windows rendering is reachable from a test on any host. A branch that can
    only be exercised on the platform that breaks is a branch CI never checks.

    `ntpath`, not `os.path`, for the same reason. `os.path` IS `ntpath` on
    Windows, so `os.path.basename` would have been correct in production and
    silently wrong everywhere else — on Linux it is `posixpath`, which does not
    treat `\` as a separator and hands back the whole path unchanged. The Linux
    CI leg caught that on the first run; with `os.path` it would have passed
    three green legs and shipped the bug it was written to prevent.

    The rendered path is `RUN_SCRATCH_DIRNAME/<basename>` (forward slash),
    not the bare basename alone: the run step's cwd is the workdir ROOT, one
    level above where the runner's own copy of the source actually lives
    (see RUN_SCRATCH_DIRNAME), so a bare name would no longer resolve. A `/`
    survives MSYS's backslash-escape re-tokenization untouched — see
    POSIX_ARGV_LANGUAGES's docstring for why that is the property that
    matters, not "no separator at all".
    """
    if windows and language in POSIX_ARGV_LANGUAGES:
        return f"{RUN_SCRATCH_DIRNAME}/{ntpath.basename(path)}"
    return path


#: python3, node, rust and go are the ONLY languages a CI job actually
#: executes and asserts real output from, on every PR — see RELIABILITY_TIERS
#: above for the evidence trail and scripts/check_runtime_tiers.py for the
#: gate that keeps this claim honest. Every other language below is
#: `best_effort`: declared, with a local smoke fixture (tests/test_smoke.py),
#: never exercised in CI.
LANGUAGES: dict[str, dict] = {
    # ── interpreters ─────────────────────────────────────────────────────
    "python3": _c(None, "python3 {file}", "tested"),
    "node":    _c(None, "node {file}", "tested"),
    "bun":     _c(None, "bun run {file}", "best_effort"),
    "deno":    _c(None, "deno run {file}", "best_effort"),
    "typescript": _c(None, "deno run {file}", "best_effort"),  # deno runs TS natively
    "ruby":    _c(None, "ruby {file}", "best_effort"),
    "php":     _c(None, "php {file}", "best_effort"),
    "perl":    _c(None, "perl {file}", "best_effort"),
    "lua":     _c(None, "lua {file}", "best_effort"),
    "tcl":     _c(None, "tclsh {file}", "best_effort"),
    "r":       _c(None, "Rscript {file}", "best_effort"),
    "elixir":  _c(None, "elixir {file}", "best_effort"),
    "erlang":  _c(None, "escript {file}", "best_effort"),
    "bash":    _c(None, "bash {file}", "best_effort"),
    "zsh":     _c(None, "zsh {file}", "best_effort"),
    "mojo":    _c(None, "mojo run {file}", "best_effort"),
    "swift":   _c(None, "swift {file}", "best_effort"),
    # ── compilers (compile -> run) ───────────────────────────────────────
    # rust and go are `tested`: tests/test_tier_evidence.py compiles and runs
    # a real program in each through executor.execute() and asserts its exact
    # computed stdout, on every PR, with skips promoted to failures on the CI
    # leg that carries the evidence (CODECALC_REQUIRE_TIER_EVIDENCE=1 in
    # ci-python.yml). That is the execution evidence this tier demands —
    # rust sat at best_effort DESPITE being the executor's own build
    # toolchain, because contract_check.py's COMPILED_BROKEN case only proves
    # a bad program is REJECTED correctly, and a review's smoke test once
    # found the rust HOST toolchain failing while `rustc` resolved cleanly.
    # `-I{work}/..` restores a behaviour the scratch subdirectory would
    # otherwise silently take away: `#include "helper.h"` (C/C++) and
    # `include 'helper.inc'` (Fortran) resolve QUOTED/relative includes
    # against the INCLUDING FILE's own directory first — which used to be
    # the workdir root itself (where a sibling written via
    # `session_write_file` actually lives), and is now RUN_SCRATCH_DIRNAME
    # instead. `{work}` is that scratch directory (see its docstring), so
    # `{work}/..` is the workdir root, purely lexically — no extra
    # placeholder needed, and the OS resolves the `..` component when gcc
    # opens the file, the same way it always resolves `..` in any path. `-I`
    # ADDS a search directory rather than replacing the implicit
    # same-directory check, so this is additive: a header already found next
    # to the scratch copy (there is never one) is unaffected, and one at the
    # workdir root is now found same as before this subdirectory existed.
    # Measured against the un-fixed behaviour: `#include "x.h"` for a
    # session's own `x.h` written via `session_write_file` failed with
    # "No such file or directory" before this flag was added.
    "c":       _c("gcc -O2 -I{work}/.. -o {exe} {file}", "{exe}", "best_effort"),
    "cpp":     _c("g++ -O2 -I{work}/.. -o {exe} {file}", "{exe}", "best_effort"),
    "c++":     _c("g++ -O2 -I{work}/.. -o {exe} {file}", "{exe}", "best_effort"),
    # rust's `mod helper;` has NO equivalent search-path flag — module
    # resolution is strictly relative to the declaring file's own directory,
    # with no rustc option to add a fallback search directory the way C's
    # `-I`/Fortran's `-I` do. A session's own `helper.rs` at the workdir
    # root, previously resolvable via `mod helper;` in `main.rs` (both used
    # to live in the same directory), is a DOCUMENTED, un-fixable behaviour
    # change from the scratch subdirectory: `mod helper;` now looks in
    # RUN_SCRATCH_DIRNAME, where no such file exists. A single-file rust
    # program (the only shape tests/test_tier_evidence.py's `tested` claim
    # covers) is entirely unaffected.
    "rust":    _c("rustc -O -o {exe} {file}", "{exe}", "tested"),
    "go":      _c(None, "go run {file}", "tested"),
    "fortran": _c("gfortran -O2 -I{work}/.. -o {exe} {file}", "{exe}", "best_effort"),
    "zig":     _c(None, "zig run {file}", "best_effort"),
    # Java 11+ single-file source launch (JEP 330) — works with JDK 26.
    "java":    _c(None, "java {file}", "best_effort"),
    "kotlin":  _c(
        "kotlinc {file} -include-runtime -d {work}/out.jar",
        "java -jar {work}/out.jar",
        "best_effort",
    ),
    # ── project-wrapper runtimes ──────────────────────────────────────────
    # csharp is NOT a wrapper any more. .NET 10 runs a single .cs
    # file directly ("file-based apps"), implicit usings included — verified
    # with this registry's own smoke snippet under the executor's env
    # allowlist. The old plan shelled out to `dotnet new console` + `cp`,
    # which made C# structurally unsupported on Windows for want of bash while
    # the runtime itself resolved fine.
    #
    # best_effort, not tested: the review that found rust's host toolchain
    # broken (rust has since earned `tested` via tests/test_tier_evidence.py)
    # found csharp's broken too, and no CI job executes a .cs file at all.
    "csharp": _c(None, "dotnet run {file}", "best_effort"),
    "gleam": _c(
        None,
        'bash -c \'gleam new "$2/proj" --name prog --skip-git && cp "$1" "$2/proj/src/prog.gleam" && cd "$2/proj" && gleam run\' codecalc {file} {work}',
        "best_effort",
    ),
    # Unlike rust's `mod` (declaring-file-relative), GHC's default import
    # search path is CWD-relative (`-i.`), and this plan's `run` cwd is the
    # workdir root — never RUN_SCRATCH_DIRNAME, since haskell has no separate
    # `compile` entry (this whole bash script is the one "run" step). So a
    # session's own sibling `Helper.hs` at the workdir root DOES still
    # resolve via `import Helper` — verified end to end, correcting an
    # earlier, wrong claim here that grouped haskell with rust as broken by
    # RUN_SCRATCH_DIRNAME. What actually changed nothing: GHC's own build
    # writes each module's `.hi`/`.o` NEXT TO ITS SOURCE, so `Helper.hi`/
    # `Helper.o` land at the workdir root beside `Helper.hs` — outside
    # RUN_SCRATCH_DIRNAME, a latent collision risk with a same-named user
    # file, but a PRE-EXISTING GHC characteristic this fix neither
    # introduced nor changed (main.hs's own copy already lived at the
    # workdir root before RUN_SCRATCH_DIRNAME existed, with the identical
    # cwd, so `Helper.hi`/`.o` landed exactly there then too).
    "haskell": _c(
        None,
        'bash -c \'f=$(printf %q "$1"); e=$(printf %q "$3"); nix-shell -p ghc --run "ghc -O2 -o $e $f && $e"\' codecalc {file} {work} {exe}',
        "best_effort",
    ),
    # ── data / query DSLs ─────────────────────────────────────────────────
    # `.read` as a SQL argument rather than a shell redirect: the only wrapper
    # language that did not need a shell, so it works on Windows too.
    "sqlite": _c(None, 'sqlite3 :memory: ".read {file}"', "best_effort"),
    "jq":     _c(None, "jq -n -f {file}", "best_effort"),
    "awk":    _c(None, "awk -f {file}", "best_effort"),
}

#: canonical name -> aliases
ALIASES: dict[str, list[str]] = {
    "python3": ["python", "py", "python3.14", "python3.12"],
    "node": ["js", "javascript", "nodejs"],
    "typescript": ["ts"],
    "c++": ["cpp", "cxx"],
    "r": ["rscript"],
    "bash": ["sh", "shell"],
    "csharp": ["cs", "c#", "dotnet"],
    "haskell": ["ghc", "hs"],
}

EXTENSIONS: dict[str, str] = {
    "python3": "py", "node": "js", "bun": "ts", "deno": "ts",
    "typescript": "ts", "ruby": "rb", "php": "php", "perl": "pl",
    "lua": "lua", "tcl": "tcl", "r": "R", "elixir": "exs",
    "erlang": "erl", "bash": "sh", "zsh": "zsh", "mojo": "mojo",
    "swift": "swift", "c": "c", "cpp": "cpp", "c++": "cpp", "rust": "rs", "go": "go",
    "fortran": "f90", "zig": "zig", "java": "java", "kotlin": "kt",
    "csharp": "cs", "gleam": "gleam", "haskell": "hs",
    "sqlite": "sql", "jq": "jq", "awk": "awk",
}


def canonical(name: str) -> str | None:
    """Resolve any alias/display name to a registry key."""
    n = name.strip().lower()
    if n in LANGUAGES:
        return n
    for canon, aliases in ALIASES.items():
        if n in aliases:
            return canon
    return None


def all_languages() -> list[dict]:
    """Human-readable catalog for the MCP list_languages tool.

    `tier` (RELIABILITY_TIERS) rides along with every entry: it is a static
    fact about how much codecalc's own CI has verified this language, which
    is orthogonal to the `status`/`available` fields `executor.catalog()`
    layers on top from a live probe of THIS machine. See RELIABILITY_TIERS'
    comment for why the two can and do disagree.
    """
    out = []
    for name, entry in sorted(LANGUAGES.items()):
        out.append({
            "name": name,
            "extension": EXTENSIONS[name],
            "compiled": entry["compile"] is not None,
            "run": " ".join(entry["run"]).replace("{work}/", ""),
            "tier": entry["tier"],
        })
    return out
