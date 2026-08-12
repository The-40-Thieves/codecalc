//! jobprobe — which job-object mechanism actually binds `ActiveProcessLimit`?
//!
//! THE-818. On Windows 11 Pro the ceiling is set, `SetInformationJobObject` and
//! `AssignProcessToJobObject` both return success, and 400 of 400 child spawns
//! go through against a limit of 24 — reproducible 3/3, from two unrelated
//! launchers including Task Scheduler with no agent in the parent chain. The
//! sandboxed process observes `LIMIT_FLAGS 0x3000`
//! (`KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK`), a flag codecalc never sets.
//!
//! PR #134 made that honest — the run now discloses `process_limit_not_enforced_*`
//! instead of claiming a ceiling it did not apply. This binary is for the repair.
//!
//! WHY A PROBE INSTEAD OF A PATCH. THE-818's first acceptance criterion is that
//! the mechanism be *confirmed on Windows* rather than argued from documentation,
//! because picking wrong from a machine that cannot observe the failure is
//! exactly how THE-775 got mis-triaged the first time. There are three candidate
//! fixes with different blast radii, and the documentation does not settle which
//! one binds:
//!
//!   baseline          what codecalc does today — the control that must FAIL
//!   breakaway_ok_own  JOB_OBJECT_LIMIT_BREAKAWAY_OK on OUR job. Smallest change.
//!                     Suspect: breakaway is granted by the ANCESTOR job, not ours.
//!   create_breakaway  CREATE_BREAKAWAY_FROM_JOB at spawn — detach from the
//!                     ambient job first, then assign to ours. Needs the ancestor
//!                     to permit it, and ERROR_ACCESS_DENIED if it does not.
//!   suspended_assign  raw CreateProcessW + CREATE_SUSPENDED, assign, ResumeThread.
//!                     Also closes the assignment race platform/windows.rs
//!                     documents. Does NOT by itself escape an ancestor job.
//!
//! Reading Microsoft's docs narrows it: "if the job has the extended limit
//! JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK, then child processes of any parent
//! process associated with the job are not associated with the job." That
//! predicts `create_breakaway` is the one that matters and that
//! `suspended_assign` alone is not sufficient — but a prediction is not a
//! measurement, which is the whole point of this file. `all` runs every
//! mechanism plus a combined one in a single pass so one Windows run answers it.
//!
//! USAGE, on the Windows 11 Pro box:
//!
//!     cargo build --release --bin jobprobe
//!     .\target\release\jobprobe.exe --spawns 400 --limit 24
//!
//! A mechanism BINDS if it spawned fewer than the limit. The control at the
//! bottom re-runs the nested-job case that already worked (`ActiveProcessLimit=5`
//! bound at 4/20 with WinError 1816) — if the control stops binding, the box
//! changed and no row above it means anything.
//!
//! On non-Windows this exits 2 without pretending to have measured something.

use std::process::ExitCode;

#[derive(Clone, Copy, PartialEq)]
enum Mechanism {
    Baseline,
    BreakawayOkOwn,
    CreateBreakaway,
    SuspendedAssign,
    /// `create_breakaway` + `suspended_assign` together: escape the ambient job
    /// AND close the assignment race. If the two are independently necessary
    /// this is the only row that both binds and has no gap.
    BreakawayAndSuspended,
}

impl Mechanism {
    fn name(self) -> &'static str {
        match self {
            Mechanism::Baseline => "baseline",
            Mechanism::BreakawayOkOwn => "breakaway_ok_own",
            Mechanism::CreateBreakaway => "create_breakaway",
            Mechanism::SuspendedAssign => "suspended_assign",
            Mechanism::BreakawayAndSuspended => "breakaway+suspended",
        }
    }

    fn all() -> [Mechanism; 5] {
        [
            Mechanism::Baseline,
            Mechanism::BreakawayOkOwn,
            Mechanism::CreateBreakaway,
            Mechanism::SuspendedAssign,
            Mechanism::BreakawayAndSuspended,
        ]
    }

    fn parse(s: &str) -> Option<Mechanism> {
        Mechanism::all().into_iter().find(|m| m.name() == s)
    }
}

struct Args {
    child: bool,
    sleeper: bool,
    spawns: u32,
    limit: u32,
    out: Option<String>,
    mechanism: Option<Mechanism>,
}

fn parse_args() -> Result<Args, String> {
    let mut a = Args {
        child: false,
        sleeper: false,
        spawns: 400,
        limit: 24,
        out: None,
        mechanism: None,
    };
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < argv.len() {
        let need = |i: usize| -> Result<String, String> {
            argv.get(i + 1)
                .cloned()
                .ok_or_else(|| format!("{} needs a value", argv[i]))
        };
        match argv[i].as_str() {
            "--child" => a.child = true,
            "--sleeper" => a.sleeper = true,
            "--spawns" => {
                a.spawns = need(i)?.parse().map_err(|_| "--spawns must be a number")?;
                i += 1;
            }
            "--limit" => {
                a.limit = need(i)?.parse().map_err(|_| "--limit must be a number")?;
                i += 1;
            }
            "--out" => {
                a.out = Some(need(i)?);
                i += 1;
            }
            "--mechanism" => {
                let v = need(i)?;
                if v != "all" {
                    a.mechanism =
                        Some(Mechanism::parse(&v).ok_or(format!("unknown mechanism '{v}'"))?);
                }
                i += 1;
            }
            "-h" | "--help" => return Err("usage".into()),
            other => return Err(format!("unknown argument '{other}'")),
        }
        i += 1;
    }
    Ok(a)
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("jobprobe: {e}");
            eprintln!(
                "usage: jobprobe [--mechanism all|baseline|breakaway_ok_own|\
                 create_breakaway|suspended_assign|breakaway+suspended] \
                 [--spawns N] [--limit L]"
            );
            return ExitCode::from(2);
        }
    };
    imp::run(args)
}

// ── Windows ─────────────────────────────────────────────────────────────────
#[cfg(windows)]
mod imp {
    use super::{Args, Mechanism};
    use std::io::Write;
    use std::os::windows::ffi::OsStrExt;
    use std::os::windows::io::AsRawHandle;
    use std::os::windows::process::CommandExt;
    use std::process::{Child, Command, ExitCode};

    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob,
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS, JOB_OBJECT_LIMIT_BREAKAWAY_OK,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
    };
    use windows_sys::Win32::System::Threading::{
        CREATE_BREAKAWAY_FROM_JOB, CREATE_NO_WINDOW, CREATE_SUSPENDED, CreateProcessW,
        GetCurrentProcess, PROCESS_INFORMATION, ResumeThread, STARTUPINFOW, WaitForSingleObject,
    };

    const INFINITE: u32 = 0xFFFF_FFFF;

    struct Job(HANDLE);
    impl Drop for Job {
        fn drop(&mut self) {
            unsafe { CloseHandle(self.0) };
        }
    }

    /// What the CHILD reports back about the job it actually landed in. The
    /// spawn count alone cannot distinguish "the ceiling does not bind" from
    /// "the child is in a different job than the one we set the ceiling on",
    /// and those need different fixes — so it reports both.
    struct ChildReport {
        spawned: u32,
        in_job: bool,
        limit_flags: u32,
        active_process_limit: u32,
        first_error: String,
    }

    fn wide(s: &str) -> Vec<u16> {
        std::ffi::OsStr::new(s)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect()
    }

    fn create_job(limit: u32, breakaway_ok_own: bool) -> Result<Job, String> {
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(format!(
                "CreateJobObjectW: {}",
                std::io::Error::last_os_error()
            ));
        }
        let job = Job(handle);
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        info.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
        if breakaway_ok_own {
            info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_BREAKAWAY_OK;
        }
        info.BasicLimitInformation.ActiveProcessLimit = limit;
        let ok = unsafe {
            SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if ok == 0 {
            return Err(format!(
                "SetInformationJobObject: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(job)
    }

    /// Spawn the child with `CREATE_SUSPENDED` through a raw `CreateProcessW`,
    /// so its initial thread handle is available to `ResumeThread` after the
    /// job assignment. `std::process::Command` does not expose that handle,
    /// which is precisely why platform/windows.rs documents the assignment race
    /// it cannot close.
    ///
    /// Returns (process handle, thread handle). Both are owned by the caller.
    fn spawn_suspended(cmdline: &str, extra_flags: u32) -> Result<(HANDLE, HANDLE), String> {
        let mut si: STARTUPINFOW = unsafe { std::mem::zeroed() };
        si.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
        let mut pi: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
        // CreateProcessW MUTATES lpCommandLine, so it must be a writable buffer.
        let mut cl = wide(cmdline);
        let ok = unsafe {
            CreateProcessW(
                std::ptr::null(),
                cl.as_mut_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                0,
                CREATE_SUSPENDED | CREATE_NO_WINDOW | extra_flags,
                std::ptr::null(),
                std::ptr::null(),
                &si,
                &mut pi,
            )
        };
        if ok == 0 {
            return Err(format!(
                "CreateProcessW: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok((pi.hProcess, pi.hThread))
    }

    fn read_report(path: &str) -> ChildReport {
        let text = std::fs::read_to_string(path).unwrap_or_default();
        let field = |k: &str| -> u32 {
            text.lines()
                .find_map(|l| l.strip_prefix(k)?.trim().parse().ok())
                .unwrap_or(0)
        };
        ChildReport {
            spawned: field("spawned="),
            in_job: field("in_job=") != 0,
            limit_flags: field("limit_flags="),
            active_process_limit: field("active_process_limit="),
            first_error: text
                .lines()
                .find_map(|l| l.strip_prefix("first_error="))
                .unwrap_or("")
                .to_string(),
        }
    }

    fn run_mechanism(m: Mechanism, spawns: u32, limit: u32) -> Result<ChildReport, String> {
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let out =
            std::env::temp_dir().join(format!("jobprobe-{}-{}.txt", m.name(), std::process::id()));
        let out_s = out.to_string_lossy().into_owned();
        let _ = std::fs::remove_file(&out);

        let job = create_job(limit, m == Mechanism::BreakawayOkOwn)?;

        let breakaway = matches!(
            m,
            Mechanism::CreateBreakaway | Mechanism::BreakawayAndSuspended
        );
        let suspended = matches!(
            m,
            Mechanism::SuspendedAssign | Mechanism::BreakawayAndSuspended
        );
        let extra = if breakaway {
            CREATE_BREAKAWAY_FROM_JOB
        } else {
            0
        };

        if suspended {
            let cmdline = format!(
                "\"{}\" --child --spawns {spawns} --out \"{out_s}\"",
                exe.display()
            );
            let (proc, thread) = spawn_suspended(&cmdline, extra)?;
            let assigned = unsafe { AssignProcessToJobObject(job.0, proc) };
            let assign_err = std::io::Error::last_os_error();
            // Resume REGARDLESS: a child left suspended never exits and the
            // wait below would hang forever, turning a failed assignment into
            // a hung probe rather than a reported one.
            unsafe { ResumeThread(thread) };
            unsafe { WaitForSingleObject(proc, INFINITE) };
            unsafe { CloseHandle(thread) };
            unsafe { CloseHandle(proc) };
            if assigned == 0 {
                return Err(format!("AssignProcessToJobObject: {assign_err}"));
            }
        } else {
            let mut cmd = Command::new(&exe);
            cmd.args(["--child", "--spawns"])
                .arg(spawns.to_string())
                .args(["--out", &out_s]);
            cmd.creation_flags(CREATE_NO_WINDOW | extra);
            let mut child: Child = cmd
                .spawn()
                .map_err(|e| format!("spawn: {e} (CREATE_BREAKAWAY_FROM_JOB is refused with ERROR_ACCESS_DENIED when the ancestor job does not permit it)"))?;
            let proc: HANDLE = child.as_raw_handle() as HANDLE;
            if unsafe { AssignProcessToJobObject(job.0, proc) } == 0 {
                let e = std::io::Error::last_os_error();
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("AssignProcessToJobObject: {e}"));
            }
            let _ = child.wait();
        }

        let report = read_report(&out_s);
        let _ = std::fs::remove_file(&out);
        Ok(report)
    }

    /// The child: report the job it actually landed in, then try to exceed the
    /// ceiling.
    fn run_child(spawns: u32, out: &str) -> ExitCode {
        let mut in_job: i32 = 0;
        unsafe { IsProcessInJob(GetCurrentProcess(), std::ptr::null_mut(), &mut in_job) };
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        let mut ret: u32 = 0;
        let queried = unsafe {
            QueryInformationJobObject(
                std::ptr::null_mut(),
                JobObjectExtendedLimitInformation,
                &mut info as *mut _ as *mut core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                &mut ret,
            )
        };
        let (flags, apl) = if queried != 0 {
            (
                info.BasicLimitInformation.LimitFlags,
                info.BasicLimitInformation.ActiveProcessLimit,
            )
        } else {
            (0, 0)
        };

        let exe = std::env::current_exe().unwrap_or_default();
        let mut kids: Vec<Child> = Vec::new();
        let mut first_error = String::new();
        for _ in 0..spawns {
            let mut c = Command::new(&exe);
            c.arg("--sleeper");
            c.creation_flags(CREATE_NO_WINDOW);
            match c.spawn() {
                // HELD OPEN. A sleeper that exited would free its slot and the
                // ceiling would never be reached no matter how many were
                // started — the count would measure spawn throughput instead
                // of the limit.
                Ok(ch) => kids.push(ch),
                Err(e) => {
                    if first_error.is_empty() {
                        // WinError 1816 = ERROR_TOO_MANY_ACTIVE_PROCESSES, which
                        // is the ceiling biting rather than an ordinary failure.
                        first_error = e.to_string().replace('\n', " ");
                    }
                    break;
                }
            }
        }
        let spawned = kids.len() as u32;
        for mut k in kids {
            let _ = k.kill();
            let _ = k.wait();
        }

        if let Ok(mut f) = std::fs::File::create(out) {
            let _ = write!(
                f,
                "spawned={spawned}\nin_job={}\nlimit_flags={flags}\nactive_process_limit={apl}\nfirst_error={first_error}\n",
                i32::from(in_job != 0)
            );
        }
        ExitCode::SUCCESS
    }

    /// The control. A self-assigned nested job with `ActiveProcessLimit=5` bound
    /// at 4/20 with WinError 1816 on the same box, which is what proves Windows
    /// can still do this and the failure above is codecalc's path rather than an
    /// OS capability gap. If this stops binding, no row above it means anything.
    fn run_control() -> String {
        let job = match create_job(5, false) {
            Ok(j) => j,
            Err(e) => return format!("control job failed: {e}"),
        };
        if unsafe { AssignProcessToJobObject(job.0, GetCurrentProcess()) } == 0 {
            return format!(
                "control assign failed: {} (expected when this process is already \
                 in a job that forbids nesting)",
                std::io::Error::last_os_error()
            );
        }
        let exe = std::env::current_exe().unwrap_or_default();
        let mut kids: Vec<Child> = Vec::new();
        let mut err = String::new();
        for _ in 0..20 {
            let mut c = Command::new(&exe);
            c.arg("--sleeper");
            c.creation_flags(CREATE_NO_WINDOW);
            match c.spawn() {
                Ok(ch) => kids.push(ch),
                Err(e) => {
                    err = e.to_string().replace('\n', " ");
                    break;
                }
            }
        }
        let n = kids.len();
        for mut k in kids {
            let _ = k.kill();
            let _ = k.wait();
        }
        let verdict = if n < 5 { "BOUND" } else { "UNBOUND" };
        format!("nested ActiveProcessLimit=5 -> {n}/20 spawned  {verdict}  {err}")
    }

    pub fn run(args: Args) -> ExitCode {
        if args.sleeper {
            // Long enough to still be alive while the loop above it runs, short
            // enough that a crashed parent does not leave the box littered.
            std::thread::sleep(std::time::Duration::from_secs(30));
            return ExitCode::SUCCESS;
        }
        if args.child {
            let out = args.out.unwrap_or_default();
            return run_child(args.spawns, &out);
        }

        println!("jobprobe — THE-818: which mechanism binds ActiveProcessLimit?");
        println!("limit={} spawns={}\n", args.limit, args.spawns);
        println!(
            "{:<21} {:>9}  {:<8} {:<11} {:<5} {}",
            "MECHANISM", "SPAWNED", "VERDICT", "LIMIT_FLAGS", "APL", "NOTE"
        );

        let mechanisms: Vec<Mechanism> = match args.mechanism {
            Some(m) => vec![m],
            None => Mechanism::all().to_vec(),
        };
        let mut any_bound = false;
        for m in mechanisms {
            match run_mechanism(m, args.spawns, args.limit) {
                Ok(r) => {
                    // Fewer than the ceiling means the ceiling stopped it. The
                    // comparison is against the LIMIT, not against `spawns`:
                    // a run that stopped early for an unrelated reason would
                    // otherwise read as a bind.
                    let bound = r.spawned < args.limit;
                    any_bound |= bound;
                    println!(
                        "{:<21} {:>4}/{:<4} {:<8} 0x{:08X}  {:<5} {}",
                        m.name(),
                        r.spawned,
                        args.spawns,
                        if bound { "BOUND" } else { "UNBOUND" },
                        r.limit_flags,
                        r.active_process_limit,
                        if r.in_job {
                            &r.first_error
                        } else {
                            "child is in NO job"
                        }
                    );
                }
                Err(e) => println!("{:<21} {:>9}  {:<8} {e}", m.name(), "-", "ERROR"),
            }
        }

        println!("\ncontrol: {}", run_control());
        println!(
            "\nA mechanism BINDS if SPAWNED < {}. `baseline` is expected to be UNBOUND —\n\
             it is what codecalc does today. Report the whole table on THE-818.",
            args.limit
        );

        // Exit code is the finding: 0 means at least one mechanism binds and
        // there is something to implement; 1 means none did and the ticket needs
        // a fourth candidate rather than a patch.
        if any_bound {
            ExitCode::SUCCESS
        } else {
            ExitCode::from(1)
        }
    }
}

// ── everything else ─────────────────────────────────────────────────────────
#[cfg(not(windows))]
mod imp {
    use super::Args;
    use std::process::ExitCode;

    pub fn run(_args: Args) -> ExitCode {
        eprintln!(
            "jobprobe measures Windows Job Object behaviour and there is nothing \
             here to measure.\n\
             It exits 2 rather than printing an empty table, because a probe that \
             reports 'no mechanism bound' on a platform without job objects is \
             worse than one that refuses: THE-818 is exactly a case where a \
             confident wrong answer already cost a triage cycle."
        );
        ExitCode::from(2)
    }
}
