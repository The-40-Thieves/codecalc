"""The language-pack extension kind (codecalc/language_packs.py).

Standalone, like the other suites: a check() accumulator, sys.exit(1) on any
failure. Runs the built-in pack and the reference third-party pack
(examples/extensions/lolcode_pack) through the shared conformance suite,
registers both in a LanguagePackRegistry, and exercises discovery/resolution.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from _language_pack_conformance import run_language_pack_conformance

from codecalc.language_packs import BuiltinLanguagePack, LanguagePackRegistry
from examples.extensions.lolcode_pack import LolcodePack

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# ── conformance: built-in ───────────────────────────────────────────────────
builtin = BuiltinLanguagePack()
run_language_pack_conformance(builtin, check)

# ── conformance: reference third-party (lolcode) ────────────────────────────
lolcode = LolcodePack()
run_language_pack_conformance(lolcode, check)

# ── registration + discovery ────────────────────────────────────────────────
registry = LanguagePackRegistry()
registry.register(builtin)
registry.register(lolcode)

catalog = registry.catalog()
language_ids = {row["language_id"] for row in catalog}
check("catalog includes a built-in language", "python3" in language_ids, f"-> {sorted(language_ids)[:5]}")
check("catalog includes the reference third-party language", "lolcode" in language_ids)

py_id, py_pack = registry.resolve("py")
check("resolve() finds the builtin pack via an alias", py_id == "python3" and py_pack is builtin,
      f"-> {py_id!r}")

lol_id, lol_pack = registry.resolve("lol")
check("resolve() finds the third-party pack via an alias", lol_id == "lolcode" and lol_pack is lolcode,
      f"-> {lol_id!r}")


print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== LANGUAGE PACKS OK ===")
sys.exit(1 if FAILS else 0)
