"""Reusable execution-provider conformance cases for THE-790."""

from __future__ import annotations

import json
from collections.abc import Callable

from codecalc import providers


def run_execution_conformance(provider: providers.ExecutionProvider,
                              check: Callable[[str, bool], None]) -> None:
    """Run backend-independent requirements against one provider instance."""
    descriptor = provider.describe()
    check("conformance: descriptor is JSON serializable",
          isinstance(json.loads(json.dumps(descriptor)), dict))

    health = provider.health()
    check("conformance: health is JSON serializable",
          isinstance(json.loads(json.dumps(health)), dict))
    check("conformance: health identifies the provider",
          health.get("provider_id") == descriptor["provider_id"])
    check("conformance: readiness is explicit",
          isinstance(health.get("ready"), bool))

    runtimes = provider.list_runtimes()
    check("conformance: runtime discovery is JSON serializable",
          isinstance(json.loads(json.dumps(runtimes)), list))

    success = provider.execute(providers.ComputationSpec(
        language="python3", code='print("conformance")'
    ))
    check("conformance: successful execution", success.get("ok") is True)
    check("conformance: successful stdout",
          success.get("stdout", "").strip() == "conformance")

    timeout = provider.execute(providers.ComputationSpec(
        language="python3", code="import time; time.sleep(5)", timeout=1
    ))
    check("conformance: timeout is classified", timeout.get("verdict") == "TLE")

    output = provider.execute(providers.ComputationSpec(
        language="python3", code='print("x" * 200000)', max_output_kb=1
    ))
    check("conformance: output limit is classified", output.get("verdict") == "OLE")

    rejected = provider.execute(providers.ComputationSpec(
        language="definitely-missing", code="pass"
    ))
    check("conformance: invalid runtime is an error", rejected.get("ok") is False)

    optional_operations = (
        ("inspect", "inspect"),
        ("stream", "stream"),
        ("cancel", "cancel"),
        ("cleanup", "cleanup"),
        ("artifacts", "collect_artifacts"),
        ("files", "list_files"),
        ("sessions", "create_session"),
    )
    for capability, method in optional_operations:
        try:
            getattr(provider, method)("conformance-run")
        except providers.UnsupportedCapability as exc:
            check(f"conformance: unsupported {capability} is explicit",
                  exc.capability == capability)
        else:
            check(f"conformance: unsupported {capability} cannot silently succeed",
                  descriptor["capabilities"][capability] is True)
