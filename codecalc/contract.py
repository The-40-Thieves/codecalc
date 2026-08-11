"""The published result contract: one version number, one schema (THE-781).

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

from . import errors

#: The contract's own version, semver. See docs/contract/README.md for what
#: each component is allowed to change — the short form is that MAJOR is the
#: only one that may break a reader, and it carries a twelve-month deprecation
#: window before anything is removed.
CONTRACT_VERSION = "1.0.0"

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
        # FOUR shapes, discriminated so that exactly one branch can match:
        #
        #   envelope  verdict present, backend in (rust, python)
        #   session   backend == "session-worker"
        #   compact   verdict present, NO backend key
        #   rejected  no verdict at all
        #
        # The first version of this schema had two branches and asserted that
        # `executor.execute` was the single choke point for every execution
        # result. Cross-vendor review disproved it in three places at once:
        # `compact_result` rebuilds a four-key result, the native streaming path
        # returns raw executor JSON, and sessions bypass the function entirely.
        # All three produced results that NEITHER branch accepted — a published
        # document that the product fails, which is worse than no document.
        "oneOf": [
            {"$ref": "#/$defs/execution_envelope"},
            {"$ref": "#/$defs/session_result"},
            {"$ref": "#/$defs/compact_result"},
            {"$ref": "#/$defs/rejected"},
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
        },
    }


#: Built once. The schema is a constant; rebuilding it per call would only add
#: a way for two callers to see different documents.
RESULT_SCHEMA = build_schema()


def build_doctor_schema(dialect: str | None = None, schema_id: str | None = None) -> dict:
    """The schema for `codecalc doctor --json` (THE-780).

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
                     "backend", "install_sandbox", "extras", "status_basis",
                     "runtimes", "runtime_summary", "workspace", "remedies"],
        "properties": {
            "contract_version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
            "codecalc_version": {"type": "string"},
            "healthy": {
                "type": "boolean",
                "description": (
                    "Deliberately NARROW, and the process exit code follows it: "
                    "true when a workspace is writable and a backend resolved. A "
                    "missing optional extra or an uninstalled runtime is a fact "
                    "about the host, not a fault, and must not fail an install "
                    "check."
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
            "install_sandbox": {
                "type": "object", "required": ["landlock_abi", "confined", "detail"],
                "properties": {"landlock_abi": {"type": ["integer", "null"]},
                               "confined": {"type": "boolean"},
                               "detail": {"type": ["string", "null"]}},
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
                    "required": ["name", "command", "status", "path"],
                    "properties": {
                        "name": {"type": "string"},
                        "command": {"type": "string"},
                        "status": {
                            "type": "string", "enum": list(RUNTIME_STATES),
                            "description": (
                                "supported = codecalc knows it, nothing resolves "
                                "here · installed = resolves and is executable, "
                                "NOT run · unhealthy = resolves but cannot run, "
                                "or was run and failed · available = actually "
                                "executed here and answered (--deep only)"
                            ),
                        },
                        "path": {"type": ["string", "null"]},
                        "detail": {"type": "string"},
                    },
                },
            },
            "runtime_summary": {
                "type": "object",
                "required": list(RUNTIME_STATES),
                "properties": {s: {"type": "integer", "minimum": 0}
                               for s in RUNTIME_STATES},
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
