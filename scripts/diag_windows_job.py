#!/usr/bin/env python3
r"""Why the Windows process ceiling does not bind (THE-818), in one command.

Windows-only. Pure `ctypes` — no build beyond `codecalc-exec.exe` itself, which
is the point: the reproduction has to be runnable on a box that has the binary
and nothing else.

WHAT THIS REPLACES. `jobprobe` built its own job object and measured that. It
reported the ceiling BINDING at 23/400 on a box where the shipped binary spawned
400/400, twice, from two launchers — and that was read as "does not reproduce".
A diagnostic that reimplements the code under test measures the
reimplementation. This one drives `codecalc-exec` and instruments the process
INSIDE the sandbox, so every number below is about the shipped path.

THE MEASUREMENT THIS EXISTS TO SETTLE. On the failing host the child reports:

    LIMIT_FLAGS    0x00003000   = KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK
    ACTIVE_PROCESS_LIMIT 0
    SPAWNED        400 of 400
    JOB_ACCT       total=1 active=1     <- that job holds ONE process

codecalc sets `0x230A` with `ActiveProcessLimit=24`. So the job governing the
child is not the job codecalc configured, and the descendants land in no job at
all — `SILENT_BREAKAWAY_OK` doing exactly what it documents.

AND THE OPEN QUESTION IT ANSWERS. `platform/windows.rs` discloses
`process_limit_not_enforced_ambient_job_allows_breakaway` when it finds
`SILENT_BREAKAWAY_OK` — but it asks `GetCurrentProcess()`, the EXECUTOR, not
the child. On the failing host that disclosure does not fire while the child
plainly sees `0x3000`. Either the two observe different jobs, or the check is
answering correctly about the wrong process. The PARENT block below prints what
that function sees, next to what the child sees, which decides it:

    parent 0x3000 too   -> the check ran and returned the wrong answer
    parent differs      -> they are in different jobs; the check must
                           interrogate the CHILD
    parent not in a job -> the ambient model is wrong entirely

PROVENANCE, stated because it matters: the child-side payload is RECONSTRUCTED
from the fields a Windows 11 Pro session reported (LIMIT_FLAGS, APL, SPAWNED,
CHILD0_IN_JOB, JOB_ACCT), not transcribed from that session's file. If the
numbers it prints disagree with theirs on the same box, trust theirs and fix
this — the reconstruction is the thing under suspicion, not the measurement.

Exit codes deliberately skip 1, so a harness failure cannot be mistaken for a
finding (the lesson from #141):

    0  the ceiling BOUND — the bug did not reproduce here
    3  the ceiling did NOT bind — reproduced, and the table says how
    2  not Windows, or codecalc-exec is missing; nothing was measured
    1  never returned by this program
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

#: Attempts the sandboxed program makes. 400 matches the original report so the
#: numbers are comparable rather than merely similar.
SPAWNS = 400

#: The ceiling handed to the executor. 24 is the original report's value.
LIMIT = 24

# ── the payload that runs INSIDE the sandbox ────────────────────────────────
#
# Everything here is measured from the process codecalc actually confined. It
# reports the job it is IN, not the job we think we put it in — that difference
# is the entire finding.
PAYLOAD = r'''
import ctypes, ctypes.wintypes as w, subprocess, sys

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ULONG_PTR = ctypes.c_size_t

class BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", w.DWORD),
                ("Affinity", ULONG_PTR),
                ("PriorityClass", w.DWORD),
                ("SchedulingClass", w.DWORD)]

class IOC(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

class EXTENDED(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IOC),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]

class ACCT(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", w.DWORD),
                ("TotalProcesses", w.DWORD),
                ("ActiveProcesses", w.DWORD),
                ("TotalTerminatedProcesses", w.DWORD)]

EXTENDED_INFO, ACCT_INFO = 9, 1

def in_job(handle=None):
    b = ctypes.c_int(0)
    h = handle if handle is not None else k32.GetCurrentProcess()
    ok = k32.IsProcessInJob(h, None, ctypes.byref(b))
    return bool(b.value) if ok else None

def query(cls, cls_id):
    """NULL job handle = 'the job this process is in', per the API contract."""
    buf, ret = cls(), w.DWORD(0)
    ok = k32.QueryInformationJobObject(None, cls_id, ctypes.byref(buf),
                                       ctypes.sizeof(buf), ctypes.byref(ret))
    return buf if ok else None

out = {}
out["IN_JOB"] = in_job()
ext = query(EXTENDED, EXTENDED_INFO)
out["LIMIT_FLAGS"] = ext.BasicLimitInformation.LimitFlags if ext else None
out["ACTIVE_PROCESS_LIMIT"] = ext.BasicLimitInformation.ActiveProcessLimit if ext else None

# Held open, not started-and-reaped: ActiveProcessLimit bounds CONCURRENT
# processes, so releasing each one would measure spawn throughput instead.
kids = []
err = ""
try:
    while len(kids) < __SPAWNS__:
        kids.append(subprocess.Popen([sys.executable, "-c",
                                      "import time; time.sleep(30)"]))
except Exception as exc:
    err = "%s: %s" % (type(exc).__name__, exc)
out["SPAWNED"] = len(kids)
out["FIRST_ERROR"] = err

# Is a GRANDCHILD in any job? If the descendants break away, this is False and
# the ceiling has nothing to count.
try:
    PROCESS_QUERY_LIMITED = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, kids[0].pid) if kids else None
    out["CHILD0_IN_JOB"] = in_job(h) if h else None
    if h:
        k32.CloseHandle(h)
except Exception as exc:
    out["CHILD0_IN_JOB"] = "error: %s" % exc

# How many processes the OBSERVED job has ever held. total=1 means this job is
# accounting for this process alone — it is not the job the tree is in.
acct = query(ACCT, ACCT_INFO)
out["JOB_TOTAL_PROCESSES"] = acct.TotalProcesses if acct else None
out["JOB_ACTIVE_PROCESSES"] = acct.ActiveProcesses if acct else None

for k in kids:
    try:
        k.kill()
    except Exception:
        pass

print("CODECALC_JOBDIAG " + repr(out))
'''


def main() -> int:
    if not sys.platform.startswith("win"):
        print("diag_windows_job measures Windows Job Object behaviour and there "
              "is nothing here to measure.\nIt exits 2 rather than printing an "
              "empty table: a diagnostic that reports 'no problem found' on a "
              "platform without job objects is worse than one that declines.",
              file=sys.stderr)
        return 2

    from codecalc import executor

    exe = executor._rust
    if not exe:
        print("::error::no native executor (codecalc-exec). Build it first: "
              "cargo build --release --manifest-path executor/Cargo.toml",
              file=sys.stderr)
        return 2

    # ── PARENT: what ambient_job_allows_breakaway() actually sees ───────────
    #
    # This is the block that decides whether the shipped disclosure is asking
    # about the right process. It mirrors platform/windows.rs:134 exactly.
    print("=== PARENT (what the executor's own check sees) ===")
    parent = _parent_job_facts()
    for k, v in parent.items():
        print(f"  {k:24} {_fmt(k, v)}")

    # ── CHILD: what the sandboxed process is actually subject to ────────────
    print(f"\n=== CHILD (inside the sandbox, limit={LIMIT}, spawns={SPAWNS}) ===")
    proc = subprocess.run(
        [str(exe), "--lang", "python3", "--timeout", "120"],
        input=PAYLOAD.replace("__SPAWNS__", str(SPAWNS)),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
        env={**_os_environ(), executor.MAX_PROCESSES_ENV: str(LIMIT)},
    )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("::error::the executor did not return JSON — this is a harness "
              f"failure, not a finding.\nstdout: {proc.stdout[:400]}\n"
              f"stderr: {proc.stderr[:400]}", file=sys.stderr)
        return 2

    child = _parse_payload(result.get("stdout") or "")
    if child is None:
        print("::error::the payload did not report. Harness failure, not a "
              f"finding.\nstdout: {(result.get('stdout') or '')[:400]}\n"
              f"stderr: {(result.get('stderr') or '')[:400]}", file=sys.stderr)
        return 2
    for k, v in child.items():
        print(f"  {k:24} {_fmt(k, v)}")

    # ── what codecalc TOLD the caller ──────────────────────────────────────
    unenforced = result.get("unenforced") or []
    disclosed = [u for u in unenforced if "process_limit" in u]
    print("\n=== WHAT THE RESULT CLAIMED ===")
    print(f"  ok                       {result.get('ok')}")
    print(f"  verdict                  {result.get('verdict')}")
    print(f"  unenforced               {unenforced}")

    # ── verdict ────────────────────────────────────────────────────────────
    spawned = child.get("SPAWNED")
    bound = isinstance(spawned, int) and spawned < LIMIT
    print("\n=== VERDICT ===")
    print(f"  ceiling {'BOUND' if bound else 'DID NOT BIND'} — "
          f"{spawned} of {SPAWNS} spawned against a limit of {LIMIT}")

    if not bound and not disclosed:
        # The whole ticket in one line: a guarantee absent AND unreported.
        print("  *** AND NOTHING WAS DISCLOSED. codecalc reported ok with no ***")
        print("  *** process_limit_* entry — a FALSE claim, not a missing one. ***")
    elif not bound:
        print(f"  disclosed: {disclosed}  (honest: the gap is reported)")

    pf, cf = parent.get("LIMIT_FLAGS"), child.get("LIMIT_FLAGS")
    print("\n  parent vs child job:")
    if pf is None or cf is None:
        print("    one side could not be read — inconclusive")
    elif pf == cf:
        print(f"    SAME job flags (0x{pf:08X}). If SILENT_BREAKAWAY_OK is set "
              "here,\n    the shipped check should have fired: debug "
              "ambient_job_allows_breakaway.")
    else:
        print(f"    DIFFERENT jobs — parent 0x{pf:08X}, child 0x{cf:08X}.")
        print("    The shipped check asks GetCurrentProcess() and reports about")
        print("    the child. It must interrogate the CHILD instead.")

    return 0 if bound else 3


def _os_environ() -> dict:
    import os

    return dict(os.environ)


def _fmt(key: str, value: object) -> str:
    if key.endswith("LIMIT_FLAGS") and isinstance(value, int):
        return f"0x{value:08X}"
    return repr(value)


def _parse_payload(stdout: str) -> dict | None:
    import ast

    for line in stdout.splitlines():
        if line.startswith("CODECALC_JOBDIAG "):
            try:
                return ast.literal_eval(line.split(" ", 1)[1])
            except (ValueError, SyntaxError):
                return None
    return None


def _parent_job_facts() -> dict:
    """Mirrors platform/windows.rs::ambient_job_allows_breakaway, in ctypes."""
    import ctypes
    import ctypes.wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", w.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", w.DWORD),
                    ("SchedulingClass", w.DWORD)]

    class IOC(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in
                    ("ReadOperationCount", "WriteOperationCount",
                     "OtherOperationCount", "ReadTransferCount",
                     "WriteTransferCount", "OtherTransferCount")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IOC),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    b = ctypes.c_int(0)
    ok = k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(b))
    facts: dict = {"IN_JOB": bool(b.value) if ok else None}

    buf, ret = EXTENDED(), w.DWORD(0)
    if k32.QueryInformationJobObject(None, 9, ctypes.byref(buf),
                                     ctypes.sizeof(buf), ctypes.byref(ret)):
        facts["LIMIT_FLAGS"] = buf.BasicLimitInformation.LimitFlags
        facts["ACTIVE_PROCESS_LIMIT"] = buf.BasicLimitInformation.ActiveProcessLimit
        # 0x1000 — the flag the shipped check keys on.
        facts["SILENT_BREAKAWAY_OK"] = bool(
            buf.BasicLimitInformation.LimitFlags & 0x1000)
    else:
        facts["LIMIT_FLAGS"] = None
        facts["ACTIVE_PROCESS_LIMIT"] = None
        facts["SILENT_BREAKAWAY_OK"] = None
    return facts


if __name__ == "__main__":
    sys.exit(main())
