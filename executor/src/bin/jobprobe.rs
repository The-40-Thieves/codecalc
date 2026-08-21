//! jobprobe — why does the process ceiling bind here and not there?
//!
//! **This file was rewritten on 2026-08-12 because its first version
//! was measuring the wrong thing, and said so confidently.**
//!
//! WHAT WENT WRONG, because it is the reason this file now works the way it
//! does. The original jobprobe built its OWN job object, spawned its OWN child
//! and counted. It reported the ceiling binding at 23/400 on a Windows 11 Pro
//! box — twice, from two launchers — and that was read as "the bug does not
//! reproduce here". It does. On the same box, in the same shell, the shipped
//! `codecalc-exec.exe` spawned 400 of 400 against the same ceiling of 24.
//!
//! Identical ambient job, opposite outcome, and the only variable was the code
//! being run. A diagnostic that REIMPLEMENTS the code under test measures the
//! reimplementation; its agreement with production is a hypothesis, not a
//! result. Two "does not reproduce" reports and a launcher hunt followed from
//! treating it as one.
//!
//! SO THE PRODUCTION ARM IS NOT A MODEL. It calls `platform::spawn_and_wait` —
//! the same function `main.rs` calls, through the same module, with the same
//! job creation, the same suspended spawn and the same disclosure logic. If it
//! ever stops matching production, it is because production changed underneath
//! a shared function, not because this file drifted.
//!
//! WHAT IT NOW MEASURES. Every arm runs the SAME child doing the SAME thing, so
//! the job configuration is the only variable. The arms bisect the one
//! difference left between the harness that bound and the binary that did not:
//!
//!     production      platform::spawn_and_wait — the shipped path, 0x230A
//!     flags_0x230A    production's FLAGS, jobprobe's spawn. Splits
//!                     "the flags matter" from "the spawn path matters".
//!     kill+active     0x2008 — what the old jobprobe used, and what bound
//!     active_only     ACTIVE_PROCESS alone, the floor
//!     +process_memory 0x2008 plus JOB_OBJECT_LIMIT_PROCESS_MEMORY
//!     +job_memory     0x2008 plus JOB_OBJECT_LIMIT_JOB_MEMORY
//!     +process_time   0x2008 plus JOB_OBJECT_LIMIT_PROCESS_TIME
//!
//! Read it as a bisection. If `kill+active` binds and `flags_0x230A` does not,
//! one of the three `+` arms names the flag responsible. If `flags_0x230A`
//! binds and `production` does not, the flags are innocent and the spawn path
//! is the difference. If everything binds except `production`, the difference
//! is neither and the remaining variable is what production does that this file
//! still does not — say so rather than concluding.
//!
//! USAGE, on the Windows 11 Pro box:
//!
//!     cargo build --release --bin jobprobe
//!     .\target\release\jobprobe.exe --spawns 400 --limit 24
//!
//! READ THE LAST TWO LINES FIRST. A run is a measurement only if it ends with
//! `=== END jobprobe ===` preceded by a `control:` line with a result after it.
//! A table with no control line is UNVALIDATED. That is not hypothetical — the
//! first version of this file killed itself before printing the control, twice,
//! silently, and the table looked complete.
//!
//! EXIT CODES deliberately skip 1:
//!
//!     0  the arms disagree — there is a difference to name
//!     3  every arm agreed; nothing was isolated
//!     2  not Windows; nothing was measured
//!     1  NEVER returned by this program. A 1 means whatever tried to start it
//!        failed instead — a mangled `cmd /c` line, a missing binary.
//!
//! That separation exists because a Task Scheduler recipe with a quoting bug
//! reported `Last Result: 1` and no output file, which was indistinguishable
//! from a clean run finding nothing.
//!
//! FROM TASK SCHEDULER, USE A .bat WRAPPER. A scheduled task has no console, so
//! output must be redirected, and `schtasks /TR "cmd /c \"...\" > \"...\""` does
//! not survive: four quote characters, so cmd strips the outer pair. Put the
//! quoting in a file where cmd cannot re-parse it:
//!
//!     # probe.bat
//!     "E:\...\jobprobe.exe" --spawns 400 --limit 24 > "E:\...\out.txt" 2>&1
//!
//!     schtasks /Create /TN jobprobe /SC ONCE /ST 00:00 /F /TR "E:\...\probe.bat"
//!     schtasks /Run /TN jobprobe
//!     schtasks /Delete /TN jobprobe /F
//!
//! THE LAUNCHER IS PART OF THE MEASUREMENT — quote it next to every table. The
//! ambient job differs per launcher, and that was a live hypothesis for a while.
//! It is no longer the leading one: the shipped binary and this harness
//! disagreed under an IDENTICAL ambient job, so the code path is the variable
//! that matters. Record the launcher anyway; it costs a line.
//!
//! On non-Windows this exits 2 without pretending to have measured something.

use std::process::ExitCode;

/// Shared with `main.rs` rather than reimplemented — see the header. On
/// non-Windows it is unused, which is why the allow is here.
#[allow(dead_code, unused_imports)]
#[path = "../platform/mod.rs"]
mod platform;

#[derive(Clone, Copy, PartialEq)]
enum Arm {
    /// `platform::spawn_and_wait`: the shipped code, not a model of it.
    Production,
    /// Production's LimitFlags, this file's spawn. Splits flags from spawn path.
    Flags230A,
    /// What the old jobprobe used, and what bound on the box that failed.
    KillActive,
    ActiveOnly,
    PlusProcessMemory,
    PlusJobMemory,
    PlusProcessTime,
}

impl Arm {
    fn name(self) -> &'static str {
        match self {
            Arm::Production => "production",
            Arm::Flags230A => "flags_0x230A",
            Arm::KillActive => "kill+active",
            Arm::ActiveOnly => "active_only",
            Arm::PlusProcessMemory => "+process_memory",
            Arm::PlusJobMemory => "+job_memory",
            Arm::PlusProcessTime => "+process_time",
        }
    }

    fn all() -> [Arm; 7] {
        [
            Arm::Production,
            Arm::Flags230A,
            Arm::KillActive,
            Arm::ActiveOnly,
            Arm::PlusProcessMemory,
            Arm::PlusJobMemory,
            Arm::PlusProcessTime,
        ]
    }

    fn parse(s: &str) -> Option<Arm> {
        Arm::all().into_iter().find(|a| a.name() == s)
    }
}

struct Args {
    child: bool,
    sleeper: bool,
    spawns: u32,
    limit: u32,
    out: Option<String>,
    arm: Option<Arm>,
}

fn parse_args() -> Result<Args, String> {
    let mut a = Args {
        child: false,
        sleeper: false,
        spawns: 400,
        limit: 24,
        out: None,
        arm: None,
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
            "--arm" => {
                let v = need(i)?;
                if v != "all" {
                    a.arm = Some(Arm::parse(&v).ok_or(format!("unknown arm '{v}'"))?);
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
                "usage: jobprobe [--arm all|production|flags_0x230A|kill+active|\
                 active_only|+process_memory|+job_memory|+process_time] \
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
    use super::{Args, Arm, platform};
    use std::io::Write;
    use std::os::windows::io::AsRawHandle;
    use std::os::windows::process::CommandExt;
    use std::process::{Child, Command, ExitCode};

    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob,
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS, JOB_OBJECT_LIMIT_JOB_MEMORY,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_OBJECT_LIMIT_PROCESS_MEMORY,
        JOB_OBJECT_LIMIT_PROCESS_TIME, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
    };
    use windows_sys::Win32::System::Threading::{CREATE_NO_WINDOW, GetCurrentProcess};

    /// Mirrors `AS_LIMIT_BYTES` in main.rs — the default when no memory ceiling
    /// is requested, which is the shape production runs in.
    const AS_LIMIT_BYTES: u64 = 2048 * 1024 * 1024 * 1024;

    struct Job(HANDLE);
    impl Drop for Job {
        fn drop(&mut self) {
            unsafe { CloseHandle(self.0) };
        }
    }

    struct Report {
        spawned: u32,
        in_job: bool,
        limit_flags: u32,
        active_process_limit: u32,
        first_error: String,
        /// Only the production arm can report this: whether the shipped
        /// disclosure fired. A run that spawns past the ceiling AND says
        /// nothing is the defect this ticket is about.
        unenforced: Option<String>,
    }

    /// `kill_on_close` MUST be false for a job this process assigns ITSELF to —
    /// closing the last handle kills every member, which for the control means
    /// killing us mid-`format!`. That is not hypothetical; see #140.
    fn create_job(limit: u32, flags: u32, kill_on_close: bool) -> Result<Job, String> {
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(format!(
                "CreateJobObjectW: {}",
                std::io::Error::last_os_error()
            ));
        }
        let job = Job(handle);
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        info.BasicLimitInformation.LimitFlags = flags;
        if kill_on_close {
            info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        }
        info.BasicLimitInformation.ActiveProcessLimit = limit;
        // Set unconditionally so the ONLY difference between arms is the flag
        // bits. A limit whose flag is clear is ignored by Windows, so carrying
        // the value everywhere keeps the comparison honest.
        info.BasicLimitInformation.PerProcessUserTimeLimit = 30 * 10_000_000;
        info.ProcessMemoryLimit = AS_LIMIT_BYTES.min(usize::MAX as u64) as usize;
        info.JobMemoryLimit = AS_LIMIT_BYTES.min(usize::MAX as u64) as usize;

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

    fn arm_flags(arm: Arm) -> u32 {
        let base = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
        match arm {
            Arm::ActiveOnly => JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
            Arm::KillActive => base,
            Arm::PlusProcessMemory => base | JOB_OBJECT_LIMIT_PROCESS_MEMORY,
            Arm::PlusJobMemory => base | JOB_OBJECT_LIMIT_JOB_MEMORY,
            Arm::PlusProcessTime => base | JOB_OBJECT_LIMIT_PROCESS_TIME,
            // Exactly what platform/windows.rs sets.
            Arm::Flags230A | Arm::Production => {
                base | JOB_OBJECT_LIMIT_PROCESS_MEMORY
                    | JOB_OBJECT_LIMIT_JOB_MEMORY
                    | JOB_OBJECT_LIMIT_PROCESS_TIME
            }
        }
    }

    fn read_report(path: &str) -> Report {
        let text = std::fs::read_to_string(path).unwrap_or_default();
        let field = |k: &str| -> u32 {
            text.lines()
                .find_map(|l| l.strip_prefix(k)?.trim().parse().ok())
                .unwrap_or(0)
        };
        Report {
            spawned: field("spawned="),
            in_job: field("in_job=") != 0,
            limit_flags: field("limit_flags="),
            active_process_limit: field("active_process_limit="),
            first_error: text
                .lines()
                .find_map(|l| l.strip_prefix("first_error="))
                .unwrap_or("")
                .to_string(),
            unenforced: None,
        }
    }

    fn child_command(exe: &std::path::Path, spawns: u32, out: &str) -> Command {
        let mut cmd = Command::new(exe);
        cmd.args(["--child", "--spawns"])
            .arg(spawns.to_string())
            .args(["--out", out]);
        cmd
    }

    fn run_arm(arm: Arm, spawns: u32, limit: u32) -> Result<Report, String> {
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let out = std::env::temp_dir().join(format!(
            "jobprobe-{}-{}.txt",
            arm.name().replace(['+', '&'], "_"),
            std::process::id()
        ));
        let out_s = out.to_string_lossy().into_owned();
        let _ = std::fs::remove_file(&out);

        let mut report = if arm == Arm::Production {
            // THE POINT OF THIS FILE. Not a reimplementation of the shipped
            // path — the shipped path. Same job creation, same suspended
            // spawn, same disclosure.
            let limits = platform::ResolvedLimits {
                timeout_secs: 120,
                cpu_secs: 30,
                memory_bytes: AS_LIMIT_BYTES,
                fsize_bytes: 256 * 1024 * 1024,
                nofile: 256,
                max_processes: u64::from(limit),
                no_net: false,
            };
            let mut cmd = child_command(&exe, spawns, &out_s);
            cmd.stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null());
            let wait = platform::spawn_and_wait(cmd, &limits, platform::RawStdio::default())
                .map_err(|e| e.to_string())?;
            let mut r = read_report(&out_s);
            r.unenforced = Some(if wait.unenforced.is_empty() {
                "(none)".to_string()
            } else {
                wait.unenforced.join(",")
            });
            r
        } else {
            let job = create_job(limit, arm_flags(arm), true)?;
            let mut cmd = child_command(&exe, spawns, &out_s);
            cmd.creation_flags(CREATE_NO_WINDOW);
            let mut child: Child = cmd.spawn().map_err(|e| format!("spawn: {e}"))?;
            let process: HANDLE = child.as_raw_handle() as HANDLE;
            if unsafe { AssignProcessToJobObject(job.0, process) } == 0 {
                let e = std::io::Error::last_os_error();
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("AssignProcessToJobObject: {e}"));
            }
            let _ = child.wait();
            read_report(&out_s)
        };
        if report.active_process_limit == 0 && report.limit_flags == 0 {
            report.first_error = format!("no report written to {out_s}");
        }
        let _ = std::fs::remove_file(&out);
        Ok(report)
    }

    /// The child: report the job it landed in, then try to exceed the ceiling.
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
                // HELD OPEN. A sleeper that exited would free its slot, and the
                // count would measure spawn throughput instead of the ceiling.
                Ok(ch) => kids.push(ch),
                Err(e) => {
                    if first_error.is_empty() {
                        // WinError 1816 = ERROR_TOO_MANY_ACTIVE_PROCESSES, the
                        // ceiling biting rather than an ordinary failure.
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

    /// Proves Windows can still enforce this at all. If the control stops
    /// binding, the box changed and no row above it means anything.
    fn run_control() -> String {
        let job = match create_job(5, JOB_OBJECT_LIMIT_ACTIVE_PROCESS, false) {
            Ok(j) => j,
            Err(e) => return format!("control job failed: {e}"),
        };
        if unsafe { AssignProcessToJobObject(job.0, GetCurrentProcess()) } == 0 {
            return format!(
                "control assign failed: {} (expected when already in a job that \
                 forbids nesting)",
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
            std::thread::sleep(std::time::Duration::from_secs(30));
            return ExitCode::SUCCESS;
        }
        if args.child {
            return run_child(args.spawns, &args.out.unwrap_or_default());
        }

        println!("jobprobe — what makes the ceiling bind here and not there?");
        println!("limit={} spawns={}", args.limit, args.spawns);
        println!("`production` calls platform::spawn_and_wait — the shipped path, not a model.\n");
        println!("ARM              SPAWNED    VERDICT  LIMIT_FLAGS APL   NOTE");

        let arms: Vec<Arm> = match args.arm {
            Some(a) => vec![a],
            None => Arm::all().to_vec(),
        };
        let mut bound = 0usize;
        let mut unbound = 0usize;
        let mut production_lied = false;

        for arm in arms {
            match run_arm(arm, args.spawns, args.limit) {
                Ok(r) => {
                    // Compared against the LIMIT, not against `spawns`: a run
                    // that stopped early for an unrelated reason would
                    // otherwise read as a bind.
                    let is_bound = r.spawned < args.limit;
                    if is_bound {
                        bound += 1;
                    } else {
                        unbound += 1;
                    }
                    println!(
                        "{:<16} {:>4}/{:<4} {:<8} 0x{:08X}  {:<5} {}",
                        arm.name(),
                        r.spawned,
                        args.spawns,
                        if is_bound { "BOUND" } else { "UNBOUND" },
                        r.limit_flags,
                        r.active_process_limit,
                        if r.in_job {
                            r.first_error.clone()
                        } else {
                            "child is in NO job".to_string()
                        }
                    );
                    if let Some(u) = &r.unenforced {
                        // The whole ticket in one line: did the shipped path
                        // spawn past its ceiling AND stay silent about it?
                        let silent = !is_bound && !u.contains("process_limit");
                        println!(
                            "                 unenforced: {u}{}",
                            if silent {
                                "   <-- SPAWNED PAST THE CEILING AND DISCLOSED NOTHING"
                            } else {
                                ""
                            }
                        );
                        production_lied |= silent;
                    }
                }
                Err(e) => println!("{:<16} {:>9}  {:<8} {e}", arm.name(), "-", "ERROR"),
            }
        }

        print!("\ncontrol: running (nested ActiveProcessLimit=5)... ");
        let _ = std::io::stdout().flush();
        println!("{}", run_control());

        println!(
            "\nAn arm BINDS if SPAWNED < {}. Read the table as a bisection:\n  \
             kill+active binds, flags_0x230A does not  -> one `+` arm names the flag\n  \
             flags_0x230A binds, production does not   -> the flags are innocent; \
             it is the spawn path\n  \
             everything binds except production        -> neither; production does \
             something this file still does not",
            args.limit
        );
        if production_lied {
            println!(
                "\nPRODUCTION SPAWNED PAST ITS CEILING AND REPORTED NOTHING. That is \
                 acceptance criterion 4 failing on this host — a false claim, \
                 not a missing one."
            );
        }
        println!("=== END jobprobe (if this line is missing, the run was truncated) ===");
        let _ = std::io::stdout().flush();

        // 0 = the arms disagree, so something was isolated. 3 = they all agreed.
        if bound > 0 && unbound > 0 {
            ExitCode::SUCCESS
        } else {
            ExitCode::from(3)
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
             It exits 2 rather than printing an empty table: a probe that reports \
             'no difference found' on a platform without job objects is worse than \
             one that declines."
        );
        ExitCode::from(2)
    }
}
