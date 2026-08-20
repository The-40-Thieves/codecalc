"""Renderer extension kind (THE-794): result envelope -> representation.

Mirrors `providers.py`'s shape (a `Protocol`, a frozen result record, a
registry that subclasses the shared `extensions.ExtensionRegistry`) per
`docs/design/2026-08-19-extension-sdk.md`. A `Renderer` turns a codecalc
result envelope into a representation — text, a markdown table, a chart spec,
whatever it declares — and never executes payload code and never mutates the
envelope it is handed: rendering is read-only by contract, enforced here by
the built-ins operating on copies and asserted by the conformance suite in
`tests/_renderer_conformance.py`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable

from . import __version__, contract, extensions

RENDERER_INTERFACE_VERSION = "1.0.0"

#: This renderer kind's declared compatibility, recorded on both built-ins and
#: reused by the reference third-party extension so the example does not drift
#: from what codecalc actually ships. Matches the pattern test_extensions.py
#: uses for its own fixture manifests: MAJOR.MINOR of the running codecalc and
#: of the result contract, open-ended on the next minor/major. Public (no
#: leading underscore) so `examples/extensions/csv_renderer` can reuse it
#: rather than hand-copying a version range that could then drift from this
#: one. Computed via `extensions.codecalc_compat_range`/`contract_compat_range`
#: (THE-872) rather than hardcoded — a hardcoded upper bound goes stale the
#: moment the running MINOR reaches it.
COMPATIBLE_CODECALC = extensions.codecalc_compat_range(__version__)
COMPATIBLE_CONTRACT = extensions.contract_compat_range(contract.CONTRACT_VERSION)


@dataclass(frozen=True, slots=True)
class RenderOutput:
    """A renderer's typed return — not an opaque dict, so a caller reading it
    does not have to guess which keys a given renderer decided to populate."""

    format: str
    media_type: str
    body: str

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class Renderer(Protocol):
    """The renderer surface every implementation (built-in or third-party)
    provides."""

    manifest: extensions.ExtensionManifest

    def formats(self) -> list[str]: ...

    def can_render(self, result: dict) -> bool: ...

    def render(self, result: dict, fmt: str) -> RenderOutput: ...

    def health(self) -> dict: ...


class RendererRegistry(extensions.ExtensionRegistry[Renderer]):
    """Renderer discovery and a guarded render() convenience."""

    def __init__(self, *, policy: extensions.ExtensionPolicy | None = None) -> None:
        super().__init__(extensions.ExtensionKind.RENDERER, RENDERER_INTERFACE_VERSION, policy=policy)

    def render(self, renderer_id: str, result: dict, fmt: str) -> RenderOutput:
        """Look up `renderer_id`, confirm it advertises `fmt`, and call it
        through `guard()` so a raising renderer surfaces as
        `extension_render_failed` rather than crashing the caller."""
        renderer = self.get(renderer_id)
        if fmt not in renderer.formats():
            raise extensions.UnsupportedCapability(renderer_id, fmt)
        return self.guard(renderer, "render", lambda: renderer.render(result, fmt))


class TextRenderer:
    """Built-in: the plain-text verdict + stdout/stderr view."""

    manifest = extensions.ExtensionManifest(
        kind=extensions.ExtensionKind.RENDERER,
        extension_id="builtin:text",
        name="Text",
        version="1.0.0",
        interface_version=RENDERER_INTERFACE_VERSION,
        origin="builtin",
        compatible_codecalc=COMPATIBLE_CODECALC,
        compatible_contract=COMPATIBLE_CONTRACT,
        declared_permissions=("render",),
        supported_operations=("text",),
    )

    def formats(self) -> list[str]:
        return ["text"]

    def can_render(self, result: dict) -> bool:
        return isinstance(result, dict) and "verdict" in result

    def render(self, result: dict, fmt: str) -> RenderOutput:
        if fmt not in self.formats():
            raise extensions.UnsupportedCapability(self.manifest.extension_id, fmt)
        verdict = result.get("verdict", "?")
        exit_code = result.get("exit_code")
        lines = [
            f"verdict: {verdict}",
            f"exit_code: {exit_code}",
            "stdout:",
            str(result.get("stdout", "")),
        ]
        stderr = result.get("stderr", "")
        if stderr:
            lines.append("stderr:")
            lines.append(str(stderr))
        return RenderOutput(format="text", media_type="text/plain", body="\n".join(lines))

    def health(self) -> dict:
        return {"ready": True}


#: The envelope fields a markdown table renders, in column order. Kept short
#: and explicit rather than dumping every key: a table with two dozen columns
#: is not more readable than one with the fields a reader actually scans for.
_MARKDOWN_TABLE_FIELDS = (
    "ok", "language", "verdict", "exit_code", "timed_out", "duration_ms",
)


class MarkdownTableRenderer:
    """Built-in: the envelope's key fields as a GitHub-flavored markdown table."""

    manifest = extensions.ExtensionManifest(
        kind=extensions.ExtensionKind.RENDERER,
        extension_id="builtin:markdown-table",
        name="Markdown Table",
        version="1.0.0",
        interface_version=RENDERER_INTERFACE_VERSION,
        origin="builtin",
        compatible_codecalc=COMPATIBLE_CODECALC,
        compatible_contract=COMPATIBLE_CONTRACT,
        declared_permissions=("render",),
        supported_operations=("markdown",),
    )

    def formats(self) -> list[str]:
        return ["markdown"]

    def can_render(self, result: dict) -> bool:
        return isinstance(result, dict) and "verdict" in result

    def render(self, result: dict, fmt: str) -> RenderOutput:
        if fmt not in self.formats():
            raise extensions.UnsupportedCapability(self.manifest.extension_id, fmt)
        header = "| " + " | ".join(_MARKDOWN_TABLE_FIELDS) + " |"
        divider = "| " + " | ".join("---" for _ in _MARKDOWN_TABLE_FIELDS) + " |"
        row = "| " + " | ".join(str(result.get(field, "")) for field in _MARKDOWN_TABLE_FIELDS) + " |"
        body = "\n".join((header, divider, row))
        return RenderOutput(format="markdown", media_type="text/markdown", body=body)

    def health(self) -> dict:
        return {"ready": True}


def configured_renderer_registry(environ: Mapping[str, str] | None = None) -> RendererRegistry:
    """Build the renderer registry from process configuration, with both
    built-ins registered — the same shape as `providers.configured_registry`."""
    env = os.environ if environ is None else environ
    registry = RendererRegistry(policy=extensions.ExtensionPolicy.from_env(env))
    registry.register(TextRenderer())
    registry.register(MarkdownTableRenderer())
    return registry
