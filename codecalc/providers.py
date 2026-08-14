"""Versioned execution-provider boundary (THE-790).

Providers receive a protocol-neutral :class:`ComputationSpec` and return the
existing execution result contract unchanged.  The interface is intentionally
capability-based: a provider must advertise optional operations, and calling
one it does not implement fails explicitly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable

from . import __version__, executor

PROVIDER_INTERFACE_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class ComputationSpec:
    """Canonical execution request shared by transports and providers."""

    language: str
    code: str
    stdin: str = ""
    timeout: int = 10
    workdir: str | None = None
    max_memory_mb: int = 0
    max_output_kb: int = 0
    max_cpu: int = 0
    no_net: bool = False


class UnsupportedCapability(RuntimeError):
    """A provider was asked to perform an operation it did not advertise."""

    code = "unsupported_capability"

    def __init__(self, provider_id: str, capability: str) -> None:
        self.provider_id = provider_id
        self.capability = capability
        super().__init__(
            f"provider {provider_id!r} does not support capability {capability!r}"
        )


@runtime_checkable
class ExecutionProvider(Protocol):
    """The provider surface implemented independently of MCP or HTTP."""

    def describe(self) -> dict: ...

    def execute(self, spec: ComputationSpec) -> dict: ...

    def cancel(self, run_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """JSON-serializable provider identity and capability declaration."""

    interface_version: str
    provider_id: str
    provider_version: str
    host_class: str
    capabilities: dict[str, bool]

    def to_dict(self) -> dict:
        return asdict(self)


class LocalExecutionProvider:
    """Adapter for CodeCalc's existing native-or-fallback local executor."""

    provider_id = "local"

    def describe(self) -> dict:
        return ProviderDescriptor(
            interface_version=PROVIDER_INTERFACE_VERSION,
            provider_id=self.provider_id,
            provider_version=__version__,
            host_class="local",
            capabilities={
                "execute": True,
                "inspect": False,
                "stream": False,
                "cancel": False,
                "cleanup": False,
                "files": False,
                "artifacts": False,
                "sessions": False,
                "network_control": executor.backend() == "rust",
            },
        ).to_dict()

    def execute(self, spec: ComputationSpec) -> dict:
        return executor.execute(
            spec.language,
            spec.code,
            stdin=spec.stdin,
            timeout=spec.timeout,
            workdir=spec.workdir,
            max_memory_mb=spec.max_memory_mb,
            max_output_kb=spec.max_output_kb,
            max_cpu=spec.max_cpu,
            no_net=spec.no_net,
        )

    def cancel(self, run_id: str) -> None:
        del run_id
        raise UnsupportedCapability(self.provider_id, "cancel")
