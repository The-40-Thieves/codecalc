"""Per-run dependencies for execute_code/session_run.

Covers: PEP 723 block parsing (valid, a tool table alongside dependencies, no
block, malformed -> validation), the merge/dedupe rule (explicit argument wins
a conflict, PEP 503 normalized), the refusal rule (no_net / deny-network never
fetch, `packages.install` is never called, and the refusal names `network` in
`requested_capabilities`), the happy path (`packages.install` mocked, called
once per dependency with the run's workdir, before the executor, and the
result carries `dependencies` with a per-install `elapsed_ms`), the
sessionless workdir (created, passed as `--workdir`, removed via the
identity-checked deletion), the aggregate dependency-install BUDGET (separate
from the run's own `timeout` — exceeding it stops before the next install and
is reported as a stamped `errors.TIMEOUT`), the per-run workdir disk QUOTA
(reused from the session-workspace cap; exceeding it after a successful
install is reported as a stamped `errors.RESOURCE_EXHAUSTED`), and that this
module itself imports nothing network-related.

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention.
`packages.install` is stubbed by reassigning the module attribute the same way
tests/test_package_allowlist.py stubs `packages.subprocess.run` — no
unittest.mock, so a stub records exactly the calls it saw rather than an
interaction history a mock object would have to be queried for.
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import capabilities, dependencies, errors, execution_service, providers, sessions

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


NET = capabilities.NETWORK


# ═══ 1. PEP 723 block parsing ══════════════════════════════════════════════

VALID_BLOCK = (
    "# /// script\n"
    "# dependencies = [\"requests==2.31.0\", \"rich\"]\n"
    "# requires-python = \">=3.11\"\n"
    "# ///\n"
    "print('hi')\n"
)
block = dependencies.parse_pep723_block(VALID_BLOCK)
check("valid block: parses", block is not None, f"-> {block}")
check("valid block: dependencies read", block and block.get("dependencies") == [
      "requests==2.31.0", "rich"], f"-> {block}")

BLOCK_WITH_TOOL_TABLE = (
    "# /// script\n"
    "# dependencies = [\"requests\"]\n"
    "#\n"
    "# [tool.uv]\n"
    "# exclude-newer = \"2023-10-16T00:00:00Z\"\n"
    "# ///\n"
    "print('hi')\n"
)
block_tool = dependencies.parse_pep723_block(BLOCK_WITH_TOOL_TABLE)
check("block with a tool table: parses without error", block_tool is not None)
check("block with a tool table: dependencies still read, tool table ignored",
      block_tool and block_tool.get("dependencies") == ["requests"],
      f"-> {block_tool}")

NO_BLOCK = "print('no metadata here')\n"
check("no block: returns None, not an error",
      dependencies.parse_pep723_block(NO_BLOCK) is None)

MALFORMED_BLOCK = (
    "# /// script\n"
    "# dependencies = [unterminated\n"
    "# ///\n"
    "print('hi')\n"
)
try:
    dependencies.parse_pep723_block(MALFORMED_BLOCK)
    check("malformed block: raises DependencyBlockError", False, "-> did not raise")
except dependencies.DependencyBlockError:
    check("malformed block: raises DependencyBlockError", True)

specs, err, implicit = dependencies.resolve("python3", MALFORMED_BLOCK, None)
check("malformed block via resolve(): surfaced as validation, not raised",
      err is not None and err.get("code") == "validation", f"-> {err}")
check("malformed block via resolve(): no specs returned", specs == [], f"-> {specs}")
check("malformed block via resolve(): not reported implicit (nothing to install)",
      implicit is False)

MULTIPLE_BLOCKS = (
    "# /// script\n"
    "# dependencies = [\"a\"]\n"
    "# ///\n"
    "# /// script\n"
    "# dependencies = [\"b\"]\n"
    "# ///\n"
)
try:
    dependencies.parse_pep723_block(MULTIPLE_BLOCKS)
    check("multiple script blocks: raises DependencyBlockError", False, "-> did not raise")
except dependencies.DependencyBlockError:
    check("multiple script blocks: raises DependencyBlockError", True)


# ═══ 2. merge / dedupe ══════════════════════════════════════════════════════

merged = dependencies.merge_dependencies(
    ["requests==2.31.0", "numpy"], ["Requests>=3", "pandas"])
check("merge: explicit argument wins a conflicting spec for the same project",
      merged == ["Requests>=3", "numpy", "pandas"], f"-> {merged}")

check("merge: PEP 503 normalization treats _/-/. as equivalent",
      dependencies.normalize_name("Foo_Bar.Baz") == "foo-bar-baz",
      f"-> {dependencies.normalize_name('Foo_Bar.Baz')}")

check("merge: no explicit argument returns the block untouched",
      dependencies.merge_dependencies(["a", "b"], None) == ["a", "b"])

no_block_specs, no_block_err, no_block_implicit = dependencies.resolve(
    "python3", NO_BLOCK, ["only-explicit"])
check("resolve(): no block + an explicit dependency still returns it",
      no_block_specs == ["only-explicit"] and no_block_err is None, f"-> {no_block_specs}")
check("resolve(): an explicit argument alone is not implicit",
      no_block_implicit is False)

node_specs, node_err, node_implicit = dependencies.resolve(
    "node", "console.log(1)", ["lodash"])
check("resolve(): node reads only the explicit argument (no block to merge)",
      node_specs == ["lodash"] and node_err is None, f"-> {node_specs}")
check("resolve(): node's explicit argument is not implicit either", node_implicit is False)

block_only_specs, block_only_err, block_only_implicit = dependencies.resolve(
    "python3", VALID_BLOCK, None)
check("resolve(): a block with NO explicit argument IS implicit",
      block_only_specs == ["requests==2.31.0", "rich"] and block_only_implicit is True,
      f"-> specs={block_only_specs} implicit={block_only_implicit}")

block_and_explicit_specs, _, block_and_explicit_implicit = dependencies.resolve(
    "python3", VALID_BLOCK, ["numpy"])
check("resolve(): a block PLUS an explicit argument is not implicit",
      block_and_explicit_implicit is False, f"-> {block_and_explicit_implicit}")


# ═══ helpers shared by 3-5: a provider that never really executes ═════════

class _CapturingLocal(providers.LocalExecutionProvider):
    """Same provider_id as the real local provider (so the dependency install
    path activates), but never hands anything to the real executor — captures
    the spec instead. Keeps 3-5 fast and independent of a real interpreter."""

    def __init__(self) -> None:
        self.provider_id = "local"
        self.captured_spec = None
        self.calls = 0

    def execute(self, spec: providers.ComputationSpec) -> dict:
        self.calls += 1
        self.captured_spec = spec
        return {"ok": True, "verdict": "OK", "backend": "rust", "language": spec.language,
                "phase": "run", "platform": sys.platform, "stdout": "", "stderr": "",
                "exit_code": 0, "timed_out": False, "output_truncated": False,
                "output_error": None, "stdout_bytes": 0, "stderr_bytes": 0,
                "duration_ms": 0, "compile_ms": 0, "total_ms": 0, "cpu_ms": 0,
                "peak_memory_kb": None, "unenforced": [], "workdir": spec.workdir or ""}


def _service(policy=None):
    provider = _CapturingLocal()
    registry = providers.ProviderRegistry(default_provider_id="local")
    registry.register(provider)
    return execution_service.ExecutionService(registry, policy=policy), provider


class _StubInstall:
    """Swaps in for `dependencies.packages.install` for the duration of a
    `with`, recording every call. Mirrors test_package_allowlist.py's
    `_stub_run` shape — a plain callable swapped onto the module, not a mock
    object.

    `sleep_seconds`, when set, blocks for that long before returning — used to
    force the aggregate install BUDGET to run out mid-loop without needing a
    real slow installer. `write_bytes`, when set, writes that many bytes into
    a file on success — under the given `workdir` for a sessionless call
    (used to force the sessionless workdir past its disk QUOTA without a real
    package), or into the SESSION's own workspace for a session-targeted call
    (`sessions._session_dir(session_id)` — real `packages.install` writes
    there too when `session_id` is given; this stub's `workdir` argument is
    always None on that path, matching the real function, so it has to
    resolve the target the same way to write somewhere a test can observe).
    """

    def __init__(self, ok: bool = True, sleep_seconds: float = 0.0,
                write_bytes: int = 0) -> None:
        self.calls: list[dict] = []
        self.ok = ok
        self.sleep_seconds = sleep_seconds
        self.write_bytes = write_bytes

    def __call__(self, language, package, session_id=None, version=None,
                audit=None, workdir=None, timeout=600):
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        self.calls.append({"language": language, "package": package,
                           "session_id": session_id, "workdir": workdir,
                           "timeout": timeout})
        if not self.ok:
            return {"ok": False, "code": "internal", "error": "stubbed failure"}
        if self.write_bytes:
            target_dir = (pathlib.Path(workdir) if workdir
                         else sessions._session_dir(session_id) if session_id
                         else None)
            if target_dir is not None:
                (target_dir / f"{package}.bin").write_bytes(b"x" * self.write_bytes)
        return {"ok": True, "language": language, "package": package,
                "target": workdir or "", "importable": True, "note": None}

    def __enter__(self):
        self._orig = dependencies.packages.install
        dependencies.packages.install = self
        return self

    def __exit__(self, *exc):
        dependencies.packages.install = self._orig


def _strip_timing(entries: list[dict]) -> list[dict]:
    """A `dependencies` result entry list with `elapsed_ms` removed, for exact
    equality checks against expected content — `elapsed_ms` is a real
    wall-clock measurement and asserted separately (present, non-negative),
    not pinned to a literal value."""
    return [{k: v for k, v in e.items() if k != "elapsed_ms"} for e in entries]


# ═══ 3. refusal: no fetch, packages.install never called ══════════════════

service, provider = _service()
with _StubInstall() as stub:
    result = service.execute(
        providers.ComputationSpec(language="python3", code="print(1)", no_net=True),
        dependencies=["requests"],
    )
check("no_net=True + dependencies: refused",
      result.get("ok") is False, f"-> {result}")
check("no_net=True + dependencies: PERMISSION_DENIED / capability_not_requested",
      result.get("code") == "permission_denied"
      and result.get("provider_error") == capabilities.CAPABILITY_NOT_REQUESTED,
      f"-> {result}")
check("no_net=True + dependencies: packages.install was NEVER called",
      stub.calls == [], f"-> {stub.calls}")
check("no_net=True + dependencies: the provider never ran", provider.calls == 0)
check("no_net=True + dependencies: requested_capabilities names what was denied",
      result.get("requested_capabilities") == [NET], f"-> {result}")

_deny = capabilities.CapabilityPolicy(default_deny=frozenset({NET}), source="deny-network")
service2, provider2 = _service(policy=_deny)
with _StubInstall() as stub2:
    result2 = service2.execute(
        providers.ComputationSpec(language="python3", code="print(1)", no_net=False),
        dependencies=["requests"],
    )
check("deny-network policy + dependencies: refused",
      result2.get("ok") is False, f"-> {result2}")
check("deny-network policy + dependencies: capability_not_requested",
      result2.get("provider_error") == capabilities.CAPABILITY_NOT_REQUESTED,
      f"-> {result2}")
check("deny-network policy + dependencies: packages.install was NEVER called",
      stub2.calls == [], f"-> {stub2.calls}")
check("deny-network policy + dependencies: requested_capabilities names network too",
      result2.get("requested_capabilities") == [NET], f"-> {result2}")

# a direct unit check: refusal_result() itself, both call shapes
check("refusal_result(no_net=True): requested_capabilities is ['network'], not []",
      dependencies.refusal_result(True, None).get("requested_capabilities") == [NET])
check("refusal_result(no_net=False, policy): same, regardless of which branch fired",
      dependencies.refusal_result(False, _deny).get("requested_capabilities") == [NET])

_strict_only = capabilities.CapabilityPolicy(strict=True, source="strict")
service3, provider3 = _service(policy=_strict_only)
with _StubInstall() as stub3:
    result3 = service3.execute(
        providers.ComputationSpec(language="python3", code="print(1)", no_net=False),
        dependencies=["requests"],
    )
check("strict policy (no deny-network) + dependencies: refused too",
      result3.get("ok") is False
      and result3.get("provider_error") == capabilities.CAPABILITY_NOT_REQUESTED,
      f"-> {result3}")
check("strict policy + dependencies: packages.install was NEVER called",
      stub3.calls == [], f"-> {stub3.calls}")

# no dependencies declared: refusal machinery never engages, no_net alone works as before
service4, provider4 = _service()
with _StubInstall() as stub4:
    result4 = service4.execute(
        providers.ComputationSpec(language="python3", code="print(1)", no_net=True))
check("no_net=True with NO dependencies: runs as before (not refused)",
      result4.get("ok") is True and "dependencies" not in result4, f"-> {result4}")
check("no dependencies declared: packages.install never touched", stub4.calls == [])


# ═══ 4/5. happy path: install mocked, called with the run's workdir, ══════
# ═══      before the executor; sessionless workdir created + removed ══════

service5, provider5 = _service()
with _StubInstall() as stub5:
    result5 = service5.execute(
        providers.ComputationSpec(language="python3", code="print(1)"),
        dependencies=["requests", "rich"],
    )
check("happy path: ok", result5.get("ok") is True, f"-> {result5}")
check("happy path: packages.install called once per dependency",
      [c["package"] for c in stub5.calls] == ["requests", "rich"], f"-> {stub5.calls}")
check("happy path: installed with the run's workdir (sessionless -> workdir, not session_id)",
      all(c["session_id"] is None and c["workdir"] for c in stub5.calls),
      f"-> {stub5.calls}")
check("happy path: every install targeted the SAME workdir as the run itself",
      len({c["workdir"] for c in stub5.calls}) == 1
      and provider5.captured_spec is not None
      and provider5.captured_spec.workdir == stub5.calls[0]["workdir"],
      f"-> installs={stub5.calls} spec.workdir={getattr(provider5.captured_spec, 'workdir', None)}")
check("happy path: install happened BEFORE the executor saw the run",
      provider5.calls == 1)
check("happy path: result carries the dependencies field",
      _strip_timing(result5.get("dependencies") or []) == [
          {"spec": "requests", "language": "python3", "ok": True, "installer": "uv"},
          {"spec": "rich", "language": "python3", "ok": True, "installer": "uv"},
      ], f"-> {result5.get('dependencies')}")
check("happy path: each entry reports its own elapsed_ms (a real measurement, >= 0)",
      all(isinstance(e.get("elapsed_ms"), int) and e["elapsed_ms"] >= 0
          for e in (result5.get("dependencies") or [])),
      f"-> {result5.get('dependencies')}")
check("happy path: each install's timeout was drawn from the remaining BUDGET, "
      "not packages.install's own 600s default",
      all(c["timeout"] <= dependencies.DEFAULT_DEPENDENCY_INSTALL_BUDGET_SECONDS
          for c in stub5.calls),
      f"-> {stub5.calls}")
check("sessionless workdir: removed after the run (identity-checked deletion)",
      not pathlib.Path(stub5.calls[0]["workdir"]).exists(),
      f"-> {stub5.calls[0]['workdir']}")

# a failing install stops before the executor runs, and still cleans up
service6, provider6 = _service()
with _StubInstall(ok=False) as stub6:
    result6 = service6.execute(
        providers.ComputationSpec(language="python3", code="print(1)"),
        dependencies=["broken-pkg"],
    )
check("a failing dependency install: run refused, not attempted",
      result6.get("ok") is False and provider6.calls == 0, f"-> {result6}")
check("a failing dependency install: the workdir is still cleaned up",
      not pathlib.Path(stub6.calls[0]["workdir"]).exists(), f"-> {stub6.calls}")
check("a failing dependency install: the failing entry is disclosed",
      _strip_timing(result6.get("dependencies") or []) == [
          {"spec": "broken-pkg", "language": "python3", "ok": False, "installer": "uv"}],
      f"-> {result6.get('dependencies')}")


# ═══ 7. the aggregate install BUDGET, separate from the run's own timeout ═

# Three specs, each install "taking" 0.12s (via a sleeping stub) against a
# 0.2s aggregate budget: the 1st and 2nd fit (elapsed .12, then .24 which is
# already over budget but was already IN FLIGHT when checked), the 3rd is
# refused BEFORE it starts — proving the check happens between installs, not
# as a wall-clock kill of one in progress.
_budget_workdir = pathlib.Path(pathlib.Path.cwd() / f"_dep_budget_test_{os.getpid()}")
_budget_workdir.mkdir(exist_ok=True)
try:
    with _StubInstall(sleep_seconds=0.12) as stub_budget:
        entries_b, failure_b = dependencies.install_dependencies(
            "python3", ["a", "b", "c"], session_id=None,
            workdir=str(_budget_workdir), budget_seconds=0.2)
    check("budget: stops before the dependency that would exceed it",
          len(stub_budget.calls) == 2, f"-> {stub_budget.calls}")
    check("budget: the two attempted installs are disclosed, both ok",
          _strip_timing(entries_b) == [
              {"spec": "a", "language": "python3", "ok": True, "installer": "uv"},
              {"spec": "b", "language": "python3", "ok": True, "installer": "uv"},
          ], f"-> {entries_b}")
    check("budget: the failure is a stamped errors.TIMEOUT",
          failure_b is not None and failure_b.get("code") == errors.TIMEOUT,
          f"-> {failure_b}")
    check("budget: provider_error names the budget, not a generic install failure",
          failure_b is not None
          and failure_b.get("provider_error") == dependencies.DEPENDENCY_INSTALL_BUDGET_EXCEEDED,
          f"-> {failure_b}")
    check("budget: the message names the budget value and 'c' as not attempted",
          failure_b is not None and "0.2" in failure_b.get("error", "")
          and "'c'" in failure_b.get("error", ""), f"-> {failure_b}")
    check("budget: each install's own timeout was bounded by the REMAINING budget, "
          "not packages.install's 600s default",
          all(c["timeout"] <= 1 for c in stub_budget.calls), f"-> {stub_budget.calls}")

    # A budget so tiny the very first dependency cannot start at all.
    with _StubInstall() as stub_budget_zero:
        entries_bz, failure_bz = dependencies.install_dependencies(
            "python3", ["a"], session_id=None,
            workdir=str(_budget_workdir), budget_seconds=0.0)
    check("budget of 0: refuses before the first install, none attempted",
          entries_bz == [] and stub_budget_zero.calls == [], f"-> {entries_bz}")
    check("budget of 0: still a stamped TIMEOUT naming the budget",
          failure_bz is not None and failure_bz.get("code") == errors.TIMEOUT
          and failure_bz.get("provider_error") == dependencies.DEPENDENCY_INSTALL_BUDGET_EXCEEDED,
          f"-> {failure_bz}")
finally:
    import shutil as _shutil
    _shutil.rmtree(_budget_workdir, ignore_errors=True)


# ═══ 8. the sessionless per-run workdir disk QUOTA ═════════════════════════

_QUOTA_ENV = sessions.SESSION_DISK_QUOTA_MB_ENV
_quota_workdir = pathlib.Path(pathlib.Path.cwd() / f"_dep_quota_test_{os.getpid()}")
_quota_workdir.mkdir(exist_ok=True)
_old_quota = os.environ.get(_QUOTA_ENV)
try:
    # ~1 KiB quota — a single 4 KiB "install" blows straight through it.
    os.environ[_QUOTA_ENV] = "0.001"
    with _StubInstall(write_bytes=4096) as stub_quota:
        entries_q, failure_q = dependencies.install_dependencies(
            "python3", ["big-pkg", "never-reached"], session_id=None,
            workdir=str(_quota_workdir))
    check("quota: the OVER-quota install itself still succeeded (it is disclosed ok=True)",
          len(entries_q) == 1 and entries_q[0]["ok"] is True, f"-> {entries_q}")
    check("quota: the SECOND dependency is never attempted",
          len(stub_quota.calls) == 1, f"-> {stub_quota.calls}")
    check("quota: the failure is a stamped errors.RESOURCE_EXHAUSTED",
          failure_q is not None and failure_q.get("code") == errors.RESOURCE_EXHAUSTED,
          f"-> {failure_q}")
    check("quota: provider_error names the workdir quota specifically",
          failure_q is not None
          and failure_q.get("provider_error") == dependencies.DEPENDENCY_WORKDIR_QUOTA_EXCEEDED,
          f"-> {failure_q}")
    check("quota: the failure names the measured size and the cap",
          failure_q is not None and failure_q.get("measured_bytes", 0) > failure_q.get("quota_bytes", 0),
          f"-> {failure_q}")

    # a session-targeted install is NOT subject to this check (session's own
    # quota_precheck/quota_postcheck already govern that workspace). No
    # write_bytes here: a fake session_id has no real directory to write
    # into, and this assertion is about the check being SKIPPED, not about
    # what got written.
    with _StubInstall():
        entries_qs, failure_qs = dependencies.install_dependencies(
            "python3", ["big-pkg"], session_id="not-a-real-session-id", workdir=None)
    check("quota: a SESSION-targeted install is not checked against the workdir quota here",
          failure_qs is None, f"-> {failure_qs}")
finally:
    if _old_quota is None:
        os.environ.pop(_QUOTA_ENV, None)
    else:
        os.environ[_QUOTA_ENV] = _old_quota
    import shutil as _shutil
    _shutil.rmtree(_quota_workdir, ignore_errors=True)


# ═══ session_run: policy-only refusal (no per-call no_net of its own) ═════

_session_service = execution_service.SessionService()
sid = sessions.start("python3")["session_id"]
try:
    write_result = _session_service.write_file(sid, "main.py", "print(1)\n")
    check("session fixture: entry file written", write_result.get("ok") is True,
          f"-> {write_result}")

    os.environ[capabilities.POLICY_ENV] = "deny-network"
    try:
        with _StubInstall() as stub_sr:
            sr_result = _session_service.run_file(
                sid, "main.py", dependencies=["requests"])
    finally:
        os.environ.pop(capabilities.POLICY_ENV, None)
    check("session_run: deny-network policy refuses a dependency-bearing run",
          sr_result.get("ok") is False
          and sr_result.get("provider_error") == capabilities.CAPABILITY_NOT_REQUESTED,
          f"-> {sr_result}")
    check("session_run: packages.install was never called under refusal",
          stub_sr.calls == [], f"-> {stub_sr.calls}")

    with _StubInstall() as stub_sr2:
        sr_result2 = _session_service.run_file(sid, "main.py", dependencies=["requests"])
    check("session_run: with no policy set, the dependency installs into the SESSION",
          [{k: v for k, v in c.items() if k != "timeout"} for c in stub_sr2.calls]
          == [{"language": "python3", "package": "requests",
              "session_id": sid, "workdir": None}],
          f"-> {stub_sr2.calls}")
    check("session_run: result carries the dependencies field",
          _strip_timing(sr_result2.get("dependencies") or []) == [
              {"spec": "requests", "language": "python3", "ok": True, "installer": "uv"}],
          f"-> {sr_result2.get('dependencies')}")
finally:
    sessions.stop(sid)


# ═══ 9. artifact-snapshot ordering: an installed dependency is NOT a "run
# ═══    artifact"; a file the entry file itself writes still is ══════════
#
# session_run reports `artifacts_created` by diffing the workspace against a
# snapshot taken earlier in the call. A dependency install writes into that
# SAME workspace (uv's --target dir, npm's node_modules, cache trees) — if
# the snapshot were taken before the install, those installer byproducts
# would be indistinguishable from files the PROGRAM wrote, and would be
# reported (and, on session_run, inlined as content blocks) as if the
# executed code had produced them. The snapshot must be taken AFTER the
# install and before the entry file runs, so installed files are already
# part of the baseline and only genuinely new/modified files are reported.

sid3 = sessions.start("python3")["session_id"]
try:
    write_result3 = _session_service.write_file(
        sid3, "main.py", 'open("output.txt", "w").write("hi")\n')
    check("artifact-snapshot fixture: entry file written",
          write_result3.get("ok") is True, f"-> {write_result3}")

    with _StubInstall(write_bytes=64) as stub_sr3:
        sr_result3 = _session_service.run_file(
            sid3, "main.py", dependencies=["fakepkg"])
    check("artifact-snapshot ordering: the install actually landed in the session",
          len(stub_sr3.calls) == 1, f"-> {stub_sr3.calls}")
    check("artifact-snapshot ordering: run_file itself succeeded",
          sr_result3.get("ok") is True, f"-> {sr_result3}")

    created_paths = {a["path"] for a in (sr_result3.get("artifacts_created") or [])}
    check("artifact-snapshot ordering: the installed dependency's own file is "
          "NOT reported as a run artifact",
          "fakepkg.bin" not in created_paths, f"-> {created_paths}")
    check("artifact-snapshot ordering: the file the ENTRY FILE wrote still is",
          "output.txt" in created_paths, f"-> {created_paths}")
finally:
    sessions.stop(sid3)


# ═══ 10. compact mode never drops a non-empty `dependencies` disclosure ═══
# Same reasoning `_COMPACT_DISCLOSURE` already applies to `unenforced` and
# `artifacts_created` (#117): a compact caller has no full-envelope reply to
# fall back to reading, so a dropped `dependencies` would silently hide a
# failed or budget/quota-refused install from exactly the caller least able
# to notice.

from codecalc import server as _srv976

check("compact_result keeps a non-empty `dependencies`",
      _srv976.compact_result(
          {"ok": True, "verdict": "OK", "stdout": "", "exit_code": 0,
           "dependencies": [{"spec": "requests", "language": "python3",
                             "ok": True, "installer": "uv", "elapsed_ms": 5}]}
      ).get("dependencies") == [{"spec": "requests", "language": "python3",
                                 "ok": True, "installer": "uv", "elapsed_ms": 5}])
check("compact_result omits `dependencies` when the run declared none",
      "dependencies" not in _srv976.compact_result(
          {"ok": True, "verdict": "OK", "stdout": "", "exit_code": 0}))


# ═══ 6. this module imports nothing network-related ═══════════════════════

import re as _re

_src = (REPO_ROOT / "codecalc" / "dependencies.py").read_text(encoding="utf-8")
_imports = _re.findall(r"^\s*(?:import|from)\s+([\w.]+)", _src, _re.M)
_network_modules = ("urllib.request", "urllib3", "http.client", "httplib", "requests",
                    "httpx", "aiohttp", "socket", "ftplib", "smtplib", "telnetlib",
                    "xmlrpc.client", "websockets")
_offending = sorted({i for i in _imports if any(
    i == n or i.startswith(n + ".") for n in _network_modules)})
check("codecalc/dependencies.py imports nothing that can reach the network",
      not _offending, f"-> {_offending}")
check("codecalc/dependencies.py embeds no outbound URL",
      "http://" not in _src and "https://" not in _src)


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL DEPENDENCY TESTS PASS ===")
sys.exit(1 if FAILS else 0)
