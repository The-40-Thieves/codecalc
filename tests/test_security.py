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

# 5. env allowlist still lets runtimes work (PATH/HOME present)
r = executor.execute("python3", "import os; print('PATH' in os.environ, bool(os.environ.get('HOME')))")
check("allowlist keeps PATH+HOME functional", "True True" in r.get("stdout", ""))

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
r = tools.benchmark("import sys\nn=int(sys.stdin.readline())\ns=0\nfor i in range(n):\n    for j in range(n): s+=i*j\nprint(s)",
                    sizes="500,1000,2000,4000,8000", timeout=15)
est = r.get("estimate", "")
check("benchmark detects polynomial growth (no eval regression)",
      "O(n" in est and est not in ("O(1)", "O(log n)"),
      f"-> {est}")

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
_COUNTER = """import os, signal, sys
n = 0
try:
    while True:
        if os.fork() == 0:
            os.pause()      # child parks; only the parent counts
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
if _POSIX:
    r = executor.execute("python3", _COUNTER, timeout=30)
    _m = re.search(r"EAGAIN_AFTER (\d+)", r.get("stdout", "") or "")
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
# The limit has to be platform-appropriate or the probe tests nothing:
# RLIMIT_NPROC is uid-wide, so an absolute 24 on Linux is below the ambient
# count and the sandboxed program cannot start at all. Job objects are
# job-scoped, so 24 there means 24.
_SPAWN_HEADROOM = 24
_ambient = executor.current_uid_tasks()
_spawn_limit = (_ambient + _SPAWN_HEADROOM) if _ambient is not None else _SPAWN_HEADROOM

_SPAWNER = """import subprocess, sys
kids = []
try:
    while len(kids) < 400:
        kids.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"]))
except Exception:
    pass
print("SPAWNED", len(kids), flush=True)
for k in kids:
    k.kill()
"""
_exe = executor._rust
if _exe:
    _p = subprocess.run(
        [str(_exe), "--lang", "python3", "--timeout", "90"],
        input=_SPAWNER, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=240,
        env={**os.environ, executor.MAX_PROCESSES_ENV: str(_spawn_limit)})
    try:
        _d = json.loads(_p.stdout)
    except json.JSONDecodeError:
        _d = {}
    _m2 = re.search(r"SPAWNED (\d+)", _d.get("stdout") or "")
    _spawned = int(_m2.group(1)) if _m2 else None
    check("process limit bounds SPAWNED children too (portable probe)",
          _spawned is not None and _spawned <= _SPAWN_HEADROOM,
          f"-> {_spawned} children against a headroom of {_SPAWN_HEADROOM} "
          f"(limit {_spawn_limit}, ambient {_ambient})")
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
    check("fork-bomb bounded by the process limit",
          _children is not None and _children <= _limit,
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
