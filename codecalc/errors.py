"""Stable error codes for the result contract (THE-781).

Every failing tool result carried a free-form English sentence and nothing
else — 121 distinct `"error":` strings across the package. A caller could show
them to a human and could do nothing else with them: "unknown language 'foo'",
"expression too long", "uv not found" and "session worker died" are four
different situations that a program has to tell apart by matching prose that
nobody promised to keep stable.

So this adds a `code` beside the message rather than replacing it. The message
stays human-facing and free to improve; the code is the part a caller may
branch on, and the part this module promises not to churn.

TWO MECHANISMS, AND THE SECOND IS TRANSITIONAL
`classify()` maps an EXCEPTION to a code and is the mechanism worth having: it
keys on the exception type, which the code that raised it chose deliberately.

That was going to be enough. It is not. The first draft attached it only to
`guarded_call`, on the assumption that symbolic tools raise and it catches
them. Measured against the eight most reachable failures — a bad expression,
an unknown language, an unknown session, a blown recursion ceiling — it
reached ZERO of them, because those paths `return {"ok": False, ...}` directly
and never raise. An assumption about control flow, disproved by running it.

So `ensure_code()` is the second mechanism: it fills in a code for a failing
result that has none, by matching the message. Message matching is fragile —
improve a sentence and the classification changes with it — so a code derived
that way is marked `code_inferred: true` and is NOT the same claim as one
attached at the raise site.

The distinction is the point. `scripts/check_claims.py` counts inferred codes
and floors the total, so the transitional half is visible and shrinking rather
than quietly permanent.
"""

from __future__ import annotations

#: The taxonomy. Values are the wire format and are API: renaming one is a
#: breaking change to the result contract, which is the whole point of having
#: them. Deliberately coarse — eight buckets a caller can act on differently,
#: not a catalogue of every way a call can fail.
VALIDATION = "validation"                    # the request was malformed or out of range
RUNTIME_UNAVAILABLE = "runtime_unavailable"  # a language/compiler is not installed here
TIMEOUT = "timeout"                          # a wall clock or CPU deadline expired
RESOURCE_EXHAUSTED = "resource_exhausted"    # memory, output or process ceiling hit
PERMISSION_DENIED = "permission_denied"      # refused by a jail, ACL or elevation gate
DEPENDENCY_MISSING = "dependency_missing"    # an optional extra is not installed
WORKER_FAILURE = "worker_failure"            # a session worker died or desynced
INTERNAL = "internal"                        # a defect here, not in the request

#: Every code, for the gate and for callers that want to validate.
ALL_CODES = frozenset({
    VALIDATION, RUNTIME_UNAVAILABLE, TIMEOUT, RESOURCE_EXHAUSTED,
    PERMISSION_DENIED, DEPENDENCY_MISSING, WORKER_FAILURE, INTERNAL,
})

#: What a caller can do about each, in one line. Returned by the MCP surface so
#: the remedy travels with the code instead of living only in documentation
#: somebody has to find.
REMEDIES = {
    VALIDATION: "fix the arguments and retry; the message names the field",
    RUNTIME_UNAVAILABLE: "install the runtime, or call list_languages to see what this host has",
    TIMEOUT: "raise `timeout`, or reduce the work; a partial result is not returned",
    RESOURCE_EXHAUSTED: "raise the relevant ceiling (max_memory_mb, max_output_kb) or reduce the work",
    PERMISSION_DENIED: "the path or operation is outside what this server permits; it will not succeed on retry",
    DEPENDENCY_MISSING: "install the named extra; the message gives the exact pip command",
    WORKER_FAILURE: "the session is unusable — call session_stop and start a new one",
    INTERNAL: "a defect in codecalc; the message is worth reporting verbatim",
}


def classify(exc: BaseException) -> str:
    """Map an exception to a code. Pure, and exception-type first.

    Ordering matters. `MissingExtra` subclasses `ImportError` on purpose (see
    optional.py), so it has to be tested BEFORE the ImportError branch or every
    absent extra reports as an internal defect — which is the opposite of
    actionable, since the caller can fix a missing extra and cannot fix a bug.

    Type before message, always. Matching on message text is how a classifier
    becomes wrong silently when someone improves a sentence, so `_from_message`
    below is the last resort and is only consulted for exception types that
    carry no useful class.
    """
    from .optional import MissingExtra

    if isinstance(exc, MissingExtra):
        return DEPENDENCY_MISSING
    if isinstance(exc, TimeoutError):
        return TIMEOUT
    if isinstance(exc, MemoryError):
        return RESOURCE_EXHAUSTED
    if isinstance(exc, RecursionError):
        # Depth is a resource here: the guard exists so a deeply nested
        # expression cannot take the process down, and the caller's remedy is
        # to simplify the input, same as any other ceiling.
        return RESOURCE_EXHAUSTED
    if isinstance(exc, PermissionError):
        return PERMISSION_DENIED
    if isinstance(exc, FileNotFoundError):
        # A missing executable is the common case on this path — a language
        # whose runtime is not installed. A missing FILE is validation, but the
        # executor reports that through its own result shape rather than here.
        return RUNTIME_UNAVAILABLE
    if isinstance(exc, ImportError):
        return DEPENDENCY_MISSING
    if isinstance(exc, (ValueError, TypeError, KeyError, IndexError, ArithmeticError)):
        return VALIDATION
    if isinstance(exc, OSError):
        return INTERNAL
    return _from_message(str(exc))


#: Message fragments consulted ONLY for exceptions with no informative type.
#: Ordered, first match wins, lowercase comparison.
#: The first version of this list had ten entries and classified four of the
#: seven most reachable failures as INTERNAL — including a bad expression and
#: an unknown session id. INTERNAL's remedy is "a defect in codecalc; the
#: message is worth reporting verbatim", so that told a caller who mistyped an
#: expression to go and file a bug. A wrong code is worse than a missing one,
#: because it is actionable in the wrong direction.
#:
#: INTERNAL is the fallback and should be RARE. If a common failure lands there,
#: the list is wrong, not the failure.
_MESSAGE_HINTS = (
    # resource ceilings — before the parse hints, because "expression too long"
    # is a ceiling and contains no parse vocabulary but sits near it
    ("timed out", TIMEOUT),
    ("timeout", TIMEOUT),
    ("memory ceiling", RESOURCE_EXHAUSTED),
    ("memory", RESOURCE_EXHAUSTED),
    ("too long", RESOURCE_EXHAUSTED),
    ("too deeply nested", RESOURCE_EXHAUSTED),
    ("too many", RESOURCE_EXHAUSTED),
    ("resource limit", RESOURCE_EXHAUSTED),
    ("exceeded", RESOURCE_EXHAUSTED),
    # dependency
    ("not installed", DEPENDENCY_MISSING),
    ("ships in the", DEPENDENCY_MISSING),
    # permission / jail
    ("permission", PERMISSION_DENIED),
    ("escapes", PERMISSION_DENIED),
    ("outside", PERMISSION_DENIED),
    ("refus", PERMISSION_DENIED),
    # worker / session lifecycle
    ("worker", WORKER_FAILURE),
    ("session is", WORKER_FAILURE),
    ("desync", WORKER_FAILURE),
    # runtime availability
    ("runtime unavailable", RUNTIME_UNAVAILABLE),
    ("not found in path", RUNTIME_UNAVAILABLE),
    # validation — the largest bucket, and the one INTERNAL was stealing
    ("unknown language", VALIDATION),
    ("unknown session", VALIDATION),
    ("no such file", VALIDATION),
    ("no such directory", VALIDATION),
    ("parse error", VALIDATION),
    ("could not parse", VALIDATION),
    ("could not be tokenised", VALIDATION),
    ("sympify", VALIDATION),
    ("unexpected character", VALIDATION),
    ("invalid", VALIDATION),
    ("must be", VALIDATION),
    ("empty", VALIDATION),
)


def _from_message(message: str) -> str:
    low = message.lower()
    for needle, code in _MESSAGE_HINTS:
        if needle in low:
            return code
    return INTERNAL


def error_result(code: str, message: str, **extra) -> dict:
    """The failing half of the result contract.

    `code` is validated rather than trusted: a typo'd code is worse than no
    code, because a caller branching on it silently takes the wrong branch and
    nothing raises. An unknown code becomes INTERNAL and says so in the
    message, which is a loud failure at the point of the mistake.
    """
    if code not in ALL_CODES:
        message = f"[unclassified code {code!r}] {message}"
        code = INTERNAL
    return {"ok": False, "code": code, "error": message,
            "remedy": REMEDIES[code], **extra}


def ensure_code(result: dict) -> dict:
    """Give a failing result a code if it has none. Mutates and returns it.

    The transitional half. Applied where every tool result passes on its way
    out, so the model-facing surface is covered without editing 121 return
    sites — but the code comes from the MESSAGE, which is a weaker claim than
    one chosen at the raise site, so it is labelled `code_inferred: true`.

    A caller can therefore tell the two apart, and so can the gate: the goal is
    for this function to have less to do over time, not to be the answer.

    Only touches results that are failing and codeless. A successful result
    gets nothing — a `code` on an `ok: True` payload would read as a warning
    and there is no such thing here.
    """
    if not isinstance(result, dict) or result.get("ok") is not False:
        return result
    if result.get("code"):
        return result
    code = _from_message(str(result.get("error") or ""))
    result["code"] = code
    result["remedy"] = REMEDIES[code]
    result["code_inferred"] = True
    return result
