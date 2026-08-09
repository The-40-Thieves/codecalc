#!/usr/bin/env python3
"""Assert the Python and Rust halves of the executor still agree.

codecalc ships the SAME three security-critical constants twice — once in Rust
(executor/src/main.rs, the production path) and once in Python
(codecalc/executor.py + codecalc/registry.py, the fallback used when the binary
is not built). Nothing links them. They agree today; this script is what keeps
them agreeing.

Why each one matters if it drifts:

  ENV_ALLOWLIST   The fix for AUDIT.md CRITICAL-02 (secret leakage to executed
                  code). Add a var to the Rust list only, and the Python
                  fallback keeps leaking it — on exactly the machines where the
                  binary was never built, which is where the fallback runs.

  RUNTIME_PATH    Decides which interpreters resolve. Drift means the same
                  snippet runs against different toolchains depending on which
                  backend answered, and `list_languages` reports availability
                  computed from one of them.

  LANGUAGES       Drift means execute_code("gleam") succeeds via one backend and
                  returns "unknown language" via the other, with no way for a
                  caller to tell which backend they got.

FLOOR: every extractor asserts it found a non-empty set before comparing. A
regex that stops matching after a refactor would otherwise compare {} to {} and
report agreement — the failure mode where a gate silently scans nothing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from codecalc import executor, registry  # noqa: E402 — needs the path above

RUST = (REPO / "executor" / "src" / "main.rs").read_text(encoding="utf-8")
#: platform/unix.rs was never read by this script, which is why a behavioural
#: difference living there could not be gated. The uid-0 check below is the
#: first thing to need it.
RUST_UNIX = (REPO / "executor" / "src" / "platform" / "unix.rs").read_text(encoding="utf-8")
PY_EXECUTOR_SRC = (REPO / "codecalc" / "executor.py").read_text(encoding="utf-8")
PY_SERVER_SRC = (REPO / "codecalc" / "server.py").read_text(encoding="utf-8")
PY_SESSIONS_SRC = (REPO / "codecalc" / "sessions.py").read_text(encoding="utf-8")

failures: list[str] = []


def fail(msg: str) -> None:
    print(f"FAIL {msg}")
    failures.append(msg)


def floor(name: str, value, minimum: int) -> bool:
    """Refuse to compare an empty extraction — that is a broken gate, not a pass."""
    if len(value) < minimum:
        fail(f"{name}: extracted {len(value)} item(s), expected >= {minimum}. "
             f"The extractor is broken, so this check proves nothing.")
        return False
    return True


# ── 1. language registry ────────────────────────────────────────────────────
rust_langs = set(re.findall(r'Lang\s*\{\s*\n?\s*name:\s*"([^"]+)"', RUST))
py_langs = set(registry.LANGUAGES)

if floor("rust LANGS", rust_langs, 10) and floor("python LANGUAGES", py_langs, 10):
    only_py = sorted(py_langs - rust_langs)
    only_rs = sorted(rust_langs - py_langs)
    if only_py or only_rs:
        fail(f"language registry drift — python-only: {only_py}, rust-only: {only_rs}")
    else:
        print(f"ok   language registry: {len(py_langs)} entries, identical in both backends")

# ── 2. env allowlist ────────────────────────────────────────────────────────
m = re.search(r"ENV_ALLOWLIST:\s*&\[&str\]\s*=\s*&\[(.*?)\];", RUST, re.S)
rust_env = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', m.group(1))) if m else set()
py_env = set(executor._ENV_ALLOWLIST)

if floor("rust ENV_ALLOWLIST", rust_env, 5) and floor("python _ENV_ALLOWLIST", py_env, 5):
    if rust_env != py_env:
        fail(f"env allowlist drift — python-only: {sorted(py_env - rust_env)}, "
             f"rust-only: {sorted(rust_env - py_env)}")
    else:
        print(f"ok   env allowlist: {len(py_env)} vars, identical in both backends")

# ── 3. runtime PATH resolution ──────────────────────────────────────────────
# Both backends resolve the sandbox PATH the same way: CODECALC_RUNTIME_PATH,
# then the process's own PATH, then a minimal default. What must match is the
# ENV VAR NAME and the DEFAULT — if they drift, `CODECALC_RUNTIME_PATH=...`
# silently configures only one of the two backends, and which one answers
# depends on whether the Rust binary happens to be built.
m = re.search(r'RUNTIME_PATH_ENV:\s*&str\s*=\s*"([^"]+)"', RUST)
rust_env_name = m.group(1) if m else ""
m = re.search(r'DEFAULT_RUNTIME_PATH:\s*&str\s*=\s*"([^"]+)"', RUST)
rust_default = m.group(1) if m else ""

if floor("rust RUNTIME_PATH_ENV", rust_env_name, 5) and floor("python RUNTIME_PATH_ENV", registry.RUNTIME_PATH_ENV, 5):
    if rust_env_name != registry.RUNTIME_PATH_ENV:
        fail(f"runtime-path env var drift — python: {registry.RUNTIME_PATH_ENV!r}, rust: {rust_env_name!r}")
    else:
        print(f"ok   runtime-path env var: {rust_env_name} in both backends")

if floor("rust DEFAULT_RUNTIME_PATH", rust_default, 10) and floor("python DEFAULT_RUNTIME_PATH", registry.DEFAULT_RUNTIME_PATH, 10):
    if rust_default != registry.DEFAULT_RUNTIME_PATH:
        rs, py = rust_default.split(":"), registry.DEFAULT_RUNTIME_PATH.split(":")
        fail("DEFAULT_RUNTIME_PATH drift — "
             f"python-only: {sorted(set(py) - set(rs))}, rust-only: {sorted(set(rs) - set(py))}")
    else:
        print(f"ok   default runtime PATH: {len(rust_default.split(':'))} entries, identical in both backends")

# A machine-specific absolute path baked into either backend is what this whole
# section exists to prevent recurring; the repo is public and the binary ships.
for label, value in (("rust", rust_default), ("python", registry.DEFAULT_RUNTIME_PATH)):
    if "/home/" in value or "/Users/" in value:
        fail(f"{label} DEFAULT_RUNTIME_PATH contains a home directory: {value!r}. "
             "The default must be machine-neutral; use CODECALC_RUNTIME_PATH to pin a toolchain.")

# ── 4. fork-bomb guard ──────────────────────────────────────────────────────
# RLIMIT_NPROC is now measured (ambient uid tasks + headroom) rather than fixed.
# Both backends must agree on the env vars and the numbers, or an operator who
# sets CODECALC_PROCESS_HEADROOM configures only whichever backend answered.
NPROC_CONSTANTS = [
    ("MAX_PROCESSES_ENV", r'MAX_PROCESSES_ENV:\s*&str\s*=\s*"([^"]+)"', executor.MAX_PROCESSES_ENV, str),
    ("PROCESS_HEADROOM_ENV", r'PROCESS_HEADROOM_ENV:\s*&str\s*=\s*"([^"]+)"', executor.PROCESS_HEADROOM_ENV, str),
    ("DEFAULT_PROCESS_HEADROOM", r"DEFAULT_PROCESS_HEADROOM:\s*u64\s*=\s*(\d+)", executor.DEFAULT_PROCESS_HEADROOM, int),
    ("FALLBACK_NPROC_LIMIT", r"FALLBACK_NPROC_LIMIT:\s*u64\s*=\s*(\d+)", executor.FALLBACK_NPROC_LIMIT, int),
]
for name, pattern, py_value, cast in NPROC_CONSTANTS:
    m = re.search(pattern, RUST)
    if not m:
        fail(f"{name}: not found in main.rs — the extractor is broken, so this check proves nothing")
        continue
    rs_value = cast(m.group(1))
    if rs_value != py_value:
        fail(f"{name} drift — python: {py_value!r}, rust: {rs_value!r}")
    else:
        print(f"ok   {name}: {py_value!r} in both backends")

# The old bug in one line: a FIXED limit is a bet on the ambient task count.
if isinstance(getattr(executor, "NPROC_LIMIT", None), int):
    fail("executor.NPROC_LIMIT is back as a fixed constant. RLIMIT_NPROC is a uid-wide "
         "TASK budget, so a constant is a bet on how busy the box is — that bet is what "
         "broke 14 of 31 runtimes. Use nproc_limit().")

# ── 5. identity-checked workdir deletion ────────────────────────────────────
# #38: the README states, as a flat guarantee, that workdir deletion is
# identity-checked — device and inode recorded at creation, re-checked right
# before removal — because executed code runs with that directory as its cwd
# and can rename another one into its place. Rust has always enforced this
# (dir_identity/remove_own_workdir in main.rs). Python did not, at three call
# sites that each delete a directory they created: the pure-Python fallback
# executor, execute_code_stream's --workdir, and session teardown. Comparing
# env-var names and numeric constants (sections 1-4 above) could not have
# caught this — the constants matched fine; the BEHAVIOUR of an entire
# function was missing. This section asserts the property directly: both
# backends' cleanup goes through an identity-checked path, not a bare
# shutil.rmtree()/fs::remove_dir_all() on a directory the executed program had
# as its cwd.

# Rust: the guard functions exist, and are actually CALLED — not just defined
# and left dead. Definition uses `remove_own_workdir(work: &Path`; every call
# site passes `&work` instead, so counting `remove_own_workdir(&work`
# occurrences counts calls, not the one definition.
if floor("rust dir_identity fn", re.findall(r"fn\s+dir_identity", RUST), 1) and \
   floor("rust remove_own_workdir fn", re.findall(r"fn\s+remove_own_workdir", RUST), 1):
    print("ok   rust: dir_identity/remove_own_workdir are defined")
rust_calls = re.findall(r"remove_own_workdir\(&work", RUST)
if floor("rust remove_own_workdir call sites", rust_calls, 3):
    print(f"ok   rust: remove_own_workdir is called at {len(rust_calls)} site(s), not just defined")

# Python: the guard pair exists in executor.py...
if floor("python _dir_identity fn", re.findall(r"def _dir_identity", PY_EXECUTOR_SRC), 1) and \
   floor("python _rmtree_checked fn", re.findall(r"def _rmtree_checked", PY_EXECUTOR_SRC), 1):
    print("ok   python: _dir_identity/_rmtree_checked are defined")

# ...and each of the three sites named in #38 routes through it rather than
# an unconditional delete. Asserted PER SITE, by name, so a fourth call site
# added later without the guard is caught by "no bare shutil.rmtree(workdir"
# below rather than silently passing because two of three sites got it right.
PY_SITES = [
    ("codecalc/executor.py: _execute_python's cleanup", PY_EXECUTOR_SRC,
     r"if not caller_workdir:\s*\n\s*_rmtree_checked\(workdir"),
    ("codecalc/server.py: execute_code_stream's cleanup", PY_SERVER_SRC,
     r"executor\._rmtree_checked\(workdir"),
    ("codecalc/sessions.py: stop()'s cleanup", PY_SESSIONS_SRC,
     r"executor\._rmtree_checked\(d"),
]
for label, src, pattern in PY_SITES:
    if re.search(pattern, src):
        print(f"ok   {label} deletes through _rmtree_checked")
    else:
        fail(f"{label} does not call _rmtree_checked — deletion here is unconditional, "
             "the exact gap #38 reported")

# The regression this whole section exists to catch: a caller-created workdir
# deleted via a BARE shutil.rmtree(), bypassing the identity check entirely.
# Matches the call, not the import — sessions.py and server.py both import
# shutil for other reasons unrelated to workdir cleanup on their own paths.
BARE_RMTREE = re.compile(r"shutil\.rmtree\(\s*(workdir|d)\b")
for label, src in (("codecalc/executor.py", PY_EXECUTOR_SRC),
                    ("codecalc/server.py", PY_SERVER_SRC),
                    ("codecalc/sessions.py", PY_SESSIONS_SRC)):
    bare = BARE_RMTREE.findall(src)
    if bare:
        fail(f"{label}: found {len(bare)} bare shutil.rmtree() call(s) on a workdir — "
             "identity-checked deletion (_rmtree_checked) must be used instead")

# ── the uid-0 process-ceiling caveat, in BOTH backends ─────────────────────
# RLIMIT_NPROC does not bind a process whose effective uid is 0: the kernel
# exempts privileged processes. Both backends set that limit, so as root both
# compute a ceiling that has no effect — and neither said so, which reads as
# "the process ceiling was applied". Verified by running each backend as root:
# the Rust executor now reports process_limit_not_enforced_for_uid_0 and the
# Python fallback reports the max_processes caveat.
#
# Gated here because the two are easy to fix on one side only. The two
# vocabularies differ by long-standing convention — Rust emits snake_case
# tokens, Python emits prose — so this asserts each backend HAS a uid-0 entry
# rather than that the strings match, which would be a false parity.
_UID0_MARKERS = (
    ("executor/src/platform/unix.rs", RUST_UNIX, "process_limit_not_enforced_for_uid_0"),
    ("codecalc/executor.py", PY_EXECUTOR_SRC, "_UID0_PROCESS_CEILING"),
)
_missing_uid0 = [label for label, src, marker in _UID0_MARKERS if marker not in src]
if _missing_uid0:
    fail(f"{_missing_uid0} does not report the uid-0 process-ceiling caveat; "
         "RLIMIT_NPROC is not enforced for uid 0 and a backend that stays silent "
         "reports a guarantee it did not apply")
else:
    print("ok   uid-0 process ceiling: both backends report the caveat")

if failures:
    print(f"\n=== {len(failures)} parity failure(s) ===")
    sys.exit(1)
print("\n=== python/rust parity holds ===")
