"""Deny-by-default network as a policy option (THE-787, Residual 2).

`no_net` is opt-in and defaults False — a job reaches the network unless it asks
not to. This adds a policy that FLIPS that default: with the policy on, a job
that does not request network runs with `no_net` enforced; with the policy unset,
nothing changes.

Enforcement is proven with a RECORDING provider rather than a live socket, so the
test is deterministic on BOTH the native (network-controlling) backend and the
pure-Python fallback, and needs no external network. It captures the exact spec
the provider is handed, so "no_net was forced" is a fact about the wiring, not an
inference from a blocked connection. (A live end-to-end block on the Linux shim
is verified out of band; see the stream report.)

Standalone runner (check()/FAILS/sys.exit), no pytest — the repo convention.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import capabilities, contract, execution_service, providers

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


NET = capabilities.NETWORK


class RecordingProvider(providers.LocalExecutionProvider):
    """Captures the spec it is handed and honestly discloses no_net it cannot
    enforce, parameterised by whether it controls the network."""

    def __init__(self, *, network_control: bool) -> None:
        self.provider_id = "recording"
        self._network_control = network_control
        self.captured = None

    def describe(self) -> dict:
        descriptor = super().describe()
        descriptor["provider_id"] = self.provider_id
        descriptor["capabilities"] = dict(descriptor["capabilities"])
        descriptor["capabilities"]["network_control"] = self._network_control
        return descriptor

    def execute(self, spec: providers.ComputationSpec) -> dict:
        self.captured = spec
        unenforced = []
        if spec.no_net and not self._network_control:
            unenforced.append("no_net_unenforced_no_network_control")
        return contract.stamp({
            "ok": True, "verdict": "OK", "stdout": "", "stderr": "",
            "exit_code": 0, "unenforced": unenforced,
        })


def _service(provider, policy):
    registry = providers.ProviderRegistry(default_provider_id="recording")
    registry.register(provider)
    return execution_service.ExecutionService(registry, policy=policy)


_DENY = capabilities.CapabilityPolicy(default_deny=frozenset({NET}), source="deny-network")

# ── A. policy ON + a network-controlling provider: no_net is ENFORCED ──────
prov_a = RecordingProvider(network_control=True)
res_a = _service(prov_a, _DENY).execute(
    providers.ComputationSpec(language="python3", code="print(1)", no_net=False))
check("deny-network forces no_net on a job that did not request network",
      prov_a.captured is not None and prov_a.captured.no_net is True)
caps_a = res_a["provider"]["capabilities"]
check("the receipt shows network requested but denied",
      caps_a["requested"] == ["network"] and caps_a["denied"] == ["network"]
      and caps_a["approved"] == [])
check("an ENFORCED denial leaves network out of the effective set",
      caps_a["effective"] == [])

# ── B. policy ON + a provider that cannot control the network ──────────────
# no_net is still forced onto the run, and the receipt HONESTLY discloses that
# the denial could not be enforced (network stays effective) — the fallback's
# fail-open reality, disclosed rather than hidden. This is the branch the CI
# python-fallback matrix job exercises.
prov_b = RecordingProvider(network_control=False)
res_b = _service(prov_b, _DENY).execute(
    providers.ComputationSpec(language="python3", code="print(1)", no_net=False))
check("deny-network forces no_net even when the provider cannot enforce it",
      prov_b.captured is not None and prov_b.captured.no_net is True)
caps_b = res_b["provider"]["capabilities"]
check("an UNENFORCEABLE denial discloses network as still effective (leak, named)",
      caps_b["denied"] == ["network"] and caps_b["effective"] == ["network"])

# ── C. strict + unenforceable denial: REJECTED, nothing runs ───────────────
prov_c = RecordingProvider(network_control=False)
strict = capabilities.CapabilityPolicy(default_deny=frozenset({NET}), strict=True,
                                       source="deny-network,strict")
res_c = _service(prov_c, strict).execute(
    providers.ComputationSpec(language="python3", code="print(1)"))
check("strict deny-network rejects when the denial cannot be enforced",
      res_c["ok"] is False
      and res_c.get("provider_error") == capabilities.CAPABILITY_UNENFORCEABLE)
check("a strict rejection never reaches the provider",
      prov_c.captured is None)

# ── D. policy UNSET: byte-for-byte the old behaviour ───────────────────────
prov_d = RecordingProvider(network_control=True)
res_d = _service(prov_d, None).execute(
    providers.ComputationSpec(language="python3", code="print(1)", no_net=False))
check("with no policy no_net is NOT forced (default stays False)",
      prov_d.captured is not None and prov_d.captured.no_net is False)
check("with no policy the receipt marks brokered:false, denies nothing",
      res_d["provider"]["capabilities"]["brokered"] is False
      and res_d["provider"]["capabilities"]["denied"] == [])

# A job that DID ask for isolation keeps it, policy on and no rejection: network
# was never requested, so deny-network has nothing to take and nothing to reject.
prov_e = RecordingProvider(network_control=True)
res_e = _service(prov_e, _DENY).execute(
    providers.ComputationSpec(language="python3", code="print(1)", no_net=True))
check("a job that requested isolation stays isolated under deny-network",
      prov_e.captured.no_net is True and res_e["ok"] is True)
check("  ...with nothing requested, nothing is denied",
      res_e["provider"]["capabilities"]["requested"] == []
      and res_e["provider"]["capabilities"]["denied"] == [])

# ── E. env parsing: the directives map to a policy ─────────────────────────
pol_env = capabilities.policy_from_env(
    {capabilities.POLICY_ENV: "deny-network, strict, bogus-token"})
check("CODECALC_CAPABILITY_POLICY=deny-network,strict parses to the right policy",
      pol_env is not None and NET in pol_env.default_deny and pol_env.strict is True)
pol_allow = capabilities.policy_from_env(
    {capabilities.POLICY_ENV: "deny-network,allow-network"})
check("allow-network parses to an explicit grant",
      pol_allow is not None and NET in pol_allow.grant and NET in pol_allow.default_deny)

print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL NETWORK-POLICY TESTS PASS ===")
sys.exit(1 if FAILS else 0)
