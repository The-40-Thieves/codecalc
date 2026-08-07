//! Unix sandbox: rlimits applied in the child, process-group kill, wait4 rusage.

use std::io;
use std::os::unix::process::CommandExt;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

use super::{ResolvedLimits, Wait};

/// rlimits we set, paired with the value we want. Resolved in the PARENT so the
/// setrlimit calls in pre_exec cannot fail: each desired value is clamped to the
/// hard limit we already hold.
///
/// The previous code called setrlimit six times and discarded every return
/// value. A limit that EINVALs — which is exactly what happens when the soft
/// value exceeds the inherited hard ceiling, e.g. a 2 TiB RLIMIT_AS under a
/// restrictive `ulimit -v` — left the sandbox running with NO limit for that
/// resource while reporting success. A safety net that silently fails to attach
/// is worse than none, because it is indistinguishable from one that held.
#[derive(Clone, Copy, Default)]
pub struct Rlimits {
    cpu: u64,
    address_space: Option<u64>,
    fsize: u64,
    nofile: u64,
    nproc: u64,
}

/// The first argument of get/setrlimit is a glibc-specific enum and a plain
/// c_int everywhere else. The gate is target_ENV, not target_os: musl is also
/// `target_os = "linux"` but has no `__rlimit_resource_t`, so keying on the OS
/// compiled fine for gnu and broke both static musl builds — which are the
/// artifacts the README tells people to use on older machines.
#[cfg(all(target_os = "linux", target_env = "gnu"))]
type RlimitResource = libc::__rlimit_resource_t;
#[cfg(not(all(target_os = "linux", target_env = "gnu")))]
type RlimitResource = libc::c_int;

fn hard_limit(resource: RlimitResource) -> Option<u64> {
    let mut rl: libc::rlimit = unsafe { std::mem::zeroed() };
    if unsafe { libc::getrlimit(resource, &mut rl) } == 0 {
        Some(rl.rlim_max)
    } else {
        None
    }
}

/// Clamp `want` to the hard ceiling we actually hold. Returns (value, clamped?).
fn clamp(resource: RlimitResource, want: u64) -> (u64, bool) {
    match hard_limit(resource) {
        // RLIM_INFINITY means no ceiling, so anything we ask for is allowed.
        Some(hard) if hard != libc::RLIM_INFINITY && hard < want => (hard, true),
        _ => (want, false),
    }
}

pub fn resolve(limits: &ResolvedLimits) -> (Rlimits, Vec<&'static str>) {
    let mut unenforced = Vec::new();
    let (cpu, c) = clamp(libc::RLIMIT_CPU, limits.cpu_secs);
    if c {
        unenforced.push("cpu_limit_clamped_to_hard_rlimit");
    }
    let (fsize, c) = clamp(libc::RLIMIT_FSIZE, limits.fsize_bytes);
    if c {
        unenforced.push("file_size_limit_clamped_to_hard_rlimit");
    }
    let (nofile, _) = clamp(libc::RLIMIT_NOFILE, limits.nofile);
    let (nproc, _) = clamp(libc::RLIMIT_NPROC, limits.max_processes);

    // RLIMIT_AS on macOS: Darwin accepts the call but does not enforce address
    // space the way Linux does, and a large soft value can be rejected outright.
    // Setting it there buys an illusion, so it is skipped and reported instead —
    // `--max-memory-mb` is a Linux guarantee, not a portable one.
    let address_space = if cfg!(target_os = "macos") {
        unenforced.push("memory_limit_not_enforced_on_macos");
        None
    } else {
        let (v, c) = clamp(libc::RLIMIT_AS, limits.memory_bytes);
        if c {
            unenforced.push("memory_limit_clamped_to_hard_rlimit");
        }
        Some(v)
    };

    (Rlimits { cpu, address_space, fsize, nofile, nproc }, unenforced)
}

/// Runs in the child between fork and exec. Must be async-signal-safe: no
/// allocation, no locks, no I/O. Every value here was computed in the parent.
fn apply(r: &Rlimits) {
    unsafe {
        let set = |res: RlimitResource, v: u64| {
            let rl = libc::rlimit { rlim_cur: v, rlim_max: v };
            libc::setrlimit(res, &rl);
        };
        set(libc::RLIMIT_CPU, r.cpu);
        if let Some(as_bytes) = r.address_space {
            set(libc::RLIMIT_AS, as_bytes);
        }
        set(libc::RLIMIT_FSIZE, r.fsize);
        set(libc::RLIMIT_NOFILE, r.nofile);
        set(libc::RLIMIT_NPROC, r.nproc);
        set(libc::RLIMIT_CORE, 0);
    }
}

/// ru_maxrss is KiB on Linux and BYTES on macOS/BSD. Reading one as the other is
/// a silent 1024x error — and it does not merely report the wrong number: the MLE verdict
/// compares peak memory against the configured cap, so on macOS every signalled
/// exit looked like a memory kill.
fn maxrss_to_kb(ru_maxrss: i64) -> u64 {
    let v = ru_maxrss.max(0) as u64;
    if cfg!(any(target_os = "macos", target_os = "ios")) {
        v / 1024
    } else {
        v
    }
}

pub fn spawn_and_wait(mut cmd: Command, limits: &ResolvedLimits) -> io::Result<Wait> {
    let (rlimits, unenforced) = resolve(limits);
    cmd.process_group(0);
    unsafe {
        cmd.pre_exec(move || {
            apply(&rlimits);
            Ok(())
        });
    }
    let mut child: Child = cmd.spawn()?;

    let start = Instant::now();
    let mut status: libc::c_int = 0;
    let mut rusage: libc::rusage = unsafe { std::mem::zeroed() };
    let mut timed_out = false;
    let pid = child.id() as libc::pid_t;

    loop {
        let r = unsafe { libc::wait4(pid, &mut status, libc::WNOHANG, &mut rusage) };
        if r == pid {
            break;
        } else if r == -1 {
            let e = io::Error::last_os_error();
            if e.raw_os_error() != Some(libc::EINTR) {
                child.kill().ok();
                let _ = child.wait();
                return Err(e);
            }
        } else {
            if start.elapsed() >= Duration::from_secs(limits.timeout_secs) {
                // Kill the whole group: the child may have spawned its own tree,
                // and killing only the direct child orphans the rest.
                unsafe { libc::killpg(pid, libc::SIGKILL) };
                let _ = unsafe { libc::wait4(pid, &mut status, 0, &mut rusage) };
                timed_out = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    let (exit_code, signal) = if libc::WIFEXITED(status) {
        (libc::WEXITSTATUS(status) as i64, None)
    } else if libc::WIFSIGNALED(status) {
        (0, Some(libc::WTERMSIG(status)))
    } else {
        (status as i64, None)
    };

    // Each field is cast individually: time_t is i64 everywhere we care about,
    // but suseconds_t is i32 on macOS and i64 on Linux, so the mixed expression
    // does not even type-check on Darwin.
    let secs = rusage.ru_utime.tv_sec as i64 + rusage.ru_stime.tv_sec as i64;
    let usecs = rusage.ru_utime.tv_usec as i64 + rusage.ru_stime.tv_usec as i64;
    let cpu_ms = (secs * 1000 + usecs / 1000).max(0) as u64;

    Ok(Wait {
        exit_code,
        signal,
        timed_out,
        cpu_ms,
        peak_memory_kb: maxrss_to_kb(rusage.ru_maxrss as i64),
        unenforced,
    })
}

/// Total tasks (threads) owned by this real uid, machine-wide — the number the
/// kernel compares RLIMIT_NPROC against. Linux only: /proc is where that lives,
/// and macOS has no equivalent that is cheap to read, so it returns None there
/// and the caller falls back to a fixed ceiling.
pub fn current_uid_tasks() -> Option<u64> {
    if !cfg!(target_os = "linux") {
        return None;
    }
    let uid = unsafe { libc::getuid() };
    let mut total: u64 = 0;
    let mut seen_any = false;
    for entry in std::fs::read_dir("/proc").ok()?.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.is_empty() || !name.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        let Ok(status) = std::fs::read_to_string(format!("/proc/{name}/status")) else {
            continue; // exited between readdir and read
        };
        let mut this_uid: Option<u32> = None;
        let mut threads: Option<u64> = None;
        for line in status.lines() {
            if let Some(rest) = line.strip_prefix("Uid:") {
                this_uid = rest.split_whitespace().next().and_then(|v| v.parse().ok());
            } else if let Some(rest) = line.strip_prefix("Threads:") {
                threads = rest.trim().parse().ok();
            }
            if this_uid.is_some() && threads.is_some() {
                break;
            }
        }
        if this_uid == Some(uid) {
            seen_any = true;
            total += threads.unwrap_or(1);
        }
    }
    if seen_any { Some(total) } else { None }
}
