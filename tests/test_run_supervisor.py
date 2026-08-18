"""Provider-neutral run lifecycle contract (THE-831)."""

from __future__ import annotations

import json
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
    """THE-778 residual: run_supervisor.cleanup() unconditionally called
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
    """THE-778 fix round, review Critical #1 — the reviewer's own repro:
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
    """THE-778 fix round, review Important #3a."""
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
    """THE-778 fix round, review Important #3b (first half — state-aware
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
    """THE-778 fix round, review Important #3b (second half — prune on
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


if __name__ == "__main__":
    test_lifecycle_is_provider_bound_and_cleanup_is_idempotent()
    test_journal_is_bounded_and_orphans_are_reconciled()
    test_cleanup_is_capability_gated_and_does_not_crash_on_a_provider_without_it()
    test_cancel_on_a_provider_that_cannot_cancel_leaves_the_run_collectible()
    test_admission_cap_rejects_a_submission_past_the_limit()
    test_prune_never_deletes_a_still_active_runs_journal()
    test_prune_runs_on_terminal_collection_not_only_cleanup()
    sys.exit(1 if FAILS else 0)
