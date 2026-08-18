"""Output backpressure cannot deadlock the payload or supervisor (THE-831).

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
STOPS reading at the cap — would let the child block on a full pipe forever;
`supervisor.wait(timeout=...)` here is the watchdog that turns such a hang into
a failed check instead of a hung CI job. A module-level faulthandler is a hard
backstop in case a hang somehow escapes an individual wait().

Runs on both backends: the producer is portable python3 (`sys.stdout.write`),
never bash, so the fallback matrix job exercises it too.
"""

from __future__ import annotations

import faulthandler
import sys
import tempfile
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

from codecalc import executor, providers, run_supervisor

FAILS: list[str] = []

#: Far more than one 64 KiB pipe buffer AND more than the 64 KiB default output
#: cap: a naive reader deadlocks on the pipe long before this much is written.
PRODUCER_BYTES = 8 * 1024 * 1024
#: The executor's own wall-clock ceiling. Deliberately generous so a CORRECT
#: run (which finishes in well under a second) never approaches it — it is the
#: last-resort backstop, not the property under test.
SPEC_TIMEOUT = 60
#: The watchdog: a correct run terminates almost instantly, so a bound this far
#: below SPEC_TIMEOUT means a run that only ends via the wall-clock kill (i.e. a
#: real output-backpressure deadlock) trips it and fails the check.
WAIT_WATCHDOG = 20
#: More runs than the pool's default worker count (8): the pool must clear a
#: backlog of large-output producers without any of them wedging a worker.
CONCURRENT_RUNS = 12
#: A correct result truncates to ~64 KiB; anything near PRODUCER_BYTES means the
#: cap was applied after the fact (an allocation, not a ceiling) — or not at all.
CAP_BOUND = 256 * 1024
#: The default output cap (executor.MAX_OUTPUT_BYTES): the measured produced
#: size must exceed it, proving the run really did overflow the ceiling.
DEFAULT_CAP = executor.MAX_OUTPUT_BYTES
#: Hard CI backstop: abort with a traceback rather than hang if a deadlock ever
#: escapes an individual wait() watchdog.
GLOBAL_DEADLINE = 180


def check(name: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name}")
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


def _wait_terminal(supervisor, run_id, *, watchdog=WAIT_WATCHDOG):
    """Collect a run under the watchdog. Returns (result, elapsed) or (None,
    elapsed) if it did NOT terminate in time — a hang, reported as a failed
    check rather than a hung process."""
    started = time.monotonic()
    try:
        result = supervisor.wait(run_id, timeout=watchdog)
    except FutureTimeoutError:
        return None, time.monotonic() - started
    return result, time.monotonic() - started


def test_a_single_run_far_over_the_cap_terminates_with_bounded_output() -> None:
    """A run writing 8 MiB of stdout — 128x the pipe buffer and the cap — must
    reach a terminal state promptly (no deadlock) with output truncated to the
    cap, and the full produced size still MEASURED so nothing vanishes silently."""
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
        return
    check("terminates far faster than the wall-clock backstop",
          elapsed < WAIT_WATCHDOG)
    check("returned stdout is BOUNDED near the cap, not the full 8 MiB",
          len(result.get("stdout", "")) < CAP_BOUND)
    check("the output was truncated, not lost",
          result.get("output_truncated") is True)
    check("the overflow is measured above the cap, not silently dropped",
          _measured_over_cap(result, "stdout"))


def test_both_pipes_over_the_cap_do_not_deadlock() -> None:
    """Large stdout AND large stderr at once — the exact shape a single-threaded
    sequential reader deadlocks on (blocked draining one pipe while the child
    blocks writing the other). Both streams must terminate, bound, and measure."""
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
        return
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


def test_concurrent_runs_over_worker_count_none_deadlock() -> None:
    """CONCURRENT_RUNS (12) > the pool's 8 workers, every one flooding stdout.
    The pool must drain a backlog of large-output producers with no worker
    wedged on a full pipe: all reach a terminal state, all bounded, all inside
    the watchdog."""
    with tempfile.TemporaryDirectory(prefix="codecalc-bp-concurrent-") as root:
        supervisor = _supervisor(root)
        handles = [
            supervisor.start(providers.ComputationSpec(
                "python3", _producer(PRODUCER_BYTES), timeout=SPEC_TIMEOUT))
            for _ in range(CONCURRENT_RUNS)
        ]
        results = []
        terminal = 0
        bounded = 0
        for handle in handles:
            result, _elapsed = _wait_terminal(supervisor, handle.run_id)
            results.append(result)
            if supervisor.inspect(handle.run_id)["state"] in run_supervisor._TERMINAL_STATES:
                terminal += 1
            if result is not None and len(result.get("stdout", "")) < CAP_BOUND:
                bounded += 1

    check(f"all {CONCURRENT_RUNS} concurrent large-output runs terminate (none hang)",
          all(r is not None for r in results))
    check(f"all {CONCURRENT_RUNS} reach a terminal state", terminal == CONCURRENT_RUNS)
    check(f"all {CONCURRENT_RUNS} produce bounded output", bounded == CONCURRENT_RUNS)
    check("each concurrent run's overflow was measured above the cap",
          all(r is not None and _measured_over_cap(r, "stdout") for r in results))


if __name__ == "__main__":
    print(f"backend under test: {executor.backend()}")
    faulthandler.dump_traceback_later(GLOBAL_DEADLINE, exit=True)
    try:
        test_a_single_run_far_over_the_cap_terminates_with_bounded_output()
        test_both_pipes_over_the_cap_do_not_deadlock()
        test_concurrent_runs_over_worker_count_none_deadlock()
    finally:
        faulthandler.cancel_dump_traceback_later()
    sys.exit(1 if FAILS else 0)
