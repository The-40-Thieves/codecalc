# example.csv-renderer

A reference **third-party** renderer for codecalc's renderer extension kind.
It is not registered by codecalc itself — it exists to prove the
third-party path: an `origin="third_party"` extension with a non-`builtin:`
id registers into a `codecalc.renderers.RendererRegistry` and renders through
it exactly like the built-in `TextRenderer` / `MarkdownTableRenderer` do.

`CsvRenderer` declares one format, `csv`, and turns a result envelope's key
fields (`ok`, `language`, `verdict`, `exit_code`, `timed_out`, `duration_ms`)
into a two-row CSV document (header + values).

## Registering it

```python
from codecalc.renderers import RendererRegistry
from examples.extensions.csv_renderer import CsvRenderer

registry = RendererRegistry()
registry.register(CsvRenderer())

output = registry.render("example.csv-renderer", result, "csv")
print(output.body)
```

See `codecalc/renderers.py` for the `Renderer` protocol and
`docs/design/2026-08-19-extension-sdk.md` for the extension trust model this
example is written against — a third-party extension is trusted-by-
installation, the same posture as a `pip` dependency.
