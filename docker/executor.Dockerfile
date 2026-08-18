# The trusted CodeCalc executor image for the Linux strict boundary (THE-828).
#
# This is the artifact `DockerGVisorRuntime` launches under `--runtime=runsc`:
# it carries `codecalc-exec` (the sandboxed multi-language executor) and its
# `blocknet.so` LD_PRELOAD shim, plus one language runtime (python3) so the
# boundary can actually run a workload. gVisor's application kernel is the
# strict syscall/filesystem boundary; this image only has to be minimal,
# non-root, and self-contained.
#
# Multi-stage: the builder compiles the Rust core from source (so the binary in
# the image is provably built from this repo, not copied from a dev box), and
# the final stage is a minimal glibc runtime with nothing but python3 and the
# executor. Both stages are bookworm/glibc so the binary and shim built in the
# first stage run unchanged in the second.
#
# BUILD (from the repo root, on a runsc-capable host — see scripts/build_executor_image.sh):
#   docker build -f docker/executor.Dockerfile -t codecalc-exec:strict .
#
# RESIDUAL, stated plainly: a registry-published, multi-architecture,
# DIGEST-PINNED image is a release step (it needs registry credentials) and is
# NOT produced here. `GVisorConfig` still requires a digest-pinned reference for
# the production execution path; this local build is what proves the boundary on
# a runsc host until that release image exists.

# ── builder: compile codecalc-exec + blocknet.so from source ────────────────
FROM rust:1-slim-bookworm AS builder

# cc for build.rs's blocknet shim (-shared -fPIC). Nothing else is needed:
# the crate has no system dependencies of its own.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
# rust-toolchain.toml pins the exact compiler; copy it first so the layer that
# resolves the toolchain is cached independently of the sources.
COPY executor/rust-toolchain.toml ./rust-toolchain.toml
COPY executor/ ./

# RUSTC_WRAPPER is cleared: any host sccache config must not leak into the image
# build, matching how the repo is built elsewhere. build.rs compiles blocknet.so
# beside the binary in target/release.
RUN RUSTC_WRAPPER= cargo build --release \
    && test -f target/release/codecalc-exec \
    && test -f target/release/blocknet.so

# ── runtime: minimal, non-root, python3 + the executor ──────────────────────
FROM python:3.12-slim-bookworm

# tini is an init, and it is REQUIRED, not cosmetic. `DockerGVisorRuntime` runs
# `codecalc-exec` as the container command, which would otherwise be PID 1 — and
# codecalc-exec is designed to be a SUBPROCESS of the codecalc server, not an
# init. As PID 1 its child-reaping and PR_SET_PDEATHSIG handling break: the
# executed program is killed before it produces output (verified: the exact
# symptom without an init is verdict=RTE, empty stdout; `docker --init` or this
# tini both fix it). tini reaps zombies and forwards signals so codecalc-exec
# runs as an ordinary process with a real init above it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# The executor resolves its shim next to its own binary (current_exe()), so the
# two travel together into one directory. Nothing else is added to the image.
COPY --from=builder /build/target/release/codecalc-exec /usr/local/bin/codecalc-exec
COPY --from=builder /build/target/release/blocknet.so /usr/local/bin/blocknet.so

# Default to an unprivileged UID. The strict runtime ALSO passes
# `--user=65534:65534` at launch, so this is defense in depth, not the only
# control — and it keeps a bare `docker run` of the image non-root too.
USER 65534:65534

# tini as an init-only entrypoint: it does NOT hardcode codecalc-exec, because
# the strict runtime supplies the full command (`codecalc-exec --lang <name>
# --timeout <secs>`) and a conformance harness supplies its own (`python3 -c
# <payload>`). Whatever the command, it runs as tini's child with proper
# reaping — never as PID 1.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["codecalc-exec"]
