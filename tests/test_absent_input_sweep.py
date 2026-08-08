"""Regressions for the absent-input sweep of 2026-08-08.

One shape, found four times across three modules: **an absent or invalid input
producing an affirmative answer.**

    z3_check("")                  -> {"ok": true, "result": "sat"}
    z3_check("(declare-const x Int)")  -> "sat", with no (check-sat) anywhere
    solve_linear(2-var, "x, y, z")     -> {x: 6, y: 4}, count 1, z never mentioned
    runtimes.update("notalanguage")    -> {"ok": true, "Dry run: nothing was updated"}
    runtimes.update("")                -> ok, total 0

Each is defensible in isolation — an unconstrained solver really is satisfiable,
sympy really cannot solve for a variable the system does not mention — and each
is useless to a caller, because the answer is indistinguishable from the same
call on real input. A generator that emitted nothing, a typo in a language name,
and a variable the model forgot to constrain all came back as success.

The counter-example that made these obvious: `truth_table("")` has always
returned a parse error. The same repository answered the same question two
different ways.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import logic, runtimes, units

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ═══ z3_check: an empty script is not a satisfiable one ════════════════════
for label, script in [("empty", ""), ("whitespace", "   \n\t "),
                      ("comment only", "; nothing here\n"),
                      ("comments and blanks", "; a\n\n; b\n")]:
    r = logic.z3_check(script)
    check(f"z3_check rejects a {label} script",
          r.get("ok") is False and "empty" in (r.get("error") or ""),
          f"-> ok={r.get('ok')} result={r.get('result')!r}")

r = logic.z3_check("(declare-const x Int)(assert (> x 5))")
check("z3_check rejects a script with no (check-sat)",
      r.get("ok") is False and "check-sat" in (r.get("error") or ""),
      f"-> ok={r.get('ok')} result={r.get('result')!r}")

# ...and still answers real questions.
r = logic.z3_check("(declare-const x Int)(assert (> x 5))(assert (< x 10))(check-sat)")
check("z3_check still solves a real system", r.get("ok") is True and r.get("result") == "sat")
check("  ...with a model inside the constraints",
      5 < int((r.get("model") or {}).get("x", 0)) < 10, f"-> {r.get('model')}")
r = logic.z3_check("(declare-const x Int)(assert (> x 5))(assert (< x 3))(check-sat)")
check("z3_check still reports unsat", r.get("result") == "unsat", f"-> {r.get('result')}")

# The comparison that exposed it: truth_table has always rejected empty input.
check("truth_table rejects an empty expression too (unchanged)",
      logic.truth_table("").get("ok") is False)

# ═══ solve_linear: a variable it did not solve for must be named ═══════════
r = logic.solve_linear("x + y = 10; x - y = 2", "x, y, z")
check("solve_linear names the variable it could not determine",
      r.get("unsolved") == ["z"], f"-> unsolved={r.get('unsolved')!r}")
check("  ...and explains what the solution does cover",
      "z" in (r.get("note") or ""), f"-> {r.get('note')!r}")
check("  ...while still returning the part it did solve",
      "x: 6" in str(r.get("solutions")) and "y: 4" in str(r.get("solutions")),
      f"-> {r.get('solutions')}")

# A fully-determined system gains no noise: `unsolved` must be absent, not [].
r = logic.solve_linear("x + y = 10; x - y = 2", "x, y")
check("a fully solved system reports no unsolved variables", "unsolved" not in r,
      f"-> {sorted(r)}")
check("  ...and is otherwise unchanged", r.get("count") == 1 and r.get("ok") is True)

# ═══ runtimes: a name nobody recognises is not a successful no-op ══════════
r = runtimes.update("notalanguage")
check("update rejects an unknown runtime name", r.get("ok") is False,
      f"-> ok={r.get('ok')} message={(r.get('message') or '')[:40]!r}")
check("  ...and names it", r.get("unknown") == ["notalanguage"], f"-> {r.get('unknown')}")
check("  ...and says nothing was done", "Nothing was checked" in (r.get("error") or ""),
      f"-> {(r.get('error') or '')[:70]}")

# A partial match is a success WITH a note, not a failure: the known name was
# genuinely processed.
r = runtimes.update("gradle,notalanguage")
check("a mix of known and unknown succeeds but reports the unknown",
      r.get("ok") is True and r.get("unknown") == ["notalanguage"],
      f"-> ok={r.get('ok')} unknown={r.get('unknown')}")
check("  ...and still processed the known one", r["summary"]["total"] >= 1,
      f"-> total={r['summary']['total']}")

# An empty filter means "no filter", like None — it used to match nothing and
# report success for having done nothing.
for label, arg in [("empty string", ""), ("whitespace", "   "), ("commas only", " , , ")]:
    r = runtimes.update(arg)
    check(f"an {label} filter means ALL runtimes, not none",
          r.get("ok") is True and r["summary"]["total"] > 1,
          f"-> total={r['summary']['total']}")
check("None still means all runtimes",
      runtimes.update()["summary"]["total"] > 1)

# Every dry run stays a dry run.
for arg in ("gradle", "", "notalanguage"):
    r = runtimes.update(arg)
    check(f"update({arg!r}) is still a dry run", r.get("dry_run") is True or r.get("ok") is False,
          f"-> dry_run={r.get('dry_run')} ok={r.get('ok')}")

# ═══ units: sympy is imported on FIRST USE, not at module load ═════════════
# The README credited lazy sympy/z3 imports for a fast start while units.py
# imported sympy at the top and server.py imports units — so the mechanism was
# documented and absent. Asserted by module state, not by timing, because a
# timing assertion fails on a busy machine for a reason that is not a defect.
# In a FRESH interpreter, because by this point in the file `logic` has already
# imported sympy lazily and an in-process check would be asserting test ordering
# rather than the property.
_probe = subprocess.run(
    [sys.executable, "-c", (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import codecalc.server\n"
        "print('sympy' in sys.modules, 'z3' in sys.modules)"
    )],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
# The default would be "False False" — which is the PASSING answer, so a probe
# that crashed would pass vacuously. Assert it ran before reading it.
check("the laziness probe actually ran",
      _probe.returncode == 0 and len((_probe.stdout or "").split()) == 2,
      f"-> rc={_probe.returncode} stdout={(_probe.stdout or '')[:40]!r} "
      f"stderr={(_probe.stderr or '')[:60]!r}")
_sympy_loaded, _z3_loaded = ((_probe.stdout or "").split() + ["?", "?"])[:2]
check("importing the SERVER does not import sympy", _sympy_loaded == "False",
      f"-> sympy in sys.modules = {_sympy_loaded} ({(_probe.stderr or '')[:50]})")
check("...nor z3 (which was always lazy)", _z3_loaded == "False",
      f"-> z3 in sys.modules = {_z3_loaded}")

units.convert(1, "km", "m")
check("...and the first real unit call loads sympy", units._SYMPY is not None)
check("...and conversion is still correct", units.convert(1, "km", "m")["value"] == 1000.0)

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== ABSENT INPUT NO LONGER RETURNS AN AFFIRMATIVE ANSWER ===")
sys.exit(1 if FAILS else 0)
