"""Differential test: the tree-sitter analyser against the regex heuristic.

The point is not that the two agree — they must NOT agree, because the regex
version is wrong on cases a parser gets right. Each case below states the
correct answer and which implementation produces it, so "replace the analyser"
is backed by evidence rather than by the assertion that parsers are better.

The regex baseline is kept verbatim in `_legacy_*` so the comparison stays
honest as `complexity.py` changes.
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import parsing

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── the regex heuristic, exactly as complexity.py had it ────────────────────
_LEGACY_LOOP_RE = re.compile(
    r"\b(for|while|foreach|until|loop|repeat)\b"
    r"|\bfor\s*\("
    r"|\.forEach\(|\.map\(|\.filter\(|\.reduce\("
    r"|for\s+\w+\s+in\b"
    r"|for\s+\w+\s*:="
    r"|for\s+\w+\s+of\b"
)


def _legacy_loops(code: str) -> int:
    n = 0
    for raw in code.splitlines():
        if not raw.strip() or raw.strip().startswith(("#", "//", "/*", "*")):
            continue
        if _LEGACY_LOOP_RE.search(raw) and not raw.strip().startswith(("}", ")")):
            n += 1
    return n


def loops(code: str, lang: str = "python3") -> int:
    return parsing.analyse(code, lang).loops


# ── 1. a loop keyword inside a STRING is not a loop ─────────────────────────
CASE = 'msg = "for each item, while you wait"\nprint(msg)\n'
check("string containing 'for'/'while': parser says 0",
      loops(CASE) == 0, f"-> parser {loops(CASE)}, regex {_legacy_loops(CASE)}")
check("  ...and the regex over-counted it",
      _legacy_loops(CASE) > 0, f"-> regex {_legacy_loops(CASE)}")

# ── 2. a loop keyword inside a COMMENT is not a loop ────────────────────────
CASE = "x = 1  # for the record, while testing\ny = 2\n"
check("trailing comment containing 'for': parser says 0",
      loops(CASE) == 0, f"-> parser {loops(CASE)}, regex {_legacy_loops(CASE)}")
check("  ...and the regex over-counted it", _legacy_loops(CASE) > 0)

# ── 3. an identifier that merely CONTAINS a keyword ─────────────────────────
CASE = 'formatted = format(value)\nforest = 3\n'
check("identifiers 'format'/'forest' are not loops", loops(CASE) == 0,
      f"-> parser {loops(CASE)}, regex {_legacy_loops(CASE)}")

# ── 4. a real nested loop is still counted, and nesting depth is right ──────
CASE = "for i in range(n):\n    for j in range(n):\n        pass\n"
f = parsing.analyse(CASE, "python3")
check("real nested loops counted", f.loops == 2, f"-> {f.loops}")
check("nesting depth is 2", f.max_loop_depth == 2, f"-> {f.max_loop_depth}")

# ── 5. a chained pipeline is ONE expression, not three loops ────────────────
CASE = "const out = xs.map(f).filter(g).reduce(h);\n"
check("chained .map/.filter/.reduce is not 3 loops",
      loops(CASE, "node") == 0,
      f"-> parser {loops(CASE, 'node')}, regex {_legacy_loops(CASE)}")
check("  ...and the regex saw a loop there", _legacy_loops(CASE) > 0)

# ── 6. recursion: a real self-call ──────────────────────────────────────────
CASE = "def fact(n):\n    return 1 if n <= 1 else n * fact(n - 1)\n"
f = parsing.analyse(CASE, "python3")
check("direct recursion detected", "fact" in f.recursive_functions, f"-> {f.recursive_functions}")

# ── 7. recursion: a name that only appears in a DOCSTRING is not a call ─────
CASE = 'def fact(n):\n    """calls fact recursively (it does not)"""\n    return n\n'
f = parsing.analyse(CASE, "python3")
check("name in a docstring is not recursion", f.recursive_functions == [],
      f"-> {f.recursive_functions}")

# ── 8. recursion: a DIFFERENT function sharing a prefix ─────────────────────
CASE = "def fact(n):\n    return factorial_helper(n)\n\ndef factorial_helper(n):\n    return n\n"
f = parsing.analyse(CASE, "python3")
check("prefix-sharing callee is not recursion", "fact" not in f.recursive_functions,
      f"-> {f.recursive_functions}")

# ── 9. it works across languages, not just python ──────────────────────────
# EXACT counts, not `>=`. An earlier version of this block used `>=` and passed
# while the analyser reported 3 loops for a 2-loop Rust function — it was
# counting the bare `while` keyword token as well as the `while_expression` that
# contains it. A loose assertion hid an over-count, which is the same failure the
# regex heuristic had.
MULTI = {
    "node": ("for (let i=0;i<n;i++){ for (const x of xs) {} }", 2),
    "go": ("package main\nfunc main(){ for i:=0;i<3;i++ { for range xs {} } }", 2),
    "rust": ("fn main(){ for i in 0..3 { while true {} } }", 2),
    "c": ("int main(){ for(int i=0;i<3;i++){ while(1){} } return 0; }", 2),
}
for lang, (src, want) in MULTI.items():
    got = parsing.analyse(src, lang)
    check(f"{lang}: loops counted EXACTLY", got.loops == want,
          f"-> {got.loops} (want {want}), parsed={got.parsed}")

# ── 10. every registry language has a parser ────────────────────────────────
from codecalc import registry

supported = parsing.supported_languages()
missing = sorted(set(registry.LANGUAGES) - set(supported))
# Report WHY, not just which. `missing ['python3']` was the entire diagnosis
# available when grammars stopped loading on py3.14 in CI (THE-820), and it is
# indistinguishable from a language this pack genuinely does not ship.
#
# Distinct REASONS rather than one per language: a pack-wide failure produces
# the same error 32 times, and printing it 32 times buries the line that matters.
_reasons = sorted({
    (parsing.grammar_error(parsing.grammar_name(m)) or "no grammar in this pack")[:160]
    for m in missing
})
check("every registry language has a grammar", not missing,
      f"-> missing {len(missing)}: {missing[:6]}"
      + (f" — {len(_reasons)} distinct reason(s): {_reasons[:2]}" if _reasons else ""))

# ── 10b. FLOOR: is the parser alive at all? ─────────────────────────────────
#
# Half this file passes VACUOUSLY when tree-sitter is absent. Every case of the
# form "the regex over-counts here and the parser correctly says 0" is also
# satisfied by a parser that says 0 about everything, so a total outage presents
# as a partial pass — five scattered failures among a dozen PASSes, with nothing
# saying "the analyser is not running at all".
#
# That is exactly what THE-820 looked like in CI. This is the existence floor
# the gate scripts already carry, applied to the differential: if the parser
# cannot count the loops in a nested loop, nothing above measured a parser, and
# the file should SAY so rather than leave a reader to infer it from which
# assertions happened to fail.
_alive = parsing.analyse("for i in range(3):\n    for j in range(3):\n        pass\n", "python3")
check("FLOOR: the parser is actually running — everything above is vacuous if not",
      _alive.parsed and _alive.loops > 0,
      f"-> parsed={_alive.parsed} loops={_alive.loops} "
      f"reason={_alive.reason or '(none)'} "
      # The underlying exception, which `reason` summarises away. This is the
      # line THE-820 needed and did not have.
      f"| grammar_error={parsing.grammar_error('python') or '(none)'}")

# ── 11. an unknown language degrades honestly ───────────────────────────────
f = parsing.analyse("x", "notalanguage")
check("unknown language: parsed=False with a reason",
      f.parsed is False and bool(f.reason), f"-> {f.reason}")

# ── 12. broken source is reported, not crashed on ───────────────────────────
f = parsing.analyse("def f(:\n  ???", "python3")
check("syntactically broken source sets has_error", f.parsed and f.has_error,
      f"-> parsed={f.parsed} has_error={f.has_error}")

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== TREE-SITTER BEATS THE REGEX ON EVERY DIVERGENT CASE ===")
sys.exit(1 if FAILS else 0)
