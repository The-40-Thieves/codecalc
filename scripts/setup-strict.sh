#!/usr/bin/env bash
# One-command bootstrap for the Linux gVisor+Docker strict execution backend
# (THE-869). Automates the manual steps in docs/deployment/README.md §2:
#
#   1. preflight — Docker present, cgroup v2 active, the `runsc` runtime
#      registered with Docker (codecalc/strict_runtime.py::host_prerequisites)
#   2. pull      — the digest-pinned executor image named in
#      docker/executor-image.lock (the ONLY reference the production
#      execution path will run — codecalc/strict_runtime.py::published_strict_image)
#   3. token     — generate CODECALC_STRICT_SERVICE_TOKEN if it isn't already
#      set (serve-strict refuses to start without one)
#   4. canary    — `doctor --deep`, pointed at the exact image just pulled, so
#      the canary proves the PRODUCTION image boots under runsc
#      (codecalc/strict_runtime.py::_startup_canary), not a separate local
#      diagnostic build
#   5. launch    — start `serve-strict` on loopback, or (the default) stop
#      after a green canary and print the exact command to run it yourself
#
# Deliberately does NOT install Docker or gVisor, and never runs apt/sudo:
# registering the `runsc` runtime is the operator's own explicit step
# (docs/deployment/README.md §2.1) and this script only checks for it and
# prints the exact fix. It also never containerizes `serve-strict` itself —
# it needs the HOST Docker daemon to spawn runsc containers, so it always
# runs directly on this host, not inside a container with a mounted socket.
#
# Idempotent: every step tolerates being re-run — a present image pull is a
# no-op, an already-exported token is reused, the canary just re-verifies.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

DOCKER_BIN="docker"
RUNSC_RUNTIME="runsc"
IMAGE_LOCK="${REPO_ROOT}/docker/executor-image.lock"

CHECK_ONLY=1
SERVE_HOST="127.0.0.1"
SERVE_PORT="8000"
DIGEST_REF=""
PYTHON_CMD=()

usage() {
  cat <<'EOF'
Usage: scripts/setup-strict.sh [OPTIONS]

Bootstraps the Linux gVisor+Docker strict execution backend documented in
docs/deployment/README.md §2, in roughly one command: preflight checks, pull
the digest-pinned executor image, generate a service token if needed, run the
deep doctor canary against that exact image, then either launch `serve-strict`
or print the exact command to do so.

Options:
  --check-only, --no-serve   Preflight + pull + canary, then PRINT the launch
                              command instead of starting the server. This is
                              the DEFAULT — nothing is left running.
  --serve                    After a green canary, exec `serve-strict` bound
                              to loopback in the foreground (Ctrl-C to stop).
  --host HOST                Bind host for --serve (default 127.0.0.1).
  --port PORT                Bind port for --serve (default 8000).
  -h, --help                  Show this help and exit.

Environment:
  CODECALC_STRICT_SERVICE_TOKEN  Reused if already set; otherwise generated
                                  with `openssl rand -hex 32` and printed once.
  CODECALC_PYTHON                Python interpreter to run codecalc with.
                                  Default: .venv/bin/python if present, else
                                  `uv run python`, else `python3`.

Never installs Docker or gVisor and never runs apt/sudo — registering the
`runsc` runtime with Docker is your own explicit step; see the printed fix
command, or docs/deployment/README.md §2.1, if preflight fails.
EOF
}

log_step() {
  printf '\n== %s ==\n' "$1"
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --check-only|--no-serve)
        CHECK_ONLY=1
        shift
        ;;
      --serve)
        CHECK_ONLY=0
        shift
        ;;
      --host)
        [ $# -ge 2 ] || { echo "setup-strict.sh: --host needs a value" >&2; exit 2; }
        SERVE_HOST="$2"
        shift 2
        ;;
      --port)
        [ $# -ge 2 ] || { echo "setup-strict.sh: --port needs a value" >&2; exit 2; }
        SERVE_PORT="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "setup-strict.sh: unknown option: $1" >&2
        usage >&2
        exit 2
        ;;
    esac
  done
}

# The same interpreter resolution order README.md's own quickstart implies:
# an already-set up venv wins, then `uv run` (auto-syncs the project from
# pyproject.toml — the one-command path on a fresh checkout), then a bare
# python3 assumed to have codecalc already installed (e.g. `pip install
# codecalc`, per the Install section of README.md).
resolve_python() {
  if [ -n "${CODECALC_PYTHON:-}" ]; then
    PYTHON_CMD=("${CODECALC_PYTHON}")
  elif [ -x "${REPO_ROOT}/.venv/bin/python" ]; then
    PYTHON_CMD=("${REPO_ROOT}/.venv/bin/python")
  elif command -v uv >/dev/null 2>&1 && [ -f "${REPO_ROOT}/pyproject.toml" ]; then
    PYTHON_CMD=(uv run python)
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD=(python3)
  else
    echo "setup-strict.sh: no Python found (checked \$CODECALC_PYTHON," >&2
    echo "  .venv/bin/python, uv, python3). Install one, or set" >&2
    echo "  CODECALC_PYTHON to an interpreter with codecalc installed." >&2
    exit 1
  fi
}

run_codecalc() {
  "${PYTHON_CMD[@]}" -m codecalc.server "$@"
}

# ---------- 1. preflight ----------------------------------------------------
preflight() {
  log_step "1/5 preflight"
  local failed=0

  if ! command -v "${DOCKER_BIN}" >/dev/null 2>&1; then
    echo "  MISSING  docker: not found on PATH"
    echo "           Fix: install Docker Engine for your distro, then re-run."
    failed=1
  elif ! "${DOCKER_BIN}" info >/dev/null 2>&1; then
    echo "  MISSING  docker: daemon unreachable (stopped, or no permission)"
    echo "           Fix: start it ('sudo systemctl start docker') and confirm"
    echo "                'docker info' works without sudo, then re-run."
    failed=1
  else
    echo "  OK       docker present ($("${DOCKER_BIN}" --version))"

    local cgroup_version
    cgroup_version="$("${DOCKER_BIN}" info --format '{{.CgroupVersion}}' 2>/dev/null || true)"
    if [ "${cgroup_version}" != "2" ]; then
      echo "  MISSING  cgroup v2: Docker reports CgroupVersion=${cgroup_version:-unknown}"
      echo "           Fix: verify for your platform against gVisor's install"
      echo "                docs: https://gvisor.dev/docs/user_guide/install/"
      failed=1
    else
      echo "  OK       cgroup v2 active"
    fi

    if ! "${DOCKER_BIN}" info --format '{{json .Runtimes}}' 2>/dev/null \
        | grep -q "\"${RUNSC_RUNTIME}\""; then
      echo "  MISSING  the '${RUNSC_RUNTIME}' runtime is not registered with Docker"
      echo "           Fix (Debian/Ubuntu, gVisor's own upstream procedure —"
      echo "           verify for your platform:"
      echo "           https://gvisor.dev/docs/user_guide/install/):"
      echo
      echo "             sudo apt-get update && sudo apt-get install -y \\"
      echo "               apt-transport-https ca-certificates curl gnupg"
      echo "             curl -fsSL https://gvisor.dev/archive.key \\"
      echo "               | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg"
      echo "             echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main\" \\"
      echo "               | sudo tee /etc/apt/sources.list.d/gvisor.list"
      echo "             sudo apt-get update && sudo apt-get install -y runsc"
      echo
      echo "             sudo runsc install"
      echo "             sudo systemctl restart docker"
      failed=1
    else
      echo "  OK       '${RUNSC_RUNTIME}' runtime registered"
    fi
  fi

  if [ "${failed}" -ne 0 ]; then
    echo
    echo "Preflight failed. This script will not install Docker, gVisor, or run"
    echo "apt/sudo for you — see the fix command(s) above, or"
    echo "docs/deployment/README.md §2.1. Fix the item(s), then re-run"
    echo "scripts/setup-strict.sh."
    exit 1
  fi
}

# ---------- 2. pull the digest-pinned executor image ------------------------
pull_image() {
  log_step "2/5 pull executor image"
  DIGEST_REF="$(grep -E '^ghcr\.io.*@sha256:' "${IMAGE_LOCK}" || true)"
  if [ -z "${DIGEST_REF}" ]; then
    echo "  FAILED  no digest-pinned image in docker/executor-image.lock yet."
    echo "          Run the publish-executor-image workflow (workflow_dispatch"
    echo "          on .github/workflows/publish-executor-image.yml) to build,"
    echo "          push, and pin it, then re-run this script."
    exit 1
  fi
  echo "  pulling ${DIGEST_REF}"
  "${DOCKER_BIN}" pull "${DIGEST_REF}"
  echo "  OK       image present: ${DIGEST_REF}"
}

# ---------- 3. service token --------------------------------------------
ensure_token() {
  log_step "3/5 service token"
  if [ -n "${CODECALC_STRICT_SERVICE_TOKEN:-}" ]; then
    echo "  OK       CODECALC_STRICT_SERVICE_TOKEN already set — reusing it"
    return
  fi
  if ! command -v openssl >/dev/null 2>&1; then
    echo "  FAILED  openssl not found; cannot generate a token."
    echo "          Set CODECALC_STRICT_SERVICE_TOKEN yourself and re-run."
    exit 1
  fi
  CODECALC_STRICT_SERVICE_TOKEN="$(openssl rand -hex 32)"
  export CODECALC_STRICT_SERVICE_TOKEN
  echo "  generated a new token — printed ONCE, store it now:"
  echo
  echo "    CODECALC_STRICT_SERVICE_TOKEN=${CODECALC_STRICT_SERVICE_TOKEN}"
}

# ---------- 4. deep canary ---------------------------------------------------
run_canary() {
  log_step "4/5 deep canary (gvisor-v1)"
  echo "  running doctor --deep against ${DIGEST_REF}"
  local doctor_json canary_ok runtime_observed
  # CODECALC_STRICT_IMAGE overrides only the DIAGNOSTIC lookup doctor/the
  # canary use (codecalc/strict_runtime.py::strict_image) — it points that
  # diagnostic at the exact production digest just pulled, so the canary
  # proves the boundary for the image that will actually run, not a
  # separately built local tag. serve-strict itself always resolves the
  # production image from docker/executor-image.lock regardless of this
  # variable, so nothing here leaks into step 5.
  doctor_json="$(CODECALC_STRICT_IMAGE="${DIGEST_REF}" run_codecalc doctor --deep --json)"
  canary_ok="$(printf '%s' "${doctor_json}" | "${PYTHON_CMD[@]}" -c '
import json, sys

try:
    report = json.load(sys.stdin)
except json.JSONDecodeError:
    print("false")
    sys.exit(0)

strict = report.get("strict_runtime") or {}
canary = strict.get("canary") or {}
ok = bool(strict.get("available")) and bool(canary.get("verified_runsc"))
print("true" if ok else "false")
')"
  if [ "${canary_ok}" != "true" ]; then
    echo "  FAILED  the deep canary could not prove the gvisor-v1 boundary:"
    echo "${doctor_json}"
    exit 1
  fi
  runtime_observed="$(printf '%s' "${doctor_json}" | "${PYTHON_CMD[@]}" -c '
import json, sys
report = json.load(sys.stdin)
print(report["strict_runtime"]["canary"]["runtime_observed"])
')"
  echo "  OK       canary ran under '${runtime_observed}' — verified out of"
  echo "           band via 'docker inspect .HostConfig.Runtime' (not a string"
  echo "           the payload printed). gvisor-v1 isolation profile confirmed"
  echo "           (codecalc/strict_runtime.py::ISOLATION_PROFILE)."
}

# ---------- 5. launch, or print the launch command ---------------------------
finish() {
  log_step "5/5 launch"
  if [ "${CHECK_ONLY}" -eq 1 ]; then
    echo "  --check-only (default): not starting the server."
    echo
    echo "  Launch it yourself with:"
    echo
    echo "    export CODECALC_STRICT_SERVICE_TOKEN=${CODECALC_STRICT_SERVICE_TOKEN}"
    echo "    ${PYTHON_CMD[*]} -m codecalc.server serve-strict --host ${SERVE_HOST} --port ${SERVE_PORT}"
    echo
    echo "  Every request must then carry: Authorization: Bearer <token above>"
  else
    echo "  starting serve-strict on ${SERVE_HOST}:${SERVE_PORT} (foreground; Ctrl-C to stop)"
    exec "${PYTHON_CMD[@]}" -m codecalc.server serve-strict \
      --host "${SERVE_HOST}" --port "${SERVE_PORT}"
  fi
}

main() {
  parse_args "$@"
  resolve_python
  preflight
  pull_image
  ensure_token
  run_canary
  finish
}

main "$@"
