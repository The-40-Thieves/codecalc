import contextlib
import io
import json
import sys
import traceback

ns = {"__name__": "__main__", "__builtins__": __builtins__}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue
    code, stdin_data = req.get("code", ""), req.get("stdin", "")
    out_buf, err_buf = io.StringIO(), io.StringIO()
    prev_stdin, prev_out, prev_err = sys.stdin, sys.stdout, sys.stderr
    try:
        sys.stdin = io.StringIO(stdin_data)
        sys.stdout, sys.stderr = out_buf, err_buf
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            exec(compile(code, "<session>", "exec"), ns)
        ok, err = True, ""
    except BaseException:
        ok, err = False, traceback.format_exc()
    finally:
        sys.stdin, sys.stdout, sys.stderr = prev_stdin, prev_out, prev_err
    print(json.dumps({"ok": ok, "stdout": out_buf.getvalue(),
                      "stderr": err_buf.getvalue() or err,
                      "exit_code": 0 if ok else 1}), flush=True)
