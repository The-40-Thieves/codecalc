"""Language registry: every runtime on Cave -> execution plan.

Each entry maps a language name to file extension and optional compile/run
argv templates. Placeholders:
  {file}  absolute path to the user's source file
  {exe}   absolute path to the compiled binary (compile step output)
  {work}  absolute path to the per-run temp workdir
  {proj}  generated project dir name (dotnet / gleam wrappers)
"""

from __future__ import annotations

import shlex

#: PATH additions so every runtime resolves (mise shims first, then user CLIs,
#: Swift toolchain, cargo, and system dirs).
RUNTIME_PATH = (
    "/data/tools/mise/shims:"
    "/home/ubuntu/.local/bin:"
    "/home/ubuntu/.npm-global/bin:"
    "/home/ubuntu/.local/share/swiftly/bin:"
    "/home/ubuntu/.cargo/bin:"
    "/nix/var/nix/profiles/default/bin:"   # nix-shell for on-demand runtimes
    "/usr/local/bin:/usr/bin:/bin"
)


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
