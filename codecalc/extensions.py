"""codecalc extension framework (THE-794).

Shared machinery for the extension kinds. Execution *providers* predate this
module and keep their own ``providers.ProviderRegistry``; language packs,
renderers, and verifiers each define their interface ``Protocol`` and a registry
that subclasses ``ExtensionRegistry`` here.

Trust model, stated so nothing is overclaimed (see
``docs/design/2026-08-19-extension-sdk.md``): an extension is Python that runs
IN-PROCESS and is trusted BY INSTALLATION — exactly like a ``pip`` dependency or
a ``pytest`` plugin. A manifest and a permission allowlist cannot confine
in-process code, so they do not pretend to: they are provenance, operator
policy, audit, and a kill switch over *what an operator runs* — not a sandbox
against a hostile extension author. The untrusted thing, the executed payload,
is confined by the execution providers (gVisor / AppContainer / rlimits), a
separate layer. Confining a hostile extension author (WASM / out-of-process RPC)
is out of scope for this version.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Generic, TypeVar

EXTENSION_FRAMEWORK_VERSION = "1.0.0"

#: Reserved id namespace: only built-in extensions may use it, which is how a
#: third-party extension is prevented from impersonating a built-in identity.
BUILTIN_ID_PREFIX = "builtin:"


class ExtensionKind(StrEnum):
    """The extension kinds. String-valued so the enum serializes as its name."""

    PROVIDER = "provider"
    LANGUAGE_PACK = "language_pack"
    RENDERER = "renderer"
    VERIFIER = "verifier"


#: The permission vocabulary an extension may DECLARE. A capability outside this
#: set is unknown and refused at registration; the operator policy may further
#: narrow which of these a third-party extension is allowed to declare. These are
#: DECLARATIONS the operator gates and audits, not runtime-enforced sandbox rights
#: (see the module docstring) — the payload sandbox is the enforcement layer.
KNOWN_PERMISSIONS = frozenset(
    {
        "execute",     # contribute a runtime/argv the payload runs under
        "network",     # ask codecalc to permit network for a run (brokered)
        "filesystem",  # ask for workspace/object file access (brokered)
        "packages",    # ask for package installation (brokered)
        "subprocess",  # spawn helper processes as part of its own work
        "secrets",     # request an opaque secret reference (brokered)
        "render",      # format results into a representation
        "verify",      # submit verification evidence
    }
)


def _major(version: str) -> str:
    """The MAJOR component of a semver string. Interface compatibility is a major
    match, deliberately not a range solve (THE-794 design)."""
    return version.split(".", 1)[0].strip()


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    """The machine-readable capability document for one extension."""

    kind: ExtensionKind
    extension_id: str
    name: str
    version: str
    interface_version: str
    origin: str  # "builtin" | "third_party"
    compatible_codecalc: str
    compatible_contract: str
    declared_permissions: tuple[str, ...] = ()
    supported_operations: tuple[str, ...] = ()
    #: "sha256:<hex>" of the extension payload, if the operator pins it.
    integrity: str | None = None

    def to_dict(self) -> dict:
        # A JSON-friendly capability document: enum → its string, tuples → lists.
        data = asdict(self)
        data["kind"] = self.kind.value
        data["declared_permissions"] = list(self.declared_permissions)
        data["supported_operations"] = list(self.supported_operations)
        return data

    @property
    def is_builtin(self) -> bool:
        return self.origin == "builtin"


# ── stable error taxonomy (each carries a `.code`, like providers.py) ─────────
class ExtensionError(RuntimeError):
    """Base for extension-framework failures; each subclass has a stable `.code`."""

    code = "extension_error"


class ExtensionInterfaceMismatch(ExtensionError):
    code = "extension_interface_mismatch"

    def __init__(self, extension_id: str, offered: str, required: str) -> None:
        self.extension_id = extension_id
        self.offered = offered
        self.required = required
        super().__init__(
            f"extension {extension_id!r} implements interface {offered!r}; "
            f"a compatible {required!r} is required"
        )


class UnknownExtension(ExtensionError):
    code = "unknown_extension"

    def __init__(self, kind: ExtensionKind, extension_id: str, available: tuple[str, ...]) -> None:
        self.kind = kind
        self.extension_id = extension_id
        self.available = available
        choices = ", ".join(available) or "none"
        super().__init__(
            f"unknown {kind.value} extension {extension_id!r}; available: {choices}"
        )


class ExtensionIdentityConflict(ExtensionError):
    code = "extension_identity_conflict"

    def __init__(self, extension_id: str, reason: str) -> None:
        self.extension_id = extension_id
        self.reason = reason
        super().__init__(f"extension id {extension_id!r} rejected: {reason}")


class ExtensionDisabled(ExtensionError):
    code = "extension_disabled"

    def __init__(self, extension_id: str) -> None:
        self.extension_id = extension_id
        super().__init__(
            f"third-party extension {extension_id!r} refused: third-party "
            f"extensions are disabled (CODECALC_DISABLE_THIRD_PARTY_EXTENSIONS)"
        )


class ExtensionPermissionDenied(ExtensionError):
    code = "extension_permission_denied"

    def __init__(self, extension_id: str, denied: tuple[str, ...]) -> None:
        self.extension_id = extension_id
        self.denied = denied
        super().__init__(
            f"extension {extension_id!r} declares permissions the policy does not "
            f"allow: {', '.join(denied)}"
        )


class ExtensionIntegrityFailure(ExtensionError):
    code = "extension_integrity_failure"

    def __init__(self, extension_id: str, expected: str, actual: str) -> None:
        self.extension_id = extension_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"extension {extension_id!r} integrity mismatch: pinned {expected!r}, "
            f"payload is {actual!r}"
        )


class UnsupportedCapability(ExtensionError):
    """An extension was asked for an operation it did not advertise."""

    code = "unsupported_capability"

    def __init__(self, extension_id: str, capability: str) -> None:
        self.extension_id = extension_id
        self.capability = capability
        super().__init__(
            f"extension {extension_id!r} does not support capability {capability!r}"
        )


class ExtensionOperationFailure(ExtensionError):
    """An extension operation raised; isolated and re-raised with a stable code so
    one extension cannot crash the server."""

    def __init__(self, extension_id: str, operation: str, cause: BaseException | None = None) -> None:
        self.extension_id = extension_id
        self.operation = operation
        self.code = f"extension_{operation}_failed"
        super().__init__(f"extension {extension_id!r} {operation} failed: {cause}")


# ── integrity ─────────────────────────────────────────────────────────────────
def compute_integrity(payload: bytes) -> str:
    """The ``sha256:<hex>`` digest an operator pins in a manifest's ``integrity``."""
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def verify_integrity(manifest: ExtensionManifest, payload: bytes) -> None:
    """Refuse a payload whose digest does not match the pinned one. A no-op when
    nothing is pinned — integrity is opt-in provenance, and (per the trust model)
    proves the payload is unaltered, NOT that its author is trusted."""
    if manifest.integrity is None:
        return
    actual = compute_integrity(payload)
    if actual != manifest.integrity:
        raise ExtensionIntegrityFailure(manifest.extension_id, manifest.integrity, actual)


# ── policy ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ExtensionPolicy:
    """Operator control over which extensions may load and what they may declare."""

    allow_third_party: bool = True
    allowed_permissions: frozenset[str] = KNOWN_PERMISSIONS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ExtensionPolicy:
        env = os.environ if environ is None else environ
        disabled = env.get("CODECALC_DISABLE_THIRD_PARTY_EXTENSIONS", "") in {"1", "true", "True"}
        raw = env.get("CODECALC_EXTENSION_ALLOWED_PERMISSIONS", "").strip()
        if raw:
            allowed = frozenset(p.strip() for p in raw.split(",") if p.strip()) & KNOWN_PERMISSIONS
        else:
            allowed = KNOWN_PERMISSIONS
        return cls(allow_third_party=not disabled, allowed_permissions=allowed)


# ── registry base ────────────────────────────────────────────────────────────
T = TypeVar("T")


def _manifest_of(extension: object) -> ExtensionManifest:
    manifest = getattr(extension, "manifest", None)
    if not isinstance(manifest, ExtensionManifest):
        raise ExtensionIdentityConflict(
            getattr(extension, "extension_id", repr(extension)),
            "extension has no ExtensionManifest `manifest` attribute",
        )
    return manifest


class ExtensionRegistry(Generic[T]):
    """Register, discover, and safely call extensions of one kind. Subclassed by
    each kind's registry (language packs, renderers, verifiers). Enforces the
    identity, version, permission, and disable rules at registration; isolates
    every call into an extension via :meth:`guard`."""

    def __init__(
        self,
        kind: ExtensionKind,
        interface_version: str,
        *,
        policy: ExtensionPolicy | None = None,
    ) -> None:
        self.kind = kind
        self.interface_version = interface_version
        self.policy = policy or ExtensionPolicy.from_env()
        self._by_id: dict[str, T] = {}

    def register(self, extension: T, *, payload: bytes | None = None) -> None:
        """Admit an extension after the identity/version/permission/disable
        gates. `payload` is the extension's own bytes: when the manifest PINS an
        `integrity` digest it is verified here, and pinning WITHOUT a payload to
        check against is itself refused — an unverifiable pin must never pass as
        if it had been verified. A manifest with `integrity=None` is unpinned and
        needs no payload."""
        manifest = _manifest_of(extension)
        if manifest.kind != self.kind:
            raise ExtensionIdentityConflict(
                manifest.extension_id,
                f"declares kind {manifest.kind.value!r}, this registry is {self.kind.value!r}",
            )
        # Integrity is enforced at the admission point, not merely recorded.
        if manifest.integrity is not None:
            if payload is None:
                raise ExtensionIntegrityFailure(
                    manifest.extension_id, manifest.integrity, "no payload supplied to verify"
                )
            verify_integrity(manifest, payload)
        if _major(manifest.interface_version) != _major(self.interface_version):
            raise ExtensionInterfaceMismatch(
                manifest.extension_id, manifest.interface_version, self.interface_version
            )
        # A permission outside the known vocabulary is refused for ANY origin.
        unknown = tuple(p for p in manifest.declared_permissions if p not in KNOWN_PERMISSIONS)
        if unknown:
            raise ExtensionPermissionDenied(manifest.extension_id, unknown)
        # Identity + origin rules.
        if manifest.origin == "builtin":
            if not manifest.extension_id.startswith(BUILTIN_ID_PREFIX):
                raise ExtensionIdentityConflict(
                    manifest.extension_id, f"a builtin id must use the {BUILTIN_ID_PREFIX!r} prefix"
                )
        elif manifest.origin == "third_party":
            if manifest.extension_id.startswith(BUILTIN_ID_PREFIX):
                raise ExtensionIdentityConflict(
                    manifest.extension_id,
                    f"a third-party extension may not use the reserved {BUILTIN_ID_PREFIX!r} prefix",
                )
            if not self.policy.allow_third_party:
                raise ExtensionDisabled(manifest.extension_id)
            denied = tuple(
                p for p in manifest.declared_permissions if p not in self.policy.allowed_permissions
            )
            if denied:
                raise ExtensionPermissionDenied(manifest.extension_id, denied)
        else:
            raise ExtensionIdentityConflict(
                manifest.extension_id, f"unknown origin {manifest.origin!r}"
            )
        if manifest.extension_id in self._by_id:
            raise ExtensionIdentityConflict(
                manifest.extension_id, "an extension with this id is already registered"
            )
        self._by_id[manifest.extension_id] = extension

    def get(self, extension_id: str) -> T:
        try:
            return self._by_id[extension_id]
        except KeyError:
            raise UnknownExtension(self.kind, extension_id, tuple(sorted(self._by_id))) from None

    def __contains__(self, extension_id: str) -> bool:
        return extension_id in self._by_id

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_id))

    def guard(self, extension: T, operation: str, fn: Callable[[], object]) -> object:
        """Run ``fn`` (a call into ``extension``), isolating any raw failure as an
        :class:`ExtensionOperationFailure`. Framework errors (unsupported
        capability, etc.) pass through unchanged."""
        try:
            return fn()
        except ExtensionError:
            raise
        except Exception as exc:
            raise ExtensionOperationFailure(_manifest_of(extension).extension_id, operation, exc) from exc

    def descriptors(self) -> list[dict]:
        """Capability discovery for this kind: each extension's manifest plus a
        health snapshot. A failing ``health()`` is REPORTED (never raised) so
        discovery stays robust."""
        out: list[dict] = []
        for extension_id in sorted(self._by_id):
            extension = self._by_id[extension_id]
            entry = _manifest_of(extension).to_dict()
            health = getattr(extension, "health", None)
            if callable(health):
                try:
                    entry["health"] = health()
                except Exception as exc:
                    entry["health"] = {"ready": False, "error": type(exc).__name__}
            else:
                entry["health"] = None
            out.append(entry)
        return out


def describe_extensions(*registries: ExtensionRegistry) -> list[dict]:
    """Flatten every registry's descriptors into one machine-readable capability
    document across kinds — the answer to "what extensions are loaded, of what
    origin, at what version, healthy, supporting which operations"."""
    out: list[dict] = []
    for registry in registries:
        out.extend(registry.descriptors())
    return sorted(out, key=lambda d: (d["kind"], d["extension_id"]))
