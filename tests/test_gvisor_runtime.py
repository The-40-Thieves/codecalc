"""Fail-closed Docker + gVisor runtime contract."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from codecalc.strict_runtime import (
    _GVISOR_HOST_OVERHEAD,
    DockerGVisorRuntime,
    GVisorConfig,
    StrictImageUnavailable,
    StrictRuntimeUnavailable,
    _effective_pids_limit,
    check_prerequisites,
    host_prerequisites,
    strict_execution_config,
)

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
        if argv[1] == "inspect":
            return completed(argv, json.dumps({
                "io.codecalc.owner": "codecalc-strict",
                "io.codecalc.run-id": "0123456789abcdef",
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
    # process_limit=24 is the GUEST budget; the host --pids-limit adds the gVisor
    # sandbox overhead so the sandbox can boot at all: 24 + 48 = 72.
    check("launch applies the effective PID limit", "--pids-limit=72" in argv)
    check("launch applies the memory limit", "--memory=256m" in argv)
    check("launch applies the CPU limit", "--cpus=1.5" in argv)
    check("launch uses the pinned image", IMAGE in argv)
    check("source is sent through stdin", kwargs["input"] == "print(42)")
    check("launch never uses a shell", kwargs["shell"] is False)
    check("result carries gvisor-v1 receipt", result["strict_receipt"]["isolation_profile"] == "gvisor-v1")
    check("container is removed after collection",
          calls[-1][0][1:] == ["rm", "--force", "--volumes", "codecalc-0123456789abcdef"])


def test_empty_stdin_launch_is_unchanged_from_before_the_bind_mount(tmp_path=None) -> None:
    """With no `stdin`, the launch must be byte-identical to before:
    no `-v` mount, no `--stdin-file`, and `source` still travels alone over the
    container's stdin pipe exactly as it always did."""
    calls: list[tuple[list[str], dict]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "info":
            return completed(argv, json.dumps({
                "Runtimes": {"runsc": {}}, "CgroupVersion": "2",
            }))
        if argv[1] == "inspect":
            return completed(argv, json.dumps({
                "io.codecalc.owner": "codecalc-strict",
                "io.codecalc.run-id": "0123456789abcdef",
            }))
        return completed(argv, '{"ok":true,"verdict":"OK"}')

    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner)
    runtime.execute(
        "0123456789abcdef", language="python3", source="print(42)", timeout=7,
    )
    argv, kwargs = next(call for call in calls if call[0][1] == "run")

    check("no stdin: no bind mount is added", "-v" not in argv)
    check("no stdin: no --stdin-file is appended", "--stdin-file" not in argv)
    check("no stdin: source is still the only thing sent over the pipe",
          kwargs["input"] == "print(42)")


def test_nonempty_stdin_is_bind_mounted_read_only_and_named_on_argv() -> None:
    """Program `stdin` must be written to a HOST temp file INSIDE A
    PRIVATE (0700) per-run directory, bind-mounted read-only into the
    container, and named via `--stdin-file` — mirroring the local Rust path's
    own `--stdin-file` pattern (executor.py), never put on argv (an E2BIG
    ceiling large stdin could exceed) and never merged into the stdin pipe
    that still carries `source` alone.

    The private parent directory is the fix for a follow-up finding: a
    world-readable file sitting in the SHARED, world-traversable system temp
    dir would let any other local host user read the caller's own program
    stdin for the run's duration. The directory (0700) blocks that; the file
    itself stays 0644 because that is what the guest's own read needs (see
    the comment on `DockerGVisorRuntime.execute`)."""
    if os.name != "posix":
        # The strict gVisor runtime is Linux + Docker + runsc; the 0700/0644
        # guarantees it asserts are POSIX file-mode semantics Windows does not
        # honor (os.chmod only toggles the read-only bit there), and the host
        # temp path is not a POSIX path. Skip where the runtime never runs.
        print("SKIP host-stdin-file permission contract — POSIX-only file semantics")
        return
    calls: list[tuple[list[str], dict]] = []
    seen_host_path: list[str] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "info":
            return completed(argv, json.dumps({
                "Runtimes": {"runsc": {}}, "CgroupVersion": "2",
            }))
        if argv[1] == "inspect":
            return completed(argv, json.dumps({
                "io.codecalc.owner": "codecalc-strict",
                "io.codecalc.run-id": "0123456789abcdef",
            }))
        if argv[1] == "run":
            mount = next(a for a in argv if a.startswith("/") and ":" in a and a.endswith(":ro"))
            host_path = Path(mount.split(":", 1)[0])
            seen_host_path.append(str(host_path))
            check("the host temp file exists while the container is launched",
                  host_path.exists())
            check("the host temp file holds exactly the caller's stdin",
                  host_path.read_text(encoding="utf-8") == "hello\n")
            check("the host temp file is o+r for the guest's read",
                  (host_path.stat().st_mode & 0o777) == 0o644)
            check("the host temp file's PARENT DIRECTORY is private (0700)",
                  (host_path.parent.stat().st_mode & 0o777) == 0o700)
            check("the parent directory is NOT the shared system temp dir",
                  host_path.parent != Path(tempfile.gettempdir()))
        return completed(argv, '{"ok":true,"verdict":"OK"}')

    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner)
    runtime.execute(
        "0123456789abcdef", language="python3", source="print(42)", timeout=7,
        stdin="hello\n",
    )
    argv, kwargs = next(call for call in calls if call[0][1] == "run")

    check("source still travels alone over the container's stdin pipe",
          kwargs["input"] == "print(42)")
    check("--stdin-file is appended after --lang/--timeout",
          "--stdin-file" in argv and argv.index("--stdin-file") > argv.index("--timeout"))
    check("the bind mount targets a fixed, non-tmpfs guest path",
          any(a.endswith(":/codecalc-stdin:ro") for a in argv))
    check("the guest path named by --stdin-file matches the bind mount target",
          argv[argv.index("--stdin-file") + 1] == "/codecalc-stdin")
    check("host temp file path was captured from the mount flag", len(seen_host_path) == 1)
    check("the host temp file is removed once execute() returns",
          bool(seen_host_path) and not Path(seen_host_path[0]).exists())
    check("the host temp file's private PARENT DIRECTORY is also removed "
          "(no leftover dir, not just the file)",
          bool(seen_host_path) and not Path(seen_host_path[0]).parent.exists())


def test_host_stdin_temp_file_is_cleaned_up_even_when_the_run_fails() -> None:
    """A failed launch must not leak the host stdin temp file OR its
    private directory — the same cleanup discipline the container itself gets
    on the failure path."""
    if os.name != "posix":
        # POSIX-only: the host stdin path and its 0700 directory are POSIX
        # filesystem semantics; the strict runtime is Linux + Docker + runsc.
        print("SKIP host-stdin-file cleanup contract — POSIX-only file semantics")
        return
    seen_host_path: list[str] = []

    def runner(argv, **_kwargs):
        if argv[1] == "info":
            return completed(argv, json.dumps({
                "Runtimes": {"runsc": {}}, "CgroupVersion": "2",
            }))
        if argv[1] == "run":
            mount = next(a for a in argv if a.startswith("/") and ":" in a and a.endswith(":ro"))
            seen_host_path.append(mount.split(":", 1)[0])
            return completed(argv, "", returncode=1)  # simulate a launch failure
        if argv[1] == "inspect":
            return completed(argv, "", returncode=1)  # nothing to remove
        raise AssertionError(f"unexpected call: {argv}")

    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner)
    expect_raises(
        "failed launch still raises", StrictRuntimeUnavailable, "failed",
        lambda: runtime.execute("0123456789abcdef", language="python3",
                                source="x", timeout=7, stdin="hello\n"),
    )
    check("the host stdin temp file is removed on the failure path too",
          bool(seen_host_path) and not Path(seen_host_path[0]).exists())
    check("the private parent directory is removed on the failure path too",
          bool(seen_host_path) and not Path(seen_host_path[0]).parent.exists())


def test_effective_pids_limit_adds_sandbox_overhead_and_floors_the_smallest_budget() -> None:
    # The measured gVisor sandbox boot floor on Cave is ~30 host tasks and a
    # trivial workload runs from ~34. The overhead must clear that even
    # for the smallest possible guest budget, so process_limit=1 still boots.
    check("overhead clears the measured boot floor", _GVISOR_HOST_OVERHEAD >= 34)
    check("guest budget is preserved additively", _effective_pids_limit(24) == 24 + _GVISOR_HOST_OVERHEAD)
    check("smallest budget still clears the boot floor",
          _effective_pids_limit(1) >= _GVISOR_HOST_OVERHEAD and _effective_pids_limit(1) >= 34)

    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "info":
            return completed(argv, json.dumps({
                "Runtimes": {"runsc": {}}, "CgroupVersion": "2",
                "Architecture": "x86_64", "ServerVersion": "28.3.3",
            }))
        if argv[1] == "inspect":
            return completed(argv, json.dumps({
                "io.codecalc.owner": "codecalc-strict",
                "io.codecalc.run-id": "0123456789abcdef",
            }))
        return completed(argv, '{"ok":true,"verdict":"OK"}')

    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner)
    runtime.execute(
        "0123456789abcdef", language="python3", source="print(42)", timeout=7,
        process_limit=1,
    )
    argv = next(call for call in calls if call[1] == "run")
    check("floor applies the overhead for process_limit=1",
          f"--pids-limit={1 + _GVISOR_HOST_OVERHEAD}" in argv)


def test_strict_receipt_discloses_the_guest_to_host_pid_translation() -> None:
    """gVisor's host --pids-limit = process_limit + overhead,
    but the strict receipt recorded neither the requested guest budget nor the
    effective host limit — so the translation was invisible to an auditor. The
    receipt must disclose both, plus the overhead applied, additively."""
    def runner(argv, **_kwargs):
        if argv[1] == "info":
            return completed(argv, json.dumps({
                "Runtimes": {"runsc": {}}, "CgroupVersion": "2",
                "Architecture": "x86_64", "ServerVersion": "28.3.3",
            }))
        if argv[1] == "inspect":
            return completed(argv, json.dumps({
                "io.codecalc.owner": "codecalc-strict",
                "io.codecalc.run-id": "0123456789abcdef",
            }))
        return completed(argv, '{"ok":true,"verdict":"OK"}')

    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner)
    result = runtime.execute(
        "0123456789abcdef", language="python3", source="print(42)", timeout=7,
        process_limit=24,
    )
    receipt = result["strict_receipt"]
    check("receipt discloses the requested guest budget", receipt["process_limit"] == 24)
    check("receipt discloses the effective host --pids-limit", receipt["pids_limit"] == 72)
    check("receipt discloses the overhead applied",
          receipt["gvisor_host_overhead"] == _GVISOR_HOST_OVERHEAD)
    check("effective limit is requested plus overhead",
          receipt["pids_limit"] == receipt["process_limit"] + receipt["gvisor_host_overhead"])


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


def test_cleanup_never_removes_a_foreign_container_even_after_a_failed_run() -> None:
    """The name `codecalc-<run_id>` can be squatted BEFORE our run:
    `docker run --name` then fails with a conflict, and the old cleanup path
    (`verify_ownership=False` in execute's finally) force-removed whatever
    held the name — a container codecalc never created. The refusal has to
    hold on the failure path too, because the failure path is exactly the one
    a squat produces."""
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "info":
            return completed(argv, json.dumps({
                "Runtimes": {"runsc": {}}, "CgroupVersion": "2",
            }))
        if argv[1] == "run":
            return completed(argv, "", returncode=125)  # name conflict
        if argv[1] == "inspect":
            return completed(argv, json.dumps({"io.codecalc.owner": "someone-else"}))
        raise AssertionError(f"unexpected mutation: {argv}")

    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner)
    expect_raises(
        "squatted name still fails the run", StrictRuntimeUnavailable, "failed",
        lambda: runtime.execute("squatted-run", language="python3",
                                source="print(1)", timeout=1),
    )
    check("a foreign container is NEVER removed, even on the failure path",
          not any(argv[1] == "rm" for argv in calls))


def test_cleanup_is_idempotent_when_the_container_is_already_gone() -> None:
    """An absent container is nothing to remove, not a refusal: inspect
    failing must make cleanup a quiet no-op, or every failed probe would
    raise a SECOND error out of the finally and mask the first."""
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "inspect":
            return completed(argv, "", returncode=1)  # no such container
        raise AssertionError(f"unexpected mutation: {argv}")

    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner)
    runtime.cleanup("long-gone")
    check("cleanup of an absent container issues no rm and does not raise",
          not any(argv[1] == "rm" for argv in calls))


def test_host_prerequisites_reports_measured_facts_without_raising() -> None:
    def runner(argv, **_kwargs):
        return completed(argv, json.dumps({
            "Runtimes": {"runsc": {"path": "/usr/bin/runsc"}, "runc": {}},
            "CgroupVersion": "2", "Architecture": "aarch64",
            "ServerVersion": "29.7.2",
        }))

    facts = host_prerequisites(runner=runner)
    check("host prereqs sees Docker", facts["docker_present"] is True)
    check("host prereqs sees cgroup v2", facts["cgroup_v2"] is True)
    check("host prereqs sees runsc registered", facts["runtime_registered"] is True)
    check("host prereqs reports architecture", facts["architecture"] == "aarch64")


def test_host_prerequisites_fails_closed_when_docker_is_absent() -> None:
    def runner(argv, **_kwargs):
        raise FileNotFoundError("docker")

    facts = host_prerequisites(runner=runner)
    check("absent Docker: not present", facts["docker_present"] is False)
    check("absent Docker: runsc not registered", facts["runtime_registered"] is False)
    check("absent Docker: cgroup unknown reported false", facts["cgroup_v2"] is False)


def test_host_prerequisites_fails_closed_when_runsc_is_unregistered() -> None:
    def runner(argv, **_kwargs):
        return completed(argv, json.dumps({
            "Runtimes": {"runc": {}}, "CgroupVersion": "2",
        }))

    facts = host_prerequisites(runner=runner)
    check("Docker present but no runsc: present", facts["docker_present"] is True)
    check("Docker present but no runsc: not registered",
          facts["runtime_registered"] is False)


def test_check_prerequisites_is_available_when_boundary_is_provable() -> None:
    def runner(argv, **_kwargs):
        if argv[1] == "info":
            return completed(argv, json.dumps({
                "Runtimes": {"runsc": {}}, "CgroupVersion": "2",
                "Architecture": "aarch64", "ServerVersion": "29.7.2",
            }))
        if argv[1] == "image":  # docker image inspect
            return completed(argv, json.dumps([{"Id": "sha256:" + "a" * 64}]))
        raise AssertionError(f"unexpected call: {argv}")

    report = check_prerequisites(image="codecalc-exec:strict", runner=runner)
    check("cheap prereqs are available", report["available"] is True)
    check("image presence measured", report["image_present"] is True)
    check("runtime name surfaced", report["runtime"] == "runsc")
    check("no canary without deep", report["canary"] is None)


def test_check_prerequisites_fails_closed_without_runsc_with_a_clear_message() -> None:
    def runner(argv, **_kwargs):
        if argv[1] == "info":
            return completed(argv, json.dumps({
                "Runtimes": {"runc": {}}, "CgroupVersion": "2",
            }))
        if argv[1] == "image":
            return completed(argv, json.dumps([{"Id": "sha256:" + "a" * 64}]))
        raise AssertionError(f"unexpected call: {argv}")

    report = check_prerequisites(image="codecalc-exec:strict", runner=runner)
    check("no runsc: not available", report["available"] is False)
    check("no runsc: structured reason names runsc",
          report["detail"] is not None and "runsc" in report["detail"])
    check("no runsc: registration flag is false",
          report["runtime_registered"] is False)


def test_check_prerequisites_deep_runs_a_canary_verified_out_of_band() -> None:
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "info":
            return completed(argv, json.dumps({
                "Runtimes": {"runsc": {}}, "CgroupVersion": "2",
                "Architecture": "aarch64", "ServerVersion": "29.7.2",
            }))
        if argv[1] == "image":
            return completed(argv, json.dumps([{"Id": "sha256:" + "a" * 64}]))
        if argv[1] == "run":
            return completed(argv, "deadbeef\n")
        if argv[1] == "inspect":
            return completed(argv, "runsc\n")  # HostConfig.Runtime, harness-observed
        if argv[1] == "rm":
            return completed(argv, "")
        raise AssertionError(f"unexpected call: {argv}")

    report = check_prerequisites(image="codecalc-exec:strict", deep=True, runner=runner)
    check("deep canary attempted", report["canary"]["attempted"] is True)
    check("deep canary ran", report["canary"]["ran"] is True)
    check("deep canary confirms runsc out of band",
          report["canary"]["verified_runsc"] is True
          and report["canary"]["runtime_observed"] == "runsc")
    check("deep available requires the verified canary", report["available"] is True)
    check("canary launches under runsc",
          any(a[1] == "run" and "--runtime=runsc" in a for a in calls))


def test_recover_orphans_removes_owned_containers_and_returns_run_ids() -> None:
    removed: list[str] = []

    def runner(argv, **_kwargs):
        if argv[1] == "ps":
            return completed(argv, "cid_one\ncid_two\n")
        if argv[1] == "inspect":
            target = argv[-1]
            return completed(argv, json.dumps({
                "io.codecalc.owner": "codecalc-strict",
                "io.codecalc.run-id": f"run-of-{target}",
            }))
        if argv[1] == "rm":
            removed.append(argv[-1])
            return completed(argv, "")
        raise AssertionError(f"unexpected call: {argv}")

    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner)
    recovered = runtime.recover_orphans()
    check("both owned orphans are removed", removed == ["cid_one", "cid_two"])
    check("recovery returns each orphan's run identity",
          recovered == ["run-of-cid_one", "run-of-cid_two"])


def test_recover_orphans_never_removes_a_container_that_lost_our_label_in_a_race() -> None:
    removed: list[str] = []

    def runner(argv, **_kwargs):
        if argv[1] == "ps":
            return completed(argv, "cid_one\n")
        if argv[1] == "inspect":
            # Re-inspected out of band: the label filter matched, but by the
            # time we look the container is someone else's — never touch it.
            return completed(argv, json.dumps({"io.codecalc.owner": "someone-else"}))
        if argv[1] == "rm":
            removed.append(argv[-1])
            return completed(argv, "")
        raise AssertionError(f"unexpected mutation: {argv}")

    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner)
    recovered = runtime.recover_orphans()
    check("a container that lost our label is not removed", removed == [])
    check("and it is not reported as recovered", recovered == [])


def test_recover_orphans_fails_closed_when_it_cannot_enumerate() -> None:
    def runner(argv, **_kwargs):
        if argv[1] == "ps":
            return completed(argv, "", returncode=1)
        raise AssertionError(f"unexpected call: {argv}")

    runtime = DockerGVisorRuntime(GVisorConfig(image=IMAGE), runner=runner)
    expect_raises(
        "enumeration failure", StrictRuntimeUnavailable, "enumerate",
        runtime.recover_orphans,
    )


# ── REAL runsc end-to-end (Cave-only, SKIPS without it) ─────────────────────
def _live_strict_image() -> tuple[str | None, str | None]:
    """The resolvable, LOCALLY-PRESENT digest-pinned strict image for a live
    round-trip, or (None, a skip reason). Mirrors the gating in
    `test_gvisor_conformance.py` / `test_strict_service.py`'s
    `test_real_runsc_end_to_end`: this suite needs a real `runsc` runtime,
    which GitHub-hosted runners do not have, so it SKIPS (not fails) off Cave.
    """
    try:
        config = strict_execution_config()
    except StrictImageUnavailable as exc:
        return None, f"no published strict image ({exc})"
    facts = host_prerequisites()
    if not facts["docker_present"]:
        return None, "Docker Engine is unavailable"
    if not facts["runtime_registered"]:
        return None, "the 'runsc' runtime is not registered with Docker"
    proc = subprocess.run(
        ["docker", "image", "inspect", config.image],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if proc.returncode != 0:
        return None, f"strict image {config.image!r} is not present locally"
    return config.image, None


def test_execute_delivers_program_stdin_to_a_real_gvisor_guest() -> None:
    """LIVE against real runsc — the bug lived entirely in the
    `docker run` argv `DockerGVisorRuntime.execute` builds (no bind mount, no
    `--stdin-file`), so a fake runner cannot observe it; only a real container
    reading real guest stdin can. WATCH-IT-FAIL-FIRST evidence (run manually
    against the pre-fix `execute`, before `--stdin-file`/the bind mount
    existed): a `python3 -c "import sys; sys.stdout.write(sys.stdin.read())"`
    payload driven the same way `DockerGVisorRuntime.execute` drives it
    (`source` alone piped to the container's stdin, nothing else) produced
    empty stdout — the guest saw immediate EOF, never the caller's `stdin`.
    This test asserts the FIXED behavior: the guest's stdout is exactly the
    bytes handed in via `stdin=`.
    """
    image, skip_reason = _live_strict_image()
    if image is None:
        print(f"SKIP live stdin delivery — {skip_reason}")
        return
    runtime = DockerGVisorRuntime(strict_execution_config())
    run_id = "the859-" + uuid.uuid4().hex[:16]
    payload = "import sys; sys.stdout.write(sys.stdin.read())"
    result = runtime.execute(
        run_id, language="python3", source=payload, timeout=15,
        stdin="hello from the caller\n",
    )
    check(f"the guest received exactly the caller's program stdin (got {result.get('stdout')!r})",
          result.get("stdout") == "hello from the caller\n")
    check(f"the run still succeeds (result={result})", result.get("ok") is True)
    check("the run still carries a verified gvisor-v1 strict receipt",
          result.get("strict_receipt", {}).get("verified") is True)


def test_execute_with_no_stdin_still_sees_eof_live() -> None:
    """The empty-stdin case must be UNCHANGED: no `stdin=` still means
    the guest sees immediate EOF, and the run still succeeds with a verified
    strict receipt — proving the fix is additive, not a behavior change on the
    path every existing caller already uses."""
    image, skip_reason = _live_strict_image()
    if image is None:
        print(f"SKIP live empty-stdin check — {skip_reason}")
        return
    runtime = DockerGVisorRuntime(strict_execution_config())
    run_id = "the859-empty-" + uuid.uuid4().hex[:16]
    payload = "import sys; sys.stdout.write('GOT:' + sys.stdin.read())"
    result = runtime.execute(run_id, language="python3", source=payload, timeout=15)
    check(f"no stdin: the guest still sees EOF (unchanged behavior; got {result.get('stdout')!r})",
          result.get("stdout") == "GOT:")
    check(f"no stdin: the run still succeeds (result={result})",
          result.get("ok") is True)
    check("no stdin: the strict receipt/isolation profile is unchanged",
          result.get("strict_receipt", {}).get("verified") is True
          and result["strict_receipt"]["isolation_profile"] == "gvisor-v1")


if __name__ == "__main__":
    test_config_requires_digest_pinned_image()
    test_probe_requires_registered_runsc_and_cgroup_v2()
    test_probe_reports_versioned_gvisor_attestation()
    test_launch_is_shell_free_and_applies_every_outer_limit()
    test_empty_stdin_launch_is_unchanged_from_before_the_bind_mount()
    test_nonempty_stdin_is_bind_mounted_read_only_and_named_on_argv()
    test_host_stdin_temp_file_is_cleaned_up_even_when_the_run_fails()
    test_effective_pids_limit_adds_sandbox_overhead_and_floors_the_smallest_budget()
    test_strict_receipt_discloses_the_guest_to_host_pid_translation()
    test_run_id_cannot_become_a_docker_option_or_foreign_container_name()
    test_cancel_refuses_container_without_matching_ownership_labels()
    test_cleanup_never_removes_a_foreign_container_even_after_a_failed_run()
    test_cleanup_is_idempotent_when_the_container_is_already_gone()
    test_host_prerequisites_reports_measured_facts_without_raising()
    test_host_prerequisites_fails_closed_when_docker_is_absent()
    test_host_prerequisites_fails_closed_when_runsc_is_unregistered()
    test_check_prerequisites_is_available_when_boundary_is_provable()
    test_check_prerequisites_fails_closed_without_runsc_with_a_clear_message()
    test_check_prerequisites_deep_runs_a_canary_verified_out_of_band()
    test_recover_orphans_removes_owned_containers_and_returns_run_ids()
    test_recover_orphans_never_removes_a_container_that_lost_our_label_in_a_race()
    test_recover_orphans_fails_closed_when_it_cannot_enumerate()
    test_execute_delivers_program_stdin_to_a_real_gvisor_guest()
    test_execute_with_no_stdin_still_sees_eof_live()
    for failure in FAILS:
        print(f"FAIL {failure}")
    raise SystemExit(1 if FAILS else 0)
