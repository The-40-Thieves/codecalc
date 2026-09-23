"""Exact dependency ordering and minimum-wave scheduling for ``plan_order``.

The fast path is ordinary graph theory: Kahn layering gives both a stable
topological order and the minimum possible number of dependency-only waves.
The constrained path uses z3 Optimize because ``exclusive_with`` and
``max_parallel`` turn the question into a bounded scheduling problem.  No
submitted code is executed and this module performs no I/O or network access.

The claim is deliberately narrow.  A successful result proves that the order
is correct *for the dependency graph supplied by the caller*.  It cannot infer
missing dependencies or decide whether a declared dependency is true.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, cast

from . import contract, errors, grades, optional

PURE_STEP_CAP = 1_000
Z3_STEP_CAP = 100
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 120


@dataclass(frozen=True)
class _Step:
    id: str
    depends_on: tuple[str, ...]
    cost: int | float | None
    exclusive_with: tuple[str, ...]
    index: int


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return contract.stamp(errors.error_result(code, message, **extra))


def _validation(message: str, **extra: Any) -> dict[str, Any]:
    return _error(errors.VALIDATION, message, **extra)


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _parse_steps(raw_steps: list[dict[str, Any]]) -> tuple[list[_Step] | None, dict[str, Any] | None]:
    if not isinstance(raw_steps, list) or not raw_steps:
        return None, _validation("steps must be a non-empty list")

    parsed: list[_Step] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            return None, _validation(f"steps[{index}] must be an object")
        step_id = raw.get("id")
        if not isinstance(step_id, str) or not step_id:
            return None, _validation(f"steps[{index}].id must be a non-empty string")
        if step_id in seen:
            return None, _validation(f"duplicate step id {step_id!r}", step_id=step_id)
        seen.add(step_id)

        depends_on = raw.get("depends_on", [])
        exclusive_with = raw.get("exclusive_with", [])
        if (not isinstance(depends_on, list)
                or any(not isinstance(value, str) for value in depends_on)):
            return None, _validation(
                f"step {step_id!r} depends_on must be a list of step ids",
                step_id=step_id,
            )
        if (not isinstance(exclusive_with, list)
                or any(not isinstance(value, str) for value in exclusive_with)):
            return None, _validation(
                f"step {step_id!r} exclusive_with must be a list of step ids",
                step_id=step_id,
            )

        cost = raw.get("cost")
        if cost is not None:
            if isinstance(cost, bool) or not isinstance(cost, (int, float)):
                return None, _validation(
                    f"step {step_id!r} cost must be a finite non-negative number or null",
                    step_id=step_id,
                )
            if not math.isfinite(cost) or cost < 0:
                return None, _validation(
                    f"step {step_id!r} cost must be finite and non-negative",
                    step_id=step_id,
                )

        parsed.append(_Step(
            id=step_id,
            depends_on=_dedupe(depends_on),
            cost=cost,
            exclusive_with=_dedupe(exclusive_with),
            index=index,
        ))

    ids = {step.id for step in parsed}
    for step in parsed:
        if step.id in step.depends_on:
            return None, _validation(
                f"step {step.id!r} cannot depend on itself", step_id=step.id,
            )
        if step.id in step.exclusive_with:
            return None, _validation(
                f"step {step.id!r} cannot be exclusive with itself", step_id=step.id,
            )
        for field, values in (("depends_on", step.depends_on),
                              ("exclusive_with", step.exclusive_with)):
            for target in values:
                if target not in ids:
                    return None, _validation(
                        f"step {step.id!r} names unknown id {target!r} in {field}",
                        step_id=step.id, field=field, unknown_id=target,
                    )
    return parsed, None


def _graphs(steps: list[_Step]) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Return dependency -> dependents and indegree, both input-order stable."""
    successors = {step.id: [] for step in steps}
    indegree = {step.id: len(step.depends_on) for step in steps}
    for step in steps:
        for dependency in step.depends_on:
            successors[dependency].append(step.id)
    return successors, indegree


def _find_cycle(steps: list[_Step], successors: dict[str, list[str]]) -> list[str] | None:
    """Return the first deterministic directed loop, including its repeated start."""
    state = {step.id: 0 for step in steps}
    for root in steps:
        if state[root.id] != 0:
            continue
        path = [root.id]
        positions = {root.id: 0}
        iterators = [iter(successors[root.id])]
        state[root.id] = 1
        while iterators:
            node = path[-1]
            try:
                successor = next(iterators[-1])
            except StopIteration:
                state[node] = 2
                positions.pop(node)
                path.pop()
                iterators.pop()
                continue
            if state[successor] == 0:
                state[successor] = 1
                positions[successor] = len(path)
                path.append(successor)
                iterators.append(iter(successors[successor]))
            elif state[successor] == 1:
                start = positions[successor]
                return [*path[start:], successor]
    return None


def _kahn_waves(steps: list[_Step], successors: dict[str, list[str]],
                indegree: dict[str, int]) -> list[list[str]]:
    """Input-stable Kahn layering; the number of layers is the DAG height."""
    remaining = dict(indegree)
    ready = [step.id for step in steps if remaining[step.id] == 0]
    waves: list[list[str]] = []
    while ready:
        wave = ready
        waves.append(wave)
        became_ready: set[str] = set()
        for step_id in wave:
            for successor in successors[step_id]:
                remaining[successor] -= 1
                if remaining[successor] == 0:
                    became_ready.add(successor)
        ready = [step.id for step in steps if step.id in became_ready]
    return waves


def _objective_value(z3: Any, handle: Any) -> int | None:
    lower = handle.lower()
    upper = handle.upper()
    if not z3.is_int_value(lower) or not z3.is_int_value(upper):
        return None
    if lower.as_long() != upper.as_long():
        return None
    return lower.as_long()


def _z3_waves(steps: list[_Step], max_parallel: int | None,
              timeout: int) -> tuple[str, list[list[str]], str]:
    """Return (verdict, waves, evidence); never labels a partial model optimal."""
    z3 = optional.require("z3")
    optimizer = z3.Optimize()
    optimizer.set(timeout=timeout * 1_000)
    wave_vars = [z3.Int(f"plan_order_wave_{i}") for i in range(len(steps))]
    by_id = {step.id: step for step in steps}

    for variable in wave_vars:
        optimizer.add(variable >= 0, variable < len(steps))
    for step in steps:
        for dependency in step.depends_on:
            optimizer.add(wave_vars[by_id[dependency].index] < wave_vars[step.index])

    exclusive_pairs: set[tuple[int, int]] = set()
    for step in steps:
        for other_id in step.exclusive_with:
            pair = tuple(sorted((step.index, by_id[other_id].index)))
            if pair not in exclusive_pairs:
                exclusive_pairs.add(pair)
                optimizer.add(wave_vars[pair[0]] != wave_vars[pair[1]])

    if max_parallel is not None:
        for wave in range(len(steps)):
            optimizer.add(z3.Sum([
                z3.If(variable == wave, 1, 0) for variable in wave_vars
            ]) <= max_parallel)

    wave_count = z3.Int("plan_order_wave_count")
    optimizer.add(wave_count >= 1, wave_count <= len(steps))
    for variable in wave_vars:
        optimizer.add(wave_count >= variable + 1)

    successors, indegree = _graphs(steps)
    dependency_height = len(_kahn_waves(steps, successors, indegree))
    lower_bound = dependency_height
    if max_parallel is not None:
        lower_bound = max(lower_bound, math.ceil(len(steps) / max_parallel))
    if exclusive_pairs:
        lower_bound = max(lower_bound, 2)
    optimizer.add(wave_count >= lower_bound)

    # Exact symmetry breaking. Steps with identical predecessors, successors,
    # and exclusivity neighborhoods are interchangeable in every constraint;
    # placing input-earlier twins no later loses no schedule and removes the
    # factorial family of renamed models that otherwise dominates Optimize.
    profiles: dict[tuple[Any, ...], list[_Step]] = {}
    for step in steps:
        exclusive_neighbors = tuple(sorted(
            other.id for other in steps
            if tuple(sorted((step.index, other.index))) in exclusive_pairs
        ))
        profile = (step.depends_on, tuple(successors[step.id]), exclusive_neighbors)
        profiles.setdefault(profile, []).append(step)
    for twins in profiles.values():
        for left, right in pairwise(twins):
            optimizer.add(wave_vars[left.index] <= wave_vars[right.index])

    objective = optimizer.minimize(wave_count)
    started = time.monotonic()

    checked = optimizer.check()
    if checked == z3.unknown:
        reason = optimizer.reason_unknown() or "z3 timeout"
        return "unknown", [], f"z3 timed out before proving the optimum: {reason}"
    if checked == z3.unsat:
        return "infeasible", [], "z3 proved the scheduling constraints unsatisfiable"

    optimum = _objective_value(z3, objective)
    if optimum is None:
        # z3 can return a best-so-far model when an Optimize timeout lands.
        # Such a model is useful, but the contract explicitly forbids calling
        # it optimal, so this tool returns no schedule at all.
        return "unknown", [], "z3 timed out before closing every optimization bound"

    count = int(optimum)

    # A second, plain Solver pass makes the tie-break deterministic without
    # asking Optimize to prove N more objectives.  Fix the proven minimum,
    # then choose the earliest satisfiable wave for each step in input order.
    # Each committed equality narrows the next check, so the final assignment
    # is the unique lexicographic minimum among all minimum-wave schedules.
    solver = z3.Solver()
    solver.add(*optimizer.assertions())
    solver.add(wave_count == count)
    for wave in range(count):
        solver.add(z3.Or([variable == wave for variable in wave_vars]))
    assignments: list[int] = []
    for variable in wave_vars:
        chosen = None
        for wave in range(count):
            remaining_ms = int((timeout - (time.monotonic() - started)) * 1_000)
            if remaining_ms <= 0:
                return "unknown", [], "z3 timed out while closing the input-order tie-break"
            solver.set(timeout=remaining_ms)
            solver.push()
            solver.add(variable == wave)
            tie_checked = solver.check()
            solver.pop()
            if tie_checked == z3.unknown:
                return "unknown", [], "z3 timed out while closing the input-order tie-break"
            if tie_checked == z3.sat:
                chosen = wave
                solver.add(variable == wave)
                assignments.append(wave)
                break
        if chosen is None:
            return "internal", [], "z3 could not reconstruct its proven minimum-wave schedule"

    waves = [[step.id for step in steps if assignments[step.index] == wave]
             for wave in range(count)]
    if any(not wave for wave in waves):
        return "internal", [], "z3 returned a non-contiguous optimized schedule"
    return "ordered", waves, (
        f"z3 {z3.get_version_string()} closed the minimum-wave objective and "
        f"lexicographic satisfiability tie-break within {timeout}s"
    )


def _clean_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _critical_path(steps: list[_Step], order: list[str],
                   successors: dict[str, list[str]]) -> tuple[dict[str, Any] | None,
                                                            dict[str, int | float] | None]:
    if any(step.cost is None for step in steps):
        return None, None

    by_id = {step.id: step for step in steps}
    earliest_start: dict[str, float] = {}
    earliest_finish: dict[str, float] = {}
    predecessor: dict[str, str | None] = {}
    for step_id in order:
        step = by_id[step_id]
        if not step.depends_on:
            earliest_start[step_id] = 0.0
            predecessor[step_id] = None
        else:
            start = max(earliest_finish[dependency] for dependency in step.depends_on)
            earliest_start[step_id] = start
            predecessor[step_id] = next(
                dependency for dependency in step.depends_on
                if earliest_finish[dependency] == start
            )
        earliest_finish[step_id] = earliest_start[step_id] + float(step.cost)

    duration = max(earliest_finish.values())
    # The chain must end at a SINK. Picking the first step whose finish ties
    # the maximum stopped at `a` in [a(5) -> b(0)] and dropped `b`, while
    # slack still reported b as critical (audit, 2026-09-22). Costs are
    # non-negative, so finishes never fall along an edge and a sink with the
    # maximum finish always exists.
    end = next(step.id for step in steps
               if not successors[step.id] and earliest_finish[step.id] == duration)
    critical: list[str] = []
    cursor: str | None = end
    while cursor is not None:
        critical.append(cursor)
        cursor = predecessor[cursor]
    critical.reverse()

    latest_finish: dict[str, float] = {}
    latest_start: dict[str, float] = {}
    for step_id in reversed(order):
        step = by_id[step_id]
        if successors[step_id]:
            latest_finish[step_id] = min(latest_start[successor]
                                         for successor in successors[step_id])
        else:
            latest_finish[step_id] = duration
        latest_start[step_id] = latest_finish[step_id] - float(step.cost)

    slack = {
        step.id: _clean_number(latest_start[step.id] - earliest_start[step.id])
        for step in steps
    }
    return {"steps": critical, "total_cost": _clean_number(duration)}, slack


def _grade(result: dict[str, Any], *, exact: bool, basis: str) -> dict[str, Any]:
    if exact:
        return grades.grade_plan_order(result, proven=True, basis=basis)
    return grades.grade_plan_order(result, proven=False, basis=basis)


def plan_order(steps: list[dict[str, Any]], max_parallel: int | None = None,
               timeout: int = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Compute an exact order or return a coded refusal.  See module docs."""
    parsed, refusal = _parse_steps(steps)
    if refusal is not None:
        return refusal
    parsed = cast(list[_Step], parsed)

    if max_parallel is not None and (
        isinstance(max_parallel, bool)
        or not isinstance(max_parallel, int)
        or max_parallel < 1
    ):
        return _validation("max_parallel must be a positive integer or null")
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        return _validation("timeout must be an integer number of seconds")
    timeout = max(1, min(timeout, MAX_TIMEOUT_S))

    constrained = max_parallel is not None or any(step.exclusive_with for step in parsed)
    cap = Z3_STEP_CAP if constrained else PURE_STEP_CAP
    if len(parsed) > cap:
        path = "z3" if constrained else "pure"
        return _error(
            errors.RESOURCE_EXHAUSTED,
            f"{path} plan_order path accepts at most {cap} steps; got {len(parsed)}",
            limit=cap, count=len(parsed), path=path,
        )

    successors, indegree = _graphs(parsed)
    cycle = _find_cycle(parsed, successors)
    if cycle is not None:
        result = contract.stamp({
            "ok": True,
            "verdict": "cyclic",
            "order": [],
            "waves": [],
            "wave_count": 0,
            "wave_count_proven_minimal": False,
            "critical_path": None,
            "slack": None,
            "cycle": cycle,
            "method": "graph_cycle",
        })
        return _grade(result, exact=True,
                      basis="deterministic depth-first graph walk returned the explicit dependency cycle")

    if constrained:
        verdict, waves, evidence = _z3_waves(parsed, max_parallel, timeout)
        if verdict == "internal":
            return _error(errors.INTERNAL, evidence, method="z3_optimize")
        if verdict != "ordered":
            result = contract.stamp({
                "ok": True,
                "verdict": verdict,
                "order": [],
                "waves": [],
                "wave_count": None,
                "wave_count_proven_minimal": False,
                "critical_path": None,
                "slack": None,
                "cycle": None,
                "method": "z3_optimize",
                "reason": evidence,
            })
            return _grade(result, exact=verdict == "infeasible", basis=evidence)
        method = "z3_optimize"
        basis = evidence
    else:
        waves = _kahn_waves(parsed, successors, indegree)
        method = "kahn"
        basis = ("Kahn layering over the complete DAG; each wave is one longest-path "
                 "level, so the wave count is minimal by construction")

    order = [step_id for wave in waves for step_id in wave]
    critical_path, slack = _critical_path(parsed, order, successors)
    result = contract.stamp({
        "ok": True,
        "verdict": "ordered",
        "order": order,
        "waves": waves,
        "wave_count": len(waves),
        "wave_count_proven_minimal": True,
        "critical_path": critical_path,
        "slack": slack,
        "cycle": None,
        "method": method,
    })
    return _grade(result, exact=True, basis=basis)
