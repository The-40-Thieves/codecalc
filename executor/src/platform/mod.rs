//! Platform-specific sandboxing.
//!
//! The three OSes do not offer the same primitives, and pretending otherwise is
//! how a sandbox ends up reporting limits it never applied. What each one
//! actually gives us:
//!
//! | Guarantee            | Linux                | macOS                | Windows                       |
//! |----------------------|----------------------|----------------------|-------------------------------|
//! | CPU time             | RLIMIT_CPU           | RLIMIT_CPU           | — (wall clock only)           |
//! | Address space        | RLIMIT_AS            | RLIMIT_AS (see below)| Job ProcessMemoryLimit        |
//! | File size            | RLIMIT_FSIZE         | RLIMIT_FSIZE         | — (output capped on read)     |
//! | Open files           | RLIMIT_NOFILE        | RLIMIT_NOFILE        | — (no equivalent)             |
//! | Fork bomb            | RLIMIT_NPROC (uid)   | RLIMIT_NPROC (uid)   | Job ActiveProcessLimit (job!) |
//! | Kill the whole tree  | killpg(SIGKILL)      | killpg(SIGKILL)      | TerminateJobObject            |
//! | CPU + peak memory    | wait4 rusage         | wait4 rusage         | Job accounting                |
//! | Block network        | LD_PRELOAD shim      | DYLD_… (SIP-limited) | — not implemented             |
//!
//! Two things are worth noticing in that table. Windows' ActiveProcessLimit is
//! scoped to the JOB, which makes it a strictly better fork-bomb guard than
//! RLIMIT_NPROC's uid-wide budget — the thing that broke 14 of 31 runtimes on
//! Linux cannot happen there. And Windows has no CPU-time or open-file limit
//! here, which is why `Wait::unenforced` exists: every caller is told which
//! guarantees did NOT apply, rather than being left to assume they all did.

use std::path::Path;
use std::process::Command;

#[cfg(unix)]
mod unix;
#[cfg(unix)]
pub use unix::{current_uid_tasks, spawn_and_wait};

#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use windows::{current_uid_tasks, spawn_and_wait};

/// Raw std handles, for the Windows creation-time job path (THE-818).
///
/// `std::process::Command` cannot pass `PROC_THREAD_ATTRIBUTE_JOB_LIST`, so
/// that path needs a raw `CreateProcessW` — which needs the three handles
/// `Stdio::from(File)` would otherwise consume. Captured before the conversion
/// and carried here. Zeroed and ignored on Unix.
#[derive(Clone, Copy, Default)]
// Read only by the Windows creation-time path; on Unix the struct is carried
// and ignored, which is the point of it being cross-platform.
#[cfg_attr(not(windows), allow(dead_code))]
pub struct RawStdio {
    pub stdin: isize,
    pub stdout: isize,
    pub stderr: isize,
}

/// Resolved, per-execution resource ceilings handed to the platform layer.
#[derive(Clone, Copy)]
pub struct ResolvedLimits {
    pub timeout_secs: u64,
    /// Unix only — Windows Job Objects have no CPU-time limit, and report it via
    /// `Wait::unenforced` instead of pretending otherwise.
    #[cfg_attr(windows, allow(dead_code))]
    pub cpu_secs: u64,
    pub memory_bytes: u64,
    /// Unix only (RLIMIT_FSIZE). Windows caps output when reading it back.
    #[cfg_attr(windows, allow(dead_code))]
    pub fsize_bytes: u64,
    /// Unix only (RLIMIT_NOFILE). Windows has no per-process equivalent.
    #[cfg_attr(windows, allow(dead_code))]
    pub nofile: u64,
    /// Max concurrent processes. Uid-wide on Unix (RLIMIT_NPROC), job-scoped on
    /// Windows (ActiveProcessLimit).
    pub max_processes: u64,
    /// Only the Windows backend reads this (to report it as unenforceable);
    /// Unix applies the shim at the Command level before spawn_and_wait.
    #[cfg_attr(unix, allow(dead_code))]
    pub no_net: bool,
}

/// Outcome of running one child to completion (or killing it).
pub struct Wait {
    pub exit_code: i64,
    /// Unix signal that killed the child, if any. Always None on Windows, which
    /// has no signals — an abnormal exit shows up as a non-zero exit_code.
    pub signal: Option<i32>,
    pub timed_out: bool,
    pub cpu_ms: u64,
    /// Peak resident memory in KiB. Normalising to KiB is deliberate: the raw
    /// source differs per platform (getrusage's ru_maxrss is KiB on Linux and
    /// BYTES on macOS/BSD; Job accounting reports bytes), and reading one as the
    /// other is a silent 1024x error. See the unit conversion in each backend.
    pub peak_memory_kb: u64,
    /// Limits this platform could not apply, by name. Surfaced in the JSON so a
    /// caller can tell "the limit held" from "there was no limit".
    pub unenforced: Vec<&'static str>,
}

/// Where the network-blocking shim lives, if this platform has one.
pub fn no_net_shim(exe_dir: &Path) -> Option<std::path::PathBuf> {
    let name = if cfg!(target_os = "macos") {
        "blocknet.dylib"
    } else if cfg!(target_os = "linux") {
        "blocknet.so"
    } else {
        return None; // Windows: no LD_PRELOAD equivalent for this purpose
    };
    let p = exe_dir.join(name);
    if p.is_file() { Some(p) } else { None }
}

/// Env var used to preload that shim. DYLD_INSERT_LIBRARIES on macOS is honoured
/// only for non-SIP-protected binaries, so `--no-net` is weaker there than on
/// Linux; the caller reports it as unenforced when the shim is absent.
pub fn preload_env_var() -> Option<&'static str> {
    if cfg!(target_os = "macos") {
        Some("DYLD_INSERT_LIBRARIES")
    } else if cfg!(target_os = "linux") {
        Some("LD_PRELOAD")
    } else {
        None
    }
}

/// Apply the no-net shim to a command if this platform supports one.
/// Returns false when the platform (or a missing shim) means no blocking happened.
pub fn apply_no_net(cmd: &mut Command, exe_dir: &Path) -> bool {
    match (preload_env_var(), no_net_shim(exe_dir)) {
        (Some(var), Some(shim)) => {
            cmd.env(var, shim.to_string_lossy().into_owned());
            true
        }
        _ => false,
    }
}

/// Quote one argument the way the MSVC C runtime parses it back.
///
/// `CreateProcessW` takes one STRING; the callee re-splits it. Getting this
/// wrong corrupts every execution silently, which is exactly how THE-817
/// happened one layer up. Rule, from Microsoft's own parser description:
/// backslashes are literal EXCEPT immediately before a quote, where they are
/// escapes and must be doubled — including the run before the closing quote.
// Used by the Windows creation-time path; unused on Unix, where it is kept
// compiled and TESTED anyway. A rule only one platform can check is a rule
// nothing checks — the lesson from THE-817's `os.path` vs `ntpath` branch.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn quote_arg(arg: &std::ffi::OsStr) -> String {
    let s = arg.to_string_lossy();
    if !s.is_empty() && !s.contains([' ', '\t', '"']) {
        return s.into_owned();
    }
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    let mut backslashes = 0usize;
    for c in s.chars() {
        match c {
            '\\' => {
                backslashes += 1;
                out.push('\\');
            }
            '"' => {
                // Double the run, then escape the quote itself.
                for _ in 0..backslashes {
                    out.push('\\');
                }
                backslashes = 0;
                out.push('\\');
                out.push('"');
            }
            _ => {
                backslashes = 0;
                out.push(c);
            }
        }
    }
    // The run before the CLOSING quote is escaping it, so double it too.
    for _ in 0..backslashes {
        out.push('\\');
    }
    out.push('"');
    out
}

#[cfg(test)]
mod quoting_tests {
    use super::quote_arg;
    use std::ffi::OsStr;

    // These test the REAL function, not a restatement of it. An earlier draft
    // asserted against a reference reimplementation written in the same sitting
    // — which agrees by construction and proves nothing. quote_arg lives here
    // rather than in the cfg(windows) module for exactly that reason: a rule
    // only Windows can check is a rule nothing checks.

    fn q(s: &str) -> String {
        quote_arg(OsStr::new(s))
    }

    #[test]
    fn a_plain_argument_is_not_quoted() {
        assert_eq!(q("main.py"), "main.py");
    }

    #[test]
    fn backslashes_not_before_a_quote_are_literal() {
        // A Windows path must survive untouched. THE-817 inverted.
        assert_eq!(q(r"C:\Temp\main.py"), r"C:\Temp\main.py");
    }

    #[test]
    fn a_space_forces_quoting() {
        assert_eq!(q("John Smith"), "\"John Smith\"");
    }

    #[test]
    fn the_backslash_run_before_the_closing_quote_is_doubled() {
        // Without doubling, the trailing backslash escapes OUR closing quote
        // and the argument swallows the rest of the command line.
        assert_eq!(q(r"C:\a b\"), "\"C:\\a b\\\\\"");
    }

    #[test]
    fn an_embedded_quote_is_escaped() {
        assert_eq!(q(r#"say "hi""#), r#""say \"hi\"""#);
    }

    #[test]
    fn a_backslash_run_before_an_embedded_quote_is_doubled() {
        assert_eq!(q(r#"a\"b"#), r#""a\\\"b""#);
    }

    #[test]
    fn the_empty_argument_survives_as_an_empty_quoted_string() {
        // Dropping it would silently shift every later argument left.
        assert_eq!(q(""), "\"\"");
    }
}
