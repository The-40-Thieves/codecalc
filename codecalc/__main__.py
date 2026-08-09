"""`python -m codecalc` — the same entry point as the `codecalc` script.

`[project.scripts]` already gives `codecalc` a console script pointing at
`server:main`, so `uvx codecalc` and `pipx run codecalc` have always worked.
What did not work was `python -m codecalc`, which needs this file and failed
with "No module named codecalc.__main__" — the form people reach for when a
package is installed but its script directory is not on PATH, which is the
normal state inside a venv someone has not activated.

Deliberately thin. Anything that lives here is invisible to the console-script
path and would be a second way for the two to diverge.
"""

from __future__ import annotations

from .server import main

if __name__ == "__main__":
    main()
