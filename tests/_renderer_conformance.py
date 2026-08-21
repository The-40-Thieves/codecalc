"""Reusable renderer conformance cases.

Mirrors `_provider_conformance.py`'s shape: one function, `run_renderer_
conformance(renderer, check)`, exercised against every built-in and every
reference third-party renderer by `tests/test_renderers.py`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable

from codecalc import extensions

#: A minimal but representative execution-envelope-shaped result, per
#: `codecalc/contract.py`'s `execution_envelope` shape — enough for a
#: renderer's `can_render`/`render` to have real fields to read.
SAMPLE_RESULT: dict = {
    "ok": True,
    "language": "python3",
    "phase": "run",
    "backend": "rust",
    "platform": "linux",
    "stdout": "conformance\n",
    "stderr": "",
    "exit_code": 0,
    "timed_out": False,
    "verdict": "OK",
    "output_truncated": False,
    "output_error": None,
    "duration_ms": 12,
    "compile_ms": 0,
    "total_ms": 12,
    "cpu_ms": 3,
    "peak_memory_kb": 4096,
    "unenforced": [],
    "workdir": "workspace:ephemeral",
    "stdout_bytes": 12,
    "stderr_bytes": 0,
    "contract_version": "1.2.0",
}


def run_renderer_conformance(renderer: object, check: Callable[[str, bool, str], None]) -> None:
    """Run kind-independent requirements against one renderer instance."""
    manifest = renderer.manifest
    check("conformance: manifest is JSON serializable",
          isinstance(json.loads(json.dumps(manifest.to_dict())), dict), "")

    formats = renderer.formats()
    check("conformance: formats() is a non-empty list",
          isinstance(formats, list) and len(formats) > 0, f"-> {formats!r}")

    can = renderer.can_render(SAMPLE_RESULT)
    check("conformance: can_render returns bool", isinstance(can, bool), f"-> {can!r}")

    fmt = formats[0]
    before = copy.deepcopy(SAMPLE_RESULT)
    output = renderer.render(SAMPLE_RESULT, fmt)
    check("conformance: render returns a RenderOutput",
          hasattr(output, "format") and hasattr(output, "media_type") and hasattr(output, "body"),
          f"-> {type(output)!r}")
    check("conformance: RenderOutput.format matches the requested format",
          output.format == fmt, f"-> {output.format!r} != {fmt!r}")
    check("conformance: RenderOutput.body is a str",
          isinstance(output.body, str), f"-> {type(output.body)!r}")
    check("conformance: render did not mutate its input result",
          before == SAMPLE_RESULT, f"-> {SAMPLE_RESULT!r} != {before!r}")

    bogus_format = "definitely-not-a-supported-format"
    try:
        renderer.render(SAMPLE_RESULT, bogus_format)
    except extensions.UnsupportedCapability as exc:
        check("conformance: an unsupported format raises UnsupportedCapability",
              exc.capability == bogus_format, f"-> {exc.capability!r}")
    else:
        check("conformance: an unsupported format cannot silently succeed", False, "")

    health = renderer.health()
    check("conformance: health is JSON serializable",
          isinstance(json.loads(json.dumps(health)), dict), "")
