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

if failures:
    print(f"\n=== {len(failures)} parity failure(s) ===")
    sys.exit(1)
print("\n=== python/rust parity holds ===")
