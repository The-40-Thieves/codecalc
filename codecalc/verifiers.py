"""Verifier extension kind (THE-794).

A :class:`Verifier` inspects a claim and submits **evidence**; it never
assigns a grade. ``codecalc/grades.py`` alone maps evidence to a grade — see
its module docstring: "VERIFIERS EMIT EVIDENCE, THIS MODULE ASSIGNS GRADES."
That rule predates this module and this module does not relax it: `Evidence`
below deliberately carries no `grade`/`score` field, and nothing in this
registry ever produces one.

Four safeguards keep a verifier from assigning trust even so (design doc
``docs/design/2026-08-19-extension-sdk.md``, gemini-3.1-pro review):

  1. `Evidence` is a frozen, slotted dataclass — not a mutable dict — so the
     record a grader reads cannot be reshaped after the fact.
  2. `VerifierRegistry.collect_evidence` passes each verifier a
     ``copy.deepcopy`` of `claim` and `context`, so a verifier that mutates
     its arguments in place cannot poison a later verifier's input or the
     caller's own dict.
  3. Declared-claim scoping: evidence whose `claim_kind` is not in the
     verifier's own `claims()` is DROPPED — a verifier cannot speak to a
     claim it never declared.
  4. Advisory weighting: the surviving evidence list is returned as INPUT
     for `grades.py`, never a verdict. A single `supports` does not lift a
     grade on its own, and third-party verifier evidence corroborates — it
     is not sufficient alone to certify a claim.

Every call into a verifier is isolated through the shared
``extensions.ExtensionRegistry.guard`` — a raising verifier is reported as
`extension_verify_failed` (or `extension_claims_failed`) rather than crashing
collection for the rest.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable

from . import __version__, contract, extensions

VERIFIER_INTERFACE_VERSION = "1.0.0"

#: The only outcomes an `Evidence.outcome` may carry. Deliberately narrow —
#: a verifier reports what it observed, not a confidence score.
EVIDENCE_OUTCOMES = frozenset({"supports", "refutes", "inconclusive"})


@dataclass(frozen=True, slots=True)
class Evidence:
    """One verifier's observation about one claim.

    NO grade or score field, on purpose: `slots=True` makes this exhaustive
    rather than merely documented — an attempt to bolt a `grade` onto an
    instance raises `AttributeError` rather than silently succeeding. A
    verifier submits evidence; only `codecalc/grades.py` assigns a grade.
    """

    claim_kind: str
    verifier_id: str
    outcome: str
    detail: str = ""
    cost_ms: int = 0

    def __post_init__(self) -> None:
        if self.outcome not in EVIDENCE_OUTCOMES:
            raise ValueError(
                f"Evidence.outcome {self.outcome!r} is not one of "
                f"{sorted(EVIDENCE_OUTCOMES)}"
            )

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class Verifier(Protocol):
    """The verifier surface: submit evidence for a claim, never a grade."""

    manifest: extensions.ExtensionManifest

    def claims(self) -> list[str]:
        """The claim kinds this verifier may speak to."""
        ...

    def verify(self, claim: dict, context: dict) -> Evidence:
        """Inspect `claim` (given `context`) and return evidence — never a
        grade."""
        ...

    def health(self) -> dict: ...


class VerifierRegistry(extensions.ExtensionRegistry[Verifier]):
    """Register verifiers and collect their evidence for a claim.

    `collect_evidence` is the only aggregation surface this registry offers,
    and it deliberately returns `list[Evidence]`, not a grade: grading stays
    the sole responsibility of `codecalc/grades.py`, which keeps its own
    versioned rules (`GRADE_RULES_VERSION`) for turning evidence into
    `executed` / `cross_checked` / `solver_proven` / `ungraded`. Third-party
    verifier evidence is advisory input to that process — corroborating, not
    certifying — so a lying or colluding third-party verifier cannot
    unilaterally certify a fraudulent claim.
    """

    def __init__(self, *, policy: extensions.ExtensionPolicy | None = None) -> None:
        super().__init__(
            extensions.ExtensionKind.VERIFIER, VERIFIER_INTERFACE_VERSION, policy=policy
        )

    def collect_evidence(self, claim: dict, context: dict) -> list[Evidence]:
        """Run every registered verifier whose `claims()` includes
        `claim["kind"]` and gather the evidence that survives the
        safeguards. See the module docstring for the four safeguards; in
        short: deep-copied inputs, isolated calls, declared-claim scoping,
        and a plain evidence list handed back — never a grade.
        """
        claim_kind = claim.get("kind")
        collected: list[Evidence] = []
        for extension_id in self.ids():
            verifier = self.get(extension_id)
            try:
                declared = self.guard(verifier, "claims", verifier.claims)
            except extensions.ExtensionError:
                continue
            if not isinstance(declared, list) or claim_kind not in declared:
                continue
            # Safeguard (a): each verifier gets its OWN deep copy, so it
            # cannot mutate the caller's dict or poison the next verifier.
            claim_copy = copy.deepcopy(claim)
            context_copy = copy.deepcopy(context)
            try:
                evidence = self.guard(
                    verifier,
                    "verify",
                    lambda v=verifier, c=claim_copy, x=context_copy: v.verify(c, x),
                )
            except extensions.ExtensionError:
                # Safeguard (b): a raising verifier is isolated, not fatal
                # to the collection.
                continue
            if not isinstance(evidence, Evidence):
                continue
            # Safeguard (c): the evidence must be for the SAME claim kind this
            # call is verifying — not merely one the verifier happens to also
            # declare. A multi-claim verifier invoked for "executed" cannot
            # smuggle in evidence tagged "solver_proven" (declared, but not what
            # was asked); such evidence is dropped so it can't pollute the result
            # for the claim actually under verification.
            if evidence.claim_kind != claim_kind:
                continue
            collected.append(evidence)
        return collected


def _codecalc_compat_range() -> str:
    major, minor, _patch = __version__.split(".")
    return f">={major}.{minor},<{major}.{int(minor) + 1}"


def _contract_compat_range() -> str:
    major, minor, _patch = contract.CONTRACT_VERSION.split(".")
    return f">={major}.{minor},<{int(major) + 1}"


class ExecutedVerifier:
    """Built-in: evidence for the `executed` claim from an execution result.

    Wraps the existing "did this actually run" signal already produced by
    `providers.py`/`executor.py` — an execution result dict whose `ok` is
    True supports the claim; anything else is `inconclusive` (never
    `refutes` on its own: an unsuccessful run is not proof the claim is
    false, only that this one attempt did not show it true). This mirrors
    `grades.EXECUTED`'s own description: bare execution evidence, with no
    independent second opinion folded in.
    """

    manifest = extensions.ExtensionManifest(
        kind=extensions.ExtensionKind.VERIFIER,
        extension_id="builtin:executed",
        name="Executed",
        version="1.0.0",
        interface_version=VERIFIER_INTERFACE_VERSION,
        origin="builtin",
        compatible_codecalc=_codecalc_compat_range(),
        compatible_contract=_contract_compat_range(),
        declared_permissions=("verify",),
        supported_operations=("verify",),
    )

    def claims(self) -> list[str]:
        return ["executed"]

    def verify(self, claim: dict, context: dict) -> Evidence:
        del claim
        result = context.get("result") if isinstance(context, dict) else None
        if isinstance(result, dict) and result.get("ok") is True:
            return Evidence(
                claim_kind="executed",
                verifier_id=self.manifest.extension_id,
                outcome="supports",
                detail="execution result reports ok=True",
            )
        return Evidence(
            claim_kind="executed",
            verifier_id=self.manifest.extension_id,
            outcome="inconclusive",
            detail="execution result does not show ok execution",
        )

    def health(self) -> dict:
        return {"ready": True}


def configured_verifier_registry(environ: Mapping[str, str] | None = None) -> VerifierRegistry:
    """Build the verifier registry from process configuration, registering
    the built-in `executed` verifier. Mirrors `providers.configured_registry`."""
    registry = VerifierRegistry(policy=extensions.ExtensionPolicy.from_env(environ))
    registry.register(ExecutedVerifier())
    return registry
