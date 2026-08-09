"""Custom hatchling build hook: bundle the Rust executor into the wheel.

Without this, the wheel packages only `codecalc/`. `bin/codecalc-exec*` and
`bin/blocknet.so` are gitignored (see .gitignore — they are build artifacts,
not source), so `pip install codecalc` produced a package with no native
binary at all, and `codecalc.executor` silently fell back to the pure-Python
backend — which cannot enforce `no_net` (it says so in its own `unenforced`
field). See GitHub issue #24.

This hook does three things for a wheel build:
  1. force_include the platform's `bin/codecalc-exec[.exe]`, if it was built
     before `hatch build`/`uv build` ran (see README.md "Build the Rust core").
  2. force_include the matching `bin/blocknet.so` / `bin/blocknet.dylib`
     shim on the platforms that have one. The shim and the binary must travel
     together — `executor/build.rs` documents why a binary without its
     version-matched shim silently enforces a stale (or no) `--no-net` policy.
  3. set `pure_python = False` and `infer_tag = True` so the wheel gets a real
     platform tag instead of `py3-none-any` — a platform-tagged wheel with no
     binary inside would be worse than an honest pure-Python one.

If no binary is present (e.g. building an sdist, or `uv build` on a checkout
that never ran `cargo build`), this degrades to an ordinary pure-Python wheel
and prints a loud warning rather than failing the build — a missing compiler
must not block `pip install` on platforms that don't need one, matching how
`executor/build.rs` treats a missing C compiler for the shim itself.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_EXE_NAME = "codecalc-exec.exe" if os.name == "nt" else "codecalc-exec"

#: Matches codecalc/executor.py's `os.name == "nt"` / POSIX split: Windows has
#: no LD_PRELOAD equivalent, so there is no shim to bundle there at all — the
#: executor reports `--no-net` as unenforced on Windows instead of pretending.
_SHIM_NAME_BY_SYSTEM = {
    "linux": "blocknet.so",
    "darwin": "blocknet.dylib",
}


class ExecutorBuildHook(BuildHookInterface):
    """force_includes the Rust executor (+ its --no-net shim) into the wheel."""

    PLUGIN_NAME = "codecalc-executor"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        bin_dir = root / "bin"
        exe = bin_dir / _EXE_NAME

        if not exe.is_file():
            self.app.display_warning(
                f"{exe} not found — building a PURE-PYTHON wheel with no native "
                "executor bundled. `pip install` from it will silently fall back "
                "to the weaker Python sandbox, which cannot enforce no_net. "
                "Build the Rust executor first (see README.md 'Build the Rust "
                "core') before running `hatch build` / `uv build --wheel`."
            )
            return

        build_data["force_include"][str(exe)] = f"bin/{_EXE_NAME}"

        shim_name = _SHIM_NAME_BY_SYSTEM.get(platform.system().lower())
        if shim_name is not None:
            shim = bin_dir / shim_name
            if shim.is_file():
                build_data["force_include"][str(shim)] = f"bin/{shim_name}"
            else:
                self.app.display_warning(
                    f"{shim} not found — packaging codecalc-exec WITHOUT its "
                    "--no-net shim. Every install from this wheel will report "
                    "no_net=True as unenforced (no_net_requested_but_no_shim_"
                    "available) regardless of platform support. Build both "
                    "together: `cd executor && cargo build --release` builds "
                    "the shim via build.rs, then copy both into bin/ (see "
                    "README.md 'Build the Rust core')."
                )

        # A real platform tag (e.g. cp312-cp312-manylinux_..., ...-macosx_...,
        # ...-win_amd64) instead of py3-none-any — this wheel now carries a
        # platform-specific binary, so it must be installable-filtered as one.
        build_data["pure_python"] = False
        build_data["infer_tag"] = True
