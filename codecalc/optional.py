"""Heavy dependencies, imported only when a tool actually needs one.

sympy (40.4 MB), z3-solver (43.1 MB) and tree-sitter-language-pack (5.0 MB) are
88.6 MB measured — most of the install, for tools a caller may never touch.
They are extras now, so the base install is the executor and the MCP surface.

WHAT THIS FILE IS FOR, and it is not the import:
every one of those imports was ALREADY lazy, inside the function that needed
it. What was missing is what happens when the import fails. An `ImportError:
No module named 'sympy'` escaping a tool tells a caller that the server is
broken, when the truth is that they installed the small variant and asked for a
tool outside it. That is a different problem with a different fix, and the
message has to say which.

So this raises `MissingExtra`, whose text names the extra, the install command,
and the tools that need it. The symbolic tools run inside `guarded_call`, which
turns any exception into `{"ok": false, "error": "<type>: <message>"}` — so the
message IS the error the caller reads, and it is written to be actionable
there rather than in a traceback nobody sees.
"""

from __future__ import annotations

import importlib
from types import ModuleType

#: extra -> (pip target, what it unlocks). The second half is in the error
#: message because "install codecalc[symbolic]" answers how, not whether: a
#: caller who wanted `analyze_complexity` should not have to guess which of two
#: extras is the one they are missing.
_EXTRAS = {
    "symbolic": (
        "codecalc[symbolic]",
        ("evaluate_expression, simplify_expression, solve_expression, "
         "limit_expression, algebraic_equiv, solve_linear, truth_table, "
         "z3_check, convert_units"),
    ),
    "parsing": (
        "codecalc[parsing]",
        "analyze_complexity and the tree-sitter static analysis",
    ),
}

#: module -> extra that provides it.
_PROVIDED_BY = {
    "sympy": "symbolic",
    "z3": "symbolic",
    "tree_sitter_language_pack": "parsing",
}


class MissingExtra(ImportError):
    """A tool needs an optional dependency that is not installed.

    Subclasses ImportError on purpose: any caller already catching ImportError
    around these lazy imports keeps working, and gets a better message than the
    one it was catching before.
    """

    def __init__(self, module: str, extra: str) -> None:
        target, unlocks = _EXTRAS[extra]
        self.module = module
        self.extra = extra
        super().__init__(
            f"{module} is not installed. It ships in the '{extra}' extra: "
            f"pip install '{target}'  (or 'codecalc[full]' for everything). "
            f"This affects: {unlocks}."
        )


def require(module: str) -> ModuleType:
    """Import `module`, or raise MissingExtra naming how to get it."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        extra = _PROVIDED_BY.get(module.split(".")[0])
        if extra is None:
            raise
        raise MissingExtra(module.split(".")[0], extra) from exc


def have(module: str) -> bool:
    """Whether `module` can be imported, without importing it.

    Used by `codecalc doctor` and by list_languages-style reporting, where the
    question is what this install can do rather than doing it. `find_spec` does
    not execute the module, so asking is cheap — which matters, because the
    whole point of the extras is that a caller who never touches sympy never
    pays sympy's 437ms import.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False
