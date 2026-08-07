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
