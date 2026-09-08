"""The `unenforced` array must tell the truth, on every platform.

The executor's cross-platform story rests on one promise: a ceiling it cannot
apply is NAMED in `unenforced` rather than silently skipped. That promise has
two halves and only one of them is easy.

    listed as unenforced   ->  fine, the caller was told
    NOT listed             ->  it had better actually bite

The second half is what this file checks, by making a program violate each
ceiling the running platform claims to enforce and confirming the sandbox stops
it. A limit that is set but not enforced looks identical to one that is enforced
until something tries to exceed it — which is exactly how
`memory_limit_not_enforced_on_macos` came to be a documented entry rather than a
surprise.

The per-platform vocabulary is also pinned against the Rust source, because the
README's platform table and the strings the executor actually emits are two
descriptions of one thing and nothing else compares them. `cpu_limit` was
reported as `unavailable_on_windows` for a while when Windows job objects have
supported a CPU ceiling since XP; the table said the same, so the two agreed
with each other and disagreed with Windows.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import executor

FAILS: list[str] = []
SKIPS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")
    SKIPS.append(name)


IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")

WINDOWS_RS = (REPO_ROOT / "executor" / "src" / "platform" / "windows.rs").read_text(
    encoding="utf-8"
)

# The raw CreateProcessW path bypasses std::process::Command's Windows
# handle preparation. bInheritHandles=TRUE only copies handles that already
# carry HANDLE_FLAG_INHERIT; files opened by Rust do not. The raw path must
# therefore duplicate its three standard handles as inheritable and pass those
# duplicates in STARTUPINFOEXW.
creation_time_job_path = WINDOWS_RS.split(
    "fn spawn_with_job_at_creation", 1
)[1].split("pub fn spawn_and_wait", 1)[0]
spawn_and_wait_path = WINDOWS_RS.split("pub fn spawn_and_wait", 1)[1]
creation_branch, legacy_branch = spawn_and_wait_path.split(
    "if at_creation {", 1
)[1].split("} else {", 1)
check(
    "the creation-time job path supplies inheritable standard handles",
    "DuplicateHandle" in WINDOWS_RS
    and "InheritableStdio::duplicate(stdio)" in creation_time_job_path
    and "si.StartupInfo.hStdInput = inheritable_stdio[0]" in creation_time_job_path
    and "si.StartupInfo.hStdOutput = inheritable_stdio[1]" in creation_time_job_path
    and "si.StartupInfo.hStdError = inheritable_stdio[2]" in creation_time_job_path,
    "-> STARTF_USESTDHANDLES requires inheritable handles in the child",
)
check(
    "the creation-time diagnostic identifies the exact process Windows created",
    "spawn_with_job_at_creation: created PID=" in creation_time_job_path
    and "pi.dwProcessId" in creation_time_job_path,
    "-> compare this PID with the payload PID to detect a launcher boundary",
)
check(
    "the raw Windows spawn resolves the executable from the command PATH",
    "resolve_command_program(cmd)" in creation_time_job_path
    and "CreateProcessW(\n            program_w.as_ptr()" in creation_time_job_path,
    "-> a child environment block cannot control CreateProcessW executable lookup",
)
check(
    "the Windows limit job cannot be hidden behind a weaker nested job",
    "JobObjectBasicUIRestrictions" in WINDOWS_RS
    and "JOB_OBJECT_UILIMIT_EXITWINDOWS" in WINDOWS_RS
    and "SetInformationJobObject(" in WINDOWS_RS,
    "-> any UI restriction makes the CodeCalc job non-nestable",
)
check(
    "the raw Windows spawn inherits only its three standard handles",
    "PROC_THREAD_ATTRIBUTE_HANDLE_LIST" in creation_time_job_path
    and "let inherited_handles = [" in creation_time_job_path
    # made the attribute count a variable (2, or 3 with the AppContainer
    # SECURITY_CAPABILITIES attribute in the same list), so the literal `2` is
    # now `attr_count`. The property — the list is initialised before use — holds.
    and "InitializeProcThreadAttributeList(std::ptr::null_mut(), attr_count"
    in creation_time_job_path,
    "-> bInheritHandles=TRUE must not leak unrelated inheritable handles",
)
check(
    "creation-time job assignment is the Windows default",
    ".unwrap_or(true)" in WINDOWS_RS
    and ".unwrap_or(false)" not in WINDOWS_RS.split(
        "let at_creation =", 1
    )[1].split(";", 1)[0],
    "-> the post-creation topology was measured not to enforce the ceiling",
)
check(
    "a successful creation-time assignment is not reported as unenforced",
    'unenforced.push("process_limit_job_assigned_at_creation_on_windows")'
    not in creation_branch
    and 'unenforced.push("process_limit_enforcement_unverified_on_windows")'
    in legacy_branch,
    "-> topology diagnostics and missing enforcement are different claims",
)

#: Every string the executor is allowed to put in `unenforced`, and where it
#: comes from. A value outside this set is either a typo or a new limitation
#: nobody wrote down — both worth failing over.
KNOWN_UNENFORCED = {
    # unix.rs — a soft limit clamped down by an existing hard limit
    "cpu_limit_clamped_to_hard_rlimit",
    "file_size_limit_clamped_to_hard_rlimit",
    "process_limit_clamped_to_hard_rlimit",
    "memory_limit_clamped_to_hard_rlimit",
    "process_limit_is_a_fixed_ceiling_not_measured",
    "memory_limit_not_enforced_on_macos",
    # RLIMIT_NPROC does not bind a process whose effective uid is 0: the
    # kernel exempts privileged processes, so as root the ceiling is set and
    # has no effect. Not an escape — running as root is a deployment error —
    # but the result used to stay silent about it, which reads as "applied".
    # Verified by running the executor under sudo, where it appears, and as an
    # ordinary uid, where it does not. The Python fallback carries the prose
    # equivalent and scripts/check_parity.py gates that both have one.
    "process_limit_not_enforced_for_uid_0",
    # windows.rs
    "cpu_limit_counts_user_time_only_on_windows",
    "open_file_limit_unavailable_on_windows",
    "file_size_limit_unavailable_on_windows",
    "no_net_unavailable_on_windows",
    # windows.rs — The process ceiling is SET, both job-object calls
    # return success, and it still does not bind: measured on Windows 11 Pro
    # from two unrelated launchers (an agent harness, and Task Scheduler with no
    # agent in the parent chain), 400 of 400 spawns went through against a
    # ceiling of 24. The runs came back ok=true with the process limit absent
    # from this array, which is the one failure this file exists to catch.
    #
    # Four strings rather than one, because they are four different claims and a
    # caller can act differently on each. "The child escaped the job" is a fact
    # about this run; "an ancestor job allows breakaway" is a fact about the
    # deployment; and the two `unknown`/`unverifiable` values say the question
    # could not be answered at all — which is NOT the same as answering "no",
    # and collapsing them into one token would lose exactly that distinction.
    #
    # None of these repairs the ceiling. They disclose it, which is correct
    # under every candidate root cause and does not require choosing between
    # them. The fix itself needs someone who can observe Windows.
    "process_limit_not_enforced_child_escaped_the_job",
    "process_limit_not_enforced_ambient_job_allows_breakaway",
    "process_limit_membership_unverifiable_on_windows",
    "process_limit_enforcement_unknown_on_windows",
    # emitted on EVERY Windows run that assigns the child to the job
    # after creation. Not a detected failure — an admission that no parent-side
    # call can verify the ceiling applied. The four strings above can each prove
    # a failure; none of them can prove success, so silence stopped meaning
    # enforcement. Measured: this process sits in an EMPTY job (0x0) while its
    # own child reports 0x3000, so the ambient check is answering truthfully
    # about a topology the child is not in.
    "process_limit_enforcement_unverified_on_windows",
    # Opt-in via CODECALC_WIN_JOB_AT_CREATION=1. NOT a limitation —
    # the opposite. It records that the job was supplied at CREATION
    # (PROC_THREAD_ATTRIBUTE_JOB_LIST) rather than assigned afterwards, so our
    # job is the child's IMMEDIATE one and its ActiveProcessLimit is the one
    # consulted. It sits in this array because a caller reading `unenforced`
    # needs to know WHICH topology produced the run; without it, a measurement
    # cannot be attributed to the path that produced it.
    "process_limit_job_assigned_at_creation_on_windows",
    # windows.rs — opt-in via CODECALC_WIN_APPCONTAINER=1 and OFF by
    # default. Records that the run was launched inside a least-privilege
    # AppContainer (a SECURITY isolation boundary — payload denied the user
    # profile, the disk outside its workdir, and the network) layered on the
    # creation-time Job Object. It sits in `unenforced` because the
    # boundary is IMPLEMENTED but UNVERIFIED on real Windows 11: the author
    # cannot run Win11, and a Server-SKU CI runner can confirm the path compiles,
    # runs and discloses but NOT that the isolation holds. Distinct from the
    # Job Object's *resource* limits above, which are a different guarantee.
    "appcontainer_isolation_unverified_on_windows",
    # main.rs — the shim is missing, so --no-net would do nothing
    "no_net_requested_but_no_shim_available",
    # main.rs — only the bypassable LD_PRELOAD/dyld SYMBOL shim held (macOS, or a
    # Linux kernel without seccomp), not the in-kernel seccomp filter.
    # The Python layer (executor.py) expands this terse marker into the full
    # best-effort disclosure; seccomp-enforced runs emit NO no_net marker at all.
    "no_net_best_effort_shim",
}

#: The fallback's vocabulary. Prose rather than the Rust backend's snake_case
#: tokens, and deliberately not unified with it: the two are read by different
#: audiences (a token is matched by code, a sentence is read by an operator)
#: and forcing one spelling on both would be a cosmetic parity that hides the
#: real question, which is whether each backend discloses what IT cannot do.
KNOWN_FALLBACK_UNENFORCED = {
    "no_net: needs the native executor (seccomp where the Linux kernel supports it, a symbol shim otherwise)",
    "peak_memory_kb: ru_maxrss is a high-water mark and cannot be attributed to one run",
    "max_processes: RLIMIT_NPROC does not bind a process running as uid 0",
    # Windows: no setrlimit and no fork, so there is no hook to apply anything
    # through. The process entry is unconditional there — the fork-bomb guard
    # is always meant to be on, which is why its absence has to be stated.
    "max_processes: the Python fallback applies no process ceiling on Windows",
    "max_memory_mb: the Python fallback applies no memory ceiling on Windows",
    "max_cpu: the Python fallback applies no CPU ceiling on Windows",
    # macOS: setrlimit(RLIMIT_AS) SUCCEEDS and is then ignored, so nothing
    # raises and the ceiling looks applied. The Rust backend has carried
    # `memory_limit_not_enforced_on_macos` for this; the fallback did not.
    "max_memory_mb: setrlimit(RLIMIT_AS) is accepted and then ignored on macOS",
}

if not executor._rust:
    # NOT a skip. This whole file used to be `if _rust: <everything>`, so on a
    # machine without a built binary it printed one SKIP line and exited 0 —
    # a green check that covered nothing, on the backend with the FEWEST
    # guarantees. That is the gap #66 named, and a vacuous pass is worse than
    # no test because it reads as coverage.
    #
    # The contract is the same one stated at the top of this file, applied to
    # the other backend: what it cannot enforce must be NAMED, and what it does
    # not name must actually bite.
    print("=== pure-Python fallback contract (no native executor) ===")

    check("the fallback identifies itself as the backend in use",
          executor.backend() == "python", f"-> {executor.backend()}")

    clean = executor.execute("python3", "print(1)", timeout=15)
    check("every result carries the backend that produced it",
          clean.get("backend") == "python", f"-> {clean.get('backend')}")
    check("a fallback run returns an unenforced array",
          isinstance(clean.get("unenforced"), list), f"-> {clean.get('unenforced')!r}")
    check("everything it reports is from the documented vocabulary",
          set(clean.get("unenforced") or []) <= KNOWN_FALLBACK_UNENFORCED,
          f"-> {sorted(set(clean.get('unenforced') or []) - KNOWN_FALLBACK_UNENFORCED)}")

    # The core of #66: asking for network isolation the fallback cannot apply
    # must SAY so. codecalc's documented answer is to report rather than refuse
    # (SECURITY.md scopes the weaker fallback as documented behaviour, and
    # CODECALC_REQUIRE_NATIVE=1 is the fail-closed lever) — which only holds up
    # if the report is actually there.
    netted = executor.execute("python3", "print(1)", timeout=15, no_net=True)
    check("no_net=True on the fallback is reported as unenforced",
          any(u.startswith("no_net:") for u in netted.get("unenforced") or []),
          f"-> {netted.get('unenforced')}")
    # A caveat that appears whether or not it was asked for is noise, and noise
    # is how a caller learns to stop reading the field.
    check("  ...and is absent when no_net was not requested",
          not any(u.startswith("no_net:") for u in clean.get("unenforced") or []),
          f"-> {clean.get('unenforced')}")

    # Reporting a caveat about a number while also reporting the number would
    # be two contradictory claims in one result.
    check("peak_memory_kb is disclaimed and left unset, not both",
          any(u.startswith("peak_memory_kb:") for u in clean.get("unenforced") or [])
          and clean.get("peak_memory_kb") is None,
          f"-> unenforced={clean.get('unenforced')} value={clean.get('peak_memory_kb')}")

    # ── the other half: what it does NOT disclaim had better bite ──────────
    # The fallback does not name the memory, output or wall-clock ceilings in
    # `unenforced`, so each one is a live claim. Verdicts are asserted only
    # where both backends agree on them: a violated memory ceiling surfaces as
    # RTE here (the child dies of MemoryError under RLIMIT_AS) where the Rust
    # path reports MLE, so the assertion is that the ceiling STOPPED it, not
    # that the two spell the outcome identically.
    # Guarded the same way the Rust branch below guards its own: assert the
    # bite only where THIS platform claims the ceiling. Written unguarded
    # first, and CI was right to fail it — Windows applies no rlimits at all
    # and macOS accepts setrlimit(RLIMIT_AS) and ignores it, so on both the
    # 400MB allocation succeeded with verdict=OK. The fix was in the executor
    # (it now says so) and the guard here is what makes the assertion mean
    # "enforced where claimed" rather than "enforced everywhere".
    mem = executor.execute("python3", "x = bytearray(400*1024*1024); print('ALLOCATED')",
                           timeout=20, max_memory_mb=64)
    if any("max_memory_mb" in u for u in mem.get("unenforced") or []):
        skip("fallback memory ceiling", f"reported unenforced: {mem.get('unenforced')}")
    else:
        check("an undisclaimed memory ceiling actually stops the program",
              mem.get("ok") is False and "ALLOCATED" not in (mem.get("stdout") or ""),
              f"-> verdict={mem.get('verdict')} stdout={(mem.get('stdout') or '')[:40]!r}")

    out = executor.execute("python3", "print('x'*200000)", timeout=20, max_output_kb=8)
    check("an undisclaimed output ceiling actually truncates",
          out.get("verdict") == "OLE" and len(out.get("stdout") or "") < 200000,
          f"-> verdict={out.get('verdict')} bytes={len(out.get('stdout') or '')}")

    tle = executor.execute("python3", "while True: pass", timeout=3)
    check("an undisclaimed wall-clock ceiling actually kills",
          tle.get("verdict") == "TLE" and tle.get("timed_out") is True,
          f"-> verdict={tle.get('verdict')} timed_out={tle.get('timed_out')}")

    # The source's own list must not drift past what is documented above — the
    # same gate the Rust vocabulary gets, so a new caveat cannot be added
    # without being written down.
    # Exercise the ceiling-dependent branches too, not just the static list —
    # the entries that only appear when a limit was REQUESTED are exactly the
    # ones a static read would miss.
    produced = (set(executor._FALLBACK_UNMEASURED)
                | set(executor._unmeasured())
                | set(executor._unmeasured(max_memory_mb=64, max_cpu=5)))
    undocumented = produced - KNOWN_FALLBACK_UNENFORCED
    check("every string the fallback can emit is documented here",
          not undocumented, f"-> {sorted(undocumented)}")

    skip("native-executor contract", "no native executor built")
else:
    # ── the vocabulary in the source matches the vocabulary here ───────────
    rust_sources = [
        REPO_ROOT / "executor" / "src" / "main.rs",
        REPO_ROOT / "executor" / "src" / "platform" / "unix.rs",
        REPO_ROOT / "executor" / "src" / "platform" / "windows.rs",
    ]
    emitted: set[str] = set()
    for src in rust_sources:
        text = src.read_text(encoding="utf-8")
        # Two precise forms, not one loose one. A first draft used
        # `unenforced[^;]*?"..."` with DOTALL and swallowed whole statements
        # between an `unenforced` mention and the next string literal anywhere
        # in the file — it "found" 12 entries, two of which were paragraphs of
        # unrelated Rust.
        for m in re.finditer(r'unenforced\.push\(\s*"([^"]+)"\s*\)', text):
            emitted.add(m.group(1))
        for block in re.finditer(r'unenforced\s*=\s*vec!\[(.*?)\]', text, re.S):
            emitted.update(re.findall(r'"([^"]+)"', block.group(1)))
    check("the source emits at least one unenforced string", bool(emitted),
          f"-> the extractor found {len(emitted)}")
    unknown = emitted - KNOWN_UNENFORCED
    check("every unenforced string the source emits is documented here",
          not unknown, f"-> undocumented: {sorted(unknown)}")

    # ── a clean run on a capable platform claims nothing ───────────────────
    r = executor.execute("python3", "print(1)", timeout=15)
    reported = r.get("unenforced")
    check("a clean run returns an unenforced array", isinstance(reported, list),
          f"-> {reported!r}")
    check("everything reported is from the known vocabulary",
          set(reported or []) <= KNOWN_UNENFORCED, f"-> {reported}")
    if IS_LINUX:
        check("Linux enforces everything on a default run", reported == [],
              f"-> {reported}")

    # ── anything NOT reported unenforced must actually bite ────────────────
    # Each case makes a program exceed one ceiling. The assertion is that the
    # sandbox stopped it — not merely that the limit was set.
    def enforced(flag: str) -> bool:
        """Does this platform claim to enforce `flag` on this run?"""
        return not any(flag in u for u in (reported or []))

    r = executor.execute("python3", "import time; time.sleep(30)", timeout=3)
    check("the wall-clock timeout bites", r.get("timed_out") is True and r.get("verdict") == "TLE",
          f"-> verdict={r.get('verdict')} timed_out={r.get('timed_out')}")

    r = executor.execute("python3", 'print("x" * 300000)', max_output_kb=8, timeout=20)
    check("the output cap bites and is reported as OLE",
          r.get("verdict") == "OLE" and len(r.get("stdout") or "") < 20_000,
          f"-> verdict={r.get('verdict')} len={len(r.get('stdout') or '')}")

    cpu = executor.execute("python3", "x=0\nwhile True: x+=1", max_cpu=1, timeout=30)
    if enforced("cpu_limit"):
        # Killed on its CPU budget, not on the wall clock: timeout is 30s and it
        # must die around 1s of CPU.
        check("the CPU ceiling bites",
              cpu.get("ok") is False and cpu.get("timed_out") is not True,
              f"-> verdict={cpu.get('verdict')} cpu_ms={cpu.get('cpu_ms')} "
              f"timed_out={cpu.get('timed_out')}")
        check("  ...on CPU time, well inside the wall clock",
              (cpu.get("duration_ms") or 0) < 15_000,
              f"-> ran {cpu.get('duration_ms')}ms of a 30000ms timeout")
    else:
        skip("CPU ceiling", f"reported unenforced: {reported}")

    mem = executor.execute("python3", "b = bytearray(400_000_000); print(len(b))",
                           max_memory_mb=64, timeout=30)
    if enforced("memory_limit"):
        check("the memory ceiling bites",
              mem.get("ok") is False and "400000000" not in (mem.get("stdout") or ""),
              f"-> verdict={mem.get('verdict')} stdout={(mem.get('stdout') or '')[:40]!r}")
    else:
        # macOS accepts setrlimit(RLIMIT_AS) and ignores it, which is why this
        # is a documented entry rather than an assertion.
        skip("memory ceiling", f"reported unenforced: {reported}")

    # ── --no-net is honest in both directions ──────────────────────────────
    net = executor.execute(
        "python3",
        "import socket\n"
        "try:\n"
        "    s = socket.socket(); s.settimeout(4); s.connect(('1.1.1.1', 80))\n"
        "    print('EGRESS REACHED')\n"
        "except OSError as e:\n"
        "    print('blocked', e.errno)\n",
        no_net=True, timeout=25)
    net_unenforced_list = net.get("unenforced") or []
    # Three states the result can report for no_net, and they are not the same:
    #   shim_absent  — no blocking happened at all (own markers below)
    #   best_effort  — only the bypassable LD_PRELOAD/dyld SYMBOL shim held (E-1)
    #   enforced     — a seccomp-bpf filter blocked it in the KERNEL,
    #                  the strongest tier, and the ONLY one where no_net's
    #                  unenforced is legitimately empty.
    shim_absent = any(u in net_unenforced_list for u in
                       ("no_net_unavailable_on_windows",
                        "no_net_requested_but_no_shim_available"))
    best_effort = executor._NO_NET_BEST_EFFORT in net_unenforced_list
    if shim_absent:
        check("--no-net says so when it cannot be applied", True,
              f"-> {net_unenforced_list}")
    else:
        check("--no-net blocks egress (shim or seccomp)",
              "EGRESS REACHED" not in (net.get("stdout") or ""),
              f"-> {(net.get('stdout') or '').strip()[:60]!r}")
        if best_effort:
            # Shim path (macOS, or a Linux kernel without seccomp): a bypassable
            # symbol interposition, so the result MUST say so — never a silent
            # unenforced=[] that reads as "fully enforced".
            check("  ...and discloses the shim is best-effort (E-1)",
                  best_effort, f"-> {net_unenforced_list}")
        else:
            # Seccomp path: genuinely enforced in-kernel, so no_net
            # contributes NOTHING to unenforced. The disclosure must be ABSENT —
            # the guarantee is real, and claiming best-effort would now be a lie.
            check("  ...and when seccomp enforces it, no best-effort disclosure",
                  not best_effort, f"-> {net_unenforced_list}")

    # ── the ctypes/raw-syscall bypass: it defeated the shim (E-1), a seccomp
    #    filter closes it ────────────────────────────────────────────
    # blocknet.c interposes socket()/connect() at the SYMBOL level, which a
    # ctypes/dlsym call or a raw syscall resolves around — verified live in the
    # audit (socket.socket(AF_INET) -> EACCES(13), but libc.socket(2, 1, 0) -> a
    # working fd, same process, same no_net=True). A seccomp-bpf filter refuses
    # the socket() SYSCALL itself, which no userspace call can dodge. So the SAME
    # probe reaches a fd on the shim path and is BLOCKED on the seccomp path.
    if not IS_LINUX:
        skip("ctypes/raw no_net bypass (E-1)",
             f"seccomp is Linux-only; not probed on {sys.platform}")
    elif shim_absent:
        skip("ctypes/raw no_net bypass (E-1)",
             f"the shim itself was not applied on this run: {net_unenforced_list}")
    else:
        bypass = executor.execute(
            "python3",
            "import ctypes, ctypes.util, os\n"
            "libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)\n"
            "fd = libc.socket(2, 1, 0)\n"
            "if fd >= 0:\n"
            "    print('BYPASS', fd)\n"
            "    os.close(fd)\n"
            "else:\n"
            "    print('BLOCKED', ctypes.get_errno())\n",
            no_net=True, timeout=15)
        bypass_stdout = bypass.get("stdout") or ""
        if not best_effort:
            # Seccomp path: the filter refuses socket(AF_INET) at the syscall
            # boundary, so the raw ctypes call that bypassed the shim is now
            # blocked. Positive control: only assert BLOCKED-under-no_net if a raw
            # inet socket actually WORKS without no_net here (a netns with no
            # AF_INET would make the assertion vacuous — a broken filter also
            # yields EAFNOSUPPORT and would false-pass).
            inet_free = executor.execute(
                "python3",
                "import ctypes, ctypes.util\n"
                "libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)\n"
                "fd = libc.socket(2, 1, 0)\n"
                "print('OK', fd) if fd >= 0 else print('NO', ctypes.get_errno())\n",
                timeout=15)  # deliberately NOT no_net
            if "OK" not in (inet_free.get("stdout") or ""):
                skip("ctypes/raw socket() BLOCKED under seccomp no_net",
                     f"raw inet socket not reachable here: {(inet_free.get('stdout') or '').strip()!r}")
            else:
                check("ctypes/raw socket() is BLOCKED under seccomp no_net (the E-1 "
                      "bypass, closed at the syscall)",
                      "BLOCKED" in bypass_stdout,
                      f"-> stdout={bypass_stdout.strip()!r} "
                      f"stderr={(bypass.get('stderr') or '')[:200]!r}")
            # AF_UNIX must survive — the filter blocks inet, not all sockets.
            unix_ok = executor.execute(
                "python3",
                "import socket\na, b = socket.socketpair()\nprint('AF_UNIX ok')\n",
                no_net=True, timeout=15)
            check("  ...and AF_UNIX still works (filter blocks inet, not all sockets)",
                  "AF_UNIX ok" in (unix_ok.get("stdout") or ""),
                  f"-> {(unix_ok.get('stdout') or '').strip()[:40]!r}")
            # io_uring egress (found in review): a ring dispatches
            # IORING_OP_SOCKET/CONNECT INSIDE the kernel, reaching the network
            # without a socket()/connect() syscall the filter would see, so the
            # filter refuses io_uring_setup (ENOSYS). io_uring_setup is syscall
            # 425 on both x86_64 and aarch64. Positive control first: skip where
            # io_uring is unavailable rather than false-pass.
            iou_probe = (
                "import ctypes\n"
                "libc = ctypes.CDLL(None, use_errno=True)\n"
                "params = (ctypes.c_ubyte * 120)()\n"
                "fd = libc.syscall(425, 8, ctypes.byref(params))\n"
                "print('IOURING_OK' if fd >= 0 else 'IOURING_BLOCKED %d' % ctypes.get_errno())\n")
            iou_free = executor.execute("python3", iou_probe, timeout=15)  # not no_net
            if "IOURING_OK" not in (iou_free.get("stdout") or ""):
                skip("io_uring refused under seccomp no_net",
                     f"io_uring not available here: {(iou_free.get('stdout') or '').strip()!r}")
            else:
                iou_net = executor.execute("python3", iou_probe, no_net=True, timeout=15)
                # Assert the block POSITIVELY (the child printed IOURING_BLOCKED),
                # not just "not IOURING_OK": the filter's contract is a graceful
                # ENOSYS so runtimes fall back, and an empty stdout (a drift to
                # KILL) must fail this, not silently pass.
                check("io_uring is refused (ENOSYS) under seccomp no_net (no in-kernel "
                      "inet socket around the filter)",
                      "IOURING_BLOCKED" in (iou_net.get("stdout") or ""),
                      f"-> {(iou_net.get('stdout') or '').strip()!r}")
        elif "BLOCKED" in bypass_stdout:
            # Shim path, but libc's own socket() refused anyway (e.g. a netns
            # with no AF_INET at all): nothing to demonstrate a bypass with.
            # Not this test's failure class, so skip rather than fail.
            skip("ctypes/raw no_net bypass (E-1)",
                 f"libc socket() itself failed on this host: {bypass_stdout!r}")
        else:
            # Shim path: the bypass reaches a real fd, and the result discloses
            # the shim as best-effort (E-1), never unenforced=[].
            check("ctypes/dlsym socket() bypasses the no_net shim (E-1)",
                  "BYPASS" in bypass_stdout,
                  f"-> stdout={bypass_stdout.strip()!r} "
                  f"stderr={(bypass.get('stderr') or '')[:200]!r}")
            check("  ...and the result DISCLOSES no_net as best-effort",
                  executor._NO_NET_BEST_EFFORT in (bypass.get("unenforced") or []),
                  f"-> {bypass.get('unenforced')}")

    # ── the env allowlist must not make one platform second-class ──────────
    # It held 11 POSIX-oriented names and no Windows plumbing, so a process
    # started there had no SystemRoot — which winsock and crypto initialisation
    # need. `node` probed as available and returned empty output with ok=false
    # through the sandbox. Dropping a variable is a security decision; dropping
    # the ones that make the OS work is just a broken platform.
    allow = executor._ENV_ALLOWLIST
    for var in ("SystemRoot", "COMSPEC", "PATHEXT", "USERPROFILE"):
        check(f"the env allowlist carries {var} for Windows", var in allow)
    # And the things it exists to keep OUT are still out.
    for secret in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH", "GEM_HOME"):
        check(f"the env allowlist still excludes {secret}", secret not in allow)

    # ── the README's platform table describes the same executor ────────────
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    check("the README documents a per-platform support table",
          "| Guarantee | Linux | macOS | Windows |" in readme)
    # The claim that broke: Windows reported the CPU ceiling as unavailable when
    # job objects have supported it since XP. Whatever the table says, it must
    # not contradict what windows.rs emits.
    win = (REPO_ROOT / "executor" / "src" / "platform" / "windows.rs").read_text(encoding="utf-8")
    check("windows.rs applies a CPU ceiling", "JOB_OBJECT_LIMIT_PROCESS_TIME" in win)
    check("  ...and the README no longer calls it unavailable",
          "| CPU-time ceiling | `RLIMIT_CPU` | `RLIMIT_CPU` | reported unenforced |" not in readme)

# ── a runtime advertised on a host where it cannot run ───────────────────────
#
# Two independent defects stacked, and only fixing both closes the ticket.
#
#   1. bash FAILED on Windows. MSYS2 — which Git-for-Windows' bash is built on —
#      re-parses the raw command line treating `\` as an ESCAPE, so every
#      separator in the workdir path was eaten and bash got a garbage path
#      (exit 127, 100% reproducible on a desktop install).
#
#   2. bash was ADVERTISED anyway, because `probe()` decides availability from
#      resolution alone. `shutil.which` returning a path says the file is
#      there; it says nothing about whether running it works.
#
# The second is the one that matters. A missing runtime that says so is a fact
# about the host; a runtime advertised as present that fails every time is a
# model being told it can use a tool that never works — and `list_languages` is
# exactly the surface a model consults before choosing.
#
# These run on EVERY platform, not just Windows. `source_arg` takes `windows`
# as a parameter precisely so the Windows rendering is reachable from a Linux
# CI leg — the alternative is a check that silently no-ops on the three legs
# most likely to run it.
from codecalc import doctor, registry

check("a POSIX-argv language set is declared",
      len(registry.POSIX_ARGV_LANGUAGES) > 0,
      f"-> {sorted(registry.POSIX_ARGV_LANGUAGES)}")
check("  ...and bash is in it (the language the failure was measured on)",
      "bash" in registry.POSIX_ARGV_LANGUAGES)

# The actual repair: what these runtimes receive must have NO BACKSLASH left
# in it for MSYS to eat — not "no separator at all": the rendered path is now
# scratch-relative (registry.RUN_SCRATCH_DIRNAME + a forward slash), since the
# run step's cwd is the workdir root, one level above where the runner's own
# copy of the source actually lives. A `/` is not a `\`, so MSYS's
# re-tokenization leaves it untouched — that is the property this asserts,
# not "equals main.sh" or "equals a bare name".
_WIN_SRC = r"C:\Users\John Smith\AppData\Local\Temp\codecalc-ab12\main.sh"
for _lang in sorted(registry.POSIX_ARGV_LANGUAGES):
    rendered = registry.source_arg(_lang, _WIN_SRC, windows=True)
    check(f"{_lang}: the Windows source arg carries no backslash",
          "\\" not in rendered, f"-> {rendered!r}")
    check("  ...and is scratch-relative, naming the same file from the run "
          "step's cwd (the workdir root, one level above the scratch dir)",
          rendered == f"{registry.RUN_SCRATCH_DIRNAME}/main.sh", f"-> {rendered!r}")
    check("  ...and no space for the re-parse to split on",
          " " not in rendered, f"-> {rendered!r}")

# Unix argv is a real array, so nothing re-parses it and the absolute path is
# unambiguous. Changing it there would be churn with a blast radius and no bug.
_NIX_SRC = "/run/user/1000/codecalc-ab12/main.sh"   # not /tmp: ruff S108
check("on Unix the absolute path is left alone",
      registry.source_arg("bash", _NIX_SRC, windows=False) == _NIX_SRC)
# A language whose runtime takes argv as given is untouched on Windows too.
check("a non-POSIX-argv language is untouched on Windows",
      registry.source_arg("python3", _WIN_SRC, windows=True) == _WIN_SRC)

# ── and the advertising half ─────────────────────────────────────────────────
#
# `available` used to be computed from resolution and NAMED for execution. The
# field stays (clients read it) but it no longer carries the whole claim on its
# own: `status` says which of doctor's four states was reached and
# `status_basis` says which measurement produced it.
_langs = {l["name"]: l for l in executor.catalog()}
_sample = next(iter(_langs))
check("list_languages entries carry a status", "status" in _langs[_sample],
      f"-> {_langs[_sample].get('status')!r}")
check("  ...and say which measurement produced it",
      "status_basis" in _langs[_sample],
      f"-> {_langs[_sample].get('status_basis')!r}")
check("  ...drawn from doctor's vocabulary, not a fifth one",
      all(l["status"] in doctor.RUNTIME_STATES for l in _langs.values()),
      f"-> {sorted({l['status'] for l in _langs.values()})}")
# The whole point of the field: resolution must not be reported as execution.
check("a resolution-based report never claims `available`",
      all(l["status"] != "available" for l in _langs.values()
          if l["status_basis"] == "resolved"))
# doctor and list_languages are two views of one measurement. They disagreed
# before only because they used different words for it.
_doc = {r["name"]: r for r in doctor.report()["runtimes"]}
_mismatch = [n for n, r in _doc.items()
             if n in _langs and _langs[n]["status"] != r["status"]]
check("doctor and list_languages agree on every runtime's status",
      not _mismatch, f"-> {_mismatch[:5]}")

# ── shell-wrapped plans are a checkable fact, not an argv string ────────────
# `windows` is a parameter (the source_arg pattern), so every verdict below is
# exercised on this leg regardless of what this leg runs on.

check("csharp's plan is shell-free",
      registry.LANGUAGES["csharp"]["run"][0] == "dotnet",
      f"-> {registry.LANGUAGES['csharp']['run']}")
check("csharp is not in SHELL_WRAPPED", "csharp" not in registry.SHELL_WRAPPED)
check("wrapped plans are unsupported on Windows",
      all(not registry.plan_supported(n, windows=True)
          for n in registry.SHELL_WRAPPED))
check("wrapped plans keep their POSIX support",
      all(registry.plan_supported(n, windows=False)
          for n in registry.SHELL_WRAPPED))
check("an unknown language is unsupported everywhere",
      not registry.plan_supported("nosuchlang", windows=False))
check("every wrapped language names its real tool",
      set(registry.WRAPPED_TOOL) == set(registry.SHELL_WRAPPED))

# The probe must require the REAL tool, not just bash. Simulated by hiding the
# tool from resolution while bash stays present — the pre-fix probe answered
# True for gleam on exactly this machine shape.
_real_which = executor.shutil.which
try:
    executor.shutil.which = lambda cmd, path=None: (
        None if cmd in registry.WRAPPED_TOOL.values() else _real_which(cmd, path=path))
    # probe() prefers the Rust --probe; hide the binary for the duration so
    # the PYTHON fallback path is the one under test.
    _saved_rust = executor._rust
    executor._rust = None
    _p = executor.probe()
    executor._rust = _saved_rust
    check("a wrapper language without its tool probes unavailable",
          all(_p[n] is False for n in registry.SHELL_WRAPPED),
          f"-> {[(n, _p[n]) for n in registry.SHELL_WRAPPED]}")
finally:
    executor.shutil.which = _real_which
    executor._rust = _saved_rust

# The fallback executor refuses an unsupported plan with a structured error,
# not an exit-127 from a shell that is not there.
_saved_win = executor.IS_WINDOWS
try:
    executor.IS_WINDOWS = True
    _r = executor._execute_python("gleam", "io.println(42)")
    check("the fallback refuses a wrapped plan on Windows, structurally",
          _r.get("ok") is False and "unsupported on this platform" in _r.get("error", ""),
          f"-> {_r.get('error', '')[:80]}")
finally:
    executor.IS_WINDOWS = _saved_win

# ── release packaging fails closed, decided by a pure function ──────────────
# The end-to-end (CODECALC_REQUIRE_BINARY=1 + `uv build` refusing on a hidden
# binary/shim, degrading to a warned pure wheel without the switch) was proven
# by hand and is exercised by every release build; this pins the DECISION so it
# cannot regress without a red check.
sys.path.insert(0, str(REPO_ROOT))
from hatch_build import missing_release_artifacts

check("release artifacts complete -> nothing missing",
      missing_release_artifacts(True, "blocknet.so", True) == [])
check("a missing executor is named",
      missing_release_artifacts(False, "blocknet.so", True) == ["codecalc-exec"])
check("a missing shim is named",
      missing_release_artifacts(True, "blocknet.so", False) == ["blocknet.so"])
check("both missing -> both named",
      missing_release_artifacts(False, "blocknet.so", False)
      == ["codecalc-exec", "blocknet.so"])
check("no shim on Windows is the platform, not a gap",
      missing_release_artifacts(True, None, False) == [])
check("Windows still requires the executor",
      missing_release_artifacts(False, None, False) == ["codecalc-exec"])

# ── a spawn failure (missing runtime/compiler) is `runtime_unavailable`, ────
# not `internal`, on BOTH backends ──────────────────────────────────────────
# A Rust spawn failure populated `stderr` and never an `error` key, so
# `errors.ensure_code` — which classifies a failing result by matching TEXT
# IN `error`, never `stderr` — had nothing to match and the result came back
# `internal` instead of `runtime_unavailable`. The pure-Python fallback never
# had this gap: `_runtime_unavailable_result` always set `error` for the
# identical failure. Fixed in executor/src/main.rs (a new `spawn_error` field
# on `StepResult`, surfaced as the envelope's `error`) and
# codecalc/executor.py's Rust-result mapping (a `stderr`-prefix fallback for
# an older binary that answers without the new field).
#
# CODECALC_RUNTIME_PATH — not the real PATH — is what BOTH backends resolve a
# runtime from (see registry.runtime_path()'s docstring), so pointing it at
# an empty scratch directory makes a language's own runtime/compiler
# unresolvable, on THIS process's own PATH, without touching the real PATH
# the test process (or the stdio subprocess below) needs to run at all.
#
# "on this process's own PATH" is the important qualifier, not a redundant
# one: on Windows, CreateProcess resolves a bare argv[0] against the
# CALLING process's OWN ambient PATH, not the env block handed to the
# child — the override below changes what the CHILD sees once spawned, but
# not whether it is found in the first place. So a fixture language whose
# runtime/compiler is genuinely absent everywhere (lua, kotlin) is
# unaffected by that distinction, but "hide an ALREADY-INSTALLED tool via
# this override" is not a safe assumption cross-platform — see the kotlin
# fixture comment below for where that bit a `c`+gcc version of this test.
import json
import os
import shutil
import subprocess
import tempfile

from codecalc import errors, server, tools, translation

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _mcp_client import data, over_stdio

_NO_RUNTIME_DIR = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-noruntime-"))
_SAVED_RUNTIME_PATH = os.environ.get("CODECALC_RUNTIME_PATH")

# A fake, WORKING `java` planted in the same scratch directory — mirrors
# tests/test_executor_sweep.py's own kotlin fixture exactly (same
# fake-binary content and naming), not `c`+gcc's "hide the real compiler
# behind an empty PATH" approach: on Windows, CreateProcess resolves the
# executable named in argv[0] against the CALLING process's OWN ambient
# PATH, not the env block handed to the child (the classic Windows
# subprocess trap this repo's own notes on npm/tar already record) — so a
# REAL, already-installed compiler is not reliably hidden by overriding
# `CODECALC_RUNTIME_PATH` alone, and a Windows CI runner that happens to
# ship gcc (several do, via a bundled MinGW) went on to actually spawn it,
# which then failed for an unrelated reason (its OWN linker/assembler
# sub-processes could not resolve on the now-emptied PATH) — a genuine
# compile failure with no `error` key, not the spawn-failure shape this
# test means to exercise. `kotlinc` sidesteps the whole question: it is
# not bundled by ANY of the three runners this suite targets, so there is
# nothing ambient to accidentally find — its absence is a fact about the
# host, not something this fixture has to engineer via PATH tricks.
_fixture_java = _NO_RUNTIME_DIR / ("java.bat" if os.name == "nt" else "java")
if os.name == "nt":
    _fixture_java.write_text("@echo off\r\necho ok\r\nexit /b 0\r\n", encoding="utf-8")
else:
    _fixture_java.write_text("#!/bin/sh\necho ok\nexit 0\n", encoding="utf-8")
    _fixture_java.chmod(0o755)


def _spawn_failure(lang: str, code: str = "x") -> dict:
    os.environ["CODECALC_RUNTIME_PATH"] = str(_NO_RUNTIME_DIR)
    try:
        return server.execute_code(lang, code)
    finally:
        if _SAVED_RUNTIME_PATH is None:
            os.environ.pop("CODECALC_RUNTIME_PATH", None)
        else:
            os.environ["CODECALC_RUNTIME_PATH"] = _SAVED_RUNTIME_PATH


try:
    # lua: a single-command (interpreted) language — its `run` step IS the
    # spawn, so this exercises the "run" phase.
    _lua = _spawn_failure("lua")
    check("a missing single-command runtime is runtime_unavailable, not internal",
          _lua.get("code") == errors.RUNTIME_UNAVAILABLE,
          f"-> code={_lua.get('code')} error={str(_lua.get('error'))[:90]!r}")
    check("...and names the phase it failed in", _lua.get("phase") == "run",
          f"-> {_lua.get('phase')}")
    check("...with exit_code null (nothing spawned) per docs/contract/README.md",
          _lua.get("exit_code") is None, f"-> {_lua.get('exit_code')!r}")

    # kotlin: compile-then-run, with `kotlinc` genuinely absent (see the
    # fixture comment above for why this beats hiding a REAL compiler like
    # gcc behind a PATH override). No SHELL_WRAPPED entanglement either —
    # kotlin's plan is argv-only — so it is supported on every platform this
    # suite runs on; no skip branch is needed.
    if registry.plan_supported("kotlin", windows=(os.name == "nt")):
        _kt = _spawn_failure("kotlin", "fun main() { println(42) }")
        check("a missing compiler is runtime_unavailable, not internal",
              _kt.get("code") == errors.RUNTIME_UNAVAILABLE,
              f"-> code={_kt.get('code')} error={str(_kt.get('error'))[:90]!r}")
        check("...and names the compile phase", _kt.get("phase") == "compile",
              f"-> {_kt.get('phase')}")
        check("...with exit_code null too", _kt.get("exit_code") is None,
              f"-> {_kt.get('exit_code')!r}")
    else:
        skip("a missing compiler is runtime_unavailable",
             "kotlin is unsupported on this platform")

    # ── adversarial: a program's OWN stderr must never impersonate a spawn
    # failure ────────────────────────────────────────────────────────────
    # Found by adversarial review of the fallback's `stderr`-prefix shim
    # above: a python3 program that RUNS, prints text starting with
    # "Runtime unavailable" to its OWN stderr, and exits nonzero used to
    # come back classified `runtime_unavailable` with the install remedy —
    # the program's message overwrote `error`, and a real exit status (7,
    # not the -2 sentinel) was silently reclassified as "codecalc has no
    # runtime". This is a REAL execution, not a spawn failure, on the RUST
    # backend specifically — the backend whose result never carried an
    # `error` key at all before this fix, so its gate is the one this
    # regresses if it drifts back to matching text instead of `exit_code`.
    if executor._rust is not None:
        _adversarial = server.execute_code(
            "python3",
            'import sys; sys.stderr.write('
            '"Runtime unavailable: my custom database is down\\n"); sys.exit(7)')
        check("a program's own stderr cannot impersonate a spawn failure",
              _adversarial.get("code") != errors.RUNTIME_UNAVAILABLE,
              f"-> code={_adversarial.get('code')} "
              f"error={_adversarial.get('error')!r}")
        check("...it keeps its REAL exit code", _adversarial.get("exit_code") == 7,
              f"-> {_adversarial.get('exit_code')!r}")
        check("...and its verdict is an ordinary nonzero exit (RTE)",
              _adversarial.get("verdict") == "RTE", f"-> {_adversarial.get('verdict')!r}")
    else:
        skip("a program's own stderr cannot impersonate a spawn failure",
             "no native executor built, nothing to compare")

    # Parity: both backends agree, key-for-key, on the SAME spawn failure —
    # scripts/check_parity.py cannot reach this (it only runs the success
    # path), and test_contract.py's own compile-failure parity check exercises
    # a genuine compile ERROR (bad source), never a missing compiler.
    if executor._rust is None:
        skip("spawn-failure key parity across backends",
             "no native executor built, nothing to compare")
    else:
        _rust_spawn = _lua
        _saved_rust = executor._rust
        executor._rust = None
        try:
            _fb_spawn = _spawn_failure("lua")
        finally:
            executor._rust = _saved_rust
        check("the fallback also reports runtime_unavailable for the same failure",
              _fb_spawn.get("code") == errors.RUNTIME_UNAVAILABLE,
              f"-> {_fb_spawn.get('code')} {str(_fb_spawn.get('error'))[:80]!r}")
        check("both backends return the SAME keys for a spawn failure",
              set(_rust_spawn) == set(_fb_spawn),
              f"-> rust-only={sorted(set(_rust_spawn) - set(_fb_spawn))} "
              f"python-only={sorted(set(_fb_spawn) - set(_rust_spawn))}")
finally:
    shutil.rmtree(_NO_RUNTIME_DIR, ignore_errors=True)


# ── the same failure, serialised over a REAL stdio MCP round-trip ──────────
# The in-process checks above prove the dict this process builds; this proves
# the JSON a caller actually receives still carries the same phase/code/
# exit_code after going through `python -m codecalc.server`'s real framing —
# `tests/test_contract.py` makes the identical distinction for its own
# MCP round-trip checks, for the same reason (serialisation is where a null
# or a missing key shows up differently from the in-process dict).
async def _stdio_spawn_failure() -> None:
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-noruntime-stdio-"))
    try:
        async with over_stdio(env={"CODECALC_RUNTIME_PATH": str(scratch)}) as c:
            r = await c.call_tool("execute_code", {"language": "lua", "code": "print(1)"})
            payload = data(r)
            check("stdio: a missing runtime round-trips as runtime_unavailable",
                  isinstance(payload, dict)
                  and payload.get("code") == errors.RUNTIME_UNAVAILABLE,
                  f"-> {payload.get('code') if isinstance(payload, dict) else payload!r}")
            if isinstance(payload, dict):
                check("stdio: phase survives serialisation", payload.get("phase") == "run",
                      f"-> {payload.get('phase')}")
                check("stdio: exit_code is null over the wire",
                      payload.get("exit_code") is None, f"-> {payload.get('exit_code')!r}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


asyncio.run(_stdio_spawn_failure())


# ── compare_execution / compare_edge_cases: the SAME spawn-failure ─────────
# classification, one level down, on a per-language ROW rather than the
# top-level envelope — errors.stamp_row (codecalc/errors.py), not
# errors.ensure_code directly, since these tools' own `ok` stays `True`
# once the comparison ran and server.py's `_coded` wrapper never reaches a
# nested row. Real execution on BOTH backends, plus a stdio round-trip —
# the in-process spawn-failure checks above cannot stand in for this: they
# never call compare_execution/compare_edge_cases at all.
#
# Needs one sibling that still resolves alongside the hidden one, unlike
# the single-language `_spawn_failure` helper above — a scratch PATH
# holding a symlink to the REAL python3 (found on this process's own PATH
# before CODECALC_RUNTIME_PATH replaces it) makes `lua` the only language
# this dir cannot resolve, regardless of what else is actually installed.
_ROW_SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-row-noruntime-"))
_real_python3 = shutil.which("python3") or shutil.which("python")
if _real_python3 is None:
    skip("compare_execution/compare_edge_cases row classification",
         "no python3 on this PATH to use as the working sibling")
else:
    _py_link = _ROW_SCRATCH / (pathlib.Path(_real_python3).name)
    if os.name == "nt":
        shutil.copy2(_real_python3, _py_link)
    else:
        _py_link.symlink_to(_real_python3)

    def _row_for(language: str, dir_: pathlib.Path = _ROW_SCRATCH) -> dict:
        os.environ["CODECALC_RUNTIME_PATH"] = str(dir_)
        try:
            r = tools.compare_execution({"python3": "print(42)", "lua": "print(42)"})
        finally:
            if _SAVED_RUNTIME_PATH is None:
                os.environ.pop("CODECALC_RUNTIME_PATH", None)
            else:
                os.environ["CODECALC_RUNTIME_PATH"] = _SAVED_RUNTIME_PATH
        return next(row for row in r["results"] if row["language"] == language), r

    try:
        for _backend_name, _force_fallback in (("rust", False), ("python fallback", True)):
            _saved_rust = executor._rust
            if _force_fallback:
                executor._rust = None
            try:
                _lua_row, _full = _row_for("lua")
                _py_row = next(row for row in _full["results"] if row["language"] == "python3")
            finally:
                executor._rust = _saved_rust
            check(f"{_backend_name}: compare_execution's outer ok stays True",
                  _full["ok"] is True, f"-> {_full['ok']}")
            check(f"{_backend_name}: a missing-runtime row is runtime_unavailable",
                  _lua_row.get("code") == errors.RUNTIME_UNAVAILABLE,
                  f"-> code={_lua_row.get('code')} error={str(_lua_row.get('error'))[:90]!r}")
            check(f"{_backend_name}: ...and carries error text and a remedy",
                  bool(_lua_row.get("error")) and _lua_row.get("remedy") is not None,
                  f"-> {_lua_row.get('error')!r} / {_lua_row.get('remedy')!r}")
            check(f"{_backend_name}: a working sibling row carries no code/error/remedy",
                  not ({"code", "error", "remedy"} & _py_row.keys()),
                  f"-> {_py_row}")

        # Parity: the SAME row-level keys on both backends for the SAME
        # failure (scripts/check_parity.py never reaches compare_execution
        # at all — it only runs execute_code's own success path).
        if executor._rust is None:
            skip("compare_execution row-key parity across backends",
                 "no native executor built, nothing to compare")
        else:
            _rust_row, _ = _row_for("lua")
            _saved_rust = executor._rust
            executor._rust = None
            try:
                _fb_row, _ = _row_for("lua")
            finally:
                executor._rust = _saved_rust
            check("compare_execution: both backends return the SAME row keys for a spawn failure",
                  set(_rust_row) == set(_fb_row),
                  f"-> rust-only={sorted(set(_rust_row) - set(_fb_row))} "
                  f"python-only={sorted(set(_fb_row) - set(_rust_row))}")

        # A timeout row is classified `timeout`, from a message compare_
        # execution builds itself — never from the killed process's own
        # `stderr`, which is free to carry real (untrusted) program output.
        _timeout_row = next(
            row for row in tools.compare_execution(
                {"python3": "import time; time.sleep(3)"}, timeout=1)["results"]
            if row["language"] == "python3")
        check("compare_execution: a still-timed-out row is classified timeout",
              _timeout_row.get("code") == errors.TIMEOUT,
              f"-> {_timeout_row.get('code')} error={_timeout_row.get('error')!r}")

        # compare_edge_cases has the identical per-run classification, via
        # the same errors.stamp_row call.
        os.environ["CODECALC_RUNTIME_PATH"] = str(_ROW_SCRATCH)
        try:
            _edge = translation.compare_edge_cases(
                {"python3": "print(input())", "lua": "print(io.read())"}, inputs=["1"])
        finally:
            if _SAVED_RUNTIME_PATH is None:
                os.environ.pop("CODECALC_RUNTIME_PATH", None)
            else:
                os.environ["CODECALC_RUNTIME_PATH"] = _SAVED_RUNTIME_PATH
        _edge_runs = _edge["results"][0]["runs"]
        check("compare_edge_cases: outer ok stays True", _edge["ok"] is True, f"-> {_edge['ok']}")
        check("compare_edge_cases: a missing-runtime run is runtime_unavailable",
              _edge_runs["lua"].get("code") == errors.RUNTIME_UNAVAILABLE,
              f"-> {_edge_runs['lua'].get('code')}")
        check("compare_edge_cases: a working run carries no code/error/remedy",
              not ({"code", "error", "remedy"} & _edge_runs["python3"].keys()),
              f"-> {_edge_runs['python3']}")
    finally:
        shutil.rmtree(_ROW_SCRATCH, ignore_errors=True)


# ── the same row-level classification, over a REAL stdio MCP round-trip ────
async def _stdio_row_spawn_failure() -> None:
    if _real_python3 is None:
        skip("stdio: compare_execution row classification", "no python3 on this PATH")
        return
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-row-noruntime-stdio-"))
    try:
        link = scratch / pathlib.Path(_real_python3).name
        if os.name == "nt":
            shutil.copy2(_real_python3, link)
        else:
            link.symlink_to(_real_python3)
        async with over_stdio(env={"CODECALC_RUNTIME_PATH": str(scratch)}) as c:
            r = await c.call_tool(
                "compare_execution",
                {"snippets": {"python3": "print(42)", "lua": "print(42)"}})
            payload = data(r)
            check("stdio: compare_execution's outer ok survives serialisation",
                  isinstance(payload, dict) and payload.get("ok") is True,
                  f"-> {payload.get('ok') if isinstance(payload, dict) else payload!r}")
            if isinstance(payload, dict):
                _lua = next((row for row in payload.get("results", [])
                             if row.get("language") == "lua"), None)
                check("stdio: a missing-runtime row round-trips as runtime_unavailable",
                      _lua is not None and _lua.get("code") == errors.RUNTIME_UNAVAILABLE,
                      f"-> {_lua.get('code') if _lua else None}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


asyncio.run(_stdio_row_spawn_failure())


# ── execute_code: the envelope-level version of the SAME rule ──────────────
# errors.stamp_row (above) classifies compare_execution/compare_edge_cases'
# per-row failures; errors.ensure_code applies the identical rule to the
# top-level envelope every OTHER tool result passes through (execute_code,
# execute_code_stream, run_inspect, session_run — server.py's `_coded`
# wrapper). Real execution on BOTH backends: a plain `sys.exit(3)` RTE must
# carry no `code` at all (docs/contract/README.md's own "The program ran and
# failed" example), and a real wall-clock timeout must carry `code:
# "timeout"` — neither `executor.execute` result sets `error` for either
# outcome, which is exactly what used to fall through to `internal`.
for _backend_name, _force_fallback in (("rust", False), ("python fallback", True)):
    _saved_rust = executor._rust
    if _force_fallback:
        executor._rust = None
    try:
        _rte = server.execute_code("python3", "import sys; sys.exit(3)")
    finally:
        executor._rust = _saved_rust
    check(f"{_backend_name}: a real sys.exit(3) is verdict RTE, exit_code 3",
          _rte.get("verdict") == "RTE" and _rte.get("exit_code") == 3,
          f"-> verdict={_rte.get('verdict')!r} exit_code={_rte.get('exit_code')!r}")
    check(f"{_backend_name}: ...and carries NO code/remedy/code_inferred",
          not ({"code", "remedy", "code_inferred"} & _rte.keys()),
          f"-> {_rte}")

    _saved_rust = executor._rust
    if _force_fallback:
        executor._rust = None
    try:
        _tle = server.execute_code(
            "python3", "import time; time.sleep(3)", timeout=1)
    finally:
        executor._rust = _saved_rust
    check(f"{_backend_name}: a real wall-clock timeout is verdict TLE",
          _tle.get("verdict") == "TLE" and _tle.get("timed_out") is True,
          f"-> verdict={_tle.get('verdict')!r} timed_out={_tle.get('timed_out')!r}")
    check(f"{_backend_name}: ...and is classified timeout, not internal",
          _tle.get("code") == errors.TIMEOUT,
          f"-> code={_tle.get('code')} error={_tle.get('error')!r}")
    check(f"{_backend_name}: ...marked code_inferred, with the timeout remedy",
          _tle.get("code_inferred") is True
          and _tle.get("remedy") == errors.REMEDIES[errors.TIMEOUT],
          f"-> {_tle.get('code_inferred')!r} / {_tle.get('remedy')!r}")


# The same RTE, over a REAL stdio MCP round-trip — proves the JSON a caller
# actually receives has no `code` key at all (a `null` would still be a key
# present, which is not the same claim as absent).
async def _stdio_execute_code_rte() -> None:
    async with over_stdio() as c:
        r = await c.call_tool(
            "execute_code",
            {"language": "python3", "code": "import sys; sys.exit(3)"})
        payload = data(r)
        check("stdio: execute_code's real RTE survives serialisation",
              isinstance(payload, dict) and payload.get("verdict") == "RTE"
              and payload.get("exit_code") == 3,
              f"-> {payload!r}")
        if isinstance(payload, dict):
            check("stdio: ...with no code/remedy/code_inferred key at all",
                  not ({"code", "remedy", "code_inferred"} & payload.keys()),
                  f"-> {sorted(payload)}")


asyncio.run(_stdio_execute_code_rte())


# ── a COMPILE failure follows the identical RTE rule, on purpose ───────────
# `errors.ensure_code` treats `phase: "compile"` no differently from
# `phase: "run"` — a compile failure is a failed PROGRAM (its source has a
# syntax error), not a failed REQUEST: retrying the identical request cannot
# succeed either way. This is a DECISION, documented in `ensure_code`'s own
# docstring and in docs/contract/README.md's "The program ran and failed"
# section, not an accident of the verdict-based gate happening to also catch
# this shape.
#
# `c` (gcc, already relied on by the segfault fixture below, and present on
# all three CI runners) is the PRIMARY fixture — it exercises the
# fallback's own compile branch (`_fallback_compile_failure`) and the Rust
# binary's. `_COMPILE_TIMEOUT` (60s, against the 10s default) is deliberate
# headroom, not decoration: CI caught this fixture reporting `verdict: TLE`
# instead of `RTE` on a cold Windows runner's rustc (see below) because the
# 10s default was the compile budget too, on a runner slow enough that the
# ordinary case — a syntax error the compiler rejects almost immediately —
# still lost the race.
_COMPILE_TIMEOUT = 60
for _backend_name, _force_fallback in (("rust", False), ("python fallback", True)):
    _saved_rust = executor._rust
    if _force_fallback:
        executor._rust = None
    try:
        _ce = server.execute_code("c", "int main( { return 0 }", timeout=_COMPILE_TIMEOUT)
    finally:
        executor._rust = _saved_rust
    check(f"{_backend_name}: a C compile error is phase compile, verdict RTE",
          _ce.get("phase") == "compile" and _ce.get("verdict") == "RTE",
          f"-> phase={_ce.get('phase')!r} verdict={_ce.get('verdict')!r}")
    check(f"{_backend_name}: ...with the compiler's diagnostic in stderr",
          bool(_ce.get("stderr")), f"-> {_ce.get('stderr')!r}")
    check(f"{_backend_name}: ...and NO code/remedy/code_inferred",
          not ({"code", "remedy", "code_inferred"} & _ce.keys()),
          f"-> {_ce}")

# `rust` is the SECOND compiled language, per the review that asked for one
# beyond C — but only where `rustc` itself responds promptly: the CI
# failure above was rustc, specifically, timing out on a cold Windows
# runner (rustup's proxy binary, first invocation ever on that runner) —
# not a defect in this fixture's LOGIC, but a fixture measuring compiler
# startup latency it never meant to measure. Probing `rustc --version`
# first, with its own generous budget, and skipping loudly rather than
# guessing at a "safe enough" timeout, is what keeps this from being the
# same flake with a bigger number.
_rustc_responsive = False
if shutil.which("rustc"):
    try:
        _rustc_probe = subprocess.run(
            ["rustc", "--version"], capture_output=True, timeout=60, check=False)
        _rustc_responsive = _rustc_probe.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as _rustc_exc:
        skip("Rust compile-failure classification",
             f"rustc --version did not respond within 60s: {_rustc_exc!r}")
else:
    skip("Rust compile-failure classification", "no rustc on this PATH")

if _rustc_responsive:
    for _backend_name, _force_fallback in (("rust", False), ("python fallback", True)):
        _saved_rust = executor._rust
        if _force_fallback:
            executor._rust = None
        try:
            _ce = server.execute_code(
                "rust", "fn main() { let x = ; }", timeout=_COMPILE_TIMEOUT)
        finally:
            executor._rust = _saved_rust
        check(f"{_backend_name}: a Rust compile error is phase compile, verdict RTE",
              _ce.get("phase") == "compile" and _ce.get("verdict") == "RTE",
              f"-> phase={_ce.get('phase')!r} verdict={_ce.get('verdict')!r}")
        check(f"{_backend_name}: ...with the compiler's diagnostic in stderr",
              bool(_ce.get("stderr")), f"-> {_ce.get('stderr')!r}")
        check(f"{_backend_name}: ...and NO code/remedy/code_inferred",
              not ({"code", "remedy", "code_inferred"} & _ce.keys()),
              f"-> {_ce}")
elif shutil.which("rustc"):
    skip("Rust compile-failure classification", "rustc --version failed or was slow")


# ── the OTHER side of a compile-phase failure: a genuine TIMEOUT ───────────
# Observed for real in the CI run above: a compile that runs OUT OF TIME
# (rather than finishing and reporting a syntax error) is verdict TLE, code
# `timeout` — correct behaviour, and a DIFFERENT case from the "compile
# failure -> no code" rule two sections up, which only applies once the
# compiler actually finished and reported failure on its own terms. Pinned
# deterministically here with a fake `gcc` that busy-loops forever, rather
# than depending on how slow any REAL compiler happens to be on a given
# runner (which is exactly what made the CI failure this guards flaky in
# the first place). A pure shell/batch busy-loop, not `sleep`/`ping`: the
# sandboxed child's PATH is `CODECALC_RUNTIME_PATH` alone here (see
# registry.RUNTIME_PATH_ENV's precedence), so an external binary this fake
# compiler tried to exec would itself be unresolvable — measured: a first
# version calling `sleep` came back a fast RTE ("sleep: not found"), not
# the timeout this fixture means to exercise.
_slow_gcc_dir = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-slow-gcc-"))
try:
    if os.name == "nt":
        _slow_gcc = _slow_gcc_dir / "gcc.bat"
        _slow_gcc.write_text("@echo off\r\n:loop\r\ngoto loop\r\n", encoding="utf-8")
    else:
        _slow_gcc = _slow_gcc_dir / "gcc"
        _slow_gcc.write_text("#!/bin/sh\nwhile true; do :; done\n", encoding="utf-8")
        _slow_gcc.chmod(0o755)
    for _backend_name, _force_fallback in (("rust", False), ("python fallback", True)):
        os.environ["CODECALC_RUNTIME_PATH"] = str(_slow_gcc_dir)
        _saved_rust = executor._rust
        if _force_fallback:
            executor._rust = None
        try:
            _ct = server.execute_code("c", "int main(){return 0;}", timeout=2)
        finally:
            executor._rust = _saved_rust
            if _SAVED_RUNTIME_PATH is None:
                os.environ.pop("CODECALC_RUNTIME_PATH", None)
            else:
                os.environ["CODECALC_RUNTIME_PATH"] = _SAVED_RUNTIME_PATH
        check(f"{_backend_name}: a compile-phase TIMEOUT is verdict TLE, not RTE",
              _ct.get("phase") == "compile" and _ct.get("verdict") == "TLE"
              and _ct.get("timed_out") is True,
              f"-> phase={_ct.get('phase')!r} verdict={_ct.get('verdict')!r} "
              f"timed_out={_ct.get('timed_out')!r}")
        check(f"{_backend_name}: ...classified code timeout, not left uncoded",
              _ct.get("code") == errors.TIMEOUT, f"-> {_ct.get('code')}")
finally:
    shutil.rmtree(_slow_gcc_dir, ignore_errors=True)


# ── a signal-killed run: both backends must agree on exit_code, on EACH OS ──
# Before this fix, the Rust backend reported `exit_code: null` for a process
# killed BY A SIGNAL (indistinguishable from one that never spawned at all),
# while the pure-Python fallback already used POSIX/subprocess's own `-N`
# convention (`subprocess.Popen.returncode` is negative-signal on a signal
# death). `exit_code_json` in executor/src/main.rs now aligns the Rust
# backend to the SAME convention `subprocess.Popen` already uses on THIS
# platform — see its own doc comment.
#
# The exact VALUE is platform-specific, not a portable constant: a null-deref
# traps as SIGSEGV (11) on Linux glibc, but as SIGTRAP (5, i.e. -5) on macOS/
# Apple silicon — measured, not assumed, after CI caught a first version of
# this test hardcoding `-11` and failing identically on both backends on
# macOS. Windows has no signals at all (see platform/mod.rs's own doc
# comment on `signal`): an access violation is a real, POSITIVE `exit_code`
# — the NTSTATUS itself, `0xC0000005` / `3221225477` for
# STATUS_ACCESS_VIOLATION — which `exit_code_json` already passes through
# unchanged (`sr.signal` is always `None` there, so the `-N` branch never
# fires). What this test can portably assert: `exit_code` is never null (the
# process DID stop, just not via `exit()`), and — the parity that actually
# matters — both backends report the IDENTICAL value for the identical
# crash on the identical OS.
if shutil.which("gcc") or shutil.which("cc"):
    _segv_c = "int main() { int *p = 0; *p = 1; return 0; }"
    _segv_by_backend: dict[str, int | None] = {}
    for _backend_name, _force_fallback in (("rust", False), ("python fallback", True)):
        _saved_rust = executor._rust
        if _force_fallback:
            executor._rust = None
        try:
            _segv = server.execute_code("c", _segv_c)
        finally:
            executor._rust = _saved_rust
        check(f"{_backend_name}: a segfaulting program is verdict RTE",
              _segv.get("verdict") == "RTE", f"-> {_segv.get('verdict')!r}")
        check(f"{_backend_name}: ...with a real exit_code, not null "
              f"(the process stopped, just not via exit())",
              _segv.get("exit_code") is not None, f"-> {_segv.get('exit_code')!r}")
        if IS_WINDOWS:
            check(f"{_backend_name}: ...positive (the NTSTATUS access-"
                  f"violation code Windows reports; no signals to be "
                  f"negative FROM)",
                  isinstance(_segv.get("exit_code"), int) and _segv["exit_code"] > 0,
                  f"-> {_segv.get('exit_code')!r}")
        else:
            check(f"{_backend_name}: ...negative (the POSIX signal number "
                  f"that killed it — -11 SIGSEGV on Linux glibc, -5 SIGTRAP "
                  f"on macOS/Apple silicon for the identical null-deref; "
                  f"either is a real signal death, never null)",
                  isinstance(_segv.get("exit_code"), int) and _segv["exit_code"] < 0,
                  f"-> {_segv.get('exit_code')!r}")
        check(f"{_backend_name}: ...and NO code/remedy/code_inferred",
              not ({"code", "remedy", "code_inferred"} & _segv.keys()),
              f"-> {_segv}")
        _segv_by_backend[_backend_name] = _segv.get("exit_code")
    check("both backends report the IDENTICAL exit_code for the identical "
          "crash on this OS — the parity that matters, not a hardcoded "
          "cross-platform constant",
          len(set(_segv_by_backend.values())) == 1, f"-> {_segv_by_backend}")
else:
    skip("signal-death exit_code parity", "no gcc/cc on this PATH")


# ── SIGINT is signal 2 — the EXACT value the OLD Rust wire format used as
# its "nothing spawned" sentinel (`exit_code: -2`) ─────────────────────────
# `exit_code_json`'s new `-N` convention (above) means a program killed BY
# SIGINT now legitimately reports `exit_code: -2` — the same NUMBER
# `codecalc/executor.py`'s compat shim (from #283) used to treat as proof
# that nothing spawned at all. Reproduced by cross-vendor review: a
# SIGINT-killed C or python3 program came back `code: "runtime_unavailable"`,
# `error: "runtime unavailable for the run phase"`, `exit_code: null`,
# remedy "install the runtime" — a request that ran a real program and was
# genuinely killed, told it needed a runtime installed. Fixed with a THIRD,
# ALWAYS-present JSON key from the binary, `spawn_error` (null when nothing
# went wrong at spawn, the message otherwise — distinct from `error`, which
# stays conditional and caller-facing): the shim now gates on `exit_code ==
# -2` AND `"spawn_error" not in result` — a binary built after this fix
# carries the key on EVERY result, so the shim can never fire against one,
# regardless of what `exit_code` says. See `codecalc/executor.py`'s updated
# comment on the gate, and `spawn_error`'s doc comment on
# `executor/src/main.rs`'s `StepResult`.
if shutil.which("gcc") or shutil.which("cc"):
    _sigint_c = "#include <signal.h>\nint main() { raise(SIGINT); return 0; }\n"
    _r = server.execute_code("c", _sigint_c)
    check("rust: a SIGINT-killed C program is verdict RTE",
          _r.get("verdict") == "RTE", f"-> {_r.get('verdict')!r}")
    check("rust: ...with a real exit_code, not null",
          _r.get("exit_code") is not None, f"-> {_r.get('exit_code')!r}")
    if not IS_WINDOWS:
        check("rust: ...specifically -2 on POSIX (SIGINT is signal 2)",
              _r.get("exit_code") == -2, f"-> {_r.get('exit_code')!r}")
    check("rust: ...and NO code/error/remedy/code_inferred — a real "
          "signal death, not a spawn failure",
          not ({"code", "error", "remedy", "code_inferred"} & _r.keys()),
          f"-> {_r}")
else:
    skip("SIGINT-killed C program classification", "no gcc/cc on this PATH")

_sigint_py = ("import os, signal\n"
              "os.kill(os.getpid(), signal.SIGINT)\n")
_r = server.execute_code("python3", _sigint_py)
check("rust: a SIGINT-killed python3 program is verdict RTE",
      _r.get("verdict") == "RTE", f"-> {_r.get('verdict')!r}")
check("rust: ...with a real exit_code, not null",
      _r.get("exit_code") is not None, f"-> {_r.get('exit_code')!r}")
if not IS_WINDOWS:
    check("rust: ...specifically -2 on POSIX (SIGINT is signal 2)",
          _r.get("exit_code") == -2, f"-> {_r.get('exit_code')!r}")
check("rust: ...and NO code/error/remedy/code_inferred — a real "
      "signal death, not a spawn failure",
      not ({"code", "error", "remedy", "code_inferred"} & _r.keys()),
      f"-> {_r}")


# ── the shim itself, pinned against SYNTHETIC binary JSON ───────────────────
# The two real-execution checks above prove the end-to-end behaviour on
# whatever binary this run happens to have; this pins the SHIM'S OWN
# decision rule directly, independent of any real compiler/runtime, so a
# future change to the gate's boolean logic fails here even if no signal
# happens to fire on the CI host that runs it.
class _FakeRustProc:
    """Stands in for `_popen_group`'s return value: `execute()` only ever
    calls `.communicate()` on it."""

    def __init__(self, stdout_json: dict) -> None:
        self._out = json.dumps(stdout_json).encode()

    def communicate(self, input=None, timeout=None):
        return self._out, b""


def _shim_result_for(payload: dict) -> dict:
    """Via `server.execute_code`, not `executor.execute` directly: `code`
    comes from `errors.ensure_code`, which only runs at `server.py`'s
    `_coded` wrapper — `executor.execute` itself never calls it (only
    `contract.stamp`), so asserting on `code` against the lower-level
    entry point would test nothing about the shim's actual effect on what
    a caller receives.

    `server.execute_code` also probes `--capabilities` (a SEPARATE
    `_popen_group` call, `no_net_kernel_enforcement_available` via
    `providers.describe`) before it ever gets to the run itself — the fake
    below only answers the RUN invocation (`--lang` in argv) and delegates
    anything else to the REAL `_popen_group`, or this fixture would break
    the capability probe rather than exercising the shim at all.
    """
    saved_rust = executor._rust
    real_popen_group = executor._popen_group

    def _fake_popen_group(argv):
        if "--lang" in argv:
            return _FakeRustProc(payload)
        return real_popen_group(argv)

    executor._rust = "/fake/codecalc-exec"
    executor._popen_group = _fake_popen_group
    try:
        return server.execute_code("python3", "print(1)")
    finally:
        executor._rust = saved_rust
        executor._popen_group = real_popen_group


_old_binary_spawn_failure = {
    "ok": False, "language": "python3", "phase": "run",
    # The wording a binary built after #280 (worded stderr) but before
    # #283 (the JSON `error` KEY) produces — the shim's own comment says
    # `stderr_text` is used AS-IS whenever it has content, "every binary
    # since #280 has one" (a bare, wordless spawn failure predates #280
    # entirely and is the fallback branch below this dict, not this one).
    # A message with NEITHER "runtime unavailable" NOR any `_MESSAGE_HINTS`
    # entry ahead of it in the list would test the wrong thing here: this
    # fixture's whole point is that `error` already says the right thing
    # in plain text, and the shim's job is only to SURFACE it as `error`.
    "stdout": "", "stderr": "runtime unavailable for the run phase: 'python3' not found (os error 2)",
    "exit_code": -2, "duration_ms": 3, "compile_ms": 0, "total_ms": 3,
    "cpu_ms": 0, "peak_memory_kb": 0, "timed_out": False,
    "output_truncated": False, "verdict": "RTE", "output_error": None,
    "stdout_bytes": None, "stderr_bytes": None,
    "platform": "linux", "workdir": "<synthetic-workdir>",
    # NO "spawn_error" key at all — the exact shape a binary built before
    # this fix emits. This is the ONLY thing that should make the shim fire.
}
_old = _shim_result_for(_old_binary_spawn_failure)
check("shim: an OLD binary's -2/no-spawn_error-key shape still classifies "
      "runtime_unavailable",
      _old.get("code") == errors.RUNTIME_UNAVAILABLE, f"-> {_old.get('code')}")
check("shim: ...exit_code normalised to null (the documented 'nothing "
      "spawned' convention)",
      _old.get("exit_code") is None, f"-> {_old.get('exit_code')!r}")

_new_binary_sigint = dict(_old_binary_spawn_failure)
_new_binary_sigint["stderr"] = ""
_new_binary_sigint["spawn_error"] = None  # present, null — a NEW binary, SIGINT (-2), no spawn failure
_new = _shim_result_for(_new_binary_sigint)
check("shim: a NEW binary's identical -2 with spawn_error PRESENT (null) "
      "does NOT fire the compat branch",
      _new.get("code") != errors.RUNTIME_UNAVAILABLE, f"-> {_new.get('code')}")
check("shim: ...exit_code is left as the real signal value, -2, not "
      "normalised to null",
      _new.get("exit_code") == -2, f"-> {_new.get('exit_code')!r}")
check("shim: ...and spawn_error itself never reaches the returned result "
      "(internal to this JSON handshake only)",
      "spawn_error" not in _new, f"-> {sorted(_new)}")

# `execute_code_stream` does NOT go through `_execute_uncontracted`: it has
# its own read-the-binary's-JSON path in `executor.execute_stream`, which is
# exactly where the review of this fix found the key still leaking (present,
# null) while every other surface — execute_code, run_inspect, session_run,
# compare_execution rows — had it popped. A real run on the Rust backend,
# since the Python fallback never sets the key and would pass vacuously.
if executor._rust is not None:
    _streamed = asyncio.run(server.execute_code_stream(
        "python3", "print('hi')", timeout=20))
    check("execute_code_stream: spawn_error never reaches the streamed "
          "result either (its own JSON path pops it too)",
          _streamed.get("ok") is True and "spawn_error" not in _streamed,
          f"-> ok={_streamed.get('ok')!r} keys={sorted(_streamed)}")
else:
    skip("execute_code_stream spawn_error pop", "no Rust binary resolved")


# ── compact mode must classify IDENTICALLY to the full envelope ────────────
# `execute_code(..., compact=True)` used to call `errors.ensure_code` (via
# `_coded`, server.py's tool-registration wrapper) AFTER `compact_result`
# had already built a fresh dict that dropped `error` — so a `compact=True`
# call to a "rejected before execution" shape (no `verdict`/`stdout`/
# `exit_code` to fall back on) had NOTHING left to classify from and came
# back `code: "internal"` regardless of the real cause. `execute_code` now
# runs `ensure_code` BEFORE `compact_result` (see both docstrings), and
# `code`/`error`/`remedy`/`code_inferred` are in `_COMPACT_DISCLOSURE` so
# they survive the compaction that follows.
for _backend_name, _force_fallback in (("rust", False), ("python fallback", True)):
    _saved_rust = executor._rust
    if _force_fallback:
        executor._rust = None
    try:
        _full = server.execute_code("nosuchlang", "x")
        _compact = server.execute_code("nosuchlang", "x", compact=True)
    finally:
        executor._rust = _saved_rust
    check(f"{_backend_name}: an unknown-language compact result matches "
          f"the full one's code",
          _compact.get("code") == _full.get("code") == errors.VALIDATION,
          f"-> full={_full.get('code')} compact={_compact.get('code')}")
    check(f"{_backend_name}: ...and still carries error/remedy",
          bool(_compact.get("error")) and bool(_compact.get("remedy")),
          f"-> error={_compact.get('error')!r} remedy={_compact.get('remedy')!r}")

_COMPACT_NORUNTIME_DIR = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-compact-noruntime-"))
if _real_python3 is None:
    skip("compact mode: missing-runtime classification", "no python3 on this PATH")
else:
    _compact_py_link = _COMPACT_NORUNTIME_DIR / pathlib.Path(_real_python3).name
    if os.name == "nt":
        shutil.copy2(_real_python3, _compact_py_link)
    else:
        _compact_py_link.symlink_to(_real_python3)
    try:
        for _backend_name, _force_fallback in (("rust", False), ("python fallback", True)):
            os.environ["CODECALC_RUNTIME_PATH"] = str(_COMPACT_NORUNTIME_DIR)
            _saved_rust = executor._rust
            if _force_fallback:
                executor._rust = None
            try:
                _full = server.execute_code("lua", "print(1)")
                _compact = server.execute_code("lua", "print(1)", compact=True)
            finally:
                executor._rust = _saved_rust
                if _SAVED_RUNTIME_PATH is None:
                    os.environ.pop("CODECALC_RUNTIME_PATH", None)
                else:
                    os.environ["CODECALC_RUNTIME_PATH"] = _SAVED_RUNTIME_PATH
            check(f"{_backend_name}: a missing-runtime compact result matches "
                  f"the full one's code",
                  _compact.get("code") == _full.get("code") == errors.RUNTIME_UNAVAILABLE,
                  f"-> full={_full.get('code')} compact={_compact.get('code')}")
    finally:
        shutil.rmtree(_COMPACT_NORUNTIME_DIR, ignore_errors=True)

for _backend_name, _force_fallback in (("rust", False), ("python fallback", True)):
    _saved_rust = executor._rust
    if _force_fallback:
        executor._rust = None
    try:
        _compact_tle = server.execute_code(
            "python3", "import time; time.sleep(3)", timeout=1, compact=True)
    finally:
        executor._rust = _saved_rust
    check(f"{_backend_name}: a compact timeout is classified timeout too",
          _compact_tle.get("code") == errors.TIMEOUT and _compact_tle.get("verdict") == "TLE",
          f"-> code={_compact_tle.get('code')} verdict={_compact_tle.get('verdict')!r}")

for _backend_name, _force_fallback in (("rust", False), ("python fallback", True)):
    _saved_rust = executor._rust
    if _force_fallback:
        executor._rust = None
    try:
        _compact_rte = server.execute_code(
            "python3", "import sys; sys.exit(3)", compact=True)
    finally:
        executor._rust = _saved_rust
    check(f"{_backend_name}: a compact plain RTE still carries no code",
          "code" not in _compact_rte and _compact_rte.get("verdict") == "RTE"
          and _compact_rte.get("exit_code") == 3,
          f"-> {_compact_rte}")

print(f"\n=== {len(FAILS)} FAILURE(S), {len(SKIPS)} skipped ===" if FAILS else
      f"\n=== PLATFORM CONTRACT HOLDS ({len(SKIPS)} skipped) ===")
sys.exit(1 if FAILS else 0)
