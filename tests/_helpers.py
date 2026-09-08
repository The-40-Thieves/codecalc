"""Shared helpers for the codecalc test suite.

`from _helpers import ...` works the same way `from _mcp_client import ...`
already does elsewhere in this directory: pytest collection is disabled here
(see conftest.py), each file is run as `python tests/test_whatever.py`, and
the interpreter puts the script's own directory first on `sys.path` — no
package, no relative import, just a sibling module.
"""

from __future__ import annotations

import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from codecalc import executor


def resolve_native_executor() -> pathlib.Path | None:
    """Resolve the Rust `codecalc-exec` binary a test should exercise.

    Several test files used to hardcode `REPO_ROOT / "bin" / "codecalc-exec"`
    and gate their "rust:" branches on that exact path's `.exists()` — which
    ignores `CODECALC_EXEC_BIN` entirely. A local run that points
    `CODECALC_EXEC_BIN` at, say, `executor/target/release/codecalc-exec`
    with an empty `bin/` silently skipped every "rust:" assertion with no
    SKIP line printed, and setting `CODECALC_REQUIRE_NATIVE=1` alongside it
    did nothing to catch the mistake, because that hardcoded check never
    consulted the variable at all. That is how two stale exit_code
    assertions passed locally and only failed in CI's sandbox job, which
    builds the binary straight into `bin/`.

    This resolves the SAME way `codecalc/executor.py` does at import time —
    `CODECALC_EXEC_BIN` first, then the arch-aware `bin/` candidates — by
    calling its own `_rust_binary()` rather than duplicating that ordering
    here, so the two can never drift apart again.

    Returns the resolved path, or `None` when nothing usable was found —
    printing exactly one loud `SKIP native executor: <reason>` line in that
    case so an empty "rust:" section is never silent. When
    `CODECALC_REQUIRE_NATIVE=1` is set and nothing resolves, raises instead
    of returning `None`, matching this repo's existing fail-closed
    convention (`executor._require_native_or_die`) — a run that means to
    test the native backend must not quietly fall through to skipping it.

    On success, also prints one `native executor: <path>` line — the CI
    existence floor for a test file whose checks are not themselves named
    "rust:" (see the sandbox job in .github/workflows/ci-python.yml, which
    greps this line the same way it greps `PASS rust:` elsewhere).
    """
    found = executor._rust_binary()
    if found is not None:
        print(f"native executor: {found}")
        return pathlib.Path(found)
    reason = (
        "no usable codecalc-exec binary found (checked $CODECALC_EXEC_BIN "
        f"and {executor._binary_candidates()}) — build it with `cargo build "
        "--release --manifest-path executor/Cargo.toml` or point "
        "CODECALC_EXEC_BIN at a working binary"
    )
    if os.environ.get(executor.REQUIRE_NATIVE_ENV, "0") != "0":
        raise RuntimeError(f"{executor.REQUIRE_NATIVE_ENV} is set, but {reason}")
    print(f"SKIP native executor: {reason}")
    return None


def expected_tool_count() -> int:
    """The tool count README.md claims ("as **N MCP tools**"), the one number
    `scripts/check_claims.py` already gates against the live registry.

    Tests that assert how many tools tools/list serves used to hard-code the
    number; every tool added then had to touch three test files, and a branch
    rebased across another branch's tool addition failed on the stale literal
    (the count changed on main without the literal changing here). Reading
    the README's gated claim keeps ONE source of truth: check_claims proves
    README == registry, and these tests prove served == README.
    """
    import re
    from pathlib import Path
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    m = re.search(r"as \*\*(\d+) MCP tools\*\*", readme)
    if m is None:
        raise RuntimeError("README.md no longer states 'as **N MCP tools**'; update expected_tool_count()")
    return int(m.group(1))

