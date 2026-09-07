"""Entry point the MCPB "uv" runtime spawns for the codecalc extension.

`uv run --directory <bundle> src/server.py` (see ../manifest.json) resolves
`../pyproject.toml`'s one dependency — the published `codecalc[full]` wheel —
into an isolated venv on first launch, then runs this file. There is nothing
to implement here: codecalc.server.main() is the same stdio entry point the
`codecalc` console script calls, so this stub just calls straight through to
it rather than duplicating any of its argv handling (see
codecalc/server.py's own docstring for what that handles).
"""

import platform
import sys

# ../pyproject.toml's dependency marker excludes Windows on ARM64 (codecalc's
# own chain — mcp -> pyjwt[crypto] -> cryptography — ships no win_arm64 wheel
# on PyPI; see that file's own comment and release.yml's windows-aarch64
# matrix leg for the same gap). On that one platform `codecalc` therefore
# never gets installed into the venv `uv run` just built, so importing it
# below would fail with a bare ModuleNotFoundError. Caught here, before that
# import, so the failure names its actual cause instead.
#
# 'ARM64' (uppercase) is the exact `platform.machine()`/marker value on
# native Windows ARM64 Python — matches ../pyproject.toml's marker exactly.
# 'aarch64' is kept alongside it defensively, in case some interpreter
# reports the lowercase POSIX-style spelling on that platform instead; it is
# not the documented value and is not expected to ever match here.
if sys.platform == "win32" and platform.machine() in ("ARM64", "aarch64"):
    print(
        "codecalc cannot start here: Windows on ARM64 has no cryptography "
        "wheel on PyPI (a codecalc dependency), so it was never installed. "
        "Workaround: run this extension under x86_64/x64 Python (Windows' "
        "own x64 emulation), or wait for a cryptography win_arm64 wheel.",
        file=sys.stderr,
    )
    raise SystemExit(1)

from codecalc.server import main

if __name__ == "__main__":
    main()
