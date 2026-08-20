"""Smoke test: run every language, exercise logic + complexity layers."""
import json
import sys

from codecalc import complexity, executor, logic

PROGRAMS = {
    "python3": 'print("hello from python", 6*7)',
    "node": 'console.log("hello from node", 6*7)',
    "bun": 'console.log("hello from bun", 6*7)',
    "deno": 'console.log("hello from deno", 6*7)',
    "typescript": 'const x: number = 42; console.log("hello from ts", x)',
    "ruby": 'puts "hello from ruby #{6*7}"',
    "php": '<?php echo "hello from php ", 6*7, "\\n";',
    "perl": 'print "hello from perl ", 6*7, "\\n";',
    "lua": 'print("hello from lua", 6*7)',
    "tcl": 'puts "hello from tcl [expr {6*7}]"',
    "r": 'cat("hello from r", 6*7, "\\n")',
    "elixir": 'IO.puts("hello from elixir #{6*7}")',
    "erlang": '#!/usr/bin/env escript\nmain(_) -> io:format("hello from erlang ~p~n", [6*7]).',
    "bash": 'echo "hello from bash $((6*7))"',
    "zsh": 'echo "hello from zsh $((6*7))"',
    "go": 'package main\nimport "fmt"\nfunc main() { fmt.Println("hello from go", 6*7) }',
    "rust": 'fn main() { println!("hello from rust {}", 6*7); }',
    "c": '#include <stdio.h>\nint main(){ printf("hello from c %d\\n", 6*7); return 0; }',
    "cpp": '#include <iostream>\nint main(){ std::cout << "hello from cpp " << 6*7 << std::endl; }',
    "fortran": 'program hello\nprint *, "hello from fortran", 6*7\nend program hello',
    "zig": 'const std = @import("std");\npub fn main() !void { std.debug.print("hello from zig {d}\\n", .{6*7}); }',
    "java": 'class Main {\n  public static void main(String[] a) { System.out.println("hello from java " + 6*7); }\n}',
    "kotlin": 'fun main() { println("hello from kotlin ${6*7}") }',
    "swift": 'print("hello from swift", 6*7)',
    "mojo": 'fn main():\n    print("hello from mojo", 6*7)',
    "sqlite": 'SELECT "hello from sqlite", 6*7;',
    "jq": '"hello from jq", 1+1',
    "awk": 'BEGIN { print "hello from awk", 6*7 }',
    "haskell": 'main = putStrLn ("hello from haskell " ++ show (6*7))',
    "csharp": 'Console.WriteLine($"hello from csharp {6*7}");',
    "gleam": 'import gleam/io\npub fn main() { io.println("hello from gleam") }',
}

def _missing_runtime(r: dict) -> bool:
    """True when this result means the interpreter/compiler for this language
    could not be spawned on this machine at all — not that the sandboxed
    program ran and misbehaved. That is a fact about the machine, and must
    read as SKIP, never as a false FAIL.

    Detects three spawn-failure shapes: the pure-Python fallback's
    `_runtime_unavailable_result` (exit_code None, stderr says "runtime
    unavailable"), the Rust executor's spawn failure (exit_code -2, stderr
    says "spawn failed"), and both backends' structural refusal for a
    SHELL_WRAPPED language (gleam, haskell) on Windows — `plan_supported()`
    returns False there regardless of what is installed, and that refusal
    carries no `exit_code`/`stderr` at all, only an `error` saying
    "unsupported on this platform". Missing that shape is exactly what let a
    platform-unsupported language read as a smoke-suite FAIL. A genuine
    wrong-output failure always carries a real exit_code from the
    interpreter/compiler that DID run, never one of these sentinels.
    """
    if r.get("ok") or r.get("timed_out"):
        return False
    text = (r.get("stderr") or "") + (r.get("error") or "")
    return r.get("exit_code") in (None, -2) and (
        "runtime unavailable" in text
        or "spawn failed" in text
        or "unsupported on this platform" in text)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")


def main():
    passed, failed, skipped = [], [], []
    for lang, code in PROGRAMS.items():
        r = executor.execute(lang, code, timeout=60)
        combined = r.get("stdout", "") + r.get("stderr", "")
        ok = r.get("ok") and "hello from" in combined
        if not ok and _missing_runtime(r):
            skipped.append((lang, r))
            skip(f"{lang} smoke",
                 (r.get("stderr") or r.get("error") or "")[:160].replace("\n", " "))
            continue
        (passed if ok else failed).append((lang, r))
        status = "PASS" if ok else "FAIL"
        err = (r.get("stderr") or r.get("error") or "")[:120].replace("\n", " ")
        print(f"{status:4} {lang:12} exit={r.get('exit_code')} {r.get('duration_ms',0):>6}ms {err}")

    print("\n=== logic layer ===")
    print("eval  :", json.dumps(logic.evaluate_expression("sqrt(144) + 2**10")))
    print("trtbl :", json.dumps(logic.truth_table("a and b or not c"))[:220])
    print("z3    :", json.dumps(logic.z3_check("(declare-const x Int)(assert (> x 5))(assert (< x 10))(check-sat)"))[:220])
    print("solve :", json.dumps(logic.solve_linear("x + y = 10; x - y = 2", "x, y")))

    print("\n=== complexity layer ===")
    for code, lang in [
        ('for i in range(n):\n    print(i)', "python3"),
        ('for i in range(n):\n    for j in range(n):\n        print(i*j)', "python3"),
        ('for i in range(n):\n    sorted(arr)', "python3"),
        ('def f(n):\n    if n <= 1: return 1\n    return f(n-1) + f(n-2)', "python3"),
    ]:
        print(json.dumps(complexity.analyze(code, lang)))

    print(f"\n=== RESULT: {len(passed)} passed, {len(failed)} failed, "
          f"{len(skipped)} skipped ===")
    if failed:
        for lang, r in failed:
            print(f"  {lang}: stdout={r.get('stdout','')[:100]!r} stderr={r.get('stderr','')[:200]!r}")
        sys.exit(1)

if __name__ == "__main__":
    main()
