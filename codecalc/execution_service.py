"""Protocol-neutral execution application service (THE-790)."""

from __future__ import annotations

from . import (
    audit as audit_module,
)
from . import (
    capabilities,
    contract,
    errors,
    executor,
    providers,
    registry,
    sessions,
)
from .providers import (
    ComputationSpec,
    ProviderOperationFailure,
    ProviderRegistry,
    UnknownProvider,
    UnsupportedCapability,
    attach_receipt,
)
from .run_supervisor import RunSupervisor

#: Re-exported beside the module that now builds the receipt (moved to
#: providers.py in the THE-778 fix-round merge — see attach_receipt), the
#: same way providers.py itself re-exports COMPUTATION_SPEC_VERSION from
#: spec_identity: callers already import `execution_service`, and this
#: keeps `execution_service.RECEIPT_VERSION` a stable name across the move.
RECEIPT_VERSION = providers.RECEIPT_VERSION


def broker_run(spec: ComputationSpec, provider, *,
               policy: capabilities.CapabilityPolicy | None,
               audit: audit_module.AuditLog, session_id: str | None = None):
    """Decide, audit, and enforce capabilities for ONE run.

    Shared by `ExecutionService` (the sync execute/stream/session paths) and by
    `server.run_submit` (the background path), so a policy the sync path enforces
    cannot be bypassed by submitting the same job to the background — the CRITICAL
    THE-787 fix-round bug. Returns `(run_spec, decision, rejection_or_None)`:

    - `run_spec` is the possibly-narrowed spec to actually run (a denied network
      forces `no_net`); `spec` (the request as written) still names the receipt.
    - `decision` is threaded onto the receipt so every path surfaces the four
      capability sets identically.
    - `rejection` is a stamped `PERMISSION_DENIED` result when the broker refuses
      (escalation, or strict + an unenforceable denial); the caller returns it
      WITHOUT starting any work.

    When no policy is active the decision is the passthrough disclosure
    (approved == requested, `brokered: false`) and the spec is unchanged.
    """
    decision = capabilities.decide(spec, provider.describe(), policy)
    if decision.rejected:
        audit.emit(
            audit_module.CAPABILITY_REJECTED,
            session_id=session_id,
            decision="rejected",
            reason=decision.reason,
            provider_id=provider.provider_id,
            provider_error=decision.provider_error,
            requested=sorted(decision.requested),
            policy=decision.policy_source or None,
        )
        rejection = contract.stamp(capabilities.rejection_result(
            decision, provider_id=provider.provider_id))
        return spec, decision, rejection
    if decision.brokered:
        audit.emit(
            audit_module.CAPABILITY_DENIED if decision.denied
            else audit_module.CAPABILITY_APPROVED,
            session_id=session_id,
            decision="denied" if decision.denied else "approved",
            provider_id=provider.provider_id,
            requested=sorted(decision.requested),
            approved=sorted(decision.approved),
            denied=sorted(decision.denied),
            policy=decision.policy_source or None,
        )
    return capabilities.enforced_spec(spec, decision), decision, None


class ExecutionService:
    """Select an execution provider and preserve CodeCalc's result contract."""

    _VERIFICATION_FIELDS = ("ok", "verdict", "stdout", "stderr", "exit_code")

    def __init__(self, registry: ProviderRegistry,
                 *, supervisor: RunSupervisor | None = None,
                 audit: audit_module.AuditLog | None = None,
                 policy: capabilities.CapabilityPolicy | None = None,
                 policy_from_env: bool = True) -> None:
        self.registry = registry
        self.supervisor = supervisor
        # An explicit AuditLog wins; otherwise a no-op sink, so brokering never
        # depends on a configured audit (server.py passes the real one).
        self.audit = audit if audit is not None else audit_module.AuditLog(None)
        # THE-787 capability policy. An explicit `policy` pins it (tests); else
        # it is read from CODECALC_CAPABILITY_POLICY per call unless
        # `policy_from_env=False` forces brokering fully off. Read per call, not
        # cached, so an operator's change takes effect without a restart — same
        # reasoning as packages._allowlist().
        self._explicit_policy = policy
        self._policy_from_env = policy_from_env

    def _policy(self) -> capabilities.CapabilityPolicy | None:
        if self._explicit_policy is not None:
            return self._explicit_policy
        if not self._policy_from_env:
            return None
        return capabilities.policy_from_env()

    def _broker(self, spec: ComputationSpec, provider, *,
                session_id: str | None = None):
        """Broker `spec` against the active policy. Thin wrapper over the shared
        `broker_run` so the sync service and the background `run_submit` path
        enforce identically (THE-787 fix round: run_submit used to bypass this)."""
        return broker_run(spec, provider, policy=self._policy(),
                          audit=self.audit, session_id=session_id)

    def execute(self, spec: ComputationSpec, *, provider_id: str | None = None) -> dict:
        try:
            provider = self.registry.select(provider_id, spec=spec)
        except UnknownProvider as exc:
            return contract.stamp(errors.error_result(
                errors.VALIDATION,
                str(exc),
                provider_error=exc.code,
                requested_provider=exc.provider_id,
                available_providers=list(exc.available),
            ))

        # THE-787: broker capabilities BEFORE any side effect. `run_spec` is the
        # possibly-narrowed spec that actually runs (network denied -> no_net);
        # `spec` (the request as written) still names the receipt, so `spec_hash`
        # and `limits.requested` describe what was asked, and the `capabilities`
        # block describes what was approved and enforced.
        run_spec, decision, rejection = self._broker(spec, provider)
        if rejection is not None:
            return rejection

        try:
            if provider.describe()["capabilities"].get("managed_runs"):
                if self.supervisor is None:
                    return contract.stamp(errors.error_result(
                        errors.INTERNAL,
                        "managed execution provider requires a run supervisor",
                        provider_error="run_supervisor_unavailable",
                        requested_provider=provider.provider_id,
                    ))
                handle = self.supervisor.start(run_spec, provider_id=provider.provider_id,
                                               capability_decision=decision)
                cleanup_failure = None
                try:
                    result = dict(self.supervisor.wait(
                        handle.run_id, timeout=run_spec.timeout + 10
                    ))
                finally:
                    try:
                        self.supervisor.cleanup(handle.run_id)
                        self.audit.emit(
                            audit_module.CLEANUP, run_id=handle.run_id,
                            decision="cleaned", provider_id=provider.provider_id)
                    except ProviderOperationFailure as exc:
                        cleanup_failure = exc
                        self.audit.emit(
                            audit_module.CLEANUP, run_id=handle.run_id,
                            decision="cleanup_failed", reason=str(exc),
                            provider_id=provider.provider_id)
                if cleanup_failure is not None:
                    # F3 (cross-vendor): cleanup is best-effort and must NOT
                    # discard the collected result. Replacing a good result
                    # (stdout/verdict/receipt) with an internal error over a
                    # provider-side resource-release problem loses exactly what
                    # the caller asked for. Mirror run_inspect (server.py): keep
                    # the result and append `cleanup_error`.
                    result["cleanup_error"] = str(cleanup_failure)
            else:
                result = dict(provider.execute(run_spec))
        except UnsupportedCapability as exc:
            return contract.stamp(errors.error_result(
                errors.VALIDATION,
                str(exc),
                provider_error=exc.code,
                requested_provider=exc.provider_id,
                capability=exc.capability,
            ))
        return contract.stamp(attach_receipt(spec, provider, result,
                                             capability_decision=decision))

    def execute_session(self, session_service: SessionService, session_id: str,
                        spec: ComputationSpec, *,
                        provider_id: str | None = None) -> dict:
        """Execute a CodeCalc workspace session without changing providers."""
        try:
            provider = self.registry.select(provider_id, spec=spec)
        except UnknownProvider as exc:
            return contract.stamp(errors.error_result(
                errors.VALIDATION,
                str(exc),
                provider_error=exc.code,
                requested_provider=exc.provider_id,
                available_providers=list(exc.available),
            ))
        if provider.provider_id != "local":
            exc = UnsupportedCapability(provider.provider_id, "sessions")
            return contract.stamp(errors.error_result(
                errors.VALIDATION,
                str(exc),
                provider_error=exc.code,
                requested_provider=exc.provider_id,
                capability=exc.capability,
            ))
        # THE-787: broker before the worker runs; a denied network forces no_net
        # on the spec the session worker receives.
        run_spec, decision, rejection = self._broker(spec, provider,
                                                     session_id=session_id)
        if rejection is not None:
            return rejection
        result = dict(session_service.execute(session_id, run_spec))
        # F6 (cross-vendor): name the session in the receipt. The spec is
        # identical across sessions, so without this two sessions running the
        # same code produced byte-identical receipts despite different state.
        return contract.stamp(attach_receipt(spec, provider, result,
                                             session_id=session_id,
                                             capability_decision=decision))

    async def execute_stream(self, spec: ComputationSpec, *, provider_id: str | None = None,
                             on_progress=None) -> dict:
        """Stream through the selected provider using protocol-neutral progress."""
        try:
            provider = self.registry.select(provider_id, spec=spec)
        except UnknownProvider as exc:
            return contract.stamp(errors.error_result(
                errors.VALIDATION,
                str(exc),
                provider_error=exc.code,
                requested_provider=exc.provider_id,
                available_providers=list(exc.available),
            ))
        run_spec, decision, rejection = self._broker(spec, provider)
        if rejection is not None:
            return rejection
        try:
            result = dict(await provider.execute_stream(run_spec, on_progress=on_progress))
        except UnsupportedCapability as exc:
            return contract.stamp(errors.error_result(
                errors.VALIDATION,
                str(exc),
                provider_error=exc.code,
                requested_provider=exc.provider_id,
                capability=exc.capability,
            ))
        return contract.stamp(attach_receipt(spec, provider, result,
                                             capability_decision=decision))

    def verify_across_providers(self, spec: ComputationSpec,
                                first_provider_id: str,
                                second_provider_id: str) -> dict:
        """Execute one canonical request twice and compare semantic outputs."""
        results = [
            self.execute(spec, provider_id=first_provider_id),
            self.execute(spec, provider_id=second_provider_id),
        ]
        comparison = {
            field: results[0].get(field) == results[1].get(field)
            for field in self._VERIFICATION_FIELDS
        }
        agreement = all(comparison.values())
        return contract.stamp({
            "ok": agreement and all(result.get("ok") is True for result in results),
            "agreement": agreement,
            "comparison": comparison,
            "results": results,
        })


class SessionService:
    """Protocol-neutral session lifecycle, workspace, and artifact service."""

    def start(self, language: str = "python3") -> dict:
        return sessions.start(language)

    def execute(self, session_id: str, spec: ComputationSpec) -> dict:
        return sessions.execute(
            session_id,
            spec.code,
            language=spec.language,
            stdin=spec.stdin,
            timeout=spec.timeout,
            max_memory_mb=spec.max_memory_mb,
            max_output_kb=spec.max_output_kb,
            max_cpu=spec.max_cpu,
            no_net=spec.no_net,
        )

    def stop(self, session_id: str) -> dict:
        return sessions.stop(session_id)

    def list_sessions(self) -> dict:
        return sessions.list_sessions()

    def list_files(self, session_id: str, path: str = "", *,
                   page_size: int | None = None,
                   cursor: str | None = None) -> dict:
        result = sessions.list_files(session_id, path)
        if not result.get("ok") or page_size is None:
            return result
        if page_size < 1:
            return {"ok": False, "error": "page_size must be positive"}
        try:
            offset = int(cursor or "0")
        except ValueError:
            return {"ok": False, "error": "invalid cursor"}
        if offset < 0:
            return {"ok": False, "error": "invalid cursor"}
        page_size = min(page_size, 1000)
        files = result["files"]
        end = min(len(files), offset + page_size)
        result["files"] = files[offset:end]
        result["next_cursor"] = str(end) if end < len(files) else None
        return result

    def write_file(self, session_id: str, path: str, content: str) -> dict:
        return sessions.write_file(session_id, path, content)

    def artifacts(self, session_id: str) -> dict:
        return sessions.artifacts(session_id)

    def read_file(self, session_id: str, path: str, max_bytes: int = 65536,
                  *, as_image: bool = False) -> dict:
        """Read a workspace file without exposing MCP content types."""
        if max_bytes < 0:
            return {"ok": False, "error": "max_bytes must be non-negative"}
        if not sessions._session_dir(session_id).is_dir():
            return {"ok": False, "error": f"unknown session '{session_id}'"}
        resource = sessions.resource_read(session_id, path)
        if resource is None:
            return {"ok": False, "error": f"no such file or file too large: {path}"}
        data, mime_type = resource
        is_image = mime_type.startswith("image/")
        return {
            "ok": True,
            "path": path,
            "size": len(data),
            "content": data[:max_bytes].decode(errors="replace"),
            "content_bytes": data,
            "mime_type": mime_type if is_image else "application/octet-stream",
            "is_image": is_image or as_image,
            "truncated": len(data) > max_bytes,
            "resource": f"codecalc://session/{session_id}/files/{path}",
        }

    def run_file(self, session_id: str, entry_file: str,
                 language: str | None = None, stdin: str = "",
                 timeout: int = 30) -> dict:
        """Run a workspace entry file as a fresh process in its session."""
        workdir = sessions._session_dir(session_id)
        if not workdir.is_dir():
            return {"ok": False, "error": f"unknown session '{session_id}'"}
        resource = sessions.resource_read(session_id, entry_file)
        if resource is None:
            return {"ok": False, "error": f"no such file or file too large: {entry_file}"}
        data, _mime_type = resource
        if language is None:
            extension = entry_file.rsplit(".", 1)[-1] if "." in entry_file else ""
            by_extension = {value: key for key, value in registry.EXTENSIONS.items()}
            language = by_extension.get(extension, "python3")
        # THE-783: captured at the larger spill ceiling and re-truncated to
        # the executor's own default cap, same as sessions.execute()'s
        # workspace branch — session_run has no caller-facing max_output_kb
        # of its own to honour instead (see sessions.SPILL_CAPTURE_KB).
        result = executor.execute(
            language,
            data.decode(errors="replace"),
            stdin=stdin,
            timeout=timeout,
            workdir=str(workdir),
            max_output_kb=sessions.SPILL_CAPTURE_KB,
        )
        result = sessions.spill_if_truncated(session_id, result, 0)
        result["entry_file"] = entry_file
        result["language"] = language
        return result
