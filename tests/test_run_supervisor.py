"""Provider-neutral run lifecycle contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from codecalc import contract, providers, run_supervisor

FAILS: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name}")
    if not condition:
        FAILS.append(name)


class BlockingProvider(providers.LocalExecutionProvider):
    provider_id = "blocking"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cancelled: list[str] = []
        self.cleaned: list[str] = []
        self.started: list[str] = []

    def describe(self) -> dict:
        result = super().describe()
        result["provider_id"] = self.provider_id
        result["capabilities"]["cancel"] = True
        result["capabilities"]["cleanup"] = True
        result["capabilities"]["managed_runs"] = True
        return result

    def execute_managed(self, run_id: str,
                        spec: providers.ComputationSpec) -> dict:
        self.started.append(run_id)
        self.entered.set()
        self.release.wait(5)
        return contract.stamp({
            "ok": True, "verdict": "OK", "stdout": spec.code,
            "stderr": "", "exit_code": 0, "unenforced": [],
        })

    def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)
        self.release.set()

    def cleanup(self, run_id: str) -> None:
        self.cleaned.append(run_id)


def test_lifecycle_is_provider_bound_and_cleanup_is_idempotent() -> None:
    provider = BlockingProvider()
    registry = providers.ProviderRegistry(default_provider_id=provider.provider_id)
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-") as root:
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        handle = supervisor.start(providers.ComputationSpec("python3", "owned"))
        provider.entered.wait(1)
        running = supervisor.inspect(handle.run_id)
        supervisor.cancel(handle.run_id)
        result = supervisor.wait(handle.run_id, timeout=2)
        first = supervisor.cleanup(handle.run_id)
        second = supervisor.cleanup(handle.run_id)

    check("run handle binds provider identity",
          handle.provider_id == provider.provider_id)
    check("provider receives the supervisor run identity",
          provider.started == [handle.run_id])
    check("running state is inspectable", running["state"] == "running")
    check("cancel reaches the owning provider", provider.cancelled == [handle.run_id])
    check("wait retains final result", result["stdout"] == "owned")
    check("cleanup reaches provider exactly once", provider.cleaned == [handle.run_id])
    check("cleanup is idempotent", first["cleaned"] and second["already_cleaned"])


def test_journal_is_bounded_and_orphans_are_reconciled() -> None:
    provider = BlockingProvider()
    registry = providers.ProviderRegistry(default_provider_id=provider.provider_id)
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-") as root:
        state_dir = Path(root)
        orphan = state_dir / "orphan.json"
        orphan.write_text(json.dumps({
            "run_id": "orphan", "provider_id": provider.provider_id,
            "state": "running",
        }), encoding="utf-8")
        supervisor = run_supervisor.RunSupervisor(
            registry, state_dir=state_dir, max_completed=1
        )
        recovered = supervisor.recover_orphans()
        provider.release.set()
        first = supervisor.start(providers.ComputationSpec("python3", "one"))
        supervisor.wait(first.run_id, timeout=2)
        supervisor.cleanup(first.run_id)
        second = supervisor.start(providers.ComputationSpec("python3", "two"))
        supervisor.wait(second.run_id, timeout=2)
        supervisor.cleanup(second.run_id)

        journals = sorted(state_dir.glob("*.json"))

    check("restart recovery cancels orphan through its provider",
          recovered == ["orphan"] and provider.cancelled == ["orphan"])
    check("completed run journal is bounded", len(journals) <= 1)


class NoCleanupCapabilityProvider(providers.LocalExecutionProvider):
    """A provider that does NOT advertise `cleanup` — the shape LocalExecution-
    Provider itself is: capabilities={"cleanup": False, ...}, and its own
    cleanup() raises UnsupportedCapability unconditionally if ever called."""

    provider_id = "no-cleanup"

    def describe(self) -> dict:
        result = super().describe()
        result["provider_id"] = self.provider_id
        result["capabilities"]["managed_runs"] = True
        # cleanup stays False — inherited from LocalExecutionProvider.describe()
        return result

    def execute_managed(self, run_id: str, spec: providers.ComputationSpec) -> dict:
        from codecalc import contract as _contract
        return _contract.stamp({
            "ok": True, "verdict": "OK", "stdout": spec.code,
            "stderr": "", "exit_code": 0, "unenforced": [],
        })


def test_cleanup_is_capability_gated_and_does_not_crash_on_a_provider_without_it() -> None:
    """Residual: run_supervisor.cleanup() unconditionally called
    provider.cleanup(), and LocalExecutionProvider.cleanup() raises
    UnsupportedCapability (its own `cleanup` capability is False). That was
    latent as long as cleanup() was only reached from ExecutionService's
    managed-provider branch, where the one managed provider happens to also
    advertise cleanup=True. run_submit/run_inspect/run_cancel call cleanup()
    for ANY selected provider, which is what surfaces it — this is the
    regression test for the capability guard added alongside them."""
    provider = NoCleanupCapabilityProvider()
    registry = providers.ProviderRegistry(default_provider_id=provider.provider_id)
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-nocleanup-") as root:
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        handle = supervisor.start(providers.ComputationSpec("python3", "no crash"))
        supervisor.wait(handle.run_id, timeout=5)
        raised = None
        try:
            result = supervisor.cleanup(handle.run_id)
        except Exception as exc:
            raised = exc
            result = None

    check("cleanup() does not raise for a provider that does not advertise cleanup",
          raised is None)
    check("cleanup() still marks the run cleaned",
          result is not None and result["cleaned"] is True)


class UncancellableBlockingProvider(providers.LocalExecutionProvider):
    """LocalExecutionProvider's own shape — no cancel, no cleanup, not a
    managed_runs provider — but blocks in execute() until released, so a
    cancel attempt races a REAL running state rather than a terminal one."""

    provider_id = "uncancellable-blocking"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def describe(self) -> dict:
        result = super().describe()
        result["provider_id"] = self.provider_id
        return result

    def execute(self, spec: providers.ComputationSpec) -> dict:
        self.entered.set()
        self.release.wait(10)
        return contract.stamp({
            "ok": True, "verdict": "OK", "stdout": spec.code,
            "stderr": "", "exit_code": 0, "unenforced": [],
        })


def test_cancel_on_a_provider_that_cannot_cancel_leaves_the_run_collectible() -> None:
    """fix round, review Critical #1 — the reviewer's own repro:
    cancel() used to set state="cancelling" BEFORE calling provider.cancel(),
    and a provider that does not advertise `cancel` (LocalExecutionProvider)
    raises UnsupportedCapability there unconditionally. The state was never
    reverted, and inspect() only auto-collected from state=="running" — so a
    run whose OWN work finished normally after a failed cancel attempt
    polled "cancelling" forever, even though wait() would have returned the
    finished result immediately. This is that exact sequence: submit ->
    cancel (honest failure) -> the run completes on its own -> inspect()
    must still reach a terminal state and return the collected result."""
    provider = UncancellableBlockingProvider()
    registry = providers.ProviderRegistry(default_provider_id=provider.provider_id)
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-cancel-strand-") as root:
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        handle = supervisor.start(providers.ComputationSpec("python3", "not stranded"))
        provider.entered.wait(2)

        raised = None
        try:
            supervisor.cancel(handle.run_id)
        except providers.UnsupportedCapability as exc:
            raised = exc

        mid_state = supervisor.inspect(handle.run_id)["state"]
        provider.release.set()  # the run's OWN work finishes now, unrelated to the cancel attempt

        terminal_state = None
        for _ in range(200):
            state = supervisor.inspect(handle.run_id)["state"]
            if state in {"finished", "cleaned"}:
                terminal_state = state
                break
            time.sleep(0.02)

    check("cancel() on a non-cancelling provider raises UnsupportedCapability, "
          "not a silent success", raised is not None)
    check("the run's state was never mutated by the failed cancel attempt",
          mid_state == "running")
    check("the run reaches a terminal state once its own work finishes "
          "(not stranded at 'cancelling' forever)", terminal_state is not None)
    if terminal_state is not None:
        result = supervisor.wait(handle.run_id, timeout=1)
        check("the collected result is the run's real output",
              result.get("stdout") == "not stranded")


def test_admission_cap_rejects_a_submission_past_the_limit() -> None:
    """fix round, review Important #3a."""
    provider = BlockingProvider()
    registry = providers.ProviderRegistry(default_provider_id=provider.provider_id)
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-cap-") as root:
        supervisor = run_supervisor.RunSupervisor(
            registry, state_dir=Path(root), max_active_runs=1,
        )
        first = supervisor.start(providers.ComputationSpec("python3", "one"))
        provider.entered.wait(2)
        raised = None
        try:
            supervisor.start(providers.ComputationSpec("python3", "two"))
        except run_supervisor.TooManyActiveRuns as exc:
            raised = exc
        provider.release.set()
        supervisor.wait(first.run_id, timeout=2)

    check("a submission past the cap raises TooManyActiveRuns", raised is not None)
    if raised is not None:
        check("the exception reports the active count and the configured limit",
              raised.active == 1 and raised.limit == 1)


def test_a_set_but_empty_admission_cap_does_not_crash_the_server_at_import() -> None:
    """`CODECALC_MAX_ACTIVE_RUNS=""` used to raise at module import.

    `int(os.environ.get(NAME, "64"))` only defaults when the variable is
    ABSENT. Set-but-empty is the shape a shell produces for
    `export CODECALC_MAX_ACTIVE_RUNS=` or a compose file with a blank value,
    and it reaches `int("")` -> ValueError with no handler above it, so the
    whole MCP server fails to start — the same fail-shape as the allowlist
    empty-string bug, one module over. Asserted by IMPORTING in a
    subprocess, because an import-time crash is not observable from inside a
    process that already imported the module successfully.
    """
    repo = Path(__file__).resolve().parents[1]
    for label, value, want in (
        ("empty", "", 64),
        ("whitespace", "   ", 64),
        ("non-numeric", "lots", 64),
        ("zero", "0", 64),
        ("negative", "-5", 64),
        ("valid", "7", 7),
    ):
        env = dict(os.environ, CODECALC_MAX_ACTIVE_RUNS=value)
        proc = subprocess.run(
            [sys.executable, "-c",
             "from codecalc import server; print(server._max_active_runs)"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=180,
        )
        check(f"CODECALC_MAX_ACTIVE_RUNS={label!r} imports the server without raising",
              proc.returncode == 0)
        got = proc.stdout.strip().splitlines()[-1] if proc.returncode == 0 else None
        check(f"CODECALC_MAX_ACTIVE_RUNS={label!r} resolves to {want}", got == str(want))


class _StuckProvider(providers.LocalExecutionProvider):
    """Blocks forever (within the test) — always the OLDEST journal by
    mtime, and always still active."""

    provider_id = "stuck"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def describe(self) -> dict:
        result = super().describe()
        result["provider_id"] = self.provider_id
        return result

    def execute(self, spec: providers.ComputationSpec) -> dict:
        self.entered.set()
        self.release.wait(10)
        return contract.stamp({
            "ok": True, "verdict": "OK", "stdout": spec.code,
            "stderr": "", "exit_code": 0, "unenforced": [],
        })


class _FastProvider(providers.LocalExecutionProvider):
    """Returns immediately — used for the burst of completed runs."""

    provider_id = "fast"

    def describe(self) -> dict:
        result = super().describe()
        result["provider_id"] = self.provider_id
        return result

    def execute(self, spec: providers.ComputationSpec) -> dict:
        return contract.stamp({
            "ok": True, "verdict": "OK", "stdout": spec.code,
            "stderr": "", "exit_code": 0, "unenforced": [],
        })


def test_prune_never_deletes_a_still_active_runs_journal() -> None:
    """fix round, review Important #3b (first half — state-aware
    pruning): _prune() used to sort EVERY `*.json` by mtime with no read of
    its recorded state, so a long-running job's journal — written once at
    start() and untouched since, so often the OLDEST by mtime — could be
    pruned out from under it by a burst of shorter runs completing around
    it. If the process then crashed before that run finished,
    recover_orphans() would have nothing left to recover it from.

    Triggered through cleanup() specifically, not wait()/inspect() alone:
    cleanup() already called _prune() before this fix round (only the
    MISSING state check is new here), so this isolates the state-awareness
    half from the "_collect() also prunes" half tested separately below —
    a version with the state check alone but without the wait()/inspect()
    trigger would still pass this one.
    """
    stuck_provider = _StuckProvider()
    fast_provider = _FastProvider()
    registry = providers.ProviderRegistry(default_provider_id=fast_provider.provider_id)
    registry.register(stuck_provider)
    registry.register(fast_provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-prune-state-") as root:
        state_dir = Path(root)
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=state_dir, max_completed=1)

        stuck = supervisor.start(providers.ComputationSpec("python3", "stuck"), provider_id="stuck")
        stuck_provider.entered.wait(2)

        for i in range(3):
            handle = supervisor.start(
                providers.ComputationSpec("python3", f"done-{i}"), provider_id="fast"
            )
            supervisor.wait(handle.run_id, timeout=2)
            supervisor.cleanup(handle.run_id)

        journals = {p.name for p in state_dir.glob("*.json")}
        stuck_journal = f"{stuck.run_id}.json"

        stuck_provider.release.set()
        supervisor.wait(stuck.run_id, timeout=2)  # let the pool finish cleanly

    check("the still-active run's journal survives pruning despite being oldest",
          stuck_journal in journals)
    check("completed journals are still bounded to max_completed",
          len(journals - {stuck_journal}) <= 1)


def test_prune_runs_on_terminal_collection_not_only_cleanup() -> None:
    """fix round, review Important #3b (second half — prune on
    collection): before this, _prune() was reachable ONLY from
    cleanup()/recover_orphans(). A caller that only ever calls run_submit
    and polls run_inspect — never cleanup() directly, which is exactly how
    an MCP caller normally uses these tools — left the on-disk journal
    growing UNBOUNDED regardless of how max_completed was set, because
    nothing ever triggered a prune at all. This calls wait() (which
    auto-collects, same as an inspect() poll reaching a terminal state) and
    deliberately never calls cleanup()."""
    fast_provider = _FastProvider()
    registry = providers.ProviderRegistry(default_provider_id=fast_provider.provider_id)
    registry.register(fast_provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-prune-collect-") as root:
        state_dir = Path(root)
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=state_dir, max_completed=1)
        for i in range(4):
            handle = supervisor.start(providers.ComputationSpec("python3", f"done-{i}"))
            supervisor.wait(handle.run_id, timeout=2)  # NEVER calls cleanup()

        journals = list(state_dir.glob("*.json"))

    check("terminal collection alone (no cleanup() call) still bounds the journal",
          len(journals) <= 1)


class _RaisingProvider(providers.LocalExecutionProvider):
    """execute() RAISES — the background-run analogue of the cancel-stranding
    bug. Pre-fix, _collect() called future.result() unchecked, so the raise
    left the run stuck 'running' with its admission slot held and its result
    unreachable via inspect()."""

    provider_id = "raising"

    def describe(self) -> dict:
        result = super().describe()
        result["provider_id"] = self.provider_id
        return result

    def execute(self, spec: providers.ComputationSpec) -> dict:
        del spec
        raise providers.UnsupportedCapability(self.provider_id, "execute")


def test_a_provider_that_raises_becomes_a_terminal_failed_result_not_a_strand() -> None:
    """F1 (cross-vendor): a provider whose execute() RAISES must not strand the
    run in 'running' forever with its admission slot held and its result
    unreachable via inspect(). ANY exception (provider error, receipt-
    construction failure) becomes a TERMINAL failed result — inspectable once,
    stable code — and the slot is freed. Same class as the cancel-stranding
    fix, different trigger (the future's own body raised)."""
    provider = _RaisingProvider()
    registry = providers.ProviderRegistry(default_provider_id=provider.provider_id)
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-raise-") as root:
        supervisor = run_supervisor.RunSupervisor(
            registry, state_dir=Path(root), max_active_runs=1)
        handle = supervisor.start(providers.ComputationSpec("python3", "boom"))

        terminal = None
        raised = None
        for _ in range(200):
            try:
                status = supervisor.inspect(handle.run_id)
            except Exception as exc:  # pre-fix: the provider's raise escapes here
                raised = exc
                break
            if status["state"] in run_supervisor._TERMINAL_STATES:
                terminal = status
                break
            time.sleep(0.02)

        result = supervisor.wait(handle.run_id, timeout=1) if terminal else None

        # The failed run's slot must be freed for the next submission (cap=1).
        second_raised = None
        second = None
        try:
            second = supervisor.start(providers.ComputationSpec("python3", "next"))
        except run_supervisor.TooManyActiveRuns as exc:
            second_raised = exc

    check("inspect() never lets the provider exception escape", raised is None)
    check("a raising run reaches a TERMINAL state, not stranded at 'running'",
          terminal is not None)
    check("the terminal result is a coded failure, inspectable once",
          result is not None and result.get("ok") is False and bool(result.get("code")))
    check("the failed run's admission slot is freed for the next submit",
          second_raised is None and second is not None)


def test_a_completed_but_uninspected_run_frees_its_admission_slot() -> None:
    """F5 (cross-vendor): admission counts stored 'running' state, and a done
    future stays 'running' until inspected/waited. With max_active_runs=1 a fast
    finished run still made the next submit RESOURCE_EXHAUSTED — contradicting
    the error's own 'wait for one to finish, then retry'. start() now reaps done
    futures before counting, so COMPLETION frees a slot, not just inspection."""
    provider = _FastProvider()
    registry = providers.ProviderRegistry(default_provider_id=provider.provider_id)
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-slot-") as root:
        supervisor = run_supervisor.RunSupervisor(
            registry, state_dir=Path(root), max_active_runs=1)
        first = supervisor.start(providers.ComputationSpec("python3", "one"))
        # Let it finish WITHOUT inspect()/wait() — the future is done, but the
        # run is still RECORDED 'running' because nothing collected it.
        supervisor._runs[first.run_id].future.result(timeout=2)

        raised = None
        second = None
        try:
            second = supervisor.start(providers.ComputationSpec("python3", "two"))
        except run_supervisor.TooManyActiveRuns as exc:
            raised = exc

    check("a completed-but-uninspected run does not hold its admission slot",
          raised is None and second is not None)


def test_recover_orphans_skips_an_unregistered_provider_without_crashing() -> None:
    """A journalled active run naming a provider NOT registered this
    boot must not crash recover_orphans() — which runs at server IMPORT time.
    registry.select() raises UnknownProvider (a LookupError) for such a run;
    the pre-fix except (KeyError, OSError, ValueError, TypeError,
    JSONDecodeError) did not catch it, so a single stale journal took the whole
    server down at startup until the file was removed by hand. The unknown-
    provider run is SKIPPED and its journal PRESERVED (the provider may register
    again next boot), while every other orphan still recovers. The ghost sorts
    FIRST, so a passing 'live' recovery also proves the loop continued past it."""
    provider = BlockingProvider()
    registry = providers.ProviderRegistry(default_provider_id=provider.provider_id)
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-ghost-") as root:
        state_dir = Path(root)
        ghost = state_dir / "ghost.json"
        ghost.write_text(json.dumps({
            "run_id": "ghost", "provider_id": "not-registered-this-boot",
            "state": "running",
        }), encoding="utf-8")
        live = state_dir / "live.json"
        live.write_text(json.dumps({
            "run_id": "live", "provider_id": provider.provider_id,
            "state": "running",
        }), encoding="utf-8")
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=state_dir)

        raised = None
        recovered = None
        try:
            recovered = supervisor.recover_orphans()
        except Exception as exc:  # pre-fix: UnknownProvider escapes here
            raised = exc

        ghost_after = (
            json.loads(ghost.read_text(encoding="utf-8")) if ghost.exists() else None
        )

    check("recover_orphans does not raise on an unregistered provider",
          raised is None)
    check("an orphan naming a registered provider still recovers past the skip",
          recovered == ["live"] and provider.cancelled == ["live"])
    check("the unregistered-provider journal is preserved, not deleted",
          ghost_after is not None and ghost_after.get("state") == "running")


def test_start_journals_the_workdir_before_the_runner_can_possibly_run() -> None:
    """cross-vendor review: `start()` used to `self._pool.submit()` BEFORE
    inserting/journalling the `_Run` record — a hard kill in that window
    left a workdir the worker had already started installing into (or was
    about to) with NO on-disk record naming it, so `recover_orphans()`'s
    startup sweep would have nothing to find. The fix journals FIRST, inside
    the SAME lock, before the worker thread is given any chance to run at
    all — proven here with a runner that blocks on an Event: `start()`
    returns (having already written the journal) while the runner is still
    provably blocked on its very first statement, and the journal is read
    in that window, not after the runner finishes."""
    from codecalc import executor

    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(providers.LocalExecutionProvider())
    entered = threading.Event()
    release = threading.Event()

    def blocking_runner() -> dict:
        entered.set()
        release.wait(5)
        return {"ok": True, "verdict": "OK", "stdout": "done", "stderr": "",
                "exit_code": 0, "unenforced": []}

    workdir = tempfile.mkdtemp(prefix="codecalc-deps-journal-order-")
    identity = executor._dir_identity(workdir)
    handle = None
    journal_exists_immediately = False
    record = None
    runner_entered = False
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-journal-order-") as root:
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        try:
            handle = supervisor.start(
                providers.ComputationSpec("python3", "unused"),
                runner=blocking_runner, workdir=workdir, workdir_identity=identity,
            )
            # The journal is checked IMMEDIATELY after start() returns, before
            # the runner is ever released — under the fix this is not a race:
            # the journal write happens before self._pool.submit() is even
            # called, so it is unconditionally on disk by the time start()
            # returns, regardless of how the worker thread gets scheduled.
            journal_path = supervisor._path(handle.run_id)
            journal_exists_immediately = journal_path.exists()
            record = (json.loads(journal_path.read_text(encoding="utf-8"))
                      if journal_exists_immediately else None)
            # Extra assurance the runner really is concurrent, not merely
            # not-yet-scheduled: confirm it actually entered and is blocked
            # on its first statement — the journal above was already read
            # before this point, so this does not loosen the assertion.
            runner_entered = entered.wait(2)
        finally:
            release.set()
            if handle is not None:
                supervisor.wait(handle.run_id, timeout=2)
    import shutil as _shutil
    _shutil.rmtree(workdir, ignore_errors=True)

    check("the runner genuinely started running (blocked on the Event)",
          runner_entered)
    check("the journal file exists the instant start() returns",
          journal_exists_immediately)
    check("the journal already names the workdir, unreleased",
          record is not None and record.get("workdir") == workdir
          and record.get("workdir_cleaned") is False,
          )


def test_start_pops_the_run_and_releases_its_workdir_when_pool_submit_raises() -> None:
    """cross-vendor review: `self._pool.submit()` CAN raise —
    ThreadPoolExecutor spawns worker threads lazily, and "cannot start new
    thread" under OS thread exhaustion is a real, reproduced failure. By the
    time it is called, the run is ALREADY in `self._runs` and journalled
    (journal-before-submit, the previous round's own fix). Left there with
    `future=None` forever, the run would never terminate, and EVERY later
    `start()`/`inspect()` call would crash dereferencing None in
    `_reap_completed_locked()`'s/`inspect()`'s own `run.future.done()` — a
    process-wide denial of service until restart. `start()` must instead
    pop the half-created run, release the workdir it owned, journal it
    TERMINAL, and re-raise — leaving the supervisor exactly as usable as
    before the failed call."""
    from codecalc import executor

    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(providers.LocalExecutionProvider())
    workdir = tempfile.mkdtemp(prefix="codecalc-deps-submit-fails-")
    identity = executor._dir_identity(workdir)

    def runner() -> dict:
        return {"ok": True, "verdict": "OK", "stdout": "unreachable",
                "stderr": "", "exit_code": 0, "unenforced": []}

    second_workdir = None
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-submit-fails-") as root:
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        real_submit = supervisor._pool.submit
        calls = {"n": 0}

        def failing_submit(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("cannot start new thread")
            return real_submit(*args, **kwargs)

        supervisor._pool.submit = failing_submit

        raised = None
        try:
            supervisor.start(
                providers.ComputationSpec("python3", "unused"),
                runner=runner, workdir=workdir, workdir_identity=identity,
            )
        except Exception as exc:
            raised = exc

        run_absent = len(supervisor._runs) == 0
        workdir_released = not Path(workdir).exists()

        # A SECOND start() (submit now succeeds) must work normally — the
        # supervisor is not left stuck by the first failure.
        second_workdir = tempfile.mkdtemp(prefix="codecalc-deps-submit-fails-2-")
        second_identity = executor._dir_identity(second_workdir)
        handle = supervisor.start(
            providers.ComputationSpec("python3", "unused"),
            runner=runner, workdir=second_workdir, workdir_identity=second_identity,
        )
        second_result = supervisor.wait(handle.run_id, timeout=2)

    check("start() re-raises the pool's own submit failure",
          isinstance(raised, RuntimeError) and "cannot start new thread" in str(raised),
          )
    check("the half-created run is NOT left in the supervisor", run_absent)
    check("the workdir it owned was released", workdir_released)
    check("a second start() succeeds after the first's submit failure",
          second_result.get("stdout") == "unreachable" and second_result.get("ok") is True,
          )

    if second_workdir is not None:
        import shutil as _shutil
        _shutil.rmtree(second_workdir, ignore_errors=True)


def test_reap_and_inspect_skip_a_record_with_no_future_yet() -> None:
    """defence in depth (cross-vendor review): `start()` itself never
    leaves a None-future record in `self._runs` any more — a failed
    `self._pool.submit()` pops it immediately, under the same lock (see
    `test_start_pops_the_run_and_releases_its_workdir_when_pool_submit_raises`).
    `_reap_completed_locked()`/`inspect()` must still not crash
    dereferencing `.done()` on one anyway, in case that invariant is ever
    violated by a future change — constructed directly here, bypassing
    `start()` entirely, to exercise the GUARD itself rather than the
    invariant it defends against."""
    from codecalc import run_supervisor as rs

    provider = providers.LocalExecutionProvider()
    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-none-future-") as root:
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        handle = run_supervisor.RunHandle(
            run_id="none-future-run", provider_id=provider.provider_id,
            started_at=time.time(), deadline=time.time() + 30)
        bare_run = rs._Run(handle=handle, provider=provider,
                           spec=providers.ComputationSpec("python3", "unused"),
                           future=None)
        supervisor._runs["none-future-run"] = bare_run

        reap_raised = None
        try:
            with supervisor._lock:
                supervisor._reap_completed_locked()
        except Exception as exc:
            reap_raised = exc

        inspect_raised = None
        inspected = None
        try:
            inspected = supervisor.inspect("none-future-run")
        except Exception as exc:
            inspect_raised = exc

    check("_reap_completed_locked() does not crash on a None-future record",
          reap_raised is None, )
    check("inspect() does not crash on a None-future record either",
          inspect_raised is None)
    check("inspect() still reports something sane (still running, not collected)",
          inspected is not None and inspected.get("state") == "running")


def test_start_runner_replaces_provider_dispatch_and_owns_a_workdir() -> None:
    """`start(runner=..., workdir=..., workdir_identity=...)` is what
    run_submit's dependency-install path uses: `runner` runs on the SAME
    worker thread instead of `provider.execute`, and the workdir it names is
    released by `_collect()` exactly once, whichever caller (inspect/wait/
    the next submission's own reap) happens to be the one that collects it
    first — never twice, and never by anyone else."""
    from codecalc import executor

    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(providers.LocalExecutionProvider())
    calls: list[str] = []

    def runner() -> dict:
        calls.append("ran")
        return {"ok": True, "verdict": "OK", "stdout": "from-runner",
                "stderr": "", "exit_code": 0, "unenforced": []}

    with tempfile.TemporaryDirectory(prefix="codecalc-runs-workdir-") as root:
        workdir = tempfile.mkdtemp(prefix="codecalc-deps-owned-")
        identity = executor._dir_identity(workdir)
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        handle = supervisor.start(
            providers.ComputationSpec("python3", "unused"),
            runner=runner, workdir=workdir, workdir_identity=identity,
        )
        result = supervisor.wait(handle.run_id, timeout=2)
        workdir_gone_after_first_collect = not Path(workdir).exists()
        # A second collection (inspect, here) must not error and must not
        # try to remove the (already-gone) directory a second time — the
        # `workdir_cleaned` guard in `_collect()` is what this asserts.
        again = supervisor.inspect(handle.run_id)

    check("runner() replaced provider.execute entirely", calls == ["ran"])
    check("the run's OWN result is exactly what runner() returned",
          result.get("stdout") == "from-runner" and result.get("ok") is True)
    check("the runner still gets the SAME receipt attachment provider.execute would",
          isinstance(result.get("provider"), dict))
    check("the owned workdir is released on the run's first collection",
          workdir_gone_after_first_collect)
    check("a second collection does not raise or re-attempt the removal",
          again.get("state") in {"finished", "cleaned"})


def test_coded_run_failure_becomes_the_verbatim_terminal_result() -> None:
    """A `runner` that raises `CodedRunFailure` — run_submit's dependency
    install path, when the install itself fails — must come back as EXACTLY
    that coded result, not the generic `errors.INTERNAL` wrapping every
    OTHER raised exception gets, and with NO execution receipt: the
    provider was never reached, and a receipt claims otherwise."""
    from codecalc import executor

    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(providers.LocalExecutionProvider())
    coded = {
        "ok": False, "code": "resource_exhausted",
        "error": "the run's dependency workdir reached the quota",
        "remedy": "raise the relevant ceiling or reduce the work",
        "provider_error": "dependency_workdir_quota_exceeded",
        "dependencies": [{"spec": "big-pkg", "language": "python3", "ok": True,
                          "installer": "uv", "elapsed_ms": 1}],
    }

    def failing_runner() -> dict:
        raise run_supervisor.CodedRunFailure(dict(coded))

    with tempfile.TemporaryDirectory(prefix="codecalc-runs-codedfail-") as root:
        workdir = tempfile.mkdtemp(prefix="codecalc-deps-codedfail-")
        identity = executor._dir_identity(workdir)
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        handle = supervisor.start(
            providers.ComputationSpec("python3", "unused"),
            runner=failing_runner, workdir=workdir, workdir_identity=identity,
        )
        result = supervisor.wait(handle.run_id, timeout=2)
        workdir_released = not Path(workdir).exists()

    check("the terminal result matches the raised CodedRunFailure verbatim",
          all(result.get(k) == v for k, v in coded.items()))
    check("no execution receipt was attached — the provider was never reached",
          "provider" not in result)
    check("the result still carries a contract_version (stamped)",
          bool(result.get("contract_version")))
    check("the owned workdir was still released despite the failure",
          workdir_released)


def test_recover_orphans_sweeps_an_uncleaned_workdir_for_a_terminal_run() -> None:
    """A run that finished and was journalled `workdir_cleaned: false` an
    instant before the process crashed has no provider-side work left (it
    is already `finished`, not `running`/`cancelling`), but its per-run
    dependency workdir is exactly as orphaned as an active run's provider
    state — nothing else will ever release it once the in-memory `_Run`
    that owned it is gone. `recover_orphans()` at the next startup is that
    release."""
    from codecalc import executor

    provider = BlockingProvider()
    registry = providers.ProviderRegistry(default_provider_id=provider.provider_id)
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-workdir-sweep-") as root:
        state_dir = Path(root)
        workdir = tempfile.mkdtemp(prefix="codecalc-deps-orphaned-")
        identity = executor._dir_identity(workdir)
        journal = state_dir / "orphan-terminal.json"
        journal.write_text(json.dumps({
            "run_id": "orphan-terminal", "provider_id": provider.provider_id,
            "state": "finished", "workdir": workdir,
            "workdir_identity": list(identity), "workdir_cleaned": False,
        }), encoding="utf-8")
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=state_dir)
        supervisor.recover_orphans()
        workdir_gone = not Path(workdir).exists()
        record_after = json.loads(journal.read_text(encoding="utf-8"))

    check("a terminal orphan's workdir is removed by the startup sweep", workdir_gone)
    check("the journal now records workdir_cleaned: true",
          record_after.get("workdir_cleaned") is True)
    check("a terminal run's STATE is untouched by the sweep (no provider work was due)",
          record_after.get("state") == "finished")


def test_recover_orphans_sweeps_a_workdir_alongside_active_provider_recovery() -> None:
    """An ACTIVE orphan (running/cancelling when the process died) gets BOTH
    sweeps: the existing provider-side cancel/cleanup, AND the workdir
    release — independently, per recover_orphans()'s own docstring."""
    from codecalc import executor

    provider = BlockingProvider()
    registry = providers.ProviderRegistry(default_provider_id=provider.provider_id)
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-workdir-active-") as root:
        state_dir = Path(root)
        workdir = tempfile.mkdtemp(prefix="codecalc-deps-active-orphan-")
        identity = executor._dir_identity(workdir)
        journal = state_dir / "orphan-active.json"
        journal.write_text(json.dumps({
            "run_id": "orphan-active", "provider_id": provider.provider_id,
            "state": "running", "workdir": workdir,
            "workdir_identity": list(identity), "workdir_cleaned": False,
        }), encoding="utf-8")
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=state_dir)
        recovered = supervisor.recover_orphans()
        workdir_gone = not Path(workdir).exists()
        record_after = json.loads(journal.read_text(encoding="utf-8"))

    check("the active orphan's provider side still recovers",
          recovered == ["orphan-active"] and provider.cancelled == ["orphan-active"])
    check("the SAME orphan's workdir is ALSO released", workdir_gone)
    check("the journal records both outcomes",
          record_after.get("state") == "recovered"
          and record_after.get("workdir_cleaned") is True)


def test_recover_orphans_refuses_a_workdir_with_mismatched_identity() -> None:
    """A journalled identity that no longer matches the directory on disk
    (the path was recreated, or never matched at all — a corrupted/hand-
    edited journal) is REFUSED, not deleted — `executor._rmtree_checked`'s
    own fail-closed rule, re-derived here rather than bypassed. It IS still
    marked `workdir_cleaned` afterward: a permanently untrustworthy identity
    is a decision, not a transient failure to retry every future restart."""
    provider = BlockingProvider()
    registry = providers.ProviderRegistry(default_provider_id=provider.provider_id)
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-runs-workdir-mismatch-") as root:
        state_dir = Path(root)
        workdir = tempfile.mkdtemp(prefix="codecalc-deps-mismatch-")
        journal = state_dir / "orphan-mismatch.json"
        journal.write_text(json.dumps({
            "run_id": "orphan-mismatch", "provider_id": provider.provider_id,
            "state": "finished", "workdir": workdir,
            "workdir_identity": [999999, 999999], "workdir_cleaned": False,
        }), encoding="utf-8")
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=state_dir)
        try:
            supervisor.recover_orphans()
            workdir_survived = Path(workdir).exists()
            record_after = json.loads(journal.read_text(encoding="utf-8"))
        finally:
            import shutil as _shutil
            _shutil.rmtree(workdir, ignore_errors=True)

    check("a mismatched identity is REFUSED — the directory survives", workdir_survived)
    check("the journal still records workdir_cleaned: true — no retry every restart",
          record_after.get("workdir_cleaned") is True)


if __name__ == "__main__":
    test_lifecycle_is_provider_bound_and_cleanup_is_idempotent()
    test_journal_is_bounded_and_orphans_are_reconciled()
    test_cleanup_is_capability_gated_and_does_not_crash_on_a_provider_without_it()
    test_cancel_on_a_provider_that_cannot_cancel_leaves_the_run_collectible()
    test_admission_cap_rejects_a_submission_past_the_limit()
    test_a_set_but_empty_admission_cap_does_not_crash_the_server_at_import()
    test_prune_never_deletes_a_still_active_runs_journal()
    test_prune_runs_on_terminal_collection_not_only_cleanup()
    test_a_provider_that_raises_becomes_a_terminal_failed_result_not_a_strand()
    test_a_completed_but_uninspected_run_frees_its_admission_slot()
    test_recover_orphans_skips_an_unregistered_provider_without_crashing()
    test_start_journals_the_workdir_before_the_runner_can_possibly_run()
    test_start_pops_the_run_and_releases_its_workdir_when_pool_submit_raises()
    test_reap_and_inspect_skip_a_record_with_no_future_yet()
    test_start_runner_replaces_provider_dispatch_and_owns_a_workdir()
    test_coded_run_failure_becomes_the_verbatim_terminal_result()
    test_recover_orphans_sweeps_an_uncleaned_workdir_for_a_terminal_run()
    test_recover_orphans_sweeps_a_workdir_alongside_active_provider_recovery()
    test_recover_orphans_refuses_a_workdir_with_mismatched_identity()
    sys.exit(1 if FAILS else 0)
