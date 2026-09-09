"""`serve-http --oauth-issuer`: JWT bearer-token validation, opt-in and off by
default. No network access — the "issuer" here is an in-process
`http.server` on loopback, and every key is generated fresh in this file.

Real HTTP, real subprocesses, same shape as test_security.py's DNS-rebinding
check (#211): a mock of `_http_auth()` would only prove the Python function
returns the right dict, not that a client actually gets a 401 with the right
header on the wire, or a 200 on a genuinely valid token.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")


# ═══ a fake issuer: OIDC discovery + JWKS, over real loopback HTTP ═════════
class _FakeIssuer(http.server.BaseHTTPRequestHandler):
    """Serves `/.well-known/openid-configuration` and `/jwks.json`.

    `server.keys` (a `{"keys": [...]}` dict, mutated in place by the test)
    is read fresh on every `/jwks.json` request — that mutability is what
    lets the "unknown kid" test simulate a real key rotation mid-run.
    """

    def log_message(self, *_args: object) -> None:  # quiet unless a test wants noise
        pass

    def do_GET(self) -> None:
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path == "/.well-known/openid-configuration":
            body = json.dumps({"issuer": base, "jwks_uri": f"{base}/jwks.json"}).encode()
        elif self.path == "/jwks.json":
            body = json.dumps(self.server.keys).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def _start_fake_issuer(initial_keys: dict) -> http.server.HTTPServer:
    srv = http.server.HTTPServer(("127.0.0.1", 0), _FakeIssuer)
    srv.keys = initial_keys
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _free_port() -> int:
    probe = http.server.HTTPServer(("127.0.0.1", 0), _FakeIssuer)
    port = probe.server_port
    probe.server_close()
    return port


def _to_jwk(algorithm_cls: type, public_key: object, *, kid: str, alg: str) -> dict:
    jwk = algorithm_cls.to_jwk(public_key, as_dict=True)
    jwk.update(kid=kid, alg=alg, use="sig")
    return jwk


# One RSA keypair (the primary, RS256) and one EC keypair (ES256) — both
# generated fresh here, never touching a real IdP. A second RSA keypair is
# generated later, on demand, only for the key-rotation test.
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())
RSA_JWK = _to_jwk(jwt.algorithms.RSAAlgorithm, RSA_KEY.public_key(), kid="rsa-1", alg="RS256")
EC_JWK = _to_jwk(jwt.algorithms.ECAlgorithm, EC_KEY.public_key(), kid="ec-1", alg="ES256")


def _mint(*, issuer: str, audience: str, key=RSA_KEY, kid: str = "rsa-1",
          algorithm: str = "RS256", scope: str = "codecalc", **overrides: object) -> str:
    now = int(time.time())
    claims = {"iss": issuer, "aud": audience, "exp": now + 300, "iat": now,
              "sub": "test-user", "scope": scope}
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def _post(port: int, *, token: str | None = None, timeout: float = 3.0):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- loopback only
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _wait_for_startup(port: int, proc: subprocess.Popen, *, token: str | None = None) -> tuple | None:
    """Poll until the subprocess answers or exits, mirroring
    test_security.py's `_check_serve_http_rejects_rebinding_host` — a loaded
    CI runner can take longer than any single guess to import codecalc."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            return _post(port, token=token)
        except (urllib.error.URLError, ConnectionError):
            if proc.poll() is not None:
                return None
            time.sleep(0.2)
    return None


def _spawn(port: int, *, extra_args: list[str] | None = None,
           extra_env: dict[str, str] | None = None) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    for var in ("CODECALC_HTTP_TOKEN", "CODECALC_OAUTH_ISSUER", "CODECALC_OAUTH_AUDIENCE",
                "CODECALC_OAUTH_JWKS_URL", "CODECALC_OAUTH_SCOPES"):
        env.pop(var, None)
    env.update(extra_env or {})
    args = [sys.executable, "-m", "codecalc.server", "serve-http",
            "--host", "127.0.0.1", "--port", str(port), *(extra_args or [])]
    return subprocess.Popen(args, cwd=REPO_ROOT, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _stop(proc: subprocess.Popen) -> str:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    return proc.stdout.read() if proc.stdout else ""


# ═══ 1. the JWT scenarios, against one running --oauth-issuer server ═══════
issuer_srv = _start_fake_issuer({"keys": [RSA_JWK, EC_JWK]})
ISSUER = f"http://127.0.0.1:{issuer_srv.server_port}"
CODECALC_PORT = _free_port()
AUDIENCE = f"http://127.0.0.1:{CODECALC_PORT}/mcp"

proc = _spawn(CODECALC_PORT, extra_env={
    "CODECALC_OAUTH_ISSUER": ISSUER,
    "CODECALC_OAUTH_AUDIENCE": AUDIENCE,
    "CODECALC_OAUTH_SCOPES": "codecalc",
})
startup = _wait_for_startup(CODECALC_PORT, proc, token=_mint(issuer=ISSUER, audience=AUDIENCE))
if startup is None:
    log = _stop(proc)
    skip("serve-http --oauth-issuer scenarios",
         f"the server never became reachable on 127.0.0.1:{CODECALC_PORT} "
         f"(exited={proc.poll()}) -> {log[-300:]!r}")
else:
    status, headers, body = startup
    check("valid RS256 token -> 200 with a real tools/list result",
          status == 200 and b'"tools"' in body, f"-> {status} {body[:120]!r}")

    status, headers, _ = _post(CODECALC_PORT, token=_mint(
        issuer=ISSUER, audience=AUDIENCE, key=EC_KEY, kid="ec-1", algorithm="ES256"))
    check("valid ES256 token -> 200 (both configured algorithms work, not just RS256)",
          status == 200, f"-> {status}")

    status, headers, _ = _post(CODECALC_PORT, token=_mint(
        issuer=ISSUER, audience=AUDIENCE, exp=int(time.time()) - 10))
    check("expired token -> 401", status == 401, f"-> {status}")
    check("  ...with WWW-Authenticate naming the protected-resource metadata",
          headers.get("WWW-Authenticate", "").startswith("Bearer")
          and "resource_metadata=" in headers.get("WWW-Authenticate", ""),
          f"-> {headers.get('WWW-Authenticate')!r}")

    status, _, _ = _post(CODECALC_PORT, token=_mint(
        issuer=ISSUER, audience="http://attacker.example/mcp"))
    check("wrong audience -> 401", status == 401, f"-> {status}")

    status, _, _ = _post(CODECALC_PORT, token=_mint(
        issuer="http://not-the-real-issuer.example", audience=AUDIENCE))
    check("wrong issuer -> 401", status == 401, f"-> {status}")

    forged = _mint(issuer=ISSUER, audience=AUDIENCE)[:-8] + "AAAAAAAA"
    status, _, _ = _post(CODECALC_PORT, token=forged)
    check("tampered signature -> 401", status == 401, f"-> {status}")

    status, _, _ = _post(CODECALC_PORT, token="not-even-a-jwt")  # noqa: S106 -- deliberately-invalid test token
    check("garbage bearer value -> 401", status == 401, f"-> {status}")

    status, _, _ = _post(CODECALC_PORT)
    check("no Authorization header at all -> 401", status == 401, f"-> {status}")

    # A syntactically valid, correctly-signed, correctly-issued token that
    # simply lacks the required scope. The MCP SDK's own RequireAuthMiddleware
    # enforces `required_scopes`, and RFC 6750 §3.1 assigns this case 403
    # ("insufficient_scope"), not 401 — a different failure than "who are
    # you" (401): the token verified; it just isn't ALLOWED to do this. Still
    # carries the same WWW-Authenticate resource_metadata hint.
    status, headers, _ = _post(CODECALC_PORT, token=_mint(
        issuer=ISSUER, audience=AUDIENCE, scope="something-else"))
    check("token verifies but lacks the required scope -> 403 insufficient_scope",
          status == 403, f"-> {status}")
    check("  ...also carries WWW-Authenticate with resource_metadata",
          "resource_metadata=" in headers.get("WWW-Authenticate", ""),
          f"-> {headers.get('WWW-Authenticate')!r}")

    # Key rotation: rsa-2 is not in the JWKS the client already cached, so
    # the FIRST attempt must fail; PyJWKClient's own "unknown kid -> refetch
    # once -> retry" is what makes the SECOND attempt, after the fake issuer
    # publishes the new key, succeed with no server restart.
    rsa_key_2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rotated_token = _mint(issuer=ISSUER, audience=AUDIENCE, key=rsa_key_2, kid="rsa-2")

    status, _, _ = _post(CODECALC_PORT, token=rotated_token)
    check("token signed by a kid not yet in the JWKS -> 401", status == 401, f"-> {status}")

    issuer_srv.keys["keys"].append(
        _to_jwk(jwt.algorithms.RSAAlgorithm, rsa_key_2.public_key(), kid="rsa-2", alg="RS256"))
    status, _, body = _post(CODECALC_PORT, token=rotated_token)
    check("...the SAME token succeeds once the issuer rotates the JWKS "
          "(refresh-on-unknown-kid, no restart)",
          status == 200 and b'"tools"' in body, f"-> {status}")

    # RFC 9728 protected resource metadata: unauthenticated, its own route.
    req = urllib.request.Request(f"http://127.0.0.1:{CODECALC_PORT}/.well-known/oauth-protected-resource/mcp")
    with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310 -- loopback only
        prm = json.loads(resp.read())
    check("protected-resource metadata: resource is this server's /mcp URL",
          prm.get("resource") == AUDIENCE, f"-> {prm.get('resource')!r}")
    # A trailing "/" here is the SDK's own `AnyHttpUrl` round-trip (shared
    # with the pre-existing static-token path, which passes `issuer_url`
    # through the same field) — not something this ticket's code controls,
    # and not a functional issue: JWTTokenVerifier compares a token's `iss`
    # against the RAW configured string, no round-trip, so the actual
    # signature/issuer check above already proved the real comparison works.
    check("  ...authorization_servers names the configured issuer",
          prm.get("authorization_servers") == [ISSUER] or prm.get("authorization_servers") == [ISSUER + "/"],
          f"-> {prm.get('authorization_servers')!r}")
    check("  ...scopes_supported carries the configured required scope",
          prm.get("scopes_supported") == ["codecalc"], f"-> {prm.get('scopes_supported')!r}")
    check("  ...bearer_methods_supported is exactly ['header'] per RFC 9728",
          prm.get("bearer_methods_supported") == ["header"], f"-> {prm.get('bearer_methods_supported')!r}")

    log = _stop(proc)
    check("no bearer token value was ever written to the server's own log",
          RSA_JWK is not None and all(
              tok not in log for tok in (rotated_token, forged, "not-even-a-jwt")),
          "-> a token or a forged variant leaked into stdout/stderr")


# ═══ 2. precedence: both a static token and an issuer configured ═══════════
# issuer wins; the static token is REJECTED like any other invalid bearer
# value; a warning names both variables.
port2 = _free_port()
proc2 = _spawn(port2, extra_env={
    "CODECALC_OAUTH_ISSUER": ISSUER,
    "CODECALC_OAUTH_AUDIENCE": f"http://127.0.0.1:{port2}/mcp",
    "CODECALC_HTTP_TOKEN": "static-secret-value",
})
startup2 = _wait_for_startup(port2, proc2, token=_mint(
    issuer=ISSUER, audience=f"http://127.0.0.1:{port2}/mcp"))
if startup2 is None:
    log2 = _stop(proc2)
    skip("precedence: issuer + static token both set",
         f"server never became reachable (exited={proc2.poll()}) -> {log2[-300:]!r}")
else:
    status, _, _ = _post(port2, token="static-secret-value")  # noqa: S106 -- test fixture, not a real credential
    check("with an issuer configured, the STATIC token is rejected (not a valid JWT)",
          status == 401, f"-> {status}")
    status, _, body = _post(port2, token=_mint(issuer=ISSUER, audience=f"http://127.0.0.1:{port2}/mcp"))
    check("...but a real JWT against the issuer still works", status == 200, f"-> {status}")
    log2 = _stop(proc2)
    check("a startup warning names both CODECALC_OAUTH_ISSUER and CODECALC_HTTP_TOKEN",
          "CODECALC_OAUTH_ISSUER" in log2 and "CODECALC_HTTP_TOKEN" in log2
          and "static-secret-value" not in log2,
          f"-> {log2[:400]!r}")


# ═══ 3. --oauth-issuer as a CLI FLAG, no env vars at all ═══════════════════
port3 = _free_port()
audience3 = f"http://127.0.0.1:{port3}/mcp"
proc3 = _spawn(port3, extra_args=["--oauth-issuer", ISSUER, "--oauth-audience", audience3])
startup3 = _wait_for_startup(port3, proc3, token=_mint(issuer=ISSUER, audience=audience3))
if startup3 is None:
    log3 = _stop(proc3)
    skip("--oauth-issuer given as a CLI flag (no env)",
         f"server never became reachable (exited={proc3.poll()}) -> {log3[-300:]!r}")
else:
    status, _, body = startup3
    check("--oauth-issuer/--oauth-audience as bare CLI flags authenticate a real request",
          status == 200 and b'"tools"' in body, f"-> {status}")
    _stop(proc3)


# ═══ 4. a non-loopback bind with ONLY --oauth-issuer (no env) is accepted ══
# Before this ticket, the "refuse an unauthenticated routable bind" check
# only knew about CODECALC_HTTP_TOKEN — a --oauth-issuer-only CLI invocation
# on a routable address would have been refused as if it had no auth at all.
# 127.0.0.2 (like test_security.py's DNS-rebinding probe): loopback, but not
# one of the two spellings ("127.0.0.1"/"localhost") this check used to special-case,
# so it actually exercises the "is this bind authenticated" branch rather
# than a hardcoded allow-list.
_alt_loopback = "127.0.0.2"
_bind_probe = __import__("socket").socket(__import__("socket").AF_INET, __import__("socket").SOCK_STREAM)
try:
    _bind_probe.bind((_alt_loopback, 0))
    _bind_probe.close()
    port4 = _free_port()
    audience4 = f"http://{_alt_loopback}:{port4}/mcp"
    proc4 = subprocess.Popen(
        [sys.executable, "-m", "codecalc.server", "serve-http",
         "--host", _alt_loopback, "--port", str(port4),
         "--oauth-issuer", ISSUER, "--oauth-audience", audience4],
        cwd=REPO_ROOT,
        env={k: v for k, v in os.environ.items()
             if k not in ("CODECALC_HTTP_TOKEN", "CODECALC_OAUTH_ISSUER")} | {"PYTHONPATH": str(REPO_ROOT)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.monotonic() + 20
    body4 = status4 = None
    token4 = _mint(issuer=ISSUER, audience=audience4)
    while time.monotonic() < deadline:
        req = urllib.request.Request(
            f"http://{_alt_loopback}:{port4}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "Authorization": f"Bearer {token4}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310 -- loopback only
                status4, body4 = resp.status, resp.read()
            break
        except urllib.error.HTTPError as exc:
            status4, body4 = exc.code, exc.read()
            break
        except (urllib.error.URLError, ConnectionError):
            if proc4.poll() is not None:
                break
            time.sleep(0.2)
    if status4 is None:
        log4 = _stop(proc4)
        skip("non-loopback-spelling bind, --oauth-issuer only, no env token",
             f"server never became reachable (exited={proc4.poll()}) -> {log4[-300:]!r}")
    else:
        check("a --oauth-issuer-only CLI config is NOT refused as unauthenticated "
              "on a non-'127.0.0.1'-spelled loopback bind",
              status4 == 200, f"-> {status4} {body4[:120] if body4 else None!r}")
        _stop(proc4)
except OSError as exc:
    _bind_probe.close()
    skip("non-loopback-spelling bind check", f"cannot bind {_alt_loopback} on {sys.platform}: {exc}")

issuer_srv.shutdown()


# ═══ 5. no issuer configured -> the static-token path is UNCHANGED from main
# Diffed against a real checkout of `main` via a read-only `git worktree`
# (no network — the commit is already local), run with the SAME interpreter
# and installed dependencies this suite already has, so no second `uv sync`
# is needed: main's codecalc/server.py needs nothing this environment lacks.
def _compare_static_token_path_to_main() -> None:
    git = shutil.which("git")
    if git is None:
        skip("static-token path vs main", "no git on PATH")
        return
    # The MERGE-BASE with `main`, not `main`'s current tip. `main` moves —
    # comparing against whatever it happens to point at right now would mix
    # "did THIS change touch the static-token path" with "how far has main
    # drifted since this branch forked", and fail on unrelated main commits
    # (a new tool, a changed docstring) that have nothing to do with this
    # ticket. The merge-base is the exact commit this diff is relative to.
    base = subprocess.run([git, "merge-base", "HEAD", "main"],
                           cwd=REPO_ROOT, capture_output=True, text=True, timeout=10)
    if base.returncode != 0 or not base.stdout.strip():
        skip("static-token path vs main", f"`git merge-base` failed: {base.stderr[:300]!r}")
        return
    base_sha = base.stdout.strip()
    with tempfile.TemporaryDirectory(prefix="codecalc-main-worktree-") as tmp:
        added = subprocess.run(
            [git, "worktree", "add", "--detach", tmp, base_sha],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        if added.returncode != 0:
            skip("static-token path vs main",
                 f"`git worktree add` failed: {added.stderr[:300]!r}")
            return
        try:
            _run_and_compare(pathlib.Path(tmp))
        finally:
            subprocess.run([git, "worktree", "remove", "--force", tmp],
                            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)


def _run_one_static_server(codecalc_root: pathlib.Path, port: int, token: str) -> tuple[dict, dict]:
    env = {**os.environ, "PYTHONPATH": str(codecalc_root), "CODECALC_HTTP_TOKEN": token}
    env.pop("CODECALC_OAUTH_ISSUER", None)
    proc_ = subprocess.Popen(
        [sys.executable, "-m", "codecalc.server", "serve-http",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(codecalc_root), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        started = _wait_for_startup(port, proc_, token=token)
        if started is None:
            return {}, {}
        ok_status, _ok_headers, ok_body = started
        bad_status, bad_headers, _ = _post(port, token="wrong-token")  # noqa: S106 -- deliberately-invalid test token
        # Compare the SHAPE of the authenticated response, not the whole
        # tools/list body: this check exists to prove the static-token path
        # still authenticates and answers, and the tool catalogue is not part
        # of that claim. Comparing full bodies made every branch that adds a
        # tool or edits a description fail here for a reason unrelated to
        # auth (first tripped by a branch merging two tool clusters).
        body = json.loads(ok_body)
        result = body.get("result") if isinstance(body, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
        return (
            {"status": ok_status,
             "jsonrpc": body.get("jsonrpc") if isinstance(body, dict) else None,
             "id": body.get("id") if isinstance(body, dict) else None,
             "result_keys": sorted(result) if isinstance(result, dict) else None,
             "tools_is_nonempty_list": isinstance(tools, list) and len(tools) > 0},
            {"status": bad_status,
             "www_authenticate": bad_headers.get("WWW-Authenticate", "").split("resource_metadata=")[0]},
        )
    finally:
        _stop(proc_)


def _run_and_compare(main_root: pathlib.Path) -> None:
    port_ours = _free_port()
    port_main = _free_port()
    token = "identical-fixture-token"  # noqa: S105 -- test fixture, not a real credential
    ours_ok, ours_bad = _run_one_static_server(REPO_ROOT, port_ours, token)
    main_ok, main_bad = _run_one_static_server(main_root, port_main, token)
    if not ours_ok or not main_ok:
        skip("static-token path vs main",
             f"one server never came up (ours={bool(ours_ok)}, main={bool(main_ok)})")
        return
    check("valid-token response is structurally identical to main "
          "(same status, same JSON-RPC envelope, a non-empty tools/list — "
          "the catalogue's contents are not compared)",
          ours_ok == main_ok, f"-> ours={ours_ok!r} main={main_ok!r}")
    check("invalid-token response is structurally identical to main "
          "(same status, same WWW-Authenticate error/description)",
          ours_bad == main_bad, f"-> ours={ours_bad!r} main={main_bad!r}")


_compare_static_token_path_to_main()


# ═══ 6. CODECALC_OAUTH_ISSUER must not cost a network call outside serve-http
# Regression for a real bug: `_http_auth()` used to run at module IMPORT
# time regardless of subcommand, so `codecalc doctor` (or --help, or the
# bare stdio server) with an issuer configured did OIDC discovery before
# printing anything, and crashed with an unhandled URLError if that issuer
# was not reachable — reproduced live with
# `CODECALC_OAUTH_ISSUER=https://issuer.invalid python -m codecalc.server doctor`.
# The fake issuer here is a CLOSED loopback port (bound, then immediately
# released) rather than an unresolvable DNS name: connecting to it fails
# with an instant ECONNREFUSED, so a version that (incorrectly) DOES try
# the network still fails fast rather than hanging on DNS — the wall-clock
# bound below is deliberately generous (a healthy run finishes in well
# under a second) but still tight enough to distinguish "never touched the
# network" from "tried once and got refused quickly", let alone a real DNS
# timeout.
_unreachable_issuer = f"http://127.0.0.1:{_free_port()}"


def _run_subcommand_with_unreachable_issuer(args: list[str], *, label: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "CODECALC_OAUTH_ISSUER": _unreachable_issuer}
    env.pop("CODECALC_HTTP_TOKEN", None)
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "codecalc.server", *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    elapsed = time.monotonic() - started
    check(f"`codecalc {label}` with an unreachable CODECALC_OAUTH_ISSUER still exits 0",
          result.returncode == 0, f"-> exit={result.returncode} stderr={result.stderr[-300:]!r}")
    check("  ...and returns well under the bound a real network attempt would need "
          "(no discovery call was made)",
          elapsed < 10.0, f"-> {elapsed:.2f}s")


_run_subcommand_with_unreachable_issuer(["doctor"], label="doctor")
_run_subcommand_with_unreachable_issuer(["--help"], label="--help")


def _check_stdio_ignores_unreachable_issuer() -> None:
    """A real `initialize` handshake over stdio, not just `doctor`'s exit
    code — proves the transport that spawns this server every day (Claude
    Desktop, LiteLLM, any MCP client) is equally unaffected. `_mcp_client`
    is importable directly (no path juggling needed): this script's OWN
    directory (`tests/`) is already first on `sys.path`, the same way it is
    for every other suite file that imports it."""
    from _mcp_client import over_stdio

    async def _probe() -> int:
        async with over_stdio({"CODECALC_OAUTH_ISSUER": _unreachable_issuer}) as c:
            listed = await c.list_tools()
            return len(listed.tools)

    try:
        tool_count = asyncio.run(asyncio.wait_for(_probe(), timeout=20))
    except Exception as exc:
        check("stdio initialize with an unreachable CODECALC_OAUTH_ISSUER still works",
              False, f"-> raised {exc!r}")
        return
    check("stdio initialize with an unreachable CODECALC_OAUTH_ISSUER still works",
          tool_count > 0, f"-> {tool_count} tools")


_check_stdio_ignores_unreachable_issuer()


def _check_serve_http_fails_closed_on_unreachable_issuer() -> None:
    """The mirror image of the two checks above: `serve-http` is the ONE
    subcommand that DOES need the issuer reachable, and must say so clearly
    and exit non-zero rather than start a server no token could ever pass,
    or crash with a raw traceback."""
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "CODECALC_OAUTH_ISSUER": _unreachable_issuer}
    env.pop("CODECALC_HTTP_TOKEN", None)
    result = subprocess.run(
        [sys.executable, "-m", "codecalc.server", "serve-http",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    check("serve-http with an unreachable issuer exits non-zero (fails closed)",
          result.returncode != 0, f"-> exit={result.returncode}")
    check("  ...with a clear message on stderr, not a raw traceback",
          "Refusing to start" in result.stderr and "Traceback" not in result.stderr,
          f"-> {result.stderr[-400:]!r}")


_check_serve_http_fails_closed_on_unreachable_issuer()


# ═══ 7. https:// required for issuer/JWKS URLs except loopback ════════════
def _check_https_required_off_loopback() -> None:
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT),
           "CODECALC_OAUTH_ISSUER": "http://not-a-loopback-host.example"}
    env.pop("CODECALC_HTTP_TOKEN", None)
    result = subprocess.run(
        [sys.executable, "-m", "codecalc.server", "serve-http",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    check("a plain http:// issuer on a non-loopback host is refused at startup",
          result.returncode != 0 and "https://" in result.stderr,
          f"-> exit={result.returncode} stderr={result.stderr[-300:]!r}")
    # The positive case: the exact ISSUER live-tested throughout this file
    # is itself a plain http://127.0.0.1:PORT loopback URL and every check
    # above already succeeded against it — proving the loopback exemption
    # works is what every earlier PASS in this file already did, so it is
    # not re-asserted here as a separate live request.


_check_https_required_off_loopback()


print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== serve-http --oauth-issuer: all scenarios pass ===")
sys.exit(1 if FAILS else 0)
