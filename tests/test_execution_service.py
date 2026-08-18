"""Protocol-neutral execution service and MCP adapter parity (THE-790)."""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from mcp.types import ImageContent

from codecalc import contract, execution_service, executor, providers, run_supervisor, server, sessions

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
    provider_ids = [item["provider_id"] for item in descriptors]
    check("provider discovery exposes local and host-strict providers",
          "local" in provider_ids
          and f"{providers.strict_host_platform()}-strict" in provider_ids)
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


def test_service_routes_streaming_through_the_selected_provider() -> None:
    progress: list[tuple[int, str]] = []

    class StreamingProvider(providers.LocalExecutionProvider):
        provider_id = "streaming"

        def describe(self) -> dict:
            descriptor = super().describe()
            descriptor["provider_id"] = self.provider_id
            descriptor["capabilities"]["stream"] = True
            return descriptor

        async def execute_stream(self, spec: providers.ComputationSpec,
                                 on_progress=None) -> dict:
            if on_progress is not None:
                await on_progress(4, "stdout so far: 4 bytes")
            return contract.stamp({
                "ok": True, "verdict": "OK", "stdout": spec.code,
                "stderr": "", "exit_code": 0, "unenforced": [],
            })

    async def record_progress(value: int, message: str) -> None:
        progress.append((value, message))

    registry = providers.ProviderRegistry(default_provider_id="streaming")
    registry.register(StreamingProvider())
    service = execution_service.ExecutionService(registry)
    spec = providers.ComputationSpec(language="python3", code="data")

    result = asyncio.run(service.execute_stream(spec, on_progress=record_progress))

    check("streaming routes through provider capability",
          result["provider"]["provider_id"] == "streaming")
    check("streaming preserves provider result contract",
          result["contract_version"] == contract.CONTRACT_VERSION)
    check("streaming forwards protocol-neutral progress",
          progress == [(4, "stdout so far: 4 bytes")])


def test_service_rejects_unsupported_streaming_without_fallback() -> None:
    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(providers.LocalExecutionProvider())
    registry.register(providers.PistonExecutionProvider(
        base_url="http://piston.test", transport=lambda *_args: {}
    ))
    service = execution_service.ExecutionService(registry)

    result = asyncio.run(service.execute_stream(
        providers.ComputationSpec(language="python3", code="pass"),
        provider_id="piston",
    ))

    check("unsupported streaming is a normalized rejection", result["ok"] is False)
    check("unsupported streaming identifies provider and capability",
          result["provider_error"] == "unsupported_capability"
          and result["requested_provider"] == "piston"
          and result["capability"] == "stream")


def test_service_normalizes_unsupported_synchronous_capabilities() -> None:
    registry = providers.ProviderRegistry(default_provider_id="piston")
    registry.register(providers.PistonExecutionProvider(
        base_url="http://piston.test", transport=lambda *_args: {}
    ))
    service = execution_service.ExecutionService(registry)

    result = service.execute(providers.ComputationSpec(
        language="python3", code="pass", no_net=True
    ))

    check("unsupported synchronous capability is a normalized rejection",
          result["ok"] is False and result["code"] == "validation")
    check("synchronous rejection identifies provider and capability",
          result["provider_error"] == "unsupported_capability"
          and result["requested_provider"] == "piston"
          and result["capability"] == "network_control")


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


def test_session_file_pagination_is_shared_and_cursor_based() -> None:
    old_root = sessions.SESSION_ROOT
    with tempfile.TemporaryDirectory(prefix="codecalc-session-pages-") as root:
        sessions.SESSION_ROOT = Path(root)
        try:
            service = execution_service.SessionService()
            session_id = service.start("bash")["session_id"]
            for name in ("a.txt", "b.txt", "c.txt"):
                service.write_file(session_id, name, name)
            first = service.list_files(session_id, page_size=2)
            second = service.list_files(
                session_id, page_size=2, cursor=first["next_cursor"]
            )
            service.stop(session_id)
        finally:
            sessions.SESSION_ROOT = old_root

    check("first file page is bounded and publishes a cursor",
          [item["path"] for item in first["files"]] == ["a.txt", "b.txt"]
          and first["next_cursor"] == "2")
    check("next file page resumes without overlap",
          [item["path"] for item in second["files"]] == ["c.txt"]
          and second["next_cursor"] is None)


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

        def list_files(self, session_id: str, path: str = "", *,
                       page_size: int | None = None,
                       cursor: str | None = None) -> dict:
            calls.append(("files", session_id, path, page_size, cursor))
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
            server.session_files("sid", "data", 10, "20"),
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
        ("files", "sid", "data", 10, "20"),
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
    check("session execution receipt identifies the selected provider",
          result.get("provider", {}).get("provider_id") == "local")
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


def test_mcp_session_execution_rejects_a_nonlocal_provider() -> None:
    calls: list[tuple] = []

    class RecordingSessionService:
        def execute(self, session_id: str, spec: providers.ComputationSpec) -> dict:
            calls.append((session_id, spec))
            return {"ok": True}

    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(providers.LocalExecutionProvider())
    registry.register(providers.PistonExecutionProvider(
        base_url="http://piston.test", transport=lambda *_args: {}
    ))
    old_execution = server._execution_service
    old_session = server._session_service
    server._execution_service = execution_service.ExecutionService(registry)
    server._session_service = RecordingSessionService()
    try:
        result = server.execute_code(
            "python3", "pass", session_id="sid", provider="piston"
        )
    finally:
        server._execution_service = old_execution
        server._session_service = old_session

    check("nonlocal session selection is rejected explicitly",
          result["ok"] is False
          and result["provider_error"] == "unsupported_capability"
          and result["capability"] == "sessions")
    check("nonlocal session selection never executes locally", calls == [])


def test_mcp_stream_adapter_compiles_spec_and_delegates_progress() -> None:
    calls: list[tuple] = []

    class RecordingExecutionService:
        async def execute_stream(self, spec: providers.ComputationSpec, *,
                                 provider_id=None, on_progress=None) -> dict:
            calls.append((spec, provider_id))
            if on_progress is not None:
                await on_progress(3, "three")
            return {"ok": True, "streamed": True}

    class RecordingContext:
        async def report_progress(self, *, progress: float, total, message: str) -> None:
            calls.append((progress, total, message))

    old_service = server._execution_service
    server._execution_service = RecordingExecutionService()
    try:
        result = asyncio.run(server.execute_code_stream(
            "python3", "print(1)", stdin="in", timeout=999,
            max_memory_mb=32, max_output_kb=4, max_cpu=2, no_net=True,
            provider="local", ctx=RecordingContext(),
        ))
    finally:
        server._execution_service = old_service

    check("stream adapter returns service result", result["streamed"] is True)
    check("stream adapter compiles a bounded canonical spec", calls[0] == (
        providers.ComputationSpec(
            language="python3", code="print(1)", stdin="in", timeout=300,
            max_memory_mb=32, max_output_kb=4, max_cpu=2, no_net=True,
        ),
        "local",
    ))
    check("stream adapter translates progress only at MCP boundary",
          calls[1] == (3.0, None, "three"))


def test_main_runs_the_same_server_over_explicit_streamable_http() -> None:
    calls: list[dict] = []

    class RecordingMCPServer:
        def run(self, **kwargs) -> None:
            calls.append(kwargs)

    old_mcp = server.mcp
    old_argv = sys.argv
    server.mcp = RecordingMCPServer()
    sys.argv = ["codecalc", "serve-http", "--host", "127.0.0.2", "--port", "8123"]
    try:
        server.main()
    finally:
        sys.argv = old_argv
        server.mcp = old_mcp

    check("HTTP uses the same registered MCP server object",
          calls == [{
              "transport": "streamable-http",
              "host": "127.0.0.2",
              "port": 8123,
              "json_response": True,
              "stateless_http": True,
          }])


def test_serve_http_refuses_a_non_loopback_bind_without_a_token() -> None:
    """THE-786 residual: fail CLOSED. serve-http on a non-loopback host with no
    CODECALC_HTTP_TOKEN would expose 51 unauthenticated code-execution tools to
    whatever network the interface reaches. Refusing to start is the only
    answer that cannot be misconfigured into an open server."""
    calls: list[dict] = []

    class RecordingMCPServer:
        def run(self, **kwargs) -> None:
            calls.append(kwargs)

    old_mcp, old_argv = server.mcp, sys.argv
    old_token = os.environ.pop(server.HTTP_TOKEN_ENV, None)
    server.mcp = RecordingMCPServer()
    sys.argv = ["codecalc", "serve-http", "--host", "0.0.0.0", "--port", "8123"]  # noqa: S104 -- the refused bind IS the subject
    try:
        raised = None
        try:
            server.main()
        except SystemExit as exc:
            raised = exc.code
    finally:
        sys.argv = old_argv
        server.mcp = old_mcp
        if old_token is not None:
            os.environ[server.HTTP_TOKEN_ENV] = old_token
    check("non-loopback bind without a token refuses to start",
          raised not in (None, 0))
    check("the refused server never ran", calls == [])


def test_serve_http_allows_non_loopback_with_a_token_and_loopback_range_without() -> None:
    calls: list[dict] = []

    class RecordingMCPServer:
        def run(self, **kwargs) -> None:
            calls.append(kwargs)

    old_mcp, old_argv = server.mcp, sys.argv
    server.mcp = RecordingMCPServer()
    try:
        os.environ[server.HTTP_TOKEN_ENV] = "test-token-value"
        sys.argv = ["codecalc", "serve-http", "--host", "0.0.0.0", "--port", "8123"]  # noqa: S104 -- the refused bind IS the subject
        server.main()
        check("non-loopback bind with a token starts", len(calls) == 1)
    finally:
        sys.argv = old_argv
        server.mcp = old_mcp
        os.environ.pop(server.HTTP_TOKEN_ENV, None)
    # 127.0.0.2 is loopback (the whole 127/8 block is), so the earlier test in
    # this file must keep passing without a token — a string comparison against
    # "127.0.0.1" would have broken it. This is asserted there, not here.


def test_http_auth_verifier_accepts_only_the_exact_token() -> None:
    """The static verifier is OUR code (the SDK's bearer middleware is not):
    wrong token -> None, right token -> an AccessToken carrying the scope."""
    os.environ[server.HTTP_TOKEN_ENV] = "sekrit"
    try:
        wired = server._http_auth()
        check("auth wiring present when the token is set",
              set(wired) == {"token_verifier", "auth"})
        verifier = wired["token_verifier"]
        good = asyncio.run(verifier.verify_token("sekrit"))
        bad = asyncio.run(verifier.verify_token("not-it"))
        prefix = asyncio.run(verifier.verify_token("sekrit "))
        check("the exact token verifies", good is not None and "codecalc" in good.scopes)
        check("a wrong token is rejected", bad is None)
        check("a near-miss token is rejected", prefix is None)
    finally:
        os.environ.pop(server.HTTP_TOKEN_ENV, None)
    check("no token means no auth wiring", server._http_auth() == {})


def test_http_auth_is_wired_into_the_server_at_import_when_the_token_is_set() -> None:
    """The verifier only protects anything if the CONSTRUCTED server carries
    it. A subprocess import with the env set is the only honest probe: this
    process's `server.mcp` was already built without one."""
    proc = subprocess.run(
        [sys.executable, "-c",
         ("import codecalc.server as s, sys; "
          "sys.exit(0 if s.mcp._token_verifier is not None else 3)")],
        env={**os.environ, server.HTTP_TOKEN_ENV: "probe-token"},
        capture_output=True, timeout=120,
    )
    check("a token in the environment wires the verifier into the server",
          proc.returncode == 0)


# ── THE-778: run_submit / run_inspect / run_cancel ──────────────────────────

class _FakeRunProvider(providers.LocalExecutionProvider):
    """A provider that finishes fast and deterministically, so run_submit/
    run_inspect tests do not depend on the real sandbox or on timing."""

    provider_id = "fake-run"

    def describe(self) -> dict:
        result = super().describe()
        result["provider_id"] = self.provider_id
        return result

    def execute(self, spec: providers.ComputationSpec) -> dict:
        return contract.stamp({
            "ok": True, "verdict": "OK", "stdout": spec.code,
            "stderr": "", "exit_code": 0, "unenforced": [],
        })


class _FakeCancellableProvider(_FakeRunProvider):
    """Like _FakeRunProvider, but blocks until cancelled and DOES advertise
    `cancel`/`cleanup` — the branch a provider like RemoteStrictExecution-
    Provider takes, as opposed to the built-in `local` provider."""

    provider_id = "fake-cancellable"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cancelled: list[str] = []

    def describe(self) -> dict:
        result = super().describe()
        result["capabilities"]["cancel"] = True
        result["capabilities"]["cleanup"] = True
        return result

    def execute(self, spec: providers.ComputationSpec) -> dict:
        self.entered.set()
        self.release.wait(5)
        return contract.stamp({
            "ok": True, "verdict": "OK", "stdout": spec.code,
            "stderr": "", "exit_code": 0, "unenforced": [],
        })

    def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)
        self.release.set()

    def cleanup(self, run_id: str) -> None:
        pass


def _run_registry(*provs) -> providers.ProviderRegistry:
    registry = providers.ProviderRegistry(default_provider_id=provs[0].provider_id)
    for p in provs:
        registry.register(p)
    return registry


def test_run_submit_returns_a_run_id_immediately_and_run_inspect_polls_to_terminal() -> None:
    registry = _run_registry(_FakeRunProvider())
    old = server._run_supervisor
    with tempfile.TemporaryDirectory(prefix="codecalc-run-tools-") as root:
        server._run_supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        try:
            submitted = server.run_submit("python3", "collected-output")
            check("run_submit succeeds and returns a run_id",
                  submitted.get("ok") is True and bool(submitted.get("run_id")))
            check("run_submit reports the selected provider and a running state",
                  submitted.get("provider_id") == "fake-run" and submitted.get("state") == "running")
            run_id = submitted.get("run_id", "")

            terminal = None
            for _ in range(100):
                inspected = server.run_inspect(run_id)
                if inspected.get("state") in {"finished", "cleaned"}:
                    terminal = inspected
                    break
                time.sleep(0.02)
            check("run_inspect eventually reaches a terminal state", terminal is not None)
            if terminal is not None:
                check("a terminal run_inspect carries the execute_code result shape",
                      terminal.get("stdout") == "collected-output"
                      and terminal.get("verdict") == "OK"
                      and terminal.get("ok") is True)
                check("a terminal run_inspect still carries its own run_id",
                      terminal.get("run_id") == run_id)
                again = server.run_inspect(run_id)
                check("a finished run stays inspectable on a second read (retention)",
                      again.get("stdout") == "collected-output")
        finally:
            server._run_supervisor = old


def test_run_inspect_reports_a_validation_error_for_an_unknown_run_id() -> None:
    registry = _run_registry(_FakeRunProvider())
    old = server._run_supervisor
    with tempfile.TemporaryDirectory(prefix="codecalc-run-tools-unknown-") as root:
        server._run_supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        try:
            result = server.run_inspect("does-not-exist")
            check("unknown run_id is a validation error, not a crash",
                  result.get("ok") is False and result.get("code") == "validation")
            check("unknown run_id names the provider_error",
                  result.get("provider_error") == "unknown_run")
        finally:
            server._run_supervisor = old


def test_run_cancel_is_idempotent_on_an_already_terminal_run() -> None:
    registry = _run_registry(_FakeRunProvider())
    old = server._run_supervisor
    with tempfile.TemporaryDirectory(prefix="codecalc-run-tools-cancel-terminal-") as root:
        server._run_supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        try:
            submitted = server.run_submit("python3", "x")
            run_id = submitted["run_id"]
            for _ in range(100):
                if server.run_inspect(run_id).get("state") == "finished":
                    break
                time.sleep(0.02)
            result = server.run_cancel(run_id)
            check("cancelling an already-finished run is a no-op, not an error",
                  result.get("ok") is True and result.get("cancelled") is False)
        finally:
            server._run_supervisor = old


class _FakeUncancellableRunningProvider(_FakeRunProvider):
    """Like _FakeRunProvider, but blocks until released — so a cancel races a
    REAL running state, not a terminal one — while still not advertising
    `cancel` (LocalExecutionProvider's own shape)."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, spec: providers.ComputationSpec) -> dict:
        self.entered.set()
        self.release.wait(5)
        return super().execute(spec)


def test_run_cancel_honestly_reports_a_provider_that_cannot_cancel() -> None:
    """The built-in `local` provider (LocalExecutionProvider) does not
    advertise `cancel` and its cancel() raises UnsupportedCapability. This
    must not escape run_cancel as an unhandled exception, and must not be
    reported as a fake success either — see run_cancel's own docstring."""
    provider = _FakeUncancellableRunningProvider()
    registry = _run_registry(provider)
    old = server._run_supervisor
    with tempfile.TemporaryDirectory(prefix="codecalc-run-tools-uncancellable-") as root:
        server._run_supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        try:
            submitted = server.run_submit("python3", "x", timeout=5)
            run_id = submitted["run_id"]
            provider.entered.wait(2)
            result = server.run_cancel(run_id)
            provider.release.set()
            check("cancelling an uncancellable provider is a validation error, not a crash",
                  result.get("ok") is False and result.get("code") == "validation")
            check("the error names the provider that could not cancel",
                  result.get("requested_provider") == provider.provider_id)
        finally:
            server._run_supervisor = old


def test_run_cancel_reaches_a_provider_that_supports_it() -> None:
    provider = _FakeCancellableProvider()
    registry = _run_registry(provider)
    old = server._run_supervisor
    with tempfile.TemporaryDirectory(prefix="codecalc-run-tools-cancellable-") as root:
        server._run_supervisor = run_supervisor.RunSupervisor(registry, state_dir=Path(root))
        try:
            submitted = server.run_submit("python3", "x", timeout=5)
            run_id = submitted["run_id"]
            provider.entered.wait(2)
            result = server.run_cancel(run_id)
            check("a provider that supports cancel reports cancelled=True",
                  result.get("ok") is True and result.get("cancelled") is True)
            check("the cancel reaches the owning provider",
                  provider.cancelled == [run_id])
        finally:
            server._run_supervisor = old


def test_run_tools_return_an_internal_error_when_no_supervisor_is_wired() -> None:
    old = server._run_supervisor
    server._run_supervisor = None
    try:
        submitted = server.run_submit("python3", "x")
        inspected = server.run_inspect("whatever")
        cancelled = server.run_cancel("whatever")
    finally:
        server._run_supervisor = old
    check("run_submit fails cleanly with no supervisor wired",
          submitted.get("ok") is False and submitted.get("code") == "internal")
    check("run_inspect fails cleanly with no supervisor wired",
          inspected.get("ok") is False and inspected.get("code") == "internal")
    check("run_cancel fails cleanly with no supervisor wired",
          cancelled.get("ok") is False and cancelled.get("code") == "internal")


def _real_local_registry() -> providers.ProviderRegistry:
    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(providers.LocalExecutionProvider())
    return registry


def test_run_cancel_on_the_real_local_provider_does_not_strand_the_result() -> None:
    """THE-778 fix round, review Critical #1 — the reviewer's own repro,
    against the ACTUAL LocalExecutionProvider (not a stand-in): submit ->
    run_cancel (gets the honest unsupported error) -> the run completes on
    its own -> run_inspect must reach a terminal state and return the FULL
    result, not poll "cancelling" forever. Reviewer's own observation was
    10 polls over 5s all reporting state=cancelling while
    _run_supervisor.wait() returned the finished result immediately —
    this exercises the same sequence end to end through the MCP tools."""
    old = server._run_supervisor
    with tempfile.TemporaryDirectory(prefix="codecalc-run-tools-real-local-") as root:
        server._run_supervisor = run_supervisor.RunSupervisor(
            _real_local_registry(), state_dir=Path(root)
        )
        try:
            submitted = server.run_submit("python3", "print('still here')", timeout=10)
            check("submission on the real local provider succeeds",
                  submitted.get("ok") is True)
            run_id = submitted.get("run_id", "")

            cancelled = server.run_cancel(run_id)
            check("run_cancel on the real local provider reports the honest "
                  "unsupported error",
                  cancelled.get("ok") is False
                  and cancelled.get("code") == "validation"
                  and cancelled.get("provider_error") == "unsupported_capability")

            terminal = None
            for _ in range(200):
                inspected = server.run_inspect(run_id)
                if inspected.get("state") in {"finished", "cleaned"}:
                    terminal = inspected
                    break
                time.sleep(0.02)
            check("run_inspect reaches a terminal state after a failed cancel "
                  "(not stranded at 'cancelling' forever)", terminal is not None)
            if terminal is not None:
                check("the terminal result is the run's real output, not stranded",
                      terminal.get("stdout", "").strip() == "still here"
                      and terminal.get("ok") is True)
        finally:
            server._run_supervisor = old


def test_run_inspect_terminal_result_has_the_same_keys_as_execute_code() -> None:
    """THE-778 fix round, review Important #2: run_submit bypasses
    ExecutionService.execute() — the only OTHER place the `provider`
    receipt was attached — so a terminal run_inspect result was missing it
    entirely. This is the gate that keeps the two surfaces from drifting
    apart again: same provider (the real local one, so both sides go
    through the identical executor.execute() call), same code, key sets
    compared modulo the documented run_* allowlist."""
    exec_result = server.execute_code("python3", "print('parity')")

    old = server._run_supervisor
    with tempfile.TemporaryDirectory(prefix="codecalc-run-tools-parity-") as root:
        server._run_supervisor = run_supervisor.RunSupervisor(
            _real_local_registry(), state_dir=Path(root)
        )
        try:
            submitted = server.run_submit("python3", "print('parity')")
            run_id = submitted.get("run_id", "")
            terminal = None
            for _ in range(200):
                inspected = server.run_inspect(run_id)
                if inspected.get("state") in {"finished", "cleaned"}:
                    terminal = inspected
                    break
                time.sleep(0.02)
            check("run_inspect reaches a terminal state for the parity check",
                  terminal is not None)
            if terminal is None:
                return
            missing = set(exec_result) - set(terminal)
            extra = set(terminal) - set(exec_result) - server._RUN_EXTRA_KEYS
            check("run_inspect terminal result carries every execute_code key",
                  not missing)
            check("run_inspect's only extra keys are the documented run_* allowlist",
                  not extra)
            check("run_inspect terminal result carries the provider receipt",
                  isinstance(terminal.get("provider"), dict)
                  and terminal["provider"].get("provider_id") == "local"
                  and "limits" in terminal["provider"])
        finally:
            server._run_supervisor = old


def test_run_submit_admission_cap_returns_resource_exhausted() -> None:
    """THE-778 fix round, review Important #3a: run_submit must refuse past
    CODECALC_MAX_ACTIVE_RUNS with a stable, tested error rather than
    growing the run table without bound."""
    provider = _FakeUncancellableRunningProvider()
    registry = _run_registry(provider)
    old = server._run_supervisor
    with tempfile.TemporaryDirectory(prefix="codecalc-run-tools-cap-") as root:
        server._run_supervisor = run_supervisor.RunSupervisor(
            registry, state_dir=Path(root), max_active_runs=1,
        )
        try:
            first = server.run_submit("python3", "x", timeout=5)
            check("the first submission, under the cap, succeeds",
                  first.get("ok") is True)
            provider.entered.wait(2)

            second = server.run_submit("python3", "y", timeout=5)
            check("a submission past the cap is a resource_exhausted error",
                  second.get("ok") is False
                  and second.get("code") == "resource_exhausted")
            check("the error reports the active count and the configured limit",
                  second.get("active_runs") == 1 and second.get("limit") == 1)

            provider.release.set()
            freed = None
            for _ in range(200):
                if server.run_inspect(first["run_id"]).get("state") in {"finished", "cleaned"}:
                    freed = True
                    break
                time.sleep(0.02)
            check("the first run reaches terminal so capacity frees up",
                  freed is True)

            third = server.run_submit("python3", "z", timeout=5)
            check("a submission after capacity frees up succeeds",
                  third.get("ok") is True)
        finally:
            server._run_supervisor = old


# ── THE-783: session output spills to a workspace artifact ─────────────────

def _spill_read(session_id: str, spill_uri: str) -> bytes:
    rel = spill_uri.split("/files/", 1)[1]
    data, _mime = sessions.resource_read(session_id, rel)
    return data


def test_session_output_spills_past_the_default_cap_instead_of_dropping_it() -> None:
    old_root = sessions.SESSION_ROOT
    with tempfile.TemporaryDirectory(prefix="codecalc-spill-") as root:
        sessions.SESSION_ROOT = Path(root)
        try:
            session_id = sessions.start("bash")["session_id"]
            # 100000 bytes of 'A', generated by bash itself — no nested-quote
            # surface with a second interpreter.
            code = "head -c 100000 /dev/zero | tr '\\0' 'A'"
            result = sessions.execute(session_id, code, language="bash")
            full = b"A" * 100_000

            check("oversized session output is reported truncated",
                  result.get("output_truncated") is True)
            marker = "\n…[truncated]"
            check("the inline stdout stays at the default cap plus the truncation marker",
                  len(result["stdout"]) == executor.MAX_OUTPUT_BYTES + len(marker))
            check("the inline stdout is a real PREFIX of the full output",
                  full.decode().startswith(result["stdout"][:-len(marker)]))
            spill_uri = result.get("stdout_spill")
            check("a spill resource URI is returned",
                  isinstance(spill_uri, str)
                  and spill_uri.startswith(f"codecalc://session/{session_id}/files/"))
            data = _spill_read(session_id, spill_uri) if spill_uri else None
            check("the spill file is byte-identical to the FULL output", data == full)

            below = sessions.execute(session_id, "echo below-threshold", language="bash")
            check("output at/under the cap is never spilled",
                  below.get("output_truncated") is False and "stdout_spill" not in below)

            explicit = sessions.execute(session_id, code, language="bash", max_output_kb=1)
            # <= 1024 + a short truncation-marker allowance — the exact
            # marker text is a per-backend detail (executor.py appends
            # "…[truncated]"; the Rust binary appends "...[truncated]", two
            # bytes longer) and is not what this assertion is about.
            check("an EXPLICIT max_output_kb caps output literally, with no spill",
                  "stdout_spill" not in explicit
                  and len(explicit["stdout"]) <= 1024 + 20)
        finally:
            sessions.SESSION_ROOT = old_root


def test_session_output_spill_is_faithful_to_the_pipelines_str_representation() -> None:
    """NOT "binary-safe" (THE-778/783 fix round, review Important #4):
    codecalc's WHOLE pipeline already decodes stdout/stderr with
    errors="replace" before spill_if_truncated's code ever sees a `str` —
    both backends do this (executor.py's `_trim`, the Rust binary's own
    JSON encoding), for every result, spill or not. So genuinely invalid
    UTF-8 is already lossy SYSTEM-WIDE, upstream of this feature entirely,
    and claiming the spill itself is "binary-safe" overstates what it can
    promise. What IS true, and asserted here explicitly in both directions:

      - a NUL byte (U+0000) IS valid UTF-8, so it round-trips byte-identical
        (this was the ONLY case the old, misleadingly-named version of this
        test checked — passing it proved NUL-safety, not binary safety);
      - a genuinely invalid byte (a lone 0xFF, not valid UTF-8 on its own)
        is NOT preserved — it comes back as U+FFFD, the same replacement
        character every other codecalc result already shows for it, not
        the original byte. Checked for loosely (replacement present,
        original byte absent) rather than an exact byte-for-byte match,
        because the exact BYTE COUNT `errors="replace"` produces for one
        invalid input byte is a decoder implementation detail that has
        already been seen to differ between the Rust and Python backends
        for TRUNCATION MARKER text in this same file — asserting an exact
        sequence here would pin that same kind of backend detail rather
        than the actual claim under test.
    """
    old_root = sessions.SESSION_ROOT
    with tempfile.TemporaryDirectory(prefix="codecalc-spill-fidelity-") as root:
        sessions.SESSION_ROOT = Path(root)
        try:
            session_id = sessions.start("bash")["session_id"]

            nul_code = (
                "head -c 70000 /dev/zero | tr '\\0' 'B'; "
                "printf '\\0'; "
                "head -c 10 /dev/zero | tr '\\0' 'C'"
            )
            nul_result = sessions.execute(session_id, nul_code, language="bash")
            nul_spill = nul_result.get("stdout_spill")
            check("a NUL byte still triggers a spill", isinstance(nul_spill, str))
            if nul_spill:
                data = _spill_read(session_id, nul_spill)
                expected = b"B" * 70_000 + b"\x00" + b"C" * 10
                check("a NUL byte (valid UTF-8) round-trips byte-identical",
                      data == expected)

            invalid_code = (
                "head -c 70000 /dev/zero | tr '\\0' 'B'; "
                "printf '\\xff'; "
                "head -c 10 /dev/zero | tr '\\0' 'C'"
            )
            invalid_result = sessions.execute(session_id, invalid_code, language="bash")
            invalid_spill = invalid_result.get("stdout_spill")
            check("invalid UTF-8 still triggers a spill (the stream is still oversized)",
                  isinstance(invalid_spill, str))
            if invalid_spill:
                data = _spill_read(session_id, invalid_spill)
                check("invalid UTF-8 is replaced with U+FFFD in the spill, matching "
                      "the pipeline's own str representation",
                      "�".encode() in data)
                check("the original 0xFF byte is genuinely gone — lossy upstream "
                      "of the spill, not something this feature can recover",
                      b"\xff" not in data)
                check("the surrounding bytes on both sides are otherwise intact",
                      data.startswith(b"B" * 70_000) and data.endswith(b"C" * 10))
        finally:
            sessions.SESSION_ROOT = old_root


def test_session_run_also_spills_oversized_output() -> None:
    """execution_service.SessionService.run_file() (the session_run MCP tool)
    calls executor.execute() directly rather than through sessions.execute(),
    so it needs its own wiring — this is that wiring's test."""
    old_root = sessions.SESSION_ROOT
    with tempfile.TemporaryDirectory(prefix="codecalc-spill-run-") as root:
        sessions.SESSION_ROOT = Path(root)
        try:
            service = execution_service.SessionService()
            session_id = service.start("bash")["session_id"]
            service.write_file(
                session_id, "main.sh",
                "head -c 100000 /dev/zero | tr '\\0' 'A'",
            )
            result = service.run_file(session_id, "main.sh", timeout=10)
            check("session_run spills oversized output the same way execute_code does",
                  result.get("output_truncated") is True
                  and isinstance(result.get("stdout_spill"), str))
            data = _spill_read(session_id, result["stdout_spill"])
            check("session_run's spill is byte-identical to the full output",
                  data == b"A" * 100_000)
        finally:
            sessions.SESSION_ROOT = old_root


def test_spill_files_are_retained_up_to_a_bounded_count() -> None:
    old_root = sessions.SESSION_ROOT
    old_retention = sessions._SPILL_RETENTION
    sessions._SPILL_RETENTION = 3
    with tempfile.TemporaryDirectory(prefix="codecalc-spill-retention-") as root:
        sessions.SESSION_ROOT = Path(root)
        try:
            session_id = sessions.start("bash")["session_id"]
            code = "head -c 100000 /dev/zero | tr '\\0' 'A'"
            for _ in range(6):
                sessions.execute(session_id, code, language="bash")
            spill_dir = sessions._session_dir(session_id) / sessions._SPILL_DIRNAME
            remaining = list(spill_dir.glob("*.bin"))
            check("spill retention prunes to the bounded count",
                  len(remaining) <= sessions._SPILL_RETENTION)
        finally:
            sessions.SESSION_ROOT = old_root
            sessions._SPILL_RETENTION = old_retention


if __name__ == "__main__":
    test_service_routes_a_canonical_spec_and_records_provider_identity()
    test_service_rejects_an_unknown_provider_without_falling_back()
    test_mcp_adapter_accepts_explicit_local_provider_selection()
    test_compact_results_keep_provider_identity()
    test_mcp_adapter_publishes_provider_descriptors()
    test_service_applies_policy_routing_when_selection_is_not_explicit()
    test_service_verifies_one_workload_independently_across_two_providers()
    test_service_routes_streaming_through_the_selected_provider()
    test_service_rejects_unsupported_streaming_without_fallback()
    test_service_normalizes_unsupported_synchronous_capabilities()
    test_service_receipt_reports_requested_and_provider_enforced_limits()
    test_session_service_owns_protocol_neutral_lifecycle_and_artifacts()
    test_session_service_reads_bounded_files_and_runs_workspace_entries()
    test_session_file_pagination_is_shared_and_cursor_based()
    test_mcp_session_adapters_delegate_to_the_shared_service()
    test_mcp_read_adapter_alone_converts_neutral_bytes_to_image_content()
    test_mcp_execute_adapter_routes_session_execution_through_the_service()
    test_mcp_session_execution_rejects_a_nonlocal_provider()
    test_mcp_stream_adapter_compiles_spec_and_delegates_progress()
    test_main_runs_the_same_server_over_explicit_streamable_http()
    test_serve_http_refuses_a_non_loopback_bind_without_a_token()
    test_serve_http_allows_non_loopback_with_a_token_and_loopback_range_without()
    test_http_auth_verifier_accepts_only_the_exact_token()
    test_http_auth_is_wired_into_the_server_at_import_when_the_token_is_set()
    test_run_submit_returns_a_run_id_immediately_and_run_inspect_polls_to_terminal()
    test_run_inspect_reports_a_validation_error_for_an_unknown_run_id()
    test_run_cancel_is_idempotent_on_an_already_terminal_run()
    test_run_cancel_honestly_reports_a_provider_that_cannot_cancel()
    test_run_cancel_reaches_a_provider_that_supports_it()
    test_run_tools_return_an_internal_error_when_no_supervisor_is_wired()
    test_run_cancel_on_the_real_local_provider_does_not_strand_the_result()
    test_run_inspect_terminal_result_has_the_same_keys_as_execute_code()
    test_run_submit_admission_cap_returns_resource_exhausted()
    test_session_output_spills_past_the_default_cap_instead_of_dropping_it()
    test_session_output_spill_is_faithful_to_the_pipelines_str_representation()
    test_session_run_also_spills_oversized_output()
    test_spill_files_are_retained_up_to_a_bounded_count()
    sys.exit(1 if FAILS else 0)
