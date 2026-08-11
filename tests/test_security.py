"""Security regression tests: the two confirmed exploits must stay dead,
plus sandbox guarantees (env isolation, fork-bomb, output caps, var caps)."""
import json
import os
import pathlib
import re
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from codecalc import executor, logic, tools

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# 1. CVE-2026-codecalc-001: truth_table eval escape (host RCE via __subclasses__)
r = logic.truth_table("().__class__.__base__.__subclasses__()")
check("truth_table blocks dunder-chain RCE", not r.get("ok"),
      f"-> {r.get('error','UNEXPECTED OK')[:80]}")
r = logic.truth_table("Popen")
check("truth_table rejects bare callable refs", r.get("ok") and r["row_count"] == 2)
r = logic.truth_table("a and b or not c")
check("truth_table still works (a and b or not c)", r.get("ok") and r["row_count"] == 8)
r = logic.truth_table("p xor q")
check("truth_table xor works", r.get("ok") and r["row_count"] == 4 and r["rows"][1]["result"] is True)

# 2. var-count cap: 2^n blowup guard
big = " or ".join(f"v{i}" for i in range(20))
r = logic.truth_table(big)
check("truth_table caps variables (2^n guard)", not r.get("ok") and "too many variables" in r.get("error", ""))

# 3. expression length cap
r = logic.truth_table("a " * 3000)
check("truth_table caps expression length", not r.get("ok"))

# 4. env isolation: executed code must NOT see host secrets
marker = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "SECRET_SHOULD_NOT_LEAK")
r = executor.execute("python3", "import os; print('LEAK' if os.environ.get('GITHUB_PERSONAL_ACCESS_TOKEN') else 'CLEAN')")
check("executed code sees NO host secrets", "CLEAN" in r.get("stdout", ""), f"-> {r.get('stdout','')[:40]!r}")

# 4b. the allowlist has to survive the FILTER, not merely be declared (THE-802)
#
# `os.environ` UPPER-CASES every key on Windows — CPython's os.py does
# `data[encodekey(key)] = value` with `encodekey = key.upper()` — so a
# membership test against a mixed-case entry never matches there and the
# variable is silently dropped from the child's environment.
#
# Measured against the real list, exactly ONE name was affected: `windir`. It is
# one of the two the allowlist's own comment says were added because "node
# returned empty output with ok=false through the sandbox on Windows while
# probing as available". `SystemRoot` survived only because `SYSTEMROOT` happens
# to be listed too. So the fix that made node work on Windows was half-applied
# on this backend, while the Rust one always passed both — `std::env::var` is
# case-insensitive there because the OS is.
#
# scripts/check_parity.py compares the two DECLARED lists and they are
# identical, which is why it could not see this: a declaration is not a
# behaviour. Asserted here against the real filter instead, with the Windows
# casing simulated so the property holds on every runner rather than only the
# one where it bites.
_win_cased = {
    "SystemRoot": r"C:\Windows", "windir": r"C:\Windows",
    "ComSpec": r"C:\Windows\system32\cmd.exe", "PATHEXT": ".COM;.EXE",
    "TEMP": r"C:\Temp", "USERPROFILE": r"C:\Users\x", "PATH": r"C:\Windows",
}
_as_python_sees = {k.upper(): v for k, v in _win_cased.items()}   # os.environ on nt
_kept = {k for k in _as_python_sees if k.upper() in {n.upper() for n in executor._ENV_ALLOWLIST}}
_dropped = sorted(set(_as_python_sees) - _kept)
check("every allowlist name survives Windows key normalisation",
      not _dropped,
      f"-> dropped {_dropped}; os.environ upper-cases keys on nt, so a "
      f"mixed-case entry is unreachable there")

# And the filter itself must do it, not just the list. Run the REAL `_env()`
# against a Windows-cased environment rather than re-deriving the rule here —
# re-deriving it is how the first version of this check compared the list to
# itself and passed whatever the code did.
class _FakeOS:
    """`os` with a substituted `environ`; everything else falls through.

    Swapping the module reference rather than assigning to `os.environ`, which
    does not clear the real environment and which ruff flags (B003) for exactly
    that reason.
    """

    def __init__(self, environ):
        self.environ = environ

    def __getattr__(self, name):
        return getattr(os, name)


_saved_os = executor.os
_saved_is_windows = executor.IS_WINDOWS
try:
    executor.os = _FakeOS(_as_python_sees)   # what Python would hand us on nt
    executor.IS_WINDOWS = True
    _passed = executor._env()
finally:
    executor.os = _saved_os
    executor.IS_WINDOWS = _saved_is_windows
check("_env() passes windir through on Windows",
      "WINDIR" in _passed, f"-> kept {sorted(_passed)}")

# 5. env allowlist still lets runtimes work (PATH + a home directory present)
#
# This asserted os.environ['HOME'] on every platform, which Windows does not
# have: _ENV_ALLOWLIST carries USERPROFILE and APPDATA and its own comment calls
# them "the Windows spelling of HOME". So the assertion encoded a Unix
# assumption and read as a sandbox defect on Windows (#75).
#
# The property worth checking is the same everywhere — executed code can still
# find its home directory after the allowlist has dropped everything else — so
# the NAME is platform-specific and the property is not. Both spellings are
# accepted rather than branching on sys.platform in the child, because a
# Windows runner that also sets HOME should not thereby fail.
_HOME_VARS = "('HOME', 'USERPROFILE')"
r = executor.execute(
    "python3",
    "import os\n"
    f"home = [v for v in {_HOME_VARS} if os.environ.get(v)]\n"
    "print('PATH' in os.environ, home)\n",
)
# Detail on BOTH outcomes. This check used to pass no detail at all, so its CI
# failure said only that something about PATH or HOME was wrong — not which,
# and not what the child actually saw. That cost a round trip.
check("allowlist keeps PATH + a home directory functional",
      r.get("stdout", "").startswith("True [")
      and r.get("stdout", "").strip() != "True []",
      f"-> stdout={r.get('stdout','').strip()!r} "
      f"backend={r.get('backend')} verdict={r.get('verdict')} "
      f"stderr={str(r.get('stderr',''))[:80]!r}")

# 6. infinite loop killed by timeout
r = executor.execute("python3", "while True: pass", timeout=3)
check("infinite loop killed by timeout", r.get("timed_out") is True)

# 7. output cap
r = executor.execute("python3", "print('x' * 200000)")
check("output capped at 64KiB", len(r.get("stdout", "")) < 70_000)

# 8. no eval/exec/shell=True anywhere in the package (excluding docstrings).
# Exception: _worker_bootstrap.py's `exec()` runs user code inside the
# isolated REPL subprocess (allowlisted env, separate process group) — the
# same threat model as the Rust executor, never in the server process.
import ast

bad = []
for p in (REPO_ROOT / "codecalc").rglob("*.py"):
    if p.name == "_worker_bootstrap.py":
        continue  # documented: exec in the isolated worker subprocess only
    tree = ast.parse(p.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("eval", "exec"):
            bad.append(f"{p.name}:{node.lineno}")
check("zero eval/exec/shell=True in codebase", not bad, f"-> {bad}")

# 9. benchmark fit still works (no eval regression). Empirical timing is
# noisy at small sizes — the security-relevant property is that it detects
# polynomial growth (not O(1)/O(log n)); exact degree may wobble.
#
# THE-808: the largest size was n=8000, which is 64 million iterations of a
# pure-Python double loop. Measured at 8.6s against this call's own 15s
# timeout on an unloaded 4-core ARM box: a margin of 1.7x. The one observed
# failure was macos-latest on 3.11, the slowest leg in the matrix, while 3.14
# passed in the same run.
#
# A timeout here is not a slow measurement that widens an error bar. `_measure`
# ABORTS the whole benchmark at the first one, deliberately, because a
# timeout's wall clock was once appended as data and reported as a complexity
# class. The result then carries `error` and NO `estimate` key, so
# `.get("estimate", "")` returned "" and this check failed printing nothing at
# all — three lines of CI output that could not distinguish "classified wrong"
# from "never produced a classification".
#
# n=4000 measures 2.1s, a 7.2x margin, classifies identically as O(n^2), and
# takes 9s instead of 35s per matrix leg.
r = tools.benchmark("import sys\nn=int(sys.stdin.readline())\ns=0\nfor i in range(n):\n    for j in range(n): s+=i*j\nprint(s)",
                    sizes="500,1000,2000,4000", timeout=15)
est = r.get("estimate", "")
check("benchmark detects polynomial growth (no eval regression)",
      "O(n" in est and est not in ("O(1)", "O(log n)"),
      f"-> {est}" if est
      else f"-> NO ESTIMATE (ok={r.get('ok')}): {r.get('error')!r}")

# ── the safety screen must run BEFORE any parse ───────────────────────────
# SymPy's parse_expr/sympify EVALUATE what they parse — its own docstring says
# "this function uses ``eval``, and thus shouldn't be used on unsanitized
# input" — so safe_expr.reject_unsafe is the only thing between a caller
# string and an evaluator.
#
# That makes ORDER a security property, and order was previously guaranteed
# only by which line came first in evaluate_expression. #67 added a SECOND
# parse (`evaluate=False`, to bound cost before doing the arithmetic), which
# is exactly the kind of edit that quietly reorders things: `evaluate=False`
# suppresses ARITHMETIC, not `eval`. So it is asserted rather than read.
#
# Upstream's own view of screens like ours, from the abandoned attempt to add
# `safe=` to sympify() (sympy/sympy#12524, never merged): "security theater
# that leads users into a false sense of security, because it can still be
# bypassed". Which is the argument for checking that ours at least runs.
# GUARDED, because sympy is an extra and this is a module-scope import.
#
# Unguarded, a base install (`pip install codecalc`, or `uv sync` without
# --all-extras) made this suite TRACEBACK after 14 passing assertions and exit
# 1. In a `for f in tests/test_*.py` loop that reads as a FAILURE — the suite
# claiming a security regression — when the truth is that the host does not
# have the optional dependency the check needs.
#
# Every other suite here already guards its capability gaps: test_bug_sweep.py
# carries five SKIP branches, test_executor_sweep.py maintains a SKIPS list and
# reports the count. The package even ships codecalc/optional.py with a
# MissingExtra class for exactly this. This one import was the exception, and it
# was found by running the suite on a base install on a real machine — CI never
# saw it, because CI's lockfile was installing sympy as a core dependency.
try:
    import sympy.parsing.sympy_parser as _spp
    _HAVE_SYMPY = True
except ImportError as _exc:
    _spp = None
    _HAVE_SYMPY = False
    print(f"SKIP the safe_expr screen-order checks — sympy is not installed "
          f"({_exc}); it ships in the 'symbolic' extra. The screen itself is "
          f"gated by scripts/check_no_eval.py, which needs no dependencies.")

_real_parse_expr = _spp.parse_expr if _HAVE_SYMPY else None
_reached_parser = []


if _HAVE_SYMPY:
    def _spy_parse_expr(s, *args, **kwargs):
        _reached_parser.append(s)
        return _real_parse_expr(s, *args, **kwargs)


    _spp.parse_expr = _spy_parse_expr
    try:
        # POSITIVE CONTROL FIRST. A spy that records nothing would make every
        # assertion below vacuously true — "the hostile string never reached the
        # parser" is worthless if NOTHING reaches the parser. Prove the instrument
        # works before trusting what it does not see.
        _reached_parser.clear()
        _ok = logic.evaluate_expression("2+2")
        check("control: a SAFE expression does reach parse_expr",
              _ok.get("ok") is True and len(_reached_parser) > 0,
              f"-> ok={_ok.get('ok')} parse calls={len(_reached_parser)}")

        for _hostile in ("__import__('os').system('id')",
                         "().__class__.__base__.__subclasses__()",
                         "x.__class__",
                         "lambda: 1"):
            _reached_parser.clear()
            _r = logic.evaluate_expression(_hostile)
            check(f"screened before the parser: {_hostile[:34]!r}",
                  _r.get("ok") is False and not _reached_parser,
                  f"-> ok={_r.get('ok')} reached_parser={_reached_parser}")
    finally:
        _spp.parse_expr = _real_parse_expr

# 10. fork-bomb guard. LAST IN THE FILE, and that placement is load-bearing.
#
# These two tests are DESTRUCTIVE: between them they spawn up to the process
# limit and those children take time to drain. Anything running afterwards
# competes for the same budget. On a macOS runner the bomb reached 1012
# processes and the benchmark two tests later came back with an empty estimate —
# a real failure caused entirely by test order. The same shape bit this suite
# once already, between the two fork-bomb tests themselves.
#
# Within the pair, the counter runs BEFORE the recursive bomb, and that order
# is load-bearing. RLIMIT_NPROC is now computed per execution as (ambient uid
# tasks + headroom), measured once at spawn. Run this after a recursive bomb and
# its children are still draining, so the ambient figure it was sized against
# has since dropped and the counter gets MORE room than the headroom — measured
# at 721 children against a headroom of 512 in exactly that order. Quiet box
# first, then the messy test.
#
# The recursive bomb in 6b can only tell you the run STOPPED, which the wall
# clock also does. This forks non-recursively and reports how many children it
# got before EAGAIN, which is the number that says the limit actually held.
# The children park so they hold their slot while the parent counts, then the
# parent KILLS THE WHOLE GROUP once it has the number. Without that they are
# orphaned and keep holding process slots after the run: on a macOS runner this
# left ~1080 parked processes, and the CI step's own shell could not fork
# afterwards — the suite printed ALL TESTS PASS and the step exited 128.
# The count is flushed to stdout (a file, from the executor's side) before the
# signal, so killing ourselves does not lose it.
# `signal.pause()`, not `os.pause()`. THE LATTER HAS NEVER EXISTED — the POSIX
# pause is exposed on the signal module, and Python's os module has no such
# attribute on any version. So every forked child raised
#
#     AttributeError: module 'os' has no attribute 'pause'
#
# printed a traceback, and died instead of parking. The probe still reported a
# number because the parent kept forking until EAGAIN, so what it actually
# measured was how many children can be alive while each is busy writing a
# traceback — load-dependent, and not the parked-process ceiling it claims.
#
# It surfaced on macOS in #75: the tracebacks blew the 64 KiB output cap, the
# executor killed the run for OLE (exit -15) BEFORE the parent could print, and
# the count came back as None. Linux was winning that race and passing.
#
# `os._exit(0)` is belt and braces. A child that ever fell out of the park —
# pause returning on a signal, or raising — would drop into the parent's `while
# True` and start forking itself, turning a bounded probe into a real fork bomb.
# The try/except keeps stderr empty so the cap is never the thing that decides
# the result.
_COUNTER = """import os, signal, sys
n = 0
try:
    while True:
        if os.fork() == 0:
            try:
                signal.pause()   # child parks; only the parent counts
            except Exception:
                pass
            os._exit(0)          # never rejoin the parent's loop
        n += 1
except OSError:
    print(f"EAGAIN_AFTER {n}", flush=True)
    os.killpg(os.getpgrp(), signal.SIGKILL)   # take the parked children with us
    sys.exit(3)
"""
# The probe uses os.fork/os.pause/os.killpg, none of which exist on Windows, so
# it produces no count there and the assertion below read that as a breach. The
# guarantee on Windows is a different primitive entirely — a job object's
# ActiveProcessLimit, which is JOB-scoped and therefore stronger than
# RLIMIT_NPROC's uid-wide budget — and verifying it needs a probe that spawns
# processes rather than forking. Not written blind: skipped, and named as an
# unverified gap rather than passed over quietly.
_POSIX = os.name != "nt"
_probe = None
if _POSIX:
    _probe = executor.execute("python3", _COUNTER, timeout=30)
    _m = re.search(r"EAGAIN_AFTER (\d+)", _probe.get("stdout", "") or "")
    _children = int(_m.group(1)) if _m else None
else:
    _children = None

# The bound to expect depends on which path computed the limit, and asserting
# the Linux one everywhere is how a correct sandbox looks broken. On Linux the
# ambient task count is measurable, so the limit is ambient+headroom and the
# ambient tasks consume nearly all of it — the bomb gets ~headroom. On macOS
# there is no cheap /proc equivalent, so nproc_limit() falls back to a FIXED
# ceiling and the (small) ambient count leaves most of it available: measured at
# 1080 children on a macOS runner against a headroom of 512, which is the
# fallback working exactly as designed, not a breach.
_measured = executor.current_uid_tasks() is not None
_limit = executor.nproc_limit()
_headroom = executor.DEFAULT_PROCESS_HEADROOM
# ── the process limit, on EVERY platform ──────────────────────────────────
# The fork probe above is POSIX-only, so the Windows guarantee — a job object's
# ActiveProcessLimit, a different primitive entirely — went unverified. This
# probe SPAWNS rather than forks, which is the portable operation, and pins the
# ceiling low through CODECALC_MAX_PROCESSES so it costs two dozen short-lived
# processes instead of walking up to a 4096 fallback on a CI runner.
#
# The ceiling has to be platform-appropriate or the probe tests nothing, and the
# property that decides it is whether the limit is UID-WIDE or JOB-SCOPED — not,
# as a first version assumed, whether the ambient count is measurable:
#
#   Windows   job-scoped ActiveProcessLimit   -> a flat 24 means 24
#   Linux     uid-wide RLIMIT_NPROC, ambient measurable -> ambient + 24
#   macOS     uid-wide RLIMIT_NPROC, ambient NOT measurable -> no safe low
#             ceiling exists; a flat 24 sits far below the ambient count and the
#             sandboxed program cannot spawn at all. Measured: 0 children, which
#             the "did it actually run" assertion below caught as the vacuous
#             pass it was.
#
# So macOS is skipped here and covered by the fork probe, which measures the
# boundary precisely and needs no ceiling of its own.
_SPAWN_HEADROOM = 24
#: How many the probe TRIES. Spawning fewer than this is the evidence that the
#: ceiling bound it; the exact number depends on how busy the uid is and is not
#: something to assert against.
_SPAWN_ATTEMPTS = 400
#: Tasks the rest of the machine may start during the probe without making the
#: consistency check below wrong. Generous on purpose: it is a sanity bound on
#: the ceiling, not a measurement of the scheduler.
_CHURN_TOLERANCE = 32
_ambient = executor.current_uid_tasks()
_JOB_SCOPED = os.name == "nt"
_spawn_limit = _SPAWN_HEADROOM if _JOB_SCOPED else (
    (_ambient + _SPAWN_HEADROOM) if _ambient is not None else None)

# The attempt cap is interpolated, not written twice. The assertion below
# compares against _SPAWN_ATTEMPTS, and a probe that tried a different number
# than the test believes would make that comparison quietly meaningless.
_SPAWNER = f"""import subprocess, sys
kids = []
try:
    while len(kids) < {_SPAWN_ATTEMPTS}:
        kids.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"]))
except Exception:
    pass
print("SPAWNED", len(kids), flush=True)
for k in kids:
    k.kill()
"""
# THE-775: this probe is the ONLY thing covering the process limit on Windows.
#
# The fork probe below is guarded by `_POSIX` and skips there, correctly. So if
# THIS one is ever skipped on Windows too, the platform has no process-limit
# coverage at all and the suite still exits 0 — a skip is not a failure, and
# "unverified" and "verified" print differently but exit the same.
#
# Measured on windows-latest at the time of writing, from that job's own log:
#
#   PASS process limit bounds SPAWNED children too (portable probe)
#        -> 23 of 400 attempted (limit 24, ambient None -> None)
#
# which is the job-scoped flat ceiling the branching above says Windows takes.
# That was the answer to "does it run"; this assertion is what keeps the answer
# true. macOS is the one platform where skipping is correct — uid-wide
# RLIMIT_NPROC with no measurable ambient count, covered by the fork probe.
_MUST_NOT_SKIP = os.name == "nt" or sys.platform.startswith("linux")
check("the portable probe is not skipped where it is the only coverage",
      not (_MUST_NOT_SKIP and _spawn_limit is None),
      f"-> platform={sys.platform} job_scoped={_JOB_SCOPED} "
      f"ambient={_ambient} limit={_spawn_limit}")

_exe = executor._rust
if _exe and _spawn_limit is None:
    print(f"SKIP portable process-limit probe — {sys.platform} has a uid-wide "
          f"RLIMIT_NPROC and no measurable ambient count, so no low ceiling can "
          f"be set safely; the fork probe below covers this platform")
elif _exe:
    _p = subprocess.run(
        [str(_exe), "--lang", "python3", "--timeout", "90"],
        input=_SPAWNER, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=240,
        env={**os.environ, executor.MAX_PROCESSES_ENV: str(_spawn_limit)})
    try:
        _d = json.loads(_p.stdout)
    except json.JSONDecodeError:
        _d = {}
    # Re-read ambient AFTER the run. The ceiling is absolute — it is pinned to
    # `ambient + headroom` through CODECALC_MAX_PROCESSES — so the number of
    # children that fit is `limit - (ambient AT THAT MOMENT)`, not `headroom`.
    # Those differ whenever the box gets busier or quieter mid-test.
    _ambient_after = executor.current_uid_tasks()
    _m2 = re.search(r"SPAWNED (\d+)", _d.get("stdout") or "")
    _spawned = int(_m2.group(1)) if _m2 else None

    # THE PROPERTY, and it does not depend on ambient at all: the ceiling
    # stopped the spawn. The probe tries 400; if RLIMIT_NPROC were not applied
    # it would get all 400. Anything short of that is the limit biting.
    #
    # This replaces `spawned <= headroom`, which failed on #97 with "45 children
    # against a headroom of 24 (limit 98, ambient 74)". Ambient had FALLEN about
    # 21 tasks between the measurement and the probe, so 45 children fit under a
    # ceiling of 98 — the sandbox held exactly as designed and the expectation
    # had gone stale. It could only fail when the machine got QUIETER, which is
    # the opposite of when you want to hear from a process-limit test.
    check("process limit bounds SPAWNED children too (portable probe)",
          _spawned is not None and _spawned < _SPAWN_ATTEMPTS,
          f"-> {_spawned} of {_SPAWN_ATTEMPTS} attempted "
          f"(limit {_spawn_limit}, ambient {_ambient} -> {_ambient_after})")

    # And the count has to be EXPLICABLE by that ceiling rather than merely
    # under the attempt cap: children plus the tasks already running cannot
    # exceed the limit. Checked against the more generous of the two ambient
    # readings, with headroom for churn during the run itself — the point is to
    # catch a ceiling that is an order of magnitude off, not to re-derive the
    # scheduler's exact state.
    if _spawned is not None and _ambient is not None and _ambient_after is not None:
        _floor = min(_ambient, _ambient_after)
        check("  ...and the count is consistent with that ceiling",
              _spawned + _floor <= _spawn_limit + _CHURN_TOLERANCE,
              f"-> {_spawned} + {_floor} ambient vs limit {_spawn_limit} "
              f"(+{_CHURN_TOLERANCE} churn allowance)")

    check("  ...and the probe actually ran rather than dying early",
          _spawned is not None and _spawned > 0,
          f"-> {_spawned!r}; verdict={_d.get('verdict')} stderr={(_d.get('stderr') or '')[:60]!r}")
else:
    print("SKIP portable process-limit probe — no native executor built")

# Whatever the limit was, it held.
if not _POSIX:
    print("SKIP fork-bomb EAGAIN probe — os.fork() does not exist on Windows; "
          "the portable spawn probe above covers the guarantee there")
else:
    # TWO DIFFERENT FAILURES, kept apart. `_children is not None and
    # _children <= _limit` reported both as "the process limit did not bound
    # the fork bomb", so a probe that never produced a count — because it timed
    # out, or the runtime was missing, or the executor could not read its
    # output — read as a sandbox breach.
    #
    # That is what #75 was on macOS: `None children (limit 4096,
    # measured=False)`. The ceiling was not exceeded; nothing was measured. An
    # instrument that cannot take a reading has to say so, or it manufactures a
    # finding out of its own failure — the same shape as #80, where an
    # unreadable output file was reported as a program that printed nothing.
    if _children is None:
        _why = (f"no EAGAIN_AFTER in stdout "
                f"(verdict={_probe.get('verdict')} ok={_probe.get('ok')} "
                f"timed_out={_probe.get('timed_out')} "
                f"exit={_probe.get('exit_code')} "
                f"backend={_probe.get('backend')} "
                f"output_error={_probe.get('output_error')!r} "
                f"stdout={(_probe.get('stdout') or '')[-80:]!r} "
                f"stderr={str(_probe.get('stderr') or '')[-160:]!r})")
        # ONE narrow case is a skip rather than a failure, and only this one:
        # the ceiling was NOT measured (so nproc_limit fell back to a fixed
        # 4096) and the probe hit the wall clock. Forking toward 4096 on a
        # runner where the ambient count is unknown is exactly the situation
        # this file already documents for the portable spawn probe — "no safe
        # low ceiling exists" — and a timeout there says the probe was too slow,
        # not that the sandbox leaked.
        #
        # Every other way of producing no count still FAILS. A missing runtime,
        # a nonzero exit, an unreadable stream (#80) or an empty stdout with a
        # clean exit are all things worth being loud about, and folding them
        # into this skip would be how a real breach goes quiet.
        if not _measured and _probe.get("timed_out"):
            print(f"SKIP fork-bomb probe — {sys.platform} cannot measure the "
                  f"ambient task count, so the ceiling is a fixed {_limit} and "
                  f"the probe outran its 30s budget. {_why}")
        else:
            check("fork-bomb probe produced a child count", False, f"-> {_why}")
    else:
        check("fork-bomb bounded by the process limit",
              _children <= _limit,
              f"-> {_children} children (limit {_limit}, measured={_measured})")
if _POSIX and _measured:
    # +64 tolerates ambient churn between the measurement and the forks; the
    # point is that the bound is ~headroom, not exactly headroom.
    check("fork-bomb bounded at the configured headroom (measured path)",
          _children is not None and _children <= _headroom + 64,
          f"-> {_children} children (headroom {_headroom})")
elif _POSIX:
    print(f"SKIP fork-bomb headroom bound — ambient task count is not measurable "
          f"on {sys.platform}; the fixed ceiling of {_limit} applies instead")

# 10b. And the runaway case still terminates rather than hanging.
# Self-cleaning, for the same reason as the counter above. A bare
# `while True: os.fork()` leaves its children behind: on Linux each child
# continues the loop, hits OSError and exits, so the tree drains on its own —
# but on a 3-core macOS runner with a 4096 ceiling the reaping is slow enough
# that the NEXT SHELL COMMAND could not fork, and the job died at
# `fork: Resource temporarily unavailable` AFTER the suite printed ALL PASS.
# Whichever process first hits the limit kills the whole group, so the tree goes
# down at once instead of draining.
_BOMB = """import os, signal
try:
    while True:
        os.fork()
except OSError:
    pass
os.killpg(os.getpgrp(), signal.SIGKILL)
"""
r = executor.execute("python3", _BOMB, timeout=10)
check("fork-bomb stopped by RLIMIT_NPROC",
      not r.get("ok") or r.get("exit_code", 0) != 0 or r.get("timed_out"),
      f"exit={r.get('exit_code')} timed_out={r.get('timed_out')}")

print(f"\n=== {len(FAILS)} failures ===" if FAILS else "\n=== ALL SECURITY TESTS PASS ===")
sys.exit(1 if FAILS else 0)
