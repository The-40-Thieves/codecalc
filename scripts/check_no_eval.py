#!/usr/bin/env python3
"""The zero-eval invariant — codecalc's single most load-bearing security claim.

AUDIT.md CRITICAL-01: `truth_table()` evaluated user boolean expressions with
`eval(expr, {"__builtins__": {}}, env)`. Restricted-globals eval is not a
sandbox — `().__class__.__base__.__subclasses__()` walks the live class tree to
`subprocess.Popen`, giving arbitrary command execution in the MCP server
process, which is the process holding every API key. It was replaced with a
recursive-descent parser.

This script is the gate that keeps it gone. It exists SEPARATELY from
tests/test_security.py (which also checks this) because that suite needs a
working sandbox, 31 runtimes and a built Rust binary, so it cannot run on a bare
checkout — and a security invariant that only holds on one machine is not an
invariant. This runs on any Python, in milliseconds, with no dependencies.

It is also stricter than the `ruff` S102/S307 rules, which can only be turned on
or off file-by-file. The real policy is narrower than that: exec is allowed in
exactly one file, and eval nowhere at all.

  codecalc/_worker_bootstrap.py — exec() runs caller-supplied code inside the
  stateful REPL *subprocess*: separate process, env allowlist, own process
  group. Same threat model as the Rust executor. It never runs in the server
  process. AUDIT.md feature-expansion item 1.

FLOOR: asserts it parsed a plausible number of files and that the allowed
exception file actually exists. `rglob` over a moved package would otherwise
walk zero files and report a clean tree.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "codecalc"

#: exec() permitted here ONLY, for the reason documented above.
EXEC_ALLOWED = {"_worker_bootstrap.py"}
#: eval() is permitted nowhere.
EVAL_ALLOWED: set[str] = set()

MIN_FILES = 10  # the package has ~17 modules; anything less means a broken walk

findings: list[str] = []
scanned = 0

for path in sorted(PKG.rglob("*.py")):
    scanned += 1
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # bare eval(...) / exec(...)
        if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec"):
            allowed = EXEC_ALLOWED if node.func.id == "exec" else EVAL_ALLOWED
            if path.name not in allowed:
                findings.append(f"{path.relative_to(REPO)}:{node.lineno}: {node.func.id}()")
        # builtins.eval(...) / os.system(...) / subprocess(..., shell=True)
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in ("eval", "exec") and path.name not in EXEC_ALLOWED:
                findings.append(f"{path.relative_to(REPO)}:{node.lineno}: .{node.func.attr}()")
            if node.func.attr in ("system", "popen"):
                findings.append(f"{path.relative_to(REPO)}:{node.lineno}: os.{node.func.attr}()")
        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                findings.append(f"{path.relative_to(REPO)}:{node.lineno}: shell=True")

# ── floors ──────────────────────────────────────────────────────────────────
if scanned < MIN_FILES:
    print(f"::error::scanned only {scanned} file(s) under {PKG} — expected >= {MIN_FILES}. "
          "The gate is scanning nothing; an empty walk is not a clean tree.")
    sys.exit(1)

# ── the OTHER evaluator: SymPy ─────────────────────────────────────────────
# This gate scanned for literal eval/exec in codecalc's own source and found
# none, correctly, while six @mcp.tool() functions handed caller strings to
# sympify/parse_expr — which evaluate what they parse. `parse_expr` populates
# its default global_dict from vars(builtins) deliberately, __import__ among
# them, so `simplify_expression("__import__('os').system('id')")` ran id.
#
# A gate that only knows the names of the evaluators IT expects cannot see a
# third-party parser that evaluates internally. So this asserts the screen
# instead: any module reaching a SymPy parser must import reject_unsafe.
#
# File-scoped on purpose. Proving each individual call site sits behind a
# guard needs flow analysis, and a check that claims more precision than it
# has is the failure this file exists to prevent. The behavioural regression
# in tests/test_bug_sweep.py probes all six tools with a live payload and
# asserts nothing executes; this is the cheap structural half that runs on a
# bare checkout.
SYMPY_EVALUATORS = ("sympify(", "parse_expr(")
sympy_sites: list[tuple[str, int]] = []
for path in sorted((REPO / "codecalc").glob("*.py")):
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if any(k in stripped for k in SYMPY_EVALUATORS):
            sympy_sites.append((path.name, lineno))

# FLOOR. Ten call sites are known to exist. If this finds far fewer the
# extractor has broken, and "no unguarded sites" would be true of nothing.
if len(sympy_sites) < 8:
    print(f"::error::found only {len(sympy_sites)} sympy parser call sites; the "
          "extractor is broken and this check would pass vacuously")
    sys.exit(1)
else:
    unguarded = sorted({
        name for name, _ in sympy_sites
        if "reject_unsafe" not in (REPO / "codecalc" / name).read_text(encoding="utf-8")
    })
    if unguarded:
        print(f"::error::module(s) {unguarded} reach a SymPy parser with no "
              "reject_unsafe screen; SymPy evaluates what it parses")
        sys.exit(1)
    else:
        print(f"ok   sympy screen: {len(sympy_sites)} parser call sites, every "
              "module importing reject_unsafe")

for name in EXEC_ALLOWED:
    if not (PKG / name).is_file():
        print(f"::error::the documented exec() exception '{name}' no longer exists. "
              "Either it moved (update EXEC_ALLOWED) or the exception is stale — "
              "an allow-list entry for a missing file silently widens the policy.")
        sys.exit(1)

if findings:
    print(f"::error::{len(findings)} forbidden dynamic-execution construct(s):")
    for f in findings:
        print(f"  {f}")
    sys.exit(1)

print(f"ok   {scanned} files scanned; zero eval/exec/os.system/shell=True "
      f"outside {sorted(EXEC_ALLOWED)}")
