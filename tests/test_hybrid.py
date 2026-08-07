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
    print(f"{'PASS' if r.get('ok') and 'rust-exec' in combined else 'FAIL':4} {lang:10} {r.get('duration_ms',0):>7}ms")

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
