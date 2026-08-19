# Extension SDK — versioned language packs, renderers, verifiers (THE-794)

**Status:** design, 2026-08-19. Execution *providers* already ship this shape
(`codecalc/providers.py`: `PROVIDER_INTERFACE_VERSION`, a `Protocol`, a frozen
descriptor, a `ProviderRegistry` with interface-major validation, and
`tests/_provider_conformance.py`). This extends the same, proven pattern to the
other three extension types and adds the cross-cutting trust/discovery layer the
ticket's acceptance criteria require.

The original ticket parked three of four types on "an SDK designed before its
second consumer is guessed API." This design answers that directly: **every new
interface ships with a built-in reference implementation *and* a reference
third-party extension** — the second consumer that validates the shape, not a
guess.

## Threat model — what this protects against, and what it does NOT

A gemini-3.1-pro architecture review (2026-08-19) flagged the load-bearing point:
**an extension is Python that runs IN-PROCESS, so a manifest and a permission
allowlist cannot confine it** — once imported it can `import os`/`socket` or
monkeypatch internals no matter what it "declared." Pretending otherwise would
be the exact dishonesty this codebase's disclosure discipline exists to avoid.

So the scope is stated plainly and the SDK does not overclaim:

- **Extensions are operator-installed and trusted-by-installation** — the same
  trust posture as a `pip` dependency, a `pytest` plugin, or a Django app. The
  operator chooses to install one; installing it IS the trust decision.
- The **untrusted** thing is the executed **payload** (the user's code), and it
  is confined by the **execution providers** (gVisor / AppContainer / rlimits) —
  a different, already-built layer. Extensions do not widen that boundary: a
  language pack contributes an argv template, but the payload still runs inside
  the provider sandbox; a renderer/verifier never executes payload code.
- The extension trust model is therefore **provenance + operator policy +
  audit + a kill switch**, not runtime confinement of extension code:
  integrity-digest pinning, an explicit permission *declaration* the operator
  policy must approve, an auditable record of what loaded, and the ability to
  disable third-party extensions entirely. This is real, useful control over
  *what an operator runs*; it is not, and is documented not to be, a sandbox
  against a hostile extension author. Confining hostile extension code (WASM /
  out-of-process RPC) is explicitly out of scope for v1.0.0 and noted as the
  future boundary if untrusted-author extensions are ever a goal.

## Shared layer — `codecalc/extensions.py`

One place for what is common to all extension kinds, so a kind's own module
(providers/language_packs/renderers/verifiers) stays about its domain.

```python
EXTENSION_FRAMEWORK_VERSION = "1.0.0"

class ExtensionKind(str, Enum):
    PROVIDER = "provider"
    LANGUAGE_PACK = "language_pack"
    RENDERER = "renderer"
    VERIFIER = "verifier"

@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    kind: ExtensionKind
    extension_id: str            # unique within a kind; see identity rules
    name: str
    version: str                 # the implementation's own version (semver)
    interface_version: str       # the kind interface version it implements
    origin: str                  # "builtin" | "third_party"
    compatible_codecalc: str     # semver range it supports, e.g. ">=0.2,<0.3"
    compatible_contract: str     # result-contract range, e.g. ">=1.2,<2"
    declared_permissions: tuple[str, ...]   # capability strings it requests
    supported_operations: tuple[str, ...]   # for capability discovery
    integrity: str | None = None            # sha256:... of the extension payload, if signed
    def to_dict(self) -> dict: ...
```

### Identity, trust, policy (the acceptance criteria that aren't per-type)

- **No impersonation.** `extension_id` for a `third_party` extension may **not**
  use the reserved `builtin:` prefix, nor collide with a registered builtin id.
  Registration rejects both with `extension_identity_conflict`. Built-ins
  register with `origin="builtin"` and a `builtin:`-prefixed id.
- **Explicit, policy-controlled permissions.** An `ExtensionPolicy` (built from
  env) holds an allowlist of permitted capability strings and
  `allow_third_party: bool`. Registration of a third-party extension whose
  `declared_permissions` exceed the allowlist fails
  `extension_permission_denied`. Nothing is granted implicitly.
- **Disable third-party entirely.** `CODECALC_DISABLE_THIRD_PARTY_EXTENSIONS=1`
  sets `allow_third_party=False`; any `third_party` registration then fails
  `extension_disabled`. Built-ins are unaffected.
- **Integrity + trust model.** Enforced at the admission point: `register(ext,
  payload=...)` recomputes the payload sha256 when the manifest pins
  `integrity`, refuses on mismatch, and ALSO refuses a pinned manifest with no
  payload to check against (an unverifiable pin must not pass as verified) —
  `extension_integrity_failure` in both cases. A built-in (no pin) needs no
  payload. The documented distribution trust model
  reuses the release story: third-party extensions SHOULD be distributed with a
  sigstore attestation (same mechanism as codecalc's own artifacts) and pinned
  by digest; the loader verifies the declared digest and the operator pins which
  origins/publishers are trusted. (Signature *verification* against a keyring is
  a documented, opt-in extension point; digest-pinning is enforced now.)
- **Version compatibility.** The ENFORCED gate is exactly what
  `ProviderRegistry` already does: the extension's `interface_version` MAJOR must
  equal the kind's `*_INTERFACE_VERSION` major, else
  `extension_interface_mismatch`. `compatible_codecalc` / `compatible_contract`
  are recorded and surfaced in discovery as the extension's *declared* support,
  and checked only by MAJOR (a compatible-major compare), NOT by solving a semver
  RANGE — pip/npm-style range resolution is a deliberate non-goal for v1.0.0
  (interface-major matching already guarantees API compatibility).
- **Isolation + stable error codes.** Every registry operation that calls into
  an extension wraps it: an exception becomes an `ExtensionOperationFailure`
  (`.code = f"extension_{op}_failed"`) carrying the extension id, never
  propagating a raw crash. Shared codes live here:
  `unsupported_capability`, `unknown_extension`, `extension_interface_mismatch`,
  `extension_disabled`, `extension_integrity_failure`,
  `extension_permission_denied`, `extension_identity_conflict`,
  `extension_operation_failed`.
- **Capability discovery.** `describe_extensions()` returns, across all kinds,
  each extension's `{kind, extension_id, name, version, origin, health,
  supported_operations}` — the machine-readable capability document.

`ExtensionRegistry` is a small generic base (`register`, `get`, `descriptors`,
`guard`) that the three NEW per-kind registries subclass. The shipped
`ProviderRegistry` predates this base and is deliberately left untouched (its own
identity/version checks keep working); unifying it onto the shared base is
optional future cleanup, not part of this change — the two implementations agree
in behaviour but are, for now, maintained separately.

## The three new kind interfaces (each mirrors `providers.py`)

### `codecalc/language_packs.py` — `LANGUAGE_PACK_INTERFACE_VERSION = "1.0.0"`
A `LanguagePack` describes how a set of languages is discovered, compiled,
invoked, and how its diagnostics normalize. Built-in `BuiltinLanguagePack` wraps
the existing `registry.LANGUAGES/ALIASES/EXTENSIONS`. Reference third-party:
`examples/extensions/lolcode_pack/` — one toy language, proving discovery →
run → normalized diagnostics through the same registry the server uses.
```python
class LanguagePack(Protocol):
    manifest: ExtensionManifest
    def languages(self) -> list[dict]: ...      # id, aliases, extension, compiled?
    def run_plan(self, language: str) -> list[str]: ...   # argv template
    def compile_plan(self, language: str) -> list[str] | None: ...
    def normalize_diagnostics(self, language: str, stderr: str) -> dict: ...
    def health(self) -> dict: ...
```

### `codecalc/renderers.py` — `RENDERER_INTERFACE_VERSION = "1.0.0"`
A `Renderer` turns a result envelope (or a typed payload) into a representation
(table, chart spec, notebook, report), declaring the formats it can emit and
whether it `can_render` a given result — never mutating the result. Built-in:
`TextRenderer` (the current stdout/verdict view) + a `MarkdownTableRenderer`.
Reference third-party: `examples/extensions/csv_renderer/`.
```python
class Renderer(Protocol):
    manifest: ExtensionManifest
    def formats(self) -> list[str]: ...
    def can_render(self, result: dict) -> bool: ...
    def render(self, result: dict, fmt: str) -> dict: ...   # {format, media_type, body}
    def health(self) -> dict: ...
```

### `codecalc/verifiers.py` — `VERIFIER_INTERFACE_VERSION = "1.0.0"`
A `Verifier` inspects a claim and **submits evidence**; `grades.py` alone maps
evidence → grade. Four safeguards, from the gemini review, keep a verifier from
assigning trust even so:
- **`Evidence` is a frozen dataclass** (`claim_kind`, `verifier_id`, `outcome ∈
  {supports, refutes, inconclusive}`, `detail`, `cost`) — not a mutable dict, so
  the record the grader reads cannot be reshaped after the fact.
- **The registry passes DEEP COPIES** of `claim`/`context` into `verify`, so a
  verifier that mutates its arguments in place cannot poison the data the grader
  or a later verifier relies on.
- **Declared-claim scoping:** the registry DROPS any evidence whose `claim_kind`
  is not in the verifier's own `claims()` — a verifier cannot speak to a claim it
  never declared.
- **Advisory weighting:** evidence is an INPUT, never a verdict. `grades.py`
  keeps its versioned rules; a single `supports` does not lift a grade. Built-in
  evidence and third-party evidence are weighed by policy — third-party verifier
  evidence corroborates, it is not sufficient alone — so a lying/colluding
  third-party verifier cannot unilaterally certify a fraudulent claim.

Built-in verifiers wrap the existing executed / cross-checked / solver-proven
evidence sources. Reference third-party: `examples/extensions/parity_verifier/`.
```python
class Verifier(Protocol):
    manifest: ExtensionManifest
    def claims(self) -> list[str]: ...          # claim kinds it may speak to
    def verify(self, claim: dict, context: dict) -> Evidence: ...  # evidence, never a grade
    def health(self) -> dict: ...
```
The same "typed record, not an opaque dict" principle applies to the renderer's
return (`RenderOutput`: `format`, `media_type`, `body`) and every descriptor.
Core operations stay synchronous to match the shipped provider `Protocol` (whose
`execute` is sync); an `async` variant, if an I/O-bound verifier needs one, is a
future interface-version addition, not a v1.0.0 shape change.

## Conformance suites (one per kind, reusable)
`tests/_language_pack_conformance.py`, `_renderer_conformance.py`,
`_verifier_conformance.py`, each exposing `run_*_conformance(impl, check)` and
asserting: manifest JSON-serializable + identity/version rules; health reports
`ready: bool`; discovery returns the declared shape; the core operation works on
a known input; optional ops either work or raise `UnsupportedCapability`;
**verifiers additionally** must return evidence whose schema carries no grade
field (a verifier that tries to grade fails conformance). `tests/test_*s.py` run
each built-in and each reference extension through the suite, wired into
`ci-python.yml` beside the existing gates.

## Docs
- `docs/extensions/README.md` — the four kinds, the manifest, the trust model
  (identity, permissions, integrity/signing, the disable switch), and the
  compatibility / deprecation / security-response policies.
- `docs/contract/language-pack-v1.md`, `renderer-v1.md`, `verifier-v1.md` — the
  published per-kind interface specs, versioned like `provider-v1.md`.

## Acceptance-criteria coverage
Each numbered criterion maps to: machine-readable manifest (ExtensionManifest +
describe_extensions); policy-can't-exceed-request (permission allowlist);
missing enforcement explicit (capabilities dict + unsupported_capability);
requested-vs-enforced (providers already; manifests carry declared vs the
conformance-verified supported_operations); default-deny + policy (disable
switch + allowlist); reference extensions for all four; evidence-not-grades
(verifier contract); identity/no-impersonation (reserved prefix); integrity +
trust model (integrity digest + documented signing); discovery
(describe_extensions); disable-third-party (env switch); conformance per kind.

## Build phases
1. Shared `extensions.py` (manifest, policy, registry base, error codes,
   discovery) as NEW code the three new kinds use; `ProviderRegistry` is left
   as-is (unifying it is optional future cleanup) + tests.
2. Language packs: interface + BuiltinLanguagePack + conformance + reference ext.
3. Renderers: interface + Text/MarkdownTable + conformance + reference ext.
4. Verifiers: interface + built-ins (evidence-only) + conformance + reference ext.
5. Docs + CI wiring + `describe_extensions` surfaced (doctor / an MCP capability).
