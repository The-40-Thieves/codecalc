"""Canonical serialization and content identity for a computation request.

`providers.ComputationSpec` is the canonical REQUEST: a frozen,
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
the job is to keep it that way.

A field whose name TOKENISES to one of `SECRET_BEARING_WORDS` is refused
outright — and tokenising is the whole point. The first version of this module
compared the field name exactly against that set; cross-vendor review showed
`db_password`, `auth_token` and `client_secret` all sailing through by value,
because none of them is the bare word and all of them are what a real codebase
calls the thing. Tokenising splits on separators and camelCase boundaries, so
those three are caught while `author` is not.

The same words are applied to MAPPING KEYS, one level deeper, because a field
policy cannot see inside a `dict` and `{"auth_token": ...}` would otherwise hash
by value with nothing to stop it.

Two escape hatches, both class attributes, both a reviewable diff:

  * `HASH_BY_NAME_FIELDS` — hash only the sorted KEY NAMES, never the values.
    That is the ticket's "hash by reference/name".
  * `NOT_SECRET_FIELDS` — the token rule caught an innocent name (`token_budget`,
    `cache_key`); keep hashing the value. Separate from the first on purpose:
    reaching for `HASH_BY_NAME_FIELDS` to silence a false positive would silently
    drop a real value out of the identity.

`key` is in the word set deliberately, and it is the aggressive entry: it catches
`private_key` and `signing_key`, and it also catches `sort_key`. A false positive
here is one declared line in `NOT_SECRET_FIELDS`; a false negative is a private
key inside a hash that gets logged. The loud direction is the right one.

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
import re
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

#: Words that carry credentials in every codebase that has one.
#:
#: Matched per NAME TOKEN, not against the whole name and not as a substring.
#: The first version of this module compared the field name EXACTLY against this
#: set, and cross-vendor review demonstrated in three lines what that protects:
#:
#:     db_password, auth_token, client_secret   ->  all hashed by VALUE, unrefused
#:
#: None of them is the bare word, and all of them are what a real codebase calls
#: the thing. A gate that only knows `password` guards a name nobody uses.
#:
#: Substring matching was rejected for the opposite failure — it fires on
#: `author` because `auth` is inside it. Tokenising splits on separators AND on
#: camelCase boundaries, so `db_password`, `authToken` and `client_secret` are
#: caught while `author` (one token, not in the set) is not.
SECRET_BEARING_WORDS = frozenset({
    "api_key", "apikey", "auth", "authorization", "credential", "credentials",
    "env", "environ", "environment", "key", "password", "secret", "secrets",
    "token",
})

#: Retained under its original name because the docs and tests refer to it, and
#: because "the words that make a field name secret-bearing" is what it always
#: meant. The rename to `SECRET_BEARING_WORDS` is the accurate one.
SECRET_BEARING_FIELD_NAMES = SECRET_BEARING_WORDS

#: Splits `db_password` and `authToken` alike: on any run of non-alphanumerics,
#: and on a lower-to-upper case boundary.
_NAME_TOKENS = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def secret_bearing(name: str) -> bool:
    """True when `name` reads as credential-bearing under the token rule."""
    tokens = {part.lower() for part in _NAME_TOKENS.split(name) if part}
    tokens.add(name.lower())
    return bool(tokens & SECRET_BEARING_WORDS)


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
            # A field-name policy cannot see inside an arbitrary mapping, so a
            # `metadata: dict` holding {"auth_token": ...} would hash its value
            # with nothing to stop it. Same rule, same words, one level deeper —
            # which is what makes "secrets never enter an identity" true rather
            # than true-of-fields.
            if secret_bearing(key):
                raise UncanonicalizableValue(
                    f"{path}.{key}",
                    "a mapping key that names a credential may not be hashed by "
                    "value; carry the secret outside the spec, or reduce the "
                    "mapping to its key names via HASH_BY_NAME_FIELDS")
            encoded[key] = _encode(item, f"{path}.{key}")
        return encoded
    if isinstance(value, Sequence):
        return [_encode(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise UncanonicalizableValue(
        path, f"{type(value).__name__} has no canonical encoding rule")


def _encode_dataclass(instance: object, path: str = "") -> dict[str, object]:
    spec_type = type(instance)
    by_name = _hash_by_name_fields(spec_type)
    not_secret = frozenset(getattr(spec_type, "NOT_SECRET_FIELDS", ()))
    encoded: dict[str, object] = {}
    for field in fields(instance):  # type: ignore[arg-type]
        value = getattr(instance, field.name)
        here = f"{path}.{field.name}" if path else field.name
        if field.name in by_name:
            encoded[field.name] = _encode_names_only(value, here)
            continue
        if field.name not in not_secret and secret_bearing(field.name):
            raise UncanonicalizableValue(
                here, "a secret-bearing field name may not be hashed by value. "
                      "Declare it in the dataclass's HASH_BY_NAME_FIELDS to hash "
                      "only its key names, or — if the token rule caught an "
                      "innocent name like `token_budget` — in NOT_SECRET_FIELDS "
                      "to keep hashing the value")
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
