"""Regression lock for scripts/skill_detect.py's highest-precision rules.

CONTRIBUTING.md: "A gate is not trusted until it has been watched failing."
Each assertion below was seeded against the fixture set in
tests/fixtures/skill_detect/ and watched pass on a correct detector — the
positive fixtures are chosen so the corresponding rule text (see that
module's RULES table) must match, and the negative fixtures are chosen so
the same rules must NOT match, which is the shape a false-positive-prone
rule would fail first.

Locked here (recall == 1.0 on their seeded positive, silence on the seeded
negatives): non_integer_arithmetic, equivalence_or_port_claim, big_o_claim.
These are the three the fixtures/labels.json ground truth covers, so
score() itself is asserted at precision == 1.0 / recall == 1.0 rather than
just the per-rule checks — a rule that matched something unlabeled would
show up there even if every individual `check()` below happened to pass.

speedup_claim and non_decimal_base_value get direct unit assertions (values,
not fixture/labels coverage) — see "Rules" in scripts/skill_detect.py's
module docstring for why the fixture set covers exactly three rules for now.

Standalone script, not pytest — see tests/conftest.py and CONTRIBUTING.md's
"Running the gates locally" for why: these suites assert at module scope and
sys.exit at the end, and pytest's import-based collection would abort on the
first one.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import skill_detect as sd

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "skill_detect"

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── seeded positives: every locked rule fires with the expected rule_id ────

labels = json.loads((FIXTURES / "labels.json").read_text(encoding="utf-8"))

float_misses = sd.detect_file(FIXTURES / "float_arith_miss.jsonl")
check("float_arith_miss.jsonl flags exactly the labeled (turn, rule_id)",
      any(m.rule_id == "non_integer_arithmetic" and m.location["turn"] == 1
          for m in float_misses),
      f"-> {[m.as_dict() for m in float_misses]}")
check("float_arith_miss.jsonl: the flagged claim IS the float arithmetic text",
      any(m.claim == "0.1 + 0.2 = 0.3" for m in float_misses),
      f"-> {[m.claim for m in float_misses]}")
check("float_arith_miss.jsonl: expected_tools names calc_exact",
      any(m.expected_tools == ["calc_exact"] for m in float_misses))

equiv_misses = sd.detect_file(FIXTURES / "equivalence_miss.jsonl")
check("equivalence_miss.jsonl flags equivalence_or_port_claim at turn 1",
      any(m.rule_id == "equivalence_or_port_claim" and m.location["turn"] == 1
          for m in equiv_misses),
      f"-> {[m.as_dict() for m in equiv_misses]}")
check("equivalence_miss.jsonl: expected_tools names verify_translation",
      any("verify_translation" in m.expected_tools for m in equiv_misses))

bigo_misses = sd.detect_file(FIXTURES / "bigo_miss.jsonl")
check("bigo_miss.jsonl flags big_o_claim at turn 1 with claim 'O(n^2)'",
      any(m.rule_id == "big_o_claim" and m.location["turn"] == 1 and m.claim == "O(n^2)"
          for m in bigo_misses),
      f"-> {[m.as_dict() for m in bigo_misses]}")

# ── the SAME float claim, but WITH the required tool call: not a miss ──────

hit_misses = sd.detect_file(FIXTURES / "float_arith_hit.jsonl")
check("float_arith_hit.jsonl (calc_exact called): zero misses", len(hit_misses) == 0,
      f"-> {[m.as_dict() for m in hit_misses]}")

# ── seeded negatives: conservatism — these must stay completely silent ─────

small_int_misses = sd.detect_file(FIXTURES / "small_int_no_miss.jsonl")
check("small_int_no_miss.jsonl ('2 + 3 + 4 = 9'): zero misses (SKILL.md's own "
      "'never wrong' example)", len(small_int_misses) == 0,
      f"-> {[m.as_dict() for m in small_int_misses]}")

estimate_misses = sd.detect_file(FIXTURES / "estimate_no_miss.jsonl")
check("estimate_no_miss.jsonl ('roughly 40% faster ... rough estimate'): zero misses",
      len(estimate_misses) == 0,
      f"-> {[m.as_dict() for m in estimate_misses]}")

# ── score() over the whole fixture folder: the aggregate the CLI reports ───

result = sd.score(FIXTURES)
check(f"score(): precision == 1.0 ({result['true_positives']} tp, "
      f"{result['false_positives']} fp)", result["precision"] == 1.0)
check(f"score(): recall == 1.0 ({result['true_positives']} tp, "
      f"{result['false_negatives']} fn)", result["recall"] == 1.0)
check("score(): exactly 3 true positives (one per locked rule)",
      result["true_positives"] == 3, f"-> {result['true_positives']}")
check("score(): zero false positives", result["false_positives"] == 0,
      f"-> {result['false_positives']}")
check("score(): zero false negatives", result["false_negatives"] == 0,
      f"-> {result['false_negatives']}")

# ── direct unit assertions: values, not shapes ──────────────────────────────

check("non_integer_arithmetic fires on '1.5 + 2.3' and returns the exact snippet",
      sd._rule_non_integer_arithmetic("The total is 1.5 + 2.3 dollars.")
      == ["1.5 + 2.3"])

check("non_integer_arithmetic does NOT fire on '2 + 3 + 4' (integer-only chain)",
      sd._rule_non_integer_arithmetic("2 + 3 + 4 = 9") == [])

check("non_integer_arithmetic does NOT fire on an estimate-guarded float claim",
      sd._rule_non_integer_arithmetic("roughly 1.5 + 2.3, an estimate") == [])

check("non_decimal_base_value fires on '0x1F' and returns the exact literal",
      sd._rule_non_decimal_base_value("the mask is 0x1F") == ["0x1F"])

check("non_decimal_base_value fires on a binary literal '0b1010'",
      sd._rule_non_decimal_base_value("stored as 0b1010") == ["0b1010"])

check("large_integer_2^53 fires on 9007199254740993 (one past 2^53) but not on "
      "9007199254740992 (exactly 2^53)",
      sd._rule_large_integer_2_53("value is 9007199254740993") == ["9007199254740993"]
      and sd._rule_large_integer_2_53("value is 9007199254740992") == [])

check("speedup_claim fires on '30% faster' with no hedge nearby",
      sd._rule_speedup_claim("the new version is 30% faster") == ["30% faster"])

check("speedup_claim does NOT fire when 'roughly'/'estimate' hedges the same claim",
      sd._rule_speedup_claim("roughly 30% faster, just a rough estimate") == [])

check("equivalence_or_port_claim does NOT fire on bare prose with no code fence",
      sd._rule_equivalence_or_port_claim("these two approaches are equivalent") == [])

check("big_o_claim does NOT fire on lowercase 'o(' inside ordinary prose",
      sd._rule_big_o_claim("please look into(this) further") == [])

# ── Miss.as_dict() carries every documented field ───────────────────────────

sample = sd.Miss(rule_id="r", claim="c", expected_tools=["t"], called_tools=[],
                  severity="high", location={"file": "f.jsonl", "turn": 0})
sample_dict = sample.as_dict()
check("Miss.as_dict() has exactly the documented keys",
      set(sample_dict) == {"rule_id", "claim", "expected_tools", "called_tools",
                            "severity", "location"},
      f"-> {sorted(sample_dict)}")

print(f"\n=== {len(FAILS)} failures ===" if FAILS else "\n=== ALL SKILL-DETECT TESTS PASS ===")
sys.exit(1 if FAILS else 0)
