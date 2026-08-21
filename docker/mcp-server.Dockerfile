# The codecalc MCP server image, for the Docker MCP Catalog (THE-870).
#
# This is NOT docker/executor.Dockerfile. That image is the trusted artifact
# the STRICT gVisor boundary launches (`DockerGVisorRuntime`, runtime=runsc)
# on a host that has Docker+runsc available to it. This image is the MCP
# SERVER ITSELF, packaged to run as an ordinary container under the Docker
# MCP Toolkit / any MCP client's own container runtime.
#
# HONESTY (read before assuming parity with the README's "strict boundary"):
# a container built from this Dockerfile cannot nest gVisor/runsc inside
# itself (no privileged docker-in-docker here), so codecalc running in THIS
# image always falls back to its DEFAULT RLIMIT SANDBOX
# (codecalc.executor.backend() == "rust", the same rlimit/seccomp-lite
# backend used on a bare install without the opt-in strict runtime) — never
# the gVisor+Docker strict boundary described in README.md's "three things"
# list. `codecalc doctor` run inside this container will report
# `strict_runtime` prerequisites unmet; that is expected and correct, not a
# bug in the image. Treat isolation the same way you would any other
# container: process/user isolation from the host, not the additional
# syscall-filtering boundary the strict runtime adds on Linux+Docker hosts.
#
# LANGUAGE RUNTIMES: codecalc supports 31 languages (codecalc/registry.py);
# shipping all of them here would balloon the image for little benefit (most
# callers use a handful). This image bundles a deliberately small, broadly
# useful subset instead of trying for parity with the full matrix:
#
#   python3  (base image)       node      ruby      php       perl
#   bash/awk (base image)       jq        sqlite3   lua
#   c / cpp (gcc, g++)
#
# `go` was tried and dropped: `golang-go` on Debian bookworm pulls in
# ~450MB (golang-1.19-go + golang-1.19-src) on its own — roughly a third of
# this image's total size for one language — measured with `docker history`
# during THE-870, not assumed.
#
# Anything else (`go`, `rust`, `java`, `csharp`, `swift`, `kotlin`, `deno`,
# `bun`, `typescript`, `r`, `elixir`, `erlang`, `zig`, `fortran`, `gleam`,
# `haskell`, `mojo`, `tcl`, `zsh`) is NOT installed. `list_languages` reports
# those as `supported` (codecalc knows the plan) but not `installed` — which
# is the honest state, not an error. Extend this Dockerfile with more
# `apt-get install` targets (and, for `lua`, a PATH symlink — see below) if
# your use case needs a runtime that isn't here.
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="codecalc" \
      org.opencontainers.image.description="Code & logic calculator MCP server: execute code in multiple languages, symbolic math, SMT logic, complexity analysis. Runs codecalc's default rlimit sandbox (NOT the opt-in gVisor strict boundary, which needs host Docker+runsc outside this container)." \
      org.opencontainers.image.source="https://github.com/The-40-Thieves/codecalc" \
      org.opencontainers.image.licenses="Apache-2.0"

# tini: codecalc runs as PID 1 in this image and spawns a child process per
# `execute_code` call. As PID 1, the kernel gives SIGTERM its default (ignore)
# disposition unless the process installs its own handler, and any reparented
# grandchild relies on PID 1 to reap it. docker/executor.Dockerfile documents
# the same class of problem for codecalc-exec and fixes it the same way: tini
# as a real init above the server, not the server standing in as PID 1 itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        # ── interpreters ────────────────────────────────────────────────
        nodejs \
        ruby \
        php-cli \
        perl \
        gawk \
        lua5.4 \
        # ── compilers ────────────────────────────────────────────────────
        gcc \
        g++ \
        # ── data / query DSLs ───────────────────────────────────────────
        jq \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    # Debian's lua package installs as `lua5.4`; codecalc's registry runs
    # the plain `lua` command (registry.py: "lua": _c(None, "lua {file}")).
    && ln -s /usr/bin/lua5.4 /usr/local/bin/lua

# Base codecalc only — NOT `codecalc[full]`. Two reasons, not one:
#  (1) matches what a plain `pip install codecalc` gives every other user
#      (pyproject.toml's own tradeoff: the symbolic/parsing extras are ~90MB
#      that a caller who "only runs code" shouldn't have to download);
#  (2) measured on this box: z3-solver's latest resolvable version (5.1.0.0,
#      satisfying codecalc's `z3-solver>=4.13,<6`) currently ships NO
#      prebuilt wheel for linux/arm64 on PyPI — only an sdist that needs
#      cmake/ninja to build, which this image doesn't carry. Since the
#      Docker MCP Catalog build is expected to target both amd64 and arm64,
#      pulling `[full]` in here would make the arm64 build brittle against
#      an upstream wheel-availability gap that is unrelated to this image.
# The wheel bundles the Rust codecalc-exec binary for this platform, so
# `execute_code` and every non-symbolic tool work with nothing to compile.
# `evaluate_expression` / `z3_check` / `solve_linear` /
# `analyze_complexity`'s parsing report "extra not installed" inside this
# image — `codecalc doctor` surfaces exactly that, by design (see
# pyproject.toml). Rebuild with `--build-arg CODECALC_EXTRA=full` (x86_64
# only, until the arm64 z3-solver wheel gap above closes) to get them.
ARG CODECALC_VERSION=0.4.0
ARG CODECALC_EXTRA=
RUN pip install --no-cache-dir "codecalc${CODECALC_EXTRA:+[$CODECALC_EXTRA]}==${CODECALC_VERSION}"

# Non-root, matching docker/executor.Dockerfile's own posture — good
# practice generally, and specifically for an image whose job is running
# arbitrary caller-submitted code.
RUN useradd --create-home --uid 1000 codecalc
USER 1000:1000
WORKDIR /home/codecalc

# Stdio MCP server: what the Docker MCP Toolkit / any MCP client spawns as
# this container's command. tini reaps codecalc's execute_code children and
# forwards signals; it does not hardcode `codecalc` so a debugging override
# (`docker run ... codecalc doctor`) still runs under the same init.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["codecalc"]
