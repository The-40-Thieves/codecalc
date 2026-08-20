"""Reusable verifier conformance cases for THE-794.

Standalone, like `_provider_conformance.py`: `run_verifier_conformance(impl,
check)` runs backend-independent requirements against one verifier instance,
using whatever `check(name, condition)` callback the caller's own suite
already prints/accumulates with.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from codecalc import verifiers


def run_verifier_conformance(
    verifier: verifiers.Verifier,
    check: Callable[[str, bool], None],
    *,
    claim: dict | None = None,
    context: dict | None = None,
) -> None:
    """Run every acceptance rule a `Verifier` implementation must satisfy.

    `claim`/`context` let a caller supply a claim its own verifier actually
    understands (verifiers do not share a claim shape the way providers
    share `ComputationSpec`); when omitted, a minimal claim naming the
    verifier's own first declared claim kind is used.
    """
    manifest = verifier.manifest
    check("conformance: manifest is JSON serializable",
          isinstance(json.loads(json.dumps(manifest.to_dict())), dict))
    check("conformance: manifest kind is VERIFIER",
          manifest.kind.value == "verifier")

    declared = verifier.claims()
    check("conformance: claims() returns a list", isinstance(declared, list))
    check("conformance: claims() is non-empty", bool(declared))

    effective_claim = claim if claim is not None else {"kind": (declared or [""])[0]}
    effective_context = {} if context is None else context

    evidence = verifier.verify(effective_claim, effective_context)
    check("conformance: verify() returns an Evidence",
          isinstance(evidence, verifiers.Evidence))
    if isinstance(evidence, verifiers.Evidence):
        check("conformance: outcome is one of the three allowed values",
              evidence.outcome in verifiers.EVIDENCE_OUTCOMES)
        check("conformance: evidence claim_kind is one this verifier declared",
              evidence.claim_kind in declared)
        # A verifier submits evidence, never a grade — `Evidence` is
        # slotted specifically so this is enforceable, not just documented.
        check("conformance: Evidence carries no grade attribute",
              not hasattr(evidence, "grade"))
        check("conformance: Evidence carries no score attribute",
              not hasattr(evidence, "score"))
        check("conformance: evidence is JSON serializable",
              isinstance(json.loads(json.dumps(evidence.to_dict())), dict))

    health = verifier.health()
    check("conformance: health is JSON serializable",
          isinstance(json.loads(json.dumps(health)), dict))
    check("conformance: health reports readiness explicitly",
          isinstance(health.get("ready"), bool))
