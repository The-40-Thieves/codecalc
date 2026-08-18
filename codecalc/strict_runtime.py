"""Fail-closed Docker + gVisor execution boundary for the Linux strict service."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Mapping
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

#: The immutable ownership labels stamped on every container codecalc launches.
#: Discovery, ownership verification, and orphan recovery all key on these — the
#: run-id label carries the caller's identity, the owner label proves the
#: container is ours and not a squatter wearing the same name.
OWNER_LABEL = "io.codecalc.owner"
OWNER_VALUE = "codecalc-strict"
RUN_ID_LABEL = "io.codecalc.run-id"

#: Where the doctor prerequisite probe and the strict service look for the
#: executor image, when nothing overrides it. A LOCAL tag by default: the
#: registry-published, multi-arch, digest-pinned image is a release step (it
#: needs registry credentials), and until it exists the boundary is proven on a
#: runsc-capable host against a locally built image. `GVisorConfig` still
#: refuses a mutable tag for the digest-pinned EXECUTION path; this default names
#: the artifact for the presence/canary DIAGNOSTIC, which is not that path.
STRICT_IMAGE_ENV = "CODECALC_STRICT_IMAGE"
DEFAULT_STRICT_IMAGE = "codecalc-exec:strict"


def strict_image(environment: Mapping[str, str] | None = None) -> str:
    """The executor image reference for host diagnostics, from the environment."""
    env = os.environ if environment is None else environment
    return (env.get(STRICT_IMAGE_ENV) or "").strip() or DEFAULT_STRICT_IMAGE


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
        """Attest the host boundary, RAISING when it cannot be proved.

        The measured host facts come from `host_prerequisites`, the single
        function `doctor` also calls — so doctor's readout and this attestation
        can never disagree about what the daemon reports. probe() adds the parts
        that are this runtime's own claim (the isolation profile, the pinned
        image, the enforcement controls it applies) and turns an unmet
        prerequisite into the fail-closed refusal the execution path needs.
        """
        facts = host_prerequisites(
            docker=self.config.docker, runtime=self.config.runtime,
            runner=self._runner,
        )
        if not facts["docker_present"]:
            raise StrictRuntimeUnavailable("Docker Engine is unavailable")
        if not facts["runtime_registered"]:
            raise StrictRuntimeUnavailable(
                f"gVisor runtime {self.config.runtime!r} is not registered with Docker"
            )
        if not facts["cgroup_v2"]:
            raise StrictRuntimeUnavailable("Docker must use cgroup v2")
        return {
            "isolation_profile": ISOLATION_PROFILE,
            "runtime": self.config.runtime,
            "architecture": facts["architecture"],
            "docker_version": facts["docker_version"],
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
            f"--label={RUN_ID_LABEL}={run_id}",
            f"--label={OWNER_LABEL}={OWNER_VALUE}",
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
        # Cleanup is OWNERSHIP-VERIFIED on both exits (THE-834). It used to be
        # `verify_ownership=False` in a finally, which force-removed whatever
        # held the name — and the name `codecalc-<run_id>` can be squatted
        # BEFORE this run, in which case `docker run --name` fails with a
        # conflict and the failure path is exactly the one that reaches a
        # container codecalc never created. On the failure path a cleanup
        # refusal is suppressed, because the run's own error (the conflict)
        # is the diagnosis and a second exception from the finally would mask
        # it; on the success path a refusal propagates, because our own
        # container having foreign labels is the story.
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
        except BaseException:
            with contextlib.suppress(StrictRuntimeUnavailable):
                self.cleanup(run_id)
            raise
        self.cleanup(run_id)
        return result

    def _name(self, run_id: str) -> str:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id is not a safe strict container identity")
        return f"codecalc-{run_id}"

    def _owned(self, run_id: str) -> bool | None:
        """Tri-state, and the middle value is load-bearing (THE-834).

        True   the container exists and carries codecalc's ownership labels
        False  the container EXISTS and is someone else's — never touch it
        None   inspect could not find it — nothing to remove, not a refusal

        Collapsing None into False is what made verified cleanup unusable
        from a finally: every failed probe (container never created) then
        raised a refusal that masked the primary error, which is why the old
        code opted out with verify_ownership=False — and force-removed
        squatters instead.
        """
        name = self._name(run_id)
        proc = self._runner(
            [self.config.docker, "inspect", "--format", "{{json .Config.Labels}}", name],
            capture_output=True, text=True, timeout=10, check=False, shell=False,
        )
        if proc.returncode != 0:
            return None
        try:
            labels = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise StrictRuntimeUnavailable("container ownership labels are invalid") from exc
        return (
            isinstance(labels, dict)
            and labels.get(OWNER_LABEL) == OWNER_VALUE
            and labels.get(RUN_ID_LABEL) == run_id
        )

    def cancel(self, run_id: str) -> None:
        if self._owned(run_id) is not True:
            raise StrictRuntimeUnavailable("refusing to cancel container without matching ownership")
        proc = self._runner(
            [self.config.docker, "kill", self._name(run_id)],
            capture_output=True, text=True, timeout=10, check=False, shell=False,
        )
        if proc.returncode != 0:
            raise StrictRuntimeUnavailable("failed to cancel owned strict container")

    def cleanup(self, run_id: str) -> None:
        """Remove the run's container — OURS, verified, every time (THE-834).

        There is deliberately no opt-out parameter any more. The one caller
        that used `verify_ownership=False` (execute's teardown) was the one
        place the guard mattered most: a squatted `codecalc-<run_id>` makes
        `docker run --name` fail, and the teardown then force-removed the
        squatter. Absent is idempotent success; foreign is a refusal.
        """
        owned = self._owned(run_id)
        if owned is None:
            return  # nothing exists under this name; removing it is a no-op
        if owned is False:
            raise StrictRuntimeUnavailable("refusing to remove container without matching ownership")
        self._runner(
            [self.config.docker, "rm", "--force", "--volumes", self._name(run_id)],
            capture_output=True, text=True, timeout=10, check=False, shell=False,
        )

    def recover_orphans(self) -> list[str]:
        """Reconcile owned strict containers left behind by a crashed service.

        Mirrors `RunSupervisor.recover_orphans` (which recovers from on-disk
        journals) for the Docker plane: a service that dies mid-run leaks its
        containers, and the run-identity label is the only durable record of
        which ones were ours. Called at strict-service startup, BEFORE any new
        run is admitted, so a restart cannot inherit a previous generation's
        live sandboxes.

        Discovery is scoped by the owner label, so nothing foreign is ever
        enumerated — but each candidate is re-inspected and its ownership
        re-verified before removal, the same guard `cleanup()` applies: a
        container whose label changed between the list and the look (a squat, a
        race) is left untouched. Enumeration failing is fail-closed: we cannot
        prove there are no orphans, so we refuse rather than report a clean host.
        """
        proc = self._runner(
            [self.config.docker, "ps", "--all", "--quiet",
             "--filter", f"label={OWNER_LABEL}={OWNER_VALUE}"],
            capture_output=True, text=True, timeout=10, check=False, shell=False,
        )
        if proc.returncode != 0:
            raise StrictRuntimeUnavailable(
                "failed to enumerate owned strict containers for orphan recovery"
            )
        recovered: list[str] = []
        for container in proc.stdout.split():
            labels = self._container_labels(container)
            if not isinstance(labels, dict) or labels.get(OWNER_LABEL) != OWNER_VALUE:
                continue  # lost our label since the filter matched — never touch it
            self._runner(
                [self.config.docker, "rm", "--force", "--volumes", container],
                capture_output=True, text=True, timeout=10, check=False, shell=False,
            )
            recovered.append(str(labels.get(RUN_ID_LABEL) or container))
        return recovered

    def _container_labels(self, container: str) -> dict | None:
        """The container's labels by id/name, or None when it cannot be read."""
        proc = self._runner(
            [self.config.docker, "inspect", "--format",
             "{{json .Config.Labels}}", container],
            capture_output=True, text=True, timeout=10, check=False, shell=False,
        )
        if proc.returncode != 0:
            return None
        try:
            labels = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        return labels if isinstance(labels, dict) else None


def host_prerequisites(
    *,
    docker: str = "docker",
    runtime: str = "runsc",
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """The measured host facts the strict boundary depends on, as data.

    NEVER RAISES. `DockerGVisorRuntime.probe()` turns an unmet prerequisite into
    a fail-closed refusal (it is on the execution path, where the only safe
    answer is to stop); `doctor` wants the same measurement as a readout it can
    surface without aborting. One function serves both so the refusal and the
    diagnostic can never describe different hosts.

    A daemon that is down, missing, or answers with something unparseable all
    collapse to `docker_present: False` — from the boundary's point of view they
    are the same fact (nothing can be launched) and each leaves the remaining
    flags false, which is the fail-closed reading.
    """
    absent = {
        "docker_present": False,
        "cgroup_v2": False,
        "runtime_registered": False,
        "architecture": None,
        "docker_version": None,
    }
    try:
        proc = runner(
            [docker, "info", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=10, check=False, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return absent
    if proc.returncode != 0:
        return absent
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return absent
    if not isinstance(info, dict):
        return absent
    runtimes = info.get("Runtimes")
    return {
        "docker_present": True,
        "cgroup_v2": str(info.get("CgroupVersion")) == "2",
        "runtime_registered": isinstance(runtimes, dict) and runtime in runtimes,
        "architecture": str(info.get("Architecture", "unknown")),
        "docker_version": str(info.get("ServerVersion", "unknown")),
    }


def _image_present(image: str, *, docker: str, runner: Runner) -> bool | None:
    """Is the executor image resolvable locally? None if the probe itself failed."""
    try:
        proc = runner(
            [docker, "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True, timeout=10, check=False, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0


#: gVisor sandbox startup can transiently fail on a busy host with these
#: errors; they mean "try again", not "the boundary is broken". The conformance
#: harness retries the same patterns.
_TRANSIENT_STARTUP = (
    "cannot read client sync file",
    "failed to create shim task",
    "creating root file system: input/output error",
)

#: pids-limit for the canary. Deliberately generous: under gVisor `--pids-limit`
#: bounds HOST-side tasks (the runsc sandbox and gofer count too), so a low
#: limit that is fine under runc stops the sandbox from STARTING (~48 is the
#: floor measured on Cave). The canary only has to prove the image comes up
#: under runsc, so it leaves ample headroom rather than exercising a tight PID
#: ceiling — that is what the conformance suite does.
_CANARY_PIDS_LIMIT = 128


def _startup_canary(
    image: str, *, docker: str, runtime: str, runner: Runner,
) -> dict[str, Any]:
    """Launch the image under `runtime` and PROVE it ran there, out of band.

    The proof is `docker inspect .HostConfig.Runtime`, read by this harness —
    not a string the workload printed. A payload can echo "runsc" from a runc
    container; it cannot change what the daemon recorded as the container's
    runtime. The canary carries the defensive flags a real run does (no network,
    read-only root, dropped capabilities, non-root, no-new-privs, bounded
    memory) so a boundary that only holds for a bare `docker run` is not mistaken
    for one that holds for the strict launch. Transient gVisor startup flakes are
    retried so `doctor --deep` does not report a working boundary as broken.
    """
    canary: dict[str, Any] = {
        "attempted": True, "ran": False,
        "runtime_observed": None, "verified_runsc": False, "detail": None,
    }
    for _ in range(4):
        name = f"codecalc-canary-{uuid.uuid4().hex}"
        try:
            run = runner(
                [docker, "run", "--detach", "--name", name,
                 f"--runtime={runtime}", "--network=none", "--read-only",
                 "--cap-drop=ALL", "--security-opt", "no-new-privileges:true",
                 "--user=65534:65534", f"--pids-limit={_CANARY_PIDS_LIMIT}",
                 "--memory=256m", "--entrypoint", "sleep", image, "3"],
                capture_output=True, text=True, timeout=45, check=False, shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            canary["detail"] = f"canary launch raised: {exc}"
            return canary
        if run.returncode != 0:
            detail = (run.stderr or run.stdout or "").strip()
            canary["detail"] = detail[:300] or "canary container failed to start"
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                runner([docker, "rm", "--force", name], capture_output=True,
                       text=True, timeout=15, check=False, shell=False)
            if any(t in detail for t in _TRANSIENT_STARTUP):
                continue  # startup flake — try again
            return canary
        canary["ran"] = True
        try:
            observed = runner(
                [docker, "inspect", "--format", "{{.HostConfig.Runtime}}", name],
                capture_output=True, text=True, timeout=10, check=False, shell=False,
            )
            canary["runtime_observed"] = (observed.stdout or "").strip() or None
            canary["verified_runsc"] = canary["runtime_observed"] == runtime
            if canary["verified_runsc"]:
                canary["detail"] = None
            else:
                canary["detail"] = (
                    f"container ran under {canary['runtime_observed']!r}, not {runtime!r}"
                )
        finally:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                runner([docker, "rm", "--force", "--volumes", name],
                       capture_output=True, text=True, timeout=10, check=False, shell=False)
        return canary
    return canary  # exhausted retries on transient startup errors


def check_prerequisites(
    image: str | None = None,
    *,
    deep: bool = False,
    docker: str = "docker",
    runtime: str = "runsc",
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """The strict-boundary prerequisites, measured and structured for `doctor`.

    Cheap by default — a `docker info` and an image lookup, the same shape as
    doctor's resolved-not-executed runtime statuses. `deep=True` promotes it to
    a real startup canary that launches the image under `runtime` and confirms,
    out of band, that it genuinely ran there (the one criterion PR #155 could
    not meet). Fail-closed: any unmet prerequisite makes `available` False and
    names the reason in `detail`, so a caller never has to infer why the strict
    plane is unusable on this host.
    """
    image_ref = image or strict_image()
    facts = host_prerequisites(docker=docker, runtime=runtime, runner=runner)
    image_present = (
        _image_present(image_ref, docker=docker, runner=runner)
        if facts["docker_present"] else None
    )
    report: dict[str, Any] = {
        "available": False,
        "docker_present": facts["docker_present"],
        "cgroup_v2": facts["cgroup_v2"],
        "runtime": runtime,
        "runtime_registered": facts["runtime_registered"],
        "architecture": facts["architecture"],
        "docker_version": facts["docker_version"],
        "image": image_ref,
        "image_present": image_present,
        "canary": None,
        "detail": None,
    }

    reasons = []
    if not facts["docker_present"]:
        reasons.append("Docker Engine is not available")
    if not facts["cgroup_v2"]:
        reasons.append("Docker is not using cgroup v2")
    if not facts["runtime_registered"]:
        reasons.append(f"the {runtime!r} runtime is not registered with Docker")
    if image_present is False:
        reasons.append(f"the executor image {image_ref!r} is not present")
    elif image_present is None and facts["docker_present"]:
        reasons.append("the executor image presence could not be determined")

    cheap_ok = not reasons
    if deep and cheap_ok:
        canary = _startup_canary(image_ref, docker=docker, runtime=runtime,
                                 runner=runner)
        report["canary"] = canary
        if not canary["verified_runsc"]:
            reasons.append(
                canary["detail"] or "the startup canary could not be verified"
            )

    report["available"] = not reasons
    report["detail"] = "; ".join(reasons) or None
    return report
