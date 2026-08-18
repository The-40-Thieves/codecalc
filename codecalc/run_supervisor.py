"""Provider-neutral execution lifecycle and crash-recovery journal (THE-831)."""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import asdict, dataclass
from pathlib import Path

from . import contract, errors
from .providers import (
    ComputationSpec,
    ExecutionProvider,
    ProviderRegistry,
    UnsupportedCapability,
    attach_receipt,
)

#: Non-terminal states: a run in one of these may still produce a result, so
#: its journal must never be pruned and its future may still complete.
_ACTIVE_STATES = frozenset({"running", "cancelling"})
#: Terminal states: the future is done and `result` (if any) is final. Only
#: journals in one of these are eligible for pruning.
_TERMINAL_STATES = frozenset({"finished", "cleaned", "recovered"})


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
    spec: ComputationSpec
    future: Future[dict]
    state: str = "running"
    result: dict | None = None
    cleaned: bool = False
    #: THE-787: the capability broker's decision for this run, threaded through
    #: so `_collect`'s receipt carries the same `capabilities` block a sync
    #: execute_code result does. In-memory only (not journalled): a recovered
    #: orphan is cancelled/cleaned, never re-collected into a receipt.
    capability_decision: object | None = None


class TooManyActiveRuns(RuntimeError):
    """The admission cap (`max_active_runs`) is already at capacity.

    A caller that never inspects/cancels its runs, or a burst of run_submit
    calls, would otherwise grow `_runs` and its background thread pool
    without bound — nothing else here rejects a submission. `active` and
    `limit` are carried so the message and any structured error built from
    them can both report the actual numbers rather than a generic refusal.
    """

    def __init__(self, active: int, limit: int) -> None:
        self.active = active
        self.limit = limit
        super().__init__(
            f"{active} runs are already active against a limit of {limit}"
        )


class RunSupervisor:
    """Own run identity, cancellation, collection, cleanup, and recovery.

    The journal intentionally contains metadata only: source, stdin, output and
    credentials never become crash-recovery state. A process that restarts
    cannot reattach to an arbitrary provider's in-memory handle, so recovery is
    conservative: ask the recorded provider to cancel and clean the orphan.
    """

    def __init__(self, registry: ProviderRegistry, *, state_dir: Path,
                 max_completed: int = 128, workers: int = 8,
                 max_active_runs: int = 64) -> None:
        if max_completed < 0:
            raise ValueError("max_completed must be non-negative")
        if max_active_runs < 1:
            raise ValueError("max_active_runs must be positive")
        self.registry = registry
        self.state_dir = state_dir
        self.max_completed = max_completed
        self.max_active_runs = max_active_runs
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

    def start(self, spec: ComputationSpec, *, provider_id: str | None = None,
              capability_decision: object | None = None) -> RunHandle:
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
        # Admission cap (THE-778 fix round, review Important #3a): nothing
        # else here rejects a submission, so an unbounded burst of
        # run_submit calls — or a caller that never inspects/cancels —
        # grows `_runs` and the thread pool without limit. Counted and
        # inserted under the SAME lock acquisition as a single atomic step,
        # not "check, then insert": two concurrent start() calls each
        # passing the check before either inserts would otherwise let the
        # active count exceed `max_active_runs` by more than one.
        # `self._pool.submit()` only enqueues and returns immediately, so
        # doing it here does not hold the lock through any real work.
        with self._lock:
            # F5 (cross-vendor): reap done futures BEFORE counting, so a run
            # that finished with nobody polling it stops occupying a slot.
            # Admission counts `_ACTIVE_STATES`, and a completed-but-uncollected
            # future sits in "running" until inspect()/wait() transitions it —
            # so a fast finished run made the next submit spuriously
            # RESOURCE_EXHAUSTED, contradicting the error's own "wait for one to
            # finish, then retry". Aligned with the state-aware prune: the state
            # a run RECORDS decides whether it is active, not the label it was
            # last left at.
            self._reap_completed_locked()
            active = sum(1 for r in self._runs.values() if r.state in _ACTIVE_STATES)
            if active >= self.max_active_runs:
                raise TooManyActiveRuns(active, self.max_active_runs)
            future = self._pool.submit(operation, *arguments)
            run = _Run(handle=handle, provider=provider, spec=spec, future=future,
                       capability_decision=capability_decision)
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
            # BOTH active states, not just "running" (THE-778 fix round,
            # review Critical #1). A run that was asked to cancel — whether
            # the attempt succeeded, failed, or was never actually made
            # because the provider does not support it — sits in
            # "cancelling" until something collects its future. Checking
            # only "running" here meant that state was a one-way door: once
            # entered, inspect() could never again see `future.done()` and
            # transition it onward, so a run whose own work simply finished
            # normally after a failed/unsupported cancel attempt polled
            # "cancelling" forever, even though wait() would have returned
            # the finished result immediately. The fix is symmetric with
            # cancel()'s own reasoning below: cancelling is a REQUEST, not a
            # promise, and the future's own completion is what actually
            # decides the outcome.
            if run.state in _ACTIVE_STATES and run.future.done():
                self._collect(run)
            return {
                **asdict(run.handle),
                "state": run.state,
                "cleaned": run.cleaned,
            }

    def _collect(self, run: _Run) -> dict:
        if run.result is None:
            try:
                result = dict(run.future.result())
                # Same receipt execute_code always carries (THE-778 fix round,
                # review Important #2): start() bypasses ExecutionService.execute(),
                # which is the only other place this gets attached, so without
                # this a run_inspect terminal result was missing `provider`
                # entirely — the one field a caller uses to see which limits the
                # provider actually enforced.
                run.result = attach_receipt(
                    run.spec, run.provider, result,
                    capability_decision=run.capability_decision)
            except Exception as exc:  # ANY failure must terminalize the run
                # F1 (cross-vendor): a provider whose future RAISES —
                # UnsupportedCapability, a transport error, or a failure while
                # attach_receipt() builds the provenance block — must not
                # strand the run in "running" forever with its admission slot
                # held and its result unreachable via inspect(). Turn ANY
                # exception into a TERMINAL, coded failure: inspectable exactly
                # once, freeing the slot, and returning the same error contract
                # every other failure does rather than re-raising inside a poll.
                # Same class as the cancel-stranding fix above, different
                # trigger (the future's own body raised).
                run.result = self._failed_result(run, exc)
            run.state = "finished"
            self._write(run)
            # THE-778 fix round, review Important #3b: pruning on every
            # terminal collection, not only from cleanup()/recover_orphans().
            # A caller that only ever calls run_submit + run_inspect (never
            # cleanup() directly) previously left the on-disk journal
            # growing without the max_completed bound ever being applied at
            # all until something ELSE happened to call cleanup().
            self._prune()
        return run.result

    def _failed_result(self, run: _Run, exc: Exception) -> dict:
        """A terminal, coded failure for a run whose future raised (F1).

        Stamped and coded so run_inspect returns the same error contract every
        other failure does. `provider_error` carries the exception's own stable
        `code` where it has one (UnsupportedCapability, ProviderOperationFailure)
        and its type name otherwise.
        """
        return contract.stamp(errors.error_result(
            errors.INTERNAL,
            f"background run failed: {exc}",
            provider_error=getattr(exc, "code", None) or type(exc).__name__,
            requested_provider=run.handle.provider_id,
            run_id=run.handle.run_id,
        ))

    def _reap_completed_locked(self) -> None:
        """Collect any run whose future is already done (F5). Caller holds
        `_lock`. `_collect` is lock-safe — inspect() already calls it under the
        same lock — and, post-F1, cannot raise even if the future's body did.
        """
        for run in list(self._runs.values()):
            if run.state in _ACTIVE_STATES and run.future.done():
                self._collect(run)

    def wait(self, run_id: str, *, timeout: float | None = None) -> dict:
        with self._lock:
            run = self._get(run_id)
        try:
            run.future.result(timeout=timeout)
        except TimeoutError:
            raise TimeoutError(f"run {run_id!r} did not finish before wait timeout") from None
        except Exception:
            # F1 (cross-vendor): block until the future settles, but do NOT
            # re-raise its body's exception here — `_collect()` below (under the
            # lock) is the single place that turns a raised future into a
            # TERMINAL failed result. Letting `future.result()` re-raise would
            # strand the caller with the provider's exception before that
            # normalization ever runs.
            pass
        with self._lock:
            return dict(self._collect(run))

    def cancel(self, run_id: str) -> dict:
        with self._lock:
            run = self._get(run_id)
            if run.state not in _ACTIVE_STATES:
                return {"run_id": run_id, "cancelled": False, "state": run.state}
            # Capability-checked BEFORE any state mutation (THE-778 fix
            # round, review Critical #1). A provider that does not
            # advertise `cancel` — LocalExecutionProvider among them — has
            # nothing to signal in the first place: raising here, with the
            # run untouched, leaves it exactly as if cancel() had never
            # been called, so its own work keeps running and reaches a
            # terminal state normally through inspect()/wait() as usual.
            # The alternative (transition to "cancelling" and let the
            # exception unwind) is what stranded the run in the bug this
            # fixes — even with inspect()'s widened condition above, a run
            # that is genuinely never going to be cancelled has no reason
            # to carry a "cancelling" label (on this object OR in the
            # on-disk journal) that nothing ever requested successfully.
            if not run.provider.describe()["capabilities"].get("cancel"):
                raise UnsupportedCapability(run.provider.provider_id, "cancel")
            run.state = "cancelling"
            self._write(run)
        try:
            run.provider.cancel(run_id)
        except Exception:
            # The provider DOES advertise cancel=True (checked above) but
            # this particular call still failed — a transport error on a
            # remote provider, say. Revert the state so the run can still
            # reach a terminal state normally, UNLESS a genuinely
            # concurrent collect() already moved it past "cancelling" (read
            # fresh, under the lock, not assumed from the local `run`
            # reference) — reverting a run that finished in the meantime
            # back to "running" would be the same class of data loss this
            # whole fix exists to prevent, in the other direction.
            with self._lock:
                if run.state == "cancelling":
                    run.state = "running"
                    self._write(run)
            raise
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
        """Delete the oldest journal files beyond `max_completed` — but ONLY
        ones recorded as terminal (THE-778 fix round, review Important #3b).

        Before this, every `*.json` in `state_dir` was sorted by mtime and
        anything past `max_completed` was deleted, with no read of what
        state it actually recorded. A journal is written once at `start()`
        and not touched again until its run finishes — so a long-running
        job's journal can be its OLDEST file by mtime while its run is very
        much still active, and a burst of shorter runs completing around it
        could prune it out from under a still-running job. The practical
        cost lands on `recover_orphans()`: if the process crashes before
        that run finishes, its journal is already gone, and there is
        nothing left to recover it from. Reading each candidate's own
        recorded `state` and excluding anything not in `_TERMINAL_STATES`
        makes a still-active run's journal ineligible for pruning no matter
        how old it is.
        """
        candidates: list[Path] = []
        for path in self.state_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                # Unreadable state is preserved, same policy
                # recover_orphans() already follows: deleting it would turn
                # an unreadable journal into a silently discarded one.
                continue
            if record.get("state") not in _TERMINAL_STATES:
                continue
            candidates.append(path)
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates[self.max_completed:]:
            path.unlink(missing_ok=True)

    def recover_orphans(self) -> list[str]:
        recovered: list[str] = []
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("state") not in _ACTIVE_STATES:
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
