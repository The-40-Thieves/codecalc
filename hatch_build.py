"""Custom hatchling build hook: bundle the Rust executor into the wheel.

Without this, the wheel packages only `codecalc/`. `bin/codecalc-exec*` and
`bin/blocknet.so` are gitignored (see .gitignore — they are build artifacts,
not source), so `pip install codecalc` produced a package with no native
binary at all, and `codecalc.executor` silently fell back to the pure-Python
backend — which cannot enforce `no_net` (it says so in its own `unenforced`
field). See GitHub issue #24.

This hook does three things for a wheel build:
  1. force_include the platform's `bin/codecalc-exec[.exe]` to `codecalc/bin/`
     inside the installed package, if it was built
     before `hatch build`/`uv build` ran (see README.md "Build the Rust core").
  2. force_include the matching `bin/blocknet.so` / `bin/blocknet.dylib` beside it
     shim on the platforms that have one. The shim and the binary must travel
     together — `executor/build.rs` documents why a binary without its
     version-matched shim silently enforces a stale (or no) `--no-net` policy.
  3. set `pure_python = False` and take the tag from CODECALC_WHEEL_TAG (never
     `infer_tag`, which ABI-pins to the build interpreter) so the wheel gets a real
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
import sysconfig
from pathlib import Path
from typing import Any

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ImportError:  # pragma: no cover — test environments without hatchling
    # Only hatchling itself ever instantiates the hook class. Guarding the
    # import keeps `missing_release_artifacts` importable by the test suite,
    # whose environment installs runtime dependencies, not build backends.
    BuildHookInterface = object  # type: ignore[assignment,misc]

_EXE_NAME = "codecalc-exec.exe" if os.name == "nt" else "codecalc-exec"

#: Release-only fail-closed switch (THE-836). The warn-and-degrade behaviour
#: below is right for a dev checkout — a missing compiler must not block
#: `pip install` — and wrong for a release build, where the same degradation
#: silently publishes a pure-Python artifact whose installs cannot enforce
#: no_net. The release workflow sets this; a dev build never needs to.
REQUIRE_BINARY_ENV = "CODECALC_REQUIRE_BINARY"


def missing_release_artifacts(exe_exists: bool, shim_name: str | None,
                              shim_exists: bool) -> list[str]:
    """What a fail-closed release build is missing, by name. Pure, testable.

    `shim_name` is None on platforms that have no shim (Windows) — an absent
    shim there is not a missing artifact, it is the platform.
    """
    missing = []
    if not exe_exists:
        missing.append("codecalc-exec")
    if shim_name is not None and not shim_exists:
        missing.append(shim_name)
    return missing

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

        if os.environ.get(REQUIRE_BINARY_ENV):
            shim_name = _SHIM_NAME_BY_SYSTEM.get(platform.system().lower())
            shim_exists = shim_name is not None and (bin_dir / shim_name).is_file()
            missing = missing_release_artifacts(exe.is_file(), shim_name, shim_exists)
            if missing:
                raise RuntimeError(
                    f"{REQUIRE_BINARY_ENV} is set and bin/ is missing "
                    f"{', '.join(missing)} — refusing to build a release wheel "
                    "that would silently downgrade installs to the pure-Python "
                    "sandbox (no no_net enforcement). Build the executor first, "
                    "or unset the variable for a dev build."
                )

        if not exe.is_file():
            self.app.display_warning(
                f"{exe} not found — building a PURE-PYTHON wheel with no native "
                "executor bundled. `pip install` from it will silently fall back "
                "to the weaker Python sandbox, which cannot enforce no_net. "
                "Build the Rust executor first (see README.md 'Build the Rust "
                "core') before running `hatch build` / `uv build --wheel`."
            )
            return

        build_data["force_include"][str(exe)] = f"codecalc/bin/{_EXE_NAME}"

        shim_name = _SHIM_NAME_BY_SYSTEM.get(platform.system().lower())
        if shim_name is not None:
            shim = bin_dir / shim_name
            if shim.is_file():
                build_data["force_include"][str(shim)] = f"codecalc/bin/{shim_name}"
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

        # A real platform tag instead of py3-none-any: this wheel carries a
        # platform-specific binary, so it must be installable-filtered as one.
        build_data["pure_python"] = False

        # But NOT infer_tag. That reads the tag off the interpreter running the
        # build and produced `cp311-cp311-linux_aarch64` — ABI-pinned to one
        # CPython version for a payload that contains no CPython extension at
        # all. `requires-python = ">=3.11"` would then be contradicted by the
        # artifact: a 3.12 user gets no wheel and silently falls back to source,
        # i.e. to the Python sandbox that cannot enforce no_net. The binary is a
        # standalone executable; nothing about it is ABI-specific.
        #
        # CODECALC_WHEEL_TAG lets the release matrix state the target tag
        # explicitly, which is also the only way to say `musllinux` — a static
        # musl build produced on a glibc runner is otherwise tagged manylinux
        # and refused on Alpine.
        tag = os.environ.get("CODECALC_WHEEL_TAG")
        if tag:
            build_data["tag"] = tag
            return
        plat = sysconfig.get_platform().replace("-", "_").replace(".", "_")
        build_data["tag"] = f"py3-none-{plat}"
        self.app.display_warning(
            f"CODECALC_WHEEL_TAG not set — tagging this wheel py3-none-{plat} "
            "from the build host. Correct for a local build; the release "
            "workflow sets the tag per target because the host cannot know "
            "whether a static binary should be published as manylinux, "
            "musllinux, or both."
        )
