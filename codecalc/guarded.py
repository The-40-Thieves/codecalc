"""Run SymPy where it can be KILLED, rather than where it must be trusted.

`safe_expr` screens what reaches SymPy and bounds the shapes known to explode.
Both are denylists, and a denylist is a bet that the list is complete. SymPy's
own maintainers made and lost that bet: PR sympy/sympy#12524 added a `safe=`
flag built on an AST whitelist plus a name blacklist and was never merged,
abandoned since 2020, with the author calling it "security theater that leads
users into a false sense of security, because it can still be bypassed".

So the screen buys time and this buys a bound. The distinction that matters is
that a bound does not have to anticipate anything: whatever the expression
does, it does it in a process the parent can kill.

WHY FORK, AND NOT A FRESH INTERPRETER:
A spawned child re-imports SymPy, which is 437ms measured on this project and
the reason the import is already lazy — that would be paid on every symbolic
call. A forked child inherits the parent's already-imported SymPy through
copy-on-write and costs single-digit milliseconds. The work is CPU-bound and
short-lived, so there is nothing a fresh interpreter buys here except latency.

THE THREADING HAZARD, STATED PLAINLY:
`fork()` in a process with threads copies only the calling thread, so a lock
held by another thread at that instant is held forever in the child. Python
3.12 warns about exactly this. Two things bound the risk rather than removing
it: the fork is serialised under `_FORK_LOCK` so this module never races
itself, and the child touches only SymPy and `os.write` before `os._exit`,
taking no path through logging, subprocess or the import system. It is the same
trade `executor.py` already makes for `preexec_fn`, which is likewise not
thread-safe and likewise serialised. A child that does deadlock is killed by
the parent's wall clock like any other overrun, which is the property this
module exists to provide.

WINDOWS HAS NO FORK.
There is no equivalent that avoids the import cost, so the call runs in-process
and the result says so in `unenforced`, in the same vocabulary the executor
uses for a ceiling it could not apply. Reporting a bound that is not there
would be the failure this repository keeps correcting.
"""

from __future__ import annotations

import json
import os
import select
import signal
import threading
from time import monotonic as _monotonic
from time import sleep as _sleep

from . import errors

#: Serialises forks from this module. See the threading note above.
_FORK_LOCK = threading.Lock()

#: The child's own ceilings. CPU is the one that stops a runaway computation;
#: RLIMIT_AS stops the other way an expression gets out of hand, which is by
#: allocating rather than by spinning.
DEFAULT_CPU_SECONDS = 10
DEFAULT_MEMORY_MB = 2048

#: Wall clock, enforced by the PARENT. RLIMIT_CPU alone is not enough: a child
#: blocked rather than computing burns no CPU and would never hit it.
DEFAULT_WALL_SECONDS = 15

#: A result is a small dict of strings. The cap is here so a child that decides
#: to emit something enormous cannot make the parent hold it.
_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024

#: Reported when the platform cannot provide the bound, in the executor's own
#: style: a caller reading this knows the guarantee was not applied.
UNENFORCED_NO_FORK = "expression_bound_not_enforced_without_fork"

CAN_FORK = hasattr(os, "fork") and hasattr(os, "waitpid")

#: The only fields a failed guarded call passes on. Everything else the child
#: or the parent put in the outcome — `value` above all — stops here.
_CARRIED_ON_FAILURE = ("ok", "code", "error", "remedy", "code_inferred")

#: The out-of-memory answer, serialised AT IMPORT TIME.
#:
#: The OOM branch in `_child` cannot afford to build anything: under MemoryError
#: even formatting a message can raise again, which is why that branch answered
#: with a bare literal. The literal carried no `code`, so the one failure in the
#: taxonomy that is unambiguously `resource_exhausted` arrived at the server
#: boundary codeless and had its code guessed back out of the words "memory
#: ceiling" — the weak claim, for the case we are most certain about.
#:
#: Building the same JSON here costs nothing at failure time and keeps the
#: property that made the literal correct: by the time a child hits MemoryError,
#: this string has existed since import.
_OOM_PAYLOAD = json.dumps(errors.error_result(
    errors.RESOURCE_EXHAUSTED, "expression exhausted the memory ceiling"))


def _child(write_fd: int, fn, args: tuple, kwargs: dict,
           cpu_seconds: int, memory_mb: int) -> None:
    """Run `fn` under rlimits and write its result back as JSON. Never returns.

    Every exception is caught, including the ones nobody predicted. That is the
    point of running here rather than in the parent: `nextprime(100)` raised
    `AttributeError: 'int' object has no attribute 'is_number'` out of
    evaluate_expression, because some SymPy functions return plain Python
    objects rather than expressions. A guard that only caught the failures
    already known would not have contained that one.
    """
    import resource

    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        if memory_mb > 0:
            as_bytes = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    except (ValueError, OSError):
        # A ceiling that could not be set is reported by the parent as
        # unenforced rather than silently skipped — but only the wall clock is
        # truly load-bearing, and that one is the parent's.
        pass

    try:
        payload = json.dumps({"ok": True, "value": fn(*args, **kwargs)})
    except MemoryError:
        # Formatting the error can itself allocate, so this one is answered
        # with a constant — see _OOM_PAYLOAD, which is built at import time
        # precisely so that nothing is constructed here.
        payload = _OOM_PAYLOAD
    except BaseException as exc:  # containment is the job — every failure, known or not
        try:
            payload = json.dumps(errors.error_result(
                errors.classify(exc), f"{type(exc).__name__}: {exc}"[:2000]))
        except Exception:
            payload = '{"ok": false, "error": "evaluation failed"}'

    data = payload.encode("utf-8", "replace")[:_MAX_PAYLOAD_BYTES]
    try:
        os.write(write_fd, data)
    except OSError:
        pass
    # _exit, not sys.exit: no atexit handlers, no buffered flushes, nothing
    # inherited from the parent that could run twice.
    os._exit(0)


def run_guarded(fn, *args, cpu_seconds: int = DEFAULT_CPU_SECONDS,
                memory_mb: int = DEFAULT_MEMORY_MB,
                wall_seconds: int = DEFAULT_WALL_SECONDS,
                **kwargs) -> dict:
    """Run `fn(*args, **kwargs)` in a killable child; return a result dict.

    Returns `{"ok": True, "value": ...}` or `{"ok": False, "error": ...}`, plus
    `"unenforced"` when the platform could not apply the bound. `fn` must return
    something JSON-serialisable, which every tool result in this project is.
    """
    if not CAN_FORK:
        # In-process, and SAYING SO. The screen in safe_expr still applies; what
        # is missing is the ability to stop something it did not anticipate.
        try:
            return {"ok": True, "value": fn(*args, **kwargs),
                    "unenforced": [UNENFORCED_NO_FORK]}
        except BaseException as exc:  # containment is the job
            return errors.error_result(
                errors.classify(exc), f"{type(exc).__name__}: {exc}"[:2000],
                unenforced=[UNENFORCED_NO_FORK])

    read_fd, write_fd = os.pipe()
    with _FORK_LOCK:
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            _child(write_fd, fn, args, kwargs, cpu_seconds, memory_mb)
            # unreachable: _child always _exits

    os.close(write_fd)
    chunks: list[bytes] = []
    total = 0
    timed_out = False
    deadline = _monotonic() + wall_seconds
    try:
        while True:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([read_fd], [], [], remaining)
            if not ready:
                timed_out = True
                break
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break  # child closed the pipe: it is done
            total += len(chunk)
            if total > _MAX_PAYLOAD_BYTES:
                timed_out = False
                chunks.append(chunk)
                break
            chunks.append(chunk)
    finally:
        os.close(read_fd)
        if timed_out:
            # SIGKILL, not SIGTERM. The child may be inside a C loop in SymPy
            # with no opportunity to handle a catchable signal.
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _reap(pid)

    # The three failures below are the PARENT's own diagnosis, not the child's.
    # Each one knows exactly what happened, so each names its code here rather
    # than leaving `ensure_code` to guess it back out of the sentence. A code
    # chosen where the failure is detected is the strong claim; one recovered
    # from prose is the weak one, and these are not cases where we have to
    # settle for the weak one.
    if timed_out:
        return errors.error_result(
            errors.TIMEOUT,
            f"expression exceeded the {wall_seconds}s evaluation limit and was killed")
    raw = b"".join(chunks)
    if not raw:
        # The child died without answering: RLIMIT_CPU (SIGXCPU) and the kernel
        # OOM killer both land here. Distinguishable from a timeout, which is
        # the parent's doing, and worth saying so rather than reporting a
        # generic failure.
        return errors.error_result(
            errors.RESOURCE_EXHAUSTED,
            "expression was terminated by a resource limit "
            f"(CPU {cpu_seconds}s / memory {memory_mb}MB) before producing a result")
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return errors.error_result(
            errors.INTERNAL, "the guarded evaluation returned no usable result")


def _reap(pid: int) -> None:
    """Wait for `pid`, without letting a stuck child block the caller forever."""
    for _ in range(100):
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if done:
            return
        _sleep(0.01)
    # Still alive after a second: it ignored the kill or is unkillable. Reaping
    # is abandoned rather than blocking the server; the process is already
    # SIGKILLed and init will collect it.
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass


def guarded_call(fn, *args, **kwargs) -> dict:
    """Run a tool function under the bound and hand back its own result dict.

    Every symbolic tool in this project returns `{"ok": ..., ...}`, so the
    unwrapping is identical for all of them and lives here rather than being
    written out at each call site — six copies of this would be six chances to
    drop the `unenforced` passthrough, which is the part a caller cannot
    reconstruct.
    """
    outcome = run_guarded(fn, *args, **kwargs)
    if outcome.get("ok"):
        result = outcome["value"]
    else:
        # Carry the failure through whole. This used to rebuild a bare
        # `{"ok": False, "error": ...}`, which DISCARDED the `code` the child's
        # `errors.classify()` had chosen at the raise site — and `ensure_code`
        # at the server boundary then re-derived one from the message text and
        # stamped it `code_inferred: True`. Two things were wrong with that.
        #
        # It downgraded every guarded failure's provenance from "chosen where it
        # was raised" to "guessed from prose", which is the exact distinction
        # errors.py exists to preserve and which check_claims counts.
        #
        # And for one code in six it did not round-trip at all. Measured across
        # the taxonomy: FileNotFoundError classifies as `runtime_unavailable`
        # (the language is not installed — remedy: install it), while its
        # message "ghc" matches no hint and falls through to `internal` (remedy:
        # report a bug). A caller was told to file an issue about a missing
        # compiler. Wrong in the actionable direction, which is the failure mode
        # errors.py's own comments call out.
        # An explicit allowlist, not "everything except unenforced". The
        # blocklist form was the first fix and a cross-vendor review flagged it:
        # no current producer in run_guarded returns a `value` on the failure
        # path, so nothing leaks today, but the shape of the code invited one.
        # A future `{"ok": False, "value": ...}` — or a child payload shaped by
        # something other than error_result — would have been forwarded whole
        # into a tool result. Naming the four fields that belong to the error
        # contract costs nothing and cannot acquire a fifth by accident.
        result = {k: outcome[k] for k in _CARRIED_ON_FAILURE if k in outcome}
        result["ok"] = False
        result.setdefault("error", "evaluation failed")
    if outcome.get("unenforced"):
        # The platform could not apply the bound. Merged rather than replacing
        # any `unenforced` the tool itself reported.
        existing = list(result.get("unenforced") or [])
        result = {**result, "unenforced": existing + list(outcome["unenforced"])}
    return result
