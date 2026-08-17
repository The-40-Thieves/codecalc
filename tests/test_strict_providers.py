"""Shared fail-closed contract for native strict providers (THE-828..830)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from codecalc import doctor, execution_service, providers, run_supervisor

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


def test_doctor_reports_strict_provider_readiness() -> None:
    report = doctor.report()
    provider_id = f"{providers.strict_host_platform()}-strict"
    rows = {row["provider_id"]: row for row in report["execution_providers"]}
    check("doctor publishes strict provider readiness", provider_id in rows)
    check("doctor does not confuse local health with strict readiness",
          rows[provider_id]["strict"] is True
          and rows[provider_id]["ready"] is False)


def _strict_health(**overrides) -> dict:
    health = {
        "interface_version": providers.PROVIDER_INTERFACE_VERSION,
        "provider_id": "linux-strict",
        "provider_version": "test",
        "isolation_profile": providers.STRICT_ISOLATION_PROFILE,
        "ready": True,
        "strict": True,
        "enforcement": dict.fromkeys(providers.STRICT_CONTROLS, True),
    }
    health.update(overrides)
    return health


def test_remote_strict_rejects_incomplete_handshake_before_payload() -> None:
    requests: list[dict] = []

    def transport(method, path, headers, payload, timeout):
        requests.append({"method": method, "path": path, "headers": headers,
                         "payload": payload, "timeout": timeout})
        health = _strict_health()
        health["enforcement"]["network"] = False
        return health

    provider = providers.RemoteStrictExecutionProvider(
        client_platform="macos", base_url="https://strict.example",
        authorization="Bearer top-secret", transport=transport,
    )
    result = provider.execute(providers.ComputationSpec(
        "python3", 'print("must-not-leave-client")', no_net=True
    ))

    check("incomplete strict handshake is rejected", result["ok"] is False)
    check("failed attestation has a stable code",
          result["provider_error"] == "strict_attestation_failed")
    check("payload is never submitted after failed attestation",
          len(requests) == 1 and requests[0]["path"] == "/v1/health"
          and requests[0]["payload"] is None)
    check("credentials are redacted from attestation errors",
          "top-secret" not in json.dumps(result))


def test_remote_strict_managed_run_preserves_identity_and_receipt() -> None:
    requests: list[dict] = []

    def transport(method, path, headers, payload, timeout):
        requests.append({"method": method, "path": path, "headers": headers,
                         "payload": payload, "timeout": timeout})
        if path == "/v1/health":
            return _strict_health()
        if path.endswith("/cancel"):
            return {"ok": True, "cancelled": True}
        if method == "DELETE":
            return {"ok": True, "cleaned": True}
        return {
            "ok": True, "verdict": "OK", "stdout": "remote\n", "stderr": "",
            "exit_code": 0, "unenforced": [],
            "enforcement": dict.fromkeys(providers.STRICT_CONTROLS, True),
        }

    provider = providers.RemoteStrictExecutionProvider(
        client_platform="macos", base_url="https://strict.example",
        authorization="Bearer top-secret", transport=transport,
    )
    result = provider.execute_managed(
        "run-123", providers.ComputationSpec("python3", "print('remote')", no_net=True)
    )
    provider.cancel("run-123")
    provider.cleanup("run-123")

    check("managed run uses supervisor identity",
          requests[1]["path"] == "/v1/runs/run-123/execute")
    check("authorization is scoped to request headers",
          all(row["headers"] == {"Authorization": "Bearer top-secret"}
              for row in requests)
          and "top-secret" not in json.dumps(provider.describe()))
    check("remote result carries verified strict receipt",
          result["strict_receipt"]["verified"] is True
          and set(result["strict_receipt"]["controls"])
          == set(providers.STRICT_CONTROLS))
    check("cancel routes to the owned remote run",
          requests[2]["path"] == "/v1/runs/run-123/cancel")
    check("cleanup routes to the owned remote run",
          requests[3]["method"] == "DELETE"
          and requests[3]["path"] == "/v1/runs/run-123")


def test_configured_registry_replaces_unavailable_stub_with_remote() -> None:
    registry = providers.configured_registry({
        providers.STRICT_URL_ENV: "https://strict.example",
        providers.STRICT_AUTHORIZATION_ENV: "Bearer registry-secret",
    }, strict_transport=lambda *_args: _strict_health())
    provider_id = f"{providers.strict_host_platform()}-strict"
    selected = registry.select(provider_id)

    check("configured strict URL activates remote provider",
          isinstance(selected, providers.RemoteStrictExecutionProvider))
    serialized = json.dumps(registry.descriptors())
    check("registry never publishes strict endpoint or credentials",
          "strict.example" not in serialized and "registry-secret" not in serialized)


def test_remote_strict_requires_authentication_before_network() -> None:
    requests: list[object] = []
    provider = providers.RemoteStrictExecutionProvider(
        client_platform="macos", base_url="https://strict.example",
        transport=lambda *args: requests.append(args),
    )
    result = provider.execute(providers.ComputationSpec("python3", "secret source"))
    check("missing strict authentication fails closed",
          result["provider_error"] == "strict_authentication_required")
    check("missing authentication makes no network request", requests == [])


def test_remote_strict_rejects_plaintext_nonloopback_endpoint() -> None:
    try:
        providers.RemoteStrictExecutionProvider(
            client_platform="macos", base_url="http://strict.example",
            authorization="Bearer secret",
        )
    except ValueError as exc:
        check("remote strict rejects plaintext endpoint",
              "HTTPS" in str(exc))
    else:
        check("remote strict rejects plaintext endpoint", False)


def test_execution_service_supervises_and_cleans_remote_strict_run() -> None:
    requests: list[tuple[str, str]] = []

    def transport(method, path, headers, payload, timeout):
        del headers, payload, timeout
        requests.append((method, path))
        if path == "/v1/health":
            return _strict_health()
        if method == "DELETE":
            return {"ok": True}
        return {
            "ok": True, "verdict": "OK", "stdout": "managed\n", "stderr": "",
            "exit_code": 0, "unenforced": [],
            "enforcement": dict.fromkeys(providers.STRICT_CONTROLS, True),
        }

    provider = providers.RemoteStrictExecutionProvider(
        client_platform="macos", base_url="https://strict.example",
        authorization="Bearer secret", transport=transport,
    )
    registry = providers.ProviderRegistry(default_provider_id="macos-strict")
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-service-runs-") as root:
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        service = execution_service.ExecutionService(registry, supervisor=supervisor)
        result = service.execute(
            providers.ComputationSpec("python3", "print('managed')"),
            provider_id="macos-strict",
        )

    execute_paths = [path for method, path in requests if method == "POST"]
    run_path = execute_paths[0]
    run_id = run_path.split("/")[3]
    check("service uses managed remote execution path",
          run_path == f"/v1/runs/{run_id}/execute")
    check("service cleans the exact managed run",
          ("DELETE", f"/v1/runs/{run_id}") in requests)
    check("managed service result keeps strict receipt",
          result["strict_receipt"]["verified"] is True)


def test_execution_service_reports_cleanup_failure_without_secret() -> None:
    def transport(method, path, headers, payload, timeout):
        del headers, payload, timeout
        if path == "/v1/health":
            return _strict_health()
        if method == "DELETE":
            raise OSError("Bearer cleanup-secret")
        return {
            "ok": True, "verdict": "OK", "stdout": "", "stderr": "",
            "exit_code": 0, "unenforced": [],
            "enforcement": dict.fromkeys(providers.STRICT_CONTROLS, True),
        }

    provider = providers.RemoteStrictExecutionProvider(
        client_platform="macos", base_url="https://strict.example",
        authorization="Bearer cleanup-secret", transport=transport,
    )
    registry = providers.ProviderRegistry(default_provider_id="macos-strict")
    registry.register(provider)
    with tempfile.TemporaryDirectory(prefix="codecalc-service-runs-") as root:
        supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        service = execution_service.ExecutionService(registry, supervisor=supervisor)
        result = service.execute(providers.ComputationSpec("python3", "pass"))

    check("cleanup failure is a normalized provider error",
          result["ok"] is False
          and result["provider_error"] == "provider_cleanup_failed")
    check("cleanup failure redacts strict credential",
          "cleanup-secret" not in json.dumps(result))


def test_remote_strict_rejects_client_workdir_before_network() -> None:
    requests: list[object] = []
    provider = providers.RemoteStrictExecutionProvider(
        client_platform="macos", base_url="https://strict.example",
        authorization="Bearer secret",
        transport=lambda *args: requests.append(args),
    )
    try:
        provider.execute(providers.ComputationSpec(
            "python3", "pass", workdir="/client/workspace"
        ))
    except providers.UnsupportedCapability as exc:
        check("remote strict rejects client-local workdir", exc.capability == "workdir")
    else:
        check("remote strict rejects client-local workdir", False)
    check("workdir rejection makes no remote request", requests == [])


def test_unauthenticated_cleanup_makes_no_request() -> None:
    requests: list[object] = []
    provider = providers.RemoteStrictExecutionProvider(
        client_platform="macos", base_url="https://strict.example",
        transport=lambda *args: requests.append(args),
    )
    try:
        provider.cleanup("run")
    except providers.ProviderOperationFailure as exc:
        check("unauthenticated cleanup fails explicitly", exc.code == "provider_cleanup_failed")
    else:
        check("unauthenticated cleanup fails explicitly", False)
    check("unauthenticated cleanup makes no request", requests == [])


def test_remote_inspection_redacts_credentials() -> None:
    provider = providers.RemoteStrictExecutionProvider(
        client_platform="macos", base_url="https://strict.example",
        authorization="Bearer inspect-secret",
        transport=lambda *_args: {"detail": "Bearer inspect-secret"},
    )
    inspected = provider.inspect("run")
    check("remote inspection redacts credentials",
          "inspect-secret" not in json.dumps(inspected)
          and inspected["detail"] == "[REDACTED]")


if __name__ == "__main__":
    test_host_strict_provider_is_discoverable_and_fails_closed()
    test_local_provider_is_explicitly_non_strict()
    test_doctor_reports_strict_provider_readiness()
    test_remote_strict_rejects_incomplete_handshake_before_payload()
    test_remote_strict_managed_run_preserves_identity_and_receipt()
    test_configured_registry_replaces_unavailable_stub_with_remote()
    test_remote_strict_requires_authentication_before_network()
    test_remote_strict_rejects_plaintext_nonloopback_endpoint()
    test_execution_service_supervises_and_cleans_remote_strict_run()
    test_execution_service_reports_cleanup_failure_without_secret()
    test_remote_strict_rejects_client_workdir_before_network()
    test_unauthenticated_cleanup_makes_no_request()
    test_remote_inspection_redacts_credentials()
    sys.exit(1 if FAILS else 0)
