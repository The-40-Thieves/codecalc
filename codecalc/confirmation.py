"""Protocol-aware confirmation gate for side-effecting admin tools.

`install_package` and `update_runtimes(apply=True)` used to carry only
`anthropic/requiresUserInteraction: true` in `_meta` — a hint one specific
client (Claude Code) reads to force its own permission prompt. Any other
client, or that client with the hint stripped, called straight through with
no confirmation at all. This module is the protocol-level replacement: the
gate lives in the MCP exchange itself, not in a client-side convention.

TWO PROTOCOL GENERATIONS, TWO MECHANISMS, ONE OUTCOME
The pinned SDK (`mcp>=2.0,<3`) exposes confirmation differently depending on
what the connection negotiated (`ctx.protocol_version`):

  >= 2026-07-28  the multi-round-trip (MRTR) flow, SEP-2322. A tool returns
                 `InputRequiredResult(input_requests=..., request_state=...)`
                 instead of its normal result; the client fulfils the
                 embedded question and RETRIES THE SAME `tools/call` carrying
                 `inputResponses`/`requestState`. `ctx.input_responses` and
                 `ctx.request_state` are `None` on that first round and
                 populated on the retry — that is the only state this module
                 threads across rounds, and it round-trips through the
                 client, not through any process-local store. `request_state`
                 itself is sealed/verified by the SDK's own
                 `RequestStateBoundary` middleware (mcp.server.request_state)
                 before this module ever sees it, so the opaque string minted
                 below needs no cryptographic care of its own.

  <  2026-07-28  no MRTR: elicitation is a standalone `elicitation/create`
                 server-to-client request sent mid-call and awaited in place
                 (`ctx.elicit`). This works ONLY if the client declared the
                 elicitation capability during its handshake; a legacy client
                 that never declared it cannot be asked at all.

FALLBACK: A LEGACY CLIENT WITH NO ELICITATION CAPABILITY
This is the one case this module does not gate. Asking would either hang (no
handler on the other end) or the SDK's own default callback answers
"Elicitation not supported" for us — neither is "the tool ran but nobody
confirmed", which is what the previous, `_meta`-only behaviour already was.
So `require_confirmation` returns `None` (proceed) exactly as it did before
this module existed, and the confirmation this call gets is whatever the
`_meta` hint's reader (if any) already provided. `_REQUIRES_USER_INTERACTION`
in server.py is kept for exactly these clients — removing it would regress
them from "a client-side prompt" to "no prompt at all".

WHAT A CALLER SEES
`require_confirmation` returns one of three shapes:

  `InputRequiredResult`  the tool must `return` this immediately (round one
                         of the MRTR flow; nothing has happened yet).
  a `dict`               a closed-enum refusal (`errors.error_result`) the
                         tool must `return` immediately; no side effect.
  `None`                 confirmed (or the fallback above applies) — proceed
                         with the real operation.

This lets every gated tool body read as:

    gate = await confirmation.require_confirmation(ctx, ...)
    if gate is not None:
        return gate
    <do the thing>

NO NINTH ERROR CODE
A declined or cancelled confirmation is `errors.PERMISSION_DENIED` — the
existing "refused by a jail, ACL or elevation gate" bucket already describes
an elevation the caller chose not to grant. A malformed or missing
confirmation response (the client retried without answering, or answered
with something that does not match the requested schema) is
`errors.VALIDATION` instead: it is a shape defect in the retry, not a
decision the user made, and `errors.py`'s taxonomy is closed by design (see
that module's docstring) — inventing a tenth bucket for "declined" alone was
never on the table.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    render_elicitation_schema,
)
from mcp.server.mcpserver import Context
from mcp.types import (
    ClientCapabilities,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
)
from mcp_types.version import is_version_at_least
from pydantic import BaseModel, Field

from . import audit as audit_module
from . import errors

#: First protocol revision whose `tools/call` carries a follow-up question
#: inside `InputRequiredResult` rather than as a standalone server-to-client
#: request. Pinned to the exact revision (not "latest"), matching how
#: `mcp/server/mcpserver/resolve.py` (the SDK's own resolver-based elicitation)
#: makes the same decision.
_MRTR_VERSION = "2026-07-28"

#: This module asks at most one question per call, so a fixed key never
#: collides — `input_requests`/`input_responses` are scoped to one `tools/call`
#: exchange, never shared across calls.
_WIRE_KEY = "confirm"

#: Opaque `request_state` minted for round one. Never decoded — its only job
#: is to be non-`None`, so `ctx.request_state is None` distinguishes "first
#: call" from "the client retried" without this module tracking anything
#: itself. The SDK's `RequestStateBoundary` seals/binds/verifies the actual
#: bytes on the wire; what travels in the plaintext this module sees is
#: whatever it minted, byte-exact.
_PENDING_STATE = "awaiting_confirmation"


class _ConfirmResponse(BaseModel):
    """The one field either protocol generation asks for.

    Rendered into the elicitation's `requestedSchema` via
    `render_elicitation_schema` (the same helper the SDK's own resolver-based
    elicitation uses), so the MRTR path and the legacy `ctx.elicit` path never
    describe the question two different ways.
    """

    confirm: bool = Field(
        description="True to proceed with the operation described above, false to cancel it."
    )


def _refusal(tool: str, reason: str) -> dict[str, Any]:
    """A closed-enum refusal result for a declined, cancelled, or malformed confirmation.

    `reason` is one of "declined", "cancelled", or "malformed" — see the
    module docstring's NO NINTH ERROR CODE section for why the first two
    share `PERMISSION_DENIED` and the third gets `VALIDATION` instead.
    """
    if reason == "malformed":
        return errors.error_result(
            errors.VALIDATION,
            f"{tool}: missing or malformed confirmation response",
        )
    return errors.error_result(errors.PERMISSION_DENIED, f"{tool}: confirmation {reason}")


def _record(
    audit_log: audit_module.AuditLog, *, event_type: str, decision: str,
    tool: str, session_id: str | None, echo: Mapping[str, Any],
) -> None:
    audit_log.emit(event_type, session_id=session_id, decision=decision, tool=tool, **echo)


def _supports_form_elicitation(capabilities: ClientCapabilities | None) -> bool:
    """True when a legacy (< 2026-07-28) client can be asked via `ctx.elicit`.

    Mirrors `mcp.server.mcpserver.resolve._require_capability`'s own check: a
    bare `elicitation: {}` (the only shape before elicitation modes existed)
    counts as form support, url-only does not.
    """
    elicitation = capabilities.elicitation if capabilities is not None else None
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def _mrtr_gate(
    ctx: Context, *, tool: str, message: str, echo: Mapping[str, Any],
    audit_log: audit_module.AuditLog, session_id: str | None,
) -> InputRequiredResult | dict[str, Any] | None:
    """The >= 2026-07-28 half of `require_confirmation`. Synchronous: round one
    only builds and returns a result, and round two only reads what the
    framework already delivered — neither needs to await anything."""
    if ctx.request_state is None:
        # Round one: nothing has been asked yet.
        schema = render_elicitation_schema(_ConfirmResponse)
        request = ElicitRequest(params=ElicitRequestFormParams(message=message, requested_schema=schema))
        return InputRequiredResult(input_requests={_WIRE_KEY: request}, request_state=_PENDING_STATE)

    response = (ctx.input_responses or {}).get(_WIRE_KEY)
    if not isinstance(response, ElicitResult):
        _record(audit_log, event_type=audit_module.CONFIRMATION_REFUSED, decision="malformed",
                tool=tool, session_id=session_id, echo=echo)
        return _refusal(tool, "malformed")

    if response.action == "accept":
        confirm = (response.content or {}).get("confirm")
        if confirm is True:
            _record(audit_log, event_type=audit_module.CONFIRMATION_GRANTED, decision="confirmed",
                    tool=tool, session_id=session_id, echo=echo)
            return None
        if confirm is False:
            _record(audit_log, event_type=audit_module.CONFIRMATION_REFUSED, decision="declined",
                    tool=tool, session_id=session_id, echo=echo)
            return _refusal(tool, "declined")
        # Accepted, but `confirm` is missing or not a bool — the response does
        # not match the schema that was asked for.
        _record(audit_log, event_type=audit_module.CONFIRMATION_REFUSED, decision="malformed",
                tool=tool, session_id=session_id, echo=echo)
        return _refusal(tool, "malformed")

    decision = "cancelled" if response.action == "cancel" else "declined"
    _record(audit_log, event_type=audit_module.CONFIRMATION_REFUSED, decision=decision,
            tool=tool, session_id=session_id, echo=echo)
    return _refusal(tool, decision)


async def _legacy_gate(
    ctx: Context, *, tool: str, message: str, echo: Mapping[str, Any],
    audit_log: audit_module.AuditLog, session_id: str | None,
) -> dict[str, Any] | None:
    """The < 2026-07-28 half of `require_confirmation`."""
    if not _supports_form_elicitation(ctx.client_capabilities):
        # FALLBACK — see the module docstring. The tool proceeds exactly as
        # it did before this module existed, `_meta` hint and all; still
        # recorded, so the trail can tell "nobody was asked" apart from
        # "asked and confirmed" rather than reading the two the same way.
        _record(audit_log, event_type=audit_module.CONFIRMATION_GATE_SKIPPED, decision="unavailable",
                tool=tool, session_id=session_id, echo=echo)
        return None

    try:
        result = await ctx.elicit(message, _ConfirmResponse)
    except ValueError:
        # elicit_with_validation raises when the client accepted without
        # content, or with content that fails the requested schema.
        _record(audit_log, event_type=audit_module.CONFIRMATION_REFUSED, decision="malformed",
                tool=tool, session_id=session_id, echo=echo)
        return _refusal(tool, "malformed")

    if isinstance(result, AcceptedElicitation):
        if result.data.confirm:
            _record(audit_log, event_type=audit_module.CONFIRMATION_GRANTED, decision="confirmed",
                    tool=tool, session_id=session_id, echo=echo)
            return None
        _record(audit_log, event_type=audit_module.CONFIRMATION_REFUSED, decision="declined",
                tool=tool, session_id=session_id, echo=echo)
        return _refusal(tool, "declined")

    # Only `DeclinedElicitation`/`CancelledElicitation` remain — `AcceptedElicitation`
    # was handled above.
    decision = "cancelled" if isinstance(result, CancelledElicitation) else "declined"
    _record(audit_log, event_type=audit_module.CONFIRMATION_REFUSED, decision=decision,
            tool=tool, session_id=session_id, echo=echo)
    return _refusal(tool, decision)


async def require_confirmation(
    ctx: Context | None, *, tool: str, message: str, echo: Mapping[str, Any],
    audit_log: audit_module.AuditLog, session_id: str | None = None,
) -> InputRequiredResult | dict[str, Any] | None:
    """Gate a side-effecting admin tool behind an explicit user confirmation.

    `tool`: the tool's registered name, for the message and the audit trail.
    `message`: the human-readable prompt, already describing what will run —
        callers compose this themselves (it is the "echo of what will be
        installed/applied" the confirmation asks about) rather than this
        module templating it, since the right phrasing differs per tool.
    `echo`: structured fields (package name, language, ...) attached to the
        audit event alongside the decision — never a credential or output,
        matching every other event `audit.AuditLog.emit` records.
    `session_id`: threaded through to the audit event; `None` for an ad-hoc
        (sessionless) call, same convention `packages.install`'s own audit
        calls use.

    Returns `None` (outside an active MCP request — see the module docstring)
    when `ctx` is `None`: a direct Python call with no client to ask behaves
    exactly as it did before this gate existed.
    """
    if ctx is None:
        return None
    protocol_version = ctx.protocol_version
    if protocol_version is not None and is_version_at_least(protocol_version, _MRTR_VERSION):
        return _mrtr_gate(ctx, tool=tool, message=message, echo=echo,
                          audit_log=audit_log, session_id=session_id)
    return await _legacy_gate(ctx, tool=tool, message=message, echo=echo,
                              audit_log=audit_log, session_id=session_id)
