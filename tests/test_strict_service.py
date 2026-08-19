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
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from urllib.parse import urlsplit

from codecalc import providers, strict_service
from codecalc.strict_runtime import (
    ENFORCEMENT_CONTROLS,
    ISOLATION_PROFILE,
    published_strict_image,
)
from codecalc.strict_service import (
    MAX_CONCURRENT_RUNS,
    MAX_CONTENT_LENGTH,
    MAX_TIMEOUT_SECONDS,
    MAX_TRACKED_RUNS,
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
                 execute_result: dict | None = None,
                 execute_raises: BaseException | None = None) -> None:
        self.enforcement_ok = enforcement_ok
        self.probe_raises = probe_raises
        self.execute_result = execute_result
        self.execute_raises = execute_raises
        self.calls: list[tuple[str, str]] = []
        self.last_timeout: int | None = None

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
        self.last_timeout = timeout
        if self.execute_raises is not None:
            raise self.execute_raises
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


def raw_oversized_request(
    base_url: str, path: str, *, declared_length: int, sent_body: bytes,
    token: str | None,
) -> tuple[int, float]:
    """Send a request whose ``Content-Length`` LIES about the body size — far
    fewer bytes actually follow than declared. ``urllib`` always sends a
    correct ``Content-Length``, so this needs a raw socket to prove the fix:
    if the handler ever calls ``rfile.read(declared_length)`` before checking
    the ceiling, it blocks waiting for bytes that never arrive, and this
    times out instead of returning a fast 413.
    """
    parts = urlsplit(base_url)
    lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {parts.hostname}:{parts.port}",
        "Content-Type: application/json",
        f"Content-Length: {declared_length}",
        "Connection: close",
    ]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    header = ("\r\n".join(lines) + "\r\n\r\n").encode()
    started = time.time()
    response = b""
    with socket.create_connection((parts.hostname, parts.port), timeout=5) as sock:
        sock.sendall(header + sent_body)
        sock.settimeout(5)
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        except TimeoutError:
            pass
    elapsed = time.time() - started
    try:
        status = int(response.split(b"\r\n", 1)[0].split(b" ")[1])
    except (IndexError, ValueError):
        status = 0
    return status, elapsed


def raw_stalled_body_request(
    base_url: str, path: str, *, declared_length: int, sent_body: bytes,
    token: str, client_wait: float,
) -> tuple[bytes, float]:
    """Send headers with a legitimate, SUB-ceiling ``Content-Length``, write
    only ``sent_body``, then never send another byte and never close our end
    — the slowloris shape THE-851 closes. Returns whatever the server sent
    back (empty if it just dropped the connection) and the elapsed wall time.

    ``client_wait`` must exceed the server's own body-read deadline so this
    proves the SERVER's bound fires, not the client giving up first; it is
    a generous fallback (so a regression fails fast instead of hanging the
    suite forever), not a race against the server's timeout.
    """
    parts = urlsplit(base_url)
    lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {parts.hostname}:{parts.port}",
        "Content-Type: application/json",
        f"Content-Length: {declared_length}",
        f"Authorization: Bearer {token}",
    ]
    header = ("\r\n".join(lines) + "\r\n\r\n").encode()
    started = time.time()
    response = b""
    with socket.create_connection(
        (parts.hostname, parts.port), timeout=client_wait
    ) as sock:
        sock.sendall(header + sent_body)
        sock.settimeout(client_wait)
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        except TimeoutError:
            pass
    elapsed = time.time() - started
    return response, elapsed


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


def test_oversized_content_length_rejected_before_read_and_before_auth() -> None:
    # A pre-auth, unbounded body read is a memory-exhaustion DoS: an
    # unauthenticated caller could force a giant allocation before the 401.
    # Declare a body far over MAX_CONTENT_LENGTH but only ever SEND a few
    # bytes, and omit the Authorization header entirely — if the handler read
    # the declared length before checking the ceiling (or before the auth
    # gate), it would block waiting on bytes that never arrive, and this test
    # would time out instead of getting a fast 413.
    server, url = serve(FakeRuntime())
    try:
        status, elapsed = raw_oversized_request(
            url, "/v1/runs/oversized-run/execute",
            declared_length=MAX_CONTENT_LENGTH + 4096,
            sent_body=b'{"language": "python3"}',
            token=None,
        )
        check("an oversized unauthenticated body is rejected with 413",
              status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"status={status}")
        check("the oversized body was refused promptly, never read",
              elapsed < 4, f"elapsed={elapsed:.2f}s")
        check("the run was never admitted to the registry",
              "oversized-run" not in server.service._runs)
    finally:
        server.shutdown()


def test_stalled_body_read_is_bounded_by_a_deadline() -> None:
    # THE-851: MAX_CONTENT_LENGTH alone only closes the OVERSIZED-body DoS.
    # A client that declares a legitimate, sub-ceiling Content-Length and
    # then dribbles/stalls the body must not pin a worker thread past a
    # bounded deadline (unbounded by MAX_CONCURRENT_RUNS: this runs before
    # dispatch() ever sees the request). The server's own deadline is
    # patched small so the test proves the SERVER's bound fires quickly,
    # not the (deliberately generous) client-side fallback.
    original = strict_service.MAX_BODY_READ_SECONDS
    strict_service.MAX_BODY_READ_SECONDS = 1.0
    server, url = serve(FakeRuntime())
    try:
        response, elapsed = raw_stalled_body_request(
            url, "/v1/runs/stalled-run/execute",
            declared_length=1000,  # well under MAX_CONTENT_LENGTH
            sent_body=b'{"lang',  # a handful of bytes, then silence forever
            token=TOKEN,
            client_wait=strict_service.MAX_BODY_READ_SECONDS + 10,
        )
        check("a stalled sub-ceiling body does not pin the worker past the "
              "server's deadline",
              elapsed < strict_service.MAX_BODY_READ_SECONDS + 5,
              f"elapsed={elapsed:.2f}s")
        check("the stalled connection is dropped, never answered with a 200",
              not response.startswith(b"HTTP/1.1 200"),
              f"response={response[:80]!r}")
        check("the stalled run was never admitted to the registry "
              "(dispatch() never ran)",
              "stalled-run" not in server.service._runs)
    finally:
        strict_service.MAX_BODY_READ_SECONDS = original
        server.shutdown()


def test_run_registry_does_not_grow_without_bound() -> None:
    runtime = FakeRuntime()
    service = StrictService(token=TOKEN, runtime=runtime)
    total = MAX_TRACKED_RUNS + 50
    for i in range(total):
        status, _ = service.dispatch(
            "POST", f"/v1/runs/run-cap-{i:05d}/execute",
            {"Authorization": f"Bearer {TOKEN}"},
            json.dumps({"language": "python3", "code": "x"}).encode(),
        )
        if status != HTTPStatus.OK:
            check(f"submission {i} succeeded", False, f"status={status}")
            break
    check("the run registry never exceeds MAX_TRACKED_RUNS",
          len(service._runs) <= MAX_TRACKED_RUNS, f"len={len(service._runs)}")
    check("the newest run is retained, not evicted",
          f"run-cap-{total - 1:05d}" in service._runs)
    check("an old run beyond the cap was evicted",
          "run-cap-00000" not in service._runs)


def test_concurrent_run_cap_returns_429() -> None:
    runtime = FakeRuntime()
    service = StrictService(token=TOKEN, runtime=runtime)
    with service._lock:
        for i in range(MAX_CONCURRENT_RUNS):
            service._runs[f"in-flight-{i}"] = {
                "run_id": f"in-flight-{i}", "status": "running",
            }
    status, _ = service.dispatch(
        "POST", "/v1/runs/one-more/execute",
        {"Authorization": f"Bearer {TOKEN}"},
        json.dumps({"language": "python3", "code": "x"}).encode(),
    )
    check("a run submitted above the concurrency cap is refused",
          status == HTTPStatus.TOO_MANY_REQUESTS, f"status={status}")
    check("the refused run was never handed to the runtime",
          not any(call == ("execute", "one-more") for call in runtime.calls))
    check("the refused run was not admitted to the registry",
          "one-more" not in service._runs)


def test_non_strict_runtime_exception_releases_the_concurrency_slot() -> None:
    # A failure from runtime.execute() that is NOT StrictRuntimeUnavailable —
    # subprocess.TimeoutExpired and OSError are the real cases, from
    # strict_runtime's `subprocess.run(..., timeout=timeout+5)` — must still
    # settle the record to a terminal state and free its concurrency slot.
    # Leaving it "running" forever would let MAX_CONCURRENT_RUNS hung
    # containers wedge the service at 429 permanently: the exact
    # availability DoS the concurrency cap exists to close, moved one
    # exception type over.
    timeout_exc = subprocess.TimeoutExpired(cmd="docker run codecalc-exec", timeout=15)
    runtime = FakeRuntime(execute_raises=timeout_exc)
    service = StrictService(token=TOKEN, runtime=runtime)

    for i in range(MAX_CONCURRENT_RUNS):
        status, _ = service.dispatch(
            "POST", f"/v1/runs/wedge-{i}/execute",
            {"Authorization": f"Bearer {TOKEN}"},
            json.dumps({"language": "python3", "code": "x"}).encode(),
        )
        check(f"wedge run {i}: a non-StrictRuntimeUnavailable failure still returns a "
              f"definitive 500, not a hang", status == HTTPStatus.INTERNAL_SERVER_ERROR,
              f"status={status}")
    check("(a) every wedge run's record is terminal, none stuck 'running'",
          all(r.get("status") != "running" for r in service._runs.values()),
          f"statuses={[r.get('status') for r in service._runs.values()]}")

    # (b) the slot was released: a fresh run is NOT wedged at 429 even though
    # MAX_CONCURRENT_RUNS runs just "failed" without ever completing.
    service._runtime = FakeRuntime()
    status, _ = service.dispatch(
        "POST", "/v1/runs/after-wedge/execute",
        {"Authorization": f"Bearer {TOKEN}"},
        json.dumps({"language": "python3", "code": "x"}).encode(),
    )
    check("(b) a run submitted after N terminal failures is not wedged at 429",
          status == HTTPStatus.OK, f"status={status}")

    # (c) _evict_oldest_locked can reclaim a record the exception path left
    # behind: push the registry past its cap and confirm the earliest
    # wedge-run record — terminal, never "completed" — is gone.
    for i in range(MAX_TRACKED_RUNS):
        service.dispatch(
            "POST", f"/v1/runs/fill-{i:05d}/execute",
            {"Authorization": f"Bearer {TOKEN}"},
            json.dumps({"language": "python3", "code": "x"}).encode(),
        )
    check("(c) a terminal record left by the exception path is reclaimed by eviction",
          "wedge-0" not in service._runs, f"len={len(service._runs)}")


def test_timeout_above_ceiling_is_clamped() -> None:
    runtime = FakeRuntime()
    service = StrictService(token=TOKEN, runtime=runtime)
    huge = MAX_TIMEOUT_SECONDS * 10
    status, _ = service.dispatch(
        "POST", "/v1/runs/clamp-run/execute",
        {"Authorization": f"Bearer {TOKEN}"},
        json.dumps({"language": "python3", "code": "x", "timeout": huge}).encode(),
    )
    check("a run with an oversized timeout still executes", status == HTTPStatus.OK)
    check("the timeout handed to the runtime is clamped to the ceiling",
          runtime.last_timeout == MAX_TIMEOUT_SECONDS,
          f"last_timeout={runtime.last_timeout}")


def test_empty_controls_receipt_yields_empty_enforcement() -> None:
    # A runtime that ever returns a receipt with missing/empty controls must
    # not have the server synthesize the full control set as True — that is
    # fail-OPEN. It must come back empty (fail-CLOSED); a genuinely partial
    # (non-empty) list is untouched, since the client already rejects it.
    empty = {
        "ok": True, "verdict": "OK", "stdout": "", "stderr": "", "exit_code": 0,
        "unenforced": [],
        "strict_receipt": {"verified": True, "isolation_profile": ISOLATION_PROFILE,
                            "runtime": "runsc", "controls": []},
    }
    missing = {
        "ok": True, "verdict": "OK", "stdout": "", "stderr": "", "exit_code": 0,
        "unenforced": [],
        "strict_receipt": {"verified": True, "isolation_profile": ISOLATION_PROFILE,
                            "runtime": "runsc"},  # no "controls" key at all
    }
    for label, tainted in (("empty controls list", empty), ("missing controls key", missing)):
        service = StrictService(token=TOKEN, runtime=FakeRuntime(execute_result=tainted))
        status, payload = service.dispatch(
            "POST", "/v1/runs/no-controls/execute",
            {"Authorization": f"Bearer {TOKEN}"},
            json.dumps({"language": "python3", "code": "x"}).encode(),
        )
        check(f"{label}: response is still 200", status == HTTPStatus.OK)
        check(f"{label}: enforcement is EMPTY, not synthesized all-True",
              payload.get("enforcement") == {}, f"enforcement={payload.get('enforcement')!r}")


def test_empty_controls_fails_client_attestation() -> None:
    # Close the loop end-to-end through the real client: fail-closed
    # enforcement must make execute_managed refuse the receipt, exactly like
    # test_broken_health_makes_client_refuse_before_payload does for health.
    tainted = {
        "ok": True, "verdict": "OK", "stdout": "42\n", "stderr": "", "exit_code": 0,
        "unenforced": [],
        "strict_receipt": {"verified": True, "isolation_profile": ISOLATION_PROFILE,
                            "runtime": "runsc", "controls": []},
    }
    server, url = serve(FakeRuntime(execute_result=tainted))
    try:
        remote = client(url)
        run_id = "run-" + uuid.uuid4().hex
        result = remote.execute_managed(
            run_id, providers.ComputationSpec("python3", "print(42)", no_net=True))
        check("an empty-controls receipt fails the client's attestation",
              result["ok"] is False
              and result["provider_error"] == "strict_attestation_failed")
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
    test_oversized_content_length_rejected_before_read_and_before_auth()
    test_stalled_body_read_is_bounded_by_a_deadline()
    test_run_registry_does_not_grow_without_bound()
    test_concurrent_run_cap_returns_429()
    test_non_strict_runtime_exception_releases_the_concurrency_slot()
    test_timeout_above_ceiling_is_clamped()
    test_empty_controls_receipt_yields_empty_enforcement()
    test_empty_controls_fails_client_attestation()
    test_unavailable_runtime_reports_not_ready()
    test_service_never_leaks_the_credential()
    test_missing_authorization_never_reaches_the_runtime()
    test_serve_strict_refuses_without_a_token()
    test_real_runsc_end_to_end()
    sys.exit(1 if FAILS else 0)
