# Embedding CodeCalc

CodeCalc's supported in-process boundary is the protocol-neutral application
service layer. MCP is one adapter over this layer, not the place computation or
session semantics are defined.

## Supported boundary

Embedded Python callers may construct and use:

- `providers.ComputationSpec` as the canonical execution request;
- `providers.ProviderRegistry` and documented `ExecutionProvider`
  implementations for discovery and selection;
- `execution_service.ExecutionService` for fresh execution, provider receipts,
  policy routing, and cross-provider verification;
- `execution_service.SessionService` for session execution, lifecycle,
  bounded workspace reads, multi-file entry execution, workspace writes and
  listings, and artifacts; and
- the versioned result and provider contracts under `docs/contract/`.

Adapters compile transport inputs into these request objects and return the
service result unchanged except for transport-only representation concerns,
such as converting image bytes into an MCP `ImageContent`. Authorization and
provider-routing policy belong at or below the shared service boundary, not in
individual adapters.

`SessionService.read_file()` returns bytes and MIME metadata without importing
MCP types; adapters decide how to render those bytes. `SessionService.run_file()`
executes an entry file in the session workspace so relative imports and data
paths retain the same behavior across MCP and embedded callers.
`SessionService.list_files()` accepts optional `page_size` and opaque `cursor`
arguments; omitting them preserves the original unpaginated response.

`ExecutionService.execute_stream()` selects providers with the same explicit
or policy routing as `execute()`. Its progress callback uses only byte counts
and messages. MCP adapters translate those values into protocol progress
notifications; provider and computation identity remain free of transport
metadata.

## Not an embedding API

The following are implementation details and may change without compatibility
support:

- private names in `sessions.py`, including `_session_dir`, `_jail`, worker
  maps, and directory-identity bookkeeping;
- the on-disk layout beneath `CODECALC_SESSION_ROOT`;
- executor temporary directories and runner filenames;
- MCP decorator objects, request contexts, and transport middleware; and
- direct access to provider credential fields or transport internals.

Callers must use `SessionService` for workspace and artifact access rather than
opening session directories directly. This preserves path confinement,
identity-checked cleanup, output limits, result stamping, and future storage
changes behind one boundary.

## Compatibility

Execution results follow `contract_version`; provider implementations follow
`PROVIDER_INTERFACE_VERSION`. Additive service methods and optional provider
capabilities are backward-compatible. Removing or changing existing request,
result, error, or capability semantics follows the corresponding documented
major-version policy.
