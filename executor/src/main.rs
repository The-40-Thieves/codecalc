//! codecalc-exec — sandboxed multi-language executor.
//!
//! Reads source code on stdin, executes it in the requested language inside a
//! fresh temp dir (or a caller-supplied --workdir) with rlimits + wall-clock
//! timeout + process-group kill, prints a JSON result on stdout.
//!
//! Usage: codecalc-exec --lang <name> [--timeout <secs>] [--stdin <data>]
//!        [--stdin-file <path>] [--workdir <dir>] [--max-memory-mb <mb>]
//!        [--max-output-kb <kb>] [--max-cpu <secs>] [--no-net] < code
//!
//! This is the security-sensitive core: written in Rust so no user input is
//! ever evaluated/interpreted by the host process — it only spawns children
//! with OS-enforced resource limits.

use std::env;
use std::fs;
use std::io::Read;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use serde_json::json;

const MAX_OUTPUT_BYTES: u64 = 64 * 1024;
const AS_LIMIT_BYTES: u64 = 2048 * 1024 * 1024 * 1024; // 2 TiB VA (V8/JVM need huge VA)
const FSIZE_LIMIT_BYTES: u64 = 256 * 1024 * 1024;
const NFILE_LIMIT: u64 = 256;
// RLIMIT_NPROC counts ALL processes of this uid (not just sandbox children) —
// this host already runs ~120 ubuntu processes, so the limit must leave
// headroom for the host while still stopping a fork bomb cold.
const NPROC_LIMIT: u64 = 1024;
const CPU_GRACE_SECONDS: u64 = 8;

/// Env allowlist: executed code must NEVER inherit secrets (API keys, tokens).
/// Only the vars a runtime needs to function. Everything else is dropped.
const ENV_ALLOWLIST: &[&str] = &[
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONUNBUFFERED",
    "JAVA_HOME", "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOMODCACHE",
];

const RUNTIME_PATH: &str = "/data/tools/mise/shims:/home/ubuntu/.local/bin:/home/ubuntu/.npm-global/bin:/home/ubuntu/.local/share/swiftly/bin:/home/ubuntu/.cargo/bin:/nix/var/nix/profiles/default/bin:/usr/local/bin:/usr/bin:/bin";

/// A language entry: optional compile step + run step. `{file}` `{exe}` `{work}` are placeholders.
struct Lang {
    name: &'static str,
    ext: &'static str,
    compile: Option<&'static [&'static str]>,
    run: &'static [&'static str],
}

const LANGS: &[Lang] = &[
    // interpreters
    Lang { name: "python3", ext: "py", compile: None, run: &["python3", "{file}"] },
    Lang { name: "node", ext: "js", compile: None, run: &["node", "{file}"] },
    Lang { name: "bun", ext: "ts", compile: None, run: &["bun", "run", "{file}"] },
    Lang { name: "deno", ext: "ts", compile: None, run: &["deno", "run", "{file}"] },
    Lang { name: "typescript", ext: "ts", compile: None, run: &["deno", "run", "{file}"] },
    Lang { name: "ruby", ext: "rb", compile: None, run: &["ruby", "{file}"] },
    Lang { name: "php", ext: "php", compile: None, run: &["php", "{file}"] },
    Lang { name: "perl", ext: "pl", compile: None, run: &["perl", "{file}"] },
    Lang { name: "lua", ext: "lua", compile: None, run: &["lua", "{file}"] },
    Lang { name: "tcl", ext: "tcl", compile: None, run: &["tclsh", "{file}"] },
    Lang { name: "r", ext: "R", compile: None, run: &["Rscript", "{file}"] },
    Lang { name: "elixir", ext: "exs", compile: None, run: &["elixir", "{file}"] },
    Lang { name: "erlang", ext: "erl", compile: None, run: &["escript", "{file}"] },
    Lang { name: "bash", ext: "sh", compile: None, run: &["bash", "{file}"] },
    Lang { name: "zsh", ext: "zsh", compile: None, run: &["zsh", "{file}"] },
    Lang { name: "mojo", ext: "mojo", compile: None, run: &["mojo", "run", "{file}"] },
    Lang { name: "swift", ext: "swift", compile: None, run: &["swift", "{file}"] },
    // compilers
    Lang { name: "c", ext: "c", compile: Some(&["gcc", "-O2", "-o", "{exe}", "{file}"]), run: &["{exe}"] },
    Lang { name: "cpp", ext: "cpp", compile: Some(&["g++", "-O2", "-o", "{exe}", "{file}"]), run: &["{exe}"] },
    Lang { name: "c++", ext: "cpp", compile: Some(&["g++", "-O2", "-o", "{exe}", "{file}"]), run: &["{exe}"] },
    Lang { name: "rust", ext: "rs", compile: Some(&["rustc", "-O", "-o", "{exe}", "{file}"]), run: &["{exe}"] },
    Lang { name: "go", ext: "go", compile: None, run: &["go", "run", "{file}"] },
    Lang { name: "fortran", ext: "f90", compile: Some(&["gfortran", "-O2", "-o", "{exe}", "{file}"]), run: &["{exe}"] },
    Lang { name: "zig", ext: "zig", compile: None, run: &["zig", "run", "{file}"] },
    Lang { name: "java", ext: "java", compile: None, run: &["java", "{file}"] },
    Lang { name: "kotlin", ext: "kt", compile: Some(&["kotlinc", "{file}", "-include-runtime", "-d", "{work}/out.jar"]), run: &["java", "-jar", "{work}/out.jar"] },
    // project wrappers
    Lang {
        name: "csharp",
        ext: "cs",
        compile: None,
        run: &["bash", "-c", "dotnet new console -o {work}/proj -n prog --force && cp {file} {work}/proj/Program.cs && dotnet run --project {work}/proj --no-launch-profile"],
    },
    Lang {
        name: "gleam",
        ext: "gleam",
        compile: None,
        run: &["bash", "-c", "gleam new {work}/proj --name prog --skip-git && cp {file} {work}/proj/src/prog.gleam && cd {work}/proj && gleam run"],
    },
    Lang {
        name: "haskell",
        ext: "hs",
        compile: None,
        run: &["bash", "-c", "nix-shell -p ghc --run \"ghc -O2 -o {exe} {file} && {exe}\""],
    },
    // data / query DSLs
    Lang { name: "sqlite", ext: "sql", compile: None, run: &["bash", "-c", "sqlite3 :memory: < {file}"] },
    Lang { name: "jq", ext: "jq", compile: None, run: &["jq", "-n", "-f", "{file}"] },
    Lang { name: "awk", ext: "awk", compile: None, run: &["awk", "-f", "{file}"] },
];

fn canonical(name: &str) -> Option<&'static Lang> {
    let n = name.trim().to_lowercase();
    LANGS.iter().find(|l| l.name == n).or_else(|| match n.as_str() {
        "python" | "py" | "python3.14" | "python3.12" => LANGS.iter().find(|l| l.name == "python3"),
        "js" | "javascript" | "nodejs" => LANGS.iter().find(|l| l.name == "node"),
        "ts" => LANGS.iter().find(|l| l.name == "typescript"),
        "cxx" => LANGS.iter().find(|l| l.name == "c++"),
        "rscript" => LANGS.iter().find(|l| l.name == "r"),
        "sh" | "shell" => LANGS.iter().find(|l| l.name == "bash"),
        "cs" | "c#" | "dotnet" => LANGS.iter().find(|l| l.name == "csharp"),
        "ghc" | "hs" => LANGS.iter().find(|l| l.name == "haskell"),
        _ => None,
    })
}

/// Does `cmd` resolve on PATH? Used by --probe to report which runtimes an
/// older/minimal machine actually has.
fn on_path(cmd: &str) -> bool {
    // absolute or contains a slash: just check executability
    if cmd.contains('/') {
        return std::fs::metadata(cmd).map(|m| m.is_file()).unwrap_or(false);
    }
    let paths = env::var("PATH").unwrap_or_default();
    for dir in paths.split(':') {
        if dir.is_empty() {
            continue;
        }
        let candidate = Path::new(dir).join(cmd);
        if candidate.is_file() {
            // cheap executable check: readable + (any) execute bit
            use std::os::unix::fs::PermissionsExt;
            if let Ok(meta) = std::fs::metadata(&candidate) {
                let mode = meta.permissions().mode();
                if mode & 0o111 != 0 {
                    return true;
                }
            }
        }
    }
    false
}

/// First non-placeholder command in a run/compile template (the runtime binary).
fn first_cmd(template: &[&'static str]) -> &'static str {
    template.iter().find(|a| !a.starts_with('{')).copied().unwrap_or("")
}

/// Probe every language's runtime against PATH; JSON: {"language": bool, ...}
fn probe() -> serde_json::Value {
    let mut out = serde_json::Map::new();
    for lang in LANGS {
        // Compiled languages have all-placeholder run templates ({exe});
        // their real runtime is the compile command's first binary.
        let cmd = match first_cmd(lang.run) {
            "" => first_cmd(lang.compile.unwrap_or(&[])),
            c => c,
        };
        let available = if cmd.is_empty() || cmd == "bash" || cmd == "sh" {
            // bash -c wrappers need bash itself; empty = nothing to probe
            on_path(if cmd.is_empty() { "bash" } else { cmd })
        } else {
            on_path(cmd)
        };
        out.insert(lang.name.to_string(), json!(available));
    }
    serde_json::Value::Object(out)
}

fn substitute(template: &str, file: &str, exe: &str, work: &str) -> String {
    template
        .replace("{file}", file)
        .replace("{exe}", exe)
        .replace("{work}", work)
}

/// Per-call resource limits (defaults applied by the caller).
#[derive(Clone, Copy)]
struct Limits {
    timeout: u64,      // wall-clock seconds
    max_cpu: u64,      // RLIMIT_CPU seconds (0 = timeout + grace)
    max_memory_mb: u64,// RLIMIT_AS, 0 = 2 TiB default
    max_output_kb: u64,// stdout/stderr cap + FSIZE, 0 = 64 KiB
    no_net: bool,      // LD_PRELOAD a socket-blocking shim
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            timeout: 10,
            max_cpu: 0,
            max_memory_mb: 0,
            max_output_kb: 0,
            no_net: false,
        }
    }
}

fn apply_limits(limits: &Limits) {
    let cpu_limit = if limits.max_cpu > 0 {
        limits.max_cpu
    } else {
        limits.timeout + CPU_GRACE_SECONDS
    };
    let as_bytes = if limits.max_memory_mb > 0 {
        limits.max_memory_mb * 1024 * 1024
    } else {
        AS_LIMIT_BYTES
    };
    let fsize_bytes = if limits.max_output_kb > 0 {
        limits.max_output_kb * 1024
    } else {
        FSIZE_LIMIT_BYTES
    };
    unsafe {
        let cpu = libc::rlimit { rlim_cur: cpu_limit, rlim_max: cpu_limit };
        libc::setrlimit(libc::RLIMIT_CPU, &cpu);
        let as_ = libc::rlimit { rlim_cur: as_bytes, rlim_max: as_bytes };
        libc::setrlimit(libc::RLIMIT_AS, &as_);
        let fsize = libc::rlimit { rlim_cur: fsize_bytes, rlim_max: fsize_bytes };
        libc::setrlimit(libc::RLIMIT_FSIZE, &fsize);
        let nfile = libc::rlimit { rlim_cur: NFILE_LIMIT, rlim_max: NFILE_LIMIT };
        libc::setrlimit(libc::RLIMIT_NOFILE, &nfile);
        // fork-bomb guard: cap child processes for this user
        let nproc = libc::rlimit { rlim_cur: NPROC_LIMIT, rlim_max: NPROC_LIMIT };
        libc::setrlimit(libc::RLIMIT_NPROC, &nproc);
        let core = libc::rlimit { rlim_cur: 0, rlim_max: 0 };
        libc::setrlimit(libc::RLIMIT_CORE, &core);
    }
}

/// Result of one exec step, including resource usage (wait4 rusage).
struct StepResult {
    exit_code: i64,
    signal: Option<i32>,
    stdout: String,
    stderr: String,
    timed_out: bool,
    cpu_ms: u64,
    peak_memory_kb: u64,
    output_truncated: bool,
}

/// Run one argv step, redirecting stdout/stderr to files in `work` (avoids pipe
/// deadlock), killing the whole process group on timeout. Uses wait4 to also
/// harvest CPU time + peak RSS for verdicts.
fn run_step(argv: &[String], work: &Path, tag: &str, stdin_data: &[u8], limits: &Limits) -> StepResult {
    let out_path = work.join(format!("{tag}.out"));
    let err_path = work.join(format!("{tag}.err"));
    let in_path = work.join(format!("{tag}.in"));
    let _ = fs::write(&in_path, stdin_data);

    let out_f = fs::File::create(&out_path).expect("create out file");
    let err_f = fs::File::create(&err_path).expect("create err file");
    let in_f = fs::File::open(&in_path).expect("open in file");

    let child = unsafe {
        let mut cmd = Command::new(&argv[0]);
        cmd.args(&argv[1..])
            .current_dir(work)
            // SECURITY: clear env, then re-add ONLY the allowlist. User code
            // must never see API keys / tokens from the host environment.
            .env_clear();
        for key in ENV_ALLOWLIST {
            if let Ok(val) = std::env::var(key) {
                cmd.env(key, val);
            }
        }
        cmd.env("PATH", RUNTIME_PATH)  // always the sandbox PATH, not host's
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::from(in_f))
            .stdout(Stdio::from(out_f))
            .stderr(Stdio::from(err_f))
            .process_group(0);
        if limits.no_net {
            // Block network egress by preloading a socket-blocking shim.
            // Best-effort: dynamic binaries only (static ones ignore LD_PRELOAD).
            if let Ok(exe_path) = env::current_exe() {
                let shim = exe_path.parent().unwrap_or(Path::new(".")).join("blocknet.so");
                if shim.is_file() {
                    cmd.env("LD_PRELOAD", shim.to_string_lossy().into_owned());
                }
            }
        }
        cmd.pre_exec({
            let limits_owned = *limits; // Copy — closure must be 'static
            move || {
                apply_limits(&limits_owned);
                Ok(())
            }
        })
        .spawn()
    };

    let mut child = match child {
        Ok(c) => c,
        Err(e) => {
            return StepResult {
                exit_code: -2, signal: None, stdout: String::new(),
                stderr: format!("spawn failed: {e}"), timed_out: false,
                cpu_ms: 0, peak_memory_kb: 0, output_truncated: false,
            };
        }
    };

    let start = Instant::now();
    let mut status: libc::c_int = 0;
    let mut rusage: libc::rusage = unsafe { std::mem::zeroed() };
    let mut timed_out = false;
    loop {
        let r = unsafe { libc::wait4(child.id() as libc::pid_t, &mut status, libc::WNOHANG, &mut rusage) };
        if r == child.id() as libc::pid_t {
            break; // reaped with rusage
        } else if r == -1 {
            // EINTR: retry; anything else: report
            let e = std::io::Error::last_os_error();
            if e.raw_os_error() != Some(libc::EINTR) {
                child.kill().ok();
                let _ = child.wait();
                return StepResult {
                    exit_code: -2, signal: None, stdout: String::new(),
                    stderr: format!("wait failed: {e}"), timed_out: false,
                    cpu_ms: 0, peak_memory_kb: 0, output_truncated: false,
                };
            }
        } else {
            // still running
            if start.elapsed() >= Duration::from_secs(limits.timeout) {
                unsafe { libc::killpg(child.id() as i32, libc::SIGKILL); }
                // reap with rusage (blocking — it is dying)
                let _ = unsafe { libc::wait4(child.id() as libc::pid_t, &mut status, 0, &mut rusage) };
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

    let cpu_ms = ((rusage.ru_utime.tv_sec + rusage.ru_stime.tv_sec) * 1000
        + (rusage.ru_utime.tv_usec + rusage.ru_stime.tv_usec) / 1000) as u64;
    let peak_memory_kb = rusage.ru_maxrss as u64; // KiB on Linux

    let (stdout, out_trunc) = read_capped(&out_path, limits.max_output_kb);
    let (stderr, err_trunc) = read_capped(&err_path, limits.max_output_kb);
    if timed_out && stderr.is_empty() {
        StepResult {
            exit_code, signal, stdout,
            stderr: "<killed: exceeded wall-clock timeout>".into(),
            timed_out, cpu_ms, peak_memory_kb,
            output_truncated: out_trunc || err_trunc,
        }
    } else {
        StepResult {
            exit_code, signal, stdout, stderr,
            timed_out, cpu_ms, peak_memory_kb,
            output_truncated: out_trunc || err_trunc,
        }
    }
}

fn read_capped(path: &Path, max_output_kb: u64) -> (String, bool) {
    let cap = if max_output_kb > 0 { max_output_kb * 1024 } else { MAX_OUTPUT_BYTES };
    let mut buf = Vec::new();
    if let Ok(mut f) = fs::File::open(path) {
        let _ = f.read_to_end(&mut buf);
    }
    let truncated = buf.len() as u64 > cap;
    if truncated {
        buf.truncate(cap as usize);
        buf.extend_from_slice(b"\n...[truncated]");
    }
    (String::from_utf8_lossy(&buf).into_owned(), truncated)
}

/// Classify a run into a verdict. Heuristics:
/// TLE = wall-clock kill · OLE = output over cap · MLE = signal + RSS near
/// memory cap · RTE = nonzero exit / signal · OK otherwise.
fn verdict(sr: &StepResult, limits: &Limits) -> &'static str {
    if sr.timed_out {
        return "TLE";
    }
    if sr.output_truncated {
        return "OLE";
    }
    if sr.signal.is_some() {
        if limits.max_memory_mb > 0 && sr.peak_memory_kb >= limits.max_memory_mb * 1024 / 2 {
            return "MLE";
        }
        return "RTE";
    }
    if sr.exit_code != 0 {
        return "RTE";
    }
    "OK"
}

fn execute(lang_name: &str, code: &str, stdin_data: &str, limits: &Limits, workdir: Option<&str>) -> serde_json::Value {
    let lang = match canonical(lang_name) {
        Some(l) => l,
        None => {
            let known: Vec<&str> = LANGS.iter().map(|l| l.name).collect();
            return json!({
                "ok": false,
                "error": format!("unknown language '{lang_name}'. Available: {}", known.join(", "))
            });
        }
    };

    static COUNTER: AtomicU64 = AtomicU64::new(0);
    // Secure tempdir: use fs::create_dir (O_EXCL semantics — fails on existing
    // path) with an unpredictable name, retrying on collision. Never
    // create_dir_all: a pre-seeded symlink at a guessable path would redirect
    // the sandbox elsewhere. Multi-user /tmp is hostile by default.
    // A caller-supplied --workdir (sessions) bypasses creation and is NOT
    // deleted afterwards.
    let work = match workdir {
        Some(dir) => Path::new(dir).to_path_buf(),
        None => {
            let dir = loop {
                let nonce = COUNTER.fetch_add(1, Ordering::Relaxed)
                    ^ std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .map(|d| d.subsec_nanos() as u64)
                        .unwrap_or(0);
                let candidate = env::temp_dir().join(format!("codecalc-{}-{nonce:x}", std::process::id()));
                match fs::create_dir(&candidate) {
                    Ok(()) => break candidate,
                    Err(_) => continue, // exists (or race) — pick a new name
                }
            };
            dir
        }
    };

    let file = work.join(format!("main.{ext}", ext = lang.ext));
    let exe = work.join("a.out");
    if fs::write(&file, code).is_err() {
        let _ = fs::remove_dir_all(&work);
        return json!({ "ok": false, "error": "failed to write source" });
    }

    let started = Instant::now();
    let work_s = work.to_string_lossy().into_owned();

    if let Some(compile) = lang.compile {
        let argv: Vec<String> = compile
            .iter()
            .map(|t| substitute(t, &file.to_string_lossy(), &exe.to_string_lossy(), &work_s))
            .collect();
        let sr = run_step(&argv, &work, "compile", b"", limits);
        if sr.timed_out || sr.exit_code != 0 || sr.signal.is_some() {
            let result = json!({
                "ok": false, "language": lang.name, "phase": "compile",
                "stdout": sr.stdout, "stderr": sr.stderr,
                "exit_code": if sr.signal.is_some() { serde_json::Value::Null } else { serde_json::Value::from(sr.exit_code) },
                "duration_ms": started.elapsed().as_millis() as u64,
                "cpu_ms": sr.cpu_ms, "peak_memory_kb": sr.peak_memory_kb,
                "timed_out": sr.timed_out, "verdict": verdict(&sr, limits),
            });
            if workdir.is_none() {
                let _ = fs::remove_dir_all(&work);
            }
            return result;
        }
    }

    let argv: Vec<String> = lang
        .run
        .iter()
        .map(|t| substitute(t, &file.to_string_lossy(), &exe.to_string_lossy(), &work_s))
        .collect();
    let sr = run_step(&argv, &work, "run", stdin_data.as_bytes(), limits);
    let duration_ms = started.elapsed().as_millis() as u64;

    let result = json!({
        "ok": sr.exit_code == 0 && !sr.timed_out && sr.signal.is_none(),
        "language": lang.name,
        "phase": "run",
        "stdout": sr.stdout,
        "stderr": sr.stderr,
        "exit_code": if sr.signal.is_some() { serde_json::Value::Null } else { serde_json::Value::from(sr.exit_code) },
        "duration_ms": duration_ms,
        "cpu_ms": sr.cpu_ms,
        "peak_memory_kb": sr.peak_memory_kb,
        "timed_out": sr.timed_out,
        "verdict": verdict(&sr, limits),
        "workdir": work_s,
    });
    if workdir.is_none() {
        let _ = fs::remove_dir_all(&work);
    }
    result
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();

    // --probe: report runtime availability, no execution
    if args.iter().any(|a| a == "--probe") {
        println!("{}", probe());
        return;
    }
    if args.iter().any(|a| a == "--languages") {
        let names: Vec<&str> = LANGS.iter().map(|l| l.name).collect();
        println!("{}", json!(names));
        return;
    }

    let mut lang = String::new();
    let mut stdin_data = String::new();
    let mut stdin_file: Option<String> = None;
    let mut workdir: Option<String> = None;
    let mut limits = Limits::default();

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--lang" => { i += 1; if i < args.len() { lang = args[i].clone(); } }
            "--timeout" => { i += 1; if i < args.len() { limits.timeout = args[i].parse().unwrap_or(10); } }
            "--max-cpu" => { i += 1; if i < args.len() { limits.max_cpu = args[i].parse().unwrap_or(0); } }
            "--max-memory-mb" => { i += 1; if i < args.len() { limits.max_memory_mb = args[i].parse().unwrap_or(0); } }
            "--max-output-kb" => { i += 1; if i < args.len() { limits.max_output_kb = args[i].parse().unwrap_or(0); } }
            "--stdin" => { i += 1; if i < args.len() { stdin_data = args[i].clone(); } }
            "--stdin-file" => {
                // stdin too large for argv (E2BIG) — read from a file instead
                i += 1;
                if i < args.len() {
                    stdin_file = Some(args[i].clone());
                }
            }
            "--workdir" => {
                i += 1;
                if i < args.len() {
                    workdir = Some(args[i].clone());
                }
            }
            "--no-net" => { limits.no_net = true; }
            _ => {}
        }
        i += 1;
    }

    if let Some(path) = stdin_file {
        stdin_data = fs::read_to_string(&path).unwrap_or_default();
    }

    let mut code = String::new();
    let _ = std::io::stdin().read_to_string(&mut code);

    let result = execute(&lang, &code, &stdin_data, &limits, workdir.as_deref());
    println!("{result}");
}
