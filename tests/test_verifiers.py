"""The verifier extension kind (codecalc/verifiers.py).

Standalone, like the other suites: a check() accumulator, sys.exit(1) on any
failure. Runs the built-in (`ExecutedVerifier`) and the reference third-party
verifier (`examples/extensions/parity_verifier`) through the shared
conformance suite, registers both in one `VerifierRegistry`, and proves the
design doc's four `collect_evidence` safeguards with adversarial fakes:
deep-copied inputs, isolated calls, declared-claim scoping, and evidence (not
a grade) as the return value.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from _verifier_conformance import run_verifier_conformance

from codecalc import extensions, verifiers
from examples.extensions.parity_verifier import ParityVerifier

FAILS: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name}")
    if not condition:
        FAILS.append(name)


def test_builtin_and_reference_verifiers_pass_conformance() -> None:
    run_verifier_conformance(
        verifiers.ExecutedVerifier(), check,
        claim={"kind": "executed"}, context={"result": {"ok": True}},
    )
    run_verifier_conformance(
        ParityVerifier(), check,
        claim={"kind": "parity", "n": 4, "expected": "even"}, context={},
    )


def test_registry_collects_evidence_only_from_verifiers_that_declared_the_claim() -> None:
    registry = verifiers.VerifierRegistry()
    registry.register(verifiers.ExecutedVerifier())
    registry.register(ParityVerifier())

    executed_evidence = registry.collect_evidence(
        {"kind": "executed"}, {"result": {"ok": True}}
    )
    check("collect_evidence runs exactly the verifier that declared 'executed'",
          len(executed_evidence) == 1 and executed_evidence[0].verifier_id == "builtin:executed")
    check("the executed evidence supports the claim",
          executed_evidence[0].outcome == "supports")

    parity_evidence = registry.collect_evidence(
        {"kind": "parity", "n": 4, "expected": "even"}, {}
    )
    check("collect_evidence runs exactly the verifier that declared 'parity'",
          len(parity_evidence) == 1 and parity_evidence[0].verifier_id == "example.parity")
    check("the parity evidence supports a correct claim",
          parity_evidence[0].outcome == "supports")

    refuted = registry.collect_evidence({"kind": "parity", "n": 4, "expected": "odd"}, {})
    check("collect_evidence surfaces a refuting verdict honestly",
          len(refuted) == 1 and refuted[0].outcome == "refutes")

    unclaimed = registry.collect_evidence({"kind": "no_such_claim_kind"}, {})
    check("collect_evidence runs NO verifier for an undeclared claim kind",
          unclaimed == [])

    check("collect_evidence never returns anything but Evidence",
          all(isinstance(item, verifiers.Evidence)
              for item in executed_evidence + parity_evidence))


def _manifest(extension_id: str, claim_kind: str) -> extensions.ExtensionManifest:
    return extensions.ExtensionManifest(
        kind=extensions.ExtensionKind.VERIFIER,
        extension_id=extension_id,
        name=extension_id,
        version="1.0.0",
        interface_version=verifiers.VERIFIER_INTERFACE_VERSION,
        origin="third_party",
        compatible_codecalc=">=0.2,<0.3",
        compatible_contract=">=1.2,<2",
        declared_permissions=("verify",),
        supported_operations=("verify",),
    )


class _MutatingVerifier:
    """Adversary (i): mutates its OWN `claim`/`context` arguments in place.

    If `collect_evidence` handed it the caller's real objects (no deepcopy),
    this mutation would be visible to the caller and to any later verifier
    called with the same claim/context.
    """

    manifest = _manifest("adv.mutator", "mutate")

    def claims(self) -> list[str]:
        return ["mutate"]

    def verify(self, claim: dict, context: dict) -> verifiers.Evidence:
        claim["poisoned"] = True
        context["poisoned"] = True
        return verifiers.Evidence(claim_kind="mutate", verifier_id="adv.mutator", outcome="supports")

    def health(self) -> dict:
        return {"ready": True}


class _ObservingVerifier:
    """Records exactly what claim/context it was called with, so the test can
    check a SECOND verifier never sees the first verifier's mutation."""

    manifest = _manifest("adv.observer", "mutate")

    def __init__(self) -> None:
        self.seen_claim: dict | None = None
        self.seen_context: dict | None = None

    def claims(self) -> list[str]:
        return ["mutate"]

    def verify(self, claim: dict, context: dict) -> verifiers.Evidence:
        self.seen_claim = dict(claim)
        self.seen_context = dict(context)
        return verifiers.Evidence(claim_kind="mutate", verifier_id="adv.observer", outcome="inconclusive")

    def health(self) -> dict:
        return {"ready": True}


def test_collect_evidence_deep_copies_so_a_mutating_verifier_cannot_poison_anything() -> None:
    registry = verifiers.VerifierRegistry()
    registry.register(_MutatingVerifier())
    observer = _ObservingVerifier()
    registry.register(observer)

    original_claim = {"kind": "mutate", "note": "original"}
    original_context = {"note": "original"}

    registry.collect_evidence(original_claim, original_context)

    check("the caller's own claim dict is untouched by a mutating verifier",
          original_claim == {"kind": "mutate", "note": "original"})
    check("the caller's own context dict is untouched by a mutating verifier",
          original_context == {"note": "original"})
    check("a second verifier never sees the first verifier's mutation (claim)",
          observer.seen_claim is not None and "poisoned" not in observer.seen_claim)
    check("a second verifier never sees the first verifier's mutation (context)",
          observer.seen_context is not None and "poisoned" not in observer.seen_context)


class _UndeclaredClaimVerifier:
    """Adversary (ii): declares one claim kind but returns evidence for a
    DIFFERENT one it never declared. `collect_evidence` must drop it."""

    manifest = _manifest("adv.oversteps", "declared_kind")

    def claims(self) -> list[str]:
        return ["declared_kind"]

    def verify(self, claim: dict, context: dict) -> verifiers.Evidence:
        del claim, context
        return verifiers.Evidence(
            claim_kind="undeclared_kind", verifier_id="adv.oversteps", outcome="supports"
        )

    def health(self) -> dict:
        return {"ready": True}


def test_evidence_for_an_undeclared_claim_kind_is_dropped() -> None:
    registry = verifiers.VerifierRegistry()
    registry.register(_UndeclaredClaimVerifier())

    evidence = registry.collect_evidence({"kind": "declared_kind"}, {})

    check("evidence naming a claim_kind the verifier never declared is dropped",
          evidence == [])


class _CrossClaimVerifier:
    """Adversary (ii-b): declares MULTIPLE claim kinds and, when invoked for one,
    returns evidence tagged for ANOTHER it also declared. The evidence's kind is
    in the verifier's own declared set, so a "in declared" check would let it
    through — but it is not the claim under verification, and must be dropped."""

    manifest = _manifest("adv.crossclaim", "executed")

    def claims(self) -> list[str]:
        return ["executed", "solver_proven"]

    def verify(self, claim: dict, context: dict) -> verifiers.Evidence:
        del claim, context
        return verifiers.Evidence(
            claim_kind="solver_proven", verifier_id="adv.crossclaim", outcome="supports"
        )

    def health(self) -> dict:
        return {"ready": True}


def test_evidence_for_a_different_declared_claim_is_dropped() -> None:
    registry = verifiers.VerifierRegistry()
    registry.register(_CrossClaimVerifier())
    # Invoked for "executed"; the verifier returns "solver_proven" evidence,
    # which it DOES declare — but that is not the claim under verification.
    evidence = registry.collect_evidence({"kind": "executed"}, {})
    check("a multi-claim verifier cannot smuggle evidence for a DIFFERENT declared claim",
          evidence == [])


class _RaisingVerifier:
    """Adversary (iii): raises inside `verify`. `collect_evidence` must
    isolate the failure rather than crash the whole collection."""

    manifest = _manifest("adv.raises", "raise_kind")

    def claims(self) -> list[str]:
        return ["raise_kind"]

    def verify(self, claim: dict, context: dict) -> verifiers.Evidence:
        del claim, context
        raise RuntimeError("kaboom")

    def health(self) -> dict:
        return {"ready": True}


def test_a_raising_verifier_is_isolated_not_fatal_to_collection() -> None:
    registry = verifiers.VerifierRegistry()
    registry.register(_RaisingVerifier())
    registry.register(ParityVerifier())

    evidence = registry.collect_evidence(
        {"kind": "parity", "n": 3, "expected": "odd"}, {}
    )

    check("collect_evidence survives a raising verifier and keeps going",
          len(evidence) == 1 and evidence[0].verifier_id == "example.parity")

    raiser = registry.get("adv.raises")
    try:
        registry.guard(raiser, "verify", lambda: raiser.verify({}, {}))
    except extensions.ExtensionOperationFailure as exc:
        check("the isolated failure carries the stable extension_verify_failed code",
              exc.code == "extension_verify_failed")
    else:
        check("a raising verify() cannot silently succeed through guard", False)


def test_evidence_rejects_an_outcome_outside_the_allowed_vocabulary() -> None:
    try:
        verifiers.Evidence(claim_kind="x", verifier_id="x", outcome="definitely-maybe")
    except ValueError as exc:
        check("an invalid outcome is refused with a ValueError", "definitely-maybe" in str(exc))
    else:
        check("Evidence cannot be constructed with an invalid outcome", False)


def test_configured_verifier_registry_registers_the_builtin() -> None:
    registry = verifiers.configured_verifier_registry({})
    check("configured_verifier_registry registers builtin:executed",
          "builtin:executed" in registry)
    evidence = registry.collect_evidence({"kind": "executed"}, {"result": {"ok": True}})
    check("the configured registry's built-in actually verifies",
          len(evidence) == 1 and evidence[0].outcome == "supports")


if __name__ == "__main__":
    test_builtin_and_reference_verifiers_pass_conformance()
    test_registry_collects_evidence_only_from_verifiers_that_declared_the_claim()
    test_collect_evidence_deep_copies_so_a_mutating_verifier_cannot_poison_anything()
    test_evidence_for_an_undeclared_claim_kind_is_dropped()
    test_evidence_for_a_different_declared_claim_is_dropped()
    test_a_raising_verifier_is_isolated_not_fatal_to_collection()
    test_evidence_rejects_an_outcome_outside_the_allowed_vocabulary()
    test_configured_verifier_registry_registers_the_builtin()
    print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== VERIFIERS OK ===")
    sys.exit(1 if FAILS else 0)
