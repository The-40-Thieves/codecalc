"""Entry point the MCPB "uv" runtime spawns for the codecalc extension.

`uv run --directory <bundle> src/server.py` (see ../manifest.json) resolves
`../pyproject.toml`'s one dependency — the published `codecalc[full]` wheel —
into an isolated venv on first launch, then runs this file. There is nothing
to implement here: codecalc.server.main() is the same stdio entry point the
`codecalc` console script calls, so this stub just calls straight through to
it rather than duplicating any of its argv handling (see
codecalc/server.py's own docstring for what that handles).
"""

from codecalc.server import main

if __name__ == "__main__":
    main()
