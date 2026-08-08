"""Stateful python3 REPL worker: JSON-lines request/response over a PRIVATE fd.

Two things here are deliberate and were both bugs before.

**The protocol does not share a file descriptor with the executed code.** The
first version swapped `sys.stdout` for a StringIO and wrote responses with
`print()`. That is a Python-level rebind: a subprocess inherits **fd 1** and
writes straight past it. So an entirely ordinary session line —

    subprocess.run(["echo", "hi"])

— put raw bytes into the response stream. At startup fd 1 is duplicated to a
private descriptor that only this module writes to, and fd 1/2 are then pointed
at capture files. Nothing the executed code can do reaches the protocol channel,
and as a bonus subprocess output is now CAPTURED rather than lost.

**Every response carries the id of the request it answers.** Corruption used to
be unrecoverable *and silent*: the reader consumed the junk, left the real reply
queued, and every later call returned the PREVIOUS call's result with
`ok: True`. Measured — a call asking for `marker-4` came back with the output of
the call before it. An echoed id makes a desync detectable instead of trusted;
see `Worker.run`, which kills the worker rather than return a stale answer.

Output is capped here too. The sandboxed path caps at 64 KiB and reports OLE;
this path returned a 20 MB string whole, built three times over in memory on the
way out through JSON.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import traceback

#: Matches MAX_OUTPUT_BYTES in the Rust executor. A session is not exempt from
#: the output cap just because it is stateful.
MAX_OUTPUT_BYTES = 64 * 1024

# The protocol channel: a dup of the ORIGINAL stdout, before anything is
# redirected. Executed code cannot name this descriptor, and closing fd 1 (which
# code is free to do) does not touch it.
_proto_fd = os.dup(1)
_proto = os.fdopen(_proto_fd, "w", encoding="utf-8", newline="\n")

# Requests still arrive on stdin. The iterator below binds to this object once,
# so rebinding sys.stdin per call cannot disturb it.
_requests = sys.stdin

ns = {"__name__": "__main__", "__builtins__": __builtins__}


def _read_capped(fh) -> tuple[str, bool]:
    """Read a capture file, capped, reporting whether anything was dropped."""
    fh.seek(0)
    data = fh.read(MAX_OUTPUT_BYTES + 1)
    truncated = len(data) > MAX_OUTPUT_BYTES
    if truncated:
        data = data[:MAX_OUTPUT_BYTES]
    return data.decode("utf-8", errors="replace") + ("\n...[truncated]" if truncated else ""), truncated


def _respond(payload: dict) -> None:
    _proto.write(json.dumps(payload) + "\n")
    _proto.flush()


for _line in _requests:
    _line = _line.strip()
    if not _line:
        continue
    try:
        req = json.loads(_line)
    except Exception:
        continue
    code = req.get("code", "")
    stdin_data = req.get("stdin", "")
    req_id = req.get("id")

    # Not a `with` block: these files have to outlive the try/finally that
    # restores fds 1 and 2, and they are read after the restore. Closed
    # explicitly below.
    out_f = tempfile.TemporaryFile()  # noqa: SIM115
    err_f = tempfile.TemporaryFile()  # noqa: SIM115
    # Save fds 1/2 so they can be restored even if the executed code closes them.
    saved_out, saved_err = os.dup(1), os.dup(2)
    prev_stdin, prev_stdout, prev_stderr = sys.stdin, sys.stdout, sys.stderr
    try:
        os.dup2(out_f.fileno(), 1)
        os.dup2(err_f.fileno(), 2)
        # Python-level streams over the SAME descriptors, so interpreter output
        # and subprocess output interleave in the order they were written.
        sys.stdout = io.TextIOWrapper(os.fdopen(os.dup(1), "wb"), encoding="utf-8",
                                      errors="replace", write_through=True)
        sys.stderr = io.TextIOWrapper(os.fdopen(os.dup(2), "wb"), encoding="utf-8",
                                      errors="replace", write_through=True)
        sys.stdin = io.StringIO(stdin_data)
        with contextlib.redirect_stdout(sys.stdout), contextlib.redirect_stderr(sys.stderr):
            exec(compile(code, "<session>", "exec"), ns)
        ok, err = True, ""
    except BaseException:
        # BaseException, not Exception: sys.exit() in a session line must end
        # that line, not the worker.
        ok, err = False, traceback.format_exc()
    finally:
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.flush()
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
        sys.stdin, sys.stdout, sys.stderr = prev_stdin, prev_stdout, prev_stderr

    stdout_s, out_trunc = _read_capped(out_f)
    stderr_s, err_trunc = _read_capped(err_f)
    out_f.close()
    err_f.close()

    _respond({
        "id": req_id,
        "ok": ok,
        "stdout": stdout_s,
        "stderr": stderr_s or err,
        "exit_code": 0 if ok else 1,
        "output_truncated": out_trunc or err_trunc,
        "verdict": "OLE" if (out_trunc or err_trunc) else ("OK" if ok else "RTE"),
    })
