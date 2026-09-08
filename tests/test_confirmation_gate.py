"""The protocol-level confirmation gate on install_package/update_runtimes
(codecalc/confirmation.py), over a real MCP connection.

Before this, the ONLY signal that either tool was side-effecting was
`anthropic/requiresUserInteraction` in `_meta` — a hint one specific client
reads, that never reached the wire as an actual question. This asserts the
replacement holds on both protocol generations, and that the ONE case this
module deliberately does not gate (a legacy client that never declared the
elicitation capability) is genuinely unchanged.

Nothing here installs a real package or updates a real toolchain:
`packages.subprocess.run` is stubbed (same pattern tests/test_package_
allowlist.py uses) so a `confirm: true` round can be asserted to have run the
installer WITHOUT actually running one, and the `update_runtimes` case uses
`languages="notalanguage"` (tests/test_runtimes_mcp.py's own no-side-effect
probe for `apply=True`) so confirming it triggers no real command either.

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _mcp_client import data, in_process
from mcp import Client
from mcp.types import ElicitResult

from codecalc import audit as audit_module
from codecalc import errors, packages, server

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


class _FakeCompleted:
    def __init__(self):
        self.returncode = 0
        self.stdout = "Successfully installed six\n"
        self.stderr = ""


def _stub_run(calls):
    def _run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()
    return _run


def _events(log: audit_module.AuditLog) -> list[dict]:
    import json
    path = pathlib.Path(log.path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def main() -> None:
    import tempfile

    calls: list[list[str]] = []
    real_run = packages.subprocess.run
    packages.subprocess.run = _stub_run(calls)
    real_audit = server._audit_log
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-confirm-"))
    try:
        # ══════════════════════════════════════════════════════════════════
        # (a) 2026-07-28 client — the multi-round-trip flow
        # ══════════════════════════════════════════════════════════════════
        args = {"language": "python3", "package": "six"}

        # ── round one: the schema/prompt shape ──────────────────────────
        server._audit_log = audit_module.AuditLog(tmp / "a1.log")
        async with in_process() as client:
            check("negotiated protocol is 2026-07-28",
                  str(client.protocol_version) == "2026-07-28", f"-> {client.protocol_version}")
            first = await client.session.call_tool("install_package", args, allow_input_required=True)
            check("first call returns an input_required result",
                  type(first).__name__ == "InputRequiredResult", f"-> {type(first).__name__}")
            check("input_required carries resultType", getattr(first, "result_type", None) == "input_required")
            reqs = first.input_requests or {}
            check("exactly one embedded input request", len(reqs) == 1, f"-> {list(reqs)}")
            key = next(iter(reqs))
            question = reqs[key]
            params = question.params
            check("the question echoes the package and language",
                  "six" in params.message and "python3" in params.message, f"-> {params.message!r}")
            schema = params.requested_schema
            check("the schema asks for a boolean 'confirm'",
                  schema.get("properties", {}).get("confirm", {}).get("type") == "boolean", f"-> {schema}")
            check("'confirm' is required", "confirm" in (schema.get("required") or []), f"-> {schema}")
            check("no side effect from round one alone", not calls)
            check("first round is not itself an audit decision (asked, not decided)",
                  not _events(server._audit_log))
        state = first.request_state

        # ── retry with confirm=true: performs the operation ─────────────
        calls.clear()
        server._audit_log = audit_module.AuditLog(tmp / "a2.log")
        async with in_process() as client:
            responses = {key: ElicitResult(action="accept", content={"confirm": True})}
            second = await client.session.call_tool(
                "install_package", args, input_responses=responses, request_state=state,
                allow_input_required=True,
            )
            check("confirm=true is not another input_required round",
                  type(second).__name__ != "InputRequiredResult", f"-> {type(second).__name__}")
            payload = data(second)
            check("confirm=true actually installs", payload.get("ok") is True, f"-> {payload}")
        check("confirm=true called the (stubbed) installer exactly once", len(calls) == 1, f"-> {calls}")
        granted = [e for e in _events(server._audit_log) if e["event_type"] == audit_module.CONFIRMATION_GRANTED]
        check("confirm=true logs exactly one confirmation_granted event", len(granted) == 1, f"-> {_events(server._audit_log)}")
        if granted:
            check("the event names the tool and echoes the package",
                  granted[0].get("tool") == "install_package" and granted[0].get("package") == "six",
                  f"-> {granted[0]}")

        # ── retry with confirm=false: refuses, no side effect ────────────
        calls.clear()
        server._audit_log = audit_module.AuditLog(tmp / "a3.log")
        async with in_process() as client:
            first_b = await client.session.call_tool("install_package", args, allow_input_required=True)
            key_b = next(iter(first_b.input_requests))
            responses = {key_b: ElicitResult(action="accept", content={"confirm": False})}
            second_b = await client.session.call_tool(
                "install_package", args, input_responses=responses, request_state=first_b.request_state,
                allow_input_required=True,
            )
            payload = data(second_b)
            check("confirm=false refuses with permission_denied",
                  payload.get("ok") is False and payload.get("code") == errors.PERMISSION_DENIED,
                  f"-> {payload}")
        check("confirm=false never calls the installer", not calls, f"-> {calls}")
        declined = [e for e in _events(server._audit_log) if e["event_type"] == audit_module.CONFIRMATION_REFUSED]
        check("confirm=false logs a confirmation_refused/declined event",
              any(e.get("decision") == "declined" for e in declined), f"-> {declined}")

        # ── missing response on retry: refuses, no side effect ───────────
        calls.clear()
        server._audit_log = audit_module.AuditLog(tmp / "a4.log")
        async with in_process() as client:
            first_c = await client.session.call_tool("install_package", args, allow_input_required=True)
            third = await client.session.call_tool(
                "install_package", args, input_responses=None, request_state=first_c.request_state,
                allow_input_required=True,
            )
            payload = data(third)
            check("a missing confirmation response refuses (not another round)",
                  type(third).__name__ != "InputRequiredResult", f"-> {type(third).__name__}")
            check("  ...with a validation code", payload.get("code") == errors.VALIDATION, f"-> {payload}")
        check("a missing response never calls the installer", not calls, f"-> {calls}")

        # ── malformed content on retry: refuses, no side effect ──────────
        calls.clear()
        server._audit_log = audit_module.AuditLog(tmp / "a5.log")
        async with in_process() as client:
            first_d = await client.session.call_tool("install_package", args, allow_input_required=True)
            key_d = next(iter(first_d.input_requests))
            bad = {key_d: ElicitResult(action="accept", content={"nope": True})}
            fourth = await client.session.call_tool(
                "install_package", args, input_responses=bad, request_state=first_d.request_state,
                allow_input_required=True,
            )
            payload = data(fourth)
            check("content missing 'confirm' refuses with validation",
                  payload.get("ok") is False and payload.get("code") == errors.VALIDATION, f"-> {payload}")
        check("malformed content never calls the installer", not calls, f"-> {calls}")
        malformed = [e for e in _events(server._audit_log) if e["event_type"] == audit_module.CONFIRMATION_REFUSED]
        check("malformed content logs a confirmation_refused/malformed event",
              any(e.get("decision") == "malformed" for e in malformed), f"-> {malformed}")

        # ══════════════════════════════════════════════════════════════════
        # (b) legacy (2025-11-25) client WITH elicitation capability
        # ══════════════════════════════════════════════════════════════════
        async def _accept(context, params):
            return ElicitResult(action="accept", content={"confirm": True})

        async def _decline(context, params):
            return ElicitResult(action="decline")

        calls.clear()
        server._audit_log = audit_module.AuditLog(tmp / "b1.log")
        async with Client(server.mcp, mode="legacy", elicitation_callback=_accept) as client:
            check("negotiated protocol is 2025-11-25 (legacy)",
                  str(client.protocol_version) == "2025-11-25", f"-> {client.protocol_version}")
            r = await client.call_tool("install_package", args)
            payload = data(r)
            check("legacy + elicitation + accept installs", payload.get("ok") is True, f"-> {payload}")
        check("legacy accept called the installer once", len(calls) == 1, f"-> {calls}")
        granted_b = [e for e in _events(server._audit_log) if e["event_type"] == audit_module.CONFIRMATION_GRANTED]
        check("legacy accept logs confirmation_granted", len(granted_b) == 1, f"-> {_events(server._audit_log)}")

        calls.clear()
        server._audit_log = audit_module.AuditLog(tmp / "b2.log")
        async with Client(server.mcp, mode="legacy", elicitation_callback=_decline) as client:
            r = await client.call_tool("install_package", args)
            payload = data(r)
            check("legacy + elicitation + decline refuses with permission_denied",
                  payload.get("ok") is False and payload.get("code") == errors.PERMISSION_DENIED,
                  f"-> {payload}")
        check("legacy decline never calls the installer", not calls, f"-> {calls}")
        declined_b = [e for e in _events(server._audit_log) if e["event_type"] == audit_module.CONFIRMATION_REFUSED]
        check("legacy decline logs confirmation_refused/declined",
              any(e.get("decision") == "declined" for e in declined_b), f"-> {declined_b}")

        # ══════════════════════════════════════════════════════════════════
        # (c) legacy client WITHOUT elicitation capability — UNCHANGED
        # ══════════════════════════════════════════════════════════════════
        calls.clear()
        server._audit_log = audit_module.AuditLog(tmp / "c1.log")
        async with Client(server.mcp, mode="legacy") as client:
            r = await client.call_tool("install_package", args)
            payload = data(r)
            check("legacy client with no elicitation capability still installs (unchanged behaviour)",
                  payload.get("ok") is True, f"-> {payload}")
        check("  ...and the installer really ran, unconfirmed", len(calls) == 1, f"-> {calls}")
        skipped = [e for e in _events(server._audit_log) if e["event_type"] == audit_module.CONFIRMATION_GATE_SKIPPED]
        check("  ...recorded as a skipped gate, not a confirmation",
              len(skipped) == 1 and skipped[0].get("decision") == "unavailable", f"-> {skipped}")

        # ══════════════════════════════════════════════════════════════════
        # update_runtimes(apply=True) — the same gate, a second tool
        # ══════════════════════════════════════════════════════════════════
        server._audit_log = audit_module.AuditLog(tmp / "d1.log")
        async with in_process() as client:
            first_u = await client.session.call_tool(
                "update_runtimes", {"languages": "notalanguage", "apply": True}, allow_input_required=True,
            )
            check("update_runtimes(apply=True) is gated the same way",
                  type(first_u).__name__ == "InputRequiredResult", f"-> {type(first_u).__name__}")
            key_u = next(iter(first_u.input_requests))
            responses = {key_u: ElicitResult(action="accept", content={"confirm": True})}
            second_u = await client.session.call_tool(
                "update_runtimes", {"languages": "notalanguage", "apply": True},
                input_responses=responses, request_state=first_u.request_state, allow_input_required=True,
            )
            payload = data(second_u)
            check("confirmed apply=True still runs (and reports the unknown language, no real update)",
                  payload.get("ok") is False and payload.get("unknown") == ["notalanguage"], f"-> {payload}")

        server._audit_log = audit_module.AuditLog(tmp / "d2.log")
        async with in_process() as client:
            r = await client.session.call_tool("update_runtimes", {"languages": "gradle,swift"})
            payload = data(r)
            check("update_runtimes(apply=False) is never gated (dry run)",
                  payload.get("dry_run") is True, f"-> {payload}")
    finally:
        packages.subprocess.run = real_run
        server._audit_log = real_audit


asyncio.run(main())
print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== CONFIRMATION GATE OK ===")
sys.exit(1 if FAILS else 0)
