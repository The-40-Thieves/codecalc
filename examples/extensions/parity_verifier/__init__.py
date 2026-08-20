"""Reference THIRD-PARTY verifier (THE-794).

`ParityVerifier` is the second consumer of `codecalc.verifiers.Verifier` —
proof the interface is usable by code outside `codecalc/` itself, not just a
shape guessed at from its own built-in. It speaks to one trivial claim kind,
`parity`: "is integer `n` even or odd?" — and, like every verifier, it
submits evidence, never a grade.

An operator installs this by registering an instance with a
`codecalc.verifiers.VerifierRegistry`:

    from codecalc import verifiers
    from examples.extensions.parity_verifier import ParityVerifier

    registry = verifiers.configured_verifier_registry()
    registry.register(ParityVerifier())
"""

from __future__ import annotations

from codecalc import extensions, verifiers

#: Reuses `verifiers.COMPATIBLE_CODECALC`/`COMPATIBLE_CONTRACT` (computed from
#: the running codecalc/contract versions, THE-872) rather than hand-copying a
#: version range that could then drift from what codecalc actually ships — the
#: same reasoning as `examples/extensions/csv_renderer`. A real third-party
#: author instead pins the range they tested against, the same way a `pip`
#: package pins its own dependency bounds; this reference reuses the built-in's
#: range so the example does not go stale on its own.
_COMPATIBLE_CODECALC = verifiers.COMPATIBLE_CODECALC
_COMPATIBLE_CONTRACT = verifiers.COMPATIBLE_CONTRACT


class ParityVerifier:
    """Third-party verifier: evidence for the `parity` claim kind.

    A claim is ``{"kind": "parity", "n": <int>, "expected": "even" | "odd"}``.
    `n`'s actual parity is computed and compared against `expected`:
    agreement is `supports`, disagreement is `refutes`. A malformed claim
    (no integer `n`, or an `expected` outside {"even", "odd"}) is
    `inconclusive` rather than guessed at.
    """

    manifest = extensions.ExtensionManifest(
        kind=extensions.ExtensionKind.VERIFIER,
        extension_id="example.parity",
        name="Parity",
        version="1.0.0",
        interface_version=verifiers.VERIFIER_INTERFACE_VERSION,
        origin="third_party",
        compatible_codecalc=_COMPATIBLE_CODECALC,
        compatible_contract=_COMPATIBLE_CONTRACT,
        declared_permissions=("verify",),
        supported_operations=("verify",),
    )

    def claims(self) -> list[str]:
        return ["parity"]

    def verify(self, claim: dict, context: dict) -> verifiers.Evidence:
        del context
        n = claim.get("n")
        expected = claim.get("expected")
        if not isinstance(n, int) or isinstance(n, bool) or expected not in {"even", "odd"}:
            return verifiers.Evidence(
                claim_kind="parity",
                verifier_id=self.manifest.extension_id,
                outcome="inconclusive",
                detail="claim needs an integer 'n' and expected in {'even', 'odd'}",
            )
        actual = "even" if n % 2 == 0 else "odd"
        outcome = "supports" if actual == expected else "refutes"
        return verifiers.Evidence(
            claim_kind="parity",
            verifier_id=self.manifest.extension_id,
            outcome=outcome,
            detail=f"{n} is {actual}",
        )

    def health(self) -> dict:
        return {"ready": True}
