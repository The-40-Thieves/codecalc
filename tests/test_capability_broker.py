"""The capability broker: approved never exceeds requested (THE-787, Residual 1).

The one load-bearing invariant of the broker is a subset relation — the set of
capabilities policy APPROVES for a job can never exceed the set the requester
REQUESTED. These tests pin that invariant three ways: policy cannot approve a
capability the request did not ask for (rejected, with a STABLE code, and BEFORE
any side effect); a requested+supported capability is approved and the four
disclosure sets reach the receipt; and an UNSET policy leaves today's behaviour
byte-for-byte unchanged.

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import capabilities, errors, execution_service, providers

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


NET = capabilities.NETWORK
_SUPPORTS_NET = frozenset({NET})
_NO_SUPPORT = frozenset()


def _service(policy=None):
    return execution_service.ExecutionService(
        providers.configured_registry(), policy=policy)


# ── the pure invariant: approved ⊆ requested, ALWAYS ───────────────────────
# Swept across every (requested, policy) pair the broker can see. A property,
# not an example: whatever the policy grants or denies, the approved set is a
# subset of what was asked for, or the decision is a rejection.
_POLICIES = [
    capabilities.CapabilityPolicy(source="empty"),
    capabilities.CapabilityPolicy(default_deny=frozenset({NET}), source="deny"),
    capabilities.CapabilityPolicy(grant=frozenset({NET}), source="grant"),
    capabilities.CapabilityPolicy(default_deny=frozenset({NET}),
                                  grant=frozenset({NET}), source="deny+grant"),
    capabilities.CapabilityPolicy(default_deny=frozenset({NET}), strict=True,
                                  source="deny+strict"),
]
for _req in (frozenset(), frozenset({NET})):
    for _pol in _POLICIES:
        for _sup in (_NO_SUPPORT, _SUPPORTS_NET):
            _d = capabilities.broker(_req, policy=_pol, provider_supported=_sup)
            check(f"approved ⊆ requested [req={sorted(_req)} pol={_pol.source} "
                  f"sup={sorted(_sup)}]",
                  _d.rejected or _d.approved <= _req,
                  f"approved={sorted(_d.approved)} requested={sorted(_req)}")


# ── Rejection A: policy may not approve an UN-REQUESTED capability ──────────
# A policy that grants network to a job that asked for NONE (no_net=True) is an
# escalation. The broker refuses it rather than silently adding the capability.
_grant_net = capabilities.CapabilityPolicy(grant=frozenset({NET}), source="allow-network")
_esc = capabilities.broker(frozenset(), policy=_grant_net, provider_supported=_SUPPORTS_NET)
check("granting an un-requested capability is REJECTED, not honoured",
      _esc.rejected is True)
check("the rejection approves nothing", _esc.approved == frozenset())
check("the rejection carries the stable discriminator",
      _esc.provider_error == capabilities.CAPABILITY_NOT_REQUESTED)

# The rejection is PRE-side-effect: a job that set no_net=True never runs, and
# the result is a stable coded permission error, not an execution result. A
# unique probe path (not a fixed name) so no leftover file can poison the
# no-side-effect assertion.
_probe = pathlib.Path(tempfile.gettempdir()) / f"broker_escalation_probe_{id(object())}"
_probe.unlink(missing_ok=True)
_svc = _service(_grant_net)
_res = _svc.execute(providers.ComputationSpec(
    language="python3", code=f"open({str(_probe)!r},'w').write('x')",
    no_net=True))
check("an escalation request is refused before it can execute",
      _res["ok"] is False)
check("  ...with the taxonomy code PERMISSION_DENIED",
      _res.get("code") == errors.PERMISSION_DENIED)
check("  ...and the capability_not_requested discriminator",
      _res.get("provider_error") == capabilities.CAPABILITY_NOT_REQUESTED)
check("  ...naming the requested capabilities it would not exceed",
      _res.get("requested_capabilities") == [])
check("  ...and it produced NO side effect (nothing ran, no verdict)",
      "verdict" not in _res and not _probe.exists())


# ── Rejection B: strict policy + a denial the provider cannot enforce ───────
_strict = capabilities.CapabilityPolicy(default_deny=frozenset({NET}),
                                        strict=True, source="deny-network,strict")
_unenf = capabilities.broker(frozenset({NET}), policy=_strict, provider_supported=_NO_SUPPORT)
check("strict rejects a denial the provider cannot enforce",
      _unenf.rejected is True
      and _unenf.provider_error == capabilities.CAPABILITY_UNENFORCEABLE)
_enf = capabilities.broker(frozenset({NET}), policy=_strict, provider_supported=_SUPPORTS_NET)
check("strict does NOT reject when the denial IS enforceable",
      _enf.rejected is False and _enf.denied == frozenset({NET}))


# ── the happy path: a requested + supported capability is APPROVED ─────────
_deny = capabilities.CapabilityPolicy(default_deny=frozenset({NET}), source="deny-network")
_allow = capabilities.CapabilityPolicy(default_deny=frozenset({NET}),
                                       grant=frozenset({NET}), source="deny-network,allow-network")
_approved = capabilities.broker(frozenset({NET}), policy=_allow, provider_supported=_SUPPORTS_NET)
check("a requested capability the policy grants is approved",
      _approved.approved == frozenset({NET}) and _approved.denied == frozenset())
check("an approved+supported capability is effective",
      _approved.effective == frozenset({NET}))


# ── the four sets reach the receipt on a real run ──────────────────────────
_deny_svc = _service(_deny)
_r = _deny_svc.execute(providers.ComputationSpec(language="python3", code="print(1)"))
_caps = _r["provider"]["capabilities"]
check("the receipt surfaces requested/approved/provider_supported/effective",
      {"requested", "approved", "provider_supported", "effective"} <= set(_caps))
check("deny-network denies the un-granted network request on the receipt",
      _caps["requested"] == ["network"] and _caps["approved"] == []
      and _caps["denied"] == ["network"])
check("the receipt records the policy that produced the decision",
      _caps["policy"] == "deny-network" and _caps["brokered"] is True)


# ── Backward compatibility: UNSET policy == today's behaviour ──────────────
# policy_from_env with the variable absent is None -> no brokering.
check("an unset CODECALC_CAPABILITY_POLICY yields no policy",
      capabilities.policy_from_env({}) is None)
check("an empty CODECALC_CAPABILITY_POLICY yields no policy",
      capabilities.policy_from_env({capabilities.POLICY_ENV: "  "}) is None)

_off = _service(None)  # explicit no-policy, matches env-unset
_r_off = _off.execute(providers.ComputationSpec(language="python3", code="print(1)", no_net=False))
_caps_off = _r_off["provider"]["capabilities"]
check("with no policy the run is unchanged: nothing denied",
      _r_off["ok"] is True and _caps_off["denied"] == [])
check("with no policy approved == requested (pure disclosure)",
      _caps_off["approved"] == _caps_off["requested"])
check("with no policy the receipt marks brokered:false and policy:null",
      _caps_off["brokered"] is False and _caps_off["policy"] is None)
check("with no policy a no_net=False job keeps network effective",
      _caps_off["effective"] == ["network"])


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL CAPABILITY-BROKER TESTS PASS ===")
sys.exit(1 if FAILS else 0)
