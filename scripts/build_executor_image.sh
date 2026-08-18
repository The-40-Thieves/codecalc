#!/usr/bin/env bash
# Build the trusted CodeCalc executor image for the strict boundary (THE-828).
#
# Produces a LOCAL image tagged `codecalc-exec:strict` (override with
# CODECALC_STRICT_IMAGE) from docker/executor.Dockerfile. This is the image the
# gVisor conformance suite (tests/test_gvisor_conformance.py) and doctor's
# strict-runtime canary look for.
#
# It does NOT publish a registry-pinned, multi-arch, digest-pinned image: that is
# the `publish-executor-image` workflow's job (it builds the SAME Dockerfile for
# amd64+arm64, pushes to ghcr.io/the-40-thieves/codecalc-exec, and digest-pins
# docker/executor-image.lock for the production execution path). This script
# builds the reproducible LOCAL diagnostic artifact that proves the boundary on a
# runsc-capable host.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${CODECALC_STRICT_IMAGE:-codecalc-exec:strict}"

echo "building executor image ${IMAGE} from ${REPO_ROOT}"
docker build \
  -f "${REPO_ROOT}/docker/executor.Dockerfile" \
  -t "${IMAGE}" \
  "${REPO_ROOT}"

echo "built ${IMAGE}:"
docker image inspect "${IMAGE}" --format '  id={{.Id}} size={{.Size}} arch={{.Architecture}}'
