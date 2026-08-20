# lolcode_pack — reference third-party language pack

A minimal example of `codecalc.language_packs.LanguagePack` implemented
*outside* codecalc, the second consumer that validates the interface shape
defined in `docs/design/2026-08-19-extension-sdk.md`.

It adds one toy language entry, `lolcode` (alias `lol`), and proves:

- **Third-party identity.** `extension_id="example.lolcode"` — no `builtin:`
  prefix, `origin="third_party"`.
- **Declared permissions.** `declared_permissions=("execute",)`, checked
  against the operator's `ExtensionPolicy` at registration.
- **Discovery.** Registering `LolcodePack()` in a
  `codecalc.language_packs.LanguagePackRegistry` makes `lolcode` show up in
  `registry.catalog()` and resolvable via `registry.resolve("lol")`.

It does not need to actually execute LOLCODE programs — `run_plan` returns a
plausible argv template (`["lci", "{file}"]`) so the shape can be exercised by
`tests/_language_pack_conformance.py` without a LOLCODE interpreter installed.

## Usage

```python
from codecalc.language_packs import LanguagePackRegistry
from examples.extensions.lolcode_pack import LolcodePack

registry = LanguagePackRegistry()
registry.register(LolcodePack())
registry.resolve("lol")  # -> ("lolcode", <LolcodePack>)
```
