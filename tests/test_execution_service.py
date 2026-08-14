"""Protocol-neutral execution service and MCP adapter parity (THE-790)."""

from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path

from mcp.types import ImageContent

from codecalc import contract, execution_service, providers, server, sessions

FAILS: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name}")
    if not condition:
        FAILS.append(name)


def _service() -> execution_service.ExecutionService:
    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(providers.LocalExecutionProvider())
    return execution_service.ExecutionService(registry)


def test_service_routes_a_canonical_spec_and_records_provider_identity() -> None:
    result = _service().execute(
        providers.ComputationSpec(language="python3", code='print("service")')
    )

    check("service executes successfully", result["ok"] is True)
    check("service preserves provider stdout", result["stdout"].strip() == "service")
    check("service preserves the result contract",
          result["contract_version"] == contract.CONTRACT_VERSION)
    check("service receipt identifies the provider",
          result["provider"]["provider_id"] == "local")
    check("service receipt identifies the provider interface",
          result["provider"]["interface_version"] == providers.PROVIDER_INTERFACE_VERSION)


def test_service_rejects_an_unknown_provider_without_falling_back() -> None:
    result = _service().execute(
        providers.ComputationSpec(language="python3", code='print("must not run")'),
        provider_id="missing",
    )

    check("unknown provider is rejected", result["ok"] is False)
    check("unknown provider is a request validation error", result["code"] == "validation")
    check("unknown provider keeps its provider-specific code",
          result["provider_error"] == "unknown_provider")
    check("unknown-provider errors carry the result contract",
          result["contract_version"] == contract.CONTRACT_VERSION)


def test_mcp_adapter_accepts_explicit_local_provider_selection() -> None:
    result = server.execute_code("python3", 'print("adapter")', provider="local")

    check("MCP adapter accepts explicit local selection", result["ok"] is True)
    check("MCP adapter preserves execution output", result["stdout"].strip() == "adapter")
    check("MCP adapter returns provider identity",
          result["provider"]["provider_id"] == "local")


def test_compact_results_keep_provider_identity() -> None:
    result = server.execute_code(
        "python3", 'print("compact")', provider="local", compact=True
    )

    check("compact execution succeeds", result["ok"] is True)
    check("compact result keeps provider identity",
          result.get("provider", {}).get("provider_id") == "local")


def test_mcp_adapter_publishes_provider_descriptors() -> None:
    descriptors = server.list_execution_providers()

    check("provider discovery returns a list", isinstance(descriptors, list))
    check("provider discovery exposes the local provider",
          [item["provider_id"] for item in descriptors] == ["local"])
    check("provider discovery exposes machine-readable capabilities",
          descriptors[0]["capabilities"]["execute"] is True)


def test_service_applies_policy_routing_when_selection_is_not_explicit() -> None:
    class RemoteProvider(providers.LocalExecutionProvider):
        provider_id = "remote"

        def describe(self) -> dict:
            descriptor = super().describe()
            descriptor["provider_id"] = self.provider_id
            return descriptor

    class RemotePolicy:
        def select_provider(self, spec: providers.ComputationSpec,
                            descriptors: list[dict]) -> str | None:
            return "remote"

    registry = providers.ProviderRegistry(
        default_provider_id="local", policy=RemotePolicy()
    )
    registry.register(providers.LocalExecutionProvider())
    registry.register(RemoteProvider())
    service = execution_service.ExecutionService(registry)

    result = service.execute(
        providers.ComputationSpec(language="python3", code='print("policy")')
    )

    check("service uses the provider selected by policy",
          result["provider"]["provider_id"] == "remote")


def test_service_verifies_one_workload_independently_across_two_providers() -> None:
    class RecordingProvider(providers.LocalExecutionProvider):
        def __init__(self, provider_id: str, stdout: str) -> None:
            self.provider_id = provider_id
            self.stdout = stdout
            self.calls: list[providers.ComputationSpec] = []

        def describe(self) -> dict:
            descriptor = super().describe()
            descriptor["provider_id"] = self.provider_id
            return descriptor

        def execute(self, spec: providers.ComputationSpec) -> dict:
            self.calls.append(spec)
            return contract.stamp({
                "ok": True,
                "verdict": "OK",
                "stdout": self.stdout,
                "stderr": "",
                "exit_code": 0,
            })

    left = RecordingProvider("left", "same\n")
    right = RecordingProvider("right", "same\n")
    registry = providers.ProviderRegistry(default_provider_id="left")
    registry.register(left)
    registry.register(right)
    service = execution_service.ExecutionService(registry)
    spec = providers.ComputationSpec(language="python3", code='print("same")')

    result = service.verify_across_providers(spec, "left", "right")

    check("cross-provider verification reports agreement",
          result["agreement"] is True)
    check("cross-provider verification runs each provider once",
          left.calls == [spec] and right.calls == [spec])
    check("cross-provider verification keeps both receipts",
          [item["provider"]["provider_id"] for item in result["results"]]
          == ["left", "right"])
    check("cross-provider verification carries the result contract",
          result["contract_version"] == contract.CONTRACT_VERSION)


def test_service_receipt_reports_requested_and_provider_enforced_limits() -> None:
    class LimitProvider(providers.LocalExecutionProvider):
        provider_id = "limits"

        def describe(self) -> dict:
            descriptor = super().describe()
            descriptor["provider_id"] = self.provider_id
            return descriptor

        def execute(self, spec: providers.ComputationSpec) -> dict:
            del spec
            return contract.stamp({
                "ok": True,
                "verdict": "OK",
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
                "unenforced": [
                    "max_output_kb_enforced_after_provider_response",
                    "no_net_unavailable_on_test_provider",
                ],
            })

    registry = providers.ProviderRegistry(default_provider_id="limits")
    registry.register(LimitProvider())
    service = execution_service.ExecutionService(registry)

    result = service.execute(providers.ComputationSpec(
        language="python3",
        code="pass",
        timeout=7,
        max_memory_mb=32,
        max_output_kb=4,
        max_cpu=2,
        no_net=True,
    ))
    limits = result["provider"]["limits"]

    check("receipt records every requested resource control",
          limits["requested"] == {
              "timeout_seconds": 7,
              "max_memory_mb": 32,
              "max_output_kb": 4,
              "max_cpu_seconds": 2,
              "no_net": True,
          })
    check("receipt names controls the provider reports as enforced",
          limits["provider_reported_enforced"] == [
              "timeout", "max_memory_mb", "max_cpu"
          ])
    check("receipt retains exact unenforced disclosures",
          limits["unenforced"] == [
              "max_output_kb_enforced_after_provider_response",
              "no_net_unavailable_on_test_provider",
          ])


def test_session_service_owns_protocol_neutral_lifecycle_and_artifacts() -> None:
    service_type = getattr(execution_service, "SessionService", None)
    check("protocol-neutral session service is available", service_type is not None)
    if service_type is None:
        return

    old_root = sessions.SESSION_ROOT
    with tempfile.TemporaryDirectory(prefix="codecalc-session-service-") as root:
        sessions.SESSION_ROOT = Path(root)
        try:
            service = service_type()
            started = service.start("bash")
            session_id = started["session_id"]
            written = service.write_file(session_id, "data/value.txt", "42")
            files = service.list_files(session_id, "data")
            artifacts = service.artifacts(session_id)
            active = service.list_sessions()
            stopped = service.stop(session_id)
        finally:
            sessions.SESSION_ROOT = old_root

    check("session service starts a real workspace session",
          started["ok"] is True and started["stateful"] is False)
    check("session service writes within the workspace", written["ok"] is True)
    check("session service lists workspace files",
          files["ok"] is True and files["files"][0]["path"] == "value.txt")
    check("session service exposes generated artifacts",
          artifacts["artifacts"] == [{"path": "data/value.txt", "size": 2}])
    check("session service lists active sessions",
          active["sessions"][0]["session_id"] == session_id)
    check("session service cleans up its workspace",
          stopped["ok"] is True and stopped["deleted"] is True)


def test_session_service_reads_bounded_files_and_runs_workspace_entries() -> None:
    service_type = getattr(execution_service, "SessionService", None)
    check("session service supports workspace reads and runs", service_type is not None)
    if service_type is None:
        return

    old_root = sessions.SESSION_ROOT
    with tempfile.TemporaryDirectory(prefix="codecalc-session-read-run-") as root:
        sessions.SESSION_ROOT = Path(root)
        try:
            service = service_type()
            started = service.start("bash")
            session_id = started["session_id"]
            service.write_file(session_id, "message.txt", "abcdef")
            service.write_file(session_id, "main.sh", 'printf "workspace-run\\n"')
            read = service.read_file(session_id, "message.txt", max_bytes=3)
            run = service.run_file(session_id, "main.sh", timeout=10)
            stopped = service.stop(session_id)
        finally:
            sessions.SESSION_ROOT = old_root

    check("session service returns a protocol-neutral bounded read", read == {
        "ok": True,
        "path": "message.txt",
        "size": 6,
        "content": "abc",
        "content_bytes": b"abcdef",
        "mime_type": "application/octet-stream",
        "is_image": False,
        "truncated": True,
        "resource": f"codecalc://session/{session_id}/files/message.txt",
    })
    check("session service infers and runs a workspace entry",
          run["ok"] is True and run["stdout"] == "workspace-run\n")
    check("workspace run reports its canonical entry and language",
          run["entry_file"] == "main.sh" and run["language"] == "bash")
    check("read/run test cleans up its workspace", stopped["deleted"] is True)


def test_mcp_session_adapters_delegate_to_the_shared_service() -> None:
    calls: list[tuple] = []

    class RecordingSessionService:
        def start(self, language: str = "python3") -> dict:
            calls.append(("start", language))
            return {"operation": "start"}

        def stop(self, session_id: str) -> dict:
            calls.append(("stop", session_id))
            return {"operation": "stop"}

        def list_sessions(self) -> dict:
            calls.append(("list",))
            return {"operation": "list"}

        def list_files(self, session_id: str, path: str = "") -> dict:
            calls.append(("files", session_id, path))
            return {"operation": "files"}

        def write_file(self, session_id: str, path: str, content: str) -> dict:
            calls.append(("write", session_id, path, content))
            return {"operation": "write"}

        def artifacts(self, session_id: str) -> dict:
            calls.append(("artifacts", session_id))
            return {"operation": "artifacts"}

        def read_file(self, session_id: str, path: str,
                      max_bytes: int = 65536, *, as_image: bool = False) -> dict:
            calls.append(("read", session_id, path, max_bytes, as_image))
            return {
                "operation": "read",
                "ok": True,
                "path": path,
                "size": 3,
                "content": "abc",
                "content_bytes": b"abc",
                "mime_type": "application/octet-stream",
                "is_image": False,
                "truncated": False,
                "resource": f"codecalc://session/{session_id}/files/{path}",
            }

        def run_file(self, session_id: str, entry_file: str,
                     language: str | None = None, stdin: str = "",
                     timeout: int = 30) -> dict:
            calls.append(("run", session_id, entry_file, language, stdin, timeout))
            return {"operation": "run"}

    old_service = getattr(server, "_session_service", None)
    check("MCP server owns a shared session service", old_service is not None)
    if old_service is None:
        return
    server._session_service = RecordingSessionService()
    try:
        results = [
            server.session_start("bash"),
            server.session_stop("sid"),
            server.session_list(),
            server.session_files("sid", "data"),
            server.session_write_file("sid", "data/x", "value"),
            server.session_artifacts("sid"),
            server.session_read_file("sid", "data/x", 7),
            server.session_file_resource("sid", "data/x"),
            server.session_run("sid", "main.py", "python3", "input", 9),
        ]
    finally:
        server._session_service = old_service

    check("MCP session adapters preserve service results",
          [result if isinstance(result, str) else result["operation"]
           for result in results]
          == ["start", "stop", "list", "files", "write", "artifacts",
              "read", "abc", "run"])
    check("MCP session adapters preserve canonical arguments", calls == [
        ("start", "bash"),
        ("stop", "sid"),
        ("list",),
        ("files", "sid", "data"),
        ("write", "sid", "data/x", "value"),
        ("artifacts", "sid"),
        ("read", "sid", "data/x", 7, False),
        ("read", "sid", "data/x", 4 * 1024 * 1024, False),
        ("run", "sid", "main.py", "python3", "input", 9),
    ])


def test_mcp_read_adapter_alone_converts_neutral_bytes_to_image_content() -> None:
    class ImageSessionService:
        def read_file(self, session_id: str, path: str,
                      max_bytes: int = 65536, *, as_image: bool = False) -> dict:
            del session_id, max_bytes, as_image
            return {
                "ok": True,
                "path": path,
                "size": 4,
                "content": "",
                "content_bytes": b"\x89PNG",
                "mime_type": "image/png",
                "is_image": True,
                "truncated": False,
                "resource": f"codecalc://session/sid/files/{path}",
            }

    old_service = server._session_service
    server._session_service = ImageSessionService()
    try:
        result = server.session_read_file("sid", "plot.png")
    finally:
        server._session_service = old_service

    check("image conversion remains an MCP adapter concern",
          isinstance(result, ImageContent))
    check("image adapter preserves bytes and MIME type",
          result.data == base64.b64encode(b"\x89PNG").decode("ascii")
          and result.mime_type == "image/png")


def test_mcp_execute_adapter_routes_session_execution_through_the_service() -> None:
    calls: list[tuple] = []

    class RecordingSessionService:
        def execute(self, session_id: str, spec: providers.ComputationSpec) -> dict:
            calls.append((session_id, spec))
            return contract.stamp({
                "ok": True,
                "verdict": "OK",
                "stdout": "session service\n",
                "stderr": "",
                "exit_code": 0,
            })

    old_service = server._session_service
    server._session_service = RecordingSessionService()
    try:
        result = server.execute_code(
            "python3",
            'print("session service")',
            stdin="input",
            timeout=200,
            session_id="sid",
            max_memory_mb=32,
            max_output_kb=4,
            max_cpu=2,
            no_net=True,
        )
    finally:
        server._session_service = old_service

    check("session execution adapter returns the shared service result",
          result.get("stdout") == "session service\n")
    check("session execution adapter delegates to the shared service", bool(calls))
    if not calls:
        return
    session_id, spec = calls[0]
    check("session execution adapter preserves the session handle",
          session_id == "sid")
    check("session execution adapter compiles a canonical bounded spec",
          spec == providers.ComputationSpec(
              language="python3",
              code='print("session service")',
              stdin="input",
              timeout=120,
              max_memory_mb=32,
              max_output_kb=4,
              max_cpu=2,
              no_net=True,
          ))


if __name__ == "__main__":
    test_service_routes_a_canonical_spec_and_records_provider_identity()
    test_service_rejects_an_unknown_provider_without_falling_back()
    test_mcp_adapter_accepts_explicit_local_provider_selection()
    test_compact_results_keep_provider_identity()
    test_mcp_adapter_publishes_provider_descriptors()
    test_service_applies_policy_routing_when_selection_is_not_explicit()
    test_service_verifies_one_workload_independently_across_two_providers()
    test_service_receipt_reports_requested_and_provider_enforced_limits()
    test_session_service_owns_protocol_neutral_lifecycle_and_artifacts()
    test_session_service_reads_bounded_files_and_runs_workspace_entries()
    test_mcp_session_adapters_delegate_to_the_shared_service()
    test_mcp_read_adapter_alone_converts_neutral_bytes_to_image_content()
    test_mcp_execute_adapter_routes_session_execution_through_the_service()
    sys.exit(1 if FAILS else 0)
