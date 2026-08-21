#!/usr/bin/env python3
"""ClusterFuzzLite/atheris coverage-guided harness for
`codecalc.safe_expr.classify_unsafe` / `safe_parse` — the denylist screen
that stands between an `evaluate_expression`-shaped caller string and
SymPy's eval-based `parse_expr` (see codecalc/safe_expr.py's own docstring).

COMPLEMENTS scripts/fuzz.py, does not replace it. That script is a fast,
deterministic, seeded-mutation REGRESSION gate (tests/test_fuzz_smoke.py
runs a fixed slice of it on every PR); this harness is coverage-guided
DISCOVERY — libFuzzer's mutation engine, steered by which branches an input
actually reaches, running continuously via ClusterFuzzLite
(.github/workflows/cflite_pr.yml, cflite_batch.yml). See fuzz/README.md for
the full split.

SAME SURFACE, SAME SEED CORPUS, SAME CONTRACT as scripts/fuzz.py's
`fuzz_safe_expr` — imported from there, not copied (a copy that could drift
from scripts/fuzz.py's corpus is a defect here; see that module's own
docstring for the full contract this mirrors). Per that contract,
`classify_unsafe`/`safe_parse` never raise for a malformed or hostile
input — they RETURN `None`/a tuple describing the refusal. Nothing here
catches an exception, which is deliberate: scripts/fuzz.py's own
`fuzz_safe_expr` treats ANY exception escaping either function as a crash,
not just an unexpected one, and this harness holds it to the same bar —
atheris reports an uncaught exception as a finding, which is exactly the
outcome wanted here.
"""

import sys
from pathlib import Path

# scripts/fuzz.py is put on PYTHONPATH by .clusterfuzzlite/Dockerfile so a
# plain `import fuzz` resolves both for pyinstaller's OWN build-time import
# analysis (it runs as this same interpreter, so it honors PYTHONPATH like
# any other import) and for the frozen binary at runtime. This insert is a
# fallback for running this file directly outside that build (same pattern
# tests/test_fuzz_smoke.py already uses to reach scripts/fuzz.py).
_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import atheris  # noqa: E402 — needs the path insert above

with atheris.instrument_imports():
    import fuzz as fuzz_corpus  # scripts/fuzz.py: SEED_CORPUS_EXPR + contract
    from codecalc import safe_expr


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    # First bytes pick a seed from scripts/fuzz.py's own corpus (or "no
    # seed" — index == len(corpus) falls through to ""), the rest become a
    # suffix appended to it. libFuzzer's coverage feedback, not an explicit
    # mutation loop, is what steers exploration from here — that is the
    # entire point of running this alongside scripts/fuzz.py's fixed-stream
    # mutator rather than instead of it.
    corpus = fuzz_corpus.SEED_CORPUS_EXPR
    choice = fdp.ConsumeIntInRange(0, len(corpus))
    seed = corpus[choice] if choice < len(corpus) else ""
    tail = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    expression = seed + tail

    # Neither call is wrapped in try/except: see the module docstring above
    # for why an uncaught exception from either is exactly the finding this
    # harness exists to surface, not a false positive to swallow.
    safe_expr.classify_unsafe(expression)
    safe_expr.safe_parse(expression)


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
