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
- `capabilities`: boolean support declarations

Version 1 defines capability keys for `execute`, `inspect`, `stream`, `cancel`,
`cleanup`, `files`, `artifacts`, `sessions`, `health`, `runtime_discovery`, and
`workdir`, and `network_control`. Callers must not infer support from provider
identity.

## Required operations

All providers implement `describe()`, `health()`, `list_runtimes()`, and
`execute(spec)`. Health reports an explicit readiness boolean and runtime
discovery returns JSON-serializable entries. Optional operations remain present
on the interface so their failure is uniform. Calling one that the descriptor
marks false raises `UnsupportedCapability` with the stable provider error code
`unsupported_capability`; CodeCalc must never silently route that operation to
a weaker provider.

Provider selection is explicit or uses the configured default. An unknown
explicit ID returns a validation result carrying `provider_error` set to
`unknown_provider`; it never falls back.

Every successful execution receipt carries provider ID, provider version,
interface version, and host class. Compact results retain this provenance.

Cross-provider verification executes the same immutable `ComputationSpec`
independently through two explicitly selected providers. It compares semantic
result fields (`ok`, verdict, stdout, stderr, and exit code), excludes timing and
resource telemetry from agreement, and retains both provider receipts.

## Included providers

`local` wraps the existing Rust executor or Python fallback. It remains the
default and requires no configuration.

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

The Piston adapter reports `backend="python"` in the version-1 result envelope
because that closed field identifies CodeCalc's adapter implementation. The
independent provider receipt identifies `provider_id="piston"`; adding Piston
to the closed backend enum would break strict version-1 schema validators.

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
