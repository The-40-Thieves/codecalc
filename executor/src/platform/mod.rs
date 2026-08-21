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

use std::path::{Path, PathBuf};
use std::process::Command;

#[cfg(unix)]
mod unix;
#[cfg(unix)]
pub use unix::{current_uid_tasks, spawn_and_wait};

#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use windows::{current_uid_tasks, spawn_and_wait};

/// Raw std handles, for the Windows creation-time job path.
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
/// wrong corrupts every execution silently, which is exactly how a past
/// misquoting bug happened one layer up. Rule, from Microsoft's own parser
/// description: backslashes are literal EXCEPT immediately before a quote,
/// where they are escapes and must be doubled — including the run before
/// the closing quote.
// Used by the Windows creation-time path; unused on Unix, where it is kept
// compiled and TESTED anyway. A rule only one platform can check is a rule
// nothing checks — the lesson from an earlier `os.path` vs `ntpath` branch.
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

/// Does `path` traverse the Windows Store app-execution alias directory,
/// `…\Microsoft\WindowsApps\…`?
///
/// Those aliases are zero-length reparse stubs that hand off to the Store
/// activation broker, which launches the real program OUTSIDE the caller's job
/// object — escaping every sandbox limit. The Windows backend refuses a runtime
/// that resolves to one. Matching is the `Microsoft`→`WindowsApps`
/// PAIR, never a bare `WindowsApps`: the real package store at
/// `Program Files\WindowsApps` holds legitimate executables and must not match.
///
/// Split on BOTH separators so the check is byte-identical on the Linux CI that
/// tests it and the Windows host that runs it. `Path::components` treats `\` as
/// an ordinary character off-Windows, so a backslash alias path would collapse
/// to one component there and the pair would never be seen — the same
/// `os.path` vs `ntpath` platform split was. Kept compiled and TESTED on
/// every platform for exactly that reason; only the Windows backend calls it.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn is_windowsapps_alias_path(path: &Path) -> bool {
    let display = path.to_string_lossy();
    let mut prev_is_microsoft = false;
    for part in display.split(['/', '\\']).filter(|p| !p.is_empty()) {
        if prev_is_microsoft && part.eq_ignore_ascii_case("WindowsApps") {
            return true;
        }
        prev_is_microsoft = part.eq_ignore_ascii_case("Microsoft");
    }
    false
}

/// Outcome of choosing a runtime executable from an ordered candidate list.
#[cfg_attr(not(windows), allow(dead_code))]
pub enum RuntimeChoice {
    /// A real, launchable executable — the first candidate that is a file and
    /// is not an app-execution alias.
    Found(PathBuf),
    /// Every matching candidate was a Store app-execution alias. Carries the
    /// FIRST one seen, so the caller can fail closed naming a concrete path.
    OnlyAlias(PathBuf),
    /// Nothing on the candidate list was a file at all.
    None,
}

/// Choose a runtime executable from `candidates`, scanning in order.
///
/// The security-critical resolution decision behind interpreter selection, kept here —
/// separate from Windows filesystem I/O — so the Linux CI can exercise the
/// control flow with injected classifiers. A candidate that is a file but an
/// app-execution alias is SKIPPED rather than returned, so an alias early on
/// PATH cannot shadow a real interpreter later on it; if every file found was an
/// alias, the first is returned as `OnlyAlias` for a specific fail-closed error
/// instead of a bare "not found". `is_file` and `is_alias` are injected so the
/// same logic is driven by real Windows checks in production and by fakes in the
/// tests below.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn choose_runtime<I, F, G>(candidates: I, is_file: F, is_alias: G) -> RuntimeChoice
where
    I: IntoIterator<Item = PathBuf>,
    F: Fn(&Path) -> bool,
    G: Fn(&Path) -> bool,
{
    let mut first_alias: Option<PathBuf> = None;
    for candidate in candidates {
        if !is_file(&candidate) {
            continue;
        }
        if is_alias(&candidate) {
            if first_alias.is_none() {
                first_alias = Some(candidate);
            }
            continue;
        }
        return RuntimeChoice::Found(candidate);
    }
    match first_alias {
        Some(alias) => RuntimeChoice::OnlyAlias(alias),
        None => RuntimeChoice::None,
    }
}

#[cfg(test)]
mod choose_runtime_tests {
    use super::{RuntimeChoice, choose_runtime};
    use std::path::{Path, PathBuf};

    // The candidate ordering is PATH order; the classifiers stand in for the
    // real Windows `is_file`/`is_app_execution_alias` so the branch logic — not
    // the filesystem — is what these tests pin down.
    fn cands(list: &[&str]) -> Vec<PathBuf> {
        list.iter().map(PathBuf::from).collect()
    }
    fn is(p: &Path, name: &str) -> bool {
        p.to_str() == Some(name)
    }

    #[test]
    fn a_real_file_after_an_alias_is_preferred_and_the_alias_skipped() {
        // The case: the alias sorts first on PATH, a real interpreter
        // sits behind it. The real one must win.
        let r = choose_runtime(cands(&["alias", "real"]), |_| true, |p| is(p, "alias"));
        assert!(matches!(r, RuntimeChoice::Found(p) if p == Path::new("real")));
    }

    #[test]
    fn a_real_file_before_an_alias_wins_immediately() {
        let r = choose_runtime(cands(&["real", "alias"]), |_| true, |p| is(p, "alias"));
        assert!(matches!(r, RuntimeChoice::Found(p) if p == Path::new("real")));
    }

    #[test]
    fn only_aliases_fails_closed_naming_the_first() {
        // Fail closed, and name the FIRST alias (what an operator's PATH hits).
        let r = choose_runtime(cands(&["alias1", "alias2"]), |_| true, |_| true);
        assert!(matches!(r, RuntimeChoice::OnlyAlias(p) if p == Path::new("alias1")));
    }

    #[test]
    fn no_file_match_is_none_not_alias() {
        // Nothing is a file → None, never a spurious alias refusal.
        let r = choose_runtime(cands(&["a", "b"]), |_| false, |_| true);
        assert!(matches!(r, RuntimeChoice::None));
    }

    #[test]
    fn missing_candidates_are_stepped_over_to_reach_a_real_file() {
        // PATHEXT / multiple dirs produce candidates that are not files; the
        // scan must step past them to the real hit.
        let r = choose_runtime(cands(&["missing", "real"]), |p| is(p, "real"), |_| false);
        assert!(matches!(r, RuntimeChoice::Found(p) if p == Path::new("real")));
    }

    #[test]
    fn a_run_of_aliases_never_masks_a_later_real_interpreter() {
        let r = choose_runtime(
            cands(&["a1", "a2", "real", "a3"]),
            |_| true,
            |p| !is(p, "real"),
        );
        assert!(matches!(r, RuntimeChoice::Found(p) if p == Path::new("real")));
    }
}

#[cfg(test)]
mod alias_path_tests {
    use super::is_windowsapps_alias_path;
    use std::path::Path;

    fn is_alias(s: &str) -> bool {
        is_windowsapps_alias_path(Path::new(s))
    }

    #[test]
    fn the_real_store_alias_path_is_detected() {
        // The shape measured `python3` resolving to on a Win11 box.
        assert!(is_alias(
            r"C:\Users\alice\AppData\Local\Microsoft\WindowsApps\python3.EXE"
        ));
    }

    #[test]
    fn the_package_store_is_not_an_alias() {
        // Program Files\WindowsApps holds REAL installed Store binaries. A bare
        // `WindowsApps` match would wrongly refuse these — the pair guards it.
        assert!(!is_alias(
            r"C:\Program Files\WindowsApps\SomeVendor.App\python.exe"
        ));
    }

    #[test]
    fn a_normal_interpreter_is_not_an_alias() {
        assert!(!is_alias(r"C:\Python312\python.exe"));
        assert!(!is_alias(r"C:\Users\alice\venv\Scripts\python.exe"));
    }

    #[test]
    fn case_is_ignored_on_both_components() {
        // Windows paths are case-insensitive; the drive may hand back any casing.
        assert!(is_alias(
            r"c:\users\x\appdata\local\microsoft\windowsapps\PYTHON3.EXE"
        ));
    }

    #[test]
    fn forward_slashes_are_matched_too() {
        // The env/PATH can carry either separator; both must split identically.
        assert!(is_alias("C:/opt/Local/Microsoft/WindowsApps/python3.exe"));
    }

    #[test]
    fn microsoft_without_windowsapps_is_not_an_alias() {
        // Microsoft appears all over Program Files; only the adjacency matters.
        assert!(!is_alias(r"C:\Program Files\Microsoft\dotnet\dotnet.exe"));
    }
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
        // A Windows path must survive untouched. inverted.
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
