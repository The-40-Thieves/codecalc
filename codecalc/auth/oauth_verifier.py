"""JWT bearer-token verification for `serve-http --oauth-issuer`.

Everything the MCP SDK needs to turn this into working auth — the 401 with a
`WWW-Authenticate: Bearer resource_metadata="..."` header, the RFC 9728
`/.well-known/oauth-protected-resource` route, the per-request scope check —
already exists in `mcp.server.auth` and is wired from `codecalc/server.py`'s
`_http_auth()`. The one piece the SDK does not ship is a `TokenVerifier` that
actually decides whether a presented JWT is valid, because that decision is
issuer-specific (which keys, which claims) in a way a general-purpose SDK
cannot pre-bake. This module is that piece.

Signature verification, key fetch-and-cache (including "unknown kid ->
refetch the JWKS once -> retry"), is `jwt.PyJWKClient` — not hand-rolled here.
It already does exactly what an issuer key ROTATION needs: a `kid` absent
from the cached JWK Set forces one synchronous refetch before giving up. The
one piece PyJWT does not do is turn an issuer URL into a JWKS URL; that is
OpenID Connect Discovery (`/.well-known/openid-configuration`'s `jwks_uri`),
plain enough to not need a library either.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

#: RFC 7517 algorithms this verifier accepts. Deliberately not "whatever the
#: token's own `alg` header says" (that is the classic `alg: none` /
#: algorithm-confusion hole PyJWT's `algorithms=` allowlist exists to close):
#: only the two asymmetric algorithms an OAuth-flavoured IdP actually issues.
ALLOWED_ALGORITHMS = ("RS256", "ES256")

#: OIDC Discovery §4.1 — a fixed, well-known PATH, not a host. No scheme or
#: host is hardcoded here; both come from the operator-supplied issuer URL at
#: call time, so this is not the "no outbound URL literal" tripwire
#: tests/test_offline.py's sibling check (`check_portability.py`) is built to
#: catch — it is the suffix appended to whatever issuer was configured.
DISCOVERY_PATH = "/.well-known/openid-configuration"


def _is_loopback_host(host: str) -> bool:
    """Same loopback test `codecalc/server.py`'s `--host` handling uses for
    its own DNS-rebinding decision — `ipaddress`, not a spelling list, so
    127.0.0.2 is exactly as loopback as 127.0.0.1. `urlsplit(...).hostname`
    is already lowercased and stripped of IPv6 brackets, so it feeds
    `ip_address` directly."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "ip6-localhost")


def require_https_or_loopback(url: str, *, what: str) -> None:
    """Refuse a plain-`http://` issuer or JWKS URL unless it names a loopback
    host. A bearer token is exactly as valuable as a password over the wire
    it travels — RFC 8414 §3.1 already requires HTTPS for an authorization
    server's own metadata for this reason, and this project's threat model
    (SECURITY.md) is "one operator, put a real TLS boundary in front of
    anything hosted" — but `http://127.0.0.1:PORT` is also this whole test
    suite's fake issuer, and codecalc's own README examples default to
    loopback everywhere else. `discover_jwks_uri` and `JWTTokenVerifier`
    both call this — once for the issuer, once for whichever JWKS URL is
    actually used (explicit `--oauth-jwks-url` or the discovered one) — so
    neither can be configured with a plaintext non-loopback endpoint.
    """
    parsed = urlsplit(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and _is_loopback_host(parsed.hostname or ""):
        return
    raise ValueError(
        f"{what} must be https:// (plain http:// is only accepted for a "
        f"loopback host, for local testing): {url!r}"
    )


def discover_jwks_uri(issuer: str, *, timeout: float = 5.0) -> str:
    """Fetch `issuer`'s OpenID Connect discovery document and return `jwks_uri`.

    Called once, synchronously, at `serve-http` startup — before the event
    loop that will serve requests exists — when `--oauth-jwks-url` was not
    given. A network or shape failure here is a startup error, not a 401: an
    operator who configured `--oauth-issuer` wrong should see that on the
    command line, not learn it from every request being unauthenticated.

    ONLY at `serve-http` startup: this is called exclusively from
    `codecalc/server.py`'s `_http_auth()`, which is itself called exclusively
    from `serve-http`'s own branch of `main()` — never at module import, so
    `doctor`, `--help`, `serve-strict`, and the bare stdio server never pay
    this network call even when `CODECALC_OAUTH_ISSUER` happens to be set in
    the environment they inherit. (It used to run at import for every
    subcommand; tests/test_serve_http_oauth.py's "no network call outside
    serve-http" section is the regression test for that.)
    """
    require_https_or_loopback(issuer, what="the OAuth issuer")
    discovery_url = issuer.rstrip("/") + DISCOVERY_PATH
    request = Request(  # noqa: S310 -- scheme is checked by require_https_or_loopback above
        discovery_url, headers={"Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 -- see above
        document = json.loads(response.read().decode("utf-8"))
    jwks_uri = document.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise ValueError(f"OIDC discovery document at {discovery_url} has no 'jwks_uri'")
    return jwks_uri


class JWTTokenVerifier(TokenVerifier):
    """Verifies a bearer token as a JWT signed by `issuer`'s own JWKS.

    Checks, all via `jwt.decode`'s own options (nothing here re-implements
    them): signature (against a key `jwt.PyJWKClient` resolves by the
    token's `kid`, refreshing its cache once on a `kid` it has not seen —
    the key-rotation path), `iss`, `aud`, `exp`, and `nbf`/`iat` if present.
    Required-SCOPE enforcement is deliberately NOT here: `AuthSettings`'s
    `required_scopes` plus the SDK's `RequireAuthMiddleware` already do that,
    against the `scopes` this returns, so duplicating the check here would
    just be a second place for the two to drift.

    Every failure mode — bad signature, wrong issuer, wrong audience,
    expired, not-yet-valid, unknown `kid` even after a refresh, a JWKS
    endpoint that is unreachable — returns `None`, never raises. The SDK
    turns a `None` into 401 with `WWW-Authenticate: Bearer
    resource_metadata="..."`; a raised exception would instead be an
    unhandled-error 500 on every request from a client with a bad token,
    which is both a worse failure mode and a bigger information leak.

    Never logs the token: not on success, not on any failure branch, not in
    an exception message (the branches below catch before a token could ever
    reach one).
    """

    def __init__(self, *, issuer: str, audience: str, jwks_url: str) -> None:
        # Validated here too, not only for the issuer at discovery time: an
        # explicit `--oauth-jwks-url` never goes through `discover_jwks_uri`
        # at all, and a discovered `jwks_uri` could in principle name a
        # different host than the issuer it came from.
        require_https_or_loopback(jwks_url, what="the JWKS URL")
        self._issuer = issuer
        self._audience = audience
        # cache_jwk_set=True (5 minute TTL) is PyJWKClient's own default; kept
        # explicit here because it is the setting this whole class leans on.
        self._jwks_client = jwt.PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300)

    async def verify_token(self, token: str) -> AccessToken | None:
        # PyJWKClient is synchronous (stdlib `urllib.request` under the hood,
        # same as discover_jwks_uri above) — off the event loop, so one slow
        # or stalled JWKS fetch cannot stall every other in-flight request.
        try:
            signing_key = await asyncio.to_thread(
                self._jwks_client.get_signing_key_from_jwt, token,
            )
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp"]},
            )
        except jwt.PyJWTError:
            # Covers every jwt.decode failure (bad signature, expired,
            # wrong iss/aud, immature) AND PyJWKClient's own errors
            # (PyJWKClientError/PyJWKClientConnectionError both subclass
            # PyJWTError) — an unknown kid that a refresh still can't find,
            # or a JWKS endpoint that is down.
            return None

        scope_claim = claims.get("scope")
        if isinstance(scope_claim, str):
            scopes = scope_claim.split()
        elif isinstance(claims.get("scp"), list):
            # A second convention some issuers use (Azure AD/Entra among
            # them) for the same information, as an array instead of a
            # space-delimited string.
            scopes = [str(s) for s in claims["scp"]]
        else:
            scopes = []

        expires_at = claims.get("exp")
        client_id = claims.get("client_id") or claims.get("azp") or claims.get("sub") or "oauth-client"
        return AccessToken(
            token=token,
            client_id=str(client_id),
            scopes=scopes,
            expires_at=int(expires_at) if expires_at is not None else None,
            resource=self._audience,
            subject=claims.get("sub"),
            claims=claims,
        )
