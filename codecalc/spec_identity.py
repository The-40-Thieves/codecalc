"""Canonical serialization and content identity for a computation request (THE-793).

THE-790 built the canonical REQUEST: `providers.ComputationSpec` is a frozen,
transport-neutral dataclass that every provider, the execution service, the
supervisor and the strict clients consume. What it did not have was an
IDENTITY. Two callers could build the same request and had no name for it, so
nothing downstream — a cache, an execution receipt, a provenance record — could
say "this is the same computation" without carrying the whole object around and
comparing it field by field.

This module is that name: a deterministic byte encoding of a request, and a
sha256 over those bytes.

WHY BYTES FIRST AND THE HASH SECOND
A hash is only as stable as the encoding under it, and every interesting bug in
a scheme like this lives in the encoding: a dict that iterated in insertion
order, a float that printed with a different number of digits on another
runtime, a defaulted field that was omitted here and emitted there. So the
canonical bytes are the published artifact and the hash is a one-line function
of them. `docs/contract/computation-spec-v1.vectors.json` pins BOTH, and
`scripts/check_contract.py` recomputes them on every push.

THE RULES, AND WHY EACH ONE EXISTS

  1. JSON, UTF-8, no insignificant whitespace, object keys sorted.
     `separators=(",", ":")` and `sort_keys=True`. This is RFC 8785 (JSON
     Canonicalization Scheme) for the value space a spec can contain. JCS sorts
     keys by UTF-16 code unit and Python sorts by code point; the two orders can
     only differ above the BMP, and every key here is a dataclass field name or
     an environment-variable name, so the divergence is unreachable rather than
     merely unlikely.

  2. EVERY field is emitted, including one left at its default.
     The tempting alternative — omit anything equal to its default, so that
     adding a defaulted field keeps old hashes valid — makes the canonical bytes
     unreadable without the default table of the version that produced them, and
     makes a changed default silently change what a stored hash MEANT. Emitting
     everything means the bytes reconstruct the request on their own. The cost is
     stated rather than hidden: see `COMPUTATION_SPEC_VERSION`.

  3. There is no "absent". `None` is a value and encodes as JSON `null`.
     `workdir=None` (let the provider choose) and `workdir=""` (an empty path)
     are different requests and get different hashes.

  4. `bool` is checked BEFORE `int`, because in Python `isinstance(True, int)`
     is true. Without the ordering, `no_net=True` and `no_net=1` would encode
     identically — two requests one type-check away from each other collapsing
     into one identity.

  5. Sequence order is PRESERVED; mapping key order is NOT.
     A list's order is part of what was asked for. A dict's is an artifact of
     how it was built. `tuple` and `list` encode identically, because JSON has
     one array type and transport neutrality is the whole point of the spec.

  6. `bytes` encode as `{"__bytes_b64__": "<base64>"}`, not as a bare string.
     A bare base64 string is indistinguishable from a `str` field that happens
     to hold the same text, which is a collision between two different requests.
     The tag is therefore RESERVED: a mapping that tries to use it as a key is
     refused rather than allowed to forge a bytes value.

  7. `float` is REFUSED. No field is a float today, and shortest-round-trip
     IEEE-754 printing is exactly where canonicalizers diverge between runtimes
     (and NaN/Infinity are not JSON at all). A float field added later fails
     loudly here, pointing at this paragraph, instead of quietly minting an
     identity that another implementation cannot reproduce.

  8. Anything with no rule — a set, an arbitrary object, a complex number — is
     refused. Refusing to guess is the rule. A set in particular has no stable
     iteration order, which is the failure this module exists to prevent.

SECRETS ARE NOT PART OF AN IDENTITY
The canonical bytes must never become a place a secret is stored: they are
hashed, logged next to receipts, and compared across processes. Read at the time
of writing, `ComputationSpec` carries NO credential-bearing field — its fields
are `language`, `code`, `stdin`, `timeout`, `workdir`, `max_memory_mb`,
`max_output_kb`, `max_cpu`, `no_net`. So there is nothing to strip today, and
the job is to keep it that way: a field whose NAME is in
`SECRET_BEARING_FIELD_NAMES` is refused outright, unless the dataclass
explicitly declares it in `HASH_BY_NAME_FIELDS`, in which case only its sorted
KEY NAMES enter the canonical content and the values never do. That is the
ticket's "hash by reference/name", implemented as a gate rather than as advice.

STABLE ACROSS PROCESSES
Nothing here reads `hash()` or iterates a `set`, so the encoding does not depend
on `PYTHONHASHSEED`. `tests/test_providers.py` proves that by recomputing a hash
in a fresh interpreter with randomization on, rather than asserting it twice in
the same process — where a seed-dependent bug is invisible by construction.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import json
import types
import typing
from collections.abc import Mapping, Sequence
from dataclasses import MISSING, fields, is_dataclass

#: Semver of the canonical form. It is part of the hashed content (see
#: `canonical_document`) rather than implicit, because rule 2 means ANY change to
#: the field set changes every hash anyway — so hiding the version would not have
#: bought stability, it would only have made two hashes from different versions
#: look comparable. Spec hashes are stable WITHIN a version; a consumer storing
#: them stores this alongside. Policy: docs/contract/README.md.
COMPUTATION_SPEC_VERSION = "1.0.0"

#: The digest, named on the wire so a reader never has to infer it from length
#: and so a future algorithm is a new prefix rather than a silent reinterpretation.
SPEC_HASH_ALGORITHM = "sha256"

#: The reserved key that marks a base64 `bytes` value. See rule 6.
BYTES_TAG = "__bytes_b64__"

#: Field names that carry credentials in every codebase that has one. A spec
#: field with one of these names is refused unless the dataclass opts it into
#: name-only hashing. Matched case-insensitively on the exact field name, not as
#: a substring: `code` must not be caught by `code`-adjacent heuristics, and a
#: substring rule would fire on names like `token_budget` that hold no secret.
SECRET_BEARING_FIELD_NAMES = frozenset({
    "api_key", "apikey", "auth", "authorization", "credential", "credentials",
    "env", "environ", "environment", "password", "secret", "secrets", "token",
})


class UncanonicalizableValue(TypeError):
    """A value has no canonical encoding, so no identity can be minted for it.

    Raised rather than worked around. The whole contract of this module is that
    equal requests produce equal bytes everywhere; a value that cannot honour
    that must stop the encoding, not be encoded approximately.
    """

    code = "uncanonicalizable_value"

    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"{path}: {detail}")


def _hash_by_name_fields(spec_type: type) -> frozenset[str]:
    return frozenset(getattr(spec_type, "HASH_BY_NAME_FIELDS", ()))


def _encode_names_only(value: object, path: str) -> list[str] | None:
    """A secret-bearing field reduced to its sorted KEY NAMES.

    The reference, not the referent: which variables were supplied is part of
    what makes a request distinct, what they contained is not something an
    identity may carry.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        names = list(value.keys())
    elif isinstance(value, (list, tuple)):
        names = list(value)
    else:
        raise UncanonicalizableValue(
            path, f"a name-hashed field must be a mapping, list or None, "
                  f"not {type(value).__name__}")
    if not all(isinstance(name, str) for name in names):
        raise UncanonicalizableValue(path, "a name-hashed field must name strings")
    return sorted(names)


def _encode(value: object, path: str) -> object:
    # bool BEFORE int — rule 4.
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise UncanonicalizableValue(
            path, "float has no canonical decimal form across runtimes; carry the "
                  "value as an int or a string, or extend this module deliberately")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {BYTES_TAG: base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, enum.Enum):
        return _encode(value.value, path)
    if is_dataclass(value) and not isinstance(value, type):
        return _encode_dataclass(value, path)
    if isinstance(value, Mapping):
        encoded: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise UncanonicalizableValue(
                    path, f"mapping key {key!r} is not a string; JSON has no other "
                          "key type and coercing one would collide with a real "
                          "string key")
            if key == BYTES_TAG:
                raise UncanonicalizableValue(
                    path, f"{BYTES_TAG!r} is reserved for the bytes encoding and "
                          "may not be used as a mapping key")
            encoded[key] = _encode(item, f"{path}.{key}")
        return encoded
    if isinstance(value, Sequence):
        return [_encode(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise UncanonicalizableValue(
        path, f"{type(value).__name__} has no canonical encoding rule")


def _encode_dataclass(instance: object, path: str = "") -> dict[str, object]:
    by_name = _hash_by_name_fields(type(instance))
    encoded: dict[str, object] = {}
    for field in fields(instance):  # type: ignore[arg-type]
        value = getattr(instance, field.name)
        here = f"{path}.{field.name}" if path else field.name
        if field.name in by_name:
            encoded[field.name] = _encode_names_only(value, here)
            continue
        if field.name.lower() in SECRET_BEARING_FIELD_NAMES:
            raise UncanonicalizableValue(
                here, "a secret-bearing field name may not be hashed by value; "
                      "declare it in the dataclass's HASH_BY_NAME_FIELDS so only "
                      "its key names enter the canonical content")
        encoded[field.name] = _encode(value, here)
    return encoded


def canonical_document(spec: object) -> dict:
    """The exact JSON structure that gets hashed.

    Nested under `spec` rather than flattened alongside `schema_version`, so no
    present or future field name can collide with the version key.
    """
    if not is_dataclass(spec) or isinstance(spec, type):
        raise UncanonicalizableValue(
            "<root>", "a computation spec must be a dataclass instance")
    return {"schema_version": COMPUTATION_SPEC_VERSION, "spec": _encode_dataclass(spec)}


def canonical_bytes(spec: object) -> bytes:
    """The canonical serialization. Equal requests, equal bytes, everywhere."""
    return json.dumps(
        canonical_document(spec),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_hash(payload: bytes) -> str:
    """`sha256:<hex>` over arbitrary bytes. Shared with the execution receipt."""
    return f"{SPEC_HASH_ALGORITHM}:{hashlib.sha256(payload).hexdigest()}"


def text_hash(text: str) -> str:
    """`sha256:<hex>` over text, encoded UTF-8 so the digest is locale-free."""
    return content_hash(text.encode("utf-8"))


def spec_hash(spec: object) -> str:
    """The request's content address: `sha256:<hex>` over the canonical bytes."""
    return content_hash(canonical_bytes(spec))


# ── the published schema, derived from the dataclass ────────────────────────
#
# Derived, never transcribed. A schema typed out by hand is a second declaration
# of the same field set, and the two agree exactly until someone adds a field —
# which is the drift `scripts/check_contract.py` exists to make impossible.

_SCALARS: dict[object, str] = {
    bool: "boolean",   # before int; `bool` is a subclass but a distinct annotation
    int: "integer",
    str: "string",
    type(None): "null",
}


def _json_types(hint: object, field_name: str) -> list[str]:
    args = typing.get_args(hint)
    if args and typing.get_origin(hint) in (typing.Union, types.UnionType):
        found: list[str] = []
        for arg in args:
            for name in _json_types(arg, field_name):
                if name not in found:
                    found.append(name)
        return found
    if hint in _SCALARS:
        return [_SCALARS[hint]]
    raise UncanonicalizableValue(
        field_name, f"no published JSON type for {hint!r}; the schema generator "
                    "refuses to guess rather than publish a document a strict "
                    "client would validate against wrongly")


def build_spec_schema(spec_type: type, *, field_docs: Mapping[str, str],
                      dialect: str | None = None,
                      schema_id: str | None = None) -> dict:
    """The published JSON Schema for the CANONICAL DOCUMENT, not the dataclass.

    The canonical document is what a second implementation has to reproduce, so
    it is what gets published — including the `schema_version` wrapper.

    `dialect` and `schema_id` are injected by `scripts/check_contract.py` for the
    same reason the result contract does it: `tests/test_offline.py` bans every
    public URL literal from this package, and two identifier URIs would be its
    first exceptions.

    `additionalProperties: false` on the spec object, which is the OPPOSITE of
    `docs/contract/result-v1.schema.json`'s deliberate openness. The two are
    different kinds of document. A result may grow fields, and a client must
    ignore what it does not know. A request identity may not: an unrecognised
    field either changed the computation — in which case letting it through
    silently produces two different runs sharing one hash — or it did not, in
    which case it does not belong in the canonical bytes at all.
    """
    hints = typing.get_type_hints(spec_type)
    properties: dict[str, dict] = {}
    required: list[str] = []
    missing_docs: list[str] = []
    for field in fields(spec_type):  # type: ignore[arg-type]
        description = field_docs.get(field.name)
        if not description:
            missing_docs.append(field.name)
            description = ""
        names = _json_types(hints[field.name], field.name)
        prop: dict = {"type": names[0] if len(names) == 1 else names,
                      "description": description}
        if field.default is not MISSING:
            # Documented, NOT optional: rule 2 emits every field, so a reader of
            # the canonical bytes never consults this. It records what a caller
            # may leave out at CONSTRUCTION time.
            prop["default"] = field.default
        required.append(field.name)
        properties[field.name] = prop
    if missing_docs:
        raise UncanonicalizableValue(
            ",".join(missing_docs),
            "every published field needs a description; an undocumented field in "
            "a schema a third party programs against is prose that was never written")

    identifiers: dict[str, str] = {}
    if dialect:
        identifiers["$schema"] = dialect
    if schema_id:
        identifiers["$id"] = schema_id
    return {
        **identifiers,
        "title": "codecalc canonical computation spec",
        "description": (
            f"The canonical form of a codecalc execution request, version "
            f"{COMPUTATION_SPEC_VERSION}. Serialize this document as RFC 8785-"
            f"style JSON (UTF-8, sorted keys, no insignificant whitespace) and "
            f"sha256 the bytes to obtain the request's spec_hash. Generated from "
            f"codecalc/providers.py by scripts/check_contract.py — edit the "
            f"dataclass, not this file."
        ),
        "type": "object",
        "required": ["schema_version", "spec"],
        "additionalProperties": False,
        "properties": {
            "schema_version": {
                "const": COMPUTATION_SPEC_VERSION,
                "description": (
                    "The canonical-form version these bytes were produced under. "
                    "Part of the hashed content: spec hashes are comparable "
                    "within a version and are NOT expected to survive a bump."
                ),
            },
            "spec": {"$ref": "#/$defs/computation_spec"},
        },
        "$defs": {
            "computation_spec": {
                "title": "the request fields",
                "description": (
                    "Every field is present in the canonical document even when "
                    "the dataclass supplies a default, so these bytes reconstruct "
                    "the request without consulting a default table."
                ),
                "type": "object",
                "required": required,
                "additionalProperties": False,
                "properties": properties,
            },
        },
    }
