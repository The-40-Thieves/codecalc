"""Hostile-workload conformance for the gVisor strict boundary.

This is the criterion PR #155 could not meet: it does NOT mock Docker. It
launches the real executor image under `--runtime=runsc` and drives five hostile
workloads through it as real containers, asserting each is CONTAINED:

  * fork bomb        the pids limit holds; the host is not affected
  * memory bomb      the cgroup memory limit holds; the container is OOM-killed
  * descendant tree  cancellation kills the whole tree — no leaked processes
  * network egress   `--network=none` blocks outbound connections
  * filesystem       a read-only root with no host bind blocks writes outside tmpfs

The runtime is proven to be GENUINELY runsc OUT OF BAND — by `docker inspect`
of the container's `HostConfig.Runtime`, observed by this harness, never by a
string the workload printed (a runc container can echo "runsc"; it cannot change
what the daemon recorded). See `_runtime_of`.

CAVE-ONLY. This needs a registered `runsc` runtime, which GitHub-hosted runners
do NOT have. When runsc is absent the suite SKIPS with a clear reason and exits
0, so CI stays green; on a runsc-capable host (Cave) it RUNS and must PASS. It
is wired into ci-python.yml as a step that skips without runsc, and into
scripts/gvisor_conformance.sh (the Cave-runnable target) which builds the image
first. Proven on Cave / a runsc host — NOT on GitHub runners.

A NOTE ON gVisor's PID-LIMIT BEHAVIOUR, learned building this suite: Docker's
`--pids-limit` bounds HOST-side tasks, and under gVisor that cgroup holds the
runsc sandbox and gofer processes too — so a limit that is fine under runc (24)
is too low for the sandbox to even START (~48 is the floor on Cave). And when a
guest fork bomb exhausts the limit, gVisor tears the sandbox down rather than
delivering EAGAIN to the guest, so the guest's stdout is lost. The fork-bomb
assertion therefore proves containment the way the boundary actually exhibits
it: the container is bounded and terminated, it was NOT OOM-killed (so the PID
ceiling, not memory, stopped it), and the host is unaffected.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid

IMAGE = "codecalc-exec:strict"
RUNTIME = "runsc"
FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name} {detail}")
    if not condition:
        FAILS.append(name)


def _docker(*args: str, timeout: int = 60, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], input=stdin, capture_output=True, text=True,
        timeout=timeout, check=False,
    )


def _runsc_registered() -> bool:
    try:
        proc = _docker("info", "--format", "{{json .Runtimes}}", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    try:
        return RUNTIME in (json.loads(proc.stdout) or {})
    except json.JSONDecodeError:
        return False


def _image_present() -> bool:
    try:
        return _docker("image", "inspect", IMAGE, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


#: The strict launch flags, mirroring DockerGVisorRuntime.execute(). Per-workload
#: overrides (pids, memory, network) are appended by each test.
def _strict_flags(*, pids: int, memory_mb: int, network_none: bool = True) -> list[str]:
    flags = [
        f"--runtime={RUNTIME}",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt", "no-new-privileges:true",
        "--user=65534:65534",
        f"--pids-limit={pids}",
        f"--memory={memory_mb}m",
        "--cpus=1",
        # A path INSIDE the container's bounded tmpfs, not a host temp path.
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",  # noqa: S108
    ]
    if network_none:
        flags += ["--network=none"]
    return flags


_TRANSIENT = ("cannot read client sync file", "failed to create shim task",
              "creating root file system: input/output error")


def _run_detached(flags: list[str], cmd: list[str]) -> str | None:
    """Start a named detached container under runsc; retry transient sandbox
    startup errors (gVisor occasionally fails to bring a sandbox up on a busy
    host — a startup flake, not a boundary failure). Returns the container name,
    or None if it could not be started."""
    name = f"codecalc-conf-{uuid.uuid4().hex[:12]}"
    for _ in range(4):
        proc = _docker("run", "--detach", "--name", name, *flags, IMAGE, *cmd,
                       timeout=90)
        if proc.returncode == 0:
            return name
        if any(t in proc.stderr for t in _TRANSIENT):
            _docker("rm", "--force", name, timeout=30)
            time.sleep(1.0)
            continue
        print(f"     run failed: {proc.stderr.strip()[:200]}")
        return None
    print("     sandbox would not start after retries (transient gVisor error)")
    return None


def _runtime_of(name: str) -> str | None:
    proc = _docker("inspect", "--format", "{{.HostConfig.Runtime}}", name)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _state(name: str, field: str) -> str:
    proc = _docker("inspect", "--format", f"{{{{.State.{field}}}}}", name)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _remove(name: str) -> None:
    _docker("rm", "--force", "--volumes", name, timeout=30)


def _container_id(name: str) -> str:
    proc = _docker("inspect", "--format", "{{.Id}}", name)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _host_procs_referencing(container_id: str) -> int:
    """Count host processes whose argv names this container. Under gVisor the
    runsc sandbox, gofer and shim carry the CONTAINER ID (not the name) in their
    command line, so the search keys on the id — the durable host-side handle on
    the sandbox and everything running inside it."""
    if not container_id:
        return 0
    proc = subprocess.run(["pgrep", "-af", container_id[:12]], capture_output=True,
                          text=True, check=False)
    return sum(1 for line in proc.stdout.splitlines() if "pgrep" not in line)


# ── the executor really runs under runsc (positive control) ─────────────────
def test_executor_runs_the_real_entrypoint_under_runsc() -> None:
    name = _run_detached(
        _strict_flags(pids=96, memory_mb=256) + ["--interactive"],
        # The real command DockerGVisorRuntime issues, minus stdin plumbing:
        # run codecalc-exec directly with an inline program via python3.
        ["python3", "-c", "print(6*7)"],
    )
    if name is None:
        check("executor entrypoint runs under runsc", False, "-> sandbox unavailable")
        return
    runtime = _runtime_of(name)
    _docker("wait", name, timeout=40)
    logs = _docker("logs", name).stdout.strip()
    _remove(name)
    check("a workload genuinely ran under runsc (docker inspect, out of band)",
          runtime == RUNTIME, f"-> runtime={runtime}")
    check("...and produced correct output (42)", logs == "42", f"-> {logs!r}")


# ── fork bomb: the PID limit holds, the host is unaffected ──────────────────
_FORK_BOMB = (
    "import os,time\n"
    "while True:\n"
    "    try: pid=os.fork()\n"
    "    except OSError: time.sleep(5); os._exit(42)\n"
    "    if pid==0: time.sleep(8); os._exit(0)\n"
)


def test_fork_bomb_is_contained_by_the_pids_limit() -> None:
    host_before = _host_task_count()
    name = _run_detached(
        _strict_flags(pids=64, memory_mb=1024),  # room for memory, tight on PIDs
        ["python3", "-c", _FORK_BOMB],
    )
    if name is None:
        check("fork bomb is contained by the pids limit", False, "-> sandbox unavailable")
        return
    runtime = _runtime_of(name)
    cid = _container_id(name)
    exit_code = _docker("wait", name, timeout=45).stdout.strip()
    oom = _state(name, "OOMKilled")
    _remove(name)
    leaked = _host_procs_referencing(cid)
    host_after = _host_task_count()

    check("fork bomb ran under runsc", runtime == RUNTIME, f"-> {runtime}")
    check("the container was BOUNDED and terminated (not runaway)",
          exit_code not in ("", "0"), f"-> exit={exit_code!r}")
    check("it was stopped by the PID ceiling, not memory (OOMKilled is false)",
          oom == "false", f"-> OOMKilled={oom}")
    check("no leaked host processes after the fork bomb", leaked == 0,
          f"-> {leaked} host procs still reference the container")
    # The host's own task count must not have been driven up: a fork bomb that
    # escaped the cgroup would leave the host inflated.
    check("the host task count is not inflated by the contained bomb",
          host_after <= host_before + 200,
          f"-> before={host_before} after={host_after}")


def _host_task_count() -> int:
    proc = subprocess.run(["ps", "-eL", "--no-headers"], capture_output=True,
                          text=True, check=False)
    return len(proc.stdout.splitlines())


# ── memory bomb: the cgroup memory limit holds → OOM-killed ─────────────────
_MEM_BOMB = (
    "b=[]\n"
    "for i in range(400):\n"
    "    b.append(bytearray(10*1024*1024))\n"
    "print('MEM_UNBOUNDED', len(b))\n"
)


def test_memory_bomb_is_oom_killed_not_host_affecting() -> None:
    name = _run_detached(
        _strict_flags(pids=96, memory_mb=128),
        ["python3", "-c", _MEM_BOMB],
    )
    if name is None:
        check("memory bomb is OOM-killed", False, "-> sandbox unavailable")
        return
    runtime = _runtime_of(name)
    exit_code = _docker("wait", name, timeout=45).stdout.strip()
    oom = _state(name, "OOMKilled")
    logs = _docker("logs", name).stdout + _docker("logs", name).stderr
    _remove(name)

    check("memory bomb ran under runsc", runtime == RUNTIME, f"-> {runtime}")
    check("the container was OOM-killed (cgroup memory limit held)",
          oom == "true" or exit_code == "137",
          f"-> OOMKilled={oom} exit={exit_code}")
    check("the bomb never reported completing its allocation",
          "MEM_UNBOUNDED" not in logs, f"-> {logs[:80]!r}")


# ── descendant tree: cancellation kills the whole tree, no leaks ────────────
_TREE = (
    "import os,time\n"
    "for _ in range(6):\n"
    "    if os.fork()==0:\n"
    "        time.sleep(300); os._exit(0)\n"
    "time.sleep(300)\n"
)


def test_descendant_tree_dies_on_cancellation_with_no_leaks() -> None:
    name = _run_detached(
        _strict_flags(pids=96, memory_mb=256),
        ["python3", "-c", _TREE],
    )
    if name is None:
        check("descendant tree dies on cancellation", False, "-> sandbox unavailable")
        return
    time.sleep(2)
    runtime = _runtime_of(name)
    cid = _container_id(name)
    running = _state(name, "Running")
    procs_while_running = _host_procs_referencing(cid)

    # Cancel — the same force-remove the runtime's cleanup issues.
    _remove(name)
    time.sleep(1)
    listed = _docker("ps", "--all", "--quiet", "--filter", f"name={name}").stdout.strip()
    procs_after = _host_procs_referencing(cid)

    check("descendant tree ran under runsc", runtime == RUNTIME, f"-> {runtime}")
    check("the tree was alive before cancellation", running == "true",
          f"-> Running={running}")
    check("...with real host-side sandbox processes while running",
          procs_while_running > 0, f"-> {procs_while_running}")
    check("cancellation removed the container", listed == "", f"-> {listed!r}")
    check("...and left NO leaked host processes (whole tree gone)",
          procs_after == 0, f"-> {procs_after} still reference the container")


# ── network egress: --network=none blocks outbound ──────────────────────────
_EGRESS = (
    "import socket\n"
    "try:\n"
    "    s=socket.socket(); s.settimeout(4); s.connect(('1.1.1.1',53))\n"
    "    print('EGRESS_OPEN')\n"
    "except OSError as e:\n"
    "    print('EGRESS_BLOCKED', e.errno)\n"
)


def test_network_egress_is_blocked() -> None:
    name = _run_detached(
        _strict_flags(pids=96, memory_mb=256, network_none=True),
        ["python3", "-c", _EGRESS],
    )
    if name is None:
        check("network egress is blocked", False, "-> sandbox unavailable")
        return
    runtime = _runtime_of(name)
    _docker("wait", name, timeout=30)
    logs = _docker("logs", name).stdout
    _remove(name)

    check("egress workload ran under runsc", runtime == RUNTIME, f"-> {runtime}")
    check("outbound connection was blocked, not opened",
          "EGRESS_BLOCKED" in logs and "EGRESS_OPEN" not in logs,
          f"-> {logs.strip()!r}")


# ── filesystem escape: read-only root, no bind → writes outside tmpfs fail ──
_FS_ESCAPE = (
    "import os,json\n"
    "r={}\n"
    "for p in ['/evil','/etc/evil','/usr/local/bin/evil','/root/evil']:\n"
    "    try:\n"
    "        open(p,'w').write('x'); r[p]='WROTE'\n"
    "    except OSError as e: r[p]='blocked:%d'%e.errno\n"
    "try:\n"
    "    open('/tmp/ok','w').write('x'); r['/tmp']='WROTE'\n"
    "except OSError as e: r['/tmp']='blocked:%d'%e.errno\n"
    "print(json.dumps(r))\n"
)


def test_filesystem_escape_is_blocked_outside_tmpfs() -> None:
    name = _run_detached(
        _strict_flags(pids=96, memory_mb=256),
        ["python3", "-c", _FS_ESCAPE],
    )
    if name is None:
        check("filesystem escape is blocked", False, "-> sandbox unavailable")
        return
    runtime = _runtime_of(name)
    _docker("wait", name, timeout=30)
    logs = _docker("logs", name).stdout.strip()
    _remove(name)

    try:
        result = json.loads(logs)
    except json.JSONDecodeError:
        check("filesystem-escape workload produced a result", False, f"-> {logs!r}")
        return

    check("filesystem workload ran under runsc", runtime == RUNTIME, f"-> {runtime}")
    outside = ["/evil", "/etc/evil", "/usr/local/bin/evil", "/root/evil"]
    check("every write outside tmpfs/workspace was blocked (read-only root)",
          all(result.get(p, "").startswith("blocked") for p in outside),
          f"-> {result}")
    tmpfs_result = result.get("/tmp")  # noqa: S108 — container tmpfs path, not host
    check("...while the bounded tmpfs workspace remained writable",
          tmpfs_result == "WROTE", f"-> {tmpfs_result}")


if __name__ == "__main__":
    if not _runsc_registered():
        print("SKIP gVisor conformance — the 'runsc' runtime is not registered "
              "with Docker on this host. This suite needs a real gVisor boundary "
              "and cannot run on GitHub-hosted runners; it runs on Cave / a "
              "runsc-capable host. Skipping (exit 0) so CI stays green.")
        sys.exit(0)
    if not _image_present():
        print(f"SKIP gVisor conformance — runsc is registered but the executor "
              f"image {IMAGE!r} is not built. Run scripts/build_executor_image.sh "
              f"(or scripts/gvisor_conformance.sh, which builds it first). "
              f"Skipping (exit 0).")
        sys.exit(0)

    print(f"running gVisor conformance under --runtime={RUNTIME} against {IMAGE}\n")
    test_executor_runs_the_real_entrypoint_under_runsc()
    test_fork_bomb_is_contained_by_the_pids_limit()
    test_memory_bomb_is_oom_killed_not_host_affecting()
    test_descendant_tree_dies_on_cancellation_with_no_leaks()
    test_network_egress_is_blocked()
    test_filesystem_escape_is_blocked_outside_tmpfs()

    print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS
          else "\n=== gVisor STRICT BOUNDARY CONTAINED EVERY HOSTILE WORKLOAD ===")
    for f in FAILS:
        print(f"  {f}")
    sys.exit(1 if FAILS else 0)
