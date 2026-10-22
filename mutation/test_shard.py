"""Tests for mutation/shard.py: the mutant plan, per-shard summary, and the merged report.

Run with the mutation lane's pins (pytest is not a project dependency):

    uv run --no-sync --with pytest==9.1.1 pytest mutation/test_shard.py -q -p no:cacheprovider
"""

import json

import pytest
import shard


def mutant_names(count):
    return [f"codecalc.safe_expr.x_f{i:03d}__mutmut_1" for i in range(count)]


def summary(index, statuses):
    return {"shard": index, "total": len(statuses), "statuses": dict(statuses)}


@pytest.mark.parametrize(("count", "shards"), [(1, 1), (10, 3), (37, 8), (37, 16), (16, 16)])
def test_plan_covers_every_name_exactly_once(count, shards):
    names = mutant_names(count)
    plan = shard.plan_shards(names, shards)
    assert len(plan) == shards
    flat = [name for part in plan for name in part]
    assert len(flat) == len(set(flat)) == count
    assert sorted(flat) == sorted(names)


def test_plan_is_deterministic_round_robin():
    names = mutant_names(20)
    shuffled = names[7:] + names[:7]  # not sorted, deterministic
    assert shard.plan_shards(shuffled, 3) == shard.plan_shards(names, 3)
    ordered = sorted(names)
    assert shard.plan_shards(names, 3) == [ordered[i::3] for i in range(3)]


@pytest.mark.parametrize(
    ("names", "shards"),
    [
        ([], 2),  # nothing generated: the lane must not go green on zero mutants
        (mutant_names(3), 4),  # a shard would get no mutants at all
        (mutant_names(3) + mutant_names(3)[:1], 1),  # duplicate name
        (mutant_names(3), 0),
    ],
)
def test_plan_rejects_bad_input(names, shards):
    with pytest.raises(ValueError):
        shard.plan_shards(names, shards)


def test_parse_results_reads_mutmut_lines():
    text = (
        "    codecalc.safe_expr.x_a__mutmut_1: killed\n"
        "    codecalc.safe_expr.x_a__mutmut_2: not checked\n"
        "    codecalc.safe_expr.x_b__mutmut_1: no tests\n"
    )
    assert shard.parse_results(text) == {
        "codecalc.safe_expr.x_a__mutmut_1": "killed",
        "codecalc.safe_expr.x_a__mutmut_2": "not checked",
        "codecalc.safe_expr.x_b__mutmut_1": "no tests",
    }


def test_parse_results_rejects_unparsable_line():
    with pytest.raises(ValueError):
        shard.parse_results("    codecalc.safe_expr.x_a__mutmut_1 killed\n")


def test_summarize_keeps_only_shard_names_and_marks_missing_not_checked():
    names = mutant_names(3)
    results = {names[0]: "killed", names[2]: "survived", "codecalc.safe_expr.other__mutmut_1": "killed"}
    out = shard.summarize_shard(1, [names[0], names[1]], results)
    assert out == {
        "shard": 1,
        "total": 2,
        "statuses": {names[0]: "killed", names[1]: "not checked"},
    }


def test_report_totals_a_fixture_and_scores_like_mutmut():
    summaries = {
        0: summary(0, {"a": "killed", "b": "survived", "c": "timeout"}),
        1: summary(1, {"d": "killed", "e": "skipped", "f": "suspicious"}),
        2: summary(2, {"g": "no tests", "h": "not checked", "i": "killed"}),
    }
    report = shard.build_report({"names": 9, "shards": 3}, summaries)
    assert report.ok, report.problems
    assert report.total == 9
    assert report.counts["killed"] == 3
    assert report.counts["survived"] == 1
    assert report.counts["timeout"] == 1
    assert report.counts["suspicious"] == 1
    assert report.counts["skipped"] == 1
    assert report.counts["not checked"] == 1
    assert report.checked == 8
    # mutmut's badge formula: (killed + timeout) / (total - skipped)
    assert report.score == pytest.approx((3 + 1) / (9 - 1) * 100)
    assert report.survivors == ["b"]


def test_report_fails_on_missing_shard():
    summaries = {0: summary(0, {"a": "killed"}), 2: summary(2, {"c": "killed"})}
    report = shard.build_report({"names": 3, "shards": 3}, summaries)
    assert not report.ok
    assert any("shard 1" in problem for problem in report.problems)


def test_report_fails_on_zero_checked_shard():
    summaries = {
        0: summary(0, {"a": "killed"}),
        1: summary(1, {"b": "not checked", "c": "not checked"}),
    }
    report = shard.build_report({"names": 3, "shards": 2}, summaries)
    assert not report.ok
    assert any("shard 1" in problem for problem in report.problems)


def test_report_fails_on_zero_mutant_shard():
    summaries = {0: summary(0, {"a": "killed"}), 1: {"shard": 1, "total": 0, "statuses": {}}}
    report = shard.build_report({"names": 1, "shards": 2}, summaries)
    assert not report.ok


def test_report_fails_when_shards_do_not_cover_the_plan():
    summaries = {0: summary(0, {"a": "killed", "b": "killed"}), 1: summary(1, {"c": "killed", "d": "killed"})}
    report = shard.build_report({"names": 5, "shards": 2}, summaries)
    assert not report.ok
    assert any("4 of 5" in problem for problem in report.problems)


def test_report_survivors_do_not_fail_the_report():
    summaries = {0: summary(0, {"a": "survived"}), 1: summary(1, {"b": "killed"})}
    report = shard.build_report({"names": 2, "shards": 2}, summaries)
    assert report.ok, report.problems
    assert report.survivors == ["a"]


def test_main_plan_writes_shard_files_and_prints_matrix(tmp_path, capsys):
    results = tmp_path / "names.txt"
    results.write_text("".join(f"    {name}: not checked\n" for name in mutant_names(5)))
    out = tmp_path / "shards"
    assert shard.main(["plan", "--results", str(results), "--shards", "2", "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out) == [0, 1]
    assert json.loads((out / "plan.json").read_text()) == {"names": 5, "shards": 2}
    assert (out / "shard-0.txt").read_text().splitlines() == mutant_names(5)[0::2]


def test_main_report_exit_codes(tmp_path):
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "plan.json").write_text(json.dumps({"names": 2, "shards": 2}))
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "summary-0.json").write_text(json.dumps(summary(0, {"a": "killed"})))
    markdown = tmp_path / "summary.md"

    # shard 1's summary is missing: the report must fail, and still write the table
    assert shard.main(["report", "--plan", str(plan), "--summaries", str(summaries), "--markdown", str(markdown)]) == 1
    assert "| killed |" in markdown.read_text()

    (summaries / "summary-1.json").write_text(json.dumps(summary(1, {"b": "survived"})))
    assert shard.main(["report", "--plan", str(plan), "--summaries", str(summaries), "--markdown", str(markdown)]) == 0
