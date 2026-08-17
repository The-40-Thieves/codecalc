# Strict native providers and session supervisor implementation plan

## Outcome

Add a fail-closed, provider-neutral run lifecycle and three explicit strict
provider adapters. The existing `local` provider remains available and
truthfully non-strict; selecting a strict provider never falls back to it.

## Contract first (THE-831)

1. Add failing provider/supervisor tests for `start`, `wait`, `cancel`,
   `collect`, and idempotent `cleanup`, provider-bound session identity,
   deadlines, bounded event retention, receipts, and orphan recovery.
2. Introduce immutable `RunRequest`/`RunReceipt` data and a thread-safe
   `RunSupervisor` state machine backed by an atomic on-disk journal.
3. Adapt one-shot `ExecutionService.execute` and session execution through the
   lifecycle while preserving the public result contract.
4. Add conformance tests that every strict adapter must pass, including an
   assertion that unavailable strict providers fail closed.

## Platform providers

### Linux strict (THE-828)

1. Add failing capability-probe and launch-receipt tests for a versioned
   `gvisor-v1` isolation profile.
2. Use Docker Engine with an explicitly registered gVisor `runsc` OCI runtime.
   Default to gVisor's `systrap` platform so Linux hosts and VMs need no KVM;
   allow a separately registered KVM runtime as an operator-selected option.
3. Support both upstream gVisor architectures (x86_64 and ARM64) with a
   multi-architecture, digest-pinned executor image. Never pull mutable tags or
   silently fall back to `runc`.
4. Apply the outer cgroup-v2 CPU, memory, and PID limits before the workload;
   disable networking; use a read-only root, bounded tmpfs, non-root UID,
   dropped capabilities, and `no-new-privileges`. Do not bind arbitrary host
   paths into the sandbox.
5. Probe Docker, cgroup v2, and the named `runsc` registration in `doctor` and
   at service startup. Run a real startup canary and fail closed before payload
   submission when the boundary cannot be proved.
6. Treat gVisor's application kernel as the strict syscall/filesystem boundary.
   Landlock remains useful local defense-in-depth but is not a required guest
   attestation for `gvisor-v1`.
7. Label every container with its immutable run identity, verify ownership
   before cancel/delete, recover owned orphans at startup, and clean up
   deterministically.
8. Add Linux CI probes for fork/memory bombs, descendants, egress, filesystem
   escape, cancellation, cleanup, and an assertion that the workload really ran
   under `runsc`.

### Windows strict (THE-829)

1. Add failing source-contract and Windows integration tests.
2. Extend the creation-time process path with a unique AppContainer profile,
   restricted workdir/runtime ACLs, `SECURITY_CAPABILITIES`, the existing job
   list and explicit inherited-handle list, and no network capabilities.
3. Delete the profile and restore/remove ACL entries during idempotent cleanup.
4. Add doctor prerequisites and Windows CI escape/egress/descendant tests.

### macOS strict (THE-830)

1. Implement `macos-strict` as an authenticated remote adapter to the Linux
   strict execution service; native macOS execution remains explicitly
   non-strict.
2. Require a versioned `gvisor-v1` readiness handshake that proves the remote
   provider is strict and reports the application-kernel, cgroup-v2, namespace,
   seccomp, read-only-root, non-root, capability, filesystem, network,
   descendant, and resource controls. Reject incomplete, incompatible, or
   differently profiled receipts.
3. Bind managed run IDs, deadlines, cancellation, collection, and cleanup to
   the remote provider. Never route a failed remote request to the host-native
   executor.
4. Run the macOS client/protocol/fail-closed suite on GitHub-hosted macOS and
   run the hostile workload conformance suite on the Linux execution host.
   This avoids unsupported nested virtualization while preserving a real VM or
   OS boundary for every strict workload.

## Documentation and delivery

1. Extend `doctor --json`, schemas, README, AUDIT, and CHANGELOG with provider
   prerequisites and exact non-strict/strict boundaries.
2. Run Python, Rust, claims, schema, and platform-contract suites.
3. Commit in reviewable slices (`THE-831`, `THE-828`, `THE-829`, `THE-830`,
   docs/CI), push this branch, open a PR, and attach verification evidence to
   all four Linear issues.

## Portability boundary

- Linux x86_64/ARM64 hosts run the strict service with Docker + `runsc`.
- macOS, Windows, and Linux clients use the same authenticated HTTPS protocol;
  none needs local virtualization or a platform-specific sandbox facility.
- The portable client and protocol are provider-neutral. A future containerd
  or Kubernetes adapter may emit the same `gvisor-v1` receipt, but only after
  equivalent measured startup and per-run evidence exists.
- Native local execution remains available on every supported OS and remains
  explicitly non-strict. Strict selection never degrades to it.
