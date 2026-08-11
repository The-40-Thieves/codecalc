#!/usr/bin/env python3
"""The published contract matches the code that produces it (THE-781).

`docs/contract/result-v1.schema.json` is what a client programs against. It is
generated from `codecalc/contract.py`, and a generated document that nobody
regenerates is a document that drifts — this repo has already shipped a repo
description and an AUDIT.md snapshot that said "48 tools" for a day while the
README correctly said 47, because the gate could only reach the README
(THE-777). A published schema is the same failure with a worse blast radius:
the README misleads a reader, a wrong schema makes a strictly-validating client
reject results that are correct.

So this asserts four things, all of them by re-deriving rather than by reading:

  1. The committed schema is byte-identical to what contract.py produces now.
     `--write` regenerates it; CI runs without the flag and fails on a diff.

  2. The schema's error-code enum is exactly `errors.ALL_CODES`. The enum is
     generated from that frozenset, so this catches a committed schema that
     predates a ninth code rather than a mismatch inside one run.

  3. The `verdict` enum covers every verdict EITHER backend can emit, and the
     ones it marks native-only are exactly the ones the fallback cannot
     produce. This is the check with teeth. `check_parity.py` diffs the two
     backends' KEY sets and says nothing about their VALUE vocabularies, so a
     new verdict added to executor/src/main.rs would be absent from the
     published enum and every client validating strictly would reject a
     perfectly good result — with nothing red anywhere, because the key set
     never changed.

  4. `CONTRACT_VERSION` appears in the docs that explain the versioning policy.
     A version constant nobody documents is a number, not a policy.

STDLIB ONLY, DELIBERATELY.
Gates in this repo run on a bare checkout, before dependencies exist, because
an invariant that only holds where the venv is installed is not an invariant.
So this does structural checks and does NOT validate the schema as JSON Schema
— `jsonschema` is a transitive dependency of `mcp` and cannot be assumed here.
Real dialect validation lives in `tests/test_contract.py`, which runs where the
dependencies do. That split is the point, not a shortcut: this file answers
"does the document match the code", the test answers "is the document valid and
does the product satisfy it".

FLOOR: every extractor below asserts it found a plausible number of things
before comparing. An extractor that silently matches nothing reports two empty
sets as equal, which is a green check for a scan that scanned nothing — the
failure mode this repo keeps rediscovering.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from codecalc import contract, errors  # noqa: E402  (after sys.path)

SCHEMA_PATH = REPO / "docs" / "contract" / "result-v1.schema.json"
DOCTOR_SCHEMA_PATH = REPO / "docs" / "contract" / "doctor-v1.schema.json"
DOCS_PATH = REPO / "docs" / "contract" / "README.md"
RUST_SRC = REPO / "executor" / "src" / "main.rs"
PY_SRC = REPO / "codecalc" / "executor.py"

#: A backend that yields fewer verdicts than this has not been parsed. Both
#: emit at least OK/TLE/OLE/RTE; four is the real floor and three leaves a
#: little room without leaving room for zero.
MIN_VERDICTS_PER_BACKEND = 3

#: The published document's two identifier URIs. They live HERE, in the
#: generator, and not in `codecalc/contract.py`, because tests/test_offline.py
#: bans every public URL literal from the package — the same ban that keeps
#: codecalc free of an LLM client and a docs fetch. Neither URI is ever
#: fetched, by us or (per MCP 2026-07-28's `$ref` rule) by a conforming client;
#: keeping them out of the wheel means the ban needs no exception, and a ban
#: with no exceptions is the only kind that stays true.
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID = (
    "https://raw.githubusercontent.com/The-40-Thieves/codecalc/main/"
    "docs/contract/result-v1.schema.json"
)
DOCTOR_SCHEMA_ID = (
    "https://raw.githubusercontent.com/The-40-Thieves/codecalc/main/"
    "docs/contract/doctor-v1.schema.json"
)

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"FAIL {msg}")


def ok(msg: str) -> None:
    print(f"ok   {msg}")


# ── 1. the committed schema is what contract.py produces ────────────────────
generated = json.dumps(
    contract.build_schema(dialect=JSON_SCHEMA_DIALECT, schema_id=SCHEMA_ID),
    indent=2, sort_keys=False) + "\n"

doctor_generated = json.dumps(
    contract.build_doctor_schema(dialect=JSON_SCHEMA_DIALECT,
                                 schema_id=DOCTOR_SCHEMA_ID),
    indent=2, sort_keys=False) + "\n"

if "--write" in sys.argv:
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(generated, encoding="utf-8")
    DOCTOR_SCHEMA_PATH.write_text(doctor_generated, encoding="utf-8")
    print(f"wrote {SCHEMA_PATH.relative_to(REPO)}")
    print(f"wrote {DOCTOR_SCHEMA_PATH.relative_to(REPO)}")
    sys.exit(0)

# The doctor schema drifts the same way and for the same reason: generated, and
# published for a client to program against. THE-780 asked for "a documented,
# versioned schema"; it is versioned by THIS contract rather than a third number.
if not DOCTOR_SCHEMA_PATH.exists():
    fail(f"{DOCTOR_SCHEMA_PATH.relative_to(REPO)} does not exist — run "
         "`python scripts/check_contract.py --write` and commit it")
elif DOCTOR_SCHEMA_PATH.read_text(encoding="utf-8") != doctor_generated:
    fail(f"{DOCTOR_SCHEMA_PATH.relative_to(REPO)} is stale — regenerate with "
         "`python scripts/check_contract.py --write`")
else:
    ok("published doctor schema matches contract.py")

published: dict | None = None

if not SCHEMA_PATH.exists():
    fail(f"{SCHEMA_PATH.relative_to(REPO)} does not exist — run "
         "`python scripts/check_contract.py --write` and commit it")
else:
    committed = SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        published = json.loads(committed)
    except ValueError as exc:
        fail(f"{SCHEMA_PATH.relative_to(REPO)} is not parseable JSON: {exc}")
    if committed != generated:
        fail(f"{SCHEMA_PATH.relative_to(REPO)} is stale — it does not match "
             "codecalc/contract.py. Regenerate with "
             "`python scripts/check_contract.py --write`. A published schema "
             "that lags the code makes a strict client reject valid results.")
    else:
        ok(f"published schema matches contract.py ({len(generated)} bytes)")


# ── 2. the PUBLISHED enum is the taxonomy ───────────────────────────────────
# Read out of the committed FILE, not out of `contract.RESULT_SCHEMA`.
#
# The first version of this check read the in-memory schema, which contract.py
# generates from `errors.ALL_CODES` — so it compared the taxonomy to itself and
# passed unconditionally. Caught by seeding a ninth code and watching the gate
# report "ok code enum is the taxonomy (9 codes)" while the published document
# still listed eight. That is the self-certification failure this repo keeps
# finding in other people's code, reproduced here in a file whose entire job is
# to notice drift.
#
# Against the file it is a real comparison: a code added without regenerating
# fails here AND at check 1, and the two failures say different things — one
# that the document is stale, one that a caller branching on `code` has no way
# to know the new value exists.
if published is None:
    pass  # already reported above; nothing to compare against
else:
    try:
        enum = published["$defs"]["rejected"]["properties"]["code"]["enum"]
    except (KeyError, TypeError):
        enum = []
    if len(enum) < 5:
        fail(f"the published code enum has {len(enum)} entries — the schema's "
             "shape changed and this extractor no longer finds it, which is a "
             "broken check, not a small taxonomy")
    elif sorted(errors.ALL_CODES) != sorted(enum):
        fail(f"published code enum {sorted(enum)} != errors.ALL_CODES "
             f"{sorted(errors.ALL_CODES)} — a caller branching on `code` cannot "
             "see the difference")
    else:
        ok(f"published code enum is the taxonomy ({len(enum)} codes)")


# ── 3. the verdict enum covers both backends ────────────────────────────────
def rust_verdicts() -> set[str]:
    """String literals the Rust `verdict()` returns, plus any emitted directly.

    Read from the source rather than by running the binary: this gate has to
    work on a checkout with no Rust toolchain, and the question is what the
    code can emit, not what one run happened to produce.
    """
    src = RUST_SRC.read_text(encoding="utf-8")
    found: set[str] = set()
    # the classifier's own body, from `fn verdict(` to the closing brace at
    # column 0 — the returns inside it are the vocabulary
    m = re.search(r"fn verdict\(.*?\n\}", src, re.S)
    if m:
        found |= set(re.findall(r'"([A-Z]{2,3})"', m.group(0)))
    # verdicts written straight into a JSON literal elsewhere in the file
    found |= set(re.findall(r'"verdict":\s*"([A-Z]{2,3})"', src))
    return found


def python_verdicts() -> set[str]:
    """Same question of the pure-Python fallback, via AST.

    AST rather than regex for the function body: `_fallback_verdict` returns
    bare string constants, and a regex for quoted uppercase words in a Python
    file also matches every docstring that happens to mention TLE — including
    this module's own explanation of why MLE is absent, which would have made
    the fallback look like it emits MLE and hidden exactly the asymmetry this
    check exists to pin down.
    """
    tree = ast.parse(PY_SRC.read_text(encoding="utf-8"), filename=str(PY_SRC))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_fallback_verdict":
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)):
                    found.add(sub.value.value)
        # `"verdict": "RTE"` written directly into a returned dict
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "verdict"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    found.add(v.value)
    return found


rust = rust_verdicts()
py = python_verdicts()

if len(rust) < MIN_VERDICTS_PER_BACKEND:
    fail(f"parsed only {sorted(rust)} out of {RUST_SRC.name} — the extractor is "
         "broken; two empty sets compare equal and that is not a pass")
elif len(py) < MIN_VERDICTS_PER_BACKEND:
    fail(f"parsed only {sorted(py)} out of {PY_SRC.name} — the extractor is broken")
else:
    declared = set(contract.VERDICTS)
    emitted = rust | py
    if declared != emitted:
        fail(f"contract.VERDICTS {sorted(declared)} != what the backends emit "
             f"{sorted(emitted)} (rust={sorted(rust)} python={sorted(py)}). "
             "A verdict missing from the published enum makes a strictly "
             "validating client reject a valid result.")
    else:
        ok(f"verdict enum is the union of both backends ({sorted(emitted)})")

    native_only = set(contract.NATIVE_ONLY_VERDICTS)
    actually_native_only = rust - py
    if native_only != actually_native_only:
        fail(f"contract.NATIVE_ONLY_VERDICTS {sorted(native_only)} != the "
             f"verdicts only the Rust backend emits {sorted(actually_native_only)}. "
             "This is the documented asymmetry between backends; if it changed, "
             "the docs changed with it or they are now wrong.")
    else:
        ok(f"native-only verdicts are accurate ({sorted(native_only) or 'none'})")


# ── 4. every Rust envelope return carries every envelope key ────────────────
# Structural, because the path that motivated it cannot be reached on demand.
#
# main.rs has a return for "the compile succeeded but consumed the entire
# wall-clock budget, so the run never started". It emits `verdict: TLE`, which
# by the contract's own discrimination rule makes it a full envelope — and it
# was missing four envelope fields, two of them (output_truncated, output_error)
# since before the byte counts existed. Nothing caught it because reaching it
# needs a compile that finishes in almost exactly the timeout, which no test can
# schedule.
#
# So this reads the source instead of running it: every `json!` block that
# declares a verdict must carry the whole envelope. A test could only ever cover
# the paths it can trigger; this covers the ones nobody can.
#
# `backend` and `contract_version` are excluded — the Rust binary emits neither,
# and both are attached on the Python side (see executor.execute).
RUST_EXEMPT = {"backend", "contract_version"}
RUST_REQUIRED = sorted(set(contract.ENVELOPE_KEYS) - RUST_EXEMPT)

#: Fewer than this and the block finder is broken rather than the source clean.
#: main.rs carries a run return, a compile-failure return and the budget return.
MIN_ENVELOPE_RETURNS = 3

rust_src = RUST_SRC.read_text(encoding="utf-8")
# Brace-match each `json!({` so a nested object cannot end the block early.
envelope_blocks: list[str] = []
for m in re.finditer(r"json!\(\{", rust_src):
    depth, i = 0, m.end() - 1
    while i < len(rust_src):
        if rust_src[i] == "{":
            depth += 1
        elif rust_src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = rust_src[m.start():i + 1]
    if '"verdict"' in block:
        envelope_blocks.append(block)

if len(envelope_blocks) < MIN_ENVELOPE_RETURNS:
    fail(f"found only {len(envelope_blocks)} Rust envelope returns (blocks "
         f"declaring a verdict); expected at least {MIN_ENVELOPE_RETURNS}. The "
         "block finder is broken, and an empty scan reports a clean source.")
else:
    incomplete = []
    for block in envelope_blocks:
        missing = [k for k in RUST_REQUIRED if f'"{k}"' not in block]
        if missing:
            head = block.splitlines()[1].strip()[:60] if "\n" in block else block[:60]
            incomplete.append((head, missing))
    if incomplete:
        for head, missing in incomplete:
            fail(f"a Rust envelope return is missing {missing} — near: {head}")
        fail("a return that declares a verdict IS an execution envelope by the "
             "contract's discrimination rule, so it must carry every envelope "
             "field or a caller reading one gets a KeyError on a path it cannot "
             "predict")
    else:
        ok(f"all {len(envelope_blocks)} Rust envelope returns carry "
           f"{len(RUST_REQUIRED)} envelope keys")


# ── 5. the version is documented, not just declared ─────────────────────────
if not DOCS_PATH.exists():
    fail(f"{DOCS_PATH.relative_to(REPO)} does not exist — the versioning policy "
         "is what makes CONTRACT_VERSION mean something")
else:
    docs = DOCS_PATH.read_text(encoding="utf-8")
    if contract.CONTRACT_VERSION not in docs:
        fail(f"CONTRACT_VERSION is {contract.CONTRACT_VERSION} and "
             f"{DOCS_PATH.relative_to(REPO)} never says so — bump the docs with "
             "the constant or the policy describes a version that is not served")
    elif "MAJOR" not in docs or "MINOR" not in docs:
        fail(f"{DOCS_PATH.relative_to(REPO)} does not state what each semver "
             "component is allowed to change; that statement IS the policy")
    else:
        ok(f"versioning policy documents {contract.CONTRACT_VERSION}")


if failures:
    print(f"\n=== {len(failures)} CONTRACT FAILURE(S) ===")
    sys.exit(1)
print("\n=== the published contract matches the code ===")
