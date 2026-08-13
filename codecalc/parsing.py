"""Real parsers for codecalc's languages, via tree-sitter.

Replaces the regex heuristics that `complexity.py` used to count loops and spot
recursion. Those regexes were wrong in both directions and could not be right:

    LOOP_RE matched the word "for" — including inside a string literal, a
    comment, or an identifier like `format()`. It also matched `.map(` and
    `.filter(`, so a chained pipeline read as several nested loops.

    RECURSION_RE found a function's body with a regex, then looked for the
    name inside it. A call to a DIFFERENT function with a matching prefix, or
    the name appearing in a docstring, both counted as recursion.

A parser does not have that class of error: a string literal is a string node,
a comment is a comment node, and a call is a call node with a resolvable
callee. Where a language's grammar cannot answer a question the analyser says
so rather than guessing.

Grammars come from `tree-sitter-language-pack`, which ships them PRE-COMPILED
with wheels for manylinux/macOS/win_amd64. That matters: building grammars at
install time would need a C toolchain on every platform and would have undone
the cross-platform work. All 32 registry entries resolve to a parser.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from functools import cache

from . import registry
from .optional import require

#: codecalc registry name -> tree-sitter grammar name, where they differ.
#: Anything absent uses the registry name unchanged. Deliberately explicit: a
#: silent fallback to "no parser" is how a language quietly stops being analysed.
GRAMMAR_ALIASES: dict[str, str] = {
    "python3": "python",
    "node": "javascript",
    "deno": "typescript",
    "bun": "typescript",
    "typescript": "typescript",
    "c++": "cpp",
    # zsh has no grammar of its own; bash's is close enough for structure
    # (loops, functions) and wrong only for zsh-specific syntax.
    "zsh": "bash",
    "sqlite": "sql",
}

#: tree-sitter Parser objects are NOT thread-safe and codecalc's MCP server is
#: async with a worker-thread pool, so parsers are per-thread rather than shared.
_local = threading.local()


def grammar_name(language: str) -> str | None:
    """tree-sitter grammar for a codecalc language, or None if unknown."""
    canon = registry.canonical(language)
    if canon is None:
        return None
    return GRAMMAR_ALIASES.get(canon, canon)


#: Why a grammar could not be loaded, keyed by grammar name (THE-820).
#:
#: The `except` below used to discard the exception, which made every possible
#: failure — the extra not installed, an ABI mismatch, a missing shared object,
#: a grammar this pack genuinely does not ship — arrive at the caller as the
#: same silent `False`. When tree-sitter stopped loading on py3.14 in CI, the
#: entire diagnosis available was `missing ['python3']`: true, useless, and
#: identical to what a language with no grammar looks like.
#:
#: The reason is KEPT rather than raised, because `parser_for` returning None is
#: the designed behaviour — callers fall back to the regex heuristic and say so.
#: What was wrong was throwing away why.
_GRAMMAR_ERRORS: dict[str, str] = {}


@cache
def _grammar_available(name: str) -> bool:
    try:
        pack = require("tree_sitter_language_pack")

        pack.get_language(name)
    except Exception as exc:
        # Type AND message: "ImportError" alone does not distinguish a missing
        # extra from a wheel that will not load on this interpreter, and those
        # need different fixes.
        _GRAMMAR_ERRORS[name] = f"{type(exc).__name__}: {exc}"
        return False
    _GRAMMAR_ERRORS.pop(name, None)
    return True


def grammar_error(name: str) -> str | None:
    """Why `name` has no parser, or None if it has one or was never asked.

    `_grammar_available` is cached, so ask it first: reading this without having
    asked reports None for a grammar that was never attempted, which is not the
    same as one that loaded.
    """
    return _GRAMMAR_ERRORS.get(name)


def parser_for(language: str):
    """A parser for `language`, or None when no grammar covers it.

    Returning None rather than raising is deliberate: callers fall back to the
    structural heuristic and SAY they did, which is better than failing a tool
    that used to work.
    """
    name = grammar_name(language)
    if name is None or not _grammar_available(name):
        return None
    cache = getattr(_local, "parsers", None)
    if cache is None:
        cache = _local.parsers = {}
    if name not in cache:
        pack = require("tree_sitter_language_pack")

        cache[name] = pack.get_parser(name)
    return cache[name]


def supported_languages() -> list[str]:
    """Registry languages that have a working parser."""
    return sorted(
        lang for lang in registry.LANGUAGES
        if (g := grammar_name(lang)) and _grammar_available(g)
    )


# ── node-type vocabulary ────────────────────────────────────────────────────
# tree-sitter node types are per-grammar, so a portable analyser needs a small
# vocabulary mapped across them. These are SUFFIX/substring matches against the
# node type, chosen by reading the grammars' actual node names rather than
# guessing: python has `for_statement`/`while_statement`, JS adds
# `for_in_statement`, Go has `for_statement` only, Ruby has `while`/`until`.

LOOP_NODES = (
    "for_statement", "while_statement", "do_statement", "repeat_statement",
    "for_in_statement", "for_of_statement", "foreach_statement", "loop_statement",
    "for_range_loop", "while", "until", "for_expression", "while_expression",
    "loop_expression", "do_while_statement", "enhanced_for_statement",
    "for_in_clause", "list_comprehension", "generator_expression",
)

FUNCTION_NODES = (
    "function_definition", "function_declaration", "method_definition",
    "function_item", "method_declaration", "function", "fn_item",
    "arrow_function", "lambda", "subroutine", "procedure_declaration",
    "constructor_declaration", "function_expression", "method",
)

CALL_NODES = ("call", "call_expression", "function_call", "method_invocation",
              "invocation_expression", "call_expression_statement")

#: Node types whose CONTENT must never be treated as code. The regex analyser's
#: single biggest source of error was counting the word "for" inside these.
INERT_NODES = ("comment", "string", "string_literal", "raw_string_literal",
               "line_comment", "block_comment", "char_literal", "string_content")


def _is(node_type: str, vocab: tuple[str, ...]) -> bool:
    return any(node_type == v or node_type.endswith("_" + v) for v in vocab)


@dataclass
class ParseFacts:
    """What the parser could actually determine about a snippet."""

    language: str
    grammar: str | None
    parsed: bool
    loops: int = 0
    max_loop_depth: int = 0
    functions: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    recursive_functions: list[str] = field(default_factory=list)
    has_error: bool = False
    #: Populated when the parser could not be used at all, so a caller can say
    #: WHY it fell back instead of silently reporting a heuristic as a parse.
    reason: str | None = None


def _node_text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf8", errors="replace")


def _callee_name(node, src: bytes) -> str | None:
    """Best-effort name of what a call node calls."""
    fn = node.child_by_field_name("function") or node.child_by_field_name("name")
    if fn is None and node.child_count:
        fn = node.child(0)
    if fn is None:
        return None
    text = _node_text(fn, src)
    # a.b.c(...) -> c ; obj->m(...) -> m
    for sep in (".", "->", "::"):
        if sep in text:
            text = text.rsplit(sep, 1)[-1]
    text = text.strip()
    return text or None


def analyse(code: str, language: str) -> ParseFacts:
    """Parse `code` and report structural facts. Never raises."""
    grammar = grammar_name(language)
    parser = parser_for(language)
    if parser is None:
        return ParseFacts(language=language, grammar=grammar, parsed=False,
                          reason=f"no tree-sitter grammar for {language!r}")

    src = code.encode("utf8")
    try:
        tree = parser.parse(src)
    except Exception as exc:  # a grammar can reject pathological input
        return ParseFacts(language=language, grammar=grammar, parsed=False,
                          reason=f"parse failed: {type(exc).__name__}: {exc}")

    facts = ParseFacts(language=language, grammar=grammar, parsed=True)
    fn_bodies: list[tuple[str, object]] = []

    def walk(node, loop_depth: int, fn_stack: list[str]) -> None:
        ntype = node.type
        # Only NAMED nodes are constructs. Keyword tokens are anonymous, and a
        # `while_statement` contains a bare `while` token as a child — counting
        # both double-counted every while loop in C and Rust. Caught because a
        # differential case reported 3 loops where the source has 2; the test
        # had used `>=` and would have let an over-counting analyser ship, which
        # is precisely the regex defect being replaced.
        if not node.is_named:
            return
        if node.is_error or ntype == "ERROR":
            facts.has_error = True
        # Never descend into a string or comment. This is the whole reason the
        # regex version over-counted.
        if _is(ntype, INERT_NODES):
            return

        here = loop_depth
        if _is(ntype, LOOP_NODES):
            facts.loops += 1
            here = loop_depth + 1
            facts.max_loop_depth = max(facts.max_loop_depth, here)

        stack = fn_stack
        if _is(ntype, FUNCTION_NODES):
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node, src) if name_node is not None else "<anonymous>"
            facts.functions.append(name)
            fn_bodies.append((name, node))
            stack = [*fn_stack, name]

        if _is(ntype, CALL_NODES):
            callee = _callee_name(node, src)
            if callee:
                facts.calls.append(callee)
                # Direct recursion: a call to a function we are lexically inside.
                if callee in stack and callee not in facts.recursive_functions:
                    facts.recursive_functions.append(callee)

        for child in node.children:
            walk(child, here, stack)

    walk(tree.root_node, 0, [])
    return facts
