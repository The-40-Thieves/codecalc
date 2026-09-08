# Distribution and listings

Where codecalc is published, and — for the two places it is not yet listed —
the exact steps to submit it. README.md's "Where to find codecalc" table is
the live status; this file is the how-to for the entries that need a human to
act, so it does not need to change every time a release ships.

## Already listed

Checked live on 2026-09-08:

- **PyPI** — [pypi.org/project/codecalc](https://pypi.org/project/codecalc/)
- **crates.io** — [crates.io/crates/codecalc-exec](https://crates.io/crates/codecalc-exec)
- **GitHub Releases** — [github.com/The-40-Thieves/codecalc/releases](https://github.com/The-40-Thieves/codecalc/releases)
  (wheels, executor binaries, the `.mcpb` bundle, SBOM, `SHA256SUMS`)
- **MCP registry (official)** — `io.github.The-40-Thieves/codecalc`, published
  from `server.json` by `.github/workflows/release.yml`; queryable at
  `https://registry.modelcontextprotocol.io/v0/servers?search=codecalc`
- **Smithery** — [smithery.ai/servers/@The-40-Thieves/codecalc](https://smithery.ai/servers/@The-40-Thieves/codecalc)
- **Glama** — [glama.ai/mcp/servers/The-40-Thieves/codecalc](https://glama.ai/mcp/servers/The-40-Thieves/codecalc)
  (`glama.json` at the repo root names the maintainer; badge is in README.md)

## Not yet listed

### PulseMCP

Checked 2026-09-08: [pulsemcp.com/submit](https://www.pulsemcp.com/submit)
states submissions are paused ("We are not accepting new MCP server or
client submissions right now", paused since 2026-09-03, no reopening date
given) and that publishing to the official MCP registry is "the best first
step even when we are not paused" — PulseMCP indexes from that registry once
its own submission system reopens. codecalc is already published there (see
above), so **there is nothing to submit today**. Recheck
[pulsemcp.com/submit](https://www.pulsemcp.com/submit) periodically for the
pause to lift; no action is needed before then.

### mcp.so

mcp.so has no public search API and its site is a client-rendered SPA — a
plain HTTP fetch of `https://mcp.so/?q=codecalc` and
`https://mcp.so/submit?type=server` returns an empty shell with the data
loaded by client-side JavaScript, so an automated check cannot confirm from
here whether codecalc is already indexed. `https://mcp.so/submit?type=server`
does resolve (HTTP 200, confirmed 2026-09-08), which is as far as a
non-browser check can go.

**Submission steps** (perform in a browser — the form needs JavaScript):

1. Open [mcp.so/submit?type=server](https://mcp.so/submit?type=server).
2. First search the listing for "codecalc" — the query-string search
   (`mcp.so/?q=codecalc`) does not render without JavaScript, so search from
   the site's own search box instead of trusting a fetched URL.
3. If not listed, submit with:
   - **Repository URL:** `https://github.com/The-40-Thieves/codecalc`
   - **Name:** `codecalc`
   - **Description:** the one-line description from `server.json` —
     "Code & logic calculator for AI agents: 31 languages, symbolic math,
     SMT/logic solving, Big-O."
4. Record the resulting listing URL here and in README.md's table once it is
   live.

## Why this file exists rather than a bot doing the submitting

Both remaining sites require either waiting on an external pause (PulseMCP)
or a JavaScript-rendered form with no public API (mcp.so) — neither is a
repo change, so it does not belong in a gated CI check. `scripts/check_llms_txt.py`
and `scripts/check_claims.py` gate everything in this repo that a script can
verify offline; a third party's submission queue is deliberately not one of
those things.
