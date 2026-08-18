"""Protocol-neutral execution service and MCP adapter parity (THE-790)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp.types import ImageContent

from codecalc import (
    contract,
    execution_service,
    executor,
    providers,
    server,
    sessions,
    spec_identity,
)

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


# ═══ the receipt names WHAT ran and under WHICH conditions (THE-782) ═══════
#
# The limits half above answers "what did you ask for and what did the provider
# claim to enforce". It does not answer the two questions someone re-reading a
# result a week later actually has: WHICH request was this, and what did the
# environment look like. Content hashes answer the first; the determinism block
# answers the second, including — loudly — the parts codecalc cannot observe.


def _receipt_provider(**overrides: object):
    class ReceiptProvider(providers.LocalExecutionProvider):
        # Deliberately NOT "local": this provider stands in for anything that
        # runs its workload somewhere codecalc does not control.
        provider_id = "receipt"

        def describe(self) -> dict:
            descriptor = super().describe()
            descriptor["provider_id"] = self.provider_id
            descriptor.update(overrides)
            return descriptor

        def execute(self, spec: providers.ComputationSpec) -> dict:
            del spec
            return contract.stamp({"ok": True, "verdict": "OK", "stdout": "",
                                   "stderr": "", "exit_code": 0, "unenforced": []})

    return ReceiptProvider


def _receipt_for(spec: providers.ComputationSpec, **overrides: object) -> dict:
    registry = providers.ProviderRegistry(default_provider_id="receipt")
    registry.register(_receipt_provider(**overrides)())
    return execution_service.ExecutionService(registry).execute(spec)["provider"]


def test_receipt_names_the_request_and_the_source_by_content() -> None:
    spec = providers.ComputationSpec(language="python3", code='print("receipt")',
                                     stdin="1\n", timeout=7, no_net=True)
    receipt = _receipt_for(spec)

    check("the receipt is versioned so a reader can evolve",
          receipt["receipt_version"] == execution_service.RECEIPT_VERSION)
    check("the receipt version is semver",
          len(execution_service.RECEIPT_VERSION.split(".")) == 3)
    check("the receipt names the request by content hash",
          receipt["spec_hash"] == spec.spec_hash()
          and receipt["spec_hash"].startswith("sha256:"))
    check("the receipt names the canonical form the hash was taken under",
          receipt["spec_schema_version"] == providers.COMPUTATION_SPEC_VERSION)
    check("the receipt hashes the source that was actually executed",
          receipt["source_sha256"]
          == spec_identity.text_hash('print("receipt")'))
    check("the source hash is not the spec hash",
          receipt["source_sha256"] != receipt["spec_hash"])
    check("the requested-vs-enforced distinction is untouched",
          receipt["limits"]["requested"]["timeout_seconds"] == 7
          and "provider_reported_enforced" in receipt["limits"]
          and "unenforced" in receipt["limits"])


def test_receipt_is_stable_for_identical_inputs_and_moves_for_different_ones() -> None:
    spec = providers.ComputationSpec(language="python3", code="pass", timeout=3)
    first = _receipt_for(spec)
    second = _receipt_for(providers.ComputationSpec(
        timeout=3, code="pass", language="python3"))
    other = _receipt_for(providers.ComputationSpec(
        language="python3", code="pass", timeout=4))

    stable = ("receipt_version", "spec_hash", "spec_schema_version",
              "source_sha256", "determinism")
    check("two identical requests produce an identical receipt",
          {k: first[k] for k in stable} == {k: second[k] for k in stable})
    check("a different request produces a different spec hash",
          other["spec_hash"] != first["spec_hash"])
    check("the same source under a different request keeps one source hash",
          other["source_sha256"] == first["source_sha256"])


def test_receipt_records_the_determinism_inputs_it_can_observe() -> None:
    # The real local provider, because the determinism block describes the
    # environment codecalc's OWN sandbox forwards — which is a claim only the
    # provider that actually runs through that sandbox is entitled to make.
    determinism = _service().execute(providers.ComputationSpec(
        language="python3", code="pass"))["provider"]["determinism"]
    forwarded = executor._env()

    check("determinism names where its values came from",
          determinism["source"] == "env_allowlist")
    check("locale is what the env allowlist actually forwards",
          determinism["locale"] == {"LANG": forwarded.get("LANG"),
                                    "LC_ALL": forwarded.get("LC_ALL")})
    check("timezone is what the env allowlist actually forwards",
          determinism["timezone"] == forwarded.get("TZ"))
    check("TZ is not in the allowlist today, so timezone is unrecorded",
          "TZ" not in executor._ENV_ALLOWLIST
          and "timezone" in determinism["unrecorded"])
    check("codecalc has no seed concept, and the receipt says so rather than "
          "reporting a null nobody can interpret",
          determinism["seed"] is None and "seed" in determinism["unrecorded"])
    check("every unrecorded entry is genuinely null",
          all(_determinism_value(determinism, name) is None
              for name in determinism["unrecorded"]))
    check("nothing is both recorded and unrecorded",
          all(_determinism_value(determinism, name) is not None
              for name in ("locale.LANG", "locale.LC_ALL", "timezone", "seed")
              if name not in determinism["unrecorded"]))


def _determinism_value(determinism: dict, dotted: str) -> object:
    node: object = determinism
    for part in dotted.split("."):
        node = node[part]  # type: ignore[index]
    return node


def test_receipt_determinism_does_not_claim_a_remote_environment() -> None:
    """CodeCalc controls the local sandbox's environment. It does not control a
    remote provider's, and a receipt that reported the client's locale for a run
    that happened on someone else's host would be a confident lie."""
    remote = _receipt_for(
        providers.ComputationSpec(language="python3", code="pass"))["determinism"]

    check("a non-sandboxed provider does not borrow the client's environment",
          remote["source"] == "provider_owned")
    check("a remote run records nothing it did not observe",
          remote["locale"] == {"LANG": None, "LC_ALL": None}
          and remote["timezone"] is None and remote["seed"] is None)
    check("and says so, rather than leaving four bare nulls",
          sorted(remote["unrecorded"])
          == ["locale.LANG", "locale.LC_ALL", "seed", "timezone"])


def test_receipt_carries_no_secret_and_no_machine_specific_path() -> None:
    """A receipt travels: it is logged, pasted into tickets, and returned to a
    caller. It must be portable JSON with nothing in it that identifies a host."""
    # THE REAL LOCAL PROVIDER, not the stand-in. An earlier version of this test
    # used the stand-in and therefore exercised the `provider_owned` branch,
    # which reads no environment at all — so it passed with the entire forwarded
    # environment spliced into the determinism block. Found by seeding exactly
    # that defect and watching nothing go red.
    with tempfile.TemporaryDirectory() as workdir:
        spec = providers.ComputationSpec(language="python3", code="pass",
                                         workdir=workdir)
        receipt = _service().execute(spec)["provider"]
        serialized = json.dumps(receipt)
        check("the workdir path never reaches the receipt, only its hash",
              workdir not in serialized)

    check("the receipt is JSON-serializable", isinstance(serialized, str))
    check("the determinism block came from the sandbox, so this is the branch "
          "that could leak",
          receipt["determinism"]["source"] == "env_allowlist")
    check("no absolute home-directory path appears anywhere in the receipt",
          str(Path.home()) not in serialized)
    forwarded = executor._env()
    check("the forwarded PATH does not leak through the determinism block",
          forwarded["PATH"] not in serialized)
    check("HOME does not leak through the determinism block",
          not forwarded.get("HOME") or forwarded["HOME"] not in serialized)
    check("the determinism block carries only the three named inputs",
          set(receipt["determinism"])
          == {"source", "locale", "timezone", "seed", "unrecorded"})


def test_every_receipt_bearing_path_carries_the_same_receipt() -> None:
    """A receipt on one path and not another is worse than none: a reader cannot
    tell "no hash" from "this path forgot". execute, execute_stream and session
    execution all attach it."""
    spec = providers.ComputationSpec(language="python3", code='print("paths")')
    service = _service()

    plain = service.execute(spec)["provider"]
    streamed = asyncio.run(service.execute_stream(spec))["provider"]
    session = server.session_start("python3")
    try:
        in_session = execution_service.ExecutionService(
            service.registry).execute_session(
                execution_service.SessionService(), session["session_id"], spec
            )["provider"]
    finally:
        server.session_stop(session["session_id"])

    required = {"receipt_version", "spec_hash", "spec_schema_version",
                "source_sha256", "determinism", "limits"}
    check("execute() attaches the full receipt", required <= set(plain))
    check("execute_stream() attaches the full receipt", required <= set(streamed))
    check("execute_session() attaches the full receipt", required <= set(in_session))
    check("all three agree on the request identity",
          plain["spec_hash"] == streamed["spec_hash"] == in_session["spec_hash"]
          == spec.spec_hash())
    check("all three agree on the source identity",
          plain["source_sha256"] == streamed["source_sha256"]
          == in_session["source_sha256"])


def test_compact_mode_keeps_the_actionable_half_of_the_receipt() -> None:
    """A receipt that doubles a compact reply defeats the mode it rides in.

    Copied wholesale, THE-782's additions took a compact result from 603 to
    1019 bytes — measured, and past the bar server.py already set when it called
    a 171-of-199-token disclosure a defect. What a caller must ACT on stays;
    what is descriptive and one call away is named and dropped.
    """
    full = server.execute_code("python3", 'print("c")', provider="local")["provider"]
    terse = server.execute_code(
        "python3", 'print("c")', provider="local", compact=True)["provider"]

    check("compact keeps the request identity", terse["spec_hash"] == full["spec_hash"])
    check("compact keeps the receipt version",
          terse["receipt_version"] == execution_service.RECEIPT_VERSION)
    check("compact keeps provider identity", terse["provider_id"] == "local")
    check("compact keeps the whole limits receipt", terse["limits"] == full["limits"])
    check("compact drops the descriptive half",
          "determinism" not in terse and "source_sha256" not in terse)
    check("compact names every key it dropped rather than truncating silently",
          all(key in terse["receipt_detail"]
              for key in set(full) - set(terse) - {"receipt_detail"}))
    check("the compact receipt is smaller than the full one",
          len(json.dumps(terse)) < len(json.dumps(full)))


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
    CODECALC_HTTP_TOKEN would expose 48 unauthenticated code-execution tools to
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
    test_receipt_names_the_request_and_the_source_by_content()
    test_receipt_is_stable_for_identical_inputs_and_moves_for_different_ones()
    test_receipt_records_the_determinism_inputs_it_can_observe()
    test_receipt_determinism_does_not_claim_a_remote_environment()
    test_receipt_carries_no_secret_and_no_machine_specific_path()
    test_every_receipt_bearing_path_carries_the_same_receipt()
    test_compact_mode_keeps_the_actionable_half_of_the_receipt()
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
    sys.exit(1 if FAILS else 0)
