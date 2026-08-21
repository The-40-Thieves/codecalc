#!/usr/bin/env python3
"""From-source entry point for the grammar prefetch. See `codecalc.prefetch`.

the actual logic now lives in `codecalc/prefetch.py` so it ships in
the wheel and gets a console script (`codecalc-prefetch-grammars`) an
installed user can run — `scripts/` is not packaged
(`[tool.hatch.build.targets.wheel] packages = ["codecalc"]`), so this file
alone was unreachable after `uv tool install codecalc` even though the README
told people to run it. This wrapper stays so the from-source workflow
(`python scripts/prefetch_grammars.py`, CI's grammar-cache step) keeps
working unchanged, calling the one implementation rather than a forked copy.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from codecalc.prefetch import main  # noqa: E402 — needs the path above

if __name__ == "__main__":
    sys.exit(main())
