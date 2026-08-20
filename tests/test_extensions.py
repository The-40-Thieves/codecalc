"""THE-794: the shared extension framework (codecalc/extensions.py).

Standalone, like the other suites: a check() accumulator, sys.exit(1) on any
failure. Exercises the registration rules (identity/no-impersonation, interface
major, permission allowlist, disable switch), integrity, call isolation, and
capability discovery — the cross-cutting acceptance criteria.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import extensions as ext

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def raises(code: str, fn) -> tuple[bool, str]:
    try:
        fn()
    except ext.ExtensionError as exc:
        return exc.code == code, f"-> got {exc.code!r}"
    except Exception as exc:
        return False, f"-> non-ExtensionError {type(exc).__name__}"
    return False, "-> did not raise"


def manifest(**over) -> ext.ExtensionManifest:
    base = {
        "kind": ext.ExtensionKind.RENDERER,
        "extension_id": "builtin:demo",
        "name": "Demo",
        "version": "1.0.0",
        "interface_version": "1.0.0",
        "origin": "builtin",
        "compatible_codecalc": ">=0.2,<0.3",
        "compatible_contract": ">=1.2,<2",
    }
    base.update(over)
    return ext.ExtensionManifest(**base)


class FakeExt:
    def __init__(self, m: ext.ExtensionManifest, *, health=None) -> None:
        self.manifest = m
        self._health = health if health is not None else {"ready": True}

    def health(self) -> dict:
        if isinstance(self._health, Exception):
            raise self._health
        return self._health


def registry(**kw) -> ext.ExtensionRegistry:
    return ext.ExtensionRegistry(ext.ExtensionKind.RENDERER, "1.0.0", **kw)


# ── manifest ─────────────────────────────────────────────────────────────────
m = manifest(supported_operations=("render",))
d = m.to_dict()
check("manifest.to_dict serializes kind as its string", d["kind"] == "renderer", f"-> {d['kind']!r}")
check("manifest.is_builtin reflects origin", m.is_builtin is True and manifest(origin="third_party", extension_id="acme.x").is_builtin is False)

# ── integrity ────────────────────────────────────────────────────────────────
digest = ext.compute_integrity(b"payload")
check("compute_integrity is a sha256: digest", digest.startswith("sha256:") and len(digest) == 71, f"-> {digest[:14]}...")
ext.verify_integrity(manifest(integrity=digest), b"payload")  # matches → no raise
check("verify_integrity accepts a matching payload", True)
ok, det = raises("extension_integrity_failure", lambda: ext.verify_integrity(manifest(integrity=digest), b"tampered"))
check("verify_integrity rejects a tampered payload", ok, det)
ext.verify_integrity(manifest(integrity=None), b"anything")  # unpinned → no raise
check("verify_integrity is a no-op when nothing is pinned", True)

# ── policy from env ──────────────────────────────────────────────────────────
p_default = ext.ExtensionPolicy.from_env({})
check("default policy allows third-party", p_default.allow_third_party is True)
p_off = ext.ExtensionPolicy.from_env({"CODECALC_DISABLE_THIRD_PARTY_EXTENSIONS": "1"})
check("disable switch turns third-party off", p_off.allow_third_party is False)
p_narrow = ext.ExtensionPolicy.from_env({"CODECALC_EXTENSION_ALLOWED_PERMISSIONS": "render, verify"})
check("policy narrows the allowlist from env", p_narrow.allowed_permissions == frozenset({"render", "verify"}), f"-> {sorted(p_narrow.allowed_permissions)}")

# ── registration: identity + no impersonation ────────────────────────────────
r = registry()
r.register(FakeExt(manifest(extension_id="builtin:text", supported_operations=("render",))))
check("a builtin registers with the builtin: prefix", "builtin:text" in r)
r.register(FakeExt(manifest(origin="third_party", extension_id="acme.csv", declared_permissions=("render",))))
check("a third-party extension registers", "acme.csv" in r)

ok, det = raises("extension_identity_conflict", lambda: registry().register(FakeExt(manifest(origin="third_party", extension_id="builtin:evil"))))
check("third-party CANNOT claim the reserved builtin: prefix", ok, det)
ok, det = raises("extension_identity_conflict", lambda: registry().register(FakeExt(manifest(origin="builtin", extension_id="nakedid"))))
check("a builtin id must carry the builtin: prefix", ok, det)
ok, det = raises("extension_identity_conflict", lambda: registry().register(FakeExt(manifest(origin="sideloaded", extension_id="x"))))
check("an unknown origin is refused", ok, det)

r2 = registry()
r2.register(FakeExt(manifest(extension_id="builtin:dup")))
ok, det = raises("extension_identity_conflict", lambda: r2.register(FakeExt(manifest(extension_id="builtin:dup"))))
check("a duplicate id is refused", ok, det)

# ── registration: interface version ──────────────────────────────────────────
ok, det = raises("extension_interface_mismatch", lambda: registry().register(FakeExt(manifest(interface_version="2.0.0"))))
check("an incompatible interface major is refused", ok, det)
registry().register(FakeExt(manifest(interface_version="1.5.9")))  # same major → ok
check("a compatible interface major (1.x) is accepted", True)

# ── registration: permissions ────────────────────────────────────────────────
ok, det = raises("extension_permission_denied", lambda: registry().register(FakeExt(manifest(origin="third_party", extension_id="acme.y", declared_permissions=("render", "mine_bitcoin")))))
check("a permission outside the known vocabulary is refused", ok, det)
r_narrow = registry(policy=ext.ExtensionPolicy(allow_third_party=True, allowed_permissions=frozenset({"render"})))
ok, det = raises("extension_permission_denied", lambda: r_narrow.register(FakeExt(manifest(origin="third_party", extension_id="acme.z", declared_permissions=("render", "network")))))
check("a third-party permission the policy disallows is refused", ok, det)

# ── registration: disable switch ─────────────────────────────────────────────
r_off = registry(policy=ext.ExtensionPolicy(allow_third_party=False))
r_off.register(FakeExt(manifest(extension_id="builtin:still-ok")))  # builtins unaffected
check("the disable switch leaves builtins unaffected", "builtin:still-ok" in r_off)
ok, det = raises("extension_disabled", lambda: r_off.register(FakeExt(manifest(origin="third_party", extension_id="acme.blocked", declared_permissions=("render",)))))
check("the disable switch refuses third-party extensions", ok, det)

# ── registration: integrity is ENFORCED at admission ────────────────────────
_pay = b"the extension source bytes"
_dg = ext.compute_integrity(_pay)
registry().register(FakeExt(manifest(extension_id="builtin:pinned", integrity=_dg)), payload=_pay)
check("a pinned extension registers when the payload matches the digest", True)
ok, det = raises("extension_integrity_failure", lambda: registry().register(FakeExt(manifest(extension_id="builtin:pinned2", integrity=_dg)), payload=b"tampered"))
check("a pinned extension with a TAMPERED payload is refused at registration", ok, det)
ok, det = raises("extension_integrity_failure", lambda: registry().register(FakeExt(manifest(extension_id="builtin:pinned3", integrity=_dg))))
check("a pinned extension with NO payload is refused (an unverifiable pin != verified)", ok, det)
registry().register(FakeExt(manifest(extension_id="builtin:unpinned")))  # integrity=None → no payload needed
check("an UNPINNED extension registers with no payload", True)

# ── lookup ───────────────────────────────────────────────────────────────────
ok, det = raises("unknown_extension", lambda: registry().get("nope"))
check("get() of an unregistered id raises unknown_extension", ok, det)

# ── isolation (guard) ────────────────────────────────────────────────────────
rg = registry()
victim = FakeExt(manifest(extension_id="builtin:boom"))
rg.register(victim)
ok, det = raises("extension_render_failed", lambda: rg.guard(victim, "render", lambda: (_ for _ in ()).throw(ValueError("kaboom"))))
check("a raising extension op is isolated as extension_<op>_failed", ok, det)
ok, det = raises("unsupported_capability", lambda: rg.guard(victim, "render", lambda: (_ for _ in ()).throw(ext.UnsupportedCapability("builtin:boom", "render"))))
check("an UnsupportedCapability passes through guard unchanged", ok, det)
check("guard returns the value on success", rg.guard(victim, "render", lambda: 42) == 42)

# ── discovery ────────────────────────────────────────────────────────────────
rd = registry()
rd.register(FakeExt(manifest(extension_id="builtin:healthy", supported_operations=("render",))))
rd.register(FakeExt(manifest(extension_id="builtin:sick"), health=RuntimeError("down")))
descs = rd.descriptors()
by_id = {d["extension_id"]: d for d in descs}
check("descriptors carry the manifest fields", by_id["builtin:healthy"]["supported_operations"] == ["render"], f"-> {by_id['builtin:healthy'].get('supported_operations')}")
check("descriptors include a health snapshot", by_id["builtin:healthy"]["health"] == {"ready": True})
check("a failing health() is REPORTED, not raised", by_id["builtin:sick"]["health"]["ready"] is False, f"-> {by_id['builtin:sick']['health']}")

# describe_extensions across kinds
other = ext.ExtensionRegistry(ext.ExtensionKind.VERIFIER, "1.0.0")
other.register(FakeExt(manifest(kind=ext.ExtensionKind.VERIFIER, extension_id="builtin:v", supported_operations=("verify",))))
combined = ext.describe_extensions(rd, other)
kinds = {c["kind"] for c in combined}
check("describe_extensions flattens across kinds", kinds == {"renderer", "verifier"}, f"-> {sorted(kinds)}")
check("describe_extensions is sorted by (kind, id)", combined == sorted(combined, key=lambda c: (c["kind"], c["extension_id"])))


print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== EXTENSION FRAMEWORK OK ===")
sys.exit(1 if FAILS else 0)
