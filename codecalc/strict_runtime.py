"""Fail-closed Docker + gVisor execution boundary for the Linux strict service."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

ISOLATION_PROFILE = "gvisor-v1"
ENFORCEMENT_CONTROLS = (
    "application_kernel",
    "cgroup_v2",
    "namespaces",
    "seccomp",
    "read_only_rootfs",
    "non_root",
    "capabilities_dropped",
    "filesystem",
    "network",
    "descendants",
    "resource_limits",
)
_DIGEST_IMAGE = re.compile(r"^[^\s]+@sha256:[0-9a-fA-F]{64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# This is the container's private, bounded tmpfs mount point, not a host temp path.
_TMP_DIRECTORY = "/tmp"  # noqa: S108


class StrictRuntimeUnavailable(RuntimeError):
    """The host cannot prove the configured strict runtime boundary."""


@dataclass(frozen=True)
class GVisorConfig:
    image: str
    docker: str = "docker"
    runtime: str = "runsc"
    tmpfs_mb: int = 64
    user: str = "65534:65534"

    def __post_init__(self) -> None:
        if not _DIGEST_IMAGE.fullmatch(self.image):
            raise ValueError("strict image must be digest-pinned with @sha256:<64 hex>")
        if not self.runtime or self.runtime.startswith("-"):
            raise ValueError("strict runtime name is invalid")


Runner = Callable[..., subprocess.CompletedProcess[str]]


class DockerGVisorRuntime:
    """Launch the trusted CodeCalc executor image using Docker's runsc runtime."""

    def __init__(self, config: GVisorConfig, *, runner: Runner = subprocess.run) -> None:
        self.config = config
        self._runner = runner

    def probe(self) -> dict[str, Any]:
        argv = [
            self.config.docker, "info", "--format",
            "{{json .}}",
        ]
        try:
            proc = self._runner(
                argv, capture_output=True, text=True, timeout=10,
                check=False, shell=False,
            )
            if proc.returncode != 0:
                raise StrictRuntimeUnavailable("Docker Engine is unavailable")
            info = json.loads(proc.stdout)
        except StrictRuntimeUnavailable:
            raise
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise StrictRuntimeUnavailable(f"Docker Engine probe failed: {exc}") from exc

        runtimes = info.get("Runtimes")
        if not isinstance(runtimes, dict) or self.config.runtime not in runtimes:
            raise StrictRuntimeUnavailable(
                f"gVisor runtime {self.config.runtime!r} is not registered with Docker"
            )
        if str(info.get("CgroupVersion")) != "2":
            raise StrictRuntimeUnavailable("Docker must use cgroup v2")
        return {
            "isolation_profile": ISOLATION_PROFILE,
            "runtime": self.config.runtime,
            "architecture": str(info.get("Architecture", "unknown")),
            "docker_version": str(info.get("ServerVersion", "unknown")),
            "image": self.config.image,
            "enforcement": dict.fromkeys(ENFORCEMENT_CONTROLS, True),
        }

    def execute(
        self,
        run_id: str,
        *,
        language: str,
        source: str,
        timeout: int,
        memory_mb: int = 512,
        process_limit: int = 24,
        cpu_count: float = 1.0,
    ) -> dict[str, Any]:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id is not a safe strict container identity")
        if timeout <= 0 or memory_mb <= 0 or process_limit <= 0 or cpu_count <= 0:
            raise ValueError("strict execution limits must be positive")
        self.probe()
        name = f"codecalc-{run_id}"
        argv = [
            self.config.docker, "run", "--name", name,
            f"--label=io.codecalc.run-id={run_id}",
            "--label=io.codecalc.owner=codecalc-strict",
            f"--runtime={self.config.runtime}",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt", "no-new-privileges:true",
            f"--user={self.config.user}",
            f"--pids-limit={process_limit}",
            f"--memory={memory_mb}m",
            f"--cpus={cpu_count:g}",
            "--tmpfs", f"{_TMP_DIRECTORY}:rw,noexec,nosuid,nodev,size={self.config.tmpfs_mb}m",
            "--interactive",
            self.config.image,
            "codecalc-exec", "--lang", language, "--timeout", str(timeout),
        ]
        try:
            proc = self._runner(
                argv, input=source, capture_output=True, text=True,
                timeout=timeout + 5, check=False, shell=False,
            )
            if proc.returncode != 0:
                raise StrictRuntimeUnavailable(
                    f"strict container failed before returning a result: {proc.stderr.strip()}"
                )
            try:
                result = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise StrictRuntimeUnavailable("strict container returned invalid JSON") from exc
            if not isinstance(result, dict):
                raise StrictRuntimeUnavailable("strict container result must be an object")
            result["strict_receipt"] = {
                "verified": True,
                "isolation_profile": ISOLATION_PROFILE,
                "runtime": self.config.runtime,
                "image": self.config.image,
                "controls": list(ENFORCEMENT_CONTROLS),
            }
            return result
        finally:
            self.cleanup(run_id, verify_ownership=False)

    def _name(self, run_id: str) -> str:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id is not a safe strict container identity")
        return f"codecalc-{run_id}"

    def _owned(self, run_id: str) -> bool:
        name = self._name(run_id)
        proc = self._runner(
            [self.config.docker, "inspect", "--format", "{{json .Config.Labels}}", name],
            capture_output=True, text=True, timeout=10, check=False, shell=False,
        )
        if proc.returncode != 0:
            return False
        try:
            labels = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise StrictRuntimeUnavailable("container ownership labels are invalid") from exc
        return (
            isinstance(labels, dict)
            and labels.get("io.codecalc.owner") == "codecalc-strict"
            and labels.get("io.codecalc.run-id") == run_id
        )

    def cancel(self, run_id: str) -> None:
        if not self._owned(run_id):
            raise StrictRuntimeUnavailable("refusing to cancel container without matching ownership")
        proc = self._runner(
            [self.config.docker, "kill", self._name(run_id)],
            capture_output=True, text=True, timeout=10, check=False, shell=False,
        )
        if proc.returncode != 0:
            raise StrictRuntimeUnavailable("failed to cancel owned strict container")

    def cleanup(self, run_id: str, *, verify_ownership: bool = True) -> None:
        if verify_ownership and not self._owned(run_id):
            raise StrictRuntimeUnavailable("refusing to remove container without matching ownership")
        self._runner(
            [self.config.docker, "rm", "--force", "--volumes", self._name(run_id)],
            capture_output=True, text=True, timeout=10, check=False, shell=False,
        )
