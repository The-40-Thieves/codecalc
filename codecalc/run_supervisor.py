"""Provider-neutral execution lifecycle and crash-recovery journal (THE-831)."""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import asdict, dataclass
from pathlib import Path

from .providers import ComputationSpec, ExecutionProvider, ProviderRegistry


@dataclass(frozen=True, slots=True)
class RunHandle:
    """Stable identity returned immediately after a provider accepts a run."""

    run_id: str
    provider_id: str
    started_at: float
    deadline: float


@dataclass(slots=True)
class _Run:
    handle: RunHandle
    provider: ExecutionProvider
    future: Future[dict]
    state: str = "running"
    result: dict | None = None
    cleaned: bool = False


class RunSupervisor:
    """Own run identity, cancellation, collection, cleanup, and recovery.

    The journal intentionally contains metadata only: source, stdin, output and
    credentials never become crash-recovery state. A process that restarts
    cannot reattach to an arbitrary provider's in-memory handle, so recovery is
    conservative: ask the recorded provider to cancel and clean the orphan.
    """

    def __init__(self, registry: ProviderRegistry, *, state_dir: Path,
                 max_completed: int = 128, workers: int = 8) -> None:
        if max_completed < 0:
            raise ValueError("max_completed must be non-negative")
        self.registry = registry
        self.state_dir = state_dir
        self.max_completed = max_completed
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="codecalc-run")
        self._lock = threading.RLock()
        self._runs: dict[str, _Run] = {}

    def _path(self, run_id: str) -> Path:
        return self.state_dir / f"{run_id}.json"

    def _write(self, run: _Run) -> None:
        payload = {
            **asdict(run.handle),
            "state": run.state,
            "cleaned": run.cleaned,
            "updated_at": time.time(),
        }
        destination = self._path(run.handle.run_id)
        temporary = destination.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(destination)

    def start(self, spec: ComputationSpec, *, provider_id: str | None = None) -> RunHandle:
        provider = self.registry.select(provider_id, spec=spec)
        now = time.time()
        handle = RunHandle(
            run_id=uuid.uuid4().hex,
            provider_id=provider.provider_id,
            started_at=now,
            deadline=now + max(0, spec.timeout),
        )
        managed = provider.describe()["capabilities"].get("managed_runs", False)
        operation = provider.execute_managed if managed else provider.execute
        arguments = (handle.run_id, spec) if managed else (spec,)
        future = self._pool.submit(operation, *arguments)
        run = _Run(handle=handle, provider=provider, future=future)
        with self._lock:
            self._runs[handle.run_id] = run
            self._write(run)
        return handle

    def _get(self, run_id: str) -> _Run:
        try:
            return self._runs[run_id]
        except KeyError:
            raise KeyError(f"unknown run {run_id!r}") from None

    def inspect(self, run_id: str) -> dict:
        with self._lock:
            run = self._get(run_id)
            if run.state == "running" and run.future.done():
                self._collect(run)
            return {
                **asdict(run.handle),
                "state": run.state,
                "cleaned": run.cleaned,
            }

    def _collect(self, run: _Run) -> dict:
        if run.result is None:
            run.result = dict(run.future.result())
            run.state = "finished"
            self._write(run)
        return run.result

    def wait(self, run_id: str, *, timeout: float | None = None) -> dict:
        with self._lock:
            run = self._get(run_id)
        try:
            run.future.result(timeout=timeout)
        except TimeoutError:
            raise TimeoutError(f"run {run_id!r} did not finish before wait timeout") from None
        with self._lock:
            return dict(self._collect(run))

    def cancel(self, run_id: str) -> dict:
        with self._lock:
            run = self._get(run_id)
            if run.state not in {"running", "cancelling"}:
                return {"run_id": run_id, "cancelled": False, "state": run.state}
            run.state = "cancelling"
            self._write(run)
        run.provider.cancel(run_id)
        return {"run_id": run_id, "cancelled": True, "state": "cancelling"}

    def cleanup(self, run_id: str) -> dict:
        with self._lock:
            run = self._get(run_id)
            if run.cleaned:
                return {"run_id": run_id, "cleaned": True, "already_cleaned": True}
            # Capability-gated, mirroring recover_orphans() below — which has
            # always checked this before calling provider.cleanup(). This site
            # did not, and every provider that does not advertise `cleanup`
            # (LocalExecutionProvider among them: capabilities={"cleanup":
            # False, ...}) raises UnsupportedCapability unconditionally, so
            # calling this against anything but a managed remote provider
            # crashed. Latent rather than caught earlier: cleanup() was only
            # ever reached from ExecutionService.execute()'s managed-provider
            # branch, where managed_runs=True happened to always pair with
            # cleanup=True for the one provider that implements it. THE-778's
            # run_submit/run_inspect/run_cancel tools call this for ANY
            # provider a caller selects, which is what surfaced it.
            if run.provider.describe()["capabilities"].get("cleanup"):
                run.provider.cleanup(run_id)
            run.cleaned = True
            run.state = "cleaned"
            self._write(run)
            self._prune()
            return {"run_id": run_id, "cleaned": True, "already_cleaned": False}

    def _prune(self) -> None:
        completed = sorted(
            (path for path in self.state_dir.glob("*.json")),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in completed[self.max_completed:]:
            path.unlink(missing_ok=True)

    def recover_orphans(self) -> list[str]:
        recovered: list[str] = []
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("state") not in {"running", "cancelling"}:
                    continue
                provider = self.registry.select(str(record["provider_id"]))
                run_id = str(record["run_id"])
                capabilities = provider.describe()["capabilities"]
                # Capability-gated (THE-778), same reasoning as the `cleanup`
                # check two lines below, which this file already had and
                # `cancel` did not: a provider that does not advertise
                # `cancel` (LocalExecutionProvider among them) raises
                # UnsupportedCapability unconditionally, and the `except`
                # below does not catch that (deliberately — a RuntimeError
                # there is a real defect, not unreadable journal state), so
                # an orphaned LOCAL-provider run left over from a crash
                # aborted THIS loop entirely — which is called at server.py
                # IMPORT time, so it took the whole server down on the very
                # restart the journal exists to survive. For a provider that
                # cannot cancel, there is also nothing TO signal: a `local`
                # run is a subprocess of the now-dead PARENT process, already
                # gone with it, so recovery here is "nothing to do", not
                # "failed to do it".
                if capabilities.get("cancel"):
                    provider.cancel(run_id)
                if capabilities.get("cleanup"):
                    provider.cleanup(run_id)
                record["state"] = "recovered"
                record["cleaned"] = True
                path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
                recovered.append(run_id)
            except (KeyError, OSError, ValueError, TypeError, json.JSONDecodeError):
                # Preserve unreadable state for operator inspection. Deleting it
                # would turn a failed recovery into a false cleanup claim.
                continue
        self._prune()
        return recovered
