"""Authenticated HTTP `/v1` service for gVisor-sandboxed strict execution (THE-830).

This is the SERVER half of the strict execution protocol whose client is
``providers.RemoteStrictExecutionProvider`` (a macOS/Windows host that cannot run
gVisor delegates to a Linux host that can). It serves exactly the five endpoints
that client speaks:

  * ``GET  /v1/health``                — the gvisor-v1 readiness attestation the
    client's ``_verified_health`` gates on before it will submit any payload.
  * ``POST /v1/runs/{run_id}/execute`` — run a ``ComputationSpec`` under
    ``--runtime=runsc`` with the published, digest-pinned executor image, and
    return the execution result plus the ``enforcement`` block the client's
    ``execute_managed`` verifies into a strict receipt.
  * ``GET  /v1/runs/{run_id}``          — inspect a tracked run.
  * ``POST /v1/runs/{run_id}/cancel``   — kill an owned, in-flight run.
  * ``DELETE /v1/runs/{run_id}``        — clean a run up, idempotently.

Fail-closed everywhere it can be:

  * BEARER AUTH IS REQUIRED. Every request is rejected with 401 unless it carries
    the exact ``Authorization: Bearer <token>``; the comparison is constant-time
    (``hmac.compare_digest``), mirroring ``server.py``'s THE-786 posture. The
    entrypoint refuses to start without a token at all — the service exists to
    authenticate a REMOTE caller, so a token-less bind (on any interface, and
    especially a routable one) would expose sandboxed code execution to whatever
    the interface reaches.
  * The health attestation and the execution path resolve the image through
    ``strict_execution_config`` — DIGEST-REQUIRED — so an unpublished image makes
    ``/v1/health`` report ``ready: False`` (the client then refuses to submit)
    rather than silently running the mutable local diagnostic tag.
  * The probe is the SAME one ``doctor`` and the execution path use
    (``DockerGVisorRuntime.probe``), so this server can never attest a boundary
    it would not actually launch under.
  * Credentials never enter a log, an error, or a result — the request logger is
    silenced and every response body is passed through ``_redact_secrets``.

No new dependency: the transport is the standard library's ``http.server``. The
client talks plain JSON over HTTP(S) via ``urllib``, so nothing here needs an
ASGI framework, and the MCP SDK's HTTP stack speaks the MCP protocol, not this
REST surface.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import sys
import threading
import time
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .providers import PROVIDER_INTERFACE_VERSION, _redact_secrets
from .strict_runtime import (
    _RUN_ID,
    ENFORCEMENT_CONTROLS,
    ISOLATION_PROFILE,
    DockerGVisorRuntime,
    StrictRuntimeUnavailable,
    strict_execution_config,
)

#: Bearer token for the strict execution service. Its own env var rather than a
#: reuse of ``CODECALC_HTTP_TOKEN`` (the MCP serve-http transport's token): the
#: two services have different trust boundaries and are provisioned separately,
#: and a shared secret would couple them. Unset means the service refuses to
#: start — see ``main``.
STRICT_SERVICE_TOKEN_ENV = "CODECALC_STRICT_SERVICE_TOKEN"  # noqa: S105 -- env var NAME

#: The provider identity the client's ``_verified_health`` requires verbatim. The
#: service runs on Linux (it is the host that can run gVisor), so this is fixed.
STRICT_SERVICE_PROVIDER_ID = "linux-strict"

#: Ceiling on a request body the strict service will read into memory, checked
#: against the declared ``Content-Length`` BEFORE any bytes are read and BEFORE
#: the auth gate runs (a declared size is not a secret) — an unauthenticated
#: caller sending a huge ``Content-Length`` must never force a giant allocation.
#: A code-execution payload has no legitimate reason to approach this; 1 MiB is
#: generous headroom over any real ``language``/``code`` pair. Only the
#: ``Content-Length``-declared path is guarded: ``_StrictRequestHandler`` never
#: negotiates chunked ``Transfer-Encoding`` and ``rfile.read(length)`` is its
#: only body read, so there is no unbounded-chunked path to close here.
MAX_CONTENT_LENGTH = 1024 * 1024  # 1 MiB

#: Ceiling on retained run records. ``_runs`` only shrinks on an explicit
#: DELETE; a client that submits and never cleans up would otherwise grow the
#: registry without bound. The oldest TERMINAL (non-"running") record is
#: evicted once this is exceeded — see ``StrictService._evict_oldest_locked``.
MAX_TRACKED_RUNS = 512

#: Ceiling on runs with status "running" at once. Refused with 429 rather than
#: queued, so a burst of submissions cannot pile up unbounded concurrent gVisor
#: containers.
MAX_CONCURRENT_RUNS = 8

#: Ceiling on a client-supplied ``timeout`` (seconds), clamped rather than
#: rejected — matching how a non-positive timeout is already treated as "use
#: the default" (see ``_positive_int``) rather than an error.
MAX_TIMEOUT_SECONDS = 300


class _HttpError(Exception):
    """An early return carrying an HTTP status and a JSON body."""

    def __init__(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("error", ""))
        self.status = status
        self.payload = payload


class StrictService:
    """Route the five strict endpoints over an injectable gVisor runtime.

    The runtime is injectable so the protocol and auth surface can be driven
    without Docker (the tests pass a fake that mimics ``DockerGVisorRuntime``);
    left unset, each call resolves a real ``DockerGVisorRuntime`` from the
    published, digest-pinned image via ``strict_execution_config``.
    """

    def __init__(
        self,
        *,
        token: str,
        runtime: Any = None,
        environment: Mapping[str, str] | None = None,
        provider_version: str = __version__,
    ) -> None:
        if not token:
            raise ValueError("strict service requires a non-empty bearer token")
        self._expected = f"Bearer {token}"
        self._runtime = runtime
        self._environment = environment
        self._provider_version = provider_version
        # Redact both the whole header value and the bare credential, longest
        # first, exactly as the providers build their own redaction sets.
        self._secrets = tuple(sorted({token, self._expected}, key=len, reverse=True))
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ── plumbing ────────────────────────────────────────────────────────────
    def _resolve_runtime(self) -> Any:
        if self._runtime is not None:
            return self._runtime
        # Digest-required: raises StrictImageUnavailable (a StrictRuntimeUnavailable)
        # when nothing is published, which callers turn into a fail-closed reply.
        return DockerGVisorRuntime(strict_execution_config(self._environment))

    def _redact(self, value: Any) -> Any:
        return _redact_secrets(value, self._secrets)

    def _evict_oldest_locked(self) -> None:
        """Drop terminal-state run records, oldest first, until ``_runs`` is
        back under ``MAX_TRACKED_RUNS``. Caller MUST hold ``self._lock``.

        A record still ``"running"`` is never evicted: ``_execute`` is
        synchronous, so ``"running"`` only exists for the duration of another
        thread's in-flight call, not as an accumulated leak — the leak this
        guards against is a client that submits and never DELETEs.
        """
        if len(self._runs) <= MAX_TRACKED_RUNS:
            return
        for stale_id, record in list(self._runs.items()):
            if len(self._runs) <= MAX_TRACKED_RUNS:
                break
            if record.get("status") != "running":
                self._runs.pop(stale_id, None)

    def authorized(self, headers: Mapping[str, str]) -> bool:
        presented = headers.get("Authorization", "") or ""
        return hmac.compare_digest(presented.encode(), self._expected.encode())

    def dispatch(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[HTTPStatus, dict[str, Any]]:
        """Authenticate, then route — the single entry the HTTP handler calls."""
        if not self.authorized(headers):
            return HTTPStatus.UNAUTHORIZED, {
                "ok": False,
                "error": "strict service authentication required",
            }
        try:
            return self._route(method, path, body)
        except _HttpError as exc:
            return exc.status, self._redact(exc.payload)
        except Exception as exc:
            return HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False,
                "error": self._redact(f"strict service error: {exc}"),
            }

    def _route(
        self, method: str, path: str, body: bytes
    ) -> tuple[HTTPStatus, dict[str, Any]]:
        segments = [s for s in path.split("?", 1)[0].split("/") if s]
        if segments == ["v1", "health"] and method == "GET":
            return self._health()
        if len(segments) >= 3 and segments[0] == "v1" and segments[1] == "runs":
            run_id = segments[2]
            tail = segments[3] if len(segments) > 3 else None
            if tail == "execute" and method == "POST":
                return self._execute(run_id, body)
            if tail == "cancel" and method == "POST":
                return self._cancel(run_id)
            if tail is None and method == "GET":
                return self._inspect(run_id)
            if tail is None and method == "DELETE":
                return self._cleanup(run_id)
        raise _HttpError(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "error": f"no such strict endpoint: {method} {path}"},
        )

    @staticmethod
    def _valid_run_id(run_id: str) -> None:
        if not _RUN_ID.fullmatch(run_id):
            raise _HttpError(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "run_id is not a safe strict identity"},
            )

    @staticmethod
    def _parse_spec(body: bytes) -> dict[str, Any]:
        try:
            spec = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _HttpError(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "request body is not valid JSON"},
            ) from None
        if not isinstance(spec, dict):
            raise _HttpError(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "request body must be a JSON object"},
            )
        return spec

    # ── endpoints ───────────────────────────────────────────────────────────
    def _health(self) -> tuple[HTTPStatus, dict[str, Any]]:
        """The gvisor-v1 readiness attestation, or a fail-closed not-ready body.

        Always HTTP 200: an unproven boundary (Docker down, runsc unregistered,
        no published image) is reported as ``ready: False`` so the CLIENT refuses
        to submit rather than the transport raising — the fail-closed path the
        client's ``_verified_health`` is written to accept.
        """
        base = {
            "interface_version": PROVIDER_INTERFACE_VERSION,
            "provider_id": STRICT_SERVICE_PROVIDER_ID,
            "provider_version": self._provider_version,
            "isolation_profile": ISOLATION_PROFILE,
            "strict": True,
        }
        try:
            probe = self._resolve_runtime().probe()
        except StrictRuntimeUnavailable as exc:
            return HTTPStatus.OK, {
                **base,
                "ready": False,
                "enforcement": dict.fromkeys(ENFORCEMENT_CONTROLS, False),
                "error": self._redact(str(exc)),
            }
        return HTTPStatus.OK, {
            **base,
            "isolation_profile": probe.get("isolation_profile", ISOLATION_PROFILE),
            "ready": True,
            "enforcement": dict(probe.get("enforcement") or {}),
            "runtime": probe.get("runtime"),
            "architecture": probe.get("architecture"),
            "docker_version": probe.get("docker_version"),
            "image": probe.get("image"),
        }

    def _execute(self, run_id: str, body: bytes) -> tuple[HTTPStatus, dict[str, Any]]:
        self._valid_run_id(run_id)
        spec = self._parse_spec(body)
        language = spec.get("language")
        code = spec.get("code")
        if not isinstance(language, str) or not isinstance(code, str):
            raise _HttpError(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "spec requires string 'language' and 'code'"},
            )
        if spec.get("workdir") not in (None, ""):
            raise _HttpError(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "strict service assigns its own ephemeral workdir"},
            )
        kwargs: dict[str, Any] = {}
        memory_mb = _positive_int(spec.get("max_memory_mb"))
        if memory_mb:
            kwargs["memory_mb"] = memory_mb
        # Clamped, not rejected: a client-supplied timeout above the ceiling
        # degrades to "runs as long as we'll allow" rather than erroring.
        timeout = min(_positive_int(spec.get("timeout")) or 10, MAX_TIMEOUT_SECONDS)

        runtime = self._resolve_runtime()
        record: dict[str, Any] = {
            "run_id": run_id,
            "status": "running",
            "language": language,
            "created_at": time.time(),
        }
        with self._lock:
            in_flight = sum(1 for r in self._runs.values() if r.get("status") == "running")
            if in_flight >= MAX_CONCURRENT_RUNS:
                raise _HttpError(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {
                        "ok": False,
                        "error": "strict service is at its concurrent-run limit",
                        "run_id": run_id,
                    },
                )
            self._runs[run_id] = record
            self._evict_oldest_locked()
        try:
            result = runtime.execute(
                run_id, language=language, source=code, timeout=timeout, **kwargs
            )
        except StrictRuntimeUnavailable as exc:
            message = self._redact(str(exc))
            with self._lock:
                record["status"] = "failed"
                record["error"] = message
            raise _HttpError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": message, "run_id": run_id},
            ) from None
        except Exception as exc:
            # ANY other failure — subprocess.TimeoutExpired, OSError, or
            # anything else strict_runtime's own except BaseException:...raise
            # lets through — must still settle the record to a terminal state.
            # MAX_CONCURRENT_RUNS is derived by counting "running" records
            # live (see the in_flight sum above); leaving one stuck at
            # "running" forever (no `finally`, no catch-all) would let a
            # handful of hung containers wedge the service at the cap
            # permanently — the exact availability DoS this cap exists to
            # close, just moved one exception type over. Treated the same as
            # StrictRuntimeUnavailable: no reason to distinguish "failed"
            # reasons here.
            message = self._redact(f"strict execution failed: {exc}")
            with self._lock:
                record["status"] = "failed"
                record["error"] = message
            raise _HttpError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": message, "run_id": run_id},
            ) from None

        result = dict(result)
        # The client's execute_managed VERIFIES this block: every strict control
        # must be present and True or it refuses the receipt. It is derived from
        # the controls the runtime's own receipt records applying (== the strict
        # launch's full set), not a second hand-maintained list. A receipt with
        # missing/empty controls must NOT synthesize the full set as True — that
        # would fail OPEN for a runtime bug or malformed receipt. It becomes
        # empty enforcement instead, which the client already rejects as an
        # incomplete set (fail CLOSED); a genuinely partial (non-empty) list is
        # left as-is for the same rejection.
        receipt = result.get("strict_receipt") or {}
        controls = receipt.get("controls") or []
        result["enforcement"] = dict.fromkeys(controls, True)
        result.setdefault("run_id", run_id)
        with self._lock:
            record["status"] = "completed"
            record["result"] = result
        return HTTPStatus.OK, self._redact(result)

    def _inspect(self, run_id: str) -> tuple[HTTPStatus, dict[str, Any]]:
        self._valid_run_id(run_id)
        with self._lock:
            record = self._runs.get(run_id)
            snapshot = dict(record) if record is not None else None
        if snapshot is None:
            raise _HttpError(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "no such run", "run_id": run_id},
            )
        return HTTPStatus.OK, self._redact(snapshot)

    def _cancel(self, run_id: str) -> tuple[HTTPStatus, dict[str, Any]]:
        self._valid_run_id(run_id)
        try:
            self._resolve_runtime().cancel(run_id)
        except StrictRuntimeUnavailable as exc:
            raise _HttpError(
                HTTPStatus.CONFLICT,
                {"ok": False, "cancelled": False,
                 "error": self._redact(str(exc)), "run_id": run_id},
            ) from None
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id]["status"] = "cancelled"
        return HTTPStatus.OK, {"ok": True, "cancelled": True, "run_id": run_id}

    def _cleanup(self, run_id: str) -> tuple[HTTPStatus, dict[str, Any]]:
        self._valid_run_id(run_id)
        # Idempotent: DockerGVisorRuntime.cleanup is a no-op when nothing owned
        # exists under the name, so a repeated DELETE succeeds.
        try:
            self._resolve_runtime().cleanup(run_id)
        except StrictRuntimeUnavailable as exc:
            raise _HttpError(
                HTTPStatus.CONFLICT,
                {"ok": False, "error": self._redact(str(exc)), "run_id": run_id},
            ) from None
        with self._lock:
            self._runs.pop(run_id, None)
        return HTTPStatus.OK, {"ok": True, "cleaned": True, "run_id": run_id}


def _positive_int(value: Any) -> int:
    """A positive int from an untrusted spec field, else 0 (use the default)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


class _StrictRequestHandler(BaseHTTPRequestHandler):
    """Thin HTTP adapter — reads the body, delegates, writes redacted JSON."""

    protocol_version = "HTTP/1.1"
    server_version = "codecalc-strict/1"

    def _reject_oversized(self, length: int) -> None:
        """Refuse a declared body size we will not buffer, WITHOUT reading it.

        This runs before ``dispatch()`` — before the auth gate — deliberately:
        a declared ``Content-Length`` is not a secret, and the whole point is
        that an unauthenticated caller can never force a giant allocation. The
        connection is then closed rather than kept alive: the unread body is
        still sitting in the socket, and there is no bound on it to safely
        drain, so persistence is not attempted.
        """
        data = json.dumps({
            "ok": False,
            "error": f"request body of {length} bytes exceeds the "
                     f"{MAX_CONTENT_LENGTH} byte ceiling",
        }).encode()
        self.send_response(int(HTTPStatus.REQUEST_ENTITY_TOO_LARGE))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _handle(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_CONTENT_LENGTH:
            self._reject_oversized(length)
            return
        body = self.rfile.read(length) if length > 0 else b""
        status, payload = self.server.service.dispatch(  # type: ignore[attr-defined]
            method, self.path, self.headers, body
        )
        data = json.dumps(payload).encode()
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def log_message(self, *args: Any) -> None:
        """Silenced. The default logs the request line to stderr; a run path is
        not a secret but the Authorization header is, and the safest posture for
        a service holding a static token is to log nothing per-request."""


class StrictHTTPServer(ThreadingHTTPServer):
    """Threaded so a ``cancel``/``inspect`` can be served while an ``execute``
    request is still blocked on its container."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: StrictService) -> None:
        super().__init__(address, _StrictRequestHandler)
        self.service = service


def _flag(argv: list[str], name: str, default: str) -> str:
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return default


def main(argv: list[str]) -> int:
    """`python -m codecalc serve-strict [--host H] [--port P]` — bearer required."""
    import os

    host = _flag(argv, "--host", "127.0.0.1")
    try:
        port = int(_flag(argv, "--port", "8000"))
    except ValueError:
        sys.stderr.write("codecalc serve-strict: --port must be an integer\n")
        return 2

    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host in ("localhost", "ip6-localhost")

    token = os.environ.get(STRICT_SERVICE_TOKEN_ENV, "").strip()
    if not token:
        where = "" if loopback else " on a routable interface"
        sys.stderr.write(
            f"codecalc serve-strict: refusing to start: {STRICT_SERVICE_TOKEN_ENV} "
            f"is not set. The strict execution service authenticates every request "
            f"with a bearer token; a token-less bind{where} would expose "
            f"gVisor-sandboxed code execution unauthenticated. Set it to a strong "
            f"secret and restart.\n"
        )
        return 2

    service = StrictService(token=token)
    # Reconcile containers a crashed previous generation may have leaked, BEFORE
    # admitting any new run — the same guard the Docker plane's recover_orphans
    # applies (owner-labelled only, ownership re-verified before removal). Best
    # effort: a host that cannot resolve the runtime yet still serves /v1/health
    # (which will report not-ready) rather than failing to start.
    try:
        recovered = DockerGVisorRuntime(strict_execution_config()).recover_orphans()
        if recovered:
            sys.stderr.write(
                f"codecalc serve-strict: recovered {len(recovered)} orphaned "
                f"strict container(s)\n"
            )
    except StrictRuntimeUnavailable as exc:
        sys.stderr.write(f"codecalc serve-strict: orphan recovery skipped: {exc}\n")

    server = StrictHTTPServer((host, port), service)
    sys.stderr.write(f"codecalc serve-strict: listening on {host}:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
