# CodeCalc execution-provider interface

Current interface version: `1.0.0`

This is the protocol-neutral boundary between CodeCalc and an execution
backend. MCP, HTTP, CLI, and library adapters compile their inputs into a
`ComputationSpec`; providers return CodeCalc's existing execution-result
contract. Transport types do not cross this boundary.

## Request

`ComputationSpec` contains the language, source, stdin, timeout, optional
working directory, memory/output/CPU ceilings, and network policy. New request
fields must have defaults so an older provider can reject an unsupported
capability explicitly rather than misinterpreting a request.

## Provider descriptor

Every provider publishes a JSON-serializable descriptor containing:

- `interface_version`: version of this interface
- `provider_id`: stable selection key
- `provider_version`: provider implementation version
- `host_class`: local, region, or other placement class
- `strict`: whether the provider claims a security boundary for hostile code
- `capabilities`: boolean support declarations

Version 1 defines capability keys for `execute`, `managed_runs`, `inspect`, `stream`, `cancel`,
`cleanup`, `files`, `artifacts`, `sessions`, `health`, `runtime_discovery`, and
`workdir`, and `network_control`. Callers must not infer support from provider
identity.

`managed_runs=true` means the provider receives CodeCalc's run ID before the
payload starts, so later inspection, cancellation, and cleanup target the same
owned workload rather than an unrelated provider-generated ID.

## Required operations

All providers implement `describe()`, `health()`, `list_runtimes()`,
`execute(spec)`, and async `execute_stream(spec, on_progress=...)`. Health reports an explicit readiness boolean and runtime
discovery returns JSON-serializable entries. Optional operations remain present
on the interface so their failure is uniform: `inspect`, `stream`, `cancel`,
`cleanup`, `collect_artifacts`, `list_files`, and `create_session`. Calling one
that the descriptor marks false raises `UnsupportedCapability` with the stable
provider error code `unsupported_capability`; CodeCalc must never silently
route that operation to a weaker provider. A provider advertising `stream=true`
executes the canonical spec and may invoke the protocol-neutral progress
callback; transport objects such as MCP contexts never cross this boundary.

Provider selection is explicit or uses the configured default. An unknown
explicit ID returns a validation result carrying `provider_error` set to
`unknown_provider`; it never falls back.

## The execution receipt

Every successful execution result carries a `provider` object — the execution
receipt — versioned by its own `receipt_version` (currently **1.1.0**) so a
reader can tell "this receipt predates source hashes" from "this run had no
source", which are not the same fact. MAJOR removes or retypes a key, MINOR adds
one, PATCH changes descriptions only.

| Key | What it says |
|---|---|
| `receipt_version` | Semver of this block. |
| `provider_id`, `provider_version`, `interface_version`, `host_class` | Who ran it. |
| `spec_hash` | `sha256:<hex>` naming the request by content — see [the request contract](README.md#the-request-half-a-canonical-spec-and-its-content-hash). |
| `spec_schema_version` | The canonical form `spec_hash` was taken under. Without it a stored hash is a number whose meaning has an undocumented expiry. |
| `source_sha256` | `sha256:<hex>` over the source that was executed, independent of the limits wrapped around it. |
| `determinism` | `locale` (`LANG`/`LC_ALL`), `timezone`, `seed`, and an `unrecorded` list. |
| `limits` | The requested-versus-enforced receipt, below. |

The limits receipt records the canonical requested values, the controls the
provider reports as enforced, and the exact `unenforced` disclosures returned
with the result. “Provider reported” is deliberate: CodeCalc preserves the
backend's enforcement claim; it does not independently attest to a remote host's
configuration.

**`determinism` reports what it observed and names what it did not.** Every input
appears either with a value or in `unrecorded` — never as a bare `null`, because
an unexplained null reads identically to "the value was empty" and to "this key
was never populated". Three facts are stated rather than smoothed over:

* `TZ` is **not** in the executor's environment allowlist, so the child inherits
  the host's system zone and CodeCalc cannot name it. `timezone` is unrecorded
  rather than guessed from the server's own zone, which describes a different
  process. Add `TZ` to the allowlist and this populates itself — the block is
  read back out of `executor._env()`, not transcribed from it.
* CodeCalc has **no seed concept**: no tool accepts one and no runtime is seeded.
  `seed` is `null` on every run and says why.
* Only the `local` provider runs through CodeCalc's own sandbox. For any other,
  `source` is `provider_owned` and nothing is reported, because this server's
  locale is not the locale of a run that happened on someone else's host.

**The receipt is portable JSON.** No secret and no machine-specific absolute path
reaches it: `workdir` enters only through `spec_hash`, never as text, and the
determinism block reads exactly three names out of the forwarded environment
rather than copying it, so `PATH` and `HOME` cannot ride along.

Compact results retain the actionable half — `receipt_version`, `provider_id`,
`spec_hash` and the whole `limits` object — and name the descriptive keys they
dropped in `receipt_detail`. Copying the receipt wholesale grew a compact result
by 69%, which defeats the mode it rides in; the split follows the same rule
already applied to `unenforced`.

Cross-provider verification executes the same immutable `ComputationSpec`
independently through two explicitly selected providers. It compares semantic
result fields (`ok`, verdict, stdout, stderr, and exit code), excludes timing and
resource telemetry from agreement, and retains both provider receipts.

## Included providers

`local` wraps the existing Rust executor or Python fallback. It remains the
default and requires no configuration. It advertises streaming: the native
executor reports partial stdout through the progress callback, while the
Python fallback completes synchronously and returns `streamed=false` with an
explicit note rather than pretending it emitted progress.

`piston` targets the open-source Piston v2 HTTP API. It is registered only when
`CODECALC_PISTON_URL` contains an absolute HTTP(S) base URL; CodeCalc has no
default public endpoint. `CODECALC_PISTON_AUTHORIZATION`, when set, is copied
only to that provider's `Authorization` header. Both the complete header value
and its credential portion are redacted from normalized results.

Piston's synchronous `/api/v2/execute` surface supports execution and resource
limits but not run inspection, streaming, cancellation, sessions, artifact
retrieval, or caller-selected working directories. Those capabilities are
advertised false and fail with `UnsupportedCapability`. Network isolation is a
Piston server setting rather than a per-request control, so `no_net=True` also
fails explicitly instead of assuming the remote deployment's configuration.
Piston's output ceiling is likewise server-wide rather than request-scoped;
CodeCalc applies a requested `max_output_kb` to the returned buffers, classifies
overflow as `OLE`, and records
`max_output_kb_enforced_after_provider_response` in `unenforced` so this
post-response protection is never presented as remote resource enforcement.
Piston health converts connection/JSON failures into `ready: false`; execution
converts them into a rejected result with
`provider_error="provider_transport_failure"`. Both paths redact configured
authorization material before returning data to an adapter.

The Piston adapter reports `backend="python"` in the version-1 result envelope
because that closed field identifies CodeCalc's adapter implementation. The
independent provider receipt identifies `provider_id="piston"`; adding Piston
to the closed backend enum would break strict version-1 schema validators.

`<host>-strict` is always discoverable for the current client OS. Without
`CODECALC_STRICT_URL` it is an unavailable, fail-closed descriptor: selecting
it returns `strict_provider_unavailable` and executes nothing. With an explicit
URL it becomes an authenticated adapter to CodeCalc's Linux strict execution
service. `CODECALC_STRICT_AUTHORIZATION` is sent only in the Authorization
header and is redacted from errors and metadata.

Before sending source, the adapter requires `/v1/health` to report a compatible
provider interface, `ready=true`, `strict=true`, `isolation_profile=gvisor-v1`,
and all of `application_kernel`, `cgroup_v2`, `namespaces`, `seccomp`,
`read_only_rootfs`, `non_root`, `capabilities_dropped`, `filesystem`, `network`,
`descendants`, and `resource_limits` as enforced. Missing controls produce
`strict_attestation_failed`; the execution endpoint is never called. Each
successful execution must repeat those controls in its receipt. Managed run
IDs are used in execute, inspect, cancel, and cleanup paths.

The Linux service profile uses a digest-pinned executor image under Docker's
explicitly registered gVisor `runsc` runtime. It supports Linux x86_64 and
ARM64; gVisor's default `systrap` platform avoids requiring KVM. Landlock may
still harden local execution, but it is not evidence for the gVisor boundary.

## Versioning policy

The interface uses semantic versioning independently of the execution-result
contract:

- MAJOR: removes or changes an operation, field, capability meaning, or error
  behavior.
- MINOR: adds an optional capability, descriptor field, or request field with a
  backward-compatible default.
- PATCH: documentation and implementation corrections that do not change the
  provider wire or call contract.

A provider with an unsupported MAJOR interface version must not be registered.
Consumers ignore unknown descriptor fields and capability keys, treating
unknown capabilities as unsupported.

## Trust boundary

CodeCalc owns request normalization, provider selection, result contracts,
receipts, policy, and verification. A provider owns only the enforcement it
advertises. Provider capabilities are claims to be checked by the conformance
suite; they do not expand CodeCalc's guarantee unless that provider passes the
corresponding tests.

CodeCalc guarantees canonical requests/results, explicit unsupported errors,
credential redaction, and provider provenance. Piston owns the isolation,
installed runtimes, configured ceilings, availability, and cleanup of its
remote job environment. A successful conformance run checks observable API
behavior; it is not an audit of the remote host's sandbox configuration.
