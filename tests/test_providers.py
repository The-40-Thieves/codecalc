"""Execution-provider contract and first local implementation (THE-790)."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from pathlib import Path

from _provider_conformance import run_execution_conformance
from jsonschema import Draft202012Validator

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
    check("configured provider registry retains the local provider",
          [item["provider_id"] for item in registry.descriptors()]
          == ["local", "piston"])
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
    sys.exit(1 if FAILS else 0)
