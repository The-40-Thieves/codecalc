"""Capability broker: policy-approved capabilities never exceed requested.

The residual leaves after receipts, the deny-by-default
install allowlist and the strict-provider attestation are
already shipped: a small policy layer between a request and its execution, whose
one load-bearing invariant is that the set of capabilities policy APPROVES for a
job can never exceed the set the requester REQUESTED. Policy may narrow; it may
never escalate.

WHY THIS IS A PURE FUNCTION, AND WHY THAT MATTERS
`broker()` takes `(requested, policy, provider_supported)` and returns a
`BrokerDecision`. It reads no environment, opens no file, runs nothing. The
environment is parsed once by `policy_from_env()` and the run wiring lives in
`execution_service.py`; keeping the decision itself pure is what lets the
invariant be tested directly (approved subset-of-or-equal requested) rather than inferred from a
side effect.

THE CAPABILITY VOCABULARY IS REUSED, NOT INVENTED
Capabilities here are the security-relevant postures codecalc already speaks:
today the one it can both broker AND enforce end to end is `network` — the
inverse of `ComputationSpec.no_net`, gated by the provider's own
`network_control` capability declaration. The tuple below is deliberately small
and extensible; a capability is only added here once codecalc can actually
enforce a decision about it, so the receipt never claims a guarantee the sandbox
cannot keep.

BACKWARD COMPATIBILITY
`CODECALC_CAPABILITY_POLICY` UNSET (or empty) means `policy_from_env()` returns
None and no brokering happens: the spec runs exactly as before, no capability is
denied and nothing is rejected. `decide()` still computes the four disclosure
sets for the receipt in that case (approved == requested, `brokered: false`), so
a reader always sees the capability posture — but the RUN is byte-for-byte the
old behaviour. Enforcement (forcing `no_net`, rejecting a request) happens only
when a policy is set, matching how every other `CODECALC_*` knob defaults off.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from . import errors

#: The capability codecalc can broker AND enforce today: network egress, the
#: inverse of `ComputationSpec.no_net`. Kept as a tuple so the vocabulary is one
#: place; add a member only when a provider capability can enforce a decision
#: about it (see the module docstring).
NETWORK = "network"
ALL_CAPABILITIES = (NETWORK,)

#: Environment knob, same convention as CODECALC_PACKAGE_ALLOWLIST / _MAX_ACTIVE_RUNS:
#: unset or empty is OFF (today's behaviour), a value activates brokering.
POLICY_ENV = "CODECALC_CAPABILITY_POLICY"

#: Stable `provider_error` discriminators for a brokered rejection. The taxonomy
#: CODE stays `errors.PERMISSION_DENIED` (a policy refusal IS a permission
#: decision) so the closed `code` enum in the result contract is untouched and
#: no contract_version bump is needed; these name the SPECIFIC refusal the same
#: way `unsupported_capability` / `strict_provider_unavailable` already do.
CAPABILITY_NOT_REQUESTED = "capability_not_requested"
CAPABILITY_UNENFORCEABLE = "capability_unenforceable"


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    """A parsed policy. Pure data; `broker()` is the only thing that reads it.

    - `default_deny`: capabilities denied unless the policy also `grant`s them.
      `network` here flips the default to network-denied (Residual 2).
    - `grant`: capabilities the policy explicitly approves. Because approved
      must never exceed requested, a granted capability the request did NOT ask
      for is an escalation and is rejected — not silently added.
    - `strict`: reject a job outright when a capability's brokered DENIAL cannot
      be enforced by the selected provider (fail closed rather than run with a
      denial the sandbox will silently leak).
    """

    default_deny: frozenset[str] = frozenset()
    grant: frozenset[str] = frozenset()
    strict: bool = False
    #: The raw directive string, carried onto the receipt so a reader can see
    #: which policy produced a decision without re-deriving it.
    source: str = ""


@dataclass(frozen=True, slots=True)
class BrokerDecision:
    """The outcome of one brokering. `approved subset-of-or-equal requested` always holds.

    `effective` is the capability posture the run actually HAS: every approved
    capability, plus any DENIED capability whose denial the provider could not
    enforce (a leak, disclosed rather than hidden — `strict` exists to reject
    exactly this case before it can happen).
    """

    requested: frozenset[str]
    approved: frozenset[str]
    provider_supported: frozenset[str]
    denied: frozenset[str]
    effective: frozenset[str]
    brokered: bool
    policy_source: str
    rejected: bool = False
    reason: str | None = None
    provider_error: str | None = None

    def to_receipt(self) -> dict:
        """The `capabilities` block attached to the execution receipt.

        Lists are sorted for a deterministic receipt (the receipt is hashed and
        compared elsewhere). `policy` is null when brokering was off.
        """
        return {
            "requested": sorted(self.requested),
            "approved": sorted(self.approved),
            "provider_supported": sorted(self.provider_supported),
            "effective": sorted(self.effective),
            "denied": sorted(self.denied),
            "brokered": self.brokered,
            "policy": self.policy_source or None,
        }


def requested_capabilities(spec: object) -> frozenset[str]:
    """The capabilities a spec asks for, read from the spec AS WRITTEN.

    `network` is requested exactly when the spec does not ask for isolation
    (`no_net` is False, its default). A spec that already set `no_net=True` is
    not requesting network and there is nothing for a deny policy to take away.
    """
    caps: set[str] = set()
    if not getattr(spec, "no_net", False):
        caps.add(NETWORK)
    return frozenset(caps)


def provider_supported_capabilities(descriptor: Mapping) -> frozenset[str]:
    """The capabilities the selected provider can ENFORCE a decision about.

    Read from the provider's own capability declaration — `network_control`
    means the provider can honour a network decision (grant or block). A
    provider that cannot is not trusted to enforce a denial; `strict` policy
    turns that into a rejection rather than a silent leak.
    """
    caps: set[str] = set()
    capabilities = descriptor.get("capabilities") if isinstance(descriptor, Mapping) else None
    if isinstance(capabilities, Mapping) and capabilities.get("network_control"):
        caps.add(NETWORK)
    return frozenset(caps)


def broker(requested: frozenset[str], *, policy: CapabilityPolicy,
           provider_supported: frozenset[str]) -> BrokerDecision:
    """Pure policy decision. `approved subset-of-or-equal requested` is guaranteed by construction.

    Two rejection conditions:

      1. The policy would GRANT a capability the request did not ask for. That is
         an escalation — the whole point of the broker is that policy narrows and
         never widens — so it is refused, not quietly honoured.
      2. Under `strict`, a requested capability whose brokered DENIAL the provider
         cannot enforce. Running it would leak the very capability policy meant
         to withhold, so the job is rejected before any side effect.
    """
    requested = frozenset(requested)
    provider_supported = frozenset(provider_supported)

    escalation = policy.grant - requested
    if escalation:
        names = ", ".join(sorted(escalation))
        return _reject(
            requested, provider_supported, policy,
            reason=(f"policy would grant capability {names!r} that the request "
                    f"did not ask for; policy may narrow requested capabilities "
                    f"but never add to them"),
            provider_error=CAPABILITY_NOT_REQUESTED,
        )

    approved = frozenset(
        cap for cap in requested
        if cap not in policy.default_deny or cap in policy.grant
    )
    denied = requested - approved

    if policy.strict:
        unenforceable = denied - provider_supported
        if unenforceable:
            names = ", ".join(sorted(unenforceable))
            return _reject(
                requested, provider_supported, policy,
                reason=(f"strict policy: the denial of capability {names!r} "
                        f"cannot be enforced by the selected provider "
                        f"(no provider enforcement available)"),
                provider_error=CAPABILITY_UNENFORCEABLE,
            )

    effective = approved | (denied - provider_supported)
    return BrokerDecision(
        requested=requested,
        approved=approved,
        provider_supported=provider_supported,
        denied=denied,
        effective=effective,
        brokered=True,
        policy_source=policy.source,
    )


def _reject(requested: frozenset[str], provider_supported: frozenset[str],
            policy: CapabilityPolicy, *, reason: str,
            provider_error: str) -> BrokerDecision:
    return BrokerDecision(
        requested=requested,
        approved=frozenset(),
        provider_supported=provider_supported,
        denied=requested,
        effective=frozenset(),
        brokered=True,
        policy_source=policy.source,
        rejected=True,
        reason=reason,
        provider_error=provider_error,
    )


def _passthrough(spec: object, descriptor: Mapping) -> BrokerDecision:
    """The disclosure-only decision used when no policy is set.

    approved == requested, nothing denied, nothing rejected, `brokered: false`.
    The four sets are still surfaced so the receipt always shows the capability
    posture — but the run is unchanged.
    """
    requested = requested_capabilities(spec)
    supported = provider_supported_capabilities(descriptor)
    return BrokerDecision(
        requested=requested,
        approved=requested,
        provider_supported=supported,
        denied=frozenset(),
        effective=requested,
        brokered=False,
        policy_source="",
    )


def decide(spec: object, descriptor: Mapping,
           policy: CapabilityPolicy | None) -> BrokerDecision:
    """Broker `spec` against `policy`, or produce the passthrough disclosure.

    `policy is None` (the CODECALC_CAPABILITY_POLICY-unset default) short-circuits
    to `_passthrough`: the receipt still gets its four sets, the run is unchanged.
    """
    if policy is None:
        return _passthrough(spec, descriptor)
    return broker(
        requested_capabilities(spec),
        policy=policy,
        provider_supported=provider_supported_capabilities(descriptor),
    )


def enforced_spec(spec, decision: BrokerDecision):
    """Return the spec to actually RUN, forcing no_net ONLY where the provider can
    enforce the network denial.

    A denied `network` is forced to `no_net=True` only when the selected provider
    declared `network_control` (it is in `decision.provider_supported`). Forcing
    `no_net` onto a provider that CANNOT enforce it is dishonest — and against a
    provider that RAISES on an unenforceable `no_net` (e.g. Piston) it converts a
    disclosable leak into a hard `validation` error. So under a
    NON-strict policy an unenforceable denial leaves the request AS-ASKED and the
    leak is disclosed through `decision.effective` (network stays effective, the
    contract's "disclosed as still effective where it cannot"); under a STRICT
    policy this branch is never reached — `broker()` has already rejected the job
    before any side effect, so strict still fails closed.

    A rejected decision never reaches here (the caller returns the error first).
    When no enforceable network denial applies the spec is returned unchanged, so
    the common case allocates no new object.
    """
    if (NETWORK in decision.denied and NETWORK in decision.provider_supported
            and not getattr(spec, "no_net", False)):
        return dataclasses.replace(spec, no_net=True)
    return spec


def rejection_result(decision: BrokerDecision, *, provider_id: str | None = None) -> dict:
    """The stable, coded failure a rejected decision becomes.

    `errors.PERMISSION_DENIED` is the taxonomy code (a policy refusal is a
    permission decision); `provider_error` carries the specific discriminator so
    a caller can branch on WHICH refusal without the free-text message.
    """
    extra: dict[str, object] = {
        "provider_error": decision.provider_error,
        "requested_capabilities": sorted(decision.requested),
    }
    if provider_id is not None:
        extra["requested_provider"] = provider_id
    return errors.error_result(errors.PERMISSION_DENIED, decision.reason or
                               "capability request rejected by policy", **extra)


def policy_from_env(environment: Mapping[str, str] | None = None) -> CapabilityPolicy | None:
    """Parse CODECALC_CAPABILITY_POLICY. None means OFF (today's behaviour).

    Comma-separated, case-insensitive directives:

      - `deny-network`   flip the default to network-denied (Residual 2)
      - `allow-network`  explicitly grant network (only honoured for a job that
                         requested it; granting network to a job that asked for
                         none is the escalation the broker rejects)
      - `strict`         reject a job whose denial the provider cannot enforce

    UNSET or empty/whitespace returns None — the only reading that means "no
    brokering", matching `_positive_int_env`'s empty-is-absent handling. An
    unrecognised token is ignored with a stderr warning rather than silently
    changing the policy, the same shape `_positive_int_env` uses for a bad value.
    """
    env = os.environ if environment is None else environment
    raw = env.get(POLICY_ENV, "").strip()
    if not raw:
        return None
    directives = [t.strip().lower() for t in raw.split(",") if t.strip()]
    default_deny: set[str] = set()
    grant: set[str] = set()
    strict = False
    unknown: list[str] = []
    for directive in directives:
        if directive in ("deny-network", "no-network", "default-deny-network"):
            default_deny.add(NETWORK)
        elif directive in ("allow-network", "grant-network"):
            grant.add(NETWORK)
        elif directive == "strict":
            strict = True
        else:
            unknown.append(directive)
    if unknown:
        print(f"codecalc: ignoring unrecognised {POLICY_ENV} directive(s) "
              f"{', '.join(unknown)!r}; recognised: deny-network, allow-network, "
              f"strict", file=sys.stderr)
    if not default_deny and not grant and not strict:
        # A value that parsed to nothing usable is still an ACTIVE (empty)
        # policy: it brokers (approved == requested, denies nothing) and emits
        # audit events, distinct from UNSET. Fail toward "brokering on" once the
        # operator has set the variable at all.
        pass
    return CapabilityPolicy(
        default_deny=frozenset(default_deny),
        grant=frozenset(grant),
        strict=strict,
        source=raw,
    )
