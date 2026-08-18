"""Versioned execution-provider boundary (THE-790).

Providers receive a protocol-neutral :class:`ComputationSpec` and return the
existing execution result contract unchanged.  The interface is intentionally
capability-based: a provider must advertise optional operations, and calling
one it does not implement fails explicitly.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from . import __version__, contract, errors, executor, spec_identity
from .provider_adapters.piston import PistonHTTPTransport
from .strict_runtime import ENFORCEMENT_CONTROLS, ISOLATION_PROFILE

PROVIDER_INTERFACE_VERSION = "1.0.0"
DEFAULT_PROVIDER_ENV = "CODECALC_EXECUTION_PROVIDER"
PISTON_URL_ENV = "CODECALC_PISTON_URL"
PISTON_AUTHORIZATION_ENV = "CODECALC_PISTON_AUTHORIZATION"
STRICT_URL_ENV = "CODECALC_STRICT_URL"
STRICT_AUTHORIZATION_ENV = "CODECALC_STRICT_AUTHORIZATION"
ProviderTransport = Callable[[str, str, dict[str, str], dict | None, int], object]
#: Re-exported beside the dataclass it versions, the same way
#: STRICT_ISOLATION_PROFILE is: callers already import `providers`.
COMPUTATION_SPEC_VERSION = spec_identity.COMPUTATION_SPEC_VERSION
STRICT_ISOLATION_PROFILE = ISOLATION_PROFILE
STRICT_CONTROLS = ENFORCEMENT_CONTROLS


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


@dataclass(frozen=True, slots=True)
class ComputationSpec:
    """Canonical execution request shared by transports and providers.

    Its IDENTITY lives in `codecalc/spec_identity.py`: `canonical_bytes()` gives
    a deterministic encoding and `spec_hash()` the sha256 over it, so a cache, a
    receipt or a provenance record can name a request instead of carrying it.

    NOTE FOR ANYONE ADDING A FIELD. Canonicalization emits every field, so a new
    one changes every spec hash — that is a MINOR bump of
    `COMPUTATION_SPEC_VERSION` and the golden vectors in
    `docs/contract/computation-spec-v1.vectors.json` must be recomputed by hand.
    Adding a credential-bearing field is refused outright unless it is declared
    in `HASH_BY_NAME_FIELDS`; see spec_identity's SECRETS section.
    """

    language: str
    code: str
    stdin: str = ""
    timeout: int = 10
    workdir: str | None = None
    max_memory_mb: int = 0
    max_output_kb: int = 0
    max_cpu: int = 0
    no_net: bool = False

    #: Fields whose VALUES must not enter the canonical bytes; only their key
    #: names do. Empty because no field of this spec carries a credential —
    #: `spec_identity.SECRET_BEARING_FIELD_NAMES` keeps it that way by refusing
    #: to hash one that appears without being declared here.
    #:
    #: Deliberately UNANNOTATED: an annotation would make it a tenth dataclass
    #: field, which would put the policy itself into the hashed content.
    HASH_BY_NAME_FIELDS = ()

    def canonical_bytes(self) -> bytes:
        """The deterministic encoding this request hashes to."""
        return spec_identity.canonical_bytes(self)

    def spec_hash(self) -> str:
        """`sha256:<hex>` over `canonical_bytes()`."""
        return spec_identity.spec_hash(self)


#: One line per published field. Kept next to the dataclass, and NOT optional:
#: `spec_identity.build_spec_schema` refuses to generate a document with an
#: undocumented field, so a field added without a description is a failing gate
#: rather than a published schema with a blank in it.
COMPUTATION_SPEC_FIELD_DOCS = {
    "language": "Runtime key from codecalc's language registry, e.g. 'python3'.",
    "code": "The program source to execute, verbatim.",
    "stdin": "Text written to the process's standard input. '' means none.",
    "timeout": "Wall-clock ceiling in seconds for the whole request.",
    "workdir": (
        "Working directory for the run, or null to let the provider choose an "
        "ephemeral one. null and '' are different requests."
    ),
    "max_memory_mb": "Address-space ceiling in MiB; 0 means the provider default.",
    "max_output_kb": "Combined stdout+stderr ceiling in KiB; 0 means uncapped.",
    "max_cpu": "CPU-time ceiling in seconds; 0 means the provider default.",
    "no_net": (
        "Request network isolation. Providers that cannot enforce it disclose so "
        "in the result's `unenforced` list rather than failing quietly."
    ),
}


def build_spec_schema(dialect: str | None = None,
                      schema_id: str | None = None) -> dict:
    """The published JSON Schema for a canonical ComputationSpec document."""
    return spec_identity.build_spec_schema(
        ComputationSpec,
        field_docs=COMPUTATION_SPEC_FIELD_DOCS,
        dialect=dialect,
        schema_id=schema_id,
    )


#: Semver of the execution receipt (THE-782). Separate from CONTRACT_VERSION
#: and from COMPUTATION_SPEC_VERSION: those version what a result LOOKS like and
#: what a request IS. This versions the provenance block attached to a result, so
#: a reader can tell "this receipt predates source hashes" from "this run had no
#: source", which are not the same fact.
#:
#: MAJOR removes or retypes a key · MINOR adds one · PATCH changes descriptions.
RECEIPT_VERSION = "1.0.0"

#: The determinism inputs the receipt has a slot for. Every one of them appears
#: EITHER with a value OR in `unrecorded` — never as a bare null, because a null
#: with no explanation reads identically to "the value was empty" and to "this
#: key was never populated", and those are different facts about a run.
DETERMINISM_INPUTS = ("locale.LANG", "locale.LC_ALL", "timezone", "seed")

#: The provider whose executions run through codecalc's OWN sandbox, and are
#: therefore the only ones whose environment codecalc can honestly describe.
#: `execute_session` already keys on this same identifier.
_SANDBOXED_PROVIDER_ID = "local"


def _determinism_value(receipt: dict, dotted: str) -> object:
    node: object = receipt
    for part in dotted.split("."):
        node = node[part]  # type: ignore[index]
    return node


def _determinism_receipt(provider_id: str) -> dict:
    """Locale, timezone and seed — as the run will actually see them.

    DERIVED, NOT TRANSCRIBED. `executor._env()` is the single function that
    builds the child's environment from the allowlist, and both the sandbox and
    the session worker call it. Reading the answer back out of it means this
    block cannot drift from what is really forwarded: add TZ to the allowlist and
    `timezone` starts being populated here with no edit, which is the whole
    point of not copying the list.

    THREE THINGS ARE HONEST HERE RATHER THAN CONVENIENT:

      * `TZ` is NOT in the env allowlist today, so the child inherits the host's
        system zone and codecalc cannot name it. Reported as unrecorded rather
        than guessed from the server's own `time.tzname`, which would describe
        the wrong process.
      * codecalc has NO seed concept — no tool accepts one and no runtime is
        seeded — so `seed` is null on every run, and says why.
      * A provider other than `local` runs somewhere codecalc does not control.
        Reporting this server's locale for a run that happened on someone else's
        host would be a confident lie, so nothing is reported at all.
    """
    if provider_id == _SANDBOXED_PROVIDER_ID:
        forwarded = executor._env()
        receipt = {
            "source": "env_allowlist",
            "locale": {"LANG": forwarded.get("LANG"),
                       "LC_ALL": forwarded.get("LC_ALL")},
            "timezone": forwarded.get("TZ"),
            "seed": None,
        }
    else:
        receipt = {
            "source": "provider_owned",
            "locale": {"LANG": None, "LC_ALL": None},
            "timezone": None,
            "seed": None,
        }
    receipt["unrecorded"] = sorted(
        name for name in DETERMINISM_INPUTS
        if _determinism_value(receipt, name) is None
    )
    return receipt


def _limit_receipt(spec: ComputationSpec, result: dict) -> dict:
    disclosures = [str(item) for item in result.get("unenforced") or []]
    disclosure_text = "\n".join(disclosures).lower()
    requested_controls = (
        ("timeout", spec.timeout > 0, ("timeout",)),
        ("max_memory_mb", spec.max_memory_mb > 0, ("memory",)),
        ("max_output_kb", spec.max_output_kb > 0, ("output",)),
        ("max_cpu", spec.max_cpu > 0, ("cpu",)),
        ("no_net", spec.no_net, ("no_net", "network")),
    )
    reported_enforced = [
        name
        for name, requested, markers in requested_controls
        if requested and not any(marker in disclosure_text for marker in markers)
    ]
    return {
        "requested": {
            "timeout_seconds": spec.timeout,
            "max_memory_mb": spec.max_memory_mb,
            "max_output_kb": spec.max_output_kb,
            "max_cpu_seconds": spec.max_cpu,
            "no_net": spec.no_net,
        },
        "provider_reported_enforced": reported_enforced,
        "unenforced": disclosures,
    }


def _execution_receipt(spec: ComputationSpec, descriptor: dict,
                       result: dict) -> dict:
    """Provider identity, content hashes, determinism inputs, limits (THE-782).

    THE-790 attached provider identity and the limits receipt. Those answer
    "who ran it" and "what did you ask for"; they do not answer the question
    someone re-reading a result a week later actually has, which is WHICH
    request this was. `spec_hash` answers it by content, `source_sha256` names
    the program independently of the limits wrapped around it, and
    `spec_schema_version` records the canonical form both were taken under —
    without it a stored hash is a number whose meaning has an expiry date
    nobody wrote down.

    NOTHING MACHINE-SPECIFIC GOES IN. `workdir` may be an absolute path on the
    operator's disk; it reaches the receipt only through `spec_hash`, never as
    text. The determinism block reads exactly three names out of the forwarded
    environment rather than copying it, so PATH and HOME cannot ride along.
    """
    return {
        "receipt_version": RECEIPT_VERSION,
        "interface_version": descriptor["interface_version"],
        "provider_id": descriptor["provider_id"],
        "provider_version": descriptor["provider_version"],
        "host_class": descriptor["host_class"],
        "spec_schema_version": spec_identity.COMPUTATION_SPEC_VERSION,
        "spec_hash": spec_identity.spec_hash(spec),
        "source_sha256": spec_identity.text_hash(spec.code),
        "determinism": _determinism_receipt(descriptor["provider_id"]),
        "limits": _limit_receipt(spec, result),
    }


def attach_receipt(spec: ComputationSpec, provider: ExecutionProvider, result: dict) -> dict:
    """Add the FULL execution receipt (identity, content hashes, determinism
    inputs, limits enforcement — THE-782) every full execution result
    carries, mutating and returning `result`.

    Moved here from execution_service.py (originally private to
    `ExecutionService.execute()`/`execute_session()`/`execute_stream()`) so
    RunSupervisor's own background-run collection (`_collect()`, THE-778
    fix-round review) can produce the identical shape `execute_code` does
    instead of a caller being able to tell "ran through ExecutionService"
    from "ran through run_submit" by whether `provider` is present, or by
    which RECEIPT FIELDS it carries. `providers.py` is the one module both
    `execution_service.py` and `run_supervisor.py` already import, which is
    what makes this the shared home without a circular import — and it
    already imports `spec_identity` for `ComputationSpec`'s own identity
    methods, so this needed no new dependency to relocate here.
    """
    result["provider"] = _execution_receipt(spec, provider.describe(), result)
    return result


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


class ProviderOperationFailure(RuntimeError):
    """A provider lifecycle operation failed after secrets were removed."""

    def __init__(self, provider_id: str, operation: str) -> None:
        self.provider_id = provider_id
        self.operation = operation
        self.code = f"provider_{operation}_failed"
        super().__init__(f"provider {provider_id!r} {operation} failed")


@runtime_checkable
class ExecutionProvider(Protocol):
    """The provider surface implemented independently of MCP or HTTP."""

    provider_id: str

    def describe(self) -> dict: ...

    def health(self) -> dict: ...

    def list_runtimes(self) -> list[dict]: ...

    def execute(self, spec: ComputationSpec) -> dict: ...

    def execute_managed(self, run_id: str, spec: ComputationSpec) -> dict: ...

    async def execute_stream(self, spec: ComputationSpec,
                             on_progress=None) -> dict: ...

    def inspect(self, run_id: str) -> dict: ...

    def stream(self, run_id: str) -> object: ...

    def cancel(self, run_id: str) -> None: ...

    def cleanup(self, run_id: str) -> None: ...

    def collect_artifacts(self, run_id: str) -> list[dict]: ...

    def list_files(self, run_id: str) -> list[dict]: ...

    def create_session(self, language: str) -> dict: ...


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
    strict: bool
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
            strict=False,
            capabilities={
                "execute": True,
                "managed_runs": False,
                "inspect": False,
                "stream": True,
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

    def execute_managed(self, run_id: str, spec: ComputationSpec) -> dict:
        del run_id, spec
        raise UnsupportedCapability(self.provider_id, "managed_runs")

    async def execute_stream(self, spec: ComputationSpec,
                             on_progress=None) -> dict:
        return await executor.execute_stream(spec, on_progress=on_progress)

    def cancel(self, run_id: str) -> None:
        del run_id
        raise UnsupportedCapability(self.provider_id, "cancel")

    def inspect(self, run_id: str) -> dict:
        del run_id
        raise UnsupportedCapability(self.provider_id, "inspect")

    def stream(self, run_id: str) -> object:
        del run_id
        raise UnsupportedCapability(self.provider_id, "stream")

    def cleanup(self, run_id: str) -> None:
        del run_id
        raise UnsupportedCapability(self.provider_id, "cleanup")

    def collect_artifacts(self, run_id: str) -> list[dict]:
        del run_id
        raise UnsupportedCapability(self.provider_id, "artifacts")

    def list_files(self, run_id: str) -> list[dict]:
        del run_id
        raise UnsupportedCapability(self.provider_id, "files")

    def create_session(self, language: str) -> dict:
        del language
        raise UnsupportedCapability(self.provider_id, "sessions")


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
            strict=False,
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
        }
        if spec.max_memory_mb > 0:
            memory_limit = spec.max_memory_mb * 1024 * 1024
            payload["compile_memory_limit"] = memory_limit
            payload["run_memory_limit"] = memory_limit
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
        compile_stage = (data.get("compile")
                         if isinstance(data.get("compile"), dict) else {})
        run_stage = data.get("run") if isinstance(data.get("run"), dict) else {}
        phase = "run" if run_stage else "compile"
        stage = run_stage or compile_stage
        stdout = str(stage.get("stdout") or "")
        stderr = str(stage.get("stderr") or "")
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
        exit_code = stage.get("code")
        compile_ms = int(compile_stage.get("wall_time") or 0)
        run_ms = int(run_stage.get("wall_time") or 0)
        duration_ms = run_ms if run_stage else compile_ms
        cpu_ms = int(stage.get("cpu_time") or 0)
        memory_bytes = stage.get("memory")
        peak_memory_kb = (int(memory_bytes) // 1024
                          if isinstance(memory_bytes, int) else None)
        status = stage.get("status")
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
            "phase": phase,
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
            "compile_ms": compile_ms,
            "total_ms": compile_ms + run_ms,
            "cpu_ms": cpu_ms,
            "peak_memory_kb": peak_memory_kb,
            "unenforced": unenforced,
            "workdir": "piston:ephemeral",
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
        }
        return contract.stamp(_redact_secrets(result, self._secrets))

    def execute_managed(self, run_id: str, spec: ComputationSpec) -> dict:
        del run_id, spec
        raise UnsupportedCapability(self.provider_id, "managed_runs")

    async def execute_stream(self, spec: ComputationSpec,
                             on_progress=None) -> dict:
        del spec, on_progress
        raise UnsupportedCapability(self.provider_id, "stream")

    def cancel(self, run_id: str) -> None:
        del run_id
        raise UnsupportedCapability(self.provider_id, "cancel")

    def inspect(self, run_id: str) -> dict:
        del run_id
        raise UnsupportedCapability(self.provider_id, "inspect")

    def stream(self, run_id: str) -> object:
        del run_id
        raise UnsupportedCapability(self.provider_id, "stream")

    def cleanup(self, run_id: str) -> None:
        del run_id
        raise UnsupportedCapability(self.provider_id, "cleanup")

    def collect_artifacts(self, run_id: str) -> list[dict]:
        del run_id
        raise UnsupportedCapability(self.provider_id, "artifacts")

    def list_files(self, run_id: str) -> list[dict]:
        del run_id
        raise UnsupportedCapability(self.provider_id, "files")

    def create_session(self, language: str) -> dict:
        del language
        raise UnsupportedCapability(self.provider_id, "sessions")


def strict_host_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "unsupported"


class NativeStrictExecutionProvider(LocalExecutionProvider):
    """Host strict provider that refuses work until its backend is verified."""

    def __init__(self, platform_name: str) -> None:
        self.platform_name = platform_name
        self.provider_id = f"{platform_name}-strict"

    def describe(self) -> dict:
        return ProviderDescriptor(
            interface_version=PROVIDER_INTERFACE_VERSION,
            provider_id=self.provider_id,
            provider_version=__version__,
            host_class=self.platform_name,
            strict=True,
            capabilities={
                "execute": True,
                "managed_runs": False,
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
                "network_control": True,
            },
        ).to_dict()

    def health(self) -> dict:
        technology = {
            "linux": "cgroup-v2+namespaces+seccomp+landlock",
            "windows": "appcontainer+job-object",
            "macos": "virtualization-framework",
        }.get(self.platform_name, "unsupported")
        return {
            "provider_id": self.provider_id,
            "provider_version": __version__,
            "ready": False,
            "strict": True,
            "technology": technology,
            "error": "strict backend prerequisites have not been verified",
        }

    def execute(self, spec: ComputationSpec) -> dict:
        del spec
        return contract.stamp(errors.error_result(
            errors.INTERNAL,
            f"{self.provider_id} is unavailable: strict backend prerequisites "
            "have not been verified; refusing to run the payload",
            provider_error="strict_provider_unavailable",
            requested_provider=self.provider_id,
        ))

    def execute_managed(self, run_id: str, spec: ComputationSpec) -> dict:
        del run_id
        return self.execute(spec)


class RemoteStrictExecutionProvider:
    """Authenticated client for the Linux strict execution service."""

    def __init__(self, *, client_platform: str, base_url: str,
                 authorization: str | None = None,
                 transport: ProviderTransport | None = None) -> None:
        self.client_platform = client_platform
        self.provider_id = f"{client_platform}-strict"
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("remote strict provider credentials belong in Authorization")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("remote strict provider requires HTTPS (HTTP is loopback-only)")
        self._transport = transport or PistonHTTPTransport(self.base_url)
        self._headers = ({"Authorization": authorization} if authorization else {})
        self._authenticated = bool(authorization)
        raw_credential = authorization.split(maxsplit=1)[-1] if authorization else ""
        self._secrets = tuple(sorted(
            {item for item in (authorization, raw_credential) if item},
            key=len, reverse=True,
        ))

    def describe(self) -> dict:
        return ProviderDescriptor(
            interface_version=PROVIDER_INTERFACE_VERSION,
            provider_id=self.provider_id,
            provider_version=__version__,
            host_class=self.client_platform,
            strict=True,
            capabilities={
                "execute": True,
                "managed_runs": True,
                "inspect": True,
                "stream": False,
                "cancel": True,
                "cleanup": True,
                "files": False,
                "artifacts": False,
                "sessions": False,
                "health": True,
                "runtime_discovery": True,
                "workdir": False,
                "network_control": True,
            },
        ).to_dict()

    def _failure(self, message: str, provider_error: str) -> dict:
        result = contract.stamp(errors.error_result(
            errors.INTERNAL,
            message,
            provider_error=provider_error,
            requested_provider=self.provider_id,
        ))
        return _redact_secrets(result, self._secrets)

    def _verified_health(self) -> tuple[dict | None, dict | None]:
        if not self._authenticated:
            return None, self._failure(
                "strict provider authentication is required",
                "strict_authentication_required",
            )
        try:
            response = self._transport(
                "GET", "/v1/health", dict(self._headers), None, 10
            )
        except Exception as exc:
            return None, self._failure(
                f"strict provider health check failed: {exc}",
                "strict_provider_unavailable",
            )
        health = response if isinstance(response, dict) else {}
        offered = str(health.get("interface_version", ""))
        compatible = offered.split(".", 1)[0] == PROVIDER_INTERFACE_VERSION.split(".", 1)[0]
        enforcement = health.get("enforcement")
        missing = [
            name for name in STRICT_CONTROLS
            if not isinstance(enforcement, dict) or enforcement.get(name) is not True
        ]
        if (health.get("provider_id") != "linux-strict"
                or health.get("isolation_profile") != STRICT_ISOLATION_PROFILE
                or health.get("ready") is not True or health.get("strict") is not True
                or not compatible or missing):
            detail = ", ".join(missing) if missing else "readiness or interface"
            return None, self._failure(
                f"strict provider attestation failed: {detail}",
                "strict_attestation_failed",
            )
        return health, None

    def health(self) -> dict:
        health, failure = self._verified_health()
        if failure is not None:
            return {
                "provider_id": self.provider_id,
                "provider_version": __version__,
                "ready": False,
                "strict": True,
                "technology": "remote-linux-strict",
                "error": failure["error"],
            }
        return {
            "provider_id": self.provider_id,
            "provider_version": __version__,
            "ready": True,
            "strict": True,
            "technology": "remote-linux-strict",
            "remote_provider_id": health["provider_id"],
            "isolation_profile": STRICT_ISOLATION_PROFILE,
            "enforcement": dict(health["enforcement"]),
        }

    def list_runtimes(self) -> list[dict]:
        _health, failure = self._verified_health()
        if failure is not None:
            return []
        response = self._transport(
            "GET", "/v1/runtimes", dict(self._headers), None, 10
        )
        return list(response) if isinstance(response, list) else []

    def execute(self, spec: ComputationSpec) -> dict:
        return self.execute_managed(uuid.uuid4().hex, spec)

    def execute_managed(self, run_id: str, spec: ComputationSpec) -> dict:
        if spec.workdir is not None:
            raise UnsupportedCapability(self.provider_id, "workdir")
        health, failure = self._verified_health()
        if failure is not None:
            return failure
        payload = asdict(spec)
        payload["run_id"] = run_id
        try:
            response = self._transport(
                "POST", f"/v1/runs/{run_id}/execute", dict(self._headers),
                payload, spec.timeout + 5,
            )
        except Exception as exc:
            return self._failure(
                f"strict provider execution failed: {exc}",
                "provider_transport_failure",
            )
        result = dict(response) if isinstance(response, dict) else {}
        enforcement = result.pop("enforcement", None)
        missing = [
            name for name in STRICT_CONTROLS
            if not isinstance(enforcement, dict) or enforcement.get(name) is not True
        ]
        if missing:
            return self._failure(
                f"strict execution receipt omitted controls: {', '.join(missing)}",
                "strict_attestation_failed",
            )
        result["strict_receipt"] = {
            "verified": True,
            "remote_provider_id": health["provider_id"],
            "isolation_profile": STRICT_ISOLATION_PROFILE,
            "controls": list(STRICT_CONTROLS),
        }
        return contract.stamp(_redact_secrets(result, self._secrets))

    async def execute_stream(self, spec: ComputationSpec, on_progress=None) -> dict:
        del spec, on_progress
        raise UnsupportedCapability(self.provider_id, "stream")

    def inspect(self, run_id: str) -> dict:
        if not self._authenticated:
            raise ProviderOperationFailure(self.provider_id, "inspect")
        response = self._transport(
            "GET", f"/v1/runs/{run_id}", dict(self._headers), None, 10
        )
        result = dict(response) if isinstance(response, dict) else {}
        return _redact_secrets(result, self._secrets)

    def stream(self, run_id: str) -> object:
        del run_id
        raise UnsupportedCapability(self.provider_id, "stream")

    def cancel(self, run_id: str) -> None:
        if not self._authenticated:
            raise ProviderOperationFailure(self.provider_id, "cancel")
        try:
            self._transport(
                "POST", f"/v1/runs/{run_id}/cancel", dict(self._headers), {}, 10
            )
        except Exception:
            raise ProviderOperationFailure(self.provider_id, "cancel") from None

    def cleanup(self, run_id: str) -> None:
        if not self._authenticated:
            raise ProviderOperationFailure(self.provider_id, "cleanup")
        try:
            self._transport(
                "DELETE", f"/v1/runs/{run_id}", dict(self._headers), None, 10
            )
        except Exception:
            raise ProviderOperationFailure(self.provider_id, "cleanup") from None

    def collect_artifacts(self, run_id: str) -> list[dict]:
        del run_id
        raise UnsupportedCapability(self.provider_id, "artifacts")

    def list_files(self, run_id: str) -> list[dict]:
        del run_id
        raise UnsupportedCapability(self.provider_id, "files")

    def create_session(self, language: str) -> dict:
        del language
        raise UnsupportedCapability(self.provider_id, "sessions")


def configured_registry(
    environment: Mapping[str, str] | None = None,
    *,
    strict_transport: ProviderTransport | None = None,
) -> ProviderRegistry:
    """Build the provider registry from process configuration."""
    env = os.environ if environment is None else environment
    registry = ProviderRegistry(
        default_provider_id=env.get(DEFAULT_PROVIDER_ENV, "local")
    )
    registry.register(LocalExecutionProvider())
    platform_name = strict_host_platform()
    strict_url = env.get(STRICT_URL_ENV, "").strip()
    if platform_name != "unsupported" and strict_url:
        registry.register(RemoteStrictExecutionProvider(
            client_platform=platform_name,
            base_url=strict_url,
            authorization=env.get(STRICT_AUTHORIZATION_ENV) or None,
            transport=strict_transport,
        ))
    elif platform_name != "unsupported":
        registry.register(NativeStrictExecutionProvider(platform_name))
    piston_url = env.get(PISTON_URL_ENV, "").strip()
    if piston_url:
        registry.register(PistonExecutionProvider(
            base_url=piston_url,
            authorization=env.get(PISTON_AUTHORIZATION_ENV) or None,
        ))
    return registry
