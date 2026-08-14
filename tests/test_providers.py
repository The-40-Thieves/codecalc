"""Execution-provider contract and first local implementation (THE-790)."""

from __future__ import annotations

import json

from codecalc import contract, providers


def test_descriptor_is_versioned_and_machine_readable() -> None:
    provider = providers.LocalExecutionProvider()

    descriptor = provider.describe()
    encoded = json.loads(json.dumps(descriptor))

    assert encoded["interface_version"] == providers.PROVIDER_INTERFACE_VERSION
    assert encoded["provider_id"] == "local"
    assert encoded["provider_version"]
    assert encoded["capabilities"]["execute"] is True
    assert encoded["capabilities"]["cancel"] is False


def test_local_provider_preserves_the_execution_result_contract() -> None:
    provider = providers.LocalExecutionProvider()
    spec = providers.ComputationSpec(language="python3", code='print("provider")')

    result = provider.execute(spec)

    assert result["ok"] is True
    assert result["stdout"].strip() == "provider"
    assert result["contract_version"] == contract.CONTRACT_VERSION
    assert result["backend"] in {"rust", "python"}


def test_unsupported_provider_capability_fails_explicitly() -> None:
    provider = providers.LocalExecutionProvider()

    try:
        provider.cancel("run-id")
    except providers.UnsupportedCapability as exc:
        assert exc.provider_id == "local"
        assert exc.capability == "cancel"
        assert exc.code == "unsupported_capability"
    else:
        raise AssertionError("cancel silently succeeded despite being unsupported")


if __name__ == "__main__":
    test_descriptor_is_versioned_and_machine_readable()
    test_local_provider_preserves_the_execution_result_contract()
    test_unsupported_provider_capability_fails_explicitly()
