"""Structured, auditable events for broker decisions and side effects (THE-787, Residual 3).

Before this, the provenance of a brokered denial or a refused install was
reconstructible only by correlating a receipt with the run-supervisor journal.
This adds an append-only audit stream. These tests pin the two events the ticket
names — a DENIED CAPABILITY and a DENIED INSTALL — and hold the two properties
that make an audit trail trustworthy: the right structured fields are present,
and NO secret or payload (executed source, credentials) is in the record. Time
is injected, so timestamps are deterministic and never read implicitly.

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import (
    audit as audit_module,
)
from codecalc import (
    capabilities,
    execution_service,
    packages,
    providers,
)

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def _read(path):
    path = pathlib.Path(path)
    if not path.exists():  # a missing file means nothing was emitted — a clean FAIL below
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


_tmp = pathlib.Path(tempfile.mkdtemp(prefix="codecalc-audit-"))


# ── injected clock: a deterministic, source-safe timestamp ─────────────────
_clock_log = audit_module.AuditLog(_tmp / "clock.log", clock=lambda: 0.0)
_ev = _clock_log.emit(audit_module.CAPABILITY_APPROVED, run_id="r1", decision="approved")
check("the timestamp is derived from the INJECTED clock, not the wall clock",
      _ev["timestamp"] == "1970-01-01T00:00:00+00:00")
check("emit returns the event and appends it",
      _read(_tmp / "clock.log")[0]["event_type"] == audit_module.CAPABILITY_APPROVED)


# ── a DENIED CAPABILITY produces an audit event, with no payload ───────────
_cap_log = audit_module.AuditLog(_tmp / "cap.log", clock=lambda: 100.0)
_deny = capabilities.CapabilityPolicy(default_deny=frozenset({capabilities.NETWORK}),
                                      source="deny-network")
_svc = execution_service.ExecutionService(
    providers.configured_registry(), audit=_cap_log, policy=_deny)
_MARKER = "SENSITIVE_SOURCE_MARKER_9c96"
_svc.execute(providers.ComputationSpec(
    language="python3", code=f"x = '{_MARKER}'; print('done')"))
_cap_events = _read(_tmp / "cap.log")
_denied = [e for e in _cap_events if e["event_type"] == audit_module.CAPABILITY_DENIED]
check("a denied capability emits exactly one capability_denied event",
      len(_denied) == 1, f"-> {[e['event_type'] for e in _cap_events]}")
if _denied:
    _e = _denied[0]
    check("the event names the decision and the denied capability",
          _e["decision"] == "denied" and _e.get("denied") == ["network"])
    check("the event names the requested set and the policy",
          _e.get("requested") == ["network"] and _e.get("policy") == "deny-network")
    check("the event carries the source-safe timestamp",
          _e["timestamp"] == "1970-01-01T00:01:40+00:00")
_raw_cap = (_tmp / "cap.log").read_text(encoding="utf-8")
check("NO executed source leaks into the audit record",
      _MARKER not in _raw_cap)


# ── a DENIED INSTALL produces an audit event, with no secret ───────────────
_ins_log = audit_module.AuditLog(_tmp / "install.log", clock=lambda: 200.0)
os.environ[packages.ALLOWLIST_ENV] = "requests"  # deny everything but 'requests'
try:
    _res = packages.install("python3", "malicious-pkg", audit=_ins_log)
finally:
    del os.environ[packages.ALLOWLIST_ENV]
check("the install was denied by the allowlist",
      _res["ok"] is False and _res["code"] == "permission_denied")
_ins_events = [e for e in _read(_tmp / "install.log")
               if e["event_type"] == audit_module.INSTALL_DENIED]
check("a denied install emits an install_denied event",
      len(_ins_events) == 1)
if _ins_events:
    _ie = _ins_events[0]
    check("the install event names the ecosystem and bare package",
          _ie.get("language") == "python3" and _ie.get("package") == "malicious-pkg")
    check("the install event records the denial decision and reason",
          _ie["decision"] == "denied" and bool(_ie.get("reason")))


# ── redaction: a configured secret never appears in the record ─────────────
_SECRET = "tok_live_abc123DEF"  # noqa: S105 -- a FAKE secret, the fixture the redaction test redacts
_red_log = audit_module.AuditLog(_tmp / "red.log", clock=lambda: 0.0, secrets=(_SECRET,))
_red_log.emit(audit_module.CLEANUP, run_id="r2", reason=f"leaked {_SECRET} oops")
_raw_red = (_tmp / "red.log").read_text(encoding="utf-8")
check("a configured secret is redacted from the serialized line",
      _SECRET not in _raw_red and "[REDACTED]" in _raw_red)


# ── a disabled (path=None) sink is a safe no-op ────────────────────────────
_noop = audit_module.AuditLog(None)
_noop_ev = _noop.emit(audit_module.CAPABILITY_APPROVED, run_id="r3")
check("a path=None sink still returns the event dict",
      _noop_ev["event_type"] == audit_module.CAPABILITY_APPROVED)
check("a path=None sink writes nothing", _noop.path is None)


# ── from_env: unset defaults on, empty disables ────────────────────────────
check("CODECALC_AUDIT_LOG unset defaults to a configured path",
      audit_module.from_env({}).path is not None)
check("CODECALC_AUDIT_LOG='' disables the sink",
      audit_module.from_env({"CODECALC_AUDIT_LOG": ""}).path is None)
check("CODECALC_AUDIT_LOG=<path> honours it",
      str(audit_module.from_env(
          {"CODECALC_AUDIT_LOG": str(_tmp / "x.log")}).path) == str(_tmp / "x.log"))


# ── the server ARMS redaction with the provider-auth secret values ─────────
# THE-787 fix round, IMPORTANT: from_env() was called with no secrets=, so the
# redaction pass existed but was never armed against the real credentials. The
# server now feeds the CODECALC_*_AUTHORIZATION / _HTTP_TOKEN values in.
# Isolate the server's run-state dir before importing it (keeps this test off the
# shared ~/.codecalc/runs).
os.environ["CODECALC_RUN_STATE_DIR"] = str(_tmp / "runs")
from codecalc import providers, server

_CRED = "sk-strict-XYZ789redactme"
os.environ[providers.STRICT_AUTHORIZATION_ENV] = f"Bearer {_CRED}"
try:
    _secrets = server._audit_secrets()
    check("the server registers the whole Authorization header for redaction",
          f"Bearer {_CRED}" in _secrets)
    check("the server also registers the bare credential portion",
          _CRED in _secrets)
    _armed = audit_module.AuditLog(_tmp / "armed.log", clock=lambda: 0.0, secrets=_secrets)
    _armed.emit(audit_module.CAPABILITY_REJECTED, run_id="r4",
                reason=f"provider said {_CRED}")
    _raw_armed = (_tmp / "armed.log").read_text(encoding="utf-8")
    check("a credential value reaching an audit field is redacted",
          _CRED not in _raw_armed and "[REDACTED]" in _raw_armed)
finally:
    os.environ.pop(providers.STRICT_AUTHORIZATION_ENV, None)


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL AUDIT-LOG TESTS PASS ===")
sys.exit(1 if FAILS else 0)
