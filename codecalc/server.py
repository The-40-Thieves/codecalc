"""FastMCP server exposing codecalc as model-usable tools.

Transport: stdio by default (what LiteLLM / Claude Desktop / any MCP client
spawns). Run:  python -m codecalc.server
"""

from __future__ import annotations

import json
import os
import shutil

from fastmcp import Context, FastMCP

from . import (
    complexity,
    context7,
    exact,
    executor,
    logic,
    optimization,
    packages,
    registry,
    runtimes,
    sessions,
    tools,
    translation,
    units,
)

mcp = FastMCP(
    "codecalc",
    instructions=(
        "Universal coding & logic calculator. Tools: list_languages (available "
        "runtimes), execute_code (run code in 30+ languages, returns stdout/"
        "stderr/exit/time), evaluate_expression (symbolic math via SymPy), "
        "truth_table (boolean logic), z3_check (SMT-LIB2 satisfiability), "
        "solve_linear (systems of equations), analyze_complexity (static Big-O), "
        "benchmark (empirical Big-O by running at increasing sizes), "
        "compare_execution (same code across many languages)."
    ),
)


@mcp.tool()
def list_languages() -> list[dict]:
    """List every language codecalc can execute, with extension, compile flag, and runtime availability on this machine."""
    langs = registry.all_languages()
    avail = executor.probe()
    for l in langs:
        l["available"] = avail.get(l["name"], True)
    return langs


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
) -> dict:
    """Execute `code` in `language` in a sandbox.

    Returns stdout, stderr, exit_code, duration_ms, cpu_ms, peak_memory_kb,
    verdict (OK/TLE/MLE/OLE/RTE).

    - `session_id`: run inside a session workspace (see session_start); with a
      stateful session (python3/node) interpreter state persists across calls.
    - `max_memory_mb` / `max_cpu`: per-call resource ceilings.
    - `max_output_kb`: raise/lower the stdout cap (default 64 KiB).
    - `no_net`: block network egress (LD_PRELOAD shim; dynamic binaries only).
    - `compact`: return only verdict + stdout, drop the heavy fields.
    """
    timeout = min(timeout, 120)
    if session_id:
        return sessions.execute(session_id, code, language=language,
                                stdin=stdin, timeout=timeout)
    result = executor.execute(language, code, stdin=stdin, timeout=timeout,
                              max_memory_mb=max_memory_mb,
                              max_output_kb=max_output_kb,
                              max_cpu=max_cpu, no_net=no_net)
    if compact:
        return {"ok": result.get("ok"), "verdict": result.get("verdict"),
                "stdout": result.get("stdout", ""),
                "exit_code": result.get("exit_code")}
    return result


@mcp.tool()
def session_start(language: str = "python3") -> dict:
    """Start a persistent session. python3/node get a stateful REPL worker
    (variables/imports persist across execute_code calls); other languages get
    a persistent workspace directory. Returns session_id."""
    return sessions.start(language)


@mcp.tool()
def session_stop(session_id: str) -> dict:
    """Stop a session: kill its REPL worker (if any) and delete its workspace."""
    return sessions.stop(session_id)


@mcp.tool()
def session_list() -> dict:
    """List active sessions and their languages/state."""
    return sessions.list_sessions()


@mcp.tool()
def session_files(session_id: str, path: str = "") -> dict:
    """List files in a session workspace (path is relative, '' = root)."""
    return sessions.list_files(session_id, path)


@mcp.tool()
def session_write_file(session_id: str, path: str, content: str) -> dict:
    """Write a file into a session workspace (relative path, no escapes).
    Use this to seed input data for executed code."""
    return sessions.write_file(session_id, path, content)


@mcp.tool()
def session_artifacts(session_id: str) -> dict:
    """List files created by executed code in a session (excluding runner
    internals like main.py/run.out)."""
    return sessions.artifacts(session_id)


@mcp.tool()
def install_package(language: str, package: str, session_id: str | None = None,
                    version: str | None = None) -> dict:
    """Install a package for a language (uv pip / npm / gem / go get / cargo add...).

    With session_id, installs into that session's workspace so executed code
    can import it. Without, installs into a shared cache. Requires network.
    """
    return packages.install(language, package, session_id=session_id, version=version)


@mcp.tool()
async def execute_code_stream(
    language: str,
    code: str,
    stdin: str = "",
    timeout: int = 30,
    max_output_kb: int = 0,
    no_net: bool = False,
    ctx: Context = None,
) -> dict:
    """Execute code and STREAM progress + partial output as it runs.

    Unlike execute_code (which returns only at exit), this reports progress
    notifications to the client while the program runs, so agents can see
    output before the process finishes. Returns the same result shape.
    """
    import asyncio
    import tempfile
    from pathlib import Path

    timeout = min(timeout, 300)
    workdir = Path(tempfile.mkdtemp(prefix="codecalc-stream-"))
    try:
        # run the Rust executor directly with --workdir so run.out grows live
        args = [executor._rust, "--lang", language, "--timeout", str(timeout),
                "--workdir", str(workdir)]
        if max_output_kb > 0:
            args += ["--max-output-kb", str(max_output_kb)]
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
            try:
                os.unlink(sf)
            except OSError:
                pass
        try:
            result = json.loads(stdout_b.decode(errors="replace"))
            if isinstance(result, dict) and "ok" in result:
                result["streamed_partial"] = partial
                return result
            return {"ok": False,
                    "error": f"executor invalid output: {stderr_b[:200]!r}"}
        except Exception as exc:
            return {"ok": False, "error": f"stream failed: {exc}"}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@mcp.tool(timeout=20)
def evaluate_expression(expression: str) -> dict:
    """Symbolically evaluate or simplify a math expression, e.g. 'integrate(x**2, x)' or 'sqrt(144) + 2**10'."""
    return logic.evaluate_expression(expression)


@mcp.tool(timeout=20)
def truth_table(expression: str) -> dict:
    """Build the truth table for a boolean expression: 'a and b or not c', 'p xor q', 'a implies b'."""
    return logic.truth_table(expression)


@mcp.tool(timeout=30)
def z3_check(smt2: str) -> dict:
    """Check an SMT-LIB2 formula with Z3: sat/unsat/unknown plus a model. Example: '(declare-const x Int)(assert (> x 5))(check-sat)'."""
    return logic.z3_check(smt2)


@mcp.tool(timeout=20)
def solve_linear(system: str, variables: str) -> dict:
    """Solve a system of equations; `system` is ';'-separated equations, `variables` comma-separated. Example: system='x + y = 10; x - y = 2', variables='x, y'."""
    vars_ = [v.strip() for v in variables.split(",") if v.strip()]
    return logic.solve_linear(system, vars_)


@mcp.tool(timeout=20)
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
    """
    return runtimes.status(languages or None)


@mcp.tool()
def update_runtimes(languages: str = "", apply: bool = False, timeout: int = 600) -> dict:
    """Update language runtimes. SAFE BY DEFAULT: with apply=False this is a
    dry run — it returns the update commands that WOULD run without changing
    anything. Pass apply=True to actually execute them (mise up, rustup update,
    swiftly update, apt-get upgrade of language packages, npm -g update, uv tool
    upgrade). `languages` = comma-separated subset; empty = all.
    """
    return runtimes.update(languages or None, apply=apply, timeout=timeout)


@mcp.resource("codecalc://session/{session_id}/files/{path*}",
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
    from fastmcp.utilities.types import Image
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
        return Image(path=str(target))

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


@mcp.tool(timeout=120)
def translate_code(code: str, source: str, target: str,
                   test_inputs: list[str] | None = None) -> dict:
    """Port `code` from `source` language to `target`, then VERIFY equivalence.

    An LLM translates; the executor runs BOTH versions on the same test inputs
    and compares stdout. Accepted only if outputs match (one retry feeding the
    diff back). Returns translated_code + per-input verification. Example:
    source='python3', target='go', code='import sys\nn=int(sys.stdin.readline())\nprint(n*2)'.
    """
    return translation.translate_code(code, source, target,
                                      test_inputs=test_inputs)


@mcp.tool()
def compare_edge_cases(snippets: dict[str, str],
                       inputs: list[str] | None = None) -> dict:
    """Run the same logic in N languages on edge-case inputs and flag divergence.

    `snippets` maps language -> code (provide a correct snippet per language;
    use translate_code first if you only have one). Default inputs cover empty,
    zero, negative, and float-precision cases: ['', '0', '1', '-1', '10',
    '100', '0.1\\n0.2']. Returns a per-input matrix plus a divergences list
    where languages disagree on identical input.
    """
    return translation.compare_edge_cases(snippets, inputs=inputs)


@mcp.tool()
def context7_docs(library_id: str, query: str, fast: bool = True) -> dict:
    """Fetch up-to-date library documentation from context7 for any language.

    `library_id` is '/owner/repo' (e.g. '/numpy/numpy', '/golang/go',
    '/Z3Prover/z3'). Returns current, LLM-reranked doc snippets for `query`.
    Use before writing code that depends on library APIs you are unsure about.
    """
    return context7.docs(library_id, query, fast=fast)


@mcp.tool(timeout=120)
def optimize_code(code: str, language: str,
                  test_inputs: list[str] | None = None,
                  sizes: list[int] | None = None,
                  min_speedup: float = 1.15) -> dict:
    """Optimize code and PROVE the improvement.

    An LLM proposes an optimized version; the executor verifies correctness
    (identical stdout on test inputs) AND measures speedup (same sizes,
    min-of-repeats, baseline-subtracted). Accepted only if correct AND
    measurably faster (default 1.15x); retried once with the failure reason.
    Returns optimized_code, speedup_ratio, before/after timings.
    """
    return optimization.optimize_code(code, language,
                                      test_inputs=test_inputs, sizes=sizes,
                                      min_speedup=min_speedup)


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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
