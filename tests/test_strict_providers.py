"""Shared fail-closed contract for native strict providers (THE-828..830)."""

from __future__ import annotations

import sys

from codecalc import providers

FAILS: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name}")
    if not condition:
        FAILS.append(name)


def test_host_strict_provider_is_discoverable_and_fails_closed() -> None:
    registry = providers.configured_registry({})
    provider_id = f"{providers.strict_host_platform()}-strict"
    strict = registry.select(provider_id)
    descriptor = strict.describe()
    result = strict.execute(providers.ComputationSpec(
        language="python3", code='raise RuntimeError("must never execute")',
        no_net=True,
    ))

    check("host strict provider is always discoverable",
          descriptor["provider_id"] == provider_id)
    check("strict capability is explicit", descriptor["strict"] is True)
    check("unavailable strict provider is not ready", strict.health()["ready"] is False)
    check("strict selection fails without executing locally", result["ok"] is False)
    check("strict failure has a stable machine code",
          result["provider_error"] == "strict_provider_unavailable")
    check("strict failure never carries payload output",
          "must never execute" not in result.get("stderr", ""))


def test_local_provider_is_explicitly_non_strict() -> None:
    descriptor = providers.LocalExecutionProvider().describe()
    check("local provider labels its trust boundary", descriptor["strict"] is False)


if __name__ == "__main__":
    test_host_strict_provider_is_discoverable_and_fails_closed()
    test_local_provider_is_explicitly_non_strict()
    sys.exit(1 if FAILS else 0)
