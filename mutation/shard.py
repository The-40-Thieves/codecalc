"""Shard the mutmut safe_expr run across CI matrix jobs, and merge the shard results.

Stdlib only (Python 3.11+). Driven by .github/workflows/mutation.yml, and runnable locally:

    uv run --with mutmut==3.8.0 mutmut run --max-children 4 'no-such-mutant-*'   # generates mutants
    uv run --with mutmut==3.8.0 mutmut results --all true > names.txt
    python3 mutation/shard.py plan --results names.txt --shards 2 --out shards
    python3 mutation/shard.py summarize --shard 0 --names shards/shard-0.txt \
        --results names.txt --out shards/summary-0.json
    python3 mutation/shard.py report --plan shards --summaries shards --markdown summary.md

The mutant names come from `mutmut results --all true`, which lists every mutant the generation
step wrote to mutants/**/*.meta (status "not checked" until a run tests it). Status strings are
mutmut 3.8.0's own (mutmut/stats.py status_by_exit_code), and the score is the one its badge uses:
(killed + timeout) / (total - skipped).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# mutmut 3.8.0 status_by_exit_code values. An unknown status is a report problem, not a silent zero.
KNOWN_STATUSES = (
    "killed",
    "survived",
    "no tests",
    "timeout",
    "suspicious",
    "skipped",
    "caught by type check",
    "check was interrupted by user",
    "segfault",
    "not checked",
)
NOT_CHECKED = "not checked"


@dataclass
class Report:
    total: int
    counts: Counter
    checked: int
    score: float
    survivors: list[str]
    statuses: dict[str, str]
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def plan_shards(names, shards):
    """Split mutant names into `shards` lists: sorted, then round-robin, so each shard gets a mix."""
    if shards < 1:
        raise ValueError(f"shards must be at least 1, got {shards}")
    if not names:
        raise ValueError("no mutants to shard: did `mutmut results` list anything?")
    if len(set(names)) != len(names):
        raise ValueError("duplicate mutant names in the plan")
    if shards > len(names):
        raise ValueError(f"{shards} shards for {len(names)} mutants would leave a shard empty")
    ordered = sorted(names)
    return [ordered[i::shards] for i in range(shards)]


def parse_results(text):
    """Read `mutmut results --all true` lines ("    <name>: <status>") into {name: status}."""
    statuses = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        name, sep, status = stripped.rpartition(": ")
        if not sep or not name or not status:
            raise ValueError(f"unparsable mutmut results line: {line!r}")
        if name in statuses:
            raise ValueError(f"mutant listed twice: {name}")
        statuses[name] = status
    return statuses


def summarize_shard(shard, names, statuses_by_name):
    """One shard's statuses. A name mutmut did not list at all counts as not checked."""
    return {
        "shard": shard,
        "total": len(names),
        "statuses": {name: statuses_by_name.get(name, NOT_CHECKED) for name in names},
    }


def build_report(plan, summaries):
    """Merge shard summaries into one report. Survivors are advisory; a missing or empty shard is not."""
    problems = []
    statuses = {}
    for index in range(plan["shards"]):
        summary = summaries.get(index)
        if summary is None:
            problems.append(f"shard {index} has no summary: its job failed or uploaded nothing")
            continue
        shard_statuses = summary["statuses"]
        if sum(status != NOT_CHECKED for status in shard_statuses.values()) == 0:
            problems.append(f"shard {index} checked zero mutants")
        for name, status in shard_statuses.items():
            if status not in KNOWN_STATUSES:
                problems.append(f"shard {index}: unknown status {status!r} for {name}")
            if name in statuses:
                problems.append(f"mutant {name} appears in more than one shard")
            statuses[name] = status

    total = len(statuses)
    if total != plan["names"]:
        problems.append(f"shards cover {total} of {plan['names']} planned mutants")

    counts = Counter(statuses.values())
    tested = total - counts["skipped"]
    score = 0.0 if tested <= 0 else (counts["killed"] + counts["timeout"]) / tested * 100
    return Report(
        total=total,
        counts=counts,
        checked=total - counts[NOT_CHECKED],
        score=score,
        survivors=sorted(name for name, status in statuses.items() if status == "survived"),
        statuses=statuses,
        problems=problems,
    )


def render_markdown(report):
    lines = [
        "## mutation: codecalc/safe_expr.py (sharded)",
        "",
        "| status | count |",
        "|---|---:|",
        *(f"| {status} | {report.counts[status]} |" for status in KNOWN_STATUSES),
        f"| **total** | {report.total} |",
        "",
        (
            f"Checked {report.checked} of {report.total}. Mutation score {report.score:.1f}% "
            "((killed + timeout) / (total - skipped)). Advisory: survivors do not fail this job."
        ),
    ]
    if report.problems:
        lines += ["", "### Problems (this job fails)", "", *(f"- {problem}" for problem in report.problems)]
    if report.survivors:
        lines += ["", f"### Survivors ({len(report.survivors)})", "", *(f"- `{name}`" for name in report.survivors)]
    return "\n".join(lines) + "\n"


def _read_summaries(directory):
    summaries = {}
    for path in sorted(directory.rglob("summary-*.json")):  # artifacts land one dir per shard
        data = json.loads(path.read_text())
        if data["shard"] in summaries:
            raise ValueError(f"two summaries claim shard {data['shard']}")
        summaries[data["shard"]] = data
    return summaries


def _cmd_plan(args):
    names = list(parse_results(args.results.read_text()))
    parts = plan_shards(names, args.shards)
    args.out.mkdir(parents=True, exist_ok=True)
    for index, part in enumerate(parts):
        (args.out / f"shard-{index}.txt").write_text("".join(f"{name}\n" for name in part))
    (args.out / "plan.json").write_text(json.dumps({"names": len(names), "shards": args.shards}) + "\n")
    print(json.dumps(list(range(args.shards))))
    return 0


def _cmd_summarize(args):
    names = args.names.read_text().split()
    summary = summarize_shard(args.shard, names, parse_results(args.results.read_text()))
    args.out.write_text(json.dumps(summary, indent=2) + "\n")
    return 0


def _cmd_report(args):
    plan = json.loads((args.plan / "plan.json").read_text())
    report = build_report(plan, _read_summaries(args.summaries))
    args.markdown.write_text(render_markdown(report))
    if args.merged:
        merged = {
            "total": report.total,
            "checked": report.checked,
            "score": report.score,
            "counts": dict(report.counts),
            "survivors": report.survivors,
            "problems": report.problems,
            "statuses": report.statuses,
        }
        args.merged.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    for problem in report.problems:
        print(f"report: {problem}", file=sys.stderr)
    return 0 if report.ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="shard.py", description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="split the listed mutants into shard files")
    plan.add_argument("--results", type=Path, required=True, help="output of `mutmut results --all true`")
    plan.add_argument("--shards", type=int, required=True)
    plan.add_argument("--out", type=Path, required=True)
    plan.set_defaults(run=_cmd_plan)

    summarize = sub.add_parser("summarize", help="one shard's statuses after its mutmut run")
    summarize.add_argument("--shard", type=int, required=True)
    summarize.add_argument("--names", type=Path, required=True, help="that shard's shard-N.txt")
    summarize.add_argument("--results", type=Path, required=True, help="output of `mutmut results --all true`")
    summarize.add_argument("--out", type=Path, required=True)
    summarize.set_defaults(run=_cmd_summarize)

    report = sub.add_parser("report", help="merge shard summaries; exit 1 on a missing or empty shard")
    report.add_argument("--plan", type=Path, required=True, help="directory holding plan.json")
    report.add_argument("--summaries", type=Path, required=True, help="directory holding summary-N.json")
    report.add_argument("--markdown", type=Path, required=True)
    report.add_argument("--merged", type=Path)
    report.set_defaults(run=_cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"shard.py {args.command}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
