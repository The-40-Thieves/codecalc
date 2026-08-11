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

use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob,
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS, JOB_OBJECT_LIMIT_JOB_MEMORY,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    JOB_OBJECT_LIMIT_PROCESS_TIME, JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JobObjectBasicAccountingInformation, JobObjectExtendedLimitInformation,
    QueryInformationJobObject, SetInformationJobObject, TerminateJobObject,
};
use windows_sys::Win32::System::Threading::{
    CREATE_NO_WINDOW, GetCurrentProcess, GetExitCodeProcess, WaitForSingleObject,
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
        return None;
    }
    if in_job == 0 {
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
        return None;
    }
    Some(info.BasicLimitInformation.LimitFlags & JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK != 0)
}

pub fn spawn_and_wait(mut cmd: Command, limits: &ResolvedLimits) -> io::Result<Wait> {
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

    // CREATE_NO_WINDOW keeps console runtimes from flashing a window per run.
    cmd.creation_flags(CREATE_NO_WINDOW);
    let mut child: Child = cmd.spawn()?;
    let process: HANDLE = child.as_raw_handle() as HANDLE;

    // HONEST CAVEAT: the child is assigned to the job immediately after spawn
    // rather than being created suspended and assigned before its first
    // instruction. std::process::Command does not expose the initial thread
    // handle, so there is no supported way to ResumeThread it. The window is
    // microseconds and any process the child creates AFTER assignment inherits
    // the job, but a process spawned inside that window would escape the limits.
    // Closing it properly means dropping std::process::Command for a raw
    // CreateProcessW.
    if unsafe { AssignProcessToJobObject(job.0, process) } == 0 {
        let err = io::Error::last_os_error();
        let _ = child.kill();
        let _ = child.wait();
        return Err(err);
    }

    // ── THE-775: the ceiling is set; is it BINDING? ──────────────────────────
    //
    // Everything above can succeed and the process limit still not apply. Both
    // API calls return success, `nproc_limit()` puts the right number in the
    // job, and 400 of 400 children still spawn — measured on Windows 11 Pro
    // from two unrelated launchers, one of them Task Scheduler with no agent in
    // the parent chain.
    //
    // Neither check below explains WHY. They are not meant to: the root cause
    // has three candidate fixes with three different blast radii, and choosing
    // between them needs someone who can observe the result. What is correct
    // under every one of those hypotheses is that codecalc must not report a
    // ceiling it did not apply. So this DISCLOSES rather than repairs.
    //
    // The distinction matters to a caller more than the fix does. `400/400
    // spawned` with `ok=true` and nothing in `unenforced` is a security
    // guarantee silently absent. The same run with the ceiling declared
    // unapplied is a documented platform limitation — a completely different
    // thing to build on.
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
                limit_unverified =
                    Some("process_limit_not_enforced_ambient_job_allows_breakaway")
            }
            None => {
                limit_unverified = Some("process_limit_enforcement_unknown_on_windows")
            }
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
        let _ = child.wait();
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
