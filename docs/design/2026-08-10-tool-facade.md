# Design: put the 47 tools behind a discovery facade

Tracks GitHub [#118](https://github.com/The-40-Thieves/codecalc/issues/118) and THE-784.
Status: designed, not implemented. Written 2026-08-10 against `main` at `fcd236f`.

## Why now

The facade was deliberately held behind the versioned result contract (THE-781). The recorded
reason was specific: a facade must wrap every result in an envelope, and choosing that envelope's
shape before THE-781 decided it would mean migrating 47 tools onto a shape THE-781 would then
revise.

THE-781 is only partly landed. Two slices merged (`output_truncated` backend divergence, and an
eight-code error taxonomy in `fcd236f`); `contract_version`, the published schema and the migration
path are still open. So the hold's rationale would still bite a facade that introduced an envelope.

This design discharges the hold a different way: **`call_capability` relays the underlying tool's
result verbatim.** The facade owns discovery and dispatch, never result shape. No envelope is
chosen, so there is nothing for THE-781 to revise, and the rest of THE-781 lands on the tools
themselves and reaches facade callers unchanged.

## A premise correction

Both #118 and the decision note state that "the SDK has no `tools/list` filter, so the facade needs
conditional registration across 47 decorator sites". That is not true of MCP SDK 2.0.
`MCPServer.list_tools()` and `MCPServer.call_tool()` are public and documented as overridable.

**Zero decorator sites change.** Every `@mcp.tool()` stays exactly as it is, which matters for more
than diff size: the SDK keeps generating each tool's schema from its type hints. That generation is
what `ci-python.yml`'s `declared == served` gate exists to protect, because a tool whose type hints
the SDK cannot schematise is dropped from `list_tools` at registration with no error anywhere. An
approach that stopped registering the 47 would delete the schema generation and the gate's subject
matter together, and would force hand-rolled schemas for `describe_capability` whose drift from the
real signatures nothing would catch.

The facade therefore filters the **view**, not the **registration**.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Result shape | Verbatim relay | Discharges the THE-781 hold; `unenforced` / `output_error` survive by construction |
| Default surface | Facade default, `flat` opt-in | Nothing is published yet, so the flip is free now and a breaking change after the first release |
| Seam | Subclass `MCPServer`, override `list_tools` | Public API; middleware is marked provisional in the SDK docs |
| CI gate | Round-trip both modes, floor the catalogue | Keeps today's invariant intact and gates the surface users actually get |

Rejected: intercepting `tools/list` via `ServerMiddleware` (works, and `mcp_middleware.py` already
uses that hook, but the SDK marks middleware provisional and it buys nothing over the subclass); and
porting obsidian-tc's registry shape literally (the large refactor this design avoids, and it drops
SDK schema generation).

## Architecture

One new module, `codecalc/facade.py`, plus an `MCPServer` subclass in `codecalc/server.py`.

```
tools/list  ->  CodecalcServer.list_tools()
                  mode == facade  ->  the three facade tools
                  mode == flat    ->  super().list_tools(), all 47

tools/call  ->  find_capability      search over super().list_tools()
                describe_capability  the SDK's generated input_schema
                call_capability      self.call_tool(name, args), result relayed unchanged
```

### Mode selection

`CODECALC_TOOL_MODE`: unset or `facade` selects the triad; `flat` selects the 47. **Any other value
raises at startup.** It does not fall back to a default. A typo'd mode that silently serves the
wrong surface is the exact defect shape this repo keeps finding, and a fallback would encode it.

In flat mode the three facade tools are **not** listed. Flat is the 47 and only the 47, so
`served == declared` stays exact arithmetic rather than 47 plus an adjustment.

This forces one structural constraint: **the triad is not declared with `@mcp.tool()`.** If it were,
`grep -c '^@mcp\.tool'` would return 50, `super().list_tools()` would return 50, and the flat-mode
identity this design relies on would be broken by the facade's own tools. Instead the triad is
defined in `codecalc/facade.py` and injected by the subclass:

- `list_tools()` returns the injected triad in facade mode, or `super().list_tools()` in flat mode.
- `call_tool()` routes the three facade names to their handlers and delegates everything else to
  `super().call_tool()`.

So `declared` keeps counting exactly the 47 real tools, and the facade surface is synthesised rather
than registered.

### The three capabilities

**`find_capability(query: str, limit: int = 10)`** scores `query` against each tool's name and
description from `super().list_tools()` and returns only matches, as `{name, summary, score}`.

The catalogue is searched, never enumerated into a tool description. This is structural, not a
matter of discipline: a facade whose description lists all 47 operations has relocated the 7,625
tokens rather than removed them, and would be worse than no facade because it adds a dispatch hop
for nothing.

Scoring is deterministic token overlap with a name-prefix boost. No new dependency, no index to
build, nothing that can go stale relative to the registered tools, because the corpus is generated
per call from the live tool list.

**`describe_capability(name: str)`** returns the SDK's generated `input_schema`, the `output_schema`
where one exists, and the description.

This is the mitigation for the argument-correctness cost the facade otherwise imposes. #118 records
hitting it concretely while measuring: calling `execute_code(code, language)` when the signature is
`(language, code)`. The schema cannot drift from the real signature because the SDK derives both
from the same type hints.

**`call_capability(name: str, args: dict)`** delegates to `super().call_tool(name, args)` and
returns the result unchanged.

It delegates to `super()`, not `self`, and refuses any of the three facade names outright. Routing
through `self` would let `call_capability("call_capability", ...)` re-enter the override and
recurse; the refusal is asserted in the tests rather than left to the delegation detail.

No envelope. No added keys. Not even a `capability` echo: a header is an envelope with fewer keys
and tends to grow into one, and the caller already knows which capability it named.

`server.compact_result()` is deliberately **not** called on this path. Its docstring says anything
that re-envelopes a result must reuse it rather than re-derive which fields are droppable, because
re-deriving that is how `unenforced` was lost the first time. Verbatim relay does not re-envelope,
so there is no droppable-field decision to get wrong.

## Error handling

| Case | Response |
|---|---|
| `name` not a registered capability | Error naming the closest `find_capability` matches, so a wrong guess self-corrects |
| `args` fails schema validation | The capability's schema, not a generic validation error, so a bad call costs one hop instead of a dead end |
| Underlying tool raises | Relayed unchanged, including its THE-781 error code |
| `name` is one of the three facade names | Refused, so the dispatcher cannot re-enter itself |
| `CODECALC_TOOL_MODE` unrecognised | Raise at startup, naming the value and the two valid modes |

## Gates and tests

The `ci-python.yml` stdio round-trip runs **twice**:

| Mode | Assertions |
|---|---|
| `flat` | `served == declared` (47 == 47), unchanged from today |
| `facade` | `served == 3`, and `catalogue == declared` (47 == 47) |

The catalogue assertion is the floor. Without it, a tool dropped at registration is invisible on a
surface that only ever shows three names, and the facade leg would stay green while the product
silently lost a tool. With it, the drop reddens both legs.

`declared` keeps being derived from `grep -c '^@mcp\.tool' codecalc/server.py` rather than
hardcoded, so it continues to track `scripts/check_claims.py` instead of becoming a second number to
keep in sync.

New `tests/test_facade.py` covers: mode selection including the raise on an unrecognised value; that
facade mode lists exactly three tools and flat lists exactly 47 with no facade tools among them;
that `find_capability` returns matches rather than the whole catalogue, and that no tool description
enumerates the catalogue; that `describe_capability` returns a schema whose required argument names
match the real function signature; that `call_capability` returns a result **byte-identical** to
calling the tool directly, asserted on a tool that emits `unenforced`; that `call_capability`
refuses its own three names; and every error path in the table above.

One assertion earns its place specifically against this design's structural constraint: that
`grep -c '^@mcp\.tool' codecalc/server.py` still returns 47 after the facade lands. If a later
change declares a facade tool with the decorator, the flat-mode identity breaks silently, and this
is the cheapest place to catch it.

Per this repo's standing practice, every new assertion is watched failing against a seeded defect
before it is trusted, and a floor asserts the round-trip gate itself exists so its breadth cannot
silently shrink.

The token figures are **re-measured** after the change. #118's 7,625 and 257 are the pre-change
baseline and are not to be restated as results.

## Out of scope

- The middle "domain" mode (obsidian-tc ships ~13 in that mode). Two surfaces to gate, not three.
- The larger endpoint #118 records but does not propose: a single `execute_code` plus a `codecalc`
  API available inside the sandbox, so a caller expresses six operations as one script. Different
  and much larger change, with its own discoverability and error-reporting trade-offs.
- The remaining THE-781 items. Under verbatim relay they land independently and reach facade
  callers for free.

## THE-784's "report the selected underlying operation"

THE-784's acceptance says high-level tools must report the selected underlying operation, which is a
response-shape requirement. Verbatim relay adds no such key.

Read as targeting **routing** tools that pick among several implementations, this does not apply to
a dispatcher that runs exactly the capability the caller named. Confirmed with the owner 2026-08-10.
Recorded here because it is an acceptance criterion this design knowingly does not satisfy under a
different reading.

## Acceptance, mapped to #118

- [ ] `tools/list` in the default mode returns the triad; the flat surface stays reachable behind an explicit mode
- [ ] The catalogue is searched, never enumerated into a description
- [ ] A malformed `call_capability` returns the schema for the named capability
- [ ] `unenforced` / `output_error` survive the facade path, gated
- [ ] The token figures are re-measured after the change rather than assumed

## Risks

**The default flip is a breaking change after release.** It is free today only because nothing is
published: no tags, no releases, PyPI `codecalc` unclaimed. If a release is cut before this lands,
this decision has to be re-taken as a compatibility question.

**Verbatim relay ties facade callers to per-tool result shapes.** That is the point, and it is what
discharges the hold, but it does mean the facade offers no insulation if THE-781 later changes a
tool's keys. The insulation would be an envelope, and an envelope is what was rejected.

**A provisional-API dependency is avoided but not eliminated.** `list_tools` and `call_tool` are
public, though `MCPServer` internals such as `_tool_manager` are not, and this design touches none
of them.
