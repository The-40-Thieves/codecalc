# `parity_verifier` — reference third-party verifier

A minimal, third-party implementation of `codecalc.verifiers.Verifier`
('s verifier extension kind). It exists to prove the interface is
usable by code outside `codecalc/` itself — the "second consumer" the
extension SDK design requires alongside every built-in.

## What it verifies

One claim kind, `parity`:

```python
claim = {"kind": "parity", "n": 4, "expected": "even"}
```

`ParityVerifier.verify` computes `n`'s actual parity and compares it against
`expected`:

- agreement → `Evidence(outcome="supports", ...)`
- disagreement → `Evidence(outcome="refutes", ...)`
- a malformed claim (no integer `n`, or `expected` outside
  `{"even", "odd"}`) → `Evidence(outcome="inconclusive", ...)`

Like every verifier, it **submits evidence and never assigns a grade** —
`Evidence` has no `grade`/`score` field, and only `codecalc/grades.py` maps
evidence to a grade.

## Registering it

```python
from codecalc import verifiers
from examples.extensions.parity_verifier import ParityVerifier

registry = verifiers.configured_verifier_registry()
registry.register(ParityVerifier())

evidence = registry.collect_evidence(
    {"kind": "parity", "n": 4, "expected": "even"}, {}
)
```

`collect_evidence` runs every registered verifier whose `claims()` includes
the claim's `kind`, isolates each call, and drops any evidence whose
`claim_kind` was not declared by its own verifier — see
`codecalc/verifiers.py` and `docs/design/2026-08-19-extension-sdk.md` for the
full safeguard list.

## Trust model

This extension is `origin="third_party"`: it is trusted **by installation**,
the same posture as a `pip` dependency or a `pytest` plugin — see the module
docstring in `codecalc/extensions.py`. It requests one permission,
`verify`, and declares the `codecalc`/result-contract ranges it was built
against (`compatible_codecalc`, `compatible_contract`).
