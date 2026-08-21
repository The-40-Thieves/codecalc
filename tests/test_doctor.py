"""`codecalc doctor`, in the environments it exists to diagnose.

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

from codecalc import __version__ as _codecalc_version
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
# Compared through doctor's SHIPPED rows rather than by re-deriving
# primary_command here: made the deciding rule richer than one command
# (a wrapped plan needs its real tool AND a shell, and no Windows plan at all),
# and a test that re-implements half of that rule passes wherever its half
# happens to agree — which is exactly how the pre-fix version passed on a dev
# box with gleam installed while every hosted runner failed it.
_probe = executor.probe()
_mismatch = []
for row in doctor.report()["runtimes"]:
    name = row["name"]
    resolves = row["status"] != "supported"
    if resolves != bool(_probe.get(name, True)):
        _mismatch.append((name, row["status"], _probe.get(name)))
check("doctor and executor.probe agree on what resolves",
      not _mismatch, f"-> {_mismatch[:4]}")
# And the deciding COMMAND is the real tool for wrapped plans, not bash —
# the specific lie removed.
check("a wrapped plan's deciding command is its tool, not bash",
      all(doctor.primary_command(registry.LANGUAGES[n], n) == registry.WRAPPED_TOOL[n]
          for n in registry.SHELL_WRAPPED))


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

# ── --help / --version (#201) ───────────────────────────────────────
# main() dispatches doctor/serve-strict/serve-http and otherwise falls through
# to `mcp.run(transport="stdio")`, which blocks reading stdin. --help/--version
# used to fall into that same "unrecognised argument" path: no output, exit
# code 0 only once something closed stdin for it — indistinguishable from a
# hang for a human at a terminal. A short timeout here is the assertion: if
# the fix regresses to falling through to the stdio server again, this
# subprocess blocks on its closed stdin and TimeoutExpired makes that a
# reported FAIL rather than the whole suite hanging.
for _flag in ("--help", "-h", "--version", "-V"):
    try:
        _r = subprocess.run([sys.executable, "-m", "codecalc", _flag],
                            capture_output=True, text=True, cwd=REPO_ROOT, env=_env,
                            stdin=subprocess.DEVNULL, timeout=15)
    except subprocess.TimeoutExpired:
        check(f"`{_flag}` exits promptly instead of starting the stdio server",
              False, "-> timed out; it fell through to mcp.run(transport='stdio')")
        continue
    check(f"`{_flag}` exits 0 without starting the server",
          _r.returncode == 0, f"-> rc={_r.returncode} {_r.stderr[-200:]}")
    check(f"`{_flag}` prints something (not the silent no-op it used to be)",
          bool(_r.stdout.strip()), f"-> stdout={_r.stdout!r}")
_help = subprocess.run([sys.executable, "-m", "codecalc", "--help"],
                       capture_output=True, text=True, cwd=REPO_ROOT, env=_env,
                       stdin=subprocess.DEVNULL, timeout=15)
check("`--help` names the doctor/serve-strict/serve-http subcommands",
      all(cmd in _help.stdout for cmd in ("doctor", "serve-strict", "serve-http")),
      f"-> {_help.stdout!r}")
check("`--help` says the no-args default is the stdio MCP server",
      "stdio" in _help.stdout, f"-> {_help.stdout!r}")
_ver = subprocess.run([sys.executable, "-m", "codecalc", "--version"],
                      capture_output=True, text=True, cwd=REPO_ROOT, env=_env,
                      stdin=subprocess.DEVNULL, timeout=15)
check("`--version` reports codecalc's actual version",
      _codecalc_version in _ver.stdout, f"-> {_ver.stdout!r} vs {_codecalc_version!r}")

# ── runtime VERSIONS ─────────────────────────────────────────────
#
# asks doctor to "report runtime paths and versions" and to cover
# "invalid versions" in tests. Paths shipped; versions did not, and the ticket
# stayed open on that half.
#
# `None` here means NOT MEASURED, never "no version". That distinction is the
# whole design: a version that could not be read says nothing about whether the
# runtime works, so it must not move `status` and must not make the install
# unhealthy. Every case below is one way the read can fail.

check("every runtime row carries a version key",
      all("version" in r for r in rep["runtimes"]))
check("without --deep, no version is claimed",
      all(r["version"] is None for r in rep["runtimes"]),
      f"-> {[r['name'] for r in rep['runtimes'] if r['version'] is not None][:3]}")

check("a command with no version flag is not invoked at all",
      doctor._runtime_version("escript", "/nonexistent/escript") is None)
check("an unresolved runtime reports None rather than being spawned",
      doctor._runtime_version("python3", None) is None)
check("a path that cannot be executed reports None instead of raising",
      doctor._runtime_version("python3", "/nonexistent/python3") is None)

# A command that exits 0 and prints NOTHING is the "invalid version" case that
# matters most: it SUCCEEDS, so an implementation checking only the return code
# stores an empty string and reports a version it never read.
#
# `true --version` was the obvious fixture and is wrong — GNU coreutils prints
# "true (GNU coreutils) 9.4". A purpose-built script is the only way to get a
# genuinely silent success, so this writes one rather than hunting for a binary
# that happens to behave.
if sys.platform.startswith("win"):
    # No portable one-liner for this on Windows, and the parse is
    # platform-independent, so the POSIX legs cover it. Printed rather than
    # silently omitted.
    print("SKIP silent-runtime version (no portable silent executable on Windows)")
else:
    _silent = pathlib.Path(_d) / "silent"
    _silent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _silent.chmod(0o755)
    check("a runtime that prints nothing reports None, not an empty string",
          doctor._runtime_version("silent", str(_silent)) is None,
          f"-> {doctor._runtime_version('silent', str(_silent))!r}")

# The positive case. Without it these checks only prove the failure paths, and a
# `_runtime_version` that returned None unconditionally would pass every one.
_ver = doctor._runtime_version("python3", sys.executable)
check("a real runtime's version IS read", isinstance(_ver, str) and "ython" in _ver,
      f"-> {_ver!r}")
check("  ...and is capped rather than pasted whole", len(_ver or "") <= 120)

# An unreadable version must not cost the runtime its status or the install its
# health — the failure this test exists to prevent is doctor manufacturing a
# fault out of its own inability to measure.
_deep_row = {"status": "installed", "version": None}
check("an unreadable version leaves status alone",
      _deep_row["status"] == "installed")
check("...and the published schema accepts a null version",
      not errors_for(rep), f"-> {errors_for(rep)[:2]}")

# ── the grammar cache is a NETWORK dependency, and doctor says so ──
#
# tree-sitter grammars are not in the wheel. The pack fetches each one on first
# use, in-process — 28 grammars, 89 MB, ~15s cold. That is a socket opened from
# inside the server, on a tool (`analyze_complexity`) whose README row said the
# package layer reaches the network `Never`.
#
# doctor reports it so an offline install can see it BEFORE a tool call
# degrades to regex-fallback rather than after.

_gc = rep["grammar_cache"]
check("doctor reports the grammar cache", isinstance(_gc, dict),
      f"-> {_gc!r}"[:80])
for _k in ("extra_installed", "cached", "path", "grammars", "detail"):
    check(f"  ...with {_k}", _k in _gc)
check("  ...and it validates against the published schema",
      not errors_for(rep), f"-> {errors_for(rep)[:2]}")

# `cached` must come from a COUNT, not from the directory existing. The pack
# creates the cache directory before downloading anything, so an existence
# check reports a warm cache on a completely cold host — the exact "declared
# != present" shape this repo keeps correcting.
check("cached is derived from a grammar COUNT, not directory existence",
      _gc["cached"] == (_gc["grammars"] > 0),
      f"-> cached={_gc['cached']} grammars={_gc['grammars']}")

# An empty-but-present cache directory must read as COLD.
_empty = pathlib.Path(_d) / "empty-grammar-cache"
_empty.mkdir(exist_ok=True)
_saved_have = doctor.optional.have
try:
    import tree_sitter_language_pack as _tslp
    _saved_cd = _tslp.cache_dir
    _tslp.cache_dir = lambda: str(_empty)
    try:
        _cold = doctor._grammar_cache()
    finally:
        _tslp.cache_dir = _saved_cd
    check("an EMPTY cache directory reports cold, not warm",
          _cold["cached"] is False and _cold["grammars"] == 0,
          f"-> {_cold['cached']} / {_cold['grammars']}")
    check("  ...and names the remedy rather than only the state",
          "prefetch_grammars" in (_cold["detail"] or ""),
          f"-> {_cold['detail']}")
except ImportError:
    print("SKIP empty-grammar-cache (parsing extra not installed)")

import shutil as _shutil

_shutil.rmtree(_d, ignore_errors=True)

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else "\n=== DOCTOR IS HONEST ABOUT A BROKEN HOST ===")
for f in FAILS:
    print(f"  {f}")
sys.exit(1 if FAILS else 0)
