"""Native streaming honours the caller's workdir (issue #352).

`execution_service.execute_stream` installs per-run dependencies into a
`codecalc-deps-*` directory and hands the provider a spec whose `workdir` is
that directory. The native branch of `executor.execute_stream` used to ignore
it: it always ran in a fresh `codecalc-stream-*` directory, so a dependency
installed for the run was invisible to a native streaming program. The Python
fallback forwarded the workdir, so the two backends disagreed.

The contract these checks pin:
  - a supplied, non-empty `spec.workdir` is the `--workdir` the native binary
    gets, progress is read from `<workdir>/.codecalc-run/run.out`, and the
    directory survives the call (its owner removes it, not the stream)
  - with no supplied workdir, the adapter allocates one, and removes ONLY that
    owned directory on success, on exception and on cancellation
  - the Python fallback still forwards the supplied workdir
  - end to end, through the execution service with `install_dependencies`
    stubbed to write a module into the dependency directory, a streamed native
    program can import it (skipped when the native binary is not built)

Standalone runner (check()/FAILS/sys.exit), no pytest: the repo convention.
The native binary is never spawned here except in the integration case, and
that case only runs when `executor._rust` already resolves to a real binary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import shutil
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import dependencies, execution_service, executor, providers

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def _result_json(**extra) -> bytes:
    body = {"ok": True, "verdict": "OK", "stdout": "hello\n", "stderr": "",
            "exit_code": 0, "unenforced": []}
    body.update(extra)
    return json.dumps(body).encode()


async def _native_ok(workdir: str) -> bytes:
    """Plays the native binary: writes progress into <workdir>/.codecalc-run/
    the way main.rs does, in two chunks so the progress loop sees both."""
    scratch = pathlib.Path(workdir) / ".codecalc-run"
    scratch.mkdir(exist_ok=True)
    (scratch / "run.out").write_bytes(b"he")
    await asyncio.sleep(0.6)
    (scratch / "run.out").write_bytes(b"hello\n")
    await asyncio.sleep(0.3)
    return _result_json()


async def _native_raise(workdir: str) -> bytes:
    raise RuntimeError("boom")


async def _native_hang(workdir: str) -> bytes:
    await asyncio.sleep(60)
    return _result_json()


class _FakeStdin:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self, workdir: str, behaviour) -> None:
        self.stdin = _FakeStdin()
        self.returncode = None
        self._workdir = workdir
        self._behaviour = behaviour

    async def communicate(self) -> tuple[bytes, bytes]:
        return await self._behaviour(self._workdir), b""

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int | None:
        return self.returncode


class _Spawn:
    """Records the argv of every spawn and returns a scripted fake process."""

    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.args: list[str] = []
        self.proc: _FakeProc | None = None
        self.started = asyncio.Event()

    async def __call__(self, *args, **kwargs) -> _FakeProc:
        self.args = list(args)
        self.proc = _FakeProc(_workdir_arg(self.args), self.behaviour)
        self.started.set()
        return self.proc


def _workdir_arg(args: list[str]) -> str:
    return args[args.index("--workdir") + 1]


@contextlib.contextmanager
def _fake_native(behaviour):
    saved_rust = executor._rust
    saved_spawn = asyncio.create_subprocess_exec
    spawn = _Spawn(behaviour)
    executor._rust = "/nonexistent/codecalc-exec"
    asyncio.create_subprocess_exec = spawn
    try:
        yield spawn
    finally:
        executor._rust = saved_rust
        asyncio.create_subprocess_exec = saved_spawn


def _spec(code: str = "print('hello')", **kwargs) -> providers.ComputationSpec:
    return providers.ComputationSpec(language="python3", code=code, **kwargs)


def _collector():
    seen: list[tuple[int, str]] = []

    async def on_progress(value: int, message: str) -> None:
        seen.append((value, message))

    return seen, on_progress


# ── 1. native stream + supplied workdir ─────────────────────────────────────
supplied = tempfile.mkdtemp(prefix="stream-workdir-supplied-")
try:
    pathlib.Path(supplied, "fixture.txt").write_text("kept")
    seen, on_progress = _collector()
    with _fake_native(_native_ok) as spawn:
        result = asyncio.run(executor.execute_stream(_spec(workdir=supplied),
                                                     on_progress=on_progress))
    argv_workdir = _workdir_arg(spawn.args) if spawn.args else None
    check("native stream + supplied workdir: --workdir is the supplied directory",
          argv_workdir == supplied, f"-> --workdir={argv_workdir!r} supplied={supplied!r}")
    check("native stream + supplied workdir: progress is read from <workdir>/.codecalc-run/run.out",
          bool(seen) and seen[-1][0] == len(b"hello\n"), f"-> progress={seen}")
    check("native stream + supplied workdir: the result is the native result",
          result.get("ok") is True and result.get("streamed") is True,
          f"-> ok={result.get('ok')} streamed={result.get('streamed')}")
    check("native stream + supplied workdir: the supplied directory survives the call",
          pathlib.Path(supplied, "fixture.txt").read_text() == "kept",
          f"-> exists={pathlib.Path(supplied).is_dir()}")
finally:
    shutil.rmtree(supplied, ignore_errors=True)


# ── 2. native stream + no workdir: the owned directory, on every exit path ──
def _owned_dir(spawn: _Spawn) -> str:
    return _workdir_arg(spawn.args)


with _fake_native(_native_ok) as spawn:
    result = asyncio.run(executor.execute_stream(_spec()))
owned = _owned_dir(spawn)
check("native stream, no workdir: a fresh codecalc-stream-* directory is allocated",
      pathlib.Path(owned).name.startswith("codecalc-stream-"), f"-> {owned}")
check("native stream, no workdir: the owned directory is removed on success",
      result.get("ok") is True and not pathlib.Path(owned).exists(), f"-> exists={pathlib.Path(owned).exists()}")

with _fake_native(_native_raise) as spawn:
    result = asyncio.run(executor.execute_stream(_spec()))
owned = _owned_dir(spawn)
check("native stream, no workdir: the owned directory is removed on exception",
      result.get("ok") is False and not pathlib.Path(owned).exists(),
      f"-> result={result.get('error')!r} exists={pathlib.Path(owned).exists()}")


async def _cancel_midstream(spawn: _Spawn):
    task = asyncio.create_task(executor.execute_stream(_spec()))
    await spawn.started.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return True
    return False


with _fake_native(_native_hang) as spawn:
    cancelled = asyncio.run(_cancel_midstream(spawn))
owned = _owned_dir(spawn)
check("native stream, no workdir: CancelledError still propagates",
      cancelled is True, f"-> cancelled={cancelled}")
check("native stream, no workdir: the owned directory is removed on cancellation",
      not pathlib.Path(owned).exists(), f"-> exists={pathlib.Path(owned).exists()}")


# ── 3. Python fallback still forwards the supplied workdir ──────────────────
fallback_calls: list[dict] = []


def _fake_execute(language, code, **kwargs):
    fallback_calls.append(kwargs)
    return {"ok": True, "verdict": "OK", "stdout": "", "stderr": "", "exit_code": 0,
            "unenforced": []}


supplied_fb = tempfile.mkdtemp(prefix="stream-workdir-fallback-")
saved_execute = executor.execute
saved_rust = executor._rust
executor.execute = _fake_execute
executor._rust = None
try:
    fallback_result = asyncio.run(executor.execute_stream(_spec(workdir=supplied_fb)))
finally:
    executor.execute = saved_execute
    executor._rust = saved_rust
    shutil.rmtree(supplied_fb, ignore_errors=True)
check("fallback stream + supplied workdir: forwarded to execute() unchanged",
      bool(fallback_calls) and fallback_calls[0].get("workdir") == supplied_fb,
      f"-> {fallback_calls}")
check("fallback stream: still reports streamed=False",
      fallback_result.get("streamed") is False, f"-> streamed={fallback_result.get('streamed')}")


# ── 4. integration: per-run dependency importable by a native streamed run ──
if executor._rust is None:
    print("SKIP integration: native executor binary not found "
          "(build executor/ or set CODECALC_EXEC_BIN)")
else:
    def _install_fixture(language, specs, *, session_id=None, workdir=None,
                         audit=None, **kwargs):
        pathlib.Path(workdir, "fixturemod.py").write_text("VALUE = 'from-deps'\n")
        return [{"spec": s, "language": language, "ok": True, "installer": "stub"}
                for s in specs], None

    saved_install = dependencies.install_dependencies
    dependencies.install_dependencies = _install_fixture
    try:
        registry = providers.ProviderRegistry(default_provider_id="local")
        registry.register(providers.LocalExecutionProvider())
        service = execution_service.ExecutionService(registry)
        seen, on_progress = _collector()
        integ = asyncio.run(service.execute_stream(
            _spec(code="import fixturemod\nprint(fixturemod.VALUE)\n"),
            dependencies=["fixturemod"], on_progress=on_progress))
    finally:
        dependencies.install_dependencies = saved_install
    check("integration: native streamed program imports the per-run dependency",
          integ.get("ok") is True and "from-deps" in (integ.get("stdout") or ""),
          f"-> ok={integ.get('ok')} stdout={integ.get('stdout')!r} error={integ.get('error')!r}")


print(f"\n{CHECKS} checks, {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
