# MCP Apps: graphical views for `verify_translation` / `verify_optimization`

**Status:** design + implementation, 2026-09-08.

## What was researched, and where it came from

**Spec.** MCP Apps (extension id `io.modelcontextprotocol/ui`, wire methods
under `ui/*`) is specified at `specification/2026-01-26/apps.mdx` in the
`modelcontextprotocol/ext-apps` repository, framed as an additive extension
under SEP-2133. Current stable spec version: **2026-01-26**, with a draft in
progress. Fetched directly (ctx7 + a repo fetch), not assumed from training
data.

**Host support** (from the ext-apps repo's own docs, as of 2026-09-08): Claude
(desktop/claude.ai), ChatGPT (OpenAI's Apps SDK), VS Code, Goose, Postman,
MCPJam, the mcp-use inspector, and the Alpic Playground. Support varies by
host and version; a host that has not implemented the extension simply never
sends the `io.modelcontextprotocol/ui` entry in its `initialize` capabilities,
which is the fallback path this change relies on (see below).

**Installed Python SDK (`mcp` 2.0.0) already ships a first-class `Apps`
extension** at `mcp.server.apps` — the ticket's fallback instruction ("if the
SDK has no helper, hand-roll the raw shape") did not apply. That module was
read in full before writing any code. It exports the exact constants used
here (`EXTENSION_ID = "io.modelcontextprotocol/ui"`,
`APP_MIME_TYPE = "text/html;profile=mcp-app"`) and a `client_supports_apps()`
capability check. **This change does not use the `Apps` extension class
itself** — that class expects tools/resources to be registered at
`MCPServer(extensions=[...])` construction time, which would mean
restructuring how every one of this server's 52 tools is declared. Instead it
uses the plain `@mcp.tool(meta=...)` / `@mcp.resource(mime_type=..., meta=...)`
kwargs the SDK already exposes as first-class decorator arguments (the same
mechanism `codecalc/server.py`'s existing `anthropic/*` `_meta` keys and
`session_file_resource` already use) — importing only the two string
constants from `mcp.server.apps` for correctness (so the MIME type and
extension id can never drift from the SDK's own values).

## Exact `_meta` shape (from the spec + SDK, cross-checked)

Tool `_meta`:

```json
{"ui": {"resourceUri": "ui://verify-translation/view.html"}}
```

`visibility` (`["model", "app"]`) is omittable — omitted here, so the default
(both) applies: the tool's text/structured result is unchanged for the model,
and a supporting host additionally renders the view.

Resource `_meta` (not used here — no CSP/permissions/domain overrides needed,
since the view makes no outbound requests and needs no browser permission):

```json
{"ui": {"csp": {...}, "permissions": {...}, "domain": "...", "prefersBorder": true|false}}
```

Left empty; the host's **default** CSP already fits a fully self-contained,
network-free document:

```
default-src 'none'; script-src 'self' 'unsafe-inline';
style-src 'self' 'unsafe-inline'; img-src 'self' data:;
media-src 'self' data:; connect-src 'none'
```

No `connectDomains`/`resourceDomains`/`frameDomains`/`baseUriDomains` are
declared because the view fetches nothing and embeds nothing external — the
default (`'none'` for connect/frame, `'self'` for base-uri) is exactly the
posture wanted, not a gap.

## Resource shape

- URI scheme: `ui://` (spec-mandated; enforced by the SDK at the `Apps`
  layer, and by this module's own resource declarations, which use the same
  scheme by construction).
- MIME type: `text/html;profile=mcp-app` — the ONLY type the 2026-01-26 spec
  admits for the MVP phase. Imported as `mcp.server.apps.APP_MIME_TYPE`
  rather than typed as a literal, so it cannot drift from the SDK's own
  constant.
- No size limit is stated in the spec. This implementation holds both
  documents to **under 60 KB** as an explicit, self-imposed ceiling (tested),
  since the whole point of an inline, dependency-free document is that it
  stays small enough to serve as a single `resources/read` response with no
  paging.

## How the tool result reaches the view

Per the ext-apps quickstart and migration docs: the app-sdk's `App` client
exposes `app.ontoolresult = (params) => { params.structuredContent }` — the
host's outer bridge calls the tool, gets its MCP result, and forwards
`structuredContent` (already present here: both tools return
`dict[str, Any]`, which the pinned SDK schemas into `outputSchema`/
`structuredContent` for every typed tool — see `server.py`'s own comment
next to `_MAX_RESULT_SIZE_CHARS` for the evidence that this already holds).

**No external `app-sdk` script is loaded** — this document cannot fetch
anything (self-contained, no network, no external `src`/`href`, per the
ticket's own constraint) — so each view hand-rolls the small slice of the
wire protocol it actually needs: a JSON-RPC 2.0 message framed over
`window.postMessage`, sending a `ui/initialize` request and listening for a
`tool-result` notification carrying `structuredContent`. This is a strict
subset of the full protocol (which also carries `ui/open-link`,
`ui/request-display-mode`, host-initiated `tool-input`/`size-changed`/
`host-context-changed`, etc.) — this is a **read-only, output-only** view; it
never calls a tool itself, never asks for a display-mode change, and never
needs those messages. A host that speaks the full protocol still
interoperates with this subset because every one of those additional
messages is one this view simply never sends or expects.

For **offline verification** (no live host available from this environment —
see "Manual verification" below), each document also checks
`window.__CODECALC_SAMPLE__` before attempting the handshake and renders
that directly if present. This is a test/screenshot hook, not a spec
mechanism.

## Deliberate deviation from the ticket's chart description

The ticket asks for "the raw runs as dots" alongside the before/after
medians on the optimization timing chart. **`verify_optimization`'s
structured result does not carry raw per-run timings** — `optimization.py`'s
internal `_timed()` keeps `all_runs_ms` per size, but `verify_optimization`'s
returned dict only ever surfaces `speedup.per_size` (medians: `before_ms`/
`after_ms`/`ratio`) and `inference.per_size` (the Mann-Whitney statistics per
size). Adding `all_runs_ms` to the public result would be a **schema change**
to a tool this same ticket requires stay **byte-identical** to main's
`tools/call` output. Byte-identity is the harder, explicit, testable
requirement; the raw-dots chart is a visual embellishment described in prose.
The view renders exactly what the tool already returns — before/after medians
per size, as a bar chart, log-scaled when `max(before_ms, after_ms) /
min(...)` across sizes is at least 20x — and this deviation is called out
here rather than silently doing less than the brief asked for.

## Fallback for hosts without Apps support

Unconditional: `_meta` is added to the tool declaration only; nothing about
`tools/call`'s return value changes, and nothing about `tools/list`'s other
fields changes. A host that never negotiated
`capabilities.extensions["io.modelcontextprotocol/ui"]` has no reason to ever
call `resources/read` on a `ui://` URI, and per the two client rules already
documented in `docs/contract/README.md`'s spirit (ignore fields you don't
recognise), an MCP-conformant client ignores an unrecognised key under
`_meta` — there is nothing to opt out of. `tests/test_mcp_apps_views.py`
proves this directly: `tools/call` on both tools is byte-identical to a
`main`-branch checkout's server, run over the same stdio transport.

## Files

- `codecalc/apps_views.py` — the two static HTML documents, as module-level
  string constants, plus size/URI constants.
- `codecalc/server.py` — `_UI_RESOURCES` (tool name -> `ui://` URI), folded
  into the existing `_tool_meta()` merge point; two `@mcp.resource(...)`
  declarations next to `session_file_resource`.
- `tests/test_mcp_apps_views.py` — resource listing/reading, MIME type, HTML
  structural checks (no external `src`/`href`, parses under `html.parser`),
  `_meta` shape, size caps, and the `tools/call` byte-identity check against
  a scratch `main` checkout.
- `docs/contract/README.md` — a note that `_meta` (tool or resource) is not
  part of the versioned JSON result contract.

## Manual verification

No live Claude Desktop/Claude.ai/ChatGPT session is drivable from this
environment. Verification instead renders each HTML document, with a bundled
sample result assigned to `window.__CODECALC_SAMPLE__`, in a headless browser
via Playwright, and saves a screenshot of the resulting DOM. See
`docs/design/verify_translation-sample.png` and
`docs/design/verify_optimization-sample.png`.
