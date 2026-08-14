"""Execution-provider contract and first local implementation (THE-790)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from _provider_conformance import run_execution_conformance

from codecalc import contract, providers

REPO_ROOT = Path(__file__).resolve().parents[1]

FAILS: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name}")
    if not condition:
        FAILS.append(name)


def test_descriptor_is_versioned_and_machine_readable() -> None:
    provider = providers.LocalExecutionProvider()

    descriptor = provider.describe()
    encoded = json.loads(json.dumps(descriptor))

    check("descriptor identifies the provider interface version",
          encoded["interface_version"] == providers.PROVIDER_INTERFACE_VERSION)
    check("descriptor identifies the local provider", encoded["provider_id"] == "local")
    check("descriptor identifies the provider implementation version",
          bool(encoded["provider_version"]))
    check("descriptor advertises execution", encoded["capabilities"]["execute"] is True)
    check("descriptor does not advertise cancellation",
          encoded["capabilities"]["cancel"] is False)


def test_local_provider_preserves_the_execution_result_contract() -> None:
    provider = providers.LocalExecutionProvider()
    spec = providers.ComputationSpec(language="python3", code='print("provider")')

    result = provider.execute(spec)

    check("local provider executes successfully", result["ok"] is True)
    check("local provider preserves stdout", result["stdout"].strip() == "provider")
    check("local provider preserves the result contract version",
          result["contract_version"] == contract.CONTRACT_VERSION)
    check("local provider preserves backend identity",
          result["backend"] in {"rust", "python"})


def test_unsupported_provider_capability_fails_explicitly() -> None:
    provider = providers.LocalExecutionProvider()

    try:
        provider.cancel("run-id")
    except providers.UnsupportedCapability as exc:
        check("unsupported error identifies its provider", exc.provider_id == "local")
        check("unsupported error identifies its capability", exc.capability == "cancel")
        check("unsupported error carries a stable code",
              exc.code == "unsupported_capability")
    else:
        check("unsupported cancellation cannot silently succeed", False)


def test_registry_supports_default_and_explicit_provider_selection() -> None:
    local = providers.LocalExecutionProvider()
    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(local)

    check("registry selects its default provider", registry.select() is local)
    check("registry supports explicit provider selection", registry.select("local") is local)
    check("registry publishes every provider descriptor",
          registry.descriptors() == [local.describe()])


def test_unknown_provider_fails_with_a_stable_machine_code() -> None:
    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(providers.LocalExecutionProvider())

    try:
        registry.select("missing")
    except providers.UnknownProvider as exc:
        check("unknown error identifies the request", exc.provider_id == "missing")
        check("unknown error carries a stable code", exc.code == "unknown_provider")
        check("unknown error lists available providers", exc.available == ("local",))
    else:
        check("unknown provider cannot silently select a fallback", False)


def test_provider_interface_is_published_with_its_versioning_policy() -> None:
    path = REPO_ROOT / "docs" / "contract" / "provider-v1.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""

    check("provider interface documentation is published", bool(text))
    check("provider documentation names the current interface version",
          providers.PROVIDER_INTERFACE_VERSION in text)
    check("provider documentation defines capability-driven degradation",
          "unsupported_capability" in text)
    check("provider documentation defines compatibility rules",
          "Versioning policy" in text)


def test_registry_rejects_an_incompatible_provider_interface() -> None:
    class FutureProvider(providers.LocalExecutionProvider):
        provider_id = "future"

        def describe(self) -> dict:
            descriptor = super().describe()
            descriptor["provider_id"] = self.provider_id
            descriptor["interface_version"] = "2.0.0"
            return descriptor

    registry = providers.ProviderRegistry(default_provider_id="local")
    try:
        registry.register(FutureProvider())
    except providers.ProviderInterfaceMismatch as exc:
        check("interface mismatch identifies its provider", exc.provider_id == "future")
        check("interface mismatch identifies the offered version", exc.offered == "2.0.0")
        check("interface mismatch carries a stable code",
              exc.code == "provider_interface_mismatch")
    else:
        check("incompatible provider cannot be registered", False)


def test_policy_routing_receives_the_spec_and_explicit_selection_wins() -> None:
    class RemoteProvider(providers.LocalExecutionProvider):
        provider_id = "remote"

        def describe(self) -> dict:
            descriptor = super().describe()
            descriptor["provider_id"] = self.provider_id
            descriptor["host_class"] = "test-region"
            return descriptor

    class NoNetworkPolicy:
        def __init__(self) -> None:
            self.seen: providers.ComputationSpec | None = None

        def select_provider(self, spec: providers.ComputationSpec,
                            descriptors: list[dict]) -> str | None:
            self.seen = spec
            return "remote" if spec.no_net else None

    policy = NoNetworkPolicy()
    local = providers.LocalExecutionProvider()
    remote = RemoteProvider()
    registry = providers.ProviderRegistry(default_provider_id="local", policy=policy)
    registry.register(local)
    registry.register(remote)
    spec = providers.ComputationSpec(language="python3", code="pass", no_net=True)

    check("policy can route from the canonical request",
          registry.select(spec=spec) is remote)
    check("policy receives the canonical request", policy.seen is spec)
    check("explicit selection overrides routing policy",
          registry.select("local", spec=spec) is local)


def test_local_provider_passes_the_shared_execution_conformance_suite() -> None:
    run_execution_conformance(providers.LocalExecutionProvider(), check)


def test_local_provider_reports_health_and_runtime_discovery() -> None:
    provider = providers.LocalExecutionProvider()

    health = provider.health()
    runtimes = provider.list_runtimes()

    check("health identifies the provider",
          health["provider_id"] == provider.provider_id)
    check("health reports the active local backend",
          health["backend"] in {"rust", "python"})
    check("healthy local provider is ready", health["ready"] is True)
    check("runtime discovery returns the canonical catalog",
          runtimes == providers.executor.catalog())
    check("health capability is advertised",
          provider.describe()["capabilities"]["health"] is True)
    check("runtime discovery capability is advertised",
          provider.describe()["capabilities"]["runtime_discovery"] is True)


if __name__ == "__main__":
    test_descriptor_is_versioned_and_machine_readable()
    test_local_provider_preserves_the_execution_result_contract()
    test_unsupported_provider_capability_fails_explicitly()
    test_registry_supports_default_and_explicit_provider_selection()
    test_unknown_provider_fails_with_a_stable_machine_code()
    test_provider_interface_is_published_with_its_versioning_policy()
    test_registry_rejects_an_incompatible_provider_interface()
    test_policy_routing_receives_the_spec_and_explicit_selection_wins()
    test_local_provider_passes_the_shared_execution_conformance_suite()
    test_local_provider_reports_health_and_runtime_discovery()
    sys.exit(1 if FAILS else 0)
