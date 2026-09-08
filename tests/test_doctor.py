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
    # python3 is `tested` tier — codecalc's own CI genuinely executes and
    # checks it on every PR — so a RESOLVED-but-broken python3 is not an
    # ordinary "toolchain not installed" host fact, it is codecalc's own
    # advertised guarantee failing, and `healthy` says so.
    check("...and a broken `tested`-tier runtime makes the install unhealthy",
          broken["healthy"] is False,
          "-> python3 is `tested`; an uninstalled BEST_EFFORT runtime must "
          "not do this, only a resolved-and-broken TESTED one")


import shutil as _shutil_fixtures

# ── A FAILED VERSION PROBE MUST NOT BE STORED AS A VERSION (#279) ──────────
#
# Reproduced against `/usr/bin/java` on a macOS host with no JDK: Apple ships
# a stub there that resolves on PATH and exits non-zero printing "The
# operation couldn't be completed. Unable to locate a Java Runtime." on
# stderr. The OLD `_runtime_version` read that as a perfectly good first
# non-empty line and RETURNED IT — `status` stayed `installed`, `unhealthy`
# never counted it, and `healthy` never noticed. This fixture reproduces the
# exact shape (a fake `java` that exits 1 printing that text) without needing
# a Mac: any command that resolves and answers `--deep`'s version probe with
# a nonzero exit is the same defect.
_apple_stub_msg = "The operation couldn't be completed. Unable to locate a Java Runtime."
_java_broken_dir = _tf.mkdtemp(prefix="codecalc-doctor-test-")
_fake_java = pathlib.Path(_java_broken_dir) / "java"
_fake_java.write_text(f'#!/bin/sh\necho "{_apple_stub_msg}" >&2\nexit 1\n', encoding="utf-8")
_fake_java.chmod(0o755)
registry.runtime_path = lambda: _java_broken_dir
try:
    java_broken = doctor.report(deep=True)
finally:
    registry.runtime_path = _saved_path
_java_row = next((r for r in java_broken["runtimes"] if r["name"] == "java"), {})
if os.name == "nt":
    print("SKIP failed-version-probe (needs a POSIX shell script fixture)")
else:
    check("a runtime whose --deep version probe exits non-zero is `unhealthy`",
          _java_row.get("status") == "unhealthy", f"-> {_java_row.get('status')}")
    check("...never `installed` (the pre-fix status for this exact host)",
          _java_row.get("status") != "installed")
    check("...and `version` is None, NEVER the probe's error text",
          _java_row.get("version") is None, f"-> {_java_row.get('version')!r}")
    check("...and the failure lands in `probe_error`, not manufactured elsewhere",
          _java_row.get("probe_error") is not None
          and _apple_stub_msg in _java_row["probe_error"],
          f"-> {_java_row.get('probe_error')!r}")
    check("...and it counts in runtime_summary.unhealthy",
          java_broken["runtime_summary"]["unhealthy"] >= 1,
          f"-> {java_broken['runtime_summary']}")
    # java is `best_effort` — nothing CI-checked ever promised it works —
    # so this must NOT be the thing that fails an install check. Only a
    # `tested`-tier runtime resolving-then-breaking does that (see above).
    check("...but java is `best_effort`, so `healthy` is UNCHANGED by it",
          java_broken["healthy"] is True,
          "-> a best_effort runtime's own probe failing is not codecalc's "
          "advertised guarantee breaking")
    check("...and the remedy names java specifically, not just \"unhealthy\"",
          any("java" in r and "install a working" in r for r in java_broken["remedies"]),
          f"-> {java_broken['remedies']}")
    check("...and the report still validates", not errors_for(java_broken))


# ── KOTLIN: RESOLUTION MUST NEED BOTH kotlinc AND java (#280) ──────────────
#
# kotlin used to be registered with its resolving `command` set to `java`
# (the binary its `run` step invokes), so it read `installed` on any host
# with a JRE and no Kotlin toolchain at all — `execute_code(language=
# "kotlin")` then failed at spawn. Two fixtures: java-without-kotlinc (the
# exact reported repro) and kotlinc-without-java (the new secondary-tool
# check this fix adds, otherwise unexercised).
_kotlin_dir = _tf.mkdtemp(prefix="codecalc-doctor-test-")
_fake_java2 = pathlib.Path(_kotlin_dir) / "java"
_fake_java2.write_text("#!/bin/sh\necho ok\nexit 0\n", encoding="utf-8")
_fake_java2.chmod(0o755)
registry.runtime_path = lambda: _kotlin_dir
try:
    kotlin_no_kotlinc = doctor.report()
finally:
    registry.runtime_path = _saved_path
_kt = next((r for r in kotlin_no_kotlinc["runtimes"] if r["name"] == "kotlin"), {})
if os.name == "nt":
    print("SKIP kotlin two-tool resolution (needs a POSIX shell script fixture)")
else:
    check("kotlin's deciding command is kotlinc, never java",
          _kt.get("command") == "kotlinc", f"-> {_kt.get('command')!r}")
    check("java alone (no kotlinc) does NOT report kotlin installed",
          _kt.get("status") != "installed", f"-> {_kt.get('status')!r}")

    _kotlin_dir2 = _tf.mkdtemp(prefix="codecalc-doctor-test-")
    _fake_kotlinc = pathlib.Path(_kotlin_dir2) / "kotlinc"
    _fake_kotlinc.write_text("#!/bin/sh\necho ok\nexit 0\n", encoding="utf-8")
    _fake_kotlinc.chmod(0o755)
    registry.runtime_path = lambda: _kotlin_dir2
    try:
        kotlin_no_java = doctor.report()
    finally:
        registry.runtime_path = _saved_path
    _kt2 = next((r for r in kotlin_no_java["runtimes"] if r["name"] == "kotlin"), {})
    check("kotlinc alone (no java) ALSO does not report kotlin installed",
          _kt2.get("status") != "installed", f"-> {_kt2.get('status')!r}")
    check("...and the missing half is named by its actual binary",
          "java" in (_kt2.get("detail") or ""), f"-> {_kt2.get('detail')!r}")
    check("...and the remedy names kotlin's missing half too",
          any("kotlin" in r and "java" in r for r in kotlin_no_java["remedies"]),
          f"-> {kotlin_no_java['remedies']}")
    _shutil_fixtures.rmtree(_kotlin_dir2, ignore_errors=True)

_shutil_fixtures.rmtree(_java_broken_dir, ignore_errors=True)
_shutil_fixtures.rmtree(_kotlin_dir, ignore_errors=True)


# ── A NONZERO EXIT ON AN UNTESTED FLAG IS "UNKNOWN", NEVER "BROKEN" ────────
#
# A shipped version of the #279 fix trusted ANY nonzero exit from `<command>
# --version` as evidence of brokenness. `--version` is a GNU convention, not
# a universal one: `go --version` exits 2 ("flag provided but not defined" —
# `go version` is correct), `lua --version` exits 1 (`-v` is correct), `zig
# --version` exits 1 (`zig version` is correct). Since go is `tested` tier,
# that shipped version reported a perfectly WORKING go install `unhealthy`
# and flipped `healthy: false` on any host with these three toolchains —
# a regression worse than the bug it fixed, caught by adversarial review, not
# by a test, because no test ran `_probe_version` against a command using an
# UNCONFIRMED flag. `_probe_version` returns `(version, probe_error,
# hard_failure, timed_out, probe_ms)`; a nonzero exit is `hard_failure=False`
# always, and the caller (report(), tested below) trusts a `False` only when
# `_VERSION_FLAG` has an explicit entry for that command.
_v, _e, _h, _t, _ms = doctor._probe_version("no-such-fake-command-xyz", None)
check("no path at all is not measured, not a failure",
      _v is None and _e is None and _h is False and _t is False and _ms is None,
      f"-> {(_v, _e, _h, _t, _ms)}")
_v, _e, _h, _t, _ms = doctor._probe_version("python3", "/nonexistent/python3")
check("a resolved-but-unspawnable path is a HARD failure, not a timeout",
      _v is None and _e is not None and _h is True and _t is False,
      f"-> {(_v, _e, _h, _t)}")

_untrusted_dir = _tf.mkdtemp(prefix="codecalc-doctor-test-")
_fake_untrusted = pathlib.Path(_untrusted_dir) / "fakelang"
_fake_untrusted.write_text(
    "#!/bin/sh\necho \"usage: fakelang [command]\" >&2\nexit 1\n", encoding="utf-8")
_fake_untrusted.chmod(0o755)
if os.name != "nt":
    assert "fakelang" not in doctor._VERSION_FLAG, "fixture invalid: pick a name not audited"
    _v, _e, _h, _t, _ms = doctor._probe_version("fakelang", str(_fake_untrusted))
    check("a command on the untested `--version` default: nonzero exit is a "
          "SOFT failure, never hard, never a timeout",
          _v is None and _e is not None and _h is False and _t is False,
          f"-> {(_v, _e, _h, _t)}")

# ── go / lua / zig: the exact three the regression broke ───────────────────
_gzl_dir = pathlib.Path(_tf.mkdtemp(prefix="codecalc-doctor-test-"))


def _write_fixture(name: str, script: str) -> None:
    p = _gzl_dir / name
    p.write_text(script, encoding="utf-8")
    p.chmod(0o755)


if os.name == "nt":
    print("SKIP go/lua/zig non-GNU version-flag fixtures (needs a POSIX shell script)")
else:
    # go: `version` is a SUBCOMMAND (no dashes); `--version` is an unknown flag.
    _write_fixture("go", (
        "#!/bin/sh\n"
        'if [ "$1" = "version" ]; then echo "go version go1.99.0 linux/arm64"; exit 0; fi\n'
        'echo "flag provided but not defined: -version" >&2\nexit 2\n'))
    # zig: same subcommand shape as go.
    _write_fixture("zig", (
        "#!/bin/sh\n"
        'if [ "$1" = "version" ]; then echo "0.99.0"; exit 0; fi\n'
        'echo "info: Usage: zig [command] [options]" >&2\nexit 1\n'))
    # lua: `-v` is correct; also doubles as the hello-world RUN step (lua is
    # in `_HELLO`), so anything but `-v`/`--version` prints "codecalc" and
    # exits 0 — a fake interpreter, not just a fake version responder.
    _write_fixture("lua", (
        "#!/bin/sh\n"
        'if [ "$1" = "-v" ]; then echo "Lua 5.4.6  Copyright (C) 1994-2023 Lua.org, PUC-Rio"; exit 0; fi\n'
        'if [ "$1" = "--version" ]; then echo "lua: unrecognized option --version" >&2; exit 1; fi\n'
        'echo codecalc\nexit 0\n'))

    # The ENV VAR, not the `registry.runtime_path` monkeypatch used
    # elsewhere in this file: lua is in `_HELLO`, so its check spawns a real
    # `execute_code` run, which on the Rust backend shells out to the
    # `codecalc-exec` binary as a SEPARATE process — one that reads
    # `CODECALC_RUNTIME_PATH` from ITS OWN inherited environment, never from
    # this Python process's `registry.runtime_path` attribute. Only the env
    # var reaches both the in-process version probe AND that child, on
    # either backend.
    _saved_runtime_path_env = os.environ.get("CODECALC_RUNTIME_PATH")
    os.environ["CODECALC_RUNTIME_PATH"] = str(_gzl_dir)
    try:
        gzl = doctor.report(deep=True)
    finally:
        if _saved_runtime_path_env is None:
            os.environ.pop("CODECALC_RUNTIME_PATH", None)
        else:
            os.environ["CODECALC_RUNTIME_PATH"] = _saved_runtime_path_env
    _rows = {r["name"]: r for r in gzl["runtimes"]}

    check("go (non-GNU --version, correct flag confirmed) is NOT unhealthy",
          _rows["go"]["status"] not in ("unhealthy", "supported"),
          f"-> {_rows['go']['status']}")
    check("  ...and its real version WAS read via the corrected flag",
          _rows["go"].get("version") == "go version go1.99.0 linux/arm64",
          f"-> {_rows['go'].get('version')!r}")
    check("zig (non-GNU --version, correct flag confirmed) is NOT unhealthy",
          _rows["zig"]["status"] not in ("unhealthy", "supported"),
          f"-> {_rows['zig']['status']}")
    check("  ...and its real version WAS read via the corrected flag",
          _rows["zig"].get("version") == "0.99.0", f"-> {_rows['zig'].get('version')!r}")
    check("lua: the hello-world run is the arbiter, not the version flag",
          _rows["lua"]["status"] == "available", f"-> {_rows['lua']['status']}")
    check("  ...and lua's own version WAS read via the corrected `-v` flag",
          _rows["lua"].get("version", "").startswith("Lua 5.4.6"),
          f"-> {_rows['lua'].get('version')!r}")
    check("go/lua/zig being merely resolved-but-fake does not cost `healthy` "
          "(they are best_effort; only a broken TESTED runtime would)",
          gzl["healthy"] is True, f"-> {gzl['healthy']}")
    check("...and the report validates", not errors_for(gzl))

_shutil_fixtures.rmtree(_untrusted_dir, ignore_errors=True)
_shutil_fixtures.rmtree(_gzl_dir, ignore_errors=True)


# ── A VERSION-PROBE TIMEOUT MUST NOT DEMOTE A WORKING RUNTIME ──────────────
#
# Reproduced live, three times, on 2026-09-08: a cold `windows-latest`
# runner's FIRST `rustc --version` goes through the rustup proxy — a tiny
# arg-forwarding shim that has to locate and re-exec the real toolchain
# component before it can answer anything — and exceeded the (then
# unconditional) probe deadline. The shipped code treated that exactly like
# a spawn failure: `hard_failure=True` regardless of WHY the probe never
# answered, which demoted a perfectly working `tested`-tier rust to
# `unhealthy` and flipped `healthy` false on every hit, main included.
# Confirmed failing this way against this exact fixture before the fix
# (`status: unhealthy`, `healthy: False`) — see the PR body for the
# transcript; the two checks below are what makes it impossible to
# regress silently.
#
# `_PROBE_TIMEOUT_S`/`_DEFAULT_PROBE_TIMEOUT_S` are monkeypatched down to
# keep these fixtures fast — a 1-second deadline rather than a real
# 10-25s one. That changes how LONG the fixture takes, never WHAT it
# proves: `_probe_version` reads both constants fresh on every call, so a
# short deadline exercises the exact same retry-then-report code path as
# the real ones.
#
# The fake binaries below block with an UNCONDITIONAL `while true; do :;
# done` rather than `sleep N` for a fixed N: a fixed sleep only reproduces
# a timeout if it reliably outlasts whatever deadline this run patched in,
# and a macOS CI run of an earlier version of this fixture (`sleep 5`
# against a 1s deadline) answered WITHIN the deadline anyway — the process
# scheduling/signal-delivery margin on that runner was enough to swallow a
# fixed multiple that this file's own Linux/mise runs could not reproduce.
# A busy-loop cannot ever finish on its own before the timeout regardless
# of platform: the ONLY way it stops is `subprocess.run`'s own SIGKILL on
# the deadline it enforces, which is what actually needs proving here.
if os.name == "nt":
    print("SKIP version-probe-timeout fixtures (needs a POSIX shell script)")
else:
    _saved_probe_timeout = dict(doctor._PROBE_TIMEOUT_S)
    _saved_default_timeout = doctor._DEFAULT_PROBE_TIMEOUT_S
    doctor._PROBE_TIMEOUT_S = {**_saved_probe_timeout, "rustc": 1.0}
    doctor._DEFAULT_PROBE_TIMEOUT_S = 1.0

    _slow_dir = _tf.mkdtemp(prefix="codecalc-doctor-test-")
    _fake_slow_rustc = pathlib.Path(_slow_dir) / "rustc"
    _fake_slow_rustc.write_text(
        "#!/bin/sh\nwhile true; do :; done\n", encoding="utf-8")
    _fake_slow_rustc.chmod(0o755)
    registry.runtime_path = lambda: _slow_dir
    try:
        slow_rustc = doctor.report(deep=True)
    finally:
        registry.runtime_path = _saved_path
    _rust_row = next(r for r in slow_rustc["runtimes"] if r["name"] == "rust")
    check("a version probe that NEVER answers in time (both attempts) stays "
          "`installed`, never `unhealthy`",
          _rust_row.get("status") == "installed", f"-> {_rust_row.get('status')!r}")
    check("...and probe_error names the timeout, not manufactured elsewhere",
          _rust_row.get("probe_error") is not None
          and "timed out" in _rust_row["probe_error"],
          f"-> {_rust_row.get('probe_error')!r}")
    check("...and probe_ms records real wall time across both attempts",
          isinstance(_rust_row.get("probe_ms"), (int, float)) and _rust_row["probe_ms"] > 0,
          f"-> {_rust_row.get('probe_ms')!r}")
    check("...and version stays unmeasured, never invented from the timeout",
          _rust_row.get("version") is None, f"-> {_rust_row.get('version')!r}")
    check("...and rust being `tested`-tier does NOT cost `healthy` — a "
          "timeout is the RUNNER being slow, not codecalc's own guarantee "
          "failing",
          slow_rustc["healthy"] is True, f"-> {slow_rustc['healthy']}")
    check("...and the report still validates", not errors_for(slow_rustc))
    _shutil_fixtures.rmtree(_slow_dir, ignore_errors=True)

    # The retry path, proven to actually RECOVER a version rather than merely
    # tolerating its absence: the FIRST call blocks past the deadline (an
    # unconditional busy-loop — see the note above this section for why not
    # `sleep N`), the SECOND is warm (a marker file left by the first call
    # short-circuits the loop) — the exact shape a cold rustup proxy takes,
    # slow once per process and fast forever after.
    _warm_dir = _tf.mkdtemp(prefix="codecalc-doctor-test-")
    _marker = pathlib.Path(_warm_dir) / ".called"
    _fake_warm_rustc = pathlib.Path(_warm_dir) / "rustc"
    _fake_warm_rustc.write_text(
        f'#!/bin/sh\n'
        f'if [ ! -f "{_marker}" ]; then\n'
        f'  /usr/bin/touch "{_marker}"\n'
        f'  while true; do :; done\n'
        f'fi\n'
        f'echo "rustc 1.99.0 (fake, warm on retry)"\n', encoding="utf-8")
    _fake_warm_rustc.chmod(0o755)
    registry.runtime_path = lambda: _warm_dir
    try:
        warm_rustc = doctor.report(deep=True)
    finally:
        registry.runtime_path = _saved_path
    _warm_row = next(r for r in warm_rustc["runtimes"] if r["name"] == "rust")
    check("a proxy that is slow only ONCE is recovered by the retry: a "
          "real version IS read",
          _warm_row.get("version") == "rustc 1.99.0 (fake, warm on retry)",
          f"-> {_warm_row.get('version')!r}")
    check("...and status is never touched by the first attempt's timeout",
          _warm_row.get("status") == "installed", f"-> {_warm_row.get('status')!r}")
    check("...and no probe_error is recorded — the retry answered, so "
          "nothing here failed",
          _warm_row.get("probe_error") is None, f"-> {_warm_row.get('probe_error')!r}")
    _shutil_fixtures.rmtree(_warm_dir, ignore_errors=True)

    # GENERALITY: the classification fix is not a rustc-only special case.
    # `report()`'s trust rule (`not probe_timed_out and cmd in
    # _VERSION_FLAG`) and the retry loop in `_probe_version` both apply to
    # every command uniformly — `_PROBE_TIMEOUT_S` only raises the ceiling
    # for the handful of commands audited as slow-start proxies, it does not
    # gate whether the classification or the retry apply at all. `go`
    # reproduces the identical failure on its OWN evidence (a later
    # 2026-09-08 windows-latest run than the rustc hits: `go version timed
    # out after 10s` -> unhealthy -> healthy false) while staying on the
    # plain DEFAULT timeout — deliberately NOT added to `_PROBE_TIMEOUT_S` —
    # so this fixture is the one that would fail if the fix had quietly been
    # a per-command special case instead of a change to `report()`'s rule.
    assert "go" not in doctor._PROBE_TIMEOUT_S, (
        "fixture invalid: go must stay on the DEFAULT timeout, unlisted in "
        "_PROBE_TIMEOUT_S, to prove the fix is not a rustc-only table")
    _slow_go_dir = _tf.mkdtemp(prefix="codecalc-doctor-test-")
    _fake_slow_go = pathlib.Path(_slow_go_dir) / "go"
    _fake_slow_go.write_text(
        "#!/bin/sh\nwhile true; do :; done\n", encoding="utf-8")
    _fake_slow_go.chmod(0o755)
    registry.runtime_path = lambda: _slow_go_dir
    try:
        slow_go = doctor.report(deep=True)
    finally:
        registry.runtime_path = _saved_path
    _go_row = next(r for r in slow_go["runtimes"] if r["name"] == "go")
    check("go, on the plain DEFAULT timeout with no per-command entry, is "
          "ALSO never demoted by a bare timeout",
          _go_row.get("status") == "installed", f"-> {_go_row.get('status')!r}")
    check("...and probe_error still names the timeout",
          _go_row.get("probe_error") is not None
          and "timed out" in _go_row["probe_error"],
          f"-> {_go_row.get('probe_error')!r}")
    check("...and healthy stays true — go is `tested`-tier, exactly the "
          "combination that flipped it false on main",
          slow_go["healthy"] is True, f"-> {slow_go['healthy']}")
    _shutil_fixtures.rmtree(_slow_go_dir, ignore_errors=True)

    doctor._PROBE_TIMEOUT_S = _saved_probe_timeout
    doctor._DEFAULT_PROBE_TIMEOUT_S = _saved_default_timeout

    # The other half: a timeout is not FREE cover. `bash` is in `_HELLO`, so
    # its hello-world run is the arbiter regardless of what the version
    # probe did — if that run ALSO fails, the row is genuinely unhealthy,
    # and a version-probe timeout must not be read as protecting it. The
    # environment variable, not the `registry.runtime_path` monkeypatch used
    # above: `_HELLO`'s check spawns `codecalc-exec` as a SEPARATE process
    # (see the go/lua/zig fixture earlier in this file for the same reason),
    # which only ever sees `CODECALC_RUNTIME_PATH` from its own inherited
    # environment.
    _dead_dir = _tf.mkdtemp(prefix="codecalc-doctor-test-")
    _fake_dead_bash = pathlib.Path(_dead_dir) / "bash"
    _fake_dead_bash.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "--version" ]; then while true; do :; done; fi\n'
        'exit 1\n', encoding="utf-8")
    _fake_dead_bash.chmod(0o755)
    doctor._PROBE_TIMEOUT_S = {**_saved_probe_timeout, "bash": 1.0}
    _saved_runtime_path_env2 = os.environ.get(registry.RUNTIME_PATH_ENV)
    os.environ[registry.RUNTIME_PATH_ENV] = str(_dead_dir)
    try:
        dead_bash = doctor.report(deep=True)
    finally:
        doctor._PROBE_TIMEOUT_S = _saved_probe_timeout
        if _saved_runtime_path_env2 is None:
            os.environ.pop(registry.RUNTIME_PATH_ENV, None)
        else:
            os.environ[registry.RUNTIME_PATH_ENV] = _saved_runtime_path_env2
    _dead_bash_row = next(r for r in dead_bash["runtimes"] if r["name"] == "bash")
    check("a timed-out version probe does NOT shield a runtime whose "
          "hello-world ALSO fails — it is genuinely `unhealthy`",
          _dead_bash_row.get("status") == "unhealthy",
          f"-> {_dead_bash_row.get('status')!r}")
    check("...and the timeout is still on record (a human reading this row "
          "sees why the version is missing too)",
          _dead_bash_row.get("probe_error") is not None
          and "timed out" in _dead_bash_row["probe_error"],
          f"-> {_dead_bash_row.get('probe_error')!r}")
    _shutil_fixtures.rmtree(_dead_dir, ignore_errors=True)


# ── AN AUDITED DEFAULT FLAG IS TRUSTED THE SAME AS AN OVERRIDE ─────────────
#
# The residual gap after #279/#280: a command with NO `_VERSION_FLAG` entry
# that exits nonzero on the untested `--version` guess is reported as merely
# unmeasured (correct — the guess might be wrong), but that same
# unmeasured-forever treatment was ALSO applied to commands whose plain
# `--version` had already been confirmed correct by this box's own audit
# (dotnet, node, sqlite3, ...) purely because nobody had bothered to say so
# in `_VERSION_FLAG` itself. `dotnet` — not `awk` — is the fixture here:
# `dotnet` is a single-vendor CLI with no `_HELLO` backstop, so a fake
# `dotnet` failing its confirmed flag proves the `_VERSION_FLAG` widening
# ALONE does something. `awk` would prove nothing here — it is ALSO in
# `_HELLO` (see below), which would arbitrate a fully-broken fake awk to
# `unhealthy` regardless of whether its flag were trusted at all.
assert "dotnet" in doctor._VERSION_FLAG and doctor._VERSION_FLAG["dotnet"] == "--version", (
    "fixture invalid: dotnet must be a CONFIRMED-default entry for this test to mean anything")
assert "dotnet" not in doctor._HELLO, (
    "fixture invalid: pick a confirmed-default command with no hello-world "
    "backstop, or this test cannot isolate what _VERSION_FLAG alone does")
if os.name == "nt":
    print("SKIP audited-default-flag trust (needs a POSIX shell script fixture)")
else:
    _dotnet_dir = _tf.mkdtemp(prefix="codecalc-doctor-test-")
    _fake_dotnet = pathlib.Path(_dotnet_dir) / "dotnet"
    _fake_dotnet.write_text(
        '#!/bin/sh\necho "dotnet: fatal: fixture failure" >&2\nexit 1\n', encoding="utf-8")
    _fake_dotnet.chmod(0o755)
    registry.runtime_path = lambda: _dotnet_dir
    try:
        dotnet_broken = doctor.report(deep=True)
    finally:
        registry.runtime_path = _saved_path
    _dotnet_row = next((r for r in dotnet_broken["runtimes"] if r["name"] == "csharp"), {})
    check("a CONFIRMED default flag's nonzero exit IS trusted as `unhealthy` "
          "(the audit widening actually does something)",
          _dotnet_row.get("status") == "unhealthy", f"-> {_dotnet_row.get('status')!r}")
    check("...and the failure is named in probe_error, not manufactured",
          _dotnet_row.get("probe_error") is not None
          and "fixture failure" in _dotnet_row["probe_error"],
          f"-> {_dotnet_row.get('probe_error')!r}")
    check("...and csharp is best_effort, so `healthy` is unaffected",
          dotnet_broken["healthy"] is True)
    _shutil_fixtures.rmtree(_dotnet_dir, ignore_errors=True)


# ── A REJECTED CONFIRMED FLAG IS ARBITRATED BY HELLO-WORLD, NOT TRUSTED ────
#
# `awk` is different from `dotnet` above precisely because its confirmed
# `--version` is only confirmed for the GNU awk THIS box happens to run —
# BSD/macOS one-true-awk and busybox awk reject GNU long options outright.
# Trusting that confirmed-here flag on every host would repeat #282's own
# regression for a command #282 never audited. `awk` is in `_HELLO` for
# exactly this reason: a `BEGIN{...}` hello-world is portable POSIX syntax
# across every real implementation and arbitrates BEFORE `probe_error` gets
# a say (see the ordering note in `report()`), regardless of which awk
# flavor is on PATH. This fixture simulates the BSD-awk shape directly: it
# rejects `--version`/`-version` but runs an ordinary awk program correctly.
#
# `_HELLO` spawns through `executor.execute()`, which on the Rust backend
# shells out to `codecalc-exec` as a SEPARATE process that inherits this
# process's OS environment wholesale (no `env=` override, unlike `_env()`'s
# callers) — the same reason the go/lua/zig fixture above sets the ENV VAR,
# not the `registry.runtime_path` attribute, and this fixture does too.
if os.name == "nt":
    print("SKIP BSD-awk hello-world arbitration (needs a POSIX shell script fixture)")
else:
    _bsdawk_dir = _tf.mkdtemp(prefix="codecalc-doctor-test-")
    _fake_bsdawk = pathlib.Path(_bsdawk_dir) / "awk"
    _fake_bsdawk.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "--version" ] || [ "$1" = "-version" ]; then\n'
        '  echo "awk: unknown option -- version" >&2\n'
        '  exit 2\n'
        'fi\n'
        'echo codecalc\n'
        'exit 0\n', encoding="utf-8")
    _fake_bsdawk.chmod(0o755)
    _saved_bsdawk_env = os.environ.get(registry.RUNTIME_PATH_ENV)
    os.environ[registry.RUNTIME_PATH_ENV] = str(_bsdawk_dir)
    try:
        bsdawk = doctor.report(deep=True)
    finally:
        if _saved_bsdawk_env is None:
            os.environ.pop(registry.RUNTIME_PATH_ENV, None)
        else:
            os.environ[registry.RUNTIME_PATH_ENV] = _saved_bsdawk_env
    _bsdawk_row = next((r for r in bsdawk["runtimes"] if r["name"] == "awk"), {})
    check("a BSD-flavored awk that rejects the confirmed --version flag is "
          "NOT demoted to unhealthy",
          _bsdawk_row.get("status") == "available", f"-> {_bsdawk_row.get('status')!r}")
    check("...and the rejected flag is still recorded, not silently dropped",
          _bsdawk_row.get("probe_error") is not None,
          f"-> {_bsdawk_row.get('probe_error')!r}")
    check("...and version stays unmeasured (the flag never answered), never invented",
          _bsdawk_row.get("version") is None, f"-> {_bsdawk_row.get('version')!r}")
    _shutil_fixtures.rmtree(_bsdawk_dir, ignore_errors=True)

# The positive half: on THIS real box, awk's confirmed default flag yields
# an actual version string, proving the audit entry didn't change *which*
# flag is sent, only whether a nonzero exit from it is trusted. Made
# TOLERANT of a real BSD/macOS/busybox awk that rejects `--version` outright
# (the exact risk flagged in review — this box's own awk was audited as GNU
# awk only): a `None` read here is not a failure, it means the flag was
# rejected — the fixture above already proves that case is handled safely —
# so this SKIPS loudly, naming the awk flavor if it can be identified,
# rather than failing on a host whose awk simply is not GNU awk.
_awk_path = _shutil_fixtures.which("awk", path=registry.runtime_path())
if _awk_path is None:
    print("SKIP newly-audited-flag real version (awk not on this host's PATH)")
else:
    _awk_version = doctor._runtime_version("awk", _awk_path)
    if _awk_version is None:
        _flavor = None
        for _probe_args in (["-W", "version"], ["-version"]):
            try:
                _p = subprocess.run([_awk_path, *_probe_args],
                                    capture_output=True, timeout=5, text=True)
            except (OSError, subprocess.SubprocessError):
                continue
            _lines = (_p.stdout or _p.stderr or "").strip().splitlines()
            if _lines:
                _flavor = _lines[0]
                break
        print(f"SKIP awk version read — this host's awk rejects --version "
              f"(not GNU awk); flavor: "
              f"{_flavor or 'unidentified, likely BSD/busybox awk'}")
    else:
        check("a newly-audited command (awk, confirmed default `--version`) "
              "reads a real version on this host",
              isinstance(_awk_version, str) and "wk" in _awk_version.lower(),
              f"-> {_awk_version!r}")


# ── AN UNRESOLVED `probe_error` IS VISIBLE, NOT JUST IN THE JSON ROW ───────
#
# Residual from #282's review: a runtime whose version probe exits nonzero
# on an UNCONFIRMED flag stays `installed` with `probe_error` set (the
# conservative, correct call) — but the text renderer showed such a row
# under NEITHER available/BROKEN/missing, and `_remedies()` said nothing
# about it either. The JSON disclosed it; a human reading `doctor` never saw
# it. Both halves fixed here: `_remedies()` names the runtime and the probe
# output, and the text renderer lists it under its own heading. Neither
# needs a real broken binary — a synthetic row exercises both in isolation
# from the (now much smaller) set of genuinely unaudited commands.
_fake_row = {"name": "fakelang", "command": "fakelang", "status": "installed",
             "path": "/usr/bin/fakelang", "tier": "best_effort", "version": None,
             "probe_error": "fakelang --version: exited 1: usage: fakelang [opts]"}
assert "fakelang" not in doctor._VERSION_FLAG, "fixture invalid: pick a name not audited"

_remedy_lines = doctor._remedies(
    backend="rust", extras=[], runtimes=[_fake_row],
    workspace={"writable": True, "path": doctor.tempfile.gettempdir(), "error": None})
check("an installed-but-probe-failed runtime gets its own remedy line",
      any("fakelang" in line and "version probe failed" in line
          and "may still work" in line for line in _remedy_lines),
      f"-> {_remedy_lines}")
check("...and it is NOT phrased as broken (that would overclaim the evidence)",
      not any("fakelang" in line and "BROKEN" in line for line in _remedy_lines))

import contextlib
import io

from codecalc import server as _server

_visibility_rep = doctor.report()
_visibility_rep = {**_visibility_rep,
                   "runtimes": [*_visibility_rep["runtimes"], _fake_row]}
_saved_report = doctor.report
doctor.report = lambda deep=False: _visibility_rep
_out = io.StringIO()
try:
    with contextlib.redirect_stdout(_out):
        _server._doctor(as_json=False, deep=False)
finally:
    doctor.report = _saved_report
_rendered = _out.getvalue()
check("the text renderer shows a heading for installed-but-probe-failed runtimes",
      "installed, version probe failed:" in _rendered, f"-> {_rendered[-400:]}")
check("...naming the runtime and its probe_error, not just a count",
      "fakelang" in _rendered and _fake_row["probe_error"] in _rendered,
      f"-> {_rendered[-400:]}")


# ── THE REAL HOST, --deep: TESTED-TIER MUST NEVER FALSE-POSITIVE ───────────
#
# The regression above escaped because no test ran `doctor.report(deep=True)`
# against what a REAL runner's toolchains actually do — every other fixture
# in this file fakes a broken binary. This runs it against whatever is
# actually installed here (CI images ship python3/node always, and often
# go/rust) and asserts the four `tested`-tier languages never read
# `unhealthy` when they resolve at all, and that `healthy` stays true.
_real_deep = doctor.report(deep=True)
check("--deep against the REAL host's own toolchains validates",
      not errors_for(_real_deep), f"-> {errors_for(_real_deep)[:3]}")
_real_tested = [r for r in _real_deep["runtimes"] if r["tier"] == "tested"]
check("all four `tested`-tier languages are present to check",
      sorted(r["name"] for r in _real_tested) == ["go", "node", "python3", "rust"],
      f"-> {sorted(r['name'] for r in _real_tested)}")
for _r in _real_tested:
    if _r["status"] == "supported":
        print(f"SKIP {_r['name']}: not installed on this host — nothing to assert")
        continue
    check(f"--deep: {_r['name']} (tested, resolved here) is installed/available, "
          f"never unhealthy",
          _r["status"] in ("installed", "available"),
          f"-> status={_r['status']} version={_r.get('version')!r} "
          f"probe_error={_r.get('probe_error')!r}")
    # Printed rather than asserted into a number: a CI log otherwise has no
    # way to tell "this runtime answered promptly" from "this runtime just
    # barely made the deadline" — the exact distinction a runner-speed flake
    # needs, and the reason this field exists at all. `rust` on a cold
    # `windows-latest` image is the case this is FOR: a slow proxy that
    # still answers should show up here as a large-but-successful number,
    # not vanish into a bare PASS line.
    print(f"     probe_ms[{_r['name']}] = {_r.get('probe_ms')!r}")
    check(f"  ...and {_r['name']}'s probe_ms is a real, non-negative "
          f"duration",
          isinstance(_r.get("probe_ms"), (int, float)) and _r["probe_ms"] >= 0,
          f"-> {_r.get('probe_ms')!r}")
check("--deep against the real host's own tested-tier toolchains stays healthy",
      _real_deep["healthy"] is True,
      f"-> {[(r['name'], r['status'], r.get('probe_error')) for r in _real_tested]}")


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
# --json was before this: accepted, ignored, prose printed anyway.
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
