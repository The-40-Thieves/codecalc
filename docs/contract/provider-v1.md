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
`cleanup`, `files`, `artifacts`, `sessions`, and `network_control`. Callers must
not infer support from provider identity.

## Required operations

All providers implement `describe()` and `execute(spec)`. Optional operations
remain present on the interface so their failure is uniform. Calling one that
the descriptor marks false raises `UnsupportedCapability` with the stable
provider error code `unsupported_capability`; CodeCalc must never silently
route that operation to a weaker provider.

Provider selection is explicit or uses the configured default. An unknown
explicit ID returns a validation result carrying `provider_error` set to
`unknown_provider`; it never falls back.

Every successful execution receipt carries provider ID, provider version,
interface version, and host class. Compact results retain this provenance.

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
