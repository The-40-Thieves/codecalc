#!/usr/bin/env bash
# Reproduce the full gVisor strict-boundary conformance on a runsc-capable host
#. Builds the executor image, then runs the hostile-workload suite
# against real containers under `--runtime=runsc`.
#
# This is the CAVE-runnable target: GitHub-hosted runners have no runsc, so CI
# only runs the suite in skip mode (see .github/workflows/ci-python.yml). Run
# THIS on Cave / any host with `runsc` registered to prove the boundary:
#
#   scripts/gvisor_conformance.sh
#
# It fails loudly if runsc is not registered — on this target that is an error,
# not a skip, because the whole point of running it here is the real boundary.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

if ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q runsc; then
  echo "::error::the 'runsc' runtime is not registered with Docker on this host."
  echo "This target proves the REAL gVisor boundary and needs runsc. Install"
  echo "gVisor and register it as a Docker runtime, then re-run."
  exit 1
fi

echo "== building the executor image =="
bash "${REPO_ROOT}/scripts/build_executor_image.sh"

echo
echo "== running the hostile-workload conformance under runsc =="
if command -v uv >/dev/null 2>&1; then
  uv run --no-sync python tests/test_gvisor_conformance.py
else
  python3 tests/test_gvisor_conformance.py
fi
