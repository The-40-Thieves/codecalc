"""Protocol-neutral execution application service (THE-790)."""

from __future__ import annotations

from . import contract, errors
from .providers import ComputationSpec, ProviderRegistry, UnknownProvider


class ExecutionService:
    """Select an execution provider and preserve CodeCalc's result contract."""

    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry

    def execute(self, spec: ComputationSpec, *, provider_id: str | None = None) -> dict:
        try:
            provider = self.registry.select(provider_id, spec=spec)
        except UnknownProvider as exc:
            return contract.stamp(errors.error_result(
                errors.VALIDATION,
                str(exc),
                provider_error=exc.code,
                requested_provider=exc.provider_id,
                available_providers=list(exc.available),
            ))

        result = dict(provider.execute(spec))
        descriptor = provider.describe()
        result["provider"] = {
            "interface_version": descriptor["interface_version"],
            "provider_id": descriptor["provider_id"],
            "provider_version": descriptor["provider_version"],
            "host_class": descriptor["host_class"],
        }
        return contract.stamp(result)
