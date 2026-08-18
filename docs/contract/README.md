# The codecalc result contract

**Current version: `1.0.0`** · Schema: [`result-v1.schema.json`](result-v1.schema.json) ·
Source of truth: [`codecalc/contract.py`](../../codecalc/contract.py)

Every codecalc tool result carries `contract_version`. This document says what
that number promises, what may change under each part of it, and how a client
written against an earlier server should migrate.

The schema is [JSON Schema 2020-12](https://json-schema.org/draft/2020-12/schema).
That dialect is not arbitrary: MCP `2026-07-28` defaults tool `inputSchema` and
`outputSchema` to 2020-12 when no `$schema` is present, so this document can be
handed to a client as an `outputSchema` and validated with no translation step.

---

## The four shapes

Every result carries `ok` and `contract_version`. Beyond that there are four
shapes, and a client discriminates them in this order:

| Shape | Discriminator | What it is |
|---|---|---|
| **rejected** | no `verdict` | Nothing ran — unknown language, malformed request, an executor that would not start. Carries `error` and a `code`. |
| **session** | `backend == "session-worker"` | Ran in a warm session worker. |
| **compact** | `verdict` present, no `backend` | `execute_code(compact=True)`. |
| **envelope** | `verdict` present, `backend` in `rust`/`python` | A fresh sandboxed run. All 21 fields. |

**The envelope** is the full shape: 21 fields including `contract_version`, identical
across both backends because `scripts/check_parity.py` runs both and diffs their
key sets — and, since this release, because `tests/test_contract.py` does the
same for the *compile* path, which the parity gate cannot reach (it probes
`python3`, an interpreted language, so nothing it compares ever compiles).

**The session shape** omits what does not apply. A warm worker has no per-run
workdir, no fresh platform to report and no compile step, so those fields are
absent rather than filled with plausible nulls. `unenforced` is required here
and is the field that matters most: a worker cannot apply several ceilings a
fresh sandbox can — the network shim is applied at exec and the worker is
long-lived — and it says so, per call.

**The compact shape** drops diagnostics to save tokens. It never drops
`unenforced` or `output_error`, which is the difference between the current
implementation and the one that was a defect (#117).

> An earlier version of this document claimed there were two shapes and that
> `executor.execute` was the single point every execution result passes through.
> Both were wrong, and a cross-vendor review found it: compact results, native
> streaming and session workers all returned shapes the published schema
> rejected. The version is now stamped at the MCP tool boundary, which is the
> only place that actually reaches all 51 tools.

Padding the short shapes with nulls so one schema fits everything was considered
and rejected. It would have made "we could not tell you the exit code" and "the
exit code was null because the process was killed" the same value.

---

## Versioning policy

`contract_version` is [semver](https://semver.org). What each component is
allowed to change:

| Component | May change | Examples |
|---|---|---|
| **MAJOR** | Anything a reader can break on | Removing a field. Changing a field's type. Changing what a value means. **Adding or removing a member of the `code` or `verdict` enum.** Changing which shape a situation returns. |
| **MINOR** | Additions only | A new field. A new result shape. A newly populated field that was always allowed to be null. |
| **PATCH** | Nothing on the wire | Description text, documentation, examples. |

> **Why enum *expansion* is MAJOR and not MINOR.**
> An earlier draft of this policy called adding an error code or verdict a MINOR
> change, on the strength of the "treat an unknown `code` as `internal`" rule
> below. That was wrong, and a cross-vendor review caught it. `code` and
> `verdict` are **closed enums** in the published schema, so a client validating
> strictly against `1.0.0` rejects a result carrying a ninth code *before* any
> application logic gets to apply the unknown-code rule. Open
> `additionalProperties` makes new *fields* additive; it does nothing for enum
> members. Either the enums are closed and expansion is breaking, or the schema
> accepts arbitrary strings and stops catching a typo. This contract chooses
> closed enums and an honest MAJOR, which also matches the taxonomy's own
> design: eight deliberately coarse buckets, not a catalogue that grows.
>
> The unknown-code rule still earns its place — it is what lets a `1.x` client
> survive contact with a `2.0.0` server instead of crashing.

### The two rules a conforming client MUST follow

1. **Ignore fields it does not recognise.** The schema leaves
   `additionalProperties` open for exactly this reason. A client that rejects
   unknown fields turns every additive change into a breaking one, and the MINOR
   row above stops being true for it.

2. **Treat an unrecognised `code` as `internal`.** This does *not* make adding a
   code a MINOR change — see the note above; a strict validator rejects the
   result first. What it does is let a `1.x` client survive contact with a
   `2.0.0` server instead of crashing on a value it has never seen. The server
   already applies the rule to itself: `errors.error_result()` degrades an
   unknown code to `internal` and says so in the message rather than passing it
   through.

A client that follows both can be written against `1.0.0` and keep working
across every `1.x` without changes.

### Deprecation

Nothing in a MAJOR is removed without notice. A field or enum member being
retired is first marked deprecated in this document and in the schema
description, and **keeps working for at least twelve months** from that mark.

That window is deliberately the same as
[MCP's own deprecation policy](https://blog.modelcontextprotocol.io/posts/2026-07-28/),
which guarantees twelve months minimum. A server that promised its callers less
than the protocol underneath it promises would be the shorter of the two in
practice, so matching it is the only number that means anything.

### How a client discovers the version

Read `contract_version` off any result. It is stamped at the **MCP tool
boundary**, which is the only place that reaches all 51 tools — so it is present
on success, on failure, on timeout, on a rejected request, on a compact result
and on a session result alike. `executor.execute()` stamps it too, for callers
using codecalc as a library rather than over MCP; the stamp uses `setdefault`,
so the two cannot disagree.

`codecalc doctor` also prints it, for checking a deployment without making a
tool call.

---

## Error codes

Eight, deliberately coarse: a caller should be able to do something *different*
for each one.

| `code` | Means | Remedy |
|---|---|---|
| `validation` | The request was malformed or out of range | Fix the arguments; the message names the field |
| `runtime_unavailable` | A language or compiler is not installed here | Install it, or call `list_languages` |
| `timeout` | A wall-clock or CPU deadline expired | Raise `timeout` or reduce the work |
| `resource_exhausted` | Memory, output or process ceiling hit | Raise the ceiling or reduce the work |
| `permission_denied` | Refused by a jail, ACL or elevation gate | It will not succeed on retry |
| `dependency_missing` | An optional extra is not installed | Install the named extra; the message gives the pip command |
| `worker_failure` | A session worker died or desynced | `session_stop` and start a new one |
| `internal` | A defect in codecalc, not in the request | Worth reporting verbatim |

`remedy` travels with the code in the result, so the fix does not live only in
this table.

### `code_inferred`

A failing result may carry `code_inferred: true`. That means the code was
derived by matching the error *message*, not chosen where the failure was
raised — a weaker claim, marked as such so a caller can tell the difference.

Its absence is the strong claim. `scripts/check_claims.py` counts inferred codes
and floors the total, so the transitional half is visible and shrinking rather
than quietly permanent.

---

## Truncation, and how much was cut

`output_truncated` says output was cut. `stdout_bytes` and `stderr_bytes` say by
how much: they are the bytes the **program** produced, before the response cap.

```
max_output_kb=1, program prints 200 000 characters:

  output_truncated  true
  len(stdout)       1039        <- what you received
  stdout_bytes      200001      <- what it printed
```

Without the counts, "printed 9 KiB" and "printed 4 MB" were the same answer, and
there was no way to size a retry. With them, a caller can decide whether raising
`max_output_kb` is worth it or whether the program is the problem.

> **When `output_truncated` is true, the count is a LOWER BOUND, and the two
> backends can disagree.** They enforce the cap differently: the pure-Python
> fallback kills the process as soon as its drain crosses the cap, while the
> native executor lets it run on under a separate file-size ceiling. Measured on
> the same 200 KiB writer at `max_output_kb=1`: native reports `204800`, the
> fallback `65536`. Both are honest reports of what each observed before its own
> enforcement stopped the program; neither is "what the program would have
> printed", and no number available to either backend is.
>
> When `output_truncated` is **false**, the count is exact and both backends
> agree. An earlier version of this section promised the exact original size
> unconditionally. That was true for a program that prints everything in one
> burst and false for one that writes slowly, which is the shape of claim a
> cross-vendor review exists to catch.

Four things to know:

- **`null` means not measured, never zero.** A program that printed nothing
  reports `0`. `null` appears where nothing ran (a runtime that failed to spawn,
  an executor that could not create its I/O files), where the output stream
  could not be read to completion, and where the native backend could not stat
  its output file. In every one of those the alternative would have been an
  integer that looked exact and was not.
- **They count the program's output, not the field next to them.** On a timeout,
  `stderr` carries codecalc's own `<killed: exceeded wall-clock timeout>` while
  `stderr_bytes` is `0`, because the *program* wrote nothing. The message is
  ours; the count is the child's.
- **A program stopped by the file-size ceiling wrote less than it wanted.** The
  count is bytes successfully written. No number available anywhere could say
  how much more it intended, because that limit is enforced by the kernel
  against the file rather than by codecalc against the program.

- **The count is sampled when the direct child has exited**, not when every
  descendant has. A grandchild that inherited the output descriptor can still be
  writing, so on the native backend a count taken at that moment is what the file
  held then. The fallback reports `null` rather than a number when its reader
  threads have not finished.

The native executor reads the count from the output file's metadata and the
fallback from its drain's running total — neither re-reads the output, because
both deliberately stop buffering at the cap. That is why the count could not
simply be `len(stdout)`.

## Backends are not identical, and the contract says where

`backend` is `rust` or `python`. The fallback enforces strictly less, and the
contract exposes exactly where rather than papering over it:

- **`unenforced`** lists bounds this host could not apply, by name. An empty
  array is a positive claim that everything requested was enforced. A non-empty
  one is the sandbox telling you what it did not do.
- **`MLE` is native-only.** The Rust path infers a memory kill from a signal
  plus an RSS reading near the cap. The fallback has neither, so an OOM there is
  reported as `RTE` rather than guessed as `MLE`. `scripts/check_contract.py`
  re-derives both backends' verdict vocabularies from source and fails if this
  asymmetry stops being true.

---

## Examples

Real transcripts, captured by running the product. `workdir`, timings and
`peak_memory_kb` vary per run; everything else is stable.

### Success

```json
{
  "ok": true,
  "contract_version": "1.0.0",
  "language": "python3",
  "phase": "run",
  "backend": "rust",
  "platform": "linux",
  "stdout": "42\n",
  "stderr": "",
  "exit_code": 0,
  "timed_out": false,
  "verdict": "OK",
  "output_truncated": false,
  "output_error": null,
  "stdout_bytes": 3,
  "stderr_bytes": 0,
  "duration_ms": 55,
  "compile_ms": 0,
  "total_ms": 55,
  "cpu_ms": 34,
  "peak_memory_kb": 15192,
  "unenforced": [],
  "workdir": "/tmp/codecalc-1788688-2b2fb109"
}
```

### The program ran and failed

Note `ok: false`. It reports "ran and exited 0", not "codecalc worked" — a
program that behaves exactly as intended and exits 3 lands here. Read `verdict`
and `exit_code` to tell a failed *program* from a failed *request*; a failed
request has a `code` and no `verdict`.

```json
{
  "ok": false,
  "contract_version": "1.0.0",
  "language": "python3",
  "phase": "run",
  "backend": "rust",
  "platform": "linux",
  "stdout": "",
  "stderr": "boom\n",
  "exit_code": 3,
  "timed_out": false,
  "verdict": "RTE",
  "output_truncated": false,
  "output_error": null,
  "stdout_bytes": 0,
  "stderr_bytes": 5,
  "duration_ms": 51,
  "compile_ms": 0,
  "total_ms": 51,
  "cpu_ms": 33,
  "peak_memory_kb": 15188,
  "unenforced": [],
  "workdir": "/tmp/codecalc-1788691-2ed8cc1c"
}
```

### Timeout

`exit_code` is `null` because the process was killed rather than exiting, and
`stderr` says which clock did it. No partial result is returned.

```json
{
  "ok": false,
  "contract_version": "1.0.0",
  "language": "python3",
  "phase": "run",
  "backend": "rust",
  "platform": "linux",
  "stdout": "",
  "stderr": "<killed: exceeded wall-clock timeout>",
  "exit_code": null,
  "timed_out": true,
  "verdict": "TLE",
  "output_truncated": false,
  "output_error": null,
  "stdout_bytes": 0,
  "stderr_bytes": 0,
  "duration_ms": 1022,
  "compile_ms": 0,
  "total_ms": 1022,
  "cpu_ms": 28,
  "peak_memory_kb": 15252,
  "unenforced": [],
  "workdir": "/tmp/codecalc-1788693-320c67bb"
}
```

### Rejected before execution

No `verdict`, because nothing ran.

```json
{
  "ok": false,
  "contract_version": "1.0.0",
  "backend": "rust",
  "code": "validation",
  "error": "unknown language 'nosuchlang'. Available: python3, node, bun, deno, ...",
  "remedy": "fix the arguments and retry; the message names the field"
}
```

### A coded failure from a symbolic tool

Symbolic tools do not produce the execution envelope — nothing was executed —
but they carry the same error half, so a caller branches on `code` identically.

```json
{
  "ok": false,
  "contract_version": "1.0.0",
  "code": "validation",
  "code_inferred": true,
  "error": "Sympify of expression 'could not parse 'x +++ ***'' failed ...",
  "remedy": "fix the arguments and retry; the message names the field"
}
```

---

## Migration from a pre-`1.0.0` server

Servers before this release emitted no `contract_version`. **Absence of the
field means `0.x`** — a contract that made no promises. Three changes matter:

| If your client... | Before | From `1.0.0` | What to do |
|---|---|---|---|
| Branches on error text | Free-form English, 121 distinct strings | `code` from a fixed set of 8, plus `remedy` | Branch on `code`. Keep the text for humans only. |
| Reads `output_truncated` | Emitted by the Python fallback, **absent** from the Rust backend even though it computed the value and raised `OLE` from it | Emitted by both | Delete any `backend == "rust"` special case. Fixed in [#120](https://github.com/The-40-Thieves/codecalc/pull/120). |
| Detects the contract | No way to | `contract_version` on every result | Read it; treat absence as `0.x`. |

No field was removed and no field changed type, so **a `0.x` client keeps
working** on `1.0.0` — it simply cannot see the new information. The migration
is opt-in, and the only thing that is genuinely retired is the practice of
matching on error prose, which was never stable enough to be a contract in the
first place.

---

## The request half: a canonical spec and its content hash

Everything above describes what comes **back**. This section describes what a
request **is**, so that two parties can agree on a name for one without shipping
the whole object around. THE-793.

`providers.ComputationSpec` is the canonical, transport-neutral request. Its
identity is a byte encoding plus a digest over it:

| Artifact | Where |
|---|---|
| Published schema | [`computation-spec-v1.schema.json`](computation-spec-v1.schema.json) |
| Golden vectors | [`computation-spec-v1.vectors.json`](computation-spec-v1.vectors.json) |
| Implementation | `codecalc/spec_identity.py` |
| Version | `COMPUTATION_SPEC_VERSION` — **1.0.0** |

The schema describes the **canonical document**, not the dataclass:

```json
{"schema_version":"1.0.0","spec":{"code":"print(1)","language":"python3","max_cpu":0,"max_memory_mb":0,"max_output_kb":0,"no_net":false,"stdin":"","timeout":10,"workdir":null}}
```

Serialize exactly that — UTF-8, object keys sorted, no insignificant whitespace —
and `sha256` the bytes. The result is prefixed with its algorithm:

```
sha256:a5093b8724c9815a4d05452288027446d17416b4009dc1e1fde9e36aea163089
```

### The canonicalization rules

They are stated here because a second implementation has to reproduce them, and
every one of them is a place where two implementations could silently disagree.

1. **JSON, UTF-8, sorted keys, `(",", ":")` separators.** This is RFC 8785 for
   the value space a spec can hold. JCS orders keys by UTF-16 code unit and this
   implementation by code point; the two can only differ above the BMP, and every
   key is a dataclass field name.
2. **Every field is emitted, defaults included.** There is no "absent". A spec
   built with defaults and one with the same values passed explicitly produce
   identical bytes, and the bytes reconstruct the request without consulting a
   default table.
3. **`null` is a value.** `workdir: null` (provider chooses) and `workdir: ""`
   (an empty path) are different requests with different hashes.
4. **Booleans are not integers.** `no_net: true` and `no_net: 1` do not collide.
5. **Array order is kept; object key order is not.** A sequence's order was
   asked for; a mapping's is an artifact of construction. Tuples and lists encode
   identically — JSON has one array type.
6. **`bytes` encode as `{"__bytes_b64__": "<base64>"}`.** A bare base64 string
   would collide with a text field holding the same characters, so the tag is
   reserved and a mapping may not use it as a key.
7. **Floats are refused.** Shortest-round-trip IEEE-754 printing is where
   canonicalizers diverge between runtimes, and NaN/Infinity are not JSON.
8. **Anything with no rule is refused** — sets above all, which have no stable
   iteration order.

### Secrets are never part of an identity

Canonical bytes are hashed, logged beside receipts and compared across
processes, so a secret in them is a secret in all three. `ComputationSpec`
carries **no** credential-bearing field today — `language`, `code`, `stdin`,
`timeout`, `workdir`, `max_memory_mb`, `max_output_kb`, `max_cpu`, `no_net` — and
the encoder keeps it that way.

A field name is **tokenised** — split on separators and camelCase boundaries —
and refused if any token is in `spec_identity.SECRET_BEARING_WORDS`. Tokenising
rather than exact-matching is load-bearing: `db_password`, `auth_token` and
`client_secret` are what a real codebase calls these things, and an exact-match
rule that only knows `password` guards a name nobody uses. Substring matching was
rejected for the opposite reason — it fires on `author`. The same words are
applied to **mapping keys** one level deeper, since a field policy cannot see
inside a `dict`.

Two escape hatches, both declared on the dataclass:

| Attribute | Effect | Use when |
|---|---|---|
| `HASH_BY_NAME_FIELDS` | Only the sorted **key names** enter the content | The field really does carry credentials |
| `NOT_SECRET_FIELDS` | The value is hashed normally | The token rule caught an innocent name (`token_budget`, `cache_key`) |

They are separate on purpose: reaching for the first to silence a false positive
would silently drop a real value out of the request's identity.

`key` is in the word set deliberately and is the aggressive entry — it catches
`private_key` and `signing_key`, and also `sort_key`. A false positive costs one
declared line; a false negative puts a private key inside a hash that gets
logged.

Changing a credential's value does not change the request's identity; adding or
removing one does.

### Versioning policy for `COMPUTATION_SPEC_VERSION`

Independent of `CONTRACT_VERSION`: they version different documents and are free
to move apart.

| Component | Means | Examples |
|---|---|---|
| **MAJOR** | An old canonical document is no longer valid, or a rule changed | Removing or renaming a field · retyping one · changing a canonicalization rule · changing a default |
| **MINOR** | Additive: new optional field, new documented rule for a value kind that was previously refused | Adding a field with a default |
| **PATCH** | Documentation and descriptions only; the bytes are untouched | Rewording a field description |

**Read this before you store a hash.** Rule 2 means MINOR is additive for the
*schema* but **not** hash-preserving: adding a field changes every canonical
document, and therefore every hash. That is the price of bytes that stand alone,
and it is stated rather than papered over. A consumer that persists spec hashes
**must** persist `schema_version` beside them and treat hashes from different
versions as incomparable rather than unequal.

### Changing the request contract

1. Edit `providers.ComputationSpec` and its entry in
   `COMPUTATION_SPEC_FIELD_DOCS` — the schema generator refuses to publish an
   undocumented field.
2. `python scripts/check_contract.py --write` regenerates
   `computation-spec-v1.schema.json`.
3. Bump `COMPUTATION_SPEC_VERSION` per the table above and say so here.
4. **Hand-edit** `computation-spec-v1.vectors.json`. Nothing regenerates it, on
   purpose: a vector its own generator rewrites cannot detect a change to the
   encoding, which is the one failure a schema check cannot see — the field set
   never moves, and every previously published hash silently means something
   else.

---

## Changing this contract

1. Edit `codecalc/contract.py`. It is the single source.
2. Run `python scripts/check_contract.py --write` to regenerate the schema.
3. Bump `CONTRACT_VERSION` per the table above, and say so here.
4. `python scripts/check_contract.py` must pass. It verifies the committed
   schema matches the module, that the `code` enum is exactly
   `errors.ALL_CODES`, that the `verdict` enum is the union of what both
   backends emit (re-derived from `main.rs` and `executor.py`, not transcribed),
   and that this document names the current version.

Step 4 is the one that matters. A published schema that lags the code does not
mislead a reader the way stale prose does — it makes a strictly validating
client reject results that are correct.
