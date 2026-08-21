# Design: put the 47 tools behind a discovery facade

Tracks GitHub [#118](https://github.com/The-40-Thieves/codecalc/issues/118).
Written 2026-08-10 against `main` at `fcd236f`.

> [!important] Status: designed, reviewed, and deliberately NOT built yet
> The first draft of this document was approved and then failed review on three independent
> grounds. It has been rewritten. The facade is not the next piece of work; the prerequisites in
> [Before this is built](#before-this-is-built) come first. What follows is the corrected design,
> kept so the work does not have to be re-derived.

## Why the first draft was wrong

Two reviews ran against the approved draft: an external-evidence pass (Anthropic platform
documentation, MCP specification, SDK sources) and an adversarial cross-vendor design review. They
never saw each other's findings and reached the same verdict on the central decision.

### 1. A facade competes with the client's own tool search, and loses

Anthropic's tool search tool is generally available. A client sends every tool definition and marks
them `defer_loading: true`; the API keeps them out of the system-prompt prefix and, on search,
returns `tool_reference` blocks that it **expands into full tool definitions**. For MCP servers
reached through the MCP connector, the client sets this once on the toolset's `default_config` —
the whole of codecalc defers with one line of client configuration and no server change.

That mechanism searches tool names, descriptions, argument names and argument descriptions, and the
definitions it expands are real, schema-enforced, strict-mode-compatible. A facade replaces all of
that with `call_capability(name, args)`, whose `args` is an untyped object at the model boundary,
and whose catalogue the client's search cannot see into.

The two do not compose. Whichever layer hides the catalogue first wins, and the server winning is
the worse outcome.

### 2. A facade erases per-operation policy metadata

Forty-seven tools carry forty-seven sets of annotations, approval prompts and audit names. One
`call_capability` makes package installation, runtime mutation, session file writes and ordinary
arithmetic indistinguishable to any client that approves or denies by tool name.

Relatedly: hiding a tool from `list_tools` does not hide it from `call_tool`. A client that guesses
`install_package` can still invoke it in facade mode. The facade filters discovery, never
invocation, and must not be described as a boundary.

### 3. The accuracy argument #118 rejected is real

#118 says the case is token cost "and nothing else", and that any argument from tool-selection
accuracy at 47 tools "should be rejected". Anthropic's documentation states that selection accuracy
"degrades once you exceed 30–50 available tools" and recommends tool search from 10 tools upward.
47 is inside that band, not safely below it. The accuracy argument is sound; it simply argues for
the client-side mechanism rather than for this facade.

## What is still true

The measured prize is real: 47 definitions cost 7,625 tokens, a triad costs 257. codecalc also sits
in a gap — Claude Code enables tool search automatically only above ~10K tokens of MCP tool
descriptions, so at 7,625 codecalc is under the trigger and clients do pay the full cost today.

**The cheapest fix is documentation, not code.** A client can set `ENABLE_TOOL_SEARCH=true`, or
`defer_loading` on the toolset, and collect most of the prize immediately. That belongs in the
README regardless of whether a facade is ever built.

And for non-Claude clients there is no standard mechanism at all: the proposal to add
`defer_loading` to the MCP Go SDK was closed as **not planned**, and what the 2026-07-28
specification added instead — `ttlMs`, `cacheScope`, cursor pagination — saves round-trips, not
context-window tokens. A server-side facade remains the only portable answer, which is why this
design is kept rather than discarded.

## Corrected design

### Default

**Flat by default. Facade behind `CODECALC_TOOL_MODE=facade`, documented as experimental.** The
first draft had this inverted, on the argument that a pre-release project can flip a default for
free. Free to change is not the same as right to change: facade-by-default degrades the
best-supported client class, and the release default should be decided by measuring total task
tokens, success rate, round-trips and approval behaviour, not `tools/list` size.

Any unrecognised value of `CODECALC_TOOL_MODE` raises at startup rather than falling back.

### The triad is registered like any other tool

The first draft synthesised the three facade tools outside the registry, specifically so
`grep -c '^@mcp\.tool'` would keep returning 47. That is designing production structure around a
grep, and it was justified by a premise that is false for MCP SDK 2.0.0.

The CI gate's comment claims a tool whose type hints cannot be schematised is "dropped from
list_tools at registration, with no error anywhere". `Tool.from_function` raises — `ValueError`,
`InvalidSignature` — it does not drop. The real silent-shrink path is a **duplicate name**:
`ToolManager.add_tool` logs a warning, returns the existing tool, and the count quietly falls by
one. The gate is worth keeping; its stated cause is wrong and should be corrected.

So: register the triad normally, tag it `meta={"surface": "facade"}`, and derive every assertion
from the registry or an explicit manifest rather than from a decorator count.

### Dispatch

`call_capability` must:

- **Pass the original request context** through to the underlying call. `call_tool(name, args)` with
  no context makes the SDK build a reduced `Context` that has lost `request_context`, breaking
  progress reporting, logging and elicitation.
- **Resolve the timeout by the underlying capability name, not the outer one.** This is the defect
  that makes the first draft unshippable: `timeout_middleware` reads the tool name from
  `ctx.params["name"]`, which under a facade is always `call_capability`. Not being in
  `TOOL_TIMEOUTS`, it inherits `DEFAULT_TIMEOUT_SECONDS = 900.0`, and all twelve per-tool deadlines
  stop firing — the AUDIT.md HIGH-05 mitigation, silently disabled by a change that never touches a
  decorator, which is precisely what that table's comment says it exists to prevent.
- **Refuse all three facade names** before dispatch, so the dispatcher cannot re-enter itself.
- Reimplement, or explicitly delegate, the argument validation, result conversion, context injection
  and output validation that the tool manager performs for registered tools.

### Result shape

Content is still relayed verbatim: the facade owns discovery and dispatch, never result keys, and
`server.compact_result()` is not called on this path.

Correlation data — which capability ran, the facade version — goes in MCP's `CallToolResult._meta`,
which carries metadata without touching content. This replaces the first draft's "not even an echo"
position: `_meta` gives correlation without inventing a competing envelope.

What `_meta` does **not** give is insulation. Verbatim relay does not remove the eventual breaking
change, it relocates it from 47 tools to `call_capability`, whose result is a polymorphic
capability-dependent object. If a uniform envelope is wanted, that is a separate piece of work to
define and the facade adopts it; the facade must not grow a competing one.

Error paths are not verbatim either, and the first draft claimed otherwise: `MCPError` propagates as
a protocol error while other exceptions become `CallToolResult(is_error=True)` with text content.

### Discovery

`find_capability` scores a query against names and descriptions from the live tool list. It needs an
escape hatch, because pure positive lexical matching can make a capability permanently
undiscoverable to a caller whose vocabulary differs — synonyms, morphology, acronyms, misspellings:

- An empty query returns a **paginated catalogue**.
- An exact capability name always matches.
- A zero-match query returns deterministic nearest suggestions.
- Query length and `limit` are bounded.

Paginating a catalogue in response to an explicit discovery call does not relocate the 7,625 tokens
into `tools/list`; it charges only the callers who ask. The first draft's blanket "never enumerate"
rule confused the description with the response, and would have made facade mode strictly less
capable than the flat surface.

### Gates

The stdio round-trip runs in both modes: flat asserts today's invariant unchanged, facade asserts
the triad is what is served.

The catalogue assertion must be **set equality, not cardinality**. `catalogue == declared` compares
counts, so it passes if one tool disappears while another appears, or if the catalogue holds the
wrong 47 names, and the flat leg already counts the same thing. Assert instead that

```
registered-name set == expected manifest == describable-name set == exact-name-searchable set
```

plus name uniqueness, schema equality between `describe_capability` and the registered tool, and
that every capability is invocable through the dispatcher with validation running before execution.

Because `mcp>=2.0,<3` admits any 2.x, contract tests pinning the `list_tools` / `call_tool` seam are
required even though both are public today.

## Before this is built

1. freezes the v1 result contract, so the facade adopts an envelope rather than inventing or
   competing with one.
2. An MCP-independent `CapabilityRegistry` with `list` / `describe` / `invoke`. It serves the facade
   and the larger endpoint below equally, so it is not throwaway work under either outcome.
3. A benchmarked vertical slice of the endpoint design — one `execute_code` plus a `codecalc` API
   available inside the execution environment — measured on realistic tasks for total tokens,
   latency, correctness and failure attribution.

Only then is the surface decision made on evidence. The facade cuts startup tokens but can replace
one operation with three round-trips; for a six-operation task that can cost more in total than it
saves, which is the measurement none of the 7,625-versus-257 arithmetic captures.

If the endpoint wins, the likely surface is `execute_code` plus `find_capability` /
`describe_capability`, with no generic `call_capability`, and with privileged operations — package
installation, runtime mutation, persistent session and file operations — kept as separately visible
tools carrying their own approval metadata. An in-environment library must not become an unguarded
bridge from executed code into privileged server operations: it needs the same validation, timeouts,
quotas and policy checks as an MCP call.

## Independently real defects, extracted

These were surfaced by the review and are worth fixing whether or not a facade is ever built:

- `TOOL_TIMEOUTS` is keyed on the inbound tool name, so any future indirection silently drops twelve
  deadlines to 900s.
- The `declared == served` gate's comment names a failure mechanism that does not exist in SDK 2.0.0
  and misses the one that does (duplicate names).
- `catalogue == declared`-style assertions compare counts where they should compare identity.

## Acceptance, mapped to #118

- [ ] The flat surface stays the default; the triad is reachable behind an explicit experimental mode
- [ ] The catalogue is searched rather than enumerated into a description, with a paginated browse escape hatch
- [ ] A malformed `call_capability` returns the schema for the named capability
- [ ] `unenforced` / `output_error` survive the facade path, gated
- [ ] Per-tool timeouts resolve by the underlying capability name, gated
- [ ] Catalogue assertions compare name sets, not counts
- [ ] The token figures are re-measured after any change rather than assumed
