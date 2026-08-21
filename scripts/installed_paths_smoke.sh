#!/usr/bin/env bash
# README installed-path install smoke test (GH #215).
#
# WHY THIS EXISTS. `readme_install_smoke.sh` runs the README's **From source**
# block, but the commands that actually broke in #199/#200/#201 are the
# *installed* ones — a user who ran `pip install`/`uvx` and then followed the
# README:
#   - #200: `codecalc-prefetch-grammars` did not exist in the wheel at all
#           (it lived only in `scripts/`, which the wheel does not ship).
#   - #201: `codecalc --help` / `--version` printed nothing and exited 0.
# Nothing in CI installed a built wheel and ran those, so the breaks shipped.
# #215 is the same class one layer up: main documented `codecalc-prefetch-
# grammars` under a version PyPI was already serving WITHOUT it, and no gate
# could see the gap because it only compared version STRINGS.
#
# This builds the wheel from THIS checkout (the artifact about to ship, not
# whatever PyPI serves — so it stays offline/bare-toolchain and proves the
# change under test), installs it into a throwaway venv, and runs the
# README's installed-path commands, asserting each EXISTS and produces the
# output the README promises. A pure-Python wheel is enough: every assertion
# here is about a console entry point existing and printing something, not
# about the Rust backend (the from-source smoke already covers that).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail=0
note() { printf '%s\n' "== $* =="; }
check() { # check <name> <condition-exit> <detail>
  if [ "$2" -eq 0 ]; then printf 'PASS %s %s\n' "$1" "${3:-}"
  else printf 'FAIL %s %s\n' "$1" "${3:-}"; fail=1; fi
}

note "build a wheel from this checkout"
# The `parsing` extra is what the README's installed grammar-prefetch command
# needs; install it so the command under test can actually run. (Base gives
# the CLI; parsing adds tree-sitter-language-pack for the cache-dir lookup.)
uv build --wheel --out-dir "$WORK/dist"
WHEEL="$(find "$WORK/dist" -name '*.whl' -print -quit)"
if [ -z "$WHEEL" ]; then
  echo "::error::uv build produced no wheel — nothing to install or test"
  exit 1
fi
echo "built: $WHEEL"

note "install the wheel into a throwaway venv"
uv venv "$WORK/venv" >/dev/null
VPY="$WORK/venv/bin"
# Install the [parsing] extra by name against the local wheel so the grammar
# command's dependency resolves without reaching PyPI for the package itself.
uv pip install --python "$VPY/python" "codecalc[parsing] @ $WHEEL" >/dev/null

# The version the wheel actually declares — assert the CLI agrees with it,
# which is the drift #215 is about (a documented command out of step with the
# artifact). Read from the installed metadata, not the repo, so this proves
# the built artifact.
PKG_VER="$("$VPY/python" -c 'import importlib.metadata as m; print(m.version("codecalc"))')"
echo "installed codecalc $PKG_VER"

note "run the README's installed-path commands"

# stdout only for the content assertions: a C extension (tree-sitter) can emit
# an unrelated Python free-threading RuntimeWarning on STDERR, and merging it
# would corrupt the value under test. The exit code is captured separately, so
# a command that fails is still caught — its stderr just is not the thing we
# grep. `run <cmd...>` sets $out (stdout) and $rc (status) without tripping
# `set -e`.
run() { if out="$("$@" 2>/dev/null)"; then rc=0; else rc=$?; fi; }

# #201: --version must print the version and exit 0 (it used to print nothing).
run "$VPY/codecalc" --version
check "codecalc --version exits 0" "$rc" "-> ${out:0:60}"
case "$out" in *"$PKG_VER"*) c=0;; *) c=1;; esac
check "codecalc --version prints the installed version ($PKG_VER)" "$c" "-> ${out:0:60}"

# #201: --help must print a usage block naming a subcommand (was empty).
run "$VPY/codecalc" --help
check "codecalc --help exits 0" "$rc"
case "$out" in *doctor*) c=0;; *) c=1;; esac
check "codecalc --help names a subcommand (doctor)" "$c" "-> $(printf '%s' "$out" | head -1)"

# #200: the grammar-prefetch console script must EXIST and print a cache dir
# (this is the command that was documented but absent from the wheel).
run "$VPY/codecalc-prefetch-grammars" --print-cache-dir
check "codecalc-prefetch-grammars --print-cache-dir exits 0" "$rc" "-> ${out:0:70}"
case "$out" in /*|[A-Za-z]:*) c=0;; *) c=1;; esac  # an absolute path, POSIX or Windows
check "codecalc-prefetch-grammars prints a cache path" "$c" "-> ${out:0:70}"

# doctor must run and report (backend will be the python fallback on a
# pure-Python wheel — that is fine; existence + output is the property here).
run "$VPY/codecalc" doctor
check "codecalc doctor exits 0" "$rc"
case "$out" in *backend*) c=0;; *) c=1;; esac
check "codecalc doctor reports a backend" "$c" "-> $(printf '%s' "$out" | grep -i backend | head -1)"

if [ "$fail" -ne 0 ]; then
  echo "=== installed-path smoke: FAILURES above ==="
  exit 1
fi
echo "=== installed-path install smoke passed ==="
