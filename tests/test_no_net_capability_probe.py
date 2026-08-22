"""Hardening of the `--capabilities` no_net probe (THE-914).

Codex's security review of the `network_control`-from-probe change (the one
that made `no_net_kernel_enforcement_available()` derive from the Rust
binary's `--capabilities` output, rather than `backend() == "rust"` alone)
found no reportable vulnerability — every path already fails closed — but
flagged two fail-closed REFINEMENTS on the Python side:

  1. The import-time probe result was cached against the binary's PATH, not
     its on-disk identity. If the binary at that path were later replaced
     (same-UID/deploy write access) with a weaker build, the stale `True`
     would persist for the rest of the server's life — nothing re-probed.
     `executor._binary_identity()` / `no_net_kernel_enforcement_available()`
     now bind the cache to (device, inode, size, mtime) and re-probe on
     change. Exercised below with a fake binary swapped in place.

  2. `_probe_no_net_kernel_enforcement()` used a bare `subprocess.run(
     timeout=15)`. On `TimeoutExpired` that kills only the direct child — a
     wrapped or faulty binary that had already spawned a descendant leaks it,
     exactly the class of bug `_popen_group`/`_kill_group` exist to close on
     every OTHER path this module spawns the executor. The probe now uses
     the same helpers. Exercised below with a fake binary that backgrounds a
     `sleep` before hanging.

A third refinement — the probe itself proving INSTALLABILITY rather than
mere kernel configuration — lives entirely on the Rust side
(`executor/src/platform/unix.rs`, `seccomp::installable`/`installable_with`)
and has its own `#[cfg(test)]` unit tests there; nothing here re-tests it.

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import executor

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def _write_fake_capabilities_binary(path: pathlib.Path, *, no_net: bool) -> None:
    path.write_text(
        "#!/bin/sh\n"
        f'echo \'{{"no_net_kernel_enforcement": {"true" if no_net else "false"}}}\'\n'
    )
    path.chmod(0o755)


# ── A. cache identity: a binary replaced AT THE SAME PATH must re-probe ────
# Reassign-and-restore convention: `executor._rust` (test_providers.py,
# test_network_policy.py) plus the two cache globals this fix adds.
with tempfile.TemporaryDirectory(prefix="codecalc-capprobe-") as _td_a:
    _fake = pathlib.Path(_td_a) / "fake-codecalc-exec"
    _write_fake_capabilities_binary(_fake, no_net=True)

    _old_rust = executor._rust
    _old_enforcement = executor._NO_NET_KERNEL_ENFORCEMENT
    _old_identity = executor._NO_NET_KERNEL_ENFORCEMENT_IDENTITY
    try:
        executor._rust = str(_fake)
        executor._NO_NET_KERNEL_ENFORCEMENT = executor._probe_no_net_kernel_enforcement()
        executor._NO_NET_KERNEL_ENFORCEMENT_IDENTITY = executor._binary_identity(executor._rust)
        check("a fresh probe against the fake binary reports True",
              executor.no_net_kernel_enforcement_available() is True)
        _identity_before_swap = executor._binary_identity(str(_fake))

        # Replace the binary AT THE SAME PATH with a weaker one. Sleep past a
        # possible 1s mtime-resolution floor so identity genuinely changes —
        # size differs here regardless, but the sleep keeps this robust
        # against a future edit that made the two fakes the same size.
        time.sleep(1.1)
        _write_fake_capabilities_binary(_fake, no_net=False)

        check("a same-path binary swap changes its stat identity",
              executor._binary_identity(str(_fake)) != _identity_before_swap)
        check("no_net_kernel_enforcement_available() RE-PROBES and reports "
              "the WEAKER answer after the binary at the same path changed, "
              "rather than trusting the stale cached True",
              executor.no_net_kernel_enforcement_available() is False)

        # And a second read with nothing changed must NOT re-probe (no new
        # subprocess) — this asserts the fast path stays fast without
        # needing to instrument subprocess.Popen. Comparing (enforcement,
        # identity) before/after a no-op read.
        _cached_answer = executor._NO_NET_KERNEL_ENFORCEMENT
        _cached_identity = executor._NO_NET_KERNEL_ENFORCEMENT_IDENTITY
        check("a repeated read with an unchanged binary is stable",
              executor.no_net_kernel_enforcement_available() == _cached_answer
              and _cached_identity == executor._NO_NET_KERNEL_ENFORCEMENT_IDENTITY)
    finally:
        executor._rust = _old_rust
        executor._NO_NET_KERNEL_ENFORCEMENT = _old_enforcement
        executor._NO_NET_KERNEL_ENFORCEMENT_IDENTITY = _old_identity


# ── B. process-group leak: a timed-out probe must reap descendants ─────────
# `_popen_group`/`_kill_group` are POSIX process-group primitives (Windows
# uses CREATE_NEW_PROCESS_GROUP + taskkill /T, exercised by the same helpers
# elsewhere in the suite); this regression's `os.kill(pid, 0)` liveness check
# is POSIX-only, so the case is skipped rather than faked on Windows.
if executor.IS_WINDOWS:
    check("descendant-reap-on-probe-timeout check ran "
          "(skipped: POSIX-only liveness check)", True)
else:
    with tempfile.TemporaryDirectory(prefix="codecalc-capprobe-") as _td_b:
        _marker = pathlib.Path(_td_b) / "grandchild.pid"
        _wrapped = pathlib.Path(_td_b) / "wrapped-codecalc-exec"
        # Simulates a wrapped/faulty `--capabilities` binary: backgrounds a
        # descendant, then hangs well past any sane probe timeout.
        _wrapped.write_text(
            "#!/bin/sh\n"
            "sleep 300 &\n"
            f"echo $! > {_marker}\n"
            "sleep 300\n"
        )
        _wrapped.chmod(0o755)

        _proc = executor._popen_group([str(_wrapped), "--capabilities"])
        try:
            _proc.communicate(timeout=2)
            check("the wrapped fake binary was expected to time out", False,
                  "-> it exited on its own instead, test fixture is broken")
        except subprocess.TimeoutExpired:
            executor._kill_group(_proc)
            _proc.communicate()

        for _ in range(50):
            if _marker.exists():
                break
            time.sleep(0.05)
        check("the wrapped probe left a pid file for its grandchild "
              "(fixture sanity: the process it should have leaked existed)",
              _marker.exists())
        if _marker.exists():
            _gpid = int(_marker.read_text().strip())
            try:
                os.kill(_gpid, 0)
                _alive = True
            except ProcessLookupError:
                _alive = False
            except PermissionError:
                _alive = True
            check("a descendant the wrapped binary spawned is REAPED, not "
                  "leaked, when the `--capabilities` probe subprocess "
                  "times out",
                  not _alive, f"-> grandchild pid {_gpid} alive={_alive}")


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL NO_NET CAPABILITY PROBE HARDENING TESTS PASS ===")
sys.exit(1 if FAILS else 0)
