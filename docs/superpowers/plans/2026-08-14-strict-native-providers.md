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

1. Add failing capability-probe and launch-receipt tests.
2. Add a dedicated Linux launcher that creates a cgroup-v2 leaf, writes
   `pids.max`, `memory.max`, and CPU controls before execution, enters isolated
   namespaces, applies `no_new_privs`, seccomp, and Landlock, and reports every
   verified primitive in its receipt.
3. Require kernel/controller/delegation prerequisites in `doctor`; strict
   selection fails before executing payload code if any required primitive is
   absent.
4. Kill via `cgroup.kill`, reap, and remove the cgroup deterministically.
5. Add Linux CI probes for fork/memory bombs, descendants, egress, filesystem
   escape, cancellation, and cleanup.

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
2. Require a versioned readiness handshake that proves the remote provider is
   strict and reports cgroup, namespace, seccomp, Landlock, filesystem, and
   network controls. Reject incomplete or incompatible receipts.
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
