"""Versioned execution-provider boundary (THE-790).

Providers receive a protocol-neutral :class:`ComputationSpec` and return the
existing execution result contract unchanged.  The interface is intentionally
capability-based: a provider must advertise optional operations, and calling
one it does not implement fails explicitly.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import __version__, contract, errors, executor

PROVIDER_INTERFACE_VERSION = "1.0.0"
DEFAULT_PROVIDER_ENV = "CODECALC_EXECUTION_PROVIDER"
PISTON_URL_ENV = "CODECALC_PISTON_URL"
PISTON_AUTHORIZATION_ENV = "CODECALC_PISTON_AUTHORIZATION"
ProviderTransport = Callable[[str, str, dict[str, str], dict | None, int], object]


def _redact_secrets(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_redact_secrets(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact_secrets(item, secrets) for key, item in value.items()}
    return value


def _truncate_utf8(value: str, max_bytes: int) -> str:
    return value.encode()[:max_bytes].decode(errors="ignore")


class PistonHTTPTransport:
    """Small JSON transport for Piston, injectable at the network boundary."""

    def __init__(self, base_url: str, *, opener: Callable = urlopen) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Piston base_url must be an absolute HTTP(S) URL")
        self.base_url = base_url.rstrip("/")
        self._opener = opener

    def __call__(self, method: str, path: str, headers: dict[str, str],
                 payload: dict | None, timeout: int) -> object:
        body = json.dumps(payload).encode() if payload is not None else None
        request_headers = dict(headers)
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(  # noqa: S310 -- constructor rejects non-HTTP(S) URLs
            f"{self.base_url}/{path.lstrip('/')}",
            data=body,
            headers=request_headers,
            method=method,
        )
        with self._opener(request, timeout=timeout) as response:
            return json.loads(response.read().decode())


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


class UnknownProvider(LookupError):
    """Selection named a provider that is not registered."""

    code = "unknown_provider"

    def __init__(self, provider_id: str, available: tuple[str, ...]) -> None:
        self.provider_id = provider_id
        self.available = available
        choices = ", ".join(available) or "none"
        super().__init__(
            f"unknown execution provider {provider_id!r}; available: {choices}"
        )


class ProviderInterfaceMismatch(ValueError):
    """A provider implements an incompatible major interface version."""

    code = "provider_interface_mismatch"

    def __init__(self, provider_id: str, offered: str, required: str) -> None:
        self.provider_id = provider_id
        self.offered = offered
        self.required = required
        super().__init__(
            f"provider {provider_id!r} implements interface {offered!r}; "
            f"CodeCalc requires a compatible {required!r} interface"
        )


@runtime_checkable
class ExecutionProvider(Protocol):
    """The provider surface implemented independently of MCP or HTTP."""

    provider_id: str

    def describe(self) -> dict: ...

    def health(self) -> dict: ...

    def list_runtimes(self) -> list[dict]: ...

    def execute(self, spec: ComputationSpec) -> dict: ...

    def cancel(self, run_id: str) -> None: ...

    def cleanup(self, run_id: str) -> None: ...

    def collect_artifacts(self, run_id: str) -> list[dict]: ...

    def list_files(self, run_id: str) -> list[dict]: ...


@runtime_checkable
class ProviderSelectionPolicy(Protocol):
    """Choose a provider from a canonical request and current descriptors."""

    def select_provider(self, spec: ComputationSpec,
                        descriptors: list[dict]) -> str | None: ...


class ProviderRegistry:
    """Provider discovery and deterministic explicit/default selection."""

    def __init__(self, *, default_provider_id: str,
                 policy: ProviderSelectionPolicy | None = None) -> None:
        self.default_provider_id = default_provider_id
        self.policy = policy
        self._providers: dict[str, ExecutionProvider] = {}

    def register(self, provider: ExecutionProvider) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"provider {provider.provider_id!r} is already registered")
        offered = str(provider.describe().get("interface_version", ""))
        offered_major = offered.split(".", 1)[0]
        required_major = PROVIDER_INTERFACE_VERSION.split(".", 1)[0]
        if not offered_major.isdigit() or offered_major != required_major:
            raise ProviderInterfaceMismatch(
                provider.provider_id, offered, PROVIDER_INTERFACE_VERSION
            )
        self._providers[provider.provider_id] = provider

    def select(self, provider_id: str | None = None, *,
               spec: ComputationSpec | None = None) -> ExecutionProvider:
        selected = provider_id
        if selected is None and self.policy is not None and spec is not None:
            selected = self.policy.select_provider(spec, self.descriptors())
        selected = selected or self.default_provider_id
        try:
            return self._providers[selected]
        except KeyError:
            raise UnknownProvider(selected, tuple(sorted(self._providers))) from None

    def descriptors(self) -> list[dict]:
        return [self._providers[key].describe() for key in sorted(self._providers)]


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
                "health": True,
                "runtime_discovery": True,
                "workdir": True,
                "network_control": executor.backend() == "rust",
            },
        ).to_dict()

    def health(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "provider_version": __version__,
            "ready": True,
            "backend": executor.backend(),
        }

    def list_runtimes(self) -> list[dict]:
        return executor.catalog()

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

    def cleanup(self, run_id: str) -> None:
        del run_id
        raise UnsupportedCapability(self.provider_id, "cleanup")

    def collect_artifacts(self, run_id: str) -> list[dict]:
        del run_id
        raise UnsupportedCapability(self.provider_id, "artifacts")

    def list_files(self, run_id: str) -> list[dict]:
        del run_id
        raise UnsupportedCapability(self.provider_id, "files")


class PistonExecutionProvider:
    """Adapter for a self-hosted Piston v2 HTTP API."""

    provider_id = "piston"

    def __init__(self, *, base_url: str, transport: ProviderTransport | None = None,
                 authorization: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport or PistonHTTPTransport(self.base_url)
        self._headers = ({"Authorization": authorization} if authorization else {})
        raw_credential = authorization.split(maxsplit=1)[-1] if authorization else ""
        self._secrets = tuple(sorted(
            {item for item in (authorization, raw_credential) if item},
            key=len,
            reverse=True,
        ))

    def describe(self) -> dict:
        return ProviderDescriptor(
            interface_version=PROVIDER_INTERFACE_VERSION,
            provider_id=self.provider_id,
            provider_version="api-v2",
            host_class="remote",
            capabilities={
                "execute": True,
                "inspect": False,
                "stream": False,
                "cancel": False,
                "cleanup": False,
                "files": False,
                "artifacts": False,
                "sessions": False,
                "health": True,
                "runtime_discovery": True,
                "workdir": False,
                "network_control": False,
            },
        ).to_dict()

    def health(self) -> dict:
        health = {
            "provider_id": self.provider_id,
            "provider_version": "api-v2",
            "ready": True,
        }
        try:
            self.list_runtimes()
        except Exception as exc:
            health["ready"] = False
            health["error"] = str(exc)
        return _redact_secrets(health, self._secrets)

    def list_runtimes(self) -> list[dict]:
        result = self._transport(
            "GET", "/api/v2/runtimes", dict(self._headers), None, 10
        )
        return list(result) if isinstance(result, list) else []

    def execute(self, spec: ComputationSpec) -> dict:
        if spec.no_net:
            raise UnsupportedCapability(self.provider_id, "network_control")
        if spec.workdir is not None:
            raise UnsupportedCapability(self.provider_id, "workdir")
        timeout_ms = spec.timeout * 1000
        payload = {
            "language": spec.language,
            "version": "*",
            "files": [{"content": spec.code}],
            "stdin": spec.stdin,
            "compile_timeout": timeout_ms,
            "run_timeout": timeout_ms,
            "compile_cpu_time": spec.max_cpu * 1000,
            "run_cpu_time": spec.max_cpu * 1000,
            "compile_memory_limit": spec.max_memory_mb * 1024 * 1024,
            "run_memory_limit": spec.max_memory_mb * 1024 * 1024,
        }
        try:
            response = self._transport(
                "POST", "/api/v2/execute", dict(self._headers), payload,
                spec.timeout + 5,
            )
        except Exception as exc:
            failure = errors.error_result(
                errors.INTERNAL,
                f"Piston transport failed: {exc}",
                provider_error="provider_transport_failure",
            )
            return contract.stamp(_redact_secrets(failure, self._secrets))
        data = response if isinstance(response, dict) else {}
        run = data.get("run") if isinstance(data.get("run"), dict) else {}
        stdout = str(run.get("stdout") or "")
        stderr = str(run.get("stderr") or "")
        stdout_bytes = len(stdout.encode())
        stderr_bytes = len(stderr.encode())
        client_output_overflow = False
        unenforced: list[str] = []
        if spec.max_output_kb > 0:
            output_cap = spec.max_output_kb * 1024
            client_output_overflow = stdout_bytes + stderr_bytes > output_cap
            stdout = _truncate_utf8(stdout, output_cap)
            remaining = max(0, output_cap - len(stdout.encode()))
            stderr = _truncate_utf8(stderr, remaining)
            unenforced.append("max_output_kb_enforced_after_provider_response")
        exit_code = run.get("code")
        duration_ms = int(run.get("wall_time") or 0)
        cpu_ms = int(run.get("cpu_time") or 0)
        memory_bytes = run.get("memory")
        peak_memory_kb = (int(memory_bytes) // 1024
                          if isinstance(memory_bytes, int) else None)
        status = run.get("status")
        timed_out = status == "TO"
        output_truncated = status in {"OL", "EL"} or client_output_overflow
        if timed_out:
            verdict = "TLE"
        elif output_truncated:
            verdict = "OLE"
        else:
            verdict = "OK" if exit_code == 0 else "RTE"
        ok = verdict == "OK"
        result = {
            "ok": ok,
            "language": spec.language,
            "phase": "run",
            "backend": "python",
            "platform": "remote",
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "verdict": verdict,
            "output_truncated": output_truncated,
            "output_error": None,
            "duration_ms": duration_ms,
            "compile_ms": 0,
            "total_ms": duration_ms,
            "cpu_ms": cpu_ms,
            "peak_memory_kb": peak_memory_kb,
            "unenforced": unenforced,
            "workdir": "piston:ephemeral",
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
        }
        return contract.stamp(_redact_secrets(result, self._secrets))

    def cancel(self, run_id: str) -> None:
        del run_id
        raise UnsupportedCapability(self.provider_id, "cancel")

    def cleanup(self, run_id: str) -> None:
        del run_id
        raise UnsupportedCapability(self.provider_id, "cleanup")

    def collect_artifacts(self, run_id: str) -> list[dict]:
        del run_id
        raise UnsupportedCapability(self.provider_id, "artifacts")

    def list_files(self, run_id: str) -> list[dict]:
        del run_id
        raise UnsupportedCapability(self.provider_id, "files")


def configured_registry(
    environment: Mapping[str, str] | None = None,
) -> ProviderRegistry:
    """Build the provider registry from process configuration."""
    env = os.environ if environment is None else environment
    registry = ProviderRegistry(
        default_provider_id=env.get(DEFAULT_PROVIDER_ENV, "local")
    )
    registry.register(LocalExecutionProvider())
    piston_url = env.get(PISTON_URL_ENV, "").strip()
    if piston_url:
        registry.register(PistonExecutionProvider(
            base_url=piston_url,
            authorization=env.get(PISTON_AUTHORIZATION_ENV) or None,
        ))
    return registry
