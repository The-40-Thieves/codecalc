"""Protocol-neutral execution service and MCP adapter parity (THE-790)."""

from __future__ import annotations

import sys

from codecalc import contract, execution_service, providers, server

FAILS: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name}")
    if not condition:
        FAILS.append(name)


def _service() -> execution_service.ExecutionService:
    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(providers.LocalExecutionProvider())
    return execution_service.ExecutionService(registry)


def test_service_routes_a_canonical_spec_and_records_provider_identity() -> None:
    result = _service().execute(
        providers.ComputationSpec(language="python3", code='print("service")')
    )

    check("service executes successfully", result["ok"] is True)
    check("service preserves provider stdout", result["stdout"].strip() == "service")
    check("service preserves the result contract",
          result["contract_version"] == contract.CONTRACT_VERSION)
    check("service receipt identifies the provider",
          result["provider"]["provider_id"] == "local")
    check("service receipt identifies the provider interface",
          result["provider"]["interface_version"] == providers.PROVIDER_INTERFACE_VERSION)


def test_service_rejects_an_unknown_provider_without_falling_back() -> None:
    result = _service().execute(
        providers.ComputationSpec(language="python3", code='print("must not run")'),
        provider_id="missing",
    )

    check("unknown provider is rejected", result["ok"] is False)
    check("unknown provider is a request validation error", result["code"] == "validation")
    check("unknown provider keeps its provider-specific code",
          result["provider_error"] == "unknown_provider")
    check("unknown-provider errors carry the result contract",
          result["contract_version"] == contract.CONTRACT_VERSION)


def test_mcp_adapter_accepts_explicit_local_provider_selection() -> None:
    result = server.execute_code("python3", 'print("adapter")', provider="local")

    check("MCP adapter accepts explicit local selection", result["ok"] is True)
    check("MCP adapter preserves execution output", result["stdout"].strip() == "adapter")
    check("MCP adapter returns provider identity",
          result["provider"]["provider_id"] == "local")


def test_compact_results_keep_provider_identity() -> None:
    result = server.execute_code(
        "python3", 'print("compact")', provider="local", compact=True
    )

    check("compact execution succeeds", result["ok"] is True)
    check("compact result keeps provider identity",
          result.get("provider", {}).get("provider_id") == "local")


def test_mcp_adapter_publishes_provider_descriptors() -> None:
    descriptors = server.list_execution_providers()

    check("provider discovery returns a list", isinstance(descriptors, list))
    check("provider discovery exposes the local provider",
          [item["provider_id"] for item in descriptors] == ["local"])
    check("provider discovery exposes machine-readable capabilities",
          descriptors[0]["capabilities"]["execute"] is True)


def test_service_applies_policy_routing_when_selection_is_not_explicit() -> None:
    class RemoteProvider(providers.LocalExecutionProvider):
        provider_id = "remote"

        def describe(self) -> dict:
            descriptor = super().describe()
            descriptor["provider_id"] = self.provider_id
            return descriptor

    class RemotePolicy:
        def select_provider(self, spec: providers.ComputationSpec,
                            descriptors: list[dict]) -> str | None:
            return "remote"

    registry = providers.ProviderRegistry(
        default_provider_id="local", policy=RemotePolicy()
    )
    registry.register(providers.LocalExecutionProvider())
    registry.register(RemoteProvider())
    service = execution_service.ExecutionService(registry)

    result = service.execute(
        providers.ComputationSpec(language="python3", code='print("policy")')
    )

    check("service uses the provider selected by policy",
          result["provider"]["provider_id"] == "remote")


def test_service_verifies_one_workload_independently_across_two_providers() -> None:
    class RecordingProvider(providers.LocalExecutionProvider):
        def __init__(self, provider_id: str, stdout: str) -> None:
            self.provider_id = provider_id
            self.stdout = stdout
            self.calls: list[providers.ComputationSpec] = []

        def describe(self) -> dict:
            descriptor = super().describe()
            descriptor["provider_id"] = self.provider_id
            return descriptor

        def execute(self, spec: providers.ComputationSpec) -> dict:
            self.calls.append(spec)
            return contract.stamp({
                "ok": True,
                "verdict": "OK",
                "stdout": self.stdout,
                "stderr": "",
                "exit_code": 0,
            })

    left = RecordingProvider("left", "same\n")
    right = RecordingProvider("right", "same\n")
    registry = providers.ProviderRegistry(default_provider_id="left")
    registry.register(left)
    registry.register(right)
    service = execution_service.ExecutionService(registry)
    spec = providers.ComputationSpec(language="python3", code='print("same")')

    result = service.verify_across_providers(spec, "left", "right")

    check("cross-provider verification reports agreement",
          result["agreement"] is True)
    check("cross-provider verification runs each provider once",
          left.calls == [spec] and right.calls == [spec])
    check("cross-provider verification keeps both receipts",
          [item["provider"]["provider_id"] for item in result["results"]]
          == ["left", "right"])
    check("cross-provider verification carries the result contract",
          result["contract_version"] == contract.CONTRACT_VERSION)


if __name__ == "__main__":
    test_service_routes_a_canonical_spec_and_records_provider_identity()
    test_service_rejects_an_unknown_provider_without_falling_back()
    test_mcp_adapter_accepts_explicit_local_provider_selection()
    test_compact_results_keep_provider_identity()
    test_mcp_adapter_publishes_provider_descriptors()
    test_service_applies_policy_routing_when_selection_is_not_explicit()
    test_service_verifies_one_workload_independently_across_two_providers()
    sys.exit(1 if FAILS else 0)
