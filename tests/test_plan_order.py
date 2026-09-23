"""Exact-value tests for plan_order.  Standalone runner, not pytest."""

from __future__ import annotations

import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import plan_order

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name} {detail}")
    if not condition:
        FAILS.append(name)


def run(steps, **kwargs):
    return plan_order.plan_order(steps, **kwargs)


# ── exact pure-graph schedules ──────────────────────────────────────────────
linear = run([
    {"id": "fetch"},
    {"id": "build", "depends_on": ["fetch"]},
    {"id": "ship", "depends_on": ["build"]},
])
check("linear chain: exact input-stable order",
      linear["order"] == ["fetch", "build", "ship"], f"-> {linear}")
check("linear chain: one step per dependency wave",
      linear["waves"] == [["fetch"], ["build"], ["ship"]])
check("linear chain: three waves proven minimal",
      linear["wave_count"] == 3 and linear["wave_count_proven_minimal"] is True)
check("linear chain: pure Kahn path is proof-graded",
      linear["method"] == "kahn" and linear["grade"] == "solver_proven")

diamond = run([
    {"id": "a"},
    {"id": "b", "depends_on": ["a"]},
    {"id": "c", "depends_on": ["a"]},
    {"id": "d", "depends_on": ["b", "c"]},
])
check("diamond: middle steps share one parallel wave",
      diamond["waves"] == [["a"], ["b", "c"], ["d"]], f"-> {diamond['waves']}")
check("diamond: deterministic linearization flattens waves in input order",
      diamond["order"] == ["a", "b", "c", "d"])

independent = run([{"id": "third"}, {"id": "first"}, {"id": "second"}])
check("independent: every step is in one wave",
      independent["waves"] == [["third", "first", "second"]])
check("deterministic tie-break: input order is preserved",
      independent["order"] == ["third", "first", "second"])
check("missing cost: critical_path and slack are explicitly null",
      independent["critical_path"] is None and independent["slack"] is None)


# ── cycles and refusals ─────────────────────────────────────────────────────
cycle2 = run([
    {"id": "a", "depends_on": ["b"]},
    {"id": "b", "depends_on": ["a"]},
])
check("two-node cycle: exact ordered loop",
      cycle2["verdict"] == "cyclic" and cycle2["cycle"] == ["a", "b", "a"],
      f"-> {cycle2}")

cycle3 = run([
    {"id": "a", "depends_on": ["c"]},
    {"id": "b", "depends_on": ["a"]},
    {"id": "c", "depends_on": ["b"]},
])
check("three-node cycle: exact ordered loop",
      cycle3["cycle"] == ["a", "b", "c", "a"], f"-> {cycle3['cycle']}")

self_loop = run([{"id": "a", "depends_on": ["a"]}])
check("self-dependency: validation refusal, not a cyclic success",
      self_loop["ok"] is False and self_loop["code"] == "validation"
      and "itself" in self_loop["error"], f"-> {self_loop}")

refusals = [
    ("empty steps", [], "validation"),
    ("duplicate ids", [{"id": "a"}, {"id": "a"}], "validation"),
    ("unknown dependency", [{"id": "a", "depends_on": ["missing"]}], "validation"),
    ("unknown exclusivity id", [{"id": "a", "exclusive_with": ["missing"]}], "validation"),
    ("negative cost", [{"id": "a", "cost": -1}], "validation"),
    ("infinite cost", [{"id": "a", "cost": math.inf}], "validation"),
    ("NaN cost", [{"id": "a", "cost": math.nan}], "validation"),
]
for name, steps, code in refusals:
    refused = run(steps)
    check(f"refusal: {name} has the exact error code",
          refused["ok"] is False and refused["code"] == code, f"-> {refused}")

unknown_dep = run([{"id": "known", "depends_on": ["missing"]}])
check("unknown id refusal names the exact id and field",
      unknown_dep["unknown_id"] == "missing" and unknown_dep["field"] == "depends_on")


# ── CPM values worked by hand ──────────────────────────────────────────────
cpm = run([
    {"id": "a", "cost": 3},
    {"id": "b", "depends_on": ["a"], "cost": 2},
    {"id": "c", "depends_on": ["a"], "cost": 4},
    {"id": "d", "depends_on": ["b", "c"], "cost": 2},
])
check("CPM: a-c-d is the critical path with total cost 9",
      cpm["critical_path"] == {"steps": ["a", "c", "d"], "total_cost": 9},
      f"-> {cpm['critical_path']}")
check("CPM: per-step slack is the hand-worked forward/backward result",
      cpm["slack"] == {"a": 0, "b": 2, "c": 0, "d": 0},
      f"-> {cpm['slack']}")

# A zero-cost tail ties the maximum finish at an interior step. The path used
# to stop at `a` and drop `b`, while slack still marked `b` critical.
milestone = run([
    {"id": "a", "cost": 5},
    {"id": "b", "depends_on": ["a"], "cost": 0},
])
check("CPM: a zero-cost milestone tail stays on the critical path",
      milestone["critical_path"] == {"steps": ["a", "b"], "total_cost": 5},
      f"-> {milestone['critical_path']}")
check("CPM: every zero-slack step on a single chain is on the reported path",
      [s for s, v in milestone["slack"].items() if v == 0]
      == milestone["critical_path"]["steps"],
      f"-> {milestone['slack']}")


# ── constrained minimum-wave schedules ─────────────────────────────────────
capacity = run([{"id": "a"}, {"id": "b"}, {"id": "c"}], max_parallel=2)
check("max_parallel: three independent steps need exactly two waves at cap 2",
      capacity["waves"] == [["a", "b"], ["c"]] and capacity["wave_count"] == 2,
      f"-> {capacity}")
check("max_parallel: z3 closed the optimum before labelling it minimal",
      capacity["method"] == "z3_optimize"
      and capacity["wave_count_proven_minimal"] is True
      and capacity["grade"] == "solver_proven")

exclusive = run([
    {"id": "a", "exclusive_with": ["b"]},
    {"id": "b"},
])
check("exclusive_with: independent steps are split into deterministic waves",
      exclusive["waves"] == [["a"], ["b"]], f"-> {exclusive}")
check("exclusive_with: symmetric enforcement works from one declaration",
      exclusive["order"] == ["a", "b"] and exclusive["wave_count"] == 2)


# ── bounded work ────────────────────────────────────────────────────────────
pure_cap = run([{"id": f"s{i}"} for i in range(plan_order.PURE_STEP_CAP + 1)])
check("pure size cap: resource_exhausted with the exact limit",
      pure_cap["code"] == "resource_exhausted"
      and pure_cap["limit"] == plan_order.PURE_STEP_CAP
      and pure_cap["count"] == plan_order.PURE_STEP_CAP + 1,
      f"-> {pure_cap}")

z3_cap = run([{"id": f"s{i}"} for i in range(plan_order.Z3_STEP_CAP + 1)],
             max_parallel=2)
check("z3 size cap: resource_exhausted with the lower exact limit",
      z3_cap["code"] == "resource_exhausted"
      and z3_cap["limit"] == plan_order.Z3_STEP_CAP
      and z3_cap["count"] == plan_order.Z3_STEP_CAP + 1,
      f"-> {z3_cap}")


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL PLAN_ORDER TESTS PASS ===")
for failure in FAILS:
    print(f"  {failure}")
sys.exit(1 if FAILS else 0)
