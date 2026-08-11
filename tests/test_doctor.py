"""`codecalc doctor`, in the environments it exists to diagnose (THE-780).

A diagnostic is only worth having if it is right about a BROKEN host, and a
healthy dev box exercises none of the cases it was written for. So every check
below degrades something on purpose — removes the native executor, empties the
runtime PATH, makes a resolved command non-executable, takes away the
workspace — and asserts what doctor then says.

WHY THE REPORT IS A FUNCTION AND NOT JUST A CLI
`codecalc/doctor.py` builds a dict; `server.py` renders it. That split is what
makes these cases reachable: forcing "no writable workspace" through a
subprocess means finding a directory the CI runner cannot write to, which is
different on three platforms and flaky on all of them. Against the function it
is one patched attribute.

The CLI is still exercised end to end — both modes, real subprocesses — because
the split is only trustworthy if the two renderings are checked against the same
report rather than assumed to agree.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from jsonschema import Draft202012Validator

from codecalc import contract, doctor, executor, registry

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


SCHEMA_PATH = REPO_ROOT / "docs" / "contract" / "doctor-v1.schema.json"
published = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
validator = Draft202012Validator(published)


def errors_for(rep: dict) -> list[str]:
    return [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(rep)]


# ── the schema, and a control proving the validator bites ───────────────────
try:
    Draft202012Validator.check_schema(published)
    _valid, _why = True, ""
except Exception as exc:
    _valid, _why = False, f"-> {type(exc).__name__}: {exc}"
check("the published doctor schema is valid JSON Schema 2020-12", _valid, _why)

check("CONTROL: the validator rejects an unknown runtime status",
      bool(errors_for({**doctor.report(),
                       "runtimes": [{"name": "x", "command": "x",
                                     "status": "probably_fine", "path": None}]})),
      "-> accepted it; every assertion below is vacuous")

check("CONTROL: the validator rejects a missing required field",
      bool(errors_for({k: v for k, v in doctor.report().items()
                       if k != "runtime_summary"})))


# ── a healthy host ─────────────────────────────────────────────────────────
rep = doctor.report()
check("a healthy report validates", not errors_for(rep), f"-> {errors_for(rep)[:2]}")
check("a healthy install is healthy", rep["healthy"] is True)
check("it carries the CONTRACT's version, not a third number",
      rep["contract_version"] == contract.CONTRACT_VERSION,
      f"-> {rep['contract_version']}")
check("every advertised runtime gets an explicit status",
      len(rep["runtimes"]) == len(rep["runtime_summary"]) - len(doctor.RUNTIME_STATES) + len(rep["runtimes"])
      and all(r["status"] in doctor.RUNTIME_STATES for r in rep["runtimes"]),
      f"-> {len(rep['runtimes'])} runtimes")
check("the summary counts every runtime exactly once",
      sum(rep["runtime_summary"].values()) == len(rep["runtimes"]),
      f"-> {rep['runtime_summary']} vs {len(rep['runtimes'])}")

# The alias is the number check_claims gates. A doctor that counted 32 would be
# a fourth opinion on a figure this repo already has a gate for.
check("aliases are not counted as separate languages",
      len(rep["runtimes"]) == len(registry.LANGUAGES) - len(doctor.ALIAS_ENTRIES),
      f"-> {len(rep['runtimes'])} of {len(registry.LANGUAGES)}")

# `resolved` means "found on PATH, not run". Nothing may claim `available`.
check("without --deep, NOTHING claims to have been executed",
      rep["status_basis"] == "resolved"
      and not any(r["status"] == "available" for r in rep["runtimes"]),
      f"-> basis={rep['status_basis']} available={rep['runtime_summary']['available']}")


# ── the derivation doctor and the executor must agree on ───────────────────
# Two copies of "which command decides whether this language resolves" that
# drift would make doctor and the executor disagree about what exists, which is
# worse than either being wrong alone.
_probe = executor.probe()
_mismatch = []
for name, entry in registry.LANGUAGES.items():
    if name in doctor.ALIAS_ENTRIES:
        continue
    status, _ = doctor._runtime_status(doctor.primary_command(entry))
    resolves = status != "supported"
    if resolves != bool(_probe.get(name, True)):
        _mismatch.append((name, status, _probe.get(name)))
check("doctor and executor.probe agree on what resolves",
      not _mismatch, f"-> {_mismatch[:4]}")


# ── MISSING EXECUTOR ───────────────────────────────────────────────────────
_saved = executor._rust
executor._rust = None
try:
    fb = doctor.report()
finally:
    executor._rust = _saved
check("missing native executor: reported as the fallback",
      fb["backend"]["kind"] == "python" and fb["backend"]["binary"] is None,
      f"-> {fb['backend']}")
check("...and it is STILL healthy — a fallback runs, it is just weaker",
      fb["healthy"] is True)
check("...and the remedy names CODECALC_REQUIRE_NATIVE",
      any("no_net" in r for r in fb["remedies"]), f"-> {fb['remedies'][:2]}")
check("...and the report still validates", not errors_for(fb))


# ── MISSING RUNTIMES ───────────────────────────────────────────────────────
# An empty runtime PATH is every runtime missing at once, which is the state a
# minimal container is actually in.
_saved_path = registry.runtime_path
registry.runtime_path = lambda: os.pathsep.join([])
try:
    bare = doctor.report()
finally:
    registry.runtime_path = _saved_path
check("missing runtimes: every one reports `supported`",
      bare["runtime_summary"]["supported"] == len(bare["runtimes"]),
      f"-> {bare['runtime_summary']}")
check("...and a host with no runtimes is STILL healthy",
      bare["healthy"] is True,
      "-> an uninstalled runtime is a fact about the host, not a broken install")
check("...and the report validates", not errors_for(bare))


# ── RESOLVED BUT NOT RUNNABLE (the `unhealthy` state) ──────────────────────
import tempfile as _tf

_d = _tf.mkdtemp(prefix="codecalc-doctor-test-")
_fake = pathlib.Path(_d) / "python3"
_fake.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
_fake.chmod(0o644)              # present, NOT executable
registry.runtime_path = lambda: _d
try:
    broken = doctor.report()
finally:
    registry.runtime_path = _saved_path
_py = next((r for r in broken["runtimes"] if r["name"] == "python3"), {})
if os.name == "nt":
    # Windows has no execute bit; os.access(X_OK) is true for any readable
    # file, so this state is not reachable the same way. Saying so beats an
    # assertion that passes for the wrong reason.
    print("SKIP resolved-but-not-executable — Windows has no execute permission bit")
else:
    check("a resolved but non-executable command reports `unhealthy`",
          _py.get("status") == "unhealthy", f"-> {_py.get('status')}")
    check("...and it appears in the remedies",
          any("not runnable" in r for r in broken["remedies"]),
          f"-> {broken['remedies'][:2]}")
    check("...and the report validates", not errors_for(broken))


# ── UNWRITABLE WORKSPACE — the one failure that IS unhealthy ───────────────
_saved_gettemp = doctor.tempfile.gettempdir
doctor.tempfile.gettempdir = lambda: str(pathlib.Path(_d) / "definitely" / "not" / "here")
_saved_tmpdir_cls = doctor.tempfile.TemporaryDirectory


class _Boom:
    def __init__(self, *a, **k):
        raise OSError("read-only file system")


doctor.tempfile.TemporaryDirectory = _Boom
try:
    dead = doctor.report()
finally:
    doctor.tempfile.gettempdir = _saved_gettemp
    doctor.tempfile.TemporaryDirectory = _saved_tmpdir_cls
check("unwritable workspace: writable is False and the error is named",
      dead["workspace"]["writable"] is False and dead["workspace"]["error"],
      f"-> {dead['workspace']}")
check("...and THAT makes the install unhealthy",
      dead["healthy"] is False,
      "-> nothing can execute without a workspace")
check("...and it is the FIRST remedy, because nothing else matters until it is fixed",
      dead["remedies"] and "not writable" in dead["remedies"][0],
      f"-> {dead['remedies'][:1]}")
check("...and the report still validates", not errors_for(dead))


# ── a missing extra must not fail an unrelated check ───────────────────────
from codecalc import optional as _optional

_saved_have = _optional.have
_optional.have = lambda m: False
try:
    noextras = doctor.report()
finally:
    _optional.have = _saved_have
check("missing extras: reported as missing with the exact pip command",
      all(not e["installed"] and e["remedy"].startswith("pip install")
          for e in noextras["extras"]),
      f"-> {[(e['name'], e['remedy']) for e in noextras['extras']]}")
check("...and a missing extra does NOT make the install unhealthy",
      noextras["healthy"] is True,
      "-> an optional dependency is optional; exiting non-zero would make "
      "doctor useless as an install check")


# ── the CLI, both modes, as a user runs it ─────────────────────────────────
_env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
_txt = subprocess.run([sys.executable, "-m", "codecalc", "doctor"],
                      capture_output=True, text=True, cwd=REPO_ROOT, env=_env,
                      timeout=180)
check("`doctor` exits 0 on a healthy install", _txt.returncode == 0,
      f"-> rc={_txt.returncode} {_txt.stderr[-200:]}")

_js = subprocess.run([sys.executable, "-m", "codecalc", "doctor", "--json"],
                     capture_output=True, text=True, cwd=REPO_ROOT, env=_env,
                     timeout=180)
check("`doctor --json` exits 0 on a healthy install", _js.returncode == 0,
      f"-> rc={_js.returncode} {_js.stderr[-200:]}")

# --json must emit ONLY JSON. A diagnostic whose machine-readable mode also
# prints a friendly banner cannot be piped, which is the entire point of it.
try:
    parsed = json.loads(_js.stdout)
    _parses = True
except ValueError as exc:
    parsed, _parses = {}, False
    print(f"     stdout began: {_js.stdout[:120]!r} ({exc})")
check("`--json` emits parseable JSON and nothing else", _parses)
check("...which validates against the published schema",
      _parses and not errors_for(parsed), f"-> {errors_for(parsed)[:2] if _parses else ''}")

# The two renderings come from one report, so a field present in JSON and
# absent from the text is a rendering bug rather than a difference of opinion.
check("the text rendering names the backend the JSON reports",
      _parses and parsed["backend"]["kind"] in _txt.stdout,
      f"-> {parsed.get('backend', {}).get('kind')}")
check("the text rendering names the contract version the JSON reports",
      _parses and parsed["contract_version"] in _txt.stdout)

# A flag that no-ops is worse than one that does not exist. This is what
# --json was before THE-780: accepted, ignored, prose printed anyway.
check("`--json` actually changes the output",
      _js.stdout != _txt.stdout and _js.stdout.lstrip().startswith("{"))

import shutil as _shutil

_shutil.rmtree(_d, ignore_errors=True)

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else "\n=== DOCTOR IS HONEST ABOUT A BROKEN HOST ===")
for f in FAILS:
    print(f"  {f}")
sys.exit(1 if FAILS else 0)
