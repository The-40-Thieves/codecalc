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
use std::path::{Path, PathBuf};
use std::process::{Child, Command};

use windows_sys::Win32::Foundation::{
    CloseHandle, DUPLICATE_SAME_ACCESS, DuplicateHandle, HANDLE, INVALID_HANDLE_VALUE, LocalFree,
    WAIT_OBJECT_0,
};
use windows_sys::Win32::Security::Authorization::{
    EXPLICIT_ACCESS_W, GRANT_ACCESS, GetNamedSecurityInfoW, NO_MULTIPLE_TRUSTEE, SE_FILE_OBJECT,
    SetEntriesInAclW, SetNamedSecurityInfoW, TRUSTEE_IS_SID, TRUSTEE_IS_UNKNOWN,
};
use windows_sys::Win32::Security::Isolation::{
    CreateAppContainerProfile, DeleteAppContainerProfile, DeriveAppContainerSidFromAppContainerName,
};
use windows_sys::Win32::Security::{
    ACL, DACL_SECURITY_INFORMATION, FreeSid, PSECURITY_DESCRIPTOR, PSID, SECURITY_CAPABILITIES,
    SUB_CONTAINERS_AND_OBJECTS_INHERIT,
};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob, JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_JOB_MEMORY, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY, JOB_OBJECT_LIMIT_PROCESS_TIME,
    JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK, JOB_OBJECT_UILIMIT_EXITWINDOWS,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_BASIC_UI_RESTRICTIONS,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectBasicAccountingInformation,
    JobObjectBasicUIRestrictions, JobObjectExtendedLimitInformation, QueryInformationJobObject,
    SetInformationJobObject, TerminateJobObject,
};
use windows_sys::Win32::System::Threading::{
    CREATE_BREAKAWAY_FROM_JOB, CREATE_NO_WINDOW, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT,
    CreateProcessW, DeleteProcThreadAttributeList, EXTENDED_STARTUPINFO_PRESENT, GetCurrentProcess,
    GetExitCodeProcess, InitializeProcThreadAttributeList, OpenThread,
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST, PROC_THREAD_ATTRIBUTE_JOB_LIST,
    PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, PROCESS_INFORMATION, ResumeThread,
    STARTF_USESTDHANDLES, STARTUPINFOEXW, STARTUPINFOW, THREAD_SUSPEND_RESUME,
    UpdateProcThreadAttribute, WaitForSingleObject,
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

/// Child-side copies of stdin/stdout/stderr for raw `CreateProcessW`.
///
/// Rust opens `File` handles as non-inheritable. `bInheritHandles=TRUE` only
/// copies handles that already carry the inherit bit, so passing the originals
/// in `STARTF_USESTDHANDLES` leaves the child with invalid standard handles.
struct InheritableStdio([HANDLE; 3]);

impl InheritableStdio {
    fn duplicate(stdio: super::RawStdio) -> io::Result<Self> {
        let process = unsafe { GetCurrentProcess() };
        let sources = [
            stdio.stdin as HANDLE,
            stdio.stdout as HANDLE,
            stdio.stderr as HANDLE,
        ];
        let mut handles = [std::ptr::null_mut(); 3];
        for (index, source) in sources.into_iter().enumerate() {
            if unsafe {
                DuplicateHandle(
                    process,
                    source,
                    process,
                    &mut handles[index],
                    0,
                    1,
                    DUPLICATE_SAME_ACCESS,
                )
            } == 0
            {
                let error = io::Error::last_os_error();
                for handle in handles.into_iter().take(index) {
                    unsafe { CloseHandle(handle) };
                }
                return Err(error);
            }
        }
        Ok(Self(handles))
    }
}

impl std::ops::Index<usize> for InheritableStdio {
    type Output = HANDLE;

    fn index(&self, index: usize) -> &Self::Output {
        &self.0[index]
    }
}

impl Drop for InheritableStdio {
    fn drop(&mut self) {
        for handle in self.0 {
            unsafe { CloseHandle(handle) };
        }
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

    // Windows only forms nested job hierarchies when neither job has UI
    // restrictions. EXITWINDOWS is harmless for this non-interactive executor
    // and makes our resource job terminal, so a launcher cannot hide the real
    // runtime behind a weaker immediate job with no ActiveProcessLimit.
    let ui = JOBOBJECT_BASIC_UI_RESTRICTIONS {
        UIRestrictionsClass: JOB_OBJECT_UILIMIT_EXITWINDOWS,
    };
    let ok = unsafe {
        SetInformationJobObject(
            job.0,
            JobObjectBasicUIRestrictions,
            &ui as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_BASIC_UI_RESTRICTIONS>() as u32,
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

/// Is `path` a Windows Store *app-execution alias* rather than a real program?
///
/// `%LOCALAPPDATA%\Microsoft\WindowsApps` holds zero-length reparse stubs that,
/// when executed, hand off to the Store activation broker. The broker starts the
/// real program OUTSIDE the caller's process tree and job object, so a runtime
/// resolved to one of these escapes EVERY Job-Object control the sandbox relies
/// on — the process ceiling, the memory caps, and the timeout tree-kill all go
/// silently void. This was the root cause of THE-818: a bare `python3` PATH-
/// resolved to `...\Microsoft\WindowsApps\python3.EXE`, and measured escape on a
/// real Windows 11 box was not a job-topology bug at all but this hand-off.
///
/// Detected two independent ways, either sufficient:
///   * the alias directory — `super::is_windowsapps_alias_path`, a `Microsoft`
///     component immediately followed by `WindowsApps`. That pure path check
///     lives in the cross-platform module so the Linux CI actually exercises it
///     (`Path::components` treats `\` as ordinary off-Windows — the THE-817
///     platform split a Windows-only test would silently miss).
///   * a zero-length stub — a real PE image is never 0 bytes, and the aliases
///     are, so this also catches an alias relocated outside the usual directory.
fn is_app_execution_alias(path: &Path) -> bool {
    super::is_windowsapps_alias_path(path)
        || std::fs::metadata(path).map(|m| m.len() == 0).unwrap_or(false)
}

/// Fail closed on a resolved app-execution alias with an actionable message.
///
/// Running untrusted code through the Store broker is strictly worse than not
/// running it: it executes fully UNCONFINED, outside the sandbox job. So the
/// resolver refuses rather than launch it, and tells the operator how to supply
/// a real interpreter instead. (THE-818.)
fn alias_refused_error(alias: &Path) -> io::Error {
    io::Error::new(
        io::ErrorKind::PermissionDenied,
        format!(
            "refusing to launch {}: it is a Windows Store app-execution alias that runs \
             outside the sandbox job object, defeating every resource limit. Install a real \
             interpreter (e.g. from python.org) or configure its absolute path.",
            alias.display()
        ),
    )
}

/// Resolve the program using the environment attached to `cmd`.
///
/// `CreateProcessW` performs executable lookup before applying the child's
/// environment block, so a null `lpApplicationName` searches the executor's
/// PATH rather than the deliberately configured sandbox PATH. Rust's normal
/// `Command` path resolves this for us; the raw creation-time path must do it.
///
/// A resolved Windows Store app-execution alias is refused (`is_app_execution_
/// alias`): it would broker-launch outside the job and escape the sandbox.
fn resolve_command_program(cmd: &Command) -> io::Result<PathBuf> {
    let requested = PathBuf::from(cmd.get_program());
    if requested.is_absolute() || requested.components().count() > 1 {
        if is_app_execution_alias(&requested) {
            return Err(alias_refused_error(&requested));
        }
        return requested.is_file().then_some(requested).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "configured executable does not exist",
            )
        });
    }

    let command_env = |name: &str| {
        cmd.get_envs().find_map(|(key, value)| {
            key.to_string_lossy()
                .eq_ignore_ascii_case(name)
                .then(|| value.map(|v| v.to_os_string()))
                .flatten()
        })
    };
    let path = command_env("PATH").unwrap_or_default();
    let pathext = command_env("PATHEXT")
        .unwrap_or_else(|| ".COM;.EXE;.BAT;.CMD".into())
        .to_string_lossy()
        .into_owned();
    let mut names = vec![requested.clone()];
    if requested.extension().is_none() {
        for extension in pathext.split(';').filter(|extension| !extension.is_empty()) {
            let mut name = requested.clone().into_os_string();
            name.push(extension);
            names.push(PathBuf::from(name));
        }
    }

    // A Store alias earlier on PATH must not shadow a real interpreter later on
    // it, so aliases are skipped rather than returned. The first one skipped is
    // remembered: if nothing real is found, the error names it and tells the
    // operator what to do, instead of a bare "not found" that hides the cause.
    let mut skipped_alias: Option<PathBuf> = None;
    for directory in std::env::split_paths(&path) {
        for name in &names {
            let candidate = directory.join(name);
            if candidate.is_file() {
                if is_app_execution_alias(&candidate) {
                    skipped_alias.get_or_insert(candidate);
                    continue;
                }
                return Ok(candidate);
            }
        }
    }
    if let Some(alias) = skipped_alias {
        return Err(alias_refused_error(&alias));
    }
    Err(io::Error::new(
        io::ErrorKind::NotFound,
        format!(
            "{} was not found on the configured runtime PATH",
            requested.display()
        ),
    ))
}

// ── THE-829: least-privilege AppContainer strict backend ─────────────────────
//
// OFF by default (CODECALC_WIN_APPCONTAINER=1 opts in) and IMPLEMENTED-BUT-
// UNVERIFIED on real Windows 11, exactly like THE-818's creation-time job
// topology it layers on. The isolation properties this is meant to provide — a
// payload that cannot read the user profile, cannot write outside the workdir,
// and gets no network — are only observable on a real Win11 desktop, which the
// author cannot run. So the code is written to be correct and to FAIL CLOSED,
// it cross-compiles for `x86_64-pc-windows-msvc`, and every run that takes this
// path emits `appcontainer_isolation_unverified_on_windows` in `unenforced`
// rather than any claim that the isolation was confirmed. Do not read a green
// CI run on the Server-SKU runner as verification of the Win11 behaviour: CI can
// at most show this compiles, runs, and discloses — never that isolation holds.

// GENERIC_* access masks. Kept as plain u32 literals (the field they feed,
// EXPLICIT_ACCESS_W::grfAccessPermissions, is a u32) so we do not drag in the
// GENERIC_ACCESS_RIGHTS newtype and its feature just to cast it straight back.
const GENERIC_READ: u32 = 0x8000_0000;
const GENERIC_EXECUTE: u32 = 0x2000_0000;
const GENERIC_ALL: u32 = 0x1000_0000;

/// HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS): the profile name is already
/// registered, which is not an error — we derive its SID and reuse it.
const HRESULT_ALREADY_EXISTS: i32 = 0x8007_00B7u32 as i32;

/// One execution's AppContainer profile, with deterministic cleanup.
///
/// Drop deletes the profile and frees the SID EVEN ON THE ERROR PATH — an
/// aborted launch must not leave a profile (or its ACL state) behind. The SID is
/// the one handed back by `CreateAppContainerProfile` /
/// `DeriveAppContainerSidFromAppContainerName`, both of which document `FreeSid`
/// as the matching free.
struct AppContainer {
    name_w: Vec<u16>,
    sid: PSID,
}

impl Drop for AppContainer {
    fn drop(&mut self) {
        // Delete first, then free: DeleteAppContainerProfile takes the NAME, not
        // the SID, so ordering is independent, but freeing last keeps the SID
        // valid for the whole teardown.
        unsafe { DeleteAppContainerProfile(self.name_w.as_ptr()) };
        if !self.sid.is_null() {
            unsafe { FreeSid(self.sid) };
        }
    }
}

/// Create (or reuse) a least-privilege AppContainer profile with NO capability
/// SIDs — no network, no user-profile access, nothing but what an explicit ACL
/// later grants. Returns `Err` on any failure so the caller can FAIL CLOSED;
/// there is deliberately no unconfined fallback.
fn create_appcontainer() -> io::Result<AppContainer> {
    use std::sync::atomic::{AtomicU64, Ordering};
    // Unique per execution: pid + a process-local counter + wall-clock nanos.
    // AppContainer names are capped at 64 UTF-16 units, so this stays short.
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let name: String = format!("codecalc.exec.{}.{n}.{nanos}", std::process::id())
        .chars()
        .take(64)
        .collect();
    let name_w = to_wide(&name);
    let display_w = to_wide("codecalc executor sandbox");
    let desc_w = to_wide("codecalc least-privilege execution AppContainer (THE-829, unverified)");

    let mut sid: PSID = std::ptr::null_mut();
    let hr = unsafe {
        CreateAppContainerProfile(
            name_w.as_ptr(),
            display_w.as_ptr(),
            desc_w.as_ptr(),
            // No capabilities: the container gets no network capability SID (or
            // any other), so network is denied by default — modelled as an
            // explicit capability that is simply never granted here.
            std::ptr::null(),
            0,
            &mut sid,
        )
    };
    if hr != 0 {
        if hr == HRESULT_ALREADY_EXISTS {
            // Reuse: derive the SID for the profile that already exists.
            let hr2 =
                unsafe { DeriveAppContainerSidFromAppContainerName(name_w.as_ptr(), &mut sid) };
            if hr2 != 0 {
                return Err(io::Error::other(format!(
                    "DeriveAppContainerSidFromAppContainerName failed: HRESULT 0x{:08X}",
                    hr2 as u32
                )));
            }
        } else {
            return Err(io::Error::other(format!(
                "CreateAppContainerProfile failed: HRESULT 0x{:08X}",
                hr as u32
            )));
        }
    }
    if sid.is_null() {
        // Profile call reported success but gave no SID: delete what we may have
        // created and refuse rather than launch unconfined.
        unsafe { DeleteAppContainerProfile(name_w.as_ptr()) };
        return Err(io::Error::other("AppContainer profile returned a null SID"));
    }
    Ok(AppContainer { name_w, sid })
}

/// ADD an allow-ACE for `sid` on `path`, preserving the directory's existing
/// ACL. Additive on purpose: the executor still has to write into and later
/// delete a temp workdir, and inherited ACEs (the operator's own access) must
/// survive — replacing the DACL wholesale would strip both. So the current DACL
/// is read, the one entry merged in, and the result written back unprotected.
fn grant_sid_path_access(sid: PSID, path: &std::path::Path, access: u32) -> io::Result<()> {
    let path_w = to_wide(&path.to_string_lossy());

    // 1. Read the existing DACL (and the security descriptor that backs it).
    let mut old_dacl: *mut ACL = std::ptr::null_mut();
    let mut sd: PSECURITY_DESCRIPTOR = std::ptr::null_mut();
    let rc = unsafe {
        GetNamedSecurityInfoW(
            path_w.as_ptr(),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut old_dacl,
            std::ptr::null_mut(),
            &mut sd,
        )
    };
    if rc != 0 {
        return Err(io::Error::from_raw_os_error(rc as i32));
    }
    // The descriptor is LocalAlloc'd; free it however we leave this function.
    struct LocalMem(*mut core::ffi::c_void);
    impl Drop for LocalMem {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe { LocalFree(self.0) };
            }
        }
    }
    let _sd_guard = LocalMem(sd);

    // 2. Describe the single ACE: this SID, this access, inherited by the files
    // and subdirectories the payload creates underneath.
    let mut ea: EXPLICIT_ACCESS_W = unsafe { std::mem::zeroed() };
    ea.grfAccessPermissions = access;
    ea.grfAccessMode = GRANT_ACCESS;
    ea.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT;
    ea.Trustee.MultipleTrusteeOperation = NO_MULTIPLE_TRUSTEE;
    ea.Trustee.pMultipleTrustee = std::ptr::null_mut();
    ea.Trustee.TrusteeForm = TRUSTEE_IS_SID;
    ea.Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN;
    // For TRUSTEE_IS_SID, ptstrName IS the PSID, reinterpreted as the API wants.
    ea.Trustee.ptstrName = sid as *mut u16;

    // 3. Merge the ACE into a NEW acl built on top of the existing one.
    let mut new_dacl: *mut ACL = std::ptr::null_mut();
    let rc = unsafe { SetEntriesInAclW(1, &ea, old_dacl, &mut new_dacl) };
    if rc != 0 {
        return Err(io::Error::from_raw_os_error(rc as i32));
    }
    let _new_dacl_guard = LocalMem(new_dacl as *mut core::ffi::c_void);

    // 4. Write it back. DACL_SECURITY_INFORMATION without the PROTECTED flag
    // means "merge with inheritance", so this ADDS the ACE rather than replacing
    // the object's protection.
    let rc = unsafe {
        SetNamedSecurityInfoW(
            path_w.as_ptr(),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            new_dacl as *const ACL,
            std::ptr::null(),
        )
    };
    if rc != 0 {
        return Err(io::Error::from_raw_os_error(rc as i32));
    }
    Ok(())
}

/// Build the least-privilege AppContainer, grant it ONLY the sandbox workdir
/// (read/write) plus read+execute on the resolved runtime's own directory, and
/// return the profile guard together with a boxed `SECURITY_CAPABILITIES` whose
/// pointer is handed to `spawn_with_job_at_creation`.
///
/// The box's address is stable and both it and the profile are kept alive by the
/// caller for the whole spawn+wait, because `CreateProcessW` reads the struct
/// (and the SID it points at) during creation. FAILS CLOSED: any error here
/// aborts the launch instead of dropping to an unconfined process.
fn prepare_appcontainer(cmd: &Command) -> io::Result<(AppContainer, Box<SECURITY_CAPABILITIES>)> {
    let ac = create_appcontainer()?;

    // The one directory the payload may write: its sandbox workdir. Full access,
    // inherited by anything it creates inside. Errors are labelled so a
    // FAIL-CLOSED launch is attributable to the appcontainer ACL path.
    if let Some(work) = cmd.get_current_dir() {
        grant_sid_path_access(ac.sid, work, GENERIC_ALL)
            .map_err(|e| io::Error::other(format!("appcontainer workdir ACL grant failed: {e}")))?;
    }
    // Read-only runtime assets: the interpreter/compiler must be able to load
    // itself. Grant read+execute on its own directory and NOTHING else — not the
    // user profile, not the rest of the disk. Comprehensive per-asset grants
    // (e.g. a full interpreter install tree) are a Win11-box verification item.
    if let Ok(program) = resolve_command_program(cmd)
        && let Some(dir) = program.parent()
    {
        grant_sid_path_access(ac.sid, dir, GENERIC_READ | GENERIC_EXECUTE).map_err(|e| {
            io::Error::other(format!("appcontainer runtime-dir ACL grant failed: {e}"))
        })?;
    }

    let caps = Box::new(SECURITY_CAPABILITIES {
        AppContainerSid: ac.sid,
        Capabilities: std::ptr::null_mut(),
        CapabilityCount: 0,
        Reserved: 0,
    });
    Ok((ac, caps))
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
/// Enabled by default after a direct Python runtime bound at 23/400 against a
/// limit of 24 on Windows 11 Pro. Set CODECALC_WIN_JOB_AT_CREATION=0 only as a
/// compatibility escape hatch; that old topology remains explicitly
/// unverified.
/// `sec_caps`, when `Some`, adds `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`
/// as a THIRD attribute in the SAME list, so the child is launched inside the
/// AppContainer (THE-829) while still being assigned to `job` at creation
/// (THE-818). The pointer and the struct it addresses must outlive this call —
/// the caller owns both. `None` reproduces the two-attribute THE-818 topology
/// exactly.
#[allow(clippy::too_many_arguments)]
fn spawn_with_job_at_creation(
    cmd: &Command,
    stdio: super::RawStdio,
    job: HANDLE,
    breakaway: bool,
    sec_caps: Option<*const SECURITY_CAPABILITIES>,
) -> io::Result<(HANDLE, HANDLE)> {
    let inheritable_stdio = InheritableStdio::duplicate(stdio)?;
    let program = resolve_command_program(cmd)?;
    let program_w = to_wide(&program.to_string_lossy());

    // Command line: program then args, each quoted for the MSVC parser.
    let mut line = super::quote_arg(program.as_os_str());
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

    // Attribute count: JOB_LIST + HANDLE_LIST always, plus SECURITY_CAPABILITIES
    // when the AppContainer path (THE-829) supplied it. All live in ONE list.
    let attr_count: u32 = if sec_caps.is_some() { 3 } else { 2 };

    // Two-call pattern: ask the size, allocate, initialise.
    let mut size: usize = 0;
    unsafe { InitializeProcThreadAttributeList(std::ptr::null_mut(), attr_count, 0, &mut size) };
    if size == 0 {
        return Err(io::Error::last_os_error());
    }
    let mut attr_buf = vec![0u8; size];
    let attr_list = attr_buf.as_mut_ptr() as *mut core::ffi::c_void;
    if unsafe { InitializeProcThreadAttributeList(attr_list, attr_count, 0, &mut size) } == 0 {
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

    // bInheritHandles must be TRUE for STARTF_USESTDHANDLES. Restrict that
    // inheritance to these child-side duplicates so files, sockets, tokens and
    // the job handle itself cannot leak from the executor into submitted code.
    let inherited_handles = [
        inheritable_stdio[0],
        inheritable_stdio[1],
        inheritable_stdio[2],
    ];
    let ok = unsafe {
        UpdateProcThreadAttribute(
            attr_list,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
            inherited_handles.as_ptr() as *const core::ffi::c_void,
            std::mem::size_of_val(&inherited_handles),
            std::ptr::null_mut(),
            std::ptr::null(),
        )
    };
    if ok == 0 {
        let e = io::Error::last_os_error();
        unsafe { DeleteProcThreadAttributeList(attr_list) };
        return Err(e);
    }

    // THE-829: the AppContainer's SECURITY_CAPABILITIES, in the same list. When
    // absent this is skipped and the topology is byte-for-byte the THE-818 one.
    if let Some(caps) = sec_caps {
        let ok = unsafe {
            UpdateProcThreadAttribute(
                attr_list,
                0,
                PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES as usize,
                caps as *const core::ffi::c_void,
                std::mem::size_of::<SECURITY_CAPABILITIES>(),
                std::ptr::null_mut(),
                std::ptr::null(),
            )
        };
        if ok == 0 {
            let e = io::Error::last_os_error();
            unsafe { DeleteProcThreadAttributeList(attr_list) };
            return Err(e);
        }
    }

    let mut si: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
    si.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    si.StartupInfo.hStdInput = inheritable_stdio[0];
    si.StartupInfo.hStdOutput = inheritable_stdio[1];
    si.StartupInfo.hStdError = inheritable_stdio[2];
    si.lpAttributeList = attr_list;

    let mut flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT;
    if breakaway {
        // Needed when this process is itself in a job that would otherwise
        // capture the child. FAILS with ERROR_ACCESS_DENIED if the ancestor
        // does not permit it — which is reported, not swallowed.
        flags |= CREATE_BREAKAWAY_FROM_JOB;
    }

    let mut pi: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
    let created = unsafe {
        CreateProcessW(
            program_w.as_ptr(),
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
    diag(&format!(
        "spawn_with_job_at_creation: created PID={} breakaway={breakaway}",
        pi.dwProcessId
    ));
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
    // THE-818: build the topology at creation so OUR job is the child's
    // IMMEDIATE job and its ActiveProcessLimit is the one consulted. The old
    // post-creation route remains only as an explicit compatibility escape.
    // THE-829: the AppContainer strict backend, OFF by default and UNVERIFIED on
    // real Windows 11. It REQUIRES the creation-time path (it rides the same
    // STARTUPINFOEX attribute list), so requesting it forces `at_creation`.
    let appcontainer = std::env::var("CODECALC_WIN_APPCONTAINER")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    let at_creation = appcontainer
        || std::env::var("CODECALC_WIN_JOB_AT_CREATION")
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(true);
    // `Some` on the ordinary path (std owns the child), `None` on the
    // creation-time path (we own a raw handle). Both converge on `process`.
    let mut child: Option<Child> = None;
    let process: HANDLE;
    // AppContainer resources kept alive for the WHOLE call: CreateProcessW reads
    // the SECURITY_CAPABILITIES (and the SID it points at) during creation, and
    // the profile's Drop runs deterministic cleanup after the child has exited.
    let mut appcontainer_guard: Option<AppContainer> = None;
    let mut caps_box: Option<Box<SECURITY_CAPABILITIES>> = None;

    if at_creation {
        let breakaway = ambient_job_allows_breakaway().unwrap_or(false);
        let mut sec_caps_ptr: Option<*const SECURITY_CAPABILITIES> = None;
        if appcontainer {
            // Implemented but UNVERIFIED on real Windows 11 — disclosed on every
            // run that takes this path, never claimed as confirmed isolation.
            unenforced.push("appcontainer_isolation_unverified_on_windows");
            // FAIL CLOSED: profile/SID/ACL failure aborts the launch. There is
            // no unconfined fallback while the strict flag is on (criterion 6).
            let (ac, caps) = prepare_appcontainer(&cmd)?;
            sec_caps_ptr = Some(caps.as_ref() as *const SECURITY_CAPABILITIES);
            appcontainer_guard = Some(ac);
            caps_box = Some(caps);
        }
        // Reported, never silently downgraded to the weaker path: a fallback
        // would put us back in the topology this exists to escape, while the
        // caller believed otherwise.
        let (p, thread) = spawn_with_job_at_creation(&cmd, stdio, job.0, breakaway, sec_caps_ptr)
            .map_err(|e| {
            // Names the appcontainer flag so a FAIL-CLOSED launch is
            // attributable to the strict path rather than a bare OS error.
            io::Error::other(format!(
                "creation-time strict launch failed (appcontainer={appcontainer}): {e}"
            ))
        })?;
        unsafe { ResumeThread(thread) };
        unsafe { CloseHandle(thread) };
        process = p;
    } else {
        cmd.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);
        let spawned: Child = cmd.spawn()?;
        process = spawned.as_raw_handle() as HANDLE;
        child = Some(spawned);
        unenforced.push("process_limit_enforcement_unverified_on_windows");
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

    // Deterministic AppContainer cleanup runs HERE, after the child has exited
    // and its accounting has been read — the profile's Drop deletes the profile
    // and frees the SID. Explicit so the ordering (and the fact these guards are
    // kept alive across the whole spawn+wait, not just to their last mention) is
    // visible rather than incidental. No-ops when the AppContainer path was off.
    drop(caps_box);
    drop(appcontainer_guard);

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
