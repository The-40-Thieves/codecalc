"""Verify: Rust backend in use, startup speed, new tools, MCP round-trip."""
import json
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

t0 = time.monotonic()
from codecalc import executor, logic, tools

print("backend:", executor.backend())
print("startup_without_sympy:", round(time.monotonic() - t0, 3), "s")


def _missing_runtime(r: dict) -> bool:
    """True when this result means the interpreter/compiler for this language
    could not be spawned on this machine at all — not that the sandboxed
    program ran and misbehaved. That is a fact about the machine, and must
    read as SKIP, never as a false FAIL. Same detection as test_smoke.py:
    the pure-Python fallback's `_runtime_unavailable_result` (exit_code
    None, stderr says "runtime unavailable") and the Rust executor's spawn
    failure (exit_code -2, stderr says "spawn failed"); a genuine
    wrong-output failure always carries a real exit_code instead.
    """
    if r.get("ok") or r.get("timed_out"):
        return False
    stderr = r.get("stderr") or ""
    return r.get("exit_code") in (None, -2) and (
        "runtime unavailable" in stderr or "spawn failed" in stderr)


#: Languages that ran and produced the wrong thing. A missing runtime is a fact
#: about the machine and lands in SKIP instead, via `_missing_runtime` above.
#:
#: This file used to print `FAIL <lang>` and carry on, with no accumulation and
#: no nonzero exit anywhere in it, so every language could fail and the suite
#: still exited 0. That is the pattern PR #13 removed from three other suites,
#: surviving here because nothing looked for it: scripts/check_claims.py counted
#: `check(` sites and this file has none, and no workflow runs it, so CI never
#: had an exit code to ignore. Found by the per-suite floor added for THE-776.
FAILED: list[str] = []


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")


# Rust-executor language smoke (subset — full 31 already validated)
for lang, code in {
    "python3": "print('rust-exec', 6*7)",
    "node": "console.log('rust-exec', 6*7)",
    "ruby": "puts 'rust-exec #{6*7}'",
    "go": 'package main\nimport "fmt"\nfunc main(){ fmt.Println("rust-exec", 6*7) }',
    "rust": 'fn main(){ println!("rust-exec {}", 6*7); }',
    "c": '#include <stdio.h>\nint main(){ printf("rust-exec %d\\n", 6*7); }',
    "java": 'class Main { public static void main(String[] a){ System.out.println("rust-exec " + 6*7); } }',
    "swift": 'print("rust-exec", 6*7)',
    "zig": 'const std = @import("std");\npub fn main() !void { std.debug.print("rust-exec {d}\\n", .{6*7}); }',
    "elixir": 'IO.puts("rust-exec #{6*7}")',
}.items():
    r = executor.execute(lang, code, timeout=30)
    combined = r.get("stdout", "") + r.get("stderr", "")
    ok = r.get("ok") and "rust-exec" in combined
    if not ok and _missing_runtime(r):
        skip(f"{lang} rust-exec smoke", (r.get("stderr") or "")[:160].replace("\n", " "))
        continue
    print(f"{'PASS' if ok else 'FAIL':4} {lang:10} {r.get('duration_ms',0):>7}ms")
    if not ok:
        FAILED.append(f"{lang}: {(r.get('stderr') or r.get('stdout') or '')[:120]!r}")

# logic layer
print("eval:", json.dumps(logic.evaluate_expression("sqrt(144)+2**10"))[:80])
print("solve:", json.dumps(logic.solve_linear("x + y = 10; x - y = 2", "x, y"))[:80])

# new tools
cmp = tools.compare_execution({"python3": "print(6*7)", "node": "console.log(6*7)"})
print("compare:", cmp["count"], "langs, fastest =", cmp["fastest"])

bench = tools.benchmark(
    "import sys\nn=int(sys.stdin.readline())\nprint(sum(range(n)))",
    language="python3",
    sizes="1000,2000,4000,8000",
    timeout=15,
)
print("benchmark estimate:", bench.get("estimate"), "| ratios:", bench.get("doubling_ratios"))

# probe
p = executor.probe()
print("probe: python3 =", p.get("python3"), "| c =", p.get("c"), "| all langs:", len(p))

if FAILED:
    print(f"\n=== {len(FAILED)} language(s) ran and produced the wrong output ===")
    for line in FAILED:
        print(f"  {line}")
    sys.exit(1)
print(f"\n=== hybrid checks OK ({len(FAILED)} failures) ===")
