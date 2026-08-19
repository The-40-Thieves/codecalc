#!/usr/bin/env python3
"""THE-818 natural-inheritance probe (Windows-only, standalone ctypes).

The executor measurements settled everything except one question. codecalc puts
the sandbox CHILD in its job (in_our_job=1), but that child's descendants never
join the job -- our job's peak ActiveProcesses stays at 1 while 400 grandchildren
spawn. Every mechanism tried established the child's *membership* via explicit
assignment (post-creation AssignProcessToJobObject, creation-time
PROC_THREAD_ATTRIBUTE_JOB_LIST, +CREATE_BREAKAWAY_FROM_JOB) and none made the
tree inherit the limit.

This probe tests the one structurally different topology left: a process that is
GENUINELY in a job (self-assigned) spawns children the ordinary way. Does the
job's ActiveProcessLimit then govern those children (natural inheritance)?

  * If BOUND -> membership-by-inheritance propagates where membership-by-
    assignment did not, and the real fix is a launcher process placed in the job
    so the sandbox tree is created as its descendant.
  * If NOT BOUND -> even a self-assigned job does not bound a spawned tree here,
    and the honest conclusion is that Job-Object ActiveProcessLimit cannot be
    relied on for codecalc's shape on Windows 11; the ceiling must be reported
    UNENFORCED.

Standalone and disposable: no executor involved, the job carries only
ACTIVE_PROCESS (no KILL_ON_JOB_CLOSE), the probe kills its children and exits.

Exit: 0 = BOUND, 3 = NOT bound, 2 = setup error / not Windows.
"""

import ctypes
import ctypes.wintypes as w
import subprocess
import sys

if not sys.platform.startswith("win"):
    print("jobprobe_natural: not Windows -> nothing measured")
    sys.exit(2)

#: Small on purpose so a bound job is unmistakable: with the probe itself
#: counting as 1, at most LIMIT-1 children may spawn if the limit governs.
LIMIT = 5
SPAWN = 20

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ULONG_PTR = ctypes.c_size_t

# Declare the ABI. ctypes defaults restype to c_int, which TRUNCATES a 64-bit
# HANDLE and silently breaks every later use of it -- the exact class of bug
# this whole investigation keeps re-finding.
k32.GetCurrentProcess.restype = w.HANDLE
k32.GetCurrentProcess.argtypes = []
k32.CreateJobObjectW.restype = w.HANDLE
k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k32.SetInformationJobObject.restype = w.BOOL
k32.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
k32.AssignProcessToJobObject.restype = w.BOOL
k32.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
k32.IsProcessInJob.restype = w.BOOL
k32.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
k32.QueryInformationJobObject.restype = w.BOOL
k32.QueryInformationJobObject.argtypes = [
    w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)
]


class BASIC(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", w.DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class IOC(ctypes.Structure):
    _fields_ = [
        (n, ctypes.c_uint64)
        for n in ("read_op", "write_op", "other_op", "read_tx", "write_tx", "other_tx")
    ]


class EXTENDED(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BASIC),
        ("IoInfo", IOC),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class ACCT(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", w.DWORD),
        ("TotalProcesses", w.DWORD),
        ("ActiveProcesses", w.DWORD),
        ("TotalTerminatedProcesses", w.DWORD),
    ]


EXTENDED_INFO, ACCT_INFO = 9, 1
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x0000_0008


def die(msg: str) -> None:
    print(f"jobprobe_natural: {msg} (err={ctypes.get_last_error()})")
    sys.exit(2)


job = k32.CreateJobObjectW(None, None)
if not job:
    die("CreateJobObjectW failed")

ext = EXTENDED()
ext.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_ACTIVE_PROCESS
ext.BasicLimitInformation.ActiveProcessLimit = LIMIT
if not k32.SetInformationJobObject(job, EXTENDED_INFO, ctypes.byref(ext), ctypes.sizeof(ext)):
    die("SetInformationJobObject failed")
if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
    die("AssignProcessToJobObject(self) failed")

in_flag = w.BOOL(0)
k32.IsProcessInJob(k32.GetCurrentProcess(), job, ctypes.byref(in_flag))
self_in_job = bool(in_flag.value)

kids = []
first_error = ""
try:
    while len(kids) < SPAWN:
        kids.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]))
except Exception as exc:  # noqa: BLE001 -- the point is to catch the spawn refusal
    first_error = f"{type(exc).__name__}: {exc}"

acct = ACCT()
ret = w.DWORD(0)
active = None
if k32.QueryInformationJobObject(job, ACCT_INFO, ctypes.byref(acct), ctypes.sizeof(acct), ctypes.byref(ret)):
    active = acct.ActiveProcesses

print(
    f"NATURAL-PROBE: self_in_job={self_in_job} limit={LIMIT} "
    f"spawned={len(kids)}/{SPAWN} job_active_processes={active} first_error={first_error!r}"
)

for p in kids:
    try:
        p.kill()
    except OSError:
        pass

# The probe itself counts as 1, so a governing limit permits at most LIMIT-1
# children before CreateProcess is refused.
if len(kids) <= LIMIT:
    print("VERDICT: BOUND -- children inherit the self-assigned job; ActiveProcessLimit governs the tree")
    sys.exit(0)
print("VERDICT: NOT BOUND -- children escape even a self-assigned job (deeper Windows limitation)")
sys.exit(3)
