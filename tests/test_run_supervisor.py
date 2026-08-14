"""Provider-neutral run lifecycle contract (THE-831)."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
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


if __name__ == "__main__":
    test_lifecycle_is_provider_bound_and_cleanup_is_idempotent()
    test_journal_is_bounded_and_orphans_are_reconciled()
    sys.exit(1 if FAILS else 0)
