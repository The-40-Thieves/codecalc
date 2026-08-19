"""Reference third-party language pack (THE-794).

Proves the third-party registration + discovery path for
``codecalc.language_packs``: one toy language, LOLCODE, registered under a
non-``builtin:`` id with a declared ``execute`` permission. It does not need
to actually run — ``run_plan``/``compile_plan`` return a plausible argv
template so the conformance suite (``tests/_language_pack_conformance.py``)
can assert its shape the same way it does for the built-in pack.
"""

from __future__ import annotations

from codecalc import extensions
from codecalc.language_packs import LANGUAGE_PACK_INTERFACE_VERSION, LanguageEntry

LOLCODE_LANGUAGE_ID = "lolcode"


class LolcodePack:
    """A single-language example third-party ``LanguagePack``."""

    def __init__(self) -> None:
        self.manifest = extensions.ExtensionManifest(
            kind=extensions.ExtensionKind.LANGUAGE_PACK,
            extension_id="example.lolcode",
            name="LOLCODE example pack",
            version="1.0.0",
            interface_version=LANGUAGE_PACK_INTERFACE_VERSION,
            origin="third_party",
            compatible_codecalc=">=0.2,<0.3",
            compatible_contract=">=1.2,<2",
            declared_permissions=("execute",),
            supported_operations=("run_plan", "normalize_diagnostics"),
        )

    def languages(self) -> list[LanguageEntry]:
        return [
            LanguageEntry(
                language_id=LOLCODE_LANGUAGE_ID,
                aliases=("lol",),
                file_extension="lol",
                compiled=False,
            )
        ]

    def run_plan(self, language: str) -> list[str]:
        del language
        return ["lci", "{file}"]

    def compile_plan(self, language: str) -> list[str] | None:
        del language
        return None

    def normalize_diagnostics(self, language: str, stderr: str) -> dict:
        return {"raw": stderr, "language": language}

    def health(self) -> dict:
        return {"ready": True, "extension_id": self.manifest.extension_id}
