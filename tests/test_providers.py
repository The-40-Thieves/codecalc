"""Execution-provider contract and first local implementation (THE-790)."""

from __future__ import annotations

import json
import sys

from codecalc import contract, providers

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


if __name__ == "__main__":
    test_descriptor_is_versioned_and_machine_readable()
    test_local_provider_preserves_the_execution_result_contract()
    test_unsupported_provider_capability_fails_explicitly()
    test_registry_supports_default_and_explicit_provider_selection()
    test_unknown_provider_fails_with_a_stable_machine_code()
    sys.exit(1 if FAILS else 0)
