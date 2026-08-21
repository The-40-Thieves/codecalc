"""Language-pack extension kind.

A :class:`LanguagePack` describes how a set of languages is discovered,
compiled, invoked, and how their diagnostics normalize — mirroring the shape
already shipped for execution providers (``codecalc/providers.py``) and built
on the shared framework in ``codecalc/extensions.py``.

Built-in: :class:`BuiltinLanguagePack`, wrapping the existing
``registry.LANGUAGES/ALIASES/EXTENSIONS`` — the second consumer that validates
the shape lives in ``examples/extensions/lolcode_pack/``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable

from . import __version__, contract, extensions, registry

LANGUAGE_PACK_INTERFACE_VERSION = "1.0.0"

#: This kind's declared compatibility, computed from the running codecalc and
#: contract versions — see `codecalc.extensions.codecalc_compat_range`
#: for why this must be computed rather than hardcoded.
COMPATIBLE_CODECALC = extensions.codecalc_compat_range(__version__)
COMPATIBLE_CONTRACT = extensions.contract_compat_range(contract.CONTRACT_VERSION)


@dataclass(frozen=True, slots=True)
class LanguageEntry:
    """One language a pack advertises for discovery."""

    language_id: str
    aliases: tuple[str, ...]
    file_extension: str
    compiled: bool

    def to_dict(self) -> dict:
        data = asdict(self)
        data["aliases"] = list(self.aliases)
        return data


@runtime_checkable
class LanguagePack(Protocol):
    """The language-pack surface a registry calls into."""

    manifest: extensions.ExtensionManifest

    def languages(self) -> list[LanguageEntry]: ...

    def run_plan(self, language: str) -> list[str]: ...

    def compile_plan(self, language: str) -> list[str] | None: ...

    def normalize_diagnostics(self, language: str, stderr: str) -> dict: ...

    def health(self) -> dict: ...


class LanguagePackRegistry(extensions.ExtensionRegistry[LanguagePack]):
    """Discovery and resolution across every registered language pack."""

    def __init__(self, *, policy: extensions.ExtensionPolicy | None = None) -> None:
        super().__init__(
            extensions.ExtensionKind.LANGUAGE_PACK,
            LANGUAGE_PACK_INTERFACE_VERSION,
            policy=policy,
        )

    def resolve(self, language_or_alias: str) -> tuple[str, LanguagePack]:
        """Find the pack that provides ``language_or_alias``.

        Returns ``(language_id, pack)`` for the FIRST registered pack (in id
        order) whose ``languages()`` declares the name or one of its aliases.
        Raises :class:`extensions.UnknownExtension` — the same shape a caller
        already handles for an unknown pack id — naming every language every
        pack advertises as the available set.
        """
        needle = language_or_alias.strip().lower()
        available: list[str] = []
        for extension_id in self.ids():
            pack = self.get(extension_id)
            for entry in self.guard(pack, "languages", pack.languages):
                available.append(entry.language_id)
                if entry.language_id == needle or needle in entry.aliases:
                    return entry.language_id, pack
        raise extensions.UnknownExtension(self.kind, language_or_alias, tuple(sorted(set(available))))

    def catalog(self) -> list[dict]:
        """Every registered pack's languages, flattened for discovery."""
        out: list[dict] = []
        for extension_id in self.ids():
            pack = self.get(extension_id)
            for entry in self.guard(pack, "languages", pack.languages):
                row = entry.to_dict()
                row["extension_id"] = extension_id
                out.append(row)
        return sorted(out, key=lambda row: (row["language_id"], row["extension_id"]))


class BuiltinLanguagePack:
    """Wraps ``registry.LANGUAGES/ALIASES/EXTENSIONS`` as a language pack."""

    def __init__(self) -> None:
        self.manifest = extensions.ExtensionManifest(
            kind=extensions.ExtensionKind.LANGUAGE_PACK,
            extension_id="builtin:core",
            name="CodeCalc core languages",
            version=__version__,
            interface_version=LANGUAGE_PACK_INTERFACE_VERSION,
            origin="builtin",
            compatible_codecalc=COMPATIBLE_CODECALC,
            compatible_contract=COMPATIBLE_CONTRACT,
            declared_permissions=("execute",),
            supported_operations=("run_plan", "compile_plan", "normalize_diagnostics"),
        )

    def languages(self) -> list[LanguageEntry]:
        aliases_by_canonical = registry.ALIASES
        out = []
        for name, entry in sorted(registry.LANGUAGES.items()):
            out.append(LanguageEntry(
                language_id=name,
                aliases=tuple(aliases_by_canonical.get(name, ())),
                file_extension=registry.EXTENSIONS[name],
                compiled=entry["compile"] is not None,
            ))
        return out

    def _entry(self, language: str) -> dict:
        canon = registry.canonical(language)
        if canon is None:
            raise extensions.UnsupportedCapability(self.manifest.extension_id, "run_plan")
        return registry.LANGUAGES[canon]

    def run_plan(self, language: str) -> list[str]:
        return list(self._entry(language)["run"])

    def compile_plan(self, language: str) -> list[str] | None:
        compile_argv = self._entry(language)["compile"]
        return list(compile_argv) if compile_argv is not None else None

    def normalize_diagnostics(self, language: str, stderr: str) -> dict:
        return {"raw": stderr, "language": language}

    def health(self) -> dict:
        return {"ready": True, "extension_id": self.manifest.extension_id,
                "language_count": len(registry.LANGUAGES)}


def configured_language_pack_registry(
    environ: Mapping[str, str] | None = None,
) -> LanguagePackRegistry:
    """Build the language-pack registry from process configuration."""
    registry_ = LanguagePackRegistry(policy=extensions.ExtensionPolicy.from_env(environ))
    registry_.register(BuiltinLanguagePack())
    return registry_
