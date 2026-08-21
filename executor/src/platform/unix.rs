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

/// `no_net` via a seccomp-bpf syscall filter (Linux). The LD_PRELOAD shim
/// (`blocknet.so`) only interposes libc SYMBOLS, so a dynamically-linked
/// `ctypes`/`dlsym` call or a raw `syscall` bypasses it — verified live in the
/// 2026-08-21 audit (E-1). A seccomp filter refuses, in the kernel, every
/// syscall-level way unprivileged code can open an `AF_INET`/`AF_INET6` socket:
/// `socket()` by domain, `io_uring` (which can open/connect a socket inside the
/// kernel without a `socket()`/`connect()` syscall), the x32 ABI and foreign-arch
/// compat range (see `seccomp::filter`). A raw `syscall` cannot get around it the
/// way it walks around the symbol shim, while `AF_UNIX` and other domains (runtimes
/// use them for internal plumbing) are left alone.
///
/// Installed in `pre_exec` alongside the rlimits, before `execve`. It needs
/// `NO_NEW_PRIVS` (so an unprivileged process may install it) and survives the
/// exec, applying to the caller's program for its whole life.
#[cfg(target_os = "linux")]
mod seccomp {
    // AUDIT_ARCH_* live in <linux/audit.h>, not the libc crate: EM value |
    // __AUDIT_ARCH_64BIT (0x8000_0000) | __AUDIT_ARCH_LE (0x4000_0000).
    #[cfg(target_arch = "x86_64")]
    const NATIVE_AUDIT_ARCH: u32 = 0xC000_003E; // EM_X86_64 = 62
    #[cfg(target_arch = "aarch64")]
    const NATIVE_AUDIT_ARCH: u32 = 0xC000_00B7; // EM_AARCH64 = 183

    // seccomp_data offsets (seccomp(2)): nr @0, arch @4, args[0] @16. Both
    // supported arches are little-endian, so a socket domain (a small int)
    // sits in the low 32-bit word loaded at offset 16.
    const OFF_NR: u32 = 0;
    const OFF_ARCH: u32 = 4;
    const OFF_ARG0: u32 = 16;
    const SECCOMP_RET_DATA: u32 = 0x0000_ffff; // errno lives in the low 16 bits

    #[inline]
    fn stmt(code: u16, k: u32) -> libc::sock_filter {
        libc::sock_filter {
            code,
            jt: 0,
            jf: 0,
            k,
        }
    }
    #[inline]
    fn jeq(k: u32, jt: u8, jf: u8) -> libc::sock_filter {
        libc::sock_filter {
            code: (libc::BPF_JMP | libc::BPF_JEQ | libc::BPF_K) as u16,
            jt,
            jf,
            k,
        }
    }

    /// Close EVERY unprivileged path that creates an `AF_INET`/`AF_INET6` socket,
    /// not just the `socket()` syscall — a raw syscall can reach the network by
    /// other doors that a naive socket-only filter leaves open (found in review):
    ///   - `socket(AF_INET|AF_INET6, …)`            → EACCES
    ///   - `io_uring_setup`/`enter`/`register`       → ENOSYS. io_uring dispatches
    ///     `IORING_OP_SOCKET`/`IORING_OP_CONNECT` INSIDE the kernel, so seccomp
    ///     never sees a `socket`/`connect` syscall for them; refusing ring
    ///     creation is the only syscall-level way to stop it. ENOSYS (not EACCES)
    ///     so a runtime that merely probes io_uring falls back to epoll cleanly.
    ///   - the x32/compat syscall range (`nr >= 0x4000_0000`) → KILL. An x32
    ///     process carries `arch == AUDIT_ARCH_X86_64` yet issues `socket` as
    ///     `__X32_SYSCALL_BIT | 41`, which would miss the `SYS_socket` check
    ///     below; no valid native syscall on either supported arch is that high.
    ///   - a foreign `arch` (32-bit-compat `socketcall`) → KILL.
    ///
    /// Everything else — `AF_UNIX`, `AF_NETLINK` (NSS), `socketpair`, `connect`,
    /// and every non-`socket` syscall — is allowed.
    fn filter() -> Vec<libc::sock_filter> {
        let ld = (libc::BPF_LD | libc::BPF_W | libc::BPF_ABS) as u16;
        let ret = (libc::BPF_RET | libc::BPF_K) as u16;
        let jge = (libc::BPF_JMP | libc::BPF_JGE | libc::BPF_K) as u16;
        vec![
            stmt(ld, OFF_ARCH),            // 0: A = arch
            jeq(NATIVE_AUDIT_ARCH, 0, 13), // 1: arch != native      -> KILL (15)
            stmt(ld, OFF_NR),              // 2: A = nr
            libc::sock_filter {
                code: jge,
                jt: 11,
                jf: 0,
                k: 0x4000_0000,
            }, // 3: x32/compat bit -> KILL (15)
            jeq(libc::SYS_io_uring_setup as u32, 7, 0), // 4: io_uring_setup       -> ENOSYS (12)
            jeq(libc::SYS_io_uring_enter as u32, 6, 0), // 5: io_uring_enter       -> ENOSYS (12)
            jeq(libc::SYS_io_uring_register as u32, 5, 0), // 6: io_uring_register    -> ENOSYS (12)
            jeq(libc::SYS_socket as u32, 0, 6), // 7: not socket           -> ALLOW (14)
            stmt(ld, OFF_ARG0),            // 8: A = domain (args[0], low word)
            jeq(libc::AF_INET as u32, 3, 0), // 9: AF_INET              -> EACCES (13)
            jeq(libc::AF_INET6 as u32, 2, 0), // 10: AF_INET6            -> EACCES (13)
            stmt(ret, libc::SECCOMP_RET_ALLOW), // 11: other domain        -> allow
            stmt(
                ret,
                libc::SECCOMP_RET_ERRNO | (libc::ENOSYS as u32 & SECCOMP_RET_DATA),
            ), // 12: ENOSYS (io_uring)
            stmt(
                ret,
                libc::SECCOMP_RET_ERRNO | (libc::EACCES as u32 & SECCOMP_RET_DATA),
            ), // 13: EACCES (inet)
            stmt(ret, libc::SECCOMP_RET_ALLOW), // 14: ALLOW (non-socket)
            stmt(ret, libc::SECCOMP_RET_KILL_PROCESS), // 15: KILL (foreign arch / x32)
        ]
    }

    /// Is the kernel able to take a seccomp filter at all? `PR_GET_SECCOMP`
    /// returns the current mode (>= 0) when seccomp is compiled in, or fails
    /// with EINVAL when it is not. Probed in the PARENT, before fork, so the
    /// result decides both the child's path and the reported enforcement.
    pub fn available() -> bool {
        // PR_GET_SECCOMP == 21; not exported by the libc crate on every target.
        unsafe { libc::prctl(21) >= 0 }
    }

    /// Build the program in the parent (allocation is not async-signal-safe);
    /// the child only installs the already-built bytes.
    pub fn program() -> Vec<libc::sock_filter> {
        filter()
    }

    /// Async-signal-safe: two `prctl` calls, no allocation. `prog` must outlive
    /// this call — it is built in the parent and copy-on-write inherited, so its
    /// pointer is valid in the child's address space. Returns Err if the kernel
    /// refuses the filter; the caller turns that into a failed spawn, so a
    /// requested `no_net` that cannot be enforced never runs (fail-closed).
    ///
    /// # Safety
    /// Runs between fork and execve in a child that must stay async-signal-safe.
    pub unsafe fn install(prog: &[libc::sock_filter]) -> std::io::Result<()> {
        let fprog = libc::sock_fprog {
            len: prog.len() as u16,
            filter: prog.as_ptr() as *mut libc::sock_filter,
        };
        // SAFETY: prctl is async-signal-safe; NO_NEW_PRIVS must precede an
        // unprivileged filter install; `fprog` points at `prog`, which the
        // caller guarantees outlives this call.
        let rc = unsafe {
            if libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            libc::prctl(
                libc::PR_SET_SECCOMP,
                libc::SECCOMP_MODE_FILTER as libc::c_ulong,
                &fprog as *const libc::sock_fprog as libc::c_ulong,
            )
        };
        if rc != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
    }
}

pub fn spawn_and_wait(
    mut cmd: Command,
    limits: &ResolvedLimits,
    // Windows-only: the creation-time job path needs the raw
    // handles. Unix has no equivalent problem and ignores this.
    _stdio: super::RawStdio,
) -> io::Result<Wait> {
    let (rlimits, unenforced) = resolve(limits);
    // Own process group, so a timeout can killpg the child's whole tree.
    cmd.process_group(0);

    // no_net enforcement (Linux): build the seccomp program in the PARENT, since
    // the child (pre_exec) must be async-signal-safe and cannot allocate. Only
    // when the kernel supports seccomp; otherwise the LD_PRELOAD shim applied by
    // the caller is the (best-effort) fallback and this stays false.
    #[cfg(target_os = "linux")]
    let net_filter: Option<Vec<libc::sock_filter>> = if limits.no_net && seccomp::available() {
        Some(seccomp::program())
    } else {
        None
    };
    #[cfg(target_os = "linux")]
    let no_net_seccomp_enforced = net_filter.is_some();
    #[cfg(not(target_os = "linux"))]
    let no_net_seccomp_enforced = false;

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
            // no_net: install the seccomp filter LAST — after our own setup
            // syscalls above, so none of them is ever subject to it — and
            // fail-closed: a requested no_net the kernel refuses to enforce
            // fails the spawn rather than running the caller's code unprotected.
            #[cfg(target_os = "linux")]
            if let Some(ref prog) = net_filter {
                seccomp::install(prog)?;
            }
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
        no_net_seccomp_enforced,
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
