"""Protocol-neutral execution application service (THE-790)."""

from __future__ import annotations

from . import contract, errors, executor, registry, sessions
from .providers import (
    ComputationSpec,
    ProviderRegistry,
    UnknownProvider,
    UnsupportedCapability,
)


def _limit_receipt(spec: ComputationSpec, result: dict) -> dict:
    disclosures = [str(item) for item in result.get("unenforced") or []]
    disclosure_text = "\n".join(disclosures).lower()
    requested_controls = (
        ("timeout", spec.timeout > 0, ("timeout",)),
        ("max_memory_mb", spec.max_memory_mb > 0, ("memory",)),
        ("max_output_kb", spec.max_output_kb > 0, ("output",)),
        ("max_cpu", spec.max_cpu > 0, ("cpu",)),
        ("no_net", spec.no_net, ("no_net", "network")),
    )
    reported_enforced = [
        name
        for name, requested, markers in requested_controls
        if requested and not any(marker in disclosure_text for marker in markers)
    ]
    return {
        "requested": {
            "timeout_seconds": spec.timeout,
            "max_memory_mb": spec.max_memory_mb,
            "max_output_kb": spec.max_output_kb,
            "max_cpu_seconds": spec.max_cpu,
            "no_net": spec.no_net,
        },
        "provider_reported_enforced": reported_enforced,
        "unenforced": disclosures,
    }


class ExecutionService:
    """Select an execution provider and preserve CodeCalc's result contract."""

    _VERIFICATION_FIELDS = ("ok", "verdict", "stdout", "stderr", "exit_code")

    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry

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

        try:
            result = dict(provider.execute(spec))
        except UnsupportedCapability as exc:
            return contract.stamp(errors.error_result(
                errors.VALIDATION,
                str(exc),
                provider_error=exc.code,
                requested_provider=exc.provider_id,
                capability=exc.capability,
            ))
        descriptor = provider.describe()
        result["provider"] = {
            "interface_version": descriptor["interface_version"],
            "provider_id": descriptor["provider_id"],
            "provider_version": descriptor["provider_version"],
            "host_class": descriptor["host_class"],
            "limits": _limit_receipt(spec, result),
        }
        return contract.stamp(result)

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
        result = dict(session_service.execute(session_id, spec))
        descriptor = provider.describe()
        result["provider"] = {
            "interface_version": descriptor["interface_version"],
            "provider_id": descriptor["provider_id"],
            "provider_version": descriptor["provider_version"],
            "host_class": descriptor["host_class"],
            "limits": _limit_receipt(spec, result),
        }
        return contract.stamp(result)

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
        try:
            result = dict(await provider.execute_stream(spec, on_progress=on_progress))
        except UnsupportedCapability as exc:
            return contract.stamp(errors.error_result(
                errors.VALIDATION,
                str(exc),
                provider_error=exc.code,
                requested_provider=exc.provider_id,
                capability=exc.capability,
            ))
        descriptor = provider.describe()
        result["provider"] = {
            "interface_version": descriptor["interface_version"],
            "provider_id": descriptor["provider_id"],
            "provider_version": descriptor["provider_version"],
            "host_class": descriptor["host_class"],
            "limits": _limit_receipt(spec, result),
        }
        return contract.stamp(result)

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
        result = executor.execute(
            language,
            data.decode(errors="replace"),
            stdin=stdin,
            timeout=timeout,
            workdir=str(workdir),
        )
        result["entry_file"] = entry_file
        result["language"] = language
        return result
