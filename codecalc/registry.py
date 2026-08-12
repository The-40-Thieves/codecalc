"""Language registry: language -> execution plan (compile + run argv).

Each entry maps a language name to file extension and optional compile/run
argv templates. Placeholders:
  {file}  absolute path to the user's source file
  {exe}   absolute path to the compiled binary (compile step output)
  {work}  absolute path to the per-run temp workdir
  {proj}  generated project dir name (dotnet / gleam wrappers)
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


def _c(compile_: str | None, run: str) -> dict:
    """Build a registry entry from shell strings (compile may be None)."""
    return {
        "compile": shlex.split(compile_) if compile_ else None,
        "run": shlex.split(run),
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
#: escaping rules instead of taking argv as handed to it (THE-817).
#:
#: WINDOWS HAS NO ARGV. `CreateProcess` takes one command-line STRING and each
#: child's C runtime decides how to split it back up. MSVC-style parsing treats
#: a backslash literally unless it precedes a quote. The MSYS2 runtime that
#: Git-for-Windows' `bash` is built on does not: it treats `\` as an ESCAPE. So
#:
#:     C:\Users\me\AppData\Local\Temp\codecalc-ab12\main.sh
#:
#: arrives at bash as `C:UsersmeAppDataLocalTempcodecalc-ab12main.sh` — every
#: separator eaten, exit 127, 100% reproducible on a desktop Git-for-Windows
#: install. Nothing on the Python side is wrong; `shlex.split` produces a
#: correct argv and Windows flattens it before the callee re-splits it.
#:
#: The repair is to hand these runtimes a name with NO separator left in it.
#: The child's cwd IS the workdir already (`_run_step(..., cwd=workdir)` here
#: and `current_dir` in main.rs), so the bare file name names the same file and
#: leaves nothing for either the escape pass or MSYS path translation to
#: corrupt. It also immunises the spaced-profile case (`C:\Users\John Smith\`),
#: which the same re-parse would split on — untested by CI and by the box that
#: found this one, so it is a property of the fix rather than a verified claim.
#:
#: SCOPED TO THE SHELLS BECAUSE THAT IS WHERE IT WAS MEASURED. A MinGW `gcc` is
#: the same kind of program and plausibly shares the mechanism, but nobody has
#: run it; this set exists to stop exactly that guess. Mirrored in main.rs and
#: gated by scripts/check_parity.py.
POSIX_ARGV_LANGUAGES = frozenset({"bash", "zsh"})


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
    """
    if windows and language in POSIX_ARGV_LANGUAGES:
        return ntpath.basename(path)
    return path


LANGUAGES: dict[str, dict] = {
    # ── interpreters ─────────────────────────────────────────────────────
    "python3": _c(None, "python3 {file}"),
    "node":    _c(None, "node {file}"),
    "bun":     _c(None, "bun run {file}"),
    "deno":    _c(None, "deno run {file}"),
    "typescript": _c(None, "deno run {file}"),      # deno runs TS natively
    "ruby":    _c(None, "ruby {file}"),
    "php":     _c(None, "php {file}"),
    "perl":    _c(None, "perl {file}"),
    "lua":     _c(None, "lua {file}"),
    "tcl":     _c(None, "tclsh {file}"),
    "r":       _c(None, "Rscript {file}"),
    "elixir":  _c(None, "elixir {file}"),
    "erlang":  _c(None, "escript {file}"),
    "bash":    _c(None, "bash {file}"),
    "zsh":     _c(None, "zsh {file}"),
    "mojo":    _c(None, "mojo run {file}"),
    "swift":   _c(None, "swift {file}"),
    # ── compilers (compile -> run) ───────────────────────────────────────
    "c":       _c("gcc -O2 -o {exe} {file}", "{exe}"),
    "cpp":     _c("g++ -O2 -o {exe} {file}", "{exe}"),
    "c++":     _c("g++ -O2 -o {exe} {file}", "{exe}"),
    "rust":    _c("rustc -O -o {exe} {file}", "{exe}"),
    "go":      _c(None, "go run {file}"),
    "fortran": _c("gfortran -O2 -o {exe} {file}", "{exe}"),
    "zig":     _c(None, "zig run {file}"),
    # Java 11+ single-file source launch (JEP 330) — works with JDK 26.
    "java":    _c(None, "java {file}"),
    "kotlin":  _c(
        "kotlinc {file} -include-runtime -d {work}/out.jar",
        "java -jar {work}/out.jar",
    ),
    # ── project-wrapper runtimes ──────────────────────────────────────────
    "csharp": _c(
        None,
        'bash -c \'dotnet new console -o "$2/proj" -n prog --force && cp "$1" "$2/proj/Program.cs" && dotnet run --project "$2/proj" --no-launch-profile\' codecalc {file} {work}',
    ),
    "gleam": _c(
        None,
        'bash -c \'gleam new "$2/proj" --name prog --skip-git && cp "$1" "$2/proj/src/prog.gleam" && cd "$2/proj" && gleam run\' codecalc {file} {work}',
    ),
    "haskell": _c(
        None,
        'bash -c \'f=$(printf %q "$1"); e=$(printf %q "$3"); nix-shell -p ghc --run "ghc -O2 -o $e $f && $e"\' codecalc {file} {work} {exe}',
    ),
    # ── data / query DSLs ─────────────────────────────────────────────────
    # `.read` as a SQL argument rather than a shell redirect: the only wrapper
    # language that did not need a shell, so it works on Windows too.
    "sqlite": _c(None, 'sqlite3 :memory: ".read {file}"'),
    "jq":     _c(None, "jq -n -f {file}"),
    "awk":    _c(None, "awk -f {file}"),
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
    """Human-readable catalog for the MCP list_languages tool."""
    out = []
    for name, entry in sorted(LANGUAGES.items()):
        out.append({
            "name": name,
            "extension": EXTENSIONS[name],
            "compiled": entry["compile"] is not None,
            "run": " ".join(entry["run"]).replace("{work}/", ""),
        })
    return out
