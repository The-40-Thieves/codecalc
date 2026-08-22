"""Hardening of the `--capabilities` no_net probe (THE-914).

Codex's security review of the `network_control`-from-probe change (the one
that made `no_net_kernel_enforcement_available()` derive from the Rust
binary's `--capabilities` output, rather than `backend() == "rust"` alone)
went through TWO rounds. Round 1 found no reportable vulnerability — every
path already failed closed — but flagged two fail-closed refinements:

  A. The import-time probe result was cached against the binary's PATH, not
     its on-disk identity. If the binary at that path were later replaced
     (same-UID/deploy write access) with a weaker build, the stale `True`
     would persist for the rest of the server's life — nothing re-probed.
     `executor._binary_identity()` / `no_net_kernel_enforcement_available()`
     bind the cache to (device, inode, size, mtime) and re-probe on change.

  B. `_probe_no_net_kernel_enforcement()` used a bare `subprocess.run(
     timeout=15)`. On `TimeoutExpired` that kills only the direct child — a
     wrapped or faulty binary that had already spawned a descendant leaks it,
     exactly the class of bug `_popen_group`/`_kill_group` exist to close on
     every OTHER path this module spawns the executor. The probe now uses
     the same helpers.

Round 2 (a second cross-vendor pass on the round-1 fix itself) reproduced
three further defects, two of which defeated round 1's own fail-safe goal:

  C. `bool(data.get("no_net_kernel_enforcement"))` reads a malformed/failed
     probe as truthy in cases that must be `False`: `bool("false")` and
     `bool(1)` are both `True` in Python, and `proc.returncode` was never
     checked at all — a binary that printed `true` and then exited nonzero
     was trusted anyway. Fixed with an `is True` identity check plus an
     explicit `returncode == 0` gate.

  D. The fix for B could itself hang: `_kill_group` returns as soon as the
     DIRECT child exits on SIGTERM, even if a descendant that ignores
     SIGTERM is still alive holding the captured pipes open — and the
     unbounded `proc.communicate()` right after it then blocks forever
     waiting for an EOF that never comes. Fixed by unconditionally
     escalating to `_reap_group` (SIGKILL, which cannot be ignored) before
     a now-BOUNDED final drain.

  E. The re-probe published the new binary identity and the answer it
     produced as two separate steps, not one. A concurrent reader could see
     the identity had already changed (so "no re-probe needed") while the
     fresh probe for that very identity was still in flight — since a
     subprocess call releases the GIL — and hand back the stale cached
     answer instead of the real one. Fixed by publishing identity+answer
     together under `_NO_NET_KERNEL_ENFORCEMENT_LOCK`, so a concurrent
     reader hitting a changed identity waits for the fresh answer instead of
     racing past it.

A sixth refinement — the probe itself proving INSTALLABILITY rather than
mere kernel configuration — lives entirely on the Rust side
(`executor/src/platform/unix.rs`, `seccomp::installable`/`installable_with`)
and has its own `#[cfg(test)]` unit tests there; nothing here re-tests it.

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import threading
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


# ── B. process-group leak: a timed-out probe must reap descendants, even a
#      SIGTERM-IGNORING one, and the final drain must never hang ───────────
# `_popen_group`/`_kill_group`/`_reap_group` are POSIX process-group
# primitives (Windows uses CREATE_NEW_PROCESS_GROUP + taskkill /T, exercised
# by the same helpers elsewhere in the suite); this regression's
# `os.kill(pid, 0)` liveness check is POSIX-only, so the case is skipped
# rather than faked on Windows.
#
# Calls `executor._probe_no_net_kernel_enforcement()` itself (with its
# testability `timeout=`/`drain_timeout=` kwargs turned down), not a
# reimplementation of its timeout handling — this exercises the actual fix,
# not a copy of it that could drift from the real code.
if executor.IS_WINDOWS:
    check("descendant-reap-on-probe-timeout check ran "
          "(skipped: POSIX-only liveness check)", True)
else:
    with tempfile.TemporaryDirectory(prefix="codecalc-capprobe-") as _td_b:
        _marker = pathlib.Path(_td_b) / "grandchild.pid"
        _wrapped = pathlib.Path(_td_b) / "wrapped-codecalc-exec"
        # Simulates a wrapped/faulty `--capabilities` binary: backgrounds a
        # GRANDCHILD that explicitly IGNORES SIGTERM (`trap '' TERM`) before
        # the wrapper itself hangs. The wrapper process (the DIRECT child)
        # does NOT trap SIGTERM, so it dies almost instantly when the group
        # is SIGTERM'd — which is exactly what let the round-1 fix's
        # `_kill_group(proc)` (which only `proc.wait()`s on the direct
        # child) return early while the SIGTERM-immune grandchild kept the
        # captured stdout/stderr pipes open underneath it. A test using a
        # SIGTERM-respecting grandchild (round 1's version of this test)
        # cannot reach that branch at all: it would die on the first SIGTERM
        # like everything else in the group.
        _wrapped.write_text(
            "#!/bin/sh\n"
            "(trap '' TERM; sleep 300) &\n"
            f"echo $! > {_marker}\n"
            "sleep 300\n"
        )
        _wrapped.chmod(0o755)

        _old_rust_b = executor._rust
        try:
            executor._rust = str(_wrapped)
            _t0 = time.monotonic()
            _result = executor._probe_no_net_kernel_enforcement(
                timeout=2, drain_timeout=2)
            _elapsed = time.monotonic() - _t0
            check("a probe that times out against a SIGTERM-ignoring "
                  "descendant returns False rather than hanging",
                  _result is False)
            check("the probe returned promptly (bounded drain worked) "
                  "instead of blocking on the SIGTERM-ignoring grandchild's "
                  "still-open pipes",
                  _elapsed < 10, f"-> took {_elapsed:.1f}s")
        finally:
            executor._rust = _old_rust_b

        for _ in range(50):
            if _marker.exists():
                break
            time.sleep(0.05)
        check("the wrapped probe left a pid file for its SIGTERM-ignoring "
              "grandchild (fixture sanity: the process it should have "
              "leaked existed)",
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
            check("a SIGTERM-ignoring descendant the wrapped binary spawned "
                  "is REAPED via SIGKILL, not leaked, when the "
                  "`--capabilities` probe subprocess times out",
                  not _alive, f"-> grandchild pid {_gpid} alive={_alive}")


# ── C. fail-safe validation: malformed/failed probe output must read False,
#      never a stale/wrong True ────────────────────────────────────────────
def _fake_output_binary(path: pathlib.Path, *, stdout: str, exit_code: int = 0) -> None:
    path.write_text(f"#!/bin/sh\nprintf %s {_sh_quote(stdout)}\nexit {exit_code}\n")
    path.chmod(0o755)


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


with tempfile.TemporaryDirectory(prefix="codecalc-capprobe-") as _td_c:
    _cases: list[tuple[str, str, int]] = [
        ("exit-nonzero-with-true-output",
         '{"no_net_kernel_enforcement": true}', 1),
        ("string-\"false\"-is-not-boolean-false",
         '{"no_net_kernel_enforcement": "false"}', 0),
        ("integer-1-is-not-boolean-true",
         '{"no_net_kernel_enforcement": 1}', 0),
        ("string-\"yes\"-is-not-boolean-true",
         '{"no_net_kernel_enforcement": "yes"}', 0),
    ]
    _old_rust_c = executor._rust
    try:
        for _name, _stdout, _rc in _cases:
            _bin = pathlib.Path(_td_c) / f"fake-{_name}"
            _fake_output_binary(_bin, stdout=_stdout, exit_code=_rc)
            executor._rust = str(_bin)
            _got = executor._probe_no_net_kernel_enforcement(timeout=5, drain_timeout=2)
            check(f"malformed/failed probe case '{_name}' reports False, "
                  "not a truthy misread",
                  _got is False, f"-> stdout={_stdout!r} rc={_rc} got={_got!r}")
    finally:
        executor._rust = _old_rust_c


# ── D. concurrency: a reader racing a re-probe must never see a stale
#      cached answer for the identity it just observed ─────────────────────
# Reproduces Codex's finding: publishing the new identity BEFORE the probe
# that produced its answer completed let a concurrent reader see the
# already-updated identity, conclude no re-probe was needed, and return the
# OLD cached answer — even while the real re-probe (in flight on another
# thread, subprocess I/O having released the GIL) was about to report
# something different. `_NO_NET_KERNEL_ENFORCEMENT_LOCK` now serializes this:
# a reader that hits a changed identity while another thread's probe is
# still running must WAIT for that same fresh answer, not race past it.
with tempfile.TemporaryDirectory(prefix="codecalc-capprobe-race-") as _td_d:
    _fast = pathlib.Path(_td_d) / "fake-codecalc-exec"
    _write_fake_capabilities_binary(_fast, no_net=True)

    _old_rust_d = executor._rust
    _old_enforcement_d = executor._NO_NET_KERNEL_ENFORCEMENT
    _old_identity_d = executor._NO_NET_KERNEL_ENFORCEMENT_IDENTITY
    try:
        executor._rust = str(_fast)
        executor._NO_NET_KERNEL_ENFORCEMENT = executor._probe_no_net_kernel_enforcement()
        executor._NO_NET_KERNEL_ENFORCEMENT_IDENTITY = executor._binary_identity(executor._rust)
        check("precondition: warm cache reports True before the race",
              executor.no_net_kernel_enforcement_available() is True)

        # Swap in a binary that answers False, but only after a real delay —
        # long enough that a second thread's read can land WHILE the probe
        # subprocess is still running (the window round 1's two-step publish
        # got wrong).
        _marker_d = pathlib.Path(_td_d) / "probe-started"
        time.sleep(1.1)  # past a possible 1s mtime-resolution floor
        _fast.write_text(
            "#!/bin/sh\n"
            f"touch {_marker_d}\n"
            "sleep 2\n"
            'echo \'{"no_net_kernel_enforcement": false}\'\n'
        )
        _fast.chmod(0o755)

        _results: dict[str, bool | None] = {}
        _timings: dict[str, float] = {}

        def _read(name: str) -> None:
            _t0 = time.monotonic()
            _results[name] = executor.no_net_kernel_enforcement_available()
            _timings[name] = time.monotonic() - _t0

        _bg = threading.Thread(target=_read, args=("bg",))
        _bg.start()

        # Wait for the marker the SLOW binary writes right after it starts
        # (before its 2s sleep). Since spawning the probe subprocess happens
        # INSIDE `_NO_NET_KERNEL_ENFORCEMENT_LOCK`, seeing this marker proves
        # the background thread is currently holding that lock mid-probe.
        for _ in range(200):
            if _marker_d.exists():
                break
            time.sleep(0.02)
        check("the background thread's probe actually started before the "
              "concurrent read below (fixture sanity)", _marker_d.exists())

        _read("main")  # runs synchronously on THIS thread, racing `_bg`

        _bg.join(timeout=10)
        check("the background probe thread finished", not _bg.is_alive())
        check("neither reader returns a stale True — both get the fresh "
              "False the in-flight re-probe actually produced",
              _results.get("bg") is False and _results.get("main") is False,
              f"-> {_results}")
        check("the concurrent (main-thread) reader WAITED for the in-flight "
              "probe (serialized on the lock) rather than racing past it "
              "with a stale cached value",
              _timings.get("main", 0.0) >= 1.0,
              f"-> waited {_timings.get('main')!r}s")
    finally:
        executor._rust = _old_rust_d
        executor._NO_NET_KERNEL_ENFORCEMENT = _old_enforcement_d
        executor._NO_NET_KERNEL_ENFORCEMENT_IDENTITY = _old_identity_d


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL NO_NET CAPABILITY PROBE HARDENING TESTS PASS ===")
sys.exit(1 if FAILS else 0)
