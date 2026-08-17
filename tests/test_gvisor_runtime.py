"""Fail-closed Docker + gVisor runtime contract for THE-828."""

from __future__ import annotations

import json
import subprocess

from codecalc.strict_runtime import DockerGVisorRuntime, GVisorConfig, StrictRuntimeUnavailable

IMAGE = "registry.example/codecalc-exec@sha256:" + "a" * 64
FAILS: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name}")
    if not condition:
        FAILS.append(name)


def expect_raises(name: str, error: type[Exception], match: str, operation) -> None:
    try:
        operation()
    except error as exc:
        if match not in str(exc):
            FAILS.append(f"{name}: wrong message: {exc}")
    else:
        FAILS.append(f"{name}: did not raise {error.__name__}")


def completed(argv: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, "")


def test_config_requires_digest_pinned_image() -> None:
    expect_raises(
        "mutable image", ValueError, "digest-pinned",
        lambda: GVisorConfig(image="registry.example/codecalc-exec:latest"),
    )


def test_probe_requires_registered_runsc_and_cgroup_v2() -> None:
    responses = iter([
        completed([], json.dumps({"Runtimes": {"runc": {}}, "CgroupVersion": "2"})),
        completed([], json.dumps({"Runtimes": {"runsc": {}}, "CgroupVersion": "1"})),
    ])
    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=lambda *args, **kwargs: next(responses))

    expect_raises("missing runsc", StrictRuntimeUnavailable, "runsc", runtime.probe)
    expect_raises("cgroup v1", StrictRuntimeUnavailable, "cgroup v2", runtime.probe)


def test_probe_reports_versioned_gvisor_attestation() -> None:
    def runner(argv, **_kwargs):
        return completed(argv, json.dumps({
            "Runtimes": {"runsc": {"path": "/usr/bin/runsc"}},
            "CgroupVersion": "2",
            "Architecture": "aarch64",
            "ServerVersion": "28.3.3",
        }))

    receipt = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner).probe()

    check("probe reports gvisor-v1", receipt["isolation_profile"] == "gvisor-v1")
    check("probe reports ARM64", receipt["architecture"] == "aarch64")
    check("probe reports configured runsc", receipt["runtime"] == "runsc")
    check("probe attests every profile control", all(receipt["enforcement"].values()))


def test_launch_is_shell_free_and_applies_every_outer_limit() -> None:
    calls: list[tuple[list[str], dict]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "info":
            return completed(argv, json.dumps({
                "Runtimes": {"runsc": {}}, "CgroupVersion": "2",
                "Architecture": "x86_64", "ServerVersion": "28.3.3",
            }))
        return completed(argv, '{"ok":true,"verdict":"OK"}')

    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner)
    result = runtime.execute(
        "0123456789abcdef", language="python3", source="print(42)", timeout=7,
        memory_mb=256, process_limit=24, cpu_count=1.5,
    )
    argv, kwargs = next(call for call in calls if call[0][1] == "run")

    check("launch uses Docker run", argv[:3] == ["docker", "run", "--name"])
    check("launch explicitly selects runsc", "--runtime=runsc" in argv)
    check("launch disables networking", "--network=none" in argv)
    check("launch uses a read-only root", "--read-only" in argv)
    check("launch drops every capability", "--cap-drop=ALL" in argv)
    check("launch forbids privilege gain", "no-new-privileges:true" in argv)
    check("launch applies the PID limit", "--pids-limit=24" in argv)
    check("launch applies the memory limit", "--memory=256m" in argv)
    check("launch applies the CPU limit", "--cpus=1.5" in argv)
    check("launch uses the pinned image", IMAGE in argv)
    check("source is sent through stdin", kwargs["input"] == "print(42)")
    check("launch never uses a shell", kwargs["shell"] is False)
    check("result carries gvisor-v1 receipt", result["strict_receipt"]["isolation_profile"] == "gvisor-v1")
    check("container is removed after collection",
          calls[-1][0][1:] == ["rm", "--force", "--volumes", "codecalc-0123456789abcdef"])


def test_run_id_cannot_become_a_docker_option_or_foreign_container_name() -> None:
    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=lambda *_a, **_k: completed([]))
    expect_raises(
        "unsafe run id", ValueError, "run_id",
        lambda: runtime.execute("--privileged", language="python3", source="", timeout=1),
    )


def test_cancel_refuses_container_without_matching_ownership_labels() -> None:
    def runner(argv, **_kwargs):
        if argv[1] == "inspect":
            return completed(argv, json.dumps({"io.codecalc.owner": "someone-else"}))
        raise AssertionError(f"unexpected mutation: {argv}")

    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner)
    expect_raises(
        "foreign container", StrictRuntimeUnavailable, "ownership",
        lambda: runtime.cancel("owned-run"),
    )


if __name__ == "__main__":
    test_config_requires_digest_pinned_image()
    test_probe_requires_registered_runsc_and_cgroup_v2()
    test_probe_reports_versioned_gvisor_attestation()
    test_launch_is_shell_free_and_applies_every_outer_limit()
    test_run_id_cannot_become_a_docker_option_or_foreign_container_name()
    test_cancel_refuses_container_without_matching_ownership_labels()
    for failure in FAILS:
        print(f"FAIL {failure}")
    raise SystemExit(1 if FAILS else 0)
