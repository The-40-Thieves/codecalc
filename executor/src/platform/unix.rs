//! Unix sandbox: rlimits applied in the child, process-group kill, wait4 rusage.

use std::io;
use std::os::unix::process::CommandExt;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

use std::sync::OnceLock;
use std::sync::atomic::{AtomicI32, Ordering};

use super::{ResolvedLimits, Wait};

/// PGID of the child currently running, or 0. Read by the signal handler.
///
/// PR_SET_PDEATHSIG (below) kills the direct child when this executor dies, but
/// NOT its descendants: verified — killing the executor reaped `python3` and
/// left its `sleep` grandchild reparented to init with no wall clock on it.
/// The grandchild IS in the child's process group, so killing that group covers
/// the whole tree — but only this process knows the pgid, so it has to do it.
static CHILD_PGID: AtomicI32 = AtomicI32::new(0);

/// Async-signal-safe. `kill` and `_exit` are on the POSIX async-signal-safe
/// list; `killpg` is NOT, which is why the negative-pid form of `kill` is used
/// here even though `killpg(pgid, ...)` reads better.
extern "C" fn terminate_child_group(sig: libc::c_int) {
    let pgid = CHILD_PGID.load(Ordering::SeqCst);
    // > 1, not > 0. kill(-1, SIGKILL) is not "kill process group 1" — POSIX
    // defines it as every process the caller may signal, which in a container
    // or a fresh PID namespace is the whole world including this executor.
    // A pgid of 1 should never happen; the cost of being wrong about that is
    // total, and the guard is one character.
    if pgid > 1 {
        unsafe { libc::kill(-pgid, libc::SIGKILL) };
    }
    unsafe { libc::_exit(128 + sig) };
}

/// The three signals the handler above covers.
fn term_signal_set() -> libc::sigset_t {
    unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut set);
        libc::sigaddset(&mut set, libc::SIGTERM);
        libc::sigaddset(&mut set, libc::SIGINT);
        libc::sigaddset(&mut set, libc::SIGHUP);
        set
    }
}

/// Block the termination signals and return the previous mask.
///
/// Without this there is a real window: the handler is installed before
/// `spawn()`, but CHILD_PGID cannot be published until `spawn()` returns a pid.
/// A SIGTERM landing in between finds CHILD_PGID == 0, kills nothing and exits
/// — and PR_SET_PDEATHSIG only reaches the direct child, so a grandchild that
/// had already been forked survives. That is precisely the leak this module
/// exists to close, so the window has to be shut rather than narrowed.
/// Blocking leaves the signal PENDING; it is delivered the moment the mask is
/// restored, by which time the pgid is published.
fn block_term_signals() -> libc::sigset_t {
    unsafe {
        let set = term_signal_set();
        let mut old: libc::sigset_t = std::mem::zeroed();
        libc::pthread_sigmask(libc::SIG_BLOCK, &set, &mut old);
        old
    }
}

fn restore_signal_mask(old: &libc::sigset_t) {
    unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, old, std::ptr::null_mut()) };
}

/// Kill the child's whole group if we are asked to stop. SIGKILL cannot be
/// caught, which is why PR_SET_PDEATHSIG stays as the backstop for that case.
fn install_termination_handler() {
    // Cast through a fn pointer, not the fn item: `fn_item as sighandler_t` is
    // a direct function-item-to-integer cast, which clippy rejects.
    let handler = terminate_child_group as extern "C" fn(libc::c_int);
    let h = handler as usize as libc::sighandler_t;
    unsafe {
        libc::signal(libc::SIGTERM, h);
        libc::signal(libc::SIGINT, h);
        libc::signal(libc::SIGHUP, h);
    }
}

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
    let (nproc, nproc_clamped) = clamp(libc::RLIMIT_NPROC, limits.max_processes);
    if nproc_clamped {
        unenforced.push("process_limit_clamped_to_hard_rlimit");
    }
    // The fork-bomb guard is sized from the MEASURED ambient task count on
    // Linux. Elsewhere there is no cheap equivalent, so it falls back to a fixed
    // ceiling — a weaker guarantee, and one a caller should be told about rather
    // than left to infer from the platform.
    if current_uid_tasks().is_none() {
        unenforced.push("process_limit_is_a_fixed_ceiling_not_measured");
    }
    // RLIMIT_NPROC does not bind a process whose EFFECTIVE uid is 0: the kernel
    // exempts privileged processes. So as root the ceiling is computed, set,
    // and has no effect, while the result said nothing about it — which reads
    // as "the process ceiling was applied".
    //
    // Running as root is a deployment error and this behaviour is documented
    // kernel semantics, so it is not an escape. It is a reporting-fidelity
    // defect, and the Python fallback carries the same entry so the two
    // backends agree about what they could not apply. scripts/check_parity.py
    // gates that they both have one.
    // SAFETY: geteuid() cannot fail and touches no memory.
    if unsafe { libc::geteuid() } == 0 {
        unenforced.push("process_limit_not_enforced_for_uid_0");
    }

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

    (
        Rlimits {
            cpu,
            address_space,
            fsize,
            nofile,
            nproc,
        },
        unenforced,
    )
}

/// Runs in the child between fork and exec. Must be async-signal-safe: no
/// allocation, no locks, no I/O. Every value here was computed in the parent.
fn apply(r: &Rlimits) {
    unsafe {
        let set = |res: RlimitResource, v: u64| {
            let rl = libc::rlimit {
                rlim_cur: v,
                rlim_max: v,
            };
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

pub fn spawn_and_wait(
    mut cmd: Command,
    limits: &ResolvedLimits,
    // Windows-only (THE-818): the creation-time job path needs the raw
    // handles. Unix has no equivalent problem and ignores this.
    _stdio: super::RawStdio,
) -> io::Result<Wait> {
    let (rlimits, unenforced) = resolve(limits);
    // Own process group, so a timeout can killpg the child's whole tree.
    cmd.process_group(0);
    unsafe {
        cmd.pre_exec(move || {
            apply(&rlimits);
            // PR_SET_PDEATHSIG: the kernel SIGKILLs this child if its parent —
            // this executor — dies. Without it the child is orphaned whenever
            // the executor is killed rather than exiting on its own, and it
            // then has NO wall clock on it at all: verified by killing the
            // executor and watching `sleep` keep running.
            //
            // The parent cannot solve this from outside, because process_group(0)
            // above deliberately puts the child in a DIFFERENT group, so a
            // killpg aimed at the executor's group never reaches it. Asking the
            // kernel is the only reliable answer.
            //
            // Linux only; other unices have no equivalent, and the caller's
            // timeout remains the backstop there.
            #[cfg(target_os = "linux")]
            {
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
                // Race: if the parent died between fork and here, we are already
                // orphaned and the signal will never come. Check and exit.
                if libc::getppid() == 1 {
                    libc::_exit(1);
                }
            }
            // The signal mask survives execve, so the child would otherwise
            // start life with SIGTERM blocked — it would ignore the very signal
            // used to stop it. sigprocmask is async-signal-safe and this side
            // of the fork is single-threaded.
            let mut empty: libc::sigset_t = std::mem::zeroed();
            libc::sigemptyset(&mut empty);
            libc::sigprocmask(libc::SIG_SETMASK, &empty, std::ptr::null_mut());
            Ok(())
        });
    }
    install_termination_handler();
    let saved_mask = block_term_signals();
    let spawned = cmd.spawn();
    let mut child: Child = match spawned {
        Ok(c) => c,
        Err(e) => {
            restore_signal_mask(&saved_mask);
            return Err(e);
        }
    };
    // process_group(0) makes the child its own group leader, so pgid == pid.
    CHILD_PGID.store(child.id() as i32, Ordering::SeqCst);
    restore_signal_mask(&saved_mask);

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
                // Same reasoning as terminate_child_group above: a pid of 1
                // would make this kill(-1, ...) and take the host with it.
                if pid > 1 {
                    unsafe { libc::kill(-pid, libc::SIGKILL) };
                }
                let _ = child.wait();
                // Clear before returning: a stale pgid here means a later
                // SIGTERM kills whatever process group has since taken that id.
                CHILD_PGID.store(0, Ordering::SeqCst);
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

    // #207: reap the whole group on a NORMAL exit too, not just a timeout.
    // The loop above only killpg'd on the timeout branch — a payload that
    // backgrounds a child (e.g. `subprocess.Popen(['sleep', '1000'])`) and
    // returns 0 hits neither branch, `wait4` reaps only the direct child, and
    // the grandchild — still in this same process group, since it never
    // called setsid itself — outlives the run with no wall clock on it at
    // all. Unconditional and best-effort: on the timeout path the group is
    // already empty, so this second killpg just returns ESRCH, which the
    // kernel does not even report back to a fire-and-forget call like this
    // one — same as every other kill in this file, the return value is not
    // checked. `pid > 1` guard for the same reason as terminate_child_group
    // above: a pid of 1 would turn this into `kill(-1, ...)`, which is not
    // "this group" but "every process the caller may signal".
    if pid > 1 {
        unsafe { libc::killpg(pid, libc::SIGKILL) };
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
    CHILD_PGID.store(0, Ordering::SeqCst);

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
/// Measured once per process. The walk reads /proc/<pid>/status for every
/// process on the machine — 590 of them on the box this was measured on — and
/// it was being repeated: once eagerly at startup, then again inside `resolve`
/// for EVERY step, plus a third time by the `is_none()` check below which threw
/// the count away. A single C compile-and-run opened 1767 status files to
/// answer one question three times.
///
/// Once per invocation is also what main.rs already claimed in a comment. The
/// executor is short-lived and spawns one child tree, so a snapshot taken at
/// the first request is the same snapshot every later caller wanted.
static UID_TASKS: OnceLock<Option<u64>> = OnceLock::new();

pub fn current_uid_tasks() -> Option<u64> {
    *UID_TASKS.get_or_init(measure_uid_tasks)
}

fn measure_uid_tasks() -> Option<u64> {
    if !cfg!(target_os = "linux") {
        return None;
    }
    use std::io::Read;

    let uid = unsafe { libc::getuid() };
    let mut total: u64 = 0;
    let mut seen_any = false;
    // Reused across every process, so the walk allocates once rather than
    // ~600 times. Capacity, not a ceiling — see the read below.
    let mut buf: Vec<u8> = Vec::with_capacity(4096);

    for entry in std::fs::read_dir("/proc").ok()?.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.is_empty() || !name.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        // read_to_end into a REUSED buffer, rather than read_to_string.
        //
        // /proc files report st_size == 0, so read_to_string cannot preallocate
        // and grows from scratch every time: measured at 7.65 reads per file,
        // 4631 reads for 597 processes. Reusing a Vec that already has capacity
        // keeps the syscall count down without allocating per process.
        //
        // NOT a single fixed-size read. A first version used one 4096-byte read
        // and was wrong twice over: `read` may legally return fewer bytes than
        // are available, and status can genuinely exceed 4 KiB — the kernel
        // emits the whole `Groups:` list before `Threads:`, and Linux allows up
        // to 65536 supplementary groups. On this machine `Threads:` sits at
        // byte 640 with 3456 bytes of headroom, so roughly 493 more groups
        // (routine on an LDAP/AD-joined host) would push it out of a 4 KiB
        // window. The process would then be counted as ONE task instead of its
        // real thread count, under-sizing RLIMIT_NPROC — and a truncated
        // `Threads:\t12345` can even parse as 123, which is worse than missing.
        let Ok(mut f) = std::fs::File::open(format!("/proc/{name}/status")) else {
            continue; // exited between readdir and open
        };
        buf.clear();
        if f.read_to_end(&mut buf).is_err() {
            continue; // exited mid-read
        }
        // Lossy, where read_to_string was strict: a task Name: may contain
        // bytes that are not valid UTF-8, and the old code skipped such a
        // process entirely rather than counting it. The replacement character
        // stays inside Name: and cannot disturb the ASCII fields parsed below.
        let status = String::from_utf8_lossy(&buf);

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
