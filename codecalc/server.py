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
import json
from pathlib import Path

from mcp.server import CacheHint, MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ImageContent

from . import (
    __version__,
    complexity,
    contract,
    doctor,
    errors,
    exact,
    execution_service,
    executor,
    logic,
    optimization,
    packages,
    providers,
    registry,
    runtimes,
    sessions,
    tools,
    translation,
    units,
)
from .mcp_middleware import timeout_middleware

_provider_registry = providers.configured_registry()
_execution_service = execution_service.ExecutionService(_provider_registry)
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

    Wraps at REGISTRATION so all 48 tools are covered by one change instead of
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
    # This wrapper is applied to all 48 tools, so stamping here makes that claim
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


# Rebind `mcp.tool` rather than renaming 48 decorator lines. The rename was the
# first attempt and broke four suites plus the CI round-trip check, all of which
# count declarations with `grep -c '^@mcp\.tool'` and read 0 against 48 served.
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
_COMPACT_DISCLOSURE = ("unenforced", "output_error", "provider")


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
        result = _session_service.execute(session_id, spec)
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
def session_files(session_id: str, path: str = "") -> dict:
    """List files in a session workspace (path is relative, '' = root)."""
    return _session_service.list_files(session_id, path)


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
    return packages.install(language, package, session_id=session_id, version=version)


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
    import asyncio
    import tempfile

    timeout = min(timeout, 300)

    # Every other tool falls back to the pure-Python executor when the Rust
    # binary is absent — a configuration the README explicitly supports. This
    # one used executor._rust directly, so argv[0] was None and it raised
    # TypeError. Streaming needs the binary (it tails run.out in a --workdir),
    # so fall back to a single non-streaming run rather than failing.
    if executor._rust is None:
        result = executor.execute(language, code, stdin=stdin, timeout=timeout,
                                  max_memory_mb=max_memory_mb,
                                  max_output_kb=max_output_kb,
                                  max_cpu=max_cpu, no_net=no_net)
        result["streamed"] = False
        result["note"] = ("no native executor binary; ran without streaming. "
                          "Build it (cargo build --release) for progress updates.")
        return result

    workdir = Path(tempfile.mkdtemp(prefix="codecalc-stream-"))
    # Recorded BEFORE the executed code gets this directory as its cwd (via
    # --workdir below), exactly like executor/src/main.rs's created_identity.
    # This dir is passed to the Rust executor as a caller-supplied --workdir,
    # so the Rust side never deletes it — that duty is entirely ours here, and
    # the unconditional shutil.rmtree() this used to be is exactly the gap
    # #38 reported: identity recorded at creation, re-checked immediately
    # before removal, so a rename-swap by the executed program is refused
    # rather than deleting whatever now sits at this path.
    created_identity = executor._dir_identity(workdir)
    try:
        # run the Rust executor directly with --workdir so run.out grows live
        args = [executor._rust, "--lang", language, "--timeout", str(timeout),
                "--workdir", str(workdir)]
        # Same flags, same conditions, as codecalc/executor.py builds for the
        # non-streaming path. The executor has always accepted --max-memory-mb
        # and --max-cpu (main.rs); this tool simply never passed them.
        if max_memory_mb > 0:
            args += ["--max-memory-mb", str(max_memory_mb)]
        if max_output_kb > 0:
            args += ["--max-output-kb", str(max_output_kb)]
        if max_cpu > 0:
            args += ["--max-cpu", str(max_cpu)]
        if no_net:
            args += ["--no-net"]
        sf = None
        if stdin:
            with tempfile.NamedTemporaryFile(mode="w", prefix="cc-stdin-",
                                             suffix=".txt", delete=False) as _sf:
                _sf.write(stdin)
                sf = _sf.name
            args += ["--stdin-file", sf]

        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        proc.stdin.write(code.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        # tail run.out while the child runs; report progress + partial output
        out_path = workdir / "run.out"
        last_len = 0
        partial = ""
        while proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.25)
            except TimeoutError:
                pass
            if out_path.exists():
                data = out_path.read_bytes()
                if len(data) > last_len:
                    chunk = data[last_len:].decode(errors="replace")
                    partial += chunk
                    last_len = len(data)
                    if ctx is not None:
                        try:
                            await ctx.report_progress(
                                progress=float(last_len), total=None,
                                message=f"stdout so far: {last_len} bytes",
                            )
                        except Exception:
                            pass

        stdout_b, stderr_b = await proc.communicate()
        if sf:
            Path(sf).unlink(missing_ok=True)
        try:
            result = json.loads(stdout_b.decode(errors="replace"))
            if isinstance(result, dict) and "ok" in result:
                result["streamed_partial"] = partial
                # The fallback path sets streamed=False; this one set nothing at
                # all, so `if not result["streamed"]` was true on the path that
                # DID stream. Both branches now answer the same question.
                result["streamed"] = True
                # The binary's JSON carries no `backend` key — executor.execute
                # adds it, and this path builds its own subprocess call instead
                # of going through that function, so it was the one execution
                # surface returning a result a caller could not attribute to a
                # backend. Same reasoning as executor.execute's own comment:
                # absent is indistinguishable from "an older build".
                result["backend"] = "rust"
                return result
            return {"ok": False,
                    "error": f"executor invalid output: {stderr_b[:200]!r}"}
        except Exception as exc:
            return {"ok": False, "error": f"stream failed: {exc}"}
    finally:
        executor._rmtree_checked(workdir, created_identity)


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
    """Check an SMT-LIB2 formula with Z3: sat/unsat/unknown plus a model. Example: '(declare-const x Int)(assert (> x 5))(check-sat)'."""
    return logic.z3_check(smt2)


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
    result = sessions.resource_read(session_id, path)
    if result is None:
        raise ValueError(f"no such file: {path}")
    data, mime = result
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
    d = sessions._session_dir(session_id)
    if not d.is_dir():
        return {"ok": False, "error": f"unknown session '{session_id}'"}
    target = sessions._jail(d, path)
    if not target.is_file():
        return {"ok": False, "error": f"no such file: {path}"}

    import mimetypes
    mime, _ = mimetypes.guess_type(str(target))
    is_image = bool(mime and mime.startswith("image/"))

    if is_image or as_image:
        if target.stat().st_size > 4 * 1024 * 1024:
            return {"ok": False, "error": "image too large (>4MiB)"}
        # fastmcp's Image(path=...) helper has no counterpart in the official
        # SDK; ImageContent is the protocol type and wants base64 itself.
        # as_image=True on a non-image file is honoured deliberately: the caller
        # is asserting it knows the bytes are renderable, so fall back to PNG
        # rather than refusing.
        return ImageContent(
            type="image",
            data=base64.b64encode(target.read_bytes()).decode("ascii"),
            mimeType=mime if is_image else "image/png",
        )

    data = target.read_bytes()
    truncated = len(data) > max_bytes
    return {"ok": True, "path": path, "size": len(data),
            "content": data[:max_bytes].decode(errors="replace"),
            "truncated": truncated,
            "resource": f"codecalc://session/{session_id}/files/{path}"}


@mcp.tool()
def session_run(session_id: str, entry_file: str, language: str | None = None,
                stdin: str = "", timeout: int = 30) -> dict:
    """Run a multi-file program in a session: execute `entry_file`, which may
    import other files already in the session workspace (helper.py, data/...).

    Runs as a fresh process in the session workdir (not the REPL worker), so
    relative imports and data files resolve. Returns stdout/stderr/verdict
    plus the entry file's path.
    """
    d = sessions._session_dir(session_id)
    if not d.is_dir():
        return {"ok": False, "error": f"unknown session '{session_id}'"}
    target = sessions._jail(d, entry_file)
    if not target.is_file():
        return {"ok": False, "error": f"no such file: {entry_file}"}

    # infer language from extension if not given
    if language is None:
        ext = target.suffix.lstrip(".")
        by_ext = {v: k for k, v in registry.EXTENSIONS.items()}
        language = by_ext.get(ext, "python3")

    code = target.read_text(errors="replace")
    result = executor.execute(language, code, stdin=stdin, timeout=timeout,
                              workdir=str(d))
    result["entry_file"] = entry_file
    result["language"] = language
    return result


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
    """
    return translation.verify_translation(
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
    """
    return optimization.verify_optimization(
        original, candidate, language, test_inputs=test_inputs,
        sizes=sizes, min_speedup=min_speedup)


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
    """
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("doctor", "--check", "--check-install"):
        # Flags read from the REST of argv, still without a parser. `--json`
        # previously did nothing at all: it was accepted, ignored, and the human
        # text printed anyway — a flag that no-ops is worse than one that does
        # not exist, because a script wraps it and parses prose forever.
        rest = sys.argv[2:]
        raise SystemExit(_doctor(as_json="--json" in rest, deep="--deep" in rest))
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
