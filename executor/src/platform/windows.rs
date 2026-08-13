//! Windows sandbox: a Job Object per execution.
//!
//! Windows has no rlimits, no signals and no process groups in the POSIX sense,
//! so none of the Unix machinery ports. A Job Object is the right primitive and
//! in one respect a better one: `ActiveProcessLimit` is scoped to the JOB rather
//! than to the user, so the fork-bomb guard cannot be starved by unrelated
//! processes the way Unix's uid-wide RLIMIT_NPROC can.
//!
//! What is NOT available here, and is reported through `Wait::unenforced` rather
//! than quietly assumed:
//!   * CPU-time limit — the wall-clock timeout is the only time bound.
//!   * Open-file limit — no per-process equivalent.
//!   * File-size limit — output is capped when read instead.
//!   * `--no-net` — there is no LD_PRELOAD equivalent to hang a socket shim on.

use std::io;
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::process::{Child, Command};

use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE, WAIT_OBJECT_0};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob, JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_JOB_MEMORY, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY, JOB_OBJECT_LIMIT_PROCESS_TIME,
    JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectBasicAccountingInformation,
    JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
    TerminateJobObject,
};
use windows_sys::Win32::System::Threading::{
    CREATE_NO_WINDOW, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, CreateProcessW,
    DeleteProcThreadAttributeList, EXTENDED_STARTUPINFO_PRESENT, GetCurrentProcess,
    GetExitCodeProcess, InitializeProcThreadAttributeList, OpenThread,
    PROC_THREAD_ATTRIBUTE_JOB_LIST, PROCESS_INFORMATION, ResumeThread, STARTF_USESTDHANDLES,
    STARTUPINFOEXW, STARTUPINFOW, THREAD_SUSPEND_RESUME, UpdateProcThreadAttribute,
    WaitForSingleObject,
};

use super::{ResolvedLimits, Wait};

/// RAII wrapper so the job (and with KILL_ON_JOB_CLOSE, the whole process tree)
/// is torn down even on an early return.
struct Job(HANDLE);

impl Drop for Job {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) };
    }
}

fn create_job(limits: &ResolvedLimits) -> io::Result<Job> {
    let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
    if handle.is_null() {
        return Err(io::Error::last_os_error());
    }
    let job = Job(handle);

    let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
    // KILL_ON_JOB_CLOSE is what makes the timeout path reliable: dropping the
    // handle kills every process still in the job, so a child that spawned its
    // own tree cannot outlive us. This is the Windows answer to killpg().
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_PROCESS_TIME;

    // CPU ceiling. This was reported as `cpu_limit_unavailable_on_windows`,
    // which was simply wrong: JOB_OBJECT_LIMIT_PROCESS_TIME has existed since
    // Windows XP, and the documented behaviour is that the system periodically
    // checks each process in the job and TERMINATES one that has exceeded
    // PerProcessUserTimeLimit.
    //
    // It is not identical to RLIMIT_CPU and the difference is reported rather
    // than glossed: this counts USER-mode time only, so a process burning
    // kernel time is not capped by it, and the check is periodic rather than
    // immediate. Per-process, like RLIMIT_CPU — JOB_OBJECT_LIMIT_JOB_TIME would
    // cap the tree as a whole, which is a different guarantee.
    //
    // The field is a LARGE_INTEGER in 100-nanosecond ticks.
    const TICKS_PER_SECOND: i64 = 10_000_000;
    info.BasicLimitInformation.PerProcessUserTimeLimit = limits
        .cpu_secs
        .saturating_mul(TICKS_PER_SECOND as u64)
        .min(i64::MAX as u64) as i64;
    // Job-scoped, unlike RLIMIT_NPROC. Clamped to u32 because that is the field.
    info.BasicLimitInformation.ActiveProcessLimit =
        limits.max_processes.min(u32::MAX as u64) as u32;
    info.ProcessMemoryLimit = limits.memory_bytes.min(usize::MAX as u64) as usize;
    info.JobMemoryLimit = limits.memory_bytes.min(usize::MAX as u64) as usize;

    let ok = unsafe {
        SetInformationJobObject(
            job.0,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(job)
}

/// Does the job THIS process already belongs to allow children to break away?
///
/// Measured on Windows 11 Pro, from two unrelated launchers — an agent harness
/// and Task Scheduler, the second started by the Schedule service with no agent
/// anywhere in the parent chain — the sandboxed process reported:
///
///     IN_JOB True
///     LIMIT_FLAGS 0x00003000   = KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK
///     ACTIVE_PROCESS_LIMIT 0
///     SPAWNED 60               (unbounded; the ceiling was 24)
///
/// while `SetInformationJobObject` and `AssignProcessToJobObject` had BOTH
/// returned success. So the ceiling was set, the calls worked, and 400 of 400
/// spawns went through — with the process limit absent from `unenforced`. A
/// guarantee that is not there and does not say so is the one failure mode this
/// array exists to prevent.
///
/// `SILENT_BREAKAWAY_OK` on an ancestor job is the mechanism: it lets processes
/// in that job create children that are not associated with it. This asks the
/// question directly rather than inferring it from a failed spawn count, which
/// would cost a probe on every run.
///
/// Returns `None` when the question cannot be answered — not in a job, or the
/// query failed. `None` is not "enforced": it means unknown, and the caller
/// treats it as such.
fn ambient_job_allows_breakaway() -> Option<bool> {
    let mut in_job: i32 = 0;
    // NULL job handle = "the job this process is in", per the API contract.
    if unsafe { IsProcessInJob(GetCurrentProcess(), std::ptr::null_mut(), &mut in_job) } == 0 {
        diag("IsProcessInJob(self) FAILED -> returning None (unknown)");
        return None;
    }
    if in_job == 0 {
        diag("self is in NO job -> returning Some(false), no disclosure");
        // Not in a job at all: nothing above us can grant breakaway. This is the
        // shape the GitHub Server-SKU runner appears to have, and it is why CI
        // measured the ceiling binding at 23 of 400 while two real desktops did
        // not bind at all.
        return Some(false);
    }
    let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
    let mut ret: u32 = 0;
    let ok = unsafe {
        QueryInformationJobObject(
            std::ptr::null_mut(),
            JobObjectExtendedLimitInformation,
            &mut info as *mut _ as *mut core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            &mut ret,
        )
    };
    if ok == 0 {
        diag("QueryInformationJobObject(self) FAILED -> returning None (unknown)");
        return None;
    }
    let flags = info.BasicLimitInformation.LimitFlags;
    let silent = flags & JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK != 0;
    diag(&format!(
        "self IS in a job: LimitFlags=0x{:08X} APL={} SILENT_BREAKAWAY_OK={} -> Some({})",
        flags, info.BasicLimitInformation.ActiveProcessLimit, silent, silent
    ));
    Some(silent)
}

/// Append a line to the path in `CODECALC_DIAG_JOB`, or do nothing (THE-818).
///
/// A FILE rather than stderr, deliberately: the executor's stderr IS the
/// sandboxed program's stderr, and a diagnostic that contaminates the thing it
/// is measuring is the failure this whole investigation keeps re-finding. Off
/// unless the variable is set, so a normal run is byte-identical.
///
/// This exists because the question it answers is UNREACHABLE from outside the
/// process. `scripts/diag_windows_job.py` mirrors this function in ctypes, but
/// it runs in the DIAGNOSTIC, not in codecalc-exec — and if the diagnostic's
/// own job carries SILENT_BREAKAWAY_OK then the executor it spawns silently
/// breaks away and is in no job at all. Those two processes can therefore give
/// opposite answers, and only this one is the answer that matters.
fn diag(msg: &str) {
    let Ok(path) = std::env::var("CODECALC_DIAG_JOB") else {
        return;
    };
    if path.is_empty() {
        return;
    }
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
    {
        let _ = writeln!(f, "ambient_job_allows_breakaway: {msg}");
    }
}

/// Resume a process created with `CREATE_SUSPENDED`, given only its PID.
///
/// `std::process::Command` owns the child's pipes, environment, cwd and
/// argument quoting, and none of that is worth reimplementing — but it does not
/// expose the initial thread handle, which is what `ResumeThread` normally
/// wants. So the thread is found by snapshotting and filtering on the owning
/// PID. A process created suspended has exactly one thread, and it has not run,
/// so it cannot have made more.
///
/// Failing here is FATAL to the call rather than ignorable: a child left
/// suspended never exits, and `WaitForSingleObject` would sit on it until the
/// wall-clock timeout and report a TLE for a program that never ran a single
/// instruction. The caller kills it instead.
fn resume_process(pid: u32) -> io::Result<()> {
    let snap = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if snap == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    let mut entry: THREADENTRY32 = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;

    let mut resumed = 0usize;
    if unsafe { Thread32First(snap, &mut entry) } != 0 {
        loop {
            if entry.th32OwnerProcessID == pid {
                let thread = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
                if !thread.is_null() {
                    // (u32::MAX) is the documented failure value; anything else
                    // is the previous suspend count, which for a freshly
                    // CREATE_SUSPENDED thread is 1.
                    if unsafe { ResumeThread(thread) } != u32::MAX {
                        resumed += 1;
                    }
                    unsafe { CloseHandle(thread) };
                }
            }
            if unsafe { Thread32Next(snap, &mut entry) } == 0 {
                break;
            }
        }
    }
    unsafe { CloseHandle(snap) };

    if resumed == 0 {
        return Err(io::Error::other(format!(
            "created process {pid} suspended and could not resume it: no resumable thread found"
        )));
    }
    Ok(())
}

fn to_wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Create the child with `J` supplied at CREATION, so it is the IMMEDIATE job.
///
/// THE-818. Post-creation `AssignProcessToJobObject` puts us SOMEWHERE in the
/// child's job chain but not necessarily at its end, and `ActiveProcessLimit`
/// is not one of the limits combined across a chain — it comes from the
/// immediate job. Measured on Windows 11 Pro: the child's immediate job
/// reported `0x3000 / APL 0` while ours was `0x230A / APL 24`, and 400 of 400
/// spawns went through.
///
/// `PROC_THREAD_ATTRIBUTE_JOB_LIST` assigns before the initial thread runs and
/// puts `J` at the end of the chain, which is the only arrangement in which our
/// ceiling is the one consulted. Jobs are applied in the order supplied; we
/// supply exactly one.
///
/// OPT-IN, and deliberately so: this cannot be tested from the machine it was
/// written on, and the argv quoting above is the same class of bug that made
/// bash unusable on Windows for the project's whole life. Set
/// `CODECALC_WIN_JOB_AT_CREATION=1` to measure it; the default path is
/// unchanged until a Windows run says this one works.
#[allow(clippy::too_many_arguments)]
fn spawn_with_job_at_creation(
    cmd: &Command,
    stdio: super::RawStdio,
    job: HANDLE,
) -> io::Result<(HANDLE, HANDLE)> {
    // Command line: program then args, each quoted for the MSVC parser.
    let mut line = super::quote_arg(cmd.get_program());
    for a in cmd.get_args() {
        line.push(' ');
        line.push_str(&super::quote_arg(a));
    }
    let mut line_w = to_wide(&line);

    // Environment block: KEY=VALUE\0 ... \0. `Command` already holds exactly
    // the allowlist, so this inherits the CRITICAL-02 filtering rather than
    // rebuilding it.
    let mut env_w: Vec<u16> = Vec::new();
    for (k, v) in cmd.get_envs() {
        let Some(v) = v else { continue };
        env_w.extend(k.to_string_lossy().encode_utf16());
        env_w.push(u16::from(b'='));
        env_w.extend(v.to_string_lossy().encode_utf16());
        env_w.push(0);
    }
    env_w.push(0);

    let cwd_w = cmd.get_current_dir().map(|d| to_wide(&d.to_string_lossy()));

    // Two-call pattern: ask the size, allocate, initialise.
    let mut size: usize = 0;
    unsafe { InitializeProcThreadAttributeList(std::ptr::null_mut(), 1, 0, &mut size) };
    if size == 0 {
        return Err(io::Error::last_os_error());
    }
    let mut attr_buf = vec![0u8; size];
    let attr_list = attr_buf.as_mut_ptr() as *mut core::ffi::c_void;
    if unsafe { InitializeProcThreadAttributeList(attr_list, 1, 0, &mut size) } == 0 {
        return Err(io::Error::last_os_error());
    }

    // `job` must outlive the call; it is owned by the caller's Job guard.
    let job_handle = job;
    let ok = unsafe {
        UpdateProcThreadAttribute(
            attr_list,
            0,
            PROC_THREAD_ATTRIBUTE_JOB_LIST as usize,
            &job_handle as *const HANDLE as *const core::ffi::c_void,
            std::mem::size_of::<HANDLE>(),
            std::ptr::null_mut(),
            std::ptr::null(),
        )
    };
    if ok == 0 {
        let e = io::Error::last_os_error();
        unsafe { DeleteProcThreadAttributeList(attr_list) };
        return Err(e);
    }

    let mut si: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
    si.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    si.StartupInfo.hStdInput = stdio.stdin as HANDLE;
    si.StartupInfo.hStdOutput = stdio.stdout as HANDLE;
    si.StartupInfo.hStdError = stdio.stderr as HANDLE;
    si.lpAttributeList = attr_list;

    // Do not add CREATE_BREAKAWAY_FROM_JOB here. An ancestor carrying
    // SILENT_BREAKAWAY_OK already excludes the child automatically, while an
    // ordinary ancestor forms a supported nested chain with the supplied job
    // as the immediate one. Combining explicit breakaway with JOB_LIST caused
    // Windows 11 Pro to terminate the new process with ERROR_NOT_SUPPORTED
    // (0x80070032) before its program executed (THE-818).
    let flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT;

    let mut pi: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
    let created = unsafe {
        CreateProcessW(
            std::ptr::null(),
            line_w.as_mut_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            1, // bInheritHandles: required for STARTF_USESTDHANDLES
            flags,
            env_w.as_ptr() as *const core::ffi::c_void,
            cwd_w
                .as_ref()
                .map(|w| w.as_ptr())
                .unwrap_or(std::ptr::null()),
            &si as *const STARTUPINFOEXW as *const STARTUPINFOW,
            &mut pi,
        )
    };
    let err = io::Error::last_os_error();
    unsafe { DeleteProcThreadAttributeList(attr_list) };
    if created == 0 {
        return Err(err);
    }
    Ok((pi.hProcess, pi.hThread))
}

pub fn spawn_and_wait(
    mut cmd: Command,
    limits: &ResolvedLimits,
    stdio: super::RawStdio,
) -> io::Result<Wait> {
    // The CPU ceiling IS applied here (JOB_OBJECT_LIMIT_PROCESS_TIME, see
    // create_job) but it counts user-mode time only, so a caller comparing it
    // to RLIMIT_CPU is told what it does not cover rather than left to assume.
    let mut unenforced = vec![
        "cpu_limit_counts_user_time_only_on_windows",
        "open_file_limit_unavailable_on_windows",
        "file_size_limit_unavailable_on_windows",
    ];
    if limits.no_net {
        unenforced.push("no_net_unavailable_on_windows");
    }

    let job = create_job(limits)?;

    // THE CHILD IS CREATED SUSPENDED AND ASSIGNED BEFORE ITS FIRST INSTRUCTION.
    //
    // This used to spawn normally and assign immediately after, which left a
    // documented race: for the microseconds between `spawn()` returning and
    // `AssignProcessToJobObject`, the child was running outside the job, and
    // anything it spawned in that window escaped every limit. The comment here
    // said closing it meant dropping `std::process::Command` for a raw
    // `CreateProcessW`. It does not — `CREATE_SUSPENDED` plus a PID-scoped
    // thread resume keeps Command's pipes, env, cwd and argument quoting while
    // closing the window to zero.
    //
    // WHAT THIS DOES NOT DO: it does not fix THE-818. That failure is the
    // ceiling not binding at all under an ambient job carrying
    // SILENT_BREAKAWAY_OK, which is a different question from when the child
    // joins the job, and it has not been reproducible on any measured launcher
    // since. `suspended_assign` was the one candidate in jobprobe that bound
    // under every launcher measured and errored under none — that is why it is
    // safe to ship, not evidence that it repairs anything. The THE-775
    // disclosure below is untouched and still fires exactly when it did.
    //
    // CREATE_NO_WINDOW keeps console runtimes from flashing a window per run.
    // THE-818 opt-in: build the topology at creation instead of assigning
    // afterwards, so OUR job is the child's IMMEDIATE job and its
    // ActiveProcessLimit is the one consulted. Off by default until a Windows
    // run confirms it; see spawn_with_job_at_creation.
    let at_creation = std::env::var("CODECALC_WIN_JOB_AT_CREATION")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    // `Some` on the ordinary path (std owns the child), `None` on the
    // creation-time path (we own a raw handle). Both converge on `process`.
    let mut child: Option<Child> = None;
    let process: HANDLE;

    if at_creation {
        // Reported, never silently downgraded to the weaker path: a fallback
        // would put us back in the topology this exists to escape, while the
        // caller believed otherwise.
        let (p, thread) = spawn_with_job_at_creation(&cmd, stdio, job.0).map_err(|e| {
            io::Error::other(format!(
                "CODECALC_WIN_JOB_AT_CREATION=1 but creation-time job assignment failed: {e}"
            ))
        })?;
        unsafe { ResumeThread(thread) };
        unsafe { CloseHandle(thread) };
        unenforced.push("process_limit_job_assigned_at_creation_on_windows");
        process = p;
    } else {
        cmd.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);
        let spawned: Child = cmd.spawn()?;
        process = spawned.as_raw_handle() as HANDLE;
        child = Some(spawned);
    }

    if let Some(c) = child.as_mut() {
        if unsafe { AssignProcessToJobObject(job.0, process) } == 0 {
            let err = io::Error::last_os_error();
            // Kill rather than resume: a child that could not be placed in the
            // job must not be allowed to run at all, which is the whole point
            // of having created it suspended.
            let _ = c.kill();
            let _ = c.wait();
            return Err(err);
        }
        // Only now does the program get to execute. A failure here is fatal for
        // the reason resume_process documents — a suspended child would
        // otherwise be reported as a timeout having never run.
        if let Err(err) = resume_process(c.id()) {
            let _ = c.kill();
            let _ = c.wait();
            return Err(err);
        }
    }

    // ── THE-775: the ceiling is set; is it BINDING? ──────────────────────────
    //
    // Everything above can succeed and the process limit still not apply. Both
    // API calls return success, `nproc_limit()` puts the right number in the
    // job, and 400 of 400 children still spawn — measured on Windows 11 Pro
    // from two unrelated launchers, one of them Task Scheduler with no agent in
    // the parent chain.
    //
    // Neither check below explains WHY. They are not meant to: what is correct
    // under every hypothesis is that codecalc must not report a ceiling it did
    // not apply. So this DISCLOSES rather than repairs.
    //
    // The distinction matters to a caller more than the fix does. `400/400
    // spawned` with `ok=true` and nothing in `unenforced` is a security
    // guarantee silently absent. The same run with the ceiling declared
    // unapplied is a documented platform limitation — a completely different
    // thing to build on.
    //
    // ── AND NONE OF THE CHECKS BELOW CAN SEE THE FAILURE (THE-818) ───────────
    //
    // Measured on Windows 11 Pro with the executor instrumented via
    // CODECALC_DIAG_JOB, three processes in one spawn chain reported three
    // different job contexts:
    //
    //     the launcher (python)   LimitFlags 0x3000
    //     THIS PROCESS            LimitFlags 0x0     <- an EMPTY job
    //     the sandboxed child     LimitFlags 0x3000
    //     its grandchildren       no job at all
    //
    // So `ambient_job_allows_breakaway()` returns Some(false) CORRECTLY: this
    // process really is in a job that really does not permit breakaway. The
    // function is not wrong. It is standing in the wrong place — its ambient
    // job is unrelated to the job that governs the child, and there is no
    // parent-side Win32 call that returns another process's immediate job, its
    // chain, or its effective ActiveProcessLimit. Confirmed against the docs
    // and by cross-vendor review.
    //
    // ActiveProcessLimit is also NOT one of the limits combined across a nested
    // chain — those take the most restrictive value; the rest come from the
    // IMMEDIATE job. So the child's 0x3000/APL 0 governs and our 24 is never
    // consulted.
    //
    // Therefore INSPECT-THEN-DISCLOSE CANNOT BE MADE CORRECT HERE at any level
    // of effort, and the honest default is to say the ceiling is unverified
    // whenever it was applied by post-creation assignment. The checks below
    // still run: each can positively prove a failure, and a proof is worth more
    // than an admission. What none of them can do is prove SUCCESS, so their
    // silence no longer implies enforcement.
    //
    // Repairing this needs the topology built at creation time
    // (PROC_THREAD_ATTRIBUTE_JOB_LIST), not inspected afterwards. Until then
    // this line is the difference between a caller who knows and one who does
    // not.
    unenforced.push("process_limit_enforcement_unverified_on_windows");

    let mut limit_unverified: Option<&'static str> = None;

    // (1) Post-assignment membership. Cheap, direct, and the one thing that can
    // be asked about THIS child rather than about the environment.
    let mut in_our_job: i32 = 0;
    let queried = unsafe { IsProcessInJob(process, job.0, &mut in_our_job) };
    if queried == 0 {
        limit_unverified = Some("process_limit_membership_unverifiable_on_windows");
    } else if in_our_job == 0 {
        limit_unverified = Some("process_limit_not_enforced_child_escaped_the_job");
    }

    // (2) The precondition that was actually measured. An ancestor job carrying
    // SILENT_BREAKAWAY_OK lets processes create children outside it, which is
    // the observed shape on both desktops that failed. Checked even when (1)
    // says the child is in our job, because membership and enforcement turned
    // out not to be the same question — the child reported job flags 0x3000
    // while codecalc sets 0x230A.
    if limit_unverified.is_none() {
        match ambient_job_allows_breakaway() {
            Some(true) => {
                limit_unverified = Some("process_limit_not_enforced_ambient_job_allows_breakaway")
            }
            None => limit_unverified = Some("process_limit_enforcement_unknown_on_windows"),
            Some(false) => {}
        }
    }

    if let Some(reason) = limit_unverified {
        unenforced.push(reason);
    }

    let timeout_ms = limits
        .timeout_secs
        .saturating_mul(1000)
        .min(u32::MAX as u64) as u32;
    let waited = unsafe { WaitForSingleObject(process, timeout_ms) };
    let timed_out = waited != WAIT_OBJECT_0;

    if timed_out {
        // Kills every process in the job, not just the one we spawned.
        unsafe { TerminateJobObject(job.0, 1) };
        if let Some(c) = child.as_mut() {
            let _ = c.wait();
        }
    }

    let mut code: u32 = 0;
    unsafe { GetExitCodeProcess(process, &mut code) };

    // Accounting covers the whole job — every process the child spawned — which
    // is what we want, and is closer to Unix's wait4 rusage over a reaped tree.
    let mut acct: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { std::mem::zeroed() };
    let mut ret: u32 = 0;
    let cpu_ms = unsafe {
        if QueryInformationJobObject(
            job.0,
            JobObjectBasicAccountingInformation,
            &mut acct as *mut _ as *mut core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
            &mut ret,
        ) != 0
        {
            // Both are in 100-nanosecond ticks.
            ((acct.TotalUserTime as u64 + acct.TotalKernelTime as u64) / 10_000) as u64
        } else {
            0
        }
    };

    let mut ext: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
    let peak_memory_kb = unsafe {
        if QueryInformationJobObject(
            job.0,
            JobObjectExtendedLimitInformation,
            &mut ext as *mut _ as *mut core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            &mut ret,
        ) != 0
        {
            // PeakJobMemoryUsed is BYTES; Wait::peak_memory_kb is KiB.
            (ext.PeakJobMemoryUsed as u64) / 1024
        } else {
            0
        }
    };

    Ok(Wait {
        // Windows has no signals; an abnormal termination is just an exit code.
        exit_code: code as i64,
        signal: None,
        timed_out,
        cpu_ms,
        peak_memory_kb,
        unenforced,
    })
}

/// No uid-wide task budget exists on Windows, and none is needed: the job's
/// ActiveProcessLimit is already scoped per execution. Returning None makes the
/// caller use the fixed ceiling, which here is the actual per-job limit rather
/// than a share of a machine-wide pool.
pub fn current_uid_tasks() -> Option<u64> {
    None
}
