"""End-to-end protocol/auth suite for the strict execution SERVICE (THE-830).

Drives the REAL ``RemoteStrictExecutionProvider`` client against an in-process
``StrictService`` — the acceptance test is that the client's own handshake,
attestation, receipt verification, and lifecycle calls all pass against the
server, not that the server returns a shape a test hand-wrote.

Two planes:

  * PROTOCOL + AUTH (always runs): the runtime is a fake that mimics
    ``DockerGVisorRuntime`` (probe/execute/cancel/cleanup), so no Docker is
    needed — the same way ``test_strict_providers`` mocks the transport. The
    server is a genuine ``http.server`` bound to loopback on an ephemeral port,
    so the auth 401s, JSON framing and routing are exercised for real over the
    wire via the client's ``urllib`` transport.
  * REAL runsc (Cave-only, SKIPS without it): starts the service with a real
    ``DockerGVisorRuntime`` and drives the client through a live gVisor run,
    proving the runtime is genuinely runsc OUT OF BAND via ``docker inspect``,
    that cancel kills a live container, and that a fork bomb is contained.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus

from codecalc import providers
from codecalc.strict_runtime import (
    ENFORCEMENT_CONTROLS,
    ISOLATION_PROFILE,
    published_strict_image,
)
from codecalc.strict_service import (
    STRICT_SERVICE_PROVIDER_ID,
    StrictHTTPServer,
    StrictService,
)

FAILS: list[str] = []
TOKEN = "test-strict-token-9f3a"  # noqa: S105 -- a test credential, not a real one


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name} {detail}")
    if not condition:
        FAILS.append(name)


# ── a fake runtime that mimics DockerGVisorRuntime, no Docker required ────────
class FakeRuntime:
    """Records lifecycle calls and returns container-shaped results."""

    def __init__(self, *, enforcement_ok: bool = True, probe_raises: str | None = None,
                 execute_result: dict | None = None) -> None:
        self.enforcement_ok = enforcement_ok
        self.probe_raises = probe_raises
        self.execute_result = execute_result
        self.calls: list[tuple[str, str]] = []

    def probe(self) -> dict:
        if self.probe_raises is not None:
            from codecalc.strict_runtime import StrictRuntimeUnavailable
            raise StrictRuntimeUnavailable(self.probe_raises)
        enforcement = dict.fromkeys(ENFORCEMENT_CONTROLS, True)
        if not self.enforcement_ok:
            enforcement["network"] = False
        return {
            "isolation_profile": ISOLATION_PROFILE,
            "runtime": "runsc",
            "architecture": "aarch64",
            "docker_version": "29.7.2",
            "image": "codecalc-exec@sha256:" + "a" * 64,
            "enforcement": enforcement,
        }

    def execute(self, run_id, *, language, source, timeout, **kwargs) -> dict:
        self.calls.append(("execute", run_id))
        if self.execute_result is not None:
            return dict(self.execute_result)
        return {
            "ok": True, "verdict": "OK", "stdout": source.strip() + "\n",
            "stderr": "", "exit_code": 0, "unenforced": [], "language": language,
            "strict_receipt": {
                "verified": True, "isolation_profile": ISOLATION_PROFILE,
                "runtime": "runsc", "controls": list(ENFORCEMENT_CONTROLS),
            },
        }

    def cancel(self, run_id) -> None:
        self.calls.append(("cancel", run_id))

    def cleanup(self, run_id) -> None:
        self.calls.append(("cleanup", run_id))


def serve(runtime) -> tuple[StrictHTTPServer, str]:
    """Start a real loopback http.server backed by ``runtime``; return it + URL."""
    service = StrictService(token=TOKEN, runtime=runtime)
    server = StrictHTTPServer(("127.0.0.1", 0), service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def client(base_url: str, *, authorization: str | None = f"Bearer {TOKEN}"):
    return providers.RemoteStrictExecutionProvider(
        client_platform="macos", base_url=base_url, authorization=authorization,
    )


def raw_status(base_url: str, path: str, *, method: str = "GET",
               token: str | None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    request = urllib.request.Request(  # noqa: S310 -- loopback test URL
        base_url + path, method=method, headers=headers,
        data=b"{}" if method == "POST" else None,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


# ── PROTOCOL + AUTH (always runs) ────────────────────────────────────────────
def test_full_client_flow_against_inprocess_service() -> None:
    runtime = FakeRuntime()
    server, url = serve(runtime)
    try:
        remote = client(url)

        health = remote.health()
        check("health handshake passes the client's verified attestation",
              health["ready"] is True and health["strict"] is True)
        check("health reports the linux-strict identity",
              health["remote_provider_id"] == STRICT_SERVICE_PROVIDER_ID)
        check("health reports the gvisor-v1 isolation profile",
              health["isolation_profile"] == ISOLATION_PROFILE)

        run_id = "run-" + uuid.uuid4().hex
        result = remote.execute_managed(
            run_id, providers.ComputationSpec("python3", "print(42)", no_net=True))
        check("execute returns a verified strict receipt",
              result["strict_receipt"]["verified"] is True)
        check("receipt lists exactly the strict controls",
              set(result["strict_receipt"]["controls"]) == set(providers.STRICT_CONTROLS))
        check("execute ran under the client-chosen run id",
              ("execute", run_id) in runtime.calls)
        check("execution result carries the payload output",
              result.get("verdict") == "OK" and "42" in result.get("stdout", ""))

        inspected = remote.inspect(run_id)
        check("inspect returns the tracked run",
              inspected.get("run_id") == run_id and inspected.get("status") == "completed")

        remote.cancel(run_id)
        remote.cleanup(run_id)
        check("cancel routed to the owned run", ("cancel", run_id) in runtime.calls)
        check("cleanup routed to the owned run", ("cleanup", run_id) in runtime.calls)
    finally:
        server.shutdown()


def test_cleanup_is_idempotent() -> None:
    runtime = FakeRuntime()
    server, url = serve(runtime)
    try:
        remote = client(url)
        remote.cleanup("run-abc")
        remote.cleanup("run-abc")  # a second DELETE must not raise
        check("repeated cleanup is idempotent",
              runtime.calls.count(("cleanup", "run-abc")) == 2)
    finally:
        server.shutdown()


def test_bearer_auth_is_required() -> None:
    server, url = serve(FakeRuntime())
    try:
        check("no token is 401",
              raw_status(url, "/v1/health", token=None) == HTTPStatus.UNAUTHORIZED)
        check("wrong token is 401",
              raw_status(url, "/v1/health", token="not-the-token")  # noqa: S106 -- a deliberately wrong test token
              == HTTPStatus.UNAUTHORIZED)
        check("right token is 200",
              raw_status(url, "/v1/health", token=TOKEN) == HTTPStatus.OK)
        check("no token cannot execute",
              raw_status(url, "/v1/runs/r1/execute", method="POST", token=None)
              == HTTPStatus.UNAUTHORIZED)
    finally:
        server.shutdown()


def test_broken_health_makes_client_refuse_before_payload() -> None:
    # An incomplete enforcement block must fail the client's attestation, and no
    # payload may be submitted after that failure.
    runtime = FakeRuntime(enforcement_ok=False)
    server, url = serve(runtime)
    try:
        remote = client(url)
        result = remote.execute(
            providers.ComputationSpec("python3", "print('must-not-run')", no_net=True))
        check("incomplete health fails the client attestation", result["ok"] is False)
        check("failed attestation has the stable code",
              result["provider_error"] == "strict_attestation_failed")
        check("no payload was executed after a failed handshake",
              not any(call[0] == "execute" for call in runtime.calls))
    finally:
        server.shutdown()


def test_unavailable_runtime_reports_not_ready() -> None:
    runtime = FakeRuntime(probe_raises="Docker Engine is unavailable")
    server, url = serve(runtime)
    try:
        remote = client(url)
        check("an unproven boundary is reported not ready",
              remote.health()["ready"] is False)
    finally:
        server.shutdown()


def test_service_never_leaks_the_credential() -> None:
    # Server-side redaction, exercised directly: a runtime result that echoes the
    # token must come back redacted, never verbatim.
    tainted = {
        "ok": True, "verdict": "OK", "stdout": f"Bearer {TOKEN}", "stderr": "",
        "exit_code": 0, "unenforced": [], "detail": TOKEN,
        "strict_receipt": {"verified": True, "isolation_profile": ISOLATION_PROFILE,
                           "runtime": "runsc", "controls": list(ENFORCEMENT_CONTROLS)},
    }
    service = StrictService(token=TOKEN, runtime=FakeRuntime(execute_result=tainted))
    status, payload = service.dispatch(
        "POST", "/v1/runs/run-x/execute", {"Authorization": f"Bearer {TOKEN}"},
        json.dumps({"language": "python3", "code": "x"}).encode())
    body = json.dumps(payload)
    check("execute response redacts the credential",
          status == HTTPStatus.OK and TOKEN not in body and "[REDACTED]" in body)


def test_missing_authorization_never_reaches_the_runtime() -> None:
    runtime = FakeRuntime()
    service = StrictService(token=TOKEN, runtime=runtime)
    status, _ = service.dispatch("GET", "/v1/health", {}, b"")
    check("unauthenticated request is 401", status == HTTPStatus.UNAUTHORIZED)
    check("unauthenticated request never touched the runtime", runtime.calls == [])


def test_serve_strict_refuses_without_a_token(monkeypatch_env=None) -> None:
    # The entrypoint refuses to start when the token env is unset — the
    # fail-closed posture for a service whose whole job is authentication.
    import os

    from codecalc import strict_service
    saved = os.environ.pop(strict_service.STRICT_SERVICE_TOKEN_ENV, None)
    try:
        rc_loopback = strict_service.main(["--host", "127.0.0.1", "--port", "0"])
        rc_routable = strict_service.main(["--host", "0.0.0.0", "--port", "0"])  # noqa: S104 -- refused before any bind; asserts the refusal
        check("token-less loopback start is refused", rc_loopback == 2)
        check("token-less routable start is refused", rc_routable == 2)
    finally:
        if saved is not None:
            os.environ[strict_service.STRICT_SERVICE_TOKEN_ENV] = saved


# ── REAL runsc end-to-end (Cave-only, SKIPS without runsc) ───────────────────
def _runsc_registered() -> bool:
    try:
        proc = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"],
                              capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    try:
        return "runsc" in (json.loads(proc.stdout) or {})
    except json.JSONDecodeError:
        return False


def _container_runtime(run_id: str) -> str | None:
    proc = subprocess.run(
        ["docker", "inspect", "--format", "{{.HostConfig.Runtime}}",
         f"codecalc-{run_id}"],
        capture_output=True, text=True, timeout=10, check=False)
    return (proc.stdout or "").strip() or None if proc.returncode == 0 else None


def test_real_runsc_end_to_end() -> None:
    if not _runsc_registered():
        print("SKIP real-runsc end-to-end — the 'runsc' runtime is not registered "
              "with Docker (expected off Cave / a gVisor host); the protocol and "
              "auth planes above cover everything that does not need real gVisor")
        return
    if published_strict_image() is None:
        print("SKIP real-runsc end-to-end — no published, digest-pinned strict "
              "image is resolvable (docker/executor-image.lock)")
        return

    from codecalc.strict_runtime import DockerGVisorRuntime, strict_execution_config
    runtime = DockerGVisorRuntime(strict_execution_config())
    service = StrictService(token=TOKEN, runtime=runtime)
    server = StrictHTTPServer(("127.0.0.1", 0), service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        remote = client(url)
        check("real health is ready under runsc", remote.health()["ready"] is True)

        run_id = "e2e-" + uuid.uuid4().hex
        result = remote.execute_managed(
            run_id, providers.ComputationSpec("python3", "print(42)", timeout=30))
        check("real gVisor run returns the payload output",
              "42" in result.get("stdout", "") and result.get("verdict") == "OK")
        check("real run carries a verified strict receipt",
              result["strict_receipt"]["verified"] is True)
        remote.cleanup(run_id)

        # Prove the runtime is GENUINELY runsc, out of band: start a long run in a
        # thread and read docker's own record of the container's runtime.
        sleep_id = "e2e-" + uuid.uuid4().hex
        errors: list[str] = []

        def _run_sleep() -> None:
            try:
                remote.execute_managed(sleep_id, providers.ComputationSpec(
                    "python3", "import time; time.sleep(20)", timeout=25))
            except Exception as exc:
                errors.append(str(exc))

        worker = threading.Thread(target=_run_sleep, daemon=True)
        worker.start()
        observed = None
        for _ in range(80):
            observed = _container_runtime(sleep_id)
            if observed is not None:
                break
            time.sleep(0.25)
        check("the live container genuinely ran under runsc (docker inspect)",
              observed == "runsc", f"observed={observed!r}")
        remote.cancel(sleep_id)
        worker.join(timeout=15)
        remote.cleanup(sleep_id)
        check("cancel killed the live run", not worker.is_alive())

        # A fork bomb is CONTAINED: the pids limit holds and the call returns
        # rather than taking the host down.
        bomb_id = "e2e-" + uuid.uuid4().hex
        bomb = ("import os\nwhile True:\n"
                "    try:\n        os.fork()\n    except Exception:\n        pass\n")
        started = time.time()
        try:
            remote.execute_managed(bomb_id, providers.ComputationSpec(
                "python3", bomb, timeout=15))
        except Exception:
            pass
        remote.cleanup(bomb_id)
        check("a fork bomb is contained (the call returned, host unaffected)",
              time.time() - started < 40)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    test_full_client_flow_against_inprocess_service()
    test_cleanup_is_idempotent()
    test_bearer_auth_is_required()
    test_broken_health_makes_client_refuse_before_payload()
    test_unavailable_runtime_reports_not_ready()
    test_service_never_leaks_the_credential()
    test_missing_authorization_never_reaches_the_runtime()
    test_serve_strict_refuses_without_a_token()
    test_real_runsc_end_to_end()
    sys.exit(1 if FAILS else 0)
