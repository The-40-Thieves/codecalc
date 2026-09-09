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

Where codecalc is published, and — for a listing that needs a human to act —
the exact steps to submit it.

## Docker MCP Catalog

[docker/mcp-registry](https://github.com/docker/mcp-registry) is the source
repository behind the [Docker MCP Catalog](https://hub.docker.com/mcp) and
Docker Desktop's MCP Toolkit. Submission is a pull request against that repo
adding `servers/codecalc/` — not yet opened as of this writing; the files
below are prepared and validated, and this section is the how-to for opening
it.

### Licence check, done first

The registry's contribution guide states the policy in prose ("Make sure
that the license of your MCP Server allows people to consume it. MIT or
Apache 2 are great, GPL is not.") and its own `cmd/validate` tool enforces it
in code — `internal/licenses.IsValid` rejects any GitHub-reported license key
with the prefix `gpl`, `agpl`, or `npl`, checked live against the GitHub
API's own read of the repository's license. codecalc was AGPL-3.0-only
before its first outside contributor and was relicensed to **Apache-2.0**
(`LICENSE`, `pyproject.toml`, `executor/Cargo.toml`) — see the relicensing
commit's own message for the reasoning. Apache-2.0 carries no rejected
prefix, and running the registry's real `task validate` against this
codebase's HEAD confirms it live: `✅ License is valid`. There is no AGPL
blocker for this submission.

### Which submission path

Two paths exist:

- **Docker-built image** — give the registry a `source.project` +
  pinned `source.commit` + `source.dockerfile`, and Docker builds the image
  itself into `mcp/<name>` on Docker Hub. This is the path taken here: it
  gets cryptographic signatures, provenance tracking, an SBOM, automatic
  security updates, and the Docker Desktop MCP Toolkit listing that a
  self-supplied image does not.
- **Bring-your-own image** — pass `--image <yourorg>/<name>` to `task
  create` and the registry references an image you build and host yourself.
  Still gets container isolation, none of the Docker-built extras above.

codecalc already has `docker/mcp-server.Dockerfile` (the MCP server image,
distinct from `docker/executor.Dockerfile`'s strict-runtime executor — see
`docker/README.md`), so the Docker-built path needs no new Dockerfile, only
the registry entry.

### Files prepared

`docker/mcp-catalog/` in this repository holds the three files a submission
PR copies into `servers/codecalc/` in a `docker/mcp-registry` fork:

- **`server.yaml`** — `image: mcp/codecalc`, `source.project` pointing at
  this repo, `source.commit` pinned to a specific SHA (the registry's
  validator requires a full 40-character lowercase SHA1, not a branch), and
  `source.dockerfile: docker/mcp-server.Dockerfile`. No `config:` block —
  codecalc needs no secrets or environment variables to run.
- **`tools.json`** — the live `tools/list` response of the exact image
  `docker build -f docker/mcp-server.Dockerfile .` produces, captured over
  the real MCP stdio handshake (`initialize` → `notifications/initialized`
  → `tools/list`), 49 entries, each argument carrying `optional: true` when the tool's input schema does not require it. `CODECALC_TOOLS` is unset in the Dockerfile,
  which registers every tool group (the `full` preset) — so this is the
  actual shipped surface, not `core`. A few of those 49 (`evaluate_expression`,
  `z3_check`, `symbolic`, and `analyze_complexity`'s grammar-based
  parsing) report their extra as not installed rather than erroring when
  called, because this image ships base `codecalc`, not `codecalc[full]`
  (documented already in `docker/README.md` and the Dockerfile itself — an
  arm64 wheel-availability gap in `z3-solver`, not an oversight). Providing
  `tools.json` is not required for a server with no startup configuration —
  the registry's own `task build --tools` can run the container and list
  tools live — but it is included so the registry's CI reads a versioned,
  reviewable list instead of running a container on every check, per
  `CONTRIBUTING.md`'s "Avoiding `build --tools` failures" section.
- **`readme.md`** — one line pointing at this repository, matching the
  convention every other local-server entry in `docker/mcp-registry` uses.

### Validated locally

Both of the registry's own gates were run against these exact files (cloned
`docker/mcp-registry`, `servers/codecalc/` populated from the three files
above, `GITHUB_TOKEN` set from `gh auth token` so the license check hits the
GitHub API authenticated):

```
$ go run ./cmd/validate --name codecalc
✅ Name is valid
✅ Directory is valid
✅ Title is valid
✅ YAML formatting is valid
✅ Commit is pinned
✅ Secrets are valid
✅ Config env is valid
✅ License is valid
✅ Icon is valid
✅ Remote validation skipped (not a remote server)
✅ OAuth dynamic configuration is valid

$ go run ./cmd/build --tools codecalc
... (git-context build of https://github.com/The-40-Thieves/codecalc.git#<pinned commit>
     using docker/mcp-server.Dockerfile) ...
52 tools found.
✅ Image built as mcp/codecalc
```

The build step actually cloned the pinned commit from GitHub and built
`docker/mcp-server.Dockerfile` — it is not a dry run — and the "52 tools
found" line came from reading the committed `tools.json`, matching the
count independently verified against the running container over stdio.

### Opening the PR (not yet done — coordinator's call)

1. Fork `docker/mcp-registry` and clone the fork.
2. `mkdir -p servers/codecalc` and copy in this repo's `docker/mcp-catalog/server.yaml`, `tools.json`, and `readme.md` unchanged, renaming none of them.
3. `export GITHUB_TOKEN=$(gh auth token)` (avoids the license check's unauthenticated GitHub API rate limit), then re-run `go run ./cmd/validate --name codecalc` and `go run ./cmd/build --tools codecalc` inside the fork to reconfirm against whatever the registry's `main` looks like at PR time.
4. Commit only `servers/codecalc/`, push a branch, and open a PR using the repository's own `.github/PULL_REQUEST_TEMPLATE.md`.
   - **Title:** `Add codecalc MCP server`
   - **Body:** fill in the template's `Server Name` (`codecalc`), `Repository URL` (`https://github.com/The-40-Thieves/codecalc`), and `Brief Description` (the same one-line description `server.yaml` carries), then check every box under "Basic Requirements" — Apache-2.0 is an accepted license, the server implements the MCP spec, the repository has recent commits, `docker/mcp-server.Dockerfile` is the Docker artifact, `README.md`/`docker/README.md` are the documentation, and `SECURITY.md` is the security contact — and every box under "Submitter Checklist", since both `task validate` and `task build --tools` were run and passed as quoted above. No test credentials to share (codecalc needs no secrets).
5. Every PR gets a review from the Docker team before merging; once approved, the PR is squashed into a single commit.

### After acceptance

Docker states listings go live "within 24 hours" of approval, at all three of:

- The [MCP catalog](https://hub.docker.com/mcp) (search "codecalc")
- Docker Desktop's MCP Toolkit
- [Docker Hub's `mcp` namespace](https://hub.docker.com/u/mcp) — `mcp/codecalc`, Docker-built and Docker-signed

Once live, add the catalog to README.md's list of where codecalc is
published (not done yet — deliberately: `scripts/check_claims.py` gates
README text, and a listing URL that does not resolve yet has no business
being asserted there).
