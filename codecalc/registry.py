"""Language registry: language -> execution plan (compile + run argv).

Each entry maps a language name to file extension and optional compile/run
argv templates. Placeholders:
  {file}  absolute path to the user's source file
  {exe}   absolute path to the compiled binary (compile step output)
  {work}  absolute path to the per-run temp workdir
  {proj}  generated project dir name (dotnet / gleam wrappers)
"""

from __future__ import annotations

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
        "bash -c 'dotnet new console -o {work}/proj -n prog --force "
        "&& cp {file} {work}/proj/Program.cs "
        "&& dotnet run --project {work}/proj --no-launch-profile'",
    ),
    "gleam": _c(
        None,
        "bash -c 'gleam new {work}/proj --name prog --skip-git "
        "&& cp {file} {work}/proj/src/prog.gleam "
        "&& cd {work}/proj && gleam run'",
    ),
    "haskell": _c(
        None,
        "bash -c 'nix-shell -p ghc --run \"ghc -O2 -o {exe} {file} && {exe}\"'",
    ),
    # ── data / query DSLs ─────────────────────────────────────────────────
    "sqlite": _c(None, "bash -c 'sqlite3 :memory: < {file}'"),
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
