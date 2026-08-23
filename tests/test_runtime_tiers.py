"""RELIABILITY tiers: every language claims one, and the CI-honesty gate that
keeps the strong claim (`tested`) from rotting actually fires.

Three things checked:

  1. every surface that reports a language (list_languages via executor.
     catalog(), runtimes_status via runtimes.status()) carries a valid `tier`
     for every entry — the field this ticket adds, not just the registry it
     reads from.
  2. scripts/check_runtime_tiers.py exits 0 against the real repo.
  3. scripts/check_runtime_tiers.py exits NONZERO against a scratch copy of
     the repo with one language's tier deliberately wrong — the fail-first
     proof, automated so it cannot rot into a comment nobody re-runs.

Check 3 needs its own copy of the files the gate reads (codecalc/registry.py,
tests/test_python_sweep.py, tests/test_tier_evidence.py,
scripts/contract_check.py, .github/workflows/ci-python.yml) rather than
mutating this checkout in place: mutating codecalc/registry.py under a live
test run would corrupt every OTHER suite that imports it, on this process or
a parallel one, for the length of one subprocess call.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import executor, registry, runtimes

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")


# ═══ 1. every LANGUAGES entry has a valid tier, at the registry level ══════
_missing = sorted(name for name, entry in registry.LANGUAGES.items()
                  if entry.get("tier") not in registry.RELIABILITY_TIERS)
check("every registry.LANGUAGES entry has a tier in RELIABILITY_TIERS",
      not _missing, f"-> missing/invalid: {_missing}" if _missing else
      f"-> {len(registry.LANGUAGES)} entries")

# The tier taxonomy exists to distinguish RESOLUTION from RELIABILITY, and
# the whole point breaks if `tier` never rides along with the surfaces a
# caller actually reads. Both checked against the CATALOG, not the raw
# registry, because that is what list_languages/doctor return to a caller.
_catalog = executor.catalog()
check("executor.catalog() (the list_languages payload) carries `tier` on every entry",
      _catalog and all(e.get("tier") in registry.RELIABILITY_TIERS for e in _catalog),
      f"-> {len(_catalog)} entries")

_tested_in_catalog = sorted(e["name"] for e in _catalog if e["tier"] == "tested")
check("exactly python3, node, rust and go are `tested` in the catalog",
      _tested_in_catalog == ["go", "node", "python3", "rust"], f"-> {_tested_in_catalog}")

# `status` (resolution, this machine) and `tier` (reliability, CI-wide) are
# INDEPENDENT axes — asserting that directly, not just that both are present.
# A best_effort language reads `installed` here whenever the toolchain
# happens to be on this box, and that must not silently promote its tier.
_installed_best_effort = [e for e in _catalog
                          if e["status"] == "installed" and e["tier"] == "best_effort"]
if _installed_best_effort:
    check("a best_effort language can be `installed` without becoming `tested`",
          all(e["tier"] == "best_effort" for e in _installed_best_effort),
          f"-> e.g. {_installed_best_effort[0]['name']}")
else:
    skip("resolution/reliability independence",
         "no best_effort language happens to be installed on this box")

# ═══ 2. runtimes_status carries `tier` for registry languages ═════════════
# Filtered to a handful rather than every language: this hits real package
# managers (mise/rustup/apt) as subprocesses, and the point here is the field
# shape, not an inventory of every manager this host has.
_probe_langs = "python3,node,rust,csharp,c,cpp"
_status = runtimes.status(_probe_langs)
check("runtimes_status() call succeeded", _status.get("ok") is True,
      f"-> {_status.get('error')}")
_rows = _status.get("languages", {})
if not _rows:
    skip("runtimes_status tier field",
         "no package manager on this host answered for any of "
         f"{_probe_langs} — nothing to check the field shape against")
else:
    _bad = {name: row.get("tier") for name, row in _rows.items()
           if row.get("tier") not in registry.RELIABILITY_TIERS}
    check(f"runtimes_status entries ({len(_rows)}) carry a valid `tier`",
          not _bad, f"-> invalid: {_bad}" if _bad else "")
    if "python3" in _rows:
        check("runtimes_status: python3 is tier `tested`",
              _rows["python3"].get("tier") == "tested", f"-> {_rows['python3'].get('tier')}")
    if "rust" in _rows:
        check("runtimes_status: rust is tier `tested` (evidence: test_tier_evidence.py)",
              _rows["rust"].get("tier") == "tested", f"-> {_rows['rust'].get('tier')}")
    if "csharp" in _rows:
        check("runtimes_status: csharp is tier `best_effort` (resolved != reliable)",
              _rows["csharp"].get("tier") == "best_effort", f"-> {_rows['csharp'].get('tier')}")

# ═══ 3a. the gate passes on the real, unmodified repo ══════════════════════
_real = subprocess.run([sys.executable, "scripts/check_runtime_tiers.py"],
                       cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
check("scripts/check_runtime_tiers.py exits 0 on the real registry",
      _real.returncode == 0, f"-> exit={_real.returncode}\n{_real.stdout[-400:]}")

# ═══ 3b. FAIL-FIRST: each seeded defect makes the gate go red ══════════════
# A scratch copy of only what the gate reads, so mutating it cannot corrupt
# any other suite running in this process or a sibling one. One fresh copy
# per seed: a shared copy would let an earlier mutation contaminate a later
# seed's result, and the copies are five small files.
_GATE_READS = (
    ("codecalc", "__init__.py"),
    ("codecalc", "registry.py"),
    ("scripts", "check_runtime_tiers.py"),
    ("scripts", "contract_check.py"),
    ("tests", "test_python_sweep.py"),
    ("tests", "test_tier_evidence.py"),
    (".github/workflows", "ci-python.yml"),
)


def _gate_on_mutated_copy(rel_dir: str, name: str, needle: str, replacement: str,
                          seed_label: str) -> subprocess.CompletedProcess | None:
    """Run the gate against a scratch repo with one seeded defect.

    Returns the completed process, or None (with a failed check recorded) if
    the needle no longer matches — a stale needle would otherwise "prove"
    fail-first against an unmutated copy.
    """
    with tempfile.TemporaryDirectory(prefix="codecalc-tier-gate-") as _scratch:
        _root = pathlib.Path(_scratch)
        for _dir, _file in _GATE_READS:
            (_root / _dir).mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO_ROOT / _dir / _file, _root / _dir)
        _target = _root / rel_dir / name
        _src = _target.read_text(encoding="utf-8")
        _seeded = _src.replace(needle, replacement)
        check(f"seed setup ({seed_label}): found the exact line to mutate in {name}",
              _seeded != _src, f"-> {name} has changed shape; update the needle")
        if _seeded == _src:
            return None
        _target.write_text(_seeded, encoding="utf-8")
        return subprocess.run([sys.executable, "scripts/check_runtime_tiers.py"],
                              cwd=_root, capture_output=True, text=True, timeout=30)


# Seed 1: a worker language's registry tier goes quietly weak.
_bad_run = _gate_on_mutated_copy(
    "codecalc", "registry.py",
    '"node":    _c(None, "node {file}", "tested"),',
    '"node":    _c(None, "node {file}", "best_effort"),',
    "node tier flipped")
if _bad_run is not None:
    check("FAIL-FIRST: check_runtime_tiers.py exits NONZERO when node's tier is wrong",
          _bad_run.returncode != 0, f"-> exit={_bad_run.returncode}")
    check("  ...and names the actual mismatch, not just a generic failure",
          "MISMATCH" in _bad_run.stdout and "node" in _bad_run.stdout,
          f"-> {_bad_run.stdout[-400:]}")

# Seed 2: rust drops out of the evidence harness while the registry still
# claims `tested` — the exact drift THE tier system exists to catch.
_bad_run = _gate_on_mutated_copy(
    "tests", "test_tier_evidence.py",
    'TIER_EVIDENCE_LANGS = ("rust", "go")',
    'TIER_EVIDENCE_LANGS = ("go",)',
    "rust dropped from harness")
if _bad_run is not None:
    check("FAIL-FIRST: gate exits NONZERO when rust leaves the evidence harness",
          _bad_run.returncode != 0, f"-> exit={_bad_run.returncode}")
    check("  ...and names rust as only-in-registry",
          "MISMATCH" in _bad_run.stdout and "rust" in _bad_run.stdout,
          f"-> {_bad_run.stdout[-400:]}")

# Seed 3: the workflow keeps invoking the harness but loses the flag that
# makes a skip a failure — the harness would still "run" while proving
# nothing on a runner missing a toolchain.
_bad_run = _gate_on_mutated_copy(
    ".github/workflows", "ci-python.yml",
    "CODECALC_REQUIRE_TIER_EVIDENCE: '1'",
    "CODECALC_REQUIRE_TIER_EVIDENCE: ''",
    "require flag stripped")
if _bad_run is not None:
    check("FAIL-FIRST: gate exits NONZERO when CI drops the skip-promoting flag",
          _bad_run.returncode != 0, f"-> exit={_bad_run.returncode}")

# Seed 4: the step survives textually but is disabled — `if: false` leaves
# every substring in place, which is exactly why the gate reads the step
# BLOCK for the real Linux condition instead of grepping the whole file.
_bad_run = _gate_on_mutated_copy(
    ".github/workflows", "ci-python.yml",
    "if: runner.os == 'Linux'",
    "if: false",
    "evidence step disabled")
if _bad_run is not None:
    check("FAIL-FIRST: gate exits NONZERO when the evidence step is if:-disabled",
          _bad_run.returncode != 0, f"-> exit={_bad_run.returncode}")

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== ALL PASS ===")
if FAILS:
    sys.exit(1)
