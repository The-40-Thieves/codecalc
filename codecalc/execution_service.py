"""Protocol-neutral execution application service."""

from __future__ import annotations

import dataclasses
import tempfile

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
from . import (
    dependencies as dependencies_module,
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
#: providers.py in the fix-round merge — see attach_receipt), the
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
    fix-round bug. Returns `(run_spec, decision, rejection_or_None)`:

    - `run_spec` is the possibly-narrowed spec to actually run (a denied network
      forces `no_net` only where the provider can enforce it — see
      `capabilities.enforced_spec`); `spec` (the request as written) still names
      the receipt.
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
        # capability policy. An explicit `policy` pins it (tests); else
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
        enforce identically (fix round: run_submit used to bypass this)."""
        return broker_run(spec, provider, policy=self._policy(),
                          audit=self.audit, session_id=session_id)

    def execute(self, spec: ComputationSpec, *, provider_id: str | None = None,
               dependencies: list[str] | None = None) -> dict:
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

        # Parse/merge a PEP 723 block (python3) with the explicit
        # `dependencies` argument BEFORE brokering — a malformed block is a
        # `validation` refusal regardless of capability policy, and costs
        # nothing to check first.
        dep_specs, dep_parse_error, dep_implicit = dependencies_module.resolve(
            spec.language, spec.code, dependencies)
        if dep_parse_error is not None:
            return contract.stamp(dep_parse_error)
        if dep_specs and dep_implicit:
            # Source text ALONE (a PEP 723 block, no `dependencies` argument)
            # is about to start a confined install — logged distinctly from an
            # explicit `install_package`/`dependencies=` call so a reader of
            # the trail can tell which one happened. Logged here, at the
            # point of detection, regardless of whether the refusal check
            # below then blocks it — a refused attempt is still worth knowing
            # source tried to trigger.
            self.audit.emit(
                audit_module.DEPENDENCY_INSTALL_IMPLICIT,
                decision="detected", language=spec.language,
                dependency_count=len(dep_specs),
            )

        # broker capabilities BEFORE any side effect. `run_spec` is the
        # possibly-narrowed spec that actually runs (network denied -> no_net
        # where the provider can enforce it; left as-asked and disclosed
        # as effective where it cannot); `spec` (the request as written) still
        # names the receipt, so `spec_hash`
        # and `limits.requested` describe what was asked, and the `capabilities`
        # block describes what was approved and enforced.
        run_spec, decision, rejection = self._broker(spec, provider)
        if rejection is not None:
            return rejection

        dep_entries = None
        dep_workdir: str | None = None
        dep_workdir_identity = None
        if dep_specs:
            # Dependencies install through packages.py's own confined path,
            # which only this provider uses — see the module's docstring for
            # why other providers (Piston, a remote strict backend) are not
            # wired to it rather than guessed at.
            if provider.provider_id != "local":
                return contract.stamp(errors.error_result(
                    errors.VALIDATION,
                    f"provider {provider.provider_id!r} does not support "
                    "per-run dependencies",
                    provider_error="unsupported_capability",
                    requested_provider=provider.provider_id,
                    capability="dependencies",
                ))
            # THE SECURITY PROPERTY: never fetch when the run has no network —
            # checked against the run AS ASKED (spec.no_net), not run_spec,
            # which the broker may have already narrowed for the SANDBOXED
            # step; that narrowing is irrelevant here because the install
            # never enters the sandbox at all. See dependencies.network_refused.
            policy = self._policy()
            if dependencies_module.network_refused(no_net=spec.no_net, policy=policy):
                return contract.stamp(dependencies_module.refusal_result(spec.no_net, policy))
            # Sessionless: the Rust executor normally owns and deletes its own
            # temp workdir. With dependencies to install first, THIS layer
            # pre-creates the workdir, installs into it, and passes it as
            # `--workdir` — which the executor then treats as caller-supplied
            # and never deletes (see executor.py's `_rmtree_checked` docstring)
            # — so cleanup below is this service's own responsibility.
            dep_workdir = tempfile.mkdtemp(prefix="codecalc-deps-")
            dep_workdir_identity = executor._dir_identity(dep_workdir)
            dep_entries, failure = dependencies_module.install_dependencies(
                spec.language, dep_specs, session_id=None,
                workdir=dep_workdir, audit=self.audit)
            if failure is not None:
                # Never reached the executor — no receipt, same as every other
                # pre-execution refusal above (broker rejection, unsupported
                # capability): a receipt claims "this ran", which this did not.
                executor._rmtree_checked(dep_workdir, dep_workdir_identity)
                result = dict(failure)
                result["dependencies"] = dep_entries
                return contract.stamp(result)
            run_spec = dataclasses.replace(run_spec, workdir=dep_workdir)

        try:
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
                                                   capability_decision=decision,
                                                   receipt_spec=spec)
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
        finally:
            if dep_workdir is not None:
                executor._rmtree_checked(dep_workdir, dep_workdir_identity)
        if dep_entries is not None:
            result["dependencies"] = dep_entries
        return contract.stamp(attach_receipt(spec, provider, result,
                                             capability_decision=decision))

    def execute_session(self, session_service: SessionService, session_id: str,
                        spec: ComputationSpec, *,
                        provider_id: str | None = None,
                        dependencies: list[str] | None = None) -> dict:
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
        # Same parse/merge as the sessionless path, before brokering.
        dep_specs, dep_parse_error, dep_implicit = dependencies_module.resolve(
            spec.language, spec.code, dependencies)
        if dep_parse_error is not None:
            return contract.stamp(dep_parse_error)
        if dep_specs and dep_implicit:
            self.audit.emit(
                audit_module.DEPENDENCY_INSTALL_IMPLICIT,
                session_id=session_id, decision="detected",
                language=spec.language, dependency_count=len(dep_specs),
            )
        # broker before the worker runs; a denied network forces no_net
        # on the spec the session worker receives where the provider can enforce
        # it (the local provider on the rust backend does).
        run_spec, decision, rejection = self._broker(spec, provider,
                                                     session_id=session_id)
        if rejection is not None:
            return rejection
        dep_entries = None
        if dep_specs:
            policy = self._policy()
            if dependencies_module.network_refused(no_net=spec.no_net, policy=policy):
                return contract.stamp(dependencies_module.refusal_result(spec.no_net, policy))
            # A session's workspace is already the run's workdir — install
            # into it directly, the same target the worker/workspace process
            # already imports from (packages.install's session_id branch).
            dep_entries, failure = dependencies_module.install_dependencies(
                spec.language, dep_specs, session_id=session_id,
                workdir=None, audit=self.audit)
            if failure is not None:
                # Never reached the worker/workspace — no receipt, same
                # reasoning as the sessionless path above.
                result = dict(failure)
                result["dependencies"] = dep_entries
                return contract.stamp(result)
        result = dict(session_service.execute(session_id, run_spec))
        if dep_entries is not None:
            result["dependencies"] = dep_entries
        # F6 (cross-vendor): name the session in the receipt. The spec is
        # identical across sessions, so without this two sessions running the
        # same code produced byte-identical receipts despite different state.
        return contract.stamp(attach_receipt(spec, provider, result,
                                             session_id=session_id,
                                             capability_decision=decision))

    async def execute_stream(self, spec: ComputationSpec, *, provider_id: str | None = None,
                             dependencies: list[str] | None = None,
                             on_progress=None) -> dict:
        """Stream through the selected provider using protocol-neutral progress.

        `dependencies` mirrors `execute()`'s sessionless path exactly: parsed/
        merged with a PEP 723 block (python3) before brokering, refused under
        `no_net`/a deny-network or strict policy, installed into a per-run
        workdir BEFORE the provider is ever asked to stream. A refusal or
        install failure is returned directly, with `on_progress` never called
        — the tool's single reply is the stream's first and only event.
        """
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

        # Same parse/merge as execute()'s sessionless path, before brokering.
        dep_specs, dep_parse_error, dep_implicit = dependencies_module.resolve(
            spec.language, spec.code, dependencies)
        if dep_parse_error is not None:
            return contract.stamp(dep_parse_error)
        if dep_specs and dep_implicit:
            self.audit.emit(
                audit_module.DEPENDENCY_INSTALL_IMPLICIT,
                decision="detected", language=spec.language,
                dependency_count=len(dep_specs),
            )

        run_spec, decision, rejection = self._broker(spec, provider)
        if rejection is not None:
            return rejection

        dep_entries = None
        dep_workdir: str | None = None
        dep_workdir_identity = None
        if dep_specs:
            if provider.provider_id != "local":
                return contract.stamp(errors.error_result(
                    errors.VALIDATION,
                    f"provider {provider.provider_id!r} does not support "
                    "per-run dependencies",
                    provider_error="unsupported_capability",
                    requested_provider=provider.provider_id,
                    capability="dependencies",
                ))
            policy = self._policy()
            if dependencies_module.network_refused(no_net=spec.no_net, policy=policy):
                return contract.stamp(dependencies_module.refusal_result(spec.no_net, policy))
            dep_workdir = tempfile.mkdtemp(prefix="codecalc-deps-")
            dep_workdir_identity = executor._dir_identity(dep_workdir)
            dep_entries, failure = dependencies_module.install_dependencies(
                spec.language, dep_specs, session_id=None,
                workdir=dep_workdir, audit=self.audit)
            if failure is not None:
                executor._rmtree_checked(dep_workdir, dep_workdir_identity)
                result = dict(failure)
                result["dependencies"] = dep_entries
                return contract.stamp(result)
            run_spec = dataclasses.replace(run_spec, workdir=dep_workdir)

        try:
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
        finally:
            if dep_workdir is not None:
                executor._rmtree_checked(dep_workdir, dep_workdir_identity)
        if dep_entries is not None:
            result["dependencies"] = dep_entries
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

    def __init__(self, *, audit: audit_module.AuditLog | None = None) -> None:
        # Same default-to-no-op shape as `ExecutionService.__init__`: an
        # explicit `audit` wins (server.py passes the real one so
        # `session_run`'s dependency installs are audited exactly like
        # `execute_code`'s); a no-op sink otherwise, so every OTHER method on
        # this class — none of which needed an audit log before — stays
        # unaffected by callers that construct `SessionService()` bare (tests
        # included).
        self.audit = audit if audit is not None else audit_module.AuditLog(None)

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
        try:
            if not sessions._session_dir(session_id).is_dir():
                return {"ok": False, "error": f"unknown session '{session_id}'"}
            resource = sessions.resource_read(session_id, path)
        except ValueError as exc:
            # #212: `_session_dir`/`_jail` (inside resource_read) REFUSE a
            # malformed session id or an out-of-workspace path by raising —
            # this tool-facing method returns the result contract like every
            # other branch here, never an exception.
            return sessions._guard_error(exc)
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
                 timeout: int = 30,
                 dependencies: list[str] | None = None) -> dict:
        """Run a workspace entry file as a fresh process in its session."""
        try:
            workdir = sessions._session_dir(session_id)
            if not workdir.is_dir():
                return {"ok": False, "error": f"unknown session '{session_id}'"}
            # refused before the entry file even runs. `entry_file`
            # is about to run as a fresh process that can write anything into
            # the workspace, same as sessions.execute()'s workspace branch —
            # see `quota_precheck`'s docstring for why re-measuring here
            # (rather than a sticky flag) is what makes "refused until freed"
            # self-healing.
            quota_refusal = sessions.quota_precheck(session_id)
            if quota_refusal is not None:
                return quota_refusal
            resource = sessions.resource_read(session_id, entry_file)
        except ValueError as exc:
            return sessions._guard_error(exc)  # #212, see read_file's comment above
        if resource is None:
            return {"ok": False, "error": f"no such file or file too large: {entry_file}"}
        data, _mime_type = resource
        source = data.decode(errors="replace")
        if language is None:
            extension = entry_file.rsplit(".", 1)[-1] if "." in entry_file else ""
            by_extension = {value: key for key, value in registry.EXTENSIONS.items()}
            language = by_extension.get(extension, "python3")
        # Parse/merge before running. session_run has no per-call
        # `no_net` of its own (the sandboxed step here has always run with
        # network open — see the module docstring), so the refusal check is
        # policy-only: `no_net=False` unconditionally, `network_refused` still
        # catches a deny-network/strict CODECALC_CAPABILITY_POLICY.
        dep_specs, dep_parse_error, dep_implicit = dependencies_module.resolve(
            language, source, dependencies)
        if dep_parse_error is not None:
            return dep_parse_error
        if dep_specs and dep_implicit:
            self.audit.emit(
                audit_module.DEPENDENCY_INSTALL_IMPLICIT,
                session_id=session_id, decision="detected",
                language=language, dependency_count=len(dep_specs),
            )
        dep_entries = None
        if dep_specs:
            policy = capabilities.policy_from_env()
            if dependencies_module.network_refused(no_net=False, policy=policy):
                return dependencies_module.refusal_result(False, policy)
            dep_entries, failure = dependencies_module.install_dependencies(
                language, dep_specs, session_id=session_id, workdir=None,
                audit=self.audit)
            if failure is not None:
                result = dict(failure)
                result["dependencies"] = dep_entries
                return result
        # Taken AFTER any dependency install, immediately before the entry
        # file runs — deliberately NOT at its original position (right after
        # quota_precheck, before resource_read/dependency resolution). A
        # dependency install into this session's own workspace (uv's
        # --target dir, npm's node_modules, both managers' .cache/ trees) IS
        # a filesystem write to `workdir`, and `_classify_new_artifacts`
        # reports anything ABSENT from `before` as something the run
        # "created or modified" — so a snapshot taken before the install
        # would misreport uv/npm's own byproducts as artifacts THE PROGRAM
        # produced. Snapshotting here instead treats the just-installed tree
        # as part of the baseline, so only what the entry file itself
        # writes below is ever reported — see tests/test_dependencies.py's
        # "artifact-snapshot ordering" section, which asserts this directly
        # (an installed file does NOT appear in `artifacts_created`; a file
        # the entry file itself writes does). `quota_postcheck` below still
        # gets the same guarantee sessions.execute() promises: "before the
        # entry file runs" — the entry file has not run yet at this point
        # either way.
        before = sessions._artifact_snapshot(workdir)
        # captured at the larger spill ceiling and re-truncated to
        # the executor's own default cap, same as sessions.execute()'s
        # workspace branch — session_run has no caller-facing max_output_kb
        # of its own to honour instead (see sessions.SPILL_CAPTURE_KB).
        result = executor.execute(
            language,
            source,
            stdin=stdin,
            timeout=timeout,
            workdir=str(workdir),
            max_output_kb=sessions.SPILL_CAPTURE_KB,
        )
        result = sessions.spill_if_truncated(session_id, result, 0)
        result["entry_file"] = entry_file
        result["language"] = language
        if dep_entries is not None:
            result["dependencies"] = dep_entries
        # point 4: the entry file just ran arbitrary code with the
        # workspace as its cwd — measure what it left behind and disclose an
        # over-quota session rather than let it grow silently forever.
        return sessions.quota_postcheck(session_id, result, d=workdir, before=before)
