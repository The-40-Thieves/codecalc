"""Append-only structured audit stream for security-relevant decisions (THE-787).

Before this, the provenance of a brokered denial or a refused install was
reconstructible only by correlating a result receipt with the run-supervisor
journal — nothing recorded the DECISION itself. This is that record: one JSON
line per event, appended to a file, never mutated.

WHAT IS AND IS NOT IN AN EVENT
An event carries only decision METADATA: an event type, an ISO-8601 timestamp,
the run/session id when there is one, the decision and its reason, and the
capability names involved. It never carries the executed source, stdin, output,
or any credential — those are the payload the audit trail exists to reason
ABOUT, not to duplicate. As a second line of defence, every configured secret
string is redacted from the serialized line before it is written, so a value
that should never appear cannot appear even if a caller passes one by mistake.

TIME IS INJECTED, NOT READ AT THE CALL SITE
`AuditLog` takes a `clock` callable (default `time.time`). Tests pass a fixed
clock and get a deterministic timestamp; nothing here reads the wall clock
implicitly. Same reasoning the run supervisor's journal follows.

BEST EFFORT, NEVER FATAL
An audit write must not be able to fail a run. Every append is wrapped: a full
disk or an unwritable directory drops the event (and says so on stderr once) but
the execution it describes still returns its result. The trail is provenance,
not a transaction log the product depends on.

CREATE-ON-WRITE, NOT ON CONSTRUCTION (THE-848)
`AuditLog.__init__` does not touch the filesystem. The directory is created
lazily, on the first `emit()` that actually has a line to write. Before this,
`__init__` did the `mkdir`, which meant merely *constructing* an `AuditLog` —
including the one `codecalc.server` builds at import time via `from_env()` —
created `~/.codecalc/audit/` as a side effect of import. An import must be
free of that: a CI runner or a test that only imports `codecalc.server`
should not leave a directory behind in the real home.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

# Event types. Stable strings a downstream reader may branch on.
CAPABILITY_APPROVED = "capability_approved"
CAPABILITY_DENIED = "capability_denied"
CAPABILITY_REJECTED = "capability_rejected"
INSTALL_DENIED = "install_denied"
STRICT_PROVIDER_REJECTED = "strict_provider_rejected"
CLEANUP = "cleanup"


def _iso(epoch: float) -> str:
    """A source-safe UTC timestamp. Derived from the injected clock, never from
    an implicit `datetime.now()`, so a fixed clock yields a fixed string."""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


class AuditLog:
    """Append-only JSON-lines audit sink. Thread-safe enough for append-only.

    `path=None` makes a no-op sink (`emit` still returns the event dict, so a
    caller/test can inspect it, but nothing is written) — the shape a disabled
    audit takes, distinct from a configured one.
    """

    def __init__(self, path: Path | None, *, clock: Callable[[], float] = time.time,
                 secrets: tuple[str, ...] = ()) -> None:
        self.path = Path(path) if path is not None else None
        self._clock = clock
        # Longest first, so a credential that contains a shorter secret as a
        # substring is redacted whole rather than partially.
        self._secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))
        self._warned = False

    def _warn(self) -> None:
        if not self._warned:
            self._warned = True
            print(f"codecalc: audit log at {self.path} is not writable; "
                  f"audit events for this process are being dropped",
                  file=sys.stderr)

    def _redact(self, line: str) -> str:
        for secret in self._secrets:
            line = line.replace(secret, "[REDACTED]")
        return line

    def emit(self, event_type: str, *, run_id: str | None = None,
             session_id: str | None = None, decision: str | None = None,
             reason: str | None = None, **fields: object) -> dict:
        """Append one event and return it. Best effort: a write failure never raises.

        `**fields` is for safe, structured metadata only (capability names,
        provider ids, package names). Do not pass source, stdin, output or
        credentials — the redaction pass is a backstop, not a licence.
        """
        event = {
            "timestamp": _iso(self._clock()),
            "event_type": event_type,
            "run_id": run_id,
            "session_id": session_id,
            "decision": decision,
            "reason": reason,
            **fields,
        }
        if self.path is not None:
            line = self._redact(json.dumps(event, sort_keys=True))
            try:
                # Created here, on first use, not in __init__ — see the module
                # docstring's CREATE-ON-WRITE note (THE-848).
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                self._warn()
        return event


def from_env(environment=None, *, clock: Callable[[], float] = time.time,
             secrets: tuple[str, ...] = ()) -> AuditLog:
    """Build the process audit log from the environment.

    `CODECALC_AUDIT_LOG` names the file. Unset defaults to
    `~/.codecalc/audit/audit.log`, alongside the run-supervisor journal — the
    trail is on by default because it is local, append-only provenance and the
    thing it records (a denied capability, a refused install) is exactly what an
    operator wants a record of. `CODECALC_AUDIT_LOG` set to an empty string
    DISABLES it (a no-op sink), matching the empty-is-a-choice reading the rest
    of the server uses for security-relevant knobs.
    """
    import os

    env = os.environ if environment is None else environment
    if "CODECALC_AUDIT_LOG" in env:
        raw = env["CODECALC_AUDIT_LOG"].strip()
        path = Path(raw).expanduser() if raw else None
    else:
        path = Path("~/.codecalc/audit/audit.log").expanduser()
    return AuditLog(path, clock=clock, secrets=secrets)
