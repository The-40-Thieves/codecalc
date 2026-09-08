"""The published result contract: one version number, one schema.

Slices 1 and 2 built the contract. Slice 1 (#120) made the two backends agree
on a key set and gated it. Slice 2 (#121) replaced 121 free-form error strings
with eight stable codes. Neither of them published anything: a caller could
observe the shape by running the thing, and had no document to program against
and no way to tell which shape a given server speaks.

This module is that document, kept next to the code that produces the shape so
the two cannot drift silently — `scripts/check_contract.py` regenerates
`docs/contract/result-v1.schema.json` from here and fails on a diff.

WHAT IS MEASURED, NOT ASSUMED
Every vocabulary below was read out of the two backends rather than designed:

  * `VERDICTS` is the union of `verdict()` in executor/src/main.rs:840 and
    `_fallback_verdict()` in executor.py:617. They are NOT the same set. The
    fallback cannot emit MLE, and says so in its own docstring: the Rust path
    infers MLE from a signal plus an RSS reading near the cap, and the fallback
    has neither, so an OOM there lands in RTE. That asymmetry is part of the
    contract and is written down rather than smoothed over.
  * `PHASES` is `compile` and `run`, the only two values either backend emits.
  * The envelope's required keys are the 19 that `check_parity` proves both
    backends return, measured by running both.

TWO SHAPES, BECAUSE THERE ARE TWO SHAPES
A request that reaches a runtime comes back as the full envelope. A request
rejected BEFORE execution — an unknown language, for instance — comes back from
the Rust binary as `{"ok": false, "error": ...}` and nothing else. Measured:

    executor.execute("nosuchlang", "x")  ->  keys ['backend', 'error', 'ok']

Writing a schema that requires all 19 keys everywhere would have been a
document that the product fails, and the honest fix is to say there are two
shapes and when each applies — not to pad the short one with nulls so a
validator stops complaining. A caller distinguishes them the same way the
schema does: `verdict` is present exactly when the code ran.

WHY THE VERSION IS EMITTED AND NOT JUST DOCUMENTED
A version nobody can read is not a version. `stamp()` puts `contract_version`
on every result at the single point both backends pass through, so a client can
branch on the contract it is actually being served rather than on the contract
it hoped was deployed.
"""

from __future__ import annotations

from . import errors, grades

#: The contract's own version, semver. See docs/contract/README.md for what
#: each component is allowed to change — the short form is that MAJOR is the
#: only one that may break a reader, and it carries a twelve-month deprecation
#: window before anything is removed.
CONTRACT_VERSION = "1.10.0"

# THE `$schema` AND `$id` URIs ARE NOT HERE ON PURPOSE.
#
# `tests/test_offline.py` bans every public URL literal from this package. That
# ban is why codecalc has no LLM client and no docs fetch, and the two URIs a
# published JSON Schema wants — the 2020-12 dialect and the document's own `$id`
# — would be the first exceptions to it.
#
# They are identifiers, not endpoints: nothing here fetches either one, and MCP
# 2026-07-28 tells implementations they MUST NOT auto-dereference external `$ref`
# URIs. But "it is only an identifier" is exactly how a fetch arrives a year
# later, and the gate is worth more than the convenience of holding two strings
# in the package that ships.
#
# So `scripts/check_contract.py` owns them and injects them when it generates
# the document. The wheel contains no URL at all; the published file is
# complete. Omitting `$schema` from the in-package dict is also correct rather
# than merely tolerable: MCP defaults `outputSchema` to 2020-12 when no
# `$schema` is present, so this dict remains a valid output schema as-is.

#: Run classification. The union of both backends; see the module docstring.
VERDICTS = ("OK", "TLE", "OLE", "MLE", "RTE")

#: Verdicts only the native executor can produce. Not a defect and not a gap to
#: close: the fallback declines to guess MLE rather than reporting a ceiling it
#: did not measure, which is the same rule `unenforced` exists to serve.
NATIVE_ONLY_VERDICTS = ("MLE",)

#: Which step produced the result.
PHASES = ("compile", "run")

#: Which executor answered a full-envelope result.
BACKENDS = ("rust", "python")

#: Sessions are a THIRD backend and were missed by the first version of this
#: module, which asserted `executor.execute` was the one place every execution
#: result passes through. It is not: `execute_code(session_id=...)` routes to a
#: warm worker in sessions.py, never touches `executor.execute`, and returns ten
#: keys with `backend="session-worker"` — a value the first published enum did
#: not contain, so every session result would have failed validation against the
#: document that claimed to describe it. Found by cross-vendor review.
SESSION_BACKEND = "session-worker"

#: The keys `check_parity.py` proves both backends return. Adding one is a MINOR
#: change; removing one is MAJOR.
ENVELOPE_KEYS = (
    "ok", "language", "phase", "backend", "platform",
    "stdout", "stderr", "exit_code", "timed_out",
    "verdict", "output_truncated", "output_error",
    "duration_ms", "compile_ms", "total_ms", "cpu_ms", "peak_memory_kb",
    "unenforced", "workdir",
    "stdout_bytes", "stderr_bytes",
)


def _error_properties() -> dict:
    """The error half. `code`'s enum is generated from errors.ALL_CODES.

    Generated rather than transcribed: a hand-copied enum is a second place the
    taxonomy lives, and the two would agree right up until someone added a
    ninth code. `scripts/check_contract.py` re-derives this and diffs it, so a
    new code that never reached the schema is a failing gate rather than a
    published document that quietly lies.
    """
    return {
        "ok": {"const": False},
        "code": {
            "type": "string",
            "enum": sorted(errors.ALL_CODES),
            "description": (
                "Stable machine-readable failure category. A caller MAY branch "
                "on this. An unrecognised value MUST be treated as 'internal' "
                "rather than as a protocol violation — that is what makes "
                "adding a code a MINOR change."
            ),
        },
        "error": {
            "type": "string",
            "description": (
                "Human-facing explanation. NOT stable and NOT a branch target: "
                "the whole point of `code` is that this text is free to improve."
            ),
        },
        "remedy": {
            "type": "string",
            "description": "What the caller can do about it, one line.",
        },
        "code_inferred": {
            "type": "boolean",
            "description": (
                "True when the code was derived from the message text rather "
                "than chosen where the failure was raised. A weaker claim, "
                "marked as such. Absent means the code came from the raise site."
            ),
        },
    }


def _dependencies_property() -> dict:
    """The optional `dependencies` field: 1.4.0's MINOR addition.

    Present only when the run declared dependencies (a PEP 723 block or the
    `dependencies` tool argument) — absent otherwise, so every existing
    result is byte-for-byte unchanged. One entry per dependency ATTEMPTED,
    in order; installation stops at the first failure, so a shorter-than-
    requested list on a failing run means "these are as far as it got",
    not "these are all that were declared".
    """
    return {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["spec", "language", "ok", "installer", "elapsed_ms"],
            "properties": {
                "spec": {"type": "string", "description": "The dependency as declared, verbatim."},
                "language": {"type": "string"},
                "ok": {"type": "boolean"},
                "installer": {
                    "type": "string",
                    "description": "The package manager invoked (uv, npm, ...).",
                },
                "elapsed_ms": {
                    "type": "integer", "minimum": 0,
                    "description": (
                        "Wall-clock time this ONE install took, milliseconds. "
                        "Not the run's own `timeout`: a separate, fixed "
                        "aggregate budget governs installs (see README/"
                        "SECURITY.md); exceeding it fails the run before this "
                        "array gains a further entry."
                    ),
                },
                "unenforced": {
                    "type": "array", "items": {"type": "string"},
                    "description": (
                        "This install's own confinement disclosure — "
                        "packages.install's `unenforced`, carried per-entry "
                        "rather than merged into the run's own."
                    ),
                },
            },
        },
        "description": (
            "Present only when the run declared dependencies. Installed "
            "BEFORE the sandboxed step, through the confined install_package "
            "path — never inside the sandbox."
        ),
    }


def _comparison_row_properties() -> dict:
    """A `compare_execution` result's per-language `results[]` row.

    Read out of `tools.compare_execution`'s own row-construction, not out of
    the full executor result each row derives from — most envelope fields
    (`platform`, `backend`, `verdict`, `unenforced`, the byte/timing
    breakdown) never survive into the row; only what a caller comparing
    languages side by side needs to. `error`/`code`/`remedy`/`code_inferred`
    (from `_error_properties()`, `ok` excluded — a row's `ok` is not fixed to
    `False` the way a dedicated error shape's is) are new in 1.10.0 and only
    present on a row that failed for a REQUEST-level reason — see
    `errors.stamp_row`'s docstring for the exact rule, including why an
    ordinary program failure (a real RTE/OLE `exit_code`) carries none of
    them.
    """
    return {
        "language": {"type": "string"},
        "ok": {"type": ["boolean", "null"]},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "exit_code": {"type": ["integer", "null"]},
        "duration_ms": {"type": ["number", "null"]},
        "timed_out": {"type": "boolean"},
        "cold_retry": {
            "type": "boolean",
            "description": (
                "True when this language timed out once and was retried "
                "exactly one warm time — a cold-start casualty (a globally "
                "slow runner, not a defect in this language's snippet) gets "
                "one chance to prove it before being reported as a failure."
            ),
        },
        "cold_retry_recovered": {
            "type": "boolean",
            "description": "Present only when cold_retry is true: did the warm retry succeed.",
        },
        "first_attempt_ms": {
            "type": ["number", "null"],
            "description": "Present only when cold_retry is true: the FIRST (timed-out) attempt's duration.",
        },
        "dependencies": {
            "type": "object",
            "description": (
                "Present only on a python3 row whose snippet carries an "
                "unparsed PEP 723 '# /// script' block — compare_execution "
                "never installs dependencies, so this discloses the block "
                "rather than silently ignoring it."
            ),
            "required": ["status", "reason"],
            "properties": {
                "status": {"const": "unsupported"},
                "reason": {"type": "string"},
            },
        },
        **{k: v for k, v in _error_properties().items() if k != "ok"},
    }


def _edge_case_run_properties() -> dict:
    """One language's run inside a `compare_edge_cases` result's per-input
    `runs` map (`results[i].runs[lang]`, and the restricted copy under a
    `divergences[i].runs`).

    `error`/`code`/`remedy`/`code_inferred` (`ok` excluded from
    `_error_properties()`, same reasoning as `_comparison_row_properties`)
    are new in 1.10.0 and present only on a run that failed for a
    REQUEST-level reason — see `errors.stamp_row`.
    """
    return {
        "type": "object",
        "required": ["ok", "stdout", "verdict", "stderr"],
        "properties": {
            "ok": {"type": ["boolean", "null"]},
            "stdout": {"type": "string"},
            "verdict": {
                "type": ["string", "null"],
                "description": "null when nothing ran (e.g. an unknown language).",
            },
            "stderr": {"type": "string"},
            **{k: v for k, v in _error_properties().items() if k != "ok"},
        },
    }


def _translation_side_properties() -> dict:
    """One side (`source` or `target`) of a `verify_translation` case.

    Read out of `translation.verify_translation`'s per-case dict — see the
    `cases.append({...})` block in codecalc/translation.py — not out of the
    full executor result each side derives from. Only four fields survive
    into the case: `phase` is null whenever nothing ran (an unknown
    language never reaches a runtime, so it never sets `phase`), which is
    the same discriminator the five execution shapes already use.
    """
    return {
        "type": "object",
        "required": ["ok", "phase", "stdout", "stderr"],
        "properties": {
            "ok": {"type": "boolean"},
            "phase": {
                "type": ["string", "null"],
                "description": (
                    "compile/run when this side actually reached a runtime; "
                    "null when nothing ran."
                ),
            },
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
        },
    }


#: The keys every `verify_translation` result carries, whether it is the
#: top-level tool result or the BARE dict `verify_optimization` embeds under
#: `verification` — see `_translation_evidence_properties` below for why the
#: two are not the same schema.
TRANSLATION_EVIDENCE_KEYS = (
    "ok", "passed", "reason", "matched", "mismatched", "inconclusive", "total", "cases",
)


def _translation_evidence_properties() -> dict:
    """The evidence `translation.aggregate` + `translation.verify_translation`
    produce: pass/fail plus the full per-case trail. Shared by TWO shapes in
    this schema:

      * the top-level `verify_translation` TOOL result (`translation_verification`
        below), which also carries `grade`/`grade_basis`/`grade_rules_version`
        because the MCP tool boundary always grades it (server.py always calls
        `grades.grade_verify_translation`);
      * the `verification` field `verify_optimization` embeds — `optimization.py`
        calls `translation.verify_translation()` directly, the BARE function,
        never the graded/stamped tool wrapper, so that nested value carries
        none of the three grade keys and no `contract_version`. Measured:
        `server.verify_optimization(...)["verification"]` has exactly these
        eight keys, never more.

    `ok` here means "the tool completed and produced a real answer", never
    the verdict — `aggregate` sets it True on a mismatch exactly the same as
    on a pass, matching `verify_optimization`'s own `{"ok": True, "accepted":
    False, ...}` early return. This one key is required on BOTH shapes,
    which is why it lives in this shared dict rather than being added only
    where the two diverge.

    One shape, two required-key sets — see `translation_verification` and
    `optimization_verification`'s own `required` lists below for where they
    diverge.
    """
    case = {
        "type": "object",
        "required": ["input", "outcome", "reason", "match", "source", "target"],
        "properties": {
            "input": {"type": "string"},
            "outcome": {
                "type": "string",
                "enum": ["match", "mismatch", "inconclusive"],
                "description": (
                    "See translation.classify_case. 'inconclusive' is NOT a "
                    "pass — it means neither side gave usable evidence."
                ),
            },
            "reason": {"type": "string"},
            "match": {
                "type": "boolean",
                "description": (
                    "True only when outcome == 'match'. Kept for callers that "
                    "predate the three-way outcome."
                ),
            },
            "source": _translation_side_properties(),
            "target": _translation_side_properties(),
        },
    }
    return {
        "ok": {
            "type": "boolean",
            "description": (
                "True whenever the tool completed and produced a real "
                "answer — set unconditionally by `aggregate`, including on "
                "a mismatch. NOT the verdict: read `passed` for that."
            ),
        },
        "passed": {
            "type": "boolean",
            "description": (
                "True only when there is REAL evidence for equivalence: no "
                "mismatch, and at least one case where both programs actually "
                "ran and agreed. An all-inconclusive run is false."
            ),
        },
        "reason": {
            "type": ["string", "null"],
            "description": "null only when passed is true; a non-pass always names why.",
        },
        "matched": {"type": "integer", "minimum": 0},
        "mismatched": {"type": "integer", "minimum": 0},
        "inconclusive": {"type": "integer", "minimum": 0},
        "total": {"type": "integer", "minimum": 0},
        "cases": {"type": "array", "items": case},
    }


def _speedup_properties() -> dict:
    """`optimization._speedup`'s return: a measured ratio, or an honest
    refusal to report one. `per_size` and `reason` are mutually exclusive in
    practice (one or the other, never both — see `_speedup`'s two `return`
    statements) but neither is declared `required`, since a schema that
    demanded both would reject every real result.

    `n_after` (added in `1.7.0`): present only when the candidate's own n at
    this position differs from the baseline's `n` — `_align_sizes` leaves a
    position deliberately unaligned when the candidate exhausted its rescale
    budget without ever clearing the visibility floor there, rather than
    forcing the baseline to re-measure at an unvalidated size (see
    `optimization._align_sizes`'s docstring). Its absence means the two
    sides ran at the same n, which is still the common case.
    """
    return {
        "type": "object",
        "required": ["ratio", "measurable"],
        "properties": {
            "ratio": {
                "type": ["number", "null"],
                "description": "Median before/after ratio at sizes where both ran. null when not measurable.",
            },
            "measurable": {"type": "boolean"},
            "reason": {
                "type": "string",
                "description": "Present only when measurable is false.",
            },
            "per_size": {
                "type": "array",
                "description": "Present only when measurable is true.",
                "items": {
                    "type": "object",
                    "required": ["n", "before_ms", "after_ms", "ratio"],
                    "properties": {
                        "n": {"type": "integer", "minimum": 0},
                        "before_ms": {"type": "number"},
                        "after_ms": {"type": "number"},
                        "ratio": {"type": "number"},
                        "n_after": {
                            "type": "integer",
                            "minimum": 0,
                            "description": (
                                "Present only when the candidate ran at a "
                                "different n than the baseline's n above — "
                                "an intentionally unaligned, disclosed "
                                "pairing, not a mislabelled row."
                            ),
                        },
                    },
                },
            },
        },
    }


def _inference_properties() -> dict:
    """`optimization._infer_speedup`'s return: the per-size one-sided
    Mann-Whitney U test that `_accept_decision` requires a MAJORITY of sizes
    to reject before `accepted` can ever be true. Every key here is read
    straight off `_infer_speedup`'s own `return` and each `per_size` entry's
    `dict` literal — `size`/`n_before`/`n_after`/`u`/`p_value`/`rank_biserial`.

    `sizes_below_floor` (added in `1.6.0`): every size excluded from
    `per_size`/`sizes_total`/`sizes_rejecting` for ANY reason — never
    cleared `optimization._VISIBILITY_FLOOR_MS` within `_timed`'s per-size
    rescale budget, an unmeasurable baseline/candidate (the same floor
    `_speedup` already applies), too few comparable runs, or no recorded
    measurement at all — rather than tested on a still-noisy sample or
    silently dropped. `before_ms`/`after_ms` are `null` when no duration was
    ever available for that size. `len(sizes) == len(per_size) +
    len(sizes_below_floor)` always holds. Always present (an empty list when
    every size cleared every bar, which is the common case), same as
    `sizes_rejecting`/`sizes_total` always are.

    The exclusion rule itself is ASYMMETRIC as of `1.7.0`: a position is
    excluded only when the BASELINE side is unmeasurable or never clears the
    floor, regardless of the candidate — a floor-clearing baseline paired
    against a candidate too fast to register even after exhausting its own
    rescale budget is the most decisive result this tool can produce, not a
    measurement gap (an earlier, symmetric rule drove `sizes_total` to 0 for
    exactly that case; see `optimization._comparable_positions`'s
    docstring). `size_after` (`1.7.0`, per-entry, only in `per_size` — see
    below) discloses when such a position pairs the baseline's sample
    against the candidate's own, larger n rather than the same n on both
    sides.
    """
    return {
        "type": "object",
        "required": ["test", "alternative", "alpha", "per_size",
                     "sizes_rejecting", "sizes_total", "sizes_below_floor",
                     "decision_basis"],
        "properties": {
            "test": {"type": "string"},
            "alternative": {"type": "string"},
            "alpha": {"type": "number"},
            "per_size": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["size", "n_before", "n_after", "u",
                                 "p_value", "rank_biserial"],
                    "properties": {
                        "size": {"type": "integer", "minimum": 0},
                        "n_before": {"type": "integer", "minimum": 0},
                        "n_after": {"type": "integer", "minimum": 0},
                        "u": {"type": "number"},
                        "p_value": {"type": "number", "minimum": 0, "maximum": 1},
                        "rank_biserial": {"type": "number"},
                        "size_after": {
                            "type": "integer",
                            "minimum": 0,
                            "description": (
                                "Present only when the candidate's own n at "
                                "this position differs from the baseline's "
                                "`size` above — an intentionally unaligned, "
                                "disclosed pairing (see "
                                "optimization._align_sizes), not a "
                                "mislabelled row. NOT a sample count — "
                                "`n_before`/`n_after` above already mean "
                                "that."
                            ),
                        },
                    },
                },
            },
            "sizes_rejecting": {"type": "integer", "minimum": 0},
            "sizes_total": {"type": "integer", "minimum": 0},
            "sizes_below_floor": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["size", "before_ms", "after_ms"],
                    "properties": {
                        "size": {"type": "integer", "minimum": 0},
                        # `null` when no duration was ever recorded for this
                        # size (a before/after length mismatch) — every
                        # exclusion reason lands here, not just a visibility
                        # miss with real numbers on both sides.
                        "before_ms": {"type": ["number", "null"]},
                        "after_ms": {"type": ["number", "null"]},
                    },
                },
            },
            "decision_basis": {"type": "string"},
        },
    }


def build_schema(dialect: str | None = None, schema_id: str | None = None) -> dict:
    """The published schema, as a dict. Single source of truth.

    `dialect` and `schema_id` are injected by `scripts/check_contract.py` rather
    than defaulted here — see the note above on why no URL literal lives in this
    package. Called with neither, the result is the same document without its
    two identifier keys, which is still a valid 2020-12 output schema.

    `additionalProperties` is left open deliberately. Forward compatibility is
    the reason: adding a field has to be a MINOR change, and a closed schema
    would make every addition breaking for any client that validates strictly.
    The rule that pairs with it — clients MUST ignore fields they do not know —
    is stated in the docs and in the descriptions below.
    """
    identifiers = {}
    if dialect:
        identifiers["$schema"] = dialect
    if schema_id:
        identifiers["$id"] = schema_id
    return {
        **identifiers,
        "title": "codecalc execution result",
        "description": (
            f"The result contract for codecalc's execution tools, version "
            f"{CONTRACT_VERSION}. Generated from codecalc/contract.py by "
            f"scripts/check_contract.py — edit the module, not this file."
        ),
        "type": "object",
        "required": ["ok", "contract_version"],
        "properties": {
            "contract_version": {
                "type": "string",
                "pattern": r"^\d+\.\d+\.\d+$",
                "description": (
                    "Semver of this contract, as served. MAJOR is the only "
                    "component that may break a reader."
                ),
            },
            "ok": {
                "type": "boolean",
                "description": (
                    "True only when the code ran and exited 0. A program that "
                    "runs correctly and exits non-zero is ok=false with "
                    "exit_code set and verdict=RTE — measured: exit(3) returns "
                    "ok=false, exit_code=3, verdict=RTE. So `ok` does NOT "
                    "separate 'codecalc worked' from 'your program worked'; "
                    "read `verdict` and `exit_code` for that, and `code` to "
                    "tell a rejected request from a failed run."
                ),
            },
        },
        # FIVE execution shapes, discriminated so that exactly one branch can
        # match:
        #
        #   envelope       verdict present, backend in (rust, python)
        #   session        backend == "session-worker"
        #   compact        verdict present, NO backend key
        #   rejected       no verdict at all, carries `error`
        #   run_lifecycle  a run_id, a state, and NO verdict/error/backend
        #
        # The first version of this schema had two branches and asserted that
        # `executor.execute` was the single choke point for every execution
        # result. Cross-vendor review disproved it in three places at once:
        # `compact_result` rebuilds a four-key result, the native streaming path
        # returns raw executor JSON, and sessions bypass the function entirely.
        # All three produced results that NEITHER branch accepted — a published
        # document that the product fails, which is worse than no document.
        #
        # A second cross-vendor review (F2) found the same defect one surface
        # over: run_submit / run_inspect-while-active / run_cancel return
        # responses stamped `contract_version` that matched none of the four —
        # ok=true with a run_id, no verdict, no backend, no error. `run_lifecycle`
        # is that fifth shape. It is discriminated AWAY from a TERMINAL
        # run_inspect (which returns a full envelope, or a rejected-shaped
        # failure, plus run_* extras) by forbidding verdict/error/backend: a
        # terminal success carries `verdict`, a terminal failure carries `error`,
        # so neither collides with this branch and `oneOf`'s exactly-one rule
        # holds.
        #
        # THREE VERIFICATION shapes, added in 1.5.0:
        #
        #   translation_verification   `passed` present
        #   optimization_verification  `accepted` present
        #   edge_case_comparison       `divergence_count` present
        #
        # Before this version, `verify_translation` and `verify_optimization`
        # were stamped `contract_version` the same as every other tool (see
        # `codecalc/server.py`'s `_coded` wrapper, applied to all 52 tools) but
        # matched NONE of the five execution shapes above — the same defect
        # `run_lifecycle` closed for the background-run tools, found here by
        # re-reading docs/contract/README.md's own caveat that these results
        # were "additive, and undocumented". `translation.aggregate()` also
        # had a real omission behind that gap, not just a shape to document
        # around: it never set `ok` at all, which review caught as
        # inconsistent with `verify_optimization`'s own `{"ok": True,
        # "accepted": False, ...}` early return — `ok` means "the tool
        # completed and produced a real answer", never the verdict, so a
        # mismatch is `ok=True` exactly like a pass. Fixed at the source
        # (`aggregate` now always sets it), so `translation_verification`
        # requires `ok` the same as every other branch here.
        #
        # `compare_edge_cases` has the same gap and closes it the same way:
        # its `ok: false` refusal ("provide at least one snippet") already
        # matches `rejected` (ok + error, no verdict) and needed no new
        # branch; only its `ok: true` success shape — `divergence_count`,
        # `divergences`, `results` — was unmodeled, which `edge_case_comparison`
        # now covers.
        #
        # ONE MORE, added in 1.10.0: `comparison_rows` for `compare_execution`.
        # It had the identical gap `edge_case_comparison` closed above — an
        # `ok: true` success shape (`count`, `succeeded`, `results`,
        # `fastest`, `fastest_note`, `discrepancies`) that matched no branch
        # here, found the same way: `compare_execution`'s per-language ROWS
        # never carried `error`/`code`/`remedy` at all (see
        # `errors.stamp_row`), so fixing that meant giving the shape those
        # rows live in a schema to be checked against, rather than adding a
        # field to a document that never covered the result in the first
        # place. Its own refusal shape does not exist — `compare_execution`
        # has none; an empty `snippets` dict just returns an empty table
        # (`count: 0`), still `comparison_rows`, not `rejected`.
        #
        # None of the four collides with the five execution shapes or with
        # each other: none of them ever carries `verdict`, `backend`, or
        # `run_id`/`state`; `accepted`, `divergence_count`, and `count`
        # together with `results` (a compare_execution row array, never a
        # dependency array — see `_comparison_row_properties`) appear on no
        # other shape in this document. A `verify_optimization` measurement
        # failure — baseline or candidate — is `{ok: false, error: ...,
        # code: ...}` with no `accepted` key, so it matches `rejected` rather
        # than `optimization_verification`, exactly like `compare_edge_cases`'s
        # own refusal.
        "oneOf": [
            {"$ref": "#/$defs/execution_envelope"},
            {"$ref": "#/$defs/session_result"},
            {"$ref": "#/$defs/compact_result"},
            {"$ref": "#/$defs/rejected"},
            {"$ref": "#/$defs/run_lifecycle"},
            {"$ref": "#/$defs/translation_verification"},
            {"$ref": "#/$defs/optimization_verification"},
            {"$ref": "#/$defs/edge_case_comparison"},
            {"$ref": "#/$defs/comparison_rows"},
        ],
        "$defs": {
            "execution_envelope": {
                "title": "a request that reached a runtime",
                "description": (
                    "Returned when the code was handed to a language runtime, "
                    "whether or not it then succeeded. Both backends return "
                    "every key listed here; scripts/check_parity.py runs both "
                    "and diffs the key sets."
                ),
                "type": "object",
                "required": list(ENVELOPE_KEYS),
                "properties": {
                    "ok": {"type": "boolean"},
                    "language": {"type": "string"},
                    "phase": {"type": "string", "enum": list(PHASES)},
                    "backend": {
                        "type": "string", "enum": list(BACKENDS),
                        "description": (
                            "Which executor answered. 'python' is the pure-"
                            "Python fallback and enforces strictly less; read "
                            "`unenforced` for what it could not apply."
                        ),
                    },
                    "platform": {"type": "string"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {
                        "type": ["integer", "null"],
                        "description": "null when the process was killed rather than exiting.",
                    },
                    "timed_out": {"type": "boolean"},
                    "verdict": {
                        "type": "string", "enum": list(VERDICTS),
                        "description": (
                            "OK ran and exited 0 · TLE wall-clock kill · OLE "
                            "output over cap · MLE memory ceiling · RTE nonzero "
                            "exit or signal. MLE is native-only: the fallback "
                            "cannot measure it and reports RTE instead of "
                            "guessing."
                        ),
                    },
                    "output_truncated": {
                        "type": "boolean",
                        "description": (
                            "True when stdout/stderr was cut at the cap. Slice 1 "
                            "(#120) exists because the Rust backend computed "
                            "this, raised the OLE verdict from it, and did not "
                            "emit it, while the fallback did."
                        ),
                    },
                    "output_error": {
                        "type": ["string", "null"],
                        "description": (
                            "Why output could not be read, when it could not. "
                            "null is 'no problem'; a string here means the "
                            "output is unreliable even if stdout looks fine."
                        ),
                    },
                    "stdout_bytes": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "description": (
                            "Bytes of stdout OBSERVED before the response cap "
                            "was applied. When `output_truncated` is false this "
                            "is exact and both backends agree. When it is true "
                            "this is a LOWER BOUND on what the program would "
                            "have produced, and the two backends can differ: "
                            "the fallback kills the process as soon as its "
                            "drain crosses the cap, while the native executor "
                            "lets it run on under a separate file-size ceiling. "
                            "Measured on one 200 KiB writer at a 1 KiB cap: "
                            "native 204800, fallback 65536. Useful for sizing a "
                            "retry — raise `max_output_kb` above this — but not "
                            "a promise about total output. null means not "
                            "measured, never zero; a program that printed "
                            "nothing reports 0."
                        ),
                    },
                    "stderr_bytes": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "description": "As stdout_bytes, for stderr.",
                    },
                    "duration_ms": {"type": "integer", "minimum": 0},
                    "compile_ms": {"type": "integer", "minimum": 0},
                    "total_ms": {"type": "integer", "minimum": 0},
                    "cpu_ms": {"type": "integer", "minimum": 0},
                    "peak_memory_kb": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "description": (
                            "Peak resident memory, or null when this host could "
                            "not attribute it to this run. The pure-Python "
                            "fallback always returns null and names the reason "
                            "in `unenforced`: ru_maxrss is a high-water mark "
                            "for the whole process. null is 'not measured', "
                            "never 'measured as zero'."
                        ),
                    },
                    "unenforced": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Bounds this host could not apply, named. An empty "
                            "array is a claim that everything requested was "
                            "enforced; a non-empty one is the sandbox telling "
                            "you what it did not do."
                        ),
                    },
                    "workdir": {"type": "string"},
                    "dependencies": _dependencies_property(),
                    # `code`/`error`/`remedy`/`code_inferred` on an envelope
                    # that DID reach a runtime, not just on the short
                    # `rejected` shape below: a spawn failure (a missing
                    # runtime/compiler) still carries `verdict` — the code
                    # ran as far as it could — but a caller needs the same
                    # `code`/`error` pair every OTHER failure gets. 1.9.0
                    # is the first version either backend actually SETS
                    # `error` here (see docs/contract/README.md); this
                    # `properties` entry already declared it beforehand
                    # because `_error_properties()` is shared with the
                    # dead-worker session shape, so no schema property
                    # changed shape at this bump — only what a real result
                    # populates.
                    **{k: v for k, v in _error_properties().items() if k != "ok"},
                },
            },
            "session_result": {
                "title": "a result from a warm session worker",
                "description": (
                    "`execute_code(session_id=...)` runs in a persistent worker "
                    "rather than a fresh sandbox, so the fields a fresh run "
                    "reports about ITS environment — language, platform, "
                    "workdir, per-run timings — do not apply and are absent "
                    "rather than faked. `unenforced` is still required and is "
                    "the field that matters most here: a worker cannot apply "
                    "several ceilings a fresh sandbox can, and it says so."
                ),
                # `stdout`/`stderr`/`exit_code`/`output_truncated`/`session` are
                # NOT required, because a session call whose worker DIED never
                # ran the code and has none of them: it returns verdict RTE,
                # backend session-worker, `error`, a worker_failure code and the
                # disclosure list, and nothing else.
                #
                # Found by CI, on a runner where the worker could not start,
                # after both a local suite and a cross-vendor review had passed.
                # Neither reached it because both ran where sessions work. What
                # `unenforced` and `verdict` being required buys is that a
                # session result still cannot omit its disclosures.
                "type": "object",
                "required": ["ok", "verdict", "backend", "unenforced"],
                "properties": {
                    "ok": {"type": "boolean"},
                    "backend": {"const": SESSION_BACKEND},
                    # A boolean flag meaning "this ran in a session", NOT the
                    # session id. Written as a string first, from the field's
                    # name rather than from its value; the validator caught it.
                    "session": {"type": "boolean"},
                    "confined": {"type": "boolean"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": ["integer", "null"]},
                    "output_truncated": {"type": "boolean"},
                    "unenforced": {"type": "array", "items": {"type": "string"}},
                    "dependencies": _dependencies_property(),
                    **{k: v for k, v in _error_properties().items() if k != "ok"},
                },
            },
            "compact_result": {
                "title": "a deliberately small result",
                "description": (
                    "What `execute_code(compact=True)` returns. Diagnostics a "
                    "caller can live without are dropped to save tokens; "
                    "`unenforced` and `output_error` are NOT droppable and "
                    "appear whenever they say anything, which is the whole "
                    "difference between this and the version of compact mode "
                    "that was a defect (#117). No `backend` key, which is what "
                    "distinguishes this branch from the full envelope."
                ),
                "type": "object",
                "required": ["ok", "verdict", "stdout", "exit_code"],
                "not": {"required": ["backend"]},
                "properties": {
                    "ok": {"type": "boolean"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "stdout": {"type": "string"},
                    "exit_code": {"type": ["integer", "null"]},
                    "unenforced": {"type": "array", "items": {"type": "string"}},
                    "output_error": {"type": ["string", "null"]},
                },
            },
            "run_lifecycle": {
                "title": "a background-run lifecycle response",
                "description": (
                    "Returned by run_submit, by run_inspect WHILE the run is "
                    "still active (state running/cancelling), and by run_cancel. "
                    "It names the run — `run_id` and `state` — but nothing ran "
                    "yet through it, so there is no `verdict`, no `backend`, and "
                    "no `error`. A TERMINAL run_inspect does not use this shape: "
                    "it returns the full execution envelope (or, on a failed "
                    "run, the rejected error shape) merged with the same run_* "
                    "extras. Forbidding verdict/error/backend here is what keeps "
                    "this branch from colliding with those, so exactly one "
                    "oneOf branch matches any stamped run response."
                ),
                "type": "object",
                "required": ["ok", "run_id", "state"],
                "not": {"anyOf": [
                    {"required": ["verdict"]},
                    {"required": ["error"]},
                    {"required": ["backend"]},
                ]},
                "properties": {
                    "ok": {"type": "boolean"},
                    "run_id": {"type": "string"},
                    "provider_id": {"type": "string"},
                    "started_at": {"type": "number"},
                    "deadline": {"type": "number"},
                    "state": {
                        "type": "string",
                        "description": (
                            "The run's lifecycle state — running/cancelling "
                            "before it settles, or a terminal state reported by "
                            "run_cancel on an already-finished run. Left as a "
                            "free string rather than a closed enum: the states "
                            "are owned by run_supervisor, and pinning them here "
                            "would be a second declaration that could drift."
                        ),
                    },
                    "cleaned": {"type": "boolean"},
                    "cancelled": {"type": "boolean"},
                },
            },
            "rejected": {
                "title": "a request rejected before execution",
                "description": (
                    "Returned when nothing ran — an unknown language, a "
                    "malformed request, an executor that could not start. "
                    "Measured: executor.execute('nosuchlang', 'x') returns "
                    "exactly ok/error/backend. There is no verdict because "
                    "there was no run to classify."
                ),
                "type": "object",
                "required": ["ok", "error"],
                "properties": _error_properties(),
                "not": {"required": ["verdict"]},
            },
            "translation_verification": {
                "title": "a verify_translation result",
                "description": (
                    "The `verify_translation` tool's result: did a source "
                    "program and a claimed port agree, across every test "
                    "input, with real evidence either way (see "
                    "translation.classify_case/aggregate). `grade`/"
                    "`grade_basis`/`grade_rules_version` are always present — "
                    "server.py grades every call, pass or not — from "
                    "codecalc.grades, itself a closed, versioned vocabulary. "
                    "`ok` is always true here — it means the tool completed "
                    "and produced a real answer, never the verdict; read "
                    "`passed` for that."
                ),
                "type": "object",
                "required": [*TRANSLATION_EVIDENCE_KEYS,
                             "grade", "grade_basis", "grade_rules_version"],
                "properties": {
                    **_translation_evidence_properties(),
                    "grade": {"type": "string", "enum": sorted(grades.GRADES)},
                    "grade_basis": {"type": "string"},
                    "grade_rules_version": {"type": "string"},
                },
            },
            "optimization_verification": {
                "title": "a verify_optimization result",
                "description": (
                    "The `verify_optimization` tool's result: is `candidate` a "
                    "genuine optimisation of `original` — same outputs "
                    "(`verification`, the embedded correctness check) AND "
                    "measurably, significantly faster (`speedup`/`inference`). "
                    "`speedup`/`inference`/`min_speedup`/`language`/`detail` "
                    "are each present only on SOME of the tool's returns — "
                    "see optimization.verify_optimization's own `return` "
                    "statements — so none of them is required here; `ok`, "
                    "`accepted`, `reason` and the embedded `verification` are "
                    "the ones every success path carries. A MEASUREMENT "
                    "failure (baseline or candidate timing failed, e.g. a "
                    "timeout) is `{ok: false, error: ..., code: ...}` with no "
                    "`accepted` key at all — that shape already matches "
                    "`rejected` above and is deliberately NOT modeled here, "
                    "so the two branches cannot both match the same result."
                ),
                "type": "object",
                "required": ["ok", "accepted", "reason", "verification",
                             "grade", "grade_basis", "grade_rules_version"],
                "properties": {
                    "ok": {"type": "boolean"},
                    "accepted": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "detail": {
                        "type": "string",
                        "description": (
                            "Present only when accepted is false because the "
                            "correctness gate failed — speed was never "
                            "measured, because a faster wrong answer is not "
                            "an optimisation."
                        ),
                    },
                    "verification": {
                        "type": "object",
                        "description": (
                            "The embedded correctness check — verify_translation's "
                            "BARE return value, called directly rather than "
                            "through the graded MCP tool. No grade, no "
                            "contract_version: those are attached only at the "
                            "MCP tool boundary, which this internal call never "
                            "crosses."
                        ),
                        "required": list(TRANSLATION_EVIDENCE_KEYS),
                        "properties": _translation_evidence_properties(),
                    },
                    "speedup": _speedup_properties(),
                    "inference": _inference_properties(),
                    "min_speedup": {"type": "number"},
                    "language": {"type": "string"},
                    "grade": {"type": "string", "enum": sorted(grades.GRADES)},
                    "grade_basis": {"type": "string"},
                    "grade_rules_version": {"type": "string"},
                },
            },
            "edge_case_comparison": {
                "title": "a compare_edge_cases result",
                "description": (
                    "The `compare_edge_cases` tool's SUCCESS result: the same "
                    "logic run in N languages on edge-case inputs, with a "
                    "per-input matrix (`results`) and the subset that "
                    "diverged (`divergences`). Its own refusal — "
                    "`{ok: false, error: 'provide at least one ... snippet'}` "
                    "— already matches `rejected` above (ok + error, no "
                    "verdict) and is not modeled here; only the ok=true shape "
                    "needed a branch."
                ),
                "type": "object",
                "required": ["ok", "inputs", "languages", "divergence_count",
                             "results", "divergences"],
                "properties": {
                    "ok": {"type": "boolean"},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "languages": {"type": "array", "items": {"type": "string"}},
                    "divergence_count": {"type": "integer", "minimum": 0},
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["input", "runs"],
                            "properties": {
                                "input": {"type": "string"},
                                "runs": {
                                    "type": "object",
                                    "description": "language -> that language's run, keyed by the `snippets` dict's own keys.",
                                    "additionalProperties": _edge_case_run_properties(),
                                },
                            },
                        },
                    },
                    "divergences": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["input", "languages", "kind", "runs"],
                            "properties": {
                                "input": {"type": "string"},
                                "languages": {"type": "array", "items": {"type": "string"}},
                                "kind": {
                                    "type": "string", "enum": ["content", "order"],
                                    "description": (
                                        "content: the output (or ok-status) "
                                        "itself differs · order: same lines, "
                                        "same ok-status, different order."
                                    ),
                                },
                                "runs": {
                                    "type": "object",
                                    "description": "same shape as results[].runs, restricted to the diverging languages.",
                                },
                            },
                        },
                    },
                },
            },
            "comparison_rows": {
                "title": "a compare_execution result",
                "description": (
                    "The `compare_execution` tool's result: the same code run "
                    "in N languages side by side (`results`), the fastest "
                    "language that actually succeeded (`fastest`), and any "
                    "cross-language discrepancies noticed along the way "
                    "(`discrepancies` — e.g. one language produced output "
                    "while a sibling silently produced none). `compare_"
                    "execution` has no refusal shape of its own the way "
                    "`compare_edge_cases` does: an empty `snippets` dict "
                    "still returns this shape, with `count: 0`.\n\n"
                    "`ok` is always `true` once the comparison ran — it means "
                    "\"this tool produced a real table\", never \"every "
                    "language succeeded\"; read each row's OWN `ok` for that, "
                    "and a row's `code`/`error`/`remedy` (new in 1.10.0 — see "
                    "`_comparison_row_properties`/`errors.stamp_row`) for "
                    "WHY a row that needed a runtime this host does not have, "
                    "or timed out, failed. An ordinary program failure (a "
                    "real RTE/OLE `exit_code`) carries none of the three, "
                    "matching the INTENDED reading that `code` means a "
                    "failed REQUEST, not a failed program — a reading the "
                    "top-level execute_code envelope does not yet honour "
                    "itself for a plain RTE/timeout (those come back "
                    "`code: \"internal\"` there today; pre-existing, "
                    "tracked separately, not something this shape changes)."
                ),
                "type": "object",
                "required": ["ok", "count", "succeeded", "results", "fastest",
                             "fastest_note", "discrepancies"],
                "properties": {
                    "ok": {"const": True},
                    "count": {"type": "integer", "minimum": 0},
                    "succeeded": {
                        "type": "integer", "minimum": 0,
                        "description": "How many rows both ran AND reported a duration — see `fastest`'s own docstring for why duration is part of this count.",
                    },
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["language", "ok", "stdout", "stderr",
                                         "exit_code", "duration_ms", "timed_out",
                                         "cold_retry"],
                            "properties": _comparison_row_properties(),
                        },
                    },
                    "fastest": {
                        "type": ["string", "null"],
                        "description": "The language with the lowest duration_ms among rows that both succeeded and reported one; null when none did.",
                    },
                    "fastest_note": {
                        "type": ["string", "null"],
                        "description": "Non-null exactly when `fastest` is null, naming why.",
                    },
                    "discrepancies": {
                        "type": "array",
                        "description": (
                            "Always present, empty when there is nothing to "
                            "disclose. Two shapes share this array — a "
                            "still-timed-out-after-retry row (`timed_out: "
                            "true`, `sibling_durations_ms`, `variance_note`) "
                            "and a silent-output row (`issue` names 'no "
                            "stdout while another language produced some') "
                            "— deliberately left as loosely-typed objects "
                            "rather than a second internal oneOf: this is a "
                            "diagnostic aid, not a value a caller is expected "
                            "to branch structurally on."
                        ),
                        "items": {
                            "type": "object",
                            "required": ["language", "issue"],
                            "properties": {
                                "language": {"type": "string"},
                                "issue": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    }


#: Built once. The schema is a constant; rebuilding it per call would only add
#: a way for two callers to see different documents.
RESULT_SCHEMA = build_schema()


def build_doctor_schema(dialect: str | None = None, schema_id: str | None = None) -> dict:
    """The schema for `codecalc doctor --json`.

    Under THIS contract's version and policy rather than a third number of its
    own. The doctor payload is machine-readable output a client programs
    against, which is exactly what the policy in docs/contract/README.md is
    for; a separate `doctor_schema_version` would mean two documents to keep in
    step, two deprecation windows, and two chances to forget one.

    `runtimes[].status` is a CLOSED enum, so by that policy adding a fifth state
    is a MAJOR change — the same rule `code` and `verdict` follow, for the same
    reason: a strictly validating client rejects an unknown member before any
    application logic can degrade it.
    """
    from .doctor import RUNTIME_STATES
    from .registry import RELIABILITY_TIERS

    identifiers = {}
    if dialect:
        identifiers["$schema"] = dialect
    if schema_id:
        identifiers["$id"] = schema_id
    return {
        **identifiers,
        "title": "codecalc doctor report",
        "description": (
            f"`codecalc doctor --json`, version {CONTRACT_VERSION} — the same "
            "contract version and policy as the execution result. Generated "
            "from codecalc/contract.py by scripts/check_contract.py."
        ),
        "type": "object",
        "required": ["contract_version", "codecalc_version", "healthy", "python",
                     "backend", "execution_providers", "strict_runtime",
                     "install_sandbox", "extras", "grammar_cache",
                     "status_basis",
                     "runtimes", "runtime_summary", "tier_summary",
                     "workspace", "remedies"],
        "properties": {
            "contract_version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
            "codecalc_version": {"type": "string"},
            "healthy": {
                "type": "boolean",
                "description": (
                    "Deliberately NARROW, and the process exit code follows it: "
                    "true when a workspace is writable, a backend resolved, and "
                    "no `tested`-tier runtime is `unhealthy`. A missing optional "
                    "extra or an uninstalled runtime — of ANY tier — is a fact "
                    "about the host, not a fault, and must not fail an install "
                    "check; a `tested`-tier runtime that RESOLVED and then "
                    "proved broken (a non-executable file, or a failed --deep "
                    "probe) is codecalc's own advertised guarantee failing, and "
                    "does."
                ),
            },
            "python": {
                "type": "object", "required": ["version", "platform"],
                "properties": {"version": {"type": "string"},
                               "platform": {"type": "string"}},
            },
            "backend": {
                "type": "object", "required": ["kind", "binary", "detail"],
                "properties": {
                    "kind": {"type": "string", "enum": list(BACKENDS)},
                    "binary": {"type": ["string", "null"]},
                    "detail": {"type": ["string", "null"]},
                },
            },
            "execution_providers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["interface_version", "provider_id",
                                 "provider_version", "host_class", "strict",
                                 "capabilities", "ready", "technology", "detail"],
                    "properties": {
                        "interface_version": {"type": "string"},
                        "provider_id": {"type": "string"},
                        "provider_version": {"type": "string"},
                        "host_class": {"type": "string"},
                        "strict": {"type": "boolean"},
                        "capabilities": {"type": "object"},
                        "ready": {"type": ["boolean", "null"]},
                        "technology": {"type": ["string", "null"]},
                        "detail": {"type": ["string", "null"]},
                    },
                    "additionalProperties": False,
                },
            },
            "strict_runtime": {
                "type": "object",
                "required": ["available", "docker_present", "cgroup_v2",
                             "runtime", "runtime_registered", "architecture",
                             "docker_version", "image", "image_present",
                             "canary", "detail"],
                "description": (
                    "The gVisor strict-execution boundary's MEASURED "
                    "prerequisites. Doctor calls the same host probe "
                    "the runtime uses. `available` is fail-closed: false with a "
                    "named `detail` on any host that cannot prove the boundary "
                    "(no Docker, no cgroup v2, no runsc, or no executor image) — "
                    "a fact about the host, never a broken install. `canary` is "
                    "null unless --deep, when it launches the image under runsc "
                    "and confirms the runtime OUT OF BAND (docker inspect), not "
                    "from anything the workload printed."
                ),
                "properties": {
                    "available": {"type": "boolean"},
                    "docker_present": {"type": "boolean"},
                    "cgroup_v2": {"type": "boolean"},
                    "runtime": {"type": "string"},
                    "runtime_registered": {"type": "boolean"},
                    "architecture": {"type": ["string", "null"]},
                    "docker_version": {"type": ["string", "null"]},
                    "image": {"type": ["string", "null"]},
                    "image_present": {"type": ["boolean", "null"]},
                    "canary": {
                        "type": ["object", "null"],
                        "required": ["attempted", "ran", "runtime_observed",
                                     "verified_runsc", "detail"],
                        "properties": {
                            "attempted": {"type": "boolean"},
                            "ran": {"type": "boolean"},
                            "runtime_observed": {"type": ["string", "null"]},
                            "verified_runsc": {"type": "boolean"},
                            "detail": {"type": ["string", "null"]},
                        },
                    },
                    "detail": {"type": ["string", "null"]},
                },
            },
            "install_sandbox": {
                "type": "object", "required": ["landlock_abi", "confined", "detail"],
                "properties": {"landlock_abi": {"type": ["integer", "null"]},
                               "confined": {"type": "boolean"},
                               "detail": {"type": ["string", "null"]}},
            },
            "grammar_cache": {
                "type": "object",
                "required": ["extra_installed", "cached", "path", "grammars",
                             "detail"],
                "description": (
                    "tree-sitter grammars are NOT in the wheel: the pack fetches "
                    "each one on first use, in-process, into a local cache. "
                    "Reported so an offline or egress-restricted "
                    "install can see it BEFORE analyze_complexity degrades to "
                    "regex-fallback. `cached: false` is not a fault."
                ),
                "properties": {
                    "extra_installed": {"type": "boolean"},
                    "cached": {
                        "type": "boolean",
                        "description": (
                            "At least one grammar is on disk. Derived from a "
                            "COUNT, not from the directory existing — the pack "
                            "creates the directory before downloading anything, "
                            "so existence alone reports a warm cache that is not."
                        ),
                    },
                    "path": {"type": ["string", "null"]},
                    "grammars": {"type": "integer", "minimum": 0},
                    "detail": {"type": ["string", "null"]},
                },
            },
            "extras": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "installed", "missing", "remedy"],
                    "properties": {"name": {"type": "string"},
                                   "installed": {"type": "boolean"},
                                   "missing": {"type": "array",
                                               "items": {"type": "string"}},
                                   "remedy": {"type": ["string", "null"]}},
                },
            },
            "status_basis": {
                "type": "string", "enum": ["resolved", "executed"],
                "description": (
                    "Which measurement produced the runtime statuses. "
                    "'resolved' means the command was found on PATH and NOT "
                    "run, so no runtime can be 'available'. 'executed' means "
                    "--deep ran them. Without this a reader cannot tell a host "
                    "with nothing installed from a report that never looked."
                ),
            },
            "runtimes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "command", "status", "path", "version",
                                 "tier"],
                    "properties": {
                        "name": {"type": "string"},
                        "command": {"type": "string"},
                        "status": {
                            "type": "string", "enum": list(RUNTIME_STATES),
                            "description": (
                                "supported = codecalc knows it, nothing resolves "
                                "here · installed = resolves and is executable, "
                                "NOT run · unhealthy = resolves but cannot run, "
                                "was run (hello-world) and failed, or (under "
                                "--deep) its version probe never got an answer "
                                "(spawn failure/timeout) or exited non-zero on a "
                                "flag this code has confirmed correct for that "
                                "command — a nonzero exit on an UNCONFIRMED "
                                "flag guess is reported as merely unmeasured, "
                                "never unhealthy · available = actually "
                                "executed here and answered (--deep only)"
                            ),
                        },
                        "path": {"type": ["string", "null"]},
                        "version": {
                            "type": ["string", "null"],
                            "description": (
                                "the runtime's own version string, read by "
                                "running it with --version. null means NOT "
                                "MEASURED — no --deep, no version flag, or an "
                                "unreadable answer — never 'no version', and "
                                "NEVER a failed probe's error text; see "
                                "`probe_error` for that."
                            ),
                        },
                        "tier": {
                            "type": "string", "enum": list(RELIABILITY_TIERS),
                            "description": (
                                "RELIABILITY, orthogonal to `status` "
                                "above: `status` is what THIS machine just "
                                "resolved or ran; `tier` is how much codecalc's "
                                "own CI has verified this language, project-"
                                "wide. tested = a CI job genuinely executes it "
                                "and checks its output, every PR · best_effort "
                                "= declared, plausibly works, never CI-checked "
                                "· plan_only = never validated anywhere. A row "
                                "can read status=installed and tier=best_effort "
                                "at once — resolution says nothing about "
                                "whether the toolchain actually works."
                            ),
                        },
                        "detail": {"type": "string"},
                        "probe_error": {
                            "type": "string",
                            "description": (
                                "present whenever a --deep version probe ran "
                                "and failed (a nonzero exit, a timeout, or a "
                                "spawn error), whether or not that failure was "
                                "trusted enough to move `status` — e.g. the "
                                "text Apple's java stub prints when no JDK is "
                                "installed. Its PRESENCE does not by itself "
                                "mean `unhealthy`: a nonzero exit on a version "
                                "flag this code has not confirmed is correct "
                                "for that command is recorded here but leaves "
                                "`status` at `installed`, since the failure "
                                "may be a wrong flag guess rather than the "
                                "runtime. Distinct from `version`, which never "
                                "carries this text; `detail` carries a "
                                "one-line human summary when the failure WAS "
                                "trusted enough to demote the row."
                            ),
                        },
                    },
                },
            },
            "runtime_summary": {
                "type": "object",
                "required": list(RUNTIME_STATES),
                "properties": {s: {"type": "integer", "minimum": 0}
                               for s in RUNTIME_STATES},
            },
            "tier_summary": {
                "type": "object",
                "description": (
                    "The RELIABILITY_TIERS mirror of runtime_summary "
                    "above: counts by how much CI has verified each language, "
                    "not by what this host just resolved."
                ),
                "required": list(RELIABILITY_TIERS),
                "properties": {t: {"type": "integer", "minimum": 0}
                              for t in RELIABILITY_TIERS},
            },
            "workspace": {
                "type": "object", "required": ["path", "writable", "error"],
                "properties": {"path": {"type": "string"},
                               "writable": {"type": "boolean"},
                               "error": {"type": ["string", "null"]}},
            },
            "skill_file": {"type": ["string", "null"]},
            "remedies": {"type": "array", "items": {"type": "string"}},
        },
    }


def stamp(result: dict) -> dict:
    """Attach `contract_version` to a result. Mutates and returns it.

    Applied at the single point in `executor.execute` that both backends pass
    through, for the same reason `backend` is attached there: it is the only
    place that can make the two agree without editing either of them.

    Non-dict results are returned untouched rather than raising. A caller that
    somehow has a non-dict here has a worse problem than a missing version, and
    turning it into an exception inside the executor would replace a readable
    failure with an unreadable one.
    """
    if isinstance(result, dict):
        result.setdefault("contract_version", CONTRACT_VERSION)
    return result
