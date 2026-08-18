"""Execution-provider contract and first local implementation (THE-790)."""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from _provider_conformance import run_execution_conformance
from jsonschema import Draft202012Validator

from codecalc import contract, providers, spec_identity

REPO_ROOT = Path(__file__).resolve().parents[1]

SPEC_SCHEMA_PATH = REPO_ROOT / "docs" / "contract" / "computation-spec-v1.schema.json"
SPEC_VECTORS_PATH = REPO_ROOT / "docs" / "contract" / "computation-spec-v1.vectors.json"

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


def test_local_provider_owns_streaming_and_truthfully_falls_back() -> None:
    provider = providers.LocalExecutionProvider()
    old_rust = providers.executor._rust
    providers.executor._rust = None
    try:
        result = asyncio.run(provider.execute_stream(
            providers.ComputationSpec(language="python3", code='print("stream")')
        ))
    finally:
        providers.executor._rust = old_rust

    check("local provider advertises streaming",
          provider.describe()["capabilities"]["stream"] is True)
    check("streaming fallback preserves execution output",
          result["ok"] is True and result["stdout"].strip() == "stream")
    check("streaming fallback discloses that progress was unavailable",
          result["streamed"] is False and "note" in result)


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


def test_piston_provider_discovers_remote_runtimes() -> None:
    requests: list[dict] = []

    def transport(method: str, path: str, headers: dict[str, str],
                  payload: dict | None, timeout: int) -> object:
        requests.append({
            "method": method,
            "path": path,
            "headers": headers,
            "payload": payload,
            "timeout": timeout,
        })
        return [{
            "language": "python",
            "version": "3.12.0",
            "aliases": ["py", "python3"],
            "runtime": "cpython",
        }]

    provider_type = getattr(providers, "PistonExecutionProvider", None)
    check("Piston provider is available", provider_type is not None)
    if provider_type is None:
        return
    provider = provider_type(base_url="http://piston.test", transport=transport)

    runtimes = provider.list_runtimes()

    check("Piston runtime discovery preserves the API payload", runtimes == [{
        "language": "python",
        "version": "3.12.0",
        "aliases": ["py", "python3"],
        "runtime": "cpython",
    }])
    check("Piston runtime discovery uses the v2 endpoint",
          requests == [{
              "method": "GET",
              "path": "/api/v2/runtimes",
              "headers": {},
              "payload": None,
              "timeout": 10,
          }])


def test_piston_credentials_are_scoped_to_transport_headers() -> None:
    authorization_value = "Bearer " + "provider-secret"
    seen_headers: list[dict[str, str]] = []

    def transport(method: str, path: str, headers: dict[str, str],
                  payload: dict | None, timeout: int) -> object:
        del method, path, payload, timeout
        seen_headers.append(headers)
        return []

    provider = providers.PistonExecutionProvider(
        base_url="http://piston.test",
        authorization=authorization_value,
        transport=transport,
    )

    descriptor = provider.describe()
    health = provider.health()
    serialized_public_data = json.dumps({"descriptor": descriptor, "health": health})

    check("Piston credentials are sent only in authorization headers",
          seen_headers == [{"Authorization": authorization_value}])
    check("Piston credentials are absent from descriptors and health",
          authorization_value not in serialized_public_data
          and "provider-secret" not in serialized_public_data)
    check("Piston health reports readiness from runtime discovery",
          health["ready"] is True)


def test_piston_execution_normalizes_the_v2_result_contract() -> None:
    requests: list[dict] = []

    def transport(method: str, path: str, headers: dict[str, str],
                  payload: dict | None, timeout: int) -> object:
        requests.append({
            "method": method,
            "path": path,
            "headers": headers,
            "payload": payload,
            "timeout": timeout,
        })
        return {
            "language": "python",
            "version": "3.12.0",
            "run": {
                "stdout": "remote result\n",
                "stderr": "",
                "output": "remote result\n",
                "code": 0,
                "signal": None,
                "message": None,
                "status": None,
                "cpu_time": 7,
                "wall_time": 12,
                "memory": 2_048_000,
            },
        }

    provider = providers.PistonExecutionProvider(
        base_url="http://piston.test", transport=transport
    )
    spec = providers.ComputationSpec(
        language="python3",
        code='print("remote result")',
        stdin="input\n",
        timeout=4,
        max_memory_mb=32,
        max_cpu=2,
    )

    result = provider.execute(spec)

    check("Piston execution uses the common success verdict",
          result["ok"] is True and result["verdict"] == "OK")
    check("Piston execution preserves output and measurements",
          result["stdout"] == "remote result\n"
          and result["cpu_ms"] == 7
          and result["duration_ms"] == 12
          and result["peak_memory_kb"] == 2000)
    check("Piston execution carries the common result contract",
          result["contract_version"] == contract.CONTRACT_VERSION
          and result["backend"] == "python")
    check("Piston execution validates against the published result schema",
          not list(Draft202012Validator(contract.build_schema()).iter_errors(result)))
    check("Piston execution sends canonical limits to the v2 API",
          requests[0]["method"] == "POST"
          and requests[0]["path"] == "/api/v2/execute"
          and requests[0]["payload"] == {
              "language": "python3",
              "version": "*",
              "files": [{"content": 'print("remote result")'}],
              "stdin": "input\n",
              "compile_timeout": 4000,
              "run_timeout": 4000,
              "compile_cpu_time": 2000,
              "run_cpu_time": 2000,
              "compile_memory_limit": 32 * 1024 * 1024,
              "run_memory_limit": 32 * 1024 * 1024,
          })


def test_piston_execution_maps_remote_statuses_to_common_verdicts() -> None:
    cases = {
        "TO": ("TLE", True, False),
        "OL": ("OLE", False, True),
        "EL": ("OLE", False, True),
        "RE": ("RTE", False, False),
        "SG": ("RTE", False, False),
        "XX": ("RTE", False, False),
    }

    for status, expected in cases.items():
        def transport(method: str, path: str, headers: dict[str, str],
                      payload: dict | None, timeout: int,
                      *, _status: str = status) -> object:
            del method, path, headers, payload, timeout
            return {
                "language": "python",
                "version": "3.12.0",
                "run": {
                    "stdout": "",
                    "stderr": "remote failure",
                    "output": "remote failure",
                    "code": None,
                    "signal": "SIGKILL" if _status == "SG" else None,
                    "message": "remote failure",
                    "status": _status,
                    "cpu_time": 3,
                    "wall_time": 8,
                    "memory": 1024,
                },
            }

        result = providers.PistonExecutionProvider(
            base_url="http://piston.test", transport=transport
        ).execute(providers.ComputationSpec(language="python3", code="pass"))

        verdict, timed_out, truncated = expected
        check(f"Piston status {status} maps to {verdict}",
              result["verdict"] == verdict)
        check(f"Piston status {status} reports timeout truthfully",
              result["timed_out"] is timed_out)
        check(f"Piston status {status} reports output truncation truthfully",
              result["output_truncated"] is truncated)


def test_piston_omits_unrequested_memory_limits() -> None:
    payloads: list[dict] = []

    def transport(method: str, path: str, headers: dict[str, str],
                  payload: dict | None, timeout: int) -> object:
        del method, path, headers, timeout
        payloads.append(dict(payload or {}))
        return {"run": {"stdout": "", "stderr": "", "code": 0}}

    providers.PistonExecutionProvider(
        base_url="http://piston.test", transport=transport
    ).execute(providers.ComputationSpec(language="python3", code="pass"))

    check("Piston omits an unset compile memory limit",
          "compile_memory_limit" not in payloads[0])
    check("Piston omits an unset run memory limit",
          "run_memory_limit" not in payloads[0])


def test_piston_preserves_compile_stage_failures() -> None:
    def transport(method: str, path: str, headers: dict[str, str],
                  payload: dict | None, timeout: int) -> object:
        del method, path, headers, payload, timeout
        return {
            "compile": {
                "stdout": "",
                "stderr": "syntax error\n",
                "code": 1,
                "cpu_time": 4,
                "wall_time": 9,
                "memory": 4096,
            }
        }

    result = providers.PistonExecutionProvider(
        base_url="http://piston.test", transport=transport
    ).execute(providers.ComputationSpec(language="c", code="bad source"))

    check("Piston compile failures retain the compile phase",
          result["phase"] == "compile")
    check("Piston compile failures retain diagnostics and exit code",
          result["stderr"] == "syntax error\n" and result["exit_code"] == 1)
    check("Piston compile failures retain compile timing",
          result["compile_ms"] == 9 and result["duration_ms"] == 9)


def test_piston_provider_rejects_unavailable_capabilities_before_transport() -> None:
    calls = 0

    def transport(method: str, path: str, headers: dict[str, str],
                  payload: dict | None, timeout: int) -> object:
        nonlocal calls
        del method, path, headers, payload, timeout
        calls += 1
        return {}

    provider = providers.PistonExecutionProvider(
        base_url="http://piston.test", transport=transport
    )

    check("Piston provider implements the complete provider protocol",
          isinstance(provider, providers.ExecutionProvider))
    try:
        provider.execute(providers.ComputationSpec(
            language="python3", code="pass", no_net=True
        ))
    except providers.UnsupportedCapability as exc:
        check("Piston rejects request-level network control explicitly",
              exc.capability == "network_control")
    else:
        check("Piston rejects request-level network control explicitly", False)
    try:
        provider.execute(providers.ComputationSpec(
            language="python3", code="pass", workdir="requested/path"
        ))
    except providers.UnsupportedCapability as exc:
        check("Piston rejects remote working-directory selection explicitly",
              exc.capability == "workdir")
    else:
        check("Piston rejects remote working-directory selection explicitly", False)
    check("Piston rejects unsupported policy before remote execution", calls == 0)


def test_piston_http_transport_serializes_json_without_exposing_credentials() -> None:
    opened: list[tuple[object, int]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self) -> bytes:
            return b'{"ok": true}'

    def opener(request: object, timeout: int) -> Response:
        opened.append((request, timeout))
        return Response()

    transport_type = getattr(providers, "PistonHTTPTransport", None)
    check("Piston default HTTP transport is available", transport_type is not None)
    if transport_type is None:
        return
    transport = transport_type("http://piston.test/root/", opener=opener)

    result = transport(
        "POST",
        "/api/v2/execute",
        {"Authorization": "Bearer " + "transport-value"},
        {"language": "python"},
        9,
    )

    request, timeout = opened[0]
    check("Piston HTTP transport joins the configured base URL",
          request.full_url == "http://piston.test/root/api/v2/execute")
    check("Piston HTTP transport preserves method and timeout",
          request.method == "POST" and timeout == 9)
    check("Piston HTTP transport sends JSON",
          json.loads(request.data) == {"language": "python"}
          and request.get_header("Content-type") == "application/json")
    check("Piston HTTP transport applies provider authorization",
          request.get_header("Authorization") == "Bearer transport-value")
    check("Piston HTTP transport decodes JSON responses", result == {"ok": True})


def test_piston_http_transport_rejects_non_http_base_urls() -> None:
    try:
        providers.PistonHTTPTransport("file:///tmp/piston")
    except ValueError as exc:
        check("Piston transport rejects non-HTTP URLs",
              "http" in str(exc).lower())
    else:
        check("Piston transport rejects non-HTTP URLs", False)


def test_piston_execution_redacts_credentials_echoed_by_the_provider() -> None:
    authorization_value = "Bearer " + "echoed-provider-value"

    def transport(method: str, path: str, headers: dict[str, str],
                  payload: dict | None, timeout: int) -> object:
        del method, path, headers, payload, timeout
        return {
            "language": "python",
            "version": "3.12.0",
            "run": {
                "stdout": authorization_value,
                "stderr": "raw echoed-provider-value",
                "output": authorization_value,
                "code": 1,
                "signal": None,
                "message": "failed with echoed-provider-value",
                "status": "RE",
                "cpu_time": 1,
                "wall_time": 2,
                "memory": 1024,
            },
        }

    result = providers.PistonExecutionProvider(
        base_url="http://piston.test",
        authorization=authorization_value,
        transport=transport,
    ).execute(providers.ComputationSpec(language="python3", code="pass"))
    serialized = json.dumps(result)

    check("Piston results redact the complete authorization value",
          authorization_value not in serialized)
    check("Piston results redact the raw provider credential",
          "echoed-provider-value" not in serialized)
    check("Piston results visibly mark credential redaction",
          "[REDACTED]" in serialized)


def test_piston_provider_passes_the_shared_execution_conformance_suite() -> None:
    def transport(method: str, path: str, headers: dict[str, str],
                  payload: dict | None, timeout: int) -> object:
        del path, headers, timeout
        if method == "GET":
            return [{"language": "python", "version": "3.12.0",
                     "aliases": ["python3"]}]
        assert payload is not None
        if payload["language"] == "definitely-missing":
            return {"message": "runtime is unknown"}
        code = payload["files"][0]["content"]
        status = "TO" if "sleep(5)" in code else "OL" if "200000" in code else None
        return {
            "language": "python",
            "version": "3.12.0",
            "run": {
                "stdout": "conformance\n" if status is None else "",
                "stderr": "",
                "output": "conformance\n" if status is None else "",
                "code": 0 if status is None else None,
                "signal": None,
                "message": None,
                "status": status,
                "cpu_time": 1,
                "wall_time": 2,
                "memory": 1024,
            },
        }

    provider = providers.PistonExecutionProvider(
        base_url="http://piston.test", transport=transport
    )

    run_execution_conformance(provider, check)


def test_piston_applies_the_requested_output_cap_to_remote_results() -> None:
    remote_stdout = "x" * 2048

    def transport(method: str, path: str, headers: dict[str, str],
                  payload: dict | None, timeout: int) -> object:
        del method, path, headers, payload, timeout
        return {
            "language": "python",
            "version": "3.12.0",
            "run": {
                "stdout": remote_stdout,
                "stderr": "",
                "output": remote_stdout,
                "code": 0,
                "signal": None,
                "message": None,
                "status": None,
                "cpu_time": 1,
                "wall_time": 2,
                "memory": 1024,
            },
        }

    result = providers.PistonExecutionProvider(
        base_url="http://piston.test", transport=transport
    ).execute(providers.ComputationSpec(
        language="python3", code="print('x')", max_output_kb=1
    ))

    check("Piston adapter classifies client-side output overflow",
          result["verdict"] == "OLE" and result["output_truncated"] is True)
    check("Piston adapter caps normalized output bytes",
          len(result["stdout"].encode()) <= 1024
          and result["stdout_bytes"] == len(remote_stdout.encode()))
    check("Piston adapter discloses post-response output enforcement",
          "max_output_kb_enforced_after_provider_response" in result["unenforced"])


def test_configured_registry_enables_piston_without_disclosing_credentials() -> None:
    authorization_value = "Bearer " + "registry-provider-value"
    environment: Mapping[str, str] = {
        "CODECALC_PISTON_URL": "https://piston.internal",
        "CODECALC_PISTON_AUTHORIZATION": authorization_value,
        "CODECALC_EXECUTION_PROVIDER": "piston",
    }

    factory = getattr(providers, "configured_registry", None)
    check("configured provider registry factory is available", factory is not None)
    if factory is None:
        return
    registry = factory(environment)
    serialized_descriptors = json.dumps(registry.descriptors())

    check("configured provider registry selects Piston by default",
          registry.select().provider_id == "piston")
    provider_ids = [item["provider_id"] for item in registry.descriptors()]
    check("configured registry retains local and host-strict providers",
          "local" in provider_ids and "piston" in provider_ids
          and f"{providers.strict_host_platform()}-strict" in provider_ids)
    check("configured provider registry never publishes credentials",
          authorization_value not in serialized_descriptors
          and "registry-provider-value" not in serialized_descriptors)


def test_piston_health_reports_redacted_transport_failure() -> None:
    authorization_value = "Bearer " + "health-provider-value"

    def transport(method: str, path: str, headers: dict[str, str],
                  payload: dict | None, timeout: int) -> object:
        del method, path, headers, payload, timeout
        raise OSError(f"connection failed for {authorization_value}")

    provider = providers.PistonExecutionProvider(
        base_url="https://piston.internal",
        authorization=authorization_value,
        transport=transport,
    )

    health = provider.health()
    serialized = json.dumps(health)

    check("Piston health reports transport failure as not ready",
          health["ready"] is False)
    check("Piston health identifies the failed provider",
          health["provider_id"] == "piston")
    check("Piston health redacts transport failure credentials",
          "health-provider-value" not in serialized
          and "[REDACTED]" in serialized)


def test_piston_execution_returns_a_redacted_transport_error_contract() -> None:
    authorization_value = "Bearer " + "execution-provider-value"

    def transport(method: str, path: str, headers: dict[str, str],
                  payload: dict | None, timeout: int) -> object:
        del method, path, headers, payload, timeout
        raise OSError(f"remote unavailable for {authorization_value}")

    result = providers.PistonExecutionProvider(
        base_url="https://piston.internal",
        authorization=authorization_value,
        transport=transport,
    ).execute(providers.ComputationSpec(language="python3", code="pass"))
    serialized = json.dumps(result)

    check("Piston transport failure returns a rejected result",
          result["ok"] is False and "verdict" not in result)
    check("Piston transport failure carries a stable provider error",
          result["code"] == "internal"
          and result["provider_error"] == "provider_transport_failure")
    check("Piston transport error carries the result contract",
          result["contract_version"] == contract.CONTRACT_VERSION)
    check("Piston transport error redacts provider credentials",
          "execution-provider-value" not in serialized
          and "[REDACTED]" in serialized)


# ═══ spec identity: canonical bytes, content hash, published schema (THE-793) ═══
#
# THE-790 built the canonical REQUEST. What was missing was its IDENTITY: two
# callers building the same request could not agree on a name for it, so nothing
# downstream — a cache, a receipt, a provenance record — could say "this is the
# same computation" without comparing whole objects.


def _spec(**overrides: object) -> providers.ComputationSpec:
    base: dict = {"language": "python3", "code": 'print("x")'}
    base.update(overrides)
    return providers.ComputationSpec(**base)  # type: ignore[arg-type]


def test_spec_canonical_bytes_ignore_construction_order() -> None:
    """Equivalent requests hash the same however they were built."""
    keyword_first = providers.ComputationSpec(
        timeout=5, code="print(1)", language="python3", no_net=True,
    )
    keyword_last = providers.ComputationSpec(
        language="python3", code="print(1)", no_net=True, timeout=5,
    )
    positional = providers.ComputationSpec("python3", "print(1)", "", 5,
                                           None, 0, 0, 0, True)

    check("keyword order does not change the canonical bytes",
          spec_identity.canonical_bytes(keyword_first)
          == spec_identity.canonical_bytes(keyword_last))
    check("positional construction does not change the canonical bytes",
          spec_identity.canonical_bytes(positional)
          == spec_identity.canonical_bytes(keyword_first))
    check("equivalent requests share one content hash",
          spec_identity.spec_hash(positional) == spec_identity.spec_hash(keyword_last))

    # A default left alone and the same value passed explicitly are the same
    # request. Canonicalization emits every field, so neither can be "absent".
    check("a defaulted field and the same value passed explicitly agree",
          spec_identity.spec_hash(_spec())
          == spec_identity.spec_hash(_spec(stdin="", timeout=10, workdir=None,
                                           max_memory_mb=0, max_output_kb=0,
                                           max_cpu=0, no_net=False)))
    check("a different request gets a different hash",
          spec_identity.spec_hash(_spec()) != spec_identity.spec_hash(_spec(timeout=11)))


def test_spec_canonical_form_is_deterministic_json() -> None:
    """The rules the hash rests on, asserted rather than described."""
    spec = _spec(code='print("héllo")', workdir=None)
    raw = spec_identity.canonical_bytes(spec)
    document = json.loads(raw)

    check("canonical bytes are UTF-8", raw.decode("utf-8") == raw.decode())
    check("canonical bytes carry no insignificant whitespace",
          b", " not in raw and b": " not in raw and b"\n" not in raw)
    check("canonical object keys are sorted",
          list(document["spec"]) == sorted(document["spec"]))
    check("the canonical document names the schema version it was built under",
          document["schema_version"] == providers.COMPUTATION_SPEC_VERSION)
    check("every dataclass field reaches the canonical document",
          set(document["spec"])
          == {f.name for f in dataclasses.fields(providers.ComputationSpec)})
    check("None is a value, not an absence", document["spec"]["workdir"] is None)
    check("None and the empty string are different requests",
          spec_identity.spec_hash(_spec(workdir=None))
          != spec_identity.spec_hash(_spec(workdir="")))
    check("booleans stay booleans and are not folded into 0/1",
          spec_identity.canonical_bytes(_spec(no_net=True)).find(b'"no_net":true') > 0)
    check("booleans and integers do not collide",
          spec_identity.spec_hash(_spec(no_net=True))
          != spec_identity.spec_hash(_spec(no_net=1)))
    check("the content hash names its algorithm",
          spec_identity.spec_hash(spec).startswith("sha256:")
          and len(spec_identity.spec_hash(spec)) == len("sha256:") + 64)


def test_spec_hash_is_stable_across_processes() -> None:
    """A hash that depends on process state is not an identity.

    Run in a FRESH interpreter with hash randomization on, because that is the
    exact way a canonicalizer built on `set` iteration or `hash()` produces a
    stable-looking value inside one process and a different one in the next.
    """
    spec = _spec(code="print(sum(range(3)))", stdin="4\n", max_cpu=2, no_net=True)
    expected = spec_identity.spec_hash(spec)

    probe = (
        "from codecalc import providers, spec_identity;"
        "print(spec_identity.spec_hash(providers.ComputationSpec("
        "language='python3', code='print(sum(range(3)))', stdin='4\\n',"
        " max_cpu=2, no_net=True)))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONHASHSEED": "random"},
    )
    check("a fresh interpreter computes the same content hash",
          proc.returncode == 0 and proc.stdout.strip() == expected)


class _Colour(enum.Enum):
    RED = "red"


@dataclasses.dataclass(frozen=True)
class _Nested:
    name: str
    weight: int


@dataclasses.dataclass(frozen=True)
class _Wide:
    """A spec-shaped dataclass covering every value kind the rules name."""

    text: str
    number: int
    flag: bool
    absent: str | None
    blob: bytes
    items: tuple[str, ...]
    mapping: dict
    nested: _Nested
    colour: _Colour


@dataclasses.dataclass(frozen=True)
class _Floaty:
    ratio: float


@dataclasses.dataclass(frozen=True)
class _Setty:
    tags: set


def test_spec_canonicalization_refuses_what_it_cannot_hash_stably() -> None:
    """Every value kind has a rule, and the rule for "no rule" is to refuse."""
    wide = _Wide(
        text="t", number=7, flag=False, absent=None, blob=b"\x00\xff",
        items=("b", "a"), mapping={"z": 1, "a": 2}, nested=_Nested("n", 3),
        colour=_Colour.RED,
    )
    document = json.loads(spec_identity.canonical_bytes(wide))["spec"]

    check("sequence order is preserved because it is semantic",
          document["items"] == ["b", "a"])
    check("a tuple and a list are the same JSON array",
          spec_identity.canonical_bytes(wide)
          == spec_identity.canonical_bytes(dataclasses.replace(wide, items=["b", "a"])))
    check("mapping keys are sorted so insertion order cannot leak in",
          list(document["mapping"]) == ["a", "z"]
          and spec_identity.canonical_bytes(wide)
          == spec_identity.canonical_bytes(
              dataclasses.replace(wide, mapping={"a": 2, "z": 1})))
    check("a nested dataclass canonicalizes as its own object",
          document["nested"] == {"name": "n", "weight": 3})
    check("an enum canonicalizes as its value", document["colour"] == "red")
    check("bytes are tagged base64 rather than an ambiguous string",
          document["blob"] == {spec_identity.BYTES_TAG: "AP8="})

    def refused(instance: object) -> bool:
        try:
            spec_identity.canonical_bytes(instance)
        except spec_identity.UncanonicalizableValue:
            return True
        return False

    check("a float is refused rather than silently rounded", refused(_Floaty(0.1)))
    check("a set is refused because it has no stable order", refused(_Setty({"a"})))
    check("a non-string mapping key is refused",
          refused(dataclasses.replace(wide, mapping={1: "a"})))
    check("the bytes tag is reserved and cannot be forged by a mapping",
          refused(dataclasses.replace(wide, mapping={spec_identity.BYTES_TAG: "AA=="})))


@dataclasses.dataclass(frozen=True)
class _SecretSpec:
    code: str
    env: dict


@dataclasses.dataclass(frozen=True)
class _ByNameSpec:
    code: str
    env: dict

    HASH_BY_NAME_FIELDS = ("env",)


@dataclasses.dataclass(frozen=True)
class _CompoundSecretSpec:
    """The names a real codebase uses. None of them is the bare word."""

    code: str
    db_password: str
    auth_token: str
    client_secret: str
    apiKey: str  # camelCase on purpose; the tokeniser must split it


@dataclasses.dataclass(frozen=True)
class _MappingSecretSpec:
    code: str
    metadata: dict


@dataclasses.dataclass(frozen=True)
class _DeclaredNotSecretSpec:
    code: str
    token_budget: int

    NOT_SECRET_FIELDS = ("token_budget",)


def test_spec_canonical_content_never_carries_secret_values() -> None:
    """The identity of a request must not become a place secrets are stored."""
    today = {f.name for f in dataclasses.fields(providers.ComputationSpec)}
    check("ComputationSpec declares no secret-bearing field today",
          not (today & spec_identity.SECRET_BEARING_FIELD_NAMES))

    try:
        spec_identity.canonical_bytes(_SecretSpec(code="x", env={"TOKEN": "s3cret"}))
        refused = False
    except spec_identity.UncanonicalizableValue as exc:
        refused = "env" in str(exc)
    check("an undeclared secret-bearing field is refused, not hashed", refused)

    by_name = spec_identity.canonical_bytes(
        _ByNameSpec(code="x", env={"TOKEN": "s3cret", "API_KEY": "also-secret"}))
    check("a declared secret-bearing field is hashed by NAME only",
          json.loads(by_name)["spec"]["env"] == ["API_KEY", "TOKEN"])
    check("no secret value reaches the canonical bytes",
          b"s3cret" not in by_name and b"also-secret" not in by_name)
    check("changing a secret VALUE does not change the request identity",
          spec_identity.spec_hash(_ByNameSpec(code="x", env={"TOKEN": "a"}))
          == spec_identity.spec_hash(_ByNameSpec(code="x", env={"TOKEN": "b"})))
    check("changing a secret NAME does change the request identity",
          spec_identity.spec_hash(_ByNameSpec(code="x", env={"TOKEN": "a"}))
          != spec_identity.spec_hash(_ByNameSpec(code="x", env={"OTHER": "a"})))


def test_secret_refusal_reads_compound_names_not_just_bare_words() -> None:
    """`db_password` is not the word `password`, and a gate that only knows the
    bare word protects nothing a real codebase would actually be named.

    Found by cross-vendor review of the first version of this module, which
    matched the field name EXACTLY against a twelve-word set: `db_password`,
    `auth_token` and `client_secret` all hashed their values unrefused while the
    docstring claimed the rule was "a gate rather than advice".
    """
    def refusal(instance: object) -> str:
        try:
            spec_identity.canonical_bytes(instance)
        except spec_identity.UncanonicalizableValue as exc:
            return str(exc)
        return ""

    # S106 noqa: these are fixture values for a test whose whole subject is
    # credential-shaped field NAMES. Nothing here is a real credential.
    compound = refusal(_CompoundSecretSpec(
        code="x", db_password="hunter2", auth_token="abc123",  # noqa: S106
        client_secret="shh", apiKey="k"))  # noqa: S106
    check("a compound secret field name is refused", bool(compound))
    check("the refusal names the field it stopped on", "db_password" in compound)

    for field, value in (("db_password", "hunter2"), ("auth_token", "abc123"),
                         ("client_secret", "shh"), ("apiKey", "k")):
        one = dataclasses.make_dataclass(
            "_One", [("code", str), (field, str)], frozen=True)
        check(f"{field} alone is refused", bool(refusal(one("x", value))))

    check("a mapping KEY that names a secret is refused too",
          bool(refusal(_MappingSecretSpec(code="x",
                                          metadata={"auth_token": "abc123"}))))
    check("an innocent mapping key still hashes",
          not refusal(_MappingSecretSpec(code="x", metadata={"run_label": "a"})))

    # The cost of a token rule is false positives, so there is a reviewable way
    # out that keeps the VALUE in the identity — distinct from HASH_BY_NAME_FIELDS,
    # which silently drops it.
    check("a field declared not-secret hashes its value normally",
          json.loads(spec_identity.canonical_bytes(
              _DeclaredNotSecretSpec(code="x", token_budget=7)))
          ["spec"]["token_budget"] == 7)
    undeclared = dataclasses.make_dataclass(
        "_Undeclared", [("code", str), ("token_budget", int)], frozen=True)
    check("...and without that declaration the same name is refused",
          bool(refusal(undeclared("x", 7))))

    check("an innocent field that merely CONTAINS a secret word is not refused",
          not refusal(dataclasses.make_dataclass(
              "_Author", [("code", str), ("author", str)], frozen=True)("x", "me")))
    check("ComputationSpec's own field names still pass the tokeniser",
          not refusal(providers.ComputationSpec(language="python3", code="x")))


def test_published_spec_schema_cannot_drift_from_the_dataclass() -> None:
    """The published document is derived from the dataclass, then diffed."""
    check("the spec schema is published", SPEC_SCHEMA_PATH.exists())
    published = json.loads(SPEC_SCHEMA_PATH.read_text(encoding="utf-8"))
    generated = providers.build_spec_schema(
        dialect=published.get("$schema"), schema_id=published.get("$id"))

    check("the published spec schema matches the generator", published == generated)

    Draft202012Validator.check_schema(published)
    validator = Draft202012Validator(published)
    properties = published["$defs"]["computation_spec"]["properties"]
    declared = {f.name for f in dataclasses.fields(providers.ComputationSpec)}

    check("the published property set is exactly the dataclass field set",
          set(properties) == declared and len(declared) >= 5)
    check("every canonical field is required, defaults included",
          sorted(published["$defs"]["computation_spec"]["required"]) == sorted(declared))
    check("the spec object is closed so an unknown field cannot pass unhashed",
          published["$defs"]["computation_spec"]["additionalProperties"] is False)
    check("every field carries a description",
          all(properties[name].get("description") for name in properties))
    check("the schema states the version the canonical document declares",
          published["properties"]["schema_version"]["const"]
          == providers.COMPUTATION_SPEC_VERSION)
    check("a real canonical document satisfies the published schema",
          validator.is_valid(json.loads(spec_identity.canonical_bytes(_spec()))))
    check("a canonical document with an extra field is rejected",
          not validator.is_valid({"schema_version": providers.COMPUTATION_SPEC_VERSION,
                                  "spec": {**json.loads(
                                      spec_identity.canonical_bytes(_spec()))["spec"],
                                      "surprise": 1}}))

    policy = (REPO_ROOT / "docs" / "contract" / "README.md").read_text(encoding="utf-8")
    check("the spec schema version is documented",
          providers.COMPUTATION_SPEC_VERSION in policy
          and "computation-spec-v1.schema.json" in policy)


def test_spec_golden_vectors_pin_the_canonical_encoding() -> None:
    """Committed vectors, recomputed. Changing a rule turns these red.

    Deliberately NOT regenerated by any --write flag: a vector the generator
    rewrites is the generator reading its own output, which is a green check
    for a comparison that never happened.
    """
    check("golden vectors are published", SPEC_VECTORS_PATH.exists())
    document = json.loads(SPEC_VECTORS_PATH.read_text(encoding="utf-8"))
    vectors = document["vectors"]

    check("the vector file names the schema version it pins",
          document["schema_version"] == providers.COMPUTATION_SPEC_VERSION)
    check("there are enough vectors to be a corpus rather than an example",
          len(vectors) >= 5)

    wrong_bytes, wrong_hash = [], []
    for vector in vectors:
        spec = providers.ComputationSpec(**vector["kwargs"])
        if spec_identity.canonical_bytes(spec).decode() != vector["canonical"]:
            wrong_bytes.append(vector["name"])
        if spec_identity.spec_hash(spec) != vector["spec_hash"]:
            wrong_hash.append(vector["name"])

    check(f"all {len(vectors)} vectors reproduce their canonical bytes", not wrong_bytes)
    check(f"all {len(vectors)} vectors reproduce their content hash", not wrong_hash)
    check("the vectors exercise defaulted and fully-explicit construction",
          any(len(v["kwargs"]) == 2 for v in vectors)
          and any(len(v["kwargs"])
                  == len(dataclasses.fields(providers.ComputationSpec)) for v in vectors))

    # Injective, not unique: the corpus deliberately holds two vectors that ARE
    # the same request (defaults left alone vs passed explicitly), so equal bytes
    # must share a hash. Different bytes sharing one is the failure.
    by_bytes: dict[str, set[str]] = {}
    for vector in vectors:
        by_bytes.setdefault(vector["canonical"], set()).add(vector["spec_hash"])
    check("each canonical byte string maps to exactly one hash",
          all(len(hashes) == 1 for hashes in by_bytes.values()))
    check("different canonical byte strings never share a hash",
          len({h for hashes in by_bytes.values() for h in hashes}) == len(by_bytes))
    check("the corpus pins that defaults and explicit values are one request",
          len(by_bytes) < len(vectors))


if __name__ == "__main__":
    test_descriptor_is_versioned_and_machine_readable()
    test_local_provider_preserves_the_execution_result_contract()
    test_local_provider_owns_streaming_and_truthfully_falls_back()
    test_unsupported_provider_capability_fails_explicitly()
    test_registry_supports_default_and_explicit_provider_selection()
    test_unknown_provider_fails_with_a_stable_machine_code()
    test_provider_interface_is_published_with_its_versioning_policy()
    test_registry_rejects_an_incompatible_provider_interface()
    test_policy_routing_receives_the_spec_and_explicit_selection_wins()
    test_local_provider_passes_the_shared_execution_conformance_suite()
    test_local_provider_reports_health_and_runtime_discovery()
    test_piston_provider_discovers_remote_runtimes()
    test_piston_credentials_are_scoped_to_transport_headers()
    test_piston_execution_normalizes_the_v2_result_contract()
    test_piston_execution_maps_remote_statuses_to_common_verdicts()
    test_piston_omits_unrequested_memory_limits()
    test_piston_preserves_compile_stage_failures()
    test_piston_provider_rejects_unavailable_capabilities_before_transport()
    test_piston_http_transport_serializes_json_without_exposing_credentials()
    test_piston_http_transport_rejects_non_http_base_urls()
    test_piston_execution_redacts_credentials_echoed_by_the_provider()
    test_piston_provider_passes_the_shared_execution_conformance_suite()
    test_piston_applies_the_requested_output_cap_to_remote_results()
    test_configured_registry_enables_piston_without_disclosing_credentials()
    test_piston_health_reports_redacted_transport_failure()
    test_piston_execution_returns_a_redacted_transport_error_contract()
    test_spec_canonical_bytes_ignore_construction_order()
    test_spec_canonical_form_is_deterministic_json()
    test_spec_hash_is_stable_across_processes()
    test_spec_canonicalization_refuses_what_it_cannot_hash_stably()
    test_spec_canonical_content_never_carries_secret_values()
    test_secret_refusal_reads_compound_names_not_just_bare_words()
    test_published_spec_schema_cannot_drift_from_the_dataclass()
    test_spec_golden_vectors_pin_the_canonical_encoding()
    sys.exit(1 if FAILS else 0)
