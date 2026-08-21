"""Every extension's compat ranges are non-empty and current.

Standalone, like the other extension-SDK suites: a check() accumulator,
sys.exit(1) on any failure.

A cross-vendor review found `compatible_codecalc`/`compatible_contract` ranges
that were either an EMPTY range (a hardcoded upper bound equal to the running
minor — `">=0.3,<0.3"` at codecalc 0.3.1, which no version satisfies) or a
STALE range that no longer covers the running version (a hardcoded
`">=0.2,<0.3"` that excludes 0.3.1). This suite enumerates every built-in
extension (language packs, renderers, verifiers) AND the three example
extensions and asserts, for each manifest:

  1. `compatible_codecalc`/`compatible_contract` parse as `">=A.B,<C.D"`,
  2. the range is NON-EMPTY (lower < upper),
  3. the range INCLUDES the running `codecalc.__version__` /
     `contract.CONTRACT_VERSION`.

This must fail on the pre-fix code (verified by temporarily reverting one
range to `">=0.2,<0.3"` — see the PR description) and pass after
the fix, which computes every range from the running version via
`codecalc.extensions.codecalc_compat_range`/`contract_compat_range` instead of
hardcoding it.
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import __version__, contract, language_packs, renderers, verifiers
from examples.extensions.csv_renderer import CsvRenderer
from examples.extensions.lolcode_pack import LolcodePack
from examples.extensions.parity_verifier import ParityVerifier

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


# The lower bound is always "MAJOR.MINOR" (e.g. codecalc's `>=0.3` or the
# contract's `>=1.2`); the upper bound is open-ended on either the next MINOR
# (codecalc: `<0.4`) or the next MAJOR (contract: `<2`) — see
# `codecalc.extensions.codecalc_compat_range`/`contract_compat_range`. Both
# forms are the ">=A.B,<C[.D]" shape the ticket describes.
_RANGE_RE = re.compile(r"^>=(\d+)\.(\d+),<(\d+)(?:\.(\d+))?$")


def _version_tuple(version: str) -> tuple[int, int]:
    major, minor, *_patch = version.split(".")
    return int(major), int(minor)


def assert_compat_range(label: str, range_str: str, running_version: str) -> None:
    """Parse `range_str` as ">=A.B,<C[.D]", assert it is non-empty, and assert
    it includes `running_version`'s (major, minor)."""
    m = _RANGE_RE.match(range_str)
    check(f"[{label}] range parses as '>=A.B,<C[.D]'", m is not None, f"-> {range_str!r}")
    if m is None:
        return
    lower = (int(m.group(1)), int(m.group(2)))
    # An upper bound with no minor (e.g. "<2") means "anything below major 2":
    # pad it to (2, 0) so the (major, minor) comparison below is well-defined.
    upper = (int(m.group(3)), int(m.group(4)) if m.group(4) is not None else 0)
    check(f"[{label}] range is non-empty (lower < upper)", lower < upper,
          f"-> {range_str!r}")
    running = _version_tuple(running_version)
    check(f"[{label}] range includes the running version {running_version!r}",
          lower <= running < upper, f"-> {range_str!r} vs {running_version!r}")


# ── every extension whose manifest declares compat ranges ───────────────────
EXTENSIONS = [
    ("language_packs.BuiltinLanguagePack", language_packs.BuiltinLanguagePack()),
    ("renderers.TextRenderer", renderers.TextRenderer()),
    ("renderers.MarkdownTableRenderer", renderers.MarkdownTableRenderer()),
    ("verifiers.ExecutedVerifier", verifiers.ExecutedVerifier()),
    ("examples.lolcode_pack.LolcodePack", LolcodePack()),
    ("examples.csv_renderer.CsvRenderer", CsvRenderer()),
    ("examples.parity_verifier.ParityVerifier", ParityVerifier()),
]

check("EXTENSIONS enumerates every built-in kind (language_pack, renderer x2, verifier) "
      "and all three example extensions (lolcode, csv_renderer, parity_verifier)",
      len(EXTENSIONS) == 7, f"-> {len(EXTENSIONS)}")

for label, extension in EXTENSIONS:
    manifest = extension.manifest
    assert_compat_range(f"{label} compatible_codecalc", manifest.compatible_codecalc, __version__)
    assert_compat_range(f"{label} compatible_contract", manifest.compatible_contract,
                         contract.CONTRACT_VERSION)

# ── every built-in + example advertises the SAME range (single source of
# truth: the fix is shared helpers, not three independently-hardcoded
# copies that could drift again) ─────────────────────────────────────────────
codecalc_ranges = {label: extension.manifest.compatible_codecalc for label, extension in EXTENSIONS}
contract_ranges = {label: extension.manifest.compatible_contract for label, extension in EXTENSIONS}
check("every extension advertises the same compatible_codecalc range",
      len(set(codecalc_ranges.values())) == 1, f"-> {codecalc_ranges}")
check("every extension advertises the same compatible_contract range",
      len(set(contract_ranges.values())) == 1, f"-> {contract_ranges}")


print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== EXTENSION COMPAT RANGES OK ===")
sys.exit(1 if FAILS else 0)
