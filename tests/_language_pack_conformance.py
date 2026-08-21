"""Reusable language-pack conformance cases for the extension framework."""

from __future__ import annotations

import json
from collections.abc import Callable

from codecalc.language_packs import LanguageEntry, LanguagePack


def run_language_pack_conformance(pack: LanguagePack,
                                  check: Callable[[str, bool], None]) -> None:
    """Run backend-independent requirements against one language-pack instance."""
    manifest = pack.manifest
    check("conformance: manifest is JSON serializable",
          isinstance(json.loads(json.dumps(manifest.to_dict())), dict))

    entries = pack.languages()
    check("conformance: languages() returns a non-empty list", len(entries) > 0)
    check("conformance: every language is a LanguageEntry",
          all(isinstance(entry, LanguageEntry) for entry in entries))

    for entry in entries:
        run_argv = pack.run_plan(entry.language_id)
        check(f"conformance: {entry.language_id} run_plan is a list",
              isinstance(run_argv, list))

        compile_argv = pack.compile_plan(entry.language_id)
        check(f"conformance: {entry.language_id} compile_plan is None or a list",
              compile_argv is None or isinstance(compile_argv, list))

        # {file} names the source. For a compiled language it appears in the
        # COMPILE plan (the source is compiled first); run_plan then executes
        # the compiled artifact ({exe}), not the source, so requiring {file}
        # specifically in run_plan would be false for c/c++/rust/etc. An
        # interpreted language has no compile_plan, so {file} must be in
        # run_plan instead. Either placement is a valid argv template.
        placeholder_argv = compile_argv if compile_argv is not None else run_argv
        check(f"conformance: {entry.language_id} plan contains {{file}}",
              any("{file}" in arg for arg in placeholder_argv))

        diagnostics = pack.normalize_diagnostics(entry.language_id, "boom")
        check(f"conformance: {entry.language_id} normalize_diagnostics returns a dict",
              isinstance(diagnostics, dict))

    health = pack.health()
    check("conformance: health returns a dict", isinstance(health, dict))
    check("conformance: health readiness is explicit",
          isinstance(health.get("ready"), bool))
