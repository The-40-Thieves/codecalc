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


# ── F. the BACKGROUND path (run_submit) is brokered identically ────────────
# THE-787 fix round, CRITICAL: run_submit used to call RunSupervisor.start
# directly, bypassing the broker entirely — a deny-network policy the sync
# execute_code path enforced was silently void on the background path (network
# reached), and a strict-unenforceable job ran to completion instead of being
# rejected. These drive the real server.run_submit / run_inspect tools against
# a recording provider registered into the server's own registry, so the fix is
# exercised end to end, not just the helper.
import os
import tempfile
import time

# Isolate the server's run-state and audit paths into throwaway dirs BEFORE the
# import that builds them, so this test's background-run journals and audit
# lines never touch the shared ~/.codecalc — a synthetic-provider journal left
# in the real state dir would make a later server import trip recover_orphans.
_state = tempfile.mkdtemp(prefix="codecalc-netpolicy-runs-")
os.environ["CODECALC_RUN_STATE_DIR"] = _state
os.environ["CODECALC_AUDIT_LOG"] = str(pathlib.Path(_state) / "audit.log")

from codecalc import server


class _ServerRecordingProvider(providers.LocalExecutionProvider):
    def __init__(self, provider_id: str, *, network_control: bool) -> None:
        self.provider_id = provider_id
        self._network_control = network_control
        self.captured = None

    def describe(self) -> dict:
        d = super().describe()
        d["provider_id"] = self.provider_id
        d["capabilities"] = dict(d["capabilities"])
        d["capabilities"]["network_control"] = self._network_control
        return d

    def execute(self, spec: providers.ComputationSpec) -> dict:
        self.captured = spec
        return contract.stamp({"ok": True, "verdict": "OK", "stdout": "",
                               "stderr": "", "exit_code": 0, "unenforced": []})


_bg = _ServerRecordingProvider("net-policy-bg", network_control=True)
_bg_strict = _ServerRecordingProvider("net-policy-bg-strict", network_control=False)
server._provider_registry.register(_bg)
server._provider_registry.register(_bg_strict)

# deny-network via the background path: no_net FORCED, receipt carries the block.
os.environ[capabilities.POLICY_ENV] = "deny-network"
try:
    _sub = server.run_submit(language="python3", code="print(1)", provider="net-policy-bg")
    check("run_submit under deny-network accepts and returns a run_id",
          _sub.get("ok") is True and bool(_sub.get("run_id")))
    _term = server.run_inspect(_sub["run_id"])
    for _ in range(50):  # let the background future settle
        if "provider" in _term:
            break
        time.sleep(0.02)
        _term = server.run_inspect(_sub["run_id"])
    check("run_submit FORCES no_net on the background provider (broker not bypassed)",
          _bg.captured is not None and _bg.captured.no_net is True)
    _bg_caps = _term.get("provider", {}).get("capabilities")
    check("the run_inspect terminal receipt carries the capabilities block",
          isinstance(_bg_caps, dict))
    check("the background receipt shows network requested but denied",
          _bg_caps and _bg_caps["denied"] == ["network"]
          and _bg_caps["approved"] == [] and _bg_caps["brokered"] is True)

    # PROVENANCE INVARIANT (fix round 2): the receipt must name the REQUEST as
    # written, not the enforced spec. run_submit built the spec with no_net=False
    # (network requested) and timeout defaulting to 30; deny-network narrowed it
    # to no_net=True for EXECUTION, but the receipt's spec_hash and
    # limits.requested must still describe what the caller ASKED. Otherwise a
    # background receipt cannot be reconciled to its request by hash, and it
    # falsely claims the caller requested isolation.
    _request = providers.ComputationSpec(
        language="python3", code="print(1)", stdin="", timeout=30,
        max_memory_mb=0, max_output_kb=0, max_cpu=0, no_net=False)
    _bg_provider_receipt = _term["provider"]
    check("the background receipt hashes the REQUEST, not the enforced spec",
          _bg_provider_receipt["spec_hash"] == _request.spec_hash())
    check("limits.requested reflects what the caller ASKED (no_net False)",
          _bg_provider_receipt["limits"]["requested"]["no_net"] is False)
    check("the enforced spec's hash is DIFFERENT (the bug would have reported it)",
          _request.spec_hash()
          != providers.ComputationSpec(
              language="python3", code="print(1)", stdin="", timeout=30,
              no_net=True).spec_hash())

    # And the invariant across paths: the SAME request produces the SAME
    # spec_hash and limits.requested whether run synchronously or in background.
    _sync = server.execute_code("python3", "print(1)", timeout=30,
                                provider="net-policy-bg")
    check("run_submit and execute_code share one spec_hash for one request",
          _sync["provider"]["spec_hash"] == _bg_provider_receipt["spec_hash"])
    check("both paths report the same requested limits",
          _sync["provider"]["limits"]["requested"]
          == _bg_provider_receipt["limits"]["requested"])
finally:
    os.environ.pop(capabilities.POLICY_ENV, None)

# strict + unenforceable denial via the background path: REJECTED PRE-START.
os.environ[capabilities.POLICY_ENV] = "deny-network,strict"
try:
    _runs_before = len(server._run_supervisor._runs)
    _rej = server.run_submit(language="python3", code="print(1)",
                             provider="net-policy-bg-strict")
    _runs_after = len(server._run_supervisor._runs)
    check("strict-unenforceable run_submit is REJECTED, not run",
          _rej.get("ok") is False
          and _rej.get("provider_error") == capabilities.CAPABILITY_UNENFORCEABLE)
    check("the rejection is PRE-start: no run was created",
          _runs_before == _runs_after)
    check("the rejected strict run never reached the provider",
          _bg_strict.captured is None)
finally:
    os.environ.pop(capabilities.POLICY_ENV, None)


print(f"\n=== {len(FAILS)} FAILURES ===" if FAILS else
      "\n=== ALL NETWORK-POLICY TESTS PASS ===")
sys.exit(1 if FAILS else 0)
