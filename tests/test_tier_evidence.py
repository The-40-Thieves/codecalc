"""Execution evidence for the compiled `tested`-tier languages.

`tier: "tested"` in codecalc/registry.py is a claim about codecalc's OWN CI:
a job genuinely EXECUTES the language and asserts on its real output, on
every PR. python3 and node earn it from the stateful-worker sweep
(tests/test_python_sweep.py). The languages here have no worker bootstrap,
so their evidence is this file: compile and run a real program through
executor.execute() — the same path execute_code serves — and assert the
exact computed stdout, not merely exit 0.

scripts/check_runtime_tiers.py reads TIER_EVIDENCE_LANGS below out of this
file's literal source and requires the registry's `tested` set to match it
(unioned with the worker langs) EXACTLY, and also requires ci-python.yml to
both invoke this file and set CODECALC_REQUIRE_TIER_EVIDENCE on the leg
that runs it. Under that flag a missing toolchain FAILS instead of
skipping: a silently-skipping evidence lane is the same defect class as a
scan that never asserts it scanned something — the tier claim would stand
on nothing.

A toolchain that RESOLVES but cannot run the program is always a FAIL,
never a skip, on every platform. That distinction is this tier system's
founding case: a review's smoke test found the rust host toolchain broken
on a machine where `rustc` resolved cleanly.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import executor, registry

FAILS: list[str] = []
REQUIRE = os.environ.get("CODECALC_REQUIRE_TIER_EVIDENCE") == "1"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    if REQUIRE:
        check(name, False, f"-> SKIP promoted to FAIL by CODECALC_REQUIRE_TIER_EVIDENCE ({why})")
    else:
        print(f"SKIP {name} ({why})")


#: The languages whose `tested` tier this file is the evidence for. A literal
#: tuple, because check_runtime_tiers.py regex-reads it from this file's
#: source — editing it here without flipping the registry tier (or vice versa)
#: fails that gate.
TIER_EVIDENCE_LANGS = ("rust", "go")

#: Programs that COMPUTE their output (10! = 3628800), so a cached or echoed
#: string cannot satisfy the assertion; each also prints a marker naming the
#: language so a plan mix-up cannot pass one language's check with another's
#: output.
PROGRAMS = {
    "rust": (
        "fn main() {\n"
        "    let v: u64 = (1..=10u64).product();\n"
        '    println!("tier-evidence rust {}", v);\n'
        "}\n"
    ),
    "go": (
        "package main\n\n"
        'import "fmt"\n\n'
        "func main() {\n"
        "    v := 1\n"
        "    for i := 1; i <= 10; i++ {\n"
        "        v *= i\n"
        "    }\n"
        '    fmt.Println("tier-evidence go", v)\n'
        "}\n"
    ),
}

# ═══ 0. floor: the tuple names real registry languages and nothing is ═══════
#        missing a program — otherwise the loop below could quietly cover
#        fewer languages than the gate believes this file exercises.
check("TIER_EVIDENCE_LANGS is non-empty and every entry is in the registry",
      bool(TIER_EVIDENCE_LANGS)
      and all(lang in registry.LANGUAGES for lang in TIER_EVIDENCE_LANGS),
      f"-> {TIER_EVIDENCE_LANGS}")
check("every TIER_EVIDENCE_LANGS entry has a program fixture",
      all(lang in PROGRAMS for lang in TIER_EVIDENCE_LANGS),
      f"-> fixtures for {sorted(PROGRAMS)}")

# ═══ 1. compile + run each language and assert its computed stdout ══════════
for lang in TIER_EVIDENCE_LANGS:
    entry = registry.LANGUAGES[lang]
    # The command whose PRESENCE gates skip-vs-run: the first token of the
    # compile step when there is one, else of the run step — taken from the
    # registry plan itself so this probe cannot drift from what execute()
    # actually invokes.
    tool = (entry["compile"] or entry["run"])[0]
    if not shutil.which(tool, path=registry.runtime_path()):
        skip(f"{lang}: execute a real program", f"`{tool}` not on the runtime PATH")
        continue

    r = executor.execute(lang, PROGRAMS[lang], timeout=120)
    expected = f"tier-evidence {lang} 3628800"
    check(f"{lang}: execute() succeeds (backend={r.get('backend')})",
          r.get("ok") is True,
          f"-> phase={r.get('phase')!r} exit={r.get('exit_code')!r} "
          f"stderr={str(r.get('stderr'))[:120]!r}")
    check(f"{lang}: stdout carries the computed marker line",
          expected in (r.get("stdout") or ""),
          f"-> expected {expected!r} in {str(r.get('stdout'))[:120]!r}")

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== TIER EVIDENCE HOLDS ===")
if FAILS:
    sys.exit(1)
