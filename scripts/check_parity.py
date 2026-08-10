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

from codecalc import executor, registry, sessions  # noqa: E402 — needs the path above

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

# ── an unreadable output stream must be REPORTED, on both backends (#80) ────
# The Rust path read its output files with `if let Ok(f) = File::open(..) { let _
# = f.read_to_end(..) }` and the Python fallback drained its pipes under a bare
# `except (OSError, ValueError): pass`. Different mechanisms, identical defect:
# output that could not be read came back as output that was simply empty, and
# `ok` was computed from exit status alone, so it read as a successful run.
#
# Gated together because the two backends reached the same wrong answer
# independently — which is the case for a parity check rather than two local
# tests.
# COUNTED, not merely present. The first draft of this gate looked for the
# string "output_error" anywhere in each file and PASSED after the JSON
# emission was deleted, because the struct field and its doc comment still
# matched. Watched failing, which is how that was found. Every result shape a
# caller can receive has to carry the field, so the floor is the number of
# emission sites, not one.
if floor("rust output_error emissions",
         re.findall(r'"output_error": sr\.output_error', RUST), 2) and \
   floor("python output_error emissions",
         re.findall(r'"output_error": \w*drain_error', PY_EXECUTOR_SRC), 3):
    print("ok   unreadable output: both backends report `output_error` "
          "on every result shape")

# The field is only worth having if it also reaches `ok`. A backend that reports
# the error while still saying ok=true has told the caller twice — once wrongly.
_OK_GUARDS = [
    ("executor/src/main.rs", RUST, "sr.output_error.is_none()"),
    ("codecalc/executor.py", PY_EXECUTOR_SRC, "drain_error is None"),
]
_missing_guard = [label for label, src, marker in _OK_GUARDS if marker not in src]
if _missing_guard:
    fail(f"{_missing_guard} computes `ok` without consulting the output-read "
         "error, so an unreadable stream still reports a successful run")
else:
    print("ok   unreadable output: both backends fold it into `ok`")

# ── a session must declare every guarantee a one-shot run carries (#104) ────
# `execute_code()` runs through codecalc-exec. `execute_code(session_id=...)`
# on a stateful language runs through `subprocess.Popen` in sessions.py, and
# the two therefore offer materially different guarantees under one tool name.
# That is allowed — the worker is long-lived on purpose — but it may not be
# SILENT, which is the class this repo has now corrected four times (#62, #66,
# #80, and the gate this file grew for its own skill).
#
# The gate is a correspondence, not a spell-check: each key names a guarantee
# the Rust path demonstrably provides, proven by a marker in its source. If the
# executor ever stops providing one, the marker goes and this fails — which is
# the point, because the session disclosure would then be describing a
# difference that no longer exists. If a NEW guarantee lands on the Rust side,
# add it here and to `_SESSION_STRUCTURAL_GAPS` together.
def _code_only(src: str) -> str:
    """Rust source with comments removed.

    Load-bearing, not hygiene. The first draft proved `process_group_kill` with
    the marker "killpg" — which appears in unix.rs exactly once, inside a
    comment explaining why `killpg` is NOT used (it is not async-signal-safe;
    the code calls `kill(-pgid)` instead). The gate was matching prose that
    said the opposite of what it claimed to prove, and would have passed with
    the kill deleted. A marker has to hit code.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


RUST_CODE = _code_only(RUST)
RUST_UNIX_CODE = _code_only(RUST_UNIX)

_ONE_SHOT_GUARANTEES = {
    # session key          # proof it is real on the one-shot path
    "no_net": ("executor/src/main.rs", RUST_CODE, "platform::apply_no_net"),
    "peak_memory_kb": ("executor/src/main.rs", RUST_CODE, "waited.peak_memory_kb"),
    # The actual mechanism, not the name of the call it deliberately avoids.
    "process_group_kill": ("executor/src/platform/unix.rs", RUST_UNIX_CODE,
                           "libc::kill(-pgid, libc::SIGKILL)"),
    # Not a single call site: the whole binary is the guarantee. Its argv/env
    # handling is what ENV_ALLOWLIST above already gates.
    "sandbox_backend": ("executor/src/main.rs", RUST_CODE, "ENV_ALLOWLIST"),
}

_declared = set(sessions._SESSION_STRUCTURAL_GAPS)
_missing_decl = sorted(set(_ONE_SHOT_GUARANTEES) - _declared)
_stale_decl = sorted(_declared - set(_ONE_SHOT_GUARANTEES))
_WORD = re.compile(r"[0-9A-Za-z_]")


def _bounded(marker: str) -> re.Pattern:
    """`marker` as a regex that will not match a longer identifier.

    A plain `in` test passes after the symbol is RENAMED, because
    "apply_no_net" is a substring of "apply_no_net_RENAMED" — watched, and it
    was the one direction of four that did not fail.

    The bound is applied per EDGE rather than with a bare `\\b`, because a
    marker may legitimately begin or end with punctuation:
    `libc::kill(-pgid, libc::SIGKILL)` ends in `)`, and `\\b` there asserts a
    word/non-word transition that a following `;` does not provide, so the
    pattern matched nothing and the gate failed on a correct tree. Watched
    again after fixing, which is the only reason that was not shipped as a
    permanently-red check.
    """
    pre = r"(?<![0-9A-Za-z_])" if _WORD.match(marker[0]) else ""
    post = r"(?![0-9A-Za-z_])" if _WORD.match(marker[-1]) else ""
    return re.compile(pre + re.escape(marker) + post)


_lost_proof = sorted(k for k, (_, src, marker) in _ONE_SHOT_GUARANTEES.items()
                     if not _bounded(marker).search(src))

if _lost_proof:
    fail(f"the one-shot path no longer shows evidence of {_lost_proof} "
         "(marker gone from the Rust source), so the session disclosure for it "
         "describes a difference that may not exist any more")
elif _missing_decl:
    fail(f"a stateful session does not declare {_missing_decl} in "
         "_SESSION_STRUCTURAL_GAPS, so execute_code(session_id=...) drops a "
         "guarantee that execute_code() applies and says nothing")
elif _stale_decl:
    fail(f"_SESSION_STRUCTURAL_GAPS declares {_stale_decl}, which no longer "
         "corresponds to anything the one-shot path provides — a result "
         "disclaiming a guarantee nobody offers is noise that trains callers "
         "to skip the field")
else:
    print(f"ok   session disclosure: all {len(_ONE_SHOT_GUARANTEES)} one-shot "
          "guarantees are declared")

# COUNTED, for the reason recorded at the output_error gate above: a helper
# that exists but is called nowhere discloses nothing. Both stateful result
# shapes — the one `session_start` returns and the one `execute` returns — have
# to carry it, so the floor is the number of call sites.
if floor("session unenforced emissions",
         re.findall(r"_session_unenforced\(w\)", PY_SESSIONS_SRC), 2):
    print("ok   session disclosure: emitted on both stateful result shapes")

# ── the two backends must return the SAME KEYS, measured not listed ────────
# The gates above compare SOURCE constructs — allowlists, path strings, marker
# presence. None of them compares the thing a caller actually receives, and a
# field can therefore exist in one backend's output and not the other's while
# every check here passes.
#
# It did. `output_truncated` was a field on the Rust StepResult, computed from
# `out_trunc || err_trunc`, and used to raise the OLE verdict — and emitted by
# neither Rust return. The Python fallback emitted it. So the Rust backend
# answered "OLE" while omitting the field that says why, the fallback did not,
# and `contract_check.py`'s "every return path must have the SAME shape" held
# only within one backend. Nothing compared them to each other.
#
# This runs both and diffs the key sets. It is the only check in this file that
# executes the product rather than reading it, which is exactly why it catches
# what the others cannot.
_probe = 'print("x" * 100000)'
_rust_result = executor.execute("python3", _probe, max_output_kb=1)
_saved_rust = executor._rust
executor._rust = None          # force the pure-Python fallback
try:
    _py_result = executor.execute("python3", _probe, max_output_kb=1)
finally:
    executor._rust = _saved_rust

if _rust_result.get("backend") != "rust":
    # Not a pass. Without the native binary there is no second backend to
    # compare against, and saying so beats a green check that compared one
    # thing with itself.
    print("SKIP backend key parity: no native executor built, nothing to compare")
else:
    _only_rust = sorted(set(_rust_result) - set(_py_result))
    _only_py = sorted(set(_py_result) - set(_rust_result))
    if _only_rust or _only_py:
        fail("the two backends return DIFFERENT keys — "
             f"rust-only={_only_rust} python-only={_only_py}. A caller cannot "
             "switch backends without its field reads changing, which is the "
             "one thing the shared result contract promises")
    else:
        print(f"ok   backend key parity: both return the same {len(_rust_result)} keys")

if failures:
    print(f"\n=== {len(failures)} parity failure(s) ===")
    sys.exit(1)
print("\n=== python/rust parity holds ===")
