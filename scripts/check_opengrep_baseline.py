#!/usr/bin/env python3
"""The opengrep baseline, as a checkable record instead of a comment that rots.

ci-security.yml carried its SAST baseline in prose:

    Baseline at wiring time: 5 findings, all `audit` severity, all reviewed —
      1x exec-detected      _worker_bootstrap.py, the documented REPL-subprocess exec
      4x dynamic-urllib-use llm.py / context7.py / complexity.py.

`llm.py` and `context7.py` were deleted when the LLM dependency and context7 were
removed. Four of five baseline entries then named files that did not exist, and
nobody noticed, because a comment has no gate. Meanwhile the scan had dropped to
2 findings and one of them — a non-literal `importlib.import_module` in
optional.py — was NEW and undocumented.

That is the trap: "2 findings against a baseline of 5" reads as improvement. What
actually happened is that four findings vanished with their files while a fresh
one appeared. The comparison was meaningless in exactly the direction that hides
a new finding.

It is also the second time a justification in this repo outlived the files it
named. The first was the `S310` ruff exemption, which cited llm.py and
context7.py and was therefore switching off a live security lint in the package
whose headline property is that it opens no sockets. check_no_eval.py already
learned the lesson and asserts its documented exec() exception still exists:
"an allow-list entry for a missing file silently widens the policy." This applies
the same rule to the one exemption record that never got it.

BASELINE below is the single source of truth. ci-security.yml points here rather
than restating it, because two copies of a list is how the first one rots.

Two modes:

  (no args)            STRUCTURAL. Runs on a bare checkout with no dependencies,
                       so it gates every PR. Asserts each accepted finding names
                       a file that exists and carries a non-empty reason. This is
                       the half that catches a file deletion the day it happens.

  --findings <file>    COMPARISON against real `opengrep --json` output. Fails on
                       any finding absent from BASELINE, and reports accepted
                       entries that no longer fire so the record can be trimmed.
                       Only meaningful where opengrep actually ran.

FLOORS, because every failure mode here produces a clean-looking pass:
  * an empty BASELINE would vacuously satisfy every check
  * an entry with no reason is an unreviewed exemption wearing a reviewed badge
  * a scan that errored emits `results: []`, byte-identical to a clean tree —
    the same trap `--fail-on-scan-errors` exists to close for trufflehog
  * a findings file whose schema we cannot read must fail, not report zero
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Accepted findings, keyed (rule id, repo-relative path). Line numbers are
#: deliberately NOT part of the key: they move on every edit above the site, and
#: a baseline that churns is a baseline nobody re-reads. Rule plus file is the
#: granularity at which a human actually reviews one of these.
#:
#: Every entry needs a reason that says why it is not exploitable. "Known" is not
#: a reason. If the reason cannot be written, the finding is not accepted yet.
BASELINE: dict[tuple[str, str], str] = {
    (
        "python.lang.security.audit.exec-detected.exec-detected",
        "codecalc/_worker_bootstrap.py",
    ): (
        "The documented REPL-subprocess exec, and the single exec() in the package. "
        "It runs caller-supplied code inside the stateful worker SUBPROCESS: separate "
        "process, env allowlist, own process group, never the server process. Same "
        "threat model as the Rust executor. AUDIT.md feature-expansion item 1, and "
        "check_no_eval.py gates it to exactly this one file."
    ),
    (
        "python.lang.security.audit.non-literal-import.non-literal-import",
        "codecalc/optional.py",
    ): (
        "optional.require(module) is the lazy-extras importer. The rule fires on the "
        "`module: str` parameter, not on a reachable untrusted value: all five call "
        "sites in the package pass string literals -- require('sympy') x3, "
        "require('z3'), require('tree_sitter_language_pack'). The one variable call "
        "in the tree, optional.have(m) in doctor.py, resolves EXTRAS keys and routes "
        "to have(), which uses importlib.util.find_spec and never reaches "
        "import_module. No caller path carries external input."
    ),
}

#: A findings file reporting fewer scanned paths than this means the scan did not
#: really run, and "no new findings" would be a statement about nothing. The tree
#: has ~116 tracked files and opengrep reports ~85 scanned (it skips binaries and
#: unsupported languages), so this floor sits well below the real number and well
#: above a fetch failure.
#:
#: This floors on `paths.scanned` specifically, and NOT on a rule count, because
#: real `opengrep --json` output has no `rules` key at all. The first version of
#: this file floored on `doc.get("rules")`, which is always absent: the guard read
#: `if rules and len(rules) < MIN`, short-circuited on the falsy empty list, and
#: could never fire. Measured against opengrep 1.26.0 output, which is the only
#: reason it was caught. A floor that cannot fail is decoration.
MIN_PATHS_SCANNED = 50


def fail(message: str) -> None:
    print(f"::error::{message}")
    sys.exit(1)


def check_structure() -> None:
    """Static assertions about the record itself. No opengrep required."""
    if not BASELINE:
        fail(
            "BASELINE is empty. An empty baseline satisfies every check below "
            "vacuously and would accept any finding the scan produces."
        )

    missing = []
    unreasoned = []
    for (rule, path), reason in sorted(BASELINE.items()):
        if not (REPO / path).is_file():
            missing.append(f"{path}  (accepted for {rule})")
        if not reason or not reason.strip():
            unreasoned.append(f"{path}  ({rule})")

    if missing:
        fail(
            f"{len(missing)} baseline entr(y/ies) name a file that no longer exists:\n  "
            + "\n  ".join(missing)
            + "\nA baseline entry for a missing file cannot be reviewed, and it hides "
            "whatever finding replaced it. Either the file moved (update BASELINE) or "
            "the finding is gone (delete the entry). This is the exact rot that took "
            "llm.py and context7.py with it."
        )

    if unreasoned:
        fail(
            f"{len(unreasoned)} baseline entr(y/ies) carry no reason:\n  "
            + "\n  ".join(unreasoned)
            + "\nAn exemption with no stated reason is unreviewed. Write why it is not "
            "exploitable, or do not accept it."
        )

    print(
        f"ok   opengrep baseline: {len(BASELINE)} accepted finding(s), every file "
        f"present, every entry carries a reason"
    )


def load_findings(path: Path) -> list[dict]:
    """Read `opengrep --json` output, failing loudly on anything unreadable.

    Every branch here exists because its failure mode otherwise looks like a
    clean scan.
    """
    if not path.is_file():
        fail(f"findings file {path} does not exist — nothing to compare against")

    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        fail(
            f"findings file {path} is empty. An empty file is not a clean scan; "
            "it is a scan whose output never arrived."
        )

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"findings file {path} is not valid JSON ({exc}) — cannot conclude anything")

    if not isinstance(doc, dict) or "results" not in doc:
        fail(
            f"findings file {path} has no 'results' key. The schema changed or this "
            "is not opengrep output, and a missing key would read as zero findings."
        )

    errors = doc.get("errors") or []
    blocking = [e for e in errors if str(e.get("level", "")).lower() in ("error", "fatal")]
    if blocking:
        detail = "; ".join(str(e.get("message", e))[:200] for e in blocking[:3])
        fail(
            f"the scan reported {len(blocking)} error(s), so its finding list is "
            f"incomplete: {detail}. A partial scan emits 'results: []' exactly like "
            "a clean one."
        )

    # FLOOR: prove the scan actually covered the tree. Absent or short means every
    # conclusion below is drawn from nothing.
    scanned = (doc.get("paths") or {}).get("scanned")
    if not isinstance(scanned, list) or not scanned:
        fail(
            "the findings file reports no scanned paths (paths.scanned absent or "
            "empty). A scan that covered nothing emits 'results: []' exactly like a "
            "clean tree, so this cannot be allowed to pass."
        )
    if len(scanned) < MIN_PATHS_SCANNED:
        fail(
            f"the scan covered only {len(scanned)} path(s), below the floor of "
            f"{MIN_PATHS_SCANNED}. The tree has far more than that, so the rule fetch "
            "or the walk failed and 'no new findings' would mean nothing."
        )

    # Not a failure, but coverage is not total and the log should say so. opengrep
    # cannot fully parse GitHub Actions expression syntax, so the workflow files
    # come back partially analyzed. Reported as a count so a jump is visible.
    partial = [e for e in errors if "PartialParsing" in str(e.get("type", ""))]
    version = doc.get("version") or "unknown"
    print(
        f"ok   scan coverage: {len(scanned)} paths scanned by opengrep {version}"
        + (f", {len(partial)} partial-parse warning(s)" if partial else "")
    )

    results = doc["results"]
    if not isinstance(results, list):
        fail("'results' is not a list — schema unreadable")
    return results


def check_findings(path: Path) -> None:
    results = load_findings(path)

    actual: dict[tuple[str, str], list[int]] = {}
    for r in results:
        rule = str(r.get("check_id", "")).strip()
        rel = str(r.get("path", "")).strip()
        if rel.startswith("./"):
            rel = rel[2:]
        if not rule or not rel:
            fail(f"a result is missing check_id or path: {json.dumps(r)[:200]}")
        line = (r.get("start") or {}).get("line")
        actual.setdefault((rule, rel), []).append(line)

    new = sorted(k for k in actual if k not in BASELINE)
    stale = sorted(k for k in BASELINE if k not in actual)

    for (rule, rel) in sorted(actual):
        if (rule, rel) in BASELINE:
            lines = ", ".join(str(x) for x in actual[(rule, rel)])
            print(f"ok   accepted: {rel}:{lines}  {rule}")

    if stale:
        # Not fatal: a finding disappearing is good news. But an accepted entry
        # that no longer fires is how the record drifts back to fiction, so it
        # is reported every run until someone deletes it.
        print(
            f"::warning::{len(stale)} baseline entr(y/ies) no longer fire and should "
            "be deleted so the record stays honest:"
        )
        for rule, rel in stale:
            print(f"  {rel}  {rule}")

    if new:
        print(f"::error::{len(new)} finding(s) not in the baseline:")
        for rule, rel in new:
            lines = ", ".join(str(x) for x in actual[(rule, rel)])
            print(f"  {rel}:{lines}  {rule}")
        print(
            "Review each one, then either fix it or add it to BASELINE in "
            "scripts/check_opengrep_baseline.py with a reason saying why it is not "
            "exploitable."
        )
        sys.exit(1)

    print(f"ok   {len(results)} finding(s), all accounted for in the baseline")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--findings",
        type=Path,
        help="path to `opengrep scan --json` output; compares it to BASELINE",
    )
    args = parser.parse_args()

    check_structure()
    if args.findings is not None:
        check_findings(args.findings)


if __name__ == "__main__":
    main()
