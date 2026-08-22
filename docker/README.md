# codecalc container images

Two different Dockerfiles live in this directory, for two different jobs.
Do not conflate them.

## `mcp-server.Dockerfile` — the MCP server

Packages codecalc itself as a container: `pip install codecalc` plus a
handful of language runtimes, run as the stdio MCP server. This is the image
submitted to the [Docker MCP Catalog](https://hub.docker.com/mcp) / Docker
Desktop's MCP Toolkit.

```
docker build -f docker/mcp-server.Dockerfile -t codecalc-mcp .
docker run --rm -i codecalc-mcp                # speaks MCP JSON-RPC on stdio
```

**Isolation, precisely stated:** this image runs codecalc's **default rlimit
sandbox** — the same backend a bare `pip install codecalc` gets without any
extra setup. It does **not** run the opt-in **strict isolation boundary**
(gVisor + Docker on Linux, AppContainer on Windows) that README.md's "three
things nobody else offers together" describes. That boundary needs a host
that can launch `runsc`-backed containers itself; a plain container spawned
by the Docker MCP Toolkit (or any other MCP client) cannot nest that inside
itself. `codecalc doctor` run inside this image reports the strict runtime's
prerequisites as unmet — that's correct, not a bug. Treat this image's
isolation the same way you'd treat any other container that runs
caller-submitted code: process/user isolation from the host, nothing more.

**Language runtimes bundled:** `python3`, `node`, `ruby`, `php`, `perl`,
`bash`, `awk`, `lua`, `jq`, `sqlite3`, `c`, `c++`/`cpp` — 13 of codecalc's 31
supported languages (`codecalc list_languages` / `codecalc doctor` enumerate
the rest as "supported" but not "installed"). Chosen for broad usefulness
against image size; see the Dockerfile's own comments for what was tried and
dropped (`go`, notably — ~450MB on its own) and how to add more.

**Symbolic extras:** ships base `codecalc`, not `codecalc[full]` —
`evaluate_expression`, `z3_check`, `solve_linear`, and `analyze_complexity`'s
grammar-based parsing report their extra as not installed. See the
Dockerfile for why (short version: the current top-of-range `z3-solver`
version has no prebuilt wheel for `linux/arm64`, and this image targets both
architectures).

## `executor.Dockerfile` — the strict-runtime executor image

Unrelated to the MCP server packaging above. This is the trusted artifact
`DockerGVisorRuntime` launches under `--runtime=runsc` on a host that
already has Docker + gVisor: `codecalc-exec` (its `no_net` enforced in-kernel
via a seccomp-bpf filter on this Linux target; `blocknet.so` ships alongside
as the fallback), built from this repo's Rust source, published to
`ghcr.io/the-40-thieves/codecalc-exec` and pinned by digest in
`executor-image.lock`. See that Dockerfile's own header for the full
rationale. Building `mcp-server.Dockerfile` neither uses nor requires this
image.
