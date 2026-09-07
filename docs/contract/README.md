# The codecalc result contract

**Current version: `1.5.0`** · Schema: [`result-v1.schema.json`](result-v1.schema.json) ·
Source of truth: [`codecalc/contract.py`](../../codecalc/contract.py)

`1.5.0` is a MINOR bump over `1.4.0`. It ADDS three new `oneOf` branches —
`translation_verification`, `optimization_verification`, `edge_case_comparison`
— covering `verify_translation`, `verify_optimization` and
`compare_edge_cases`, which were previously stamped `contract_version` like
every other tool but matched none of the five execution shapes and were
explicitly scoped OUT of the "exactly one branch matches" claim by this same
document (see the removed caveat under "The five shapes" below, now "The
eight shapes"). Concretely:

- **`translation_verification`** is `verify_translation`'s result: `ok`,
  `passed`, `reason`, `matched`, `mismatched`, `inconclusive`, `total`,
  `cases[]` (`translation.aggregate`'s evidence), plus `grade`/`grade_basis`/
  `grade_rules_version` (every call is graded — see `codecalc/grades.py`).
  `translation.aggregate()` had never set `ok` at all before this bump — a
  real omission caught in review, not a shape to document around:
  `verify_optimization`'s own `{"ok": True, "accepted": False, ...}` early
  return already established that `ok` means "the tool completed and
  produced a real answer", never the verdict, so a mismatch is `ok: true`
  exactly like a pass. `aggregate` now sets it unconditionally, and this
  branch requires `ok` the same as every other shape in this document.
- **`optimization_verification`** is `verify_optimization`'s result: `ok`,
  `accepted`, `reason`, an embedded `verification` (the BARE
  `translation_verification` evidence, without the grade/`contract_version`
  wrapper — `verify_optimization` calls `verify_translation()` directly, not
  through the graded MCP tool), plus `speedup` (ratio + per-size before/after
  timings), `inference` (the per-size one-sided Mann-Whitney U test —
  `test`/`alternative`/`alpha`/`per_size[].{size,n_before,n_after,u,p_value,
  rank_biserial}`/`sizes_rejecting`/`sizes_total`/`decision_basis` —
  `_accept_decision` requires a MAJORITY of sizes to reject before `accepted`
  can be true), `min_speedup`, `language`, and `grade`/`grade_basis`/
  `grade_rules_version`. A measurement failure (baseline or candidate timing
  failed — e.g. a timeout) is `{ok: false, error, code}` with no `accepted`
  key and already matched **`rejected`** before this bump; it is deliberately
  NOT re-modeled here, so the two branches cannot both match one result.
- **`edge_case_comparison`** is `compare_edge_cases`'s **success** result:
  `ok`, `inputs`, `languages`, `divergence_count`, `results[]` (a per-input
  matrix, `{input, runs: {language: {ok, stdout, verdict, stderr}}}`), and
  `divergences[]` (`{input, languages, kind: content|order, runs}`). Its own
  refusal (`{ok: false, error: "provide at least one ... snippet"}`) already
  matched `rejected` and needed no new branch.

None of the three collides with the five execution shapes or with each
other — `accepted` and `divergence_count` appear on no other shape, and
`ok` and `contract_version` are required on all eight. A `1.4.0` client is
unaffected: nothing about the five execution shapes, the error taxonomy, or
the verdict enum moved — this bump only widens what the schema accepts, by
naming three shapes it was always silently serving.

`1.4.0` is a MINOR bump over `1.3.0`, and carries THREE additive changes
landed against the same `1.3.0` base — one bump, not three:

- It ADDS `artifacts_created` to a session-scoped result — `session_run` and
  `execute_code(session_id=...)` — naming the files that run just created or
  modified in the session workspace, as `{path, size, mime, resource}` per
  file (`resource` is a `codecalc://session/{sid}/files/{path}` URI, also
  readable via `session_read_file`). `session_run` additionally adds
  `truncated_inline`, set `true` when more artifacts exist than the reply's
  inline-content budget (8 blocks / 4 MiB) could attach — the full list
  still appears in `artifacts_created` either way. The MCP content blocks
  `session_run` attaches alongside its JSON result (`ImageContent` for a
  small image, `EmbeddedResource` for a small text file, `ResourceLink`
  otherwise) are a TRANSPORT-level addition, not a result-v1 field — this
  document and the schema describe the JSON result only.
- It is the wire gaining `outputSchema` and `structuredContent`, described
  in the note below — a client that only read the `content` text block is
  unaffected.
- It ADDS an optional `dependencies` field to the execution envelope and to
  the session result shape (per-run dependencies for
  `execute_code`/`session_run`) —
  `[{spec, language, ok, installer, elapsed_ms, unenforced?}]`, present only
  when a run declared dependencies via a PEP 723 block or the `dependencies`
  tool argument.

All three are additions to the existing **session** shape (present on the
envelope shape too, for a workspace-only session run) and to **compact**,
which never drops any of them for the same reason it never drops
`unenforced`. None moves or removes what an existing field means, and none
is present on a result that never triggers it, so a `1.3.0` client is
unaffected by any of the three — additions only, hence MINOR.

`1.3.0` is a MINOR bump over `1.2.0`: it ADDS a `tier` field to every entry in
the `doctor` diagnostic document's `runtimes` array, plus a `tier_summary`
block alongside the existing `runtime_summary`. `tier` is a
RELIABILITY claim — how much codecalc's own CI has verified a language's
toolchain actually works — orthogonal to the existing `status` field, which is
a RESOLUTION claim about what this one machine found on PATH. The execution
**result** shape is unchanged, so a `1.2.0` result client is unaffected; only
the doctor document gained fields, hence MINOR.

`1.2.0` is a MINOR bump over `1.1.0`: it ADDS a `strict_runtime` block to the
`doctor` diagnostic document — the gVisor strict-execution boundary's measured
prerequisites (Docker present, cgroup v2, the `runsc` runtime registered, the
executor image present, and under `--deep` a real startup canary verified out of
band). The execution **result** shape is unchanged, so a `1.1.0` result client
is unaffected; the doctor schema is versioned by this same contract number
(see below), and gaining a field is an addition, hence MINOR.

`1.1.0` was a MINOR bump over `1.0.0`: it ADDED a fifth result shape
(`run_lifecycle`, for the `run_submit`/`run_inspect`/`run_cancel` background-run
tools) and added fields to existing results (an execution receipt with
`session_id`, and grade metadata). Additions only — a `1.0.0` client keeps
working. See the versioning policy below for why an addition is MINOR.

Every codecalc tool result carries `contract_version`. This document says what
that number promises, what may change under each part of it, and how a client
written against an earlier server should migrate.

The schema is [JSON Schema 2020-12](https://json-schema.org/draft/2020-12/schema).
That dialect is not arbitrary: MCP `2026-07-28` defaults tool `inputSchema` and
`outputSchema` to 2020-12 when no `$schema` is present, so this document can be
handed to a client as an `outputSchema` and validated with no translation step.

**Updated, 2026-09-07 — `outputSchema` is now emitted for most tools, and it
is a looser document than this one.** Most tools are now annotated
`-> dict[str, Any]` (previously a bare `-> dict`, which the SDK cannot
schematise at all — verified, SDK 2.0.0: a bare `-> dict` return yields
`output_schema: None` and no `structuredContent`, while `-> dict[str, T]`
yields both). That flips a typed tool onto the SDK's structured-output path,
so its `tools/list` entry carries an `outputSchema` and every `tools/call`
result carries `structuredContent` alongside the unchanged text block.

Three tools are deliberately left untyped, because their return value is not
always a dict:

- `session_read_file` can return a raw `ImageContent` block instead of a
  dict (`as_image=True`, or any image file read without it).
- `session_run` can return a list of content blocks
  (`[TextContent, *artifact blocks]`) when the run created artifacts.
- `list_languages`/`list_execution_providers` return `list[dict]`, which
  the SDK schematises fine on its own (measured: identical output schema
  either way) — not an exception in practice, just not the `dict[str, Any]`
  form the other 49 tools took.

Annotating either of the first two as a `dict[str, Any] | ImageContent`/
`dict[str, Any] | list[...]` union builds a schema, but the SDK wraps EVERY
Union return in `{"result": ...}` (measured), which is a wire-shape change
to a path that already works — and for `session_run` specifically, a caller
whose run produced artifacts would then fail the SDK's own output
validation ("validation error for DictModel ... Input should be a valid
dictionary") instead of getting a result. So `session_read_file` and
`session_run` keep their pre-existing untyped return, `outputSchema` stays
`None` for those two, and `structuredContent` stays unpopulated for them —
nothing else about their behavior changed.

What actually gets published is **not** this file. The SDK derives
`outputSchema` mechanically from the Python return-type annotation, and
`dict[str, Any]` produces the generic, permissive shape Python's typing gives
it license to produce — measured on `execute_code`:

```json
{"type": "object", "additionalProperties": true, "title": "execute_codeDictOutput"}
```

— an object with no fields, no `required`, no enums. This document's schema
(the eight shapes, the 21 envelope fields, the verdict/code enumerations) is
still the one worth validating against; it is just not the one a client reads
back from `tools/list`. Getting the SDK to emit *this* schema verbatim would
need a `TypedDict` (or `BaseModel`) per shape, one of which — `run_lifecycle` vs
`envelope` vs `rejected` — a tool can return conditionally at runtime, and that
is future work, not done here. Until then: `outputSchema` on the wire tells a
client "this is a JSON object", which is strictly true and worth having, and
this document tells it what the object actually contains.

One caveat for a client UI: in some MCP clients `structuredContent` displaces
the `content` text block's default display. Nothing is actually lost — the SDK
still emits the text block on every call, `tests/_mcp_client.py`'s `data()`
helper still reads it as a fallback, and a client that only speaks `content`
still works exactly as before.

---

## The eight shapes

Every result carries `ok` and `contract_version`, and a client discriminates
the eight shapes in this order:

| Shape | Discriminator | What it is |
|---|---|---|
| **rejected** | no `verdict`, carries `error` | Nothing ran — unknown language, malformed request, an executor that would not start. Carries `error` and a `code`. Also covers a `verify_optimization` measurement failure and a `compare_edge_cases` refusal (see below) — both are `{ok: false, error, code}` with no shape-specific key, so neither needed its own branch. |
| **run_lifecycle** | a `run_id` and `state`, no `verdict`/`error`/`backend` | A background-run response from `run_submit`, `run_inspect` *while the run is still active*, or `run_cancel`. Names the run; nothing has run through it yet. |
| **session** | `backend == "session-worker"` | Ran in a warm session worker. |
| **compact** | `verdict` present, no `backend` | `execute_code(compact=True)`. |
| **envelope** | `verdict` present, `backend` in `rust`/`python` | A fresh sandboxed run. All 21 fields. |
| **translation_verification** | `passed` present | `verify_translation`'s result: did a source program and a claimed port agree, with real evidence either way. `ok` is true whenever the tool completed — including a mismatch; it is not the verdict. |
| **optimization_verification** | `accepted` present | `verify_optimization`'s result: is `candidate` a genuine, measurably-faster optimisation of `original`. |
| **edge_case_comparison** | `divergence_count` present | `compare_edge_cases`'s success result: the same logic run in N languages, and where it diverged. |

The first five are **execution** shapes — their discriminators only ever
fire for something that ran code (or explicitly refused to). The last three,
added in `1.5.0`, are **verification** shapes: `verify_translation`,
`verify_optimization` and `compare_edge_cases` were always stamped
`contract_version` by the same `stamp()` at the MCP tool boundary — every
tool gets that — but before `1.5.0` their own shapes matched none of the
five execution branches and were explicitly scoped OUT of this document's
"exactly one branch matches" claim. That claim now covers all eight: none of
the three verification shapes collides with the five execution shapes or
with each other (`accepted` and `divergence_count` appear on no other
shape), and a `verify_optimization`/`compare_edge_cases` FAILURE that
carries no shape-specific key falls through to `rejected` rather than
double-matching. `translation.aggregate()` also had a real omission behind
this gap: it never set `ok` at all, which would have made
`translation_verification` the one shape in this document without it.
Fixed at the source instead of carved into the schema — `ok` means "the
tool completed and produced a real answer", never the verdict, so a
mismatch is `ok: true` exactly like a pass (see `verify_optimization`'s own
`{"ok": True, "accepted": False, ...}` early return for the same
distinction already in the contract) — so all eight shapes require it.

**The run_lifecycle shape** (added in `1.1.0`) is the reply the background-run
tools return before a run finishes — `run_submit`'s handle, a poll of a
still-running `run_inspect`, or `run_cancel`'s acknowledgement. A *terminal*
`run_inspect` does not use it: once the run settles, `run_inspect` returns the
full **envelope** (or, on a failed run, the **rejected** error shape) merged with
the run's own extras (`run_id`, `state`, `cleaned`, …). Forbidding
`verdict`/`error`/`backend` on this shape is what keeps it from colliding with
those, so exactly one branch matches any stamped run response.

**The envelope** is the full shape: 21 fields including `contract_version`, identical
across both backends because `scripts/check_parity.py` runs both and diffs their
key sets — and, since this release, because `tests/test_contract.py` does the
same for the *compile* path, which the parity gate cannot reach (it probes
`python3`, an interpreted language, so nothing it compares ever compiles).

**The session shape** omits what does not apply. A warm worker has no per-run
workdir, no fresh platform to report and no compile step, so those fields are
absent rather than filled with plausible nulls. `unenforced` is required here
and is the field that matters most: a worker cannot apply several ceilings a
fresh sandbox can — `no_net` enforcement (seccomp where the Linux kernel
supports it, a symbol shim otherwise) is applied at exec and the worker is
long-lived — and it says so,
per call.

**`artifacts_created`** (added in `1.4.0`) is present on any session-scoped
result — this shape from a stateful `python3`/`node` worker, or the envelope
shape from a workspace-only session language — because both write into a
workspace that outlives the call. It lists, as `{path, size, mime, resource}`,
every file the run just created or modified; empty when nothing changed.
Sessionless `execute_code` (no `session_id`) never carries it: it runs in a
Rust-owned temp directory deleted the moment the call returns, so there is no
workspace left to report on. `session_run` — a dedicated tool for running a
workspace entry file, covered above under the run_lifecycle/envelope split —
carries the same field, plus `truncated_inline` when its own inline-content
budget (8 MCP content blocks / 4 MiB) could not attach every artifact; see
`CHANGELOG.md` ([Unreleased]/"Inline artifacts") or the `_MAX_INLINE_*`
constants in `codecalc/server.py` for the per-type caps (image/text/link)
that decide what an artifact becomes on the wire — kept out of the tool's
own docstring (the live `tools/list` description a client sees) to avoid
diluting it with detail a lexical tool-selector never needs.

**The compact shape** drops diagnostics to save tokens. It never drops
`unenforced` or `output_error` — or, since `1.4.0`, `artifacts_created` and
`truncated_inline` — which is the difference between the current
implementation and the one that was a defect (#117).

> An earlier version of this document claimed there were two shapes and that
> `executor.execute` was the single point every execution result passes through.
> Both were wrong, and a cross-vendor review found it: compact results, native
> streaming and session workers all returned shapes the published schema
> rejected. The version is now stamped at the MCP tool boundary, which is the
> only place that actually reaches all 52 tools.

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
| **MINOR** | Additions only | A new field. A new result shape. A newly populated field that was always allowed to be null. Narrowing a TOP-LEVEL `required` list down to what every individual `oneOf` branch already required on its own — no branch that validated before stops validating; it only admits a branch that could not previously satisfy the global requirement. |
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

### Deprecation, and the compatibility window

Nothing in a MAJOR is removed without notice. A field or enum member being
retired is first marked deprecated in this document and in the schema
description, and **keeps working for at least twelve months** from that mark.

**The compatibility window is twelve months, full stop — the same number for
every kind of deprecated thing this contract can carry:** a result field, a
`code` enum member, and a `verdict` enum member. There is no shorter window for
a smaller-looking change (e.g. one field) and no separate release-count clock
running alongside it (e.g. "N minor releases") — a single duration, so a
deprecation's expiry date is answerable by reading one mark and adding twelve
months, not by also checking how many `MINOR` releases happened to ship in
that time. Concretely: mark a member deprecated in this document and the
schema description on day zero; it keeps returning exactly as before through
every `MINOR` release for at least the next twelve months; only a `MAJOR`
release on or after that date may remove it.

That window is deliberately the same as
[MCP's own deprecation policy](https://blog.modelcontextprotocol.io/posts/2026-07-28/),
which guarantees twelve months minimum. A server that promised its callers less
than the protocol underneath it promises would be the shorter of the two in
practice, so matching it is the only number that means anything.

### How a client discovers the version

Read `contract_version` off any result. It is stamped at the **MCP tool
boundary**, which is the only place that reaches all 52 tools — so it is present
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
  "contract_version": "1.1.0",
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
  "contract_version": "1.1.0",
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
  "contract_version": "1.1.0",
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
  "contract_version": "1.1.0",
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
  "contract_version": "1.1.0",
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
the whole object around.

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

**Identity is over the request AS SPELLED, not the normalized runtime.**
`language: "python"`, `"py"` and `"python3"` all execute on python3, but they are
three different requests and get three different `spec_hash`es — the hash names
the request as given, and does not claim two spellings are the same computation.
Normalizing an alias before hashing would collapse distinct requests onto one
identity; this contract keeps them distinct. (`max_output_kb: 0` is likewise
literal-not-magic: it selects the backend's 64 KiB default, not an uncapped
stream.)

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
