//! codecalc-exec — sandboxed multi-language executor.
//!
//! Reads source code on stdin, executes it in the requested language inside a
//! fresh temp dir (or a caller-supplied --workdir) with rlimits + wall-clock
//! timeout + process-group kill, prints a JSON result on stdout.
//!
//! Usage: codecalc-exec --lang <name> [--timeout <secs>] [--stdin <data>]
//!        [--stdin-file <path>] [--workdir <dir>] [--max-memory-mb <mb>]
//!        [--max-output-kb <kb>] [--max-cpu <secs>] [--no-net] < code
//!
//! This is the security-sensitive core: written in Rust so no user input is
//! ever evaluated/interpreted by the host process — it only spawns children
//! with OS-enforced resource limits.

use std::env;
use std::fs;
use std::io::{Read, Write};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use serde_json::json;

mod platform;
use platform::ResolvedLimits;

const MAX_OUTPUT_BYTES: u64 = 64 * 1024;
const AS_LIMIT_BYTES: u64 = 2048 * 1024 * 1024 * 1024; // 2 TiB VA (V8/JVM need huge VA)
const FSIZE_LIMIT_BYTES: u64 = 256 * 1024 * 1024;
const NFILE_LIMIT: u64 = 256;
// ── fork-bomb guard ─────────────────────────────────────────────────────────
//
// RLIMIT_NPROC is not a per-sandbox limit. The kernel compares it against the
// real uid's TOTAL task count, machine-wide, and it counts THREADS: every
// clone() is checked, CLONE_THREAD included. So any fixed constant is a bet on
// how busy the rest of the box is.
//
// The previous value, 1024, was chosen from a process count — "this host runs
// ~120 ubuntu processes, so 1024 leaves headroom". The kernel was counting
// 1009 tasks for the same uid at the same moment. Real headroom was ~15
// threads, not ~900, so every runtime with a thread pool died at startup:
//
//     go:      failed to create new OS thread (have 5 already; errno=11)
//     erlang:  Failed to create dirty cpu scheduler thread 2, error = 11
//     node/deno/ruby/python3: tokio "OS can't spawn worker thread" (mise shim)
//
// 14 of 31 languages, load-dependent, on the machine the project was built for.
// A `print("ok")` probe spawns no threads and does not reproduce it.
//
// The fix is to stop guessing the ambient count and measure it: the limit is
// (current tasks for this uid) + headroom, computed fresh per execution. A fork
// bomb can then add at most `headroom` tasks before EAGAIN, while a legitimate
// runtime that wants a handful of threads always has room no matter how busy
// the box is.
//
// This is a mitigation, not isolation. The budget is still shared with every
// other process owned by this uid — two concurrent executions draw on the same
// pool. cgroup v2 `pids.max` is the real answer because it is scoped to the
// cgroup rather than the uid, but it needs delegated cgroup write access that a
// stdio MCP server launched by an arbitrary client cannot assume. Reach for it
// when codecalc moves behind a container, which is also when the other residual
// risks in AUDIT.md stop being acceptable.
const DEFAULT_PROCESS_HEADROOM: u64 = 512;
/// Used when the ambient task count cannot be read (non-Linux, /proc not
/// mounted). Generous on purpose: failing OPEN here degrades the fork-bomb
/// guard, while failing closed would break every execution on that host.
const FALLBACK_NPROC_LIMIT: u64 = 4096;
const MAX_PROCESSES_ENV: &str = "CODECALC_MAX_PROCESSES";
const PROCESS_HEADROOM_ENV: &str = "CODECALC_PROCESS_HEADROOM";
const CPU_GRACE_SECONDS: u64 = 8;

/// Env allowlist: executed code must NEVER inherit secrets (API keys, tokens).
/// Only the vars a runtime needs to function. Everything else is dropped.
/// Environment variables executed code may see. Everything else is dropped —
/// the CRITICAL-02 fix against secret leakage. Kept identical to the Python
/// fallback's; scripts/check_parity.py gates that.
///
/// The Windows names are here because their absence made Windows a second-class
/// platform rather than a secured one: a process started without SystemRoot
/// fails inside winsock and crypto initialisation, and `node` returned empty
/// output with ok=false through the sandbox on Windows while probing as
/// available. These are OS plumbing, not credentials — SystemRoot and windir
/// locate the OS itself, COMSPEC and PATHEXT are how Windows resolves a command
/// at all, and USERPROFILE/APPDATA are the Windows spelling of HOME, which this
/// list has always allowed. A name absent from the environment is simply not
/// copied, so these are inert on POSIX.
const ENV_ALLOWLIST: &[&str] = &[
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "PYTHONUNBUFFERED",
    "JAVA_HOME",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "GOPATH",
    "GOMODCACHE",
    // Windows
    "SystemRoot",
    "SYSTEMROOT",
    "windir",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
];

/// Env var an operator sets to pin the PATH executed code resolves runtimes on.
/// Kept identical to the Python fallback's; scripts/check_parity.py gates that.
const RUNTIME_PATH_ENV: &str = "CODECALC_RUNTIME_PATH";

/// Last-resort PATH. Deliberately minimal and machine-neutral.
///
/// This used to be a hardcoded list of one developer's home directory and mise
/// shims, compiled into a binary shipped from a PUBLIC repo. On any other
/// machine it resolved almost nothing, which sat badly next to a README
/// promising static musl builds that "run on any Linux".
const DEFAULT_RUNTIME_PATH: &str = "/usr/local/bin:/usr/bin:/bin";

/// PATH handed to executed code.
///
/// Precedence: CODECALC_RUNTIME_PATH, then this process's own PATH, then the
/// minimal default. Inheriting the caller's PATH is the right default because
/// the caller is the codecalc server, launched by the operator — not the
/// untrusted program. Pinning it explicitly matters when the server is spawned
/// by an MCP client with a stripped environment, where an inherited PATH can
/// miss a toolchain manager's shims entirely; `list_languages` reports each
/// runtime's availability, so that shows up as unavailable rather than silently.
fn runtime_path() -> String {
    env::var(RUNTIME_PATH_ENV)
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| env::var("PATH").ok().filter(|s| !s.is_empty()))
        .unwrap_or_else(|| DEFAULT_RUNTIME_PATH.to_string())
}

/// RLIMIT_NPROC for this execution. See the constants above for why it is
/// measured rather than fixed.
fn nproc_limit() -> u64 {
    if let Some(v) = env::var(MAX_PROCESSES_ENV)
        .ok()
        .and_then(|s| s.parse::<u64>().ok())
    {
        return v; // operator override: an absolute cap, measurement skipped
    }
    let headroom = env::var(PROCESS_HEADROOM_ENV)
        .ok()
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(DEFAULT_PROCESS_HEADROOM);
    match platform::current_uid_tasks() {
        Some(n) => n.saturating_add(headroom),
        None => FALLBACK_NPROC_LIMIT,
    }
}

/// A language entry: optional compile step + run step. `{file}` `{exe}` `{work}` are placeholders.
struct Lang {
    name: &'static str,
    ext: &'static str,
    compile: Option<&'static [&'static str]>,
    run: &'static [&'static str],
}

const LANGS: &[Lang] = &[
    // interpreters
    Lang {
        name: "python3",
        ext: "py",
        compile: None,
        run: &["python3", "{file}"],
    },
    Lang {
        name: "node",
        ext: "js",
        compile: None,
        run: &["node", "{file}"],
    },
    Lang {
        name: "bun",
        ext: "ts",
        compile: None,
        run: &["bun", "run", "{file}"],
    },
    Lang {
        name: "deno",
        ext: "ts",
        compile: None,
        run: &["deno", "run", "{file}"],
    },
    Lang {
        name: "typescript",
        ext: "ts",
        compile: None,
        run: &["deno", "run", "{file}"],
    },
    Lang {
        name: "ruby",
        ext: "rb",
        compile: None,
        run: &["ruby", "{file}"],
    },
    Lang {
        name: "php",
        ext: "php",
        compile: None,
        run: &["php", "{file}"],
    },
    Lang {
        name: "perl",
        ext: "pl",
        compile: None,
        run: &["perl", "{file}"],
    },
    Lang {
        name: "lua",
        ext: "lua",
        compile: None,
        run: &["lua", "{file}"],
    },
    Lang {
        name: "tcl",
        ext: "tcl",
        compile: None,
        run: &["tclsh", "{file}"],
    },
    Lang {
        name: "r",
        ext: "R",
        compile: None,
        run: &["Rscript", "{file}"],
    },
    Lang {
        name: "elixir",
        ext: "exs",
        compile: None,
        run: &["elixir", "{file}"],
    },
    Lang {
        name: "erlang",
        ext: "erl",
        compile: None,
        run: &["escript", "{file}"],
    },
    Lang {
        name: "bash",
        ext: "sh",
        compile: None,
        run: &["bash", "{file}"],
    },
    Lang {
        name: "zsh",
        ext: "zsh",
        compile: None,
        run: &["zsh", "{file}"],
    },
    Lang {
        name: "mojo",
        ext: "mojo",
        compile: None,
        run: &["mojo", "run", "{file}"],
    },
    Lang {
        name: "swift",
        ext: "swift",
        compile: None,
        run: &["swift", "{file}"],
    },
    // compilers
    // `-I{work}/..` restores a behaviour RUN_SCRATCH_DIRNAME would otherwise
    // silently take away — see codecalc/registry.py's LANGUAGES table
    // comment for the full reasoning (mirrored here; scripts/
    // check_parity.py's test_executor_sweep.py counterpart gates that the
    // two argv lists agree).
    Lang {
        name: "c",
        ext: "c",
        compile: Some(&["gcc", "-O2", "-I{work}/..", "-o", "{exe}", "{file}"]),
        run: &["{exe}"],
    },
    Lang {
        name: "cpp",
        ext: "cpp",
        compile: Some(&["g++", "-O2", "-I{work}/..", "-o", "{exe}", "{file}"]),
        run: &["{exe}"],
    },
    Lang {
        name: "c++",
        ext: "cpp",
        compile: Some(&["g++", "-O2", "-I{work}/..", "-o", "{exe}", "{file}"]),
        run: &["{exe}"],
    },
    // rust's `mod helper;` has no search-path flag equivalent to C's `-I` —
    // see registry.py's comment on this same entry for the documented,
    // un-fixable behaviour change this implies for multi-file rust.
    Lang {
        name: "rust",
        ext: "rs",
        compile: Some(&["rustc", "-O", "-o", "{exe}", "{file}"]),
        run: &["{exe}"],
    },
    Lang {
        name: "go",
        ext: "go",
        compile: None,
        run: &["go", "run", "{file}"],
    },
    Lang {
        name: "fortran",
        ext: "f90",
        compile: Some(&["gfortran", "-O2", "-I{work}/..", "-o", "{exe}", "{file}"]),
        run: &["{exe}"],
    },
    Lang {
        name: "zig",
        ext: "zig",
        compile: None,
        run: &["zig", "run", "{file}"],
    },
    Lang {
        name: "java",
        ext: "java",
        compile: None,
        run: &["java", "{file}"],
    },
    Lang {
        name: "kotlin",
        ext: "kt",
        compile: Some(&[
            "kotlinc",
            "{file}",
            "-include-runtime",
            "-d",
            "{work}/out.jar",
        ]),
        run: &["java", "-jar", "{work}/out.jar"],
    },
    // project wrappers
    // csharp is not one any more: .NET 10 runs a single .cs file
    // directly, implicit usings included, so the bash/cp scaffold — which made
    // C# structurally unsupported on Windows — is gone. Mirrored in
    // codecalc/registry.py; check_parity gates the registries.
    Lang {
        name: "csharp",
        ext: "cs",
        compile: None,
        run: &["dotnet", "run", "{file}"],
    },
    Lang {
        name: "gleam",
        ext: "gleam",
        compile: None,
        run: &[
            "bash",
            "-c",
            "gleam new \"$2/proj\" --name prog --skip-git && cp \"$1\" \"$2/proj/src/prog.gleam\" && cd \"$2/proj\" && gleam run",
            "codecalc",
            "{file}",
            "{work}",
        ],
    },
    Lang {
        name: "haskell",
        ext: "hs",
        compile: None,
        run: &[
            "bash",
            "-c",
            "f=$(printf %q \"$1\"); e=$(printf %q \"$3\"); nix-shell -p ghc --run \"ghc -O2 -o $e $f && $e\"",
            "codecalc",
            "{file}",
            "{work}",
            "{exe}",
        ],
    },
    // data / query DSLs
    // `.read` as a SQL argument rather than a shell redirect: this is the only
    // wrapper language that did not actually need a shell, and dropping bash
    // makes sqlite work on Windows too.
    Lang {
        name: "sqlite",
        ext: "sql",
        compile: None,
        run: &["sqlite3", ":memory:", ".read {file}"],
    },
    Lang {
        name: "jq",
        ext: "jq",
        compile: None,
        run: &["jq", "-n", "-f", "{file}"],
    },
    Lang {
        name: "awk",
        ext: "awk",
        compile: None,
        run: &["awk", "-f", "{file}"],
    },
];

fn canonical(name: &str) -> Option<&'static Lang> {
    let n = name.trim().to_lowercase();
    LANGS
        .iter()
        .find(|l| l.name == n)
        .or_else(|| match n.as_str() {
            "python" | "py" | "python3.14" | "python3.12" => {
                LANGS.iter().find(|l| l.name == "python3")
            }
            "js" | "javascript" | "nodejs" => LANGS.iter().find(|l| l.name == "node"),
            "ts" => LANGS.iter().find(|l| l.name == "typescript"),
            "cxx" => LANGS.iter().find(|l| l.name == "c++"),
            "rscript" => LANGS.iter().find(|l| l.name == "r"),
            "sh" | "shell" => LANGS.iter().find(|l| l.name == "bash"),
            "cs" | "c#" | "dotnet" => LANGS.iter().find(|l| l.name == "csharp"),
            "ghc" | "hs" => LANGS.iter().find(|l| l.name == "haskell"),
            _ => None,
        })
}

/// Does `cmd` resolve on PATH? Used by --probe to report which runtimes an
/// older/minimal machine actually has.
fn on_path(cmd: &str) -> bool {
    // absolute or contains a separator: just check it exists
    if cmd.contains('/') || cmd.contains('\\') {
        return Path::new(cmd).is_file();
    }
    let path_var = env::var_os("PATH").unwrap_or_default();
    // split_paths, NOT split(':'). Windows separates with ';' and its entries
    // contain a drive-letter colon, so splitting on ':' there produced garbage
    // fragments that matched nothing — `--probe` reported ZERO available
    // runtimes on Windows, and the contract check could not execute anything.
    for dir in env::split_paths(&path_var) {
        if dir.as_os_str().is_empty() {
            continue;
        }
        for candidate in executable_names(cmd) {
            let p = dir.join(&candidate);
            if p.is_file() && is_executable(&p) {
                return true;
            }
        }
    }
    false
}

/// Names to try for `cmd` on this platform. Unix uses the bare name; Windows
/// resolves through PATHEXT, so `python3` on disk is really `python3.exe`.
fn executable_names(cmd: &str) -> Vec<String> {
    if !cfg!(windows) {
        return vec![cmd.to_string()];
    }
    let mut names = vec![cmd.to_string()];
    let pathext = env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into());
    for ext in pathext.split(';').filter(|e| !e.is_empty()) {
        names.push(format!("{cmd}{}", ext.to_lowercase()));
    }
    names
}

/// Is this file executable? Windows has no execute bit — presence on PATH with
/// an executable extension is the closest equivalent, and PATHEXT is how the
/// shell itself decides, so `python3` there is really `python3.exe`.
#[cfg(unix)]
fn is_executable(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(p)
        .map(|m| m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(windows)]
fn is_executable(p: &Path) -> bool {
    p.is_file()
}

/// First non-placeholder command in a run/compile template (the runtime binary).
fn first_cmd(template: &[&'static str]) -> &'static str {
    template
        .iter()
        .find(|a| !a.starts_with('{'))
        .copied()
        .unwrap_or("")
}

/// A SECOND executable `lang`'s plan needs, beyond the compile tool that
/// decides whether it resolves at all — or None when there isn't one.
/// Mirrors `codecalc/registry.py`'s `secondary_command` (see its docstring
/// for the bug this closes: kotlin's `run` step launches `java -jar ...`
/// directly, a binary `kotlinc` resolving says nothing about, which is what
/// let `--probe` — and `doctor`, before its own matching fix — report kotlin
/// available from a JRE alone with no Kotlin toolchain on the host).
///
/// Computed mechanically from `compile`/`run`, the same as the Python side,
/// rather than a hardcoded per-language table: any FUTURE compile-then-run
/// language whose `run` step names a literal command different from its
/// compile tool is caught by the same rule instead of quietly repeating this
/// bug.
fn secondary_cmd(lang: &Lang) -> Option<&'static str> {
    let compile = lang.compile?;
    let run_cmd = *lang.run.first()?;
    let compile_cmd = *compile.first()?;
    if run_cmd.starts_with('{') || run_cmd == compile_cmd {
        return None;
    }
    Some(run_cmd)
}

/// Probe every language's runtime against PATH; JSON: {"language": bool, ...}
fn probe() -> serde_json::Value {
    let mut out = serde_json::Map::new();
    for lang in LANGS {
        // A shell-wrapped plan is available only when the REAL tool and the
        // shell both resolve, and never on a platform whose plan cannot run
        //. Probing bash alone claimed gleam was available on
        // machines that had never seen gleam.
        let available = if let Some(tool) = wrapped_tool(lang.name) {
            plan_supported(lang.name, cfg!(windows)) && on_path(tool) && on_path("bash")
        } else {
            // Compiled languages have all-placeholder run templates ({exe});
            // their real runtime is the compile command's first binary.
            let cmd = match first_cmd(lang.run) {
                "" => first_cmd(lang.compile.unwrap_or(&[])),
                c => c,
            };
            let primary_available = if cmd.is_empty() || cmd == "bash" || cmd == "sh" {
                on_path(if cmd.is_empty() { "bash" } else { cmd })
            } else {
                on_path(cmd)
            };
            // Available only when a SECOND tool the run step needs (kotlin's
            // `java`) resolves too — see `secondary_cmd`'s doc comment.
            primary_available && secondary_cmd(lang).is_none_or(on_path)
        };
        out.insert(lang.name.to_string(), json!(available));
    }
    serde_json::Value::Object(out)
}

/// Identity (device, inode) of a directory, on platforms that have one.
///
/// The cleanup guard used to key on how the pathname ORIGINATED — "we created
/// it, so we may delete it". That is a claim about the past, and the executed
/// program can invalidate it: it runs with the workdir as its cwd, so
///
///     os.rename(work, work + ".held")          # move ours aside
///     os.rename("/tmp/something-i-want-gone", work)   # put a victim there
///
/// leaves `remove_dir_all(&work)` deleting a directory the executor never made.
/// Recording the identity at creation and re-checking it at deletion keys on
/// what the path IS at the moment of the delete instead.
#[cfg(unix)]
fn dir_identity(path: &Path) -> Option<(u64, u64)> {
    use std::os::unix::fs::MetadataExt;
    // symlink_metadata, not metadata: a symlink swapped in must not be followed
    // to the directory it points at.
    let md = fs::symlink_metadata(path).ok()?;
    if !md.is_dir() {
        return None;
    }
    Some((md.dev(), md.ino()))
}

#[cfg(not(unix))]
fn dir_identity(path: &Path) -> Option<(u64, u64)> {
    // Windows has no cheap stable (dev, ino) without opening a handle. The
    // rename swap needs the sandboxed program to hold the directory open, which
    // Windows makes much harder; recorded as a gap rather than faked.
    let _ = path;
    None
}

/// Delete a workdir the executor created, refusing if it is no longer the same
/// directory that was created.
fn remove_own_workdir(work: &Path, created: Option<(u64, u64)>) {
    if created.is_some() && dir_identity(work) != created {
        eprintln!(
            "codecalc-exec: refusing to delete {} — it is not the directory this run created",
            work.display()
        );
        return;
    }
    let _ = fs::remove_dir_all(work);
}

/// The same identity-checked deletion as `remove_own_workdir`, for
/// `execute()`'s scratch-directory RESET rather than final workdir cleanup
/// — `run_dir` was not created by THIS call (it may be reused from an
/// earlier one in the same session, after passing `find_tainted_entry`'s
/// scan), so the same "is this still the directory I just looked at"
/// re-check applies before removing it. Returns whether the directory was
/// actually removed, so the caller can refuse the run rather than silently
/// proceeding on a directory it could not safely clear.
fn remove_dir_checked(dir: &Path, created: Option<(u64, u64)>) -> bool {
    if created.is_some() && dir_identity(dir) != created {
        eprintln!(
            "codecalc-exec: refusing to delete {} — it is not the directory just scanned",
            dir.display()
        );
        return false;
    }
    fs::remove_dir_all(dir).is_ok()
}

/// The first entry under `dir` (any depth) that is not a plain file or a
/// plain directory, or `None` if the whole tree under `dir` is clean.
///
/// Used to refuse reusing a session's `.codecalc-run/` scratch directory
/// across calls when its contents look tampered with — see `execute()`'s
/// own comment on the CRITICAL symlink-follow this closes. `DirEntry::
/// file_type()` reports the entry itself (like `lstat`, never `stat`): it
/// does NOT follow a symlink to ask what it points to, which is the
/// property this whole check depends on — a symlink is identified as a
/// symlink, not silently resolved into "a directory" or "a file" first.
fn find_tainted_entry(dir: &Path) -> std::io::Result<Option<std::path::PathBuf>> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let ft = entry.file_type()?;
        if ft.is_symlink() {
            return Ok(Some(entry.path()));
        } else if ft.is_dir() {
            if let Some(bad) = find_tainted_entry(&entry.path())? {
                return Ok(Some(bad));
            }
        } else if !ft.is_file() {
            // A FIFO, device or socket — planting one of these instead of a
            // symlink is a different mechanism toward the same end (a
            // program in this session can open() it and block or misbehave
            // the reader, or an unusual device node can do worse), so it is
            // refused exactly like a symlink rather than treated as "not a
            // symlink, therefore fine".
            return Ok(Some(entry.path()));
        }
    }
    Ok(None)
}

fn substitute(template: &str, file: &str, exe: &str, work: &str) -> String {
    template
        .replace("{file}", file)
        .replace("{exe}", exe)
        .replace("{work}", work)
}

/// Languages whose runtime re-parses the raw Windows command line with POSIX
/// escaping rules instead of taking argv as handed to it.
///
/// WINDOWS HAS NO ARGV. `CreateProcess` takes one command-line STRING and the
/// child's C runtime splits it back up. MSVC-style parsing keeps a backslash
/// literal unless it precedes a quote; the MSYS2 runtime that
/// Git-for-Windows' `bash` is built on treats `\` as an ESCAPE, so
/// `C:\Users\me\...\main.sh` reaches bash as `C:Usersmemain.sh` — every
/// backslash eaten, exit 127, reproducible on a desktop install. This is the
/// path that box was running, so this copy is the one that closes the bug.
///
/// The repair hands these runtimes a path with NO BACKSLASH left in it — not
/// "no separator": the runner's own copy of the source lives inside
/// RUN_SCRATCH_DIRNAME, one level under the run step's own cwd (the workdir
/// root), so a bare basename is no longer enough on its own to name the same
/// file from that cwd. `source_arg` renders a scratch-relative path with a
/// forward slash instead (`RUN_SCRATCH_DIRNAME/main.<ext>`) — a `/` is not a
/// `\`, so MSYS's re-tokenization passes it through untouched, which is the
/// property this set actually needs; MSYS bash treats `/` as a directory
/// separator regardless of host OS.
///
/// Mirrored in codecalc/registry.py; scripts/check_parity.py gates that the
/// two lists stay identical.
const POSIX_ARGV_LANGUAGES: &[&str] = &["bash", "zsh"];

/// Languages whose canonical plan still needs a POSIX shell to scaffold a
/// workspace. Before this set existed all wrapper languages probed
/// as `bash`, so a Windows box with Git-for-Windows advertised them as
/// available for plans that were structurally unable to run — the same lie,
/// one level up. Mirrored in codecalc/registry.py; scripts/check_parity.py
/// gates the two copies.
const SHELL_WRAPPED: &[&str] = &["gleam", "haskell"];

/// The runner's own scratch subdirectory of a workdir/session root: the
/// entry source copy (`main.<ext>`), the compiled binary (`a.out`/`a.exe`),
/// any compile output a language template names via `{work}` (Kotlin's
/// `out.jar`, gleam's scaffolded `proj/` tree), and this backend's own
/// compile/run redirect files (`{tag}.out`/`.err`/`.in`) all live HERE,
/// never at the workdir root.
///
/// The workdir root itself stays the RUNNING PROGRAM's cwd (`execute()` sets
/// this for the run step specifically — the compile step's cwd is this
/// scratch directory, since compiling is not the user's own program running),
/// so a user's own relative file access resolves exactly where a caller can
/// see it, and a session's own files can no longer collide with the
/// runner's scratch copies at all — the two no longer share a directory.
///
/// Mirrored in codecalc/registry.py; scripts/check_parity.py gates that the
/// two agree.
const RUN_SCRATCH_DIRNAME: &str = ".codecalc-run";

/// HIGH, reported by adversarial review: node's CommonJS `require("./sibling")`
/// resolves relative to the REQUIRING FILE's own directory — the same class
/// of gap `PYTHONPATH` above closes for python3, but `NODE_PATH` does NOT
/// reach it (it only affects BARE module specifiers, never a relative
/// `./`/`../` one). A sibling `helper.js` written via `session_write_file`
/// at the workdir root, importable before the entry file's runner-owned copy
/// moved into `RUN_SCRATCH_DIRNAME`, now fails with `Cannot find module
/// './helper'` — measured end to end on both backends before this existed.
///
/// Written fresh into the scratch directory alongside the entry source on
/// EVERY node run (unconditionally, on both backends, mirroring how
/// `PYTHONPATH` is set unconditionally regardless of language) and loaded
/// via `NODE_OPTIONS=--require=<absolute path>`: it patches `Module.
/// _resolveFilename` to retry a failed RELATIVE lookup against
/// `CODECALC_WORKDIR_ROOT` (the workdir root) before giving up, restoring
/// the exact resolution a sibling module had before the entry copy's
/// directory changed. Absolute paths and bare package specifiers are
/// untouched — this only widens the relative-path fallback.
///
/// Mirrored in codecalc/executor.py's `NODE_SIBLING_SHIM_JS` as a
/// byte-identical string constant rather than a shared asset file crossing
/// the Python/Rust boundary for no benefit that gives.
const NODE_SIBLING_SHIM_JS: &str = "\
// Auto-generated by codecalc on every node run — see main.rs's
// NODE_SIBLING_SHIM_JS / executor.py's NODE_SIBLING_SHIM_JS for why this exists.
const Module = require(\"module\");
const path = require(\"path\");
const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (request, parent, isMain, options) {
  try {
    return originalResolve.call(this, request, parent, isMain, options);
  } catch (err) {
    const root = process.env.CODECALC_WORKDIR_ROOT;
    if (root && (request.startsWith(\"./\") || request.startsWith(\"../\"))) {
      try {
        const alt = path.resolve(root, request);
        return originalResolve.call(this, alt, parent, isMain, options);
      } catch (_fallbackErr) {
        // fall through to the original error below — the fallback failed too
      }
    }
    throw err;
  }
};
";

/// Filename the shim above is written under, inside the scratch directory —
/// never a name a compiled/interpreted entry itself could collide with.
const NODE_SIBLING_SHIM_NAME: &str = "_codecalc_node_sibling_shim.js";

/// The tool that does the real work inside each wrapper. Probing THIS is what
/// makes availability honest: bash being present says nothing about gleam.
fn wrapped_tool(lang: &str) -> Option<&'static str> {
    match lang {
        "gleam" => Some("gleam"),
        "haskell" => Some("nix-shell"),
        _ => None,
    }
}

/// Whether `lang`'s canonical plan can execute on this platform AT ALL —
/// distinct from whether its runtime is installed. `windows` is a parameter
/// rather than a `cfg!` read for the same reason `source_arg` takes one: a
/// branch only the breaking platform can reach is a branch CI never checks.
fn plan_supported(lang: &str, windows: bool) -> bool {
    !(windows && SHELL_WRAPPED.contains(&lang))
}

/// What `{file}` becomes for `language`.
///
/// The run step's cwd is the WORKDIR ROOT (`run_step` sets `current_dir` to
/// it for the run tag — see `execute()`), one level ABOVE
/// `RUN_SCRATCH_DIRNAME`, where the runner's own copy of the source actually
/// lives. A bare basename is therefore not enough on its own to name the
/// same file from that cwd any more; the rendered path is
/// `RUN_SCRATCH_DIRNAME/<basename>` (forward slash) instead. A `/` still
/// leaves nothing for MSYS's backslash-escape re-tokenization to corrupt —
/// see POSIX_ARGV_LANGUAGES's doc comment for why "no backslash", not "no
/// separator at all", is the property that actually matters here.
/// `{exe}` is deliberately NOT given this treatment: it is spawned rather
/// than read, and a bare name would be resolved against PATH instead of the
/// scratch directory.
///
/// `windows` is a PARAMETER rather than a `cfg!(windows)` read inside the body
/// for the same reason the Python twin takes one: `cfg!` is a compile-time
/// constant, so on a Linux build the interesting branch is dead code that no
/// Linux CI leg can reach. Passing it in makes the Windows rendering testable
/// on every host, which is where the tests below run.
fn source_arg(language: &str, file: &str, windows: bool) -> String {
    if !windows || !POSIX_ARGV_LANGUAGES.contains(&language) {
        return file.to_string();
    }
    // Split on BOTH separators: Windows accepts either, and a path that mixed
    // them would keep whichever half this missed.
    let base = match file.rsplit(['\\', '/']).next() {
        Some(base) if !base.is_empty() => base,
        _ => file,
    };
    format!("{RUN_SCRATCH_DIRNAME}/{base}")
}

/// Per-call resource limits (defaults applied by the caller).
#[derive(Clone, Copy)]
struct Limits {
    timeout: u64,       // wall-clock seconds
    max_cpu: u64,       // RLIMIT_CPU seconds (0 = timeout + grace)
    max_memory_mb: u64, // RLIMIT_AS, 0 = 2 TiB default
    max_output_kb: u64, // stdout/stderr cap + FSIZE, 0 = 64 KiB
    no_net: bool,       // block network egress: seccomp filter, or LD_PRELOAD shim fallback
                        // Precomputed in main() BEFORE any fork. apply_limits runs inside pre_exec,
                        // which must be async-signal-safe — it cannot walk /proc or allocate there.
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            timeout: 10,
            max_cpu: 0,
            max_memory_mb: 0,
            max_output_kb: 0,
            no_net: false,
        }
    }
}

/// Result of one exec step, including resource usage (wait4 rusage).
struct StepResult {
    exit_code: i64,
    signal: Option<i32>,
    stdout: String,
    stderr: String,
    timed_out: bool,
    cpu_ms: u64,
    peak_memory_kb: u64,
    output_truncated: bool,
    /// Why an output stream could not be read, if one could not. `None` is the
    /// normal case and means the streams below are what the program actually
    /// produced. `Some` means at least one of them is NOT, which a caller has
    /// no other way to learn — an unreadable file and a silent program look
    /// identical in `stdout`.
    output_error: Option<String>,
    /// Guarantees this platform could not apply. Carried into the JSON so a
    /// caller can distinguish "the limit held" from "there was no limit".
    unenforced: Vec<&'static str>,
    /// How many bytes each stream actually produced, BEFORE the response cap.
    ///
    /// `output_truncated` says that output was cut; these say by how much, which
    /// is the difference between "your program printed more than 8 KiB" and
    /// "your program printed 4 MB". A caller deciding whether to re-run with a
    /// higher `max_output_kb` cannot make that decision from a boolean.
    ///
    /// Read from the file's metadata rather than from the buffer: `read_capped`
    /// deliberately stops at `cap + 1` bytes so a program that fills stdout
    /// cannot make this process allocate proportionally to its output. The
    /// buffer length is therefore the CAPPED size and can never answer this
    /// question; one `stat` can, at no allocation cost.
    ///
    /// `None`, not 0, when the file could not be stat'd. Zero is a real and
    /// common measurement here — a program that printed nothing — so using it
    /// for "unknown" would be the same defect this struct's `output_error`
    /// field exists to prevent.
    stdout_bytes: Option<u64>,
    stderr_bytes: Option<u64>,
}

/// The two extra-environment inputs `run_step` layers on top of the
/// allowlisted, PATH-pinned base environment every step already gets —
/// bundled into one struct rather than two more bare parameters (see
/// `run_step`'s own doc comment for what each one is for).
struct StepEnv<'a> {
    pythonpath: &'a Path,
    extra: &'a [(&'a str, String)],
}

/// Run one argv step, redirecting stdout/stderr to FILES in `io_dir` rather
/// than pipes — a pipe fills at 64 KiB and deadlocks any program that writes
/// more before we read. Limits, the timeout kill and resource accounting are
/// all delegated to platform::spawn_and_wait; see platform/mod.rs for what
/// each OS can actually enforce.
///
/// `io_dir` and `cwd` are DELIBERATELY separate parameters, not one directory
/// serving both roles: `io_dir` is always `RUN_SCRATCH_DIRNAME` — the
/// `{tag}.out`/`.err`/`.in` redirect files are the runner's OWN scratch, same
/// as the source copy and the compiled binary, and never belong at the
/// workdir root. `cwd` is the scratch directory too for the "compile" tag
/// (compiling is not the user's own program running) but the WORKDIR ROOT
/// for the "run" tag — that call is the user's actual program, and its own
/// relative file access must resolve where a caller can see it. See
/// `RUN_SCRATCH_DIRNAME`'s doc comment.
///
/// `pythonpath` is always the workdir root, regardless of `cwd` — measured
/// against the pre-scratch-subdirectory behaviour before fixing it: a
/// python3 entry file used to live directly at the workdir root, so
/// `sys.path[0]` (the interpreter's OWN directory-of-the-script, unrelated
/// to `cwd`) put a sibling module written via `session_write_file`
/// (`helper.py` next to `main.py`, `from helper import ...`) on the import
/// path for free. Moving the runner's copy of the source into
/// `RUN_SCRATCH_DIRNAME` makes `sys.path[0]` that scratch directory
/// instead, which holds nothing a caller ever wrote — silently breaking
/// every such import. Setting `PYTHONPATH` to the workdir root restores the
/// same resolution (Python appends `PYTHONPATH` entries to `sys.path`
/// after `sys.path[0]`) without moving the runner's own copy back to a
/// caller-visible path. Set unconditionally, on every step and every
/// language, rather than threading a python3-specific branch through this
/// generic argv runner: no other language's runtime consults the variable,
/// so it is inert everywhere it is not python3.
///
/// `env.extra` is the same seam for a language-specific need that is not
/// `pythonpath` — currently `NODE_OPTIONS`/`CODECALC_WORKDIR_ROOT` (see
/// `NODE_SIBLING_SHIM_JS`) — kept generic here rather than adding a new
/// named parameter for every future one. Both live on one `StepEnv` rather
/// than as two more bare arguments to this function, which clippy's
/// `too_many_arguments` already flagged once at 8.
fn run_step(
    argv: &[String],
    io_dir: &Path,
    cwd: &Path,
    tag: &str,
    stdin_data: &[u8],
    limits: &Limits,
    env: &StepEnv,
) -> StepResult {
    let pythonpath = env.pythonpath;
    let extra_env = env.extra;
    let out_path = io_dir.join(format!("{tag}.out"));
    let err_path = io_dir.join(format!("{tag}.err"));
    let in_path = io_dir.join(format!("{tag}.in"));
    // `create_new(true)`, not `fs::write`: these three names are written
    // into `RUN_SCRATCH_DIRNAME`, freshly wiped and recreated by `execute()`
    // immediately before this runs (see that reset's own doc comment for
    // the reproduced symlink-follow this closes) — `O_EXCL`/`CREATE_NEW`
    // refuses ANY pre-existing entry at the final path component
    // atomically, same guarantee as the source-file write above.
    let _ = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&in_path)
        .and_then(|mut f| f.write_all(stdin_data));

    // Previously `.expect(...)` — a panic here produced NO JSON on stdout, so the
    // Python caller saw only "executor produced invalid output" with the real
    // cause (an unwritable workdir) nowhere to be found.
    let (out_f, err_f, in_f) = match (
        fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&out_path),
        fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&err_path),
        fs::File::open(&in_path),
    ) {
        (Ok(o), Ok(e), Ok(i)) => (o, e, i),
        _ => {
            return StepResult {
                exit_code: -2,
                signal: None,
                stdout: String::new(),
                stderr: format!("cannot create I/O files in {}", io_dir.display()),
                timed_out: false,
                cpu_ms: 0,
                peak_memory_kb: 0,
                // None, not an error: nothing was ever written, so there is no
                // output we failed to READ. The cause is already in stderr and
                // exit_code -2 makes this ok=false regardless.
                output_truncated: false,
                output_error: None,
                unenforced: Vec::new(),
                // None, not Some(0). No program ran, so there is no program
                // output to have counted — and the stderr above is OUR
                // sentence, not the child's. Reporting 0 would be a
                // measurement of something that never happened.
                stdout_bytes: None,
                stderr_bytes: None,
            };
        }
    };

    let mut cmd = Command::new(&argv[0]);
    cmd.args(&argv[1..])
        .current_dir(cwd)
        // SECURITY: clear env, then re-add ONLY the allowlist. User code must
        // never see API keys / tokens from the host environment.
        .env_clear();
    for key in ENV_ALLOWLIST {
        if let Ok(val) = std::env::var(key) {
            cmd.env(key, val);
        }
    }
    // Captured BEFORE `Stdio::from` consumes the Files. The Windows
    // creation-time job path needs a raw CreateProcessW, which needs
    // these; `Stdio` does not give them back. The handles stay valid for the
    // lifetime of the Stdio values, which outlive the spawn.
    #[cfg(windows)]
    let raw_stdio = {
        use std::os::windows::io::AsRawHandle;
        platform::RawStdio {
            stdin: in_f.as_raw_handle() as isize,
            stdout: out_f.as_raw_handle() as isize,
            stderr: err_f.as_raw_handle() as isize,
        }
    };
    #[cfg(not(windows))]
    let raw_stdio = platform::RawStdio::default();

    cmd.env("PATH", runtime_path()) // always the sandbox PATH, not the host's
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONPATH", pythonpath); // see this fn's doc comment
    for (key, val) in extra_env {
        cmd.env(key, val);
    }
    cmd.stdin(Stdio::from(in_f))
        .stdout(Stdio::from(out_f))
        .stderr(Stdio::from(err_f));

    let mut no_net_applied = true;
    if limits.no_net {
        let exe_dir = env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|d| d.to_path_buf()))
            .unwrap_or_else(|| Path::new(".").to_path_buf());
        no_net_applied = platform::apply_no_net(&mut cmd, &exe_dir);
    }

    let resolved = resolve_limits(limits);
    let waited = match platform::spawn_and_wait(cmd, &resolved, raw_stdio) {
        Ok(w) => w,
        Err(e) => {
            return StepResult {
                exit_code: -2,
                signal: None,
                stdout: String::new(),
                // Names the phase AND the binary this call tried to launch —
                // a bare "spawn failed: No such file or directory (os error
                // 2)" is exactly what a kotlin compile step with no
                // `kotlinc` on PATH reported: true (nothing ran, no output
                // to lose), but silent about WHICH of the plan's tools was
                // missing, which is the one fact a caller needs to act on.
                // Mirrors the Python fallback's `_runtime_unavailable_result`
                // phrasing (`executor.py`), which already names both.
                stderr: format!(
                    "runtime unavailable for the {tag} phase: {:?} not found ({e})",
                    argv.first().map_or("", String::as_str)
                ),
                timed_out: false,
                cpu_ms: 0,
                peak_memory_kb: 0,
                output_truncated: false,
                // As above: the spawn failure is the story, not a read failure.
                output_error: None,
                unenforced: Vec::new(),
                // As above: nothing ran, so nothing produced output.
                stdout_bytes: None,
                stderr_bytes: None,
            };
        }
    };

    let mut unenforced = waited.unenforced;
    if limits.no_net {
        if waited.no_net_seccomp_enforced {
            // Enforced in-kernel by a seccomp-bpf filter: a raw
            // syscall cannot bypass it, so nothing is unenforced and no
            // best-effort disclosure is warranted.
        } else if no_net_applied {
            // Only the LD_PRELOAD/dyld symbol shim held. It is best-effort — a
            // dynamically-linked ctypes/dlsym or raw-syscall network call
            // bypasses it (E-1). The Python layer expands this marker into the
            // full disclosure sentence.
            unenforced.push("no_net_best_effort_shim");
        } else {
            // No shim and no seccomp. Saying nothing would let `no_net: true`
            // read as "network blocked" when nothing was.
            unenforced.push("no_net_requested_but_no_shim_available");
        }
    }

    let (stdout, out_trunc, out_err, out_bytes) = read_capped(&out_path, limits.max_output_kb);
    let (stderr, err_trunc, err_err, err_bytes) = read_capped(&err_path, limits.max_output_kb);
    // Both are reported, and stdout's is named separately from stderr's: a
    // caller acting on the answer needs to know which stream it cannot trust.
    let output_error = match (out_err, err_err) {
        (Some(a), Some(b)) => Some(format!("stdout: {a}; stderr: {b}")),
        (Some(a), None) => Some(format!("stdout: {a}")),
        (None, Some(b)) => Some(format!("stderr: {b}")),
        (None, None) => None,
    };
    let stderr = if waited.timed_out && stderr.is_empty() {
        "<killed: exceeded wall-clock timeout>".to_string()
    } else {
        stderr
    };

    StepResult {
        exit_code: waited.exit_code,
        signal: waited.signal,
        stdout,
        stderr,
        timed_out: waited.timed_out,
        cpu_ms: waited.cpu_ms,
        peak_memory_kb: waited.peak_memory_kb,
        output_truncated: out_trunc || err_trunc,
        output_error,
        unenforced,
        stdout_bytes: out_bytes,
        stderr_bytes: err_bytes,
    }
}

/// Turn the per-call knobs into concrete ceilings for the platform layer.
fn resolve_limits(limits: &Limits) -> ResolvedLimits {
    ResolvedLimits {
        timeout_secs: limits.timeout,
        cpu_secs: if limits.max_cpu > 0 {
            limits.max_cpu
        } else {
            limits.timeout + CPU_GRACE_SECONDS
        },
        memory_bytes: if limits.max_memory_mb > 0 {
            limits.max_memory_mb * 1024 * 1024
        } else {
            AS_LIMIT_BYTES
        },
        // FSIZE must stay STRICTLY ABOVE the output cap. Setting it equal to
        // the cap made overflow undetectable: the child hit EFBIG/SIGXFSZ at
        // exactly the cap, so `read_capped` never saw a file larger than the
        // cap, `output_truncated` was never true, and OLE could never fire.
        // Measured: `print("x"*200000)` gives verdict=OLE at the default cap and
        // verdict=OK with a 4 MB output silently cut to 8 KiB once the caller
        // passed --max-output-kb 8. Passing the flag that lowers the cap
        // disabled the detection of exceeding it.
        //
        // Headroom, not the 256 MiB default: FSIZE is still a disk guard, and a
        // caller asking for a small cap should not thereby be allowed to fill
        // the workdir.
        //
        // The floor used to be 1 MiB (GH #206): for `--max-output-kb 1` that
        // meant the sandbox actually let the child write up to 1024x the
        // requested cap before SIGXFSZ ever intervened — silently, nothing in
        // `unenforced` said so. Measured before this fix: a 5 MB program
        // capped at 1 KiB still reported `stdout_bytes: 1048576`. The
        // returned `stdout` text was always correctly capped (`read_capped`
        // truncates it at the literal `max_output_kb * 1024`, no floor there)
        // — this floor governs only how much the child is ALLOWED TO WRITE
        // before being stopped, i.e. the resource ceiling `max_output_kb` is
        // documented as, not merely what gets echoed back.
        //
        // 4 KiB is enough slack on its own for any max_output_kb >= 1 (4 *
        // 1024 = 4096), so the floor below no longer binds for a real
        // request — it exists only to keep a degenerate value (0 handled
        // separately above) from producing a ceiling smaller than one page.
        fsize_bytes: if limits.max_output_kb > 0 {
            (limits.max_output_kb * 1024 * 4).clamp(4096, FSIZE_LIMIT_BYTES)
        } else {
            FSIZE_LIMIT_BYTES
        },
        nofile: NFILE_LIMIT,
        // Measured on first use, then cached: resolve_limits runs once per STEP,
        // so a compiled language asked the same question for compile and again
        // for run.
        max_processes: nproc_limit(),
        no_net: limits.no_net,
    }
}

/// Read one output file, capped, and SAY SO if it could not be read.
///
/// The third return value is the whole point. This used to be
///
///     if let Ok(mut f) = fs::File::open(path) { let _ = f.read_to_end(&mut buf); }
///
/// which discarded both failures, so "we could not read the output" and "the
/// program printed nothing" produced byte-identical results — an unreadable
/// file was reported as a successful run with an empty answer. Measured on the
/// old code, four cases, three indistinguishable:
///
///     printed 42        -> stdout="42\n"  truncated=false
///     printed nothing   -> stdout=""      truncated=false
///     file MISSING      -> stdout=""      truncated=false
///     file UNREADABLE   -> stdout=""      truncated=false
///
/// Both failures are kept, not just the open: a read that fails part-way
/// returns whatever arrived first, which is WORSE than empty because it looks
/// like complete output. `raw_os_error` is carried because the OS code is the
/// thing that would identify the cause — on Windows, error 5 (access denied)
/// and 32 (sharing violation) are documented, intermittent CI failures
/// (rust-lang/rust#127883 measured ~15% of MSVC builds), and no one can tell
/// which is happening here without the number.
fn read_capped(path: &Path, max_output_kb: u64) -> (String, bool, Option<String>, Option<u64>) {
    let cap = if max_output_kb > 0 {
        max_output_kb * 1024
    } else {
        MAX_OUTPUT_BYTES
    };
    // The true size, taken BEFORE the capped read and independently of it. This
    // is the only place the original length is still knowable: the read below
    // stops at cap+1 on purpose, so `buf.len()` answers a different question.
    //
    // What this counts is bytes the child managed to WRITE. A program stopped by
    // the FSIZE ceiling (SIGXFSZ) wrote less than it intended, and no number
    // available here could say how much more it wanted — that limit is enforced
    // by the kernel against the file, not by us against the program.
    let original = fs::metadata(path).ok().map(|m| m.len());
    let mut buf = Vec::new();
    let mut error = None;
    match fs::File::open(path) {
        Ok(mut f) => {
            // `take(cap + 1)`, not a bare read_to_end. FSIZE_LIMIT_BYTES is
            // 256 MiB, so a program that fills stdout AND stderr made this
            // process allocate half a gigabyte before the truncate below threw
            // most of it away — per execution, concurrently. The cap is now
            // applied at the source.
            //
            // +1 so `buf.len() > cap` below still detects overflow: reading
            // exactly `cap` cannot distinguish "filled it" from "exceeded it".
            if let Err(e) = (&mut f).take(cap + 1).read_to_end(&mut buf) {
                // `{e}` already renders as "<message> (os error N)", so the
                // code is carried without appending it again — the first draft
                // printed "Permission denied (os error 13) (os error Some(13))".
                error = Some(format!(
                    "read {} failed after {} bytes: {e}",
                    path.display(),
                    buf.len()
                ));
            }
        }
        Err(e) => {
            error = Some(format!("open {} failed: {e}", path.display()));
        }
    }
    let truncated = buf.len() as u64 > cap;
    if truncated {
        buf.truncate(cap as usize);
        buf.extend_from_slice(b"\n...[truncated]");
    }
    (
        String::from_utf8_lossy(&buf).into_owned(),
        truncated,
        error,
        original,
    )
}

/// Classify a run into a verdict. Heuristics:
/// TLE = wall-clock kill · OLE = output over cap · MLE = signal + RSS near
/// memory cap · RTE = nonzero exit / signal · OK otherwise.
fn verdict(sr: &StepResult, limits: &Limits) -> &'static str {
    if sr.timed_out {
        return "TLE";
    }
    if sr.output_truncated {
        return "OLE";
    }
    if sr.signal.is_some() {
        if limits.max_memory_mb > 0 && sr.peak_memory_kb >= limits.max_memory_mb * 1024 / 2 {
            return "MLE";
        }
        return "RTE";
    }
    if sr.exit_code != 0 {
        return "RTE";
    }
    "OK"
}

/// How many whole seconds remain in the wall-clock budget for the run step,
/// given how long compiling already took. `elapsed_ms` is `started.elapsed()`
/// captured right after the compile step returns, at millisecond precision —
/// not a floored `compile_ms / 1000`, which is what let a 9,900ms compile get
/// charged as spending only 9 whole seconds. Returns None when nothing is
/// left — compiling alone met or exceeded the budget — meaning the run must
/// not be attempted at all: the old code's unconditional `.max(1)` handed the
/// run step a guaranteed extra second regardless of the deficit, so
/// `--timeout 10` could take up to ~11.5s end to end.
fn remaining_run_timeout_secs(budget_secs: u64, elapsed_ms: u64) -> Option<u64> {
    let budget_ms = budget_secs.saturating_mul(1000);
    if elapsed_ms >= budget_ms {
        return None;
    }
    // spawn_and_wait's kill timer only understands whole seconds
    // (Duration::from_secs in platform::spawn_and_wait), so any genuinely
    // positive remainder is rounded UP to the smallest unit it supports
    // rather than floored to zero — a compile that "just barely" fits inside
    // the budget still gets to attempt the run. The guard above is what
    // confines that rounding-up to cases where time truly remains.
    Some((budget_ms - elapsed_ms).div_ceil(1000))
}

fn execute(
    lang_name: &str,
    code: &str,
    stdin_data: &str,
    limits: &Limits,
    workdir: Option<&str>,
) -> serde_json::Value {
    let lang = match canonical(lang_name) {
        Some(l) => l,
        None => {
            let known: Vec<&str> = LANGS.iter().map(|l| l.name).collect();
            return json!({
                "ok": false,
                "error": format!("unknown language '{lang_name}'. Available: {}", known.join(", "))
            });
        }
    };
    if !plan_supported(lang.name, cfg!(windows)) {
        // A structured refusal, not an exit-127 from a shell that is not
        // there. Mirrored in the Python fallback's _execute_python.
        return json!({
            "ok": false,
            "error": format!(
                "'{}' is unsupported on this platform: its plan needs a POSIX \
                 shell to scaffold a project", lang.name),
        });
    }

    static COUNTER: AtomicU64 = AtomicU64::new(0);
    // Secure tempdir: use fs::create_dir (O_EXCL semantics — fails on existing
    // path) with an unpredictable name, retrying on collision. Never
    // create_dir_all: a pre-seeded symlink at a guessable path would redirect
    // the sandbox elsewhere. Multi-user /tmp is hostile by default.
    // A caller-supplied --workdir (sessions) bypasses creation and is NOT
    // deleted afterwards.
    let work = match workdir {
        Some(dir) => Path::new(dir).to_path_buf(),
        None => {
            // Retry ONLY on a name collision. `Err(_) => continue` retried every
            // error, including permanent ones — an unwritable or full temp dir
            // (EACCES/EROFS/ENOSPC) made this spin at 100% CPU forever with no
            // output, until something upstream killed it. Verified: TMPDIR set
            // to a mode-500 directory hung the process indefinitely.
            const MAX_TEMPDIR_ATTEMPTS: u32 = 64;
            let mut last_err: Option<std::io::Error> = None;
            let mut chosen: Option<std::path::PathBuf> = None;
            for _ in 0..MAX_TEMPDIR_ATTEMPTS {
                let nonce = COUNTER.fetch_add(1, Ordering::Relaxed)
                    ^ std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .map(|d| u64::from(d.subsec_nanos()))
                        .unwrap_or(0);
                let candidate =
                    env::temp_dir().join(format!("codecalc-{}-{nonce:x}", std::process::id()));
                match fs::create_dir(&candidate) {
                    Ok(()) => {
                        chosen = Some(candidate);
                        break;
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                    Err(e) => {
                        last_err = Some(e);
                        break;
                    }
                }
            }
            match chosen {
                Some(dir) => dir,
                None => {
                    let why = last_err.map_or_else(
                        || format!("{MAX_TEMPDIR_ATTEMPTS} name collisions in a row"),
                        |e| e.to_string(),
                    );
                    return json!({
                        "ok": false,
                        "error": format!("cannot create a work directory in {}: {why}",
                                         env::temp_dir().display()),
                    });
                }
            }
        }
    };

    // Recorded BEFORE anything runs in the directory, so cleanup compares
    // against the directory as CREATED rather than as the program left it.
    // None for a caller-supplied --workdir, which is never deleted anyway.
    let created_identity = if workdir.is_none() {
        dir_identity(&work)
    } else {
        None
    };

    // The runner's own scratch subdirectory — see RUN_SCRATCH_DIRNAME's doc
    // comment for what lives here and why it is never the workdir root
    // itself. WIPED AND RECREATED FRESH on every call, never reused: a
    // caller-supplied --workdir is a SESSION directory whose executed code
    // is adversarial across calls (same threat model as the Rust
    // `cleanup`/lockfile machinery elsewhere in this codebase).
    //
    // CRITICAL, reported by adversarial review and reproduced end to end:
    // an earlier version of this REUSED an existing real directory here
    // (created fresh, or accepted as-is if already a plain directory) —
    // which made the directory ITSELF symlink-safe but left every file
    // WRITTEN INSIDE it exposed. A prior call's executed code can plant
    // `.codecalc-run/main.<ext> -> <work>/important.txt` (the program runs
    // with `work` as its cwd, so it can reach both names), and the next
    // call's `fs::write(&file, code)` — an ordinary open-write-truncate —
    // FOLLOWS that symlink and overwrites `important.txt` with whatever
    // entry file is executing next, including, via a session_list-
    // disclosed absolute workdir path, a DIFFERENT session's file.
    //
    // Recreating from empty every time removes the attack surface at its
    // root: `fs::remove_dir_all` on a modern Rust toolchain (1.58.1+; this
    // one pins 1.97.1) walks by directory file descriptor on platforms that
    // support it and never opens through a nested symlink, only unlinks it
    // — so wiping cannot be redirected outside `work` either. `.codecalc-
    // run` existing as a symlink (or any other non-directory) is still
    // refused outright rather than removed, via `fs::symlink_metadata`
    // (never `fs::metadata`, which would follow it). A pre-existing REAL
    // directory is not silently wiped past either: `find_tainted_entry`
    // below scans everything inside it first, and the WHOLE RUN is refused
    // if anything there is not a plain file or plain directory — a
    // symlink, FIFO, device or socket planted between two calls in the
    // same session is tampering worth surfacing, not debris worth quietly
    // cleaning up before proceeding.
    //
    // The remaining writes into this now-guaranteed-fresh directory (the
    // source file below, and `run_step`'s three redirect files) open with
    // `create_new(true)` instead of `fs::write`/`File::create` — `O_EXCL`
    // (Unix) / `CREATE_NEW` (Windows) refuses ANY pre-existing entry at the
    // final path component atomically, with no separate check-then-open
    // window to lose a race in, which is what makes the guarantee real
    // rather than probably-fine.
    let run_dir = work.join(RUN_SCRATCH_DIRNAME);
    match fs::symlink_metadata(&run_dir) {
        Ok(md) if md.file_type().is_dir() => {
            match find_tainted_entry(&run_dir) {
                Ok(Some(bad)) => {
                    if workdir.is_none() {
                        remove_own_workdir(&work, created_identity);
                    }
                    return json!({
                        "ok": false,
                        "error": format!(
                            "refusing to run: {} inside the runner scratch directory \
                             is not a plain file or directory (symlink, device, FIFO \
                             or socket) — this session's own workspace may have been \
                             tampered with between calls",
                            bad.display()),
                    });
                }
                Ok(None) => {}
                Err(e) => {
                    if workdir.is_none() {
                        remove_own_workdir(&work, created_identity);
                    }
                    return json!({
                        "ok": false,
                        "error": format!("cannot inspect runner scratch directory {}: {e}",
                                         run_dir.display()),
                    });
                }
            }
            // Identity-checked, not a bare `fs::remove_dir_all` — this
            // directory was not created by THIS call (it passed the taint
            // scan above from an earlier one), so the same "is this still
            // what I just looked at" re-check `remove_own_workdir` applies
            // to `work` applies here too. scripts/check_parity.py gates
            // that codecalc/executor.py's Python twin uses the equivalent
            // `_rmtree_checked` rather than a bare `shutil.rmtree`.
            let scratch_identity = dir_identity(&run_dir);
            if !remove_dir_checked(&run_dir, scratch_identity) {
                if workdir.is_none() {
                    remove_own_workdir(&work, created_identity);
                }
                return json!({
                    "ok": false,
                    "error": format!(
                        "refusing to reset runner scratch directory {}: its identity \
                         changed between being scanned and being removed",
                        run_dir.display()),
                });
            }
        }
        Ok(_) => {
            if workdir.is_none() {
                remove_own_workdir(&work, created_identity);
            }
            return json!({
                "ok": false,
                "error": format!(
                    "refusing to use {} as the runner scratch directory: it \
                     already exists and is not a plain directory",
                    run_dir.display()),
            });
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => {
            if workdir.is_none() {
                remove_own_workdir(&work, created_identity);
            }
            return json!({
                "ok": false,
                "error": format!("cannot inspect runner scratch directory {}: {e}",
                                 run_dir.display()),
            });
        }
    }
    if let Err(e) = fs::create_dir(&run_dir) {
        if workdir.is_none() {
            remove_own_workdir(&work, created_identity);
        }
        return json!({
            "ok": false,
            "error": format!("cannot create runner scratch directory {}: {e}",
                             run_dir.display()),
        });
    }

    let file = run_dir.join(format!("main.{ext}", ext = lang.ext));
    // Windows needs the .exe extension for the compiled artifact; CreateProcess
    // will not treat an extensionless PE as executable the way exec() does.
    let exe = run_dir.join(if cfg!(windows) { "a.exe" } else { "a.out" });
    if let Err(e) = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&file)
        .and_then(|mut f| f.write_all(code.as_bytes()))
    {
        // Only remove a directory WE created. This used to be unconditional, so
        // a failed source write deleted a caller-supplied --workdir — i.e. the
        // whole session workspace, user data included. And it was reachable
        // from inside the sandbox: executed code doing
        //     rm -f main.sh && mkdir main.sh
        // makes the next write fail, and the next execute_code wiped the
        // session. Verified end to end before the fix.
        if workdir.is_none() {
            remove_own_workdir(&work, created_identity);
        }
        return json!({
            "ok": false,
            "error": format!("failed to write source to {}: {e}", file.display()),
        });
    }

    // Written and set unconditionally, regardless of language — see
    // NODE_SIBLING_SHIM_JS's own doc comment for why this is inert for
    // every runtime but node, the same shape as `pythonpath` above.
    let node_shim = run_dir.join(NODE_SIBLING_SHIM_NAME);
    if let Err(e) = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&node_shim)
        .and_then(|mut f| f.write_all(NODE_SIBLING_SHIM_JS.as_bytes()))
    {
        if workdir.is_none() {
            remove_own_workdir(&work, created_identity);
        }
        return json!({
            "ok": false,
            "error": format!("failed to write {}: {e}", node_shim.display()),
        });
    }

    let started = Instant::now();
    let work_s = work.to_string_lossy().into_owned();
    let run_dir_s = run_dir.to_string_lossy().into_owned();
    let node_extra_env: [(&str, String); 2] = [
        ("NODE_OPTIONS", format!("--require={}", node_shim.display())),
        ("CODECALC_WORKDIR_ROOT", work_s.clone()),
    ];

    // Compile and run SHARE the wall-clock budget. Each step used to get the
    // full `limits.timeout`, so a compiled language could take 2x the value the
    // caller asked for — and the Python wrapper kills the executor at
    // timeout+30, which for a 120s request lands well inside that 240s ceiling.
    let mut compile_ms: u64 = 0;
    if let Some(compile) = lang.compile {
        let argv: Vec<String> = compile
            .iter()
            .map(|t| {
                substitute(
                    t,
                    &source_arg(lang.name, &file.to_string_lossy(), cfg!(windows)),
                    &exe.to_string_lossy(),
                    &run_dir_s,
                )
            })
            .collect();
        // Compile-step cwd is the scratch directory, not `work`: compiling is
        // not the user's own program running, so any incidental compiler
        // byproduct belongs in scratch, never at the workdir root.
        let sr = run_step(
            &argv,
            &run_dir,
            &run_dir,
            "compile",
            b"",
            limits,
            &StepEnv {
                pythonpath: &work,
                extra: &node_extra_env,
            },
        );
        compile_ms = u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX);
        if sr.timed_out || sr.exit_code != 0 || sr.signal.is_some() {
            let result = json!({
                "ok": false, "language": lang.name, "phase": "compile",
                "stdout": sr.stdout, "stderr": sr.stderr,
                "exit_code": if sr.signal.is_some() { serde_json::Value::Null } else { serde_json::Value::from(sr.exit_code) },
                "duration_ms": compile_ms, "compile_ms": compile_ms,
                "cpu_ms": sr.cpu_ms, "peak_memory_kb": sr.peak_memory_kb,
                "timed_out": sr.timed_out, "verdict": verdict(&sr, limits),
                "unenforced": sr.unenforced,
                // Same key set as the success return — contract_check.py gates
                // that the two agree, so a field added to one must be added to
                // the other or a compile failure loses a key its caller has.
                "output_error": sr.output_error,
                // Whether either stream hit the cap. Computed since the cap
                // landed, used to raise the OLE verdict, and emitted by NEITHER
                // return until now — so the Rust backend told a caller "OLE"
                // and left the field that says why out of the payload, while
                // the Python fallback sent it. Two backends, two key sets,
                // under a contract whose whole claim is that they agree.
                "output_truncated": sr.output_truncated,
                // The sizes behind that boolean. A compiler that emits megabytes
                // of template errors is the case this exists for: the caller
                // needs to know whether raising max_output_kb would help.
                "stdout_bytes": sr.stdout_bytes,
                "stderr_bytes": sr.stderr_bytes,
                // total_ms/platform/workdir are on the success return and were
                // missing here, so result["workdir"] was a KeyError for callers
                // whose only mistake was writing code that did not compile.
                // A compile failure is an ordinary outcome, not an exceptional
                // one, and nothing in the contract says these fields are
                // conditional. contract_check.py now asserts the two key sets
                // are EQUAL rather than listing fields per path, because
                // listing them per path is what let this diverge unnoticed.
                "total_ms": u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX),
                "platform": std::env::consts::OS,
                "workdir": work_s,
            });
            if workdir.is_none() {
                remove_own_workdir(&work, created_identity);
            }
            return result;
        }
    }

    let argv: Vec<String> = lang
        .run
        .iter()
        .map(|t| {
            substitute(
                t,
                &source_arg(lang.name, &file.to_string_lossy(), cfg!(windows)),
                &exe.to_string_lossy(),
                &run_dir_s,
            )
        })
        .collect();
    // Budget what is LEFT after compiling, so compile+run cannot exceed the
    // caller's timeout between them.
    let mut run_limits = *limits;
    if compile_ms > 0 {
        let elapsed_ms = u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX);
        match remaining_run_timeout_secs(limits.timeout, elapsed_ms) {
            Some(secs) => run_limits.timeout = secs,
            None => {
                // Compiling alone met or exceeded the whole wall-clock budget
                // (compile's own kill enforcement is itself only
                // second-granular, so it can overrun its nominal share
                // slightly before the kill lands). Report TLE without
                // starting the run — handing it a `.max(1)` floor regardless
                // of the deficit was exactly the bug this replaces: with a
                // --timeout 10 request, a 10.5s compile used to still get a
                // guaranteed extra second for the run, pushing the total to
                // ~11.5s.
                let result = json!({
                    "ok": false, "language": lang.name, "phase": "run",
                    "stdout": "", "stderr": "<killed: exceeded wall-clock timeout>",
                    "exit_code": serde_json::Value::Null,
                    "duration_ms": 0, "compile_ms": compile_ms,
                    "total_ms": elapsed_ms,
                    "cpu_ms": 0, "peak_memory_kb": 0,
                    "timed_out": true, "verdict": "TLE",
                    "unenforced": Vec::<&str>::new(),
                    // This return carries a `verdict`, so it IS a full envelope
                    // by the contract's own discrimination rule, and it was
                    // missing four of the envelope's fields — two of them
                    // (output_truncated, output_error) since before the byte
                    // counts existed. Nobody noticed because no schema had ever
                    // been written down to check it against.
                    //
                    // null for both counts, not 0: the run phase never started,
                    // so there is no run output to have measured. The compile
                    // step's own output was already reported and discarded with
                    // its StepResult when the budget check failed.
                    "output_truncated": false,
                    "output_error": serde_json::Value::Null,
                    "stdout_bytes": serde_json::Value::Null,
                    "stderr_bytes": serde_json::Value::Null,
                    // platform and workdir are on the normal return too. A new
                    // return that omits fields the success path carries is how
                    // this executor's shape drifted before: AUDIT.md records
                    // "The documented return shape is the SAME on both
                    // backends. It was not". A caller reading result["workdir"]
                    // should not have to know which of three ways it got here.
                    "platform": std::env::consts::OS,
                    "workdir": work_s,
                });
                if workdir.is_none() {
                    remove_own_workdir(&work, created_identity);
                }
                return result;
            }
        }
    }
    let run_started = Instant::now();
    // Run-step cwd is `work` itself (the workdir root), NOT the scratch
    // directory — this is the user's actual program running, and its own
    // relative file access must resolve where a caller can see it. Only the
    // I/O redirect files go into the scratch directory (`run_dir` as
    // `io_dir`).
    let sr = run_step(
        &argv,
        &run_dir,
        &work,
        "run",
        stdin_data.as_bytes(),
        &run_limits,
        &StepEnv {
            pythonpath: &work,
            extra: &node_extra_env,
        },
    );
    // duration_ms is the RUN, not run+compile. It used to be measured from
    // before the compile step, so `benchmark` on C/C++/Rust was timing gcc:
    // a hello-world reported duration_ms=126 with cpu_ms=0.
    let duration_ms = u64::try_from(run_started.elapsed().as_millis()).unwrap_or(u64::MAX);
    let total_ms = u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX);

    let result = json!({
        // An output we could not read is not a successful run. This used to
        // be exit-status-only, so a failed read returned ok=true with an empty
        // stdout — a wrong answer wearing a success. `output_error` below says
        // which stream and why.
        "ok": sr.exit_code == 0 && !sr.timed_out && sr.signal.is_none()
            && sr.output_error.is_none(),
        "language": lang.name,
        "phase": "run",
        "stdout": sr.stdout,
        "stderr": sr.stderr,
        "exit_code": if sr.signal.is_some() { serde_json::Value::Null } else { serde_json::Value::from(sr.exit_code) },
        "duration_ms": duration_ms,
        "compile_ms": compile_ms,
        "total_ms": total_ms,
        "cpu_ms": sr.cpu_ms,
        "peak_memory_kb": sr.peak_memory_kb,
        "timed_out": sr.timed_out,
        "verdict": verdict(&sr, &run_limits),
        // Which guarantees this OS could not apply. Empty on a full-featured
        // Linux run; non-empty is not an error, it is the sandbox declining to
        // claim something it did not do.
        "unenforced": sr.unenforced,
        // Present ONLY when a stream could not be read. Absent is the normal
        // case and means stdout/stderr above are what the program produced;
        // present means at least one of them is not, and names which and why
        // including the OS error number.
        "output_error": sr.output_error,
        // See the compile return for why this was missing.
        "output_truncated": sr.output_truncated,
        // How much each stream ACTUALLY produced, before the response cap.
        // `output_truncated` alone told a caller that output was cut and not by
        // how much, so "printed 9 KiB" and "printed 4 MB" were the same answer
        // and there was no way to size a retry.
        "stdout_bytes": sr.stdout_bytes,
        "stderr_bytes": sr.stderr_bytes,
        "platform": std::env::consts::OS,
        "workdir": work_s,
    });
    if workdir.is_none() {
        remove_own_workdir(&work, created_identity);
    }
    result
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();

    // --probe: report runtime availability, no execution
    if args.iter().any(|a| a == "--probe") {
        println!("{}", probe());
        return;
    }
    if args.iter().any(|a| a == "--languages") {
        let names: Vec<&str> = LANGS.iter().map(|l| l.name).collect();
        println!("{}", json!(names));
        return;
    }
    // --capabilities: static host facts the CLI can answer without executing
    // anything, distinct from --probe's per-language runtime map so a new key
    // here can never be misread as a language. Today just whether `no_net`
    // gets an in-kernel guarantee on THIS host — the fact a capability broker
    // needs to advertise `network_control` truthfully rather than assuming
    // every rust-backend host enforces it (a Linux kernel without seccomp, or
    // macOS, only has the bypassable symbol shim; Windows has no `no_net`
    // mechanism at all).
    if args.iter().any(|a| a == "--capabilities") {
        println!(
            "{}",
            json!({
                "no_net_kernel_enforcement": platform::no_net_kernel_enforcement_available(),
            })
        );
        return;
    }

    let mut lang = String::new();
    let mut stdin_data = String::new();
    let mut stdin_file: Option<String> = None;
    let mut workdir: Option<String> = None;
    // NOT measured here. Sizing RLIMIT_NPROC means walking /proc for every
    // process on the machine, and doing it during argument parsing charged that
    // cost to invocations that never spawn anything: `--lang notalanguage`
    // performed 6379 syscalls to produce a one-line error, ~11ms of it system
    // time on a box with 590 processes. It is measured lazily instead, at the
    // point a step is actually about to run, and cached for the rest of the
    // invocation. apply_limits() still cannot walk /proc — it runs in pre_exec —
    // so the measurement remains a parent-side one.
    let mut limits = Limits::default();

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--lang" => {
                i += 1;
                if i < args.len() {
                    lang = args[i].clone();
                }
            }
            "--timeout" => {
                i += 1;
                if i < args.len() {
                    limits.timeout = args[i].parse().unwrap_or(10);
                }
            }
            "--max-cpu" => {
                i += 1;
                if i < args.len() {
                    limits.max_cpu = args[i].parse().unwrap_or(0);
                }
            }
            "--max-memory-mb" => {
                i += 1;
                if i < args.len() {
                    limits.max_memory_mb = args[i].parse().unwrap_or(0);
                }
            }
            "--max-output-kb" => {
                i += 1;
                if i < args.len() {
                    limits.max_output_kb = args[i].parse().unwrap_or(0);
                }
            }
            "--stdin" => {
                i += 1;
                if i < args.len() {
                    stdin_data = args[i].clone();
                }
            }
            "--stdin-file" => {
                // stdin too large for argv (E2BIG) — read from a file instead
                i += 1;
                if i < args.len() {
                    stdin_file = Some(args[i].clone());
                }
            }
            "--workdir" => {
                i += 1;
                if i < args.len() {
                    workdir = Some(args[i].clone());
                }
            }
            "--no-net" => {
                limits.no_net = true;
            }
            _ => {}
        }
        i += 1;
    }

    if let Some(path) = stdin_file {
        stdin_data = fs::read_to_string(&path).unwrap_or_default();
    }

    let mut code = String::new();
    let _ = std::io::stdin().read_to_string(&mut code);

    let result = execute(&lang, &code, &stdin_data, &limits, workdir.as_deref());
    println!("{result}");
}

#[cfg(test)]
mod tests {
    use super::*;

    // Bug #39: compile and run share `--timeout`'s wall-clock budget, but the
    // remaining-budget arithmetic floored `compile_ms / 1000` and then
    // unconditionally applied `.max(1)`, so the run step was ALWAYS handed at
    // least one more second — even when compiling alone had already met or
    // exceeded the entire budget. --timeout 10 could then take up to ~11.5s.

    #[test]
    fn compile_that_exactly_exhausts_the_budget_gets_no_run() {
        assert_eq!(remaining_run_timeout_secs(10, 10_000), None);
    }

    #[test]
    fn compile_that_overruns_the_budget_gets_no_run() {
        // The reported case: a 10.5s compile against a 10s budget. The old
        // formula floored 10_500ms to spent_secs=10, then
        // 10.saturating_sub(10).max(1) handed the run a guaranteed extra
        // second anyway, pushing the total to ~11.5s.
        assert_eq!(remaining_run_timeout_secs(10, 10_500), None);
    }

    #[test]
    fn compile_that_leaves_a_sub_second_remainder_still_gets_one_second() {
        // The other reported case: a 9.9s compile against a 10s budget truly
        // leaves 100ms. The platform's kill timer only understands whole
        // seconds (Duration::from_secs in platform::spawn_and_wait), so that
        // remainder rounds UP to the smallest unit it supports rather than
        // being refused outright — a compile that "just barely" fits still
        // gets to attempt the run. This is unchanged from the old formula in
        // THIS case; what changes is that it no longer ALSO applies when
        // nothing (or less than nothing) is left, per the two tests above.
        assert_eq!(remaining_run_timeout_secs(10, 9_900), Some(1));
    }

    #[test]
    fn compile_using_half_the_budget_leaves_the_other_half() {
        assert_eq!(remaining_run_timeout_secs(10, 5_000), Some(5));
    }

    //
    // These pass `windows` explicitly rather than relying on the build target,
    // so the Windows rendering is checked on the Linux and macOS legs too. The
    // bug being prevented is only reachable on Windows; a test that could only
    // run there would have caught it after shipping, not before.

    /// The repair, stated as the property that makes it safe: nothing left for
    /// the MSYS escape pass to eat is "no BACKSLASH", not "no separator at
    /// all" — the rendered path is scratch-relative (RUN_SCRATCH_DIRNAME's
    /// forward slash), which the escape pass leaves untouched. A spaced
    /// profile is in the fixture because the same re-parse splits on spaces,
    /// and `C:\Users\John Smith\` is the untested case flags as still open
    /// for every other runtime.
    #[test]
    fn posix_argv_languages_get_a_scratch_relative_path_with_no_backslash_on_windows() {
        let win = r"C:\Users\John Smith\AppData\Local\Temp\codecalc-ab12\main.sh";
        for lang in POSIX_ARGV_LANGUAGES {
            let got = source_arg(lang, win, true);
            assert_eq!(
                got,
                format!("{RUN_SCRATCH_DIRNAME}/main.sh"),
                "{lang} kept a path"
            );
            assert!(!got.contains('\\'), "{lang} kept a backslash");
            assert!(!got.contains(' '), "{lang} kept a space");
        }
    }

    /// Forward slashes are legal separators on Windows, so a mixed path must
    /// not keep whichever half a single-separator split missed.
    #[test]
    fn a_mixed_separator_path_is_still_reduced_to_the_scratch_relative_name() {
        assert_eq!(
            source_arg("bash", r"C:/Users/me\tmp/main.sh", true),
            format!("{RUN_SCRATCH_DIRNAME}/main.sh")
        );
    }

    /// Unix argv is a real array — nothing re-parses it, so there is no bug to
    /// fix and no reason to change what works.
    #[test]
    fn unix_keeps_the_absolute_path() {
        assert_eq!(
            source_arg("bash", "/tmp/codecalc-ab12/main.sh", false),
            "/tmp/codecalc-ab12/main.sh"
        );
    }

    /// A runtime that takes argv as given is untouched even on Windows: this
    /// list is scoped to where the failure was measured, not applied broadly.
    #[test]
    fn a_normal_language_is_untouched_on_windows() {
        let win = r"C:\Temp\codecalc-ab12\main.py";
        assert_eq!(source_arg("python3", win, true), win);
    }

    /// `{exe}` must NOT get this treatment — it is spawned, and a bare name
    /// would be resolved against PATH instead of the workdir. Asserting it
    /// through `substitute` covers the wiring, not just the helper.
    #[test]
    fn the_compiled_artifact_keeps_its_absolute_path() {
        let out = substitute("{exe}", "main.sh", r"C:\Temp\w\a.exe", r"C:\Temp\w");
        assert_eq!(out, r"C:\Temp\w\a.exe");
    }

    // `windows` is a parameter for the same reason source_arg's is: these run
    // on every CI leg, so the Windows verdicts are checked where Windows is
    // not available to check them.

    #[test]
    fn shell_wrapped_plans_are_unsupported_on_windows() {
        for lang in SHELL_WRAPPED {
            assert!(!plan_supported(lang, true), "{lang} claimed a Windows plan");
            assert!(plan_supported(lang, false), "{lang} lost its POSIX plan");
        }
    }

    #[test]
    fn csharp_is_no_longer_shell_wrapped_and_runs_everywhere() {
        let lang = canonical("csharp").expect("csharp is registered");
        assert_eq!(lang.run[0], "dotnet", "csharp reacquired a wrapper");
        assert!(plan_supported("csharp", true));
        assert!(!SHELL_WRAPPED.contains(&"csharp"));
    }

    #[test]
    fn every_wrapped_language_names_its_real_tool() {
        // A wrapper with no tool mapping would fall through to the bash probe,
        // which is the exact lie this set exists to end.
        for lang in SHELL_WRAPPED {
            assert!(wrapped_tool(lang).is_some(), "{lang} has no wrapped_tool");
        }
        assert!(wrapped_tool("python3").is_none());
    }

    // ── secondary_cmd: kotlin's run step needs java, and nothing else does ──
    // A regression test for the exact reported defect: kotlin's `run` step
    // launches `java -jar ...` directly, a binary `kotlinc` resolving says
    // nothing about, and `probe()` used to advertise kotlin available from
    // java alone. Reproduced as a unit test on the mechanical derivation
    // itself, separately from the subprocess-level fixture in
    // tests/test_executor_sweep.py that exercises the whole `--probe` CLI.

    #[test]
    fn kotlin_needs_java_beyond_its_compile_tool() {
        let kotlin = canonical("kotlin").expect("kotlin is registered");
        assert_eq!(
            secondary_cmd(kotlin),
            Some("java"),
            "kotlin's run step launches java directly; kotlinc resolving says nothing about it"
        );
    }

    #[test]
    fn a_compiled_language_whose_run_step_is_just_its_own_binary_needs_no_second_tool() {
        // c/cpp/rust/fortran all run `{exe}` — the binary compiling just
        // produced — so the compile tool resolving is already the whole
        // story. Asserted over every LANG with a compile step rather than
        // one hand-picked name, so a future compiled language is covered by
        // construction instead of needing its own line added here.
        for lang in LANGS {
            if lang.compile.is_none() {
                continue;
            }
            if lang.run.first().is_some_and(|c| c.starts_with('{')) {
                assert_eq!(
                    secondary_cmd(lang),
                    None,
                    "{}: an {{exe}}-style run step needs no second tool",
                    lang.name
                );
            }
        }
    }

    #[test]
    fn an_interpreted_language_with_no_compile_step_has_no_secondary_command() {
        let python3 = canonical("python3").expect("python3 is registered");
        assert_eq!(secondary_cmd(python3), None);
    }
}
