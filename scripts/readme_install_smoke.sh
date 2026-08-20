#!/usr/bin/env bash
# README from-source install smoke test (THE-877).
#
# WHY THIS EXISTS. Issue #199: the "From source" block in README.md's Install
# section did `cp executor/target/release/codecalc-exec bin/` with no `mkdir -p
# bin` first, and `bin/` is gitignored — it does not exist in a fresh clone. The
# documented steps failed (or, on some shells, created a plain FILE named `bin`)
# for anyone who actually followed them. Nothing in CI ran the README's own
# commands, so the break shipped unnoticed. #200 was the same shape one layer
# over: the offline-warm-up command it told installed users to run lived in
# `scripts/`, which is not in the wheel.
#
# This script EXTRACTS the "From source" fenced bash block from README.md
# itself — rather than a hand-copied transcript of it — and runs it, so a
# future edit to those steps is what CI proves, not a maintainer's memory of
# what they used to say.
#
# The `git clone`/`cd codecalc` lines are dropped: this runs inside the PR's
# own checkout (so it proves the change under test, not whatever is on `main`
# right now), and the rest of the block is what actually installs and builds.
#
# Scope: from-source only, on the host's already-present toolchain (uv +
# cargo). It does not attempt the other 30 optional runtimes `execute_code`
# can use — `doctor` reporting a healthy rust backend with python3 available is
# the from-source install's own documented success criterion, not full
# language coverage.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

README="$REPO/README.md"

block=$(awk '/\*\*From source\*\*/{f=1} f && /^```bash/{c=1; next} c && /^```/{exit} c{print}' "$README")

if [ -z "$block" ]; then
  echo "::error::could not find the 'From source' install code block in README.md — the extractor matched nothing"
  exit 1
fi

echo "== extracted README from-source block =="
echo "$block"
echo "=========================================="

filtered=$(printf '%s\n' "$block" | grep -Ev '^\s*(git clone|cd codecalc)\b')

if [ -z "$filtered" ]; then
  echo "::error::nothing left to run after dropping the clone/cd lines — the extractor or the filter is broken"
  exit 1
fi

# Clean-workdir precondition, the actual shape of #199: bin/ must not exist
# going in, the same as it would not after a fresh `git clone`.
rm -rf bin
if [ -e bin ]; then
  echo "::error::bin/ still exists after rm -rf — the clean-workdir precondition failed"
  exit 1
fi

echo "== running the extracted commands =="
# Written to a file and run as a script, NOT fed through an unquoted heredoc:
# the README's own comments contain backticks (`` `rust` ``), and an unquoted
# heredoc performs command substitution on backticks during expansion — before
# the inner shell ever gets to treat that text as a comment. A temp file hands
# the extracted text to bash's own lexer instead, where `#` really does start
# a comment.
extracted_script=$(mktemp "${TMPDIR:-/tmp}/readme-install-smoke.XXXXXX.sh")
trap 'rm -f "$extracted_script"' EXIT
printf '%s\n' "$filtered" > "$extracted_script"
bash -xeuo pipefail "$extracted_script"

echo "== asserting the documented outcome =="
# The README's own promised verification step, run again independently of the
# block above so a doctor call REMOVED from the README (rather than merely
# broken) still fails this job.
uv run --no-sync codecalc doctor
uv run --no-sync python -c "
from codecalc import executor
backend = executor.backend()
print('backend:', backend)
assert backend == 'rust', f'expected the rust backend after a from-source build, got {backend!r}'
"

echo "README from-source install smoke test: OK"
