"""MCP server exposing codecalc as model-usable tools.

Built on the official SDK (`mcp` 2.0), protocol revision **2026-07-28**.

Transport: stdio by default (what LiteLLM / Claude Desktop / any MCP client
spawns). Run:  python -m codecalc.server

A note on verifying the protocol version, because it is easy to get wrong:
`mcp.types.LATEST_PROTOCOL_VERSION` reads "2026-07-28" regardless of what any
given connection negotiates. A client that connects with the OLD
`ClientSession.initialize()` handshake negotiates **2025-11-25** against this
same server — 2026-07-28 removed that handshake in favour of a stateless core.
Only `mcp.Client` (mode "auto" or "2026-07-28") actually gets you the new
protocol. tests/test_mcp_protocol.py asserts the negotiated value from a real
connection for exactly that reason.
"""

from __future__ import annotations

import base64
import functools
import inspect
import os
from pathlib import Path

from mcp.server import CacheHint, MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ImageContent

from . import (
    __version__,
    capabilities,
    complexity,
    contract,
    doctor,
    errors,
    exact,
    execution_service,
    executor,
    grades,
    logic,
    optimization,
    packages,
    providers,
    run_supervisor,
    runtimes,
    sessions,
    tools,
    translation,
    units,
)
from . import (
    audit as audit_module,
)
from .mcp_middleware import timeout_middleware

#: Bearer token for the Streamable HTTP transport (THE-786). Unset means the
#: transport is loopback-only: `serve-http` REFUSES a non-loopback bind
#: without it, because a token-less bind on a routable interface exposes 51
#: unauthenticated code-execution tools to whatever the interface reaches.
#: stdio ignores this entirely — auth is HTTP middleware, and an MCP client
#: spawning the server over stdio is already inside the trust boundary.
HTTP_TOKEN_ENV = "CODECALC_HTTP_TOKEN"  # noqa: S105 -- the env var's NAME, not a credential

#: What the auth metadata advertises as this server's own URL. Only consulted
#: when a token is set. Loopback default rather than a public placeholder: the
#: package bans public URL literals (tests/test_offline.py), and the operator
#: binding a real interface knows the real URL.
HTTP_URL_ENV = "CODECALC_HTTP_URL"


def _http_auth() -> dict:
    """Constructor kwargs wiring bearer auth into the server, or {}.

    Computed at import because `token_verifier`/`auth` are constructor-only in
    the SDK (both must be supplied together), and the module-level server below
    is the one every transport serves. The token comparison is constant-time:
    a timing oracle on an auth check is the classic way a static token leaks.

    The SDK's `BearerAuthBackend` does the header parsing and 401s; this
    verifier is the part that is OURS — it decides what counts as valid.
    """
    import hmac

    token = os.environ.get(HTTP_TOKEN_ENV, "")
    if not token:
        return {}

    from mcp.server.auth.provider import AccessToken, TokenVerifier
    from mcp.server.auth.settings import AuthSettings
    from pydantic import AnyHttpUrl

    class _StaticTokenVerifier(TokenVerifier):
        async def verify_token(self, presented: str) -> AccessToken | None:
            if not hmac.compare_digest(presented.encode(), token.encode()):
                return None
            return AccessToken(token=presented, client_id="codecalc-operator",
                               scopes=["codecalc"])

    base = os.environ.get(HTTP_URL_ENV, "").strip() or "http://127.0.0.1:8000"
    return {
        "token_verifier": _StaticTokenVerifier(),
        "auth": AuthSettings(
            issuer_url=AnyHttpUrl(base),
            resource_server_url=AnyHttpUrl(base.rstrip("/") + "/mcp"),
            required_scopes=["codecalc"],
        ),
    }


_provider_registry = providers.configured_registry()
# Unconditional (THE-778). Previously built only when a "managed_runs"
# provider (today: RemoteStrictExecutionProvider) was configured, because the
# only consumer was ExecutionService.execute()'s managed-provider branch,
# which itself checks `self.supervisor is None` before using it — so building
# one for every OTHER provider selection was a genuine no-op. run_submit/
# run_inspect/run_cancel below are the first consumer that needs a supervisor
# for the DEFAULT `local` provider too (RunSupervisor.start() already runs a
# non-managed provider's plain `execute()` on a thread-pool worker — see its
# own docstring — so nothing about managed_runs is actually required here).
_run_state_dir = Path(os.environ.get(
    "CODECALC_RUN_STATE_DIR", Path.home() / ".codecalc" / "runs"
))


def _positive_int_env(name: str, default: int) -> int:
    """A positive integer from the environment, or `default` — never a raise.

    `int(os.environ.get(NAME, "64"))` defaults only when the variable is
    ABSENT. Set-but-EMPTY is a different thing and a common one: `export
    CODECALC_MAX_ACTIVE_RUNS=` in a shell profile, a blank value in a compose
    file or a systemd `Environment=` line all produce it, and it reached
    `int("")` -> ValueError at MODULE IMPORT with nothing above it to catch —
    the server did not start at all. Non-numeric and non-positive values did
    the same thing one line later, since RunSupervisor rejects a
    max_active_runs that is not positive.

    Falls back rather than refusing to start, because the failure this
    replaces was itself a refusal to start over a knob nobody has to set —
    but LOUDLY on stderr, so an operator who meant to raise the cap is not
    left believing a typo took effect. Same shape as the allowlist
    empty-string handling in `packages`, which treats a blank entry as absent
    instead of as a value.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        import sys
        print(f"codecalc: ignoring {name}={raw!r} — expected a positive "
              f"integer; using the default of {default}", file=sys.stderr)
        return default
    return value


#: Admission cap for run_submit (THE-778 fix round, review Important #3a).
#: Bounds RunSupervisor's in-memory run table and thread pool against an
#: unbounded burst of submissions, or a caller that never inspects/cancels
#: what it starts — nothing else on this path rejects a submission. 64
#: matches RunSupervisor's own constructor default; the env var lets an
#: operator raise or lower it without a code change, same pattern as
#: CODECALC_RUN_STATE_DIR above.
_max_active_runs = _positive_int_env("CODECALC_MAX_ACTIVE_RUNS", 64)
_run_supervisor = run_supervisor.RunSupervisor(
    _provider_registry, state_dir=_run_state_dir, max_active_runs=_max_active_runs
)
_run_supervisor.recover_orphans()
# THE-787: one append-only audit sink for broker decisions and security-relevant
# side effects (denied capability, refused install, cleanup). Defaults to
# ~/.codecalc/audit/audit.log; CODECALC_AUDIT_LOG relocates it, or disables it
# when set empty. Best effort — a write failure never fails a run.
def _audit_secrets() -> tuple[str, ...]:
    """The provider-auth credential VALUES to redact by value from audit events.

    Events carry only metadata today, so nothing leaks — but the "redacted of
    secrets" guarantee has to be ARMED to exist. The same env vars the providers
    read for their Authorization headers are registered here, both the whole
    header value and its credential portion (after the scheme), mirroring how
    PistonExecutionProvider / RemoteStrictExecutionProvider build their own
    redaction sets.
    """
    values: set[str] = set()
    for env_name in (providers.STRICT_AUTHORIZATION_ENV,
                     providers.PISTON_AUTHORIZATION_ENV, HTTP_TOKEN_ENV):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            values.add(raw)
            values.add(raw.split(maxsplit=1)[-1])  # the credential after "Bearer "
    return tuple(v for v in values if v)


_audit_log = audit_module.from_env(secrets=_audit_secrets())
_execution_service = execution_service.ExecutionService(
    _provider_registry, supervisor=_run_supervisor, audit=_audit_log
)
_session_service = execution_service.SessionService()

mcp = MCPServer(
    name="codecalc",
    version=__version__,
    # ttlMs/cacheScope became REQUIRED on list and read results in 2026-07-28
    # (SEP-2549). They are a freshness hint that lets a client cache instead of
    # re-listing; "public" is right here because this server has no per-caller
    # authorization, so one client's tool list is every client's.
    #
    # These are silently dropped on a legacy (2025-11-25) connection — verified:
    # the same server returns ttl_ms=0/private under the old handshake and
    # 60000/public under 2026-07-28. Do not read a zero here as "not configured".
    cache_hints={
        "tools/list": CacheHint(ttl_ms=60_000, scope="public"),
        "resources/list": CacheHint(ttl_ms=10_000, scope="public"),
        "resources/templates/list": CacheHint(ttl_ms=60_000, scope="public"),
        # resources/read is per-session workspace content and changes as
        # executed code writes files, so it must not be cached.
        "resources/read": CacheHint(ttl_ms=0, scope="private"),
        "server/discover": CacheHint(ttl_ms=60_000, scope="public"),
    },
    middleware=[timeout_middleware],
    # Bearer auth for the HTTP transport, absent unless CODECALC_HTTP_TOKEN is
    # set. Inert over stdio either way — see _http_auth.
    **_http_auth(),
    # `instructions` is metadata every MCP client receives on connect, before
    # it has called a single tool — the least invasive surface to put backend
    # visibility on. list_languages() was the other candidate and was
    # rejected: it returns a `list[dict]`, one entry per language, with no
    # natural top-level slot for a server-wide field, and a caller only sees
    # it if they think to call that specific tool. This f-string is evaluated
    # once, at import time, after `executor` above has already resolved
    # `_rust` (and, with CODECALC_REQUIRE_NATIVE=1, already refused to import
    # at all if it came up empty) — so what it reports is what this process
    # actually has, not a static claim that can drift from it.
    instructions=(
        "Universal coding & logic calculator. Tools: list_languages (available "
        "runtimes), execute_code (run code in 30+ languages, returns stdout/"
        "stderr/exit/time), evaluate_expression (symbolic math via SymPy), "
        "truth_table (boolean logic), z3_check (SMT-LIB2 satisfiability), "
        "solve_linear (systems of equations), analyze_complexity (static Big-O), "
        "benchmark (empirical Big-O by running at increasing sizes), "
        "compare_execution (same code across many languages). "
        f"Execution backend: {executor.backend()} (rust = full sandbox "
        "including no_net; python = fallback, no_net and peak_memory_kb "
        "unenforced — see CODECALC_REQUIRE_NATIVE)."
    ),
)



def _coded(fn):
    """Attach an error code to any failing dict a tool returns.

    Wraps at REGISTRATION so all 51 tools are covered by one change instead of
    121 edits at the return sites. The first attempt put this on
    `guarded_call`, which measured 0 of 8 on the most reachable failures
    because those paths return rather than raise — see errors.py.

    Codes attached here are inferred from the message and marked
    `code_inferred: true`. That is a weaker claim than one chosen at a raise
    site, and the label is what keeps the two distinguishable.
    """
    # THE contract stamp goes here too, and this — not executor.execute — is the
    # only place that reaches every result.
    #
    # The first version stamped `contract_version` inside executor.execute,
    # reasoning that both backends pass through it. Both EXECUTOR backends do.
    # Three tool surfaces do not: compact_result rebuilds the dict afterwards,
    # the native streaming path returns the executor's JSON directly, and
    # `execute_code(session_id=...)` routes to a warm worker that never calls
    # executor.execute at all. All three returned unversioned results while the
    # documentation said every result carries a version.
    #
    # This wrapper is applied to all 51 tools, so stamping here makes that claim
    # true by construction. `stamp` uses setdefault, so the executor's own stamp
    # is not overwritten and the two cannot disagree.
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def _async(*args, **kwargs):
            return contract.stamp(errors.ensure_code(await fn(*args, **kwargs)))
        return _async

    @functools.wraps(fn)
    def _sync(*args, **kwargs):
        return contract.stamp(errors.ensure_code(fn(*args, **kwargs)))
    return _sync


# Rebind `mcp.tool` rather than renaming 51 decorator lines. The rename was the
# first attempt and broke four suites plus the CI round-trip check, all of which
# count declarations with `grep -c '^@mcp\.tool'` and read 0 against 51 served.
# That identity is load-bearing here, so the change that preserves it is the
# right one: every `@mcp.tool()` below is unchanged and every counter still
# works, while the wrapper is applied underneath.
_mcp_tool = mcp.tool


def _tool(*d_args, **d_kwargs):
    def deco(fn):
        return _mcp_tool(*d_args, **d_kwargs)(_coded(fn))
    return deco


mcp.tool = _tool


@mcp.tool()
def list_languages() -> list[dict]:
    """List every language codecalc can execute, with extension, compile flag, and what this machine resolved. `status` is `installed` (its command was found on the sandbox PATH) or `supported` (nothing for it here); `status_basis` is `resolved`, meaning nothing was executed to check. Run `codecalc doctor --deep` to promote a runtime to `available` by actually running it."""
    return executor.catalog()


@mcp.tool()
def list_execution_providers() -> list[dict]:
    """List execution providers and their machine-readable capabilities."""
    return _provider_registry.descriptors()


#: What a compact result ALWAYS carries.
_COMPACT_ALWAYS = ("ok", "verdict", "stdout", "exit_code")

#: Disclosure and provenance fields, carried at EVERY verbosity whenever they
#: say something. Provider identity is part of the execution receipt, not a
#: diagnostic detail, so compact mode must retain it too.
#:
#: These are the reason compact mode was a defect rather than a trade-off (#117).
#: The old implementation returned exactly _COMPACT_ALWAYS, so
#: `execute_code(no_net=True, compact=True)` came back as
#: `{"ok": true, "verdict": "OK", ...}` on a platform where the LD_PRELOAD shim
#: cannot be applied — a successful-looking result for a guarantee that was
#: never applied, which is the one thing SECURITY.md puts in scope by name.
#:
#: Splitting by KIND rather than by size is the whole fix: `workdir` and
#: `total_ms` are diagnostics a caller can live without, and `unenforced` is
#: not. An empty `unenforced` is omitted because it costs tokens to say
#: nothing; a non-empty one is the entire point of the field.
#:
#: The `stdout_spill`/`stderr_spill` pair (THE-783) belongs here for the same
#: reason: compact mode already keeps a truncated `stdout` prefix via
#: _COMPACT_ALWAYS, and dropping the pointer to the FULL stream on top of
#: that would make "spill instead of just truncating" true for every caller
#: except the ones who asked for the small reply — exactly backwards, since a
#: compact caller is the one least likely to have the full text any other
#: way.
_COMPACT_DISCLOSURE = (
    "unenforced", "output_error", "provider",
    "stdout_spill", "stderr_spill", "stdout_spill_capped", "stderr_spill_capped",
)

#: The receipt keys compact mode keeps, in the order they are emitted.
#:
#: THE-782 grew the receipt from provider identity to provider identity PLUS
#: content hashes and determinism inputs. Copied wholesale, that took a compact
#: result from 603 to 1019 bytes — a 69% increase in the one mode whose entire
#: purpose is not spending tokens, and past the bar this file already set when
#: it measured a disclosure at 171 of a 199-token reply and called that a defect.
#:
#: So the same split applies here as to `unenforced`: keep what a caller must
#: ACT on, drop what they can fetch. `limits` stays whole because it is the
#: unapplied-guarantee half that #117 exists for, and `spec_hash` stays because
#: naming which request this was is what THE-782 added. `provider_version`,
#: `interface_version`, `host_class`, `source_sha256`, `spec_schema_version` and
#: the determinism block are descriptive, unchanged between calls, and one
#: non-compact call away — which the caller is told, rather than left to notice.
#: `session_id` (F6) is kept, not dropped: it is part of what makes a receipt
#: distinguish two runs of the same spec in different sessions, so dropping it in
#: compact mode would reopen F6 for compact session receipts. It is one short
#: uuid or null.
_COMPACT_RECEIPT = ("receipt_version", "provider_id", "session_id", "spec_hash", "limits")


def compact_result(result: dict) -> dict:
    """A small result that cannot hide an unapplied guarantee.

    Public because anything that re-envelopes a result — a facade over the tool
    surface (#118), a future response_format — has to reuse this rather than
    re-derive which fields are droppable. Re-deriving it is how the first
    version lost `unenforced`.
    """
    out = {k: result.get(k) for k in _COMPACT_ALWAYS}
    out["stdout"] = result.get("stdout", "")
    for key in _COMPACT_DISCLOSURE:
        value = result.get(key)
        # Truthiness, not `is not None`: `unenforced` is [] when everything was
        # applied and `output_error` is None when the streams read cleanly.
        # Both mean "nothing to disclose" and neither is worth the tokens.
        if value:
            out[key] = value

    # Keep WHAT was not applied; drop WHY. Each `unenforced` entry is
    # "<guarantee>: <prose explaining the remedy>", and on a stateful session
    # there are six of them — measured at 171 of a 199-token result, so the
    # disclosure became the whole cost of a "compact" reply.
    #
    # The name is the part a caller must act on: it says which guarantee it did
    # not get. The prose is a remedy, and it is available in full from the same
    # call without `compact`, and from `session_start`. Naming survives; the
    # explanation is the diagnostic half, which is what compact mode is for.
    #
    # `_full` is carried so nothing is silently truncated: the caller is told
    # the explanations exist and where to get them.
    terse = out.get("unenforced")
    if terse:
        out["unenforced"] = [e.split(":", 1)[0].strip() for e in terse]
        out["unenforced_detail"] = "call without compact=True for the reason and remedy of each"

    # Same treatment for the execution receipt (THE-782). See _COMPACT_RECEIPT.
    receipt = out.get("provider")
    if isinstance(receipt, dict):
        dropped = [key for key in receipt if key not in _COMPACT_RECEIPT]
        out["provider"] = {key: receipt[key] for key in _COMPACT_RECEIPT
                           if key in receipt}
        if dropped:
            out["provider"]["receipt_detail"] = (
                "call without compact=True for " + ", ".join(sorted(dropped)))
    return out


@mcp.tool()
def execute_code(
    language: str,
    code: str,
    stdin: str = "",
    timeout: int = 10,
    session_id: str | None = None,
    max_memory_mb: int = 0,
    max_output_kb: int = 0,
    max_cpu: int = 0,
    no_net: bool = False,
    compact: bool = False,
    provider: str | None = None,
) -> dict:
    """Execute `code` in `language` in a sandbox.

    Returns stdout, stderr, exit_code, duration_ms, cpu_ms, peak_memory_kb,
    verdict (OK/TLE/MLE/OLE/RTE).

    - `session_id`: run inside a session workspace (see session_start); with a
      stateful session (python3/node) interpreter state persists across calls.
    - `max_memory_mb` / `max_cpu`: per-call resource ceilings.
    - `max_output_kb`: raise/lower the stdout cap (default 64 KiB).
    - `no_net`: block network egress (LD_PRELOAD shim; dynamic binaries only).
    - `compact`: drop the diagnostic fields (timings, workdir, platform). Never
      drops `unenforced` or `output_error` — if a guarantee you asked for was
      not applied, a compact result still says so.

    With `session_id` set and `max_output_kb` left at its default, output that
    would otherwise be truncated is instead SPILLED (THE-783): the inline
    `stdout`/`stderr` still carry the same truncated prefix as before, and
    `stdout_spill`/`stderr_spill` name a `codecalc://session/{sid}/files/...`
    resource carrying the fuller stream (session_read_file or the resource
    route reads it back) — capped at 4 MiB, `..._spill_capped: true` if even
    that was not enough to hold everything. Passing an EXPLICIT
    `max_output_kb` is honoured as a literal ceiling with no spill, same as
    before. Session-LESS runs (no `session_id`) have no workspace to spill
    into and keep the old truncate-and-drop behaviour.
    """
    timeout = min(timeout, 120)
    spec = providers.ComputationSpec(
        language=language,
        code=code,
        stdin=stdin,
        timeout=timeout,
        max_memory_mb=max_memory_mb,
        max_output_kb=max_output_kb,
        max_cpu=max_cpu,
        no_net=no_net,
    )
    if session_id:
        # Every ceiling documented above is forwarded. They used to stop here:
        # the session branch passed only stdin and timeout, so `no_net=True`
        # silently reached the network and `max_memory_mb` was ignored. What a
        # stateful worker genuinely cannot apply now comes back in `unenforced`.
        result = _execution_service.execute_session(
            _session_service, session_id, spec, provider_id=provider
        )
    else:
        result = _execution_service.execute(spec, provider_id=provider)
    if compact:
        return compact_result(result)
    return result


@mcp.tool()
def session_start(language: str = "python3") -> dict:
    """Start a persistent session. python3/node get a stateful REPL worker
    (variables/imports persist across execute_code calls); other languages get
    a persistent workspace directory. Returns session_id."""
    return _session_service.start(language)


@mcp.tool()
def session_stop(session_id: str) -> dict:
    """Stop a session: kill its REPL worker (if any) and delete its workspace."""
    return _session_service.stop(session_id)


@mcp.tool()
def session_list() -> dict:
    """List active sessions and their languages/state."""
    return _session_service.list_sessions()


@mcp.tool()
def session_files(session_id: str, path: str = "", page_size: int | None = None,
                  cursor: str | None = None) -> dict:
    """List workspace files, optionally using a bounded cursor page."""
    return _session_service.list_files(
        session_id, path, page_size=page_size, cursor=cursor
    )


@mcp.tool()
def session_write_file(session_id: str, path: str, content: str) -> dict:
    """Write a file into a session workspace (relative path, no escapes).
    Use this to seed input data for executed code."""
    return _session_service.write_file(session_id, path, content)


@mcp.tool()
def session_artifacts(session_id: str) -> dict:
    """List files created by executed code in a session (excluding runner
    internals like main.py/run.out)."""
    return _session_service.artifacts(session_id)


@mcp.tool()
def install_package(language: str, package: str, session_id: str | None = None,
                    version: str | None = None) -> dict:
    """Install a package for a language (uv pip / npm / gem / go get / cargo add...).

    With session_id, installs into that session's workspace so executed code
    can import it. Without, installs into a shared cache.

    NETWORK: yes, always. The package manager fetches from its registry (PyPI,
    npm, rubygems, crates.io). codecalc opens no socket itself; the child
    process does.

    NOT SANDBOXED: the installer runs as a direct subprocess of the server, so
    install-time hooks (npm postinstall, Python build backends, Cargo build
    scripts) execute with the server user's filesystem access. The environment
    is still restricted to the allowlist, so secrets do not leak, but the
    filesystem is not confined. Do not point this at untrusted input. See
    SECURITY.md and issue #23.
    """
    return packages.install(language, package, session_id=session_id,
                            version=version, audit=_audit_log)


@mcp.tool()
async def execute_code_stream(
    language: str,
    code: str,
    stdin: str = "",
    timeout: int = 30,
    max_memory_mb: int = 0,
    max_output_kb: int = 0,
    max_cpu: int = 0,
    no_net: bool = False,
    provider: str | None = None,
    ctx: Context = None,
) -> dict:
    """Execute code and STREAM progress + partial output as it runs.

    Unlike execute_code (which returns only at exit), this reports progress
    notifications to the client while the program runs, so agents can see
    output before the process finishes. Returns the same result shape.

    It also applies the SAME ceilings: max_memory_mb, max_output_kb and
    max_cpu are forwarded to the executor exactly as execute_code forwards
    them. That sentence used to say only "the same result shape", which was
    true of the shape and false of the guarantees — this tool accepted
    neither a memory nor a CPU bound, so a caller who set them on
    execute_code and then switched to streaming silently lost both.

    The one difference that remains, deliberately: the wall-clock cap is 300s
    here against execute_code's 120s, because streaming exists for runs long
    enough to want progress. That divergence is stated rather than left for a
    reader to discover by comparing two min() calls.
    """
    timeout = min(timeout, 300)
    spec = providers.ComputationSpec(
        language=language, code=code, stdin=stdin, timeout=timeout,
        max_memory_mb=max_memory_mb, max_output_kb=max_output_kb,
        max_cpu=max_cpu, no_net=no_net,
    )

    async def report_progress(value: int, message: str) -> None:
        if ctx is None:
            return
        try:
            await ctx.report_progress(
                progress=float(value), total=None, message=message
            )
        except Exception:
            pass

    return await _execution_service.execute_stream(
        spec, provider_id=provider, on_progress=report_progress
    )


#: Keys a terminal run_inspect() result carries BEYOND execute_code's own
#: result shape (THE-778 fix round, review Important #2): RunHandle's own
#: fields, RunSupervisor's state bookkeeping, and the one best-effort
#: disclosure (`cleanup_error`) that appears only if a provider's own
#: cleanup() fails. Named explicitly, not left implicit, so the key-set
#: parity test in tests/test_execution_service.py has one place to compare
#: against instead of a second hardcoded copy of this list — the exact
#: pattern that let `provider` silently go missing from run_inspect in the
#: first place (execute_code and run_inspect drifted because nothing
#: compared their shapes).
_RUN_EXTRA_KEYS = frozenset({
    "run_id", "provider_id", "started_at", "deadline", "state", "cleaned",
    "cleanup_error",
})


@mcp.tool()
def run_submit(
    language: str,
    code: str,
    stdin: str = "",
    timeout: int = 30,
    max_memory_mb: int = 0,
    max_output_kb: int = 0,
    max_cpu: int = 0,
    no_net: bool = False,
    provider: str | None = None,
) -> dict:
    """Submit code for BACKGROUND execution; returns a run_id immediately.

    Same request shape as execute_code (minus session_id: a run is a
    standalone process, not a session workspace). The work proceeds on a
    background worker; poll it with run_inspect(run_id) and, if needed, stop
    it early with run_cancel(run_id).

    Use this instead of execute_code when you would rather not hold an MCP
    call open for the whole computation. `timeout` is still the WORK's own
    deadline (same 120s ceiling as execute_code) — it bounds the run, not how
    long you wait to collect it.

    Admission is capped (CODECALC_MAX_ACTIVE_RUNS, default 64): past that
    many runs still running/cancelling at once, this returns a
    resource_exhausted error rather than growing without bound — call
    run_inspect/run_cancel to make room, or wait for one to finish.

    Retention: see run_inspect.
    """
    if _run_supervisor is None:
        return errors.error_result(
            errors.INTERNAL, "run supervisor unavailable",
            provider_error="run_supervisor_unavailable",
        )
    timeout = min(timeout, 120)
    spec = providers.ComputationSpec(
        language=language, code=code, stdin=stdin, timeout=timeout,
        max_memory_mb=max_memory_mb, max_output_kb=max_output_kb,
        max_cpu=max_cpu, no_net=no_net,
    )
    # THE-787 fix round (CRITICAL): broker the background run through the SAME
    # path execute_code uses, BEFORE any work starts. Previously run_submit
    # called start(spec) directly, so a policy the sync path enforced
    # (deny-network, strict) was silently bypassed on the background path. The
    # provider is resolved here so the broker can read its enforcement
    # capabilities; a rejection (escalation / strict-unenforceable) returns
    # pre-start with no run created, and the decision is threaded into the run
    # so run_inspect's terminal receipt carries the same `capabilities` block.
    # Resolve against the SUPERVISOR's own registry (not the module-level one):
    # start() selects from `self.registry`, so brokering the same registry keeps
    # the two consistent — in production they are the same object; a test that
    # swaps in a fake supervisor with its own registry must broker that one.
    try:
        provider_obj = _run_supervisor.registry.select(provider, spec=spec)
    except providers.UnknownProvider as exc:
        return errors.error_result(
            errors.VALIDATION, str(exc), provider_error=exc.code,
            requested_provider=exc.provider_id, available_providers=list(exc.available),
        )
    run_spec, decision, rejection = execution_service.broker_run(
        spec, provider_obj, policy=capabilities.policy_from_env(), audit=_audit_log)
    if rejection is not None:
        return rejection
    try:
        handle = _run_supervisor.start(
            run_spec, provider_id=provider_obj.provider_id,
            capability_decision=decision, receipt_spec=spec)
    except run_supervisor.TooManyActiveRuns as exc:
        return errors.error_result(
            errors.RESOURCE_EXHAUSTED,
            f"{exc.active} runs are already active against a limit of "
            f"{exc.limit}; call run_inspect/run_cancel to free one up, or "
            "wait for one to finish, then retry",
            active_runs=exc.active, limit=exc.limit,
        )
    return {
        "ok": True, "run_id": handle.run_id, "provider_id": handle.provider_id,
        "started_at": handle.started_at, "deadline": handle.deadline,
        "state": "running",
    }


@mcp.tool()
def run_inspect(run_id: str) -> dict:
    """Poll a background run started with run_submit.

    While running: {"ok": True, "state": "running"|"cancelling", "run_id",
    "provider_id", "started_at", "deadline"}.

    Once terminal (`state` "finished"/"cleaned"/"recovered"), this returns
    the SAME result shape execute_code returns — stdout/stderr/exit_code/
    verdict/unenforced/provider (the interface_version/provider_id/limits
    receipt)/... — merged with a small set of run_* extras (run_id,
    provider_id, started_at, deadline, state, cleaned; see server.py's
    _RUN_EXTRA_KEYS). Read `ok` and `verdict` on a terminal result to tell a
    clean finish from a failure; a
    run stopped by run_cancel is only reflected there for a provider that
    actually supports cancellation (see run_cancel's own docstring) — check
    the result the same way you would any other run.

    Retention: a finished run's result stays inspectable for the life of
    this server process — call this as many times as you like; nothing is
    consumed by reading it. What IS released on the first terminal read is
    the PROVIDER's own resources for that run (RunSupervisor.cleanup(),
    idempotent on repeat calls) — the in-memory record of the run itself is
    not evicted; there is no cap or TTL on it here, deliberately: the durable
    state machine, leases and TTL-based eviction are out of this residual's
    scope (see run_supervisor.py's own docstring). A long-lived server that
    calls run_submit very many times will grow this table; the on-disk
    crash-recovery journal underneath it is already bounded
    (RunSupervisor.max_completed), independent of this.
    """
    if _run_supervisor is None:
        return errors.error_result(
            errors.INTERNAL, "run supervisor unavailable",
            provider_error="run_supervisor_unavailable",
        )
    try:
        status = _run_supervisor.inspect(run_id)
    except KeyError:
        return errors.error_result(
            errors.VALIDATION, f"unknown run {run_id!r}",
            provider_error="unknown_run", run_id=run_id,
        )
    if status["state"] not in {"finished", "cleaned", "recovered"}:
        return {"ok": True, **status}
    try:
        result = dict(_run_supervisor.wait(run_id, timeout=0))
    except TimeoutError:
        # Should be unreachable: `status` above already reported a terminal
        # state, so the future backing it is done. Reported rather than
        # assumed, in case RunSupervisor's state machine changes under this.
        return errors.error_result(
            errors.INTERNAL,
            f"run {run_id!r} reported state {status['state']!r} but its "
            "result was not yet collectible",
            provider_error="run_result_not_collectible", run_id=run_id,
        )
    try:
        _run_supervisor.cleanup(run_id)
    except providers.ProviderOperationFailure as exc:
        # Best-effort and disclosed, not fatal: the CALLER's result is
        # already collected above and is correct regardless of whether the
        # provider released its own side of the run. Failing the whole
        # inspect call here would poison every future poll of this run_id
        # over a resource-release problem that has nothing to do with the
        # result being reported.
        result = {**result, "cleanup_error": str(exc)}
    # F9 (cross-vendor): re-read the status AFTER cleanup(). `status` above was
    # captured pre-cleanup, so returning it made the FIRST terminal read report
    # cleaned=false (state "finished") and the NEXT read cleaned=true (state
    # "cleaned") — a spurious change for a caller polling to completion. A
    # successful cleanup advanced the run to "cleaned"; a failed one left it
    # "finished" with cleanup_error already appended above. Either way the fresh
    # status is the accurate one.
    status = _run_supervisor.inspect(run_id)
    return {**result, **status}


@mcp.tool()
def run_cancel(run_id: str) -> dict:
    """Cancel a background run started with run_submit.

    Idempotent: calling this on a run that is already finished/cleaned
    reports `cancelled: false, state: <its actual terminal state>` rather
    than erroring — matching execute_code's own "no partial result" rule,
    there is nothing partial to hand back either way.

    Propagation depends on the SELECTED PROVIDER (see
    list_execution_providers' `cancel` capability). The built-in `local`
    provider does not support stopping a run once it has started; that is
    reported honestly here rather than silently pretended to have worked —
    the computation keeps running to completion and its result stays
    available via run_inspect, so bound it in advance with run_submit's own
    `timeout` instead. A provider that DOES advertise `cancel: true` reaches
    the full spawned process tree the same way execute_code's own
    cancellation does — RunSupervisor already owns that; this tool only
    calls it.
    """
    if _run_supervisor is None:
        return errors.error_result(
            errors.INTERNAL, "run supervisor unavailable",
            provider_error="run_supervisor_unavailable",
        )
    try:
        result = _run_supervisor.cancel(run_id)
    except KeyError:
        return errors.error_result(
            errors.VALIDATION, f"unknown run {run_id!r}",
            provider_error="unknown_run", run_id=run_id,
        )
    except providers.UnsupportedCapability as exc:
        return errors.error_result(
            errors.VALIDATION,
            f"provider {exc.provider_id!r} does not support cancelling a "
            "run once it has started; the computation continues to "
            "completion and its result stays available via run_inspect",
            provider_error=exc.code, requested_provider=exc.provider_id,
            run_id=run_id,
        )
    except providers.ProviderOperationFailure as exc:
        return errors.error_result(
            errors.INTERNAL, str(exc), provider_error=exc.code,
            requested_provider=exc.provider_id, run_id=run_id,
        )
    return {"ok": True, **result}


@mcp.tool()
def evaluate_expression(expression: str) -> dict:
    """Symbolically evaluate or simplify a math expression, e.g. 'integrate(x**2, x)' or 'sqrt(144) + 2**10'."""
    return logic.evaluate_expression(expression)


@mcp.tool()
def truth_table(expression: str) -> dict:
    """Build the truth table for a boolean expression: 'a and b or not c', 'p xor q', 'a implies b'."""
    return logic.truth_table(expression)


@mcp.tool()
def z3_check(smt2: str) -> dict:
    """Check an SMT-LIB2 formula with Z3: sat/unsat/unknown plus a model. Example:
    '(declare-const x Int)(assert (> x 5))(check-sat)'.

    `unsat` is graded `solver_proven` — see `grade_basis` for the engine
    version and timeout bound it was decided within. `sat` is graded
    `ungraded`: it's a real decided answer, just not a proof — reserving
    `solver_proven` for `unsat` means a counterexample can never wear a
    proof grade. `unknown` carries no proof either way and is also graded
    `ungraded`.
    """
    return grades.grade_z3_check(logic.z3_check(smt2))


@mcp.tool()
def solve_linear(system: str, variables: str) -> dict:
    """Solve a system of equations; `system` is ';'-separated equations, `variables` comma-separated. Example: system='x + y = 10; x - y = 2', variables='x, y'."""
    vars_ = [v.strip() for v in variables.split(",") if v.strip()]
    return logic.solve_linear(system, vars_)


@mcp.tool()
def analyze_complexity(code: str, language: str = "python3") -> dict:
    """Estimate the asymptotic (Big-O) time complexity of a code snippet via structural analysis."""
    return complexity.analyze(code, language)


@mcp.tool()
def benchmark(code: str, language: str = "python3", sizes: str = "100,1000,10000,100000",
              timeout: int = 30) -> dict:
    """Empirically measure time complexity by running code at increasing input sizes.

    Contract: the code must read an integer N from stdin (first line) and do work
    sized by N. codecalc runs it at each size in `sizes` (comma-separated) and fits
    the growth curve to estimate Big-O (O(1), O(log n), O(n), O(n log n), O(n^2)...).
    Example python: 'import sys\\nn=int(sys.stdin.readline()); s=0\\nfor i in range(n): s+=i\\nprint(s)'
    """
    return tools.benchmark(code, language=language, sizes=sizes, timeout=timeout)


@mcp.tool()
def compare_execution(snippets: dict[str, str], stdin: str = "", timeout: int = 15) -> dict:
    """Run the same code in multiple languages side by side.

    `snippets` maps language name -> code (each snippet must be valid in its own
    language). Returns per-language stdout/stderr/exit/duration plus which was fastest.
    Example: {"python3": "print(6*7)", "node": "console.log(6*7)"}
    """
    return tools.compare_execution(snippets, stdin=stdin, timeout=timeout)


@mcp.tool()
def runtimes_status(languages: str = "") -> dict:
    """Check every language runtime for available updates (NON-MUTATING).

    Reports current vs latest version per language, which package manager owns
    it (mise/rustup/swiftly/apt/npm/uv), and the exact command that would run.
    Optional `languages` = comma-separated subset, e.g. "python3,node,rust".

    NETWORK: yes. Non-mutating refers to this machine's runtimes, not to
    traffic — each package manager is asked what the latest version is, and
    they answer by contacting their own remote index.
    """
    return runtimes.status(languages or None)


@mcp.tool()
def update_runtimes(languages: str = "", apply: bool = False, timeout: int = 600) -> dict:
    """Update language runtimes. SAFE BY DEFAULT: with apply=False this is a
    dry run — it returns the update commands that WOULD run without changing
    anything. Pass apply=True to actually execute them (mise up, rustup update,
    swiftly update, apt-get upgrade of language packages, npm -g update, uv tool
    upgrade). `languages` = comma-separated subset; empty = all.

    PRIVILEGE: the apt manager updates system packages and its command begins
    with `sudo`. Those commands do NOT run unless the HOST has set
    CODECALC_ALLOW_RUNTIME_APPLY=1; without it they are reported as skipped
    with `ok: false` and the variable named, and the rest still run. Every
    entry carries an `elevated` flag either way. mise/rustup/swiftly/npm/uv
    touch user-owned toolchains and are never gated.

    NETWORK: yes, on both paths. apply=False still asks each manager what the
    latest version is, which is a remote lookup; apply=True additionally
    downloads and installs. "Dry run" bounds what changes on disk, not what is
    sent.
    """
    return runtimes.update(languages or None, apply=apply, timeout=timeout)


@mcp.resource("codecalc://session/{session_id}/files/{+path}",
              name="Session file",
              description="Any file in a codecalc session workspace. Images render inline; text returns as text; other files download.",
              mime_type="application/octet-stream")
def session_file_resource(session_id: str, path: str):
    """MCP resource: session workspace file. str for text, bytes for binary."""
    result = _session_service.read_file(
        session_id, path, max_bytes=sessions.RESOURCE_MAX_BYTES
    )
    if not result.get("ok"):
        raise ValueError(f"no such file: {path}")
    data = result["content_bytes"]
    mime = result["mime_type"]
    if mime.startswith("image/"):
        return data  # bytes -> BlobResourceContents, rendered inline by clients
    try:
        return data.decode("utf-8")  # str -> TextResourceContents
    except UnicodeDecodeError:
        return data


@mcp.tool()
def session_read_file(session_id: str, path: str, max_bytes: int = 65536,
                      as_image: bool = False):
    """Read a file from a session workspace.

    Text files return content. With as_image=True (or for image files), the
    file is returned as an inline image the model can see. Use session_files
    to discover paths; session_artifacts lists what executed code produced.
    """
    result = _session_service.read_file(
        session_id, path, max_bytes=max_bytes, as_image=as_image
    )
    if not result.get("ok"):
        return result
    data = result.pop("content_bytes")
    if result.pop("is_image"):
        # fastmcp's Image(path=...) helper has no counterpart in the official
        # SDK; ImageContent is the protocol type and wants base64 itself.
        # as_image=True on a non-image file is honoured deliberately: the caller
        # is asserting it knows the bytes are renderable, so fall back to PNG
        # rather than refusing.
        return ImageContent(
            type="image",
            data=base64.b64encode(data).decode("ascii"),
            mimeType=(result.pop("mime_type")
                      if result["mime_type"].startswith("image/") else "image/png"),
        )
    result.pop("mime_type")
    return result


@mcp.tool()
def session_run(session_id: str, entry_file: str, language: str | None = None,
                stdin: str = "", timeout: int = 30) -> dict:
    """Run a multi-file program in a session: execute `entry_file`, which may
    import other files already in the session workspace (helper.py, data/...).

    Runs as a fresh process in the session workdir (not the REPL worker), so
    relative imports and data files resolve. Returns stdout/stderr/verdict
    plus the entry file's path. Oversized output spills into the session
    workspace the same way execute_code's does — see its docstring for
    `stdout_spill`/`stderr_spill`.
    """
    return _session_service.run_file(
        session_id, entry_file, language=language, stdin=stdin, timeout=timeout
    )


@mcp.tool()
def convert_units(value: float, from_unit: str, to_unit: str) -> dict:
    """Convert a value between units (dimensional analysis via sympy).

    Supports metric/imperial length, mass, time, speed, energy, power, force,
    pressure, temperature (°C/°F/K), volume, area, data sizes, frequency.
    Examples: ('60','mph','km/h'), ('100','celsius','fahrenheit'),
    ('1','gb','mib'). Use list_units for the full alias table.
    """
    return units.convert(value, from_unit, to_unit)


@mcp.tool()
def physical_constants(name: str | None = None) -> dict:
    """Look up a physical constant (speed_of_light, planck, avogadro,
    gravity, electron_mass, gas_constant, ...) or list all 22 with values."""
    return units.constants(name)


@mcp.tool()
def list_units() -> dict:
    """List every supported unit alias for convert_units."""
    return units.list_units()


# ── exact arithmetic & programmer-mode (ported from the Claude calc skill) ──

@mcp.tool()
def calc_exact(expr: str) -> dict:
    """EXACT arithmetic: 0.1 + 0.2 == 0.3 is True here (False in plain Python).

    Everything is an exact rational, integers are arbitrary precision. Supports
    + - * / // % ** comparisons, bitwise ops (& | ^ << >> ~) on integers, and
    whitelisted math functions (sqrt, log, sin, ...) plus pi/e/tau. Use BEFORE
    asserting any computed number: thresholds, ratios, overflows, 'X is N% of Y'.
    Examples: '2**64 - 1', 'comb(52,5)', '0.1+0.2 == 0.3', '0xff & 0x0f'.
    """
    return exact.eval_exact(expr)


@mcp.tool()
def compare_threshold(a: str, op: str, b: str) -> dict:
    """Exact threshold check with a verdict and the shortfall when it fails.

    `a OP b` with op in ==, !=, >, >=, <, <= (= accepted for ==). Both sides
    are evaluated exactly and printed as fractions — a threshold comparison
    written out cannot be gotten backwards. Example: ('1/25', '>', '0.05').
    """
    return exact.compare_threshold(a, op, b)


@mcp.tool()
def percentage(part: str, total: str) -> dict:
    """Exact share and percentage of PART / TOTAL (rationals accepted)."""
    return exact.percentage(part, total)


@mcp.tool()
def calc_stats(nums: list[float]) -> dict:
    """mean, median, sample stdev, and coefficient of variation (CV).

    CV > 0.2 means run-to-run noise swamps the effect being measured — the
    numbers cannot be compared across runs.
    """
    return exact.stats(nums)


@mcp.tool()
def percentiles(nums: list[float]) -> dict:
    """p50/p90/p95/p99 by nearest-rank AND linear interpolation.

    Warns when n < 100 that p99 is just the maximum wearing a label.
    """
    return exact.percentiles(nums)


@mcp.tool()
def collision_probability(items: int, bits: int) -> dict:
    """Birthday-bound hash collision probability: 1 - exp(-n^2 / (2*2^b)).

    Sizes hashes: 1e6 items into 64 bits is ~2.7e-8; 1e5 into 32 bits is ~0.69
    — the answer to 'can I truncate this to 8 hex chars?' (no).
    """
    return exact.collision_prob(items, bits)


@mcp.tool()
def data_sizes(n: int) -> dict:
    """Byte sizes both ways: binary (KiB/MiB/GiB) AND decimal (KB/MB/GB).

    The 1024/1000 gap is where '291 MB' and '277 MiB' silently disagree by 5%.
    """
    return exact.data_sizes(n)


@mcp.tool()
def human_duration(seconds: float) -> dict:
    """Humanised duration plus per-day and per-30d rates."""
    return exact.human_duration(seconds)


@mcp.tool()
def epoch_time(n: str) -> dict:
    """Epoch seconds/millis/micros/nanos to ISO 8601 UTC (implausible readings
    suppressed)."""
    return exact.epoch_time(n)


@mcp.tool()
def base_repr(n: int, width: int | None = None) -> dict:
    """hex/oct/bin of N; with WIDTH, two's complement and signed-overflow
    detection. `base_repr(3000000000, 32)` says plainly it does not fit i32."""
    return exact.base_repr(n, width)


@mcp.tool()
def radix_convert(value: str, from_base: int = 10, to_base: int = 10) -> dict:
    """Convert a value between ANY bases 2..36, fractions included; bases that
    cannot represent the fraction (e.g. 0.1 in base 2) are flagged
    non-terminating. `radix_convert('zz', 36, 7)` is one call."""
    return exact.radix_convert(value, from_base, to_base)


@mcp.tool()
def float_repr(x: float) -> dict:
    """What binary64 actually stores for X: exact value, raw bits, ULP, both
    neighbours, and whether the literal is representable. `float_repr(0.1)`
    shows 0.1000000000000000055511151231257827...; `float_repr(0.25)` says
    EXACT. Above 2^53 warns consecutive integers are indistinguishable."""
    return exact.float_repr(x)


@mcp.tool()
def int_widths(n: int) -> dict:
    """Which widths (i8..i64/u8..u64) hold N, and the wrapped value where they
    do not. Flags anything past 2^53 as unable to round-trip through a JS
    number or JSON float. `int_widths(3000000000)` shows the i32 wrap."""
    return exact.int_widths(n)


@mcp.tool()
def bit_analysis(n: int, align: int | None = None) -> dict:
    """popcount, bit length, trailing zeros, power-of-two check, next power of
    two, and (with align) padding needed to reach an alignment boundary."""
    return exact.bit_analysis(n, align)


@mcp.tool()
def bitop(a: int, op: str, b: int | None = None, width: int = 64) -> dict:
    """Programmer-mode bit ops: and or xor nand nor xnor not shl shr sar rol ror
    at width 8/16/32/64. Every result shows unsigned, signed (two's complement),
    hex, octal and binary. shr is logical (zero-fill); sar is arithmetic
    (sign-propagating) — 0x80 shr 1 = 0x40 (+64) but 0x80 sar 1 = 0xC0 (-64).
    A left shift that drops bits says OVERFLOW and shows the unbounded answer.
    """
    return exact.bitop(a, op, b, width)


@mcp.tool()
def algebraic_equiv(a: str, b: str) -> dict:
    """Are two expressions algebraically identical? Refs: 'is (a*b)/c the same
    as a*(b/c)?' answered exactly. Caveat: symbolic identity says nothing
    about float rounding, integer truncation or modular overflow."""
    return exact.algebraic_equiv(a, b)


@mcp.tool()
def solve_expression(expr: str, var: str = "x") -> dict:
    """Solve for a root or crossover: 'x**2 - 4 = 0', '2*x + 1 = 7'."""
    return exact.solve_expression(expr, var)


@mcp.tool()
def limit_expression(expr: str, var: str = "x", point: str = "oo") -> dict:
    """Asymptotic behaviour: limit of EXPR as var -> point (default oo).
    'limit_expression(\"n*log(n)/n**2\", \"n\")' returns 0 — settles complexity
    arguments faster than arguing."""
    return exact.limit_expression(expr, var, point)


@mcp.tool()
def simplify_expression(expr: str) -> dict:
    """Simplified, factored and expanded forms of an expression."""
    return exact.simplify_expression(expr)


@mcp.tool()
def verify_translation(source_code: str, source_language: str,
                       target_code: str, target_language: str,
                       test_inputs: list[str] | None = None) -> dict:
    """PROVE that a port is equivalent: run both programs, compare their output.

    You write the translation — you are the language model. This runs your
    source and your port on the same inputs and reports, per input, whether
    they matched, diverged, or could not be compared (a runtime that is missing
    or a program that failed on both sides is INCONCLUSIVE, never a pass).

    Use it after porting anything: python3 -> go, node -> rust, a rewritten
    function against the original. Pair with compare_edge_cases to find the
    inputs worth testing.

    A pass is graded `cross_checked` (two independent implementations, run
    and agreeing — see `grade_basis` for which runtimes). A non-pass is
    graded `ungraded`: never a softer positive grade.
    """
    result = translation.verify_translation(
        source_language, source_code, target_language, target_code,
    # The FULL set, not DEFAULT_EDGE_INPUTS[:4]. The slice kept '', '0', '1', '-1'
        # and discarded '10', '100' and '0.1\n0.2' — the multi-digit cases and the
        # float one. Float formatting is among the most common genuine divergences
        # between ports, so the default evidence excluded the input most likely to
        # find a real bug.
        #
        # Demonstrated rather than argued: a python3 -> node port that agrees on
        # every integral sum and differs only in float rendering was CERTIFIED by
        # the old default and rejected by the full set.
        #
        #     D[:4]  passed=True   matched=4 mismatched=0
        #     D      passed=False  mismatched on '0.1\n0.2':
        #                          '0.30000000000000004' vs '0.3'
        #
        # Cost of the change, measured: 0.38s -> 0.67s per call.
        test_inputs if test_inputs else translation.DEFAULT_EDGE_INPUTS)
    return grades.grade_verify_translation(result, source_language, target_language)


@mcp.tool()
def compare_edge_cases(snippets: dict[str, str],
                       inputs: list[str] | None = None) -> dict:
    """Run the same logic in N languages on edge-case inputs and flag divergence.

    `snippets` maps language -> code (provide a correct snippet per language;
    write one per language). Default inputs cover empty,
    zero, negative, and float-precision cases: ['', '0', '1', '-1', '10',
    '100', '0.1\\n0.2']. Returns a per-input matrix plus a divergences list
    where languages disagree on identical input.
    """
    return translation.compare_edge_cases(snippets, inputs=inputs)


@mcp.tool()
def verify_optimization(original: str, candidate: str, language: str,
                        test_inputs: list[str] | None = None,
                        sizes: list[int] | None = None,
                        min_speedup: float = 1.15) -> dict:
    """PROVE an optimisation: same outputs, and measurably faster.

    You write the optimised version. This runs both against the same inputs to
    confirm they still agree, then TIMES both at increasing sizes and compares.
    Accepted only if equivalent AND at least `min_speedup` faster.

    A rejection tells you which gate failed and by how much — "correct but only
    1.09x" is the answer an optimiser that fabricates wins cannot give. A
    candidate that is faster but wrong fails the first gate, and its speed is
    never measured, because a faster wrong answer is not an optimisation.

    An accepted result is graded `cross_checked` (see `grade_basis` for the
    runtime and the measured speedup). A rejection — wrong OR merely not
    faster enough — is graded `ungraded`: correctness alone does not earn a
    grade for the optimisation claim this tool exists to answer.
    """
    result = optimization.verify_optimization(
        original, candidate, language, test_inputs=test_inputs,
        sizes=sizes, min_speedup=min_speedup)
    return grades.grade_verify_optimization(result, result.get("language", language))


@mcp.tool()
def extract_function(code: str, language: str, function_name: str,
                     call: str | None = None,
                     test_inputs: list[str] | None = None) -> dict:
    """Extract a named function (with its imports + referenced helpers) into a
    standalone program and run it in the sandbox.

    python3 gets exact ast extraction; other languages best-effort block
    extraction (pass `call` to execute non-python). Returns the extracted
    program and per-input runs.
    """
    return optimization.extract_function(code, language, function_name,
                                         call=call, test_inputs=test_inputs)


#: Registry entries that are a second spelling of a language already counted,
#: not a language of their own. Mirrors ALIAS_ENTRIES in scripts/check_claims.py
#: — the count this command prints has to agree with the one the README states
#: and the gate enforces, or it is just a fourth opinion.
_ALIAS_ENTRIES = {"c++"}


def _doctor(as_json: bool = False, deep: bool = False) -> int:
    """Report what this install actually resolved, before a tool call has to.

    The gap this fills: everything below is discoverable ONLY by making a tool
    call and reading the result — the backend from `execute_code`'s `backend`
    field, the missing runtimes from `list_languages`, the sandbox gaps from
    `unenforced`. An operator wiring up an MCP client has no way to see any of
    it until something has already gone quietly weaker than they expected.

    The report is BUILT in codecalc/doctor.py and only rendered here, so the
    text a human reads and the JSON a script parses cannot drift apart. The
    exit code is the report's own `healthy`, which is deliberately narrow: a
    missing extra or an uninstalled Haskell is a fact about a host, not a
    fault, and exiting non-zero for either would make this useless as the
    install check THE-780 wants it to be.

    Writes to STDOUT, which is safe here and nowhere else in this file: stdout
    is the MCP transport, so anything printed during a normal run corrupts the
    protocol stream. This path never starts the server.
    """
    import json
    import shutil
    import sys

    rep = doctor.report(deep=deep)

    if as_json:
        print(json.dumps(rep, indent=2, sort_keys=True))
        return 0 if rep["healthy"] else 1

    print(f"codecalc {rep['codecalc_version']}")
    print(f"  python           {rep['python']['version']} on {rep['python']['platform']}")
    # The result contract is versioned separately from the package on purpose:
    # a package release that changes no field a caller reads should not look
    # like a contract change, and a contract break has to be visible even in a
    # patch release. The doctor JSON is covered by that same version and policy
    # rather than a third number of its own.
    print(f"  result contract  {rep['contract_version']} "
          f"(schema: docs/contract/result-v1.schema.json)")

    print(f"  execution backend {rep['backend']['kind']}")
    if rep["backend"]["kind"] == "rust":
        print(f"    binary         {rep['backend']['binary']}")
    else:
        print("    binary         NOT FOUND — running the pure-Python fallback.")
        print("                   Set CODECALC_REQUIRE_NATIVE=1 to make this a")
        print("                   startup failure instead of a weaker sandbox.")

    print("  execution providers")
    for provider in rep["execution_providers"]:
        readiness = ("ready" if provider["ready"] is True else
                     "UNAVAILABLE" if provider["ready"] is False else "not probed")
        boundary = "strict" if provider["strict"] else "non-strict"
        print(f"    {provider['provider_id']:18} {readiness}, {boundary}")
        if provider["detail"]:
            print(f"      {provider['detail']}")

    strict = rep["strict_runtime"]
    strict_state = ("available" if strict["available"] else "UNAVAILABLE")
    print(f"  strict runtime    {strict_state} "
          f"({strict['runtime']} on Docker + gVisor)")
    if strict["canary"] is not None and strict["canary"]["verified_runsc"]:
        print(f"    canary         ran under {strict['canary']['runtime_observed']} "
              "(verified out of band)")
    if strict["detail"]:
        print(f"    {strict['detail']}")

    sandbox = rep["install_sandbox"]
    print("  install sandbox   " + (f"Landlock ABI {sandbox['landlock_abi']}"
                                    if sandbox["confined"] else
                                    "unavailable (installs are not confined)"))

    # Extras BEFORE runtimes: a caller whose symbolic tools are erroring needs
    # this line, and it is the one thing `doctor` can tell them that no tool
    # result will (a tool result names its own extra; only this names them all).
    for extra in rep["extras"]:
        if extra["installed"]:
            print(f"  extra: {extra['name']:9} installed")
        else:
            print(f"  extra: {extra['name']:9} MISSING "
                  f"({', '.join(extra['missing'])}) — {extra['remedy']}")

    # Counted the way the README counts, which is the way check_claims.py
    # verifies: `c++` and `cpp` are one language written twice, so a raw
    # len(LANGUAGES) says 32 where every other number in this project says 31.
    summary, total = rep["runtime_summary"], len(rep["runtimes"])
    resolved = total - summary["supported"]
    print(f"  runtimes          {resolved}/{total} resolved "
          f"({rep['status_basis']})")
    for state in ("available", "unhealthy", "supported"):
        names = sorted(r["name"] for r in rep["runtimes"] if r["status"] == state)
        if names:
            label = {"available": "verified", "unhealthy": "BROKEN",
                     "supported": "missing"}[state]
            print(f"    {label:14} {', '.join(names)}")

    # Versions are only read under --deep, so this block is silent without it
    # rather than printing a column of blanks that reads as "no version".
    versioned = [r for r in rep["runtimes"] if r.get("version")]
    if versioned:
        print(f"    versions       {len(versioned)}/{total} read")
        for r in versioned:
            print(f"      {r['name']:14} {r['version']}")

    # Printed unconditionally, including when warm. An operator planning an
    # offline install needs to see the state, and a line that only appears when
    # something is wrong cannot be used to confirm something is right.
    gc = rep["grammar_cache"]
    if not gc["extra_installed"]:
        print("  grammars          parsing extra not installed (regex fallback)")
    elif gc["cached"]:
        print(f"  grammars          {gc['grammars']} cached  {gc['path']}")
    else:
        print(f"  grammars          NONE CACHED — first use per language "
              f"DOWNLOADS  {gc['path']}")

    ws = rep["workspace"]
    print(f"  workspace         {ws['path']}"
          f"{'' if ws['writable'] else '  NOT WRITABLE — ' + str(ws['error'])}")

    # Where the shipped skill is, because a skill nobody can find is a skill
    # nobody installs. It travels inside the wheel, so this path is correct for
    # a uvx run, a venv, or a checkout without any of them differing.
    print(f"  skill file        {rep['skill_file'] or '(MISSING)'}")
    print("                    copy to your client's skills directory to make "
          "the calling rules apply")

    if rep["remedies"]:
        print("\n  what to do:")
        for r in rep["remedies"]:
            print(f"    - {r}")

    # Absolute paths, because the whole point is that it can be pasted. A
    # relative one resolves against the CLIENT's working directory, which is
    # not something the person pasting it controls or can easily predict.
    exe = shutil.which("codecalc") or sys.executable
    args = [] if shutil.which("codecalc") else ["-m", "codecalc"]
    print("\n  MCP client config (copy into your client's JSON):")
    print(json.dumps({"mcpServers": {"codecalc": {"command": exe, "args": args}}},
                     indent=2))
    return 0 if rep["healthy"] else 1


def main() -> None:
    """stdio MCP server, or `doctor` when asked.

    argv is inspected rather than parsed. A client spawns this with NO
    arguments and speaks MCP on stdin/stdout; anything clever here — a parser
    that prints usage to stdout, or exits on an unrecognised flag — would
    break that in a way that looks like a protocol error. Unrecognised
    arguments are therefore IGNORED and the server starts, which is the
    behaviour that was always there.

    `--help`/`-h` and `--version`/`-V` are the one deliberate exception: a
    human running `codecalc --help` at a shell got no output and a hung
    stdio server instead of usage text (THE-876/#201) — the "ignore and
    start" rule is right for an MCP client, but silently wrong for a person
    at a terminal. Both are intercepted here, print to stdout, and exit
    WITHOUT starting the server; every other unrecognised argument still
    falls through to the stdio server exactly as before.
    """
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print(
            "codecalc: universal coding & logic calculator (MCP server)\n"
            "\n"
            "Usage: codecalc [COMMAND]\n"
            "\n"
            "Commands:\n"
            "  doctor [--json] [--deep]           environment/runtime health report\n"
            "  serve-strict [ARGS]                authenticated HTTP execution service\n"
            "  serve-http [--host H] [--port P]   MCP over streamable HTTP\n"
            "  -h, --help                         show this message and exit\n"
            "  -V, --version                      show the codecalc version and exit\n"
            "\n"
            "With no command (the default), codecalc speaks MCP over stdio — the way an\n"
            "MCP client spawns it."
        )
        raise SystemExit(0)
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"codecalc {__version__}")
        raise SystemExit(0)
    if len(sys.argv) > 1 and sys.argv[1] in ("doctor", "--check", "--check-install"):
        # Flags read from the REST of argv, still without a parser. `--json`
        # previously did nothing at all: it was accepted, ignored, and the human
        # text printed anyway — a flag that no-ops is worse than one that does
        # not exist, because a script wraps it and parses prose forever.
        rest = sys.argv[2:]
        raise SystemExit(_doctor(as_json="--json" in rest, deep="--deep" in rest))
    if len(sys.argv) > 1 and sys.argv[1] == "serve-strict":
        # THE-830: the authenticated `/v1` strict execution service — the server
        # half of RemoteStrictExecutionProvider. Its own module and stdlib
        # http.server transport, separate from the MCP serve-http transport
        # above; it does its own bearer-required / non-loopback handling.
        from . import strict_service
        raise SystemExit(strict_service.main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "serve-http":
        rest = sys.argv[2:]
        host = (rest[rest.index("--host") + 1]
                if "--host" in rest and rest.index("--host") + 1 < len(rest)
                else "127.0.0.1")
        port_text = (rest[rest.index("--port") + 1]
                     if "--port" in rest and rest.index("--port") + 1 < len(rest)
                     else "8000")
        # FAIL CLOSED on a routable bind with no auth (THE-786). The whole
        # 127/8 block and ::1 are loopback — `ipaddress` decides, not a string
        # compare, because 127.0.0.2 is exactly as loopback as 127.0.0.1 and a
        # list of spellings would refuse it wrongly. An unparsable host
        # ("localhost", a DNS name) is loopback only for the names that mean
        # it; anything else is treated as routable, which errs closed.
        import ipaddress
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host in ("localhost", "ip6-localhost")
        if not loopback and not os.environ.get(HTTP_TOKEN_ENV):
            print(
                f"codecalc serve-http: refusing to bind {host}: it is not a "
                f"loopback address and {HTTP_TOKEN_ENV} is not set. An "
                "unauthenticated bind on a routable interface would expose "
                "every execution tool to that network. Set the variable to a "
                "strong secret and restart, or bind 127.0.0.1.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        mcp.run(
            transport="streamable-http",
            host=host,
            port=int(port_text),
            json_response=True,
            stateless_http=True,
        )
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
