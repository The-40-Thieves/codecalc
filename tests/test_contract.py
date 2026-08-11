"""The published contract is valid, and the product satisfies it (THE-781).

`scripts/check_contract.py` answers "does the published schema match the module
that generates it". That check is structural and stdlib-only, because gates in
this repo run on a bare checkout. It cannot answer the two questions that need
dependencies and a running product:

  1. Is `docs/contract/result-v1.schema.json` a VALID JSON Schema 2020-12
     document? A generated file can match its generator perfectly and still be
     a schema no validator will load.

  2. Do real results actually validate against it? A schema is a claim about
     the product. Nothing so far has run the product and checked the claim.

POSITIVE AND NEGATIVE CONTROLS, BOTH.
A validator that accepts everything passes every assertion below while proving
nothing, which is this repository's most-repeated defect: an instrument that
reports its own inability to measure as a result. So `check_rejects` runs first
and asserts the validator REJECTS a deliberately malformed result. If that
control fails, every acceptance below is vacuous and the suite says so rather
than printing a column of PASS.

WHY jsonschema IS FAIR GAME HERE AND NOT IN THE GATE
`jsonschema` arrives as a transitive dependency of `mcp`, so it is present
wherever codecalc's dependencies are installed and absent on a bare checkout.
That is exactly the line between this file and `scripts/check_contract.py`, and
the split is deliberate rather than a workaround.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _mcp_client import data, over_stdio
from jsonschema import Draft202012Validator

from codecalc import contract, errors, executor

FAILS: list[str] = []

SCHEMA_PATH = REPO_ROOT / "docs" / "contract" / "result-v1.schema.json"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── the schema loads at all ────────────────────────────────────────────────
check("the published schema file exists", SCHEMA_PATH.exists(),
      f"-> {SCHEMA_PATH.relative_to(REPO_ROOT)}")

published = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

try:
    Draft202012Validator.check_schema(published)
    _schema_valid, _why = True, ""
except Exception as exc:  # a malformed schema is the failure this catches
    _schema_valid, _why = False, f"-> {type(exc).__name__}: {exc}"
check("the published schema is a valid JSON Schema 2020-12 document",
      _schema_valid, _why)

# Compared with the two identifier keys removed. `$schema` and `$id` are
# injected by scripts/check_contract.py rather than living in the package —
# tests/test_offline.py bans public URL literals from codecalc/, and holding
# them here would be the first exception to the ban that keeps this package
# free of an LLM client and a docs fetch. Their VALUES are gated by the byte
# comparison in check_contract.py; what this asserts is that every other field
# in the published document still comes from the module.
_published_body = {k: v for k, v in published.items() if k not in ("$schema", "$id")}
check("the published file matches what contract.py builds right now",
      _published_body == contract.build_schema(),
      "-> run `python scripts/check_contract.py --write`")
check("the published file carries both identifier keys",
      published.get("$schema") and published.get("$id"),
      f"-> $schema={bool(published.get('$schema'))} $id={bool(published.get('$id'))}")

validator = Draft202012Validator(published)


def errors_for(result: dict) -> list[str]:
    return [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(result)]


# ── THE CONTROL. Everything below is vacuous if this fails ─────────────────
# A result with a verdict outside the enum and a missing required field. If the
# validator accepts this, it accepts anything, and every PASS after it means
# only that the validator ran.
_bogus = {
    "ok": True, "contract_version": "1.0.0", "language": "python3",
    "phase": "run", "backend": "rust", "platform": "linux",
    "stdout": "", "stderr": "", "exit_code": 0, "timed_out": False,
    "verdict": "DEFINITELY_NOT_A_VERDICT",
    "output_truncated": False, "output_error": None,
    "duration_ms": 1, "compile_ms": 0, "total_ms": 1, "cpu_ms": 1,
    # A placeholder, not a real path. Written without a /tmp prefix on purpose:
    # ruff's S108 flags a hardcoded temp path even inside a fixture, and this
    # field's value is irrelevant to what the control asserts.
    "peak_memory_kb": 1, "unenforced": [], "workdir": "(placeholder)",
}
_control_rejected = bool(errors_for(_bogus))
check("CONTROL: the validator REJECTS an invalid verdict",
      _control_rejected,
      "-> accepted it; every assertion below is vacuous" if not _control_rejected else "")

_bogus_missing = {"ok": True, "contract_version": "1.0.0", "verdict": "OK"}
check("CONTROL: the validator REJECTS an envelope missing required fields",
      bool(errors_for(_bogus_missing)))

_bogus_version = {"ok": False, "error": "x", "contract_version": "one point oh"}
check("CONTROL: the validator REJECTS a malformed contract_version",
      bool(errors_for(_bogus_version)))


# ── every execution status the product can produce ─────────────────────────
_truncating = 'print("x" * 200000)'

STATUS_CASES = [
    ("OK   success",
     lambda: executor.execute("python3", 'print("42")'), "OK"),
    ("RTE  program exited non-zero",
     lambda: executor.execute("python3", "import sys; sys.exit(3)"), "RTE"),
    ("TLE  wall-clock timeout",
     lambda: executor.execute("python3", "import time; time.sleep(5)", timeout=1), "TLE"),
    ("OLE  output over the cap",
     lambda: executor.execute("python3", _truncating, max_output_kb=1), "OLE"),
]

for _label, _call, _want_verdict in STATUS_CASES:
    _r = _call()
    _errs = errors_for(_r)
    check(f"{_label} validates against the published schema", not _errs,
          f"-> {_errs[:2]}")
    check(f"{_label} is classified {_want_verdict}",
          _r.get("verdict") == _want_verdict, f"-> {_r.get('verdict')}")
    check(f"{_label} carries contract_version",
          _r.get("contract_version") == contract.CONTRACT_VERSION,
          f"-> {_r.get('contract_version')}")

# the rejected shape: no verdict, because nothing ran
_rejected = executor.execute("nosuchlang", "x")
check("a rejected request validates against the published schema",
      not errors_for(_rejected), f"-> {errors_for(_rejected)[:2]}")
check("a rejected request carries NO verdict",
      "verdict" not in _rejected, f"-> {_rejected.get('verdict')}")
check("a rejected request still carries contract_version",
      _rejected.get("contract_version") == contract.CONTRACT_VERSION)

# BOTH backends, because the contract's whole claim is that they agree. The
# fallback is forced the same way scripts/check_parity.py forces it.
_saved = executor._rust
executor._rust = None
try:
    _fb = executor.execute("python3", 'print("42")')
finally:
    executor._rust = _saved
check("the pure-Python fallback validates against the same schema",
      not errors_for(_fb), f"-> {errors_for(_fb)[:2]}")
check("the fallback identifies itself as backend=python",
      _fb.get("backend") == "python", f"-> {_fb.get('backend')}")

# TYPE parity, which the key-set diff structurally cannot see.
#
# scripts/check_parity.py runs both backends and compares `set(result)`. That
# catches a missing field and is blind to a field whose TYPE differs, which is
# how this was found: the fallback returns peak_memory_kb=None while the native
# executor returns an int, both backends carrying the key, key sets identical,
# gate green. A caller doing arithmetic on it crashes on one backend only.
#
# The divergence is legitimate and is now IN the contract (null means "not
# measured", and `unenforced` says why) rather than being smoothed away. What
# this asserts is that any such divergence is one the published schema already
# permits, so it cannot be introduced silently again.
_saved2 = executor._rust
_pairs = []
for _label2, _kw in [
    ("success", {"language": "python3", "code": 'print("42")'}),
    ("failure", {"language": "python3", "code": "import sys; sys.exit(3)"}),
    ("timeout", {"language": "python3", "code": "import time; time.sleep(5)", "timeout": 1}),
]:
    _rust_r = executor.execute(**_kw)
    executor._rust = None
    try:
        _py_r = executor.execute(**_kw)
    finally:
        executor._rust = _saved2
    _pairs.append((_label2, _rust_r, _py_r))

for _label2, _rust_r, _py_r in _pairs:
    check(f"both backends' {_label2} results validate",
          not errors_for(_rust_r) and not errors_for(_py_r),
          f"-> rust={errors_for(_rust_r)[:1]} python={errors_for(_py_r)[:1]}")
    _mismatch = {k: (type(_rust_r[k]).__name__, type(_py_r[k]).__name__)
                 for k in set(_rust_r) & set(_py_r)
                 if type(_rust_r[k]) is not type(_py_r[k])}
    # Not "there must be none" — peak_memory_kb legitimately differs. The claim
    # is that every difference is one the schema already declares, which the
    # validation above proves, and that the set has not GROWN unnoticed.
    check(f"{_label2}: every type divergence is a declared one",
          set(_mismatch) <= {"peak_memory_kb", "exit_code"},
          f"-> {_mismatch}")

# The asymmetry the docs promise: MLE is native-only. Asserted against the
# fallback's own classifier rather than against prose about it.
check("the fallback cannot emit a native-only verdict",
      all(v not in {executor._fallback_verdict(rc, to, tr)
                    for rc in (0, 1, -9) for to in (True, False) for tr in (True, False)}
          for v in contract.NATIVE_ONLY_VERDICTS),
      f"-> native-only is {list(contract.NATIVE_ONLY_VERDICTS)}")


# ── the byte counts behind output_truncated ───────────────────────────────
#
# `output_truncated` is a boolean, so "printed 9 KiB" and "printed 4 MB" were
# the same answer and there was no way to size a retry. These assert the count
# is the ORIGINAL size, on both backends, which is the whole claim — and the
# reason it could not simply be len(stdout): both backends deliberately stop
# buffering at the cap, so the received string can never answer it.
_BIG = 'print("x" * 200000)'
_EXPECTED_BYTES = 200001          # 200000 characters plus the newline

_saved4 = executor._rust
_byte_cases = []
_rust_trunc = executor.execute("python3", _BIG, max_output_kb=1)
executor._rust = None
try:
    _py_trunc = executor.execute("python3", _BIG, max_output_kb=1)
    _py_quiet = executor.execute("python3", "pass")
finally:
    executor._rust = _saved4
_rust_quiet = executor.execute("python3", "pass")

for _label4, _r4 in (("rust", _rust_trunc), ("python", _py_trunc)):
    check(f"{_label4}: truncated output is flagged", _r4.get("output_truncated") is True)
    # A LOWER BOUND, not the original size. The two backends enforce the cap
    # differently — the fallback kills the process the moment its drain crosses
    # it, the native executor lets it run on under a file-size ceiling — so the
    # only claim that holds for both is "at least this much, and more than we
    # gave you".
    #
    # The first version of this asserted both backends reported exactly 200001.
    # That passed because `print` emits everything in one burst; a program
    # writing the same total slowly measured 204800 on native and 65536 on the
    # fallback. The assertion was not wrong about this program, it was wrong
    # about what the field means, and it would have failed as a flake later.
    check(f"{_label4}: stdout_bytes exceeds what was delivered",
          (_r4.get("stdout_bytes") or 0) > len(_r4.get("stdout") or ""),
          f"-> {_r4.get('stdout_bytes')} vs len(stdout)={len(_r4.get('stdout') or '')}")
    check(f"{_label4}: stdout_bytes is a plausible lower bound",
          0 < (_r4.get("stdout_bytes") or 0) <= _EXPECTED_BYTES,
          f"-> {_r4.get('stdout_bytes')} (program wrote {_EXPECTED_BYTES})")

# Where the backends MUST agree: an untruncated run. Then the count is exact and
# nothing about enforcement differs, so a divergence here would be a real defect
# rather than the documented consequence of two cap strategies.
_rust_small = executor.execute("python3", 'print("x" * 100)')
_saved5 = executor._rust
executor._rust = None
try:
    _py_small = executor.execute("python3", 'print("x" * 100)')
finally:
    executor._rust = _saved5
check("untruncated: both backends report the SAME exact size",
      _rust_small.get("stdout_bytes") == _py_small.get("stdout_bytes") == 101,
      f"-> rust={_rust_small.get('stdout_bytes')} python={_py_small.get('stdout_bytes')}")
check("untruncated: the count equals the delivered length",
      _rust_small.get("stdout_bytes") == len(_rust_small.get("stdout") or ""),
      f"-> {_rust_small.get('stdout_bytes')} vs {len(_rust_small.get('stdout') or '')}")

# 0 is a real measurement and must not be confused with "not measured". A
# program that prints nothing is the case that would have been papered over if
# the absent value had been spelled 0 rather than null.
for _label4, _r4 in (("rust", _rust_quiet), ("python", _py_quiet)):
    check(f"{_label4}: a silent program reports 0, not null",
          _r4.get("stdout_bytes") == 0, f"-> {_r4.get('stdout_bytes')!r}")

# The counts describe the PROGRAM's output, not the field beside them. On a
# timeout `stderr` carries codecalc's own kill message while the child wrote
# nothing, and conflating the two would make the count meaningless.
_tle = executor.execute("python3", "import time; time.sleep(5)", timeout=1)
check("a timeout's stderr_bytes counts the CHILD, not our kill message",
      _tle.get("stderr_bytes") == 0 and len(_tle.get("stderr") or "") > 0,
      f"-> stderr_bytes={_tle.get('stderr_bytes')} len(stderr)={len(_tle.get('stderr') or '')}")


# ── every error category, not just the ones that are easy to reach ─────────
# The criterion is "contract tests cover every status and error category". A
# loop over the four codes that happen to be reachable from a tool call would
# satisfy the sentence and not the intent, so this iterates ALL_CODES and fails
# on any that the schema will not accept.
_uncovered = []
for _code in sorted(errors.ALL_CODES):
    _res = contract.stamp(errors.error_result(_code, f"synthetic {_code} for contract coverage"))
    _errs = errors_for(_res)
    if _errs:
        _uncovered.append((_code, _errs[0]))
    check(f"error category {_code!r} validates", not _errs, f"-> {_errs[:1]}")
    check(f"error category {_code!r} carries an actionable remedy",
          bool(_res.get("remedy")))

check("every code in the taxonomy is covered by this suite",
      not _uncovered and len(errors.ALL_CODES) >= 8,
      f"-> {len(errors.ALL_CODES)} codes, uncovered={_uncovered}")

# An unknown code must degrade rather than escape into a published result: the
# schema's enum is closed, so a code that bypassed error_result's validation
# would produce a result the schema rejects. Both halves asserted.
_degraded = contract.stamp(errors.error_result("not_a_real_code", "x"))
check("an unknown code degrades to internal", _degraded["code"] == errors.INTERNAL,
      f"-> {_degraded['code']}")
check("...and the degraded result still validates", not errors_for(_degraded))


# ── the provenance distinction survives the guard boundary ────────────────
# Regression floor for the defect this slice fixed: guarded_call rebuilt a bare
# {ok, error} and DISCARDED the code chosen at the raise site, after which
# ensure_code re-derived one from the message and marked it code_inferred.
# Measured before the fix: 1 of 6 codes came back wrong (FileNotFoundError ->
# internal instead of runtime_unavailable), and 6 of 6 were mislabelled.
from codecalc import guarded

_PROVENANCE_CASES = {
    errors.VALIDATION: ValueError("x must be a positive integer"),
    errors.TIMEOUT: TimeoutError("solver deadline"),
    errors.RESOURCE_EXHAUSTED: MemoryError("out of memory"),
    errors.PERMISSION_DENIED: PermissionError("path escapes the jail"),
    errors.RUNTIME_UNAVAILABLE: FileNotFoundError("ghc"),
    errors.INTERNAL: OSError("ioctl failed on the pty"),
}
for _want, _exc in _PROVENANCE_CASES.items():
    def _raise(_e=_exc):
        raise _e

    _got = errors.ensure_code(dict(guarded.guarded_call(_raise)))
    check(f"raise-site {_want!r} survives guarded_call",
          _got.get("code") == _want, f"-> {_got.get('code')}")
    check(f"...and is NOT relabelled as inferred ({_want})",
          _got.get("code_inferred") is None, f"-> {_got.get('code_inferred')}")

# The OOM path answers with a constant built at import time; it must carry the
# code, or the one failure we are most certain about arrives codeless.
check("the pre-built OOM payload carries resource_exhausted",
      json.loads(guarded._OOM_PAYLOAD).get("code") == errors.RESOURCE_EXHAUSTED,
      f"-> {json.loads(guarded._OOM_PAYLOAD).get('code')}")


# ── EVERY execution surface, not just executor.execute ────────────────────
#
# This block exists because the first version of this suite did not have it,
# and a cross-vendor review found three surfaces returning results that the
# published schema rejected — while every check above passed.
#
#   compact_result       rebuilds the dict after executor.execute returns
#   native streaming     returns the executor's own JSON directly
#   session workers      never call executor.execute at all
#
# The pattern is worth naming: the suite tested the function I had reasoned
# about, and the claim was about the product. Reaching these needs the SERVER
# surface, not the executor module, which is the only reason testing the
# module felt like testing the contract.
from codecalc import server

_sid = server.session_start("python3").get("session_id")
try:
    _SURFACES = [
        ("server: full envelope",
         lambda: server.execute_code("python3", 'print("42")')),
        ("server: compact",
         lambda: server.execute_code("python3", 'print("42")', compact=True)),
        ("server: rejected",
         lambda: server.execute_code("nosuchlang", "x")),
        ("server: timeout",
         lambda: server.execute_code("python3", "import time; time.sleep(5)", timeout=1)),
        # A COMPILED language, deliberately. scripts/check_parity.py diffs the
        # two backends using python3 — interpreted — so nothing this repo
        # compares ever compiles, and the fallback's compile-failure branch
        # returned 9 keys against the native backend's 19 without any gate
        # noticing. Both compile branches are exercised here.
        ("server: rust compile failure",
         lambda: server.execute_code("c", "int main(){ bad }")),
        ("server: session worker",
         lambda: server.execute_code("python3", 'print("42")', session_id=_sid)),
        ("server: session worker, failing",
         lambda: server.execute_code("python3", "import sys; sys.exit(3)", session_id=_sid)),
    ]
    for _label3, _call3 in _SURFACES:
        _r3 = _call3()
        _e3 = errors_for(_r3)
        check(f"{_label3} validates", not _e3, f"-> {_e3[:1]}")
        check(f"{_label3} carries contract_version",
              _r3.get("contract_version") == contract.CONTRACT_VERSION,
              f"-> {_r3.get('contract_version')}")

    # The fallback's compile path, forced. Its own branch, because forcing the
    # fallback and compiling are two independent conditions and the bug lived
    # at their intersection.
    _saved3 = executor._rust
    executor._rust = None
    try:
        _fb_compile = server.execute_code("c", "int main(){ bad }")
        _fb_compact = server.execute_code("python3", "print(1)", compact=True)
    finally:
        executor._rust = _saved3
    check("fallback COMPILE failure validates", not errors_for(_fb_compile),
          f"-> {errors_for(_fb_compile)[:1]}")
    check("fallback compile failure reports a verdict",
          _fb_compile.get("verdict") in contract.VERDICTS,
          f"-> {_fb_compile.get('verdict')}")
    check("fallback compact validates", not errors_for(_fb_compact))

    # Key-set parity on the COMPILE path, which check_parity cannot reach.
    _rust_compile = server.execute_code("c", "int main(){ bad }")
    check("both backends agree on the compile-failure key set",
          set(_rust_compile) == set(_fb_compile),
          f"-> rust-only={sorted(set(_rust_compile) - set(_fb_compile))} "
          f"python-only={sorted(set(_fb_compile) - set(_rust_compile))}")
finally:
    if _sid:
        server.session_stop(_sid)

# The failure path carries an allowlist, not everything-but-one. A synthetic
# outcome with a `value` proves the allowlist rather than the absence of a
# current producer that would populate it.
_real_run_guarded = guarded.run_guarded
guarded.run_guarded = lambda *a, **k: {"ok": False, "value": "LEAKED",
                                       "error": "x", "code": errors.INTERNAL}
try:
    _leak = guarded.guarded_call(lambda: None)
finally:
    guarded.run_guarded = _real_run_guarded
check("guarded_call cannot forward a stale 'value'", "value" not in _leak,
      f"-> {sorted(_leak)}")
check("...while still carrying the error contract",
      _leak.get("code") == errors.INTERNAL and _leak.get("ok") is False)


# ── MCP round-trip: the SERIALISED response, not the dict in this process ──
async def main() -> None:
    async with over_stdio() as c:
        r = await c.call_tool("execute_code", {"language": "python3",
                                               "code": 'print("42")'})
        payload = data(r)
        check("execute_code round-trips as a dict", isinstance(payload, dict),
              f"-> {type(payload).__name__}")
        if isinstance(payload, dict):
            _errs = errors_for(payload)
            check("the SERIALISED success response validates", not _errs,
                  f"-> {_errs[:2]}")
            check("the serialised response carries contract_version",
                  payload.get("contract_version") == contract.CONTRACT_VERSION,
                  f"-> {payload.get('contract_version')}")

        # A failure over the wire, because serialisation is where a null or a
        # missing key would show up differently from the in-process dict.
        r2 = await c.call_tool("execute_code", {"language": "python3",
                                               "code": "import time; time.sleep(5)",
                                               "timeout": 1})
        payload2 = data(r2)
        if isinstance(payload2, dict):
            _errs2 = errors_for(payload2)
            check("the SERIALISED timeout response validates", not _errs2,
                  f"-> {_errs2[:2]}")
            check("the serialised timeout is classified TLE",
                  payload2.get("verdict") == "TLE", f"-> {payload2.get('verdict')}")

        # A rejected request over the wire: the short shape has to survive
        # serialisation too, and it is the one the schema treats differently.
        r3 = await c.call_tool("execute_code", {"language": "nosuchlang",
                                               "code": "x"})
        payload3 = data(r3)
        if isinstance(payload3, dict):
            _errs3 = errors_for(payload3)
            check("the SERIALISED rejection validates", not _errs3,
                  f"-> {_errs3[:2]}")
            check("the serialised rejection carries a code",
                  payload3.get("code") in errors.ALL_CODES,
                  f"-> {payload3.get('code')}")


asyncio.run(main())


# ── the gate itself exists ────────────────────────────────────────────────
# Same floor as tests/test_features.py:274. A gate destroyed by an editing
# accident raises nothing and every check it performed silently stops
# happening — which has already happened once in this repo, to check_claims.py.
_gate = REPO_ROOT / "scripts" / "check_contract.py"
check("the contract gate exists", _gate.exists(), f"-> {_gate}")
if _gate.exists():
    _src = _gate.read_text(encoding="utf-8")
    check("the gate still checks the schema is in sync", "--write" in _src)
    check("the gate still cross-checks both backends' verdicts",
          "rust_verdicts" in _src and "python_verdicts" in _src)


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL CONTRACT TESTS PASS ===")
for _f in FAILS:
    print(f"  {_f}")
sys.exit(1 if FAILS else 0)
