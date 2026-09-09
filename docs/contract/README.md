# The codecalc result contract

**Current version: `1.15.0`** · Schema: [`result-v1.schema.json`](result-v1.schema.json) ·
Source of truth: [`codecalc/contract.py`](../../codecalc/contract.py)

`1.14.0` is a MINOR bump over `1.13.0`. It adds a TENTH shape,
`execution_trace`, for the new `trace_execution` tool: the identical
`execution_envelope` `execute_code` returns for the same python3 program
(same 20 envelope keys, same `stdout`/`stderr`/`exit_code`/`verdict`/
timings), plus a per-line event trace and a static branch/line-coverage
report computed from an AST parse of the submitted source —

* **`events`** — ordered `{step, line, event, func, locals}` for USER-CODE
  frames only (library/stdlib frames excluded by filename). `event` is
  `line`/`call`/`return`/`exception`; `locals` on every event is only the
  names that CHANGED since the previous event in that same frame (never a
  full dump each step), each repr capped at ~200 chars. A `return` event
  additionally carries `return_value`; an `exception` event carries
  `exception_type`/`exception_message`. CPython's own trace protocol fires a
  spurious `return` event with `arg=None` for a frame an exception is
  UNWINDING through (indistinguishable, read naively, from a function that
  genuinely returned `None`) — the harness tracks which frames are
  currently unwinding and suppresses that event, so a `return` in `events`
  always means the function actually returned.
* **`event_count`** / **`steps_before_truncation`** — the former is
  `len(events)`; the latter is always equal to it by construction (nothing
  is recorded once tracing stops) and exists because a caller reasoning
  about a truncated trace wants the count of what stopped tracing without a
  second lookup.
* **`truncated`** / **`truncated_reason`** (`max_events`, an internal
  trace-byte ceiling, or `trace_file_exceeded` — present only when
  `truncated` is true) — the first two caps stop RECORDING, never the
  program: the underlying `exec()` keeps running to completion at full
  speed regardless, so `stdout`/`exit_code`/`verdict` are always the real,
  complete ones even when `events` is partial. A hard kill (TLE/OLE/MLE)
  before either cap trips reports `truncated: false` — that story is
  already told by `verdict`/`timed_out`/`output_truncated`, and this field
  is deliberately scoped to this tool's own recording caps rather than
  overloaded with an unrelated meaning. `trace_file_exceeded` is different
  in kind from the other two — see `discarded_events`/`events_consistent`
  immediately below.
* **`discarded_events`** / **`events_consistent`** — the trace is produced
  BY the traced process, at the SAME privilege it runs with, so a
  sandboxed program can derive the trace file's own path from `__file__`
  and write to it directly (a cross-vendor review reproduced a forged
  trailing `return` event this way, using `os._exit(0)` to skip the
  harness's own cleanup). This tool cannot make the trace an attestation of
  behaviour — it remains exactly as trustworthy as that program's own
  stdout — but it bounds and surfaces tampering rather than trusting the
  file blindly: the (unsandboxed) parser never reads more than
  `_MAX_TRACE_BYTES + 4 KiB` off disk regardless of how much a program
  appends (`trace_file_exceeded` fires when the file is bigger than that),
  every candidate line is schema-validated (unknown keys, wrong types, or a
  `step` that skips or repeats the harness's own monotonic sequence are
  DISCARDED and counted in `discarded_events`, never raised), and the
  harness writes a final `{"event": "end", "step": N, "emitted": N}` line
  on every path it returns through normally — `events_consistent` is
  `false` whenever that line is missing, its count disagrees with what this
  parser actually accepted, or anything follows it. That catches crude
  tampering, not a deliberate forger: the program can read its own trace
  file, append a step-continuous forged event plus a matching `end` line,
  and exit before the harness writes the real one, and the parser accepts
  that as consistent. `events_consistent: true` is therefore not proof of
  an untampered trace; the trace is exactly as trustworthy as the program's
  own stdout. See `codecalc/tracing.py`'s module docstring, "TRUST
  BOUNDARY" section, for exactly what this does and does not guarantee.
* **`branches`** — source line number (string key) -> execution count, for
  every `if`/`elif`/`while`/`for`/`try` line, from a static `ast` parse of
  the submitted code (never from the trace alone, so a branch that never
  executed still gets a `0` entry rather than being absent).
* **`lines_executed`** / **`lines_never_executed`** — the cheap "which
  branch actually ran" view: every statically-detected executable line,
  split by whether a `line` event ever fired on it.

Two more differences from `execute_code` are disclosed through the
EXISTING `unenforced` array rather than a new field, because both describe
a guarantee this tool could not fully apply rather than new information
about the traced program:

* **`"threads: only the main thread is traced"`** — `sys.settrace` is a
  per-THREAD hook; a program that starts its own thread runs code this
  tracer never sees events for (that code still executes normally — only
  `events`' completeness is affected, never `stdout`/`exit_code`). Added
  whenever the harness observes more than one thread alive at any point.
* **`"exit_code: fallback backend may differ on output-limit kills"`** —
  the pure-Python fallback's own OLE enforcement polls a flag on a 20ms
  timer, racing the child's natural exit; that race is PRE-EXISTING and
  already nondeterministic for plain `execute_code` on this backend, and
  this tool's extra per-event file I/O shifts its timing enough to make
  `trace_execution`'s `exit_code` on an OLE verdict, fallback backend only,
  unreliable to compare against `execute_code`'s for the same program —
  `verdict`/`output_truncated` are unaffected. Added only when
  `backend == "python"` and `verdict == "OLE"`; the Rust backend's own cap
  enforcement is not timing-sensitive this way.

All of it is additive: a `1.12.0` client reading `execute_code`'s own
`execution_envelope` shape sees no change, because `trace_execution` is a
new tool with a new shape, not a change to an existing one.
`execution_trace` would otherwise ALSO match `execution_envelope` (this
schema's `additionalProperties` is intentionally open, so extra keys alone
never disqualify a match) — closed by adding `not: {required: [events]}`
to `execution_envelope`'s own definition, the same technique `compact`
already uses against `backend` and `rejected` against `verdict`, so
`oneOf`'s "exactly one shape matches" claim still holds for a trace result,
a plain envelope, and every other shape in this document.

`1.15.0` is a MINOR bump over `1.14.0`. It adds an ELEVENTH shape,
`session_snapshot_result`, for the new `session_snapshot` tool — archive a
session's workspace files to a snapshot stored outside the jailed workspace,
and restore one into a new or the same session. Purely additive: nothing an
existing `1.14.0` client already reads changes shape or meaning: no existing
tool's result gained, lost, or renamed a field, and the new shape's own
discriminator (`action`) appears on none of the other ten. A failing
`session_snapshot` call was already representable — it matches `rejected`,
same as every other tool — so this bump is the SUCCESS shape becoming
describable, not a new failure mode. See CHANGELOG.md's `[Unreleased]` entry
for the tool's own design (caps, the identity checks at save/replace, the
closed-enum refusals a hostile archive gets on restore) and "Examples" below
for one real transcript per action.

`1.13.0` is a MINOR bump over `1.12.0`, on the **doctor** schema
(`doctor-v1.schema.json`) rather than the execution result — the two share
this one version and policy, see "Two version numbers, on purpose" below.
It adds `probe_ms` to every `runtimes[]` row a `--deep` version probe was
attempted for, and narrows what a version-probe **timeout** is allowed to
mean, closing four separate 2026-09-08 hosted-runner failures (main among
them): three were a cold `windows-latest` runner's FIRST `rustc --version`
going through the rustup proxy — an arg-forwarding shim that has to locate
and re-exec the real toolchain component before it can answer anything — and
exceeding the probe's 10-second deadline; a fourth, on a later commit, was
plain `go version` doing the same with no proxy involved and no per-command
override for `go` at all. `_probe_version` reported that exactly like a
spawn failure (`hard_failure=True` either way), and `report()` trusted the
confirmed `_VERSION_FLAG` entry (both `rustc` and `go` are audited, GNU-style
commands) the same way it trusts a genuine nonzero exit, so a `tested`-tier
runtime that was never actually broken read `unhealthy`, `healthy` flipped
`false`, and `tests/test_doctor.py`'s real-host `--deep` assertions (added
for the earlier go/lua/zig regression, #282 — they assert every resolved
`tested`-tier language stays installed/available) failed alongside it. A
timeout means
"the probe did not answer within the deadline"; it is not evidence the
runtime is broken, and this version stops treating the two as the same
claim: `_probe_version` now returns `hard_failure=False` for a timeout, and
`report()` never promotes a bare timeout to `unhealthy` regardless of
whether the command has a confirmed `_VERSION_FLAG` entry — only a
`_HELLO` runtime whose own hello-world run independently fails still gets
demoted, exactly as it did before. Two mitigations reduce how often the
timeout fires at all: one retry (a slow-start proxy is warm on its second
call) and a raised per-command deadline for the toolchains audited as
routing through this shape on a cold host (`rustc`/rustup, `dotnet`, `java`,
`kotlinc`, `swift`). `probe_ms` is the new field: the total wall time a
`--deep` version probe took across every attempt, present whenever one was
attempted at all — a `1.12.0` client that already reads `runtimes[].status`
sees no change in what a nonzero exit or a spawn failure demotes; only the
timeout case narrows, and only in the direction of trusting less, so no
existing `unhealthy` classification is undone by this.

`1.12.0` is a MINOR bump over `1.11.0`. It adds `stdout_raw` to every
`translation_verification` case's `source`/`target` (`_translation_side_
properties`) and to every `compare_edge_cases` per-language run
(`_edge_case_run_properties`), closing part of codecalc #286: `_normalize`
(`translation.py`) used `str.splitlines()` to compare two programs' stdout,
which treats VT (0x0B), FF (0x0C), FS/GS/RS (0x1C-0x1E), NEL (0x85), U+2028
LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR as line breaks and folds all
seven into `\n` — so a source printing a real `\n` and a port printing U+2028
compared equal, and the `stdout` shown in the result was the NORMALIZED
string, so a reader had no way to see the difference even after being told
where to look. `_normalize` itself is fixed (see `translation.
NORMALIZE_TOLERANCE`: only `\r\n`/`\r` -> `\n`, trailing per-line spaces/tabs,
and trailing blank lines are tolerated now — nothing else Python calls a line
or whitespace boundary is touched), which is a behavior change with no shape
to model in a schema. `stdout_raw` is the shape change: exactly what a side
wrote, before normalization, sitting next to the normalized `stdout` so a
reader can see the two are the same string, or are not, without re-running
anything. Both fields are always present, not only when they differ — a
`1.11.0` client that already reads `stdout` sees no change; `stdout_raw` is
purely additive, hence MINOR.

`1.11.0` is a MINOR bump over `1.10.0`. It adds four fields to
`optimization_verification.inference`, closing a false accept reproduced live
in CI: `verify_optimization` certified IDENTICAL before/after code as a
verified speedup (macOS sandbox job, native executor, main; measured ratio
1.21x, `sizes_rejecting` 2/3, a bare majority satisfied). All four are
additive — nothing a `1.10.0` client already reads changes shape or meaning:

* **`method`, on every `inference.per_size[]` row.** `stats.mann_whitney_u`
  already computes `"exact"` or `"normal_approximation"` per test; `_infer_
  speedup` computed it and threw it away, so a caller could not tell an exact
  p-value from an asymptotic one produced at n=5 — a sample size `stats.py`'s
  own module docstring says is "not a number worth calling alpha=0.05
  against". Both of the CI incident's "rejecting" sizes were `normal_
  approximation`, and this is why a caller can now see that for themselves.
* **`ratio`, on every `inference.per_size[]` row.** That size's OWN
  before/after ratio, the same computation `speedup.per_size` already
  reports per position. `sizes_rejecting` now requires it to clear the
  caller's `min_speedup` as well as `p_value < effective_alpha` — a size can
  be statistically significant on a difference too small to be the speedup
  the caller asked for, and `p_value` alone could not previously show that.
* **`effective_alpha` and `correction`, on `inference` itself.** The
  family-wise-error-controlled bar `sizes_rejecting` is actually compared
  against: `"unanimity"` (every counted size must reject; `sizes_total <= 3`)
  or `"bonferroni"` (`alpha / sizes_total` per size, a majority of those
  required; `sizes_total > 3`), or `"n/a"` when `sizes_total` is 0. `alpha`
  itself is unchanged in meaning — it stays the nominal, uncorrected level a
  caller already reads. A bare majority at the nominal `alpha`, always, is
  what let the CI incident's two false-positive sizes (out of three) carry
  the vote; unanimity at `sizes_total <= 3` alone would already have refused
  it (2 of 3 is not 3 of 3). Unanimity over a SINGLE counted size is no
  correction at all (one uncorrected test at the nominal `alpha`), so
  `accepted` is never `true` with `sizes_total` below 2; `decision_basis`
  says so in words when that is the reason.
* **`reason`, on a `sizes_below_floor[]` entry**, present only when that
  entry was excluded for one of two NEW reasons: fewer than
  `stats.min_testable_n(alpha)` runs a side (structurally unable to ever
  reject at `alpha` — replaces a flat "fewer than 2 runs" guard), or a
  `normal_approximation`-method result on fewer than 8 runs a side whose two
  samples' raw-run ranges overlap (the literal "the timer cannot always say
  which side a given pair of runs favoured" case — see
  `optimization._ranges_overlap`; gating on the tie alone, without the
  overlap qualifier, was measured live as nearly blinding the tool to
  genuine wins, so it is deliberately narrower than "any tie"). An entry
  excluded for an OLDER reason (the visibility floor, an unmeasurable side)
  carries no `reason` — a `1.10.0` client that already matches `sizes_below_
  floor` entries by their original three keys (`size`/`before_ms`/`after_ms`)
  still matches exactly the entries it always did.

See `codecalc/optimization.py`'s `_infer_speedup`/`_accept_decision`/
`_fwer_correction`/`_ranges_overlap` for the full mechanism, and
`CHANGELOG.md`'s `[Unreleased]` entry for the measured before/after
false-accept counts.

`1.10.0` is a MINOR bump over `1.9.0`, and adds a new result shape plus two new
optional fields on an existing one:

* **A `comparison_rows` branch, for `compare_execution`.** Its result — the
  same code run in N languages side by side — never matched any of this
  document's `oneOf` branches; it sat outside the published contract
  entirely, the same gap `edge_case_comparison` closed for `compare_
  edge_cases` in `1.5.0`. Closing it is what makes the second change below
  possible to *describe*, not just to ship: a field added to an unmodeled
  shape is not a documented contract change.
* **`error`/`code`/`remedy`/`code_inferred` on `compare_execution`'s
  per-language `results[]` rows, and on `compare_edge_cases`'s per-language
  `runs[lang]` entries.** Before this version, a row or run that failed for
  a REQUEST-level reason — no runtime installed for that language, or a
  timeout — carried only `stdout`/`stderr`/`exit_code` (or `verdict`), same
  as an ordinary program failure. A caller could not tell "this language's
  own snippet is broken" from "this host does not have this language
  installed" without re-running that one language alone through `execute_
  code` and reading `code` there. Both tools' OUTER `ok` was, and remains,
  `True` once the comparison itself ran — it means "this tool produced a
  real table", never "every language succeeded" — so `server.py`'s `_coded`
  wrapper (which classifies a result via `errors.ensure_code`) never reached
  these nested rows either; `errors.stamp_row` is the same classification
  applied at the row level instead. An ordinary program failure (a real
  `RTE`/`OLE` `exit_code`, the code ran and failed on its own terms) still
  carries none of the four fields, matching the INTENDED reading of "The
  program ran and failed" vs "Rejected before execution" below: `code`
  is meant to mark a failed REQUEST, never a failed program. That is NOT yet
  what the top-level execution envelope itself does for every case: a plain
  RTE (`sys.exit(3)`) or a plain wall-clock timeout through `execute_code`
  comes back `code: "internal"` today (`executor.execute` never sets `error`
  for either, so `errors.ensure_code`'s message matcher falls through to
  `internal`) — pre-existing on the envelope, not something this version
  touches, and tracked separately; `errors.stamp_row` applies the rule as it
  is meant to work at the row level, not as the envelope currently does. This
  closes the SAME gap `1.9.0` closed one backend at a time (below): a
  Rust-backend spawn failure now sets `error` on the envelope itself, but
  `compare_execution`/`compare_edge_cases` never forwarded that field — on
  EITHER backend — into their own rows at all.

`1.9.0` is a MINOR bump over `1.8.0`. A spawn failure — the requested
language's runtime or compiler could not be launched at all, typically
because it is not installed — used to reach a caller two different ways
depending on backend: the pure-Python fallback set `error` (and
`code_inferred` classified it `runtime_unavailable`), while the native Rust
executor put the identical fact only in `stderr` and left `error` unset,
which classified it `internal` — the wrong remedy for "install the
language". Both backends now set `error` for this failure (worded to start
"runtime unavailable for the ... phase: ..."), and both now report
`exit_code: null` rather than the Rust backend's previous internal sentinel
`-2` — the SAME convention the contract already documented for "nothing ran"
elsewhere (see "Truncation, and how much was cut" below). The `error`
property itself is not new — it already appeared in the schema's execution
envelope shape (shared with the dead-session-worker shape) — so nothing
about the SCHEMA changed at this bump, only what a real spawn-failure result
now populates: a `1.8.0` client already tolerant of an absent `error` on the
envelope shape sees no shape it cannot parse, and one that reads `error`
when present now gets it on a failure it previously had to infer from
`stderr` prose — hence MINOR, not MAJOR.

`1.8.0` is a MINOR bump over `1.7.0`. It ADDS a `probe_error` field to the
`doctor` diagnostic document's `runtimes[]` entries, present whenever a
`--deep` version probe ran and failed — a nonzero exit, a timeout, or a
spawn error — whether or not that failure was trusted enough to move
`status`. This closes a report reproduced on macOS with no JDK:
`/usr/bin/java` (Apple's stub, always present on PATH there) exits non-zero
printing "The operation couldn't be completed. Unable to locate a Java
Runtime.", and the version probe used to read that text as a perfectly good
version string and leave `status` at `installed` — the failure was observed
and then discarded. `version` never carries a failed probe's text;
`probe_error` does.

A probe failure moves `status` to `unhealthy` only when it is TRUSTED: a
spawn failure or timeout (the probe never got an answer at all, regardless
of which flag was used) always is; a mere nonzero exit is trusted only when
the flag used is one this code has confirmed correct for that command (an
explicit override, the same list java's `-version` was already on). This
distinction exists because `--version` is a GNU convention, not a universal
one — `go --version` exits 2 (`go version` is the real form), `lua
--version` exits 1 (`-v` is correct), `zig --version` exits 1 (`zig
version` is correct) — and a nonzero exit on an UNCONFIRMED flag is reported
as merely unmeasured (`probe_error` still recorded, `status` untouched),
never as evidence the runtime itself is broken. A language with a
hello-world check (`_HELLO`) is stronger evidence than either reading and is
always consulted first.

`healthy` also gained a narrow new condition alongside the existing
"workspace writable and a backend resolved" rule: it now goes `false` when a
`tested`-tier runtime (the tier a CI job genuinely executes and checks on
every PR — see `tier_summary`) resolved and then proved broken by a TRUSTED
failure, whether a non-executable file or a failed `--deep` probe. An
`unhealthy` `best_effort`/`plan_only` runtime — java and kotlin included —
still leaves `healthy` untouched, same as before: nothing ever promised
those work. No result a `1.7.0` client already understood changes meaning —
`probe_error` is additive and absent on every row it does not apply to, and
`healthy`'s new condition only ever turns a currently-`true` value `false`
in a situation this document already called "the install cannot execute
what it advertises" — hence MINOR, not MAJOR.

`1.7.0` is a MINOR bump over `1.6.0`. It ADDS `n_after` to
`optimization_verification.speedup.per_size[]` and `size_after` to
`optimization_verification.inference.per_size[]`, both present ONLY when the
candidate's own n at that position differs from the baseline's — the
disclosure for a position `_align_sizes` (`codecalc/optimization.py`)
deliberately leaves unaligned: when the candidate exhausted its ENTIRE
rescale budget (`_MAX_RESCALE_ROUNDS`, 10^4x the starting size) without ever
clearing `optimization._VISIBILITY_FLOOR_MS`, re-measuring anyone would be
either unsafe (the candidate's exhausted n was never validated as safe for
the baseline) or pointless (the baseline already tried its own smaller
sizes and failed to help). This bump also makes the visibility-floor
exclusion rule itself ASYMMETRIC: a position now lands in
`inference.sizes_below_floor` only when the BASELINE side is unmeasurable
or never clears the floor, regardless of the candidate. The PRIOR, symmetric
rule (excluded when EITHER side was below the floor) was a bug, not an extra
safety margin: a baseline visibly costing real time, paired against a
candidate too fast to register even after exhausting its own rescale
budget, is the single most decisive result this tool can produce — the
symmetric rule discarded exactly that pairing, driving `inference.sizes_total`
to 0 (and `accepted` to `False` with "no size had enough comparable runs")
for the canonical genuine-O(1)-win case this whole auto-scale system exists
to certify. The new rule instead feeds the baseline's sample at its own n
directly against the candidate's sample at ITS OWN (necessarily larger) n —
conservative by monotonicity: a candidate still measured faster while doing
at least as much work is faster, full stop — and `size_after` on that
`per_size` entry (in both `speedup` and `inference`) discloses the
mismatched pairing rather than silently presenting it as same-n. No result
that a `1.6.0` client already understood changes meaning: a position that
was comparable before stays comparable with the same `size`/`n`, and the
new fields are additive, absent whenever the two sides ran at the same n
(still the common case) — hence MINOR, not MAJOR.

`1.6.0` is a MINOR bump over `1.5.0`. It ADDS `sizes_below_floor` to
`optimization_verification`'s `inference` object: every size excluded from
`per_size`/`sizes_total`/`sizes_rejecting` (the majority-of-sizes vote) for
ANY reason — never cleared `optimization._VISIBILITY_FLOOR_MS` within
`_timed`'s per-size auto-scale budget, an unmeasurable baseline/candidate,
or too few comparable runs — rather than tested on a still-noisy sample or
silently dropped; `before_ms`/`after_ms` are `null` when no duration was
ever recorded. See `codecalc/optimization.py`'s `_VISIBILITY_FLOOR_MS`
docstring for the bug this closes (a real, macOS-CI-observed false rejection
of a genuine 1.9-2.4x speedup, because the OLD floor was applied across all
sizes combined instead of per size). `sizes_below_floor` is `required`
(always present on `inference`, an empty list when every size cleared every
bar, the common case), same as `sizes_rejecting`/`sizes_total` always are —
the same choice `tier` made at `1.3.0`, and with the same consequence: a
strict validator applying the `1.6.0` schema to a result STORED before this
bump (shaped like `1.5.0`, with no `sizes_below_floor` key at all) rejects
it as missing a required field, exactly as it would a pre-`1.3.0` doctor
document missing `tier`. No existing key moves or changes meaning on either
result a `1.5.0` client already understood, so that client is unaffected —
additions only, hence MINOR.

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
  through the graded MCP tool), plus `speedup` (ratio +
  `per_size[].{n,before_ms,after_ms,ratio,n_after}` — `n_after`, added in
  `1.7.0`, present only when the candidate ran at a different n than `n`),
  `inference` (the per-size one-sided Mann-Whitney U test —
  `test`/`alternative`/`alpha`/`per_size[].{size,n_before,n_after,u,p_value,
  rank_biserial,size_after}` (`size_after`, `1.7.0`, present under the same
  condition as `speedup`'s `n_after` — note `n_before`/`n_after` here are
  SAMPLE COUNTS, not sizes; `size_after` is the candidate's n)/
  `sizes_rejecting`/`sizes_total`/
  `sizes_below_floor[].{size,before_ms,after_ms}` (added in `1.6.0`;
  ASYMMETRIC as of `1.7.0` — excluded only on the BASELINE's failure, never
  the candidate's)/`decision_basis` —
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
  form the other 50 tools (including `trace_execution`, added `1.14.0`) took.

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
(the eleven shapes, the 21 envelope fields, the verdict/code enumerations) is
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

## The eleven shapes

Every result carries `ok` and `contract_version`, and a client discriminates
the eleven shapes in this order:

| Shape | Discriminator | What it is |
|---|---|---|
| **rejected** | no `verdict`, carries `error` | Nothing ran — unknown language, malformed request, an executor that would not start. Carries `error` and a `code`. Also covers a `verify_optimization` measurement failure, a `compare_edge_cases` refusal, and any failing `session_snapshot` call (see below) — all three are `{ok: false, error, code}` with no shape-specific key, so none needed its own branch. |
| **run_lifecycle** | a `run_id` and `state`, no `verdict`/`error`/`backend` | A background-run response from `run_submit`, `run_inspect` *while the run is still active*, or `run_cancel`. Names the run; nothing has run through it yet. |
| **session** | `backend == "session-worker"` | Ran in a warm session worker. |
| **compact** | `verdict` present, no `backend` | `execute_code(compact=True)`. |
| **envelope** | `verdict` present, `backend` in `rust`/`python`, no `events` | A fresh sandboxed run. All 21 fields. |
| **execution_trace** | `verdict` present, `backend` in `rust`/`python`, `events` present | `trace_execution`'s result (added `1.14.0`): the same envelope, plus `events`/`branches`/`lines_executed`/`lines_never_executed`/`truncated`. |
| **translation_verification** | `passed` present | `verify_translation`'s result: did a source program and a claimed port agree, with real evidence either way. `ok` is true whenever the tool completed — including a mismatch; it is not the verdict. |
| **optimization_verification** | `accepted` present | `verify_optimization`'s result: is `candidate` a genuine, measurably-faster optimisation of `original`. |
| **edge_case_comparison** | `divergence_count` present | `compare_edge_cases`'s success result: the same logic run in N languages, and where it diverged. |
| **comparison_rows** | `count`/`succeeded`/`fastest` present | `compare_execution`'s result: the same code run in N languages side by side, which was fastest, and any cross-language discrepancies noticed. |
| **session_snapshot_result** | `action` present | `session_snapshot`'s result: archived or restored a session's workspace files. `action` (`save`/`restore`/`list`/`delete`) picks which of the four success shapes applies; see below. |

The first six are **execution** shapes — their discriminators only ever
fire for something that ran code (or explicitly refused to). The next three,
added in `1.5.0`, are **verification** shapes: `verify_translation`,
`verify_optimization` and `compare_edge_cases` were always stamped
`contract_version` by the same `stamp()` at the MCP tool boundary — every
tool gets that — but before `1.5.0` their own shapes matched none of the
execution branches and were explicitly scoped OUT of this document's
"exactly one branch matches" claim. That claim now covers all nine of them:
none of the three verification shapes collides with any execution shape or
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
distinction already in the contract) — so all nine of those shapes require
it.

**`execution_trace`**, added in `1.14.0`, is `trace_execution`'s own shape —
see the version-history entry above for its fields. It is the one shape here
that would otherwise double-match another (`envelope`): both carry
`verdict`/`backend`, and this schema's `additionalProperties` is
intentionally left open (see `build_schema`'s own docstring), so an
`execution_trace` result's extra `events` key alone would not stop it from
ALSO validating against `envelope`. Closed the same way `compact` excludes
`backend` and `rejected` excludes `verdict`: `envelope`'s own definition
gains `not: {required: [events]}`, so a plain `execute_code` result (no
`events` key) matches `envelope` alone, and a `trace_execution` result
(always carries `events`, even as an empty array) matches `execution_trace`
alone.

**`comparison_rows`**, added in `1.10.0`, is `compare_execution`'s own result
— the identical gap `edge_case_comparison` closed for its sibling tool in
`1.5.0`, found the same way: `compare_execution` matched none of the other
eight branches either, and had no branch of its own to fall back on
(`compare_execution` has no refusal shape — an empty `snippets` dict still
returns this shape, with `count: 0`). Unlike the three verification shapes,
it collides with none of them structurally (`count`/`succeeded`/`fastest`
appear on no other shape) and needed no failure-shape reasoning of its own,
because it has no failure shape: `ok` is unconditionally `true` once the
comparison ran, the same as `edge_case_comparison`.

**`session_snapshot_result`**, added in `1.15.0`, is `session_snapshot`'s own
result — the identical "unmodeled success shape" gap `comparison_rows` and
`edge_case_comparison` each closed above, found the same way: every tool's
result is stamped `contract_version` at the same `_coded` wrapper, but
`session_snapshot` matched none of the ten existing branches. It is not an
execution shape at all — no `verdict`, no `backend`, no `run_id`/`state` — and
collides with none of them: `action` (required, one of `save`/`restore`/
`list`/`delete`) appears on no other shape here. A failing call (`ok: false`)
falls through to `rejected` like every other tool, so this shape only ever
describes success. See "Examples" below for one real transcript per action.

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
> only place that actually reaches all 53 tools.

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

### `_meta` is not part of this contract

`contract_version` and this document describe the shape of a tool's
**result** — the dict a tool returns, stamped and validated. Server-side
`_meta` on `tools/list` (the `anthropic/*` policy keys, and MCP Apps'
`ui.resourceUri`) and on a `ui://` resource's own `resources/read` response
lives entirely outside it: it is wire metadata about the *tool or resource
declaration*, not about any result this contract versions, and an MCP client
is already required to ignore an unrecognised `_meta` key regardless of what
it names. Adding, removing, or changing a `_meta` key is therefore never a
`contract_version` bump on its own — see
`docs/design/2026-09-08-mcp-apps-verification-views.md` for where the two
verification tools' `ui` key comes from.

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

`install_package` and `update_runtimes(apply=True)` gate on a caller
confirmation before doing anything (see `codecalc/confirmation.py`); a
declined or cancelled confirmation is `permission_denied` — the same
"elevation gate" this table already describes — and a missing or malformed
confirmation response is `validation`. Neither is a new code: both reuse the
existing eight-entry taxonomy, in the same `{"ok": false, "code", "error",
"remedy"}` shape as every other rejection in this document.

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
- **A process killed abnormally never reports `exit_code: null` on EITHER
  backend, on any OS** — but the VALUE's convention is the platform's own,
  not this contract's invention: negative (the POSIX signal number) on
  Linux/macOS, positive (the raw NTSTATUS) on Windows, which has no
  signals. Both backends agree with EACH OTHER on the same OS; neither
  backend invents a cross-platform encoding the OS itself does not use. See
  "A program killed by a signal" below.

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
request has a `code` and no `verdict`. `errors.ensure_code` enforces this: a
real `RTE`/`OLE`/`MLE` verdict with no `error` of its own gets no `code` at
all, never a message-matched `internal`.

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

A COMPILE failure is the identical shape, `phase: "compile"` instead of
`"run"` — a compile-then-run language (`c`, `rust`, ...) whose source does
not compile. `code` is absent here too, deliberately: retrying the same
request cannot succeed either way, and the fix — a syntax error in this
case — is in the program, not something codecalc got wrong. `stderr` carries
the compiler's own diagnostic in full; nothing summarizes or drops it.
Neither backend emits a verdict distinct from an ordinary run's `RTE` for
this — a caller tells "failed to compile" from "compiled and then failed"
by reading `phase`, never by a `code` or a different `verdict`.

```json
{
  "ok": false,
  "contract_version": "1.10.0",
  "language": "c",
  "phase": "compile",
  "backend": "rust",
  "platform": "linux",
  "stdout": "",
  "stderr": "main.c:1:11: error: expected declaration specifiers or '...' before '{' token\n    1 | int main( { return 0 }\n      |           ^\n",
  "exit_code": 1,
  "timed_out": false,
  "verdict": "RTE",
  "output_truncated": false,
  "output_error": null,
  "stdout_bytes": 0,
  "stderr_bytes": 181,
  "duration_ms": 64,
  "compile_ms": 64,
  "total_ms": 64,
  "cpu_ms": 12,
  "peak_memory_kb": 13156,
  "unenforced": [],
  "workdir": "/tmp/codecalc-2722576-ea0d6fe"
}
```

### A program killed by a signal

`exit_code` is a real, non-`null` value here — never `null` — because the
process DID stop, just not via `exit()`. The SIGN, and which convention
applies, is PER-PLATFORM, not one portable constant:

- **POSIX (Linux, macOS): negative — the signal number that killed it.**
  This is the SAME convention Python's own `subprocess.Popen.returncode`
  uses, and both backends now agree on it: the pure-Python fallback always
  returned `-N` for a signal death (it is `subprocess`'s own `returncode`,
  unmodified); the native backend now matches it (`exit_code_json` in
  `executor/src/main.rs`) rather than reporting `null` — which used to be
  indistinguishable from a runtime that never spawned at all. The exact
  NUMBER is not portable even across two POSIX hosts: the identical
  null-pointer dereference traps as `SIGSEGV` (11, so `exit_code: -11`) on
  Linux glibc, but as `SIGTRAP` (5, so `exit_code: -5`) on macOS/Apple
  silicon — measured, not assumed, after a first version of this contract's
  own test suite hardcoded `-11` and failed identically on both backends on
  macOS. A caller checking for "was this killed by a signal" should test
  `exit_code < 0`, never a specific value.
- **Windows: positive — the NTSTATUS itself.** Windows has no signals at
  all (`platform/mod.rs`'s own doc comment on `StepResult::signal`: always
  `None` there); an abnormal termination like an access violation is just a
  large positive exit code — `0xC0000005` / `3221225477` for
  `STATUS_ACCESS_VIOLATION`. `exit_code_json` already passes this through
  unchanged on Windows (the `-N` branch only fires when `signal` is
  `Some`), so nothing needed to change there.

`null` is reserved for the other case entirely: nothing ran to a stop at
all (a timeout kill, or a runtime that never spawned — see below), on
either OS.

```json
{
  "ok": false,
  "contract_version": "1.10.0",
  "language": "c",
  "phase": "run",
  "backend": "rust",
  "platform": "linux",
  "stdout": "",
  "stderr": "",
  "exit_code": -11,
  "timed_out": false,
  "verdict": "RTE",
  "output_truncated": false,
  "output_error": null,
  "stdout_bytes": 0,
  "stderr_bytes": 0,
  "duration_ms": 204,
  "compile_ms": 237,
  "total_ms": 441,
  "cpu_ms": 0,
  "peak_memory_kb": 752,
  "unenforced": [],
  "workdir": "/tmp/codecalc-2786582-f7d1faa"
}
```

A real transcript from Linux (`SIGSEGV`, hence `-11`); the identical
null-deref on macOS carries `"platform": "darwin"` and `"exit_code": -5`
instead, and on Windows `"platform": "win32"` and a positive
`"exit_code": 3221225477`, with everything else about the shape unchanged.

`stderr` is empty here because a segfault does not itself write anything —
a program that DID print diagnostics before crashing would still have them
in `stdout`/`stderr`, same as any other run. `verdict` is `RTE` — the same
verdict an ordinary nonzero exit gets — because neither backend infers a
verdict distinct from a signal-caused crash except `MLE` (a signal near the
memory ceiling; see "Backends are not identical" above). `code` is absent
for the same reason it is absent from an ordinary RTE above: the process
ran and failed on its own terms, which `errors.ensure_code` does not
classify as a REQUEST-level failure.

### Timeout

`exit_code` is `null` because the process was killed rather than exiting, and
`stderr` says which clock did it. No partial result is returned. Unlike an
ordinary program failure above, a timeout DOES carry `code` — `"timeout"`,
`code_inferred: true` — because it is something the caller can act on (raise
`timeout`), the same actionable shape every other coded failure has;
`executor.execute` never sets `error` for this either, so `errors.
ensure_code` classifies it from `verdict`/`timed_out` directly rather than
falling through to `internal` the way it once did.

```json
{
  "ok": false,
  "contract_version": "1.10.0",
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
  "workdir": "/tmp/codecalc-1788693-320c67bb",
  "code": "timeout",
  "remedy": "raise `timeout`, or reduce the work; a partial result is not returned",
  "code_inferred": true
}
```

### Spawn failure (a missing runtime)

Still an execution envelope — `verdict` is present, because the code ran as
far as it could — but `exit_code` is `null` for the SAME reason as the
timeout above: the process never produced a real exit status. `error` names
the phase and the binary that could not be launched; a caller at the MCP
tool boundary (where `errors.ensure_code` runs) sees this classified `code:
"runtime_unavailable"` from that text.

```json
{
  "ok": false,
  "contract_version": "1.9.0",
  "language": "lua",
  "phase": "run",
  "backend": "rust",
  "platform": "linux",
  "stdout": "",
  "stderr": "runtime unavailable for the run phase: \"lua\" not found (No such file or directory (os error 2))",
  "exit_code": null,
  "timed_out": false,
  "verdict": "RTE",
  "output_truncated": false,
  "output_error": null,
  "stdout_bytes": null,
  "stderr_bytes": null,
  "duration_ms": 19,
  "compile_ms": 0,
  "total_ms": 19,
  "cpu_ms": 0,
  "peak_memory_kb": 0,
  "unenforced": [],
  "workdir": "/tmp/codecalc-1965350-399f7fd9",
  "error": "runtime unavailable for the run phase: \"lua\" not found (No such file or directory (os error 2))"
}
```

`stderr` and `error` carry the identical text here — the Rust binary's own
spawn-failure message already says "runtime unavailable"; `error` exists
so a caller can read it as a stable machine-facing field rather than
parsing `stderr` prose, not because the two texts need to differ.

A compile-then-run language missing its COMPILER reports the identical
shape with `"phase": "compile"` instead. `stdout_bytes`/`stderr_bytes` are
`null` rather than `0` on EITHER phase — nothing spawned, so there is no
program output to have counted (see "Truncation, and how much was cut"
above).

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

### A `compare_execution` row that needed a runtime this host does not have

The tool's OUTER `ok` is `true` — the comparison itself ran — but the `lua`
row failed for a REQUEST-level reason, so it carries `code`/`error`/`remedy`
the same as a top-level coded failure. The `python3` row ran and succeeded
and carries none of the three; a row that ran and failed on its OWN terms
(a real `RTE`/`OLE` `exit_code`) would carry none of them either, only
`exit_code` and `timed_out` saying so — see `errors.stamp_row` in
`codecalc/errors.py` for the full rule. `compare_edge_cases`'s per-language
`runs[lang]` entries follow the identical rule.

```json
{
  "ok": true,
  "contract_version": "1.10.0",
  "count": 2,
  "succeeded": 1,
  "results": [
    {
      "language": "python3", "ok": true, "stdout": "42\n", "stderr": "",
      "exit_code": 0, "duration_ms": 33, "timed_out": false, "cold_retry": false
    },
    {
      "language": "lua", "ok": false, "stdout": "", "exit_code": null,
      "duration_ms": 4, "timed_out": false, "cold_retry": false,
      "stderr": "runtime unavailable for the run phase: \"lua\" not found (No such file or directory (os error 2))",
      "error": "runtime unavailable for the run phase: \"lua\" not found (No such file or directory (os error 2))",
      "code": "runtime_unavailable",
      "code_inferred": true,
      "remedy": "install the runtime, or call list_languages to see what this host has"
    }
  ],
  "fastest": "python3",
  "fastest_note": null,
  "discrepancies": []
}
```

### `session_snapshot`, one transcript per action

Captured from a real `python3` session that wrote one file (`model.py`),
saved it, restored it into a new session, then deleted the snapshot.

```json
{
  "ok": true,
  "action": "save",
  "session_id": "python3-812e07bf",
  "snapshot_id": "d452eb8b6cd649aa8beaf7a2b7de0d52",
  "bytes": 243,
  "files": 1,
  "created_at": "2026-09-08T22:04:56Z",
  "label": "before-refactor",
  "contract_version": "1.14.0"
}
```

```json
{
  "ok": true,
  "action": "list",
  "session_id": "python3-812e07bf",
  "snapshots": [
    {
      "snapshot_id": "d452eb8b6cd649aa8beaf7a2b7de0d52",
      "bytes": 243,
      "files": 1,
      "created_at": "2026-09-08T22:04:56Z",
      "label": "before-refactor"
    }
  ],
  "contract_version": "1.14.0"
}
```

`restore` without `replace=True` returns the NEW session's own `session_start`
shape, plus what this call itself restored (`restored_files`, `bytes` —
uncompressed bytes actually written, not the archive's own on-disk size):

```json
{
  "ok": true,
  "session_id": "python3-6ef239e3",
  "language": "python3",
  "stateful": true,
  "workdir": "/path/to/codecalc/sessions/python3-6ef239e3",
  "files": [
    {"path": ".codecalc-session-lock", "type": "file", "size": 7},
    {"path": "model.py", "type": "file", "size": 14}
  ],
  "confined": true,
  "unenforced": ["sandbox_backend: ...", "no_net: ...", "process_group_kill: ...", "peak_memory_kb: ..."],
  "action": "restore",
  "restored_files": 1,
  "bytes": 14,
  "contract_version": "1.14.0"
}
```

```json
{
  "ok": true,
  "action": "delete",
  "session_id": "python3-812e07bf",
  "snapshot_id": "d452eb8b6cd649aa8beaf7a2b7de0d52",
  "deleted": true,
  "contract_version": "1.14.0"
}
```

A failing call — an unknown `snapshot_id`, a workspace whose identity could
not be verified, a hostile archive on restore — matches `rejected` instead:

```json
{
  "ok": false,
  "contract_version": "1.14.0",
  "code": "permission_denied",
  "error": "snapshot member 'link' is a symlink/hardlink — refused",
  "remedy": "the path or operation is outside what this server permits; it will not succeed on retry",
  "member": "link"
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
