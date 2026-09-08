"""Line-level execution tracing for `trace_execution` (MCP tool in server.py).

WHAT THIS IS. `execute_code` answers "what did the program print". This
answers "why" — which lines ran, in what order, what the locals looked like
at each step, which branch of an if/while/for/try actually fired, and which
never did. Python-only in v1 (see `UNSUPPORTED_LANGUAGE_REMEDY` below): the
mechanism is `sys.settrace`, which is a CPython interpreter hook with no
equivalent this package can drive uniformly across the other 30 registered
languages.

MECHANISM, end to end.

1. `execute_trace()` (the entry point server.py calls) refuses anything but
   python3 before touching a filesystem or a subprocess — see
   `_refuse_unsupported_language`.

2. It owns a workdir, the same pattern `executor.execute_stream` uses for the
   identical reason: `executor.execute()` never deletes a CALLER-supplied
   workdir (`caller_workdir` in `_execute_python`; the Rust binary the same,
   given `--workdir`), so passing one explicitly is what lets this module
   read a file back after the run finishes, before the directory is gone.

3. The user's source is written as a SIBLING file at the WORKDIR ROOT, not
   inside `registry.RUN_SCRATCH_DIRNAME` (`.codecalc-run/`) — deliberately
   different from the instinctive "put everything next to main.py" reading of
   how a compile-then-run language stages its files. `.codecalc-run/` is
   WIPED AND RECREATED by `_reset_run_scratch_dir` (both backends) at the
   START of every `executor.execute()` call, before anything is written or
   spawned — so a sibling file placed there *before* calling `execute()`
   would be deleted the instant that call begins, before the child process
   that needs it ever starts. The workdir ROOT is untouched by that reset
   (only its `.codecalc-run/` subdirectory is), which is exactly the
   "sibling file at the workdir root, readable by relative or absolute path
   because the RUN step's cwd is the workdir root" pattern node's own sibling
   shim (`executor.NODE_SIBLING_SHIM_JS`) already relies on — this reuses it
   for the wrapper's own source lookup instead of a require() shim.

4. `execute_trace()` calls `executor.execute(language="python3", code=<the
   WRAPPER>, workdir=<the workdir above>, ...)` — the SAME function
   `execute_code` calls, with the SAME two backends, so `trace_execution`
   gets the identical sandbox, the identical stdout/stderr/verdict/timeout/
   OLE/MLE handling, for free. The "code" that actually gets staged as
   `.codecalc-run/main.py` and run is not the user's program — it is a
   generated harness (`_build_wrapper_source`) that reads the sibling file,
   installs `sys.settrace`, execs the user source, and writes trace events
   to a file.

5. The trace file lives at `<workdir>/.codecalc-run/trace_events.jsonl` —
   inside the scratch directory, same as `run.out` in `executor.execute_
   stream`. That is safe DESPITE the wipe-on-every-call rule in step 3,
   because by the time the wrapper (running AS the spawned child) writes
   into it, the wipe already happened — `_reset_run_scratch_dir` runs before
   spawn, not concurrently with it. This is exactly `execute_stream`'s own
   `run.out` precedent: a file written during the run, inside the scratch
   dir, read back afterward by the caller before the caller (not the
   executor) deletes the workdir.

6. Once `executor.execute()` returns, this module reads the trace file (if
   it exists — a hard kill before the wrapper opened it leaves none),
   deletes the workdir itself (`executor._rmtree_checked`, identity-checked,
   the same call `execute_stream` makes in its `finally`), and merges the
   parsed trace data into the same envelope `execute_code` would have
   returned.

WHY THE PROGRAM'S OWN BEHAVIOUR IS UNCHANGED. The wrapper never prints
anything of its own to stdout, and stdin is inherited unmodified (the
wrapper and the user code run in the same OS process, so `input()` reads the
same fd `execute_code` would have connected). Its stderr handling is the
subtle part: an uncaught exception raised through `exec()` carries the
wrapper's own call frame in its `__traceback__` in addition to the user
code's — printing that verbatim would show the caller a stack trace with an
extra, meaningless frame in it. `_run_source` trims the traceback to the
first frame whose `co_filename` is the user's own file before printing,
which was verified BYTE FOR BYTE against a real `python3 program.py`
subprocess for a raise several frames deep (see tests/test_trace_execution.py
and this module's own `_run_source` docstring) — same for a top-level
`SystemExit` (replicated by hand: `code is None` -> exit 0, an int -> that
exit code, anything else -> printed to stderr and exit 1, which is what
CPython's own interpreter does for an uncaught `SystemExit`) and for a
`SyntaxError` in the user source, which CPython prints with NO
"Traceback (most recent call last):" header at all —
`traceback.print_exception(type(exc), exc, None)` (a `None` tb, regardless of
what the real chain looked like) reproduces that exactly, verified against a
real `python3 broken.py` run byte for byte.

CAPS. `max_events` (caller-facing) and `_MAX_TRACE_BYTES` (internal, not a
tool parameter — a backstop against a caller who raises `max_events` far
past what the ~499,520-char MCP result-size ceiling `server.py` advertises
for this tool can actually hold, even before considering that most events
carry several ~200-char-capped locals reprs each). Either cap trips tracing,
never the program: the wrapper's tracer function returns `None` and calls
`sys.settrace(None)`, which stops it from being invoked again, but the
`exec()` call underneath keeps running the user's code to completion at full
speed — stdout, exit_code and verdict are the real ones, only the trace is
partial. A trailing JSON line naming which cap fired is written the instant
it trips (see `_TRUNCATION_MARKER_KEY`); this module treats that marker,
not a client-side inference from the raw event count, as the source of
truth for `truncated`/`truncated_reason` — a program whose trace HAPPENS to
produce exactly `max_events` real events and then finishes on its own would
otherwise be misreported as truncated with no cap ever having fired. A hard
kill (TLE/OLE/MLE) before either cap trips leaves this marker absent, so a
killed run reports `truncated: false` — the standard `verdict`/`timed_out`/
`output_truncated` fields already carry that story, and overloading
`truncated_reason` with a third, cap-unrelated meaning would blur two
different questions ("did codecalc's own recording limit cut this short" vs
"did the sandbox kill the process") into one field.

TRUST BOUNDARY — READ BEFORE TRUSTING `events`. The trace is produced BY the
traced process, using the SAME privilege the traced code itself runs with.
It is exactly as trustworthy as that program's own stdout: a debugging aid,
never an attestation of behaviour. A sandboxed program can compute the trace
sink's own path from `__file__` (it is a sibling of the workdir the harness
already discloses) and append arbitrary bytes to it — a cross-vendor review
reproduced this with a forged trailing `return` event, keeping it last via
`os._exit(0)` to skip the harness's own cleanup entirely. This module cannot
stop that (in-process code sharing the traced program's own uid is not a
security boundary this package can construct from Python alone — see
`_refuse_unsupported_provider`'s docstring for the same "cannot exclude same-
privilege code" limitation, one layer up), so instead of pretending
otherwise it bounds the DAMAGE and SURFACES the tampering:

  * The parser (`_parse_trace_file`) never reads more than `_MAX_TRACE_BYTES
    + 4 KiB` off disk (`os.open`/`os.read` in a bounded loop, never
    `Path.read_text()` of the whole file) — a sandboxed program appending
    megabytes of junk cannot force this (unsandboxed, parent-process)
    parser into unbounded work. If the file on disk is bigger than that
    window, `truncated: true, truncated_reason: "trace_file_exceeded"` says
    so and only the bounded prefix is read.
  * Every candidate line is schema-validated (`_validate_event`): exactly
    the keys its `event` value permits, correct types, `line` within the
    program's own line range, and `step` continuing the exact monotonic
    sequence the harness's OWN counter would have produced. Anything else —
    malformed JSON, an unknown key, a skipped or repeated `step` — is
    DISCARDED, counted in `discarded_events`, and never raises.
  * The harness writes a final `{"event": "end", "step": N, "emitted": N}`
    line, whether or not either cap fired, in every path `_run_source`
    returns through normally. `events_consistent` is `false` whenever that
    line is missing, its `emitted` count disagrees with how many events this
    parser actually accepted, or anything appears after it — the ONE thing
    `os._exit(0)` (or any other bypass of the harness's own return path)
    cannot fake, because it never runs the code that writes it. A forged
    event placed BEFORE a genuine `end` line still inflates the accepted
    count past what `emitted` claims, so `events_consistent` catches that
    case too, not just an outright-missing terminator.

None of this turns `events` into a signed record — a sufficiently determined
attacker with same-process code execution can still corrupt it in ways this
parser accepts (see `_validate_event`'s own docstring for the one class of
forgery — an exact step-and-shape match — this design cannot detect at the
per-line level, only via the aggregate `events_consistent` check). What it
guarantees is that a CARELESS or MODERATE corruption is either rejected
outright or visibly flagged, and that no amount of appended data can force
this process to do more than a fixed, small amount of extra work.

THREADS. `sys.settrace` is a per-THREAD hook; installing it in the main
thread traces only frames running there. A program that starts its own
thread runs code this tracer never sees `call`/`line`/`return`/`exception`
events for — that code still executes normally (this only affects what
`events` can show, never `stdout`/`exit_code`), but a reader must not read
`events` as a complete account of everything the program did. The harness
checks `threading.active_count()` cheaply on every `call` event and once
more as it exits; the first time it observes more than one thread, it flags
that in the trace file, and `execute_trace` copies `"threads: only the main
thread is traced"` into the result's own `unenforced` array — the same
disclosure list `execute_code` already uses for a guarantee it could not
apply.

FALLBACK-BACKEND OLE RACE. The pure-Python fallback's own output-cap
enforcement (`executor._run_step`) polls `overflow.is_set()` on a 20ms
timer, racing the child's own natural exit — confirmed PRE-EXISTING and
already nondeterministic for plain `execute_code` on this backend (the same
program's `exit_code` flips between 0 and a negative signal across repeated
runs at sizes near the cap). This module's extra per-event file I/O shifts
that race's timing enough to change which side usually wins for a given
program, so `trace_execution`'s `exit_code` on an OLE verdict, fallback
backend only, is not reliably comparable to what `execute_code` reports for
the identical program — `verdict` and `output_truncated` are unaffected and
always agree. Not fixed here: the race lives in shared executor code every
tool depends on, and this module works around its OWN sensitivity to it
rather than changing that polling loop. `execute_trace` adds `"exit_code:
fallback backend may differ on output-limit kills"` to `unenforced`
whenever `backend == "python"` and `verdict == "OLE"`. The Rust backend's
own cap enforcement is not timing-sensitive this way — confirmed
deterministic across dozens of trials — so this note is fallback-only.
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
import textwrap
from collections import Counter
from pathlib import Path
from typing import Any

from . import contract, errors, executor, providers, registry

#: Only python3 is traced in v1 — see the module docstring. `registry.
#: canonical()` resolves aliases ("python", "py", "python3.12", ...) to this
#: same name, so `trace_execution(language="python", ...)` is accepted, not
#: rejected for spelling a real alias differently than execute_code would.
SUPPORTED_LANGUAGE = "python3"

#: Named once, quoted in the refusal message AND in server.py's docstring /
#: docs/deployment or tier notes, so the two never drift apart.
UNSUPPORTED_LANGUAGE_REMEDY = (
    "trace_execution only supports python3 in v1 (sys.settrace is a CPython "
    "hook this package cannot drive uniformly across the other registered "
    "languages) — call execute_code for any other language"
)

#: The sibling file the user's source is written to, at the WORKDIR ROOT —
#: see the module docstring's step 3 for why the workdir root and not
#: `.codecalc-run/`. Underscore-prefixed and specific enough that a user
#: program's own relative file access is very unlikely to collide with it —
#: the same reasoning `executor._NODE_SIBLING_SHIM_NAME` documents for its
#: own sibling file.
_USER_SOURCE_FILENAME = "_codecalc_trace_target.py"

#: Where the wrapper writes trace events — INSIDE the scratch directory, same
#: as `run.out`. See the module docstring's step 5.
_TRACE_FILENAME = "trace_events.jsonl"

#: Internal safety net, not a caller-facing parameter. `server.py`'s
#: `_MAX_RESULT_SIZE_CHARS` bounds this tool's whole serialized result at
#: 499,520 chars; 512 KiB of raw trace JSON leaves comfortable room for the
#: rest of the envelope (stdout/stderr/branches/etc.) alongside it even after
#: `json.dumps` re-serializes the parsed events back into the tool's own
#: result. A caller who wants more should ask for a NARROWER trace (a smaller
#: `code` snippet, or their own instrumentation), not this tool holding
#: multiple megabytes of locals dumps in one MCP reply.
_MAX_TRACE_BYTES = 512 * 1024

#: The JSON key that marks the trailing "why tracing stopped" line the
#: wrapper writes the instant a cap trips — see the module docstring's
#: "CAPS" section for why this is the source of truth over inferring
#: truncation from the raw event count.
_TRUNCATION_MARKER_KEY = "__trace_truncated__"

#: The JSON key marking the harness's own "more than one thread was alive"
#: disclosure — see the module docstring's "THREADS" section.
_THREAD_MARKER_KEY = "__trace_multithreaded__"

#: Slack ABOVE `_MAX_TRACE_BYTES` the parser will read before concluding the
#: file is bigger than the harness could legitimately have written — enough
#: for the truncation marker, the final `end` line, and JSON overhead on the
#: last real event, none of which count against the wrapper's own in-process
#: `_MAX_TRACE_BYTES` check (see `_stop_tracing`). See the module docstring's
#: "TRUST BOUNDARY" section for why this bound exists at all: it is what
#: keeps a sandboxed program appending megabytes to the trace file from
#: forcing this UNSANDBOXED parser to do unbounded work.
_TRACE_READ_SLACK_BYTES = 4096
_MAX_TRACE_READ_BYTES = _MAX_TRACE_BYTES + _TRACE_READ_SLACK_BYTES

#: The event types a trace line may legitimately declare, and the EXTRA keys
#: (beyond `_BASE_EVENT_KEYS`) each one requires — no more, no fewer.
#: `_validate_event` requires an exact key-set match, so an event carrying
#: an extra or missing key is discarded rather than partially trusted.
_BASE_EVENT_KEYS = frozenset({"step", "line", "event", "func", "locals"})
_EVENT_EXTRA_KEYS: dict[str, frozenset[str]] = {
    "line": frozenset(),
    "call": frozenset(),
    "return": frozenset({"return_value"}),
    "exception": frozenset({"exception_type", "exception_message"}),
}

#: Defensive ceilings on a single event's string/dict fields, independent of
#: the whole-file byte bound above — a forged event that passes every other
#: check still cannot make one line disproportionately large. The wrapper's
#: own `_LOCALS_REPR_LIMIT` (200) already caps a legitimate repr well under
#: `_MAX_STR_FIELD_LEN`; the gap is headroom, not a promise either value is
#: exact.
_MAX_FUNC_LEN = 200
_MAX_STR_FIELD_LEN = 4096
_MAX_LOCALS_ENTRIES = 200

#: Copied into the result's own `unenforced` array — see the module
#: docstring's "THREADS" / "FALLBACK-BACKEND OLE RACE" sections.
THREADS_UNENFORCED_NOTE = "threads: only the main thread is traced"
FALLBACK_OLE_EXIT_CODE_UNENFORCED_NOTE = (
    "exit_code: fallback backend may differ on output-limit kills"
)

#: AST node types that are branch POINTS for the `branches` report — every
#: line where control can diverge. `ast.Try`'s `lineno` is its `try:` line;
#: `except`/`finally` clauses are not separately counted; each is entered at
#: most once per exception per try, and this report is deliberately the
#: cheap "which branch ran" view the module docstring promises, not a full
#: control-flow-graph edge coverage tool.
_BRANCH_NODE_TYPES: tuple[type, ...] = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try)
if hasattr(ast, "TryStar"):  # 3.11+, exception groups — additive, never required
    _BRANCH_NODE_TYPES = (*_BRANCH_NODE_TYPES, ast.TryStar)


def _refuse_unsupported_provider(provider: str | None) -> dict[str, Any] | None:
    """A `rejected`-shape result if `provider` names anything but the local
    Rust/Python-fallback executor, else None.

    `execute_code`/`execute_code_stream` route through `ExecutionService`,
    which can broker to a remote or gVisor-strict provider adapter — this
    tool cannot: the harness in `_build_wrapper_source` stages a companion
    source file and reads a trace file back out of the SAME workdir it
    handed the executor, which only `providers.LocalExecutionProvider`
    (`executor.execute()` directly) guarantees it can do. Refusing a named
    non-local provider up front is honest about that; silently running
    locally anyway while a caller believes their `provider="piston"` request
    was honoured would not be.
    """
    if provider is None or provider == providers.LocalExecutionProvider.provider_id:
        return None
    return contract.stamp(errors.error_result(
        errors.VALIDATION,
        f"trace_execution only runs through the local executor "
        f"({providers.LocalExecutionProvider.provider_id!r}); "
        f"provider {provider!r} is not supported here",
        remedy="omit `provider` or pass 'local'; call execute_code for a "
               "remote/strict provider",
        requested_provider=provider,
    ))


def _refuse_unsupported_language(language: str) -> dict[str, Any] | None:
    """A `rejected`-shape result if `language` is not python3, else None.

    Resolved through `registry.canonical()` so an alias ("python", "py",
    "python3.12") is accepted the same as `execute_code` accepts it — a
    caller should not have to know this tool is pickier about spelling than
    the one it wraps, only about which LANGUAGES it covers.
    """
    canon = registry.canonical(language)
    if canon == SUPPORTED_LANGUAGE:
        return None
    return contract.stamp(errors.error_result(
        errors.VALIDATION,
        f"trace_execution does not support language {language!r} "
        f"(resolved: {canon!r}); python3 only in v1",
        remedy=UNSUPPORTED_LANGUAGE_REMEDY,
    ))


def _analyze_source(source: str) -> tuple[set[int], set[int]]:
    """`(branch_lines, executable_lines)` from a static AST parse.

    Raises `SyntaxError` exactly when the user's own source does not parse —
    callers catch that and report an empty analysis rather than crash: a
    program that failed to compile executed none of its lines, so "no branch
    counts, nothing executed, nothing never-executed" is the honest answer,
    not a missing tool result.
    """
    tree = ast.parse(source)
    branch_lines = {n.lineno for n in ast.walk(tree) if isinstance(n, _BRANCH_NODE_TYPES)}
    executable_lines = {n.lineno for n in ast.walk(tree) if isinstance(n, ast.stmt)}
    return branch_lines, executable_lines


def _build_wrapper_source(user_source_path: str, trace_path: str,
                           max_events: int, max_trace_bytes: int) -> str:
    """The harness that becomes `.codecalc-run/main.py` for this run.

    Everything the harness itself does (`_emit`, `_diff_locals`, `json.dumps`,
    ...) runs in a DIFFERENT file than `user_source_path`, so the tracer's own
    filename check (`frame.f_code.co_filename != _USER_SOURCE`) excludes it
    from the trace automatically — no separate "don't trace myself" bookkeeping
    needed. `sys.settrace`'s global tracer fires for every new frame in the
    WHOLE process regardless of who called it, which is what lets a callback
    the harness never sees directly (the user's own nested function calls)
    still get traced: each is a fresh 'call' event, filtered by filename, same
    as the top-level module frame.

    Paths are embedded via `!r` (Python's own repr), which round-trips
    correctly through backslashes/quotes on every platform this package
    targets — the same reasoning `registry.source_arg` documents for why a
    raw f-string path is not safe to splice into generated source.

    Writes a final `{{"event": "end", "step": N, "emitted": N}}` line on every
    path `_run_source` returns through — see the module docstring's "TRUST
    BOUNDARY" section for what that buys the parser. Installs a FRESH
    `__main__` module (`sys.modules["__main__"]`) with the user's own
    `__file__` before `exec()`-ing their code, restored afterward, so
    `sys.modules['__main__']` matches what `execute_code` would show instead
    of leaking this harness's own module object and internals (cross-vendor
    review, "sys.modules leaks the wrapper").
    """
    return textwrap.dedent(f"""\
        import json
        import sys
        import threading
        import traceback
        import types

        _USER_SOURCE = {user_source_path!r}
        _TRACE_PATH = {trace_path!r}
        _MAX_EVENTS = {int(max_events)!r}
        _MAX_TRACE_BYTES = {int(max_trace_bytes)!r}
        _LOCALS_REPR_LIMIT = 200

        _trace_file = open(_TRACE_PATH, "w", encoding="utf-8")
        _event_count = 0
        _trace_bytes = 0
        _step = 0
        _frame_snapshots = {{}}
        # Frames currently UNWINDING due to a propagating exception (an
        # 'exception' event seen, no subsequent 'line' event in that same
        # frame since). CPython fires a 'return' event with arg=None for a
        # frame an exception is unwinding through, same as a genuine `return`
        # with no value — reported verbatim, that reads as "the function
        # returned None" for a call that never returned at all. A 'line'
        # event for a frame already in this set means the exception was
        # caught and handled INSIDE that frame (execution continued past it),
        # so the frame is no longer unwinding and a later real return must
        # not be suppressed.
        _unwinding = set()
        # See the module docstring's "THREADS" section: sys.settrace is
        # per-thread, so a second thread's frames are invisible here
        # regardless of filename. Checked cheaply (once per 'call' event,
        # never per 'line') rather than not at all.
        _multithreaded = False
        _thread_marker_written = False


        def _safe_repr(value):
            try:
                r = repr(value)
            except Exception as exc:
                r = "<unrepresentable %s: %s>" % (type(value).__name__, exc)
            if len(r) > _LOCALS_REPR_LIMIT:
                r = r[:_LOCALS_REPR_LIMIT] + "...<truncated>"
            return r


        def _diff_locals(frame_id, current):
            prev = _frame_snapshots.get(frame_id, {{}})
            reprs = {{name: _safe_repr(val) for name, val in current.items()}}
            changed = {{name: rep for name, rep in reprs.items() if prev.get(name) != rep}}
            _frame_snapshots[frame_id] = reprs
            return changed


        def _emit(event):
            global _event_count, _trace_bytes
            line = json.dumps(event) + "\\n"
            _trace_bytes += len(line.encode("utf-8"))
            _event_count += 1
            _trace_file.write(line)
            _trace_file.flush()


        def _note_threads():
            global _multithreaded, _thread_marker_written
            if not _multithreaded and threading.active_count() > 1:
                _multithreaded = True
            if _multithreaded and not _thread_marker_written:
                _thread_marker_written = True
                try:
                    _trace_file.write(
                        json.dumps({{{_THREAD_MARKER_KEY!r}: True}}) + "\\n")
                    _trace_file.flush()
                except Exception:
                    pass


        def _stop_tracing(reason):
            sys.settrace(None)
            _emit({{{_TRUNCATION_MARKER_KEY!r}: True, "reason": reason}})


        def _tracer(frame, event, arg):
            global _step
            if event == "call":
                _note_threads()
            if frame.f_code.co_filename != _USER_SOURCE:
                return None
            if _event_count >= _MAX_EVENTS:
                _stop_tracing("max_events")
                return None
            if _trace_bytes >= _MAX_TRACE_BYTES:
                _stop_tracing("max_trace_bytes")
                return None
            frame_id = id(frame)
            func = frame.f_code.co_name
            lineno = frame.f_lineno
            if event in ("call", "line"):
                if event == "line":
                    _unwinding.discard(frame_id)
                changed = _diff_locals(frame_id, frame.f_locals)
                _step += 1
                _emit({{"step": _step, "line": lineno, "event": event,
                       "func": func, "locals": changed}})
            elif event == "return":
                was_unwinding = frame_id in _unwinding
                _unwinding.discard(frame_id)
                if not was_unwinding:
                    changed = _diff_locals(frame_id, frame.f_locals)
                    _step += 1
                    _emit({{"step": _step, "line": lineno, "event": "return",
                           "func": func, "locals": changed,
                           "return_value": _safe_repr(arg)}})
                _frame_snapshots.pop(frame_id, None)
            elif event == "exception":
                exc_type, exc_value, _exc_tb = arg
                _unwinding.add(frame_id)
                changed = _diff_locals(frame_id, frame.f_locals)
                _step += 1
                _emit({{"step": _step, "line": lineno, "event": "exception",
                       "func": func, "locals": changed,
                       "exception_type": exc_type.__name__,
                       "exception_message": _safe_repr(str(exc_value))}})
            return _tracer


        def _run_source():
            \"\"\"Compile + exec the user's source, reproducing exactly what
            `python3 <file>` would print/exit for the same three outcomes
            execute_code's own docstring distinguishes: a syntax error (no
            'Traceback' header at all — verified byte for byte against a real
            interpreter), an uncaught exception (the traceback trimmed to
            start at the user's own top frame, dropping this wrapper's exec()
            call site, ALSO verified byte for byte), and sys.exit — CPython's
            own convention for an uncaught SystemExit: None -> 0, an int ->
            that code, anything else -> printed to stderr and exit 1.
            \"\"\"
            try:
                with open(_USER_SOURCE, "r", encoding="utf-8") as fh:
                    source_text = fh.read()
                code_obj = compile(source_text, _USER_SOURCE, "exec")
            except SyntaxError as exc:
                sys.settrace(None)
                traceback.print_exception(type(exc), exc, None, file=sys.stderr)
                return 1
            sys.settrace(_tracer)
            # A FRESH module object, not a bare dict, so introspection from
            # inside the traced program (sys.modules['__main__'], its
            # __file__, __dict__) matches what execute_code would show —
            # not this harness's OWN module/globals/temp-file identity.
            # Restored in the finally below regardless of outcome.
            user_module = types.ModuleType("__main__")
            user_module.__file__ = _USER_SOURCE
            user_module.__builtins__ = __builtins__
            _saved_main = sys.modules.get("__main__")
            sys.modules["__main__"] = user_module
            try:
                exec(code_obj, user_module.__dict__)
            except SystemExit as exc:
                sys.settrace(None)
                code = exc.code
                if code is None:
                    return 0
                if isinstance(code, int):
                    return code
                print(str(code), file=sys.stderr)
                return 1
            except BaseException as exc:
                sys.settrace(None)
                tb = exc.__traceback__
                while tb is not None and tb.tb_frame.f_code.co_filename != _USER_SOURCE:
                    tb = tb.tb_next
                traceback.print_exception(type(exc), exc, tb, file=sys.stderr)
                return 1
            else:
                return 0
            finally:
                sys.settrace(None)
                if _saved_main is not None:
                    sys.modules["__main__"] = _saved_main
                else:
                    sys.modules.pop("__main__", None)


        sys.argv = [_USER_SOURCE]
        _exit_code = _run_source()
        _note_threads()
        try:
            _trace_file.write(
                json.dumps({{"event": "end", "step": _step,
                            "emitted": _event_count}}) + "\\n")
            _trace_file.flush()
        except Exception:
            pass
        _trace_file.close()
        sys.exit(_exit_code)
        """)


def _bounded_read(path: Path, limit: int) -> tuple[bytes, bool]:
    """`(data, exceeded)` — at most `limit` bytes off `path`, via `os.open`/
    `os.read` in a loop, NEVER `Path.read_text()` of the whole file. `exceeded`
    is true when the file on disk is bigger than `limit`, regardless of
    whether the bytes actually read turn out to be well-formed.

    This is the one function standing between a sandboxed program appending
    an unbounded amount of data to the trace sink and this UNSANDBOXED parent
    process doing unbounded work reading it back — see the module docstring's
    "TRUST BOUNDARY" section. `os.fstat` on the SAME fd being read, not a
    separate `path.stat()`, so the size and the bytes come from one open of
    the file rather than two lookups that could race a concurrent writer.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        to_read = min(size, limit)
        chunks: list[bytes] = []
        remaining = to_read
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), size > limit
    finally:
        os.close(fd)


def _validate_event(obj: object, expected_step: int, max_line: int) -> dict | None:
    """A clean, schema-conformant copy of `obj` if it is a legitimate trace
    event for step `expected_step`, else `None`.

    Exact key-set match (`_BASE_EVENT_KEYS` plus exactly the extras
    `_EVENT_EXTRA_KEYS[event]` names — no more, no fewer), correct types, and
    `step == expected_step`: the running counter this parser expects next,
    which only ever matches the harness's OWN monotonic `_step` sequence
    unless a forger happens to guess the exact next value. That ONE class of
    forgery — an event that also matches the expected step — is the kind
    this function cannot catch on its own; `execute_trace`'s aggregate
    `events_consistent` check (against the harness's own `end` line) is what
    catches it instead, because such an event still inflates the accepted
    count past what the harness's real, in-memory counter reported. See the
    module docstring's "TRUST BOUNDARY" section.

    Returns a FRESH dict built field-by-field, never `obj` itself passed
    through: an attacker-controlled dict reaching the result verbatim would
    make this validator a filter on ADMISSION only, not on CONTENT, the
    moment a future field is added to `obj` without a matching check here.
    """
    if not isinstance(obj, dict):
        return None
    event = obj.get("event")
    extra = _EVENT_EXTRA_KEYS.get(event) if isinstance(event, str) else None
    if extra is None:
        return None
    if set(obj) != _BASE_EVENT_KEYS | extra:
        return None
    step = obj.get("step")
    if not isinstance(step, int) or isinstance(step, bool) or step != expected_step:
        return None
    line = obj.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or not (0 <= line <= max_line):
        return None
    func = obj.get("func")
    if not isinstance(func, str) or len(func) > _MAX_FUNC_LEN:
        return None
    locals_ = obj.get("locals")
    if (not isinstance(locals_, dict) or len(locals_) > _MAX_LOCALS_ENTRIES
            or not all(isinstance(k, str) and len(k) <= _MAX_FUNC_LEN
                       and isinstance(v, str) and len(v) <= _MAX_STR_FIELD_LEN
                       for k, v in locals_.items())):
        return None
    clean = {"step": step, "line": line, "event": event, "func": func,
             "locals": dict(locals_)}
    for key in extra:
        value = obj.get(key)
        if not isinstance(value, str) or len(value) > _MAX_STR_FIELD_LEN:
            return None
        clean[key] = value
    return clean


def _parse_trace_file(path: Path, code: str, max_events: int) -> dict[str, Any]:
    """The trusted-as-far-as-possible reading of a trace file the SANDBOXED
    program had write access to. See the module docstring's "TRUST BOUNDARY"
    section for the threat this defends against and what it does not.

    Returns `{"events", "truncated", "truncated_reason", "discarded_events",
    "events_consistent", "multithreaded"}`. A trace file that does not exist
    (a hard kill before the wrapper opened it) or ends mid-line (a hard kill
    mid-write, despite the per-line flush) is read as "no more trace to
    show", never as an error — the envelope's own `verdict`/`timed_out`
    already say the run was killed, and `events_consistent` is correctly
    `false` in that case too (the harness's own `end` line was never
    reached).
    """
    empty = {"events": [], "truncated": False, "truncated_reason": None,
              "discarded_events": 0, "events_consistent": False,
              "multithreaded": False}
    if not path.is_file():
        return empty

    data, exceeded = _bounded_read(path, _MAX_TRACE_READ_BYTES)
    truncated = exceeded
    truncated_reason = "trace_file_exceeded" if exceeded else None

    max_line = max(1, len(code.splitlines()))
    events: list[dict] = []
    discarded_events = 0
    multithreaded = False
    end_seen = False
    events_after_end = 0
    end_emitted: int | None = None
    end_step: int | None = None
    expected_step = 1

    text = data.decode("utf-8", errors="replace")
    # The LAST line may be a partial write (a hard kill mid-flush, or simply
    # where this bounded read happened to stop) — `splitlines()` on
    # `errors="replace"`-decoded bytes cannot tell "clean EOF" from "cut
    # short" either way, so a trailing line with no `\n` in the raw bytes is
    # always dropped rather than risk decoding a truncated JSON object into
    # something that HAPPENS to parse.
    lines = text.split("\n")
    if not data.endswith(b"\n"):
        lines = lines[:-1]

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if end_seen:
            events_after_end += 1
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            discarded_events += 1
            continue
        if not isinstance(obj, dict):
            discarded_events += 1
            continue
        if obj.get(_TRUNCATION_MARKER_KEY):
            if not truncated:
                reason = obj.get("reason")
                if reason in ("max_events", "max_trace_bytes"):
                    truncated = True
                    truncated_reason = reason
            continue
        if obj.get(_THREAD_MARKER_KEY):
            multithreaded = True
            continue
        if obj.get("event") == "end":
            if (set(obj) == {"event", "step", "emitted"}
                    and isinstance(obj.get("step"), int)
                    and not isinstance(obj.get("step"), bool)
                    and isinstance(obj.get("emitted"), int)
                    and not isinstance(obj.get("emitted"), bool)):
                end_seen = True
                end_step = obj["step"]
                end_emitted = obj["emitted"]
            else:
                discarded_events += 1
            continue
        clean = _validate_event(obj, expected_step, max_line)
        if clean is None:
            discarded_events += 1
            continue
        events.append(clean)
        expected_step += 1
        if len(events) >= max_events:
            if not truncated:
                truncated = True
                truncated_reason = "max_events"
            break

    events_consistent = (
        end_seen and events_after_end == 0
        and end_emitted == len(events) and end_step == len(events)
    )
    return {
        "events": events, "truncated": truncated,
        "truncated_reason": truncated_reason,
        "discarded_events": discarded_events,
        "events_consistent": events_consistent,
        "multithreaded": multithreaded,
    }


def _branch_report(source: str, events: list[dict]) -> dict[str, Any]:
    """`branches`, `lines_executed`, `lines_never_executed` — see the module
    docstring. Falls back to an empty analysis on a `SyntaxError`: a program
    that never compiled executed nothing, so there is nothing to report
    rather than nothing to compute.
    """
    try:
        branch_lines, executable_lines = _analyze_source(source)
    except SyntaxError:
        branch_lines, executable_lines = set(), set()
    line_hits = Counter(e["line"] for e in events
                        if e.get("event") == "line" and isinstance(e.get("line"), int))
    lines_executed = sorted(line_hits)
    branches = {str(line): line_hits.get(line, 0) for line in sorted(branch_lines)}
    lines_never_executed = sorted(executable_lines - set(lines_executed))
    return {
        "branches": branches,
        "lines_executed": lines_executed,
        "lines_never_executed": lines_never_executed,
    }


def execute_trace(language: str, code: str, stdin: str = "", timeout: int = 30,
                  max_events: int = 2000, max_memory_mb: int = 0,
                  max_output_kb: int = 0, max_cpu: int = 0,
                  no_net: bool = False, provider: str | None = None) -> dict[str, Any]:
    """Run `code` through the SAME executor path `execute_code` uses, wrapped
    in a tracing harness, and merge the trace into the standard envelope.
    See the module docstring for the mechanism end to end.
    """
    refusal = _refuse_unsupported_provider(provider)
    if refusal is not None:
        return refusal
    refusal = _refuse_unsupported_language(language)
    if refusal is not None:
        return refusal

    max_events = max(1, min(int(max_events), 200_000))

    workdir = Path(tempfile.mkdtemp(prefix="codecalc-trace-"))
    # Recorded before anything is written into it — the executed program has
    # this directory as its cwd and could rename another one into its place
    # before this module's own cleanup runs. Same reasoning as
    # `executor.execute_stream`'s `created_identity`.
    created_identity = executor._dir_identity(workdir)
    try:
        user_source_path = workdir / _USER_SOURCE_FILENAME
        # `executor._write_scratch_nofollow` (O_EXCL, no-follow-symlink) is
        # safe here for the same reason it is safe inside a just-reset
        # `.codecalc-run/`: `workdir` was just created by `mkdtemp`, fresh and
        # exclusive to this call, so nothing legitimate can already be at
        # this path.
        executor._write_scratch_nofollow(user_source_path, code)

        trace_path = workdir / registry.RUN_SCRATCH_DIRNAME / _TRACE_FILENAME
        wrapper_source = _build_wrapper_source(
            str(user_source_path), str(trace_path), max_events, _MAX_TRACE_BYTES)

        result = executor.execute(
            SUPPORTED_LANGUAGE, wrapper_source, stdin=stdin, timeout=timeout,
            workdir=str(workdir), max_memory_mb=max_memory_mb,
            max_output_kb=max_output_kb, max_cpu=max_cpu, no_net=no_net,
        )

        parsed = _parse_trace_file(trace_path, code, max_events)
        events = parsed["events"]
        result["events"] = events
        result["event_count"] = len(events)
        # Always equal to `event_count` by construction: every event that
        # reaches `events` was already recorded, and nothing is recorded
        # after tracing stops (see `_stop_tracing`). Kept as its own key
        # because the tool's documented shape names it separately from the
        # total — see server.py's `trace_execution` docstring.
        result["steps_before_truncation"] = len(events)
        result["truncated"] = parsed["truncated"]
        if parsed["truncated_reason"] is not None:
            result["truncated_reason"] = parsed["truncated_reason"]
        # See the module docstring's "TRUST BOUNDARY" section: neither of
        # these can be forged away by a sandboxed program the way the
        # events themselves partially can, because both are computed BY
        # this (unsandboxed) parser rather than trusted from the file.
        result["discarded_events"] = parsed["discarded_events"]
        result["events_consistent"] = parsed["events_consistent"]
        result.update(_branch_report(code, events))

        unenforced = result.get("unenforced")
        if not isinstance(unenforced, list):
            unenforced = []
            result["unenforced"] = unenforced
        if parsed["multithreaded"] and THREADS_UNENFORCED_NOTE not in unenforced:
            unenforced.append(THREADS_UNENFORCED_NOTE)
        # See the module docstring's "FALLBACK-BACKEND OLE RACE" section —
        # fallback-only and OLE-only, because that is exactly where the
        # race is confirmed to live; the Rust backend's own cap enforcement
        # is not timing-sensitive this way.
        if (result.get("backend") == "python" and result.get("verdict") == "OLE"
                and FALLBACK_OLE_EXIT_CODE_UNENFORCED_NOTE not in unenforced):
            unenforced.append(FALLBACK_OLE_EXIT_CODE_UNENFORCED_NOTE)
        return result
    finally:
        executor._rmtree_checked(workdir, created_identity)
