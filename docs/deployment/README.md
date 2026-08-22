# Deploying a strict execution backend

This is the operator runbook for the **strict** execution backends —
provisioning and running the host-side pieces that back a `<host>-strict`
provider. [`docs/contract/provider-v1.md`](../contract/provider-v1.md)
documents the provider *interface* those backends implement; this document is
about standing one up in production. Every command below is derived from the
code cited beside it — anything not verifiable from this repo is marked
"verify for your platform" rather than guessed.

---

## 1. Overview

codecalc's default (`local`) provider sandboxes with rlimits (Linux/macOS) or
a Job Object (Windows). It is real hardening, but the README says plainly it
is **not a hostile-code security boundary** — no filesystem, network, or
kernel-level containment. Reach for a strict backend when you intend to run
code you do not trust: an untrusted-input pipeline, a multi-tenant service, a
public-facing tool.

There is exactly one implementation of the strict boundary, and it lives on
Linux: gVisor (`runsc`) under Docker
(`codecalc/strict_runtime.py::DockerGVisorRuntime`). Every OS reaches it as
follows:

| Client OS | How it gets a strict boundary |
|---|---|
| **Linux** | Either host the gVisor+Docker service locally (§2) and point yourself at it, or configure `CODECALC_STRICT_URL` to a remote instance of the same service — codecalc never runs `DockerGVisorRuntime` in-process inside the MCP server itself, only inside the separate `codecalc serve-strict` service. |
| **Windows** | Two independent options, not mutually exclusive: (a) opt the **local** provider into the AppContainer boundary (§3) — a real security boundary layered on the default backend, running on the Windows box itself; (b) configure `CODECALC_STRICT_URL` to a Linux gVisor host, same as macOS. |
| **macOS** | No local strict primitive is wired up. The only path is a client of the remote Linux service (§4). |

Without `CODECALC_STRICT_URL` set, the `<host>-strict` provider is still
*discoverable* (`describe()` always returns it for the current OS) but it is
an unavailable, fail-closed descriptor: selecting it returns
`strict_provider_unavailable` and executes nothing
(`codecalc/providers.py::NativeStrictExecutionProvider`). That is deliberate —
codecalc never silently downgrades a strict request to the unhardened local
path.

## 2. Linux / gVisor + Docker

This section stands up `codecalc serve-strict` — the authenticated HTTP
service that owns the real gVisor boundary
(`codecalc/strict_service.py`, `codecalc/strict_runtime.py`). Every other
platform's strict provider is a client of this service.

### Quick start (one command)

`scripts/setup-strict.sh` automates §2.1–§2.4 below: it checks the
prerequisites (and prints the exact fix if one is missing, rather than
installing anything), pulls the digest-pinned executor image, generates
`CODECALC_STRICT_SERVICE_TOKEN` if you haven't set one, and runs the deep
`doctor` canary against that exact image before it will let you launch.

```bash
scripts/setup-strict.sh --check-only   # preflight + pull + canary only (default; nothing is left running)
scripts/setup-strict.sh --serve        # ...then start serve-strict on 127.0.0.1:8000 in the foreground
scripts/setup-strict.sh --help         # every flag, incl. --host/--port
```

It never runs `apt`/`sudo` or installs Docker/gVisor for you — registering
the `runsc` runtime is still your explicit step (§2.1) — and it never
containerizes `serve-strict` itself: the service needs the **host** Docker
daemon to spawn `runsc` containers, so a socket-in-container setup would only
broaden the attack surface. Safe to re-run. The rest of this section is the
manual reference the script is built from, for auditing or when you need to
deviate from a default.

### 2.1 Prerequisites

- **Docker Engine**, with **cgroup v2**. `DockerGVisorRuntime.probe()` and
  `codecalc doctor` both read `docker info --format '{{json .}}'` and refuse
  to proceed unless `CgroupVersion == "2"`
  (`codecalc/strict_runtime.py::host_prerequisites`).
- **gVisor's `runsc` runtime, registered with Docker** under the name
  `runsc` (`docker info`'s `Runtimes` map must contain that key — the runtime
  name is configurable via `GVisorConfig.runtime` but `runsc` is the
  default and what every command below assumes).

  Installing gVisor itself is outside this repo — verify the exact package
  and version for your distro against
  [gVisor's own install docs](https://gvisor.dev/docs/user_guide/install/).
  On a Debian/Ubuntu host, gVisor's documented procedure is:

  ```bash
  # verify for your platform — this is gVisor's own upstream procedure,
  # not something this repo pins or tests
  sudo apt-get update && sudo apt-get install -y \
    apt-transport-https ca-certificates curl gnupg
  curl -fsSL https://gvisor.dev/archive.key \
    | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
    | sudo tee /etc/apt/sources.list.d/gvisor.list
  sudo apt-get update && sudo apt-get install -y runsc

  # registers the "runsc" runtime with Docker (writes /etc/docker/daemon.json)
  sudo runsc install
  sudo systemctl restart docker
  ```

  Confirm the runtime landed where codecalc looks for it:

  ```bash
  docker info --format '{{json .Runtimes}}'
  # must contain a "runsc" key, and CgroupVersion must be "2"
  docker info --format '{{.CgroupVersion}}'
  ```

  gVisor's default `systrap` platform needs no KVM
  ([`docs/contract/provider-v1.md`](../contract/provider-v1.md)), so this
  works on a VM without nested virtualization. Linux x86_64 and ARM64 are
  both supported; the published image is multi-arch (§2.3).

### 2.2 The executor image

The strict boundary launches a purpose-built image
(`docker/executor.Dockerfile`: multi-stage, minimal, non-root, `tini` as
init) carrying `codecalc-exec`, its `--no-net` seccomp filter (symbol-shim
fallback), and a python3 runtime.
Two different references matter, and codecalc keeps them deliberately
separate:

- **Diagnostic tag** — `codecalc-exec:strict` (override with
  `CODECALC_STRICT_IMAGE`). This is what `doctor`, `check_prerequisites`, and
  the startup canary look for. Build it locally:

  ```bash
  scripts/build_executor_image.sh
  ```

- **Production image** — digest-pinned, published to GHCR, and the *only*
  reference the execution path will run under. Pull it explicitly by the
  digest committed in `docker/executor-image.lock`:

  ```bash
  docker pull "$(grep -E '^ghcr\.io.*@sha256:' docker/executor-image.lock)"
  ```

  `strict_execution_config()` refuses to build a `GVisorConfig` — and the
  execution path raises `StrictImageUnavailable` — if that lock file holds no
  digest yet (`codecalc/strict_runtime.py::published_strict_image`,
  `strict_execution_config`). It never falls back to the mutable diagnostic
  tag on the production path. An operator who needs to (re)publish the image
  dispatches `.github/workflows/publish-executor-image.yml`
  (`workflow_dispatch` only — it never fires from a push or PR), which builds
  `linux/amd64` and `linux/arm64`, pushes to
  `ghcr.io/the-40-thieves/codecalc-exec`, and commits the resulting digest
  back into `docker/executor-image.lock`.

### 2.3 What the runtime actually runs

Every strict execution is a `docker run` your service account should expect
to see on this host. This is not a wrapper script to copy-paste — it is the
exact shape `DockerGVisorRuntime.execute()` builds
(`codecalc/strict_runtime.py`), reconstructed here so an operator auditing
the host knows the contract the service holds itself to:

```bash
docker run \
  --name codecalc-<run_id> \
  --label=io.codecalc.run-id=<run_id> \
  --label=io.codecalc.owner=codecalc-strict \
  --runtime=runsc \
  --network=none \
  --read-only \
  --cap-drop=ALL \
  --security-opt no-new-privileges:true \
  --user=65534:65534 \
  --pids-limit=<process_limit + 48> \
  --memory=<memory_mb>m \
  --cpus=<cpu_count> \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=<tmpfs_mb>m \
  [-v <host-stdin-file>:/codecalc-stdin:ro] \
  --interactive \
  ghcr.io/the-40-thieves/codecalc-exec@sha256:<digest> \
  codecalc-exec --lang <language> --timeout <timeout> [--stdin-file /codecalc-stdin]
```

Notes an operator needs, not just an auditor:

- `--pids-limit` is a **host**-side task ceiling, and under gVisor that
  cgroup also holds the sandbox process, the gofer, and the platform's own
  threads — none of which exist under plain `runc`. codecalc adds a fixed
  48-task overhead on top of the caller's guest `process_limit` so the
  sandbox has room to boot at all (`_GVISOR_HOST_OVERHEAD`,
  `codecalc/strict_runtime.py:173-205`) — a bare `process_limit=24` alone
  measured below the boot floor on Cave.
- Program stdin (when supplied) never rides the source pipe or argv — it is
  written to a private, per-run host temp directory (`0700`), the file
  itself is `0644` so the gVisor gofer's guest-uid check can read it, and
  it is bind-mounted read-only at a fixed guest path distinct from the
  sandbox's own tmpfs.
- Cleanup (`docker rm --force --volumes`) is ownership-verified on every
  exit path against the `io.codecalc.owner`/`io.codecalc.run-id` labels — it
  will never remove a container it did not create, even one squatting on the
  same name.

### 2.4 Running the service

```bash
export CODECALC_STRICT_SERVICE_TOKEN="$(openssl rand -hex 32)"
.venv/bin/python -m codecalc.server serve-strict --host 127.0.0.1 --port 8000
```

`serve-strict` **refuses to start** without
`CODECALC_STRICT_SERVICE_TOKEN` set — a token-less bind would expose
gVisor-sandboxed code execution unauthenticated
(`codecalc/strict_service.py::main`). Every request must carry
`Authorization: Bearer <token>`, compared constant-time
(`hmac.compare_digest`); an unauthenticated request gets `401` before any
routing happens.

**The service speaks plain HTTP; it does not terminate TLS.** A client
(`RemoteStrictExecutionProvider`) refuses anything except a loopback
`http://` URL or an `https://` one
(`codecalc/providers.py`, `RemoteStrictExecutionProvider.__init__`). Reaching
this service from another host therefore requires an operator-supplied
reverse proxy terminating TLS — put it behind one before binding to a
routable interface, and prefer keeping the bind on loopback and reaching it
through the proxy rather than binding a routable interface directly.

At startup the service best-effort reconciles containers a crashed previous
generation may have leaked (§5) before it starts accepting new runs.

## 3. Windows / AppContainer

This is a **different mechanism** from §2 and §4: it hardens the *local*
provider in-process on the Windows host itself, rather than delegating to a
Linux service. It is not registered under the `windows-strict` descriptor —
that descriptor is either the fail-closed stub or, with `CODECALC_STRICT_URL`
set, a client of the Linux service exactly as in §4. AppContainer hardening
is an independent, complementary opt-in on the default backend.

### 3.1 Enabling it

```
CODECALC_WIN_APPCONTAINER=1
```

Off by default. When set, every native execution launches inside a
least-privilege AppContainer profile
(`CreateAppContainerProfile`, no capability SIDs — no network) created fresh
per run (`executor/src/platform/windows.rs::create_appcontainer`,
`prepare_appcontainer`).

### 3.2 What it grants, and to whom

Two different grants, deliberately split:

- **The run's own sandbox workdir** — full access (`GENERIC_ALL`), granted
  to *this run's own* AppContainer SID only, inheritable so files the
  payload creates are covered
  (`windows.rs::grant_sid_path_access`). Concurrent runs cannot reach each
  other's workdirs.
- **The interpreter's own directory tree** — read+execute, granted to the
  fixed, well-known "ALL APPLICATION PACKAGES" SID (`S-1-15-2-1`), which
  every AppContainer is a member of
  (`windows.rs::all_application_packages_sid`). This grant is **per-node and
  non-inheritable** — measured on real Windows 11, an *inheritable* ACE
  (however applied: `SetNamedSecurityInfoW`, `TreeSetNamedSecurityInfoW`,
  even `icacls /T`) never reaches an interpreter's pre-existing,
  inheritance-protected files, so `grant_tree_explicit_read` walks the tree
  and applies an explicit ACE to every file individually.

  This walk runs **once per interpreter**, not once per execution: a marker
  keyed by the interpreter's own size+mtime is cached under
  `%LOCALAPPDATA%\codecalc\ac-granted\`
  (`windows.rs::ac_grant_marker`, `ensure_interpreter_ac_readable`). A
  several-thousand-file interpreter tree is walked on first use and never
  again until the interpreter itself changes. The trade-off this leaves: a
  read-only ACE, readable by *any* AppContainer on the machine, sits on a
  public interpreter's files — acceptable because those files carry no
  secrets, and the alternative is a full tree walk on every single
  execution.

### 3.3 Prerequisites and least-privilege notes

- No Docker, no runsc, no external service — the boundary is native Win32
  APIs (`CreateAppContainerProfile`, `SetEntriesInAclW`,
  `SetNamedSecurityInfoW`).
- **Fails closed.** If profile creation, SID derivation, or an ACL grant
  fails, the launch is refused outright rather than dropping to an
  unconfined process (README, "Sandbox guarantees" / AppContainer section;
  `windows.rs::prepare_appcontainer` propagates every error).
- The interpreter grant needs write access to `%LOCALAPPDATA%\codecalc\` for
  its cache marker, and read access to the interpreter's own directory tree
  to perform the initial grant — run the service account with those, not
  with broader filesystem rights.
- **Disclosed as unverified, every run.** CI has no way to exercise
  AppContainer behavior (server SKUs can't), so every strict-hardened
  Windows run still carries `appcontainer_isolation_unverified_on_windows`
  in `unenforced`, even though the isolation has been independently verified
  by hand on a real Windows 11 box (AppContainer SID present, user-profile
  secrets unreadable, writes confined to the workdir, network denied). Treat
  that string as this repo's honest limit, not a defect — it says "we could
  not prove it from CI," not "it does not work."
- If you need the CI-verified gVisor boundary instead of this native one,
  configure this Windows host as a client of §2's service via
  `CODECALC_STRICT_URL` (§4's configuration applies identically here).

## 4. macOS / remote-Linux provider

macOS has no local strict primitive. The only strict path is
`RemoteStrictExecutionProvider` — an authenticated client of a Linux host
running the §2 service (`codecalc/providers.py`).

### 4.1 Configuration

```
CODECALC_STRICT_URL=https://strict-host.internal:8443
CODECALC_STRICT_AUTHORIZATION=Bearer <the same token the service was started with>
```

Both are read once, at provider-registry construction
(`codecalc/providers.py::configured_registry`). Without `CODECALC_STRICT_URL`
set, `macos-strict` is registered as the fail-closed stub (§1) instead — it
is never silently absent, but it never silently runs unhardened either.

### 4.2 The plaintext-endpoint rejection

`RemoteStrictExecutionProvider.__init__` parses `CODECALC_STRICT_URL` and
raises `ValueError` at construction — before any request is sent — unless
the scheme is `https://`, **or** the scheme is `http://` **and** the host is
a loopback address (`127.0.0.1`, `::1`, `localhost`). Any other plaintext
endpoint is refused outright:

```
remote strict provider requires HTTPS (HTTP is loopback-only)
```

It also refuses a URL carrying embedded `user:pass@` credentials — those
belong in `CODECALC_STRICT_AUTHORIZATION`, not the URL, so they cannot end
up serialized somewhere the URL does.

### 4.3 The health handshake before any source is sent

Before submitting code, the client requires `GET /v1/health` to report a
compatible `interface_version`, `ready: true`, `strict: true`,
`isolation_profile: "gvisor-v1"`, and every one of `application_kernel`,
`cgroup_v2`, `namespaces`, `seccomp`, `read_only_rootfs`, `non_root`,
`capabilities_dropped`, `filesystem`, `network`, `descendants`, and
`resource_limits` as enforced
(`codecalc/providers.py::RemoteStrictExecutionProvider._verified_health`).
Any gap fails with `strict_attestation_failed` and the execution endpoint is
never called — a source snippet never leaves the Mac against a boundary that
did not attest fully. Each successful execution must repeat the same
controls in its own receipt, verified again on the way back.

Both `CODECALC_STRICT_AUTHORIZATION` and the bare credential inside it are
redacted from every error, descriptor, and result this provider returns.

## 5. Operations

### 5.1 Startup and pre-flight checks

```bash
# cheap: docker info + image presence, no container launched
.venv/bin/python -m codecalc.server doctor --json

# deep: actually launches the image under runsc and verifies, out of band
# (docker inspect .HostConfig.Runtime), that it really ran there
.venv/bin/python -m codecalc.server doctor --deep --json
```

`doctor --deep` promotes `strict_runtime.check_prerequisites()` from a cheap
`docker info` + image-presence check to a real startup canary
(`codecalc/strict_runtime.py::_startup_canary`): it launches the image with
the same defensive flags a real run uses (no network, read-only root,
dropped capabilities, non-root, no-new-privileges, bounded memory) and reads
the runtime the daemon actually recorded, not a string the payload printed.
Transient gVisor boot flakes (`cannot read client sync file`, `failed to
create shim task`, an I/O error building the root filesystem) are retried up
to four times before being reported as real failures — run this after any
host change (kernel update, Docker upgrade, gVisor upgrade) and before
trusting the boundary in production.

### 5.2 Orphan recovery

A crashed `serve-strict` process can leak containers it never got to clean
up. `DockerGVisorRuntime.recover_orphans()` runs automatically at service
startup, **before** any new run is admitted
(`codecalc/strict_service.py::main`): it enumerates containers carrying
codecalc's `io.codecalc.owner` label, re-verifies ownership on each one
individually (a container whose label changed between the list and the
look — a squat, a race — is left untouched), and force-removes the rest.
Enumeration failing is itself fail-closed: the service refuses to start
serving rather than silently reporting a clean host it could not actually
prove clean.

You do not need to run this by hand in the ordinary case — restarting the
service is enough. If you want to check it worked, `serve-strict`'s stderr
names how many containers it recovered on startup.

### 5.3 Fail-closed behavior, by cause

| Symptom | Cause | Where it's enforced |
|---|---|---|
| `strict_provider_unavailable` | `CODECALC_STRICT_URL` not set on this client | `codecalc/providers.py::NativeStrictExecutionProvider.execute` |
| `StrictImageUnavailable` | No digest committed in `docker/executor-image.lock` yet — the publish workflow has never run | `codecalc/strict_runtime.py::strict_execution_config` |
| `401` from the service | Missing/wrong `Authorization: Bearer <token>` | `codecalc/strict_service.py::StrictService.authorized` |
| Service refuses to start | `CODECALC_STRICT_SERVICE_TOKEN` unset | `codecalc/strict_service.py::main` |
| `strict_attestation_failed` (client-side) | `/v1/health` missing a required control, wrong `isolation_profile`, or incompatible `interface_version` | `RemoteStrictExecutionProvider._verified_health` |
| `ValueError` at provider construction | `CODECALC_STRICT_URL` is plaintext HTTP to a non-loopback host, or carries embedded credentials | `RemoteStrictExecutionProvider.__init__` |
| `429` from the service | `MAX_CONCURRENT_RUNS` (8, fixed) already running | `strict_service.py::_execute` |
| `413` from the service | Request body over `MAX_CONTENT_LENGTH` (1 MiB, fixed) | `strict_service.py::_StrictRequestHandler._reject_oversized` |

None of these fall back to unhardened execution. A strict request that
cannot be proven safe is refused, never silently downgraded.

### 5.4 Quota and limit tuning

What a caller of the remote service can actually move, versus what is fixed:

| Knob | Source | Default | Client-tunable? |
|---|---|---|---|
| `timeout` | `ComputationSpec.timeout` | 10s | Yes — clamped server-side to `MAX_TIMEOUT_SECONDS` (300s); a request above that runs for as long as the ceiling allows rather than erroring (`strict_service.py::_execute`). |
| `memory_mb` | `ComputationSpec.max_memory_mb` | 512 MiB | Yes, when the spec sets `max_memory_mb > 0`; otherwise the runtime default of 512 applies. |
| `process_limit` (guest budget; host `--pids-limit` adds a fixed +48 gVisor overhead) | `DockerGVisorRuntime.execute`'s own default | 24 | **No** — fixed by design, not a dropped field: `ComputationSpec` carries no `process_limit` knob at all (its fields are `language`, `code`, `stdin`, `timeout`, `workdir`, `max_memory_mb`, `max_output_kb`, `max_cpu`, `no_net`), so there is nothing on the wire for the strict service to forward. Tune it by editing the `serve-strict` call site if your workload needs a different guest process budget. |
| `cpu_count` | `DockerGVisorRuntime.execute`'s own default | 1.0 | **Yes** — clamped server-side to `MAX_CPU_COUNT` (4); a request above that runs with as much CPU share as the ceiling allows rather than erroring (`strict_service.py::_execute`). Driven by `ComputationSpec.max_cpu` on the wire. |
| `MAX_CONCURRENT_RUNS` | `strict_service.py` module constant | 8 | No — fixed in code, not an environment variable. Raise it by editing the constant if your host can sustain more concurrent gVisor sandboxes. |
| `MAX_TRACKED_RUNS` | `strict_service.py` module constant | 512 | No — oldest terminal-state run record is evicted once exceeded; a run still `"running"` is never evicted. |

`max_cpu` is now forwarded end-to-end, closing the gap this section used to
flag. `process_limit`'s "No" is fixed-by-design rather than a gap: there is
no wire knob for it, so a deliberate limit — not an oversight — keeps it at
the runtime's built-in default.

---

Cross-referenced from the top-level [`README.md`](../../README.md#configuration)
and from [`docs/contract/provider-v1.md`](../contract/provider-v1.md).
