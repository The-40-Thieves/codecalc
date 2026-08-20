"""Reference third-party renderer (THE-794): proves the non-builtin path.

`CsvRenderer` is not wired into codecalc's own registration; it exists so the
renderer interface has a second, independently-authored implementation to
validate the shape against, and so `tests/test_renderers.py` can prove a
`third_party`-origin extension registers and renders through the same
`RendererRegistry` a built-in does.
"""

from __future__ import annotations

import csv
import io

from codecalc import extensions, renderers

#: The fields a CSV row carries, in column order — the same envelope fields
#: `renderers.MarkdownTableRenderer` picks, so the two built-in/third-party
#: examples answer the same question in different formats.
_CSV_FIELDS = ("ok", "language", "verdict", "exit_code", "timed_out", "duration_ms")


class CsvRenderer:
    """Third-party example: the envelope's key fields as CSV."""

    manifest = extensions.ExtensionManifest(
        kind=extensions.ExtensionKind.RENDERER,
        extension_id="example.csv-renderer",
        name="CSV (example)",
        version="1.0.0",
        interface_version=renderers.RENDERER_INTERFACE_VERSION,
        origin="third_party",
        compatible_codecalc=renderers.COMPATIBLE_CODECALC,
        compatible_contract=renderers.COMPATIBLE_CONTRACT,
        declared_permissions=("render",),
        supported_operations=("csv",),
    )

    def formats(self) -> list[str]:
        return ["csv"]

    def can_render(self, result: dict) -> bool:
        return isinstance(result, dict) and "verdict" in result

    def render(self, result: dict, fmt: str) -> renderers.RenderOutput:
        if fmt not in self.formats():
            raise extensions.UnsupportedCapability(self.manifest.extension_id, fmt)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(_CSV_FIELDS)
        writer.writerow([result.get(field, "") for field in _CSV_FIELDS])
        return renderers.RenderOutput(format="csv", media_type="text/csv", body=buffer.getvalue())

    def health(self) -> dict:
        return {"ready": True}
