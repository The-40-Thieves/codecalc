"""Output backpressure cannot deadlock the payload or supervisor.

THE-831's sixth acceptance criterion — the one property the lifecycle suite
(test_run_supervisor.py) leaves untested because it runs against fake providers
with no real output — is that a run producing far more output than any pipe
buffer, and more than the output cap, still reaches a terminal state and its
output is BOUNDED, never a hang and never an unbounded allocation.

The backpressure boundary in this codebase is the executor's output capture,
which both backends share the shape of:

  * rust  (production): `codecalc-exec` drains the child's stdout/stderr with a
    bounded `read_capped` (issue #80) and emits capped JSON, so the Python
    wrapper's `communicate()` never sees more than the cap.
  * python (fallback):  `_BoundedDrain` runs one reader thread per pipe, each
    reading to EOF while retaining only cap+1 bytes (issue #25) — so a child
    that fills the ~64 KiB pipe buffer can always keep writing and the reader
    can never deadlock it; overflow trips a prompt process-group kill.

RunSupervisor (managed_runs=False for the local provider) runs each request in
its ThreadPoolExecutor, so this suite drives the REAL path: RunSupervisor.start
-> pool worker -> executor.execute -> the bounded capture above. A naive
implementation — a single synchronous unbounded read, or a bounded buffer that
STOPS reading at the cap — would let the child block on a full pipe forever.

DIAGNOSABILITY UNDER A REAL DEADLOCK is a first-class requirement of this suite,
not an afterthought: a seeded deadlock must fail FAST and with a NAMED check
line visible in CI, never a multi-minute wall of silence ended by a bare thread
dump. Three mechanisms enforce that:

  * every check() line is FLUSHED as it prints, so nothing a check reports is
    lost to stdout block-buffering even if the process is later force-killed;
  * each stage bounds its blocking wait with a watchdog far below the executor's
    60 s wall-clock kill — the single/both stages via `supervisor.wait(timeout)`,
    the concurrent stage via one `concurrent.futures.wait(timeout)` over ALL its
    futures at once (never a per-run wait in series, whose worst case would be
    workers x wall-clock and could outlast the backstop). A stage that detects a
    hang short-circuits the rest;
  * on failure the runner flushes and `os._exit`s, because a detected hang leaves
    pool workers blocked until the 60 s wall-clock kill and a normal interpreter
    exit would re-hang CI joining them (ThreadPoolExecutor's atexit).

`faulthandler` remains only as a last-resort backstop; the named, flushed checks
above are the primary path a deadlock takes, not it.

Runs on both backends: the producer is portable python3 (`sys.stdout.write`),
never bash, so the fallback matrix job exercises it too.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import tempfile
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import wait as futures_wait
from pathlib import Path

from codecalc import executor, providers, run_supervisor

FAILS: list[str] = []

#: Far more than one 64 KiB pipe buffer AND more than the 64 KiB default output
#: cap: a naive reader deadlocks on the pipe long before this much is written.
PRODUCER_BYTES = 8 * 1024 * 1024
#: The executor's own wall-clock ceiling. Deliberately generous so a CORRECT
#: run (which finishes in well under a second) never approaches it — it is the
#: executor's last-resort kill, NOT the property under test, and precisely the
#: bound a real backpressure deadlock would otherwise wait out.
SPEC_TIMEOUT = 60
#: Per-stage watchdog for the single/both-pipe stages. A correct run terminates
#: in ~0.1 s, so this is ~100x margin (non-flaky even on a loaded CI runner)
#: while sitting far below SPEC_TIMEOUT: a run that only ends via the wall-clock
#: kill — i.e. a real deadlock — trips it in seconds with a named check.
STAGE_WATCHDOG = 10
#: The concurrent stage waits on all its futures at once; 12 subprocess spawns on
#: a slow runner take a few seconds, so it gets more headroom than a single run.
CONCURRENT_WATCHDOG = 25
#: More runs than the pool's default worker count (8): the pool must clear a
#: backlog of large-output producers without any of them wedging a worker.
CONCURRENT_RUNS = 12
#: A correct result truncates to ~64 KiB; anything near PRODUCER_BYTES means the
#: cap was applied after the fact (an allocation, not a ceiling) — or not at all.
CAP_BOUND = 256 * 1024
#: The default output cap (executor.MAX_OUTPUT_BYTES): the measured produced
#: size must exceed it, proving the run really did overflow the ceiling.
DEFAULT_CAP = executor.MAX_OUTPUT_BYTES
#: LAST-RESORT backstop only. The flushed, named per-stage checks above are the
#: primary path a deadlock takes; this exists solely so a hang that somehow
#: escapes every stage watchdog still cannot pin CI open forever. Set well above
#: the worst legitimate total (three stages near their watchdogs) and well below
#: the old value so it is never the mechanism that actually reports a deadlock.
GLOBAL_DEADLINE = 90


def check(name: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name}")
    # Flush every line as it prints: stdout is block-buffered to a pipe under CI,
    # and this suite may `os._exit` on a detected deadlock (below), which does
    # NOT flush. Without this, the very FAIL lines that diagnose a hang would be
    # the ones lost. The cost is negligible — 16 checks, not a hot loop.
    sys.stdout.flush()
    if not condition:
        FAILS.append(name)


def _producer(stdout_bytes: int, stderr_bytes: int = 0) -> str:
    """Portable python3 that writes fixed volumes to stdout and/or stderr.

    `sys.stdout.write` of a single large string (no bash, no `yes`) so the same
    source runs on the native backend and the pure-Python fallback matrix.
    """
    lines = ["import sys"]
    if stdout_bytes:
        lines.append(f'sys.stdout.write("O" * {stdout_bytes})')
    if stderr_bytes:
        lines.append(f'sys.stderr.write("E" * {stderr_bytes})')
    lines += ["sys.stdout.flush()", "sys.stderr.flush()"]
    return "\n".join(lines) + "\n"


def _supervisor(root: str, **kwargs) -> run_supervisor.RunSupervisor:
    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(providers.LocalExecutionProvider())
    return run_supervisor.RunSupervisor(registry, state_dir=Path(root), **kwargs)


def _measured_over_cap(result: dict, stream: str) -> bool:
    """The stream's produced size was MEASURED and exceeds the cap — proof the
    overflow was reported, not silently swallowed. Not `>= PRODUCER_BYTES`: a
    prompt overflow-kill correctly stops the child before it writes the full
    payload, so how much it managed is timing-dependent; that the measured size
    crossed the cap is the stable, backend-agnostic fact."""
    measured = result.get(f"{stream}_bytes")
    return isinstance(measured, int) and measured > DEFAULT_CAP


def _wait_terminal(supervisor, run_id, *, watchdog=STAGE_WATCHDOG):
    """Collect a run under the watchdog. Returns (result, elapsed) or (None,
    elapsed) if it did NOT terminate in time — a hang, reported as a failed
    check rather than a hung process."""
    started = time.monotonic()
    try:
        result = supervisor.wait(run_id, timeout=watchdog)
    except FutureTimeoutError:
        return None, time.monotonic() - started
    return result, time.monotonic() - started


def test_a_single_run_far_over_the_cap_terminates_with_bounded_output() -> bool:
    """A run writing 8 MiB of stdout — 128x the pipe buffer and the cap — must
    reach a terminal state promptly (no deadlock) with output truncated to the
    cap, and the produced size still MEASURED so nothing vanishes silently.

    Returns True if the run HUNG (watchdog fired), so the runner can short-circuit
    the remaining stages — which would hang identically — and fail fast."""
    with tempfile.TemporaryDirectory(prefix="codecalc-bp-single-") as root:
        supervisor = _supervisor(root)
        spec = providers.ComputationSpec(
            "python3", _producer(PRODUCER_BYTES), timeout=SPEC_TIMEOUT)
        handle = supervisor.start(spec)
        result, elapsed = _wait_terminal(supervisor, handle.run_id)
        state = supervisor.inspect(handle.run_id)["state"]

    check("a run 128x over the cap does not hang (terminates within watchdog)",
          result is not None)
    check("the run reaches a terminal state, not stranded active",
          state in run_supervisor._TERMINAL_STATES)
    if result is None:
        return True
    check("terminates far faster than the wall-clock backstop",
          elapsed < STAGE_WATCHDOG)
    check("returned stdout is BOUNDED near the cap, not the full 8 MiB",
          len(result.get("stdout", "")) < CAP_BOUND)
    check("the output was truncated, not lost",
          result.get("output_truncated") is True)
    check("the overflow is measured above the cap, not silently dropped",
          _measured_over_cap(result, "stdout"))
    return False


def test_both_pipes_over_the_cap_do_not_deadlock() -> bool:
    """Large stdout AND large stderr at once — the exact shape a single-threaded
    sequential reader deadlocks on (blocked draining one pipe while the child
    blocks writing the other). Both streams must terminate, bound, and measure.
    Returns True if the run HUNG."""
    with tempfile.TemporaryDirectory(prefix="codecalc-bp-both-") as root:
        supervisor = _supervisor(root)
        spec = providers.ComputationSpec(
            "python3", _producer(PRODUCER_BYTES, PRODUCER_BYTES), timeout=SPEC_TIMEOUT)
        handle = supervisor.start(spec)
        result, _elapsed = _wait_terminal(supervisor, handle.run_id)
        state = supervisor.inspect(handle.run_id)["state"]

    check("a run flooding BOTH pipes does not hang", result is not None)
    check("the two-pipe run reaches a terminal state",
          state in run_supervisor._TERMINAL_STATES)
    if result is None:
        return True
    check("stdout is bounded near the cap", len(result.get("stdout", "")) < CAP_BOUND)
    check("stderr is bounded near the cap", len(result.get("stderr", "")) < CAP_BOUND)
    # The overflow-kill is prompt AND asymmetric: it fires the instant the FIRST
    # stream crosses the cap, so the second may be cut short below it — that is
    # the anti-deadlock behaviour, not a defect. What must hold is that the
    # combined overflow was reported (output_truncated) and the stream that
    # triggered it was measured over the cap; requiring BOTH to individually
    # exceed the cap would assert a race the prompt kill deliberately loses.
    check("the two-pipe overflow is reported, not silently dropped",
          result.get("output_truncated") is True)
    check("at least one flooded stream is measured above the cap",
          _measured_over_cap(result, "stdout") or _measured_over_cap(result, "stderr"))
    return False


def test_concurrent_runs_over_worker_count_none_deadlock() -> bool:
    """CONCURRENT_RUNS (12) > the pool's 8 workers, every one flooding stdout.
    The pool must drain a backlog of large-output producers with no worker
    wedged on a full pipe.

    Crucially the wait is CONCURRENT, not serial: one `futures.wait` over all 12
    futures with a SINGLE bound. A per-run `wait()` in a loop would, under a real
    deadlock, sum to (workers x 60 s wall-clock) worst case and outlast the
    global backstop before this stage could report — the exact defect this
    revision fixes. Returns True if any run HUNG."""
    with tempfile.TemporaryDirectory(prefix="codecalc-bp-concurrent-") as root:
        supervisor = _supervisor(root)
        handles = [
            supervisor.start(providers.ComputationSpec(
                "python3", _producer(PRODUCER_BYTES), timeout=SPEC_TIMEOUT))
            for _ in range(CONCURRENT_RUNS)
        ]
        futures = [supervisor._runs[h.run_id].future for h in handles]
        # ONE bounded wait over ALL futures at once — they run concurrently in
        # the pool, so this returns when the slowest settles or the watchdog
        # fires, never workers x wall-clock.
        _done, not_done = futures_wait(futures, timeout=CONCURRENT_WATCHDOG)
        hung = len(not_done) > 0

        results = []
        terminal = 0
        for handle in handles:
            future = supervisor._runs[handle.run_id].future
            # Only collect a future that has already settled — a wait() on a
            # still-pending one would re-introduce a blocking call the single
            # bound above deliberately avoids.
            result = supervisor.wait(handle.run_id, timeout=0) if future.done() else None
            results.append(result)
            if supervisor.inspect(handle.run_id)["state"] in run_supervisor._TERMINAL_STATES:
                terminal += 1

    check(f"all {CONCURRENT_RUNS} concurrent large-output runs terminate (none hang)",
          all(r is not None for r in results))
    check(f"all {CONCURRENT_RUNS} reach a terminal state", terminal == CONCURRENT_RUNS)
    check(f"all {CONCURRENT_RUNS} produce bounded output",
          all(r is not None and len(r.get("stdout", "")) < CAP_BOUND for r in results))
    check("each concurrent run's overflow was measured above the cap",
          all(r is not None and _measured_over_cap(r, "stdout") for r in results))
    return hung


def _run_stages() -> None:
    """Run the stages in order, short-circuiting on the FIRST detected hang: the
    later stages would hang identically, so running them would only add
    watchdog-length delays to a diagnosis already made and flushed. A real
    deadlock therefore fails in one stage's worth of seconds with a named line."""
    if test_a_single_run_far_over_the_cap_terminates_with_bounded_output():
        return
    if test_both_pipes_over_the_cap_do_not_deadlock():
        return
    test_concurrent_runs_over_worker_count_none_deadlock()


if __name__ == "__main__":
    print(f"backend under test: {executor.backend()}")
    sys.stdout.flush()
    # Last resort only — the per-stage watchdogs are what actually report a
    # deadlock (fast, named, flushed). This just guarantees the process cannot be
    # pinned open indefinitely if a hang ever escapes all of them.
    faulthandler.dump_traceback_later(GLOBAL_DEADLINE, exit=True)
    try:
        _run_stages()
    finally:
        faulthandler.cancel_dump_traceback_later()

    sys.stdout.flush()
    sys.stderr.flush()
    if FAILS:
        # A detected hang leaves pool workers blocked until the executor's 60 s
        # wall-clock kill; a normal interpreter exit would join them via
        # ThreadPoolExecutor's atexit and re-hang CI for ~a minute AFTER the
        # diagnosis is already printed. os._exit skips that join. Safe: every
        # check() line was flushed as it printed, so nothing is lost here.
        os._exit(1)
    sys.exit(0)
