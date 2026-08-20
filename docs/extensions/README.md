# codecalc extensions

codecalc has four extension kinds. Each has a **versioned interface**, a
**machine-readable manifest**, a **conformance suite**, a **built-in reference
implementation**, and a **reference third-party extension** under
`examples/extensions/`.

| Kind | Module | Interface const | Built-in | Reference ext |
|---|---|---|---|---|
| Execution provider | `codecalc/providers.py` | `PROVIDER_INTERFACE_VERSION` | local / Piston / strict | — |
| Language pack | `codecalc/language_packs.py` | `LANGUAGE_PACK_INTERFACE_VERSION` | `builtin:core` | `examples/extensions/lolcode_pack` |
| Renderer | `codecalc/renderers.py` | `RENDERER_INTERFACE_VERSION` | `builtin:text`, `builtin:markdown-table` | `examples/extensions/csv_renderer` |
| Verifier | `codecalc/verifiers.py` | `VERIFIER_INTERFACE_VERSION` | `builtin:executed` | `examples/extensions/parity_verifier` |

## Trust model — read this first

**An extension is Python that runs IN-PROCESS, and is trusted BY INSTALLATION.**
This is the same posture as a `pip` dependency, a `pytest` plugin, or a Django
app: installing one is the trust decision. A manifest and a permission allowlist
**cannot** confine in-process code — an imported module can `import os` or
`socket` regardless of what it "declared" — so codecalc does not pretend they do.

What the manifest and policy *do* give you is real and useful, just not a
sandbox against a hostile extension author:

- **Provenance** — an optional `integrity` digest (`sha256:…`) the operator pins;
  the loader refuses a payload whose bytes don't match. This proves the code is
  unaltered, not that its author is safe.
- **Operator policy** — a permission *declaration* the operator's policy must
  approve, and a per-permission allowlist (`CODECALC_EXTENSION_ALLOWED_PERMISSIONS`).
- **Audit + discovery** — every loaded extension is enumerable (`codecalc doctor`
  → `extensions`) with id, kind, version, origin, health, and supported operations.
- **A kill switch** — `CODECALC_DISABLE_THIRD_PARTY_EXTENSIONS=1` refuses every
  `third_party`-origin extension; built-ins are unaffected.
- **No impersonation** — the `builtin:` id prefix is reserved; a third-party
  extension that claims it, or collides with a built-in id, is rejected.

The genuinely untrusted input — the **payload** (the user's code) — is confined
by the **execution providers** (gVisor / AppContainer / rlimits), a separate,
enforced layer. An extension never widens that boundary: a language pack only
contributes an argv template; the payload still runs inside the provider sandbox.
A renderer or verifier never executes payload code at all.

Confining a hostile extension *author* (WebAssembly, or out-of-process RPC with
OS-level restrictions) is out of scope for this version and is the boundary to
add if untrusted-author extensions ever become a goal.

## Manifest

Every extension carries an `extensions.ExtensionManifest` (frozen):
`kind`, `extension_id`, `name`, `version`, `interface_version`, `origin`
(`builtin`|`third_party`), `compatible_codecalc`, `compatible_contract`,
`declared_permissions`, `supported_operations`, and optional `integrity`.

Permissions are drawn from a fixed vocabulary (`extensions.KNOWN_PERMISSIONS`);
a permission outside it is refused for any origin.

## Policies

- **Compatibility.** The enforced gate is an **interface-major match**: an
  extension's `interface_version` major must equal the kind's
  `*_INTERFACE_VERSION` major. `compatible_codecalc` / `compatible_contract` are
  declared and surfaced in discovery, compared only by major — codecalc does not
  solve semver *ranges* (a deliberate non-goal; interface-major matching already
  guarantees API compatibility).
- **Deprecation.** An interface is stable within a major. A breaking change bumps
  the kind's major; the previous major is supported for **twelve months** after
  the bump (the same window the result contract uses). Additive, compatible
  changes bump the minor and never break a conforming extension.
- **Signing / integrity.** Pin an extension by `integrity` digest; the loader
  verifies it. For distribution, third-party extensions SHOULD ship a **sigstore
  build-provenance attestation** (the same mechanism codecalc's own release
  artifacts use) and be pinned by digest; verifying a signature against an
  operator-managed keyring is a documented opt-in on top of digest pinning.
- **Security response.** A vulnerability in a built-in extension is handled like
  any codecalc security issue (see `SECURITY.md`). A third-party extension is the
  installer's responsibility, but codecalc gives the operator the tools to
  respond: pin/repin the `integrity` digest, narrow the permission allowlist, or
  disable third-party extensions entirely with the kill switch.

## Verifiers submit evidence — they never grade

A `Verifier.verify()` returns an immutable `Evidence` record (outcome ∈
`supports`/`refutes`/`inconclusive`) — there is **no grade/score field**.
`grades.py` alone maps evidence to a grade. The `VerifierRegistry.collect_evidence`
path (a) hands each verifier a deep copy of the claim/context so it cannot poison
another verifier or the grader, (b) isolates each call, and (c) drops any evidence
for a `claim_kind` the verifier did not declare. A single third-party `supports`
is advisory input, not a verdict.

## Discovering what's loaded

`codecalc doctor` includes an `extensions` block: the framework version, the
operator policy (`allow_third_party`, the allowlist), and every loaded
extension's descriptor. Built-ins load always; third-party extensions load only
when the operator wires them and the policy permits.
